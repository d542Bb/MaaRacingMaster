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
    terminate_process_by_hwnd,
)
from maaracing_master.core.logger import logger
from maaracing_master.core.base import ActivityContext, ModuleDependencyError
from maaracing_master.core.registry import create_module


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
        self._debug_mode = False  # 调试模式开关（由 GUI 控制）
        self._click_mode = "real"  # 点击方式：real(前台=鼠标 SendInput) / gamepad(后台=手柄导航+A键)
        self._intent_mode = False  # 意图开关（仅显示意图）：只导航不确认，由用户自己按
        self._running = False  # 模块运行标志（start_module 生命周期内为 True）
        self._active_module = None  # 当前活动模块实例（生命周期由 start_module 管理）
        self._ctx = None  # ActivityContext 懒创建
        self.stop_event = threading.Event()  # 停止信号（唯一 clear 位置在 start_module）
        self._manual_stop = False  # 本次运行是否被用户手动停止（区分"模块自然跑完"如到限自动停止）
        self._lifecycle_lock = threading.Lock()  # 生命周期互斥锁
        # 紧急停止快捷键开关：开启后同时按下任意 ≥2 个按键 → 立即停止逻辑
        self._emergency_stop_enabled = False
        self._emergency_thread: threading.Thread | None = None
        # 运行结束后自动关闭（GUI「运行结束后」卡片）：仅在流程"正常完成"时生效，
        # 报错退出 / 手动停止（stop_event 置位）不触发。
        self._auto_close_game = False  # 结束后关闭游戏进程
        self._auto_exit_mra = False    # 结束后退出 MaaRM 程序
        self._last_run_natural = False  # 上次 start_module 是否正常跑完（非报错、非手动停止）
        self._mute_game_enabled = False  # 「运行选项」运行时静音游戏（结束恢复 100%）
        self._wgc_capture = None  # WGC 中心采集器（单生产者，全模块读缓存；运行期存在）

    # ---------- 模块生命周期 ----------

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
    def current_stage(self) -> str:
        """返回当前执行阶段名称（委托给活动模块）"""
        return self.active_module.current_stage if self.active_module else ""

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

    def start_module(self, module_id: str, start_from: str | None = None,
                     module_config: dict | None = None):
        """GUI 唯一入口：创建并运行指定活动模块（阻塞，运行于 worker 线程）"""
        with self._lifecycle_lock:
            if self.active_module is not None:
                raise RuntimeError("已有模块在运行")
            self.stop_event.clear()          # ★ 唯一 clear 位置
            self._manual_stop = False
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
                        logger.log(f"[controller] 模块{module_id!r}配置已注入: {sorted(module_config.keys())}")
                    except Exception as exc:  # noqa: BLE001
                        logger.log(f"[controller] 模块{module_id!r}配置注入失败: {exc}", "WARNING")
            self.active_module = module
        self._running = True
        self._last_run_natural = False
        # 「运行选项」运行时静音游戏：每次启动都静音（结束由 finally 统一恢复 100%）。
        # 必须放在这里而非 connect()：connect 幂等（第二次启动直接短路返回），
        # 放 connect 会导致"停止后再开始不再静音"（2026-09-01 实测 bug）。
        # hwnd 取 self._hwnd（二次运行复用上次句柄）或 find_game_hwnd 兜底（首次运行）。
        if self._mute_game_enabled:
            self._apply_game_volume(0.0, "运行开始：静音游戏", verify=True)
        # WGC 中心采集器前置条件：确保 _hwnd 已由 connect() 建立（connect 幂等，
        # 二次运行直接短路返回；首次运行在此完成窗口连接生命周期）。
        # 注意：不能把 connect 塞进 _start_wgc_capture——那是 WGC 生命周期函数，
        # 职责只负责"假定 HWND 已存在后启动采集"；窗口连接归 connect() 所有。
        # conn_ok=False 时跳过 WGC 与模块启动，但仍走 finally 统一清理
        # （active_module/ctx/_running/音量恢复），避免裸 return 残留状态。
        conn_ok = True
        if not self._hwnd:
            if not self.connect():
                logger.log("窗口连接失败，模块终止", "ERROR")
                conn_ok = False
        if conn_ok:
            self._start_wgc_capture()  # WGC 中心采集器（启动失败回退 MAA 截图，不阻断）
        try:
            if conn_ok:
                module.start(start_from)
                # 仅当 start 正常返回（未抛异常）才标记"自然完成"；
                # 手动停止（stop_event 置位）或报错退出不满足，避免误触发自动关闭。
                self._last_run_natural = True
        except Exception as e:
            logger.log(f"模块执行异常: {e}", "ERROR")
        finally:
            try:
                module.cleanup()
            except Exception:
                pass
            # 断开栈帧引用：module 局部变量钉住 模块→点击器→手柄对象 整条链，
            # 不置 None 的话 _destroy_gpad 里的 gc.collect() 回收不到它们。
            module = None
            # 释放模块期间 Context 登记的所有资源（renderer 等）。
            # close() 调用权只在编排层；close 后置空，下次 start_module 新建全新 Context/ExitStack，
            # 保证重复启停不累积 renderer/gamepad ownership。
            if self._ctx is not None:
                try:
                    self._ctx.close()
                except Exception as e:
                    logger.log(f"Context 关闭异常: {e}", "ERROR")
                self._ctx = None
            with self._lifecycle_lock:
                self.active_module = None
            self._running = False
            # 运行结束销毁虚拟手柄（懒创建：下次 start_module 首次用到手柄方式时重建）。
            # 用户要求：GUI 点停止后系统内虚拟手柄应立即消失，不残留干扰手动游戏；
            # 正常完成/异常退出同样销毁，保持"运行期才存在手柄"的生命周期约定。
            # 此时模块主循环已退出（含停止信号中止的手柄导航），无并发使用，销毁安全。
            self._destroy_gpad()
            self._stop_wgc_capture()  # 运行结束停止 WGC 中心采集（资源不常驻，下次运行再启动）
            # 「运行选项」运行时静音游戏：任何停止路径（正常/报错/手动）都恢复 100%
            if self._mute_game_enabled:
                self._apply_game_volume(1.0, "运行结束：恢复游戏音量为 100%")
            # 运行结束后行为（GUI「运行选项」卡片）：仅"正常跑完"生效 ——
            # 手动停止（_manual_stop）或报错退出（natural=False）不触发；
            # 模块自己跑完（含每日循环到限自动停止）算正常完成，开关照常生效。
            # 注意：不能以 stop_event 判定手动停止——模块到限自动停止也会置位它，
            # 改用显式 _manual_stop 标志（stop() 置位，start_module 复位）。
            if self._last_run_natural and not self._manual_stop:
                logger.log("运行结束：任务正常完成，按「运行结束后」选项执行", "INFO")
                self._maybe_auto_shutdown()
            elif self._manual_stop:
                logger.log("运行结束：手动停止，「运行结束后」开关不生效", "INFO")

    def stop(self):
        """停止当前活动模块（幂等）。手动停止：自动关闭游戏/退出 MaaRM 开关不生效"""
        self._manual_stop = True
        self.stop_event.set()
        if self.active_module:
            try:
                self.active_module.stop()
            except Exception as e:
                logger.log(f"停止模块异常: {e}", "ERROR")

    # ---------- 运行结束后自动关闭（GUI「运行结束后」卡片；仅正常完成生效）----------

    @property
    def auto_close_game(self) -> bool:
        """是否在流程正常结束后自动关闭游戏进程（设置开关）"""
        return self._auto_close_game

    @property
    def auto_exit_mra(self) -> bool:
        """是否在流程正常结束后自动退出 MaaRM 程序（设置开关）"""
        return self._auto_exit_mra

    @property
    def last_run_natural(self) -> bool:
        """上次 start_module 是否"正常跑完"（非报错、非手动停止）。供 sidecar 决定是否发退出事件"""
        return self._last_run_natural and not self._manual_stop

    def set_auto_shutdown(self, close_game: bool | None = None, exit_mra: bool | None = None) -> None:
        """设置「运行选项」自动关闭/退出的开关（GUI 设置卡片）。"""
        if close_game is not None:
            self._auto_close_game = bool(close_game)
        if exit_mra is not None:
            self._auto_exit_mra = bool(exit_mra)

    # ---------- 运行时静音游戏（GUI「运行选项」卡片）----------

    @property
    def mute_game_enabled(self) -> bool:
        """是否开启「运行时静音游戏」（设置开关）"""
        return self._mute_game_enabled

    def set_mute_game(self, enabled: bool) -> None:
        """设置「运行时静音游戏」开关。运行期间由 start_module/finally 执行静音与恢复。"""
        self._mute_game_enabled = bool(enabled)

    def _apply_game_volume(self, level: float, label: str, verify: bool = False) -> None:
        """把游戏进程音量设为 level（0~1），作用于其全部音频会话。

        verify=True 时设置后读回确认。失败仅 WARNING，不中断运行。
        """
        try:
            from maaracing_master.core.audio_volume import get_game_volume, set_game_volume
            hwnd = self._hwnd or find_game_hwnd()
            if not hwnd:
                logger.log(f"{label}：未能定位游戏窗口，跳过音量设置", "WARNING")
                return
            n = set_game_volume(hwnd, level)
            if n == 0:
                logger.log(f"{label}：未找到游戏音频会话（游戏可能尚未发声），音量设置未生效", "WARNING")
                return
            if verify:
                vols = get_game_volume(hwnd)
                if vols and not all(abs(v - level) <= 0.01 for v in vols):
                    logger.log(
                        f"{label}：设置后读回音量={[round(v, 2) for v in vols]} ≠ 目标{level:.2f}，"
                        "部分会话未生效", "WARNING")
                    return
            logger.log(f"{label}（游戏音量 {level:.0%}，会话×{n}）", "INFO")
        except Exception as e:  # noqa: BLE001
            logger.log(f"{label}：音量设置异常: {e}", "WARNING")

    def _maybe_auto_shutdown(self) -> None:
        """流程正常结束后执行：按开关关闭游戏进程（退出 MaaRM 由 sidecar 依 last_run_natural 发起，以保证时序）"""
        if not self._auto_close_game:
            return
        hwnd = self._hwnd or find_game_hwnd()
        if not hwnd:
            logger.log("运行结束自动关闭：未能定位游戏窗口，跳过关闭游戏进程", "WARNING")
            return
        try:
            logger.log(f"运行结束自动关闭：正在关闭游戏进程 (hwnd={hwnd})…", "INFO")
            terminate_process_by_hwnd(hwnd)
            logger.log("运行结束自动关闭：游戏进程已关闭", "INFO")
        except Exception as e:  # noqa: BLE001
            logger.log(f"运行结束自动关闭：关闭游戏进程失败: {e}", "WARNING")

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
        触发后冷却 1s，避免按住不松导致 stop() 连发刷日志。
        """
        while self._emergency_stop_enabled:
            try:
                if count_pressed_keys() >= 2:
                    if self._running:
                        logger.log("紧急停止：检测到同时按下 ≥2 个按键，立即停止逻辑", "ERROR")
                        self.stop()
                    # 无论是否运行：冷却 1s，避免按住不松时反复触发
                    time.sleep(1.0)
                    continue
            except Exception:
                pass
            time.sleep(0.05)

    # ---------- 共享能力 ----------

    def set_debug_mode(self, enabled: bool):
        """开启/关闭调试截图模式"""
        self._debug_mode = enabled
        self.debug.enabled = enabled

    def _start_wgc_capture(self) -> None:
        """启动 WGC 中心采集器（幂等）：全模块截图统一读缓存，不再各自 post_screencap。

        启动失败即截图链路故障（宪法 6：帧只从中心缓存来，无 MAA 回退通道）——
        WARNING 后取帧方一律拿到 None，模块按"无帧"处理而非静默截旧帧。
        """
        if not self._hwnd:
            # 与 docstring 契约一致：不静默。正常路径下 start_module 已建立
            # _hwnd 前置条件（P2），此分支仅作异常防御。
            logger.log(
                "WGC 中心采集器启动跳过：游戏窗口尚未连接，截图链路不可用",
                "WARNING",
            )
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
            logger.log("WGC 中心采集器已就绪（全模块截图统一走缓存）", "INFO")
        except Exception as e:  # noqa: BLE001 —— 启动失败即无帧可用，不回退 MAA 截图
            self._wgc_capture = None
            logger.log(f"WGC 中心采集启动失败，截图链路不可用: {e}", "WARNING")

    def _stop_wgc_capture(self) -> None:
        """停止 WGC 中心采集器（幂等）。"""
        cap = self._wgc_capture
        self._wgc_capture = None
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass

    def connect(self) -> bool:
        """幂等窗口连接：仅创建 Win32Controller 并保存句柄，MAA 资源归活动模块创建"""
        if self.controller is not None:
            return True

        hwnd = find_game_hwnd()
        if hwnd == 0:
            logger.log("未找到游戏窗口", "ERROR")
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
            logger.log("连接窗口超时(10s)，请检查游戏是否正常运行/管理员权限", "ERROR")
            self.controller = None
            return False
        if not conn_ok[0]:
            logger.log("连接失败，请检查游戏是否运行/管理员权限", "ERROR")
            self.controller = None
            return False

        self._hwnd = hwnd
        logger.log(f"已连接窗口 (hWnd={hwnd})")

        # 按下开始后的窗口准备：仅「前台(鼠标)」模式切前台（用户明确操作）。
        # 「后台(手柄)」模式保留游戏在后台——后台点击的意义就是游戏留在后台，
        # 前台校验也只在 real 模式生效（见 treasure 模块 _execute_click）。
        if self._click_mode == "real":
            if not activate_window(hwnd):
                logger.log("切换到游戏窗口前台失败（Windows 前台锁定）——逻辑继续运行，点击需游戏在前台", "WARNING")

        # 注：运行时静音不再放这里——connect 幂等，第二次启动会短路返回导致不再静音；
        # 静音统一在 start_module 每次启动时执行（见 start_module）。

        # 统一把所有模块的游戏窗口客户区调整为 720p（截图/模板/ROI 均按 720p 归一化）。
        # 调整失败不阻断：退化到原尺寸并交由后续 16:9 / 屏幕内校验兜底告警。
        resize_game_window_720p(hwnd)

        # 窗口屏幕内校验：窗口完全不可见（被最小化且无法自动还原 / 被拖出屏幕）→ 无法点击 → 报错并终止模块
        if not is_window_on_screen(hwnd):
            logger.log(
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
        """获取虚拟手柄（懒创建 + 保持复用，不销毁重建）"""
        if self._gpad is None:
            self._gpad = vg.VX360Gamepad()
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

    def _destroy_gpad(self):
        """销毁虚拟手柄：从 ViGEmBus 确定性移除设备（游戏内光标/手柄立即断开）。

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
            logger.log("虚拟手柄已销毁（已从 ViGEmBus 移除）", "DEBUG")
        except Exception as e:  # noqa: BLE001 —— 驱动缺失/已移除等场景不阻塞收尾
            logger.log(f"虚拟手柄销毁异常（可能驱动未装或已移除）: {e}", "DEBUG")
        self._gpad = None
        import gc
        gc.collect()  # 兜底：回收模块/点击器链上的手柄对象（free 仍由 __del__ 恰好执行一次）
