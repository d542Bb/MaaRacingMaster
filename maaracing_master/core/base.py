# -*- coding: utf-8 -*-
"""
活动模块框架基础：活动上下文（ActivityContext）与活动模块抽象基类（ActivityModule）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    # 仅类型检查用；运行时注解为字符串不求值（from __future__ import annotations）
    from maaracing_master.core.controller import MaaRacingMasterController as AppController
    from maaracing_master.core.debug import NavigationDebugger
    from maaracing_master.core.capabilities import (
        CaptureCapability,
        DebugRendererCapability,
        GamepadCapability,
        Lifecycle,
    )

_T = TypeVar("_T")


class ActivityContext:
    """活动上下文：为活动模块提供对主控制器能力的安全访问门面。

    模块通过三组 typed capability（capture / gamepad / lifecycle）接触宿主，
    不直接访问 controller.app 私有接口。固有能力 lifecycle 常驻，可选能力为 None。
    """

    def __init__(self, app: "AppController"):
        self.app = app
        # 延迟装配 capability（避免循环 import 与构造顺序耦合）
        self._capture = None
        self._gamepad = None
        self._lifecycle = None
        # 资源所有权：模块经 enter_context 登记的资源（如 renderer），
        # 由生命周期编排层（controller）在模块结束时调用 close() 统一释放。
        self._stack = ExitStack()
        self._closed = False

    # ---------- 资源生命周期（模块登记，编排层关闭） ----------

    def enter_context(self, cm: "AbstractContextManager[_T]") -> _T:
        """登记一个 context manager，其生命周期由本 Context 接管（close() 时释放）。

        只暴露此方法，不开放内部 `_stack` 或任意 `callback()`，保持窄 capability 原则。
        close() 调用权只在生命周期编排层（controller），模块不得主动关闭整个 Context。
        """
        if self._closed:
            raise RuntimeError("ActivityContext 已关闭，禁止再登记资源")
        return self._stack.enter_context(cm)

    def close(self) -> None:
        """关闭 Context：释放所有登记的资源（逆序）。幂等。"""
        if self._closed:
            return
        self._closed = True
        self._stack.close()

    # ---------- capability（仅在首访时装配） ----------

    @property
    def capture(self) -> "CaptureCapability":
        """截图能力（首次访问时装配，恒可用）"""
        if self._capture is None:
            from maaracing_master.core.capabilities import CaptureAdapter
            self._capture = CaptureAdapter(self.app)
        return self._capture

    @property
    def gamepad(self) -> "GamepadCapability":
        """虚拟手柄能力（首次访问时装配，恒可用）"""
        if self._gamepad is None:
            from maaracing_master.core.capabilities import GamepadAdapter
            self._gamepad = GamepadAdapter(self.app)
        return self._gamepad

    @property
    def lifecycle(self) -> "Lifecycle":
        """固有能力：生命周期（所有 Context 保证存在）"""
        if self._lifecycle is None:
            from maaracing_master.core.capabilities import LifecycleAdapter
            self._lifecycle = LifecycleAdapter(self.app)
        return self._lifecycle

    @property
    def debug_renderer(self) -> "DebugRendererCapability":
        """调试渲染器能力（可选）：renderer() 返回租约供 enter_context 接管"""
        from maaracing_master.core.capabilities import DebugRendererAdapter
        return DebugRendererAdapter(self.app.debug)

    @property
    def capabilities(self) -> frozenset[str]:
        """当前可用能力集合（单一来源，随构造结果自动推导）"""
        result = {"lifecycle"}
        if self.capture is not None:
            result.add("capture")
        # gamepad 仅在 vgamepad（ViGEmBus 驱动）可用时暴露；缺失时 REQUIRES 校验会把
        # 依赖它的模块在启动前拦下，而不是运行中途崩。
        if self.app.gamepad_available():
            result.add("gamepad")
        # onnx 能力已随模型插件化移除：YOLO 等模型由各插件自带（plugins/<id>/resources/），
        # 缺失校验走模块类 REQUIRED_ASSETS 声明，
        # 由 registry/sidecar 按插件目录检查，不再是宿主全局能力。
        # debug_renderer 恒可用（debug 实例常驻）
        result.add("debug_renderer")
        return frozenset(result)

    # ---------- 调试（兼容既有，已迁入 debug_renderer capability） ----------

    @property
    def debug(self) -> "NavigationDebugger":
        """调试器实例"""
        return self.app.debug

    @property
    def proj(self) -> Path:
        """项目根目录"""
        return self.app.proj

    @property
    def click_mode(self) -> str:
        """点击方式（real 前台鼠标 / gamepad 后台手柄）"""
        return self.app._click_mode

    @property
    def intent_mode(self) -> bool:
        """意图开关（仅显示意图）：开启后程序只导航到目标、不确认点击，由用户自己按。"""
        return self.app._intent_mode

    @property
    def hwnd(self) -> int:
        """已连接的游戏窗口句柄（未连接为 0）"""
        return self.app._hwnd

    # ---------- 窗口事实（平台层事实经此暴露，插件不直接 import window_utils）----------

    @property
    def window_foreground(self) -> bool:
        """目标窗口是否为当前前台窗口。

        前台鼠标点击的安全前置（程序不主动抢前台）：非前台时调用方应取消本次点击。
        """
        from maaracing_master.core.window_utils import is_foreground

        return bool(is_foreground(self.app._hwnd))

    def verify_frame_client(self, frame_w: int, frame_h: int) -> None:
        """校验「截图帧尺寸 vs 客户区物理尺寸」一致（坐标映射 1:1 的前提），偏差时告警。"""
        from maaracing_master.core.window_utils import verify_frame_client

        verify_frame_client(self.app._hwnd, frame_w, frame_h)

    def check_window_aspect(self, tol: float = 0.05) -> bool:
        """校验客户区宽高比是否约 16:9（模板/ROI 按 720p 归一化的前提）。"""
        from maaracing_master.core.window_utils import check_game_window_aspect

        return bool(check_game_window_aspect(self.app._hwnd, tol))

    def connect(self) -> bool:
        """幂等窗口连接，成功返回 True"""
        return self.app.connect()

    def bind_tasker(self, tasker, resource) -> None:
        """MAA 集成：把 Resource 绑定到 Tasker（内部持帧注入控制器，不对外暴露）。

        模块不得直接获取 controller 高权限对象；此方法是 MAA 深度绑定点的窄入口。
        绑的是 `WgcapController`（读 WGC 中心缓存），**不是** MAA Win32Controller——
        宪法 6 要求帧只从中心缓存来，插件侧一律不得持有同步截图通道。
        WGC 采集器未就绪时抛 ModuleIntegrationError（语义化失败，而非裸断言）。
        """
        from maaracing_master.core.nav_graph import WgcapController

        cap = getattr(self.app, "_wgc_capture", None)
        if cap is None or not cap.is_running:
            raise ModuleIntegrationError("WGC 中心采集器未就绪，无法绑定 Tasker（截图链路故障）")
        tasker.bind(resource, WgcapController(self.capture))


class ModuleDependencyError(RuntimeError):
    """模块依赖缺失：启动时验证 REQUIRES 与 ctx.capabilities 不匹配时抛出"""
    pass


class ModuleIntegrationError(RuntimeError):
    """模块宿主集成失败：如 MAA controller 未连接受绑定等集成边界错误"""
    pass


class ActivityModule(ABC):
    """活动模块抽象基类：每个活动（导航/对局等）实现为独立模块"""

    ID = ""
    NAME = ""
    STAGE_ORDER: list[str] = []
    REQUIRES_GAMEPAD_EXCLUSIVE = False

    # 声明模块需要的可选能力（启动前由 controller 验证）。
    # 固有能力 lifecycle 隐式满足，无需声明。
    # 示例：REQUIRES = frozenset({"capture", "gamepad"})
    REQUIRES: frozenset[str] = frozenset()

    # 声明插件自带的必需资源（相对插件目录的路径，如 "resources/onnx/model.onnx"）。
    # 启动前由 sidecar 逐项检查存在性，缺失时拦截并给出插件内具体路径。
    REQUIRED_ASSETS: tuple[str, ...] = ()

    # 声明模块配置面的**键集合与默认值**（GUI 配置项的唯一真源）。
    # 两个用途：
    #   1. 模块未运行（没有实例）时给 GUI 回填初值——故"读配置"不必构造实例，
    #      模块代码不会因为"被看一眼配置"而执行；
    #   2. profile 落盘与回填的键白名单——**新增可持久化的配置项必须加进这里**，
    #      否则它不会被保存（core 不再持有任何模块的字段名）。
    # 只放"用户可设置"的项；运行实况（进度、计数等）走 get_module_config 的 _state。
    DEFAULT_MODULE_CONFIG: dict = {}

    def __init__(self, ctx: ActivityContext | None):
        # ctx=None 为离线形态（registry.create_module 的公开契约：只读默认配置，
        # 不启动运行时）。运行期编排层总是注入实 ctx；模块内访问 ctx 的属性前
        # 应经运行态守卫（如 self._running）。
        self.ctx = ctx

    @property
    @abstractmethod
    def current_stage(self) -> str | None:
        """当前阶段名，None 表示尚未开始"""

    @abstractmethod
    def start(self, start_from: str | None = None) -> None:
        """启动模块，start_from 为可选起始阶段名"""

    @abstractmethod
    def stop(self) -> None:
        """停止模块"""

    @abstractmethod
    def cleanup(self) -> None:
        """释放模块自身拥有的资源并归还共享资源"""
