# -*- coding: utf-8 -*-
"""结算 OCR 页面门控（闸②）：按本帧页面令牌过滤跨页读数。

背景是本仓实测的两次脏读：结算页转场后，`settle_my_income` 的 ROI 对在了大厅场次卡上，
把「资产要求 300000」当成收入落盘。第一次根因是阶段判定（_current_stage）带防抖，画面
已切走时仍滞后停在旧页；改用投递时快照 `_last_raw_stage` 后仍会漏——该值由观察线程按
STAGE_JUDGE_INTERVAL_MS 周期写，而帧是决策线程每 tick 自截的，两个捕获流不同源，转场
恰好落在窗口里时令牌仍是旧页、像素已是新页（实证：帧 997 令牌='领取分红' 而读到 300000）。

令牌因此改由 worker 对**被识别的那一帧**现场判定（`_judge_frame_page` → detector
`probe_page`），同源由构造保证。真源始终是 policy 的 `definitions[*].ocr`（「该阶段扫哪些
OCR 信号」）反转而成的「该信号允许出现在哪些阶段」；判定锚点由 `active` 里的标志锚点
派生，不新增配置字段。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

try:
    from maaracing_master.core.navkit import ROUND_PHASE_STAGE
    from maaracing_master.core.navkit.v4_source import load_nav_source
    from maaracing_master.plugins.treasure.detector import TreasureStageDetector
    from maaracing_master.plugins.treasure.module import TreasureModule

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)

_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "maaracing_master"
    / "plugins"
    / "treasure"
    / "resources"
    / "policy"
    / "treasure.policy.json"
)

_SETTLE_SIGNALS = (
    "settle_final_price",
    "settle_total_price",
    "settle_profit",
    "settle_my_income",
)


class _StubDetector:
    """只提供 plan 的 detector 桩（门控只读 detector.plan 与回合族判据）。

    回合族判据直接借用真实现——桩只桩掉「装配」，不复制定义，否则这里的副本一旦
    与生产分叉，测试会替生产背书。
    """

    is_round_stage = staticmethod(TreasureStageDetector.is_round_stage)

    def __init__(self, plan: object | None) -> None:
        self.plan = plan


def _new_module(plan: object | None) -> TreasureModule:
    """绕过 __init__ 造一个只带门控所需字段的 module（构造完整模块依赖过重）。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = _StubDetector(plan)
    mod._ocr_page_drops = 0
    return mod


def _filter(plan: object | None, stage: str | None, data: dict) -> tuple[dict, int]:
    """跑一次门控，返回 (保留下来的读数, 丢弃个数)。"""
    mod = _new_module(plan)
    result = {"frame_id": 1, "round_no": None, "stage": stage, "data": dict(data)}
    kept = TreasureModule._ocr_filter_by_page(mod, result)
    return kept, mod._ocr_page_drops


@pytest.fixture(scope="module")
def plan():
    return load_nav_source(_POLICY_PATH).plan


# --------------------------------------------------------------------------
# 真源：stages_for 是 ocr_for 的逆查
# --------------------------------------------------------------------------

def test_stages_for_is_inverse_of_ocr_for(plan):
    for signal in _SETTLE_SIGNALS:
        assert plan.stages_for(signal) == frozenset({"中标结算", "领取分红"})


def test_stages_for_returns_none_for_unregistered_signal(plan):
    """未在任何阶段登记的信号 → 无页面约束（门控必须放行，不能误杀）。"""
    assert plan.stages_for("这个信号不存在") is None


# --------------------------------------------------------------------------
# 门控行为
# --------------------------------------------------------------------------

def test_settle_readings_dropped_on_hall_page(plan):
    """回归：结算页转场到大厅后，结算读数不得被消费（300000 脏读场景）。"""
    data = {s: 300000 for s in _SETTLE_SIGNALS}
    kept, dropped = _filter(plan, "鉴宝大厅(选择场次)", data)
    assert kept == {}
    assert dropped == len(_SETTLE_SIGNALS)


def test_settle_readings_kept_on_settle_page(plan):
    data = {s: 1234 for s in _SETTLE_SIGNALS}
    kept, dropped = _filter(plan, "中标结算", data)
    assert kept == data
    assert dropped == 0


def test_settle_readings_kept_on_payout_page(plan):
    """同一套结算 ROI 服务中标结算与领取分红两个阶段，两者都必须放行。"""
    data = {s: 1234 for s in _SETTLE_SIGNALS}
    kept, dropped = _filter(plan, "领取分红", data)
    assert kept == data
    assert dropped == 0


def test_unknown_page_token_drops_registered_signals(plan):
    """转场中 detector 认不出当前页 → 已登记信号一律丢弃（不复制闸①的 None 放行例外）。"""
    data = {"settle_profit": 5000}
    kept, dropped = _filter(plan, None, data)
    assert kept == {}
    assert dropped == 1


def test_unregistered_signal_passes_on_any_page(plan):
    """未登记信号无页面约束：任何页（含转场）都放行，避免门控误伤。"""
    kept, dropped = _filter(plan, "鉴宝大厅(选择场次)", {"未登记信号": 7})
    assert kept == {"未登记信号": 7}
    assert dropped == 0


def test_bid_signals_not_consumed_on_settle_page(plan):
    """反向验证：出价信号只允许在出价回合，结算页读到也不得消费。"""
    kept, dropped = _filter(plan, "中标结算", {"bid_player1": 999})
    assert kept == {}
    assert dropped == 1


def test_round_family_token_passes_bid_signals(plan):
    """回合族令牌（本帧在出价面板页，未推导第几回合）→ 出价信号放行。

    出价面板整族共用同一块招牌，stages_for 本就把 5 个回合一起返回、任一都放行，
    故令牌按族产出即可；具体第几回合门控并不需要（也就不该为它去读跨线程状态）。
    """
    data = {"bid_player1": 800, "round_label_area": "第2回合"}
    kept, dropped = _filter(plan, ROUND_PHASE_STAGE, data)
    assert kept == data
    assert dropped == 0


def test_round_family_token_does_not_open_settle_signals(plan):
    """反向：回合族令牌对结算信号无效——出价面板页读到结算 ROI 仍须丢弃。"""
    data = {"settle_my_income": 300000, "bid_player1": 800}
    kept, dropped = _filter(plan, ROUND_PHASE_STAGE, data)
    assert kept == {"bid_player1": 800}
    assert dropped == 1


def test_mixed_payload_filters_per_signal(plan):
    """按信号逐个过滤：同一份结果里的跨页项被丢，本页项保留。"""
    data = {"settle_profit": 5000, "bid_player2": 800, "未登记信号": 3}
    kept, dropped = _filter(plan, "中标结算", data)
    assert kept == {"settle_profit": 5000, "未登记信号": 3}
    assert dropped == 1


def test_missing_plan_means_no_filtering():
    """真源缺失（plan 未装配）→ 不过滤，保持既有行为，不因门控缺失而误杀。"""
    kept, dropped = _filter(None, "随便什么页", {"settle_profit": 5000})
    assert kept == {"settle_profit": 5000}
    assert dropped == 0


# --------------------------------------------------------------------------
# 帧内判定：令牌由 worker 对同一帧现场产出（不再跨线程搬运）
# --------------------------------------------------------------------------

class _ProbeStubDetector:
    """令牌产地桩：plan 是真的（真源数据面），只有「本帧判定结果」可控。"""

    is_round_stage = staticmethod(TreasureStageDetector.is_round_stage)

    def __init__(self, plan: object | None, verdict: str | None) -> None:
        self.plan = plan
        self.verdict = verdict
        self.seen_stages: list[set] = []

    def probe_page(self, frame_rgb, stages):
        self.seen_stages.append(set(stages))
        return self.verdict


class _CorroboratingStubDetector(_ProbeStubDetector):
    """在产地桩之上加「第二类同帧证据」桩：记录被问到的回合号，印证结果可控。"""

    def __init__(self, plan: object | None, verdict: str | None, confirm: bool) -> None:
        super().__init__(plan, verdict)
        self.confirm = confirm
        self.confirm_calls: list = []

    def confirm_round_page(self, frame_rgb, round_no):
        self.confirm_calls.append(round_no)
        return self.confirm


def _judge(mod: TreasureModule, keys, round_no=None):
    mod._ocr_page_token_src = None   # 裸模块（__new__ 构造）没有 __init__ 里那个观测字段
    return TreasureModule._judge_frame_page(
        mod, np.zeros((8, 8, 3), dtype=np.uint8), keys, round_no)


def test_ocr_push_carries_no_page_token():
    """投递槽是 6 元组：令牌与像素同源这件事，投递侧给不出来。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._ocr_lock = threading.Lock()
    mod._ocr_wakeup = threading.Event()
    mod._ocr_frame_id = 0
    mod._round_no = None
    mod._ocr_pending = None
    mod._last_raw_stage = "中标结算"  # 观察线程恰好停在结算页，也不许进槽

    mod._ocr_push(np.zeros((8, 8, 3), dtype=np.uint8))

    assert mod._ocr_pending is not None
    assert len(mod._ocr_pending) == 6
    # 槽里唯一的字符串是 task——没有页面令牌这一位（numpy 数组不能直接做 `in` 比较）
    assert [x for x in mod._ocr_pending if isinstance(x, str)] == ["ocr"]


def test_judge_frame_page_derives_stages_from_signals(plan):
    """判定阶段集由本次识别信号反查 stages_for —— 与消费侧同一份真源。"""
    det = _ProbeStubDetector(plan, "领取分红")
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = det

    token = _judge(mod, frozenset({"settle_my_income"}))

    assert token == "领取分红"
    assert det.seen_stages == [{"中标结算", "领取分红"}]


def test_judge_frame_page_none_when_page_unrecognizable(plan):
    """转场帧判不出本页 → None，正是 300000 脏读被拦下的那个判据。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = _ProbeStubDetector(plan, None)

    assert _judge(mod, frozenset({"settle_my_income"})) is None


def test_judge_frame_page_none_without_plan_or_probe():
    """真源/detector 缺失 → None（消费侧在 plan 缺失时不过滤，行为不变）。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = _StubDetector(None)
    assert _judge(mod, frozenset({"settle_my_income"})) is None


def test_judge_frame_page_none_when_no_signals():
    """本批信号都没页面约束 → 无令牌（消费侧对这些信号本就放行）。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = _ProbeStubDetector(None, "随便什么页")
    assert _judge(mod, frozenset()) is None
    assert _judge(mod, None) is None


def test_in_frame_token_drives_page_gate(plan):
    """端到端：帧内判定产出令牌 → 同一帧的门控消费它（#453 路径复现）。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = _ProbeStubDetector(plan, None)
    mod._ocr_page_drops = 0

    token = _judge(mod, frozenset({"settle_my_income"}))
    kept = TreasureModule._ocr_filter_by_page(mod, {
        "frame_id": 997, "round_no": None, "stage": token,
        "data": {"settle_my_income": 300000},
    })

    assert kept == {}
    assert mod._ocr_page_drops == 1


def test_round_family_token_drives_page_gate(plan):
    """端到端：局内帧的回合族令牌经门控放行出价读数（本次回归的现场路径）。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = _ProbeStubDetector(plan, ROUND_PHASE_STAGE)
    mod._ocr_page_drops = 0

    token = _judge(mod, frozenset({"bid_player1"}))
    kept = TreasureModule._ocr_filter_by_page(mod, {
        "frame_id": 12, "round_no": 1, "stage": token,
        "data": {"bid_player1": 800},
    })

    assert token == ROUND_PHASE_STAGE
    assert kept == {"bid_player1": 800}
    assert mod._ocr_page_drops == 0


# --------------------------------------------------------------------------
# 标志锚点派生（真 detector + 真 policy，不依赖图像）
# --------------------------------------------------------------------------

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[1] / "maaracing_master" / "plugins" / "treasure"
)


@pytest.fixture(scope="module")
def detector():
    return TreasureStageDetector(_PLUGIN_DIR)


def test_proof_anchors_take_only_own_stage(detector):
    """只取归属本阶段的锚点：「领取分红」的 active 含 daily_high_banner（属结算弹窗）。"""
    stages = detector.plan.stages_for("settle_my_income")
    assert detector._proof_anchors(stages) == {"settle_title", "result_banner"}


def test_proof_anchors_accept_round_phase_sentinel(detector):
    """回合族阶段的标志锚点归属 __round_phase__ 哨兵，必须被认作本阶段证据。"""
    stages = detector.plan.stages_for("bid_player1")
    assert detector._proof_anchors(stages) == {"smart_bid_btn", "round_big_banner"}


def test_proof_anchors_empty_for_unknown_stage(detector):
    assert detector._proof_anchors(["不存在的阶段"]) == set()


def test_probe_page_none_when_no_anchor(detector):
    """无标志锚点可判（未知阶段/空集/None）→ None，不瞎猜。"""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert detector.probe_page(frame, ["不存在的阶段"]) is None
    assert detector.probe_page(frame, None) is None
    assert detector.probe_page(frame, ()) is None


def test_probe_page_writes_no_instance_state(detector):
    """帧内判定不得改动观察线程的判定产物（决策线程正在读 _last_hit_roi_key）。"""
    before = (detector._last_hit_roi_key, dict(detector._last_detect_scores),
              detector._last_round)
    detector.probe_page(np.zeros((720, 1280, 3), dtype=np.uint8),
                        detector.plan.stages_for("settle_my_income"))
    after = (detector._last_hit_roi_key, dict(detector._last_detect_scores),
             detector._last_round)
    assert before == after


def _forced_matcher(detector, anchor: str):
    """_match_score 替身：只让 anchor 的首个模板必中，其余锚点走真实匹配。

    打桩只打在模板匹配这一层，判定链路（优先级、阈值、margin 仲裁、哨兵分支）全是真的。
    刻意只顶首个模板：多模板锚点（回合横幅有 5 张）若张张满分，次高分与最高分并列，会被
    `arbitration.margin` 判成歧义而命中不了。
    """
    orig = detector._match_score
    only = detector.plan.spec[anchor].templates[0]

    def fake(roi_key, tpl_name, frame_rgb, gray_frame, px_roi, colorspace):
        if roi_key == anchor and tpl_name == only:
            return px_roi, 0.999
        return orig(roi_key, tpl_name, frame_rgb, gray_frame, px_roi, colorspace)

    return fake


def test_probe_page_round_token_ignores_last_round(monkeypatch):
    """回合族令牌不得读 _last_round（观察线程按周期维护的跨线程状态）。

    这是本次回归的锁：探针曾在 smart_bid_btn 命中后去读该字段（它自己从不写），字段
    为空时返回 None，并把自带回合号的横幅一并短路。实测同一批 515 帧，读它只判出 18
    帧局内页、不读判出 80 帧——局内读数被 fail-closed 大面积丢弃。
    """
    det = TreasureStageDetector(_PLUGIN_DIR)
    monkeypatch.setattr(det, "_match_score", _forced_matcher(det, "smart_bid_btn"))
    stages = det.plan.stages_for("bid_player1")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    monkeypatch.setattr(det, "_last_round", None)
    without_round = det.probe_page(frame, stages)
    monkeypatch.setattr(det, "_last_round", 3)
    with_round = det.probe_page(frame, stages)

    assert without_round == with_round == ROUND_PHASE_STAGE


def test_probe_page_round_token_survives_banner_first(monkeypatch):
    """同一帧上横幅（自带回合号）先命中也产出同一种令牌——令牌按族，不按来源分叉。"""
    det = TreasureStageDetector(_PLUGIN_DIR)
    monkeypatch.setattr(det, "_match_score", _forced_matcher(det, "round_big_banner"))
    stages = det.plan.stages_for("bid_player1")

    token = det.probe_page(np.zeros((720, 1280, 3), dtype=np.uint8), stages)

    assert token == ROUND_PHASE_STAGE


# --------------------------------------------------------------------------
# 端到端：闸②确实挡在消费路径上（不止是过滤函数本身正确）
# --------------------------------------------------------------------------

def _new_consuming_module(plan, consumed: list) -> TreasureModule:
    """只装配 _apply_ocr_result 所需字段的 module；消费目标换成收集器。"""
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = _StubDetector(plan)
    mod._ocr_lock = threading.Lock()
    mod._ocr_result = None
    mod._ocr_result_critical = None
    mod._ocr_page_drops = 0
    mod._ocr_stale_drops = 0
    mod._ocr_stale_drops_wr = 0
    mod._ocr_expired_drops = 0
    mod._ocr_expired_drops_wr = 0
    mod._ocr_applied = 0
    mod._ocr_applied_wr = 0
    mod._ocr_total_runs = 0
    mod._ocr_source_frame_id = 0
    mod._ocr_result_age_ms = 0.0
    mod._age_ms_win = []
    mod._dur_ms_win = []
    mod._bid_phase = ""
    mod._round_no = None
    mod._consume_ocr_result = consumed.append
    mod._maybe_build_snapshot = lambda: None
    return mod


def _run_apply(plan, stage: str | None, data: dict) -> list:
    consumed: list = []
    mod = _new_consuming_module(plan, consumed)
    mod._ocr_result = {
        "frame_id": 1,
        "round_no": None,  # 结算/分红期恒为 None → 闸①放行，正由闸②兜住
        "stage": stage,
        "captured_ts": time.perf_counter(),
        "completed_ts": time.time(),
        "duration_ms": 1.0,
        "data": dict(data),
    }
    mod._apply_ocr_result()
    return consumed


def test_cross_page_result_never_reaches_consumer(plan):
    """端到端回归：大厅页的结算读数不会进 _consume_ocr_result（300000 不会落盘）。"""
    consumed = _run_apply(plan, "鉴宝大厅(选择场次)", {"settle_my_income": 300000})
    assert consumed == []


def test_settle_page_result_reaches_consumer(plan):
    consumed = _run_apply(plan, "中标结算", {"settle_my_income": 0, "settle_profit": 12000})
    assert consumed == [{"settle_my_income": 0, "settle_profit": 12000}]


def test_transition_frame_does_not_reach_consumer(plan):
    """转场中（页面令牌 None）不消费：宁可少读一帧，也不把别的页的数字当本页读数。"""
    consumed = _run_apply(plan, None, {"settle_profit": 12000})
    assert consumed == []


# --------------------------------------------------------------------------
# 第二类同帧证据：模板锚点缺席时，同帧回合小字与投递时回合号互相印证
#
# 为什么需要：两个标志锚点（smart_bid_btn / round_big_banner）只在回合内的部分时段可见，
# 而 4 人报价数字是在「都已出价、面板关闭」之后才逐个显示的。那段窗口第一类证据恒为 None
# （实测帧上 OCR 已读出 122,100/250,000/163,100，令牌却是 None），整段公开报价被 fail-closed
# 丢掉 → 4 个报价槽全场命中≈0 → 快照建不起来 → 相位停在 wait_result。
# 第二类证据要求两侧互相印证：任一方单独说话都不作数（阶段标签有结算转场慢半拍的前科，
# 本帧文字也只能证明画面上写着某个回合号）。
# --------------------------------------------------------------------------

def test_round_label_corroboration_produces_round_token(plan):
    """公开报价窗口的现场路径：第一类缺席 + 印证成立 → 产出回合族令牌。"""
    det = _CorroboratingStubDetector(plan, None, True)
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = det

    token = _judge(mod, frozenset({"bid_player1", "round_label_area"}), 1)

    assert token == ROUND_PHASE_STAGE
    assert det.confirm_calls == [1]          # 印证问的就是投递时那个回合号


def test_round_label_mismatch_stays_fail_closed(plan):
    """回合号对不上（画面已切回合、标签还在旧回合）→ 仍 None，不放开读数。"""
    det = _CorroboratingStubDetector(plan, None, False)
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = det

    assert _judge(mod, frozenset({"bid_player1"}), 1) is None
    assert det.confirm_calls == [1]


def test_corroboration_not_used_for_non_round_signals(plan):
    """非回合族批次（结算/分红）不启用第二类证据——300000 那类跨页脏读照旧拦得住。"""
    det = _CorroboratingStubDetector(plan, None, True)
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = det

    assert _judge(mod, frozenset({"settle_my_income"}), 1) is None
    assert det.confirm_calls == []           # 连问都不问：结算页没有回合小字这条证据


def test_template_anchor_wins_over_corroboration(plan):
    """第一类命中即止，不再多跑一次回合小字识别（省一次 OCR，令牌口径不分叉）。"""
    det = _CorroboratingStubDetector(plan, ROUND_PHASE_STAGE, True)
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = det

    assert _judge(mod, frozenset({"bid_player1"}), 1) == ROUND_PHASE_STAGE
    assert det.confirm_calls == []


def test_detector_confirm_round_page_requires_matching_round(monkeypatch):
    """真 detector 的印证口径：解析回合号必须等于快照回合号；附加回合按 clamp 比。"""
    det = TreasureStageDetector(_PLUGIN_DIR)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    monkeypatch.setattr(det, "_detect_round_full", lambda *a, **k: 3)
    assert det.confirm_round_page(frame, 3) is True
    assert det.confirm_round_page(frame, 2) is False
    assert det.confirm_round_page(frame, None) is False   # 快照回合号缺失 → 不印证

    monkeypatch.setattr(det, "_detect_round_full", lambda *a, **k: 6)
    assert det.confirm_round_page(frame, 5) is True       # 附加回合在 stage 名里 clamp 成 5
    assert det.confirm_round_page(frame, 6) is False

    monkeypatch.setattr(det, "_detect_round_full", lambda *a, **k: None)
    assert det.confirm_round_page(frame, 3) is False      # 读不出回合号 → 不印证


def test_corroborated_token_reaches_consumer(plan):
    """端到端：印证产出的令牌 → 同一帧的报价读数过闸②进消费（改前这条链断在令牌）。"""
    det = _CorroboratingStubDetector(plan, None, True)
    mod = TreasureModule.__new__(TreasureModule)
    mod._detector = det
    mod._ocr_page_drops = 0

    token = _judge(mod, frozenset({"bid_player1", "round_label_area"}), 1)
    kept = TreasureModule._ocr_filter_by_page(mod, {
        "frame_id": 32, "round_no": 1, "stage": token,
        "data": {"bid_player1": 122100, "round_label_area": {"text": "第1回合"}},
    })

    assert kept["bid_player1"] == 122100
    assert mod._ocr_page_drops == 0
    # 同一条链上，令牌若仍为 None（改前行为），同一批读数会被整批丢掉
    mod2 = TreasureModule.__new__(TreasureModule)
    mod2._detector = _StubDetector(plan)
    mod2._ocr_page_drops = 0
    assert TreasureModule._ocr_filter_by_page(mod2, {
        "frame_id": 32, "round_no": 1, "stage": None,
        "data": {"bid_player1": 122100},
    }) == {}
