#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能采样器——系统/进程资源快照的常驻采集与落盘（记录流三分方案的性能流）。

三方供数契约（sidecar 为聚合方）：
- 本模块（core）→ 资源快照：系统 CPU/内存、受管进程（自身 + 嗅探到的模拟器
  类进程）CPU/内存、GPU 系统级读数；
- 活动模块 → 领域项（按 core/perf_contract.py 的声明行上报，core 负责校验
  与判级；模块不再自定等级）；
- sidecar → 聚合：带 schema_version 的 perf 载荷；采样器不可用时显式
  available=False，不伪装成零值或正常。

落盘三档（logs/<会话>/perf.jsonl，受「日志记录」开关管辖）：
- 低频背景：每 keep_every 个样本落一个（默认 5s 一个）；
- 事件倾倒：模块经 record_event 报事件（如「控制拍间隔超阈值」），前后窗
  口内的样本逐个补落——**每样本只写一次**（sample_id 唯一，已落永不重写，
  重叠窗口靠同一标记去重）；窗口结束写一条事件记录引用实际落盘区间；
- 收尾全量：会话关闭时把环内未落样本一次性落盘（可配关）。

指标现实化：
- GPU 走 nvidia-smi 子进程（带超时）：失败/超时/不可用与零值显式区分
  （status 字段），读数带龄期；NV 驱动采样窗口 ~1/6–1s，只作整体忙碌度；
- psutil cpu_percent 首个非阻塞读数无意义——启动即基线初始化，占用率一律
  差分口径；
- 采样器故障隔离：指标构建异常降级为带 error 的样本，不拖垮状态服务。

时钟轴：样本带 ts_wall_ns（对表）/ ts_mono_ns（算间隔）双时钟 + sample_id
（进程内单调，永不重置）；性能流不进玩法流水（消费者与对表轴不同）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

__all__ = ["PerfSampler", "perf_sampler", "ensure_perf_sampler_started",
           "build_perf_payload"]

SCHEMA_VERSION = 1

# 受管进程嗅探：模拟器家族的可执行名子串（小写匹配）。这是环境级进程清单，
# 与任何玩法模块无关；命中即纳入逐秒 CPU/内存读数。
DEFAULT_WATCH_PATTERNS = (
    "mumu", "nemu",            # MuMu 系
    "dnplayer", "ldbox",       # 雷电系
    "nox",                     # 夜神
    "hd-player", "bluestacks", # 蓝叠系
)

_GPU_QUERY = ["--query-gpu=utilization.gpu,memory.used,memory.total",
              "--format=csv,noheader,nounits"]


class _GpuUnavailable(Exception):
    """nvidia-smi 不存在：永久不可用（不重试）。"""


class PerfSampler:
    """常驻采样线程 + 会话生命周期监听（协议见 core/logger.py）。"""

    name = "perf_sampler"

    def __init__(self, *, interval_s: float = 1.0, ring_cap: int = 1800,
                 gpu_every: int = 5, gpu_timeout_s: float = 1.0,
                 keep_every: int = 5, scan_every: int = 10,
                 watch_patterns: tuple[str, ...] = DEFAULT_WATCH_PATTERNS,
                 dump_on_close: bool = True,
                 gpu_runner: Callable[[float], str] | None = None):
        self._interval_s = interval_s
        self._ring_cap = ring_cap
        self._gpu_every = max(1, gpu_every)
        self._gpu_timeout_s = gpu_timeout_s
        self._keep_every = max(1, keep_every)
        self._scan_every = max(1, scan_every)
        self._watch = tuple(p.lower() for p in watch_patterns)
        self._dump_on_close = dump_on_close
        # gpu_runner 注入点（测试/替代实现）：参数=超时秒，返回 nvidia-smi stdout
        self._gpu_runner = gpu_runner or self._nvidia_smi
        self._lk = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_seq = 0
        self._evt_seq = 0
        self._ring: deque[dict] = deque(maxlen=ring_cap)
        self._latest: dict | None = None
        self._pending: list[dict] = []      # 已受理待倾倒的事件
        self._active: list[dict] = []       # 后置窗口进行中的事件
        # 会话态（logger 生命周期驱动）
        self._session_token: str | None = None
        self._session_path: Path | None = None
        # 文件句柄仅采样线程与收尾路径写，统一过 _file_lk
        self._file = None
        self._file_lk = threading.Lock()
        self._close_requested = False
        # 受管进程表：pid → (psutil.Process, name)
        self._procs: dict[int, tuple[Any, str]] = {}
        self._last_scan = -1e9
        self._tick_count = 0
        self._gpu: dict = {"status": "unavailable", "util_pct": None,
                           "mem_used_mb": None, "mem_total_mb": None,
                           "ts_mono": None}
        self._gpu_dead = False
        self._status_error: str | None = None
        # 领域行取值口（sidecar 在模块启动/停止时登记，采样器只持一个可调用对象，
        # 不认识任何模块——分层红线：通用层不指向业务）
        self._domain_reader: Callable[[], list[dict]] | None = None
        self._domain_slow_warn_at = -1e9
        # 落盘失败：待补缺口区间（起止 sample_id）+ 累计/连续计数。
        # 状态机见 _write_lines：标记与当前样本都成功才算这一拍落盘完成。
        self._persist_gap: tuple[int, int] | None = None
        self._persist_failures = 0
        self._persist_consecutive = 0

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        """起采样线程（幂等）。返回是否处于运行态。"""
        with self._lk:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._close_requested = False
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="perf-sampler")
            self._thread.start()
        self._prime_baseline()
        return True

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout_s)
        with self._file_lk:
            self._close_file_locked()

    @property
    def running(self) -> bool:
        th = self._thread
        return th is not None and th.is_alive() and not self._stop.is_set()

    # ---------- 会话生命周期回调（logger 调用） ----------

    def on_session_open(self, session_dir: Path, token: str) -> None:
        """开新会话：重置环形缓冲（关闸期样本永不进入新会话文件）、开句柄。"""
        path = Path(session_dir) / "perf.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a", encoding="utf-8", buffering=1)
        except OSError:
            fh = None
        with self._lk:
            self._session_token = token
            self._session_path = path
            self._ring.clear()
            self._active.clear()
            self._pending.clear()
        with self._file_lk:
            self._close_file_locked()
            self._file = fh
            # 新会话新文件：缺口与计数都从头记（旧文件的缺口已随旧句柄关闭）
            self._persist_gap = None
            self._persist_failures = 0
            self._persist_consecutive = 0

    def on_session_closing(self, deadline_monotonic: float):
        """会话关闭：未落样本收尾全量（可配关）、未完事件记 incomplete、关句柄。"""
        ok = True
        try:
            with self._lk:
                token = self._session_token
                pending = list(self._pending)
                active = list(self._active)
                self._pending.clear()
                self._active.clear()
            lines: list[str] = []
            if self._dump_on_close:
                for s in self._ring:
                    if not s.get("_p"):
                        lines.append(self._sample_line(s))
                        s["_p"] = True
            for evt in pending:
                lines.append(self._event_line(evt, incomplete=True,
                                              reason="session_closing"))
            for evt in active:
                lines.append(self._event_line(evt, incomplete=True,
                                              reason="session_closing"))
            if token is not None:
                lines.append(json.dumps(
                    {"v": SCHEMA_VERSION, "type": "end", "token": token,
                     "samples": self._sample_seq,
                     "ts_wall_ns": time.time_ns(),
                     "ts_mono_ns": time.monotonic_ns()},
                    ensure_ascii=False, separators=(",", ":")))
            with self._file_lk:
                fh = self._file
                if fh is not None:
                    # 先补历史缺口，再写收尾行；收尾行失败同样计入缺口
                    self._write_gap_locked(fh)
                    if lines:
                        try:
                            for ln in lines:
                                fh.write(ln + "\n")
                        except OSError:
                            ok = False
                            self._persist_failures += 1
                    # 收尾再试一次：仍失败就留在摘要里，不静默
                    if self._persist_gap is not None and not self._write_gap_locked(fh):
                        ok = False
                self._close_file_locked()
        except Exception:  # noqa: BLE001 —— 收尾异常如实记账，不拖垮 logger 关闭
            ok = False
        finally:
            with self._lk:
                self._session_token = None
                self._session_path = None
                self._close_requested = False
        if self._persist_gap is not None:
            ok = False                      # 缺口没补完，不宣称数据已完整落盘
        return type("CloseSummary", (), {"ok": ok, "details": {
            "persist_failures": self._persist_failures,
            "persist_gap": self._persist_gap,
        }})()

    # ---------- 领域面取值口 ----------

    def set_domain_reader(self, reader: Callable[[], list[dict]] | None) -> None:
        """登记/注销领域行取值口（sidecar 在模块启动、停止时调用）。

        reader 契约：返回已规范化的行数组（`perf_contract.normalize` 的产物），
        **不抛异常**——异常处理在登记侧完成，采样器只负责调用与计时。
        传 None 表示当前没有活动模块：样本只带资源面。
        """
        self._domain_reader = reader

    def _sample_domain(self) -> tuple[list[dict], str | None]:
        """取一拍领域行。每拍读一次引用：中途换模块不会拿到半新半旧的数据。"""
        reader = self._domain_reader
        if reader is None:
            return [], None
        try:
            rows = reader()
        except Exception as exc:  # noqa: BLE001 —— 取值口违约不能拖垮采样线程
            return [], repr(exc)
        return list(rows or []), None

    # ---------- 模块事件接口 ----------

    def record_event(self, source: str, reason: str,
                     detail: dict | None = None,
                     pre_s: float = 10.0, post_s: float = 10.0) -> int | None:
        """受理一次事件倾倒（模块线程调用，零 IO；采样线程择机落盘）。"""
        with self._lk:
            if self._stop.is_set():
                return None
            self._evt_seq += 1
            eid = self._evt_seq
            self._pending.append({
                "event_id": eid, "source": source, "reason": reason,
                "detail": detail, "pre_s": pre_s, "post_s": post_s,
                "triggered_mono": time.monotonic(),
                "ts_wall_ns": time.time_ns(),
            })
        self._wake.set()
        return eid

    # ---------- 读侧（sidecar） ----------

    def snapshot(self) -> dict:
        """最新样本的公开快照（原子替换；不可用显式 available=False）。"""
        with self._lk:
            snap = self._latest
            err = self._status_error
        if snap is None:
            return {"schema_version": SCHEMA_VERSION, "available": False,
                    "error": err}
        out = {k: v for k, v in snap.items() if not k.startswith("_")}
        out["schema_version"] = SCHEMA_VERSION
        out["available"] = True
        return out

    def stats(self) -> dict:
        with self._lk:
            return {"running": self.running,
                    "samples": self._sample_seq,
                    "events": self._evt_seq,
                    "ring": len(self._ring),
                    "session": self._session_token,
                    "error": self._status_error,
                    "persist_failures": self._persist_failures,
                    "persist_consecutive": self._persist_consecutive,
                    "persist_gap": self._persist_gap}

    # ---------- 内部：采样循环 ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001 —— 故障隔离：单拍失败不退线程
                with self._lk:
                    self._status_error = repr(exc)
            spent = time.monotonic() - t0
            remain = self._interval_s - spent
            if remain > 0:
                self._wake.wait(remain)
                self._wake.clear()

    def tick_once(self) -> dict:
        """单拍采样（公开给测试手动驱动）：构建→入环→事件/背景落盘。"""
        sample = self._build_sample()
        with self._lk:
            self._tick_count += 1
            tick_no = self._tick_count
            self._ring.append(sample)
            self._latest = sample
            pending = list(self._pending)
            self._pending.clear()
        lines: list[str] = []
        now_mono = sample["ts_mono_ns"] / 1e9
        # 事件前置窗口：补落未落样本（每样本只写一次）
        for evt in pending:
            cutoff = now_mono - evt["pre_s"]
            oldest = (self._ring[0]["ts_mono_ns"] / 1e9
                      if self._ring else now_mono)
            from_id = None
            for s in self._ring:
                if s["ts_mono_ns"] / 1e9 < cutoff:
                    continue
                if from_id is None:
                    from_id = s["sample_id"]
                if not s.get("_p"):
                    lines.append(self._sample_line(s))
                    s["_p"] = True
            if from_id is None:
                from_id = sample["sample_id"]
            evicted = (max(0, int((oldest - cutoff) / self._interval_s))
                       if oldest > cutoff else 0)
            evt.update({"_from_id": from_id, "_evicted": evicted,
                        "post_until": now_mono + evt["post_s"]})
            with self._lk:
                self._active.append(evt)
        # 后置窗口/背景档落盘
        with self._lk:
            active = list(self._active)
        in_window = any(evt["post_until"] >= now_mono for evt in active)
        write_this = in_window or (sample["sample_id"] % self._keep_every == 0)
        if write_this and not sample.get("_p"):
            lines.append(self._sample_line(sample))
            sample["_p"] = True
        # 窗口到期：写事件记录（引用实际落盘区间 + 缺失说明）
        expired = [evt for evt in active if evt["post_until"] < now_mono]
        if expired:
            with self._lk:
                self._active = [e for e in self._active
                                if e["post_until"] >= now_mono]
            for evt in expired:
                to_id = sample["sample_id"]
                lines.append(self._event_line(evt, incomplete=False,
                                              reason=None, to_id=to_id))
        if lines:
            self._write_lines(lines, sample_id=sample["sample_id"])
        return sample

    # ---------- 内部：指标 ----------

    def _prime_baseline(self) -> None:
        """psutil 基线初始化：首个非阻塞读数无意义，先各读一次丢弃。"""
        try:
            import psutil
        except ImportError:
            with self._lk:
                self._status_error = "psutil 不可用"
            return
        try:
            psutil.cpu_percent(None)
            p = psutil.Process(os.getpid())
            p.cpu_percent(None)
            with self._lk:
                self._procs[p.pid] = (p, "self")
        except Exception:  # noqa: BLE001 —— 基线失败不阻断启动，首拍按 0 记
            pass

    #: 领域行取值耗时告警线（毫秒）。契约要求取值口纯读、常数时间；超线说明
    #: 模块在采样线程里干了别的事，先让人看见，别静默变慢。
    DOMAIN_SLOW_MS = 5.0

    def _build_sample(self) -> dict:
        """一拍样本：样本号与时间戳取 tick 开始那一刻，三个面各自计时。

        三个面不是同一瞬间的快照（sys 先采、domain 后采），`cost_ms` 记各自
        耗时偏移——事后能判断「这一拍慢在哪一面」，而不是把时间差当采样误差。
        """
        with self._lk:
            self._sample_seq += 1
            sid = self._sample_seq
        t_tick = time.monotonic()
        sample = {
            "v": SCHEMA_VERSION, "type": "sample", "sample_id": sid,
            "ts_wall_ns": time.time_ns(), "ts_mono_ns": time.monotonic_ns(),
            "_p": False,
        }
        cost: dict[str, float] = {}

        t0 = time.monotonic()
        sample.update(self._sample_system())
        cost["sys"] = (time.monotonic() - t0) * 1000.0

        t0 = time.monotonic()
        sample["procs"] = self._sample_procs()
        cost["procs"] = (time.monotonic() - t0) * 1000.0

        t0 = time.monotonic()
        if sid % self._gpu_every == 0:
            self._refresh_gpu()
        gpu = dict(self._gpu)
        ts = gpu.get("ts_mono")
        gpu["age_s"] = None if ts is None else round(time.monotonic() - ts, 1)
        gpu.pop("ts_mono", None)
        sample["gpu"] = gpu
        cost["gpu"] = (time.monotonic() - t0) * 1000.0

        t0 = time.monotonic()
        rows, domain_err = self._sample_domain()
        sample["domain"] = rows
        if domain_err:
            sample["domain_error"] = domain_err
        cost["domain"] = (time.monotonic() - t0) * 1000.0

        cost["total"] = (time.monotonic() - t_tick) * 1000.0
        sample["cost_ms"] = {k: round(v, 3) for k, v in cost.items()}
        self._warn_slow_domain(cost["domain"])
        return sample

    def _warn_slow_domain(self, ms: float) -> None:
        """取值口超阈值告警（60 秒最多一条，避免刷屏）。"""
        if ms < self.DOMAIN_SLOW_MS:
            return
        now = time.monotonic()
        if now - self._domain_slow_warn_at < 60.0:
            return
        self._domain_slow_warn_at = now
        from maaracing_master.core.logger import logger
        logger.log(f"[perf] 领域行取值耗时 {ms:.1f}ms（阈值 "
                   f"{self.DOMAIN_SLOW_MS}ms）：模块取值口应纯读、常数时间",
                   "WARNING")

    def _sample_system(self) -> dict:
        try:
            import psutil
            cpu = psutil.cpu_percent(None)
            mem = psutil.virtual_memory()
            return {"cpu_pct": round(cpu, 1),
                    "mem_pct": round(mem.percent, 1),
                    "mem_used_gb": round(mem.used / 2**30, 2),
                    "mem_total_gb": round(mem.total / 2**30, 2)}
        except Exception as exc:  # noqa: BLE001 —— 指标失败显式降级
            return {"sys_error": repr(exc)}

    def _sample_procs(self) -> list[dict]:
        try:
            import psutil
        except ImportError:
            return []
        out = []
        now = time.monotonic()
        if now - self._last_scan >= self._scan_every * self._interval_s:
            self._last_scan = now
            self._scan_procs(psutil)
        for pid, (p, name) in list(self._procs.items()):
            try:
                cpu = p.cpu_percent(None)      # 差分口径（上次调用以来）
                mem = p.memory_info().rss / 2**20
                out.append({"pid": pid, "name": name,
                            "cpu_pct": round(cpu, 1), "mem_mb": round(mem, 1)})
            except psutil.NoSuchProcess:
                self._procs.pop(pid, None)
            except psutil.Error:
                continue                        # 权限/临时失败：本拍跳过不除名
        out.sort(key=lambda r: -r["cpu_pct"])
        return out

    def _scan_procs(self, psutil) -> None:
        """嗅探受管进程（模拟器家族 + 自身）；新发现先做 cpu_percent 基线。"""
        try:
            mine = os.getpid()
            for info in psutil.process_iter(["pid", "name"]):
                name = (info.info.get("name") or "")
                low = name.lower()
                if not any(pat in low for pat in self._watch):
                    continue
                pid = info.info["pid"]
                if pid in self._procs or pid == mine:
                    continue
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent(None)        # 基线初始化
                    self._procs[pid] = (p, name)
                except psutil.Error:
                    continue
        except psutil.Error:
            pass

    def _refresh_gpu(self) -> None:
        if self._gpu_dead:
            return
        try:
            out = self._gpu_runner(self._gpu_timeout_s)
        except (_GpuUnavailable, FileNotFoundError):
            self._gpu_dead = True
            self._gpu = {"status": "unavailable", "util_pct": None,
                         "mem_used_mb": None, "mem_total_mb": None,
                         "ts_mono": None}
            return
        except subprocess.TimeoutExpired:
            self._gpu.update({"status": "timeout", "ts_mono": time.monotonic()})
            return
        except Exception as exc:  # noqa: BLE001 —— 查询失败显式记录，不中断采样
            self._gpu.update({"status": f"error:{exc}", "ts_mono": None})
            return
        try:
            parts = [x.strip() for x in out.strip().splitlines()[0].split(",")]
            util, used, total = (int(float(x)) for x in parts[:3])
            self._gpu = {"status": "ok", "util_pct": util,
                         "mem_used_mb": used, "mem_total_mb": total,
                         "ts_mono": time.monotonic()}
        except (ValueError, IndexError):
            self._gpu.update({"status": "error:parse", "ts_mono": None})

    @staticmethod
    def _nvidia_smi(timeout_s: float) -> str:
        import subprocess
        try:
            r = subprocess.run(["nvidia-smi", *_GPU_QUERY],
                               capture_output=True, text=True,
                               timeout=timeout_s)
        except FileNotFoundError as exc:
            raise _GpuUnavailable() from exc
        if r.returncode != 0:
            raise RuntimeError(f"nvidia-smi rc={r.returncode}")
        return r.stdout

    # ---------- 内部：落盘 ----------

    @staticmethod
    def _sample_line(sample: dict) -> str:
        return json.dumps({k: v for k, v in sample.items()
                           if not k.startswith("_")},
                          ensure_ascii=False, separators=(",", ":"))

    def _event_line(self, evt: dict, *, incomplete: bool, reason: str | None,
                    to_id: int | None = None) -> str:
        from_id = evt.get("_from_id", evt.get("sample_id"))
        missing = None
        if incomplete:
            missing = "会话关闭截断，后置窗口未完整采集"
        elif evt.get("_evicted"):
            missing = f"前置窗口超出环形缓冲，估缺 {evt['_evicted']} 个样本"
        return json.dumps(
            {"v": SCHEMA_VERSION, "type": "perf_event",
             "event_id": evt["event_id"], "source": evt["source"],
             "reason": evt["reason"], "detail": evt.get("detail"),
             "sample_from": from_id, "sample_to": to_id,
             "missing": missing, "incomplete": incomplete,
             "close_reason": reason,
             "ts_wall_ns": evt.get("ts_wall_ns", time.time_ns()),
             "ts_mono_ns": time.monotonic_ns()},
            ensure_ascii=False, separators=(",", ":"), default=str)

    def _write_lines(self, lines: list[str], *,
                     sample_id: int | None = None) -> None:
        """落盘（含缺口状态机）：标记与当前样本都成功才算这一拍落盘完成。

        失败时记缺口区间，下一拍**优先**补写 persist_gap；标记写成功后继续写
        当前样本——只写标记就收工会留下「状态已恢复、样本凭空消失」的假象，
        而缺口记账比不记账更糟，因为它宣称自己记得。
        """
        with self._file_lk:
            fh = self._file
            if fh is None:
                return
            if self._persist_gap is not None and not self._write_gap_locked(fh):
                if sample_id is not None:
                    self._extend_gap_locked(sample_id)
                return                          # 标记没补上，整拍推迟，不丢记账
            try:
                for ln in lines:
                    fh.write(ln + "\n")
            except OSError:
                self._persist_failures += 1
                self._persist_consecutive += 1
                if sample_id is not None:
                    self._extend_gap_locked(sample_id)
                return                          # 失败不拖垮采样线程
            self._persist_consecutive = 0

    def _write_gap_locked(self, fh) -> bool:
        """写缺口标记（调用方持 _file_lk）。成功清区间，失败保留待下次重试。"""
        gap = self._persist_gap
        if gap is None:
            return True
        line = json.dumps(
            {"v": SCHEMA_VERSION, "type": "persist_gap",
             "from": gap[0], "to": gap[1],
             "failures": self._persist_failures,
             "ts_wall_ns": time.time_ns(),
             "ts_mono_ns": time.monotonic_ns()},
            ensure_ascii=False, separators=(",", ":"))
        try:
            fh.write(line + "\n")
        except OSError:
            self._persist_failures += 1
            self._persist_consecutive += 1
            return False
        self._persist_gap = None
        return True

    def _extend_gap_locked(self, sample_id: int) -> None:
        """把一次失败并入待补区间（相邻区间合并，不覆盖）。"""
        a, b = self._persist_gap or (sample_id, sample_id)
        self._persist_gap = (min(a, sample_id), max(b, sample_id))

    def _close_file_locked(self) -> None:
        fh, self._file = self._file, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


perf_sampler = PerfSampler()
_listener_bound = False


def ensure_perf_sampler_started() -> PerfSampler:
    """幂等启动并绑定会话生命周期（sidecar 主入口与事件消费方共用）。"""
    global _listener_bound
    if not _listener_bound:
        from maaracing_master.core.logger import logger
        logger.add_session_listener(perf_sampler)
        _listener_bound = True
        # 已开着的会话：立即补绑（进程中途启用）
        if logger.session_dir is not None and logger.session_token:
            perf_sampler.on_session_open(logger.session_dir,
                                         logger.session_token)
    if not perf_sampler.running:
        perf_sampler.start()
    return perf_sampler


def build_perf_payload(perf_rows: list[dict] | None,
                       snapshot: dict | None = None) -> dict:
    """sidecar 聚合纯函数：模块领域行 ∪ core 资源面。

    领域行由 perf_contract.normalize 产出（已校验、已判级、已格式化），
    本函数只做拼装，不再二次解释数值——**判级与格式只有一个产生点**。

    - 资源面 cpu 是本进程瞬时占用（口径=进程 CPU% ÷ 全核），磁贴直接用；
    - 采样器不可用时 cpu 显式 available=False（不伪装零值），领域行照放；
    - schema_version=3：领域面由 v2 的「模块自定义字典」改为「声明行数组」。
    """
    snap = snapshot if snapshot is not None else perf_sampler.snapshot()
    payload: dict = {"schema_version": 3,
                     "rows": list(perf_rows or []),
                     # 采样号：面板只在它变化时推进走势图（4Hz 轮询 / 1Hz 采样，
                     # 否则同一个值被塞四次，曲线被拉平）
                     "sample_id": snap.get("sample_id"),
                     "sampler_available": bool(snap.get("available"))}
    if not snap.get("available"):
        payload["cpu"] = {"available": False}
        return payload
    selfproc = next((p for p in snap.get("procs") or []
                     if p.get("name") == "self"), None)
    cores = os.cpu_count() or 1
    if selfproc is not None:
        load = round(selfproc.get("cpu_pct", 0.0) / (cores * 100.0), 4)
        level = ("error" if load >= 0.85
                 else "warn" if load >= 0.6 else "ok")
        payload["cpu"] = {"available": True, "level": level,
                          "load_p50": load, "load_p95": load,
                          "p50": selfproc.get("cpu_pct"),
                          "p95": selfproc.get("cpu_pct"), "cores": cores}
    else:
        payload["cpu"] = {"available": False}
    payload["sys"] = {"cpu_pct": snap.get("cpu_pct"),
                      "mem_pct": snap.get("mem_pct"),
                      "mem_used_gb": snap.get("mem_used_gb"),
                      "mem_total_gb": snap.get("mem_total_gb")}
    payload["gpu"] = snap.get("gpu")
    payload["procs"] = snap.get("procs")
    return payload
