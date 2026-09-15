"""只读：打印指定时间窗内的心跳行（用于核实「新一场开局槽位是否仍带着上一场的锁定值」）。

用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_window.py 21:11:20 21:11:36
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LOGS = Path.home() / "AppData/Roaming/MaaRacingMaster/logs"
OUT = Path(__file__).resolve().parent / "report_window.md"
TS = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
NOISE = re.compile(r"槽位=\d+ \|")


def main() -> None:
    start, end = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("21:11:20", "21:11:36")
    path = sorted(LOGS.glob("MaaRM_20260915_2110*"),
                  key=lambda p: p.stat().st_size, reverse=True)[0]
    out = [f"# 心跳窗口 {start}~{end}（{path.name}）\n", "```"]
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS.search(raw)
        if not m or not (start <= m.group(1) <= end):
            continue
        if "心跳" in raw:
            line = raw.split("] ", 2)[-1]
            out.append(f"{m.group(1)}  {line[:210]}")
    out.append("```")
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}（{len(out)} 行）")


if __name__ == "__main__":
    main()
