# -*- coding: utf-8 -*-
"""具名键盘按键状态读取（Win32 平台层）。

**为什么独立成件**：``window_utils`` 是插件不得直接 import 的平台件；
它的 ``count_pressed_keys`` 只服务于急停的"按下几个键"计数，没有按键级读取。
驾驶类能力需要按名字查询具体按键（A/D/W/S 与方向键），故在此提供**只读**的
具名接口——口径与 ``core/xinput.py`` 相同：平台事实独立成件，插件只依赖本模块。

本模块只读 ``GetAsyncKeyState``，不注入、不监听、不抢焦点。系统级读取意味着
"游戏窗口在前台"需要调用方自行判断（见 ``ActivityContext.window_foreground``）。
"""

from __future__ import annotations

# 驾驶键 → Win32 虚拟键码（只收当前需要的一组，不做通用键盘库）
KEY_VK: dict[str, int] = {
    "a": 0x41,
    "d": 0x44,
    "w": 0x57,
    "s": 0x53,
    "left": 0x25,
    "right": 0x27,
    "up": 0x26,
    "down": 0x28,
}


def key_down(name: str) -> bool:
    """按键名查询当前是否按下；未知键名返回 False。"""
    vk = KEY_VK.get(name)
    if vk is None:
        return False
    from maaracing_master.core.window_utils import is_key_down

    return bool(is_key_down(vk))


def read_keys() -> dict[str, bool]:
    """一次读取全部具名键的状态（逐键调用 GetAsyncKeyState）。"""
    return {name: key_down(name) for name in KEY_VK}
