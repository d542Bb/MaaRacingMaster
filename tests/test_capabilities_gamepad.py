# -*- coding: utf-8 -*-
"""Gamepad Protocol 收敛测试：VGamepadAdapter 转发 + 按钮语义常量。

覆盖 racing 收敛到 core gamepad 能力后新增的接口（right_joystick / left_trigger /
right_trigger）与 BUTTON_A/BUTTON_B 常量，用假 pad 隔离 vgamepad 依赖。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

# 屏蔽可能触发底层 vgamepad 导入的模块，保证测试在无 ViGEmBus 环境独立运行。
# capabilities 的按钮常量是惰性的（模块级 __getattr__），仅访问 BUTTON_A/B 才触发；
# 这里注入假 vgamepad 语义模块，使两个常量可被编译验证。
_FakeXUSB = types.SimpleNamespace(
    XUSB_GAMEPAD_A="BUTTON_A_ENUM",
    XUSB_GAMEPAD_B="BUTTON_B_ENUM",
)
_FakeVg = types.SimpleNamespace(XUSB_BUTTON=_FakeXUSB)
# capabilities.py 里 `from ...vgamepad_lazy import vg` 需要从该模块取名为 `vg` 的对象，
# 因此假模块要有一个 `vg` 属性承载 XUSB_BUTTON 枚举。
# ⚠️ 本桩注入后不会撤销（sys.modules 全局生效到会话结束），所以**被替换模块的公开符号
#    必须一并建模**：缺 `gamepad_available` 会让任何后续导入 controller/sidecar 的测试
#    在收集期报 ImportError（桩只给 vg 时就是这样污染过性能仪表测试）。
_FakeLazy = types.SimpleNamespace(vg=_FakeVg, gamepad_available=lambda: False)
for _key in ("vgamepad_lazy", "maaracing_master.core.vgamepad_lazy"):
    assert _key not in sys.modules
    sys.modules[_key] = _FakeLazy

from maaracing_master.core.capabilities import (  # noqa: E402
    VGamepadAdapter,
)


class _FakePad:
    """记录调用参数的假手柄，隔离底层 vgamepad。"""

    def __init__(self):
        self.calls = []

    def press_button(self, button):
        self.calls.append(("press_button", button))

    def release_button(self, button):
        self.calls.append(("release_button", button))

    def left_joystick(self, x_value=0, y_value=0):
        self.calls.append(("left_joystick", x_value, y_value))

    def right_joystick(self, x_value=0, y_value=0):
        self.calls.append(("right_joystick", x_value, y_value))

    def left_trigger(self, value=0):
        self.calls.append(("left_trigger", value))

    def right_trigger(self, value=0):
        self.calls.append(("right_trigger", value))

    def update(self):
        self.calls.append(("update",))


def test_right_joystick_forwarded():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.right_joystick(x_value=100, y_value=-200)
    assert pad.calls[-1] == ("right_joystick", 100, -200)


def test_left_trigger_forwarded():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.left_trigger(value=150)
    assert pad.calls[-1] == ("left_trigger", 150)


def test_right_trigger_forwarded():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.right_trigger(value=230)
    assert pad.calls[-1] == ("right_trigger", 230)


def test_left_joystick_default_args():
    pad = _FakePad()
    adapter = VGamepadAdapter(pad)
    adapter.left_joystick()
    assert pad.calls[-1] == ("left_joystick", 0, 0)


def test_button_constants_exist():
    from maaracing_master.core.capabilities import __getattr__
    # 惰性求值返回枚举（此处为假语义模块注入的值）
    a = __getattr__("BUTTON_A")
    b = __getattr__("BUTTON_B")
    assert a == "BUTTON_A_ENUM"
    assert b == "BUTTON_B_ENUM"

# ======================================================================
# 租约 vs 常驻持有者：`reset_device()` 的准入（① 自愈链的契约基础）
# ======================================================================
#
# 真机事故（2026-09-16 / 09-17 两次复现）：鉴宝 v4 启动时用 `acquire()`（借出-归还
# 的**租约**语义）表达「整场持有手柄」，于是 `_active` 恒 > 0，而 `reset_device()`
# 按能力契约在活跃租约存在时必须抛错——模块自己的「光标长时间丢失 → 重建手柄」
# 自愈因此恒不可用，日志里只剩一句「仍有 N 个活跃手柄租约」。
# 契约正解：跨整个会话持有的导航器走 `persistent_adapter()`（不计入 `_active`），
# 租约留给「借一次就还」的调用方（如跑一次跳转图）。以下锁住这条区分。


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


def test_lease_blocks_reset_device_by_contract():
    """活跃租约存在时 `reset_device()` 必须抛错——绝不静默销毁借用中的设备。"""
    import pytest
    from maaracing_master.core.capabilities import GamepadAdapter

    app = _FakeApp()
    cap = GamepadAdapter(app)
    with cap.acquire() as pad:
        assert pad is not None
        with pytest.raises(RuntimeError) as exc:
            cap.reset_device()
        assert "活跃手柄租约" in str(exc.value)
        assert app.destroyed == 0, "抛错路径不得真的动设备"
    # 归还后即可销毁：懒创建 → 下次取用重建
    cap.reset_device()
    assert app.destroyed == 1


def test_persistent_holder_does_not_block_reset_device():
    """常驻持有者（导航器）**不占租约**：`reset_device()` 必须始终可用。

    这是 ① 自愈链能成立的唯一前提——若它按租约语义实现，自愈就会恒不可用。
    """
    from maaracing_master.core.capabilities import GamepadAdapter

    app = _FakeApp()
    cap = GamepadAdapter(app)
    gpad = cap.persistent_adapter()          # 整场持有
    assert gpad is not None and app.created == 1
    assert cap._active == 0, "常驻持有者不得计入活跃租约计数"
    cap.reset_device()                       # 自愈要走的这一步必须放行
    assert app.destroyed == 1
    # 销毁后下次取用重新创建（换绑到新设备的前提）
    assert cap.persistent_adapter() is not None
    assert app.created == 2


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
    from maaracing_master.plugins.treasure.module import TreasureModule

    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        __import__("maaracing_master.plugins.treasure.module", fromlist=["logger"]),
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
    from maaracing_master.plugins.treasure.module import TreasureModule

    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        __import__("maaracing_master.plugins.treasure.module", fromlist=["logger"]),
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

    from maaracing_master.plugins.treasure.module import TreasureModule

    src = inspect.getsource(TreasureModule)
    assert "gamepad.persistent_adapter()" in src, "v4 绑定应走常驻持有者入口"
    assert "gamepad.acquire()" not in src, (
        "不得取租约——整场持有会让 reset_device() 恒抛，"
        "「光标长时间丢失 → 重建手柄」自愈失效"
    )
