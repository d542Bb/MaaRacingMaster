# -*- coding: utf-8 -*-
"""speedrush 驾驶录制器的回归锁。

覆盖：帧落盘与索引、手柄样本落盘（注入假手柄，不依赖真实硬件）、meta 计数、
队列满时的丢帧计数（反压纪律）、stop 幂等、程序叠加图与索引的配对。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from maaracing_master.core import xinput
from maaracing_master.plugins.speedrush.recorder import (
    DriveRecorder,
    make_session_dir,
)
from maaracing_master.plugins.speedrush.session_layout import SCHEMA_VERSION


def _frame(w: int = 64, h: int = 48) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _fake_pad(index: int):
    """注入用假手柄：槽 0 有连、其余无。"""
    if index != 0:
        return None
    return xinput.PadState(
        index=0, packet=7, buttons=xinput.BUTTON_A,
        left_trigger=210, right_trigger=0,
        left_stick=(-12000, 300), right_stick=(0, 0))


@pytest.fixture(autouse=True)
def _no_real_pad(monkeypatch):
    """默认隔离真实 XInput：除非用例自行覆盖，一律视为无手柄，避免测试依赖硬件。"""
    monkeypatch.setattr(xinput, "read_state", lambda index: None)


def test_records_frames_and_writes_index(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s1", pad_poll_hz=50.0)
    rec.start()
    for i in range(5):
        rec.record_frame(_frame(), frame_id=100 + i, ts_ns=1_000_000 + i * 33_000_000)
    rec.stop()

    lines = _read_jsonl(tmp_path / "s1" / "frames.jsonl")
    assert len(lines) == 5
    for i, row in enumerate(lines):
        assert row["seq"] == i + 1
        # frame_id 与 ts_ns 必须原样落盘：跨源对齐就靠它们，丢了就退化成"按序号猜"
        assert row["frame_id"] == 100 + i
        assert row["ts_ns"] == 1_000_000 + i * 33_000_000
        # 索引 file 是相对会话根的路径（与程序控制局同一口径），不再隐含 frames/
        assert row["file"] == f"raw/{i + 1:04d}_raw.jpg"
        assert (tmp_path / "s1" / row["file"]).is_file()
        # 没注入渲染器 = 不产叠加图：hud 显式 null，不是键缺席
        assert row["hud"] is None


def test_records_pad_samples(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(xinput, "read_state", _fake_pad)
    rec = DriveRecorder(tmp_path / "s2", pad_poll_hz=200.0)
    rec.start()
    # 给采样线程留出产样本的时间（200Hz × 0.15s ≈ 30 条，取宽限判据）
    import time
    deadline = time.monotonic() + 2.0
    while rec.stats["pad_samples"] < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    rec.stop()

    rows = _read_jsonl(tmp_path / "s2" / "pads.jsonl")
    assert len(rows) >= 3, "手柄采样线程未产出样本"
    for row in rows:
        assert row["lx"] == -12000 and row["ly"] == 300
        assert row["lt"] == 210 and row["rt"] == 0
        assert row["btn"] == xinput.BUTTON_A
        assert row["ts_ns"] > 0


def test_meta_counts_and_schema(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s3", jpeg_quality=80, pad_poll_hz=50.0)
    rec.start()
    rec.record_frame(_frame(w=32, h=16), frame_id=1, ts_ns=123)
    rec.stop(reason="drive_phase_end")

    meta = json.loads((tmp_path / "s3" / "meta.json").read_text(encoding="utf-8"))
    assert meta["schema"] == SCHEMA_VERSION
    # 帧图字节序必须进 meta：把"这份数据按哪种语义落盘"钉死，离线工具据此选读法
    # 而不必猜——猜错就是整体 R/B 互换，且在灰白画面上看不出来。
    assert meta["disk_pixel_order"] == "rgb"
    assert meta["frames_written"] == 1
    assert meta["frames_dropped"] == 0
    assert meta["source"] == "manual"
    assert meta["jpeg_quality"] == 80
    # 没注入渲染器：叠加图项显式空，"没采集到"不等于"字段恰好没有"
    assert meta["hud_written"] == 0
    assert meta["hud_jpeg_quality"] is None
    assert meta["pad_samples"] == 0
    assert meta["frame_w"] == 32 and meta["frame_h"] == 16
    assert meta["stop_reason"] == "drive_phase_end"
    assert meta["monotonic_start_ns"] > 0
    # 场景分层字段（v3）：调用方不给时为 None——"没采集到"必须是可辨认的显式状态。
    # 靠键缺席来猜，正是 schema 1 那批数据认不出字节序的成因，别重演。
    assert "phase" in meta and meta["phase"] is None
    assert "round_no" in meta and meta["round_no"] is None


def test_meta_records_scene_strata(tmp_path) -> None:
    """阶段号 / 回合号必须落进 meta。

    离线按场景分层（分组划分、按阶段筛选）靠它——此前阶段号只隐式存在于会话目录名的
    `_p<N>` 后缀里、回合号则根本没有来源，筛选只能靠解析目录名。
    """
    rec = DriveRecorder(tmp_path / "s8", phase=2, round_no=3, pad_poll_hz=50.0)
    rec.start()
    rec.stop()

    meta = json.loads((tmp_path / "s8" / "meta.json").read_text(encoding="utf-8"))
    assert meta["phase"] == 2 and meta["round_no"] == 3


def test_queue_full_counts_drop_without_blocking(tmp_path) -> None:
    """反压纪律：队列满时丢帧计数，绝不阻塞采集侧。"""
    rec = DriveRecorder(tmp_path / "s4")
    rec._running = True  # 只测入队路径，不起后台线程
    for _ in range(rec._frame_q.maxsize):
        rec._frame_q.put_nowait((0, 0, 0, 0.0, _frame(2, 2), False))
    rec.record_frame(_frame(2, 2), frame_id=1, ts_ns=1)  # 队满 → 应计数而非等待
    assert rec._frames_dropped == 1
    assert rec._frames_written == 0


def test_stop_is_idempotent(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s5")
    rec.start()
    rec.record_frame(_frame(), frame_id=1, ts_ns=1)
    rec.stop()
    rec.stop()  # 重复调用不得抛异常、不得重写 meta
    assert rec.stats["frames_written"] == 1


def test_record_after_stop_is_ignored(tmp_path) -> None:
    rec = DriveRecorder(tmp_path / "s6")
    rec.start()
    rec.stop()
    rec.record_frame(_frame(), frame_id=1, ts_ns=1)
    assert rec.stats["frames_written"] == 0
    assert rec.stats["frames_dropped"] == 0


def test_recorded_frame_is_standard_pixel_order(tmp_path) -> None:
    """落盘帧必须是**标准图像语义**：看图/标注/训练框架按常规读即得运行时同色。

    录制器曾绕开 ``image_io`` 直写 imwrite，把 R/B 写反（imwrite 期望 BGR 序），
    已录三批会话的 JPEG 全是互换色——而 speedrush 的锚点齿轮是白灰（R=G=B）、
    摄像机图标也是白的，画面上完全看不出来。判据取通道幅度对比而非精确值：
    JPEG 有损，但"哪个通道亮"不会被压缩破坏。
    """
    import cv2

    rec = DriveRecorder(tmp_path / "s7", pad_poll_hz=50.0)
    rec.start()
    frame = np.zeros((16, 24, 3), dtype=np.uint8)
    frame[:, :, 0] = 220   # R 高
    frame[:, :, 1] = 120
    frame[:, :, 2] = 30    # B 低
    rec.record_frame(frame, frame_id=1, ts_ns=1)
    rec.stop()

    row = _read_jsonl(tmp_path / "s7" / "frames.jsonl")[0]
    got = cv2.imread(str(tmp_path / "s7" / row["file"]), cv2.IMREAD_COLOR)
    assert got is not None
    r_ch, b_ch = got[:, :, 2].mean(), got[:, :, 0].mean()  # imread 返回 BGR
    assert r_ch > b_ch + 100, f"R/B 互换（R={r_ch:.0f} B={b_ch:.0f}）—— 写盘前未转通道"


def _wait_until(pred, timeout_s: float = 2.0) -> None:
    import time
    deadline = time.monotonic() + timeout_s
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_hud_pair_written_for_sampled_frame(tmp_path) -> None:
    """注入渲染器 + 同拍快照 → 录制器自己写叠加图，索引 hud 指向实际文件。

    人工局的叠加图必须与索引同源：索引里出现的文件名就一定存在，
    渲染缺席写 null，不挂悬空指针。
    """
    snaps: list[dict] = []

    def _render(frame, snap):
        snaps.append(snap)
        return np.zeros((8, 12, 3), dtype=np.uint8)

    rec = DriveRecorder(tmp_path / "h1", pad_poll_hz=50.0, hud_render=_render)
    rec.start()
    rec.record_frame(_frame(), frame_id=7, ts_ns=70)
    rec.attach_hud_snap(7, {"fid": 7, "decision": {"steer": 0}})
    _wait_until(lambda: rec.stats["frames_written"] >= 1)
    rec.stop()

    row = _read_jsonl(tmp_path / "h1" / "frames.jsonl")[0]
    assert row["hud"] == "0001_hud.jpg"
    assert (tmp_path / "h1" / row["hud"]).is_file()
    # 渲染器拿到的是同一帧的形状（投影标记依赖它）
    assert snaps[0]["frame_shape"] == (48, 64, 3)
    meta = json.loads((tmp_path / "h1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["hud_written"] == 1 and meta["hud_jpeg_quality"] == 85


def test_hud_pair_absent_without_snap(tmp_path) -> None:
    """被节流选中但本拍没有决策快照：索引写 null，叠加图不落盘、也不算写错。"""
    rec = DriveRecorder(tmp_path / "h2", pad_poll_hz=50.0,
                        hud_render=lambda frame, snap: np.zeros((8, 8, 3), np.uint8))
    rec.start()
    rec.record_frame(_frame(), frame_id=9, ts_ns=90)
    rec.attach_hud_snap(9, None)
    _wait_until(lambda: rec.stats["frames_written"] >= 1)
    rec.stop()

    row = _read_jsonl(tmp_path / "h2" / "frames.jsonl")[0]
    assert row["hud"] is None
    assert rec.stats["hud_written"] == 0
    assert not list((tmp_path / "h2").glob("*_hud.jpg"))


def test_hud_pair_throttled_to_interval(tmp_path) -> None:
    """叠加图按节流窗采样：窗口内的后续帧只录原帧，不重复渲染。"""
    rec = DriveRecorder(tmp_path / "h3", pad_poll_hz=50.0,
                        hud_render=lambda frame, snap: np.zeros((8, 8, 3), np.uint8),
                        hud_interval_s=10.0)
    rec.start()
    for i in range(3):
        rec.record_frame(_frame(), frame_id=20 + i, ts_ns=200 + i)
        rec.attach_hud_snap(20 + i, {"fid": 20 + i})
    _wait_until(lambda: rec.stats["frames_written"] >= 3)
    rec.stop()

    rows = _read_jsonl(tmp_path / "h3" / "frames.jsonl")
    assert len(rows) == 3
    assert [r["hud"] is not None for r in rows] == [True, False, False]


def test_make_session_dir_uses_timestamp(tmp_path) -> None:
    d = make_session_dir(tmp_path)
    assert d.parent == tmp_path
    assert len(d.name) == len("20260916_120000")


def test_make_session_dir_tag_appends_to_name(tmp_path) -> None:
    """用途标签追加在时间戳之后：`<运行编号>_<标签>`，时间戳恒在最前。"""
    d = make_session_dir(tmp_path, tag="manual")
    assert d.parent == tmp_path
    assert d.name.endswith("_manual")
    assert len(d.name) == len("20260916_120000_manual")
    # 无标签 = 纯时间戳
    assert make_session_dir(tmp_path).name.startswith("2")


def test_make_session_dir_honours_given_stamp(tmp_path) -> None:
    """调用方可传运行编号（日志会话 token），让语料目录与日志目录同名。"""
    d = make_session_dir(tmp_path, tag="manual", stamp="20261028_220635")
    assert d.name == "20261028_220635_manual"


def _pad(index: int, *, lx: int = 0, ly: int = 0, rx: int = 0, ry: int = 0,
         lt: int = 0, rt: int = 0, btn: int = 0):
    return xinput.PadState(index=index, packet=1, buttons=btn,
                           left_trigger=lt, right_trigger=rt,
                           left_stick=(lx, ly), right_stick=(rx, ry))


def test_pad_sampling_survives_intermittent_read_failures(tmp_path, monkeypatch) -> None:
    """零星读取失败不得把采样线程判死。

    停采阈值是**连续**失败次数。此前实现只在成功时写样本、从不把计数清零，
    等于按"累计"算——整局里任何 5 次分散的抖动（设备省电、驱动重枚举）都会让
    采样永久停止，而日志里只有一条"未读到手柄"，meta 里只剩一个偏小的样本数，
    事后分不清"人没推杆"和"采样早就停了"。这里用成功/失败交替锁住语义。
    """
    calls = {"n": 0}

    def _alternating(index: int):
        if index != 0:
            return None
        calls["n"] += 1
        return None if calls["n"] % 2 == 0 else _pad(0, lx=-12000, lt=210)

    monkeypatch.setattr(xinput, "read_state", _alternating)
    rec = DriveRecorder(tmp_path / "p1", pad_poll_hz=200.0)
    rec.start()
    _wait_until(lambda: rec.stats["pad_samples"] >= 20, timeout_s=3.0)
    rec.stop()

    assert rec.stats["pad_samples"] >= 20, "交替失败被当成连续失败，采样线程提前退出"
    assert len(_read_jsonl(tmp_path / "p1" / "pads.jsonl")) >= 20


def test_pad_slot_prefers_slot_with_input(tmp_path, monkeypatch) -> None:
    """多槽位时锁"有输入活动"的那个，不锁第一个能读到的。

    虚拟手柄在程序不驱动时恒为静止全零。上一局控制模式残留的虚拟设备若占着
    0 号槽，旧实现会锁它，整局录到全零——文件看着完好，比没有数据更坏。
    """
    def _read(index: int):
        if index == 0:
            return _pad(0)                      # 静止：疑似虚拟手柄
        if index == 1:
            return _pad(1, lx=-9000, lt=180)    # 有人在推
        return None

    monkeypatch.setattr(xinput, "read_state", _read)
    rec = DriveRecorder(tmp_path / "p2", pad_poll_hz=200.0)
    rec.start()
    _wait_until(lambda: rec.stats["pad_samples"] >= 5, timeout_s=3.0)
    rec.stop()

    assert rec.pad_slot == 1, "锁到了静止槽位（虚拟手柄）"
    rows = _read_jsonl(tmp_path / "p2" / "pads.jsonl")
    assert rows and all(r["lx"] == -9000 and r["lt"] == 180 for r in rows)


def test_pad_slot_locks_single_idle_slot_without_waiting(tmp_path, monkeypatch) -> None:
    """唯一槽位不必等输入：无歧义时立即锁定并开始采样。"""
    monkeypatch.setattr(xinput, "read_state",
                        lambda index: _pad(0) if index == 0 else None)
    rec = DriveRecorder(tmp_path / "p3", pad_poll_hz=200.0)
    rec.start()
    _wait_until(lambda: rec.stats["pad_samples"] >= 5, timeout_s=3.0)
    rec.stop()

    assert rec.pad_slot == 0
    assert rec.stats["pad_samples"] >= 5


def test_pad_slot_probe_falls_back_after_timeout(tmp_path, monkeypatch) -> None:
    """多槽位且始终无输入：超时后锁首个并告警，不能无限期不采样。"""
    from maaracing_master.plugins.speedrush import recorder as recorder_mod

    monkeypatch.setattr(recorder_mod, "_PAD_SLOT_PROBE_TIMEOUT_S", 0.15)
    monkeypatch.setattr(xinput, "read_state",
                        lambda index: _pad(index) if index < 2 else None)
    rec = DriveRecorder(tmp_path / "p4", pad_poll_hz=100.0)
    rec.start()
    _wait_until(lambda: rec.stats["pad_samples"] >= 5, timeout_s=3.0)
    rec.stop()

    assert rec.pad_slot == 0
    assert rec.stats["pad_samples"] >= 5
