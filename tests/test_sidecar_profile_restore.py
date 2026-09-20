# -*- coding: utf-8 -*-
"""sidecar profile 恢复与 peep 开关的持久化边界。

**要守住的两条**：
1. debug 段的会话开关照常跨启动恢复（debug_mode / auto_shutdown / click_mode /
   intent_mode / mute_game）——peep 之外的原有行为不得被波及；
2. peep 是会话内实时预览开关，**不持久化不恢复**：`set_peep` 不写 profile，
   启动回填忽略 profile 里残留的 peep_enabled（旧版本写入的历史键）——
   每次启动 GUI 默认关闭。

构造方式沿用 test_rpc_allowlist / test_module_config_slots：`__new__` 绕过
`__init__`（避免拉起控制器），只注入被测方法用到的最小状态；
`_load_profile` / `_save_profile` 运行期替换即生效。
"""
from __future__ import annotations

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


class _StubPeep:
    def __init__(self):
        self.enabled = False
        self.calls = []

    def enable_peep(self):
        self.enabled = True
        self.calls.append(True)

    def disable_peep(self):
        self.enabled = False
        self.calls.append(False)


class _StubController:
    """只实现 restore / set_peep 触碰的接口，记录调用结果。"""

    def __init__(self):
        self.debug = _StubPeep()
        self.debug_mode = False
        self.auto_shutdown = None
        self.click_mode = None
        self.intent_mode = None
        self.mute_game = None

    def set_debug_mode(self, v):
        self.debug_mode = v

    def set_auto_shutdown(self, close_game=None, exit_mra=None):
        self.auto_shutdown = (close_game, exit_mra)

    def set_click_mode(self, v):
        self.click_mode = v

    def set_intent_mode(self, v):
        self.intent_mode = v

    def set_mute_game(self, v):
        self.mute_game = v


def _svc(monkeypatch, profile: dict):
    svc = SidecarService.__new__(SidecarService)
    svc._controller = _StubController()
    svc._module_config_cache = {}
    svc.saved_profiles = []
    monkeypatch.setattr(sc, "_load_profile", lambda: profile)
    monkeypatch.setattr(sc, "_save_profile", lambda partial: svc.saved_profiles.append(partial))
    return svc


def test_restore_ignores_peep_and_restores_rest(monkeypatch):
    """peep_enabled=True 残留不恢复；其余 debug 开关照常回填（原正常行为回归锁）。"""
    profile = {"debug": {
        "debug_mode": True,
        "peep_enabled": True,  # 旧版本残留：必须被忽略
        "auto_close_game": True,
        "auto_exit_mra": False,
        "click_mode": "joystick",
        "intent_mode": True,
        "mute_game": True,
    }}
    svc = _svc(monkeypatch, profile)
    svc._restore_profile()
    assert svc._controller.debug.enabled is False  # peep 启动保持默认关
    assert svc._controller.debug.calls == []       # 连 toggle 都不该发生
    assert svc._controller.debug_mode is True
    assert svc._controller.auto_shutdown == (True, False)
    assert svc._controller.click_mode == "joystick"
    assert svc._controller.intent_mode is True
    assert svc._controller.mute_game is True


def test_set_peep_does_not_persist(monkeypatch):
    """set_peep 只改运行态，不写 profile（写侧持久化已断开）。"""
    svc = _svc(monkeypatch, {"debug": {"debug_mode": False}})
    ok, data, err = svc.set_peep({"enabled": True})
    assert ok is True and err is None
    assert data == {"peep_enabled": True}
    assert svc._controller.debug.enabled is True
    assert svc.saved_profiles == []  # 一次落盘都不发生
