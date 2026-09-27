# -*- coding: utf-8 -*-
"""深度几何观测层（depth_geo）的回归锁。

两层用例：
- **合成**（零数据依赖）：按度量几何构造视差图（平面相机模型 + 已知路宽/墙体/，
  锁新读法机制——逐帧尺度自标定对输出不变、直路双缘读数与侧别、车框遮挡
  逐行弃权、同行路障内沿夹紧、平面拟合失败的诚实弃权。
- **数据锚点**（skipif：无离线缓存则跳）：金标 DA-V2s @336 视差场上的行为锁
  （双侧在场帧 / 单侧帧 / 骑路缘帧），钉读数与金标结构的相对关系不许漂。

合成场景构造口径：相机内参用产码钉定常量（FY/FX_FY/CX/CY），地面平面
Y = b·Z + c（b 取实测典型 pitch 项，c=相机离地高），墙体为 X=±半宽的竖直面，
视差 d = s_true/Z。"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush import depth_geo as dg
from maaracing_master.plugins.speedrush.depth_geo import (
    AsyncDepthRoadObserver, CX, CY, DepthRoadObserver, DepthRoadReading,
    FX_FY, FY, S0, W_REF, Y0, DIAG_Y1, infer_map, reading_from_map)
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of

CAL = load_calib()
FX = FY * FX_FY
H_CAM = 1.6          # 合成场景相机离地高（实测典型 1.61）
PITCH_B = -0.06      # 合成场景平面 pitch 项（实测典型量级）
S_TRUE = 24.0        # 合成视差的真尺度（≠ 种子 S0，锁自标定）

NPY = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
       / "speedrush" / "depth_review" / "npy")


# ── 合成度量场景 ────────────────────────────────────────────────────────

def _ground_y(z):
    """地面在深度 z 处的相机系高度（Y 向下为正）。"""
    return H_CAM + PITCH_B * z


def _ground_z(v):
    """行 v 的地面深度（X=0 列，倾角项并入分母，与 gold_line 同式）。"""
    return H_CAM / ((v - CY) / FY - PITCH_B)


def _scene_map(x_left=W_REF, x_right=W_REF, cars=(), obstacles=(), s=S_TRUE):
    """度量场景 → (全帧视差图, 车盒掩码)。逐像素光线取最近命中，d = s/Z；
    无命中（天空）d=0。cars/obstacles: (x1, x2, z1, z2, top) 轴对齐盒，top=顶面
    离地高；车盒是 YOLO 可见物（返回掩码模拟生产 object_mask），路障不是
    （留在视差里走平面"上凸体"路径）。"""
    m = np.zeros((720, 1280), np.float32)
    cm = np.zeros((720, 1280), bool)
    for v in range(Y0, DIAG_Y1):
        for u in range(1280):
            best = np.inf
            kind = None
            zg = _ground_z(v)
            if zg > 0:
                best, kind = zg, "ground"     # 地面（无限平面，恒有命中）
            for x_wall, sgn in ((x_left, -1.0), (x_right, 1.0)):
                if sgn * (u - CX) <= 0:
                    continue                   # 墙在本列的远侧，光线到不了
                zw = sgn * x_wall * FX / (u - CX)
                y_ray = (v - CY) * zw / FY
                if _ground_y(zw) - 60.0 <= y_ray <= _ground_y(zw) and zw < best:
                    best, kind = zw, "wall"    # 竖直墙面（墙高 60m 内）
            for (x1, x2, z1, z2, top) in (*cars, *obstacles):
                den = u - CX
                ta, tb = sorted((x1 * FX / den, x2 * FX / den))
                if tb <= 0 or ta > z2 or tb < z1:
                    continue
                zc = max(ta, z1)               # 近侧面（或盒近缘）
                if zc > z2:
                    continue
                y_ray = (v - CY) * zc / FY
                if _ground_y(zc) - top <= y_ray <= _ground_y(zc) and zc < best:
                    best, kind = zc, "box"
            if np.isfinite(best) and best > 0:
                m[v, u] = s / best
                if kind == "box":
                    cm[v, u] = True
    return m, cm


def _lane_of_x(x_m, v):
    """真值换算：地面上的横向米位置 x_m 在行 v 的像素 → 旧车道单位。"""
    u = x_m * FX / _ground_z(v) + CX
    return x_lane_of(int(round(u)), v, CAL), u


# ── 读法机制 ────────────────────────────────────────────────────────────

WALL = 2.5   # 合成路半宽（米）：近带 Z<8 内整段缘须在画框内（宽路近行出画是
             # 出画判据的诚实弃权，不是读法失效）

def test_straight_road_both_edges():
    """直路双墙：双侧在场、读数车道量落在真值上、px 诊断与参考行真缘一致。"""
    m, _ = _scene_map(x_left=WALL, x_right=WALL)
    rd = reading_from_map(m, CAL, None)
    assert rd.sides == 2, f"rejects={rd.rejects}"
    lane_l, u_l = _lane_of_x(-WALL, 560)
    lane_r, u_r = _lane_of_x(WALL, 560)
    assert abs(rd.left_edge_lane - lane_l) < 0.15, \
        f"{rd.left_edge_lane} vs {lane_l}"
    assert abs(rd.right_edge_lane - lane_r) < 0.15
    # 中位行像素落在缘线的行范围内（近行~远行之间，中位行在带中部）
    assert 15.0 < rd.left_x < u_l + 400
    assert 800.0 < rd.right_x < 1277.0


def test_scale_drift_invariant_output():
    """屏幕空间不变性（已实证推论成回归锁）：真尺度漂 ±25% 读数不变。

    度量场每帧尺度漂移（DA 归一化锚，CV≈14%）不得进入车道读数——
    车道量对均匀缩放不变，逐帧 W_REF 标定把度量门限拉回自洽。"""
    m1, _ = _scene_map(x_left=WALL, x_right=WALL, s=24.0)
    m2, _ = _scene_map(x_left=WALL, x_right=WALL, s=30.0)
    r1 = reading_from_map(m1, CAL, None)
    r2 = reading_from_map(m2, CAL, None)
    assert r1.sides == 2 and r2.sides == 2, f"rejects={r1.rejects}/{r2.rejects}"
    assert abs(r1.left_edge_lane - r2.left_edge_lane) < 0.05
    assert abs(r1.right_edge_lane - r2.right_edge_lane) < 0.05


def test_car_box_left_occludes_left_rows_only():
    """左侧低障（0.5m，|X|>1.5，骑在左缘上）遮挡左缘近行：左缘从未遮远行投影
    读出（逐行弃权，非整侧弃权）。（1.3m 高盒的角跨度覆盖缘线全程，几何上
    必然整帧遮挡——部分遮挡场景须低障构造。）"""
    m, cm = _scene_map(x_left=2.0, x_right=2.0, cars=((-2.5, -1.5, 4.5, 5.5, 0.5),))
    rd = reading_from_map(m, CAL, object_mask=cm)
    assert rd.sides == 2, f"rejects={rd.rejects}"
    lane_r, _ = _lane_of_x(2.0, 560)
    assert abs(rd.right_edge_lane - lane_r) < 0.35   # 近带行斜率混合的行漂余量
    lane_l, _ = _lane_of_x(-2.0, 560)
    assert rd.left_edge_lane is not None
    assert abs(rd.left_edge_lane - lane_l) < 0.25


def test_object_mask_occlusion_abstains_side():
    """object_mask（生产 YOLO 掩码）盖住右半路近带全部行 → R 诚实弃权。"""
    m, _ = _scene_map()
    mask = np.zeros((720, 1280), bool)
    mask[400:700, 780:1280] = True      # 右半路全部读数行（含右缘）
    rd = reading_from_map(m, CAL, object_mask=mask)
    assert rd.right_edge_lane is None
    assert any("R" in r for r in rd.rejects)
    assert rd.left_edge_lane is not None


def test_obstacle_clamps_inner_edge():
    """同行路障（整段在路中心 GAP 外）夹紧内沿：右缘读路障近侧，不是穿车读墙。"""
    # 贯全带的竖薄墙（Z 4~60、全高）：每一行都夹到其近侧，聚合中位=夹紧值
    m, _ = _scene_map(x_left=WALL, x_right=WALL, obstacles=((1.5, 2.2, 4.0, 60.0, 60.0),))
    rd = reading_from_map(m, CAL, None)
    assert rd.sides == 2, f"rejects={rd.rejects}"
    lane_clamp, _ = _lane_of_x(1.5, 560)
    assert abs(rd.right_edge_lane - lane_clamp) < 0.2


def test_no_ground_abstains_honestly():
    """近带无地面（全高墙场景）→ 平面拟合失败，双侧弃权并留原因。"""
    m = np.zeros((720, 1280), np.float32)
    m[Y0:DIAG_Y1, :] = 60.0             # 视差处处≈0（Z→∞，无地面种子）
    rd = reading_from_map(m, CAL, None)
    assert rd.sides == 0
    assert rd.left_edge_lane is None and rd.right_edge_lane is None
    assert rd.rejects


def test_side_gate_rejects_crossing_edge():
    """内沿落在画面中央（车道量 |·|<0.15）→ 侧别门拒，不成读数。"""
    # 左墙推到 X=-0.2（几乎正中）：左缘 x_lane≈-0.07 被侧别门拒；右缘照常在场
    m, _ = _scene_map(x_left=0.2, x_right=W_REF)
    rd = reading_from_map(m, CAL, None)
    assert rd.left_edge_lane is None
    assert any("L" in r for r in rd.rejects)
    assert rd.right_edge_lane is not None


# ── 资产与会话降级 ──────────────────────────────────────────────────────

def test_ego_mask_asset_loads():
    """挖除区 = 上半身矩形（y351~532）∪ 车带下延（同列带到底，皮肤无关段）。"""
    mask = DepthRoadObserver._load_ego_mask()
    assert mask is not None and mask.shape == (720, 1280)
    assert mask[351:532, 512:765].all()          # 上半身高区
    assert mask[532:715, 512:765].all()          # 车带下延（粘连带整体排除）
    assert not mask[:351, 512:766].any()         # 上缘之上不挖
    assert not mask[:, :512].any() and not mask[:, 766:].any()


def test_observer_without_session_returns_none():
    """权重缺失（session=None）时 observe 恒 None——road_offset 退纯模型积分。"""
    obs = DepthRoadObserver(None, CAL)
    assert obs.observe(np.zeros((720, 1280, 3), np.uint8)) is None


# ── 数据锚点（金标 DA-V2s @336 视差场；无离线缓存则跳）──────────────────

def _cached_map(key: str, tag: str = "d336q4f16") -> np.ndarray | None:
    p = NPY / f"frames__{key}__{tag}.npy"
    if not p.exists():
        p = NPY / f"frames__{key}__da2s.npy"
        if not p.exists():
            return None
    return np.load(p).astype(np.float32)


@pytest.mark.skipif(_cached_map("000100") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_000100_wall_both_sides():
    """金标 wall 帧（000100）：双侧墙基在场，读数与金标结构同侧同量级。

    锚点值=2026-09-27 新读法落产时的实测（±0.2 锁回归不锁真值；绝对量受
    DA@336 雾带/墙基模糊限制，验收口径见模块头注三戒）。"""
    m = _cached_map("000100")
    rd = reading_from_map(m, CAL, ego_mask=DepthRoadObserver._load_ego_mask())
    assert rd.sides == 2, f"rejects={rd.rejects}"
    assert abs(rd.left_edge_lane - (-3.29)) < 0.2
    assert abs(rd.right_edge_lane - 3.45) < 0.2


@pytest.mark.skipif(_cached_map("000714") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_000714_curve():
    """金标弯道帧（000714，curve_cont2）：双侧在场（弯道不把读数打飞）。"""
    m = _cached_map("000714")
    rd = reading_from_map(m, CAL, ego_mask=DepthRoadObserver._load_ego_mask())
    assert rd.sides == 2, f"rejects={rd.rejects}"
    assert abs(rd.left_edge_lane - (-1.34)) < 0.2
    assert abs(rd.right_edge_lane - 1.38) < 0.2


@pytest.mark.skipif(_cached_map("000518", "da2s") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_000518_straddle():
    """骑路缘 518（@518 场）：双侧在场，量级与旧 q20 锚点同域（L 人行道缘、
    R 真右缘；新读法 L 读到人行道外缘——矮路缘石在 DA@336 场低于 15cm
    上凸体门不可分辨，语义差异记录于此）。"""
    m = _cached_map("000518", "da2s")
    rd = reading_from_map(m, CAL, ego_mask=DepthRoadObserver._load_ego_mask())
    assert rd.sides == 2, f"rejects={rd.rejects}"
    assert -1.4 < rd.left_edge_lane < -0.9
    assert 0.8 < rd.right_edge_lane < 1.2


@pytest.mark.skipif(_cached_map("000437", "da2s") is None,
                    reason="需离线深度缓存（APPDATA depth_review/npy）")
def test_anchor_000437_wallhug():
    """贴护栏 437（@518 场）：双侧在场（新读法比旧 q20 多读出左墙基——
    黄线出画不等于深度出画），量级锚定不许漂。"""
    m = _cached_map("000437", "da2s")
    rd = reading_from_map(m, CAL, ego_mask=DepthRoadObserver._load_ego_mask())
    assert rd.sides == 2, f"rejects={rd.rejects}"
    assert abs(rd.left_edge_lane - (-1.18)) < 0.2
    assert abs(rd.right_edge_lane - 0.92) < 0.2


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
                            latency_ms=1.0, rejects=("R:遮挡",))


class _StubObserver:
    """DepthRoadObserver 替身：按脚本回放 读数/None/异常，记录调用。"""

    def __init__(self, script, session_ready: bool = True) -> None:
        self._script = list(script)
        self.session_ready = session_ready
        self.calls = 0

    def observe(self, frame, object_mask=None):
        reading, _ = self.observe_debug(frame, object_mask)
        return reading

    def observe_debug(self, frame, object_mask=None):
        self.calls += 1
        item = self._script.pop(0) if self._script else None
        if isinstance(item, Exception):
            raise item
        return item, None


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


# ── DML 互斥（core.dml_lock）：两会话并发 run 会段错误杀进程，2026-09-25 实证 ──

class _FakeSess:
    """sess.run 替身：断言调用期间持有 DML 锁，返回定形视差图。"""

    def run(self, _names, _feeds):
        assert dml_lock.LOCK.locked(), "session.run 期间必须持有 DML 锁"
        return [np.zeros((1, 90, 160), np.float32)]


def _frame_np() -> "np.ndarray":
    return np.zeros((720, 1280, 3), np.uint8)


def test_infer_map_holds_dml_lock_during_run():
    m = infer_map(_FakeSess(), _frame_np(), 336)
    assert m.shape == (720, 1280)


def test_infer_map_busy_raises_not_blocks():
    """锁被占（控制拍感知在飞）：立即抛 Busy 让 worker 跳帧，不得阻塞等待。"""
    with dml_lock.LOCK:
        with pytest.raises(dml_lock.Busy):
            infer_map(_FakeSess(), _frame_np(), 336)


def test_async_dml_busy_skip_not_failure():
    """worker 让锁跳帧：计 busy_skips、不计 failures，下一帧照常出结果。"""
    stub = _StubObserver([dml_lock.Busy("busy"), _mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: a.health()["busy_skips"] >= 1)
        a.push(_frame())
        assert _wait_until(lambda: a.take() is not None), "让锁跳帧后 worker 未恢复"
        assert a.health()["failures"] == 0
    finally:
        a.stop()


def test_async_throttle_bounds_rate():
    """节流窗：窗内不出工（第二帧压着不跑），窗过即恢复出工。"""
    stub = _StubObserver([_mk_reading(), _mk_reading(), _mk_reading()])
    a = AsyncDepthRoadObserver(stub, infer_min_interval_s=0.5)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: stub.calls >= 1)   # 首帧立即出工（_last_infer 初值 0）
        a.push(_frame())
        _time.sleep(0.15)                       # 窗内（0.5s）
        assert stub.calls == 1, "节流窗内 worker 不得二次出工"
        assert _wait_until(lambda: stub.calls >= 2, timeout=1.5), "窗过后未恢复出工"
    finally:
        a.stop()


def test_async_debug_writes_replayable_evidence(tmp_path):
    """调试图同拍落可复现证据包：读数带视差（fp16）+ 生效掩码——
    只有有损渲染时，实机读数故障无法离线重放（2026-09-25 21:41 局的教训）。"""
    stub = _StubObserver([_mk_reading()])
    stub._ego_mask = np.zeros((720, 1280), bool)
    stub._ego_mask[:10, :10] = True
    def _map_observe(frame, object_mask=None):
        stub.calls += 1
        return _mk_reading(), np.full((720, 1280), 4.0, np.float32)
    stub.observe_debug = _map_observe
    a = AsyncDepthRoadObserver(stub, debug_dir=tmp_path)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: stub.calls >= 1)
        # 渲染+JPEG 编码在 worker 线程要几百 ms：轮询等落盘，别按调用数硬等
        band_p = next(iter(tmp_path.glob("d*_band.npy")), None) if _wait_until(
            lambda: any(tmp_path.glob("d*_band.npy"))) else None
        assert band_p is not None, "视差带未落盘"
        band = np.load(band_p)
        assert band.shape == (dg.DIAG_Y1 - dg.Y0, 1280)
        assert band.dtype == np.float16
        assert list(tmp_path.glob("d*.jpg")), "渲染图未落盘"
        mask = np.unpackbits(np.load(next(iter(tmp_path.glob("d*_mask.npy")))))
        mask = mask[: 720 * 1280].reshape(720, 1280).astype(bool)
        assert mask[:10, :10].all() and not mask[100:, 100:].any()
    finally:
        a.stop()
