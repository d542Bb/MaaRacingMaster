#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把项目内的 ``dev/mpelb.exe`` 更新到 MPE 官方最新 release。

数据源是 MPE 官方 release 清单 ``mpe-manifest.json``（不是另起版本表）：
Windows 二进制的 URL 与 SHA256 都从清单读取，下载后校验、试运行，再原子替换。
首次 ``--update`` 直接创建 ``dev/mpelb.exe``；已安装时把上一版留成
``dev/mpelb.previous.exe``（只留一份，避免堆旧二进制）。只动本地二进制，
不装全局环境、不改 PATH、不碰 MaaFramework/OCR 运行时（本项目用 ``.venv`` 的 maa）。

用法：
    python tools/pipeline/mpelb_update.py --check    # 只看本地/最新
    python tools/pipeline/mpelb_update.py --update   # 下载、校验、备份、替换

也可以从入口调用：``mpe.cmd --check`` / ``mpe.cmd --update``，
``studio.cmd --check`` / ``studio.cmd --update``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MANIFEST_URL = (
    "https://github.com/kqcoxn/MaaPipelineEditor/releases/latest/download/mpe-manifest.json"
)
PLATFORM_KEY = "windows-amd64"
DEFAULT_TARGET = Path(__file__).resolve().parent / "dev" / "mpelb.exe"
USER_AGENT = "MaaRacingMaster-mpelb-updater"


def _log(message: str) -> None:
    print(f"[mpelb-update] {message}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_url(request: urllib.request.Request, timeout: int, attempts: int = 3):
    """GitHub 资产偶发断连；4xx 直接抛出，其余做短重试。"""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                raise
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    assert last is not None
    raise last


def _fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _open_url(request, timeout=60) as response:
        return json.load(response)


def _pick_asset(manifest: dict) -> tuple[str, str, str]:
    """返回 (MPE 版本, 二进制 URL, SHA256)；清单缺项/格式不对直接报错。"""
    try:
        version = manifest["version"]
        artifact = manifest["platforms"][PLATFORM_KEY]["binary"]
        url = artifact["url"]
        sha = artifact["sha256"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"官方清单缺少 {PLATFORM_KEY} 二进制项") from exc
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("官方清单的二进制 URL 非 https")
    if not isinstance(sha, str) or len(sha) != 64:
        raise ValueError("官方清单的 SHA256 格式不对")
    return str(version), url, sha.lower()


def _version_of(exe: Path) -> str:
    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unknown ({exc})"
    line = (result.stdout or result.stderr).strip().splitlines()
    return line[0].strip() if line else "unknown"


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with _open_url(request, timeout=300) as response, destination.open("wb") as out:
                shutil.copyfileobj(response, out)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            destination.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _previous_path(target: Path) -> Path:
    """只保留一份上一版，避免每次更新都堆一个 15MB 的旧二进制。"""
    return target.with_name("mpelb.previous.exe")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="更新项目内的 mpelb 到官方最新 release")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只检查，不下载")
    group.add_argument("--update", action="store_true", help="下载并替换本地 mpelb")
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help="要更新的 mpelb 路径（默认 tools/pipeline/dev/mpelb.exe）",
    )
    args = parser.parse_args(argv)

    if platform.system() != "Windows" or platform.machine().lower() not in ("amd64", "x86_64"):
        _log("仅支持 Windows x64：仓库的入口脚本只提供 .cmd")
        return 2
    target = args.target.resolve()
    installed = target.is_file()
    if installed:
        local_sha = _sha256(target)
        local_version = _version_of(target)
        _log(f"本地：{target}（{local_version}，sha256 {local_sha[:12]}…）")
    else:
        local_sha = None
        _log(f"本地未安装：{target}")

    try:
        remote_version, url, remote_sha = _pick_asset(_fetch_json(MANIFEST_URL))
    except Exception as exc:  # 网络/清单错误都直接报给用户
        _log(f"读取官方清单失败：{exc}")
        return 1
    _log(f"官方：MPE {remote_version}（sha256 {remote_sha[:12]}…）")

    if local_sha == remote_sha:
        _log("已是最新。")
        return 0
    if not args.update:
        _log("运行 --update 安装。" if not installed else "有更新可用；确认后运行 --update。")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    # Windows 上运行中的 exe 无法覆盖；更新前先停掉本地桥。
    subprocess.run(["taskkill", "/f", "/im", "mpelb.exe"], capture_output=True)
    temp = target.with_name(f".{target.name}.download")
    try:
        _log("下载中…")
        _download(url, temp)
        got_sha = _sha256(temp)
        if got_sha != remote_sha:
            raise RuntimeError(f"SHA256 不匹配（期望 {remote_sha}，实际 {got_sha}）")
        new_version = _version_of(temp)
        if "unknown" in new_version:
            raise RuntimeError(f"新二进制无法运行：{new_version}")

        if installed:
            backup = _previous_path(target)
            shutil.copy2(target, backup)
            _log(f"已备份上一版：{backup.name}")
        os.replace(temp, target)
        _log(f"{'已更新' if installed else '已安装'}：{new_version}（MPE {remote_version}）")
        return 0
    except Exception as exc:
        _log(f"更新失败：{exc}")
        return 1
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
