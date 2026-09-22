# -*- coding: utf-8 -*-
"""实机闭环接线锁（planner 设计稿 §九 step 3）：_drive_loop 的控制链装配与下发。

锁的是**接线**（感知结果→跟踪→聚合→边界→决策→规划→手柄这条管道通不通、
V0/V1 开关在不在位、异常降级停不亦），不重测各层内部（那些有各自的单测）。
桩手柄记录调用，detect_boundary 打桩避开 CV 依赖。
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

try:
    import maaracing_master.plugins.speedrush.module as smod
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖（maa/…）：{_ERR}")

from maaracing_master.plugins.speedrush.boundary import BoundarySummary  # noqa: E402
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    Detection, PerceptionResult)
from maaracing_master.plugins.speedrush.planner import LateralPlanner  # noqa: E402


class StubPad:
    def __init__(self):
        self.joy = None
        self.trig = None
        self.updates = 0

    def left_joystick(self, x_value=0, y_value=0):
        self.joy = (x_value, y_value)

    def right_trigger(self, value=0):
        self.trig = value

    def update(self):
        self.updates += 1


def _boundary():
    return BoundarySummary(schema_version=1, left_x=80.0, right_x=1200.0,
                           road_width=1120.0, straight_residual=0.0, vp_row=None,
                           validity=True, uncertainty=0.1)


def _perc(fid, coins=(), cars=()):
    return PerceptionResult(frame_id=fid, ts_ns=fid * 50_000_000,
                            cars=list(cars), coins=list(coins), bonuses=[], infer_ms=1.0)


def _module():
    return smod.SpeedRushModule(None)


@pytest.fixture(autouse=True)
def _stub_boundary(monkeypatch):
    monkeypatch.setattr(smod, "detect_boundary", lambda frame, **kw: _boundary())


# ---------- V0：allow_all_moves=false → 杆值恒 0、油门恒 255 ----------

def test_v0_straight_line(monkeypatch):
    v0 = replace(load_decision(), mode=replace(load_decision().mode, allow_all_moves=False))
    monkeypatch.setattr(smod, "load_decision", lambda: v0)
    m = _module()
    chain = m._build_control_chain()
    pad = StubPad()
    for fid in range(1, 12):
        m._control_tick(chain, pad, None, _perc(fid), fid, fid * 50_000_000, 10.0, 1)
        assert pad.joy[0] == 0                 # 横向恒零
        assert pad.trig == 255                 # 油门恒满
    assert pad.updates == 11
    assert chain["disabled"] is False
    assert m._control_last["state"] == "CRUISE"


# ---------- V1：横向通道在位（自车偏置 → 反向收力回中）----------

def test_v1_lateral_channel_live(monkeypatch):
    v1 = load_decision()
    assert v1.mode.allow_all_moves is True
    monkeypatch.setattr(smod, "load_decision", lambda: v1)
    m = _module()
    chain = m._build_control_chain()
    chain["planner"].state.executed_lane = 1.0   # 自车在 +1 车道，无目标 → 应向左回中
    pad = StubPad()
    saw_negative = False
    for fid in range(1, 16):
        m._control_tick(chain, pad, None, _perc(fid), fid, fid * 50_000_000, 10.0, 1)
        assert pad.trig == 255
        if pad.joy[0] < 0:
            saw_negative = True
    assert saw_negative                          # 横向控制确实活了
    # 回中：executed_lane 从 1.0 向 0 收敛
    assert chain["planner"].state.executed_lane < 1.0


# ---------- 控制链异常 → 停控转观测，不崩主循环、不 strand 油门 ----------

def test_chain_exception_disables_control():
    m = _module()
    chain = m._build_control_chain()
    pad = StubPad()
    m._control_tick(chain, pad, None, _perc(10), 10, 10 * 50_000_000, 10.0, 1)
    assert pad.updates == 1
    # frame_id 倒退：tracker.update fail-loud → _control_tick 捕获、停控
    m._control_tick(chain, pad, None, _perc(5), 5, 5 * 50_000_000, 10.0, 1)
    assert chain["disabled"] is True
    assert pad.updates == 1                      # 停控后不再下发
    # 停控是终态（本阶段）：再喂正常帧也不再动
    m._control_tick(chain, pad, None, _perc(20), 20, 20 * 50_000_000, 10.0, 1)
    assert pad.updates == 1


# ---------- 配置面：control_mode 读写 + 默认关 ----------

def test_control_mode_config_roundtrip():
    m = _module()
    assert m._control_mode is False              # 默认不接管（实机安全边界）
    assert smod.SpeedRushModule.DEFAULT_MODULE_CONFIG["control_mode"] is False
    out = m.set_module_config({"control_mode": True})
    assert out["control_mode"] is True and m._control_mode is True
    assert m.set_module_config({"control_mode": False})["control_mode"] is False


# ---------- 控制实况字段形状（GUI 消费契约）----------

def test_control_last_shape():
    m = _module()
    chain = m._build_control_chain()
    m._control_tick(chain, StubPad(), None, _perc(3), 3, 3 * 50_000_000, 10.0, 1)
    assert set(m._control_last) == {"state", "reason", "steer", "frame_id", "executed_lane"}
    assert m._control_last["frame_id"] == 3
    assert isinstance(m._control_last["executed_lane"], float)
