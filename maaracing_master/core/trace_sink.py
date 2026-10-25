#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩法流水汇——模块声明的异步 JSONL 写手（记录流三分方案的通用汇）。

一个模块一份声明（`get_trace_sink("<模块名>")`），落点随日志会话目录走：
`logs/<会话>/trace/<模块>.jsonl`。落盘开关 = 日志记录开关（经 logger 的会话
生命周期回调驱动绑定与收尾），模块不感知文件与目录。

契约要点（行为以本模块 + 单测为准）：
- **序号作用域**：`seq` 在每模块文件的会话生命周期内单调递增，从 0 起；
  `run_id` 是身份字段不参与排序，变更不重置序号；会话切换（换文件）归零。
- **先预留后判定**：提交进入有效会话接收流程即预留序号，再判定入队/拒收。
  超大行、队满拒收的序号进待记空洞；队满丢旧保留原序号由带内空洞标记表达。
- **空洞（gap）语义**：标记只覆盖实际丢失且序号连续的极大区间，被幸存记录
  隔开的丢弃必须拆成多个 gap；文件内按逻辑序号归并输出（gap 物理位置 =
  区间内最小丢失序号之前、最近的幸存记录之后）。
- **两类控制消息**：可回放控制记录（会话边界/阶段开始/结束/切换）写文件、
  消耗序号、走有界保护通道（满员时合并相邻「结束→开始」或显式拒收）；纯同步
  屏障（flush/close）不写文件、不消耗序号，只携带完成语义。
- **生命周期屏障不是队列消息**：会话绑定与关闭以原子状态 + 条件变量握手，
  不可被拒收、不占通道容量——logger 永不等待一个可能投递不出去的关闭事件。
- **关闭失败终态化**：排空期限到或 IO 失败即终态，不复活、不后台重试；未完成
  信息经关闭返回值交给调用方；文件尾三态（end / close_incomplete / 无标记），
  硬杀损失范围 = 最后写入位置 + 未落队列。

行协议公共头：`v` / `mod` / `run_id` / `stage_id` / `seq` / `fid` / `ts_wall_ns`
（墙钟，对表用，受校时影响）/ `ts_mono_ns`（单调钟，算间隔用）；双时钟不混用。
数据行不写 `type` 键；控制行带 `type`（session_begin / stage_begin / stage_end /
stage_switch / end）；gap 行 `{"type":"gap","from":a,"to":b}` 与尽力收尾行
`close_incomplete`（附统计、不占序号）不消耗序号。业务行的保留键
（`v`/`mod`/…）会被丢弃防串位，其余键原样透传。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

__all__ = [
    "AppendResult", "FlushResult", "CloseResult", "SinkCloseSummary",
    "TraceSink", "TraceSinkManager", "get_trace_sink", "validate_trace_file",
]

PROTOCOL_VERSION = 1

# 业务行合并进协议头时被丢弃的保留键（fid 单独处理：行内提供即采用）。
RESERVED_KEYS = frozenset({
    "v", "mod", "run_id", "stage_id", "seq", "seq_to",
    "ts_wall_ns", "ts_mono_ns", "type", "token",
})

_MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _json_fallback(value: Any) -> str:
    """json.dumps 的兜底转换：不可序列化值转字符串，不让一行拖垮热路径。"""
    return str(value)


def _merge_seq_list(seqs: list[int]) -> list[tuple[int, int]]:
    """有序化并把连续序号合并成区间（诊断输出用）。"""
    out: list[tuple[int, int]] = []
    for s in sorted(set(seqs)):
        if out and s == out[-1][1] + 1:
            out[-1] = (out[-1][0], s)
        else:
            out.append((s, s))
    return out


@dataclass(frozen=True)
class AppendResult:
    """append/阶段控制记录的提交结果（预留状态机全表）。"""
    status: str     # accepted / rejected_oversize / rejected_queue_full / rejected_no_session
    seq: int | None  # 会话作用域内预留的序号（rejected_no_session 无预留）


@dataclass(frozen=True)
class FlushResult:
    """纯同步屏障结果：不写文件、不消耗序号。"""
    confirmed: int                 # 屏障达成时已确认序号（已写行或已落 gap 标记）
    written: int                   # 本次屏障期间新写条数
    dropped: list[tuple[int, int]]     # 本批丢弃区间（上次 flush 后新增）
    timed_out: list[tuple[int, int]]   # 超时未落区间（目标序号内未确认的入队记录）
    write_failed: bool


@dataclass(frozen=True)
class CloseResult:
    """会话关闭结果（终态，经返回值交接给 logger 记账）。"""
    state: str      # ok / timeout / io_error / no_session
    tail: str       # end / close_incomplete / absent
    token: str | None
    written: int
    dropped: list[tuple[int, int]] = field(default_factory=list)
    unwritten: list[tuple[int, int]] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"


@dataclass(frozen=True)
class SinkCloseSummary:
    """管理器汇总的关闭结果（logger 按此记账，不认识单个 sink 类型）。"""
    ok: bool
    details: dict[str, CloseResult]


class _Entry:
    """队列条目：一行待写内容（数据行或可回放控制记录）。"""
    __slots__ = ("seq", "seq_to", "line", "etype", "stage")

    def __init__(self, seq: int, seq_to: int | None, line: str,
                 etype: str, stage: Any):
        self.seq = seq
        self.seq_to = seq_to      # 合并的 stage_switch 覆盖 [seq, seq_to]；其余 None
        self.line = line
        self.etype = etype        # data / session_begin / stage_begin / stage_end / stage_switch
        self.stage = stage

    @property
    def last_seq(self) -> int:
        return self.seq_to if self.seq_to is not None else self.seq


def _gap_line(module: str, a: int, b: int) -> str:
    return json.dumps(
        {"v": PROTOCOL_VERSION, "type": "gap", "mod": module, "from": a, "to": b,
         "ts_wall_ns": time.time_ns(), "ts_mono_ns": time.monotonic_ns()},
        ensure_ascii=False, separators=(",", ":"))


class _Session:
    """一次会话绑定内的全部状态（TraceSink._lk 保护）。

    文件句柄由 worker 线程独占；其余线程只操作队列与计数。
    """
    __slots__ = ("token", "path", "writer", "queue", "protected", "pending_drops",
                 "dropped_raw", "next_seq", "confirmed", "written_count",
                 "close_requested", "close_deadline", "terminal", "result",
                 "write_error", "flush_drop_mark")

    def __init__(self, token: str, path: Path, writer: TextIO):
        self.token = token
        self.path = path
        self.writer = writer
        self.queue: deque[_Entry] = deque()
        self.protected: deque[_Entry] = deque()
        self.pending_drops: list[list[int]] = []   # 升序、互不重叠、已合并连续
        self.dropped_raw: list[int] = []           # 逐序号丢弃流水（flush/收尾统计用）
        self.next_seq = 1                          # seq 0 预留给 session_begin
        self.confirmed = -1                        # ≤该序号的记录已写行或已落 gap
        self.written_count = 0
        self.close_requested = False
        self.close_deadline: float | None = None   # monotonic 绝对时刻
        self.terminal = False
        self.result: CloseResult | None = None
        self.write_error: str | None = None
        self.flush_drop_mark = 0                    # 上次屏障完成时的丢弃水位

    @property
    def dropped_merged(self) -> list[tuple[int, int]]:
        return _merge_seq_list(self.dropped_raw)


class TraceSink:
    """单模块流水汇：模块面 begin_stage / append / end_stage / flush。

    close_session 不在模块调用面——由会话生命周期屏障（logger → 管理器）驱动。
    """

    def __init__(self, module: str, *, queue_cap: int = 512,
                 protected_cap: int = 8, overflow: str = "drop_oldest",
                 max_line_chars: int = 65536, close_grace_s: float = 0.5,
                 writer_factory: Callable[[Path], TextIO] | None = None):
        if not _MODULE_NAME_RE.match(module or ""):
            raise ValueError(f"模块名不合规: {module!r}（限字母数字_-，≤32 字符）")
        if overflow not in ("drop_oldest", "reject_new"):
            raise ValueError(f"overflow 策略不合规: {overflow!r}")
        self.module = module
        self._queue_cap = queue_cap
        self._protected_cap = protected_cap
        self._overflow = overflow
        self._max_line_chars = max_line_chars
        self._close_grace_s = close_grace_s
        self._writer_factory = writer_factory
        self._run_id: str | None = None
        self._stage_id: Any = None
        self._lk = threading.Lock()
        self._cond = threading.Condition(self._lk)
        self._session: _Session | None = None
        self._bind_error: str | None = None
        self._counters = {"accepted": 0, "rejected_oversize": 0,
                          "rejected_queue_full": 0, "rejected_no_session": 0,
                          "merged_switches": 0}

    # ---------- 模块调用面 ----------

    def set_run_id(self, run_id: str | None) -> None:
        """当前运行身份（行头字段，不参与排序、不重置序号）。"""
        self._run_id = run_id

    def begin_stage(self, stage_id: Any) -> AppendResult:
        """阶段开始：可回放控制记录（受保护通道）。"""
        res = self._submit_control("stage_begin", stage_id)
        if res.status == "accepted":
            self._stage_id = stage_id
        return res

    def end_stage(self, stage_id: Any) -> AppendResult:
        """阶段结束：可回放控制记录（受保护通道）。"""
        res = self._submit_control("stage_end", stage_id)
        if res.status == "accepted":
            self._stage_id = None
        return res

    def append(self, row: dict, *, fid: Any = None) -> AppendResult:
        """提交一行数据（不做文件 I/O；序列化 + 尺寸判定 + 有界入队）。"""
        if not isinstance(row, dict):
            row = {"payload": str(row)}
        now_wall = time.time_ns()
        now_mono = time.monotonic_ns()
        with self._lk:
            sess = self._session
            if sess is None or sess.terminal or sess.close_requested:
                self._counters["rejected_no_session"] += 1
                return AppendResult("rejected_no_session", None)
            seq = sess.next_seq
            sess.next_seq += 1
            fid_eff = fid if fid is not None else row.get("fid")
            record = {"v": PROTOCOL_VERSION, "mod": self.module,
                      "run_id": self._run_id, "stage_id": self._stage_id,
                      "seq": seq, "fid": fid_eff,
                      "ts_wall_ns": now_wall, "ts_mono_ns": now_mono}
            for k, v in row.items():
                if k in RESERVED_KEYS or k == "fid":
                    continue
                record[k] = v
            line = json.dumps(record, ensure_ascii=False,
                              separators=(",", ":"), default=_json_fallback)
            if len(line) > self._max_line_chars:
                self._add_drop(sess, seq)
                self._counters["rejected_oversize"] += 1
                return AppendResult("rejected_oversize", seq)
            if self._overflow == "drop_oldest":
                while len(sess.queue) >= self._queue_cap:
                    old = sess.queue.popleft()
                    self._add_drop(sess, old.seq)
            elif len(sess.queue) >= self._queue_cap:
                self._add_drop(sess, seq)
                self._counters["rejected_queue_full"] += 1
                return AppendResult("rejected_queue_full", seq)
            sess.queue.append(_Entry(seq, None, line, "data", None))
            self._counters["accepted"] += 1
            self._cond.notify_all()
            return AppendResult("accepted", seq)

    def flush(self, timeout_s: float = 5.0) -> FlushResult:
        """纯同步屏障：等目标序号（提交时点已预留的全部）写行或落 gap 标记。"""
        deadline = time.monotonic() + timeout_s
        with self._lk:
            sess = self._session
            if sess is None:
                return FlushResult(-1, 0, [], [], False)
            if sess.terminal:
                return FlushResult(sess.confirmed, 0, [], [],
                                   sess.write_error is not None)
            target = sess.next_seq - 1
            mark_written = sess.written_count
            mark_drops = sess.flush_drop_mark    # 上次屏障完成以来的新增才算本批
            while (sess.confirmed < target and not sess.terminal
                   and sess.write_error is None):
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                self._cond.wait(remain)
            dropped = _merge_seq_list(sess.dropped_raw[mark_drops:])
            sess.flush_drop_mark = len(sess.dropped_raw)
            timed_out = []
            if sess.confirmed < target:
                timed_out = _merge_seq_list(
                    [e.seq for e in sess.queue if e.seq <= target]
                    + [e.seq for e in sess.protected if e.seq <= target])
            return FlushResult(sess.confirmed,
                               sess.written_count - mark_written,
                               dropped, timed_out,
                               sess.write_error is not None)

    def stats(self) -> dict:
        """诊断快照（测试与自检用）。"""
        with self._lk:
            sess = self._session
            return {
                "module": self.module,
                "token": sess.token if sess else None,
                "terminal": bool(sess and sess.terminal),
                "close_requested": bool(sess and sess.close_requested),
                "reserved": sess.next_seq if sess else None,
                "written": sess.written_count if sess else None,
                "confirmed": sess.confirmed if sess else None,
                "pending_drops": [tuple(iv) for iv in sess.pending_drops] if sess else [],
                "dropped_total": sess.dropped_merged if sess else [],
                "bind_error": self._bind_error,
                "counters": dict(self._counters),
            }

    # ---------- 会话生命周期（管理器调用面，非模块面） ----------

    def bind_session(self, session_dir: Path, token: str) -> None:
        """会话开启：建 trace/<模块>.jsonl 并起 worker（不可被拒绝）。"""
        with self._lk:
            if self._session is not None:
                # 上一会话未经关闭流程（异常路径）：终态化放弃，不复活
                old = self._session
                old.terminal = True
                self._session = None
        trace_dir = Path(session_dir) / "trace"
        path = trace_dir / f"{self.module}.jsonl"
        try:
            trace_dir.mkdir(parents=True, exist_ok=True)
            if self._writer_factory is not None:
                writer = self._writer_factory(path)
            else:
                writer = open(path, "a", encoding="utf-8", buffering=1)
        except OSError as exc:
            self._bind_error = repr(exc)
            return
        with self._lk:
            self._bind_error = None
            self._session = _Session(token, path, writer)
            sess = self._session
        threading.Thread(target=self._worker, args=(sess,),
                         daemon=True, name=f"trace-{self.module}").start()

    def close_session(self, timeout_s: float) -> CloseResult:
        """会话关闭屏障：请求排空 → 期限内收尾；超时/IO 失败即终态化。

        不可被拒绝、不占保护通道容量（状态握手而非队列消息）。
        """
        deadline = time.monotonic() + timeout_s + self._close_grace_s
        with self._lk:
            sess = self._session
            if sess is None:
                return CloseResult("no_session", "absent", None, 0)
            if sess.terminal and sess.result is not None:
                res = sess.result
                self._session = None
                return res
            sess.close_requested = True
            sess.close_deadline = time.monotonic() + timeout_s
            self._cond.notify_all()
            while sess.result is None and not sess.terminal:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                self._cond.wait(remain)
            if sess.result is not None:
                res = sess.result
                self._session = None
                return res
            if sess.terminal:       # worker 已终态化但未发布（防御分支）
                res = sess.result or CloseResult(
                    "io_error", "absent", sess.token, sess.written_count,
                    sess.dropped_merged, error=sess.write_error)
                self._session = None
                return res
            # worker 超出宽限仍未收尾（极罕见：卡死在 OS 调用）——终态化，
            # 不碰句柄（避免与 worker 竞写）；文件尾退化为无标记。
            sess.terminal = True
            unwritten = _merge_seq_list(
                [e.seq for e in sess.queue] + [e.seq for e in sess.protected])
            res = CloseResult("timeout", "absent", sess.token,
                              sess.written_count, sess.dropped_merged, unwritten)
            sess.result = res
            self._session = None
            self._cond.notify_all()
            return res

    # ---------- 内部 ----------

    def _submit_control(self, etype: str, stage_id: Any) -> AppendResult:
        now_wall = time.time_ns()
        now_mono = time.monotonic_ns()
        with self._lk:
            sess = self._session
            if sess is None or sess.terminal or sess.close_requested:
                self._counters["rejected_no_session"] += 1
                return AppendResult("rejected_no_session", None)
            seq = sess.next_seq
            sess.next_seq += 1
            line = json.dumps(
                {"v": PROTOCOL_VERSION, "type": etype, "mod": self.module,
                 "run_id": self._run_id, "stage_id": stage_id, "seq": seq,
                 "ts_wall_ns": now_wall, "ts_mono_ns": now_mono},
                ensure_ascii=False, separators=(",", ":"), default=_json_fallback)
            if len(sess.protected) < self._protected_cap:
                sess.protected.append(_Entry(seq, None, line, etype, stage_id))
                self._cond.notify_all()
                self._counters["accepted"] += 1
                return AppendResult("accepted", seq)
            # 满员：合并相邻「阶段结束→阶段开始」为单条 switch（腾一格）
            for i in range(len(sess.protected) - 1):
                a, b = sess.protected[i], sess.protected[i + 1]
                if a.etype == "stage_end" and b.etype == "stage_begin":
                    merged = json.dumps(
                        {"v": PROTOCOL_VERSION, "type": "stage_switch",
                         "mod": self.module, "run_id": self._run_id,
                         "from_stage": a.stage, "to_stage": b.stage,
                         "seq": a.seq, "seq_to": b.seq,
                         "ts_wall_ns": now_wall, "ts_mono_ns": now_mono},
                        ensure_ascii=False, separators=(",", ":"))
                    kept = [x for j, x in enumerate(sess.protected)
                            if j != i and j != i + 1]
                    kept[i:i] = [_Entry(a.seq, b.seq, merged, "stage_switch",
                                        (a.stage, b.stage))]
                    sess.protected.clear()
                    sess.protected.extend(kept)
                    sess.protected.append(_Entry(seq, None, line, etype, stage_id))
                    self._counters["merged_switches"] += 1
                    self._cond.notify_all()
                    return AppendResult("accepted", seq)
            # 合并不了：显式拒收，预留序号进空洞
            self._add_drop(sess, seq)
            self._counters["rejected_queue_full"] += 1
            return AppendResult("rejected_queue_full", seq)

    def _add_drop(self, sess: _Session, seq: int) -> None:
        """登记一个丢失序号（与相邻区间合并成极大连续区间）。"""
        sess.dropped_raw.append(seq)
        ivs = sess.pending_drops
        if not ivs:
            ivs.append([seq, seq])
            return
        for i, iv in enumerate(ivs):
            if iv[0] - 1 <= seq <= iv[1] + 1:
                iv[0] = min(iv[0], seq)
                iv[1] = max(iv[1], seq)
                self._merge_neighbor(ivs, i)
                return
            if seq < iv[0] - 1:
                ivs.insert(i, [seq, seq])
                self._merge_neighbor(ivs, i)
                return
        ivs.append([seq, seq])

    @staticmethod
    def _merge_neighbor(ivs: list[list[int]], i: int) -> None:
        if i > 0 and ivs[i - 1][1] + 1 >= ivs[i][0]:
            ivs[i][0] = ivs[i - 1][0]
            del ivs[i - 1]
            i -= 1
        if i + 1 < len(ivs) and ivs[i][1] + 1 >= ivs[i + 1][0]:
            ivs[i][1] = ivs[i + 1][1]
            del ivs[i + 1]

    def _worker(self, sess: _Session) -> None:
        """写线程：独占文件句柄；按逻辑序号归并写出数据、控制记录与 gap。"""
        begin_line = json.dumps(
            {"v": PROTOCOL_VERSION, "type": "session_begin", "mod": self.module,
             "token": sess.token, "seq": 0,
             "ts_wall_ns": time.time_ns(), "ts_mono_ns": time.monotonic_ns()},
            ensure_ascii=False, separators=(",", ":"))
        try:
            sess.writer.write(begin_line + "\n")
        except OSError as exc:
            self._terminal(sess, "io_error", exc, [])
            return
        while True:
            # 锁内：取走全部待写条目并按逻辑序号定序（gap 依幸存记录定位）
            with self._lk:
                while (not sess.queue and not sess.protected
                       and not sess.close_requested):
                    self._cond.wait(0.2)
                    if sess.terminal:
                        return
                batch: list[_Entry] = []
                while sess.queue and sess.protected:
                    if sess.queue[0].seq <= sess.protected[0].seq:
                        batch.append(sess.queue.popleft())
                    else:
                        batch.append(sess.protected.popleft())
                batch.extend(sess.queue)
                sess.queue.clear()
                batch.extend(sess.protected)
                sess.protected.clear()
                plan: list[tuple[str, Any]] = []
                ivs = sess.pending_drops
                idx = 0
                for entry in batch:
                    while idx < len(ivs) and ivs[idx][1] < entry.seq:
                        plan.append(("gap", tuple(ivs[idx])))
                        idx += 1
                    plan.append(("entry", entry))
                while idx < len(ivs):     # 批尾：本轮已知的全部丢弃区间
                    plan.append(("gap", tuple(ivs[idx])))
                    idx += 1
            # 锁外写出（writer 为 worker 独占）；期限/IO 失败按前缀落账后终态化
            written_entries = 0
            covered_max = -1
            for i, (kind, item) in enumerate(plan):
                if sess.terminal:
                    return
                if (sess.close_deadline is not None
                        and time.monotonic() > sess.close_deadline):
                    self._settle_cycle(sess, plan[:i], written_entries, covered_max)
                    self._terminal(sess, "timeout", None,
                                   [it.seq for k, it in plan[i:] if k == "entry"])
                    return
                try:
                    if kind == "gap":
                        sess.writer.write(_gap_line(self.module, item[0], item[1]) + "\n")
                        covered_max = max(covered_max, item[1])
                    else:
                        sess.writer.write(item.line + "\n")
                        written_entries += 1
                        covered_max = max(covered_max, item.last_seq)
                except OSError as exc:
                    self._settle_cycle(sess, plan[:i], written_entries, covered_max)
                    self._terminal(sess, "io_error", exc,
                                   [it.seq for k, it in plan[i:] if k == "entry"])
                    return
            self._settle_cycle(sess, plan, written_entries, covered_max)
            with self._lk:
                if (sess.close_requested and not sess.queue
                        and not sess.protected and not sess.terminal):
                    break               # 排空完成 → 收尾标记
        self._finish(sess)

    def _settle_cycle(self, sess: _Session, done: list[tuple[str, Any]],
                      written_entries: int, covered_max: int) -> None:
        """一轮写出的落账：清已写 gap 区间、推进 confirmed（条目数由调用方给）。"""
        with self._lk:
            if sess.terminal:
                return
            written_gaps = {tuple(g) for k, g in done if k == "gap"}
            sess.pending_drops = [iv for iv in sess.pending_drops
                                  if tuple(iv) not in written_gaps]
            sess.written_count += written_entries
            if covered_max >= 0:
                sess.confirmed = max(sess.confirmed, covered_max)
            elif not sess.pending_drops:
                # 空轮次：队列为空且无待落 gap，才允许推到「已预留即已落」
                sess.confirmed = max(sess.confirmed, sess.next_seq - 1)
            self._cond.notify_all()

    def _finish(self, sess: _Session) -> None:
        """正常收尾：写 end 标记（占一个序号）→ 关句柄 → 发布结果。"""
        with self._lk:
            end_seq = sess.next_seq
            sess.next_seq += 1
            dropped = sess.dropped_merged
        try:
            sess.writer.write(json.dumps(
                {"v": PROTOCOL_VERSION, "type": "end", "mod": self.module,
                 "token": sess.token, "seq": end_seq,
                 "written": sess.written_count, "dropped": [list(iv) for iv in dropped],
                 "ts_wall_ns": time.time_ns(), "ts_mono_ns": time.monotonic_ns()},
                ensure_ascii=False, separators=(",", ":")) + "\n")
            sess.writer.close()
            res = CloseResult("ok", "end", sess.token,
                              sess.written_count, dropped)
        except OSError as exc:
            # end 行写失败：已为其预留的序号进 unwritten 统计（终态不复活）
            self._terminal(sess, "io_error", exc, [end_seq])
            return
        self._publish(sess, res)

    def _terminal(self, sess: _Session, state: str, exc: Exception | None,
                  extra_unwritten_seqs: list[int]) -> None:
        """终态化：停止写入，尽力写 close_incomplete（写不出=无标记），发布结果。"""
        with self._lk:
            if sess.terminal:
                return
            if exc is not None:
                sess.write_error = repr(exc)
            unwritten_seqs = (extra_unwritten_seqs
                              + [e.seq for e in sess.queue]
                              + [e.seq for e in sess.protected])
            dropped = sess.dropped_merged
            unwritten = _merge_seq_list(unwritten_seqs)
            stats_line = json.dumps(
                {"v": PROTOCOL_VERSION, "type": "close_incomplete",
                 "mod": self.module, "token": sess.token, "reason": state,
                 "seq": sess.next_seq, "written": sess.written_count,
                 "dropped": [list(iv) for iv in dropped],
                 "unwritten": [list(iv) for iv in unwritten],
                 "ts_wall_ns": time.time_ns(), "ts_mono_ns": time.monotonic_ns()},
                ensure_ascii=False, separators=(",", ":"))
            sess.queue.clear()
            sess.protected.clear()
            sess.pending_drops = []
        tail = "close_incomplete"
        try:
            sess.writer.write(stats_line + "\n")
        except OSError:
            tail = "absent"
        try:
            sess.writer.close()
        except OSError:
            pass
        self._publish(sess, CloseResult(state, tail, sess.token,
                                        sess.written_count, dropped, unwritten,
                                        sess.write_error))

    def _publish(self, sess: _Session, res: CloseResult) -> None:
        with self._lk:
            sess.terminal = True
            sess.result = res
            sess.close_deadline = None
            self._cond.notify_all()


class TraceSinkManager:
    """进程内流水汇注册表 + logger 会话生命周期回调（协议见 core/logger.py）。"""

    name = "trace_sink"

    def __init__(self) -> None:
        self._sinks: dict[str, TraceSink] = {}
        self._lk = threading.Lock()
        self._bound = False

    def get(self, module: str, **sink_kwargs) -> TraceSink:
        with self._lk:
            sink = self._sinks.get(module)
            if sink is None:
                sink = TraceSink(module, **sink_kwargs)
                self._sinks[module] = sink
        self._ensure_bound()
        return sink

    def _ensure_bound(self) -> None:
        if self._bound:
            return
        from maaracing_master.core.logger import logger
        logger.add_session_listener(self)
        self._bound = True
        # 模块中途注册：当前已有会话则立即补绑
        if logger.session_dir is not None and logger.session_token:
            with self._lk:
                sinks = list(self._sinks.values())
            for s in sinks:
                s.bind_session(logger.session_dir, logger.session_token)

    # ---- 会话生命周期回调（logger 生命周期锁内、记录锁外执行） ----

    def on_session_open(self, session_dir: Path, token: str) -> None:
        with self._lk:
            sinks = list(self._sinks.values())
        for s in sinks:
            s.bind_session(session_dir, token)

    def on_session_closing(self, deadline_monotonic: float) -> SinkCloseSummary:
        with self._lk:
            sinks = list(self._sinks.values())
        details: dict[str, CloseResult] = {}
        for s in sinks:
            timeout = max(0.05, deadline_monotonic - time.monotonic())
            details[s.module] = s.close_session(timeout)
        return SinkCloseSummary(ok=all(r.ok for r in details.values()),
                                details=details)


_manager = TraceSinkManager()


def get_trace_sink(module: str, **sink_kwargs) -> TraceSink:
    """模块声明入口：取本模块的流水汇（首次调用即注册，随日志会话自动开合）。

    sink_kwargs 仅首次创建时生效。
    """
    return _manager.get(module, **sink_kwargs)


# --------------------------------------------------------------------------
# 离线校验器（空洞语义四规则的机检入口；探针与验收共用）
# --------------------------------------------------------------------------

def validate_trace_file(path: str | Path) -> list[str]:
    """校验一份流水文件的序号连续性与 gap 语义；返回违规清单（空 = 通过）。

    规则：① gap 区间内不得有幸存序号，幸存序号也不得落入任何 gap；
    ② 文件内序号严格递增（gap 行按逻辑序定位：前一行最大序号 < from）；
    ③ 缺失序号要么落入 gap、要么由 close_incomplete 统计豁免（无收尾标记时
    仅检查到最后写入序号——硬杀尾部损失属显式承诺，不算违规）；
    ④ 头尾放宽——首条数据 gap 无前驱要求，尾部以 end / close_incomplete /
    无标记三态界定。
    """
    errs: list[str] = []
    covered: list[tuple[int, int]] = []
    gaps: list[tuple[int, int]] = []
    prev_max: int | None = None
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    records.append(json.loads(ln))
                except json.JSONDecodeError as exc:
                    errs.append(f"无法解析的行: {exc}")
    except OSError as exc:
        return [f"文件不可读: {exc}"]

    for idx, rec in enumerate(records):
        rtype = rec.get("type")
        if rtype == "close_incomplete":
            continue        # 尽力收尾的统计注记，不占序号、不参与排序
        if rtype == "gap":
            a, b = rec.get("from"), rec.get("to")
            if not (isinstance(a, int) and isinstance(b, int) and a <= b):
                errs.append(f"第 {idx} 行 gap 区间非法: [{a},{b}]")
                continue
            if gaps and a <= gaps[-1][1]:
                errs.append(f"第 {idx} 行 gap 与前区间重叠/乱序")
            if prev_max is not None and a <= prev_max:
                errs.append(f"第 {idx} 行 gap 起点 {a} 未落在前一幸存记录 {prev_max} 之后")
            gaps.append((a, b))
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int):
            errs.append(f"第 {idx} 行缺 seq")
            continue
        seq_to = rec.get("seq_to", seq)
        if not isinstance(seq_to, int) or seq_to < seq:
            errs.append(f"第 {idx} 行 seq_to 非法: {seq_to}")
            seq_to = seq
        if prev_max is not None and seq <= prev_max:
            errs.append(f"第 {idx} 行序号 {seq} 非严格递增（前 max={prev_max}）")
        prev_max = max(prev_max if prev_max is not None else -1, seq_to)
        covered.append((seq, seq_to))

    if records:
        first = records[0]
        if first.get("type") != "session_begin" or first.get("seq") != 0:
            errs.append("首行不是 session_begin(seq=0)")

    # 规则①：gap 与幸存序号互斥
    for a, b in gaps:
        for s, s2 in covered:
            if not (s2 < a or s > b):
                errs.append(f"幸存序号 {s}..{s2} 与 gap [{a},{b}] 相交")

    # 规则③：缺失序号归属
    last = records[-1] if records else {}
    last_type = last.get("type")
    known: set[int] = set()
    for s, s2 in covered:
        known.update(range(s, s2 + 1))
    for a, b in gaps:
        known.update(range(a, b + 1))
    hi = prev_max if prev_max is not None else -1
    if last_type == "end" and isinstance(last.get("seq"), int):
        hi = last["seq"]
    missing = [x for x in range(0, hi + 1) if x not in known]
    if missing:
        excused: set[int] = set()
        if last_type == "close_incomplete":
            for key in ("dropped", "unwritten"):
                for iv in (last.get(key) or []):
                    if isinstance(iv, list) and len(iv) == 2 \
                            and isinstance(iv[0], int) and isinstance(iv[1], int):
                        excused.update(range(iv[0], iv[1] + 1))
        bad = [x for x in missing if x not in excused]
        if bad:
            errs.append(f"缺失序号未由 gap/收尾统计覆盖: {_merge_seq_list(bad)[:8]}")
    # 尾部标记必须居尾
    for idx, rec in enumerate(records[:-1]):
        if rec.get("type") in ("end", "close_incomplete"):
            errs.append(f"第 {idx} 行尾部标记不在文件末尾")
    return errs
