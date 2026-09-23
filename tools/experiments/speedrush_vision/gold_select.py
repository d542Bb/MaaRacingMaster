"""金标候选帧清单生成（分层，零标注）。输出 depth_review/gold_frames.csv。

分层承 README「金标规模与统计口径」：{墙, 抬升路缘} × {有障碍, 无障碍} + 三类必选
（已知坏帧 / R2 撞墙帧 / 右路肩帧）+ 连续段（≥10 帧，弯道与贴墙）。
来源（维护者口径）：优先用存量 demos 连续段（113809_p2 906~、113932_p1 714~），
不新录。badframes 为 R2 撞墙复盘现场（overlay 定名，独立标尺）。
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

APP = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMO = APP / "demos"
BAD = APP / "control_traces"

PLAN = [
    # (stratum, path列表)
    ("wall", [DEMO / "20260922_113724_p1" / "frames" / f"{i:06d}.jpg"
              for i in (100, 180, 260, 340, 420, 500, 580, 660)]),
    ("kerb", [DEMO / "20260919_202138_p2" / "frames" / f"{i:06d}.jpg"
              for i in (1160, 1220, 1280, 1340, 1400, 1460)]),
    ("curve_cont", [DEMO / "20260922_113809_p2" / "frames" / f"{i:06d}.jpg"
                    for i in range(906, 921)]),
    ("curve_cont2", [DEMO / "20260922_113932_p1" / "frames" / f"{i:06d}.jpg"
                     for i in range(714, 729)]),
    ("obstacle", [BAD / "badframes_20260922_204603_p2" / f"fid_{i}.jpg"
                  for i in (3426, 3503, 3582, 3658)]),
    ("wallhug", [BAD / "badframes_20260922_201847_p1" / f"fid_{i}.jpg"
                 for i in (1418, 2152, 2390)] +
                [BAD / "badframes_20260922_201932_p2" / f"fid_{i}.jpg"
                 for i in (3072, 3306)] +
                [BAD / "badframes_20260922_204519_p1" / "fid_1295.jpg"]),
]

if __name__ == "__main__":
    out = APP / "depth_review" / "gold_frames.csv"
    n = miss = 0
    with out.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["path", "stratum"])
        for stratum, paths in PLAN:
            for p in paths:
                if not p.exists():
                    print(f"[miss] {p}")
                    miss += 1
                    continue
                wr.writerow([str(p), stratum])
                n += 1
    print(f"[gold_select] {out}  共 {n} 帧（缺 {miss}）")
