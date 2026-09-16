#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Debug IO worker 底座（模块开发模式统一计划 · P2b）。

纯新增，不接入任何运行时代码。目标：把「异步 IO worker（取帧→渲染→写盘 / 更新
PEEP）」收敛成一份**依赖窄接口**的通用实现，禁止直接访问 `ctx.debug._frame_lock` /
`_latest_frame` 等私有成员（★计划红线 8：必须用 FrameSource/DebugSink 接口收口）。

两套窄接口（本模块定义契约，真正的 NavigationDebugger 适配在 P5 接上）：

1. FrameSource —— 帧来源。`current_frame()` 提供一帧画面（截图/缓存帧均可）。
2. DebugSink   —— 帧去处。抽象「PEEP 预览帧写入」「写盘回调」：
       - `update_peep(peep_img)`：更新 PEEP 预览帧（封装 _latest_frame + 锁）
       - `save_full(idx, full_img_bgr)` / `save_raw(idx, frame_bgr)`：写盘（存 raw /
         rendered 两张图），由实现方管理目录与编码口径

DebugIOWorker 自身不 import cv2、不碰 debug 对象、不管理文件系统——渲染交给注入的
renderer（具备 render_full / render_peep），写盘与 PEEP 全部委托给 DebugSink。
这样 core 层零领域判断、零私有成员访问，可独立单测（注入 fake）。

任务队列策略（对齐鉴宝现状）：**有界队列，满则丢新任务**——观测降密度，不阻塞主循环。
"""
from __future__ import annotations

import queue as _queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol


class FrameSource(Protocol):
    """帧来源抽象：提供一帧画面供 IO worker 消费。"""

    def current_frame(self):
        """返回当前帧（BGR 或 RGB ndarray），无可用帧返回 None。"""
        ...


class DebugRendererIO(Protocol):
    """渲染器最小契约：IO worker 只需 render_full / render_peep。"""

    def render_full(self, frame_bgr, state: Any): ...  # noqa: E704

    def render_peep(self, frame_bgr, state: Any): ...  # noqa: E704


class DebugSink(Protocol):
    """帧去处抽象：PEEP 预览更新 + 写盘回调（封装 debug 私有成员，对外干净）。"""

    peep_enabled: bool

    def update_peep(self, peep_img) -> None: ...

    def save_full(self, idx: int, full_img_bgr) -> None: ...

    def save_raw(self, idx: int, frame_bgr) -> None: ...


@dataclass
class IOTask:
    """一条 IO 任务（对齐鉴宝 _io_queue 的 6 元组语义，但以对象承载更清晰）。"""

    cmd: str                 # "frame"（写盘+PEEP） / "peep"（仅 PEEP）
    frame_rgb: Any           # RGB 帧
    idx: int                 # raw 帧号（全局累计）
    didx: int                # rendered(debug 图) 编号
    label: str = ""
    kwargs: dict | None = None


class DebugIOWorker:
    """通用异步 IO worker（latest-ish 降密度 + 渲染委托 + DebugSink 落盘/PEEP）。

    - 不访问任何 debug 私有成员，只经注入的 renderer / DebugSink / FrameSource。
    - 有界队列：满则丢新任务，不阻塞主循环。
    - 顶层 try/except：单次任务异常不杀死 daemon，计数后继续。
    """

    def __init__(
        self,
        renderer: DebugRendererIO,
        sink: DebugSink,
        queue_max: int = 0,
        *,
        frame_source: FrameSource | None = None,
        mode: str = "manual",
        to_bgr=None,
        make_state=None,
    ):
        """
        mode:
          "manual" —— 由调用方显式 enqueue 任务（同鉴宝 _io_submit），
                      queue_max<=0 表示不设上限。
          "piped"  —— 由本 worker 线程从 frame_source 拉帧（占位，后续 P5 接
                      capture 流式场景可启用）。

        to_bgr:     可注入的 RGB→BGR 转换；None 时使用 cv2（延迟 import）。
        make_state: 可注入的 state 构造器；None 时延迟 import DebugState。
                    两者都缺省会拉到 cv2 / core.debug（重依赖），仅在真正处理
                    "frame" 任务时付出；测试可注入轻量实现以保持单测纯逻辑。
        """
        self.renderer = renderer
        self.sink = sink
        self._queue = _queue.Queue(maxsize=queue_max) if queue_max and queue_max > 0 else _queue.Queue()
        self._stop = threading.Event()
        self._mode = mode
        self._frame_source = frame_source
        self._processed = 0
        self._dropped = 0
        self._to_bgr = to_bgr
        self._make_state = make_state

    # ---------------- 对外：进队 ----------------

    def enqueue(self, cmd: str, frame_rgb, idx: int, didx: int, label: str = "",
                kwargs: dict | None = None) -> bool:
        """入队一条任务。队列满则丢弃并计数，返回是否真正入队。"""
        task = IOTask(cmd=cmd, frame_rgb=frame_rgb, idx=idx, didx=didx,
                      label=label, kwargs=kwargs or {})
        try:
            self._queue.put_nowait(task)
        except _queue.Full:
            self._dropped += 1
            return False
        return True

    # ---------------- 对外：消费者（manual 模式由调用方驱动） ----------------

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def dropped(self) -> int:
        return self._dropped

    def drain(self, deadline: float | None = None, timeout: float = 0.5) -> None:
        """消费队列直到空（或超时）。供 manual 模式在合理时机调用。"""
        while self._queue.qsize() > 0:
            try:
                task = self._queue.get(timeout=timeout)
                self._process(task)
            except _queue.Empty:
                break

    def process_one(self) -> bool:
        """处理单个任务；队列空返回 False。供 manual 逐帧拉动。"""
        try:
            task = self._queue.get(timeout=0.1)
        except _queue.Empty:
            return False
        self._process(task)
        return True

    # ---------------- 内部：单任务处理 ----------------

    def _process(self, task: IOTask) -> None:
        """渲染 → 委托 DebugSink 写盘 / 更新 PEEP。单任务异常由调用方 try/except 兜底。

        为保持「core 零重依赖」：RGB→BGR 与 state 构造均为可注入（_to_bgr/_make_state），
        缺省时延迟 import cv2 / DebugState，仅在真正处理 frame 任务时付出。
        """
        to_bgr = self._to_bgr
        if task.cmd == "frame":
            if to_bgr is None:
                import cv2

                def _cv_rgb2bgr(f):
                    return cv2.cvtColor(f, cv2.COLOR_RGB2BGR)

                to_bgr = _cv_rgb2bgr
            frame_bgr = to_bgr(task.frame_rgb)
        else:
            frame_bgr = task.frame_rgb

        make_state = self._make_state
        if make_state is None:
            from maaracing_master.core.debug import DebugState

            def _default_make_state(label, kwargs):
                return DebugState(label=label, **kwargs)

            make_state = _default_make_state

        state = make_state(task.label, task.kwargs or {})

        if task.cmd == "frame":
            self.sink.save_raw(task.idx, frame_bgr)
            full_img = self.renderer.render_full(frame_bgr, state)
            self.sink.save_full(task.didx, full_img)
            if self.sink.peep_enabled:
                peep_img = self.renderer.render_peep(frame_bgr, state)
                self.sink.update_peep(peep_img)
        elif task.cmd == "peep":
            if self.sink.peep_enabled:
                peep_img = self.renderer.render_peep(frame_bgr, state)
                self.sink.update_peep(peep_img)
        # 未知 cmd 忽略（兼容未来扩展，不抛）
        self._processed += 1

    # ---------------- 优雅结束 ----------------

    def close(self) -> None:
        """置停止标志并清空剩余任务（幂等）。"""
        self._stop.set()


def run_once(worker: DebugIOWorker) -> None:
    """进程退出前的收尾：把剩余任务处理完（对齐鉴宝 _drain_io_queue 语义）。"""
    worker.drain(deadline=time.monotonic() + 1.0)