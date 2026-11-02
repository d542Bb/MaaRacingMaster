# -*- coding: utf-8 -*-
"""数据看板内容契约——模块声明 block，core 校验，前端统一渲染。

契约正文见 core/CODE_WIKI.md §1.20。本模块只做两件事，都不依赖任何具体模块：

1. **校验与规范化**：模块返回的 ``read_dashboard()`` 载荷按版本与 block 白名单
   过滤、截断、丢弃非法项；非法项不阻断整张看板。
2. **词表收口**：block 类型、色调与标准动作是共享词表；新增视觉原语时在这里加一次，
   前端同步加渲染器，模块只组合已有 block。

设计约束（勿破坏）：
- **不认识业务**：不知道「银币盈亏」是什么，只认识 label/value/tone 这类通用字段；
- **不猜**：非法块丢弃并给 warning，不把坏数据伪装成空字符串；
- **不信任模块**：动作只收标准白名单，文本由前端转义，后端不下发 HTML；
- **兼容旧模块**：``today_stats`` 是给 pre-contract 模块（当前为冻结的鉴宝）的兼容块，
  它的 ``data`` 原样透传，由前端对应渲染器解释；新模块用 stats/list 等标准块。
"""

from __future__ import annotations

import re
from typing import Any

DASHBOARD_CONTRACT_VERSION = 1

# block 白名单。today_stats 是兼容块，不是新模块的推荐形态。
BLOCK_TYPES = frozenset({"stats", "list", "text", "path", "action", "today_stats"})

# 色调只影响前端样式，不允许任意类名/内联样式。
TONES = frozenset({"neutral", "positive", "negative", "muted", "warn"})

# 标准动作白名单：模块只能引用这些能力，不能自带 RPC 名或 URL。
ACTIONS = frozenset({"open_user_data_folder"})

MAX_BLOCKS = 12
MAX_STATS = 8
MAX_ROWS = 50
MAX_TITLE = 40
MAX_SUBTITLE = 120
MAX_BADGE = 24
MAX_LABEL = 60
MAX_VALUE = 64
MAX_SUB = 80
MAX_TEXT = 2000

# 允许换行与制表；其余控制字符拒绝（避免布局/日志被破坏）。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DashboardContractError(ValueError):
    """看板载荷整体不可用（版本不支持、顶层形状错误等）。"""


def _text(
    value: Any,
    field: str,
    limit: int,
    warnings: list[str],
    *,
    required: bool = True,
) -> str | None:
    """字符串字段：类型 / 控制字符 / 长度；可选字段空串视为未提供。"""
    if value is None:
        if required:
            warnings.append(f"{field} 缺失")
        return None
    if not isinstance(value, str):
        warnings.append(f"{field} 不是字符串")
        return None
    if _CONTROL_RE.search(value):
        warnings.append(f"{field} 含控制字符")
        return None
    text = value
    if len(text) > limit:
        text = text[:limit]
        warnings.append(f"{field} 超 {limit} 字已截断")
    if required and not text.strip():
        warnings.append(f"{field} 为空")
        return None
    return text or None


def _tone(value: Any, field: str, warnings: list[str]) -> str:
    if value is None:
        return "neutral"
    if not isinstance(value, str) or value not in TONES:
        warnings.append(f"{field} 非法: {value!r}")
        return "neutral"
    return value


def _badge(value: Any, warnings: list[str]) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        warnings.append("badge 不是对象")
        return None
    text = _text(value.get("text"), "badge.text", MAX_BADGE, warnings)
    if text is None:
        return None
    return {"text": text, "tone": _tone(value.get("tone"), "badge.tone", warnings)}


def _stats(block: dict, warnings: list[str]) -> dict | None:
    items = block.get("items")
    if not isinstance(items, list):
        warnings.append("stats.items 不是列表")
        return None
    if len(items) > MAX_STATS:
        warnings.append(f"stats.items 超过 {MAX_STATS} 项，已截断")
    out: list[dict] = []
    for index, item in enumerate(items[:MAX_STATS]):
        if not isinstance(item, dict):
            warnings.append(f"stats[{index}] 不是对象")
            continue
        label = _text(item.get("label"), f"stats[{index}].label", MAX_LABEL, warnings)
        value = _text(item.get("value"), f"stats[{index}].value", MAX_VALUE, warnings)
        if label is None or value is None:
            continue
        entry = {
            "label": label,
            "value": value,
            "tone": _tone(item.get("tone"), f"stats[{index}].tone", warnings),
        }
        sub = _text(item.get("sub"), f"stats[{index}].sub", MAX_SUB, warnings, required=False)
        if sub is not None:
            entry["sub"] = sub
        if item.get("dots") is True:
            entry["dots"] = True
        out.append(entry)
    return {"type": "stats", "items": out} if out else None


def _list(block: dict, warnings: list[str]) -> dict | None:
    rows = block.get("rows")
    if not isinstance(rows, list):
        warnings.append("list.rows 不是列表")
        return None
    if len(rows) > MAX_ROWS:
        warnings.append(f"list.rows 超过 {MAX_ROWS} 行，已截断")
    out: list[dict] = []
    for index, row in enumerate(rows[:MAX_ROWS]):
        if not isinstance(row, dict):
            warnings.append(f"list[{index}] 不是对象")
            continue
        text = _text(row.get("text"), f"list[{index}].text", MAX_TEXT, warnings)
        if text is None:
            continue
        leading = _text(row.get("leading"), f"list[{index}].leading", MAX_LABEL, warnings, required=False)
        trailing = _text(row.get("trailing"), f"list[{index}].trailing", MAX_VALUE, warnings, required=False)
        entry = {"leading": leading or "", "text": text, "trailing": trailing or ""}
        badge = _badge(row.get("badge"), warnings)
        if badge is not None:
            entry["badge"] = badge
        out.append(entry)
    return {"type": "list", "rows": out} if out else None


def _plain_text(block: dict, kind: str, warnings: list[str]) -> dict | None:
    text = _text(block.get("text"), f"{kind}.text", MAX_TEXT, warnings)
    return {"type": kind, "text": text} if text is not None else None


def _action(block: dict, warnings: list[str]) -> dict | None:
    action = block.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        warnings.append(f"action 不在白名单: {action!r}")
        return None
    label = _text(block.get("label"), "action.label", MAX_LABEL, warnings)
    if label is None:
        return None
    return {"type": "action", "label": label, "action": action}


def _today_stats(block: dict, warnings: list[str]) -> dict | None:
    """兼容块：data 原样透传，由前端对应渲染器解释。"""
    data = block.get("data")
    if not isinstance(data, dict):
        warnings.append("today_stats.data 不是对象")
        return None
    return {"type": "today_stats", "data": data}


_BLOCK_NORMALIZERS = {
    "stats": _stats,
    "list": _list,
    "text": lambda block, warnings: _plain_text(block, "text", warnings),
    "path": lambda block, warnings: _plain_text(block, "path", warnings),
    "action": _action,
    "today_stats": _today_stats,
}


def normalize_dashboard(payload: Any) -> tuple[dict, tuple[str, ...]]:
    """校验并规范化整张看板；返回 (规范化载荷, warnings)。

    顶层形状/版本错误抛 :class:`DashboardContractError`；单块非法只丢弃该块。
    """
    warnings: list[str] = []
    if not isinstance(payload, dict):
        raise DashboardContractError("看板载荷必须是对象")
    version = payload.get("version")
    if version != DASHBOARD_CONTRACT_VERSION:
        raise DashboardContractError(f"看板契约版本不支持: {version!r}")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise DashboardContractError("看板 blocks 必须是列表")
    if len(blocks) > MAX_BLOCKS:
        warnings.append(f"blocks 超过 {MAX_BLOCKS} 个，已截断")

    out_blocks: list[dict] = []
    for index, block in enumerate(blocks[:MAX_BLOCKS]):
        if not isinstance(block, dict):
            warnings.append(f"blocks[{index}] 不是对象")
            continue
        kind = block.get("type")
        normalizer = _BLOCK_NORMALIZERS.get(kind)
        if normalizer is None:
            warnings.append(f"blocks[{index}] 未知类型: {kind!r}")
            continue
        normalized = normalizer(block, warnings)
        if normalized is not None:
            out_blocks.append(normalized)

    result: dict[str, Any] = {
        "version": DASHBOARD_CONTRACT_VERSION,
        "blocks": out_blocks,
    }
    title = _text(payload.get("title"), "title", MAX_TITLE, warnings, required=False)
    subtitle = _text(payload.get("subtitle"), "subtitle", MAX_SUBTITLE, warnings, required=False)
    badge = _badge(payload.get("badge"), warnings)
    if title is not None:
        result["title"] = title
    if subtitle is not None:
        result["subtitle"] = subtitle
    if badge is not None:
        result["badge"] = badge
    return result, tuple(warnings)


__all__ = [
    "DASHBOARD_CONTRACT_VERSION",
    "BLOCK_TYPES",
    "TONES",
    "ACTIONS",
    "DashboardContractError",
    "normalize_dashboard",
]
