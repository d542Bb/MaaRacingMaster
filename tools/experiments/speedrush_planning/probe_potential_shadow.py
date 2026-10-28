# -*- coding: utf-8 -*-
"""人工局影子势场回放（C 类实验，不改生产）。

用 demos 的人工帧重放当前感知/深度/走廊链，逐拍记录走廊、车/币/奖励、
人工手柄与横向位置；再在连续横位网格上算一个多目标势场，做消融，并与
人工未来 0.5s 的横向位移对照。

回答的问题：近场连续势场有没有足够信号；远场流向项是提供方向还是只添
噪声；势场目标是否比当前「目标点+死区」更接近人的实际走线。

用法（仓库根，.venv）:
    python tools/experiments/speedrush_planning/probe_potential_shadow.py \
        --sess manual_20261019_173132_p2 --start 100 --limit 300 --depth-every 2
输出（数据目录，不入库）:
    <APPDATA>/MaaRacingMaster/data/speedrush/potential_shadow_<sess>.jsonl
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE)
from maaracing_master.plugins.speedrush.coin_group import (  # noqa: E402
    CoinGroupAggregator)
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.decision import DecisionEngine  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DepthRoadObserver, load_session)
from maaracing_master.plugins.speedrush.module import (  # noqa: E402
    _EgoRoadObserver, _yolo_car_boxes, _yolo_object_mask)
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)
from maaracing_master.plugins.speedrush.tracking import Tracker  # noqa: E402
from maaracing_master.plugins.speedrush.traffic import TrafficObserver  # noqa: E402
from maaracing_master.plugins.speedrush.wall_track import WallTrack  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = DATA / "demos"

X_GRID = np.arange(-5.0, 5.0001, 0.05)
W_CENTER = 0.05
W_CAR = 1.0
W_COIN = 0.6
W_REWARD = 1.5
W_FAR = 0.15
WALL_MARGIN_LANE = 0.35
HUMAN_HORIZON_S = 0.5


def heading_obs(corridor):
    """近场/远场道路中心线斜率（度，车头系），航向误差的观测量。

    车头与道路对齐时近场斜率≈0；斜率变化是车头先行量。far 用 12m 外的
    带单独拟合，near/far 之差近似道路曲率项。"""
    pts = []
    for b in corridor.bands:
        if b.left is None or b.right is None:
            continue
        lx, rx = b.left.x_m, b.right.x_m
        if lx is None or rx is None or b.z1 <= 0.0:
            continue
        pts.append((0.5 * (b.z0 + b.z1), 0.5 * (lx + rx)))

    def fit(zlo, zhi):
        q = [p for p in pts if zlo <= p[0] <= zhi]
        if len(q) < 2:
            return None
        n = len(q)
        sz = sum(a for a, _ in q)
        sx = sum(b for _, b in q)
        szz = sum(a * a for a, _ in q)
        szx = sum(a * b for a, b in q)
        den = n * szz - sz * sz
        if abs(den) < 1e-9:
            return None
        return math.degrees(math.atan((n * szx - sz * sx) / den))

    a1 = a2 = resid = None
    if len(pts) >= 4:
        A = np.array([[1.0, z, z * z] for z, _ in pts])
        y = np.array([x for _, x in pts])
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        a1 = math.degrees(math.atan(coef[1]))
        a2 = float(coef[2])
        resid = float(np.sqrt(((y - A @ coef) ** 2).mean()))

    return fit(0.0, 9.0), fit(12.0, 30.0), len(pts), a1, a2, resid


def nearest_pad(ts: int, pad_ts: list[int], pad_lx: list[int]) -> int:
    i = bisect.bisect_left(pad_ts, ts)
    if i <= 0:
        return pad_lx[0]
    if i >= len(pad_ts):
        return pad_lx[-1]
    return pad_lx[i - 1] if ts - pad_ts[i - 1] <= pad_ts[i] - ts \
        else pad_lx[i]


def potential(scene, corridor, x_grid: np.ndarray, *,
              far=True, cars=True, coins=True, rewards=True) -> float | None:
    """连续横位势能（选择帧，米）：硬走廊 + 弱居中 + 车斥力 + 币/奖励引力
    + 远场流向。返回势能最小横位；无可行域 None。"""
    lane_w = scene.lane_w_m
    margin = WALL_MARGIN_LANE * lane_w
    u = W_CENTER * (x_grid / lane_w) ** 2
    hard = np.zeros(x_grid.shape, dtype=bool)
    for b in scene.road.bands:
        if b.left is None or b.right is None or b.z0 >= 24.0 or b.z1 <= 0.0:
            continue
        hard |= (x_grid < b.left + margin) | (x_grid > b.right - margin)
    for o in scene.objs:
        if not o.in_corridor:
            continue
        if o.kind == "car" and cars:
            t = o.t_arrive_s if o.t_arrive_s is not None else (
                o.z_m / 40.0 if o.z_m is not None else None)
            if t is None or not (0.0 <= t <= 3.0):
                continue
            sig = max(0.8, o.sigma_m + 0.6)
            w = W_CAR / (0.5 + t) * (0.5 if o.coarse else 1.0)
            u += w * np.exp(-0.5 * ((x_grid - o.x_m) / sig) ** 2)
        elif o.kind == "coin" and coins:
            t = o.t_arrive_s
            if t is None or not (0.0 <= t <= 2.0):
                continue
            sig = 0.8 * lane_w
            u -= W_COIN * max(1.0, o.count) * np.exp(
                -0.5 * ((x_grid - o.x_m) / sig) ** 2)
        elif o.kind == "reward" and rewards:
            t = o.t_arrive_s
            if t is None or not (0.0 <= t <= 3.0):
                continue
            sig = 0.8 * lane_w
            u -= W_REWARD * np.exp(-0.5 * ((x_grid - o.x_m) / sig) ** 2)
    if far and corridor is not None and corridor.far_hint_lane is not None \
            and corridor.far_hint_age_ms is not None \
            and corridor.far_hint_age_ms <= 300.0:
        u -= W_FAR * corridor.far_hint_lane * (x_grid / lane_w)
    u = np.where(hard, np.inf, u)
    return float(x_grid[int(np.argmin(u))]) if np.isfinite(u).any() else None


def run(args) -> Path:
    sess = DEMOS / args.sess
    idx = [json.loads(x) for x in
           (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines()
           if x.strip()][args.start:args.start + args.limit]
    meta = json.loads((sess / "meta.json").read_text(encoding="utf-8"))
    phase = int(meta.get("phase", 1))
    pads = [json.loads(x) for x in
            (sess / "pads.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    pad_ts = [p["ts_ns"] for p in pads]
    pad_lx = [p.get("lx", 0) for p in pads]

    cal = load_calib()
    cfg = load_decision()
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    depth = DepthRoadObserver(load_session(DEPTH_MODEL_FILE), cal)
    observer = _EgoRoadObserver(
        cfg.grid_xcheck, cal.lane_w_m, WallTrack(cal),
        mem_fallback=cfg.corridor_reach.enabled,
        far_ground_supply=cfg.geometry.far_ground_supply)
    tracker = Tracker(bridge_enabled=cfg.metric_bridge.enabled,
                      bridge_stay=cfg.metric_bridge.stay)
    agg = CoinGroupAggregator()
    traffic = TrafficObserver(cfg.traffic, conf_floor=cfg.scoring.conf_floor)
    engine = DecisionEngine(cfg)

    out_path = DATA / f"potential_shadow_{sess.name}.jsonl"
    reading = grid = anchors = None
    prev_ts = None
    rows = []
    t0 = time.perf_counter()
    with out_path.open("w", encoding="utf-8") as f:
        for k, item in enumerate(idx):
            img = cv2.imread(str(sess / "frames" / item["file"]))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            is_new = (k % max(1, args.depth_every) == 0)
            # 感知每拍都跑（与实机一致）；只有深度/走廊走 10Hz 慢变量
            per = perc.detect(rgb, frame_id=item["frame_id"],
                              ts_ns=item["ts_ns"])
            if is_new:
                mask = _yolo_object_mask(per)
                reading, evid = depth.observe_debug(
                    rgb, mask, detections=_yolo_car_boxes(per),
                    fid=item["frame_id"], frame_ts_ns=item["ts_ns"])
                grid = None if evid is None else evid.get("grid")
                anchors = None if evid is None else evid.get("anchors")
            dt = 0.05 if prev_ts is None else max(
                0.01, min(0.12, (item["ts_ns"] - prev_ts) / 1e9))
            prev_ts = item["ts_ns"]
            obs = tracker.update(per, frame_age_ms=item.get("age_ms", 0.0),
                                 stage=phase, anchors=anchors if is_new else None)
            obs = agg.update(obs)
            tviews, tevents = traffic.update(obs)
            far = () if reading is None else reading.far_extents
            corridor = observer.update(
                reading, new=is_new, grid=grid,
                fid=item["frame_id"], far_extents=far)
            road_offset = None if corridor is None else -corridor.center_lane
            human_lane = 0.0 if road_offset is None else road_offset
            out = engine.update(
                obs, dt, executed_lane=human_lane,
                traffic=(tviews, tevents), road_offset=road_offset,
                corridor=corridor)
            scene = engine.scene
            sel = engine.selection_report
            coarse = engine.coarse_report
            egom = None if scene is None else scene.ego_x_m
            pot = {}
            if scene is not None and egom is not None:
                for name, kw in (("full", {}), ("nofar", {"far": False}),
                                 ("nocar", {"cars": False}),
                                 ("nocoin", {"coins": False}),
                                 ("noreward", {"rewards": False})):
                    pot[name] = potential(scene, corridor, X_GRID, **kw)
            head = None if corridor is None else heading_obs(corridor)
            row = {
                "fid": item["frame_id"], "ts": item["ts_ns"], "dt": round(dt, 4),
                "state": out.state.value, "reason": out.reason,
                "lx": nearest_pad(item["ts_ns"], pad_ts, pad_lx),
                "ego_m": None if egom is None else round(egom, 3),
                "lane_w": None if scene is None else round(scene.lane_w_m, 3),
                "cor": None if corridor is None else [
                    round(corridor.center_lane, 3),
                    round(corridor.width_lane, 3), corridor.src,
                    None if corridor.far_hint_lane is None
                    else round(corridor.far_hint_lane, 3),
                    None if corridor.far_hint_age_ms is None
                    else round(corridor.far_hint_age_ms, 1)],
                "sel": None if sel is None else [
                    sel.layer, sel.why, round(sel.target_x_m, 3)],
                "coarse": None if coarse is None else [
                    round(coarse.lane, 3), coarse.reason],
                "head": None if head is None else [
                    None if head[0] is None else round(head[0], 2),
                    None if head[1] is None else round(head[1], 2),
                    head[2],
                    None if head[3] is None else round(head[3], 2),
                    None if head[4] is None else round(head[4], 5),
                    None if head[5] is None else round(head[5], 3)],
                "objs": [] if scene is None else [
                    [o.kind, round(o.x_m, 2),
                     None if o.t_arrive_s is None else round(o.t_arrive_s, 2),
                     round(o.sigma_m, 2), round(o.age_ms, 1),
                     None if o.z_m is None else round(o.z_m, 2),
                     int(o.in_corridor), int(o.coarse)]
                    for o in scene.objs],
                "pot": pot,
            }
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    el = time.perf_counter() - t0
    print(f"{sess.name}: {len(rows)} 拍，{el:.1f}s，{len(rows)/el:.1f}fps → {out_path}")
    summarize(rows)
    return out_path


def summarize(rows) -> None:
    valid = [r for r in rows if r["ego_m"] is not None and r["pot"].get("full") is not None]
    print(f"有效势场拍 {len(valid)}/{len(rows)}")
    if not valid:
        return
    by_ts = {r["ts"]: r for r in rows}
    ts_list = sorted(by_ts)
    # 人工未来 0.5s 横向位移 vs 各势场目标/当前选择目标的方向
    def future_move(r):
        target = r["ts"] + int(HUMAN_HORIZON_S * 1e9)
        i = bisect.bisect_left(ts_list, target)
        for j in range(i, len(ts_list)):
            ego = by_ts[ts_list[j]]["ego_m"]
            if ego is not None:
                return ego - r["ego_m"]
        return None

    names = ("full", "nofar", "nocar", "nocoin", "noreward", "center", "sel")
    fut = {k: [0, 0] for k in names}
    lx_s = {k: [0, 0] for k in names}

    def target_of(name, r):
        if name == "center":
            return 0.0
        if name == "sel":
            return None if r["sel"] is None else r["sel"][2]
        return r["pot"].get(name)

    def agree(stats, name, target, ego, ref):
        if target is None:
            return
        s = 1 if target - ego > 0.05 else -1 if target - ego < -0.05 else 0
        if s:
            stats[name][0] += (s == ref)
            stats[name][1] += 1

    moves = 0
    lx_n = 0
    for r in valid:
        dh = future_move(r)
        if dh is not None and abs(dh) >= 0.34:
            moves += 1
            ref = 1 if dh > 0 else -1
            for name in names:
                agree(fut, name, target_of(name, r), r["ego_m"], ref)
        lx = r.get("lx") or 0
        if abs(lx) >= 3000:
            lx_n += 1
            ref = 1 if lx > 0 else -1
            for name in names:
                agree(lx_s, name, target_of(name, r), r["ego_m"], ref)
    print(f"对照 A：人工未来 {HUMAN_HORIZON_S}s 横移 >=0.1 道，{moves} 拍")
    print(f"对照 B：手柄 |lx|>=3000，{lx_n} 拍")
    for name in names:
        a = fut[name]
        b = lx_s[name]
        print(f"  {name:>8}: 未来位移 {a[0]}/{a[1]}"
              + (f"={a[0]/a[1]:.0%}" if a[1] else "")
              + f"   手柄方向 {b[0]}/{b[1]}"
              + (f"={b[0]/b[1]:.0%}" if b[1] else ""))
    # 势场目标抖动（逐拍 |Δx*|）
    for name in ("full", "nofar"):
        vals = [(r["ts"], r["pot"][name]) for r in valid if r["pot"].get(name) is not None]
        d = [abs(b[1] - a[1]) for a, b in zip(vals, vals[1:])]
        if d:
            print(f"  {name:>8}: 目标逐拍变化中位 {np.median(d):.2f}m，"
                  f">0.3m 占比 {np.mean(np.array(d) > 0.3):.0%}")


def main() -> None:
    ap = argparse.ArgumentParser(description="人工局影子势场回放")
    ap.add_argument("--sess", default="manual_20261019_173132_p2")
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--depth-every", type=int, default=2)
    run(ap.parse_args())


if __name__ == "__main__":
    main()
