"""只读复验（第二段）：把剩余的跨页丢弃拆到「令牌 × 丢的键组合」，并统计业务结果。

只读，不写生产文件。用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_after2.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

LOGS = Path.home() / "AppData/Roaming/MaaRacingMaster/logs"
OUT = Path(__file__).resolve().parent / "report_after2.md"

TS = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
PAGE = re.compile(r"本帧页=(\S+?)[\s)]")
DROP = re.compile(r"丢弃=\[([^\]]*)\]")

ROUND_FAMILY = {"bid_player1", "bid_player2", "bid_player3", "bid_player4",
                "bid_result_amount_box", "player_name1", "player_name2",
                "player_name3", "player_name4", "round_label_area"}


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "MaaRM_20260915_2110"
    path = sorted(LOGS.glob(f"{prefix}*"), key=lambda p: p.stat().st_size, reverse=True)[0]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out = [f"# 丢弃归因与业务结果：{path.name}\n"]

    groups: Counter[tuple] = Counter()
    span: dict[tuple, list[str]] = defaultdict(list)
    round_family_hits: Counter[str] = Counter()
    stage = ""
    drops_during_round_stage = Counter()
    seen_msgs: dict[str, str] = {}

    for raw in lines:
        ts = (TS.search(raw) or [None, ""])[1] if TS.search(raw) else ""
        if "进入阶段:" in raw:
            m = re.search(r"进入阶段: (\S+)", raw)
            if m:
                stage = m.group(1)
        if "跨页丢弃" in raw:
            p = PAGE.search(raw)
            token = p.group(1) if p else "?"
            d = DROP.search(raw)
            keys = tuple(sorted(x.strip().strip("'\"") for x in d.group(1).split(","))) if d else ()
            groups[(token, keys)] += 1
            span[(token, keys)].append(ts)
            for k in keys:
                if k in ROUND_FAMILY:
                    round_family_hits[k] += 1
            if stage.startswith("第"):
                drops_during_round_stage[(token, keys)] += 1
        # 收集「快照 / 结果 / 我方 / 玩家出价」类消息的形态（去数字后取模板）
        for kw in ("快照", "我方出价", "本场", "结果", "落盘", "结算", "利润", "收入"):
            if kw in raw:
                msg = raw.split("] ", 3)[-1]
                shape = re.sub(r"[\d,\.]+", "#", msg)[:90]
                seen_msgs.setdefault(shape, ts or "")

    out.append("## 跨页丢弃：按「令牌 × 被丢键组合」拆开\n")
    out.append("| 令牌 | 被丢的键 | 次数 | 时间段 |")
    out.append("|---|---|---|---|")
    for (token, keys), n in groups.most_common():
        t = f"{span[(token, keys)][0]}~{span[(token, keys)][-1]}"
        out.append(f"| `{token}` | {', '.join(keys)} | {n} | {t} |")

    out.append("\n## 回合族信号被丢次数（关键：应为 0）\n")
    if round_family_hits:
        for k, v in round_family_hits.most_common():
            out.append(f"- `{k}`：{v}")
    else:
        out.append("- 无")

    out.append("\n## 阶段=第N回合出价 期间发生的丢弃\n")
    if drops_during_round_stage:
        for (token, keys), n in drops_during_round_stage.most_common():
            out.append(f"- `{token}` + {list(keys)}：{n}")
    else:
        out.append("- 无")

    out.append("\n## 日志消息形态（去数字后的模板 → 首次出现时间）\n")
    for shape, ts in list(seen_msgs.items())[:60]:
        out.append(f"- {ts}  {shape}")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
