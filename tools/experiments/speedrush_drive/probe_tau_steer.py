"""τ（转向指令→画面横向响应延迟）实测：从到场录制局读数。

**要回答的问题**（C 类）：Pure Pursuit 的 lookahead 需求 = 指令到画面响应的延迟 τ
加惯性时间常数（control-route §三）。维护者 2026-09-21 到场局为连续游戏打舵
（非干净阶跃序列），但每次打舵的**上升沿**仍是接近理想的阶跃输入（手柄推满
<1 个 200Hz 采样周期），且变道动作大、横向流信号强，可用。

**方法**：
1. pads.jsonl 提取 RISE 事件（|lx| 从 <3000 跳到 >20000，持舵 ≥0.25s 才计入，
   剔除抖动型短舵）；
2. 对每个事件，以指令时刻前最后一帧为参考帧，用 cv2.phaseCorrelate 对 ROI
   （近场带 y∈[430,700]，横向流最强）逐帧累积水平位移 dx(t)；
3. 基线漂移扣除：指令前 0.2s 窗内 dx 的线性趋势作为路面/相机基线流，
   残差 |dx_res| 首次越过 THRESH(4px) 的时刻即响应起点；
4. τ = 响应起点 − 指令时刻；帧时钟 ~15fps（67ms 间隔），τ 读数精度 ±1 帧，
   工程上够用（lookahead 是秒级量）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_tau_steer.py <p1场目录> [<p2场目录> ...]

输出：逐事件 (方向, 指令时刻, τ, 峰值 dx, 上升时间) + 汇总统计。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROI = (0, 430, 1280, 700)          # 近场横向带
THRESH_PX = 4.0                    # 响应判定阈值（残差）
HOLD_MIN_S = 0.25                  # 最短持舵
RISE_HI, RISE_LO = 20000, 3000
SCALE = 0.5                        # phaseCorrelate 前缩放


def load_pads(sess: Path):
    return [json.loads(l) for l in open(sess / "pads.jsonl", encoding="utf-8")]


def load_frames(sess: Path):
    recs = [json.loads(l) for l in open(sess / "frames.jsonl", encoding="utf-8")]
    return recs


def rise_events(pads):
    t0 = pads[0]["ts_ns"]
    events = []
    state, t_rise, dirn = 0, 0.0, ""
    for x in pads:
        v, t = x["lx"], (x["ts_ns"] - t0) / 1e9
        if state == 0 and abs(v) > RISE_HI:
            state, t_rise, dirn = 1, t, ("L" if v < 0 else "R")
        elif state == 1 and abs(v) < RISE_LO:
            if t - t_rise >= HOLD_MIN_S:
                events.append((t_rise, dirn, t - t_rise))
            state = 0
    return events


def gray_frame(sess: Path, rec):
    img = cv2.imread(str(sess / "frames" / rec["file"]))
    g = cv2.cvtColor(img[ROI[1]:ROI[3], ROI[0]:ROI[2]], cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, None, fx=SCALE, fy=SCALE)


def _flow_series(sess: Path, frames, i_from: int, i_to: int):
    """frames[i_from..i_to] 相邻帧水平位移序列（原始 px，未累积）。"""
    out = []
    cur = gray_frame(sess, frames[i_from])
    for f in frames[i_from + 1: i_to + 1]:
        nxt = gray_frame(sess, f)
        (dx, _dy), resp = cv2.phaseCorrelate(cur.astype(np.float32), nxt.astype(np.float32))
        cur = nxt
        out.append((dx / SCALE, resp))
    return out


def measure(sess: Path, t_cmd: float, pads, frames) -> dict | None:
    """τ = 画面横向流偏离指令前基线流速 4px 的时刻 − 指令时刻。

    参考帧 = 指令前最后一帧；基线流速由指令前 ~0.5s 的相邻帧位移均值给出
    （扣除路面/相机自身流动）。响应判定后继续看 1.5s 取峰值与 63% 上升时刻。
    """
    t0 = pads[0]["ts_ns"]
    ft = [(f["ts_ns"] - t0) / 1e9 for f in frames]
    pre = [i for i, t in enumerate(ft) if t <= t_cmd]
    if len(pre) < 10:
        return None
    ref = pre[-1]
    base = max(0, ref - 8)
    pre_flow = _flow_series(sess, frames, base, ref)
    if not pre_flow:
        return None
    v_base = float(np.median([d for d, r in pre_flow if r > 0.2])) if pre_flow else 0.0
    post = _flow_series(sess, frames, ref, min(len(frames) - 1, ref + 23))
    if len(post) < 6:
        return None
    ts = np.array([ft[ref + 1 + i] - ft[ref] for i in range(len(post))])
    resid = np.cumsum([d for d, _r in post]) - v_base * ts
    over = np.where(np.abs(resid) > THRESH_PX)[0]
    if len(over) == 0:
        return None
    i0 = int(over[0])
    peak_i = int(np.argmax(np.abs(resid)))
    level = abs(resid[peak_i]) * 0.63
    j = int(np.where(np.abs(resid) >= level)[0][0])
    # 指令落在参考帧之后的 (0, 帧间隔] 内：τ 以指令时刻为原点，需扣除该偏置
    offset = t_cmd - ft[ref]
    return {"tau": round(float(ts[i0] - offset), 3),
            "peak_dx": round(float(resid[peak_i]), 1),
            "rise_s": round(float(ts[j] - offset), 3)}


def main() -> None:
    all_rows = []
    for arg in sys.argv[1:]:
        sess = Path(arg)
        pads, frames = load_pads(sess), load_frames(sess)
        for t_cmd, dirn, hold in rise_events(pads):
            r = measure(sess, t_cmd, pads, frames)
            if r is None:
                continue
            r.update(sess=sess.name, t=t_cmd, dir=dirn, hold=hold)
            all_rows.append(r)
    print(f"{'场':>20s} {'t(s)':>6s} {'向':>2s} {'τ(s)':>6s} {'峰dx':>6s} {'63%上升':>7s}")
    for r in all_rows:
        print(f"{r['sess']:>20s} {r['t']:>6.2f} {r['dir']:>2s} "
              f"{r['tau']:>6.3f} {r['peak_dx']:>6.1f} {r['rise_s']:>7.3f}")
    if not all_rows:
        print("无有效事件")
        return
    taus = np.array([r["tau"] for r in all_rows])
    print(f"\n事件数 {len(all_rows)} | τ 中位 {np.median(taus):.3f}s "
          f"p25 {np.percentile(taus,25):.3f} p75 {np.percentile(taus,75):.3f} "
          f"min {taus.min():.3f} max {taus.max():.3f}")
    for d in ("L", "R"):
        td = np.array([r["tau"] for r in all_rows if r["dir"] == d])
        if len(td):
            print(f"  方向 {d}: n={len(td)} τ中位 {np.median(td):.3f}s")


if __name__ == "__main__":
    main()
