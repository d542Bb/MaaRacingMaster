#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaaRacingMaster
巅峰极速 · 活动自动化平台
MAA Framework + YOLOv8 ONNX + vgamepad
"""

from __future__ import annotations

import sys

# 禁用字节码写入：本项目进程运行不再到处生成 __pycache__（.gitignore 已忽略，
# 这里从源头停止生成；打包产物 embedded python 跑 sidecar 同样受益）。
# 注意：__init__.py 自身的 .pyc 会在本行执行前由解释器写出，属正常现象。
sys.dont_write_bytecode = True

import re  # noqa: E402 —— 上一行的停用字节码副作用须先于一切导入生效
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

# 版本号由 setuptools-scm 从 Git Tag 自动生成（构建时写入 _version.py 快照）
# 手动修改无效！改版本请打 git tag vX.Y.Z 并推送
#
# 双轨版本策略（解决「源码运行显示过期版本号」）：
#   1. 打包/安装产物（无 .git，或上层 .git 属于别的仓库）：读 _version.py 构建快照
#      —— 版本固化，旧版本永远不会被仓库里后来的新 tag 带歪；
#   2. 源码直接运行（本包就是那个 git 仓库的工作副本）：忽略可能过期的 _version.py
#      快照，启动时用 git describe 按「当前 checkout」动态推导 —— 开发模式自动跟随
#      所在提交，checkout 到旧 tag 就显示旧版本号，不会取全局最新 tag；
#   3. 两者都不可用（无 git 且无快照）→ "0.0.0.dev"。
try:
    from ._version import version as _vcs_version
except ImportError:
    _vcs_version = None

__version__ = _vcs_version or "0.0.0.dev"


def _find_git_root(start: Path) -> Path | None:
    """从包目录向上找含 .git 的仓库根；找不到返回 None。"""
    p = start
    for _ in range(6):
        if (p / ".git").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _git_describe_version() -> str | None:
    """源码模式：git describe 推导当前 checkout 版本（轻量解析，仅显示用途）。

    git describe --tags --long 输出形如 "v0.13.0-dev.4-0-g0f5c5bf"：
      base=0.13.0-dev.4, dist=0 → "0.13.0.dev4"（恰好停在 tag 上）
      dist>0 → "0.13.0.dev4+3"（距 tag 3 个 commit，附本地版本号）
    任何失败（无 git/无 tag/命令超时）返回 None，由调用方回退。
    """
    git_root = _find_git_root(Path(__file__).resolve().parent)
    if git_root is None:
        return None
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--long", "--always"],
            cwd=str(git_root), stderr=subprocess.DEVNULL, timeout=2,
        ).decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not out:
        return None
    m = re.match(r"^v?(.+)-(\d+)-g[0-9a-f]+$", out)
    if m:
        base, dist = m.group(1), int(m.group(2))
        ver = base.replace("-dev.", ".dev").replace(".dev.", ".dev")
        if dist == 0:
            return ver
        return f"{ver}+{dist}"  # 距 tag 若干 commit 的本地开发版本
    # 仓库一个 tag 都没有：直接返回 describe 输出（如短 hash）
    return out


def _is_source_checkout(pkg_init: Path) -> bool:
    """包本体是否就是所在 git 仓库的工作副本（= 源码直跑）。

    只问「向上有没有 .git」不够：正式包只要落在任何 git 仓库的子目录里（开发机上
    build/ 目录就是这种形态），都会撞见外层 .git 而被误判成源码模式，版本号随后被
    外层仓库的 git describe 劫持，包里固化的快照形同虚设。真判据是——那个仓库根里的
    maaracing_master 必须正是当前被导入的这份文件。
    """
    git_root = _find_git_root(pkg_init.parent)
    if git_root is None:
        return False
    try:
        return (git_root / "maaracing_master" / "__init__.py").resolve() == pkg_init.resolve()
    except OSError:
        return False


SOURCE_MODE = _is_source_checkout(Path(__file__).resolve())


def _display_version(version: str, source_mode: bool) -> str:
    """UI 显示用版本串。

    源码模式：git describe 已把「距 tag 领先几个 commit」写进 +N，连同模式说明一起
    显示，一眼可辨眼前跑的是工作副本而非发布产物；恰好停在 tag 上（dist=0）时没有 +N。
    打包/安装产物：_version.py 快照原样呈现，不带任何模式后缀。
    """
    return f"{version}（源码模式）" if source_mode else version


# 源码运行（本包即仓库工作副本）时动态推导，覆盖可能过期的构建快照；
# 打包产物（无 .git，或上层 .git 属于别的仓库）保持读取 _version.py 快照。
if SOURCE_MODE:
    _dev_version = _git_describe_version()
    if _dev_version:
        __version__ = _dev_version

__display_version__ = _display_version(__version__, SOURCE_MODE)
