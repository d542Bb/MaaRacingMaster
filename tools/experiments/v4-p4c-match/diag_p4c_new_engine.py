# -*- coding: utf-8 -*-
"""P4c 终验：新 detector（template_match 统一引擎 + 按锚点 colorspace）对真机录帧直跑。

同政策对照（关键）：录帧是 2026-09-06 v3 时代产物，其 trace 分数属**旧数据代**
（现政策 settle_title 等模板集已不同）——拿 trace 当真值会把数据代差算进引擎差。
本脚本用同一帧同时喂「现政策 + 旧全灰度引擎」与「现政策 + 新引擎」，比对二者
hit_anchor/stage 差异：这才是 P4c 收敛（引擎替换 + 按锚点 colorspace）的净效应。

用法：
  .venv\\Scripts\\python.exe tools/experiments/v4-p4c-match/diag_p4c_new_engine.py [--per-anchor 30] [--vs-trace]
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_assistant.plugins.treasure.detector import TreasureStageDetector  # noqa: E402

POLICY_PATH = (ROOT / "maaracing_assistant" / "plugins" / "treasure"
               / "resources" / "policy" / "treasure.policy.json")
IMAGE_DIR = ROOT / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "image"
_gray_tpl_cache: dict[str, np.ndarray | None] = {}


def _load_gray(name: str) -> np.ndarray | None:
    if name in _gray_tpl_cache:
        return _gray_tpl_cache[name]
    img = cv2.imread(str(IMAGE_DIR / f"{Path(name).stem}.png"))
    g = None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _gray_tpl_cache[name] = g
    return g


class LegacyGrayDetector(TreasureStageDetector):
    """同政策旧引擎：全锚点强制灰度（P4c 前生产行为），匹配内核亦走新引擎——
    唯一变量 = colorspace 集合（全灰 vs 按锚点声明）。不改写共享 plan，
    自持帧灰度缓存（detect 入口刷新）。"""

    _frame_gray: np.ndarray | None = None

    def detect(self, frame_rgb, active_rois=None):
        self._frame_gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        return super().detect(frame_rgb, active_rois)

    def _match_score(self, roi_key, tpl_name, frame_rgb, gray_frame, px_roi, colorspace):
        gt = _load_gray(tpl_name)
        if gt is None:
            return None
        from maaracing_assistant.core.template_match import best_match_score
        return best_match_score(self._frame_gray, gt,
                                scales=self.match_scales, roi=px_roi)

DEBUG_ROOT = Path.home() / "AppData" / "Roaming" / "MaaRacingAssistant" / "debug" / "treasure"
SESSIONS = ["20260906_181301", "20260906_203109", "20260906_212501", "20260906_180949"]


def even_sample(items, k):
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def main():
    per_anchor = 30
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--per-anchor" and i + 1 < len(argv):
            per_anchor = int(argv[i + 1])
    if TreasureStageDetector(None).plan is None:
        print("[FAIL] DetectionPlan 未加载")
        return 1
    total = agree = 0
    mismatches = []
    times_new = []
    times_old = []
    for sess in SESSIONS:
        sdir = DEBUG_ROOT / sess
        if not sdir.is_dir():
            print(f"[skip] {sdir}")
            continue
        # 每会话一套全新实例：回合状态机（_last_round）按会话时间序自然演进
        det_new = TreasureStageDetector(None)
        det_old = LegacyGrayDetector(None)
        frames = {}
        for line in (sdir / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if "scores" in rec:
                frames[rec["frame"]] = rec
        by_anchor = defaultdict(list)
        for fno, rec in frames.items():
            ha = rec.get("hit_anchor")
            if ha:
                by_anchor[ha].append(fno)
        miss = [f for f, rec in frames.items() if not rec.get("hit_anchor")]
        sample = set()
        for lst in by_anchor.values():
            sample.update(even_sample(sorted(lst), per_anchor))
        sample.update(even_sample(sorted(miss), 40))
        for fno in sorted(sample):
            raw = sdir / "raw" / f"{fno:04d}_raw.jpg"
            if not raw.exists():
                continue
            bgr = cv2.imread(str(raw))
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            active = set(frames[fno].get("active_used") or ()) or None
            t0 = time.perf_counter()
            res_old = det_old.detect(rgb, active)
            times_old.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            res_new = det_new.detect(rgb, active)
            times_new.append((time.perf_counter() - t0) * 1000)
            total += 1
            if (res_new.hit_anchor, res_new.stage) == (res_old.hit_anchor, res_old.stage):
                agree += 1
            else:
                mismatches.append((sess, fno, res_old.hit_anchor, res_new.hit_anchor,
                                   res_old.stage, res_new.stage))
    print(f"\n同政策直跑 {total} 帧：旧全灰 vs 新按锚点声明——hit/stage 一致 "
          f"{agree}（{agree/max(1,total):.1%}）")
    print(f"耗时/帧：旧灰 均值 {statistics.fmean(times_old):.1f}ms p95 {sorted(times_old)[int(len(times_old)*0.95)]:.1f}ms | "
          f"新引擎 均值 {statistics.fmean(times_new):.1f}ms p95 {sorted(times_new)[int(len(times_new)*0.95)]:.1f}ms")
    if mismatches:
        print("差异明细（sess#frame old_hit→new_hit | old_stage→new_stage）:")
        for m in mismatches[:40]:
            print(f"  {m[0]}#{m[1]}: {m[2]} → {m[3]}  | {m[4]} → {m[5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
