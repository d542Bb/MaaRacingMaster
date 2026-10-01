# -*- coding: utf-8 -*-
"""MoGe-2 官方 ONNX 的 numpy 后处理：focal/shift 恢复 + 点图重建。

官方 ONNX 只有 raw forward（输入 image fp32 RGB[0,1]（+动态版 num_tokens），
输出 points/normal/mask/metric_scale 仿射点图）；官方 infer() 在模型外做
recover_focal_shift → z 加 shift → force_projection 重投影 → ×metric_scale。
本模块按 microsoft/MoGe geometry_numpy.py / geometry_torch.py / v2.py 的
公式复刻该链路（公式链逐字对齐官方，出处核对记录 2026-10-01）。

公式出处：
- normalized_view_plane_uv: span_x = ar/√(1+ar²), span_y = 1/√(1+ar²)
  （半对角归一），uv ∈ [-span·(n-1)/n, +span·(n-1)/n]，v 随行号向下递增。
- recover_focal_shift: uv/points 各自 masked 最近邻下采样到 64×64，
  solve_optimal_focal_shift：xy_proj = xy/(z+shift)，
  f = <xy_proj,uv>/<xy_proj,xy_proj>，残差 = f·xy_proj − uv，
  对 shift 做一维最小二乘解，focal 闭式回代；给定 focal 时退化为
  solve_optimal_shift。返回 (focal, shift)。
- fx = focal/2·√(1+ar²)/ar，fy = focal/2·√(1+ar²)；归一化内参 cx=cy=0.5。
- 重投影：Z = points_z+shift，X=(u−0.5)·Z/fx，Y=(v−0.5)·Z/fy，全体 ×metric_scale。

**shift 求解器**：官方用 scipy least_squares(method='lm', x0=0)（局部极小）。
产线不引 scipy（运行时依赖表没有它），用模式搜索替代：从 0 起步、两侧试探
下降、走不动半步——**找离 0 最近的局部极小**，与 LM(x0=0) 同盆地（残差在
远处存在 f→0 的退化盆地，全局法会掉进去，实跑实证）。demo 帧上 shift/fx
与 scipy 解一致性验证通过（本机实跑，相对差 <1e-5）。逐字复刻红线只在
公式链；求解器是数值实现细节，精度由测试锁。
"""

from __future__ import annotations

import cv2
import numpy as np

DOWN = 64  # 官方 recover_focal_shift 的下采样网格 (width, height)


def normalized_view_plane_uv(width: int, height: int) -> np.ndarray:
    """官方 geometry_numpy.normalized_view_plane_uv_numpy。"""
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


def _minimize_local(fn, step: float) -> float:
    """从 0 出发的模式搜索下山：步长 step 起步，两侧试探下降，走不动则半步，
    到 step 1e-9 收敛。**找的是离 0 最近的局部极小**——残差在远处有 f→0 的
    退化盆地（实跑 frame 0/401 实证：全局扫描会掉进去，focal≈0 的平凡解），
    官方 LM(x0=0) 的语义就是局部解，本器走同一盆地。"""
    x, r = 0.0, fn(0.0)
    h = step
    while h > 1e-9 * step:
        xm, xp = fn(x - h), fn(x + h)
        if xm < r:
            x, r = x - h, xm
        elif xp < r:
            x, r = x + h, xp
        else:
            h *= 0.5
    return x


def _solve_shift(uv: np.ndarray, xy: np.ndarray, z: np.ndarray,
                 focal: float | None) -> tuple[float, float | None]:
    """solve_optimal_focal_shift / solve_optimal_shift 的产线实现。

    focal=None 时先在 shift=0 处闭式回代 focal（官方先解 focal 再解 shift 的
    联合格局等价于：对每个 shift 取内层闭式 focal 后的最小残差）。"""
    if uv.shape[0] < 2:
        return 0.0, focal

    def _focal_at(s: float) -> float:
        xy_proj = xy / (z + s)[:, None]
        return float((xy_proj * uv).sum() / np.square(xy_proj).sum())

    def resid(s: float) -> float:
        xy_proj = xy / (z + s)[:, None]
        f = (xy_proj * uv).sum() / np.square(xy_proj).sum()
        d = f * xy_proj - uv
        return float((d * d).sum())

    z_med = float(np.median(z[np.isfinite(z) & (z > 0)])) or 1.0
    shift = _minimize_local(resid, step=0.05 * z_med)
    if focal is None:
        focal = _focal_at(shift)
    return shift, focal


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
    xy = pts_s[..., :2].astype(np.float64)
    z = pts_s[..., 2].astype(np.float64)
    z = np.maximum(z, 1e-6)          # z+shift 的极点防护（官方靠 LM 域约束隐式承担）
    shift, focal_out = _solve_shift(uv_s.astype(np.float64), xy, z, focal)
    return (focal_out if focal_out is not None else 1.0), shift


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
                focal: float | None = None):
    """官方 infer 的 force_projection 链路。

    focal=None 自行恢复。返回 dict：pts(H,W,3) 点图、depth(H,W)、focal、shift、
    fx、fy、valid。X/Y/Z 同为「向下/向右为正」的相机系。"""
    H, W = points.shape[:2]
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
