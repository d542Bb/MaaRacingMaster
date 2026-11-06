# -*- coding: utf-8 -*-
"""运行级调试流契约测试（P0）。

锁定 ADR-0019（分段/预览）与 ADR-0021（会话目录）及 core/CODE_WIKI.md §1.4 的：
- 入队绑定与 FIFO 顺序（旧段帧不被标成新段、控制事件不随帧淘汰）
- 双速率独立、开关位置生效（帧携带入队时状态）
- 排空与故障隔离
- 计数闭合
"""
from __future__ import annotations

import json
import time

import numpy as np

from maaracing_master.core.debug import (
    NavigationDebugger,
    _StreamItem,
    _StreamQueue,
)


def _frame(value: int = 0) -> np.ndarray:
    return np.full((16, 16, 3), value % 256, dtype=np.uint8)


def _hub(tmp_path, *, debug_on: bool = True, peep_on: bool = True) -> NavigationDebugger:
    d = NavigationDebugger(tmp_path)
    d.enabled = debug_on
    d.peep_enabled = peep_on
    d.begin_run("run_test")
    return d


def _wait(d: NavigationDebugger, timeout: float = 3.0) -> None:
    assert d._wait_drain(timeout), "调试流未在期限内排空"


def _index_lines(tmp_path) -> list[dict]:
    path = tmp_path / "index.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


class _BoomRenderer:
    def render_full(self, *args, **kwargs):
        raise RuntimeError("render boom")

    def render_peep(self, *args, **kwargs):
        raise RuntimeError("peep boom")


class TestBindingAndOrder:
    def test_frame_keeps_enqueue_segment(self, tmp_path):
        d = _hub(tmp_path)
        try:
            scope = d.open_scope("launch_game", "启动游戏")
            a = d.open_segment("nav", "进入大厅", peep_fps=1000, save_fps=1000)
            a.offer(_frame(1), fid=1)
            a.close("reached")
            b = d.open_segment("nav", "进入活动页", peep_fps=1000, save_fps=1000)
            b.offer(_frame(2), fid=2)
            b.close("reached")
            _wait(d)
            d.close_segment(scope, "completed")
            _wait(d)

            lines = _index_lines(tmp_path)
            frames = [x for x in lines if x["type"] == "frame"]
            assert [f["owner"] for f in frames] == ["nav", "nav"]
            assert frames[0]["segment"] != frames[1]["segment"]
            assert [f["fid"] for f in frames] == [1, 2]
            # 每个帧的段结束事件排在该帧之后（FIFO）
            for f in frames:
                end_at = [i for i, x in enumerate(lines)
                          if x["type"] == "segment_end" and x["segment"] == f["segment"]]
                frame_at = lines.index(f)
                assert end_at and max(end_at) > frame_at
        finally:
            d.end_run()

    def test_close_clears_peep(self, tmp_path):
        d = _hub(tmp_path)
        try:
            seg = d.open_segment("nav", "进入大厅", peep_fps=1000)
            seg.offer(_frame(9))
            _wait(d)
            assert d.get_peep_frame()["state"] == "live"
            seg.close("reached")
            _wait(d)
            assert d.get_peep_frame()["state"] == "idle"
            assert d.get_peep_frame()["frame"] is None
        finally:
            d.end_run()

    def test_no_frame_segment_is_gap(self, tmp_path):
        d = _hub(tmp_path)
        try:
            d.open_segment("close_game", "关闭游戏", peep_fps=1000, save="off")
            _wait(d)
            out = d.get_peep_frame()
            assert out["frame"] is None
            assert out["state"] == "gap"
        finally:
            d.end_run()


class TestStorageLayout:
    def test_groups_by_scope_and_executor_without_order_prefix(self, tmp_path):
        """目录只表达模块/执行器；顺序与时间在索引里，不靠数字前缀。"""
        d = _hub(tmp_path)
        try:
            scope = d.open_scope("speedrush", "极速狂飙")
            nav = d.open_segment("nav", "点比赛", peep_fps=1000, save_fps=1000)
            nav.offer(_frame(1))
            _wait(d)
            nav.close("reached")
            _wait(d)
            own = d.open_segment("speedrush", "控制", peep_fps=1000, save_fps=1000)
            own.offer(_frame(2))
            _wait(d)
            own.close("completed")
            d.close_segment(scope, "completed")
            _wait(d)

            files = [x["file"] for x in _index_lines(tmp_path) if x["type"] == "frame"]
            assert files[0].startswith("speedrush/nav/")
            assert files[1].startswith("speedrush/control/")
            assert all("/00_" not in f and "/01_" not in f for f in files)
        finally:
            d.end_run()


class TestPreviewContinuity:
    def test_inner_segment_switch_keeps_last_frame(self, tmp_path):
        """同一运行项内换导航步骤不清预览：否则每个 UI 步骤都会闪成空卡片。"""
        d = _hub(tmp_path)
        try:
            scope = d.open_scope("speedrush", "极速狂飙")
            seg = d.open_segment("nav", "点比赛", peep_fps=1000)
            seg.offer(_frame(1))
            _wait(d)
            assert d.get_peep_frame()["state"] == "live"

            seg.close("reached")
            _wait(d)
            out = d.get_peep_frame()
            assert out["frame"] is not None and out["state"] == "live"

            d.open_segment("nav", "点娱乐玩法", peep_fps=1000)
            assert d.get_peep_frame()["frame"] is not None

            d.close_segment(scope, "completed")
            _wait(d)
            assert d.get_peep_frame()["frame"] is None
        finally:
            d.end_run()


class TestQueue:
    def test_control_events_survive_frame_eviction(self):
        q = _StreamQueue(frame_cap=2)
        for i in range(3):
            q.put_control(_StreamItem(kind="control", seq=i,
                                      event="segment_start", segment_id=i))
            q.put_frame(_StreamItem(kind="frame", seq=100 + i, segment_id=i))
        items = q.take_all()
        controls = [x for x in items if x.kind == "control"]
        frames = [x for x in items if x.kind == "frame"]
        assert len(controls) == 3
        assert [f.seq for f in frames] == [101, 102]
        assert q.dropped_frames == 1

    def test_control_high_water_coalesces_stage_only(self):
        q = _StreamQueue(frame_cap=2, control_high_water=3)
        q.put_control(_StreamItem(kind="control", seq=0,
                                  event="segment_start", segment_id=1))
        for i in range(1, 4):
            q.put_control(_StreamItem(kind="control", seq=i,
                                      event="stage_change", segment_id=1))
        items = q.take_all()
        starts = [x for x in items if x.event == "segment_start"]
        stages = [x for x in items if x.event == "stage_change"]
        assert len(starts) == 1
        assert len(stages) == 1 and stages[0].seq == 3
        assert q.degraded
        assert q.stage_events_coalesced == 2


class TestRatesAndCounters:
    def test_peep_and_save_rates_independent(self, tmp_path):
        d = _hub(tmp_path)
        try:
            d._q._frame_cap = 1000
            seg = d.open_segment("nav", "x", peep_fps=10, save_fps=5)
            for i in range(100):
                # 模拟 10Hz 到达、5Hz 存盘：直接回拨到期时刻。
                # 回拨 0.11s 而不是 0.1s——Windows monotonic 约 15ms 粒度，
                # 恰好 0.1 会被量化成「还没到期」，测试变成每个隔一帧。
                seg._last_peep -= 0.11
                seg._last_save -= 0.11
                seg.offer(_frame(i))
            _wait(d, 5.0)
            c = d.status()["counters"]
            assert 95 <= c["peep_encoded"] <= 105
            assert 45 <= c["save_written"] <= 55
            assert c["frames_due_peep"] == c["peep_encoded"]
            assert c["frames_due_save"] == c["save_written"]
        finally:
            d.end_run()

    def test_counter_closure_and_index_file_match(self, tmp_path):
        d = _hub(tmp_path)
        try:
            d._q._frame_cap = 1000
            seg = d.open_segment("nav", "x", peep_fps=1000, save_fps=1000)
            for i in range(20):
                seg.offer(_frame(i))
            _wait(d)
            c = d.status()["counters"]
            assert c["frames_in"] == c["frames_processed"] + c["frames_dropped_queue"] + c["pending_at_close"]
            assert c["frames_due_save"] == (
                c["save_written"] + c["save_render_failed"] + c["save_write_failed"])
            assert c["frames_due_peep"] == (
                c["peep_encoded"] + c["peep_render_failed"] + c["peep_encode_failed"])
            d.close_segment(seg, "completed")
            _wait(d)
            files = list(tmp_path.rglob("*.jpg"))
            frames = [x for x in _index_lines(tmp_path) if x["type"] == "frame"]
            assert len(files) == c["save_written"] == len(frames)
        finally:
            d.end_run()

    def test_evicted_frames_do_not_break_due_closure(self, tmp_path):
        """队列满丢旧帧时，被丢帧不能继续算“到期”——否则 due = 成功 + 失败 断裂。"""
        d = _hub(tmp_path)
        try:
            d._q._frame_cap = 2
            d._stop_worker()
            seg = d.open_segment("nav", "x", peep_fps=1000, save_fps=1000)
            for i in range(12):
                # 每帧都回拨到期时刻，保证 12 帧全部“到期入队”（否则会被限速跳过）
                seg._last_peep -= 0.5
                seg._last_save -= 0.5
                seg.offer(_frame(i))
            d._ensure_worker()
            _wait(d)
            c = d.status()["counters"]
            assert c["frames_dropped_queue"] == 10
            assert c["frames_in"] == c["frames_processed"] + c["frames_dropped_queue"]
            assert c["frames_due_save"] == (
                c["save_written"] + c["save_render_failed"] + c["save_write_failed"])
            assert c["frames_due_peep"] == (
                c["peep_encoded"] + c["peep_render_failed"] + c["peep_encode_failed"])
        finally:
            d.end_run()


class TestToggleBoundary:
    def test_peep_off_stops_new_frames_and_clears(self, tmp_path):
        d = _hub(tmp_path)
        try:
            seg = d.open_segment("nav", "x", peep_fps=1000)
            seg.offer(_frame(1))
            _wait(d)
            assert d.get_peep_frame()["state"] == "live"

            d.peep_enabled = False
            _wait(d)
            out = d.get_peep_frame()
            assert out["state"] == "disabled" and out["frame"] is None

            before = d.status()["counters"]["peep_encoded"]
            seg.offer(_frame(2))
            _wait(d)
            assert d.status()["counters"]["peep_encoded"] == before
        finally:
            d.end_run()

    def test_save_inflight_frame_completes_after_off(self, tmp_path):
        d = _hub(tmp_path)
        try:
            seg = d.open_segment("nav", "x", peep_fps=0, save_fps=1000)
            seg.offer(_frame(1))        # 关闭前入队：按旧状态写
            d.enabled = False           # 关闭事件入队
            seg.offer(_frame(2))        # 关闭后入队：不写
            _wait(d)
            c = d.status()["counters"]
            assert c["save_written"] == 1
            # 在途帧照常落盘，索引也要有行：文件数 = 索引帧行数
            files = list(tmp_path.rglob("*.jpg"))
            frames = [x for x in _index_lines(tmp_path) if x["type"] == "frame"]
            assert len(files) == len(frames) == 1
        finally:
            d.end_run()


class TestFailureIsolation:
    def test_renderer_failure_does_not_stop_stream(self, tmp_path):
        d = _hub(tmp_path)
        try:
            token = d.install_renderer(_BoomRenderer())
            seg = d.open_segment("nav", "x", peep_fps=1000, save_fps=1000)
            seg.offer(_frame(1))
            _wait(d)
            c = d.status()["counters"]
            assert c["save_render_failed"] == 1
            assert c["peep_encoded"] == 1     # PEEP 基线不调用模块渲染器
            assert d._worker_errors == 0
            d.remove_renderer(token)
        finally:
            d.end_run()


class TestStaticFrame:
    def test_same_fid_is_not_resampled(self, tmp_path):
        """静止画面（同一帧号）不重复渲染/写盘：WGC 不出新帧时不该按 10Hz 刷同一张图。"""
        d = _hub(tmp_path)
        try:
            d.set_frame_source(lambda: (_frame(5), 7, 0, 100.0))
            d.open_segment("nav", "x", peep_fps=1000, save_fps=1000)
            time.sleep(0.35)
            _wait(d)
            assert d.status()["counters"]["frames_in"] == 1
        finally:
            d.end_run()

    def test_static_frame_reoffered_after_segment_switch(self, tmp_path):
        """段切换后同一帧号要再入队一次：否则静止画面下新段永远停在 gap。"""
        d = _hub(tmp_path)
        try:
            d.set_frame_source(lambda: (_frame(5), 7, 0, 100.0))
            a = d.open_segment("nav", "a", peep_fps=1000, save_fps=1000)
            time.sleep(0.2)
            _wait(d)
            assert d.status()["counters"]["frames_in"] == 1
            a.close("reached")
            b = d.open_segment("nav", "b", peep_fps=1000, save_fps=1000)
            time.sleep(0.2)
            _wait(d)
            d.close_segment(b, "completed")
            _wait(d)
            # 栈空后 status 返回整轮计数：两段各拿到一帧
            assert d.status()["counters"]["frames_in"] == 2
        finally:
            d.end_run()


class TestForcedClose:
    def test_end_run_closes_leaked_segments(self, tmp_path):
        d = _hub(tmp_path)
        try:
            d.open_scope("launch_game", "启动游戏")
            d.open_segment("nav", "进入大厅", peep_fps=1000)
            d.end_run()
            lines = _index_lines(tmp_path)
            ends = [x for x in lines if x["type"] == "segment_end"]
            assert len(ends) == 2
            assert all(x["outcome"] == "forced_end" for x in ends)
            assert d.status()["depth"] == 0
        finally:
            d.end_run()

    def test_end_run_records_pending_on_drain_timeout(self, tmp_path):
        """排空超时不得静默清队列：剩余帧计入 pending_at_close 并写进结束事件。"""
        d = _hub(tmp_path)
        seg = d.open_segment("nav", "x", peep_fps=1000, save_fps=1000)
        d._stop_worker()                 # 消费者先停，队列里的帧必然处理不了
        seg.offer(_frame(1))
        seen = []
        orig_end = d._enqueue_end

        def rec(seg_obj, outcome, drained, pending):
            seen.append((seg_obj.id, outcome, drained, pending))
            return orig_end(seg_obj, outcome, drained, pending)

        d._enqueue_end = rec
        d._wait_drain = lambda timeout: False
        d.end_run()
        assert seen == [(seg.id, "forced_end", False, 1)]
        assert d.status()["counters"]["pending_at_close"] == 1


class TestSeqAndRejected:
    def test_seq_starts_at_one(self, tmp_path):
        d = _hub(tmp_path)
        try:
            d.open_segment("nav", "x", peep_fps=1000)
            _wait(d)
            starts = [x for x in _index_lines(tmp_path) if x["type"] == "segment_start"]
            assert starts and starts[0]["seq"] == 1
            # 段边界也必须带双时钟，才能落到运行时间轴
            assert starts[0]["ts_wall_ns"] > 0
            assert starts[0]["ts_mono_ns"] > 0
        finally:
            d.end_run()

    def test_late_offer_to_closed_segment_is_counted(self, tmp_path):
        d = _hub(tmp_path)
        try:
            seg = d.open_segment("nav", "x", peep_fps=1000)
            seg.close("reached")
            seg.offer(_frame(2))
            assert d.status()["counters"]["rejected_at_close"] == 1
        finally:
            d.end_run()
