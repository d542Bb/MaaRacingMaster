# -*- coding: utf-8 -*-
"""HUD 双方比分读取与对手末分锚选。

覆盖 2026-11-05 实测的两类假读：
- 双方 score 都 trusted、但串位且没有速度行（第二局段 1）；
- 速度行存在、我方分虚高、对手分回落（第一局段 1）。
"""

from __future__ import annotations

from maaracing_master.plugins.speedrush import dashboard_store
from maaracing_master.plugins.speedrush.hud import (
    SIDE_EGO, SIDE_OPPONENT, read_hud_scores)


def _cell(value, side, trusted=True):
    return {"value": value, "trusted": trusted, "side": side}


def _fields(ego_slot="a", ego=100, opp=90, ego_rate=42):
    other = "b" if ego_slot == "a" else "a"
    return {
        f"score_{ego_slot}": _cell(ego, SIDE_EGO),
        f"rate_{ego_slot}": _cell(ego_rate, SIDE_EGO),
        f"score_{other}": _cell(opp, SIDE_OPPONENT),
        f"rate_{other}": _cell(None, SIDE_OPPONENT, trusted=False),
    }


def test_rate_slot_a_is_ego():
    scores = read_hud_scores(_fields("a", 100, 90))
    assert (scores.ego_score, scores.ego_rate, scores.opponent_score) == (100, 42, 90)


def test_rate_slot_b_is_ego_after_swap():
    scores = read_hud_scores(_fields("b", 120, 80))
    assert (scores.ego_score, scores.ego_rate, scores.opponent_score) == (120, 42, 80)


def test_no_rate_abstains():
    """第二局段 1 的串位假读：双方 score 都 trusted，但没有速度行。"""
    fields = {
        "score_a": _cell(283, SIDE_EGO),
        "score_b": _cell(22840, SIDE_OPPONENT),
        "rate_a": _cell(None, SIDE_EGO, trusted=False),
        "rate_b": _cell(None, SIDE_OPPONENT, trusted=False),
    }
    scores = read_hud_scores(fields)
    assert (scores.ego_score, scores.ego_rate, scores.opponent_score) == (None, None, None)


def test_untrusted_opponent_is_ignored():
    fields = _fields()
    fields["score_b"] = _cell(90, SIDE_OPPONENT, trusted=False)
    scores = read_hud_scores(fields)
    assert scores.ego_score == 100
    assert scores.opponent_score is None


def test_unknown_side_is_ignored():
    fields = _fields()
    fields["score_b"] = _cell(90, "?")
    assert read_hud_scores(fields).opponent_score is None


def test_missing_opponent_cell_is_none():
    fields = _fields()
    del fields["score_b"]
    assert read_hud_scores(fields).opponent_score is None


def test_pick_opponent_rejects_spike_above_final():
    """第一局段 1 的虚高假读：我方 4878 超出末分 2579 的采样余量。"""
    pairs = [(2420, 2846), (2579, 2919), (4878, 888)]
    assert dashboard_store.pick_opponent(pairs, 2579) == 2919


def test_pick_opponent_without_final_or_pairs():
    assert dashboard_store.pick_opponent([], 2579) is None
    assert dashboard_store.pick_opponent([(2579, 2919)], None) is None
