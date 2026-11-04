# -*- coding: utf-8 -*-
"""MoGe 输入/输出分辨率的降档 A/B：forward 收益、upsize 浪费、读数损失。

背景（2026-10-17 实测）：模型输入被 free dimension override 折叠成 1×720×1280，
输出点图 336×598（0.467×）。三条降档路径，机制完全不同：

- **降输入分辨率**：只省 preprocess resize 与 patch embedding，forward 主体是
  固定 token 网格，降不动（实测 −6ms，不是 −20ms）；
- **降 num_tokens**：模型静态折叠，该输入被忽略（768/512 与 1032 输出逐位相同）；
- **不 upsize**：`infer_points` 把 336×598 升到 1280×720 花 ~16ms，而几何块
  随后又抽到 360×640（`GEO_DS=2`）——先放大 4× 再抽回 1/4，是重复劳动。
  原生直吃省 16ms 且无插值损失，但读数像素列变成原生列（598 宽），
  诊断面需换算回全幅。

**预登记判据（先写后跑）**：相对现状臂，
1. observe 总时延收益 ≥ 15ms；
2. `sides` 同判率 ≥ 90%；
3. 边缘车道差 P50 ≤ 0.10 lane 且 P90 ≤ 0.30 lane；
4. `cam_h`（平面 c 项）相对差 P50 ≤ 2%。
全满足 → 「损失很小，可考虑」；否则判「不可忽略」。

用法：``.venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_resolution_ab.py``
环境变量 ``SR_FRAMES`` 换局名、``SR_N`` 换帧数（缺省 12）。"""
from __future__ import annotations

import glob
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import maaracing_master.plugins.speedrush.depth_geo as D    # noqa: E402
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE, moge_post  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

FULL_W, FULL_H = 1280, 720
RUN = os.environ.get("SR_FRAMES", "20261017_132405_p1")
N = int(os.environ.get("SR_N", "12"))

ARMS = [
    ("现状(upsize+DS2)", 1280, 720, 1032, False, 2),
    ("降输入960(upsize+DS2)", 960, 540, 1032, False, 2),
    ("原生直吃(不upsize,DS1)", 1280, 720, 1032, True, 1),
]


def _build(w: int, h: int) -> ort.InferenceSession:
    """同 load_session 的折叠口径，尺寸参数化。"""
    avail = set(ort.get_available_providers())
    prov = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider",
                        "CPUExecutionProvider") if p in avail]
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"), (1, h, w)):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=so,
                                providers=prov)


def _frames() -> list[np.ndarray]:
    base = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
    d = (base / RUN).resolve()
    if base.resolve() not in d.parents:
        raise ValueError(f"路径越界：{RUN}")
    paths = sorted(glob.glob(str(d / "depth_debug" / "*_frame.jpg")))[:N]
    return [cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2RGB) for p in paths]


def _arm(w, h, tokens, native, ds, frames, cal) -> list[dict]:
    sess = _build(w, h)
    D.GEO_DS = ds
    rows = []
    for rgb in frames:
        blob = moge_post.preprocess(rgb, w, h)
        t = time.perf_counter()
        pts0, mask, ms = moge_post.forward(sess, blob, tokens)
        fwd = (time.perf_counter() - t) * 1e3
        t = time.perf_counter()
        res = moge_post.reconstruct(pts0, mask, ms)
        rec = (time.perf_counter() - t) * 1e3
        pts = res["pts"]
        oh, ow = pts.shape[:2]
        t = time.perf_counter()
        if native:
            img = pts.copy()
            img[~res["valid"]] = np.nan
            fx = float(res["fx"]) * ow
            fy = float(res["fy"]) * oh
        else:
            img = np.stack([cv2.resize(pts[..., k], (FULL_W, FULL_H),
                                       interpolation=cv2.INTER_LINEAR)
                            for k in range(3)], -1)
            vf = cv2.resize(res["valid"].astype(np.float32), (FULL_W, FULL_H),
                            interpolation=cv2.INTER_NEAREST).astype(bool)
            img[~vf] = np.nan
            fx = float(res["fx"]) * FULL_W
            fy = float(res["fy"]) * FULL_H
        up = (time.perf_counter() - t) * 1e3
        t = time.perf_counter()
        rd = D.reading_from_points(img, fx, cal, fy=fy)
        geo = (time.perf_counter() - t) * 1e3
        rows.append({"fwd": fwd, "rec": rec, "up": up, "geo": geo,
                     "oh": oh, "ow": ow, "rd": rd,
                     "resid": (res["solver"] or {}).get("resid")})
    del sess
    return rows


def _p(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def main() -> None:
    frames = _frames()
    cal = load_calib()
    print(f"局 {RUN}  帧 {len(frames)}")
    out = {}
    for label, w, h, tk, native, ds in ARMS:
        rows = _arm(w, h, tk, native, ds, frames, cal)
        out[label] = rows
        tot = [r["fwd"] + r["rec"] + r["up"] + r["geo"] for r in rows]
        print(f"\n{label}  输入 {w}x{h} tokens={tk} → 点图 {rows[0]['oh']}x{rows[0]['ow']}")
        print(f"  forward={_p([r['fwd'] for r in rows], .5):5.1f} "
              f"reconstruct={_p([r['rec'] for r in rows], .5):4.1f} "
              f"upsize={_p([r['up'] for r in rows], .5):4.1f} "
              f"geo={_p([r['geo'] for r in rows], .5):5.1f} "
              f"| 合计={_p(tot, .5):5.1f} ms")
        print(f"  sides={[r['rd'].sides for r in rows]}")
        print(f"  cam_h P50={_p([(r['rd'].coef or (0, 0, 0))[2] for r in rows], .5):.3f} "
              f"resid P50={_p([r['resid'] for r in rows], .5):.5f} "
              f"fx P50={_p([r['rd'].fx for r in rows], .5):.1f} "
              f"edge_pts P50={_p([len(r['rd'].edge_pts) for r in rows], .5):.0f}")

    base = out[ARMS[0][0]]
    base_tot = _p([r["fwd"] + r["rec"] + r["up"] + r["geo"] for r in base], .5)
    print("\n=== 相对现状的收益与损失 ===")
    for label, *_ in ARMS[1:]:
        rows = out[label]
        tot = _p([r["fwd"] + r["rec"] + r["up"] + r["geo"] for r in rows], .5)
        same = sum(1 for a, b in zip(base, rows) if a["rd"].sides == b["rd"].sides)
        dl = [abs(a["rd"].left_edge_lane - b["rd"].left_edge_lane)
              for a, b in zip(base, rows)
              if a["rd"].left_edge_lane is not None
              and b["rd"].left_edge_lane is not None]
        dr = [abs(a["rd"].right_edge_lane - b["rd"].right_edge_lane)
              for a, b in zip(base, rows)
              if a["rd"].right_edge_lane is not None
              and b["rd"].right_edge_lane is not None]
        edge = dl + dr
        ch_pairs = [(abs((a["rd"].coef or (0, 0, 0))[2]
                         - (b["rd"].coef or (0, 0, 0))[2]), a["rd"].coef[2])
                    for a, b in zip(base, rows) if a["rd"].coef and b["rd"].coef]
        chr_ = [d / c for d, c in ch_pairs]
        gain = base_tot - tot
        print(f"\n{label}: 合计收益 {gain:+.1f}ms ({100 * gain / base_tot:+.0f}%) "
              f"| sides 同判 {same}/{len(base)} "
              f"| 边缘差 P50={_p(edge, .5):.3f} P90={_p(edge, .9):.3f} lane "
              f"| cam_h 相对差 P50={100 * _p(chr_, .5):.1f}%")
        ok = (gain >= 15.0 and same / len(base) >= 0.90
              and _p(edge, .5) <= 0.10 and _p(edge, .9) <= 0.30
              and 100 * _p(chr_, .5) <= 2.0)
        print(f"  预登记四条件 → "
              f"{'全部满足（损失很小，可考虑）' if ok else '未全满足（损失不可忽略）'}")


if __name__ == "__main__":
    main()
