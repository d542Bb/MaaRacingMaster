# -*- coding: utf-8 -*-
"""R3 贴身猎车（corridor_reach.hug，2026-10-16 设计稿 §2.2）的回归锁。

锁面（设计稿 §2.2/§4.2 ①边界构造矩阵）：可达性唯一门（t_overtake≥need+τ+margin，
过近来不及即自然失败）、窗下沿硬地板（d̂<lo=同车道贴尾，0 分）、窗内平台常数
（30+120×P(d_hold)≈132，真源推导非手选）、窗上沿 logistic 衰减（上沿处 ≈105，
两处断崖是待测边界行为，锁的是实现正确不是行为收益）、按仪器互斥分支（米制
锚前车走贴身 E 项不进旧错身项，同一辆车不双计）、仪器失效回退（t_overtake
None=旧错身项逐位）、双车累加、占用/遮挡/碰撞判据照旧（窗在归属半宽内，
car_in_path 照旧含贴身窗前车）、闸关逐位（hug 参数缺=行为与 R3 前一致）。
密集段行为收益不在此锁（那是影子回放 probe_hug_replay 的验收面）。

场景几何以标定真值反推（lane_w=3.38：归属半宽 2.535m、贴身窗 [1.859,2.366]m、
P 中心 0.73 道×3.38），改标定会红——参数与场景是契约面。
"""
from __future__ import annotations

import math

import pytest

from maaracing_master.plugins.speedrush.corridor_reach import (
    CarSource, CoinSource, CorridorBandSpan, ReachParams, evaluate)

LANE_W = 3.38                      # 标定真源 gate0.json（改标定=重新收杆）
HALF = 0.75 * LANE_W               # 归属半宽 2.535m（窗整体落在 in-path 区内）
LO = 0.55 * LANE_W                 # 窗下沿 1.859m
HI = 0.70 * LANE_W                 # 窗上沿 2.366m
PC = 0.73 * LANE_W                 # P logistic 中心 2.4674m
PS = 0.06 * LANE_W                 # P logistic 尺度 0.2028m
LIMIT = 120.0                      # 极限动作分（RULES §4.11 镜像）


def _p(d: float) -> float:
    return 1.0 / (1.0 + math.exp((d - PC) / PS))


PLATFORM = 30.0 + LIMIT * _p((LO + HI) / 2.0)      # ≈132.23
EDGE = 30.0 + LIMIT * _p(HI)                       # 上沿处 ≈104.70


def _params(**kw) -> ReachParams:
    """need 线性模型 need(dx)=0.3+0.1·dx（秒）：起步 0.3 + 0.1/米。"""
    base = dict(step_m=0.5, horizon_s=2.0, crash_t_s=0.8, crash_penalty=100.0,
                pass_value=30.0, far_coin_weight=0.3, tie_margin=20.0,
                shift_cost_per_m=0.0, coin_value_per_unit=30.0,
                coin_group_min_count=3.0, attribution_half_m=HALF,
                pass_band_m=LANE_W, tau_s=0.13, margin_s=0.15,
                need_of_dx_m=lambda dx: 0.3 + 0.1 * dx,
                hug_d_lo_m=LO, hug_d_hi_m=HI,
                hug_p_center_m=PC, hug_p_scale_m=PS, hug_value_limit=LIMIT)
    return ReachParams(**{**base, **kw})


def _params_off(**kw) -> ReachParams:
    """闸关参数面（hug 字段全缺=逐位回今天）。"""
    return _params(**{**kw, "hug_d_lo_m": None, "hug_d_hi_m": None,
                      "hug_p_center_m": None, "hug_p_scale_m": None,
                      "hug_value_limit": 0.0})


def _bands(n_bands=3, left=-6.0, right=6.0, z0=0.0, band_len=3.0):
    return tuple(CorridorBandSpan(z0 + i * band_len, z0 + (i + 1) * band_len,
                                  left, right) for i in range(n_bands))


def _car(x, t_overtake=1.5, t_meet=None):
    return CarSource(x_m=x, t_meet_s=t_meet, t_overtake_s=t_overtake)


def _best_by_x(rep):
    """候选按 x 索引（浮点网格键舍入到 1e-6——step 采样带尾差）。"""
    return {round(c.x_m, 6): c for c in rep.candidates}


# ---------- 窗内平台常数（真源推导） ----------

def test_hug_window_platform_value():
    """窗内候选：平台常数 30+120×P(d_hold)，pass_value_sum=0（互斥不双计）。"""
    car = _car(2.2, t_overtake=1.5)
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[4.25]                      # d̂=2.05 ∈ 窗；need=0.725，门 1.5≥1.005 过
    assert c.hug_cars == 1
    assert c.hug_value == pytest.approx(PLATFORM)
    assert c.pass_value_sum == 0.0      # 互斥分支：出贴身值的车不进旧错身项
    assert c.score == pytest.approx(PLATFORM)
    # 平台常数与 d̂ 无关：窗内另一候选同分
    c2 = by_x[4.75]                     # d̂=2.55 > hi——这是衰减区，另测
    assert c2.hug_value < PLATFORM      # 见 test_hug_logistic_above_hi


def test_hug_platform_constant_across_window():
    """平台内各候选同分（窗 [lo,hi] 内 d̂ 不同、值同）→ 位移代价决定选中位。"""
    car = _car(0.0, t_overtake=1.5)     # 细步进让两个候选同落窗内
    rep = evaluate(_bands(), (), (car,), 0.0, _params(step_m=0.2))
    assert rep is not None
    by_x = _best_by_x(rep)
    # 步进 0.2：候选 2.1/2.3（d̂=2.1/2.3 都 ∈ [1.859, 2.366]）
    a = by_x[2.1]
    b = by_x[2.3]
    assert a.hug_value == pytest.approx(PLATFORM)
    assert b.hug_value == pytest.approx(PLATFORM)
    assert a.score == pytest.approx(b.score)   # 平台内同分（位移代价 0）
    assert by_x[2.5].hug_value < PLATFORM      # d̂=2.5 窗外，衰减区
    assert by_x[1.7].hug_value == 0.0          # d̂=1.7 < lo，硬地板


# ---------- 窗下沿硬地板（断崖一：0 → 平台，待测边界行为） ----------

def test_hug_floor_below_lo():
    """d̂ < lo：同车道贴尾不是错身，0 分（占用面维持现状）。"""
    car = _car(0.0, t_overtake=1.5)
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    assert by_x[1.75].hug_value == 0.0          # d̂=1.75 < lo
    assert by_x[1.75].score == 0.0
    assert by_x[2.25].hug_value == pytest.approx(PLATFORM)  # d̂=2.25 ≥ lo


def test_hug_boundary_flip_is_cliff():
    """边界附近 1cm 估计噪声：0 ↔ 平台翻转（断崖，危险向——锁行为存在，
    不预设已解决；实测振荡再议平滑，设计稿 §2.2）。"""
    car_in = _car(4.25 - HI, t_overtake=1.5)    # d̂=hi 恰在沿上 → 平台
    car_out = _car(4.25 - HI - 0.01, t_overtake=1.5)   # d̂=hi+0.01 → 衰减区
    rep = evaluate(_bands(), (), (car_in,), 0.0, _params())
    assert rep is not None
    assert _best_by_x(rep)[4.25].hug_value == pytest.approx(PLATFORM)
    rep2 = evaluate(_bands(), (), (car_out,), 0.0, _params())
    assert rep2 is not None
    v_out = _best_by_x(rep2)[4.25].hug_value
    assert v_out == pytest.approx(30.0 + LIMIT * _p(HI + 0.01))
    assert PLATFORM - v_out > 25.0              # 上沿断崖 ≈27 分


# ---------- 窗上沿 logistic 衰减（断崖二：平台 → 衰减，待测边界行为） ----------

def test_hug_logistic_above_hi():
    """d̂ > hi：30+120×P(d̂) 连续衰减，上沿处 ≈105（约 27 分出窗跳变）。"""
    car = _car(2.2, t_overtake=1.5)
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[4.75]                      # d̂=2.55 > hi
    assert c.hug_value == pytest.approx(30.0 + LIMIT * _p(2.55))
    assert c.hug_value < PLATFORM
    assert c.hug_value > 30.0           # 远处仍 ≥ 错身保底
    # 衰减单调：更远的候选值更低
    c_far = by_x[5.75]                  # d̂=3.55
    assert c_far.hug_value < c.hug_value
    # 上沿处（恰过沿）≈ EDGE ≈105
    car_edge = _car(4.25 - HI - 1e-9, t_overtake=1.5)
    rep2 = evaluate(_bands(), (), (car_edge,), 0.0, _params())
    assert rep2 is not None
    assert _best_by_x(rep2)[4.25].hug_value == pytest.approx(EDGE, rel=1e-3)


# ---------- R5 层 2：hug 分支扩到像素仪器车（2026-10-17 设计稿 §9.2） ----------

def test_hug_visual_gate_off_pixel_car_byte_identical():
    """hug_visual 关（默认）：无米制锚的车走旧路径（t_overtake None→旧错身
    项判据；d̂=2.05 在归属区 ≤ 半宽 → 无错身值），逐位回今天。"""
    car = _car(2.2, t_overtake=None, t_meet=1.5)
    rep = evaluate(_bands(), (), (car,), 0.0, _params(hug_visual=False))
    assert rep is not None
    c = _best_by_x(rep)[4.25]           # d̂=2.05 ∈ 窗内——但闸关不进贴身分支
    assert c.hug_cars == 0 and c.hug_value == 0.0
    assert c.pass_value_sum == 0.0      # d̂ ≤ 归属半宽：旧错身项也不计
    assert c.score == 0.0


def test_hug_visual_pixel_car_gets_platform():
    """hug_visual 开：无米制锚的车回退 t_meet 过同一门与 E 曲线——窗内
    平台 ≈132（不再是 0），互斥不进旧错身项。"""
    car = _car(2.2, t_overtake=None, t_meet=1.5)
    rep = evaluate(_bands(), (), (car,), 0.0, _params(hug_visual=True))
    assert rep is not None
    c = _best_by_x(rep)[4.25]
    assert c.hug_cars == 1
    assert c.hug_value == pytest.approx(PLATFORM)
    assert c.pass_value_sum == 0.0      # 互斥分支：同一辆车不双计
    assert c.score == pytest.approx(PLATFORM)


def test_hug_visual_pixel_car_gate_rejects():
    """hug_visual 开：回退 t_meet 也过可达门——t_meet ≤ need+τ+margin 拒
    （硬地板 0，不落旧错身项）；窗外（> 前瞻窗）同拒。"""
    near = _car(2.2, t_overtake=None, t_meet=0.6)   # need=0.505，门 0.785>0.6
    rep = evaluate(_bands(), (), (near,), 0.0, _params(hug_visual=True))
    assert _best_by_x(rep)[4.25].hug_cars == 0
    assert _best_by_x(rep)[4.25].score == 0.0
    far = _car(2.2, t_overtake=None, t_meet=2.5)    # > 前瞻窗 2.0
    rep2 = evaluate(_bands(), (), (far,), 0.0, _params(hug_visual=True))
    assert _best_by_x(rep2)[4.25].hug_cars == 0


def test_hug_visual_pass_band_gets_e_curve():
    """hug_visual 开：旧错身带（d̂ ∈ (半宽, 半宽+道宽]）的车从平价 30 换成
    E 曲线（30+limit×P(d̂) 衰减区 ≥ 30）——擦边倾向的连续价值面。"""
    car = _car(2.2, t_overtake=None, t_meet=1.5)
    rep = evaluate(_bands(), (), (car,), 0.0, _params(hug_visual=True))
    assert rep is not None
    c = _best_by_x(rep)[5.25]           # d̂=3.05 ∈ (2.535, 5.915]
    assert c.hug_cars == 1
    assert c.pass_value_sum == 0.0      # 互斥：出 E 值不进旧错身项
    e_expected = 30.0 + LIMIT * _p(3.05)
    assert c.hug_value == pytest.approx(e_expected)
    assert c.score == pytest.approx(e_expected)
    assert c.score >= 30.0              # E 曲线在错身带处处 ≥ 保底


# ---------- 可达性唯一门（极短相遇） ----------

def test_hug_gate_rejects_unreachable():
    """t_overtake < need+τ+margin：过近来不及即 0（可达性唯一门，不另设门）。
    crash 判据照旧——t_ov ≥ need 才进 crash 窗，本例 t_ov < need 无碰撞罚。"""
    car = _car(2.2, t_overtake=0.5)     # need(4.25)=0.725，门需 ≥1.005
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[4.25]
    assert not c.crash
    assert c.hug_value == 0.0
    assert c.hug_cars == 0
    # 门恰好过/不过的边界：t_ov 过门线（need+τ+margin）一点即收（判据含等号；
    # 精确等值在浮点上不可靠，给 1e-6 余量）
    car_star = _car(2.2, t_overtake=0.725 + 0.13 + 0.15 + 1e-6)
    rep2 = evaluate(_bands(), (), (car_star,), 0.0, _params())
    assert rep2 is not None
    assert _best_by_x(rep2)[4.25].hug_value == pytest.approx(PLATFORM)


def test_hug_gate_uses_candidate_need():
    """门随候选位变化：近候选（need 小）收短 t_ov，远候选（need 大）拒同一 t_ov。"""
    car = _car(-2.2, t_overtake=1.0)    # 对称：另一侧窗
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    # 候选 -2.25：d̂=0.05 < lo 硬地板；候选 -4.25：d̂=2.05 ∈ 窗，need=0.725，
    # 门需 ≥1.005 > 1.0 → 拒；候选 -4.0 侧不存在同窗候选……
    assert by_x[-4.25].hug_value == 0.0         # 1.0 < 1.005：差一点不及
    # 稍远车（更早到站窗口更大）→ 同 t_ov 过门
    car2 = _car(-2.2, t_overtake=1.01)
    rep2 = evaluate(_bands(), (), (car2,), 0.0, _params())
    assert rep2 is not None
    assert _best_by_x(rep2)[-4.25].hug_value == pytest.approx(PLATFORM)


# ---------- 按仪器互斥分支（同一辆车不双计） ----------

def test_metric_car_not_double_counted():
    """米制锚前车在旧错身带内：只有贴身 E 项（pass_value_sum=0）；
    同几何的像素车：只有旧错身项（hug_value=0）。"""
    car_m = _car(2.2, t_overtake=1.5)   # d̂(4.25)=2.05 ∈ 窗；d̂(5.25)=3.05 在旧错身带
    rep = evaluate(_bands(), (), (car_m,), 0.0, _params())
    assert rep is not None
    by_x = _best_by_x(rep)
    c = by_x[5.25]                      # d̂=3.05 ∈ (2.535, 5.915] 旧错身带
    assert c.hug_value == pytest.approx(30.0 + LIMIT * _p(3.05))
    assert c.pass_value_sum == 0.0      # 互斥：旧错身 30 不再叠加
    car_px = CarSource(x_m=2.2, t_meet_s=1.5, t_overtake_s=None)
    rep2 = evaluate(_bands(), (), (car_px,), 0.0, _params())
    assert rep2 is not None
    c2 = _best_by_x(rep2)[5.25]
    assert c2.pass_value_sum == pytest.approx(30.0)
    assert c2.hug_value == 0.0


def test_instruments_failed_falls_back_to_legacy():
    """仪器失效（t_overtake=None）：走旧错身项，行为逐位回今天（闸开着也一样）。"""
    car = CarSource(x_m=2.2, t_meet_s=1.5, t_overtake_s=None)
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    rep_off = evaluate(_bands(), (), (car,), 0.0, _params_off())
    assert rep is not None and rep_off is not None
    for c, c_off in zip(rep.candidates, rep_off.candidates):
        assert c.score == pytest.approx(c_off.score)


# ---------- 多车累加 ----------

def test_two_cars_accumulate_hug_value():
    """双车分居两侧评分区：窗内车（平台）+ 窗外车（衰减）逐车累加。"""
    near = _car(2.2, t_overtake=1.5)            # 候选 4.25：d̂=2.05 窗内
    far = _car(1.2, t_overtake=1.5)             # 候选 4.25：d̂=3.05 衰减区
    rep = evaluate(_bands(), (), (near, far), 0.0, _params())
    assert rep is not None
    c = _best_by_x(rep)[4.25]
    assert c.hug_cars == 2
    assert c.hug_value == pytest.approx(PLATFORM + 30.0 + LIMIT * _p(3.05))


# ---------- 占用/遮挡/碰撞判据照旧 ----------

def test_crash_unchanged_for_hug_car():
    """贴身窗前车到站 < crash_t：碰撞判据照旧（大负罚，第三方与贴身目标同判）。
    crash 判据吃 t_meet（米制车 t_meet=t_overtake 同源，仪器链同值）。"""
    car = _car(2.2, t_overtake=0.5, t_meet=0.5)
    # 候选 1.25：|2.2−1.25|=0.95 in-path，need(1.25)=0.425 ≤ 0.5 < 0.8 → crash
    rep = evaluate(_bands(), (), (car,), 0.0, _params())
    assert rep is not None
    c = _best_by_x(rep)[1.25]
    assert c.crash
    assert c.score == pytest.approx(-100.0)


def test_coin_occlusion_unchanged_for_hug_car():
    """贴身窗前车仍记 in-path：遮挡语义照旧（先于币到达的车把币挡住）。"""
    coin = CoinSource(x_m=4.25, observed_count=3, conf=0.85, t_miss_s=1.5)
    car = _car(2.2, t_overtake=1.5, t_meet=1.5)  # |2.2−4.25|=2.05 in-path
    # need=0.725 ≤ 1.5 ≤ t_miss=1.5 → 币被挡（照旧）
    rep = evaluate(_bands(), (coin,), (car,), 0.0, _params())
    assert rep is not None
    c = _best_by_x(rep)[4.25]
    assert c.coin_value == 0.0          # 被贴身车挡住（照旧）
    rep_nocar = evaluate(_bands(), (coin,), (), 0.0, _params())
    assert rep_nocar is not None
    assert _best_by_x(rep_nocar)[4.25].coin_value == pytest.approx(30.0 * 3 * 0.85)


# ---------- 闸关逐位（回放等价性） ----------

def test_gate_off_ignores_overtake_instrument():
    """hug 参数全缺（闸关）：米制锚车被整体忽略——t_overtake 不进任何分支，
    行为与 R3 前逐位一致（窗内米制车不再得分，旧错身带像素车照旧）。"""
    # 米制锚车在贴身窗内（d̂=2.05）但 t_meet=None：闸关 → 不进旧错身项（None
    # 不判错身）也不进贴身项 → 零贡献；闸开 → 平台值
    car_win = _car(2.2, t_overtake=1.5, t_meet=None)
    rep_off = evaluate(_bands(), (), (car_win,), 0.0, _params_off())
    assert rep_off is not None
    c = _best_by_x(rep_off)[4.25]
    assert c.score == 0.0
    assert c.hug_value == 0.0 and c.hug_cars == 0
    rep_on = evaluate(_bands(), (), (car_win,), 0.0, _params())
    assert rep_on is not None
    assert _best_by_x(rep_on)[4.25].score == pytest.approx(PLATFORM)
    # 旧错身带像素车：闸关/闸开同值（仪器分支不变）
    car_px = CarSource(x_m=2.2, t_meet_s=1.5, t_overtake_s=None)
    rep_off2 = evaluate(_bands(), (), (car_px,), 0.0, _params_off())
    rep_on2 = evaluate(_bands(), (), (car_px,), 0.0, _params())
    s_off = _best_by_x(rep_off2)[5.25].score
    s_on = _best_by_x(rep_on2)[5.25].score
    assert s_off == pytest.approx(30.0) and s_on == pytest.approx(s_off)


# ---------- 窗沿/换算与边界几何（config 校验） ----------

_MIN_CONFIG = {
    "schema_version": 1,
    "mode": {"allow_all_moves": True, "allow_car_graze": False},
    "control": {"frame_rate_hz": 20.0, "t_change_max_s": 2.0,
                "abort_settle_v_lane_s": 0.3},
    "timing": {"tau_resp_s": 0.13, "lane_change_base_s": 0.3,
               "lane_change_k_s_per_lane": 0.4, "margin_s": 0.15},
    "scoring": {"coin_value_per_unit": 30.0, "coin_group_min_count": 3.0,
                "conf_floor": 0.5, "conf_hi": 0.8,
                "shift_cost_per_lane": 10.0, "life_tau_s": 5.0},
    "hysteresis": {"switch_margin": 15.0, "min_score": 20.0,
                   "t_cool_s": 1.0, "tau_filter_s": 0.15,
                   "dead_zone_lane": 0.15},
    "validate": {"t_empty_s": 2.0, "t_recover_s": 1.0,
                 "t_conserve_max_s": 10.0, "x_lane_abs_max": 2.6,
                 "cy_jump_max_px": 90.0,
                 "lat_veto_gap_lane": 0.45, "lat_veto_horizon_s": 1.5},
    "planner": {"lookahead_tau_s": 0.25, "k_p": 0.7, "k_d": 0.2,
                "tau_steer_s": 0.1, "rate_limit_raw": 22000.0,
                "stick_deadzone_raw": 256.0, "throttle_raw": 255,
                "a_lat_gain": 6.0, "tau_align_s": 0.5, "v_lat_max": 2.5,
                "hold_max_ticks": 3, "obs_alpha": 0.3, "obs_beta": 0.5,
                "obs_jump_max_lane": 0.8, "anchor_max_off": 1.0,
                "anchor_stale_s": 1.0, "hold_deadband_lane": 0.5,
                "hold_stick_max": 0.45},
    "overtake": {"value_base": 30.0, "value_limit": 120.0,
                 "p_center_lane": 0.73, "p_scale_lane": 0.06,
                 "d_hold_lane": 0.625, "lane_band_lo": 0.65,
                 "lane_band_hi": 1.6, "t_pass_max_s": 8.0,
                 "space_margin_lane": 0.15, "t_cool_pass_s": 0.2,
                 "ttc_dodge_s": 0.5},
    "traffic": {"exit_margin_px": 80.0, "presumed_cy_px": 430.0,
                "presumed_lane_max": 2.5, "min_obs_ticks": 3,
                "ghost_max_age_ticks": 200, "ghost_rel_eps": 1.0,
                "v_lat_ema_alpha": 0.3},
    "corridor_reach": {"enabled": True, "control": True},
}


def _read_cfg(cr_overrides: dict):
    """最小合法 config + corridor_reach 覆写 → 临时文件读侧（fail-loud 校验）。"""
    import copy
    import json
    import tempfile
    from pathlib import Path

    from maaracing_master.plugins.speedrush.config import _read_decision
    d = copy.deepcopy(_MIN_CONFIG)
    d["corridor_reach"].update(cr_overrides)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(d, f)
        p = Path(f.name)
    try:
        return _read_decision(p)
    finally:
        p.unlink(missing_ok=True)


def test_config_hug_gate_reads_and_validates():
    """闸开 + 合法半宽：读取成功且窗沿派生值在界内（0 < lo < center ≤ hi）。"""
    cfg = _read_cfg({"overtake_hunting": True, "hug_value": True,
                     "hug_half_width_lane": 0.075})
    assert cfg.corridor_reach.hug_half_width_lane == 0.075
    assert 0 < cfg.overtake.d_hold_lane - cfg.corridor_reach.hug_half_width_lane


def test_config_rejects_half_width_ge_center():
    """半宽 ≥ 平台中心：窗下沿 ≤0 → 拒载（fail-loud，设计稿 §2.2）。"""
    import pytest
    with pytest.raises(ValueError, match="贴身窗半宽"):
        _read_cfg({"overtake_hunting": True, "hug_value": True,
                   "hug_half_width_lane": 0.7})


def test_config_default_gate_off():
    """部署 json 默认（无新键）：闸全关，行为逐位回今天。"""
    cfg = _read_cfg({})
    assert cfg.corridor_reach.overtake_hunting is False
    assert cfg.corridor_reach.hug_value is False


# ---------- 决策边界接线（t_overtake 注入 + 闸挂参数） ----------

def test_boundary_injects_overtake_and_hug_params():
    """闸开：参数面挂贴身字段（米制换算）、米制车带 t_overtake；
    闸关：贴身字段全缺（t_overtake 是仪器数据面，与闸无关照常携带——
    evaluate 闸关时不读它，行为逐位回今天）。"""
    from dataclasses import replace as dreplace

    from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH
    from maaracing_master.plugins.speedrush.config import _read_decision
    from maaracing_master.plugins.speedrush.decision import DecisionEngine
    from maaracing_master.plugins.speedrush.tracking import MetricCarState
    from maaracing_master.plugins.speedrush.world_model import load_calib

    base = _read_decision(CFG_PATH)
    lane_w = load_calib().lane_w_m
    # 闸开（不落全局缓存，直构引擎）
    eng_on = DecisionEngine(dreplace(
        base, corridor_reach=dreplace(base.corridor_reach,
                                      overtake_hunting=True, hug_value=True)))
    p = eng_on._reach_params
    assert p.hug_d_lo_m == pytest.approx((0.625 - 0.075) * lane_w)
    assert p.hug_d_hi_m == pytest.approx((0.625 + 0.075) * lane_w)
    assert p.hug_p_center_m == pytest.approx(0.73 * lane_w)
    assert p.hug_value_limit == 120.0
    # 闸关（显式构造，不依赖部署 json 当前闸态——部署文件随实机 A/B 翻闸）：
    # 贴身字段全缺
    eng_off = DecisionEngine(dreplace(
        base, corridor_reach=dreplace(base.corridor_reach,
                                      overtake_hunting=False, hug_value=False)))
    p_off = eng_off._reach_params
    assert p_off.hug_d_lo_m is None and p_off.hug_value_limit == 0.0

    # 车仪器链：v_close>0 的米制车 → t_overtake = z/v_close；v_close 缺位 → None
    from maaracing_master.plugins.speedrush.tracking import DistanceSource
    met = MetricCarState(target_id=7, source=DistanceSource.RESCALED_ANCHOR,
                         z_m=30.0, x_m=1.0, v_close_mps=20.0, t_meet_s=1.5,
                         anchor_age_ms=50.0)
    met_no_v = MetricCarState(target_id=8, source=DistanceSource.ANCHOR,
                              z_m=30.0, x_m=1.0, v_close_mps=None,
                              t_meet_s=None, anchor_age_ms=10.0)

    class _V:
        id = 7
        x_lane = 0.3
        x_memb = None
        cy = 500.0
        rel_approach = 5.0

    class _V2(_V):
        id = 8

    eng_on._metric_cars = (met, met_no_v)
    eng_on._views = (_V(), _V2())
    sources = eng_on._reach_car_sources(0.0)
    assert len(sources) == 2
    s_met = next(s for s in sources if s.t_overtake_s is not None)
    assert s_met.t_overtake_s == pytest.approx(1.5)     # 30/20
    s_no = next(s for s in sources if s.t_overtake_s is None)
    assert s_no.t_meet_s is not None                    # 像素回退 t_meet 在
    # 闸关引擎：t_overtake 照常携带（数据面），但参数面无贴身字段 → 不被消费
    eng_off._metric_cars = (met,)
    eng_off._views = (_V(),)
    sources_off = eng_off._reach_car_sources(0.0)
    assert any(s.t_overtake_s == pytest.approx(1.5) for s in sources_off)
