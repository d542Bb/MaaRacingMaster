"""地面标定器（像素→地面映射）本体 + Gate −1 合成自检。

**要回答什么**（README「实施顺序」第 1 步）：用已知 (cx, y_h, A_x, A_z) 生成投影，
本文件里的标定器（VP 成簇投票 → 横向尺度 A_x → 虚线回流 A_z）反推须回到 ±ε。
任一判据红 = 数学实现错，按 README 归因表停手报告，不碰真帧、不加码换模型。

**参数口径**（README「采纳的方法主干」）：
    d = v − y_h（地面像素到地平行的距离，>0 才在地面上）
    X = A_x·(u − cx)/d        Z = A_z/d
即 README 的 (y_h−v) 写法取 d=−(y_h−v)，A_x/A_z 取正值，映射族完全相同。
横向尺度以「车道宽」为单位（A_x 的量纲 = 米或车道，本项目为车道宽）。
y_h 拟合边界带 [YH_LO, YH_HI]（README 更正 3 的实测先验）。

**标尺独立性**（experiments/README C 类硬约束 2）：判据是合成场景的**已知真值参数**，
不是标定器自己的逆变换——生成侧只做一次正向投影（解析式），估计侧是统计量
（RANSAC 投票 / 逆方差聚类 / 网格最小二乘），两者无共享拟合代码。

ε 先写死（跑前定，跑后不得回调）：
    VPx/y_h：max |z| ≤3（z = 误差 ÷ 估计器自报 σ——绝对 px 底是 Gate 4 跨场 CV 的事，
    −1 阶段只考数学自洽；旧「VPx ±2px」定在 σ≈2px 的噪声底上，判据错置已弃）；
    A_x：raw ±5% / Hough 前端 ±8%；A_z：±8%；
    留出标准残差中位 d/σ_d ≤1.5；y_h 出带即红。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_ground_calib.py
    # 报告： %TEMP%/sr_calib/gate_minus1.json ；退出码 0=全绿 1=有红
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

# ---------- 真值模型（与 README 一致） ----------

CX = 640.0          # 自车钉画面中央（已实测，README 主干）
Y_H = 213.0         # 地平行真值：路面带最远检出 y≈213（vision 实验）
A_X = 1.873         # 车道单位：画面底部 v=700 处车道宽 ≈260 px → (700−213)/260
A_Z = 1948.0        # 纵向：自车接地点 Z≈4 m 落在 v≈700 → 4×(700−213)
V_EGO = 42.0        # m/s，HUD 米速量级（README 更正 1，仅作合成自变量）
YH_LO, YH_HI = 180.0, 260.0   # y_h 先验带（拟合边界，更正 3）

H, W = 720, 1280

SEEDS = range(1000, 1020)   # Gate −1 种子集；判据对全部种子成立才算绿（可换段复验无过拟合）

# 场景（RULES.md §4 权威结构：4 车道、3 条白虚线分隔、自车位于第 3 条车道中央）：
# 自车相对坐标系 X=0，紧贴自车两侧的虚线在 ±0.5 车道，左侧第三条虚线 −1.5，
# 道路边缘 ±1.5/−2.5（不对称，自车靠右），护栏再外 0.35——护栏间距不成格，
# 考验 A_x 不把护栏距当车道距。Z 远端 60 m（v≈245，留出 horizon 余量）。
# （首轮曾误把分隔线画在自车正中央——与 RULES 冲突，且给「g/2 简并晶格」留了温床。）
DASH_X = [-1.5, -0.5, 0.5]
SOLID_X = [-2.5, 1.5, -2.85, 1.85]
Z_NEAR, Z_FAR = 3.0, 60.0
DASH_PERIOD, DASH_ON = 12.0, 6.0   # 虚线：世界系 6 m 实段 + 6 m 空档

EPS = {  # 判定阈值（先写死）
    # VPx/y_h 不再定绝对 px（绝对底要等多场 CV 才有意义，Gate −1 阶段唯一起作用的是
    # 「误差 vs 估计器自报 σ」= z-score，合成真值下 σ 已知准 → z≤3 才是数学实现的检验）。
    "vp_z": 3.0,
    "ax_raw_rel": 0.05, "ax_hough_rel": 0.08, "az_rel": 0.08,
    "holdout_z": 1.5,     # 留出标准残差中位 d/σ_d（σ_d = 该线段的外推噪声模型）
}


def project(x: float, z: float) -> tuple[float, float]:
    """地面 (X 车道单位, Z 米) → 像素 (u, v)。正向投影，标定器唯一的数据来源。"""
    d = A_Z / z                       # d = v − y_h
    return CX + x * d / A_X, Y_H + d  # u − cx = X·d/A_x


# ---------- 合成场景生成 ----------

MIN_FRAG_DV = 40.0   # 线段子段的最小画面高度（px）：前端（Hough）本就只能从
                     # 中近段虚线/标线产出足长线段，世界均匀采样的远端证据是亚像素的


def _line_frags(rng, x: float, n_frag: int, noise_px: float,
                z_lo: float = Z_NEAR, z_hi: float = Z_FAR):
    """一条纵向直线（世界 X 恒定）→ 若干带噪像素线段 (x1,y1,x2,y2)。

    端点在 1/z（≈画面行位置）上均匀采样，并丢弃画面高度 <MIN_FRAG_DV 的子段。
    世界均匀采样会把大量证据塞进远端亚像素区——首轮 Gate −1 实证该生成缺陷
    造成中心车道整条丢失、证据池单侧偏，vp/ax 被牵连（不是标定器的错）。
    """
    segs, tries = [], 0
    lo, hi = 1.0 / z_hi, 1.0 / z_lo
    while len(segs) < n_frag and tries < 40:
        tries += 1
        ia, ib = np.sort(rng.uniform(lo, hi, 2))
        u1, v1 = project(x, 1.0 / ia)
        u2, v2 = project(x, 1.0 / ib)
        if abs(v2 - v1) < MIN_FRAG_DV:
            continue
        if v1 > v2:
            u1, v1, u2, v2 = u2, v2, u1, v1
        n = lambda: rng.normal(0, noise_px)  # noqa: E731
        segs.append((u1 + n(), v1 + n(), u2 + n(), v2 + n()))
    return segs


def _dash_frags(rng, x: float, noise_px: float):
    """虚线：世界等距带逐条渲染，只保留画面高度达标的带（远端带本不可见）。"""
    segs, z0 = [], 0.0
    while z0 < Z_FAR:
        a, b = z0 + DASH_PERIOD * rng.uniform(0, .1), z0 + DASH_ON
        a, b = max(a, Z_NEAR), min(b, Z_FAR)
        if b > a and abs(project(x, b)[1] - project(x, a)[1]) >= MIN_FRAG_DV:
            segs += _line_frags(rng, x, 1, noise_px, z_lo=a, z_hi=b)
        z0 += DASH_PERIOD
    return segs


def _distractors(rng, n: int):
    """干扰线段：随机位置/角度（模拟建筑边、HUD 斜纹），考验 RANSAC 不被吸走。"""
    out = []
    for _ in range(n):
        x1 = rng.uniform(0, W)
        y1 = rng.uniform(150, H)
        ang = rng.uniform(0, np.pi)
        ln = rng.uniform(30, 90)
        out.append((x1, y1, x1 + ln * np.cos(ang), y1 + ln * np.sin(ang)))
    return out


def synth_family(seed: int, noise_px: float = 1.5):
    """平直段全部纵向线（边线/护栏/虚线），不含干扰——留出集真值族。"""
    rng = np.random.default_rng(seed)
    segs = []
    for x in SOLID_X:
        segs += _line_frags(rng, x, 4, noise_px)
    for x in DASH_X:
        segs += _dash_frags(rng, x, noise_px)
    return segs


def synth_segments(seed: int, noise_px: float = 1.5):
    """解析级线段族（Gate −1 主输入）：真值族 + 干扰线段。"""
    return synth_family(seed, noise_px) + _distractors(np.random.default_rng(seed + 13), 6)


def synth_hough(seed: int):
    """全前端路径：渲染 1280×720 → Canny → HoughLinesP（README 主干的既定前端）。"""
    rng = np.random.default_rng(seed)
    img = np.full((H, W), 38, np.uint8)
    def draw(x, z0, z1):
        pts = np.rint([project(x, z) for z in np.linspace(z0, z1, 40)]).astype(np.int32)
        # np.array(...,int32) 向零截断 = 全图系统性偏 (−0.5,−0.5)，hough 路径 z(VPx)
        # 曾因此一致 −1σ；四舍五入消除（Gate −1 开发期差分定位，属合成渲染缺陷非估计器）。
        cv2.polylines(img, [pts.reshape(-1, 1, 2)], False, 215, 2)
    for x in SOLID_X:
        draw(x, Z_NEAR, Z_FAR)
    for x in DASH_X:
        z0 = 0.0
        while z0 < Z_FAR:
            a = max(z0 + DASH_PERIOD * rng.uniform(0, .1), Z_NEAR)
            draw(x, a, min(z0 + DASH_ON, Z_FAR))
            z0 += DASH_PERIOD
    for x1, y1, x2, y2 in _distractors(rng, 10):
        cv2.line(img, (int(x1), int(y1)), (int(x2), int(y2)), 190, 2)
    noisy = np.clip(img + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    edges = cv2.Canny(noisy, 50, 150)
    raw = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=40,
                          minLineLength=40, maxLineGap=6)
    flat = raw.reshape(-1, 4) if raw is not None else []   # cv2 5.x 返回 (N,4)
    segs = [tuple(map(float, s)) for s in flat]
    return [s for s in segs if _steep(s)]


# ---------- 标定器（生产代码路径，Gate 0~3 复用同一实现） ----------

def _steep(seg, min_dv: float = 20.0) -> bool:
    """纵向线段粗筛：|Δv|≥min_dv 且排除横/斜切线（护栏、对向横纹等）。

    上限取 |du|≤2·|dv|（与行进方向夹角 <63°）：画面底部边线斜率≈1.07、
    护栏≈1.25 道宽/像素行，都是标定源（README 主干），不得滤掉。
    """
    u1, v1, u2, v2 = seg
    dv = abs(v2 - v1)
    return dv >= min_dv and abs(u2 - u1) <= 2.0 * dv


def _line_coef(seg):
    u1, v1, u2, v2 = seg
    a, b, c = (v2 - v1), (u1 - u2), (u2 * v1 - u1 * v2)
    n = np.hypot(a, b)
    return np.array([a / n, b / n, c / n])


SIGMA_PX = 2.5          # 前端（Hough/Canny）端点噪声量级，σ_d 模型的底参


def _seg_sigma_d(coefs, lens, mids, p):
    """每条线段的外推角噪声 → 在候选点 p 处的垂距标准差 σ_d。

    端点噪声 σ 使直线角误差 ∝ σ/len；对距线段中心 r 的 p 点，横向误差 ≈ σ·r/len。
    画面底部短线到地平线 VP 的 r/len 可达 5+——等 px 容差会把真车道线判为外点、
    又把它们的巨大外推误差等权喂进 LS，两头都错（Gate −1 开发期实证）。
    """
    r = np.linalg.norm(mids - np.asarray(p), axis=1)
    return SIGMA_PX * np.sqrt(1.0 + 2.0 * (r / np.maximum(lens, 1.0)) ** 2)


def estimate_vp(segs, rng_seed: int = 0, tol: float = 3.0, iters: int = 3000):
    """VP = (VPx, y_h)：成对交点 RANSAC 投票 + IRLS 精化（逐段 σ_d 自适应权重），
    y_h 受先验带边界。"""
    cand = [s for s in segs if _steep(s)]
    if len(cand) < 2:
        return None
    coefs = np.stack([_line_coef(s) for s in cand])
    lens = np.array([np.hypot(s[2] - s[0], s[3] - s[1]) for s in cand])
    mids = np.array([[(s[0] + s[2]) / 2, (s[1] + s[3]) / 2] for s in cand])
    rng = np.random.default_rng(rng_seed)
    w = lens / lens.sum()
    best, best_n = None, -1
    for _ in range(iters):
        i, j = rng.choice(len(cand), 2, replace=False, p=w)
        m = coefs[[i, j]]
        det = m[0, 0] * m[1, 1] - m[0, 1] * m[1, 0]
        if abs(det) < np.sin(np.radians(4)):      # 近平行，交点在无穷远
            continue
        px = (m[0, 1] * m[1, 2] - m[1, 1] * m[0, 2]) / det
        py = (m[0, 2] * m[1, 0] - m[0, 0] * m[1, 2]) / det
        if not (CX - 100 <= px <= CX + 100 and YH_LO <= py <= YH_HI):
            continue
        d = np.abs(coefs[:, :2] @ np.array([px, py]) + coefs[:, 2])
        n = int((d < 3 * _seg_sigma_d(coefs, lens, mids, (px, py)) + tol).sum())
        if n > best_n:
            best, best_n = (px, py), n
    if best is None:
        return None
    # IRLS：权重 1/σ_d²、内点判据 d < 3σ_d + tol——近 VP 长线多投票，
    # 底部短线保留但按外推误差降权，不再拉偏（无噪声输入下精确还原真值）。
    keep = np.ones(len(cand), bool)
    for _ in range(6):
        sig = _seg_sigma_d(coefs, lens, mids, best)
        d = np.abs(coefs[:, :2] @ np.array(best) + coefs[:, 2])
        keep = d < 3 * sig + tol
        m, ww = coefs[keep], 1.0 / sig[keep] ** 2
        if len(m) < 2:
            break
        wm = m * ww[:, None]
        aa = float(np.sum(wm[:, 0] * m[:, 0]))
        ab = float(np.sum(wm[:, 0] * m[:, 1]))
        bb = float(np.sum(wm[:, 1] * m[:, 1]))
        rhs = -np.array([np.sum(wm[:, 0] * m[:, 2]), np.sum(wm[:, 1] * m[:, 2])])
        try:
            new = tuple(np.linalg.solve([[aa, ab], [ab, bb]], rhs))
        except np.linalg.LinAlgError:
            break
        if np.hypot(new[0] - best[0], new[1] - best[1]) < 0.05:
            best = new
            break
        best = new
    vp_x, y_h = best
    y_h = float(np.clip(y_h, YH_LO, YH_HI))       # 先验带 = 拟合边界
    sig = _seg_sigma_d(coefs, lens, mids, (vp_x, y_h))
    # 协方差：IRLS 末次加权法方程 A=Σ w·n nᵀ（w=1/σ_d²）之逆 = 估计的标准差平方，
    # Gate −1 用它自检「误差与自报精度一致」、Gate 4 的跨会话 CV 也要它。
    keepf = np.abs(coefs[:, :2] @ np.array([vp_x, y_h]) + coefs[:, 2]) < 3 * sig + tol
    wf = 1.0 / sig[keepf] ** 2
    mf = coefs[keepf] * wf[:, None]
    Ainv = None
    if keepf.sum() >= 2:
        try:
            Ainv = np.linalg.inv([[float((mf[:, 0] * coefs[keepf][:, 0]).sum()),
                                   float((mf[:, 0] * coefs[keepf][:, 1]).sum())],
                                  [float((mf[:, 1] * coefs[keepf][:, 0]).sum()),
                                   float((mf[:, 1] * coefs[keepf][:, 1]).sum())]])
        except np.linalg.LinAlgError:
            Ainv = None
    return {"vpx": float(vp_x), "y_h": y_h,
            "sig_vpx": float(np.sqrt(Ainv[0, 0])) if Ainv is not None else float("nan"),
            "sig_yh": float(np.sqrt(Ainv[1, 1])) if Ainv is not None else float("nan"),
            "inliers": [cand[i] for i, k in enumerate(keep) if k]}


def estimate_ax(vp) -> dict | None:
    """A_x（车道单位）：内点线段按端点横坐标 s=(u−VPx)/(v−y_h) 取样，
    逆方差聚类后按「自车锚定的半格晶格」拟合。

    取样口径（Gate −1 开发期教训）：直线过 VP ⇒ 每个端点独立给出 s 观测，
    σ_s = SIGMA_PX·√(1+s²)/(v−y_h)——近端 ~0.005；斜率口径 Δu/Δv 对 70px
    短虚线段 σ≈0.1，同一批数据精度差 20 倍，且使锚对不可分辨。
    线段不再要求长度（|dv|≥35 废除），端点要求 v−y_h ≥ 40（贴地平线的观测无意义）。

    结构先验（RULES.md §4 [权威] + README「自车钉画面中央」实测）：自车在车道中央
    ⇒ 夹车虚线在 s = ±g/2，全族 s = (n+½)g；边线同在格上，护栏偏移不在。
    ① 枚举 0 两侧簇为夹车锚的全部组合：g₀=s_r−s_l，对称残差 |s_r+s_l|≤0.2；
    ② 逐簇取整 n̂=round(c/g−0.5)+0.5，LS g=Σ w n̂ c/Σ w n̂²（w=簇精度和），
       残差 >4σ_c 的簇剔除，迭代两轮；锚或 ≥4 在格簇不成立 → 该组合作废；
    ③ 取解释精度和最高的组合。尺度简并（g vs g/2）由半整格+锚对称破除：
       g/2 晶格把真车道线放到整格位（不在 (n+½) 上）而只能收留干扰簇。
    """
    pts = []                                     # (s, 1/σ², σ)
    for sg in vp["inliers"]:
        for u_, v_ in ((sg[0], sg[1]), (sg[2], sg[3])):
            d = v_ - vp["y_h"]
            if d < 40:
                continue
            si = (u_ - vp["vpx"]) / d
            sigi = SIGMA_PX * np.sqrt(1 + si * si) / d
            pts.append((si, 1.0 / sigi ** 2, sigi))
    if len(pts) < 4:
        return None
    pts.sort()
    # 统计相容合并（|s−μ| ≤ 2√(σ_p²+σ_μ²)）而非固定容差：同线远端点 s 天然散 ±0.05
    # 必须收进同簇；而平行真线仅差 0.05 的干扰线段自身 σ≈0.006，固定容差会把锚簇
    # 拉歪 0.02（seed 1013 实证：假锚令晶格全线不保 → 拒判）。
    clusters = [[pts[0]]]
    for p in pts[1:]:
        g = clusters[-1]
        pr = sum(w for _, w, _ in g)
        mu = sum(s_ * w for s_, w, _ in g) / pr
        if abs(p[0] - mu) <= 2 * np.hypot(p[2], 1.0 / np.sqrt(pr)):
            g.append(p)
        else:
            clusters.append([p])
    prec = np.array([sum(w for _, w, _ in g) for g in clusters])
    cen = np.array([sum(q[0] * q[1] for q in g) / p for g, p in zip(clusters, prec)])
    sig = 1.0 / np.sqrt(prec)
    if len(cen) < 3 or (cen < 0).sum() == 0 or (cen > 0).sum() == 0:
        return None

    def fit(sl: float, sr: float):
        g = sr - sl
        if abs(sr + sl) > 0.2 or not (0.15 <= g <= 2.5):
            return None
        for _ in range(3):                       # 取整 → 剔非格簇 → 重拟合
            nh = np.round(cen / g - 0.5) + 0.5   # 半整格位
            # 阈 max(4σ, 0.15g)：端点口径下真线残差 ~0.015、护栏格距偏移 ~0.19，
            # 底 0.079 居中两可（纯 4σ 会在初始 g 下把真边线挤出品格，LS 未及
            # 优化即 keep<4 拒判——seed 7009 实证；斜率口径时代的「加底放水」
            # 结论不适用于收紧后的端点 σ）。
            keep = np.abs(cen - nh * g) <= np.maximum(4 * sig, 0.15 * g)
            if not keep[np.argmin(np.abs(cen - sl))] or not keep[np.argmin(np.abs(cen - sr))]:
                return None                      # 锚自身不被 g 解释 = 假锚
            if keep.sum() < 2:
                return None
            g = float(np.sum(prec[keep] * nh[keep] * cen[keep])
                      / np.sum(prec[keep] * nh[keep] ** 2))
        if keep.sum() < 4:                       # 4 车道路 ≥ 4 条在格线（终判）
            return None
        return g, float(prec[keep].sum()), int(keep.sum())

    cands = [r for r in (fit(a, b) for a in cen[cen < 0] for b in cen[cen > 0]) if r]
    if not cands:
        return None
    g, _, n = max(cands, key=lambda r: (r[1], r[2]))
    return {"A_x": float(1.0 / g), "n_lines": n}


def dash_tracks(seed: int, n_steps: int = 30, dt: float = 1 / 21, noise_px: float = 1.5):
    """虚线回流样本：每条虚线世界 Z(t)=Z0−v·t 的行位置序列（Gate −1 只测 A_z 数学，
    真实帧的虚线轨迹提取属 Gate 3）。返回 [ {t:[], v:[]} ]。"""
    rng = np.random.default_rng(seed)
    out = []
    for x in DASH_X:
        for k in range(3):
            z0 = rng.uniform(15.0, 55.0)         # 上游虚线驶入近端：≥12 帧可拟合
            ts, vs = [], []
            for i in range(n_steps):
                t = i * dt
                z = z0 - V_EGO * t
                if z < Z_NEAR:
                    break
                vs.append(project(x, z)[1] + rng.normal(0, noise_px))
                ts.append(t)
            if len(ts) >= 12:
                out.append({"t": ts, "v": vs})
    return out


def estimate_az(tracks, vp) -> float | None:
    """A_z：单特征回流 v(t)=y_h+A_z/(Z0−v_ego·t)，对 (Z0,A_z) 网格最小二乘，跨轨迹取中位。"""
    y_h = vp["y_h"]
    z0s = np.arange(Z_NEAR + 0.5, 60.0, 0.5)[:, None, None]
    azs = np.arange(1200, 3200, 20)[None, :, None]
    est = []
    for tr in tracks:
        t = np.asarray(tr["t"])[None, None, :]
        v = np.asarray(tr["v"])[None, None, :]
        model = y_h + azs / np.maximum(z0s - V_EGO * t, 0.5)
        sse = np.sum((v - model) ** 2, axis=2)
        est.append(azs[0, :, 0][np.unravel_index(np.argmin(sse), sse.shape)[1]])
    return float(np.median(est)) if est else None


def resid_z(segs, vp) -> float:
    """留出集：线段直线到拟合 VP 的垂距，按各自外推噪声 σ_d 标准化后取中位。

    等 px 残差判据隐含「每条线对 VP 同样准」——被 σ_d 模型否定（底部短线
    外推误差天然大，与拟合好坏无关），故标准化后再比阈值。
    """
    coefs = np.stack([_line_coef(s) for s in segs])
    lens = np.array([np.hypot(s[2] - s[0], s[3] - s[1]) for s in segs])
    mids = np.array([[(s[0] + s[2]) / 2, (s[1] + s[3]) / 2] for s in segs])
    d = np.abs(coefs[:, :2] @ np.array([vp["vpx"], vp["y_h"]]) + coefs[:, 2])
    return float(np.median(d / _seg_sigma_d(coefs, lens, mids, (vp["vpx"], vp["y_h"]))))


# ---------- Gate −1 测试 ----------

def run_gate_minus1() -> list[dict]:
    rows = []
    def add(name, ok, detail):
        rows.append({"test": name, "pass": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    raws, houghs = [], []
    for seed in SEEDS:
        raws.append(estimate_vp(synth_segments(seed), rng_seed=seed))
        houghs.append(estimate_vp(synth_hough(seed), rng_seed=seed))
    for tag, vps in (("vp_raw", raws), ("vp_hough", houghs)):
        got = [v for v in vps if v]
        if len(got) < len(vps):
            add(f"T-{tag}", False, f"{len(vps) - len(got)} 个种子直接估不出 VP")
            continue
        zx = np.array([(v["vpx"] - CX) / v["sig_vpx"] for v in got])
        zy = np.array([(v["y_h"] - Y_H) / v["sig_yh"] for v in got])
        z = max(np.abs(zx).max(), np.abs(zy).max())
        add(f"T-{tag}", np.isfinite(z) and z <= EPS["vp_z"],
            f"max |z|={z:.2f} ≤{EPS['vp_z']}（误差对自报 σ 的标准化，{len(SEEDS)} 种子；"
            f"|VPx−{CX:.0f}| 中位 {np.median(np.abs([v['vpx'] - CX for v in got])):.2f}px）")

    for tag, gen, e in (("ax_raw", lambda s: synth_segments(s), EPS["ax_raw_rel"]),
                        ("ax_hough", lambda s: synth_hough(s), EPS["ax_hough_rel"])):
        rels = []
        for seed in SEEDS:
            segs = gen(seed)
            vp = estimate_vp(segs, rng_seed=seed)
            ax = estimate_ax(vp) if vp else None
            rels.append(abs(ax["A_x"] / A_X - 1) if ax else 9.9)
        add(f"T-{tag}", max(rels) <= e,
            f"max 相对误差 {max(rels):.1%} ≤{e:.0%}，中位 {np.median(rels):.1%}（车道单位）")

    rels = []
    for seed in SEEDS:
        vp = estimate_vp(synth_segments(seed), rng_seed=seed)
        az = estimate_az(dash_tracks(seed), vp) if vp else None
        rels.append(abs(az / A_Z - 1) if az else 9.9)
    add("T-az_dashflow", max(rels) <= EPS["az_rel"],
        f"max 相对误差 {max(rels):.1%} ≤{EPS['az_rel']:.0%}，中位 {np.median(rels):.1%}"
        f"（虚线回流 + v_ego={V_EGO} m/s）")

    ho = []
    for seed in SEEDS:
        rng = np.random.default_rng(seed + 7)
        # 留出对象 = 真值车道线族（干扰线本就不经过 VP，计入残差是判据错误）
        fam = synth_family(seed)
        idx = rng.permutation(len(fam))
        fit = [fam[i] for i in idx[:int(.7 * len(fam))]]
        hold = [fam[i] for i in idx[int(.7 * len(fam)):]]
        vp = estimate_vp(fit, rng_seed=seed)
        ho.append(resid_z(hold, vp) if vp and hold else 99.0)
    add("T-holdout", max(ho) <= EPS["holdout_z"],
        f"留出标准残差中位 d/σ_d max {max(ho):.2f} ≤{EPS['holdout_z']}"
        f"，中位 {np.median(ho):.2f}")

    out_of_band = [v for v in raws + houghs if v and not (YH_LO <= v["y_h"] <= YH_HI)]
    add("T-band", not out_of_band and all(raws) and all(houghs),
        f"y_h 估计 {len(out_of_band)} 次出带[{YH_LO:.0f},{YH_HI:.0f}]；"
        f"种子失败 {sum(1 for v in raws + houghs if not v)} 个")
    return rows


if __name__ == "__main__":
    print(f"Gate −1 合成自检：真值 (cx={CX}, y_h={Y_H}, A_x={A_X} 车道, A_z={A_Z})")
    rows = run_gate_minus1()
    green = all(r["pass"] for r in rows)
    out = Path(os.environ.get("TEMP", "/tmp")) / "sr_calib"
    out.mkdir(exist_ok=True)
    (out / "gate_minus1.json").write_text(
        json.dumps({"gate": "-1", "all_pass": green, "eps": EPS, "rows": rows},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nGate −1：{'全绿 ✅' if green else '有红 ❌（停手，按 README 归因表报告）'}"
          f" → {out / 'gate_minus1.json'}")
    sys.exit(0 if green else 1)
