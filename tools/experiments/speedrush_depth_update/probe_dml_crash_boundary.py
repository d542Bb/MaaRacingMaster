#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DML 崩溃边界 + forward 分段计时探针（自包含，不 import 业务码）。

目的：不再靠一把大锁把"提交+等+读回"整段圈住，而是实测——
  A) forward 那 ~54ms 里，多少是 run_with_iobinding（提交/等 GPU），
     多少是 copy_outputs_to_cpu（把结果读回 CPU）。
  B) 两个 DML 会话在哪些并发组合下会段错误（崩溃边界），
     从而定出"必须串行"的最小段。

崩溃类模式各跑一个独立子进程（崩了只死子进程，不殃及驱动/父进程）。

跑法：
  计时:      python probe_dml_crash_boundary.py
  崩溃扫描:  python probe_dml_crash_boundary.py --drive
  单模式:    python probe_dml_crash_boundary.py --pattern P1   （供 --drive 内部调用）
"""
from __future__ import annotations
import sys, os, time, threading, subprocess, statistics as st
import numpy as np
import onnxruntime as ort

REPO = "D:/maaracing_assistant"
MOGE = os.path.join(REPO, "maaracing_master/plugins/speedrush/resources/onnx/depth/moge2_vits_static_336x598_t1032_q4f16.onnx")
YOLO = os.path.join(REPO, "maaracing_master/plugins/speedrush/resources/onnx/perception/model.onnx")
PROV = ["DmlExecutionProvider", "CPUExecutionProvider"]


def _moge_session(h=720, w=1280):
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"), (1, h, w)):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(MOGE, so, providers=PROV)


def _yolo_session():
    so = ort.SessionOptions()
    return ort.InferenceSession(YOLO, so, providers=PROV)


def _feed(sess, h, w):
    inp = sess.get_inputs()
    name = inp[0].name
    x = np.random.rand(1, 3, h, w).astype(np.float32)
    f = {name: x}
    for i in inp[1:]:
        if "int64" in i.type:
            f[i.name] = np.array(1032, np.int64)
    return f


def timing():
    print("=== A. forward 分段计时（单线程，DML）===")
    ms = _moge_session()
    h, w = 720, 1280
    feed = _feed(ms, h, w)
    iname = ms.get_inputs()[0].name
    run_t, copy_t, plain_t = [], [], []
    for it in range(12):
        # io-binding 路径（同生产 forward）
        b = ms.io_binding()
        xb = np.ascontiguousarray(feed[iname])
        b.bind_output(ms.get_outputs()[0].name, "cpu", 0)
        for o in ms.get_outputs()[1:]:
            b.bind_output(o.name, "cpu", 0)
        b.bind_input(iname, "cpu", 0, xb.dtype, xb.shape, xb.ctypes.data)
        t0 = time.perf_counter()
        ms.run_with_iobinding(b, None)
        t1 = time.perf_counter()
        out = b.copy_outputs_to_cpu()
        t2 = time.perf_counter()
        if it >= 2:                       # 跳过预热
            run_t.append((t1 - t0) * 1e3)
            copy_t.append((t2 - t1) * 1e3)
        # 普通 run（YOLO 用的调用式样）计时
        t0 = time.perf_counter()
        ms.run(None, feed)
        t1 = time.perf_counter()
        if it >= 2:
            plain_t.append((t1 - t0) * 1e3)
    print(f"  run_with_iobinding   p50={st.median(run_t):5.1f}  (提交+等GPU完成)")
    print(f"  copy_outputs_to_cpu  p50={st.median(copy_t):5.1f}  (结果读回CPU)")
    print(f"  普通 run(None) 合计  p50={st.median(plain_t):5.1f}")
    print("  注：run_with_iobinding 是否含 GPU 等待——若它很长而 copy 很短，"
          "说明 GPU 完成已在 run 内同步等待；若 run 短 copy 长，则读回里藏着等待。")


def _validate_sessions():
    """先各跑一次，确认两会话在 DML 上真能出结果（否则并发测试是假阴性）。"""
    ms = _moge_session(); ys = _yolo_session()
    mf = _feed(ms, 720, 1280)
    yf = _feed(ys, *_shape_hw(ys))
    mo = ms.run(None, mf); yo = ys.run(None, yf)
    print("validate  moge_out:", [f"{o.shape}" for o in mo],
          " yolo_in:", yf[ys.get_inputs()[0].name].shape,
          " yolo_out:", [f"{o.shape}" for o in yo])
    return mf, yf


def _pattern(name, seconds=6.0):
    """并发模式；计数器证明两次 run 真在同时高频跑，异常一律捕获上报。"""
    ms = _moge_session(); ys = _yolo_session()
    mf = _feed(ms, 720, 1280)
    yf = _feed(ys, *_shape_hw(ys))
    stop = threading.Event()
    cnt = {"moge": 0, "yolo": 0, "copy": 0}
    errs: list = []
    lk = threading.Lock()

    def bump(k):
        with lk:
            cnt[k] += 1

    def moge_run():
        try:
            while not stop.is_set():
                ms.run(None, mf); bump("moge")
        except BaseException as e:
            errs.append(("moge_run", repr(e)))

    def yolo_run():
        try:
            while not stop.is_set():
                ys.run(None, yf); bump("yolo")
        except BaseException as e:
            errs.append(("yolo_run", repr(e)))

    def moge_run_then_copy():
        try:
            while not stop.is_set():
                b = ms.io_binding()
                xb = np.ascontiguousarray(mf[ms.get_inputs()[0].name])
                for o in ms.get_outputs():
                    b.bind_output(o.name, "cpu", 0)
                b.bind_input(ms.get_inputs()[0].name, "cpu", 0,
                             xb.dtype, xb.shape, xb.ctypes.data)
                ms.run_with_iobinding(b, None); bump("moge")
                time.sleep(0.002)         # 让另一线程的 run 有机会插进来
                b.copy_outputs_to_cpu(); bump("copy")
        except BaseException as e:
            errs.append(("moge_run_copy", repr(e)))

    def cpu_only():
        a = np.random.rand(2000, 2000).astype(np.float32)
        try:
            while not stop.is_set():
                _ = a @ a
        except BaseException as e:
            errs.append(("cpu_only", repr(e)))

    if name == "P_serial":
        try:
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < seconds:
                ms.run(None, mf); bump("moge"); ys.run(None, yf); bump("yolo")
        except BaseException as e:
            errs.append(("serial", repr(e)))
    elif name == "P_run_run":
        ts = [threading.Thread(target=moge_run), threading.Thread(target=yolo_run)]
        [t.start() for t in ts]; time.sleep(seconds); stop.set(); [t.join(timeout=3) for t in ts]
    elif name == "P_run_vs_runio":
        ts = [threading.Thread(target=moge_run_then_copy), threading.Thread(target=yolo_run)]
        [t.start() for t in ts]; time.sleep(seconds); stop.set(); [t.join(timeout=3) for t in ts]
    elif name == "P_run_vs_cpu":
        ts = [threading.Thread(target=moge_run), threading.Thread(target=cpu_only)]
        [t.start() for t in ts]; time.sleep(seconds); stop.set(); [t.join(timeout=3) for t in ts]
    print(f"pattern {name}: survived  runs={cnt}  errs={errs if errs else '无'}")


def _shape_hw(sess):
    s = sess.get_inputs()[0].shape
    if isinstance(s[2], int) and isinstance(s[3], int):
        return (s[2], s[3])
    # 动态维：perception 常见 640，取元数据里能定出的，兜底 384x640
    return (384, 640)


def sweep():
    """run 时间 vs 输入分辨率——量化"DML 内部把一次 run 变小"还剩多少空间。"""
    print("=== C. MoGe run 时间随输入分辨率（同权重、不重训、不换后端）===")
    print(f"{'输入 WxH':>12}{'约缩放':>8}{'run_ms p50':>12}")
    base = 1280
    for scale in (1.0, 0.75, 0.625, 0.5, 0.375):
        w = int(round(base * scale / 32) * 32)      # 对齐 32（ViT patch 友好）
        h = int(round(720 * scale / 32) * 32)
        try:
            ms = _moge_session(h, w)
            feed = _feed(ms, h, w)
        except Exception as e:
            print(f"{w}x{h:>6}  建会话/喂数失败：{type(e).__name__}: {str(e)[:80]}")
            continue
        ts = []
        for it in range(8):
            t0 = time.perf_counter()
            ms.run(None, feed)
            if it >= 2:
                ts.append((time.perf_counter() - t0) * 1e3)
        print(f"{w}x{h:>6}{scale:>8.2f}{st.median(ts):>12.1f}")


def _sess(with_capture: bool):
    so = ort.SessionOptions()
    if with_capture:
        so.add_session_config_entry("ep.dml.enable_graph_capture", "1")
    for name, dim in zip(("batch_size", "height", "width"), (1, 720, 1280)):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(MOGE, so, providers=PROV)


def _run_io(sess, x):
    """io-binding 跑一帧（每帧重绑输入指针——这是避开"冻结输入"的前提，同生产 forward）。"""
    name = sess.get_inputs()[0].name
    xb = np.ascontiguousarray(x)
    b = sess.io_binding()
    b.bind_input(name, "cpu", 0, xb.dtype, xb.shape, xb.ctypes.data)
    for o in sess.get_outputs():
        b.bind_output(o.name, "cpu", 0)
    t0 = time.perf_counter()
    sess.run_with_iobinding(b, None)
    t1 = time.perf_counter()
    out = b.copy_outputs_to_cpu()
    return out[0], (t1 - t0) * 1e3


def capture_probe():
    """DML graph capture 正确性命门：喂不同帧输出会不会变 + 对得上非 capture 参考 + 提速否。"""
    print("=== D. DML graph capture 正确性 + 提速（当前已折叠 q4f16 模型）===", flush=True)
    rng = np.random.default_rng(7)
    A = (rng.random((1, 3, 720, 1280))).astype(np.float32)
    B = (rng.random((1, 3, 720, 1280))).astype(np.float32)   # 明显不同的另一帧
    print("  [1] 建 capture 会话…", flush=True)
    cap = _sess(True)
    print("  [2] 建非 capture 参考会话…", flush=True)
    base = _sess(False)
    print("  [3] capture 预热 run…", flush=True)
    _run_io(cap, A)
    _run_io(cap, A)
    print("  [4] 喂 A 帧…", flush=True)
    oA, rA = _run_io(cap, A)
    print("  [5] 喂 B 帧（换输入，测是否冻结/崩）…", flush=True)
    oB, rB = _run_io(cap, B)
    print("  [6] 非 capture 参考跑 A/B…", flush=True)
    roA, _ = _run_io(base, A)
    roB, _ = _run_io(base, B)

    def diff(a, b):
        fa = np.isfinite(a) & np.isfinite(b)
        return float(np.max(np.abs(a[fa] - b[fa]))) if fa.any() else float("nan")
    frozen = diff(oA, oB)                       # 近 0 → 喂不同帧输出不变 = 冻结输入（坏）
    match_ref = diff(oA, roA)                   # 与非 capture 参考差 → capture 算错（坏）
    runs = []
    for _ in range(10):
        _, t = _run_io(cap, A if _ % 2 else B); runs.append(t)
    print(f"  run p50 (capture)  = {st.median(runs):.1f}ms   （非 capture 基线约 39.8ms）")
    print(f"  喂不同帧输出差 = {frozen:.3e}  → {'★冻结输入(capture 不可用)' if frozen < 1e-6 else '随帧变化(未冻结)'}")
    print(f"  capture vs 参考 输出差 = {match_ref:.3e}  → {'数值一致' if match_ref < 1e-2 else '★与不 capture 结果不一致(capture 改变数值)'}")


def drive():
    print("=== B. DML 崩溃边界（各模式独立子进程；先 validate 再并发）===")
    _validate_sessions()
    print()
    print(f"{'模式':<16}{'退出码':>8}   并发计数与判读")
    pats = {
        "P_serial":      "两会话同线程串行 run（无并发）——应当安全",
        "P_run_run":     "两线程并发 session.run（生产不加锁场景）——若双方 runs 均≫0 且未崩=并发可行",
        "P_run_vs_runio": "一线程 run+（延时后）copy 与另一线程 run 并发——测读回能否露在锁外",
        "P_run_vs_cpu":  "一线程 run 与另一线程纯 CPU matmul 并发——测是否任何并发都崩",
    }
    for p, desc in pats.items():
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--pattern", p],
                           capture_output=True, text=True, timeout=40)
        rc = r.returncode
        line = next((l for l in (r.stdout or "").splitlines() if "pattern" in l), "")
        crashed = (rc != 0)
        tag = ("崩溃(该并发必须串行)" if crashed else
               ("安全" if "survived" in line else f"未确认 rc={rc}"))
        print(f"{p:<16}{rc:>8}   {desc}")
        print(f"{'':16}{'':8}   → {tag}  |  {line}")
        if crashed:
            tail = (r.stderr or "").strip().splitlines()[-3:]
            print(f"{'':16}{'':8}     stderr: {' | '.join(tail)}")


if __name__ == "__main__":
    if "--drive" in sys.argv:
        drive()
    elif "--capture" in sys.argv:
        capture_probe()
    elif "--sweep" in sys.argv:
        sweep()
    elif "--pattern" in sys.argv:
        _pattern(sys.argv[sys.argv.index("--pattern") + 1])
    else:
        timing()
