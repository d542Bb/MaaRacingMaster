# -*- coding: utf-8 -*-
"""金标 v2：免标定直线族横位（不碰生产代码，不改旧探针）。

**为什么不用旧探针的口径**：旧版把每条黄线相对**假定的**消失点 VPX=640.7 与
地平线 Y_H=354.2 求斜率反解偏移，标定常数一旦偏了就是一条系统偏差，而且它
与生产共用 cam_h/LANE_W——用它当裁判，等于让被测方参与制定尺子。

**本版只用一条世界事实**：同一条路上所有车道标线互相平行，相邻线横向相隔
1 个道宽。地面直线在针孔图上的投影满足

    x_px(y) = vp + (k - x) * s * (y - y_h),   s = 车道宽/相机高（像素斜率）

即每条线都是直线 ``x_px = a*y + b``，斜率 ``a = (k - x) * s``。于是

* 相邻两条线斜率差恒为 ``s``（与自车在哪、与标定无关）→ **s 由数据自标定**；
* 左右两条黄路缘的斜率符号相反（``a=(k-x)s``，k=∓2 时符号恒定）→ **侧别由斜率
  符号定，不需要 VPX**；
* 自车横位 ``x = k - a/s``，取各线中值。

全程不需要 VPX / Y_H / focal / cam_h / 米制道宽。

**自检**（金标自己要先过验收）：
  ``x_lo`` / ``x_hi``  下半幅与上半幅分别求解的横位差——弯道、误聚类、非平面
                       路面都会把它顶开；差超阈值即整帧作废。
  ``s_spread``        相邻斜率差的离散度——s 不稳说明线族分错。
  ``n_yellow``        参与求解的黄缘条数（0=无绝对锚，只能给相对量）。
  ``width``           两条黄缘之间的道宽估计，用来发现"这段路不是四车道"。

输出：``<out>/gold2_<session>.jsonl`` 逐帧一行；``--overlay`` 时另出叠图。
"""
from __future__ import annotations

import argparse
import json
import math
from itertools import combinations
from pathlib import Path

import cv2
import numpy as np

ROWS = (400, 720)          # 只用地面：400 以上含 HUD/天空/远处非路面
ROW_STEP = 2
MIN_RUN_PX = 2             # 一行里连续黄/白像素段的最短长度
MAX_RUN_PX = 60
FAMILY_INLIER_TOL = 3.0    # px，到线的法向距离容差
FAMILY_MIN_ROWS = 10
FAMILY_MIN_SPAN = 60       # 行跨度（px）
FILL_MIN_YELLOW = 0.55     # 黄缘是实线：内点行要占满跨度，字符串金币不满足
CONTRAST_MIN = 35          # 标线必须比紧邻暗侧亮这么多
ASPHALT_V_MAX = 150        # 暗侧亮度上限：超过说明两侧都不是路面
N_FAMILIES = 6
CURB_K = 2.0               # 四车道：路缘在第 ±2 道
X_PHYS = 2.2               # 车不可能开出路面：解出 |x|>2.2 即为金标自己错了
X_SPLIT_MAX = 0.25         # 上下半幅横位差上限（道）
X_AGREE_MAX = 0.25         # 单族反解横位与中值的最大偏差（道）
GRID_RES_MAX = 0.20        # 线族落格残差均值上限：超过说明本帧的 s 不对
VP_TOL = 45.0              # 共消失点门：该族与其余线的交点离公共点多远算离群
PAIR_GAP_MAX = 0.45        # 左右两缘各自反解的横位差上限（两块独立像素互证）
MERGE_DA = 0.25            # 合并两族的斜率差上限
MERGE_DX = 10.0            # 合并两族在第 560 行的横向距离上限


def _masks(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """黄缘 / 白标线两张掩码，只留路面且**比两侧明显更亮**的段。

    局部对比度门是必需的：护栏板是浅灰（低饱和高亮度），板缝在图上就是竖直
    白线——不加这道门，线族里会混进一堆与地面无关的"线"，把 s 与道号一起带偏
    （首版实测 12 帧里 8 帧因 s 离散度作废，全是它）。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    yellow = (h >= 18) & (h <= 40) & (s >= 150) & (v >= 140)
    white = (s < 75) & (v >= 125)
    cut = ROWS[0]
    yellow[:cut] = False
    white[:cut] = False
    vv = v.astype(np.int16)
    return _contrast(yellow, vv), _contrast(white, vv)


def _contrast(mask: np.ndarray, vv: np.ndarray) -> np.ndarray:
    """标线门：比两侧亮 **且** 至少一侧是沥青。

    只加"比两侧亮"不够——护栏顶缘、护栏板缝都满足；加上"必须紧贴暗路面"才
    把它们排除（实测右护栏顶在图上就是一条与黄缘几乎平行的浅亮线）。"""
    out = np.zeros_like(mask)
    H, W = mask.shape
    for y in range(ROWS[0], min(H, ROWS[1]), ROW_STEP):
        for xc, w in _runs(mask[y]):
            x0 = max(0, int(xc - w / 2))
            x1 = min(W - 1, int(xc + w / 2))
            if x1 <= x0:
                continue
            lw = vv[y, max(0, x0 - 14):max(0, x0 - 5)]
            rw = vv[y, min(W, x1 + 6):min(W, x1 + 15)]
            if len(lw) < 3 and len(rw) < 3:
                continue
            lm = int(np.median(lw)) if len(lw) >= 3 else 255
            rm = int(np.median(rw)) if len(rw) >= 3 else 255
            if min(lm, rm) > ASPHALT_V_MAX:
                continue
            if int(vv[y, x0:x1 + 1].mean()) - min(lm, rm) >= CONTRAST_MIN:
                out[y, x0:x1 + 1] = True
    return out


def _runs(row: np.ndarray) -> list[tuple[float, int]]:
    idx = np.flatnonzero(row)
    if len(idx) == 0:
        return []
    splits = np.flatnonzero(np.diff(idx) > 3) + 1
    out = []
    for g in np.split(idx, splits):
        w = int(g[-1] - g[0] + 1)
        if MIN_RUN_PX <= w <= MAX_RUN_PX:
            out.append((float(g.mean()), w))
    return out


def _points(yellow: np.ndarray, white: np.ndarray) -> list[dict]:
    pts = []
    for y in range(ROWS[0], min(ROWS[1], yellow.shape[0]), ROW_STEP):
        for color, m in (("y", yellow), ("w", white)):
            for xc, w in _runs(m[y]):
                pts.append({"y": float(y), "x": xc, "w": w, "c": color})
    return pts


def _fit_family(pts: list[dict], seed: list[dict]) -> dict | None:
    """两点定线 + 内点迭代重拟合（一次最小二乘收敛即可）。"""
    inl = seed
    for _ in range(3):
        if len(inl) < 2:
            return None
        ys = np.array([q["y"] for q in inl])
        xs = np.array([q["x"] for q in inl])
        if ys.max() - ys.min() < 20:
            return None
        a, b = np.polyfit(ys, xs, 1)
        d = np.abs(xs - (a * ys + b))
        keep = [q for q, dd in zip(inl, d) if dd <= FAMILY_INLIER_TOL]
        if len(keep) < 2:
            return None
        inl = keep
    ys = np.array([q["y"] for q in inl])
    xs = np.array([q["x"] for q in inl])
    a, b = np.polyfit(ys, xs, 1)
    rms = float(np.sqrt(np.mean((xs - (a * ys + b)) ** 2)))
    cols = [q["c"] for q in inl]
    yshare = sum(1 for c in cols if c == "y") / len(cols)
    fill = len(set(ys.tolist())) / ((ys.max() - ys.min()) / ROW_STEP + 1)
    return {"a": float(a), "b": float(b), "rms": rms, "n": len(inl),
            "y0": float(ys.min()), "y1": float(ys.max()),
            "fill": round(float(fill), 2),
            "yellow": round(yshare, 2),
            "pts": [(float(q["y"]), float(q["x"]), q["c"]) for q in inl]}


def _extract_families(pts: list[dict]) -> list[dict]:
    """RANSAC 逐条抽线族：每次取内点最多的一条，摘掉再抽下一条。

    内点计数走 numpy（逐帧候选上千个，纯 Python 循环实测 2.7s/帧）。"""
    ys = np.array([q["y"] for q in pts])
    xs = np.array([q["x"] for q in pts])
    alive = np.ones(len(pts), dtype=bool)
    fams = []
    for _ in range(N_FAMILIES):
        idx = np.flatnonzero(alive)
        if len(idx) < FAMILY_MIN_ROWS:
            break
        ay, ax = ys[idx], xs[idx]
        order = np.argsort(ay, kind="stable")
        ay, ax = ay[order], ax[order]
        rowsy = np.unique(ay)
        if len(rowsy) < 8:
            break
        best_n, best_ab = 0, None
        for i in range(0, len(rowsy) - 12, 4):
            for qa in ax[ay == rowsy[i]]:
                for qb in ax[ay == rowsy[i + 12]]:
                    a = (qb - qa) / (rowsy[i + 12] - rowsy[i])
                    if abs(a) > 6.0:
                        continue
                    b = qa - a * rowsy[i]
                    n = int(np.count_nonzero(np.abs(ax - (a * ay + b)) <= 2.0))
                    if n > best_n:
                        best_n, best_ab = n, (a, b)
        if best_ab is None or best_n < FAMILY_MIN_ROWS:
            break
        a0, b0 = best_ab
        seed = [dict(y=float(qy), x=float(qx), c=pts[j]["c"])
                for j, qy, qx in zip(idx, ay, ax)
                if abs(qx - (a0 * qy + b0)) <= 2.0]
        f = _fit_family(pts, seed)
        if f is None:
            break
        alive[idx[np.abs(ax - (f["a"] * ay + f["b"])) <= 4.0]] = False
        # 黄缘是实线：内点行必须几乎铺满跨度（一串金币/断续噪声不算）
        need = FILL_MIN_YELLOW if f["yellow"] >= 0.6 else 0.25
        if f["y1"] - f["y0"] < FAMILY_MIN_SPAN or f["n"] < FAMILY_MIN_ROWS \
                or f["fill"] < need:
            continue
        fams.append(f)
    return fams


def _merge(fams: list[dict]) -> list[dict]:
    """同一条线被虚线/明暗切成两族时合并（否则两族间距会把 s 拉小一半）。"""
    fams = sorted(fams, key=lambda f: f["a"])
    out: list[dict] = []
    for f in fams:
        if out:
            g = out[-1]
            dx = abs((f["a"] - g["a"]) * 560.0 + (f["b"] - g["b"]))
            if abs(f["a"] - g["a"]) <= MERGE_DA and dx <= MERGE_DX:
                pts = [{"y": y, "x": x, "c": c}
                       for (y, x, c) in g["pts"] + f["pts"]]
                m = _fit_family(pts, pts)
                if m is not None:
                    out[-1] = m
                    continue
        out.append(dict(f))
    return out


def _try_s(fams: list[dict], s: float):
    """给定 s，解一次「道号—残差—颜色」自洽性。

    s 必须来自局级标定而不是本帧猜测：半道宽歧义解在单帧像素上同样自洽，只有
    跨帧常量 + 白线落格能把它挤掉。落格残差 ``grid_res`` 就是本帧对 s 的复核。"""
    cx = _curb_x(fams, s)
    if cx is None:
        return None
    x0, n_sides, gap = cx
    kept: dict[int, tuple[dict, float]] = {}
    res = []
    for f in fams:
        kf = x0 + f["a"] / s            # 落格残差：这条线离整数道号多远
        k = int(round(kf))
        if abs(k) > CURB_K:
            continue
        res.append(abs(kf - k))
        xi = k - f["a"] / s
        if abs(xi - x0) > X_AGREE_MAX:
            continue
        if k in kept and kept[k][0]["n"] >= f["n"]:
            continue
        kept[k] = (f, xi)
    if len(kept) < 2:
        return None
    viol = sum(1 for k, (f, _) in kept.items()
               if (abs(k) == CURB_K) != (f["yellow"] >= 0.6))
    x = float(np.median([xi for _, xi in kept.values()]))
    rmax = max(abs(xi - x) for _, xi in kept.values())
    return {"s": s, "x": x, "viol": viol, "rmax": rmax, "n": len(kept),
            "sides": n_sides, "gap": gap,
            "grid_res": round(float(np.mean(res)), 3) if res else None,
            "kept": kept}


def _vp_hits(fams: list[dict]) -> list[tuple[float, float]]:
    pts = []
    for f0, f1 in combinations(fams, 2):
        d = f0["a"] - f1["a"]
        if abs(d) < 0.05:
            continue
        x = (f1["b"] - f0["b"]) / d
        pts.append((x, f0["a"] * x + f0["b"]))
    return pts


def _vp_of(pts: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not pts:
        return None
    vx = float(np.median([p[0] for p in pts]))
    vy = float(np.median([p[1] for p in pts]))
    if not (-2000 <= vx <= 3200 and 100 <= vy <= 900):
        return None
    return vx, vy


def _fam_vp_dist(f: dict, others: list[dict], vp: tuple[float, float]) -> float:
    """该族与其余各族的交点，到公共消失点的距离中值（沿线的法向）。"""
    dd = []
    for g in others:
        d = f["a"] - g["a"]
        if abs(d) < 0.05:
            continue
        x = (g["b"] - f["b"]) / d
        dd.append(math.hypot(x - vp[0], f["a"] * x + f["b"] - vp[1]))
    return float(np.median(dd)) if dd else 0.0


def _vp_gate(fams: list[dict]) -> tuple[list[dict], tuple[float, float] | None]:
    """只留与**公共消失点**一致的线。

    路面标线在三维里互相平行，投影必共点；而路面上的阴影边界、自车车身边缘、
    横贯路面的亮带边都不满足——这条判据是几何的，不靠颜色与亮度调参，正好治
    首版实测到的三类假线（阴影边界与白线一样"比两侧亮"，颜色门拦不住）。
    只剩一条线时无法定消失点：本帧弃权，不猜。"""
    cur = list(fams)
    while len(cur) >= 3:
        pts = _vp_hits(cur)
        vp = _vp_of(pts)
        if vp is None:
            return [], None
        bad = [f for f in cur
               if _fam_vp_dist(f, [g for g in cur if g is not f], vp) > VP_TOL]
        if not bad:
            return cur, vp
        cur.remove(min(bad, key=lambda f: f["n"]))   # 先摘证据最少的那条
    if len(cur) < 2:
        return [], None
    return cur, _vp_of(_vp_hits(cur))


def _solve(fams: list[dict], s_cal: float | None) -> dict:
    """局级 s + 共消失点门 → 横位。"""
    fams = _merge(fams)
    out = {"s": s_cal, "x": None, "x_lo": None, "x_hi": None, "width": None,
           "split": None, "grid_res": None, "n_yellow": 0, "n_fam": len(fams),
           "why": ""}
    fams, vp = _vp_gate(fams)
    out["vp"] = None if vp is None else [round(vp[0], 1), round(vp[1], 1)]
    out["n_fam"] = len(fams)
    if len(fams) < 2:
        out["why"] = "fam<2"
        return out
    fams = sorted(fams, key=lambda f: f["a"])
    curbs = [f for f in fams if f["yellow"] >= 0.6]
    out["n_yellow"] = len(curbs)
    if len(curbs) >= 2:
        out["width"] = round(abs(curbs[-1]["a"] - curbs[0]["a"]) / s_cal, 2) \
            if s_cal else None
    if s_cal is None:
        out["why"] = "no-s"
        return out
    r = _try_s(fams, s_cal)
    if r is None:
        out["why"] = "no-fit"
        return out
    kept = r["kept"]
    out["x"] = round(r["x"], 3)
    out["viol"] = r["viol"]
    out["rmax"] = round(r["rmax"], 3)
    out["grid_res"] = r["grid_res"]
    out["n_used"] = r["n"]
    out["sides"] = r["sides"]
    out["pair_gap"] = None if r["gap"] is None else round(r["gap"], 3)
    out["grid"] = sorted(k for k in kept)
    for f in fams:
        f["used"] = any(f is k0 for k0, _ in kept.values())
    for k, (f, _) in kept.items():
        f["k"] = k
    half = (fams[0]["y0"] + fams[-1]["y1"]) / 2.0
    lo = [xi for _, (f, xi) in kept.items() if (f["y0"] + f["y1"]) / 2.0 >= half]
    hi = [xi for _, (f, xi) in kept.items() if (f["y0"] + f["y1"]) / 2.0 < half]
    out["x_lo"] = round(float(np.median(lo)), 3) if len(lo) >= 2 else None
    out["x_hi"] = round(float(np.median(hi)), 3) if len(hi) >= 2 else None
    out["split"] = (round(abs(out["x_lo"] - out["x_hi"]), 3)
                    if out["x_lo"] is not None and out["x_hi"] is not None
                    else None)
    bad = []
    if out["split"] is not None and out["split"] > X_SPLIT_MAX:
        bad.append("split")
    if out["grid_res"] is not None and out["grid_res"] > GRID_RES_MAX:
        bad.append("grid")
    if r["gap"] is not None and r["gap"] > PAIR_GAP_MAX:
        bad.append("pairgap")
    out["why"] = ",".join(bad)
    return out


def gold_frame(rgb: np.ndarray, s_cal: float | None = None) -> dict:
    yl, wh = _masks(rgb)
    pts = _points(yl, wh)
    fams = _extract_families(pts)
    sol = _solve(fams, s_cal)
    sol["n_pts"] = len(pts)
    sol["fams"] = [{k: v for k, v in f.items() if k != "pts"} for f in fams]
    return sol


def curb_pair_s(fams: list[dict]) -> float | None:
    """两缘同框帧给出的 s：两条黄缘恒相隔 4 个道宽，斜率差即 4s。"""
    fams = _merge(fams)
    curbs = [f for f in fams if f["yellow"] >= 0.6]
    if len(curbs) < 2:
        return None
    s = (curbs[-1]["a"] - curbs[0]["a"]) / (2 * CURB_K)
    return s if 0.5 <= s <= 8.0 else None


def overlay(rgb: np.ndarray, g: dict, fams: list[dict]) -> np.ndarray:
    img = rgb.copy()
    for f in fams:
        y0, y1 = int(f["y0"]), int(f["y1"])
        used = f.get("used")
        c = ((255, 0, 0) if f.get("yellow", 0) >= 0.6 else (0, 200, 255)) if used \
            else (120, 120, 120)
        cv2.line(img, (int(f["a"] * y0 + f["b"]), y0),
                 (int(f["a"] * y1 + f["b"]), y1), c, 3 if used else 1)
        if used and f.get("k") is not None:
            xm = int(f["a"] * (y0 + y1) / 2 + f["b"])
            cv2.putText(img, str(f["k"]), (xm, int((y0 + y1) / 2)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
                        cv2.LINE_AA)
    txt = (f"gold x={g['x']} s={g['s']} res={g.get('grid_res')} "
           f"w={g['width']} sp={g['split']} {g['why']}")
    cv2.rectangle(img, (0, 0), (1280, 30), (0, 0, 0), -1)
    cv2.putText(img, txt, (6, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (255, 255, 255), 1, cv2.LINE_AA)
    return img


def collect_families(files: list[Path]) -> list[dict]:
    """第一遍：逐帧只提线族（CV 是耗时项，缓存后标定与求解不再重跑）。"""
    out = []
    for p in files:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        if rgb is None:
            out.append({"file": p.name, "fams": [], "rgb": None})
            continue
        yl, wh = _masks(rgb)
        pts = _points(yl, wh)
        out.append({"file": p.name, "fams": _merge(_extract_families(pts)),
                    "rgb": rgb, "n_pts": len(pts)})
    return out


def _curb_x(fams: list[dict], s: float):
    """最外侧地面线反解横位——**路缘的身份是"最外"，不是"黄"**。

    颜色不能当身份：实测同一条右路缘在远段读作黄、近段读作白（光照与透视使
    饱和度跨过阈值），一旦按颜色选锚就会把金币带、护栏缝当成路缘（帧 000421
    车在左道第 1 格，按颜色选锚给出 +0.03，按最外选给出 −1.04，与画面一致）。
    金币铺在道中心（k=±0.5、±1.5），斜率一定比路缘靠内，"每侧取最外"天然排除。"""
    if not fams:
        return None
    xs = []
    ar = max(f["a"] for f in fams)
    al = min(f["a"] for f in fams)
    if ar > 0:
        xs.append(CURB_K - ar / s)
    if al < 0 and abs(al - ar) > 0.05:
        xs.append(-CURB_K - al / s)
    inb = [v for v in xs if abs(v) <= X_PHYS]
    if not inb:
        return None
    gap = abs(xs[0] - xs[1]) if len(xs) == 2 else None
    return float(np.mean(inb)), len(inb), gap


def calibrate_s(frames: list[dict], lo: float = 1.2, hi: float = 4.0,
                step: float = 0.005) -> tuple[list[float | None], dict]:
    """局级 s：一维搜索让**全帧白线落格残差**最小。

    为什么不能逐帧估 s：单帧里「s 取半」配上一整套改号的道号在像素上同样自洽
    （实测 5 个双缘帧给出的 s 从 0.60 跳到 2.78）。只有把整局的落格一致性放到
    一起看，s 才被钉住——道号是整数这件事在几十个帧上同时成立的概率极低，除非
    s 是对的。"""
    fams_by_frame = []
    for fr in frames:
        fams = _merge(fr["fams"])
        fams, _ = _vp_gate(fams)
        fams_by_frame.append(fams)
    best = None
    curve = []
    s = lo
    while s <= hi:
        res, n = [], 0
        for fams in fams_by_frame:
            cx = _curb_x(fams, s)
            if cx is None:
                continue
            x = cx[0]
            r = [abs((x + f["a"] / s) - round(x + f["a"] / s))
                 for f in fams if abs(round(x + f["a"] / s)) <= CURB_K]
            if not r:
                continue
            res.append(float(np.mean(r)))
            n += 1
        if n >= 8:
            m = float(np.mean(res))
            curve.append((round(s, 3), round(m, 4), n))
            if best is None or m < best[0]:
                best = (m, s, n)
        s += step
    if best is None:
        return [None] * len(frames), {"n_pair": 0}
    s_cal = round(best[1], 4)
    curve.sort(key=lambda c: c[1])
    diag = {"s": s_cal, "frames": best[2], "res": round(best[0], 4),
            "top": curve[:6]}
    per = [s_cal] * len(frames)
    return per, diag


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", required=True, help="录制会话 frames/ 目录")
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--overlay", type=int, default=0, help="抽 N 张叠图")
    args = ap.parse_args()
    d = Path(args.frames)
    session = d.parent.name
    out = Path(args.out) if args.out else d.parent / "gold2"
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(d.glob("*.jpg"))[::max(1, args.every)]
    if args.limit:
        files = files[:args.limit]
    frames = collect_families(files)
    s_list, s_diag = calibrate_s(frames)
    recs = []
    pick = set(np.linspace(0, len(frames) - 1, max(1, args.overlay)).astype(int)) \
        if args.overlay else set()
    for i, fr in enumerate(frames):
        fams = [dict(f) for f in fr["fams"]]
        sol = _solve(fams, s_list[i])
        rec = {"file": fr["file"], "s_cal": s_list[i], **sol,
               "n_pts": fr.get("n_pts", 0),
               "fams": [{k: v for k, v in f.items() if k != "pts"} for f in fams]}
        recs.append(rec)
        if i in pick and fr["rgb"] is not None:
            cv2.imwrite(str(out / f"ov_{fr['file']}"),
                        cv2.cvtColor(overlay(fr["rgb"], sol, fams),
                                    cv2.COLOR_RGB2BGR))
    path = out / f"gold2_{session}.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs),
                    encoding="utf-8")
    ok = [r for r in recs if r["x"] is not None and not r["why"]]
    print(f"{session}: {len(recs)} frames, usable {len(ok)}, "
          f"s_cal {s_diag.get('s')} (frames {s_diag.get('frames')}, "
          f"grid_res {s_diag.get('res')})")
    for key in ("x", "grid_res", "split", "width"):
        v = sorted(abs(r[key]) if key in ("split", "grid_res") else r[key]
                   for r in recs if r.get(key) is not None)
        if v:
            print(f"  {key:9s} p50 {v[len(v)//2]:.3f}  p90 {v[int(.9*len(v))]:.3f}"
                  f"  n {len(v)}")
    why: dict[str, int] = {}
    for r in recs:
        why[r["why"] or "ok"] = why.get(r["why"] or "ok", 0) + 1
    print("  reject", dict(sorted(why.items(), key=lambda kv: -kv[1])))
    print("wrote", path)


if __name__ == "__main__":
    main()
