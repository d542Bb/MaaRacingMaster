# -*- coding: utf-8 -*-
"""性能采样器阶段 B：领域面入样本与落盘、单写手、写盘失败状态机。

对应契约文档 §7 验收条件中属于阶段 B 的部分（6、7、15、22），
以及 §4.6 的样本一致性语义（三面各自计时、样本号取 tick 开始）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from maaracing_master.core.perf_sampler import PerfSampler, build_perf_payload

ROWS = [
    {"key": "tick_rate", "label": "驾驶拍率", "kind": "rate",
     "category": "player", "value": 12.4, "display": "12.4 次/秒",
     "level": "warn", "reason": None, "hint": None},
    {"key": "decision", "label": "决策实况", "kind": "state",
     "category": "player", "value": None, "display": "巡航 · 规划正常",
     "level": "ok", "reason": None, "hint": None},
]


def make_sampler(**kw) -> PerfSampler:
    s = PerfSampler(interval_s=0.01, gpu_every=1, keep_every=1,
                    scan_every=10_000, gpu_runner=lambda t: "50, 3000, 8192",
                    **kw)
    s._sample_system = lambda: {"cpu_pct": 12.0, "mem_pct": 50.0,
                                "mem_used_gb": 8.0, "mem_total_gb": 16.0}
    s._sample_procs = lambda: [
        {"pid": 1, "name": "self", "cpu_pct": 30.0, "mem_mb": 200.0}]
    return s


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines() if x.strip()]


class _BoomFile:
    """会失败 N 次的假句柄：写满 N 次后恢复正常，用来驱动状态机。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.buf: list[str] = []

    def write(self, text: str) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("disk full")
        self.buf.append(text)
        return len(text)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- 领域面


def test_domain_rows_enter_sample_and_file(tmp_path):
    """验收 7：跑一轮后能从 perf.jsonl 读到模块声明的每一行。"""
    s = make_sampler()
    s.set_domain_reader(lambda: ROWS)
    s.on_session_open(tmp_path, "t_1")
    sample = s.tick_once()
    assert sample["domain"] == ROWS

    path = tmp_path / "perf.jsonl"
    samples = [r for r in _lines(path) if r.get("type") == "sample"]
    assert len(samples) == 1
    assert [r["key"] for r in samples[0]["domain"]] == ["tick_rate", "decision"]
    assert samples[0]["domain"][0]["display"] == "12.4 次/秒"
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_no_reader_yields_resource_only(tmp_path):
    """没有活动模块：样本只带资源面，domain 是空数组（不是缺失字段）。"""
    s = make_sampler()
    s.on_session_open(tmp_path, "t_1")
    sample = s.tick_once()
    assert sample["domain"] == []
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_domain_reader_error_does_not_break_sample(tmp_path):
    """取值口违约抛异常：样本照出、资源面照常，错误显式记下。"""
    s = make_sampler()
    s.set_domain_reader(lambda: (_ for _ in ()).throw(RuntimeError("模块炸了")))
    s.on_session_open(tmp_path, "t_1")
    sample = s.tick_once()
    assert sample["domain"] == [] and "模块炸了" in sample["domain_error"]
    assert sample["cpu_pct"] == 12.0                # 资源面不受牵连
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_domain_reader_swapped_between_ticks(tmp_path):
    """换模块：每拍读一次引用，下一拍就跟上，不会拿到半新半旧的数据。"""
    s = make_sampler()
    s.set_domain_reader(lambda: ROWS[:1])
    s.on_session_open(tmp_path, "t_1")
    assert len(s.tick_once()["domain"]) == 1
    s.set_domain_reader(lambda: ROWS)
    assert len(s.tick_once()["domain"]) == 2
    s.set_domain_reader(None)                        # 模块停止
    assert s.tick_once()["domain"] == []
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_cost_ms_breaks_down_each_face(tmp_path):
    """三面不是同一瞬间：各面耗时都要记，total 不小于任一单项。"""
    s = make_sampler()
    s.set_domain_reader(lambda: ROWS)
    s.on_session_open(tmp_path, "t_1")
    cost = s.tick_once()["cost_ms"]
    assert set(cost) == {"sys", "procs", "gpu", "domain", "threads", "total"}
    assert cost["total"] >= max(cost[k] for k in
                                ("sys", "procs", "gpu", "domain", "threads"))
    assert all(v >= 0 for v in cost.values())
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_panel_payload_reads_same_sample_as_log(tmp_path):
    """面板与日志同源：同一 sample_id 下，载荷的 rows 与盘上样本的 domain 一致。"""
    s = make_sampler()
    s.set_domain_reader(lambda: ROWS)
    s.on_session_open(tmp_path, "t_1")
    s.tick_once()
    snap = s.snapshot()
    payload = build_perf_payload(snap.get("domain"), snap)
    disk = [r for r in _lines(tmp_path / "perf.jsonl") if r.get("type") == "sample"]
    assert payload["sample_id"] == disk[0]["sample_id"]
    assert payload["rows"] == disk[0]["domain"]
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


# ---------------------------------------------------------------- 写盘失败


def test_persist_failure_records_gap_then_recovers(tmp_path):
    """验收 15：失败记缺口 → 补标记 → **继续写当前样本**，三步都验。"""
    s = make_sampler()
    s.on_session_open(tmp_path, "t_1")
    s._file.close()
    boom = _BoomFile(fail_times=2)
    s._file = boom

    s.tick_once()                                   # ① 样本写失败
    assert s._persist_failures == 1 and s._persist_gap == (1, 1)

    s.tick_once()                                   # ② 补标记也失败 → 整拍推迟
    assert s._persist_failures == 2 and s._persist_gap == (1, 2)

    s.tick_once()                                   # ③ 标记成功 + 样本成功
    assert s._persist_gap is None
    kinds = [json.loads(x).get("type") for x in boom.buf]
    assert kinds[0] == "persist_gap"                # 先补缺口
    assert kinds.count("sample") == 1               # 再写当前样本（没有丢）
    gap = json.loads(boom.buf[0])
    assert (gap["from"], gap["to"]) == (1, 2)

    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_persist_gap_marker_failure_keeps_interval(tmp_path):
    """验收 22：标记写失败时区间保留并合并，不回「正常」。"""
    s = make_sampler()
    s.on_session_open(tmp_path, "t_1")
    s._file.close()
    s._file = _BoomFile(fail_times=10)              # 一直坏

    for _ in range(3):
        s.tick_once()
    assert s._persist_gap == (1, 3)                 # 相邻区间合并，不覆盖
    assert s._persist_consecutive >= 3
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


def test_close_summary_reports_unresolved_gap(tmp_path):
    """缺口没补完：关闭摘要必须 ok=False 并带上区间，不能宣称已完整落盘。"""
    s = make_sampler()
    s.on_session_open(tmp_path, "t_1")
    s._file.close()
    s._file = _BoomFile(fail_times=10)
    s.tick_once()
    summary = s.on_session_closing(time.monotonic() + 1)
    assert summary.ok is False
    assert summary.details["persist_gap"] == (1, 1)
    assert summary.details["persist_failures"] >= 1
    s.stop()


def test_session_open_resets_persist_state(tmp_path):
    """新会话新文件：缺口与计数从头记（旧文件的缺口随旧句柄结束）。"""
    s = make_sampler()
    s.on_session_open(tmp_path, "t_1")
    s._file.close()
    s._file = _BoomFile(fail_times=1)
    s.tick_once()
    assert s._persist_gap == (1, 1)
    s.on_session_open(tmp_path / "s2", "t_2")
    assert s._persist_gap is None
    assert s._persist_failures == 0
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


# ---------------------------------------------------------------- 单写手


def test_single_writer_static():
    """验收 6：只有采样器打开 perf.jsonl（其余文件提到它只能是注释）。"""
    root = Path(__file__).resolve().parents[1] / "maaracing_master"
    holders = [p.name for p in root.rglob("*.py")
               if '"perf.jsonl"' in p.read_text(encoding="utf-8")]
    assert holders == ["perf_sampler.py"], holders


# ---------------------------------------------------------------- 取值口闭包


def test_sidecar_reader_follows_active_module():
    """取值口按**当前**活动模块取值：无模块 → 空，有模块 → 声明行，违约 → read_error。"""
    from maaracing_master.core.sidecar import SidecarService

    class Mod:
        PERF_CONTRACT = 1
        PERF_ROWS = (dict(key="tick_rate", label="驾驶拍率", kind="rate",
                          stat="p50_gap_inverse", category="player",
                          precision=1),)

        def read_perf_snapshot(self):
            return {"tick_rate": 20.0}

    class Boom(Mod):
        def read_perf_snapshot(self):
            raise RuntimeError("模块内部炸了")

    class Ctl:
        active_module = None

    h = SidecarService.__new__(SidecarService)
    h._controller = Ctl()
    h._perf_unknown_warned = set()
    read = h._make_perf_reader()

    assert read() == []                          # 没有活动模块
    Ctl.active_module = Mod()
    rows = read()
    assert rows[0]["display"] == "20.0 次/秒" and rows[0]["level"] == "ok"

    Ctl.active_module = Boom()
    assert read()[0]["reason"] == "read_error"   # 违约就地转行，不抛给采样线程


def test_cost_ms_resolution_is_sub_millisecond(tmp_path):
    """回归锁：各面耗时必须用高精度时钟。

    Windows 上 `time.monotonic()` 步长约 15.6ms，用它测亚毫秒级的各面耗时只会
    得到 0 或台阶值（首局实测 cost 全 0），「这一拍慢在哪一面」就失效了。
    """
    s = make_sampler()
    s.on_session_open(tmp_path, "t_1")
    costs = [s.tick_once()["cost_ms"] for _ in range(5)]
    fine = [v for c in costs for k, v in c.items()
            if k != "total" and 0 < v < 15.0]
    assert fine, f"没有任何一面落在亚毫秒区间，计时精度可疑: {costs}"
    s.on_session_closing(time.monotonic() + 1)
    s.stop()


# ---------------------------------------------------------------- 断档可见


def test_snapshot_reports_stall_only_while_running():
    """验收 13：采样停摆必须由外部观察者可见。

    卡住的线程喊不出「我卡住了」，所以判据是快照的龄期；而且只在「本该在采」
    时判——会话关闭后不再更新是正常的，不能报停摆。
    """
    import threading

    s = make_sampler()
    s._interval_s = 0.5                              # 断档线 = 3 拍 = 1.5s
    s.tick_once()
    assert s.snapshot()["stalled"] is False          # 刚采过

    s._latest["ts_mono_ns"] -= int(5e9)              # 假装 5 秒没更新
    assert s.snapshot()["stalled"] is False          # 没在跑 → 不判停摆

    s._thread = threading.current_thread()           # 假装采样线程还在跑
    snap = s.snapshot()
    assert snap["stalled"] is True
    assert snap["age_s"] >= 5.0

    # 恢复后的第一条样本必须自带缺口标记：时间轴有洞就得自己说，
    # 否则事后按样本号等距推算会得出「那段时间很平稳」的错误结论
    s._last_tick_mono -= 4.0
    recovered = s.tick_once()
    assert recovered["gap_ms"] >= 4000.0
    assert "gap_ms" not in s.tick_once()             # 正常节奏不标


def test_payload_carries_stall_flag():
    """停摆标志进载荷：读者据它判断数字可信度，而不是把陈旧值当实时值。"""
    snap = {"available": True, "sample_id": 3, "domain": ROWS,
            "stalled": True, "age_s": 7.5,
            "procs": [{"pid": 1, "name": "self", "cpu_pct": 20.0}]}
    p = build_perf_payload(ROWS, snap)
    assert p["sampler_stalled"] is True and p["sample_age_s"] == 7.5


def test_sidecar_reports_stall_once_per_episode(monkeypatch):
    """外部观察者每段断档只报一次（250ms 轮询不能刷屏），恢复时补一条。"""
    from maaracing_master.core import sidecar as sc

    svc = sc.SidecarService.__new__(sc.SidecarService)
    svc._perf_stalled = False
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(sc.logger, "log",
                        lambda msg, level="INFO": logs.append((level, msg)))

    svc._check_sampler_stall({"stalled": True, "age_s": 6.2})
    svc._check_sampler_stall({"stalled": True, "age_s": 7.2})
    assert len([x for x in logs if x[0] == "WARNING"]) == 1
    assert "6.2s" in logs[0][1]

    svc._check_sampler_stall({"stalled": False})
    assert logs[-1][0] == "INFO" and "恢复" in logs[-1][1]

    svc._check_sampler_stall({"stalled": True, "age_s": 4.0})
    assert len([x for x in logs if x[0] == "WARNING"]) == 2


def test_payload_aggregation_is_cheap():
    """验收 8：250ms 一次聚合是纯字典运算，单次必须远低于 1ms。

    超了说明有人在聚合里做 IO 或重新计算——那会拖住 4Hz 的状态轮询。
    """
    import statistics

    snap = {"available": True, "sample_id": 9, "cpu_pct": 20.0,
            "stalled": False, "age_s": 0.1, "domain": ROWS,
            "procs": [{"pid": 1, "name": "self", "cpu_pct": 300.0}],
            "gpu": {"status": "ok", "util_pct": 30}, "mem_pct": 50.0,
            "mem_used_gb": 8.0, "mem_total_gb": 16.0}
    costs = []
    for _ in range(300):
        t0 = time.perf_counter()
        build_perf_payload(ROWS, snap)
        costs.append((time.perf_counter() - t0) * 1000.0)
    assert statistics.median(costs) < 1.0, f"聚合中位耗时 {statistics.median(costs):.3f}ms"
