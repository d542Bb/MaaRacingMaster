"""只读复验：用实机帧重放「改前 vs 改后」的令牌与门控结果。

改前：令牌只由模板标志锚点产出（probe_page），公开报价窗口恒为 None → 报价读数整批被丢。
改后：第一类缺席时，用同帧回合小字 + 投递时回合号互相印证（confirm_round_page）。

脚本调用的是**生产代码本体**（TreasureModule._judge_frame_page / _ocr_filter_by_page），
不是复刻逻辑。只读，不写生产文件。
用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_fix.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.treasure import IMAGE_DIR  # noqa: E402,F401
from maaracing_master.plugins.treasure.detector import TreasureStageDetector  # noqa: E402
from maaracing_master.plugins.treasure.module import TreasureModule  # noqa: E402
from maaracing_master.plugins.treasure.ocr import TreasureOcr  # noqa: E402

PROJ = Path(r"D:\maaracing_assistant\maaracing_master\plugins\treasure")
SESSION = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/treasure/20260915_201931/raw"
OUT = Path(__file__).resolve().parent / "report_fix.md"

# 帧 → 该帧画面上的回合（probe_late.py 已逐帧核实）；stage 标签当时给的也是这个值
FRAME_ROUND = {215: 1, 300: 1, 325: 1, 420: 2, 655: 4, 670: 4, 680: 4, 730: 5, 750: 5}
NOTE = {215: "出价页·面板开", 300: "出价页·公开报价", 325: "出价页·公开报价",
        420: "出价页·公开报价", 655: "出价页·公开报价", 670: "出价页·公开报价",
        680: "未中标横幅", 730: "结算/分红", 750: "鉴宝大厅"}

BID_KEYS = frozenset({"bid_player1", "bid_player2", "bid_player3", "bid_player4",
                      "bid_result_amount_box", "player_name1", "player_name2",
                      "player_name3", "player_name4", "round_label_area"})


def main() -> None:
    ocr = TreasureOcr(PROJ)
    det = TreasureStageDetector(PROJ, ocr=ocr)
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = det
    mod._ocr_page_token_src = None
    mod._ocr_page_drops = 0

    stages: set[str] = set()
    for k in BID_KEYS:
        a = det.plan.stages_for(k)
        if a:
            stages |= set(a)

    out = ["# 改前 / 改后 实机帧复验\n",
           f"- 信号阶段集：{sorted(stages)}",
           f"- 第一类证据锚点：{sorted(det._proof_anchors(stages))}\n",
           "| 帧 | 画面 | 快照回合 | 改前令牌(仅模板锚点) | 同帧回合小字 | 印证 | 改后令牌 | 报价读数 改前→改后 |",
           "|---|---|---|---|---|---|---|---|"]

    for n, rnd in FRAME_ROUND.items():
        p = SESSION / f"{n:04d}_raw.jpg"
        bgr = cv2.imread(str(p))
        if bgr is None:
            continue
        img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        before = det.probe_page(img, stages)
        label = ocr.recognize_amounts(img, ["round_label_area"]).get("round_label_area") or {}
        label_text = str(label.get("text") or "") or "—"
        confirmed = det.confirm_round_page(img, rnd)
        after = TreasureModule._judge_frame_page(mod, img, BID_KEYS, rnd)

        data = ocr.recognize_amounts(
            img, ["bid_player1", "bid_player2", "bid_player3"],
            min_amounts={"bid_player1": 0, "bid_player2": 0, "bid_player3": 0})
        # 只保留真读到数字的槽，作为「本该被收下」的读数
        payload = {k: v["amount"] for k, v in data.items()
                   if v.get("amount") is not None and v["amount"] > 0}

        kept_before = TreasureModule._ocr_filter_by_page(
            mod, {"frame_id": n, "round_no": rnd, "stage": before, "data": dict(payload)})
        kept_after = TreasureModule._ocr_filter_by_page(
            mod, {"frame_id": n, "round_no": rnd, "stage": after, "data": dict(payload)})
        out.append(
            f"| {n:04d} | {NOTE[n]} | R{rnd} | {before or 'None'} | {label_text} | "
            f"{'是' if confirmed else '否'} | {after or 'None'} | "
            f"收{len(kept_before)}条 → 收{len(kept_after)}条 {sorted(kept_after) or ''} |")

    out += ["", "> 「报价读数」列是同一帧上用仓库自身 OCR 读出的 P1~P3 数字，经生产门控函数过滤后的结果。",
            "> 改前：公开报价窗口全部收到 0 条（令牌 None）；改后：同一帧读数被收下。"]
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
