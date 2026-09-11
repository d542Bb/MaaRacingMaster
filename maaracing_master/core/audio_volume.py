#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程级音频会话音量控制（纯 ctypes 实现 WASAPI，零第三方依赖）。

背景：GUI「运行选项」的「运行时静音游戏」开关需要把【游戏进程】的音量静音、
结束后恢复 100%。Windows 音量合成器按「音频会话（session）」粒度控制进程音量。

接口链（2026-09-01 实测定稿）：
    CoCreateInstance(MMDeviceEnumerator)
      → IMMDeviceEnumerator.GetDefaultAudioEndpoint(eRender, eMultimedia)
      → IMMDevice.Activate(IAudioSessionManager2)
      → IAudioSessionManager2.GetSessionEnumerator
      → IAudioSessionEnumerator.GetCount / GetSession
      → IAudioSessionControl2.GetProcessId       ← 按 PID 匹配目标进程（可能多会话）
      → ISimpleAudioVolume.SetMasterVolume        ← 对每个匹配会话设音量

实测踩坑（重要）：
- IAudioSessionControl2 的 IID 权威值是 {BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}，
  误记成 ...-6799-4530-91E7-096F1B8C2E85 会让 QI 全部 E_NOINTERFACE（曾误判为
  "虚拟声卡不支持 Control2"，实际是 IID 错）。
- GetSimpleAudioVolume(NULL, pid) 只能拿到【默认会话】；巅峰极速（g112 引擎）有
  多个并发会话（实测 4 个 active），发声不走默认会话——必须枚举 + GetProcessId
  匹配后对全部匹配会话设音量。
- 进程"尚无音频会话"时（未发声）枚举匹配不到 → 返回 0/空，调用方记 WARNING。
- COM 调用必须在已 CoInitialize 的线程内完成，本模块每次调用函数内
  CoInitializeEx/CoUninitialize 配对（sidecar 的 worker 线程也可安全调用）。
"""
from __future__ import annotations

import ctypes
from ctypes import WINFUNCTYPE, byref, c_float, c_long, c_ulong, c_void_p

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
ole32.CoInitializeEx.restype = c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, c_void_p, c_ulong, c_void_p, ctypes.POINTER(c_void_p)]
ole32.CoCreateInstance.restype = c_long

HRESULT = c_long
FLOAT = c_float

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_ALL = 0x17
# 音频端点枚举参数
ERender = 0
EMultimedia = 1


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(hexs: str) -> GUID:
    """"{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}" → GUID。

    GUID 文本前 3 段是【大端数值】（Data1/Data2/Data3 直接按整数解析，
    内存里由 ctypes 自动按本机小端存放）；末 8 字节原样拷贝。
    """
    p = hexs.strip("{}").split("-")
    if len(p) != 5:
        raise ValueError(f"非法 GUID 文本: {hexs!r}")
    g = GUID()
    g.Data1 = int(p[0], 16)
    g.Data2 = int(p[1], 16)
    g.Data3 = int(p[2], 16)
    ctypes.memmove(byref(g.Data4), bytes.fromhex(p[3] + p[4]), 8)
    return g


# ---- 关键 GUID ----
CLSID_MMDeviceEnumerator = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IMMDevice = _guid("{D666063F-1587-4E43-81F1-B948E807363F}")
IID_IAudioSessionManager2 = _guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioSessionEnumerator = _guid("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
# 权威值（Microsoft win32metadata RecompiledIdlHeaders/um/audiopolicy.h）：
#   BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D —— 之前误记为 ...-6799-4530-91E7-096F1B8C2E85，
#   导致 QI 全部 E_NOINTERFACE（2026-09-01 实测踩坑）。
IID_IAudioSessionControl2 = _guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
IID_ISimpleAudioVolume = _guid("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")


class COM(ctypes.Structure):
    """通用 COM 接口外壳：所有接口都是 vtbl 指针表 + 实例指针。"""
    _fields_ = [("vtbl", ctypes.POINTER(c_void_p))]


# ---- vtable 原型（HRESULT = long，stdcall） ----
_IUnknown_QI = WINFUNCTYPE(HRESULT, ctypes.POINTER(COM), ctypes.POINTER(GUID), ctypes.POINTER(c_void_p))
_IUnknown_AddRef = WINFUNCTYPE(c_ulong, ctypes.POINTER(COM))
_IUnknown_Release = WINFUNCTYPE(c_ulong, ctypes.POINTER(COM))

# IMMDeviceEnumerator: GetDefaultAudioEndpoint 在索引 4
_GetDefaultAudioEndpoint = WINFUNCTYPE(
    HRESULT, ctypes.POINTER(COM), c_ulong, c_ulong, ctypes.POINTER(ctypes.POINTER(COM)))
# IMMDevice: Activate 在索引 3
_Activate = WINFUNCTYPE(
    HRESULT, ctypes.POINTER(COM), ctypes.POINTER(GUID), c_ulong, c_void_p, ctypes.POINTER(c_void_p))
# IAudioSessionManager2: GetSessionEnumerator 在索引 5
_GetSessionEnumerator = WINFUNCTYPE(
    HRESULT, ctypes.POINTER(COM), ctypes.POINTER(ctypes.POINTER(COM)))
# IAudioSessionEnumerator: GetCount=3 / GetSession=4
_GetCount = WINFUNCTYPE(HRESULT, ctypes.POINTER(COM), ctypes.POINTER(ctypes.c_int))
_GetSession = WINFUNCTYPE(
    HRESULT, ctypes.POINTER(COM), ctypes.c_int, ctypes.POINTER(ctypes.POINTER(COM)))
# IAudioSessionControl2: GetProcessId 在索引 14
# （继承 IAudioSessionControl 前 12 个方法：3..11，然后 12 GetSessionIdentifier、
#   13 GetSessionInstanceIdentifier、14 GetProcessId、15 IsSystemSoundsSession、
#   16 SetDuckingPreference）
_GetProcessId = WINFUNCTYPE(HRESULT, ctypes.POINTER(COM), ctypes.POINTER(c_ulong))
# ISimpleAudioVolume: SetMasterVolume=3 / GetMasterVolume=4
_SetMasterVolume = WINFUNCTYPE(HRESULT, ctypes.POINTER(COM), FLOAT, ctypes.POINTER(GUID))
_GetMasterVolume = WINFUNCTYPE(HRESULT, ctypes.POINTER(COM), ctypes.POINTER(FLOAT))


def _release(iface) -> None:
    """释放 COM 接口（vtbl[2] = Release）。iface 必须为 POINTER(COM)。"""
    if iface and iface.contents and iface.contents.vtbl:
        _IUnknown_Release(iface.contents.vtbl[2])(iface)


def _target_pid(hwnd: int) -> int | None:
    """窗口 → 进程 PID。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [c_void_p, ctypes.POINTER(c_ulong)]
    user32.GetWindowThreadProcessId.restype = c_ulong
    pid = c_ulong(0)
    if not user32.GetWindowThreadProcessId(hwnd, byref(pid)) and not pid.value:
        return None
    return pid.value


def _iter_game_volume(hwnd: int, callback):
    """枚举渲染会话，对属于 hwnd 进程的每个会话调用 callback(vol_iface)（ISimpleAudioVolume）。

    callback 收到 POINTER(COM)（本函数负责释放）。返回 callback 成功次数。
    已在本函数内 CoInitialize/CoUninitialize。
    """
    target = _target_pid(hwnd)
    if target is None:
        return 0
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr not in (0, 1):
        return 0

    ok_count = 0
    dev_enum = c_void_p()
    device = c_void_p()
    session_mgr = c_void_p()
    session_enum = c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
            byref(IID_IMMDeviceEnumerator), byref(dev_enum))
        if hr != 0 or not dev_enum.value:
            return 0
        enum_iface = ctypes.cast(dev_enum, ctypes.POINTER(COM))
        out_device = ctypes.POINTER(COM)()
        hr = _GetDefaultAudioEndpoint(enum_iface.contents.vtbl[4])(enum_iface, ERender, EMultimedia, byref(out_device))
        if hr != 0 or not out_device:
            return 0
        device.value = ctypes.cast(out_device, c_void_p).value
        mgr = c_void_p()
        hr = _Activate(out_device.contents.vtbl[3])(
            out_device, byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, byref(mgr))
        if hr != 0 or not mgr.value:
            return 0
        session_mgr.value = mgr.value
        mgr_iface = ctypes.cast(mgr, ctypes.POINTER(COM))
        enum_out = ctypes.POINTER(COM)()
        hr = _GetSessionEnumerator(mgr_iface.contents.vtbl[5])(mgr_iface, byref(enum_out))
        if hr != 0 or not enum_out:
            return 0
        session_enum.value = ctypes.cast(enum_out, c_void_p).value
        enum_if = ctypes.cast(enum_out, ctypes.POINTER(COM))
        count = ctypes.c_int(0)
        if _GetCount(enum_if.contents.vtbl[3])(enum_if, byref(count)) != 0:
            return 0
        for i in range(count.value):
            one = ctypes.POINTER(COM)()
            if _GetSession(enum_if.contents.vtbl[4])(enum_if, i, byref(one)) != 0 or not one:
                continue
            try:
                ctl2 = c_void_p()
                hr = _IUnknown_QI(one.contents.vtbl[0])(one, byref(IID_IAudioSessionControl2), byref(ctl2))
                if hr != 0 or not ctl2.value:
                    continue
                ctl2_iface = ctypes.cast(ctl2, ctypes.POINTER(COM))
                try:
                    proc_id = c_ulong(0)
                    if _GetProcessId(ctl2_iface.contents.vtbl[14])(ctl2_iface, byref(proc_id)) != 0:
                        continue
                    if proc_id.value != target:
                        continue
                    # 匹配：拿 ISimpleAudioVolume 交给 callback
                    vol_out = c_void_p()
                    hr = _IUnknown_QI(one.contents.vtbl[0])(one, byref(IID_ISimpleAudioVolume), byref(vol_out))
                    if hr != 0 or not vol_out.value:
                        continue
                    vol_iface = ctypes.cast(vol_out, ctypes.POINTER(COM))
                    try:
                        if callback(vol_iface):
                            ok_count += 1
                    finally:
                        _release(vol_iface)
                finally:
                    _release(ctl2_iface)
            finally:
                _release(one)
        return ok_count
    finally:
        for p in (session_enum, session_mgr, device, dev_enum):
            if p.value:
                _release(ctypes.cast(p, ctypes.POINTER(COM)))
        ole32.CoUninitialize()


def set_game_volume(hwnd: int, level: float) -> int:
    """把 hwnd 所属进程的【全部】音频会话音量设为 level（0.0~1.0）。

    返回设置成功的会话数（0 = 未找到该进程的任何会话，如游戏尚未发声）。
    注意：巅峰极速有多个并发会话（实测 4 个），必须全部设置。
    """
    if not hwnd:
        return 0
    level = max(0.0, min(1.0, float(level)))

    def _set(vol_iface) -> bool:
        return _SetMasterVolume(vol_iface.contents.vtbl[3])(vol_iface, FLOAT(level), None) == 0

    return _iter_game_volume(hwnd, _set)


def get_game_volume(hwnd: int) -> list[float]:
    """读回 hwnd 所属进程全部匹配会话的音量（0.0~1.0）。无匹配会话返回 []。"""
    if not hwnd:
        return []
    result: list[float] = []

    def _get(vol_iface) -> bool:
        f = FLOAT(0)
        if _GetMasterVolume(vol_iface.contents.vtbl[4])(vol_iface, byref(f)) != 0:
            return False
        result.append(f.value)
        return True

    _iter_game_volume(hwnd, _get)
    return result
