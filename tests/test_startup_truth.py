# -*- coding: utf-8 -*-
"""启动链真源的静态闸（ADR-0018）。

看护四件事：
1. 真源本身能过 truth（引用闭合、rect 归一、分层红线）；
2. 汇聚与确认链的候选顺序：弹窗 → 登录 → 大厅，且四处前缀逐字一致
   （MaaFW 无节点继承，同一份前缀只能重复四遍，漏改一处就是静默丢一条弹窗）；
3. 终点节点有 `_end` 且被引用——成功条件必须是可审计的图结构；
4. 模板都能在 startup/image 里解析，且不与插件模板重名（不会遮蔽插件搜索）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.pipeline import truth as ct

REPO = Path(__file__).resolve().parents[1]
STARTUP_DIR = REPO / "maaracing_master" / "core" / "resources" / "startup"
PIPELINE = STARTUP_DIR / "pipeline.json"
IMAGE_DIR = STARTUP_DIR / "image"

POPUPS = [
    "startup.popup.claim_daily",
    "startup.popup.claim_signin",
    "startup.popup.continue",
    "startup.popup.close_reward",
    "startup.popup.close_marketing",
]
LOGIN = "startup.login.engine_start"
CONFIRMS = ["startup.hall.confirm.1", "startup.hall.confirm.2", "startup.hall.confirm.3"]
ANCHORS = ["startup.anchor.hall_race", "startup.anchor.hall_manager"]


def _graph() -> tuple[dict, dict]:
    return ct.load_graph()


def _startup_nodes(graph: dict, origin: dict) -> dict[str, dict]:
    return {n: d for n, d in graph.items() if STARTUP_DIR in origin[n].parents}


def _templates(node: dict) -> list[str]:
    params = node["recognition"]["param"]
    return list((params.get("custom_recognition_param") or {}).get("templates") or [])


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_truth_passes_checker():
    graph, origin = _graph()
    errors, warns = ct.validate_graph(graph)
    errors += ct.action_checks(graph)
    errors += ct.namespace_checks(graph, origin)
    errors += ct.rect_checks(graph)
    assert errors == []
    assert warns == []


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_nodes_are_startup_scoped_and_complete():
    graph, origin = _graph()
    nodes = _startup_nodes(graph, origin)
    assert nodes, "启动链节点未被 truth 发现（发现范围失效？）"
    assert all(n.startswith("startup.") for n in nodes)


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_boot_order_popups_then_login_then_hall():
    graph, _ = _graph()
    nxt = graph["startup.boot"]["next"]
    idx = {name: i for i, name in enumerate(nxt)}
    for p in POPUPS:
        assert p in idx, f"汇聚节点漏了弹窗 {p}"
    assert LOGIN in idx
    assert CONFIRMS[0] in idx
    # 弹窗必须排在登录与大厅之前：弹窗叠在大厅上时大厅锚点仍可能命中
    assert max(idx[p] for p in POPUPS) < idx[LOGIN] < idx[CONFIRMS[0]]


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_confirm_window_keeps_popup_priority():
    """三个确认节点的 next 前缀必须与汇聚节点逐字一致，且各自接到下一个确认。

    这是「确认窗口内弹窗优先规则持续生效」的静态保证：确认链不是只认下一个
    确认节点，弹窗与登录页始终在候选里。
    """
    graph, _ = _graph()
    head = POPUPS + [LOGIN]
    nxt = graph["startup.boot"]["next"]
    assert nxt[:len(head)] == head

    for i, name in enumerate(CONFIRMS):
        seq = graph[name]["next"]
        assert seq[:len(head)] == head, f"{name} 的候选前缀与汇聚节点不一致: {seq[:len(head)]}"
        target = CONFIRMS[i + 1] if i + 1 < len(CONFIRMS) else "startup.done"
        assert seq[-1] == target, f"{name} 的最后一个候选应是 {target}，实际 {seq[-1]}"
        assert graph[name]["on_error"] == ["startup.boot"]


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_end_node_is_referenced_and_marked():
    graph, _ = _graph()
    done = graph["startup.done"]
    assert done.get("attach", {}).get("_end") is True
    assert not done.get("next") and not done.get("on_error")
    assert CONFIRMS[-1] in [
        n for n in graph if "startup.done" in (graph[n].get("next") or [])
    ]


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_every_node_declares_timing_explicitly():
    """默认值（timeout 20s / delay 200ms / rate_limit 1s）会让链路慢一个数量级，
    所以每个节点都显式写全，防止后续编辑悄悄回落到默认。"""
    graph, origin = _graph()
    for name, node in _startup_nodes(graph, origin).items():
        for key in ("pre_delay", "post_delay", "timeout", "rate_limit"):
            assert key in node, f"{name} 缺 {key}（会静默回落到协议默认值）"


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_templates_resolve_and_do_not_shadow_plugins():
    graph, origin = _graph()
    plugin_images = {p.name for d in ct.PLUGIN_PIPELINE_DIRS
                     for p in (d.parent / "image").glob("*.png")}
    for name, node in _startup_nodes(graph, origin).items():
        for tpl in _templates(node):
            assert (IMAGE_DIR / tpl).is_file(), f"{name} 引用的模板不存在: {tpl}"
            assert tpl.startswith("startup_"), f"启动链模板必须带 startup_ 前缀: {tpl}"
            assert tpl not in plugin_images, f"启动链模板与插件模板重名: {tpl}"


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_hall_anchor_threshold_leaves_room_over_popup_frames():
    """实测：干净大厅帧 0.99、弹窗帧最高 0.83。阈值 0.90 才有分离度。"""
    graph, _ = _graph()
    for name in ANCHORS:
        th = graph[name]["recognition"]["param"]["custom_recognition_param"]["threshold"]
        assert th >= 0.9, f"{name} 阈值 {th} 低于弹窗帧的最高分，会假命中"


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_login_uses_gamepad_confirm():
    graph, _ = _graph()
    action = graph[LOGIN]["action"]["param"]
    assert action["custom_action"] == "MaaRM_Click"
    assert action["custom_action_param"]["gamepad_button"] == "Y"


@pytest.mark.skipif(not PIPELINE.is_file(), reason="启动链真源不存在")
def test_pipeline_is_valid_json_object():
    data = json.loads(PIPELINE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data
