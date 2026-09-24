# -*- coding: utf-8 -*-
"""深度几何观测层（架构裁决 2026-09-24：三方同卷考试后，深度区域当几何主人）。

**职责**：帧 → DA-S 视差 → 全局逐行 q20 地面基线 → 相对门区域块 → 双守卫
（射线收敛 + 侧别一致）→ 两侧内沿读数（车道单位）。输出与 BoundarySummary 的
``left_edge_lane / right_edge_lane`` 字段鸭子同构，_EgoRoadObserver 原样消费；
黄线 detect_boundary 退役为骨架（照跑照记账，不再当 road_offset 的证据源）。

**为什么是这套读法**（证据链 commit ef5a42d、1a08321；122 帧三方同卷 +
守卫阈值标定）：
- **全局 q20 基线而非行中位数**：车骑上路缘时人行道占近半行宽，中位数把
  "最大连片群体"当地面 ⇒ 真边界成负台阶、只认正偏差的管线全盲；滑动低分位
  是局部的、跨不过车身同样失效——全局低分位才能把少数派路面钉成地面。
- **区域而非逐行**：路面时间噪声地板 σ0.39%、台阶读数 4.5%（自身 4.2σ），
  单像素不可信；边界=贯穿多行朝消失点收敛的线，车/金币=行跨度有限的团块
  ⇒ 相对门 + 横向收紧 + 连通块 + 触画面侧边 + 跨行门槛。
- **守卫**（122 帧标定，锚点全部分开）：①射线收敛——内沿 x(y) 稳健拟合
  后外推到地平线须落在 VP 附近（拒 518 帧出租车伪块）；②侧别一致——近带
  读数 x_lane 须在自己一侧（拒贴护栏帧的翻面块）；③近带行 ≥2——块主体在
  远带（归一发散区）时读数本身不可用，整块弃权；④门本底——内沿内侧
  40~120px 窗的相对偏离中位 >门的一半 ⇒ 门已失去物理语义，该侧弃权
  （第一关终选附带守卫：跨档落分辨率时让失效档位主动交权）。

**降级路径**：权重缺失/推理异常 → observe 返回 None，road_offset 链路退回
纯模型积分（旧行为不变）；ego_mask 缺失 → 跳过挖除并 WARNING（合并桥风险
回升，双守卫部分兜底）。

**部署待办**：默认 @518（fp16，同卷考试口径）；部署规格已裁定 @518 超线性
出局、@392 为 15fps 档——但 @392 的区域口径未过考（同卷复核 obstacle 层
坏率 44%、真块被守卫误杀），落档前须重标守卫并复跑同卷。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from maaracing_master.plugins.speedrush.world_model import Calib, x_lane_of

# ── 流水常数（冻结口径，与三方同卷/守卫标定一致）────────────────────────
Y0, Y1, DIAG_Y1 = 340, 690, 715   # 检测带 / 诊断带（含近场）
EGO_COLS = (540, 740)             # 基线统计的非自车列带
GATE = 0.02                       # 相对门 (M−g)/g：噪声地板 0.39% 与台阶 4.5% 的中间量
HOLD = 12                         # 横向持续收紧窗（px）
MIN_SPAN = 100                    # 块最小跨行（行跨度有限的团块不够格当边界）
Q_GROUND = 0.20                   # 全局逐行低分位（q20rescue 定案值）
DEFAULT_SHORT = 518               # DA 推理短边（同卷口径；部署落档见模块头注）

# ── 守卫阈值（122 帧标定：conv/resid/侧别全过 + 近带行≥2；锚点 518L 留、
#    518R 出租车拒、437R 留、437L 翻面拒、000420 横贯双侧拒、远带背景块拒）──
NEAR_LO, NEAR_HI, MIN_NEAR = 550, 700, 2
FIT_MIN_ROWS, FIT_RESID_PX = 20, 30.0
CONV_MAX_PX = 350.0    # |x(Y_H) − vpx|：边界线外推须过 VP 附近
RESID_MAX_PX = 45.0    # 内沿直线拟合的中位残差
LANE_SIDE_MIN = 0.15   # 近带读数 x_lane 的侧别门（车道）
BASE_LO_PX, BASE_HI_PX = 40, 120   # 本底守卫窗：内沿内侧 40~120px（路面侧）


@dataclass(frozen=True)
class DepthRoadReading:
    """一帧的深度几何读数。edge_lane 与 BoundarySummary 同名同义（车道单位，
    原点=车），_EgoRoadObserver 鸭子消费；弃权侧为 None。"""

    left_edge_lane: float | None
    right_edge_lane: float | None
    left_x: float | None          # 近带内沿中位（px，诊断）
    right_x: float | None
    sides: int                    # 在场侧数（0~2）
    latency_ms: float
    rejects: tuple[str, ...] = field(default_factory=tuple)


def preprocess(rgb: np.ndarray, short: int) -> np.ndarray:
    """短边缩到 ``short``、各边取 14 的倍数（DPTImageProcessor 同口径）→ NCHW。"""
    h, w = rgb.shape[:2]
    r = short / min(h, w)
    nh, nw = int(round(h * r)), int(round(w * r))
    nh, nw = nh - nh % 14, nw - nw % 14
    img = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    mean = np.array([.485, .456, .406], np.float32)
    std = np.array([.229, .224, .225], np.float32)
    return ((img - mean) / std).transpose(2, 0, 1)[None]


def load_session(weights: Path) -> ort.InferenceSession:
    """DA-S ONNX 会话（DML 优先，逐级回退 CPU——权重是相对视差，与提供器无关）。"""
    avail = set(ort.get_available_providers())
    providers = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider",
                             "CPUExecutionProvider") if p in avail]
    return ort.InferenceSession(str(weights), providers=providers)


def infer_map(sess: ort.InferenceSession, rgb: np.ndarray, short: int) -> np.ndarray:
    """→ 原帧尺寸（720×1280）的 float32 视差图（大=近）。"""
    d = sess.run(None, {"pixel_values": preprocess(rgb, short)})[0][0]
    d = np.nan_to_num(d.astype(np.float32), nan=0.0)
    return cv2.resize(d, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def _ground_q20(m: np.ndarray) -> np.ndarray:
    """全局逐行低分位地面基线 g(y)（长度 = DIAG_Y1−Y0）。

    全局（全行宽非自车列）而非局部窗：滑窗跨不过车身，骑缘帧上人行道自己的
    下尾就成了基线；全局 q20 才把少数派路面钉成地面。"""
    cols = np.r_[0:EGO_COLS[0], EGO_COLS[1]:m.shape[1]]
    return np.quantile(m[Y0:DIAG_Y1][:, cols].astype(np.float32), Q_GROUND, axis=1)


def _rel_and_blocks(m: np.ndarray, ego_mask: np.ndarray | None
                    ) -> tuple[np.ndarray,
                               list[tuple[int, int, dict[int, int], dict[int, int], bool, bool]]]:
    """相对偏离图 r=(M−g)/g + 非地面区域块提取。

    块 = 相对门 → 挖自车（断车身→护栏→墙合并桥）→ 横向收紧 → 8 向连通块 →
    触画面侧边且跨行 ≥MIN_SPAN。innerL/innerR 按 x<640 分流取内沿
    （横贯块两侧各自可用）。r 供本底守卫复用（同一张图，不重算）。"""
    g = _ground_q20(m)
    r = np.full(m.shape, np.nan, np.float32)
    r[Y0:DIAG_Y1] = (m[Y0:DIAG_Y1] - g[:, None]) / np.maximum(g[:, None], 1e-6)
    over = np.where(np.nan_to_num(r, nan=-1) > GATE, 1.0, 0.0)
    if ego_mask is not None:
        over[ego_mask] = 0.0
    mask = (cv2.filter2D(over, -1, np.ones((1, HOLD), np.float32)) >= HOLD).astype(np.uint8)
    ncc, lab = cv2.connectedComponents(mask, connectivity=8)[:2]
    out = []
    for c in range(1, ncc):
        ys, xs = np.nonzero(lab == c)
        touchL = bool(xs.min() <= 2)
        touchR = bool(xs.max() >= 1277)
        if not (touchL or touchR) or ys.max() - ys.min() < MIN_SPAN:
            continue
        innerL: dict[int, int] = {}
        innerR: dict[int, int] = {}
        for y, x in zip(ys, xs):
            if x >= 640:
                innerR[y] = min(innerR.get(y, 1 << 20), int(x))
            else:
                innerL[y] = max(innerL.get(y, -1), int(x))
        out.append((int(ys.min()), int(ys.max()), innerL, innerR, touchL, touchR))
    return r, out


def _baseline_ok(r: np.ndarray, inner: dict[int, int], side: str) -> bool:
    """门本底守卫：内沿**内侧**（路面侧）40~120px 窗的相对偏离中位须 ≈0。

    门 G 的物理语义是"路面 vs 边界外的高差"；本底窗已被门吃掉一半以上
    （>GATE/2）时读数是"松门交点"而非边界——门已失效，该侧主动弃权。
    跨档落分辨率时 g 随之压缩、本底抬升，此守卫让失效档位交出决策权
    （第一关终选附带守卫，测量口=金标线内侧窗的生产化替代）。"""
    vals: list[float] = []
    for y in range(NEAR_LO, NEAR_HI + 1, 5):
        x0 = inner.get(y)
        if x0 is None:
            continue
        lo, hi = ((x0 + BASE_LO_PX, x0 + BASE_HI_PX) if side == "L"
                  else (x0 - BASE_HI_PX, x0 - BASE_LO_PX))
        lo, hi = max(lo, 0), min(hi, r.shape[1])
        if hi - lo < 8:
            continue
        v = r[y, lo:hi]
        v = v[np.isfinite(v)]
        if len(v):
            vals.append(float(np.median(v)))
    if len(vals) < 3:
        return True
    return abs(float(np.median(vals))) <= GATE / 2.0


def _fit(inner: dict[int, int], cal: Calib) -> dict | None:
    """内沿几何特征 + 双守卫裁决 → None（<20 行不评）或 {dead, conv, resid, lane}。"""
    ys = np.array(sorted(inner), float)
    xs = np.array([inner[int(y)] for y in ys], float)
    if len(ys) < FIT_MIN_ROWS:
        return None
    sl, ic = np.polyfit(ys, xs, 1)
    keep = np.abs(xs - (sl * ys + ic)) <= FIT_RESID_PX
    if keep.sum() >= FIT_MIN_ROWS:
        sl, ic = np.polyfit(ys[keep], xs[keep], 1)
    near = ys[(ys >= NEAR_LO) & (ys <= NEAR_HI)]
    if len(near) < MIN_NEAR:
        return {"dead": True, "n": int(len(ys)), "near": int(len(near))}
    med_x = float(np.median([inner[int(y)] for y in near]))
    return {"dead": False, "n": int(len(ys)), "near": int(len(near)),
            "conv": abs(float(sl * cal.y_h + ic) - cal.vpx),
            "resid": float(np.median(np.abs(xs - (sl * ys + ic)))),
            "x_near": med_x,
            "lane": x_lane_of(int(round(med_x)), int(np.median(near)), cal)}


def _pass(f: dict, side: str) -> bool:
    """双守卫 + 近带行门槛的通过判据（阈值见模块头注标定锚点）。"""
    if f["dead"]:
        return False
    if f["conv"] > CONV_MAX_PX or f["resid"] > RESID_MAX_PX:
        return False
    return f["lane"] <= -LANE_SIDE_MIN if side == "L" else f["lane"] >= LANE_SIDE_MIN


def reading_from_map(m: np.ndarray, cal: Calib,
                     ego_mask: np.ndarray | None) -> DepthRoadReading:
    """深度图 → 读数（纯函数，回归锁可直接喂缓存图；observe=推理+本函数）。"""
    r, blocks = _rel_and_blocks(m, ego_mask)
    edges: dict[str, tuple[float, float] | None] = {"L": None, "R": None}
    rejects: list[str] = []
    for side in ("L", "R"):
        tkey = 4 if side == "L" else 5
        cands = sorted((b for b in blocks if b[tkey]),
                       key=lambda b: b[1] - b[0], reverse=True)
        for _, _, il, ir, _, _ in cands:
            inner = il if side == "L" else ir
            if not inner:
                continue
            f = _fit(inner, cal)
            if f is None:
                rejects.append(f"{side}:行不足")
                break
            if not _pass(f, side):
                rejects.append(
                    f"{side}:" + ("远带" if f["dead"] else
                                  f"conv{f['conv']:.0f}/res{f['resid']:.0f}/"
                                  f"lane{f['lane']:+.2f}"))
                continue
            if not _baseline_ok(r, inner, side):
                rejects.append(f"{side}:门本底失效")
                continue
            edges[side] = (f["x_near"], f["lane"])
            break
    lx, rx = edges["L"], edges["R"]
    return DepthRoadReading(
        left_edge_lane=None if lx is None else lx[1],
        right_edge_lane=None if rx is None else rx[1],
        left_x=None if lx is None else lx[0],
        right_x=None if rx is None else rx[0],
        sides=int(lx is not None) + int(rx is not None),
        latency_ms=0.0,
        rejects=tuple(rejects))


class DepthRoadObserver:
    """深度几何观测器（阶段生命期=chain；ORT session 跨阶段复用、外部注入）。

    session 为 None（权重缺失/加载失败）时 observe 恒 None——road_offset 退回
    纯模型积分，与黄线层退役前的"无边界"路径行为一致。"""

    def __init__(self, session: ort.InferenceSession | None,
                 cal: Calib, short: int = DEFAULT_SHORT) -> None:
        self._sess = session
        self._cal = cal
        self._short = short
        self._ego_mask = self._load_ego_mask()

    @staticmethod
    def _load_ego_mask() -> np.ndarray | None:
        """块掩码挖除区 = 自车高区矩形 ∪ 车带下延（同一 JSON 的列带向下到底）。

        上半身矩形（y0~y1）是深度里看得见的车身高读数区，皮肤相关（换车重算）；
        车带下延（y1~检测带底 × 同列带）按结构事实泛化：追车相机恒把车钉画面
        中央 ⇒ 真边界永不进入中央列带（贴墙极限 x=217/1288 仍在带外），
        下延挖多无伤——车尾/车身中段的粘连残迹（ego_mask 下缘之下）被整体
        排除，不需要也不应该对"车屁股"做精细建模。"""
        p = Path(__file__).resolve().parent / "resources" / "calibration" / "ego_mask.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            mask = np.zeros((720, 1280), bool)
            mask[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
            mask[d["y1"]:DIAG_Y1, d["x0"]:d["x1"]] = True
            return mask
        except Exception:
            return None

    def observe(self, frame_rgb: np.ndarray) -> DepthRoadReading | None:
        """一帧 → 读数（两侧可各自弃权）；推理失败 → None（不抛，控制链不因感知停摆）。"""
        if self._sess is None:
            return None
        t0 = time.perf_counter()
        try:
            m = infer_map(self._sess, frame_rgb, self._short)
        except Exception:
            return None
        reading = reading_from_map(m, self._cal, self._ego_mask)
        return replace(reading,
                       latency_ms=(time.perf_counter() - t0) * 1000.0)
