#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志模块：Logger 类 + GroupHandle + 全局 logger 实例

磁盘写入默认关闭（GUI 内存缓冲不受影响）；由设置页「日志记录」开关或
sidecar 启动时的 profile 回读启用。每次开启新建一个**会话目录**
`user_data_dir()/logs/<YYYYMMDD_HHMMSS>/`，日志与伴随产物（鉴宝决策流水
`trace.jsonl`，与本开关共用）同放其中，取证时整个目录打包即可；开发版与
发行版位置一致（%APPDATA%/MaaRacingMaster/logs）。

设计要点：
- 进程内缓冲用有界 deque（环形），长时间运行不随日志量增长。
- GUI 增量读取用单调序列号（_seq）作游标，而非行数；环形回绕后仍不重不漏。
- 落盘**单流全量**：一次开启只建一个会话目录与一个 `MaaRM_<ts>.log`，各级别按
  发生顺序写入。不按级别分档——诊断是顺序的（「失败之前发生了什么」），按级别切分
  会把时序切断且不可逆：切出的两份文件各自都读不通（DEBUG 档没有业务锚点，INFO 档
  没有诊断细节）。分级过滤留给读侧（GUI）与导出侧，写侧只保证时序完整。
- 按大小轮转 + 启动时按会话组保留清理，占用有上限。
- 写盘持句柄 + Lock 跨线程安全。

结构化记录（契约 v2 冻结于 commit 81c2eb2，实验目录退役后本 docstring 即契约正文的 home）：
- 环形缓冲存 `(seq, record)`；文本行是记录的**投影**——文件投影含全事件
  （group 开合渲染为 `::group:: 标题` / `::endgroup:: outcome`，GH Actions 风格，
  `% : 换行` 按 %25/%3A/%0A 转义）；GUI 文本投影只渲染 log 事件（过渡期前端仍靠
  措辞锚点，结构事件不得混入，保证现网字节兼容）。
- 协议版本字段统一为 `schema_version`（record 与未来 RPC 信封同名，不另设
  log_protocol_version）。
- 分组原语：`group()` 返回 GroupHandle（句柄制，**不依赖线程隐式栈**——sidecar、
  Tasker、OCR/IO worker 多线程并发下隐式栈会串组）；`with` 糖在异常时以
  failure 收尾并**原样抛出业务异常**；跨线程追加事件用 `log(..., group_id=...)`。
- 生命周期语义（锁死，勿凭猜）：
  * `handle.end()` 首次调用返回最终 outcome 字符串；重复调用幂等——返回同一
    outcome、不产生新事件、计入 `internal_counters["double_end"]`；
  * 未知/已回收 group_id 的 `log()` 降级为无组事件并计数；
  * `close()`（sidecar 停止路径，全仓唯一退出入口）为所有未收尾组补
    `group_end(outcome="incomplete")`，`_finalized` 标志保证只执行一次；
  * group_start/group_end **不受通道级别闸门约束**——结构完整性优先于流量控制，
    否则闸门会制造「无头组」；组内 log 事件照常受闸门约束，且被闸门挡下的事件
    不参与 outcome 推导。
- 通道继承：`g.log()` / `log(group_id=...)` 的 channel 缺省时继承所在组的
  channel，显式传值允许覆盖。
- fields 在进入记录**之前**完成净化（标量化、NaN/Inf→None、键值裁剪、敏感键
  丢弃），记录本身即 JSON 安全——序列化失败不可能发生在 RPC 边界。
- 一切误用只进 `internal_counters` 计数，不回灌日志（防递归）；日志路径的
  失败永不抛进业务线程。
"""

import math
import re
import secrets
import shutil
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Lock

from maaracing_master.core.paths import logs_dir


class _TierFile:
    """单档落盘句柄：持句柄 + line-buffering + 大小轮转。

    轮转：超过 MAX_BYTES 时把当前文件依次后移为 `.1/.2/...`，最多保留
    backup_count 份备份，然后重开新句柄。写失败静默（I6 语义）。
    """

    def __init__(self, path: Path, max_bytes: int, backup_count: int):
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.fh = None

    def _rotate(self) -> None:
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None
        # 从最老备份开始逐个后移：.N -> .N+1，当前主文件 -> .1
        for i in range(self.backup_count, 0, -1):
            src = self.path if i == 1 else Path(f"{self.path}.{i - 1}")
            dst = Path(f"{self.path}.{i}")
            if src.exists():
                try:
                    src.replace(dst)
                except OSError:
                    pass
        try:
            self.fh = open(str(self.path), "a", encoding="utf-8", buffering=1)
        except OSError:
            self.fh = None

    def write(self, line: str) -> None:
        if self.fh is None:
            try:
                self.fh = open(str(self.path), "a", encoding="utf-8", buffering=1)
            except OSError:
                return  # 打不开（无权限等）：静默跳过本行
        try:
            if self.fh.tell() > self.max_bytes:
                self._rotate()
            self.fh.write(line + "\n")
        except OSError:
            pass

    def close(self) -> None:
        fh, self.fh = self.fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


class Logger:
    # 日志级别：TRACE < DEBUG < INFO < WARNING < ERROR
    LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARNING": 3, "ERROR": 4}
    # GUI 只显示 INFO 及以上级别。分级判据（写日志时自问）：这行是玩家事后想回看的
    # 「发生了什么」（起停/阶段/场次结果/收益/降级/异常留痕），还是机件「怎么运转」
    # （点击意图/状态机 phase·epoch/性能快照/配置装载）？后者一律 DEBUG——
    # 落盘是单流全量，降下去不丢，排查照常用文件。异常留痕即使节流也保持 WARNING。
    GUI_MIN_LEVEL = "INFO"
    DEFAULT_CHANNEL = "app" # 通道名：core 通用用 app；业务层用模块 id（core 不校验、不外显）
    # 进程内行缓冲上限。实测峰值约 4 行/秒，按最坏情况放大 25 倍取 100 行/秒，
    # 5000 行对应 50 秒最坏积压，安全裕度充足。
    BUFFER_CAPACITY = 5000
    # 落盘参数：单文件轮转上限 / 备份份数 / 启动保留的会话组数。
    MAX_BYTES = 10 * 1024 * 1024
    BACKUP_COUNT = 2
    KEEP_SESSIONS = 5

    # ---- 结构化契约常量（DESIGN_log_api v2） ----
    SCHEMA_VERSION = 1
    # group_end 的合法终态；running 只出现在 group_start。不设 cancelled：
    # 用户停止导致的未收尾就是 incomplete。
    END_OUTCOMES = {"success", "warning", "failure", "incomplete"}
    # fields 约束：JSON 标量、键英文机器名；先净化后入记录（记录即 JSON 安全）。
    FIELDS_MAX_KEYS = 16
    FIELDS_MAX_KEY_LEN = 32
    FIELDS_MAX_STR_LEN = 200
    _SENSITIVE_KEY_RE = re.compile(
        r"(?i)password|passwd|token|secret|api_?key|cookie|authorization")
    # 已收尾组的 outcome 墓碑上限（重复 end 要能幂等回读）。
    _CLOSED_TOMBSTONES = 256

    # `(?:\.debug)?` 兼容旧版分档产物，让保留清理顺带回收历史 .debug.log。
    _SESSION_RE = re.compile(r"MaaRM_(\d{8}_\d{6})(?:\.debug)?\.log(?:\.\d+)?$")
    # 会话目录形态：logs/<YYYYMMDD_HHMMSS>/（现行落盘形态，内含 MaaRM_<ts>.log 与伴随产物）
    _SESSION_DIR_RE = re.compile(r"(\d{8}_\d{6})$")

    def __init__(self, log_dir: Path):
        self._log_dir = Path(log_dir)
        self.log_file = None          # 兼容属性：本次会话日志路径（磁盘写入启用后非 None）
        self._file_enabled = False
        # 环形缓冲：元素为 (seq, record)。deque 满时自动丢弃最旧元素。
        self._lines = deque(maxlen=self.BUFFER_CAPACITY)
        self._seq = 0                 # 每写一行 +1，只增不减；GUI 增量读取的单调游标
        self._lock = Lock()
        self._session_ts = None       # 当前会话时间戳（YYYYMMDD_HHMMSS），用于跳过一次当前会话清理
        self._session_dir = None      # 当前会话目录 logs/<ts>/；未启用写盘时为 None
        self._tier: _TierFile | None = None   # 单流写盘句柄（未启用写盘时为 None）
        self._channel_levels: dict[str, str] = {}  # 通道 → 级别（运行时调级）；查不到回落 _min_level
        self._min_level: str | None = None        # 全局最低记录级别；None = 全记录（兼容旧行为）
        # ---- 分组状态（全部在 _lock 下读写） ----
        # session_id：进程内日志流的身份；WebView 重连/新旧侧对账用。
        self._session_id = (datetime.now().strftime("%Y%m%dT%H%M%S")
                            + "-" + secrets.token_hex(2))
        self._groups: dict[str, dict] = {}       # group_id → 开组状态
        self._closed_outcomes: dict[str, str] = {}  # group_id → 终态（幂等回读墓碑）
        self._finalized = False
        self._counters: dict[str, int] = {}      # 误用计数：只增不回灌日志

    # ---------- 对外只读属性 ----------
    @property
    def file_logging(self) -> bool:
        """当前是否启用磁盘写入。"""
        return self._file_enabled

    @property
    def session_dir(self) -> Path | None:
        """当前写盘会话目录（logs/<ts>/）；未启用写盘时为 None。

        伴随产物（如鉴宝决策流水 trace.jsonl）与日志共用同一个开关、同放这个目录：
        取到 None 即表示本次不落盘。
        """
        return self._session_dir

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def internal_counters(self) -> dict:
        """误用/降级计数快照（测试与自检用；运行时误用只进这里）。"""
        with self._lock:
            return dict(self._counters)

    def _bump(self, name: str) -> None:
        """误用计数。假设持有 _lock。"""
        self._counters[name] = self._counters.get(name, 0) + 1

    # ---------- 落盘开关与会话清理 ----------
    def set_file_logging(self, enabled: bool) -> None:
        """开关磁盘写入。

        开启：惰性创建会话目录 `logs/<ts>/` 并为本次开启新建一个日志文件（单流全量，
        各级别按发生顺序写入）；关闭：立即停止写盘（已写文件保留，内存缓冲继续累积，
        GUI 显示不受影响），会话目录引用一并清空。
        """
        self._file_enabled = bool(enabled)
        if self._file_enabled:
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                session_dir = self._log_dir / ts
                session_dir.mkdir(parents=True, exist_ok=True)
                self._session_ts = ts          # 与 _SESSION_RE 的 group(1) 同形（带下划线）
                self._session_dir = session_dir
                self.log_file = session_dir / f"MaaRM_{ts}.log"
                # 注意：传字符串路径用内置 open（buffering=1 行缓冲；本环境 open 的
                # line_buffering 关键字被拦截，故用等价的标准 buffering=1）。
                self._tier = _TierFile(self.log_file, self.MAX_BYTES, self.BACKUP_COUNT)
            except OSError:
                self._file_enabled = False   # 目录建不出来（无权限等）：回到关闭态，不干扰主流程
                self.log_file = None
                self._tier = None
                self._session_ts = None
                self._session_dir = None
        else:
            with self._lock:
                self._close_tier()
            self.log_file = None
            self._session_ts = None
            self._session_dir = None

    def _close_tier(self) -> None:
        """关闭写盘句柄（幂等）。假设持有 `_lock` 或无并发写盘。"""
        tier, self._tier = self._tier, None
        if tier is not None:
            tier.close()

    def close(self) -> None:
        """关闭写盘句柄并 flush 尾部（进程退出路径调用；sidecar 停止为全仓唯一入口）。

        退出收尾（契约 §2）：为所有未收尾组补 `group_end(outcome="incomplete")`，
        `_finalized` 保证只执行一次；重复调用仅重走幂等的句柄关闭。
        """
        with self._lock:
            if not self._finalized:
                self._finalized = True
                for gid in list(self._groups.keys()):
                    self._end_group_locked(gid, "incomplete")
            self._close_tier()

    def prune_sessions(self, keep: int = KEEP_SESSIONS) -> None:
        """按会话清理 logs/ 下旧记录，保留最近 `keep` 组。

        一个会话 = `logs/<ts>/` 目录（现行形态：内含 MaaRM_<ts>.log 与伴随产物
        trace.jsonl，整目录同删）或旧版平铺的 `MaaRM_<14位数字>(.debug)?.log(.N)?`
        文件；两类都按时间戳归组。当前正在写入的会话永不删除；不碰
        sidecar_stderr.log 等其它文件与目录。删除失败不中断业务。
        """
        if not self._log_dir.exists():
            return
        # 按会话时间戳归组（目录与旧版平铺文件同组同清）
        groups: dict[str, list] = {}
        for p in self._log_dir.iterdir():
            if p.is_dir():
                m = self._SESSION_DIR_RE.match(p.name)
                if m:
                    groups.setdefault(m.group(1), []).append(p)
                continue
            if not p.name.startswith("MaaRM_"):
                continue
            m = self._SESSION_RE.match(p.name)
            if not m:
                continue
            groups.setdefault(m.group(1), []).append(p)
        if not groups:
            return
        ordered = sorted(groups.keys(), reverse=True)  # 新的在前
        for ts in ordered[keep:]:
            if ts == self._session_ts:
                continue  # 当前会话永不删
            for p in groups[ts]:
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                except OSError:
                    pass  # 删除失败不中断业务

    # ---------- 记录构造 / 投影 / 发射 ----------
    @staticmethod
    def _as_text(value) -> str:
        if isinstance(value, str):
            return value
        return str(value)

    def _sanitize_fields(self, fields):
        """净化（假设持有 _lock）：非 dict 整体丢弃；键裁剪/敏感键丢弃/值标量化。

        返回 None 表示无有效 fields。净化后记录即 JSON 安全。
        """
        if fields is None:
            return None
        if not isinstance(fields, dict):
            self._bump("fields_not_dict")
            return None
        out = {}
        for k, v in fields.items():
            if len(out) >= self.FIELDS_MAX_KEYS:
                self._bump("fields_overflow")
                break
            if not isinstance(k, str):
                k = str(k)
                self._bump("fields_key_coerced")
            k = k[:self.FIELDS_MAX_KEY_LEN]
            if self._SENSITIVE_KEY_RE.search(k):
                self._bump("fields_sensitive_dropped")
                continue
            if v is None or isinstance(v, bool) or isinstance(v, int):
                pass
            elif isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    v = None
                    self._bump("fields_nonfinite")
            elif isinstance(v, str):
                if len(v) > self.FIELDS_MAX_STR_LEN:
                    v = v[:self.FIELDS_MAX_STR_LEN - 1] + "…"
                    self._bump("fields_str_trimmed")
            else:
                v = str(v)
                self._bump("fields_value_coerced")
            out[k] = v
        return out or None

    def _make_record(self, event_type, *, channel, level, message=None, title=None,
                     kind=None, group_id=None, fields=None, outcome=None,
                     has_warning=None, has_error=None, duration_ms=None, seq=None):
        ts = datetime.now().strftime("%H:%M:%S")
        return {
            "schema_version": self.SCHEMA_VERSION,
            "seq": seq,
            "event_type": event_type,
            "group_id": group_id,
            "session_id": self._session_id,
            "ts": ts,
            "channel": channel,
            "level": level,
            "kind": kind,
            "title": title,
            "message": message,
            "fields": fields,
            "outcome": outcome,
            "has_warning": has_warning,
            "has_error": has_error,
            "duration_ms": duration_ms,
        }

    @staticmethod
    def _esc(s: str) -> str:
        """落盘标记的转义（GH Actions 规则）：先 % 后 : 与换行，防二次转义。"""
        return (s.replace("%", "%25").replace(":", "%3A")
                 .replace("\n", "%0A").replace("\r", "%0D"))

    @classmethod
    def _project_file(cls, rec: dict) -> str:
        et = rec["event_type"]
        if et == "group_start":
            return f"::group:: {cls._esc(rec['title'] or '')}"
        if et == "group_end":
            return f"::endgroup:: {rec['outcome']}"
        return f"[{rec['ts']}] [{rec['level']}] {rec['message']}"

    @staticmethod
    def _project_gui(rec: dict):
        """GUI 文本投影：过渡期只渲染 log 事件——结构事件混入会让仍靠措辞锚点
        的现网前端把它们显示成裸行。前端结构化 renderer 上线后此函数退役。"""
        if rec["event_type"] != "log":
            return None
        return f"[{rec['ts']}] [{rec['level']}] {rec['message']}"

    def _emit_locked(self, rec: dict) -> int:
        """入环形缓冲 + 落盘。假设持有 _lock。"""
        if rec["seq"] is None:
            self._seq += 1
            rec["seq"] = self._seq
        else:
            self._seq = rec["seq"]
        self._lines.append((rec["seq"], rec))
        if self._tier is not None:
            self._tier.write(self._project_file(rec))
        return rec["seq"]

    # ---------- 公开写入 API ----------
    def log(self, msg: str, level: str = "INFO", channel: str | None = None, *,
            group_id: str | None = None, fields: dict | None = None):
        """记录一个 log 事件。

        channel=None → 有 group_id 时继承组 channel，否则 DEFAULT_CHANNEL（app）。
        通道管「要不要打」（调级）：低于该通道有效级别的行直接丢弃（不进内存、
        不落盘、不参与组 outcome 推导）。通道级别查不到时回落到 _min_level
        （None=全记录）。级别不参与写侧落点判定——落盘是单流全量，级别过滤只发生
        在读侧（GUI）与导出侧。
        group_id 未知/已收尾 → 降级为无组事件并计数。返回分配的 seq。
        """
        with self._lock:
            grp = self._groups.get(group_id) if group_id else None
            if group_id and grp is None:
                self._bump("unknown_group_id")
                group_id = None
            if channel is None:
                channel = grp["channel"] if grp else self.DEFAULT_CHANNEL
            eff = self._channel_levels.get(channel, self._min_level)
            if eff is not None and self.LEVELS.get(level, 2) < self.LEVELS.get(eff, 2):
                return None  # 通道级别过滤
            rec = self._make_record(
                "log", channel=channel, level=level,
                message=self._as_text(msg), group_id=group_id,
                fields=self._sanitize_fields(fields))
            seq = self._emit_locked(rec)
            if grp is not None:
                if level == "ERROR":
                    grp["has_error"] = True
                elif level == "WARNING":
                    grp["has_warning"] = True
            return seq

    def group(self, title: str, kind: str | None = None,
              channel: str | None = None) -> "GroupHandle":
        """开一个组（GUI 卡片的真源）。返回句柄；组事件不受通道级别闸门约束。

        kind 为稳定机器枚举字符串（phase/session/loop…），中文不进枚举；
        非字符串 kind 记 None 并计数。
        """
        with self._lock:
            if kind is not None and not isinstance(kind, str):
                self._bump("kind_coerced")
                kind = None
            seq = self._seq + 1
            gid = f"g-{seq}"
            rec = self._make_record(
                "group_start", channel=channel or self.DEFAULT_CHANNEL, level="INFO",
                title=self._as_text(title), kind=kind, group_id=gid,
                outcome="running", seq=seq)
            self._groups[gid] = {
                "channel": rec["channel"], "kind": kind, "title": rec["title"],
                "opened": time.monotonic(), "has_warning": False, "has_error": False,
            }
            self._emit_locked(rec)
            return GroupHandle(self, gid)

    def end_group(self, group_id: str, outcome: str | None = None):
        """收尾组。首次调用返回最终 outcome；重复调用幂等返回同一 outcome。

        outcome=None → 自动推导（组内有 ERROR→failure，否则 WARNING→warning，
        否则 success）；显式值必须在 END_OUTCOMES 内（非法值计数后回落自动推导）。
        显式 success 与组内出现过 WARNING 可共存（本项目 WARNING=可恢复降级）。
        """
        with self._lock:
            return self._end_group_locked(group_id, outcome)

    def _end_group_locked(self, group_id: str, outcome: str | None):
        grp = self._groups.pop(group_id, None)
        if grp is None:
            tomb = self._closed_outcomes.get(group_id)
            if tomb is not None:
                self._bump("double_end")
                return tomb
            self._bump("unknown_group_end")
            return None
        auto = ("failure" if grp["has_error"]
                else "warning" if grp["has_warning"] else "success")
        if outcome is None:
            final = auto
        elif outcome in self.END_OUTCOMES:
            final = outcome
        else:
            self._bump("invalid_outcome")
            final = auto
        rec = self._make_record(
            "group_end", channel=grp["channel"], level="INFO",
            title=grp["title"], kind=grp["kind"], group_id=group_id,
            outcome=final, has_warning=grp["has_warning"], has_error=grp["has_error"],
            duration_ms=int((time.monotonic() - grp["opened"]) * 1000))
        self._emit_locked(rec)
        if len(self._closed_outcomes) >= self._CLOSED_TOMBSTONES:
            self._closed_outcomes.pop(next(iter(self._closed_outcomes)))
        self._closed_outcomes[group_id] = final
        return final

    def set_channel_level(self, channel: str, level: str) -> None:
        """运行时调整某通道的最低记录级别（channel 名不透明字符串）。"""
        self._channel_levels[channel] = level

    def clear_channel_level(self, channel: str) -> None:
        """移除通道级别覆盖，回落全局 _min_level。"""
        self._channel_levels.pop(channel, None)

    # ---------- 读侧（GUI 文本投影，过渡期契约不变） ----------
    def get_lines(self, min_level: str = "INFO"):
        """获取日志行，可按级别过滤。GUI 默认只显示 INFO 及以上。

        只含 log 事件的投影（结构事件不进 GUI 文本流，见 _project_gui）。
        """
        min_val = self.LEVELS.get(min_level, 2)
        with self._lock:
            return [line for _s, rec in self._lines
                    if (line := self._project_gui(rec)) is not None
                    and self.LEVELS.get(rec["level"], 2) >= min_val]

    def get_lines_since(self, seq: int, min_level: str = "INFO"):
        """返回单调序列号 `seq` 之后满足级别的行，及最新游标。

        返回 (lines, new_seq, truncated)：
          - lines: list[str]，seq 严格大于给定 seq 且级别满足的行（仅 log 事件）。
          - new_seq: 本次快照的总写入数（含结构事件占用的号段），作为下一次 fetch 的游标。
          - truncated: 给定 seq 已落在环形缓冲覆盖范围之外（GUI 落后过多），
            由调用方据此插入提示分隔，而非静默丢行。
        """
        min_val = self.LEVELS.get(min_level, 2)
        with self._lock:
            out = []
            truncated = False
            if self._lines:
                oldest_seq = self._lines[0][0]
                # 游标 seq 的语义是「只要 seq 之后的行」，故最早需要的是 seq+1；
                # 只有 seq+1 也已落出缓冲才算真丢行，即 seq < oldest_seq - 1。
                # 写成 seq < oldest_seq 会误判边界：缓冲未回绕时 oldest_seq 恒为 1，
                # 首帧游标 0 即落在边界上，会让 GUI 每次会话首次拉取都插一条假的截断提示。
                if seq < oldest_seq - 1:
                    truncated = True
                for s, rec in self._lines:
                    if s <= seq:
                        continue
                    if self.LEVELS.get(rec["level"], 2) < min_val:
                        continue
                    line = self._project_gui(rec)
                    if line is not None:
                        out.append(line)
            new_seq = self._seq
        return out, new_seq, truncated

    def get_events_since(self, seq: int, min_level: str = "INFO"):
        """结构化 GUI 通道（契约 §6）：返回 (events, new_seq, truncated, gap)。

        - events: `seq` 之后的记录副本——log 事件按级别过滤，**组事件不受级别
          闸门约束**（结构完整性优先，见模块 docstring）。
        - new_seq: 本次快照的总写入数（含结构事件号段），下一次 fetch 的游标。
        - truncated / gap: 游标落在环形覆盖外时 gap 给出丢号区间 {from_seq, to_seq}，
          由消费方显式渲染截断，不再伪装成文本行。
        """
        min_val = self.LEVELS.get(min_level, 2)
        with self._lock:
            out = []
            truncated = False
            gap = None
            if self._lines:
                oldest_seq = self._lines[0][0]
                if seq < oldest_seq - 1:
                    truncated = True
                    gap = {"from_seq": seq + 1, "to_seq": oldest_seq - 1}
                for s, rec in self._lines:
                    if s <= seq:
                        continue
                    if rec["event_type"] == "log" and self.LEVELS.get(rec["level"], 2) < min_val:
                        continue
                    out.append(dict(rec))
            return out, self._seq, truncated, gap

    @staticmethod
    def _extract_level(line: str) -> str:
        """从日志行中提取级别，如 [INFO] → INFO（兼容旧调用方；记录化后内部不再使用）"""
        parts = line.split("] [")
        if len(parts) >= 2:
            return parts[1].split("]")[0]
        return "INFO"


class GroupHandle:
    """logger.group() 返回的句柄。跨线程传句柄或 group_id 均可继续写组内事件。"""

    __slots__ = ("_logger", "id")

    def __init__(self, lg: Logger, group_id: str):
        self._logger = lg
        self.id = group_id

    def log(self, msg: str, level: str = "INFO", fields: dict | None = None,
            channel: str | None = None):
        """组内事件。channel 缺省继承组 channel，显式传值覆盖。返回 seq（被闸门挡下为 None）。"""
        return self._logger.log(msg, level, channel, group_id=self.id, fields=fields)

    def end(self, outcome: str | None = None):
        """收尾。首次返回最终 outcome；重复调用幂等返回同一 outcome。"""
        return self._logger.end_group(self.id, outcome)

    def __enter__(self) -> "GroupHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 异常路径以 failure 收尾，但**不吞原异常**（返回 False 让异常继续传播）。
        self.end("failure" if exc_type else None)
        return False


# 全局日志单例（日志根 = 用户数据目录 / logs，开发版与发行版一致；默认不写盘）
logger = Logger(logs_dir())
