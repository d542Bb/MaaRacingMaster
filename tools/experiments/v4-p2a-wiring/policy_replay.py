# -*- coding: utf-8 -*-
"""P2a-Q3a：PolicyPlan.decide 纯函数重放对拍。

用 trace 会话 decision 事件行自带的 facts_projection（json-safe 全量投影）
重放 decide，逐帧对拍当时的 decision 输出——验证：
1. policy 表迁移无损（v3 编译出的 plan 与 trace 产生时的行为一致）；
2. decide 是纯函数（同 facts 同输出），可被 v4 闭环节点安全复用。

对拍项：key / source / payload / side_effects / fatal（hint 未入 trace 契约不比）。
payload 中 tuple 在 JSON 往返后变 list，对比前递归归一。

用法：.venv\\Scripts\\python.exe tools\\experiments\\v4-p2a-wiring\\policy_replay.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_assistant.core.navkit.assets import Assets  # noqa: E402
from maaracing_assistant.core.navkit.policy import DecisionFacts, compile_plan  # noqa: E402
from maaracing_assistant.core.paths import debug_dir  # noqa: E402

V3_ASSETS = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "config" / "treasure_assets.json"


def norm(v: Any) -> Any:
    if isinstance(v, tuple):
        return [norm(x) for x in v]
    if isinstance(v, list):
        return [norm(x) for x in v]
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    return v


def main() -> int:
    assets = Assets.load(V3_ASSETS)
    plan = compile_plan(assets.policies, assets.anchors)
    print(f"[编译] rules={len(plan.rules)} stage_map={len(plan.stage_map)}")

    root = debug_dir() / "treasure"
    sessions = sorted([p for p in root.iterdir() if (p / "trace.jsonl").is_file()],
                      reverse=True) if root.is_dir() else []
    total = same = 0
    diffs: list[str] = []
    for s in sessions:
        n_session = 0
        for line in (s / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            if '"decision"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "decision":
                continue
            snap = row.get("decision_snapshot") or {}
            proj = snap.get("facts_projection")
            recorded = snap.get("decision")
            if not proj or not recorded:
                continue
            total += 1
            n_session += 1
            replayed = plan.decide(DecisionFacts(values=proj))
            got = {
                "key": replayed.key,
                "source": replayed.source,
                "payload": norm(dict(replayed.payload)),
                "side_effects": norm(list(replayed.side_effects)),
            }
            want = {
                "key": recorded.get("key"),
                "source": recorded.get("source"),
                "payload": norm(recorded.get("payload") or {}),
                "side_effects": norm(recorded.get("side_effects") or []),
            }
            if got != want or (snap.get("fatal") or None) != (replayed.fatal or None):
                diffs.append(f"{s.name}@frame={row.get('frame')}: want={want} got={got} "
                             f"fatal want={snap.get('fatal')!r} got={replayed.fatal!r}")
        if n_session:
            print(f"[会话] {s.name} decision 帧 {n_session}")
    print(f"[重放] decision 帧 {total}：一致 {total - len(diffs)}，分歧 {len(diffs)}")
    for d in diffs[:10]:
        print(f"    分歧: {d}")
    return 0 if total > 0 and not diffs else 1


if __name__ == "__main__":
    sys.exit(main())
