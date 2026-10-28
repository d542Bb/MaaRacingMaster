# -*- coding: utf-8 -*-
"""性能面板契约——声明面校验与运行期取值规范化（纯函数，无状态）。

契约正文见 core/CODE_WIKI.md §1.15（声明面字段、校验规则、判级权归属）。
本模块只做两件事，都无副作用、不依赖任何具体模块：

1. **声明期校验**：模块类上的 ``PERF_ROWS`` 不合规 → 整行拒绝并给出原因；
2. **运行期规范化**：把模块返回的原始字典按声明折成行数组
   （值 / 等级 / 缺失原因），非法输入只让那一行失效。

第三层强制力（开发期）由此导出：模块自己的测试直接 import
:func:`validate_rows`，不合规在提交前跑红，不必等运行时才发现。

设计约束（勿破坏）：
- 不认识业务：不知道「拍率」是什么，只认识 kind 与阈值；
- 不猜：缺值给 None + 原因，不填 0、不夹紧、不用上一次的值；
- 逐行隔离：任一行的校验抛意外只影响该行。
"""

from __future__ import annotations

import math
import numbers
import re
import weakref
from typing import Any

__all__ = [
    "PERF_CONTRACT_VERSION", "KINDS", "STATS", "LEVELS", "CATEGORIES",
    "REASONS", "RESERVED_KEYS",
    "validate_rows", "rows_for", "normalize", "format_display",
    "all_missing",
]

# 声明契约版本：模块 PERF_CONTRACT 不等于此值 → 领域面整体降级（只显示资源面）
PERF_CONTRACT_VERSION = 1

KINDS = ("rate", "ratio", "duration", "count", "state")
STATS = ("instant", "p50_gap_inverse")
LEVELS = ("ok", "warn", "error", "idle")
CATEGORIES = ("player", "diagnostic")

# 缺失/失效原因（进样本与日志，面板一律显示「—」）
REASONS = (
    "not_reported",   # 声明了，本轮没给值
    "invalid",        # 类型不对 / 非有限值 / 状态文本无效
    "out_of_range",   # 数值越界
    "read_error",     # 取值口抛异常
)

# core 自有键名，模块不得占用
RESERVED_KEYS = ("cpu", "sys", "gpu", "procs", "threads", "meta")

MAX_PLAYER_ROWS = 3
MAX_DIAGNOSTIC_ROWS = 8
MAX_KEY_LEN = 32
MAX_LABEL_CHARS = 8
MAX_HINT_CHARS = 24
MAX_TEXT_CHARS = 24

_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# 单位由 kind 锁死，模块不能自带单位字符串
_UNIT = {"rate": " 次/秒", "duration": " ms", "count": " 个", "ratio": "", "state": ""}


# ---------------------------------------------------------------- 声明期校验


def _check_label(raw: Any) -> tuple[str | None, str | None, str | None]:
    """label 四步校验，顺序不可换。返回 (label, 降级告警, 拒绝原因)。"""
    if not isinstance(raw, str):
        return None, None, "label 不是字符串"
    if not _CJK_RE.search(raw):
        return None, None, "label 不含中文字符"
    label, warn = raw, None
    if len(label) > MAX_LABEL_CHARS:
        label = label[:MAX_LABEL_CHARS]
        warn = f"label 超 {MAX_LABEL_CHARS} 字已截断"
    # 截断后复核：防 abcdefgh中文 被截成纯英文
    if not _CJK_RE.search(label):
        return None, None, "label 截断后不含中文字符"
    return label, warn, None


def _check_thresholds(row: dict, kind: str) -> str | None:
    """阈值成对 + 顺序 + 有限性。返回拒绝原因或 None。

    方向默认按 kind：rate 越小越差（below），ratio/duration/count 越大越差
    （above）。显式声明优先。
    """
    has_warn = row.get("warn_at") is not None
    has_err = row.get("error_at") is not None
    if has_warn != has_err:
        return "阈值必须成对出现"
    if not has_warn:
        # 都不给：合法，该行不判级
        return "worse_when 无阈值可施加" if row.get("worse_when") else None
    for k in ("warn_at", "error_at"):
        v = row[k]
        if isinstance(v, bool) or not isinstance(v, numbers.Real):
            return f"{k} 不是数值"
        try:
            f = float(v)
        except (OverflowError, ValueError):
            return f"{k} 无法转成浮点"
        if not math.isfinite(f):
            return f"{k} 不是有限数值"
    w, e = float(row["warn_at"]), float(row["error_at"])
    if w == e:
        return "阈值重叠"
    default_dir = "below" if kind == "rate" else "above"
    if (row.get("worse_when") or default_dir) == "below":
        if not (e <= w):
            return "below 时要求 error_at ≤ warn_at"
    else:
        if not (w <= e):
            return "above 时要求 warn_at ≤ error_at"
    return None


def validate_rows(rows: Any, *, module_id: str = "",
                  max_player: int = MAX_PLAYER_ROWS,
                  max_diagnostic: int = MAX_DIAGNOSTIC_ROWS
                  ) -> tuple[list[dict], list[tuple[str, str]]]:
    """声明期校验：返回 (通过的行, [(被拒的 key 或下标, 原因)])。

    通过的行是**规范化副本**（label 可能已截断、precision 已补默认值），
    调用方应使用副本而不是原始声明。
    """
    accepted: list[dict] = []
    rejected: list[tuple[str, str]] = []
    if not isinstance(rows, (list, tuple)):
        return [], [("<PERF_ROWS>", "不是序列")]

    seen_keys: set[str] = set()
    seen_labels: set[str] = set()
    n_player = n_diag = 0

    for idx, raw in enumerate(rows):
        if not isinstance(raw, dict):
            rejected.append((f"#{idx}", "声明不是字典"))
            continue
        key = raw.get("key")
        tag = key if isinstance(key, str) and key else f"#{idx}"

        # 1) key：严格 snake_case + 长度
        if not isinstance(key, str) or not _KEY_RE.match(key) \
                or len(key) > MAX_KEY_LEN:
            rejected.append((tag, "key 不是合法 snake_case 或超长"))
            continue
        # 2) 保留名
        if key in RESERVED_KEYS:
            rejected.append((tag, "key 命中 core 保留名"))
            continue
        # 3) 同模块重复
        if key in seen_keys:
            rejected.append((tag, "key 重复"))
            continue

        kind = raw.get("kind")
        if kind not in KINDS:
            rejected.append((tag, f"kind 非法: {kind!r}"))
            continue

        category = raw.get("category", "player")
        if category not in CATEGORIES:
            rejected.append((tag, f"category 非法: {category!r}"))
            continue

        # 4) label 四步
        label, label_warn, label_err = _check_label(raw.get("label"))
        if label_err:
            rejected.append((tag, label_err))
            continue
        if label in seen_labels:
            rejected.append((tag, "label 同模块重复"))
            continue

        # 5) stat：rate 必填、非 rate 禁用
        stat = raw.get("stat")
        if kind == "rate":
            if stat not in STATS:
                rejected.append((tag, f"rate 缺 stat 或值非法: {stat!r}"))
                continue
        elif raw.get("stat") is not None:
            rejected.append((tag, "非 rate 不得声明 stat"))
            continue

        # 6) 阈值：state 不接受；其余成对可选
        if kind == "state":
            if any(raw.get(k) is not None
                   for k in ("warn_at", "error_at", "worse_when")):
                rejected.append((tag, "state 不接受阈值与 worse_when"))
                continue
            worse_when = None
        else:
            default_dir = "below" if kind == "rate" else "above"
            thr_err = _check_thresholds(raw, kind)
            if thr_err:
                rejected.append((tag, thr_err))
                continue
            worse_when = raw.get("worse_when") or default_dir

        # 7) 行数上限（超出的丢弃，不拒绝已通过的）
        if category == "player":
            n_player += 1
            if n_player > max_player:
                rejected.append((tag, f"player 行超上限 {max_player}"))
                continue
        else:
            n_diag += 1
            if n_diag > max_diagnostic:
                rejected.append((tag, f"diagnostic 行超上限 {max_diagnostic}"))
                continue

        # 8) precision：非法降级为 1，不拒绝整行
        precision = raw.get("precision", 1)
        if isinstance(precision, bool) or not isinstance(precision, int) \
                or not (0 <= precision <= 3):
            precision = 1
            label_warn = (label_warn + "；" if label_warn else "") + \
                "precision 非法已降级为 1"

        hint = raw.get("hint")
        if hint is not None and not isinstance(hint, str):
            hint = None
        if isinstance(hint, str) and len(hint) > MAX_HINT_CHARS:
            hint = hint[:MAX_HINT_CHARS]
            label_warn = (label_warn + "；" if label_warn else "") + \
                f"hint 超 {MAX_HINT_CHARS} 字已截断"

        row = {
            "key": key,
            "label": label,
            "kind": kind,
            "category": category,
            "precision": precision,
            "stat": stat if kind == "rate" else None,
            "worse_when": worse_when,
            "warn_at": float(raw["warn_at"]) if raw.get("warn_at") is not None else None,
            "error_at": float(raw["error_at"]) if raw.get("error_at") is not None else None,
            "hint": hint,
        }
        accepted.append(row)
        seen_keys.add(key)
        seen_labels.add(label)

    return accepted, rejected


# 校验结果缓存：模块类/实例只校验一次（声明是静态的）。
# WeakKeyDictionary 避免 id 复用误命中；模块对象不可弱引用时退化为每次校验。
_ROW_CACHE: "weakref.WeakKeyDictionary[Any, list[dict]]" = \
    weakref.WeakKeyDictionary()


def rows_for(module: Any) -> list[dict]:
    """取模块的已校验声明行（带缓存）。模块没有声明 → 空列表。"""
    if module is None:
        return []
    try:
        cached = _ROW_CACHE.get(module)
    except TypeError:
        cached = None
    if cached is not None:
        return cached

    from maaracing_master.core.logger import logger

    module_id = str(getattr(module, "module_id", "") or type(module).__name__)
    version = getattr(module, "PERF_CONTRACT", None)
    rows = getattr(module, "PERF_ROWS", ())
    if version is None and not rows:
        accepted: list[dict] = []       # 未声明：不告警，只显示资源面
    elif version != PERF_CONTRACT_VERSION:
        logger.log(f"[perf] {module_id} 契约版本 {version!r} 不认识"
                   f"（期望 {PERF_CONTRACT_VERSION}），领域面整体不显示",
                   "WARNING")
        accepted = []
    else:
        accepted, rejected = validate_rows(rows, module_id=module_id)
        for tag, why in rejected:
            logger.log(f"[perf] {module_id} 声明行 {tag} 被拒：{why}", "WARNING")

    try:
        _ROW_CACHE[module] = accepted
    except TypeError:
        pass
    return accepted


# ---------------------------------------------------------------- 运行期规范化


def _number(value: Any) -> tuple[float | None, str | None]:
    """五道闸的前两闸：类型（拒布尔/非 Real）+ 有限性。返回 (值, 原因)。"""
    if isinstance(value, bool):
        return None, "invalid"
    if not isinstance(value, numbers.Real):
        return None, "invalid"
    try:
        f = float(value)
    except (OverflowError, ValueError, TypeError):
        return None, "invalid"
    if not math.isfinite(f):
        return None, "invalid"
    return f, None


def _domain(kind: str, value: float) -> str | None:
    """第三闸：合法域。越界一律拒绝，不夹紧。"""
    if kind == "ratio":
        return None if 0.0 <= value <= 1.0 else "out_of_range"
    if kind in ("rate", "duration"):
        return None if value >= 0.0 else "out_of_range"
    if kind == "count":
        if value < 0.0:
            return "out_of_range"
        return None if float(value).is_integer() else "out_of_range"
    return None


def _state_text(raw: Any) -> tuple[str | None, str | None]:
    """state.text 五步：类型 → 规范化 → 截断 → 查空白 → 保留。"""
    if not isinstance(raw, str):
        return None, "invalid"
    text = _CTRL_RE.sub(" ", raw)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    if not text.strip():
        return None, "invalid"
    return text, None


def _level(row: dict, value: float) -> str:
    """第四闸：判级。没有阈值 → 恒 ok（不判级，不染色）。"""
    warn, err = row.get("warn_at"), row.get("error_at")
    if warn is None or err is None:
        return "ok"
    below = (row.get("worse_when") or "below") == "below"
    if below:
        if value <= err:
            return "error"
        if value <= warn:
            return "warn"
    else:
        if value >= err:
            return "error"
        if value >= warn:
            return "warn"
    return "ok"


def format_display(row: dict, value: float | None) -> str:
    """显示串（单位由 kind 锁死，模块不自带单位）。"""
    if value is None:
        return "—"
    kind = row["kind"]
    if kind == "ratio":
        pct = value * 100.0
        prec = row.get("precision", 1)
        return f"{pct:.{prec}f}%"
    if kind == "count":
        return f"{int(round(value))}"
    prec = row.get("precision", 1)
    return f"{value:.{prec}f}{_UNIT.get(kind, '')}"


def normalize(rows: list[dict], raw: Any) -> tuple[list[dict], list[str]]:
    """按声明规范化模块返回的字典。返回 (行数组, 未声明的键列表)。

    行结构：``{key,label,kind,category,value,display,level,reason,hint}``
    - ``value`` 为 None 表示不可用，``reason`` 说明为什么（见 REASONS）；
    - 状态行的 ``display`` 是文本，``value`` 恒 None；
    - 任何单行意外只让该行 ``invalid``，不影响同批其它行。
    """
    src = raw if isinstance(raw, dict) else {}
    declared = {r["key"] for r in rows}
    unknown = [k for k in src.keys() if k not in declared] if src else []

    out: list[dict] = []
    for row in rows:
        item = {
            "key": row["key"], "label": row["label"], "kind": row["kind"],
            "category": row["category"], "value": None, "display": "—",
            "level": "idle", "reason": None, "hint": row.get("hint"),
        }
        try:
            if row["key"] not in src:
                item["reason"] = "not_reported"
                out.append(item)
                continue
            value = src[row["key"]]

            if row["kind"] == "state":
                if not isinstance(value, dict):
                    item["reason"] = "invalid"
                    out.append(item)
                    continue
                lvl = value.get("level")
                text, err = _state_text(value.get("text"))
                if text is None:
                    item["reason"] = err or "invalid"
                    out.append(item)
                    continue
                # 等级非法只降级（文本还在，能说明问题）
                item["level"] = lvl if lvl in LEVELS else "idle"
                item["display"] = text
                out.append(item)
                continue

            num, why = _number(value)
            if num is None:
                item["reason"] = why or "invalid"
                out.append(item)
                continue
            dom = _domain(row["kind"], num)
            if dom is not None:
                item["reason"] = dom
                out.append(item)
                continue
            item["value"] = num
            item["display"] = format_display(row, num)
            item["level"] = _level(row, num)
        except Exception:  # noqa: BLE001 —— 逐行隔离：单行意外不牵连同批
            item.update({"value": None, "display": "—", "level": "idle",
                         "reason": "invalid"})
        out.append(item)
    return out, unknown


def all_missing(rows: list[dict], reason: str = "read_error") -> list[dict]:
    """整批失效（取值口抛异常时用）：所有声明行统一标一个原因。"""
    out = []
    for row in rows:
        out.append({
            "key": row["key"], "label": row["label"], "kind": row["kind"],
            "category": row["category"], "value": None, "display": "—",
            "level": "idle", "reason": reason, "hint": row.get("hint"),
        })
    return out
