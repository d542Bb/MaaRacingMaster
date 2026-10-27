# -*- coding: utf-8 -*-
"""标线掩码逐帧位移探针（工作台，不入库）。

**要回答什么**：控制器唯一的横速观测是深度 β（噪声约 0.15 道/0.25s）。
线横位是同一路标的另一种读数，但两帧差分噪声同级。本探针直接在生产分割
掩码上量"同一行上标线横坐标的逐帧变化"——静态偏置相消——回答：

  1. 增益：与单帧拟合差分相比，逐帧位移的噪声低多少？
  2. 一致性：同一帧对里左右两条边界各自估的横向位移是否一致（无真值检验）；
  3. 透视：逐行位移对行号线性回归的斜率/截距，分别对应横向位移与航向。

**几何**（与 lane_obs 同源）::

    x_px = VPX + f·tanψ + X0·(y−Y_H)/cam_h

同一行（地面深度相同）上所有标线的位移相同；横向位移改变斜率
（c1 = X0/cam_h），航向改变截距（c0 = f·tanψ）。所以"透视修正"= 对行号
做一次线性回归，而不是取全图一个平均位移。

**为什么不是常规光流**：虚线沿自身方向的流动受前向速度影响且有周期混叠；
这里只用垂直向位移，虚线相位只决定该行有没有线，不决定它的横坐标。

**为什么无需道号**：同一行上左右所有候选线位移相同，取最近的线即可；换道时
"最近线"换身份也不影响位移量。唯一的身份风险是最近线缺失时匹配到隔壁
车道（间距≈一个道宽），用"允许位移 ≤ 0.45 道"的行域门挡掉。

**有效性门**（跳跃镜头/换路段/腾空时地面平面假设失效，掩码整体错位）：
  - 回归 rms > 3px 或 |c0| > 30px（>2° 航向跳变）或样本 < 20 行 → 弃权。

**缩放项（已试，不采用）**：游戏镜头是否随速度呼吸未定。加第三项
``c2·(x−VPX)`` 能把 rms 从 16.6px 压到 1.1px，但在只有一侧线或行数少时
三个回归量近似共线——c2 会飙到 ±100%/0.1s（物理不可能），把横向位移也
一起吃掉。故本探针只用两项；左右一致性与单帧拟合对照是唯一可信的精度尺。

用法（仓库根，.venv）::

    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_lane_flow.py \
        --session 20261027_111258_p2 --start 100 --n 120

权重与生产同源（不入库）：默认读 AppData 下 data/speedrush/models/。
"""
from __future__ import annotations

import argparse
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from maaracing_master.plugins.speedrush.lane_obs import (  # noqa: E402
    DEFAULT_CAM_H, LANE_W, NEAR_Y, VPX, Y_H,
    _build_tracks, _fit, _letterbox, _prob_from_out, _row_segments,
    _side_from_x, _near_x)

DEFAULT_DEBUG = os.path.join(os.environ.get(
    "MAA_DEBUG", r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug"),
    "speedrush")
DEFAULT_WEIGHTS = os.path.join(os.environ.get(
    "MAA_DATA", r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data"),
    "speedrush", "models", "plus_small_384x640.onnx")

VPX_HALF = VPX / 2.0
LR_GATE_LANE = 0.25           # 左右位移差门
RMS_GATE_PX = 3.0
C0_GATE_PX = 30.0             # 0.1s 内 >2° 航向跳变视为非刚体
MIN_ROWS = 20


def load_session(debug_dir: str, session: str, start: int, n: int):
    raw = os.path.join(debug_dir, session, "raw")
    names = sorted(x for x in os.listdir(raw) if x.endswith("_raw.jpg"))
    out = []
    for name in names[start:start + n]:
        img = cv2.imread(os.path.join(raw, name))
        if img is not None:
            out.append((name, img))
    return out


def lane_prob(sess, bgr: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    inp = sess.get_inputs()[0].name
    return _prob_from_out(sess.run(None, {inp: _letterbox(rgb)}))


def _nearest(segs, side: int):
    cand = [s for s in segs if (s["xc"] < VPX_HALF) == (side < 0)]
    if not cand:
        return None
    return min(cand, key=lambda s: abs(s["xc"] - VPX_HALF))


def row_shifts(prev_rows, cur_rows):
    """逐行左右最近线的横坐标变化 [(y720, dx720, x720, p, side)]。

    掩码是 640×360，几何常数是 720p（lane_obs 同源），故 dx/x 都乘 2。
    """
    pm = {yc: segs for yc, segs in prev_rows}
    out = []
    for yc, segs in cur_rows:
        psegs = pm.get(yc)
        if not psegs:
            continue
        allow = 0.45 * LANE_W * (2.0 * yc - Y_H) / DEFAULT_CAM_H
        for side in (-1, 1):
            a, b = _nearest(psegs, side), _nearest(segs, side)
            if a is None or b is None:
                continue
            dx = 2.0 * (b["xc"] - a["xc"])
            if abs(dx) > allow:
                continue
            out.append((2.0 * yc, dx, 2.0 * b["xc"],
                        min(a["p"], b["p"]), side))
    return out


def fit_shift(samples):
    """Δx = c0 + c1·(y−Y_H)（720p），按 p 加权；两会合剔除 3px 外残差。"""
    if len(samples) < 8:
        return None
    ys = np.array([s[0] for s in samples], dtype=np.float64)
    dx = np.array([s[1] for s in samples], dtype=np.float64)
    wt = np.maximum(np.array([s[3] for s in samples], dtype=np.float64), 0.05)
    for _ in range(2):
        A = np.vstack([np.ones_like(ys), ys - Y_H]).T
        coef = np.linalg.lstsq(A * wt[:, None], dx * wt, rcond=None)[0]
        keep = np.abs(dx - A @ coef) <= 3.0
        if keep.sum() < 8 or keep.all():
            break
        ys, dx, wt = ys[keep], dx[keep], wt[keep]
    A = np.vstack([np.ones_like(ys), ys - Y_H]).T
    c0, c1 = np.linalg.lstsq(A * wt[:, None], dx * wt, rcond=None)[0]
    rms = float(np.sqrt(np.average((dx - A @ [c0, c1]) ** 2, weights=wt)))
    return {"c0": float(c0), "c1": float(c1), "rms": rms, "n": int(len(ys)),
            "span": float(ys.max() - ys.min())}


def fit_L(prob, cam_h: float):
    """单帧生产拟合：每侧近场 L（道号无关，侧别按近场点判）。"""
    out = {}
    for pts in _build_tracks(_row_segments(prob)):
        f = _fit(pts, cam_h, *NEAR_Y)
        if f is None:
            continue
        side = _side_from_x(_near_x(pts))
        if side is None:
            continue
        prev = out.get(side)
        if prev is None or f["n"] > prev["n"]:
            out[side] = f
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--debug", default=DEFAULT_DEBUG)
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--hz", type=float, default=10.0)
    args = ap.parse_args()

    import onnxruntime as ort
    sess = ort.InferenceSession(args.weights, providers=["CPUExecutionProvider"])
    frames = load_session(args.debug, args.session, args.start, args.n)
    print("session %s frames=%d  weights=%s" % (
        args.session, len(frames), os.path.basename(args.weights)))

    cam_h = DEFAULT_CAM_H
    dt = 1.0 / args.hz
    pairs = []
    prev = None
    for name, bgr in frames:
        prob = lane_prob(sess, bgr)
        rows = _row_segments(prob)
        fl = fit_L(prob, cam_h)
        if prev is not None:
            samples = row_shifts(prev["rows"], rows)
            allf = fit_shift(samples)
            lf = fit_shift([s for s in samples if s[4] < 0])
            rf = fit_shift([s for s in samples if s[4] > 0])
            rec = {"name": name, "flow": None, "fit": None, "valid": False}
            if allf is not None:
                rec["flow"] = allf["c1"] * cam_h    # 米（横向位移，正=?
                rec["c0"] = allf["c0"]
                rec["rms"] = allf["rms"]
                rec["n"] = allf["n"]
            if lf is not None and rf is not None:
                rec["lr"] = abs(lf["c1"] * cam_h - rf["c1"] * cam_h) / LANE_W
            # 单帧拟合差分
            dl = {}
            for side in (-1, 1):
                a, b = prev["fit"].get(side), fl.get(side)
                if a is not None and b is not None:
                    dl[side] = (b["L"] - a["L"]) * LANE_W
            if dl:
                rec["fit"] = float(np.mean(list(dl.values())))
            rec["valid"] = bool(
                rec["flow"] is not None
                and rec["n"] >= MIN_ROWS
                and rec["rms"] <= RMS_GATE_PX
                and abs(rec["c0"]) <= C0_GATE_PX
                and rec.get("lr") is not None
                and rec["lr"] <= LR_GATE_LANE)
            pairs.append(rec)
        prev = {"rows": rows, "fit": fl}

    valid = [p for p in pairs if p["valid"]]
    print("\n帧对=%d  有效=%d (%.0f%%)  弃权: 左右不一致=%d rms/航向门=%d 拟合缺失=%d" % (
        len(pairs), len(valid), 100 * len(valid) / max(len(pairs), 1),
        sum(1 for p in pairs if p.get("lr") is not None and p["lr"] > LR_GATE_LANE),
        sum(1 for p in pairs if p["flow"] is not None and p.get("lr") is not None
            and p["lr"] <= LR_GATE_LANE and (p["rms"] > RMS_GATE_PX
                                             or abs(p["c0"]) > C0_GATE_PX)
            and p["n"] >= MIN_ROWS),
        sum(1 for p in pairs if p["flow"] is None)))

    fv = np.array([p["flow"] for p in valid])
    print("  横向位移 p50/|p90|/max: %.3f / %.3f / %.3f m  (%.2f / %.2f 道/s)" % (
        np.median(fv), np.percentile(np.abs(fv), 90), np.abs(fv).max(),
        np.median(fv) / (LANE_W * dt),
        np.percentile(np.abs(fv), 90) / (LANE_W * dt)))
    lr = [p["lr"] for p in valid]
    print("  左右独立位移差 p50/p90: %.3f / %.3f 道" % (
        np.median(lr), np.percentile(lr, 90)))
    both = [p for p in valid if p["fit"] is not None]
    if len(both) >= 20:
        a = np.array([p["fit"] for p in both])
        b = np.array([p["flow"] for p in both])
        r = float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 1e-9 else 0.0
        print("  与单帧拟合差分 corr=%.2f  MAE=%.3f 道  (n=%d)" % (
            r, np.mean(np.abs(a - b)) / LANE_W, len(both)))

    # 高频噪声：二阶差分（去匀速趋势），单位道/s。逐对序列中间不许有弃权空洞，
    # 故只取连续有效段。
    def hf(seq):
        seq = np.asarray(seq, dtype=np.float64) / (LANE_W * dt)
        if len(seq) < 5:
            return float("nan")
        return float(np.sqrt(np.mean((seq[2:] - 2 * seq[1:-1] + seq[:-2]) ** 2)))

    def runs(xs):
        out, cur = [], []
        for p in xs:
            if p["valid"] and p["flow"] is not None and p["fit"] is not None:
                cur.append(p)
            else:
                if len(cur) >= 5:
                    out.append(cur)
                cur = []
        if len(cur) >= 5:
            out.append(cur)
        return out

    rr = runs(pairs)
    hf_f = [hf([p["flow"] for p in r]) for r in rr]
    hf_l = [hf([p["fit"] for p in r]) for r in rr]
    hf_f = [x for x in hf_f if np.isfinite(x)]
    hf_l = [x for x in hf_l if np.isfinite(x)]
    if hf_f and hf_l:
        print("  二阶差分噪声(连续段 %d): flow p50=%.2f  fit p50=%.2f 道/s  比值=%.2f" % (
            len(rr), np.median(hf_f), np.median(hf_l),
            np.median(hf_f) / max(np.median(hf_l), 1e-9)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
