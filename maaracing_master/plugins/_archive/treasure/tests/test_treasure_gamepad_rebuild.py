# -*- coding: utf-8 -*-
"""鉴宝虚拟手柄自愈链与常驻绑定形态锁。"""
from __future__ import annotations

import types

import pytest

pytestmark = pytest.mark.retired


class _FakePad:
    """记录调用参数的假手柄，隔离底层 vgamepad。"""

    def __init__(self):
        self.calls = []


class _FakeApp:
    """controller 手柄管理面的最小桩：只实现能力适配器真正调用的四个方法。"""

    def __init__(self):
        self.gpad = None
        self.created = 0
        self.destroyed = 0
        self.reset_calls = 0

    def _get_gpad(self):
        if self.gpad is None:
            self.gpad = _FakePad()
            self.created += 1
        return self.gpad

    def _reset_gpad(self):
        self.reset_calls += 1

    def _destroy_gpad(self):
        if self.gpad is None:
            return
        self.gpad = None
        self.destroyed += 1


class _RebuildSelf:
    """`TreasureModule._rebuild_gamepad_device` 的最小桩。"""

    def __init__(self, cap):
        self.ctx = types.SimpleNamespace(gamepad=cap)
        self.swapped = []
        self._clicker = types.SimpleNamespace(
            gamepad_bound=True,
            swap_gamepad=lambda new: self.swapped.append(new),
        )


def test_rebuild_chain_succeeds_for_persistent_holder(monkeypatch):
    """自愈链端到端走通（原本正常场景）：reset_device → 取新设备 → 换绑导航器。

    这是修复前**从未成功过**的那条路径：真机上它每次都被租约挡回。
    """
    from maaracing_master.core.capabilities import GamepadAdapter
    from maaracing_master.plugins._archive.treasure.module import TreasureModule

    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        __import__("maaracing_master.plugins._archive.treasure.module", fromlist=["logger"]),
        "logger", types.SimpleNamespace(log=lambda m, lv="INFO": logged.append((lv, m))))

    app = _FakeApp()
    cap = GamepadAdapter(app)
    cap.persistent_adapter()                 # v4 常驻绑定
    fake = _RebuildSelf(cap)

    assert TreasureModule._rebuild_gamepad_device(fake) is True
    assert app.destroyed == 1, "旧设备必须被确定性拔除"
    assert app.created == 2, "必须取到重建后的新设备"
    assert len(fake.swapped) == 1, "导航器必须换绑到新设备"
    assert any("已重建虚拟手柄并换绑导航器" in m for lv, m in logged)


def test_rebuild_chain_reports_failure_instead_of_crashing(monkeypatch):
    """自愈失败必须留痕且不拖垮主循环（返回 False，交下帧重试）。"""
    from maaracing_master.core.capabilities import GamepadAdapter
    from maaracing_master.plugins._archive.treasure.module import TreasureModule

    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        __import__("maaracing_master.plugins._archive.treasure.module", fromlist=["logger"]),
        "logger", types.SimpleNamespace(log=lambda m, lv="INFO": logged.append((lv, m))))

    app = _FakeApp()
    cap = GamepadAdapter(app)
    fake = _RebuildSelf(cap)
    with cap.acquire():                      # 别人正持租约 → reset_device 必然抛
        assert TreasureModule._rebuild_gamepad_device(fake) is False
    assert fake.swapped == [], "失败时不得换绑"
    warns = [m for lv, m in logged if lv == "WARNING"]
    assert warns and "虚拟手柄重建失败" in warns[0], "失败必须留痕"


def test_treasure_v4_binding_never_takes_a_session_lease():
    """静态守卫：鉴宝模块不得用 `acquire()` 表达整场持有（否则自愈恒不可用）。

    租约会把 `_active` 钉在 > 0，`reset_device()` 契约性地拒绝执行；
    整场持有者的正确入口是 `persistent_adapter()`。若将来确需一次短借
    （如跑一次跳转图），应显式评估后再放开本守卫。
    """
    import inspect

    from maaracing_master.plugins._archive.treasure.module import TreasureModule

    src = inspect.getsource(TreasureModule)
    assert "gamepad.persistent_adapter()" in src, "v4 绑定应走常驻持有者入口"
    assert "gamepad.acquire()" not in src, (
        "不得取租约——整场持有会让 reset_device() 恒抛，"
        "「光标长时间丢失 → 重建手柄」自愈失效"
    )
