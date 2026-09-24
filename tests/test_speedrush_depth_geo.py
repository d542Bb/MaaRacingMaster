# -*- coding: utf-8 -*-
"""深度几何观测层（depth_geo）的回归锁。

两层用例：
- **合成**（零数据依赖）：锁读法机制——全局 q20 把少数派路面钉成地面
  （中位数会被人行道捕获，518 帧实测三群体值）、直路双边界的读数与侧别、
  双守卫的逐个拒绝行为（收敛/侧别/远带）。合成深度图按透视口径构造：
  边界线过 (vpx, y_h)，路面占统计列 ≥20%（q20 适用条件，见 _ground_q20 注）。
- **数据锚点**（skipif：无离线缓存则跳）：锁定骑路缘 518（L 人行道 + R 真右缘，
  出租车行被稳健拟合剔除）与贴护栏 437（R 在场 + L 翻面块被拒）的守卫裁决——
  标定锚点（conv≤350 / resid≤45 / 侧别 0.15 / 源行≥20 且 y−y_h≥FLOOR）不许漂。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from maaracing_master.plugins.speedrush.depth_geo import (
    AsyncDepthRoadObserver, FIT_FLOOR_PX, DepthRoadObserver, DepthRoadReading,
    _ground_q20, _rel_and_blocks, reading_from_map)
from maaracing_master.plugins.speedrush.world_model import load_calib

CAL = load_calib()
ROAD0, ROAD_SLOPE = 4.0, 1.0 / 375.0     # 路面视差 c(y)：y340→4.0、y714→5.0
RAISE = 1.12                             # 边界外抬升面（+12% > 门 8%）

NPY = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
       / "speedrush" / "depth_review" / "npy")


def _road(y: int) -> float:
    return ROAD0 + (y - 340) * ROAD_SLOPE


def _base_map() -> np.ndarray:
    """全路面楔形（近带占统计列 ≥20%，q20 可钉住）。"""
    ys = np.arange(720)
    return (ROAD0 + (ys - 340).clip(0)[:, None] * ROAD_SLOPE).astype(np.float32) \
        * np.ones((1, 1280), np.float32)


def _draw_raised(m: np.ndarray, inner, side: str, y0: int, y1: int) -> None:
    """画抬升块：内沿 x=inner(y)，L 侧填 [0,x)、R 侧填 [x,1280)，值=c(y)·RAISE。"""
    for y in range(y0, y1):
        xi = int(round(inner(y)))
        if side == "L":
            m[y, :max(xi, 0)] = _road(y) * RAISE
        else:
            m[y, min(xi, 1280):] = _road(y) * RAISE


def _vp_line(k: float, side: str):
    """过 VP 的边界线：x = vpx ± k·(y−y_h)。"""
    sgn = -1.0 if side == "L" else 1.0
    return lambda y: CAL.vpx + sgn * k * (y - CAL.y_h)


# ── 读法机制 ────────────────────────────────────────────────────────────

def test_ground_q20_pins_minority_road_median_fails():
    """行内路面是少数派时：q20 钉住路面，中位数被人行道捕获（518 实测值）。"""
    m = np.full((720, 1280), 5.316, np.float32)
    m[:, :300] = 5.551                       # 人行道（统计列内 301）
    m[:, 301:540] = 5.316                    # 路面（统计列内 239，22%）
    m[:, 740:1000] = 5.551                   # 人行道（261）
    m[:, 1000:] = 6.148                      # 出租车（280）
    g = _ground_q20(m)
    assert abs(float(g[600 - 340]) - 5.316) < 1e-3      # q20 = 路面
    med = np.median(m[600, np.r_[0:540, 740:1280]])
    assert abs(float(med) - 5.551) < 1e-3               # 中位数 = 人行道（失效）


def test_straight_road_both_edges_detected():
    """直路双边界：双侧在场、读数落在真线上（源行中位评估）。

    注：合成楔形路在 y<544 占统计列 <20%，q20 适用条件外块不成立——
    评估行取**当选块**的源行中位，不是假设全带都是块。"""
    m = _base_map()
    _draw_raised(m, _vp_line(0.95, "L"), "L", 340, 715)
    _draw_raised(m, _vp_line(0.95, "R"), "R", 340, 715)
    rd = reading_from_map(m, CAL, None)
    assert rd.sides == 2
    _, blocks = _rel_and_blocks(m, None)
    bR = max((b for b in blocks if b[5]), key=lambda b: b[1] - b[0])
    y_ref = float(np.median([y for y in bR[3] if y >= CAL.y_h + FIT_FLOOR_PX]))
    x_l = CAL.vpx - 0.95 * (y_ref - CAL.y_h)
    x_r = CAL.vpx + 0.95 * (y_ref - CAL.y_h)
    assert abs(rd.left_x - x_l) <= 8
    assert abs(rd.right_x - x_r) <= 8
    assert rd.left_edge_lane <= -0.15
    assert rd.right_edge_lane >= 0.15


# ── 双守卫的拒绝行为 ────────────────────────────────────────────────────

def test_conv_guard_rejects_nonconverging_block():
    """竖直内沿不过 VP（外推偏移 >350px）→ R 弃权，原因含 conv。"""
    m = _base_map()
    _draw_raised(m, lambda y: 1000.0, "R", 550, 715)
    rd = reading_from_map(m, CAL, None)
    assert rd.right_edge_lane is None
    assert any("conv" in r for r in rd.rejects)


def test_side_guard_rejects_crossing_block():
    """横贯块的内沿落在画面中央（lane≈0）→ L 侧别拒；右缘不触边无 R 候选。"""
    m = _base_map()
    for y in range(550, 715):
        m[y, :1000] = _road(y) * RAISE       # 触左缘、内沿 x=999 ≈ 中央
    rd = reading_from_map(m, CAL, None)
    assert rd.left_edge_lane is None
    assert any("lane" in r for r in rd.rejects)
    assert rd.right_edge_lane is None


def test_far_band_block_rejected():
    """源行不足（块行全在发散区 y−y_h<FLOOR）→ _fit 判 dead（读数带重定语义）。"""
    from maaracing_master.plugins.speedrush.depth_geo import _fit
    inner = {y: 1200 for y in range(340, int(CAL.y_h + FIT_FLOOR_PX))}
    f = _fit(inner, CAL)
    assert f is not None and f["dead"]
    assert f["near"] == 0


def test_baseline_guard_rejects_soft_gate():
    """内沿内侧 40~120px 本底窗被垫到 3%（>门/2、<门）→ 门失效弃权。

    复刻第一关终选的跨档失效形态（块从路面里起跳、边界=松门交点）：
    守卫让失去物理语义的门主动交出决策权，而不是继续伪装成功。"""
    m = _base_map()
    inner = _vp_line(0.95, "R")
    _draw_raised(m, inner, "R", 340, 715)
    # 本底窗跟随源行中位评估行（y_ref±60）；垫高 3% > 门/2(4%)? 门 0.08 → 垫到 5%
    y_ref = int(np.median([y for y in range(340, 715) if y >= CAL.y_h + FIT_FLOOR_PX]))
    for y in range(y_ref - 60, y_ref + 60):
        x0 = int(round(inner(y)))
        m[y, x0 - 120:x0 - 40] = _road(y) * 1.05      # 松门本底（<门 8% 不自成块）
    rd = reading_from_map(m, CAL, None)
    assert rd.right_edge_lane is None
    assert any("门本底" in r for r in rd.rejects)


# ── 资产与会话降级 ──────────────────────────────────────────────────────

def test_ego_mask_asset_loads():
    """挖除区 = 上半身矩形（y351~532）∪ 车带下延（同列带到底，皮肤无关段）。"""
    mask = DepthRoadObserver._load_ego_mask()
    assert mask is not None and mask.shape == (720, 1280)
    assert mask[351:532, 512:765].all()          # 上半身高区
    assert mask[532:715, 512:765].all()          # 车带下延（粘连带整体排除）
    assert not mask[:351, 512:766].any()         # 上缘之上不挖
    assert not mask[:, :512].any() and not mask[:, 766:].any()


def test_carband_breaks_tail_bridge():
    """车尾粘连（内沿在 y540~565 段冲进中央列带）被车带下延挖除救回。

    不挖除时粘连段污染内沿拟合（残差爆、整块被拒）；挖除后内沿回归
    真边界线——按"真边界不进中央带"的结构事实泛化，无需精细车形。"""
    inner = _vp_line(0.95, "L")
    m = _base_map()
    _draw_raised(m, inner, "L", 340, 715)
    for y in range(540, 566):                    # 车尾粘连：内沿冲到中央带
        m[y, :700] = _road(y) * RAISE
    m[533:715, 512:765] = _road(600) * RAISE * 2  # 车身高读数（挖除区形状）
    rd = reading_from_map(m, CAL, DepthRoadObserver._load_ego_mask())
    assert rd.left_edge_lane is not None and rd.left_edge_lane <= -0.15
    rd_noguard = reading_from_map(m, CAL, None)
    assert rd_noguard.left_edge_lane is None      # 不挖除：粘连桥污染被守卫拒


def test_observer_without_session_returns_none():
    """权重缺失（session=None）时 observe 恒 None——road_offset 退纯模型积分。"""
    obs = DepthRoadObserver(None, CAL)
    assert obs.observe(np.zeros((720, 1280, 3), np.uint8)) is None


# ── 数据锚点（守卫标定不许漂；无离线缓存则跳）──────────────────────────
#
# 两层各锁一事：
# - @518 缓存 + 显式 gate=0.02：守卫标定锚点（@518 参照口径下守卫的裁决）；
# - @336 q4f16 缓存 + 生产默认 gate：落档后的生产行为（含本底守卫在
#   @336 本底抬升下的弃权形态）。

def _cached_map(key: str) -> np.ndarray | None:
    p = NPY / f"{key}__da2s.npy"
    if not p.exists():
        return None
    return np.load(p).astype(np.float32)


def _cached_336(key: str) -> np.ndarray | None:
    p = NPY / f"{key}__d336q4f16.npy"
    if not p.exists():
        return None
    return np.load(p).astype(np.float32)


@pytest.mark.skipif(_cached_map("frames__000518") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_518_straddle():
    """@518 参照口径（gate=0.02）：骑路缘 518 双侧在场。

    源行规则（2026-09-24）下的行为升级并案：L=人行道右缘 −0.394（旧锚点值不变）；
    R=+0.90 真右缘在场——出租车污染的 375~440 行被两轮稳健拟合剔除（留点 416~714、
    resid 6.5、conv 137 收敛），旧近带规则在此整侧弃权。钉住"剔点留线"的行为。"""
    m = _cached_map("frames__000518")
    rd = reading_from_map(m, CAL, DepthRoadObserver._load_ego_mask(), gate=0.02)
    assert rd.sides == 2
    assert abs(rd.left_edge_lane - (-0.394)) < 0.05
    assert abs(rd.right_edge_lane - 0.897) < 0.05
    assert rd.rejects == ()


@pytest.mark.skipif(_cached_map("frames__000437") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_437_wallhug():
    """@518 参照口径（gate=0.02）：贴护栏 437 R=护栏基部在场，L 翻面块被拒。"""
    m = _cached_map("frames__000437")
    rd = reading_from_map(m, CAL, DepthRoadObserver._load_ego_mask(), gate=0.02)
    assert rd.right_edge_lane is not None and rd.right_edge_lane > 0.15
    assert rd.left_edge_lane is None
    assert any(r.startswith("L:") for r in rd.rejects)


@pytest.mark.skipif(_cached_336("wallhug_437") is None,
                    reason="需 @336 q4f16 离线缓存（rescale_gate_336.py infer）")
def test_anchor_336_production_gate():
    """@336 生产口径（gate=0.08）：贴墙场景门半失效（本底抬升）→ 诚实弃权。

    锁定落档形态：贴护栏帧在本档退纯模型积分（守卫交权），而非给出松门读数。"""
    m = _cached_336("wallhug_437")
    rd = reading_from_map(m, CAL, DepthRoadObserver._load_ego_mask())
    assert rd.sides == 0
    assert any("门本底" in r for r in rd.rejects)


# ── 折叠锁（时延悬案的直接机制，不许静默回退）──────────────────────────

def _load_session_or_skip():
    from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE
    from maaracing_master.plugins.speedrush.depth_geo import load_session
    if not DEPTH_MODEL_FILE.exists():
        pytest.skip("深度权重未随检出提供")
    try:
        return load_session(DEPTH_MODEL_FILE)
    except Exception as exc:  # noqa: BLE001 —— 无 DML 且 CPU EP 起不了 q4f16 的环境
        pytest.skip(f"本环境无法建会话: {exc!r}")


def test_load_session_is_folded_static_shape():
    """load_session 的输入维必须是编译期常量（batch/height/width 三 free dim 全固定）。

    漏折叠的形态是"能跑但慢 6 倍"（cubic Resize 落 CPU，@336 实测 133 vs 20ms，
    2026-09-24 实机复核收口）——性能回退不会红任何功能测试，只能由这条形状锁拦。
    只固定 height/width 而漏 batch_size 同样锁得住：那时图不变、维仍是符号名。"""
    dims = _load_session_or_skip().get_inputs()[0].shape
    assert all(isinstance(d, int) for d in dims), f"输入维未静态化（折叠未生效）: {dims}"
    assert tuple(dims) == (1, 3, 336, 588)


# ── 异步解耦协议（AsyncDepthRoadObserver，stub 零数据依赖）────────────────

import time as _time  # noqa: E402


def _mk_reading(lane: float = -0.4) -> DepthRoadReading:
    return DepthRoadReading(left_edge_lane=lane, right_edge_lane=None,
                            left_x=300.0, right_x=None, sides=1,
                            latency_ms=1.0, rejects=("R:远带",))


class _StubObserver:
    """DepthRoadObserver 替身：按脚本回放 读数/None/异常，记录调用。"""

    def __init__(self, script, session_ready: bool = True) -> None:
        self._script = list(script)
        self.session_ready = session_ready
        self.calls = 0

    def observe(self, frame):
        self.calls += 1
        item = self._script.pop(0) if self._script else None
        if isinstance(item, Exception):
            raise item
        return item


def _wait_until(pred, timeout=2.0) -> bool:
    end = _time.perf_counter() + timeout
    while _time.perf_counter() < end:
        if pred():
            return True
        _time.sleep(0.005)
    return False


def _frame() -> "object":
    import numpy as np
    return np.zeros((720, 1280, 3), np.uint8)


def test_async_roundtrip_single_consume():
    """push→worker→take 闭环；结果取走即清（一拍最多应用一次）。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: a.take() is not None), "worker 未在 2s 内发布结果"
        assert a.take() is None, "结果槽未被取走即清（同帧二次应用）"
        assert a.health()["applied"] == 1
    finally:
        assert a.stop() is True


def test_async_stale_gate_drops():
    """age 闸：max_age_ms≈0 时已发布结果按 stale 丢弃（take 返回 None）。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub, max_age_ms=0.001)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        # age 闸在消费侧（发布不判龄，take 时才判——treasure 同构）：轮询 take()
        # 直到某次消费把已发布的结果判成 stale。
        assert _wait_until(lambda: a.take() is None
                           and (h := a.health())["applied"] + h["stale_drops"] >= 1), \
            "消费侧未把超龄结果判为 stale"
        assert a.health()["stale_drops"] == 1
    finally:
        a.stop()


def test_async_worker_exception_survives():
    """单帧异常计 failures 不杀 daemon：下一帧照常出结果。"""
    stub = _StubObserver([RuntimeError("boom"), _mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: a.health()["failures"] >= 1)
        a.push(_frame())
        assert _wait_until(lambda: a.take() is not None), "异常后 worker 未恢复"
        assert a.health()["failures"] == 1
    finally:
        a.stop()


def test_async_no_session_no_thread():
    """session 不可用：start 不起线程，push/take 空转（行为=session 缺失降级）。"""
    a = AsyncDepthRoadObserver(_StubObserver([], session_ready=False))  # type: ignore[arg-type]
    a.start()
    assert a._thread is None
    a.push(_frame())
    assert a.take() is None
    assert a.health()["pushed"] == 0, "无 worker 时 push 不得计数（零结果报警的前提）"
    assert a.stop() is True


def test_async_none_reading_not_published():
    """observe 返回 None（推理失败）不发布结果、不污染计数。"""
    stub = _StubObserver([None])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: stub.calls >= 1)
        _time.sleep(0.05)  # 给 worker 足够时间把（错误地）发布暴露出来
        assert a.take() is None
        h = a.health()
        assert h["applied"] == 0 and h["stale_drops"] == 0 and h["failures"] == 0
    finally:
        a.stop()
