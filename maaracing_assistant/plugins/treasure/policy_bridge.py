# -*- coding: utf-8 -*-
"""v4 policy 闭环节点的动作桥（P2a-Q3b）。

图形态（迁移器产出）：每个 stage dwell 的 next 兜底位挂
`{"name": "<module>.policy_loop", "jump_back": true}`；节点本体
`action: Custom + custom_action: MRA_Policy`。

语义：锚点/链头/通配全部未命中时执行一帧决策段（`module._decision_phase()`：
意图解析 → 点击消费/提交 → 决策契约落盘），
`[JumpBack]` 返回父 dwell 重判——「每帧重判」的图化。

分层（宪法 3）：桥注册在 plugin（持 module 引用，感知业务决策件）；
core 的 v4 runner 只提供通用加载（MRA_Template/MRA_Click），不感知本桥。
"""
from __future__ import annotations

from typing import Any

from maa.custom_action import CustomAction
from maa.resource import Resource

__all__ = ["POLICY_ACTION_NAME", "PolicyBridge"]

POLICY_ACTION_NAME = "MRA_Policy"


class PolicyBridge(CustomAction):
    """`<module>.policy_loop` 节点的动作桥：一次 run = 一帧完整工作。

    全部帧工作（stage 标注/阶段自动化/OCR/决策/点击/调试存盘）
    都在本桥（Tasker 线程）执行——直接委托 `module._tick_once()`，
    单线程无竞态。module 主线程退化为健康守护（poll）与生命周期。
    """

    def __init__(self, module: Any) -> None:
        super().__init__()
        self._module = module

    def run(self, context: Any, argv: Any) -> bool:
        # custom_action_param 的 table 引用由 module 侧持有（决策栈在模块
        # 启动时已编译），桥不做二次解析。
        self._module._tick_once()
        return True

    def register(self, resource: Resource) -> None:
        """注册到 v4 runner 的 Resource（Q4 接线时由 runner 调用）。"""
        resource.register_custom_action(POLICY_ACTION_NAME, self)
