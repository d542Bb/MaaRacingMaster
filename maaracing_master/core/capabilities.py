# -*- coding: utf-8 -*-
"""
类型化能力接口（Protocol）与最薄 adapter。

一期目标：模块只通过稳定 typed capability 接触宿主，不穿透 controller/app 私有接口。
设计要点（见 docs/ARCHITECTURE_MODULE_SEPARATION.md §3）：
- ownership API 一期就朝二期兼容设计：句柄/租约自带对称 release，二期换 ExitStack 时
  调用方只把 `acquire()` 换成 `enter_context(acquire())`，业务逻辑零改动。
- Protocol 由业务实际调用反推，只暴露当前真正使用的方法，防止"大接口"重新泄漏宿主。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import vgamepad as vg

# 按钮语义常量：模块只看到 BUTTON_A/B，不接触 vg 底层枚举。
# 采用 PEP 562 模块级 __getattr__ **惰性**求值：import 本模块绝不触发 vgamepad，
# 只有真正访问 BUTTON_A/B 才底层导入——避免净机（无 ViGEmBus 驱动）启动即崩
# （capabilities 被 sidecar→controller 链路 import，不能在模块级立即访问 XUSB_BUTTON）。
_BUTTON_CACHE: dict[str, object] = {}


def __getattr__(name: str):
    if name in ("BUTTON_A", "BUTTON_B"):
        if name not in _BUTTON_CACHE:
            from maaracing_master.core.vgamepad_lazy import vg
            enum = vg.XUSB_BUTTON.XUSB_GAMEPAD_A if name == "BUTTON_A" else vg.XUSB_BUTTON.XUSB_GAMEPAD_B
            _BUTTON_CACHE[name] = enum
        return _BUTTON_CACHE[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ==================== Gamepad ====================

class Gamepad(Protocol):
    """虚拟手柄操作面：模块只依赖这一组方法，不接触具体实现/底层对象。"""

    def press_button(self, button) -> None: ...
    def release_button(self, button) -> None: ...
    def left_joystick(self, x_value: int = 0, y_value: int = 0) -> None: ...
    def right_joystick(self, x_value: int = 0, y_value: int = 0) -> None: ...
    def left_trigger(self, value: int = 0) -> None: ...
    def right_trigger(self, value: int = 0) -> None: ...
    def update(self) -> None: ...


class GamepadCapability(Protocol):
    """虚拟手柄能力：以租约（lease）方式取得可操作的手柄会话。

    - `acquire()` 返回 context manager（租约）。进入时获取/复用虚拟手柄，
      退出时**归零归还**（松开按钮 + 摇杆归中），实例仍可复用。
    - `reset_device()` 显式断开底层虚拟设备，下次 `acquire()` 时重新创建。
      它描述的是"销毁/重建设备"这一实际控制能力（游戏光标回左上角只是当前
      游戏对该行为的响应，不是 API 承诺）。**活跃租约存在时调用必须抛错**，
      绝不静默销毁正在借用中的设备。
    """

    def acquire(self) -> AbstractContextManager[Gamepad]: ...

    def reset_device(self) -> None: ...


class GamepadLease:
    """GamepadCapability.acquire() 返回的租约：进入获取、退出归还（归零复用）。"""

    def __init__(self, cap: "GamepadAdapter"):
        self._cap = cap
        self._pad: Gamepad | None = None

    def __enter__(self) -> Gamepad:
        self._pad = self._cap._enter()
        return self._pad

    def __exit__(self, *exc) -> None:
        if self._pad is not None:
            self._cap._exit()
            self._pad = None
        return None


class VGamepadAdapter:
    """把 vg.VX360Gamepad 包装成 Gamepad 薄接口（只转发热用方法）。"""

    def __init__(self, pad: "vg.VX360Gamepad"):
        self._pad = pad

    def press_button(self, button) -> None:
        self._pad.press_button(button)

    def release_button(self, button) -> None:
        self._pad.release_button(button)

    def left_joystick(self, x_value: int = 0, y_value: int = 0) -> None:
        self._pad.left_joystick(x_value=x_value, y_value=y_value)

    def right_joystick(self, x_value: int = 0, y_value: int = 0) -> None:
        self._pad.right_joystick(x_value=x_value, y_value=y_value)

    def left_trigger(self, value: int = 0) -> None:
        self._pad.left_trigger(value=value)

    def right_trigger(self, value: int = 0) -> None:
        self._pad.right_trigger(value=value)

    def update(self) -> None:
        self._pad.update()


# ==================== CaptureCapability ====================

class CaptureCapability(Protocol):
    """截图能力：返回 RGB ndarray，失败返回 None。"""

    def screenshot(self) -> np.ndarray | None: ...


# ==================== Lifecycle ====================

class Lifecycle(Protocol):
    """固有能力：所有 Context 都保证存在的基础生命周期环境（不进 REQUIRES）。"""

    @property
    def running(self) -> bool: ...

    def request_stop(self) -> None: ...

    def sleep(self, seconds: float) -> bool: ...


# ==================== Adapters（最薄包装，朝向 controller 私有接口） ====================

class CaptureAdapter:
    """把 controller 的截图能力包装成 CaptureCapability。

    screenshot() **只读 WGC 中心缓存**（宪法 6：帧只从中心缓存来，引擎永不自截帧）。
    缓存缺席/未启动/读帧异常一律返回 None，**不回退 MAA 同步截图**——回退会造成
    双时间线（消费者各自持有不同时刻的帧，且回退路径在调用线程内阻塞等截图）。
    所有消费者（v4 决策段 / 导航线程 / 模板装载 / OCR）经此接口取帧。
    返回 RGB ndarray（WGC 侧为标准 16:9 720p 帧）；返回 None 时调用方按
    "采集链路故障"处理，不得当作"画面无变化"继续推进状态机。
    """

    def __init__(self, app):  # app: MaaRacingMasterController
        self._app = app

    def screenshot(self) -> np.ndarray | None:
        wgc = getattr(self._app, "_wgc_capture", None)
        if wgc is not None and wgc.is_running:
            try:
                rgb, _fid, _ts, _age = wgc.get_latest_rgb()
                if rgb is not None:
                    return rgb
            except Exception:  # noqa: BLE001 —— 读帧异常按缺帧处理，不回退
                pass
        return None

    def frame_with_age(self):
        """带新鲜度的帧四元组 (rgb, frame_id, ts_ns, age_ms)。

        v4 WgcapController 专用（帧新鲜度守卫的数据源）。WGC 缺席/异常时
        返回 (None, 0, 0, inf)——调用方按"帧缺失"处理，不回退 MAA 截图
        （v4 宪法：帧只从中心缓存来，回退会造成双时间线）。
        """
        import math

        wgc = getattr(self._app, "_wgc_capture", None)
        if wgc is not None and wgc.is_running:
            try:
                return wgc.get_latest_rgb()
            except Exception:  # noqa: BLE001 —— 读帧异常按缺帧处理
                pass
        return (None, 0, 0, math.inf)


class GamepadAdapter:
    """把 controller 手柄管理包装成 GamepadCapability（租约语义）。

    - `acquire()` 进入时懒创建/复用 controller._gpad，退出时归零归还（复用）。
    - `reset_device()` 断开底层设备（controller._destroy_gpad），下次 acquire 重建。
    - 不变量：活跃租约（_active > 0）存在时 `reset_device()` 抛 RuntimeError，
      绝不静默销毁正在借用中的设备。
    """

    def __init__(self, app):  # app: MaaRacingMasterController
        self._app = app
        self._active = 0  # 活跃租约计数（跨所有 acquire 的并发活跃数）

    def acquire(self) -> AbstractContextManager[Gamepad]:
        return GamepadLease(self)

    def persistent_adapter(self) -> Gamepad:
        """取一个**长期持有**的手柄适配器（不随上下文退出释放）。

        `acquire()` 是租约语义（`__exit__` 归零归还：松按钮 + 摇杆归中 + reset），
        适合"借一次就还"的调用方；手柄导航器则要跨整个会话持有同一设备（导航线程
        常驻、每步推摇杆），租约语义不适用。本方法是模块侧获取导航设备的唯一公开
        入口——此前鉴宝模块直接穿 `ctx.gamepad._app._get_gpad()` 掏两层私有。

        调用方自担生命周期：设备重连/重建走 `reset_device()`（有活跃租约保护）；
        本方法不计入 `_active`——导航器不是租约，而是常驻持有者。
        """
        return VGamepadAdapter(self._app._get_gpad())

    def _enter(self) -> Gamepad:
        self._active += 1
        return VGamepadAdapter(self._app._get_gpad())

    def _exit(self) -> None:
        self._active -= 1
        # 归零归还：松开按钮 + 摇杆归中，实例仍可复用
        self._app._reset_gpad()

    def reset_device(self) -> None:
        if self._active > 0:
            raise RuntimeError(
                f"reset_device: 仍有 {self._active} 个活跃手柄租约，禁止断开正在借用的设备"
            )
        self._app._destroy_gpad()


class LifecycleAdapter:
    """把 controller 停止信号/可中断睡眠包装成 Lifecycle。"""

    def __init__(self, app):  # app: MaaRacingMasterController
        self._app = app

    @property
    def running(self) -> bool:
        return not self._app.stop_event.is_set()

    def request_stop(self) -> None:
        self._app.stop_event.set()

    def sleep(self, seconds: float) -> bool:
        # 返回是否被中断（False=被停止信号提前返回）。
        # 墙钟语义：正常情况下实际睡眠时长 ≥ seconds——量化实现做不到这一点：
        # 旧版按 int(seconds/0.1) 迭代，小于 0.1s 的入参静默退化为零睡眠的忙旋
        # （真机 2026-09-14：彩蛋链等待循环节传 0.05，6s 窗空转数百万次、持 GIL
        # 饿死导航 worker 与观察线程），非整数倍入参也被向下截断。故改为
        # deadline 分片：每片 ≤0.1s 保可中断性，末片补齐余数保墙钟下界。
        # 「睡眠时长 ≥ 入参」由 tests/test_capabilities_lifecycle_sleep.py 机检。
        import time
        deadline = time.monotonic() + max(seconds, 0.0)
        while True:
            if not self._app._running or self._app.stop_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.1))


# ==================== DebugRenderer ====================

class DebugRendererCapability(Protocol):
    """调试渲染器能力：以租约方式安装，由 Context 的 ExitStack 接管生命周期。

    - `renderer(renderer)` 返回 context manager：进入时 install，退出时 remove。
      所有权归 Context（通过 `ActivityContext.enter_context` 登记），模块不保存 token。
    - `current()` 只读返回当前安装的渲染器（供绘制复用），未安装返回 None。
    """

    def renderer(self, renderer) -> AbstractContextManager[None]: ...

    def current(self): ...


class _RendererLease:
    """DebugRendererCapability.renderer() 返回的租约：进入 install、退出 remove。"""

    def __init__(self, debug, renderer: object):
        self._debug = debug
        self._renderer = renderer
        self._token: int | None = None

    def __enter__(self) -> None:
        self._token = self._debug.install_renderer(self._renderer)
        return None

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            self._debug.remove_renderer(self._token)
            self._token = None
        return None


class DebugRendererAdapter:
    """把 debug 渲染器管理包装成 DebugRendererCapability（租约 + 只读复用）。"""

    def __init__(self, debug):  # debug: NavigationDebugger
        self._debug = debug

    def renderer(self, renderer) -> AbstractContextManager[None]:
        return _RendererLease(self._debug, renderer)

    def current(self):
        return getattr(self._debug, "_renderer", None)