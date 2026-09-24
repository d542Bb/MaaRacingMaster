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
- **守卫**（122 帧标定 + 金标 44 帧源行重考，锚点全部分开）：①射线收敛——内沿
  x(y) 在**源行**（y−y_h ≥ FIT_FLOOR_PX）上稳健拟合、外推到地平线须落在 VP 附近
  （拒出租车伪块；518 帧的出租车行由两轮剔点自动剔除、真右缘得以在场）；②侧别
  一致——源行评估的 x_lane 须在自己一侧（拒贴护栏帧的翻面块）；③源行不足判
  「远带」弃权（发散区读数误差 ∝ 1/(y−y_h)，不可信）；④门本底——内沿内侧
  40~120px 窗（跟随评估行 ±60）的相对偏离中位 >门的一半 ⇒ 门失去物理语义，
  该侧弃权（拒自车晕影连块产生的挖除切口伪影边）。

**降级路径**：权重缺失/推理异常 → observe 返回 None，road_offset 链路退回
纯模型积分（旧行为不变）；ego_mask 缺失 → 跳过挖除并 WARNING（合并桥风险
回升，双守卫部分兜底）。

**上拍形态**：生产经 AsyncDepthRoadObserver（异步 worker，协议同 treasure OCR）
使用本层——控制拍只付 push+take（≈0.15ms），observe 成本在后台线程；
本模块的同步 observe 保留为纯函数面（测试与离线复算直接调用）。

**读数带（2026-09-24 重定，替代已撤回的 @336 固定近带落档）**：源行=块内
y−y_h≥50 的行、评估行=其剔点后中位——不再绑死 [550,700]（金标 R 51/54 帧边线
在 y<550 已出画，固定带内物理无边）。门 0.08：自车晕影（+3~6%）与墙台阶
（+14~24%）之间取刀，金标 44 帧 @336：L 覆盖 79%、R 23%、双侧同帧 18%、
dev p50 双侧 |·|≤0.14。**推理必须全帧**：带@280 裁天带实验被金标重考+渲染
双重否决——DA 的相对视差按整帧自归一，天空是尺度锚，裁掉后墙/路对比度被
压平（L 覆盖 79%→20%）。@518 在源行规则下覆盖反而更低（R≈0~3/44），维持
质量上限参照档。双侧同帧的缺口由消费侧「每侧保鲜槽」解决（待施工，见 README）。

**时延口径（折叠会话，2026-09-24 收口）**：本层时延数字一律为 load_session 的
free-dim 折叠口径（@336 实测 p50 ≈20ms）；不折叠时图内唯一 cubic Resize（pos_embed
插值）被 DML 拒收落 CPU、占 ~85%（133ms）——落档期的 21.4/24.7ms 即折叠口径，生产
接线一度漏装折叠，已在 load_session 补上。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from maaracing_master.plugins.speedrush.world_model import Calib, x_lane_of

# ── 流水常数（冻结口径，与三方同卷/守卫标定一致）────────────────────────
# 推理必须吃全帧：DA-V2 的相对视差按整帧内容自归一（天空/远景是尺度锚），
# 裁天带实验（2026-09-24，带@280）金标重考负结果 + 渲染定性——墙/路台阶对比
# 被重新归一压平（L 覆盖 61%→20%）。天空不产读数，但产归一化，不能裁。
Y0, Y1, DIAG_Y1 = 340, 690, 715   # 检测带 / 诊断带（含近场）
EGO_COLS = (540, 740)             # 基线统计的非自车列带
GATE = 0.08                       # 相对门 (M−g)/g：自车晕影幅度 +3~6% 与墙台阶
                                  # +14~24% 之间取刀（全帧@336 金标 44 帧网格：
                                  # 0.05 时晕影入门、R 内沿钉上挖除切口 dev −0.18；
                                  # 0.08 时 R dev p50 −0.02、p90 0.12；≥0.10 弱台阶
                                  # 误伤回升。README「取证续」节）
HOLD = 12                         # 横向持续收紧窗（px）
MIN_SPAN = 100                    # 块最小跨行（行跨度有限的团块不够格当边界）
Q_GROUND = 0.20                   # 全局逐行低分位（q20rescue 定案值）
DEFAULT_SHORT = 336               # DA 推理短边（折叠口径 q4f16 p50 ≈22ms；
                                  # @518=91ms 为质量上限档，异步形态下可选，
                                  # 决策数据见 README「全链路时延分解」+金标重考）

# ── 守卫阈值（源行规则，2026-09-24 读数带重定；旧"固定近带 550~700"把源行与
#    评估行绑死、金标 R 51/54 帧带内无边——见 README「实机复核」节）──
FIT_FLOOR_PX = 50    # 源行发散区下限：y − y_h ≥ 50（车道尺误差 ∝ 1/(y−y_h)）
FIT_MIN_SRC = 20     # 源行数门槛（不足则该块该侧弃权「远带」）
FIT_MIN_ROWS, FIT_RESID_PX = 20, 30.0
CONV_MAX_PX = 350.0    # |x(Y_H) − vpx|：边界线外推须过 VP 附近
RESID_MAX_PX = 45.0    # 内沿直线拟合的中位残差
LANE_SIDE_MIN = 0.15   # 源行读数 x_lane 的侧别门（车道）
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
    """DA-S 视差会话（DML 优先，逐级回退 CPU——权重是相对视差，与提供器无关）。

    **折叠是时延成立的前提，不是优化项**：图内唯一一个 cubic Resize（pos_embed 插值）
    被 DML 拒收、落 CPU 且卡在关键路径上，占每拍 ~85%（@336 实测 133ms vs 折叠 20ms，
    2026-09-23 折叠验证 / 2026-09-24 实机复核收口）。把 batch/height/width 三个自由维
    全部固定即触发常量折叠、DML 整图融为单节点——**只固定 height/width 而漏掉
    batch_size 则图完全不变**（折叠验证读事 2，必要条件而非可选）。代价是会话只吃
    preprocess 的定形输出（短边 DEFAULT_SHORT、按标定捕获几何 1280×720 定长宽）：
    捕获几何一变，infer 立即抛形状错——observe 捕获后返回 None，road_offset 退回
    纯模型积分（宁走降级路径，不回落到 133ms 的静默慢道）。"""
    avail = set(ort.get_available_providers())
    providers = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider",
                             "CPUExecutionProvider") if p in avail]
    probe = preprocess(np.zeros((720, 1280, 3), np.uint8), DEFAULT_SHORT)
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"),
                         (probe.shape[0], probe.shape[2], probe.shape[3])):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(weights), sess_options=so, providers=providers)


def infer_map(sess: ort.InferenceSession, rgb: np.ndarray, short: int) -> np.ndarray:
    """→ 原帧尺寸（720×1280）的 float32 视差图（大=近）。全帧输入，见上方归一化注。"""
    d = sess.run(None, {"pixel_values": preprocess(rgb, short)})[0][0]
    d = np.nan_to_num(d.astype(np.float32), nan=0.0)
    return cv2.resize(d, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def _ground_q20(m: np.ndarray) -> np.ndarray:
    """全局逐行低分位地面基线 g(y)（长度 = DIAG_Y1−Y0）。

    全局（全行宽非自车列）而非局部窗：滑窗跨不过车身，骑缘帧上人行道自己的
    下尾就成了基线；全局 q20 才把少数派路面钉成地面。"""
    cols = np.r_[0:EGO_COLS[0], EGO_COLS[1]:m.shape[1]]
    return np.quantile(m[Y0:DIAG_Y1][:, cols].astype(np.float32), Q_GROUND, axis=1)


def _rel_and_blocks(m: np.ndarray, ego_mask: np.ndarray | None,
                    gate: float = GATE
                    ) -> tuple[np.ndarray,
                               list[tuple[int, int, dict[int, int], dict[int, int], bool, bool]]]:
    """相对偏离图 r=(M−g)/g + 非地面区域块提取。

    块 = 相对门（``gate``，落档实验参数化；生产=GATE）→ 挖自车（断车身→
    护栏→墙合并桥）→ 横向收紧 → 8 向连通块 → 触画面侧边且跨行 ≥MIN_SPAN。
    innerL/innerR 按 x<640 分流取内沿（横贯块两侧各自可用）。r 供本底守卫
    复用（同一张图，不重算）。"""
    g = _ground_q20(m)
    r = np.full(m.shape, np.nan, np.float32)
    r[Y0:DIAG_Y1] = (m[Y0:DIAG_Y1] - g[:, None]) / np.maximum(g[:, None], 1e-6)
    # over = (r > gate)：NaN 与 gate 比较恒假，等价于原 nan_to_num(r,-1) > gate
    # （省一次全图画幅拷贝），且用 float32 而非 float64 承载（filter2D 内存减半）。
    over = (r > gate).astype(np.float32)
    if ego_mask is not None:
        over[ego_mask] = 0.0
    mask = (cv2.filter2D(over, -1, np.ones((1, HOLD), np.float32)) >= HOLD).astype(np.uint8)
    # 逐块内沿 = (块, 行) 分组的 L 侧 max x / R 侧 min x（x<640 归 L，x≥640 归 R）。
    # 实现按 WithStats 的 bbox 裁到子区域再用 cv2.reduce 行归约——原逐像素 Python
    # 循环是全链最大单项（每帧 ~19 万像素进解释器；commit 11babe4 cProfile 77%），
    # 归约在 C 层一次算完。语义逐位等价：行内取极值与逐像素累积 min/max 同值，
    # 块仅取 bbox 与全数组 nonzero 同集；块序 = 连通块标签序（供消费端稳定排序）。
    ncc, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for c in range(1, ncc):
        x0, y0, w, h = (int(v) for v in stats[c, :4])
        touchL = x0 <= 2
        touchR = x0 + w - 1 >= 1277
        if not (touchL or touchR) or h - 1 < MIN_SPAN:
            continue
        sel = lab[y0:y0 + h, x0:x0 + w] == c
        grid = np.broadcast_to(np.arange(x0, x0 + w, dtype=np.float32), (h, w))
        lcol = cv2.reduce(np.where(sel & (grid < 640), grid, np.float32(-1.0)),
                          1, cv2.REDUCE_MAX).reshape(-1)
        rows = np.nonzero(lcol >= 0)[0]
        innerL = dict(zip((rows + y0).tolist(), lcol[rows].astype(np.int32).tolist()))
        rcol = cv2.reduce(np.where(sel & (grid >= 640), grid, np.float32(1e9)),
                          1, cv2.REDUCE_MIN).reshape(-1)
        rows = np.nonzero(rcol <= 1279)[0]
        innerR = dict(zip((rows + y0).tolist(), rcol[rows].astype(np.int32).tolist()))
        out.append((y0, y0 + h - 1, innerL, innerR, touchL, touchR))
    return r, out


def _baseline_ok(r: np.ndarray, inner: dict[int, int], side: str, y_ref: float,
                 gate: float = GATE) -> bool:
    """门本底守卫：内沿**内侧**（路面侧）40~120px 窗的相对偏离中位须 ≈0。

    门 ``gate`` 的物理语义是"路面 vs 边界外的高差"；本底窗已被门吃掉一半以上
    （>gate/2）时读数是"松门交点"而非边界——门已失效，该侧主动弃权。
    窗随源行迁移（y_ref±60）：源行规则下边在哪些行可见逐帧不同，固定行窗会
    整体跳过守卫（2026-09-24 读数带重定一并修正）。"""
    vals: list[float] = []
    for y in range(int(y_ref) - 60, int(y_ref) + 60, 5):
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
    return abs(float(np.median(vals))) <= gate / 2.0


def _fit(inner: dict[int, int], cal: Calib) -> dict | None:
    """内沿几何特征 + 守卫裁决所需量 → None（<20 行不评）或 {dead, conv, resid, lane, y_ref}。

    源行规则（2026-09-24 读数带重定）：车道量沿 3D 直线不变 ⇒ 读数不必绑死在固定
    近带——凡 y−y_h ≥ FIT_FLOOR_PX 的行都是合法源行。**拟合也只在源行上做**：
    发散区行的内沿噪声会把直线拽歪（金标 A/B：全行拟合 R dev p50 −0.20，
    源行拟合 −0.02），源行不足 FIT_MIN_SRC 判「远带」弃权。"""
    ys_all = np.array(sorted(inner), float)
    if len(ys_all) < FIT_MIN_ROWS:
        return None
    ys = ys_all[ys_all >= cal.y_h + FIT_FLOOR_PX]
    if len(ys) < FIT_MIN_SRC:
        return {"dead": True, "n": int(len(ys_all)), "near": int(len(ys))}
    xs = np.array([inner[int(y)] for y in ys], float)
    sl, ic = np.polyfit(ys, xs, 1)
    keep = np.abs(xs - (sl * ys + ic)) <= FIT_RESID_PX
    if keep.sum() >= FIT_MIN_ROWS:
        sl, ic = np.polyfit(ys[keep], xs[keep], 1)
        ys, xs = ys[keep], xs[keep]   # 剔点后再取中位：keep 不对称时 med_x 会偏
    med_x = float(np.median(xs))
    y_ref = float(np.median(ys))
    return {"dead": False, "n": int(len(ys_all)), "near": int(len(ys)),
            "conv": abs(float(sl * cal.y_h + ic) - cal.vpx),
            "resid": float(np.median(np.abs(xs - (sl * ys + ic)))),
            "x_near": med_x, "y_ref": y_ref,
            "lane": x_lane_of(int(round(med_x)), int(round(y_ref)), cal)}


def _pass(f: dict, side: str) -> bool:
    """双守卫 + 近带行门槛的通过判据（阈值见模块头注标定锚点）。"""
    if f["dead"]:
        return False
    if f["conv"] > CONV_MAX_PX or f["resid"] > RESID_MAX_PX:
        return False
    return f["lane"] <= -LANE_SIDE_MIN if side == "L" else f["lane"] >= LANE_SIDE_MIN


def reading_from_map(m: np.ndarray, cal: Calib, ego_mask: np.ndarray | None,
                     gate: float = GATE) -> DepthRoadReading:
    """深度图 → 读数（纯函数，回归锁可直接喂缓存图；observe=推理+本函数）。"""
    r, blocks = _rel_and_blocks(m, ego_mask, gate)
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
            if not _baseline_ok(r, inner, side, f["y_ref"], gate):
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

    @property
    def session_ready(self) -> bool:
        return self._sess is not None


class AsyncDepthRoadObserver:
    """深度几何异步观测器（2026-09-24 解耦落地，协议与 treasure OCR worker 同构）。

    为什么解耦这一层：observe ≈26ms p50 / 31.5ms p95（折叠 + 后处理向量化后的口径，
    probe_latency 实测），同步形态把控制拍预算吃光（回路在 14~20Hz 间赌运气）。
    路缘是**慢变量**——滞后 1~2 拍（50~100ms）读数仍有效；金币/街车是快变目标、
    必须与本拍像素同源，所以感知与黄线层不做异步（treasure「令牌与读数字节同源」
    的教训：跨线程搬运必有窗口，快变对象上窗口=脏读）。

    协议（latest-only，无队列不积压）：
    - push：主线程拷帧覆盖 pending 槽 + wakeup；worker 慢则丢中间帧（丢的是输入，
      不是结果——路缘下一拍还会再来）。captured_ts 用 perf_counter（与耗时/时效
      同族时钟；monotonic 在本机粒度 ~16ms 会把 age 量化）。
    - worker：pop latest → 同步 observe → **非 None 才发布**结果槽（整包替换，
      不原地改已发布对象）；顶层 try/except 计 failures，单帧异常不杀 daemon。
    - take：主线程消费结果槽（取走即清，一拍最多应用一次）；
      age = now − captured_ts 超 max_age_ms → 计 stale 丢弃、本拍返回 None
      （road_offset 退纯模型积分——与 session 缺失同一降级路径，宁旧不如无）。
    - health：applied/stale/failures 计数 + age/duration 滑窗——不达标报警数据面。
    """

    MAX_AGE_MS = 150.0    # 结果时效预算：20Hz 拍 ×3 拍；超龄读数按 stale 丢弃
    PERF_WINDOW = 200     # age/duration 滑窗（与 treasure 同族口径：判据只看尾部）

    def __init__(self, observer: DepthRoadObserver,
                 max_age_ms: float = MAX_AGE_MS) -> None:
        self._obs = observer
        self._max_age_ms = float(max_age_ms)
        self._lock = threading.Lock()
        self._pending: tuple[int, np.ndarray, float] | None = None
        self._result: tuple[DepthRoadReading, float] | None = None
        self._pushed = 0
        self._applied = 0
        self._stale_drops = 0
        self._failures = 0
        self._age_win: deque[float] = deque(maxlen=self.PERF_WINDOW)
        self._dur_win: deque[float] = deque(maxlen=self.PERF_WINDOW)
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_age_ms = 0.0

    # ---------- 生命周期（阶段=chain：建即 start，收口 stop） ----------

    def start(self) -> None:
        """session 不可用则不起线程（push/take 全程空转，行为=session 缺失）。"""
        if self._thread is not None or not self._obs.session_ready:
            return
        self._thread = threading.Thread(
            target=self._loop, name="speedrush-depth-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """停 worker；返回是否干净退出（False=超时未退，调用方决定怎么报警——
        本层不引 logger，日志归 module）。"""
        if self._thread is None:
            return True
        self._stop.set()
        self._wakeup.set()   # 唤醒阻塞在 wait 的 worker 立即退出
        self._thread.join(timeout=timeout)
        alive = self._thread.is_alive()
        self._thread = None
        return not alive

    # ---------- 主线程面 ----------

    def push(self, frame_rgb: np.ndarray) -> None:
        """无 worker（session 缺失/start 未调）时直接空转：不拷帧、不计数——
        阶段出口的"零结果"报警以 pushed>0 为前提，计数了就会误报。"""
        if self._thread is None:
            return
        with self._lock:
            self._pushed += 1
            self._pending = (self._pushed, frame_rgb.copy(), time.perf_counter())
        self._wakeup.set()

    def take(self) -> DepthRoadReading | None:
        """消费最新结果（取走即清）。超龄返回 None 并计 stale——控制侧按"本拍无读数"处理。"""
        with self._lock:
            item = self._result
            self._result = None
            if item is None:
                return None
            reading, captured_ts = item
        age_ms = (time.perf_counter() - captured_ts) * 1000.0
        self.last_age_ms = age_ms
        self._age_win.append(age_ms)
        self._dur_win.append(reading.latency_ms)
        if age_ms > self._max_age_ms:
            with self._lock:
                self._stale_drops += 1
            return None
        with self._lock:
            self._applied += 1
        return reading

    def health(self) -> dict:
        with self._lock:
            pushed, applied, stale, failures = (
                self._pushed, self._applied, self._stale_drops, self._failures)
        ages, durs = list(self._age_win), list(self._dur_win)

        def _p(xs: list[float], q: float) -> float:
            return 0.0 if not xs else sorted(xs)[min(len(xs) - 1, int(q * (len(xs) - 1)))]

        return {"pushed": pushed, "applied": applied, "stale_drops": stale,
                "failures": failures,
                "age_p50": _p(ages, 0.5), "age_p95": _p(ages, 0.95),
                "dur_p50": _p(durs, 0.5), "dur_p95": _p(durs, 0.95),
                "max_age_ms": self._max_age_ms}

    # ---------- worker 线程 ----------

    def _pop_latest(self) -> tuple[int, np.ndarray, float] | None:
        with self._lock:
            item, self._pending = self._pending, None
            return item

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._pop_latest()
                if item is None:
                    self._wakeup.wait(timeout=0.5)
                    self._wakeup.clear()
                    continue
            except Exception:  # noqa: BLE001 —— 取帧异常（理论不可达）不杀 daemon
                self._failures += 1
                continue
            try:
                reading = self._obs.observe(item[1])
            except Exception:  # noqa: BLE001 —— 单帧异常计数后继续（与 treasure 同姿态）
                self._failures += 1
                continue
            if reading is None:  # session 中途失效/推理异常：本帧无结果，不发布
                continue
            with self._lock:
                self._result = (reading, item[2])  # captured_ts 随结果过闸（age 的锚）
