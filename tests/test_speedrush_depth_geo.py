# -*- coding: utf-8 -*-
"""深度几何观测层 v4（MoGe 点云 3D 找边）的回归锁。

三层用例：
- **合成**（零数据依赖）：按度量几何直接构造点云（相机针孔模型 + 已知
  路宽/墙体/车盒，fx/fy 任意一致值），锁找边机制——直路双缘读数与车道
  换算、单侧诚实弃权、物体掩码挖除、平面拟合失败弃权、走廊域拟合的
  抗墙歪斜。
- **后处理单元**（moge_post）：shift 求解器在已知焦距合成场景上收敛到真值
  （官方 scipy 版的产线替代，等价性由金标实跑验证、本锁防退化）。
- **数据锚点**（skipif：无离线缓存则跳）：金标帧 MoGe 点云缓存上的读数锁
  （干净直路帧 ±0.5m 口径），钉读数不许漂。缓存由金标回归探针同款推理生成
  （见深度几何 v4 换装提交）。

合成场景构造口径：Y 向下为正，地面 Y = H_CAM + PITCH_B·Z（c≈相机离地高），
墙体为 X=±半宽的竖直面，全帧逐像素取最近命中。"""
from __future__ import annotations

import json
import ctypes
import os
import time as _time
from pathlib import Path

import numpy as np
import pytest

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush import depth_geo as dg
from maaracing_master.plugins.speedrush.depth_geo import (
    AsyncDepthRoadObserver, DepthRoadObserver, DepthRoadReading,
    infer_points, load_session, reading_from_points)
from maaracing_master.plugins.speedrush import moge_post
from maaracing_master.plugins.speedrush.world_model import load_calib

CAL = load_calib()
FX, FY = 857.0, 851.4        # 合成相机焦距（任意一致值，引擎按入参用）
CX, CY = 640.0, 360.0
H_CAM = 2.0                  # 合成相机离地高（实测典型 ~2.0）
PITCH_B = -0.03              # 合成平面 pitch 项（Y = H_CAM + B·Z）

NPY = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
       / "speedrush" / "depth_review" / "npy_moge")


# ── 合成度量点云 ────────────────────────────────────────────────────────

def _scene_points(x_left=None, x_right=None, boxes=(), wall_h=30.0,
                  shape=(720, 1280), noise=0.05, seed=7) -> np.ndarray:
    """度量场景 → 全幅点云 (H,W,3)，无效（天空）nan。boxes: (x1,x2,z1,z2,top)。

    noise：各轴独立高斯噪声 σ（米，模拟 MoGe 残差实测 3~9cm 量级）——零噪声
    下竖直面全部点落在同一横格，形成不了真实数据里噪声展宽出的「两格持续」
    穿越剖面。"""
    rng = np.random.default_rng(seed)
    pts = np.full(shape + (3,), np.nan, np.float32)
    vs, us = np.mgrid[0:shape[0], 0:shape[1]]
    dx = (us - CX) / FX
    dy = (vs - CY) / FY
    z_g = H_CAM / (dy - PITCH_B)                    # 地面命中深
    best = np.where(np.isfinite(z_g) & (z_g > 0), z_g, np.inf)
    for x_wall, sgn in ((x_left, -1.0), (x_right, 1.0)):
        if x_wall is None:
            continue
        zw = sgn * x_wall * FX / np.where(us != CX, us - CX, 1e-9)
        y_ray = dy * zw
        on_wall = (sgn * (us - CX) > 0) & np.isfinite(zw) & (zw > 0) \
            & (y_ray >= H_CAM + PITCH_B * zw - wall_h) & (zw < best)
        best = np.where(on_wall, zw, best)
    for (x1, x2, z1, z2, top) in boxes:
        with np.errstate(divide="ignore", invalid="ignore"):
            ta = np.minimum(x1 * FX / (us - CX), x2 * FX / (us - CX))
        zc = np.maximum(ta, z1)
        hit = (np.isfinite(zc) & (zc <= z2) & (zc >= z1) & (zc < best)
               & (dy * zc >= H_CAM + PITCH_B * zc - top))
        best = np.where(hit, zc, best)
    ok = np.isfinite(best) & (best < np.inf)
    safe = np.where(ok, best, 1.0)
    pts[..., 0] = np.where(ok, dx * safe + rng.normal(0, noise, shape), np.nan)
    pts[..., 1] = np.where(ok, dy * safe + rng.normal(0, noise, shape), np.nan)
    pts[..., 2] = np.where(ok, safe + rng.normal(0, noise, shape), np.nan)
    return pts


WALL = 5.5   # 合成路半宽（米，真实路宽尺度）：须在拟合走廊域（|X|<4）之外——
             # 墙进拟合域会拽歪平面（正是 2026-09-30 定案淘汰的病理），合成场景
             # 不得复刻该违规形态
LANE = WALL / CAL.lane_w_m


def test_straight_road_both_edges():
    """直路双墙：双侧在场、车道量落在 x_m/lane_w 真值上。"""
    pts = _scene_points(x_left=WALL, x_right=WALL)
    rd = reading_from_points(pts, FX, CAL, fy=FY)
    assert rd.sides == 2, f"rejects={rd.rejects}"
    assert abs(rd.left_edge_lane + LANE) < 0.15, f"{rd.left_edge_lane} vs {-LANE:.2f}"
    assert abs(rd.right_edge_lane - LANE) < 0.15
    # 像素诊断锚：最近箱检出缘回投的 u 落在墙的画内段
    assert 0 < rd.left_x < CX and CX < rd.right_x < 1280


def test_single_side_abstains_honestly():
    """单侧墙：该侧读数、对侧 None + rejects 留因（宁缺毋假）。"""
    pts = _scene_points(x_right=WALL)
    rd = reading_from_points(pts, FX, CAL, fy=FY)
    assert rd.sides == 1, f"rejects={rd.rejects}"
    assert abs(rd.right_edge_lane - LANE) < 0.15
    assert rd.left_edge_lane is None
    assert any("L:无穿越" in r for r in rd.rejects)


NEAR_BARRIER_X = 4.0    # 近场护栏内脸（米）：比远墙更近的绑定约束
FAR_ONLY_LANES = WALL / CAL.lane_w_m


def test_edge_nearest_consistent_prefers_near_boundary():
    """最近一致边界：双箱一致的近场护栏压住远墙（读数≠跨箱中位）。

    2026-10-02 换装锁：跨箱中位数在这里落在远墙（近远箱数打平后中位落远），
    读数参照系随箱可用性翻转——最近的双箱一致边界才是驾驶要守的最近可信
    约束。近场护栏只跨两箱（z5-9）是有意设计：测试相机 FOV 窄，更近的结构
    出不了两箱（画界守卫裁掉），那正是「单箱孤证」要拒的形态。"""
    pts = _scene_points(
        x_left=WALL, x_right=WALL,
        boxes=((-4.5, -NEAR_BARRIER_X, 5.0, 9.0, 0.8),))
    rd = reading_from_points(pts, FX, CAL, fy=FY)
    assert rd.sides == 2, f"rejects={rd.rejects}"
    want = -3.67 / CAL.lane_w_m                 # 近场护栏双箱一致穿越（实测）
    assert abs(rd.left_edge_lane - want) < 0.15, \
        f"L={rd.left_edge_lane} 应取近场护栏 {want:.2f}（而非远墙 {-FAR_ONLY_LANES:.2f}）"
    assert abs(rd.right_edge_lane - LANE) < 0.15


def test_edge_isolated_nearest_bin_rejected():
    """最近箱孤证不盲取：仅一箱出穿越的近场假缘被邻箱一致性跳过。

    d00010 真机实证（z6-8 箱 −1.26m 阴影假穿越）：单箱孤证不可信——「最近」
    须带「一致」才算边界。锁钉读数落在双箱一致的真护栏，而非最近孤箱。"""
    pts = _scene_points(
        x_left=WALL, x_right=WALL,
        boxes=((-2.4, -2.2, 3.0, 5.0, 0.6),      # 最近箱孤证：仅 z3-5 一箱
               (-4.5, -4.0, 5.0, 9.0, 0.8)))     # 真护栏：z5-9 双箱一致
    rd = reading_from_points(pts, FX, CAL, fy=FY)
    assert rd.sides == 2, f"rejects={rd.rejects}"
    want = -3.67 / CAL.lane_w_m                  # 真护栏双箱一致穿越（实测）
    blind_nearest = -1.91 / CAL.lane_w_m         # 最近孤箱（若被盲取即中招）
    assert abs(rd.left_edge_lane - want) < 0.15, f"L={rd.left_edge_lane} vs {want:.2f}"
    assert abs(rd.left_edge_lane - blind_nearest) > 0.3, "最近孤证不应被盲取"


def test_edge_all_bins_disagree_falls_back_to_median():
    """弯道形态（逐箱 x 系统漂移、无一致对）：退回中位数，保持旧行为。"""
    pts = _scene_points(
        x_left=None, x_right=WALL,
        boxes=((-2.75, -2.5, 3.0, 5.0, 1.0),
               (-3.75, -3.5, 5.0, 7.0, 1.0),
               (-4.75, -4.5, 7.0, 9.0, 1.0)))
    rd = reading_from_points(pts, FX, CAL, fy=FY)
    assert rd.left_edge_lane is not None, f"rejects={rd.rejects}"
    want = -3.14 / CAL.lane_w_m                  # 三箱穿越（实测）的中位
    assert abs(rd.left_edge_lane - want) < 0.2, f"L={rd.left_edge_lane} vs {want:.2f}"


def test_object_mask_digs_objects_from_cloud():
    """YOLO 物体掩码从点云源头挖除：掩码盖掉右墙可见段 → 右侧诚实弃权，
    左侧照常读数（物体从源头消失，穿车读墙假缘不再产生）。"""
    pts = _scene_points(x_left=WALL, x_right=WALL)
    obj = np.zeros((720, 1280), bool)
    obj[:, 900:] = True                    # 右墙可见段（z≳7.4 时 u≥934）
    rd = reading_from_points(pts, FX, CAL, object_mask=obj, fy=FY)
    assert rd.right_edge_lane is None, f"rejects={rd.rejects}"
    assert any("R:无穿越" in r for r in rd.rejects)
    assert rd.left_edge_lane is not None


def test_plane_fit_fail_abstains():
    """近带无路面（走廊域点不足）：诚实弃权不硬读。"""
    pts = np.full((720, 1280, 3), np.nan, np.float32)
    rd = reading_from_points(pts, FX, CAL)
    assert rd.sides == 0 and rd.left_edge_lane is None
    assert any("平面拟合失败" in r for r in rd.rejects)


def test_fit_road_plane_feature_seed_resists_wall_tilt():
    """特征种子拟合抗墙歪斜：墙在走廊域外/内都不改变路面平面钉结果。

    历史定案：拟合起步种子被墙劫持会把平面拽歪（000660 实测被拽 5.25°，
    路自身平面 -2.06°/99% 内点；2026-09-30 指正、2026-10-01 双平面回归、
    2026-10-02 列剖面特征换装）。墙基与路面相连、迭代内点沿墙爬升的病理
    由特征种子切断——走廊内墙（000660 形态，墙基在 |X|<4 内）同样不许拽歪。
    fy=None 的旧位置圈地路径是回退开关，其抗墙性由走廊外墙用例一并锁定。"""
    dig = np.zeros((dg.DIAG_Y1 - dg.Y0, 1280), bool)

    def _band_coefs(pts, fy):
        X, Y, Z = (pts[dg.Y0:dg.DIAG_Y1, :, k] for k in (0, 1, 2))
        return dg._fit_road_plane(X, Y, Z, dig, fy=fy)

    pts_open = _scene_points(x_left=None, x_right=None)
    X2, Y2, Z2 = (pts_open[dg.Y0:dg.DIAG_Y1, :, k] for k in (0, 1, 2))
    coef_open = dg._fit_road_plane(X2, Y2, Z2, dig, fy=FY)
    assert coef_open is not None

    # 特征种子路径（产线）：走廊外墙与走廊内墙（000660 形态）都不拽歪
    coef = _band_coefs(_scene_points(x_left=WALL, x_right=WALL), FY)
    assert coef is not None
    np.testing.assert_allclose(coef, coef_open, atol=1e-3)
    coef_in = _band_coefs(_scene_points(x_left=3.0, x_right=3.0), FY)
    assert coef_in is not None
    # 走廊内墙基的 15cm 内点带会把截距拖 ~1.3cm（远小于找边阈值量级），
    # 角度项钉死、截距放宽到 2cm
    assert abs(coef_in[0] - coef_open[0]) < 2e-4
    assert abs(coef_in[1] - coef_open[1]) < 1e-3
    assert abs(coef_in[2] - coef_open[2]) < 0.02

    # 回退路径（fy=None）：走廊外墙不进旧拟合域，平面钉结果同样不变
    coef_leg = _band_coefs(_scene_points(x_left=WALL, x_right=WALL), None)
    coef_leg_open = dg._fit_road_plane(X2, Y2, Z2, dig, fy=None)
    assert coef_leg is not None and coef_leg_open is not None
    np.testing.assert_allclose(coef_leg, coef_leg_open, atol=1e-3)


def test_ego_mask_asset_loads_tight_rect():
    """ego 掩码资产在场且=紧矩形（3D 语义；列带下延是已退役的 2D 构造）。"""
    m = DepthRoadObserver._load_ego_mask()
    assert m is not None and m.shape == (720, 1280)
    d = json.loads((Path(dg.__file__).parent / "resources" / "calibration"
                    / "ego_mask.json").read_text(encoding="utf-8"))
    expect = np.zeros((720, 1280), bool)
    expect[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
    np.testing.assert_array_equal(m, expect)


# ── moge_post 后处理单元 ────────────────────────────────────────────────

def _synthetic_affine_points(focal_true: float, shift_true: float = 0.0,
                             shape=(336, 598)) -> tuple[np.ndarray, np.ndarray]:
    """官方链自洽的仿射点图：xy = uv·(z−shift)/focal（uv=官方 span 归一网格）。

    官方 recover_focal_shift 的单焦距模型只在「点由同一 focal 投影」时精确可解
    （598×336 下 span_x≠span_y，任意针孔场景必有系统残差被 shift 吸收）——
    所以合成点必须从链自身生成，求解器才有 shift≈0 / focal=focal_true 的真值。"""
    H, W = shape
    uv = moge_post.normalized_view_plane_uv(W, H).astype(np.float64)
    rng_z = 2.0 + 6.0 * ((np.arange(H) + 0.5) / H)[:, None]
    zz = np.broadcast_to(rng_z, (H, W))
    xy = uv * (zz - shift_true)[..., None] / focal_true
    pts = np.concatenate([xy, zz[..., None]], -1).astype(np.float32)
    return pts, np.ones(shape, bool)


def test_moge_shift_solver_recovers_known_focal():
    """shift 求解器：已知焦距场景上收敛到真值（shift≈0 / focal=focal_true）。

    这是官方 scipy LM 的产线替代（模式搜索，同走「离 0 最近局部极小」盆地）：
    等价性由金标帧实跑验证（shift/fx 相对差 <1e-5），本锁防求解器退化。"""
    for focal_true in (1.0, 1.5, 2.2):
        pts, mask = _synthetic_affine_points(focal_true)
        focal, shift = moge_post.recover_focal_shift(pts, mask)
        assert abs(shift) < 1e-3, f"f={focal_true}: shift={shift}"
        assert abs(focal - focal_true) < 1e-3, f"f={focal_true}: got {focal}"


def test_moge_shift_solver_beats_shallow_valley_trap():
    """浅谷场景（step 迈得过真谷的形态）：模式搜索不得停在 x0=0 的假极小。"""
    # R(s) = (s + 0.05)² + 0.1·s² 的单谷函数，谷在 s = -1/22 ≈ -0.04545：
    # 大步长两侧试探都回升的形态（历史 frame 0 实证）——求解器必须走到真谷。
    r = lambda s: (s + 0.05) ** 2 + 0.1 * s * s
    x = moge_post._minimize_local(r, step=0.5)
    assert abs(x + 0.05 / 1.1) < 1e-4, f"{x}"


# ── 会话与推理封装 ──────────────────────────────────────────────────────

def _load_session_or_skip():
    w = Path(__import__("maaracing_master.plugins.speedrush", fromlist=["DEPTH_MODEL_FILE"])
             .DEPTH_MODEL_FILE)
    if not w.exists():
        pytest.skip(f"权重缺失：{w}")
    return load_session(w)


def test_load_session_is_folded_static_shape():
    """load_session 的输入维必须是编译期常量（batch/height/width 三 free dim 全固定）。

    漏折叠的形态是"能跑但慢数倍"（未融合图落 CPU）——性能回退不会红任何功能
    测试，只能由这条形状锁拦。"""
    dims = _load_session_or_skip().get_inputs()[0].shape
    assert all(isinstance(d, int) for d in dims), f"输入维未静态化（折叠未生效）: {dims}"
    assert tuple(dims) == (1, 3, dg.MOGE_IN_H, dg.MOGE_IN_W)


class _FakeMogeSess:
    """sess.run 替身：断言调用期间持有 DML 锁，返回定形 MoGe 四元输出。"""

    def get_inputs(self):
        class _In:
            name = "image"
        return [_In()]

    def run(self, _names, _feeds):
        assert dml_lock.LOCK.locked(), "session.run 期间必须持有 DML 锁"
        pts, mask = _synthetic_affine_points(1.5)
        return [pts[None], np.zeros((1, 8, 598, 3), np.float32),
                mask[None].astype(np.float32),
                np.array([1.0], np.float32)]


def _frame_np() -> np.ndarray:
    return np.zeros((720, 1280, 3), np.uint8)


def test_infer_points_holds_dml_lock_during_run():
    pts, fx, fy = infer_points(_FakeMogeSess(), _frame_np())
    assert pts.shape == (720, 1280, 3) and np.isfinite(pts).all()


def test_infer_points_busy_raises_not_blocks():
    """锁被占（控制拍感知在飞）：立即抛 Busy 让 worker 跳帧，不得阻塞等待。"""
    with dml_lock.LOCK:
        with pytest.raises(dml_lock.Busy):
            infer_points(_FakeMogeSess(), _frame_np())


# ── forward 的 IO binding（固定输入/输出缓冲，抗显存分页）──────────────────

class _BindableSess:
    """io_binding 接口替身：记录绑定次数与 run 时缓冲内容，回放定形输出。"""

    def __init__(self):
        self.io_binding_calls = 0
        self.run_calls = 0
        self.run_seen_input_head = None
        self._pts, self._mask = _synthetic_affine_points(1.5)

    def get_inputs(self):
        class _In:
            name = "image"
        return [_In()]

    def get_outputs(self):
        class _Out:
            def __init__(self, name):
                self.name = name
        return [_Out("points"), _Out("normal"), _Out("mask"), _Out("scale")]

    def io_binding(self):
        self.io_binding_calls += 1
        return _FakeBinding()

    def run_with_iobinding(self, bind, _opts):
        self.run_calls += 1
        self.run_seen_input_head = float(bind.xbuf_view[0, 0, 0, 0])
        bind.outputs = [self._pts[None], np.zeros((1, 8, 598, 3), np.float32),
                        self._mask[None].astype(np.float32),
                        np.array([1.0], np.float32)]

    def run(self, _names, _feeds):
        self.run_calls += 1
        return [self._pts[None], np.zeros((1, 8, 598, 3), np.float32),
                self._mask[None].astype(np.float32),
                np.array([1.0], np.float32)]


class _FakeBinding:
    """SessionIOBinding 替身：bind_input 记录指针，按地址重建缓冲视图。"""

    def __init__(self):
        self.xbuf_view = None

    def bind_input(self, _name, _dev, _did, dtype, shape, ptr):
        dt = np.dtype(dtype)
        n = int(np.prod(shape))
        buf = (ctypes.c_ubyte * (dt.itemsize * n)).from_address(ptr)
        self.xbuf_view = np.frombuffer(buf, dtype=dt).reshape(shape)

    def bind_output(self, *_a, **_k):
        pass

    def copy_outputs_to_cpu(self):
        return self.outputs


def test_forward_binding_caches_and_overwrites_input():
    """binding 只建一次（会话级缓存），新帧内容覆写进同一固定缓冲。"""
    import ctypes
    moge_post._BINDINGS.clear()
    sess = _BindableSess()
    img1 = np.zeros((1, 3, 720, 1280), np.float32)
    pts, mask, ms = moge_post.forward(sess, img1, 1032)
    assert sess.io_binding_calls == 1 and sess.run_calls == 1
    assert pts.shape == (336, 598, 3) and ms == 1.0   # 原生点图（升采样在 infer_points）
    img2 = np.full((1, 3, 720, 1280), 0.5, np.float32)
    moge_post.forward(sess, img2, 1032)
    assert sess.io_binding_calls == 1          # 缓存复用：不重建 binding
    assert sess.run_calls == 2
    assert sess.run_seen_input_head == pytest.approx(0.5)  # 新帧已覆写进缓冲


class _NonBindableSess:
    """io_binding 缺失的会话替身（forward 负缓存路径锁）：读属性即抛。"""

    def __init__(self):
        self.attempts = 0
        self.run_calls = 0
        self._pts, self._mask = _synthetic_affine_points(1.5)

    def get_inputs(self):
        class _In:
            name = "image"
        return [_In()]

    def get_outputs(self):
        class _Out:
            def __init__(self, name):
                self.name = name
        return [_Out("points"), _Out("normal"), _Out("mask"), _Out("scale")]

    @property
    def io_binding(self):
        self.attempts += 1
        raise AttributeError("no io_binding")

    def run(self, _names, _feeds):
        self.run_calls += 1
        return [self._pts[None], np.zeros((1, 8, 598, 3), np.float32),
                self._mask[None].astype(np.float32),
                np.array([1.0], np.float32)]


def test_forward_falls_back_and_negatively_caches_non_bindable():
    """无 io_binding 能力的会话（测试桩类）：降级普通 run，且只试探一次。"""
    moge_post._BINDINGS.clear()
    sess = _NonBindableSess()
    img = np.zeros((1, 3, 720, 1280), np.float32)
    for _ in range(2):
        pts, mask, ms = moge_post.forward(sess, img, 1032)
        assert pts.shape == (336, 598, 3) and ms == 1.0
    assert sess.run_calls == 2                # 两帧都走普通 run
    assert sess.attempts == 1                 # 负缓存：第二次不再试探


def test_forward_shape_drift_drops_binding_for_that_frame():
    """输入形状漂移：该帧走普通 run，不把漂移帧写进固定缓冲。"""
    moge_post._BINDINGS.clear()
    sess = _BindableSess()
    img = np.zeros((1, 3, 720, 1280), np.float32)
    moge_post.forward(sess, img, 1032)
    assert sess.run_calls == 1
    odd = np.zeros((1, 3, 640, 960), np.float32)
    moge_post.forward(sess, odd, 1032)
    assert sess.run_calls == 2                   # 漂移帧降级
    moge_post.forward(sess, img, 1032)
    assert sess.run_calls == 3 and sess.io_binding_calls == 1  # 常规帧仍走 binding


# ── 异步解耦协议（AsyncDepthRoadObserver，stub 零数据依赖）────────────────


def _mk_reading(lane: float = -0.4) -> DepthRoadReading:
    return DepthRoadReading(left_edge_lane=lane, right_edge_lane=None,
                            left_x=300.0, right_x=None, sides=1,
                            latency_ms=1.0, rejects=("R:无穿越",))


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


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), np.uint8)


def test_async_roundtrip_resident_consume():
    """push→worker→take 闭环；结果驻留：同一结果可多拍复用（is_new 首次 True
    其后 False），applied 只计新结果一次——使用次数 ≠ 学习次数。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        r1, new1 = None, None
        for _ in range(200):            # 轮询拿首读（worker 异步发布）
            r1, new1 = a.take()
            if r1 is not None:
                break
            _time.sleep(0.01)
        assert r1 is not None and new1 is True, "首次消费应标记 is_new"
        r2, new2 = a.take()
        assert r2 is r1 and new2 is False, "驻留槽应复用同结果且不再标记新证据"
        assert a.health()["applied"] == 1, "applied 只计新结果一次"
    finally:
        assert a.stop() is True


def test_async_stale_gate_drops():
    """age 闸：max_age_ms≈0 时已发布结果按 stale 丢弃（take 返回 None），
    驻留槽清空——后续拍持续 None 且 stale 不重复累计。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub, max_age_ms=0.001)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: a.take()[0] is None
                           and (h := a.health())["applied"] + h["stale_drops"] >= 1), \
            "消费侧未把超龄结果判为 stale"
        assert a.health()["stale_drops"] == 1
        assert a.take() == (None, False), "清槽后不应重复计 stale 或吐旧结果"
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
    assert a.take() == (None, False)
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
        assert a.take() == (None, False)
        h = a.health()
        assert h["applied"] == 0 and h["stale_drops"] == 0 and h["failures"] == 0
    finally:
        a.stop()


# ── DML 互斥（core.dml_lock）：两会话并发 run 会段错误杀进程，2026-09-25 实证 ──

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


def _mk_evid():
    pts, mask = _synthetic_affine_points(0.652)
    return {"pts": pts, "valid": mask, "fx": 0.652, "fy": 0.65}


def test_async_debug_writes_replayable_evidence(tmp_path):
    """调试图同拍落可复现证据包：336×598 原生点图 fp16 + valid + 原生焦距——
    只有有损渲染时，实机读数故障无法离线重放（2026-09-25 21:41 局的教训）。"""
    stub = _StubObserver([_mk_reading()])
    stub._ego_mask = np.zeros((720, 1280), bool)
    stub._ego_mask[:10, :10] = True
    def _evid_observe(frame, object_mask=None):
        stub.calls += 1
        return _mk_reading(), _mk_evid()
    stub.observe_debug = _evid_observe
    a = AsyncDepthRoadObserver(stub, debug_dir=tmp_path)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: stub.calls >= 1)
        # 渲染+JPEG 编码在 worker 线程要几百 ms：轮询等落盘，别按调用数硬等
        evid_p = next(iter(tmp_path.glob("d*_evid.npz")), None) if _wait_until(
            lambda: any(tmp_path.glob("d*_evid.npz"))) else None
        assert evid_p is not None, "证据包未落盘"
        z = None
        for _ in range(50):            # glob 见到文件≠写完（1.2MB npz 有写入窗）
            try:
                z = np.load(evid_p)
                break
            except Exception:
                _time.sleep(0.05)
        assert z is not None, "证据包读取失败（写入窗竞态）"
        assert z["pts"].shape == (336, 598, 3) and z["pts"].dtype == np.float16
        assert list(tmp_path.glob("d*.jpg")), "渲染图未落盘"
        mask = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
        assert mask.all()
    finally:
        a.stop()


# ── 数据锚点（skipif：无离线缓存则跳；缓存=金标帧 MoGe 点云）─────────────

def _anchor_rows():
    import csv
    gold = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
            / "speedrush" / "depth_review" / "gold_labels.csv")
    if not gold.exists():
        return []
    return list(csv.DictReader(gold.open(encoding="utf-8")))


@pytest.mark.parametrize("stem,l_lo,l_hi,r_lo,r_hi", [
    # 读数锁（车道单位）：直路帧干净侧 ≤0.5m 口径（对质验收 2026-10-01）
    ("000906", -3.6, -2.6, 1.4, 2.4),
    ("000340", -3.2, -2.2, 1.2, 2.2),
])
def test_anchor_clean_straight_frames(stem, l_lo, l_hi, r_lo, r_hi):
    if not NPY.exists():
        pytest.skip(f"离线点云缓存缺失：{NPY}")
    f = NPY / f"{stem}.npz"
    if not f.exists():
        pytest.skip(f"该帧未缓存：{f}")
    z = np.load(f)
    pts = z["pts"].astype(np.float32)
    rd = reading_from_points(pts, float(z["fx"]), CAL,
                             ego_mask=DepthRoadObserver._load_ego_mask())
    assert rd.sides == 2, f"rejects={rd.rejects}"
    assert l_lo <= rd.left_edge_lane <= l_hi, f"L={rd.left_edge_lane}"
    assert r_lo <= rd.right_edge_lane <= r_hi, f"R={rd.right_edge_lane}"


# ---------- render_depth_debug：锚点画回真实像素（Y 轴约定无关） ----------

def _yellow_px(img):
    """纯黄 (0,255,255) 像素坐标 (row, col)。"""
    return np.argwhere((img[:, :, 0] < 60) & (img[:, :, 1] > 200)
                       & (img[:, :, 2] > 200))


@pytest.mark.parametrize("up_positive", [False, True],
                         ids=["y_down", "y_up"])
def test_render_anchors_at_true_pixels(up_positive):
    """调试图黄锚必须落在「检出穿越真实所在像素」（①原图 + ②高度图同位）。

    旧解析回投 v = h/2 + (b·zc+c)·fy/zc 直接吃平面系数、不走 hgt 的 sgn
    翻转——点云 Y 轴约定朝上时行数为负，黄圈全部画飞（2026-10-02 挂账实证：
    恰好挡住「锚到底锁在哪个结构」的肉眼复核）。改按 (side, zc, x_m) 反查
    带内点云像素取中位后，两种约定下锚点都必须压在缘底上。"""
    pts = _scene_points(x_left=WALL, x_right=WALL)
    if up_positive:
        pts = pts * np.array([1.0, -1.0, 1.0], np.float32)
    rd = reading_from_points(pts, FX, CAL, fy=FY)
    assert rd.edge_pts, f"场景须有检出缘 rejects={rd.rejects}"
    evid = {"pts": pts.astype(np.float32),
            "valid": np.isfinite(pts[..., 0]),
            "fx": np.float64(FX / 1280), "fy": np.float64(FY / 720)}
    img = dg.render_depth_debug(np.full((720, 1280, 3), 128, np.uint8),
                                evid, rd, None, None, None)
    band_h = dg.DIAG_Y1 - dg.Y0
    ys = _yellow_px(img[720:720 + band_h])
    assert len(ys) > 0, "高度图带内无黄锚（画飞或未画）"
    # 真值（解析，约定无关——翻转 Y 不动 (X,Z)，缘底像素两场景同位）：
    # 检出穿越 (xm, zc) 的投影列 + 该深度的地面行。
    wr = 0
    for _s, zc, xm in rd.edge_pts:
        u_t = CX + xm * FX / zc
        v_t = CY + FY * (H_CAM + PITCH_B * zc) / zc
        pts2 = np.stack([ys[:, 1], ys[:, 0] + dg.Y0], 1).astype(float)
        d = np.hypot(pts2[:, 0] - u_t, pts2[:, 1] - v_t)
        wr += int((d < 15).sum())
    assert wr >= len(rd.edge_pts), f"黄锚未全部压在缘底真值上（命中 {wr}）"
    ytop = _yellow_px(img[:720])
    assert len(ytop) > 0, "原图未画锚"
    hit = 0
    for _s, zc, xm in rd.edge_pts:
        u_t = CX + xm * FX / zc
        v_t = CY + FY * (H_CAM + PITCH_B * zc) / zc
        d = np.hypot(ytop[:, 1].astype(float) - u_t,
                     ytop[:, 0].astype(float) - v_t)
        hit += int((d < 15).sum())
    assert hit >= len(rd.edge_pts), "原图黄锚未压在缘底真值上"


# ---------- _scan_side 穿越质量门：缘阶差 + 尾部持续（2026-10-02 真机剖面） ----

def _profile_points(spec, side=1, noise=0.01, seed=11):
    """侧向剖面 spec=[(x_m, h_m), ...]（格中心,格中位高）→ _scan_side 直吃点集。

    每格撒 MIN_BIN_PTS 个点（格内均匀 + 噪声），构造出与实机同形的横向剖面。
    格心必须对齐扫描网格（−X_MAX+DX/2 起 0.25 步进），错位会把点摊进邻格
    饿死 MIN_BIN_PTS。"""
    rng = np.random.default_rng(seed)
    xs, hs = [], []
    grid = -dg.X_MAX_M + dg.XBIN_DX / 2 + dg.XBIN_DX * np.arange(
        int(2 * dg.X_MAX_M / dg.XBIN_DX))
    for x, h in spec:
        gx = grid[int(np.argmin(np.abs(grid - x)))]   # 吸附到最近格心
        n = dg.MIN_BIN_PTS
        xs.append(gx - dg.XBIN_DX / 2 + dg.XBIN_DX * (np.arange(n) + 0.5) / n)
        hs.append(np.full(n, h) + rng.normal(0, noise, n))
    x = np.concatenate(xs)
    return (x * side).astype(np.float32), np.concatenate(hs).astype(np.float32)


_ROAD = [(x * dg.XBIN_DX + dg.XBIN_DX / 2, 0.0)
         for x in range(16)]                    # 0~4m 平路面


def test_scan_side_wall_step_detected():
    """真缘（墙台阶 0.45m）→ 穿越检出在台阶处。"""
    wall = [(4.125 + 0.25 * k, h) for k, h in enumerate((0.45, 0.70, 0.85))]
    xb, hb = _profile_points(_ROAD + wall)
    ex = dg._scan_side(xb, hb, 1, zc=5.0, extent=3.0)
    assert np.isfinite(ex) and 3.85 < ex < 4.15, f"ex={ex}"


def test_scan_side_rejects_two_bin_bump():
    """两格宽影子 bump（d00017 L 实测形态 +0.06/+0.09 后回落）→ 无穿越。

    「两格持续」挡不住恰好两格宽的 bump；尾部持续门：bump 后剖面回落路面。
    （平面残差缓坡类假缘尾部也持续，本门杀不掉——与真 kerb 剖面同形，
    离线语料级判据另立，见 depth_geo EDGE_TAIL_FRAC 注释挂账。）"""
    bump = list(_ROAD)
    bump[5] = (5 * dg.XBIN_DX + dg.XBIN_DX / 2, 0.06)
    bump[6] = (6 * dg.XBIN_DX + dg.XBIN_DX / 2, 0.09)
    xb, hb = _profile_points(bump)
    ex = dg._scan_side(xb, hb, 1, zc=5.0, extent=3.0)
    assert not np.isfinite(ex), f"bump 被当缘：ex={ex}"


def test_scan_side_kerb_step_detected():
    """矮 kerb（0.15m 台阶，缘后持续抬高）→ 检出（kerb 与护栏两类通用口径）。"""
    kerb = [(x * dg.XBIN_DX + dg.XBIN_DX / 2, 0.15 + 0.01 * x)
            for x in range(12, 16)]
    xb, hb = _profile_points(_ROAD + kerb)
    ex = dg._scan_side(xb, hb, 1, zc=5.0, extent=3.0)
    assert np.isfinite(ex) and 2.9 < ex < 3.2, f"ex={ex}"
