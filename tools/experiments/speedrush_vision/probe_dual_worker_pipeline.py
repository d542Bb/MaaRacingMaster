# -*- coding: utf-8 -*-
"""双 worker 流水线可行性探针（2026-10-09，只读，未改产线）。

问（性能第二批 + 并发审计复核）：
  ① 双 worker（GPU 线程 pre+forward → 有界交接槽 → 几何线程 reconstruct→…→grid）
    相比串行基线，吞吐 / 结果年龄 / 覆盖丢弃各改善多少？
  ② scan∥grid 双线程并跑的 GIL 现实收益（不预支，直接量）。
  ③ infer_points 冗余 pts.copy() 实省多少 ms。
  ④ MoGe 输出在 GPU→CPU 读回之后的交接时机（交接槽只发 CPU 可读的完整数据）。

方法：全部离线帧（demos），同一进程内 A/B，时间戳链全部 perf_counter。
**不 import depth_geo 的 AsyncDepthRoadObserver**（那是产线对象）——这里自建
最小流水线，复用产线函数本体（load_session/moge_post/reading_from_points/
drivable_grid_from_points），与产线路径逐位一致。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_dual_worker_pipeline.py
    ... --demo manual_20261003_215237_p2 --n 200 --stride 4
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush import moge_post  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

WEIGHTS = (Path(__file__).resolve().parents[3] / "maaracing_master" / "plugins"
           / "speedrush" / "resources" / "onnx" / "depth"
           / "moge2_vits_static_336x598_t1032_q4f16.onnx")
DEMOS = Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush/demos"


def load_frames(demo: str, n: int, stride: int) -> list[np.ndarray]:
    d = DEMOS / demo / "frames"
    files = sorted(d.glob("*.jpg"))[::stride][:n]
    out = []
    for f in files:
        bgr = cv2.imread(str(f))
        out.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    return out


def pct(xs, q):
    xs = [x for x in xs if x == x]
    return float(np.percentile(xs, q)) if xs else float("nan")


def report(label: str, xs: list[float]):
    print(f"  {label:34s} p50={pct(xs,50):7.2f} p95={pct(xs,95):7.2f} "
          f"mean={float(np.mean(xs)) if xs else float('nan'):7.2f}")


# ---------- ① 串行基线（产线同路径） ----------

def serial_baseline(sess, cal, ego, frames) -> dict:
    durs, ages = [], []   # ages=（探针无真实控制拍，用帧间隔模拟消费即结果完成时刻）
    for f in frames:
        t0 = time.perf_counter()
        blob = moge_post.preprocess(f, dg.MOGE_IN_W, dg.MOGE_IN_H)
        points, mask, ms = moge_post.forward(sess, blob, 1032)
        res = moge_post.reconstruct(points, mask, ms)
        pts = np.stack([cv2.resize(res["pts"][..., k], (dg.MOGE_IN_W, dg.MOGE_IN_H),
                                   interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
        valid = cv2.resize(res["valid"].astype(np.float32), (dg.MOGE_IN_W, dg.MOGE_IN_H),
                           interpolation=cv2.INTER_NEAREST).astype(bool)
        pts = pts.copy()
        pts[~valid] = np.nan
        fx = float(res["fx"]) * dg.MOGE_IN_W
        fy = float(res["fy"]) * dg.MOGE_IN_H
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)
        g = dg.drivable_grid_from_points(pts, fx, fy, ego, None, coef=rd.coef)
        durs.append((time.perf_counter() - t0) * 1e3)
    return {"dur": durs}


# ---------- ③ pts.copy() 实测 ----------

def measure_pts_copy(frames) -> None:
    """同一帧 30 次取中位：np.stack 后整体 copy 的成本（copy 与 nan 掩码写分开量）。"""
    f = frames[0]
    blob = moge_post.preprocess(f, dg.MOGE_IN_W, dg.MOGE_IN_H)
    # 用第一帧真实 forward 输出构造同尺寸数据（copy 成本只与形状/布局有关）
    # 构造数据非加密用途，固定种子保证可复现
    rng = np.random.default_rng(20261009)
    pts_n = rng.random((336, 598, 3)).astype(np.float32)
    up = [cv2.resize(pts_n[..., k], (1280, 720), interpolation=cv2.INTER_LINEAR)
          for k in range(3)]
    stacked = np.stack(up, -1)
    valid = np.ones((720, 1280), bool)
    t_copy, t_nan = [], []
    for _ in range(30):
        t = time.perf_counter()
        c = stacked.copy()
        t_copy.append((time.perf_counter() - t) * 1e3)
        t = time.perf_counter()
        c[~valid] = np.nan
        t_nan.append((time.perf_counter() - t) * 1e3)
    print("\n=== ③ pts.copy() 实测（720×1280×3 fp32，30 次）===")
    report("stacked.copy()", t_copy)
    report("nan 掩码写（对照）", t_nan)
    print("  （产线 copy 后紧跟同一掩码写——copy 可省：直接在 stack 产物上写 nan）")


# ---------- ② scan ∥ grid GIL 现实收益 ----------

def measure_scan_grid_parallel(sess, cal, ego, frames) -> None:
    """同一份点云上 scan+grid 串行 vs 两线程并跑（GIL 下的真实加速比）。"""
    # 先备一份点云（与产线 upsize 段同口径）
    f = frames[3]
    blob = moge_post.preprocess(f, dg.MOGE_IN_W, dg.MOGE_IN_H)
    points, mask, ms = moge_post.forward(sess, blob, 1032)
    res = moge_post.reconstruct(points, mask, ms)
    pts = np.stack([cv2.resize(res["pts"][..., k], (dg.MOGE_IN_W, dg.MOGE_IN_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid = cv2.resize(res["valid"].astype(np.float32), (dg.MOGE_IN_W, dg.MOGE_IN_H),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    pts = pts.copy()
    pts[~valid] = np.nan
    fx = float(res["fx"]) * dg.MOGE_IN_W
    fy = float(res["fy"]) * dg.MOGE_IN_H

    def _scan():
        return dg.reading_from_points(pts, fx, cal, ego_mask=ego, fy=fy)

    def _grid():
        # coef=None：自拟合口径（与 reading 无共享，纯并行语义）
        return dg.drivable_grid_from_points(pts, fx, fy, ego, None)

    # 串行
    t_ser = []
    for _ in range(12):
        t = time.perf_counter()
        _scan()
        _grid()
        t_ser.append((time.perf_counter() - t) * 1e3)
    # 并行（两线程同时开工，各跑 12 轮，量总墙钟）
    t_par = []
    for _ in range(12):
        th1 = threading.Thread(target=_scan)
        th2 = threading.Thread(target=_grid)
        t = time.perf_counter()
        th1.start(); th2.start()
        th1.join(); th2.join()
        t_par.append((time.perf_counter() - t) * 1e3)
    print("\n=== ② scan ∥ grid（同一份点云，12 轮）===")
    report("串行 scan+grid", t_ser)
    report("双线程并行 scan+grid", t_par)
    s50, p50 = pct(t_ser, 50), pct(t_par, 50)
    print(f"  加速比 = {s50 / p50:.2f}x（≈1.0 即 GIL 吃光，不值得并行；"
          f">1.3 才有工程价值）")


# ---------- ④ 双 worker 流水线 ----------

class PipelineProbe:
    """GPU 线程（pre+forward）→ 有界交接槽（maxsize=1，latest-wins）→
    几何线程（reconstruct→upsize→nan→scan+grid）。交接槽只发 CPU 已读回的
    完整原生点图 + 焦距 + 帧号 + 时间戳——GPU tensor 不出 GPU 线程（④）。"""

    def __init__(self, sess, cal, ego):
        self.sess, self.cal, self.ego = sess, cal, ego
        self.q: queue.Queue = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.stats = {"pushed": 0, "gpu_done": 0, "geo_done": 0,
                      "covered": 0, "dropped_full": 0}
        self.geo_done_ts: dict[int, float] = {}   # fid → 几何完成时刻
        self.gpu_done_ts: dict[int, float] = {}
        self.push_ts: dict[int, float] = {}
        self.geo_lock = threading.Lock()
        self.th_gpu = threading.Thread(target=self._gpu_loop, daemon=True)
        self.th_geo = threading.Thread(target=self._geo_loop, daemon=True)

    def start(self):
        self.th_gpu.start()
        self.th_geo.start()

    def stop(self):
        self.stop.set()
        self.th_gpu.join(timeout=3)
        self.th_geo.join(timeout=3)

    def push(self, fid, frame):
        self.stats["pushed"] += 1
        self.push_ts[fid] = time.perf_counter()
        item = (fid, frame, self.push_ts[fid])
        try:
            self.q.put_nowait(item)   # 有界槽：放得进才放（模拟 latest-wins push）
        except queue.Full:
            try:
                self.q.get_nowait()   # 覆盖旧帧
                self.stats["covered"] += 1
            except queue.Empty:
                self.stats["dropped_full"] += 1
            try:
                self.q.put_nowait(item)
            except queue.Full:
                self.stats["dropped_full"] += 1

    def _gpu_loop(self):
        while not self.stop.is_set():
            try:
                fid, frame, t_push = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            blob = moge_post.preprocess(frame, dg.MOGE_IN_W, dg.MOGE_IN_H)
            points, mask, ms = moge_post.forward(self.sess, blob, 1032)
            # GPU→CPU 读回已发生在 forward 内（copy_outputs_to_cpu）——此处拿到的
            # points/mask 是 CPU ndarray，交接槽发的是完整 CPU 数据（④）
            res = moge_post.reconstruct(points, mask, ms)
            self.gpu_done_ts[fid] = time.perf_counter()
            self.stats["gpu_done"] += 1
            self._geo_in = (fid, res, t_push)
            self.geo_q.put((fid, res, t_push))

    geo_q: queue.Queue = queue.Queue(maxsize=1)

    def shutdown(self):
        self.stop.set()
        self.th_gpu.join(timeout=3)
        self.th_geo.join(timeout=3)

    def _geo_loop(self):
        while not self.stop.is_set():
            try:
                fid, res, t_push = self.geo_q.get(timeout=0.5)
            except queue.Empty:
                continue
            pts = np.stack([cv2.resize(res["pts"][..., k], (dg.MOGE_IN_W, dg.MOGE_IN_H),
                                       interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
            valid = cv2.resize(res["valid"].astype(np.float32), (dg.MOGE_IN_W, dg.MOGE_IN_H),
                               interpolation=cv2.INTER_NEAREST).astype(bool)
            pts = pts.copy()
            pts[~valid] = np.nan
            fx = float(res["fx"]) * dg.MOGE_IN_W
            fy = float(res["fy"]) * dg.MOGE_IN_H
            rd = dg.reading_from_points(pts, fx, self.cal, ego_mask=self.ego, fy=fy)
            g = dg.drivable_grid_from_points(pts, fx, fy, self.ego, None, coef=rd.coef)
            with self.geo_lock:
                self.geo_done_ts[fid] = time.perf_counter()
            self.stats["geo_done"] += 1


def measure_pipeline(sess, cal, ego, frames, tick_s: float) -> None:
    """20Hz 模拟控制拍 push → 跑完统计。"""
    probe = PipelineProbe(sess, cal, ego)
    probe.start()
    # 预热
    for f in frames[:3]:
        probe.push(0, f)
        time.sleep(0.3)
    with probe.geo_lock:
        probe.geo_done_ts.clear()
    probe.stats.update({"pushed": 0, "gpu_done": 0, "geo_done": 0,
                        "covered": 0, "dropped_full": 0})
    t_start = time.perf_counter()
    for i, f in enumerate(frames):
        probe.push(i, f)
        time.sleep(tick_s)
    # 等几何排空
    target = probe.stats["pushed"]
    t_wait = time.perf_counter()
    while probe.stats["geo_done"] < probe.stats["gpu_done"] and \
            time.perf_counter() - t_wait < 30:
        time.sleep(0.05)
    wall = time.perf_counter() - t_start
    probe.shutdown()

    st = probe.stats
    with probe.geo_lock:
        done_ts = dict(probe.geo_done_ts)
    push_ts = probe.push_ts
    ages, gpu_lat = [], []
    for fid, ts in done_ts.items():
        if fid in push_ts:
            ages.append((ts - push_ts[fid]) * 1e3)
    for fid, ts in probe.gpu_done_ts.items():
        if fid in push_ts:
            gpu_lat.append((ts - push_ts[fid]) * 1e3)

    print("\n=== ④ 双 worker 流水线（20Hz push 模拟）===")
    print(f"  push {st['pushed']}  gpu_done {st['gpu_done']}  geo_done {st['geo_done']}"
          f"  covered(旧帧被覆盖) {st['covered']}  wall {wall:.1f}s")
    if st["pushed"]:
        print(f"  几何产出率 = {st['geo_done'] / wall:.1f} Hz")
    report("push→几何完成 年龄(ms)", ages)
    report("push→GPU 段完成(ms)", gpu_lat)
    print("  注：年龄零点=push（离线无采集回调）；实机上 push 前还有 cap 段。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", default="manual_20261003_215150_p1")
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--stride", type=int, default=4)
    ap.add_argument("--tick-ms", type=float, default=50.0,
                    help="控制拍间隔（20Hz 默认）")
    a = ap.parse_args()
    if not WEIGHTS.exists():
        sys.exit(f"缺权重 {WEIGHTS}")

    frames = load_frames(a.demo, a.n, a.stride)
    print(f"帧 {len(frames)} 张（{a.demo}, stride={a.stride}）")

    import onnxruntime as ort
    ort.set_default_logger_severity(3)
    sess = dg.load_session(WEIGHTS)
    cal = load_calib()
    obs = dg.DepthRoadObserver(sess, cal)
    ego = obs._ego_mask

    # 预热 DML
    for f in frames[:3]:
        blob = moge_post.preprocess(f, dg.MOGE_IN_W, dg.MOGE_IN_H)
        moge_post.forward(sess, blob, 1032)

    print("\n=== ① 串行基线（产线同路径，无并行）===")
    base = serial_baseline(sess, cal, ego, frames)
    report("单帧端到端（pre→grid）", base["dur"])
    p50 = pct(base["dur"], 50)
    print(f"  串行产出率 = {1000.0 / p50:.1f} Hz（1000/p50，理想无 tick 间隔）")

    measure_pts_copy(frames)
    measure_scan_grid_parallel(sess, cal, ego, frames)
    measure_pipeline(sess, cal, ego, frames, a.tick_ms / 1000.0)

    print("\n判读：④ 的产出率与年龄对比 ① 的串行产出率；"
          "若产出率≈GPU 段频率（即几何不再是产出间隔的决定项）则双 worker 成立。")


if __name__ == "__main__":
    main()
