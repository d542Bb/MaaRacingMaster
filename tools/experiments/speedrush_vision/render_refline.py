# -*- coding: utf-8 -*-
"""阶段二参考线验证渲染器：证据包 / 金标帧 / 录制帧 → 参考线候选画回真实画面。

三种输入（2026-10-03，阶段二第一刀「验证必须看图」）：
* 默认      depth_debug_*/d*_evid.npz 证据包（坏帧语料）
* --gold    depth_review/npy_moge/*.npz 金标帧（高度图做底）
* --frames  录制会话 frames/*.jpg（正常帧语料）——离线跑产线 MoGe，同产线读数

叠加四件套（像素落点一律按点云真值反查，零解析回投——2026-10-02 焦距单位旧账
不允许重犯）：黄圈=产线缘点锚（render_depth_debug 原样）；品红=双侧齐备箱的
缘中点链；红=中点一次拟合；青黄=路面区域逐箱 X 中位（不依赖找边的对照链）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/render_refline.py \
        --root <control_traces|speedrush数据根> [--gold | --frames <会话目录> \
        [--every 60]] [--out 输出目录]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DIAG_Y1, Y0, ZBIN, _dig_band, _fit_road_plane, infer_points,
    load_session, reading_from_points, render_depth_debug)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

FULL_W, FULL_H = 1280, 720
X_TOL = 0.4          # 像素反查的横向窗（米）
Z_HALF = 1.0         # 像素反查的深度半窗（米），与产线锚点同宽


def _pixel_of(pts: np.ndarray, x: float, zc: float) -> tuple[int, int] | None:
    """按点云真值反查 (x, zc) 邻域的中位像素；邻域空则 None（诚实断线）。"""
    Z = pts[Y0:DIAG_Y1, :, 2]
    X = pts[Y0:DIAG_Y1, :, 0]
    m = ((Z >= zc - Z_HALF) & (Z < zc + Z_HALF)
         & (np.abs(X - x) < X_TOL) & np.isfinite(X) & np.isfinite(Z))
    if not m.any():
        return None
    rr, cc = np.nonzero(m)
    return int(round(np.median(cc))), int(round(np.median(rr)))


def _overlay(img: np.ndarray, pts: np.ndarray, fy: float,
             rd, ego, obj) -> str:
    """在 render_depth_debug 产物上叠参考线候选，返回一行摘要。"""
    by_z: dict[float, dict[int, float]] = {}
    for side, zc, xm in rd.edge_pts:
        by_z.setdefault(zc, {})[side] = xm
    mids = [(zc, (s[1] + s[-1]) / 2.0) for zc, s in sorted(by_z.items())
            if 1 in s and -1 in s]
    line = f"bins={len(mids)}/{len(ZBIN)}"
    if len(mids) >= 2:
        zs = np.array([z for z, _ in mids])
        ms = np.array([m for _, m in mids])
        p1, p0 = np.polyfit(zs, ms, 1)
        resid = float(np.abs(ms - (p1 * zs + p0)).max())
        line += f" fit=x={p1:+.3f}z{p0:+.3f} resid_max={resid:.2f}m"
        prev = None
        for zc in np.arange(3.5, 15.6, 0.5):
            hit = _pixel_of(pts, float(p1 * zc + p0), float(zc))
            if hit is None:
                prev = None
                continue
            cv2.circle(img, (hit[0], hit[1] + Y0), 3, (0, 0, 255), -1)
            if prev is not None:
                cv2.line(img, prev, (hit[0], hit[1] + Y0), (0, 0, 255), 2)
            prev = (hit[0], hit[1] + Y0)
    for zc, mid in mids:
        hit = _pixel_of(pts, mid, zc)
        if hit is not None:
            cv2.circle(img, (hit[0], hit[1] + Y0), 4, (255, 0, 255), 2)

    # 对照链 v2：路面区域逐箱「边界中点」——路面=hgt<0.15 单侧门（阴影也是路，
    # 不设下限剔阴影）；左右边界取 2%/98% 分位（对称化，抗单侧遮挡/挖除不对称）；
    # 跨度超 25m=背景漏入，弃权。橙色=左右边界链，青黄=边界中点链。
    Xa, Ya, Za = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
    digm = _dig_band(ego, pts.shape[1])
    if obj is not None:
        digm = digm | _dig_band(obj, pts.shape[1])
    coef = _fit_road_plane(Xa, Ya, Za, digm, fy=fy)
    if coef is not None:
        a_, b_, c_ = map(float, coef)
        sgn = -1.0 if b_ * 5.0 + c_ > 0 else 1.0
        road_y = sgn * (Ya - (a_ * Xa + b_ * Za + c_))
        prev_mid = None
        prev_lo = prev_hi = None
        for z0, z1 in ZBIN:
            zc = (z0 + z1) / 2
            m = ((Za >= z0) & (Za < z1) & (~digm) & np.isfinite(road_y)
                 & (road_y < 0.15) & np.isfinite(Xa))
            if m.sum() < 200:
                prev_mid = prev_lo = prev_hi = None
                continue
            xs = Xa[m]
            lo, hi = (float(v) for v in np.percentile(xs, [2, 98]))
            if hi - lo > 25.0:
                prev_mid = prev_lo = prev_hi = None
                continue
            rr, cc = np.nonzero(m)
            mid = (lo + hi) / 2

            def _at(xv):
                pick = np.argsort(np.abs(xs - xv))[: max(1, m.sum() // 50)]
                return (int(round(np.median(cc[pick]))),
                        int(round(np.median(rr[pick]))))

            u_mid, v_mid = _at(mid)
            u_lo, v_lo = _at(lo)
            u_hi, v_hi = _at(hi)
            cv2.circle(img, (u_mid, v_mid + Y0), 4, (255, 255, 0), 2)
            cv2.circle(img, (u_lo, v_lo + Y0), 2, (0, 128, 255), -1)
            cv2.circle(img, (u_hi, v_hi + Y0), 2, (0, 128, 255), -1)
            if prev_mid is not None:
                cv2.line(img, prev_mid, (u_mid, v_mid + Y0), (255, 255, 0), 2)
                cv2.line(img, prev_lo, (u_lo, v_lo + Y0), (0, 128, 255), 1)
                cv2.line(img, prev_hi, (u_hi, v_hi + Y0), (0, 128, 255), 1)
            prev_mid, prev_lo, prev_hi = (u_mid, v_mid + Y0), (u_lo, v_lo + Y0), \
                (u_hi, v_hi + Y0)
    cv2.putText(img, line, (8, 50), cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 255, 255), 2)
    return line


def _load_evid(stem: Path):
    z = np.load(str(stem) + "_evid.npz")
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid_full] = np.nan
    fx, fy = float(z["fx"]) * FULL_W, float(z["fy"]) * FULL_H
    ego = obj = None
    if Path(str(stem) + "_ego.npy").exists():
        ego = np.unpackbits(np.load(str(stem) + "_ego.npy"))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        if Path(str(stem) + "_mask.npy").exists():
            merged = np.unpackbits(np.load(str(stem) + "_mask.npy"))[: FULL_W * FULL_H] \
                .reshape(FULL_H, FULL_W).astype(bool)
            obj = merged & ~ego
    return pts, fx, fy, ego, obj, {"pts": z["pts"], "valid": valid,
                                   "fx": np.float64(float(z["fx"])),
                                   "fy": np.float64(float(z["fy"]))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True,
                    help="control_traces 根（默认模式）或 speedrush 数据根（--gold）")
    ap.add_argument("--out", default="../../.workbuddy-ai/refline_render")
    ap.add_argument("--gold", action="store_true")
    ap.add_argument("--frames", help="录制会话目录（frames/*.jpg，离线跑产线 MoGe）")
    ap.add_argument("--every", type=int, default=60, help="帧采样间隔")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cal = load_calib()

    if a.gold:
        for p in sorted((Path(a.root) / "depth_review" / "npy_moge").glob("*.npz")):
            z = np.load(p)
            pts = z["pts"].astype(np.float32).copy()
            pts[~np.isfinite(pts[..., 2])] = np.nan
            fx, fy = float(z["fx"]), float(z["fy"])
            ego = None
            try:
                from maaracing_master.plugins.speedrush.depth_geo import \
                    DepthRoadObserver
                ego = DepthRoadObserver._load_ego_mask()
            except Exception:  # noqa: BLE001
                pass
            rd = reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
            from maaracing_master.plugins.speedrush.depth_geo import _hgt_rgb
            X, Y, Z = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
            coef = _fit_road_plane(X, Y, Z, _dig_band(ego, pts.shape[1])
                                   if ego is not None else None, fy=fy)
            hm = np.zeros((DIAG_Y1 - Y0, pts.shape[1], 3), np.uint8)
            if coef is not None:
                a_, b_, c_ = map(float, coef)
                sgn = -1.0 if b_ * 5.0 + c_ > 0 else 1.0
                road_y = sgn * (Y - (a_ * X + b_ * Z + c_))
                ok = np.isfinite(road_y) & (road_y <= 1.2)
                hm[ok] = _hgt_rgb(road_y[ok])
            img = np.vstack([np.zeros((Y0, pts.shape[1], 3), np.uint8),
                             hm.astype(np.uint8),
                             np.zeros((pts.shape[0] - DIAG_Y1, pts.shape[1], 3),
                                      np.uint8)])
            line = _overlay(img, pts, fy, rd, ego, None)
            cv2.putText(img, f"L {rd.left_edge_lane} R {rd.right_edge_lane}",
                        (8, 26), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 255, 255), 2)
            cv2.imwrite(str(out / f"gold_{p.stem}_refline.jpg"), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
            print(f"gold/{p.stem}: {line}")
        print(f"输出 → {out.resolve()}")
        return

    if a.frames:
        from maaracing_master.plugins.speedrush.module import (
            DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE, _yolo_object_mask)
        from maaracing_master.plugins.speedrush.perception import StreetPerception
        sess = load_session(DEPTH_MODEL_FILE)
        perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
        from maaracing_master.plugins.speedrush.depth_geo import DepthRoadObserver
        ego = DepthRoadObserver._load_ego_mask()
        frames = sorted(Path(a.frames, "frames").glob("*.jpg"))[::a.every]
        tag = Path(a.frames).name
        print(f"{a.frames}: 采样 {len(frames)} 帧（每 {a.every} 帧）")
        for jpg in frames:
            rgb = cv2.cvtColor(cv2.imread(str(jpg)), cv2.COLOR_BGR2RGB)
            result = perc.detect(rgb, frame_id=0, ts_ns=0)
            obj = _yolo_object_mask(result)
            pts, fx, fy, _ev = infer_points(sess, rgb, with_evidence=True)
            rd = reading_from_points(pts, fx, cal, ego_mask=ego,
                                     object_mask=obj, fy=fy)
            img = render_depth_debug(
                rgb, {"pts": _ev["pts"], "valid": _ev["valid"],
                      "fx": _ev["fx"], "fy": _ev["fy"]},
                rd, ego, obj, None)
            line = _overlay(img, pts, fy, rd, ego, obj)
            cv2.imwrite(str(out / f"{tag}_{jpg.stem}_refline.jpg"), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
            print(f"{tag}/{jpg.stem}: {line}")
        print(f"输出 → {out.resolve()}")
        return

    for ev in sorted(Path(a.root).glob("depth_debug_*/d*_evid.npz")):
        stem = Path(str(ev)[:-len("_evid.npz")])
        run = stem.parent.name.replace("depth_debug_", "")
        try:
            pts, fx, fy, ego, obj, evid = _load_evid(stem)
            rd = reading_from_points(pts, fx, cal, ego_mask=ego,
                                     object_mask=obj, fy=fy)
            frame_rgb = cv2.cvtColor(cv2.imread(str(stem) + ".jpg")[:FULL_H],
                                     cv2.COLOR_BGR2RGB)
            img = render_depth_debug(frame_rgb, evid, rd, ego, obj, None)
            line = _overlay(img, pts, fy, rd, ego, obj)
        except Exception as exc:  # noqa: BLE001 —— 单帧失败不挡批量
            print(f"{stem.name}: FAIL {exc!r}")
            continue
        cv2.imwrite(str(out / f"{run}_{stem.name}_refline.jpg"), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 88])
        print(f"{run}/{stem.name}: {line}")
    print(f"输出 → {out.resolve()}")


if __name__ == "__main__":
    main()
