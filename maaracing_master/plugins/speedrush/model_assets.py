# -*- coding: utf-8 -*-
"""深度模式模型包：用户数据目录优先、安装包兜底，带完整性校验。

发布包默认不带三个 ONNX 权重；要用深度模式时，把模型包解压到
``<用户数据>/data/speedrush/models/``（布局与本目录 ``resources/models/`` 同构，
去掉 ``resources/models/`` 前缀）。源码运行或显式全量包仍可直接读包内权重。
深度档要求 perception + depth；lane 是可选增强，缺失时线观测退回深度/墙供数。

校验口径：尺寸 + SHA256，按（路径、大小、mtime）缓存，同一进程只哈希一次。
模型文件更新时必须同步改 ``MODEL_MANIFEST``；旧下载包会被拒绝并提示重新下载。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from maaracing_master.core.paths import data_dir
from maaracing_master.plugins.speedrush import (
    DEPTH_MODEL_REL,
    LANE_MODEL_REL,
    MODELS_DIR,
    PERCEPTION_MODEL_REL,
)

PACK_PREFIX = "resources/models/"

# 深度档必需：缺任一项就不允许启动深度模式
DEPTH_MANIFEST: dict[str, tuple[int, str]] = {
    PERCEPTION_MODEL_REL: (
        10605615,
        "f12b5958637932f3a01f413c93a8a5ceba86693a8133a52fa32224ec8f5e3fe4",
    ),
    DEPTH_MODEL_REL: (
        71913448,
        "2e5c58b4bd6367593a8ed6039ef729a89c4b19f3a64e5954713f10a36ec0f116",
    ),
}

# 线观测可选：缺失时退回深度/墙供数，不影响深度档启动
LANE_MANIFEST: dict[str, tuple[int, str]] = {
    LANE_MODEL_REL: (
        543153,
        "45044c268d54ee34cbd097abcf19fb0d53858a8815e0024018db0df6137289e4",
    ),
}

# 模型包内容（可选包一次装齐深度与线观测）
MODEL_MANIFEST: dict[str, tuple[int, str]] = {
    **DEPTH_MANIFEST, **LANE_MANIFEST,
}

_VERIFIED: dict[tuple[str, int, int], bool] = {}


def model_pack_dir() -> Path:
    """用户下载的模型包根目录。"""
    return data_dir() / "speedrush" / "models"


def packed_rel(rel: str) -> str:
    """插件相对路径 → 模型包内相对路径。"""
    return rel[len(PACK_PREFIX):] if rel.startswith(PACK_PREFIX) else rel


def pack_path(rel: str) -> Path:
    return model_pack_dir() / packed_rel(rel)


def candidates(rel: str) -> tuple[Path, ...]:
    """候选文件，按优先级：用户下载包 → 安装包内权重。"""
    return (pack_path(rel), MODELS_DIR / packed_rel(rel))


def resolve(rel: str) -> Path | None:
    """只查存在性（启动期轻量检查，不哈希）。"""
    for path in candidates(rel):
        if path.is_file():
            return path
    return None


def missing() -> tuple[str, ...]:
    """深度档缺失或大小不符的权重（便宜检查；线观测可选，不计入门禁）。"""
    out: list[str] = []
    for rel, (want_size, _sha) in DEPTH_MANIFEST.items():
        ok = False
        for path in candidates(rel):
            try:
                if path.is_file() and path.stat().st_size == want_size:
                    ok = True
                    break
            except OSError:
                continue
        if not ok:
            out.append(rel)
    return tuple(out)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path | None, rel: str) -> str | None:
    """返回错误文本；None 表示通过。未知 rel 不做校验。"""
    if path is None:
        return f"缺少 {rel}（应放在 {pack_path(rel)}）"
    expected = MODEL_MANIFEST.get(rel)
    if expected is None:
        return None
    want_size, want_hash = expected
    try:
        st = path.stat()
    except OSError as exc:
        return f"无法读取 {path}：{exc}"
    if st.st_size != want_size:
        return (f"{path.name} 大小不符（{st.st_size} != {want_size}），"
                f"请重新下载模型包")
    cache_key = (str(path), st.st_size, st.st_mtime_ns)
    ok = _VERIFIED.get(cache_key)
    if ok is None:
        try:
            ok = _hash_file(path) == want_hash
        except OSError as exc:
            return f"无法读取 {path}：{exc}"
        _VERIFIED[cache_key] = ok
    if not ok:
        return f"{path.name} 校验失败（文件损坏或版本不符），请重新下载模型包"
    return None


def select(rel: str) -> tuple[Path | None, str | None]:
    """按候选顺序选第一个通过校验的文件；返回 (路径, 错误文本)。"""
    errors: list[str] = []
    for path in candidates(rel):
        if not path.is_file():
            continue
        err = verify(path, rel)
        if err is None:
            return path, None
        errors.append(err)
    if errors:
        return None, "；".join(errors)
    return None, f"缺少 {rel}（应放在 {pack_path(rel)}）"


def check_pack() -> str | None:
    """完整校验深度模式所需权重；None 表示可运行。线观测不计入。"""
    errors = []
    for rel in DEPTH_MANIFEST:
        _path, err = select(rel)
        if err:
            errors.append(err)
    return "；".join(errors) if errors else None


def clear_cache() -> None:
    """测试/热更新用：清空哈希缓存。"""
    _VERIFIED.clear()
