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

EGG_CHAIN_ANCHORS = (
    "hall_back_btn", "act_get_silver_btn", "egg_panel_tabbar", "egg_task_tab3",
    "egg_claim_red_btn", "egg_claim_title", "hall_home_btn",
    "claim_coin_medal", "claim_score_medal",
)

POLICY_PATH = (Path(__file__).resolve().parents[1]
               / "maaracing_master" / "plugins" / "treasure"
               / "resources" / "policy" / "treasure.policy.json")


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
    doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    perception = doc["perception"]
    spec = perception["spec"]
    for name in EGG_CHAIN_ANCHORS:
        anchor = spec.get(name)
        assert isinstance(anchor, dict), f"spec 缺收尾锚点 {name}"
        assert anchor["kind"] == "template" and anchor["templates"], name
    transition_ons = {tr.get("on") for tr in perception.get("transitions") or []}
    active_names = {a for d in perception["stages"]["definitions"].values()
                    for a in (d.get("active") or [])}
    global_names = set(perception["stages"].get("global_anchors") or [])
    leaked = [n for n in EGG_CHAIN_ANCHORS
              if n in transition_ons or n in active_names or n in global_names]
    assert not leaked, f"收尾锚点泄入检测面（会被每帧扫描）: {leaked}"
    order = set(perception["stages"]["order"])
    assert not (set(EGG_CHAIN_ANCHORS) & {s for s in order})  # 不新增阶段（A′）


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
