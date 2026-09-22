# -*- coding: utf-8 -*-
"""模块配置的分槽存储、键白名单与「读配置不建实例」契约。

三条要守住的：
1. **按模块分槽**：切换模块不丢彼此的配置，也不再互相覆盖；
2. **键白名单跟着模块走**（`DEFAULT_MODULE_CONFIG`）：core 不再硬编码任何模块的字段名；
3. **读配置不创建实例**：模块代码不得因为"被看一眼配置"而执行。

本文件导入 core.sidecar（经 controller 拉 maa 等重依赖），缺依赖时整文件 SKIP。
主体用假注册表（不依赖真实插件能否加载），末尾一条针对真实模块的回归锁。
"""
from __future__ import annotations

import threading

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"模块配置测试需要完整运行时依赖：{_ERR}"
)


# ---------- 假注册表（实例化即报错，用来证明"读配置没建实例"）----------

class _NoInstantiateModule:
    """配置面可静态读取，但**构造即失败**——只要测试通过，就说明没人构造它。"""

    DEFAULT_MODULE_CONFIG: dict = {}


class _FakeA(_NoInstantiateModule):
    ID = "fake_a"
    DEFAULT_MODULE_CONFIG = {"alpha": 1, "beta": "x"}

    def __init__(self, ctx=None):  # pragma: no cover —— 被调用即测试失败
        raise AssertionError("读配置不得构造模块实例")


class _FakeB(_NoInstantiateModule):
    ID = "fake_b"
    DEFAULT_MODULE_CONFIG = {"gamma": False}

    def __init__(self, ctx=None):  # pragma: no cover
        raise AssertionError("读配置不得构造模块实例")


_FAKE_REG = {"fake_a": _FakeA, "fake_b": _FakeB}


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setattr(sc, "MODULE_REGISTRY", _FAKE_REG)
    monkeypatch.setattr(sc, "module_expired", lambda mid: False)
    return _FAKE_REG


# ---------- 裸服务：只装配置路径用到的那几个字段 ----------

class _StubController:
    def __init__(self):
        self.active_module = None


class _StubLiveModule:
    """运行中的模块实例（只实现读配置接口）。"""

    ID = "fake_a"

    def get_module_config(self):
        return {"alpha": 9, "_state": {"running": True}}


def _svc(monkeypatch, selected="fake_a", cache=None, live=None):
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._controller = _StubController()
    svc._controller.active_module = live
    svc._selected_module = selected
    svc._module_config_cache = dict(cache or {})
    svc.saved_profiles = []
    monkeypatch.setattr(sc, "_save_profile", lambda partial: svc.saved_profiles.append(partial))
    return svc


# ---------- profile 段解析（新旧两种格式）----------

def test_parse_slots_new_format():
    slots = sc._parse_module_config_slots({"fake_a": {"alpha": 2}, "fake_b": {"gamma": True}})
    assert slots == {"fake_a": {"alpha": 2}, "fake_b": {"gamma": True}}


def test_parse_slots_legacy_flat_format_migrates():
    """旧单槽格式（flat dict + module_id）→ 归到该 module_id 的槽，值不丢。"""
    slots = sc._parse_module_config_slots(
        {"module_id": "fake_a", "alpha": 7, "beta": "y"}
    )
    assert slots == {"fake_a": {"alpha": 7, "beta": "y"}}


def test_parse_slots_drops_non_dict_slot_values():
    slots = sc._parse_module_config_slots({"fake_a": {"alpha": 1}, "fake_b": "garbage"})
    assert slots == {"fake_a": {"alpha": 1}}


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_parse_slots_non_dict_input(bad):
    assert sc._parse_module_config_slots(bad) == {}


# ---------- 默认值静态可读（不构造实例）----------

def test_defaults_are_read_statically(fake_registry):
    """默认值来自模块类声明，读取过程不构造实例（假类的 __init__ 会抛）。"""
    assert sc._module_config_defaults("fake_a") == {"alpha": 1, "beta": "x"}
    assert sc._module_config_defaults("fake_b") == {"gamma": False}


def test_defaults_unknown_module_is_empty(fake_registry):
    assert sc._module_config_defaults("no_such_module") == {}


# ---------- 回填：分槽、迁移、过滤 ----------

def test_restore_multislot(monkeypatch, fake_registry):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(
        sc, "_load_profile",
        lambda: {"module_config": {"fake_a": {"alpha": 5}, "fake_b": {"gamma": True}}},
    )
    svc._restore_profile()
    assert svc._module_config_cache == {"fake_a": {"alpha": 5}, "fake_b": {"gamma": True}}


def test_restore_legacy_single_slot(monkeypatch, fake_registry):
    """旧格式迁移后值仍在（升级不丢用户已设的配置）。"""
    svc = _svc(monkeypatch)
    monkeypatch.setattr(
        sc, "_load_profile",
        lambda: {"module_config": {"module_id": "fake_b", "gamma": True}},
    )
    svc._restore_profile()
    assert svc._module_config_cache == {"fake_b": {"gamma": True}}


def test_restore_drops_undeclared_keys(monkeypatch, fake_registry):
    """模块没声明过的键（手工塞进 profile 的垃圾）一律丢弃。"""
    svc = _svc(monkeypatch)
    monkeypatch.setattr(
        sc, "_load_profile",
        lambda: {"module_config": {"fake_a": {"alpha": 1, "injected": "x"}}},
    )
    svc._restore_profile()
    assert svc._module_config_cache == {"fake_a": {"alpha": 1}}


def test_restore_drops_unregistered_module(monkeypatch, fake_registry):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(
        sc, "_load_profile",
        lambda: {"module_config": {"ghost": {"whatever": 1}, "fake_a": {"alpha": 1}}},
    )
    svc._restore_profile()
    assert svc._module_config_cache == {"fake_a": {"alpha": 1}}


def test_restore_drops_expired_module(monkeypatch, fake_registry):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(sc, "module_expired", lambda mid: mid == "fake_b")
    monkeypatch.setattr(
        sc, "_load_profile",
        lambda: {"module_config": {"fake_a": {"alpha": 1}, "fake_b": {"gamma": True}}},
    )
    svc._restore_profile()
    assert svc._module_config_cache == {"fake_a": {"alpha": 1}}


# ---------- 读写：分槽隔离 / 未知键 / 不建实例 ----------

def test_get_returns_declared_defaults_when_no_cache(monkeypatch, fake_registry):
    svc = _svc(monkeypatch)
    ok, cfg, err = svc.get_module_config({"module_id": "fake_a"})
    assert ok and err is None
    assert cfg["alpha"] == 1 and cfg["beta"] == "x"


def test_set_then_get_is_isolated_per_module(monkeypatch, fake_registry):
    """多槽隔离：给 B 写配置不影响 A 的槽，反之亦然。"""
    svc = _svc(monkeypatch)
    svc.set_module_config({"module_id": "fake_a", "config": {"alpha": 42}})
    svc.set_module_config({"module_id": "fake_b", "config": {"gamma": True}})

    _, a, _ = svc.get_module_config({"module_id": "fake_a"})
    _, b, _ = svc.get_module_config({"module_id": "fake_b"})
    assert a["alpha"] == 42 and b["gamma"] is True
    # 两槽互不污染：A 的 alpha 不该出现在 B 的配置里
    assert "alpha" not in b and "gamma" not in a


def test_set_ignores_undeclared_keys(monkeypatch, fake_registry):
    svc = _svc(monkeypatch)
    _, cfg, _ = svc.set_module_config(
        {"module_id": "fake_a", "config": {"alpha": 2, "injected": "x"}}
    )
    assert cfg["alpha"] == 2 and "injected" not in cfg
    assert svc._module_config_cache["fake_a"] == {"alpha": 2}


def test_set_persists_multislot_snapshot_without_module_id(monkeypatch, fake_registry):
    """落盘快照是新格式（模块 id → 配置），槽内不混入 module_id 字段。"""
    svc = _svc(monkeypatch)
    svc.set_module_config({"module_id": "fake_a", "config": {"alpha": 1}})
    svc.set_module_config({"module_id": "fake_b", "config": {"gamma": False}})
    last = svc.saved_profiles[-1]
    assert set(last["module_config"].keys()) == {"fake_a", "fake_b"}
    assert "module_id" not in last["module_config"]["fake_a"]
    assert "module_id" not in last["module_config"]["fake_b"]


def test_read_and_write_never_construct_module(monkeypatch, fake_registry):
    """读写路径都不构造实例：假注册表里的类一构造就抛 AssertionError。"""
    svc = _svc(monkeypatch)
    assert svc.get_module_config({"module_id": "fake_a"})[0]
    assert svc.set_module_config({"module_id": "fake_a", "config": {"alpha": 3}})[0]
    assert svc.get_module_config({"module_id": "fake_b"})[0]


def test_set_without_selected_module_is_rejected(monkeypatch, fake_registry):
    svc = _svc(monkeypatch, selected=None)
    ok, _, err = svc.set_module_config({"config": {"alpha": 1}})
    assert not ok and "未选择活动模块" in err


def test_set_unknown_module_is_rejected(monkeypatch, fake_registry):
    svc = _svc(monkeypatch)
    ok, _, err = svc.set_module_config({"module_id": "ghost", "config": {"alpha": 1}})
    assert not ok and "模块不存在" in err


# ---------- 运行中：实例权威 ----------

def test_live_instance_is_authoritative(monkeypatch, fake_registry):
    """模块在跑时读实例（含 _state 实况），缓存不参与。"""
    live = _StubLiveModule()
    svc = _svc(monkeypatch, selected="fake_a", cache={"fake_a": {"alpha": 1}}, live=live)
    ok, cfg, err = svc.get_module_config({"module_id": "fake_a"})
    assert ok and err is None
    assert cfg["alpha"] == 9
    assert cfg["_state"] == {"running": True}


class _StubBadLiveModule:
    """违约模块：get_module_config 返回非 dict（接口契约写错）。"""

    ID = "fake_a"

    def get_module_config(self):
        return None


def test_live_instance_returning_non_dict_is_treated_as_empty(monkeypatch, fake_registry):
    """模块违约返回非 dict 时按空配置处理，不打断 GUI 的配置读取。"""
    svc = _svc(monkeypatch, selected="fake_a", live=_StubBadLiveModule())
    ok, cfg, err = svc.get_module_config({"module_id": "fake_a"})
    assert ok and err is None and cfg == {}


def test_other_module_running_does_not_hijack_read(monkeypatch, fake_registry):
    """跑的是别的模块时，读本模块配置走缓存/默认值，不去读那个实例。"""
    live = _StubLiveModule()  # ID == fake_a
    svc = _svc(monkeypatch, selected="fake_b", cache={"fake_b": {"gamma": True}}, live=live)
    _, cfg, _ = svc.get_module_config({"module_id": "fake_b"})
    assert cfg["gamma"] is True and "alpha" not in cfg


def test_merge_prefers_start_params_over_cached_slot():
    """start 合并优先级：本次带参覆盖缓存槽，缓存槽里其它键保留。"""
    merged = sc._merge_module_config({"alpha": 1, "beta": "old"}, {"beta": "new"})
    assert merged == {"alpha": 1, "beta": "new"}


def test_merge_handles_missing_sides():
    assert sc._merge_module_config(None, {"alpha": 1}) == {"alpha": 1}
    assert sc._merge_module_config({"alpha": 1}, None) == {"alpha": 1}
    assert sc._merge_module_config(None, None) == {}


# ---------- 真实模块的回归锁（缺插件注册时跳过）----------

@pytest.mark.skipif(
    not (_OK and "speedrush" in sc.MODULE_REGISTRY and "treasure" in sc.MODULE_REGISTRY),
    reason="真实插件未注册（依赖缺失时 registry 会跳过该插件）",
)
def test_real_modules_declare_their_config_surface():
    """真实模块必须自述配置面——尤其 speedrush 的 record_mode 要能被持久化。

    这条是修复的回归锁：record_mode 曾经不在 core 的键白名单里，写盘后回填被静默丢弃。
    """
    from maaracing_master.core.registry import MODULE_REGISTRY as real_registry
    speedrush = real_registry["speedrush"]
    treasure = real_registry["treasure"]

    sr = dict(speedrush.DEFAULT_MODULE_CONFIG)
    assert "record_mode" in sr and sr["record_mode"] is False
    # 配置面的键与模块实际接受/返回的键必须一致（不多不少）
    # perception_mode：阶段 B 感知开关（2026-09-21 接线）；
    # control_mode：实机闭环接管开关（2026-09-22 planner §九 step 3，默认关）。
    keys = set(sc._module_config_defaults("speedrush"))
    assert keys == {"record_mode", "perception_mode", "control_mode"}

    tr = set(sc._module_config_defaults("treasure"))
    assert tr == {"max_daily_loops", "target_session"}
    assert tr <= set(treasure.DEFAULT_MODULE_CONFIG)
