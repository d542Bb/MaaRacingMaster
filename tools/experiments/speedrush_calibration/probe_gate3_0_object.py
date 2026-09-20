"""3R-0：白色车道分隔虚线的**对象身份**验证——只用视觉语义，禁用运动。

**目的（维护者裁定）**：先证明「仪器现在找的确实是白色车道分隔虚线」，
再谈动态。**对象定义只能来自目标本身的视觉语义**：
  白色 + 车道分隔线几何（陡、朝 VP）+ 断续结构 + 位于道路区域。
**禁止**用「它会向下流动」参与筛选——那是自证（假定运动→用运动筛→得运动）。

三条判据（阈值取自本帧像素色统计，非门限结果反推）：
- 白色：min(R,G,B)≥95 且 max−min≤35 且 均值≥105（黄线因缺蓝被排除）；
- 黄色（须显式排除）：(R+G)/2 − B ≥ 30 且 均值≥80；
- 断续（单帧空间判据）：沿段自身方向走，量测「连续白」行程；两端在 EXTEND
  像素内落到非白 → 该段是被间隙包围的**虚线段**；白行程顶到探测窗边缘 →
  **连续线**（排除，含连续白线与黄实线同类处理）。

输出（人眼可审查，非 precision/recall）：
- 标注帧：绿框=选中的白虚线，橙框=被排除的黄实线，蓝框=被排除的连续白线；
- 放大蒙太奇：选中虚线段 + 其 ±EXTEND 探测线 + 连续白行程区间（看得见「间隙」）；
- 跨帧同一对象：连续帧各自独立分类后叠框，供人确认「框确实套在白虚线上」。
- JSON：逐段颜色类、断续判定、连续白行程长度、选中/排除清单。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_gate3_0_object.py <session> [seq...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import cv2
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import CACHE, DEMOS, load_base, score_intervals, staged_filter
from probe_gate2_hist import get_cal
from probe_gate1_invariance import road_segs
import probe_gate3_az as g3

# ---------- 视觉语义阈值（取自本素材像素色统计，非结果反推）----------
WHITE_MIN = 95.0        # 白：三通道最小值下限（黄线 B 低、沥青整体低，均被挡）
WHITE_SPREAD = 35.0     # 白：通道最大-最小上限（近中性）
WHITE_MEAN = 105.0      # 白：亮度下限
YELLOW_BDEF = 30.0      # 黄：(R+G)/2 − B 下限（缺蓝）
YELLOW_MEAN = 80.0      # 黄：亮度下限
EXTEND = 60.0           # 断续探测：从段两端沿方向向外延伸的最大像素
RUN_EDGE = EXTEND - 8.0  # 白行程顶到探测窗边（留 8px 容差）即判连续（实心线）
ROAD_D_MIN = 50.0       # 道路区域：段中点在 y_h 下方至少这么多像素（近地平线不可分辨）
VP_CONV_DEG = 12.0      # 车道分隔线几何：段方向须指向 VP（与"中点→VP"方向夹角上限）
ROAD_IN_MARGIN = 18.0   # 道路区域：须在黄色右边界线内侧至少这么宽（护栏在黄线外侧）

# ---------- 候选生成前端（仅决定"候选段从哪来"，不改上面四条对象语义）----------
# Hough 前端复用标定同源 road_segs（minLineLength=40）。连通域前端：白掩膜极大
# 连通域的长轴段。因连通域已是"极大白区"，连续白线=一个横跨画面的大域，靠长度
# 上限在候选阶段即被丢弃（这是"断续 vs 连续"语义在新前端的忠实落地，非新增判据）。
CC_MIN_LEN = 30.0       # 长轴下限（噪声碎点，替代 Hough minLineLength 作用）
CC_MAX_LEN = 170.0      # 长轴上限（超过=连续线/结构，非虚线）
CC_MIN_AREA = 40.0      # 面积下限（碎点）

# ---------- B1：定向切分 + 共线合并（只换候选单元，不动 30/170 与四条语义）----------
# 核心：长度不稳（粘连/切碎），宽度稳。用「宽度带」做误检防火墙——只对长得像
# 车道线的细块动刀（切分/合并），粗块（车身、护栏板、宽边缘）原样送进四条语义，
# 行为与 B0 逐条一致。宽度带 w/laneW 取自真虚线亮核实测（≈0.013–0.016）与
# 粗块实测（≥0.056）之间的物理间隔，非门限结果反推。
W_LO, W_HI = 0.004, 0.045     # 准入宽度带：w/laneW ∈ 此区 → 视为「细·像虚线」
SPLIT_MARGIN = 25.0           # 切分：灰度剖面局部极小 < (WHITE_MEAN − 此值) 处下刀
G_FRAC = 0.06                 # 合并：轴向间隙 < G_FRAC·laneW（随深度缩放，非常数）
MERGE_ANG = 15.0              # 合并：与参考长段轴向夹角上限（度）
# 止血（任务0）：split 腿空载（要切的是实心线，无暗隙）、merge 腿过度合并吞相邻真虚线
# （seq599 id21 被并入 id20）。两腿默认关闭，代码保留；前端回退到「原始 CC + 四条语义
# + 30/170」。任务 3 修好合并判据后再按物理尺度重新开启。
SPLIT_ON = False
MERGE_ON = False

# ---------- B1.5：dash/solid 判别器（机械、可复现，人工不介入）----------
# 在原始 CC 上沿轴取灰度剖面，用 B1 同一间隙判据数间隙。间隙≥2 → dash_train；
# 间隙=0 且 L≥2 个虚线周期 → solid（边缘实线）；否则 unknown（短片段看不出周期性，
# 不得判 solid，以免把远处单段虚线误杀）。周期按 3m、车道 3.5m 折算 px_per_m。
DASH_PERIOD_M = 3.0           # 一个虚线周期（dash+gap）物理长度（米）
LANE_M = 3.5                  # 一个车道物理宽度（米）
GAP_MIN_PX = 3.0              # 连续多少像素的暗段才算一个真间隙（排噪点）


def _hough_candidates(rgb):
    return road_segs(rgb)


def _cc_candidates(rgb):
    """白掩膜连通域 → 每个域的长轴端点段（候选 segment）。"""
    white, _ = _masks(rgb)
    m = (white.astype(np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    cands = []
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < CC_MIN_AREA:
            continue
        ys, xs = np.where(lab == i)
        if len(xs) < CC_MIN_AREA:
            continue
        pts = np.column_stack([xs, ys]).astype(np.float32)
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        t = (pts - np.array([x0, y0])) @ np.array([vx, vy])
        tmin, tmax = t.min(), t.max()
        if (tmax - tmin) < CC_MIN_LEN or (tmax - tmin) > CC_MAX_LEN:
            continue
        p1 = np.array([x0, y0]) + tmin * np.array([vx, vy])
        p2 = np.array([x0, y0]) + tmax * np.array([vx, vy])
        cands.append((float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1])))
    return cands


def _split_streak(gray, x0, y0, ux, uy, tmin, tmax, gray_cut):
    """沿轴在灰度剖面局部极小且 < gray_cut 处下刀，返回子段 [(t_a,t_b), ...]。"""
    ts = np.arange(tmin, tmax + 1e-6, 2.0)
    prof = np.empty(len(ts))
    H, Wd = gray.shape
    for k, t in enumerate(ts):
        x = int(round(x0 + ux * t)); y = int(round(y0 + uy * t))
        x = min(max(x, 0), Wd - 1); y = min(max(y, 0), H - 1)
        prof[k] = gray[max(0, y - 1):y + 2, max(0, x - 1):x + 2].mean()
    cuts = [tmin]
    for k in range(1, len(ts) - 1):
        if prof[k] < gray_cut and prof[k] <= prof[k - 1] and prof[k] <= prof[k + 1]:
            if ts[k] - cuts[-1] >= CC_MIN_LEN * 0.5:   # 切点别太密
                cuts.append(ts[k])
    cuts.append(tmax)
    return [(cuts[j], cuts[j + 1]) for j in range(len(cuts) - 1)
            if cuts[j + 1] - cuts[j] >= 6.0]


def _cc_dashes(rgb, y_h, ax):
    """B1 候选单元：白连通域 → 细块按轴切分成长度合适的虚线段，再共线合并近邻碎片。

    只换候选表示（blob→定向虚线段），不改四条语义、不改 30/170。粗块（车身/护栏/
    宽边缘）不切不并，原样作为一段候选送进语义（与 B0 行为一致）。
    """
    white, _ = _masks(rgb)
    gray = rgb.mean(2)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(white.astype(np.uint8), 8)
    segs = []   # 每段: dict(p1,p2,ux,uy,L,laneW,thin)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < CC_MIN_AREA:
            continue
        cy = cent[i][1]
        if cy <= y_h + 20:
            continue
        ys, xs = np.where(lab == i)
        pts = np.column_stack([xs, ys]).astype(np.float32)
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        t = (pts - np.array([x0, y0])) @ np.array([vx, vy])
        tmin, tmax = float(t.min()), float(t.max())
        L = tmax - tmin
        laneW = (cy - y_h) / ax
        w = len(xs) / max(L, 1.0)
        thin = (w / max(laneW, 1.0)) >= W_LO and (w / max(laneW, 1.0)) <= W_HI
        if SPLIT_ON and thin and L > CC_MAX_LEN:
            for ta, tb in _split_streak(gray, x0, y0, vx, vy, tmin, tmax,
                                        WHITE_MEAN - SPLIT_MARGIN):
                p1 = np.array([x0, y0]) + ta * np.array([vx, vy])
                p2 = np.array([x0, y0]) + tb * np.array([vx, vy])
                segs.append({"p1": p1, "p2": p2, "ux": float(vx), "uy": float(vy),
                             "L": tb - ta, "laneW": laneW, "thin": True})
        else:
            p1 = np.array([x0, y0]) + tmin * np.array([vx, vy])
            p2 = np.array([x0, y0]) + tmax * np.array([vx, vy])
            segs.append({"p1": p1, "p2": p2, "ux": float(vx), "uy": float(vy),
                         "L": L, "laneW": laneW, "thin": bool(thin)})
    # 合并（任务0：默认关闭）：仅细段之间；以较长段轴为参考，轴向间隙 < G_FRAC·laneW 且共线
    if not MERGE_ON:
        merged = segs
    else:
        segs.sort(key=lambda s: -s["L"])
        used = [False] * len(segs)
        merged = []
        for i, a in enumerate(segs):
            if used[i] or not a["thin"]:
                merged.append(a); used[i] = True
                continue
            p1, p2, ux, uy, lw = a["p1"], a["p2"], a["ux"], a["uy"], a["laneW"]
            used[i] = True
            changed = True
            while changed:
                changed = False
                for j, b in enumerate(segs):
                    if used[j] or not b["thin"]:
                        continue
                    mid = (b["p1"] + b["p2"]) / 2
                    rel = mid - (p1 + p2) / 2
                    along = float(rel @ np.array([ux, uy]))
                    perp = float(rel @ np.array([-uy, ux]))
                    gap = abs(along) - (a["L"] + b["L"]) / 2
                    ang = abs(np.degrees(np.arctan2(b["uy"], b["ux"]) -
                                         np.arctan2(uy, ux)))
                    ang = min(ang, 180 - ang)
                    if (gap > 0 and gap < G_FRAC * lw and abs(perp) < 0.6 * max(lw * W_HI, 6.0)
                            and ang <= MERGE_ANG):
                        ta = float((b["p1"] - (p1 + p2) / 2) @ np.array([ux, uy])) - b["L"] / 2
                        tb = ta + b["L"]
                        c = (p1 + p2) / 2
                        e1 = c + min(float(-(a["L"]) / 2), ta) * np.array([ux, uy])
                        e2 = c + max(float(a["L"]) / 2, tb) * np.array([ux, uy])
                        p1, p2 = e1, e2
                        a = dict(a); a["p1"], a["p2"] = p1, p2; a["L"] = float(np.hypot(*(p2 - p1)))
                        used[j] = True; changed = True
            merged.append(a)
    # 30/170 原封不动作用在**新** axis_len 上（与 _cc_candidates 同一道长度窗）
    out = []
    for s in merged:
        p1, p2 = s["p1"], s["p2"]
        L = float(np.hypot(p2[0] - p1[0], p2[1] - p1[1]))
        if CC_MIN_LEN <= L <= CC_MAX_LEN:
            out.append((float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1])))
    return out


def _count_gaps(gray, x0, y0, ux, uy, tmin, tmax, gray_cut, step=2.0):
    """沿轴灰度剖面数间隙：连续 GAP_MIN_PX 以上低于 gray_cut 的暗段算一个间隙。"""
    ts = np.arange(tmin, tmax + 1e-6, step)
    H, Wd = gray.shape
    gaps = 0
    run = 0.0
    for t in ts:
        x = int(round(x0 + ux * t)); y = int(round(y0 + uy * t))
        x = min(max(x, 0), Wd - 1); y = min(max(y, 0), H - 1)
        g = gray[max(0, y - 1):y + 2, max(0, x - 1):x + 2].mean()
        if g < gray_cut:
            run += step
        else:
            if run >= GAP_MIN_PX:
                gaps += 1
            run = 0.0
    if run >= GAP_MIN_PX:
        gaps += 1
    return gaps


def _cc_geom(rgb, y_h, ax, a_z=4500.0):
    """枚举地平线下所有白色 CC（宽松），返回几何 + 间隙 + dash/solid 分类。

    物理长度用 v 区间积分 ΔZ = A_z·(1/d_min − 1/d_max)，d=v−y_h（M0-1 修正：深度只由
    v 决定，不吃长轴 L、不吃 u/VP/cosθ，自动积分段内尺度变化）。长轴 L 只用于宽长比。
    """
    white, _ = _masks(rgb)
    gray = rgb.mean(2)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(white.astype(np.uint8), 8)
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < CC_MIN_AREA:
            continue
        cy = cent[i][1]
        if cy <= y_h + 20:
            continue
        ys, xs = np.where(lab == i)
        pts = np.column_stack([xs, ys]).astype(np.float32)
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        t = (pts - np.array([x0, y0])) @ np.array([vx, vy])
        tmin, tmax = float(t.min()), float(t.max())
        L = tmax - tmin
        v_min, v_max = float(ys.min()), float(ys.max())
        d_min, d_max = v_min - y_h, v_max - y_h
        dz_m = a_z * (1.0 / d_min - 1.0 / d_max) if d_min > 1 else float("inf")
        laneW = (cy - y_h) / ax
        w = len(xs) / max(L, 1.0)
        gaps = _count_gaps(gray, x0, y0, vx, vy, tmin, tmax, WHITE_MEAN - SPLIT_MARGIN)
        solid_min = 2 * DASH_PERIOD_M * laneW / LANE_M   # 2 个虚线周期（米→px）
        if gaps >= 2:
            cls = "dash_train"
        elif gaps == 0 and L >= solid_min:
            cls = "solid"
        else:
            cls = "unknown"
        out.append({"sg": (float(x0 + vx * tmin), float(y0 + vy * tmin),
                           float(x0 + vx * tmax), float(y0 + vy * tmax)),
                    "um": float(x0), "vm": float(y0), "L": round(L), "w": round(w, 1),
                    "v_min": v_min, "v_max": v_max, "dz_m": round(dz_m, 2),
                    "laneW": round(laneW), "solid_min": round(solid_min),
                    "gaps": gaps, "cls": cls, "area": int(len(xs))})
    return out


def ledger(sess_name, seqs):
    """3R-0-B1.5 机械真值账：以 dash_train 为分母算虚线召回，dash/solid/unknown 计数。

    人工不介入。四条语义与 30/170 全程不动；判别器只用于「分类 CC + 定分母」，
    不塞进 dashed 语义。
    """
    base = load_base()
    rows = g3.analyze_series1(sess_name)
    rows = staged_filter(rows, base["base"], score_intervals(sess_name))
    cal = get_cal(sess_name, [r for r in rows if r["stage"] == 4])
    y_h, vpx, ax = cal["y_h"], cal["vpx"], cal["A_x_used"]
    sess = DEMOS / sess_name
    by_seq = {r["seq"]: r for r in rows}
    agg = {"dash_train": 0, "solid": 0, "unknown": 0, "dash_train_pass": 0}
    per = {}
    for s in seqs:
        r = by_seq.get(s)
        if not r:
            continue
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        white, _ = _masks(rgb)
        eL, eR = _fit_yellow_edges(rgb, y_h, vpx)
        ccs = _cc_geom(rgb, y_h, ax)
        c = {"dash_train": 0, "solid": 0, "unknown": 0, "dash_train_pass": 0}
        for cc in ccs:
            sem = _seg_semantics(rgb, white, cc["sg"], y_h, vpx, eL, eR)
            cc["passes"] = bool(sem["selected"])
            c[cc["cls"]] += 1
            if cc["cls"] == "dash_train" and cc["passes"]:
                c["dash_train_pass"] += 1
        for k in ("dash_train", "solid", "unknown", "dash_train_pass"):
            agg[k] += c[k]
        per[s] = {"ccs": ccs, **c}
        print(f"seq{s}: dash_train {c['dash_train']}(过{c['dash_train_pass']})  "
              f"solid {c['solid']}  unknown {c['unknown']}")
    rec = agg["dash_train"] / max(agg["dash_train"], 1)
    print(f"\n== 机械账（{len(seqs)} 帧）==")
    print(f"dash_train 总数 {agg['dash_train']}  其中通过四条语义 {agg['dash_train_pass']}  "
          f"→ 虚线召回 {agg['dash_train_pass']/max(agg['dash_train'],1):.0%}")
    print(f"solid（正确排除）{agg['solid']}   unknown（短片段，B2/上限域）{agg['unknown']}")
    (CACHE / f"gate3_0b15_ledger_{sess_name}.json").write_text(
        json.dumps({"agg": agg, "frames": {k: v["ccs"] for k, v in per.items()}},
                   ensure_ascii=False), encoding="utf-8")
    print(f"账 → {CACHE / ('gate3_0b15_ledger_' + sess_name + '.json')}")
    return agg, per


def _masks(rgb: np.ndarray):
    f = rgb.astype(float)
    r, gg, b = f[..., 0], f[..., 1], f[..., 2]
    mx = np.max(f, axis=2)
    mn = np.min(f, axis=2)
    mean = f.mean(axis=2)
    white = (mn >= WHITE_MIN) & ((mx - mn) <= WHITE_SPREAD) & (mean >= WHITE_MEAN)
    yellow = (((r + gg) / 2 - b) >= YELLOW_BDEF) & (mean >= YELLOW_MEAN)
    return white, yellow


def _seg_mean(rgb, sg):
    u1, v1, u2, v2 = sg
    n = int(max(abs(v2 - v1), abs(u2 - u1), 1))
    xs = np.clip(np.linspace(u1, u2, n).astype(int), 0, rgb.shape[1] - 1)
    ys = np.clip(np.linspace(v1, v2, n).astype(int), 0, rgb.shape[0] - 1)
    return rgb[ys, xs].astype(float).mean(0)


def _run_along(mask, sg, extend):
    """从段两端沿方向**向外**量测连续白行程（像素）。

    虚线段：白在端点外很快断掉（间隙），两端行程 < extend。
    连续线：白一直铺到探测窗边（行程顶到 extend）。
    从端点起走（非中点），避免中心单像素噪声翻转判定。
    """
    u1, v1, u2, v2 = sg
    du, dv = u2 - u1, v2 - v1
    ln = float(np.hypot(du, dv)) or 1.0
    ux, uy = du / ln, dv / ln
    H, W = mask.shape
    step = 2.0

    def outward(px, py, sign):
        t = step
        while t <= extend:
            x = int(round(px + sign * ux * t))
            y = int(round(py + sign * uy * t))
            if not (0 <= x < W and 0 <= y < H):
                return t
            if not mask[y, x]:
                return t
            t += step
        return extend
    return outward(u1, v1, -1.0), outward(u2, v2, 1.0)


def _fit_yellow_edges(rgb, y_h, vpx):
    """拟合道路左右两条黄色边界线 u = a·v + b。

    游戏事实：道路两侧皆为黄实线（左/右边缘），白虚线是内部分隔线。左右对称：
    均从地平线以下的**黄色掩膜像素**按 vpx 分侧做直线拟合（u=a·v+b）。用黄色像素，
    独立于白色虚线候选（不成闭环）。左边缘在远场收敛向 VP、近场扫出画面左外，
    故近处左虚线不被误杀、远处静态左边缘残片落在界外被排除。返回 (left, right)。
    """
    _, yellow = _masks(rgb)
    ys, xs = np.where(yellow)
    keep = ys > y_h + 20
    ys, xs = ys[keep], xs[keep]
    if len(ys) < 4:
        return None, None

    def fit(sel):
        if sel.sum() < 20:
            return None
        a, b = np.polyfit(ys[sel], xs[sel], 1)
        return float(a), float(b)
    return fit(xs < vpx), fit(xs >= vpx)


def _seg_semantics(rgb, white, sg, y_h, vpx, eL, eR):
    """对单个候选段求四条语义（白/断续/朝VP收敛/道路区域）+ 颜色类。纯单帧视觉语义。

    classify 与 decompose 共用此函数，保证「归因」用的判据与「选择」用的判据逐字一致。
    """
    u1, v1, u2, v2 = sg
    um, vm = (u1 + u2) / 2, (v1 + v2) / 2
    d = vm - y_h
    W = rgb.shape[1]
    left_edge = (eL[0] * vm + eL[1]) if eL else 0.0
    right_edge = (eR[0] * vm + eR[1]) if eR else float(W)
    m = _seg_mean(rgb, sg)
    in_road = (d >= ROAD_D_MIN
               and um >= left_edge + ROAD_IN_MARGIN
               and um <= right_edge - ROAD_IN_MARGIN)
    is_white = (float(m.min()) >= WHITE_MIN
                and (m.max() - m.min()) <= WHITE_SPREAD
                and float(m.mean()) >= WHITE_MEAN)
    is_yellow = (float((m[0] + m[1]) / 2 - m[2]) >= YELLOW_BDEF
                 and float(m.mean()) >= YELLOW_MEAN)
    rb, rf = _run_along(white, sg, EXTEND) if is_white else (0.0, 0.0)
    dashed = is_white and rb < RUN_EDGE and rf < RUN_EDGE
    seg_ang = np.degrees(np.arctan2(v2 - v1, u2 - u1))
    vp_ang = np.degrees(np.arctan2(y_h - vm, vpx - um))
    da = abs((seg_ang - vp_ang + 90.0) % 180.0 - 90.0)   # 折叠到 [0,90]（线无向）
    converges = da <= VP_CONV_DEG
    return {"sg": [float(u1), float(v1), float(u2), float(v2)],
            "um": um, "vm": vm, "d": d,
            "rgb": [round(float(x), 1) for x in m],
            "color": "white" if is_white else ("yellow" if is_yellow else "other"),
            "run_back": round(rb, 1), "run_fwd": round(rf, 1),
            "is_white": bool(is_white), "is_yellow": bool(is_yellow),
            "dashed": bool(dashed), "cont_white": bool(is_white and not dashed),
            "conv_deg": round(float(da), 1), "converges": bool(converges),
            "left_edge_u": round(float(left_edge), 1), "right_edge_u": round(float(right_edge), 1),
            "in_road": bool(in_road),
            "selected": bool(is_white and dashed and in_road and converges)}


def classify(rgb, y_h, vpx, frontend="hough", ax=0.563):
    """逐候选段分类：白 + 断续 + 朝VP收敛 + 位于道路区域。仅单帧视觉语义。

    frontend: "hough"（标定同源陡段）/ "cc"（白连通域长轴）/ "dash"（B1：定向切分+
    共线合并后的虚线段）。三者仅决定候选来源，四条对象语义完全一致。
    """
    white, yellow = _masks(rgb)
    eL, eR = _fit_yellow_edges(rgb, y_h, vpx)
    if frontend == "cc":
        cands = _cc_candidates(rgb)
    elif frontend == "dash":
        cands = _cc_dashes(rgb, y_h, ax)
    else:
        cands = road_segs(rgb)
    out = []
    for sg in cands:
        r = _seg_semantics(rgb, white, sg, y_h, vpx, eL, eR)
        r["excl_yellow"] = bool(r["is_yellow"] and r["d"] >= ROAD_D_MIN)
        r["excl_cont_white"] = bool(r["cont_white"] and r["in_road"])
        r["excl_outside_road"] = bool(r["is_white"] and r["dashed"] and r["converges"] and not r["in_road"])
        out.append(r)
    return out


def annotate(rgb_img, recs, title, path, edges=None):
    dr = ImageDraw.Draw(rgb_img)
    if edges is not None:                       # 拟合道路左右黄边界线（道路区域参考，橙）
        eL, eR = edges
        v0, v1 = 300, rgb_img.height - 1
        for e in (eL, eR):
            if e:
                dr.line([(e[0] * v0 + e[1], v0), (e[0] * v1 + e[1], v1)],
                        fill=(255, 140, 0), width=2)
    for rc in recs:
        u1, v1, u2, v2 = rc["sg"]
        if rc["selected"]:
            box = (min(u1, u2) - 6, min(v1, v2) - 6, max(u1, u2) + 6, max(v1, v2) + 6)
            dr.rectangle(box, outline=(0, 255, 0), width=3)
        else:                                  # 前端产出但被语义判据拒绝（紫，细）
            dr.line([(u1, v1), (u2, v2)], fill=(200, 0, 200), width=2)
    dr.rectangle([(0, 0), (rgb_img.width, 22)], fill=(0, 0, 0))
    dr.text((4, 6), title, fill=(255, 255, 255))
    rgb_img.save(path)


def run(sess_name: str, seqs: list[int], frontend: str = "hough"):
    base = load_base()
    rows = g3.analyze_series1(sess_name)
    rows = staged_filter(rows, base["base"], score_intervals(sess_name))
    race = [r for r in rows if r["stage"] == 4]
    cal = get_cal(sess_name, race)
    y_h, vpx, ax = cal["y_h"], cal["vpx"], cal["A_x_used"]
    sess = DEMOS / sess_name
    by_seq = {r["seq"]: r for r in rows}
    report = {}
    for s in seqs:
        r = by_seq.get(s)
        if not r:
            continue
        img = Image.open(sess / "frames" / r["file"]).convert("RGB")
        rgb = np.array(img)
        recs = classify(rgb, y_h, vpx, frontend, ax)
        sel = [x for x in recs if x["selected"]]
        rej = [x for x in recs if not x["selected"]]
        annotate(img, recs,
                 f"3R-0[{frontend}] {sess_name} seq{s}  绿=选中白虚线{len(sel)}  "
                 f"紫=前端候选被语义拒绝{len(rej)}  橙=拟合道路左右黄边界线",
                 CACHE / f"gate3_0_annot_{frontend}_{sess_name}_{s}.png",
                 edges=_fit_yellow_edges(rgb, y_h, vpx))
        report[s] = {"n_cand": len(recs), "n_selected": len(sel), "n_rejected": len(rej),
                     "selected": sel, "rejected": rej}
        print(f"[{frontend}] seq{s}: 候选{len(recs)} 选中白虚线{len(sel)} 拒绝{len(rej)}")
    (CACHE / f"gate3_0_object_{frontend}_{sess_name}.json").write_text(
        json.dumps({"y_h": y_h, "vpx": vpx, "frontend": frontend, "frames": report},
                   ensure_ascii=False), encoding="utf-8")
    print(f"报告 → {CACHE / ('gate3_0_object_' + frontend + '_' + sess_name + '.json')}")
    return report


def _cc_segments_all(rgb, y_h):
    """所有白色连通域长轴段（宽松：仅 area/地平线下，**不施长度窗**）。用于漏检归因。"""
    white, _ = _masks(rgb)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(white.astype(np.uint8), 8)
    segs = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < CC_MIN_AREA:
            continue
        cy = cent[i][1]
        if cy <= y_h + 20:
            continue
        ys, xs = np.where(lab == i)
        pts = np.column_stack([xs, ys]).astype(np.float32)
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        t = (pts - np.array([x0, y0])) @ np.array([vx, vy])
        tmin, tmax = t.min(), t.max()
        p1 = np.array([x0, y0]) + tmin * np.array([vx, vy])
        p2 = np.array([x0, y0]) + tmax * np.array([vx, vy])
        segs.append(((float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1])),
                     float(tmax - tmin)))
    return segs


def _death_stage(sg, L, sem):
    """按流水线顺序返回该 CC 的死因阶段（不改任何阈值，仅归因）。"""
    if L < CC_MIN_LEN:
        return "len<min"
    if L > CC_MAX_LEN:
        return "len>max"
    if not sem["is_white"]:
        return "white"
    if not sem["dashed"]:
        return "dashed"
    if not sem["converges"]:
        return "converge"
    if not sem["in_road"]:
        return "road"
    return "PASS"


STAGE_COLOR = {"PASS": (0, 255, 0), "len<min": (255, 60, 60), "len>max": (255, 150, 0),
               "white": (80, 140, 255), "dashed": (255, 0, 255),
               "converge": (0, 255, 255), "road": (255, 255, 0)}


def decompose(sess_name, seqs):
    """3R-0-B0 漏检归因：对每个白色 CC 标注其死因阶段 + 编号，出人眼可核的编号图与 JSON。

    不改任何参数。人工在编号图上指出「哪些编号是真白虚线」，即可统计真值对象各级死亡数；
    另行人工记录「真虚线但无任何 CC 覆盖」= CC生成 级损失。
    """
    base = load_base()
    rows = g3.analyze_series1(sess_name)
    rows = staged_filter(rows, base["base"], score_intervals(sess_name))
    cal = get_cal(sess_name, [r for r in rows if r["stage"] == 4])
    y_h, vpx = cal["y_h"], cal["vpx"]
    sess = DEMOS / sess_name
    by_seq = {r["seq"]: r for r in rows}
    out = {}
    for s in seqs:
        r = by_seq.get(s)
        if not r:
            continue
        img = Image.open(sess / "frames" / r["file"]).convert("RGB")
        rgb = np.array(img)
        white, _ = _masks(rgb)
        eL, eR = _fit_yellow_edges(rgb, y_h, vpx)
        dr = ImageDraw.Draw(img)
        items = []
        for idx, (sg, L) in enumerate(_cc_segments_all(rgb, y_h)):
            sem = _seg_semantics(rgb, white, sg, y_h, vpx, eL, eR)
            stage = _death_stage(sg, L, sem)
            u1, v1, u2, v2 = sg
            col = STAGE_COLOR[stage]
            dr.line([(u1, v1), (u2, v2)], fill=col, width=3)
            dr.text((min(u1, u2), min(v1, v2) - 12), str(idx), fill=col)
            items.append({"id": idx, "um": round(sem["um"]), "vm": round(sem["vm"]),
                          "d": round(sem["d"]), "len": round(L), "stage": stage})
        img.save(CACHE / f"gate3_0b0_{sess_name}_{s}.png")
        out[s] = items
        from collections import Counter
        print(f"seq{s}: CC {len(items)}  阶段分布 {dict(Counter(x['stage'] for x in items))}")
    (CACHE / f"gate3_0b0_{sess_name}.json").write_text(
        json.dumps({"y_h": y_h, "vpx": vpx, "frames": out}, ensure_ascii=False),
        encoding="utf-8")
    print(f"归因 → {CACHE / ('gate3_0b0_' + sess_name + '.json')}")
    return out


if __name__ == "__main__":
    sn = sys.argv[1] if len(sys.argv) > 1 else "20260919_211329_p2"
    rest = sys.argv[2:]
    sqs = [int(x) for x in rest if x.isdigit()]
    mode = next((x for x in rest if not x.isdigit()), "hough")
    if mode == "ledger":
        ledger(sn, sqs or [2, 19, 551, 599])
    elif mode == "decompose":
        decompose(sn, sqs or [2, 19, 551, 599])
    else:
        run(sn, sqs or [77], mode)
