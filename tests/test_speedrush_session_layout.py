# -*- coding: utf-8 -*-
"""会话布局的回归锁：人工局与控制局必须是同一套目录、索引与 meta。

这条锁防的正是 2026-10-30 之前的形态——两条落盘路径各写一套：同名
``frames.jsonl`` 的 ``file`` 在人工局是裸文件名、控制局是相对路径，HUD 读数
一个人工局在会话根、一个在 ``hud/``，离线工具必须先看目录名才知道怎么解释。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from maaracing_master.plugins.speedrush import debugview as dv
from maaracing_master.plugins.speedrush.recorder import DriveRecorder
from maaracing_master.plugins.speedrush.session_layout import (
    SCHEMA_VERSION,
    hud_relpath,
    raw_relpath,
)


def _rows(path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line]


def test_path_helpers_are_the_only_naming_rule():
    assert raw_relpath(1) == "raw/0001_raw.jpg"
    assert raw_relpath(12345) == "raw/12345_raw.jpg"   # 超四位不截断、不撞名
    assert hud_relpath(7) == "0007_hud.jpg"


def test_index_keys_identical_between_modes(tmp_path, monkeypatch) -> None:
    """两边的索引行必须同键：离线工具不需要按目录名分支。"""
    rec = DriveRecorder(tmp_path / "manual", pad_poll_hz=50.0)
    rec.start()
    rec.record_frame(np.zeros((16, 24, 3), np.uint8), frame_id=1, ts_ns=10)
    rec.stop()
    manual_row = _rows(tmp_path / "manual" / "frames.jsonl")[0]

    # 渲染替换成空图：本锁只关心落盘协议，不关心 BEV 画法
    monkeypatch.setattr(dv, "render_hud",
                        lambda frame, snap, cal: np.zeros((8, 8, 3), np.uint8))
    writer = dv.DebugPairWriter(tmp_path / "control", interval_s=0.0, cal=None)
    try:
        writer.tick(np.zeros((16, 24, 3), np.uint8), {"fid": 2, "ts_ns": 20})
    finally:
        writer.stop()
    control_row = _rows(tmp_path / "control" / "frames.jsonl")[0]

    assert set(manual_row) == set(control_row) == {
        "seq", "frame_id", "ts_ns", "age_ms", "file", "hud"}
    assert manual_row["file"].startswith("raw/") and control_row["file"].startswith("raw/")


def test_meta_keys_identical_between_modes(tmp_path, monkeypatch) -> None:
    rec = DriveRecorder(tmp_path / "manual", pad_poll_hz=50.0)
    rec.start()
    rec.record_frame(np.zeros((8, 8, 3), np.uint8), frame_id=1, ts_ns=1)
    rec.stop()
    manual = json.loads((tmp_path / "manual" / "meta.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(dv, "render_hud",
                        lambda frame, snap, cal: np.zeros((8, 8, 3), np.uint8))
    writer = dv.DebugPairWriter(tmp_path / "control", interval_s=0.0, cal=None)
    writer.stop()
    control = json.loads((tmp_path / "control" / "meta.json").read_text(encoding="utf-8"))

    assert set(manual) == set(control)
    assert manual["schema"] == control["schema"] == SCHEMA_VERSION
    assert manual["source"] == "manual" and control["source"] == "control"
    # 人工局专属项在控制局显式 None，而不是缺键
    assert manual["pad_samples"] >= 0 and control["pad_samples"] is None
    assert manual["disk_pixel_order"] == control["disk_pixel_order"] == "rgb"


@pytest.mark.parametrize("mode", ["manual", "control"])
def test_hud_pairs_land_next_to_index(tmp_path, monkeypatch, mode) -> None:
    """叠加图落会话根、原帧落 raw/、索引能指到两者——两种模式同一口径。"""
    monkeypatch.setattr(dv, "render_hud",
                        lambda frame, snap, cal: np.zeros((8, 8, 3), np.uint8))
    frame = np.zeros((16, 24, 3), np.uint8)
    if mode == "manual":
        rec = DriveRecorder(tmp_path / "s", pad_poll_hz=50.0,
                            hud_render=lambda f, s: np.zeros((8, 8, 3), np.uint8))
        rec.start()
        rec.record_frame(frame, frame_id=1, ts_ns=1)
        rec.attach_hud_snap(1, {"fid": 1})
        rec.stop()
        sess = tmp_path / "s"
    else:
        writer = dv.DebugPairWriter(tmp_path / "s", interval_s=0.0, cal=None)
        writer.tick(frame, {"fid": 1, "ts_ns": 1})
        writer.stop()
        sess = tmp_path / "s"

    row = _rows(sess / "frames.jsonl")[0]
    assert row["hud"] == "0001_hud.jpg"
    assert (sess / row["file"]).is_file() and (sess / row["hud"]).is_file()


def _fresh_mod(monkeypatch, root):
    """把产物根指到 tmp、清空目录缓存，模拟一次干净的进程内解析。"""
    from maaracing_master.plugins.speedrush import module as smod
    monkeypatch.setattr(smod, "debug_dir", lambda: root)
    monkeypatch.setattr(smod, "_run_dirs", {})
    return smod


def test_run_dir_prefix_is_per_round(tmp_path, monkeypatch) -> None:
    """一局一个前缀：同局两阶段共用、不同局必然不同。

    前缀若取日志会话编号（它标识的是"一次启动"），同一次启动连跑多局会重复，
    后一局整批覆盖前一局的图——2026-10-30 实机踩到，两局的索引还叠成了一份。
    """
    smod = _fresh_mod(monkeypatch, tmp_path)
    a1 = smod._run_dir("20261030_200822", 1)
    a2 = smod._run_dir("20261030_200822", 2)
    b1 = smod._run_dir("20261030_201500", 1)

    assert a1.name == "20261030_200822_p1"
    assert a2.name == "20261030_200822_p2"
    assert b1.name == "20261030_201500_p1"
    # 同局两阶段同父目录（能配对），不同局不共前缀
    assert a1.parent == a2.parent == b1.parent


def test_run_dir_never_reuses_existing_session(tmp_path, monkeypatch) -> None:
    """目录已存在且非空时换名，绝不给既有会话续写。

    复用会让新一局的图覆盖旧图、索引被追加成两局混合——丢数据还留下假指针
    （索引里的行指向的已是新局的画面）。
    """
    smod = _fresh_mod(monkeypatch, tmp_path)
    first = smod._run_dir("20261030_200822", 1)
    (first / "raw").mkdir(parents=True)
    (first / "raw" / "0001_raw.jpg").write_bytes(b"old")

    # 模拟进程重启后同名再建：缓存是进程内的，重启后自然为空
    monkeypatch.setattr(smod, "_run_dirs", {})
    second = smod._run_dir("20261030_200822", 1)

    assert second != first
    assert second.name == "20261030_200822_p1_2"
    assert (first / "raw" / "0001_raw.jpg").read_bytes() == b"old"


def test_run_dir_caches_per_stamp_and_phase(tmp_path, monkeypatch) -> None:
    """同 (前缀, 阶段) 必须返回同一路径。

    同阶段的目录会被控制链与 HUD 两处先后取用；第二次若重新判重，会看到刚建好
    的目录、误判撞名而再加一个后缀，同阶段的产物就分家了。
    """
    smod = _fresh_mod(monkeypatch, tmp_path)
    first = smod._run_dir("20261030_200822", 1)
    (first / "raw").mkdir(parents=True)
    (first / "raw" / "0001_raw.jpg").write_bytes(b"x")

    assert smod._run_dir("20261030_200822", 1) == first
