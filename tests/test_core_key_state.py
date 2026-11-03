# -*- coding: utf-8 -*-
"""具名键盘读取件的回归锁：键名→VK 映射与未知键行为（不碰真实 Win32）。"""
from __future__ import annotations

import sys
import types

from maaracing_master.core import key_state


def test_key_names_map_to_win32_vk():
    assert key_state.KEY_VK["a"] == 0x41
    assert key_state.KEY_VK["d"] == 0x44
    assert key_state.KEY_VK["w"] == 0x57
    assert key_state.KEY_VK["s"] == 0x53
    assert key_state.KEY_VK["left"] == 0x25
    assert key_state.KEY_VK["right"] == 0x27


def test_key_down_unknown_name_is_false(monkeypatch):
    fake = types.ModuleType("maaracing_master.core.window_utils")
    fake.is_key_down = lambda vk: vk == 0x41
    monkeypatch.setitem(sys.modules, "maaracing_master.core.window_utils", fake)
    assert key_state.key_down("a") is True
    assert key_state.key_down("d") is False
    assert key_state.key_down("no_such_key") is False


def test_read_keys_covers_all_named_keys(monkeypatch):
    fake = types.ModuleType("maaracing_master.core.window_utils")
    fake.is_key_down = lambda vk: vk == 0x57
    monkeypatch.setitem(sys.modules, "maaracing_master.core.window_utils", fake)
    keys = key_state.read_keys()
    assert set(keys) == set(key_state.KEY_VK)
    assert keys["w"] is True
    assert keys["a"] is False
