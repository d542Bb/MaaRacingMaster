# -*- coding: utf-8 -*-
"""speedrush 驾驶页 HUD 实时读数：低频采样 → ``hud.jsonl``。

**要解决什么**：三个未解的规则问题（里程口径的两假说、面板在场时的假读过滤、分数增量
归因）都卡在"没有连续序列"上——离线稀疏采样给不出「面板持续在场的一段连续读数」。
本模块在驾驶途中以低频把记分读数落成 ``hud.jsonl``，供事后与同会话 ``frames.jsonl``
按 ``frame_id`` / ``ts_ns`` 对齐（对齐方式与录制器同源：两者都取自
``ctx.capture.frame_with_age()``，同一帧号必带同一采集时刻）。

**产出形态**（写进录制会话目录，与 ``frames.jsonl`` / ``pads.jsonl`` 同级）::

    <会话>/
      hud.jsonl      逐次采样的读数序列（原始文本 + 解析值 + 判定标志）
      hud_meta.json  采样参数、真源指纹与计数

**为什么是独立线程**：30Hz 驾驶循环是热路径，单 ROI OCR 实测 10~30ms，同步调用会把它
按十倍量级拖慢——而驾驶循环同时还在做锚点复查与录制入队。故采样线程自带节拍、只做
``frame_with_age()`` 这一种**非阻塞**取帧（WGC 中心缓存的共享引用，多消费者各按自己
节奏读是它的既有契约），OCR 与落盘全在循环之外。

**为什么间隔取 0.5s**：面板一次在场约 3.5 秒，0.5s 给出约 7 个连续样本——足以看出
"持续增长直到段结束"与"涨到平台就饱和"的区别（H1/H2 的分野），也足以在段边界处给出
前后各若干点。代价侧：一轮读数约 10 个 ROI × 10~30ms ≈ 0.1~0.3s，即单核占用约
20~60%，且发生在独立线程；取 0.3s 只会把占用翻倍、换到边际收益很小的样本增量。
首个样本**推迟一个间隔**：阶段短于一个间隔时全程不碰 OCR（启停代价为零）。

**假读必须标出来、不能静默丢**：面板缺席时识别会强行解码出小数字（实验记录坑 6），
消费方只看"有没有数字"是分不出来的。故每行同时记**原始文本**、**解析值**、**该字段
是否过暗底闸门**、**比分块按颜色判出的归属**，以及逐字段的可信标志与理由；已知的
假读形态（``合计 < 里程``、单字面板读数）落到 ``flags`` 与字段 ``note`` 里，行照旧落盘。

**区域真源**：``resources/policy/hud_regions.json``（本插件内唯一一份，离线探针读同一份）。
本模块不复制任何 rect 常量，也不假定键集——采样范围就是该文件的键集。

**在场判据与敌我判据搬自实验探针**（``tools/experiments/speedrush_scoring/probe_hud_ocr.py``
的 ``field_on_dark()`` / ``block_side()``）：那两条判据是逐格复核出来的，口径保持一致；
探针按实验自包含约定自带一份代码副本（它不 import 本模块），只有区域**数据**是单一真源。
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from maaracing_master.core.logger import logger
from maaracing_master.plugins.speedrush import HUD_REGIONS_FILE

__all__ = [
    "HudObserver",
    "SAMPLE_INTERVAL_S",
    "SCHEMA_VERSION",
    "GATED_FIELDS",
    "SIDE_FIELDS",
    "SINGLE_CHAR_SUSPECT_FIELDS",
    "block_side",
    "field_on_dark",
    "load_hud_regions",
    "parse_int",
    "parse_value",
    "region_digest",
]

# 记录格式版本。字段语义变动时递增，供离线消费方识别。
SCHEMA_VERSION = 1

# 采样间隔（秒）。理由见模块 docstring：面板在场约 3.5s → 约 7 个连续样本。
SAMPLE_INTERVAL_S = 0.5

# 采样队列容量（约 4 秒）。满则丢样本计数，绝不阻塞采样线程——反压口径照录制器
# （``put_nowait`` + 丢弃计数），落盘慢不该拖住下一次取帧。
_QUEUE_MAX = 8

# 左侧信息面板的在场闸门（口径搬自实验探针 ``field_on_dark``）：面板是半透明暗底，
# 亮的是天空。**逐字段**判而不是整面板判——面板有"矮版"变体（不显示合计），
# 整面板判在场会把合计那格放行到面板下方的天空上。
DARK_LUM_MAX = 90.0
FIELD_DARK_MIN = 0.35

# 需要过暗底闸门的字段（左侧面板三格，顺序即面板自上而下的格序）
GATED_FIELDS: tuple[str, ...] = ("mileage", "overtake", "total_left")
# 比分面板：位置会上下互换，敌我必须按块色判（见 ``block_side``）
SIDE_FIELDS: tuple[str, ...] = ("score_top", "score_bottom")
# 单字读数与噪声不可区分的字段：这两个计数的可信读数都是多位（由公式
# 「合计 = 里程 + 30×超车」自身给出量级），单字只可能是面板缺席时被强行解码出来的
# 碎片。``overtake`` 刻意不在内——它的合法域就是 0~5 这类单字。
SINGLE_CHAR_SUSPECT_FIELDS: tuple[str, ...] = ("mileage", "total_left")

# 比分块归属的判据参数（搬自探针 ``block_side``）：取带内**饱和像素**均色。
# 用固定采样点会被天空骗过（天空同样满足 B>R）。
_SIDE_MIN_SAT = 80.0
_SIDE_MIN_PIXELS = 50

# 取整文本：'1,234' / '238' / '+120' 都能吃
_INT_RE = re.compile(r"\d[\d,]*")
# 计时字段是 mm:ss 倒计时；通用取整口径会把 "04:46" 读成 4（分钟）——那是个陷阱，
# 故这一格的值按**秒**记（口径同探针的 _mmss）。原始文本照旧完整落盘。
_TIMER_FIELD = "timer"
_MMSS_RE = re.compile(r"(\d{1,2}):(\d{2})")

# 取帧入口：(rgb, frame_id, ts_ns, age_ms)；契约同 ``CaptureCapability.frame_with_age``
FrameSource = Callable[[], "tuple[Any, int, int, float]"]


# ---------------- 区域真源 ----------------

def load_hud_regions(path: Path | str | None = None) -> dict[str, list[float]]:
    """读并校验区域真源，返回 ``{字段名: [x1, y1, x2, y2]}``（归一化）。

    校验口径与 ``tools/navkit/check_truth.py`` 的 ``rect_checks()`` 一致：4 元数值数组、
    值域 [0,1]、``x1<x2`` 且 ``y1<y2``——越界 rect 会静默读错区域，是最难发现的一类
    数据错误。**额外要求**：不得有 ``_`` 前缀键（真源是纯数据、不放说明），且
    ``GATED_FIELDS`` / ``SIDE_FIELDS`` 必须齐备——本模块的在场闸门与敌我判据硬依赖
    这两组字段，缺了它们闸门就成了摆设（那正是"面板缺席读出垃圾值"的成因）。

    非法即抛 ``ValueError``（fail loud）：调用方（``HudObserver``）捕获后停用观察线程
    并记 WARNING，不让读数链路带着错区域静默跑下去。
    """
    src = Path(path) if path is not None else HUD_REGIONS_FILE
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 读不到/解析不了都按"真源不可用"处理
        raise ValueError(f"区域真源读取失败 {src.name}: {exc!r}") from exc
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"区域真源必须是「字段名 → rect」的非空对象: {src.name}")

    out: dict[str, list[float]] = {}
    for name, rect in raw.items():
        if not isinstance(name, str) or name.startswith("_"):
            raise ValueError(f"区域名非法（真源为纯数据，不得有说明键）: {name!r}")
        if not (isinstance(rect, list) and len(rect) == 4
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in rect)):
            raise ValueError(f"{name} 的 rect 必须是 4 元数值数组: {rect!r}")
        vals = [float(x) for x in rect]
        if any(not 0.0 <= x <= 1.0 for x in vals):
            raise ValueError(f"{name} 的 rect 值越出 [0,1]: {rect!r}")
        if not (vals[0] < vals[2] and vals[1] < vals[3]):
            raise ValueError(f"{name} 的 rect 必须满足 x1<x2 且 y1<y2: {rect!r}")
        out[name] = vals

    missing = [f for f in (*GATED_FIELDS, *SIDE_FIELDS) if f not in out]
    if missing:
        raise ValueError(f"区域真源缺语义必需字段 {missing}（在场闸门/敌我判据依赖它们）")
    return out


def region_digest(regions: dict[str, list[float]]) -> str:
    """区域集的稳定指纹（写进每行与 meta，供事后确认"这行是拿哪套区域读的"）。

    区域集是会被重标的（复核结论推翻候选是常态），没有指纹就无法判断一行旧读数是
    按现在这套框读的、还是按已被推翻的那套。取规范序列化后的 sha1 前 12 位。
    """
    canon = json.dumps({k: [float(x) for x in v] for k, v in sorted(regions.items())},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


# ---------------- 判据（口径搬自实验探针） ----------------

def _px_bounds(rect, w: int, h: int) -> tuple[int, int, int, int]:
    """归一化 rect → 像素边界（截断 + 夹紧到帧内；口径同 core.ocr）。"""
    x1, y1 = max(0, int(float(rect[0]) * w)), max(0, int(float(rect[1]) * h))
    x2, y2 = min(w, int(float(rect[2]) * w)), min(h, int(float(rect[3]) * h))
    return x1, y1, x2, y2


def field_on_dark(frame_rgb: Any, rect) -> bool:
    """该 ROI 是否主要压在暗底上（左侧面板在该字段处是否在场）。

    纯 numpy，零 OCR 开销；实时采样每轮都要问一遍，故必须便宜。
    """
    h, w = frame_rgb.shape[:2]
    x1, y1, x2, y2 = _px_bounds(rect, w, h)
    reg = frame_rgb[y1:y2, x1:x2]
    if reg.size == 0:
        return False
    lum = reg.mean(axis=2) if reg.ndim == 3 else reg
    return float((lum < DARK_LUM_MAX).mean()) > FIELD_DARK_MIN


def block_side(frame_rgb: Any, rect) -> str:
    """该比分面板当前是"本机玩家（蓝）"还是"对手（红）"，判不出返回 ``"?"``。

    判据取带内**饱和像素**的均色而不是固定采样点：天空/路面是低饱和的，固定点采样
    会被天空骗过（实测"下方永远是我方"就是这么来的）。**位置不等于敌我**——两块面板
    会上下互换，故敌我判据只能来自颜色。
    """
    h, w = frame_rgb.shape[:2]
    x1, y1, x2, y2 = _px_bounds(rect, w, h)
    reg = frame_rgb[y1:y2, x1:x2].reshape(-1, 3).astype(float)
    if reg.size == 0:
        return "?"
    sat = reg.max(axis=1) - reg.min(axis=1)
    sel = reg[sat > _SIDE_MIN_SAT]
    if len(sel) < _SIDE_MIN_PIXELS:
        return "?"
    m = sel.mean(axis=0)
    return "本机(蓝)" if m[2] > m[0] else "对手(红)"


def parse_int(text: str | None) -> int | None:
    """取文本里的第一个整数（'1,234' / '238' / '+120' 都吃）；无数字返回 None。"""
    m = _INT_RE.findall(text or "")
    if not m:
        return None
    try:
        return int(m[0].replace(",", ""))
    except ValueError:
        return None


def parse_value(name: str, text: str | None) -> int | None:
    """按字段语义解析读数：多数格子取第一个整数，``timer`` 取 mm:ss 的秒数。

    为什么单独处理计时格：``"04:46"`` 走通用取整会得到 ``4``，看上去像个合法的
    "剩余 4 秒"，而它其实是 4 分 46 秒——这类"读得出来但解释错"的值正是本记录要防的。
    """
    if name == _TIMER_FIELD:
        m = _MMSS_RE.search(text or "")
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None
    return parse_int(text)


# ---------------- 观察线程 ----------------

class HudObserver:
    """驾驶页 HUD 低频观察者（start/stop 幂等；采样与落盘各一线程）。

    用法：``start()`` → 驾驶阶段结束后 ``stop(reason)``。采样线程按
    ``SAMPLE_INTERVAL_S`` 取帧读数并入队，落盘线程顺序写 ``hud.jsonl``。

    与录制器的时机语义一致（就绪后启动、阶段结束停止），且**写在录制会话目录里**：
    对齐目标就是同会话的 ``frames.jsonl``，没有会话目录就没有可对齐的帧。
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        frame_source: FrameSource,
        phase: int | None = None,
        round_no: int | None = None,
        interval_s: float = SAMPLE_INTERVAL_S,
        engine: Any | None = None,
        regions: dict[str, list[float]] | None = None,
        regions_path: Path | str | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.phase = phase
        self.round_no = round_no
        self.interval_s = max(0.05, float(interval_s))
        self._frame_source = frame_source
        # 引擎与区域都可注入（测试注入假引擎，不加载真模型）；注入 None 时按真源懒加载
        self._engine = engine
        self._engine_failed = False
        self._regions = regions if regions is not None else load_hud_regions(regions_path)
        self._digest = region_digest(self._regions)

        self._q: queue.Queue[dict] = queue.Queue(_QUEUE_MAX)
        self._stop = threading.Event()
        self._sample_thread: threading.Thread | None = None
        self._write_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._seq = 0
        self._rows_written = 0
        self._rows_dropped = 0
        self._rows_flagged = 0
        self._samples_no_frame = 0
        self._ocr_no_result = 0
        self._ocr_errors = 0
        self._started_ns = 0
        self._started_iso = ""
        self._running = False

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def regions(self) -> dict[str, list[float]]:
        return self._regions

    @property
    def stats(self) -> dict[str, int]:
        """实时计数（供 GUI 状态展示；不阻塞）。"""
        return {
            "rows_written": self._rows_written,
            "rows_dropped": self._rows_dropped,
            "rows_flagged": self._rows_flagged,
            "samples_no_frame": self._samples_no_frame,
            "ocr_no_result": self._ocr_no_result,
        }

    def start(self) -> None:
        if self._running:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._started_ns = time.perf_counter_ns()
        self._started_iso = datetime.now().isoformat(timespec="seconds")
        self._sample_thread = threading.Thread(
            target=self._sample_worker, name="speedrush-hud-sample", daemon=True)
        self._write_thread = threading.Thread(
            target=self._write_worker, name="speedrush-hud-write", daemon=True)
        self._write_thread.start()
        self._sample_thread.start()
        self._running = True
        logger.log(
            f"[极速狂飙] HUD 读数开始（{len(self._regions)} 区域 / 每 {self.interval_s:.2f}s）"
            f" → {self.out_dir / 'hud.jsonl'}", "INFO")

    def stop(self, reason: str = "normal") -> None:
        """停止并 flush。幂等；重复调用只写一次 meta。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._stop.set()
        for th in (self._sample_thread, self._write_thread):
            if th is not None:
                th.join(timeout=10.0)
        self._sample_thread = None
        self._write_thread = None
        self._write_meta(reason)
        logger.log(
            f"[极速狂飙] HUD 读数结束：{self._rows_written} 行"
            f"（标记假读 {self._rows_flagged}、丢 {self._rows_dropped}、"
            f"缺帧 {self._samples_no_frame}、OCR 无结果 {self._ocr_no_result}）",
            "INFO" if self._rows_dropped == 0 and self._ocr_no_result == 0 else "WARNING")

    # ---------- 采样线程 ----------

    def _sample_worker(self) -> None:
        """按间隔取帧读数并入队；``Event.wait`` 兼作节拍与停止唤醒。

        **首个样本推迟一个间隔**：阶段若短于一个间隔（就绪后立刻离场），全程不碰 OCR
        ——启停因此是零代价的。真实阶段以分钟计，少首帧不影响连续性。
        """
        while not self._stop.wait(self.interval_s):
            rec = self._sample_once()
            if rec is None:
                continue
            self._enqueue(rec)

    def _enqueue(self, rec: dict) -> None:
        """入队（非阻塞）；队满即丢并计数——反压口径照录制器，绝不阻塞采样侧。"""
        try:
            self._q.put_nowait(rec)
        except queue.Full:
            self._rows_dropped += 1

    def _sample_once(self) -> dict | None:
        """一次采样：非阻塞取帧 → 逐区域判在场/识别 → 组行。异常一律不抛。"""
        try:
            frame, frame_id, ts_ns, age_ms = self._frame_source()
        except Exception as exc:  # noqa: BLE001 —— 取帧异常按缺帧处理，不终止观察
            logger.log(f"[极速狂飙] HUD 取帧异常: {exc!r}", "DEBUG")
            self._samples_no_frame += 1
            return None
        if frame is None:
            # 缺帧不写行：seq 因此出现缺口，缺口数就是"这段时间没采到"的证据
            self._samples_no_frame += 1
            return None

        self._seq += 1
        t0 = time.perf_counter()
        fields: dict[str, dict] = {}
        for name, rect in self._regions.items():
            fields[name] = self._read_field(frame, name, rect)
        rec: dict = {
            "seq": self._seq,
            # frame_id / ts_ns 与 frames.jsonl 同源（同一帧号必带同一采集时刻），对齐靠它们
            "frame_id": int(frame_id),
            "ts_ns": int(ts_ns),
            "age_ms": round(float(age_ms), 3),
            "read_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "regions": self._digest,
            "fields": fields,
            "flags": [],
        }
        self._apply_read_filters(rec)
        return rec

    def _read_field(self, frame: Any, name: str, rect: list[float]) -> dict:
        """读一格：先过在场闸门（仅面板三格），再识别，最后给可信标志与理由。"""
        entry: dict = {"gate": None, "text": None, "value": None, "trusted": False,
                       "note": []}
        if name in SIDE_FIELDS:
            # 归属按块色判（位置会上下互换），与文本是否读出无关
            entry["side"] = block_side(frame, rect)
        if name in GATED_FIELDS:
            dark = field_on_dark(frame, rect)
            entry["gate"] = dark
            if not dark:
                # 面板不在场：不识别（省一次 OCR，且那里的字必然是垃圾）
                entry["note"].append("panel_absent")
                return entry

        res = self._recognize(frame, rect)
        if res is None:
            entry["note"].append("ocr_unavailable")
            self._ocr_no_result += 1
            return entry
        text = str(getattr(res, "text", "") or "").strip()
        entry["text"] = text
        if not text:
            entry["note"].append("no_text")
            return entry
        value = parse_value(name, text)
        entry["value"] = value
        if value is None:
            entry["note"].append("unparsable")
            return entry
        entry["trusted"] = True
        return entry

    def _recognize(self, frame: Any, rect: list[float]) -> Any | None:
        """单 ROI 识别。引擎懒加载、不可用即返回 None（不抛、不阻塞观察线程）。"""
        engine = self._get_engine()
        if engine is None:
            return None
        try:
            return engine.recognize(frame, rect)
        except Exception as exc:  # noqa: BLE001 —— 单格识别异常不该终止整条观察
            self._ocr_errors += 1
            level = "WARNING" if self._ocr_errors == 1 else "DEBUG"
            logger.log(f"[极速狂飙] HUD 识别异常({self._ocr_errors}): {exc!r}", level)
            return None

    def _get_engine(self) -> Any | None:
        """懒加载 OCR 引擎（取引擎失败后不再重试，不刷日志）。

        引擎唯一真源是 ``core.ocr``（对外收 RGB，内部只在 image_io 边界翻一次通道）；
        本模块不自建引擎、也不自写通道翻转。导入与构造都推迟到第一次识别——插件
        导入期不拉 RapidOCR，测试注入假引擎时更不会碰到真模型。
        """
        if self._engine is not None:
            return self._engine
        if self._engine_failed:
            return None
        try:
            from maaracing_master.core.ocr import RapidOcrEngine
            self._engine = RapidOcrEngine()
        except Exception as exc:  # noqa: BLE001 —— 引擎不可用即降级，不抛给调用方
            self._engine_failed = True
            logger.log(f"[极速狂飙] HUD 读数引擎不可用({exc!r})，本阶段不产出读数", "WARNING")
            return None
        return self._engine

    # ---------- 已知假读的过滤器 ----------

    def _apply_read_filters(self, rec: dict) -> None:
        """落实已知假读形态：标记而非丢弃（行照旧落盘，事后看得见"这个值不可信"）。

        两条判据：
        1. **``合计 < 里程`` 必是假读**——由公式「合计 = 里程 + 30×超车 ≥ 里程」自身导出。
           实测成因是面板的"矮版"变体没有合计那一行，该处仍是暗底、闸门拦不住，识别
           强行解码出 ``1``/``92`` 这类小数字。
        2. **单字面板读数与噪声不可区分**——面板缺席时被强行解码出的碎片与真值同形，
           单帧无从分辨（见 ``SINGLE_CHAR_SUSPECT_FIELDS`` 的字段范围说明）；时间连续性
           才是二次判据，故这里只标记。
        """
        fields = rec["fields"]
        total = fields.get("total_left") or {}
        mile = fields.get("mileage") or {}
        if (total.get("value") is not None and mile.get("value") is not None
                and total["value"] < mile["value"]):
            total["trusted"] = False
            total["note"].append("total_lt_mileage")
            rec["flags"].append("total_lt_mileage")
        for name in SINGLE_CHAR_SUSPECT_FIELDS:
            entry = fields.get(name)
            if entry is None or not entry["trusted"]:
                continue
            if len(str(entry["text"]).strip()) == 1:
                entry["trusted"] = False
                entry["note"].append("single_char")
                rec["flags"].append(f"{name}_single_char")

    # ---------- 落盘线程 ----------

    def _write_worker(self) -> None:
        """hud.jsonl 的唯一写者，故无需加锁；逐行 flush（低频，代价可忽略）。"""
        path = self.out_dir / "hud.jsonl"
        with path.open("w", encoding="utf-8") as f:
            while True:
                try:
                    rec = self._q.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                self._rows_written += 1
                if rec["flags"]:
                    self._rows_flagged += 1

    def _write_meta(self, reason: str) -> None:
        meta = {
            "schema": SCHEMA_VERSION,
            "started_at": self._started_iso,
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "monotonic_start_ns": self._started_ns,
            "phase": self.phase,
            "round_no": self.round_no,
            "sample_interval_s": self.interval_s,
            # 区域真源指纹 + 文件名（不落绝对路径：交付物不得含本机路径）
            "regions": self._digest,
            "regions_file": HUD_REGIONS_FILE.name,
            "fields": list(self._regions),
            "gate_fields": list(GATED_FIELDS),
            "side_fields": list(SIDE_FIELDS),
            "single_char_suspect_fields": list(SINGLE_CHAR_SUSPECT_FIELDS),
            "rows_written": self._rows_written,
            "rows_dropped": self._rows_dropped,
            "rows_flagged": self._rows_flagged,
            "samples_no_frame": self._samples_no_frame,
            "ocr_no_result": self._ocr_no_result,
            "ocr_errors": self._ocr_errors,
            "stop_reason": reason,
        }
        try:
            (self.out_dir / "hud_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 —— meta 写失败不该抛给调用方
            logger.log(f"[极速狂飙] HUD meta 写入失败: {exc!r}", "WARNING")