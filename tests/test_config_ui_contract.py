# -*- coding: utf-8 -*-
"""模块配置表单契约（core/config_ui_contract.py）与 sidecar 注入的回归锁。

覆盖四件事：
1. 契约状态必须可区分：none / ok / partial / invalid / unsupported_version；
2. 重建式白名单：未知键、未知成员、非法条件都要被丢弃，不能把标记带进前端；
3. 摘要入参是投影后的有效配置（默认值合并、旧键折算、无 _ 元数据、副本隔离）；
4. 配置修订号 _rev 单调，静态与 live 两条读取路径都携带。
"""
from __future__ import annotations

import threading

import pytest

from maaracing_master.core import config_ui_contract as C
from maaracing_master.core import sidecar as sc

try:
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


# ---------- 契约纯函数 ----------

class _None:
    pass


class _VersionOnly:
    CONFIG_UI_CONTRACT = 1


class _UiOnly:
    CONFIG_UI = {"groups": []}


class _BadVersion:
    CONFIG_UI_CONTRACT = 99
    CONFIG_UI = {"groups": []}


class _Minimal:
    CONFIG_UI_CONTRACT = 1
    CONFIG_UI = {"groups": [{"title": "通用", "fields": [
        {"key": "alpha", "type": "switch", "label": "开关"}]}]}


def test_contract_state_matrix():
    assert C.normalize_config_ui(_None, {"alpha"}).state == C.UI_NONE
    assert C.normalize_config_ui(_VersionOnly, {"alpha"}).state == C.UI_INVALID
    assert C.normalize_config_ui(_UiOnly, {"alpha"}).state == C.UI_INVALID
    unsupported = C.normalize_config_ui(_BadVersion, {"alpha"})
    assert unsupported.state == C.UI_UNSUPPORTED
    assert unsupported.schema is None and "版本不受支持" in unsupported.note
    ok = C.normalize_config_ui(_Minimal, {"alpha"})
    assert ok.state == C.UI_OK and ok.warnings == ()
    assert ok.schema["groups"][0]["fields"][0]["key"] == "alpha"


def test_fields_are_filtered_by_config_surface():
    class _Mod:
        CONFIG_UI_CONTRACT = 1
        CONFIG_UI = {"groups": [{"fields": [
            {"key": "alpha", "type": "switch", "label": "可用"},
            {"key": "unknown", "type": "switch", "label": "越界"},
            {"key": "alpha", "type": "switch", "label": "重复"},
            {"key": "beta", "type": "wat", "label": "坏类型"},
            {"key": "beta", "type": "switch"},
        ]}]}

    result = C.normalize_config_ui(_Mod, {"alpha", "beta"})
    assert result.state == C.UI_PARTIAL
    keys = [f["key"] for f in result.schema["groups"][0]["fields"]]
    assert keys == ["alpha"]


def test_conditions_and_variants_require_declared_field():
    class _Mod:
        CONFIG_UI_CONTRACT = 1
        CONFIG_UI = {"groups": [{"fields": [
            {"key": "alpha", "type": "switch", "label": "A",
             "visible_if": {"key": "ghost", "op": "equals", "value": 1},
             "variants": [
                 {"when": {"key": "ghost", "op": "equals", "value": 1}, "label": "幽灵"},
                 {"when": {"key": "beta", "op": "equals", "value": "x"}, "label": "命中"},
             ]},
            {"key": "beta", "type": "text", "label": "B"},
        ]}]}

    result = C.normalize_config_ui(_Mod, {"alpha", "beta"})
    field = result.schema["groups"][0]["fields"][0]
    assert "visible_if" not in field
    assert [v["label"] for v in field["variants"]] == ["命中"]
    assert any("ghost" in w for w in result.warnings)


def test_option_contracts_and_unknown_members_are_rebuilt():
    class _Mod:
        CONFIG_UI_CONTRACT = 1
        CONFIG_UI = {"groups": [{"fields": [
            {"key": "mode", "type": "segmented", "label": "模式",
             "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
             "onclick": "alert(1)", "html": "<b>x</b>"},
            {"key": "group", "type": "select", "label": "组",
             "options": [{"value": "a", "label": "A"}], "choices": "group"},
            {"key": "count", "type": "number", "label": "数量",
             "min": 5, "max": 1, "step": 0, "suffix": "场"},
        ]}]}

    result = C.normalize_config_ui(_Mod, {"mode", "group", "count"})
    fields = result.schema["groups"][0]["fields"]
    assert [f["key"] for f in fields] == ["mode", "count"]  # options+choices 冲突 → 丢字段
    mode, count = fields
    assert "onclick" not in mode and "html" not in mode
    assert "min" not in count and "max" not in count and "step" not in count
    assert count["suffix"] == "场"
    assert result.state == C.UI_PARTIAL


def test_empty_declaration_is_invalid_not_none():
    class _Mod:
        CONFIG_UI_CONTRACT = 1
        CONFIG_UI = {"groups": []}

    result = C.normalize_config_ui(_Mod, {"alpha"})
    assert result.state == C.UI_INVALID and result.schema is None


def test_summary_and_status_normalization():
    chips, problem = C.normalize_summary([" 甲 ", "", 3, "乙"])
    assert chips == ["甲", "乙"] and problem
    assert C.normalize_summary("not-a-list") == ([], "摘要必须是字符串数组")
    assert C.normalize_status(None) == (None, None)
    assert C.normalize_status({"text": "运行中", "level": "silly"}) == (None, "状态等级不受支持: 'silly'")
    status, problem = C.normalize_status({"text": "  运行中  ", "level": "ok"})
    assert problem is None and status == {"text": "运行中", "level": "ok"}


# ---------- sidecar 注入 ----------

class _FakeModule:
    ID = "fake"
    DEFAULT_MODULE_CONFIG = {"alpha": 1, "beta": "x", "mode": "a", "count": 5}
    CONFIG_UI_CONTRACT = 1
    CONFIG_UI = {"groups": [{"fields": [
        {"key": "alpha", "type": "switch", "label": "开关"},
        {"key": "mode", "type": "segmented", "label": "模式", "options": [
            {"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
    ]}]}
    seen_summary: list[dict] = []

    @classmethod
    def normalize_config(cls, config):
        out = dict(config)
        if "legacy" in out:
            out["mode"] = "b"
            out.pop("legacy")
        return out

    @classmethod
    def config_choices(cls):
        return {"beta": [{"value": "x", "label": "X"}]}

    @classmethod
    def config_summary(cls, config):
        cls.seen_summary.append(dict(config))
        return [f"alpha={config['alpha']}"]


class _LiveModule:
    ID = "fake"

    def __init__(self, status=None, config=None):
        self._status = status
        self._config = config or {}

    def get_module_config(self):
        return dict(self._config)

    def config_status(self):
        return self._status


def _svc(monkeypatch, live=None, cache=None):
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._controller = type("C", (), {"active_module": live})()
    svc._selected_module = "fake"
    svc._module_config_cache = dict(cache or {})
    svc._module_config_rev = {}
    svc._config_meta_notes = {}
    svc.saved = []
    monkeypatch.setattr(sc, "_save_profile", lambda partial: svc.saved.append(partial))
    monkeypatch.setattr(sc, "MODULE_REGISTRY", {"fake": _FakeModule})
    monkeypatch.setattr(sc, "module_expired", lambda mid: False)
    return svc


def test_static_view_carries_ui_summary_and_rev(monkeypatch):
    _FakeModule.seen_summary.clear()
    svc = _svc(monkeypatch, cache={"fake": {"legacy": True, "junk": 1}})
    ok, cfg, err = svc.get_module_config({"module_id": "fake"})
    assert ok and err is None
    assert cfg["_ui_state"] == C.UI_OK
    assert cfg["_summary"] == ["alpha=1"]
    assert cfg["_rev"] == 0
    # 摘要只拿到白名单键：旧键已折算、未知键已剔除、默认值已合并、无 _ 元数据
    assert _FakeModule.seen_summary[-1] == {"alpha": 1, "beta": "x", "mode": "b", "count": 5}


def test_summary_input_is_a_copy(monkeypatch):
    class _Mutating(_FakeModule):
        seen: list[dict] = []

        @classmethod
        def config_summary(cls, config):
            cls.seen.append(dict(config))
            config["alpha"] = 999
            return ["x"]

    svc = _svc(monkeypatch)
    monkeypatch.setattr(sc, "MODULE_REGISTRY", {"fake": _Mutating})
    svc.get_module_config({"module_id": "fake"})
    svc.get_module_config({"module_id": "fake"})
    assert [c["alpha"] for c in _Mutating.seen] == [1, 1]


def test_set_increments_rev_and_returns_ui(monkeypatch):
    svc = _svc(monkeypatch)
    _, first, _ = svc.set_module_config({"module_id": "fake", "config": {"alpha": 3}})
    _, second, _ = svc.set_module_config({"module_id": "fake", "config": {"mode": "b"}})
    assert first["_rev"] == 1 and second["_rev"] == 2
    assert second["alpha"] == 3 and second["mode"] == "b"
    _, read, _ = svc.get_module_config({"module_id": "fake"})
    assert read["_rev"] == 2 and read["_ui_state"] == C.UI_OK


def test_live_path_carries_status_and_uses_same_rev(monkeypatch):
    svc = _svc(monkeypatch)
    svc.set_module_config({"module_id": "fake", "config": {"alpha": 2}})
    live = _LiveModule(status={"text": "运行中", "level": "ok"},
                       config={"alpha": 4, "beta": "x", "mode": "a", "count": 5})
    svc._controller.active_module = live
    ok, cfg, _ = svc.get_module_config({"module_id": "fake"})
    assert ok and cfg["_status"] == {"text": "运行中", "level": "ok"}
    assert cfg["_rev"] == 1 and cfg["_ui_state"] == C.UI_OK


def test_invalid_status_is_omitted_and_logged_once(monkeypatch):
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(sc.logger, "log", lambda msg, level="INFO", **kw: logs.append((msg, level)))
    svc = _svc(monkeypatch)
    svc._controller.active_module = _LiveModule(status={"text": "", "level": "ok"})
    for _ in range(3):
        _, cfg, _ = svc.get_module_config({"module_id": "fake"})
        assert "_status" not in cfg
    status_logs = [m for m, _ in logs if "运行状态不可用" in m]
    assert len(status_logs) == 1       # 1s 轮询不刷屏


def test_unsupported_ui_version_is_reported(monkeypatch):
    class _Future(_FakeModule):
        CONFIG_UI_CONTRACT = 99

    svc = _svc(monkeypatch)
    monkeypatch.setattr(sc, "MODULE_REGISTRY", {"fake": _Future})
    _, cfg, _ = svc.get_module_config({"module_id": "fake"})
    assert cfg["_ui_state"] == C.UI_UNSUPPORTED
    assert "_ui" not in cfg and "版本不受支持" in cfg["_ui_note"]
