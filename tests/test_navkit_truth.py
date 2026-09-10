# -*- coding: utf-8 -*-
"""NavKit v4 盘上真源契约测试。

本套件看护 **落盘真源三件套本身** 的形态与行为契约——历史定案（真机五/七/
八炸、P2b 节拍与起跑汇聚）逐条保留，fixture 直读盘上文件；等价性对拍记录见
tools/experiments/v4-p4b-source/。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.navkit import check_truth as ct

REPO = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO / "tools" / "navkit" / "schema"


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def truth():
    glob = _load(ct.GLOBAL_TRUTH)
    trea = _load(ct.TREASURE_TRUTH)
    policy = _load(ct.POLICY_TRUTH)
    return {**glob, **trea}, policy


def test_graph_clean_and_shaped(truth):
    full, policy = truth
    errors, warns = ct.validate_graph(full)
    assert errors == []
    assert warns == []
    dwell = [n for n, d in full.items() if ct.att(d).get("_dwell")]
    assert len(dwell) == 13
    assert sum(1 for d in full.values() if ct.att(d).get("_boot")) == 1
    assert sum(1 for d in full.values() if ct.att(d).get("_policy_loop")) == 1
    # chain=4：仅导航 route 链（hall_to_treasure 3 节点+confirm）；session_to_matching
    # 链整体摘除（决策阶段图不代点，真机七炸定案）
    chain = [n for n in full if ".rhall_to_treasure." in n or ".__confirm." in n]
    assert len(chain) == 4
    # 可导航锚点仅 goto_appraise_btn / hall_peak_appraise_card
    assert {"global.goto_appraise_btn", "global.hall_peak_appraise_card"} \
        <= set(full)
    assert len(full) == 21
    assert sorted(n for n, d in full.items() if ct.att(d).get("_entry")) == [
        "global.hall_peak_appraise_card.rhall_to_treasure.0",
        "treasure.__boot.dwell"]
    assert len(policy["actuators"]) == 33
    assert len(policy["perception"]["spec"]) == 53
    # 引擎契约段：白名单计数见证（改契约 = 有意识的真源变更，须过本锁）
    ec = policy["engine_contract"]
    assert len(ec["facts"]) == 16
    assert len(ec["state_fields"]) == 10
    assert len(ec["wait_keys"]) == 10
    assert ec["fallback"]["key"] == "stage_waiting"


def test_policy_loop_wired_into_every_dwell(truth):
    """单脑原则：局内/流程 dwell 挂 policy_loop 兜底；大厅二阶段不挂
    （导航归图锚点，决策脑不得在厅类阶段图外点击——真机五炸定案）。"""
    full, _ = truth
    loop = full["treasure.policy_loop"]
    assert loop["custom_action"] == "MRA_Policy"
    assert loop["custom_action_param"]["table"] == "treasure.policy.json#policy"
    assert loop["next"] == [] and loop["timeout"] == -1
    dwells = [n for n, d in full.items() if ct.att(d).get("_dwell")]
    assert len(dwells) == 13
    hall = {"global.游戏大厅.dwell", "global.活动页面.dwell"}
    # 鉴宝大厅(选择场次)挂 policy_loop：场次选择是动态决策（target_session+
    # 彩蛋计算），生态无法静态表达——MRA_Policy 本职（真机七炸定案）。
    for n in dwells:
        tail = full[n]["next"][-1]
        wired = isinstance(tail, dict) and tail.get("name") == "treasure.policy_loop" \
            and tail.get("jump_back") is True
        assert wired == (n not in hall), n


def test_spec_colorspace_contract(truth):
    """P4c 数据面契约：colorspace 三值合法；灰度豁免集 = 帧预算标定的声明集。

    灰度集依据 tools/experiments/v4-p4c-match/ 对拍报告（rgb 2.3~2.8× 成本、
    selected_check 彩图松判、session_start_match_btn 边界翻转）；改这里=改帧率
    与识别行为，须重跑对拍。其余锚点缺省 rgb（宪法 §6 默认彩色）。
    """
    _, policy = truth
    spec = policy["perception"]["spec"]
    for name, a in spec.items():
        cs = a.get("colorspace")
        assert cs is None or cs in ("gray", "rgb", "rgb_strict"), f"{name}: {cs!r}"
    gray_set = {n for n, a in spec.items() if a.get("colorspace") == "gray"}
    assert gray_set == {
        "appraiser_p1_caroline", "appraiser_p2_shotaro", "appraiser_selected_check",
        "session_start_match_btn", "round_big_banner", "result_banner",
    }, f"灰度声明集漂移: {gray_set}"


def _tpls_of(node) -> set[str]:
    r = node.get("recognition")
    if isinstance(r, dict) and r.get("type") == "Or":
        return {t for s in r["param"]["any_of"]
                for t in s["custom_recognition_param"]["templates"]}
    p = node.get("custom_recognition_param")
    return set(p["templates"]) if p else set()


def test_dwell_semantics(truth):
    full, _ = truth
    hall = full["global.游戏大厅.dwell"]
    assert hall["action"] == "DoNothing" and ct.att(hall)["_dwell"]
    # 信号专属化（真机八炸定案）：游戏大厅只认本页判定信号
    # （transitions: hall_peak_appraise_card → 游戏大厅）；共享信号（场次卡/
    # 活动页按钮）剔除——否则场次页会被大厅 dwell 误判回导航层
    # 链头前置（权威导航路径优先于裸锚点兜底）
    assert _tpls_of(hall) == {"hall_peak_appraise_card.png"}
    assert hall["next"][0] == "global.hall_peak_appraise_card.rhall_to_treasure.0"
    assert "global.goto_appraise_btn" in hall["next"]


def test_dwell_signals_scoped_by_transitions(truth):
    """信号专属化契约（真机八炸定案）：dwell 识别信号只保留「transitions 判定
    阶段含本阶段」的信号——判定表（on→to =「看到该信号=当前在该页」）驱动，
    共享信号不再让深层页面被浅层 dwell 截胡。"""
    full, _ = truth
    # 活动页面只认前往按钮（场次卡判定=鉴宝厅，剔除）
    assert _tpls_of(full["global.活动页面.dwell"]) == {"act_goto_appraise_btn.png"}
    # 鉴宝厅只认场次卡（is_matching_btn 判定=匹配中，剔除）
    assert _tpls_of(full["treasure.鉴宝大厅(选择场次).dwell"]) == {"hall_session_cards.png"}
    # 回合 dwell 保留双信号（round_banner/smart_bid_btn 判定=$round ∈ 回合）
    assert _tpls_of(full["treasure.第1回合出价.dwell"]) == {
        "round1_banner.png", "round2_banner.png", "round3_banner.png",
        "round4_banner.png", "round5_banner.png", "bid_smart_btn.png"}


def test_navigation_dwell_fallback_to_boot(truth):
    """纯导航兜底契约（真机八炸定案）：纯导航 dwell 的 next 是固定候选，
    timeout=-1 + 画面意外 = 永久静默——默认超时 + on_error 回 boot 重判
    自愈；决策阶段 dwell 保留 -1（policy_loop DirectHit 永远兜底，永不超时
    是决策循环设计）。route 链节点同款兜底。"""
    full, _ = truth
    for n in ["global.游戏大厅.dwell", "global.活动页面.dwell"]:
        d = full[n]
        assert "timeout" not in d, n  # 默认 20s 识别窗口
        assert d["on_error"] == ["treasure.__boot.dwell"], n
    dyn = full["treasure.鉴宝大厅(选择场次).dwell"]
    assert dyn["timeout"] == -1
    assert "on_error" not in dyn
    for n, d in full.items():
        if ".rhall_to_treasure." in n or ".__confirm.hall_to_treasure." in n:
            assert d["on_error"] == ["treasure.__boot.dwell"], n


def test_decision_loop_rhythm(truth):
    """决策循环节拍契约（P2b 延迟实测定案）：真机帧间隔 1s 远超 policy 的
    300ms——policy_loop 未置 0 pre/post_delay 白付框架默认 400ms，决策 dwell
    卡 600ms 超过 policy 节拍。修复：policy_loop 去默认 delay、决策阶段 dwell
    rate_limit 对齐 300，非决策（导航）保持 600。"""
    full, _ = truth
    pol = full["treasure.policy_loop"]
    assert pol["pre_delay"] == 0 and pol["post_delay"] == 0
    dyn = ["treasure.鉴宝大厅(选择场次).dwell", "treasure.第1回合出价.dwell",
           "treasure.第2回合出价.dwell", "treasure.第3回合出价.dwell",
           "treasure.第4回合出价.dwell", "treasure.第5回合出价.dwell",
           "treasure.选择鉴宝师.dwell"]
    nav = ["global.游戏大厅.dwell", "global.活动页面.dwell",
           "treasure.匹配中.dwell", "treasure.中标结算.dwell",
           "treasure.领取分红.dwell", "treasure.结算弹窗.dwell"]
    for s in dyn:
        assert full[s]["rate_limit"] == 300, s
    for s in nav:
        assert full[s]["rate_limit"] == 600, s


def test_boot_node_aggregates_stage_signals(truth):
    """起跑汇聚契约（P2b 真机三炸根修）：识别 = 全 stage 专属信号并集（去重）、
    next = 全 dwell 表、未知画面无限驻留——「任意 stage 起跑」语义。"""
    full, _ = truth
    boot = full["treasure.__boot.dwell"]
    assert ct.att(boot)["_boot"] is True and ct.att(boot)["_entry"] is True
    assert boot["action"] == "DoNothing" and boot["timeout"] == -1
    subs = boot["recognition"]["param"]["any_of"]
    assert all(s.get("recognition") != "DirectHit" for s in subs)  # 直过信号不进
    tpls = [t for s in subs for t in s["custom_recognition_param"]["templates"]]
    assert len(tpls) == len(set(tpls))  # 共享模板去重（真机八炸定案）
    # 全部 9 页专属信号可起跑（模板文件级：回合横幅 5 张、胜负横幅 2 张）
    assert len(set(tpls)) == 16
    assert {"act_goto_appraise_btn.png", "hall_peak_appraise_card.png",
            "hall_session_cards.png"} <= set(tpls)
    dwells = {n for n, d in full.items() if ct.att(d).get("_dwell")}
    assert set(boot["next"]) == dwells  # 全 dwell 表


def test_signals_not_clickable(truth):
    """单脑原则·图不代点（真机七炸定案）：纯信号锚点不进图的可点击位、
    参数归 policy actuators 表；可导航锚点保留全表回判。"""
    full, policy = truth
    pure_signals = ["hall_session_cards", "smart_bid_btn", "round_big_banner",
                    "appraiser_title", "is_matching_btn", "settle_title"]
    for a in pure_signals:
        assert f"treasure.{a}" not in full, a
        assert f"treasure.{a}" in policy["actuators"], a
        for n, d in full.items():
            for r in d.get("next") or []:
                assert ct.ref_name(r) != f"treasure.{a}", (n, a)
    # 信号内联不丢：鉴宝大厅 dwell 识别仍含 hall_session_cards 模板
    sess = full["treasure.鉴宝大厅(选择场次).dwell"]
    assert "hall_session_cards.png" in _tpls_of(sess)
    # 可导航锚点（routes target）保留且带全表回判
    assert len(full["global.goto_appraise_btn"]["next"]) >= 13


def test_dyn_stages_priority_and_exit(truth):
    """决策阶段契约（真机七炸定案）：rules 带动态 decision.source 的阶段
    （鉴宝厅/选鉴宝师/回合1-5）= boot 候选前置（共享信号截胡消歧）、
    出口只挂 policy_loop（不挂 route 链头，其链整体摘除归 actuators）。"""
    full, policy = truth
    dyn = {"鉴宝大厅(选择场次)", "选择鉴宝师", "第1回合出价", "第2回合出价",
           "第3回合出价", "第4回合出价", "第5回合出价"}
    boot_next = full["treasure.__boot.dwell"]["next"]
    stage_of = {n: n.split(".", 1)[1].removesuffix(".dwell") for n in boot_next}
    order = [stage_of[n] in dyn for n in boot_next]
    assert order == sorted(order, reverse=True), boot_next  # True 全在前
    sess = full["treasure.鉴宝大厅(选择场次).dwell"]
    tails = [r for r in sess["next"] if isinstance(r, dict)]
    assert len(tails) == 1 and tails[0]["name"] == "treasure.policy_loop"
    # session_to_matching 链不在图、其 steps 锚点归 actuators
    assert "treasure.session_start_match_click.rsession_to_matching.0" not in full
    assert "treasure.session_start_match_click" in policy["actuators"]


def test_entry_chain_walk(truth):
    full, _ = truth
    entry = full["global.hall_peak_appraise_card.rhall_to_treasure.0"]
    assert ct.att(entry)["_entry"] is True
    cur = entry["next"][0]
    for _ in range(8):
        if cur.endswith(".dwell"):
            break
        cur = full[cur]["next"][0]
    assert cur == "treasure.鉴宝大厅(选择场次).dwell"


def test_namespace_partition(truth):
    """分文件真源形态：global.json 只装大厅骨架（hall/activity 页），
    treasure.json 不出现 global.* 节点（切分幂等的落盘见证）。"""
    glob = _load(ct.GLOBAL_TRUTH)
    trea = _load(ct.TREASURE_TRUTH)
    assert all(n.startswith("global.") for n in glob)
    assert not any(n.startswith("global.") for n in trea)
    assert "global.游戏大厅.dwell" in glob and "global.活动页面.dwell" in glob


def test_actuators_isolated_from_canvas(truth):
    full, policy = truth
    act = policy["actuators"]
    assert "treasure.bid_numpad_0" in act
    assert "treasure.bid_numpad_0" not in full
    for k, v in act.items():
        assert "custom_recognition_param" in v  # 参数随行，引擎按名取用


def test_cross_truth_gates_pass(truth):
    """CI 同款三闸：图自洽 + 数据面可装配 + 两面交叉互洽。"""
    full, policy = truth
    errors, _ = ct.validate_graph(full)
    assert errors == []
    assert ct.cross_checks(full, policy) == []
    from maaracing_assistant.core.navkit.v4_source import load_nav_source
    nav = load_nav_source(ct.POLICY_TRUTH)
    assert len(nav.plan.spec) == 53
    assert nav.plan.detect_anchors and len(nav.policies.rules) == 24


def test_schema_files_present_and_valid():
    pipeline = json.loads((SCHEMA_DIR / "pipeline.schema.json").read_text(encoding="utf-8"))
    reco = json.loads((SCHEMA_DIR / "custom.recognition.schema.json").read_text(encoding="utf-8"))
    action = json.loads((SCHEMA_DIR / "custom.action.schema.json").read_text(encoding="utf-8"))
    assert pipeline["$defs"]["CustomRecognitionSchema"]["$ref"] == "./custom.recognition.schema.json"
    assert "MRA_Template" in json.dumps(reco, ensure_ascii=False)
    assert "MRA_Policy" in json.dumps(action, ensure_ascii=False)


def test_mra_template_nodes_match_schema_contract(truth):
    full, policy = truth
    for name, n in {**full, **policy["actuators"]}.items():
        p = n.get("custom_recognition_param")
        if p is None:
            continue
        assert n.get("recognition") == "Custom", name
        assert n.get("custom_recognition") == "MRA_Template", name
        assert p.get("mode") in ("template", "point"), name
        assert isinstance(p.get("rect"), list) and len(p["rect"]) == 4, name
        assert all(0.0 <= v <= 1.0 for v in p["rect"]), name


def test_dedup_rule_exempt_structural_copies(truth):
    full, _ = truth
    _, warns = ct.validate_graph(full)
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
        "treasure.a.dwell": {"recognition": "DirectHit", "action": "DoNothing",
                             "attach": {"_dwell": True}, "next": ["treasure.b"]},
        "treasure.b.dwell": {"recognition": "DirectHit", "action": "DoNothing",
                             "attach": {"_dwell": True}, "next": ["treasure.a"]},
    }
    _, warns = ct.validate_graph(fake)
    assert any("重复识别" in w for w in warns)
