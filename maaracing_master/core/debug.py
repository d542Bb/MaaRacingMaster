#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaaRacingMaster — 调试可视化模块
每帧截图标注：探测轮廓(黄) / 入围候选(绿) / 选中光标(红) / 按钮目标

两套渲染：
  • 存盘模式（enabled）→ 全量绘制，保存到磁盘
  • PEEP 模式（peep_enabled）→ 精简绘制，仅关键逻辑

共用辅助方法 _draw_* 统一绘制逻辑，lite=True 时跳过文字标注和边缘散点。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np
from pathlib import Path
from datetime import datetime

# 运行级调试流的节拍与容量（契约见 docs/plan/debug_peep_unification.md 5.4/5.9/5.10）
RUN_SAMPLE_HZ = 10.0
MAX_SAMPLE_HZ = 20.0
FRAME_QUEUE_MAX = 30          # 约 3 秒 @10Hz
CONTROL_HIGH_WATER = 4096     # 控制事件高水位：到达即判定 IO 线程失速
DRAIN_TIMEOUT_S = 1.0
JOIN_TIMEOUT_S = 2.0
PEEP_STALE_MS = 1500.0

_COUNTER_KEYS = (
    "frames_in", "frames_dropped_queue", "frames_processed",
    "frames_due_peep", "frames_due_save",
    "frames_not_due_peep", "frames_not_due_save",
    "peep_encoded", "peep_render_failed", "peep_encode_failed",
    "save_written", "save_render_failed", "save_write_failed",
    "pending_at_close", "rejected_at_close",
)


def _new_counters() -> dict:
    return {k: 0 for k in _COUNTER_KEYS}


def _safe_name(name: str, limit: int = 24) -> str:
    """段目录名：只保留字母数字下划线与中文，其余折成下划线。"""
    out = []
    for ch in str(name or "seg"):
        out.append(ch if (ch.isalnum() or ch == "_" or ord(ch) > 127) else "_")
    text = "".join(out).strip("_") or "seg"
    return text[:limit]


@dataclass
class _StreamItem:
    """调试流 FIFO 的一项：帧或控制事件。

    帧的段归属、阶段、开关状态都在**入队时**固定（契约 5.2/5.4），
    消费者不再回查全局状态。
    """

    kind: str                     # "frame" | "control"
    seq: int
    segment_id: int | None = None
    owner: str = ""
    label: str = ""
    stage: str = ""
    depth: int = 0
    event: str = ""               # segment_start / segment_end / stage_change / toggle
    payload: dict | None = None
    frame: object | None = None
    fid: int = 0
    ts_ns: int = 0
    age_ms: float = 0.0
    state: dict | None = None
    pointer: dict | None = None
    due_peep: bool = False
    due_save: bool = False


class _StreamQueue:
    """有界帧 + 不淘汰控制事件的单一 FIFO（契约 5.2）。

    容量只约束帧；队列满时从队头找最旧的帧淘汰（丢旧保新），控制事件原位保留。
    控制事件达到高水位时判定消费者失速：先合并阶段事件，再成对丢弃最旧的已结束
    段边界；开关事件永不淘汰。
    """

    def __init__(self, frame_cap: int = FRAME_QUEUE_MAX,
                 control_high_water: int = CONTROL_HIGH_WATER):
        self._cv = threading.Condition()
        self._dq: deque[_StreamItem] = deque()
        self._frame_cap = int(frame_cap)
        self._control_hw = int(control_high_water)
        self._frame_count = 0
        self._control_count = 0
        self._inflight = 0
        self._closed = False
        self.degraded = False
        self.dropped_frames = 0
        self.stage_events_coalesced = 0
        self.segment_events_dropped = 0
        self.frames_rejected_degraded = 0

    def put_frame(self, item: _StreamItem):
        """入队一帧；返回 (是否接收, 被淘汰的帧或 None)。"""
        with self._cv:
            if self._closed:
                return False, None
            if self.degraded:
                self.frames_rejected_degraded += 1
                return False, None
            evicted = None
            while self._frame_count >= self._frame_cap:
                idx = next((i for i, it in enumerate(self._dq)
                            if it.kind == "frame"), None)
                if idx is None:
                    break
                evicted = self._dq[idx]
                del self._dq[idx]
                self._frame_count -= 1
                self.dropped_frames += 1
            self._dq.append(item)
            self._frame_count += 1
            self._cv.notify_all()
            return True, evicted

    def put_control(self, item: _StreamItem) -> bool:
        with self._cv:
            if self._closed:
                return False
            if self._control_count >= self._control_hw:
                self.degraded = True
                if item.event == "stage_change":
                    if not self._coalesce_stage(item.segment_id):
                        self.stage_events_coalesced += 1
                        return False
                elif item.event in ("segment_start", "segment_end"):
                    if not self._drop_oldest_closed_pair():
                        self.segment_events_dropped += 1
                        return False
                # toggle 永不淘汰
            self._dq.append(item)
            self._control_count += 1
            self._cv.notify_all()
            return True

    def _coalesce_stage(self, segment_id) -> bool:
        """失速时把同段的阶段事件收敛成最新一条（帧本身已携带阶段，不丢归属）。"""
        removed = 0
        i = 0
        while i < len(self._dq):
            it = self._dq[i]
            if (it.kind == "control" and it.event == "stage_change"
                    and it.segment_id == segment_id):
                del self._dq[i]
                self._control_count -= 1
                removed += 1
            else:
                i += 1
        if removed:
            self.stage_events_coalesced += removed
            return True
        return False

    def _drop_oldest_closed_pair(self) -> bool:
        opened: dict = {}
        for i, it in enumerate(self._dq):
            if it.kind != "control":
                continue
            if it.event == "segment_start":
                opened[it.segment_id] = i
            elif it.event == "segment_end" and it.segment_id in opened:
                start_i = opened[it.segment_id]
                del self._dq[i]
                del self._dq[start_i]
                self._control_count -= 2
                self.segment_events_dropped += 2
                return True
        return False

    def get(self, timeout: float = 0.0):
        with self._cv:
            if not self._dq:
                self._cv.wait(timeout)
            if not self._dq:
                return None
            item = self._dq.popleft()
            if item.kind == "frame":
                self._frame_count -= 1
            else:
                self._control_count -= 1
            self._inflight += 1
            self._cv.notify_all()
            return item

    def done(self) -> None:
        """标记一个已取出项处理完毕；与 empty() 一起构成可靠的排空判据。"""
        with self._cv:
            if self._inflight > 0:
                self._inflight -= 1
            self._cv.notify_all()

    def empty(self) -> bool:
        with self._cv:
            return not self._dq and self._inflight == 0

    def take_all(self) -> list:
        with self._cv:
            items = list(self._dq)
            self._dq.clear()
            self._frame_count = 0
            self._control_count = 0
            return items

    def note_control_dropped(self, n: int) -> None:
        with self._cv:
            self.segment_events_dropped += int(n)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class DebugSegment:
    """一段执行器持有的调试归属（契约 5.1）。

    模块只通过 publish()/offer() 交不可变快照与可选帧；导航执行器由 core 自己
    提供 state_provider / pointer，可在采样线程上只读调用。
    """

    def __init__(self, hub, seg_id: int, owner: str, label: str, *,
                 kind: str = "segment", source: str = "pull",
                 save: str = "on", peep_fps: float | None = None,
                 save_fps: float | None = None, pointer=None,
                 state_provider=None, stage: str = ""):
        self._hub = hub
        self.id = int(seg_id)
        self.owner = owner or "?"
        self.label = label or owner or "?"
        self.kind = kind
        self.source = source
        self.save = save
        self.peep_fps = float(peep_fps) if peep_fps is not None else RUN_SAMPLE_HZ
        self.save_fps = float(save_fps) if save_fps is not None else RUN_SAMPLE_HZ
        self.stage = stage or ""
        self.pointer = pointer
        self._state_provider = state_provider
        self._state: dict = {}
        self._state_lock = threading.Lock()
        self.closed = False
        self._last_peep = 0.0
        self._last_save = 0.0

    def publish(self, state: dict | None) -> None:
        if not isinstance(state, dict):
            return
        with self._state_lock:
            self._state = dict(state)

    def snapshot_state(self) -> dict:
        with self._state_lock:
            base = dict(self._state)
        if self._state_provider is not None:
            try:
                extra = self._state_provider()
                if isinstance(extra, dict):
                    base.update(extra)
            except Exception:  # noqa: BLE001 —— 读状态失败不拖垮采样
                pass
        return base

    def set_stage(self, stage: str) -> None:
        self._hub._set_stage(self, stage)

    def offer(self, frame_rgb, fid: int = 0, ts_ns: int = 0,
              age_ms: float = 0.0, state: dict | None = None) -> None:
        if state is not None:
            self.publish(state)
        self._hub._offer(self, frame_rgb, fid, ts_ns, age_ms)

    def close(self, outcome: str = "completed") -> None:
        self._hub.close_segment(self, outcome)

def _put_text(frame, text, pos, scale=0.5, color=(255, 255, 255), stroke=1):
    """带黑色阴影和描边的文字绘制，确保任何背景都清晰"""
    x, y = pos
    # 1px 阴影（始终有，极低成本提升可读性）
    cv2.putText(frame, text, (x + 1, y + 1), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 1)
    if stroke > 1:
        cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), stroke * 2 + 1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, max(1, stroke))


def _draw_dashed_rect(frame, pt1, pt2, color, thickness=1, dash_len=8):
    """画虚线矩形"""
    x1, y1 = pt1
    x2, y2 = pt2
    for i in range(x1, x2, dash_len * 2):
        x_end = min(i + dash_len, x2)
        cv2.line(frame, (i, y1), (x_end, y1), color, thickness)
        cv2.line(frame, (i, y2), (x_end, y2), color, thickness)
    for i in range(y1, y2, dash_len * 2):
        y_end = min(i + dash_len, y2)
        cv2.line(frame, (x1, i), (x1, y_end), color, thickness)
        cv2.line(frame, (x2, i), (x2, y_end), color, thickness)


def _dedup_overlapping(dets, iou_thresh=0.5):
    """同类别重叠框去重：每个重叠区域只保留最高置信度的一个，避免虚线框堆叠"""

    if not dets:
        return []
    by_class: dict[str, list] = {}
    for d in dets:
        by_class.setdefault(d["class_name"], []).append(d)

    result = []
    for cls, cls_dets in by_class.items():
        sorted_dets = sorted(cls_dets, key=lambda d: -d["confidence"])
        kept = []
        for d in sorted_dets:
            x1, y1, x2, y2 = d["box"]
            overlap = False
            for k in kept:
                kx1, ky1, kx2, ky2 = k["box"]
                ix1, iy1 = max(x1, kx1), max(y1, ky1)
                ix2, iy2 = min(x2, kx2), min(y2, ky2)
                if ix1 < ix2 and iy1 < iy2:
                    inter = (ix2 - ix1) * (iy2 - iy1)
                    union = (x2 - x1) * (y2 - y1) + (kx2 - kx1) * (ky2 - ky1) - inter
                    if union > 0 and inter / union > iou_thresh:
                        overlap = True
                        break
            if not overlap:
                kept.append(d)
        result.extend(kept)
    return result


class DebugState:
    """调试帧状态容器：save_frame 收到的全部 kwargs 快照，供渲染器读取。

    简单透传（__dict__ 存字段），渲染器可通过 to_kwargs() 展开为关键字参数，
    复用 DebugManager 的内置默认视图（_render_full/_render_peep）。
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def to_kwargs(self) -> dict:
        """返回状态字段字典（供委托内置视图时展开）"""
        return dict(self.__dict__)


class NavigationDebugger:
    """调试管理器（DebugManager 角色）：调试基础设施 + 渲染器分发。

    • 基础设施：PEEP 线程 / frame buffer / 文件存盘，由本类统一管理。
    • 渲染器分发：模块通过 install_renderer 安装自己的调试视图（owner/token
      机制，旧模块 token 不匹配时无法误清新模块的 renderer）。
    • 内置默认视图：_render_full/_render_peep 作为未安装 renderer 时的兜底
      （导航侧元素 + 检测框 + 顶部信息栏；类别配色等模块语义归模块渲染器）。

    类名保持 NavigationDebugger 不变，避免大改调用方。
    """

    def __init__(self, debug_root: Path):
        self.debug_root = Path(debug_root)  # 调试存图根：user_data_dir()/debug（开发/发行一致）
        self.session_dir: Path | None = None
        self.frame_count = 0

        # PEEP 兼容缓冲（update_peep / get_peep_jpeg 旧接口仍可用）
        self._latest_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()

        # 渲染器（token 机制：install 返回递增 token，remove 需 token 匹配）
        self._renderer = None
        self._renderer_token = 0
        self._renderer_lock = threading.Lock()

        # 运行级调试流（契约 5.1/5.2/5.4/5.9/5.10）
        self._debug_on = False
        self._peep_on = False
        self._lock = threading.RLock()
        self._frame_source = None
        self._run_id: str | None = None
        self._run_dir: Path | None = None
        self._index_f = None
        self._seq = 0
        self._seg_id = 0
        self._stack: list[DebugSegment] = []
        self._counters: dict[int, dict] = {}
        self._run_counters = _new_counters()
        self._q = _StreamQueue()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._processing = False
        self._last_sample = 0.0
        self._last_sample_fid = -1  # 静止画面不重复采样：WGC 同一帧号不重画不重存
        self._degraded = False
        self._worker_errors = 0
        self._peep_jpeg: bytes | None = None
        self._peep_meta: dict = {"state": "idle", "seq": 0}
        self._last_toggle_seq = 0  # 最近一次开关事件在流中的位置（RPC 返回）

    # ---------- 开关（运行中切换 = 有序事件，见契约 5.4） ----------

    @property
    def enabled(self) -> bool:
        return self._debug_on

    @enabled.setter
    def enabled(self, value: bool) -> None:
        value = bool(value)
        if value == self._debug_on:
            return
        self._debug_on = value
        if value:
            self._ensure_index()
        self._enqueue_toggle()

    @property
    def peep_enabled(self) -> bool:
        return self._peep_on

    @peep_enabled.setter
    def peep_enabled(self, value: bool) -> None:
        value = bool(value)
        if value == self._peep_on:
            return
        self._peep_on = value
        self._enqueue_toggle()
        if not value and self._run_id is None:
            self._clear_peep("disabled")

    def enable_peep(self):
        self.peep_enabled = True

    def disable_peep(self):
        self.peep_enabled = False

    # ---------- 运行 / 段生命周期 ----------

    def set_frame_source(self, fn) -> None:
        """登记中心缓存取帧口：返回 (rgb, fid, ts_ns, age_ms)。"""
        self._frame_source = fn

    def begin_run(self, run_id: str | None = None) -> str:
        run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
        with self._lock:
            if self._run_id == run_id:
                return run_id
        if self._run_id is not None:
            self.end_run()
        with self._lock:
            self._run_id = run_id
            self._seq = 0
            self._seg_id = 0
            self._stack.clear()
            self._counters.clear()
            self._run_counters = _new_counters()
            self._degraded = False
            self._run_dir = None
            self._last_sample = 0.0
            self._last_sample_fid = -1
            self._peep_jpeg = None
            self._peep_meta = {"state": "idle", "seq": 0}
        self._ensure_worker()
        self._ensure_index()
        return run_id

    def end_run(self) -> None:
        with self._lock:
            segs = list(reversed(self._stack))
            self._stack.clear()
            for s in segs:
                s.closed = True
            self._last_sample_fid = -1
        # 先停止接收（closed 已置）再排空：结束事件必须排在剩余帧之后，
        # 且超时未处理帧要落进 pending_at_close，不能静默清队列。
        # 排空必须在锁外等——消费者处理项时要拿同一把锁。
        drained = self._wait_drain(DRAIN_TIMEOUT_S)
        pending_by_seg = {} if drained else self._discard_pending()
        for s in segs:
            self._enqueue_end(s, "forced_end", drained=drained,
                              pending=pending_by_seg.get(s.id, 0))
        if not self._wait_drain(DRAIN_TIMEOUT_S):
            self._discard_pending()
        self._stop_worker()
        with self._lock:
            if self._index_f is not None:
                try:
                    self._index_f.close()
                except Exception:  # noqa: BLE001
                    pass
                self._index_f = None
            self._run_id = None
            self._run_dir = None
            self._peep_jpeg = None
            self._peep_meta = {"state": "idle", "seq": self._seq}
            self._stack.clear()
        if self._thread is None:
            self._degraded = False

    def open_scope(self, owner: str, label: str, *, save: str = "on") -> DebugSegment:
        if self._run_id is None:
            self.begin_run()
        return self.open_segment(owner, label, kind="scope", save=save)

    def open_segment(self, owner: str, label: str, *, kind: str = "segment",
                     source: str = "pull", peep_fps: float | None = None,
                     save_fps: float | None = None, save: str = "on",
                     pointer=None, state_provider=None,
                     stage: str = "") -> DebugSegment:
        """开一段归属。scope 由控制器按运行项开，内层段由执行器（跳转图/模块）压栈。"""
        if self._run_id is None:
            self.begin_run()
        with self._lock:
            seg_id = self._seg_id
            self._seg_id += 1
            depth = len(self._stack)
            seg = DebugSegment(
                self, seg_id, owner, label, kind=kind, source=source,
                save=save, peep_fps=peep_fps, save_fps=save_fps,
                pointer=pointer, state_provider=state_provider, stage=stage)
            self._stack.append(seg)
            self._counters[seg_id] = _new_counters()
            # 段切换后允许同一采集帧再入队一次：WGC 对静止画面不出新帧，
            # 不去重会让新段一直停在 gap（A1/A2 的“连续有画面”）。
            self._last_sample_fid = -1
            seq = self._next_seq_locked()
        self._ensure_worker()
        self._q.put_control(_StreamItem(
            kind="control", seq=seq, segment_id=seg_id, owner=seg.owner,
            label=seg.label, stage=seg.stage, depth=depth,
            event="segment_start",
            payload={"kind": kind, "source": source, "save": save}))
        # 只在运行项边界清预览：同一项内的执行器切换（导航步骤之间）保留最后一帧，
        # 否则每秒一次的段切换会把画面闪成空卡片。
        if kind == "scope":
            self._clear_peep("gap")
        return seg

    def close_segment(self, seg: DebugSegment | None,
                      outcome: str = "completed") -> None:
        """弹段。内层段只入队结束事件；作用域关闭要排空后才返回（契约 5.1/5.9）。"""
        if seg is None:
            return
        forced = []
        with self._lock:
            if seg.closed:
                return
            while self._stack and self._stack[-1] is not seg:
                inner = self._stack.pop()
                inner.closed = True
                forced.append(inner)
            if self._stack and self._stack[-1] is seg:
                self._stack.pop()
            seg.closed = True
            wait = seg.kind == "scope"
            # 弹段后新的栈顶要能再拿到一帧（静止画面下 WGC 不会自己出新帧）
            if self._stack:
                self._last_sample_fid = -1
        # 先排空再写结束事件：超时剩余帧必须记进对应段的 segment_end（B10）
        drained, pending_by_seg = True, {}
        if wait:
            drained = self._wait_drain(DRAIN_TIMEOUT_S)
            if not drained:
                pending_by_seg = self._discard_pending()
        for inner in reversed(forced):
            self._enqueue_end(inner, "forced_end", drained=drained,
                              pending=pending_by_seg.get(inner.id, 0))
        self._enqueue_end(seg, outcome, drained=drained,
                          pending=pending_by_seg.get(seg.id, 0))
        if wait:
            self._wait_drain(DRAIN_TIMEOUT_S)

    def _set_stage(self, seg: DebugSegment, stage: str) -> None:
        stage = str(stage or "")
        with self._lock:
            if seg.closed or seg.stage == stage:
                return
            seg.stage = stage
            seq = self._next_seq_locked()
        self._q.put_control(_StreamItem(
            kind="control", seq=seq, segment_id=seg.id, owner=seg.owner,
            label=seg.label, stage=stage, event="stage_change"))

    def _enqueue_end(self, seg: DebugSegment, outcome: str,
                     drained: bool, pending: int) -> None:
        with self._lock:
            seq = self._next_seq_locked()
        self._q.put_control(_StreamItem(
            kind="control", seq=seq, segment_id=seg.id, owner=seg.owner,
            label=seg.label, stage=seg.stage, event="segment_end",
            payload={"outcome": outcome, "drained": drained, "pending": pending}))

    # ---------- 线程与采样 ----------

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self._stop_evt.is_set():
                self._degraded = True   # 上一轮消费者未退出：本运行按降级处理
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="debug-stream", daemon=True)
        self._thread.start()

    def _stop_worker(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(JOIN_TIMEOUT_S)
            if t.is_alive():
                # 消费者卡住：保留线程与停止标志，不再启动第二个消费者
                self._degraded = True
                return
        self._thread = None
        self._stop_evt.clear()

    def _top_segment(self) -> DebugSegment | None:
        with self._lock:
            return self._stack[-1] if self._stack else None

    def _output_on(self, seg: DebugSegment) -> tuple[bool, bool]:
        peep_on = self._peep_on and not seg.closed and seg.peep_fps > 0
        save_on = (self._debug_on and not seg.closed
                   and seg.save not in ("off", "self") and seg.save_fps > 0)
        return peep_on, save_on

    def _worker_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                seg = self._top_segment()
                if seg is not None:
                    peep_on, save_on = self._output_on(seg)
                    if peep_on or save_on:
                        self._maybe_sample(seg)
                self._processing = True
                item = self._q.get(0.02)
                if item is None:
                    self._processing = False
                    continue
                try:
                    self._process(item)
                finally:
                    self._q.done()
                    self._processing = False
            except Exception:  # noqa: BLE001 —— 调试流故障不得中止执行（I3）
                self._processing = False
                self._worker_errors += 1
                self._degraded = True

    def _maybe_sample(self, seg: DebugSegment) -> None:
        now = time.monotonic()
        if now - self._last_sample < 1.0 / RUN_SAMPLE_HZ:
            return
        self._last_sample = now
        src = self._frame_source
        if src is None:
            return
        try:
            frame, fid, ts_ns, age = src()
        except Exception:  # noqa: BLE001
            return
        if frame is None:
            self._clear_peep("gap")
            return
        if fid and int(fid) == self._last_sample_fid:
            return
        if fid:
            self._last_sample_fid = int(fid)
        self._offer(seg, frame, fid, ts_ns, age)

    def _offer(self, seg: DebugSegment, frame, fid: int = 0,
               ts_ns: int = 0, age_ms: float = 0.0) -> None:
        """帧入口：到期判断、段/开关绑定、入队（契约 5.2/5.4）。"""
        with self._lock:
            if seg.closed:
                self._bump_locked(seg.id, "rejected_at_close")
                return
            if not self._stack or self._stack[-1] is not seg:
                return
            now = time.monotonic()
            peep_on = self._peep_on and seg.peep_fps > 0
            save_on = (self._debug_on and seg.save not in ("off", "self")
                       and seg.save_fps > 0)
            due_peep = due_save = False
            if peep_on:
                if now - seg._last_peep >= 1.0 / max(seg.peep_fps, 0.1):
                    due_peep = True
                    seg._last_peep = now
                else:
                    self._bump_locked(seg.id, "frames_not_due_peep")
            if save_on:
                if now - seg._last_save >= 1.0 / max(seg.save_fps, 0.1):
                    due_save = True
                    seg._last_save = now
                else:
                    self._bump_locked(seg.id, "frames_not_due_save")
            if not (due_peep or due_save):
                return
            seq = self._next_seq_locked()
            item = _StreamItem(
                kind="frame", seq=seq, segment_id=seg.id, owner=seg.owner,
                label=seg.label, stage=seg.stage, depth=len(self._stack) - 1,
                frame=frame, fid=int(fid or 0), ts_ns=int(ts_ns or 0),
                age_ms=float(age_ms or 0.0),
                due_peep=due_peep, due_save=due_save)
        item.state = seg.snapshot_state()
        item.pointer = self._snapshot_pointer(seg)
        ok, evicted = self._q.put_frame(item)
        if evicted is not None:
            self._bump(evicted.segment_id, "frames_dropped_queue")
        if ok:
            self._bump(seg.id, "frames_in")

    def _snapshot_pointer(self, seg: DebugSegment):
        fn = seg.pointer
        if fn is None:
            return None
        try:
            res = fn()
            return res if isinstance(res, dict) else None
        except Exception:  # noqa: BLE001
            return None

    # ---------- 消费 ----------

    def _process(self, item: _StreamItem) -> None:
        if item.kind == "control":
            self._process_control(item)
            return
        seg_id = item.segment_id
        self._bump(seg_id, "frames_processed")
        # 到期计数在“真正处理”时记：入队后被淘汰/收尾丢弃的帧不算到期，
        # 否则 frames_due_* = 成功 + 失败 的闭合恒等式会断。
        if item.due_peep:
            self._bump(seg_id, "frames_due_peep")
        if item.due_save:
            self._bump(seg_id, "frames_due_save")
        try:
            frame_bgr = cv2.cvtColor(item.frame, cv2.COLOR_RGB2BGR)
        except Exception:  # noqa: BLE001
            frame_bgr = item.frame
        base = self._render_baseline(frame_bgr, item)
        if item.due_save:
            full = base
            renderer = self._renderer
            if renderer is not None:
                try:
                    full = renderer.render_full(
                        base.copy(), DebugState(label=item.label, **(item.state or {})))
                except Exception:  # noqa: BLE001
                    self._bump(seg_id, "save_render_failed")
                    full = None
            if full is not None:
                rel = self._save_full(item, full)
                if rel is not None:
                    self._bump(seg_id, "save_written")
                    self._index({
                        "type": "frame", "seq": item.seq, "segment": seg_id,
                        "owner": item.owner, "label": item.label,
                        "stage": item.stage, "fid": item.fid,
                        "ts_ns": item.ts_ns,
                        "age_ms": round(float(item.age_ms), 1), "file": rel})
                else:
                    self._bump(seg_id, "save_write_failed")
        if item.due_peep:
            ok = self._encode_peep(base, item)
            self._bump(seg_id, "peep_encoded" if ok else "peep_encode_failed")

    def _process_control(self, item: _StreamItem) -> None:
        ev = item.event
        if ev == "segment_start":
            self._index({
                "type": "segment_start", "seq": item.seq,
                "segment": item.segment_id, "owner": item.owner,
                "label": item.label, "depth": item.depth,
                "stage": item.stage, "payload": item.payload})
        elif ev == "segment_end":
            with self._lock:
                counters = dict(self._counters.get(item.segment_id, _new_counters()))
                remaining = bool(self._stack)
            payload = dict(item.payload or {})
            self._index({
                "type": "segment_end", "seq": item.seq,
                "segment": item.segment_id, "owner": item.owner,
                "label": item.label, "stage": item.stage,
                "outcome": payload.get("outcome"),
                "drained": payload.get("drained"),
                "pending": payload.get("pending"),
                "counters": counters})
            # 外层作用域还在：保留最后一帧直到外层/下一段出新帧；清掉会闪空。
            if not remaining:
                self._clear_peep("idle")
        elif ev == "stage_change":
            self._index({
                "type": "stage", "seq": item.seq, "segment": item.segment_id,
                "owner": item.owner, "stage": item.stage})
        elif ev == "toggle":
            payload = dict(item.payload or {})
            self._index({"type": "toggle", "seq": item.seq,
                         "debug": payload.get("debug"),
                         "peep": payload.get("peep")})
            if not self._peep_on:
                self._clear_peep("disabled")
            elif self._peep_jpeg is None:
                with self._lock:
                    remaining = bool(self._stack)
                self._clear_peep("gap" if remaining else "idle")

    # ---------- 基线绘制（PEEP 最小集） ----------

    def _render_baseline(self, frame_bgr: np.ndarray, item: _StreamItem) -> np.ndarray:
        state = item.state or {}
        ptr = item.pointer or {}
        pos = ptr.get("pos")
        if pos:
            x, y = int(pos[0]), int(pos[1])
            age = ptr.get("age_s")
            color = (0, 0, 220) if age is None or age <= 1.0 else (0, 0, 150)
            cv2.circle(frame_bgr, (x, y), 10, color, 2)
            cv2.line(frame_bgr, (x - 7, y), (x + 7, y), color, 1)
            cv2.line(frame_bgr, (x, y - 7), (x, y + 7), color, 1)
            if age is not None:
                _put_text(frame_bgr, f"cursor {float(age):.1f}s",
                          (x + 12, y - 6), 0.4, color)

        intent = state.get("intent") or {}
        pt = intent.get("point")
        if pt:
            age_s = None
            if intent.get("ts"):
                age_s = time.monotonic() - float(intent["ts"])
            if age_s is None or age_s <= 2.0:
                ix, iy = int(pt[0]), int(pt[1])
                color = (235, 206, 135)
                cv2.circle(frame_bgr, (ix, iy), 14, color, 2)
                cv2.line(frame_bgr, (ix - 9, iy), (ix + 9, iy), color, 2)
                cv2.line(frame_bgr, (ix, iy - 9), (ix, iy + 9), color, 2)
                label = str(intent.get("label") or "")
                status = str(intent.get("status") or "")
                text = " ".join(x for x in (label, status) if x)
                if text:
                    _put_text(frame_bgr, text, (ix + 17, iy + 5), 0.4, color)

        nav = state.get("nav") or {}
        box = nav.get("box")
        if box and len(box) >= 4:
            _draw_dashed_rect(
                frame_bgr, (int(box[0]), int(box[1])),
                (int(box[2]), int(box[3])), (255, 255, 0), 2)
        node = nav.get("node") or item.stage or ""
        if node:
            line = str(node)
            if nav.get("phase"):
                line += f" · {nav['phase']}"
            if nav.get("template"):
                line += f" · {nav['template']}"
            if nav.get("score") is not None:
                try:
                    line += f" {float(nav['score']):.3f}"
                except (TypeError, ValueError):
                    pass
            _put_text(frame_bgr, line, (10, 22), 0.5, (255, 255, 255))

        head = (f"{item.owner} · {item.stage or item.label} · "
                f"#{item.seq} · {float(item.age_ms or 0):.0f}ms")
        _put_text(frame_bgr, head, (10, frame_bgr.shape[0] - 10), 0.5, (230, 230, 230))
        return frame_bgr

    def _save_full(self, item: _StreamItem, full: np.ndarray) -> str | None:
        with self._lock:
            run_dir = self._run_dir
        if run_dir is None:
            return None
        try:
            seg_dir = run_dir / f"{int(item.segment_id):02d}_{_safe_name(item.owner)}"
            seg_dir.mkdir(parents=True, exist_ok=True)
            path = seg_dir / f"{int(item.seq):06d}.jpg"
            if not cv2.imwrite(str(path), full, [cv2.IMWRITE_JPEG_QUALITY, 90]):
                return None
            return str(path.relative_to(run_dir)).replace("\\", "/")
        except Exception:  # noqa: BLE001
            return None

    def _encode_peep(self, peep_bgr: np.ndarray, item: _StreamItem) -> bool:
        ok, buf = cv2.imencode(".jpg", peep_bgr, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return False
        with self._lock:
            self._peep_jpeg = buf.tobytes()
            self._peep_meta = {
                "seq": item.seq, "segment": item.segment_id,
                "ts_ms": int(time.time() * 1000), "owner": item.owner,
                "label": item.label, "stage": item.stage,
                "age_ms": round(float(item.age_ms), 1),
                "painted_at": time.monotonic(), "state": "live"}
        with self._frame_lock:
            self._latest_frame = peep_bgr
        return True

    # ---------- 小工具 ----------

    def _next_seq_locked(self) -> int:
        # 契约 A3：一次运行内序号从 1 起（文件名 / 索引 / 预览共用同一编号）
        self._seq += 1
        return self._seq

    def _bump(self, seg_id, key: str, n: int = 1) -> None:
        with self._lock:
            self._bump_locked(seg_id, key, n)

    def _bump_locked(self, seg_id, key: str, n: int = 1) -> None:
        counters = self._counters.get(seg_id)
        if counters is not None and key in counters:
            counters[key] += n
        if key in self._run_counters:
            self._run_counters[key] += n

    def _wait_drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._q.empty():
                return True
            time.sleep(0.005)
        return False

    def _discard_pending(self) -> dict[int, int]:
        """排空超时：剩余帧按段计入 pending_at_close，返回 {段: 丢弃帧数}。"""
        items = self._q.take_all()
        pending: dict[int, int] = {}
        controls = 0
        for it in items:
            if it.kind == "frame":
                self._bump(it.segment_id, "pending_at_close")
                pending[it.segment_id] = pending.get(it.segment_id, 0) + 1
            else:
                controls += 1
        if controls:
            self._q.note_control_dropped(controls)
        self._degraded = True
        return pending

    def _ensure_index(self) -> None:
        with self._lock:
            self._ensure_index_locked()

    def _ensure_index_locked(self) -> None:
        if (not self._debug_on or self._run_id is None
                or self._index_f is not None):
            return
        try:
            self._run_dir = self.debug_root / self._run_id
            self._run_dir.mkdir(parents=True, exist_ok=True)
            self._index_f = open(self._run_dir / "index.jsonl", "a", encoding="utf-8")
        except Exception:  # noqa: BLE001
            self._run_dir = None
            self._index_f = None

    def _index(self, record: dict) -> None:
        # 每条事件都带双时钟：段边界/阶段/开关行过去只有运行内序号，无法落到时间轴
        record.setdefault("ts_wall_ns", time.time_ns())
        record.setdefault("ts_mono_ns", time.monotonic_ns())
        with self._lock:
            # 索引文件一旦开过就写到运行结束：运行中关 DEBUG 只停“新帧”，
            # 已在途的存盘帧仍要落索引行，否则文件数会大于索引行数（A5/A11）。
            self._ensure_index_locked()
            if self._index_f is None:
                return
            try:
                self._index_f.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._index_f.flush()
            except Exception:  # noqa: BLE001
                pass

    def _enqueue_toggle(self) -> None:
        with self._lock:
            if self._run_id is None:
                self._last_toggle_seq = 0
                if not self._peep_on:
                    self._clear_peep("disabled")
                return
            seq = self._next_seq_locked()
            self._last_toggle_seq = seq
        self._q.put_control(_StreamItem(
            kind="control", seq=seq, event="toggle",
            payload={"debug": self._debug_on, "peep": self._peep_on}))

    @property
    def last_toggle_seq(self) -> int:
        """最近一次开关事件的序号；0 表示当前没有运行（立即生效、无队列位置）。"""
        return self._last_toggle_seq

    def status(self) -> dict:
        with self._lock:
            top = self._stack[-1] if self._stack else None
            return {
                "run_id": self._run_id,
                "owner": top.owner if top else "",
                "label": top.label if top else "",
                "stage": top.stage if top else "",
                "depth": len(self._stack),
                "seq": self._seq,
                "peep_state": self._peep_meta.get("state"),
                "degraded": bool(self._degraded or self._q.degraded
                                 or self._worker_errors),
                "counters": dict(self._counters.get(top.id, _new_counters()))
                            if top else dict(self._run_counters),
            }

    # ---------- PEEP 预览（新协议：序号 + 元数据 + 不重编码） ----------

    def get_peep_frame(self, after_seq: int | None = None) -> dict:
        """取最新 PEEP 帧与元数据。帧由 IO 线程编码，本方法不编码。"""
        with self._lock:
            top = self._stack[-1] if self._stack else None
            base = {
                "seq": self._seq,
                "owner": top.owner if top else "",
                "label": top.label if top else "",
                "stage": top.stage if top else "",
                "age_ms": 0.0,
            }
            if not self._peep_on:
                return {"frame": None, "state": "disabled", **base}
            meta = dict(self._peep_meta)
            jpeg = self._peep_jpeg
            if jpeg is None:
                state = "gap" if top else (meta.get("state") or "idle")
                out = {**base, **{k: meta[k] for k in
                                  ("owner", "label", "stage", "seq", "age_ms")
                                  if k in meta}}
                out["frame"] = None
                out["state"] = state
                return out
            if after_seq is not None and int(after_seq) == int(meta.get("seq", -1)):
                out = {**base, **meta}
                out["frame"] = None
                out["state"] = "same"
                return out
            out = {**base, **meta}
            painted = float(meta.get("painted_at") or 0.0)
            if painted and (time.monotonic() - painted) * 1000.0 > PEEP_STALE_MS:
                out["state"] = "stale"
            else:
                out["state"] = "live"
            out["frame"] = jpeg
            return out

    def _clear_peep(self, reason: str) -> None:
        with self._lock:
            self._peep_jpeg = None
            self._peep_meta = {
                "state": reason,
                "seq": self._seq,
                "painted_at": time.monotonic(),
            }
        with self._frame_lock:
            self._latest_frame = None

    def get_peep_jpeg(self, quality: int = 70) -> bytes | None:
        """兼容入口：优先返回 IO 线程已编码的帧；旧路径无帧时才补编码。"""
        with self._lock:
            jpeg = self._peep_jpeg
        if jpeg is not None:
            return jpeg
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            frame = self._latest_frame.copy()
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        return buf.tobytes()

    def update_peep(self, peep_img) -> None:
        """兼容入口：模块直接注入预览帧（归档模块 / 旧测试）。

        运行级流启用时以 IO 线程产出为准；本方法只维护旧缓冲，不参与新协议。
        """
        with self._frame_lock:
            self._latest_frame = peep_img.copy()
        with self._lock:
            if self._peep_jpeg is None:
                self._peep_meta = {**self._peep_meta, "state": "live"}

    def start_session(self, label: str):
        """开始一次导航调试会话（仅 enabled 时创建目录）。

        目录名 = `<时间戳>_<标签>`，与 debug/ 下其它会话、logs/ 会话同一规则：
        时间戳恒在最前，标签按「用途」追加在后。
        """
        self.frame_count = 0
        self.session_dir = None
        if not self.enabled:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = self.debug_root / "navigate" / f"{ts}_{label}"
        self.session_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 检测框颜色（core 不认识类别语义，类别配色由模块渲染器自定） ----------

    _DET_SOLID_COLOR = (0, 200, 0)      # 正式检测框（实线）
    _DET_RAW_COLOR = (160, 160, 160)    # 低阈值原始框（虚线）

    # ==================================================================
    #  共用绘制辅助方法
    # ==================================================================

    def _draw_yolo_dets(self, frame, detections, lite=False):
        """YOLO 正式检测框（置信度过滤后）"""
        if not detections:
            return
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            cls_name = det.get("class_name", "?")
            color = self._DET_SOLID_COLOR
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            if not lite:
                _put_text(frame, f"{cls_name} {det['confidence']:.2f}", (x1, y1 - 5), 0.4, color)

    def _draw_raw_dets(self, frame, all_raw, detections):
        """全部原始检测框（低阈值，虚线，仅 debug 全量模式）

        去重策略：
          1. 同类别重叠框只保留最高分（_dedup_overlapping）
          2. 与实线框重叠的虚线框→隐藏（NMS 跨类压制导致高分变虚线很迷惑）
        """
        if not all_raw:
            return
        det_set = {(d["box"], d["class_name"]) for d in (detections or [])}
        deduped = _dedup_overlapping(all_raw, iou_thresh=0.5)

        # 构建实线框的边界盒列表供重叠检查
        solid_boxes = [d["box"] for d in (detections or [])]

        to_draw = []
        for det in deduped:
            if (det["box"], det["class_name"]) in det_set:
                continue
            dx1, dy1, dx2, dy2 = det["box"]
            # 检查是否与任何实线框显著重叠（IoU > 0.3，跨类也压）
            overlaps_solid = False
            for sx1, sy1, sx2, sy2 in solid_boxes:
                ix1, iy1 = max(dx1, sx1), max(dy1, sy1)
                ix2, iy2 = min(dx2, sx2), min(dy2, sy2)
                if ix1 < ix2 and iy1 < iy2:
                    inter = (ix2 - ix1) * (iy2 - iy1)
                    d_area = (dx2 - dx1) * (dy2 - dy1)
                    s_area = (sx2 - sx1) * (sy2 - sy1)
                    union = d_area + s_area - inter
                    if union > 0 and inter / union > 0.3:
                        overlaps_solid = True
                        break
            if not overlaps_solid:
                to_draw.append(det)

        for det in to_draw:
            x1, y1, x2, y2 = det["box"]
            cls_name = det.get("class_name", "?")
            conf = det.get("confidence", 0)
            color = self._DET_RAW_COLOR
            _draw_dashed_rect(frame, (x1, y1), (x2, y2), color, 1)
            _put_text(frame, f"{cls_name} {conf:.2f}", (x1, y1 - 3), 0.3, color)

    def _draw_cursor(self, frame, cursor_pos, cursor_area=0.0, cursor_score=0.0, dist=None, lite=False):
        """选中的光标位置（红色圈 + 十字）"""
        h, w = frame.shape[:2]
        if cursor_pos:
            cx, cy = cursor_pos
            r = 8 if lite else 12
            cv2.circle(frame, (cx, cy), r, (0, 0, 220), 2)
            cv2.line(frame, (cx - 6, cy), (cx + 6, cy), (0, 0, 220), 1)
            cv2.line(frame, (cx, cy - 6), (cx, cy + 6), (0, 0, 220), 1)
            if not lite:
                info = f"CURSOR({cx},{cy}) A={cursor_area:.0f} S={cursor_score:.3f}"
                if dist is not None:
                    info += f" D={dist:.0f}"
                _put_text(frame, info, (cx + 14, cy - 6), 0.4, (0, 0, 220))
        elif not lite:
            _put_text(frame, "NO CURSOR", (w // 2 - 50, 22), 0.55, (0, 0, 220), stroke=2)

    def _draw_button(self, frame, button_pos, lite=False):
        """按钮目标（蓝色圈 + 十字）"""
        if not button_pos:
            return
        bx, by = button_pos
        r = 10 if lite else 14
        cv2.circle(frame, (bx, by), r, (235, 206, 135), 2)
        cv2.line(frame, (bx - 8, by), (bx + 8, by), (235, 206, 135), 1)
        cv2.line(frame, (bx, by - 8), (bx, by + 8), (235, 206, 135), 1)
        if not lite:
            _put_text(frame, f"btn({bx},{by})", (bx + 16, by + 4), 0.4, (235, 206, 135))

    def _draw_templates(self, frame, template_rects):
        """模板匹配矩形（青色）— 仅 debug 全量模式"""
        if not template_rects:
            return
        for tr in template_rects:
            cx, cy = tr["pos"]
            tw, th = tr["size"]
            x1 = cx - tw // 2
            y1 = cy - th // 2
            cv2.rectangle(frame, (x1, y1), (x1 + tw, y1 + th), (255, 255, 0), 2)
            _put_text(frame, f"TPL {tr.get('name','')} {tr['confidence']:.2f}", (x1, y1 - 5), 0.4, (255, 255, 0))

    def _draw_nav_candidates(self, frame, candidates, all_candidates):
        """导航光标候选（黑=过滤, 绿=入围, 紫=拉黑）— 仅 debug 全量模式"""
        # 被过滤拉黑的轮廓（黑色）
        if all_candidates:
            cand_set = {c["pos"] for c in (candidates or [])}
            for c in all_candidates:
                if c["pos"] in cand_set:
                    continue
                px, py = c["pos"]
                cv2.circle(frame, (px, py), 5, (0, 0, 0), 1)
                _put_text(frame, f"A{c['area']:.0f} R{c['circularity']:.2f}", (px + 6, py - 4), 0.30, (0, 0, 0))
        # 入围候选（绿色/紫色）
        if candidates:
            for c in candidates:
                px, py = c["pos"]
                color = (255, 0, 255) if c.get("blacklisted") else (0, 200, 0)
                cv2.circle(frame, (px, py), 8, color, 1)
                _put_text(frame, f"A{c['area']:.0f} R{c['circularity']:.2f}", (px + 9, py - 4), 0.32, color)

    # ==================================================================
    #  组合渲染
    # ==================================================================

    def _render_full(self, img: np.ndarray, **kw) -> np.ndarray:
        """全量标注绘制（存盘用），返回 BGR 帧

        未安装模块渲染器时的内置兜底视图：导航侧元素 + 检测框 + 顶部信息栏。
        """
        frame = img.copy()
        label = kw.get("label", "")

        # 导航场景元素
        self._draw_nav_candidates(frame, kw.get("candidates"), kw.get("all_candidates"))
        self._draw_button(frame, kw.get("button_pos"))
        self._draw_cursor(frame, kw.get("cursor_pos"), kw.get("cursor_area", 0.0),
                          kw.get("cursor_score", 0.0), kw.get("dist"))
        self._draw_templates(frame, kw.get("template_rects"))

        # 检测框（调用方传了 detections 时）
        self._draw_raw_dets(frame, kw.get("all_raw_dets"), kw.get("detections"))
        self._draw_yolo_dets(frame, kw.get("detections"))

        # 顶部信息栏
        info_line = f"#{self.frame_count}"
        if label:
            info_line += f" | {label}"
        _put_text(frame, info_line, (10, 22), 0.5, (255, 255, 255))
        if kw.get("cursor_score", 0) > 0:
            _put_text(frame, f"score={kw['cursor_score']:.3f}", (10, 42), 0.4, (200, 200, 200))

        return frame

    def _render_peep(self, img: np.ndarray, **kw) -> np.ndarray:
        """精简绘制（PEEP 实时预览用），返回 BGR 帧

        未安装模块渲染器时的内置兜底视图：导航侧元素 + 检测框 + 顶部信息栏。
        """
        frame = img.copy()
        label = kw.get("label", "")

        # 检测框（精简：无置信度文字）
        self._draw_yolo_dets(frame, kw.get("detections"), lite=True)

        # 导航场景元素
        self._draw_cursor(frame, kw.get("cursor_pos"), lite=True)
        self._draw_button(frame, kw.get("button_pos"), lite=True)

        info_parts = [f"#{self.frame_count}"]
        if label:
            info_parts.append(label)
        _put_text(frame, " | ".join(info_parts), (10, 30), 0.75, (255, 255, 255), stroke=3)

        return frame

    # ==================================================================
    #  渲染器管理（owner/token 机制）
    # ==================================================================

    def install_renderer(self, renderer) -> int:
        """安装渲染器，返回 token；再次安装会替换旧渲染器

        模块退出时用返回的 token 调用 remove_renderer 归还；
        若期间被新模块替换，旧 token 将不匹配，remove 不会误清新渲染器。
        """
        with self._renderer_lock:
            self._renderer_token += 1
            self._renderer = renderer
            return self._renderer_token

    def remove_renderer(self, token: int) -> None:
        """按 token 移除渲染器；token 不匹配（已被新模块替换）时不动"""
        with self._renderer_lock:
            if token == self._renderer_token:
                self._renderer = None

    # ==================================================================
    #  统一入口
    # ==================================================================

    def save_frame(
        self,
        frame_rgb: np.ndarray,
        cursor_pos: tuple[int, int] | None = None,
        cursor_area: float = 0.0,
        cursor_score: float = 0.0,
        button_pos: tuple[int, int] | None = None,
        candidates: list[dict] | None = None,
        all_candidates: list[dict] | None = None,
        dist: float | None = None,
        label: str = "",
        template_rects: list[dict] | None = None,
        detections: list[dict] | None = None,
        save_to_disk: bool = True,
        all_raw_dets: list[dict] | None = None,
        **extra_kwargs,
    ):
        """保存一帧调试截图

        save_to_disk: False 时跳过 cv2.imwrite。
        PEEP 预览帧独立于存盘帧——PEEP 使用精简绘制，存盘使用全量绘制。

        颜色约定（全量绘制）：
          🔴 红 — 选中的光标
          🟢 绿 — 入围候选（通过硬过滤，参与评分）
          🟣 紫 — 静止拉黑（连续3帧不动，被跳过评分）
          ⚫ 黑 — 被硬过滤拉黑的探测项
          🔵 亮蓝 — 按钮目标
        """
        if not self.enabled and not self.peep_enabled:
            return
        if self.session_dir is None and not self.peep_enabled:
            return

        self.frame_count += 1
        img_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

        kwargs = dict(
            cursor_pos=cursor_pos, cursor_area=cursor_area, cursor_score=cursor_score,
            button_pos=button_pos, candidates=candidates, all_candidates=all_candidates,
            dist=dist, label=label, template_rects=template_rects,
            detections=detections,
            all_raw_dets=all_raw_dets,
            **extra_kwargs,   # 模块自定义状态字段（如鉴宝的 treasure_*）透传给渲染器
        )

        # 渲染器分发：已安装 renderer 时走模块视图（render_full/render_peep 各自独立调用），
        # 否则走 DebugManager 内置默认视图（兼容兜底，与重构前行为一致）
        renderer = self._renderer
        if renderer is not None:
            state = DebugState(**kwargs)
            full_img = renderer.render_full(img_bgr, state)
            peep_img = renderer.render_peep(img_bgr, state)
        else:
            full_img = self._render_full(img_bgr, **kwargs)   # 内置默认视图（兼容兜底）
            peep_img = self._render_peep(img_bgr, **kwargs)

        # 存盘：全量绘制
        if self.enabled and self.session_dir is not None and save_to_disk:
            fname = f"{self.frame_count:03d}.png"
            cv2.imwrite(str(self.session_dir / fname), full_img)

        # PEEP：精简绘制
        if self.peep_enabled:
            with self._frame_lock:
                self._latest_frame = peep_img

        # 返回全量渲染结果，供调用方复用（如 treasure 模块全量 debug 存盘），
        # 避免同一帧被 render_full 二次渲染（CPU 竞争是 OCR 推理被拖慢的主因之一）。
        return full_img
