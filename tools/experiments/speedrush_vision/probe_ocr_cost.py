"""复现 RapidOCR 引擎的线程开销。

线索：sidecar 对局中 4 个重线程（各 ~0.88 核，3 个 py-spy 看不见）诞生于
19:19:28，而该时刻日志正是 RapidOCR 加载 det/cls/rec 三个 onnx 模型。
本探针按项目同参数建 RapidOCR 引擎，量空闲/推理时每线程 CPU。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.getcwd(), "tools/experiments/speedrush_vision"))
import numpy as np  # noqa: E402
from probe_thread_cpu import snapshot_threads  # noqa: E402


def mine():
    return snapshot_threads().get(os.getpid(), {})


def measure(label, dur, drive=None, hz=0.0):
    import threading
    stop = threading.Event()
    cnt = [0]
    if drive is not None:
        def loop():
            while not stop.is_set():
                try:
                    drive()
                    cnt[0] += 1
                except Exception as e:
                    print("   异常:", type(e).__name__, e)
                    break
                if hz > 0:
                    time.sleep(max(0.0, 1.0 / hz - 0.003))
        threading.Thread(target=loop, daemon=True).start()
    time.sleep(0.5)
    a = mine(); ta = time.monotonic()
    time.sleep(dur)
    b = mine(); tb = time.monotonic()
    stop.set()
    dt = tb - ta
    rows = []
    for tid, (u, k) in b.items():
        if tid in a:
            c = ((u + k) - (a[tid][0] + a[tid][1])) / dt
            if c > 0.01:
                rows.append((tid, c))
    rows.sort(key=lambda x: -x[1])
    tot = sum(c for _, c in rows)
    print(f"\n### {label}  进程合计 {tot:.2f} 核  线程数 {len(b)}  驱动 {cnt[0]} 次")
    for tid, c in rows[:12]:
        print(f"    tid={tid:7} {c:6.3f} 核")
    return tot


def main():
    from rapidocr import RapidOCR
    print("构造 RapidOCR 引擎（intra=4, inter=1, use_det=False, use_cls=False）…")
    eng = RapidOCR(params={
        "Global.use_det": False,
        "Global.use_cls": False,
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })
    print("引擎就绪，线程数:", len(mine()))
    measure("A. 引擎就绪后空闲", 10)

    img = (np.random.rand(48, 200, 3) * 255).astype(np.uint8)
    measure("B. 2Hz 反复识别", 15, drive=lambda: eng(img), hz=2)
    measure("C. 停止识别后空闲", 8)


if __name__ == "__main__":
    main()
