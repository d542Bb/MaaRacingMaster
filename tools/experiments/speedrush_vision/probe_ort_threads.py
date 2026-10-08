"""验证 onnxruntime 会话是否凭空产生自旋线程（纯原生 CPU）。

sidecar 实测：对局中进程吃 ~4.2 核，而 Python 线程只占 ~1.3 核；
多出 3 个纯原生线程各烧 ~0.88 核。本探针在本进程内复现会话创建+推理，
用 NtQuerySystemInformation 读自己每线程 CPU，看线程从哪来、烧多少。
"""
import argparse
import os
import sys
import time

import numpy as np
import onnxruntime as ort

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_thread_cpu import snapshot_threads  # noqa: E402

PERCEPTION = "maaracing_master/plugins/speedrush/resources/onnx/perception/model.onnx"


def snapshot_mine() -> dict:
    return snapshot_threads().get(os.getpid(), {})


def measure(label: str, dur: float, drive=None, rate_hz: float = 0.0):
    """dur 秒内测本进程每线程 CPU（核）。drive() 在另一线程按 rate_hz 调用。"""
    import threading
    stop = threading.Event()
    calls = [0]

    def worker():
        while not stop.is_set():
            if drive is not None:
                drive()
                calls[0] += 1
            if rate_hz > 0:
                time.sleep(max(0.0, 1.0 / rate_hz - 0.001))

    t = None
    if drive is not None:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
    time.sleep(0.3)  # 让线程起来
    a = snapshot_mine()
    ta = time.monotonic()
    time.sleep(dur)
    b = snapshot_mine()
    tb = time.monotonic()
    if t:
        stop.set()
        t.join(timeout=2)
    dt = tb - ta
    rows = []
    tot = 0.0
    for tid, (u, k) in b.items():
        if tid in a:
            c = ((u + k) - (a[tid][0] + a[tid][1])) / dt
            if c > 0.01:
                rows.append((tid, c))
                tot += c
    rows.sort(key=lambda x: -x[1])
    print(f"\n### {label}   进程合计 {tot:.2f} 核   线程数 {len(b)}   drive 调用 {calls[0]} 次")
    for tid, c in rows[:14]:
        print(f"    tid={tid:7} {c:6.3f} 核")
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intra", type=int, default=4)
    ap.add_argument("--inter", type=int, default=4)
    a = ap.parse_args()

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = a.intra
    so.inter_op_num_threads = a.inter
    print(f"建会话: DML, intra={a.intra} inter={a.inter}")
    sess = ort.InferenceSession(
        PERCEPTION, sess_options=so,
        providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    print("输入:", inp.name, inp.shape, inp.type)

    measure("A. 建会话后空闲（不推理）", 8)

    shape = [d if isinstance(d, int) else 1 for d in inp.shape]
    x = np.zeros(shape, np.float32)

    def run_once():
        sess.run(None, {inp.name: x})

    measure("B. 10Hz 推理", 10, drive=run_once, rate_hz=10)

    def idle_noop():
        pass

    measure("C. 停止推理后空闲", 8)


if __name__ == "__main__":
    main()
