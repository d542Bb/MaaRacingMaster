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
    """只提供 plan 的 detector 桩（门控只读 detector.plan）。"""

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

    def __init__(self, plan: object | None, verdict: str | None) -> None:
        self.plan = plan
        self.verdict = verdict
        self.seen_stages: list[set] = []

    def probe_page(self, frame_rgb, stages):
        self.seen_stages.append(set(stages))
        return self.verdict


def _judge(mod: TreasureModule, keys):
    return TreasureModule._judge_frame_page(
        mod, np.zeros((8, 8, 3), dtype=np.uint8), keys)


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
