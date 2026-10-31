# -*- coding: utf-8 -*-
"""S1 影子线观测：等间距 + 深度锚定、颜色无关、锚跳变确认与降级。

2026-10-24 重写后，道号不再由黄边界/颜色决定：每拍用新鲜深度路心做锚点，
``k=round(anchor+L)``，线只负责等间距约束与子道残差修正。本文件锁住这条
口径，以及 worker 的同拍快照/锚点入队协议。
"""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from maaracing_master.plugins.speedrush import lane_obs as la
from maaracing_master.plugins.speedrush.config import load_decision
from maaracing_master.plugins.speedrush.lane_obs import (
    COAST_MAX_TICKS, LaneObsConfig, LaneObsWorker, LineTracker, _fit)


def _line(L, color="y", n=20, theta=0.0, side=None):
    if side is None:
        side = -1 if L < 0 else 1
    return {"L": L, "color": color, "n": n, "theta": theta,
            "yellow": 1.0 if color == "y" else 0.0,
            "white": 1.0 if color == "w" else 0.0, "side": side}


def _synth_line(s, a, y0=560, y1=700, n=15):
    """全幅像素直线 x = s·y + a → 模型半分辨率点集（_fit 的输入口径）。"""
    return [{"yc": y / 2.0, "xc": (s * y + a) / 2.0, "p": 1.0}
            for y in (y0 + i * (y1 - y0) / (n - 1) for i in range(n))]


def test_line_theta_uses_given_focal():
    """线角正切 = 像素偏置 ÷ 调用方给的焦距（标定值）：焦距越小线角越大；
    无焦距只丢线角、不丢横位（模块内不再有焦距字面量）。"""
    pts = _synth_line(0.05, 520.0)
    a670 = _fit(pts, 1.28, *la.NEAR_Y, 670.0)
    a808 = _fit(pts, 1.28, *la.NEAR_Y, 807.7)
    assert a670["L"] == pytest.approx(a808["L"])      # 横位不依赖焦距
    assert a670["theta"] is not None and a808["theta"] is not None
    ratio = math.tan(a670["theta"]) / math.tan(a808["theta"])
    assert ratio == pytest.approx(807.7 / 670.0, rel=1e-9)
    none = _fit(pts, 1.28, *la.NEAR_Y, None)
    assert none["theta"] is None
    assert none["L"] == pytest.approx(a670["L"])


def test_worker_focal_holds_last_valid():
    """缺值/越界焦距保持上一次有效测量（不退回常量）。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    w._set_focal(700.0)
    assert w._focal == pytest.approx(700.0)
    w._set_focal(None)
    assert w._focal == pytest.approx(700.0)
    w._set_focal(800.0)
    assert w._focal == pytest.approx(700.0 + la.FOCAL_EMA * 100.0)
    w._set_focal(300.0)                               # 物理带外
    assert w._focal == pytest.approx(700.0 + la.FOCAL_EMA * 100.0)


def test_config_defaults_off_and_detect_every_frame():
    assert LaneObsConfig().enabled is False
    assert LaneObsConfig().detect_every == 1
    assert LaneObsConfig().provider == "cpu"
    assert load_decision().lane_obs is not None
    assert load_decision().lane_obs.detect_every == 1
    # 影子不得与生产深度抢 DML 锁（2026-10-24 实机相位锁把深度饿到 5%）
    assert load_decision().lane_obs.provider == "cpu"


def test_no_anchor_stays_relock():
    """没有深度锚点时不凭空造绝对位；等第一拍新鲜路心。"""
    tr = LineTracker()
    r = tr.update([_line(-0.2)], 0, 0.05)
    assert r["state"] == "RELOCK" and r["x"] is None
    assert r["n_locked"] == 0


def test_anchor_beyond_physical_limit_is_garbage():
    """深度锚超出物理横位上限（车心离路心 >2.6 道）当垃圾：不能拿它给线
    定道号（整道错位/坏平面），本拍诚实弃权；上限内照常锚定。"""
    tr = LineTracker()
    bad = tr.update([_line(-0.2)], 1, 0.05, anchor=2.7)
    assert bad["state"] == "RELOCK" and bad["x"] is None
    assert bad["anchor_src"] == "none"
    ok = tr.update([_line(-0.2)], 2, 0.05, anchor=2.1)
    assert ok["state"] == "TRACK" and ok["anchor_src"] == "depth"
    assert ok["x"] == pytest.approx(2.2, abs=1e-6)


def test_worker_injects_off_max_into_tracker():
    w = LaneObsWorker(LaneObsConfig(enabled=True), off_max=2.6)
    assert w._tracker.OFF_MAX == pytest.approx(2.6)


def test_off_max_default_matches_policy():
    """直构缺省与 policy 的物理上限同值：防止两条尺子各自漂移。"""
    assert la.OFF_MAX == pytest.approx(
        load_decision().validate.x_lane_abs_max)


def test_anchor_sets_integer_k_and_sub_lane_x():
    """anchor=0、线在左 0.2 道 → k=0，子道修正给出 x=+0.2。"""
    tr = LineTracker()
    r = tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    assert r["state"] == "TRACK" and r["relock_src"] == "line"
    assert r["anchor_src"] == "depth"
    assert r["lines"][0]["k"] == 0
    assert r["x"] == pytest.approx(0.2, abs=1e-6)


def test_anchor_is_ego_offset_not_road_center():
    """锚点口径 = 自车横位（右正，与输出 x 同号），不是路心相对自车的位置。

    自车在路心右 0.5 道、两条线分别在其右 0.5 / 左 1.5 道：
    k=round(0.5+L) → (+1, −1)，x=k−L → +0.5。若把路心位置（−0.5）当锚，
    会得到 k=(0, −2)、x=−0.5——整张网格反向偏一道。
    """
    tr = LineTracker()
    r = tr.update([_line(0.5, "w"), _line(-1.5, "w")], 1, 0.05, anchor=0.5)
    assert r["state"] == "TRACK"
    assert [ln["k"] for ln in r["lines"]] == [1, -1]
    assert r["x"] == pytest.approx(0.5, abs=1e-6)


def test_line_lock_ignores_depth_anchor_disagreement():
    """线连续稳定 5 拍后，深度锚整道跳变不能改判 k、也不能否决线：
    输出仍是线的绝对横位（usable=True）。旧口径会沿锚点把两线同时改号，
    横位整道跳到 1.1。"""
    tr = LineTracker()
    lines = [_line(-1.1, "w"), _line(0.9, "w")]
    for fid in range(1, 6):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
        assert r["x"] == pytest.approx(0.1, abs=1e-6)
    assert r["usable"] is True and r["locked"] >= 5
    r2 = tr.update(lines, 6, 0.05, anchor=1.0)      # 深度锚整道跳
    assert r2["state"] == "TRACK"
    assert r2["x"] == pytest.approx(0.1, abs=1e-6)
    assert r2["usable"] is True
    assert [ln["k"] for ln in r2["lines"] if ln.get("accepted")] == [-1, 1]


def test_strong_lock_allows_single_line_origin():
    """强锁（曾以双侧严格条件建锁）之后，只剩一条线也继续作绝对位置：
    线已锁定几十拍时把原点让给深度，深度会差 1~2 道（实测残留整道跳）。"""
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    for fid in range(1, 8):
        tr.update(both, fid, 0.05, anchor=0.0)
    r = tr.update([_line(0.9, "w")], 8, 0.05, anchor=0.0)
    assert r["state"] == "TRACK"
    assert r["x"] == pytest.approx(0.1, abs=1e-6)
    assert r["usable"] is True


def test_coast_keeps_line_lock():
    """短暂 COAST 不清线锁：轨迹 k 还在，一两拍线不合格不该把原点交给
    深度；强锁在横位跳变/RELOCK 时才作废。"""
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    for fid in range(1, 8):
        tr.update(both, fid, 0.05, anchor=0.0)
    r = tr.update([], 8, 0.05, anchor=0.1)          # 无可用线但有深度锚
    assert r["state"] == "COAST"
    r2 = tr.update(both, 9, 0.05)                   # 线回来（深度空窗）
    assert r2["state"] == "TRACK" and r2["usable"] is True


def test_usable_requires_both_sides():
    """位置原点要求左右两侧都有高质量线；同侧两条只能给相对横位，
    usable=False（不能当绝对位置）。"""
    tr = LineTracker()
    lines = [_line(0.9, "w"), _line(2.9, "w")]
    for fid in range(1, 7):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
    assert r["state"] == "TRACK" and r["n_locked"] == 2
    assert r["usable"] is False


def test_equal_spacing_and_color_independent():
    """同样的 L 集合，颜色互换不改变 k/横位——颜色不再是身份。"""
    lines_a = [_line(-1.1, "y"), _line(0.9, "w")]
    lines_b = [_line(-1.1, "w"), _line(0.9, "y")]
    a = LineTracker().update(lines_a, 1, 0.05, anchor=0.0)
    b = LineTracker().update(lines_b, 1, 0.05, anchor=0.0)
    assert [ln["k"] for ln in a["lines"]] == [-1, 1]
    assert a["x"] == pytest.approx(0.1, abs=1e-6)
    assert b["x"] == pytest.approx(a["x"], abs=1e-6)
    assert [ln["k"] for ln in b["lines"]] == [-1, 1]


def test_same_k_duplicate_keeps_smaller_residual():
    """同一整数道号的两条线只留残差小的；等间距下不应有重复身份。"""
    tr = LineTracker()
    r = tr.update([_line(-0.1), _line(-0.3)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 1
    assert r["x"] == pytest.approx(0.1, abs=1e-6)
    assert any(ln.get("reject") == "dup" for ln in r["lines"])


def test_outlier_residual_rejected():
    """残差相对中值偏超 OUTLIER_TOL 的线不参与本拍（野线不拖横位）。"""
    tr = LineTracker()
    r = tr.update([_line(0.0), _line(1.0), _line(2.4)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 2
    assert r["x"] == pytest.approx(0.0, abs=1e-6)
    assert any(ln.get("reject") == "outlier" for ln in r["lines"])


def test_resid_over_gate_rejected():
    tr = LineTracker()
    r = tr.update([_line(0.47)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 0
    assert r["state"] == "COAST"
    assert r["lines"][0].get("reject") == "resid"


def test_anchor_jump_reanchors_after_confirm():
    """2 道级锚跳变：首拍横位跟深度兜底，连续两拍后持久道号才改。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    first = tr.update([_line(-0.2)], 2, 0.05, anchor=2.0)
    assert first["anchor_src"] == "depth"
    assert first["n_locked"] == 0
    assert first["x"] == pytest.approx(2.0, abs=1e-6)
    jump = tr.update([_line(-0.2)], 3, 0.05, anchor=2.0)
    assert jump["anchor_src"] == "depth"
    assert jump["x"] == pytest.approx(2.2, abs=1e-6)


def test_stale_k_fixed_same_frame_when_two_lines_confirm_anchor():
    """两条线同拍支持新鲜锚点、且锚点离当前横位不远：当拍纠正陈旧道号。"""
    tr = LineTracker()
    tr.update([_line(-0.25), _line(0.86)], 1, 0.05, anchor=-1.0)
    assert [ln["k"] for ln in tr.last_lines] == [-1, 0]
    tr.tracks[1].k = 1                       # 模拟空窗里被带偏的陈旧道号
    r = tr.update([_line(-0.25), _line(0.86)], 2, 0.05, anchor=-1.0)
    assert r["anchor_src"] == "depth"
    assert r["n_locked"] == 2
    assert [ln["k"] for ln in r["lines"]] == [-1, 0]
    assert r["x"] == pytest.approx(-0.805, abs=1e-3)


def test_one_lane_depth_jump_still_waits_for_confirm():
    """整道级深度跳变即使两根线都支持，也不当拍翻道（须连续确认）。"""
    tr = LineTracker()
    tr.update([_line(-0.9), _line(0.1)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.9), _line(0.1)], 2, 0.05, anchor=1.0)
    assert r["n_locked"] == 0
    assert r["x"] == pytest.approx(1.0, abs=1e-6)


def test_half_lane_noise_does_not_trigger_same_frame_reanchor():
    """0.5 道级深度噪声即使两根线都能凑上，也不当拍翻道；仍走两拍确认。"""
    tr = LineTracker()
    tr.update([_line(-0.9), _line(0.1)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.9), _line(0.1)], 2, 0.05, anchor=0.5)
    assert r["n_locked"] == 0
    assert r["x"] == pytest.approx(0.5, abs=1e-6)


def test_single_line_still_waits_for_anchor_confirm():
    """只有一条线时不享当拍重锚：单拍深度噪声不能翻道。"""
    tr = LineTracker()
    tr.update([_line(-0.9)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.9)], 2, 0.05, anchor=2.0)
    assert r["n_locked"] == 0
    assert r["x"] == pytest.approx(2.0, abs=1e-6)


def test_no_fresh_anchor_refines_against_last_x():
    """深度空窗：锚点退回上一拍横位，线只做子道细化，不自漂绝对位。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.3)], 2, 0.05, anchor=None)
    assert r["state"] == "TRACK"
    assert r["anchor_src"] == "lines"
    assert r["x"] == pytest.approx(0.3, abs=1e-6)


def test_yellow_wrong_fit_sign_is_rejected():
    """左黄线拟合出正 L（实拍 p2 fid 4123）不作绝对参照。"""
    tr = LineTracker()
    r = tr.update([_line(4.43, side=-1)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 0
    assert tr.side_reject == 1
    assert r["lines"][0].get("reject") == "side"


def test_yellow_unknown_side_can_still_be_used():
    """侧别未知不再一票否决：道号由深度锚定，颜色/侧别只做质量提示。"""
    tr = LineTracker()
    r = tr.update([_line(0.3, side=0)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 1
    assert r["x"] == pytest.approx(-0.3, abs=1e-6)


def test_short_gap_coasts_then_relocks():
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    for fid in range(2, 2 + COAST_MAX_TICKS):
        r = tr.update([], fid, 0.05, anchor=None)
        assert r["state"] == "COAST"
        assert r["x"] is not None
    r = tr.update([], 2 + COAST_MAX_TICKS, 0.05, anchor=None)
    assert r["state"] == "RELOCK"


def test_blind_coast_stops_extrapolating_after_budget():
    """盲窗封顶（2026-10-31）：线不可用又无深度锚时，横位只按上次横速外推；
    超过 COAST_MAX_TICKS 就没有可积的运动证据了，位置必须冻住——旧行为会
    以恒速一路外推到整道外，随后被新鲜证据整道拽回。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    tr.x_vel = 1.0                       # 人为给一个恒速外推
    for fid in range(2, 2 + COAST_MAX_TICKS):
        tr.update([], fid, 0.05, anchor=None)
    frozen = tr.x_last
    # 检测拍继续空：位置不动（状态 RELOCK，外推已封顶）
    for fid in range(2 + COAST_MAX_TICKS, 2 + COAST_MAX_TICKS + 6):
        r = tr.update([], fid, 0.05, anchor=None)
        assert r["state"] == "RELOCK"
        assert r["lost"] >= COAST_MAX_TICKS
        assert r["x"] == pytest.approx(frozen, abs=1e-9)
    # 非检测帧同样不再推进
    r = tr.predict(0.05)
    assert r["x"] == pytest.approx(frozen, abs=1e-9)


def test_coast_anchor_accepts_line_near_prediction():
    """空窗后线在上一横位附近重新可用：按上一横位定 k，重新 TRACK。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    for fid in range(2, 5):
        tr.update([], fid, 0.05, anchor=None)
    r = tr.update([_line(-1.0)], 5, 0.05, anchor=None)
    assert r["state"] == "TRACK"
    assert r["relock_src"] == "line"
    assert r["x"] == pytest.approx(0.0, abs=0.2)


def test_new_line_k_uses_all_live_lines_when_no_anchor():
    """空窗里新线由所有在期线投票定号；票一致才采用。"""
    tr = LineTracker()
    tr.update([_line(-1.9), _line(0.0), _line(1.0)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-1.9), _line(-0.9), _line(0.0), _line(1.0)],
                  2, 0.05, anchor=None)
    new = [ln for ln in r["lines"] if ln["L"] == -0.9][0]
    assert new["k"] == -1
    assert new["accepted"] is True
    assert r["n_locked"] == 4


def test_new_line_k_rejected_when_live_lines_disagree():
    """邻居自己漂了一道时票会分歧：宁可不给新线定号，也不照抄单条邻居。"""
    tr = LineTracker()
    tr.update([_line(-1.0), _line(0.0)], 1, 0.05, anchor=0.0)
    tr.update([_line(-1.0)], 2, 0.05, anchor=2.0)
    tr.update([_line(-1.0)], 3, 0.05, anchor=2.0)
    r = tr.update([_line(-0.5)], 4, 0.05, anchor=None)
    new = r["lines"][0]
    assert new["k"] is None
    assert new["accepted"] is False
    assert r["n_locked"] == 0


def _warm_lock(tr: LineTracker, lines: list[dict], n: int = 6) -> dict:
    r = {}
    for fid in range(1, n + 1):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
    assert r["state"] == "TRACK" and r["locked"] >= la.LINE_LOCK_TICKS
    return r


def test_locked_grid_anchors_new_line_to_its_x():
    """锁定期新线按网格自身横位接号，不靠邻居相对票（164953 局 p2 判据）。

    网格 x=+0.1，新线在右 2.0 道 → k=round(0.1+2.0)=2，横位保持 +0.1。
    若按邻居票/整道改判，新线会把网格拽走；这里只接号、不重定网格。
    """
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    r = _warm_lock(tr, both)
    x0 = r["x"]
    r2 = tr.update([*both, _line(2.0, "w")], 7, 0.05, anchor=None)
    new = [ln for ln in r2["lines"] if ln["L"] == 2.0][0]
    assert new["k"] == 2 and new["accepted"] is True
    assert r2["x"] == pytest.approx(x0, abs=1e-6)


def test_locked_grid_rejects_half_lane_new_line():
    """半道位置的新线不接号：x_last+L 落在整数半道，残差门拒，不硬取整。

    这是网格只能在整道身份内接新线、不能被半道拖走的边界。"""
    tr = LineTracker()
    r = _warm_lock(tr, [_line(-1.1, "w"), _line(0.9, "w")])
    r2 = tr.update([_line(-1.1, "w"), _line(0.9, "w"), _line(2.36, "w")],
                   7, 0.05, anchor=None)
    new = [ln for ln in r2["lines"] if ln["L"] == 2.36][0]
    assert new["k"] is None and new["accepted"] is False
    assert r2["x"] == pytest.approx(r["x"], abs=1e-6)


def test_locked_grid_survives_short_coast():
    """锁定后短断一拍（COAST）不解除冻结：线回来时仍按网格横位接号。"""
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    r = _warm_lock(tr, both)
    x0 = r["x"]
    coast = tr.update([], 7, 0.05, anchor=None)
    assert coast["state"] == "COAST" and tr.locked >= la.LINE_LOCK_TICKS
    r2 = tr.update([*both, _line(2.0, "w")], 8, 0.05, anchor=None)
    new = [ln for ln in r2["lines"] if ln["L"] == 2.0][0]
    assert new["k"] == 2 and new["accepted"] is True
    assert r2["x"] == pytest.approx(x0, abs=0.05)   # 滑行期允许小量外推
    # 冻结期新鲜深度锚不改判身份：新线仍接网格号，横位不跳
    r3 = tr.update([*both, _line(2.0, "w")], 9, 0.05, anchor=0.3)
    new = [ln for ln in r3["lines"] if ln["L"] == 2.0][0]
    assert new["k"] == 2 and new["accepted"] is True
    assert r3["x"] == pytest.approx(x0, abs=0.05)


def test_relock_without_depth_reanchors_from_last_x():
    """深度断供的长盲窗后，线靠上一横位重新接号，不再永久卡 RELOCK。"""
    tr = LineTracker()
    _warm_lock(tr, [_line(-1.1, "w"), _line(0.9, "w")])
    for fid in range(7, 7 + COAST_MAX_TICKS + 2):
        r = tr.update([], fid, 0.05, anchor=None)
    assert r["state"] == "RELOCK" and tr.locked == 0
    # 与冻结横位一致的线组 → 直接上线（k 由 x_last 取整）
    r2 = tr.update([_line(-0.2, "w"), _line(0.8, "w")], 20, 0.05,
                   anchor=None)
    assert r2["state"] == "TRACK"
    assert [ln["k"] for ln in r2["lines"] if ln["accepted"]] == [0, 1]
    assert r2["x"] == pytest.approx(0.2, abs=1e-6)
    # 半道对不上的线仍不接号（不凭空造绝对位）
    tr2 = LineTracker()
    _warm_lock(tr2, [_line(-1.1, "w"), _line(0.9, "w")])
    for fid in range(7, 7 + COAST_MAX_TICKS + 2):
        r = tr2.update([], fid, 0.05, anchor=None)
    r3 = tr2.update([_line(-0.64, "w")], 20, 0.05, anchor=None)
    assert r3["lines"][0]["k"] is None
    assert r3["n_locked"] == 0


def test_relock_allows_anchor_to_assign_k_again():
    """冻结只在锁定期：跳变/RELOCK 后新线仍由深度锚重新定号。"""
    tr = LineTracker()
    _warm_lock(tr, [_line(-1.1, "w"), _line(0.9, "w")])
    for fid in range(7, 7 + COAST_MAX_TICKS + 2):
        r = tr.update([], fid, 0.05, anchor=None)
    assert r["state"] == "RELOCK" and tr.locked == 0
    r2 = tr.update([_line(-0.2, "w"), _line(0.8, "w")], 20, 0.05,
                   anchor=0.0)
    assert [ln["k"] for ln in r2["lines"] if ln["accepted"]] == [0, 1]
    assert r2["x"] == pytest.approx(0.2, abs=1e-6)


def test_theta_weighted_by_lines():
    tr = LineTracker()
    r = tr.update([_line(-1.0, theta=0.1, n=10),
                   _line(1.0, theta=0.3, n=30)], 1, 0.05, anchor=0.0)
    assert r["theta"] == pytest.approx(0.25, abs=1e-6)


def test_near_side_uses_pixel_side_not_fit_sign():
    from maaracing_master.plugins.speedrush.lane_obs import (
        VPX, _near_x, _side_from_x)
    left = [{"yc": 300 + i, "xc": 200.0} for i in range(6)]
    right = [{"yc": 300 + i, "xc": 440.0} for i in range(6)]
    mid = [{"yc": 300, "xc": VPX / 2.0}]
    assert _side_from_x(_near_x(left)) == -1
    assert _side_from_x(_near_x(right)) == 1
    assert _side_from_x(_near_x(mid)) is None


def test_lane_cam_h_falls_back_outside_bumper_band():
    from maaracing_master.plugins.speedrush.lane_obs import (
        DEFAULT_CAM_H, _lane_cam_h)
    assert _lane_cam_h(4.97) == (DEFAULT_CAM_H, True)
    assert _lane_cam_h(3.603) == (DEFAULT_CAM_H, True)
    assert _lane_cam_h(None) == (DEFAULT_CAM_H, True)
    assert _lane_cam_h(1.40) == (1.40, False)


def test_worker_disabled_is_noop():
    w = LaneObsWorker(LaneObsConfig(enabled=False))
    w.start()
    assert w.latest() is None
    w.stop()


def test_worker_missing_weights_degrades():
    w = LaneObsWorker(LaneObsConfig(enabled=True, weights="不存在.onnx"))
    w.start()
    assert w.error is not None
    assert w.latest() is None
    w.stop()


def test_dml_busy_skips_without_calling_model(monkeypatch):
    import maaracing_master.plugins.speedrush.lane_obs as la
    w = LaneObsWorker(LaneObsConfig(enabled=True, provider="dml"))
    w._use_dml = True
    w._sess = object()
    called = {"n": 0}

    class _Sess:
        def get_inputs(self):
            return [type("I", (), {"name": "input"})()]

        def run(self, *args):
            called["n"] += 1

    class _Lock:
        def acquire(self, blocking=False):
            return False

        def release(self):
            pass

    w._sess = _Sess()
    monkeypatch.setattr(la, "_letterbox", lambda rgb: np.zeros((1, 3, 384, 640),
                                                              np.float32))
    monkeypatch.setattr(la.dml_lock, "LOCK", _Lock())
    assert w._run(np.zeros((8, 8, 3), np.uint8)) is None
    assert w.busy_skips == 1
    assert called["n"] == 0


def test_latest_is_one_consistent_snapshot():
    """横位、线、锚点必须同拍；不再把新预测和旧检测线拼在一起。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    now = time.perf_counter()
    snap = {"fid": 10, "cam_h": 1.4, "pred": False, "ms": 11.0,
            "n_lines": 1, "x": 0.40, "theta": 0.01, "state": "TRACK",
            "n_locked": 1, "relock_src": "line", "anchor_src": "depth",
            "grid_reject": 0, "side_reject": 0,
            "lines": [{"L": 0.2, "k": 0, "resid": 0.2, "accepted": True}],
            "t_done": now, "t_det": now}
    w._latest = snap
    out = w.latest()
    assert out["x"] == 0.40
    assert out["lines"] == snap["lines"]
    assert out["anchor_src"] == "depth"
    assert out["pred"] is False
    assert out["det_age_ms"] is not None and out["det_age_ms"] >= 0.0


def test_worker_snapshot_carries_line_origin_flags():
    """线程快照必须搬运 usable/locked：漏搬时消费方永远读 None，线为主
    静默失效——2026-10-31 实机一局整局没生效的根因就是这里漏了两个键。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    res = {"state": "TRACK", "x": 0.4, "theta": 0.01, "n_locked": 2,
           "relock_src": "line", "n_lines": 2, "grid_reject": 0,
           "side_reject": 0, "anchor_src": "lines",
           "usable": True, "locked": 7, "lines": [], "pred": False}
    w._focal = 671.3
    snap = w._snapshot(res, 10, 1.28, 0.0, 0.02, False)
    assert snap["usable"] is True and snap["locked"] == 7
    assert snap["focal"] == pytest.approx(671.3)     # 线角尺度可审计


def test_predict_marks_usable_false():
    """滑行预测不是测量：可作位置原点的标志必须为 False，位置交给执行层
    的短断供预算，而不是把外推值冒充新线。"""
    tr = LineTracker()
    lines = [_line(-1.1, "w"), _line(0.9, "w")]
    r = None
    for fid in range(1, 7):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
    assert r["usable"] is True
    p = tr.predict(0.05, 0.0)
    assert p["usable"] is False and p["pred"] is True


def test_push_without_throttle_queues_every_tick():
    """hz=0：每拍都推，由队列深度 1 丢旧帧限流。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True, hz=0.0))
    w._sess = object()
    rgb = np.zeros((4, 4, 3), np.uint8)
    for fid in range(3):
        w.push(rgb, fid, 1.4, 0.0, 0.05, False)
    assert w.push_n == 3
    assert w.drop_n == 2


def test_push_plumbs_same_frame_anchor():
    """锚点跟帧入队：worker 拿到的深度锚点与这条帧同拍。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True, hz=0.0))
    w._sess = object()
    rgb = np.zeros((4, 4, 3), np.uint8)
    w.push(rgb, 7, 1.4, 0.0, 0.05, False, anchor=0.7)
    item = w._q.get_nowait()
    assert item[-1] == pytest.approx(0.7)
    assert item[1] == 7


def test_push_throttle_honored_when_configured():
    w = LaneObsWorker(LaneObsConfig(enabled=True, hz=10.0))
    w._sess = object()
    w._last_t = time.perf_counter()
    w.push(np.zeros((4, 4, 3), np.uint8), 1, 1.4, 0.0, 0.05, False)
    assert w.push_n == 0


def test_predict_advances_x_without_new_identity():
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    x0 = tr.x_last
    r = tr.predict(0.1, v_lat=0.0)
    assert r["state"] == "COAST"
    assert r["x"] == pytest.approx(x0, abs=1e-9)
    assert r["n_locked"] == 0
    assert r["pred"] is True
