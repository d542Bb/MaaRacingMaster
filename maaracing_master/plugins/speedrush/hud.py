# -*- coding: utf-8 -*-
"""speedrush 驾驶页 HUD 实时读数：低频采样 → ``hud.jsonl``。

**要解决什么**：驾驶途中的战况与得分是**持续跳动**的量，离线稀疏帧给不出连续序列。本模块
在驾驶途中以低频把右侧四格（双方比分 + 我方得分速度）落成 ``hud.jsonl``，供事后与同会话
``frames.jsonl`` 按 ``frame_id`` / ``ts_ns`` 对齐（对齐方式与录制器同源：两者都取自
``ctx.capture.frame_with_age()``，同一帧号必带同一采集时刻）。

**它不读左侧卡片**（里程 / 超车 / 合计）：那张卡片每周期只在后段约三秒显示数字，随后进入
"经过了一段时间"的状态——其间三格不显示而统计仍在后台累加，故可见值**滞后一个周期**（语义
见 ``RULES.md`` §4.8）。它作为实时判据不可用，不在本模块的读取范围内；需要离线复算那三格时
走实验探针（自包含，读同一份区域真源）。

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

**读不出来不静默丢、也不冒充读数**：每行同时记**原始文本**、**解析值**、**比分块按颜色判出的
归属**，以及逐格的可信标志（``trusted``）与理由（``note``）。面板不在场或识别失败的那格只带
理由、不带值——"没读到"与"读到 0"必须能分开，行照旧落盘。

**比分面板的归属必须成对判**（``pair_sides``）：两块面板必是一蓝（本机）一红（对手），
而面板是半透明的——背景景物会给**两块**染上同一偏色，单块绝对阈值在白天蓝天场会把两块
都判成"本机"（实测 62/69 行错，且下游据此产出过 51 处假"比分回落"）。故按**两块之差**
定归属（公共偏色被抵消），差太小则两块都弃权 ``?``；条带采样同时收窄到**最饱和的一撮**
（色条本身），让色条而非背景主导。逐格记 ``side_source``（``pair``/``abstain``）留痕——
单块兜底已不在运行期：成对判不可用即整拍弃权。

**区域真源**：``resources/policy/hud_regions.json``（本插件内唯一一份，离线探针读同一份）。
本模块不复制任何 rect 常量；真源里的键**按角色三分**（读格 / 判色带 / 跳过），角色之外没有
第四种，缺角或半改名都会在加载期 fail loud。

**读数框按「卡片色 × 槽位」取名**（``score_blue_a`` …）：两块卡的**版式不同**——本机那张多
一行「得分速度」、比分字号更大、**比分右缘更靠左**（实测 ≈x1135，对手 ≈x1155），两卡的图标
位置也不同。故同一个槽位里两块卡的数值带**并不重合**：按槽位取一个"够大的框"，会把对手比分
的末位读掉（实测 8500→850）或把图标读进来。**身份先判、再按身份选题框**——判色带（``pair_*``）
与读数框分开正是为此：判色不能依赖身份，否则就成了循环依赖（见 ``pair_sides``）。

**敌我判据搬自实验探针**（``tools/experiments/speedrush_scoring/probe_hud_ocr.py`` 的
``pair_sides()``）：口径一致；探针按实验自包含约定自带一份代码副本（它不 import 本模块），
只有区域**数据**是单一真源。
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
    "PAIR_FIELDS",
    "SAMPLE_INTERVAL_S",
    "SCHEMA_VERSION",
    "SIDE_FIELDS",
    "SIDE_PAIR_MIN_DELTA",
    "SKIPPED_FIELDS",
    "band_blue_bias",
    "load_hud_regions",
    "pair_sides",
    "parse_int",
    "parse_value",
    "region_digest",
    "region_roles",
]

# 记录格式版本。字段语义变动时递增，供离线消费方识别。
# v2：卡片三格增记 ``settled``（该三格与定值判据已于 v5 一并移除）。
# v3：比分格增记 ``side_source``（归属来自成对判还是单块兜底），且归属改为成对判
#     ——v2 及以前按单块绝对阈值判，白天蓝天场会把两块判成同一方（见 ``pair_sides``）。
# v4：右侧四格由位置名改为**槽位名**（``score_a/b``、``rate_a/b``）——名字不再承载敌我；
#     速度格只在"本槽是本机"时识别；成对判不可用时归属**弃权**，``side_source`` 取
#     ``pair`` / ``abstain``。
# v5：**停读左侧卡片三格**（里程 / 超车 / 合计）——它只在周期后段约三秒可见、且值是
#     **滞后一个周期**的快照（语义见 ``RULES.md`` §4.8），不适合作为实时判据；随之一并去掉
#     在场闸门、定值判据与两类假读标记（它们只服务那三格）。字段集自此只剩右侧四格。
# v6：**判色带与读数框分家**，读数框改按「卡片色 × 槽位」取（``score_blue_a`` …）——两卡版式
#     不同（本机那张多一行速度、比分右缘更靠左），按槽位取一个够大的框会读掉对手比分末位、
#     或把图标读进来（实测；见模块 docstring）。行内四格仍是槽位名，每格增记 ``rect``——那格
#     这一次到底用哪块框读的。
SCHEMA_VERSION = 6

# 采样间隔（秒）：比分与速度是持续跳动的量，0.5s 足以看清增减；四格单 ROI OCR 实测
# 10~30ms，一轮约 0.04~0.12s，且全程在独立线程里。
SAMPLE_INTERVAL_S = 0.5

# 采样队列容量（约 4 秒）。满则丢样本计数，绝不阻塞采样线程——反压口径照录制器
# （``put_nowait`` + 丢弃计数），落盘慢不该拖住下一次取帧。
_QUEUE_MAX = 8

# 比分面板：**位置会上下互换，故这两格是「槽位」而不是敌我**——a = 上档、b = 下档。
# 敌我由块色成对判（见 ``pair_sides``）写进每格 ``side``；消费方按 ``side`` 取、不按槽位取。
SIDE_FIELDS: tuple[str, ...] = ("score_a", "score_b")
# 得分速度读数（与同槽位的比分格配对：``rate_a`` 恒在 ``score_a`` 上方）。
# **它只为本方显示**（实测本机侧 100% 有读数、对手侧只有零星几处——那是背景的假读）：
# 故只识别"本槽是蓝"的那一格，另一格连识别都不做（读到的是面板背景/空区）。
RATE_FIELDS: tuple[str, ...] = ("rate_a", "rate_b")
# 本模块**不读**的区域：左侧卡片三格。真源里保留它们的 rect（离线探针要读），故这里是
# **跳过名单**而不是读取名单——采样范围仍是"真源的键集减去这三个"，其余键集既不假定也不写死。
SKIPPED_FIELDS: tuple[str, ...] = ("mileage", "overtake", "total_left")
# **判色带**：只用于判归属，一次 OCR 都不做（判色靠像素饱和度，不需要读字）。它必须覆盖
# 两块卡各自的色条；与读数框分开，是为了让"判身份"不依赖身份（否则是循环依赖）。
PAIR_FIELDS: tuple[str, ...] = ("pair_a", "pair_b")

# 槽位后缀（a=上档、b=下档）由比分格派生，不另抄一份。
_SLOTS: tuple[str, ...] = tuple(n.rsplit("_", 1)[1] for n in SIDE_FIELDS)
# 卡片色的取值与"敌我 → 颜色"的映射。**这条映射是已验证的**（官方规则页 + 维护者口径 +
# 速度行只在本机卡上出现三重佐证），故可以进真源的键名——"名字不得承载**未验证**的语义假设"
# 这条判据管的是未验证的假设，不是已验证的事实（见 CODE_WIKI §5）。
_CARD_TOKENS: tuple[str, ...] = ("blue", "red")
_CARD_OF_SIDE: dict[str, str] = {"本机(蓝)": "blue", "对手(红)": "red"}
# 真源里属于"读数框"的键全集：比分两色 × 两槽，速度只在本机卡上（对手没有那一行）。
_CARD_RECTS: tuple[str, ...] = tuple(
    f"score_{c}_{s}" for c in _CARD_TOKENS for s in _SLOTS
) + tuple(f"rate_blue_{s}" for s in _SLOTS)

# 比分块归属的判据参数（口径源自探针的逐格复核，2026-09-17 起收窄到"最饱和一撮"）：
# 用固定采样点会被天空骗过（天空同样满足 B>R）；用**全部**饱和像素会被半透明面板背后的
# 景物骗过（白天蓝天场两块条带都偏蓝，见 ``pair_sides``）。故先按饱和度筛，再只取最高的一撮。
_SIDE_MIN_SAT = 80.0
_SIDE_MIN_PIXELS = 50
SIDE_TOP_QUANTILE = 0.95
# 两块条带的 (B−R) 之差小于此值即两块都弃权：归属靠"谁更蓝"，公共偏色下差值被压缩，
# 差太小说明这一刻没有可用信号（实测正常场里也有单帧差 5.5、问题场最小 86）。
SIDE_PAIR_MIN_DELTA = 40.0

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
    数据错误。**额外要求**：不得有 ``_`` 前缀键（真源是纯数据、不放说明）；角色的键必须齐备
    ——判色带 ``PAIR_FIELDS``、读数框 ``_CARD_RECTS``（缺一个都只能整段弃权）；且不得出现
    多余的 ``pair_*`` / ``score_*`` / ``rate_*`` 键（半改名会让某些格静默读到别的框）。
    真源里可以保留别的键（例如左侧卡片三格），它们由 ``SKIPPED_FIELDS`` 声明为不读。

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

    missing = [f for f in PAIR_FIELDS if f not in out]
    if missing:
        raise ValueError(f"区域真源缺判色带 {missing}（归属判据依赖它们）")
    missing = [f for f in _CARD_RECTS if f not in out]
    if missing:
        raise ValueError(
            f"区域真源缺读数框 {missing}（行内 {list(SIDE_FIELDS + RATE_FIELDS)} 依赖它们）")
    stray = sorted(n for n in out
                   if n.split("_", 1)[0] in ("pair", "score", "rate")
                   and n not in _CARD_RECTS and n not in PAIR_FIELDS)
    if stray:
        raise ValueError(f"区域真源有多余的比分/速度键 {stray}（半改名会静默读到别的框）")
    return out


def region_roles(regions: dict[str, list[float]]) -> dict[str, str]:
    """把真源键集判成**四种角色**（供机检与消费者对齐，不另抄一份清单）。

    - ``plain``：普通读格（阶段 / 计时 / 横幅），照自己的框读、读进行内同名格。
    - ``pair``：判色带，只用于判归属，不读字。
    - ``card``：读数框，按「卡片色 × 槽位」取，读进行内 ``score_*`` / ``rate_*`` 槽位格。
    - ``skip``：本模块不碰（左侧卡片三格，留给离线探针）。

    角色之外没有第五种：任何键必须落在其中之一，否则就是真源与读取路径已经脱节。
    """
    roles: dict[str, str] = {}
    for name in regions:
        if name in SKIPPED_FIELDS:
            roles[name] = "skip"
        elif name in PAIR_FIELDS:
            roles[name] = "pair"
        elif name in _CARD_RECTS:
            roles[name] = "card"
        else:
            roles[name] = "plain"
    return roles


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


def band_blue_bias(frame_rgb: Any, rect) -> float | None:
    """条带内**最饱和一撮**像素的 (B−R) 均值；像素不足以判时 None。

    为什么取"最饱和的一撮"而不是"全部饱和像素"：面板是**半透明**的，背景景物会给条带整体
    染上同一个偏色——白天蓝天场实测两块条带的 (B−R) **都是正的**（上块 +68、下块 +130），
    此时"整体偏蓝"不再是归属信号。色条是实色块，饱和度显著高于背景与被染色的面板本身，
    故取饱和度的最高一撮让色条主导（实测最小差值由 41 提到 86，分离更干净）。

    非 3 通道帧直接返回 None：``reshape(-1, 3)`` 会把通道错位分组，算出一个看着合理但错的
    归属——与其静默给错标签，不如说判不出。
    """
    if frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
        return None
    h, w = frame_rgb.shape[:2]
    x1, y1, x2, y2 = _px_bounds(rect, w, h)
    reg = frame_rgb[y1:y2, x1:x2]
    if reg.size == 0:
        return None
    flat = reg.reshape(-1, 3).astype(float)
    sat = flat.max(axis=1) - flat.min(axis=1)
    sel = flat[sat > _SIDE_MIN_SAT]
    if len(sel) < _SIDE_MIN_PIXELS:
        return None
    sat_sel = sel.max(axis=1) - sel.min(axis=1)
    # 分位阈值用排序取下标算（只对饱和子集排序，规模很小）——不为此在模块里引 numpy
    ordered = sorted(float(x) for x in sat_sel)
    cut = ordered[min(len(ordered) - 1, int(SIDE_TOP_QUANTILE * len(ordered)))]
    top = sel[sat_sel >= cut]
    if len(top) < _SIDE_MIN_PIXELS:
        return None
    return float(top[:, 2].mean() - top[:, 0].mean())


def pair_sides(frame_rgb: Any, rect_a, rect_b) -> tuple[str, str]:
    """**成对判**两块比分面板的归属：``(rect_a 的归属, rect_b 的归属)``。

    这条是面板归属的**权威判据**，理由是一条物理约束——两块面板必是一块蓝（本机）、一块红
    （对手），"两块同判"不可能。于是用**两块之差**定归属，公共偏色被抵消：

    - 白天蓝天场实测（问题现场）：上块 B−R=+68、下块 +130。单块绝对判据会因两块都偏蓝而把
      **两块都判成"本机"**（那一场 62/69 行都错，且工具据此产出了 51 处假"比分回落"）；成对
      判取差值 → 下块更蓝 → 下块是本机，与画面一致。
    - 夜里深色背景场：上块 +159、下块 −115，差 274，同样得到"上块本机"。

    差值小于 ``SIDE_PAIR_MIN_DELTA`` 时两块**都弃权**（``?``）——多半是面板正在淡入淡出、
    两块都还没显出各自颜色（实测正常场里也有单帧差 5.5 的样本）。**不猜**是这里的正确行为：
    消费方拿到 ``?`` 就知道这一刻的归属不可用，而不是拿到一个看着合理的错标签。
    """
    a = band_blue_bias(frame_rgb, rect_a)
    b = band_blue_bias(frame_rgb, rect_b)
    if a is None or b is None or abs(a - b) < SIDE_PAIR_MIN_DELTA:
        return "?", "?"
    if a > b:
        return "本机(蓝)", "对手(红)"
    return "对手(红)", "本机(蓝)"


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
        self._samples_no_frame = 0
        self._ocr_no_result = 0
        self._ocr_errors = 0
        self._read_errors = 0
        self._write_errors = 0
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
            "samples_no_frame": self._samples_no_frame,
            "ocr_no_result": self._ocr_no_result,
            "read_errors": self._read_errors,
            "write_errors": self._write_errors,
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
            f"（丢 {self._rows_dropped}、"
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
        sides = self._side_pair(frame)
        fields: dict[str, dict] = {}
        # 普通格（阶段 / 计时 / 事件横幅）：各读各的框
        for name, rect in self._regions.items():
            if name in SKIPPED_FIELDS or name in PAIR_FIELDS or name in _CARD_RECTS:
                continue
            fields[name] = self._read_field_safe(frame, name, rect)
        # 行内四格用**槽位名**（敌我随面板换位而变）：先判身份，再按身份取那一块卡的框。
        # 顺序按「两个比分 → 两个速度」（与真源里旧版的分组一致，读记录时不必重新拼）
        for prefix in ("score", "rate"):
            for slot in _SLOTS:
                name = f"{prefix}_{slot}"
                fields[name] = self._read_field_safe(
                    frame, name, None, slot=slot, side=sides.get(slot))
        return {
            "seq": self._seq,
            # frame_id / ts_ns 与 frames.jsonl 同源（同一帧号必带同一采集时刻），对齐靠它们
            "frame_id": int(frame_id),
            "ts_ns": int(ts_ns),
            "age_ms": round(float(age_ms), 3),
            "read_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "regions": self._digest,
            "fields": fields,
        }

    def _row_fields(self) -> list[str]:
        """行内格名（由真源角色派生，不另抄一份清单）：普通格 + 四个槽位卡格。"""
        plain = [n for n in self._regions if n not in SKIPPED_FIELDS
                 and n not in PAIR_FIELDS and n not in _CARD_RECTS]
        return plain + list(SIDE_FIELDS) + list(RATE_FIELDS)

    def _side_pair(self, frame: Any) -> dict[str, str]:
        """一次采样算一次各槽位的**敌我**：只用**判色带**成对判两块卡的颜色。

        为什么整帧只算一次：归属是一条**物理约束**下的联合判断（一蓝一红），逐格各判会
        退化成单块绝对判据——那正是实测出错的形态（白天蓝天场两块都判成"本机"）。这里
        算错也只是本拍归属不可用（返回空 dict），不影响其它格。

        判色**只用判色带**（不做 OCR）：读数框按身份取，若判身份也要先读字，就成了循环依赖。
        """
        names = [n for n in PAIR_FIELDS if n in self._regions]
        if len(names) != 2:
            return {}
        try:
            a, b = pair_sides(frame, self._regions[names[0]], self._regions[names[1]])
        except Exception as exc:  # noqa: BLE001 —— 归属算不出来就整拍弃权，不终止观察
            logger.log(f"[极速狂飙] HUD 归属成对判异常: {exc!r}", "DEBUG")
            return {}
        return {n.rsplit("_", 1)[1]: side for n, side in zip(names, (a, b))}

    def _read_field(self, frame: Any, name: str, rect: list[float] | None,
                    *, slot: str | None = None, side: str | None = None) -> dict:
        """读一格：识别并给出可信标志与理由。

        两类格子两条路：

        - **普通格**（阶段 / 计时 / 横幅）：直接读自己的框。
        - **行内卡格**（两个比分槽 + 两个速度槽，``slot`` 非空）：先看这一槽判出的**身份**，
          再由身份选出**那一块卡**的读数框——两卡版式不同，框不能按槽位共用（见模块
          docstring）。身份判不出（``?``）就**不读**：选框本身就需要身份，猜一个去读等于
          把错框里的数字当读数。用哪块框记进 ``rect``，事后可查。
        """
        entry: dict = {"text": None, "value": None, "trusted": False, "note": []}
        if slot is not None:
            # 敌我按块色判（面板会上下互换），与文本是否读出无关。**只认成对判**：单块绝对
            # 阈值在强公共偏色下会把两块判成同一方（见 ``pair_sides`` 的实测），故成对判
            # 不可用时**弃权**——宁可为空，也不给一个看着合理的错身份。
            entry["side"] = side if side is not None else "?"
            entry["side_source"] = "pair" if side is not None else "abstain"
            token = _CARD_OF_SIDE.get(side or "")
            if token is None:
                entry["note"].append("side_unknown")
                return entry
            if name.startswith("rate_") and token != "blue":
                # 得分速度**只为本方显示**：对手那一格是空的/背景，识别只会产出垃圾
                # （实测那块能读出背景值）。那一格连识别都不做。
                entry["note"].append("not_displayed")
                return entry
            rect = self._regions[f"{name.split('_', 1)[0]}_{token}_{slot}"]
            entry["rect"] = f"{name.split('_', 1)[0]}_{token}_{slot}"
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

    def _read_field_safe(self, frame: Any, name: str, rect: list[float] | None,
                         *, slot: str | None = None, side: str | None = None) -> dict:
        """``_read_field`` 的护栏：单格读挂只废掉那一格，**绝不让异常逃到采样线程**。

        为什么必须在这里兜：采样线程没有外层 try——异常一旦逃出去线程就静默死掉，
        实机上的表现是"跑了一整轮却只有几行"，而且事后无从归因（meta 里没有任何一项
        指向"线程死了"）。成因不是假想：块色判据要对切片 ``reshape(-1, 3)``，拿到非
        3 通道的帧就抛。判据本身照旧 fail loud（区域真源非法时构造期就抛），这里兜的是
        **运行期单帧异常**这一类。
        """
        try:
            return self._read_field(frame, name, rect, slot=slot, side=side)
        except Exception as exc:  # noqa: BLE001 —— 单格异常按"该格读不出"处理
            self._read_errors += 1
            level = "WARNING" if self._read_errors == 1 else "DEBUG"
            logger.log(
                f"[极速狂飙] HUD 单格读数异常（{name}，累计 {self._read_errors}）: {exc!r}", level)
            entry: dict = {"text": None, "value": None, "trusted": False,
                           "note": ["read_error"]}
            if slot is not None:
                entry["side"] = side if side is not None else "?"
                entry["side_source"] = "pair" if side is not None else "abstain"
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

    # ---------- 落盘线程 ----------

    def _write_worker(self) -> None:
        """hud.jsonl 的唯一写者，故无需加锁；逐行 flush（低频，代价可忽略）。

        单行写失败**不终止写线程**（磁盘满、序列化意外都一样）：线程一死，队列再也不会
        被排空、后续读数全部丢失，而这些只在事件结束时才被发现——计数与 WARNING 至少要
        留下"丢在哪一步"的痕迹。
        """
        path = self.out_dir / "hud.jsonl"
        with path.open("w", encoding="utf-8") as f:
            while True:
                try:
                    rec = self._q.get(timeout=0.2)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                try:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
                except Exception as exc:  # noqa: BLE001 —— 单行写失败不终止写线程
                    self._write_errors += 1
                    level = "WARNING" if self._write_errors == 1 else "DEBUG"
                    logger.log(
                        f"[极速狂飙] HUD 落盘失败（累计 {self._write_errors}）: {exc!r}", level)
                    continue
                self._rows_written += 1

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
            "fields": self._row_fields(),
            "skipped_fields": list(SKIPPED_FIELDS),
            "pair_fields": list(PAIR_FIELDS),
            "side_fields": list(SIDE_FIELDS),
            "rate_fields": list(RATE_FIELDS),
            "card_rects": list(_CARD_RECTS),
            "rows_written": self._rows_written,
            "rows_dropped": self._rows_dropped,
            "samples_no_frame": self._samples_no_frame,
            "ocr_no_result": self._ocr_no_result,
            "read_errors": self._read_errors,
            "write_errors": self._write_errors,
            "ocr_errors": self._ocr_errors,
            "stop_reason": reason,
        }
        try:
            (self.out_dir / "hud_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 —— meta 写失败不该抛给调用方
            logger.log(f"[极速狂飙] HUD meta 写入失败: {exc!r}", "WARNING")