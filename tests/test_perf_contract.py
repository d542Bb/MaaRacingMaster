# -*- coding: utf-8 -*-
"""性能面板契约的声明期校验与运行期规范化。

对应契约文档 §7 验收条件中属于阶段 A 的部分（1、9–12、16–24）。
"""

from __future__ import annotations

import pytest

from maaracing_master.core.perf_contract import (
    MAX_DIAGNOSTIC_ROWS, MAX_HINT_CHARS, MAX_LABEL_CHARS, MAX_TEXT_CHARS,
    format_display, normalize, validate_rows)


def _row(**kw):
    base = dict(key="tick_rate", label="驾驶拍率", kind="rate",
                stat="p50_gap_inverse", category="player", precision=1)
    base.update(kw)
    return base


# ---------------------------------------------------------------- 声明期


def test_valid_row_passes_and_is_normalized_copy():
    ok, bad = validate_rows([_row(warn_at=15.0, error_at=10.0,
                                  worse_when="below")])
    assert not bad and len(ok) == 1
    assert ok[0]["label"] == "驾驶拍率"
    assert ok[0]["warn_at"] == 15.0 and ok[0]["error_at"] == 10.0


def test_unknown_kind_and_bad_category_rejected():
    ok, bad = validate_rows([
        _row(key="a1", kind="percent"),
        _row(key="a2", category="debug"),
    ])
    assert not ok
    assert {k for k, _ in bad} == {"a1", "a2"}


@pytest.mark.parametrize("key", [
    "tick_rate_", "tick__rate", "Tick_Rate", "_rate", "拍率", "",
    "a" * 33,
])
def test_key_regex_rejects_non_snake_case(key):
    ok, bad = validate_rows([_row(key=key)])
    assert not ok, key
    assert bad


@pytest.mark.parametrize("key", ["tick_rate", "rate2", "a1_b2", "a" * 32])
def test_key_regex_accepts_snake_case(key):
    ok, _ = validate_rows([_row(key=key)])
    assert ok, key


def test_reserved_key_and_duplicate_key_rejected():
    ok, bad = validate_rows([
        _row(key="cpu"),
        _row(key="dup"),
        _row(key="dup", label="另一个"),
    ])
    assert [r["key"] for r in ok] == ["dup"]
    assert len(bad) == 2


def test_label_rules():
    ok, bad = validate_rows([
        _row(key="a1", label="abcdefgh"),          # 纯英文 → 拒
        _row(key="a2", label="abcdefgh中文"),       # 截断后失中文 → 拒
        _row(key="a3", label="一二三四五六七八九十"),  # 超 8 字 → 截断
        _row(key="a4", label="短名"),
        _row(key="a5", label="短名"),               # 同模块重名 → 拒
        _row(key="a6", label=123),                  # 类型错 → 拒
    ])
    assert [r["key"] for r in ok] == ["a3", "a4"]
    assert ok[0]["label"] == "一二三四五六七八"
    assert len(ok[0]["label"]) == MAX_LABEL_CHARS
    assert len(bad) == 4


def test_stat_required_for_rate_and_forbidden_elsewhere():
    ok, bad = validate_rows([
        _row(key="a1", stat=None),
        _row(key="a2", kind="duration", stat="instant"),
        _row(key="a3", kind="count", stat="instant"),
    ])
    assert not ok
    assert {k for k, _ in bad} == {"a1", "a2", "a3"}


def test_threshold_pairs():
    ok, bad = validate_rows([
        _row(key="a1", warn_at=15.0),                          # 只给一个
        _row(key="a2"),                                        # 都不给：合法
        _row(key="a3", worse_when="below"),                    # 无阈值却给方向
        _row(key="a4", warn_at=10.0, error_at=10.0),           # 重叠
        _row(key="a5", warn_at=10.0, error_at=15.0),           # below 反序
        _row(key="a6", warn_at=float("nan"), error_at=8.0),    # 非有限
    ])
    assert [r["key"] for r in ok] == ["a2"]
    assert ok[0]["warn_at"] is None and ok[0]["error_at"] is None
    assert len(bad) == 5


def test_state_rejects_thresholds_and_worse_when():
    ok, bad = validate_rows([
        _row(key="d1", kind="state", stat=None, warn_at=1.0, error_at=0.5),
        _row(key="d2", kind="state", stat=None, worse_when="above"),
        _row(key="d3", kind="state", stat=None),
    ])
    assert [r["key"] for r in ok] == ["d3"]
    assert len(bad) == 2


def test_precision_degrades_not_rejects():
    ok, _ = validate_rows([_row(precision=True),
                           _row(key="a2", label="另一个", precision=9)])
    assert len(ok) == 2
    assert all(r["precision"] == 1 for r in ok)


def test_hint_truncated():
    ok, _ = validate_rows([_row(hint="字" * 40)])
    assert len(ok[0]["hint"]) == MAX_HINT_CHARS


def test_row_count_caps():
    rows = [_row(key=f"k{i}", label=f"行{i}") for i in range(5)]
    ok, bad = validate_rows(rows)
    assert len(ok) == 3                       # player 上限 3
    assert len(bad) == 2

    rows = [_row(key=f"d{i}", label=f"诊{i}", category="diagnostic")
            for i in range(MAX_DIAGNOSTIC_ROWS + 2)]
    ok, bad = validate_rows(rows)
    assert len(ok) == MAX_DIAGNOSTIC_ROWS


# ---------------------------------------------------------------- 运行期


def test_normalize_missing_and_unknown_keys():
    rows, _ = validate_rows([_row(), _row(key="depth", label="深度",
                                          kind="duration", stat=None,
                                          category="diagnostic")])
    out, unknown = normalize(rows, {"nope": 1})
    assert [r["reason"] for r in out] == ["not_reported", "not_reported"]
    assert unknown == ["nope"]


def test_numeric_gate():
    rows, _ = validate_rows([_row()])
    for bad in (float("nan"), float("inf"), True, "12.4", 10 ** 400):
        out, _ = normalize(rows, {"tick_rate": bad})
        assert out[0]["value"] is None and out[0]["display"] == "—", bad
        assert out[0]["reason"] == "invalid"


def test_numpy_float_accepted():
    np = pytest.importorskip("numpy")
    rows, _ = validate_rows([_row()])
    out, _ = normalize(rows, {"tick_rate": np.float32(12.5)})
    assert out[0]["value"] == pytest.approx(12.5)
    assert out[0]["reason"] is None


def test_ratio_out_of_range_not_clamped():
    rows, _ = validate_rows([_row(key="load", label="负载",
                                  kind="ratio", stat=None, warn_at=0.6,
                                  error_at=0.85)])
    out, _ = normalize(rows, {"load": 1.8})
    assert out[0]["reason"] == "out_of_range"
    assert out[0]["display"] == "—"


def test_count_accepts_integral_float_only():
    rows, _ = validate_rows([_row(key="n", label="个数", kind="count",
                                  stat=None)])
    assert normalize(rows, {"n": 42.0})[0][0]["value"] == 42.0
    assert normalize(rows, {"n": 42.5})[0][0]["reason"] == "out_of_range"


def test_leveling_from_declaration_only():
    rows, _ = validate_rows([_row(warn_at=15.0, error_at=10.0)])
    assert normalize(rows, {"tick_rate": 20.0})[0][0]["level"] == "ok"
    assert normalize(rows, {"tick_rate": 12.0})[0][0]["level"] == "warn"
    assert normalize(rows, {"tick_rate": 5.0})[0][0]["level"] == "error"


def test_no_threshold_means_no_leveling_but_still_validated():
    rows, _ = validate_rows([_row(key="laps", label="圈数", kind="count",
                                  stat=None)])
    out, _ = normalize(rows, {"laps": 3})
    assert out[0]["level"] == "ok" and out[0]["reason"] is None
    # 不判级 ≠ 免校验
    assert normalize(rows, {"laps": float("nan")})[0][0]["reason"] == "invalid"
    assert normalize(rows, {})[0][0]["reason"] == "not_reported"


def test_state_level_and_text_rules():
    rows, _ = validate_rows([_row(key="d", label="决策", kind="state", stat=None)])
    ok = normalize(rows, {"d": {"level": "ok", "text": "DRIVING · 保持车道"}})[0][0]
    assert ok["display"] == "DRIVING · 保持车道" and ok["level"] == "ok"

    # 等级非法只降级，文本照常
    bad_level = normalize(rows, {"d": {"level": "fine", "text": "行驶中"}})[0][0]
    assert bad_level["level"] == "idle" and bad_level["display"] == "行驶中"

    # 文本无效整条失效
    for t in (None, 123, "", "   ", "\n\n"):
        out = normalize(rows, {"d": {"level": "ok", "text": t}})[0][0]
        assert out["reason"] == "invalid", t
        assert out["display"] == "—"


def test_state_text_normalized_then_checked():
    rows, _ = validate_rows([_row(key="d", label="决策", kind="state", stat=None)])
    # 控制字符折空格后成空白 → 整条无效（校验对象＝显示对象）
    assert normalize(rows, {"d": {"level": "ok", "text": "\x00\x01"}})[0][0]["reason"] == "invalid"
    # 折空格后非空白 → 保留
    out = normalize(rows, {"d": {"level": "ok", "text": "a\nb"}})[0][0]
    assert out["display"] == "a b" and out["reason"] is None
    # 超长截断
    long = normalize(rows, {"d": {"level": "ok", "text": "字" * 40}})[0][0]
    assert len(long["display"]) == MAX_TEXT_CHARS


def test_row_isolation_on_unexpected_error():
    rows, _ = validate_rows([_row(), _row(key="laps", label="圈数",
                                          kind="count", stat=None)])

    class Boom(float):
        def __float__(self):
            raise RuntimeError("boom")

    out, _ = normalize(rows, {"tick_rate": Boom(1.0), "laps": 7})
    assert out[0]["reason"] == "invalid"        # 坏行只坏自己
    assert out[1]["value"] == 7.0


def test_display_formatting():
    rows, _ = validate_rows([
        _row(precision=1),
        _row(key="r", label="比例", kind="ratio", stat=None, precision=0),
        _row(key="d", label="耗时", kind="duration", stat=None, precision=0),
    ])
    out, _ = normalize(rows, {"tick_rate": 12.44, "r": 0.234, "d": 87.6})
    assert out[0]["display"] == "12.4 次/秒"
    assert out[1]["display"] == "23%"
    assert out[2]["display"] == "88 ms"


def test_format_display_none():
    rows, _ = validate_rows([_row()])
    assert format_display(rows[0], None) == "—"


# ---------------------------------------------------------------- 版本与开销


def test_contract_version_mismatch_hides_domain(monkeypatch):
    """验收 1（超版本）：契约版本不认识 → 领域面整体不显示 + 一条 WARNING。

    不猜旧格式：版本不符说明两侧对「行长什么样」的理解可能已经分叉，猜着
    解析等于把格式错误渲染成看似正常的数字。
    """
    import maaracing_master.core.perf_contract as pc
    from maaracing_master.core import logger as lg

    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(lg.logger, "log",
                        lambda msg, level="INFO": logs.append((level, msg)))

    class Old:
        module_id = "oldmod"
        PERF_CONTRACT = 99
        PERF_ROWS = (_row(),)

    assert pc.rows_for(Old()) == []
    assert any(lv == "WARNING" and "99" in m for lv, m in logs)

    class Undeclared:
        module_id = "quiet"

    assert pc.rows_for(Undeclared()) == []
    assert len(logs) == 1, "未声明的模块不该告警（只显示资源面）"
