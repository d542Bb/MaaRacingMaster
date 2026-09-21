#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
窗口查找与物理手柄检测工具 + Win32 交互原语（DPI / 坐标换算 / 真实点击 / 按键计数 / 窗口调整）。
"""

from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes

from maa.toolkit import Toolkit

from maaracing_master.core.logger import logger
from maaracing_master.core.paths import framework_dir

# ---- Win32 常量 ----
_UD = ctypes.windll.user32
_SHcore = ctypes.windll.shcore
_K32 = ctypes.windll.kernel32

_GWL_STYLE = -16
_GWL_EXSTYLE = -20
# GWL_STYLE 返回值是 32 位 LONG（64 位进程下 GetWindowLongW 语义不变）
_UD.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_UD.GetWindowLongW.restype = ctypes.c_long
_UD.AdjustWindowRectEx.argtypes = [
    ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_UD.AdjustWindowRectEx.restype = wintypes.BOOL
_UD.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT]
_UD.SetWindowPos.restype = wintypes.BOOL
_UD.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_UD.SetCursorPos.restype = wintypes.BOOL
_UD.GetForegroundWindow.restype = wintypes.HWND
_UD.GetAsyncKeyState.argtypes = [ctypes.c_int]
_UD.GetAsyncKeyState.restype = ctypes.c_short

# SendInput（INPUT/MOUSEINPUT + KEYBDINPUT 结构，dwExtraInfo 为 ULONG_PTR=指针宽）
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
# F13：标准键盘不存在该键，无任何系统/应用组合键副作用，用于解除前台锁定的安全注入键
VK_F13 = 0x7C
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
# GetAsyncKeyState 的「当前按下」高位
_ASYNC_DOWN = 0x8000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR（64 位下 8 字节）
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR（64 位下 8 字节）
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_UD.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_UD.SendInput.restype = wintypes.UINT

# 前台激活 / 显示器枚举（窗口切前台 + 屏幕内校验）
_UD.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_UD.GetWindowRect.restype = wintypes.BOOL
_UD.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_UD.GetWindowThreadProcessId.restype = wintypes.DWORD
_K32.GetCurrentThreadId.restype = wintypes.DWORD
_UD.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_UD.AttachThreadInput.restype = wintypes.BOOL
_UD.SetForegroundWindow.argtypes = [wintypes.HWND]
_UD.SetForegroundWindow.restype = wintypes.BOOL
_UD.BringWindowToTop.argtypes = [wintypes.HWND]
_UD.BringWindowToTop.restype = wintypes.BOOL
_UD.SetFocus.argtypes = [wintypes.HWND]
_UD.SetFocus.restype = wintypes.HWND
_UD.IsIconic.argtypes = [wintypes.HWND]
_UD.IsIconic.restype = wintypes.BOOL
_UD.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_UD.ShowWindow.restype = wintypes.BOOL
_UD.EnumDisplayMonitors.argtypes = [
    wintypes.HDC, ctypes.POINTER(wintypes.RECT),
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM),
    wintypes.LPARAM,
]
_UD.EnumDisplayMonitors.restype = wintypes.BOOL


# =====================================================================
#  DPI awareness（进程级，须尽早调用；子进程不继承父进程配置）
# =====================================================================

def ensure_dpi_aware() -> bool:
    """显式建立 DPI awareness：首选 Per-Monitor(2)，失败回退 System DPI。返回是否成功。

    注意：DPI awareness 是进程级语义，Python sidecar 不会自动继承 C# shell 的配置，
    必须在创建任何窗口 / 初始化 Win32 坐标 API 之前调用（幂等，重复调用失败会忽略）。
    """
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        if _SHcore.SetProcessDpiAwareness(2) == 0:
            return True
    except Exception:
        pass
    try:
        return bool(_UD.SetProcessDPIAware())
    except Exception:
        return False


# =====================================================================
#  客户区尺寸 / 屏幕原点（坐标换算的基础）
# =====================================================================

def window_client_origin(hwnd: int) -> tuple[int, int] | None:
    """客户区左上角在全屏坐标系中的位置（物理像素）。失败返回 None。"""
    pt = wintypes.POINT(0, 0)
    if not hwnd or not _UD.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return pt.x, pt.y


def window_client_size(hwnd: int) -> tuple[int, int] | None:
    """客户区物理尺寸 (w, h)。失败返回 None。"""
    rect = wintypes.RECT()
    if not hwnd or not _UD.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.right - rect.left, rect.bottom - rect.top


def norm_to_screen(hwnd: int, cx: float, cy: float, client_w: int, client_h: int) -> tuple[int, int] | None:
    """归一化意图坐标 (cx, cy) → 全屏物理坐标。

    以客户区物理尺寸为锚（与截图帧对齐），并按像素索引 clamp 防 (1,1) 落出客户区。
    """
    origin = window_client_origin(hwnd)
    if origin is None:
        return None
    ox, oy = origin
    x = min(client_w - 1, max(0, round(cx * client_w)))
    y = min(client_h - 1, max(0, round(cy * client_h)))
    return ox + x, oy + y


def verify_frame_client(hwnd: int, frame_w: int, frame_h: int) -> None:
    """校验「截图帧尺寸 vs 客户区物理尺寸」：显著不等则映射可能偏移，WARNING 提示。

    WGC 中心采集固定输出 1280x720 标准帧；客户区经 AdjustWindowRectEx 取整后可能
    为 1281x721（±1px，映射偏移 <0.1% 可忽略）——仅在差异 ≥2px 时告警，避免每帧
    误报刷屏（2026-09-04 WGC 中心化后适配）。
    """
    size = window_client_size(hwnd)
    if size is None:
        return
    cw, ch = size
    if abs(cw - frame_w) >= 2 or abs(ch - frame_h) >= 2:
        logger.log(
            f"[坐标] 截图帧尺寸({frame_w}x{frame_h}) ≠ 客户区物理尺寸({cw}x{ch})，"
            "归一化→屏幕映射可能偏移（本链路应 1:1）", "WARNING",
        )


# =====================================================================
#  真实点击（可见移动 → 停顿 → SendInput 左键）
# =====================================================================

def set_cursor_visible(sx: int, sy: int) -> bool:
    """把可见鼠标指针移到全屏坐标 (sx, sy)。"""
    if not sx or not sy:
        return False
    return bool(_UD.SetCursorPos(sx, sy))


def send_left_click(down_up_gap_ms: int = 30) -> bool:
    """SendInput 注入鼠标左键 按下→抬起（间隔 down_up_gap_ms）。返回是否成功。

    dx=dy=0 且无 MOUSEEVENTF_ABSOLUTE → 使用当前光标位置。
    """
    def _event(flags: int) -> bool:
        inp = _INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dwFlags = flags
        inp.u.mi.dx = 0
        inp.u.mi.dy = 0
        return _UD.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1

    if not _event(MOUSEEVENTF_LEFTDOWN):
        return False
    time.sleep(max(0.02, down_up_gap_ms / 1000.0))
    return _event(MOUSEEVENTF_LEFTUP)


def is_foreground(hwnd: int) -> bool:
    """目标窗口是否为当前前台窗口（点击安全校验用，不主动抢前台）。"""
    if not hwnd:
        return True  # 无句柄时不拦截（正常运行时句柄必有效）
    return _UD.GetForegroundWindow() == hwnd


def _send_key(vk: int, keyup: bool = False) -> bool:
    """注入一次键盘按键（SendInput）。用于让 Windows 把当前进程标记为「最近收到输入」，解除前台锁定。"""
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = 0
    inp.u.ki.dwFlags = KEYEVENTF_KEYUP if keyup else 0
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = 0
    return _UD.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1


def activate_window(hwnd: int) -> bool:
    """把窗口切到前台（开始按钮后的用户明确操作；运行中点击不调用此函数）。

    组合：还原最小化 → 注入一次 F13（让系统认为本进程最近收到输入，解除前台锁定）
    → SetForegroundWindow → BringWindowToTop → SetFocus。
    F13 是标准键盘不存在的键，不触发任何系统/应用组合键，安全无副作用。
    返回是否已成为前台（仍可能被前台锁定拒绝，仅告警不阻断）。
    """
    if not hwnd:
        return False
    try:
        if _UD.IsIconic(hwnd):
            _UD.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 注入一次 F13 按键：让 Windows 把本进程标记为「最近收到输入」，从而解除前台锁定
        _send_key(VK_F13)
        _send_key(VK_F13, keyup=True)
        fg = _UD.GetForegroundWindow()
        fg_tid = _UD.GetWindowThreadProcessId(fg, None)
        cur_tid = _K32.GetCurrentThreadId()
        attached = False
        if fg and fg != hwnd and fg_tid != cur_tid:
            attached = bool(_UD.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            _UD.SetForegroundWindow(hwnd)
            _UD.BringWindowToTop(hwnd)
            _UD.SetFocus(hwnd)
        finally:
            if attached:
                _UD.AttachThreadInput(cur_tid, fg_tid, False)
        # 前台切换是异步的：轮询等待确认（最多 ~300ms），避免切换未完成就误判失败
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            if _UD.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.02)
        return False
    except Exception:
        return False


def is_window_on_screen(hwnd: int) -> bool:
    """窗口是否完整落在显示器可视范围内（任一角超出屏幕 → False）。

    启动校验用：窗口部分拖出屏幕时，点击目标坐标（归一化→全屏）可能落在屏外导致点击落空、流程卡死，
    故要求窗口四个角都位于某块显示器内（跨屏分屏窗口同样通过；只有真的伸出屏幕才拦截）。
    """
    rect = wintypes.RECT()
    if not hwnd or not _UD.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return False

    monitors: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def _cb(_hmon, _hdc, lprc, _lparam):
        m = lprc.contents
        monitors.append((m.left, m.top, m.right, m.bottom))
        return True

    try:
        _UD.EnumDisplayMonitors(None, None, _cb, 0)
    except Exception:
        return True  # 无法枚举显示器时不误杀，放行
    if not monitors:
        return True

    def _inside(x: int, y: int) -> bool:
        return any(mleft <= x < mright and mtop <= y < mbottom for mleft, mtop, mright, mbottom in monitors)

    return all(
        _inside(cx, cy)
        for cx, cy in (
            (rect.left, rect.top), (rect.right - 1, rect.top),
            (rect.left, rect.bottom - 1), (rect.right - 1, rect.bottom - 1),
        )
    )


def count_pressed_keys() -> int:
    """统计当前同时按下的键盘按键数（GetAsyncKeyState，系统级）。

    只统计键盘键（跳过 0x01~0x06 鼠标键与保留码 0x0A）；命中 2 个即提前返回。
    """
    n = 0
    for vk in range(0x08, 0xFF):
        if vk == 0x0A:  # 保留
            continue
        if _UD.GetAsyncKeyState(vk) & _ASYNC_DOWN:
            n += 1
            if n >= 2:
                return n
    return n


# =====================================================================
#  窗口比例校验（只读：不调整窗口/分辨率，仅检查客户区宽高比 ≈ 16:9）
# =====================================================================

def check_game_window_aspect(hwnd: int, tol: float = 0.05) -> bool:
    """校验游戏窗口客户区宽高比是否大致 16:9（只读，不改窗口尺寸/分辨率）。

    模板与 ROI 均按 720p(16:9) 归一化，客户区非 16:9（16:10 / 21:9 / 4:3 全屏等）
    会识别错位，故启动时校验：不符返回 False，由调用方报错终止。
    容差 tol=±5%（≈1.69~1.87），覆盖边框/DPI 缩放等微小偏差。
    获取客户区失败（窗口无效）同样返回 False。
    """
    size = window_client_size(hwnd)
    if size is None or size[0] <= 0 or size[1] <= 0:
        logger.log("[坐标] 获取游戏窗口客户区尺寸失败，无法校验 16:9 比例", "DEBUG")
        return False
    cw, ch = size
    target = 16 / 9
    aspect = cw / ch
    ok = abs(aspect - target) <= target * tol
    if not ok:
        logger.log(
            f"[坐标] 游戏窗口客户区 {cw}x{ch}（比例 {aspect:.3f}）不是 16:9（目标 {target:.3f}）",
            "DEBUG",
        )
    return ok


# =====================================================================
#  窗口尺寸调整（写：把游戏窗口客户区统一调整为 720p）
# =====================================================================

# SWP 组合标志：保留位置与 Z 序、不激活；FRAMECHANGED 让系统重算边框
_SWP_FRAMECHANGED = 0x0020
_SW_RESTORE = 9


def ensure_window_restored(hwnd: int) -> bool:
    """窗口若处于最小化则还原（幂等）。用于 WGC 采集启动前——最小化窗口无
    渲染内容可捕获（GraphicsCaptureItem 无帧）。"""
    if not hwnd:
        return False
    try:
        if _UD.IsIconic(hwnd):
            return bool(_UD.ShowWindow(hwnd, _SW_RESTORE))
        return True
    except Exception:
        return False


def resize_game_window_720p(hwnd: int, client_w: int = 1280, client_h: int = 720) -> bool:
    """把游戏窗口客户区调整为 1280×720（720p），保持左上角位置不变。等幂：已是目标尺寸则跳过。

    各模块启动（connect）前统一调用，保证截图帧 / 模板 / ROI 均落在 720p 客户区，
    避免多尺寸适配前的识别错位。返回是否成功；窗口无效或已是最佳尺寸同样视为成功返回 True。
    """
    size = window_client_size(hwnd)
    if size is not None and size[0] == client_w and size[1] == client_h:
        return True
    if not hwnd:
        return False
    try:
        if _UD.IsIconic(hwnd):
            _UD.ShowWindow(hwnd, _SW_RESTORE)  # 最小化先还原，再调整尺寸
        style = _UD.GetWindowLongW(hwnd, _GWL_STYLE)
        exstyle = _UD.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        rect = wintypes.RECT(0, 0, client_w, client_h)
        # 由目标客户区反推包含边框的窗口总尺寸（菜单条按无系统菜单处理）
        if not _UD.AdjustWindowRectEx(ctypes.byref(rect), style, 0, exstyle):
            return False
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        ok = bool(_UD.SetWindowPos(
            hwnd, 0, 0, 0, win_w, win_h,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | _SWP_FRAMECHANGED,
        ))
        if ok:
            logger.log(f"[坐标] 已将游戏窗口客户区调整为 {client_w}x{client_h}（720p）")
        return ok
    except Exception:
        logger.log("[坐标] 调整窗口尺寸失败", "WARNING")
        return False


def hwnd_from_pid(pid: int) -> int:
    user32 = ctypes.windll.user32
    _cache = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        found_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
        if found_pid.value == pid:
            _cache["hwnd"] = hwnd
            return False
        return True

    user32.EnumWindows(callback, 0)
    return _cache.get("hwnd", 0)


def terminate_process_by_hwnd(hwnd: int) -> None:
    """根据窗口句柄终结其所属进程（含子进程树）。用于「运行结束后自动关闭游戏」。

    经由 GetWindowThreadProcessId 取 PID → taskkill /T 连子树一并杀。
    """
    if not hwnd:
        raise ValueError("hwnd 为空")
    pid = wintypes.DWORD()
    _UD.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        raise RuntimeError("无法由 hwnd 获取 PID")
    # capture_output 抑制 taskkill 中文输出刷到控制台；check=False 失败时返回非零码由调用方判读
    subprocess.run(
        ["taskkill", "/PID", str(pid.value), "/F", "/T"],
        timeout=15, check=False, capture_output=True,
    )


def has_physical_controller() -> bool:
    """检测是否有物理 Xbox 手柄已连接（在创建虚拟手柄前调用）"""
    try:
        for dll_name in ["xinput1_4.dll", "xinput9_1_0.dll", "xinput1_3.dll"]:
            try:
                dll = ctypes.windll[dll_name]
                break
            except Exception:
                continue
        else:
            return False

        buf = ctypes.create_string_buffer(16)
        for i in range(4):
            if dll.XInputGetState(i, buf) == 0:
                return True
        return False
    except Exception:
        return False


def find_game_hwnd(group_id: str | None = None) -> int:
    """查找游戏窗口句柄。group_id 非空时过程日志归入该组（契约方案 A：显式捕获，
    调用方决定归属；默认无组散行，行为与旧一致）。"""
    def _lg(msg: str, level: str = "INFO") -> None:
        if group_id is not None:
            logger.log(msg, level, group_id=group_id)
        else:
            logger.log(msg, level)

    # MAA user_path 指向 framework/ 子目录：maafw 产物（maafw.log/cache）与应用数据隔离，
    # 开发/发行一致且不受安装目录权限影响；路径解析失败时退回包根
    try:
        fw = framework_dir()
        fw.mkdir(parents=True, exist_ok=True)
        Toolkit.init_option(str(fw))
    except Exception:
        pass

    windows = Toolkit.find_desktop_windows()

    # 按窗口标题关键词匹配（跨设备稳定；不按类名 UnrealWindow——多 UE 窗口场景会歧义连错）
    keywords = ["巅峰极速", "g112", "Racing Master"]
    for win in windows:
        for kw in keywords:
            if kw in win.window_name:
                hwnd = int(win.hwnd)
                _lg(f"找到窗口(标题): hWnd={hwnd}, title={win.window_name}")
                return hwnd

    GAME_PID = 0
    if GAME_PID:
        hwnd = hwnd_from_pid(GAME_PID)
        if hwnd:
            _lg(f"找到窗口(PID): hWnd={hwnd}")
            return hwnd

    _lg("未找到游戏窗口，可用窗口前10个:", "ERROR")
    for win in windows[:10]:
        _lg(f"  hWnd={win.hwnd}, class={win.class_name}, title={win.window_name}", "ERROR")

    return 0
