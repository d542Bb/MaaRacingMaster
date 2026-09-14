# -*- coding: utf-8 -*-
"""pass 二级确认弹窗锚点契约（bid_pass_confirm_btn）。

背景（真机 2026-09-14）：策略 pass=输入 0 → 点「确认出价」→ 游戏弹「是否确认本轮放弃
出价？」；弹窗无表示时压暗面板使 smart_bid 掉到阈值缝（0.736，决策路 0.72 / 告警路 0.75）
仍被判「面板开」→ 相位卡 bidding 空转，直至人工停止。修复=弹窗红「确认」钮表示成惰性
spec 锚点，出价子机 bidding 判定前先处置。本文件锁三件事：
  1) 锚点在 spec 且素材齐备（rect/threshold/模板文件）；
  2) 惰性：不进 transitions / stages.active / global_anchors（detector 零扫描）；
  3) 真帧契约：弹窗裁剪帧上按 spec 参数必须命中「确认」钮，非弹窗区不得命中。
重依赖（cv2/模块装配）缺失时整文件 SKIP，与 runtime_golden 同一约定。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
POLICY_PATH = _REPO / "maaracing_master" / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"
IMAGE_DIR = _REPO / "maaracing_master" / "plugins" / "treasure" / "resources" / "image"
FIXTURE = _REPO / "tools" / "experiments" / "bid-pass-dialog" / "dialog_crop.jpg"

try:
    import cv2
    import numpy as np
    from maaracing_master.core.template_match import match_template_cs
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要 cv2/template_match 运行时依赖：{_ERR}")

# dialog_crop.jpg = 全帧 (640,180)-(1280,520) 裁剪（1280×720 基准），无玩家 ID 水印区。
_FULL_W, _FULL_H = 1280, 720
_CROP_X, _CROP_Y, _CROP_W, _CROP_H = 640, 180, 640, 340


def _spec_anchor():
    doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    return doc["perception"]["spec"]["bid_pass_confirm_btn"]


def _to_crop_rect(rect):
    """全帧归一化 rect → 裁剪帧归一化 rect。"""
    x1 = (rect[0] * _FULL_W - _CROP_X) / _CROP_W
    y1 = (rect[1] * _FULL_H - _CROP_Y) / _CROP_H
    x2 = (rect[2] * _FULL_W - _CROP_X) / _CROP_W
    y2 = (rect[3] * _FULL_H - _CROP_Y) / _CROP_H
    return (x1, y1, x2, y2)


def test_pass_confirm_anchor_configured_and_inert():
    doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    perception = doc["perception"]
    a = perception["spec"]["bid_pass_confirm_btn"]
    assert a["kind"] == "template" and a["templates"]
    assert (IMAGE_DIR / a["templates"][0]).exists(), "弹窗确认钮模板文件缺失"
    rect = a["rect"]
    assert rect[2] > rect[0] and rect[3] > rect[1]
    assert 0.5 <= a.get("threshold", 0) <= 1.0
    # 惰性：不得进检测面（进了=每帧 detector 扫描，且被 dwell 清单判成阶段页面）
    transition_ons = {tr.get("on") for tr in perception.get("transitions") or []}
    active_names = {n for d in perception["stages"]["definitions"].values()
                    for n in (d.get("active") or [])}
    global_names = set(perception["stages"].get("global_anchors") or [])
    assert "bid_pass_confirm_btn" not in (transition_ons | active_names | global_names)


def test_pass_confirm_matches_dialog_frame():
    if not FIXTURE.exists():
        pytest.skip("弹窗真帧裁剪 fixtures 未入库")
    a = _spec_anchor()
    tpl = cv2.imread(str(IMAGE_DIR / a["templates"][0]))
    assert tpl is not None
    tpl = cv2.cvtColor(tpl, cv2.COLOR_BGR2RGB)
    frame = cv2.cvtColor(cv2.imread(str(FIXTURE)), cv2.COLOR_BGR2RGB)
    H, W = frame.shape[:2]
    x1n, y1n, x2n, y2n = _to_crop_rect(a["rect"])
    px = (int(max(0.0, x1n) * W), int(max(0.0, y1n) * H),
          max(4, int((min(1.0, x2n) - max(0.0, x1n)) * W)),
          max(4, int((min(1.0, y2n) - max(0.0, y1n)) * H)))
    box, score = match_template_cs(frame, tpl, colorspace=a.get("colorspace", "rgb"),
                                   threshold=float(a["threshold"]), scales=(1.0,), roi=px)
    assert box is not None, f"弹窗帧上「确认」钮未命中（最高分 {score:.3f}）"
    # 命中中心≈全帧 (0.865,0.656)（量测：红块 (996,443)-(1217,501)）
    gx = (_CROP_X + (box[0] + box[2]) / 2) / _FULL_W
    gy = (_CROP_Y + (box[1] + box[3]) / 2) / _FULL_H
    assert abs(gx - 0.865) < 0.02 and abs(gy - 0.656) < 0.02, f"命中中心漂移 {(gx, gy)}"


def test_pass_confirm_no_hit_on_claim_popup_frame():
    """聚合奖励弹窗（红蛋/红礼物区同屏）按同一 ROI 匹配不得命中——防串环境。"""
    other = _REPO / "tools" / "experiments" / "egg-claim-coin-read" / "claim_popup_0609.png"
    if not other.exists():
        pytest.skip("奖励弹窗真帧 fixtures 未入库")
    a = _spec_anchor()
    tpl = cv2.cvtColor(cv2.imread(str(IMAGE_DIR / a["templates"][0])), cv2.COLOR_BGR2RGB)
    frame = cv2.cvtColor(cv2.imread(str(other)), cv2.COLOR_BGR2RGB)
    H, W = frame.shape[:2]
    r = a["rect"]
    px = (int(r[0] * W), int(r[1] * H),
          max(4, int((r[2] - r[0]) * W)), max(4, int((r[3] - r[1]) * H)))
    box, score = match_template_cs(frame, tpl, colorspace=a.get("colorspace", "rgb"),
                                   threshold=float(a["threshold"]), scales=(1.0,), roi=px)
    assert box is None, f"奖励弹窗上误命中「放弃确认」钮（score={score:.3f}）"
