# -*- coding: utf-8 -*-
"""v4 policy 闭环节点的动作桥（P2a-Q3b）+ 决策帧自节流闸门。

图形态（迁移器产出）：每个 stage dwell 的 next 兜底位挂
`{"name": "<module>.policy_loop", "jump_back": true}`；节点本体
`action: Custom + custom_action: MaaRM_Policy`。

语义：锚点/链头/通配全部未命中时执行一帧决策段（`module._decision_phase()`：
意图解析 → 点击消费/提交 → 决策契约落盘），
`[JumpBack]` 返回父 dwell 重判——「每帧重判」的图化。

自节流为什么住在桥里（5.12.3 实测，见 tools/experiments/v4-frame-pacing/）：
`rate_limit` 的作用域只是「节点自己等后继命中的那段轮询」；一旦下一跳命中挂
`jump_back` 的兜底节点，父 dwell 的 `rate_limit` 与 `pre_delay`/`post_delay`
**全部旁路**，相邻决策帧间隔 = 兜底节点动作自身耗时 + ≈3.5ms 框架开销。
于是决策段变轻（例如某阶段整轮 OCR 被跳过）时，回弹闭环会以每秒数百次自旋，
图上调参拦不住，只能在这里按单调时间戳补足下限。

分层（宪法 3）：桥注册在 plugin（持 module 引用，感知业务决策件）；
core 的 v4 runner 只提供通用加载（MaaRM_Template/MaaRM_Click），不感知本桥。
本闸门暂留桥内——目前只有鉴宝一个决策通路；第二个模块接入时再上移 core
（单实现先抽公共层 = 内容必然全是当前实现的私货）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from maa.custom_action import CustomAction
from maa.resource import Resource

__all__ = ["POLICY_ACTION_NAME", "MIN_FRAME_INTERVAL_MS", "PolicyBridge"]

POLICY_ACTION_NAME = "MaaRM_Policy"

# 相邻决策帧的最小间隔下限（毫秒）。取 100ms：真机决策段自身耗时约 125ms，
# 因此今天不会触发——本常量是「变轻后不自旋」的闸门，不是节律调参旋钮。
# 注意它不承担 v3 的「300ms/帧」口径：凡计时语义一律时间口径（P2b 定案）。
MIN_FRAME_INTERVAL_MS = 100.0

_SLEEP_GRANULARITY_S = 0.02  # 分片睡眠粒度：停止信号最多 20ms 内返回，不把 Tasker 睡死


class PolicyBridge(CustomAction):
    """`<module>.policy_loop` 节点的动作桥：一次 run = 一帧完整工作。

    全部帧工作（stage 标注/阶段自动化/OCR/决策/点击/调试存盘）
    都在本桥（Tasker 线程）执行——直接委托 `module._tick_once()`，
    单线程无竞态。module 主线程退化为健康守护（poll）与生命周期。
    """

    def __init__(self, module: Any, *,
                 min_interval_ms: float = MIN_FRAME_INTERVAL_MS,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        super().__init__()
        self._module = module
        self._min_interval_s = max(0.0, float(min_interval_ms) / 1000.0)
        self._clock = clock
        self._sleeper = sleeper
        self._last_run_at: float | None = None

    def run(self, context: Any, argv: Any) -> bool:
        # `custom_action_param.table`（treasure.policy.json#policy）的引用由
        # module 侧持有：决策栈在 start() 编译（policy 段缺失 = 启动失败，P1e），
        # 值由 test_navkit_truth + check_truth 双向锁——桥不做二次解析，
        # 只前置断言编译事实（未编译 = 装配违序，尽早暴露）。
        assert getattr(self._module, "_policy_plan", None) is not None, \
            "MaaRM_Policy 前置未满足：决策栈未编译"
        now = self._clock()
        wait_s = self._next_wait_s(now)
        if wait_s > 0.0:
            self._sleep(wait_s)
        self._last_run_at = self._clock()
        self._module._tick_once()
        return True

    def _next_wait_s(self, now: float) -> float:
        """纯函数：给定当前时刻，返回为补足最小间隔还需等待的秒数（0 = 不等待）。

        间隔按「相邻两帧的起始时刻」计，故决策段自身耗时已计入——工作够重时
        本闸门恒为 0，不额外增加延迟。
        """
        if self._min_interval_s <= 0.0 or self._last_run_at is None:
            return 0.0
        return max(0.0, self._last_run_at + self._min_interval_s - now)

    def _sleep(self, seconds: float) -> None:
        """分片可中断睡眠：宿主发出停止信号时立即返回，不留 20ms 以上的尾延迟。"""
        deadline = self._clock() + seconds
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0.0 or not self._host_running():
                return
            self._sleeper(min(_SLEEP_GRANULARITY_S, remaining))

    def _host_running(self) -> bool:
        """宿主是否仍在运行（无 ctx 时按运行处理，保持离线构造可用）。"""
        ctx = getattr(self._module, "ctx", None)
        lifecycle = getattr(ctx, "lifecycle", None) if ctx is not None else None
        return bool(getattr(lifecycle, "running", True))

    def register(self, resource: Resource) -> None:
        """注册到 v4 runner 的 Resource（Q4 接线时由 runner 调用）。"""
        resource.register_custom_action(POLICY_ACTION_NAME, self)
