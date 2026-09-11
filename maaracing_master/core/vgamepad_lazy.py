# -*- coding: utf-8 -*-
"""
vgamepad 懒加载代理。

背景（详见 docs/CLEAN_ENV.md 结论）：
  - sidecar 导入链 controller.py / navigation.py / racing_loop.py 原来在**模块级** `import vgamepad`，
    而无 ViGEmBus 驱动的净机/用户机上连「导入」都会抛 `VIGEM_ERROR_BUS_NOT_FOUND`，
    导致 sidecar 启动即崩（GUI 报 backend disconnected exit code 1）。
  - 修为：把模块级导入换成**按需懒导入**的代理对象 `vg`。只有真正用到手柄（VX360Gamepad /
    XUSB_BUTTON 等属性）时才触发 `import vgamepad`；底层缺失时抛出语义化 GamepadUnavailableError，
    上层据此在「启动检测」里给用户明确提示 + 引导装驱动，而不是让整个后端崩掉。
"""

from __future__ import annotations

# 统一错误码（前端据此判定是否弹「下载 ViGEmBus 驱动」引导框）
VIGEM_BUS_MISSING = "VIGEM_BUS_MISSING"


class GamepadUnavailableError(RuntimeError):
    """虚拟手柄不可用（通常因缺少 ViGEmBus 内核驱动，驱动无法 app-local 随包分发）。"""

    def __init__(self, reason: str = VIGEM_BUS_MISSING, message: str = ""):
        super().__init__(message or VIGEM_BUS_MISSING)
        self.reason = reason


class _LazyVg:
    """模块代理：首次访问任意属性时才真正 `import vgamepad`。

    用法：`from maaracing_master.core.vgamepad_lazy import vg`，然后照常 `vg.VX360Gamepad()` /
    `vg.XUSB_BUTTON.XUSB_GAMEPAD_A`。后台自动按需导入、失败抛 GamepadUnavailableError。
    """

    _m = None  # 缓存真实 vgamepad 模块（成功后不再重试导入）

    def __getattr__(self, name: str):
        if _LazyVg._m is None:
            try:
                import vgamepad as vg
                _LazyVg._m = vg
            except Exception as exc:  # noqa: BLE001 —— 任何导入失败都归因于缺驱动/缺 VM runtime
                raise GamepadUnavailableError(
                    VIGEM_BUS_MISSING,
                    "缺少 ViGEmBus 驱动，虚拟手柄无法创建。请先安装 ViGEmBus 驱动后重试。"
                ) from exc
        return getattr(_LazyVg._m, name)


vg = _LazyVg()


def gamepad_available() -> bool:
    """探测 vgamepad 在当前主机是否可直接使用（结果按导入是否成功判定，不创建实例）。"""
    try:
        _ = vg.VX360Gamepad  # 触发懒导入；缺驱动时抛 GamepadUnavailableError
        return True
    except GamepadUnavailableError:
        return False