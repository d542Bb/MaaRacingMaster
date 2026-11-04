# -*- coding: utf-8 -*-
"""宏录制/回放（``plugins/speedrush/macro.py``）的回归锁。

分四层，全部不碰真手柄/真游戏：
- 合并规则：键盘覆盖轴/扳机、A+D 回中、W/S 是两个独立扳机；
- 文件层：读写校验、越界/时间倒退/缺文件拒绝、宏组枚举与最新组解析；
- 录制器：短线程写 p1.jsonl + meta（g0 由 HUD 外推，meta 记槽位）；
- 回放器：等待对齐、播完松手、漂移重锚、非法输入拒绝。
"""
from __future__ import annotations

import contextlib
import json
import time

import pytest

from maaracing_master.core import xinput
from maaracing_master.plugins.speedrush import macro

try:
    import maaracing_master.plugins.speedrush.module as smod
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 轻依赖环境整段跳过接线层
    _OK, _ERR = False, str(exc)


def _pad(lx=0, ly=0, rx=0, ry=0, lt=0, rt=0, btn=0) -> xinput.PadState:
    return xinput.PadState(index=0, packet=1, buttons=btn,
                           left_trigger=lt, right_trigger=rt,
                           left_stick=(lx, ly), right_stick=(rx, ry))


# ---------- 合并规则 ----------

def test_merge_without_pad_or_keys_is_neutral():
    assert macro.merge_sample(None, {}) == {
        "lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "btn": 0}


def test_merge_keyboard_steer_overrides_pad():
    pad = _pad(lx=1000)
    assert macro.merge_sample(pad, {"a": True})["lx"] == -xinput.STICK_MAX
    assert macro.merge_sample(pad, {"right": True})["lx"] == xinput.STICK_MAX
    assert macro.merge_sample(pad, {"a": True, "d": True})["lx"] == 0
    assert macro.merge_sample(pad, {})["lx"] == 1000


def test_merge_w_and_s_are_independent_triggers():
    pad = _pad(rt=120, lt=30)
    both = macro.merge_sample(pad, {"w": True, "s": True})
    assert both["rt"] == xinput.TRIGGER_MAX
    assert both["lt"] == xinput.TRIGGER_MAX
    assert macro.merge_sample(pad, {"w": True})["lt"] == 30
    assert macro.merge_sample(pad, {"s": True})["rt"] == 120
    assert macro.merge_sample(pad, {})["rt"] == 120


# ---------- 文件层 ----------

def _write_group(root, name, rows, *, phase=1, meta=None):
    d = root / name
    d.mkdir(parents=True)
    (d / f"p{phase}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def _row(t_ms, **over):
    base = {"t_ms": t_ms, "lx": 0, "ly": 0, "rx": 0, "ry": 0,
            "lt": 0, "rt": 0, "btn": 0}
    base.update(over)
    return base


def test_load_phase_roundtrip_and_g0(tmp_path):
    d = _write_group(tmp_path, "20261103_120000", [_row(0, rt=255), _row(16.6, lx=-100)],
                     meta={"phases": {"1": {"g0": 34.5}}})
    samples, g0 = macro.load_phase(d, 1)
    assert [s["rt"] for s in samples] == [255, 0]
    assert g0 == 34.5


def test_load_phase_rejects_bad_files(tmp_path):
    with pytest.raises(macro.MacroError):
        macro.load_phase(tmp_path, 1)  # 缺文件

    d = _write_group(tmp_path, "a", [{"t_ms": 0}])      # 缺字段
    d2 = _write_group(tmp_path, "b", [_row(0), _row(-1)])
    d3 = _write_group(tmp_path, "c", [_row(0, lx=99999)])
    d4 = _write_group(tmp_path, "d", [_row(5), _row(1)])
    for bad in (d, d2, d3, d4):
        with pytest.raises(macro.MacroError):
            macro.load_phase(bad, 1)


def test_group_listing_and_latest(tmp_path):
    _write_group(tmp_path, "20261103_120000", [_row(0)],
                 meta={"phases": {"1": {"duration_ms": 32000}}})
    _write_group(tmp_path, "20261104_090000", [_row(0)])
    groups = macro.list_groups(tmp_path)
    assert [g["value"] for g in groups] == ["20261104_090000", "20261103_120000"]
    assert "p1 32s" in groups[1]["label"]
    assert macro.resolve_group("", tmp_path).name == "20261104_090000"
    assert macro.resolve_group("20261103_120000", tmp_path).name == "20261103_120000"
    with pytest.raises(macro.MacroError):
        macro.resolve_group("../evil", tmp_path)
    with pytest.raises(macro.MacroError):
        macro.resolve_group("missing", tmp_path)


# ---------- 录制器 ----------

def test_recorder_writes_rows_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(macro.xinput, "read_state",
                        lambda i: _pad(lx=-9000, rt=120) if i == 0 else None)
    rec = macro.MacroRecorder(tmp_path / "g", phase=1, poll_hz=100,
                              foreground=lambda: True,
                              keys_reader=lambda: {"w": True})
    rec.start()
    time.sleep(0.06)
    rec.note_clock(30, time.perf_counter_ns())
    time.sleep(0.06)
    rec.stop()

    rows = [json.loads(line) for line in
            (tmp_path / "g" / "p1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows
    assert all(r["rt"] == xinput.TRIGGER_MAX for r in rows), "W 应覆盖手柄扳机"
    assert any(r["lx"] == -9000 for r in rows)
    meta = json.loads((tmp_path / "g" / "meta.json").read_text(encoding="utf-8"))
    assert meta["schema"] == macro.SCHEMA_VERSION
    assert meta["pad_slot"] == 0
    assert meta["phases"]["1"]["g0"] > 30
    assert "C:\\" not in json.dumps(meta) and "/Users/" not in json.dumps(meta)


# ---------- 回放器 ----------

class _StubPad:
    def __init__(self):
        self.calls = []

    def left_joystick(self, x_value=0, y_value=0):
        self.calls.append(("joy", x_value, y_value))

    def left_trigger(self, value=0):
        self.calls.append(("lt", value))

    def right_trigger(self, value=0):
        self.calls.append(("rt", value))

    def update(self):
        self.calls.append(("update",))


def _samples():
    return [
        {"t_ms": 0.0, "lx": -100, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 255, "btn": 0},
        {"t_ms": 20.0, "lx": 100, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "btn": 0},
    ]


def test_player_waits_for_alignment_then_plays_and_releases():
    pad = _StubPad()
    clock = {"v": (31.0, time.perf_counter_ns())}
    player = macro.MacroPlayer(_samples(), 30.0,
                               pad_cm=lambda: contextlib.nullcontext(pad),
                               phase=1, clock=lambda: clock["v"])
    player.start()
    time.sleep(0.05)
    assert player.stats["state"] in ("waiting", "playing")
    clock["v"] = (30.0, time.perf_counter_ns())
    deadline = time.time() + 2.0
    while time.time() < deadline and player.stats["state"] != "finished":
        time.sleep(0.01)
    player.stop()
    assert player.stats["state"] == "finished"
    assert player.stats["sent"] == 2
    assert ("joy", -100, 0) in pad.calls
    assert ("rt", 255) in pad.calls
    assert ("joy", 100, 0) in pad.calls
    assert ("update",) in pad.calls


def test_player_drift_reanchors_only_beyond_threshold():
    player = macro.MacroPlayer(_samples(), 30.0,
                               pad_cm=lambda: contextlib.nullcontext(_StubPad()),
                               phase=1)
    player._t0_ns = 1_000_000_000
    player._correct_drift(29.9, 1_000_000_000)   # 偏差 0.1s：不动
    assert player._t0_ns == 1_000_000_000
    player._correct_drift(33.0, 1_500_000_000)   # 期望 29.5 → 偏差 +3.5s
    assert player._t0_ns == 1_000_000_000 + int(3.5e9)


def test_player_without_g0_starts_immediately():
    player = macro.MacroPlayer(_samples(), None,
                               pad_cm=lambda: contextlib.nullcontext(_StubPad()),
                               phase=1)
    now = time.perf_counter_ns()
    assert player._align((None, 0), now, time.monotonic() + 5.0) is True
    assert player._t0_ns == now


# ---------- 插件接线 ----------

pytestmark_mod = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


@pytestmark_mod
def test_drive_mode_config_roundtrip_and_legacy_migration():
    m = smod.SpeedRushModule(None)
    out = m.set_module_config({"drive_mode": "macro"})
    assert out["drive_mode"] == "macro" and m._resolve_drive_mode() == "macro"
    out = m.set_module_config({"drive_mode": "straight"})
    assert out["drive_mode"] == "straight" and m._resolve_drive_mode() == "straight"
    out = m.set_module_config({"drive_mode": "bogus"})
    assert out["drive_mode"] == "straight"   # 非法值忽略，保留上一档
    # 实例直收旧布尔键也折算（宏优先，与旧互斥收口同序）
    out = m.set_module_config({"macro_mode": True})
    assert out["drive_mode"] == "macro" and "macro_mode" not in out
    out = m.set_module_config({"macro_group": "../evil"})
    assert out["macro_group"] == ""
    out = m.set_module_config({"macro_group": "20261103_120000"})
    assert out["macro_group"] == "20261103_120000"


@pytestmark_mod
def test_legacy_boolean_keys_normalize_to_drive_mode():
    norm = smod.SpeedRushModule.normalize_config
    assert norm({"straight_mode": True, "macro_mode": True}) == {"drive_mode": "macro"}
    assert norm({"straight_mode": True}) == {"drive_mode": "straight", "record_mode": False}
    assert norm({"macro_mode": True}) == {"drive_mode": "macro"}
    assert norm({"straight_mode": False, "macro_mode": False}) == {"drive_mode": "depth"}
    # 新键已存在时不被旧键覆盖
    assert norm({"drive_mode": "straight", "macro_mode": True}) == {
        "drive_mode": "straight", "record_mode": False}
    # 直行档不变量：残留的录制开关一律关掉（界面在直行档不显示该开关）
    assert norm({"drive_mode": "straight", "record_mode": True}) == {
        "drive_mode": "straight", "record_mode": False}
    assert norm({"drive_mode": "depth", "record_mode": True}) == {
        "drive_mode": "depth", "record_mode": True}


@pytestmark_mod
def test_macro_mode_skips_model_preload_and_choices_prepends_latest(monkeypatch):
    m = smod.SpeedRushModule(None)
    m.set_module_config({"drive_mode": "macro", "control_mode": True})
    assert m._preload_wanted() is False
    monkeypatch.setattr(smod, "list_groups",
                        lambda: [{"value": "20261103_120000", "label": "x"}])
    choices = smod.SpeedRushModule.config_choices()["macro_group"]
    assert choices[0]["value"] == ""
    assert choices[1]["value"] == "20261103_120000"


@pytestmark_mod
def test_macro_group_dir_is_shared_by_both_phases(tmp_path, monkeypatch):
    """p1 建好的组目录不能被 p2 判成撞名而分家。"""
    monkeypatch.setattr(smod, "macro_root", lambda: tmp_path)
    smod._macro_groups.clear()
    d1 = smod._macro_group_dir("20261103_120000")
    d1.mkdir(parents=True)
    (d1 / "p1.jsonl").write_text("x", encoding="utf-8")
    d2 = smod._macro_group_dir("20261103_120000")
    assert d1 == d2
    assert d2.name == "20261103_120000"


@pytestmark_mod
def test_hud_clock_row_only_trusted():
    class _Obs:
        def __init__(self, row):
            self._row = row

        def latest(self):
            return self._row

    trusted = _Obs({"ts_ns": 123, "fields": {"timer": {"value": 34, "trusted": True}}})
    untrusted = _Obs({"ts_ns": 123, "fields": {"timer": {"value": 34, "trusted": False}}})
    assert smod.SpeedRushModule._hud_clock_row(trusted) == (34.0, 123)
    assert smod.SpeedRushModule._hud_clock_row(untrusted) is None
    assert smod.SpeedRushModule._hud_clock_row(None) is None
