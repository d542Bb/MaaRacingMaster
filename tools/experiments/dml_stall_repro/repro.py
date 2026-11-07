#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DML 推理停顿离线复现台（自包含，不 import 业务代码）。

重建生产调用面（见 README「方法」）：感知 20Hz 持锁 run + 深度 worker 错峰
Busy 让锁 + 可选录制 IO；nvidia-smi 独立标尺轮询。

用法：
  python repro.py --duration 90                          # V1 仅感知
  python repro.py --duration 180 --moge-interval 0.16    # V2 双会话
  python repro.py --duration 180 --moge-interval 0.16 --io   # V3 +录制 IO
输出：<out>/timings.jsonl（逐拍）、<out>/gpu.jsonl（标尺）、stdout 汇总。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

REPO = Path(__file__).resolve().parents[3]
PERCEPTION_ONNX = REPO / "maaracing_master/plugins/speedrush/resources/models/perception/model.onnx"
DEPTH_ONNX = REPO / "maaracing_master/plugins/speedrush/resources/models/depth/moge2_vits_static_336x598_t1032_q4f16.onnx"
MOGE_W, MOGE_H = 1280, 720
YOLO_IN = 640


# ── 会话构造（与生产同参）───────────────────────────────────────────

def yolo_session(cache_dir: Path) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.optimized_model_filepath = str(cache_dir / "yolo_optimized.onnx")
    so.add_session_config_entry("session.dml_kernel_cache_path", str(cache_dir))
    so.add_session_config_entry("session.dml_kernel_cache_enabled", "1")
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 4
    so.enable_mem_pattern = True
    so.enable_cpu_mem_arena = True
    return ort.InferenceSession(str(PERCEPTION_ONNX), sess_options=so,
                                providers=["DmlExecutionProvider", "CPUExecutionProvider"])


def moge_session() -> ort.InferenceSession:
    avail = set(ort.get_available_providers())
    providers = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider",
                             "CPUExecutionProvider") if p in avail]
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"), (1, MOGE_H, MOGE_W)):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(DEPTH_ONNX), sess_options=so, providers=providers)


# ── 锁协议（同生产：阻塞持有 / Busy 让锁 / 错峰节拍）────────────────

LOCK = threading.Lock()


class Beat:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._seq = 0

    def note(self) -> None:
        with self._cv:
            self._seq += 1
            self._cv.notify_all()

    def snapshot(self) -> int:
        with self._cv:
            return self._seq

    def wait_newer(self, seq: int, timeout: float) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._seq != seq, timeout=timeout)


BEAT = Beat()


# ── 预处理（与生产同形）─────────────────────────────────────────────

def yolo_blob(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    scale = min(YOLO_IN / h, YOLO_IN / w)
    nh, nw = int(h * scale), int(w * scale)
    pad_y, pad_x = (YOLO_IN - nh) // 2, (YOLO_IN - nw) // 2
    padded = np.full((YOLO_IN, YOLO_IN, 3), 114, dtype=np.uint8)
    padded[pad_y:pad_y + nh, pad_x:pad_x + nw] = cv2.resize(
        img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0


def moge_blob(img: np.ndarray) -> np.ndarray:
    im = cv2.resize(img, (MOGE_W, MOGE_H), interpolation=cv2.INTER_AREA)
    return (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]


# ── nvidia-smi 独立标尺 ─────────────────────────────────────────────

def cpu_hog(stop: threading.Event, gil: bool) -> None:
    """生产缺口的 CPU 压力：gil=True 纯 Python 循环（占 GIL）；False 用 numpy 大数组。"""
    if gil:
        x = 0
        while not stop.is_set():
            for _ in range(10000):
                x = (x * 1103515245 + 12345) & 0x7FFFFFFF
            stop.wait(0.001)
    else:
        a = np.random.rand(720, 1280, 3).astype(np.float32)
        while not stop.is_set():
            np.multiply(a, 1.0000001, out=a)
            np.sqrt(a, out=a)


def gpu_poller(stop: threading.Event, path: Path, interval: float = 0.5) -> None:
    q = ("timestamp,utilization.gpu,memory.used,memory.total,temperature.gpu,"
         "clocks.sm,clocks.mem,power.draw")
    while not stop.is_set():
        t = time.perf_counter()
        try:
            r = subprocess.run(
                ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else "NA"
        except Exception as exc:                       # nvidia-smi 缺失等
            line = f"ERR,{exc}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": t, "raw": line}) + "\n")
        stop.wait(interval)


# ── 场景线程 ────────────────────────────────────────────────────────

def tick_loop(stop: threading.Event, sess: ort.InferenceSession,
              frames: list[np.ndarray], args, out_rows: list, io_dir: Path | None) -> None:
    input_name = sess.get_inputs()[0].name
    period = 1.0 / args.yolo_hz
    n = len(frames)
    i = 0
    warm = 0
    while not stop.is_set():
        t0 = time.perf_counter()
        img = frames[i % n]
        i += 1
        blob = yolo_blob(img)
        t_wait = time.perf_counter()
        with LOCK:
            wait_ms = (time.perf_counter() - t_wait) * 1000.0
            t_run = time.perf_counter()
            sess.run(None, {input_name: blob})
            run_ms = (time.perf_counter() - t_run) * 1000.0
        BEAT.note()
        if run_ms >= 100.0:
            print(f"[STALL] t={t0:.2f} yolo_run={run_ms:.1f}ms wait={wait_ms:.1f}ms idx={i % n}",
                  flush=True)
        if i > warm:                                   # 首拍预热不计
            out_rows.append({"t": t0, "kind": "yolo", "wait_ms": round(wait_ms, 2),
                             "run_ms": round(run_ms, 2), "i": i % n})
        if io_dir is not None:                         # 录制 IO：JPEG 编码+写盘
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if ok:
                (io_dir / "raw.jpg").write_bytes(buf.tobytes())
            ok2, buf2 = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok2:
                (io_dir / "hud.jpg").write_bytes(buf2.tobytes())
        rest = period - (time.perf_counter() - t0)
        if rest > 0:
            stop.wait(rest)


def depth_loop(stop: threading.Event, sess: ort.InferenceSession,
               frames: list[np.ndarray], args, out_rows: list) -> None:
    names = {i.name for i in sess.get_inputs()}
    n = len(frames)
    i = 0
    while not stop.is_set():
        t_due = time.perf_counter() + args.moge_interval
        seq = BEAT.snapshot()
        BEAT.wait_newer(seq, args.moge_interval)       # 错峰：等感知放锁后的新拍
        img = frames[(i * 7) % n]
        i += 1
        blob = moge_blob(img)
        t_wait = time.perf_counter()
        got = LOCK.acquire(blocking=False)
        wait_ms = (time.perf_counter() - t_wait) * 1000.0
        if not got:
            out_rows.append({"t": time.perf_counter(), "kind": "moge", "busy": 1})
            stop.wait(max(0.0, t_due - time.perf_counter()))
            continue
        try:
            t_run = time.perf_counter()
            feed = {"image": blob}
            if "num_tokens" in names:
                feed["num_tokens"] = np.array(1032, np.int64)
            sess.run(None, feed)
            run_ms = (time.perf_counter() - t_run) * 1000.0
        finally:
            LOCK.release()
        if run_ms >= 200.0:
            print(f"[STALL-M] t={t_run:.2f} moge_run={run_ms:.1f}ms", flush=True)
        out_rows.append({"t": t_run, "kind": "moge", "wait_ms": round(wait_ms, 2),
                         "run_ms": round(run_ms, 2)})
        stop.wait(max(0.0, t_due - time.perf_counter()))


# ── 汇总 ────────────────────────────────────────────────────────────

def pct(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) -1))))] if s else float("nan")


def summarize(rows: list) -> str:
    y = [r["run_ms"] for r in rows if r["kind"] == "yolo"]
    yw = [r["wait_ms"] for r in rows if r["kind"] == "yolo"]
    m = [r["run_ms"] for r in rows if r["kind"] == "moge" and "run_ms" in r]
    busy = sum(1 for r in rows if r.get("busy"))
    lines = [f"yolo n={len(y)} run_ms p50={pct(y,50):.1f} p95={pct(y,95):.1f} "
             f"p99={pct(y,99):.1f} max={max(y):.1f}",
             f"yolo lock_wait p95={pct(yw,95):.1f} max={max(yw):.1f}  "
             f"yolo>100ms={sum(1 for v in y if v>100)} >500ms={sum(1 for v in y if v>500)} "
             f">1000ms={sum(1 for v in y if v>1000)}"]
    if m:
        lines.append(f"moge n={len(m)} run_ms p50={pct(m,50):.1f} p95={pct(m,95):.1f} "
                     f"max={max(m):.1f}  busy_skip={busy} "
                     f"moge>200ms={sum(1 for v in m if v>200)}")
    return "\n".join(lines)


def capture_loop(stop: threading.Event, monitor: int, gaps: list) -> None:
    """WGC 常驻采集（同生产库）：回调只计数记时——断流探测比 run 耗时更灵敏。"""
    from windows_capture import WindowsCapture
    cap = WindowsCapture(monitor_index=monitor, draw_border=False,
                         minimum_update_interval=16)
    last = {"t": None, "n": 0}

    @cap.event
    def on_frame_arrived(frame, _control):
        now = time.perf_counter()
        if last["t"] is not None:
            gap = now - last["t"]
            if gap > 0.5:
                gaps.append((last["t"], now, gap))
        last["t"] = now
        last["n"] += 1

    @cap.event
    def on_closed():
        pass

    control = cap.start_free_threaded()
    stop.wait(1.0)
    while not stop.is_set():
        stop.wait(1.0)
    control.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=90.0)
    ap.add_argument("--yolo-hz", type=float, default=20.0)
    ap.add_argument("--moge-interval", type=float, default=0.0,
                    help="深度观察间隔秒；0=关闭")
    ap.add_argument("--io", action="store_true", help="每拍 JPEG 编码+写盘（录制仿真）")
    ap.add_argument("--cpu-hog", type=int, default=0, help="numpy 大数组计算线程数")
    ap.add_argument("--gil-hog", type=int, default=0, help="纯 Python 占 GIL 线程数")
    ap.add_argument("--capture", type=int, default=0,
                    help=">0 = 同时开 WGC 监视器采集（值为 monitor_index，1 起）")
    ap.add_argument("--frames", default=str(Path(os.environ["APPDATA"])
                                            / "MaaRacingMaster/debug/20261107_143537/"
                                            "speedrush/drive/20261107_145435_p1/raw"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"))
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    io_dir = out / "io_tmp"
    if args.io:
        io_dir.mkdir(exist_ok=True)

    frame_paths = sorted(Path(args.frames).glob("*_raw.jpg"))[:60]
    assert frame_paths, f"无帧: {args.frames}"
    frames = [cv2.imread(str(p), cv2.IMREAD_COLOR) for p in frame_paths]
    frames = [f for f in frames if f is not None]
    print(f"[cfg] frames={len(frames)} size={frames[0].shape[1]}x{frames[0].shape[0]} "
          f"duration={args.duration}s yolo_hz={args.yolo_hz} "
          f"moge_interval={args.moge_interval or 'off'} io={args.io}", flush=True)

    import tempfile
    cache = Path(tempfile.gettempdir()) / "maar_dml_repro_cache"
    cache.mkdir(exist_ok=True)
    ysess = yolo_session(cache)
    print(f"[sess] yolo providers={ysess.get_providers()}", flush=True)
    msess = None
    if args.moge_interval > 0:
        msess = moge_session()
        print(f"[sess] moge providers={msess.get_providers()}", flush=True)
        b = moge_blob(frames[0])
        msess.run(None, {"image": b, "num_tokens": np.array(1032, np.int64)}
                  if "num_tokens" in {i.name for i in msess.get_inputs()} else {"image": b})

    stop = threading.Event()
    rows: list = []
    cap_gaps: list = []
    th_poll = threading.Thread(target=gpu_poller, args=(stop, out / "gpu.jsonl"), daemon=True)
    th_poll.start()
    if args.capture > 0:
        th_cap = threading.Thread(target=capture_loop,
                                  args=(stop, args.capture, cap_gaps), daemon=True)
        th_cap.start()
        print(f"[cfg] WGC 采集 monitor={args.capture} 已启动", flush=True)
    workers = [threading.Thread(target=tick_loop,
                                args=(stop, ysess, frames, args, rows, io_dir if args.io else None))]
    for _ in range(args.cpu_hog):
        workers.append(threading.Thread(target=cpu_hog, args=(stop, False)))
    for _ in range(args.gil_hog):
        workers.append(threading.Thread(target=cpu_hog, args=(stop, True)))
    if msess is not None:
        workers.append(threading.Thread(target=depth_loop,
                                        args=(stop, msess, frames, args, rows)))
    for w in workers:
        w.start()
    try:
        stop.wait(args.duration)
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=5)
    stop.wait(0.6)                                     # 让 poller 收尾

    with open(out / f"timings_{args.tag}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    if cap_gaps:
        print(f"[capture] 断流 >0.5s 共 {len(cap_gaps)} 次: "
              + "; ".join(f"{g:.2f}s@t{a:.0f}" for a, b, g in cap_gaps[:8]))
    else:
        print("[capture] 无 >0.5s 断流" if args.capture > 0 else "[capture] 未启用")
    print(summarize(rows))


if __name__ == "__main__":
    main()
