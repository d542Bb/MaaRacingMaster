# -*- coding: utf-8 -*-
"""双决策器离线回放（阶段二 §三.1）：manual 语料同观测流喂现役栈与轨迹模式。

**口径（预期管理，验收前先读）**：语料是人开的车，回放是**开环**的——
自车执行位取 road_offset 观测（真人实际横向位置），两个决策器只比**决策**：
选中目标一致性、状态/原因分布、轨迹模式破判率、输出点 vs 目标位；**比不了
结局分数率**（车的实际运动由人的方向盘决定），闭环结局对比只能实机 V2。
深度离线逐帧同步供数（产线是异步带龄）——决策看到的是深度上界质量。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/replay_dual_decision.py \
        --session <录制会话目录> [--render-every 30] [--out ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from maaracing_master.plugins.speedrush.coin_group import CoinGroupAggregator  # noqa: E402
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.decision import DecisionEngine  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DepthRoadObserver, infer_points, load_session, reading_from_points)
from maaracing_master.plugins.speedrush.module import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE, _EgoRoadObserver,
    _yolo_object_mask)
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.traffic import TrafficObserver  # noqa: E402
from maaracing_master.plugins.speedrush.tracking import Tracker  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from dataclasses import replace  # noqa: E402


def _x_to_px(x_lane: float, cy: int, cal) -> int | None:
    """A1 尺子逆变换（world_model.x_lane_of 的精确反演）：决策输出画回画面。
    非有限输入（存续组 nan）→ None。"""
    if x_lane is None or x_lane != x_lane or cy != cy:
        return None
    e = (cal.ego_cx - cal.vpx) / (cal.v_ego - cal.y_h)
    return int(round(cal.vpx + (x_lane / cal.a_x + e) * (cy - cal.y_h)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--render-every", type=int, default=30)
    ap.add_argument("--out", default="../../.workbuddy-ai/replay_dual")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    cfg = load_decision()
    cfg_traj = replace(cfg, mode=replace(cfg.mode, trajectory_sampling=True))
    eng_leg = DecisionEngine(cfg)
    eng_traj = DecisionEngine(cfg_traj)
    tracker = Tracker()
    agg = CoinGroupAggregator()
    traffic_obs = TrafficObserver(cfg.traffic)
    ego_road = _EgoRoadObserver()
    cal = load_calib()

    sess = load_session(DEPTH_MODEL_FILE)
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    ego = DepthRoadObserver._load_ego_mask()

    sessdir = Path(a.session)
    meta = json.loads((sessdir / "meta.json").read_text(encoding="utf-8"))
    phase = int(meta.get("phase", 1))
    rows_meta = [json.loads(l) for l in
                 (sessdir / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
    tag = sessdir.name
    print(f"{tag}: {len(rows_meta)} 帧（阶段 {phase}），离线全量供数")

    rows: list[dict] = []
    last_ro = None
    prev_ts = None
    t_acc = 0.0
    for i, m in enumerate(rows_meta):
        jpg = sessdir / "frames" / f"{m['seq']:06d}.jpg"
        rgb = cv2.cvtColor(cv2.imread(str(jpg)), cv2.COLOR_BGR2RGB)
        result = perc.detect(rgb, frame_id=m["frame_id"], ts_ns=m["ts_ns"])
        obs = tracker.update(result, frame_age_ms=30.0, stage=phase)
        obs = agg.update(obs)
        # 深度逐帧同步（产线为异步带龄，此处上界质量——口径见 docstring）
        ro = None
        try:
            obj = _yolo_object_mask(result)
            pts, fx, fy = infer_points(sess, rgb, with_evidence=False)
            rd = reading_from_points(pts, fx, cal, ego_mask=ego,
                                     object_mask=obj, fy=fy)
            ro = ego_road.update(rd, now=t_acc)
        except Exception:  # noqa: BLE001 —— 单帧深度失败不挡回放
            ro = ego_road.update(None, now=t_acc)
        width = ego_road.width
        tviews, tevents = traffic_obs.update(obs)

        ts = m["ts_ns"]
        dt = 0.05 if prev_ts is None or ts <= prev_ts else (ts - prev_ts) / 1e9
        prev_ts = ts
        t_acc += dt
        # 开环执行位=真人实际横向位置（ro 观测；缺席拍保持上次）
        if ro is not None:
            last_ro = ro
        executed = last_ro

        o_leg = eng_leg.update(obs, dt, executed_lane=executed,
                               traffic=(tviews, tevents),
                               road_offset=ro, road_width=width)
        o_traj = eng_traj.update(obs, dt, executed_lane=executed,
                                 traffic=(tviews, tevents),
                                 road_offset=ro, road_width=width)
        plan = getattr(eng_traj, "_traj_prev", None)
        rows.append({
            "seq": m["seq"], "fid": m["frame_id"], "dt": round(dt, 4),
            "ro": None if ro is None else round(ro, 3),
            "width": None if width is None else round(width, 3),
            "n_groups": len(obs.coin_groups), "n_cars": len(tviews),
            "leg_state": o_leg.state.name, "leg_reason": o_leg.reason,
            "leg_target": o_leg.target_id, "leg_xt": o_leg.x_target,
            "traj_state": o_traj.state.name, "traj_reason": o_traj.reason,
            "traj_target": o_traj.target_id, "traj_xt": o_traj.x_target,
            "traj_d1": None if plan is None else round(plan.d1, 3),
            "traj_T": None if plan is None else plan.T,
            "traj_cost": None if plan is None else round(plan.cost, 3),
        })

        if i % a.render_every == 0:
            img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            cy_mark = 600
            for g in obs.coin_groups:
                cx = _x_to_px(g.x_center, g.cy_max, cal)
                if cx is not None:
                    cv2.circle(img, (cx, g.cy_max), 8, (0, 255, 0), 2)
            for v in tviews:
                cx = _x_to_px(v.x_lane, v.cy, cal)
                if cx is None:
                    continue
                cv2.rectangle(img, (cx - 25, v.cy - 18), (cx + 25, v.cy + 18),
                              (0, 0, 255), 2)
            for name, o in (("LEG", o_leg), ("TRAJ", o_traj)):
                if o.x_target is None or last_ro is None:
                    continue
                # 输出点路心系 → A1 画位：x_A1 = x_target − ro
                cx = _x_to_px(o.x_target - last_ro, cy_mark, cal)
                color = (255, 255, 0) if name == "LEG" else (255, 0, 255)
                cv2.line(img, (cx, cy_mark - 40), (cx, cy_mark + 40), color, 3)
                cv2.putText(img, f"{name} {o.state.name[:4]} xt={o.x_target:+.2f}",
                            (cx - 60, cy_mark - 48),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
            cv2.putText(img, f"ro={last_ro} W={width} cars={len(tviews)}",
                        (8, 620), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (0, 255, 255), 1)
            cv2.imwrite(str(out / f"{tag}_{m['seq']:06d}_dual.jpg"), img,
                        [cv2.IMWRITE_JPEG_QUALITY, 88])

    (out / f"{tag}_dual.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8")

    # ---- 聚合 ----
    def hist(key):
        return dict(Counter(r[key] for r in rows))

    both_change = [r for r in rows
                   if r["leg_state"] == "CHANGE" and r["traj_state"] == "CHANGE"]
    agree = [r for r in both_change
             if r["leg_target"] == r["traj_target"]]
    traj_block = sum(1 for r in rows if "traj_blocked" in (r["traj_reason"] or ""))
    summary = {
        "ticks": len(rows),
        "ro_present": sum(1 for r in rows if r["ro"] is not None) / len(rows),
        "W_final": rows[-1]["width"],
        "leg_reasons": hist("leg_reason"), "traj_reasons": hist("traj_reason"),
        "leg_states": hist("leg_state"), "traj_states": hist("traj_state"),
        "both_change_ticks": len(both_change),
        "target_agreement": (len(agree) / len(both_change)) if both_change else None,
        "traj_blocked_ticks": traj_block,
        "traj_abort_ticks": sum(1 for r in rows if r["traj_state"] == "ABORT_CHANGE"),
    }
    (out / f"{tag}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"输出 → {out.resolve()}")


if __name__ == "__main__":
    main()
