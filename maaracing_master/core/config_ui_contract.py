# -*- coding: utf-8 -*-
"""模块配置表单契约：模块声明字段，宿主通用渲染。

**这是什么**：活动模块通过 `CONFIG_UI_CONTRACT`（契约版本）与 `CONFIG_UI`（分组字段）
自述配置面板；sidecar 把规范化结果随 `get_module_config` 下发，GUI 按类型渲染，
不写任何模块字段名。摘要与运行状态同样由模块产出（`config_summary` / `config_status`），
宿主只做类型、长度与白名单校验。

**为什么不引入完整 JSON Schema / 插件自带 HTML**：前端是 file:// 静态单页、无构建链，
信任边界只能靠「重建式白名单 + textContent」；插件如果自带标记或脚本，注入面不可控。
缺口优先扩控件类型并升契约版本，不开自带 UI 的口子。

**降级口径**：契约问题只让配置面板失联，不打断配置读写。状态分四级，见 `ConfigUiResult`。
"""
from __future__ import annotations

from dataclasses import dataclass

CONFIG_UI_CONTRACT_VERSION = 1

FIELD_TYPES = frozenset({"switch", "segmented", "select", "number", "text"})
COND_OPS = frozenset({"equals", "not_equals", "in"})
STATUS_LEVELS = frozenset({"", "ok", "warn", "error"})

MAX_GROUPS = 4
MAX_FIELDS = 12
MAX_OPTIONS = 16
MAX_VARIANTS = 8
MAX_LABEL = 40
MAX_DESCRIPTION = 240
MAX_HINT = 120
MAX_SUFFIX = 8
MAX_PLACEHOLDER = 60
MAX_SUMMARY_CHIPS = 4
MAX_SUMMARY_CHIP = 80
MAX_STATUS = 320

UI_NONE = "none"
UI_OK = "ok"
UI_PARTIAL = "partial"
UI_INVALID = "invalid"
UI_UNSUPPORTED = "unsupported_version"

_NOTE_PARTIAL = "部分配置项声明无效，详见日志"
_NOTE_INVALID = "配置面板声明无效，详见日志"


@dataclass(frozen=True)
class ConfigUiResult:
    """配置表单规范化结果。

    state：none=未声明（按设计无面板）| ok | partial=部分字段被丢弃 | invalid=声明不可用 |
    unsupported_version=模块版本与宿主不符。schema 仅在 ok/partial 时非 None。
    """

    state: str
    schema: dict | None = None
    warnings: tuple[str, ...] = ()
    note: str = ""


def _is_scalar(value) -> bool:
    return isinstance(value, (str, bool, int, float))


def _text(value, limit: int) -> str | None:
    """字符串字段：非字符串 → None；去空白、超长截断。"""
    if not isinstance(value, str):
        return None
    return value.strip()[:limit]


def _normalize_cond(raw) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "条件必须是对象"
    key = raw.get("key")
    op = raw.get("op")
    if not isinstance(key, str) or not key:
        return None, "条件缺少 key"
    if op not in COND_OPS:
        return None, f"条件运算符不受支持: {op!r}"
    value = raw.get("value")
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            return None, "in 条件需要非空数组"
        vals = [v for v in value if _is_scalar(v)]
        if len(vals) != len(value):
            return None, "in 条件含非标量值"
        return {"key": key, "op": op, "value": vals}, None
    if not _is_scalar(value):
        return None, f"条件值必须是标量: {value!r}"
    return {"key": key, "op": op, "value": value}, None


def _normalize_options(raw) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    if not isinstance(raw, (list, tuple)) or not raw:
        return [], ["选项列表为空"]
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw[:MAX_OPTIONS]:
        if not isinstance(item, dict):
            warnings.append("选项必须是对象")
            continue
        value = item.get("value")
        label = _text(item.get("label"), MAX_LABEL)
        if not _is_scalar(value) or not label:
            warnings.append(f"选项缺少合法 value/label: {item!r}")
            continue
        token = repr(value)
        if token in seen:
            warnings.append(f"选项值重复: {value!r}")
            continue
        seen.add(token)
        out.append({"value": value, "label": label})
    if len(raw) > MAX_OPTIONS:
        warnings.append(f"选项超过上限 {MAX_OPTIONS}，多余项已丢弃")
    if not out:
        warnings.append("没有可用选项")
    return out, warnings


def _normalize_field(raw, allowed_keys: set[str], seen: set[str],
                     warnings: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        warnings.append("字段必须是对象")
        return None
    key = raw.get("key")
    if not isinstance(key, str) or key not in allowed_keys:
        warnings.append(f"字段键不在配置面内: {key!r}")
        return None
    if key in seen:
        warnings.append(f"字段键重复: {key}")
        return None
    ftype = raw.get("type")
    if ftype not in FIELD_TYPES:
        warnings.append(f"{key}: 控件类型不受支持: {ftype!r}")
        return None
    label = _text(raw.get("label"), MAX_LABEL)
    if not label:
        warnings.append(f"{key}: 缺少 label")
        return None

    field: dict = {"key": key, "type": ftype, "label": label}
    for name, limit in (("description", MAX_DESCRIPTION), ("hint", MAX_HINT),
                        ("suffix", MAX_SUFFIX), ("placeholder", MAX_PLACEHOLDER)):
        if name not in raw:
            continue
        text = _text(raw[name], limit)
        if text is None:
            warnings.append(f"{key}: {name} 必须是字符串，已忽略")
            continue
        if text:
            field[name] = text

    options_raw = raw.get("options")
    choices = raw.get("choices")
    if choices is not None and not isinstance(choices, str):
        warnings.append(f"{key}: choices 必须是配置候选键名，已忽略")
        choices = None
    if ftype == "segmented":
        if choices:
            warnings.append(f"{key}: 分段控件不支持动态候选，choices 已忽略")
            choices = None
        options, opt_warns = _normalize_options(options_raw)
        warnings.extend(f"{key}: {w}" for w in opt_warns)
        if not options:
            return None
        field["options"] = options
    elif ftype == "select":
        if options_raw is not None and choices:
            warnings.append(f"{key}: options 与 choices 互斥，字段已丢弃")
            return None
        if choices:
            field["choices"] = choices
        else:
            options, opt_warns = _normalize_options(options_raw)
            warnings.extend(f"{key}: {w}" for w in opt_warns)
            if not options:
                return None
            field["options"] = options
    elif ftype == "number":
        if options_raw is not None or choices:
            warnings.append(f"{key}: 数字控件不接受选项，已忽略")
        lo = raw.get("min")
        hi = raw.get("max")
        step = raw.get("step")
        for name, value in (("min", lo), ("max", hi), ("step", step)):
            if value is not None and (not _is_scalar(value) or isinstance(value, (str, bool))):
                warnings.append(f"{key}: {name} 必须是数字，已忽略")
                if name == "min":
                    lo = None
                elif name == "max":
                    hi = None
                else:
                    step = None
        if lo is not None and hi is not None and lo > hi:
            warnings.append(f"{key}: min 大于 max，约束已忽略")
            lo = hi = None
        if step is not None and step <= 0:
            warnings.append(f"{key}: step 必须为正数，约束已忽略")
            step = None
        for name, value in (("min", lo), ("max", hi), ("step", step)):
            if value is not None:
                field[name] = value
    else:  # switch / text
        if options_raw is not None or choices:
            warnings.append(f"{key}: 该控件不接受选项，已忽略")

    if "visible_if" in raw:
        cond, problem = _normalize_cond(raw["visible_if"])
        if problem:
            warnings.append(f"{key}: {problem}，显隐条件已忽略")
        else:
            field["visible_if"] = cond

    variants_raw = raw.get("variants")
    if variants_raw is not None:
        if not isinstance(variants_raw, (list, tuple)):
            warnings.append(f"{key}: variants 必须是数组，已忽略")
        else:
            variants: list[dict] = []
            for item in variants_raw[:MAX_VARIANTS]:
                if not isinstance(item, dict):
                    warnings.append(f"{key}: 变体必须是对象")
                    continue
                cond, problem = _normalize_cond(item.get("when"))
                if problem:
                    warnings.append(f"{key}: 变体条件非法（{problem}），已忽略")
                    continue
                variant: dict = {"when": cond}
                for name, limit in (("label", MAX_LABEL), ("description", MAX_DESCRIPTION)):
                    if name in item:
                        text = _text(item[name], limit)
                        if text is None:
                            warnings.append(f"{key}: 变体 {name} 必须是字符串，已忽略")
                        elif text:
                            variant[name] = text
                if "label" not in variant and "description" not in variant:
                    warnings.append(f"{key}: 变体没有可覆盖的文案，已忽略")
                    continue
                variants.append(variant)
            if len(variants_raw) > MAX_VARIANTS:
                warnings.append(f"{key}: 变体超过上限 {MAX_VARIANTS}，多余项已丢弃")
            if variants:
                field["variants"] = variants
    seen.add(key)
    return field


def normalize_config_ui(cls, allowed_keys) -> ConfigUiResult:
    """把模块类上的表单声明规范化成宿主可渲染的结构。

    `allowed_keys` = `DEFAULT_MODULE_CONFIG` 的键集；字段键不在其中即丢弃。
    重建式白名单：只复制认可的成员，未知成员静默丢弃。
    """
    version = getattr(cls, "CONFIG_UI_CONTRACT", None)
    raw = getattr(cls, "CONFIG_UI", None)
    if version is None and raw is None:
        return ConfigUiResult(UI_NONE)
    if version is None or raw is None:
        return ConfigUiResult(
            UI_INVALID, None,
            ("声明不完整：CONFIG_UI 与 CONFIG_UI_CONTRACT 必须同时提供",),
            _NOTE_INVALID)
    if version != CONFIG_UI_CONTRACT_VERSION:
        note = f"配置面板版本不受支持（模块 v{version} / 宿主 v{CONFIG_UI_CONTRACT_VERSION}）"
        return ConfigUiResult(
            UI_UNSUPPORTED, None,
            (f"版本不符: 模块 v{version}，宿主 v{CONFIG_UI_CONTRACT_VERSION}",), note)
    if not isinstance(raw, dict) or not isinstance(raw.get("groups"), (list, tuple)):
        return ConfigUiResult(UI_INVALID, None, ("CONFIG_UI 必须是含 groups 数组的对象",),
                              _NOTE_INVALID)

    allowed = set(allowed_keys)
    warnings: list[str] = []
    groups: list[dict] = []
    seen: set[str] = set()
    total_fields = 0
    for group_raw in list(raw["groups"])[:MAX_GROUPS + 1]:
        if total_fields >= MAX_FIELDS:
            warnings.append(f"字段超过上限 {MAX_FIELDS}，多余项已丢弃")
            break
        if not isinstance(group_raw, dict):
            warnings.append("分组必须是对象")
            continue
        fields_raw = group_raw.get("fields")
        if not isinstance(fields_raw, (list, tuple)):
            warnings.append("分组缺少 fields 数组")
            continue
        fields: list[dict] = []
        for field_raw in fields_raw:
            if total_fields >= MAX_FIELDS:
                warnings.append(f"字段超过上限 {MAX_FIELDS}，多余项已丢弃")
                break
            field = _normalize_field(field_raw, allowed, seen, warnings)
            if field is None:
                continue
            fields.append(field)
            total_fields += 1
        if not fields:
            continue
        group: dict = {"fields": fields}
        title = _text(group_raw.get("title"), MAX_LABEL)
        if title:
            group["title"] = title
        groups.append(group)
    if len(raw["groups"]) > MAX_GROUPS:
        warnings.append(f"分组超过上限 {MAX_GROUPS}，多余项已丢弃")

    # 条件只能引用本次真正渲染出来的字段；引用了不存在的字段 → 丢条件（字段常显）
    keys = {f["key"] for g in groups for f in g["fields"]}
    for group in groups:
        for field in group["fields"]:
            cond = field.get("visible_if")
            if cond is not None and cond["key"] not in keys:
                warnings.append(f"{field['key']}: 显隐条件引用了未声明字段 {cond['key']}，已忽略")
                del field["visible_if"]
            variants = field.get("variants")
            if variants:
                kept = []
                for variant in variants:
                    if variant["when"]["key"] in keys:
                        kept.append(variant)
                    else:
                        warnings.append(
                            f"{field['key']}: 变体条件引用了未声明字段 "
                            f"{variant['when']['key']}，已忽略")
                if kept:
                    field["variants"] = kept
                else:
                    del field["variants"]

    if not groups:
        return ConfigUiResult(UI_INVALID, None,
                              tuple(warnings + ["没有可渲染的字段"]), _NOTE_INVALID)
    state = UI_PARTIAL if warnings else UI_OK
    return ConfigUiResult(state, {"groups": groups}, tuple(warnings),
                          _NOTE_PARTIAL if warnings else "")


def normalize_summary(raw) -> tuple[list[str], str | None]:
    """摘要规范化；返回 (chips, 问题描述)。仅接受字符串列表。"""
    if not isinstance(raw, (list, tuple)):
        return [], "摘要必须是字符串数组"
    problem = None
    chips: list[str] = []
    for item in raw[:MAX_SUMMARY_CHIPS]:
        if not isinstance(item, str):
            problem = "摘要含非字符串项"
            continue
        text = item.strip()[:MAX_SUMMARY_CHIP]
        if text:
            chips.append(text)
    if len(raw) > MAX_SUMMARY_CHIPS:
        problem = f"摘要超过 {MAX_SUMMARY_CHIPS} 条"
    return chips, problem


def normalize_status(raw) -> tuple[dict | None, str | None]:
    """运行状态规范化；返回 ({text, level}, 问题描述)。None 表示模块未提供状态。"""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "状态必须是对象"
    text = raw.get("text")
    level = raw.get("level", "")
    if not isinstance(text, str) or not text.strip():
        return None, "状态缺少 text"
    if level not in STATUS_LEVELS:
        return None, f"状态等级不受支持: {level!r}"
    return {"text": text.strip()[:MAX_STATUS], "level": level}, None
