# -*- coding: utf-8 -*-
"""工作基线验证：帧内页面令牌在「探针独立工作」口径下的召回与串页安全性。

修复纪律要求「修复条目自证未打穿工作基线」。本脚本锁两个面：

  1. **召回**：生产里探针跑在 OCR worker 线程，而 detector 的 `_last_round` 是**观察
     线程**的生产物（探针只读不写）。所以探针必须在不依赖任何跨帧状态的前提下判出本页
     ——脚本因此用**独立 detector 实例**跑探针，与真值实例互不喂状态，口径等同生产。
     用同一个实例「先全量 detect 再探针」会把回合号喂新鲜，测出的是生产不存在的口径。
  2. **串页**：令牌只认同族信号——出价帧不得让结算读数通过，反之亦然。

令牌词汇（见 detector.probe_page）：出价面板整族共用同一块招牌、门控按族放行，故回合族
上产出哨兵 `ROUND_PHASE_STAGE`（不推导第几回合——那是跨线程状态，且门控用不到它）；具体
页（中标结算/领取分红）仍产出阶段名。

用法：
    .venv\\Scripts\\python.exe tools/experiments/frame-page-token/verify_round_baseline.py [会话目录] [--step N]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_master.core.navkit import ROUND_PHASE_STAGE  # noqa: E402
from maaracing_master.plugins.treasure.detector import TreasureStageDetector  # noqa: E402

PLUGIN = REPO / "maaracing_master/plugins/treasure"
DEFAULT_SESSION = "20260915_090037"
_SETTLE = ("中标结算", "领取分红")


def main() -> int:
    session = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path.home() / f"AppData/Roaming/MaaRacingMaster/debug/treasure/{DEFAULT_SESSION}")
    step = 5
    for i, a in enumerate(sys.argv):
        if a == "--step":
            step = max(1, int(sys.argv[i + 1]))

    probe_det = TreasureStageDetector(PLUGIN)   # 探针口径：独立实例，不吃任何跨帧状态
    truth_det = TreasureStageDetector(PLUGIN)   # 真值口径：全量判定（观察线程的用法）
    plan = probe_det.plan
    if plan is None:
        print("[FAIL] DetectionPlan 未加载")
        return 1

    bid_stages = plan.stages_for("bid_player1")
    settle_stages = plan.stages_for("settle_my_income")
    is_round = probe_det.is_round_stage
    print(f"出价信号允许阶段={sorted(bid_stages)}")
    print(f"出价 proof 锚点={sorted(probe_det._proof_anchors(bid_stages))}")
    print(f"结算 proof 锚点={sorted(probe_det._proof_anchors(settle_stages))}")

    frames = sorted((session / "raw").glob("*_raw.jpg"))[::step]
    if not frames:
        print(f"[FAIL] {session}/raw 无 *_raw.jpg")
        return 2
    print(f"\n会话 {session.name} 抽样 {len(frames)} 帧（step={step}）\n")

    bid_total = bid_ok = 0
    settle_total = settle_ok = 0
    leaks: list[tuple] = []
    probe_ms: list[float] = []

    for f in frames:
        bgr = cv2.imread(str(f))
        if bgr is None:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        idx = int(f.stem.split("_")[0])

        ref = truth_det.detect(rgb, None).stage

        t = time.perf_counter()
        bid_token = probe_det.probe_page(rgb, bid_stages)
        settle_token = probe_det.probe_page(rgb, settle_stages)
        probe_ms.append((time.perf_counter() - t) * 1000)

        if is_round(ref):
            bid_total += 1
            if bid_token == ROUND_PHASE_STAGE:
                bid_ok += 1
            else:
                leaks.append((idx, "出价帧漏判", ref, bid_token))
            if settle_token in _SETTLE:      # 否则结算读数会被放行 = 脏读
                leaks.append((idx, "出价帧放行结算读数", ref, settle_token))
        elif ref in _SETTLE:
            settle_total += 1
            if settle_token == ref:
                settle_ok += 1
            else:
                leaks.append((idx, "结算帧漏判", ref, settle_token))
            if bid_token == ROUND_PHASE_STAGE:
                leaks.append((idx, "结算帧放行出价读数", ref, bid_token))

    def pct(xs, q):
        s = sorted(xs)
        return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))] if s else 0.0

    print(f"出价帧（真值=第N回合出价）：{bid_total} 帧 | 令牌判出 {bid_ok} "
          f"| 漏判 {bid_total - bid_ok}")
    print(f"结算帧（真值=中标结算/领取分红）：{settle_total} 帧 | 令牌一致 {settle_ok} "
          f"| 漏判 {settle_total - settle_ok}")
    if leaks:
        print("\n串页/漏判明细（帧号, 说明, 真值, 令牌）：")
        for row in leaks[:20]:
            print(f"  {row}")
    print(f"\n探针耗时：p50 {pct(probe_ms, .5):.2f} / p95 {pct(probe_ms, .95):.2f} ms"
          f"（两个阶段集合计，命中即短路）")
    ok = not leaks and bid_total > 0
    print(f"判定：{'未打穿工作基线 ✅' if ok else '存在退化 ❌ 需评估'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
