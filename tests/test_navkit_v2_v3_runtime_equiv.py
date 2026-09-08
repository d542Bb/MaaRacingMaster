# -*- coding: utf-8 -*-
"""M1 · 鉴宝运行时「v3 读出值 == 旧 v2 读出值」字段等价对照单测。

TREASURE_V2_V3_SINGLE_SOURCE_PLAN V-4：`regress_stages` 逐帧回归仅覆盖 stage 检测，
对独立匹配段（appraiser / selected_check / smart_bid_btn / ocr regions / round_label / egg）
无证明力。本文件为 M1 从 v2 切到 v3 优先的 6 个读取点，逐处证明「v3 分支读到的几何/阈值/
优先级/领域参数 == v2 分支读到的值」，作为 N-1 行为等价的主证据。

手法：同一份真实资产（treasure_rois.json / treasure_assets.json）下，
分别以 `NAVKIT_SOURCE=v2` 与默认 v3 调用读取函数，比对配置派生字段。切换前必须清 `lru_cache`
（`v3_assets` / `_perception_tuning` / `_policy_tuning` 均带缓存），否则读到缓存值污染对照。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import maaracing_assistant.plugins.treasure as treasure
from maaracing_assistant.plugins.treasure import CONFIG_DIR, v3_assets
from maaracing_assistant.plugins.treasure import module as treasure_module
from maaracing_assistant.plugins.treasure.detector import TreasureStageDetector
from maaracing_assistant.plugins.treasure.eggs import EggRewardRecognizer
from maaracing_assistant.plugins.treasure.ocr import TreasureOcr

RECT_TOL = 1e-9
_PROJ = CONFIG_DIR.parent


def _rect_close(a, b) -> bool:
    assert a is not None and b is not None, f"一侧 rect 为 None: {a!r} vs {b!r}"
    assert len(a) == 4 and len(b) == 4
    return all(abs(float(x) - float(y)) <= RECT_TOL for x, y in zip(a, b))


@pytest.fixture
def force_source():
    """切换 NAVKIT_SOURCE 并清空所有相关 lru_cache，测后还原。"""
    def _set(mode: str):
        if mode == "v2":
            os.environ["NAVKIT_SOURCE"] = "v2"
        else:
            os.environ.pop("NAVKIT_SOURCE", None)
        v3_assets.cache_clear()
        treasure_module._policy_tuning.cache_clear()
        treasure_module._perception_tuning.cache_clear()
    yield _set
    os.environ.pop("NAVKIT_SOURCE", None)
    v3_assets.cache_clear()
    treasure_module._policy_tuning.cache_clear()
    treasure_module._perception_tuning.cache_clear()


def _appraiser_config(mode: str, force_source):
    """_load_appraiser_templates 返回 [(prio, key, gray, rect, threshold)] → 抽配置字段（不含 gray）。"""
    force_source(mode)
    out = treasure_module._load_appraiser_templates(_PROJ)
    return [(prio, key, rect, threshold) for prio, key, _gray, rect, threshold in out]


def test_appraiser_templates_v2_v3_equal(force_source):
    v2 = _appraiser_config("v2", force_source)
    v3 = _appraiser_config("v3", force_source)
    assert v3, "v3 分支应读出至少一个鉴宝师"
    by_key_v2 = {key: (prio, rect, th) for prio, key, rect, th in v2}
    by_key_v3 = {key: (prio, rect, th) for prio, key, rect, th in v3}
    assert set(by_key_v2) == set(by_key_v3), "v3 与 v2 的鉴宝师 key 集合应一致"
    for key, (prio2, rect2, th2) in by_key_v2.items():
        prio3, rect3, th3 = by_key_v3[key]
        assert prio2 == prio3, f"{key} prio 漂移: {prio2}≠{prio3}"
        assert _rect_close(rect2, rect3), f"{key} rect 漂移: {rect2}≠{rect3}"
        assert abs(float(th2) - float(th3)) <= RECT_TOL, f"{key} threshold 漂移: {th2}≠{th3}"


def test_selected_check_v2_v3_equal(force_source):
    force_source("v2")
    r2 = treasure_module._load_selected_check(_PROJ)
    force_source("v3")
    r3 = treasure_module._load_selected_check(_PROJ)
    assert (r2 is None) == (r3 is None)
    if r2 is not None:
        assert _rect_close(r2[1], r3[1]), f"selected_check rect 漂移: {r2[1]}≠{r3[1]}"


def test_smart_bid_btn_v2_v3_equal(force_source):
    force_source("v2")
    r2 = treasure_module._load_smart_bid_btn(_PROJ)
    force_source("v3")
    r3 = treasure_module._load_smart_bid_btn(_PROJ)
    assert (r2 is None) == (r3 is None)
    if r2 is not None:
        assert _rect_close(r2[1], r3[1]), f"smart_bid_btn rect 漂移: {r2[1]}≠{r3[1]}"


def test_ocr_regions_v2_v3_equal(force_source):
    force_source("v2")
    o2 = TreasureOcr(_PROJ)
    v2_regions = dict(o2._regions)
    force_source("v3")
    o3 = TreasureOcr(_PROJ)
    v3_regions = dict(o3._regions)
    assert v3_regions, "v3 分支应读出 ocr 识别区"
    # v2 每个键都须在 v3 出现且 rect 等价（v3 可为超集，消费方按 key 取用）
    assert set(v2_regions).issubset(set(v3_regions)), (
        f"v3 丢失 v2 ocr 键: {set(v2_regions) - set(v3_regions)}"
    )
    for key, rect2 in v2_regions.items():
        assert _rect_close(rect2, v3_regions[key]), f"ocr[{key}] rect 漂移: {rect2}≠{v3_regions[key]}"


def test_egg_entry_v2_v3_equal(force_source):
    force_source("v2")
    e2 = EggRewardRecognizer(_PROJ)
    force_source("v3")
    e3 = EggRewardRecognizer(_PROJ)
    assert e2.configured == e3.configured
    if e3.configured:
        assert _rect_close(e2._entry[1], e3._entry[1]), "egg rect 漂移"
        assert abs(float(e2._entry[2]) - float(e3._entry[2])) <= RECT_TOL, "egg threshold 漂移"
    for attr in ("_count_dx", "_count_dy", "_count_w", "_count_h"):
        assert abs(float(getattr(e2, attr)) - float(getattr(e3, attr))) <= RECT_TOL, (
            f"egg 计数参数 {attr} 漂移: {getattr(e2, attr)}≠{getattr(e3, attr)}"
        )


def test_round_label_rect_v2_v3_equal(force_source):
    # v2 模式：detector.__init__ 不建 plan，_round_label_rect 走 v2 schema；
    # v3 模式：建 DetectionPlan，走 plan.spec['round_label_area']。两条都应返回同一 rect。
    force_source("v2")
    d2 = TreasureStageDetector(_PROJ)
    assert d2.plan is None, "v2 模式下 detector 不应加载 DetectionPlan"
    r2 = d2._round_label_rect()
    force_source("v3")
    d3 = TreasureStageDetector(_PROJ)
    assert d3.plan is not None, "v3 模式下 detector 应加载 DetectionPlan"
    r3 = d3._round_label_rect()
    assert r2 is not None and r3 is not None
    assert _rect_close(r2, r3), f"round_label_area rect 漂移: {r2}≠{r3}"
