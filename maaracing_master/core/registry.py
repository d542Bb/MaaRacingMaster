# -*- coding: utf-8 -*-
"""活动模块注册表：自动扫描 plugins/*/manifest.py 发现插件，并提供查询与创建。

- 插件 = plugins/ 下每个含 manifest.py 的子目录；
- manifest 契约：ID（唯一标识）+ MODULE_CLASS（定位模块类）；
  NAME / STAGE_ORDER / REQUIRES / REQUIRES_GAMEPAD_EXCLUSIVE / REQUIRED_ASSETS 从模块类读取（单一来源）；
- 可选的活动有效期：manifest 里的 VALID_FROM / VALID_UNTIL（ISO 8601，两端均含，
  未声明 = 永久有效）。过期模块照常注册（GUI 仍需列出并置灰），只是不再自动选中，
  显式选择必须带 force；
- 单插件加载失败（如缺依赖）仅记 WARNING 跳过，不阻断整体；
- 剥离 = 删目录，安装 = 丢入自包含目录，GUI 列表自动随之变化。
"""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger
from maaracing_master.core.module_validity import in_window, is_expired, parse_endpoint

MODULE_REGISTRY: dict[str, type[ActivityModule]] = {}

# 插件根目录：maaracing_master/plugins
_PLUGINS_ROOT = Path(__file__).resolve().parent.parent / "plugins"

# 已注册插件的物理目录（mod_id → plugins/<name>/），供资源存在性校验定位
_PLUGIN_DIRS: dict[str, Path] = {}

# 已注册插件声明的有效期（mod_id → (VALID_FROM, VALID_UNTIL)），未声明的一端为 None
_PLUGIN_VALIDITY: dict[str, tuple[datetime | None, datetime | None]] = {}


def _parse_validity(raw, plugin: str, field: str) -> datetime | None:
    """解析有效期端点；非法值记 WARNING 并按「未声明」处理（不阻断插件加载）。"""
    dt, err = parse_endpoint(raw, field)
    if err:
        logger.log(f"[registry] 插件 {plugin}: {err}，按未声明处理", "WARNING")
    return dt


def _discover_plugins() -> None:
    """扫描 plugins/*/manifest.py 并注册模块类（幂等、容错）。"""
    if not _PLUGINS_ROOT.is_dir():
        return
    for entry in sorted(_PLUGINS_ROOT.iterdir()):
        if not entry.is_dir() or not (entry / "manifest.py").exists():
            continue
        pkg = f"maaracing_master.plugins.{entry.name}"
        try:
            manifest = importlib.import_module(f"{pkg}.manifest")
            mod_id = getattr(manifest, "ID", None)
            cls_ref = getattr(manifest, "MODULE_CLASS", None)
            if not mod_id or not cls_ref or mod_id in MODULE_REGISTRY:
                continue
            path, _, attr = cls_ref.rpartition(".")
            mod = importlib.import_module(f"{pkg}.{path}" if path else pkg)
            cls = getattr(mod, attr)
            if not (isinstance(cls, type) and issubclass(cls, ActivityModule)):
                logger.log(f"[registry] 插件 {entry.name}: {cls_ref} 非 ActivityModule 子类，跳过", "WARNING")
                continue
            MODULE_REGISTRY[mod_id] = cls
            _PLUGIN_DIRS[mod_id] = entry
            _PLUGIN_VALIDITY[mod_id] = (
                _parse_validity(getattr(manifest, "VALID_FROM", None), entry.name, "VALID_FROM"),
                _parse_validity(getattr(manifest, "VALID_UNTIL", None), entry.name, "VALID_UNTIL"),
            )
            logger.log(f"[registry] 已注册插件模块: {mod_id}", "DEBUG")
        except Exception as exc:  # noqa: BLE001 —— 缺依赖等按插件隔离，不影响其它插件
            logger.log(f"[registry] 插件 {entry.name} 加载失败，跳过: {exc!r}", "WARNING")


_discover_plugins()


def module_validity(module_id: str) -> tuple[datetime | None, datetime | None]:
    """模块声明的有效期两端（未声明的一端为 None），模块不存在时抛出 KeyError(module_id)"""
    if module_id not in MODULE_REGISTRY:
        raise KeyError(module_id)
    return _PLUGIN_VALIDITY.get(module_id, (None, None))


def module_expired(module_id: str, now: datetime | None = None) -> bool:
    """模块是否已过有效期（未声明 VALID_UNTIL = 永不过期）。"""
    _, until = module_validity(module_id)
    return is_expired(until, now)


def module_available(module_id: str, now: datetime | None = None) -> bool:
    """模块当前是否处于有效期内（未声明的一端不构成限制）。"""
    start, until = module_validity(module_id)
    return in_window(start, until, now)


def first_available_module_id(now: datetime | None = None) -> str | None:
    """注册顺序里第一个仍在有效期内的模块 id；全部不可用时返回 None。"""
    for mid in MODULE_REGISTRY:
        if module_available(mid, now):
            return mid
    return None


def get_module_info(module_id: str) -> dict:
    """获取模块元信息（含声明有效期与当前过期判定），模块不存在时抛出 KeyError(module_id)"""
    cls = MODULE_REGISTRY[module_id]
    valid_from, valid_until = module_validity(module_id)
    return {
        "id": module_id,
        "name": cls.NAME,
        "stages": cls.STAGE_ORDER,
        "requires": sorted(cls.REQUIRES),
        "requires_gamepad_exclusive": cls.REQUIRES_GAMEPAD_EXCLUSIVE,
        "required_assets": tuple(getattr(cls, "REQUIRED_ASSETS", ())),
        "valid_from": valid_from.isoformat() if valid_from else None,
        "valid_until": valid_until.isoformat() if valid_until else None,
        "expired": module_expired(module_id),
    }


def get_plugin_dir(module_id: str) -> Path:
    """获取插件物理目录，模块不存在时抛出 KeyError(module_id)"""
    return _PLUGIN_DIRS[module_id]


def check_required_assets(module_id: str) -> list[str]:
    """检查插件 REQUIRED_ASSETS 声明的资源是否齐全，返回缺失项的完整路径列表。"""
    missing: list[str] = []
    for rel in get_module_info(module_id)["required_assets"]:
        if not (get_plugin_dir(module_id) / rel).is_file():
            missing.append(str(Path("plugins") / module_id / rel))
    return missing


def create_module(module_id: str, ctx: ActivityContext) -> ActivityModule:
    """创建模块实例，模块不存在时抛出 KeyError(module_id)"""
    return MODULE_REGISTRY[module_id](ctx)
