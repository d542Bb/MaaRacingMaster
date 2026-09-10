#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detector 契约测试（P4c 换引擎后形态）。

- DetectResult 解包兼容（D7）不变；
- R4 模板缓存失效语义已收敛进 core.template_match.load_template（mtime 指纹
  热修 + 缺失文件后补自动重读）——测试随语义搬家，断言不弱化；
- banner_result 阈值优先级回归（2026-09-06 be18268 缩进 bug 的历史护栏）：
  per-template（win 0.60）> ROI 通用 > 全局 match_threshold；plan 缺失 → None
  （P4c 起无常量兜底，M4/E1 语义）。
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from maaracing_assistant.core import template_match as tm
from maaracing_assistant.plugins.treasure.detector import DetectResult, TreasureStageDetector


def test_detect_result_keeps_legacy_unpacking():
    result = DetectResult(
        stage="第2回合出价",
        round_no=2,
        scores={"round_big_banner": 0.91},
        hit_anchor="round_big_banner",
        active_used=("round_big_banner",),
        hit_template="round2_banner.png",
        threshold=0.75,
    )
    stage, round_no = result
    assert (stage, round_no) == ("第2回合出价", 2)
    assert result.scores["round_big_banner"] == 0.91
    assert result.hit_anchor == "round_big_banner"


# ---- R4：引擎模板缓存的热修失效语义（自 detector._load_gray 迁入）----

def test_engine_template_cache_reloads_after_mtime_or_size_change(tmp_path: Path):
    tm._cache.clear()
    path = tmp_path / "sample.png"
    first = np.full((8, 8, 3), 30, dtype=np.uint8)
    cv2.imwrite(str(path), first)
    loaded_first = tm.load_template("sample", [tmp_path])
    assert loaded_first is not None
    assert abs(int(loaded_first.mean()) - 30) <= 1

    # Windows 文件系统的 mtime 分辨率可能较粗，主动等待并改变 size，确保指纹变化。
    time.sleep(0.01)
    second = np.full((11, 8, 3), 200, dtype=np.uint8)
    cv2.imwrite(str(path), second)
    loaded_second = tm.load_template("sample", [tmp_path])
    assert loaded_second is not None
    assert loaded_second.shape == (11, 8, 3)
    assert abs(int(loaded_second.mean()) - 200) <= 1
    tm._cache.clear()


def test_engine_template_cache_retries_missing_file_when_it_appears(tmp_path: Path):
    tm._cache.clear()
    assert tm.load_template("later", [tmp_path]) is None
    image = np.full((8, 8, 3), 77, dtype=np.uint8)
    cv2.imwrite(str(tmp_path / "later.png"), image)
    loaded = tm.load_template("later", [tmp_path])
    assert loaded is not None
    assert int(loaded.mean()) == 77
    tm._cache.clear()


# ---- banner_result 阈值解析回归（2026-09-06 be18268 缩进 bug 修复）----
# 修复前：plan_spec/per_tpl_th/threshold 解析块被误缩进不可达 → 必抛
# UnboundLocalError。本组测试验证修复后 threshold 的**优先级**而非仅"不抛异常"：
#   per-template（win 0.60 / plan template_thresholds）> ROI 通用 > 全局。
# P4c：匹配走引擎、灰度/彩色按锚点声明；mock 接缝从 _match_local 移到
# detector._match_score（返回 (box, score)）。

class _FakePlanSpec:
    def __init__(self, threshold, template_thresholds, tpl_names, colorspace="rgb"):
        self.threshold = threshold
        self.arbitration = {"template_thresholds": template_thresholds}
        self.rect = (0.0, 0.0, 1.0, 1.0)
        self.templates = list(tpl_names)
        self.colorspace = colorspace


def _make_banner_detector(tpl_names, threshold=None, template_thresholds=None,
                          match_threshold=0.75, colorspace="rgb"):
    det = TreasureStageDetector.__new__(TreasureStageDetector)
    spec = _FakePlanSpec(threshold, template_thresholds or {}, tpl_names, colorspace)
    det.plan = type("P", (), {"spec": {"result_banner": spec}})()
    det.match_threshold = match_threshold
    det.match_scales = (1.0,)
    det._gray_view = {}
    return det


def _banner_score(det, score, monkeypatch):
    """banner_result 的每模板匹配分固定为 score；返回 (返回值, 是否抛异常)。"""
    frame = np.zeros((100, 160, 3), dtype=np.uint8)
    monkeypatch.setattr(det, "_match_score",
                        lambda *a, **k: ((10, 10, 30, 30), score))
    raised = None
    try:
        result = det.banner_result(frame)
    except Exception as e:  # noqa: BLE001
        raised = e
    return result, raised


def test_banner_result_win_uses_per_template_threshold(monkeypatch):
    """win 模板：plan template_thresholds 中 win=0.60 → 0.65 命中、0.55 不命中。"""
    det = _make_banner_detector(
        ("result_auction_win_banner.png",), threshold=0.90,
        template_thresholds={"result_auction_win_banner.png": 0.60})

    hit, raised = _banner_score(det, 0.65, monkeypatch)
    assert raised is None
    assert hit == "win"
    miss, raised = _banner_score(det, 0.55, monkeypatch)
    assert raised is None
    assert miss is None  # 0.55 < 0.60 → 不命中（若误用 ROI 阈值 0.90 则 0.65 也应 miss）


def test_banner_result_per_template_threshold_by_stem_key(monkeypatch):
    """仲裁表键兼容裸名形态（历史 v3 锚点以无扩展名为键落盘）。"""
    det = _make_banner_detector(
        ("result_auction_win_banner.png",), threshold=0.90,
        template_thresholds={"result_auction_win_banner": 0.60})
    hit, raised = _banner_score(det, 0.65, monkeypatch)
    assert raised is None
    assert hit == "win"


def test_banner_result_roi_threshold_when_no_per_template(monkeypatch):
    """无 per-template 覆盖 → 回落 ROI 通用阈值（plan_spec.threshold）。"""
    det = _make_banner_detector(
        ("result_auction_fail_banner.png",), threshold=0.80,
        template_thresholds={})

    hit, raised = _banner_score(det, 0.85, monkeypatch)
    assert raised is None
    assert hit == "fail"  # 0.85 ≥ 0.80（ROI）
    miss, raised = _banner_score(det, 0.75, monkeypatch)
    assert raised is None
    assert miss is None  # 0.75 < 0.80


def test_banner_result_global_threshold_when_no_roi(monkeypatch):
    """无 per-template 且无 ROI 阈值 → 回落全局 match_threshold。"""
    det = _make_banner_detector(("result_auction_fail_banner.png",),
                                threshold=None, match_threshold=0.75)

    hit, raised = _banner_score(det, 0.80, monkeypatch)
    assert raised is None
    assert hit == "fail"  # 0.80 ≥ 0.75（全局）
    miss, raised = _banner_score(det, 0.70, monkeypatch)
    assert raised is None
    assert miss is None  # 0.70 < 0.75


def test_banner_result_plan_missing_returns_none():
    """P4c/M4 语义：plan 缺失（真源不可用）→ 直接 None，无任何常量兜底路径。"""
    det = TreasureStageDetector.__new__(TreasureStageDetector)
    det.plan = None
    frame = np.zeros((100, 160, 3), dtype=np.uint8)
    assert det.banner_result(frame) is None


def test_detect_plan_missing_returns_empty_result():
    det = TreasureStageDetector.__new__(TreasureStageDetector)
    det.plan = None
    frame = np.zeros((100, 160, 3), dtype=np.uint8)
    res = det.detect(frame)
    assert res.stage is None and res.round_no is None
    assert res.scores == {} and res.hit_anchor is None
    assert res.active_used == ()
