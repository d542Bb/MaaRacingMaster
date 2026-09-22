# -*- coding: utf-8 -*-
"""边界感知层 v2（古典 CV + 几何选择器；HSV 宽口与选择器同批上岗，试错史见 _HSV_LOW 注）：驾驶帧 → BoundarySummary 摘要。

**分层归属**（control-route §一「边界感知」行；契约消费方是校验层与横向位移代价，
**路缘不进目标候选集、不参与评分**——设计稿 v2 §三对审查 §一.2 的裁定）。

**与旧栈的差异是升级不是重写**（形态学去噪沿用；HSV 宽口与"最左/最右段"分类为 v2
几何选择器取代，依据见 _HSV_LOW 注）：旧栈没有标定，只能靠
Hough 直线 + 角度分类在像素空间里凑；本层有 Gate-0 常数（vpx/y_h，38 场收杆）与
A1 尺子（world_model.x_lane_of），于是拿到一条**免费的强判据**——
透视正确时，直道的路缘经归一后 `x_lane` 沿行恒定（路缘是过消失点的直线）。
所以"直道假设残差"就是路缘点集 x_lane 的散布度，弯道帧会系统性漂——
不需要 Hough，也不需要为判弯道再造一条像素通路。

**几何选择器（v2，2026-09-22，设计稿 §十三）**：宽 HSV 口下同色系建筑/护栏会冒充
路缘（只放宽颜色不换选择器=road_offset 极值 ±7 车道、实机醉驾，当晚回退）。v2 三段：
全带 run 中心 → x_lane 聚类（间隙切分留主簇）→ **路宽配对**（分离 ∈ [2.8,5.4]，由
RULES 四车道标称 ± 容差派生，非实测 4.72 抄数）→ 配对后按线吸收（偏航扫描碎片归一）。
建筑杂光成不了路宽级配对，被几何拒掉——**颜色负责看得见，几何负责分得清**。
验收（69 坏帧 + 10 实战好帧离线）：坏帧可用检测 10%→90%（双侧 58%+单侧 32%）、
|road_offset|≤2.8 物理界内、好帧 100% 可用。

**扫描带**：行 y_h+30 至 y_h+230、每 4 行采样。下沿不越过画面底、上沿不贴近
地平线（远处归一发散，§8 误差注记同族；且带内黄线最长最稳）。
**口径边界（如实声明）**：隧道/夜间/雨天黄线褪色属未验证域——validity 只报
"检没检到、几不一致"，不报"检到的可信度"；阈值全部起值，定档走到场轮回放。
**单侧容忍**：真机常只单侧黄线可见，validity 采"任一侧稳定即降级可用"，
`sides`（0/1/2）显式记录稳定侧数——sides==1 时 road_width/vp_row/vp_x 不可用（需双侧），
居中/路宽类消费方须先看 sides==2。**vp_x（schema 2）**：两边线交点列，与校准 vpx
的横偏 ∝ 车头航向角——真机证据链（2026-09-22）证明打舵画面是旋转不是平移，
平移假设的归一尺子在打舵帧系统性失真（"观察者失明"），vp_x 是航向的免费观测量，
trace 落列供双积分运动学参数（a_lat_gain/tau_align）回放定档。
"""

from __future__ import annotations

import cv2
import numpy as np

from maaracing_master.plugins.speedrush.tracking import BoundarySummary
from maaracing_master.plugins.speedrush.world_model import Calib, load_calib, x_lane_of

# 黄色范围：宽口 [8,45,80]-[32,255,255]。**2026-09-22 三次迭代定案**（证据：
# control_traces/badframes_* 69 张真帧 + 上午人驾录像离线回放）：
# ①旧栈口 [20,80,80]-[30,255,255] 在本作暗色/黄昏场景整段漏检（路缘实测 H=9~17/
#   S=47~74，坏帧双侧率 0%）；②只放宽颜色不换选择器=同色系建筑/护栏喂毒（road_offset
#   极值 ±7，实机醉驾，当晚回退）；③宽口必须与几何选择器（下方聚类+配对+吸收）同批
#   上线——颜色负责"看得见"，几何负责"分得清"。H 下限 8 避开尾灯红带（0~5）。
_HSV_LOW = np.array([8, 45, 80], dtype=np.uint8)
_HSV_HIGH = np.array([32, 255, 255], dtype=np.uint8)

# 几何选择器（边界层 v2）参数：
CLUSTER_GAP_LANE = 0.4     # x_lane 沿行聚类的切分间隙（车道）
CLUSTER_MIN_ROWS = 12      # 主簇最少行数（带 50 行的 ~24%；建筑杂光到不了）
# 配对窗：路宽标称 4.0 车道（RULES §3.8 四车道，游戏事实）× (1−0.30, 1+0.35)——
# 容差给 a_x 的 18% 标度误差（§7.4 实测）留边距，**不是**把实测 4.72 抄进代码。
ROAD_PAIR_MIN_LANE = 2.8
ROAD_PAIR_MAX_LANE = 5.4

BAND_TOP_OFF = 30      # 扫描带上沿相对地平线（px）
BAND_BOT_OFF = 230     # 扫描带下沿相对地平线（px）
BAND_STEP = 4          # 采样行距（px）
MIN_RUN_PX = 6         # 单侧黄色连续段最小宽度（更窄当噪点）
KERNEL = np.ones((3, 3), np.uint8)

_SCHEMA = 2  # v2：vp_x（航向观测量，2026-09-22 双积分证据链）


def _row_run_centers(mask_row: np.ndarray) -> list[float]:
    """一行掩码里**全部**合格连续段的中点（v2：不再只取最左/最右段——
    宽 HSV 口下同一段里既有真缘也有杂光，选择交给聚类）。"""
    xs = np.flatnonzero(mask_row)
    if xs.size == 0:
        return []
    out: list[float] = []
    start = prev = int(xs[0])
    for x in xs[1:]:
        x = int(x)
        if x != prev + 1:
            if prev + 1 - start >= MIN_RUN_PX:
                out.append((start + prev + 1) / 2.0)
            start = x
        prev = x
    if prev + 1 - start >= MIN_RUN_PX:
        out.append((start + prev + 1) / 2.0)
    return out


def _cluster(votes: list[tuple[float, float, int]],
             gap: float = CLUSTER_GAP_LANE,
             min_rows: int = CLUSTER_MIN_ROWS) -> list[list[tuple[float, float, int]]]:
    """(x_lane, x_px, y) 按 x_lane 排序、间隙切簇，留 ≥min_rows 的主簇。"""
    if not votes:
        return []
    vs = sorted(votes)
    out: list[list] = []
    cur = [vs[0]]
    for v in vs[1:]:
        if v[0] - cur[-1][0] > gap:
            out.append(cur)
            cur = [v]
        else:
            cur.append(v)
    out.append(cur)
    return [c for c in out if len(c) >= min_rows]


def _absorb(seed: list[tuple[float, float, int]],
            others: list[list[tuple[float, float, int]]],
            y_h: float) -> list[tuple[float, float, int]]:
    """种子簇拟线一次，把其他簇里落进线容差的点吸回来——偏航扫描把一条真缘
    切成多段碎片时，碎片各自行数可能不足 min_rows/中点偏置，吸收后归一。"""
    ys = np.array([p[2] for p in seed], dtype=float)
    xs = np.array([p[1] for p in seed], dtype=float)
    a, b = np.polyfit(ys, xs, 1) if len(seed) >= 4 else (0.0, float(xs.mean()))
    out = list(seed)
    for c in others:
        for p in c:
            if abs((a * p[2] + b) - p[1]) <= max(6.0, 0.3 * (p[2] - y_h)):
                out.append(p)
    return out


def detect_boundary(frame_rgb: np.ndarray, cal: Calib | None = None,
                    scan_top_off: int = BAND_TOP_OFF,
                    scan_bot_off: int = BAND_BOT_OFF,
                    step: int = BAND_STEP) -> BoundarySummary:
    """一帧 → BoundarySummary。检不出来时 validity=False + 数值字段尽量给，
    调用方（校验层）以 validity 为一票否决，不得消费半可信摘要。
    cal 缺省读几何真源（gate0.json）。"""
    if cal is None:
        cal = load_calib()
    h, w = frame_rgb.shape[:2]
    y0 = max(int(cal.y_h) + scan_top_off, 0)
    y1 = min(int(cal.y_h) + scan_bot_off, h)
    band = frame_rgb[y0:y1]
    hsv = cv2.cvtColor(band, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, _HSV_LOW, _HSV_HIGH)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, KERNEL)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, KERNEL)

    # ── v2 几何选择器（2026-09-22，设计稿 §十三；宽 HSV 口与之同批上岗）──
    # 行对齐纪律不再需要（无逐行左右配对）：全带收集合格 run 中心 →
    # x_lane 聚类（间隙切分）→ 路宽配对（RULES 四车道 ± 容差，最大票数对）→
    # 配对后按线吸收（偏航扫描碎片归一）。建筑/护栏杂光不成"路宽级"配对，被拒。
    votes: list[tuple[float, float, int]] = []
    for y in range(y0, y1, step):
        for c in _row_run_centers(mask[y - y0]):
            votes.append((x_lane_of(int(c), y, cal), c, y))

    def _side(pts: list[tuple[float, float, int]]) -> dict:
        u = np.array([p[0] for p in pts], dtype=float)
        ys = np.array([p[2] for p in pts], dtype=float)
        xs = np.array([p[1] for p in pts], dtype=float)
        order = np.argsort(ys)
        pf = np.polyfit(ys, xs, 1) if len(pts) >= 4 else None
        rms = (float(np.sqrt(np.mean((xs - np.polyval(pf, ys)) ** 2)))
               if pf is not None else float("nan"))
        return {"med": float(np.median(u)),
                "top": float(xs[order][0]), "bot": float(xs[order][-1]),
                "std": float(np.std(u)) if u.size >= 2 else 0.0,
                "rms": rms,
                "line": (float(pf[0]), float(pf[1])) if pf is not None else (0.0, 0.0)}

    cl = _cluster(votes)
    meds = [float(np.median([p[0] for p in c])) for c in cl]
    pair: tuple[int, int, int] | None = None
    for i in range(len(cl)):
        for j in range(len(cl)):
            sep = meds[j] - meds[i]
            if ROAD_PAIR_MIN_LANE <= sep <= ROAD_PAIR_MAX_LANE:
                sc = len(cl[i]) + len(cl[j])
                if pair is None or sc > pair[0]:
                    pair = (sc, i, j)

    if pair is not None:
        _, i, j = pair
        rest = [c for k, c in enumerate(cl) if k not in (i, j)]
        L = _side(_absorb(cl[i], rest, cal.y_h))
        R = _side(_absorb(cl[j], rest, cal.y_h))
        vp_row = vp_x = None
        pl, pr = L["line"], R["line"]
        da = pl[0] - pr[0]
        if abs(da) > 1e-6:
            yv = (pr[1] - pl[1]) / da
            if 0.0 <= yv <= y1:      # 曲率主导的假交点不给数，宁缺毋滥
                vp_row = float(yv - cal.y_h)
                vp_x = float(pl[0] * yv + pl[1])
        return BoundarySummary(
            schema_version=_SCHEMA, left_x=L["top"], right_x=R["top"],
            road_width=R["bot"] - L["bot"],
            straight_residual=max(L["std"], R["std"]),
            vp_row=vp_row, validity=True,
            uncertainty=L["rms"] if L["rms"] == L["rms"] else R["rms"],
            sides=2, vp_x=vp_x,
            left_edge_lane=L["med"], right_edge_lane=R["med"])

    if cl:
        # 单侧容忍（step 6 裁定沿用）：只有一条主簇时如实记 sides==1——
        # road_width/vp 类需双侧的量不给，缘距给该侧（空间闸门/路观测可用单侧）。
        k = int(np.argmax([len(c) for c in cl]))
        S = _side(_absorb(cl[k], [c for m, c in enumerate(cl) if m != k], cal.y_h))
        left = S["med"] < 0
        return BoundarySummary(
            schema_version=_SCHEMA,
            left_x=S["top"] if left else float("nan"),
            right_x=S["top"] if not left else float("nan"),
            road_width=float("nan"), straight_residual=S["std"],
            vp_row=None, validity=True, uncertainty=S["rms"], sides=1,
            left_edge_lane=S["med"] if left else None,
            right_edge_lane=S["med"] if not left else None)

    return BoundarySummary(
        schema_version=_SCHEMA, left_x=float("nan"), right_x=float("nan"),
        road_width=float("nan"), straight_residual=float("nan"),
        vp_row=None, validity=False, uncertainty=float("nan"), sides=0)
