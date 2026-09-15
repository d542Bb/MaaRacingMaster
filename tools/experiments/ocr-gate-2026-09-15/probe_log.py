"""只读探针：解析 MaaRM 日志，回答「出价报价数字是被 OCR 读到了却被闸门丢掉，还是根本没进 OCR」。

只读，不写任何生产文件；报告写到本目录 report.md。
用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_log.py <日志glob或路径>
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

LOGS = Path.home() / "AppData/Roaming/MaaRacingMaster/logs"
OUT = Path(__file__).resolve().parent / "report.md"

TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2}[.,]\d{3})")
PAGE_RE = re.compile(r"本帧页=(\S+?)[\s)]")
DROP_RE = re.compile(r"丢弃=\[([^\]]*)\]")
SLOT_RE = re.compile(r"槽(\d).*?消费(\d+)/输出(\d+)/命中(\d+)")


def pick(pattern: str) -> Path:
    hits = sorted(LOGS.glob(pattern), key=lambda p: p.stat().st_size, reverse=True)
    if not hits:
        raise SystemExit(f"没找到日志 {pattern}")
    return hits[0]


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "MaaRM_20260915_2019*.log*"
    cand = Path(arg)
    path = cand if cand.exists() else pick(arg)

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    out.append(f"# 探针报告：{path.name}（{path.stat().st_size / 1024:.0f} KB，{len(lines)} 行）\n")

    out.append("## 原始行样本（前 12 行）\n```")
    out.extend(lines[:12])
    out.append("```\n")

    pages: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    window: list[str] = []
    applied: Counter[str] = Counter()
    locks: list[str] = []
    slot_snap: dict[str, str] = {}
    ts_first = ts_last = None

    for raw in lines:
        m = TS_RE.search(raw)
        ts = m.group(1) if m else ""
        if ts:
            ts_first = ts_first or ts
            ts_last = ts
        if "跨页丢弃" in raw:
            pm = PAGE_RE.search(raw)
            page = pm.group(1) if pm else "?"
            pages[page] += 1
            dm = DROP_RE.search(raw)
            keys = [k.strip().strip("'\"") for k in dm.group(1).split(",")] if dm else []
            for k in keys:
                dropped[k] += 1
            if "20:20:0" in ts or "20:20:1" in ts or "20:20:2" in ts:
                window.append(f"{ts} 页={page} 丢={keys}")
        if "已应用" in raw:
            applied[ts[:5]] += 1
        if "固化" in raw:
            locks.append(f"{ts} {raw.strip()[:220]}")
        sm = SLOT_RE.search(raw)
        if sm:
            slot_snap[f"槽{sm.group(1)}"] = f"{ts} 消费{sm.group(2)}/输出{sm.group(3)}/命中{sm.group(4)}"

    out.append(f"时间范围：{ts_first} → {ts_last}\n")
    out.append("## 本帧页取值分布（来自「跨页丢弃」日志）\n")
    for k, v in pages.most_common():
        out.append(f"- `{k}`：{v}")
    out.append("\n## 被丢弃的键分布\n")
    for k, v in dropped.most_common():
        out.append(f"- `{k}`：{v}")
    out.append("\n## 已应用帧时间线（分钟 → 帧数）\n")
    for k in sorted(applied):
        out.append(f"- {k}：{applied[k]}")
    out.append(f"\n## 槽固化事件：{len(locks)} 条\n")
    out.extend(f"- {x}" for x in locks[:40])
    out.append(f"\n## 槽状态快照（最后一次统计行）：{len(slot_snap)} 条\n")
    out.extend(f"- {k} {v}" for k, v in sorted(slot_snap.items()))
    out.append(f"\n## 20:20:0x~20:20:2x 逐帧丢弃明细（{len(window)} 条，前 80）\n")
    out.extend(f"- {x}" for x in window[:80])

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
