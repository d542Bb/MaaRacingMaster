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
    右联 = BEV 俯视：可行驶栅格三态 + 挖洞格 + 路心/左右缘叠层（C=滤波 ro、
           c0=当帧原始 (L+R)/2，两者分离=时间链效应；cg=同拍栅格 wmid 路心，
           与 c0 分离=互证分歧面）+ 检测反投影标记
           + 距离标尺（z 每 3m、x 每 5m 刻度）+ d(t) 规划曲线子带

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
    GRID_BLOCKED, GRID_CELL, GRID_DRIVABLE, GRID_NX, GRID_NZ, GRID_UNKNOWN,
    GRID_X_MAX, GRID_Z_LO)
from maaracing_master.plugins.speedrush.latsample import SampledTraj, eval_traj
from maaracing_master.plugins.speedrush.world_model import (
    Calib, load_calib)

DEBUG_FPS = 3
DEBUG_INTERVAL_S = 1.0 / DEBUG_FPS
_QUEUE_MAX = 4                    # IO 有界队列：满则丢新快照（观测降密度）

# 三态着色（BGR）：unknown 灰 / drivable 暗绿 / blocked 红；挖洞格橄榄描重
_STATE_BGR = {GRID_UNKNOWN: (64, 64, 64),
              GRID_DRIVABLE: (70, 140, 70),
              GRID_BLOCKED: (50, 50, 220)}
_DIG_BGR = (30, 100, 110)
_CLS_BGR = {"coins": (0, 255, 255), "cars": (0, 0, 255),
            "bonuses": (255, 0, 255)}

_PANEL_W = 560                    # 右联宽
_TITLE_H = 26                     # 右联标题条
_Z_TICK_M = (3.0, 6.0, 9.0, 12.0, 15.0, 18.0, 21.0, 24.0)   # 标尺（含目检延伸区）
_X_TICK_M = (-10.0, -5.0, 0.0, 5.0, 10.0)
_RENDER_Z_HI = 24.0               # BEV 渲染上限（> 栅格采集窗 16m）：目检延伸区
_LIMIT_BGR = (0, 215, 255)        # 程序接受域上限虚线（亮黄）


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

    z 上限=_RENDER_Z_HI（渲染域）而非栅格采集窗——16m 外的检测标记画进目检
    延伸区供人看，程序供数仍止步于栅格窗。"""
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
    if not (GRID_Z_LO <= z <= _RENDER_Z_HI) or abs(x) > GRID_X_MAX:
        return None
    return x, z


def _draw_bev(canvas: np.ndarray, snap: dict, cal: Calib) -> None:
    """右联：栅格三态 + 路面叠层 + 标尺 + d(t) 规划子带（全部 None 安全）。

    渲染域 z 到 _RENDER_Z_HI（24m）：栅格只占 3~16m 采集窗，其上为目检延伸
    区（检测反投影标记照画，程序供数不收）——虚线亮黄标出采集窗上限，
    「线以上程序不接受」一眼可见（维护者要求，方便目检车/币的真实纵深）。"""
    H = canvas.shape[0]
    plot_x0, plot_x1 = 46, _PANEL_W - 12
    plot_y0, plot_y1 = _TITLE_H + 6, int(H * 0.56)
    sx = (plot_x1 - plot_x0) / GRID_NX
    sy = (plot_y1 - plot_y0) / ((_RENDER_Z_HI - GRID_Z_LO) / GRID_CELL)
    bm = _BevMap(plot_x0, plot_y1, sx, sy)
    grid_y0 = bm.py(GRID_Z_LO + GRID_NZ * GRID_CELL)   # 栅格采集窗顶（16m）

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

    # 程序接受域上限虚线（z=16m 栅格采集窗顶）：线以下=供数域，线以上=只画不收
    for xd in range(plot_x0, plot_x1, 14):
        cv2.line(canvas, (xd, grid_y0), (min(xd + 7, plot_x1), grid_y0),
                 _LIMIT_BGR, 1)
    _put_text(canvas, "grid<=16m", (plot_x0 + 8, grid_y0 - 6), scale=0.36,
              color=_LIMIT_BGR)

    # 距离标尺：z 每 3m 横线+标签、x 每 5m 刻度
    for z in _Z_TICK_M:
        y = bm.py(z)
        cv2.line(canvas, (plot_x0 - 4, y), (plot_x1, y), (120, 120, 120), 1)
        _put_text(canvas, f"{int(z)}m", (plot_x0 - 42, y + 4), scale=0.38,
                  color=(200, 200, 200))
    for x in _X_TICK_M:
        xp = bm.px(x)
        cv2.line(canvas, (xp, plot_y0), (xp, plot_y1 + 4), (120, 120, 120), 1)
        _put_text(canvas, f"{int(x)}", (xp - 8, plot_y1 + 16), scale=0.38,
                  color=(200, 200, 200))

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

    # 自车标记（X=0、域底）
    cv2.drawMarker(canvas, (bm.px(0.0), plot_y1 - 6), (0, 255, 0),
                   cv2.MARKER_TRIANGLE_UP, 12, 2)

    # 检测反投影标记：框底中心 → (X,Z)，域内画点
    result = snap.get("result")
    shape = snap.get("frame_shape")
    if result is not None and reading is not None \
            and shape is not None and len(shape) >= 2:
        coef = reading.coef
        fx, fy = reading.fx, reading.fy
        fh, fw = int(shape[0]), int(shape[1])
        if coef is not None and fx and fy:
            marks = (("coins", 3), ("cars", 5), ("bonuses", 5))
            for cls, rad in marks:
                color = _CLS_BGR[cls]
                for d in getattr(result, cls):
                    hit = _project_detection(d.cx, d.cy + d.h // 2, coef,
                                             fx, fy, fw, fh)
                    if hit is None:
                        continue
                    cv2.circle(canvas, (bm.px(hit[0]), bm.py(hit[1])),
                               rad, color, -1)

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
    for i, text in enumerate(lines):
        _put_text(img_bgr, text, (8, 18 + i * 18), scale=0.45)

    # ---- 合成：左联底图 + 右联 BEV ----
    canvas = np.zeros((H, W + _PANEL_W, 3), np.uint8)
    canvas[:, :W] = img_bgr
    canvas[:, W:] = (28, 28, 28)
    _put_text(canvas, "speedrush hud  ·  BEV drivable grid + plan",
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
