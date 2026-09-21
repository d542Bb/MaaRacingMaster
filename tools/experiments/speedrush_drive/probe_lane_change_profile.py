"""§七.1 变道时长剖面（v2 方法：路缘 x_lane 平移法）：从到场轮"左右变道"局
测 `lane_change_duration(Δx)` 的形状，供 decision.json timing 段定档。

**方法沿革**（为什么不 phaseCorrelate）：v1 用近场带横向流累积，读数位移只有
0.15–0.17 车道、n=4、拟合负斜率——**方法证伪**：强纵向滚动的路面上，相邻帧
水平相位相关测不了百像素级大位移（饱和/失置信）。换假设（诊断三原则③）：
用我们自己的尺子量自己——追车相机把自车钉死画面中央，**自车变道 1 车道 =
世界路缘在图像里整体横移 1 车道**；路缘是黄色强线（旧栈口径），按行取黄段
中心，喂 `world_model.x_lane_of` 得"路缘车道坐标"，事件窗内它的平移量就是
自车横移，**免标定外推、免帧间积分漂移**。

方法自带校验（两关，不过就退回设计起值）：
① 整道变道的位移读数应聚在 ≈1.0 车道（±0.3）；
② 位移方向符号应与人驾舵向一致（右舵 → 路缘 x_lane 减小 → 自记 delta=−位移）。

逐事件输出 (t_cmd, 舵向, 持舵, amp_lane, t_settle=峰值90%落位时刻) →
线性拟合 t_settle = base + k·amp。R²≥0.5 且 n≥8 才作定档依据。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_lane_change_profile.py <p1场> <p2场>
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_tau_steer import load_frames, load_pads, rise_events  # noqa: E402
from maaracing_master.plugins.speedrush.boundary import _HSV_HIGH, _HSV_LOW  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

CAL = load_calib()
ROWS = (420, 440, 460)        # 路缘取行带（分母 96–136：1 车道 ≈ 164–231px，远距噪声区之上）
MIN_RUN_PX = 8
WINDOW_S = 2.5
SETTLE_FRAC = 0.90
FPS_NOMINAL = 15.0            # 素材行率粗估，仅定窗长


def _edge_xlanes(gray_row_mask) -> float | None:
    """一行黄掩码 → 左右路缘的中位 x_lane（无左右缘各一才给数）。"""
    xs = np.flatnonzero(gray_row_mask)
    if xs.size == 0:
        return None
    runs, start, prev = [], int(xs[0]), int(xs[0])
    for x in xs[1:]:
        if x != prev + 1:
            if prev + 1 - start >= MIN_RUN_PX:
                runs.append((start + prev + 1) / 2.0)
            start = x
        prev = x
    if prev + 1 - start >= MIN_RUN_PX:
        runs.append((start + prev + 1) / 2.0)
    if not runs:
        return None
    mid = gray_row_mask.shape[0] // 2
    left = [c for c in runs if c < mid]
    right = [c for c in runs if c >= mid]
    if not left or not right:
        return None
    return left[0], right[-1]


def _frame_shift(img_bgr, y_row: int) -> float | None:
    """该行的 (左右缘 x_lane) → 平均车道坐标（右移为正）；拿不到双侧 None。"""
    roi = img_bgr[y_row - 2:y_row + 3, :]          # 3px 竖带容错
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, _HSV_LOW, _HSV_HIGH).max(axis=0) > 0
    er = _edge_xlanes(mask)
    if er is None:
        return None
    lx, rx = er
    # 自记 delta：路缘整体左移（自车右移）→ x_lane 减小 → 取负号使"自车右移为正"
    return -0.5 * (x_lane_of(int(lx), y_row, CAL) + x_lane_of(int(rx), y_row, CAL))


def measure(sess: Path, t_cmd: float, pads, frames) -> dict | None:
    ft = np.array([(f["ts_ns"] - pads[0]["ts_ns"]) / 1e9 for f in frames])
    pre = np.where(ft <= t_cmd)[0]
    if len(pre) < 8:
        return None
    ref = int(pre[-1])
    i0 = max(0, ref - 5)                                  # 指令前基线
    i1 = min(len(frames) - 1, ref + int(WINDOW_S * FPS_NOMINAL))
    base_vals: list[float] = []
    deltas: list[tuple[float, float]] = []               # (t−t_cmd, shift)
    for i in range(i0, i1 + 1):
        img = cv2.imread(str(sess / "frames" / frames[i]["file"]))
        if img is None:
            continue
        vals = [v for r in ROWS if (v := _frame_shift(img, r)) is not None]
        if not vals:
            continue
        v = float(np.median(vals))
        if i <= ref:
            base_vals.append(v)
        else:
            deltas.append((ft[i] - t_cmd, v))
    if len(base_vals) < 3 or len(deltas) < 8:
        return None
    base = float(np.median(base_vals))
    ts = np.array([t for t, _ in deltas])
    dv = np.array([v - base for _, v in deltas])         # 自车右移为正
    i_pk = int(np.argmax(np.abs(dv)))
    amp = float(dv[i_pk])
    if abs(amp) < 0.3:
        return None                                      # 没真挪道，剔
    level = abs(amp) * SETTLE_FRAC
    idx = np.where(np.abs(dv) >= level)[0]
    t_settle = float(ts[int(idx[0])]) if len(idx) else float(ts[i_pk])
    return {"amp_lane": round(amp, 2), "t_settle": round(t_settle, 3),
            "dir_ok": bool(np.sign(amp) > 0)}            # 见 main 里对人舵向核


def main() -> None:
    rows = []
    for arg in sys.argv[1:]:
        sess = Path(arg)
        pads, frames = load_pads(sess), load_frames(sess)
        for t_cmd, dirn, hold in rise_events(pads):
            r = measure(sess, t_cmd, pads, frames)
            if r:
                r.update(sess=sess.name, t=round(t_cmd, 2), dir=dirn, hold=round(hold, 2))
                rows.append(r)
    print(f"{'场':>20s} {'t':>6s} {'向':>2s} {'持舵':>5s} {'位移车道':>7s} {'落位s':>6s} {'向一致':>5s}")
    ok_n = 0
    for r in rows:
        want = 1 if r["dir"] == "R" else -1
        agree = np.sign(r["amp_lane"]) == want
        ok_n += int(agree)
        print(f"{r['sess']:>20s} {r['t']:>6.2f} {r['dir']:>2s} {r['hold']:>5.2f} "
              f"{r['amp_lane']:>7.2f} {r['t_settle']:>6.2f} {'✓' if agree else '✗':>4s}")
    n = len(rows)
    print(f"\nn={n} 方向一致 {ok_n}/{n}（{ok_n/max(1,n):.0%}）"
          f"——一致率<70% 则方法或素材存疑，数据不作定档依据")
    amps = np.array([abs(r["amp_lane"]) for r in rows]) if rows else np.array([])
    if n:
        print(f"位移分布：P50={np.median(amps):.2f} 车道，"
              f"[0.7,1.3] 内 {int(np.sum((amps>=0.7)&(amps<=1.3)))}/{n}"
              f"（整道变道应聚 1.0 附近——方法校验关）")
    if n < 8:
        print("样本 <8：只出画像，不做拟合定档 → timing 维持设计起值 base=0.30 k=0.40")
        return
    ts_ = np.array([r["t_settle"] for r in rows])
    A = np.vstack([np.ones(n), amps]).T
    (base, k), *_ = np.linalg.lstsq(A, ts_, rcond=None)
    pred = base + k * amps
    r2 = 1 - float(np.sum((ts_ - pred) ** 2)) / max(1e-9, float(np.sum((ts_ - ts_.mean()) ** 2)))
    print(f"拟合 base={base:.3f}s k={k:.3f}s/车道 R²={r2:.3f}"
          f"（R²≥0.5 方可作 timing 定档）")


if __name__ == "__main__":
    main()
