# -*- coding: utf-8 -*-
"""物理手柄状态读取（XInput，Windows 平台层）。

**为什么独立成件**：XInput 是 Win32 平台事实，按分层纪律插件不得直接接触；而
``window_utils.has_physical_controller()`` 原有的调用只判断"是否连上"，没有解析
摇杆量。驾驶类能力需要**连续量**（左摇杆推量、扳机油门），故在此提供完整状态读取。

**只读，不注入**：本模块只调 ``XInputGetState``（只读接口），不涉及 ViGEm/vgamepad
（那是发送侧，见 ``core/abilities`` 的 Gamepad 能力）。录制物理手柄演示时使用本模块，
控制车辆时使用虚拟手柄——两者不可同时占用同一游戏输入通道。

**零依赖**：直接用 ctypes 调系统自带的 xinput DLL，不引入第三方包。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

# XInput 按钮位掩码（官方头文件取值）
BUTTON_DPAD_UP = 0x0001
BUTTON_DPAD_DOWN = 0x0002
BUTTON_DPAD_LEFT = 0x0004
BUTTON_DPAD_RIGHT = 0x0008
BUTTON_START = 0x0010
BUTTON_BACK = 0x0020
BUTTON_LEFT_THUMB = 0x0040
BUTTON_RIGHT_THUMB = 0x0080
BUTTON_LEFT_SHOULDER = 0x0100
BUTTON_RIGHT_SHOULDER = 0x0200
BUTTON_A = 0x1000
BUTTON_B = 0x2000
BUTTON_X = 0x4000
BUTTON_Y = 0x8000

# 摇杆与扳机的取值域（协议常量，非可调参数）
STICK_MIN = -32768
STICK_MAX = 32767
TRIGGER_MIN = 0
TRIGGER_MAX = 255

# XInputGetState 返回码（成功值；其余含 1167=未连接，一律按"该槽无手柄"处理）
_ERROR_SUCCESS = 0

_MAX_SLOTS = 4  # XInput 固定四槽

# DLL 名按优先级尝试：1_4 是 Win8+ 首选，9_1_0 / 1_3 为旧系统回退
_DLL_NAMES = ("xinput1_4.dll", "xinput9_1_0.dll", "xinput1_3.dll")

_dll = None
_dll_probed = False


class _XINPUT_GAMEPAD(ctypes.Structure):
    """XINPUT_GAMEPAD：12 字节（全部成员自然对齐即为紧凑布局）。"""

    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    """XINPUT_STATE：dwPacketNumber(4) + XINPUT_GAMEPAD(12) = 16 字节。"""

    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", _XINPUT_GAMEPAD),
    ]


@dataclass(frozen=True)
class PadState:
    """手柄一帧的完整状态快照。坐标域见 STICK_MIN/MAX 与 TRIGGER_MIN/MAX。"""

    index: int
    packet: int
    buttons: int
    left_trigger: int
    right_trigger: int
    left_stick: tuple[int, int]
    right_stick: tuple[int, int]

    def pressed(self, button_mask: int) -> bool:
        """该按钮当前是否按下。"""
        return bool(self.buttons & button_mask)


def _load_dll():
    """惰性加载 xinput DLL；非 Windows 或全部缺失时返回 None（调用方按"无手柄"处理）。"""
    global _dll, _dll_probed
    if _dll_probed:
        return _dll
    _dll_probed = True
    if sys.platform != "win32":
        return None
    for name in _DLL_NAMES:
        try:
            dll = ctypes.windll[name]  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 —— 缺该 DLL 是预期回退路径
            continue
        dll.XInputGetState.argtypes = [ctypes.c_ulong, ctypes.POINTER(_XINPUT_STATE)]
        dll.XInputGetState.restype = ctypes.c_ulong
        _dll = dll
        break
    return _dll


def parse_state_bytes(index: int, raw: bytes) -> PadState:
    """从 16 字节 XINPUT_STATE 原始报文解析出 PadState。

    独立成函数是为了让"内存布局 = 协议"这件事可被测试锁住：ctypes 结构体一旦
    对齐/字段顺序写错，读出来的是静默错值（不报错），只能靠合成报文验证。
    """
    st = _XINPUT_STATE.from_buffer_copy(raw)
    g = st.Gamepad
    return PadState(
        index=index,
        packet=int(st.dwPacketNumber),
        buttons=int(g.wButtons),
        left_trigger=int(g.bLeftTrigger),
        right_trigger=int(g.bRightTrigger),
        left_stick=(int(g.sThumbLX), int(g.sThumbLY)),
        right_stick=(int(g.sThumbRX), int(g.sThumbRY)),
    )


def read_state(index: int) -> PadState | None:
    """读第 index 槽（0~3）的手柄状态；未连接 / 平台不支持返回 None。

    只读接口，不改变任何设备状态。
    """
    if not 0 <= index < _MAX_SLOTS:
        return None
    dll = _load_dll()
    if dll is None:
        return None
    state = _XINPUT_STATE()
    try:
        ret = dll.XInputGetState(index, ctypes.byref(state))
    except Exception:  # noqa: BLE001 —— DLL 调用异常按"不可用"处理，不中断调用方循环
        return None
    if ret != _ERROR_SUCCESS:
        # 未连接（1167）与其他错误一律按"该槽无手柄"处理：调用方要的是可用状态，
        # 不是错误分类。
        return None
    return parse_state_bytes(index, bytes(state))


def connected_indices() -> list[int]:
    """当前已连接的手柄槽位列表（通常 0~1 个）。"""
    found = []
    for i in range(_MAX_SLOTS):
        if read_state(i) is not None:
            found.append(i)
    return found


def read_first_connected() -> PadState | None:
    """读第一个已连接手柄的状态；无手柄返回 None。"""
    for i in range(_MAX_SLOTS):
        st = read_state(i)
        if st is not None:
            return st
    return None