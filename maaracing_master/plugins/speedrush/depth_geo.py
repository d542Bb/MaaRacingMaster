# -*- coding: utf-8 -*-
"""深度几何观测层（架构裁决 2026-09-24：深度区域当 road_offset 几何主人）。

**职责**：帧 → DA-S 视差 → 视差点云（近似米制）→ 倾角感知平面拟合分地/障 →
逐行提边（同行路障内沿夹紧 + 车框遮挡逐行弃权）→ 两侧内沿读数（车道单位）。
输出与 BoundarySummary 的 ``left_edge_lane / right_edge_lane`` 字段鸭子同构，
_EgoRoadObserver 原样消费。

**为什么是这套读法**（55 金标帧同判据对比定案 2026-09-26，用户拍板）：
- **平面拟合而非逐行基线**：旧消费算法（全局逐行 q20 低分位 + 固定相对门）已被
  金标重考证伪（相对门对"高度"表征的依赖随距离衰减、远带 q20 锁雾），不再回退。
  视差经近似内参反投影成 (X, Y, Z) 点云后，地面是 Y=aX+bZ+c 平面——弯道倾斜、
  相机 pitch 全部被平面系数吸收，地面带 |res|<0.02Z+0.3、上凸体（墙/车/路障）
  res<−0.15，物理量纲直读。
- **逐帧尺度自标定**：DA 相对视差缺米制（Z=s/d 的 s 逐帧漂 CV≈14%，天空是
  归一化锚）。产码禁依赖 UniDepth（不进包）——以**路半宽为已知常数**自标定：
  种子尺度下平面拟合+提边量出近带半宽，s ← S0·W_REF/实测半宽（夹在种子 ±2 倍、
  实测半宽须物理合理），第二遍在标定后的米制系里重跑拟合与提边。度量门限
  （0.3m 地面带、1.5m 夹紧 GAP）由此逐帧自洽。
- **车道量走 px+相机几何**（与黄线层/世界模型同一约定）：车道量对整帧均匀缩放
  不变（u=fx·sX/sZ+cx 约掉 s）——尺度漂移对控制无害是本设计的承重推论；fx 绝对
  值的误差进一步被逐帧标定吸收，内参只需承担 fx/fy 各向异性。
- **车框遮挡逐行弃权**：YOLO 掩码（生产已有）∪ ego 静态掩码挖除 + 该行掩码像素
  的 |X|>GAP 判该侧遮挡——遮挡行不产读数，宁缺毋假。

**标定常量出处**（金标 54 帧 UniDepth fp32 场实测钉定，2026-09-27；改数=重跑
标定探针，不是调参）：FY/fx 比/主点=UniDepth 自估内参跨帧中位（fx 逐帧漂 18%
但 fx/fy 比值仅 4%——比值才可钉）；S0=视差×UniDepth Z 配准尺度中位（种子）；
W_REF=近带半宽（wall/kerb/curve_cont 三可信层中位 3.7m；curve_cont2 层左缘出画、
obstacle 层被路障夹紧，均为低估，不入选）。

**降级路径**：权重缺失/推理异常 → observe 返回 None，road_offset 链路退回
纯模型积分（旧行为不变）；平面拟合失败/行数不足 → 该侧弃权（None，rejects 留因）。

**上拍形态**：生产经 AsyncDepthRoadObserver（异步 worker，协议同 treasure OCR）
使用本层——控制拍只付 push+take（≈0.15ms），observe 成本在后台线程；
本模块的同步 observe 保留为纯函数面（测试与离线复算直接调用）。

**读数带**：逐行扫描只取行均深 Z∈[4, 8)（近带，越栏视线判据见常数注；标定与读数同带）。
参考实现（55 帧、逐行中位口径）帧内一致性 MAD 0.45m；绝对量对标金标线受金标
自身偏置限制（墙基恒定偏置、弯道帧失真、跨帧池化——三戒口径），验收看
「每帧去偏 MAD + 半宽一致性 + 连续帧抖动」。

**时延口径**：本层时延数字一律为 load_session 的 free-dim 折叠口径（@336 推理
p50 ≈20ms）；平面拟合与逐行扫描为向量化 numpy，实测见实验区时延探针。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np
import onnxruntime as ort

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush.world_model import Calib, x_lane_of

# ── 检测带（与既有捕获几何 1280×720 绑定；推理必须吃全帧，天空是尺度锚）────
Y0, DIAG_Y1 = 340, 715
DEFAULT_SHORT = 336   # DA 推理短边（折叠口径 q4f16 p50 ≈22ms；异步形态下可选档）

# ── 相机内参（近似针孔，出处见模块头注；fx=FY·FX_FY）────────────────────
FY = 851.4        # 纵向焦距中位
FX_FY = 1.006     # fx/fy 比值中位（绝对 fx 逐帧漂 18% 不可钉，比值仅 4%）
CX, CY = 643.2, 360.5   # 主点中位（主点误差被平面系数吸收，只需近似值）
_FX = FY * FX_FY

# ── 尺度自标定（出处见模块头注）─────────────────────────────────────────
S0 = 24.06        # 种子尺度：视差→米的配准中位（只为度量门限提供初始米制）
W_REF = 3.70      # 近带路半宽常数（逐帧标定锚，米）
HW_LO, HW_HI = 1.5, 8.0   # 实测半宽物理合理域（出界=测量不可信，保种子）
HW_PCT = 0.25             # 标定宽度取低分位：近带行的宽度（远行斜穿/雾胀是单侧离群）
S_RATIO = (0.6, 1.6)      # 标定后尺度对种子的钳位比（DA 逐帧漂 ±14%，钳位防坏测量撬飞米制系）
CAL_SELF_TOL = 0.15       # 尺度协变自检容差：标定系里重测半宽须回到 W_REF±15%
                          # （一步公式的前提是 hw∝s；超容差=行集/地面分类随尺度漂，米制不可信）

# ── 平面拟合（Y = aX + bZ + c；阈值=参考实现口径，55 帧同判据验证）────────
PLANE_ITERS = 3       # 近带种子迭代轮数
PLANE_TOL = 0.02      # 地面带斜坡项 |res| < TOL·Z + OFF
PLANE_OFF = 0.30
ABOVE_M = 0.15        # 上凸体门：res < −ABOVE_M（高于平面 15cm）
SEED_Z_LO, SEED_Z_HI, SEED_X_MAX = 3.0, 10.0, 5.0   # 近带种子（窄而准）
FIT_Z_LO, FIT_Z_HI, FIT_X_MAX = 2.0, 45.0, 12.0     # 迭代域（雾带/远景出域）
MIN_PLANE_PTS = 80

# ── 逐行提边与读数 ──────────────────────────────────────────────────────
Z_LO, Z_HI = 4.0, 14.0    # 读数/标定带（行均深；宽路近行缘出画，须远行补位）
MIN_ROW_GROUND = 15       # 行内地面像素下限
MIN_ROWS = 5              # 侧读数最少行数
MIN_SPAN_M = 1.0          # 行内路宽下限（米）
GAP_M = 1.5               # 路障夹紧 GAP（米，路中心起算）+ 车框遮挡判据
SEG_GAP_M = 0.6           # 路障分段间隙（米）
LANE_SIDE_MIN = 0.15      # 侧别门：车道量须在自己一侧（带符号）
EDGE_IN_PCT = 20          # 聚合分位（向路心侧）：远行能越过矮护栏/路肩看到路外
EGO_SEED_DILATE = 2       # 自车吸收种子外扩（迭代数，桥接静态掩码边缘小缝隙）
EGO_ABSORB_Z = 10.0       # 自车吸收深度上限（自车 Z≈4~9；远场路墙投影会落进
#                           静态矩形区，无此限会把整段墙链进吸收、拆掉夹紧材料）
CLAMP_TOUCH_PX = 4        # 夹紧段贴挖除洞的判距（px）——自车伪缘保险丝
                          # 地表，把缘读宽（单侧危险方向，实机 173721 局近行
                          # R+0.81=真护栏 vs 远行+1.94=栏外地面）——真缘簇在
                          # 向路心侧，取分位而非中位；夹紧缘（路障）同样在窄侧。


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
    """→ 原帧尺寸（720×1280）的 float32 视差图（大=近）。全帧输入，见上方归一化注。

    DML 互斥（core.dml_lock）：感知会话与深度会话并发 run 会段错误杀进程
    （2026-09-25 实机 + 双线程复现），故 run 本体抢锁、抢不到抛 Busy 让调用方
    跳帧——本层不等待，控制拍的感知优先。"""
    blob = {"pixel_values": preprocess(rgb, short)}
    if not dml_lock.LOCK.acquire(blocking=False):
        raise dml_lock.Busy("DML 被感知推理占用，本帧放弃（latest-only 下一帧再来）")
    try:
        d = sess.run(None, blob)[0][0]
    finally:
        dml_lock.LOCK.release()
    d = np.nan_to_num(d.astype(np.float32), nan=0.0)
    return cv2.resize(d, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def _band_grids() -> tuple[np.ndarray, np.ndarray]:
    """检测带内 (u, v) 网格（列、行号），点云反投影共用。"""
    vv, uu = np.mgrid[Y0:DIAG_Y1, 0:1280]
    return uu.astype(np.float32), vv.astype(np.float32)


_UU, _VV = _band_grids()
_K3 = np.ones((3, 3), np.uint8)


def _cloud_band(m: np.ndarray, s: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """视差带 → (X, Y, Z) 点云带（Z=s/d，X/Y 沿针孔反投影；d≈0 出巨 Z 被后续域滤掉）。"""
    d = np.nan_to_num(np.asarray(m, np.float32), nan=0.0,
                      posinf=0.0, neginf=0.0)[Y0:DIAG_Y1]
    Z = s / np.maximum(d, 1e-3)
    X = (_UU - CX) * Z / _FX
    Y = (_VV - CY) * Z / FY
    return X.astype(np.float32), Y.astype(np.float32), Z.astype(np.float32)


def _plane_fit_band(X: np.ndarray, Y: np.ndarray, Z: np.ndarray, dig: np.ndarray,
                    ) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    """倾角感知平面拟合 Y=aX+bZ+c（近带种子迭代）→ (coef, 地面掩码, 上凸体掩码)。

    种子=近带窄域（Z 3~10、|X|<5：路面最可信处），每轮以 |res|<TOL·Z+OFF 重选
    地面、法方程闭式解（N 可达数十万，SVD 不必要）；任何一轮地面点不足判失败
    （None——近带无路面，诚实弃权）。上凸体=平面上方 15cm 内的域外点（墙/车/路障）。"""
    ok = np.isfinite(Z) & (Z > 0)
    absX = np.abs(X)                       # 逐轮不变量，外提（abs 确定性，逐位同）
    notdig = ~dig
    sel = ok & (Z > SEED_Z_LO) & (Z < SEED_Z_HI) & (absX < SEED_X_MAX) & notdig
    fitdom = ok & (Z > FIT_Z_LO) & (Z < FIT_Z_HI) & (absX < FIT_X_MAX) & notdig
    lo = -PLANE_TOL * Z - PLANE_OFF        # 地面带阈值逐轮不变，外提
    hi = PLANE_TOL * Z + PLANE_OFF
    t1 = np.empty_like(Z)
    t2 = np.empty_like(Z)
    res = np.empty_like(Z)
    coef = None
    for _ in range(PLANE_ITERS):
        n = int(sel.sum())
        if n < MIN_PLANE_PTS:
            return None, None, None
        idx = np.flatnonzero(sel)                 # 索引一次，三列共享
        A = np.empty((n, 3), np.float32)          # 法方程经 BLAS（N 数十万，SVD 不必要）
        A[:, 0] = X.take(idx)
        A[:, 1] = Z.take(idx)
        A[:, 2] = 1.0
        y = Y.take(idx)
        mmat = (A.T @ A).astype(np.float64)
        rhs = (A.T @ y).astype(np.float64)
        try:
            coef = np.linalg.solve(mmat, rhs)
        except np.linalg.LinAlgError:
            coef = np.linalg.lstsq(A.astype(np.float64), y.astype(np.float64),
                                   rcond=None)[0]
        np.multiply(Z, coef[1], out=t1)
        np.multiply(X, coef[0], out=t2)
        np.add(t1, t2, out=t1)
        np.add(t1, coef[2], out=t1)
        np.subtract(Y, t1, out=res)
        sel = fitdom & (res > lo) & (res < hi)
    ground = sel & (res > -ABOVE_M)
    # 最终地面标记收紧：拟合带对称（鲁棒），标记带与上凸体门互补——墙体下沿
    # （DA 模糊把墙基拖进 0.3m 容差带）必须归"上凸体"，否则近行地面延伸到画框
    # 缘、整段行被出画判据误杀，夹紧也没有材料（金标 000180 实证）。
    above = fitdom & (res < -ABOVE_M)
    return coef, ground, above


def _grow_ego_above(above: np.ndarray, ego_band: np.ndarray,
                    Z: np.ndarray) -> np.ndarray | None:
    """动态自车吸收：与静态自车掩码连通的"上凸体"像素 → 并入挖除区。

    静态矩形掩码（ego_mask.json）盖不全车形（尾翼/车顶附件出矩形）也跟不住
    镜头位移（飞车镜头会动）；但车在点云里必然是"贴着静态掩码的高出地面
    连通域"——连通域标记一次吸收整车，无需建模车形、无需逐皮肤标定。候选限
    Z<EGO_ABSORB_Z（自车近场）：远场路墙投影会落进矩形区，不限深度会把整段
    墙链进吸收、拆掉真路障的夹紧材料。远处真障碍物不与自车连通（画面上有
    路面间隔），不进吸收。"""
    if not ego_band.any() or not above.any():
        return None
    cand = above & (Z < EGO_ABSORB_Z)
    if not cand.any():
        return None
    seed = cv2.dilate(ego_band.astype(np.uint8), _K3,
                      iterations=EGO_SEED_DILATE).astype(bool)
    lab = cv2.connectedComponents((cand | seed).astype(np.uint8),
                                  connectivity=8)[1]
    keep = np.unique(lab[seed])
    keep = keep[keep > 0]
    if keep.size == 0:
        return None
    grown = np.isin(lab, keep) & cand
    return grown if grown.any() else None


class _RowEdge(NamedTuple):
    """一行的提边结果（米制位置 + 内沿像素 + 遮挡旗 + 行地面中位深）。"""

    v: int
    xl: float
    xr: float
    u_l: float
    u_r: float
    occ_l: bool
    occ_r: bool
    zmed: float


def _row_scan(X: np.ndarray, Z: np.ndarray, ground: np.ndarray, above: np.ndarray,
              dig: np.ndarray, obj: np.ndarray | None, coef: np.ndarray,
              step: int = 1) -> list[_RowEdge]:
    """近带逐行提边：地面 min/max X 为候选缘，同行上凸体整段越 GAP 才夹紧内沿。

    遮挡旗只由 ``obj``（YOLO 物体掩码）触发：行内物体像素 |X|>GAP=该侧被
    压缘遮挡，聚合时弃权。``dig``（ego∪object）只负责从点云挖除——ego 静态
    掩码在远行的 |X| 本就 >GAP（自车身体在 11m 处横向 ±1.9m），拿它判遮挡
    会把所有远行误杀（金标 000906 实证）。

    内沿像素 u 一律经**拟合平面反投影**（u = X·FX/Z_edge + CX，Z_edge 由平面式
    给出）：行内 X↔u 的单调性只对地面点成立——墙基滑带（0.3m 容差带内的墙面
    像素）X 恒等于墙平面 X 而 u 任意，逐像素关联会产出噪声 u；夹紧缘是真实
    上凸体像素，直接用其像素 u。``step``：行采样步长（标定遍减半省时）。"""
    a_c, b_c, c_c = (float(coef[0]), float(coef[1]), float(coef[2]))
    out: list[_RowEdge] = []
    nrows, ncols = Z.shape
    cntg = ground.sum(1)
    fin2d = np.isfinite(X)
    # 行内地面值逐行排序（Z/X 各一次，-inf 垫尾 → 地面段落位行尾 [ncols-cntg, ncols)），
    # 中位/最小/最大全部转为按行 gather——等价于原逐行 median/min/max（偶数中位
    # 仍按 float32 的 (a+b)/2 语义复刻），省掉每行一次的 numpy 调用开销。
    zs = np.sort(np.where(ground, Z, -np.inf), axis=1)
    xs = np.sort(np.where(ground, X, -np.inf), axis=1)
    rbs = np.arange(0, nrows, step)
    cgv = cntg[rbs]
    rbv = rbs[cgv >= MIN_ROW_GROUND]
    cgv = cntg[rbv]
    s0v = ncols - cgv
    # 行带判据=行内**地面**像素的 Z 中位（全行均值会被墙面像素拉进近带——
    # 墙占多数的远行看似"近行"，读数带被远景污染）
    zmed = ((zs[rbv, s0v + (cgv - 1) // 2] + zs[rbv, s0v + cgv // 2])
            / np.float32(2.0))
    xl = xs[rbv, s0v].astype(np.float64)
    xr = xs[rbv, ncols - 1].astype(np.float64)
    med = ((xs[rbv, s0v + (cgv - 1) // 2] + xs[rbv, s0v + cgv // 2])
           / np.float32(2.0))
    den = (rbv + Y0 - CY) / FY - b_c
    base_ok = (zmed >= Z_LO) & (zmed < Z_HI) & (den >= 1e-4)
    # den<1e-4 的行在原实现里先 continue 再除；向量化后用钳位值兜住除法
    # （该行必被 base_ok 过滤，钳位值不参与任何读数）。
    z_edge = (c_c + a_c * xl) / np.maximum(den, 1e-4)
    z_edger = (c_c + a_c * xr) / np.maximum(den, 1e-4)
    u_l = xl * _FX / z_edge + CX
    u_r = xr * _FX / z_edger + CX
    if obj is not None:
        occ_l = (obj & fin2d & (X < -GAP_M)).any(axis=1)
        occ_r = (obj & fin2d & (X > GAP_M)).any(axis=1)
    else:
        occ_l = occ_r = np.zeros(nrows, bool)
    aa_any = (above & fin2d).any(axis=1)
    for k, rb in enumerate(rbv):
        if not base_ok[k]:
            continue
        if not aa_any[rb]:
            continue            # 无上凸体行：无夹紧材料，基础量即终值
        aa = above[rb] & fin2d[rb]
        Xa = X[rb][aa]
        Ua = _UU[rb][aa]
        order = np.argsort(Xa)
        Xas, Uas = Xa[order], Ua[order]
        start = 0
        segs = []
        for i in range(1, Xas.size):
            if Xas[i] - Xas[i - 1] > SEG_GAP_M:
                segs.append((start, i - 1))
                start = i
        segs.append((start, Xas.size - 1))
        for s0i, s1i in segs:
            # 保险丝：夹紧段贴着挖除洞（±CLAMP_TOUCH_PX）= 洞边缘泄漏的
            # 自车/已检物像素（吸收偶尔漏边），该侧置遮挡弃权，绝不把
            # 洞边当边界读出（防"自车当缘"残留）。
            def _touches_hole(us: np.ndarray) -> bool:
                lo = max(int(us.min()) - CLAMP_TOUCH_PX, 0)
                hi = min(int(us.max()) + CLAMP_TOUCH_PX + 1, X.shape[1])
                return bool(dig[rb, lo:hi].any())
            if Xas[s0i] > med[k] + GAP_M:       # 整段在右侧 GAP 外 → 夹右缘
                if _touches_hole(Uas[s0i:s1i + 1]):
                    occ_r[rb] = True
                    continue
                nx = float(Xas[s0i])
                if nx < xr[k] - 0.05:           # 真路障（显著内收）→ 取其像素 u
                    xr[k], u_r[k] = nx, float(Uas[s0i])
                elif nx < xr[k]:                # 噪声级收紧（缘共面墙段）→ 保平面 u
                    xr[k] = nx
            elif Xas[s1i] < med[k] - GAP_M:     # 整段在左侧 GAP 外 → 夹左缘
                if _touches_hole(Uas[s0i:s1i + 1]):
                    occ_l[rb] = True
                    continue
                nx = float(Xas[s1i])
                if nx > xl[k] + 0.05:
                    xl[k], u_l[k] = nx, float(Uas[s1i])
                elif nx > xl[k]:
                    xl[k] = nx
    drop = (u_l <= 2.0) | (u_r >= 1277.0)       # 缘出画：读数是画框不是边界
    span_ok = (xr - xl) > MIN_SPAN_M            # （贴边=单侧 C 类，该行弃权）
    for k, rb in enumerate(rbv):
        if not base_ok[k] or drop[k] or not span_ok[k]:
            continue
        out.append(_RowEdge(int(rb + Y0), float(xl[k]), float(xr[k]),
                            float(u_l[k]), float(u_r[k]),
                            bool(occ_l[rb]), bool(occ_r[rb]), float(zmed[k])))
    return out


def _side_from_rows(rows: list[_RowEdge], side: str,
                    cal: Calib) -> tuple[float | None, float | None, str | None]:
    """侧读数：该侧未遮挡行**逐行**取内沿车道量（x_lane_of(u, v)）后聚合。

    **近带优先双票**：远行视线可越过矮结构读到路外（越栏视线，只会读宽——
    单向危险误差），近带行才是可信票；但按行数取固定分位会在近行占比低于
    分位数时滑进远行（1933 局实证：近行 13% 的帧 20 分位摇摆于近/远之间，
    读数帧间跳变）。而纯近带子集又会丢掉远行才看得到的障碍夹紧约束
    （近行地面还在障碍之前）。修法=**双票取更绑**：全行集分位票之外，再取
    zmed 最小半数行（≥MIN_ROWS 才成立）的子集分位票，每侧取更靠路心
    （更窄=更约束）的——远行的单向读宽保证近带票不被幽灵污染，全行集票
    保住夹紧约束，近行占比不足时子集≈全行集自动退化。

    为什么不投影到固定参考行：投影沿消失点线把远行的边缘不确定性放大
    (VREF−vp_y)/(v−vp_y) 倍（远行视差噪声本就大，放大 2~5 倍后中位被垃圾行
    撬动，金标 000500 实证 R 偏 +2 车道）；逐行评估的误差不放大、一行一票。
    代价是缘线上 x_lane 随行系统漂 ~±0.1 车道（y_h/vpx 标定系与针孔地平线
    差 ~15px 所致）——行分布被遮挡推移时读数随之小幅移动，实机验收口径内
    消化。"""
    usable = [r for r in rows
              if not (r.occ_l if side == "L" else r.occ_r)]

    def _pct(rs: list[_RowEdge]) -> float:
        ls = [x_lane_of(int(round(r.u_l if side == "L" else r.u_r)), r.v, cal)
              for r in rs]
        return float(np.percentile(ls, 100 - EDGE_IN_PCT if side == "L"
                                   else EDGE_IN_PCT))

    if len(usable) < MIN_ROWS:
        return None, None, f"{side}:行不足"
    lane = _pct(usable)
    if len(usable) >= 2 * MIN_ROWS:              # 近带子集票（双票取更绑）
        near = sorted(usable, key=lambda r: r.zmed)[: len(usable) // 2]
        if len(near) >= MIN_ROWS:
            lane_near = _pct(near)
            lane = max(lane, lane_near) if side == "L" else min(lane, lane_near)
    us = [r.u_l if side == "L" else r.u_r for r in usable]
    # 侧别门=带符号（L 须在自己一侧为负、R 为正）——只查幅度会放进对侧
    # "缘"（实机 173721 局 L=+0.48 的被超车夹紧读数混进配对，2026-09-27）。
    ok = lane <= -LANE_SIDE_MIN if side == "L" else lane >= LANE_SIDE_MIN
    if not ok:
        return None, None, f"{side}:lane{lane:+.2f}"
    return lane, float(np.median(us)), None


def reading_from_map(m: np.ndarray, cal: Calib, ego_mask: np.ndarray | None = None,
                     object_mask: np.ndarray | None = None) -> DepthRoadReading:
    """视差图 → 读数（纯函数，回归锁可直接喂缓存图；observe=推理+本函数）。

    ``ego_mask``（ego 静态掩码）与 ``object_mask``（YOLO 物体掩码）都从点云
    挖除（断车身→路面粘连）；侧遮挡判据只看 object_mask（语义=检测到的他车
    压缘，见 _row_scan 注）。"""
    t0 = time.perf_counter()

    def _ret(lane_l, lane_r, u_l, u_r, rejects):
        return DepthRoadReading(
            left_edge_lane=lane_l, right_edge_lane=lane_r,
            left_x=u_l, right_x=u_r,
            sides=int(lane_l is not None) + int(lane_r is not None),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            rejects=tuple(rejects))

    dig = _dig_band(ego_mask, m.shape[1])
    if object_mask is not None:
        dig = dig | _dig_band(object_mask, m.shape[1])
    obj = None if object_mask is None else _dig_band(object_mask, m.shape[1])
    rejects: list[str] = []

    X, Y, Z = _cloud_band(m, S0)
    coef, ground, above = _plane_fit_band(X, Y, Z, dig)
    if coef is None:
        return _ret(None, None, None, None, ["平面拟合失败"])
    # 动态自车吸收：静态矩形盖不全的车身（尾翼等）在点云里是"贴着掩码的
    # 上凸体"，连通域一次并入挖除区后重拟合——不吸收时这些像素会被夹紧
    # 判据当成边界（实机 181518 局右缘读成自车侧面 +0.35 道，金标目检实锤）。
    ego_band = _dig_band(ego_mask, m.shape[1])
    grown = _grow_ego_above(above, ego_band, Z) if ego_band.any() else None
    if grown is not None:
        dig = dig | grown
        coef, ground, above = _plane_fit_band(X, Y, Z, dig)
        if coef is None:
            return _ret(None, None, None, None, ["自车吸收后平面失败"])
    rows = _row_scan(X, Z, ground, above, dig, obj, coef, step=2)

    # 逐帧尺度自标定：种子系量近带半宽（低分位抗远行斜穿/雾胀）→ s 修正到 W_REF，
    # 第二遍在标定系重跑。
    #
    # **本步只走一步，不许改成迭代**：X∝Z∝s ⇒ hw(s)∝s，故 s' = S0·W_REF/hw(s)
    # = C/s 是**对合映射**（自己是自己的逆），迭代必然 2-周期振荡——一步公式本身
    # 就是不动点。
    # 反之，失效全在**静默兜底**：坏测量被悄悄吃成"种子值"（24.06）或"钳位值"，
    # 下游分不清"标定成功"与"没标定"（两者可差近 2×）。故每一类失效都写进
    # rejects——读数不自证清白，账目在 rejects。
    hw = [(r.xr - r.xl) / 2.0 for r in rows
          if not (r.occ_l or r.occ_r) and r.zmed < Z_HI]
    s = S0
    if len(hw) < 3:
        rejects.append(f"s:行不足({len(hw)})")
    else:
        hwm = float(np.percentile(hw, HW_PCT * 100))
        if not (HW_LO <= hwm <= HW_HI):
            rejects.append(f"s:半宽出域({hwm:.1f})")
        else:
            s_raw = S0 * W_REF / hwm
            s = float(np.clip(s_raw, S0 * S_RATIO[0], S0 * S_RATIO[1]))
            if abs(s - s_raw) > 1e-6:
                rejects.append(f"s:钳位({s_raw:.1f})")
    if abs(s / S0 - 1.0) > 0.02:
        X, Y, Z = _cloud_band(m, s)
        coef, ground, above = _plane_fit_band(X, Y, Z, dig)
        if coef is None:
            rejects.append("重标定后平面失败")
            coef = None
        else:
            rows = _row_scan(X, Z, ground, above, dig, obj, coef)
            # 尺度协变自检：一步公式的前提是 hw∝s，故标定系里同一批行的半宽必须
            # 回到 W_REF。超容差 = 行集/地面分类随尺度漂（振荡的实因），本帧米制
            # 不可信——照实记账，不再冒充"已标定"。
            hw2 = [(r.xr - r.xl) / 2.0 for r in rows
                   if not (r.occ_l or r.occ_r) and r.zmed < Z_HI]
            if len(hw2) < 3:
                rejects.append(f"s:自检行不足({len(hw2)})")
            else:
                hwm2 = float(np.percentile(hw2, HW_PCT * 100))
                if abs(hwm2 - W_REF) > CAL_SELF_TOL * W_REF:
                    rejects.append(f"s:自检不过({hwm2:.1f})")

    lane_l, u_l, rej_l = _side_from_rows(rows, "L", cal)
    lane_r, u_r, rej_r = _side_from_rows(rows, "R", cal)
    if rej_l:
        rejects.append(rej_l)
    if rej_r:
        rejects.append(rej_r)
    return _ret(lane_l, lane_r, u_l, u_r, rejects)


class DepthRoadObserver:
    """深度几何观测器（阶段生命期=chain；ORT session 跨阶段复用、外部注入）。

    session 为 None（权重缺失/加载失败）时 observe 恒 None——road_offset 退回
    纯模型积分，与"无边界"路径行为一致。"""

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

    def observe(self, frame_rgb: np.ndarray,
                object_mask: np.ndarray | None = None) -> DepthRoadReading | None:
        """一帧 → 读数（两侧可各自弃权）；推理失败 → None（不抛，控制链不因感知停摆）。

        ``object_mask``：YOLO 检测框（车/金币/奖励，外扩后）布尔掩码，与 ego
        掩码同通道挖除——物体从点云源头消失，"穿车读墙"假缘不再产生；
        掩码像素的米制横向同时充当逐行遮挡判据（|X|>GAP 该侧行弃权）。"""
        reading, _ = self.observe_debug(frame_rgb, object_mask)
        return reading

    def observe_debug(self, frame_rgb: np.ndarray, object_mask: np.ndarray | None = None
                      ) -> tuple[DepthRoadReading | None, np.ndarray | None]:
        """同 observe，另返回原始视差图（实机 debug 渲染用；与 reading 同源同拍）。"""
        if self._sess is None:
            return None, None
        t0 = time.perf_counter()
        try:
            m = infer_map(self._sess, frame_rgb, self._short)
        except dml_lock.Busy:
            raise    # 让锁跳帧是协议行为（DML 互斥），不算推理失败——worker 侧单独计数
        except Exception:
            return None, None
        reading = reading_from_map(m, self._cal, ego_mask=self._ego_mask,
                                   object_mask=object_mask)
        return replace(reading,
                       latency_ms=(time.perf_counter() - t0) * 1000.0), m

    @property
    def session_ready(self) -> bool:
        return self._sess is not None


def render_depth_debug(frame_rgb: np.ndarray, m: np.ndarray,
                       reading: DepthRoadReading, ego_mask: np.ndarray | None,
                       object_mask: np.ndarray | None = None,
                       note: dict | None = None) -> np.ndarray:
    """实机可视化判据（三行堆叠，BGR）：程序看了什么、算了什么、判了什么。

    ① 画面帧：ego 掩码橙描边 / YOLO 物体掩码蓝描边 / 读数黄竖线 + L/R 车道量；
    ② 视差图（带内对数归一，与探针同一色标）；
    ③ 平面残差图 + 地面（绿）/ 上凸体（青）+ 读数内沿（黄线）+ 弃权原因。
    纯函数只渲染不落盘——落盘归异步 worker 节流。"""
    h, w = m.shape[:2]
    f = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    for mask, col in ((ego_mask, (255, 128, 0)), (object_mask, (255, 0, 0))):
        if mask is None:
            continue
        cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(f, cnts, -1, col, 2)
    for x in (reading.left_x, reading.right_x):
        if x is not None and x == x:
            cv2.line(f, (int(x), 0), (int(x), h - 1), (0, 255, 255), 2)
    lt = "-" if reading.left_edge_lane is None else f"{reading.left_edge_lane:+.2f}"
    rt = "-" if reading.right_edge_lane is None else f"{reading.right_edge_lane:+.2f}"
    cv2.putText(f, f"L {lt}  R {rt}  sides={reading.sides}", (8, 26),
                cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 2)

    logs = np.log(np.clip(m[Y0:DIAG_Y1].ravel(), 1e-3, None))
    lo, hi = np.percentile(logs, [2, 98])
    disp = cv2.applyColorMap(
        (np.clip((np.log(np.clip(m, 1e-3, None)) - lo) / max(hi - lo, 1e-6), 0, 1)
         * 255).astype(np.uint8), cv2.COLORMAP_JET)

    dig = ego_mask
    if object_mask is not None:
        dig = object_mask if dig is None else (dig | object_mask)
    X, Y, Z = _cloud_band(m, S0)
    coef, ground, above = _plane_fit_band(X, Y, Z, _dig_band(dig, m.shape[1]))
    ego_band = _dig_band(ego_mask, m.shape[1])          # 与 reading 同口径：
    if coef is not None and ego_band.any():             # 自车吸收进残差图
        grown = _grow_ego_above(above, ego_band, Z)
        if grown is not None:
            coef, ground, above = _plane_fit_band(
                X, Y, Z, _dig_band(dig, m.shape[1]) | grown)
    hm = np.zeros((DIAG_Y1 - Y0, w, 3), np.uint8)
    if coef is not None:
        res = Y - (coef[0] * X + coef[1] * Z + coef[2])
        ok = np.isfinite(res)
        v = np.clip((res[ok] + 0.45) / 0.90, 0, 1)     # ±0.45m 视窗
        hm[ok] = np.stack([np.interp(v, [0, .5, 1], [255, 255, 0]),
                           np.interp(v, [0, .5, 1], [0, 255, 0]),
                           np.interp(v, [0, .5, 1], [0, 255, 255])], axis=-1)
        hm[ground] = (0, 200, 0)
        hm[above] = (255, 255, 0)
    for x in (reading.left_x, reading.right_x):
        if x is not None and x == x:
            cv2.line(hm, (int(x), 0), (int(x), hm.shape[0] - 1), (0, 255, 255), 2)
    rej = ";".join(reading.rejects)[:110]
    if rej:
        cv2.putText(hm, rej, (8, hm.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX,
                    .55, (255, 255, 255), 1)
    body = np.vstack([f, disp[Y0:DIAG_Y1], hm])
    if note is None:
        return body
    # 决策带：消费端一拍快照（state/reason/杆/喂入路心/来源/EMA/槽/帧龄）。
    # 感知三联只回答"眼睛看到什么"，撞墙复盘还要"大脑当时拿了什么、怎么动"——
    # 人工逐帧核对的账本（快照为 push 前一拍，与图帧差一拍 ≈50ms）。
    band = np.full((56, w, 3), 30, np.uint8)
    st = note.get("state", "?"); rs = note.get("reason", "?")
    sn = note.get("steer"); ro = note.get("ro"); el = note.get("elane")
    l1 = (f"D[{note.get('fid', '?')}] {st} {rs}  steer={sn:+.3f}"
          if isinstance(sn, (int, float)) else f"D[{note.get('fid', '?')}] {st} {rs}")
    l1 += f"  elane={el:+.2f}" if isinstance(el, (int, float)) else ""
    def _f(v):
        return f"{v:+.2f}" if isinstance(v, (int, float)) else "-"
    l2 = (f"ro={_f(ro)}[{note.get('src', '-')}]"
          f" age={note.get('age', '-')}ms new={note.get('new', '-')}")
    cv2.putText(band, l1, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 255, 255), 1)
    cv2.putText(band, l2, (8, 46), cv2.FONT_HERSHEY_SIMPLEX, .55, (200, 255, 200), 1)
    return np.vstack([body, band])


def _dig_band(dig: np.ndarray | None, width: int) -> np.ndarray:
    """全帧掩码 → 检测带切片（None 给全假）。"""
    if dig is None:
        return np.zeros((DIAG_Y1 - Y0, width), bool)
    return np.asarray(dig, bool)[Y0:DIAG_Y1]


class AsyncDepthRoadObserver:
    """深度几何异步观测器（2026-09-24 解耦落地，协议与 treasure OCR worker 同构）。

    为什么解耦这一层：observe 成本（推理 ≈20ms 折叠口径 + 点云/平面/逐行后处理）
    同步形态把控制拍预算吃光；路缘是**慢变量**——滞后 1~2 拍（50~100ms）读数仍
    有效；金币/街车是快变目标、必须与本拍像素同源，所以感知与黄线层不做异步
    （treasure「令牌与读数字节同源」的教训：跨线程搬运必有窗口，快变对象上
    窗口=脏读）。

    协议（latest-only，无队列不积压）：
    - push：主线程拷帧覆盖 pending 槽 + wakeup；worker 慢则丢中间帧（丢的是输入，
      不是结果——路缘下一拍还会再来）。captured_ts 用 perf_counter（与耗时/时效
      同族时钟；monotonic 在本机粒度 ~16ms 会把 age 量化）。
    - worker：节流窗（INFER_MIN_INTERVAL_S，压 DML 锁占空比）→ pop latest →
      同步 observe（run 本体受 core.dml_lock 互斥：与控制拍感知并发 run 会段错误
      杀进程，2026-09-25 实证；抢不到锁抛 Busy → 计 busy_skips 弃本帧，不排队）
      → **非 None 才发布**结果槽（整包替换，不原地改已发布对象）；
      顶层 try/except 计 failures，单帧异常不杀 daemon。
    - take：主线程消费结果槽（**驻留**：闸内同一结果可被多拍反复消费，取走不清；
      is_new=本拍是否首次消费该结果——消费端保鲜槽与学习只认新证据）；
      age = now − captured_ts 超 max_age_ms → 计 stale、清槽、本拍返回 None
      （road_offset 退纯模型积分——与 session 缺失同一降级路径，宁旧不如无）。
    - health：applied/stale/failures/busy_skips 计数 + age/duration 滑窗——
      不达标报警数据面。
    """

    MAX_AGE_MS = 150.0    # 结果时效预算=复用上限：闸内读数才喂控制（1830 局实证：
                          # 300ms 复用窗让新值与旧值交替成方波踢 planner，翻转率
                          # 反升）。150ms 内路心漂移在机动横移下 ≤~0.25 道；超龄
                          # 清槽退纯模型积分，路缘慢变量、保鲜槽 TTL 0.4s 同量级
    INFER_MIN_INTERVAL_S = 0.07   # worker 出工下间隔：锁内只有 run 本体（q4f16 实测
                                  # ~19ms，锁占空比 ~11%），控制拍感知（同锁）的碰撞
                                  # 等待有上界即可（见 dml_lock）；旧值 150ms 按当年
                                  # "推理占大头"的误估留白（2026-09-28 实测修正）。
    PERF_WINDOW = 200     # age/duration 滑窗（与 treasure 同族口径：判据只看尾部）

    DEBUG_INTERVAL_S = 2.0   # 调试图节流（与坏帧取证同量级，封顶磁盘占用）

    def __init__(self, observer: DepthRoadObserver,
                 max_age_ms: float = MAX_AGE_MS,
                 debug_dir: Path | None = None,
                 infer_min_interval_s: float = INFER_MIN_INTERVAL_S) -> None:
        self._obs = observer
        self._max_age_ms = float(max_age_ms)
        self._infer_interval_s = float(infer_min_interval_s)
        self._debug_dir = Path(debug_dir) if debug_dir is not None else None
        self._debug_last = float("-inf")   # 「从未出工」：0 会在进程早期(monotonic<窗)误判窗内
        self._debug_seq = 0
        self._lock = threading.Lock()
        self._pending: tuple[int, np.ndarray, float, np.ndarray | None] | None = None
        self._result: tuple[int, DepthRoadReading, float] | None = None
        self._last_seq = -1               # 消费端已见结果序号（仅主线程触碰）
        self._pushed = 0
        self._applied = 0
        self._stale_drops = 0
        self._failures = 0
        self._busy_skips = 0
        self._last_infer = float("-inf")   # 同上：进程早期不得被节流窗拦下首拍
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

    def push(self, frame_rgb: np.ndarray,
             object_mask: np.ndarray | None = None,
             note: dict | None = None) -> None:
        """object_mask：本帧 YOLO 检测框掩码（与帧同源同拍，见 observe 注）。
        note：消费端上一拍决策快照（只读小 dict，主线程每拍新建）——随帧进
        调试图决策带，供人工核对。无 worker（session 缺失/start 未调）时直接
        空转：不拷帧、不计数——阶段出口的"零结果"报警以 pushed>0 为前提，
        计数了就会误报。"""
        if self._thread is None:
            return
        with self._lock:
            self._pushed += 1
            self._pending = (self._pushed, frame_rgb.copy(), time.perf_counter(),
                             None if object_mask is None else object_mask.copy(),
                             note)
        self._wakeup.set()

    def take(self) -> tuple[DepthRoadReading | None, bool]:
        """读最新驻留结果 → (reading | None, is_new)。

        驻留语义（2026-09-28，"每结果只喂一拍"是闭眼主因之一）：结果不再
        取走即清——同一结果在 age 闸内可被多个控制拍反复消费（路缘是慢
        变量，闸内旧读数仍可用），无新结果的拍不再退纯模型积分。is_new
        标记本拍是否首次消费该结果：路心合成可复用旧结果，但消费端的
        保鲜槽与半宽学习只认新证据（**使用次数 ≠ 学习次数**——合成不是
        新证据的同一条纪律）。超龄返回 (None, False)、清槽并计 stale
        （每结果至多一次）。"""
        with self._lock:
            item = self._result
            if item is None:
                return None, False
            reading, captured_ts = item[1], item[2]
        age_ms = (time.perf_counter() - captured_ts) * 1000.0
        self.last_age_ms = age_ms
        is_new = item[0] != self._last_seq
        if is_new:
            self._last_seq = item[0]
            self._age_win.append(age_ms)
            self._dur_win.append(reading.latency_ms)
        if age_ms > self._max_age_ms:
            with self._lock:   # seq 比对防误删 worker 刚发布的新结果
                if self._result is not None and self._result[0] == item[0]:
                    self._result = None
                self._stale_drops += 1
            return None, False
        if is_new:
            with self._lock:
                self._applied += 1
        return reading, is_new

    def health(self) -> dict:
        with self._lock:
            pushed, applied, stale, failures, busy = (
                self._pushed, self._applied, self._stale_drops, self._failures,
                self._busy_skips)
        ages, durs = list(self._age_win), list(self._dur_win)

        def _p(xs: list[float], q: float) -> float:
            return 0.0 if not xs else sorted(xs)[min(len(xs) - 1, int(q * (len(xs) - 1)))]

        return {"pushed": pushed, "applied": applied, "stale_drops": stale,
                "failures": failures, "busy_skips": busy,
                "age_p50": _p(ages, 0.5), "age_p95": _p(ages, 0.95),
                "dur_p50": _p(durs, 0.5), "dur_p95": _p(durs, 0.95),
                "max_age_ms": self._max_age_ms}

    # ---------- worker 线程 ----------

    def _write_debug(self, frame, m, reading, object_mask, note=None) -> None:
        """节流落实机调试图（三联图+决策带）+ 可复现证据包（读数带视差 fp16 +
        生效掩码）。

        只有渲染图时读数故障无法离线复现（色标有损、看不到基线钉住了什么）——
        视差带 + 合并掩码足以离线重放 reading_from_map 全程。失败静默吞掉——
        debug 绝不干扰主路。"""
        now = time.monotonic()
        if now - self._debug_last < self.DEBUG_INTERVAL_S:
            return
        self._debug_last = now
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            self._debug_seq += 1
            stem = self._debug_dir / f"d{self._debug_seq:05d}"
            img = render_depth_debug(frame, m, reading,
                                     self._obs._ego_mask, object_mask, note)
            cv2.imwrite(str(stem) + ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            np.save(str(stem) + "_band.npy",
                    m[Y0:DIAG_Y1].astype(np.float16))
            merged = self._obs._ego_mask
            if object_mask is not None:
                merged = object_mask if merged is None else (merged | object_mask)
            if merged is not None:
                np.save(str(stem) + "_mask.npy", np.packbits(merged))
            if self._obs._ego_mask is not None:
                np.save(str(stem) + "_ego.npy",
                        np.packbits(self._obs._ego_mask))
        except Exception:  # noqa: BLE001 —— 可视化判据失败不碰主路
            pass

    def _pop_latest(self) -> tuple[int, np.ndarray, float, np.ndarray | None] | None:
        with self._lock:
            item, self._pending = self._pending, None
            return item

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 节流窗内不出工也不取帧（取了也只能弃——age 闸会丢）：睡到窗尾，
            # push 唤醒只提前醒来重新看窗。路缘慢变量，产出节奏≈窗+推理+后处理。
            rest = self._infer_interval_s - (time.monotonic() - self._last_infer)
            if rest > 0:
                self._wakeup.wait(timeout=rest)
                self._wakeup.clear()
                continue
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
                reading, m = self._obs.observe_debug(item[1], object_mask=item[3])
            except dml_lock.Busy:
                # DML 被控制拍感知占用：弃本帧不排队（latest-only，下一帧再来），
                # 单独计数——这是让锁的常规代价，不是故障。
                self._busy_skips += 1
                continue
            except Exception:  # noqa: BLE001 —— 单帧异常计数后继续（与 treasure 同姿态）
                self._failures += 1
                continue
            self._last_infer = time.monotonic()
            if reading is None:  # session 中途失效/推理异常：本帧无结果，不发布
                continue
            if self._debug_dir is not None:
                self._write_debug(item[1], m, reading, item[3], item[4])
            with self._lock:
                self._result = (item[0], reading, item[2])  # (push 序号, 读数, captured_ts)
