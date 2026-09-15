# -*- coding: utf-8 -*-
"""工作基线验证：帧内判定改成「本帧现场」后，出价场景的令牌是否与原来等价。

修复纪律要求「修复条目自证未打穿工作基线」——本次只改了令牌产地（投递快照 → 帧内判定），
必须证明原本正常的场景在改动后仍正常。出价期是主要风险面：它的标志锚点归属
`__round_phase__` 哨兵，令牌要靠 detector 实例化出「第N回合出价」，若拿不到回合号就会
退化成 None（fail-closed 丢读）。

口径：以**全量判定**（active_rois=None，13 锚点，不依赖任何裁剪先验）为参考真值，
比对帧内判定在这些帧上的产出。两者都跑同一帧、同一 detector，模拟生产序列
（观察线程 detect 更新权威回合号 → worker probe 读取）。

用法：
    .venv\\Scripts\\python.exe tools/experiments/frame-page-token/verify_round_baseline.py <会话目录> [--step N]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_master.plugins.treasure.detector import TreasureStageDetector  # noqa: E402

PLUGIN = REPO / "maaracing_master/plugins/treasure"
DEFAULT_SESSION = "20260915_090037"


def main() -> int:
    session = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path.home() / f"AppData/Roaming/MaaRacingMaster/debug/treasure/{DEFAULT_SESSION}")
    step = 5
    for i, a in enumerate(sys.argv):
        if a == "--step":
            step = max(1, int(sys.argv[i + 1]))

    det = TreasureStageDetector(PLUGIN)
    plan = det.plan
    if plan is None:
        print("[FAIL] DetectionPlan 未加载")
        return 1

    bid_stages = plan.stages_for("bid_player1")
    settle_stages = plan.stages_for("settle_my_income")
    print(f"出价信号允许阶段={sorted(bid_stages)}")
    print(f"出价 proof 锚点={sorted(det._proof_anchors(bid_stages))}")
    print(f"结算 proof 锚点={sorted(det._proof_anchors(settle_stages))}")

    frames = sorted((session / "raw").glob("*_raw.jpg"))[::step]
    if not frames:
        print(f"[FAIL] {session}/raw 无 *_raw.jpg")
        return 2
    print(f"\n会话 {session.name} 抽样 {len(frames)} 帧（step={step}）\n")

    bid_total = bid_agree = bid_lost = 0
    settle_total = settle_agree = settle_lost = 0
    probe_ms, ref_ms = [], []
    disagreements: list[tuple[int, object, object]] = []

    for f in frames:
        idx = int(f.stem.split("_")[0])
        bgr = cv2.imread(str(f))
        if bgr is None:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        t = time.perf_counter()
        ref = det.detect(rgb, None)
        ref_ms.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        got = det.probe_page(rgb, bid_stages)
        probe_ms.append((time.perf_counter() - t) * 1000)

        ref_stage = ref.stage
        if isinstance(ref_stage, str) and "回合出价" in ref_stage:
            bid_total += 1
            if got == ref_stage:
                bid_agree += 1
            else:
                bid_lost += 1
                disagreements.append((idx, ref_stage, got))
        if ref_stage in ("中标结算", "领取分红"):
            settle_total += 1
            if det.probe_page(rgb, settle_stages) == ref_stage:
                settle_agree += 1
            else:
                settle_lost += 1

    def pct(xs, q):
        s = sorted(xs)
        return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))] if s else 0.0

    print(f"出价帧（参考真值=第N回合出价）：{bid_total} 帧 | 帧内判定一致 {bid_agree} "
          f"| 丢失 {bid_lost}")
    print(f"结算帧（参考真值=中标结算/领取分红）：{settle_total} 帧 | 一致 {settle_agree} "
          f"| 丢失 {settle_lost}")
    if disagreements:
        print("\n不一致明细（帧号, 参考真值, 帧内判定）：")
        for row in disagreements[:20]:
            print(f"  {row}")
    print(f"\n耗时：帧内判定 p50 {pct(probe_ms, .5):.2f} / p95 {pct(probe_ms, .95):.2f} ms"
          f"  |  全量对照 p50 {pct(ref_ms, .5):.2f} / p95 {pct(ref_ms, .95):.2f} ms")
    verdict = ("未打穿工作基线 ✅" if bid_lost == 0 and settle_lost == 0
               else "存在退化 ❌ 需评估")
    print(f"判定：{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
