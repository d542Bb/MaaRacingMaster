#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏进程原语：定位安装、直启主程序、等待游戏窗口、关闭进程。

**为什么在 core**：进程启停不属于任何活动玩法，所有模块共用同一份「游戏在哪、
怎么起、怎么关」。插件不该各自实现一遍，也不该反过来操纵宿主窗口句柄。

**平台事实**（2026-10-29 实测结论）：
- 官方启动器先做版本比对与逐文件 MD5 校验，再拼参数拉起游戏主程序；
- 游戏主程序可直接拉起，不经启动器：13s 出窗口，39s 登录完成进大厅；
- 直启参数与启动器一致（含 -nosound / -nohmd 等），照抄即等价；
- 启动器窗口标题与游戏窗口同名，按标题找窗口会认错——这里一律按进程名匹配。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time
from pathlib import Path

from maaracing_master.core.logger import logger
from maaracing_master.core.window_utils import terminate_process_by_hwnd

# 游戏主程序相对安装目录的位置（安装根 = 启动器配置里的 game_dir）
_GAME_EXE_REL = Path("g112") / "Binaries" / "Win64" / "g112-Win64-Shipping.exe"

# 直启参数：与官方启动器实测命令行逐字一致。少一个都可能影响加载（-force_load_content
# 关系到补丁 pak 的加载），所以默认值不由使用者手打，界面给的是这一份。
DEFAULT_ARGS = (
    "-nosound",
    "-nohmd",
    "--MuMuLauncher",
    "-noexceptionhandler",
    "-Hdd",
    "-force_load_content",
)

# 启动器配置（记录 game_dir 的那份）：%LOCALAPPDATA%\RacingMaster\CN\config\config.ini
_LAUNCHER_CONFIG_REL = Path("RacingMaster") / "CN" / "config" / "config.ini"
# 启动器安装包的卸载表名（用于反查 Launcher.exe，回退启动时用）
_LAUNCHER_DISPLAY_HINTS = ("巅峰极速",)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPPROCESS = 0x00000002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# --------------------------------------------------------------------------
# 安装定位
# --------------------------------------------------------------------------

def _read_ini_section(path: Path, section: str) -> dict[str, str]:
    """读 ini 的一个小节（只做本模块需要的简单解析，不引第三方库）。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            continue
        if current != section.lower() or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip().lower()] = v.strip()
    return out


def launcher_config_path() -> Path:
    """官方启动器的配置文件路径（不保证存在）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / _LAUNCHER_CONFIG_REL


def detect_game_exe() -> Path | None:
    """从启动器配置里读出游戏安装目录并拼出主程序路径；探测失败返回 None。

    只读别人的配置文件、不写、不猜多个候选路径——猜出来的路径填进界面比留空更糟。
    """
    info = _read_ini_section(launcher_config_path(), "Info")
    game_dir = info.get("game_dir", "").strip()
    if not game_dir:
        return None
    exe = Path(game_dir.replace("\\", "/")) / _GAME_EXE_REL
    return exe if exe.is_file() else None


def detect_launcher_exe() -> Path | None:
    """反查官方启动器路径（回退启动时用）：扫卸载表里的显示名。

    找不到返回 None——回退是"有则更好"的能力，不因探测失败而阻断启动步骤。
    """
    try:
        import winreg
    except Exception:  # noqa: BLE001 —— 非 Windows 平台不应走到这里
        return None
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as k:
                            display = str(winreg.QueryValueEx(k, "DisplayName")[0])
                            if not any(h in display for h in _LAUNCHER_DISPLAY_HINTS):
                                continue
                            icon = str(winreg.QueryValueEx(k, "DisplayIcon")[0])
                    except OSError:
                        continue
                    exe = Path(icon.split(",")[0].strip().strip('"'))
                    if exe.is_file():
                        return exe
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# 窗口与进程
# --------------------------------------------------------------------------

def _exe_of_pid(pid: int) -> str:
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        _kernel32.CloseHandle(h)


def find_window_by_exe(exe_name: str) -> int:
    """按**进程可执行文件名**找可见窗口，返回 hwnd（找不到为 0）。

    不按标题：启动器窗口与游戏窗口同名，标题匹配会认错窗口。同名进程可能有多个窗口
    （启动画面/主窗口），取面积最大的那个——主窗口才是要连的那个。
    """
    target = exe_name.lower()
    best: tuple[int, int] = (0, 0)  # (面积, hwnd)

    def _cb(hwnd, _lparam):
        nonlocal best
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of_pid(int(pid.value)).lower() != target:
            return True
        r = wt.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        if area > best[0]:
            best = (area, int(hwnd))
        return True

    _user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(_cb), 0)
    return best[1]


def game_running(exe_name: str) -> bool:
    """游戏主程序是否已在运行（按进程名，不依赖窗口是否已出现）。"""
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return False
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe_name.lower():
                return True
            ok = _kernel32.Process32NextW(snap, ctypes.byref(e))
        return False
    finally:
        _kernel32.CloseHandle(snap)


def terminate_by_exe(exe_name: str) -> int:
    """按进程名结束全部同名进程，返回结束的数量（窗口找不到时的兜底）。"""
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    pids: list[int] = []
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return 0
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe_name.lower():
                pids.append(int(e.th32ProcessID))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _kernel32.CloseHandle(snap)

    n = 0
    for pid in pids:
        h = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not h:
            continue
        try:
            if _kernel32.TerminateProcess(h, 1):
                n += 1
        finally:
            _kernel32.CloseHandle(h)
    return n


# --------------------------------------------------------------------------
# 启动 / 等待 / 关闭
# --------------------------------------------------------------------------

def spawn(exe: Path, args: list[str], detached: bool = True) -> subprocess.Popen:
    """拉起进程。detached=True 时脱离本进程树（本程序退出不带走游戏）。"""
    flags = 0
    if detached:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：不共享控制台、不受本进程退出牵连
        flags = 0x00000008 | 0x00000200
    return subprocess.Popen(
        [str(exe), *args],
        cwd=str(exe.parent),
        creationflags=flags,
        close_fds=True,
    )


def wait_for_window(exe_name: str, timeout_s: float, stop_event=None) -> int:
    """轮询等待游戏窗口出现，返回 hwnd；超时/被停止返回 0。

    每 0.5s 查一次：窗口出现是秒级事件，更密的轮询只是空耗 CPU。
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return 0
        hwnd = find_window_by_exe(exe_name)
        if hwnd:
            return hwnd
        time.sleep(0.5)
    return 0


def close_game(hwnd: int = 0, exe_name: str = "") -> str:
    """关闭游戏：优先按窗口句柄结束进程，找不到窗口时按进程名兜底。

    返回人类可读的结果串（供调用方写日志），失败不抛异常——关闭失败要能在日志里
    看见原因，而不是把整轮打成异常退出。
    """
    if hwnd:
        try:
            terminate_process_by_hwnd(hwnd)
            return "游戏进程已关闭"
        except Exception as exc:  # noqa: BLE001
            logger.log(f"[game] 按窗口关闭失败，改按进程名兜底: {exc!r}", "WARNING")
    if exe_name:
        n = terminate_by_exe(exe_name)
        if n:
            return f"游戏进程已关闭（按进程名结束 {n} 个）"
        return "未找到运行中的游戏进程"
    return "未找到游戏窗口，跳过关闭"
