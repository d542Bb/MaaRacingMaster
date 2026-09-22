# -*- coding: utf-8 -*-
"""实机闭环接线锁（planner 设计稿 §九 step 3）：_drive_loop 的控制链装配与下发。

锁的是**接线**（感知结果→跟踪→聚合→边界→决策→规划→手柄这条管道通不通、
V0/V1 开关在不在位、异常降级停不亦），不重测各层内部（那些有各自的单测）。
桩手柄记录调用，detect_boundary 打桩避开 CV 依赖。
"""
import json
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
    return BoundarySummary(schema_version=2, left_x=80.0, right_x=1200.0,
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
    # 显式 V1（横向开），不依赖 decision.json 的当前部署态
    v1 = replace(load_decision(), mode=replace(load_decision().mode, allow_all_moves=True))
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


# ---------- 控制 trace：逐拍记录 + 阶段出口 flush ----------

def test_control_trace_records_and_flushes(tmp_path, monkeypatch):
    monkeypatch.setattr(smod, "_control_trace_root", lambda: tmp_path)
    m = _module()
    chain = m._build_control_chain()
    pad = StubPad()
    for fid in range(1, 6):
        m._control_tick(chain, pad, None, _perc(fid), fid, fid * 50_000_000, 10.0, 1)
    assert len(chain["trace"]) == 5
    row = chain["trace"][-1]
    # C1/§七.1 标定要的关键列都在
    for k in ("fid", "dt", "state", "x_target", "steer_norm", "steer_x",
              "executed_lane", "v_lat_est", "bnd_valid", "bnd_left_x", "bnd_right_x"):
        assert k in row, k
    m._flush_control_trace(chain, 1)
    files = list(tmp_path.glob("trace_*_p1.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert json.loads(lines[0])["fid"] == 1


def test_control_trace_empty_not_flushed(tmp_path, monkeypatch):
    monkeypatch.setattr(smod, "_control_trace_root", lambda: tmp_path)
    m = _module()
    m._flush_control_trace({"trace": [], "t_start": 0.0}, 1)
    assert list(tmp_path.glob("trace_*.jsonl")) == []   # 空 trace 不落盘


# ---------- 控制实况字段形状（GUI 消费契约）----------

def test_control_last_shape():
    m = _module()
    chain = m._build_control_chain()
    m._control_tick(chain, StubPad(), None, _perc(3), 3, 3 * 50_000_000, 10.0, 1)
    assert set(m._control_last) == {"state", "reason", "steer", "frame_id", "executed_lane"}
    assert m._control_last["frame_id"] == 3
    assert isinstance(m._control_last["executed_lane"], float)


# ---------- step 2.5b _EgoRoadObserver：单侧反推 + 半宽记忆 + 虚线护栏 ----------

def _bnd_edges(l, r):
    return SimpleNamespace(left_edge_lane=l, right_edge_lane=r)


def test_ego_road_both_sides_and_memory():
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_edges(-2.3, 2.4)) == pytest.approx(-0.05)   # 双侧直读
    assert o._hw == pytest.approx(2.35)
    # 单侧左缘：off = −(l + hw)
    assert o.update(_bnd_edges(-1.3, None)) == pytest.approx(-1.05)
    # 单侧右缘：off = −(r − hw)
    assert o.update(_bnd_edges(None, 3.4)) == pytest.approx(-1.05)
    assert o.update(_bnd_edges(None, None)) is None


def test_ego_road_no_memory_single_side_is_none():
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_edges(-1.0, None)) is None      # 没学过路宽，单侧不猜
    assert o.update(None) is None


def test_ego_road_dashed_guard():
    """单侧"缘"其实是车道虚线：反推偏移出界（>hw+0.5）→ 弃观测，不喂假路中心。"""
    o = smod._EgoRoadObserver()
    o.update(_bnd_edges(-2.3, 2.3))                       # 学 hw≈2.3
    assert o.update(_bnd_edges(-0.2, None)) == pytest.approx(-2.1)  # 界内放行
    assert o.update(_bnd_edges(1.5, None)) is None        # −(1.5+2.3)=−3.8 |·|>2.8 出界弃
    assert o.update(_bnd_edges(-1.0, None)) == pytest.approx(-1.3)


# ---------- 坏帧采样器（三局复盘悬案取证：录控互斥不动，控制回路自存真帧） ----------

import math
import numpy as np
import time as _time


def _bad_bnd():
    return BoundarySummary(schema_version=2, left_x=float("nan"),
                           right_x=float("nan"), road_width=float("nan"),
                           straight_residual=float("nan"), vp_row=None,
                           validity=False, uncertainty=float("nan"), sides=0)


def _good_bnd():
    return BoundarySummary(schema_version=2, left_x=80.0, right_x=1200.0,
                           road_width=1120.0, straight_residual=0.0, vp_row=None,
                           validity=True, uncertainty=0.1, sides=2)


def _bad_chain():
    return {"bad_frames": {"next_at": 0.0, "saved": 0, "dir": None},
            "t_start": _time.time()}


def test_bad_frame_saved_and_indexed(monkeypatch, tmp_path):
    monkeypatch.setattr(smod, "_control_trace_root", lambda: tmp_path)
    ch = _bad_chain()
    frame = np.zeros((60, 80, 3), np.uint8)
    smod._maybe_save_bad_frame(ch, frame, _bad_bnd(), 42, 1, 100.0, 999)
    d = ch["bad_frames"]["dir"]
    assert d is not None and (d / "fid_42.jpg").exists()
    row = json.loads((d / "index.jsonl").read_text(encoding="utf-8"))
    assert row["fid"] == 42 and row["ts_ns"] == 999 and row["steer_x"] == 0
    # 节流窗内再来一帧：不落
    smod._maybe_save_bad_frame(ch, frame, _bad_bnd(), 43, 1, 100.5, 1000)
    assert not (d / "fid_43.jpg").exists()
    # 窗过后再来：落
    smod._maybe_save_bad_frame(ch, frame, _bad_bnd(), 44, 1, 102.5, 1001)
    assert (d / "fid_44.jpg").exists()


def test_good_frames_not_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(smod, "_control_trace_root", lambda: tmp_path)
    ch = _bad_chain()
    smod._maybe_save_bad_frame(ch, np.zeros((60, 80, 3), np.uint8),
                               _good_bnd(), 1, 1, 0.0, 0)
    assert ch["bad_frames"]["dir"] is None
    assert list(tmp_path.iterdir()) == []


def test_bad_frame_cap_and_self_disable(monkeypatch, tmp_path):
    monkeypatch.setattr(smod, "_control_trace_root", lambda: tmp_path)
    ch = _bad_chain()
    frame = np.zeros((60, 80, 3), np.uint8)
    for k in range(smod.BAD_FRAME_MAX_PER_PHASE + 10):
        smod._maybe_save_bad_frame(ch, frame, _bad_bnd(), k, 1,
                                   float(k) * 3.0, k)
    assert ch["bad_frames"]["saved"] == smod.BAD_FRAME_MAX_PER_PHASE
    # 异常路径（frame=None → cvtColor 抛）：自禁且不外抛，驾驶链无感
    ch2 = _bad_chain()
    smod._maybe_save_bad_frame(ch2, None, _bad_bnd(), 1, 1, 0.0, 0)
    assert ch2["bad_frames"]["saved"] == smod.BAD_FRAME_MAX_PER_PHASE
