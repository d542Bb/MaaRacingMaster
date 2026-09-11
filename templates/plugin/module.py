# -*- coding: utf-8 -*-
"""<插件名> 活动模块骨架：演示 ActivityModule 契约的最小完整实现。

生命周期：controller.start_module() → start()（新线程）→ stop()/异常 → cleanup()。
主循环约定：每帧检查 ctx.lifecycle.running，睡眠一律用 ctx.lifecycle.sleep（100ms 级响应停止）。
"""

from __future__ import annotations

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger
from maaracing_master.core.stage_tracker import StageTracker


class SampleModule(ActivityModule):
    """把一个游戏活动（导航 → 循环 → 结算）实现为独立模块的最小骨架。"""

    ID = "sample"
    NAME = "示例模块"

    # 阶段清单：GUI 阶段列表与断点下拉的唯一来源；首阶段通常为「归位/进入活动」
    STAGE_ORDER = [
        "归位",
        "导航",
        "主循环",
        "结算",
    ]

    # 启动前校验的能力集；可用值见 core/capabilities.py：
    #   "capture"（恒可用） / "gamepad"（需 ViGEmBus 驱动） / "debug_renderer"（恒可用）
    # 示例：REQUIRES = frozenset({"gamepad"})
    REQUIRES: frozenset[str] = frozenset()

    # 本模块要求虚拟手柄独占时置 True（启动前会提示断开物理手柄）
    REQUIRES_GAMEPAD_EXCLUSIVE = False

    # 插件自带必需资源（相对插件目录），启动前逐项检查存在性，缺失即拦截
    # 示例：REQUIRED_ASSETS = ("resources/onnx/model.onnx",)
    REQUIRED_ASSETS: tuple[str, ...] = ()

    def __init__(self, ctx: ActivityContext):
        super().__init__(ctx)
        self._stage_tracker = StageTracker(self.STAGE_ORDER)
        self._current_stage: str | None = None

    # ------------------------------------------------------------------
    #  ActivityModule 契约（core/base.py）
    # ------------------------------------------------------------------

    @property
    def current_stage(self) -> str | None:
        """当前阶段名；GUI 运行状态展示与断点定位都读它"""
        return self._current_stage

    def start(self, start_from: str | None = None) -> None:
        """启动模块：连接窗口 → 断点解析 → 主循环"""
        # 1. 连接游戏窗口（幂等；失败即终止，由 GUI 展示错误）
        if not self.ctx.connect():
            logger.log(f"[{self.NAME}] 窗口连接失败，模块终止", "ERROR")
            return

        # 2. 断点解析：start_from 为 None 从头跑；非法值抛 InvalidStageError
        #    （GUI 断点下拉只提供合法值，这里让错误显式暴露）
        if start_from is not None:
            skip_until = self._stage_tracker.resolve_start_from(start_from)
            self._current_stage = self.STAGE_ORDER[skip_until]
            logger.log(f"[{self.NAME}] 从断点开始: 「{self._current_stage}」")
        else:
            self._current_stage = self.STAGE_ORDER[0]

        # 3. 能力取用示例（按需删减）：
        #    frame = self.ctx.capture.screenshot()      # 截图（RGB ndarray，失败返回 None）
        #    ctx_renderer = self.ctx.debug_renderer      # 调试渲染租约
        #    renderer = self.ctx.enter_context(ctx_renderer.renderer())  # 生命周期托管
        #    self.ctx.gamepad.xxx                       # 虚拟手柄（REQUIRES 声明后才可用）

        # 4. 阶段驱动：主循环 + 阶段分发
        logger.log(f"[{self.NAME}] 模块启动")
        while self.ctx.lifecycle.running:
            handler = self._stage_handlers().get(self._current_stage)
            if handler is not None:
                handler()

            if self._advance_stage():
                continue

            # 睡眠必须走 lifecycle（可中断）；不要用 time.sleep
            self.ctx.lifecycle.sleep(0.5)

        logger.log(f"[{self.NAME}] 模块停止")

    def stop(self) -> None:
        """请求停止：置停止信号，主循环在下一个 100ms 检查点退出"""
        self.ctx.lifecycle.request_stop()

    def cleanup(self) -> None:
        """释放模块自有资源；经 enter_context 登记的资源由 Context 统一释放，无需重复归还"""
        self._current_stage = None

    # ------------------------------------------------------------------
    #  内部实现
    # ------------------------------------------------------------------

    def _stage_handlers(self) -> dict[str, callable]:
        """阶段名 → 处理函数；返回 None 的阶段走默认推进"""
        return {
            "归位": self._stage_home,
            "导航": self._stage_navigate,
            "主循环": self._stage_loop,
            "结算": self._stage_settle,
        }

    def _advance_stage(self) -> bool:
        """推进到下一阶段；末阶段返回 False（主循环可直接收尾或重置回首阶段）"""
        assert self._current_stage is not None
        idx = self.STAGE_ORDER.index(self._current_stage)
        if idx + 1 >= len(self.STAGE_ORDER):
            return False
        self._current_stage = self.STAGE_ORDER[idx + 1]
        logger.log(f"[{self.NAME}] 阶段切换 → 「{self._current_stage}」")
        return True

    # 各阶段示例实现：实际逻辑替换为 模板匹配 / OCR / Pipeline 驱动 等
    def _stage_home(self) -> None:
        frame = self.ctx.capture.screenshot()
        if frame is None:
            logger.log(f"[{self.NAME}] 截图失败，重试", "WARNING")
        logger.log(f"[{self.NAME}] 归位：识别当前界面（示例占位）")

    def _stage_navigate(self) -> None:
        logger.log(f"[{self.NAME}] 导航：点进活动入口（示例占位）")

    def _stage_loop(self) -> None:
        logger.log(f"[{self.NAME}] 主循环：活动核心玩法（示例占位）")

    def _stage_settle(self) -> None:
        logger.log(f"[{self.NAME}] 结算：领取奖励并回大厅（示例占位）")
