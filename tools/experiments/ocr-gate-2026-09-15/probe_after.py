"""只读复验：2026-09-15 21:10 会话「第二类同帧证据」上线后的效果。

看点：①令牌产地是否出现「回合小字印证」②报价槽命中数是否由 0 转涨 ③已应用/跨页丢弃对比
④时效是否仍低于上限。另统计固化的对手报价条数与整场业务结果。

只读，不写生产文件。用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_after.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

LOGS = Path.home() / "AppData/Roaming/MaaRacingMaster/logs"
OUT = Path(__file__).resolve().parent / "report_after.md"

TS = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
PAGE = re.compile(r"本帧页=(\S+?)[\s)]")
DROP = re.compile(r"丢弃=\[([^\]]*)\]")
SLOT = re.compile(r"P(\d):(\S+?)\s+(\d+)/(\d+)/(\d+)")
ROUND = re.compile(r"阶段=第(\d)回合出价")
AGE = re.compile(r"时效(\d+)ms")
SRC = re.compile(r"页面令牌产地: (\S+)")


def pick(prefix: str) -> Path:
    hits = sorted(LOGS.glob(f"{prefix}*"),
                  key=lambda p: p.stat().st_size, reverse=True)
    if not hits:
        raise SystemExit(f"没找到日志 {prefix}*")
    return hits[0]


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "MaaRM_20260915_2110"
    path = pick(prefix)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = [f"# 上线后复验：{path.name}（{path.stat().st_size/1024/1024:.2f} MB，{len(lines)} 行）\n"]

    pages: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    src_timeline: list[str] = []
    src_count: Counter[str] = Counter()
    fixed: list[str] = []
    snap_wait: list[str] = []
    applied = 0
    stale = expired = 0
    ages: list[int] = []
    slot_max: dict[tuple[int, int], int] = defaultdict(int)
    stage_lines: list[str] = []
    summary: list[str] = []
    first = last = None
    pending_bids: list[str] = []

    for raw in lines:
        m = TS.search(raw)
        ts = m.group(1) if m else ""
        if ts:
            first = first or ts
            last = ts
        if "页面令牌产地" in raw:
            s = SRC.search(raw)
            v = s.group(1) if s else "?"
            src_count[v] += 1
            src_timeline.append(f"{ts} {v}")
        if "跨页丢弃" in raw:
            p = PAGE.search(raw)
            pages[p.group(1) if p else "?"] += 1
            d = DROP.search(raw)
            if d:
                for k in (x.strip().strip("'\"") for x in d.group(1).split(",")):
                    dropped[k] += 1
        if "已应用" in raw:
            applied += 1
        if "超龄丢弃" in raw:
            stale += 1
        if "过期丢弃" in raw:
            expired += 1
        a = AGE.search(raw)
        if a:
            ages.append(int(a.group(1)))
        if "固化第" in raw:
            fixed.append(f"{ts} {raw.split('] ', 2)[-1].strip()[:150]}")
        if "快照构建" in raw:
            snap_wait.append(f"{ts} {raw.split('] ', 2)[-1].strip()[:170]}")
        if "进入阶段" in raw and ("第" in raw or "大厅" in raw or "结算" in raw or "分红" in raw):
            stage_lines.append(f"{ts} {raw.split('] ', 2)[-1].strip()[:80]}")
        if "我方出价" in raw or "玩家" in raw and "出价" in raw and "固化" not in raw:
            pending_bids.append(f"{ts} {raw.split('] ', 2)[-1].strip()[:120]}")
        if "性能汇总" in raw or "会话汇总" in raw or "报价窗口" in raw:
            summary.append(raw.strip()[:400])
        r = ROUND.search(raw)
        if r:
            rnd = int(r.group(1))
            for pid, _st, c, o, h in SLOT.findall(raw):
                key = (rnd, int(pid))
                if int(h) > slot_max[key]:
                    slot_max[key] = int(h)

    out.append(f"时间范围：{first} → {last}\n")
    out.append("## ① 令牌产地（新增观测行，只在变化时打）\n")
    for k, v in src_count.most_common():
        out.append(f"- `{k}`：{v} 次")
    out.append("\n变化时间线（前 60）：")
    out += [f"  - {x}" for x in src_timeline[:60]]

    out.append("\n## ② 报价槽命中数（每回合每槽的历史最大值）\n")
    out.append("| 回合 | P1 | P2 | P3 | P4 |")
    out.append("|---|---|---|---|---|")
    for rnd in sorted({k[0] for k in slot_max}):
        row = [f"R{rnd}"] + [str(slot_max.get((rnd, p), 0)) for p in (1, 2, 3, 4)]
        out.append("| " + " | ".join(row) + " |")

    out.append(f"\n## ③ 固化事件（对手报价真正入表）：{len(fixed)} 条\n")
    out += [f"- {x}" for x in fixed[:60]]

    out.append(f"\n## ④ 跨页丢弃：{sum(pages.values())} 次\n")
    out.append("本帧页分布：")
    for k, v in pages.most_common():
        out.append(f"- `{k}`：{v}")
    out.append("\n被丢键分布（前 12）：")
    for k, v in dropped.most_common(12):
        out.append(f"- `{k}`：{v}")

    out.append(f"\n## ⑤ 吞吐与时效\n")
    out.append(f"- 已应用：{applied} 次")
    out.append(f"- 超龄丢弃：{stale} 次；过期（跨回合）丢弃：{expired} 次")
    if ages:
        ages_sorted = sorted(ages)
        p50 = ages_sorted[len(ages_sorted) // 2]
        p90 = ages_sorted[int(len(ages_sorted) * 0.9)]
        out.append(f"- 时效样本 {len(ages)}：p50 {p50} ms / p90 {p90} ms / max {ages_sorted[-1]} ms")
        out.append(f"- 超 800 ms 的样本数：{sum(1 for a in ages if a > 800)}")

    out.append(f"\n## ⑥ 快照构建相关：{len(snap_wait)} 条（前 25）\n")
    out += [f"- {x}" for x in snap_wait[:25]]

    out.append(f"\n## ⑦ 阶段推进（前 40）\n")
    out += [f"- {x}" for x in stage_lines[:40]]

    out.append(f"\n## ⑧ 汇总行\n")
    out += [f"- {x}" for x in summary[:12]]

    out.append(f"\n## ⑨ 出价记录相关（前 30）\n")
    out += [f"- {x}" for x in pending_bids[:30]]

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
