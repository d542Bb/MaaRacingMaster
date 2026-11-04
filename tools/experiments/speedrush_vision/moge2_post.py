# -*- coding: utf-8 -*-
"""MoGe-2 官方 ONNX 的 numpy 后处理：focal/shift 恢复 + 点图重建。

官方 ONNX 只有 raw forward（输入 image fp32 RGB[0,1]（+动态版 num_tokens），
输出 points/normal/mask/metric_scale 仿射点图）；官方 infer() 在模型外做
recover_focal_shift → z 加 shift → force_projection 重投影 → ×metric_scale。
本模块按 microsoft/MoGe geometry_numpy.py / geometry_torch.py / v2.py 的
公式逐字 numpy 复刻该链路，供 compare_depth_sources 挂新源。

公式出处（2026-10-01 逐字核对官方源码）：
- normalized_view_plane_uv: span_x = ar/√(1+ar²), span_y = 1/√(1+ar²)
  （半对角归一），uv ∈ [-span·(n-1)/n, +span·(n-1)/n]，v 随行号向下递增。
- recover_focal_shift: uv/points 各自 masked 最近邻下采样到 64×64，
  solve_optimal_focal_shift：xy_proj = xy/(z+shift)，
  f = <xy_proj,uv>/<xy_proj,xy_proj>，残差 = f·xy_proj − uv，
  least_squares(method='lm', x0=0) 解 shift，focal 闭式回代；
  给定 focal 时退化为 solve_optimal_shift。返回 (focal, shift)。
- fx = focal/2·√(1+ar²)/ar，fy = focal/2·√(1+ar²)；归一化内参 cx=cy=0.5。
- 重投影：Z = points_z+shift，X=(u−0.5)·Z/fx，Y=(v−0.5)·Z/fy，全体 ×metric_scale。
"""

from __future__ import annotations

import cv2
import numpy as np

DOWN = 64  # 官方 recover_focal_shift 的下采样网格 (width, height)


def normalized_view_plane_uv(width: int, height: int) -> np.ndarray:
    """官方 geometry_numpy.normalized_view_plane_uv_numpy 逐字复刻。"""
    ar = width / height
    span_x = ar / (1 + ar**2) ** 0.5
    span_y = 1 / (1 + ar**2) ** 0.5
    u = np.linspace(-span_x * (width - 1) / width, span_x * (width - 1) / width,
                    width, dtype=np.float32)
    v = np.linspace(-span_y * (height - 1) / height, span_y * (height - 1) / height,
                    height, dtype=np.float32)
    u, v = np.meshgrid(u, v, indexing="xy")
    return np.stack([u, v], axis=-1)


def _masked_nearest_resize(a: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """utils3d.masked_nearest_resize 的近似：窗口内取首个有效像素（官方按窗内最近有效）。"""
    H, W = mask.shape
    oh, ow = min(DOWN, H), min(DOWN, W)
    ys = (np.arange(oh) * (H / oh)).astype(int)
    xs = (np.arange(ow) * (W / ow)).astype(int)
    m = mask[np.ix_(ys, xs)]
    out = a[np.ix_(ys, xs)]
    return out, m


def solve_optimal_focal_shift(uv: np.ndarray, xyz: np.ndarray):
    """官方逐字：min |f·xy/(z+shift) − uv| 对 shift；focal 闭式回代。"""
    from scipy.optimize import least_squares
    uv = uv.reshape(-1, 2).astype(np.float64)
    xy = xyz[..., :2].reshape(-1, 2).astype(np.float64)
    z = xyz[..., 2].reshape(-1).astype(np.float64)

    def fn(shift):
        xy_proj = xy / (z + shift[0])[:, None]
        f = (xy_proj * uv).sum() / np.square(xy_proj).sum()
        return (f * xy_proj - uv).ravel()

    sol = least_squares(fn, x0=0, ftol=1e-3, method="lm")
    shift = float(sol.x.squeeze())
    xy_proj = xy / (z + shift)[:, None]
    focal = float((xy_proj * uv).sum() / np.square(xy_proj).sum())
    return focal, shift


def solve_optimal_shift(uv: np.ndarray, xyz: np.ndarray, focal: float) -> float:
    """官方逐字：focal 固定时只解 shift。"""
    from scipy.optimize import least_squares
    uv = uv.reshape(-1, 2).astype(np.float64)
    xy = xyz[..., :2].reshape(-1, 2).astype(np.float64)
    z = xyz[..., 2].reshape(-1).astype(np.float64)

    def fn(shift):
        xy_proj = xy / (z + shift[0])[:, None]
        return (focal * xy_proj - uv).ravel()

    sol = least_squares(fn, x0=0, ftol=1e-3, method="lm")
    return float(sol.x.squeeze())


def recover_focal_shift(points: np.ndarray, mask: np.ndarray,
                        focal: float | None = None):
    """官方 infer 的 focal/shift 恢复，返回 (focal, shift)。"""
    H, W = points.shape[:2]
    uv_full = normalized_view_plane_uv(W, H)
    uv, m = _masked_nearest_resize(uv_full, mask)
    pts, _ = _masked_nearest_resize(points, mask)
    sel = m
    uv_s = uv[sel]
    pts_s = pts[sel]
    if uv_s.shape[0] < 2:
        return (1.0, 0.0) if focal is None else (focal, 0.0)
    if focal is None:
        return solve_optimal_focal_shift(uv_s, pts_s)
    return float(focal), solve_optimal_shift(uv_s, pts_s, focal)


def focal_to_normalized_intrinsics(focal: float, W: int, H: int):
    """官方: fx = focal/2·√(1+ar²)/ar，fy = focal/2·√(1+ar²)，cx=cy=0.5。"""
    ar = W / H
    fx = focal / 2 * (1 + ar**2) ** 0.5 / ar
    fy = focal / 2 * (1 + ar**2) ** 0.5
    return float(fx), float(fy)


def preprocess(img_rgb_u8: np.ndarray, W: int, H: int) -> np.ndarray:
    """RGB u8 → (1,3,H,W) fp32 [0,1]；归一化(mean/std)在模型内部，勿重复。"""
    im = cv2.resize(img_rgb_u8, (W, H), interpolation=cv2.INTER_AREA)
    x = im.astype(np.float32) / 255.0
    return x.transpose(2, 0, 1)[None]


def forward(sess, image_input: np.ndarray, num_tokens: int):
    """raw forward（动态/静态会话自适应）。返回 points(H,W,3)、mask(H,W)、metric_scale。"""
    names = {i.name for i in sess.get_inputs()}
    feed = {"image": image_input}
    if "num_tokens" in names:
        feed["num_tokens"] = np.array(num_tokens, np.int64)
    out = sess.run(None, feed)
    points, mask, metric_scale = out[0][0], out[2][0], float(out[3][0])
    mask = (mask > 0.5) & np.isfinite(points).all(-1)
    return points.astype(np.float32), mask, metric_scale


def reconstruct(points: np.ndarray, mask: np.ndarray, metric_scale: float,
                focal: float | None = None, fx_fy: tuple | None = None):
    """官方 infer 的 force_projection 链路。

    focal=None 自行恢复；focal 给定只解 shift；fx_fy=(fx,fy) 直接给定内参
    （已知相机真源时跳过 focal 恢复，shift 仍按 solve_optimal_shift 解）。
    返回 dict：pts(H,W,3) 点图、depth(H,W)、focal、shift、fx、fy、valid。
    """
    H, W = points.shape[:2]
    if fx_fy is not None:
        fx, fy = fx_fy
        focal_ = 2.0 * fx * (W / H) / (1 + (W / H) ** 2) ** 0.5
        shift = _shift_with_intrinsics(points, mask, fx, fy)
    else:
        focal_, shift = recover_focal_shift(points, mask, focal=focal)
        fx, fy = focal_to_normalized_intrinsics(focal_, W, H)
    z = points[..., 2] + shift
    valid = mask & (z > 0)
    u = (np.arange(W, dtype=np.float32) + 0.5) / W
    v = (np.arange(H, dtype=np.float32) + 0.5) / H
    X = (u[None, :] - 0.5) * z / fx
    Y = (v[:, None] - 0.5) * z / fy
    pts = np.stack([X, Y, z], -1) * metric_scale
    return {"pts": pts.astype(np.float32),
            "depth": (z * metric_scale).astype(np.float32),
            "focal": focal_, "shift": shift, "fx": fx, "fy": fy, "valid": valid}


def _shift_with_intrinsics(points: np.ndarray, mask: np.ndarray,
                           fx: float, fy: float) -> float:
    """内参给定时等价于官方 solve_optimal_shift：focal = fx·2·span_x（口径换算）。"""
    H, W = points.shape[:2]
    ar = W / H
    focal = fx * 2 * ar / (1 + ar**2) ** 0.5
    uv_full = normalized_view_plane_uv(W, H)
    uv, m = _masked_nearest_resize(uv_full, mask)
    pts, _ = _masked_nearest_resize(points, mask)
    sel = m
    return solve_optimal_shift(uv[sel], pts[sel], focal)
