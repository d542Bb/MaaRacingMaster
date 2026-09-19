#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaaRM Python sidecar —— 唯一业务后端（stdin/stdout JSONL RPC）。

通道契约（与 Step 2 契约测试一致）：
    stdin  = JSONL request only
    stdout = JSONL response/event only（协议专用，入口处强制隔离 print）
    stderr = diagnostics/log only

线程模型：
    main thread → stdin reader（逐行读，立即派发，绝不阻塞）
    handler     → 每个 request 独立线程执行；start 只做校验 + 起 worker，立即响应
    response    → stdout 带写锁

方法（JSONL RPC，供 MaaRacingMaster.Shell.exe 前端调用）：
    get_initial_state / select_module / get_status / start / stop / fetch_logs / close / shutdown
    get_debug_state / set_debug_mode / set_peep / set_click_mode（调试页）
    get_registry_optimizations / set_registry_optimization（启动体检：注册表权限优化中心）
    set_optimization_prompt_ignored（按项忽略/恢复启动提醒，profile 持久化）

运行：python -u -m maaracing_master.core.sidecar
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any, TextIO, cast

from maaracing_master import __display_version__, __version__
from maaracing_master.core import opencv_utf8_patch  # noqa: F401  中文路径读写兼容，须先于任何 cv2 存图生效
from maaracing_master.core.controller import MaaRacingMasterController
from maaracing_master.core.logger import logger
from maaracing_master.core.registry import (
    MODULE_REGISTRY,
    check_required_assets,
    first_available_module_id,
    get_module_info,
    module_expired,
)
from maaracing_master.core.paths import config_dir, user_data_dir
from maaracing_master.core.remote_meta import (
    EXTERNAL_TARGETS,
    GITHUB_REPO,
    is_expired,
    is_openable_url,
    parse_announcement,
    parse_release,
)
from maaracing_master.core.window_utils import ensure_dpi_aware, has_physical_controller

# 协议转移用 stderr：_StdoutGuard 把误写 stdout 的第三方 print 转移到这里。
# sys.__stderr__ 类型上可为 None，但运行期解释器必有该流；cast 后复用同一引用。
_STDERR = cast(TextIO, sys.__stderr__)

# --------------------------------------------------------------------------
# 用户偏好持久化（profile）：%APPDATA%/MaaRacingMaster/config/profile.json
# 只写/读本程序自己管理的键；文件里出现未知类/键一律忽略，绝不因此崩溃。
# --------------------------------------------------------------------------
_PROFILE_FILENAME = "profile.json"
# 默认选中模块 id（仅作 id 引用，不直接 import 插件包；GUI 进入默认展示鉴宝）
_DEFAULT_MODULE_ID = "treasure"
# 模块配置的键白名单不在这里定义：它由各模块类的 DEFAULT_MODULE_CONFIG 声明
# （配置面由模块自述，见 ActivityModule 的契约）。core 不得出现任何模块的字段名
# ——否则每加一个配置项都要回头改这里。

# 注册表权限优化项注册表（数据驱动：新增优化项只改这里，前后端体检/设置页自动生效）。
# 字段语义：
#   kind = "dword"（写 DWORD 值，单 path）
#        | "noopenwith"（写/删 NoOpenWith REG_SZ，paths 多路径）
#        | "protocol_command"（给孤儿协议补空 shell\open\command 处理程序 + NoOpenWith 双保险，
#          paths 多路径；恢复时删 NoOpenWith 并逐层删回 shell 子树——实测 NoOpenWith 单独
#          写入管不住「协议已注册但无处理程序 → 弹 Store 推荐」的路径，空 command 才是阻断键）。
#   optimized = 期望写入的"优化值"（本程序推荐态）；default = 系统默认值（值缺失时按此判定）。
#   options/apply_label/restore_label = 前端展示文案（不同 kind 的可选值语义不同，由后端下发）。
#   needs_admin = 写入是否需要管理员（HKLM 项）；发布版 sidecar 经 MaaRacingMaster.Shell UAC 提权可写，
#   开发模式非管理员终端写入会失败并返回明确错误。
#   appx_absent = 仅 protocol_command：目标应用（如 Microsoft.XboxGamingOverlay）包注册
#   存在时该项"无需优化"（协议有真实处理程序不会弹窗，写空 command 反而拦截正常唤起），
#   前端按 available 字段展示，体检自动跳过。
_REGISTRY_OPTIMIZATIONS = (
    {
        "id": "gamedvr_appcapture",
        "kind": "dword",
        "name": "Xbox 后台捕获（ms-gamebar 弹窗）",
        "effect": "关闭 Xbox Game Bar 后台捕获，杜绝游戏启动时 ms-gamebar 窗口抢焦点",
        "hive": "HKCU",
        "path": r"SOFTWARE\Microsoft\Windows\CurrentVersion\GameDVR",
        "value_name": "AppCaptureEnabled",
        "optimized": 0,
        "default": 1,
        "needs_admin": False,
        "options": {"0": "关闭（推荐）", "1": "开启（系统默认）"},
        "apply_label": "优化（写 0）",
        "restore_label": "恢复系统默认（写 1）",
        "detail": "写 0 后 Game Bar 后台录制停用；Win+G 手动打开不受影响；不涉及 Xbox 服务。",
    },
    {
        "id": "controller_nav",
        "kind": "dword",
        "name": "手柄 UI 导航（打字弹手柄虚拟键盘）",
        "effect": "关闭 Windows「手柄→界面按键」映射，杜绝文本框聚焦时自动弹手柄虚拟键盘",
        "hive": "HKLM",
        "path": r"SOFTWARE\Microsoft\Input\Settings\ControllerProcessor\ControllerToVKMapping",
        "value_name": "Enabled",
        "optimized": 0,
        "default": 1,
        "needs_admin": True,
        "options": {"0": "关闭（推荐）", "1": "开启（系统默认）"},
        "apply_label": "优化（写 0）",
        "restore_label": "恢复系统默认（写 1）",
        "detail": "写 0 后开始菜单/任务栏等 Windows 界面不再响应手柄导航；游戏内手柄操作不受影响；"
                  "想恢复手柄操作 Windows 可改回 1。",
    },
    {
        "id": "msgamebar_protocol",
        "kind": "protocol_command",
        "name": "ms-gamebar 协议弹窗（获取应用对话框）",
        "effect": "为已卸载的 Xbox Game Bar 的孤儿协议补空处理程序，杜绝 Win11 游戏运行中"
                  "点击任务栏游戏图标时弹『获取打开此 ms-gamebar 链接的应用』",
        "hive": "HKCU",
        "path": r"Software\Classes\ms-gamebar",
        "paths": (
            r"Software\Classes\ms-gamebar",
            r"Software\Classes\ms-gamebarservices",
        ),
        "value_name": "NoOpenWith",
        "optimized": 0,
        "default": 1,
        "needs_admin": False,
        "appx_absent": "Microsoft.XboxGamingOverlay",
        "options": {"0": "屏蔽弹窗（推荐）", "1": "恢复系统默认"},
        "apply_label": "屏蔽弹窗（补空处理程序）",
        "restore_label": "恢复默认（会弹窗）",
        "detail": "Win11 会把游戏窗口的任务栏图标点击当作『激活游戏』，并经 ms-gamebar: 协议"
                  "尝试拉起 Game Bar；Game Bar 卸载后协议注册残留（只有 URL Protocol、无"
                  " shell\\open\\command 处理程序），于是弹『获取应用』对话框（与手柄导航无关，"
                  "关闭手柄导航仍会触发）。补空 shell\\open\\command 后协议调用静默结束"
                  "（NoOpenWith 一并写入双保险），恢复默认即删除上述写入；重新安装 Game Bar"
                  " 后本项自动显示为无需处理。",
    },
)


def _read_reg_dword(hive: str, path: str, value_name: str):
    """读注册表 DWORD 值；键/值缺失返回 None，其他 OSError 抛给调用方处理。"""
    try:
        import winreg
    except ImportError:
        return None  # 非 Windows：调用方按"无优化项"处理
    h = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(h, path) as key:
            value, _vtype = winreg.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return None
    return value


def _reg_value_exists(hive: str, path: str, value_name: str) -> bool:
    """检查注册表值是否存在（用于 NoOpenWith 等空 REG_SZ 标记值）。"""
    try:
        import winreg
    except ImportError:
        return False
    h = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(h, path) as key:
            winreg.QueryValueEx(key, value_name)
    except (FileNotFoundError, OSError):
        return False
    return True


def _protocol_command_exists(hive: str, path: str) -> bool:
    """协议是否已有 shell\\open\\command 处理程序（子键存在即算，值可为空串=空处理程序）。"""
    try:
        import winreg
    except ImportError:
        return False
    h = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(h, path + r"\shell\open\command"):
            pass
    except (FileNotFoundError, OSError):
        return False
    return True


def _delete_protocol_shell_tree(hive: str, path: str) -> None:
    """删除协议键下我们补的 shell\\open\\command 子树（自底向上，缺层幂等）。

    安全阀：command 默认值非空 = 存在真实处理程序（可能非本程序所写），不删。
    """
    try:
        import winreg
    except ImportError:
        return
    h = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    cmd_path = path + r"\shell\open\command"
    try:
        with winreg.OpenKey(h, cmd_path) as key:
            default_value, _vtype = winreg.QueryValueEx(key, "")
    except (FileNotFoundError, OSError):
        return  # 子树不存在（或读不到）= 已是目标状态
    if default_value:
        return
    for sub in (cmd_path, path + r"\shell\open", path + r"\shell"):
        try:
            winreg.DeleteKey(h, sub)
        except (FileNotFoundError, OSError):
            pass


# UWP 包注册探测点：HKCU 当前用户已注册包仓库 = Get-AppxPackage 的同源数据，
# 也是协议处理程序的 per-user 解析依据。不能用 HKLM AppxAllUserStore\Applications：
# 那是 staged 清单，用户 Remove-AppxPackage 后仍留残留条目，会把已卸载误判为已安装。
_APPX_PROBE_PATHS = (
    ("HKCU", r"Software\Classes\Local Settings\Software\Microsoft\Windows"
             r"\CurrentVersion\AppModel\Repository\Packages"),
)


def _appx_package_registered(package_id_prefix: str) -> bool:
    """按包族名前缀（如 Microsoft.XboxGamingOverlay）探测 UWP 包是否已注册/安装。"""
    try:
        import winreg
    except ImportError:
        return False
    for hive, path in _APPX_PROBE_PATHS:
        h = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        try:
            with winreg.OpenKey(h, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    if winreg.EnumKey(key, i).startswith(package_id_prefix + "_"):
                        return True
        except (FileNotFoundError, OSError):
            continue  # 探测点缺失（老系统/精简系统）→ 换下一个探测点
    return False


def _get_ignored_prompt_ids() -> list:
    """已忽略启动提醒的优化项 id 列表（profile 持久化）。按项忽略：新增优化项不受影响。"""
    ids = _load_profile().get("ignored_optimization_prompts")
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def _set_ignored_prompt_ids(ids: list) -> None:
    _save_profile({"ignored_optimization_prompts": ids})


def _profile_path() -> Path:
    """profile 文件路径：配置目录（%APPDATA%/MaaRacingMaster/config/ 下）。"""
    return config_dir() / _PROFILE_FILENAME


def _load_profile() -> dict:
    """安全读取 profile：文件缺失/损坏/非 dict/IO 异常，一律返回空 dict，不抛异常。"""
    try:
        with open(_profile_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 容错：读不了就当作无偏好
        return {}


def _save_profile(partial: dict) -> None:
    """原子写 profile：仅更新 partial 提供的段，其余段（含未知键）原样保留。

    写失败不抛异常（只记警告），避免干扰主流程。
    """
    try:
        merged = _load_profile()            # 先并合再写，避免覆盖其它写入的数据
        merged.update(partial)
        path = _profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        tmp.replace(path)                   # 原子替换，防半写文件
    except Exception as exc:  # noqa: BLE001
        logger.log(f"[sidecar] 偏好落盘失败: {exc!r}", "WARNING")


def _parse_module_config_slots(mc: object) -> dict[str, dict]:
    """把 profile 的 module_config 段解析成「模块 id → 配置 dict」的多槽形态。

    兼容两种格式，判别依据是段内是否存在**字符串值**的 ``module_id`` 键：
      - 新：``{"treasure": {"max_daily_loops": 50}, "speedrush": {...}}``
      - 旧（单槽 flat）：``{"module_id": "treasure", "max_daily_loops": 50}``

    无法归槽的内容（类型不符、键不是字符串）一律丢弃：多槽下"猜一个模块替它收下"
    会把 A 模块的字段名塞进 B 模块的槽，不收比收错好。
    """
    if not isinstance(mc, dict):
        return {}
    mid = mc.get("module_id")
    if isinstance(mid, str):
        return {mid: {k: v for k, v in mc.items() if k != "module_id"}}
    return {k: dict(v) for k, v in mc.items() if isinstance(k, str) and isinstance(v, dict)}


def _module_config_defaults(module_id: str) -> dict:
    """模块声明的配置面：键集合与默认值（``DEFAULT_MODULE_CONFIG``，真源在模块类）。

    **静态读取，不创建实例**：读配置不得执行模块代码——否则"看一眼配置"就会跑一遍
    模块的 ``__init__``，而"构造函数里有副作用（起线程、开文件）"本身是合理的事。
    键白名单取自这里，所以新增配置项只要写进模块的声明就自动可持久化。
    """
    cls = MODULE_REGISTRY.get(module_id)
    defaults = getattr(cls, "DEFAULT_MODULE_CONFIG", None)
    return dict(defaults) if isinstance(defaults, dict) else {}


def _merge_module_config(cache_cfg: dict | None, param_cfg: dict | None) -> dict:
    """start 时的配置合并：缓存槽 ← 本次带参（带参优先）。

    单独成函数是为了让"配置来源的优先级"只有一个落点、可被直接验证，
    不然它散在 start 的启动流程里，只能靠跑整条 start 才间接覆盖。
    """
    merged: dict = dict(cache_cfg or {})
    merged.update(param_cfg or {})
    return merged


class _StdoutGuard:
    """把一切误写 stdout 的输出（第三方库 print 等）转移到 stderr，保证协议通道纯净。"""

    def __init__(self, protocol_stdout):
        self._protocol = protocol_stdout

    def write(self, s: str) -> None:
        _STDERR.write(s)
        _STDERR.flush()

    def flush(self) -> None:
        _STDERR.flush()


# ---------- RPC 信任边界 ----------
# 前端可调方法白名单。_dispatch 只查本表，不做属性反射——WebView 内任意 JS
# 不得触达服务对象的其余成员（含 shutdown 之外的全部内部方法与未来新增成员）。
# 新增 RPC 必须同时改三处：本表、SidecarService 方法、前端与 C# 侧常量表
# （apps/MaaRacingMaster.Shell/RpcBridge.cs AllowedMethods）。
HANDLERS = frozenset({
    # 生命周期与模块
    "get_initial_state", "select_module", "get_status", "start", "stop",
    "close", "shutdown",
    # 模块配置
    "get_module_config", "set_module_config",
    # 外部资源与下载引导
    "open_vigembus_download", "open_external_url", "open_user_data_folder",
    # 注册表优化建议
    "get_registry_optimizations", "set_optimization_prompt_ignored",
    "set_registry_optimization",
    # 更新与公告
    "check_update", "fetch_announcement",
    # 观测
    "fetch_logs", "get_today_stats", "get_debug_state",
    # 调试与显示开关
    "set_debug_mode", "set_file_logging", "set_peep", "get_peep_frame",
    # 行为开关
    "set_emergency_stop", "set_click_mode", "set_intent_mode",
    "set_auto_close_game", "set_auto_exit_mra", "set_mute_game",
})

# 在途请求并发上限：stdin reader 永不阻塞是协议铁律，超限直接拒（前端按
# 普通错误处理并下轮重试），不排队——排队会让 reader 等锁、违背同一铁律。
_MAX_INFLIGHT = 16


class SidecarService:
    """业务 dispatch 层：线程安全（复用 bridge.py 的 _lock/_worker 语义）。"""

    def __init__(self, protocol_stdout):
        self._out = protocol_stdout
        self._out_lock = threading.Lock()
        self._controller = MaaRacingMasterController()
        self._lock = threading.RLock()
        self._worker = None  # 非 None = start slot 已占用（互斥依据，与 bridge.py 一致）
        # 默认选中鉴宝模块（GUI 进入即默认展示鉴宝；未注册或已过期时回退到第一个
        # 仍在有效期内的模块；全部不可用则不预选，由 GUI「（空）」承担该状态）。
        # 以模块 id 常量引用，避免 sidecar 耦合具体插件包。
        self._selected_module = (
            _DEFAULT_MODULE_ID
            if _DEFAULT_MODULE_ID in MODULE_REGISTRY and not module_expired(_DEFAULT_MODULE_ID)
            else first_available_module_id()
        )
        try:
            self._stages = get_module_info(self._selected_module)["stages"] if self._selected_module else []
        except KeyError:
            self._stages = []
        self._inflight = threading.Semaphore(_MAX_INFLIGHT)
        self._last_log_seq = 0   # 日志增量游标：单调序列号（见 Logger.get_lines_since）
        # 远程数据里**已校验通过**的可打开地址（公告详情 / 下载页）：前端只按逻辑目标名
        # 请求打开，地址留在本进程，不经过 RPC 往返（信任边界见 remote_meta 模块头）。
        self._remote_urls: dict[str, str] = {}
        self._closed = False
        # 模块配置缓存：**按模块 id 分槽**，切走的模块配置留在自己的槽里不被覆盖。
        # 槽里只放纯配置数据，**绝不放模块实例**——实例持有线程/文件句柄等资源，
        # 缓存若持有实例，等于把"已经切走的模块"留在内存里继续活着。
        self._module_config_cache: dict[str, dict] = {}
        # 启动即回填上次会话的用户偏好（模块配置缓存 + 调试开关）。
        self._restore_profile()

    # ---------- 协议输出 ----------

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._out_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _respond(self, rid, ok: bool, data=None, error=None) -> None:
        self.send({"type": "response", "id": rid, "ok": ok, "data": data, "error": error})

    # ---------- dispatch ----------

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                print(f"[sidecar] malformed stdin line ignored: {line!r}", file=sys.stderr)
                continue
            rid = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
            # 每个 request 独立 handler 线程：stdin reader 永不阻塞。
            # 非 daemon：stdin EOF 后仍等所有在途请求写完响应再退出（shutdown 响应不能丢）。
            if not self._inflight.acquire(blocking=False):
                self._respond(rid, False, None, "sidecar busy (in-flight limit)")
                continue
            threading.Thread(
                target=self._dispatch, args=(method, params, rid)
            ).start()

    def _dispatch(self, method, params, rid) -> None:
        is_shutdown = method == "shutdown"
        try:
            # 白名单查表，不做属性反射：非表内方法名（含任何下划线/内部成员）
            # 一律 unknown，WebView 侧无法触达服务对象的其余接口。
            handler = getattr(self, method, None) if method in HANDLERS else None
            if handler is None:
                self._respond(rid, False, None, f"unknown method {method}")
                return
            ok, data, error = handler(params)
            self._respond(rid, ok, data, error)
        except Exception as exc:  # noqa: BLE001 —— 任何异常如实回给 shell
            logger.log(f"sidecar dispatch 异常: {exc}", "ERROR")
            self._respond(rid, False, None, repr(exc))
        finally:
            self._inflight.release()
            if is_shutdown:
                os._exit(0)  # 响应已发出，立即退出（worker 线程中 sys.exit 无效）

    # ---------- 业务方法（返回 (ok, data, error)） ----------

    def _restore_profile(self) -> None:
        """启动回填上次会话偏好：只取本程序认识的键，未知/非法内容一律忽略。

        - module_config → 按模块分槽回填 _module_config_cache（下次 start 注入新实例）；
        - debug 段 → 直接恢复 controller 的调试/peep 开关状态。
        """
        data = _load_profile()
        if not data:
            return
        # 1) 调试开关（容错：仅按 bool 值恢复，其它类型忽略）
        dbg = data.get("debug")
        if isinstance(dbg, dict):
            dm = dbg.get("debug_mode")
            if isinstance(dm, bool):
                self._controller.set_debug_mode(dm)
            peep = dbg.get("peep_enabled")
            if isinstance(peep, bool):
                debug = self._controller.debug
                debug.enable_peep() if peep else debug.disable_peep()
            acg = dbg.get("auto_close_game")
            aem = dbg.get("auto_exit_mra")
            if isinstance(acg, bool) or isinstance(aem, bool):
                # 运行结束自动关闭开关（GUI「运行结束后」卡片），单独恢复不串扰
                self._controller.set_auto_shutdown(
                    close_game=acg if isinstance(acg, bool) else None,
                    exit_mra=aem if isinstance(aem, bool) else None,
                )
            cm = dbg.get("click_mode")
            if isinstance(cm, str):
                try:
                    self._controller.set_click_mode(cm)
                except ValueError:
                    pass  # 非法值忽略，保持默认
            im = dbg.get("intent_mode")
            if isinstance(im, bool):
                self._controller.set_intent_mode(im)
            mg = dbg.get("mute_game")
            if isinstance(mg, bool):
                self._controller.set_mute_game(mg)
        # 2) 模块配置 → 按模块分槽回填；键白名单由各模块的 DEFAULT_MODULE_CONFIG 声明
        slots: dict[str, dict] = {}
        for mid, flat in _parse_module_config_slots(data.get("module_config")).items():
            # 未注册（模块已被剥离）或已过有效期的模块不收：留下的槽没有生效路径
            if mid not in MODULE_REGISTRY or module_expired(mid):
                continue
            allowed = _module_config_defaults(mid)
            kept = {k: flat[k] for k in allowed if k in flat}
            if kept:
                slots[mid] = kept
        self._module_config_cache = slots

    def get_initial_state(self, params):
        return (True, {
            # 显示串：源码模式带「+领先次数（源码模式）」，打包模式为包内固化快照；
            # 版本比较另用 _version_tuple()（内部仍取干净基线，不受此后缀影响）
            "version": __display_version__,
            "modules": self._module_list(),
            "selected_module": self._selected_module,
            "stages": list(self._stages),
            "model_ok": self._selected_module_assets_ok(),
            "is_running": self._controller.module_active,
        }, None)

    def _module_list(self) -> list:
        """GUI 下拉栏数据源：含声明有效期与过期标记（过期项前端置灰、不自动选中）。"""
        return [
            {
                "id": mid,
                "name": info["name"],
                "stages": info["stages"],
                "requires_gamepad_exclusive": info["requires_gamepad_exclusive"],
                "expired": info["expired"],
                "valid_until": info["valid_until"],
            }
            for mid, info in (
                (mid, get_module_info(mid)) for mid in MODULE_REGISTRY
            )
        ]

    def _selected_module_assets_ok(self) -> bool:
        """当前选中模块的插件自带资源（REQUIRED_ASSETS）是否齐全。

        未选中模块时不告警（True）；模块被剥离/未注册时同样不告警，由启动校验兜底。
        """
        if not self._selected_module:
            return True
        try:
            return not check_required_assets(self._selected_module)
        except KeyError:
            return True

    def select_module(self, params):
        """切换活动模块。

        - **运行中一律拒绝**：所选模块与正在跑的模块必须始终一致。运行中切换会让
          GUI 的阶段列表、当前阶段高亮、配置面板全部指向另一个模块，而跑着的仍是旧模块
          ——界面与实况对不上，用户会照着错的阶段名读日志。判据用 worker 槽（从 start
          受理到 worker 结束）而非模块自身的 running 标志，把"启动中/停止中"也一并拦住。
        - module_id 为空 = GUI「（空）」选项：清空选择并正常返回（无模块可用时的合法状态）；
        - 已过有效期的模块须带 force=true（GUI 弹窗确认后下发），否则拒绝；
        - 未知 id 仍返回「模块不存在」。
        """
        with self._lock:
            busy = self._worker is not None
        if busy:
            return (False, None, "运行中不允许切换活动模块，请先停止")
        module_id = params.get("module_id")
        if not module_id:
            with self._lock:
                self._selected_module = None
                self._stages = []
            logger.log("活动模块已清空选择")
            return (True, {"stages": [], "module_id": None}, None)
        try:
            info = get_module_info(module_id)
        except KeyError:
            return (False, None, f"模块不存在: {module_id}")
        if info["expired"] and not params.get("force"):
            return (False, None, f"模块已过期，需确认后才能选择: {module_id}")
        with self._lock:
            self._selected_module = module_id
            self._stages = info["stages"]
        logger.log(f"活动模块已切换: {module_id}")
        return (True, {"stages": info["stages"], "module_id": module_id}, None)

    def get_status(self, params):
        with self._lock:
            worker = self._worker
            selected = self._selected_module
        # 性能快照按鸭子类型取：谁实现了 read_perf_snapshot 就带谁的数据，
        # core 不点名任何活动模块（方向红线：通用层不指向模块）。模块没实现 /
        # 未运行 → perf=None，前端据此渲染"无数据"，不猜默认值。
        perf = None
        reader = getattr(self._controller.active_module, "read_perf_snapshot", None)
        if callable(reader):
            try:
                perf = reader()
            except Exception as exc:  # noqa: BLE001 —— 仪表读数失败绝不影响状态轮询
                logger.log(f"[sidecar] 性能快照读取失败: {exc}", "DEBUG")
        return (True, {
            "is_running": self._controller.module_active,
            "current_stage": self._controller.current_stage,
            "worker_active": worker is not None and worker.is_alive(),
            "selected_module": selected,
            "perf": perf,
        }, None)

    # ---------- 活动模块配置（当前 GUI 用 treasure：每日循环上限；接口为通用 module_config 路由）----------

    def _module_config_view(self, module_id: str) -> dict:
        """某模块的配置视图（未运行时）：模块声明的默认值 ← 该模块自己的缓存槽。

        两条纪律：
          - **不创建实例**——读配置是纯数据操作。模块的 ``get_module_config`` 只在
            "该模块正在运行"时被调用（那份带 `_state` 实况），不再为了读默认值建离线段实例；
          - **槽按模块隔离**——切走的模块配置留在自己的槽里，不被后来者覆盖。
        """
        view = _module_config_defaults(module_id)
        with self._lock:
            cached = self._module_config_cache.get(module_id)
        if cached:
            view.update(cached)
        return view

    def get_module_config(self, params):
        module_id = params.get("module_id") or self._selected_module
        if not module_id:
            return (True, {}, None)
        live = self._controller.active_module
        if live is not None and getattr(live, "ID", None) == module_id:
            # 运行中：实例权威——同 id 的运行实例是唯一知道实况（_state）的地方
            getter: Any = getattr(live, "get_module_config", None)
            if not callable(getter):
                return (True, {}, None)
            try:
                raw = getter()
            except Exception as exc:  # noqa: BLE001
                return (False, None, f"读模块配置失败: {exc!r}")
            # 契约：模块的 get_module_config() 返回 dict；返回值不符时按空配置处理，
            # 不让一个写错的模块把 GUI 的配置读取整条打断。
            if not isinstance(raw, dict):
                return (True, {}, None)
            raw.setdefault("module_id", module_id)
            return (True, raw, None)
        # 未运行：默认值 ∪ 缓存（GUI 改过但还没 start 的值）
        view = self._module_config_view(module_id)
        view.setdefault("module_id", module_id)
        return (True, view, None)

    def set_module_config(self, params):
        config = params.get("config") or {}
        module_id = params.get("module_id") or self._selected_module
        if not module_id:
            return (False, None, "未选择活动模块，配置无处可存")
        if module_id not in MODULE_REGISTRY:
            return (False, None, f"模块不存在: {module_id}")
        # 只收该模块声明过的键（白名单 = DEFAULT_MODULE_CONFIG 的键集），未知键丢弃
        allowed = _module_config_defaults(module_id)
        try:
            with self._lock:
                target = dict(self._module_config_cache.get(module_id) or {})
                target.update({k: v for k, v in config.items() if k in allowed})
                self._module_config_cache[module_id] = target
                snapshot = {mid: dict(cfg) for mid, cfg in self._module_config_cache.items() if cfg}
        except Exception as exc:  # noqa: BLE001
            return (False, None, f"写模块配置失败: {exc!r}")
        # 写盘持久化（按模块分槽），下次启动由 _restore_profile 回填；失败仅记警告。
        # **只写缓存、不做运行中热更新**：运行中的模块以实例为准，配置下次「开始」才注入。
        _save_profile({"module_config": snapshot})
        result = self._module_config_view(module_id)
        result.setdefault("module_id", module_id)
        return (True, result, None)

    def start(self, params):
        """快速响应 + worker 线程跑 controller；stop/get_status 在运行期间必须仍可处理。"""
        start_from = params.get("start_from")
        with self._lock:
            module_id = self._selected_module
            stages = list(self._stages)
            # 组装：start_params.module_config（本次 start 带参）∪ 该模块自己的缓存槽
            # （GUI 先改后 start 的值）。槽按模块 id 取，不存在"老模块缓存污染新模块"。
            param_cfg = params.get("module_config") if isinstance(params.get("module_config"), dict) else {}
            cache_cfg = dict(self._module_config_cache.get(module_id) or {}) if module_id else {}
            merged_cfg = _merge_module_config(cache_cfg, param_cfg)
            start_module_config = merged_cfg or None
            # 本次带参并入该模块的槽（下一次 get_module_config 回读一致）
            if start_module_config and module_id:
                self._module_config_cache[module_id] = dict(merged_cfg)
        if module_id is None:
            return (False, None, "未选择活动模块")
        if start_from is not None and start_from not in stages:
            return (False, None, f"断点 {start_from} 不属于模块 {module_id} 的阶段")
        if module_id not in MODULE_REGISTRY:
            return (False, None, f"模块不存在: {module_id}")
        module_cls = MODULE_REGISTRY[module_id]
        # 启动约束按**本次配置**求值：模块可因模式不同放宽。比如 speedrush 的演示录制模式
        # 不操纵车辆（不创建虚拟设备），而维护者正要用物理手柄驾驶——不能要求他断开手柄。
        if module_cls.requires_exclusive_gamepad(start_module_config) and has_physical_controller():
            return (False, None, "请断开所有物理手柄后再运行")

        # 插件自带资源校验（REQUIRED_ASSETS，如 racing 的 YOLO 模型）：
        # 模型已随插件自包含（plugins/<id>/resources/），缺失时拦截并给出插件内具体路径。
        missing_assets = check_required_assets(module_id)
        if missing_assets:
            return (False, None, "插件资源缺失: " + ", ".join(missing_assets))

        # 启动阶段检测：依赖虚拟手柄的模块在 ViGEmBus 驱动缺失时提前拦下，
        # 返回结构化错误码 VIGEM_BUS_MISSING，供前端弹「下载并安装 ViGEmBus 驱动」引导。
        # 同样按本次配置求值（录制模式不需要手柄能力，无驱动也该能跑）。
        if ("gamepad" in module_cls.required_capabilities(start_module_config)
                and not self._controller.gamepad_available()):
            return (False, None, (
                "VIGEM_BUS_MISSING: 检测到缺少 ViGEmBus 驱动，无法创建虚拟手柄。"
                "请先点击「下载并安装 ViGEmBus 驱动」完成安装后重试。"
            ))

        with self._lock:
            if self._closed:
                return (False, None, "正在关闭")
            if self._worker is not None:  # slot 已占用，不以 is_alive() 判断
                return (False, None, "已在运行")
            worker = threading.Thread(
                target=self._worker_main,
                args=(module_id, start_from, start_module_config),
                daemon=True,
                name="module-worker",
            )
            self._worker = worker
            worker.start()

        if start_from is not None and start_from != (stages[0] if stages else None):
            logger.log(f"断点模式: 从「{start_from}」开始运行")
        return (True, None, None)

    def _worker_main(self, module_id: str, start_from, module_config: dict | None) -> None:
        try:
            # 模块配置注入：在 start_module 内部 create_module 之后、module.start() 之前调用 set_module_config
            self._controller.start_module(module_id, start_from, module_config=module_config)
        except Exception as exc:
            logger.log(f"运行异常: {exc}", "ERROR")
        finally:
            # 运行结束自动退出程序：与「关闭游戏」同源判定（last_run_natural），
            # 仅"正常完成"生效，报错/手动停止不触发。发 auto_exit 事件 → shell 关闭窗口优雅退出。
            if self._controller.last_run_natural and self._controller.auto_exit_mra:
                self.send({"type": "event", "event": "auto_exit"})
            with self._lock:
                if self._worker is threading.current_thread():
                    self._worker = None

    def stop(self, params):
        with self._lock:
            if self._closed:
                return (True, None, None)
            worker = self._worker
        if worker is not None and worker.is_alive():
            self._controller.stop()
        return (True, None, None)

    def _resolve_target(self, target) -> str:
        """逻辑目标名 → 可打开地址；未知目标返回空串。

        静态目标来自 `remote_meta.EXTERNAL_TARGETS`；`announcement` / `download` 是
        「远程数据里已校验通过的地址」，取本进程最近一次解析结果——前端不传递 URL，
        因此即使前端被完全控制，也只能请求这几个目标，无法指定打开哪里。
        """
        if not isinstance(target, str):
            return ""
        if target in EXTERNAL_TARGETS:
            return EXTERNAL_TARGETS[target]
        if target in ("announcement", "download"):
            with self._lock:
                remembered = self._remote_urls.get(target, "")
            if remembered:
                return remembered
            # 远程没给（或给的地址不合规）时，下载页回退官方 release 页；
            # 公告没有可回退的地址，返回空串让调用方报「暂不可用」。
            return EXTERNAL_TARGETS["release"] if target == "download" else ""
        return ""

    def open_vigembus_download(self, params):
        """在用户默认浏览器打开 ViGEmBus 官方下载页，引导安装驱动。

        训练/运行必需的内核驱动无法随包 app-local 分发，只能由用户手动安装一次。
        地址由本进程固定，不接受调用方传 URL；返回是否已打开发布页。
        """
        import webbrowser
        url = EXTERNAL_TARGETS["vigembus"]
        try:
            webbrowser.open(url)
            return (True, {"opened": url}, None)
        except Exception as exc:  # noqa: BLE001
            return (False, None, f"打开 ViGEmBus 下载页失败: {exc}")

    def open_external_url(self, params):
        """按**逻辑目标名**在默认浏览器打开官方地址（关于页跳转 / 公告详情 / 下载页）。

        调用方只报「打开哪个目标」（`target`），不报「打开哪里」——地址一律由本进程
        决定：静态目标查 `EXTERNAL_TARGETS`，远程目标取已校验过的地址。放行前再过一次
        `is_openable_url` 作纵深防御：不论地址从哪条路径攒出来，都逃不过官方白名单。
        """
        import webbrowser
        url = self._resolve_target(params.get("target"))
        if not url:
            return (False, None, f"未知或暂不可用的外部目标: {params.get('target')!r}")
        if not is_openable_url(url):
            return (False, None, "目标地址不在允许范围内")
        try:
            webbrowser.open(url)
            return (True, {"opened": url}, None)
        except Exception as exc:  # noqa: BLE001
            return (False, None, f"打开链接失败: {exc}")

    def open_user_data_folder(self, params):
        """用资源管理器打开用户数据目录（%APPDATA%/MaaRacingMaster）。

        目录不存在时自动创建，避免首次运行即报错。os.startfile 是 Windows
        原生调起关联程序的方式，比 subprocess.Popen(['explorer', path]) 更稳。
        """
        path = user_data_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows 特有
            return (True, {"path": str(path)}, None)
        except Exception as exc:  # noqa: BLE001
            return (False, None, f"打开文件夹失败: {exc}")

    def get_registry_optimizations(self, params):
        """读取全部注册表优化项的当前状态（启动体检 + 设置页优化中心共用）。

        判定：值缺失按 default（系统默认）判定 —— default 非 0 即视为未优化，
        与"Windows 默认行为 = 功能开启"的语义一致（写入一次后值恒存在，不再打扰）。
        非 Windows 平台返回空列表，前端静默跳过。
        """
        items = []
        ignored_ids = set(_get_ignored_prompt_ids())
        for opt in _REGISTRY_OPTIMIZATIONS:
            if opt.get("kind") == "noopenwith":
                # NoOpenWith 标记型：全部路径存在标记 = 已优化；current 归一化为 0/1 供前端展示
                all_present = all(
                    _reg_value_exists(opt["hive"], p, opt["value_name"]) for p in opt["paths"]
                )
                current = 0 if all_present else 1
                optimized = all_present
            elif opt.get("kind") == "protocol_command":
                # 协议空处理程序型：全部路径存在 shell\open\command = 已优化
                all_present = all(_protocol_command_exists(opt["hive"], p) for p in opt["paths"])
                current = 0 if all_present else 1
                optimized = all_present
            else:  # dword
                current = _read_reg_dword(opt["hive"], opt["path"], opt["value_name"])
                effective = opt["default"] if current is None else current
                optimized = effective == opt["optimized"]
            # 可用性：目标应用包已注册时（协议有真实处理程序、不会弹窗）项"无需优化"，
            # 前端不展示操作按钮、体检自动跳过；set 端对优化（写 0）也会拒绝兜底。
            unavailable = bool(opt.get("appx_absent")) and _appx_package_registered(opt["appx_absent"])
            items.append({
                "id": opt["id"],
                "kind": opt["kind"],
                "name": opt["name"],
                "effect": opt["effect"],
                "hive": opt["hive"],
                "path": opt["path"],
                "paths": list(opt.get("paths", (opt["path"],))),
                "value_name": opt["value_name"],
                "current": current,
                "optimized": optimized,
                "available": not unavailable,
                "default": opt["default"],
                "optimized_value": opt["optimized"],
                "needs_admin": opt["needs_admin"],
                "detail": opt["detail"],
                "unavailable_note": (
                    f"检测到 {opt['appx_absent']} 已安装：协议由系统正常注册、不会弹窗，本项无需处理。"
                    if unavailable else ""
                ),
                "options": opt["options"],
                "apply_label": opt["apply_label"],
                "restore_label": opt["restore_label"],
                "prompt_ignored": opt["id"] in ignored_ids,
            })
        return (True, {"items": items}, None)

    def set_optimization_prompt_ignored(self, params):
        """忽略/恢复某优化项的启动提醒（按项持久化到 profile）。

        只影响启动体检弹窗；权限优化中心仍可见可手动操作——新增优化项 id
        不在忽略列表，照常提醒（用户顾虑：全局忽略会误伤未来新增项，故按项记录）。
        """
        opt_id = params.get("id")
        ignored = bool(params.get("ignored"))
        if not any(o["id"] == opt_id for o in _REGISTRY_OPTIMIZATIONS):
            return (False, None, f"未知优化项: {opt_id}")
        ids = [i for i in _get_ignored_prompt_ids() if i != opt_id]
        if ignored:
            ids.append(opt_id)
        _set_ignored_prompt_ids(ids)
        logger.log(f"优化项 {opt_id} 启动提醒{'已忽略' if ignored else '已恢复'}", "DEBUG")
        return (True, {"id": opt_id, "ignored": ignored}, None)

    def set_registry_optimization(self, params):
        """写入指定优化项的注册表值（id + value），写后回读确认。

        HKLM 项需要管理员权限：发布版 sidecar 经 MaaRacingMaster.Shell UAC 提权可写；
        开发模式非管理员终端会收到带指引的明确错误。
        """
        import winreg

        opt_id = params.get("id")
        value = params.get("value")
        opt = next((o for o in _REGISTRY_OPTIMIZATIONS if o["id"] == opt_id), None)
        if opt is None:
            return (False, None, f"未知优化项: {opt_id}")
        if not isinstance(value, int):
            return (False, None, f"value 必须为整数，收到: {value!r}")
        hive = winreg.HKEY_CURRENT_USER if opt["hive"] == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        if opt.get("kind") == "protocol_command":
            # 协议空处理程序型：0 = 补 NoOpenWith + 空 shell\open\command（屏蔽）；
            # 1 = 删 NoOpenWith + 删空 shell 子树（恢复默认弹窗）。
            # 目标应用已安装时协议有真实处理程序，写空 command 反而拦截正常唤起 → 拒绝优化。
            if value == 0 and opt.get("appx_absent") and _appx_package_registered(opt["appx_absent"]):
                return (False, None, f"检测到 {opt['appx_absent']} 已安装，协议有真实处理程序不会弹窗，无需屏蔽")
            try:
                for path in opt["paths"]:
                    with winreg.CreateKey(hive, path) as key:
                        if value == 0:
                            winreg.SetValueEx(key, opt["value_name"], 0, winreg.REG_SZ, "")
                        else:
                            try:
                                winreg.DeleteValue(key, opt["value_name"])
                            except FileNotFoundError:
                                pass  # 已不存在 = 目标状态，幂等
                    if value == 0:
                        with winreg.CreateKey(hive, path + r"\shell\open\command") as key:
                            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "")
                    else:
                        _delete_protocol_shell_tree(opt["hive"], path)
            except OSError as exc:
                logger.log(f"[sidecar] 优化项 {opt_id} 写入失败: {exc!r}", "WARNING")
                return (False, None, f"写入注册表失败: {exc}")
            if value == 0:
                confirmed = all(_protocol_command_exists(opt["hive"], p) for p in opt["paths"])
            else:
                confirmed = not any(_protocol_command_exists(opt["hive"], p) for p in opt["paths"])
            if not confirmed:
                return (False, None, "写入后回读异常（shell\\open\\command 未达目标状态）")
            state = "已优化" if value == opt["optimized"] else "已恢复系统默认"
            logger.log(f"注册表优化项 {opt_id} {state}（protocol_command={'补空处理程序' if value == 0 else '已删除'}）", "INFO")
            return (True, {"id": opt_id, "value": value, "optimized": value == opt["optimized"]}, None)
        if opt.get("kind") == "noopenwith":
            # 标记型：0 = 在全部路径写 NoOpenWith（屏蔽）；1 = 删除标记（恢复默认弹窗）
            try:
                for path in opt["paths"]:
                    with winreg.CreateKey(hive, path) as key:
                        if value == 0:
                            winreg.SetValueEx(key, opt["value_name"], 0, winreg.REG_SZ, "")
                        else:
                            try:
                                winreg.DeleteValue(key, opt["value_name"])
                            except FileNotFoundError:
                                pass  # 已不存在 = 目标状态，幂等
            except OSError as exc:
                logger.log(f"[sidecar] 优化项 {opt_id} 写入失败: {exc!r}", "WARNING")
                return (False, None, f"写入注册表失败: {exc}")
            if value == 0:
                confirmed = all(
                    _reg_value_exists(opt["hive"], p, opt["value_name"]) for p in opt["paths"]
                )
            else:
                confirmed = not any(
                    _reg_value_exists(opt["hive"], p, opt["value_name"]) for p in opt["paths"]
                )
            if not confirmed:
                return (False, None, f"写入后回读异常（{opt['value_name']} 未达目标状态）")
            state = "已优化" if value == opt["optimized"] else "已恢复系统默认"
            logger.log(f"注册表优化项 {opt_id} {state}（{opt['value_name']}={value}）", "INFO")
            return (True, {"id": opt_id, "value": value, "optimized": value == opt["optimized"]}, None)
        # dword 型：CreateKey 补建 + 写值 + 回读确认
        try:
            with winreg.CreateKey(hive, opt["path"]) as key:
                winreg.SetValueEx(key, opt["value_name"], 0, winreg.REG_DWORD, value)
                confirm, _vtype = winreg.QueryValueEx(key, opt["value_name"])
        except OSError as exc:
            logger.log(f"[sidecar] 优化项 {opt_id} 写入失败: {exc!r}", "WARNING")
            if opt["needs_admin"] and getattr(exc, "winerror", None) == 5:
                return (False, None, "该项位于 HKLM，需要管理员权限（请以管理员身份运行程序后重试）")
            return (False, None, f"写入注册表失败: {exc}")
        if confirm != value:
            return (False, None, f"写入后回读异常（{opt['value_name']}={confirm}）")
        state = "已优化" if value == opt["optimized"] else "已恢复系统默认"
        logger.log(f"注册表优化项 {opt_id} {state}（{opt['value_name']}={value}）", "INFO")
        return (True, {"id": opt_id, "value": value, "optimized": value == opt["optimized"]}, None)

    # ---------- 关于页：检查更新 / 公告 ----------

    _GITHUB_REPO = GITHUB_REPO  # 唯一真源在 remote_meta（与官方域白名单同源，避免两处副本漂移）
    # CNB 镜像仓库（cnb.cool/MaaRacingMaster/MAIN）：只做 git 同步（mirror-to-cnb.yml），
    # 无发布包与匿名 releases API。「检测更新」改读版本标记文件 docs/latest_release.json
    # （release.yml 打 tag 时自动生成并回写 master，镜像随之同步）——CNB 优先（国内快），
    # GitHub raw / GitHub API 兜底。公告同理 CNB raw 优先。
    # 所有源都只是「取字节」；「这些字节能不能信」由 remote_meta 的校验器统一判定。
    _CNB_RAW_BASE = "https://cnb.cool/MaaRacingMaster/MAIN/-/git/raw/master"
    _RELEASE_URLS = (
        f"{_CNB_RAW_BASE}/docs/latest_release.json",
        f"https://raw.githubusercontent.com/{_GITHUB_REPO}/master/docs/latest_release.json",
        f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
    )
    _ANNOUNCEMENT_URLS = (
        f"{_CNB_RAW_BASE}/docs/announcement.json",
        f"https://raw.githubusercontent.com/{_GITHUB_REPO}/master/docs/announcement.json",
        f"https://cdn.jsdelivr.net/gh/{_GITHUB_REPO}@master/docs/announcement.json",
    )
    _NET_TIMEOUT = 4.0  # 秒（手动触发可接受）

    @staticmethod
    def _http_get(url: str) -> str:
        """带 UA 的 GET，超时返回空串；不抛网络异常（由调用方处理结果断言）。"""
        req = Request(url, headers={"User-Agent": f"MaaRacingMaster/{__version__}"})
        with urlopen(req, timeout=SidecarService._NET_TIMEOUT) as resp:
            return resp.read().decode("utf-8")

    def check_update(self, params):
        """查最新版本与当前版本比较。仅手动触发（GUI 按钮）。

        多源顺序（CNB 优先，2026-09-04）：CNB raw 版本标记 → GitHub raw 版本标记 →
        GitHub API releases/latest。前两者读 release.yml 生成的 docs/latest_release.json
        （{tag, version, published_at, download_url}）；最后回退官方 API 原生字段。

        每个源都过同一个校验器（`remote_meta.parse_release`）：tag 不合规的源按
        「该源不可用」跳过、继续 fallback。`download_url` 只接受官方域，不合规即回退
        官方 release 页——远程数据不得决定把用户带去哪个站点。
        """
        latest: str | None = None
        published = ""
        download_url = ""
        for url in self._RELEASE_URLS:
            try:
                rel = parse_release(json.loads(self._http_get(url)))
            except HTTPError as exc:
                if exc.code == 404 and url.startswith("https://api.github.com"):
                    # 仅 GitHub 官方 API 的 404 权威判定「仓库无 release」；CNB raw 404
                    # 只说明标记文件尚未生成（发版前），继续尝试后续源。
                    return (True, {"has_update": False, "error": None, "status": "no_release"}, None)
                continue
            except (URLError, TimeoutError, OSError, ValueError, TypeError):
                continue
            if rel is None:
                continue
            latest = rel["tag"]
            published = rel["published_at"]
            download_url = rel["download_url"] or EXTERNAL_TARGETS["release"]
            break
        if latest is None:
            return (True, {"has_update": False, "error": "无法连接到更新服务器，请稍后再试",
                           "status": "network"}, None)

        with self._lock:
            self._remote_urls["download"] = download_url
        cur = ".".join(str(x) for x in self._version_tuple())
        has_update = self._compare_versions(latest, cur) > 0
        return (True, {
            "has_update": has_update,
            "latest_tag": latest,
            "published_at": published,
            "download_url": download_url,
            "error": None,
            "status": "ok",
        }, None)

    def fetch_announcement(self, params):
        """拉取公告（CNB raw 优先 → GitHub raw → jsdelivr）。过期返回空；数据异常返回空。

        每个源都过同一个校验器（`remote_meta.parse_announcement`）：不合规的源按
        「本条无效」跳过、继续 fallback，不存在「先渲染再校验」的路径。全部源都不可用时
        返回 level=none（前端显示「暂无公告」），不影响启动。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        for url in self._ANNOUNCEMENT_URLS:
            try:
                data = json.loads(self._http_get(url))
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
                continue
            ann = parse_announcement(data)
            if ann is None or is_expired(ann["effective_until"], today):
                continue  # 无效/过期公告：跳过该源，继续 fallback
            with self._lock:
                # 详情地址留在本进程：前端只按目标名请求打开（见 _resolve_target）
                self._remote_urls["announcement"] = ann["url"]
            return (True, ann, None)
        return (True, {"title": "", "body": "", "level": "none", "date": "", "url": "", "url_text": ""}, None)

    @staticmethod
    def _version_tuple() -> tuple:
        """当前版本 → (主, 次, 修) 元组；dev 后缀取基线。异常回退 (0,0,0)。"""
        import re
        raw = __version__.split("+")[0].split("-")[0]
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", raw)
        if not m:
            return (0, 0, 0)
        return tuple(int(x) for x in m.groups())

    @staticmethod
    def _compare_versions(a: str, b: str) -> int:
        """semver 三段比较，返回 a-b 的符号（>0 表示 a 较新）。非数字段按 0 处理。"""
        import re
        def key(s):
            nums = re.findall(r"\d+", s) or ["0"]
            return tuple(int(x) for x in nums[:3] + ["0"] * (3 - len(nums)))
        ka, kb = key(a), key(b)
        return (ka > kb) - (ka < kb)

    def fetch_logs(self, params):
        # 环形缓冲下「已返回行数」当游标语义失效，改用单调序列号。
        # 初值 0 → 首次拉取返回全部历史（现有行为保留）。
        lines, new_seq, truncated = logger.get_lines_since(self._last_log_seq, "INFO")
        self._last_log_seq = new_seq
        result = []
        if truncated:
            result.append("[!!] 日志界面因落后过多已自动截断，以下为最新日志")
        result.extend(lines)
        return (True, {"lines": result}, None)

    def get_today_stats(self, params):
        """今日看板路由：读法由各模块自述（ActivityModule.read_today_stats），core 不持有
        任何模块的看板 schema（表结构、列名、日界语义全部住在插件内）。

        返回 (True, {"bucket", "summary", "games"}, None)；模块未注册或未声明看板读法
        时返回空看板（前端仅对声明了看板的模块轮询，此分支是删除模块后的健壮性兜底）。
        """
        module_id = params.get("module_id") if isinstance(params, dict) else None
        cls = MODULE_REGISTRY.get(module_id) if isinstance(module_id, str) else None
        reader = getattr(cls, "read_today_stats", None)
        if reader is None:
            return (True, {"bucket": None, "summary": None, "games": []}, None)
        return (True, reader(), None)

    # ---------- 调试页 ----------

    def get_debug_state(self, params):
        debug = self._controller.debug
        return (True, {
            "debug_mode": bool(getattr(self._controller, "_debug_mode", False)),
            "peep_enabled": bool(debug.peep_enabled),
            "click_mode": self._controller.click_mode,
            "intent_mode": self._controller.intent_mode,
            "emergency_stop_enabled": bool(getattr(self._controller, "_emergency_stop_enabled", False)),
            "file_logging": logger.file_logging,
            "auto_close_game": bool(self._controller.auto_close_game),
            "auto_exit_mra": bool(self._controller.auto_exit_mra),
            "mute_game": bool(self._controller.mute_game_enabled),
        }, None)

    def set_debug_mode(self, params):
        enabled = bool(params.get("enabled", False))
        self._controller.set_debug_mode(enabled)
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["debug_mode"] = bool(enabled)
        _save_profile({"debug": cur})
        logger.log(f"调试截图模式: {'开启' if enabled else '关闭'}")
        return (True, {"debug_mode": enabled}, None)

    def set_file_logging(self, params):
        """日志记录开关：开启后才把日志写入磁盘（user_data_dir/logs，每次开启新建文件）。"""
        enabled = bool(params.get("enabled", False))
        logger.set_file_logging(enabled)
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["file_logging"] = bool(enabled)
        _save_profile({"debug": cur})
        logger.log(f"日志记录(写盘): {'开启' if enabled else '关闭'}")
        return (True, {"file_logging": enabled}, None)

    def set_peep(self, params):
        enabled = bool(params.get("enabled", False))
        if enabled:
            self._controller.debug.enable_peep()
        else:
            self._controller.debug.disable_peep()
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["peep_enabled"] = bool(enabled)
        _save_profile({"debug": cur})
        logger.log(f"PEEP 实时预览: {'开启' if enabled else '关闭'}")
        return (True, {"peep_enabled": enabled}, None)

    def get_peep_frame(self, params):
        """取最新 PEEP 预览帧（JPEG base64），供前端数据页内嵌显示。未开启/无帧时返回 frame=None。"""
        import base64
        data = self._controller.debug.get_peep_jpeg()
        if data is None:
            return (True, {"frame": None}, None)
        return (True, {"frame": base64.b64encode(data).decode("ascii")}, None)

    def set_emergency_stop(self, params):
        enabled = bool(params.get("enabled", False))
        self._controller.set_emergency_stop(enabled)
        return (True, {"emergency_stop_enabled": enabled}, None)

    def set_click_mode(self, params):
        """切换点击方式：real(前台=鼠标 SendInput) / gamepad(后台=手柄导航+A键)。

        模式持久化到 profile（debug.click_mode），下次启动由 _restore_profile 回填。
        """
        mode = params.get("mode", "real")
        try:
            self._controller.set_click_mode(mode)
        except ValueError as e:
            return (False, None, str(e))
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["click_mode"] = mode
        _save_profile({"debug": cur})
        logger.log(f"点击方式: {mode}")
        return (True, {"click_mode": mode}, None)

    def set_intent_mode(self, params):
        """切换意图开关（仅显示意图）：开启后程序只导航到目标、不确认点击，由用户自己按。

        持久化到 profile（debug.intent_mode），下次启动由 _restore_profile 回填。
        """
        enabled = bool(params.get("enabled", False))
        self._controller.set_intent_mode(enabled)
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["intent_mode"] = enabled
        _save_profile({"debug": cur})
        logger.log(f"仅显示意图: {'开启' if enabled else '关闭'}")
        return (True, {"intent_mode": enabled}, None)

    def set_auto_close_game(self, params):
        enabled = bool(params.get("enabled", False))
        self._controller.set_auto_shutdown(close_game=enabled)
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["auto_close_game"] = enabled
        _save_profile({"debug": cur})
        logger.log(f"运行结束后自动关闭游戏: {'开启' if enabled else '关闭'}")
        return (True, {"auto_close_game": enabled}, None)

    def set_auto_exit_mra(self, params):
        enabled = bool(params.get("enabled", False))
        self._controller.set_auto_shutdown(exit_mra=enabled)
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["auto_exit_mra"] = enabled
        _save_profile({"debug": cur})
        logger.log(f"运行结束后自动退出程序: {'开启' if enabled else '关闭'}")
        return (True, {"auto_exit_mra": enabled}, None)

    def set_mute_game(self, params):
        """「运行选项」运行时静音游戏开关：运行期间静音游戏，结束后恢复 100%。

        静音/恢复动作由 controller.start_module 的 finally 统一执行（任何停止路径都恢复）；
        此处只改开关并持久化到 profile（debug.mute_game）。
        """
        enabled = bool(params.get("enabled", False))
        self._controller.set_mute_game(enabled)
        cur = _load_profile().get("debug")
        cur = cur if isinstance(cur, dict) else {}
        cur["mute_game"] = enabled
        _save_profile({"debug": cur})
        logger.log(f"运行时静音游戏: {'开启' if enabled else '关闭'}")
        return (True, {"mute_game": enabled}, None)

    def close(self, params):
        """shell 关闭前的业务清理：置 _closed + 停止 worker + 关写盘句柄。"""
        with self._lock:
            self._closed = True
            worker = self._worker
        if worker is not None and worker.is_alive():
            self._controller.stop()
        logger.log("sidecar 业务已停止", "DEBUG")
        logger.close()  # 关写盘句柄，flush 尾部（进程退出路径）
        return (True, None, None)

    def shutdown(self, params):
        """graceful shutdown：停止业务，响应后由 _dispatch 退出进程。"""
        return self.close(params)


def main() -> None:
    # DPI awareness 须最早设置：进程级语义，不继承 C# shell 配置（坐标换算前提）
    ensure_dpi_aware()
    # 日志写盘开关回读（profile.debug.file_logging，默认关）：须在 SidecarService
    # 构造前应用，让启动初期的日志也遵守开关状态
    _dbg = _load_profile().get("debug")
    logger.set_file_logging(bool(isinstance(_dbg, dict) and _dbg.get("file_logging", False)))
    # 启动时执行一次旧会话保留清理（不依赖本次是否启用写盘）
    logger.prune_sessions()
    protocol_stdout = sys.stdout
    sys.stdout = _StdoutGuard(protocol_stdout)  # 后续一切 print 都走 stderr，协议通道纯净
    try:
        service = SidecarService(protocol_stdout)
    except Exception as exc:
        print(f"[sidecar] 初始化失败: {exc}", file=sys.stderr)
        _STDERR.flush()
        os._exit(1)
    service.run()


if __name__ == "__main__":
    main()
