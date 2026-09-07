#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 决策重放校验（记录决策 vs 当前 PolicyPlan 重放，逐帧 diff）。

`--trace <trace.jsonl>`：读取运行期落盘的 `event=decision` 行（含
facts_projection 与 policy 输出），用**当前** PolicyPlan 从同一份 facts
重放，逐帧比对（key / payload / side_effects / fatal）。

用途：P1e 收敛后的持续回归——实机 trace 随时可用当前版本策略重放，
验证"历史行为 = 当前策略"，策略改动导致的历史行为变化会在此暴露。

退出码：0 一致；1 有差异；2 数据/环境错误。
纯标准库 + 项目 .venv（Assets 加载需要）；不读个人隐私数据，报告写 stdout。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROJ = Path(__file__).resolve().parents[2]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from maaracing_assistant.core.navkit import (  # noqa: E402
    Assets,
    DecisionFacts,
    PolicyPlan,
    compile_plan,
)

DEFAULT_MODULE = "treasure"


def default_assets_path(module: str) -> Path:
    """v3 资产路径按模块名派生：plugins/<m>/resources/config/<m>_assets.json。"""
    return (
        _PROJ / "maaracing_assistant" / "plugins" / module / "resources" / "config"
        / f"{module}_assets.json"
    )


@dataclass
class Diff:
    frame: int
    stage: str | None
    field: str
    recorded: Any
    replay: Any
    message: str = ""


@dataclass
class ReplayReport:
    total: int = 0
    diffs: list[Diff] = field(default_factory=list)
    fatal_frames: int = 0

    @property
    def ok(self) -> bool:
        return not self.diffs

    def text(self) -> str:
        if self.ok:
            lines = [
                f"[replay] {self.total} 帧重放一致：diff=0 ✓",
                "  decision:       "
                f"{self.total}/{self.total} 帧 key/payload/side_effects/fatal 全等",
            ]
            if self.fatal_frames:
                lines.append(f"  fatal: {self.fatal_frames} 帧当前策略输出终止指令（等价终止路径）")
            return "\n".join(lines)
        lines = [f"[replay] {self.total} 帧中发现 {len(self.diffs)} 处差异："]
        for d in self.diffs[:50]:
            lines.append(
                f"  frame={d.frame} stage={d.stage} {d.field}: "
                f"recorded={d.recorded!r} replay={d.replay!r} {d.message}"
            )
        if len(self.diffs) > 50:
            lines.append(f"  ... 其余 {len(self.diffs) - 50} 处省略")
        return "\n".join(lines)


def build_plan(assets_path: Path, module: str) -> PolicyPlan:
    assets = Assets.load(assets_path, module=module)
    if assets.policies is None:
        raise RuntimeError(f"{assets_path} 缺少 policies 段（决策策略缺失 = 启动失败）")
    return compile_plan(assets.policies, assets.anchors)


def _norm(value: Any) -> Any:
    """trace 序列化会把 tuple 变 list；比较前递归归一化。"""
    if isinstance(value, tuple):
        return [_norm(v) for v in value]
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    return value


def _compare_decision(
    frame: int,
    stage: str | None,
    snap: dict[str, Any],
    d_plan: Any,
    diffs: list[Diff],
) -> None:
    recorded = snap.get("decision") or {}
    recorded_fatal = snap.get("fatal")

    def push(field: str, a: Any, b: Any, message: str = "") -> None:
        if _norm(a) != _norm(b):
            diffs.append(Diff(frame, stage, field, a, b, message))

    push("decision.key", recorded.get("key"), d_plan.key)
    push("decision.payload", recorded.get("payload") or {}, dict(d_plan.payload))
    push("decision.side_effects", recorded.get("side_effects") or [], list(d_plan.side_effects))
    push("decision.fatal", recorded_fatal, d_plan.fatal)


def replay_trace(trace_path: Path, plan: PolicyPlan) -> ReplayReport:
    report = ReplayReport()
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "decision":
                continue
            snap = row.get("decision_snapshot") or {}
            report.total += 1
            facts_proj = snap.get("facts_projection") or {}
            frame = int(row.get("frame", 0))
            stage = facts_proj.get("stage")
            facts = DecisionFacts(values=dict(facts_proj))
            d_plan = plan.decide(facts)
            _compare_decision(frame, stage, snap, d_plan, report.diffs)
            if d_plan.fatal is not None:
                report.fatal_frames += 1
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="P1 决策重放校验（记录 vs 当前 PolicyPlan）")
    parser.add_argument("--trace", type=Path, required=True, help="trace.jsonl 路径")
    parser.add_argument("--module", default=DEFAULT_MODULE,
                        help=f"插件模块 ID（默认 {DEFAULT_MODULE}；决定资产缺省路径与资产归属）")
    parser.add_argument("--assets", type=Path, default=None,
                        help="v3 资产路径（缺省按 --module 派生）")
    args = parser.parse_args()

    assets_path = args.assets if args.assets is not None else default_assets_path(args.module)
    try:
        plan = build_plan(assets_path, args.module)
    except Exception as exc:
        print(f"[replay] 环境/数据错误：{exc}", file=sys.stderr)
        return 2

    if not args.trace.is_file():
        print(f"[replay] trace 文件不存在：{args.trace}", file=sys.stderr)
        return 2

    report = replay_trace(args.trace, plan)
    print(report.text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
