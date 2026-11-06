# -*- coding: utf-8 -*-
"""speedrush 宏：录制玩家操作、按组存放、按时间轴回放。

**与深度几何录制的区别**：深度模式的录制产出训练语料（帧 + 手柄 + HUD，落
``debug/``）；宏只产出输入时间轴（落 ``data/speedrush/macros/<组>/``），回放
时不加载 YOLO/深度模型，只往虚拟手柄按时间轴送摇杆与扳机。

**为什么录制与回放分成两个线程**：
- 录制：采样物理手柄 + 键盘，写 ``p<phase>.jsonl``；目标 200Hz，但**按真实
  时间戳记账**（Windows sleep 粒度约 15.6ms，不得对外宣称 200Hz）。
- 回放：按录制时间轴调度。线程独立于 20Hz 驾驶循环——驾驶循环还在做每 0.5 秒
  一次的锚点判定，不能被回放节拍拖慢，回放也不能因锚点判定阻塞而丢拍。

**阶段结束不由本模块判定**：驾驶循环的锚点识别是主判据，HUD 计时归零是兜底；
本模块只负责"收到结束信号立即松手"。

**时间对齐**：录制起点 t=0 是驾驶页就绪时刻；首个可信 HUD 计时样本外推得到
``g0``（录制起点对应的游戏剩余秒数）。回放等待实时计时降到 ``g0`` 再起步，
之后按录制时间轴走；HUD 计时与墙钟偏差超过阈值时重锚（游戏暂停/卡顿）。
HUD 计时是 1 秒量化，故阈值取 2 秒，不宣称亚秒精度。
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from maaracing_master.core import xinput
from maaracing_master.core.logger import logger
from maaracing_master.core.paths import data_dir
from maaracing_master.core.session_layout import (
    SESSION_TOKEN_RE,
    session_token_label,
)
from maaracing_master.plugins.speedrush.pad_source import (
    PAD_SLOT_PROBE_TIMEOUT_S,
    PadSampler,
)

SCHEMA_VERSION = 1
KEY_MAP_VERSION = "v1"

# 单个阶段的上限：阶段 2 计时上限 300 秒，留出起步前空档与收尾余量
MAX_DURATION_MS = 400_000
MAX_SAMPLES = 100_000
# 回放起步等待 HUD 对齐的上限；超时按墙钟直接起步并告警
ALIGN_TIMEOUT_S = 5.0
# 墙钟与游戏计时的偏差阈值（秒）。HUD 是 1 秒量化，取 2 秒避免噪声抖动
DRIFT_THRESHOLD_S = 2.0
# 回放线程的空转节拍（秒）
_PLAY_TICK_S = 0.004
# 末样本保持时长（ms）：播完最后一条再松手，避免最后一段输入只生效 0 毫秒
_END_HOLD_MS = 20.0

_FULL_STEER = xinput.STICK_MAX
_FULL_TRIGGER = xinput.TRIGGER_MAX


class MacroError(ValueError):
    """宏文件/宏组不可用（消息直接面向日志，不抛给最终用户）。"""


def macro_root() -> Path:
    """宏组根目录：``data/speedrush/macros/``（用户资产，不随 debug 开关清理）。"""
    return data_dir() / "speedrush" / "macros"


def valid_group_name(name: str) -> bool:
    """宏组名只允许单段目录名，防路径穿越。"""
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    if name in (".", ".."):
        return False
    return not any(c in name for c in ("/", "\\", "\x00")) and name.strip() == name


def _group_dirs(base: Path) -> list[Path]:
    """按编号逆序列出宏组目录；只认会话编号形态的目录名（与 core 同一套格式）。"""
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir()
                   if p.is_dir() and SESSION_TOKEN_RE.match(p.name)),
                  key=lambda p: p.name, reverse=True)


def _phases_of(path: Path) -> dict:
    """组元数据里的阶段记录；读不出返回空——半成品不冒充素材。"""
    try:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— meta 缺失/损坏都按无记录处理
        return {}
    phases = meta.get("phases") if isinstance(meta, dict) else None
    return phases if isinstance(phases, dict) else {}


# 正常收尾的结束原因：阶段自然结束（实机）与显式正常停止（测试桩与工具）
_DONE_REASONS = frozenset({"phase_end", "normal"})


def _phase_done(phases: dict, phase: str) -> bool:
    info = phases.get(phase)
    return isinstance(info, dict) and info.get("stop_reason") in _DONE_REASONS


def is_usable_group(path: Path) -> bool:
    """至少有一段带收尾记录的宏才可用；没有元数据的历史目录一律不可用。"""
    phases = _phases_of(path)
    return any(isinstance(phases.get(ph), dict) for ph in ("1", "2"))


def is_complete_group(path: Path) -> bool:
    """两个阶段都有记录且都正常收尾；被提前中止或整段没录的都算不完整。"""
    phases = _phases_of(path)
    return all(_phase_done(phases, ph) for ph in ("1", "2"))


def resolve_group(group: str | None, root: Path | None = None) -> Path:
    """解析宏组目录；空组名 = 最新一组。不可用时抛 MacroError。"""
    base = root or macro_root()
    if group:
        if not valid_group_name(group):
            raise MacroError(f"非法宏组名: {group!r}")
        path = base / group
        if not path.is_dir():
            raise MacroError(f"宏组不存在: {group}")
        if not is_usable_group(path):
            raise MacroError(f"宏组 {group} 没有录制收尾的记录，不能回放")
        return path
    for path in _group_dirs(base):
        if is_usable_group(path):
            return path
    raise MacroError("还没有录制过宏")


def list_groups(root: Path | None = None) -> list[dict]:
    """供 GUI 下拉用的宏组列表（含 value/label），按编号逆序。

    没有录制收尾记录的目录不列（半截产物不该出现在选择里）；只录了一半或
    被提前中止的组照常列出，标签带「不完整」——缺的那一段回放时降级直行。
    """
    out: list[dict] = []
    for path in _group_dirs(root or macro_root()):
        if not is_usable_group(path):
            continue
        label = session_token_label(path.name)
        if not is_complete_group(path):
            label += "（不完整）"
        out.append({"value": path.name, "label": label})
    return out


def choice_label(group: str | None, items: list[dict] | None = None) -> str:
    """配置值 → 给人看的宏组标签：空值取最新一组并标注，无宏可放时给「无」。

    ``items`` 传 ``list_groups()`` 的结果可避免重复枚举目录。"""
    entries = list_groups() if items is None else items
    if not entries:
        return "无"
    if not group:
        return "最新 · " + entries[0]["label"]
    for item in entries:
        if item["value"] == group:
            return item["label"]
    return "无（原选项已失效）"


def merge_sample(pad: xinput.PadState | None, keys: dict[str, bool]) -> dict:
    """把物理手柄与键盘状态合并成一个样本。

    键盘是**覆盖式**映射：按下的键覆盖对应轴/扳机，未按的轴取手柄原值。
    左右转向键同时按下 → 摇杆回中；W 与 S 是两个独立扳机（RT / LT），互不抵消。
    """
    lx = int(pad.left_stick[0]) if pad is not None else 0
    ly = int(pad.left_stick[1]) if pad is not None else 0
    rx = int(pad.right_stick[0]) if pad is not None else 0
    ry = int(pad.right_stick[1]) if pad is not None else 0
    lt = int(pad.left_trigger) if pad is not None else 0
    rt = int(pad.right_trigger) if pad is not None else 0
    btn = int(pad.buttons) if pad is not None else 0

    left = bool(keys.get("a") or keys.get("left"))
    right = bool(keys.get("d") or keys.get("right"))
    if left and right:
        lx = 0
    elif left:
        lx = -_FULL_STEER
    elif right:
        lx = _FULL_STEER
    if keys.get("w") or keys.get("up"):
        rt = _FULL_TRIGGER
    if keys.get("s") or keys.get("down"):
        lt = _FULL_TRIGGER
    return {"lx": lx, "ly": ly, "rx": rx, "ry": ry,
            "lt": lt, "rt": rt, "btn": btn}


def _row(t_ms: float, sample: dict) -> str:
    return json.dumps({
        "t_ms": round(float(t_ms), 3),
        "lx": int(sample["lx"]), "ly": int(sample["ly"]),
        "rx": int(sample["rx"]), "ry": int(sample["ry"]),
        "lt": int(sample["lt"]), "rt": int(sample["rt"]),
        "btn": int(sample["btn"]),
    }, ensure_ascii=False)


def load_phase(group_dir: Path, phase: int) -> tuple[list[dict], float | None]:
    """读并校验一个阶段的样本；返回 ``(样本列表, g0)``。非法即抛 MacroError。"""
    path = Path(group_dir) / f"p{int(phase)}.jsonl"
    if not path.is_file():
        raise MacroError(f"缺少阶段 {phase} 的宏文件: {path.name}")
    g0 = None
    try:
        meta = json.loads((Path(group_dir) / "meta.json").read_text(encoding="utf-8"))
        info = (meta.get("phases") or {}).get(str(int(phase))) or {}
        g0 = info.get("g0")
        if g0 is not None:
            g0 = float(g0)
    except Exception:  # noqa: BLE001 —— meta 缺失/损坏只丢对齐信息，不丢样本
        g0 = None
    samples: list[dict] = []
    last_t = -1.0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    row = _validate_row(raw)
                except Exception as exc:  # noqa: BLE001 —— 坏行即整文件不可用
                    raise MacroError(f"{path.name} 第 {line_no} 行非法: {exc!r}") from exc
                if row["t_ms"] < last_t:
                    raise MacroError(f"{path.name} 第 {line_no} 行时间倒退")
                if row["t_ms"] > MAX_DURATION_MS:
                    raise MacroError(f"{path.name} 时长超过上限")
                last_t = row["t_ms"]
                samples.append(row)
                if len(samples) > MAX_SAMPLES:
                    raise MacroError(f"{path.name} 样本数超过上限")
    except MacroError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 读失败按文件不可用
        raise MacroError(f"读取 {path.name} 失败: {exc!r}") from exc
    if not samples:
        raise MacroError(f"{path.name} 没有样本")
    return samples, g0


def _validate_row(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("样本必须是对象")
    t_ms = float(raw["t_ms"])
    if t_ms < 0:
        raise ValueError("t_ms 为负")
    out = {"t_ms": t_ms}
    for name in ("lx", "ly", "rx", "ry"):
        v = int(raw[name])
        if not (xinput.STICK_MIN <= v <= xinput.STICK_MAX):
            raise ValueError(f"{name} 越界: {v}")
        out[name] = v
    for name in ("lt", "rt"):
        v = int(raw[name])
        if not (xinput.TRIGGER_MIN <= v <= xinput.TRIGGER_MAX):
            raise ValueError(f"{name} 越界: {v}")
        out[name] = v
    btn = int(raw.get("btn", 0))
    if not (0 <= btn <= 0xFFFF):
        raise ValueError(f"btn 越界: {btn}")
    out["btn"] = btn
    return out


@dataclass
class MacroStats:
    state: str = "waiting"       # waiting / playing / finished / stopped
    sent: int = 0
    total: int = 0
    align_offset_ms: float = 0.0
    t0_ns: int = 0


class MacroRecorder:
    """采样物理手柄 + 键盘，写 ``p<phase>.jsonl``；``note_clock`` 由驾驶循环喂 HUD 计时。"""

    def __init__(
        self,
        out_dir: Path,
        *,
        phase: int,
        pad_slot: int | None = None,
        keyboard: bool = True,
        poll_hz: float = 200.0,
        group_id: str | None = None,
        foreground: Callable[[], bool] | None = None,
        keys_reader: Callable[[], dict[str, bool]] | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.phase = int(phase)
        self.keyboard = bool(keyboard)
        self.poll_hz = max(1.0, float(poll_hz))
        self._group_id = group_id
        self._foreground = foreground or (lambda: True)
        self._keys_reader = keys_reader
        self._pad = PadSampler(slot=pad_slot, log=self._log)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        self._t0_ns = 0
        self._samples = 0
        self._nonzero = 0
        self._gaps: list[float] = []
        self._last_ns = 0
        self._hud_anchor: dict | None = None   # {"t_ms":..., "timer_s":...}
        self._file = None
        self._started_iso = ""

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        with self._lock:
            return {"samples": self._samples, "points": self._nonzero,
                    "keyboard": self.keyboard, "phase": self.phase}

    def _log(self, msg: str, level: str = "INFO") -> None:
        logger.log(msg, level, group_id=self._group_id)

    def start(self) -> None:
        if self._running:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._file = (self.out_dir / f"p{self.phase}.jsonl").open(
            "w", encoding="utf-8", buffering=1)
        self._stop.clear()
        self._t0_ns = time.perf_counter_ns()
        self._last_ns = self._t0_ns
        self._samples = self._nonzero = 0
        self._gaps = []
        self._hud_anchor = None
        self._started_iso = datetime.now().isoformat(timespec="seconds")
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="speedrush-macro-rec", daemon=True)
        self._thread.start()
        self._log(f"[极速狂飙] 宏录制开始 → {self.out_dir}（阶段 {self.phase}）", "INFO")

    def note_clock(self, timer_s: int | float | None, frame_ts_ns: int) -> None:
        """驾驶循环喂入可信 HUD 计时样本；只记第一个，用于外推 g0。"""
        if timer_s is None or self._hud_anchor is not None:
            return
        t_ms = max(0.0, (int(frame_ts_ns) - self._t0_ns) / 1e6)
        self._hud_anchor = {"t_ms": round(t_ms, 3), "timer_s": float(timer_s)}

    def stop(self, reason: str = "normal") -> None:
        if not self._running:
            return
        self._running = False
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(timeout=10.0)
        self._thread = None
        try:
            if self._file is not None:
                self._file.close()
        except Exception:  # noqa: BLE001 —— 收尾失败不抛给调用方
            pass
        self._file = None
        self._write_meta(reason)
        self._log(
            f"[极速狂飙] 宏录制结束：{self._samples} 条样本"
            f"（非零 {self._nonzero}）", "INFO" if self._nonzero else "WARNING")
        if not self._nonzero:
            self._log("[极速狂飙] 宏里没有任何非零输入——录制期间是否没碰手柄/键盘？",
                      "WARNING")

    def _run(self) -> None:
        interval = 1.0 / self.poll_hz
        probe_deadline = time.monotonic() + PAD_SLOT_PROBE_TIMEOUT_S
        assert self._file is not None   # start() 建线程前已打开文件
        while not self._stop.is_set():
            t0 = time.perf_counter()
            st, _countable = self._pad.poll(probe_deadline)
            keys: dict[str, bool] = {}
            if self.keyboard:
                try:
                    if self._foreground():
                        keys = (self._keys_reader or _read_keys)()
                except Exception:  # noqa: BLE001 —— 键盘读取失败按无键处理
                    keys = {}
            sample = merge_sample(st, keys)
            now_ns = time.perf_counter_ns()
            t_ms = (now_ns - self._t0_ns) / 1e6
            with self._lock:
                try:
                    self._file.write(_row(t_ms, sample) + "\n")
                except Exception as exc:  # noqa: BLE001 —— 写盘失败即停止录制
                    self._log(f"[极速狂飙] 宏录写失败，停止录制: {exc!r}", "WARNING")
                    self._stop.set()
                    break
                self._samples += 1
                if any(sample[k] for k in ("lx", "ly", "rx", "ry", "lt", "rt", "btn")):
                    self._nonzero += 1
                if self._samples > 1:
                    self._gaps.append((now_ns - self._last_ns) / 1e6)
                    if len(self._gaps) > 1000:
                        self._gaps.pop(0)
                self._last_ns = now_ns
            time.sleep(max(0.0, interval - (time.perf_counter() - t0)))

    def _write_meta(self, reason: str) -> None:
        meta_path = self.out_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 首次写或旧 meta 损坏都重建
            meta = {}
        meta.update({
            "schema": SCHEMA_VERSION,
            "key_map": KEY_MAP_VERSION,
            "keyboard": self.keyboard,
            "pad_slot": self._pad.slot,
        })
        # 组创建时间只在第一阶段写；第二阶段的 stop 不得把它改成阶段 2 的时间
        meta.setdefault("created_at", self._started_iso)
        phases = meta.get("phases")
        if not isinstance(phases, dict):
            phases = {}
        duration_ms = ((self._last_ns - self._t0_ns) / 1e6
                       if self._last_ns and self._t0_ns else 0.0)
        info = {
            "samples": self._samples,
            "duration_ms": round(duration_ms, 3),
            "median_dt_ms": round(statistics.median(self._gaps), 3) if self._gaps else None,
            "stop_reason": reason,
        }
        if self._hud_anchor is not None:
            info["hud_anchor"] = self._hud_anchor
            info["g0"] = round(self._hud_anchor["timer_s"]
                               + self._hud_anchor["t_ms"] / 1000.0, 3)
        phases[str(self.phase)] = info
        meta["phases"] = phases
        try:
            tmp = meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(meta_path)
        except Exception as exc:  # noqa: BLE001 —— meta 写失败不抛给调用方
            self._log(f"[极速狂飙] 宏 meta 写入失败: {exc!r}", "WARNING")


def _read_keys() -> dict[str, bool]:
    from maaracing_master.core.key_state import read_keys
    return read_keys()


class MacroPlayer:
    """按录制时间轴向虚拟手柄下发摇杆/扳机；自己持有手柄租约。"""

    def __init__(
        self,
        samples: list[dict],
        g0: float | None,
        *,
        pad_cm,
        phase: int,
        group_id: str | None = None,
        clock: Callable[[], tuple[float | None, int] | None] | None = None,
    ) -> None:
        self._samples = samples
        self._g0 = float(g0) if g0 is not None else None
        self._pad_cm = pad_cm
        self.phase = int(phase)
        self._group_id = group_id
        self._clock_fn = clock
        self._ts_ms = [float(s["t_ms"]) for s in samples]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        # 对齐完成后的时间轴原点（None=尚未起步）；漂移重锚直接改它
        self._t0_ns: int | None = None
        self._stats = MacroStats(total=len(samples))

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "state": self._stats.state,
                "sent": self._stats.sent,
                "total": len(self._samples),
                "align_offset_ms": round(self._stats.align_offset_ms, 1),
            }

    def _log(self, msg: str, level: str = "INFO") -> None:
        logger.log(msg, level, group_id=self._group_id)

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="speedrush-macro-play", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(timeout=3.0)
        self._thread = None

    # ---------- 回放线程 ----------

    def _run(self) -> None:
        try:
            with self._pad_cm() as gpad:
                self._play(gpad)
        except Exception as exc:  # noqa: BLE001 —— 回放失败只记录，不炸驾驶循环
            self._log(f"[极速狂飙] 宏回放异常: {exc!r}", "WARNING")
            with self._lock:
                self._stats.state = "stopped"

    def _play(self, gpad) -> None:
        start_deadline = time.monotonic() + ALIGN_TIMEOUT_S
        last_i = -1
        last_drift_ts = 0
        while not self._stop.is_set():
            now = time.perf_counter_ns()
            clk = self._clock_fn() if self._clock_fn is not None else None
            if self._t0_ns is None:
                if not self._align(clk, now, start_deadline):
                    time.sleep(_PLAY_TICK_S)
                    continue
            if clk is not None and clk[0] is not None:
                if int(clk[1]) != last_drift_ts:
                    last_drift_ts = int(clk[1])
                    self._correct_drift(float(clk[0]), int(clk[1]))
            if self._stop.is_set():
                break
            assert self._t0_ns is not None   # 上面 _align 成功即已写入起点
            elapsed_ms = (now - self._t0_ns) / 1e6
            i = bisect_right(self._ts_ms, elapsed_ms) - 1
            if i >= 0 and i != last_i:
                _emit(gpad, self._samples[i])
                last_i = i
                with self._lock:
                    self._stats.sent += 1
            if elapsed_ms > self._ts_ms[-1] + _END_HOLD_MS:
                with self._lock:
                    self._stats.state = "finished"
                self._log("[极速狂飙] 宏回放结束：已播完，松手等待阶段结束", "INFO")
                return
            time.sleep(_PLAY_TICK_S)
        with self._lock:
            if self._stats.state != "finished":
                self._stats.state = "stopped"

    def _align(self, clk, now: int, deadline: float) -> bool:
        """等到游戏计时降到录制起点；返回 True 表示已置 t0。"""
        if self._g0 is None:
            # 录制时没拿到 HUD 计时：没有可对齐的起点，直接按墙钟起步
            self._t0_ns = now
        elif clk is None or clk[0] is None:
            if time.monotonic() < deadline:
                return False
            self._t0_ns = now
            self._log("[极速狂飙] 宏回放：HUD 计时不可用，按墙钟直接起步", "WARNING")
        else:
            timer = float(clk[0])
            if timer > self._g0:
                if time.monotonic() < deadline:
                    return False
                self._t0_ns = now
                self._log("[极速狂飙] 宏回放：等待计时对齐超时，直接起步", "WARNING")
            else:
                lag = max(0.0, self._g0 - timer)
                # 计时值属于帧时戳 ts，不属于"读到它的 now"：用帧时戳反推起点，
                # 抵消 OCR 处理延迟。时钟陈旧（>1.5s）时退回 now，避免大跳。
                frame_ts = int(clk[1]) or now
                t_cross = frame_ts + int((timer - self._g0) * 1e9)
                stale = (now - frame_ts) > 1_500_000_000
                self._t0_ns = now if stale else t_cross
                with self._lock:
                    self._stats.align_offset_ms = lag * 1000.0
                if lag > 1.0:
                    self._log(f"[极速狂飙] 宏回放：检测晚 {lag:.1f}s，跳过开头", "WARNING")
        with self._lock:
            self._stats.state = "playing"
            self._stats.t0_ns = self._t0_ns
        self._log(f"[极速狂飙] 宏回放开始（阶段 {self.phase}，"
                  f"{len(self._samples)} 条）", "INFO")
        return True

    def _correct_drift(self, timer: float, frame_ts_ns: int) -> None:
        """按 HUD 帧时戳对齐墙钟：偏差 ≥2s 才重锚（游戏暂停/卡顿）。"""
        if self._g0 is None or self._t0_ns is None:
            return
        expected = self._g0 - (int(frame_ts_ns) - self._t0_ns) / 1e9
        drift = timer - expected
        if abs(drift) < DRIFT_THRESHOLD_S:
            return
        self._t0_ns += int(drift * 1e9)
        with self._lock:
            self._stats.t0_ns = self._t0_ns
        self._log(f"[极速狂飙] 宏回放：计时偏差 {drift:+.1f}s，重锚时间轴", "INFO")


def _emit(gpad, sample: dict) -> None:
    gpad.left_joystick(x_value=int(sample["lx"]), y_value=int(sample["ly"]))
    gpad.left_trigger(value=int(sample["lt"]))
    gpad.right_trigger(value=int(sample["rt"]))
    gpad.update()
