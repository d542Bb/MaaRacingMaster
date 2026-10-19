# -*- coding: utf-8 -*-
"""远场意图慢层的离线影子（不接生产链路）。

目的：验证“提前 1~2 秒锁定一个连续横向目标”能否消除
“远的消极、近的应激”。慢层只吃已录制的 scene 快照，输出一个
连续横位（米，走廊中心=0），不引入车道号/lane_w 量化/第二份路心。

评估面：
1. 2~4.5s 有机会时，慢层是否在目标进 2s 视界前锁定并保持；
2. 锁定后的翻面率（次/秒）；
3. 贴脸段（如 184758_p2 fid 3139）是否本来可以更早、更缓地并线。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_plan/intent_shadow.py \
        <trace.jsonl> [--fid-from 3090] [--fid-to 3160]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_plan import load_rows, scene_of  # noqa: E402

from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.motion_plan import shift_scene  # noqa: E402

# ---- 慢层参数（影子期常量；定档后如需上线再进 decision.json）----
NEAR_S = 1.8           # 视界下沿：再近归快层，慢层不抢
FAR_S = 3.5            # 视界上沿：>3.5s 的横位误差大到不值得锁（宁可等下一次确认）
GRID_M = 0.25          # 连续横位采样步长（米）
REACTION_S = 0.2       # 横向起步响应
CONFIRM_TICKS = 3      # 目标稳定多少拍后才锁定
SWITCH_HOLD_S = 0.5    # 锁定后最短保持时间（换目标仍需 3 拍确认 + 30 分差）
KEEP_M = 0.7           # 与已锁目标的横向差小于此值视为同一意图
CONFIRM_M = 1.5        # 确认窗内目标允许的漂移（远场误差量级；锁“区域”不锁点）
SWITCH_MARGIN = 30.0   # 换目标所需的期望收益差
FAR_GAIN = 1.5         # 时间折扣尺度（秒）
SIGMA_BASE_M = 0.45    # 位置不确定度下限
SIGMA_PER_S = 0.18     # 越远越不确定：每秒到站时间追加的 σ（米）
GRAZE_WIDTH_M = 0.55   # 擦边软带宽度（约半车道抹平读数抖动）
EFFORT_PER_M = 2.0     # 横移代价（分/米）：同等收益优先近侧，抑制左右对称翻面
MIN_SCORE = 5.0        # 低于此视为“没有远场机会”，不锁意图
RELEASE_S = 1.5        # 远场对象消失多久后释放意图：留足执行时间（不是一丢就回中）


@dataclass
class ObjVal:
    kind: str
    x: float          # 目标通过线（车的擦边线 / 币或奖励车的中心）
    t: float
    value: float
    desc: str
    sigma: float = 0.0
    car_x: float | None = None   # 车心（碰撞判据用；币/奖励车=None）


def _gauss(dx: float, width: float) -> float:
    return math.exp(-0.5 * (dx / max(width, 1e-6)) ** 2)


def _reachable(ego_x: float, x: float, t: float, lane_w: float,
               v_lat_lane: float) -> bool:
    """到站时刻前能否横移到 x（粗运动学上界，含响应延迟）。"""
    budget = v_lat_lane * lane_w * max(t - REACTION_S, 0.0)
    return abs(x - ego_x) <= budget + 0.05


def _objects(scene, graze_m: float) -> list[ObjVal]:
    out: list[ObjVal] = []
    for o in scene.objs:
        if o.t_arrive_s is None or not (NEAR_S < o.t_arrive_s <= FAR_S):
            continue
        t = o.t_arrive_s
        if o.kind == "reward":
            out.append(ObjVal("reward", o.x_m, t, 240.0, "奖励车", o.sigma_m))
        elif o.kind == "coin":
            v = 120.0 if o.count >= 4 else 90.0
            out.append(ObjVal("coin", o.x_m, t, v,
                              f"金币{int(o.count)}枚", o.sigma_m))
        else:
            # 车：两条擦边线（左/右）都可能，按候选就近取值；碰撞在打分时扣
            for side in (-1.0, 1.0):
                out.append(ObjVal("car", o.x_m + side * graze_m, t,
                                  120.0, f"car{'L' if side < 0 else 'R'}",
                                  o.sigma_m, car_x=o.x_m))
    return out


def _score_x(x: float, objs: list[ObjVal], ego_x: float, lane_w: float,
             cfg) -> tuple[float, list[str]]:
    """候选横位 x 的远场期望收益（软命中 + 不确定度 + 可达门）。"""
    m = cfg.motion
    sel = cfg.selection
    eh = sel.ego_half_lane * lane_w
    ch = sel.car_half_lane * lane_w
    v_lat = cfg.planner.v_lat_max
    score = 0.0
    notes: list[str] = []
    for ov in objs:
        if not _reachable(ego_x, x, ov.t, lane_w, v_lat):
            continue
        sigma = max(SIGMA_BASE_M, ov.sigma) \
            + SIGMA_PER_S * max(ov.t - NEAR_S, 0.0)
        w = math.exp(-max(ov.t - NEAR_S - 0.5, 0.0) / FAR_GAIN)
        dx = abs(x - ov.x)
        if ov.kind == "car":
            # 碰撞按车心判，擦边按擦边线判——两者不是同一个横位。
            clr = abs(x - (ov.car_x if ov.car_x is not None else ov.x)) \
                - (eh + ch)
            if clr < 0.0:                       # 软碰撞惩罚（远场也怕）
                score -= w * (m.cost_collision
                              + (-clr) * m.cost_collision_depth_m)
            score += w * ov.value * _gauss(dx, GRAZE_WIDTH_M + sigma)
        else:
            score += w * ov.value * _gauss(dx, sigma + 0.35 * lane_w)
        if w * ov.value * _gauss(dx, GRAZE_WIDTH_M + sigma) > 20.0:
            notes.append(f"{ov.desc}@{ov.t:.1f}s")
    score -= EFFORT_PER_M * abs(x - ego_x)
    return score, notes


def run(path: Path, fid_from: int | None, fid_to: int | None) -> dict:
    cfg = load_decision()
    rows = load_rows(path)
    gate_cal = cfg.motion
    committed: float | None = None
    committed_ts: float = 0.0
    pending_x: float | None = None
    pending_n = 0
    pending_sum = 0.0
    empty_t = 0.0
    flips = 0

    def feed(d: float) -> None:
        """确认窗喂样本：只要求目标在 CONFIRM_M 内漂移，锁定时取样本均值。"""
        nonlocal pending_x, pending_n, pending_sum
        if pending_n == 0:
            pending_x, pending_n, pending_sum = d, 1, d
        elif abs(d - pending_x) <= CONFIRM_M:
            pending_sum += d
            pending_n += 1
            pending_x = pending_sum / pending_n
        else:
            pending_x, pending_n, pending_sum = d, 1, d
    commits = 0
    left_commits = 0
    timeline = []
    prev_ts = None
    prev_fid = None
    lane_w = 3.38
    for r in rows:
        if r.get("scene") is None:
            continue
        sc = scene_of(r["scene"])
        if sc.schema < 2 and r.get("road_offset") is not None:
            sc = shift_scene(sc, float(r["road_offset"]) * sc.lane_w_m
                             - sc.ego_x_m)
        lane_w = sc.lane_w_m
        ts = r["ts_ns"] / 1e9
        dt = (ts - prev_ts) if prev_ts is not None else 0.05
        prev_ts = ts
        if r.get("state") != "CRUISE":
            committed = pending_x = None
            pending_n = 0
            pending_sum = 0.0
            empty_t = 0.0
            prev_fid = r["fid"]
            continue
        ego_x = sc.ego_x_m
        objs = _objects(sc, cfg.motion.graze_gap_lane * lane_w)
        left = sc.road.left_m if sc.road.left_m is not None else -6.5
        right = sc.road.right_m if sc.road.right_m is not None else 6.5
        lo, hi = left + 0.4, right - 0.4
        # 目标候选：走廊内连续网格；可达门在打分内部
        n = max(1, int((hi - lo) / GRID_M))
        cand = [lo + i * GRID_M for i in range(n + 1)]
        scored = [(x, _score_x(x, objs, ego_x, lane_w, cfg))
                  for x in cand]
        scored.sort(key=lambda z: -z[1][0])
        best_x, (best_v, best_notes) = scored[0]
        # 第二优：与最优横向差 > KEEP_M 的候选
        second = next((v for x, (v, _) in scored
                       if abs(x - best_x) > KEEP_M), 0.0)
        # 没有远场对象（或收益可忽略）→ 没有意图，不要拿空场景的左缘当目标
        desired = best_x if (objs and best_v >= MIN_SCORE) else None
        now = ts
        changed = False
        if desired is None:
            empty_t += dt
            pending_x, pending_n, pending_sum = None, 0, 0.0
            if committed is not None and empty_t >= RELEASE_S:
                committed = None
                changed = True
        else:
            empty_t = 0.0
            if committed is None:
                feed(desired)
                if pending_n >= CONFIRM_TICKS:
                    committed, committed_ts = pending_x, now
                    commits += 1
                    if committed < ego_x - 0.5:
                        left_commits += 1
                    pending_x, pending_n, pending_sum = None, 0, 0.0
                    changed = True
            else:
                hold = now - committed_ts
                if abs(desired - committed) <= KEEP_M:
                    pending_x, pending_n, pending_sum = None, 0, 0.0
                else:
                    feed(desired)
                    cur_v = _score_x(committed, objs, ego_x, lane_w, cfg)[0]
                    if hold >= SWITCH_HOLD_S and pending_n >= CONFIRM_TICKS \
                            and best_v > cur_v + SWITCH_MARGIN:
                        committed, committed_ts = pending_x, now
                        pending_x, pending_n, pending_sum = None, 0, 0.0
                        flips += 1
                        changed = True
        if fid_from is not None and (r["fid"] < fid_from
                                     or (fid_to is not None
                                         and r["fid"] > fid_to)):
            prev_fid = r["fid"]
            continue
        timeline.append({
            "fid": r["fid"], "ego": round(ego_x, 2),
            "desired": None if desired is None else round(desired, 2),
            "committed": None if committed is None else round(committed, 2),
            "best": round(best_v, 1), "second": round(second, 1),
            "n_far": len(objs), "changed": changed,
            "notes": best_notes[:3],
        })
        prev_fid = r["fid"]
    dur = (rows[-1]["ts_ns"] - rows[0]["ts_ns"]) / 1e9 if rows else 0.0
    return {"timeline": timeline, "commits": commits,
            "left_commits": left_commits, "flips": flips,
            "flip_per_s": flips / dur if dur else 0.0,
            "duration_s": round(dur, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=Path)
    ap.add_argument("--fid-from", type=int, default=None)
    ap.add_argument("--fid-to", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    res = run(args.trace, args.fid_from, args.fid_to)
    for row in res["timeline"]:
        print(json.dumps(row, ensure_ascii=False))
    print(json.dumps({k: v for k, v in res.items() if k != "timeline"},
                     ensure_ascii=False, indent=2))
    if args.out is not None:
        args.out.write_text(json.dumps(res, ensure_ascii=False, indent=1),
                            encoding="utf-8")


if __name__ == "__main__":
    main()
