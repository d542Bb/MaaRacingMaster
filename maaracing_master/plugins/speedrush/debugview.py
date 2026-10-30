# -*- coding: utf-8 -*-
"""speedrush 实机调试图（treasure 规范接入）：raw + hud 双图，10fps 节流。

**职责与边界**：控制拍每 tick 喂一次快照（只入队引用，不复制不渲染）；IO 线程
按 10fps 节流渲染+落盘。渲染/写盘失败静默计数——调试图绝不干扰控制链（与
depth_geo 取证写手同姿态；后者是深度管线的证据包面，与本图互补、互不替代）。

**目录契约**（与录制语料同一套，见 ``session_layout``）：
  `raw/<seq:04d>_raw.jpg`   原始帧全幅（q95，人工金标标注/离线重渲染的底图）
  `<seq:04d>_hud.jpg`       双联渲染图（q85）：
    左联 = 画面叠加：YOLO 框（coin 黄 / car 红 / bonus 品红）+ 车道线观测叠加
           （黄/白线按拟合参数回投到画面，标注颜色/侧别/道号）+ 决策/供数读数带
           + HUD 比分读数（HudObserver 最新行，score/rate × a/b 槽位）
           + **底部输入带**：当帧实际手柄左摇杆杆量（人工局读物理手柄；自动局
           显示程序下发值）+ 程序反事实杆量标记 + 右扳机油门值 + 线观测状态摘要。
           人工对照局用它直接核对「车是被人打歪的还是被程序带歪的」。
    右联 = BEV 雷达：可行驶栅格三态 + 挖洞格 + 当帧墙检出折线（edge_pts 两侧，
           供数窗内「两边墙」可见）+ 路心/左右缘叠层（C=滤波 ro、c0=当帧原始
           (L+R)/2，两者分离=时间链效应；cg=同拍栅格 wmid 路心，与 c0 分离=
           互证分歧面）+ 分带走廊边界（来源着色）+ **走廊中心带（±band 虚线）**
           + 检测反投影标记（40m 外钉
           顶缘+距离标注）+ 距离标尺 + 三层窗口标注 + **选择层子带**（横轴秒、
           纵轴道：机会点/路点/目标/自车）+ **转向力度-时间子带**（最近 6s
           杆量，上=R/下=L，0.45 与满量程参考线）

**选择层子带**（2026-10-18 走廊重建）：不再画已删除的轨迹候选集。横轴
[0, 3s]、纵轴道；黄点=金币组、红点=普通车、品红方块=奖励车（选择层坐标，
右正）；绿线=选择路点；青线=本拍目标；白线=自车（人工录制局=人工横位）。
选择器看到的和它选的东西在同一张图上，帧间可直接对账。

**BEV 口径**（与 _grid_penalty 同源，不另立坐标）：栅格 x 原点=相机光轴=自车，
格 (ix, iz) → X=ix·CELL−X_MAX、Z=Z_LO+iz·CELL；**渲染近下远上**（z=Z_LO 在
画布底，与 depth_geo 取证面板 z3(bottom)..z16(top) 同约定，自车标在底=近端）；
路心系 d（道）→ 相机轴米 X=(d−ro)·lane_w_m。检测标记：框底中心像素经同帧焦距
+ 路面平面反投影（Z=c/(v−a·u−b)，X=u·Z），域外点丢弃。

**为什么选择层用时间轴**：路点/机会带的是到站时间 t，不是纵深 z（金币没有
可靠的米制 z）；臆造 t→z 映射就是把假数据画进证据面。以 (t, 道) 子带呈现，
横轴固定 [0, 3s]、纵轴 ±2.5 道，帧间可比。
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core.debug import _put_text
from maaracing_master.plugins.speedrush.depth_geo import (
    ANCHOR_Z_HI, CAM_Z_DATUM_M, GRID_BLOCKED, GRID_CELL, GRID_DRIVABLE,
    GRID_NX, GRID_NZ, GRID_UNKNOWN, GRID_X_MAX, GRID_Z_HI,
    GRID_Z_LO, NEAR_Z, REGION_WIDTH_Z_HI, REGION_Z_HI, ROAD_Z_HI,
    X_MAX_M)
from maaracing_master.plugins.speedrush.lane_obs import (
    DEFAULT_CAM_H, FOCAL, LANE_W, VPX, Y_H)
from maaracing_master.plugins.speedrush.session_layout import (
    SOURCE_CONTROL,
    build_meta,
    hud_relpath,
    raw_relpath,
)
from maaracing_master.plugins.speedrush.world_model import (
    Calib, load_calib)

# 渲染用车半宽（米）：只影响图上的车身圆点大小，不参与行为判定
_CAR_HALF_M = 0.9

# 10Hz 取证（2026-10-24 从逐拍下调）：控制拍 ≈20Hz，调试图按 0.1s 节流，
# 与 trace 的 20Hz 同钟但不同密度——人工对照局看得清又不淹没磁盘。
# 磁盘代价 ≈ 10 对/秒 × (raw ~0.2MB + hud ~0.3MB) ≈ 5MB/s；IO 线程跟不上时
# 由有界队列丢旧保新，主循环只做 put_nowait，绝不阻塞。
DEBUG_FPS = 10
DEBUG_INTERVAL_S = 1.0 / DEBUG_FPS
_QUEUE_MAX = 20                   # 约 2 秒 @10Hz；满则丢旧快照，保最新
_RAW_JPEG_QUALITY = 95
_HUD_JPEG_QUALITY = 85

# 三态着色（BGR）：unknown 灰 / drivable 暗绿 / blocked 红；挖洞格橄榄描重
_STATE_BGR = {GRID_UNKNOWN: (64, 64, 64),
              GRID_DRIVABLE: (70, 140, 70),
              GRID_BLOCKED: (50, 50, 220)}
_DIG_BGR = (30, 100, 110)
_CLS_BGR = {"coins": (0, 255, 255), "cars": (0, 0, 255),
            "bonuses": (255, 0, 255)}
_STEER_FULL = 32767.0             # 手柄杆量满量程（steer_x 原始值）
_STEER_HIST_MAX = 121             # 转向时间带：6s @ 20Hz + 当前拍

_PANEL_W = 560                    # 右联宽
_TITLE_H = 26                     # 右联标题条
_Z_TICK_M = (3.0, 6.0, 9.0, 12.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0)
# 距离口径全图统一=车头系前向距离（2026-10-10 挪正：标签=相机系 z 减
# CAM_Z_DATUM_M，0m=车头着地点；供数窗/检测反投影标注同口径对账一致。
# 旧口径"前向距离"以相机为原点，"3m"其实在车底下——用户指正挪正）
_X_TICK_M = (-10.0, -5.0, 0.0, 5.0, 10.0)
_RENDER_Z_HI = 40.0               # BEV 渲染上限（> 栅格采集窗 30m）：目检延伸区
_PLOT_Y1_FRAC = 0.66              # BEV 绘图区底 = 画布高 × 此值（竖长化，下余 d(t) 子带）
_LIMIT_BGR = (0, 215, 255)        # 程序接受域上限虚线（亮黄）
_EDGE_BGR = (0, 255, 170)         # 当帧原始墙检出折线（edge_pts）
# 走廊边界来源配色（第 6 步）：每带每侧的边界假设按来源画——实测墙=亮橙实线，
# 遮挡墙（历史证据）=暗橙虚线，空气墙（先验，永远 inferred）=紫虚线
_COR_SRC_BGR = {"measured": (0, 170, 255),
                "occluded": (30, 110, 190),
                "inferred": (255, 90, 255)}


@dataclass(frozen=True)
class _BevMap:
    """BEV 绘图映射：栅格域 → 右联画布像素。近端（z=Z_LO）在画布**底**、远端
    在顶（与 depth_geo 取证面板 z3(bottom)..z16(top) 同约定，自车标在底=近端）。"""

    x0: int                      # 绘图区左
    y1: int                      # 绘图区底（近端 z=GRID_Z_LO）
    sx: float                    # 像素/格（横向）
    sy: float                    # 像素/格（纵深）

    def px(self, x_m: float) -> int:
        return int(self.x0 + (x_m + GRID_X_MAX) / GRID_CELL * self.sx)

    def py(self, z_m: float) -> int:
        return int(self.y1 - (z_m - GRID_Z_LO) / GRID_CELL * self.sy)


def _project_detection(px: int, py: int, coef, fx: float, fy: float,
                       frame_w: int, frame_h: int) -> tuple[float, float] | None:
    """检测框底中心像素 → 路面 (X, Z) 米（相机轴系）；退化/域外返回 None。

    z 上限=深度锚点有效域（ANCHOR_Z_HI=90m，真源 depth_geo）——40m 外的
    远距检测照常投影，画不画、画在哪由消费端定（钉绘图区顶缘+距离标注，
    「程序能看多远」一目了然）；程序供数仍止步于栅格窗。"""
    if coef is None or not fx or not fy:
        return None
    a, b, c = coef
    u = (px - frame_w / 2.0) / fx
    v = (py - frame_h / 2.0) / fy
    den = v - a * u - b
    if abs(den) < 1e-6:
        return None
    z = c / den
    x = u * z
    if not (GRID_Z_LO <= z <= ANCHOR_Z_HI):
        return None
    return x, z


def _edge_side_pts(eps, side: int) -> list[tuple[float, float]]:
    """当帧原始墙检出分侧按 z 排序（(z_m, x_m) 相机轴米）——检出折线的数据面。"""
    return sorted(((z, x) for s, z, x in eps if s == side), key=lambda p: p[0])


# 车道线叠加配色：黄边界亮黄、白线亮白、无法判色品红（与线观测语义同源）
_LANE_BGR = {"y": (0, 220, 255), "w": (235, 235, 235), "?": (255, 0, 255)}
_BAND_BGR = (18, 18, 18)


def _lane_line_x(line: dict, cam_h: float, y: float) -> float:
    """按线观测的拟合参数回投全幅像素横坐标。

    线方程（lane_obs 同源）：``x_px = a + s*y``，其中 ``s=L*LANE_W/cam_h``，
    截距由 theta 反解 ``a = VPX - s*Y_H - tan(theta)*FOCAL``。参数齐备，
    不需要重新推理——图上画的就是观测器实际拟合的那根线。
    """
    L = float(line.get("L") or 0.0)
    th = float(line.get("theta") or 0.0)
    s = L * LANE_W / cam_h
    a = VPX - s * Y_H - math.tan(th) * FOCAL
    return a + s * y


def _draw_lane_overlay(img: np.ndarray, snap: dict) -> None:
    """左联：把线观测的拟合线回投到游戏画面（黄/白/未知按颜色着色）。

    只画观测器实际锁到的线（``lines`` 来自最近一次检测，含证据龄），
    预测帧沿用同一组线并保留标注。坐标按 1280×720 设计域缩放，帧尺寸
    变化不跑位；线与 YOLO 框同图，空间对应关系可直接目检。
    """
    lane = snap.get("lane")
    if not lane:
        return
    lines = lane.get("lines") or []
    if not lines:
        return
    H, W = img.shape[:2]
    sx, sy = W / 1280.0, H / 720.0
    cam_h = lane.get("cam_h")
    if not (isinstance(cam_h, (int, float)) and 0.5 <= float(cam_h) <= 3.0):
        cam_h = DEFAULT_CAM_H
    cam_h = float(cam_h)
    # 检测帧与当前控制帧的差：按线的跨帧速度把 L 外推到当前帧再画，
    # 消掉「图上线比车慢一拍」的肉眼延迟（身份/k 仍用检测帧的值）。
    det_fid = lane.get("det_fid")
    cur_fid = snap.get("fid")
    gap = 0
    if isinstance(det_fid, int) and isinstance(cur_fid, int):
        gap = max(0, min(6, cur_fid - det_fid))
    for ln in lines:
        if ln.get("L") is None:
            continue
        try:
            y0 = float(ln.get("y0"))
            y1 = float(ln.get("y1"))
        except (TypeError, ValueError):
            continue
        if y1 <= y0:
            continue
        y0 = max(0.0, min(720.0, y0))
        y1 = max(0.0, min(720.0, y1))
        if y1 <= y0:
            continue
        use = dict(ln)
        if gap:
            use["L"] = float(ln["L"]) + float(ln.get("v") or 0.0) * gap
        xa = _lane_line_x(use, cam_h, y0) * sx
        xb = _lane_line_x(use, cam_h, y1) * sx
        acc = bool(ln.get("accepted", True))
        col = _LANE_BGR.get(ln.get("color"), _LANE_BGR["?"])
        if not acc:
            col = (120, 120, 120)      # 被残差/等间距门淘汰：灰线保留可审计
        # 黄边界是绝对路心锚，画粗一档；白线只作补充
        thick = 2 if (acc and ln.get("color") == "y") else 1
        cv2.line(img, (int(xa), int(y0 * sy)), (int(xb), int(y1 * sy)),
                 col, thick)
        cv2.circle(img, (int(xb), int(y1 * sy)), 2, col, -1)
        k = ln.get("k")
        resid = ln.get("resid")
        label = f"k{k:+d}" if k is not None else "k-"
        if isinstance(resid, (int, float)):
            label += f" r{resid:+.2f}"
        if not acc and ln.get("reject"):
            label += f" {ln['reject']}"
        lx = int(min(max(xb + 3, 2), W - 2))
        ly = int(min(max(y1 * sy + 12, 12), H - 68))   # 避开底部输入带
        _put_text(img, label, (lx, ly), scale=0.36, color=col)


def _draw_input_band(img: np.ndarray, snap: dict) -> None:
    """左联底部输入带：实际杆量 + 程序反事实 + 油门窗值 + 线观测摘要。

    人工对照局读物理手柄（gpad=None 时快照带 ``pad``），自动局则显示程序
    下发值；两者同时在场时程序值只画细标记，避免和「人实际打了多少」混淆。
    """
    H, W = img.shape[:2]
    band_h = 64
    y0 = H - band_h
    overlay = img.copy()
    cv2.rectangle(overlay, (0, y0), (W, H), _BAND_BGR, -1)
    cv2.addWeighted(overlay, 0.62, img, 0.38, 0, img)
    cv2.line(img, (0, y0), (W, y0), (80, 80, 80), 1)

    dec = snap.get("decision") or {}
    pad = snap.get("pad") or {}
    cmd = dec.get("steer")
    pad_x = pad.get("lx")
    rt = pad.get("rt")
    has_pad = isinstance(pad_x, (int, float)) and not isinstance(pad_x, bool)
    primary = pad_x if has_pad else cmd
    label = "pad" if has_pad else "cmd"

    xa, xb = 66, max(240, W - 340)
    cx = (xa + xb) // 2
    half = max(40, (xb - xa) // 2)
    ybar = y0 + 20
    cv2.rectangle(img, (xa - 2, ybar - 7), (xb + 2, ybar + 7),
                  (48, 48, 48), -1)
    cv2.line(img, (cx, ybar - 10), (cx, ybar + 10), (190, 190, 190), 1)
    for s in (-1, 1):
        for frac in (0.45, 1.0):
            x = int(cx + s * frac * half)
            for xx in range(min(x, cx), max(x, cx), 8):
                cv2.line(img, (xx, ybar - 3), (min(xx + 4, x), ybar - 3),
                         (105, 105, 105), 1)
    _put_text(img, label, (6, ybar + 4), scale=0.4, color=(220, 220, 220))
    if isinstance(primary, (int, float)) and not isinstance(primary, bool):
        frac = max(-1.0, min(1.0, float(primary) / 32767.0))
        xv = int(cx + frac * half)
        col = (0, 200, 0) if frac >= 0 else (0, 120, 255)
        cv2.rectangle(img, (min(cx, xv), ybar - 6), (max(cx, xv), ybar + 6),
                      col, -1)
        txt = f"{frac * 100:+.0f}% {'R' if frac >= 0 else 'L'}"
        _put_text(img, txt, (xb + 10, ybar + 4), scale=0.38, color=col)
    else:
        _put_text(img, "n/a", (xb + 10, ybar + 4), scale=0.38,
                  color=(160, 160, 160))
    extra_x = xb + 116
    if has_pad and isinstance(cmd, (int, float)) and not isinstance(cmd, bool):
        cfrac = max(-1.0, min(1.0, float(cmd) / 32767.0))
        xv = int(cx + cfrac * half)
        cv2.drawMarker(img, (xv, ybar), (255, 255, 255),
                       cv2.MARKER_TRIANGLE_DOWN, 8, 1)
        _put_text(img, f"prog {cfrac * 100:+.0f}%",
                  (extra_x, ybar + 4), scale=0.36, color=(200, 200, 200))
        extra_x += 126
    if isinstance(rt, (int, float)) and not isinstance(rt, bool):
        _put_text(img, f"RT {int(rt)}", (extra_x, ybar + 4),
                  scale=0.36, color=(180, 180, 180))

    lane = snap.get("lane")
    ly = y0 + 50
    if not lane:
        _put_text(img, "lane: none", (6, ly), scale=0.38,
                  color=(0, 200, 255))
        return
    state = lane.get("state", "-")
    age = lane.get("det_age_ms")
    lines = lane.get("lines") or []
    parts = [f"lane {state}"]
    anchor_src = lane.get("anchor_src")
    if anchor_src:
        parts.append(f"anchor={anchor_src}")
    x = lane.get("x")
    th = lane.get("theta")
    if isinstance(x, (int, float)):
        parts.append(f"x={x:+.2f}")
    if isinstance(th, (int, float)):
        parts.append(f"th={th:+.2f}")
    if isinstance(age, (int, float)):
        parts.append(f"det={age:.0f}ms")
    kept = lane.get("n_locked")
    parts.append(f"lines={len(lines)} kept={0 if kept is None else kept}")
    resids = [ln.get("resid") for ln in lines
              if ln.get("accepted") and isinstance(ln.get("resid"),
                                                   (int, float))]
    if resids:
        parts.append(f"r={float(np.median(resids)):+.2f}")
    if lane.get("pred"):
        parts.append("pred")
    stale = (state != "TRACK"
             or (isinstance(age, (int, float)) and age > 250))
    if stale:
        parts.append("STALE")
    _put_text(img, " ".join(parts), (6, ly), scale=0.38,
              color=(0, 170, 255) if stale else (220, 220, 220))


def _draw_bev(canvas: np.ndarray, snap: dict, cal: Calib) -> None:
    """右联：栅格三态 + 墙检出折线 + 路面叠层 + 距离标尺 + d(t) 规划子带
    （全部 None 安全）。

    渲染域 z 到 _RENDER_Z_HI（40m）：栅格只占 3~30m 采集窗，其上为目检延伸
    区（深灰底，检测反投影标记照画，程序供数不收）——虚线亮黄标出采集窗上限，
    「线以上程序不接受」一眼可见（维护者要求，方便目检车/币的真实纵深）。
    绘图区竖长化（画布高 × _PLOT_Y1_FRAC），下余给 d(t) 规划子带。"""
    H = canvas.shape[0]
    plot_x0, plot_x1 = 46, _PANEL_W - 12
    plot_y0, plot_y1 = _TITLE_H + 6, int(H * _PLOT_Y1_FRAC)
    sx = (plot_x1 - plot_x0) / GRID_NX
    sy = (plot_y1 - plot_y0) / ((_RENDER_Z_HI - GRID_Z_LO) / GRID_CELL)
    bm = _BevMap(plot_x0, plot_y1, sx, sy)
    grid_y0 = bm.py(GRID_Z_LO + GRID_NZ * GRID_CELL)   # 栅格采集窗顶

    grid = snap.get("grid")
    reading = snap.get("reading")
    if grid is not None and grid.state is not None:
        img = np.empty((GRID_NZ, GRID_NX, 3), np.uint8)
        for st, col in _STATE_BGR.items():
            img[grid.state == st] = col
        if grid.dig_mask is not None:
            img[grid.dig_mask] = _DIG_BGR
        # state 行序 iz0=近端，渲染翻成近下远上（与 py()/标尺/自车标同约定）；
        # 只铺采集窗带 [grid_y0, plot_y1]，其上是目检延伸区（深灰底）
        cv2.rectangle(canvas, (plot_x0, plot_y0), (plot_x1, grid_y0),
                      (38, 38, 38), -1)
        canvas[grid_y0:plot_y1, plot_x0:plot_x1] = cv2.resize(
            img[::-1], (plot_x1 - plot_x0, plot_y1 - grid_y0),
            interpolation=cv2.INTER_NEAREST)
    else:
        cv2.rectangle(canvas, (plot_x0, plot_y0), (plot_x1, plot_y1),
                      (90, 90, 90), 1)
        _put_text(canvas, "grid: none", (plot_x0 + 8, plot_y0 + 20))

    # 质量门/拟合失败提示：栅格全灰（coef=None）时一眼知道为什么
    if grid is not None and reading is not None and reading.coef is None \
            and reading.rejects:
        _put_text(canvas, ";".join(reading.rejects)[:44],
                  (plot_x0 + 8, plot_y1 - 20), scale=0.4, color=(0, 200, 255))

    # 距离标尺：z 等值线每 3m 近场/5m 远场 横线+标签（全图统一车头系口径
    # 2026-10-10 挪正：标签=相机系 z 减 CAM_Z_DATUM_M，"0m"=车头着地点；
    # 刻度位置仍走相机系映射——栅格/检出折线是相机系数据面）、x 每 5m 刻度
    for z in _Z_TICK_M:
        y = bm.py(z)
        cv2.line(canvas, (plot_x0 - 4, y), (plot_x1, y), (120, 120, 120), 1)
        _put_text(canvas, f"{int(z - CAM_Z_DATUM_M)}m", (plot_x0 - 42, y + 4),
                  scale=0.38, color=(200, 200, 200))
    for x in _X_TICK_M:
        xp = bm.px(x)
        cv2.line(canvas, (xp, plot_y0), (xp, plot_y1 + 4), (120, 120, 120), 1)
        _put_text(canvas, f"{int(x)}", (xp - 8, plot_y1 + 16), scale=0.38,
                  color=(200, 200, 200))

    # 程序接受域上限虚线（z=GRID_Z_HI 栅格采集窗顶）：线以下=供数域，线以上=
    # 只画不收。后画压过同行的 z 刻度灰线（GRID_Z_HI=30 后窗顶与 30m 刻度
    # 重合，先画会被盖）。
    for xd in range(plot_x0, plot_x1, 14):
        cv2.line(canvas, (xd, grid_y0), (min(xd + 7, plot_x1), grid_y0),
                 _LIMIT_BGR, 1)
    _put_text(canvas, f"grid<={int(GRID_Z_HI - CAM_Z_DATUM_M)}m",
              (plot_x0 + 8, grid_y0 - 6), scale=0.36, color=_LIMIT_BGR)

    # 路面叠层：路心 / 左右缘（路心系 d → 相机轴米 X=(d−ro)·lane_w_m）
    ro = snap.get("road_offset")
    lwm = cal.lane_w_m if cal is not None else None
    if ro is not None and lwm:

        def _vline(d_lane: float, color, label: str) -> None:
            xp = bm.px((d_lane - ro) * lwm)
            if plot_x0 <= xp <= plot_x1:
                cv2.line(canvas, (xp, plot_y0), (xp, plot_y1), color, 1)
                _put_text(canvas, label, (xp + 2, plot_y0 + 12), scale=0.36,
                          color=color)

        _vline(0.0, (255, 255, 255), "C")
        if reading is not None:
            if reading.left_edge_lane is not None:
                _vline(reading.left_edge_lane, (255, 255, 0), "L")
            if reading.right_edge_lane is not None:
                _vline(reading.right_edge_lane, (255, 255, 0), "R")
            # 当帧原始路心 c0=((L+R)/2)·lane_w_m（自车系米，直接定位，不经
            # _vline 的 d−ro 换算）：与滤波 C 并排。两者分离=中值窗/保鲜槽/
            # 链路滞后的时间链效应，不是路心合成式错（2026-10-05 实机排障）。
            if reading.left_edge_lane is not None \
                    and reading.right_edge_lane is not None:
                xp = bm.px((reading.left_edge_lane + reading.right_edge_lane)
                           / 2.0 * lwm)
                if plot_x0 <= xp <= plot_x1:
                    cv2.line(canvas, (xp, plot_y0), (xp, plot_y1),
                             (80, 170, 255), 1)
                    _put_text(canvas, "c0", (xp + 2, plot_y0 + 12), scale=0.36,
                              color=(80, 170, 255))
        # 栅格路心 cg（同拍可行驶栅格全带 wmid，grid_center_lane 口径）：
        # 快照带 off 系 grid_off，路心位 X=−grid_off·lane_w_m。与找边 c0 并排
        # ——两线分离=找边锁错结构或深度共同错误的互证分歧面（2026-10-06）。
        go = snap.get("grid_off")
        if go is not None:
            xp = bm.px(-go * lwm)
            if plot_x0 <= xp <= plot_x1:
                cv2.line(canvas, (xp, plot_y0), (xp, plot_y1), (80, 220, 80), 1)
                _put_text(canvas, "cg", (xp + 2, plot_y0 + 24), scale=0.36,
                          color=(80, 220, 80))

    # 自车标记（X=0、车头着地点 z=CAM_Z_DATUM_M——车头系 0m 处，与距离标尺
    # 同基准）：三角大小=米制车半宽（俯视正交下恒定像素，
    # 与车标记同尺——BEV 里的米数对得上真实车宽）
    ego_r = max(8, int(_CAR_HALF_M / GRID_CELL * sx))
    cv2.drawMarker(canvas, (bm.px(0.0), bm.py(CAM_Z_DATUM_M)), (0, 255, 0),
                   cv2.MARKER_TRIANGLE_UP, ego_r, 2)

    # 当帧原始墙检出折线（reading.edge_pts，5 深度箱每侧一检出）：分侧按 z
    # 连线——「两边墙」在供数窗内一眼可见（维护者要求，2026-10-09）；走廊
    # 叠层是消化后的 believe 边界，此线是原始检出。质量门拒绝帧不产
    # edge_pts（整帧弃权），黑视拍自然缺席——缺席本身是可读信号。
    if reading is not None:
        eps = getattr(reading, "edge_pts", ()) or ()
        for side in (-1, 1):
            spts = _edge_side_pts(eps, side)
            if len(spts) < 2:
                continue
            pix = np.array([(bm.px(x), bm.py(z)) for z, x in spts], np.int32)
            cv2.polylines(canvas, [pix], False, _EDGE_BGR, 1)
            for z, x in spts:
                cv2.circle(canvas, (bm.px(x), bm.py(z)), 2, _EDGE_BGR, -1)

    # 分带走廊叠层（第 6 步）：逐带边界按来源着色——每带一段竖线（近下远上），
    # 「程序这一带相信哪道墙、信多少」一眼可见；unknown（无证据）不画。
    # 旧 L/R 线只画 dgeo 当帧缘——质量门黑视/单侧帧 dgeo 无缘而走廊仍供数
    # （wall/region 路径），此叠层就是那个差异的可见面。
    corridor = snap.get("corridor")
    if corridor is not None:
        for b in corridor.bands:
            # 带标已挪车头系（corridor 对外字段），画回相机系映射
            y_top, y_bot = bm.py(b.z1 + CAM_Z_DATUM_M), bm.py(b.z0 + CAM_Z_DATUM_M)
            for bb in (b.left, b.right):
                if bb.x_m is None or bb.src == "unknown":
                    continue
                xp = bm.px(bb.x_m)
                if not (plot_x0 <= xp <= plot_x1):
                    continue
                col = _COR_SRC_BGR.get(bb.src)
                if bb.src == "measured":
                    cv2.line(canvas, (xp, y_top), (xp, y_bot), col, 2)
                else:                      # occluded/inferred=半可信：虚线
                    for y in range(y_top, y_bot, 8):
                        cv2.line(canvas, (xp, y), (xp, min(y + 4, y_bot)),
                                 col, 1)

    # 走廊中心线：只画中心，不再画旧选择层的 L4 容差带。
    if corridor is not None and cal is not None:
        cpts = []
        for b in corridor.bands:
            if b.left.x_m is None or b.right.x_m is None:
                continue
            c = 0.5 * (b.left.x_m + b.right.x_m)
            z0c, z1c = b.z0 + CAM_Z_DATUM_M, b.z1 + CAM_Z_DATUM_M
            cpts.append((bm.px(c), bm.py(0.5 * (z0c + z1c))))
        if len(cpts) >= 2:
            cv2.polylines(canvas, [np.array(cpts, np.int32)], False,
                          (0, 255, 255), 1)
            _put_text(canvas, "corridor center",
                      (plot_x0 + 8, plot_y0 + 48), scale=0.36,
                      color=(0, 255, 255))
    # 供数摘要 + 图例（目检延伸区左上：栅格采集窗上方的深灰带）——
    # corridor=None（黑视拍）也画：图例不缺席，"全灰=U 无证据"在黑视帧可读
    _put_text(canvas,
              f"cor={'none' if corridor is None else corridor.src}"
              f" w={_fmt(None if corridor is None or corridor.width_lane is None else round(corridor.width_lane, 2))}"
              f" ph={'-' if corridor is None else corridor.plane_health}",
              (plot_x0 + 8, plot_y0 + 8), scale=0.4, color=(230, 230, 230))
    ly = plot_y0 + 28
    for label, col, dashed in (("wall", _COR_SRC_BGR["measured"], False),
                               ("occl", _COR_SRC_BGR["occluded"], True),
                               ("air", _COR_SRC_BGR["inferred"], True),
                               ("edge", _EDGE_BGR, False)):
        for x in range(plot_x0 + 8, plot_x0 + 34, 8):
            cv2.line(canvas, (x, ly), (x + 4, ly), col,
                     2 if not dashed else 1)
        _put_text(canvas, label, (plot_x0 + 38, ly - 5), scale=0.36,
                  color=(200, 200, 200))
        ly += 18
    ly += 2
    lx = plot_x0 + 8                     # 图例游标（勿用 sx——那是 BEV 横向比例尺）
    for label, col in (("U", _STATE_BGR[GRID_UNKNOWN]),
                       ("D", _STATE_BGR[GRID_DRIVABLE]),
                       ("B", _STATE_BGR[GRID_BLOCKED])):
        cv2.rectangle(canvas, (lx, ly - 9), (lx + 12, ly + 3), col, -1)
        _put_text(canvas, label, (lx + 15, ly - 5), scale=0.36,
                  color=(200, 200, 200))
        lx += 46
    # 三层窗口标注（2026-10-10 扩窗定案 + 同日车头系挪正）：各层窗口常量派生
    # （z 层减 CAM_Z_DATUM_M 与标尺同基准；scan 是横向域不减）——「程序各层
    # 看多远」在图上一行读尽，数值不漂
    _put_text(canvas,
              f"win: grid<={int(GRID_Z_HI - CAM_Z_DATUM_M)} "
              f"scan<={int(X_MAX_M)}x "
              f"lane{int(REGION_Z_HI - CAM_Z_DATUM_M)}"
              f"/w{int(REGION_WIDTH_Z_HI - CAM_Z_DATUM_M)} "
              f"plane<={int(ROAD_Z_HI - CAM_Z_DATUM_M)} "
              f"anchor<={int(ANCHOR_Z_HI - CAM_Z_DATUM_M)}",
              (plot_x0 + 8, ly + 8), scale=0.36, color=(160, 160, 160))

    # 检测反投影标记：框底中心 → (X,Z)，域内画点
    result = snap.get("result")
    shape = snap.get("frame_shape")
    if result is not None and reading is not None \
            and shape is not None and len(shape) >= 2:
        coef = reading.coef
        fx, fy = reading.fx, reading.fy
        fh, fw = int(shape[0]), int(shape[1])
        if coef is not None and fx and fy:
            # 车标记=米制半径（车半宽，latsample.GRID_CAR_HALF_LANE_M 真源：
            # BEV 俯视正交下恒定像素、不随 z 缩放——远近的车一样大才对得上
            # 真实米数），空心不遮栅格；币/奖励=点目标固定像素（无尺寸信源）
            car_r = max(3, int(_CAR_HALF_M / GRID_CELL * sx))
            marks = (("coins", 3, True), ("cars", car_r, False),
                     ("bonuses", 5, True))
            for cls, rad, filled in marks:
                color = _CLS_BGR[cls]
                for d in getattr(result, cls):
                    hit = _project_detection(d.cx, d.cy + d.h // 2, coef,
                                             fx, fy, fw, fh)
                    if hit is None:
                        continue
                    x_m, z_m = hit
                    if z_m > _RENDER_Z_HI:
                        # 40m 外（渲染域上限）：标记钉在绘图区顶缘、距离如实
                        # 标注——「程序能看多远」一目了然；横向位置=投影 x
                        # 夹到域内（俯视正交，远处方向仍真实）。程序供数仍
                        # 止步于栅格窗，此层只回答"看见了"。
                        xp = max(bm.px(-GRID_X_MAX) + 2,
                                 min(bm.px(GRID_X_MAX) - 2, bm.px(x_m)))
                        yp = plot_y0 + 5
                        cv2.circle(canvas, (xp, yp), rad, color,
                                   -1 if filled else 1)
                        _put_text(canvas, f"{int(z_m - CAM_Z_DATUM_M)}m",
                                  (xp + rad + 2, yp - 5), scale=0.34,
                                  color=color)
                    else:
                        cv2.circle(canvas, (bm.px(x_m), bm.py(z_m)),
                                   rad, color, -1 if filled else 1)

    # 选择层子带 + 转向力度-时间子带（下半区一分为二）：选择带回答「跟谁」，
    # 转向带回答「这一秒实际怎么打方向」——操作与决策能按时间对上。
    py0 = plot_y1 + 30
    py1 = H - 24
    split = py0 + int((py1 - py0) * 0.56)
    _draw_selection_strip(canvas, snap, plot_x0, plot_x1, py0, split - 3)
    _draw_steer_strip(canvas, snap, plot_x0, plot_x1, split + 2, py1)


def _draw_selection_strip(canvas: np.ndarray, snap: dict,
                          x0: int, x1: int, y0: int, y1: int) -> None:
    """轨迹计划子带：横轴 t[0,3]s、纵轴 x±2.5 道；只画新计划。"""
    cv2.rectangle(canvas, (x0, y0), (x1, y1), (90, 90, 90), 1)
    tmax, xlo, xhi = 3.0, -2.5, 2.5

    def tx(t: float) -> int:
        return int(x0 + (x1 - x0) * min(max(t / tmax, 0.0), 1.0))

    def yx(v: float) -> int:
        return int(y1 - (y1 - y0) * (min(max(v, xlo), xhi) - xlo)
                   / (xhi - xlo))

    for t in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        gx = tx(t)
        cv2.line(canvas, (gx, y0 + 18), (gx, y1 - 4), (62, 62, 62), 1)
        _put_text(canvas, f"{t:g}", (gx - 6, y1 - 2), scale=0.3,
                  color=(140, 140, 140))
    for x in (-2, -1, 0, 1, 2):
        gy = yx(float(x))
        cv2.line(canvas, (x0, gy), (x1, gy), (62, 62, 62), 1)
        _put_text(canvas, f"{x:+d}", (x0 + 2, gy - 2), scale=0.3,
                  color=(140, 140, 140))
    _draw_plan_overlay(canvas, snap, x0, x1, y0, y1, tx, yx)


def _draw_plan_overlay(canvas: np.ndarray, snap: dict,
                       x0: int, x1: int, y0: int, y1: int,
                       tx, yx) -> None:
    """轨迹计划叠层：8 条候选（细线）+ 选中（粗绿）+ 预览点。"""
    plan = snap.get("plan")
    scene = snap.get("scene")
    if plan is None or scene is None or not plan.cand_x:
        return
    lw = scene.lane_w_m
    dt = plan.dt_s
    for i, xs in enumerate(plan.cand_x):
        col = (0, 220, 0) if i == plan.chosen else (110, 110, 110)
        pts = [(tx(k * dt), yx(x / lw)) for k, x in enumerate(xs)
               if k * dt <= 3.0]
        if len(pts) >= 2:
            cv2.polylines(canvas, [np.array(pts, np.int32)], False, col,
                          2 if i == plan.chosen else 1)
    px = tx(plan.dt_s * max(1, round(0.2 / max(plan.dt_s, 1e-9))))
    cv2.circle(canvas, (px, yx(plan.x_preview_m / lw)), 3, (0, 255, 0), -1)
    _put_text(canvas,
              f"plan:{plan.reason} L{plan.layer}"
              f" v={plan.value_total:.0f} u={plan.u_feedforward:+.2f}",
              (x0 + 2, y0 + 26), scale=0.32,
              color=(0, 220, 0) if plan.valid else (0, 160, 255))


def _draw_steer_strip(canvas: np.ndarray, snap: dict,
                      x0: int, x1: int, y0: int, y1: int) -> None:
    """转向力度/方向-时间子带：纵轴=杆量（上=R，下=L），横轴=最近 6s。"""
    cv2.rectangle(canvas, (x0, y0), (x1, y1), (90, 90, 90), 1)
    _put_text(canvas, "steer force  R up / L down  last 6s",
              (x0 + 6, y0 + 13), scale=0.34, color=(200, 200, 200))
    mid = (y0 + y1) // 2 + 3
    half = max(8, (y1 - y0) // 2 - 13)
    cv2.line(canvas, (x0 + 4, mid), (x1 - 4, mid), (110, 110, 110), 1)
    for frac, col in ((0.45, (72, 72, 72)), (1.0, (96, 96, 96))):
        for s in (-1, 1):
            yy = int(mid - s * frac * half)
            for xx in range(x0 + 4, x1 - 4, 10):
                cv2.line(canvas, (xx, yy), (min(xx + 5, x1 - 4), yy), col, 1)
    _put_text(canvas, "R", (x1 - 16, y0 + 13), scale=0.34,
              color=(140, 220, 140))
    _put_text(canvas, "L", (x1 - 16, y1 - 4), scale=0.34,
              color=(140, 140, 220))

    hist = [(f, v) for f, v in (snap.get("steer_hist") or ())
            if isinstance(v, (int, float))]
    if not hist:
        _put_text(canvas, "steer: wait", (x0 + 6, mid + 4), scale=0.38,
                  color=(0, 200, 255))
        return
    n = len(hist)
    span = x1 - x0 - 12
    # 时间刻度按控制拍 20Hz 折算（历史条长度=拍数/20 秒）
    for k in range(1, 6):
        frac = k / 6.0
        gx = int(x1 - 6 - span * frac)
        cv2.line(canvas, (gx, mid - 3), (gx, mid + 3), (120, 120, 120), 1)
        _put_text(canvas, f"-{int(round(k * n / 120.0))}s",
                  (gx - 6, y1 - 4), scale=0.26, color=(130, 130, 130))
    pts = []
    for i, (_, v) in enumerate(hist):
        x = int(x0 + 6 + span * i / max(n - 1, 1))
        y = int(mid - max(-1.0, min(1.0, v / _STEER_FULL)) * half)
        pts.append((x, y))
    for (xa, ya), (xb, yb), (_, v) in zip(pts, pts[1:], hist[1:]):
        col = (0, 200, 0) if v >= 0 else (0, 90, 255)
        cv2.line(canvas, (xa, ya), (xb, yb), col, 2)
    cv2.circle(canvas, pts[-1], 3, (255, 255, 255), -1)
    cur = hist[-1][1] / _STEER_FULL
    _put_text(canvas,
              f"now {cur * 100:+.0f}% ({'R' if cur >= 0 else 'L'})",
              (x0 + 6, mid + 14), scale=0.36,
              color=(0, 220, 0) if cur >= 0 else (0, 120, 255))


def _fmt(v) -> str:
    return "-" if v is None else str(v)


def render_hud(frame_rgb: np.ndarray, snap: dict, cal: Calib) -> np.ndarray:
    """双联 hud 图：左联=画面叠加，右联=BEV。纯函数，None 全安全。"""
    img_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    H, W = img_bgr.shape[:2]

    # ---- 左联：YOLO 框（先画在底图上，再合成双联）----
    result = snap.get("result")
    if result is not None:
        for cls, color in _CLS_BGR.items():
            for d in getattr(result, cls):
                x1, y1 = d.cx - d.w // 2, d.cy - d.h // 2
                cv2.rectangle(img_bgr, (x1, y1), (x1 + d.w, y1 + d.h),
                              color, 2)
                _put_text(img_bgr, f"{d.conf:.2f}", (x1, max(12, y1 - 4)),
                          scale=0.4, color=color)

    # ---- 左联：车道线观测叠加（最近一次检测的拟合线；预测帧沿用并标 STALE）----
    _draw_lane_overlay(img_bgr, snap)

    # ---- 左联：读数带（决策/供数/HUD 比分）----
    dec = snap.get("decision") or {}
    hud = snap.get("hud") or {}
    fields = hud.get("fields") or {}
    lines = [
        f"#{snap.get('fid', 0)} p{snap.get('phase', 0)}"
        f" {dec.get('state', '')} {dec.get('reason', '')}",
        f"steer={dec.get('steer', 0)} el={_fmt(dec.get('executed_lane'))}",
        f"ro={_fmt(dec.get('road_offset'))} dgeo age={dec.get('dgeo_age', '-')}"
        f"ms new={dec.get('dgeo_new', '-')}",
        f"S_a={_fmt(fields.get('score_a'))} R_a={_fmt(fields.get('rate_a'))}"
        f" | S_b={_fmt(fields.get('score_b'))} R_b={_fmt(fields.get('rate_b'))}",
        f"coins={len(result.coins) if result else 0}"
        f" cars={len(result.cars) if result else 0}"
        f" bonus={len(result.bonuses) if result else 0}"
        f" groups={snap.get('n_groups', 0)}",
    ]
    c = snap.get("corridor")
    if c is not None:
        lines.append(f"cor={c.src} w={_fmt(None if c.width_lane is None else round(c.width_lane, 2))}"
                     f" ph={c.plane_health} bands={len(c.bands)}")
    for i, text in enumerate(lines):
        _put_text(img_bgr, text, (8, 18 + i * 18), scale=0.45)

    # ---- 左联底部：实际杆量/程序反事实/线观测摘要（覆盖在画面最下方）----
    _draw_input_band(img_bgr, snap)

    # ---- 合成：左联底图 + 右联 BEV ----
    canvas = np.zeros((H, W + _PANEL_W, 3), np.uint8)
    canvas[:, :W] = img_bgr
    canvas[:, W:] = (28, 28, 28)
    _put_text(canvas, "speedrush hud  ·  BEV radar (grid + corridor + plan)",
              (W + 8, 17), scale=0.48)
    _draw_bev(canvas[:, W:], snap, cal)
    return canvas


class DebugPairWriter:
    """raw+hud 落盘器：tick() 入队本拍快照，IO 线程逐拍渲染写盘（10Hz）。

    生命周期 = 控制链：链建即起 daemon，链收 stop()（排空队列后退出）。
    队列有界（满丢旧保新）；渲染/写盘异常静默计数——观测件不碰主路。
    tick() 里的 frame 是 WGC 当前缓存数组（帧号变才重绑新数组，旧数组不被
    原地改写）；snap 内的 result/corridor/scene/decision 均为本拍新建对象，
    入队后主循环不再改它们，因此 IO 线程延迟渲染拿到的仍是本拍结果。

    产物布局与人工录制会话同一套（``session_layout``）：``raw/`` + 叠加图 +
    ``frames.jsonl`` + ``meta.json``。人工局由录制器自己写这套（叠加图引用
    同一份原帧），本 writer 只在控制链里建，两种局不会同时写一个目录。"""

    def __init__(self, session_dir: Path, cal: Calib | None = None,
                 interval_s: float | None = None,
                 *, phase: int | None = None,
                 round_no: int | None = None):
        self._dir = Path(session_dir)
        self._raw_dir = self._dir / "raw"
        self._cal = cal if cal is not None else load_calib()
        self._interval = DEBUG_INTERVAL_S if interval_s is None else interval_s
        self._last_push = float("-inf")     # 首拍立出图
        self._seq = 0
        self.saved = 0
        self.dropped = 0
        self.errors = 0
        self.hud_written = 0
        self.last_ms = 0.0
        self._phase = phase
        self._round_no = round_no
        self._frame_size: tuple[int, int] | None = None
        self._started_ns = time.perf_counter_ns()
        self._started_iso = datetime.now().isoformat(timespec="seconds")
        # 帧索引：raw/hud 的 seq 与采集帧号、帧时戳一一对应。此前实跑会话只有
        # 图没有索引，队列满丢旧保新会让「第 i 张图 ↔ 第 i 拍」不成立——离线
        # 把像素结论接到控制状态上时只能作废（2026-10-27 当日 P1 核查踩过）。
        # 字段与人工录制器同一套（``session_layout``），不另起编号。
        self._idx_path = self._dir / "frames.jsonl"
        self._idx_f = None
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._steer_hist: deque = deque(maxlen=_STEER_HIST_MAX)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="speedrush-debug-io")
        self._thread.start()

    def stats(self) -> dict:
        """IO 侧观测：写盘数/丢旧数/异常数/队列深度/单对耗时（ms）。"""
        return {"saved": self.saved, "dropped": self.dropped,
                "errors": self.errors, "q": self._q.qsize(),
                "ms": round(self.last_ms, 1)}

    def tick(self, frame_rgb: np.ndarray | None, snap: dict) -> None:
        """控制拍调用：每拍先记转向历史，再按节流入队（默认逐拍）。绝不抛。"""
        dec = snap.get("decision") or {}
        sv = dec.get("steer")
        if isinstance(sv, (int, float)) and not isinstance(sv, bool):
            self._steer_hist.append((snap.get("fid"), int(sv)))
        if frame_rgb is None:
            return
        now = time.monotonic()
        if now - self._last_push < self._interval:
            return
        self._last_push = now
        snap = dict(snap)
        snap["steer_hist"] = list(self._steer_hist)
        pair = (frame_rgb, snap)
        try:
            self._q.put_nowait(pair)
        except queue.Full:
            # 丢旧保新：取证更看重最新一拍（旧快照的 fid 已在 trace 里）
            try:
                self._q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(pair)
            except queue.Full:
                self.dropped += 1

    def stop(self, reason: str = "chain_end") -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self._idx_f is not None:
            try:
                self._idx_f.close()
            except OSError:
                pass
            self._idx_f = None
        self._write_meta(reason)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame_rgb, snap = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(frame_rgb, snap)
            except Exception:  # noqa: BLE001 —— 可视化失败不碰主路
                self.errors += 1

    def _process(self, frame_rgb: np.ndarray, snap: dict) -> None:
        t0 = time.perf_counter()
        self._seq += 1
        self._dir.mkdir(parents=True, exist_ok=True)
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        h, w = frame_rgb.shape[:2]
        if self._frame_size is None:
            self._frame_size = (int(w), int(h))
        raw_name = raw_relpath(self._seq)
        ok_raw = cv2.imwrite(
            str(self._dir / raw_name),
            cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, _RAW_JPEG_QUALITY])
        if not ok_raw:
            self.errors += 1       # 索引不指向不存在的文件
            return
        snap = dict(snap)
        snap["frame_shape"] = frame_rgb.shape
        # 渲染失败只让"叠加图缺席"，原帧照常进索引（hud=null）——与人工局同一条
        # 纪律：索引里的文件一定存在，缺席用 null 表达，而不是整行不留。
        hud_name = None
        try:
            hud = render_hud(frame_rgb, snap, self._cal)
            name = hud_relpath(self._seq)
            if cv2.imwrite(str(self._dir / name), hud,
                           [cv2.IMWRITE_JPEG_QUALITY, _HUD_JPEG_QUALITY]):
                self.hud_written += 1
                hud_name = name
            else:
                self.errors += 1
        except Exception:  # noqa: BLE001 —— 可视化失败不碰主路
            self.errors += 1
        self._write_index(snap, hud_name)
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        self.saved += 1

    def _write_index(self, snap: dict, hud_name: str | None) -> None:
        """帧索引一行：seq ↔ 采集帧号 ↔ 帧时戳 ↔ 文件（与人工局同一套字段）。

        缺 fid 或时戳就写 null，不拿 seq 反推、不用当前时刻冒充采集时刻——
        这条索引的全部价值就是"图与拍能一一对上"，伪造一个就等于没有。
        写失败只计数（观测件不碰主路），与渲染异常同一纪律。"""
        row = {"seq": self._seq, "frame_id": snap.get("fid"),
               "ts_ns": snap.get("ts_ns"), "age_ms": snap.get("age_ms"),
               "file": raw_relpath(self._seq), "hud": hud_name}
        try:
            if self._idx_f is None:
                self._idx_f = open(self._idx_path, "a", encoding="utf-8")
            self._idx_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._idx_f.flush()
        except OSError:
            self.errors += 1

    def _write_meta(self, reason: str) -> None:
        """会话元信息（与人工局同构）：控制局没有手柄项，显式 None。"""
        w, h = self._frame_size or (0, 0)
        meta = build_meta(
            source=SOURCE_CONTROL,
            started_at=self._started_iso,
            ended_at=datetime.now().isoformat(timespec="seconds"),
            monotonic_start_ns=self._started_ns,
            phase=self._phase,
            round_no=self._round_no,
            frame_w=w,
            frame_h=h,
            jpeg_quality=_RAW_JPEG_QUALITY,
            hud_jpeg_quality=_HUD_JPEG_QUALITY,
            frames_written=self.saved,
            frames_dropped=self.dropped,
            hud_written=self.hud_written,
            write_errors=self.errors,
            pad_slot=None,
            pad_poll_hz=None,
            pad_samples=None,
            stop_reason=reason,
        )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.errors += 1
