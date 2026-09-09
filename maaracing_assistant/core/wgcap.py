#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
常驻 WGC (Windows Graphics Capture) 中心采集器。

单生产者 / 多消费者架构：采集线程持续捕获游戏窗口帧，业务线程纯读缓存——
**不在各自线程里发起截图请求**，从根上消灭「多线程争抢截图通道串行阻塞」。

零拷贝架构：WGC → D3D11 CopyResource → Map → memoryview → ndarray
每帧独立 staging texture，NativeMappedFrame 持有所有权，ndarray 通过 base 链
阻止 Unmap，因此 latest_frame 是安全的 immutable snapshot。

设计原则：
- 回调只做引用交换 + 记录元数据，不做 NumPy 重计算
- get_latest() 永远不触发截图、永远不等下一帧（~3μs）
- get_latest_rgb() 惰性产出标准 16:9 720p RGB 帧（帧号变才重算一次），
  多消费者共享同一缓存数组（只读约定）
- 60fps 上限：超频回调帧直接丢弃（游戏 70Hz 全速下只保留 ≤60fps）
- 客户区精确裁剪（去标题栏/边框）+ 非 16:9 兜底校正（底部锚定）
- 锁内只交换 Python 引用和整数
"""

from __future__ import annotations

import ctypes
import time
import threading
from ctypes import wintypes

import numpy as np
import cv2
from windows_capture import WindowsCapture, Frame, InternalCaptureControl

from maaracing_assistant.core.logger import logger


_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4  # Win10 1607+
_dwmapi = ctypes.WinDLL("dwmapi")
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT
_dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


class WgcCapture:
    """常驻 WGC 中心采集器。

    用法：
        cap = WgcCapture(hwnd, max_fps=60)
        cap.start()
        bgra, fid, ts_ns, age_ms = cap.get_latest()      # 裁剪后客户区 BGRA（零拷贝视图）
        rgb, fid, ts_ns, age_ms = cap.get_latest_rgb()   # 标准 16:9 720p RGB（缓存数组）
        cap.stop()

    参数：
        max_fps: 采集帧率上限（丢弃更高速率的回调帧，默认 60）。
        standard_size: get_latest_rgb() 的标准输出尺寸（默认 1280x720，真正的 16:9）。
    """

    def __init__(self, hwnd: int, max_fps: int = 60,
                 standard_size: tuple[int, int] = (1280, 720)):
        self._hwnd = hwnd
        self._max_interval_ns = int(1_000_000_000 / max_fps) if max_fps > 0 else 0
        self._standard_size = tuple(standard_size)
        self._capture: WindowsCapture | None = None
        self._lock = threading.Lock()

        # 最新帧状态（锁保护）
        self._latest_frame: np.ndarray | None = None
        self._frame_id = 0
        self._capture_ts_ns = 0          # callback 到达时间，perf_counter_ns
        self._source_timespan = 0        # WGC frame 的 SystemRelativeTime（原始值）

        # 客户区裁剪（启动时计算一次；窗口经 controller 统一 720p，边框固定）
        self._client_offset: tuple[int, int] | None = None  # 客户区在窗口帧中的偏移 (dx, dy)
        self._client_size: tuple[int, int] | None = None    # 客户区尺寸 (w, h)
        self._dwm_size: tuple[int, int] | None = None       # DWM 可见边界尺寸（判定渲染链用）
        self._crop_warned = False                            # 裁剪越界只告警一次

        # 60fps 节流
        self._last_accept_ts_ns: int | None = None

        # 标准帧缓存（get_latest_rgb 惰性重算；只读约定，消费方不得原地修改）
        self._std_fid: int = -1
        self._std_rgb: np.ndarray | None = None

        # 运行状态
        self._started = False
        self._stopped = False

        # metrics（仅统计，不保存 ndarray）
        self._callback_intervals: list[float] = []
        self._last_callback_ts_ns = 0

    # ---- 公开接口 ----

    def start(self):
        """启动后台 WGC 捕获线程（幂等）。"""
        if self._started:
            return
        self._started = True
        self._stopped = False
        self._compute_client_rect()

        capture = WindowsCapture(
            window_hwnd=self._hwnd,
            cursor_capture=False,   # 不叠加系统光标（游戏内光标由游戏自己渲染）
            draw_border=False,
            minimum_update_interval=0,  # 系统全速采集，由本模块按 max_fps 节流
            dirty_region=False,
        )
        self._capture = capture

        @capture.event
        def on_frame_arrived(frame: Frame, _control: InternalCaptureControl):
            self._on_frame(frame)

        @capture.event
        def on_closed():
            logger.log("WGC 捕获窗口已关闭", "WARNING")
            self._stopped = True

        capture.start_free_threaded()
        logger.log("WGC 中心采集已启动", "DEBUG")

    def stop(self):
        """停止捕获（幂等）。

        原生 stop 返回 ≠ windows_capture 线程已收摊：帧 ndarray 的 base 链
        持有 staging texture，Python 引用不清零就退出/回收会与原生收尾竞争
        （0xC0000005，P2b 实验 run13 二分定案：stop 后清引用+宽限则稳定干净）。
        """
        if self._stopped and self._capture is None:
            return
        self._stopped = True
        cap = self._capture
        self._capture = None
        if cap is not None:
            try:
                cap.stop()  # windows_capture 原生优雅停止
            except Exception:
                pass
        with self._lock:
            self._latest_frame = None
            self._std_rgb = None
            self._std_fid = -1
        time.sleep(0.25)  # 原生线程收摊宽限
        logger.log("WGC 中心采集已停止", "DEBUG")

    def get_latest(self):
        """获取最新帧及其元数据（客户区 BGRA，零拷贝视图，非 C-contiguous）。

        Returns:
            (frame, frame_id, capture_ts_ns, frame_age_ms) 或 (None, 0, 0, 0)
        """
        now_ns = time.perf_counter_ns()
        with self._lock:
            frame = self._latest_frame
            fid = self._frame_id
            ts_ns = self._capture_ts_ns
        if frame is None:
            return None, 0, 0, 0.0
        return frame, fid, ts_ns, (now_ns - ts_ns) / 1_000_000

    def get_latest_rgb(self):
        """获取标准 16:9 RGB 帧（720p，C-contiguous，缓存数组）。

        全模块统一截图入口：主循环 / 导航线程 / racing / OCR 都从这里读——多消费者
        共享同一数组（帧号变才重算一次），零阻塞、零争抢，各自按自己节奏消费。

        Returns:
            (rgb_frame, frame_id, capture_ts_ns, frame_age_ms) 或 (None, 0, 0, 0)
        """
        frame, fid, ts_ns, age_ms = self.get_latest()
        if frame is None:
            return None, 0, 0, 0.0
        if self._std_fid != fid or self._std_rgb is None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
            sw, sh = self._standard_size
            if (rgb.shape[1], rgb.shape[0]) != (sw, sh):
                rgb = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
            self._std_rgb = np.ascontiguousarray(rgb)
            self._std_fid = fid
        return self._std_rgb, fid, ts_ns, age_ms

    @property
    def frame_id(self) -> int:
        with self._lock:
            return self._frame_id

    @property
    def is_running(self) -> bool:
        return self._started and not self._stopped

    @property
    def source_timespan(self) -> int:
        """WGC frame 的原始 SystemRelativeTime，可用于分析 native pipeline 延迟。"""
        with self._lock:
            return self._source_timespan

    # ---- metrics ----

    def get_metrics(self):
        """返回回调间隔统计（ms）。"""
        intervals = self._callback_intervals
        if not intervals:
            return {}
        arr = np.array(intervals)
        return {
            "callback_interval_p50": float(np.median(arr)),
            "callback_interval_p95": float(np.percentile(arr, 95)),
            "callback_interval_p99": float(np.percentile(arr, 99)),
            "callback_count": len(intervals),
            "client_offset": self._client_offset,
            "client_size": self._client_size,
        }

    def reset_metrics(self):
        """重置统计。"""
        self._callback_intervals.clear()

    # ---- 客户区裁剪 ----

    def _compute_client_rect(self):
        """计算客户区在 WGC 窗口帧中的偏移与尺寸（去标题栏/边框）。

        WGC 捕获的是窗口整体（含标题栏/边框）；先拿 DWM 可见边界（不含阴影），
        再拿客户区屏幕原点，偏移 = 客户区原点 - 可见边界原点。
        坐标系统一（真机八炸定案）：DWM 边界恒为物理像素，而 GetClientRect/
        ClientToScreen 随进程 DPI awareness 返回逻辑像素——125% 缩放下混系
        相减得出负偏移，裁剪静默失效、整窗（含标题栏）下传。用
        SetThreadDpiAwarenessContext(PMv2) 线程级切换统一取物理坐标，
        调用完即恢复，不影响进程其他部分。
        失败（DWM 不可用等）→ 不裁剪（整窗），后续 16:9 校正兜底。
        """
        try:
            import ctypes.wintypes as _wt

            rect = _wt.RECT()
            hret = _dwmapi.DwmGetWindowAttribute(
                self._hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(rect), ctypes.sizeof(rect))
            if hret != 0:
                raise OSError(f"DwmGetWindowAttribute 失败 HRESULT={hret:#x}")
            u32 = ctypes.windll.user32
            old_ctx = None
            try:  # 线程级 PMv2：本函数内 Win32 坐标全部物理像素
                set_ctx = u32.SetThreadDpiAwarenessContext
                set_ctx.restype = ctypes.c_void_p
                set_ctx.argtypes = [ctypes.c_void_p]
                old_ctx = set_ctx(ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
            except (AttributeError, OSError):
                pass  # Win10 1607 前无此 API：保持原状（低概率，且兜底路径已修向）
            try:
                pt = _wt.POINT(0, 0)
                if not u32.ClientToScreen(self._hwnd, ctypes.byref(pt)):
                    raise OSError("ClientToScreen 失败")
                crect = _wt.RECT()
                if not u32.GetClientRect(self._hwnd, ctypes.byref(crect)):
                    raise OSError("GetClientRect 失败")
            finally:
                if old_ctx is not None:
                    set_ctx(old_ctx)
            self._client_offset = (pt.x - rect.left, pt.y - rect.top)
            self._client_size = (crect.right - crect.left, crect.bottom - crect.top)
            self._dwm_size = (rect.right - rect.left, rect.bottom - rect.top)
            if self._client_offset[0] < 0 or self._client_offset[1] < 0:
                raise OSError(f"客户区偏移为负 {self._client_offset}（坐标系仍不一致，"
                              f"拒绝带病裁剪）")
            logger.log(
                f"WGC 客户区裁剪: offset={self._client_offset} size={self._client_size} "
                f"dwm={self._dwm_size}",
                "DEBUG")
        except Exception as e:  # noqa: BLE001 —— 裁剪失败不阻断采集，整窗兜底
            self._client_offset = None
            self._client_size = None
            self._dwm_size = None
            logger.log(f"WGC 客户区裁剪计算失败（整窗采集兜底）: {e}", "WARNING")

    def _crop_client(self, img: np.ndarray) -> np.ndarray:
        """裁剪出客户区（视图/切片，无拷贝），并做非 16:9 兜底校正（底部锚定）。

        渲染链判定（2026-09-04 实测）：WGC 帧尺寸 ≈ DWM 可见边界尺寸 → 捕获的是
        窗口装饰链（含标题栏/边框），用客户区 offset 裁剪；否则（如游戏走独立
        交换链，帧 1604x902 ≫ 窗口 1281x721）→ 帧即纯游戏画面（天然无标题栏），
        整帧直接使用——不依赖 offset 符号，两种场景都正确。
        兜底方向（真机八炸定案）：非 16:9 时裁顶部、保底部——游戏主体 UI 在
        底部，而混入的窗口装饰在顶部；旧实现 `img[:target_h]` 恰好裁反。
        """
        if (self._client_offset is not None and self._client_size is not None
                and self._dwm_size is not None):
            dw, dh = self._dwm_size
            h, w = img.shape[:2]
            if abs(w - dw) <= 2 and abs(h - dh) <= 2:  # 窗口装饰链 → 裁客户区
                dx, dy = self._client_offset
                cw, ch = self._client_size
                if dx >= 0 and dy >= 0 and dx + cw <= w and dy + ch <= h:
                    img = img[dy:dy + ch, dx:dx + cw]
                elif not self._crop_warned:
                    self._crop_warned = True
                    logger.log(f"WGC 客户区裁剪越界 offset={self._client_offset} "
                               f"size={self._client_size} frame={w}x{h}（整窗下传）",
                               "WARNING")
            # 独立渲染链 → 整帧即内容（跳过裁剪）
        # 非 16:9 兜底：底部锚定裁到 16:9（bottom 是画面主体，顶部裁最安全）
        h, w = img.shape[:2]
        target_h = int(round(w * 9 / 16))
        if 0 < target_h < h:
            img = img[h - target_h:, :]
        return img

    # ---- 内部回调 ----

    def _on_frame(self, frame: Frame):
        if self._stopped:
            return
        now_ns = time.perf_counter_ns()
        # 60fps 上限：超频帧直接丢弃（回调保持极轻，只为节流+裁剪）
        if self._max_interval_ns > 0 and self._last_accept_ts_ns is not None:
            if now_ns - self._last_accept_ts_ns < self._max_interval_ns:
                return

        try:
            img = frame.frame_buffer  # BGRA ndarray，零拷贝
            if img is None or img.size == 0:
                return
            img = self._crop_client(img)
            with self._lock:
                self._latest_frame = img
                self._frame_id += 1
                self._capture_ts_ns = now_ns
                self._source_timespan = frame.timespan
            self._last_accept_ts_ns = now_ns

            # metrics：回调间隔
            if self._last_callback_ts_ns != 0:
                interval_ms = (now_ns - self._last_callback_ts_ns) / 1_000_000
                self._callback_intervals.append(interval_ms)
            self._last_callback_ts_ns = now_ns

        except Exception as e:
            logger.log(f"WGC 帧处理异常: {e}", "ERROR")