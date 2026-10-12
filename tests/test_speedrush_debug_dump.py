# -*- coding: utf-8 -*-
"""debug_dump 三态闸 + 局结局取数面（2026-10-12 设计裁决收口）。

覆盖：config loader 三态（off/events/full，非法 mode 拦截）、
_ego_hud_readout 本机归属（rate 槽位判据）、worker events 模式
（ring 前情 + 窗口后果）、局汇总行聚合。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import DebugDump, _read_decision
from maaracing_master.plugins.speedrush.depth_geo import AsyncDepthRoadObserver
from maaracing_master.plugins.speedrush.module import SpeedRushModule, _ego_hud_readout


# ---------- config loader ----------

def test_debug_dump_parsed_from_real_policy() -> None:
    cfg = _read_decision(DECISION_FILE)
    assert cfg.debug_dump.mode == "events"
    assert cfg.debug_dump.ring >= 0
    assert cfg.debug_dump.event_window >= 0


def test_debug_dump_invalid_mode_rejected(tmp_path: Path) -> None:
    d = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
    d["debug_dump"] = {"mode": "bogus"}
    p = tmp_path / "decision.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="debug_dump"):
        _read_decision(p)


# ---------- worker events 模式 ----------

_PAYLOAD = {"img.jpg": b"i", "frame.jpg": b"f", "evid.npz": b"e", "mask.npy": None}


def _events_observer(ring: int, window: int):
    obs = AsyncDepthRoadObserver(
        object(), debug_dir=Path("unused"),
        dump=DebugDump(mode="events", ring=ring, event_window=window))
    dumped: list[dict] = []
    obs._dump_stem = dumped.append            # 落盘替身（不写盘）
    obs._encode_debug_payload = lambda *a, **k: dict(_PAYLOAD)  # 渲染替身
    return obs, dumped


def test_events_mode_triggers_ring_flush_and_window() -> None:
    obs, dumped = _events_observer(ring=3, window=2)
    cruise = {"state": "CRUISE", "reason": "hold:no_candidate", "fid": 0}
    for i in range(3):
        obs._write_debug_events(None, None, None, None,
                                {"state": "CRUISE", "reason": "hold:x", "fid": i})
    assert not dumped and len(obs._dump_ring) == 3     # 平时只进 ring
    obs._write_debug_events(None, None, None, None,
                            {"state": "CONSERVE", "reason": "conserving", "fid": 9})
    assert len(dumped) == 4                            # ring 前情 3 + 触发帧 1
    assert obs._event_left == 1                        # 窗口余量 = window-1
    obs._write_debug_events(None, None, None, None,
                            {"state": "CRUISE", "reason": "hold:x", "fid": 10})
    assert len(dumped) == 5 and obs._event_left == 0   # 后果帧直写
    obs._write_debug_events(None, None, None, None, cruise)
    assert len(dumped) == 5 and len(obs._dump_ring) == 1   # 窗口耗尽回 ring


def test_events_mode_dodge_reason_triggers() -> None:
    obs, dumped = _events_observer(ring=2, window=1)
    obs._write_debug_events(None, None, None, None,
                            {"state": "CRUISE", "reason": "hold:x", "fid": 1})
    obs._write_debug_events(None, None, None, None,
                            {"state": "CRUISE", "reason": "select:dodge", "fid": 2})
    assert len(dumped) == 2                            # ring 前情 1 + 触发帧 1
    assert obs._event_left == 0


# ---------- HUD 本机归属 ----------

class _Hud:
    def __init__(self, fields):
        self._f = fields

    def latest(self):
        if self._f is None:
            return None
        return {"fields": self._f}


def _cell(v):
    return {"value": v}


def test_ego_hud_rate_slot_identifies_ego() -> None:
    hud = _Hud({"score_a": _cell(2146), "score_b": _cell(1320), "rate_a": _cell(20)})
    assert _ego_hud_readout(hud) == (2146, 20)


def test_ego_hud_swapped_position_uses_rate_slot() -> None:
    # 比分面板上下换位：本机卡在 b 槽——按 rate 槽位取，不按 a/b 猜
    hud = _Hud({"score_a": _cell(1320), "score_b": _cell(2146), "rate_b": _cell(56)})
    assert _ego_hud_readout(hud) == (2146, 56)


def test_ego_hud_missing_rate_is_honest_none() -> None:
    hud = _Hud({"score_a": _cell(2146), "score_b": _cell(1320)})
    assert _ego_hud_readout(hud) == (None, None)      # rate 缺席不臆造归属
    assert _ego_hud_readout(_Hud(None)) == (None, None)
    assert _ego_hud_readout(None) == (None, None)


# ---------- 局汇总行 ----------

def test_game_summary_row_appended(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {"ts_ns": 0, "state": "CRUISE", "reason": "hold:x",
         "hud_score": None, "hud_rate": None},
        {"ts_ns": 1_000_000_000, "state": "CRUISE", "reason": "select:dodge",
         "hud_score": 100, "hud_rate": 41},
        {"ts_ns": 2_000_000_000, "state": "CRUISE", "reason": "select:dodge",
         "hud_score": 130, "hud_rate": 41},
        {"ts_ns": 3_000_000_000, "state": "CRUISE", "reason": "hold:x",
         "hud_score": 130, "hud_rate": 41},
    ]
    plugin = SpeedRushModule.__new__(SpeedRushModule)   # 方法只用 _tlog（失败路径）
    monkeypatch.setattr(
        "maaracing_master.plugins.speedrush.module._control_trace_root",
        lambda: tmp_path)
    plugin._write_game_summary({"t_start": 0.0}, 1, rows)
    out = json.loads(
        (tmp_path / "game_summary.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert out["score"] == 130 and out["rate"] == 41    # 末值（累积量）
    assert out["dodge_episodes"] == 1                    # 连续触发拍归并
    assert out["conserve_segments"] == 0
    assert out["ticks"] == 4 and out["phase"] == 1
