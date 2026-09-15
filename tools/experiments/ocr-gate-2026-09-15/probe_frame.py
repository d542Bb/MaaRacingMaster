"""只读帧探针：对「公开报价」窗口的实机帧，同时验证

  ① detector.probe_page 给出的页面令牌（闸② 的判据输入）是什么；
  ② 同一帧上 OCR 究竟认不认得出 4 个玩家的报价数字。

两者合起来回答：报价数字是被闸门丢掉的（OCR 读得出来），还是根本读不出来。

只读，不写生产文件。用法：
  .venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_frame.py
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
OUT = Path(__file__).resolve().parent / "report_frame.md"

# 公开报价窗口（R1 面板关闭 20:20:09 → R2 开始 20:20:29）附近的实机帧
FRAMES = [200, 215, 230, 245, 260, 280, 300, 315, 325]

KEYS = [
    "bid_player1", "bid_player2", "bid_player3", "bid_player4",
    "bid_result_amount_box", "round_label_area",
    "player_name1", "player_name2", "player_name3", "player_name4",
]
ROUND_ANCHORS = {"round_big_banner", "smart_bid_btn"}


def main() -> None:
    ocr = TreasureOcr(PROJ)
    det = TreasureStageDetector(PROJ, ocr=ocr)
    plan = det.plan
    if plan is None:
        raise SystemExit("plan 未装配，探针无法运行")

    stages: set[str] = set()
    for k in KEYS:
        allowed = plan.stages_for(k)
        if allowed:
            stages |= set(allowed)
    proof = det._proof_anchors(stages)

    out: list[str] = ["# 帧探针报告：公开报价窗口（20260915_201931）\n"]
    out.append(f"- 本批信号反查得到的阶段集：{sorted(stages)}")
    out.append(f"- `_proof_anchors` 给出的标志锚点集：{sorted(proof)}")
    out.append(f"- 其中回合族常驻候选：{sorted(proof & ROUND_ANCHORS)}\n")

    for n in FRAMES:
        p = SESSION / f"{n:04d}_raw.jpg"
        if not p.exists():
            out.append(f"## 帧 {n:04d}：文件不存在\n")
            continue
        bgr = cv2.imread(str(p))
        if bgr is None:
            out.append(f"## 帧 {n:04d}：读取失败\n")
            continue
        frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        token = det.probe_page(frame, stages)
        anchor_scores: dict[str, float] = {}
        det._scan(frame, ROUND_ANCHORS, record=True)
        anchor_scores = dict(det._last_detect_scores)
        tpl_hit = det._last_hit_roi_key
        res = ocr.recognize_amounts(
            frame, KEYS,
            min_amounts={k: 0 for k in KEYS if k.startswith("bid_player")},
        )
        out.append(f"## 帧 {n:04d}  令牌={token!r}  回合族锚点得分={anchor_scores}  命中={tpl_hit}")
        out.append("")
        out.append("| 键 | amount | text |")
        out.append("|---|---|---|")
        for k in KEYS:
            info = res.get(k) or {}
            amt = info.get("amount")
            txt = str(info.get("text") or "").replace("|", "/")[:40]
            if amt is None and not txt:
                continue
            out.append(f"| {k} | {amt} | {txt} |")
        out.append("")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
