# -*- coding: utf-8 -*-
"""日志去噪契约（2026-09-17 真机日志实测后收紧）。

背景（会话 20260917_113520，后台手柄方式，78,502 行）：**69% 的篇幅是逐帧重复**，
其中约一半是同一件事被三个层各说一遍——「大厅里那两个常驻锚点当然不匹配」：

| 产出层 | 行数 | 占比 |
|---|---|---|
| `template_match.find_template` 逐模板未命中（经 match_template_cs 转调） | 18,712 | 23.8% |
| `nav_graph.TemplateRecognizer` 逐节点聚合未命中 | 11,830 | 15.1% |
| `pipeline_logger` 识别未命中事件 | 5,915 | 7.5% |
| `pipeline_logger` 动作生命周期（Starting / 无结论 Succeeded） | 17,742 | 22.5% |

契约（本文件逐条锁）：
  1. **不带结果的行不记**：只说明「节点在跑」的生命周期行不产出；
  2. **稳态重复按边沿记**：未命中只在「进入未命中」时记一次，命中后清位可再记；
  3. **做跨模板仲裁的调用方不产生逐模板日志**（聚合层自己出带节点名与分数的日志）；
  4. **该记的一条都不能少**：动作失败仍是 WARNING、成功仍记、结果信息仍透出。
"""
from __future__ import annotations

import types

import pytest

try:
    from maa.event_sink import NotificationType

    from maaracing_master.core import nav_graph as nav_mod
    from maaracing_master.core import pipeline_logger as pl_mod
    from maaracing_master.core import template_match as tm_mod
    from maaracing_master.core.pipeline_logger import PipelineLogger
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"测试需要完整运行时依赖（maa/…）：{_ERR}"
)


class _Rec:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO", channel=None):  # noqa: ANN001 —— 与真 logger 同形
        self.lines.append((level, str(msg)))

    def msgs(self, level: str | None = None) -> list[str]:
        return [m for lv, m in self.lines if level is None or lv == level]


@pytest.fixture
def rec(monkeypatch):
    r = _Rec()
    for mod in (pl_mod, nav_mod, tm_mod):
        monkeypatch.setattr(mod, "logger", r)
    return r


def _node(name: str, **kw):
    return types.SimpleNamespace(name=name, **kw)


# ------------------------------------------------------------------
#  1 + 2：pipeline_logger —— 不带结果的不记，稳态重复按边沿记
# ------------------------------------------------------------------


def test_action_lifecycle_without_outcome_is_not_logged(rec):
    """只说明「节点在跑」的动作生命周期行不得产出（真机上占 22.5% 篇幅）。"""
    p = PipelineLogger()
    node = _node("treasure.policy_loop")
    for _ in range(50):                       # 真机每帧 3 条 × 12 分钟
        p.on_node_action(None, NotificationType.Starting, node)
        p.on_node_action(None, NotificationType.Succeeded, node)  # success=None
    assert rec.msgs() == [], "无结论的动作生命周期行必须零产出"


def test_action_failure_is_still_warning_and_success_still_logged(rec):
    """该记的不能少：动作失败是「可继续运行的降级」必须 WARNING，成功仍记。"""
    p = PipelineLogger()
    p.on_node_action(None, NotificationType.Succeeded,
                     _node("click_x", success=False))
    p.on_node_action(None, NotificationType.Succeeded,
                     _node("click_x", success=True))
    assert rec.msgs("WARNING") and "动作❌失败" in rec.msgs("WARNING")[0]
    assert any("动作✅成功" in m for m in rec.msgs("DEBUG"))


def test_recognition_miss_is_edge_triggered(rec):
    """未命中边沿触发：连续 N 帧只记 1 条；命中后再次未命中要重新记。"""
    p = PipelineLogger()
    node = _node("treasure.goto_appraise_btn")
    for _ in range(30):
        p.on_node_recognition(None, NotificationType.Failed, node)
    assert len(rec.msgs("DEBUG")) == 1, "稳态重复必须折叠为一条"

    p.on_node_recognition(None, NotificationType.Succeeded, _node(
        "treasure.goto_appraise_btn", hit=True))
    assert any("识别✅命中" in m for m in rec.msgs("DEBUG")), "命中仍要记（状态翻转）"

    p.on_node_recognition(None, NotificationType.Failed, node)
    assert len([m for m in rec.msgs("DEBUG") if "识别未命中" in m]) == 2, \
        "命中之后再次进入未命中必须重新记录"


def test_recognition_without_hit_detail_is_not_logged(rec):
    """有结果但没带 hit 细节（如 Custom 识别器驻留节点）：无结论信息，不记。"""
    p = PipelineLogger()
    for _ in range(20):
        p.on_node_recognition(None, NotificationType.Succeeded, _node("treasure.policy_loop"))
    assert rec.msgs() == []


# ------------------------------------------------------------------
#  3：逐模板日志归聚合层
# ------------------------------------------------------------------


def test_arbitrating_caller_gets_no_per_template_logs(rec):
    """`match_template_cs` 的契约是「调用方自己做阈值/仲裁」→ 不得逐模板打日志。"""
    import numpy as np
    # 独立噪声：常量图在 NCC 下会退化（全黑恒等匹配 1.0），必须用不相关纹理才有"真未命中"
    rng = np.random.default_rng(11)
    frame = rng.integers(0, 255, (60, 60, 3), dtype=np.uint8)
    tpl = rng.integers(0, 255, (10, 10, 3), dtype=np.uint8)
    box, val = tm_mod.match_template_cs(frame, tpl, colorspace="rgb", threshold=0.99)
    assert box is None, f"独立噪声不该命中，实际 val={val}"
    assert rec.msgs() == [], "仲裁路径不得产生逐模板未命中日志"


def test_threshold_decision_is_unchanged_by_silencing(rec):
    """消音只改日志，不改判定：阈值语义与返回框完全不变。"""
    import numpy as np
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 255, (80, 80, 3), dtype=np.uint8)
    tpl = frame[20:40, 20:40].copy()          # 用帧内子块作模板 → 必然命中
    box, val = tm_mod.match_template_cs(frame, tpl, colorspace="rgb", threshold=0.5)
    assert box is not None and val >= 0.99
    box_loud, val_loud = tm_mod.find_template(frame, tpl, threshold=0.5)
    assert box_loud == box and abs(val_loud - val) < 1e-9, "消音不得影响判定结果"


def test_self_judging_caller_still_gets_miss_log(rec):
    """自己判阈值的调用方（find_template 默认路径）仍保留逐模板日志。"""
    import numpy as np
    rng = np.random.default_rng(13)
    frame = rng.integers(0, 255, (40, 40, 3), dtype=np.uint8)
    tpl = rng.integers(0, 255, (8, 8, 3), dtype=np.uint8)   # 同上：不相关纹理
    tm_mod.find_template(frame, tpl, threshold=0.99)
    assert rec.msgs("DEBUG") and "模板未命中" in rec.msgs("DEBUG")[0]


# ------------------------------------------------------------------
#  2：v4 识别器聚合日志同样按边沿记
# ------------------------------------------------------------------


class _GraphStub:
    image_dirs: list = []


def test_nav_recognizer_miss_log_is_edge_triggered(rec, monkeypatch):
    """逐节点聚合未命中：连续帧只记一次；命中后清位可再记。"""
    import numpy as np

    recog = nav_mod.TemplateRecognizer(_GraphStub())
    frame = np.zeros((40, 40, 3), dtype=np.uint8)
    calls = {"n": 0}

    def _fake_any_cs(*_a, **_k):
        calls["n"] += 1
        return None, 0.31, ""

    monkeypatch.setattr(nav_mod, "find_any_cs", _fake_any_cs)
    monkeypatch.setattr(nav_mod, "_parse", lambda _p: {
        "mode": "template", "templates": ["t.png"], "rect": [0, 0, 1, 1],
    })
    argv = types.SimpleNamespace(
        node_name="treasure.goto_appraise_btn",
        custom_recognition_param="{}", image=None,
    )
    monkeypatch.setattr(recog._graph, "frame", lambda: frame, raising=False)

    for _ in range(25):
        recog.analyze(None, argv)
    assert calls["n"] == 25, "判定必须每帧照跑（消音只影响日志）"
    assert len(rec.msgs("DEBUG")) == 1, "稳态重复必须折叠为一条"

    # 命中一次 → 清位；再次未命中要重新记录
    monkeypatch.setattr(nav_mod, "find_any_cs",
                        lambda *_a, **_k: ((1, 1, 9, 9), 0.95, "t"))
    recog.analyze(None, argv)
    monkeypatch.setattr(nav_mod, "find_any_cs", _fake_any_cs)
    recog.analyze(None, argv)
    assert len([m for m in rec.msgs("DEBUG") if "识别未命中" in m]) == 2