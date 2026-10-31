#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置步骤：不依赖任何插件、由宿主直接执行的任务项。

**为什么不是活动模块**：活动模块的契约是「阶段 / 断点 / 有效期 / 性能行 / 自带资源」，
启动与关闭游戏一样都没有。硬塞进活动模块会得到零阶段的空壳，还会让任务卡上出现
永远空着的阶段进度。步骤只需要两件事：**跑一次**、**可被停止**。

**也不是插件**：进程启停不属于任何玩法，它是平台对"游戏"这个对象的操作。插件反过来
操纵宿主窗口句柄会把分层方向拧反。

步骤与活动模块共用同一条任务列表：前端表现完全一致（同样的卡、同样的拖拽与勾选），
只是步骤没有细分阶段、配置面板由宿主自己渲染。
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from maaracing_master.core import game_process


class BuiltinStep:
    """内置步骤契约：一个 id、一份配置面、一次执行。"""

    ID = ""
    NAME = ""
    # 配置面（键 → 默认值）。与活动模块的 DEFAULT_MODULE_CONFIG 同语义：
    # 既是 GUI 回填初值的来源，也是持久化白名单。
    DEFAULT_CONFIG: dict = {}
    # 封面的文件名（相对 core/resources/，如 "cover_launch_game.jpg"）。
    # 与 ActivityModule.COVER 同语义：只声明，不声明磁盘位置（解析在 core/covers.py）。
    # 步骤属于核心，所以图放核心资源目录，而不是某个插件的 resources/。
    COVER: str = ""

    def __init__(self, app):
        self.app = app  # MaaRacingMasterController（宿主）

    @classmethod
    def required_capabilities(cls, config: dict | None = None) -> frozenset[str]:
        """步骤不依赖任何可选能力（不需要截图/手柄），恒为空集。"""
        return frozenset()

    def run(self, config: dict, stop_event, log) -> bool:
        """执行一次。返回 True = 正常完成；False = 未完成（会被判为失败并中止整轮）。"""
        raise NotImplementedError


class LaunchGameStep(BuiltinStep):
    """启动游戏：直接拉起游戏主程序，不经过官方启动器。"""

    ID = "launch_game"
    NAME = "启动游戏"
    COVER = "cover_launch_game.jpg"
    DEFAULT_CONFIG = {
        "path": "",                                   # 空 = 从启动器配置自动探测
        "args": " ".join(game_process.DEFAULT_ARGS),  # 与官方启动器实测命令行一致
        "timeout_s": 120,                             # 等游戏窗口的上限
        "skip_if_running": True,                      # 已在运行就不再拉第二个
        "detached": True,                             # 脱离本程序进程树
        "fallback_launcher": True,                    # 等窗口超时后改走官方启动器
    }

    def _resolve_exe(self, cfg: dict, log) -> Path | None:
        raw = str(cfg.get("path") or "").strip()
        if raw:
            exe = Path(raw.strip('"'))
            if exe.is_file():
                return exe
            log(f"指定的程序不存在: {exe}，改从启动器配置探测", "WARNING")
        exe = game_process.detect_game_exe()
        if exe is None:
            log("未能定位游戏主程序：请填写程序路径，或先用官方启动器安装一次游戏", "ERROR")
        return exe

    def run(self, config: dict, stop_event, log) -> bool:
        cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        exe = self._resolve_exe(cfg, log)
        if exe is None:
            return False
        timeout_s = float(cfg.get("timeout_s") or 120)

        if bool(cfg.get("skip_if_running")) and game_process.game_running(exe.name):
            hwnd = game_process.find_window_by_exe(exe.name)
            log("游戏已在运行，跳过启动")
            if hwnd:
                self.app.adopt_window(hwnd)
                log(f"已接管现有游戏窗口 (hWnd={hwnd})")
            return True

        args = _split_args(str(cfg.get("args") or ""))
        t0 = time.monotonic()
        try:
            proc = game_process.spawn(exe, args, detached=bool(cfg.get("detached", True)))
        except OSError as exc:
            log(f"启动失败: {exc}", "ERROR")
            return False
        log(f"已拉起 {exe.name} (pid={proc.pid})，等待游戏窗口（最长 {timeout_s:.0f}s）…")

        hwnd = game_process.wait_for_window(exe.name, timeout_s, stop_event)
        if not hwnd and bool(cfg.get("fallback_launcher")):
            launcher = game_process.detect_launcher_exe()
            if launcher is None:
                log("未找到官方启动器，无法回退", "WARNING")
            else:
                log(f"等窗口超时，改走官方启动器（会先做版本校验与更新）: {launcher}")
                try:
                    game_process.spawn(launcher, [], detached=bool(cfg.get("detached", True)))
                except OSError as exc:
                    log(f"启动器拉起失败: {exc}", "ERROR")
                hwnd = game_process.wait_for_window(exe.name, timeout_s, stop_event)

        if stop_event is not None and stop_event.is_set():
            log("已停止，放弃等待游戏窗口", "WARNING")
            return False
        if not hwnd:
            log(f"{timeout_s:.0f}s 内未等到游戏窗口；{_diagnose(exe)}", "ERROR")
            return False

        self.app.adopt_window(hwnd)
        log(f"已找到游戏窗口 (hWnd={hwnd})，用时 {time.monotonic() - t0:.0f}s")
        return True


class CloseGameStep(BuiltinStep):
    """关闭游戏：结束游戏进程，可选随后退出本程序。"""

    ID = "close_game"
    NAME = "关闭游戏"
    COVER = "cover_close_game.jpg"
    DEFAULT_CONFIG = {
        "exit_app": False,  # 这一步完成后退出本程序（后面的步骤不再执行）
    }

    def run(self, config: dict, stop_event, log) -> bool:
        cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        exe = game_process.detect_game_exe()
        exe_name = exe.name if exe else ""
        hwnd = self.app.hwnd or (game_process.find_window_by_exe(exe_name) if exe_name else 0)
        if hwnd:
            log(f"正在关闭游戏进程 (hwnd={hwnd})…")
        result = game_process.close_game(hwnd, exe_name)
        log(result, "INFO" if "已关闭" in result else "WARNING")
        # 关掉后窗口句柄即失效：清掉连接态，下一次运行会重新查找窗口
        self.app.forget_window()
        if bool(cfg.get("exit_app")):
            log("按本步骤设置，本轮结束后退出本程序")
            self.app.request_exit()
        return True


# 注册表：id → 类。core 侧只认这两条边，不认识任何插件。
BUILTIN_STEPS: dict[str, type[BuiltinStep]] = {
    LaunchGameStep.ID: LaunchGameStep,
    CloseGameStep.ID: CloseGameStep,
}


def _split_args(text: str) -> list[str]:
    """把界面上的参数字符串拆成 argv（Windows 口径：保留引号内的空格）。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        return [a.strip('"') for a in shlex.split(text, posix=False)]
    except ValueError:
        return text.split()


def _diagnose(exe: Path) -> str:
    """超时诊断：把"当时到底有没有进程、有没有窗口"记下来，比事后猜省事。"""
    alive = game_process.game_running(exe.name)
    hwnd = game_process.find_window_by_exe(exe.name) if alive else 0
    if not alive:
        return f"{exe.name} 进程不存在（可能启动即退出，检查是否被反外挂或权限拦下）"
    if not hwnd:
        return f"{exe.name} 进程在运行但还没有可见窗口"
    return f"{exe.name} 进程与窗口都在 (hWnd={hwnd})"
