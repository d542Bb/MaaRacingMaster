# -*- coding: utf-8 -*-
"""地面基线候选 v2：全局平面拟合（替换逐行 q20 的表征级修正）。

动机（2026-09-26 维护者目测两帧 debug 渲染 + 数值取证）：
- 逐行 q20 赌「每行路面 ≥20%」——远带（y≈340-400）路面是窄条，基线锁到
  雾/天，路面相对基线 −46%（渲染里的蓝带）；
- 固定相对门 0.08/0.10 赌「台阶处处过门」——护栏相对台阶沿画面塌缩
  （+102%→+37%→+7%→+4%），块只剩远带 120 行碎片，内沿拟合采自透视最
  极端区段 → L=−3.41（路半宽 2.5）这种读数过完全部守卫。

候选模型：平坦路面 + 针孔相机 ⇒ 地面视差 d ≈ a·(y−y_h) + b·(x−640) + c。
一次全局稳健拟合三个参数：远带雾/天作为小占比被非对称残差剔除（治蓝带）；
基线不再逐行独立，块的内沿来自全带干净拟合（治碎片）；b 项=相机滚转，
tilt 问题从「13 档网格搜索」降为「拟合自带一项」。路面平不齐从隐含假设
变成可检验量（残差）。

测试面（维护者裁定）：只跑金标 45 帧（d336q4f16 缓存），与 rowq20/tilt
同卷同守卫对照；另渲染 3 帧不连续样本（原图/视差/两个基线的高度图）供目测。

用法（仓库根 .venv）：
    python tools/experiments/speedrush_vision/probe_plane_ground.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402
import probe_ground_ab as pab  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

CAL = pab.CAL
NPY_DIR = pd.OUT / "npy"
REV_DIR = pd.OUT / "plane_review"


def plane_field(m: np.ndarray, stride: int = 4, iters: int = 4) -> np.ndarray:
    """全局地面平面拟合 → 与 m 同带的 2D 基线场 g(y,x)。

    种子=逐行 q20 以下像素（近带可靠），随后非对称剔除（物体只在地面之上：
    上尾砍 0.5·MAD，下尾留 3·MAD 容噪声）。返回 float32 (DIAG_Y1−Y0, W)。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    h, w = band.shape
    yy = np.arange(h, dtype=np.float32)[:, None] + dg.Y0 - CAL.y_h
    xx = np.arange(w, dtype=np.float32)[None, :] - 640.0
    dy = np.broadcast_to(yy, (h, w))
    dx = np.broadcast_to(xx, (h, w))
    ok_col = np.r_[0:dg.EGO_COLS[0], dg.EGO_COLS[1]:w]
    q20 = np.quantile(band[:, ok_col], dg.Q_GROUND, axis=1)
    seed = (band <= q20[:, None] * 1.02)
    sub = (slice(None, None, stride), slice(None, None, stride))
    keep = seed[sub]
    A = np.stack([dy[sub][keep], dx[sub][keep], np.ones(keep.sum(), np.float32)], 1)
    d = band[sub][keep]
    coef, *_ = np.linalg.lstsq(A, d, rcond=None)
    for _ in range(iters):
        pred = (dy[sub] * coef[0] + dx[sub] * coef[1] + coef[2])
        r = band[sub] - pred
        mad = float(np.median(np.abs(r[keep]))) or 1e-6
        keep = (r < 0.5 * mad) & (r > -3.0 * mad)
        A = np.stack([dy[sub][keep], dx[sub][keep],
                      np.ones(int(keep.sum()), np.float32)], 1)
        coef, *_ = np.linalg.lstsq(A, band[sub][keep], rcond=None)
    return (dy * coef[0] + dx * coef[1] + coef[2]).astype(np.float32)


def _load_frames() -> list[tuple[dict, np.ndarray]]:
    labels = list(csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8")))
    frames = []
    for r in labels:
        p = NPY_DIR / f"{pcq.frame_key(Path(r['path']))}__d336q4f16.npy"
        if p.exists():
            frames.append((r, np.load(p).astype(np.float32)))
    return frames


def _gold_row(frames, tag, fields, gl, gr, bad_lane=1.0):
    """fields: 预计算的 {帧序: 基线场}（tilt/plane 不重复算）。"""
    devs = {"L": [], "R": []}
    both = 0
    for i, (r, m) in enumerate(frames):
        g = fields[i]
        blocks = pab._blocks_with_field(m, gl, gr, g)
        got = {}
        for side in ("L", "R"):
            f = pab._select(blocks, m, side, gl, gr)
            if f is None:
                continue
            gx = pab._gold_x_at(r, side, f["y_ref"])
            if gx is None:
                continue
            got[side] = f
            devs[side].append(f["lane"] - dg.x_lane_of(
                int(round(gx)), int(round(f["y_ref"])), CAL))
        both += len(got) == 2
    cells = [f"{tag:>10} {gl:.2f}/{gr:.2f}"]
    for side in ("L", "R"):
        d = devs[side]
        cells += [f"{side} n={len(d):>2}",
                  f"dev={np.median(d):+.2f}" if d else "dev= n/a",
                  f"p90={np.percentile(np.abs(d), 90):.2f}" if d else "p90=n/a",
                  f"坏={sum(1 for v in d if abs(v) > bad_lane)}"]
    cells += [f"双={both}"]
    return " ".join(cells), devs


def render(frames, picks: list[int]) -> None:
    """3 帧四联图：原图 / 视差 JET / 高度图(rowq20) / 高度图(plane)。"""
    REV_DIR.mkdir(parents=True, exist_ok=True)
    for i in picks:
        r, m = frames[i]
        src = cv2.imread(r["path"])
        band = m[dg.Y0:dg.DIAG_Y1]
        lo, hi = np.percentile(np.log(np.clip(band, 1e-3, None)), [2, 98])
        disp = cv2.applyColorMap(
            (np.clip((np.log(np.clip(band, 1e-3, None)) - lo) / max(hi - lo, 1e-6), 0, 1)
             * 255).astype(np.uint8), cv2.COLORMAP_JET)

        def relmap(g):
            rel = (band - g) / np.maximum(g, 1e-6)
            v = np.clip((rel + 0.10) / 0.20, 0, 1)
            return cv2.applyColorMap((v * 255).astype(np.uint8), cv2.COLORMAP_JET)
        g_row = pab._row_q20_field(m)
        g_pl = plane_field(m)
        # 两张高度图同一 ±10% 色标（与产码 render_depth_debug 同口径）
        panel = np.vstack([src, disp, relmap(g_row), relmap(g_pl)])
        cv2.putText(panel, f"{Path(r['path']).parent.parent.parent.name[-15:]}"
                    f"/{Path(r['path']).name} stratum={r['stratum']}",
                    (8, src.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (255, 255, 255), 2)
        out = REV_DIR / f"plane_vs_q20_{Path(r['path']).stem}.jpg"
        cv2.imwrite(str(out), panel, [cv2.IMWRITE_JPEG_QUALITY, 88])
        print("渲染:", out)


def main() -> None:
    frames = _load_frames()
    print(f"金标缓存 {len(frames)} 帧")
    # 平面拟合耗时 sanity
    import time
    t0 = time.perf_counter()
    _ = plane_field(frames[0][1])
    print(f"plane_field 单帧 {1000*(time.perf_counter()-t0):.0f}ms")

    print("\n── 同卷对照（守卫/选择环=产码，dev=读数−金标同 y_ref lane）──")
    fields = {tag: [field(m) for _, m in frames]
              for tag, field in (("rowq20", pab._row_q20_field),
                                 ("tilt", pab._tilt_field),
                                 ("plane", plane_field))}
    for gl, gr in ((0.08, 0.10), (0.06, 0.08), (0.10, 0.12)):
        for tag in ("rowq20", "tilt", "plane"):
            line, _ = _gold_row(frames, tag, fields[tag], gl, gr)
            print(line)
        print()

    # 拟合质量自检：平面基线在近带的残差分布（平不齐=路拱/弯道，如实报）
    res = []
    for _, m in frames:
        band = m[dg.Y0:dg.DIAG_Y1]
        g = plane_field(m)
        r = (band - g) / np.maximum(g, 1e-6)
        near = r[130:230]  # y≈470-715 近带
        res.append(float(np.median(np.abs(near))))
    print(f"近带路面残差 |rel| 中位：p50={np.percentile(res, 50):.3f} "
          f"p90={np.percentile(res, 90):.3f}")

    # 3 帧不连续目测样本（首/中/尾，跨不同层）
    picks = [2, len(frames) // 2, len(frames) - 3]
    render(frames, picks)


if __name__ == "__main__":
    main()
