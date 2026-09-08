#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit 迁移器单测（纯标准库）。

测三件事（对应 §7.1 的三个接口）：

1. **v2 只读判定**：`schema_of` / `inspect_v2` 能把 v2 的五段结构、段级元数据、
   跨段同名、模板悬空与孤儿如实抽出来。
2. **缺口清单非空可查**：v2 里没有 `owner` / `page` / `kind` / `guarded_by` / `transitions`，
   迁移器必须把它们**逐条列出来给人过目**，绝不静默造默认值。
   清单非空是常态，空才是异常——这条是本文件的核心断言。
3. **diff_v2_v3 逐字段等价**：rect / threshold / templates 必须逐位相同。
   这是 §7.2"纯搬迁，改动与搬迁分开提交"的机器保证：搬迁提交若顺手改了数值，
   这里立刻现形，回归失败才能归因。

最后一条 `test_real_treasure_rois_migration_is_pure` 直接对**真实文件**
（`plugins/treasure/resources/config/treasure_rois.json`）跑一遍迁移 + diff，
断言"搬迁本身不产生任何数值差异"。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from maaracing_assistant.core.navkit import (
    SCHEMA_V3,
    V2_SEGMENTS,
    diff_v2_v3,
    inspect_v2,
    migrate_v2_to_v3,
    schema_of,
)

REAL_V2 = (
    Path(__file__).resolve().parents[1]
    / "archive"
    / "treasure_v2"
    / "treasure_rois.json"
)  # M4/E1：v2 已退役归档（只读历史对照）

MODULE = "treasure"

# 合成 v2：刻意造出跨段同名、无模板的 actions、段级元数据、悬空与孤儿模板。
V2: dict[str, Any] = {
    "_schema_ver": 2,
    "reference_size": [1280, 720],
    "stage": {
        "hall_card": {
            "rect": [0.1, 0.1, 0.3, 0.3],
            "templates": ["hall_card.png"],
            "threshold": 0.9,
        },
        "start_btn": {                      # 与 actions 段同名 → 跨段冲突
            "rect": [0.4, 0.4, 0.6, 0.6],
            "templates": ["start_btn.png"],
        },
    },
    "appraisers": {
        "_comment": "偏好鉴宝师",
        "appraiser_p1": {
            "prio": 1,
            "rect": [0.09, 0.26, 0.90, 0.72],
            "templates": ["p1.png"],
            "threshold": 0.8,
        },
    },
    "ocr": {
        "bid_amount": {"rect": [0.33, 0.65, 0.53, 0.72], "templates": []},
    },
    "eggs": {
        "_comment": "彩蛋识别",
        "_count_dx_norm": 0.03,
        "_count_dy_norm": 0,
        "_count_w_norm": 0.04,
        "_count_h_norm": 0.03,
        "egg": {
            "rect": [0.30, 0.34, 0.70, 0.56],
            "templates": ["egg.png"],
            "threshold": 0.72,
        },
    },
    "actions": {
        "confirm_btn": {                    # 有模板 → 可归 template
            "rect": [0.41, 0.77, 0.58, 0.85],
            "templates": ["confirm_btn.png"],
        },
        "start_btn": {                      # 无模板 → 归 point，必须有担保人
            "rect": [0.71, 0.77, 0.88, 0.84],
            "templates": [],
        },
        "numpad_1": {                       # 无模板 → 归 point
            "rect": [0.56, 0.51, 0.59, 0.56],
            "templates": [],
        },
        "missing_tpl_btn": {                # 引用了不存在的模板图
            "rect": [0.1, 0.1, 0.2, 0.2],
            "templates": ["ghost.png"],
        },
    },
}


# ------------------------------------------------------------------
# 辅助
# ------------------------------------------------------------------


def v2() -> dict[str, Any]:
    return copy.deepcopy(V2)


def minimal_semantic() -> dict[str, Any]:
    """只给模块名和阶段顺序——刻意不给 kind/owner/page/label/guarded_by，
    以便验证缺口清单会如实把它们列出来。"""
    return {
        "module": MODULE,
        "order": ["游戏大厅", "出价", "结算"],
        "pages": {"hall": {"label": "大厅"}, "bidding": {"label": "出价"}},
    }


def gap_kinds(gaps: list[str]) -> set[str]:
    return {g.split("]")[0].lstrip("[") for g in gaps}


def has_gap(gaps: list[str], kind: str, target: str) -> bool:
    return any(g.startswith(f"[{kind}] {target}") for g in gaps)


# ------------------------------------------------------------------
# v2 只读判定
# ------------------------------------------------------------------


def test_schema_of():
    assert schema_of(v2()) == 2
    assert schema_of({"_schema_ver": 3}) == 3
    assert schema_of({}) == 0                    # 缺失 → 0，不猜
    assert schema_of({"_schema_ver": "3"}) == 0  # 字符串 → 0，不猜


def test_inspect_v2_counts_every_segment():
    rep = inspect_v2(v2())
    assert rep.schema_ver == 2
    assert rep.reference_size == (1280, 720)
    # stage 2 + appraisers 1 + ocr 1 + eggs 1 + actions 4 = 9
    assert len(rep.items) == 9
    assert {seg: len(rep.items_in(seg)) for seg in V2_SEGMENTS} == {
        "stage": 2, "appraisers": 1, "ocr": 1, "eggs": 1, "actions": 4,
    }


def test_inspect_v2_skips_segment_metadata():
    """段级元数据（_comment / _count_*）不是 ROI，不得混进条目。"""
    rep = inspect_v2(v2())
    assert not any(i.key.startswith("_") for i in rep.items)
    assert "_comment" in rep.segment_meta["eggs"]
    assert "_count_dx_norm" in rep.segment_meta["eggs"]


def test_inspect_v2_detects_cross_segment_name_collision():
    rep = inspect_v2(v2())
    assert ("start_btn", ("stage", "actions")) in rep.name_collisions


def test_inspect_v2_detects_dangling_and_orphan(tmp_path):
    img = tmp_path / "image"
    img.mkdir()
    for name in ("hall_card.png", "start_btn.png", "p1.png", "egg.png", "confirm_btn.png"):
        (img / name).write_bytes(b"\x89PNG")
    (img / "unused.png").write_bytes(b"\x89PNG")   # 目录里有、配置里没引用

    rep = inspect_v2(v2(), image_dirs=(img,))
    assert rep.dangling_templates == ("ghost.png",)
    assert rep.orphan_templates == ("unused.png",)


def test_inspect_v2_summary_is_renderable():
    text = inspect_v2(v2()).summary()
    assert "schema_ver = 2" in text
    assert "跨段同名" in text


# ------------------------------------------------------------------
# 缺口清单
# ------------------------------------------------------------------


def test_migrate_gaps_are_non_empty_by_design():
    """核心断言：v2 缺的那些字段，一个都不许被静默补默认值。"""
    v3, gaps = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    assert gaps, "缺口清单不得为空——v2 没有 owner/page/kind/guarded_by/transitions"

    kinds = gap_kinds(gaps)
    # 五类"v2 里根本不存在、只能人来定"的信息
    assert "未确认/kind" in kinds
    assert "未确认/owner" in kinds
    assert "未确认/page" in kinds
    assert "未确认/label" in kinds
    assert "缺失/guarded_by" in kinds
    # 结构性缺口
    assert "冲突/同名" in kinds
    assert "缺失/match口径" in kinds
    assert "缺失/迁移边" in kinds


def test_migrate_point_targets_without_guard_are_reported():
    """actions 段里没有模板图的项归为 point，而 v2 里没有任何字段能证明它的画面归属。

    注意 `start_btn` 在 stage / actions 两段重名，actions 那份会被自动改名成
    `actions__start_btn`（v3 的 anchors 是扁平 map，不允许重名），缺口按改名后的
    id 报出——这正是需要人来决定"这两份到底是不是同一个东西"的地方。
    """
    v3, gaps = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    assert has_gap(gaps, "缺失/guarded_by", "anchors.actions__start_btn")
    assert has_gap(gaps, "缺失/guarded_by", "anchors.numpad_1")
    # 有模板图的 actions 项归 template，不该按 point 要担保
    assert not has_gap(gaps, "缺失/guarded_by", "anchors.confirm_btn")
    # stage 段那份保留原名（先到先得），它自带模板图所以不要担保
    assert "start_btn" in v3["anchors"]


def test_migrate_records_kind_inference_per_item():
    """推断出来的 kind 也要进清单，标记为「推断待确认」——推断不是事实。"""
    v3, gaps = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    inferred = [g for g in gaps if g.startswith("[未确认/kind]")]
    assert any("推断待确认" in g for g in inferred)
    # 推断结果本身按 §4.2 分类判据：ocr 段 → ocr；actions 无模板 → point；stage → template
    assert v3["anchors"]["bid_amount"]["kind"] == "ocr"
    assert v3["anchors"]["numpad_1"]["kind"] == "point"
    assert v3["anchors"]["hall_card"]["kind"] == "template"


def test_semantic_explicit_values_suppress_gaps():
    """人确认过的项不再进缺口——这样清单可以逐批消化、逐步变短。"""
    sem = minimal_semantic()
    sem["anchors"] = {
        "stage.hall_card": {
            "kind": "template", "owner": "global", "page": "hall", "label": "大厅卡片",
        },
    }
    sem["match"] = {"scales": [1.0], "threshold": 0.8}
    v3, gaps = migrate_v2_to_v3(v2(), semantic=sem)

    assert not has_gap(gaps, "未确认/kind", "v2.stage.hall_card")
    assert not has_gap(gaps, "未确认/owner", "v2.stage.hall_card")
    assert not has_gap(gaps, "未确认/page", "v2.stage.hall_card")
    assert not has_gap(gaps, "未确认/label", "v2.stage.hall_card")
    assert v3["anchors"]["hall_card"]["owner"] == "global"
    assert "缺失/match口径" not in gap_kinds(gaps)


def test_semantic_qualified_key_resolves_collision():
    """跨段同名必须靠 `段.键` 精确指定；只给裸键会被记为待拆分。"""
    sem = minimal_semantic()
    sem["anchors"] = {
        "stage.start_btn": {"kind": "template", "page": "hall", "label": "开始匹配按钮"},
        "actions.start_btn": {
            "kind": "point", "page": "hall", "label": "开始匹配点击区",
            "guarded_by": "start_btn", "rename": "start_btn_click",
        },
    }
    v3, gaps = migrate_v2_to_v3(v2(), semantic=sem)

    assert "start_btn" in v3["anchors"]           # stage 版保留原名
    assert "start_btn_click" in v3["anchors"]     # actions 版按 rename 分开
    assert v3["anchors"]["start_btn_click"]["guarded_by"] == "start_btn"
    assert not has_gap(gaps, "缺失/guarded_by", "anchors.start_btn_click")


def test_semantic_vague_key_on_collision_is_flagged():
    sem = minimal_semantic()
    sem["anchors"] = {"start_btn": {"kind": "template", "page": "hall", "label": "开始"}}
    v3, gaps = migrate_v2_to_v3(v2(), semantic=sem)
    assert has_gap(gaps, "冲突/同名", "semantic.anchors.start_btn")


def test_semantic_item_without_v2_counterpart_is_reported():
    """语义项在 v2 里找不到对应条目 = 常量与配置已经不同步。"""
    sem = minimal_semantic()
    sem["anchors"] = {"stage.ghost_key": {"kind": "template"}}
    v3, gaps = migrate_v2_to_v3(v2(), semantic=sem)
    assert has_gap(gaps, "未映射/语义项", "semantic.anchors.stage.ghost_key")


def test_migrate_attaches_egg_domain_params():
    """eggs 的 `_count_*` 是领域参数，navkit 不解释，原样透传到 domain 袋。"""
    v3, gaps = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    domain = v3["anchors"]["egg"]["domain"]
    assert domain["_count_dx_norm"] == 0.03
    assert domain["_count_w_norm"] == 0.04


def test_migrate_does_not_fabricate_defaults():
    """没有 semantic 时，page 等关键字段宁可留空，也不许编一个看起来合理的值。"""
    v3, gaps = migrate_v2_to_v3(v2(), semantic={"module": MODULE})
    assert v3["anchors"]["hall_card"]["page"] == ""
    assert has_gap(gaps, "未确认/page", "v2.stage.hall_card")


def test_migrate_output_is_v3_shaped():
    v3, gaps = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    assert v3["_schema_ver"] == SCHEMA_V3
    assert v3["_module"] == MODULE
    assert v3["reference_size"] == [1280, 720]
    assert set(v3) >= {"match", "pages", "anchors", "stages", "transitions", "routes"}


def test_migrate_reports_dangling_and_orphan_templates(tmp_path):
    img = tmp_path / "image"
    img.mkdir()
    for name in ("hall_card.png", "start_btn.png", "p1.png", "egg.png", "confirm_btn.png"):
        (img / name).write_bytes(b"\x89PNG")
    (img / "unused.png").write_bytes(b"\x89PNG")

    sem = minimal_semantic()
    sem["image_dirs"] = (img,)
    v3, gaps = migrate_v2_to_v3(v2(), semantic=sem)
    assert "悬空/模板" in gap_kinds(gaps)
    assert "孤儿/模板" in gap_kinds(gaps)
    assert any("ghost.png" in g for g in gaps)
    assert any("unused.png" in g for g in gaps)


def test_migrate_reports_unknown_references():
    """semantic 里引用了 v2 不存在的锚点 → 说明常量与配置不同步。"""
    sem = minimal_semantic()
    sem["anchors"] = {
        "actions.numpad_1": {
            "kind": "point", "page": "bidding", "label": "数字键1",
            "guarded_by": "不存在的面板标题",
        },
    }
    v3, gaps = migrate_v2_to_v3(v2(), semantic=sem)
    assert "未知引用" in gap_kinds(gaps)


# ------------------------------------------------------------------
# diff_v2_v3 逐字段等价
# ------------------------------------------------------------------


def test_diff_is_empty_for_pure_move():
    """纯搬迁：一个数值都不许变。"""
    v3, gaps = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    assert diff_v2_v3(v2(), v3) == []


def test_diff_detects_rect_change():
    v3, _ = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    v3["anchors"]["hall_card"]["rect"] = [0.2, 0.1, 0.3, 0.3]
    diffs = diff_v2_v3(v2(), v3)
    assert len(diffs) == 1 and "rect" in diffs[0]


def test_diff_detects_threshold_change():
    v3, _ = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    v3["anchors"]["hall_card"]["threshold"] = 0.75   # v2 是 0.9
    diffs = diff_v2_v3(v2(), v3)
    assert any("threshold" in d for d in diffs)


def test_diff_detects_threshold_added_or_removed():
    """v2 没给阈值就不许在 v3 里冒出一个，反之亦然——这是最容易顺手改坏的地方。"""
    v3, _ = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    del v3["anchors"]["hall_card"]["threshold"]       # v2 有 0.9，v3 没了
    assert any("threshold" in d for d in diff_v2_v3(v2(), v3))

    v3b, _ = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    v3b["anchors"]["numpad_1"]["threshold"] = 0.8     # v2 没有，v3 多了
    assert any("threshold" in d for d in diff_v2_v3(v2(), v3b))


def test_diff_detects_template_reorder():
    """模板列表顺序即优先级（round_big_banner 依赖它），乱序必须被抓到。"""
    v2doc = v2()
    v2doc["stage"]["hall_card"]["templates"] = ["a.png", "b.png"]
    v3, _ = migrate_v2_to_v3(v2doc, semantic=minimal_semantic())
    v3["anchors"]["hall_card"]["templates"] = ["b.png", "a.png"]
    assert any("templates" in d for d in diff_v2_v3(v2doc, v3))


def test_diff_detects_missing_anchor():
    v3, _ = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    del v3["anchors"]["numpad_1"]
    assert any("numpad_1" in d for d in diff_v2_v3(v2(), v3))


def test_diff_falls_back_to_id_matching_without_trace():
    """清洗掉 `_v2` 追溯字段后，非同名条目仍可按 id 回配；同名条目如实报"无法唯一匹配"。

    这条反向证明了追溯字段的必要性：v2 靠段名消歧，一旦丢掉来源信息，
    跨段同名就再也配不回去——所以 `_v2` 在人工复核完成前不能删。
    """
    v3, _ = migrate_v2_to_v3(v2(), semantic=minimal_semantic())
    for entry in v3["anchors"].values():
        entry.pop("_v2", None)
    diffs = diff_v2_v3(v2(), v3)
    assert diffs, "跨段同名条目应无法回配"
    assert all("start_btn" in d for d in diffs), f"除同名歧义外不应有差异：{diffs}"


# ------------------------------------------------------------------
# 真实文件（只读，不落盘）
# ------------------------------------------------------------------


@pytest.mark.skipif(not REAL_V2.exists(), reason=f"缺少真实 v2 文件：{REAL_V2}")
class TestRealTreasureRois:
    """对真实 `treasure_rois.json` 跑只读体检与纯搬迁校验。

    只读，不写任何文件——§7.2 第 1 条要求迁移草稿"不落运行时路径"。
    """

    def test_is_v2(self):
        import json

        doc = json.loads(REAL_V2.read_text(encoding="utf-8"))
        assert schema_of(doc) == 2

    def test_inspect_does_not_crash(self):
        import json

        doc = json.loads(REAL_V2.read_text(encoding="utf-8"))
        rep = inspect_v2(doc)
        assert rep.schema_ver == 2
        assert len(rep.items) > 30, f"真实配置条目数异常：{len(rep.items)}"
        assert rep.reference_size == (1280, 720)

    def test_migration_is_pure(self):
        """最关键的一条：真实配置的搬迁过程不改变任何 rect/threshold/templates。"""
        import json

        doc = json.loads(REAL_V2.read_text(encoding="utf-8"))
        v3, gaps = migrate_v2_to_v3(doc, semantic={"module": MODULE})
        diffs = diff_v2_v3(doc, v3)
        assert diffs == [], f"搬迁不应产生数值差异，实际：{diffs}"
        assert gaps, "真实配置必须产出非空缺口清单（v2 无 owner/page/kind/guarded_by）"
