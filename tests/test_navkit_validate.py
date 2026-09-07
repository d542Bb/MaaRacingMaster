#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navkit 校验器单测（纯标准库，CI 只装 pytest 即可运行）。

覆盖 docs/NAVKIT_PLAN.md §3.3 的规则表 E01-E20 / W01-W07 **各一正一反**：

- 正例：基线文档 `BASE` 是一份刻意做到"零 error、零 warning"的最小合法 v3 资产，
  `test_baseline_is_clean` 守住它。任何规则被误改成"永远报错"，这条会先红。
- 反例：从基线出发破坏单个字段，断言**恰好**报出预期编号，不多不少——
  "不多"很重要，否则规则之间互相串味（改一个字段炸五条）时看不出来。

另有一条静态守卫 `test_navkit_imports_are_stdlib_only`：用 AST 扫描 navkit 全部源文件，
禁止出现任何非标准库 import。这是"navkit 必须是纯标准库"这条红线的机器保证——
比"运行时没崩"可靠得多，因为它不依赖当前环境装了什么。
"""
from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from maaracing_assistant.core import navkit
from maaracing_assistant.core.navkit import (
    Assets,
    NavKitError,
    NavKitValidationError,
    assert_valid,
    safe_load,
    validate_assets,
    validate_compiled,
    validate_merged,
)

MODULE = "demo"

# 纸上的两条具体边（通配边 ("*","hall_card") 不参与互查，见 _check_code_edges 注释）
CODE_EDGES = {("大厅", "hall_card"), ("出价", "panel_title")}

# 基线：一份刻意做到零 error、零 warning 的最小合法 v3 资产。
BASE: dict[str, Any] = {
    "_schema_ver": 3,
    "_module": MODULE,
    "reference_size": [1280, 720],
    "match": {"scales": [1.0], "threshold": 0.8, "margin_default": 0.0},
    "pages": {
        "hall": {"label": "大厅"},
        "bidding": {"label": "出价面板"},
        "result": {"label": "结算"},
    },
    "anchors": {
        "hall_card": {
            "kind": "template", "owner": MODULE, "page": "hall", "label": "大厅卡片",
            "rect": [0.1, 0.1, 0.3, 0.3], "templates": ["hall_card.png"], "order": 1,
        },
        "panel_title": {
            "kind": "template", "owner": MODULE, "page": "bidding", "label": "面板标题",
            "rect": [0.0, 0.0, 0.5, 0.2], "templates": ["panel_title.png"], "order": 1,
        },
        "bid_btn": {
            "kind": "point", "owner": MODULE, "page": "bidding", "label": "出价按钮",
            "rect": [0.4, 0.4, 0.6, 0.6], "guarded_by": "panel_title", "order": 2,
        },
        "ocr_value": {
            "kind": "ocr", "owner": MODULE, "page": "bidding", "label": "报价读数",
            "rect": [0.1, 0.1, 0.2, 0.2], "order": 3,
        },
        "result_banner": {
            "kind": "template", "owner": MODULE, "page": "result", "label": "结果横幅",
            "rect": [0.2, 0.2, 0.8, 0.5], "templates": ["result_banner.png"], "order": 1,
        },
    },
    "stages": {
        "order": ["大厅", "出价", "结算"],
        "global_anchors": ["hall_card"],
        "definitions": {
            "大厅": {"page": "hall", "anchors": ["hall_card"], "ocr": []},
            "出价": {
                "page": "bidding",
                "anchors": ["bid_btn", "panel_title"],
                "ocr": ["ocr_value"],
                "dynamic_narrow": {"by": "code:_active_stage_rois"},
            },
            "结算": {"page": "result", "anchors": ["result_banner"], "ocr": []},
        },
    },
    "transitions": [
        {"stage": "大厅", "on": "hall_card", "to": "出价"},
        {"stage": "出价", "on": "panel_title", "to": "same"},
        {"stage": "*", "on": "hall_card", "to": "大厅"},
    ],
    "routes": {
        "大厅→出价": {
            "entry": True,
            "start_stage": "大厅",
            "steps": [
                {
                    "target": "hall_card",
                    "action": "click",
                    "confirm": "panel_title",
                    "timeout_ms": 45000,
                }
            ],
        },
    },
}


# ------------------------------------------------------------------
# 辅助
# ------------------------------------------------------------------


def doc() -> dict[str, Any]:
    """基线文档的深拷贝（每个测试独立，避免相互污染）。"""
    return copy.deepcopy(BASE)


def build(
    d: dict[str, Any] | None = None, *, image_dirs: tuple[Path, ...] = ()
) -> Assets:
    return Assets.from_document(
        d if d is not None else doc(), module=MODULE, image_dirs=image_dirs
    )


def err_codes(report) -> set[str]:
    return {i.code for i in report.errors}


def warn_codes(report) -> set[str]:
    return {i.code for i in report.warnings}


def validate(
    d: dict[str, Any] | None = None,
    *,
    code_edges=None,
    global_assets=None,
    image_dirs: tuple[Path, ...] = (),
):
    """校验一份（可能被改坏的）基线文档。

    `image_dirs` 走 `Assets` 构造，`code_edges` / `global_assets` 走校验器——
    两者是不同阶段的参数，不要混着传。
    """
    return validate_assets(
        build(d, image_dirs=image_dirs),
        code_edges=code_edges,
        global_assets=global_assets,
    )


# ------------------------------------------------------------------
# 基线
# ------------------------------------------------------------------


def test_baseline_is_clean():
    """基线必须零 error 零 warning——它是所有反例的对照组。"""
    report = validate(code_edges=CODE_EDGES)
    assert report.ok, report.text()
    assert report.warnings == (), report.text()


def test_navkit_imports_are_stdlib_only():
    """静态守卫：navkit 全部源码不得出现非标准库 import。

    用 AST 而非"跑一遍没崩"来判定，因为后者依赖当前环境装了什么：
    CI 只装 pytest，而开发机装了 cv2/maa，运行时检查在开发机上永远绿、在 CI 上才红。
    """
    navkit_dir = Path(navkit.__file__).resolve().parent
    sources = sorted(navkit_dir.glob("*.py"))
    assert sources, f"未找到 navkit 源码：{navkit_dir}"

    banned = {
        "cv2", "numpy", "maa", "vgamepad", "onnxruntime", "rapidocr",
        "requests", "PIL", "torch", "shapely", "pyautogui",
    }
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 相对导入（如 from ..roi_config），包内引用，安全
                    continue
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                assert root not in banned, f"{path.name} 引入了重型依赖 {root!r}"
                assert root in sys.stdlib_module_names, (
                    f"{path.name} 引入了非标准库 {root!r}"
                )


# ------------------------------------------------------------------
# 构造期即抛的规则：E01 / E03 / E04 / E06
# ------------------------------------------------------------------


def test_e01_version_gate():
    d = doc()
    d["_schema_ver"] = 2
    with pytest.raises(NavKitError) as exc:
        build(d)
    assert exc.value.code == "E01"


def test_e01_missing_version():
    d = doc()
    del d["_schema_ver"]
    with pytest.raises(NavKitError) as exc:
        build(d)
    assert exc.value.code == "E01"


def test_e03_reference_size_must_be_positive_pair():
    for bad in ([0, 720], [1280], [1280, 720, 1], "1280x720"):
        d = doc()
        d["reference_size"] = bad
        with pytest.raises(NavKitError) as exc:
            build(d)
        assert exc.value.code == "E03", f"{bad!r} 未报 E03"


@pytest.mark.parametrize(
    "bad_match",
    [
        {"scales": [], "threshold": 0.8},
        {"scales": [1.0, 0.0], "threshold": 0.8},          # 含非正数
        {"scales": [1.0], "threshold": 0.0},               # 不在 (0,1]
        {"scales": [1.0], "threshold": 1.5},
        {"scales": "1.0", "threshold": 0.8},               # 非数组
    ],
)
def test_e04_match_policy(bad_match):
    d = doc()
    d["match"] = bad_match
    with pytest.raises(NavKitError) as exc:
        build(d)
    assert exc.value.code == "E04"


def test_e04_match_is_required():
    d = doc()
    del d["match"]
    with pytest.raises(NavKitError) as exc:
        build(d)
    assert exc.value.code == "E04"


@pytest.mark.parametrize(
    "bad_rect",
    [
        [0.5, 0.1, 0.3, 0.3],   # x1 > x2
        [0.1, 0.9, 0.3, 0.2],   # y1 > y2
        [0.1, 0.1, 1.5, 0.3],   # 越出 [0,1]
        [-0.1, 0.1, 0.3, 0.3],  # 负数
        [0.1, 0.1, 0.3],        # 长度不足
        [0.1, 0.1, 0.1, 0.3],   # 退化（x1 == x2）
    ],
)
def test_e06_rect_contract(bad_rect):
    """E06 与 NormalizedROI 同规则：越界在构造期抛，绝不静默 clamp。"""
    d = doc()
    d["anchors"]["hall_card"]["rect"] = bad_rect
    with pytest.raises(NavKitError) as exc:
        build(d)
    assert exc.value.code == "E06"


# ------------------------------------------------------------------
# 语义规则
# ------------------------------------------------------------------


def test_e02_module_mismatch():
    d = doc()
    d["_module"] = "racing"
    assert err_codes(validate(d)) == {"E02"}


def test_e02_missing_module_declaration():
    d = doc()
    del d["_module"]
    assert err_codes(validate(d)) == {"E02"}


def test_e05_unknown_anchor_kind():
    # 改 `ocr_value` 而非 `hall_card`：后者被 transitions 引用，改坏会连带触发 E12，
    # 那样就验不出"E05 单独成立"了。
    d = doc()
    d["anchors"]["ocr_value"]["kind"] = "magic"
    assert err_codes(validate(d)) == {"E05"}


@pytest.mark.parametrize(
    "bad_templates",
    [
        [],                        # kind=template 但模板为空
        ["../escape.png"],         # 含 ..
        ["sub/dir.png"],           # 含路径分隔符
        ["hall_card.bmp"],         # 扩展名不合法
    ],
)
def test_e07_template_name(bad_templates):
    d = doc()
    d["anchors"]["hall_card"]["templates"] = bad_templates
    assert err_codes(validate(d)) == {"E07"}


def test_e07_valid_suffixes_accepted():
    for name in ("a.png", "a.jpg", "a.jpeg", "a.PNG"):
        d = doc()
        d["anchors"]["hall_card"]["templates"] = [name]
        # 模板文件不存在只触发 W02（需注入 image_dirs），此处未注入 → 无任何问题
        assert err_codes(validate(d)) == set()


def test_e08_owner_must_be_global_or_own_module():
    d = doc()
    d["anchors"]["hall_card"]["owner"] = "racing"
    assert err_codes(validate(d)) == {"E08"}


def test_e08_global_owner_is_legal():
    d = doc()
    d["anchors"]["hall_card"]["owner"] = "global"
    assert err_codes(validate(d)) == set()


def test_e09_undefined_page():
    d = doc()
    d["anchors"]["hall_card"]["page"] = "nowhere"
    assert err_codes(validate(d)) == {"E09"}


def test_e10_point_requires_guard():
    d = doc()
    del d["anchors"]["bid_btn"]["guarded_by"]
    assert err_codes(validate(d)) == {"E10"}


@pytest.mark.parametrize(
    "mutate",
    [
        # 引用闭合：四类引用各自断一次
        lambda d: d["stages"]["global_anchors"].append("nope"),
        lambda d: d["stages"]["definitions"]["出价"]["anchors"].append("nope"),
        lambda d: d["stages"]["definitions"]["出价"]["ocr"].append("nope"),
        lambda d: d["anchors"]["bid_btn"].update(guarded_by="nope"),
        lambda d: d["transitions"].__setitem__(0, {"stage": "大厅", "on": "nope", "to": "出价"}),
        lambda d: d["routes"]["大厅→出价"]["steps"][0].update(target="nope"),
        lambda d: d["routes"]["大厅→出价"]["steps"][0].update(confirm="nope"),
    ],
)
def test_e12_dangling_reference(mutate):
    d = doc()
    mutate(d)
    assert err_codes(validate(d)) == {"E12"}


def test_e12_transition_signal_must_be_template():
    d = doc()
    d["transitions"].append({"stage": "出价", "on": "ocr_value", "to": "结算"})
    assert err_codes(validate(d)) == {"E12"}


def test_e13_guardian_must_be_template():
    """面板内件不能由另一个面板内件担保——担保链必须以模板锚点收口。"""
    d = doc()
    d["anchors"]["bid_btn"]["guarded_by"] = "ocr_value"
    assert err_codes(validate(d)) == {"E13"}


def test_e14_ghost_stage():
    d = doc()
    d["stages"]["definitions"]["幽灵阶段"] = {"anchors": ["hall_card"]}
    assert err_codes(validate(d)) == {"E14"}


def test_e15_order_must_be_unique():
    d = doc()
    d["stages"]["order"] = ["大厅", "大厅", "结算"]
    assert "E15" in err_codes(validate(d))


def test_e15_order_must_be_non_empty():
    d = doc()
    d["stages"]["order"] = []
    assert "E15" in err_codes(validate(d))


def test_e16_dynamic_narrow_must_point_to_code():
    """上不了纸的条件逻辑只留指针，禁止在 JSON 里写伪表达式。"""
    d = doc()
    d["stages"]["definitions"]["出价"]["dynamic_narrow"] = {"by": "_active_stage_rois"}
    assert err_codes(validate(d)) == {"E16"}


def test_e17_paper_edge_not_implemented():
    """纸上有边、代码没实现 → 画了一棵不存在的树。"""
    d = doc()
    report = validate(d, code_edges={("大厅", "hall_card")})  # 少了 ("出价","panel_title")
    assert err_codes(report) == {"E17"}


def test_e18_code_edge_not_declared():
    """代码有边、纸上没声明 → 藏了一条边，事后还原不出来。"""
    d = doc()
    extra = CODE_EDGES | {("出价", "result_banner")}
    report = validate(d, code_edges=extra)
    assert err_codes(report) == {"E18"}


def test_e17_e18_skipped_without_code_edges():
    """不传 code_edges 时跳过互查——校验器对运行时保持零依赖。"""
    report = validate()
    assert err_codes(report) == set()


def test_wildcard_transition_exempt_from_cross_check():
    """通配边表示"任意阶段"，与具体边不是一对一，不参与互查。"""
    d = doc()
    report = validate(d, code_edges=CODE_EDGES)  # BASE 里有 ("*","hall_card")
    assert report.ok, report.text()


@pytest.mark.parametrize(
    "bad_to", ["不存在的阶段", "", "$other"],
)
def test_e19_transition_target(bad_to):
    d = doc()
    d["transitions"][0]["to"] = bad_to
    assert "E19" in err_codes(validate(d))


def test_e19_special_targets_accepted():
    for to in ("same", "$round"):
        d = doc()
        d["transitions"][0]["to"] = to
        assert err_codes(validate(d)) == set()


def test_e19_transition_source_stage_unknown():
    d = doc()
    d["transitions"][0]["stage"] = "不存在的阶段"
    assert "E19" in err_codes(validate(d))


def test_e11_click_requires_confirm():
    d = doc()
    del d["routes"]["大厅→出价"]["steps"][0]["confirm"]
    assert err_codes(validate(d)) == {"E11"}


def test_e11_do_nothing_needs_no_confirm():
    """`do_nothing` 不产生跳转动作，无需证伪判据。"""
    d = doc()
    d["routes"]["大厅→出价"]["steps"][0].update(action="do_nothing")
    d["routes"]["大厅→出价"]["steps"][0].pop("confirm")
    assert err_codes(validate(d)) == set()


def test_e20_compiled_node_name_collision():
    """编译产物里出现同名节点 = 产物被手改或合并时撞车。"""
    assets = build()
    compiled = {
        "_generated": True,
        "source_hash": assets.source_hash,
        "node_names": ["demo::r::0::a", "demo::r::0::a"],
    }
    assert not validate_compiled(compiled, assets).ok


def test_e20_merged_collision_across_assets():
    """同一模块的两份资产生成同名节点 → 说明资产被重复加载/错挂。"""
    a = build()
    b = build()
    b.source_path = Path("dup/demo_assets.json")
    report = validate_merged([a, b])
    assert "E20" in err_codes(report)


def test_e20_no_collision_when_modules_differ():
    """节点名带模块前缀的意义：不同模块的同名 route 不该互相覆盖。"""
    a = build()
    b = Assets.from_document(doc(), module="treasure")
    assert validate_merged([a, b]).ok


def test_compiled_source_hash_mismatch(tmp_path):
    """产物头部的来源 hash 与源资产对不上 = 产物已过期或被手改。"""
    p = tmp_path / "a.json"
    p.write_text(json.dumps(doc(), ensure_ascii=False), encoding="utf-8")
    assets = Assets.load(p, module=MODULE)
    assert assets.source_hash, "从文件加载的资产必须有 source_hash"

    stale = {"_generated": True, "source_hash": "deadbeef", "node_names": []}
    assert not validate_compiled(stale, assets).ok

    fresh = {
        "_generated": True,
        "source_hash": assets.source_hash,
        "node_names": assets.compilation_node_names(),
    }
    assert validate_compiled(fresh, assets).ok


def test_source_hash_absent_for_in_memory_assets():
    """内存构造的资产没有源文件，source_hash 为 None（不造假 hash 让产物永远'看起来一致'）。"""
    assert build().source_hash is None


# ------------------------------------------------------------------
# 告警 W01-W07
# ------------------------------------------------------------------


def _make_image_dirs(tmp_path: Path, global_files, module_files) -> tuple[Path, Path]:
    g = tmp_path / "global"
    m = tmp_path / "demo"
    g.mkdir()
    m.mkdir()
    for name in global_files:
        (g / name).write_bytes(b"\x89PNG")
    for name in module_files:
        (m / name).write_bytes(b"\x89PNG")
    return g, m


def test_w01_unreferenced_template(tmp_path):
    g, m = _make_image_dirs(
        tmp_path, ["hall_card.png"], ["panel_title.png", "result_banner.png", "unused.png"]
    )
    report = validate(image_dirs=(g, m))
    assert "W01" in warn_codes(report)
    assert report.ok, "W01 是告警，不得阻断启动"


def test_w02_dangling_template(tmp_path):
    g, m = _make_image_dirs(tmp_path, [], ["panel_title.png", "result_banner.png"])
    report = validate(image_dirs=(g, m))   # hall_card.png 两个目录都没有
    assert "W02" in warn_codes(report)
    assert report.ok


def test_w03_empty_global_anchors():
    """global_anchors 为空 = 阶段冻结事故（不变量 I-1），必须可见。"""
    d = doc()
    d["stages"]["global_anchors"] = []
    assert "W03" in warn_codes(validate(d))


def test_w04_global_owner_but_file_only_in_module(tmp_path):
    g, m = _make_image_dirs(
        tmp_path, ["panel_title.png", "result_banner.png"], ["hall_card.png"]
    )
    d = doc()
    d["anchors"]["hall_card"]["owner"] = "global"   # 声明全局，图却只在模块目录
    report = validate(d, image_dirs=(g, m))
    assert "W04" in warn_codes(report)


def test_w04_not_raised_when_file_in_global(tmp_path):
    g, m = _make_image_dirs(
        tmp_path, ["hall_card.png", "panel_title.png", "result_banner.png"], []
    )
    d = doc()
    d["anchors"]["hall_card"]["owner"] = "global"
    assert "W04" not in warn_codes(validate(d, image_dirs=(g, m)))


def test_w05_stage_without_definitions():
    """允许（与运行时"未登记→回退全量检测"的既有兜底一致），只告警，不升级为 error。"""
    d = doc()
    del d["stages"]["definitions"]["结算"]
    report = validate(d)
    assert "W05" in warn_codes(report)
    assert report.ok, "W05 不得阻断启动"


def test_w06_duplicate_order_within_page():
    d = doc()
    d["anchors"]["panel_title"]["order"] = 2   # 与 bid_btn 同页同 order
    assert "W06" in warn_codes(validate(d))


def test_w07_override_must_be_explicit():
    """同名规则已通电为 E 级（OPEN-6 定案 b）：未声明的隐式覆盖阻断启动。"""
    d = doc()
    global_doc = doc()
    global_doc["_module"] = "global"
    g_assets = Assets.from_document(global_doc, module="global")
    report = validate(d, global_assets=g_assets)
    assert "W07" in err_codes(report)
    assert not report.ok

    # 显式声明 _override 后不再报错
    d2 = doc()
    for anchor in d2["anchors"].values():
        anchor["_override"] = True
    report2 = validate(d2, global_assets=g_assets)
    assert "W07" not in err_codes(report2)
    assert report2.ok


# ------------------------------------------------------------------
# 加载与启动期断言
# ------------------------------------------------------------------


def test_safe_load_returns_issue_on_structural_error(tmp_path):
    d = doc()
    d["_schema_ver"] = 2
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    assets, report = safe_load(p, module=MODULE)
    assert assets is None
    assert err_codes(report) == {"E01"}


def test_safe_load_ok(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps(doc(), ensure_ascii=False), encoding="utf-8")

    assets, report = safe_load(p, module=MODULE)
    assert assets is not None
    assert report.ok, report.text()


def test_assert_valid_raises_with_full_report(tmp_path):
    d = doc()
    d["anchors"]["bid_btn"]["guarded_by"] = "ocr_value"   # E13
    d["stages"]["global_anchors"] = []                    # W03
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    assets, report = safe_load(p, module=MODULE)
    with pytest.raises(NavKitValidationError) as exc:
        assert_valid(assets, report)
    text = str(exc.value)
    assert "E13" in text and "W03" in text


def test_assert_valid_rejects_none_assets():
    """加载就失败时也要抛，调用方无需再判空。"""
    report = navkit.Report(
        issues=(navkit.Issue("E01", "error", "_schema_ver", "版本不对"),)
    )
    with pytest.raises(NavKitValidationError):
        assert_valid(None, report)


def test_report_text_renders_counts():
    d = doc()
    d["anchors"]["bid_btn"]["guarded_by"] = "ocr_value"
    text = validate(d).text()
    assert "1 项错误" in text
    assert "E13" in text
