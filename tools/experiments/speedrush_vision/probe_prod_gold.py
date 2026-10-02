# -*- coding: utf-8 -*-
"""产线 3D 找边引擎的金标回归（深度几何 v4 换装验收）。

与 probe_boundary3d 的区别：那条探针实现自己的管线（实验区平面拟合+找边），
本探针**只调产线代码**（depth_geo.load_session/infer_points/reading_from_points
+ gate0 标定），回答三个验收问题：
  1. 各金标帧的逐箱检出与金标边线的米制差（直路帧 ≤1m 口径）；
  2. lane_w_m 定案数据：可信帧 z=8m 剖面的 L/R 路宽（÷4=车道宽）；
  3. cam_h（|c|）跨帧稳定性——逐帧焦距仲裁的持续监视口径。
  4. **换装差分**（2026-10-02 换装后口径）：产线面 = reading_from_points 本体
     （特征种子 + 走廊收敛，含自车矩形挖除）；回退面 = _fit_road_plane fy=None
     的旧位置圈地路径。差分回答「换装带来什么」；对质锚用产线面射线（锚一致
     才可比）。注：实验区混合复刻与产线版差一处（自车挖除），以产线版为准。

对质几何：金标像素线两端点经**产线平面**射线求交得 (X, Z)（同帧 fx/fy，
禁静态内参——第二信源教训），对质 z 以近点为锚夹进检出覆盖域后线性插值。
用法（仓库根，.venv）：python tools/experiments/speedrush_vision/probe_prod_gold.py
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402

GOLD = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/"
            r"depth_review/gold_labels.csv")


def pick_frames(rows, dedup_class: bool = False):
    """金标帧集：默认**全量**。按 (lcls,rcls) 去重只留 6 帧会把同类的难帧静默
    丢掉——000660（雨天湿面、滚转 −7.3°）与 000100 同类，整轮 v4 验收因此没看过
    它，而它恰是产线读数塌掉的那一帧（2026-10-02 实测）。`--dedup-class` 恢复
    旧的冒烟口径。"""
    if not dedup_class:
        return list(rows)
    seen: dict = {}
    order = []
    for r in rows:
        key = (r["lcls"], r["rcls"])
        if key not in seen:
            seen[key] = r
            order.append(r)
    for want in ("curve_cont", "obstacle"):
        extra = [r for r in rows if r["stratum"] == want and r not in order]
        if extra:
            order.append(extra[0])
    return order[:6]


def gold_ray_ground(u, v, coef, fx, fy, cx, cy):
    """金标像素射线与产线路面平面求交 → (X, Z)；近水平/不在前方 nan。"""
    a, b, c = coef
    denom = (v - cy) / fy - a * (u - cx) / fx - b
    if abs(denom) < 1e-4 or c == 0:
        return np.nan, np.nan
    z = c / denom
    if not (1.0 < z < 200):
        return np.nan, np.nan
    return (u - cx) * z / fx, z


def eval_at(samples, z: float):
    if len(samples) < 2:
        return np.nan
    zs = np.array([s[0] for s in samples])
    if z < zs.min() - 1.0 or z > zs.max() + 1.0:
        return np.nan
    order = np.argsort(zs)
    return float(np.interp(z, zs[order], np.array([s[1] for s in samples])[order]))


def main() -> None:
    rows = list(csv.DictReader(GOLD.open(encoding="utf-8")))
    ap = argparse.ArgumentParser()
    ap.add_argument("--dedup-class", action="store_true",
                    help="按 (lcls,rcls) 去重只留 6 帧（旧冒烟口径）")
    frames = pick_frames(rows, ap.parse_args().dedup_class)
    cal = load_calib()
    sess = dg.load_session(DEPTH_MODEL_FILE)
    ego = dg.DepthRoadObserver._load_ego_mask()
    print(f"lane_w_m={cal.lane_w_m}  frames={len(frames)}")
    print(f"{'帧':26s} {'侧':2s} {'类':10s} {'对质Z':>6s} {'产线差m':>8s} {'回退差m':>8s} {'探针差m':>8s}")
    errs = []
    errs_leg = []
    errs_eph = []
    widths = []
    cam_hs = []
    for r in frames:
        stem = Path(r["path"]).stem
        img = cv2.cvtColor(cv2.imread(r["path"]), cv2.COLOR_BGR2RGB)
        pts, fx, fy = dg.infer_points(sess, img)
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
        # 差分对照：同一份点云，产线平面 vs 实验区平面，各自走产线找边
        X, Y, Z = pts[dg.Y0:dg.DIAG_Y1, :, 0], pts[dg.Y0:dg.DIAG_Y1, :, 1], pts[dg.Y0:dg.DIAG_Y1, :, 2]
        dig = dg._dig_band(ego, pts.shape[1])
        coef = dg._fit_road_plane(X, Y, Z, dig)
        if coef is None:
            print(f"{stem}  平面拟合失败（产线）")
            continue
        a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
        cam_hs.append(abs(c))
        coef_e = eph.fit_road_plane(X, Y, Z, ego[dg.Y0:dg.DIAG_Y1])
        rd_e = None
        if coef_e is not None:
            rd_e = _reading_with_coef(X, Y, Z, coef_e, fx, cal, dig, pts.shape)
        # 回退面 = 旧位置圈地路径（fy=None 开关），差分回答「换装带来什么」
        coef_l = dg._fit_road_plane(X, Y, Z, dig)
        rd_l = (_reading_with_coef(X, Y, Z, coef_l, fx, cal, dig, pts.shape)
                if coef_l is not None else None)
        for key, side in (("l", -1), ("r", 1)):
            cls = r[f"{key}cls"]
            smp = [(zc, x) for s, zc, x in rd.edge_pts if s == side]
            smp_e = ([(zc, x) for s, zc, x in rd_e["edge_pts"] if s == side]
                     if rd_e is not None else [])
            smp_l = ([(zc, x) for s, zc, x in rd_l["edge_pts"] if s == side]
                     if rd_l is not None else [])
            u_n, v_n = float(r[f"{key}_nx"]), float(r[f"{key}_ny"])
            u_f, v_f = float(r[f"{key}_fx"]), float(r[f"{key}_fy"])
            xn, zn = gold_ray_ground(u_n, v_n, coef, fx, fy, pts.shape[1] / 2, pts.shape[0] / 2)
            if not np.isfinite(xn):
                print(f"{stem:26s} {key.upper():2s} {cls:10s}   —   金标射线不可评")
                continue
            e_prod = _eval_err(smp, xn, zn)
            e_eph = _eval_err(smp_e, xn, zn) if smp_e else np.nan
            e_leg = _eval_err(smp_l, xn, zn) if smp_l else np.nan
            if cls in ("wall", "kerb") and np.isfinite(e_prod):
                errs.append((stem, key, cls, e_prod))
            if cls in ("wall", "kerb") and np.isfinite(e_eph):
                errs_eph.append((stem, key, cls, e_eph))
            if cls in ("wall", "kerb") and np.isfinite(e_leg):
                errs_leg.append((stem, key, cls, e_leg))
            fmt = lambda e: f"{e:8.2f}" if np.isfinite(e) else f"{'—':>8s}"
            print(f"{stem:26s} {key.upper():2s} {cls:10s} {zn:6.1f} {fmt(e_prod)} {fmt(e_leg):>8s} {fmt(e_eph):>8s}")
        l8 = [x for s, zc, x in rd.edge_pts if s == -1 and abs(zc - 8) < 1.6]
        r8 = [x for s, zc, x in rd.edge_pts if s == 1 and abs(zc - 8) < 1.6]
        if l8 and r8:
            widths.append(r8[0] - l8[0])
        lt = "-" if rd.left_edge_lane is None else f"{rd.left_edge_lane:+.2f}"
        rt = "-" if rd.right_edge_lane is None else f"{rd.right_edge_lane:+.2f}"
        rej = ";".join(rd.rejects) or "—"
        print(f"    读数 L{lt} R{rt} sides={rd.sides} rejects={rej} fx={fx:.0f} cam_h={abs(c):.2f}")
    print("\n== 汇总 ==")
    for name, ee in (("产线面", errs), ("回退面", errs_leg), ("探针面", errs_eph)):
        if ee:
            abs_errs = [abs(e) for *_, e in ee]
            over = [f"{s}/{k}" for s, k, _c, e in ee if abs(e) > 1.0]
            print(f"{name}: n={len(ee)} 中位={np.median(abs_errs):.2f}m "
                  f"最大={max(abs_errs):.2f}m 超1m={over or '无'}")
    if widths:
        print(f"z=8m 路宽: n={len(widths)} 中位={np.median(widths):.2f}m "
              f"→ lane_w_m 候选={np.median(widths) / 4:.3f}")
    if cam_hs:
        print(f"cam_h: n={len(cam_hs)} 中位={np.median(cam_hs):.2f}m "
              f"域=[{min(cam_hs):.2f}, {max(cam_hs):.2f}]")


def _eval_err(smp, xn, zn):
    if not smp:
        return np.nan
    zmin = min(s[0] for s in smp)
    zmax = max(s[0] for s in smp)
    zj = min(max(zn, zmin), zmax)
    fx_e = eval_at(smp, zj)
    return fx_e - xn if np.isfinite(fx_e) else np.nan


def _reading_with_coef(X, Y, Z, coef, fx, cal, dig, shape):
    """与 reading_from_points 相同的找边/聚合，平面系数外注入（差分开关）。"""
    a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Y - (a * X + b * Z + c))
    sky = np.isfinite(hgt) & (hgt > dg.SKY_HGT)
    extent = (shape[1] / 2.0) / fx
    edge_pts = []
    for z0, z1 in dg.ZBIN:
        m = ((Z >= z0) & (Z < z1) & np.isfinite(X) & np.isfinite(hgt)
             & (~dig) & (~sky))
        if m.sum() < dg.MIN_BOX_PTS:
            continue
        zc = (z0 + z1) / 2
        for side in (1, -1):
            ex = dg._scan_side(X[m], hgt[m], side, zc, extent)
            if np.isfinite(ex):
                edge_pts.append((side, zc, ex))

    def _side(side):
        smp = [(zc, x) for s, zc, x in edge_pts if s == side]
        if not smp:
            return None
        return float(np.median([x / cal.lane_w_m for _, x in smp]))
    return {"edge_pts": edge_pts, "l": _side(-1), "r": _side(1)}


if __name__ == "__main__":
    main()
