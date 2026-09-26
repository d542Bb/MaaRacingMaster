# -*- coding: utf-8 -*-
"""实机控制 trace → 生产 planner 离线回放 + 增益网格（13:02 局极限环标定）。

回答的事实：obs_alpha × obs_jump_max_lane × k_p × k_d（及单侧合成路观测是否
进闭环）能否把杆饱和率/符号翻转率压下去，代价（|executed| 散布）是什么。

纪律：
- **先复现后网格**——生产配置回放同一条流，**首个 CONSERVE 拍前**逐拍对上
  trace 才说明流重建正确（valid_until 按引擎公式 fid+max(2,round(0.1·hz))
  重构、dt/fid/state/x_target/road_offset/reanchor 全部取自 trace）；CONSERVE
  之后是有意的语义分叉（c1512bf），不入复现判据。复现不过，网格结论无效。
- 回放的是生产 `LateralPlanner` 本身（C3 纯函数纪律的存在意义），不另写一份。
- 变体改了 executed 轨迹后与 trace 分叉是**预期**（what-if 模拟），指标只看
  模拟流自身：饱和率（|杆|≥30000 拍占比）、翻转率（大杆段符号翻转次数/秒，
  dt 归一）、|executed| p90。

用法：
    python tools/experiments/speedrush_control/probe_planner_replay.py <trace.jsonl>
    python tools/experiments/speedrush_control/probe_planner_replay.py <trace.jsonl> --grid
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics as st
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from maaracing_master.plugins.speedrush import DECISION_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.config import Planner, _read_decision  # noqa: E402
from maaracing_master.plugins.speedrush.planner import LateralPlanner  # noqa: E402
from maaracing_master.plugins.speedrush.tracking import (  # noqa: E402
    DecisionOutput, DecisionState)

CFG = _read_decision(DECISION_FILE)
SAT_RAW = 30000          # 杆饱和判据：|raw| ≥ 此值（满幅 32767 的 91.5%）
BIG_STICK = 5000         # 翻转统计只数大杆段（死区边缘抖动不计）


def load_rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows.sort(key=lambda r: r["fid"])
    return rows


def replay(rows: list[dict], over: dict | None = None, pair_only: bool = False):
    base = {k: getattr(CFG.planner, k) for k in Planner.__dataclass_fields__}
    base.update(over or {})
    pl = LateralPlanner(Planner(**base))
    valid_ticks = max(2, int(round(0.1 * CFG.control.frame_rate_hz)))
    out = []
    for r in rows:
        d = DecisionOutput(
            schema_version=2, state=DecisionState(r["state"]), target_id=None,
            x_target=r["x_target"], move_allowed=True, reason=r.get("reason", ""),
            emitted_fid=r["fid"], valid_until_fid=r["fid"] + valid_ticks,
            reanchor_lane=r.get("reanchor_lane"))
        ro = r.get("road_offset")
        if pair_only and r.get("ro_source") != "pair":
            ro = None
        cmd = pl.update(d, r["dt"], r["fid"], road_offset=ro)
        out.append((cmd.steer_x, pl.state.executed_lane, pl._stale_t,
                    pl._road_anchor))
    return out


def metrics(out, rows) -> tuple[float, float, float, float]:
    """(饱和率, 翻转/s, |exec|p90, 最长饥饿 s)。饥饿=距上次被接受修正的秒数
    （planner._stale_t 直读）——13:02 网格只优化了前三项，看不见锁死，
    pair-only 定档被 13:29 局证伪正出此盲点。"""
    steer = [o[0] for o in out]
    ex = [abs(o[1]) for o in out]
    sat = sum(1 for s in steer if abs(s) >= SAT_RAW) / len(steer)
    t_tot = sum(r["dt"] for r in rows)
    big = [s for s in steer if abs(s) > BIG_STICK]
    flips = sum(1 for a, b in zip(big, big[1:]) if a * b < 0) / max(t_tot, 1e-9)
    p90 = sorted(ex)[int(0.9 * (len(ex) - 1))]
    starve = max((o[2] for o in out), default=0.0)
    return sat, flips, p90, starve


def fidelity(out, rows) -> tuple[float, float]:
    """复现误差只数**首个 CONSERVE 前**的段：trace 录于旧语义（归零直开），
    当前代码的 CONSERVE=保持车道是有意的分叉（c1512bf），分叉一经入态就
    污染其后所有拍——整段比误差没有信息量，前缀逐拍对齐才是流重建的检验。"""
    d = [abs(s - r["steer_x"]) for s, r in zip((o[0] for o in out), rows)]
    return (st.mean(d), max(d)) if d else (0.0, 0.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", type=Path)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--skip-fidelity", action="store_true",
                    help="录于旧语义代码的 trace 前缀必然分叉（如 12:39 局早于路锚卫生），"
                         "跳过复现门仅作探索性 what-if——其流被旧控制器行为污染，"
                         "绝对指标与排序都不作定档依据")
    args = ap.parse_args()
    rows = load_rows(args.trace)
    print(f"trace: {args.trace.name}  ticks={len(rows)}  "
          f"ro拍={sum(1 for r in rows if r.get('road_offset') is not None)}")

    base_out = replay(rows)
    n_pre = next((i for i, r in enumerate(rows) if r["state"] == "CONSERVE"),
                 len(rows))
    mean_d, max_d = fidelity(base_out[:n_pre], rows[:n_pre])
    sat, flips, p90, starve = metrics(base_out, rows)
    print(f"[复现] 首 CONSERVE 前 {n_pre} 拍逐拍对齐 mean|Δsteer|={mean_d:.1f} "
          f"max|Δ|={max_d}（其后分叉=CONSERVE 新语义，属预期）")
    print(f"[基线] 生产配置整段指标: 饱和率={sat:.1%} 翻转={flips:.2f}/s "
          f"|exec|p90={p90:.2f} 最长饥饿={starve:.2f}s")
    if mean_d > 500:
        print("!! 前缀复现不过（流重建有误），网格结论无效——先查 valid_until/dt/state。")
        return

    if not args.grid:
        return
    print(f"\n{'obs_alpha':>9} {'obs_jump':>8} {'k_p':>4} {'k_d':>4} {'pair':>5} "
          f"{'饱和率':>7} {'翻转/s':>7} {'|ex|p90':>8} {'饥饿s':>7}")
    grid = []
    for alpha, jump, kp, kd, pair in itertools.product(
            (0.6, 0.3, 0.15), (1.5, 0.8), (1.2, 0.7), (0.2, 0.4), (False, True)):
        over = {"obs_alpha": alpha, "obs_jump_max_lane": jump, "k_p": kp, "k_d": kd}
        out = replay(rows, over, pair_only=pair)
        grid.append(((alpha, jump, kp, kd, pair), metrics(out, rows)))
    # 排序含饥饿惩罚：锁死（stale 顶到 anchor_stale_s）即判据失效信号
    for key, (sat, flips, p90, starve) in sorted(
            grid, key=lambda g: g[1][0] + 0.1 * g[1][1] + 0.05 * g[1][3]):
        print(f"{key[0]:>9} {key[1]:>8} {key[2]:>4} {key[3]:>4} {str(key[4]):>5} "
              f"{sat:>6.1%} {flips:>7.2f} {p90:>8.2f} {starve:>7.2f}")


if __name__ == "__main__":
    main()
