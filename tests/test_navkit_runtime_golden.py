# -*- coding: utf-8 -*-
"""鉴宝运行时读取器 · 金标回归（真源数值防漂移锁）。

金标 = 2026-09-08 M4/E1 收口时固化的历代一致值；P4b 数据源切换（policy.json
数据面）沿用同一套金标值——切换的逐字段等价由 tools/experiments/v4-p4b-source/
双跑对拍证明，本文件负责此后锁值不回退漂移。
V-1 守卫不变：monkeypatch `open` 断言运行时绝不触碰已退役的 treasure_rois.json。

本文件导入 module（经 core.capabilities 拉 maa），属完整运行时测试；CI 只装 numpy+opencv-headless
时由 importorskip 整文件跳过，dev 环境（.venv 全依赖）全跑。
"""
from __future__ import annotations

import pytest

# 完整运行时依赖（module 经 core.capabilities 拉 maa/vgamepad 等，ocr/eggs 需 cv2）。
# conftest 约定 CI 尽量避开这些重依赖：任一缺失则整文件 SKIP（不误红），dev（.venv 全依赖）全跑。
try:
    from maaracing_master.plugins.treasure import POLICY_PATH  # noqa: E402
    from maaracing_master.plugins.treasure import module as tm  # noqa: E402
    from maaracing_master.plugins.treasure import ocr as toc  # noqa: E402
    from maaracing_master.plugins.treasure.detector import TreasureStageDetector  # noqa: E402
    from maaracing_master.plugins.treasure.eggs import EggRewardRecognizer  # noqa: E402
    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"运行时金标测试需要完整依赖（maa/cv2/…）：{_RUNTIME_ERR}"
)

_PROJ = POLICY_PATH.parent.parent if _RUNTIME_OK else None  # resources 目录
TOL = 1e-4  # 金标为历代一致值（6dp 记录），容差仅吸收浮点噪声，足以抓真实漂移


def _close(a, b) -> bool:
    return all(abs(float(x) - float(y)) <= TOL for x, y in zip(a, b))


# ---- 金标（2026-09-08 M4/E1 收口时固化的历代一致值）----
GOLDEN_APPRAISERS = {
    "appraiser_p1_caroline": {"prio": 1, "rect": [0.094617, 0.266892, 0.903443, 0.729013], "threshold": 0.8},
    "appraiser_p2_shotaro": {"prio": 2, "rect": [0.086333, 0.265420, 0.905099, 0.733432], "threshold": 0.8},
}
GOLDEN_SELECTED_CHECK_RECT = [0.03, 0.26, 0.97, 0.38]
GOLDEN_SMART_BID_RECT = [0.708148, 0.762634, 0.807407, 0.847654]
GOLDEN_ROUND_LABEL_RECT = [0.399259, 0.152675, 0.469630, 0.195802]
GOLDEN_EGG = {"rect": [0.300844, 0.346293, 0.701004, 0.560942], "threshold": 0.72,
              "counts": [0.03, 0.0, 0.04, 0.03]}
GOLDEN_OCR = {
    "bid_result_amount_box": [0.339259, 0.650535, 0.537778, 0.726502],
    "bid_player1": [0.149562, 0.278955, 0.274504, 0.311307],
    "bid_player2": [0.147658, 0.440987, 0.275330, 0.478708],
    "bid_player3": [0.150988, 0.606819, 0.275388, 0.642100],
    "bid_player4": [0.150162, 0.772934, 0.273762, 0.809901],
    "player_name1": [0.111283, 0.237678, 0.224111, 0.272626],
    "player_name2": [0.108964, 0.403916, 0.230294, 0.437489],
    "player_name3": [0.109737, 0.568779, 0.231066, 0.603727],
    "player_name4": [0.110510, 0.733642, 0.234158, 0.765842],
    "settle_final_price": [0.684444, 0.236708, 0.896296, 0.309053],
    "settle_total_price": [0.682963, 0.363128, 0.895888, 0.437139],
    "settle_profit": [0.68, 0.489547, 0.899259, 0.559259],
    "settle_my_income": [0.685926, 0.668642, 0.908148, 0.735720],
    "my_balance": [0.842458, 0.007525, 0.980741, 0.077284],
    "round_label_area": [0.399259, 0.152675, 0.469630, 0.195802],
    "bid_main_btn_label": [0.431388, 0.805286, 0.523954, 0.857562],
    "session_daily_count": [0.079086, 0.778495, 0.181993, 0.813510],
    "daily_high_score": [0.318998, 0.506637, 0.692456, 0.612335],
}


def test_appraiser_templates_match_golden():
    out = tm._load_appraiser_templates(_PROJ)
    got = {key: (prio, rect, th) for prio, key, _tpl, rect, th, _cs in out}
    assert set(got) == set(GOLDEN_APPRAISERS), f"鉴宝师键漂移: {set(got)^set(GOLDEN_APPRAISERS)}"
    for key, g in GOLDEN_APPRAISERS.items():
        prio, rect, th = got[key]
        assert prio == g["prio"], f"{key} prio 漂移 {prio}"
        assert _close(rect, g["rect"]), f"{key} rect 漂移 {rect}"
        assert abs(th - g["threshold"]) <= TOL, f"{key} threshold 漂移 {th}"


def test_selected_check_match_golden():
    r = tm._load_selected_check(_PROJ)
    assert r is not None
    assert _close(r[1], GOLDEN_SELECTED_CHECK_RECT), f"selected_check rect 漂移 {r[1]}"


def test_smart_bid_btn_match_golden():
    r = tm._load_smart_bid_btn(_PROJ)
    assert r is not None
    assert _close(r[1], GOLDEN_SMART_BID_RECT), f"smart_bid_btn rect 漂移 {r[1]}"


def test_ocr_regions_match_golden():
    regions = toc.TreasureOcr(_PROJ)._regions
    for key, rect in GOLDEN_OCR.items():
        assert key in regions, f"丢失 ocr 识别区: {key}"
        assert _close(regions[key], rect), f"ocr[{key}] rect 漂移 {regions[key]}"


def test_round_label_rect_match_golden():
    det = TreasureStageDetector(_PROJ)
    assert det.plan is not None, "policy.json 数据面应可装配 DetectionPlan"
    r = det._round_label_rect()
    assert r is not None and _close(r, GOLDEN_ROUND_LABEL_RECT), f"round_label rect 漂移 {r}"


def test_egg_entry_match_golden():
    e = EggRewardRecognizer(_PROJ)
    assert e.configured
    assert _close(e._entry[1], GOLDEN_EGG["rect"]), "egg rect 漂移"
    assert abs(float(e._entry[2]) - GOLDEN_EGG["threshold"]) <= TOL, "egg threshold 漂移"
    got_counts = [getattr(e, a) for a in ("_count_dx", "_count_dy", "_count_w", "_count_h")]
    assert _close(got_counts, GOLDEN_EGG["counts"]), f"egg 计数参数漂移 {got_counts}"


def test_result_banner_per_template_threshold_from_plan():
    det = TreasureStageDetector(_PROJ)
    ths = det.plan.spec["result_banner"].arbitration.get("template_thresholds", {})
    assert any(abs(ths.get(k, -1) - 0.6) <= TOL for k in ("result_auction_win_banner", "result_auction_win_banner.png")), (
        f"result_banner win 放宽阈值未由 plan.arbitration 提供: {ths}"
    )


def test_runtime_never_opens_treasure_rois(monkeypatch):
    """V-1 行为级证明：构造 detector + 调全部运行时读取器，任何一次 open(treasure_rois.json) 即失败。"""
    import builtins

    real_open = builtins.open

    def _guard(file, *args, **kwargs):
        if "treasure_rois" in str(file):
            raise AssertionError(f"运行时不得再打开 treasure_rois.json（v2 已退役）：{file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guard)
    TreasureStageDetector(_PROJ)
    tm._load_appraiser_templates(_PROJ)
    tm._load_selected_check(_PROJ)
    tm._load_smart_bid_btn(_PROJ)
    tm._load_session_panel(_PROJ)
    tm._load_action_centers(_PROJ)
    toc.TreasureOcr(_PROJ)
    EggRewardRecognizer(_PROJ)
