# -*- coding: utf-8 -*-
"""speedrush 驾驶演示录制器：帧 + 物理手柄 + 同源时间戳。

**产出形态**（一个会话 = 一个目录）::

    <会话>/
      meta.json        会话元信息、计数、丢帧数
      frames.jsonl     逐帧索引：seq / ts_ns / 文件名
      pads.jsonl       手柄样本序列：ts_ns / 摇杆 / 扳机 / 按键
      frames/000000.jpg 帧图

**帧与手柄为何分开落盘、离线对齐**：两者的合适频率不同——帧与实机推理同为 30fps
（"K 帧堆叠"的时间跨度必须一致），而手柄要 ≥100Hz（转向延迟 τ 只能从高频数据里读出来，
30fps 的 33ms 分辨率不够）。所以各按各的频率录、各自带同源 ``perf_counter_ns`` 时间戳，
由离线步骤对齐，而不是在采集时就绑成一一对应。

**反压纪律**（调研同类实时项目的教训）：帧入队用 ``put_nowait``，队列满即丢帧并计数，
**绝不阻塞采集侧**——落盘慢一秒不该拖慢控制循环。丢帧数进 meta，异常可查。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2

from maaracing_master.core import xinput
from maaracing_master.core.logger import logger

# 帧队列容量：约两秒（30fps）。满则丢帧计数，不阻塞采集侧。
_FRAME_QUEUE_MAX = 64

# 录制数据格式版本。字段语义变动时递增，供离线步骤识别。
SCHEMA_VERSION = 1


class DriveRecorder:
    """驾驶演示录制器（线程安全；start/stop 幂等）。

    用法：``start()`` → 主循环反复 ``record_frame()`` → ``stop()``。
    帧写盘与手柄采样都在各自线程，调用方只做非阻塞入队。
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        jpeg_quality: int = 85,
        pad_poll_hz: float = 200.0,
        pad_slot: int | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.jpeg_quality = int(jpeg_quality)
        self.pad_poll_hz = float(pad_poll_hz)
        # None = 每次采样自动找第一个已连接手柄（适配用户插哪个槽都行）
        self.pad_slot = pad_slot

        self._frame_q: queue.Queue[tuple[int, int, Any]] = queue.Queue(_FRAME_QUEUE_MAX)
        self._stop = threading.Event()
        self._frame_thread: threading.Thread | None = None
        self._pad_thread: threading.Thread | None = None

        self._frames_written = 0
        self._frames_dropped = 0
        self._pad_samples = 0
        self._pad_none_streak = 0
        self._seq_counter = 0
        self._started_ns = 0
        self._started_iso = ""
        self._running = False
        self._lock = threading.Lock()
        self._frame_size: tuple[int, int] | None = None  # (w, h)

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict[str, int]:
        """实时计数（供 GUI 状态展示；不阻塞）。"""
        return {
            "frames_written": self._frames_written,
            "frames_dropped": self._frames_dropped,
            "pad_samples": self._pad_samples,
        }

    def start(self) -> None:
        if self._running:
            return
        (self.out_dir / "frames").mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._started_ns = time.perf_counter_ns()
        self._started_iso = datetime.now().isoformat(timespec="seconds")
        self._frame_thread = threading.Thread(
            target=self._frame_worker, name="speedrush-rec-frames", daemon=True)
        self._pad_thread = threading.Thread(
            target=self._pad_worker, name="speedrush-rec-pad", daemon=True)
        self._frame_thread.start()
        self._pad_thread.start()
        self._running = True
        logger.log(f"[极速狂飙] 录制开始 → {self.out_dir}", "INFO")

    def stop(self, reason: str = "normal") -> None:
        """停止并 flush。幂等；重复调用只写一次 meta。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._stop.set()
        for th in (self._frame_thread, self._pad_thread):
            if th is not None:
                th.join(timeout=10.0)
        self._frame_thread = None
        self._pad_thread = None
        self._write_meta(reason)
        logger.log(
            f"[极速狂飙] 录制结束：帧 {self._frames_written} 张"
            f"（丢 {self._frames_dropped}）、手柄样本 {self._pad_samples} 条",
            "INFO" if self._frames_dropped == 0 else "WARNING",
        )

    # ---------- 采集侧（调用方线程） ----------

    def record_frame(self, frame_bgr: Any, ts_ns: int | None = None) -> None:
        """非阻塞存入一帧。队列满则丢帧并计数——不得阻塞调用方。"""
        if not self._running:
            return
        if ts_ns is None:
            ts_ns = time.perf_counter_ns()
        h, w = frame_bgr.shape[:2]
        if self._frame_size is None:
            self._frame_size = (int(w), int(h))
        # 序列号在采集侧单调递增：丢帧不使编号跳变，缺口数即丢帧证据
        self._seq_counter += 1
        try:
            self._frame_q.put_nowait((self._seq_counter, ts_ns, frame_bgr))
        except queue.Full:
            self._frames_dropped += 1

    # ---------- 落盘线程 ----------

    def _frame_worker(self) -> None:
        frames_jsonl = self.out_dir / "frames.jsonl"
        frames_dir = self.out_dir / "frames"
        with frames_jsonl.open("w", encoding="utf-8") as fj:
            while True:
                try:
                    seq, ts_ns, frame = self._frame_q.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                name = f"{seq:06d}.jpg"
                try:
                    ok = cv2.imwrite(
                        str(frames_dir / name), frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
                except Exception as exc:  # noqa: BLE001 —— 单帧写失败不该终止录制
                    logger.log(f"[极速狂飙] 录制写帧失败 seq={seq}: {exc!r}", "WARNING")
                    continue
                if not ok:
                    logger.log(f"[极速狂飙] 录制写帧返回失败 seq={seq}", "WARNING")
                    continue
                fj.write(json.dumps(
                    {"seq": seq, "ts_ns": ts_ns, "file": name},
                    ensure_ascii=False) + "\n")
                self._frames_written += 1

    def _pad_worker(self) -> None:
        """高频采样物理手柄并逐行落盘。

        采样线程是 pads.jsonl 的唯一写者，故无需加锁。
        """
        interval = 1.0 / max(self.pad_poll_hz, 1.0)
        slots = [self.pad_slot] if self.pad_slot is not None else [0, 1, 2, 3]
        with (self.out_dir / "pads.jsonl").open("w", encoding="utf-8") as f:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                st = None
                for i in slots:
                    st = xinput.read_state(i)
                    if st is not None:
                        if self.pad_slot is None:
                            self.pad_slot = i  # 锁定首次命中的槽位，后续不再轮询
                        break
                if st is None:
                    self._pad_none_streak += 1
                    if self._pad_none_streak == 1 or self._pad_none_streak % 1000 == 0:
                        logger.log(
                            "[极速狂飙] 录制中未读到物理手柄——"
                            "请确认手柄已连接且未被虚拟手柄占用", "WARNING")
                    if self._pad_none_streak >= 5:
                        break  # 连续读不到即停止采样，不空转刷日志
                else:
                    f.write(json.dumps({
                        "ts_ns": time.perf_counter_ns(),
                        "lx": st.left_stick[0], "ly": st.left_stick[1],
                        "rx": st.right_stick[0], "ry": st.right_stick[1],
                        "lt": st.left_trigger, "rt": st.right_trigger,
                        "btn": st.buttons,
                    }, ensure_ascii=False) + "\n")
                    self._pad_samples += 1
                # 扣掉本次采样耗时，避免采样率随调用开销漂移
                time.sleep(max(0.0, interval - (time.perf_counter() - t0)))

    # ---------- 元信息 ----------

    def _write_meta(self, reason: str) -> None:
        w, h = self._frame_size or (0, 0)
        meta = {
            "schema": SCHEMA_VERSION,
            "started_at": self._started_iso,
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "monotonic_start_ns": self._started_ns,
            "jpeg_quality": self.jpeg_quality,
            "frame_w": w,
            "frame_h": h,
            "pad_slot": self.pad_slot,
            "pad_poll_hz": self.pad_poll_hz,
            "frames_written": self._frames_written,
            "frames_dropped": self._frames_dropped,
            "pad_samples": self._pad_samples,
            "stop_reason": reason,
        }
        try:
            (self.out_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 —— meta 写失败不该抛给调用方
            logger.log(f"[极速狂飙] 录制 meta 写入失败: {exc!r}", "WARNING")


def make_session_dir(root: Path) -> Path:
    """按时间戳建会话目录（与 debug 会话同构），供调用方传入 DriveRecorder。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(root) / stamp