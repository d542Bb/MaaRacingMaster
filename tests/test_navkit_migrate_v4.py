# -*- coding: utf-8 -*-
"""P1-M1 迁移器契约测试：字段映射行为位一致、禁猜译、确定性、不冗余。"""
from __future__ import annotations

import json

import pytest

from tools.navkit import migrate_v4 as mv


def _v3_doc() -> dict:
    with mv.V3_ASSETS["treasure"].open(encoding="utf-8") as f:
        return json.load(f)


def test_coverage_matches_v3_anchor_kinds():
    doc = _v3_doc()
    anchors = doc["anchors"]
    nodes, sensors = mv.build_v4("treasure", None)
    assert len(nodes) == sum(1 for a in anchors.values() if a["kind"] != "ocr")
    assert len(sensors) == sum(1 for a in anchors.values() if a["kind"] == "ocr")
    assert nodes and sensors


def test_template_param_bitwise_identical():
    doc = _v3_doc()
    nodes, _ = mv.build_v4("treasure", None)
    src = doc["anchors"]["daily_high_banner"]
    param = nodes["treasure.daily_high_banner"]["custom_recognition_param"]
    assert param["templates"] == src["templates"]
    assert param["rect"] == pytest.approx(src["rect"])
    assert param["threshold"] == src["threshold"]


def test_arbitration_and_guard_follow_param():
    doc = _v3_doc()
    nodes, _ = mv.build_v4("treasure", None)
    arbitrated = [n for n, a in doc["anchors"].items() if a.get("arbitration") and a["kind"] != "ocr"]
    assert arbitrated
    name = f"treasure.{arbitrated[0]}"
    param = nodes[name]["custom_recognition_param"]
    assert param["arbitration"] == doc["anchors"][arbitrated[0]]["arbitration"]
    guarded = [n for n, a in doc["anchors"].items()
               if a.get("guarded_by") and a["kind"] != "ocr"]
    if guarded:
        gparam = nodes[f"treasure.{guarded[0]}"]["custom_recognition_param"]
        assert gparam["guarded_by"].startswith("treasure.")


def test_ocr_anchors_are_sensors_not_nodes():
    doc = _v3_doc()
    nodes, sensors = mv.build_v4("treasure", None)
    for name, a in doc["anchors"].items():
        if a["kind"] == "ocr":
            assert f"treasure.{name}" not in nodes
            assert f"treasure.{name}" in sensors


def test_unknown_kind_raises_no_guessing():
    doc = _v3_doc()
    doc["anchors"]["quantum_thing"] = {"kind": "quantum", "rect": [0, 0, 1, 1]}
    with pytest.raises(mv.MigrateError, match="quantum"):
        mv.migrate_anchor("quantum_thing", doc["anchors"]["quantum_thing"], {"a.png"})


def test_missing_template_file_raises():
    with pytest.raises(mv.MigrateError, match="模板文件缺失"):
        mv.migrate_anchor("ghost", {"kind": "template", "rect": [0, 0, 0.5, 0.5],
                                    "templates": ["definitely_not_exist_xyz.png"]}, set())


def test_bad_rect_raises():
    with pytest.raises(mv.MigrateError, match="越界"):
        mv.migrate_anchor("bad", {"kind": "template", "rect": [0, 0, 1.4, 0.5],
                                  "templates": []}, {"x.png"})


def test_deterministic_and_validate_clean():
    nodes1, sensors1 = mv.build_v4("treasure", None)
    nodes2, sensors2 = mv.build_v4("treasure", None)
    assert json.dumps(nodes1, sort_keys=True) == json.dumps(nodes2, sort_keys=True)
    assert mv.validate_nodes(nodes1) == []


def test_mapped_fields_not_duplicated_in_residual():
    doc = _v3_doc()
    nodes, _ = mv.build_v4("treasure", None)
    mapped_keys = {"label", "page", "order", "owner", "kind", "rect", "templates",
                   "threshold", "arbitration", "guarded_by"}
    for name, node in nodes.items():
        v3res = node.get("_v3", {})
        assert not (set(v3res) & mapped_keys), f"{name}: _v3 冗余了已映射字段"
        if doc["anchors"][name.removeprefix("treasure.")].get("owner"):
            assert node["_owner"] == "treasure"


# ===================== M2 图展开 =====================

@pytest.fixture(scope="module")
def full_graph():
    full, policy_doc, audit = mv.build_full("treasure")
    return full, policy_doc, audit


def test_graph_clean_and_shaped(full_graph):
    full, policy_doc, audit = full_graph
    errors, warns = mv.validate_graph(full)
    assert errors == []
    assert warns == []
    v4 = audit["v4"]
    assert v4["dwell"] == 13 and v4["chain"] == 6
    assert v4["total"] == v4["dwell"] + v4["anchor_graph"] + v4["chain"]
    assert v4["actuators"] == 24 and v4["ocr_sensors"] == 18
    assert audit["entry"] == ["treasure.hall_peak_appraise_card.rhall_to_treasure.0"]


def test_dwell_semantics(full_graph):
    full, _, _ = full_graph
    hall = full["treasure.游戏大厅.dwell"]
    assert hall["action"] == "DoNothing"
    assert hall["timeout"] == -1 and hall["_dwell"]
    assert hall["recognition"]["type"] == "Or"
    assert len(hall["recognition"]["param"]["any_of"]) == 3  # 3 个阶段信号内联
    assert "treasure.hall_peak_appraise_card.rhall_to_treasure.0" in hall["next"]
    assert "treasure.goto_appraise_btn" in hall["next"]


def test_round_expansion_and_target_first(full_graph):
    full, _, _ = full_graph
    banner = full["treasure.round_big_banner"]["next"]
    rounds = [f"treasure.第{i}回合出价.dwell" for i in range(1, 6)]
    assert all(r in banner for r in rounds)
    assert banner[:5] == rounds or set(banner[:5]) == set(rounds)  # 声明目标优先
    wild = full["treasure.smart_bid_btn"]["next"]  # 通配 $round 锚点也有目标
    assert any("回合" in t for t in wild[:5])


def test_entry_chain_walk(full_graph):
    full, _, _ = full_graph
    entry = full["treasure.hall_peak_appraise_card.rhall_to_treasure.0"]
    assert entry["_entry"] is True
    cur = entry["next"][0]
    for _ in range(8):
        if cur.endswith(".dwell"):
            break
        cur = full[cur]["next"][0]
    assert cur == "treasure.鉴宝大厅(选择场次).dwell"


def test_actuators_isolated_from_canvas(full_graph):
    full, policy_doc, _ = full_graph
    act = policy_doc["actuators"]
    assert "treasure.bid_numpad_0" in act
    assert "treasure.bid_numpad_0" not in full
    for k, v in act.items():
        assert "custom_recognition_param" in v  # 参数随行，引擎按名取用


def test_full_deterministic():
    a = json.dumps(mv.build_full("treasure")[0], sort_keys=True)
    b = json.dumps(mv.build_full("treasure")[0], sort_keys=True)
    assert a == b


# ===================== M3 global 切分 =====================

def test_split_global_reference_consistency(full_graph):
    full, _, _ = full_graph
    glob, trea, renames = mv.split_global(full)
    assert renames, "hall/activity 骨架应切出 global 节点"
    merged = {**glob, **trea}
    old_names = {old for old, _ in renames}
    for n, d in merged.items():
        for key in ("next", "on_error"):
            for ref in d.get(key) or []:
                assert ref not in old_names, f"{n} 引用了改名前的 {ref}"
                assert ref in merged, f"{n} 悬空引用 {ref}"
    errors, warns = mv.validate_graph(merged)
    assert errors == [] and warns == []
    assert len(merged) == len(full)


def test_split_entry_in_global_and_idempotent(full_graph):
    full, _, _ = full_graph
    glob1, trea1, _ = mv.split_global(full)
    glob2, trea2, _ = mv.split_global(full)
    assert json.dumps(glob1, sort_keys=True) == json.dumps(glob2, sort_keys=True)
    assert json.dumps(trea1, sort_keys=True) == json.dumps(trea2, sort_keys=True)
    entries = [n for n, d in {**glob1, **trea1}.items() if d.get("_entry")]
    assert entries and all(e.startswith("global.") for e in entries)
    assert any(n.startswith("global.游戏大厅.dwell") for n in glob1)
    assert not any(n.startswith("global.") for n in trea1)


# ===================== M4 schema 与校验器第 6 条 =====================

SCHEMA_DIR = mv.REPO / "tools" / "navkit" / "schema"


def test_schema_files_present_and_valid():
    pipeline = json.loads((SCHEMA_DIR / "pipeline.schema.json").read_text(encoding="utf-8"))
    reco = json.loads((SCHEMA_DIR / "custom.recognition.schema.json").read_text(encoding="utf-8"))
    action = json.loads((SCHEMA_DIR / "custom.action.schema.json").read_text(encoding="utf-8"))
    assert pipeline["$defs"]["CustomRecognitionSchema"]["$ref"] == "./custom.recognition.schema.json"
    assert "MRA_Template" in json.dumps(reco, ensure_ascii=False)
    assert "MRA_Policy" in json.dumps(action, ensure_ascii=False)


def test_mra_template_nodes_match_schema_contract(full_graph):
    full, policy_doc, _ = full_graph
    for name, n in {**full, **policy_doc["actuators"]}.items():
        p = n.get("custom_recognition_param")
        if p is None:
            continue
        assert n.get("recognition") == "Custom", name
        assert n.get("custom_recognition") == "MRA_Template", name
        assert p.get("mode") in ("template", "point"), name
        assert isinstance(p.get("rect"), list) and len(p["rect"]) == 4, name
        assert all(0.0 <= v <= 1.0 for v in p["rect"]), name


def test_dedup_rule_exempt_structural_copies(full_graph):
    full, _, _ = full_graph
    _, warns = mv.validate_graph(full)
    assert not [w for w in warns if "重复识别" in w]  # 链/dwell/confirm 结构性复制不报


def test_dedup_rule_catches_cross_anchor_duplicate():
    fake = {
        "treasure.a": {"recognition": "Custom", "custom_recognition": "MRA_Template",
                       "custom_recognition_param": {"mode": "template", "rect": [0, 0, 1, 1],
                                                   "templates": ["x.png"]},
                       "action": "Custom", "custom_action": "MRA_Click",
                       "next": ["treasure.b.dwell"]},
        "treasure.b": {"recognition": "Custom", "custom_recognition": "MRA_Template",
                       "custom_recognition_param": {"mode": "template", "rect": [0, 0, 1, 1],
                                                   "templates": ["x.png"]},
                       "action": "Custom", "custom_action": "MRA_Click",
                       "next": ["treasure.a.dwell"]},
        "treasure.a.dwell": {"recognition": "DirectHit", "action": "DoNothing", "_dwell": True,
                             "next": ["treasure.b"]},
        "treasure.b.dwell": {"recognition": "DirectHit", "action": "DoNothing", "_dwell": True,
                             "next": ["treasure.a"]},
    }
    _, warns = mv.validate_graph(fake)
    assert any("重复识别" in w for w in warns)
