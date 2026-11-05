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

**启动链（ADR-0018）**：「启动游戏」的完成判据不是"窗口出现"，而是"游戏被交付到可
用状态"。真源住 `core/resources/startup/`，由本步骤显式加载——不放进
`core/resources/pipeline/`，那个目录会被所有模块的导航图自动并入。资源需求由本步骤
自己声明（`required_resources`），业务结果由本步骤自己判定，两者不互相推导。

**配置面只放用户能决定的事**：怎么起进程、起不来时怎么办、窗口怎么认，这些是步骤
自己的实现，不进面板；面板上留路径、参数与两个时限。
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from maaracing_master.core import game_process

_CORE_RES_DIR = Path(__file__).resolve().parent / "resources"
STARTUP_PIPELINE = _CORE_RES_DIR / "startup" / "pipeline.json"
STARTUP_IMAGE_DIR = _CORE_RES_DIR / "startup" / "image"
STARTUP_ENTRY = "startup.boot"
STARTUP_DONE = "startup.done"


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

    def __init__(self, app, grp=None):
        self.app = app  # MaaRacingMasterController（宿主）
        self.grp = grp  # 本步骤的日志组（connect / 采集等宿主行挂到这里）
        # 业务结果：completed / skipped / stopped / failed。
        # 与导航结局（reached/stopped/deadline/...）分层——`skipped` 这类业务判定
        # 不能由导航层产生，因为 intent 模式下根本不会建图。
        self.last_outcome: str = "failed"

    @classmethod
    def required_capabilities(cls, config: dict | None = None) -> frozenset[str]:
        """步骤不依赖任何可选能力（不需要截图/手柄），恒为空集。"""
        return frozenset()

    def required_resources(self, config: dict) -> frozenset[str]:
        """本次执行需要宿主准备哪些资源（game_link / wgc_capture / gamepad）。

        空集 = 不建立任何游戏链路。**只回答"要不要准备资源"**，不决定业务结果：
        仅显示意图既不建链路，也不算完成。
        """
        return frozenset()

    def run(self, config: dict, stop_event, log, session) -> bool:
        """执行一次。返回 True = 正常完成；False = 未完成（会被判为失败并中止整轮）。

        session：`StepSession`，会话独占导航图、宿主资源归控制器。不需要重资源的
        步骤可以完全不碰它。
        """
        raise NotImplementedError


class LaunchGameStep(BuiltinStep):
    """启动游戏：拉起游戏主程序，再一路导航到可交付的大厅状态。"""

    ID = "launch_game"
    NAME = "启动游戏"
    COVER = "cover_launch_game.jpg"
    DEFAULT_CONFIG = {
        "path": "",                                   # 空 = 从启动器配置自动探测
        "args": " ".join(game_process.DEFAULT_ARGS),  # 与官方启动器实测命令行一致
        "timeout_s": 120,                             # 等游戏窗口的上限
        "hall_timeout_s": 300,                        # 进入大厅的总时限
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

    def _hall_enabled(self) -> bool:
        """本次是否要执行大厅链路。资源声明与结果判定共用这一个判据，防两处漂移。"""
        return not self.app.intent_mode

    def required_resources(self, cfg: dict) -> frozenset[str]:
        if not self._hall_enabled():
            return frozenset()
        need = {"game_link", "wgc_capture"}
        if self.app.click_mode == "gamepad":
            # 登录页要按手柄键：提前暴露 ViGEmBus 缺失，而不是按到一半才失败。
            need.add("gamepad")
        return frozenset(need)

    def run(self, config: dict, stop_event, log, session) -> bool:
        cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        exe = self._resolve_exe(cfg, log)
        if exe is None:
            self.last_outcome = "failed"
            return False
        timeout_s = float(cfg.get("timeout_s") or 120)

        if not self._ensure_window(cfg, exe, timeout_s, stop_event, log):
            self.last_outcome = "failed"
            return False

        if not self._hall_enabled():
            log("仅显示意图不会自动确认，已跳过进入大厅", "WARNING")
            self.last_outcome = "skipped"
            return True

        return self._enter_hall(session, float(cfg.get("hall_timeout_s") or 300),
                                stop_event, log)

    def _ensure_window(self, cfg: dict, exe: Path, timeout_s: float,
                       stop_event, log) -> bool:
        """把游戏窗口准备好并交给连接层。返回 False = 窗口没拿到。"""
        t0 = time.monotonic()
        if game_process.game_running(exe.name):
            log("游戏已在运行，跳过启动")
            hwnd = game_process.find_window_by_exe(exe.name)
            if not hwnd:
                # 进程在但窗口还没出（刚双击完、还在加载）：等一轮再交给连接层，
                # 否则下一步只报「链路建立失败」，看不出真因是没有窗口。
                hwnd = game_process.wait_for_window(exe.name, timeout_s, stop_event)
        else:
            hwnd = self._launch_and_wait(cfg, exe, timeout_s, stop_event, log)

        if stop_event is not None and stop_event.is_set():
            log("已停止，放弃等待游戏窗口", "WARNING")
            return False
        if not hwnd:
            log(f"{timeout_s:.0f}s 内未等到游戏窗口；{_diagnose(exe)}", "ERROR")
            return False

        self.app.adopt_window(hwnd)
        log(f"已找到游戏窗口 (hWnd={hwnd})，用时 {time.monotonic() - t0:.0f}s")
        return True

    def _launch_and_wait(self, cfg: dict, exe: Path, timeout_s: float,
                         stop_event, log) -> int:
        """直启主程序并等窗口；超时改走官方启动器再等一轮。返回 hwnd（0 = 没拿到）。"""
        args = _split_args(str(cfg.get("args") or ""))
        try:
            proc = game_process.spawn(exe, args)
        except OSError as exc:
            log(f"启动失败: {exc}", "ERROR")
            return 0
        log(f"已拉起 {exe.name} (pid={proc.pid})，等待游戏窗口（最长 {timeout_s:.0f}s）…")

        hwnd = game_process.wait_for_window(exe.name, timeout_s, stop_event)
        if not hwnd:
            # 直启跳过了版本校验：游戏更新后首次可能起不来，这一跳由官方启动器补上
            launcher = game_process.detect_launcher_exe()
            if launcher is None:
                log("未找到官方启动器，无法回退", "WARNING")
            else:
                log(f"等窗口超时，改走官方启动器（会先做版本校验与更新）: {launcher}")
                try:
                    game_process.spawn(launcher, [])
                except OSError as exc:
                    log(f"启动器拉起失败: {exc}", "ERROR")
                hwnd = game_process.wait_for_window(exe.name, timeout_s, stop_event)
        return hwnd

    def _enter_hall(self, session, timeout_s: float, stop_event, log) -> bool:
        """跑启动链到终点。结局映射见 ADR-0018「结果分层」。"""
        if stop_event is not None and stop_event.is_set():
            log("已停止，不再进入大厅", "WARNING")
            self.last_outcome = "stopped"
            return False
        t0 = time.monotonic()
        log(f"开始进入游戏大厅（最多 {timeout_s:.0f}s）")
        if not session.start():
            # 具体原因（窗口连接失败 / 采集没起来）已由会话逐条记过，这里补一条
            # 能一眼看出"本步骤为什么没往下走"的总括行。
            log("游戏链路建立失败，无法进入大厅", "ERROR")
            self.last_outcome = "failed"
            return False
        graph = session.graph(STARTUP_PIPELINE, STARTUP_IMAGE_DIR)
        ok = graph.run(STARTUP_ENTRY, reached=STARTUP_DONE, deadline_s=timeout_s)
        outcome = graph.last_outcome or "task_failed"
        elapsed = time.monotonic() - t0

        if ok and outcome == "reached":
            log(f"已进入游戏大厅（用时 {elapsed:.0f}s）")
            self.last_outcome = "completed"
            return True
        if outcome == "stopped":
            log("已停止：未完成进入大厅", "WARNING")
            self.last_outcome = "stopped"
            return False
        if outcome == "deadline":
            log(f"{timeout_s:.0f}s 内未进入游戏大厅：可能停在登录页、未识别的弹窗或"
                "对局中。界面改版导致这里过不去时，临时把本步骤移出任务列表——手动把"
                "游戏点进大厅后，后续模块照常从大厅起跑", "ERROR")
        elif outcome == "load_failed":
            log("启动链加载失败：检查 core/resources/startup 下的真源与模板是否完整", "ERROR")
        else:
            log(f"进入大厅失败：{outcome}", "ERROR")
        self.last_outcome = "failed"
        return False


class CloseGameStep(BuiltinStep):
    """关闭游戏：结束游戏进程，可选随后退出本程序。"""

    ID = "close_game"
    NAME = "关闭游戏"
    COVER = "cover_close_game.jpg"
    DEFAULT_CONFIG = {
        "exit_app": False,  # 这一步完成后退出本程序（后面的步骤不再执行）
    }

    def run(self, config: dict, stop_event, log, session) -> bool:
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
        self.last_outcome = "completed"
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
