# -*- coding: utf-8 -*-
"""DML 单 session 吞吐解剖探针：沿「CPU orchestration → sync → 数据搬运 → GPU」拆 sess.run()。

背景（2026-10-02）：产线 MoGe q4f16 静态图（720×1280 → 336×598）DML 稳定 50–60ms、
GPU 利用率仅 20–40%。已排除显存分页（IO binding 定案）与算力不足假设；本探针验证
剩余假设——每帧 command recording/submission/读回同步占了 run 墙钟的大头。

条件（每条件独立会话，产线 load_session 同口径：free-dim 覆盖 1/720/1280 + DML 优先）：
  baseline_run   纯 sess.run(feed)（无 binding 参照）
  iobind_cpu     产线路径：CPU 输入缓冲 + CPU 输出绑定 + copy_outputs_to_cpu（每帧读回）
  capture_cpu    iobind_cpu + ep.dml.enable_graph_capture=1（P0 主对照）
  spin_cpu       iobind_cpu + ep.dml.enable_cpu_sync_spinning=1（P4 A/B）
  out_dml        输入 CPU + 输出绑 dml，循环内不读回，仅末尾读一次（量化读回同步占比）
  in_dml         输入/输出全驻 dml（量化每帧上传成本；产线每帧必须换图，仅诊断）

判读（写死在这里防事后挪门柱）：
  - capture 若生效：CPU 时间（process_time）应显著低于 iobind_cpu（record→replay）；
    wall p50 降幅 <3% 且 CPU 时间无差 → 判「capture 未生效或瓶颈不在 submission」。
  - out_dml vs iobind_cpu 的 wall 差 ≈ 每帧读回同步+拷贝成本。
  - in_dml vs iobind_cpu 的差 ≈ 每帧 11MB 上传成本（诊断值，产线不可省）。
  - 输出校验和逐条件对比，任何条件与 baseline 不一致即记录（capture 重放正确性）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_dml_orchestration.py
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_dml_orchestration.py --verbose-capture
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

MODEL = Path(r"D:\maaracing_assistant\maaracing_master\plugins\speedrush"
             r"\resources\onnx\depth\moge2_vits_static_336x598_t1032_q4f16.onnx")
MOGE_IN_H, MOGE_IN_W = 720, 1280   # 与 depth_geo.load_session 同口径


def make_sess(configs: tuple[tuple[str, str], ...] = (), log_sev: int | None = 3):
    import onnxruntime as ort
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"),
                         (1, MOGE_IN_H, MOGE_IN_W)):
        so.add_free_dimension_override_by_name(name, dim)
    for k, v in configs:
        so.add_session_config_entry(k, v)
    if log_sev is not None:
        so.log_severity_level = log_sev
    return ort.InferenceSession(str(MODEL), sess_options=so,
                                providers=("DmlExecutionProvider",
                                           "CPUExecutionProvider"))


class GpuSampler:
    """nvidia-smi 100ms 采样 GPU 利用率与显存；不可用时静默降级。"""

    def __init__(self):
        self.rows: list[tuple[float, float, float]] = []
        self._proc = None
        self._stop = threading.Event()
        exe = shutil.which("nvidia-smi")
        if exe:
            try:
                self._proc = subprocess.Popen(
                    [exe, "--query-gpu=utilization.gpu,memory.used",
                     "--format=csv,noheader,nounits", "-lms", "100"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True)
                threading.Thread(target=self._pump, daemon=True).start()
            except Exception:
                self._proc = None

    def _pump(self):
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            try:
                util, mem = (float(v) for v in line.strip().split(","))
                self.rows.append((time.perf_counter(), util, mem))
            except ValueError:
                pass

    def window(self, t0: float, t1: float) -> tuple[float, float] | None:
        sel = [r for r in self.rows if t0 <= r[0] <= t1]
        if not sel:
            return None
        return (float(np.mean([r[1] for r in sel])),
                float(np.max([r[2] for r in sel])))

    def close(self):
        self._stop.set()
        if self._proc is not None:
            self._proc.kill()


def bench(sess, mode: str, iters: int, warmup: int, rng_val: np.ndarray,
          sampler: GpuSampler, baseline_hash: str | None) -> dict:
    """mode: feed | iobind | iobind_noreadback | iobind_dml_in"""
    import onnxruntime as ort
    in_name = sess.get_inputs()[0].name
    feed = {in_name: rng_val}
    bind = None
    xbuf = None
    if mode != "feed":
        bind = sess.io_binding()
        if mode == "iobind_dml_in":
            ov = ort.OrtValue.ortvalue_from_numpy(rng_val, "dml", 0)
            bind.bind_ortvalue_input(in_name, ov)
        else:
            xbuf = np.ascontiguousarray(rng_val)
            bind.bind_input(in_name, "cpu", 0, xbuf.dtype, xbuf.shape,
                            xbuf.ctypes.data)
        out_dev = "dml" if mode == "iobind_noreadback" else "cpu"
        for o in sess.get_outputs():
            bind.bind_output(o.name, out_dev, 0)

    def one_run():
        if bind is None:
            sess.run(None, feed)
        else:
            sess.run_with_iobinding(bind, None)

    t0 = time.perf_counter()
    one_run()
    first = (time.perf_counter() - t0) * 1e3

    for _ in range(warmup):
        one_run()

    walls, cpus = [], []
    loop_t0 = time.perf_counter()
    for _ in range(iters):
        if xbuf is not None:
            np.copyto(xbuf, rng_val)      # 产线每帧覆写输入，口径保真
        c0 = time.process_time_ns()
        t1 = time.perf_counter()
        one_run()
        if mode != "iobind_noreadback":
            if bind is not None:
                bind.copy_outputs_to_cpu()
        walls.append((time.perf_counter() - t1) * 1e3)
        cpus.append((time.process_time_ns() - c0) / 1e6)
    loop_wall = (time.perf_counter() - loop_t0) * 1e3

    # 排空 GPU 队列后再读回：readback 阻塞段 = 残余积压 + 拷贝，计入端到端吞吐
    time.sleep(0.25)
    rb0 = time.perf_counter()
    if bind is not None:
        outs = bind.copy_outputs_to_cpu()
    else:
        outs = sess.run(None, feed)
    readback_ms = (time.perf_counter() - rb0) * 1e3
    throughput_ms = (loop_wall + readback_ms) / iters
    digest = hashlib.sha256()
    for o in outs:
        digest.update(np.ascontiguousarray(o).tobytes())
    out_hash = digest.hexdigest()[:16]

    w = np.array(walls)
    c = np.array(cpus)
    return {"mode": mode, "first_ms": first,
            "loop_wall_ms": loop_wall, "readback_ms": readback_ms,
            "e2e_ms": throughput_ms,
            "p50": float(np.percentile(w, 50)),
            "p95": float(np.percentile(w, 95)),
            "p99": float(np.percentile(w, 99)),
            "min": float(w.min()), "cv": float(w.std() / max(w.mean(), 1e-9)),
            "cpu_p50": float(np.percentile(c, 50)),
            "hash": out_hash,
            "hash_match": (baseline_hash is None or out_hash == baseline_hash),
            "outputs": outs}


def run_verbose_capture() -> None:
    """独立子进程模式：VERBOSE 日志建 capture 会话跑一帧，验证 capture 是否真实生效。"""
    sess = make_sess((("ep.dml.enable_graph_capture", "1"),), log_sev=0)
    x = np.zeros((1, 3, MOGE_IN_H, MOGE_IN_W), np.float32)
    b = sess.io_binding()
    b.bind_input(sess.get_inputs()[0].name, "cpu", 0, x.dtype, x.shape,
                 x.ctypes.data)
    for o in sess.get_outputs():
        b.bind_output(o.name, "cpu", 0)
    sess.run_with_iobinding(b, None)
    b.copy_outputs_to_cpu()
    sess.run_with_iobinding(b, None)
    b.copy_outputs_to_cpu()
    print("VERBOSE-CAPTURE-RUN-DONE")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=120)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--verbose-capture", action="store_true")
    args = ap.parse_args()
    if args.verbose_capture:
        run_verbose_capture()
        return

    rng = np.random.default_rng(0)
    rng_val = rng.integers(0, 256, (MOGE_IN_H, MOGE_IN_W, 3),
                           dtype=np.uint8).astype(np.float32) / 255.0
    rng_val = rng_val.transpose(2, 0, 1)[None]

    conditions = [
        ("baseline_run",  (),                                "feed"),
        ("iobind_cpu",    (),                                "iobind"),
        ("capture_cpu",   (("ep.dml.enable_graph_capture", "1"),), "iobind"),
        ("spin_cpu",      (("ep.dml.enable_cpu_sync_spinning", "1"),), "iobind"),
        ("out_dml",       (),                                "iobind_noreadback"),
        ("in_dml",        (),                                "iobind_dml_in"),
    ]
    sampler = GpuSampler()
    baseline_hash = None
    results = []
    try:
        for name, configs, mode in conditions:
            try:
                sess = make_sess(configs)
            except Exception as e:
                print(f"[{name}] 会话创建失败: {type(e).__name__}: {e}", flush=True)
                results.append({"mode": name, "error": str(e)})
                continue
            try:
                t0 = time.perf_counter()
                r = bench(sess, mode, args.iters, args.warmup, rng_val,
                          sampler, baseline_hash)
                r["mode"] = name   # bench 返回的是内部模式名，这里覆盖为条件名
                g = sampler.window(t0, time.perf_counter())
                r["gpu_util_mean"] = g[0] if g else None
                r["vram_max_mb"] = g[1] if g else None
            except Exception as e:
                print(f"[{name}] 运行失败: {type(e).__name__}: {e}", flush=True)
                results.append({"mode": name, "error": str(e)})
                continue
            if name == "baseline_run":
                baseline_hash = r["hash"]
            results.append(r)
            print(f"[{name:14s}] 首跑{r['first_ms']:7.1f}ms "
                  f"wall p50={r['p50']:6.1f} (e2e={r['e2e_ms']:6.1f} "
                  f"loop={r['loop_wall_ms']:.0f} readback={r['readback_ms']:.0f}) "
                  f"min={r['min']:6.1f} cv={r['cv']:.3f} | "
                  f"cpu p50={r['cpu_p50']:6.1f}ms | gpu≈{r['gpu_util_mean']}% "
                  f"vram≤{r['vram_max_mb']}MB | hash={r['hash']} "
                  f"一致={r['hash_match']}", flush=True)
    finally:
        sampler.close()

    print("\n==== 判读 ====")
    by = {r["mode"]: r for r in results if "error" not in r}
    # capture 数值有效性：baseline vs capture 逐输出对比 + 换输入 B 验证重放吃新图
    if "capture_cpu" in by and "baseline_run" in by:
        base_outs = by["baseline_run"]["outputs"]
        cap_outs = by["capture_cpu"]["outputs"]
        print("==== capture 数值对比（输入 A，排空后读回）====")
        for i, (a, b) in enumerate(zip(base_outs, cap_outs)):
            d = float(np.abs(a.astype(np.float64) - b.astype(np.float64)).max())
            print(f"  out[{i}] max|Δ|={d:.3e}")
        try:
            bs = make_sess()
            cb = make_sess((("ep.dml.enable_graph_capture", "1"),))
            in_name = cb.get_inputs()[0].name
            b2 = (rng_val * 0.5 + 0.25).astype(np.float32)   # 输入 B
            ref_b = bs.run(None, {in_name: b2})
            xb = np.ascontiguousarray(b2)
            bd = cb.io_binding()
            bd.bind_input(in_name, "cpu", 0, xb.dtype, xb.shape, xb.ctypes.data)
            for o in cb.get_outputs():
                bd.bind_output(o.name, "cpu", 0)
            np.copyto(xb, b2)
            cb.run_with_iobinding(bd, None)
            time.sleep(0.3)
            got_b = bd.copy_outputs_to_cpu()
            dA = max(float(np.abs(a.astype(np.float64) - b.astype(np.float64)).max())
                     for a, b in zip(ref_b, got_b))
            print(f"  换输入B重放: max|Δ| vs baseline(B) = {dA:.3e}")
        except Exception as e:
            print(f"  换输入B重放失败: {type(e).__name__}: {e}")
    if "iobind_cpu" in by and "capture_cpu" in by:
        a, b = by["iobind_cpu"], by["capture_cpu"]
        print(f"capture: wall Δ{(b['p50']/a['p50']-1)*100:+.1f}%  "
              f"cpu Δ{(b['cpu_p50']/a['cpu_p50']-1)*100:+.1f}%")
    if "iobind_cpu" in by and "out_dml" in by:
        a, b = by["iobind_cpu"], by["out_dml"]
        print(f"读回同步+拷贝(输出绑dml): {a['p50']-b['p50']:+.1f}ms")
    if "iobind_cpu" in by and "in_dml" in by:
        a, b = by["iobind_cpu"], by["in_dml"]
        print(f"每帧输入上传(输入驻dml,诊断): {a['p50']-b['p50']:+.1f}ms")


if __name__ == "__main__":
    main()
