# -*- coding: utf-8 -*-
"""找边离散化逐阶段敏感性追踪（焦距链路收口后的下一工，GPT 方案步骤 1/2）。

对 5 个同帧扰动敏感包，在「同帧、零内容混淆」条件下施加与实测拍间同量级的
参数扰动（s±0.01 / fx±3%），逐级镜像产线 _scan_side 管线并对比基线/扰动的：
A 格中位剖面 ph → B 地面锚 g → C 候选穿越（两格持续）与各门裁决 → D 终选穿越
与子格插值。回答：大跳变第一次发生在哪一级（GPT：定位到阶段再谈改表示/门/置信）。

镜像口径与 depth_geo._scan_side 逐行同构（常量与 _binned_median/
_edge_body_evidence 直接复用产线函数），仅作诊断不做判据。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, r"D:\maaracing_assistant")

from maaracing_master.plugins.speedrush import depth_geo as dg
from maaracing_master.plugins.speedrush import moge_post as mp
from maaracing_master.plugins.speedrush.world_model import load_calib

ROOT = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush"
            r"\control_traces")
DD = ROOT / "depth_debug_20261010_212349"
# 上一轮同帧扰动实验的 5 个敏感包与其翻转扰动
CASES = [  # (stem, tag, fx_mult, ds, side_flipped)
    ("d00007", "s-0.01", 1.00, -0.01, "L"),
    ("d00019", "s-0.01", 1.00, -0.01, "L"),
    ("d00031", "s-0.01", 1.00, -0.01, "R"),
    ("d00049", "fx-3%", 0.97, 0.00, "R"),
    ("d00067", "fx-3%", 0.97, 0.00, "R"),
]
FX_N = 0.460
sess = dg.load_session(
    Path(__import__("maaracing_master.plugins.speedrush", fromlist=["DEPTH_MODEL_FILE"])
         .DEPTH_MODEL_FILE))
CAL = load_calib()


def rebuild(points, mask, ms, fxn, s):
    H, W = points.shape[:2]
    z = points[..., 2] + s
    u = (np.arange(W) + 0.5) / W
    v = (np.arange(H) + 0.5) / H
    X = (u[None, :] - 0.5) * z / fxn
    Y = (v[:, None] - 0.5) * z / (fxn * W / H)
    pts = np.stack([X, Y, z], -1) * ms
    valid = mask & (z > 0)
    pts = np.stack([cv2.resize(pts.astype(np.float32)[..., k], (1280, 720),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (1280, 720),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~vf] = np.nan
    return pts


def stage_trace(xb, hb, side, zc, extent):
    """镜像 _scan_side：返回 (px, ph, thr, 候选列表, 终选)。候选元素=
    (i, 过尾部门, 过缘体门, genuine, x)。"""
    s = xb * side
    sel = (s > 0.2) & (s <= dg.X_MAX_M) & np.isfinite(hb)
    xs, hs = xb[sel], hb[sel]
    bi = np.floor((xs.astype(np.float64) + dg.X_MAX_M) / dg.XBIN_DX).astype(np.int64)
    inb = (bi >= 0) & (bi < dg.N_XBIN)
    bi = np.where(inb, bi, 0)
    k_ok = (dg.XBIN_CENTERS * side > 0) & (np.abs(dg.XBIN_CENTERS) <= extent * zc)
    keep = inb & k_ok[bi]
    counts = np.bincount(bi[keep], minlength=dg.N_XBIN)
    want = np.nonzero(k_ok & (counts >= dg.MIN_BIN_PTS))[0]
    if want.size < 3:
        return None
    px = dg.XBIN_CENTERS[want]
    ph = dg._binned_median(hs[keep], bi[keep], want, counts)
    srt = np.argsort(px * side)
    px, ph = px[srt], ph[srt]
    thr = dg.EDGE_HT + dg.EDGE_HT_SLOPE * max(0.0, zc - 3.0)
    g = next((i for i in range(len(px)) if ph[i] <= thr), -1)
    cands = []
    chosen = None
    if g >= 0:
        for i in range(g + 1, len(px) - 1):
            if ph[i] > thr and ph[i + 1] > thr:
                tail = ph[i + 2:]
                ok_tail = not (len(tail) and (tail > thr).mean() < dg.EDGE_TAIL_FRAC)
                genuine = ph[i - 1] <= thr
                ok_body = dg._edge_body_evidence(ph, i, thr, genuine)
                cands.append((i, ok_tail, ok_body, genuine))
                if ok_tail and ok_body and chosen is None:
                    if genuine:
                        f = (thr - ph[i - 1]) / (ph[i] - ph[i - 1])
                        chosen = (i, float(px[i - 1] + f * (px[i] - px[i - 1])), "interp")
                    else:
                        chosen = (i, float(px[i]), "step")
    return px, ph, thr, g, cands, chosen


for stem, tag, fmul, ds, side_fl in CASES:
    rgb = cv2.cvtColor(cv2.imread(str(DD / f"{stem}_frame.jpg")), cv2.COLOR_BGR2RGB)
    points, mask, ms = mp.forward(sess, mp.preprocess(rgb, 1280, 720), 1032)
    uv_g, m = mp._masked_nearest_resize(
        mp.normalized_view_plane_uv(points.shape[1], points.shape[0]), mask)
    ptsw, _ = mp._masked_nearest_resize(points, mask)
    uv_s = uv_g[m].astype(np.float64)
    xy = ptsw[m][..., :2].astype(np.float64)
    z = np.maximum(ptsw[m][..., 2].astype(np.float64), 1e-6)
    z_med = float(np.median(z))
    ar = points.shape[1] / points.shape[0]
    f0 = FX_N * 2 * ar / (1 + ar ** 2) ** 0.5

    def rf(s):
        p = xy / (z + s)[:, None]
        return float(((f0 * p - uv_s) ** 2).sum())
    s0 = mp._minimize_local(rf, 0.05 * z_med)

    traces = {}
    for label, fxn_, s_ in (("base", FX_N, s0), (tag, FX_N * fmul, s0 + ds)):
        pts_full = rebuild(points, mask, ms, fxn_, s_)
        rd = dg.reading_from_points(pts_full, fxn_ * 1280, CAL)
        # 截获 _scan_side 输入（近箱 zc<=NEAR_Z 才进车道读数）
        rec = []

        def wrapper(xbb, hbb, sd, zc, ext, _rec=rec):
            r = dg._scan_side.__wrapped__(xbb, hbb, sd, zc, ext) \
                if hasattr(dg._scan_side, "__wrapped__") else None
            _rec.append((sd, zc, ext, xbb.copy(), hbb.copy(), r))
            return r

        orig = dg._scan_side
        # 直接重放：拿 reading 内部的逐箱调用（不 patch，手动按同口径重算）
        obj = None
        Xb = pts_full[dg.Y0:dg.DIAG_Y1, :, 0]
        Yb = pts_full[dg.Y0:dg.DIAG_Y1, :, 1]
        Zb = pts_full[dg.Y0:dg.DIAG_Y1, :, 2]
        coef = dg._fit_road_plane(Xb, Yb, Zb, fy=fxn_ * 1280)
        a, b, c = map(float, coef)
        sgn = -1.0 if b * 5.0 + c > 0 else 1.0
        hgt = sgn * (Yb - (a * Xb + b * Zb + c))
        dig = dg._dig_3d_mask(Xb, Zb, hgt, obj)
        extent = (pts_full.shape[1] / 2.0) / (fxn_ * 1280)
        base_mask = np.isfinite(Xb) & np.isfinite(hgt) & (~dig) \
            & ~(np.isfinite(hgt) & (hgt > dg.SKY_HGT))
        boxes = []
        for z0, z1 in dg.ZBIN:
            mm = base_mask & (Zb >= z0) & (Zb < z1)
            zc = (z0 + z1) / 2
            if mm.sum() < dg.MIN_BOX_PTS or zc > dg.NEAR_Z:
                continue
            for sd in (1, -1):
                ex = orig(Xb[mm], hgt[mm], sd, zc, extent)
                tr = stage_trace(Xb[mm], hgt[mm], sd, zc, extent)
                boxes.append((sd, zc, ex, tr))
        traces[label] = {"reading": rd, "boxes": boxes, "coef_c": c}

    rb, rp = traces["base"], traces[tag]
    print(f"\n=== {stem}  {tag}  读数 L/R: {rb['reading'].left_edge_lane:+.2f}/"
          f"{rb['reading'].right_edge_lane:+.2f} → {rp['reading'].left_edge_lane:+.2f}/"
          f"{rp['reading'].right_edge_lane:+.2f} ===")
    side_num = 1 if side_fl == "R" else -1
    for (sd, zc, exb, trb), (sd2, zc2, exp_, trp) in zip(rb["boxes"], rp["boxes"]):
        if sd != side_num or trb is None or trp is None:
            continue
        pxb, phb, thrb, gb, cb, chb = trb
        pxp, php, thrp, gp, cp, chp = trp
        dph = float(np.nanmax(np.abs(phb - php))) if len(phb) == len(php) else float("nan")
        same = "同" if chb and chp and chb[0] == chp[0] else "异"
        print(f"  z{zc:>4}.5m side{sd:+d}: |Δph|max={dph:.3f}m  终选 {same}"
              f"  x: {chb[1] if chb else float('nan'):+.2f} → {chp[1] if chp else float('nan'):+.2f}"
              f"  候选数 {len(cb)}→{len(cp)}")
        if chb and chp and chb[0] == chp[0] and abs((chb[1] or 0) - (chp[1] or 0)) < 0.05:
            continue
        detail = []
        cbmap = {c[0]: c for c in cb}
        cpmap = {c[0]: c for c in cp}
        for i in sorted(set(cbmap) | set(cpmap)):
            a_, b_ = cbmap.get(i), cpmap.get(i)
            if a_ != b_:
                detail.append(f"格{i}: 尾门{a_[1] if a_ else '-'}/{b_[1] if b_ else '-'} "
                              f"体门{a_[2] if a_ else '-'}/{b_[2] if b_ else '-'}")
        print(f"      首个分歧级: {'剖面' if dph > 0.02 else '门/候选'}"
              + (f"  [{'; '.join(detail[:3])}]" if detail else ""))
