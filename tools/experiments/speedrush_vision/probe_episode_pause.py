# -*- coding: utf-8 -*-
"""改道集生命周期分析（承诺制验收仪：Track 1 v2 §3.5/§5 + 需求面 §8.6 L1）。

一集 = select:* → 终态（done:*/cancel:*/switch:*）；trace 结尾未闭合的尾集
计入总数、结局记 open。与 P0 诊断（106 集 = 44 完成 + 61 打断 + 1 尾集，
中位 0.34s）同口径，观测面：

每集拆两笔时间 + 一笔位移：
  active_s   非暂停拍时长（改道实际推进时间）
  paused_s   reason=pause:blocked 拍时长（承诺制冻结横位等恢复）
  disp_lane  集内 executed_lane 净位移（有符号，道）——active 后的实际位移

需求面（trace 带 intent 列才有值，§8）：
  want       |d0 − 锁定拍 executed|（意图需求量；对照旧 select-want 口径）
  arrive_err done 集 |末位 executed − 末拍 d0|——≤dead_zone 即真到达；
             坍缩假完成在此现形（L1 验收①：坍缩桶清零）
  absent_s / gap_s   hold:reach_absent / hold:reach_gap 持位时长（缺席记忆
             与空窗等下一组的生命周期观察；cancel:reach_lost 的缺席拍
             absence ≈ 预算=预算耗尽形态，L1 验收②）
  n_up       switch:reach_upgrade 次数（升级转移活跃度）

主判据（§5）：active 后位移↑；总集时长可被暂停刷过，只算生命周期观察。
安全面：lat_veto 频次不得上升。最终收益（collect_coin/P2）走 log ground
truth 另步对账，不在本探针。

用法：.venv python probe_episode_pause.py <trace.jsonl> [...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _is_select(r: str) -> bool:
    return r.startswith("select:")


def _is_terminal(r: str) -> bool:
    return (r.startswith("done:") or r.startswith("cancel:")
            or r.startswith("switch:"))


def episodes(rows: list[dict]) -> list[dict]:
    """按 select→终态切集；返回每集的时长/暂停/位移/结局/意图面。"""
    out: list[dict] = []

    def close(cur: list[dict], outcome: str) -> dict:
        first, last = cur[0], cur[-1]
        dur = sum((q.get("dt") or 0.0) for q in cur)
        paused = sum((q.get("dt") or 0.0) for q in cur
                     if q.get("reason") == "pause:blocked")
        e0 = first.get("executed_lane")
        e1 = last.get("executed_lane")
        its = [(q, q["intent"]) for q in cur if q.get("intent")]
        want = d0_last = arr_err = None
        if its:
            q0, it0 = its[0]
            want = abs(it0["d0"] - q0.get("executed_lane")) \
                if q0.get("executed_lane") is not None else None
            d0_last = its[-1][1]["d0"]
            if outcome.startswith("done:") and e1 is not None:
                arr_err = abs(e1 - d0_last)
        return {
            "dur_s": dur, "active_s": dur - paused, "paused_s": paused,
            "disp": (e1 - e0) if (e0 is not None and e1 is not None) else None,
            "outcome": outcome, "select": first.get("reason"),
            "want": want, "arrive_err": arr_err,
            "absent_s": sum((q.get("dt") or 0.0) for q in cur
                            if q.get("reason") == "hold:reach_absent"),
            "gap_s": sum((q.get("dt") or 0.0) for q in cur
                         if q.get("reason") == "hold:reach_gap"),
            "n_up": sum(1 for q in cur
                        if q.get("reason") == "switch:reach_upgrade"),
            "has_intent": bool(its)}

    cur: list[dict] | None = None
    for r in rows:
        reason = r.get("reason") or ""
        if cur is None:
            if _is_select(reason):
                cur = [r]
            continue
        cur.append(r)
        if _is_terminal(reason):
            out.append(close(cur, reason))
            cur = None
    if cur is not None:
        out.append(close(cur, "open"))
    return out


def _med(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def report(label: str, path: str) -> None:
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    rows.sort(key=lambda r: r["fid"])
    eps = episodes(rows)
    n = len(eps)
    done = [e for e in eps if e["outcome"].startswith("done:")]
    cancel = [e for e in eps if e["outcome"].startswith("cancel:")]
    opened = [e for e in eps if e["outcome"] in ("open",)
              or e["outcome"].startswith("switch:")]
    cancels: dict[str, int] = {}
    for e in cancel:
        cancels[e["outcome"]] = cancels.get(e["outcome"], 0) + 1
    paused_eps = [e for e in eps if e["paused_s"] > 0]
    lat_veto = cancels.get("cancel:lat_veto", 0)

    def disp_of(group) -> float:
        xs = [abs(e["disp"]) for e in group if e["disp"] is not None]
        return _med(xs) if xs else float("nan")

    print(f"\n== {label}（{Path(path).name}）")
    print(f"  集 {n} = done {len(done)} + cancel {len(cancel)} + "
          f"switch/open {len(opened)}；完成率 {len(done)*100/max(n,1):.0f}%")
    print(f"  时长中位 {_med([e['dur_s'] for e in eps]):.2f}s"
          f"（P0 基线 0.34s）；active 中位 "
          f"{_med([e['active_s'] for e in eps]):.2f}s；总暂停 "
          f"{sum(e['paused_s'] for e in eps):.1f}s")
    print(f"  打断分型 {cancels}；lat_veto {lat_veto} 次（P0 基线 6/回合，不得升）")
    if paused_eps:
        print(f"  暂停参与 {len(paused_eps)} 集：paused 中位 "
              f"{_med([e['paused_s'] for e in paused_eps]):.2f}s，"
              f"其中 done {sum(1 for e in paused_eps if e['outcome'].startswith('done:'))}"
              f"/{len(paused_eps)}")
    else:
        print("  暂停参与 0 集（闸关或无连续阻塞）")
    print(f"  active 后 |位移| 中位（道）：done "
          f"{disp_of(done):.2f}，cancel {disp_of(cancel):.2f}"
          f"（P0 基线：完成集 ~0.1 道碎步）")
    intent_eps = [e for e in eps if e["has_intent"]]
    if intent_eps:
        done_i = [e for e in intent_eps if e["outcome"].startswith("done:")]
        true_arr = [e for e in done_i
                    if e["arrive_err"] is not None and e["arrive_err"] <= 0.15]
        wants = [e["want"] for e in intent_eps if e["want"] is not None]
        n_up = sum(e["n_up"] for e in intent_eps)
        print(f"  意图面（§8.6 L1，{len(intent_eps)} 集带 intent）："
              f"锁需求 want 中位 {_med(wants):.2f} 道"
              f"（旧口径完成集 0.27-0.48 / 坍缩集 0.76-0.79 二分）")
        print(f"    真到达 {len(true_arr)}/{len(done_i)} done"
              f"（|末位−d0|≤0.15；坍缩桶=差值，L1 验收①清零目标）；"
              f"升级 {n_up} 次；缺席持位 "
              f"{sum(e['absent_s'] for e in intent_eps):.1f}s / gap 持位 "
              f"{sum(e['gap_s'] for e in intent_eps):.1f}s")
    else:
        print("  意图面：trace 无 intent 列（demand 闸前数据，跳过）")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for path in sys.argv[1:]:
        report(Path(path).parent.name, path)


if __name__ == "__main__":
    main()
