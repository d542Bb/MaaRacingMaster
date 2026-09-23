"""台阶边界可检性探针：检测器入代码 + 检不出帧归因（C 类，2026-09-23）。

**为什么有这个文件**：预验证（README「台阶可检性预验证」）的 v2 检测器此前只存在于
已删的一次性脚本里——读数 79/141 无代码可复现。本文件把口径固化为代码，并在此之上
做维护者复核要求的归因分析（全部零标注、零推理，读 `{k}__da2s.npy` 参照缓存）。

**要回答的问题**（维护者 2026-09-23 复核）：
  ① 141 帧里 62 帧在任何阈值下都检不出——归因分类（调阈值管辖不到的那部分）；
  ② 阈值 θ 的物理含义（可检最小台阶高度比 h/H？还是与行有关？）；
  ③ 检出物的类别精确率：墙 / 抬升路缘 / 车底缘 各占多少（纯几何判别，零标注）；
  ④ 边界**定位**是否被阈值污染（竖直墙的"阈值穿越点" ≠ 墙脚）；
  ⑤ 覆盖率改口径：射线识别率（左/右 0-1）+ 分行带覆盖；
  ⑥ 帧内列间抖动 ≠ 跨帧抖动（P3 需另测，本探针在唯一真连续段上给读数）。

**检测口径**（同 README，入代码以便复现）：
  检测带 y∈[340,690)，列 XS = 20:1240:2；下窗 W1=8px 要求**全为地面**
  （|M−g| < 0.15·量程）；上窗 W2=8px；量程 = M[340:700] 非自车列的 p95−p5；
  地面签名 g(y) = 逐行中位数（非自车列 0:540 ∪ 740:1280），两轮稳健化
  （偏离 >25% 量程不参与中位）。

**三种检出变体**（记录表 79/70/65/59 无法复现，故三种并列实测；见 sweep）：
  - `abs`：窗对比度 C = median(上窗) − median(下窗)，除以**量程**；
  - `rel`：同上但除以 median(下窗)（相对量，≈ h/H 但含 8px 窗的透视项）；
  - `mask`：**命中区（|D| > θ·量程）的下沿**，要求其下 W1 全干净。
  合成自检给出三者的结构差异（`selftest`）：
  - 竖直墙（墙脚行 450）：abs/rel **四种配置全零检出**——下窗必须全地面 ⇒
    上窗只能取到墙脚上方 8px，偏离量上限 ≈ (7+W2/2)/350 ≈ 3% 量程，**结构上
    看不见竖直墙**（与 W2 有关，W2=8 时必然如此）；mask 能检到，但检出位置
    随 θ 上移：θ=0.06 → 431 行、0.15 → 402、0.20 → 386（真墙脚 450，
    偏置 −19/−48/−64px）⇒ **θ 直接污染墙的边界位置**。
  - 抬升水平面（h/H=0.15，边沿行 480）：abs 零检出（0.046 量程 < θ=0.06）、
    rel 与 mask 都在 480 行检出 ⇒ **抬升面定位无偏，θ 只影响可检性**。
  ⇒ 定位与阈值必须解耦：位置取非地面区下沿（抬升面无偏；墙需形状外推），
    θ 只作验证（维护者 §5 的结论，本探针以合成证据坐实）。

**几何（本探针新增，零标注）**——偏离量 D = M − g 的**形状**把两类边界分开：
  - 抬升水平面（路缘/护栏基座）：Z_raised = Z_road·(1 − h/H) ⇒ M/g = 1 + h/H
    **逐行恒定** ⇒ (M−g)/g 就是该点可测的台阶高度比 h/H。
  - 竖直墙面：列 x 固定时墙是一族同纵向距离的点 ⇒ M ≈ g(y_base) **沿列近常数**，
    D = (B/H)(y_base − y)：**墙脚处 D=0、越往上越大**。
  于是"上侧非地面行程里 M 近常数"⇒ 面（墙/车体），"M/g 近常数"⇒ 抬升面。
  再叠射线几何（边界线过消失点 ⇒ 沿单条射线延展；车底缘是横向弦 ⇒ 跨射线延展）
  ⇒ 墙 / 抬升路缘 / 车底缘 三分类。

用法（仓库根，.venv Python）：
    P=tools/experiments/speedrush_vision/probe_step_detect.py
    python $P selftest                     # 合成图自检：索引 + θ↔h/H + 墙定位偏置
    python $P sweep                        # 三变体 × 四阈值，对照预验证记录表
    python $P attrib --variant rel         # 归因分类 + CSV + 两张目检拼图（--th 默认 0.06）
    python $P attrib --variant mask
    python $P jitter --variant rel         # 唯一真连续段（stab 100..140）跨帧抖动
    python $P horizon                      # 逐帧隐含地平线行（g(y) 零点）vs 冻结 Y_H
    python $P rayfit [--anchor frozen|est] # 掩码下沿点上的边界射线识别率
    python $P vpfit                        # 逐帧估 VP（两线交点）+ 锚点行/列缺口
"""

from __future__ import annotations

import argparse
import csv
import sys
import warnings
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)   # 全 NaN 行中位数

APP, OUT, NPY = pd.APP, pd.OUT, pd.OUT / "npy"
Y0, Y1 = 340, 690                     # 检测带
DIAG_Y1 = 715                         # 诊断带（含近场）
W1 = W2 = 8                           # 下窗/上窗
XS = np.arange(20, 1240, 2)           # 检测列（避左右各 20px 边缘）
EGO = pcq.EGO_X                       # 自车列带
GATE = 0.15                           # 非地面门（量程比）
VPX, Y_H = pd.VPX, pd.Y_H             # 消失点横坐标 / 地平线行（calibration/gate0.json）
RAY_SUP = 30                          # 认定"一条边界射线"的最小支持列数
RAY_MIN = 100.0                       # 认定"一条边界射线"的最小径向延展（px）

# 预验证记录表（README 原值，用于比对归一方式）
RECORDED = {0.06: 79, 0.10: 70, 0.15: 65, 0.20: 59}
THS = (0.06, 0.10, 0.15, 0.20)


def cols_of(m: np.ndarray) -> np.ndarray:
    return np.r_[0:EGO[0], EGO[1]:m.shape[1]]


def road_range(m: np.ndarray) -> float:
    """路面量程 = M[340:700] 非自车列的 p95 − p5。"""
    band = m[Y0:700][:, cols_of(m)]
    return float(np.percentile(band, 95) - np.percentile(band, 5))


def ground_model(m: np.ndarray, rng: float) -> np.ndarray:
    """逐行中位数地面签名 g(y)（两轮稳健化），长度 = DIAG_Y1−Y0（含近场）。"""
    p = m[Y0:DIAG_Y1][:, cols_of(m)].astype(np.float32)
    med = np.nanmedian(p, axis=1)
    for _ in range(2):
        dev = np.abs(p - med[:, None])
        med = np.nanmedian(np.where(dev < 0.25 * rng, p, np.nan), axis=1)
    return med


def dev_map(m: np.ndarray, g: np.ndarray) -> np.ndarray:
    """全帧偏离量 D = M − g（带外 NaN）。"""
    d = np.full(m.shape, np.nan, np.float32)
    d[Y0:DIAG_Y1] = m[Y0:DIAG_Y1] - g[:, None]
    return d


def detect(m: np.ndarray, g: np.ndarray, rng: float, th: float, norm: str = "abs"):
    """→ (xs, ys, C)：逐列取对比度最大且 >θ 的候选；ys 为帧坐标。"""
    g = g[:Y1 - Y0]
    p = m[Y0:Y1][:, XS].astype(np.float32)
    ground = np.abs(p - g[:, None]) < GATE * rng
    bmed = np.median(np.lib.stride_tricks.sliding_window_view(p, W1, axis=0), axis=2)
    amed = np.median(np.lib.stride_tricks.sliding_window_view(p, W2, axis=0), axis=2)
    below_ok = np.lib.stride_tricks.sliding_window_view(ground, W1, axis=0).all(axis=2)
    b = np.arange(W2, below_ok.shape[0])                 # 下窗 b..b+W1-1，上窗 b-W2..b-1
    c = amed[b - W2] - bmed[b]
    c = c / np.maximum(bmed[b], 1e-6) if norm == "rel" else c / rng
    c = np.where(below_ok[b], c, -np.inf)
    i = np.argmax(c, axis=0)
    best = c[i, np.arange(c.shape[1])]
    rows = Y0 + b[i]
    sel = np.isfinite(best) & (best > th)
    return XS[sel], rows[sel], best[sel]


def detect_mask(m: np.ndarray, g: np.ndarray, rng: float, th: float):
    """变体 C（阈值穿越点）：边界 = 逐列**命中区（|D| > θ·量程）的下沿**，且其下 W1 全干净。

    与窗对比度的区别在定位：本变体的检出**行号**随 θ 变化，因此 θ 直接污染边界位置。
    合成自检给出两类边界的定位性质：
      - 抬升面：D 在几何边沿处连续归零 ⇒ 命中区下沿 = 真边沿，**定位无偏**；
      - 竖直墙：D = (B/H)(y_base − y) ⇒ 命中区下沿 = 偏离量达到 θ 的那一行，
        位于墙脚**上方 θ·量程·350 像素**（θ=0.06 → 约 19px，θ=0.20 → 约 64px），
        **定位有偏且偏置随 θ 线性增大**。
    """
    g = g[:Y1 - Y0]
    p = m[Y0:Y1][:, XS].astype(np.float32)
    hit = np.abs(p - g[:, None]) > th * rng
    below_ok = np.lib.stride_tricks.sliding_window_view(~hit, W1, axis=0).all(axis=2)
    above_hit = np.zeros_like(hit)
    above_hit[1:] = hit[:-1]                       # 上一行命中 ⇒ 本行是命中区下沿
    cand = below_ok & above_hit[:below_ok.shape[0]]
    i = np.where(cand, np.arange(cand.shape[0])[:, None], -1).max(axis=0)
    sel = i >= 0
    ys = Y0 + i[sel]
    c = np.abs(p[i[sel], np.arange(p.shape[1])[sel]] - g[i[sel] - 1]) / rng
    return XS[sel], ys, c


def run_detect(m: np.ndarray, g: np.ndarray, rng: float, th: float, variant: str = "mask"):
    """变体分发：abs/rel = 窗对比度；mask = 命中区下沿。"""
    if variant == "mask":
        return detect_mask(m, g, rng, th)
    return detect(m, g, rng, th, variant)


def jitter_pct(ys: np.ndarray) -> float:
    """帧内列间抖动：相邻检测列（x 相差 2px）的行差中位 / 350，%。"""
    if len(ys) < 3:
        return np.nan
    return float(np.median(np.abs(np.diff(ys))) / 350 * 100)


def hough_rays(xs: np.ndarray, ys: np.ndarray, res_deg: float = 0.25, tol: float = 3.0,
               min_slope: float = 0.0, vp: tuple[float, float] = (VPX, Y_H)):
    """过给定点（默认冻结消失点）的 1-D Hough（角度扫描）：边界线必过 VP ⇒ 一条线=一个角度。

    比固定角度分箱稳健——分箱会把一条略弯的边界线切碎（实测 0.5° 分箱下
    raylen_med = 0px）。`min_slope` 见 hough_free：与自由线对照时必须同设。
    → (角度, 支持列数, 径向延展, 内点掩码)；无支持时 None。
    """
    if len(xs) < 3:
        return None
    vpx, vpy = vp
    ang = np.degrees(np.arctan2(ys - vpy, xs - vpx))
    rad = np.hypot(xs - vpx, ys - vpy)
    grid = np.arange(-89.0, 89.0 + 1e-9, res_deg)
    if min_slope > 0:
        lim = np.degrees(np.arctan(min_slope))
        grid = grid[np.abs(grid) >= lim]
    if grid.size == 0:
        return None
    dist = np.abs(rad[None, :] * np.sin(np.radians(ang[None, :] - grid[:, None])))
    inside = dist <= tol
    sup = inside.sum(axis=1)
    i = int(np.argmax(sup))
    if sup[i] == 0:
        return None
    m = inside[i]
    return float(grid[i]), int(sup[i]), float(rad[m].max() - rad[m].min()), m


def side_of(xs: np.ndarray, ys: np.ndarray, sign: int) -> np.ndarray:
    """左右按 x 与消失点横坐标的关系定。

    勿用 atan2 的符号——检测点恒有 y > Y_H，故 atan2 值域是 (0°,180°)，符号恒正，
    用符号分左右会得到"左=空集"的假读数（实测踩过）。
    """
    if len(xs) == 0:
        return np.zeros(0, bool)
    return (xs < VPX) if sign < 0 else (xs > VPX)


def shape_of(m: np.ndarray, g: np.ndarray, rng: float, x: int, y: int) -> tuple[str, float, int]:
    """检测点上方非地面行程的形状 → ("face"|"raised"|"other"|"none", 台阶比, 行程长)。

    先按偏离符号定族，再验该族的几何不变量（符号与形状必须**同时**成立，否则
    车体这类三维近区会被误判成抬升面）：
      - 近区（D>0）：竖直墙面/车体正面 ⇒ M 沿列近常数 → "face"，
        返回 mean(M)/mean(g) − 1（近于地面的相对量）。
      - 远区（D<0）：抬升水平面 ⇒ M/g 近常数 → "raised"，返回 h/H = 1 − mean(M/g)。
      - 不变量不成立 ⇒ "other"（三维车体、混合符号等）。
    """
    rows = np.arange(y - 1, Y0 - 1, -1)
    if len(rows) < 3:
        return "none", 0.0, 0
    dv = m[rows, x] - g[rows - Y0]
    ok = np.abs(dv) > GATE * rng
    if ok.sum() < 3:
        return "none", 0.0, int(ok.sum())
    run = 0
    miss = 0
    for v in ok:
        if v:
            run += 1
            miss = 0
        else:
            miss += 1
            if miss > 2:
                break
    run = max(run, 3)
    seg = rows[:run]
    mv = m[seg, x]
    gv = g[seg - Y0]
    ratio = mv / np.maximum(gv, 1e-6)
    cv_m = float(np.std(mv) / max(abs(np.mean(mv)), 1e-6))
    cv_r = float(np.std(ratio) / max(abs(np.mean(ratio)), 1e-6))
    if float(np.median(dv[:run])) > 0:                      # 近区
        return ("face", float(np.mean(mv) / max(abs(np.mean(gv)), 1e-6) - 1.0), run) \
            if cv_m <= cv_r else ("other", 0.0, run)
    return ("raised", float(1.0 - np.mean(ratio)), run) if cv_r <= cv_m \
        else ("other", 0.0, run)


def max_run(mask: np.ndarray) -> int:
    """沿 x 的最长连续 True 段（逐行取最大）。"""
    best = np.zeros(mask.shape[0], int)
    cur = np.zeros(mask.shape[0], int)
    for j in range(mask.shape[1]):
        cur = np.where(mask[:, j], cur + 1, 0)
        best = np.maximum(best, cur)
    return int(best.max()) if mask.size else 0


def region_stats(m: np.ndarray, g: np.ndarray, rng: float, stride: int = 12):
    """用 15% 门定位非地面区下沿（与 θ 无关），再判其形状 → 该帧**存在什么**。

    归因需要"这一帧画面里到底有什么"，而不是"某个 θ 下检出了什么"。
    """
    xs, ys, _ = detect_mask(m, g, rng, GATE)
    kinds, hhs = [], []
    for x, y in zip(xs[::stride], ys[::stride]):
        k, hh, _ = shape_of(m, g, rng, int(x), int(y))
        kinds.append(k)
        if k == "raised":
            hhs.append(hh)
    return kinds, hhs


def diag_frame(m: np.ndarray, g: np.ndarray, rng: float, xs, ys) -> dict:
    """逐帧诊断量（全零标注，读参照图）。掩码一律用**全帧列轴**，勿用压缩列轴。"""
    d = dev_map(m, g)
    dd = d[Y0:DIAG_Y1]                          # (375, 1280)
    ne = dd[:, cols_of(m)]                      # 非自车列（地面模型支撑集）
    near_f = dd > GATE * rng                    # 近于地面 = 墙面/车体（全帧列轴）
    far_f = dd < -GATE * rng                    # 远于地面 = 抬升面（全帧列轴）
    out = {
        "fit_inlier": float((np.abs(ne) < GATE * rng).mean()),
        "fit_mono": pcq.spearman(g[:Y1 - Y0], np.arange(Y0, Y1)),
        "far_area": float(far_f[:, cols_of(m)].mean()),
        "near_area": float(near_f[:, cols_of(m)].mean()),
        "occ_run": float(max_run(near_f[460 - Y0:620 - Y0, 300:980]) / 680),
        "edge_l": float(near_f[Y1 - Y0:, :60].mean()),
        "edge_r": float(near_f[Y1 - Y0:, 1220:].mean()),
        "far_lat": float(np.concatenate([far_f[:, :300], far_f[:, 980:]], axis=1).mean()),
        "n_det": int(len(xs)),
        "cov": float(len(xs) / len(XS)),
        "jit": jitter_pct(ys),
    }
    r = hough_rays(xs, ys)
    out["line_cols"] = int(r[1]) if r else 0
    out["line_len"] = float(r[2]) if r else 0.0
    for side, sign in (("l", -1), ("r", 1)):
        sel = side_of(xs, ys, sign)
        h = hough_rays(xs[sel], ys[sel])
        ok = bool(h and h[1] >= RAY_SUP and h[2] >= RAY_MIN)
        out[f"ray_{side}"] = int(ok)
        out[f"ray_{side}_sup"] = int(h[1]) if h else 0
        out[f"ray_{side}_len"] = float(h[2]) if h else 0.0
        out[f"ray_{side}_ymax"] = int(ys[sel][h[3]].max()) if ok else 0
    # 检出物形状：抽样至多 30 列
    kinds, hhs = [], []
    if len(xs):
        step = max(len(xs) // 30, 1)
        for x, y in zip(xs[::step], ys[::step]):
            k, hh, _ = shape_of(m, g, rng, int(x), int(y))
            kinds.append(k)
            if k == "raised":
                hhs.append(hh)
    out["kind_face"] = kinds.count("face")
    out["kind_raised"] = kinds.count("raised")
    out["kind_none"] = kinds.count("none")
    out["n_shape_sampled"] = len(kinds)
    out["hh_med"] = float(np.median(hhs)) if hhs else np.nan
    rk, rhh = region_stats(m, g, rng)
    out["reg_face"] = rk.count("face")
    out["reg_raised"] = rk.count("raised")
    out["reg_other"] = rk.count("other")
    out["reg_none"] = rk.count("none")
    out["reg_hh"] = float(np.median(rhh)) if rhh else np.nan
    # 掩码下边界（阈值无关参照）：非地面近区的下边界行，按门 0.05/0.10/0.15
    out["mask_base"] = {}
    for gate in (0.05, 0.10, 0.15):
        n2 = d[Y0:Y1] > gate * rng
        rows_any = np.where(n2.any(axis=1))[0]
        out["mask_base"][gate] = int(Y0 + rows_any.max()) if len(rows_any) else 0
    return out


def is_strong(d: dict) -> bool:
    """预验证记录表的口径：cov≥0.30 且 jit≤5%（帧内列间抖动）。"""
    return d["cov"] >= 0.30 and (np.isnan(d["jit"]) or d["jit"] <= 5.0)


def classify(d: dict) -> str:
    """归因分类（优先级自上而下；判据见 README 同名小节）。

    只在**非强检出**帧上调用。`occ_run` 不参与分类：实测两群几乎相同
    （非强 0.31 vs 强 0.33）⇒ 遮挡假设被数据否掉，只留作诊断量。
    C6 是维护者四类之外、数据里最大的一类："有检出但两侧边界射线都没识别出来"
    ——cov≥0.30 由**散点**（路面纹理/车身/远带噪声）满足，而不是由一条边界线满足。
    """
    if d["fit_inlier"] < 0.60 or d["fit_mono"] < 0.90:
        return "C1_地面签名失败"
    if d["edge_l"] >= 0.30 or d["edge_r"] >= 0.30:
        return "C3_边界出画"
    if d["n_det"] == 0 and d["reg_face"] + d["reg_raised"] == 0:
        return "C5_画面内无台阶"
    if not (d["ray_l"] or d["ray_r"]):
        return "C6_检出散点非边界线"
    if d["reg_face"] >= d["reg_raised"]:
        return "C4a_有墙面但检不出"
    return "C4b_抬升面低于阈值"


# ---------------------------------------------------------------- 合成自检
def _synth(kind: str) -> np.ndarray:
    h, w = 720, 1280
    yy = np.arange(h, dtype=np.float32)[:, None]
    road = np.broadcast_to(np.where(yy >= Y_H, (yy - Y_H) / 350.0, np.nan), (h, w)).copy()
    if kind == "road":
        return road
    m = road.copy()
    if kind == "raised":                       # 左侧抬升水平面 h/H=0.15，边沿行 480
        m[400:480, 0:400] = road[400:480, 0:400] * 1.15
    elif kind == "wall":                       # 右侧竖直墙，墙脚行 450，列 ≥1100
        m[350:450, 1100:] = road[450, 0]
    return m


def hough_free(xs: np.ndarray, ys: np.ndarray, res_deg: float = 0.5, tol: float = 3.0,
               min_slope: float = 0.25):
    """自由线 1-D Hough（方向 × 法向偏移），→ (支持, 方向角, 偏移, 径向延展)。

    与 hough_rays 的唯一区别是**不强制过消失点**：两者支持数之差 = "VP 约束
    （冻结标定）本身吃掉多少支持"，即 §4 判别器前提的检验。

    `min_slope` 必需：不设斜率下限时，自由线会去拟近水平的**弦**（车底缘、远场
    边缘），那与"边界是过 VP 的射线"无关，且其与地平线行的交点会跑到上千 px 外
    （实测漂移中位 1660px 即此病态）。真实边界射线从 VP 到画面下角的斜率 ≥0.5，
    取下限 0.25 留余量。
    """
    if len(xs) < 3:
        return None
    best = None
    lim = np.degrees(np.arctan(min_slope))
    for a in np.arange(-89.0, 90.0, res_deg):
        if abs(a) < lim:
            continue
        rr = np.radians(a)
        off = xs * np.sin(rr) - ys * np.cos(rr)
        s = np.sort(off)
        cnt = np.searchsorted(s, off + tol, "right") - np.searchsorted(s, off - tol, "left")
        i = int(np.argmax(cnt))
        if best is None or cnt[i] > best[0]:
            m = np.abs(off - off[i]) <= tol
            rad = np.hypot(xs[m] - VPX, ys[m] - Y_H)
            best = (int(cnt[i]), float(a), float(off[i]),
                    float(rad.max() - rad.min()) if m.any() else 0.0)
    return best


def vp_x_of(ang_deg: float, off: float) -> float:
    """自由线与地平线行 y=Y_H 的交点横坐标（与 VPX 比 = VP 漂移量）。"""
    rr = np.radians(ang_deg)
    sa = np.sin(rr)
    return float((off + Y_H * np.cos(rr)) / sa) if abs(sa) > 1e-6 else np.nan


def cmd_rayfit(args) -> None:
    """§6 正确口径 + §4 前提检验：在**掩码下沿点**（θ 无关）上拟合边界射线。

    argmax 检出点上的"最优射线"会选到远场浅线（实测），不能当"边界识别率"；
    掩码下沿点按构造就是边界候选。

    `--anchor {frozen,est}`：射线锚点的**行**用冻结 Y_H 还是逐帧 g(y) 零点（列都用
    冻结 VPX）。两者之差直接回答"识别率低是标定行偏了，还是横向（航向/弯道）不对"。
    """
    frames = pcq.all_frames()
    rec = []
    for p in frames:
        k = pcq.frame_key(p)
        m = np.load(NPY / f"{k}__da2s.npy").astype(np.float32)
        rng = road_range(m)
        g = ground_model(m, rng)
        y0 = horizon_row(m, g) if args.anchor == "est" else Y_H
        if not np.isfinite(y0):
            y0 = Y_H
        bx, by, _ = detect_mask(m, g, rng, GATE)
        row = {"key": k}
        for side, sign in (("l", -1), ("r", 1)):
            sel = side_of(bx, by, sign)
            hv = hough_rays(bx[sel], by[sel], min_slope=0.25, vp=(VPX, y0))
            hf = hough_free(bx[sel], by[sel], min_slope=0.25)
            row[f"n_{side}"] = int(sel.sum())
            row[f"vp_{side}"] = int(bool(hv and hv[1] >= RAY_SUP and hv[2] >= RAY_MIN))
            row[f"free_{side}"] = int(bool(hf and hf[0] >= RAY_SUP and hf[3] >= RAY_MIN))
            row[f"sup_vp_{side}"] = hv[1] if hv else 0
            row[f"sup_free_{side}"] = hf[0] if hf else 0
            row[f"drift_{side}"] = abs(vp_x_of(hf[1], hf[2]) - VPX) if hf else np.nan
        rec.append(row)
    n = len(rec)
    print(f"== 掩码下沿点上的射线拟合（{n} 帧；射线口径 = 支持≥{RAY_SUP} 列且延展≥"
          f"{RAY_MIN:.0f}px；锚点行={args.anchor}）==")
    print(f"{'侧':4s} {'点数中位':>8s} {'VP约束识别':>10s} {'自由线识别':>10s} "
          f"{'支持中位VP':>10s} {'支持中位自由':>10s} {'VP漂移中位':>10s}")
    for side in ("l", "r"):
        print(f"{side:4s} {np.median([r[f'n_{side}'] for r in rec]):8.0f} "
              f"{sum(r[f'vp_{side}'] for r in rec):8d}/{n} "
              f"{sum(r[f'free_{side}'] for r in rec):8d}/{n} "
              f"{np.median([r[f'sup_vp_{side}'] for r in rec]):10.0f} "
              f"{np.median([r[f'sup_free_{side}'] for r in rec]):10.0f} "
              f"{np.nanmedian([r[f'drift_{side}'] for r in rec]):10.0f}px")
    gain = sum(1 for r in rec for s in ("l", "r")
               if r[f"free_{s}"] and not r[f"vp_{s}"])
    print(f"\n自由线能识别而 VP 约束不能的（帧×侧）= {gain}/{2*n}"
          f"  ← 该值大 = 冻结 VP 标定是识别率的主要瓶颈，不是检测器")
    both = sum(1 for r in rec if r["vp_l"] and r["vp_r"])
    anyv = sum(1 for r in rec if r["vp_l"] or r["vp_r"])
    print(f"VP 约束：两侧都识别={both}/{n} 至少一侧={anyv}/{n}")


def line_intersect(a1: float, o1: float, a2: float, o2: float):
    """两条线（方向角、法向偏移）的交点。法向 n=(sin a, −cos a)，线 = {p : p·n = off}。"""
    r1, r2 = np.radians(a1), np.radians(a2)
    A = np.array([[np.sin(r1), -np.cos(r1)], [np.sin(r2), -np.cos(r2)]], np.float64)
    det = A[0, 0] * A[1, 1] - A[0, 1] * A[1, 0]
    if abs(det) < 1e-6:
        return np.nan, np.nan
    b = np.array([o1, o2], np.float64)
    return ((A[1, 1] * b[0] - A[0, 1] * b[1]) / det,
            (A[0, 0] * b[1] - A[1, 0] * b[0]) / det)


def cmd_vpfit(_args) -> None:
    """逐帧估消失点：左右边界线各自自由拟合（不绑 VP），交点 = 该帧 VP。

    回答三问：① 估出的 VP 与冻结值差多少（= 冻结标定在真实帧上的实际误差）；
    ② 交点 y 与地平线行 Y_H 差多少（= "VP 落在地平线行"这个标定假设是否成立）；
    ③ 用逐帧 VP 重跑"边界=过 VP 射线"的识别率，能否救回 `rayfit` 里冻结 VP 的低覆盖
      ——这一条才是决定第一关参数化怎么写的答案。
    """
    frames = pcq.all_frames()
    rec = []
    for p in frames:
        k = pcq.frame_key(p)
        m = np.load(NPY / f"{k}__da2s.npy").astype(np.float32)
        rng = road_range(m)
        g = ground_model(m, rng)
        bx, by, _ = detect_mask(m, g, rng, GATE)
        row = {"key": k, "vx": np.nan, "vy": np.nan}
        fits = {}
        for side, sign in (("l", -1), ("r", 1)):
            sel = side_of(bx, by, sign)
            hf = hough_free(bx[sel], by[sel], min_slope=0.25)
            ok = bool(hf and hf[0] >= 20 and hf[3] >= 80)
            fits[side] = hf if ok else None
            row[f"fit_{side}"] = int(ok)
        if fits["l"] and fits["r"]:
            row["vx"], row["vy"] = line_intersect(fits["l"][1], fits["l"][2],
                                                  fits["r"][1], fits["r"][2])
            for side, sign in (("l", -1), ("r", 1)):
                sel = side_of(bx, by, sign)
                he = hough_rays(bx[sel], by[sel], min_slope=0.25, vp=(row["vx"], row["vy"]))
                hz = hough_rays(bx[sel], by[sel], min_slope=0.25)
                row[f"est_{side}"] = int(bool(he and he[1] >= RAY_SUP and he[2] >= RAY_MIN))
                row[f"frz_{side}"] = int(bool(hz and hz[1] >= RAY_SUP and hz[2] >= RAY_MIN))
        rec.append(row)
    n = len(rec)
    got = [r for r in rec if np.isfinite(r["vx"])]
    print(f"== 逐帧估 VP（{n} 帧；两侧都拟出直线 = {len(got)}）==")
    print(f"单侧拟出：左 {sum(r['fit_l'] for r in rec)}/{n}  右 {sum(r['fit_r'] for r in rec)}/{n}")
    if got:
        dx = np.abs([r["vx"] - VPX for r in got])
        dy = np.abs([r["vy"] - Y_H for r in got])
        inframe = sum(1 for r in got if 0 <= r["vx"] < 1280)
        print(f"VP 横向偏差 |vx−{VPX:.1f}|：中位={np.median(dx):.0f}px "
              f"p90={np.percentile(dx, 90):.0f}px  估点落在画面内={inframe}/{len(got)}")
        print(f"VP 纵向偏差 |vy−{Y_H:.1f}|（地平线行假设）：中位={np.median(dy):.0f}px "
              f"p90={np.percentile(dy, 90):.0f}px")
        print("\n-- 识别率对比（同一批掩码下沿点、同一口径：支持≥30 且延展≥100px）--")
        print(f"{'':10s} {'左':>8s} {'右':>8s} {'两侧':>8s} {'至少一侧':>9s}")
        for tag, pre in (("冻结 VP", "frz"), ("逐帧估 VP", "est")):
            both = sum(1 for r in got if r[f"{pre}_l"] and r[f"{pre}_r"])
            anyv = sum(1 for r in got if r[f"{pre}_l"] or r[f"{pre}_r"])
            print(f"{tag:10s} {sum(r[f'{pre}_l'] for r in got):7d} "
                  f"{sum(r[f'{pre}_r'] for r in got):8d} {both:8d} {anyv:9d}"
                  f"   （/{len(got)} 帧）")
    seg = [r for r in rec if r["key"].startswith(f"{pd.CTRL_SESSION.name}__")
           and 100 <= int(r["key"].split("__")[1]) <= 140 and np.isfinite(r["vx"])]
    if len(seg) > 3:
        vx = np.array([r["vx"] for r in seg])
        vy = np.array([r["vy"] for r in seg])
        print(f"\n-- 连续段（100..140，估出 {len(seg)} 帧）的时序可用性 --")
        print(f"vx 范围 [{vx.min():.0f}, {vx.max():.0f}] std={vx.std():.0f}px "
              f"一阶差分 std={np.diff(vx).std():.0f}px")
        print(f"vy 范围 [{vy.min():.0f}, {vy.max():.0f}] std={vy.std():.0f}px "
              f"一阶差分 std={np.diff(vy).std():.0f}px")


def horizon_row(m: np.ndarray, g: np.ndarray) -> float:
    """g(y) 的零点 = 深度图自己承认的地平线行（不依赖直线拟合、不依赖 VP 假设）。

    地面平面的视差严格正比于 (y − 地平线行)，故对 g 做一次稳健线性拟合取零点即可。
    """
    yy = np.arange(Y0, Y0 + len(g), dtype=np.float64)
    ok = np.isfinite(g)
    if ok.sum() < 50:
        return np.nan
    a, b = pcq.theil_sen(yy[ok], g[ok].astype(np.float64))
    if not np.isfinite(a) or abs(a) < 1e-6:
        return np.nan
    return float(-b / a)


def cmd_horizon(_args) -> None:
    """冻结 Y_H 与深度图隐含地平线的对照（第一关几何锚点的选址依据）。"""
    frames = pcq.all_frames()
    y0, lin, resid = [], [], []
    for p in frames:
        m = np.load(NPY / f"{pcq.frame_key(p)}__da2s.npy").astype(np.float32)
        rng = road_range(m)
        g = ground_model(m, rng)[:Y1 - Y0].astype(np.float64)
        yy = np.arange(Y0, Y1, dtype=np.float64)
        ok = np.isfinite(g)
        y0.append(horizon_row(m, g))
        if ok.sum() < 50:
            continue
        a, b = pcq.theil_sen(yy[ok], g[ok])
        lin.append(float(np.corrcoef(yy[ok], g[ok])[0, 1]))
        resid.append(float(np.median(np.abs(g[ok] - (a * yy[ok] + b))) / max(rng, 1e-9)))
    v = np.array(y0)
    good = np.isfinite(v)
    d = np.abs(v[good] - Y_H)
    print(f"== 隐含地平线行（g(y) 零点，n={int(good.sum())}/{len(frames)}）==")
    print(f"分位 5/25/50/75/95 = {np.round(np.percentile(v[good], [5, 25, 50, 75, 95]), 1)}")
    print(f"vs 冻结 Y_H={Y_H}：偏差中位={np.median(v[good]) - Y_H:+.1f}px "
          f"|偏差| 中位={np.median(d):.0f}px p90={np.percentile(d, 90):.0f}px "
          f"落在 ±10px 内={int((d <= 10).sum())}/{int(good.sum())}")
    print(f"g 对 y 线性相关：中位={np.median(lin):.4f} p5={np.percentile(lin, 5):.4f}；"
          f"残差/量程：中位={np.median(resid):.4f} p90={np.percentile(resid, 90):.4f}")
    seg = [v[i] for i, p in enumerate(frames)
           if p.parent == pd.CTRL_SESSION and 100 <= int(p.stem) <= 140
           and np.isfinite(v[i])]
    if len(seg) > 3:
        s = np.array(seg)
        print(f"连续段（100..140，{len(s)} 帧）：std={s.std():.1f}px "
              f"一阶差分 std={np.diff(s).std():.1f}px  ← 逐帧估计可用性的判据")


def cmd_selftest(_args) -> None:
    print("== 合成自检（已知几何，验证索引与 θ↔h/H 关系）==")
    for kind in ("road", "raised", "wall"):
        m = _synth(kind)
        rng = road_range(m)
        g = ground_model(m, rng)
        print(f"\n[{kind}] 量程={rng:.3f}  g 单调={pcq.spearman(g[:Y1 - Y0], np.arange(Y0, Y1)):.3f} "
              f"g(690)={g[Y1 - Y0 - 1]:.3f}")
        for norm in ("abs", "rel"):
            for th in (0.06, 0.15):
                xs, ys, c = detect(m, g, rng, th, norm)
                tag = f"  {norm} θ={th:.2f}"
                if len(xs) == 0:
                    print(f"{tag}: 零检出")
                else:
                    print(f"{tag}: n={len(xs):4d} 列 x∈[{xs.min()},{xs.max()}] "
                          f"行中位={np.median(ys):.0f} C中位={np.median(c):.4f}")
        for th in (0.06, 0.15, 0.20):
            xs, ys, c = detect_mask(m, g, rng, th)
            if len(xs) == 0:
                print(f"  mask θ={th:.2f}: 零检出")
            else:
                print(f"  mask θ={th:.2f}: n={len(xs):4d} 列 x∈[{xs.min()},{xs.max()}] "
                      f"行中位={np.median(ys):.0f}")
    print("\n[wall] 掩码下边界（门 0.05/0.10/0.15）vs 真墙脚 450：")
    m = _synth("wall")
    rng = road_range(m)
    g = ground_model(m, rng)
    d = dev_map(m, g)
    for gate in (0.05, 0.10, 0.15):
        rows = np.where((d[Y0:Y1, 1100:] > gate * rng).any(axis=1))[0]
        print(f"  门 {gate:.2f} → 下边界行 {Y0 + rows.max() if len(rows) else 0} "
              f"（偏置 {450 - (Y0 + rows.max() if len(rows) else 0):+d}px）")


# ---------------------------------------------------------------- 阈值扫描
def cmd_sweep(_args) -> None:
    frames = pcq.all_frames()
    print(f"== 阈值扫描（{len(frames)} 帧，读 da2s 参照缓存，零推理）==")
    print(f"预验证记录表：{RECORDED}")
    acc = {(v, th): {"n": 0, "cov": [], "c": []}
           for v in ("abs", "rel", "mask") for th in THS}
    for p in frames:
        m = np.load(NPY / f"{pcq.frame_key(p)}__da2s.npy").astype(np.float32)
        rng = road_range(m)
        g = ground_model(m, rng)
        for th in THS:
            for norm in ("abs", "rel"):
                xs, ys, c = detect(m, g, rng, th, norm)
                a = acc[(norm, th)]
                if len(xs) == 0:
                    continue
                cov = len(xs) / len(XS)
                jit = jitter_pct(ys)
                if cov >= 0.30 and (np.isnan(jit) or jit <= 5.0):
                    a["n"] += 1
                a["cov"].append(cov)
                a["c"].append(float(np.median(c)))
            xs, ys, c = detect_mask(m, g, rng, th)
            a = acc[("mask", th)]
            if len(xs) == 0:
                continue
            cov = len(xs) / len(XS)
            jit = jitter_pct(ys)
            if cov >= 0.30 and (np.isnan(jit) or jit <= 5.0):
                a["n"] += 1
            a["cov"].append(cov)
            a["c"].append(float(np.median(c)))
    for norm in ("abs", "rel", "mask"):
        print(f"\n-- 归一方式 = {norm} --")
        print("θ     强检出帧(cov≥0.30 且 jit≤5%)  cov中位(检出帧)  C中位")
        for th in THS:
            a = acc[(norm, th)]
            rec = RECORDED[th]
            print(f"{th:.2f}  {a['n']:3d}/{len(frames)}  (记录 {rec:3d})"
                  f"        {np.median(a['cov']) if a['cov'] else float('nan'):.3f}"
                  f"          {np.median(a['c']) if a['c'] else float('nan'):.4f}")


# ---------------------------------------------------------------- 归因
def cmd_attrib(args) -> None:
    variant, th = args.variant, args.th
    frames = pcq.all_frames()
    rows = []
    maps = {}
    for p in frames:
        k = pcq.frame_key(p)
        m = np.load(NPY / f"{k}__da2s.npy").astype(np.float32)
        rng = road_range(m)
        g = ground_model(m, rng)
        xs, ys, _ = run_detect(m, g, rng, th, variant)
        d = diag_frame(m, g, rng, xs, ys)
        d["key"] = k
        d["path"] = str(p)
        d["rng"] = rng
        d["strong"] = int(is_strong(d))
        d["label"] = "OK_强检出" if d["strong"] else classify(d)
        rows.append(d)
        maps[k] = (m, g, rng, xs, ys)
    # CSV
    flat = [{**{k: v for k, v in r.items() if not isinstance(v, dict)},
             **{f"maskbase_{int(k*100)}": v for k, v in r["mask_base"].items()}}
            for r in rows]
    out_csv = OUT / f"step_attrib_{variant}_th{int(th*100)}.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(flat[0]))
        wr.writeheader()
        wr.writerows(flat)
    strong = [r for r in rows if r["strong"]]
    weak = [r for r in rows if not r["strong"]]
    print(f"== 归因（变体={variant} θ={th:.2f}；{len(rows)} 帧）==")
    print(f"强检出（cov≥0.30 且 jit≤5%）= {len(strong)}；非强检出 = {len(weak)}"
          f"（其中零检出 {sum(1 for r in weak if r['n_det'] == 0)} 帧）")
    print("分类              帧数")
    lab = {}
    for r in rows:
        lab[r["label"]] = lab.get(r["label"], 0) + 1
    for k, v in sorted(lab.items(), key=lambda x: -x[1]):
        print(f"{k:20s} {v:4d}")

    print("\n-- 画面里有什么（15% 门定位非地面区下沿，与 θ 无关；抽样列）--")
    print(f"面(face) 列合计={sum(r['reg_face'] for r in rows)} "
          f"抬升面(raised) 列合计={sum(r['reg_raised'] for r in rows)} "
          f"其他(other) 列合计={sum(r['reg_other'] for r in rows)} "
          f"无 列合计={sum(r['reg_none'] for r in rows)}")
    rhh = np.array([r["reg_hh"] for r in rows if np.isfinite(r["reg_hh"])])
    if len(rhh):
        print(f"抬升面 h/H 中位={np.median(rhh):.3f} "
              f"p25/p75={np.percentile(rhh, 25):.3f}/{np.percentile(rhh, 75):.3f} "
              f"（n={len(rhh)} 帧）")
    far_lat = np.array([r["far_lat"] for r in rows])
    print(f"横向带(左右各300px) 远于地面 >15% 的面积占比：中位={np.median(far_lat):.4f} "
          f"p90={np.percentile(far_lat, 90):.4f} 超 2% 的帧={int((far_lat > 0.02).sum())}/{len(rows)}"
          f"（此值高 = 墙被判成更远 = 静默错误）")

    print("\n-- 诊断量中位（非强检出 vs 强检出）--")
    keys = ("fit_inlier", "fit_mono", "far_area", "near_area", "occ_run",
            "edge_l", "edge_r", "far_lat", "reg_face", "reg_raised", "reg_other",
            "reg_hh", "line_cols", "line_len", "ray_l_sup", "ray_r_sup")
    print(f"{'量':12s} {'非强':>10s} {'强':>10s}")
    for k in keys:
        a = np.nanmedian([r[k] for r in weak]) if weak else np.nan
        b = np.nanmedian([r[k] for r in strong]) if strong else np.nan
        print(f"{k:12s} {a:10.4f} {b:10.4f}")

    print("\n-- 检出物形状（抽样列）--")
    print(f"face 列合计={sum(r['kind_face'] for r in rows)} "
          f"raised 列合计={sum(r['kind_raised'] for r in rows)} "
          f"none 列合计={sum(r['kind_none'] for r in rows)}")
    hh = np.array([r["hh_med"] for r in rows if np.isfinite(r["hh_med"])])
    if len(hh):
        print(f"检出抬升面 h/H 中位={np.median(hh):.3f} "
              f"p25/p75={np.percentile(hh, 25):.3f}/{np.percentile(hh, 75):.3f}")

    print("\n-- 覆盖率改口径（θ 无关的可用量；射线 = 1-D Hough 支持列数≥30 且延展≥100px）--")
    print(f"左射线识别={sum(r['ray_l'] for r in rows)}/{len(rows)} "
          f"右射线识别={sum(r['ray_r'] for r in rows)}/{len(rows)} "
          f"两侧都识别={sum(1 for r in rows if r['ray_l'] and r['ray_r'])}/{len(rows)}")
    print(f"射线触达近场(y≥600)={sum(1 for r in rows if r['ray_l_ymax'] >= 600 or r['ray_r_ymax'] >= 600)}/{len(rows)}")
    bands = (("远 y<450", Y0, 450), ("中 450-600", 450, 600), ("近 y≥600", 600, DIAG_Y1))
    print(f"{'行带':12s} {'有检出的帧':>10s} {'带内列覆盖中位':>14s}")
    for name, lo, hi in bands:
        has = 0
        covs = []
        for r in rows:
            ys = maps[r["key"]][4]
            n = int(((ys >= lo) & (ys < hi)).sum())
            has += n > 0
            covs.append(n / len(XS))
        print(f"{name:12s} {has:7d}/{len(rows)} {np.median(covs) if covs else 0:14.3f}")
    print(f"\n[CSV] {out_csv}")
    _sheet_nodet(weak, maps, f"{variant}_th{int(th*100)}")
    _sheet_rep(rows, maps, f"{variant}_th{int(th*100)}")


def _sheet_nodet(nodet, maps, variant) -> None:
    """检不出帧：低分率缩略图 + 分类标签（维护者目检归因是否成立）。"""
    from PIL import Image, ImageDraw, ImageFont
    if not nodet:
        return
    cw, ch, cols = 320, 180, 8
    rows_n = (len(nodet) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cw, rows_n * (ch + 16)), "white")
    dr = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, r in enumerate(sorted(nodet, key=lambda x: (x["label"], x["key"]))):
        im = Image.open(r["path"]).resize((cw, ch))
        x, y = (i % cols) * cw, (i // cols) * (ch + 16)
        sheet.paste(im, (x, y))
        dr.text((x + 3, y + ch + 2), f"{r['label'][:2]} {r['key'][:28]}",
                fill="black", font=font)
    o = OUT / f"step_nodet_sheet_{variant}.jpg"
    sheet.save(o, quality=86)
    print(f"[sheet] {o}（{len(nodet)} 帧检不出，标签 = 归因分类）")


def _draw_ray(img, ang_deg: float, color, r0: float = 40.0, r1: float = 1400.0) -> None:
    """画一条过消失点的射线（角度 = atan2 口径，度）。"""
    a = np.radians(ang_deg)
    cv2.line(img, (int(VPX + r0 * np.cos(a)), int(Y_H + r0 * np.sin(a))),
             (int(VPX + r1 * np.cos(a)), int(Y_H + r1 * np.sin(a))), color, 2)


def _sheet_rep(rows, maps, variant) -> None:
    """每类取 2 帧：RGB + 视差叠加（检出点 / 非地面近区 / 非地面远区）。"""
    from PIL import Image, ImageDraw, ImageFont
    picks = []
    for lab in ("C1_地面签名失败", "C3_边界出画", "C4a_有墙面但检不出",
                "C4b_抬升面低于阈值", "C5_画面内无台阶", "C6_检出散点非边界线"):
        g = [r for r in rows if r["label"] == lab][:2]
        picks += [(lab, r) for r in g]
    if not picks:
        return
    pw, ph = 480, 270
    sheet = Image.new("RGB", (2 * pw, len(picks) * (ph + 16)), "white")
    dr = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, (lab, r) in enumerate(picks):
        m, g, rng, xs, ys = maps[r["key"]]
        bgr = cv2.imread(r["path"])
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        # 统一刻度：路面区 p0.5–p99.5（避免天空吃掉暖端）
        band = m[Y0:DIAG_Y1][:, cols_of(m)]
        lo, hi = np.percentile(band, [0.5, 99.5])
        du = np.clip((m - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
        vis = cv2.applyColorMap(du, cv2.COLORMAP_JET)          # BGR
        vis = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)             # → RGB（防通道互换）
        d = dev_map(m, g)
        far = d < -GATE * rng
        near = d > GATE * rng
        vis[near] = (vis[near] * 0.4 + np.array([255, 255, 255]) * 0.6).astype(np.uint8)
        vis[far] = (vis[far] * 0.4 + np.array([0, 0, 0]) * 0.6).astype(np.uint8)
        for x, y in zip(xs, ys):
            cv2.circle(vis, (int(x), int(y)), 3, (0, 255, 0), -1)
        # 识别到的边界射线（青=左/右各自最佳，品红=全图最佳）→ 目检"散点 vs 线"
        for side, col in (("l", (255, 255, 0)), ("r", (255, 255, 0))):
            sel = side_of(xs, ys, -1 if side == "l" else 1)
            h = hough_rays(xs[sel], ys[sel])
            if h and h[1] >= RAY_SUP and h[2] >= RAY_MIN:
                _draw_ray(vis, h[0], col)
        hall = hough_rays(xs, ys)
        if hall and hall[1] >= RAY_SUP:
            _draw_ray(vis, hall[0], (255, 0, 255))
        sheet.paste(Image.fromarray(cv2.resize(rgb, (pw, ph))), (0, i * (ph + 16)))
        sheet.paste(Image.fromarray(cv2.resize(vis, (pw, ph))), (pw, i * (ph + 16)))
        dr.text((4, i * (ph + 16) + ph + 2),
                f"{lab} | {r['key'][:40]} | 检出{r['n_det']} 最优射线支持{r['line_cols']}列"
                f"/{r['line_len']:.0f}px 左{r['ray_l_sup']} 右{r['ray_r_sup']}"
                f"（白=近区 黑=远区 青/品红=射线）",
                fill="black", font=font)
    o = OUT / f"step_rep_sheet_{variant}.jpg"
    sheet.save(o, quality=88)
    print(f"[sheet] {o}（每类 2 帧：左 RGB / 右 视差+检出点，JET 红=近 蓝=远）")


# ---------------------------------------------------------------- 跨帧抖动
def cmd_jitter(args) -> None:
    """唯一真连续段（stab = 同一 session 的 100..140）上的跨帧抖动。

    帧内列间抖动（jit）不是 P3：本命令给真跨帧量——左右边界射线角的一阶差分
    （含真实运动）与二阶差分（去掉匀速运动后的高频抖动）。
    """
    variant = args.variant
    print(f"== 跨帧抖动（连续段 100..140，41 帧；变体={variant}）==")
    print("θ     侧  识别帧  角std    1阶std   2阶std")
    for th in (0.06, 0.15):
        seq = {s: [] for s in ("l", "r")}
        for i in range(100, 141):
            p = pd.CTRL_SESSION / f"{i:06d}.jpg"
            if not p.exists():
                continue
            k = pcq.frame_key(p)
            m = np.load(NPY / f"{k}__da2s.npy").astype(np.float32)
            rng = road_range(m)
            g = ground_model(m, rng)
            xs, ys, _ = run_detect(m, g, rng, th, variant)
            for side, sign in (("l", -1), ("r", 1)):
                sel = side_of(xs, ys, sign)
                h = hough_rays(xs[sel], ys[sel])
                ok = bool(h and h[1] >= RAY_SUP and h[2] >= RAY_MIN)
                seq[side].append(h[0] if ok else np.nan)
        for side in ("l", "r"):
            v = np.array(seq[side])
            good = np.isfinite(v)
            d1 = np.diff(v[good])
            d2 = np.diff(d1)
            print(f"{th:.2f}  {side}   {good.sum():3d}   {np.nanstd(v):6.2f}°  "
                  f"{np.nanstd(d1):6.2f}°  {np.nanstd(d2):6.2f}°")
    print("（1阶含真实运动；2阶去掉匀速成分后才是抖动。缺失帧=该帧未识别到该侧射线）")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    sub.add_parser("sweep")
    s = sub.add_parser("rayfit")
    s.add_argument("--anchor", choices=("frozen", "est"), default="frozen")
    sub.add_parser("vpfit")
    sub.add_parser("horizon")
    for name in ("attrib", "jitter"):
        s = sub.add_parser(name)
        s.add_argument("--variant", choices=("abs", "rel", "mask"), default="mask")
        if name == "attrib":
            s.add_argument("--th", type=float, default=0.06)
    args = ap.parse_args()
    {"selftest": cmd_selftest, "sweep": cmd_sweep, "rayfit": cmd_rayfit,
     "vpfit": cmd_vpfit, "horizon": cmd_horizon,
     "attrib": cmd_attrib, "jitter": cmd_jitter}[args.cmd](args)


if __name__ == "__main__":
    main()
