# -*- coding: utf-8 -*-
"""speedrush 实机调试图（treasure 规范接入）：raw + hud 双图，3fps 节流。

**职责与边界**：控制拍每 tick 喂一次快照（只入队引用，不复制不渲染）；IO 线程
按 3fps 节流渲染+落盘。渲染/写盘失败静默计数——调试图绝不干扰控制链（与
depth_geo 取证写手同姿态；后者是深度管线的证据包面，与本图互补、互不替代）。

**目录契约**（沿 treasure：`debug/<模块>/<会话>/` + `raw/` 子目录）：
  `<seq:04d>_raw.jpg`   原始帧全幅（q95，人工金标标注/离线重渲染的底图）
  `<seq:04d>_hud.jpg`   双联渲染图（q85）：
    左联 = 画面叠加：YOLO 框（coin 黄 / car 红 / bonus 品红）+ 决策/供数读数带
           + HUD 比分读数（HudObserver 最新行，score/rate × a/b 槽位）
    右联 = BEV 雷达：可行驶栅格三态 + 挖洞格 + 当帧墙检出折线（edge_pts 两侧，
           供数窗内「两边墙」可见）+ 路心/左右缘叠层（C=滤波 ro、c0=当帧原始
           (L+R)/2，两者分离=时间链效应；cg=同拍栅格 wmid 路心，与 c0 分离=
           互证分歧面）+ 分带走廊边界（来源着色）+ 检测反投影标记（40m 外钉顶缘
           +距离标注）+ 距离标尺（z 等值线近场每 3m/远场每 5m，全图统一
           车头系口径）+ 三层窗口标注（供数 11m/平面拟合
           25m/锚点 85m，车头系）+ d(t) 规划曲线子带

**BEV 口径**（与 _grid_penalty 同源，不另立坐标）：栅格 x 原点=相机光轴=自车，
格 (ix, iz) → X=ix·CELL−X_MAX、Z=Z_LO+iz·CELL；**渲染近下远上**（z=Z_LO 在
画布底，与 depth_geo 取证面板 z3(bottom)..z16(top) 同约定，自车标在底=近端）；
路心系 d（道）→ 相机轴米 X=(d−ro)·lane_w_m。检测标记：框底中心像素经同帧焦距
+ 路面平面反投影（Z=c/(v−a·u−b)，X=u·Z），域外点丢弃。

**规划曲线为何不进 BEV**：轨迹是时间参数化的 d(t)，本域没有自车前向速度估计，
臆造 t→z 映射就是把假数据画进证据面——以 d(t) 子带呈现（横轴秒、纵轴道）。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core.debug import _put_text
from maaracing_master.plugins.speedrush.depth_geo import (
    ANCHOR_Z_HI, CAM_Z_DATUM_M, GRID_BLOCKED, GRID_CELL, GRID_DRIVABLE,
    GRID_NX, GRID_NZ, GRID_SHOULDER, GRID_UNKNOWN, GRID_X_MAX, GRID_Z_HI,
    GRID_Z_LO, NEAR_Z, ROAD_Z_HI, X_MAX_M)
from maaracing_master.plugins.speedrush.latsample import (
    GRID_CAR_HALF_LANE_M, SampledTraj, eval_traj)
from maaracing_master.plugins.speedrush.world_model import (
    Calib, load_calib)

DEBUG_FPS = 3
DEBUG_INTERVAL_S = 1.0 / DEBUG_FPS
_QUEUE_MAX = 4                    # IO 有界队列：满则丢新快照（观测降密度）

# 三态着色（BGR）：unknown 灰 / drivable 暗绿 / blocked 红；挖洞格橄榄描重
_STATE_BGR = {GRID_UNKNOWN: (64, 64, 64),
              GRID_DRIVABLE: (70, 140, 70),
              GRID_BLOCKED: (50, 50, 220),
              GRID_SHOULDER: (60, 110, 160)}
_DIG_BGR = (30, 100, 110)
_CLS_BGR = {"coins": (0, 255, 255), "cars": (0, 0, 255),
            "bonuses": (255, 0, 255)}

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
    """当帧原始墙检出分侧按 z 排序（(z_m, x_m) 相机轴米）——墙线折线的数据面。"""
    return sorted(((z, x) for s, z, x in eps if s == side), key=lambda p: p[0])


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
    ego_r = max(8, int(GRID_CAR_HALF_LANE_M / GRID_CELL * sx))
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
                       ("B", _STATE_BGR[GRID_BLOCKED]),
                       ("S", _STATE_BGR[GRID_SHOULDER])):
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
              f"lane{int(NEAR_Z - CAM_Z_DATUM_M)} "
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
            car_r = max(3, int(GRID_CAR_HALF_LANE_M / GRID_CELL * sx))
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

    # d(t) 规划子带（时间参数化轨迹，诚实呈现；不臆造 t→z）
    py0 = plot_y1 + 30
    py1 = H - 24
    cv2.rectangle(canvas, (plot_x0, py0), (plot_x1, py1), (90, 90, 90), 1)
    _put_text(canvas, "plan d(t) [lane] / t [s]", (plot_x0 + 6, py0 + 16),
              scale=0.4, color=(220, 220, 220))
    plan: SampledTraj | None = snap.get("plan")
    mid = (py0 + py1) // 2
    scale = max(12.0, (py1 - py0) / 5.0)   # 纵向 ~±2.5 道铺满
    if plan is not None and plan.coeffs:
        cv2.line(canvas, (plot_x0, mid), (plot_x1, mid), (110, 110, 110), 1)
        T = max(plan.T, 1e-3)
        pts = []
        for k in range(41):
            t = T * k / 40.0
            d = float(eval_traj(plan.coeffs, t))
            pts.append((int(plot_x0 + (plot_x1 - plot_x0) * k / 40.0),
                        int(mid - d * scale)))
        for p, q in zip(pts, pts[1:]):
            cv2.line(canvas, p, q, (0, 255, 0), 2)
        cv2.circle(canvas, pts[-1], 4, (0, 255, 0), -1)
        _put_text(canvas, f"d1={plan.d1:.2f} T={plan.T:.2f}s",
                  (plot_x1 - 150, py1 - 8), scale=0.42)
    else:
        diag = snap.get("diag") or {}
        _put_text(canvas, f"plan: none ({diag.get('why', '')})",
                  (plot_x0 + 6, mid + 4), scale=0.42, color=(0, 200, 255))


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

    # ---- 左联：读数带（决策/供数/HUD 比分）----
    dec = snap.get("decision") or {}
    hud = snap.get("hud") or {}
    fields = hud.get("fields") or {}
    lines = [
        f"#{snap.get('fid', 0)} p{snap.get('phase', 0)}"
        f" {dec.get('state', '')} {dec.get('reason', '')}",
        f"steer={dec.get('steer', 0)} el={_fmt(dec.get('executed_lane'))}"
        f" xt={_fmt(dec.get('x_target'))}",
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

    # ---- 合成：左联底图 + 右联 BEV ----
    canvas = np.zeros((H, W + _PANEL_W, 3), np.uint8)
    canvas[:, :W] = img_bgr
    canvas[:, W:] = (28, 28, 28)
    _put_text(canvas, "speedrush hud  ·  BEV radar (grid + walls + plan)",
              (W + 8, 17), scale=0.48)
    _draw_bev(canvas[:, W:], snap, cal)
    return canvas


class DebugPairWriter:
    """raw+hud 落盘器：tick() 入队快照，IO 线程 3fps 节流渲染写盘。

    生命周期 = 控制链：链建即起 daemon，链收 stop()（排空队列后退出）。
    队列有界（满丢新）；渲染/写盘异常静默计数——观测件不碰主路。"""

    def __init__(self, session_dir: Path, cal: Calib | None = None,
                 interval_s: float | None = None):
        self._dir = Path(session_dir)
        self._raw_dir = self._dir / "raw"
        self._cal = cal if cal is not None else load_calib()
        self._interval = DEBUG_INTERVAL_S if interval_s is None else interval_s
        self._last_push = float("-inf")     # 首拍立出图
        self._seq = 0
        self.saved = 0
        self.dropped = 0
        self.errors = 0
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="speedrush-debug-io")
        self._thread.start()

    def tick(self, frame_rgb: np.ndarray | None, snap: dict) -> None:
        """控制拍调用：3fps 节流后入队（引用传递，不复制）。绝不抛。"""
        if frame_rgb is None:
            return
        now = time.monotonic()
        if now - self._last_push < self._interval:
            return
        self._last_push = now
        try:
            self._q.put_nowait((frame_rgb, snap))
        except queue.Full:
            self.dropped += 1

    def stop(self, reason: str = "chain_end") -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

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
        self._seq += 1
        self._dir.mkdir(parents=True, exist_ok=True)
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        # raw 落 raw/ 子目录（treasure 契约）；hud 渲染图落会话根
        cv2.imwrite(str(self._raw_dir / f"{self._seq:04d}_raw.jpg"),
                    cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        snap = dict(snap)
        snap["frame_shape"] = frame_rgb.shape
        hud = render_hud(frame_rgb, snap, self._cal)
        cv2.imwrite(str(self._dir / f"{self._seq:04d}_hud.jpg"), hud,
                    [cv2.IMWRITE_JPEG_QUALITY, 85])
        self.saved += 1
