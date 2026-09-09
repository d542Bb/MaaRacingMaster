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
    # chain=4：仅导航 route 链（hall_to_treasure 3 节点+confirm）；session_to_matching
    # 链整体摘除（决策阶段图不代点，真机七炸定案）
    assert v4["dwell"] == 13 and v4["chain"] == 4 and v4["policy_loop"] == 1
    assert v4["boot"] == 1
    assert v4["total"] == v4["dwell"] + v4["policy_loop"] + v4["boot"] \
        + v4["anchor_graph"] + v4["chain"]
    assert v4["actuators"] == 33 and v4["ocr_sensors"] == 18  # 24 执行 + 9 纯信号（图不代点）
    assert v4["anchor_graph"] == 2  # 可导航锚点仅 goto_appraise_btn / hall_peak_appraise_card
    assert audit["entry"] == ["treasure.__boot.dwell",
                              "treasure.hall_peak_appraise_card.rhall_to_treasure.0"]


def test_policy_loop_wired_into_every_dwell(full_graph):
    """单脑原则：局内/流程 dwell 挂 policy_loop 兜底；大厅三阶段不挂
    （导航归图锚点，v3 决策脑不得在厅类阶段图外点击——真机五炸定案）。"""
    full, _, _ = full_graph
    loop = full["treasure.policy_loop"]
    assert loop["custom_action"] == "MRA_Policy"
    assert loop["custom_action_param"]["table"] == "treasure.policy.json#policy"
    assert loop["next"] == [] and loop["timeout"] == -1
    dwells = [n for n, d in full.items() if d.get("_dwell")]
    assert len(dwells) == 13
    hall = {"treasure.游戏大厅.dwell", "treasure.活动页面.dwell"}
    # 鉴宝大厅(选择场次)挂 policy_loop：场次选择是动态决策（target_session+
    # 彩蛋计算），生态无法静态表达——MRA_Policy 本职（真机七炸定案）。
    for n in dwells:
        tail = full[n]["next"][-1]
        wired = isinstance(tail, dict) and tail.get("name") == "treasure.policy_loop" \
            and tail.get("jump_back") is True
        assert wired == (n not in hall), n


def test_dwell_semantics(full_graph):
    full, _, _ = full_graph
    hall = full["treasure.游戏大厅.dwell"]
    assert hall["action"] == "DoNothing" and hall["_dwell"]
    # 信号专属化（真机八炸定案）：游戏大厅只认本页判定信号（transitions:
    # hall_peak_appraise_card → 游戏大厅）；共享信号（场次卡/活动页按钮）
    # 剔除——否则场次页会被大厅 dwell 误判回导航层
    assert hall["recognition"] == "Custom"
    assert hall["custom_recognition_param"]["templates"] == ["hall_peak_appraise_card.png"]
    # 链头前置（v3 权威导航路径优先于裸锚点兜底）
    assert hall["next"][0] == "treasure.hall_peak_appraise_card.rhall_to_treasure.0"
    assert "treasure.goto_appraise_btn" in hall["next"]


def test_dwell_signals_scoped_by_transitions(full_graph):
    """信号专属化契约（真机八炸定案）：dwell 识别信号只保留「transitions 判定
    阶段含本阶段」的信号——判定表（on→to =「看到该信号=当前在该页」）驱动，
    共享信号不再让深层页面被浅层 dwell 截胡。"""
    full, _, _ = full_graph

    def tpls_of(node):
        r = node.get("recognition")
        if isinstance(r, dict) and r.get("type") == "Or":
            return {t for s in r["param"]["any_of"]
                    for t in s["custom_recognition_param"]["templates"]}
        return set(node["custom_recognition_param"]["templates"])

    # 活动页面只认前往按钮（场次卡判定=鉴宝厅，剔除）
    assert tpls_of(full["treasure.活动页面.dwell"]) == {"act_goto_appraise_btn.png"}
    # 鉴宝厅只认场次卡（is_matching_btn 判定=匹配中，剔除）
    assert tpls_of(full["treasure.鉴宝大厅(选择场次).dwell"]) == {"hall_session_cards.png"}
    # 回合 dwell 保留双信号（round_banner/smart_bid_btn 判定=$round ∈ 回合）
    assert tpls_of(full["treasure.第1回合出价.dwell"]) == {
        "round1_banner.png", "round2_banner.png", "round3_banner.png",
        "round4_banner.png", "round5_banner.png", "bid_smart_btn.png"}


def test_navigation_dwell_fallback_to_boot(full_graph):
    """纯导航兜底契约（真机八炸定案）：纯导航 dwell 的 next 是固定候选，
    timeout=-1 + 画面意外 = 永久静默——改默认超时 + on_error 回 boot 重判
    自愈；决策阶段 dwell 保留 -1（policy_loop DirectHit 永远兜底，永不超时
    是决策循环设计）。route 链节点同款兜底（v3 timeout_ms 重试语义）。"""
    full, _, _ = full_graph
    nav_dwells = ["treasure.游戏大厅.dwell", "treasure.活动页面.dwell"]
    dyn_dwell = "treasure.鉴宝大厅(选择场次).dwell"
    for n in nav_dwells:
        d = full[n]
        assert "timeout" not in d, n  # 默认 20s 识别窗口
        assert d["on_error"] == ["treasure.__boot.dwell"], n
    assert full[dyn_dwell]["timeout"] == -1
    assert "on_error" not in full[dyn_dwell]
    for n, d in full.items():
        if ".rhall_to_treasure." in n or ".__confirm.hall_to_treasure." in n:
            assert d["on_error"] == ["treasure.__boot.dwell"], n


def test_decision_loop_rhythm(full_graph):
    """决策循环节拍契约（P2b 延迟实测定案）：真机帧间隔 1s 远超 policy 的
    300ms——policy_loop 未置 0 pre/post_delay 白付框架默认 400ms，
    决策 dwell 卡 600ms 超过 policy 节拍。修复：policy_loop 去默认 delay、
    决策阶段 dwell rate_limit 对齐 300，非决策（导航）保持 600。"""
    full, _, _ = full_graph
    pol = full["treasure.policy_loop"]
    assert pol["pre_delay"] == 0 and pol["post_delay"] == 0  # 桥内已自带节奏，去框架空转
    dyn = ["鉴宝大厅(选择场次)", "第1回合出价", "第2回合出价", "第3回合出价",
           "第4回合出价", "第5回合出价", "选择鉴宝师"]
    nav = ["游戏大厅", "活动页面", "匹配中", "中标结算", "领取分红", "结算弹窗"]
    for s in dyn:
        assert full[f"treasure.{s}.dwell"]["rate_limit"] == 300, s
    for s in nav:
        assert full[f"treasure.{s}.dwell"]["rate_limit"] == 600, s


def test_boot_node_aggregates_stage_signals(full_graph):
    """起跑汇聚契约（P2b 真机三炸根修）：识别 = 全 stage 专属信号并集（去重）、
    next = 全 dwell 表、未知画面无限驻留——v3「任意 stage 起跑」语义。"""
    full, _, _ = full_graph
    boot = full["treasure.__boot.dwell"]
    assert boot["_boot"] is True and boot["_entry"] is True
    assert boot["action"] == "DoNothing" and boot["timeout"] == -1
    subs = boot["recognition"]["param"]["any_of"]
    assert all(s.get("recognition") != "DirectHit" for s in subs)  # 直过信号不进
    tpls = [t for s in subs for t in s["custom_recognition_param"]["templates"]]
    assert len(tpls) == len(set(tpls))  # 共享模板去重（真机八炸定案）
    # 全部 9 页专属信号可起跑（模板文件级：回合横幅 5 张、胜负横幅 2 张）
    assert len(set(tpls)) == 16
    assert {"act_goto_appraise_btn.png", "hall_peak_appraise_card.png",
            "hall_session_cards.png"} <= set(tpls)
    dwells = [n for n, d in full.items() if d.get("_dwell")]
    assert boot["next"] and set(boot["next"]) <= set(dwells)
    assert len(boot["next"]) == len(dwells)  # 全 dwell 表


def test_signals_not_clickable(full_graph):
    """单脑原则·图不代点（真机七炸定案）：纯信号锚点（非 routes target 的
    stage 信号）不进图的可点击位、节点本体归 actuators；可导航锚点保留全表回判。"""
    full, policy_doc, _ = full_graph
    pure_signals = ["hall_session_cards", "smart_bid_btn", "round_big_banner",
                    "appraiser_title", "is_matching_btn", "settle_title"]
    for a in pure_signals:
        assert f"treasure.{a}" not in full, a  # 不在图
        assert f"treasure.{a}" in policy_doc["actuators"], a  # 决策资产在表
        for n, d in full.items():
            for r in d.get("next") or []:
                assert mv.ref_name(r) != f"treasure.{a}", (n, a)
    # 信号内联不丢：鉴宝大厅 dwell 识别仍含 hall_session_cards 模板
    # （专属化后单信号形态：recognition=Custom + 顶层参数）
    sess = full["treasure.鉴宝大厅(选择场次).dwell"]
    assert "hall_session_cards.png" in sess["custom_recognition_param"]["templates"]
    # 可导航锚点（routes target）保留且带全表回判（full_graph 未切分，treasure 命名空间）
    goto = full["treasure.goto_appraise_btn"]
    assert len(goto["next"]) >= 13


def test_dyn_stages_priority_and_exit(full_graph):
    """决策阶段契约（真机七炸定案）：rules 带动态 decision.source 的阶段
    （鉴宝厅/选鉴宝师/回合1-5）= boot 候选前置（共享信号截胡消歧）、
    出口只挂 policy_loop（不挂 route 链头，其链整体摘除归 actuators）。"""
    full, policy_doc, audit = full_graph
    dyn = set(audit["dyn_stages"])
    assert dyn == {"鉴宝大厅(选择场次)", "选择鉴宝师",
                   "第1回合出价", "第2回合出价", "第3回合出价",
                   "第4回合出价", "第5回合出价"}
    boot_next = full["treasure.__boot.dwell"]["next"]
    # 精确断言：dyn 阶段全部排在不 dyn 的前面
    stage_of = {n: n.removeprefix("treasure.").removesuffix(".dwell") for n in boot_next}
    order = [stage_of[n] in dyn for n in boot_next]
    assert order == sorted(order, reverse=True), boot_next  # True 全在前
    sess = full["treasure.鉴宝大厅(选择场次).dwell"]
    tails = [r for r in sess["next"] if isinstance(r, dict)]
    assert len(tails) == 1 and tails[0]["name"] == "treasure.policy_loop"
    # session_to_matching 链不在图、其 steps 锚点归 actuators
    assert "treasure.session_start_match_click.rsession_to_matching.0" not in full
    assert "treasure.session_start_match_click" in policy_doc["actuators"]


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
            for raw in d.get(key) or []:
                ref = mv.ref_name(raw)
                assert ref is not None, f"{n} 的 {key} 元素形态非法 {raw!r}"
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
    # 入口两类：global 链头（route 线性段）+ 插件 boot 汇聚（生产入口，P2b 设计）
    assert set(entries) == {"global.hall_peak_appraise_card.rhall_to_treasure.0",
                            "treasure.__boot.dwell"}
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
