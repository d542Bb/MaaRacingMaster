# -*- coding: utf-8 -*-
"""speedrush HUD 实时读数（``hud.py``）的回归锁。

分两层：

- **读数层**（不依赖 maa 运行时，CI 上照跑）：区域真源机检、在场闸门、敌我归属、
  假读标记、``hud.jsonl`` 落盘与和 ``frames.jsonl`` 的对齐。一律注入**假引擎**——
  真 RapidOCR 加载要秒级，且识别结果会随数据漂移，绝不进单测。
- **接线层**（需完整运行时依赖，CI 上整类跳过）：模块在录制模式下与录制器同一时机
  启停观察者、非录制模式不起、``_state`` 暴露记录状态。

**区域真源的机检口径照 ``tools/navkit/check_truth.py`` 的 ``rect_checks()``**，且直接
复用那个函数（把区域集包成 ``{"rect": [...]}`` 形状喂进去），不在这里另写一份判据——
两份判据会各自漂移，而这一条的全部意义就是"区域集与图/policy 面同一把尺子"。
判据落在本文件而非 ``check_truth.py``：``tools/navkit/**`` 机检的是 treasure 图/policy
数据面，HUD 区域集是插件侧纯数据真源，两者不是一条链（见汇报口径）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from maaracing_master.core.ocr import OcrText
from maaracing_master.core import xinput
from maaracing_master.plugins.speedrush import HUD_REGIONS_FILE
from maaracing_master.plugins.speedrush import hud
from maaracing_master.plugins.speedrush.recorder import DriveRecorder
from tools.navkit import check_truth as ct

REPO = Path(__file__).resolve().parents[1]
REGIONS = hud.load_hud_regions()
FRAME_W, FRAME_H = 320, 180

# 旧（实验目录）区域真源路径：搬走后任何消费者都不得再引用它
_STALE_REGION_PATH = "experiments/speedrush_scoring/hud_regions.json"
_REGION_CONSUMERS = (
    REPO / "tools" / "experiments" / "speedrush_scoring" / "probe_hud_ocr.py",
    REPO / "tools" / "experiments" / "speedrush_scoring" / "README.md",
    REPO / "tools" / "navkit" / "studio_server.py",
    REPO / "tools" / "navkit" / "README.md",
    REPO / "tools" / "navkit" / "static" / "app.js",
)


def _px(rect: list[float]) -> tuple[int, int, int, int]:
    return (int(rect[0] * FRAME_W), int(rect[1] * FRAME_H),
            int(rect[2] * FRAME_W), int(rect[3] * FRAME_H))


def _frame(*, absent: tuple[str, ...] = (), sides: dict[str, tuple] | None = None):
    """造一帧：面板三格默认压在暗底（在场）；``absent`` 里的格换成亮底（面板缺席/矮版）。

    暗底/亮底是闸门的唯一判据，故"面板在不在场"在测试里就是"那格暗不暗"。
    """
    f = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    for name in absent:
        x1, y1, x2, y2 = _px(REGIONS[name])
        f[y1:y2, x1:x2] = 200
    for name, rgb in (sides or {}).items():
        x1, y1, x2, y2 = _px(REGIONS[name])
        f[y1:y2, x1:x2] = rgb
    return f


class _FakeEngine:
    """假引擎：按 rect 值给出预设文本（真模型不进单测）。"""

    def __init__(self, texts: dict[tuple, str]) -> None:
        self.texts = texts
        self.calls: list[tuple] = []

    def recognize(self, frame_rgb, rect_norm):
        self.calls.append(tuple(rect_norm))
        return OcrText(lines=(self.texts.get(tuple(rect_norm), ""),))


def _texts(**over: str) -> dict[tuple, str]:
    """默认一套"面板与比分都在场"的读数；``over`` 按字段名覆盖。"""
    base = {
        "stage": "阶段:1", "timer": "00:25", "mileage": "338", "overtake": "1",
        "total_left": "368", "score_a": "560", "score_b": "593",
        "rate_a": "41", "rate_b": "38", "event_banner": "极限超车×2",
    }
    base.update(over)
    return {tuple(REGIONS[name]): text for name, text in base.items()}


class _BadFrame:
    """一个"取 shape 即抛"的帧替身，代表运行期单帧异常（真实成因之一是通道数不对）。

    用它而不是造某种具体的坏帧：回归锁要锁的是"**异常被关在单格内**"这条契约，
    不该依赖"某种坏输入恰好能触发异常"这种会随实现漂移的细节。
    """

    @property
    def shape(self):
        raise ValueError("bad frame")


def _observer(tmp_path, engine, *, frame=None, absent=(), sides=None, **kw) -> hud.HudObserver:
    f = frame if frame is not None else _frame(absent=absent, sides=sides)
    obs = hud.HudObserver(
        tmp_path,
        frame_source=lambda: (f, 1001, 5_000_000, 3.5),
        engine=engine,
        regions=REGIONS,
        **kw,
    )
    return obs


class _MutableEngine:
    """文本可换的引擎：定值判据跨采样比较，单帧造不出它。"""

    def __init__(self, texts: dict[tuple, str]) -> None:
        self.texts = texts

    def recognize(self, frame_rgb, rect_norm) -> OcrText:
        return OcrText(lines=(self.texts.get(tuple(rect_norm), ""),))


def _walker(tmp_path, texts, *, ts0=5_000_000_000, **kw):
    """可推进的采样器：返回 ``(观察者, 引擎, state)``。

    ``state`` 里改 ``ts``（帧时刻）/ ``frame``（换画面）；``engine.texts`` 换读数。
    """
    state = {"ts": ts0, "frame": _frame(), "frame_id": 1001}
    engine = _MutableEngine(texts)
    obs = hud.HudObserver(
        tmp_path,
        frame_source=lambda: (state["frame"], state["frame_id"], state["ts"], 3.0),
        engine=engine,
        regions=REGIONS,
        **kw,
    )
    return obs, engine, state


def _tick(state, seconds: float = 0.5) -> None:
    state["ts"] += int(seconds * 1e9)


def _sample(obs: hud.HudObserver) -> dict:
    rec = obs._sample_once()
    assert rec is not None, "帧在场时采样必须产出记录"
    return rec


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ----------------------------------------------------------------------
# 区域真源：唯一真源 + 几何机检（口径 = check_truth.rect_checks）
# ----------------------------------------------------------------------


class TestRegionTruth:
    def test_geometry_passes_rect_checks(self) -> None:
        """区域集喂进 rect_checks() 必须零错误——与图/policy 面同一把尺子。"""
        wrapped = {"speedrush_hud": {n: {"rect": r} for n, r in REGIONS.items()}}
        assert ct.rect_checks({}, wrapped) == []

    def test_region_keys_carry_no_identity_assumption(self) -> None:
        """区域键只描述**槽位/序号**，不得承载敌我或位置语义。

        假设写进名字，读取路径就会按假设分支：`score_self/opp` 与 `score_top/bottom` 各诱导过
        一次错判（见 `plugins/speedrush/CODE_WIKI.md` §5「读数结论的标尺与纪律」第 3 条）。
        敌我由数据里的 `side` 承担，名字里不许出现。
        """
        forbidden = {"self", "opp", "own", "mine", "enemy", "ally", "top", "bottom",
                     "upper", "lower", "left_side", "right_side"}
        for name in REGIONS:
            assert not (set(name.lower().split("_")) & forbidden), \
                f"{name} 的键名带身份/位置语义——改用槽位名或序号"

    @pytest.mark.parametrize("bad,needle", [
        ([0.9, 0.1, 0.2, 0.2], "x1<x2"),          # x 反序
        ([0.1, 0.9, 0.2, 0.2], "x1<x2"),          # y 反序
        ([0.1, 0.1, 0.2], "4 元数值"),              # 非 4 元
        ([0.1, 0.1, 0.2, 1.2], "[0,1]"),           # 越界
        ([0.1, "x", 0.2, 0.3], "4 元数值"),          # 非数值
    ])
    def test_loader_rejects_bad_geometry(self, tmp_path, bad, needle) -> None:
        """非法几何必须 fail loud（越界 rect 运行时静默读错区域，是最难发现的一类错）。"""
        f = tmp_path / "hud_regions.json"
        f.write_text(json.dumps({**{n: list(r) for n, r in REGIONS.items()},
                                 "mileage": bad}), encoding="utf-8")
        with pytest.raises(ValueError, match=needle):
            hud.load_hud_regions(f)

    def test_loader_rejects_comment_key_and_missing_semantic_fields(self, tmp_path) -> None:
        """真源是纯数据：说明键与"缺语义必填字段"都要拦（缺了比分格，敌我判据只能整段弃权）。"""
        comment = tmp_path / "a.json"
        comment.write_text(json.dumps({"_note": "说明", **REGIONS}), encoding="utf-8")
        with pytest.raises(ValueError, match="说明键"):
            hud.load_hud_regions(comment)
        missing = tmp_path / "b.json"
        missing.write_text(json.dumps({k: v for k, v in REGIONS.items()
                                       if k != "score_a"}), encoding="utf-8")
        with pytest.raises(ValueError, match="语义必需字段"):
            hud.load_hud_regions(missing)

    def test_default_path_is_the_plugin_truth(self) -> None:
        assert hud.load_hud_regions() == REGIONS
        assert HUD_REGIONS_FILE.is_file()

    def test_no_stale_region_path_remains(self) -> None:
        """区域真源只留一份：旧路径的文件已删，且各消费者不再引用它。"""
        assert not (REPO / "tools" / _STALE_REGION_PATH).exists()
        offenders = [str(f) for f in _REGION_CONSUMERS
                     if _STALE_REGION_PATH in f.read_text(encoding="utf-8")]
        assert not offenders, f"仍引用已搬走的区域真源：{offenders}"

    def test_every_region_is_consumed(self, tmp_path) -> None:
        """采样范围 == 真源键集 − 跳过名单（派生断言，不另抄一份字段清单）。"""
        rec = _sample(_observer(tmp_path, _FakeEngine(_texts())))
        assert set(rec["fields"]) == set(REGIONS) - set(hud.SKIPPED_FIELDS)
        assert rec["regions"] == hud.region_digest(REGIONS)


# ----------------------------------------------------------------------
# 读数可用性与敌我归属
# ----------------------------------------------------------------------


class TestReadingJudgements:
    def test_side_attribution_lands_in_record(self, tmp_path) -> None:
        """比分面板上下互换 → 归属由成对判给出，并逐格写进记录。"""
        sides = {"score_a": (30, 60, 220), "score_b": (220, 60, 30)}
        rec = _sample(_observer(tmp_path, _FakeEngine(_texts()), sides=sides))
        assert rec["fields"]["score_a"]["side"] == "本机(蓝)"
        assert rec["fields"]["score_b"]["side"] == "对手(红)"
        # 位置与敌我解耦：读数照读，归属单列
        assert rec["fields"]["score_a"]["value"] == 560

    def test_pair_abstains_on_unsaturated_band(self, tmp_path) -> None:
        """低饱和带（天空/路面）判不出归属 → 显式 ``?``，不猜。"""
        gray = np.full((FRAME_H, FRAME_W, 3), 120, dtype=np.uint8)
        rec = _sample(_observer(tmp_path, _FakeEngine(_texts()), frame=gray))
        assert rec["fields"]["score_a"]["side"] == "?"

    def test_timer_value_is_seconds_not_first_int(self, tmp_path) -> None:
        """计时格按秒记：'04:46' 走通用取整会得到 4（分钟）——那是"读得出但解释错"的陷阱。"""
        rec = _sample(_observer(tmp_path, _FakeEngine(_texts(timer="04:46"))))
        timer = rec["fields"]["timer"]
        assert timer["value"] == 4 * 60 + 46
        assert timer["text"] == "04:46"          # 原始文本照旧完整
        assert hud.parse_value("stage", "阶段:2") == 2

    def test_missing_frame_is_counted_not_written(self, tmp_path) -> None:
        """缺帧不写行（seq 缺口即证据），计数进 stats。"""
        obs = hud.HudObserver(tmp_path, frame_source=lambda: (None, 0, 0, float("inf")),
                              engine=_FakeEngine(_texts()), regions=REGIONS)
        assert obs._sample_once() is None
        assert obs.stats["samples_no_frame"] == 1
        assert obs.stats["rows_written"] == 0

    def test_ocr_failure_degrades_without_raising(self, tmp_path) -> None:
        """引擎不可用 → 记 ocr_unavailable、不给可信值，绝不抛。"""
        class _Dead:
            def recognize(self, frame_rgb, rect_norm):
                return None

        rec = _sample(_observer(tmp_path, _Dead()))
        assert rec["fields"]["stage"]["trusted"] is False
        assert "ocr_unavailable" in rec["fields"]["stage"]["note"]

    def test_one_bad_field_does_not_kill_the_row(self, tmp_path) -> None:
        """单格读数异常只废掉那一格：记 read_error、不给可信值，且有累计计数。

        成因不是假想：引擎对某格抛一次（模型/内存/尺寸都可能），异常若逃到采样线程，
        **整条观察会静默死掉**——实机上的表现是"跑了一整轮却只有几行"，而 meta 里没有
        任何一项指向"线程死了"，事后无从归因。
        """
        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        obs = _observer(tmp_path, _FakeEngine(_texts()))
        obs._read_field = _boom  # type: ignore[method-assign]
        entry = obs._read_field_safe(_frame(), "stage", REGIONS["stage"])
        assert entry["trusted"] is False and "read_error" in entry["note"]
        assert obs.stats["read_errors"] == 1

    def test_bad_frame_degrades_to_abstention(self, tmp_path) -> None:
        """整帧异常不终止观察：归属整拍**弃权**，行照旧落盘、还能继续采。

        判归属要碰帧（解 shape、取通道），故坏帧下右侧四格一律 ``side=?`` 且
        ``side_source=abstain``——既不冒充一个身份，也不谎报"读错"（成对判自己吞异常）。
        其余格子只把 rect 交给引擎，不受影响。
        """
        obs = _observer(tmp_path, _FakeEngine(_texts()), frame=_BadFrame())
        for _ in range(2):
            rec = _sample(obs)
            for name in (*hud.SIDE_FIELDS, *hud.RATE_FIELDS):
                entry = rec["fields"][name]
                assert entry["side"] == "?" and entry["side_source"] == "abstain"
            assert rec["fields"]["timer"]["value"] is not None, "不碰帧的格子不该被牵连"
            assert rec["regions"], "区域指纹照旧入行（这行读的是哪套框仍可查）"

    def test_non_three_channel_frame_never_lies_about_side(self) -> None:
        """非 3 通道帧的归属说"判不出"，而不是把通道错位分组后给一个看着合理的错标签。"""
        f = np.zeros((FRAME_H, FRAME_W, 4), dtype=np.uint8)
        assert hud.pair_sides(f, REGIONS["score_a"], REGIONS["score_b"]) == ("?", "?")


# ----------------------------------------------------------------------
# 归属：面板半透明 → 必须**成对判**（单块绝对阈值在公共偏色下会两块同判）
# ----------------------------------------------------------------------


class TestSidePairJudgement:
    """实测现场（2026-09-17 白天蓝天场）：两块条带的饱和像素**都偏蓝**。

    单块绝对判据于是把两块都判成"本机"（那一场 62/69 行都错，下游据此产出 51 处假"比分
    回落"——两个块的值被混进同一条序列）。物理约束是一蓝一红，故用**两块之差**定归属。
    """

    def test_pair_recovers_attribution_under_common_bias(self, tmp_path) -> None:
        """两块都偏蓝时，更蓝的那块是本机。"""
        # 上块 B−R=+90（被蓝天染过），下块 +180（本机色条更实）
        sides = {"score_a": (60, 90, 150), "score_b": (20, 80, 200)}
        rec = _sample(_observer(tmp_path, _FakeEngine(_texts()), sides=sides))
        assert rec["fields"]["score_a"]["side"] == "对手(红)"
        assert rec["fields"]["score_b"]["side"] == "本机(蓝)"
        assert rec["fields"]["score_a"]["side_source"] == "pair"

    def test_pair_abstains_when_two_bands_are_alike(self, tmp_path) -> None:
        """差值太小 → 两块都弃权：多半是面板淡入/淡出，这一刻没有可用信号。"""
        sides = {"score_a": (30, 60, 140), "score_b": (32, 61, 141)}
        rec = _sample(_observer(tmp_path, _FakeEngine(_texts()), sides=sides))
        assert rec["fields"]["score_a"]["side"] == "?"
        assert rec["fields"]["score_b"]["side"] == "?"

    def test_unsaturated_band_abstains_in_pair_too(self) -> None:
        """低饱和带（天空/路面）连单块判都判不出 → 成对判同样弃权，不拿噪声凑。"""
        gray = np.full((FRAME_H, FRAME_W, 3), 120, dtype=np.uint8)
        assert hud.pair_sides(gray, REGIONS["score_a"], REGIONS["score_b"]) == ("?", "?")

    def test_pair_abstains_when_a_block_is_missing(self, tmp_path) -> None:
        """区域集缺一块（成对判无从谈起）时**弃权**，不拿单块绝对阈值凑一个身份。

        单块绝对判据在公共偏色下会两块同判（见上一条锁），那是本模块证伪过的形态，故运行期
        没有它；`side_source` 如实记 `abstain`——留着这个值是为了让"这一拍的归属为什么空"
        在事后可归因。
        """
        only = {n: REGIONS[n] for n in ("stage", "timer", "score_a")}
        obs = hud.HudObserver(
            tmp_path,
            frame_source=lambda: (_frame(sides={"score_a": (30, 60, 220)}), 1001, 5_000_000, 3.5),
            engine=_FakeEngine(_texts()), regions=only)
        entry = _sample(obs)["fields"]["score_a"]
        assert entry["side"] == "?"
        assert entry["side_source"] == "abstain"

    def test_enemy_rate_slot_is_not_read(self, tmp_path) -> None:
        """**对手那一格没有得分速度**：不识别、标 `not_displayed`；本方那一格照常读。

        两种朝向都验（蓝在上 / 蓝在下各一次）：速度格跟随**同槽位比分格**的敌我，而不是
        槽位本身——按槽位读会在互换后去读对手的空区（实测那块能读出背景值，看着像读数）。
        """
        for blue_top in (True, False):
            top = (30, 60, 220) if blue_top else (220, 60, 30)
            bot = (220, 60, 30) if blue_top else (30, 60, 220)
            engine = _FakeEngine(_texts())
            rec = _sample(_observer(tmp_path, engine, sides={"score_a": top, "score_b": bot}))
            own, enemy = (("rate_a", "rate_b") if blue_top else ("rate_b", "rate_a"))
            assert rec["fields"]["score_a"]["side"] == ("本机(蓝)" if blue_top else "对手(红)")
            assert rec["fields"][own]["side"] == "本机(蓝)"
            assert rec["fields"][own]["trusted"] is True
            assert rec["fields"][own]["value"] in (41, 38)     # 两槽各有自己的文本
            assert rec["fields"][enemy]["trusted"] is False
            assert "not_displayed" in rec["fields"][enemy]["note"]
            assert tuple(REGIONS[enemy]) not in engine.calls, "对手那一格不该被识别"

    def test_rate_slot_not_read_when_side_unknown(self, tmp_path) -> None:
        """归属判不出时同样不读速度格：不知道这一槽是谁的，就没理由把那里的像素当读数。"""
        engine = _FakeEngine(_texts())
        gray = np.full((FRAME_H, FRAME_W, 3), 120, dtype=np.uint8)
        rec = _sample(_observer(tmp_path, engine, frame=gray))
        for name in hud.RATE_FIELDS:
            assert rec["fields"][name]["trusted"] is False
            assert "side_unknown" in rec["fields"][name]["note"]
            assert tuple(REGIONS[name]) not in engine.calls


# ----------------------------------------------------------------------
# 落盘 + 与录制帧对齐
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_pad(monkeypatch):
    """隔离真实 XInput（本文件的录制器夹具也会起手柄采样线程）。"""
    monkeypatch.setattr(xinput, "read_state", lambda index: None)


def _wait_rows(obs: hud.HudObserver, n: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while obs.stats["rows_written"] < n and time.monotonic() < deadline:
        time.sleep(0.02)
    assert obs.stats["rows_written"] >= n, "观察线程未产出足够的行"


class TestHudJsonl:
    def test_rows_align_with_recorded_frames(self, tmp_path) -> None:
        """⑤ hud.jsonl 与同会话 frames.jsonl 按 frame_id / ts_ns 对齐。

        frames.jsonl 由**真录制器**写出（形状即真源），观察线程拿同一批
        ``(frame_id, ts_ns)`` 当帧源——对齐成立才谈得上"事后按帧复查读数"。
        """
        sess = tmp_path / "20260917_120000_p1"
        pairs = [(1001, 5_000_000), (1002, 5_040_000_000), (1003, 5_080_000_000)]
        rec = DriveRecorder(sess, pad_poll_hz=50.0, phase=1, round_no=1)
        rec.start()
        for fid, ts in pairs:
            rec.record_frame(np.zeros((FRAME_H, FRAME_W, 3), np.uint8), frame_id=fid, ts_ns=ts)
        rec.stop()

        it = iter(pairs)

        def src():
            fid, ts = next(it, pairs[-1])
            return (_frame(), fid, ts, 2.0)

        # 合计故意读成 2（矮版假读）：线上必须既产行、又带标记
        obs = hud.HudObserver(sess, frame_source=src, engine=_FakeEngine(_texts(total_left="2")),
                              regions=REGIONS, interval_s=0.05, phase=1, round_no=1)
        obs.start()
        _wait_rows(obs, len(pairs))
        obs.stop("phase_end")

        frames = _read_jsonl(sess / "frames.jsonl")
        assert {r["frame_id"] for r in frames} == {fid for fid, _ in pairs}
        idx = {r["frame_id"]: r["ts_ns"] for r in frames}

        rows = _read_jsonl(sess / "hud.jsonl")
        assert rows, "hud.jsonl 无行"
        # 每行的帧号必须能在录制索引里找到，且采集时刻逐值相等（同源同帧）
        for r in rows:
            assert r["frame_id"] in idx, f"帧号 {r['frame_id']} 不在 frames.jsonl 里"
            assert r["ts_ns"] == idx[r["frame_id"]]
        assert {r["frame_id"] for r in rows} == {fid for fid, _ in pairs}
        # 原始文本逐格齐全（可审计）；读到的格 == 真源键集 − 跳过名单
        assert [n for n in rows[0]["fields"]] == [n for n in REGIONS
                                                  if n not in hud.SKIPPED_FIELDS]
        # 读到的格带原始文本；速度格可能整格不读（归属判不出 / 对手那一槽不显示），text 为 None
        assert isinstance(rows[0]["fields"]["stage"]["text"], str)

        meta = json.loads((sess / "hud_meta.json").read_text(encoding="utf-8"))
        assert meta["schema"] == hud.SCHEMA_VERSION
        assert meta["regions"] == hud.region_digest(REGIONS)
        assert meta["phase"] == 1 and meta["round_no"] == 1
        assert meta["rows_written"] == len(rows)
        assert meta["stop_reason"] == "phase_end"
        # 交付物不落本机绝对路径
        assert "/" not in meta["regions_file"] and "\\" not in meta["regions_file"]

    def test_stop_is_idempotent_and_queue_drop_does_not_block(self, tmp_path) -> None:
        """反压口径照录制器：队列满即丢并计数，绝不阻塞采样侧；stop 幂等。"""
        obs = _observer(tmp_path, _FakeEngine(_texts()))
        rec = obs._sample_once()
        assert rec is not None
        for _ in range(obs._q.maxsize):
            obs._q.put_nowait(rec)
        obs._enqueue(rec)            # 队满 → 丢并计数，不等待
        assert obs._rows_dropped == 1
        assert obs._q.qsize() == obs._q.maxsize

        obs2 = hud.HudObserver(tmp_path / "s2", frame_source=lambda: (None, 0, 0, 0.0),
                               engine=_FakeEngine(_texts()), regions=REGIONS, interval_s=0.05)
        obs2.start()
        obs2.stop()
        obs2.stop()                  # 重复调用不得抛、不得重写 meta
        assert obs2.running is False


# ----------------------------------------------------------------------
# 模块接线（需完整运行时依赖）
# ----------------------------------------------------------------------

try:
    from maaracing_master.plugins.speedrush import module as sr

    _MOD_OK, _MOD_ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺重依赖的机器上整类 SKIP
    _MOD_OK, _MOD_ERR = False, str(exc)
    sr = None  # type: ignore[assignment]


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t


class _FakeLifecycle:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.running = True

    def sleep(self, seconds: float) -> bool:
        self._clock.t += seconds
        return self.running


class _FakeCapture:
    def __init__(self) -> None:
        self._fid = 0

    def screenshot(self):
        return np.zeros((8, 8, 3), dtype=np.uint8)

    def frame_with_age(self):
        self._fid += 1
        return (np.zeros((8, 8, 3), dtype=np.uint8), self._fid, 1000 * self._fid, 1.5)


class _FakeCtx:
    def __init__(self, clock: _Clock) -> None:
        self.lifecycle = _FakeLifecycle(clock)
        self.capture = _FakeCapture()


class _FakeGraph:
    def __init__(self, seq: list[bool]) -> None:
        self.seq = list(seq)
        self.calls = 0

    def run(self, entry: str, reached: str | None = None) -> bool:
        v = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        return v


class _SpyObserver:
    """替身观察者：只记"被建/被启/被停"，避免单测里起真 OCR 线程。"""

    instances: list["_SpyObserver"] = []

    def __init__(self, out_dir, **kw) -> None:
        self.out_dir = Path(out_dir)
        self.kw = kw
        self.started = False
        self.stopped_reason: str | None = None
        self.running = False
        _SpyObserver.instances.append(self)

    @property
    def stats(self) -> dict[str, int]:
        return {"rows_written": 0, "rows_dropped": 0,
                "samples_no_frame": 0, "ocr_no_result": 0}

    def start(self) -> None:
        self.started = True
        self.running = True

    def stop(self, reason: str = "normal") -> None:
        self.stopped_reason = reason
        self.running = False


@pytest.mark.skipif(not _MOD_OK, reason=f"需要完整运行时依赖：{_MOD_ERR}")
class TestModuleWiring:
    @pytest.fixture(autouse=True)
    def _spy(self, monkeypatch):
        _SpyObserver.instances = []
        monkeypatch.setattr(sr, "HudObserver", _SpyObserver)
        return _SpyObserver

    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        clock = _Clock()
        monkeypatch.setattr(sr, "time", clock)
        monkeypatch.setattr(sr, "data_dir", lambda: tmp_path / "data")
        ctx = _FakeCtx(clock)
        mod = sr.SpeedRushModule(ctx)  # type: ignore[arg-type]
        mod._running = True
        mod._graph = _FakeGraph([True, True, True, False, False])
        return mod, ctx

    def test_record_mode_starts_hud_with_recorder(self, env, _spy) -> None:
        """⑥（对偶）录制模式：观察者与录制器同一时机启停、拿到采集入口、退出即释放。"""
        mod, ctx = env
        mod._record_mode = True
        assert mod._drive(1) is True

        assert len(_spy.instances) == 1
        obs = _spy.instances[0]
        assert obs.started and obs.stopped_reason == "phase_end"
        # 与录制器同一会话目录（对齐目标就是同会话的 frames.jsonl）
        assert obs.out_dir.parent == sr.data_dir() / "speedrush" / "demos"
        assert (obs.out_dir / "frames.jsonl").is_file()
        # 取帧走 frame_with_age（帧号/采集时刻的唯一来源），不是 screenshot
        assert obs.kw["frame_source"].__self__ is ctx.capture
        assert obs.kw["phase"] == 1 and obs.kw["round_no"] == 1
        # 退出后必须释放引用（否则 _state.hud_recording 会永远为真）
        assert mod._hud is None

    def test_non_record_mode_starts_no_hud(self, env, _spy) -> None:
        """⑥ 非录制模式（无会话目录、无可对齐的帧）→ 观察线程不起，也不落盘。"""
        mod, _ = env
        mod._record_mode = False
        assert mod._drive(1) is True
        assert _spy.instances == []
        assert mod._hud is None
        assert not (sr.data_dir() / "speedrush" / "demos").exists()

    def test_state_exposes_hud_recording(self, env) -> None:
        """``_state`` 按录制器的既有做法暴露是否在记录。"""
        mod, _ = env
        state = mod.get_module_config()["_state"]
        assert state["hud_recording"] is False
        assert state["hud_rows"] == 0

        spy = _SpyObserver(Path("."), phase=1)
        spy.running = True
        mod._hud = spy  # type: ignore[assignment]
        state = mod.get_module_config()["_state"]
        assert state["hud_recording"] is True
        assert state["hud_rows"] == 0