# -*- coding: utf-8 -*-
"""实机闭环接线锁（planner 设计稿 §九 step 3）：_drive_loop 的控制链装配与下发。

锁的是**接线**（感知结果→跟踪→聚合→边界→决策→规划→手柄这条管道通不通、
V0/V1 开关在不在位、异常降级停不亦），不重测各层内部（那些有各自的单测）。
桩手柄记录调用，detect_boundary 打桩避开 CV 依赖。
"""
import json
import time as _time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
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


# ---------- 闭环供数：pair 与单侧合成都喂（80e9a1c 供数门撤销锁） ----------

def _bnd_lanes(el, er, sides=2):
    return BoundarySummary(schema_version=2, left_x=80.0, right_x=1200.0,
                           road_width=1120.0, straight_residual=0.0, vp_row=None,
                           validity=True, uncertainty=0.1, sides=sides,
                           left_edge_lane=el, right_edge_lane=er)


def test_road_offset_all_sources_feed_loop(monkeypatch):
    """单侧合成也进闭环（13:29 局证伪 pair-only：挡单侧=制造供数黑视，
    死亡螺旋段 87/118 拍被挡、陈旧重基与跳变门同时失效——黑视比噪声致命）。
    噪声由 0.8 新息门 + 1.0s 陈旧重基消化，不由接线层挡。"""
    cur = {}
    monkeypatch.setattr(smod, "detect_boundary", lambda frame, **kw: cur.get("b"))
    m = _module()
    chain = m._build_control_chain()
    chain["geo_master"] = "hsv"
    pad = StubPad()
    fid = 0

    def tick():
        nonlocal fid
        fid += 1
        m._control_tick(chain, pad, None, _perc(fid), fid,
                        fid * 50_000_000, 10.0, 1)
        return chain["trace"][-1]

    cur["b"] = _bnd_lanes(-1.5, 1.5)               # 同帧双侧对（黄线语义：车道中心）
    t1 = tick()
    assert t1["ro_source"] == "pair"
    # W=3.0 → lane_w=0.75,p=1.5 → 车道 2 中心 1.875 → off=+0.375
    assert t1["road_offset"] == pytest.approx(0.375)
    cur["b"] = _bnd_lanes(-2.0, None, sides=1)      # 单侧：路面半宽 1.5 → p=2.0
    t2 = tick()
    assert t2["ro_source"] == "single_L"
    # 车道 2 中心 1.875 → off=−0.125（照喂，不黑视）
    assert t2["road_offset"] == pytest.approx(-0.125)


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
    assert set(m._control_last) == {"state", "reason", "steer", "frame_id",
                                    "executed_lane", "road_offset",
                                    "dgeo_age", "dgeo_new"}
    assert m._control_last["frame_id"] == 3
    assert isinstance(m._control_last["executed_lane"], float)


# ---------- step 2.5b _EgoRoadObserver：单侧反推 + 半宽记忆 + 虚线护栏 ----------

def _bnd_edges(el, er):
    return SimpleNamespace(left_edge_lane=el, right_edge_lane=er)


def test_ego_road_both_sides_and_memory():
    o = smod._EgoRoadObserver()
    t = 1000.0
    assert o.update(_bnd_edges(-1.2, 1.3), now=t) == pytest.approx(-0.05)  # 双侧直读
    assert o._hw == pytest.approx(1.25)
    # 单侧左缘：off = −(l + hw)
    assert o.update(_bnd_edges(-0.8, None), now=t + 0.03) == pytest.approx(-0.45)
    # 单侧右缘：off = −(r − hw)
    assert o.update(_bnd_edges(None, 1.7), now=t + 0.07) == pytest.approx(-0.45)
    # 全弃权拍：保鲜槽（2026-09-25）内仍有 0.4s 旧证据 → 取最新鲜槽 + 半宽反推
    # （此前返回 None 退纯模型积分；槽内证据自洽时同值）
    assert o.update(None, now=t + 0.10) == pytest.approx(-0.45)


def test_ego_road_no_memory_single_side_is_none():
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_edges(-1.0, None)) is None      # 没学过路宽，单侧不猜
    assert o.update(None) is None


def test_ego_road_dashed_guard():
    """单侧"缘"其实是车道虚线：反推偏移出界（>hw+0.5）→ 弃观测，不喂假路中心。"""
    o = smod._EgoRoadObserver()
    o.update(_bnd_edges(-1.2, 1.3))                       # 学 hw≈1.25
    assert o.update(_bnd_edges(-0.2, None)) == pytest.approx(-1.05)  # 界内放行
    assert o.update(_bnd_edges(1.5, None)) is None        # −(1.5+1.25)=−2.75 出界弃
    assert o.update(_bnd_edges(-1.0, None)) == pytest.approx(-0.25)


def test_ego_road_resident_replay_not_new_evidence():
    """驻留协议（2026-09-28）：is_new=False 的旧读数复用——路心照出，但
    槽时间戳不刷新（保鲜 TTL 不被推新）、半宽 EMA 不重学（使用次数≠学习次数）。"""
    o = smod._EgoRoadObserver()
    t = 1000.0
    assert o.update(_bnd_edges(-1.2, 1.3), now=t) == pytest.approx(-0.05)
    hw1 = o._hw
    slot_t = o._slot["L"][1]
    # 同一旧读数第二拍消费：is_new=False——pair 路心照出，但 EMA 不动
    assert o.update(_bnd_edges(-1.2, 1.3), now=t + 0.05, is_new=False) \
        == pytest.approx(-0.05)
    assert o._hw == hw1, "驻留复用不得重学半宽"
    assert o._slot["L"][1] == slot_t, "驻留复用不得刷新槽时间戳（TTL 语义）"
    # 驻留读数也不得让过期槽"复活保鲜"：1s 后（TTL 0.4s 已过）无当前读数 → 弃权
    assert o.update(None, now=t + 1.0) is None
    # 单侧驻留读数反推照走（用记忆半宽），同样不学不刷
    o2 = smod._EgoRoadObserver()
    o2.update(_bnd_edges(-1.2, 1.3), now=t)
    hw2 = o2._hw
    o2.update(_bnd_edges(-0.8, None), now=t + 0.03)       # 单侧反推 −0.45
    assert o2.update(_bnd_edges(-0.8, None), now=t + 0.06, is_new=False) \
        == pytest.approx(-0.45)
    assert o2._hw == hw2
    assert o2._slot["L"][1] == t + 0.03, "槽停在最后一次新证据时刻"


# ---------- 坏帧采样器（三局复盘悬案取证：录控互斥不动，控制回路自存真帧） ----------


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


def test_ego_road_hw_learning_bounded():
    """20:45 漏网锁：垃圾双侧帧（两假缘相距 21 车道）整帧弃——中点碰巧在界内
    （off=−2.5）也不行：对宽不物理=两"缘"都不是路缘，off 同样不可信。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_edges(-8.0, 13.0)) is None       # hw=10.5 出 [0.8,2.2]
    assert o._hw is None                                   # 不学记忆
    assert o.update(_bnd_edges(-1.0, None)) is None        # 无合法记忆，单侧仍不猜
    # 合法对照常放行并学习
    assert o.update(_bnd_edges(-1.2, 1.3)) == pytest.approx(-0.05)
    assert o._hw == pytest.approx(1.25)


def test_ego_road_hw_learn_gate_rejects_outlier_pair():
    """半宽 EMA 一致性门（181518 局幸存者偏差教训）+ 稳态门（1830 局教训）：
    离群对——当帧路心仍可信（off 照给），不学记忆；且离群对进稳态窗后
    2s 内极差超限 → 后续一致对也冻结（离群冷却期，垃圾对混喂的保守防线），
    离群滚出候选窗后恢复学习。"""
    o = smod._EgoRoadObserver()
    o.update(_bnd_edges(-1.2, 1.3), now=1000.0)            # hw≈1.25（冷启动锚）
    # hw=2.1 界内但偏 0.85>0.6：off=−1.3 界内放行，EMA 不动
    assert o.update(_bnd_edges(-0.8, 3.4), now=1000.03) == pytest.approx(-1.3)
    assert o._hw == pytest.approx(1.25)
    # 冷却期内的一致对也不学（稳态窗被离群对撑爆，极差 0.8>0.25）
    o.update(_bnd_edges(-1.3, 1.3), now=1000.07)
    assert o._hw == pytest.approx(1.25)
    # 离群对滚出 2s 候选窗后，稳态一致对恢复学习（EMA 向 1.3 收敛）
    for i in range(5):
        o.update(_bnd_edges(-1.3, 1.3), now=1002.1 + i * 0.05)
    assert o._hw == pytest.approx(1.25 * 0.7 + 1.3 * 0.3)


def test_ego_road_absolute_bound_without_memory():
    """绝对物理界不依赖 _hw（旧护栏在 _hw=None 时整条旁路，±7 就是这么漏的）：
    同侧双假缘 hw=4.0 出界弃；合法对但中点出界（|off|>2.5）也弃。"""
    o = smod._EgoRoadObserver()
    assert o.update(_bnd_edges(-9.0, -1.0)) is None        # hw=4.0 不物理
    o2 = smod._EgoRoadObserver()
    o2._hw = 1.25                                          # 有合法记忆
    assert o2.update(_bnd_edges(None, 9.0)) is None        # off=−(9−1.25)=−7.75 超界


# ---------- 每侧保鲜槽（2026-09-25 落产码，README「取证续」节 5 设计） ----------

def test_ego_road_slot_cold_start_cross_frame_pair():
    """双侧从不同帧：L 槽 × 当前 R 跨时刻配对学半宽（冷启动待办就此吃掉）。"""
    o = smod._EgoRoadObserver()
    t = 2000.0
    assert o.update(_bnd_edges(-1.2, None), now=t) is None          # 只有 L，无记忆
    assert o.update(_bnd_edges(None, 1.5), now=t + 0.05) == pytest.approx(-0.15)
    assert o._hw == pytest.approx(1.35)                             # 从跨帧对学到半宽


def test_ego_road_slot_ttl_expiry():
    """age 预算到点即弃：0.4s 后槽不再供证据（宁退积分，不用馊读数）。
    弃权拍双侧槽都新鲜时优先跨时刻合成（不依赖半宽记忆，不学）。"""
    o = smod._EgoRoadObserver()
    t = 3000.0
    o.update(_bnd_edges(-1.2, 1.3), now=t)
    o.update(_bnd_edges(None, 1.7), now=t + 0.03)                   # R 槽刷新
    # 双槽新鲜：合成 off=−(−1.2+1.7)/2=−0.25，且不学（合成不是新证据）
    assert o.update(None, now=t + 0.05) == pytest.approx(-0.25)
    assert o._hw == pytest.approx(1.25)
    assert o.update(None, now=t + 0.44) is None                     # 双槽过期（0.41s）


def test_ego_road_ghost_slot_abstains_instead_of_single_infer():
    """假宽缘躺在槽里时弃权拍整拍弃（1933 局蛇形根因的回归锁）：幽灵侧
    单侧反推会给出 ±2 道的荒唐路心，绝不允许。"""
    o = smod._EgoRoadObserver()
    t = 6000.0
    o.update(_bnd_edges(-1.2, 1.3), now=t)                          # hw≈1.25
    # 当前帧只有 R=3.5（越栏假宽）：单侧反推 off=−(3.5−1.25)=−2.25 超相对界 → 弃
    assert o.update(_bnd_edges(None, 3.5), now=t + 0.03) is None
    # 但幽灵值已进 R 槽：弃权拍双槽合成对宽 2.35 出界 → 整拍弃，不退单侧
    assert o.update(None, now=t + 0.06) is None


def test_ego_road_slot_unphysical_cross_pair_rejected():
    """虚线假缘 × 陈槽：跨帧对宽度校验拦下（不学 _hw、不供 off）。"""
    o = smod._EgoRoadObserver()
    t = 4000.0
    o.update(_bnd_edges(None, 1.5), now=t)                          # 先立 R 槽
    # L 假缘 0.9（对侧值）与槽 R 1.5 凑对 hw=0.3 不物理 → 弃
    assert o.update(_bnd_edges(0.9, None), now=t + 0.05) is None
    assert o._hw is None


def test_ego_road_same_frame_pair_still_wins_over_slots():
    """同帧双缘走原语义（合成不覆盖同帧）；单侧在场且已有记忆时仍单侧反推。"""
    o = smod._EgoRoadObserver()
    t = 5000.0
    o.update(_bnd_edges(-1.2, 1.3), now=t)
    # 槽里有 R=1.3，但当前帧 L=−0.8 走单侧反推（当前值+常数半宽，误差不随时延涨）
    assert o.update(_bnd_edges(-0.8, None), now=t + 0.05) == pytest.approx(-0.45)
    assert o._hw == pytest.approx(1.25)                             # 未被跨帧对污染


def test_ego_road_steady_reanchor_after_lane_switch():
    """稳态重锚（1830 局教训）：跨车道后结构对切换（对宽 1.9 稳定出现），
    一致性门（差 0.65>TOL）会永远拒学、EMA 卡死旧值——连续 HW_REANCH_N 个
    稳定一致的候选判结构对切换，重锚到均值。"""
    o = smod._EgoRoadObserver()
    t = 7000.0
    o.update(_bnd_edges(-1.2, 1.3), now=t)                          # 锚 1.25,窗清空
    # 新车道结构对 hw=1.9 稳定重复（间隔 0.2s,8 个候选跨 1.4s 在 2s 窗内）
    for i in range(1, 8):
        o.update(_bnd_edges(-1.9, 1.9), now=t + i * 0.2)
    assert o._hw == pytest.approx(1.25)                             # 未达重锚数,不动
    o.update(_bnd_edges(-1.9, 1.9), now=t + 8 * 0.2)
    assert o._hw == pytest.approx(1.9)                              # 重锚
    # 重锚后单侧反推用新半宽：off=−(−1.5+1.9)=−0.4
    assert o.update(_bnd_edges(-1.5, None), now=t + 1.9) == pytest.approx(-0.4)


def test_ego_road_lane_change_freezes_learning():
    """变道冻结（can_learn=False）：CHANGE/ABORT 期结构对在切换,半宽不学,
    槽照常保鲜（读数仍是真观测）。"""
    o = smod._EgoRoadObserver()
    t = 8000.0
    o.update(_bnd_edges(-1.2, 1.3), now=t)                          # 锚 1.25
    o.update(_bnd_edges(-1.6, 1.9), now=t + 0.05, can_learn=False)  # 变道期对
    assert o._hw == pytest.approx(1.25)
    assert o._slot["R"][0] == pytest.approx(1.9)                    # 槽照刷
    assert o._slot["R"][1] == pytest.approx(t + 0.05)
    # 变道结束恢复学习（稳态窗攒够 5 个一致候选）
    for i in range(5):
        o.update(_bnd_edges(-1.6, 1.9), now=t + 5.0 + i * 0.05)
    assert o._hw != pytest.approx(1.25)


def test_ego_road_lane_marking_semantics():
    """黄线语义（2026-09-28 T1 量测，52 demos 会话 855 双侧重放）：检测器配对
    全路面两侧缘（对宽 3~5 道）；off=**最近车道中心**（非路面中心——2026-09-28
    纠偏：回路面中心与 T2 目标车道制冲突）。结构语义的合法对（半宽 1.0）在
    黄线语义下不物理。"""
    o = smod._EgoRoadObserver(semantics="lane_marking")
    assert o.HW_MIN == 1.5 and o.HW_MAX == 2.7 and o.OFF_MAX == 2.5
    t = 9000.0
    # 路面对宽 4.24（p50，lane_w=1.06）：车距左缘 2.0 → 车道 1，中心在 1.59
    # → off=−0.41（小修正拉回本车道中心，不再被拽向路面中心）
    assert o.update(_bnd_edges(-2.0, 2.24), now=t) == pytest.approx(-0.41)
    assert o._hw == pytest.approx(2.12)
    assert o._lane_cur == 1
    # 半宽 1.0（结构语义的合法值）在黄线语义下不物理 → 整拍弃
    assert o.update(_bnd_edges(-1.0, 1.0), now=t + 0.05) is None
    # 单侧反推用路面半宽+车道中心：左缘 -0.35 → p=0.35 → 车道 0，off=+0.18
    o2 = smod._EgoRoadObserver(semantics="lane_marking")
    o2.update(_bnd_edges(-2.0, 2.24), now=t)
    assert o2.update(_bnd_edges(-0.35, None), now=t + 0.03) == pytest.approx(0.18)
    assert o2._lane_cur == 0


def test_ego_road_lane_marking_hysteresis_on_boundary():
    """车道归属迟滞：车骑在车道边界（p≈2×lane_w）时毫米级漂移不得来回改归属
    （LANE_SWITCH_MARGIN=车道宽 10%）。"""
    o = smod._EgoRoadObserver(semantics="lane_marking")
    t = 9500.0
    o.update(_bnd_edges(-2.0, 2.24), now=t)                 # 锚,p=2.0 → 车道 1
    assert o._lane_cur == 1
    # 漂过边界一点点（p=2.13,车道 2 侧）:距离差不显著 → 保持车道 1
    o.update(_bnd_edges(-2.13, 2.11), now=t + 0.05)
    assert o._lane_cur == 1
    # 显著进入车道 2（p=2.6,中心 2.65 vs 车道1中心 1.59:差显著）→ 归属切换
    o.update(_bnd_edges(-2.6, 1.64), now=t + 0.10)
    assert o._lane_cur == 2


def test_control_chain_ego_road_semantics_follows_geo_master():
    """chain 的 ego_road 守卫语义随 geo_master（T1：黄线供数默认档）。"""
    m = _module()
    m._geo_master = "hsv"
    assert m._build_control_chain()["ego_road"].HW_MIN == 1.5
    m._geo_master = "depth"
    assert m._build_control_chain()["ego_road"].HW_MIN == 0.8


# ---------- YOLO 物体掩码：框入掩码（外扩+夹边）；无检测 None（深度路径零成本） ----------

def test_yolo_object_mask_boxes_with_margin():
    d = Detection(cx=100, cy=100, w=40, h=20, conf=0.9)
    mask = smod._yolo_object_mask(_perc(1, cars=(d,)))
    assert mask is not None and mask.shape == (720, 1280)
    ys, xs = np.nonzero(mask)
    m = smod.YOLO_MASK_MARGIN
    assert (int(xs.min()), int(xs.max())) == (100 - 20 - m, 100 + 20 + m)
    assert (int(ys.min()), int(ys.max())) == (100 - 10 - m, 100 + 10 + m)


def test_yolo_object_mask_edge_clamped():
    """框贴画面边缘：外扩后夹在画面内，不越界。"""
    d = Detection(cx=2, cy=2, w=40, h=40, conf=0.9)
    mask = smod._yolo_object_mask(_perc(1, cars=(d,)))
    assert mask is not None
    assert mask[0, 0] and not mask[:, 43 + smod.YOLO_MASK_MARGIN:].any()


def test_yolo_object_mask_none_when_no_detections():
    assert smod._yolo_object_mask(_perc(1)) is None
