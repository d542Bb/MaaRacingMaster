# -*- coding: utf-8 -*-
"""贴栏弯道普查（2026-10-11）：p2 局 raw 帧序号 >100 的逐帧产线同链复算
（infer_points → reading_from_points → 质量门），检验「贴左/右栏过弯都能触发
拟合失败」的方向对称性。逐帧一行 CSV（增量落盘，可断点续跑）；拒帧另存
pc3d_debug HTML。左右半幅内点率分开统计——右半崩=贴右栏型，左半崩=贴左栏型。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_curve_census.py \
        --out tools/experiments/speedrush_vision/curve_census_out/census.csv \
        raw1.jpg raw2.jpg ...
"""
from __future__ import annotations

import argparse
import base64
import math
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from tools.experiments.speedrush_vision import compare_depth_sources as cds  # noqa: E402

OUT_HTML = cds.OUT.parent / "pc3d_debug"
MODEL = (Path(ROOT) / "maaracing_master" / "plugins" / "speedrush" / "resources"
         / "onnx" / "depth" / "moge2_vits_static_336x598_t1032_q4f16.onnx")

HEADER = ("session,seq,fx,roll,pitch,cam_h,rate,resid_cm,verdict,sides,"
          "L_lane,R_lane,rate_L,rate_R,sol_resid,ms")


def halves(pts, coef) -> tuple[float, float]:
    """质量域内左/右半幅（X<0 / X>=0）内点率分开统计。"""
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    a, b, c = map(float, coef)
    hgt = Y - (a * X + b * Z + c)
    dig = ((np.abs(X) < dg.EGO_ABS_X) & (Z < dg.EGO_ABS_Z)) & (hgt > dg.OBJ_KNIFE_HGT)
    road = (np.isfinite(hgt) & (np.abs(X) < dg.ROAD_X_MAX)
            & (Z >= dg.ROAD_Z_LO) & (Z < dg.PLANE_QUALITY_Z_HI) & (~dig))
    inl = road & (np.abs(hgt) < dg.PLANE_TOL)
    rl = float(inl[X < 0].sum()) / max(int(road[X < 0].sum()), 1)
    rr = float(inl[X >= 0].sum()) / max(int(road[X >= 0].sum()), 1)
    return rl, rr


def html_for(name: str, pts, frame, fy) -> None:
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()
    s = cds._worldize(
        name, pts[..., 0], pts[..., 1], pts[..., 2], frame,
        np.zeros(frame.shape[:2], bool), None, 6,
        note="贴栏弯道普查 · 拒帧留证",
        fit_fn=lambda X, Y, Z, excl: dg._fit_road_plane(X, Y, Z, fy=fy))
    js = ('{name:"' + s["name"] + '",hud:`' + s["hud"] + '`,'
          'xyz:f32(`' + b64(s["xyz"]) + '`),rgb:u8(`' + b64(s["rgb"]) + '`),'
          'ht:f32(`' + b64(s["ht"]) + '`),bd:u8(`' + b64(s["bd"]) + '`)}')
    html = (cds.HTML.replace("__STEM__", name).replace("__SRC__", "[" + js + "]")
            .replace("__TW__", "480"))
    OUT_HTML.mkdir(parents=True, exist_ok=True)
    (OUT_HTML / f"{name}_debug.html").write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--list", dest="list_file", default=None,
                    help="帧路径清单文件（每行一个）；Windows 命令行长度超限时用")
    ap.add_argument("raws", nargs="*")
    args = ap.parse_args()
    raws = list(args.raws)
    if args.list_file:
        raws += [ln.strip() for ln in Path(args.list_file).read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if outp.exists():
        for ln in outp.read_text(encoding="utf-8").splitlines()[1:]:
            parts = ln.split(",")
            if len(parts) >= 2:
                done.add((parts[0], parts[1]))
        print(f"续跑：已有 {len(done)} 行", flush=True)
    if not outp.exists():
        outp.write_text(HEADER + "\n", encoding="utf-8")

    cal = load_calib()
    sess = dg.load_session(MODEL)
    nan = float("nan")
    for raw in raws:
        raw = Path(raw)
        session = raw.parent.parent.name
        seq = raw.stem.split("_")[0]
        if (session, seq) in done:
            continue
        frame = cv2.cvtColor(cv2.imread(str(raw)), cv2.COLOR_BGR2RGB)
        try:
            pts, fx, fy, evid = dg.infer_points(sess, frame, with_evidence=True)
        except Exception as e:  # noqa: BLE001 —— 单帧失败记录后继续
            with outp.open("a", encoding="utf-8") as f:
                f.write(f"{session},{seq},ERR,{type(e).__name__}\n")
            continue
        reading = dg.reading_from_points(pts, fx, cal, None, fy=fy)
        sol = (f"{float(evid['sol_resid']):.3f}"
               if "sol_resid" in evid else "")
        if reading.coef is not None:
            rl, rr = halves(pts, reading.coef)
            a, b, c = map(float, reading.coef)
            roll, pitch = math.degrees(math.atan(a)), math.degrees(math.atan(b))
            X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
            hgt = Y - (a * X + b * Z + c)
            road = (np.isfinite(hgt) & (np.abs(X) < dg.ROAD_X_MAX)
                    & (Z >= dg.ROAD_Z_LO) & (Z < dg.PLANE_QUALITY_Z_HI))
            inl = road & (np.abs(hgt) < dg.PLANE_TOL)
            rate = float(inl.sum()) / max(int(road.sum()), 1)
            resid = float(np.nanmedian(np.abs(hgt[road]))) * 100
            cam_h = abs(c)
            verdict = "过"
        else:
            roll = pitch = cam_h = rate = resid = rl = rr = nan
            fail = ";".join(reading.rejects) or "拟合失败"
            verdict = "拟合失败" if ("失败" in fail or "不足" in fail) else "拒"
        lane = lambda v: f"{v:.2f}" if v is not None else ""
        row = (f"{session},{seq},{fx:.1f},{roll:.2f},{pitch:.2f},{cam_h:.2f},"
               f"{rate:.2f},{resid:.1f},{verdict},{reading.sides},"
               f"{lane(reading.left_edge_lane)},{lane(reading.right_edge_lane)},"
               f"{rl:.2f},{rr:.2f},{sol},{float(evid['ms']):.2f}")
        with outp.open("a", encoding="utf-8") as f:
            f.write(row + "\n")
        if verdict == "拒":
            html_for(f"{session}_{seq}", pts, frame, fy)
        print(f"{session} {seq}: {verdict} rate={rate:.2f} L/R={rl:.2f}/{rr:.2f}",
              flush=True)


if __name__ == "__main__":
    main()
