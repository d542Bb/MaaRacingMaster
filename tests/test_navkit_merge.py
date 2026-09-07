# -*- coding: utf-8 -*-
"""Assets.merge 跨文档契约（G2）与锚点规范化签名（D2）单测。

merge 契约（§〇 I-1/I-3/I-4 + G0 断言）：
- 单向可见、同名保留模块版、pages 并集模块优先、global_anchors 同步并入；
- global 锚点入 detect_anchors 且 stage_priority 与同 order 模块锚点等值（不加权）；
- owner/_module 判定输入不变。

签名契约（D2）：影响匹配结果的字段变更 → 签名变；注释/label/order/量化精度内
的 rect 抖动 → 签名不变；threshold/scales 取继承后有效值。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from maaracing_assistant.core.navkit import (
    Assets,
    anchor_signature,
    compile_detection,
    validate_assets,
)
from tools.navkit import compile_routes as cr

MODULE = "demo"

_GLOBAL_DOC: dict[str, Any] = {
    "_schema_ver": 3,
    "_module": "global",
    "reference_size": [1280, 720],
    "match": {"scales": [1.0], "threshold": 0.75, "margin_default": 0.01},
    "pages": {"hall": {"label": "游戏大厅"}, "shared": {"label": "global 版"}},
    "anchors": {
        "hall_race_btn": {
            "kind": "template", "owner": "global", "page": "hall",
            "label": "比赛按钮", "rect": [0.1, 0.1, 0.3, 0.3],
            "templates": ["hall_race_btn.png"], "order": 10,
        },
    },
    "stages": {"order": [], "global_anchors": ["hall_race_btn"]},
    "routes": {},
}

_MODULE_DOC: dict[str, Any] = {
    "_schema_ver": 3,
    "_module": MODULE,
    "reference_size": [1280, 720],
    "match": {"scales": [1.0], "threshold": 0.75, "margin_default": 0.01},
    "pages": {"activity": {"label": "活动页"}, "shared": {"label": "模块版"}},
    "anchors": {
        "entry_card": {
            "kind": "template", "owner": MODULE, "page": "activity",
            "label": "入口卡", "rect": [0.4, 0.4, 0.6, 0.6],
            "templates": ["entry_card.png"], "order": 10,
        },
    },
    "stages": {"order": ["活动页"], "global_anchors": []},
    "routes": {},
}


def _global() -> Assets:
    return Assets.from_document(copy.deepcopy(_GLOBAL_DOC), module="global")


def _module() -> Assets:
    return Assets.from_document(copy.deepcopy(_MODULE_DOC), module=MODULE)


# ------------------------------------------------------------------
# merge 契约
# ------------------------------------------------------------------


def test_merge_single_direction_and_immutability():
    g, m = _global(), _module()
    merged = m.merge(g)
    assert set(merged.anchors) == {"hall_race_btn", "entry_card"}
    assert merged.module == MODULE
    # 源对象不被污染（返回新实例）
    assert set(g.anchors) == {"hall_race_btn"}
    assert set(m.anchors) == {"entry_card"}
    assert m.global_anchors == ()


def test_merge_owner_and_module_preserved():
    """I-4：owner 保持文档原值、_module 仍是模块名（W04/E02 判定输入不变）。"""
    merged = _module().merge(_global())
    assert merged.anchors["hall_race_btn"].owner == "global"
    assert merged.anchors["entry_card"].owner == MODULE
    assert merged.declared_module == MODULE
    assert merged.module == MODULE


def test_merge_global_anchors_synced():
    """I-1：global 锚点 id 必须并入 stages.global_anchors，
    否则 compile_detection 的入集条件收不到它（引用了等于没引用）。"""
    merged = _module().merge(_global())
    assert merged.global_anchors == ("hall_race_btn",)


def test_merge_global_anchor_enters_detect_anchors():
    """G0 断言①：合并后 global 模板锚点 ∈ detect_anchors ∩ plan.global_anchors。"""
    plan = compile_detection(_module().merge(_global()))
    assert "hall_race_btn" in plan.detect_anchors
    assert "hall_race_btn" in plan.global_anchors


def test_merge_priority_no_boost():
    """G0 断言②：全局不加权——同 order 的 global 与模块锚点 stage_priority 等值。"""
    plan = compile_detection(_module().merge(_global()))
    g_prio = plan.spec["hall_race_btn"].stage_priority
    m_prio = plan.spec["entry_card"].stage_priority
    assert g_prio == m_prio == 1000 - 10


def test_merge_module_wins_on_same_id():
    m_doc = copy.deepcopy(_MODULE_DOC)
    m_doc["anchors"]["hall_race_btn"] = {
        "kind": "template", "owner": MODULE, "page": "activity",
        "label": "模块覆盖版", "rect": [0.7, 0.7, 0.9, 0.9],
        "templates": ["override.png"], "_override": True,
    }
    merged = Assets.from_document(m_doc, module=MODULE).merge(_global())
    assert merged.anchors["hall_race_btn"].label == "模块覆盖版"
    assert merged.anchors["hall_race_btn"].owner == MODULE


def test_merge_pages_union_module_wins():
    merged = _module().merge(_global())
    assert set(merged.pages) == {"hall", "activity", "shared"}
    assert merged.pages["shared"]["label"] == "模块版"


def test_merge_module_side_only_sections():
    merged = _module().merge(_global())
    assert dict(merged.routes) == dict(_module().routes)
    assert tuple(merged.stage_order) == tuple(_module().stage_order)


def test_validate_cross_doc_closure_via_merge():
    """跨文档引用闭合：模块 transitions 引用 global 锚点——
    未合并校验报 E12（目标悬空），合并后闭合且不引入新错误。"""
    m_doc = copy.deepcopy(_MODULE_DOC)
    m_doc["transitions"] = [{"stage": "*", "on": "hall_race_btn", "to": "活动页"}]
    m = Assets.from_document(m_doc, module=MODULE)

    unmerged = validate_assets(m)
    assert "E12" in {i.code for i in unmerged.errors}

    merged = validate_assets(m.merge(_global()))
    merged_errs = {i.code for i in merged.errors}
    assert not merged_errs & {"E11", "E12"}


def test_merge_rejects_non_global_argument():
    with pytest.raises(ValueError, match="owner=global"):
        _module().merge(_module())


def test_merge_rejects_global_self_merge():
    with pytest.raises(ValueError, match="单向可见"):
        _global().merge(_global())


def test_real_global_doc_validates_clean():
    """global 校验剖面（§五）：真实 global 文档 0 error 0 warning——
    E15（order 非空）对 global 豁免；W03 不触发（global_anchors 非空）；
    W01/W02 模板引用全闭合（真实 core 目录）。"""
    p = (
        Path(__file__).resolve().parents[1]
        / "maaracing_assistant" / "core" / "resources" / "config" / "global_assets.json"
    )
    assets = Assets.load(p, module="global")
    report = validate_assets(assets)
    assert report.ok, [f"{i.code}: {i.message}" for i in report.issues]


# ------------------------------------------------------------------
# 锚点规范化签名（D2）
# ------------------------------------------------------------------


def test_signature_is_stable():
    m = _module()
    assert anchor_signature(m, "entry_card") == anchor_signature(m, "entry_card")


def test_signature_changes_with_matching_fields():
    m = _module()
    base = anchor_signature(m, "entry_card")

    changed = copy.deepcopy(_MODULE_DOC)
    changed["anchors"]["entry_card"]["threshold"] = 0.6
    assert anchor_signature(Assets.from_document(changed, module=MODULE),
                            "entry_card") != base

    changed = copy.deepcopy(_MODULE_DOC)
    changed["anchors"]["entry_card"].setdefault(
        "arbitration", {"margin": 0.0, "round_from_template": False}
    )["template_thresholds"] = {"entry_card.png": 0.6}
    assert anchor_signature(Assets.from_document(changed, module=MODULE),
                            "entry_card") != base

    changed = copy.deepcopy(_MODULE_DOC)
    changed["anchors"]["entry_card"]["scales"] = [0.9, 1.0]
    assert anchor_signature(Assets.from_document(changed, module=MODULE),
                            "entry_card") != base

    changed = copy.deepcopy(_MODULE_DOC)
    changed["anchors"]["entry_card"]["rect"] = [0.401, 0.4, 0.6, 0.6]
    assert anchor_signature(Assets.from_document(changed, module=MODULE),
                            "entry_card") != base


def test_signature_stable_across_non_matching_fields():
    """注释/label/order/第 5 位小数的 rect 抖动不影响匹配行为，签名不变。"""
    m = _module()
    base = anchor_signature(m, "entry_card")

    changed = copy.deepcopy(_MODULE_DOC)
    changed["anchors"]["entry_card"]["label"] = "改个显示名"
    changed["anchors"]["entry_card"]["order"] = 99
    changed["anchors"]["entry_card"]["comment"] = "格式化说明"
    changed["anchors"]["entry_card"]["rect"] = [0.40001, 0.4, 0.6, 0.6]
    assert anchor_signature(Assets.from_document(changed, module=MODULE),
                            "entry_card") == base


def test_signature_threshold_scales_use_effective_values():
    """threshold/scales 取继承后有效值：锚点未声明时落 match 唯一口径，
    match 口径变更 → 有效值变更 → 签名必须跟着变。"""
    m = _module()
    assert m.anchors["entry_card"].threshold is None
    base = anchor_signature(m, "entry_card")

    changed = copy.deepcopy(_MODULE_DOC)
    changed["match"]["threshold"] = 0.8
    assert anchor_signature(Assets.from_document(changed, module=MODULE),
                            "entry_card") != base


# ------------------------------------------------------------------
# 路由侧 merge 通电（G3）：compile_one 编译前先并入 global
# ------------------------------------------------------------------


def _write_route_doc_referencing_global(cfg_dir: Path) -> None:
    """demo 模块：入口路由点击 global 的 hall_race_btn、confirm 自有 entry_card。
    锚点 hall_race_btn 不在模块内，只有编译前 merge global 才能解析。"""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "_schema_ver": 3,
        "_module": MODULE,
        "reference_size": [1280, 720],
        "match": {"scales": [1.0], "threshold": 0.75, "margin_default": 0.01},
        "pages": {"activity": {"label": "活动页"}},
        "anchors": {
            "entry_card": {
                "kind": "template", "owner": MODULE, "page": "activity",
                "label": "入口卡", "rect": [0.4, 0.4, 0.6, 0.6],
                "templates": ["entry_card.png"], "order": 10,
            },
        },
        "stages": {"order": ["活动页"], "global_anchors": []},
        "transitions": [],
        "routes": {
            "to_detail": {
                "start_stage": "活动页",
                "steps": [
                    {"target": "hall_race_btn", "action": "click",
                     "confirm": "entry_card", "timeout_ms": 45000, "rate_limit_ms": 600},
                ],
            }
        },
    }
    (cfg_dir / f"{MODULE}_assets.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def _demo_plugins(tmp_path, monkeypatch):
    """把 compile_routes 的路径常量指向 tmp：PLUGINS/（demo 模块）+ GLOBAL_ASSETS（真实样例）。"""
    plugins = tmp_path / "plugins"
    cfg = plugins / MODULE / "resources" / "config"
    _write_route_doc_referencing_global(cfg)
    g = tmp_path / "global_assets.json"
    g.write_text(json.dumps(copy.deepcopy(_GLOBAL_DOC), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cr, "PLUGINS", plugins)
    monkeypatch.setattr(cr, "GLOBAL_ASSETS", g)
    monkeypatch.setattr(cr, "CORE_RES", tmp_path / "core_res")
    return cfg


def test_compile_one_resolves_global_anchor_via_route_side_merge(_demo_plugins):
    """通电证明：路由引用 global 锚点，经 compile_one（内部先 merge）编译成功，
    产物里对 hall_race_btn 的识别参数被正确解析（模板名来自 global 段）。"""
    assert cr.compile_one(MODULE, check=False) == 0
    out = (
        cr.PLUGINS / MODULE / "resources" / "generated" / "pipeline" / f"{MODULE}_routes.json"
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    hall_nodes = [
        node for node in data.values()
        if isinstance(node, dict)
        and node.get("custom_recognition_param", {}).get("templates") == ["hall_race_btn.png"]
    ]
    assert hall_nodes, "hall_race_btn 锚点应经 merge 后进入编译产物"


def test_compile_one_fails_without_global_when_route_references_it(_demo_plugins, monkeypatch):
    """反向：global 文件缺失（过渡期）→ 未合并 → hall_race_btn 悬空，编译按预期报错，
    说明正是路由侧 merge 让该引用可解析，而非默默编出缺锚点的产物。"""
    monkeypatch.setattr(cr, "GLOBAL_ASSETS", monkeypatch_tmp_missing := (_demo_plugins.parent.parent / "absent_global.json"))
    with pytest.raises(KeyError):
        cr.compile_one(MODULE, check=False)


# ------------------------------------------------------------------
# 检测侧不变量守卫（G3）：鉴宝 DetectionPlan 绝不并入 global
# ------------------------------------------------------------------

_TREASURE_ASSETS = (
    Path(__file__).resolve().parents[1]
    / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "config"
    / "treasure_assets.json"
)
_GLOBAL_ASSETS = (
    Path(__file__).resolve().parents[1]
    / "maaracing_assistant" / "core" / "resources" / "config" / "global_assets.json"
)


def test_treasure_detection_excludes_global_anchors():
    """鉴宝检测真源保持独占：detector 用 `Assets.load(treasure)`（不 merge）编译，
    detect_anchors 与 global 锚点零交集——大厅锚点无 order→priority 1000，一旦并入
    会在局内帧抢先短路、破坏 v2/v3 逐帧等价回归。本测试锁死该契约（贴 MAA：入口识别
    属导航段，不进每帧检测环）。同时用"若误 merge 则必污染"反证本守卫有意义。"""
    g = Assets.load(_GLOBAL_ASSETS, module="global")
    global_ids = set(g.anchor_ids)

    treasure = Assets.load(_TREASURE_ASSETS, module="treasure")
    unmerged_detect = set(compile_detection(treasure).detect_anchors)
    assert unmerged_detect & global_ids == set(), "鉴宝检测真源不得含任何 global 锚点"

    # 反证：若误把 global 并入检测，hall 锚点确实会漏进 detect_anchors → 上断言才在守护真实风险
    merged_detect = set(compile_detection(treasure.merge(g)).detect_anchors)
    assert merged_detect & global_ids, "merge 后 global 锚点应进入 detect_anchors（否则守卫形同虚设）"
