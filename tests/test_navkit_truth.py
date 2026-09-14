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
    graph, _origin = ct.load_graph()
    policy = _load(ct.POLICY_TRUTH)
    return graph, policy


def test_graph_clean_and_shaped(truth):
    full, policy = truth
    errors, warns = ct.validate_graph(full)
    assert errors == []
    assert warns == []
    dwell = [n for n, d in full.items() if ct.att(d).get("_dwell")]
    assert len(dwell) == 15
    assert sum(1 for d in full.values() if ct.att(d).get("_boot")) == 1
    assert sum(1 for d in full.values() if ct.att(d).get("_policy_loop")) == 1
    # chain=4：仅导航 route 链（hall_to_treasure 3 节点+confirm）；session_to_matching
    # 链整体摘除（决策阶段图不代点，真机七炸定案）
    chain = [n for n in full if ".rhall_to_treasure." in n or ".__confirm." in n]
    assert len(chain) == 4
    # 可导航锚点仅 goto_appraise_btn / hall_peak_appraise_card
    assert {"treasure.goto_appraise_btn", "treasure.hall_peak_appraise_card"} \
        <= set(full)
    # 27 = 原 21 + 待机链 3（聊天框 anchor / dwell / wake）
    #       + 控制器弹窗链 3（面板 anchor / dwell / close）
    assert len(full) == 27
    assert sorted(n for n, d in full.items() if ct.att(d).get("_entry")) == [
        "treasure.__boot.dwell",
        "treasure.hall_peak_appraise_card.rhall_to_treasure.0"]
    assert len(policy["actuators"]) == 33
    # spec = 检测面锚点 + 收尾链专用锚点族（stage-from-node-plan §10：不进 transitions）
    #        + pass 二级确认弹窗惰性锚点（bid_pass_confirm_btn，module 直读）
    assert len(policy["perception"]["spec"]) == 65
    # 引擎契约段：白名单计数见证（改契约 = 有意识的真源变更，须过本锁）
    ec = policy["engine_contract"]
    assert len(ec["facts"]) == 16
    assert len(ec["state_fields"]) == 10
    assert len(ec["wait_keys"]) == 10
    assert ec["fallback"]["key"] == "stage_waiting"


def test_policy_loop_wired_into_every_dwell(truth):
    """单脑原则：局内/流程 dwell 挂 policy_loop 兜底；厅类页面/障碍态不挂
    （导航归图锚点，决策脑不得在厅类阶段图外点击——真机五炸定案；待机与控制器
    弹窗同属厅类，next 已是确定动作，无需也不得挂决策段）。"""
    full, _ = truth
    loop = full["treasure.policy_loop"]
    act = loop["action"]
    assert act["type"] == "Custom" and act["param"]["custom_action"] == "MaaRM_Policy"
    assert act["param"]["custom_action_param"]["table"] == "treasure.policy.json#policy"
    assert loop["next"] == [] and loop["timeout"] == -1
    dwells = [n for n, d in full.items() if ct.att(d).get("_dwell")]
    assert len(dwells) == 15
    hall = {"treasure.游戏大厅.dwell", "treasure.活动页面.dwell",
            "treasure.待机.dwell", "treasure.控制器指引弹窗.dwell"}
    # 鉴宝大厅(选择场次)挂 policy_loop：场次选择是动态决策（target_session+
    # 彩蛋计算），生态无法静态表达——MaaRM_Policy 本职（真机七炸定案）。
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
        "hall_chat_left", "hall_controller_popup",
        # 彩蛋收尾链专用（白字/图标类，进不了检测扫描，§10）
        "egg_claim_title", "egg_panel_tabbar", "egg_task_tab3",
        "hall_back_btn", "hall_home_btn",
    }, f"灰度声明集漂移: {gray_set}"


def _tpls_of(node) -> set[str]:
    """节点识别模板集（v1 平铺 / v2 嵌套 / Or 分支全兼容，口径同 check_truth）。"""
    return {t for p in ct.custom_reco_params(node) for t in p.get("templates") or []}


def test_dwell_semantics(truth):
    full, _ = truth
    hall = full["treasure.游戏大厅.dwell"]
    # action 兼容三种写法：v1 字符串、v2 {type,param} 嵌套、MPE 省略等于默认值的键
    act = hall.get("action", "DoNothing")
    assert (act if isinstance(act, str) else act.get("type")) == "DoNothing"
    assert ct.att(hall)["_dwell"]
    # 信号专属化（真机八炸定案）：游戏大厅只认本页判定信号
    # （transitions: hall_peak_appraise_card → 游戏大厅）；共享信号（场次卡/
    # 活动页按钮）剔除——否则场次页会被大厅 dwell 误判回导航层
    # 链头前置（权威导航路径优先于裸锚点兜底）
    assert _tpls_of(hall) == {"hall_peak_appraise_card.png"}
    assert hall["next"][0] == "treasure.hall_peak_appraise_card.rhall_to_treasure.0"
    assert "treasure.goto_appraise_btn" in hall["next"]


def test_dwell_signals_scoped_by_transitions(truth):
    """信号专属化契约（真机八炸定案）：dwell 识别信号只保留「transitions 判定
    阶段含本阶段」的信号——判定表（on→to =「看到该信号=当前在该页」）驱动，
    共享信号不再让深层页面被浅层 dwell 截胡。"""
    full, _ = truth
    # 活动页面只认前往按钮（场次卡判定=鉴宝厅，剔除）
    assert _tpls_of(full["treasure.活动页面.dwell"]) == {"act_goto_appraise_btn.png"}
    # 鉴宝厅只认场次卡（is_matching_btn 判定=匹配中，剔除）
    assert _tpls_of(full["treasure.鉴宝大厅(选择场次).dwell"]) == {"hall_session_cards.png"}
    # 回合 dwell 保留双信号（round_banner/smart_bid_btn 判定=$round ∈ 回合）
    assert _tpls_of(full["treasure.第1回合出价.dwell"]) == {
        "round1_banner.png", "round2_banner.png", "round3_banner.png",
        "round4_banner.png", "round5_banner.png", "bid_smart_btn.png"}


def test_stage_face_gate(truth):
    """页面清单等集闸：盘上真源必须过闸，且四类漂移各自被拦（漏改静默漂移的机检）。"""
    import copy

    full, policy = truth
    assert ct.stage_face_checks(full, policy) == []

    no_stage = copy.deepcopy(full)
    no_stage["treasure.待机.dwell"]["attach"].pop("_stage")
    assert any("treasure.待机.dwell" in e
               for e in ct.stage_face_checks(no_stage, policy))

    dup = copy.deepcopy(full)
    dup["treasure.待机.dwell"]["attach"]["_stage"] = "游戏大厅"
    assert any("_stage 重名" in e for e in ct.stage_face_checks(dup, policy))

    drift_policy = copy.deepcopy(policy)
    drift_policy["perception"]["stages"]["order"].remove("待机")
    errs = ct.stage_face_checks(full, drift_policy)
    assert any("不等集" in e and "待机" in e for e in errs)
    assert any("definitions 键集" in e and "待机" in e for e in errs)

    # 清单加一页、忘配 transitions 入边 → 该页运行时永远判不出
    unreach = copy.deepcopy(policy)
    unreach["perception"]["stages"]["order"].append("不存在的页")
    unreach["perception"]["stages"]["definitions"]["不存在的页"] = {
        "active": [], "ocr": [], "page": "hall"}
    errs = ct.stage_face_checks(full, unreach)
    assert any("无 transitions 入边" in e and "不存在的页" in e for e in errs)


def test_navigation_dwell_fallback_to_boot(truth):
    """纯导航兜底契约（真机八炸定案）：纯导航 dwell 的 next 是固定候选，
    timeout=-1 + 画面意外 = 永久静默——默认超时 + on_error 回 boot 重判
    自愈；决策阶段 dwell 保留 -1（policy_loop DirectHit 永远兜底，永不超时
    是决策循环设计）。route 链节点同款兜底。"""
    full, _ = truth
    for n in ["treasure.游戏大厅.dwell", "treasure.活动页面.dwell"]:
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
    nav = ["treasure.游戏大厅.dwell", "treasure.活动页面.dwell",
           "treasure.匹配中.dwell", "treasure.中标结算.dwell",
           "treasure.领取分红.dwell", "treasure.结算弹窗.dwell"]
    for s in dyn:
        assert full[s]["rate_limit"] == 300, s
    for s in nav:
        assert full[s]["rate_limit"] == 600, s


def test_boot_node_aggregates_stage_signals(truth):
    """起跑汇聚契约（P2b 真机三炸根修；2026-09-11 识别改按名引用）：识别 = 全 stage
    专属信号并集、next = 全 dwell 表、未知画面无限驻留——「任意 stage 起跑」语义。

    引用化前后行为等价的依据（两条，缺一不可）：
      · 协议侧——Or/And 的字符串子项按名取**被引节点的识别定义**、只跑识别不跑其动作
        （5.12.3 实测，tools/experiments/pipeline-inheritance/）；
      · 真源侧——原 11 块内联参数与这 9 个被引节点的 11 处参数逐一全等（第 2~5 回合与
        第 1 回合同规格），由两面同图闸 anchor_face_checks 持续锁住。
    """
    full, _ = truth
    boot = full["treasure.__boot.dwell"]
    assert ct.att(boot)["_boot"] is True and ct.att(boot)["_entry"] is True
    assert boot["action"]["type"] == "DoNothing" and boot["timeout"] == -1
    subs = boot["recognition"]["param"]["any_of"]
    assert all(isinstance(s, str) for s in subs), "汇聚识别必须按名引用，不留内联副本"
    assert subs == ct.and_or_refs(boot)

    dwells = {n for n, d in full.items() if ct.att(d).get("_dwell")}
    assert set(boot["next"]) == dwells  # 全 dwell 表
    dup_rounds = {f"treasure.第{i}回合出价.dwell" for i in (2, 3, 4, 5)}
    # 引用集 = 全 dwell 去掉同规格重复（第 1 回合作代表）。any_of 的**序**只影响识别
    # 开销，不影响落到哪个页面——路由序看 next（优先级契约见 test_dyn_stages_priority）
    assert set(subs) == dwells - dup_rounds, sorted(set(dwells) - set(subs) - dup_rounds)
    assert "treasure.第1回合出价.dwell" in subs
    for n in dwells - set(subs):     # 未被引用的 dwell 必须与某被引节点同规格，否则=漏页
        assert ct.custom_recognitions(full[n]) in [
            ct.custom_recognitions(full[m]) for m in subs], n

    def _rtype(n: dict) -> str:
        r = n.get("recognition")
        return r.get("type") if isinstance(r, dict) else r

    assert all(_rtype(full[s]) != "DirectHit" for s in subs)  # 直过信号不进汇聚
    tpls = {t for s in subs for _c, p in ct.custom_recognitions(full[s])
            for t in (p.get("templates") or [])}
    assert len(tpls) == 16, sorted(tpls)   # 与原内联并集同集（真机八炸去重定案不回归）
    assert {"act_goto_appraise_btn.png", "hall_peak_appraise_card.png",
            "hall_session_cards.png"} <= tpls


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
    # 可导航锚点点击后交起跑汇聚重判：全页面清单的唯一真源是 boot（Or 全信号 →
    # 全 dwell 表），锚点不再各自抄一份（2026-09-10 归位；曾三处重复 13 条清单）
    for anchor in ("treasure.goto_appraise_btn", "treasure.hall_peak_appraise_card"):
        assert full[anchor]["next"] == ["treasure.__boot.dwell"], anchor


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
    entry = full["treasure.hall_peak_appraise_card.rhall_to_treasure.0"]
    assert ct.att(entry)["_entry"] is True
    cur = entry["next"][0]
    for _ in range(8):
        if cur.endswith(".dwell"):
            break
        cur = full[cur]["next"][0]
    assert cur == "treasure.鉴宝大厅(选择场次).dwell"


def test_namespace_partition(truth):
    """真源归位见证（2026-09-10）：大厅入口链与页面锚点属**模块知识**，全部住
    plugin；core 侧无 pipeline 真源（尚无跨模块共用链）。

    协议层节点名全城唯一、没有命名空间 ⇒ "core/plugin 分离"的可见形态只能是
    引用方向单向 + 命名空间归属诚实，而不是文件摆放位置。
    """
    graph, origin = ct.load_graph()
    assert origin, "pipeline 真源发现为空（加载路径漂移）"
    assert all(n.startswith("treasure.") for n in graph), \
        [n for n in graph if not n.startswith("treasure.")]
    core_nodes = [n for n, f in origin.items() if ct.CORE_PIPELINE_DIR in f.parents]
    assert core_nodes == [], f"core 真源不得承载模块知识: {core_nodes}"
    assert {f.name for f in origin.values()} == {"treasure.json", "treasure.entry.json"}


def test_root_dollar_keys_never_become_nodes():
    """`$` 前缀根级键（MPE 回写的画布配置与外部节点占位）不得被当节点。

    框架明文不解析 `$` 根级字段；校验器若把它们算进图，就会凭空产出「入口不可达/
    无出口」告警与节点数漂移（2026-09-10 MPE 存盘后实测发生）。
    """
    graph, origin = ct.load_graph()
    assert not [n for n in graph if n.startswith("$")], graph.keys()
    assert not [n for n in origin if n.startswith("$")]


@pytest.mark.parametrize("node", [
    # v1 平铺
    {"custom_recognition": "MaaRM_Template",
     "custom_recognition_param": {"mode": "template", "templates": ["a.png"]}},
    # v2 归一（MPE 保存形态）
    {"recognition": {"type": "Custom",
                     "param": {"custom_recognition": "MaaRM_Template",
                               "custom_recognition_param": {"mode": "template",
                                                           "templates": ["a.png"]}}}},
    # Or 分支内联（起跑汇聚形态）
    {"recognition": {"type": "Or", "param": {"any_of": [
        {"recognition": "Custom", "custom_recognition": "MaaRM_Template",
         "custom_recognition_param": {"mode": "template", "templates": ["a.png"]}},
        {"recognition": "Custom", "custom_recognition": "MaaRM_Template",
         "custom_recognition_param": {"mode": "template", "templates": ["b.png"]}}]}}},
], ids=["v1平铺", "v2嵌套", "Or分支"])
def test_custom_recognitions_covers_all_protocol_shapes(node):
    """读取面必须同时吃下两种协议形态——否则 MPE 一存盘，机检就静默失明。"""
    got = ct.custom_recognitions(node)
    assert [n for n, _ in got] == ["MaaRM_Template"] * len(got)
    assert {t for _n, p in got for t in p.get("templates") or []} & {"a.png", "b.png"}


def test_truth_source_normalized_to_v2(truth):
    """项目规范形态 = **v2 归一**（框架内部规范形 + MPE 与官方 PipelineDumper 原生产）。

    v1 平铺在协议上仍合法（同一解析路径、新字段两边都生效），但仓库统一 v2：MPE 存盘
    不再产生形态翻转，人读与 diff 口径一致。本锁防的是"手写回 v1"让形态再度混居。
    默认值省写合法（`DoNothing`/`DirectHit` 由框架补），故只约束"写了就必须是对象形"。
    """
    flat_custom = {"custom_recognition", "custom_recognition_param",
                   "custom_action", "custom_action_param"}

    def check(n: dict, where: str) -> None:
        for key in ("recognition", "action"):
            v = n.get(key)
            if v is None:
                continue
            assert isinstance(v, dict) and "type" in v, f"{where}.{key} 非 v2 对象形: {v!r}"
            assert "param" in v or v["type"] in ("DirectHit", "DoNothing"), \
                f"{where}.{key} 缺 param: {v!r}"
            rp = v.get("param")
            if isinstance(rp, dict):
                # param 内出现 custom_recognition/custom_action 正是 v2 该放的位置
                for branch in ("any_of", "all_of"):
                    for i, sub in enumerate(rp.get(branch) or []):
                        if isinstance(sub, str):
                            continue  # 按名引用子项（v5.7）与形态无关，不是 v1 平铺
                        check(sub, f"{where}.{key}.{branch}[{i}]")
        assert not (flat_custom & set(n)), f"{where} 残留 v1 平铺 Custom 字段"

    full, _ = truth
    for name, n in full.items():
        check(n, name)


def test_layering_red_line_is_live():
    """分层红线必须是活的（防它退化成装饰）：合成违规图必须报，盘上真源必须零报。"""
    fake = {"global.hall_dwell": {"next": ["treasure.foo.dwell"]},
            "treasure.foo.dwell": {}}
    fake_origin = {"global.hall_dwell": ct.CORE_PIPELINE_DIR / "core.json",
                   "treasure.foo.dwell": ct.PLUGIN_PIPELINE_DIRS[0] / "t.json"}
    problems = ct.namespace_checks(fake, fake_origin)
    assert any("引用模块节点" in p for p in problems), f"core 点名模块节点未报: {problems}"

    occupy = {"treasure.hall_dwell": {}}
    occupy_origin = {"treasure.hall_dwell": ct.CORE_PIPELINE_DIR / "core.json"}
    assert any("占用模块命名空间" in p
               for p in ct.namespace_checks(occupy, occupy_origin)), "core 占用模块前缀未报"

    graph, origin = ct.load_graph()
    assert ct.namespace_checks(graph, origin) == []


# ---------------- 引用位闭合 + 两面同图（2026-09-11 新增闸门） ----------------
# 依据：MaaFW 5.12.3 实测（tools/experiments/pipeline-inheritance/）——And/Or 按名子项
# 是真引用（运行期取被引用节点的识别定义），但框架加载期不校验其名字，拼错要到运行期
# 才 `Bad sub ref` 静默判该 Or 未命中；`[Anchor]名` 在子项里同样按字面节点名解析，不可用。

def test_and_or_refs_collected_across_forms():
    """and_or_refs 必须同时吃 v2 嵌套、v1 平铺、以及内联子识别里再套一层 And/Or。"""
    v2 = {"recognition": {"type": "Or", "param": {"any_of": [
        "sig.a",
        {"recognition": {"type": "And", "param": {"all_of": ["sig.b"]}}}]}}}
    v1 = {"recognition": "Or", "any_of": ["sig.c"]}
    assert ct.and_or_refs(v2) == ["sig.a", "sig.b"]
    assert ct.and_or_refs(v1) == ["sig.c"]
    assert ct.and_or_refs({"recognition": "DirectHit"}) == []
    # 内联 Custom 参数不是引用，不得被误收
    inline = {"recognition": {"type": "Or", "param": {"any_of": [
        {"recognition": {"type": "Custom", "param": {
            "custom_recognition": "MaaRM_Template",
            "custom_recognition_param": {"templates": ["a.png"]}}}}]}}}
    assert ct.and_or_refs(inline) == []


def test_and_or_dangling_ref_is_error():
    """And/Or 子项写错门牌必须在这里红——框架不拦，运行期只静默失败。"""
    fake = {"入口": {"recognition": {"type": "Or", "param": {"any_of": ["不存在的节点"]}},
                     "next": ["尾"]},
            "尾": {"attach": {"_dwell": True}}}
    errors, _ = ct.validate_graph(fake)
    assert any("And/Or 子项悬空引用" in e for e in errors), errors


def test_anchor_ref_inside_and_or_is_error():
    """`[Anchor]` 出现在 And/Or 子项 = 按字面节点名解析的死引用，必须报并说明原因。"""
    fake = {"设锚": {"recognition": "DirectHit", "anchor": "回", "next": ["借锚"]},
            "借锚": {"recognition": {"type": "Or", "param": {"any_of": ["[Anchor]回"]}}}}
    errors, _ = ct.validate_graph(fake)
    assert any("And/Or 子项悬空引用" in e and "[Anchor]" in e for e in errors), errors


def test_builtin_input_action_is_error():
    """内置输入 action 打在我们的占位控制器上=假成功，v1/v2 两形态都要拦。"""
    errors = ct.action_checks({
        "点一下": {"action": "Click"},
        "按键": {"action": {"type": "ClickKey", "param": {"key_list": [27]}}},
    })
    assert len(errors) == 2, errors
    assert all("假成功" in e for e in errors), errors
    # Custom 与无副作用的内置类型不受影响
    assert ct.action_checks({"正常": {"action": {"type": "Custom", "param": {}}},
                             "停手": {"action": "StopTask"},
                             "什么都不做": {}}) == []


def test_anchor_only_needs_no_route_but_needs_a_referrer():
    """纯锚点：零路由不报「不可达/无出口」，但没人抄它就是孤儿真源。"""
    anchored = {"入口": {"attach": {"_entry": True}, "next": ["公共.聊天框"]},
                "公共.聊天框": {"attach": {"_anchor_only": True},
                                 "recognition": "DirectHit"}}
    errors, warns = ct.validate_graph(anchored)
    assert errors == [], errors
    assert warns == [], warns

    orphan = {"入口": {"attach": {"_entry": True}, "next": ["尾"]},
              "尾": {"attach": {"_dwell": True}},
              "公共.聊天框": {"attach": {"_anchor_only": True}, "recognition": "DirectHit"}}
    errors, _ = ct.validate_graph(orphan)
    assert any("孤儿真源" in e for e in errors), errors


def test_anchor_only_must_stay_route_free():
    """纯锚点带 next 就不是纯锚点了——搬进通用层时会点名业务层，当场拦。"""
    fake = {"入口": {"attach": {"_entry": True}, "next": ["公共.聊天框"]},
            "公共.聊天框": {"attach": {"_anchor_only": True},
                             "recognition": "DirectHit", "next": ["尾"]},
            "尾": {"attach": {"_dwell": True}}}
    errors, _ = ct.validate_graph(fake)
    assert any("纯锚点必须零路由" in e for e in errors), errors


def test_layering_red_line_covers_every_reference_slot():
    """红线口径 = 一切按名字指人的位置：And/Or 子项与 anchor 对象 value 同样受检。

    只堵 next/on_error 会留后门：公共层可以靠 any_of 借模块的识别定义，
    等于间接点名业务层，而机检一声不响。
    """
    fake = {"global.hall": {
        "recognition": {"type": "Or", "param": {"any_of": ["treasure.sig"]}},
        "anchor": {"出口": "treasure.entry"}}}
    origin = {"global.hall": ct.CORE_PIPELINE_DIR / "hall.json"}
    problems = ct.namespace_checks(fake, origin)
    refs = [p for p in problems if "引用模块节点" in p]
    assert len(refs) == 2, problems


def test_anchor_face_checks_live():
    """两面同图闸必须是活的：rect 分叉报、colorspace 分叉报、图侧新规格报、全等零报。"""
    def _g(rect, colorspace="rgb", templates=("a.png",)):
        return {"m.sig": {"recognition": {"type": "Custom", "param": {
            "custom_recognition": "MaaRM_Template",
            "custom_recognition_param": {"mode": "template", "templates": list(templates),
                                         "rect": list(rect), "colorspace": colorspace}}}}}

    policy = {"perception": {"spec": {
        "sig_a": {"kind": "template", "templates": ["a.png"],
                  "rect": [0.0, 0.0, 0.5, 0.5], "colorspace": "gray"}}}}
    errors, warns = ct.anchor_face_checks(_g([0.0, 0.0, 1.0, 1.0]), policy)
    assert any("两面不一致" in e and ".rect=" in e for e in errors), errors
    assert any("两面不一致" in e and ".colorspace=" in e for e in errors), errors
    assert warns == [], warns
    # 图侧引入 spec 未登记的规格 → 两面无从比对，必须拦
    errors2, _ = ct.anchor_face_checks(_g([0.0, 0.0, 0.5, 0.5], templates=("新图.png",)), policy)
    assert any("spec 无登记" in e for e in errors2), errors2
    # 结构性字段全等时零报（防闸退化成永远绿的装饰）；spec 省略 colorspace = 缺省 rgb
    errors3, warns3 = ct.anchor_face_checks(_g([0.0, 0.0, 0.5, 0.5], colorspace="gray"), policy)
    assert errors3 == [] and warns3 == [], (errors3, warns3)
    errors4, _ = ct.anchor_face_checks(_g([0.0, 0.0, 0.5, 0.5]), {"perception": {"spec": {
        "sig_a": {"kind": "template", "templates": ["a.png"], "rect": [0.0, 0.0, 0.5, 0.5]}}}})
    assert errors4 == [], errors4


def test_real_truth_faces_agree(truth):
    """盘上真源：两面逐字段零分叉、零告警（colorspace 定案「默认 gray，灰度拉不开转
    rgb」后，两面已统一；出现任何一条报或告警都说明有一面被单独改过）。"""
    full, policy = truth
    errors, warns = ct.anchor_face_checks(full, policy)
    assert errors == []
    assert warns == []


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
    from maaracing_master.core.navkit.v4_source import load_nav_source
    nav = load_nav_source(ct.POLICY_TRUTH)
    assert len(nav.plan.spec) == 65
    assert nav.plan.detect_anchors and len(nav.policies.rules) == 24
    # 待机的判定信号必须进 global_anchors：否则只有已判为待机时才扫它，而阶段又由它定
    # → 鸡生蛋，该页永远进不去。控制器指引弹窗相反——它只会在大厅/待机两页弹，故挂在
    # 那两页的 active 上即可（它的 rect 覆盖半屏，是全场最贵锚点，不进全局以免每帧交税）。
    assert "hall_chat_left" in nav.plan.global_anchors
    assert "hall_controller_popup" not in nav.plan.global_anchors
    assert {nav.plan.stage_stage.get("hall_chat_left"),
            nav.plan.stage_stage.get("hall_controller_popup")} == {"待机", "控制器指引弹窗"}
    assert set(nav.plan.global_anchors) == {
        "hall_peak_appraise_card", "hall_session_cards", "hall_chat_left"}


def test_schema_files_present_and_valid():
    pipeline = json.loads((SCHEMA_DIR / "pipeline.schema.json").read_text(encoding="utf-8"))
    reco = json.loads((SCHEMA_DIR / "custom.recognition.schema.json").read_text(encoding="utf-8"))
    action = json.loads((SCHEMA_DIR / "custom.action.schema.json").read_text(encoding="utf-8"))
    assert pipeline["$defs"]["CustomRecognitionSchema"]["$ref"] == "./custom.recognition.schema.json"
    assert "MaaRM_Template" in json.dumps(reco, ensure_ascii=False)
    assert "MaaRM_Policy" in json.dumps(action, ensure_ascii=False)


def test_mra_template_nodes_match_schema_contract(truth):
    """MaaRM_Template 参数面契约（v1 平铺 / v2 recognition.param 嵌套 / Or 分支全验）。"""
    full, policy = truth
    for name, n in {**full, **policy["actuators"]}.items():
        for reco_name, p in ct.custom_recognitions(n):
            assert reco_name == "MaaRM_Template", (name, reco_name)
            assert p.get("mode") in ("template", "point"), name
            assert isinstance(p.get("rect"), list) and len(p["rect"]) == 4, name
            assert all(0.0 <= v <= 1.0 for v in p["rect"]), name


def test_dedup_rule_exempt_structural_copies(truth):
    full, _ = truth
    _, warns = ct.validate_graph(full)
    assert not [w for w in warns if "重复识别" in w]  # 链/dwell/confirm 结构性复制不报


def test_dedup_rule_catches_cross_anchor_duplicate():
    fake = {
        "treasure.a": {"recognition": "Custom", "custom_recognition": "MaaRM_Template",
                       "custom_recognition_param": {"mode": "template", "rect": [0, 0, 1, 1],
                                                   "templates": ["x.png"]},
                       "action": "Custom", "custom_action": "MaaRM_Click",
                       "next": ["treasure.b.dwell"]},
        "treasure.b": {"recognition": "Custom", "custom_recognition": "MaaRM_Template",
                       "custom_recognition_param": {"mode": "template", "rect": [0, 0, 1, 1],
                                                   "templates": ["x.png"]},
                       "action": "Custom", "custom_action": "MaaRM_Click",
                       "next": ["treasure.a.dwell"]},
        "treasure.a.dwell": {"recognition": "DirectHit", "action": "DoNothing",
                             "attach": {"_dwell": True}, "next": ["treasure.b"]},
        "treasure.b.dwell": {"recognition": "DirectHit", "action": "DoNothing",
                             "attach": {"_dwell": True}, "next": ["treasure.a"]},
    }
    _, warns = ct.validate_graph(fake)
    assert any("重复识别" in w for w in warns)
