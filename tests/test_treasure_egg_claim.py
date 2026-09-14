# -*- coding: utf-8 -*-
"""彩蛋任务收尾链（stage-from-node-plan §10 A′）的落盘与数据面契约。

- record_egg_claim：弹窗蛋数按日桶累加进 daily_summary（games 蛋列退役后蛋数唯一写入点）；
- 收尾锚点族惰性：在 perception.spec（链直读的 rect/模板/阈值真源），但不得进
  transitions / stages.active / global_anchors 任何一处——进了就会被 detector 每帧
  扫描（A′ 的成本红线），也会被阶段闸判成"清单页面无 dwell"。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from maaracing_master.plugins.treasure.store import TreasureStore

POLICY_PATH = (Path(__file__).resolve().parents[1]
               / "maaracing_master" / "plugins" / "treasure"
               / "resources" / "policy" / "treasure.policy.json")


def _load_module_class():
    """取 TreasureModule —— 链内锚点清单的唯一真源（本文件不再手抄副本）。

    历史：本文件曾手抄一份 EGG_CHAIN_ANCHORS，与 module 常量漂移（少一项
    hall_peak_appraise_card；真源后来把它拆成独立的大厅锚点常量）。手抄副本与
    「唯一真源」直接冲突，故改为直接 import。
    module 顶层会 import maa.toolkit，CI 轻依赖环境下收集期即失败 → 局部导入 +
    skip，不把本文件其余纯数据面用例一并拖下水。
    """
    try:
        from maaracing_master.plugins.treasure.module import TreasureModule
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"需要完整运行时依赖（maa/…）：{exc}")
    return TreasureModule


class _FakeModule:
    """record_egg_claim 只触达 _refresh_daily_bucket / current_bucket_str 经 store 自身，
    module 侧仅 _refresh_daily_bucket 一个钩子。"""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir

    def _refresh_daily_bucket(self) -> None:
        return None


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = TreasureStore(_FakeModule(Path(d) / "data"))
        yield s
        s.close_db()


def _egg_row(store, bucket):
    return store._conn.execute(
        "SELECT egg_red, egg_yellow, egg_blue, egg_coin, egg_score, games"
        " FROM daily_summary WHERE bucket = ?",
        (bucket,),
    ).fetchone()


def test_record_egg_claim_creates_and_accumulates(store):
    b = store.current_bucket_str()
    assert _egg_row(store, b) is None
    store.record_egg_claim({"red": 2, "yellow": 1, "blue": 0}, coin=250000, score=30000)
    assert _egg_row(store, b) == (2, 1, 0, 250000, 30000, 0)
    store.record_egg_claim({"red": 1, "yellow": 1, "blue": 2}, coin=100000, score=5000)
    assert _egg_row(store, b) == (3, 2, 2, 350000, 35000, 0)


def test_record_egg_claim_coin_only_round_is_recorded(store):
    """蛋读数失败但银币/积分有值 → 照记（金额是 T0 主数，不因数蛋降级丢账）。"""
    b = store.current_bucket_str()
    store.record_egg_claim({}, coin=250000, score=30000)
    assert _egg_row(store, b) == (0, 0, 0, 250000, 30000, 0)


def test_record_egg_claim_zero_and_negative_are_noop(store):
    b = store.current_bucket_str()
    store.record_egg_claim({"red": 0, "yellow": 0, "blue": 0})
    store.record_egg_claim({"red": 0, "yellow": 0, "blue": 0}, coin=0, score=0)
    store.record_egg_claim({"red": -1, "yellow": 5, "blue": 0})
    store.record_egg_claim({}, coin=-1, score=0)
    store.record_egg_claim({})
    assert _egg_row(store, b) is None


def test_record_egg_claim_missing_keys_default_zero(store):
    b = store.current_bucket_str()
    store.record_egg_claim({"red": 4})
    assert _egg_row(store, b) == (4, 0, 0, 0, 0, 0)


def test_egg_chain_anchors_inert_in_detection_plane():
    """惰性锚点族：在 spec 有定义，但不得进检测面任何一处。"""
    anchors = _load_module_class().EGG_CHAIN_ANCHORS
    doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    perception = doc["perception"]
    spec = perception["spec"]
    for name in anchors:
        anchor = spec.get(name)
        assert isinstance(anchor, dict), f"spec 缺收尾锚点 {name}"
        assert anchor["kind"] == "template" and anchor["templates"], name
    transition_ons = {tr.get("on") for tr in perception.get("transitions") or []}
    active_names = {a for d in perception["stages"]["definitions"].values()
                    for a in (d.get("active") or [])}
    global_names = set(perception["stages"].get("global_anchors") or [])
    leaked = [n for n in anchors
              if n in transition_ons or n in active_names or n in global_names]
    assert not leaked, f"收尾锚点泄入检测面（会被每帧扫描）: {leaked}"
    order = set(perception["stages"]["order"])
    assert not (set(anchors) & {s for s in order})  # 不新增阶段（A′）


def test_egg_chain_lobby_anchor_is_global_not_lazy():
    """链尾回大厅复用全局锚点：它不属于惰性清单，两者语义不得混。

    拆分理由：惰性机检要求「不得进 global_anchors」，而链尾确实要复用它确认已回
    大厅——混在一份清单里会让两条断言互相矛盾（历史正是靠测试手抄少一项来回避，
    于是副本与真源漂移）。
    """
    mod = _load_module_class()
    lobby = mod.EGG_CHAIN_LOBBY_ANCHOR
    assert lobby not in mod.EGG_CHAIN_ANCHORS, "大厅锚点不得并入惰性清单"
    doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    perception = doc["perception"]
    assert lobby in (perception["stages"].get("global_anchors") or []), \
        f"{lobby} 应为全局锚点（链尾回大厅的确认信号）"
    assert isinstance(perception["spec"].get(lobby), dict), f"spec 缺 {lobby}"


def test_egg_anchor_repointed_to_claim_popup():
    """数蛋锚点已搬到聚合奖励弹窗（§9「搬家」）：rect 覆盖弹窗蛋卡行而非旧结算弹窗区。

    计数区几何不再进真源——由代码常量按「奖励卡通用几何」从命中框推导
    （plugins/treasure/eggs.py::CARD_COUNT_BAND，校准 tools/experiments/egg-claim-coin-read）。
    """
    doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    spec = doc["perception"]["spec"]
    egg = spec["egg"]
    assert list(egg["rect"]) == [0.2, 0.35, 0.8, 0.56]
    # 固定偏移计数区已退役：三处读数锚点均不得再携带 _count_* 参数
    for name in ("egg", "claim_coin_medal", "claim_score_medal"):
        dom = spec[name].get("domain") or {}
        assert not [k for k in dom if k.startswith("_count_")], \
            f"{name} 仍带固定偏移计数区参数：{dom}"


# ---------------------------------------------------------------------------
# 奖励卡通用几何（红→蓝→绿）：合成图单元 + 真帧端到端契约
# ---------------------------------------------------------------------------
def test_reward_card_geometry_on_synthetic_card():
    """合成一张「图标带+×N带+名称带」的奖励卡：验证外扩找蓝框与绿带切分。

    不依赖真帧与 OCR——蓝框由白色描边构成，高宽比落进卡体区间；
    命中框（图标）在蓝框内上部 → 数字带应覆盖 y≈0.62-0.805 段。
    """
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from maaracing_master.plugins.treasure.eggs import (
        CARD_COUNT_BAND, band_rect_norm, find_card_body, parse_count_text,
    )
    frame = np.zeros((720, 1281, 3), dtype=np.uint8)          # 暗背景
    cv2.rectangle(frame, (300, 254), (429, 432), (255, 255, 255), 2)  # 整卡白描边
    cv2.rectangle(frame, (330, 279), (399, 373), (200, 60, 60), -1)   # 图标（红蛋位）
    hit = (330, 279, 399, 373)
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    card = find_card_body(gray, hit, (frame.shape[1], frame.shape[0]))
    assert card is not None, "合成卡描边都没找到，几何判别过严"
    rx, ry, rw, rh = card
    assert abs(rx - 300) <= 4 and abs(ry - 254) <= 4 and abs(rw - 129) <= 6 and abs(rh - 178) <= 6
    x1n, y1n, x2n, y2n = band_rect_norm(card, CARD_COUNT_BAND,
                                        (frame.shape[1], frame.shape[0]))
    # 数字带应落在卡内 y≈0.62-0.805 段（像素 364-397）：罩住 ×N 行、避开名称带
    assert 254 + 178 * 0.58 <= y1n * 720 <= 254 + 178 * 0.66
    assert 254 + 178 * 0.76 <= y2n * 720 <= 254 + 178 * 0.85
    assert parse_count_text("×2") == 2
    assert parse_count_text("x2") == 2
    assert parse_count_text("×250,000") == 250000
    assert parse_count_text("30000") == 30000
    assert parse_count_text("红色彩蛋") is None
    assert parse_count_text("") is None


def test_egg_recognize_golden_frame_counts():
    """真帧端到端契约（第一阶段交付的教训：锚点参数必须过真帧，不再口头遵守）。

    fixtures = tools/experiments/egg-claim-coin-read/claim_popup_0609.png
    （实验校准资产，缺失即 skip；期望值来自 card_geom_probe.py 实测 5/5）。
    只验数蛋通路（含 OCR）；medal 读金额与其同机制，链路上还需实机帧。
    """
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("rapidocr")
    from maaracing_master.plugins.treasure.eggs import EggRewardRecognizer
    from maaracing_master.plugins.treasure.ocr import TreasureOcr

    fixture = (Path(__file__).resolve().parents[1]
               / "tools" / "experiments" / "egg-claim-coin-read" / "claim_popup_0609.png")
    if not fixture.exists():
        pytest.skip("实验真帧 fixtures 未入库")
    frame = cv2.cvtColor(cv2.imread(str(fixture)), cv2.COLOR_BGR2RGB)
    rec = EggRewardRecognizer(Path(__file__).resolve().parents[1],
                              ocr=TreasureOcr(Path(__file__).resolve().parents[1]))
    res = rec.recognize(frame)
    assert res is not None and rec.configured
    assert res["counts"] == {"red": 2, "yellow": 1, "blue": 2}, f"真帧数蛋漂移 {res}"
    assert len(res["eggs"]) == 3
    for e in res["eggs"]:
        assert e["count_rect"] is not None, f"{e['color']} 卡描边推导失败"
        assert e["count_text"] in ("×2", "x2", "×1", "x1", "2", "1")


# ---------------------------------------------------------------------------
# 链内点击通路：与主链路同一条出口协议（2026-09-14 修复的回归锁）
#   背景：链内曾自成一套点击出口——硬编码关闭意图开关、提交前不消费任务槽遗留
#   结果、结果不落事件与日志。下面四条各锁一条行为。
# ---------------------------------------------------------------------------
class _FakeClicker:
    """点击器桩：记录调用序列 + 固定结果，供断言协议顺序与有界重试。"""

    def __init__(self, *, mode="gamepad", busy_times=0, result=None, submit_ok=True):
        self.mode = mode
        self.intent = None
        self.calls = []
        self.last_pos = (100, 200)
        self.cancelled = 0
        self.submits = 0
        self._busy_left = busy_times
        self._result = result
        self._submit_ok = submit_ok

    def set_mode(self, mode):
        self.calls.append(("set_mode", mode))
        self.mode = mode

    def set_intent(self, intent):
        self.calls.append(("set_intent", intent))
        self.intent = intent

    @property
    def need_foreground(self):
        return self.mode == "real"

    def is_busy(self):
        self.calls.append(("is_busy",))
        if self._busy_left > 0:
            self._busy_left -= 1
            return True
        return False

    def consume_result(self):
        self.calls.append(("consume_result",))
        return self._result

    def submit_click(self, cx, cy, *, box=None, down_up_gap_ms=30, move_pause_s=0.4):
        self.calls.append(("submit_click",))
        self.submits += 1
        return self._submit_ok

    def cancel(self):
        self.cancelled += 1

    def order(self) -> list:
        return [c[0] if isinstance(c, tuple) else c for c in self.calls]


class _FakeLifecycle:
    def __init__(self):
        self.running = True

    def sleep(self, _s):
        return None

    def request_stop(self):
        self.running = False


class _FakeCtx:
    def __init__(self, intent_mode, click_mode):
        self.intent_mode = intent_mode
        self.click_mode = click_mode
        self.hwnd = 1
        self.lifecycle = _FakeLifecycle()


class _FakeChainSelf:
    """彩蛋链点击的最小桩：只喂 _egg_chain_click / _egg_chain_drain_slot 读到的字段。

    被测方法是真身（与 test_treasure_observer 同一口径）；外围（点击器、生命周期、
    遗留消费、结果记录）用桩，以便观察调用序列。CLICK_TIMEOUT 压到 0.05s，避免
    「结果迟迟不回」的分支让测试真等 6 秒。
    """

    def __init__(self, clicker, *, intent_mode=False, click_mode="gamepad"):
        mod = _load_module_class()
        self.ctx = _FakeCtx(intent_mode, click_mode)
        self._clicker_stub = clicker
        self._consumed_legacy = 0
        self._recorded = []
        self.EGG_CHAIN_CLICK_TIMEOUT_S = 0.05
        self.EGG_CHAIN_CLICK_RETRY_MAX = mod.EGG_CHAIN_CLICK_RETRY_MAX
        self.EGG_CHAIN_POLL_S = 0.0
        self.CLICK_MODE_LABELS = mod.CLICK_MODE_LABELS
        self.CLICK_DOWN_UP_GAP_MS = 30
        self.CLICK_MOVE_PAUSE_S = 0.0
        self._egg_chain_click = mod._egg_chain_click.__get__(self)
        self._egg_chain_drain_slot = mod._egg_chain_drain_slot.__get__(self)

    def _get_clicker(self):
        return self._clicker_stub

    def _ensure_gamepad_bound(self):
        return None

    def _consume_click_result(self):
        self._consumed_legacy += 1
        self._clicker_stub.consume_result()

    def _record_click(self, key, state, center, mode_label, *, ok):
        self._recorded.append((key, state, ok))


def test_egg_chain_click_follows_ctx_intent_mode():
    """链内点击跟随 GUI「仅意图」开关：不得再单方面覆盖共享 Clicker 的意图状态。"""
    clicker = _FakeClicker(result={"type": "click", "ok": True})
    fake = _FakeChainSelf(clicker, intent_mode=True)
    assert fake._egg_chain_click(0.1, 0.2, key="hall_back_btn") is True
    assert clicker.intent is True, "链内点击绕过了 GUI「仅意图」开关"
    clicker2 = _FakeClicker(result={"type": "click", "ok": True})
    fake2 = _FakeChainSelf(clicker2, intent_mode=False)
    assert fake2._egg_chain_click(0.1, 0.2, key="hall_back_btn") is True
    assert clicker2.intent is False, "开关关闭时应真实点击"


def test_egg_chain_click_drains_legacy_slot_before_submit():
    """提交前先消化主链路遗留结果：链在决策段消费点之前触发，槽里必有残留。

    真机 2026-09-14：返回键命中置信度 1.000，却因 is_busy 直接判「未点中」放弃整链。
    """
    clicker = _FakeClicker(busy_times=1, result={"type": "click", "ok": True})
    fake = _FakeChainSelf(clicker)
    assert fake._egg_chain_click(0.1, 0.2, key="hall_back_btn") is True
    assert fake._consumed_legacy >= 1, "未消化遗留结果，首次提交会被 is_busy 拒掉"
    order = clicker.order()
    assert order.index("consume_result") < order.index("submit_click")


def test_egg_chain_click_retries_submit_bounded():
    """提交持续被拒 → 有界重试（含首点共 RETRY_MAX 次），不无限空转。"""
    clicker = _FakeClicker(submit_ok=False)
    fake = _FakeChainSelf(clicker)
    assert fake._egg_chain_click(0.1, 0.2, key="hall_back_btn") is False
    assert clicker.submits == fake.EGG_CHAIN_CLICK_RETRY_MAX


def test_egg_chain_click_records_outcome():
    """链内点击结果必须留痕：此前链内点击不写事件、不打日志，真机无从取证。"""
    clicker = _FakeClicker(result={"type": "click", "ok": True})
    fake = _FakeChainSelf(clicker)
    fake._egg_chain_click(0.1, 0.2, key="hall_back_btn")
    assert fake._recorded == [("hall_back_btn", "egg_chain", True)]
