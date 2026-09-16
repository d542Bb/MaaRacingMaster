# -*- coding: utf-8 -*-
"""Logger 日志模块单测：环形缓冲 / 序列号游标 / 加锁写盘 / 通道机制。

覆盖计划（log-channel-separation）：
- P1 T2 有界环形缓冲：回绕后 get_lines_since 不重不漏、truncated 语义
- P1 T4 加锁持句柄写盘：多线程并发写行完整、无交错、close 幂等
- P2 T3 单流落盘 + 轮转 + 会话保留
- P3 T1 通道机制：通道级别覆盖与回落、DEFAULT 回落
- 会话目录形态：logs/<ts>/ 承载日志与伴随产物（trace.jsonl），整目录同清
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from maaracing_master.core.logger import Logger


def _msg(line: str) -> str:
    """从 `[HH:MM:SS] [LEVEL] msg` 行里解析出 msg。"""
    m = re.search(r"\] \[[A-Z]+\] (.*)$", line)
    return m.group(1) if m else line


def _session_logs(root: Path) -> list[Path]:
    """会话里的日志文件：现行形态 logs/<ts>/MaaRM_*.log* 与旧版平铺同列。"""
    return sorted(root.glob("*/MaaRM_*.log*")) + sorted(root.glob("MaaRM_*.log*"))


def _main_log(root: Path) -> Path:
    """会话目录里的日志主档（排除轮转备份）。"""
    files = [p for p in _session_logs(root) if not re.search(r"\.log\.\d+$", p.name)]
    assert len(files) == 1
    return files[0]


@pytest.fixture
def log(tmp_path: Path):
    return Logger(tmp_path)


# ---------- T2 环形缓冲 ----------

def test_get_lines_since_returns_incremental(log):
    log.log("a")
    log.log("b")
    lines1, new_seq1, _ = log.get_lines_since(0, "INFO")
    assert [_msg(ln) for ln in lines1] == ["a", "b"]
    assert new_seq1 == 2
    # 游标续读：无新行
    lines2, new_seq2, _ = log.get_lines_since(new_seq1, "INFO")
    assert lines2 == [] and new_seq2 == new_seq1


def test_get_lines_since_rollover_no_loss_no_dup(log):
    """环形回绕后，从缓冲内某 seq 起步，不重不漏。"""
    cap = Logger.BUFFER_CAPACITY
    total = cap + cap // 2
    for i in range(1, total + 1):
        log.log(f"line-{i}")
    assert len(log._lines) == cap                 # 环形：缓冲恒定
    # 缓冲内最老 seq = total - cap + 1，取其中一点 sample_seq
    sample_seq = total - cap + 1
    lines, new_seq, truncated = log.get_lines_since(sample_seq, "INFO")
    assert truncated is False
    assert new_seq == total
    msgs = [_msg(ln) for ln in lines]
    assert msgs == [f"line-{i}" for i in range(sample_seq + 1, total + 1)]  # 无重复、无遗漏


def test_get_lines_since_truncated_flag(log):
    cap = Logger.BUFFER_CAPACITY
    for i in range(cap + 5):
        log.log(f"line-{i}")
    _, _, truncated = log.get_lines_since(1, "INFO")          # 已落出缓冲 → 截断
    assert truncated is True
    oldest = Logger.BUFFER_CAPACITY + 5 - cap + 1
    _, _, truncated2 = log.get_lines_since(oldest, "INFO")    # 缓冲内 → 不截断
    assert truncated2 is False


def test_get_lines_since_truncated_boundary(log):
    """边界：游标恰好等于 oldest-1 时不算截断（此时 seq+1 仍在缓冲内）。

    这是 `seq < oldest` 与 `seq < oldest-1` 两式的唯一分歧点。缓冲未回绕时
    oldest 恒为 1，GUI 首帧游标 0 正落在此边界上——误判会让每次会话首次拉取
    都插一条假的「落后过多已截断」提示。上一条用例取的是 seq=1 与 seq=oldest
    两点，恰好跨过该边界而未覆盖。
    """
    log.log("first")
    lines, _, truncated = log.get_lines_since(0, "INFO")
    assert truncated is False                      # 未回绕：0 之后的行全在缓冲里
    assert [_msg(ln) for ln in lines] == ["first"]

    for i in range(Logger.BUFFER_CAPACITY):
        log.log(f"line-{i}")
    oldest = log._lines[0][0]
    assert oldest > 1                              # 已回绕
    _, _, at_boundary = log.get_lines_since(oldest - 1, "INFO")
    assert at_boundary is False                    # 要的行从 oldest 起，都在
    _, _, past_boundary = log.get_lines_since(oldest - 2, "INFO")
    assert past_boundary is True                   # oldest-1 本身已丢


def test_get_lines_since_level_filter(log):
    log.log("info", "INFO")
    log.log("debug-a", "DEBUG")
    log.log("warn", "WARNING")
    info_only, _, _ = log.get_lines_since(0, "INFO")
    assert [_msg(ln) for ln in info_only] == ["info", "warn"]
    debug_all, _, _ = log.get_lines_since(0, "DEBUG")
    assert len(debug_all) == 3


def test_get_lines_compat(log):
    """旧 API get_lines 保持级别过滤（list[str]）。"""
    log.log("x", "INFO")
    log.log("y", "DEBUG")
    assert [_msg(ln) for ln in log.get_lines("INFO")] == ["x"]


# ---------- T4 加锁持句柄写盘 ----------

def test_file_logging_writes_lines(tmp_path: Path):
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.log("hello disk", "INFO")
    log.close()
    main = _main_log(tmp_path)
    assert "hello disk" in main.read_text(encoding="utf-8")


def test_session_dir_holds_log_and_companions(tmp_path: Path):
    """会话目录形态：logs/<ts>/ 承载日志与伴随产物（决策流水），session_dir 即共用落点。"""
    log = Logger(tmp_path)
    assert log.session_dir is None                     # 未开写盘 → 无会话目录
    log.set_file_logging(True)
    sink = log.session_dir
    assert sink is not None and sink.parent == tmp_path
    assert re.fullmatch(r"\d{8}_\d{6}", sink.name)
    (sink / "trace.jsonl").write_text("{}\n", encoding="utf-8")   # 伴随产物与日志同目录
    log.log("hello", "INFO")
    assert log.log_file == sink / f"MaaRM_{sink.name}.log"
    log.close()
    assert (sink / "trace.jsonl").is_file()
    assert "hello" in log.log_file.read_text(encoding="utf-8")

    log.set_file_logging(False)                        # 关开关 → 落点引用清空（伴随产物随之停写）
    assert log.session_dir is None


def test_multithread_writes_no_interleave(tmp_path: Path):
    """8 线程并发写同一句柄：不丢行、每行完整、无交错。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    n_threads = 8
    per_thread = 200
    marker = "MARKER"
    expected = {f"{marker}-t{t}-i{i}" for t in range(n_threads) for i in range(per_thread)}

    def worker(tid):
        for i in range(per_thread):
            log.log(f"{marker}-t{tid}-i{i}", "INFO")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()

    content = _main_log(tmp_path).read_text(encoding="utf-8")
    body = [_msg(ln) for ln in content.splitlines() if marker in ln]
    assert len(body) == n_threads * per_thread       # 无丢行
    assert set(body) == expected                      # 每行完整、无交错/截断


def test_close_idempotent(log):
    log.set_file_logging(True)
    log.close()
    log.close()  # 二次 close 不抛异常


def test_file_logging_disabled_ignored_writes(log):
    """关闭写盘后，log 只进内存缓冲不写盘（也不建会话目录）。"""
    log.set_file_logging(False)
    log.log("no disk", "INFO")
    assert _session_logs(log._log_dir) == []
    assert log.session_dir is None


# ---------- T3 单流落盘 + 轮转 + 会话保留（P2） ----------

def test_single_file_keeps_all_levels_in_order(tmp_path: Path):
    """单流全量：一次开启只建一个文件，各级别按发生顺序写入（时序不被打散）。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.log("i", "INFO")
    log.log("d", "DEBUG")
    log.log("w", "WARNING")
    log.log("t", "TRACE")
    log.close()
    files = [p for p in _session_logs(tmp_path) if not re.search(r"\.log\.\d+$", p.name)]
    assert len(files) == 1                        # 不再按级别分档
    body = [_msg(ln) for ln in files[0].read_text(encoding="utf-8").splitlines()]
    assert body == ["i", "d", "w", "t"]           # 顺序即发生顺序，四个级别同处一文件


def test_rotation_triggers_and_backup_count(tmp_path: Path):
    """超过 MAX_BYTES 触发轮转，备份份数受 BACKUP_COUNT 限制。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    # 用小阈值强制轮转（避免真的写 16MB）
    assert log._tier is not None
    log._tier.max_bytes = 1000  # 1KB 即轮转
    for _ in range(1200):
        log.log("x" * 100, "ERROR")
    log.close()
    names = [p.name for p in _session_logs(tmp_path)]
    assert any(n.endswith(".log.1") for n in names)  # 发生过轮转
    backups = [n for n in names if re.search(r"\.log\.(\d+)$", n)]
    nums = [int(re.search(r"\.log\.(\d+)$", n).group(1)) for n in backups]
    assert nums and max(nums) <= Logger.BACKUP_COUNT


def test_prune_keeps_recent_and_ignores_others(tmp_path: Path):
    """保留只清旧会话；不碰 sidecar_stderr.log / 无关文件；旧版 .debug.log 一并回收。"""
    log = Logger(tmp_path)
    # 造 5 个会话（互不相同的时间戳），其中夹带旧版分档遗留的 .debug.log
    for i in range(5):
        ts = f"2026090{i + 1}_000000"
        (tmp_path / f"MaaRM_{ts}.log").write_text("old\n", encoding="utf-8")
        (tmp_path / f"MaaRM_{ts}.debug.log").write_text("old\n", encoding="utf-8")
    (tmp_path / "sidecar_stderr.log").write_text("keep\n", encoding="utf-8")
    (tmp_path / "NOT_a_log.txt").write_text("keep\n", encoding="utf-8")
    log.prune_sessions(keep=1)   # 只保留最近 1 组
    # 非目标文件保留
    assert (tmp_path / "sidecar_stderr.log").exists()
    assert (tmp_path / "NOT_a_log.txt").exists()
    # 只剩最近 1 组：其 .log 与旧版 .debug.log 同属一组，整组同清
    remaining = sorted(p.name for p in tmp_path.glob("MaaRM_*"))
    assert remaining == ["MaaRM_20260905_000000.debug.log", "MaaRM_20260905_000000.log"]

def test_prune_never_deletes_active_session(tmp_path: Path):
    """活动会话永不删；写盘关闭后回落普通保留语义（会话目录整目录回收）。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.log("active", "INFO")          # 写入当前会话文件（session_ts 此时非 None）
    cur_file = log.log_file            # 当前会话文件
    session_dir = log.session_dir
    assert session_dir is not None
    (session_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")   # 伴随产物
    assert cur_file.exists()
    log.prune_sessions(keep=0)         # keep=0：理论上全清，但活动会话必须保留
    assert cur_file.exists()           # 活动会话未被删
    assert (session_dir / "trace.jsonl").exists()
    log.set_file_logging(False)        # 关闭写盘（session_ts 清空 → 不再保护）
    log.prune_sessions(keep=0)
    assert not cur_file.exists()
    assert not session_dir.exists()    # 会话目录连同伴随产物整目录回收


def test_prune_recycles_session_dirs_with_companions(tmp_path: Path):
    """现行形态按会话目录整目录回收（日志 + trace.jsonl 同清），不碰无关目录。"""
    log = Logger(tmp_path)
    for i in range(3):
        d = tmp_path / f"2026090{i + 1}_000000"
        d.mkdir()
        (d / f"MaaRM_{d.name}.log").write_text("old\n", encoding="utf-8")
        (d / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    misc = tmp_path / "misc"
    misc.mkdir()
    (misc / "notes.txt").write_text("keep\n", encoding="utf-8")
    log.prune_sessions(keep=1)
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ["20260903_000000", "misc"]
    # 旧会话目录里的 trace.jsonl 随目录一起清掉，不残留孤儿文件
    assert not (tmp_path / "20260901_000000").exists()
    assert (tmp_path / "20260903_000000" / "trace.jsonl").is_file()
    assert (misc / "notes.txt").is_file()


# ---------- T1 通道机制（P3） ----------

def test_channel_default_to_app(log):
    """channel=None 回落 DEFAULT_CHANNEL(app)；通道级别过滤生效。"""
    log.set_channel_level("app", "ERROR")
    log.log("low", "INFO")             # app 通道，INFO < ERROR → 不打
    log.log("high", "ERROR")           # 达到阈值 → 打
    lines, _, _ = log.get_lines_since(0, "TRACE")
    assert not any("low" in ln for ln in lines)
    assert any("high" in ln for ln in lines)


def test_channel_level_override_isolated(log):
    """通道级别独立过滤，不互相污染；默认通道（app）不受影响。"""
    log.set_channel_level("treasure", "ERROR")
    # app 通道（无覆盖）全记录
    log.log("app-info", "INFO")
    # treasure 通道被提到 ERROR：其 INFO/DEBUG 丢弃，ERROR 保留
    log.log("t-debug", "DEBUG", channel="treasure")
    log.log("t-info", "INFO", channel="treasure")
    log.log("t-error", "ERROR", channel="treasure")
    lines, _, _ = log.get_lines_since(0, "TRACE")
    text = "\n".join(lines)
    assert "t-debug" not in text and "t-info" not in text
    assert "t-error" in text
    assert "app-info" in text          # app 通道不受 treasure 覆盖影响


def test_channel_level_reset(log):
    """clear_channel_level 回落全局（None=全记录）。"""
    log.set_channel_level("ocr", "ERROR")
    log.clear_channel_level("ocr")
    log.log("doc", "INFO", channel="ocr")   # 回落 → 打
    lines, _, _ = log.get_lines_since(0, "TRACE")
    assert any("doc" in ln for ln in lines)


def test_channel_filter_applies_to_disk(tmp_path: Path):
    """通道级别过滤同样作用于落盘（被过滤行不写盘）。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.set_channel_level("treasure", "ERROR")
    log.log("kept", "ERROR", channel="treasure")
    log.log("dropped", "INFO", channel="treasure")
    log.close()
    text = "".join(
        p.read_text(encoding="utf-8")
        for p in _session_logs(tmp_path)
    )
    assert "kept" in text
    assert "dropped" not in text