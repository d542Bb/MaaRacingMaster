# -*- coding: utf-8 -*-
"""最小实验：在「被消费的那一帧」上现场判定页面，能否拦住跨页脏读、代价多少。

背景（#453 实证）：`_ocr_push` 投递的页面令牌是 `_last_raw_stage`，由观察线程按
STAGE_JUDGE_INTERVAL_MS=300ms 周期写入。决策线程每 tick（~110ms）自截一帧投递，
令牌却是「最近一次观察判定」——两个捕获流、两种节拍。转场恰好落在窗口里时，
令牌仍是旧页、像素已是新页，门控按旧页放行。日志实证：帧 997 令牌='领取分红'
而读到大厅场次卡的 300,000。

本实验验证「帧内判定」的两个前提：
  1. 正确性：对同一帧跑判定，转场帧能否判出「不是结算页」；
  2. 成本：判定集（该页标志锚点）相对 OCR 单帧（~110ms）占多少。

判定集取自 policy 派生而非硬编码：active_for(stage) 中 stage_stage[锚点]==stage 者，
即「命中即进入该阶段」的锚点。对 settle_* 信号，allowed={中标结算,领取分红}。

用法：
    .venv\\Scripts\\python.exe tools/experiments/frame-page-token/probe.py <会话目录> [起帧] [止帧]
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
DEFAULT_SESSION = "20260915_121005"
SETTLE_SIGNALS = ("settle_my_income", "settle_profit",
                  "settle_final_price", "settle_total_price")


def pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def proof_anchors(plan, stages) -> set[str]:
    """该页的「标志锚点」：active 里归属阶段就是本阶段的模板锚点。

    纯派生，不新增 policy 字段——与 stages_for/ocr_for 同一份真源的另一面。
    """
    out: set[str] = set()
    for st in stages:
        for a in (plan.active_for(st) or ()):
            if plan.stage_stage.get(a) == st:
                out.add(a)
    return out


def main() -> int:
    session = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path.home() / f"AppData/Roaming/MaaRacingMaster/debug/treasure/{DEFAULT_SESSION}")
    lo = int(sys.argv[2]) if len(sys.argv) > 2 else 994
    hi = int(sys.argv[3]) if len(sys.argv) > 3 else 1007

    det = TreasureStageDetector(PLUGIN)
    plan = det.plan
    if plan is None:
        print("[FAIL] DetectionPlan 未加载")
        return 1

    allowed = plan.stages_for(SETTLE_SIGNALS[0])
    proof = proof_anchors(plan, allowed)
    print(f"信号 {SETTLE_SIGNALS[0]} 允许阶段={sorted(allowed)}")
    print(f"派生标志锚点={sorted(proof)}")
    print(f"扫描序（prio 降序）："
          f"{sorted(proof, key=lambda n: -plan.spec[n].stage_priority)}")
    print()

    frames = sorted((session / "raw").glob("*_raw.jpg"))
    if not frames:
        print(f"[FAIL] {session}/raw 无 *_raw.jpg")
        return 2

    print(f"{'帧':>6} {'proof判定':>22} {'令牌':>12} {'probe ms':>9} "
          f"{'全量判定':>22} {'全量 ms':>8}  裁决")
    probe_ms, full_ms = [], []
    for f in frames:
        idx = int(f.stem.split("_")[0])
        if not (lo <= idx <= hi):
            continue
        bgr = cv2.imread(str(f))
        if bgr is None:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

        t = time.perf_counter()
        r = det.detect(rgb, proof)
        d_probe = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        rf = det.detect(rgb, None)
        d_full = (time.perf_counter() - t) * 1000

        token = plan.stage_stage.get(r.hit_anchor) if r.hit_anchor else None
        verdict = "放行" if token in allowed else "丢弃"
        probe_ms.append(d_probe)
        full_ms.append(d_full)
        print(f"{idx:>6} {str(r.hit_anchor):>22} {str(token):>12} {d_probe:>9.2f} "
              f"{str(rf.hit_anchor):>22} {d_full:>8.2f}  {verdict}")

    if probe_ms:
        print(f"\nprobe（判定集按 prio 短路，命中即返回）："
              f"均 {sum(probe_ms)/len(probe_ms):.2f}  p50 {pct(probe_ms,.5):.2f}  "
              f"max {max(probe_ms):.2f} ms")
        print(f"全量对照（13 锚点）："
              f"均 {sum(full_ms)/len(full_ms):.2f}  p50 {pct(full_ms,.5):.2f}  "
              f"max {max(full_ms):.2f} ms")
        print(f"probe 占 OCR 单帧（~110ms）比例：p50 "
              f"{pct(probe_ms,.5)/110*100:.1f}%  max {max(probe_ms)/110*100:.1f}%")
        print("注：绝对 ms 受同期负载抬高，当相对量级用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
