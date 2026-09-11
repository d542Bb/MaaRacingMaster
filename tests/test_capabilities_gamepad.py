# -*- coding: utf-8 -*-
"""Gamepad Protocol 收敛测试：VGamepadAdapter 转发 + 按钮语义常量。

覆盖 racing 收敛到 core gamepad 能力后新增的接口（right_joystick / left_trigger /
right_trigger）与 BUTTON_A/BUTTON_B 常量，用假 pad 隔离 vgamepad 依赖。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

# 屏蔽可能触发底层 vgamepad 导入的模块，保证测试在无 ViGEmBus 环境独立运行。
# capabilities 的按钮常量是惰性的（模块级 __getattr__），仅访问 BUTTON_A/B 才触发；
# 这里注入假 vgamepad 语义模块，使两个常量可被编译验证。
_FakeXUSB = types.SimpleNamespace(
    XUSB_GAMEPAD_A="BUTTON_A_ENUM",
    XUSB_GAMEPAD_B="BUTTON_B_ENUM",
)
_FakeVg = types.SimpleNamespace(XUSB_BUTTON=_FakeXUSB)
# capabilities.py 里 `from ...vgamepad_lazy import vg` 需要从该模块取名为 `vg` 的对象，
# 因此假模块要有一个 `vg` 属性承载 XUSB_BUTTON 枚举。
_FakeLazy = types.SimpleNamespace(vg=_FakeVg)
for _key in ("vgamepad_lazy", "maaracing_master.core.vgamepad_lazy"):
    assert _key not in sys.modules
    sys.modules[_key] = _FakeLazy

from maaracing_master.core.capabilities import (  # noqa: E402
    VGamepadAdapter,
)


class _FakePad:
    """记录调用参数的假手柄，隔离底层 vgamepad。"""

    def __init__(self):
        self.calls = []

    def press_button(self, button):
        self.calls.append(("press_button", button))

    def release_button(self, button):
        self.calls.append(("release_button", button))

    def left_joystick(self, x_value=0, y_value=0):
        self.calls.append(("left_joystick", x_value, y_value))

    def right_joystick(self, x_value=0, y_value=0):
        self.calls.append(("right_joystick", x_value, y_value))

    def left_trigger(self, value=0):
        self.calls.append(("left_trigger", value))

    def right_trigger(self, value=0):
        self.calls.append(("right_trigger", value))

    def update(self):
        self.calls.append(("update",))


def test_right_joystick_forwarded():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.right_joystick(x_value=100, y_value=-200)
    assert pad.calls[-1] == ("right_joystick", 100, -200)


def test_left_trigger_forwarded():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.left_trigger(value=150)
    assert pad.calls[-1] == ("left_trigger", 150)


def test_right_trigger_forwarded():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.right_trigger(value=230)
    assert pad.calls[-1] == ("right_trigger", 230)


def test_left_joystick_default_args():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.left_joystick()
    assert pad.calls[-1] == ("left_joystick", 0, 0)


def test_button_constants_exist():
    from maaracing_master.core.capabilities import __getattr__
    # 惰性求值返回枚举（此处为假语义模块注入的值）
    a = __getattr__("BUTTON_A")
    b = __getattr__("BUTTON_B")
    assert a == "BUTTON_A_ENUM"
    assert b == "BUTTON_B_ENUM"