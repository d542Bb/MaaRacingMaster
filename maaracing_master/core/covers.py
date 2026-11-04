# -*- coding: utf-8 -*-
"""模块封面：插件声明「我有一张封面」，核心负责解析成前端能直接用的 URL。

契约（三方各管一段，谁都不越界）：

| 责任方 | 职责 |
|---|---|
| 插件模块 | 在模块类上写 ``COVER = "cover.jpg"``（相对自己的 ``resources/``） |
| 内置步骤 | 同上，但相对 ``core/resources/``（步骤本来就属于核心） |
| 核心（本文件） | 校验声明、定位文件、解析成 URL 下发给前端 |
| 前端 | 只消费下发的 URL，不认识 ``plugins/<id>/resources/`` 这类内部路径 |

**为什么解析权在核心**：插件只声明"我有封面"，不声明它在磁盘哪、更不把绝对路径或
URL 塞进前端。将来从 ``file://`` 换到资源服务器，只改这一个文件，插件声明与前端都不动。

**为什么声明是相对路径而不是文件名**：允许 ``image/cover.jpg`` 这类子目录写法，
但拒绝绝对路径与 ``..`` 越界——封面的归属就是自己的 ``resources/``，越界即配置错误。

**缺省即回落**：未声明、文件不存在、声明非法，一律返回 ``None``（记 WARNING），
由前端回落到 ``default_cover_url()`` 指向的通用默认封面。封面是装饰，不阻断模块运行。
"""

from __future__ import annotations

from pathlib import Path

from maaracing_master.core.logger import logger

# 核心资源根：通用默认封面与内置步骤的封面都在这里
CORE_RESOURCES: Path = Path(__file__).resolve().parent / "resources"

# 通用默认封面（无专属封面的模块回落到它）。换图只改这一个文件名。
DEFAULT_COVER = "cover_default.jpg"

# 插件资源根：<插件目录>/resources
_PLUGIN_RESOURCES_DIRNAME = "resources"


def _declared_file(base: Path, declared, owner: str) -> Path | None:
    """把声明解析成 base 内的真实文件；非法、越界、缺失一律 None（记 WARNING，不抛）。"""
    if not declared:
        return None
    if not isinstance(declared, str):
        logger.log(f"[covers] {owner}: COVER 应为字符串，已忽略: {declared!r}", "WARNING")
        return None
    rel = Path(declared)
    if rel.is_absolute() or ".." in rel.parts:
        logger.log(
            f"[covers] {owner}: COVER 必须是 resources/ 内的相对路径，已忽略: {declared}",
            "WARNING",
        )
        return None
    path = base / rel
    if not path.is_file():
        logger.log(f"[covers] {owner}: 封面文件不存在，回落默认封面: {path}", "WARNING")
        return None
    return path


def _url(path: Path | None) -> str | None:
    """文件 → 前端可用 URL；文件不存在时 None（默认封面缺失属于部署错误，不静默）。"""
    if path is None or not path.is_file():
        return None
    return path.resolve().as_uri()


def module_cover_url(module_id: str) -> str | None:
    """插件模块的封面 URL；未声明或文件缺失时 None。模块不存在抛 KeyError。"""
    # 延迟 import：本模块被 registry/sidecar 共用，顶层互相拉起来容易把循环 import
    # 变成"谁先加载谁说了算"的隐式契约。
    from maaracing_master.core.registry import MODULE_REGISTRY, get_plugin_dir

    cls = MODULE_REGISTRY[module_id]
    base = get_plugin_dir(module_id) / _PLUGIN_RESOURCES_DIRNAME
    return _url(_declared_file(base, getattr(cls, "COVER", ""), module_id))


def builtin_cover_url(step_id: str) -> str | None:
    """内置步骤的封面 URL（相对 core/resources/）；未声明或缺失时 None。步骤不存在抛 KeyError。"""
    from maaracing_master.core.builtin_steps import BUILTIN_STEPS

    cls = BUILTIN_STEPS[step_id]
    return _url(_declared_file(CORE_RESOURCES, getattr(cls, "COVER", ""), step_id))


def default_cover_url() -> str | None:
    """通用默认封面 URL；文件缺失时 None（前端此时退回纯渐变底）。"""
    return _url(CORE_RESOURCES / DEFAULT_COVER)


def cover_url_for(entry_id: str) -> str | None:
    """任务列表任意一项（内置步骤或插件模块）的封面 URL；未注册或未声明时 None。

    只有"没有封面"这一种结果返回 None——声明了但文件丢了同样返回 None（并记 WARNING），
    前端不必区分这两种情况，统一回落默认图。
    """
    try:
        return builtin_cover_url(entry_id)
    except KeyError:
        pass
    try:
        return module_cover_url(entry_id)
    except KeyError:
        logger.log(f"[covers] {entry_id}: 未注册的任务项，无封面", "DEBUG")
        return None
