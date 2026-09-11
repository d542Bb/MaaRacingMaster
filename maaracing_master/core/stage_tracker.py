#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段记录器 / 校验器（模块开发模式统一计划 · P1b）。

纯新增，不接入任何运行时代码。本质是轻量「记录 + 校验」工具，**不拥有**任何
transfer policy，也**不维护**转移条件——明确不做什么：不管顾"何时进入某阶段"、
"为什么进入某阶段"、以及"进入时要执行什么领域动作"。

替代 racing/module.py（手写 `STAGE_ORDER.index(start_from)` + if 链）与
treasure/module.py 里手写的断点/阶段记录逻辑，收敛成同一份线性阶段骨架。

设计原则（对齐计划 §四/§七）：
- 取名 StageTracker 而非 StageMachine：避免 "input→transition→state" 的错误预期，
  transition 永远归领域代码。
- `set_stage(name)` 只说「我现在知道进入阶段 N 了」，不说「我替你决定进入 N」。
- `resolve_start_from(start_from)` 只做断点解析，不做任何跳过副作用。

按计划，racing / treasure 迁移（P4/P5)才用本类替换手写逻辑；本轮仅交付类与测试。
"""
from __future__ import annotations

from typing import Iterable, Sequence


class InvalidStageError(ValueError):
    """阶段名不合法（不在 stage order 中）。"""


class StageTracker:
    """线性阶段记录器/校验器（不拥有转移策略）。

    用法（对齐计划 §四 期望契约）：
        tracker = StageTracker(["大厅", "选择", "第1回合", "第2回合"])
        tracker.set_stage("第1回合")            # 仅记录 current_stage
        tracker.current_stage                   # "第1回合"
        tracker.resolve_start_from("第2回合")   # 断言合法 → 返回 skip 索引 3

    order 提供者可以是纯字符串序列，也可以从 ROIConfig（from_roi_config）复用
    stage_order，保持单一事实来源。
    """

    def __init__(self, order: Iterable[str] | None = None):
        self._order: tuple[str, ...] = tuple(order or ())
        self._current: str | None = None
        if not self._order:
            raise ValueError("StageTracker.order 不能为空")

    @classmethod
    def from_roi_config(cls, cfg) -> "StageTracker":
        """从 ROIConfig 的 stage_order 构造（复用统一配置底座）。"""
        return cls(cfg.stage_order)

    # ---------------- 只读访问 ----------------

    @property
    def order(self) -> tuple[str, ...]:
        """有序阶段序列。"""
        return self._order

    @property
    def current_stage(self) -> str | None:
        """当前记录到的阶段名（尚未 set_stage 时为 None）。"""
        return self._current

    @current_stage.setter
    def current_stage(self, name: str | None) -> None:
        """与 set_stage 等价；属性形式便于迁移旧代码的 `self._current_stage`。"""
        self.set_stage(name)

    # ---------------- 记录 ----------------

    def set_stage(self, name: str | None) -> None:
        """校验阶段名合法并记录 current_stage（改名自 treasure.set_stage）。

        - name 为 None：允许（重置为「未开始」），常用于 state 机尚未进入任何阶段。
        - name 不在 order 中：抛 InvalidStageError（启动期就能发现拼写/配置错误）。
        仅记录，不触发任何领域动作。
        """
        if name is not None and name not in self._order:
            raise InvalidStageError(
                f"未知阶段名: {name!r}（合法阶段: {', '.join(self._order)}）"
            )
        self._current = name

    # ---------------- 断点解析 ----------------

    def resolve_start_from(self, start_from: str | None = None) -> int:
        """断点解析：把 start_from 换算为 skip 索引（对齐 racing 的 skip_until 语义）。

        - start_from 为 None → 从头开始，返回 0。
        - start_from 在 order 中 → 返回其索引（= 从该阶段起继续，前面 index 个阶段跳过）。
        - start_from 不在 order 中 → 抛 InvalidStageError（断言小节合法，不静默忽略）。

        只做解析换算，不做「跳过的副作用」——副作用（如 skip_until<=N 时不执行导航）
        由领域代码自行判断，与本类无关。
        """
        if start_from is None:
            return 0
        try:
            return self._order.index(start_from)
        except ValueError:
            raise InvalidStageError(
                f"start_from 断点不合法: {start_from!r}（合法阶段: {', '.join(self._order)}）"
            ) from None

    # ---------------- 工具 ----------------

    def next_stage(self) -> str | None:
        """当前阶段的后一阶段（最后一个阶段后返回 None）。

        纯查询、「按 order 线性推进」的最保守次一阶段，不包含任何转移条件判断。
        领域代码可自主决定是否采用（很多场景转移靠检测而非线性 ++）。
        """
        if self._current is None:
            return None
        try:
            idx = self._order.index(self._current)
        except ValueError:
            return None
        return self._order[idx + 1] if idx + 1 < len(self._order) else None

    def stage_index(self, name: str) -> int:
        """阶段名 → 索引；未知抛 InvalidStageError。"""
        if name not in self._order:
            raise InvalidStageError(
                f"未知阶段名: {name!r}（合法阶段: {', '.join(self._order)}）"
            )
        return self._order.index(name)