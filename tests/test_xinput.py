# -*- coding: utf-8 -*-
"""core/xinput 的回归锁：内存布局即协议。

XINPUT_STATE 是 C 结构体的二进制映射，字段顺序或对齐写错时**不会报错**，只会
静默读出错误数值（摇杆值串位）。故本测试用合成报文锁住布局，不依赖真实手柄。
"""

from __future__ import annotations

import ctypes
import struct

import pytest

from maaracing_master.core import xinput


def _pack_state(packet: int, buttons: int, lt: int, rt: int,
                lx: int, ly: int, rx: int, ry: int) -> bytes:
    """按 XINPUT_STATE 的 C 布局打包 16 字节（小端）。

    DWORD dwPacketNumber | WORD wButtons | BYTE bLeftTrigger | BYTE bRightTrigger
    | SHORT sThumbLX | SHORT sThumbLY | SHORT sThumbRX | SHORT sThumbRY
    """
    raw = struct.pack("<IHBBhhhh", packet, buttons, lt, rt, lx, ly, rx, ry)
    assert len(raw) == 16
    return raw


def test_struct_sizes_match_c_protocol() -> None:
    """结构体大小必须是 12 / 16 字节——这是与系统 DLL 的二进制契约。"""
    assert ctypes.sizeof(xinput._XINPUT_GAMEPAD) == 12
    assert ctypes.sizeof(xinput._XINPUT_STATE) == 16


def test_parse_extracts_each_field_in_order() -> None:
    """逐字段解析：任一字段顺序错位都会在此暴露。"""
    raw = _pack_state(
        packet=0xDEADBEEF, buttons=xinput.BUTTON_A | xinput.BUTTON_LEFT_SHOULDER,
        lt=137, rt=255, lx=-32768, ly=32767, rx=-1, ry=1)
    st = xinput.parse_state_bytes(2, raw)

    assert st.index == 2
    assert st.packet == 0xDEADBEEF
    assert st.buttons == (xinput.BUTTON_A | xinput.BUTTON_LEFT_SHOULDER)
    assert st.left_trigger == 137
    assert st.right_trigger == 255
    assert st.left_stick == (-32768, 32767)
    assert st.right_stick == (-1, 1)


def test_parse_handles_negative_stick_values() -> None:
    """摇杆是有符号量：左推为负。若无符号解析会得到 65535 之类的值。"""
    st = xinput.parse_state_bytes(0, _pack_state(0, 0, 0, 0, -16000, -8000, 16000, 8000))
    assert st.left_stick == (-16000, -8000)
    assert st.right_stick == (16000, 8000)
    assert all(-32768 <= v <= 32767 for v in (*st.left_stick, *st.right_stick))


def test_pressed_reads_button_mask() -> None:
    st = xinput.parse_state_bytes(0, _pack_state(1, xinput.BUTTON_B, 0, 0, 0, 0, 0, 0))
    assert st.pressed(xinput.BUTTON_B) is True
    assert st.pressed(xinput.BUTTON_A) is False


def test_button_masks_match_xinput_header() -> None:
    """按钮位掩码取自官方头文件，改动即破坏与系统 DLL 的约定。"""
    assert (xinput.BUTTON_DPAD_UP, xinput.BUTTON_DPAD_DOWN) == (0x0001, 0x0002)
    assert (xinput.BUTTON_DPAD_LEFT, xinput.BUTTON_DPAD_RIGHT) == (0x0004, 0x0008)
    assert (xinput.BUTTON_START, xinput.BUTTON_BACK) == (0x0010, 0x0020)
    assert (xinput.BUTTON_LEFT_THUMB, xinput.BUTTON_RIGHT_THUMB) == (0x0040, 0x0080)
    assert (xinput.BUTTON_LEFT_SHOULDER, xinput.BUTTON_RIGHT_SHOULDER) == (0x0100, 0x0200)
    assert (xinput.BUTTON_A, xinput.BUTTON_B, xinput.BUTTON_X, xinput.BUTTON_Y) == (
        0x1000, 0x2000, 0x4000, 0x8000)


def test_read_state_rejects_out_of_range_slot() -> None:
    """XInput 固定四槽，越界不得触发 DLL 调用。"""
    for bad in (-1, 4, 99):
        assert xinput.read_state(bad) is None


def test_read_state_returns_none_or_valid_snapshot() -> None:
    """真实调用：无手柄应为 None；有手柄则各字段须落在协议值域内。

    不断言"必定为 None"——开发机可能插着手柄，那种情况下应验证解析结果合法。
    """
    st = xinput.read_state(0)
    if st is None:
        pytest.skip("本机槽位 0 无手柄连接")
    assert st.index == 0
    assert 0 <= st.left_trigger <= xinput.TRIGGER_MAX
    assert 0 <= st.right_trigger <= xinput.TRIGGER_MAX
    for v in (*st.left_stick, *st.right_stick):
        assert xinput.STICK_MIN <= v <= xinput.STICK_MAX


def test_connected_indices_are_within_slots() -> None:
    idx = xinput.connected_indices()
    assert isinstance(idx, list)
    assert all(0 <= i < 4 for i in idx)