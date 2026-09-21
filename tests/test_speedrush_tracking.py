# -*- coding: utf-8 -*-
"""跟踪层与世界观测契约的回归锁（设计稿 v2 §八 step 2 的"接线前必锁清单"）。

裁决列出的八项逐条对应：
  1 过旧帧 / 2 空目标 / 3 目标短暂遮挡 / 4 目标重识别 / 5 远距 lane_side /
  6 帧间跳变 / 7 弯道·几何无效 / 8 阶段切换清空。
另锁：帧号单调（fail-loud）、外推纪律（横向保持、纵向匀速）、id 不复用、
排序与 JSON 序列化（回放落盘的前提）、参数校验、rel_approach 口径、x_sigma 量级。
"""

from __future__ import annotations

import json

import pytest

from maaracing_master.plugins.speedrush.perception import Detection, PerceptionResult
from maaracing_master.plugins.speedrush.tracking import (
    SCHEMA_VERSION, DecisionOutput, DecisionState, FarTarget, LaneSide,
    PerceptionHealth, Tracker, TrackerParams, TrackedTarget, to_jsonable)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_CAR, KIND_COIN, Calib, x_lane_of)

CAL = Calib()


def _per(fid: int, *, cars=(), coins=(), bonuses=()) -> PerceptionResult:
    return PerceptionResult(frame_id=fid, ts_ns=fid * 33_000_000,
                            cars=list(cars), coins=list(coins), bonuses=list(bonuses))


def _near_car(cx=800, cy=500):
    """cy=500 在域内（denom≈176），x_lane≈正值（cx>vpx）。"""
    return Detection(cx, cy, 60, 40, 0.9)


# ---------- 1 过旧帧 ----------

def test_stale_frame_flagged_but_observation_produced():
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_near_car()]), frame_age_ms=500.0, stage=1)
    assert obs.health.frame_fresh is False      # 过旧如实上报，供校验层合取
    assert len(obs.targets) == 1                # 本层不拦截——判定权在校验层


def test_fresh_frame_flag_ok():
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_near_car()]), frame_age_ms=12.0, stage=1)
    assert obs.health.frame_fresh is True


# ---------- 2 空目标 ----------

def test_empty_frame_presence_false_and_no_targets():
    trk = Tracker()
    obs = trk.update(_per(1), frame_age_ms=10.0, stage=1)
    assert obs.targets == () and obs.far_targets == ()
    assert obs.health.target_presence is False


def test_above_horizon_dropped():
    """cy ≤ y_h（地平线以上）几何无效，直接丢——与 build_world 同口径。"""
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_near_car(cy=int(CAL.y_h) - 5)]),
                     frame_age_ms=10.0, stage=1)
    assert obs.targets == () and obs.far_targets == ()
    assert obs.health.target_presence is False


# ---------- 3 目标短暂遮挡 ----------

def test_occlusion_holds_id_and_lateral():
    trk = Tracker()
    first = trk.update(_per(1, cars=[_near_car()]), frame_age_ms=10.0, stage=1)
    held_x = first.targets[0].x_lane
    gone = trk.update(_per(2), frame_age_ms=10.0, stage=1)   # 该帧检不到
    assert len(gone.targets) == 1                            # 宽限窗内保持
    t = gone.targets[0]
    assert t.id == first.targets[0].id
    assert t.x_lane == held_x                    # 横向不外推：保持最后读数
    assert t.last_seen_fid == 1
    assert t.cy == first.targets[0].cy           # 静止目标：外推量 = rel_approach = 0
    back = trk.update(_per(3, cars=[_near_car()]), frame_age_ms=10.0, stage=1)
    assert back.targets[0].id == t.id            # 重现续旧身份
    assert back.targets[0].first_seen_fid == 1   # 首见时刻不被重写


def test_occlusion_extrapolates_cy_by_velocity():
    """遮挡期的纵向保持是"匀速外推"，不是冻住——否则宽限窗内距离序会骗人。"""
    trk = Tracker()
    trk.update(_per(1, cars=[_near_car(cy=480)]), frame_age_ms=10.0, stage=1)
    trk.update(_per(2, cars=[_near_car(cy=520)]), frame_age_ms=10.0, stage=1)  # rel=20
    obs = trk.update(_per(3), frame_age_ms=10.0, stage=1)
    assert obs.targets[0].cy == 540          # 520 + 20×1
    assert obs.targets[0].x_lane == x_lane_of(800, 520, CAL)  # 横向仍是最后读数（520 那帧）


# ---------- 4 目标重识别（退役后新 id） ----------

def test_expired_track_retires_and_id_never_reused():
    p = TrackerParams(grace_ticks=2)
    trk = Tracker(params=p)
    first = trk.update(_per(1, cars=[_near_car()]), frame_age_ms=10.0, stage=1)
    old_id = first.targets[0].id
    trk.update(_per(2), frame_age_ms=10.0, stage=1)   # 失联 1
    trk.update(_per(3), frame_age_ms=10.0, stage=1)   # 失联 2：宽限边界
    gone = trk.update(_per(4), frame_age_ms=10.0, stage=1)   # 失联 3：退役
    assert gone.targets == ()
    again = trk.update(_per(5, cars=[_near_car()]), frame_age_ms=10.0, stage=1)
    assert again.targets[0].id != old_id             # 不复用旧 id
    assert again.targets[0].first_seen_fid == 5


def test_validity_window_on_output():
    p = TrackerParams(grace_ticks=3)
    trk = Tracker(params=p)
    obs = trk.update(_per(10, cars=[_near_car()]), frame_age_ms=10.0, stage=1)
    assert obs.targets[0].validity_until_fid == 13


# ---------- 5 远距目标：x_lane 不可用但 lane_side 可用 ----------

def test_far_targets_carry_side_not_xlane():
    trk = Tracker()
    far = int(CAL.y_h) + 10                       # 域外：denom 10 < MIN_DENOM
    obs = trk.update(_per(1, coins=[
        Detection(900, far, 20, 20, 0.8),         # 右侧
        Detection(400, far, 20, 20, 0.8),         # 左侧
        Detection(int(CAL.vpx), far, 20, 20, 0.8),  # 中列
    ]), frame_age_ms=10.0, stage=1)
    assert obs.targets == ()                      # 域外不进近场
    sides = sorted(f.lane_side for f in obs.far_targets)
    assert sides == [int(LaneSide.LEFT), int(LaneSide.MID), int(LaneSide.RIGHT)]
    assert all(isinstance(f, FarTarget) for f in obs.far_targets)
    assert obs.health.target_presence is True     # 远场也算 presence


def test_far_to_near_promotion_keeps_id():
    """远→近按物理速度渐进时同一 id、last_x_lane 从此有值——金币组提前规划的通道。"""
    trk = Tracker()
    far = int(CAL.y_h) + 10
    o1 = trk.update(_per(1, coins=[Detection(700, far, 20, 20, 0.8)]),
                    frame_age_ms=10.0, stage=1)
    assert len(o1.far_targets) == 1 and o1.far_targets[0].last_x_lane is None
    o2 = trk.update(_per(2, coins=[Detection(720, far + 40, 24, 24, 0.85)]),
                    frame_age_ms=10.0, stage=1)
    assert o2.far_targets == ()
    t = o2.targets[0]
    assert isinstance(t, TrackedTarget)
    assert t.id == o1.far_targets[0].id           # 同一目标同一 id
    assert t.x_lane == pytest.approx(x_lane_of(720, far + 40, CAL))


# ---------- 6 帧间目标跳变 ----------

def test_cy_jump_beyond_gate_is_new_track():
    trk = Tracker()
    a = trk.update(_per(1, cars=[_near_car(cx=700, cy=500)]), frame_age_ms=10.0, stage=1)
    b = trk.update(_per(2, cars=[_near_car(cx=700, cy=500 + 400)]), frame_age_ms=10.0, stage=1)
    assert b.targets[0].id != a.targets[0].id     # 跳变超门限：不按同一目标
    assert len(b.targets) == 2                    # 旧目标进宽限、新目标在册


def test_lateral_jump_beyond_gate_is_new_track():
    trk = Tracker()
    a = trk.update(_per(1, cars=[_near_car(cx=700, cy=500)]), frame_age_ms=10.0, stage=1)
    b = trk.update(_per(2, cars=[_near_car(cx=1100, cy=500)]), frame_age_ms=10.0, stage=1)
    assert len(b.targets) == 2                    # 旧目标进宽限，新目标另立
    new = [t for t in b.targets if t.id != a.targets[0].id][0]
    assert new.x_lane == pytest.approx(x_lane_of(1100, 500, CAL))


# ---------- 7 弯道 / 几何无效输入 ----------

def test_geometry_invalid_passthrough_not_gated():
    """本层不判弯道（设计决定 10：判据属校验层），只把标志如实带进观测。"""
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_near_car()]), frame_age_ms=10.0, stage=1,
                     geometry_valid=False)
    assert obs.health.geometry_valid is False
    assert len(obs.targets) == 1                  # 不拦截——保守与否由校验层合取决定


def test_stage_transition_passthrough():
    trk = Tracker()
    obs = trk.update(_per(1), frame_age_ms=10.0, stage=1, stage_transition=True)
    assert obs.health.stage_transition is True


# ---------- 8 阶段切换清空 ----------

def test_stage_change_clears_all_tracks():
    trk = Tracker()
    a = trk.update(_per(1, cars=[_near_car()]), frame_age_ms=10.0, stage=1)
    old_id = a.targets[0].id
    b = trk.update(_per(2, cars=[_near_car()]), frame_age_ms=10.0, stage=2)
    assert b.targets[0].id != old_id              # 清空后全新检出
    assert b.targets[0].first_seen_fid == 2
    assert b.stage == 2


# ---------- 契约杂项 ----------

def test_frame_id_must_increase():
    trk = Tracker()
    trk.update(_per(5), frame_age_ms=10.0, stage=1)
    with pytest.raises(ValueError):
        trk.update(_per(5), frame_age_ms=10.0, stage=1)   # 重复帧
    with pytest.raises(ValueError):
        trk.update(_per(3), frame_age_ms=10.0, stage=1)   # 乱序帧


def test_targets_sorted_near_to_far():
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_near_car(cy=450), _near_car(cx=480, cy=650)]),
                     frame_age_ms=10.0, stage=1)
    assert [t.cy for t in obs.targets] == [650, 450]


def test_rel_approach_positive_when_closing():
    trk = Tracker()
    trk.update(_per(1, cars=[_near_car(cy=480)]), frame_age_ms=10.0, stage=1)
    obs = trk.update(_per(2, cars=[_near_car(cy=520)]), frame_age_ms=10.0, stage=1)
    t = obs.targets[0]
    assert t.rel_approach == pytest.approx(0.5 * 0 + 0.5 * 40)  # EMA(0, 40)
    assert t.id == 1                               # 匀速接近不误建新轨迹


def test_x_sigma_matches_error_note_order():
    """§6 注记：分母 20–50 区间误差 ±0.2–0.5 车道——σ 模型必须同量级。"""
    trk = Tracker()
    cy = int(CAL.y_h) + 30                      # 分母 30，落在注记区间
    obs = trk.update(_per(1, cars=[_near_car(cy=cy)]), frame_age_ms=10.0, stage=1)
    t = obs.targets[0]
    assert t.x_lane is not None                 # 分母 30 ≥ MIN_DENOM：域内
    assert 0.2 <= t.x_sigma <= 0.6              # 15×0.587/30 ≈ 0.29
    assert t.kind == KIND_CAR


def test_observation_is_json_serializable():
    """回放落盘的前提：契约对象过 to_jsonable 后必须直接 json 可写。"""
    trk = Tracker()
    obs = trk.update(_per(1, cars=[_near_car()], coins=[
        Detection(900, int(CAL.y_h) + 10, 20, 20, 0.8)]), frame_age_ms=10.0, stage=1)
    data = to_jsonable(obs)
    s = json.dumps(data, ensure_ascii=False)
    back = json.loads(s)
    assert back["schema_version"] == SCHEMA_VERSION
    assert back["health"]["perception_alive"] is True
    assert back["far_targets"][0]["lane_side"] == 1
    assert back["targets"][0]["kind"] == KIND_CAR


def test_decision_output_contract_shape():
    """DecisionOutput 生产者属 step 4，但契约形状与枚举值现在锁死。"""
    d = DecisionOutput(schema_version=SCHEMA_VERSION, state=DecisionState.CONSERVE,
                       target_id=None, x_target=0.0, move_allowed=False,
                       reason="validate_fail:frame_stale",
                       emitted_fid=42, valid_until_fid=44)
    j = to_jsonable(d)
    assert j["state"] == "CONSERVE"          # Enum → value，回放侧读的是码串
    assert set(DecisionState) == {DecisionState.CRUISE, DecisionState.CHANGE,
                                  DecisionState.ABORT_CHANGE, DecisionState.CONSERVE,
                                  DecisionState.FAULT}


def test_boundary_is_none_until_layer_lands():
    trk = Tracker()
    obs = trk.update(_per(1), frame_age_ms=10.0, stage=1)
    assert obs.boundary is None              # 边界感知层未落地，不许有伪造摘要


def test_health_passthrough_fields():
    trk = Tracker()
    obs = trk.update(_per(1), frame_age_ms=10.0, stage=2)
    h = obs.health
    assert isinstance(h, PerceptionHealth)
    assert (h.perception_alive, h.geometry_valid, h.stage_transition) == (True, True, False)
    assert obs.stage == 2 and obs.frame_age_ms == 10.0 and obs.ts_ns == 33_000_000 * 1


@pytest.mark.parametrize("kwargs", [
    {"grace_ticks": 0}, {"ema_alpha": 1.0}, {"max_cy_step": -1},
    {"pos_err_px": 0}, {"fresh_max_ms": 0},
])
def test_params_fail_loud(kwargs):
    with pytest.raises(ValueError):
        TrackerParams(**kwargs)


def test_different_kinds_never_associate():
    """金币位置恰与消失的车同构——跨类不得继承 id。"""
    trk = Tracker()
    a = trk.update(_per(1, cars=[_near_car()]), frame_age_ms=10.0, stage=1)
    b = trk.update(_per(2, coins=[Detection(800, 500, 60, 40, 0.9)]),
                   frame_age_ms=10.0, stage=1)
    assert b.far_targets == ()
    assert len(b.targets) == 2                    # 旧车进宽限 + 新金币
    coin = [t for t in b.targets if t.kind == KIND_COIN][0]
    assert coin.id != a.targets[0].id
