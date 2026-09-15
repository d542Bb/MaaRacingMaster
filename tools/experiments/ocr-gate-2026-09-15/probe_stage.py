"""只读实验：取公开报价窗口内与「阶段」相关的日志行，确认阶段标签当时说的是哪一页。

用途：判断「用阶段标签替代像素目击」是否可行（阶段在这段窗口里究竟准不准）。
用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_stage.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LOGS = Path.home() / "AppData/Roaming/MaaRacingMaster/logs"
OUT = Path(__file__).resolve().parent / "report_stage.md"
TS = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")

WINDOW = ("20:20:00", "20:20:35")   # R1 面板关闭(20:20:09) → R2 开始(20:20:29)


def main() -> None:
    path = sorted(LOGS.glob("MaaRM_20260915_2019*.log*"),
                  key=lambda p: p.stat().st_size, reverse=True)[0]
    keep: list[str] = []
    stage_lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS.search(raw)
        if not m:
            continue
        ts = m.group(1)
        if WINDOW[0] <= ts <= WINDOW[1]:
            if "阶段" in raw or "回合" in raw:
                keep.append(raw.rstrip())
        if "阶段切换" in raw or "阶段:" in raw or "阶段=" in raw:
            stage_lines.append(raw.rstrip())

    out = [f"# 阶段证据：{path.name}\n",
           f"窗口 {WINDOW[0]}~{WINDOW[1]}（R1 面板关闭 → R2 开始）含「阶段/回合」的行：{len(keep)} 条\n", "```"]
    out += keep[:120]
    out.append("```\n")
    out.append(f"## 全场「阶段切换」类日志共 {len(stage_lines)} 条（前 40）\n```")
    out += stage_lines[:40]
    out.append("```")
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
