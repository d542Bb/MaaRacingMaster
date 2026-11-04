# -*- coding: utf-8 -*-
"""深度 observe 单进程阶段分解（GPU 侧）：preprocess / forward / reconstruct /
上采样 / reading_from_points 各段耗时 + GPU 利用率采样。

问（2026-10-04 性能归因）：worker 耗时 p50 130~153ms 里，MoGe 推理本体、
recover_focal_shift/点云后处理、帧拷贝/缩放各占多少？

本探针用**产线同一路径**（depth_geo.load_session + moge_post.* + reading_from_points）
在录制的 1280×720 帧上逐段计时，全部在同一进程/同一时刻测，比例不受机器负载漂移影响
（绝对值受；故同时报 min 与 p50）。CPU 侧证据包重放见
`probe_depth_pipeline_attribution.py`（无需 GPU，口径与产线逐位一致）。

**边界**：本机当前**未跑游戏**（nvidia-smi 空闲态），故 forward 是「无游戏并发」口径；
与游戏并发时 GPU 争用会把 forward 抬高——该差额必须实机复核，本探针只给空载分解。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_moge_stage_decompose.py --n 60
    ... --demo manual_20261003_215237_p2 --n 40
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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


def _use_ref_module() -> str | None:
    """DEPTH_GEO_REF=<path> 时把 dg.reading_from_points 换成该文件里的实现。

    改造前后同机对比用（`git show HEAD:<...>/depth_geo.py > ref.py`）。只换入口
    函数：参考实现自带它的全部辅助函数；推理/上采样段不受影响。"""
    p = os.environ.get("DEPTH_GEO_REF")
    if not p:
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("depth_geo_ref", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["depth_geo_ref"] = mod
    spec.loader.exec_module(mod)
    dg.reading_from_points = mod.reading_from_points
    return p

WEIGHTS = (Path(__file__).resolve().parents[3] / "maaracing_master" / "plugins"
           / "speedrush" / "resources" / "onnx" / "depth"
           / "moge2_vits_static_336x598_t1032_q4f16.onnx")
DEMOS = Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush/demos"


class GpuSampler:
    def __init__(self):
        self.rows: list[tuple[float, float, float, float]] = []
        self._stop = threading.Event()
        exe = shutil.which("nvidia-smi")
        self._proc = None
        if exe:
            try:
                self._proc = subprocess.Popen(
                    [exe, "--query-gpu=utilization.gpu,memory.used,clocks.sm",
                     "--format=csv,noheader,nounits", "-lms", "100"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                threading.Thread(target=self._pump, daemon=True).start()
            except Exception:
                self._proc = None

    def _pump(self):
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            try:
                u, m, c = (float(v) for v in line.strip().split(","))
                self.rows.append((time.perf_counter(), u, m, c))
            except ValueError:
                pass

    def window(self, t0, t1):
        sel = [r for r in self.rows if t0 <= r[0] <= t1]
        if not sel:
            return None
        return (float(np.mean([r[1] for r in sel])),
                float(np.max([r[2] for r in sel])),
                float(np.mean([r[3] for r in sel])))

    def close(self):
        self._stop.set()
        if self._proc:
            self._proc.kill()


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


def report(name: str, xs: list[float]):
    print(f"  {name:22s} min={min(xs):7.2f} p50={pct(xs,50):7.2f} "
          f"p95={pct(xs,95):7.2f} max={max(xs):7.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", default="manual_20261003_215150_p1")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--stride", type=int, default=10)
    ap.add_argument("--with-observe", action="store_true",
                    help="另跑产线 observe_debug 端到端做交叉核对")
    a = ap.parse_args()
    ref = _use_ref_module()
    if ref:
        print(f"reading_from_points 实现 = {ref}")
    if not WEIGHTS.exists():
        sys.exit(f"缺权重 {WEIGHTS}")

    frames = load_frames(a.demo, a.n, a.stride)
    print(f"帧 {len(frames)} 张（{a.demo}, stride={a.stride}），权重 {WEIGHTS.name}")

    import onnxruntime as ort
    ort.set_default_logger_severity(3)
    avail = set(ort.get_available_providers())
    print("providers:", [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
                         if p in avail])

    sess = dg.load_session(WEIGHTS)
    cal = load_calib()
    obs = dg.DepthRoadObserver(sess, cal)
    ego = obs._ego_mask

    sampler = GpuSampler()
    st = {k: [] for k in ("pre", "fwd", "rec", "resize", "read", "sum")}
    # 预热（走产线 observe_debug，确保各段与 DML 编译都热）
    for f in frames[:3]:
        obs.observe_debug(f)
    t0 = time.perf_counter()
    for f in frames:
        t = time.perf_counter()
        blob = moge_post.preprocess(f, dg.MOGE_IN_W, dg.MOGE_IN_H)
        st["pre"].append((time.perf_counter() - t) * 1e3)
        t = time.perf_counter()
        points, mask, ms = moge_post.forward(sess, blob, 1032)
        st["fwd"].append((time.perf_counter() - t) * 1e3)
        t = time.perf_counter()
        res = moge_post.reconstruct(points, mask, ms)
        st["rec"].append((time.perf_counter() - t) * 1e3)
        t = time.perf_counter()
        pts = np.stack([cv2.resize(res["pts"][..., k], (dg.MOGE_IN_W, dg.MOGE_IN_H),
                                   interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
        valid = cv2.resize(res["valid"].astype(np.float32), (dg.MOGE_IN_W, dg.MOGE_IN_H),
                           interpolation=cv2.INTER_NEAREST).astype(bool)
        pts = pts.copy()
        pts[~valid] = np.nan
        st["resize"].append((time.perf_counter() - t) * 1e3)
        t = time.perf_counter()
        rd = dg.reading_from_points(pts, float(res["fx"]) * dg.MOGE_IN_W, cal,
                                    ego_mask=ego, fy=float(res["fy"]) * dg.MOGE_IN_H)
        st["read"].append((time.perf_counter() - t) * 1e3)
        st["sum"].append(st["pre"][-1] + st["fwd"][-1] + st["rec"][-1]
                         + st["resize"][-1] + st["read"][-1])
    g = sampler.window(t0, time.perf_counter())
    sampler.close()

    print("\n=== 阶段分解（同进程顺序测，单位 ms）===")
    for k, label in (("pre", "preprocess(RGB→720x1280)"),
                     ("fwd", "forward(DML run+读回)"),
                     ("rec", "reconstruct(focal/shift)"),
                     ("resize", "上采样(点图→全幅)"),
                     ("read", "reading_from_points"),
                     ("sum", "observe 合计(五段和)")):
        report(label, st[k])
    if g:
        print(f"\nGPU 采样: util≈{g[0]:.0f}%  vram≤{g[1]:.0f}MB  sm_clk≈{g[2]:.0f}MHz")
    else:
        print("\nGPU 采样不可用")

    if a.with_observe:
        lat = []
        for f in frames:
            r, _ = obs.observe_debug(f)
            if r is not None:
                lat.append(r.latency_ms)
        print("\n=== observe_debug 端到端（产线函数，含 session 内锁）===")
        report("observe_debug.latency_ms", lat)
    print(f"\n注：本机当前未跑游戏 → forward 为无并发口径；与游戏并发需实机复核。")


if __name__ == "__main__":
    main()
