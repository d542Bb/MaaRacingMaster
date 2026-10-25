# -*- coding: utf-8 -*-
"""S1 参考系影子回归锁：历史不是测量、跳变不是运动、校正限速。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from maaracing_master.plugins.speedrush.config import LateralFrame, load_decision
from maaracing_master.plugins.speedrush.lateral_frame import LateralFrameShadow


def _frame(**kw) -> LateralFrameShadow:
    cfg = replace(LateralFrame(enabled=True), **kw)
    return LateralFrameShadow(cfg, load_decision().planner)


def _init(lf: LateralFrameShadow, x: float = 0.0) -> dict:
    return lf.update(0.05, 0.0, 0.0, x, "wall", True)


def test_init_from_first_fresh_observation():
    lf = _frame()
    r = _init(lf, 0.2)
    assert r["x"] == pytest.approx(0.2)
    assert r["x_model"] == pytest.approx(0.2)
    assert r["ev"] == "init" and r["fresh"] == 1


def test_init_gate_rejects_far_observation():
    lf = _frame()
    far = lf.p.anchor_max_off + 0.4
    r = lf.update(0.05, 0.0, 0.0, far, "wall", True)
    assert r["x"] is None
    assert r["init_reject"] == 1 and r["ev"] == "init_hold"


def test_memory_is_slow_anchor_not_measurement():
    lf = _frame()
    _init(lf, 0.0)
    r = lf.update(0.05, 0.0, 0.0, 1.0, "memory", True)
    assert 0.0 < r["x"] < 0.2               # 慢锚靠拢，绝不阶跃到 1.0
    assert r["ev"] == "hold" and r["jump"] == 0
    x1 = r["x"]
    r = lf.update(0.05, 0.0, 0.0, 1.0, "wall_occl", True)
    assert x1 < r["x"] < 0.2
    assert r["ev"] == "hold"


def test_hold_alpha_zero_disables_anchor():
    lf = _frame(hold_alpha=0.0)
    _init(lf, 0.0)
    r = lf.update(0.05, 0.0, 0.0, 1.0, "memory", True)
    assert r["x"] == pytest.approx(0.0)
    assert r["ev"] == "hold"


def test_reused_observation_is_not_new_evidence():
    lf = _frame()
    _init(lf, 0.0)
    r = lf.update(0.05, 0.0, 0.0, 0.4, "wall", False)
    assert r["x"] == pytest.approx(0.0)
    assert r["ev"] == "model" and r["fresh"] == 1


def test_single_jump_is_held_then_reanchored_at_rate_limit():
    lf = _frame()
    _init(lf, 0.0)
    r = lf.update(0.05, 0.0, 0.0, 1.0, "wall", True)
    assert r["x"] == pytest.approx(0.0)      # 单拍跳变不当运动
    assert r["ev"] == "jump" and r["cand_n"] == 1
    r = lf.update(0.05, 0.0, 0.0, 1.0, "wall", True)
    assert r["ev"] == "reanchor" and r["reanchor"] == 1
    assert r["x"] == pytest.approx(0.1)      # 确认后按 2 道/秒限速靠拢
    assert r["target"] == pytest.approx(1.0)
    for _ in range(40):
        r = lf.update(0.05, 0.0, 0.0, None, None, False)
    assert r["x"] == pytest.approx(1.0, abs=1e-6)
    assert r["target"] is None


def test_jump_candidate_resets_on_direction_change():
    lf = _frame()
    _init(lf, 0.0)
    assert lf.update(0.05, 0.0, 0.0, 1.0, "wall", True)["cand_n"] == 1
    r = lf.update(0.05, 0.0, 0.0, -1.0, "wall", True)
    assert r["cand_n"] == 1 and r["reanchor"] == 0
    assert r["target"] is None


def test_small_innovation_rate_limited():
    lf = _frame()
    _init(lf, 0.0)
    r = lf.update(0.05, 0.0, 0.0, 0.4, "wall", True)
    assert r["x"] == pytest.approx(0.1)      # α·r=0.12 被 2 道/秒×dt 截住
    assert r["ev"] == "accept" and r["accept"] == 1


def test_model_predicts_between_observations():
    lf = _frame()
    _init(lf, 0.0)
    r = lf.update(0.2, 1.0, 0.0, None, None, False)
    assert r["x"] > 0.1 and r["ev"] == "model"
    x0 = r["x"]
    r = lf.update(0.2, 1.0, 0.0, None, None, False)
    assert r["x"] > x0


def test_bad_dt_does_not_crash():
    lf = _frame()
    _init(lf, 0.0)
    r = lf.update(0.0, 0.0, 0.0, 0.5, "wall", True)
    assert r["ev"] == "bad_dt" and r["x"] == pytest.approx(0.0)


def test_production_shadow_enabled_and_trace_only():
    cfg = load_decision().lateral_frame
    assert cfg.enabled is True
    assert 0.0 < cfg.jump_tol_lane <= 2.0
    assert cfg.confirm_n >= 1
    assert 0.0 < cfg.fresh_alpha <= 1.0
    assert cfg.max_corr_lane_s > 0.0


# ---------- 线一致性桥接 ----------


def _tick(lf, obs, src="wall", new=True, lane=None, state=None, lock=0,
          age=50.0, dt=0.05, far=None, far_new=False, theta=None):
    return lf.update(dt, 0.0, 0.0, obs, src, new, lane, state, lock, age,
                     far, far_new, theta)


def _agree(lf, obs=0.0, lane=0.0):
    return _tick(lf, obs, "wall", True, lane, "TRACK", 2, 50.0)


def test_lane_bridges_gap_only_after_two_agreements():
    lf = _frame()
    _agree(lf, 0.0, 0.0)          # 初始化 + 第 1 次一致
    r = _agree(lf, 0.0, 0.0)      # 第 2 次一致 → 建立信任
    assert r["lane_ok"] is True
    r = _tick(lf, None, None, False, 0.4, "TRACK", 2, 50.0)
    assert r["ev"] == "lane" and r["lane_n"] == 1
    assert r["x"] == pytest.approx(0.032)   # hold_alpha 0.08 × 0.4


def test_lane_disagreement_revokes_trust():
    lf = _frame()
    _agree(lf, 0.0, 0.0)
    _agree(lf, 0.0, 0.0)
    assert lf.lane_ok is True
    r = _tick(lf, 0.6, "wall", True, 0.0, "TRACK", 2, 50.0)
    assert r["lane_ok"] is False           # 新鲜深度与线差 0.6 道
    r = _tick(lf, None, None, False, 0.0, "TRACK", 2, 50.0)
    assert r["ev"] != "lane" and r["lane_n"] == 0


def test_lane_jump_revokes_trust():
    lf = _frame()
    _agree(lf, 0.0, 0.0)
    _agree(lf, 0.0, 0.0)
    r = _tick(lf, None, None, False, 0.8, "TRACK", 2, 50.0)
    assert r["lane_ok"] is False and r["lane_jump"] is True
    r = _tick(lf, None, None, False, 0.8, "TRACK", 2, 50.0)
    assert r["ev"] != "lane" and r["lane_n"] == 0


def test_lane_not_locked_or_stale_is_not_used():
    lf = _frame()
    _agree(lf, 0.0, 0.0)
    _agree(lf, 0.0, 0.0)
    r = _tick(lf, None, None, False, 0.4, "TRACK", 0, 50.0)
    assert r["ev"] != "lane"
    r = _tick(lf, None, None, False, 0.4, "TRACK", 2, 300.0)
    assert r["ev"] != "lane"


def test_lane_trust_expires_without_fresh_depth_agreement():
    lf = _frame()
    _agree(lf, 0.0, 0.0)
    _agree(lf, 0.0, 0.0)
    for _ in range(60):                    # 3 秒空窗，超过 2.5 秒信任期
        _tick(lf, None, None, False, None, None, 0, None, dt=0.05)
    assert lf.lane_ok is False
    r = _tick(lf, None, None, False, 0.4, "TRACK", 2, 50.0)
    assert r["ev"] != "lane" and r["lane_n"] == 0


# ---------- 航向慢状态 ----------


def test_heading_initializes_and_rate_limits():
    lf = _frame()
    r = _tick(lf, 0.0, "wall", True, 0.0, "TRACK", 2, 50.0, theta=0.2)
    assert r["theta"] == pytest.approx(0.2)
    assert r["theta_ok"] is True and r["theta_n"] == 1
    assert r["v_theta"] == pytest.approx(0.0)  # 首帧进基线，不产生横速
    r = _tick(lf, None, None, False, 0.0, "TRACK", 2, 50.0, theta=-0.2)
    assert r["theta"] == pytest.approx(0.16)   # 限速 0.8 rad/s × 0.05
    assert r["theta_rel"] < 0.0 and r["v_theta"] < 0.0


def test_heading_pushes_x_only_when_theta_changes():
    """常值线角进慢基线后不推横位；线角快速变化才推。"""
    lf = _frame()
    _tick(lf, 0.0, "wall", True, 0.0, "TRACK", 2, 50.0, theta=0.0)
    for _ in range(20):                       # 线角稳定在 0.05，基线跟上
        r = _tick(lf, None, None, False, 0.0, "TRACK", 2, 50.0, theta=0.05)
    settled = r["v_theta"]
    x0 = lf.snapshot()["x"]
    for _ in range(10):
        _tick(lf, None, None, False, 0.0, "TRACK", 2, 50.0, theta=0.10)
    assert abs(r["v_theta"]) >= abs(settled)
    assert lf.snapshot()["x"] > x0 + 0.005


def test_heading_disabled_has_no_effect():
    on = _frame()
    off = _frame(theta_enabled=False)
    for lf in (on, off):
        _tick(lf, 0.0, "wall", True, 0.0, "TRACK", 2, 50.0, theta=0.0)
    for _ in range(10):
        _tick(on, None, None, False, 0.0, "TRACK", 2, 50.0, theta=0.08)
        _tick(off, None, None, False, 0.0, "TRACK", 2, 50.0, theta=0.08)
    assert on.v_theta > 0.0 and off.v_theta == 0.0
    assert off.snapshot()["theta_n"] == 0


def test_heading_stops_after_ttl_without_observation():
    lf = _frame()
    _tick(lf, 0.0, "wall", True, 0.0, "TRACK", 2, 50.0, theta=0.0)
    for _ in range(5):                        # 建立非零相对航向
        r = _tick(lf, None, None, False, 0.0, "TRACK", 2, 50.0, theta=0.1)
    assert r["v_theta"] > 0.0
    for _ in range(25):                       # 1.25 s 无新线角
        r = _tick(lf, None, None, False, None, None, 0, None)
    assert r["theta_ok"] is False and r["v_theta"] == 0.0
    assert r["theta"] > 0.0                   # 状态保留，但不参与修正


def test_heading_ignored_when_lane_not_track_or_stale():
    lf = _frame()
    r = _tick(lf, 0.0, "wall", True, 0.0, "COAST", 2, 50.0, theta=0.2)
    assert r["theta_n"] == 0 and r["theta_ok"] is False
    r = _tick(lf, None, None, False, 0.0, "TRACK", 2, 300.0, theta=0.2)
    assert r["theta_n"] == 0 and r["theta_ok"] is False


# ---------- 远场地面宏观锚 ----------


def test_macro_bias_aligns_far_with_near():
    lf = _frame()
    r = _tick(lf, 0.0, "wall", True, None, None, 0, None,
              far=0.4, far_new=True)
    assert r["macro"] == pytest.approx(0.4)
    assert r["far_bias"] == pytest.approx(0.4)   # 远场偏差 = 0.4 道
    r = _tick(lf, None, None, False, None, None, 0, None,
              far=0.4, far_new=True)
    assert r["ev"] == "macro" and r["macro_n"] == 1
    assert r["x"] == pytest.approx(0.0)          # 扣偏差后宏观锚=近场


def test_macro_pulls_depth_gap_after_stable_far():
    lf = _frame()
    _tick(lf, 0.0, "wall", True, None, None, 0, None,
          far=0.0, far_new=True)
    for _ in range(20):
        r = _tick(lf, None, None, False, None, None, 0, None,
                  far=0.2, far_new=True)
    assert r["ev"] == "macro" and r["macro_n"] > 0
    assert r["x"] > 0.02


def test_macro_jump_is_dropped_then_rebuilds():
    lf = _frame()
    _tick(lf, 0.0, "wall", True, None, None, 0, None,
          far=0.0, far_new=True)
    r = _tick(lf, None, None, False, None, None, 0, None,
              far=1.0, far_new=True)
    assert r["far_jump"] is True and r["macro"] is None
    assert r["ev"] != "macro"
    r = _tick(lf, None, None, False, None, None, 0, None,
              far=1.0, far_new=True)
    assert r["far_jump"] is False
    assert r["macro"] == pytest.approx(1.0)
    assert r["ev"] == "macro"


def test_macro_expires_without_far_updates():
    lf = _frame()
    _tick(lf, 0.0, "wall", True, None, None, 0, None,
          far=0.0, far_new=True)
    for _ in range(40):                    # 2 秒，超过 1.5 秒信任期
        _tick(lf, None, None, False, None, None, 0, None, dt=0.05)
    assert lf.macro_age_s > 1.5
    r = _tick(lf, None, None, False, None, None, 0, None)
    assert r["ev"] != "macro"


def test_macro_can_be_disabled():
    lf = _frame(macro_enabled=False)
    _tick(lf, 0.0, "wall", True, None, None, 0, None,
          far=0.0, far_new=True)
    r = _tick(lf, None, None, False, None, None, 0, None,
              far=0.2, far_new=True)
    assert r["macro"] is None and r["ev"] != "macro"
