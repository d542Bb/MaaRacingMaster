# -*- coding: utf-8 -*-
"""sides=1 被拒侧剖面重放（2026-10-09，只读）：真无结构 vs 被门杀 vs 缓坡类真缘。

问（维护者"从头走一遍"）：sides=1 43.1% 的被拒侧，剖面长什么样——
  A 剖面平坦（真无结构/开阔路侧）
  B 有骤升但缘体门没过（结构存在，判据严）
  C 缓坡爬升不到顶（斜板类真缘被"缓坡假缘"门系统性拒绝）
  D 点太少（覆盖问题）

方法：trace（dgeo_sides==1 的拍）→ 证据包 d*.npz（按 fid 映射）→ 产线口径
重放（moge_post 离线复算 → reading_from_points）拿 rejects/edge_pts →
对被拒侧复刻 _scan_side 的选点+分箱+中位（复刻不复制，判据常量同源），
逐箱输出剖面统计并画图（剖面曲线 + 阈值线，cv2）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_side_profile.py \
        --trace trace_20261008_184526_p2 --debug depth_debug_20261008_184526
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

TRACES = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
          / "speedrush" / "control_traces")
OUT = Path(__file__).resolve().parent / "side_profile_out"


def replay_pts(npz_path: Path):
    """证据包 → 全幅点图（离线复算口径，见 depth_geo docstring）。"""
    d = np.load(npz_path)
    pts_n, valid = d["pts"].astype(np.float32), d["valid"]
    fx, fy = float(d["fx"]), float(d["fy"])
    H, W = 720, 1280
    pts = np.stack([cv2.resize(pts_n[..., k], (W, H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid = cv2.resize(valid.astype(np.float32), (W, H),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    pts = pts.copy()
    pts[~valid] = np.nan
    return pts, fx * W, fy * H


def side_profile(pts: np.ndarray, fx: float, fy: float, coef, side: int,
                 ego_mask, object_mask):
    """被拒侧的逐箱剖面（复刻 _scan_side 选点+分箱+中位，返回统计不返回穿越）。"""
    if coef is None:
        return None
    a, b, c = coef
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    X, Y, Z = pts[dg.Y0:dg.DIAG_Y1, :, 0], pts[dg.Y0:dg.DIAG_Y1, :, 1], \
        pts[dg.Y0:dg.DIAG_Y1, :, 2]
    hgt = sgn * (Y - (a * X + b * Z + c))
    dig = dg._dig_band(ego_mask, pts.shape[1])
    if object_mask is not None:
        dig = dig | dg._dig_band(object_mask, pts.shape[1])
    sky = np.isfinite(hgt) & (hgt > dg.SKY_HGT)
    extent = (pts.shape[1] / 2.0) / fx
    base = np.isfinite(X) & np.isfinite(hgt) & (~dig) & (~sky)
    out = []
    for z0, z1 in dg.ZBIN:
        m = base & (Z >= z0) & (Z < z1)
        if m.sum() < dg.MIN_BOX_PTS:
            out.append((z0, z1, None, "no_pts"))
            continue
        zc = (z0 + z1) / 2
        s = X[m] * side
        sel = (s > 0.2) & (s <= dg.X_MAX_M) & np.isfinite(hgt[m])
        if sel.sum() < dg.MIN_SIDE_PTS:
            out.append((z0, z1, None, "few_pts"))
            continue
        xs, hs = X[m][sel], hgt[m][sel]
        bi = np.floor((xs.astype(np.float64) + dg.X_MAX_M) / dg.XBIN_DX).astype(np.int64)
        inb = (bi >= 0) & (bi < dg.N_XBIN)
        bi = np.where(inb, bi, 0)
        k_ok = (dg.XBIN_CENTERS * side > 0) & (np.abs(dg.XBIN_CENTERS) <= extent * zc)
        keep = inb & k_ok[bi]
        counts = np.bincount(bi[keep], minlength=dg.N_XBIN)
        want = np.nonzero(k_ok & (counts >= dg.MIN_BIN_PTS))[0]
        if want.size < 3:
            out.append((z0, z1, None, "few_bins"))
            continue
        px = dg.XBIN_CENTERS[want]
        ph = dg._binned_median(hs[keep], bi[keep], want, counts)
        srt = np.argsort(px * side)
        px, ph = px[srt], ph[srt]
        thr = dg.EDGE_HT + dg.EDGE_HT_SLOPE * max(0.0, zc - 3.0)
        above = (ph > thr).mean()
        mx = float(ph.max()) if ph.size else 0.0
        # 分类（维护者四类）：A 平坦（max≤阈）B 有骤升（max≥0.15 或最大增量≥脸阈）
        # C 缓坡爬升（max>阈但 <0.15 且无骤升）D 混合
        if mx <= thr:
            kind = "A_flat"
        else:
            inc = float(np.diff(ph).max()) if ph.size > 1 else 0.0
            if mx >= 0.15 or inc >= dg.EDGE_FACE_MIN:
                kind = "B_rise"
            else:
                kind = "C_slope"
        out.append((z0, z1, (px.tolist(), ph.tolist(), thr), kind))
    return out


def draw(profiles, side, fid, path):
    """逐箱剖面拼图（cv2，剖面曲线+阈值线）。"""
    W, H0 = 560, 200
    rows = []
    for z0, z1, data, kind in profiles:
        img = np.full((H0, W, 3), 25, np.uint8)
        cv2.putText(img, f"z{z0}-{z1} {kind}", (8, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 220, 255), 1)
        if data:
            px, ph, thr = data
            lo, hi = -0.2, max(0.6, float(max(ph)) + 0.1)
            for i in range(len(px) - 1):
                x0 = int((i / max(1, len(px) - 1)) * (W - 40)) + 30
                x1 = int(((i + 1) / max(1, len(px) - 1)) * (W - 40)) + 30
                y0 = H0 - 30 - int((ph[i] - lo) / (hi - lo) * (H0 - 50))
                y1 = H0 - 30 - int((ph[i + 1] - lo) / (hi - lo) * (H0 - 50))
                cv2.line(img, (x0, y0), (x1, y1), (0, 255, 120), 2)
            ty = H0 - 30 - int((thr - lo) / (hi - lo) * (H0 - 50))
            cv2.line(img, (30, ty), (W - 10, ty), (0, 140, 255), 1)
        rows.append(img)
    body = np.vstack(rows)
    cv2.putText(body, f"fid={fid} side={'R' if side > 0 else 'L'}",
                (8, body.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (255, 255, 255), 1)
    cv2.imwrite(str(path), body)


def draw_bev(pts, fx, fy, coef, fid, raw_path, path):
    """点云 BEV 俯视（X 横向 × Z 深度，颜色=高出路面 hgt）+ raw + 剖面拼图。

    问：被拒侧的真实边界在点云里是锐利台阶还是被 MoGe 抹平——俯视图一眼定。
    颜色标尺 0~1m（护栏脸高 ~0.7-1m；路面 ~0；暗=0 亮橙=1）。"""
    a, b, c = coef
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    X, Y, Z = pts[dg.Y0:dg.DIAG_Y1, :, 0], pts[dg.Y0:dg.DIAG_Y1, :, 1], \
        pts[dg.Y0:dg.DIAG_Y1, :, 2]
    hgt = sgn * (Y - (a * X + b * Z + c))
    ok = np.isfinite(X) & np.isfinite(Z) & np.isfinite(hgt) & (hgt <= dg.SKY_HGT)
    xz, zz, hz = X[ok], Z[ok], hgt[ok]
    zlo, zhi, dxy = 2.0, 16.0, 0.25
    zi = ((zz - zlo) / dxy).astype(np.int64)
    xi = ((xz + 12.0) / dxy).astype(np.int64)
    inb = (zi >= 0) & (zi < int((zhi - zlo) / dxy)) & (xi >= 0) & (xi < 96)
    zi, xi, hz = zi[inb], xi[inb], hz[inb]
    nz = int(zi.max()) + 1
    flat = zi * 96 + xi
    s = np.bincount(flat, weights=hz, minlength=nz * 96)
    n = np.bincount(flat, minlength=nz * 96)
    mean = np.where(n > 0, s / np.maximum(n, 1), np.nan)
    grid = mean.reshape(nz, 96)
    img = np.full((grid.shape[0], 96, 3), 25, np.uint8)
    t = np.clip(grid / 1.0, 0, 1)
    img[..., 2] = (np.nan_to_num(t) * 255).astype(np.uint8)   # 蓝=0
    img[..., 0] = (np.nan_to_num(t) * 0 + 60).astype(np.uint8)
    img[..., 1] = (np.nan_to_num(t) * 180).astype(np.uint8)
    img = np.repeat(np.repeat(img, 6, axis=0), 8, axis=1)     # 放大到 ~768 宽
    cv2.putText(img, f"BEV hgt 0-1m  fid={fid}", (8, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    for zm in (5, 10, 15):
        y = int((zm - zlo) / dxy) * 6
        cv2.line(img, (0, y), (img.shape[1], y), (120, 120, 120), 1)
        cv2.putText(img, f"z={zm}m", (4, y - 3), cv2.FONT_HERSHEY_SIMPLEX,
                    0.4, (180, 180, 180), 1)
    for xm in (-5, 0, 5, 10):
        x = int((xm + 12.0) / dxy) * 8
        cv2.line(img, (x, 0), (x, img.shape[0]), (120, 120, 120), 1)
        cv2.putText(img, f"x={xm:+d}", (x + 2, img.shape[0] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (180, 180, 180), 1)
    rows = []
    if raw_path and Path(raw_path).exists():
        raw = cv2.imread(str(raw_path))
        sc = img.shape[1] / raw.shape[1]
        rows.append(cv2.resize(raw, (img.shape[1], int(raw.shape[0] * sc))))
    rows.append(img)
    prof_p = OUT / f"p{fid:05d}_L.png"
    if not prof_p.exists():
        prof_p = OUT / f"p{fid:05d}_R.png"
    if prof_p.exists():
        pr = cv2.imread(str(prof_p))
        sc = img.shape[1] / pr.shape[1]
        rows.append(cv2.resize(pr, (img.shape[1], int(pr.shape[0] * sc))))
    cv2.imwrite(str(path), np.vstack(rows))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace", default="trace_20261008_184526_p2")
    ap.add_argument("--debug", default="depth_debug_20261008_184526")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--fids", default="", help="只看这些 fid（逗号分隔）")
    ap.add_argument("--bev", default="",
                    help="只画这些 fid 的 BEV 拼图（逗号分隔，配合 --raw-seq）")
    ap.add_argument("--raw-seq", default="",
                    help="与 --bev 对齐的 raw 序号（逗号分隔；hud 图 #fid 标注对出来的）")
    ap.add_argument("--raw-dir", default="",
                    help="raw 帧目录（DebugPairWriter 的 raw/）")
    a = ap.parse_args()

    trace_p = TRACES / f"{a.trace}.jsonl"
    debug_d = TRACES / a.debug
    cal = load_calib()
    ego = dg.DepthRoadObserver._load_ego_mask()
    # fid → 证据包
    evid = {}
    for p in sorted(debug_d.glob("*_evid.npz")):
        try:
            fid = int(np.load(p)["fid"])
            if fid >= 0:
                evid[fid] = p
        except Exception:
            pass

    # trace 里 sides=1 的拍
    want = set(int(x) for x in a.fids.split(",") if x.strip())
    targets = []
    for line in trace_p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("dgeo_sides") == 1 and r.get("fid") in evid:
            if not want or r["fid"] in want:
                targets.append((int(r["fid"]), r.get("dgeo_rejects") or ""))
    print(f"sides=1 且有证据包的帧：{len(targets)}（取前 {a.limit}）")

    OUT.mkdir(parents=True, exist_ok=True)

    if a.bev:
        seqs = [int(x) for x in a.raw_seq.split(",") if x.strip()]
        for fid, seq in zip((int(x) for x in a.bev.split(",") if x.strip()), seqs):
            pts, fx, fy = replay_pts(evid[fid])
            rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
            if rd.coef is None:
                print(f"fid={fid} 平面拟合失败，跳过")
                continue
            raw = ""
            if a.raw_dir and seq:
                side_c = "L" if any("L:" in x for x in rd.rejects) else "R"
                cand = Path(a.raw_dir) / f"{seq:04d}_raw.jpg"
                raw = str(cand) if cand.exists() else ""
            draw_bev(pts, fx, fy, rd.coef, fid, raw,
                     OUT / f"bev_{fid:05d}.png")
            print(f"fid={fid} BEV → bev_{fid:05d}.png")
        return 0

    kind_c: dict[str, int] = {}
    n_done = 0
    for fid, rej in targets[:a.limit]:
        pts, fx, fy = replay_pts(evid[fid])
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
        side = 1 if any("R:" in x for x in rd.rejects) else -1
        prof = side_profile(pts, fx, fy, rd.coef, side, ego, None)
        if prof is None:
            continue
        n_done += 1
        kinds = []
        for z0, z1, _, kind in prof:
            if kind != "no_pts":
                kind_c[kind] = kind_c.get(kind, 0) + 1
                kinds.append(f"z{z0}:{kind[:1]}")
        print(f"fid={fid} side={'R' if side > 0 else 'L'}  " + " ".join(kinds))
        draw(prof, side, fid, OUT / f"p{fid:05d}_{'R' if side > 0 else 'L'}.png")
    print(f"\n重放 {n_done} 帧，剖面分类计数（逐箱）：")
    for k, v in sorted(kind_c.items()):
        print(f"  {k:10s} {v:4d}")
    print(f"\n图已存 {OUT}")
    print("判读：C_slope 占比高=斜板类真缘被'缓坡假缘'门系统性拒绝；"
          "A_flat 占比高=真无结构（单侧供数无意义）；B_rise=判据严可放宽。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
