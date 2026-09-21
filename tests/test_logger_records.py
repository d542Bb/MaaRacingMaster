# -*- coding: utf-8 -*-
"""结构化日志记录与组生命周期单测。

契约真源：tools/experiments/log_card_lab/DESIGN_log_api.md（v2 冻结稿）。
本文件逐条锁死：生命周期语义（幂等 end / 野 id / shutdown 补 incomplete）、
状态模型（自动推导 + 显式终态共存）、fields 净化管道、双投影（GUI 文本兼容 /
文件 ::group:: 标记与转义）、通道继承与闸门、schema_version 统一命名。
"""
import math
import threading

import pytest

from maaracing_master.core.logger import Logger


@pytest.fixture
def log(tmp_path):
    return Logger(tmp_path)


def _records(lg):
    return [rec for _s, rec in lg._lines]


def _events(lg, et):
    return [r for r in _records(lg) if r["event_type"] == et]


# ---------- 向后兼容（现网字节契约） ----------
def test_log_text_projection_unchanged(log):
    log.log("hello", "INFO")
    log.log("bad", "ERROR")
    lines, new_seq, truncated = log.get_lines_since(0, "INFO")
    assert [ln.split("] ", 2)[2] for ln in lines] == ["hello", "bad"]
    assert lines[0].startswith("[") and "] [INFO] hello" in lines[0]
    assert "] [ERROR] bad" in lines[1]
    assert new_seq == 2 and truncated is False


def test_schema_version_field_unified(log):
    # 契约命名统一：记录里是 schema_version，不存在 log_protocol_version。
    log.log("x")
    rec = _records(log)[0]
    assert rec["schema_version"] == Logger.SCHEMA_VERSION
    assert "log_protocol_version" not in rec


# ---------- 生命周期 ----------
def test_group_lifecycle_auto_outcome_success(log):
    g = log.group("拍品观察", kind="phase", channel="treasure")
    g.log("识别到目标")
    assert g.end() == "success"
    start = _events(log, "group_start")[0]
    end = _events(log, "group_end")[0]
    assert start["outcome"] == "running" and start["group_id"] == g.id
    assert start["kind"] == "phase" and start["title"] == "拍品观察"
    assert end["outcome"] == "success" and end["has_error"] is False
    assert end["duration_ms"] is not None


def test_group_outcome_warning_and_failure(log):
    g1 = log.group("a")
    g1.log("降级", "WARNING")
    assert g1.end() == "warning"
    g2 = log.group("b")
    g2.log("炸了", "ERROR")
    assert g2.end() == "failure"


def test_explicit_success_coexists_with_warning(log):
    # 本项目 WARNING=可恢复降级：显式 success 合法，且 has_warning 如实入档。
    g = log.group("c")
    g.log("重试成功", "WARNING")
    assert g.end(outcome="success") == "success"
    end = _events(log, "group_end")[0]
    assert end["outcome"] == "success" and end["has_warning"] is True


def test_invalid_outcome_falls_back_to_auto(log):
    g = log.group("d")
    g.log("x", "ERROR")
    assert g.end(outcome="cancelled") == "failure"  # 非法值→自动推导+计数
    assert log.internal_counters["invalid_outcome"] == 1


def test_double_end_idempotent(log):
    g = log.group("e")
    first = g.end()
    second = g.end()
    assert first == second == "success"
    assert len(_events(log, "group_end")) == 1
    assert log.internal_counters["double_end"] == 1


def test_unknown_group_id_degrades(log):
    seq = log.log("孤儿行", group_id="g-999")
    assert seq is not None
    rec = _records(log)[-1]
    assert rec["group_id"] is None
    assert log.internal_counters["unknown_group_id"] == 1
    assert log.end_group("g-999") is None
    assert log.internal_counters["unknown_group_end"] == 1


def test_with_block_exception_marks_failure_and_reraises(log):
    with pytest.raises(RuntimeError):
        with log.group("f"):
            raise RuntimeError("boom")
    end = _events(log, "group_end")[0]
    assert end["outcome"] == "failure"  # 不吞原异常由 pytest.raises 证明


def test_close_finalizes_open_groups_once(log):
    log.group("g1")
    log.group("g2")
    log.close()
    ends = _events(log, "group_end")
    assert len(ends) == 2 and all(e["outcome"] == "incomplete" for e in ends)
    log.close()  # 幂等：不重复补
    assert len(_events(log, "group_end")) == 2


# ---------- fields 净化管道（先净化后入记录） ----------
def test_fields_sanitized(log):
    g = log.group("h")
    g.log("载荷", fields={
        "ok_int": 5, "ok_bool": True, "ok_none": None,
        "nan": math.nan, "inf": math.inf,
        "long_str": "x" * 300,
        "obj": (1, 2),
        "password": "hunter2",
    })
    fields = _events(log, "log")[0]["fields"]
    assert fields["ok_int"] == 5 and fields["ok_bool"] is True
    assert fields["nan"] is None and fields["inf"] is None
    assert len(fields["long_str"]) == 200 and fields["long_str"].endswith("…")
    assert fields["obj"] == "(1, 2)"
    assert "password" not in fields
    c = log.internal_counters
    assert c["fields_nonfinite"] == 2 and c["fields_sensitive_dropped"] == 1
    assert c["fields_str_trimmed"] == 1 and c["fields_value_coerced"] == 1


def test_fields_key_limit_and_non_dict(log):
    g = log.group("i")
    g.log("x", fields={f"k{i}": i for i in range(40)})
    assert len(_events(log, "log")[0]["fields"]) == Logger.FIELDS_MAX_KEYS
    g.log("y", fields=["not", "a", "dict"])
    assert _records(log)[-1]["fields"] is None
    assert log.internal_counters["fields_not_dict"] == 1


# ---------- 双投影 ----------
def test_gui_projection_excludes_group_events(log):
    g = log.group("j")
    g.log("内容行")
    g.end()
    lines, new_seq, _ = log.get_lines_since(0, "INFO")
    assert all("::group::" not in ln and "endgroup" not in ln for ln in lines)  # 结构事件不混入
    assert any("内容行" in ln for ln in lines)
    # 游标含结构事件号段：组开+内容+组收 = 3 个 seq
    assert new_seq == 3


def test_file_projection_group_markers_and_escaping(tmp_path):
    lg = Logger(tmp_path)
    lg.set_file_logging(True)
    g = lg.group("阶段: 100%\n换行")
    g.log("正文")
    g.end()
    lg.close()
    text = lg.log_file.read_text(encoding="utf-8")
    assert "::group:: 阶段%3A 100%25%0A换行" in text
    assert "::endgroup:: success" in text
    assert "] [INFO] 正文" in text  # log 事件保持原格式


# ---------- 通道语义 ----------
def test_group_channel_inheritance_and_override(log):
    g = log.group("k", channel="treasure")
    g.log("继承")
    g.log("覆盖", channel="app")
    recs = _events(log, "log")
    assert recs[0]["channel"] == "treasure"
    assert recs[1]["channel"] == "app"
    log.log("跨线程挂组继承", group_id=g.id)
    assert _records(log)[-1]["channel"] == "treasure"
    g.end()


def test_channel_gate_drops_log_keeps_structure(log):
    log.set_channel_level("treasure", "WARNING")
    g = log.group("m", channel="treasure")
    g.log("被闸门挡下", "INFO")
    g.log("放行", "WARNING")
    assert g.end() == "warning"
    # 闸门挡下的 INFO 不参与 outcome 推导，也不进缓冲
    assert all("被闸门挡下" not in (r["message"] or "") for r in _records(log))
    # 结构事件不受闸门约束：组开/组收都在
    assert len(_events(log, "group_start")) == 1


# ---------- 派发捕获 / 迟到归属（裁定边界规则 2/5） ----------
def test_worker_captured_group_id_routes_to_dispatch_group(log):
    # worker 不读可变当前组：派发时捕获的 group_id 即使在别的组开着时也归原组。
    g1 = log.group("派发组")
    g2 = log.group("当前组")          # g1 未收尾，两组并存在开组表
    log.log("worker 迟到结果", "INFO", group_id=g1.id)   # 显式捕获 id，不读 current
    g2.end()
    g1.end()
    rows = [r for r in _events(log, "log")]
    assert [r["group_id"] for r in rows] == [g1.id]
    assert log.internal_counters == {}


def test_late_log_after_end_degrades_not_retargeted(log):
    # 组已收尾后的迟到旧 id：降级无组并计数，禁止自动改挂到新组。
    g1 = log.group("旧组")
    g1.end()
    g2 = log.group("新组")
    log.log("迟到", "INFO", group_id=g1.id)
    rec = _records(log)[-1]
    assert rec["group_id"] is None
    assert log.internal_counters["unknown_group_id"] == 1
    assert all(r["group_id"] != g2.id for r in _events(log, "log"))
    g2.end()


# ---------- 并发冒烟 ----------
def test_concurrent_groups_no_cross_contamination(log):
    def worker(n):
        g = log.group(f"w{n}")
        for i in range(20):
            g.log(f"msg-{n}-{i}")
        assert g.end() == "success"

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(_events(log, "group_start")) == 4
    assert len(_events(log, "group_end")) == 4
    assert len(_events(log, "log")) == 80
    # 每条组内事件的 group_id 都真实存在且通道一致，无串组
    gids = {e["group_id"] for e in _events(log, "group_start")}
    assert all(r["group_id"] in gids for r in _events(log, "log"))
    assert log.internal_counters == {}
