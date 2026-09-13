# -*- coding: utf-8 -*-
"""鉴宝性能仪表（快照 / 识别健康三态 / 场次边界清零）行为锁定。

为什么值得单独锁：这套仪表的存在理由是一次「第1、2回合报价没录入」的回归——
OCR 耗时 p50 全程正常，坏掉的只有尾部，而定性靠的是手扫整份日志算丢弃比值。
仪表把那两个比值变成读数后，**阈值本身**就成了新的静默失败点：定松了，
真机那种"两回合全丢"只判到 warn；定紧了，正常机器负载抖动就报红。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

# module 顶层 import maa.toolkit，CI 轻依赖环境下收集期 ERROR 会中断整个会话；
# 按仓内既有口径整文件优雅跳过。
try:
    from maaracing_master.plugins.treasure.module import TreasureModule

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)
    TreasureModule = None

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)


def _mk(*, applied=0, stale=0, expired=0, wr_applied=0, wr_stale=0, wr_expired=0, ages=()):
    """离线实例 + 直接灌计数：仪表是纯读侧聚合，灌数即等价于跑过那些帧。"""
    m = TreasureModule()
    m._ocr_applied, m._ocr_stale_drops, m._ocr_expired_drops = applied, stale, expired
    m._ocr_applied_wr, m._ocr_stale_drops_wr, m._ocr_expired_drops_wr = (
        wr_applied, wr_stale, wr_expired)
    for a in ages:
        m._age_ms_win.append(float(a))
    return m


class TestPercentile:
    def test_empty_window_is_zero_not_crash(self):
        assert TreasureModule._percentile([], 0.5) == 0.0
        assert TreasureModule._percentile((), 0.95) == 0.0

    def test_single_sample(self):
        assert TreasureModule._percentile([120.0], 0.95) == 120.0

    def test_linear_interpolation_on_ordered_range(self):
        vals = list(range(1, 101))
        assert TreasureModule._percentile(vals, 0.50) == pytest.approx(50.5)
        assert TreasureModule._percentile(vals, 0.95) == pytest.approx(95.05)

    def test_unordered_input_is_sorted_internally(self):
        assert TreasureModule._percentile([900.0, 100.0, 500.0], 0.5) == 500.0

    def test_ratio_guards_zero_denominator(self):
        assert TreasureModule._ratio(0, 0) == 0.0
        assert TreasureModule._ratio(3, 12) == 0.25


class TestRecognitionHealthCalibration:
    """三态判据的校准点——每条都对应一个真实或假想的机器状态。"""

    def test_no_data_is_idle_not_ok(self):
        """开局无数据不得报"正常"，否则仪表在最需要它的时候最安静。"""
        assert _mk().read_recognition_health()["level"] == "idle"

    def test_true_incident_shape_is_error(self):
        """真机那场：全局丢弃只有 18.8%（判 warn 太松），报价窗口内 3/5 丢。

        这条锁的是"必须按窗口分桶"这个决定本身——只看全局比值就会漏放一次
        两回合数据全丢的事故。
        """
        h = _mk(applied=315, stale=73, wr_applied=2, wr_stale=3,
                ages=[218, 904, 1200]).read_recognition_health()
        assert h["level"] == "error"
        assert h["scope"] == "bid_window"
        assert h["drop_ratio"] == pytest.approx(0.6)

    def test_healthy_run_is_ok(self):
        """未开落盘的历史基线：0% 丢弃、时效 p95 220ms（闸值 800ms 的 27%）。"""
        h = _mk(applied=973, wr_applied=4, ages=[177, 220]).read_recognition_health()
        assert h["level"] == "ok"
        assert h["drop_ratio"] == 0.0

    def test_mild_degradation_is_warn(self):
        h = _mk(applied=700, stale=22, wr_applied=20, wr_stale=2,
                ages=[223, 342]).read_recognition_health()
        assert h["level"] == "warn"

    def test_tail_age_alone_trips_warn_even_at_zero_drops(self):
        """丢弃率还没起来、但尾部时效已逼近闸门 → 提前 warn，别等数据丢了才说。"""
        gate = TreasureModule.OCR_MAX_AGE_MS
        h = _mk(applied=50, wr_applied=50, ages=[100, gate * 0.9]).read_recognition_health()
        assert h["level"] == "warn"

    def test_falls_back_to_overall_before_any_bid_window(self):
        """还没进过 wait_result 时用全局口径，且 scope 要如实标出来。"""
        h = _mk(applied=50, stale=1).read_recognition_health()
        assert h["scope"] == "overall"
        assert h["drop_ratio"] == pytest.approx(1 / 51, abs=1e-4)


class TestSnapshotContract:
    """GUI 卡片按 key 取值，结构就是接口。"""

    def test_snapshot_has_the_three_card_sections(self):
        s = _mk(applied=10, ages=[200]).read_perf_snapshot()
        assert {"response", "ocr", "debug_io", "health", "cpu"} <= set(s)
        assert {"available", "p50", "p95", "max"} <= set(s["cpu"])
        assert {"fps", "tick_gap_ms"} <= set(s["response"])
        assert {"p50", "p95"} <= set(s["ocr"]["age_ms"])
        assert {"applied", "stale_drops", "expired_drops", "drop_ratio"} <= set(
            s["ocr"]["bid_window"])
        assert {"enqueued", "dropped", "drop_ratio", "queue_peak", "queue_max"} <= set(
            s["debug_io"])

    def test_snapshot_is_side_effect_free(self):
        """可被 GUI 轮询反复调用：读两次必须完全一致。"""
        m = _mk(applied=10, stale=2, wr_applied=3, wr_stale=1, ages=[200, 900])
        assert m.read_perf_snapshot() == m.read_perf_snapshot()

    def test_age_gate_is_reported_alongside_ages(self):
        """阈值要随读数一起出，否则 GUI 只能把 800 抄成第二份真源。"""
        s = _mk(applied=1).read_perf_snapshot()
        assert s["ocr"]["age_gate_ms"] == TreasureModule.OCR_MAX_AGE_MS


class TestSessionBoundaryReset:
    def test_reset_clears_instrument_but_not_hud_counters(self):
        """仪表按场清零；`_ocr_total_runs` 是 debug HUD「运行次数」的整轮口径，不得动。"""
        m = _mk(applied=10, stale=2, wr_applied=3, wr_stale=1, ages=[200])
        m._ocr_total_runs, m._ocr_failures = 999, 3
        m._io_enqueued, m._io_dropped, m._io_queue_peak = 40, 6, 8
        m._tick_gap_ms.append(150.0)
        m._cpu_pct_win.append(137.0)
        m._cpu_last = (10.0, 20.0)

        m._reset_perf_counters()

        assert m._ocr_applied == 0 and m._ocr_stale_drops == 0
        assert m._ocr_applied_wr == 0 and m._ocr_stale_drops_wr == 0
        assert m._io_enqueued == 0 and m._io_dropped == 0 and m._io_queue_peak == 0
        assert len(m._tick_gap_ms) == 0 and len(m._age_ms_win) == 0
        assert len(m._cpu_pct_win) == 0 and m._cpu_last is None
        assert m._ocr_total_runs == 999 and m._ocr_failures == 3
        assert m.read_recognition_health()["level"] == "idle"
        assert m.read_perf_snapshot()["cpu"]["available"] is False, "无读数不得冒充 0%"

    def test_new_session_boundary_emits_summary_then_recounts(self):
        """跨场必须先把上一场的数字说出口，再清零——否则 50 场摊薄后看不出哪场坏。"""
        m = _mk(applied=10, stale=2, wr_applied=3, wr_stale=1, ages=[200])
        m._strategy = None
        seen: list[str] = []

        from maaracing_master.plugins.treasure import module as mod

        real_log = mod.logger.log

        def _capture(text, level="INFO", *_a, **_k):
            seen.append(text)

        mod.logger.log = _capture
        try:
            m._reset_round_state("进入鉴宝大厅(选择场次)")
        finally:
            mod.logger.log = real_log

        assert any("[性能]" in t and "上一场" in t for t in seen), seen
        assert m._ocr_applied == 0 and m._ocr_stale_drops == 0

    def test_io_only_activity_skips_summary_but_still_recounts(self):
        """预热期只有落盘、OCR 全 0 → 不打空汇总（噪音），但计数照样在边界清零。"""
        m = _mk()
        m._io_enqueued, m._io_dropped = 38, 0
        seen: list[str] = []
        from maaracing_master.plugins.treasure import module as mod

        real_log = mod.logger.log
        mod.logger.log = lambda text, level="INFO", *_a, **_k: seen.append(text)
        try:
            m._reset_round_state("进入鉴宝大厅(选择场次)")
        finally:
            mod.logger.log = real_log

        assert not any("[性能]" in t for t in seen), seen
        assert m._io_enqueued == 0 and m._io_dropped == 0


class TestLevelCalibration:
    """响应/负载两项目标的三态阈值——和识别健康一样，定松了就是没有仪表。"""

    def test_response_level_bands(self):
        m = _mk(applied=10)
        for gap, expect in ((120.0, "ok"), (200.0, "ok"), (250.0, "warn"), (600.0, "error")):
            m._tick_gap_ms.clear()
            m._tick_gap_ms.append(gap)
            assert m.read_perf_snapshot()["response"]["level"] == expect, f"{gap}ms→{expect}"

    def test_no_response_data_is_idle_not_error(self):
        assert _mk().read_perf_snapshot()["response"]["level"] == "idle"

    def test_load_is_normalized_by_core_count(self, monkeypatch):
        import maaracing_master.plugins.treasure.module as mod

        monkeypatch.setattr(mod, "logical_core_count", lambda: 8)
        m = _mk(applied=10)
        m._cpu_pct_win.append(480.0)     # 8 核跑满一半 → 负载 0.60
        c = m.read_perf_snapshot()["cpu"]
        assert c["cores"] == 8
        assert c["load_p50"] == pytest.approx(0.60, abs=0.01)
        assert c["level"] in ("warn", "ok")     # 恰在阈值上，两态皆可接受
        m._cpu_pct_win.clear()
        m._cpu_pct_win.append(760.0)            # 0.95 → error
        assert m.read_perf_snapshot()["cpu"]["level"] == "error"

    def test_unavailable_cpu_is_idle_not_zero_load(self, monkeypatch):
        import maaracing_master.plugins.treasure.module as mod

        monkeypatch.setattr(mod, "process_cpu_seconds", lambda: None)
        c = _mk(applied=10).read_perf_snapshot()["cpu"]
        assert c["available"] is False and c["level"] == "idle"


class TestSidecarPassthrough:
    """get_status 把快照带出去这一段是 GUI 唯一数据通道，且不得因仪表而炸轮询。"""

    @staticmethod
    def _service(active_module):
        from maaracing_master.core.sidecar import SidecarService
        import threading

        svc = SidecarService.__new__(SidecarService)      # 绕开真实 controller 装配
        svc._lock = threading.Lock()
        svc._worker = None
        svc._selected_module = "treasure"
        svc._controller = SimpleNamespace(
            module_active=False, current_stage=None, active_module=active_module)
        return svc

    def test_perf_flows_through_get_status(self):
        m = _mk(applied=10, ages=[200])
        ok, payload, err = TestSidecarPassthrough._service(m).get_status({})
        assert ok and err is None
        assert payload["perf"]["ocr"]["applied"] == 10
        assert {"response", "cpu", "health"} <= set(payload["perf"])

    def test_module_without_instrument_yields_none_perf(self):
        class _Bare:
            pass

        ok, payload, _ = TestSidecarPassthrough._service(_Bare()).get_status({})
        assert ok and payload["perf"] is None

    def test_no_active_module_yields_none_perf(self):
        ok, payload, _ = TestSidecarPassthrough._service(None).get_status({})
        assert ok and payload["perf"] is None

    def test_instrument_failure_must_not_break_polling(self):
        class _Boom:
            def read_perf_snapshot(self):
                raise RuntimeError("仪表内部炸了")

        ok, payload, _ = TestSidecarPassthrough._service(_Boom()).get_status({})
        assert ok and payload["perf"] is None


class TestCpuSampling:
    """CPU 占用是差分量：无读数时必须标不可得，不能拿 0% 冒充"机器很空闲"。"""

    def test_first_sample_only_primes_without_emitting(self):
        m = _mk()
        m._sample_cpu(1000.0)
        assert m._cpu_last is not None
        assert len(m._cpu_pct_win) == 0

    def test_delta_over_wall_clock_becomes_percentage(self, monkeypatch):
        import maaracing_master.plugins.treasure.module as mod

        seq = iter([10.0, 10.5])
        monkeypatch.setattr(mod, "process_cpu_seconds", lambda: next(seq))
        m = _mk()
        m._sample_cpu(1000.0)      # 预热
        m._sample_cpu(1000.5)      # 0.5 CPU 秒 / 0.5 墙钟秒 = 100%
        assert m._cpu_pct_win and m._cpu_pct_win[-1] == pytest.approx(100.0, abs=1.0)

    def test_unavailable_source_leaves_window_empty(self, monkeypatch):
        import maaracing_master.plugins.treasure.module as mod

        monkeypatch.setattr(mod, "process_cpu_seconds", lambda: None)
        m = _mk()
        m._sample_cpu(1000.0)
        m._sample_cpu(1001.0)
        assert len(m._cpu_pct_win) == 0
        assert m._cpu_last is None
        assert m.read_perf_snapshot()["cpu"]["available"] is False
