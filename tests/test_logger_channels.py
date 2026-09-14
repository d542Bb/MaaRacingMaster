# -*- coding: utf-8 -*-
"""Logger 日志模块单测：环形缓冲 / 序列号游标 / 加锁写盘 / 通道机制。

覆盖计划（log-channel-separation）：
- P1 T2 有界环形缓冲：回绕后 get_lines_since 不重不漏、truncated 语义
- P1 T4 加锁持句柄写盘：多线程并发写行完整、无交错、close 幂等
- P2 T3 落盘分档 + 轮转 + 会话保留
- P3 T1 通道机制：通道级别覆盖与回落、DEFAULT 回落
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


@pytest.fixture
def log(tmp_path: Path):
    return Logger(tmp_path)


# ---------- T2 环形缓冲 ----------

def test_get_lines_since_returns_incremental(log):
    log.log("a")
    log.log("b")
    lines1, new_seq1, _ = log.get_lines_since(0, "INFO")
    assert [_msg(l) for l in lines1] == ["a", "b"]
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
    msgs = [_msg(l) for l in lines]
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


def test_get_lines_since_level_filter(log):
    log.log("info", "INFO")
    log.log("debug-a", "DEBUG")
    log.log("warn", "WARNING")
    info_only, _, _ = log.get_lines_since(0, "INFO")
    assert [_msg(l) for l in info_only] == ["info", "warn"]
    debug_all, _, _ = log.get_lines_since(0, "DEBUG")
    assert len(debug_all) == 3


def test_get_lines_compat(log):
    """旧 API get_lines 保持级别过滤（list[str]）。"""
    log.log("x", "INFO")
    log.log("y", "DEBUG")
    assert [_msg(l) for l in log.get_lines("INFO")] == ["x"]


# ---------- T4 加锁持句柄写盘 ----------

def test_file_logging_writes_lines(tmp_path: Path):
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.log("hello disk", "INFO")
    log.close()
    files = list(tmp_path.glob("MaaRM_*.log"))
    assert len(files) == 1
    assert "hello disk" in files[0].read_text(encoding="utf-8")


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

    content = list(tmp_path.glob("MaaRM_*.log"))[0].read_text(encoding="utf-8")
    body = [_msg(l) for l in content.splitlines() if marker in l]
    assert len(body) == n_threads * per_thread       # 无丢行
    assert set(body) == expected                      # 每行完整、无交错/截断


def test_close_idempotent(log):
    log.set_file_logging(True)
    log.close()
    log.close()  # 二次 close 不抛异常


def test_file_logging_disabled_ignored_writes(log):
    """关闭写盘后，log 只进内存缓冲不写盘。"""
    log.set_file_logging(False)
    log.log("no disk", "INFO")
    assert list(log._log_dir.glob("MaaRM_*")) == []


# ---------- T3 分档落盘 + 轮转 + 会话保留（P2） ----------

def test_tiered_files_info_vs_debug(tmp_path: Path):
    """同一次开启生成 INFO 档与 DEBUG 档两个文件，级别各归各位。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.log("i", "INFO")
    log.log("w", "WARNING")
    log.log("d", "DEBUG")
    log.log("t", "TRACE")
    log.close()
    info_files = list(tmp_path.glob("MaaRM_*.log"))
    debug_files = [p for p in info_files if ".debug.log" in p.name]
    info_files = [p for p in info_files if ".debug.log" not in p.name and not re.search(r"\.log\.\d+$", p.name)]
    assert len(info_files) == 1 and len(debug_files) == 1
    assert "i" in info_files[0].read_text(encoding="utf-8")
    assert "w" in info_files[0].read_text(encoding="utf-8")
    assert "d" not in info_files[0].read_text(encoding="utf-8")  # DEBUG 不进上报档
    dbg = debug_files[0].read_text(encoding="utf-8")
    assert "d" in dbg and "t" in dbg
    assert "i" not in dbg  # INFO 不进 debug 档


def test_rotation_triggers_and_backup_count(tmp_path: Path):
    """超过 MAX_BYTES 触发轮转，备份份数受 BACKUP_COUNT 限制。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    # 用小阈值强制轮转（避免真的写 16MB）
    for t in log._tiers.values():
        t.max_bytes = 1000  # 1KB 即轮转
    for _ in range(1200):
        log.log("x" * 100, "ERROR")   # 全进上报档
    log.close()
    names = [p.name for p in tmp_path.glob("MaaRM_*.log*")]
    info_names = [n for n in names if ".debug" not in n]
    assert any(n.endswith(".log.1") for n in info_names)  # 发生过轮转
    tiers = [n for n in info_names if re.search(r"\.log\.(\d+)\.$", n)] or \
        [n for n in info_names if re.search(r"\.\d+$", n)]
    nums = [int(re.search(r"\.(\d+)$", n).group(1)) for n in tiers]
    assert nums and max(nums) <= Logger.BACKUP_COUNT


def test_prune_keeps_recent_and_ignores_others(tmp_path: Path):
    """保留只清旧会话组；不碰 sidecar_stderr.log / 无关文件。"""
    log = Logger(tmp_path)
    # 造 5 个会话组（互不相同的时间戳）+ 不相关文件
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
    # 只剩最近 1 组（2 文件：.log + .debug.log）
    remaining = sorted(p.name for p in tmp_path.glob("MaaRM_*"))
    assert len(remaining) == 2
    assert any(n.endswith(".log") for n in remaining)
    assert any(n.endswith(".debug.log") for n in remaining)

def test_prune_never_deletes_active_session(tmp_path: Path):
    """活动会话永不删；写盘关闭后回落普通保留语义。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.log("active", "INFO")          # 写入当前会话文件（session_ts 此时非 None）
    cur_file = log.log_file            # 当前会话 INFO 档
    assert cur_file.exists()
    log.prune_sessions(keep=0)         # keep=0：理论上全清，但活动会话必须保留
    assert cur_file.exists()           # 活动会话未被删
    log.set_file_logging(False)        # 关闭写盘（session_ts 清空 → 不再保护）
    log.prune_sessions(keep=0)
    assert not cur_file.exists()


# ---------- T1 通道机制（P3） ----------

def test_channel_default_to_app(log):
    """channel=None 回落 DEFAULT_CHANNEL(app)；通道级别过滤生效。"""
    log.set_channel_level("app", "ERROR")
    log.log("low", "INFO")             # app 通道，INFO < ERROR → 不打
    log.log("high", "ERROR")           # 达到阈值 → 打
    lines, _, _ = log.get_lines_since(0, "TRACE")
    assert not any("low" in l for l in lines)
    assert any("high" in l for l in lines)


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
    assert any("doc" in l for l in lines)


def test_channel_filter_applies_to_disk(tmp_path: Path):
    """通道级别过滤同样作用于落盘（被过滤行不进任何档位文件）。"""
    log = Logger(tmp_path)
    log.set_file_logging(True)
    log.set_channel_level("treasure", "ERROR")
    log.log("kept", "ERROR", channel="treasure")
    log.log("dropped", "INFO", channel="treasure")
    log.close()
    text = "".join(
        p.read_text(encoding="utf-8")
        for p in tmp_path.glob("MaaRM_*")
    )
    assert "kept" in text
    assert "dropped" not in text