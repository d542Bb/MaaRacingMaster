# -*- coding: utf-8 -*-
"""极速狂飙回合战绩库：阶段结果落库、看板载荷与历史导入共用。

定位：**回合级派生结果库**，不是逐拍流水副本。字段由与
``trace/speedrush_summary.jsonl`` 相同的阶段结果函数生成；日志开启时可由
summary + HUD 重建，日志关闭时它是唯一的用户战绩留存（与鉴宝业务库同先例）。

只收自动控制局（深度几何 / 全程直行）。人工录制与宏录制/回放不写库，由
``module._record_round_phase`` 的 gate 保证；本模块不猜模式。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from maaracing_master.core.paths import data_dir

SCHEMA_VERSION = 1
MAX_LIST_ROWS = 50
# 对手末分锚选余量：HUD 比流水最多快一个采样（0.5s × 最高得分速度）。
OPPONENT_MARGIN = 600

# ---------------- 比分合理性门 ----------------
# 单步增幅上限（分/秒）。2026-11-07 用 11 份会话 1027 个步进全量回放校准：
# 真实单步增幅我方最高 ≈330/秒、对手最高 ≈910/秒（对手突进显著更快，用户亦确认
# 「对手有时很快，但不至于半秒一万」）——故两套上限分开定，对手放宽到 4 倍于我方。
# 假读是**单样本尖峰/塌坑**（下一拍回落原轨迹；首位并位虚高、掉首位塌低，双向都有），
# 折算速率我方最小 ≈700/秒、对手最小 ≈15000/秒，与真值之间留有倍数空隙。
EGO_RATE_CAP = 500.0
OPP_RATE_CAP = 2000.0
# 步进时长钳制（秒）：下限防采样抖动把真步进算短（上限变小而误拒）；上限防读数
# 长时间平台（OCR 断读/显示冻结）把允许增量抬到假读也能过。
_STEP_MIN_S = 0.3
_STEP_MAX_S = 3.0
# 立基线后连续被拒达到此数，视为**真电平跃迁**（实测一拍 +581、871/秒的真突进会被
# 上限拒绝）——以最新读数重新引导。宁可失明两拍，也不把基线焊死在旧电平。
_UNSTICK_REJECTS = 2

_CREATE_ROUNDS = """
CREATE TABLE IF NOT EXISTS rounds (
    round_key       TEXT PRIMARY KEY,
    session_token   TEXT NOT NULL DEFAULT '',
    round_no        INTEGER NOT NULL DEFAULT 1,
    round_stamp     TEXT NOT NULL,
    drive_mode      TEXT NOT NULL DEFAULT 'unknown',
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    phase1_ok       INTEGER,
    phase1_ticks    INTEGER NOT NULL DEFAULT 0,
    phase1_score    INTEGER,
    phase1_rate     REAL,
    phase1_dur_s    REAL,
    phase1_opponent INTEGER,
    phase2_ok       INTEGER,
    phase2_ticks    INTEGER NOT NULL DEFAULT 0,
    phase2_score    INTEGER,
    phase2_rate     REAL,
    phase2_dur_s    REAL,
    phase2_opponent INTEGER,
    source          TEXT NOT NULL DEFAULT 'live',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
)
"""
_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_rounds_started "
    "ON rounds(started_at DESC)"
)

_PHASE_COLUMNS = (
    "ok", "ticks", "score", "rate", "dur_s", "opponent",
)


class StoreError(RuntimeError):
    """战绩库不可用（版本过新 / 无法打开 / 写入失败）。"""


@dataclass(frozen=True)
class PhaseStats:
    """一个驾驶阶段的汇总（与 summary 行同源）。"""

    ticks: int
    score: int | None
    rate: float | None
    dur_s: float | None
    conserve_segments: int = 0
    # 合理性门在本阶段拒掉的比分读数条数（诊断面；0=无假读或旧口径）
    gate_rejected: int = 0


class ScoreGate:
    """比分序列的单步合理性门：把「跳变不合理」的读数挡在战绩口径之外。

    判据三条，全部来自回合内比分的物理性质（回合内累计、只增不减、增速有上界）
    与全量回放校准（见 ``EGO_RATE_CAP`` 注）：

    - **只增不减**：读数低于基线即拒、基线不动——实测 12 处下降步进全部是假读
      （掉首位或敌我串位），下一拍真值自然接上（回落步进相对基线变回小增量）；
    - **增幅有上界**：增量超过 ``rate_cap × 步进时长`` 即拒，单样本尖峰都在此列；
      零增量（平台）采纳但**不推进基线时刻**——平台越久允许的下一跳越大，但被
      ``_STEP_MAX_S`` 钳住；
    - **引导 + 解卡**：头两拍互证（第二拍落在第一拍的合法增量内）才立基线，防
      首拍即假读把基线带歪；互证失败以最新读数滚动重引导。立基线后连续
      ``_UNSTICK_REJECTS`` 拍被拒视为真电平跃迁，同样重新引导。

    去重分工：同一 HUD 样本会被逐拍流水重复携带——读数对侧用样本时刻去重（本门
    ``feed`` 内建），阶段汇总侧的行时刻随控制拍前进、须由调用方按「值不变即同一
    样本」去重后再喂。被拒读数只在本门计数：原始值留在 hud.jsonl 与 trace 行里
    （证据层忠实留痕），本门只裁决「什么值有资格代表这一阶段的比分」。
    """

    __slots__ = ("_cap", "_base", "_cand", "_reject_run",
                 "_last_ts", "_last_value", "_last_ok", "rejected")

    def __init__(self, rate_cap: float = EGO_RATE_CAP) -> None:
        self._cap = float(rate_cap)
        self._base: tuple[int, int] | None = None   # (值, 首见 ts_ns)
        self._cand: tuple[int, int] | None = None   # 引导候选
        self._reject_run = 0
        self._last_ts = 0
        self._last_value: int | None = None
        self._last_ok = False
        self.rejected = 0

    def _allow(self, dt_s: float) -> float:
        step = min(max(dt_s, _STEP_MIN_S), _STEP_MAX_S)
        return self._cap * step

    def feed(self, ts_ns: int, value: int) -> bool:
        """喂入一次读数；返回是否采纳（采纳即基线前移到该值）。

        同一时刻同一值的重复喂入（逐拍流水携带同一 HUD 样本）按缓存判定返回，
        不重复计数、不重复推动状态。
        """
        ts_ns = int(ts_ns)
        value = int(value)
        if ts_ns == self._last_ts and value == self._last_value:
            return self._last_ok
        ok = self._judge(ts_ns, value)
        self._last_ts = ts_ns
        self._last_value = value
        self._last_ok = ok
        return ok

    def _judge(self, ts_ns: int, value: int) -> bool:
        if self._base is None:
            if self._cand is None:
                self._cand = (value, ts_ns)
                return False
            cand_v, cand_ts = self._cand
            dt = (ts_ns - cand_ts) / 1e9
            if value >= cand_v and value - cand_v <= self._allow(dt):
                self._base = (value, ts_ns)
                self._cand = None
                self._reject_run = 0
                return True
            # 互证失败：以最新读数滚动重引导（首拍假高/假低都在下一拍被纠正）
            self._cand = (value, ts_ns)
            self.rejected += 1
            return False
        base_v, base_ts = self._base
        dt = (ts_ns - base_ts) / 1e9
        if value >= base_v and value - base_v <= self._allow(dt):
            if value != base_v:
                self._base = (value, ts_ns)   # 零增量不推进时刻：平台不放大限额
            self._reject_run = 0
            return True
        self.rejected += 1
        self._reject_run += 1
        if self._reject_run >= _UNSTICK_REJECTS:
            self._base = None
            self._cand = (value, ts_ns)
            self._reject_run = 0
        return False

    @property
    def accepted(self) -> int | None:
        """当前有资格代表阶段比分的值：已立基线取基线；否则取引导候选
        （超短阶段整场只出现一拍时，退化回旧的「读到即认」口径）。"""
        if self._base is not None:
            return self._base[0]
        return self._cand[0] if self._cand is not None else None


def new_pair_gates() -> tuple[ScoreGate, ScoreGate]:
    """读数对（我方, 对手）用的两道门：我方严、对手宽，参数见常量注。"""
    return ScoreGate(EGO_RATE_CAP), ScoreGate(OPP_RATE_CAP)


def db_path() -> Path:
    """战绩库路径；函数而非常量，测试可 monkeypatch 到 tmp_path。"""
    return data_dir() / "speedrush" / "speedrush.db"


def summarize_trace_rows(rows: Sequence[Mapping[str, Any]]) -> PhaseStats:
    """从逐拍行算阶段汇总；口径与 ``module._write_game_summary`` 逐字一致。

    末分**不是我方读数的最后一条非空值**——那是「读到即认」，一拍 OCR 假读
    （实测 214,760）恰好落在最后一拍就会定稿入库。这里沿时间轴过
    ``ScoreGate``（我方上限）：行值不变视为同一 HUD 样本不重复喂，假读被拒后
    末分保持最近一次被采纳的值；拒了几个只进 ``gate_rejected`` 诊断面，原始
    序列照旧留在 trace 行里。
    """
    gate = ScoreGate(EGO_RATE_CAP)
    rate: float | None = None
    conserve = 0
    cons_segments = 0
    prev_value: int | None = None
    for row in rows:
        if row.get("state") == "CONSERVE":
            if conserve == 0:
                cons_segments += 1
            conserve += 1
        else:
            conserve = 0
        value = row.get("hud_score")
        if value is not None:
            value = int(value)
            if value != prev_value:
                ts = row.get("ts_ns")
                if ts is not None:
                    gate.feed(int(ts), value)
            prev_value = value
        if row.get("hud_rate") is not None:
            rate = row["hud_rate"]
    dur_s = None
    if rows and rows[0].get("ts_ns") is not None \
            and rows[-1].get("ts_ns") is not None:
        dur_s = round((rows[-1]["ts_ns"] - rows[0]["ts_ns"]) / 1e9, 1)
    return PhaseStats(len(rows), gate.accepted, rate, dur_s, cons_segments,
                      gate.rejected)


def pick_opponent(pairs: Sequence[tuple[int, int]],
                  ego_final: int | None,
                  margin: int = OPPONENT_MARGIN) -> int | None:
    """以汇总末分为锚选对手末分：取「我方分 ≤ 末分 + 余量」里最大的一对。

    实测假读有两种：无速度行的串位（283 / 22840）、有速度行但虚高回落
    （4878 / 888）。前者在 ``hud.read_hud_scores`` 弃权；后者超出余量，落选。
    传入的读数对**应当已过 ``ScoreGate``**（读数对侧的假读在采集/导入处被拒），
    本函数的余量是锚选面的第二道防线，不重复承担序列判伪。
    """
    if ego_final is None or not pairs:
        return None
    eligible = [pair for pair in pairs if pair[0] <= ego_final + margin]
    if not eligible:
        return None
    return int(max(eligible, key=lambda pair: pair[0])[1])


def _connect(write: bool) -> sqlite3.Connection | None:
    path = db_path()
    if not write and not path.is_file():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=3.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
    else:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        conn.execute("PRAGMA busy_timeout=1000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise StoreError(f"战绩库版本过新: {version}")
    conn.execute(_CREATE_ROUNDS)
    conn.execute(_CREATE_INDEX)
    if version == 0:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _round_key(session_token: str, round_stamp: str) -> str:
    return f"{session_token or ''}::{round_stamp}"


def write_phase(
    *,
    session_token: str,
    round_stamp: str,
    round_no: int,
    drive_mode: str,
    phase: int,
    stats: PhaseStats,
    completed: bool,
    opponent: int | None,
    started_at: str,
    ended_at: str,
    source: str = "live",
) -> str | None:
    """阶段结果 upsert；成功返回 None，失败返回错误字符串。

    阶段 1/2 各写自己的列；同一 ``round_key`` 再次写入只更新本阶段，
    行数与另一阶段不受影响。``source='import'`` 不覆盖 ``source='live'``。
    """
    if phase not in (1, 2):
        return f"非法阶段: {phase!r}"
    key = _round_key(session_token, round_stamp)
    now = _now_iso()
    phase_cols = [f"phase{phase}_{name}" for name in _PHASE_COLUMNS]
    insert_cols = [
        "round_key", "session_token", "round_no", "round_stamp", "drive_mode",
        "started_at", "ended_at", *phase_cols, "source", "created_at", "updated_at",
    ]
    values = [
        key, session_token or "", round_no, round_stamp, drive_mode or "unknown",
        started_at, ended_at,
        1 if completed else 0,
        stats.ticks,
        stats.score,
        stats.rate,
        stats.dur_s,
        opponent,
        source, now, now,
    ]
    assignments = ", ".join(f"{col} = excluded.{col}" for col in phase_cols)
    meta_updates = (
        "session_token = CASE WHEN rounds.session_token = '' "
        "THEN excluded.session_token ELSE rounds.session_token END, "
        "round_no = CASE WHEN rounds.round_no <= 0 "
        "THEN excluded.round_no ELSE rounds.round_no END, "
        "drive_mode = CASE WHEN rounds.drive_mode = 'unknown' "
        "THEN excluded.drive_mode ELSE rounds.drive_mode END, "
        "started_at = CASE WHEN excluded.started_at < rounds.started_at "
        "THEN excluded.started_at ELSE rounds.started_at END, "
        "ended_at = CASE WHEN rounds.ended_at IS NULL "
        "OR excluded.ended_at > rounds.ended_at "
        "THEN excluded.ended_at ELSE rounds.ended_at END"
    )
    source_update = "source = 'live'" if source == "live" else "source = 'import'"
    where = "" if source == "live" else " WHERE rounds.source != 'live'"
    sql = (
        f"INSERT INTO rounds ({', '.join(insert_cols)}) "
        f"VALUES ({', '.join('?' for _ in values)}) "
        f"ON CONFLICT(round_key) DO UPDATE SET "
        f"{assignments}, {meta_updates}, {source_update}, "
        "updated_at = excluded.updated_at" + where
    )
    try:
        conn = _connect(write=True)
        assert conn is not None
        try:
            _ensure_schema(conn)
            conn.execute(sql, values)
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, StoreError) as exc:
        return f"{exc}"
    return None


def existing_source(round_stamp: str, session_token: str = "") -> str | None:
    """导入前查重：返回已有行来源（live/import）或 None。"""
    key = _round_key(session_token, round_stamp)
    try:
        conn = _connect(write=False)
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT source FROM rounds WHERE round_key = ?", (key,)
            ).fetchone()
            return row["source"] if row is not None else None
        finally:
            conn.close()
    except (sqlite3.Error, OSError, StoreError):
        return None


def _fetch_rows() -> list[dict[str, Any]]:
    try:
        conn = _connect(write=False)
        if conn is None:
            return []
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(f"战绩库版本过新: {version}")
            rows = conn.execute(
                "SELECT * FROM rounds "
                "ORDER BY started_at DESC, round_key DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, StoreError):
        raise


def _started_dt(row: Mapping[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row.get("started_at") or ""))
    except ValueError:
        return None


def _final_scores(row: Mapping[str, Any]) -> tuple[int | None, int | None, bool]:
    """(我方末分, 对手末分, 是否终盘)。只有阶段 2 正常结束才是终盘。"""
    if row.get("phase2_ok") is not None:
        return (row.get("phase2_score"), row.get("phase2_opponent"),
                row.get("phase2_ok") == 1)
    return row.get("phase1_score"), row.get("phase1_opponent"), False


def _result(row: Mapping[str, Any]) -> int | None:
    ego, opp, final = _final_scores(row)
    if not final or ego is None or opp is None:
        return None
    if ego > opp:
        return 1
    if ego < opp:
        return -1
    return 0


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _row_payload(row: Mapping[str, Any], today: date,
                 labels: Mapping[str, str]) -> dict[str, Any]:
    dt = _started_dt(row)
    group = None
    leading = ""
    if dt is not None:
        group = "今日" if dt.date() == today else dt.strftime("%m-%d")
        leading = dt.strftime("%H:%M")
    mode_key = str(row.get("drive_mode") or "unknown")
    mode = labels.get(mode_key, mode_key)
    ego, opp, final = _final_scores(row)
    entry: dict[str, Any] = {"leading": leading}
    if group:
        entry["group"] = group

    if final:
        if ego is None:
            entry["text"] = f"{mode} · 无得分"
        elif opp is None:
            entry["text"] = f"{mode} · 对手分未记录"
        else:
            entry["parts"] = [
                {"text": f"{mode} · ", "tone": "muted"},
                {"text": _fmt_int(ego), "tone": "info"},
                {"text": " / ", "tone": "muted"},
                {"text": _fmt_int(opp), "tone": "negative"},
            ]
            diff = int(ego) - int(opp)
            if diff > 0:
                entry["trailing"] = f"胜 +{diff:,}"
            elif diff < 0:
                entry["trailing"] = f"负 -{abs(diff):,}"
            else:
                entry["trailing"] = "平 0"
            return entry
        entry["trailing"] = "—"
        return entry

    if row.get("phase2_ok") is not None or row.get("phase1_ok") is not None:
        entry["badge"] = {"text": "未完成", "tone": "warn"}
        phase_label = "阶段2" if row.get("phase2_ok") is not None else "阶段1"
        if ego is None:
            entry["text"] = f"{mode} · 无得分"
            entry["trailing"] = "—"
        else:
            entry["text"] = f"{mode} · {phase_label}"
            entry["trailing"] = f"{_fmt_int(ego)} 分"
        return entry

    entry["text"] = f"{mode} · 对手分未记录"
    entry["trailing"] = "—"
    return entry


def read_dashboard_payload(labels: Mapping[str, str]) -> dict[str, Any]:
    """读全表并生成看板载荷；DB 不存在/空表返回空态。

    ``labels`` 是模式短名表（深度/直行/人工/宏/—），由模块提供；
    本模块不持有业务文案。
    """
    rows = _fetch_rows()
    if not rows:
        return {
            "version": 1,
            "title": "回合战绩",
            "blocks": [{"type": "text", "text": "还没有回合记录。"}],
        }

    today = date.today()
    today_rows = [
        row for row in rows
        if (dt := _started_dt(row)) is not None and dt.date() == today
    ]
    today_results = [res for row in today_rows if (res := _result(row)) is not None]
    all_results = [res for row in rows if (res := _result(row)) is not None]
    today_wins: int | None = (
        None if today_rows and not today_results
        else sum(1 for res in today_results if res > 0)
    )
    total_wins: int | None = (
        None if not all_results else sum(1 for res in all_results if res > 0)
    )

    stats = {
        "type": "stats",
        "items": [
            {"label": "今日回合", "value": str(len(today_rows))},
            {"label": "今日胜场",
             "value": "—" if today_wins is None else str(today_wins)},
            {"label": "总回合数", "value": str(len(rows))},
            {"label": "总胜场",
             "value": "—" if total_wins is None else str(total_wins)},
        ],
    }
    list_rows = [
        _row_payload(row, today, labels)
        for row in rows[:MAX_LIST_ROWS]
    ]
    return {
        "version": 1,
        "title": "回合战绩",
        "subtitle": f"{today.isoformat()} 00:00 起",
        "blocks": [
            stats,
            {"type": "list", "page_size": 5, "rows": list_rows},
        ],
    }


def read_rows() -> list[dict[str, Any]]:
    """只读全表（导入核对与测试用）。"""
    return _fetch_rows()


__all__ = [
    "SCHEMA_VERSION",
    "StoreError",
    "PhaseStats",
    "ScoreGate",
    "EGO_RATE_CAP",
    "OPP_RATE_CAP",
    "new_pair_gates",
    "db_path",
    "summarize_trace_rows",
    "pick_opponent",
    "write_phase",
    "existing_source",
    "read_dashboard_payload",
    "read_rows",
]
