# -*- coding: utf-8 -*-
"""边界感知层 v1（古典 CV，沿用旧栈黄线口径）：驾驶帧 → BoundarySummary 摘要。

**分层归属**（control-route §一「边界感知」行；契约消费方是校验层与横向位移代价，
**路缘不进目标候选集、不参与评分**——设计稿 v2 §三对审查 §一.2 的裁定）。

**与旧栈的差异是升级不是重写**（archive/racing/loop.py::_detect_lane 的 HSV 黄色范围
[20,80,80]–[30,255,255]、形态学去噪、左右分类三件沿用）：旧栈没有标定，只能靠
Hough 直线 + 角度分类在像素空间里凑；本层有 Gate-0 常数（vpx/y_h，38 场收杆）与
A1 尺子（world_model.x_lane_of），于是拿到一条**免费的强判据**——
透视正确时，直道的路缘经归一后 `x_lane` 沿行恒定（路缘是过消失点的直线）。
所以"直道假设残差"就是路缘点集 x_lane 的散布度，弯道帧会系统性漂——
不需要 Hough，也不需要为判弯道再造一条像素通路。

**扫描带**：行 y_h+30 至 y_h+230、每 4 行采样。下沿不越过画面底、上沿不贴近
地平线（远处归一发散，§8 误差注记同族；且带内黄线最长最稳）。
**口径边界（如实声明）**：隧道/夜间/雨天黄线褪色属未验证域——validity 只报
"检没检到、几不一致"，不报"检到的可信度"；阈值全部起值，定档走到场轮回放。
"""

from __future__ import annotations

import cv2
import numpy as np

from maaracing_master.plugins.speedrush.tracking import BoundarySummary
from maaracing_master.plugins.speedrush.world_model import Calib, x_lane_of

# 旧栈验证过的黄色范围（含阴影暗黄；S/V 下限沿用）
_HSV_LOW = np.array([20, 80, 80], dtype=np.uint8)
_HSV_HIGH = np.array([30, 255, 255], dtype=np.uint8)

BAND_TOP_OFF = 30      # 扫描带上沿相对地平线（px）
BAND_BOT_OFF = 230     # 扫描带下沿相对地平线（px）
BAND_STEP = 4          # 采样行距（px）
MIN_RUN_PX = 6         # 单侧黄色连续段最小宽度（更窄当噪点）
MIN_COVERAGE = 0.5     # 双侧检出行占比低于此 → validity False
KERNEL = np.ones((3, 3), np.uint8)

_SCHEMA = 1


def _edge_runs(mask_row: np.ndarray) -> tuple[int | None, int | None]:
    """一行掩码里取左/右各一段连续黄区的中点（左右以画面中线分侧）。"""
    xs = np.flatnonzero(mask_row)
    if xs.size == 0:
        return None, None
    runs: list[tuple[int, int]] = []   # (start, end_exclusive)
    start = prev = int(xs[0])
    for x in xs[1:]:
        x = int(x)
        if x != prev + 1:
            runs.append((start, prev + 1))
            start = x
        prev = x
    runs.append((start, prev + 1))
    mid = mask_row.shape[0] // 2
    left = right = None
    for s, e in runs:                     # 左取最左合格段、右取最右合格段
        if e - s < MIN_RUN_PX:
            continue
        c = (s + e) / 2.0
        if c < mid and left is None:
            left = c
        if c >= mid:
            right = c                     # runs 按 x 升序，持续覆盖即最右
    return left, right


def detect_boundary(frame_rgb: np.ndarray, cal: Calib = Calib(),
                    scan_top_off: int = BAND_TOP_OFF,
                    scan_bot_off: int = BAND_BOT_OFF,
                    step: int = BAND_STEP) -> BoundarySummary:
    """一帧 → BoundarySummary。检不出来时 validity=False + 数值字段尽量给，
    调用方（校验层）以 validity 为一票否决，不得消费半可信摘要。"""
    h, w = frame_rgb.shape[:2]
    y0 = max(int(cal.y_h) + scan_top_off, 0)
    y1 = min(int(cal.y_h) + scan_bot_off, h)
    band = frame_rgb[y0:y1]
    hsv = cv2.cvtColor(band, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, _HSV_LOW, _HSV_HIGH)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, KERNEL)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, KERNEL)

    # 行对齐纪律：rows/lefts/rights 三个列表同索引推进，缺一侧以 NaN 占位——
    # 错位一格，后面所有逐行拟合就整盘皆错，这是本函数最容易写错的点。
    rows2: list[int] = []
    l2: list[float] = []
    r2: list[float] = []
    n_rows = 0
    for y in range(y0, y1, step):
        n_rows += 1
        lx, rx = _edge_runs(mask[y - y0])
        if lx is None and rx is None:
            continue
        rows2.append(y)
        l2.append(lx if lx is not None else float("nan"))
        r2.append(rx if rx is not None else float("nan"))
    both = sum(1 for a, b in zip(l2, r2) if not (np.isnan(a) or np.isnan(b)))
    coverage = both / n_rows if n_rows else 0.0
    valid = coverage >= MIN_COVERAGE

    if not valid or both == 0:
        return BoundarySummary(
            schema_version=_SCHEMA,
            left_x=float(l2[0]) if l2 and not np.isnan(l2[0]) else float("nan"),
            right_x=float(r2[0]) if r2 and not np.isnan(r2[0]) else float("nan"),
            road_width=float("nan"),
            straight_residual=float("nan"),
            vp_row=None,
            validity=False,
            uncertainty=float("nan"))

    la = np.asarray(l2, dtype=float)
    ra = np.asarray(r2, dtype=float)
    ry = np.asarray(rows2, dtype=float)

    # 摘要字段口径（契约注释即此三行）：left/right_x 取**扫描带上沿**检出中点、
    # road_width 取**下沿**（近处最宽最稳、车道级余量的直接量）。
    def _first_valid(arr: np.ndarray, default: float) -> float:
        idx = np.flatnonzero(~np.isnan(arr))
        return float(arr[idx[0]]) if idx.size else default

    def _last_valid(arr: np.ndarray, default: float) -> float:
        idx = np.flatnonzero(~np.isnan(arr))
        return float(arr[idx[-1]]) if idx.size else default

    left_x = _first_valid(la, float("nan"))
    right_x = _first_valid(ra, float("nan"))
    road_width = _last_valid(ra, float("nan")) - _last_valid(la, float("nan"))

    # 直道残差：逐行 x_lane 的散布度——左右缘**各算各的**（并池切半会把两条
    # 本应分离的边线混成双峰，那测的是路宽不是直度），取更大的一侧。
    xl = np.array([x_lane_of(int(a), int(yb), cal) if not np.isnan(a) else np.nan
                   for a, yb in zip(la, ry)])
    xr = np.array([x_lane_of(int(b), int(yb), cal) if not np.isnan(b) else np.nan
                   for b, yb in zip(ra, ry)])

    def _disp(arr: np.ndarray) -> float:
        a = arr[~np.isnan(arr)]
        return float(np.std(a)) if a.size >= 2 else 0.0

    residual = max(_disp(xl), _disp(xr))

    # vp_row：左右缘各自线性拟合 x(y)，交点行对 y_h 的漂移；近平行（拟合退化）→ None
    vp_row = None
    li = np.flatnonzero(~np.isnan(la))
    ri = np.flatnonzero(~np.isnan(ra))
    if li.size >= 4 and ri.size >= 4:
        pl = np.polyfit(ry[li], la[li], 1)
        pr = np.polyfit(ry[ri], ra[ri], 1)
        da = pl[0] - pr[0]
        if abs(da) > 1e-6:
            yv = (pr[1] - pl[1]) / da
            drift = yv - cal.y_h
            # 交点明显出带（曲率主导的假交点）不给 vp 数，宁缺毋滥
            if 0.0 <= yv <= y1:
                vp_row = float(drift)

    # uncertainty：左缘对其线性拟合的残差 RMS（px），供校验层判"检到的线稳不稳"
    unc = float("nan")
    if li.size >= 4:
        pl = np.polyfit(ry[li], la[li], 1)
        unc = float(np.sqrt(np.mean((la[li] - np.polyval(pl, ry[li])) ** 2)))

    return BoundarySummary(
        schema_version=_SCHEMA, left_x=left_x, right_x=right_x,
        road_width=float(road_width), straight_residual=residual,
        vp_row=vp_row, validity=bool(valid), uncertainty=unc)
