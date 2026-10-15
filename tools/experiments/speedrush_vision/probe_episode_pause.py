# -*- coding: utf-8 -*-
"""改道集生命周期分析（承诺制 Track 1 验收仪，2026-10-15 设计稿 v2 §3.5/§5）。

一集 = select:* → 终态（done:*/cancel:*/switch:*）；trace 结尾未闭合的尾集
计入总数、结局记 open。与 P0 诊断（106 集 = 44 完成 + 61 打断 + 1 尾集，
中位 0.34s）同口径，新增承诺制观测面：

每集拆两笔时间 + 一笔位移：
  active_s   非暂停拍时长（改道实际推进时间）
  paused_s   reason=pause:blocked 拍时长（承诺制冻结横位等恢复）
  disp_lane  集内 executed_lane 净位移（有符号，道）——active 后的实际位移

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
    """按 select→终态切集；返回每集的时长/暂停/位移/结局。"""
    out: list[dict] = []
    cur: list[dict] | None = None
    for r in rows:
        reason = r.get("reason") or ""
        if cur is None:
            if _is_select(reason):
                cur = [r]
            continue
        cur.append(r)
        if _is_terminal(reason):
            first, last = cur[0], cur[-1]
            dur = sum((q.get("dt") or 0.0) for q in cur)
            paused = sum((q.get("dt") or 0.0) for q in cur
                         if q.get("reason") == "pause:blocked")
            e0 = first.get("executed_lane")
            e1 = last.get("executed_lane")
            out.append({
                "dur_s": dur, "active_s": dur - paused, "paused_s": paused,
                "disp": (e1 - e0) if (e0 is not None and e1 is not None) else None,
                "outcome": reason, "select": cur[0].get("reason")})
            cur = None
    if cur is not None:
        first, last = cur[0], cur[-1]
        dur = sum((q.get("dt") or 0.0) for q in cur)
        paused = sum((q.get("dt") or 0.0) for q in cur
                     if q.get("reason") == "pause:blocked")
        e0 = first.get("executed_lane")
        e1 = last.get("executed_lane")
        out.append({"dur_s": dur, "active_s": dur - paused, "paused_s": paused,
                    "disp": (e1 - e0) if (e0 is not None and e1 is not None)
                    else None, "outcome": "open", "select": cur[0].get("reason")})
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


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for path in sys.argv[1:]:
        report(Path(path).parent.name, path)


if __name__ == "__main__":
    main()
