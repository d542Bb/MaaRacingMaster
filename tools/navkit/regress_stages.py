#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鉴宝阶段逐帧等价回归（S1 合入硬闸门，§9.1）。

默认输入是用户既有的 C 盘 `%APPDATA%/MaaRacingAssistant/debug/treasure/*/raw/*_raw.jpg`。
本脚本只读这些帧，不写入个人目录；报告写到项目目录 `tools/navkit/out/`。

对照
----
- old：显式 `NAVKIT_SOURCE=v2` 的 TreasureStageDetector（v2 JSON + Python 常量）
- new：默认 v3 TreasureStageDetector（treasure_assets.json → DetectionPlan）

比较键
------
逐帧 `(stage, round_no)` 完全一致；分数明细的同名锚点绝对差 ≤ 1e-4。
退出码：0 一致；1 有差异（阻塞合入）；2 数据/环境错误。

说明：Raw 帧为 1280×720 JPEG，按会话顺序与文件名数字排序。默认每个会话都跑，
不会一次把 1.29GB 全读进内存，逐帧读、立即释放，避免单次请求过大。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

_PROJ = Path(__file__).resolve().parents[2]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

try:
    import cv2
except ImportError as exc:  # 环境错误 → exit 2，不把依赖问题伪装成回归失败
    print(f"[regress] 缺少 cv2：{exc}", file=sys.stderr)
    raise SystemExit(2)

from maaracing_assistant.plugins.treasure.detector import TreasureStageDetector  # noqa: E402
from maaracing_assistant.core.paths import debug_dir  # noqa: E402

DEFAULT_ROOT = debug_dir() / "treasure"
DEFAULT_OUT = _PROJ / "tools" / "navkit" / "out"
SCORE_TOLERANCE = 1e-4


@dataclass(frozen=True)
class FrameDiff:
    session: str
    frame: str
    old_stage: str | None
    new_stage: str | None
    old_round: int | None
    new_round: int | None
    reason: str
    score_diffs: dict[str, float]


class RegressionError(RuntimeError):
    pass


def frame_files(session: Path) -> list[Path]:
    """兼容 raw/0001_raw.jpg 与旧平铺 0001.webp 两种会话布局。"""
    raw = sorted(session.glob("raw/*_raw.jpg"), key=_frame_key)
    if raw:
        return raw
    return sorted(
        [p for p in session.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}],
        key=_frame_key,
    )


def _frame_key(path: Path) -> tuple[int, str]:
    digits = "".join(ch for ch in path.stem if ch.isdigit())
    return (int(digits or 0), path.name)


def iter_sessions(root: Path, selected: str | None) -> Iterable[Path]:
    if not root.is_dir():
        raise RegressionError(f"会话目录不存在：{root}")
    sessions = sorted([p for p in root.iterdir() if p.is_dir()])
    if selected and selected != "all":
        sessions = [p for p in sessions if p.name == selected]
    if not sessions:
        raise RegressionError(f"未找到会话：root={root} selected={selected!r}")
    return sessions


def run_detector(detector: TreasureStageDetector, frame_rgb):
    result = detector.detect(frame_rgb)
    stage, round_no = result
    scores = getattr(result, "scores", {})
    return stage, round_no, scores


def compare_scores(old: dict[str, float], new: dict[str, float]) -> dict[str, float]:
    diffs: dict[str, float] = {}
    for key in sorted(set(old) & set(new)):
        delta = abs(float(old[key]) - float(new[key]))
        if delta > SCORE_TOLERANCE:
            diffs[key] = delta
    return diffs


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p / 100.0
    lo = int(index)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--sessions", default="all", help="all 或单个会话名")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--new-only", action="store_true", help="只跑 v3 计划并输出统计，不做 v2 对照")
    args = parser.parse_args()

    try:
        sessions = list(iter_sessions(args.root, args.sessions))
        old_detector = None
        if not args.new_only:
            os.environ["NAVKIT_SOURCE"] = "v2"
            old_detector = TreasureStageDetector(_PROJ)
        os.environ["NAVKIT_SOURCE"] = "v3"
        new_detector = TreasureStageDetector(_PROJ)
    except Exception as exc:
        print(f"[regress] 初始化失败：{exc}", file=sys.stderr)
        return 2

    # M4/E1：v2 真源退役后 detector 已无 v2 模式（即便置 NAVKIT_SOURCE=v2 仍载入 v3 DetectionPlan）。
    # 此时「old(v2) vs new(v3)」会静默退化成 v3==v3 的假绿。检测到即明说退役并跳过对照，
    # 避免误报"等价"。逐帧 v2↔v3 等价证据已在迁移期（bd69aa3，2077 帧 diff=0）固化。
    if not args.new_only and old_detector is not None and getattr(old_detector, "plan", None) is not None:
        print("[regress] M4/E1：detector 已无 v2 模式，v2↔v3 逐帧对照退役（历史证据见提交 bd69aa3）。"
              "如需 v3 自身回归请用 --new-only。本次跳过对照，退出码 0。")
        return 0

    diffs: list[FrameDiff] = []
    frame_count = 0
    score_values: dict[str, list[float]] = {}
    hit_counts: dict[str, int] = {}
    centers: dict[str, list[tuple[float, float]]] = {}
    errors: list[str] = []

    for session in sessions:
        files = frame_files(session)
        if not files:
            # trace 独立会话（session_*，无 raw 帧）等非帧目录静默跳过，不计为错误
            print(f"[regress] {session.name}: 无 raw 帧，跳过")
            continue
        print(f"[regress] {session.name}: {len(files)} 帧")
        for index, path in enumerate(files, 1):
            # 逐帧读取：不批量载入 1.29GB，处理后立即丢弃
            bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if bgr is None:
                errors.append(f"{session.name}/{path.name}: cv2.imread 失败")
                continue
            frame_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            try:
                new_stage, new_round, new_scores = run_detector(new_detector, frame_rgb)
                if old_detector is not None:
                    old_stage, old_round, old_scores = run_detector(old_detector, frame_rgb)
                else:
                    old_stage = old_round = None
                    old_scores = {}
            except Exception as exc:
                errors.append(f"{session.name}/{path.name}: detector 异常 {exc}")
                continue
            frame_count += 1

            for key, score in new_scores.items():
                score_values.setdefault(key, []).append(float(score))
                if score >= new_detector.match_threshold:
                    hit_counts[key] = hit_counts.get(key, 0) + 1

            if old_detector is None:
                continue
            score_diffs = compare_scores(old_scores, new_scores)
            if (old_stage, old_round) != (new_stage, new_round):
                diffs.append(FrameDiff(
                    session=session.name, frame=path.name,
                    old_stage=old_stage, new_stage=new_stage,
                    old_round=old_round, new_round=new_round,
                    reason="stage_round_mismatch", score_diffs=score_diffs,
                ))
            elif score_diffs:
                diffs.append(FrameDiff(
                    session=session.name, frame=path.name,
                    old_stage=old_stage, new_stage=new_stage,
                    old_round=old_round, new_round=new_round,
                    reason="score_drift_gt_1e-4", score_diffs=score_diffs,
                ))

    report: dict[str, Any] = {
        "sessions": [p.name for p in sessions],
        "frames": frame_count,
        "new_only": args.new_only,
        "score_tolerance": SCORE_TOLERANCE,
        "diff_count": len(diffs),
        "errors": errors,
        "anchors": {
            key: {
                "hit_frames": hit_counts.get(key, 0),
                "p10": percentile(vals, 10),
                "p50": percentile(vals, 50),
                "p90": percentile(vals, 90),
            }
            for key, vals in sorted(score_values.items())
        },
        "diffs": [asdict(diff) for diff in diffs[:500]],
    }
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / "regress_stages.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[regress] 处理帧数：{frame_count}")
    print(f"[regress] 差异数：{len(diffs)}")
    print(f"[regress] 数据/环境错误：{len(errors)}")
    print(f"[regress] 报告：{report_path}")
    if errors:
        return 2
    if diffs:
        first = diffs[0]
        print(
            f"[regress] 首个差异：{first.session}/{first.frame} "
            f"old=({first.old_stage},{first.old_round}) "
            f"new=({first.new_stage},{first.new_round})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
