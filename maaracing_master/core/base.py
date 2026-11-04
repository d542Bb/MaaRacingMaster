# -*- coding: utf-8 -*-
"""
活动模块框架基础：活动上下文（ActivityContext）与活动模块抽象基类（ActivityModule）
"""

from __future__ import annotations

import inspect
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

    # 声明插件自带的必需资源（相对插件目录的路径，如 "resources/models/model.onnx"）。
    # 启动前由 sidecar 逐项检查存在性，缺失时拦截并给出插件内具体路径。
    REQUIRED_ASSETS: tuple[str, ...] = ()

    # 声明模块封面的文件名（相对插件 resources/，如 "cover.jpg"）。
    # 只是「我有一张封面」的声明，不含磁盘位置：定位与 URL 解析在 core/covers.py，
    # 前端只消费解析结果。未声明 = 用通用默认封面（装饰件，缺失不阻断模块运行）。
    COVER: str = ""

    # 声明模块是否有「数据看板」（GUI 数据页看板中心的候选资格）。
    # HAS_DASHBOARD / DASHBOARD_CONTRACT（版本 > 0）/ 覆盖 read_today_stats 任一成立
    # 即进入候选集。新模块用 DASHBOARD_CONTRACT + read_dashboard；覆盖
    # read_today_stats 的旧模块由 core 包成 today_stats 兼容块（当前为冻结的鉴宝）。
    HAS_DASHBOARD: bool = False

    # 看板内容契约版本（见 core/dashboard_contract.py）。
    # None = 不提供看板；0 = 已登记能力位但尚未实现（不进入候选集）；
    # 正整数 = 实现版本，read_dashboard() 必须返回同版本的载荷。
    DASHBOARD_CONTRACT: int | None = None

    # 声明模块配置面的**键集合与默认值**（GUI 配置项的唯一真源）。
    # 两个用途：
    #   1. 模块未运行（没有实例）时给 GUI 回填初值——故"读配置"不必构造实例，
    #      模块代码不会因为"被看一眼配置"而执行；
    #   2. profile 落盘与回填的键白名单——**新增可持久化的配置项必须加进这里**，
    #      否则它不会被保存（core 不再持有任何模块的字段名）。
    # 只放"用户可设置"的项；运行实况（进度、计数等）走 get_module_config 的 _state。
    DEFAULT_MODULE_CONFIG: dict = {}

    # 配置表单契约（细则见 core/config_ui_contract.py）：模块声明「面板有哪些字段、
    # 用什么控件」，宿主通用渲染，core 不认识任何模块的字段语义。
    #   CONFIG_UI_CONTRACT：契约版本；不声明 = 无面板（默认无）
    #   CONFIG_UI         ：{"groups": [{"title"?, "fields": [...]}]}
    # 字段键必须在本模块的 DEFAULT_MODULE_CONFIG 里；默认值与动态候选分别由
    # DEFAULT_MODULE_CONFIG / config_choices() 提供，不在表单声明里重复。
    CONFIG_UI_CONTRACT: int | None = None
    CONFIG_UI: dict | None = None

    # 性能面板契约（细则见 core/perf_contract.py）：模块声明「自己会报哪几行」，
    # core 只认格子（kind / 阈值 / 分类），不认识业务。三个都是可选的——
    # 不声明就只显示资源面（整机 / 本程序），不报错。
    #   PERF_CONTRACT：声明契约版本，与 core 的 PERF_CONTRACT_VERSION 不等 → 领域面不显示
    #   PERF_ROWS    ：声明行（key / label / kind / stat / category / 阈值 …）
    #   THREAD_LABELS：线程名 → 中文别名；线程本身由 core 嗅探，模块只贴标签
    PERF_CONTRACT: int | None = None
    PERF_ROWS: tuple[dict, ...] = ()
    THREAD_LABELS: dict[str, str] = {}

    def __init__(self, ctx: ActivityContext | None):
        # ctx=None 为离线形态（registry.create_module 的公开契约：只读默认配置，
        # 不启动运行时）。运行期编排层总是注入实 ctx；模块内访问 ctx 的属性前
        # 应经运行态守卫（如 self._running）。
        self.ctx = ctx

    # ---------- 启动约束（可按本次配置求值）----------
    #
    # 为什么要有这两个查询、而不是直接读上面两个类属性：**同一个模块在不同配置下
    # 需要的启动条件可能不同**。典型：speedrush 的演示录制模式不操纵车辆，既不需要
    # 虚拟手柄能力（无 ViGEmBus 驱动也该能跑），也不该要求断开物理手柄——录制恰恰
    # 要用物理手柄驾驶。类属性表达不了"按配置变化"，故把判定收在这两个可覆盖的
    # 查询里；调用方（controller / sidecar）一律问它们，不直接读类属性。

    @classmethod
    def required_capabilities(cls, config: dict | None = None) -> frozenset[str]:
        """本次启动（按这份配置）需要的能力；默认即 ``REQUIRES`` 声明。

        ``config`` 是将要注入模块的那份配置（可能为空）。覆盖时请基于 ``cls.REQUIRES``
        增删，别另抄一份清单——类属性仍是"模块能力的完整声明"，这里只是本次的子集。
        """
        return cls.REQUIRES

    @classmethod
    def requires_exclusive_gamepad(cls, config: dict | None = None) -> bool:
        """本次启动是否独占手柄（即：必须断开所有物理手柄）；默认取类属性声明。"""
        return cls.REQUIRES_GAMEPAD_EXCLUSIVE

    # ---------- 人工接管期声明（紧急停止的作用域，可选）----------

    def manual_input_active(self) -> bool:
        """当前是否处于「玩家自己操作、程序不接管输入」的阶段；默认 False。

        紧急停止（同时按 ≥2 键即停）只对程序操作期生效：人工驾驶/录制阶段按键
        是正常操作，不能当成急停信号。模块覆盖本方法返回实况即可（例如速跑模块
        在录制驾驶阶段返回 True）。
        """
        return False

    # ---------- GUI 配置项可选值（可选，模块自述）----------

    @classmethod
    def config_choices(cls) -> dict[str, list[dict]]:
        """为下拉型配置项提供可选值；默认空。只读静态枚举，不创建实例。

        形状：``{配置键: [{"value": ..., "label": ...}, ...]}``。core 原样并入
        配置视图的 ``_choices`` 供 GUI 展示；取值合法性仍由模块在 set/start 时
        自行校验，core 不解释这些值。
        """
        return {}

    @classmethod
    def normalize_config(cls, config: dict) -> dict:
        """把旧版配置键折算成当前配置面、施加自身不变量；默认原样返回。

        core 在读回存档与接收 GUI 写入前各调用一次，让"改键名/合并互斥键/档位
        约束"这类配置面演进留在模块内，核心不必认识任何模块的旧字段。
        """
        return config

    @classmethod
    def config_summary(cls, config: dict) -> list[str]:
        """任务卡摘要文案（可选）；默认空。只读、纯函数，不创建实例。

        入参是宿主投影后的有效配置：默认值已合并、旧键已折算、只含
        ``DEFAULT_MODULE_CONFIG`` 里的键，不含 ``_choices`` / ``_state`` 等元数据。
        返回字符串列表；宿主统一做类型与长度校验。
        """
        return []

    def config_status(self) -> dict | None:
        """配置面板下的运行状态行（可选）；默认 None。

        仅运行中的实例会被调用；形状 ``{"text": str, "level": ""|"ok"|"warn"|"error"}``。
        异常由宿主隔离，不影响配置读取。
        """
        return None

    # ---------- GUI 数据看板内容（可选，模块自述）----------
    #
    # 新模块实现 read_dashboard()，返回 core/dashboard_contract.py 定义的 block 载荷；
    # 旧模块的 read_today_stats 会被 core 包成 today_stats 兼容块（鉴宝冻结桥）。
    # 看板数据不得依赖模块实例：读持久化数据或既有 RPC。

    @classmethod
    def read_dashboard(cls) -> dict | None:
        """返回版本化看板载荷；默认 None = 本模块不提供看板内容。

        载荷形状与 block 白名单见 core/dashboard_contract.py。"""
        return None

    # ---------- GUI 今日看板数据（旧模块兼容）----------
    #
    # 覆盖本方法即视为旧模块声明了看板资格；core 会把返回值包成 today_stats 兼容块。
    # 新模块请用 read_dashboard / DASHBOARD_CONTRACT。
    # 看板读什么表、哪些列、日界语义全部是模块业务（与 DEFAULT_MODULE_CONFIG 的
    # "配置面由模块自述"同理）——core 只持有通用路由，不出现任何模块的 schema 字段。

    @classmethod
    def read_today_stats(cls) -> dict | None:
        """读取本模块的今日统计；默认 None = 本模块无今日统计。

        覆盖后自动成为看板候选（旧模块兼容桥）。
        返回 {"bucket": 日界字符串, "summary": 汇总行或 None, "games": 明细列表}；
        库不存在/读取失败由实现自行兜底为空看板，不抛错（GUI 显示空态）。"""
        return None

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


def overrides_classmethod(cls: type, name: str) -> bool:
    """模块类是否覆盖了 ActivityModule 的某个 classmethod（静态比较，不触发执行）。"""
    base = inspect.getattr_static(ActivityModule, name)
    return inspect.getattr_static(cls, name) is not base


def declares_dashboard(cls: type) -> bool:
    """模块是否进入数据页看板候选集。

    ``DASHBOARD_CONTRACT = 0`` 是「已登记、尚未实现」的占位，不参与候选；
    其余非零版本与旧资格位（``HAS_DASHBOARD`` / 覆盖 ``read_today_stats``）
    任一成立即可。资格判定只在这里定义一次，元信息与路由都走它。
    """
    contract = getattr(cls, "DASHBOARD_CONTRACT", None)
    if contract == 0:
        return False
    return bool(
        getattr(cls, "HAS_DASHBOARD", False)
        or contract is not None
        or overrides_classmethod(cls, "read_today_stats")
    )
