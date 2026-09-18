"""speedrush 录制复盘：从 `hud.jsonl` 复算「每个时刻的分从哪来」。

**只读落盘产物，不再跑 OCR**：画面判读在生产侧已经做过一次
（`plugins/speedrush/hud.py` 的观察线程）。复盘再识别一遍既慢，又会引入第二套判据——
同一帧两次识别结果不同时，谁对？所以本工具的输入就是那次采样留下的原始文本、解析值
与判定标志，它做的是**在读数之上做算术与不变量检验**。

**三问**
  1. 这份读数**可用吗**：计数自洽、与录制帧对齐、假读标出（`check`）；
  2. 每个时刻的分**从哪来**：时间线 + 增量归因，侧别先归一（`timeline`）；
  3. 左侧卡片的**窗口模型**成立吗：在场窗口切分 → 周期 / 时长 / 窗内稳定值（`windows`）。

**判据一：定值（settled）。** 左侧卡片周期性出现，出现时三个数字先**滚动爬升**再停住
（首轮实机数据：268→292→317→338，随后 338 连续重复）。爬升段读到的值"看起来很合理"
（338 与 292 都是合法里程），但它只是动画中间帧；拿它算增量会得到假的分数来源，拿它
比对公式会得到假的违例。故对每个字段判「**连续两次采样相同**」才算定值，非定值的可信
行标 `ramp`，增量归因与公式检验只用定值点。这条判据**已由生产侧给出**（`hud.py` 的
`_mark_settled`，SCHEMA 2 起每格带 `settled`）——本工具优先读它，只有旧会话（SCHEMA 1）
才按**同一规则**重算，两处不许各写一套。

**判据二：不变量。** 两条，都只作用在定值点上：
  - **合计 = 里程 + 30 × 超车**（换算系数由探针 `formula` 模式给出，实机逐点确认）；
  - **比分单调不减**（掉一位数的假读实测存在：`18804`、`39`、`458`——且它们都带着
    `trusted=True`，靠行内标志是拦不住的）。
违例点因此有两种解释：读数假了，或规则不成立。工具把它们**都列出来**，不替调用方选。

**本脚本自包含**（实验目录约定，见 `tools/experiments/README.md`）：不 import
`maaracing_master`。区域真源仍在插件内（唯一的区域真源），本工具只从 `hud_meta.json`
读其指纹用于核对，不复制 rect；出图所需的框在此仅作**裁剪视图**用，不参与判读。

用法：
    python tools/experiments/speedrush_scoring/review_run.py check --last 2
    python tools/experiments/speedrush_scoring/review_run.py timeline --session 20260917_200445_p1
    python tools/experiments/speedrush_scoring/review_run.py windows
    python tools/experiments/speedrush_scoring/review_run.py sheet --session 20260917_200445_p1
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 录制会话根目录：默认按 Windows 用户数据目录推导，可用 --demos 或环境变量覆盖
DEFAULT_DEMOS = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
                 / "speedrush" / "demos")

# 左侧周期卡片的三格；窗口切分与"定值"判据都只作用于它们
CARD_FIELDS: tuple[str, ...] = ("mileage", "overtake", "total_left")
# 比分面板两格：**槽位名**（a=上档、b=下档），不是敌我——敌我按行内 side 判
SIDE_FIELDS: tuple[str, ...] = ("score_a", "score_b")
# 得分速度两格：只为本方显示（对手那格在 2026-09-17 之后连识别都不做）
RATE_FIELDS: tuple[str, ...] = ("rate_a", "rate_b")
# 旧会话（区域真源还是位置名时录的）→ 新槽位名。复盘要能读历史文件，故载入时统一改名；
# 顺带提醒：旧文件里"对手那一格"的得分速度是**无条件识别**出来的，可能是背景假读。
LEGACY_FIELD_RENAMES = {"score_top": "score_a", "score_bottom": "score_b",
                        "rate_top": "rate_a", "rate_bottom": "rate_b"}

# 合计 = 里程 + w×超车。w 由探针 formula 模式定出，本工具作不变量复核。
OVERTAKE_WEIGHT = 30

# 「连续两次采样相同」的相邻判定上限：与生产侧 `hud.SETTLE_GAP_S` 同值（口径见 settled_map）
SETTLE_GAP_S = 1.5
# 面板在场行之间超过这个间隔即算两个窗口（实测窗时长约 3.5s、间隔约 10s）
BURST_GAP_S = 2.0
# 与录制帧对齐的容差（实测同帧为 ts_ns 精确相等；容差只用于统计"邻近帧"）
PAIR_TOL_NS = 50_000_000
# 比分假读的"串位尖峰"判据：值高于前后可信点且超出量级（见 side_series）
SPIKE_FACTOR = 3.0
# 认得的 `hud_meta.schema`：1 无 per-field `settled`；2 有（回退重算，见 settled_map）；
# 3 归属改为成对判并记 `side_source`；4 右侧四格改槽位名、速度格按归属读、归属判不出即弃权
HUD_SCHEMAS = (1, 2, 3, 4)

# 出图：卡片由「全帧缩略 + 左侧卡片放大 + 比分面板放大」三块拼成，标注条在底
CELL_W = 1264
BAR_H = 46
THUMB_W = 460
PANEL_BOX = (0.050, 0.015, 0.220, 0.300)
SCORE_BOX = (0.755, 0.015, 0.930, 0.280)
ZOOM = 1.45
SHEET_COLS, SHEET_ROWS = 2, 3


# ---------------- 读盘 ----------------

def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path} 第 {lineno} 行不是合法 JSON：{exc}") from exc
    return out


def _json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Row:
    """一次 HUD 采样（`hud.jsonl` 一行）+ 复盘时贴上的标签。"""

    seq: int
    ts_ns: int
    frame_id: int
    fields: dict
    flags: list[str]
    t: float = 0.0                 # 相对本会话首个采样点的秒数
    tags: list[str] = field(default_factory=list)

    def f(self, name: str) -> dict:
        return self.fields.get(name) or {}

    def trusted(self, name: str) -> bool:
        return bool(self.f(name).get("trusted"))

    def val(self, name: str):
        return self.f(name).get("value")

    def text(self, name: str) -> str:
        return (self.f(name).get("text") or "")

    def side(self, name: str) -> str:
        return (self.f(name).get("side") or "")

    def present(self) -> bool:
        """左侧周期卡片这一行是否在场（任一格读出并可信）。"""
        return any(self.trusted(f) for f in CARD_FIELDS)


@dataclass
class Session:
    path: Path
    meta: dict
    hud_meta: dict
    frames: list[dict]
    rows: list[Row]
    pad_samples: int

    @property
    def name(self) -> str:
        return self.path.name

    def frame_index(self) -> dict[int, dict]:
        return {int(fr["ts_ns"]): fr for fr in self.frames}

    def nearest_frame(self, ts_ns: int) -> tuple[dict | None, int]:
        """最近的录制帧与其时间差（ns）。帧表已按 ts_ns 升序落盘，可直接折半。"""
        import bisect
        keys = [int(fr["ts_ns"]) for fr in self.frames]
        if not keys:
            return None, 1 << 62
        i = bisect.bisect_left(keys, ts_ns)
        best, gap = None, 1 << 62
        for j in (i - 1, i):
            if 0 <= j < len(keys):
                g = abs(keys[j] - ts_ns)
                if g < gap:
                    best, gap = self.frames[j], g
        return best, gap


def load_session(path: Path) -> Session:
    """读一个录制会话；缺 `hud.jsonl` 时明说原因与补救（不是静默当空数据）。"""
    path = Path(path)
    if not (path / "meta.json").is_file():
        raise SystemExit(f"{path} 不是录制会话（无 meta.json）")
    if not (path / "hud.jsonl").is_file():
        raise SystemExit(
            f"{path} 没有 hud.jsonl——该会话未开启录制模式（HUD 观察线程只在录制模式下"
            f"启动：对齐要靠同会话的 frames.jsonl）")
    hud_meta = _json(path / "hud_meta.json")
    schema = hud_meta.get("schema")
    if schema is not None and schema not in HUD_SCHEMAS:
        raise SystemExit(f"{path} 的 hud_meta.schema={schema}，本工具按 {HUD_SCHEMAS} 读——"
                         f"格式已变，先对齐字段语义再加模式")
    rows = []
    for r in _jsonl(path / "hud.jsonl"):
        raw_fields = r.get("fields") or {}
        rows.append(Row(seq=int(r["seq"]), ts_ns=int(r["ts_ns"]),
                        frame_id=int(r.get("frame_id", -1)),
                        fields={LEGACY_FIELD_RENAMES.get(k, k): v for k, v in raw_fields.items()},
                        flags=list(r.get("flags") or [])))
    if rows:
        t0 = rows[0].ts_ns
        for r in rows:
            r.t = (r.ts_ns - t0) / 1e9
    return Session(path=path, meta=_json(path / "meta.json"), hud_meta=hud_meta,
                   frames=_jsonl(path / "frames.jsonl"), rows=rows,
                   pad_samples=len(_jsonl(path / "pads.jsonl")))


# ---------------- 判据：定值 + 不变量 ----------------

def settled_map(rows: list[Row], name: str) -> dict[int, bool]:
    """每个可信采样点是否**定值**。

    口径与生产侧（`plugins/speedrush/hud.py` 的 `_mark_settled`）**逐字一致**：与**上一个**
    可信采样同值、且间隔 ≤ ``SETTLE_GAP_S``。同一事实在两处各写一套判据迟早会分叉，故落盘行
    里带 ``settled`` 时**直接取它**（生产侧算的那一位就是权威），只有 SCHEMA 1 的旧会话没有
    这一位时才按同一规则重算——两条路的结果必然相同。

    "与上一个相同"而非"与下一个相同"：写盘是流式的，生产侧那一拍看不到未来。复盘本可以看
    未来（把每段平台的第一个采样点也认成定值），但那会让工具与生产侧对同一个值给出不同判定，
    正是要避免的第二真源——所以这里不退让。
    """
    idx = [i for i, r in enumerate(rows) if r.trusted(name)]
    out: dict[int, bool] = {}
    prev: tuple[int, int] | None = None      # (value, ts_ns) —— 上一个可信采样
    for i in idx:
        r = rows[i]
        recorded = r.f(name).get("settled")
        if recorded is None:                 # SCHEMA 1：无此位 → 按同一规则重算
            same = (prev is not None and prev[0] == r.val(name)
                    and abs(r.ts_ns - prev[1]) / 1e9 <= SETTLE_GAP_S)
            out[i] = bool(same)
        else:
            out[i] = bool(recorded)
        prev = (r.val(name), r.ts_ns)
    return out


@dataclass
class Findings:
    settle: dict[str, dict[int, bool]] = field(default_factory=dict)
    formula_bad: list[tuple[Row, int, int, int]] = field(default_factory=list)
    formula_ok: int = 0
    nonmono: list[tuple[Row, str, int, int, str]] = field(default_factory=list)
    timer_bad: list[tuple[Row, int, int]] = field(default_factory=list)
    n_exact: int = 0
    n_near: int = 0
    n_no_frame: int = 0
    n_side_conflict: int = 0
    gap_median_ms: float = 0.0
    gap_max_ms: float = 0.0
    bursts: list[list[int]] = field(default_factory=list)


def annotate(sess: Session) -> Findings:
    """给每行贴标签（`ramp:*` / `mismatch:total` / `nonmono:*` / `timer:*` / `hud:*`）。

    标签是**给人和给下一步筛选用的**，不改变原始字段——原始文本永远留在 `hud.jsonl` 里，
    复盘结论必须能回到它。
    """
    f = Findings()
    rows = sess.rows
    for name in (*CARD_FIELDS, *SIDE_FIELDS, *RATE_FIELDS, "stage", "timer", "event_banner"):
        if any(name in r.fields for r in rows):
            f.settle[name] = settled_map(rows, name)

    # 爬升行：卡片三格里"可信但未定值"的采样点——动画中间值就是这一类的常客
    for i, r in enumerate(rows):
        for name in CARD_FIELDS:
            if r.trusted(name) and not f.settle[name].get(i, False):
                r.tags.append(f"ramp:{name}")

    # 公式：只看三格都可信且都定值的采样点
    for i, r in enumerate(rows):
        if not all(r.trusted(n) and f.settle[n].get(i, False) for n in CARD_FIELDS):
            continue
        mi, ov, to = r.val("mileage"), r.val("overtake"), r.val("total_left")
        if to == mi + OVERTAKE_WEIGHT * ov:
            f.formula_ok += 1
        else:
            r.tags.append("mismatch:total")
            f.formula_bad.append((r, mi, ov, to))

    # 比分单调：按归属（本机/对手）分列，位置上下互换不影响；两块同判的行**不进检验**
    # （归属不可用，硬算会把两家的值混成一条序列——那正是"51 处假回落"的成因）
    seen: dict[str, tuple[int, Row]] = {}
    for r in rows:
        if side_conflict(r):
            f.n_side_conflict += 1
            r.tags.append("side:conflict")
            continue
        for name in SIDE_FIELDS:
            if not r.trusted(name):
                continue
            key = side_of(r, name)
            if key == "?":
                continue  # 归属未知（含两块同判以外的弃权）：不进检验，免得把两块混成一条
            v = r.val(name)
            if v is None:
                continue
            prev = seen.get(key)
            if prev is not None and v < prev[0]:
                r.tags.append(f"nonmono:{name}")
                f.nonmono.append((r, key, prev[0], v, prev[1].seq))
            seen[key] = (v, r)

    # 计时：mm:ss 倒计时应单调不增；阶段切换会重置，故按 stage 值分段比对
    stage = None
    prev_t = None
    for r in rows:
        if r.trusted("stage"):
            cur_stage = r.val("stage")
            if cur_stage != stage:
                stage, prev_t = cur_stage, None
        if not r.trusted("timer"):
            continue
        v = r.val("timer")
        if v is None:
            continue
        if prev_t is not None and v > prev_t:
            r.tags.append("timer:up")
            f.timer_bad.append((r, prev_t, v))
        prev_t = v

    # 对齐：同帧 ts_ns 精确相等为常态（两端都取自中心缓存的同一帧）
    by_ts = sess.frame_index()
    gaps = []
    for r in rows:
        if r.ts_ns in by_ts:
            f.n_exact += 1
            gaps.append(0)
            continue
        fr, gap = sess.nearest_frame(r.ts_ns)
        if fr is None or gap > PAIR_TOL_NS:
            f.n_no_frame += 1
            continue
        f.n_near += 1
        gaps.append(gap / 1e6)
    if gaps:
        gs = sorted(gaps)
        f.gap_median_ms = gs[len(gs) // 2]
        f.gap_max_ms = gs[-1]

    # 在场窗口
    present = [i for i, r in enumerate(rows) if r.present()]
    for i in present:
        if f.bursts and rows[i].ts_ns - rows[f.bursts[-1][-1]].ts_ns <= BURST_GAP_S * 1e9:
            f.bursts[-1].append(i)
        else:
            f.bursts.append([i])

    for r in rows:
        for flag in r.flags:
            r.tags.append(f"hud:{flag}")
    return f


def runs_of(rows: list[Row], idx: list[int], name: str) -> list[tuple[int, int]]:
    """一段窗口内某字段的**同值连续段**（在给定行序上）。"""
    out: list[tuple[int, int]] = []
    k = 0
    while k < len(idx):
        j = k
        v = rows[idx[k]].val(name)
        while j + 1 < len(idx) and rows[idx[j + 1]].trusted(name) \
                and rows[idx[j + 1]].val(name) == v:
            j += 1
        if j > k:
            out.append((v, j - k + 1))
        k = j + 1
    return out


def burst_line(rows: list[Row], idx: list[int], prev_start_s: float | None) -> str:
    """一个窗口一行字：起止 / 时长 / 周期 / 各格「爬升 → 定值」。"""
    t1, t2 = rows[idx[0]].t, rows[idx[-1]].t
    parts = []
    for name in CARD_FIELDS:
        ramp = [rows[i].val(name) for i in idx
                if rows[i].trusted(name) and rows[i].val(name) is not None]
        rs = runs_of(rows, idx, name)
        settled = rs[-1][0] if rs else None
        head = "→".join(str(v) for v in ramp[:4]) or "—"
        tail = f"定 {settled}" if settled is not None else "未定值"
        parts.append(f"{name}={head}{'…' if len(ramp) > 4 else ''} {tail}")
    period = "" if prev_start_s is None else f"{t1 - prev_start_s:6.2f}"
    return (f"  {t1:6.2f} {t2:6.2f} {t2 - t1:6.2f}   {period:>6}   " + " | ".join(parts))


# ---------------- 模式 ----------------

def resolve_sessions(args) -> list[Session]:
    if args.session:
        p = Path(args.session)
        if not p.is_dir():
            p = Path(args.demos) / args.session
        if not p.is_dir():
            raise SystemExit(f"找不到会话目录：{args.session}")
        return [load_session(p)]
    root = Path(args.demos)
    if not root.is_dir():
        raise SystemExit(f"录制根目录不存在：{root}（用 --demos 指定）")
    cands = sorted((d for d in root.iterdir() if d.is_dir() and (d / "hud.jsonl").is_file()),
                   key=lambda d: d.name, reverse=True)
    if not cands:
        raise SystemExit(f"{root} 下没有带 hud.jsonl 的会话——先跑一轮录制模式")
    if args.last and args.last > 0:
        cands = cands[:args.last]
    return [load_session(d) for d in reversed(cands)]  # 按时间正序打印


def cmd_check(args) -> None:
    sessions = resolve_sessions(args)
    print("=== 会话自检 ===")
    print(f"{'会话':22s} {'阶段':7s} {'帧':>5} {'HUD':>4} {'丢帧/丢样':>9} {'精确对齐':>8} "
          f"{'邻近对齐':>8} {'无帧':>4} {'定值占比(卡片)':>14} {'公式':>9} {'违例':>5}")
    detail = []
    for s in sessions:
        f = annotate(s)
        hm = s.hud_meta
        hdr = (f"{s.name:22s} {str(s.meta.get('phase')) + '/r' + str(s.meta.get('round_no')):7s} "
               f"{len(s.frames):>5} {len(s.rows):>4} "
               f"{str(s.meta.get('frames_dropped')) + '/' + str(hm.get('rows_dropped')):>9} "
               f"{f.n_exact:>8} {f.n_near:>8} {f.n_no_frame:>4} ")
        cnt = []
        for n in CARD_FIELDS:
            t = sum(1 for r in s.rows if r.trusted(n))
            st = sum(1 for i, ok in f.settle[n].items() if ok)
            cnt.append(f"{n[:3]}:{st}/{t}")
        print(hdr + f"{' '.join(cnt):>14} {f.formula_ok:>4}/{len(f.formula_bad) + f.formula_ok:<4} "
              f"{len(f.formula_bad):>5}")
        detail.append((s, f))

    for s, f in detail:
        hm, meta = s.hud_meta, s.meta
        print(f"\n--- {s.name} ---")
        print(f"  区域指纹 {hm.get('regions')}  采样间隔 {hm.get('sample_interval_s')}s  "
              f"停止原因 {hm.get('stop_reason')}")
        recorded = any(r.f(n).get("settled") is not None
                       for r in s.rows for n in CARD_FIELDS)
        print(f"  定值判据来源：{'落盘 settled 位（生产侧所判，权威）' if recorded else '按同一规则重算（旧格式无此位）'}"
              f"  阈值 {hm.get('settle_gap_s') or SETTLE_GAP_S}s")
        srcs = sorted({str(r.f(n).get("side_source")) for r in s.rows for n in SIDE_FIELDS
                       if r.f(n).get("side_source")})
        print(f"  归属判据来源：{srcs or ['(旧格式无此位：SCHEMA≤2 单块绝对判据)']}")
        if f.n_side_conflict:
            print(f"  ⚠ 归属异常：两块比分面板被判成**同一方** {f.n_side_conflict}/{len(s.rows)} 行"
                  f"——物理上不可能（一块蓝一块红），该行归属按 `?` 处理、不进序列检验。"
                  f"成因是单块绝对判据在公共偏色下失效（白天蓝天背景），SCHEMA 3 起改为成对判")
        mism = []
        for k, got, want in (("frames", len(s.frames), meta.get("frames_written")),
                             ("hud", len(s.rows), hm.get("rows_written")),
                             ("pads", s.pad_samples, meta.get("pad_samples"))):
            if want is not None and got != want:
                mism.append(f"{k}: 文件 {got} ≠ meta {want}")
        print(f"  计数自洽：{'无偏差' if not mism else '；'.join(mism)}")
        errs = [f"{k}={hm.get(k)}" for k in ("samples_no_frame", "ocr_no_result", "read_errors",
                                             "write_errors", "ocr_errors")
                if hm.get(k)]
        print(f"  误差计数：{'全 0' if not errs else '；'.join(errs)}")
        for n in (*CARD_FIELDS, *SIDE_FIELDS):
            t = sum(1 for r in s.rows if r.trusted(n))
            st = sum(1 for i, ok in f.settle[n].items() if ok)
            print(f"    {n:12s} 可信 {t:3d}/{len(s.rows)}  其中定值 {st:3d}")
        ramp = [r for r in s.rows if any(tg.startswith("ramp:") for tg in r.tags)]
        print(f"  爬升行（可信但非定值）{len(ramp)} 行：seq "
              f"{[r.seq for r in ramp][:24]}{'…' if len(ramp) > 24 else ''}")
        print(f"  公式（合计=里程+{OVERTAKE_WEIGHT}×超车，仅定值点）通过 {f.formula_ok}，"
              f"违例 {len(f.formula_bad)}"
              + (f"：{[(r.seq, mi, ov, to) for r, mi, ov, to in f.formula_bad[:8]]}"
                 if f.formula_bad else ""))
        print(f"  比分回落（假读嫌疑，掉位数）{len(f.nonmono)} 处："
              + ("；".join(f"seq{r.seq} {k} {a}→{b}（前值在 seq{p}）"
                           for r, k, a, b, p in f.nonmono[:8]) if f.nonmono else "无"))
        print(f"  计时回跳（倒计时应不增）{len(f.timer_bad)} 处："
              + ("；".join(f"seq{r.seq} {a}→{b}" for r, a, b in f.timer_bad[:8])
                 if f.timer_bad else "无"))
        print(f"  对齐：精确 {f.n_exact}/{len(s.rows)}  邻近(≤{PAIR_TOL_NS / 1e6:.0f}ms) "
              f"{f.n_near}  无帧 {f.n_no_frame}  邻近最大间隔 {f.gap_max_ms:.1f}ms")
        flagged = [r for r in s.rows if r.flags]
        print(f"  行内 flags {len(flagged)} 行："
              + ("；".join(f"seq{r.seq}{r.flags}" for r in flagged[:8]) if flagged else "无"))
        print(f"  在场窗口 {len(f.bursts)} 个：起点(s) "
              f"{[round(s.rows[b[0]].t, 1) for b in f.bursts]}")


def _norm_side(r: Row, name: str) -> str:
    side = r.side(name)
    return "本机" if side.startswith("本机") else ("对手" if side.startswith("对手") else "?")


def side_conflict(r: Row) -> bool:
    """两块比分面板被判成**同一方**——物理上不可能（一块蓝＝本机、一块红＝对手）。

    这不是理论情况：SCHEMA ≤ 2 的**单块绝对判据**在强公共偏色下就会这样——白天蓝天背景把
    半透明面板的两块条带都染蓝，于是两块都判成"本机"（实测一场 62/69 行）。生产侧自 SCHEMA 3
    起改为**成对判**（`pair_sides`：按两块之差定归属）修掉了它，但**旧会话仍在**，而且
    "两块同判"任何时候都值得当作数据质量事件看一眼——故本工具一律把它认成"归属不可用"，
    否则两块的值会被混进同一条序列，产出成片的假"回落"（实测 51 处）。
    """
    a, b = (_norm_side(r, n) for n in SIDE_FIELDS)
    return a != "?" and a == b


def side_of(r: Row, name: str) -> str:
    """归一后的归属（本机/对手/``?``）；整行冲突时一律 ``?``（见 ``side_conflict``）。"""
    return "?" if side_conflict(r) else _norm_side(r, name)


def _self_rate(r: Row):
    """**本机**的得分速度：速度格与比分格同属一块面板，谁在上谁在下随互换一起变。

    为什么必须按归属取而不是按位置：**「得分速度」只为我方显示**（实测本机侧 100% 有读数，
    对手侧只有零星几处——那是空区的假读）。按槽位取 `rate_a`，在面板互换后就会去读对手
    那一块的空区，得到"空"或一个假读小数字（实测 2/1/0 那一类）。
    """
    return _side_rate(r, "本机")


def _side_rate(r: Row, side: str):
    """指定归属那一块的得分速度读数（无该归属时 None）。

    注意：**只有本机那一块真的有这个读数**，故 ``side="对手"`` 拿到的是空区假读——除诊断外
    不要用它（见 `_self_rate`）。
    """
    if side_of(r, "score_a") == side:
        return r.val("rate_a") if r.trusted("rate_a") else None
    if side_of(r, "score_b") == side:
        return r.val("rate_b") if r.trusted("rate_b") else None
    return None


def side_series(sess: Session, name: str) -> list[tuple[str, float, int, int]]:
    """某比分格的 `(归属, 秒, 值, seq)` 序列，**剔除两类假读**。

    比分在阶段内累计递增，于是假读只有两种形态，都能从序列自身认出来：

    - **掉位数**：值低于已见最高值（实测 `39`、`458`）→ 用"跑动最大值"过滤；
    - **串位尖峰**：值高于前后可信点、且超出量级（实测 `18804`，前后是 794 / 814）→
      用"局部尖峰且 ≥ 3 倍"过滤。**倍数取得大**是为了不误杀真实突跳：真实事件跳分后
      序列继续上行（如 2506 → 3097 → 3139），而假尖峰的下一拍会掉回来。

    两类都是**行内 `trusted=True` 的读数**——靠单行标志拦不住，只能靠序列不变量。
    """
    raw: list[tuple[str, float, int, int]] = []
    for r in sess.rows:
        if not r.trusted(name):
            continue
        side = side_of(r, name)
        v = r.val(name)
        if side == "?" or v is None:
            continue
        raw.append((side, r.t, v, r.seq))

    spike: set[int] = set()
    for i in range(1, len(raw) - 1):
        side, _t, v, _s = raw[i]
        pv = raw[i - 1][2] if raw[i - 1][0] == side else None
        nv = raw[i + 1][2] if raw[i + 1][0] == side else None
        if pv is not None and nv is not None and v > pv and v > nv \
                and v >= SPIKE_FACTOR * max(pv, nv):
            spike.add(i)

    out: list[tuple[str, float, int, int]] = []
    best: dict[str, int] = {}
    for i, (side, t, v, s) in enumerate(raw):
        if i in spike:
            continue
        if side in best and v < best[side]:
            continue
        best[side] = v
        out.append((side, t, v, s))
    return out


def cmd_timeline(args) -> None:
    sessions = resolve_sessions(args)
    for s in sessions:
        f = annotate(s)
        print(f"\n=== {s.name} 时间线（阶段 {s.meta.get('phase')} 轮 {s.meta.get('round_no')}）===")
        print("卡片三格：值 + 定/爬；比分按**归一后**的敌我列（面板上下互换不影响本表）")
        print(f"{'seq':>4} {'t(s)':>6} {'计时':>6} {'里程':>9} {'超车':>6} {'合计':>9} "
              f"{'比分本机':>9} {'比分对手':>9} {'速度':>6}  事件 / 标签")
        for i, r in enumerate(s.rows):
            cell = {}
            for n in CARD_FIELDS:
                if not r.trusted(n):
                    cell[n] = "—"
                else:
                    cell[n] = f"{r.val(n)}{'定' if f.settle[n].get(i) else '爬'}"
            sc = {}
            for n in SIDE_FIELDS:
                sc[side_of(r, n)] = r.val(n) if r.trusted(n) else None
            if side_conflict(r):
                # 两块同判 → 归属不可用：按**位置**显示（上/下）而不是按敌我，免得读者
                # 以为这行有归属；值本身不丢（原始行里都在）
                cell_self = f"a槽{r.val('score_a') if r.trusted('score_a') else '—'}"
                cell_opp = f"b槽{r.val('score_b') if r.trusted('score_b') else '—'}"
            else:
                cell_self, cell_opp = str(sc.get("本机") or "—"), str(sc.get("对手") or "—")
            rate = _self_rate(r)
            ev = r.text("event_banner").strip()
            extra = " ".join(r.tags)
            print(f"{r.seq:>4} {r.t:>6.2f} {str(r.val('timer') if r.trusted('timer') else '—'):>6} "
                  f"{cell['mileage']:>9} {cell['overtake']:>6} {cell['total_left']:>9} "
                  f"{cell_self:>9} {cell_opp:>9} "
                  f"{str(rate if rate is not None else '—'):>6}  {ev} {extra}")

        # 增量归因：右侧比分**连续跳动**，"定值"对它无意义（判据只服务周期性卡片），
        # 故这里只要求可信 + 同归属 + 相邻；回落样本按假读嫌疑跳过并显式列出。
        print("\n  增量归因（比分：相邻可信样本，Δ/Δt 供与画面「得分速度」读数对照）：")
        for n in SIDE_FIELDS:
            pts = [(i, side_of(s.rows[i], n), s.rows[i]) for i in range(len(s.rows))
                   if s.rows[i].trusted(n) and side_of(s.rows[i], n) != "?"]
            shown, skipped = 0, []
            for (i1, sd1, r1), (i2, sd2, r2) in zip(pts, pts[1:]):
                dt = r2.t - r1.t
                if sd1 != sd2 or dt <= 0 or dt > SETTLE_GAP_S:
                    continue
                d = r2.val(n) - r1.val(n)
                if d < 0:
                    skipped.append(f"seq{r2.seq}({r1.val(n)}→{r2.val(n)})")
                    continue
                if shown < 12:
                    rt = _side_rate(r2, sd1) if sd1 == "本机" else None
                    print(f"    {sd1} {r1.t:6.2f}→{r2.t:6.2f}s  {r1.val(n)}→{r2.val(n)}  "
                          f"Δ={d:+5d}  Δ/Δt={d / dt:7.1f}/s"
                          + (f"  画面速度 {rt}" if rt is not None else ""))
                shown += 1
            if shown > 12:
                print(f"    …（共 {shown} 段，只列前 12）")
            if shown == 0:
                print(f"    {n}: 无可用的相邻同归属可信点")
            if skipped:
                print(f"    跳过（值回落 = 假读嫌疑，已计入自检）：{'，'.join(skipped)}")
        print("\n  阶段均速（首个 → 末个**已剔假读**的可信点，含事件突跳；画面「得分速度」是另一回事）：")
        for name in SIDE_FIELDS:
            for side in ("本机", "对手"):
                q = [p for p in side_series(s, name) if p[0] == side]
                if len(q) < 2 or q[-1][1] <= q[0][1]:
                    continue
                d, dt = q[-1][2] - q[0][2], q[-1][1] - q[0][1]
                print(f"    {side} {q[0][2]:>6}@{q[0][1]:6.2f}s → {q[-1][2]:>6}@{q[-1][1]:6.2f}s  "
                      f"均 {d / dt:6.1f}/秒（n={len(q)}）")

        if args.csv:
            dest = Path(args.csv)
            rows = ["seq,t,frame_id,field,text,value,trusted,side,tags"]
            for r in s.rows:
                for n in sorted(r.fields):
                    fd = r.f(n)
                    rows.append(",".join([
                        str(r.seq), f"{r.t:.3f}", str(r.frame_id), n,
                        '"' + str(fd.get("text") or "").replace('"', "'") + '"',
                        str(fd.get("value")), str(bool(fd.get("trusted"))),
                        str(fd.get("side") or ""), " ".join(r.tags)]))
            dest.write_text("\n".join(rows) + "\n", encoding="utf-8")
            print(f"\n  已写 CSV：{dest}")


def cmd_windows(args) -> None:
    sessions = resolve_sessions(args)
    all_periods: list[float] = []
    all_dur: list[float] = []
    all_settled: list[tuple[str, int, int, int]] = []
    for s in sessions:
        print(f"\n=== {s.name} 在场窗口（切分依据：{args.split}）===")
        if args.split == "score":
            print("  窗口由**比分面板**在场行切分（比分面板比卡片出现得早、缺席得晚）")
            split = [i for i, r in enumerate(s.rows)
                     if any(r.trusted(n) for n in SIDE_FIELDS)]
        else:
            split = [i for i, r in enumerate(s.rows) if r.present()]
        bursts: list[list[int]] = []
        for i in split:
            if bursts and s.rows[i].ts_ns - s.rows[bursts[-1][-1]].ts_ns <= BURST_GAP_S * 1e9:
                bursts[-1].append(i)
            else:
                bursts.append([i])
        print(f"  {'起(s)':>6} {'止(s)':>6} {'时长':>6} {'周期':>7}   窗内各格「爬升 → 定值」")
        prev = None
        for b in bursts:
            print(burst_line(s.rows, b, prev))
            if prev is not None:
                all_periods.append(s.rows[b[0]].t - prev)
            all_dur.append(s.rows[b[-1]].t - s.rows[b[0]].t)
            prev = s.rows[b[0]].t
            settled = {}
            for n in CARD_FIELDS:
                rs = runs_of(s.rows, b, n)
                settled[n] = rs[-1][0] if rs else None
            if all(settled[n] is not None for n in CARD_FIELDS):
                all_settled.append((s.name, settled["mileage"], settled["overtake"],
                                    settled["total_left"]))
        # 窗内稳定值是否处处满足公式 —— 这是「里程=窗口里程」与换算系数的一次联合复核
        bad = [(nm, m, o, t) for nm, m, o, t in all_settled if t != m + OVERTAKE_WEIGHT * o]
        print(f"  窗口定值点公式复核：{len(all_settled) - len(bad)}/{len(all_settled)} 通过"
              + (f"，违例 {bad[:6]}" if bad else ""))
    if all_periods:
        ps = sorted(all_periods)
        ds = sorted(all_dur)
        print(f"\n=== 汇总（{len(sessions)} 个会话）===")
        print(f"  窗口周期(s)：n={len(ps)} 中位 {ps[len(ps) // 2]:.2f}  "
              f"范围 {ps[0]:.2f}~{ps[-1]:.2f}")
        print(f"  窗口时长(s)：n={len(ds)} 中位 {ds[len(ds) // 2]:.2f}  "
              f"范围 {ds[0]:.2f}~{ds[-1]:.2f}")
        vals = sorted({m for _, m, _, _ in all_settled})
        print(f"  窗内里程定值：{vals}（若各窗接近同一量级 → 里程是**该窗口**的量，不是跨窗累计）")


def _font(size: int):
    from PIL import ImageFont
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _zoom_crop(img, box, zoom: float):
    w, h = img.size
    x1, y1, x2, y2 = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
    c = img.crop((x1, y1, x2, y2))
    return c.resize((int(c.width * zoom), int(c.height * zoom)))


def _label(r: Row, f: Findings, i: int) -> tuple[str, str]:
    card = "  ".join(
        f"{n}={r.val(n)}{'定' if f.settle[n].get(i) else '爬'}" if r.trusted(n) else f"{n}=—"
        for n in CARD_FIELDS)
    sc = "  ".join(f"{side_of(r, n)}:{r.val(n) if r.trusted(n) else '—'}"
                   for n in SIDE_FIELDS)
    ev = r.text("event_banner").strip() or "—"
    l1 = (f"#{r.seq}  t={r.t:6.2f}s  帧 {r.frame_id}   {card}   {sc}   "
          f"速度(本机) {_self_rate(r) if _self_rate(r) is not None else '—'}   {ev}")
    l2 = ("  判定：" + ("；".join(r.tags) if r.tags else "无标签（全部定值、无违例）")
          + (f"   [该行 side={r.side('score_a')}/{r.side('score_b')}]"
             if any(side_of(r, n) == "?" for n in SIDE_FIELDS) else ""))
    return l1, l2


def _card(sess: Session, i: int, f: Findings):
    from PIL import Image, ImageDraw
    r = sess.rows[i]
    by_ts = sess.frame_index()
    fr = by_ts.get(r.ts_ns)
    note = ""
    if fr is None:
        nf, gap = sess.nearest_frame(r.ts_ns)
        if nf is None or gap > PAIR_TOL_NS:
            return None
        fr, note = nf, f"（非同帧：最近帧 Δ{gap / 1e6:.0f}ms）"
    src = sess.path / "frames" / fr["file"]
    if not src.is_file():
        return None
    img = Image.open(src).convert("RGB")
    thumb = img.resize((THUMB_W, round(THUMB_W * img.height / img.width)), Image.LANCZOS)
    left = _zoom_crop(img, PANEL_BOX, ZOOM)
    right = _zoom_crop(img, SCORE_BOX, ZOOM)
    body = max(thumb.height, left.height, right.height)
    card = Image.new("RGB", (CELL_W, body + BAR_H), (18, 18, 18))
    card.paste(thumb, (8, (body - thumb.height) // 2))
    x = 8 + THUMB_W + 10
    card.paste(left, (x, (body - left.height) // 2))
    card.paste(right, (x + left.width + 10, (body - right.height) // 2))
    d = ImageDraw.Draw(card)
    l1, l2 = _label(r, f, i)
    d.text((8, body + 3), l1 + note, font=_font(15), fill=(255, 226, 120))
    d.text((8, body + 24), l2, font=_font(14), fill=(150, 205, 255))
    return card


def interest_rows(sess: Session, f: Findings, limit: int) -> list[int]:
    """挑行：先违例与爬升（判读的争议点），再窗口边界，最后定值点。按时间序。"""
    picks: list[int] = []

    def add(i: int) -> None:
        if i not in picks:
            picks.append(i)

    for i, r in enumerate(sess.rows):
        if any(t.startswith(("mismatch:", "nonmono:", "timer:")) for t in r.tags):
            add(i)
    for i, r in enumerate(sess.rows):
        if any(t.startswith("ramp:") for t in r.tags):
            add(i)
    for b in f.bursts:                      # 每个窗口取首行与首个定值行
        add(b[0])
        for i in b:
            if all(f.settle[n].get(i, False) for n in CARD_FIELDS if sess.rows[i].trusted(n)):
                add(i)
                break
    for i, r in enumerate(sess.rows):       # 兜底：任何定值行
        if r.present() and any(f.settle[n].get(i, False) for n in CARD_FIELDS):
            add(i)
    picks.sort()
    if limit and len(picks) > limit:
        step = len(picks) / limit
        picks = [picks[int(k * step)] for k in range(limit)]
    return picks


def cmd_sheet(args) -> None:
    sessions = resolve_sessions(args)
    out_root = Path(args.out) if args.out else Path(args.demos).parent / "hud_review"
    from PIL import Image
    for s in sessions:
        f = annotate(s)
        picks = interest_rows(s, f, args.limit)
        pairs = [(i, _card(s, i, f)) for i in picks]
        pairs = [(i, c) for i, c in pairs if c is not None]
        if not pairs:
            print(f"{s.name}: 没有可出图的行（缺帧或参与行太少）")
            continue
        out_dir = out_root / s.name
        out_dir.mkdir(parents=True, exist_ok=True)
        per = SHEET_COLS * SHEET_ROWS
        for k in range(0, len(pairs), per):
            chunk = pairs[k:k + per]
            cw = max(c.width for _, c in chunk)
            ch = max(c.height for _, c in chunk)
            sheet = Image.new("RGB", (cw * SHEET_COLS, ch * SHEET_ROWS), (8, 8, 8))
            for n, (_i, c) in enumerate(chunk):
                sheet.paste(c, ((n % SHEET_COLS) * cw, (n // SHEET_COLS) * ch))
            dest = out_dir / f"sheet{k // per + 1}.png"
            sheet.save(dest)
            print(f"{s.name}: {dest}  （行 seq {[s.rows[i].seq for i, _c in chunk]}）")
        print(f"  出图行数 {len(pairs)}/{len(s.rows)}；选行口径见 interest_rows（违例 → 爬升 → "
              f"窗口边界 → 定值）")


def main() -> None:
    ap = argparse.ArgumentParser(description="speedrush 录制复盘（读 hud.jsonl，不跑 OCR）")
    ap.add_argument("mode", choices=["check", "timeline", "windows", "sheet"])
    ap.add_argument("--demos", default=str(DEFAULT_DEMOS), help="录制会话根目录")
    ap.add_argument("--session", default=None, help="会话目录名（或绝对路径）")
    ap.add_argument("--last", type=int, default=0, help="只取最近 N 个会话（0 = 全部）")
    ap.add_argument("--split", choices=["card", "score"], default="card",
                    help="windows：按左侧卡片在场切分（默认）或按比分面板在场切分")
    ap.add_argument("--limit", type=int, default=12, help="sheet 最多出多少行")
    ap.add_argument("--out", default=None, help="sheet 输出根目录（默认 demos 同级 hud_review）")
    ap.add_argument("--csv", default=None, help="timeline 追加写出的 CSV 路径")
    args = ap.parse_args()
    {"check": cmd_check, "timeline": cmd_timeline, "windows": cmd_windows,
     "sheet": cmd_sheet}[args.mode](args)


if __name__ == "__main__":
    main()