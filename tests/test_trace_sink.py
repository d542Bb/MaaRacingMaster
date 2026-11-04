# -*- coding: utf-8 -*-
"""玩法流水汇契约单测（记录流三分方案 §5.1）。

逐条锁死：序号作用域（跨 run_id 不重置、会话切换归零）、预留状态机全表
（三类拒收 + 队满丢旧带内空洞）、非连续丢弃拆分 gap、gap 逻辑序定位、
两类控制消息（屏障不留假缺口、保护通道满员合并）、生命周期屏障可投递、
关闭超时终态与三态文件尾、硬杀无标记判定、同秒开关 token 隔离、
logger 会话生命周期协议（回调在记录锁外、关闭状态记账、开合串行）。
"""
import json
import threading
import time
from pathlib import Path

import pytest

from maaracing_master.core.logger import Logger
from maaracing_master.core.trace_sink import (
    TraceSink,
    TraceSinkManager,
    validate_trace_file,
)


# ---------- 测试辅助 ----------

class _GateWriter:
    """受控行写入器：gate 关闭时 write 阻塞；fail_on 子串命中注入 OSError。"""

    def __init__(self, path):
        self.path = Path(path)
        self.lines: list[str] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail_on: str | None = None
        self.closed = False

    def write(self, line: str) -> int:
        if not self.gate.is_set():
            self.gate.wait(5.0)
        if self.fail_on is not None and self.fail_on in line:
            raise OSError("injected write failure")
        self.lines.append(line)
        return len(line)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def dump(self) -> None:
        """把记录的行落到真实文件（供离线校验器读取）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture
def sink(tmp_path):
    """真实文件写手的 sink；用例结束兜底收会话，防 worker 泄漏。"""
    s = TraceSink("demo")
    s.bind_session(tmp_path, "20260101_000000_1")
    yield s
    s.close_session(2.0)


@pytest.fixture
def path_of(tmp_path):
    return tmp_path / "trace" / "demo.jsonl"


# ---------- A. 序号作用域与基础协议 ----------

def test_happy_path_protocol(sink, path_of):
    sink.set_run_id("run-A")
    assert sink.begin_stage("p1").status == "accepted"
    assert sink.append({"fid": 7, "x": 1}).status == "accepted"
    assert sink.append({"x": 2}).status == "accepted"
    assert sink.end_stage("p1").status == "accepted"
    fl = sink.flush(2.0)
    assert fl.confirmed == 4 and not fl.timed_out and not fl.write_failed
    res = sink.close_session(2.0)
    assert res.ok and res.tail == "end"
    rows = _read(path_of)
    assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4, 5]
    assert rows[0]["type"] == "session_begin" and rows[0]["token"] == "20260101_000000_1"
    assert rows[1]["type"] == "stage_begin" and rows[1]["stage_id"] == "p1"
    assert "type" not in rows[2]            # 数据行不带 type
    assert rows[2]["fid"] == 7              # 行内 fid 被采用
    assert rows[2]["stage_id"] == "p1"      # 阶段随行
    assert rows[2]["run_id"] == "run-A"
    assert rows[2]["ts_mono_ns"] > 0 and rows[2]["ts_wall_ns"] > 0
    assert "type" not in rows[3]                  # 数据行不带 type
    assert rows[4]["type"] == "stage_end"
    assert rows[5]["type"] == "end"
    assert validate_trace_file(path_of) == []


def test_run_id_change_keeps_seq(sink, path_of):
    sink.set_run_id("run-A")
    sink.append({"x": 1})
    sink.set_run_id("run-B")
    sink.append({"x": 2})
    sink.flush(2.0)
    sink.close_session(2.0)
    rows = _read(path_of)
    data = [r for r in rows if "type" not in r]
    assert [r["seq"] for r in data] == [1, 2]      # 跨 run_id 序号连续
    assert [r["run_id"] for r in data] == ["run-A", "run-B"]
    assert validate_trace_file(path_of) == []


def test_session_switch_resets_seq(sink, tmp_path, path_of):
    sink.append({"x": 1})
    sink.close_session(2.0)
    first = _read(path_of)
    assert first[-1]["type"] == "end"
    sink.bind_session(tmp_path / "s2", "20260101_000500_2")
    sink.append({"x": 2})
    sink.close_session(2.0)
    second = _read(tmp_path / "s2" / "trace" / "demo.jsonl")
    seqs = [r["seq"] for r in second]
    assert seqs == [0, 1, 2]     # 归零重计：session_begin(0) + 数据(1) + end 标记(2)
    assert [r["seq"] for r in second if "type" not in r] == [1]
    assert validate_trace_file(tmp_path / "s2" / "trace" / "demo.jsonl") == []


def test_append_without_session_rejected():
    s = TraceSink("lonely")
    res = s.append({"x": 1})
    assert res.status == "rejected_no_session" and res.seq is None
    assert s.stats()["counters"]["rejected_no_session"] == 1


# ---------- B. 预留状态机全表 ----------

def test_oversize_rejected_as_gap(tmp_path):
    s = TraceSink("demo", max_line_chars=1000)
    s.bind_session(tmp_path, "t_1")
    big = s.append({"blob": "x" * 5000})
    assert big.status == "rejected_oversize" and big.seq == 1
    ok = s.append({"x": 1})
    assert ok.status == "accepted" and ok.seq == 2
    s.flush(2.0)
    s.close_session(2.0)
    rows = _read(tmp_path / "trace" / "demo.jsonl")
    gap = next(r for r in rows if r.get("type") == "gap")
    assert gap["from"] == 1 and gap["to"] == 1
    i_gap = rows.index(gap)
    i_data = next(i for i, r in enumerate(rows) if "type" not in r)
    assert i_gap < i_data                         # gap 在幸存记录之前
    assert validate_trace_file(tmp_path / "trace" / "demo.jsonl") == []


def test_queue_full_reject_new(tmp_path):
    w = _GateWriter(tmp_path / "t.jsonl")
    w.gate.clear()                                # 卡住 worker，先攒满队列
    s = TraceSink("demo", queue_cap=2, overflow="reject_new",
                  writer_factory=lambda p: w)
    s.bind_session(tmp_path, "t_1")
    assert s.append({"x": 1}).status == "accepted"
    assert s.append({"x": 2}).status == "accepted"
    rej = s.append({"x": 3})
    assert rej.status == "rejected_queue_full" and rej.seq == 3
    w.gate.set()
    fl = s.flush(3.0)
    assert fl.confirmed >= 3
    s.close_session(2.0)
    w.dump()
    gap = next(r for r in _read(w.path) if r.get("type") == "gap")
    assert gap["from"] == 3 and gap["to"] == 3
    assert validate_trace_file(w.path) == []


def test_drop_oldest_in_band_gap(tmp_path):
    w = _GateWriter(tmp_path / "t.jsonl")
    w.gate.clear()
    s = TraceSink("demo", queue_cap=2, overflow="drop_oldest",
                  writer_factory=lambda p: w)
    s.bind_session(tmp_path, "t_1")
    for i in range(1, 5):
        assert s.append({"x": i}).status == "accepted"
    w.gate.set()
    s.close_session(3.0)
    w.dump()
    rows = _read(w.path)
    data = [r for r in rows if "type" not in r]
    assert [r["seq"] for r in data] == [3, 4]     # 丢旧：1、2 被挤掉
    gap = next(r for r in rows if r.get("type") == "gap")
    assert (gap["from"], gap["to"]) == (1, 2)
    assert rows.index(gap) < rows.index(data[0])  # gap 在幸存记录之前
    assert validate_trace_file(w.path) == []


def test_noncontiguous_drops_split_gaps(tmp_path):
    s = TraceSink("demo", max_line_chars=1000)
    s.bind_session(tmp_path, "t_1")
    r1 = s.append({"blob": "y" * 5000})           # seq 1 拒收 → gap [1,1]
    assert r1.status == "rejected_oversize"
    assert s.append({"x": 1}).seq == 2
    assert s.append({"x": 2}).seq == 3
    r4 = s.append({"blob": "z" * 5000})           # seq 4 拒收 → gap [4,4]
    assert r4.status == "rejected_oversize"
    s.flush(2.0)
    s.close_session(2.0)
    rows = _read(tmp_path / "trace" / "demo.jsonl")
    gaps = [r for r in rows if r.get("type") == "gap"]
    assert [(g["from"], g["to"]) for g in gaps] == [(1, 1), (4, 4)]  # 拆开，不吞 2、3
    data = [r for r in rows if "type" not in r]
    assert [r["seq"] for r in data] == [2, 3]     # 幸存者不在任何 gap 内
    assert rows.index(gaps[0]) < rows.index(data[0]) < rows.index(gaps[1])
    assert validate_trace_file(tmp_path / "trace" / "demo.jsonl") == []


# ---------- C. 两类控制消息与屏障 ----------

def test_flush_barrier_leaves_no_fake_gap(sink, path_of):
    sink.append({"x": 1})
    fl1 = sink.flush(2.0)
    assert fl1.confirmed == 1 and fl1.written == 1 and fl1.dropped == []
    n_after_first = len(_read(path_of))
    fl2 = sink.flush(2.0)                          # 无新内容的屏障
    assert fl2.written == 0 and fl2.confirmed == 1
    assert len(_read(path_of)) == n_after_first    # 屏障不写文件、不占序号
    rows = _read(path_of)
    assert not any(r.get("type") == "gap" for r in rows)
    sink.close_session(2.0)
    assert validate_trace_file(path_of) == []


def test_flush_reports_drops_since_last(tmp_path):
    s = TraceSink("demo", max_line_chars=1000)
    s.bind_session(tmp_path, "t_1")
    s.append({"x": 1})
    s.flush(2.0)
    rej = s.append({"blob": "q" * 5000})
    assert rej.status == "rejected_oversize"
    fl = s.flush(2.0)
    assert fl.dropped == [(rej.seq, rej.seq)]     # 本批新增丢弃区间
    s.close_session(2.0)


def test_protected_full_merges_switch(tmp_path):
    w = _GateWriter(tmp_path / "t.jsonl")
    w.gate.clear()
    s = TraceSink("demo", protected_cap=3, writer_factory=lambda p: w)
    s.bind_session(tmp_path, "t_1")
    s.begin_stage("A")                              # seq 1
    s.end_stage("A")                                # seq 2
    s.begin_stage("B")                              # seq 3 → 保护通道满
    r4 = s.end_stage("B")                           # seq 4：满员 → 合并 end→begin
    assert r4.status == "accepted"
    w.gate.set()
    s.close_session(3.0)
    w.dump()
    assert s.stats()["counters"]["merged_switches"] == 1
    rows = _read(w.path)
    sw = next(r for r in rows if r.get("type") == "stage_switch")
    assert sw["seq"] == 2 and sw["seq_to"] == 3     # 单条覆盖两个序号
    assert sw["from_stage"] == "A" and sw["to_stage"] == "B"
    assert validate_trace_file(w.path) == []


def test_close_delivered_with_full_channels(tmp_path):
    """生命周期屏障不走队列：保护通道与数据队列双满时关闭照样收尾。"""
    w = _GateWriter(tmp_path / "t.jsonl")
    w.gate.clear()
    s = TraceSink("demo", queue_cap=2, protected_cap=2,
                  writer_factory=lambda p: w)
    s.bind_session(tmp_path, "t_1")
    s.begin_stage("A")
    s.end_stage("A")                                # 保护通道满
    s.append({"x": 1})
    s.append({"x": 2})
    s.append({"x": 3})                              # 数据队列满（丢旧）
    w.gate.set()                                    # 放行后立即关闭
    res = s.close_session(3.0)
    assert res.ok and res.tail == "end"             # 不被满员通道拒绝
    w.dump()
    assert validate_trace_file(w.path) == []


# ---------- D. 关闭终态与三态文件尾 ----------

def test_close_timeout_terminal_no_revive(tmp_path):
    w = _GateWriter(tmp_path / "t.jsonl")
    w.gate.clear()                                  # 永久卡死
    s = TraceSink("demo", close_grace_s=0.2, writer_factory=lambda p: w)
    s.bind_session(tmp_path, "t_1")
    s.append({"x": 1})
    res = s.close_session(0.3)
    assert res.state == "timeout" and res.tail == "absent"
    after = s.append({"x": 2})
    assert after.status == "rejected_no_session"    # 终态不复活
    # 新会话是新文件：正常可用
    s.bind_session(tmp_path, "t_2")
    assert s.append({"x": 3}).status == "accepted"
    w.gate.set()
    assert s.close_session(2.0).ok


def test_io_error_close_incomplete_tail(tmp_path):
    w = _GateWriter(tmp_path / "t.jsonl")
    s = TraceSink("demo", writer_factory=lambda p: w)
    s.bind_session(tmp_path, "t_1")
    s.append({"x": 1})
    s.flush(2.0)
    w.fail_on = '"type":"end"'                      # 只让 end 标记写失败
    res = s.close_session(2.0)
    assert res.state == "io_error" and res.tail == "close_incomplete"
    w.dump()
    rows = _read(w.path)
    ci = rows[-1]
    assert ci["type"] == "close_incomplete"
    assert (ci["seq"] - 1, ci["seq"] - 1) == tuple(res.unwritten[0])  # end 预留序号进了统计
    assert validate_trace_file(w.path) == []


def test_hardkill_tail_without_marker_is_valid(tmp_path):
    """硬杀（无收尾标记）：最后写入行之后的损失不判违规，内部缺口才判。"""
    p = tmp_path / "hard.jsonl"
    lines = [
        {"v": 1, "type": "session_begin", "seq": 0},
        {"v": 1, "seq": 1, "x": 1},
        {"v": 1, "seq": 2, "x": 2},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                 encoding="utf-8")
    assert validate_trace_file(p) == []
    broken = lines[:1] + [lines[2]]                 # 抽走 seq 1：内部缺口
    p.write_text("\n".join(json.dumps(x) for x in broken) + "\n",
                 encoding="utf-8")
    errs = validate_trace_file(p)
    assert len(errs) == 1 and "缺失序号" in errs[0]


# ---------- E. 离线校验器负例 ----------

def _write_rows(p, rows):
    p.write_text("\n".join(json.dumps(x) for x in rows) + "\n",
                 encoding="utf-8")


def test_validator_catches_survivor_inside_gap(tmp_path):
    p = tmp_path / "a.jsonl"
    _write_rows(p, [
        {"v": 1, "type": "session_begin", "seq": 0},
        {"v": 1, "seq": 1},
        {"v": 1, "type": "gap", "from": 2, "to": 3},
        {"v": 1, "seq": 3},                         # 幸存序号落进 gap
    ])
    assert any("相交" in e for e in validate_trace_file(p))


def test_validator_catches_disorder_and_gap_position(tmp_path):
    p = tmp_path / "b.jsonl"
    _write_rows(p, [
        {"v": 1, "type": "session_begin", "seq": 0},
        {"v": 1, "seq": 2},
        {"v": 1, "type": "gap", "from": 1, "to": 1},  # 定位在幸存 2 之后
        {"v": 1, "seq": 3},
    ])
    errs = validate_trace_file(p)
    assert any("未落在前一幸存记录" in e for e in errs)


def test_validator_catches_out_of_order_seq(tmp_path):
    p = tmp_path / "c.jsonl"
    _write_rows(p, [
        {"v": 1, "type": "session_begin", "seq": 0},
        {"v": 1, "seq": 2},
        {"v": 1, "seq": 1},
    ])
    assert any("非严格递增" in e for e in validate_trace_file(p))


def test_validator_catches_end_not_last_and_bad_head(tmp_path):
    p = tmp_path / "d.jsonl"
    _write_rows(p, [
        {"v": 1, "seq": 1},                          # 首行不是 session_begin
        {"v": 1, "type": "end", "seq": 2},
        {"v": 1, "seq": 3},                          # end 不在末尾
    ])
    errs = validate_trace_file(p)
    assert any("首行" in e for e in errs)
    assert any("不在文件末尾" in e for e in errs)


# ---------- F. logger 会话生命周期协议 ----------

class _Recorder:
    name = "recorder"

    def __init__(self, lg: Logger):
        self.lg = lg
        self.events: list[str] = []
        self.block_evt = threading.Event()
        self.entered = threading.Event()

    def on_session_open(self, session_dir, token):
        self.events.append(f"open:{token}")

    def on_session_closing(self, deadline):
        self.entered.set()
        self.block_evt.wait(5.0)                     # 测试可控的收尾阻塞
        self.lg.log("recorder closing diagnostics", "DEBUG")  # 记录锁外可安全 log
        return type("R", (), {"ok": True, "details": {}})()


def test_manager_follows_logger_session(tmp_path):
    lg = Logger(tmp_path)
    mgr = TraceSinkManager()
    lg.add_session_listener(mgr)
    sink = mgr.get("speedrush")
    lg.set_file_logging(True)
    assert sink.stats()["token"] == lg.session_token
    sink.set_run_id("game-1")
    sink.begin_stage(1)
    sink.append({"fid": 10, "pace_body": 33.0})
    lg.set_file_logging(False)
    p = tmp_path / lg.last_session_close_status[-1]["token"] / "trace" / "speedrush.jsonl"
    rows = _read(p)
    assert rows[0]["type"] == "session_begin"
    assert any("type" not in r and r.get("pace_body") == 33.0 for r in rows)
    assert rows[-1]["type"] == "end"
    assert validate_trace_file(p) == []
    assert lg.last_session_close_status[-1]["failed"] == []
    assert lg.lifecycle_state == "closed"


def test_listener_logging_during_close_no_deadlock(tmp_path):
    lg = Logger(tmp_path)
    rec = _Recorder(lg)
    rec.block_evt.set()                              # 不阻塞，只验证锁序
    lg.add_session_listener(rec)
    lg.set_file_logging(True)
    lg.set_file_logging(False)
    assert rec.events and rec.events[-1].startswith("open:")
    assert lg.lifecycle_state == "closed"


def test_same_second_reopen_isolates_tokens(tmp_path):
    lg = Logger(tmp_path)
    mgr = TraceSinkManager()
    lg.add_session_listener(mgr)
    sink = mgr.get("demo")
    lg.set_file_logging(True)
    tok1 = lg.session_token
    sink.append({"x": 1})
    lg.set_file_logging(True)                        # 同秒再开 = 换会话
    tok2 = lg.session_token
    assert tok1 != tok2
    assert (tmp_path / tok1).is_dir() and (tmp_path / tok2).is_dir()
    # 旧会话经完整关闭流程：文件有 end 标记
    rows1 = _read(tmp_path / tok1 / "trace" / "demo.jsonl")
    assert rows1[-1]["type"] == "end"
    lg.set_file_logging(False)


def test_open_during_closing_waits(tmp_path):
    lg = Logger(tmp_path)
    rec = _Recorder(lg)
    lg.add_session_listener(rec)
    lg.set_file_logging(True)
    closing_token = lg.session_token
    t_close = threading.Thread(target=lambda: lg.set_file_logging(False))
    t_close.start()
    assert rec.entered.wait(2.0)                     # 关闭进行中（CLOSING）
    t_open = threading.Thread(target=lambda: lg.set_file_logging(True))
    t_open.start()
    time.sleep(0.15)
    assert t_open.is_alive()                         # 开启必须等关闭完成
    rec.block_evt.set()
    t_close.join(3.0)
    t_open.join(3.0)
    assert not t_open.is_alive()
    assert lg.lifecycle_state == "open"
    assert lg.session_token != closing_token
    lg.set_file_logging(False)


