# -*- coding: utf-8 -*-
"""线程级 CPU 时间、身份与心跳（Win32 GetThreadTimes，零依赖）。

为什么自己调 Win32：psutil 的线程接口在本机实测一轮 60–90 毫秒，且**不释放
GIL**——按秒采集会把同进程其它 Python 线程一起冻住（60Hz 心跳出现 90ms 空档）。
直调 Win32 一轮 0.05 毫秒（10 线程），心跳抖动 <1ms。先例见 core/cpu_time.py。

线程身份 = `(tid, 创建时间)`。线程名和 tid 都不是身份：名字会重名，tid 会被
系统复用。创建时间取 `GetThreadTimes` 的第一个出参——与 CPU 时间同一次调用，
零额外开销，是系统给的出生证明而不是推测。

已知边界（契约 §3.4）：
- 同一 tid 若在同一个时钟刻度内退出又新建，会被判为同一身份。这类情形**检测
  不到**，只能靠下游信号发现（心跳差分出负值 → 该线程降级为「身份未知」）。
- 非 Windows 平台：`thread_times` 恒返回 None，清单里线程只有名字没有占用。

分层：本模块不认识任何业务模块。标签由外部塞进来（`set_labels`），它只负责
按名字匹配与统计命中，不判断标签从哪来。
"""

from __future__ import annotations

import re
import sys
import threading
import time

__all__ = ["IS_WINDOWS", "ThreadProbe", "beat", "beat_count",
           "thread_times", "DEFAULT_NAME_RE"]

IS_WINDOWS = sys.platform == "win32"

# 解释器自动起的名字（`Thread-12`、`Thread-2 (_dispatch)` 这类）不是模块起的，
# 进清单只是噪声——用前缀匹配，带后缀的同样算默认名
DEFAULT_NAME_RE = re.compile(r"^Thread-\d+")

_FILETIME_UNIT_S = 100e-9

# 心跳计数：模块工作线程每转一圈 `beat()` 一次。键是系统线程号。
# 每拍只由所属线程写自己的键，GIL 下 `d[k] = d.get(k, 0) + 1` 不会被别的线程
# 插进来改同一个键；读侧拿到的是某一瞬间的值，差分算频率足够。
_BEATS: dict[int, int] = {}

_kernel32 = None
_OpenThread = None
_GetThreadTimes = None
_CloseHandle = None
_bind_error: str | None = None

_THREAD_QUERY_LIMITED_INFORMATION = 0x0800


def _bind() -> bool:
    """懒绑定 Win32 入口；失败一次即永久判定不可用（不每拍重试）。"""
    global _kernel32, _OpenThread, _GetThreadTimes, _CloseHandle, _bind_error
    if _GetThreadTimes is not None:
        return True
    if _bind_error is not None:
        return False
    if not IS_WINDOWS:
        _bind_error = "非 Windows 平台"
        return False
    import ctypes
    import ctypes.wintypes as wt

    try:
        lib = ctypes.windll.kernel32  # type: ignore[attr-defined]
        open_thread = lib.OpenThread
        open_thread.restype = ctypes.c_void_p
        open_thread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        get_times = lib.GetThreadTimes
        get_times.restype = ctypes.c_bool
        get_times.argtypes = [ctypes.c_void_p] + [
            ctypes.POINTER(ctypes.c_uint64)] * 4
        close = lib.CloseHandle
        close.restype = ctypes.c_bool
        close.argtypes = [ctypes.c_void_p]
        _kernel32, _OpenThread = lib, open_thread
        _GetThreadTimes, _CloseHandle = get_times, close
    except Exception as exc:  # noqa: BLE001 —— 平台事实取不到就是不可用
        _bind_error = str(exc)
        return False
    return True


def thread_times(tid: int) -> tuple[int, float] | None:
    """读一个线程的 `(创建时间, 累计 CPU 秒)`；读不到返回 None。

    读不到是常态而非异常：线程可能刚退出、可能没有权限、可能压根不是 Windows。
    调用方按「身份未知」处理，不要当错误上报。
    """
    if not _bind():
        return None
    import ctypes

    open_thread, get_times, close = _OpenThread, _GetThreadTimes, _CloseHandle
    if open_thread is None or get_times is None or close is None:
        return None
    handle = open_thread(_THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not handle:
        return None
    try:
        creation = ctypes.c_uint64()
        exit_ = ctypes.c_uint64()
        kernel = ctypes.c_uint64()
        user = ctypes.c_uint64()
        ok = get_times(ctypes.c_void_p(handle),
                       ctypes.byref(creation), ctypes.byref(exit_),
                       ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        return creation.value, (kernel.value + user.value) * _FILETIME_UNIT_S
    finally:
        close(ctypes.c_void_p(handle))


def beat() -> None:
    """模块工作线程每转一圈调一次：无参、无锁、无分配。

    没有调用的线程频率列显示「—」——不猜、不按 CPU 占用反推。
    """
    tid = threading.get_native_id()
    _BEATS[tid] = _BEATS.get(tid, 0) + 1


def beat_count(tid: int) -> int:
    return _BEATS.get(tid, 0)


class ThreadProbe:
    """线程清单：过滤、占用、频率、标签。每拍调一次 `sample()`。

    只收「生命周期长于一个采样周期」的线程：每帧新建即退的并行计算线程
    （深度链找边/栅格那类）第一次看到时不计，等活过一个周期才算——这类线程
    存活只有几毫秒，1 秒采样根本见不到第二次，天然被挡掉。
    """

    def __init__(self, *, max_rows: int = 10, min_age_s: float = 1.0,
                 exclude_names: tuple[str, ...] = ()):
        self._max_rows = max_rows
        self._min_age_s = min_age_s
        # 观察者不出现在被观察名单里（采样线程自己）
        self._exclude = tuple(exclude_names)
        self._labels: dict[str, str] = {}
        self._labels_hit: set[str] = set()
        self._first_seen: dict[int, float] = {}
        self._prev_cpu: dict[int, tuple[int, float]] = {}
        self._prev_beats: dict[int, tuple[int, int]] = {}
        self._prev_mono: float | None = None

    def set_labels(self, labels: dict[str, str] | None) -> None:
        """设置「线程名 → 中文别名」。换模块时整体替换，命中记录保留。"""
        self._labels = dict(labels or {})

    def missing_labels(self) -> list[str]:
        """整场没匹配到任何线程的声明（收尾告警用）。"""
        return sorted(set(self._labels) - self._labels_hit)

    def reset(self) -> None:
        self._first_seen.clear()
        self._prev_cpu.clear()
        self._prev_beats.clear()
        self._labels_hit.clear()
        self._prev_mono = None

    def sample(self, proc_cpu_pct: float | None) -> dict:
        """产出一拍的线程段。

        返回 `{"rows": [...], "total_pct": .., "unattributed_pct": ..,
        "hidden": n}`：
        - `rows` 按占用降序，最多 `max_rows` 条，每条
          `{tid, name, label, cpu_pct, hz, identity_ok}`；
        - `unattributed_pct` = 进程占用 − 清单合计，**不是采样误差**，而是
          生命周期短于采样周期的线程与原生线程池（推理 / OpenCV）那部分
          在采样粒度上不可归因；为 None 表示进程占用本身拿不到。
        """
        now = time.monotonic()
        dt = None if self._prev_mono is None else now - self._prev_mono
        self._prev_mono = now

        rows: list[dict] = []
        alive_tids: set[int] = set()
        for t in threading.enumerate():
            tid = t.native_id
            if tid is None or tid in alive_tids:
                continue
            alive_tids.add(tid)
            name = t.name
            if DEFAULT_NAME_RE.match(name) or name in self._exclude:
                continue                      # 解释器默认名 / 观察者自己：不收
            first = self._first_seen.get(tid)
            if first is None:
                # 第一次看到：不进清单（还不知它活多久），但**先读一次基线**，
                # 否则要多等一拍才有占用与频率——短命线程也正好在这里被挡掉
                self._first_seen[tid] = now
                times = thread_times(tid)
                if times is not None:
                    self._prev_cpu[tid] = times
                    self._prev_beats[tid] = (times[0], beat_count(tid))
                continue
            if now - first < self._min_age_s:
                continue

            times = thread_times(tid)
            creation, cpu_s = times if times else (None, None)
            prev = self._prev_cpu.get(tid)
            cpu_pct = None
            identity_ok = True
            if times is not None and prev is not None and dt and dt > 0:
                if prev[0] == creation:
                    delta = cpu_s - prev[1]
                    if delta < 0:
                        # 心跳差分出负值说明身份判定漏了（同一刻度内换线程）：
                        # 降级为身份未知，不修正数字硬算
                        identity_ok = False
                    else:
                        cpu_pct = delta / dt * 100.0
                else:
                    identity_ok = False       # 复用：旧基线作废
            if times is not None:
                self._prev_cpu[tid] = (creation, cpu_s)

            hz = None
            count = beat_count(tid)
            prev_beat = self._prev_beats.get(tid)
            # 从没调过 beat 的线程给 None（显示「—」），不是 0——0 的含义是
            # 「调过但现在停了」，两者诊断意义不同
            if count and prev_beat is not None and dt and dt > 0 \
                    and prev_beat[0] == creation:
                delta_b = count - prev_beat[1]
                hz = delta_b / dt if delta_b >= 0 else None
            self._prev_beats[tid] = (creation, count)

            rows.append({
                "tid": tid,
                "name": name,
                "label": self._labels.get(name) or None,
                "cpu_pct": None if cpu_pct is None else round(cpu_pct, 2),
                "hz": None if hz is None else round(hz, 2),
                "identity_ok": identity_ok,
            })
            if name in self._labels:
                self._labels_hit.add(name)

        # 已消失的线程：清掉跨拍状态，避免 tid 复用后接到旧基线上
        for tid in list(self._first_seen):
            if tid not in alive_tids:
                self._first_seen.pop(tid, None)
                self._prev_cpu.pop(tid, None)
                self._prev_beats.pop(tid, None)

        rows.sort(key=lambda r: (r["cpu_pct"] is None, -(r["cpu_pct"] or 0.0)))
        hidden = max(0, len(rows) - self._max_rows)
        rows = rows[:self._max_rows]

        total = None
        for r in rows:
            if r["cpu_pct"] is not None:
                total = (total or 0.0) + r["cpu_pct"]
        unattributed = None
        if proc_cpu_pct is not None and total is not None:
            unattributed = round(max(0.0, proc_cpu_pct - total), 2)
        return {"rows": rows, "total_pct": None if total is None else round(total, 2),
                "unattributed_pct": unattributed, "hidden": hidden}
