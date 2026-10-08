"""验证：多个 DML 会话并存（+串行锁）是否触发原生自旋线程。

sidecar 有 3 个 DML 会话（depth / yolo / ocr），实机对局中进程吃 4.33 核，
其中 4 个线程各 ~0.88 核，且 3 个是 py-spy 完全看不到的纯原生线程。
单会话测试（YOLO 20Hz）只有 0.12 核——所以怀疑点在「多会话并存」。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.getcwd(), "tools/experiments/speedrush_vision"))
import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from probe_thread_cpu import snapshot_threads  # noqa: E402

PERC = "maaracing_master/plugins/speedrush/resources/onnx/perception/model.onnx"
DEPTH = "maaracing_master/plugins/speedrush/resources/onnx/depth/moge2_vits_static_336x598_t1032_q4f16.onnx"


def mine():
    return snapshot_threads().get(os.getpid(), {})


def build(path, intra, inter):
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if intra:
        so.intra_op_num_threads = intra
    if inter:
        so.inter_op_num_threads = inter
    return ort.InferenceSession(path, sess_options=so,
                                providers=["DmlExecutionProvider", "CPUExecutionProvider"])


def make_input(sess):
    inp = sess.get_inputs()[0]
    shape = [d if isinstance(d, int) and d > 0 else 1 for d in inp.shape]
    return inp.name, np.random.rand(*shape).astype(np.float32)


def measure(label, dur, threads_to_run, stop):
    for t in threads_to_run:
        t.start()
    time.sleep(0.5)
    a = mine(); ta = time.monotonic()
    time.sleep(dur)
    b = mine(); tb = time.monotonic()
    stop.set()
    for t in threads_to_run:
        t.join(timeout=3)
    dt = tb - ta
    rows = []
    for tid, (u, k) in b.items():
        if tid in a:
            c = ((u + k) - (a[tid][0] + a[tid][1])) / dt
            if c > 0.01:
                rows.append((tid, c))
    rows.sort(key=lambda x: -x[1])
    tot = sum(c for _, c in rows)
    print(f"\n### {label}  进程合计 {tot:.2f} 核  线程数 {len(b)}")
    for tid, c in rows[:12]:
        print(f"    tid={tid:7} {c:6.3f} 核")
    return tot


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "multi"
    print("可用 providers:", ort.get_available_providers())
    lock = threading.Lock()
    sessions = []
    if mode == "multi":
        sd = build(DEPTH, 0, 0)      # 深度：默认线程数
        sy = build(PERC, 4, 4)       # YOLO：intra/inter=4
        sessions = [("depth", sd, 5.8), ("yolo", sy, 18.5)]
    else:
        sessions = [("yolo", build(PERC, 4, 4), 18.5)]
    for nm, s, _ in sessions:
        print(f"  {nm}: {s.get_providers()}")
        n, x = make_input(s)
        s._p = (n, x)
        s.run(None, {n: x})  # 预热

    stop = threading.Event()
    ctr = {}

    def worker(nm, s, hz):
        n, x = s._p
        ctr[nm] = 0
        while not stop.is_set():
            with lock:                      # 复刻 dml_lock：同刻只有一次 run
                s.run(None, {n: x})
            ctr[nm] += 1
            time.sleep(max(0.0, 1.0 / hz - 0.002))

    ths = [threading.Thread(target=worker, args=(nm, s, hz), daemon=True)
           for nm, s, hz in sessions]
    measure(f"{mode} 会话并存（{len(sessions)} 会话，共享锁）", 15, ths, stop)
    print("   各会话调用次数:", ctr)


if __name__ == "__main__":
    main()
