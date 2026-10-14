# -*- coding: utf-8 -*-
"""路径预估渲染蒙太奇探针（临时，2026-10-14）：录制证据包 + 同拍 trace 数据
→ render_depth_debug 路径预估叠加逐帧核验。

数据流：录制会话 depth_debug/d*_evid.npz（点云+焦距）+ trace_*.jsonl（同拍
fid 的 road_offset/cor_w/executed/ledger 币车行）→ 重建走廊带（空气墙近似，
录制 ro_off/cor_w）→ corridor_reach.evaluate 收益场 → cr_path 自车系 →
render_depth_debug 蒙太奇。验证面：路径点画在哪、深度@距离标注、决策带
cr 摘要——「渲染代码路径对不对」人眼核验，不验决策质量（那是 shadow/control
实机 A/B 的事）。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/probe_reach_render.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush.corridor_reach import (
    CarSource, CoinSource, CorridorBandSpan, ReachParams, evaluate)
from maaracing_master.plugins.speedrush.depth_geo import (
    DepthRoadReading, render_depth_debug)

FULL_W, FULL_H = 1280, 720
SESS = Path(os.path.expandvars(
    r"%APPDATA%\MaaRacingMaster\debug\speedrush\20261014_112029_p2"))
OUT = Path(__file__).resolve().parent / "reach_render_out"
STRIDE = 12                    # 每隔多少帧取一张（证据包 ≈ 3fps 落盘，~69 包）


def load_trace() -> dict[int, dict]:
    """trace jsonl → fid 行索引（road_offset/cor_w/executed/ledger）。"""
    rows = {}
    for fp in glob.glob(str(SESS / "trace_*.jsonl")):
        with open(fp, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                rows[int(r["fid"])] = r
    return rows


def load_evid(stem: Path):
    z = np.load(str(stem) + "_evid.npz")
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    evid = {"pts": pts_n, "valid": valid,
            "fx": float(z["fx"]), "fy": float(z["fy"]),
            "ms": float(z["ms"]), "fid": int(z["fid"])}
    img = cv2.imread(str(stem) + "_frame.jpg")
    return evid, cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def reach_path_for(row: dict, lane_w: float) -> tuple | None:
    """同拍 trace 行 → 收益场路径（(cr_path 自车系, cr_best)）；走廊/币缺席
    返回 None。走廊=空气墙近似（录制 ro_off/cor_w，全带同缘——重放只验渲染
    代码路径，不验决策）。"""
    lwm = row.get("cor_w")
    ro = row.get("road_offset")
    if lwm is None or ro is None:
        return None
    exec_lane = row.get("executed_lane") or 0.0
    led = row.get("ledger") or {}
    shift = exec_lane * lane_w
    center = -ro * lane_w                  # 自车系米
    width = lwm * lane_w
    bands = [CorridorBandSpan(3.0 + i * 3.0, 3.0 + (i + 1) * 3.0,
                              center - width / 2.0 + shift,
                              center + width / 2.0 + shift)
             for i in range(9)]
    coins = [CoinSource(x_m=(c["x"] + exec_lane) * lane_w,
                        observed_count=c.get("n", 1), conf=c.get("conf", 0.5),
                        t_miss_s=c.get("t_miss"))
             for c in led.get("coins", []) if c.get("x") is not None]
    cars = [CarSource(x_m=(c["x"] + exec_lane) * lane_w,
                      t_meet_s=c.get("t_mtr") or c.get("t_meet"))
            for c in led.get("cars", []) if c.get("x") is not None]
    p = ReachParams(need_of_dx_m=lambda dx: 0.3 + 0.4 * dx / lane_w)
    rep = evaluate(bands, coins, cars, shift, p)
    if rep is None:
        return None
    path = [(z, x - shift) for z, x in rep.path]
    return path, rep.best_x_m - shift


def main() -> None:
    rows = load_trace()
    stems = sorted((SESS / "depth_debug").glob("d*_evid.npz"))
    OUT.mkdir(parents=True, exist_ok=True)
    n_out = 0
    for stem in stems[::STRIDE]:
        mp = Path(str(stem).replace("_evid.npz", "_mask.npy"))
        evid, frame = load_evid(Path(str(stem).replace("_evid.npz", "")))
        fid = evid["fid"]
        row = next((rows[f] for f in range(fid, fid + 30) if f in rows), None)
        if row is None:
            continue
        lane_w = 3.38
        r = reach_path_for(row, lane_w)
        note = {"fid": fid, "state": "CRUISE", "reason": "probe:reach",
                "steer": 0.0, "elane": row.get("executed_lane") or 0.0}
        if r is not None:
            note["cr_path"] = [[z, round(x, 3)] for z, x in r[0]]
            note["cr_best"] = round(r[1], 3)
        reading = DepthRoadReading(
            row.get("dgeo_left"), row.get("dgeo_right"), None, None,
            2 if row.get("dgeo_sides") else 0, 0.0,
            fx=float(evid["fx"]) * FULL_W, fy=float(evid["fy"]) * FULL_H)
        mask = None
        if mp.exists():
            mask = np.unpackbits(np.load(str(mp)))[: FULL_W * FULL_H] \
                .reshape(FULL_H, FULL_W).astype(bool)
        img = render_depth_debug(frame, evid, reading, mask, note)
        out = OUT / f"reach_{stem.stem}.jpg"
        cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        n_out += 1
        print(f"{out.name}  fid={fid}  "
              f"{'cr_best=%.2f' % r[1] if r else '无走廊/币（xt 路径）'}")
    print(f"共 {n_out} 张 → {OUT}")


if __name__ == "__main__":
    main()
