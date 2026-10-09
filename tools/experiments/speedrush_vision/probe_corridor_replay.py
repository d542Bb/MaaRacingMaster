# -*- coding: utf-8 -*-
"""分带走廊影子重放（第 6 步验收）：83 包证据 → 同一套当前读数喂新旧两条
供数路径，逐帧对账。

**它对什么账**：旧路径复刻（第 6 步前语义，master=区读数当主人、区弃权走
0.4s 槽/None 不回退）vs 新路径（corridor：墙实体路径+区兜底+W±20%）。同一
份 reading/dgrid 进两条路——差异只来自供数路径本身，读数变化（第 1~5 步）
不混进对账。trace 的 road_offset 列仅作旧实机运行的旁证（那是旧代码+旧读数）。

**必须喂 ego+objects 掩码**（评审硬要求）：物体框不带掩码=旧故事。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_corridor_replay.py \
        --dir "%APPDATA%/MaaRacingMaster/data/speedrush/control_traces/depth_debug_20261008_183813" \
        --trace "%APPDATA%/MaaRacingMaster/data/speedrush/control_traces/trace_20261008_183813_p2.jsonl"
"""
from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.plugins.speedrush.depth_geo import (
    reading_from_points, drivable_grid_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib

FULL_W, FULL_H = 1280, 720
OFF_MAX = 3.0
SLOT_TTL_S = 0.4


def load_packet(stem: Path, cal):
    """evid npz（stem 已含 _evid.npz 后缀）→ (reading, dgrid, fid)。
    掩码口径与 replay_debug_evid 同源。"""
    z = np.load(stem)
    fid = int(z["fid"])
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid_full] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H
    base = str(stem)[: -len("_evid.npz")]
    ego = obj = None
    if Path(base + "_ego.npy").exists():
        ego = np.unpackbits(np.load(base + "_ego.npy"))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        mp = Path(base + "_mask.npy")
        if mp.exists():
            obj = np.unpackbits(np.load(mp))[: FULL_W * FULL_H] \
                .reshape(FULL_H, FULL_W).astype(bool) & ~ego
    rd = reading_from_points(pts, fx, cal, ego_mask=ego, object_mask=obj, fy=fy)
    g = drivable_grid_from_points(pts, fx, fy, ego=ego, obj=obj)
    return rd, g, fid


class OldPathReplica:
    """第 6 步前语义复刻（master 生产口径）：区读数(sides==2)当主人 →
    0.4s 保鲜槽 → None（不回退找边）。门禁/中值窗随旧口径。"""

    def __init__(self, lane_w_m: float) -> None:
        self.lw = lane_w_m
        self._slot = None
        self._win: deque = deque(maxlen=3)
        self._w: deque = deque(maxlen=200)

    def update(self, rd, g, now: float) -> tuple[str, float | None]:
        # master：区读数当主人
        from maaracing_master.plugins.speedrush.depth_geo import region_reading
        reg = region_reading(g, self.lw) if g is not None else None
        ok = reg is not None and reg.sides == 2 \
            and reg.left_edge_lane is not None and reg.right_edge_lane is not None
        if ok:
            el, er = reg.left_edge_lane, reg.right_edge_lane
            if abs((el + er) / 2.0) <= OFF_MAX:
                formed = (float(np.median(self._w))
                          if len(self._w) >= 30 else None)
                if formed is None or \
                        abs((er - el) - formed) <= 0.4 * formed:
                    self._win.append((el, er))
                    if len(self._win) >= 3:
                        el = sorted(w[0] for w in self._win)[1]
                        er = sorted(w[1] for w in self._win)[1]
                    self._slot = (el, er, now)
                    self._w.append(er - el)
                    return "region", -((el + er) / 2.0)
            return "region", None
        s = self._slot
        if s is not None and now - s[2] <= SLOT_TTL_S:
            return "region_slot", -((s[0] + s[1]) / 2.0)
        return "none", None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--trace", required=True)
    args = ap.parse_args()
    cal = load_calib()
    lw = cal.lane_w_m

    with open(args.trace, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    ts = {r["fid"]: r.get("ts_ns", 0) / 1e9 for r in rows if r.get("fid")}
    trace_ro = {r["fid"]: r.get("road_offset") for r in rows if r.get("fid")}

    from maaracing_master.plugins.speedrush.module import _EgoRoadObserver
    from maaracing_master.plugins.speedrush.wall_track import WallTrack
    from types import SimpleNamespace

    old = OldPathReplica(lw)
    # 新路径双模式：master=部署口径（decision.json mode=master，区读数当主人）、
    # off=缺省口径（找边对当主人）——逐帧各对一遍账
    new_m = _EgoRoadObserver(xcheck=SimpleNamespace(mode="master", tol_lane=0.45,
                                                    bypass_streak=8),
                             lane_w_m=lw, wall_track=WallTrack(cal))
    new_o = _EgoRoadObserver(lane_w_m=lw, wall_track=WallTrack(cal))

    stems = sorted(Path(args.dir).glob("*_evid.npz"))
    n_up = n_regress = n_val = 0
    n_src = {}
    print(f"{'fid':>6} {'sides':>5} {'old':>11} {'m_src':>9} {'m_off':>7} "
          f"{'o_src':>9} {'o_off':>7}  note")
    for stem in stems:
        rd, g, fid = load_packet(stem, cal)
        if fid < 0:
            print(f"{'-':>6} {'-':>5}  （fid=-1 捕获失败，跳过）")
            continue
        now = ts.get(fid, 0.0)
        o_src, o_off = old.update(rd, g, now)
        cm = new_m.update(rd, new=True, grid=g, fid=fid, now=now)
        co = new_o.update(rd, new=True, grid=g, fid=fid, now=now)
        n_src[o_src] = n_src.get(o_src, 0) + 1
        m_off = None if cm is None else -cm.center_lane
        d_off = None if co is None else -co.center_lane
        note = ""
        if o_off is None and m_off is not None:
            n_up += 1
            note = "新供数（旧黑视）"
        elif o_off is not None and m_off is None:
            n_regress += 1
            note = "新弃权（须归因）"
        elif o_off is not None and m_off is not None \
                and abs(o_off - m_off) > 0.1:
            n_val += 1
            note = f"值分歧（trace 旧实机={trace_ro.get(fid)}）"
        fmt = lambda v: 'None' if v is None else format(v, '.3f')
        print(f"{fid:>6} {rd.sides:>5} {o_src + ('/' + format(o_off, '.2f') if o_off is not None else ''):>14} "
              f"{cm.src if cm is not None else 'none':>9} {fmt(m_off):>7} "
              f"{co.src if co is not None else 'none':>9} {fmt(d_off):>7}  {note}")
    print(f"\n汇总：新供数 {n_up} 帧（旧黑视）、新弃权 {n_regress} 帧（须归因）、"
          f"值分歧 {n_val} 帧（>0.1 道，master 口径）；旧路径来源分布 {n_src}")


if __name__ == "__main__":
    main()
