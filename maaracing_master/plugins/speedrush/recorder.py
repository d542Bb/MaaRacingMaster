# -*- coding: utf-8 -*-
"""speedrush 驾驶演示录制器：帧 + 程序叠加图 + 物理手柄 + 同源时间戳。

**产出形态**（一个会话 = 一个目录，与控制取证会话同一套布局，见
``session_layout``）::

    <会话>/
      meta.json        会话元信息、来源、计数、丢帧数
      frames.jsonl     逐帧索引：seq / frame_id / ts_ns / age_ms / file / hud
      raw/0001_raw.jpg 原始帧（录制帧率，标准图像语义）
      0001_hud.jpg     程序叠加图（按 ``hud_interval_s`` 节流；缺失=该帧未渲染）
      hud/hud.jsonl    驾驶页 HUD 读数（HudObserver 落，同会话对齐）
      pads.jsonl       手柄样本序列：ts_ns / 摇杆 / 扳机 / 按键

**帧与手柄为何分开落盘、离线对齐**：两者的合适频率不同——帧与实机推理同为 30fps
（"K 帧堆叠"的时间跨度必须一致），而手柄要 ≥100Hz（转向延迟 τ 只能从高频数据里读出来，
30fps 的 33ms 分辨率不够）。所以各按各的频率录、各自带同源 ``perf_counter_ns`` 时间戳，
由离线步骤对齐，而不是在采集时就绑成一一对应。

**叠加图为什么由录制器写**：人工局的画面只在录制器里出现一次，叠加图必须引用
同一份原帧、同一拍的决策快照，否则就是第二份拷贝 + 无法对齐的时延。故录制器
接管「原帧 + 叠加图 + 索引」三者，索引行里 ``hud`` 就是该帧叠加图的实际文件名；
渲染失败或未节流到就是 ``None``，不写一个指向不存在文件的指针。

**反压纪律**（调研同类实时项目的教训）：帧入队用 ``put_nowait``，队列满即丢帧并计数，
**绝不阻塞采集侧**——落盘慢一秒不该拖慢控制循环。丢帧数进 meta，异常可查。
叠加图渲染在落盘线程串行进行（与控制循环隔离），失败只计数不抛。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cv2

from maaracing_master.core import xinput
from maaracing_master.core.image_io import write_rgb
from maaracing_master.core.logger import logger
from maaracing_master.plugins.speedrush.session_layout import (
    SOURCE_MANUAL,
    build_meta,
    hud_relpath,
    raw_relpath,
)

# 帧队列容量：约两秒（30fps）。满则丢帧计数，不阻塞采集侧。
_FRAME_QUEUE_MAX = 64

# 等控制拍交来叠加快照的上限：采集与决策在同一拍内先后发生，正常是微秒级；
# 超时只说明这一帧没有可配对的决策快照（感知缺席），按"无叠加图"落盘。
_HUD_SNAP_WAIT_S = 0.25

# 连续读不到手柄的容忍次数：5 次 @200Hz ≈ 25ms，够跨过一次驱动抖动，
# 又不至于在真的没插手柄时空转。**必须是连续**——按累计计数的话，整局里零星的
# 读取失败会攒够阈值后把采样线程永久停掉（实现曾与注释不符，2026-10-30 复盘发现）。
_PAD_NONE_LIMIT = 5
# 未读到手柄的告警节流：失败时每秒会过 200 次采样，压到每 5 秒最多一条
_PAD_WARN_INTERVAL_S = 5.0
# 槽位探测：摇杆/扳机超过此幅度才算"有明显输入"。虚拟手柄在程序不驱动时恒为
# 静止全零，用它把物理手柄从多槽位里挑出来。
_PAD_ACTIVE_THRESHOLD = 2000
# 多槽位且始终无输入时的兜底等待：超时后锁首个并告警，不无限期不采样本
_PAD_SLOT_PROBE_TIMEOUT_S = 5.0


def _pad_active(st: xinput.PadState) -> bool:
    """该槽位是否出现"明显输入"——多槽位时用它把物理手柄从静止的虚拟手柄里挑出来。

    阈值远低于 XInput 摇杆死区（约 7800），轻微碰杆即算；扳机按 >10 计（0~255），
    按键非零也算。静止的虚拟设备恒为全零，故不会误判为活跃。
    """
    lx, ly = st.left_stick
    rx, ry = st.right_stick
    return (abs(lx) > _PAD_ACTIVE_THRESHOLD or abs(ly) > _PAD_ACTIVE_THRESHOLD
            or abs(rx) > _PAD_ACTIVE_THRESHOLD or abs(ry) > _PAD_ACTIVE_THRESHOLD
            or st.left_trigger > 10 or st.right_trigger > 10 or st.buttons != 0)


class DriveRecorder:
    """驾驶演示录制器（线程安全；start/stop 幂等）。

    用法：``start()`` → 主循环反复 ``record_frame()`` → ``stop()``。
    帧写盘与手柄采样都在各自线程，调用方只做非阻塞入队。

    ``hud_render``（可选）：``(frame_rgb, snap) -> BGR 图像`` 的渲染器。注入后
    录制器按 ``hud_interval_s`` 节流产程序叠加图；快照由控制拍经
    ``attach_hud_snap`` 交回（同拍同帧，见模块头注）。不注入=不产叠加图。

    场景分层元信息：``phase``（驾驶阶段号 1/2）与 ``round_no``（第几轮）由调用方注入，
    写进 meta。**天光（晴/阴）尚未采集**——它没有可信来源：既无天气检测代码，也没做成
    配置项。要记它得先有来源（GUI 配置项或实测出的图像判据），否则只会得到一个恒空的
    字段；方案 §10.1 把它列为该记的项，缺口记在这里而不是留个假字段。
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        jpeg_quality: int = 85,
        pad_poll_hz: float = 200.0,
        pad_slot: int | None = None,
        phase: int | None = None,
        round_no: int | None = None,
        group_id: str | None = None,
        hud_render: Callable[[Any, dict], Any] | None = None,
        hud_interval_s: float = 0.1,
        hud_jpeg_quality: int = 85,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.jpeg_quality = int(jpeg_quality)
        self.pad_poll_hz = float(pad_poll_hz)
        # 派发时刻捕获的业务组号（驾驶阶段卡）；worker 不读线程局部当前组。
        self._group_id = group_id
        # None = 每次采样自动找第一个已连接手柄（适配用户插哪个槽都行）
        self.pad_slot = pad_slot
        # 场景分层用：本次会话属于哪个驾驶阶段、第几轮
        self.phase = phase
        self.round_no = round_no
        # 程序叠加图（可缺省）：渲染器 + 节流窗 + 质量
        self._hud_render = hud_render
        self._hud_interval = max(0.0, float(hud_interval_s))
        self.hud_jpeg_quality = int(hud_jpeg_quality)

        self._frame_q: queue.Queue[tuple[int, int, int, float, Any, bool]] = queue.Queue(
            _FRAME_QUEUE_MAX)
        self._stop = threading.Event()
        self._frame_thread: threading.Thread | None = None
        self._pad_thread: threading.Thread | None = None

        self._frames_written = 0
        self._frames_dropped = 0
        self._write_errors = 0
        self._hud_written = 0
        self._pad_samples = 0
        self._pad_none_streak = 0
        self._pad_warn_last = 0.0
        self._seq_counter = 0
        self._hud_last = float("-inf")   # 首帧即取样
        self._started_ns = 0
        self._started_iso = ""
        self._running = False
        self._lock = threading.Lock()
        self._frame_size: tuple[int, int] | None = None  # (w, h)

        # 叠加快照：采集拍入队时登记帧号，控制拍算完后按帧号交回。
        # 只登记被节流选中的帧，故映射大小受采样率约束，不会随录制时长增长。
        self._snap_cv = threading.Condition()
        self._awaiting_hud: set[int] = set()
        self._hud_snaps: dict[int, dict | None] = {}

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def hud_pairs_enabled(self) -> bool:
        return self._hud_render is not None

    def _log(self, msg: str, level: str = "INFO") -> None:
        """阶段正文日志：派发时捕获的组号在 worker 线程里显式透传。"""
        logger.log(msg, level, group_id=self._group_id)

    @property
    def stats(self) -> dict[str, int]:
        """实时计数（供 GUI 状态展示；不阻塞）。"""
        return {
            "frames_written": self._frames_written,
            "frames_dropped": self._frames_dropped,
            "hud_written": self._hud_written,
            "write_errors": self._write_errors,
            "pad_samples": self._pad_samples,
        }

    def start(self) -> None:
        if self._running:
            return
        (self.out_dir / "raw").mkdir(parents=True, exist_ok=True)
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
        self._log(f"[极速狂飙] 录制开始 → {self.out_dir}", "INFO")

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
        self._log(
            f"[极速狂飙] 录制结束：帧 {self._frames_written} 张"
            f"（丢 {self._frames_dropped}）、叠加图 {self._hud_written} 张、"
            f"手柄样本 {self._pad_samples} 条",
            "INFO" if self._frames_dropped == 0 else "WARNING",
        )

    # ---------- 采集侧（调用方线程） ----------

    def record_frame(self, frame_rgb: Any, *, frame_id: int, ts_ns: int,
                     age_ms: float = 0.0) -> None:
        """非阻塞存入一帧。队列满则丢帧并计数——不得阻塞调用方。

        ``frame_rgb`` 是内部 RGB 帧（``ctx.capture`` 的契约）。落盘由
        ``image_io.write_rgb`` 转成标准图像语义后写——本方法不得自行 imwrite，
        直写会把 R/B 写反（imwrite 期望 BGR 序）。

        ``frame_id`` 与 ``ts_ns`` **必填**，且必须来自 ``ctx.capture.frame_with_age()``：
        那是帧到达采集回调的时刻。调用侧自打的时间戳是"读取时刻"，与真实采集时刻差
        一个帧龄——对训练标签而言是实打实的时序偏差，故不留默认值这种可误用的口子。

        叠加图节流在这里（采集侧）判定：只有被选中的帧才等控制拍的快照，
        其余帧不占登记位，落盘线程也不会为它们多等。
        """
        if not self._running:
            return
        h, w = frame_rgb.shape[:2]
        if self._frame_size is None:
            self._frame_size = (int(w), int(h))
        sampled = False
        if self._hud_render is not None:
            now = time.perf_counter()
            if now - self._hud_last >= self._hud_interval:
                self._hud_last = now
                sampled = True
        if sampled:
            with self._snap_cv:
                self._awaiting_hud.add(int(frame_id))
        # 序列号在采集侧单调递增：丢帧不使编号跳变，缺口数即丢帧证据
        self._seq_counter += 1
        try:
            self._frame_q.put_nowait(
                (self._seq_counter, int(frame_id), int(ts_ns), float(age_ms),
                 frame_rgb, sampled))
        except queue.Full:
            self._frames_dropped += 1
            if sampled:
                with self._snap_cv:
                    self._awaiting_hud.discard(int(frame_id))

    def attach_hud_snap(self, frame_id: int, snap: dict | None) -> None:
        """控制拍交回本帧的决策快照（None=本拍无决策结果）。

        只对被节流选中的帧生效：未选中的帧直接丢弃，映射不随录制时长增长。
        快照由控制拍新建、交出后不再改写，落盘线程延迟渲染读到的是同一拍内容。
        """
        with self._snap_cv:
            if frame_id in self._awaiting_hud:
                self._hud_snaps[frame_id] = snap
                self._snap_cv.notify_all()

    def _take_snap(self, frame_id: int) -> dict | None:
        """取本帧快照；控制拍尚未交回时短等（正常微秒级，缺席即放弃）。"""
        deadline = time.perf_counter() + _HUD_SNAP_WAIT_S
        with self._snap_cv:
            while frame_id not in self._hud_snaps:
                remain = deadline - time.perf_counter()
                if remain <= 0:
                    break
                self._snap_cv.wait(remain)
            self._awaiting_hud.discard(frame_id)
            return self._hud_snaps.pop(frame_id, None)

    def _discard_snap(self, frame_id: int) -> None:
        """本帧原图写失败，叠加图无从谈起：清掉登记，不让映射滞留。"""
        with self._snap_cv:
            self._awaiting_hud.discard(frame_id)
            self._hud_snaps.pop(frame_id, None)

    # ---------- 落盘线程 ----------

    def _frame_worker(self) -> None:
        frames_jsonl = self.out_dir / "frames.jsonl"
        with frames_jsonl.open("w", encoding="utf-8") as fj:
            while True:
                try:
                    seq, frame_id, ts_ns, age_ms, frame, sampled = \
                        self._frame_q.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                name = raw_relpath(seq)
                try:
                    # 走 image_io.write_rgb（内部 RGB → 标准图像语义）：直写
                    # imwrite 会把 R/B 写反（imwrite 期望 BGR 序），而白灰画面
                    # 上看不出来——已录的三批会话就是这么错的。
                    ok = write_rgb(self.out_dir / name, frame,
                                   jpeg_quality=self.jpeg_quality)
                except Exception as exc:  # noqa: BLE001 —— 单帧写失败不该终止录制
                    self._write_errors += 1
                    if sampled:
                        self._discard_snap(frame_id)
                    logger.log(f"[极速狂飙] 录制写帧失败（累计 {self._write_errors}）"
                               f" seq={seq}: {exc!r}",
                               "WARNING" if self._write_errors == 1 else "DEBUG")
                    continue
                if not ok:
                    self._write_errors += 1
                    if sampled:
                        self._discard_snap(frame_id)
                    logger.log(f"[极速狂飙] 录制写帧返回失败（累计 {self._write_errors}）"
                               f" seq={seq}",
                               "WARNING" if self._write_errors == 1 else "DEBUG")
                    continue
                hud_name = None
                if sampled:
                    snap = self._take_snap(frame_id)
                    if snap is not None:
                        hud_name = self._write_hud_pair(seq, frame, snap)
                # frame_id 是 WGC 帧号，跨源对齐用它；ts_ns 是采集时刻（非写入时刻）；
                # hud 只写实际落盘的文件名，渲染缺席/失败就是 None（不留悬空指针）
                fj.write(json.dumps(
                    {"seq": seq, "frame_id": frame_id, "ts_ns": ts_ns,
                     "age_ms": round(age_ms, 3), "file": name,
                     "hud": hud_name},
                    ensure_ascii=False) + "\n")
                self._frames_written += 1

    def _write_hud_pair(self, seq: int, frame: Any, snap: dict) -> str | None:
        """渲染并落一张程序叠加图；失败计数返回 None（不碰主路）。"""
        render = self._hud_render
        if render is None:                     # 调用方只在启用时走到这里
            return None
        try:
            # 渲染器要的帧形状按本帧实际尺寸给（投影标记依赖它）
            canvas = render(frame, dict(snap, frame_shape=frame.shape))
            name = hud_relpath(seq)
            ok = cv2.imwrite(
                str(self.out_dir / name), canvas,
                [cv2.IMWRITE_JPEG_QUALITY, self.hud_jpeg_quality])
        except Exception as exc:  # noqa: BLE001 —— 观测件失败不终止录制
            ok = False
            detail = repr(exc)
        else:
            detail = "imwrite 返回失败"
        if ok:
            self._hud_written += 1
            return name
        self._write_errors += 1
        self._log(f"[极速狂飙] 录制叠加图失败（累计 {self._write_errors}）"
                  f" seq={seq}: {detail}",
                  "WARNING" if self._write_errors == 1 else "DEBUG")
        return None

    def _pad_worker(self) -> None:
        """高频采样物理手柄并逐行落盘。

        采样线程是 pads.jsonl 的唯一写者，故无需加锁。
        """
        interval = 1.0 / max(self.pad_poll_hz, 1.0)
        probe_deadline = time.monotonic() + _PAD_SLOT_PROBE_TIMEOUT_S
        with (self.out_dir / "pads.jsonl").open("w", encoding="utf-8") as f:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                st, countable = self._poll_pad(probe_deadline)
                if st is not None:
                    self._pad_none_streak = 0
                    f.write(json.dumps({
                        "ts_ns": time.perf_counter_ns(),
                        "lx": st.left_stick[0], "ly": st.left_stick[1],
                        "rx": st.right_stick[0], "ry": st.right_stick[1],
                        "lt": st.left_trigger, "rt": st.right_trigger,
                        "btn": st.buttons,
                    }, ensure_ascii=False) + "\n")
                    self._pad_samples += 1
                elif countable:
                    self._pad_none_streak += 1
                    now = time.monotonic()
                    if (self._pad_none_streak == 1
                            and now - self._pad_warn_last >= _PAD_WARN_INTERVAL_S):
                        self._pad_warn_last = now
                        self._log(
                            "[极速狂飙] 录制中未读到物理手柄——"
                            "请确认手柄已连接且未被虚拟手柄占用", "WARNING")
                    if self._pad_none_streak >= _PAD_NONE_LIMIT:
                        # 退出必须留痕：此前只靠 meta 里偏小的样本数反推，
                        # 事后分不清"人没推"和"采样已停"。
                        self._log(
                            f"[极速狂飙] 手柄连续 {_PAD_NONE_LIMIT} 次读不到，停止采样"
                            f"（本局已录 {self._pad_samples} 条，之后无手柄数据）",
                            "WARNING")
                        break  # 连续读不到即停止采样，不空转刷日志
                # 扣掉本次采样耗时，避免采样率随调用开销漂移
                time.sleep(max(0.0, interval - (time.perf_counter() - t0)))

    def _poll_pad(self, probe_deadline: float) -> tuple[xinput.PadState | None, bool]:
        """读当前该采样的手柄；返回 ``(状态, 是否计入"读不到")``。

        已锁定槽位时只读那一个。未锁定时轮询全部槽位并按证据锁定：

        - 只有一个已连接槽位 → 直接锁定，不必等输入（无歧义）；
        - 多个槽位 → 锁定**第一个出现明显输入**的。虚拟手柄在程序不驱动时恒为
          静止全零，永远不满足条件，故不会被误锁——这正是旧实现"锁定第一个能
          读到的槽位"会踩的坑：上一局控制模式残留的虚拟设备占着 0 号槽时，
          整局录到的都是全零；
        - 多槽位且超时仍无任何输入 → 锁首个并告警（XInput 层面无法再区分物理
          与虚拟，只能留一条可查的痕迹）。

        探测期返回 ``(None, False)``：不写样本（避免同文件里混槽位），也不计入
        连续失败（否则 5 次探测就把采样线程判死）。
        """
        if self.pad_slot is not None:
            return xinput.read_state(self.pad_slot), True
        states = [st for st in (xinput.read_state(i) for i in range(4))
                  if st is not None]
        if not states:
            return None, True
        if len(states) == 1:
            self.pad_slot = states[0].index
            self._log(f"[极速狂飙] 手柄槽位锁定 {self.pad_slot}（唯一已连接）", "INFO")
            return states[0], True
        active = [st for st in states if _pad_active(st)]
        if active:
            self.pad_slot = active[0].index
            self._log(
                f"[极速狂飙] 手柄槽位锁定 {self.pad_slot}"
                f"（{len(states)} 个候选槽位，此槽有输入活动）", "INFO")
            return active[0], True
        if time.monotonic() >= probe_deadline:
            self.pad_slot = states[0].index
            self._log(
                f"[极速狂飙] {len(states)} 个手柄槽位在"
                f" {_PAD_SLOT_PROBE_TIMEOUT_S:.0f}s 内均无输入活动，锁定首个"
                f" {self.pad_slot}——若本局杆值恒为零，检查是否锁到了虚拟手柄",
                "WARNING")
            return states[0], True
        return None, False

    # ---------- 元信息 ----------

    def _write_meta(self, reason: str) -> None:
        w, h = self._frame_size or (0, 0)
        meta = build_meta(
            source=SOURCE_MANUAL,
            started_at=self._started_iso,
            ended_at=datetime.now().isoformat(timespec="seconds"),
            monotonic_start_ns=self._started_ns,
            phase=self.phase,
            round_no=self.round_no,
            frame_w=w,
            frame_h=h,
            jpeg_quality=self.jpeg_quality,
            hud_jpeg_quality=(self.hud_jpeg_quality
                              if self._hud_render is not None else None),
            frames_written=self._frames_written,
            frames_dropped=self._frames_dropped,
            hud_written=self._hud_written,
            write_errors=self._write_errors,
            pad_slot=self.pad_slot,
            pad_poll_hz=self.pad_poll_hz,
            pad_samples=self._pad_samples,
            stop_reason=reason,
        )
        try:
            (self.out_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 —— meta 写失败不该抛给调用方
            logger.log(f"[极速狂飙] 录制 meta 写入失败: {exc!r}", "WARNING")


def make_session_dir(root: Path, tag: str | None = None,
                     stamp: str | None = None) -> Path:
    """按运行编号建会话目录（与 debug 会话同构），供调用方传入 DriveRecorder。

    目录名 = ``<运行编号>[_<标签>]``：时间戳恒在最前，标签按用途追加在后，
    与 ``logs/`` 会话目录、``debug/`` 其它会话同一规则。``stamp`` 由调用方
    传入（通常是日志会话 token，让日志与语料目录同名）；缺省取当前时刻。
    ``tag`` 进目录名后缀（如 ``20260916_120000_manual``）：人工驾驶语料局与
    纯时间戳会话一眼可分。"""
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{stamp}_{tag}" if tag else stamp
    return Path(root) / name
