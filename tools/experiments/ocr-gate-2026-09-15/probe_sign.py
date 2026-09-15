"""只读实验：回答「能不能把牌子留下来」——即出价页在公开报价窗口是否还有

  (a) 现成的模板锚点可用（无需新素材），或
  (b) 一块既常驻又只在本页出现的图块（需新素材，但可行）。

方法：对同一场会话中不同页面的实机帧，
  1. 逐锚点单独打分（禁用短路），得到「锚点 × 帧」得分矩阵；
  2. 以公开报价帧为参考裁出候选图块，逐帧做多尺度匹配，得到「候选块 × 帧」矩阵。

判据：好牌子的定义 = 在本页帧（面板开/面板关）都 ≥ 阈值，且在非本页帧都明显低。
只读，不写生产文件。用法：.venv\\Scripts\\python.exe tools\\experiments\\ocr-gate-2026-09-15\\probe_sign.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.core.template_match import best_match_score, load_template  # noqa: E402
from maaracing_master.plugins.treasure import IMAGE_DIR  # noqa: E402
from maaracing_master.plugins.treasure.detector import TreasureStageDetector  # noqa: E402

PROJ = Path(r"D:\maaracing_assistant\maaracing_master\plugins\treasure")
SESSION = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/treasure/20260915_201931/raw"
OUT = Path(__file__).resolve().parent / "report_sign.md"

# 覆盖整场（帧身份已由 probe_late.py 逐帧核实）：
#   非出价页：0005 大厅 / 0060 场次 / 0120 选鉴宝师 / 0160 回合横幅 / 0680 未中标横幅 / 0730 结算 / 0750 大厅
#   出价页·面板开：0215 / 0520 / 0620        出价页·面板关（公开报价）：0300 / 0325 / 0420 / 0655 / 0670
FRAMES = [5, 60, 120, 160, 215, 300, 325, 420, 520, 620, 655, 670, 680, 730, 750]

KEYS = ["bid_player1", "bid_player2", "bid_player3", "bid_player4",
        "bid_result_amount_box", "player_name1", "player_name2", "player_name3",
        "player_name4", "round_label_area"]

# 候选「牌子」块（归一化 rect），全部取自公开报价帧的常驻区域
CANDS: dict[str, tuple[float, float, float, float]] = {
    "回合小字行(含字)": (0.395, 0.145, 0.475, 0.205),
    "回合小字行(窄)": (0.400, 0.155, 0.440, 0.195),
    "主按钮底板(左)": (0.390, 0.795, 0.440, 0.862),
    "主按钮底板(右)": (0.515, 0.795, 0.562, 0.862),
    "玩家列左边缘": (0.148, 0.280, 0.170, 0.810),
    "玩家1卡顶条": (0.148, 0.272, 0.274, 0.285),
    "玩家2卡顶条": (0.146, 0.435, 0.274, 0.448),
    "玩家行间隙": (0.150, 0.360, 0.274, 0.380),
    "左上HUD": (0.000, 0.000, 0.150, 0.100),
    "数字键盘区": (0.565, 0.505, 0.600, 0.570),
}


def px(rect, W, H):
    x1, y1, x2, y2 = rect
    return (max(0, int(x1 * W)), max(0, int(y1 * H)),
            max(1, int(x2 * W)) - max(0, int(x1 * W)),
            max(1, int(y2 * H)) - max(0, int(y1 * H)))


def main() -> None:
    det = TreasureStageDetector(PROJ)
    plan = det.plan
    if plan is None:
        raise SystemExit("plan 未装配")
    out: list[str] = ["# 牌子实验：出价页还有没有可用的常驻标志\n"]

    stages: set[str] = set()
    for k in KEYS:
        a = plan.stages_for(k)
        if a:
            stages |= set(a)
    proof = sorted(det._proof_anchors(stages))
    detect_anchors = sorted(plan.detect_anchors)
    out.append(f"- 本批信号声明的阶段集：{sorted(stages)}")
    out.append(f"- `_proof_anchors` 现有标志锚点：{proof}")
    out.append(f"- 参与扫描的锚点全集（{len(detect_anchors)}）：{detect_anchors}")
    missing = sorted(set(proof) - set(detect_anchors))
    out.append(f"- **标志锚点里不在扫描全集中的**：{missing or '无'}（在则说明探针永远扫不到它）\n")

    loaded: dict[int, np.ndarray] = {}
    for n in FRAMES:
        bgr = cv2.imread(str(SESSION / f"{n:04d}_raw.jpg"))
        if bgr is None:
            continue
        loaded[n] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    nums = sorted(loaded)

    out.append("## 各帧的整页扫描首命中（判断这一帧是什么页）\n")
    out.append("| 帧 | 首命中锚点 | 判定阶段 |")
    out.append("|---|---|---|")
    for n in nums:
        st, rnd, tpl, _box = det._scan(loaded[n], None, record=False)
        out.append(f"| {n:04d} | {tpl} | {st} |")
    out.append("")

    # ---- 1) 现有模板锚点逐点打分 ----
    tpl_anchors = [a for a in detect_anchors if plan.spec[a].templates]
    out.append(f"## 现有模板锚点 × 帧（单锚点扫描，{len(tpl_anchors)} 锚点）\n")
    header = "| 锚点 | " + " | ".join(f"{n:04d}" for n in nums) + " |"
    out.append(header)
    out.append("|---" * (len(nums) + 1) + "|")
    for a in tpl_anchors:
        row = [a]
        for n in nums:
            det._scan(loaded[n], {a}, record=True)
            row.append(f"{det._last_detect_scores.get(a, 0.0):.3f}")
        out.append("| " + " | ".join(row) + " |")
    out.append("")

    # ---- 2) 候选牌子块 ----
    ref = loaded.get(325)
    if ref is None:
        raise SystemExit("缺参考帧 0325")
    H, W = ref.shape[:2]
    out.append("## 候选牌子块 × 帧（参考块取自 0325 公开报价帧）\n")
    out.append(f"- 尺度表：{tuple(round(s, 2) for s in plan.scales)}")
    out.append(f"- 阈值：默认 {plan.default_threshold}\n")
    out.append("| 候选块 | 标准差 | " + " | ".join(f"{n:04d}" for n in nums) + " |")
    out.append("|---" * (len(nums) + 2) + "|")
    for name, rect in CANDS.items():
        x, y, w, h = px(rect, W, H)
        patch = ref[y:y + h, x:x + w]
        if patch.size == 0:
            continue
        std = float(np.std(patch.astype(np.float32)))
        row = [name, f"{std:.1f}"]
        for n in nums:
            img = loaded[n]
            img_roi = px(rect, img.shape[1], img.shape[0])
            _box, s = best_match_score(img, patch, scales=plan.scales, roi=img_roi)
            row.append(f"{float(s):.3f}")
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    out.append("> 参考帧 0325 自身为 1.000（自比）。好牌子的判据：出价页帧（0215 面板开 / 0300 0325 0420 0655 0670 面板关）"
               "普遍 ≥ 0.80，非出价页帧（0005 0060 0120 0160 0680 0730 0750）明显偏低。")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {OUT}")


if __name__ == "__main__":
    main()
