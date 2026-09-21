# -*- coding: utf-8 -*-
"""RPC 信任边界三方一致性回归锁。

sidecar.py HANDLERS（Python 白名单）、RpcBridge.cs AllowedMethods（C# 前置闸）、
SidecarService 公开 handler、前端 mra.call 字面量四处必须同集合演进：

- HANDLERS ⊆ SidecarService 公开方法（表内方法必须真存在，防幽灵条目）
- AllowedMethods == HANDLERS - {shutdown, close}（shell 生命周期通道不对前端开放）
- 前端调用集 ⊆ AllowedMethods（JS 不得调白名单外方法）

机制动机（2026-09-16 外部审查 §4.2）：_dispatch 曾以 getattr 反射全量暴露
服务对象；白名单若只改一处、缺一致性锁，表与实现必然漂移（同"手抄数值必腐坏"
判据）。本锁让漂移在 CI 即红，不靠 review 自觉。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

try:
    from maaracing_master.core import sidecar as sc
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺重依赖的机器上整文件 SKIP（同 today_stats 口径）
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")

REPO = Path(__file__).resolve().parent.parent
SIDECAR = REPO / "maaracing_master" / "core" / "sidecar.py"
RPC_BRIDGE = REPO / "apps" / "MaaRacingMaster.Shell" / "RpcBridge.cs"
FRONTEND_DIR = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"

# shell 侧直接发起、不经前端通道的生命周期方法
SHELL_ONLY = {"shutdown", "close"}


def test_handlers_subset_of_service_methods():
    """白名单表内每个名字都必须是 SidecarService 的真实公开方法。"""
    tree = ast.parse(SIDECAR.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "SidecarService"
    )
    public = {
        f.name for f in cls.body
        if isinstance(f, ast.FunctionDef) and not f.name.startswith("_")
    }
    ghost = set(sc.HANDLERS) - public
    assert not ghost, f"HANDLERS 含不存在/私有方法：{sorted(ghost)}"


def test_cs_allowlist_matches_python_handlers():
    """C# AllowedMethods 必须恰等于 HANDLERS 去掉 shell 生命周期方法。"""
    src = RPC_BRIDGE.read_text(encoding="utf-8")
    m = re.search(
        r"AllowedMethods = new\(StringComparer\.Ordinal\)\s*\{(.*?)\};",
        src, re.S,
    )
    assert m, "RpcBridge.cs 找不到 AllowedMethods 字面量块"
    cs_set = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    expected = set(sc.HANDLERS) - SHELL_ONLY
    missing = expected - cs_set
    extra = cs_set - expected
    assert not missing and not extra, (
        f"C#/Python 白名单漂移：仅 Python 有 {sorted(missing)}；"
        f"仅 C# 有 {sorted(extra)}"
    )


def test_frontend_calls_within_allowlist():
    """前端所有 mra.call('name') 字面量必须在 C# 白名单内。

    必须递归扫 frontend/：前端按 js/ 子目录分模块后，非递归 glob 只覆盖根层
    文件，子目录里的调用点会静默脱离看护（实测从 28 处掉到 10 处）。
    """
    src = RPC_BRIDGE.read_text(encoding="utf-8")
    m = re.search(
        r"AllowedMethods = new\(StringComparer\.Ordinal\)\s*\{(.*?)\};",
        src, re.S,
    )
    assert m
    allowed = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    called: set[str] = set()
    for js in FRONTEND_DIR.rglob("*.js"):
        called.update(re.findall(r"mra\.call\(\s*'([a-z_]+)'", js.read_text(encoding="utf-8")))
        called.update(re.findall(r'mra\.call\(\s*"([a-z_]+)"', js.read_text(encoding="utf-8")))
    # 空过防护：抽取规则或扫描范围失效时报错，而不是静默通过
    assert called, "未抽到任何 mra.call 字面量，扫描范围或抽取规则已失效"
    rogue = called - allowed
    assert not rogue, f"前端调用了白名单外方法：{sorted(rogue)}"


def test_dispatch_rejects_non_allowlist():
    """活性锁：_dispatch 对表外名字（含内部成员、含 dunder）一律 unknown，
    对表内名字必须走到 handler——防止白名单只是装饰。"""
    svc = sc.SidecarService.__new__(sc.SidecarService)  # 不跑 __init__（避免拉控制器）
    responses = []
    svc._respond = lambda rid, ok, data=None, error=None: responses.append((rid, ok, error))
    svc._inflight = __import__("threading").Semaphore(16)

    for forbidden in ("_restore_profile", "__init__", "os", "logger", "nope"):
        svc._dispatch(forbidden, {}, f"r-{forbidden}")
        assert responses[-1][1] is False and "unknown" in (responses[-1][2] or ""), forbidden

    # 表内方法必须命中 handler（用一个零副作用的公开方法验证查表通路）
    assert "get_status" in sc.HANDLERS
    # get_status 依赖控制器状态，这里只断言不再报 unknown（错误形态即可放行到 handler）
    svc._controller = None
    svc._lock = __import__("threading").RLock()
    svc._dispatch("get_status", {}, "r-status")
    assert not (responses[-1][2] or "").startswith("unknown")
