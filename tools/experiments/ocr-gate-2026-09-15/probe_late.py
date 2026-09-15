"""只读实验：判定会话尾段各帧是什么页面，以及「第N回合」小字是否在非出价页仍然出现。

决定候选牌子块（玩家列边缘等）到底唯不唯一。
用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_late.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.treasure.detector import TreasureStageDetector  # noqa: E402
from maaracing_master.plugins.treasure.ocr import TreasureOcr  # noqa: E402

PROJ = Path(r"D:\maaracing_assistant\maaracing_master\plugins\treasure")
SESSION = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/treasure/20260915_201931/raw"
OUT = Path(__file__).resolve().parent / "report_late.md"

FRAMES = [380, 420, 460, 500, 640, 655, 660, 665, 670, 680, 690, 700, 710, 730, 750]
KEYS = ["round_label_area", "bid_player1", "bid_player2", "bid_player3",
        "player_name1", "settle_final_price", "settle_my_income", "session_daily_count"]


def main() -> None:
    det = TreasureStageDetector(PROJ)
    ocr = TreasureOcr(PROJ)
    stages = set()
    for k in ("bid_player1", "round_label_area"):
        a = det.plan.stages_for(k)
        if a:
            stages |= set(a)

    out = ["# 尾段与中段帧的页面判定 + 常驻文字\n",
           "| 帧 | 整页首命中 | 回合族探针令牌 | round_label_area | bid_player1 | player_name1 | settle_my_income |",
           "|---|---|---|---|---|---|---|"]
    for n in FRAMES:
        p = SESSION / f"{n:04d}_raw.jpg"
        bgr = cv2.imread(str(p))
        if bgr is None:
            continue
        img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        st, _r, tpl, _b = det._scan(img, None, record=False)
        token = det.probe_page(img, stages)
        res = ocr.recognize_amounts(img, KEYS)
        def cell(k):
            info = res.get(k) or {}
            t = str(info.get("text") or "").replace("|", "/")[:22]
            a = info.get("amount")
            return f"{t}" + (f"({a})" if a is not None else "")
        out.append(f"| {n:04d} | {tpl or st or '—'} | {token} | {cell('round_label_area')} | "
                   f"{cell('bid_player1')} | {cell('player_name1')} | {cell('settle_my_income')} |")
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
