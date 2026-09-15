"""只读：解析 trace.jsonl 的 decision_snapshot，给出出价相位分布与「锁死」检查。

用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_trace.py
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

TRACE = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/treasure/20260915_211008/trace.jsonl"
OUT = Path(__file__).resolve().parent / "report_trace.md"


def flatten(prefix: str, obj, acc: Counter, samples: dict) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(f"{prefix}.{k}" if prefix else k, v, acc, samples)
    else:
        acc[prefix] += 1
        if prefix not in samples:
            samples[prefix] = obj


def main() -> None:
    out = [f"# trace 复验：{TRACE.name}\n"]
    if not TRACE.exists():
        OUT.write_text("trace 不存在", encoding="utf-8")
        return

    fields: Counter[str] = Counter()
    samples: dict[str, object] = {}
    phase_by_round: dict[tuple, Counter] = defaultdict(Counter)
    state_counts: Counter[str] = Counter()
    stuck_runs: list[str] = []
    hints: Counter[str] = Counter()
    cur_state = None
    cur_len = 0
    cur_round = None
    total = 0
    snap_examples: list[str] = []

    with TRACE.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            total += 1
            try:
                rec = json.loads(line)
            except Exception:
                continue
            snap = rec.get("decision_snapshot")
            if isinstance(snap, dict):
                if len(snap_examples) < 2:
                    snap_examples.append(json.dumps(snap, ensure_ascii=False)[:600])
                flatten("", snap, fields, samples)
                # 找相位字段：facts_projection.bidding_decision.state
                bd = (snap.get("facts_projection") or {}).get("bidding_decision")
                st = None
                if isinstance(bd, dict):
                    st = str(bd.get("state"))
                    hints[str(bd.get("hint"))[:60]] += 1
                if st is not None and st != "None":
                    state_counts[st] += 1
                    if isinstance(rec.get("stage"), str) and rec["stage"].startswith("第"):
                        phase_by_round[(rec["stage"], rec.get("round_no"))][st] += 1
                    if st == cur_state:
                        cur_len += 1
                    else:
                        if cur_len >= 150:
                            stuck_runs.append(f"{cur_state} × {cur_len} 帧（回合 {cur_round}）")
                        cur_state, cur_len, cur_round = st, 1, rec.get("round_no")
    if cur_len >= 150:
        stuck_runs.append(f"{cur_state} × {cur_len} 帧（回合 {cur_round}）")

    out.append(f"总记录 {total} 行\n")
    out.append("## decision_snapshot 样本\n```")
    out += snap_examples
    out.append("```\n")
    out.append("## 快照内字段（出现次数，前 40）\n")
    for k, v in fields.most_common(40):
        out.append(f"- `{k}`：{v}   例值={samples.get(k)!r}"[:160])

    out.append(f"\n## 出价相位分布（共 {sum(state_counts.values())} 帧）\n")
    for k, v in state_counts.most_common(20):
        out.append(f"- `{k}`：{v}")

    out.append("\n## 出价决策 hint 分布（前 15）\n")
    for k, v in hints.most_common(15):
        out.append(f"- `{k}`：{v}")

    out.append("\n## 按「阶段 × 回合」的相位分布\n")
    for (stage, rnd), c in sorted(phase_by_round.items()):
        out.append(f"- {stage} R{rnd}：{dict(c)}")

    out.append(f"\n## 同一相位连续 ≥150 帧的段落（旧的「静默锁死」形态，期望大幅减少）\n")
    out += [f"- {x}" for x in stuck_runs[:20]] or ["- 无"]

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
