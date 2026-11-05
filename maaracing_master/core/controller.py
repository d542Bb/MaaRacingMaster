#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主控制器模块：模块生命周期管理（AppController 角色）、窗口连接、截图与手柄共享能力提供。
具体活动流程由各活动插件（plugins/<id>/module.py，经 core/registry 自动发现）自行实现。
"""

from __future__ import annotations

import threading
import time
from typing import Any
from pathlib import Path

from maa.controller import Win32Controller
from maa.define import MaaWin32ScreencapMethodEnum

from maaracing_master.core.debug import NavigationDebugger
from maaracing_master.core.paths import debug_dir
from maaracing_master.core.vgamepad_lazy import gamepad_available as _vgamepad_available
from maaracing_master.core.vgamepad_lazy import vg
from maaracing_master.core.window_utils import (
    activate_window,
    count_pressed_keys,
    ensure_dpi_aware,
    find_game_hwnd,
    is_window_on_screen,
    resize_game_window_720p,
)
from maaracing_master.core.logger import logger
from maaracing_master.core.base import ActivityContext, ModuleDependencyError
from maaracing_master.core.registry import create_module


class _NullGroup:
    """测试桩 logger 无 group() 时的回退句柄：id 恒 None，log 落无组散行，end 空转。"""

    id = None

    def log(self, msg: str, level: str = "INFO") -> None:
        logger.log(msg, level)

    def end(self, outcome: str | None = None) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _run_group(title: str, kind: str = "session"):
    """controller 生命周期组（运行启动/运行收尾）；桩无 group() 时回退 legacy 行。"""
    if getattr(logger, "group", None) is None:
        logger.log(title)
        return _NullGroup()
    return logger.group(title, kind=kind, channel="controller")


def _clog(grp, msg: str, level: str = "INFO") -> None:
    """有组句柄走组，无组（None/未迁移路径）保持散行——调用点不必各自分支。"""
    if grp is not None:
        grp.log(msg, level)
    else:
        logger.log(msg, level)


TEARDOWN_FAILED_MSG = "上次收尾未完成（框架线程未退出），已保留资源；请重启程序后再运行"


class StepSession:
    """内置步骤的执行会话：只独占导航图，宿主资源交还控制器回收。

    所有权分三层（ADR-0018）：
      - **控制器**：窗口连接、WGC 采集器、虚拟手柄设备、ActivityContext——跨任务
        共享，由控制器按自己的生命周期回收，会话只请求启动；
      - **会话**：导航图及其框架句柄——本会话独占，结束即销毁；
      - **租约**：手柄导航期间向 ctx.gamepad 借、跑图结束即归还。归还租约不等于
        可以销毁设备：设备由控制器按「谁创建」决定销毁时机，避免拆掉别的消费者
        还要复用的共享对象。

    失败路径：`start()` 半途失败（连接成功但采集没起来等）不自清，统一由
    `close()` 走同一出口——否则会留下半开资源或漏关。
    """

    def __init__(self, app, needs, grp=None):
        self._app = app
        self.needs = frozenset(needs)
        self._grp = grp
        self._graph = None
        self._touched = False  # 是否动过宿主资源（含半初始化）
        self._closed = False

    def start(self) -> bool:
        """按需建立游戏链路（幂等）；空需求恒成功。失败不自清，交给 close()。"""
        if not self.needs or self._touched:
            return True
        self._touched = True
        if "game_link" in self.needs and not self._app.connect(grp=self._grp):
            _clog(self._grp, "游戏窗口连接失败，无法进入大厅", "ERROR")
            return False
        if "wgc_capture" in self.needs:
            self._app._start_wgc_capture(grp=self._grp)
            cap = getattr(self._app, "_wgc_capture", None)
            if cap is None or not cap.is_running:
                _clog(self._grp, "截图采集未就绪，无法进入大厅", "ERROR")
                return False
        return True

    def graph(self, pipeline, image_dir):
        """建导航图并登记所有权（会话独占）。图与模板目录由步骤自己指定。"""
        from maaracing_master.core.nav_graph import NavGraph

        g = NavGraph(self._app.ctx)
        g.set_log_group(self._grp.id if self._grp is not None else None)
        g.add_plugin(Path(pipeline), Path(image_dir))
        self._graph = g
        return g

    def close(self) -> bool:
        """回收（幂等、不抛）。返回 False = 框架未收摊：此时既不复用也不释放宿主资源。"""
        if self._closed:
            return True
        self._closed = True
        if self._graph is not None:
            graph, self._graph = self._graph, None
            try:
                if not graph.shutdown():
                    return False
            except Exception as exc:  # noqa: BLE001 —— 按未收摊处理，宁可泄漏
                _clog(self._grp, f"导航图收尾异常，按未收摊处理: {exc!r}", "ERROR")
                return False
        if self._touched:
            self._app.release_step_session(grp=self._grp)
        return True


class MaaRacingMasterController:
    """主控制器（AppController）：生命周期编排 + 共享能力提供，活动流程已迁入模块"""
    def __init__(self):
        # DPI awareness：进程级语义，须在创建窗口 / 初始化坐标 API 前显式建立（不继承 shell 配置）
        ensure_dpi_aware()
        self.proj = Path(__file__).resolve().parent.parent.parent
        self.controller = None  # MAA Win32Controller（连接后有效，仅用于连接校验；截图一律走 WGC）
        self._hwnd = 0  # 已连接的游戏窗口句柄（未连接为 0）
        self._gpad = None  # 虚拟手柄，首次使用时创建，不复位不销毁
        self._gp_avail = None  # vgamepad 可用性缓存（None=未探测）
        self.debug = NavigationDebugger(debug_dir())
        self.debug.set_frame_source(self._debug_frame_source)
        self._debug_mode = False  # 调试模式开关（由 GUI 控制）
        self._click_mode = "real"  # 点击方式：real(前台=鼠标 SendInput) / gamepad(后台=手柄导航+A键)
        self._intent_mode = False  # 意图开关（仅显示意图）：只导航不确认，由用户自己按
        self._running = False  # 模块运行标志（start_module 生命周期内为 True）
        self._step_active = False  # 内置步骤执行中（与 _running 并列，互不赋值）
        self._active_module = None  # 当前活动模块实例（生命周期由 start_module 管理）
        self._ctx = None  # ActivityContext 懒创建
        self.stop_event = threading.Event()  # 停止信号（运行开始清；手动停止保留到整轮结束）
        self._manual_stop = False  # 本次运行是否被用户手动停止（区分"模块自然跑完"如到限自动停止）
        self._lifecycle_lock = threading.Lock()  # 生命周期互斥锁
        # 紧急停止快捷键开关：开启后同时按下任意 ≥2 个按键 → 立即停止逻辑
        self._emergency_stop_enabled = False
        self._emergency_thread: threading.Thread | None = None
        # 运行结束后行为不再是开关：关闭游戏 / 退出本程序都是任务列表里的步骤，
        # 由步骤按队列位置执行（见 core/builtin_steps.py）。这里只留一个本轮退出标志。
        self._exit_requested = False   # 步骤请求「本轮结束后退出本程序」
        self._last_run_natural = False  # 上次 start_module 是否正常跑完（非报错、非手动停止）
        self._last_step_outcome = None  # 上次内置步骤的业务结果（completed/skipped/stopped/failed）
        self._gpad_owner: str | None = None  # 虚拟手柄由谁创建（module / step），决定销毁时机
        self._teardown_failed = False  # 上次收尾未完成（框架 worker 未退出）：拒绝下一轮启动
        self._mute_game_enabled = False  # 「运行选项」运行时静音游戏（运行边界获取/恢复原值）
        self._run_muted = False  # 本轮是否已成功静音（结束据此恢复）
        self._mute_original: list[float] | None = None  # 静音前的用户原音量（按会话）
        self._mute_gen = 0  # 运行代际：迟到的静音线程不得跨轮生效
        self._mute_started_gen = -1  # 本代是否已起静音重试线程
        self._mute_lock = threading.Lock()
        self._mute_thread: threading.Thread | None = None
        self._wgc_capture = None  # WGC 中心采集器（单生产者，全模块读缓存；运行期存在）

    # ---------- 模块生命周期 ----------

    def _debug_frame_source(self):
        """调试流取帧口：只读 WGC 中心缓存（契约 I1），采集未运行时返回无帧。"""
        wgc = self._wgc_capture
        if wgc is None or not getattr(wgc, "is_running", False):
            return (None, 0, 0, float("inf"))
        try:
            return wgc.get_latest_rgb()
        except Exception:  # noqa: BLE001 —— 读帧失败按无帧处理
            return (None, 0, 0, float("inf"))

    def _open_debug_scope(self, owner: str, label: str, *, save: str = "on"):
        debug = getattr(self, "debug", None)
        if debug is None:
            return None
        try:
            return debug.open_scope(owner, label, save=save)
        except Exception as exc:  # noqa: BLE001 —— 调试流不得阻断执行
            logger.log(f"[debug] 打开调试作用域失败: {exc!r}", "DEBUG")
            return None

    def _close_debug_scope(self, scope, outcome: str) -> None:
        if scope is None:
            return
        debug = getattr(self, "debug", None)
        if debug is None:
            return
        try:
            debug.close_segment(scope, outcome)
        except Exception as exc:  # noqa: BLE001
            logger.log(f"[debug] 关闭调试作用域失败: {exc!r}", "DEBUG")

    # ---------- 运行边界：全局性质按轮获取与释放 ----------

    def begin_run(self) -> None:
        """队列一轮开始：清运行级请求，并在边界获取全局性质（静音）。

        运行项入口不再各自清手动停止/退出请求——两项之间的停止必须保留到整轮结束。
        """
        self._manual_stop = False
        self.stop_event.clear()
        self._exit_requested = False
        self._last_run_natural = False
        self._last_step_outcome = None
        # 上一轮若因异常没走到恢复，先兜底再开始本轮
        self._restore_run_mute()
        self._start_run_mute()

    def end_run(self) -> None:
        """队列一轮结束：释放运行级全局性质（恢复用户原音量）。"""
        self._restore_run_mute()

    @property
    def active_module(self):
        """当前活动模块实例（None 表示无模块在运行）"""
        return self._active_module

    @active_module.setter
    def active_module(self, module):
        self._active_module = module

    @property
    def ctx(self) -> ActivityContext:
        """活动上下文门面（懒创建，绑定本控制器）"""
        if self._ctx is None:
            self._ctx = ActivityContext(self)
        return self._ctx

    @property
    def hwnd(self) -> int:
        """已连接（或由启动步骤采纳）的游戏窗口句柄，未连接为 0。"""
        return self._hwnd

    @property
    def current_stage(self) -> str:
        """返回当前执行阶段名称（委托给活动模块）"""
        return self.active_module.current_stage if self.active_module else ""

    @property
    def current_module_id(self) -> str:
        """正在运行的模块 id（无活动模块时为空串）"""
        return getattr(self.active_module, "ID", "") if self.active_module else ""

    @property
    def manual_stop_requested(self) -> bool:
        """本次运行是否被用户手动停止（stop() 置位，运行开始复位）。

        运行队列据此区分「用户停止 → 中止整轮」与「模块自然结束 → 跑下一个」；
        不以 stop_event 判定——模块到限自动停止也会置位它（见 stop() 注释）。
        """
        return self._manual_stop

    @property
    def is_running(self) -> bool:
        """是否仍在运行（由 stop_event 控制）"""
        return not self.stop_event.is_set()

    @property
    def click_mode(self) -> str:
        """点击方式（real 前台鼠标 / gamepad 后台手柄），由设置页切换，模块点击统一读取。"""
        return self._click_mode

    def set_click_mode(self, mode: str) -> None:
        """切换点击方式；非法值直接报错（防止静默错点）。"""
        from maaracing_master.core.clicker import CLICK_MODES
        if mode not in CLICK_MODES:
            raise ValueError(f"非法点击方式: {mode!r}，可选 {CLICK_MODES}")
        self._click_mode = mode

    @property
    def intent_mode(self) -> bool:
        """意图开关（仅显示意图）：开启后程序只导航到目标、不确认点击，由用户自己按。"""
        return self._intent_mode

    def set_intent_mode(self, intent: bool) -> None:
        """切换意图开关（仅显示意图）。"""
        self._intent_mode = bool(intent)

    @property
    def module_active(self) -> bool:
        """是否有活动模块正在运行（start_module 生命周期内为 True）"""
        return self._running and self._active_module is not None

    @property
    def execution_active(self) -> bool:
        """是否有执行体在跑（活动模块或内置步骤）。

        `sleep` 是否真的睡、急停是否生效，都只看这一个判据——不借用 `_running`：
        那个字段的语义是「有模块在跑」，`module_active` 与 GUI 的运行显示依赖它。
        """
        return self._running or self._step_active

    @property
    def last_step_outcome(self) -> str | None:
        """上次内置步骤的业务结果：completed / skipped / stopped / failed。"""
        return self._last_step_outcome

    def start_module(self, module_id: str, start_from: str | None = None,
                     module_config: dict | None = None):
        """GUI 唯一入口：创建并运行指定活动模块（阻塞，运行于 worker 线程）

        生命周期归组（契约 §8 终态）：启动行（配置注入/静音/窗口连接/WGC）入
        「运行启动」组，收尾行（资源释放/音量恢复/结束后行为）入「运行收尾」组——
        这批行在 GUI 不再散作裸行。启动段异常路径把「运行启动」组以 failure 收尾；
        「模块执行异常」保持裸 ERROR 行（发生即红，不并入收尾组——收尾组卡建在其后）。
        """
        if self._teardown_failed:
            # 上一轮收尾时框架线程没有退出：句柄还活着，任何新任务都会与它抢同一批
            # C 资源。只报错不尝试恢复——恢复手段是重启程序。
            raise RuntimeError(TEARDOWN_FAILED_MSG)

        g = _run_group("运行启动")
        scope = None
        try:
            with self._lifecycle_lock:
                if self.active_module is not None:
                    raise RuntimeError("已有模块在运行")
                # 上一项到限自动停止会置位 stop_event；手动停止则保留到整轮结束，
                # 由侧车在下一项启动前中止（不再被项入口清掉）。
                if not self._manual_stop:
                    self.stop_event.clear()
                module = create_module(module_id, self.ctx)
                if start_from and start_from not in module.STAGE_ORDER:
                    raise ValueError(f"断点 {start_from} 不属于模块 {module_id} 的阶段")
                # fail-fast：启动前验证**本次配置下**需要的能力是否可用（固有能力 lifecycle 隐式满足）。
                # 问 module.required_capabilities(config) 而非 module.REQUIRES：同一模块在不同
                # 配置下需求可能不同（如 speedrush 的录制模式不操纵车辆、不需要手柄能力）。
                missing = module.required_capabilities(module_config) - self.ctx.capabilities
                if missing:
                    raise ModuleDependencyError(
                        f"{module_id}: missing capabilities: {sorted(missing)}"
                    )
                # 模块配置注入（GUI 设置的循环上限、策略 profile 等）：
                # 模块未定义 set_module_config 时静默跳过（如 racing 暂不支持）。
                if module_config:
                    setter = getattr(module, "set_module_config", None)
                    if callable(setter):
                        try:
                            setter(dict(module_config))
                            g.log(f"[controller] 模块{module_id!r}配置已注入: {sorted(module_config.keys())}")
                        except Exception as exc:  # noqa: BLE001
                            g.log(f"[controller] 模块{module_id!r}配置注入失败: {exc}", "WARNING")
                self.active_module = module
            # 调试作用域：段收尾必须早于 ctx.close（渲染器租约与最后几帧）。
            # 存盘归属由模块声明：save=self 的模块（如极速狂飙）只写索引段行，
            # 帧仍走自己的取证目录，统一流不复制第二份（契约 I7）。
            scope = self._open_debug_scope(
                module_id, module_id, save=getattr(module, "DEBUG_SAVE", "on"))
            self._running = True
            self._last_run_natural = False
            # WGC 中心采集器前置条件：_hwnd 与 MAA 连接由 connect() 建立（幂等：已连接
            # 即短路返回）。这里不按 `self._hwnd` 设守卫：「启动游戏」只采纳句柄、不建
            # 连接，守卫会把真实连接推给模块自己发起的无组那次，窗口连接行即散成裸行。
            # 注意：不能把 connect 塞进 _start_wgc_capture——那是 WGC 生命周期函数，
            # 职责只负责"假定 HWND 已存在后启动采集"；窗口连接归 connect() 所有。
            # conn_ok=False 时跳过 WGC 与模块启动，但仍走 finally 统一清理
            # （active_module/ctx/_running），避免裸 return 残留状态。
            conn_ok = True
            if not self.connect(grp=g):
                g.log("窗口连接失败，模块终止", "ERROR")
                conn_ok = False
            if conn_ok:
                self._start_wgc_capture(grp=g)  # WGC 中心采集器（启动失败回退 MAA 截图，不阻断）
        except BaseException:
            g.end("failure")
            raise
        g.end()
        try:
            if conn_ok:
                module.start(start_from)
                # 仅当 start 正常返回（未抛异常）才标记"自然完成"；
                # 手动停止（stop_event 置位）或报错退出不满足，避免误触发自动关闭。
                self._last_run_natural = True
        except Exception as e:
            logger.log(f"模块执行异常: {e}", "ERROR")
        finally:
            cg = _run_group("运行收尾")
            try:
                try:
                    module.cleanup()
                except Exception:
                    pass
                # 断开栈帧引用：module 局部变量钉住 模块→点击器→手柄对象 整条链，
                # 不置 None 的话 _destroy_gpad 里的 gc.collect() 回收不到它们。
                module = None
                # 先收调试作用域，再释放 Context / 采集（契约 5.1）
                self._close_debug_scope(
                    scope,
                    "stopped" if self._manual_stop
                    else ("completed" if self._last_run_natural else "failed"))
                if self._ctx is not None:
                    try:
                        self._ctx.close()
                    except Exception as e:
                        cg.log(f"Context 关闭异常: {e}", "ERROR")
                    self._ctx = None
                with self._lifecycle_lock:
                    self.active_module = None
                self._running = False
                # 运行结束销毁虚拟手柄（懒创建：下次 start_module 首次用到手柄方式时重建）。
                # 用户要求：GUI 点停止后系统内虚拟手柄应立即消失，不残留干扰手动游戏；
                # 正常完成/异常退出同样销毁，保持"运行期才存在手柄"的生命周期约定。
                # 此时模块主循环已退出（含停止信号中止的手柄导航），无并发使用，销毁安全。
                self._destroy_gpad(grp=cg)
                self._stop_wgc_capture()  # 运行结束停止 WGC 中心采集（资源不常驻，下次运行再启动）
                # 运行结束后行为已改为任务列表里的步骤（关闭游戏 / 关闭后退出本程序）：
                # 这里不再按开关自动关游戏或退出——步骤按队列位置执行，语义在界面上，
                # 不在这里的特判里。
                if self._manual_stop:
                    cg.log("运行结束：手动停止", "INFO")
            except BaseException:
                cg.end("failure")
                raise
            cg.end()

    def run_builtin_step(self, step_id: str, step_config: dict | None = None) -> bool:
        """运行一个内置步骤（启动游戏 / 关闭游戏），阻塞，运行于 worker 线程。

        与 start_module 的区别：不装配模块能力、不参与阶段与断点——步骤只跑一次，
        日志归入以步骤名命名的组。**需要游戏链路的步骤**（启动游戏的「进入大厅」）
        通过 `required_resources()` 按**本次配置**声明需求：资源需求只回答「要不要
        准备资源」，业务结果（completed/skipped/stopped/failed）由步骤自己判定。

        所有权：窗口连接、截图采集、虚拟手柄设备、ActivityContext 归控制器；会话
        （`StepSession`）只独占导航图。收尾未完成时保留资源并标记污染，拒绝下一轮。

        停止信号：项入口只在没有手动停止时清 stop_event——上一项"到限自动停止"也会
        置位它；手动停止保留到整轮结束，由侧车中止后续项。
        """
        from maaracing_master.core.builtin_steps import BUILTIN_STEPS

        cls = BUILTIN_STEPS.get(step_id)
        if cls is None:
            raise KeyError(f"未知的内置步骤: {step_id}")
        if self._teardown_failed:
            raise RuntimeError(TEARDOWN_FAILED_MSG)

        g = _run_group(cls.NAME, kind="step")
        # 上一项到限自动停止会置位 stop_event；手动停止保留到整轮结束
        if not self._manual_stop:
            self.stop_event.clear()
        self._last_run_natural = False
        self._last_step_outcome = "failed"
        cfg = dict(step_config or {})
        step = cls(self, grp=g)
        session = StepSession(self, step.required_resources(cfg), grp=g)
        scope = self._open_debug_scope(step_id, cls.NAME)

        step_ok = False
        teardown_ok = True
        self._step_active = True
        try:
            step_ok = bool(step.run(cfg, self.stop_event, g.log, session))
            self._last_step_outcome = step.last_outcome
        except BaseException:
            g.end("failure")
            raise
        finally:
            try:
                # 先收调试作用域，再释放会话（Context / 手柄 / 采集）
                self._close_debug_scope(scope, self._last_step_outcome or "failed")
                teardown_ok = session.close()
            except Exception as exc:  # noqa: BLE001 —— close 约定不抛，这里只防御
                teardown_ok = False
                g.log(f"会话收尾异常: {exc!r}", "ERROR")
            self._step_active = False

        if not teardown_ok:
            # 框架 worker 未退出：资源已保留，后续启动一律拒绝（宁可泄漏一个进程周期）
            self._teardown_failed = True
            g.log(TEARDOWN_FAILED_MSG, "ERROR")
        ok = step_ok and teardown_ok
        self._last_run_natural = ok
        g.end()
        return ok

    def stop(self):
        """停止当前活动模块（幂等）。手动停止：队列里剩余步骤不再执行"""
        self._manual_stop = True
        self.stop_event.set()
        if self.active_module:
            try:
                self.active_module.stop()
            except Exception as e:
                logger.log(f"停止模块异常: {e}", "ERROR")

    # ---------- 窗口接管与退出请求（内置步骤的宿主侧接口）----------

    @property
    def last_run_natural(self) -> bool:
        """上次 start_module 是否"正常跑完"（非报错、非手动停止）"""
        return self._last_run_natural and not self._manual_stop

    def adopt_window(self, hwnd: int) -> None:
        """采纳一个已定位的游戏窗口：后续 connect() 直接用它，不再按标题搜索。

        启动步骤已经按进程名认准了窗口（启动器窗口与游戏窗口同名，按标题会认错），
        这里把它交给连接层，避免连接时又搜一次并搜到错的那个。
        """
        if hwnd:
            self._hwnd = int(hwnd)

    def forget_window(self) -> None:
        """忘掉当前窗口：游戏被关掉后句柄即失效，下次运行必须重新定位。"""
        self._hwnd = 0
        self.controller = None

    def request_exit(self) -> None:
        """请求本轮结束后退出本程序（由「关闭游戏」步骤的选项发起）。"""
        self._exit_requested = True

    @property
    def exit_requested(self) -> bool:
        """本轮结束后是否退出本程序（sidecar 据此发 auto_exit 事件）。"""
        return self._exit_requested

    # ---------- 运行时静音游戏（GUI「运行选项」卡片）----------

    @property
    def mute_game_enabled(self) -> bool:
        """是否开启「运行时静音游戏」（设置开关）"""
        return self._mute_game_enabled

    def set_mute_game(self, enabled: bool) -> None:
        """设置「运行时静音游戏」开关；运行中切换立即生效（关=恢复原值，开=重新静音）。"""
        enabled = bool(enabled)
        if enabled == self._mute_game_enabled:
            return
        self._mute_game_enabled = enabled
        if not self.execution_active:
            return
        if enabled:
            self._start_run_mute()
        else:
            self._restore_run_mute()

    def _apply_game_volume(self, level: float, label: str, verify: bool = False,
                           grp=None) -> None:
        """把游戏进程音量设为 level（0~1），作用于其全部音频会话。

        verify=True 时设置后读回确认。失败仅 WARNING，不中断运行。
        grp 非空时过程日志归入该组（运行启动/收尾生命周期组）。
        """
        try:
            from maaracing_master.core.audio_volume import get_game_volume, set_game_volume
            hwnd = self._hwnd or find_game_hwnd(
                group_id=grp.id if grp is not None else None, verbose=False)
            if not hwnd:
                _clog(grp, f"{label}：未能定位游戏窗口，跳过音量设置", "WARNING")
                return
            n = set_game_volume(hwnd, level)
            if n == 0:
                _clog(grp, f"{label}：未找到游戏音频会话（游戏可能尚未发声），音量设置未生效", "WARNING")
                return
            if verify:
                vols = get_game_volume(hwnd)
                if vols and not all(abs(v - level) <= 0.01 for v in vols):
                    _clog(grp,
                          f"{label}：设置后读回音量={[round(v, 2) for v in vols]} ≠ 目标{level:.2f}，"
                          "部分会话未生效", "WARNING")
                    return
            _clog(grp, f"{label}（游戏音量 {level:.0%}，会话×{n}）", "INFO")
        except Exception as e:  # noqa: BLE001
            _clog(grp, f"{label}：音量设置异常: {e}", "WARNING")

    def _try_game_volume(self, level: float) -> int:
        """静默尝试设置音量，返回受影响会话数（0 = 暂无可设置的会话）。"""
        try:
            from maaracing_master.core.audio_volume import set_game_volume
            hwnd = self._hwnd or find_game_hwnd(verbose=False)
            if not hwnd:
                return 0
            return int(set_game_volume(hwnd, level))
        except Exception:  # noqa: BLE001
            return 0

    def _try_capture_volume(self) -> list[float] | None:
        """静默读取游戏进程当前各音频会话音量；无窗口/无会话返回 None。"""
        try:
            from maaracing_master.core.audio_volume import get_game_volume
            hwnd = self._hwnd or find_game_hwnd(verbose=False)
            if not hwnd:
                return None
            vols = [float(v) for v in get_game_volume(hwnd)]
            return vols or None
        except Exception:  # noqa: BLE001
            return None

    def _start_run_mute(self, grp=None) -> None:
        """运行开始后静音一次：窗口/音频会话晚到时有界重试。

        原音量在设置 0 之前读取并保留，运行结束按原值恢复（L2）。
        代际号保证运行中关闭开关或进入下一轮后，迟到的线程不会再把游戏静音。
        """
        if not getattr(self, "_mute_game_enabled", False):
            return
        lock = getattr(self, "_mute_lock", None)
        if lock is None:
            return
        with lock:
            if self._run_muted or self._mute_started_gen == self._mute_gen:
                return
            gen = self._mute_gen
            self._mute_started_gen = gen

        def _run():
            # 冷启动可能几分钟才出现窗口/音频会话：重试覆盖整轮上限，
            # 运行结束（代际变化）会立即让线程退出，不会泄漏。
            deadline = time.monotonic() + 300.0
            while time.monotonic() < deadline:
                with lock:
                    if self._mute_gen != gen or not self._mute_game_enabled:
                        return
                vols = self._try_capture_volume()
                if vols:
                    n = self._try_game_volume(0.0)
                    if n:
                        with lock:
                            if self._mute_gen != gen or not self._mute_game_enabled:
                                return
                            self._mute_original = vols
                            self._run_muted = True
                        uniq = sorted({round(v, 2) for v in vols})
                        note = ("" if len(uniq) <= 1
                                else f"，原会话音量不一致 {uniq}，结束按中位数恢复")
                        _clog(grp, f"运行开始：静音游戏（原音量 {vols[0]:.0%}，"
                                   f"会话×{n}{note}）")
                        return
                time.sleep(0.5)
            _clog(grp, "运行开始：未找到游戏音频会话，静音未生效", "WARNING")

        t = threading.Thread(target=_run, daemon=True, name="run-mute")
        self._mute_thread = t
        t.start()

    def _restore_run_mute(self, grp=None) -> None:
        """运行结束（含关闭开关）恢复用户原音量；没静过音则不动。"""
        lock = getattr(self, "_mute_lock", None)
        if lock is None:
            return
        with lock:
            self._mute_gen += 1
            original = self._mute_original
            muted = self._run_muted
            self._run_muted = False
            self._mute_original = None
            self._mute_started_gen = -1
        if not muted:
            return
        if original:
            level = sorted(original)[len(original) // 2]
        else:
            level = 1.0
        self._apply_game_volume(level, f"运行结束：恢复游戏音量（原 {level:.0%}）", grp=grp)

    # ---------- 紧急停止快捷键（GUI 调试选项卡开关控制）----------

    def set_emergency_stop(self, enabled: bool):
        """开/关紧急停止：开启后同时按下键盘任意 ≥2 个按键立即停止逻辑（全局生效）。

        轮询线程为 daemon，随进程退出；关闭开关时线程在下一次循环检测到标志退出。
        """
        self._emergency_stop_enabled = enabled
        if enabled and self._emergency_thread is None:
            self._emergency_thread = threading.Thread(
                target=self._emergency_stop_loop,
                name="emergency-stop",
                daemon=True,
            )
            self._emergency_thread.start()
            logger.log("紧急停止快捷键已启用：同时按下任意 2 个及以上按键将立即停止逻辑", "INFO")
        elif not enabled:
            self._emergency_thread = None  # 线程检测到标志关闭后自行退出

    def _emergency_stop_loop(self):
        """轮询系统按键状态：≥2 键同时按下 → 停止逻辑。

        仅在「逻辑运行中」触发 stop + ERROR 日志；未运行（只开了开关但没点开始）
        时检测到多按键静默跳过，避免「还没启动就满屏 ERROR」。
        开关保持开启（直到用户在 GUI 手动关闭）：触发一次不自动关闭，
        否则"跑完一轮后想再停"会因开关已关而失效（实测教训）。
        **人工驾驶/录制阶段不检测**：模块声明 manual_input_active 时跳过——键盘
        驾驶本身就是多键组合，不能当成急停信号（速跑宏录制即此形态）。
        触发后冷却 1s，避免按住不松导致 stop() 连发刷日志。
        """
        while self._emergency_stop_enabled:
            try:
                if self._manual_input_active():
                    time.sleep(0.05)
                    continue
                if count_pressed_keys() >= 2:
                    if self.execution_active:
                        logger.log("紧急停止：检测到同时按下 ≥2 个按键，立即停止逻辑", "ERROR")
                        self.stop()
                    # 无论是否运行：冷却 1s，避免按住不松时反复触发
                    time.sleep(1.0)
                    continue
            except Exception:
                pass
            time.sleep(0.05)

    def _manual_input_active(self) -> bool:
        """当前活动模块是否处于人工驾驶/录制阶段（急停作用域判定，异常按否处理）。"""
        mod = self._active_module
        fn = getattr(mod, "manual_input_active", None)
        if not callable(fn):
            return False
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 —— 模块判定异常不得打崩急停线程
            return False

    # ---------- 共享能力 ----------

    def set_debug_mode(self, enabled: bool):
        """开启/关闭调试截图模式"""
        self._debug_mode = enabled
        self.debug.enabled = enabled

    def _start_wgc_capture(self, grp=None) -> None:
        """启动 WGC 中心采集器（幂等）：全模块截图统一读缓存，不再各自 post_screencap。

        启动失败即截图链路故障（宪法 6：帧只从中心缓存来，无 MAA 回退通道）——
        WARNING 后取帧方一律拿到 None，模块按"无帧"处理而非静默截旧帧。
        grp 非空时过程日志归入该组（运行启动生命周期组）。
        """
        if not self._hwnd:
            # 与 docstring 契约一致：不静默。正常路径下 start_module 已建立
            # _hwnd 前置条件（P2），此分支仅作异常防御。
            _clog(grp,
                  "WGC 中心采集器启动跳过：游戏窗口尚未连接，截图链路不可用",
                  "WARNING")
            return
        if self._wgc_capture is not None and self._wgc_capture.is_running:
            return
        # 窗口被最小化时 WGC 无帧可采（GraphicsCaptureItem 最小化无渲染内容）：
        # 还原窗口再启动。connect 幂等短路时 resize 的还原不会重复执行，这里兜底。
        try:
            from maaracing_master.core.window_utils import ensure_window_restored

            ensure_window_restored(self._hwnd)
        except Exception:  # noqa: BLE001 —— 还原失败不阻断启动
            pass
        try:
            from maaracing_master.core.wgcap import WgcCapture

            cap = WgcCapture(self._hwnd, max_fps=60)
            cap.start()
            for _ in range(40):  # 等首帧（最多 2s）
                frame, *_ = cap.get_latest()
                if frame is not None:
                    break
                time.sleep(0.05)
            else:
                cap.stop()
                raise RuntimeError("WGC 启动后 2 秒未收到首帧")
            self._wgc_capture = cap
            _clog(grp, "WGC 中心采集器已就绪（全模块截图统一走缓存）", "INFO")
        except Exception as e:  # noqa: BLE001 —— 启动失败即无帧可用，不回退 MAA 截图
            self._wgc_capture = None
            _clog(grp, f"WGC 中心采集启动失败，截图链路不可用: {e}", "WARNING")

    def _stop_wgc_capture(self) -> None:
        """停止 WGC 中心采集器（幂等）。"""
        cap = self._wgc_capture
        self._wgc_capture = None
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass

    def release_step_session(self, grp=None) -> None:
        """回收内置步骤会话占用的宿主资源。

        只回收**会话期间启动/创建**的那部分：Context 关闭并置空（与 start_module
        的收尾同一条规则）、按 owner 决定是否销毁虚拟手柄、停采集器。窗口连接
        （`controller` / `_hwnd`）是跨任务共享状态，不在这里清理——见 ADR-0018。
        """
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception as exc:  # noqa: BLE001
                _clog(grp, f"Context 关闭异常: {exc}", "ERROR")
            self._ctx = None
        # 设备归控制器，但只有当它是本次步骤创建的时候才销毁：
        # 模块先建好的设备可能还要复用于后续模块。
        if self._gpad_owner == "step":
            self._destroy_gpad(grp=grp)
        self._stop_wgc_capture()

    def connect(self, grp=None) -> bool:
        """幂等窗口连接：仅创建 Win32Controller 并保存句柄，MAA 资源归活动模块创建。

        grp 非空时连接过程日志归入该组（运行启动生命周期组）。
        """
        if self.controller is not None:
            return True

        # 启动步骤已按进程名认准窗口时直接用它的句柄：启动器窗口与游戏窗口同名，
        # 再按标题搜一次有认错窗口的风险。
        hwnd = self._hwnd or find_game_hwnd(group_id=grp.id if grp is not None else None)
        if hwnd == 0:
            _clog(grp, "未找到游戏窗口", "ERROR")
            return False

        self.controller = Win32Controller(hWnd=hwnd, screencap_method=MaaWin32ScreencapMethodEnum.FramePool)

        # 连接等待加超时保护：post_connection().wait() 在异常窗口状态下可能无限阻塞，
        # 会导致 worker 卡死在 connect 处、GUI 一直显示「停止中」。
        conn_ok = [False]
        conn_done = threading.Event()

        def _wait_connection():
            try:
                assert self.controller is not None  # 130 行已赋值，连接中不置空
                conn_ok[0] = bool(self.controller.post_connection().wait())
            except Exception:
                conn_ok[0] = False
            finally:
                conn_done.set()

        threading.Thread(target=_wait_connection, daemon=True).start()
        if not conn_done.wait(10.0):
            _clog(grp, "连接窗口超时(10s)，请检查游戏是否正常运行/管理员权限", "ERROR")
            self.controller = None
            return False
        if not conn_ok[0]:
            _clog(grp, "连接失败，请检查游戏是否运行/管理员权限", "ERROR")
            self.controller = None
            return False

        self._hwnd = hwnd
        _clog(grp, f"已连接窗口 (hWnd={hwnd})")

        # 按下开始后的窗口准备：仅「前台(鼠标)」模式切前台（用户明确操作）。
        # 「后台(手柄)」模式保留游戏在后台——后台点击的意义就是游戏留在后台，
        # 前台校验也只在 real 模式生效（见 treasure 模块 _execute_click）。
        if self._click_mode == "real":
            if not activate_window(hwnd):
                _clog(grp, "切换到游戏窗口前台失败（Windows 前台锁定）——逻辑继续运行，点击需游戏在前台", "WARNING")

        # 注：运行时静音不再放这里——connect 幂等，第二次启动会短路返回导致不再静音；
        # 静音统一在 start_module 每次启动时执行（见 start_module）。

        # 统一把所有模块的游戏窗口客户区调整为 720p（截图/模板/ROI 均按 720p 归一化）。
        # 调整失败不阻断：退化到原尺寸并交由后续 16:9 / 屏幕内校验兜底告警。
        resize_game_window_720p(hwnd, group_id=grp.id if grp is not None else None)

        # 窗口屏幕内校验：窗口完全不可见（被最小化且无法自动还原 / 被拖出屏幕）→ 无法点击 → 报错并终止模块
        if not is_window_on_screen(hwnd):
            _clog(grp,
                  "游戏窗口不在任何屏幕的可视范围内（被最小化且无法自动还原，或被拖出屏幕），无法点击。"
                  "模块已终止，请将窗口还原并移回屏幕后重新开始", "ERROR",
                  )
            self.controller = None
            self._hwnd = 0
            return False

        return True

    # ---------- 手柄管理 ----------

    def gamepad_available(self) -> bool:
        """vgamepad 是否可用（ViGEmBus 驱动是否就绪）。结果缓存，避免重复探测。"""
        if self._gp_avail is None:
            try:
                self._gp_avail = bool(_vgamepad_available())
            except Exception:  # noqa: BLE001
                self._gp_avail = False
        return self._gp_avail

    def _get_gpad(self) -> "Any":  # vg 为懒加载代理（vgamepad_lazy），类型经 Any 传递
        """获取虚拟手柄（懒创建 + 保持复用，不销毁重建）。

        创建时记下消费者类型：设备归控制器所有，但**销毁**只在「谁创建谁负责」
        的范围内发生——步骤用完不会拆掉模块已经建好、后续还要复用的设备。
        """
        if self._gpad is None:
            self._gpad = vg.VX360Gamepad()
            self._gpad_owner = "step" if self._step_active else "module"
            self._gpad.reset()
            self._gpad.update()
            time.sleep(0.2)
            logger.log("虚拟手柄已创建", "DEBUG")
        return self._gpad

    def _reset_gpad(self):
        """重置手柄：摇杆归零 + 按钮释放，但不销毁"""
        if self._gpad is not None:
            try:
                self._gpad.reset()
                self._gpad.update()
            except Exception:
                pass

    def _destroy_gpad(self, grp=None):
        """销毁虚拟手柄：从 ViGEmBus 确定性移除设备（游戏内光标/手柄立即断开）。

        grp 非空时过程日志归入该组（运行收尾生命周期组）。
        vgamepad 没有公开的销毁 API，设备移除只发生在对象析构 __del__
        （vigem_target_remove + free）——但析构时机不可控：模块
        Clicker→GamepadClicker 循环引用、以及 start_module 栈帧对 module 局部
        变量的引用，都会让 gc.collect() 回收不掉（实测"停止后游戏内光标仍在，
        关程序窗口才消失"）。故这里显式 ctypes 调 vigem_target_remove 把设备
        从总线拔掉（确定性、即时）；对象内存释放仍留给后续 GC 的 __del__
        （先 remove 后 free，无二次释放问题）。
        """
        if self._gpad is None:
            return
        try:
            self._gpad.reset()
            self._gpad.update()
        except Exception:
            pass
        try:
            # vgamepad 无公开销毁 API，只能摸 VGamepad 的内部句柄（_busp/_devicep）
            from vgamepad.win import vigem_client as _vcli
            _vcli.vigem_target_remove(self._gpad._busp, self._gpad._devicep)
            _clog(grp, "虚拟手柄已销毁（已从 ViGEmBus 移除）", "DEBUG")
        except Exception as e:  # noqa: BLE001 —— 驱动缺失/已移除等场景不阻塞收尾
            _clog(grp, f"虚拟手柄销毁异常（可能驱动未装或已移除）: {e}", "DEBUG")
        self._gpad = None
        self._gpad_owner = None
        import gc
        # 回收窗口的边界日志：这一带的崩溃只能靠日志断点定位（收尾无其它输出），
        # 有头有尾才能判断崩溃落在回收内还是回收之后。
        _clog(grp, "虚拟手柄对象回收开始", "DEBUG")
        gc.collect()  # 兜底：回收模块/点击器链上的手柄对象（free 仍由 __del__ 恰好执行一次）
        _clog(grp, "虚拟手柄对象回收完成", "DEBUG")
