"""只读复验（第三段）：业务结果实据 + trace 结构。

只读。用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_biz.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

LOGS = Path.home() / "AppData/Roaming/MaaRacingMaster/logs"
TRACE = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/treasure/20260915_211008/trace.jsonl"
OUT = Path(__file__).resolve().parent / "report_biz.md"

PATTERNS = ["快照已构建", "已记录第", "出价 P1=", "系统报价", "决策", "我方出价", "智能出价"]


def main() -> None:
    path = sorted(LOGS.glob("MaaRM_20260915_2110*"),
                  key=lambda p: p.stat().st_size, reverse=True)[0]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out = [f"# 业务结果实据：{path.name}\n"]

    for pat in PATTERNS:
        hits = [ln.strip() for ln in lines if pat in ln]
        out.append(f"\n## `{pat}`（{len(hits)} 条，前 22）\n```")
        out += hits[:22]
        out.append("```")

    out.append(f"\n\n# trace 结构：{TRACE.name}\n")
    if not TRACE.exists():
        out.append("（trace 不存在）")
    else:
        keys: Counter[str] = Counter()
        decisions: Counter[str] = Counter()
        total = 0
        with TRACE.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            out.append("首行键：\n```\n" + first[:900] + "\n```\n")
            fh.seek(0)
            for line in fh:
                total += 1
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                for k in rec:
                    keys[k] += 1
                dec = rec.get("bidding_decision") or rec.get("decision") or {}
                if isinstance(dec, dict):
                    st = dec.get("state") or dec.get("action") or "-"
                    decisions[str(st)] += 1
                elif isinstance(dec, str):
                    decisions[dec] += 1
        out.append(f"总记录 {total} 行；字段出现次数：\n")
        for k, v in keys.most_common(40):
            out.append(f"- `{k}`：{v}")
        out.append("\n决策状态分布（bidding_decision.state / decision）：\n")
        for k, v in decisions.most_common(25):
            out.append(f"- `{k}`：{v}")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
