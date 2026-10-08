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
import cv2

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

    def observe_debug(self, frame, object_mask=None, detections=None,
                      fid=None, frame_ts_ns=None):
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
            r1, g1, new1 = a.take()
            if r1 is not None:
                break
            _time.sleep(0.01)
        assert r1 is not None and new1 is True, "首次消费应标记 is_new"
        r2, g2, new2 = a.take()
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
        assert a.take() == (None, None, False), "清槽后不应重复计 stale 或吐旧结果"
        assert a.health()["stale_drops"] == 1
    finally:
        a.stop()


def test_async_stale_at_start_counted():
    """开工即超龄的启动记 stale_starts（浪费算力账）：行为不变（observe 仍
    执行），只记账——为 value-driven 调度攒「哪些计算不该启动」的判据。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub, max_age_ms=0.001)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame())
        assert _wait_until(lambda: a.health()["stale_starts"] >= 1), \
            "开工时帧龄已超 age 闸的启动应计入 stale_starts"
        assert stub.calls == 1, "stale 账不改行为：observe 仍应执行"
    finally:
        a.stop()


def test_async_cap_pre_segments_telescope():
    """cap 内段拆解（性能第二批）：push 携带拍内时间戳链（拾取→录制→检测
    →掩码，perf_counter 秒、与 frame_ts_ns 同钟）→ last_segments 展开
    cap_* 五段；同钟同线程严格伸缩，Σ内段 ≡ cap_ms。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        now = _time.perf_counter()
        a.push(_frame(), frame_ts_ns=int((now - 0.040) * 1e9),
               cap_pre={"pick": now - 0.030, "rec": now - 0.028,
                        "det": now - 0.015, "mask": now - 0.001})
        r = None
        for _ in range(200):
            r, _, new = a.take()
            if r is not None:
                break
            _time.sleep(0.005)
        assert r is not None
        seg = a.last_segments
        assert seg["cap_ms"] == pytest.approx(40.0, abs=3.0)
        assert seg["cap_pick_ms"] == pytest.approx(10.0, abs=1.0)
        assert seg["cap_rec_ms"] == pytest.approx(2.0, abs=1.0)
        assert seg["cap_det_ms"] == pytest.approx(13.0, abs=1.0)
        assert seg["cap_mask_ms"] == pytest.approx(14.0, abs=1.0)
        assert seg["cap_copy_ms"] == pytest.approx(1.0, abs=3.0)
        total = sum(seg[k] for k in ("cap_pick_ms", "cap_rec_ms",
                                     "cap_det_ms", "cap_mask_ms",
                                     "cap_copy_ms"))
        assert total == pytest.approx(seg["cap_ms"], abs=1.0)
        # worker 封包的启动分账随段走到消费面（trace dgeo_seg.reason 的供数源）
        assert seg["reason"] in ("interval_due", "stale_at_start")
    finally:
        a.stop()


def test_async_cap_pre_partial_and_legacy_shape():
    """cap_pre 缺环（录制关闭→rec=None）：相邻在环段照算、缺环段不出现；
    不传 cap_pre 保持旧三段形态（cap/queue/pub），无 cap_* 内段。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        now = _time.perf_counter()
        a.push(_frame(), frame_ts_ns=int((now - 0.020) * 1e9),
               cap_pre={"pick": now - 0.010, "rec": None, "det": now - 0.005})
        r = None
        for _ in range(200):
            r, _, new = a.take()
            if r is not None:
                break
            _time.sleep(0.005)
        assert r is not None
        seg = a.last_segments
        assert seg["cap_pick_ms"] == pytest.approx(10.0, abs=1.0)
        assert "cap_rec_ms" not in seg
        assert seg["cap_det_ms"] == pytest.approx(5.0, abs=1.0)
        assert "cap_mask_ms" not in seg
        assert "cap_copy_ms" in seg
    finally:
        a.stop()

    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame(), frame_ts_ns=int(_time.perf_counter() * 1e9))
        r = None
        for _ in range(200):
            r, _, new = a.take()
            if r is not None:
                break
            _time.sleep(0.005)
        assert r is not None
        seg = a.last_segments
        assert "cap_ms" in seg and "queue_ms" in seg
        assert not [k for k in seg if k.startswith("cap_") and k != "cap_ms"]
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
    assert a.take() == (None, None, False)
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
        assert a.take() == (None, None, False)
        h = a.health()
        assert h["applied"] == 0 and h["stale_drops"] == 0 and h["failures"] == 0
    finally:
        a.stop()


# ---------- 分段计时（端到端延迟归因：cap/queue/pub 三段 + MoGe stages）----------

def test_timing_segments_math():
    """纯函数口径：三段=已知打点差；缺 cap_ns 无该键（age 外段不可得）；
    跨线程时钟微抖夹紧 0 不产假负段。"""
    seg = dg._timing_segments(1_000_000_000, 1.5, 1.6, 1.7, 1.75)
    assert seg["cap_ms"] == pytest.approx(500.0)      # 入队 − 采集回调
    assert seg["queue_ms"] == pytest.approx(100.0)    # 开工 − 入队
    assert seg["pub_ms"] == pytest.approx(50.0)       # 消费 − 发布
    assert "cap_ms" not in dg._timing_segments(None, 1.5, 1.6, 1.7, 1.75)
    clamp = dg._timing_segments(None, 1.6, 1.5, 1.7, 1.69)
    assert clamp["queue_ms"] == 0.0 and clamp["pub_ms"] == 0.0


def test_async_segments_on_new_evidence_only():
    """分段随新证据消费落位：cap 段覆盖 push 前预设的采集回调延迟；
    驻留复用拍不刷新 last_segments。"""
    stub = _StubObserver([_mk_reading()])
    a = AsyncDepthRoadObserver(stub)  # type: ignore[arg-type]
    a.start()
    try:
        a.push(_frame(), frame_ts_ns=_time.perf_counter_ns() - 8_000_000)
        r, new = None, False
        for _ in range(200):
            r, _, new = a.take()
            if r is not None:
                break
            _time.sleep(0.01)
        assert r is not None and new, "新证据首拍才落分段"
        seg = a.last_segments
        assert seg is not None and seg["cap_ms"] >= 8.0, \
            "cap 段应覆盖采集回调→入队（含预设 8ms）"
        assert seg["queue_ms"] >= 0.0 and seg["pub_ms"] >= 0.0
        assert isinstance(seg["stages"], dict)
        keep = dict(seg)
        a.take()                                    # 驻留复用拍
        assert a.last_segments == keep, "复用拍重复记录无信息，不得刷新"
    finally:
        assert a.stop() is True


def test_stage_plane_key_written_by_reading():
    """plane 分段由 reading_from_points 写入 LAST_STAGE_MS（逐帧快照键位锁）
    ——后处理拆账的三段（plane/edges/grid）必须都能进 dgeo_seg.stages；
    平面拟合失败早退路径也落计时。"""
    pts = np.full((720, 1280, 3), np.nan, np.float32)
    r = dg.reading_from_points(pts, 1000.0, None)
    assert r.sides == 0 and "平面拟合失败" in r.rejects
    assert dg.LAST_STAGE_MS["plane"] >= 0.0


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
    def _evid_observe(frame, object_mask=None, detections=None,
                      fid=None, frame_ts_ns=None):
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
    # 读数锁（车道单位）：真实路物（墙基/kerb 台阶）±0.5m 口径（2026-10-03
    # 语料普查重推导：缘体证据门换装后读数落在固定路物上——000100 双侧墙基、
    # 000906 R 墙基、000340 L 墙基为重推导区间；000906 L 读数未变沿用原区间；
    # 000340 R 为真 kerb（四箱一致），沿用对质验收原值）
    ("000100", -3.07, -2.77, 2.63, 2.92),
    ("000906", -3.6, -2.6, 2.66, 2.96),
    ("000340", -3.14, -2.85, 1.2, 2.2),
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


def test_render_decision_band_note_dict_does_not_throw():
    """决策带（note 非 None）必须可渲染——曾 _f 先用后定义 UnboundLocalError
    被 _write_debug 的静默 try 吞掉，实机调试图零产出（10-03/10-04 四局
    depth_debug 目录全空、mkdir 有目录无文件）。note 每拍必传，此路径必炸。"""
    rd = dg.DepthRoadReading(left_edge_lane=-2.0, right_edge_lane=2.0,
                             left_x=100, right_x=540, sides=2, latency_ms=130.0)
    note = {"fid": 123, "state": "CRUISE", "reason": "hold:no_candidate",
            "steer": 0.1, "elane": 0.2, "xt": 0.3, "ro": -0.1, "src": "pair",
            "ro_raw": -0.12, "age": 301.0, "new": False}
    img = dg.render_depth_debug(np.full((720, 1280, 3), 128, np.uint8),
                                None, rd, None, None, note)
    assert img.shape == (720 + (dg.DIAG_Y1 - dg.Y0) + 56, 1280, 3)


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


def test_scan_side_rejects_pure_residual_ramp():
    """平面残差缓坡（单调爬坡越阈、缘后持续爬升）→ 无穿越。

    第八轮挂账案（211445/d00003 L 实测 0.085/m 爬升、跨箱仅漂 0.79——跨箱
    稳定性杀不掉）：逐格 0.02 爬升，穿越后剖面持续高（过尾部持续门）但既无
    脸（单格增量 ≤0.02 < EDGE_FACE_MIN）也无平台。"""
    ramp = [(4.125 + 0.25 * k, 0.02 * (k + 1)) for k in range(12)]
    xb, hb = _profile_points(_ROAD + ramp)
    ex = dg._scan_side(xb, hb, 1, zc=5.0, extent=10.0)
    assert not np.isfinite(ex), f"缓坡被当缘：ex={ex}"


def test_scan_side_ramp_lands_on_wall_behind():
    """缓坡接真墙：缓坡段穿越全被缘体证据门否决，检出落到墙台阶。

    墙台阶以 above-above 对出现（缓坡尾格已在阈上）——缘基取对首格
    px[i]，禁 (i−1→i) 线性外插（000906 R zc10 外插出 −5.6/+30.6 幽灵
    穿越实证；旧码此处先被缓坡穿越截住，两处修复共同落位）。"""
    ramp = [(4.125 + 0.25 * k, 0.02 * (k + 1)) for k in range(8)]   # 0.02~0.16
    wall = [(6.125 + 0.25 * k, h) for k, h in enumerate((0.50, 0.52, 0.51))]
    xb, hb = _profile_points(_ROAD + ramp + wall)
    ex = dg._scan_side(xb, hb, 1, zc=5.0, extent=10.0)
    assert np.isfinite(ex) and 5.4 <= ex <= 6.1, f"ex={ex}"


def test_scan_side_rejects_low_shoulder_plateau():
    """缓坡肩台（平台高度 1.25×阈 <1.5×）≠ 缘体 → 无穿越。

    000906 R zc10 语料形态：缓坡爬到 0.077~0.081（1.13~1.19×阈）后近平，
    平台形状成立但高度不够——肩台不是物体。"""
    shoulder = [(4.125 + 0.25 * k, h) for k, h in enumerate(
        (0.02, 0.04, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05))]
    xb, hb = _profile_points(_ROAD + shoulder)
    ex = dg._scan_side(xb, hb, 1, zc=5.0, extent=10.0)
    assert not np.isfinite(ex), f"肩台被当缘：ex={ex}"


def test_scan_side_smeared_far_kerb_kept_by_plateau():
    """远场 smeared kerb（逐格爬 0.015 不足脸、顶部平台 0.145=1.7×阈）→ 检出。

    000340 R zc14 语料形态：脸门单独会误杀远缘，平台条款兜底——回归锁，
    拆平台条款此测红。"""
    kerb = [(4.125 + 0.25 * k, h) for k, h in enumerate(
        (0.069, 0.080, 0.095, 0.109, 0.128, 0.145,
         0.145, 0.144, 0.145, 0.143))]
    xb, hb = _profile_points(_ROAD + kerb)
    ex = dg._scan_side(xb, hb, 1, zc=14.0, extent=10.0)
    assert np.isfinite(ex) and 4.3 < ex < 4.6, f"ex={ex}"


# ---------- 后处理向量化的等价回归锁（2026-10-04）------------------------
#
# 改造把 _scan_side 的「逐格 while 循环 + 逐格 np.median」换成「一次算格号 +
# 复合键排序取格中位」，并把 Z 无关掩码提出 ZBIN 循环、给平面迭代加提前收敛。
# 本锁把**改造前的逐格实现**原样留在测试里当参照，对随机点云、格边界应力集与
# 既有合成场景断言两者输出**严格相等**（不是近似）——等价性由这一节守，不靠
# 一次性人工比对。


def _ref_scan_side(xb: np.ndarray, hb: np.ndarray, side: int, zc: float,
                   extent: float) -> float:
    """改造前实现（逐格循环 + 逐格 np.median）——参照，勿随产线改动。"""
    sel = (xb * side > 0.2) & (np.abs(xb) <= dg.X_MAX_M) \
        & np.isfinite(xb) & np.isfinite(hb)
    if sel.sum() < dg.MIN_SIDE_PTS:
        return np.nan
    xs, hs = xb[sel], hb[sel]
    prof_x: list[float] = []
    prof_h: list[float] = []
    x0 = -dg.X_MAX_M + dg.XBIN_DX / 2
    while x0 < dg.X_MAX_M:
        if x0 * side > 0 and abs(x0) <= extent * zc:
            m = (xs >= x0 - dg.XBIN_DX / 2) & (xs < x0 + dg.XBIN_DX / 2)
            if m.sum() >= dg.MIN_BIN_PTS:
                prof_x.append(float(x0))
                prof_h.append(float(np.median(hs[m])))
        x0 += dg.XBIN_DX
    if len(prof_x) < 3:
        return np.nan
    px = np.array(prof_x)
    ph = np.array(prof_h)
    srt = np.argsort(px * side)
    px, ph = px[srt], ph[srt]
    thr = dg.EDGE_HT + dg.EDGE_HT_SLOPE * max(0.0, zc - 3.0)
    g = -1
    for i in range(len(px)):
        if ph[i] <= thr:
            g = i
            break
    if g < 0:
        return np.nan
    for i in range(g + 1, len(px) - 1):
        if ph[i] > thr and ph[i + 1] > thr:
            tail = ph[i + 2:]
            if len(tail) and (tail > thr).mean() < dg.EDGE_TAIL_FRAC:
                continue
            genuine = ph[i - 1] <= thr
            if not dg._edge_body_evidence(ph, i, thr, genuine):
                continue
            if genuine:
                f = (thr - ph[i - 1]) / (ph[i] - ph[i - 1])
                return float(px[i - 1] + f * (px[i] - px[i - 1]))
            return float(px[i])
    return np.nan


def _assert_same_ex(a: float, b: float, msg: str) -> None:
    """严格相等（nan==nan 视为相等；不做近似）。"""
    if np.isnan(a) and np.isnan(b):
        return
    assert a == b, f"{msg}: ref={a!r} new={b!r}"


@pytest.mark.parametrize("seed", range(24))
def test_scan_side_matches_reference_on_random_clouds(seed):
    """随机点云（含 nan/±inf/野值）上新旧 _scan_side 输出严格相等。"""
    rng = np.random.default_rng(seed)
    n = int(rng.integers(200, 5000))
    xb = rng.uniform(-14.0, 14.0, n).astype(np.float32)
    # 外侧（|x|>4）抬高一截，制造真缘/缓坡混合剖面
    hb = (rng.normal(0.0, 0.05, n)
          + (np.abs(xb) > 4.0) * rng.uniform(0.0, 0.8, n)).astype(np.float32)
    k = int(rng.integers(1, max(2, n // 20)))
    idx = rng.choice(n, size=min(k, n), replace=False)
    xb[idx[: len(idx) // 2]] = np.nan
    hb[idx[len(idx) // 2:]] = np.nan
    for j in rng.choice(n, size=min(4, n), replace=False):
        hb[j] = rng.choice([np.inf, -np.inf, 1e6, -1e6])
    for j in rng.choice(n, size=min(4, n), replace=False):
        xb[j] = rng.choice([np.inf, -np.inf, 13.9, -13.9])
    for side in (1, -1):
        for zc in (4.0, 5.0, 10.0, 14.0):
            for extent in (2.0, 3.0, 10.0):
                a = _ref_scan_side(xb, hb, side, zc, extent)
                b = dg._scan_side(xb, hb, side, zc, extent)
                _assert_same_ex(a, b, f"seed={seed} side={side} zc={zc} ext={extent}")


def test_scan_side_matches_reference_on_bin_edges():
    """格边界归属（左闭右开 [−12+0.25k, −12+0.25(k+1))）上新旧同判定。

    点精确落在格边界上是最容易分箱漂的位置（浮点 floor vs 区间比较）；
    每个边界塞 MIN_BIN_PTS 个点，保证相邻格都有剖面值。"""
    edges = -dg.X_MAX_M + dg.XBIN_DX * np.arange(dg.N_XBIN + 1)
    xs = np.concatenate([np.full(dg.MIN_BIN_PTS, e, np.float32) for e in edges])
    hs = np.concatenate([np.full(dg.MIN_BIN_PTS, 0.02 * i, np.float32)
                         for i in range(edges.size)])
    for side in (1, -1):
        for zc in (5.0, 14.0):
            a = _ref_scan_side(xs, hs, side, zc, 10.0)
            b = dg._scan_side(xs, hs, side, zc, 10.0)
            _assert_same_ex(a, b, f"edges side={side} zc={zc}")


@pytest.mark.parametrize("seed", range(12))
def test_binned_median_matches_numpy_median(seed):
    """复合键分箱中位数与逐格 ``np.median`` **逐位**同值（含偶数格均值）。"""
    rng = np.random.default_rng(2000 + seed)
    n = int(rng.integers(40, 6000))
    scale = (1.0, 1e-3, 1e3, 1e6)[seed % 4]
    vals = (rng.normal(0.0, 1.0, n) * scale).astype(np.float32)
    nb = int(rng.integers(3, 50))
    bi = rng.integers(0, nb, n).astype(np.int64)
    counts = np.bincount(bi, minlength=nb)
    want = np.nonzero(counts)[0]
    if want.size == 0:
        return
    got = dg._binned_median(vals, bi, want, counts)
    exp = np.array([np.median(vals[bi == k]) for k in want], np.float64)
    assert got.dtype == np.float64
    np.testing.assert_array_equal(got.view(np.uint64), exp.view(np.uint64))


@pytest.mark.parametrize("case", [
    "both_walls", "single_wall", "object_mask", "near_barrier", "bend_bins",
])
def test_reading_from_points_matches_reference_pipeline(monkeypatch, case):
    """端到端等价：把 _scan_side 换回逐格参照实现，reading_from_points 的全部
    输出字段（lane/像素锚/sides/rejects/edge_pts）必须一字不差。

    这一条同时覆盖「掩码提出 ZBIN 循环」与「平面迭代提前收敛」两处改动。"""
    if case == "both_walls":
        pts = _scene_points(x_left=WALL, x_right=WALL)
        obj = None
    elif case == "single_wall":
        pts = _scene_points(x_right=WALL)
        obj = None
    elif case == "object_mask":
        pts = _scene_points(x_left=WALL, x_right=WALL)
        obj = np.zeros((720, 1280), bool)
        obj[:, 900:] = True
    elif case == "near_barrier":
        pts = _scene_points(x_left=WALL, x_right=WALL,
                            boxes=((-4.5, -NEAR_BARRIER_X, 5.0, 9.0, 0.8),))
        obj = None
    else:                                   # 弯道形态：逐箱漂移、退回中位
        pts = _scene_points(x_left=None, x_right=WALL, boxes=(
            (-2.75, -2.5, 3.0, 5.0, 1.0), (-3.75, -3.5, 5.0, 7.0, 1.0),
            (-4.75, -4.5, 7.0, 9.0, 1.0)))
        obj = None
    ego = DepthRoadObserver._load_ego_mask()
    new = reading_from_points(pts, FX, CAL, ego_mask=ego, object_mask=obj, fy=FY)
    monkeypatch.setattr(dg, "_scan_side", _ref_scan_side)
    old = reading_from_points(pts, FX, CAL, ego_mask=ego, object_mask=obj, fy=FY)
    assert (old.left_edge_lane, old.right_edge_lane, old.left_x, old.right_x,
            old.sides, old.rejects, old.edge_pts) == \
           (new.left_edge_lane, new.right_edge_lane, new.left_x, new.right_x,
            new.sides, new.rejects, new.edge_pts), case


def test_reading_from_points_abstain_paths_match_reference(monkeypatch):
    """弃权路径等价：全 nan（平面拟合失败）与单侧无墙（一侧弃权）两路上，
    新旧实现同样一字不差——提前收敛改动不许改弃权行为。"""
    ego = DepthRoadObserver._load_ego_mask()
    for pts in (np.full((720, 1280, 3), np.nan, np.float32),
                _scene_points(x_right=WALL)):
        new = reading_from_points(pts, FX, CAL, ego_mask=ego, fy=FY)
        monkeypatch.setattr(dg, "_scan_side", _ref_scan_side)
        old = reading_from_points(pts, FX, CAL, ego_mask=ego, fy=FY)
        monkeypatch.undo()
        assert (old.left_edge_lane, old.right_edge_lane, old.left_x, old.right_x,
                old.sides, old.rejects, old.edge_pts) == \
               (new.left_edge_lane, new.right_edge_lane, new.left_x, new.right_x,
                new.sides, new.rejects, new.edge_pts)


def test_fit_road_plane_early_exit_matches_full_iterations(monkeypatch):
    """平面迭代提前收敛不改结果：把 PLANE_ITERS 临时放大到 6 轮（早收失效、
    必跑满），系数必须与产线路径逐位相同。"""
    pts = _scene_points(x_left=WALL, x_right=WALL)
    X, Y, Z = (pts[dg.Y0:dg.DIAG_Y1, :, k] for k in (0, 1, 2))
    dig = np.zeros((dg.DIAG_Y1 - dg.Y0, 1280), bool)
    for fy in (FY, None):        # 特征种子路径 + fy=None 回退路径
        fast = dg._fit_road_plane(X, Y, Z, dig, fy=fy)
        monkeypatch.setattr(dg, "PLANE_ITERS", 6)
        slow = dg._fit_road_plane(X, Y, Z, dig, fy=fy)
        monkeypatch.undo()
        assert fast is not None and slow is not None, f"fy={fy}"
        np.testing.assert_array_equal(fast, slow)


# ---------- 可行驶栅格 drivable_grid_from_points（2026-10-04 离线调查落产线）----
#
# 三态：0 未知（无证据）/ 1 可走 / 2 不可走（软代价）。判据=格内非挖洞点 hgt 中位：
# |中位|≤GRID_TOL → 可走，>TOL → 不可走；挖洞（ego/物体框）覆盖的格一律未知。


def _cell_state(grid, x_m: float, z_m: float) -> int:
    ix = int(np.floor((x_m + dg.GRID_X_MAX) / dg.GRID_CELL))
    iz = int(np.floor((z_m - dg.GRID_Z_LO) / dg.GRID_CELL))
    return int(grid.state[iz, ix])


def _band_rows(z0: float, z1: float) -> np.ndarray:
    iz = np.arange(dg.GRID_NZ)
    zc = dg.GRID_Z_LO + (iz + 0.5) * dg.GRID_CELL
    return (zc >= z0) & (zc < z1)


def test_drivable_grid_double_wall_road_green_walls_red():
    """双墙：路中可走、墙格不可走、墙外无点未知——三态各就位。"""
    pts = _scene_points(x_left=WALL, x_right=WALL)
    g = dg.drivable_grid_from_points(pts, FX, FY)
    assert g.coef is not None
    assert _cell_state(g, 0.0, 6.0) == dg.GRID_DRIVABLE
    # 墙在 |x|=WALL=5.5；z=12 时在视锥内（半宽 12·640/857≈9.0 > 5.5）
    assert _cell_state(g, WALL, 12.0) == dg.GRID_BLOCKED
    assert _cell_state(g, -WALL, 12.0) == dg.GRID_BLOCKED
    # 墙外（|x|>WALL）无点 → 未知（不得外推成可走）
    assert _cell_state(g, WALL + 2.0, 12.0) == dg.GRID_UNKNOWN


def test_drivable_grid_single_wall_other_side_unknown():
    """单侧无墙：该侧缘外无点（模拟缘外虚空）→ 未知，不被外推成可走。"""
    pts = _scene_points(x_right=WALL)
    pts = pts.copy()
    pts[pts[..., 0] < -(WALL - 0.2)] = np.nan    # 左缘外抹掉证据（缘外虚空）
    g = dg.drivable_grid_from_points(pts, FX, FY)
    assert g.coef is not None
    assert _cell_state(g, 0.0, 6.0) == dg.GRID_DRIVABLE
    assert _cell_state(g, WALL, 12.0) == dg.GRID_BLOCKED      # 右墙仍在场
    assert _cell_state(g, -8.0, 8.0) == dg.GRID_UNKNOWN       # 左缘外无证据


def test_drivable_grid_dug_cells_all_unknown():
    """挖洞格全未知、0 误判：ego/物体框覆盖的格一律未知（证据被主动移除）。"""
    pts = _scene_points(x_left=WALL, x_right=WALL)
    X, Z = pts[..., 0], pts[..., 2]
    ego = np.isfinite(X) & (np.abs(X) < 1.5) & (Z > 4.5) & (Z < 8.0)
    g = dg.drivable_grid_from_points(pts, FX, FY, ego, None)
    assert g.coef is not None and g.dig_cells > 0
    # 洞内（略内缩避边界格）全未知；洞外照常可走
    for x in np.arange(-1.3, 1.4, 0.25):
        for z in np.arange(4.7, 7.9, 0.25):
            assert _cell_state(g, x, z) == dg.GRID_UNKNOWN, (x, z)
    assert _cell_state(g, 0.0, 12.0) == dg.GRID_DRIVABLE, "洞外路面被误杀"
    # 反查实现登记的挖洞格：无一被声明为可走/不可走
    Xb, Yb, Zb = (pts[dg.Y0:dg.DIAG_Y1, :, k] for k in (0, 1, 2))
    ok = (np.isfinite(Xb) & np.isfinite(Yb) & np.isfinite(Zb)
          & (Xb >= -dg.GRID_X_MAX) & (Xb < dg.GRID_X_MAX)
          & (Zb >= dg.GRID_Z_LO) & (Zb < dg.GRID_Z_HI))
    du = dg._dig_band(ego, 1280) & ok
    assert du.any()
    ix = np.floor((Xb[du] + dg.GRID_X_MAX) / dg.GRID_CELL).astype(int)
    iz = np.floor((Zb[du] - dg.GRID_Z_LO) / dg.GRID_CELL).astype(int)
    st = g.state[iz, ix]
    assert (st == dg.GRID_UNKNOWN).all(), f"挖洞格出现非未知态：{np.unique(st)}"


def test_drivable_grid_shared_coef_bitwise_identical():
    """coef 共享：传 reading 的平面与函数自拟合，栅格逐位同值——观察链传
    reading.coef 免掉一次列剖面重算（自拟合口径 ~34ms/帧），不允许改结果。"""
    pts = _scene_points(x_left=WALL, x_right=WALL)
    rd = dg.reading_from_points(pts, FX, CAL, fy=FY)
    assert rd.coef is not None
    g_shared = dg.drivable_grid_from_points(pts, FX, FY, coef=rd.coef)
    g_self = dg.drivable_grid_from_points(pts, FX, FY)
    assert (g_shared.state == g_self.state).all()
    assert (g_shared.counts == g_self.counts).all()
    assert g_shared.dig_cells == g_self.dig_cells
    assert g_shared.coef == g_self.coef


def test_drivable_grid_plane_fit_fail_all_unknown():
    """平面拟合失败（域内点不足）→ 全未知、coef=None、不抛异常（诚实弃权）。"""
    pts = np.full((720, 1280, 3), np.nan, np.float32)
    g = dg.drivable_grid_from_points(pts, FX, FY)
    assert g.coef is None
    assert (g.state == dg.GRID_UNKNOWN).all()
    assert int(g.counts.sum()) == 0 and g.dig_cells == 0


# ---- 代表帧回归（skipif：证据包不在盘则跳；assemble 口径同离线探针）--------

EV = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
      / "speedrush" / "control_traces")

# 140930/d00009 近场带 3~9m 内判 2（不可走）的格数基线（干净晴天宽路帧；含墙/
# 路缘/影；防恶化锁）。本值=本实现实测（±10.5m 窗、0.15m 容差、挖洞判未知）。
D09_NEAR_BLOCKED_BASELINE = 367


def _assemble_evid(stem: Path):
    """证据包 → (全幅点云, fx, fy, ego, obj)；口径同产线离线复算
    （336×598 fp16 点图各通道 resize 到 1280×720 + valid 最近邻 + invalid 置
    nan；ego 取资源紧矩形，obj = *_mask.npy 减 ego）。"""
    z = np.load(str(stem) + "_evid.npz")
    valid = np.unpackbits(z["valid"])[: 336 * 598].reshape(336, 598).astype(bool)
    pn = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pn[..., k], (1280, 720),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (1280, 720),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts = pts.copy()
    pts[~vf] = np.nan
    ego = DepthRoadObserver._load_ego_mask()
    obj = None
    mp = Path(str(stem) + "_mask.npy")
    if mp.exists():
        merged = np.unpackbits(np.load(mp))[: 1280 * 720] \
            .reshape(720, 1280).astype(bool)
        obj = merged & ~ego
    return pts, float(z["fx"]) * 1280, float(z["fy"]) * 720, ego, obj


def _evid_or_skip(dirname: str, seq: int) -> Path:
    stem = EV / dirname / f"d{seq:05d}"
    if not Path(str(stem) + "_evid.npz").exists():
        pytest.skip(f"证据包不在盘：{stem}")
    return stem


def test_drivable_grid_frame_anchor_dug_all_unknown():
    """175855/d00055（雨天超车帧）：挖洞格全灰——0 误判锁。"""
    stem = _evid_or_skip("depth_debug_20261004_175855", 55)
    pts, fx, fy, ego, obj = _assemble_evid(stem)
    g = dg.drivable_grid_from_points(pts, fx, fy, ego, obj)
    assert g.coef is not None and g.dig_cells > 0
    dig = dg._dig_band(ego, 1280)
    if obj is not None:
        dig = dig | dg._dig_band(obj, 1280)
    Xb, Yb, Zb = (pts[dg.Y0:dg.DIAG_Y1, :, k] for k in (0, 1, 2))
    ok = (np.isfinite(Xb) & np.isfinite(Yb) & np.isfinite(Zb)
          & (Xb >= -dg.GRID_X_MAX) & (Xb < dg.GRID_X_MAX)
          & (Zb >= dg.GRID_Z_LO) & (Zb < dg.GRID_Z_HI))
    du = dig & ok
    assert du.any(), "该帧应有挖洞格（超车帧 ego/物体框覆盖）"
    ix = np.floor((Xb[du] + dg.GRID_X_MAX) / dg.GRID_CELL).astype(int)
    iz = np.floor((Zb[du] - dg.GRID_Z_LO) / dg.GRID_CELL).astype(int)
    st = g.state[iz, ix]
    assert (st == dg.GRID_UNKNOWN).all(), \
        f"挖洞格出现非未知态：{np.unique(st)}"


def test_drivable_grid_frame_anchor_clean_frame_baseline():
    """140930/d00009（晴天宽路帧）：近场不可走格数不超基线——防恶化锁。"""
    stem = _evid_or_skip("depth_debug_20261004_140930", 9)
    pts, fx, fy, ego, obj = _assemble_evid(stem)
    g = dg.drivable_grid_from_points(pts, fx, fy, ego, obj)
    assert g.coef is not None
    n = int((g.state[_band_rows(dg.GRID_Z_LO, 9.0)] == dg.GRID_BLOCKED).sum())
    assert n <= D09_NEAR_BLOCKED_BASELINE, \
        f"近场不可走格数 {n} 超基线 {D09_NEAR_BLOCKED_BASELINE}"


def test_drivable_grid_obs_debug_attaches_grid():
    """observe_debug 在 reading 旁挂栅格：evid['grid'] 是 DrivableGrid 且与
    reading 同拍（异步 worker 消费链的数据面）。"""
    pts, mask = _synthetic_affine_points(0.652)

    class _Sess:
        def get_inputs(self):
            class _In:
                name = "image"
            return [_In()]

        def run(self, _n, _f):
            return [pts[None], np.zeros((1, 8, 598, 3), np.float32),
                    mask[None].astype(np.float32), np.array([1.0], np.float32)]

    obs = DepthRoadObserver(_Sess(), CAL)   # type: ignore[arg-type]
    reading, evid = obs.observe_debug(_frame_np())
    assert reading is not None and evid is not None
    assert isinstance(evid.get("grid"), dg.DrivableGrid)
    assert evid["grid"].state.shape == (dg.GRID_NZ, dg.GRID_NX)


def test_render_depth_debug_grid_panel_shape():
    """render 叠栅格小图：grid 非 None 时图高增加一栏；None 时形状不变。"""
    rd = dg.DepthRoadReading(left_edge_lane=-2.0, right_edge_lane=2.0,
                             left_x=100, right_x=540, sides=2, latency_ms=130.0)
    frame = np.full((720, 1280, 3), 128, np.uint8)
    base = dg.render_depth_debug(frame, None, rd, None, None, None)
    assert base.shape == (720 + (dg.DIAG_Y1 - dg.Y0), 1280, 3)
    g = dg.DrivableGrid(state=np.ones((dg.GRID_NZ, dg.GRID_NX), np.int8),
                        coef=(0.0, 0.0, 2.0),
                        counts=np.ones((dg.GRID_NZ, dg.GRID_NX), np.int32),
                        dig_cells=3, latency_ms=5.0)
    withg = dg.render_depth_debug(frame, None, rd, None, None, None, grid=g)
    assert withg.shape[1] == 1280 and withg.shape[0] > base.shape[0]


# ---------- grid_center_lane：栅格全带 wmid 路心（路心互证的供数口径） ----------

def _xg_grid(rows_cols, coef=(0.0, 0.0, 0.72)):
    """构造 DrivableGrid：rows_cols = [(行号, 列lo, 列hi), ...]，其余全 unknown。
    列→X 映射用真 GRID_NX（84 列），行数任意（跨行只做宽度加权）。"""
    st = np.full((4, dg.GRID_NX), dg.GRID_UNKNOWN, np.int8)
    for r, c0, c1 in rows_cols:
        st[r, c0:c1 + 1] = dg.GRID_DRIVABLE
    return dg.DrivableGrid(state=st, coef=coef,
                           counts=np.full(st.shape, 9, np.int32),
                           dig_cells=0, latency_ms=1.0)


def test_grid_center_lane_polarity_and_value():
    """极性契约（v3 事故链同款门禁）：绿区在 X=+1m → 返回 +1/lane_w_m
    （正值=路心在右）；消费方取 off=−值，与 off=−(L+R)/2 同式。列 44~47 的
    格界 [0.5, 1.5]m，范围中点恰为 +1m。"""
    g = _xg_grid([(1, 44, 47)])
    c = dg.grid_center_lane(g, 2.75)
    assert c == pytest.approx(1.0 / 2.75)
    assert c > 0


def test_grid_center_lane_range_midpoint_survives_wedge_split():
    """口径锁：逐行取绿区 [min,max] **范围中点**而非质心——ego 挖洞楔居中
    劈开绿区时中点仍落在真路心，质心会被楔拽偏。列 40~43 与 48~51 两段，
    格界 [−0.5, 2.5]m → 中点 +1.0m（质心不等值）。"""
    g = _xg_grid([(2, 40, 43), (2, 48, 51)])
    assert dg.grid_center_lane(g, 2.75) == pytest.approx(1.0 / 2.75)


def test_grid_center_lane_width_weighted_median_across_rows():
    """跨行按绿区宽度加权取中位：宽行压过窄行，单侧窄证据带不拽走全带路心。
    宽行 X∈[0.5,1.5]（中点 +1m，权重 1m）+ 窄行 X∈[−2.25,−2.0]（列 33，
    中点 −2.125m，权重 0.25m）→ 加权中位仍 +1m。"""
    g = _xg_grid([(0, 44, 47), (2, 33, 33)])
    assert dg.grid_center_lane(g, 2.75) == pytest.approx(1.0 / 2.75)


def test_grid_center_lane_honest_abstain():
    """无绿行（全 unknown / 拟合失败全 0 栅格）与非法 lane_w_m → None 弃权，
    不造观测。"""
    empty = _xg_grid([])
    assert dg.grid_center_lane(empty, 2.75) is None
    zero = dg.DrivableGrid(state=np.zeros((4, dg.GRID_NX), np.int8),
                           coef=None, counts=np.zeros((4, dg.GRID_NX), np.int32),
                           dig_cells=0, latency_ms=1.0)
    assert dg.grid_center_lane(zero, 2.75) is None
    g = _xg_grid([(1, 44, 47)])
    assert dg.grid_center_lane(g, 0.0) is None


# ---------- region_reading：绿区边界=路的物理边界（供数主人口径） ----------

def test_region_reading_sides2_and_polarity():
    """区读数极性与找边同鸭子面：绿区 X∈[0.5,1.5]m → 左缘 +0.5/2.75、右缘
    +1.5/2.75（左负右正，原点=车）；区中心 −(L+R)/2=−1.0/2.75 与 wmid 同值。"""
    g = _xg_grid([(1, 44, 47)])
    rr = dg.region_reading(g, 2.75)
    assert rr.sides == 2
    assert rr.left_edge_lane == pytest.approx(0.5 / 2.75)
    assert rr.right_edge_lane == pytest.approx(1.5 / 2.75)
    assert rr.n_rows == 1 and rr.left_clip_frac == 0.0 and rr.right_clip_frac == 0.0


def test_region_reading_interior_hole_immune():
    """车盒把绿区劈洞（列 44~47 被挖空、两侧绿）：[min,max] 边界不动——
    挖洞在中部对区缘免疫（与 wmid 同一构造性优势）。"""
    g = _xg_grid([(1, 40, 43), (1, 48, 51)])
    rr = dg.region_reading(g, 2.75)
    assert rr.sides == 2
    assert rr.left_edge_lane == pytest.approx(-0.5 / 2.75)
    assert rr.right_edge_lane == pytest.approx(2.5 / 2.75)


def test_region_reading_clip_side_honest():
    """绿区贴住横窗边界（列 0 起）→ 该侧是截断假缘：超 clip_frac 判 sides
    降级，不给 2（单侧诚实弃权，交保鲜槽/None 链路）。"""
    st = np.full((4, dg.GRID_NX), dg.GRID_UNKNOWN, np.int8)
    st[1, 0:40] = dg.GRID_DRIVABLE          # 左缘贴窗（lo_ix==0 比例 100%）
    g = dg.DrivableGrid(state=st, coef=(0.0, 0.0, 0.72),
                        counts=np.full(st.shape, 9, np.int32),
                        dig_cells=0, latency_ms=1.0)
    rr = dg.region_reading(g, 2.75)
    assert rr.sides == 1 and rr.left_edge_lane is None
    assert rr.left_clip_frac == pytest.approx(1.0)
    assert dg.region_reading(_xg_grid([]), 2.75).sides == 0
