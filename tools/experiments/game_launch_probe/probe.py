#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直启游戏主程序的观测探针（自包含，不 import 仓库业务代码）。

做四件事：
  1. 记录启动前的窗口/进程基线；
  2. 直接拉起游戏主程序（不经官方启动器），脱离本进程树；
  3. 逐秒记录：新出现的窗口（标题/类名/尺寸/所属进程）、目标进程与反外挂进程的出现与消失；
  4. 判定"游戏窗口"是否出现、用了多久、标题是否命中现有窗口查找关键词，结果写 result.json。

不点击、不抢焦点、不改窗口大小。跑完游戏仍在运行。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

GAME_EXE = Path(r"E:\DFJS\g112\Binaries\Win64\g112-Win64-Shipping.exe")
GAME_ARGS = ["-nosound", "-nohmd", "--MuMuLauncher", "-noexceptionhandler", "-Hdd", "-force_load_content"]
# 现有窗口查找用的标题关键词（core/window_utils.py），命中与否决定要不要改匹配逻辑
TITLE_KEYWORDS = ("巅峰极速", "g112", "Racing Master")
# 关心的旁路进程（反外挂 / 启动器 / UE 前置壳）
WATCH_PROC = ("g112", "ace", "anticheat", "launcher")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_of_pid(pid: int) -> str:
    """进程可执行文件名（失败返回空串）。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_windows() -> list[dict]:
    """当前可见且有标题的顶层窗口。"""
    out: list[dict] = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append({
            "hwnd": int(hwnd),
            "title": title.value,
            "class": cls.value,
            "pid": int(pid.value),
            "exe": _exe_of_pid(int(pid.value)),
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
        })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def list_procs() -> list[dict]:
    """枚举进程（名字 + pid + 父 pid），只保留关心的那些。"""
    import ctypes.wintypes as w

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", w.DWORD),
            ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    out: list[dict] = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            name = e.szExeFile
            if any(k in name.lower() for k in WATCH_PROC):
                out.append({"name": name, "pid": int(e.th32ProcessID),
                            "ppid": int(e.th32ParentProcessID)})
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=180.0, help="等窗口的上限秒数")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cmd = [str(GAME_EXE), *GAME_ARGS]
    print("命令:", subprocess.list2cmdline(cmd))
    print("工作目录:", GAME_EXE.parent)
    if args.dry_run:
        return 0
    if not GAME_EXE.is_file():
        print("主程序不存在:", GAME_EXE)
        return 2

    t0 = time.time()
    base_windows = {w["hwnd"] for w in list_windows()}
    base_procs = {p["pid"] for p in list_procs()}
    print(f"基线: 可见窗口 {len(base_windows)} 个, 关心的进程 {len(base_procs)} 个")

    # DETACHED_PROCESS(0x8) | CREATE_NEW_PROCESS_GROUP(0x200)：脱离本进程树
    proc = subprocess.Popen(cmd, cwd=str(GAME_EXE.parent),
                            creationflags=0x00000008 | 0x00000200,
                            close_fds=True)
    events: list[dict] = [{"t": 0.0, "kind": "launched", "pid": proc.pid}]
    print(f"已拉起 pid={proc.pid}")

    seen_win: dict[int, dict] = {}
    seen_proc: dict[int, str] = {}
    game_window = None
    game_pid = proc.pid
    deadline = t0 + args.timeout
    while time.time() < deadline:
        time.sleep(1.0)
        t = round(time.time() - t0, 1)

        for w in list_windows():
            if w["hwnd"] in base_windows or w["hwnd"] in seen_win:
                continue
            w["t"] = t
            seen_win[w["hwnd"]] = w
            hit = any(k.lower() in w["title"].lower() for k in TITLE_KEYWORDS)
            events.append({"t": t, "kind": "window", "hit_keyword": hit, **w})
            print(f"  [{t:6.1f}s] 新窗口 hwnd={w['hwnd']} pid={w['pid']} exe={w['exe']!r} "
                  f"title={w['title']!r} class={w['class']!r} size={w['rect'][2]}x{w['rect'][3]} "
                  f"关键词命中={hit}")
            if w["exe"].lower() == GAME_EXE.name.lower() and w["rect"][2] > 320:
                game_window = w
                events.append({"t": t, "kind": "game_window_found", **w})
                print(f"  [{t:6.1f}s] → 判定为游戏窗口")
                break

        for p in list_procs():
            if p["pid"] in base_procs or p["pid"] in seen_proc:
                continue
            seen_proc[p["pid"]] = p["name"]
            events.append({"t": t, "kind": "proc", **p})
            print(f"  [{t:6.1f}s] 新进程 {p['name']} pid={p['pid']} ppid={p['ppid']}")

        if game_window:
            # 再观察 8 秒，看有没有第二个窗口（登录 / 公告 / 反外挂提示）跟上
            time.sleep(8.0)
            for w in list_windows():
                if w["hwnd"] in seen_win:
                    continue
                seen_win[w["hwnd"]] = w
                events.append({"t": round(time.time() - t0, 1), "kind": "window_late", **w})
                print(f"  [+8s] 追加窗口 exe={w['exe']!r} title={w['title']!r}")
            break

    elapsed = round(time.time() - t0, 1)
    alive = proc.poll() is None
    result = {
        "started_at": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
        "cmd": subprocess.list2cmdline(cmd),
        "cwd": str(GAME_EXE.parent),
        "elapsed_s": elapsed,
        "launched_pid": proc.pid,
        "still_running": alive,
        "game_window": game_window,
        "time_to_game_window_s": (game_window and round(game_window.get("t", 0), 1)),
        "events": events,
    }
    out = HERE / "result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 结论 ===")
    if game_window:
        print(f"游戏窗口在 {result['time_to_game_window_s']}s 出现：{game_window['title']!r} "
              f"{game_window['rect'][2]}x{game_window['rect'][3]}，进程 {game_window['exe']}")
        print("标题关键词命中:", any(k.lower() in game_window["title"].lower() for k in TITLE_KEYWORDS))
    else:
        print(f"等了 {args.timeout:.0f}s 没有出现游戏窗口（进程仍在运行: {alive}）")
    print("时间线写入:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
