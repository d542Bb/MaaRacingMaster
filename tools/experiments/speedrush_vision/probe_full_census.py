"""speedrush 全量运行时探针（一次运行抓全部，事后离线筛选）。

设计原则（针对「不要反复跑」）：
  - **一次挂载、长时间窗口**：用户想跑几局跑几局，全程连续采样。
  - **不预设关注对象**：所有进程都记（进程级 CPU/RSS），所有"有 CPU 的进程"的
    每个线程都记（tid→核），不做角色假设，筛选交给离线分析。
  - **每线程元数据一并留存**：起始地址、创建时间、上下文切换数——用于事后
    判定"纯原生线程 / 自旋 / 归属模块"。
  - GPU 侧同步记 util/显存/温度/SM时钟/功耗 + 每进程显存占用。

免提权：线程数据走 NtQuerySystemInformation(SystemProcessInformation)，
内核返回全系统进程/线程表，每线程带 UserTime/KernelTime（elevated 目标也可读）。
"""
import argparse
import ctypes
import json
import struct
import subprocess
import sys
import time

import psutil

ntdll = ctypes.WinDLL("ntdll")
SystemProcessInformation = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

# 线程表内偏移（x64 SYSTEM_THREAD_INFORMATION，size 0x50）
T_KT, T_UT, T_CT = 0x00, 0x08, 0x10
T_START, T_TID = 0x20, 0x30
T_CTXSW, T_STATE, T_WAITR = 0x40, 0x44, 0x48
# 进程表内偏移（x64 SYSTEM_PROCESS_INFORMATION）
P_NTH, P_PID, P_THREADS = 0x04, 0x50, 0x100


def sys_snapshot() -> dict:
    """{pid: {"nthreads":n, "threads":{tid:{u,k,ct,start,ctxsw,state,waitr}}}}"""
    size = 1 << 22
    data = None
    for _ in range(8):
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong()
        st = ntdll.NtQuerySystemInformation(SystemProcessInformation, buf, size,
                                            ctypes.byref(ret))
        if st == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size, ret.value + 65536)
            continue
        if st != 0:
            raise OSError(f"NtQuerySystemInformation 0x{st & 0xffffffff:08X}")
        data = buf.raw[: ret.value]
        break
    if data is None:
        raise RuntimeError("扩容失败")

    out = {}
    off, n = 0, len(data)
    while off < n:
        nxt = struct.unpack_from("<I", data, off)[0]
        nth = struct.unpack_from("<I", data, off + P_NTH)[0]
        pid = struct.unpack_from("<Q", data, off + P_PID)[0]
        base = off + P_THREADS
        thr = {}
        for i in range(nth):
            t = base + i * 0x50
            thr[struct.unpack_from("<Q", data, t + T_TID)[0]] = {
                "u": struct.unpack_from("<q", data, t + T_UT)[0] / 1e7,
                "k": struct.unpack_from("<q", data, t + T_KT)[0] / 1e7,
                "ct": struct.unpack_from("<q", data, t + T_CT)[0] / 1e7,
                "sa": struct.unpack_from("<Q", data, t + T_START)[0],
                "cx": struct.unpack_from("<I", data, t + T_CTXSW)[0],
                "st": struct.unpack_from("<I", data, t + T_STATE)[0],
                "wr": struct.unpack_from("<I", data, t + T_WAITR)[0],
            }
        out[pid] = {"nthreads": nth, "threads": thr}
        if nxt == 0:
            break
        off += nxt
    return out


def gpu_snapshot() -> dict:
    try:
        q = ("utilization.gpu,memory.used,memory.total,temperature.gpu,"
             "clocks.sm,clocks.mem,power.draw,power.limit")
        r = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        vals = [v.strip() for v in r.stdout.strip().split(",")]
        keys = ["util_pct", "mem_used_mb", "mem_total_mb", "temp_c",
                "sm_clk_mhz", "mem_clk_mhz", "power_w", "power_limit_w"]
        g = dict(zip(keys, vals))
        r2 = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                             "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=5)
        g["compute_apps"] = [l.strip() for l in r2.stdout.strip().splitlines() if l.strip()]
        return g
    except Exception as e:
        return {"error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=1200.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-proc-cores", type=float, default=0.02,
                    help="进程级低于此核数不入账（仍然采样，只是不写，减体积）")
    ap.add_argument("--min-thr-cores", type=float, default=0.02,
                    help="线程级低于此核数不入账")
    ap.add_argument("--gpu-every", type=int, default=2, help="每 N 个样本采一次 GPU")
    a = ap.parse_args()

    ncpu = psutil.cpu_count(logical=True)
    me = psutil.Process().pid
    t0 = time.monotonic()

    # 进程元数据缓存：pid -> (create_time, name, cmdline)
    meta: dict[int, tuple] = {}

    def meta_of(pid: int):
        m = meta.get(pid)
        if m is not None:
            return m
        try:
            p = psutil.Process(pid)
            m = (p.create_time(), p.name() or "?", " ".join(p.cmdline() or [])[:220])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            m = (0.0, "?", "")
        meta[pid] = m
        return m

    prev = sys_snapshot()
    prev_t = time.monotonic()
    thread_meta_written = False
    fh = open(a.out, "w", encoding="utf-8")
    n = 0

    while time.monotonic() - t0 < a.duration:
        nxt = prev_t + a.interval
        slp = nxt - time.monotonic()
        if slp > 0:
            time.sleep(slp)
        now = time.monotonic()
        dt = max(1e-6, now - prev_t)
        cur = sys_snapshot()

        procs_out = []
        for pid, info in cur.items():
            pv = prev.get(pid)
            if pv is None:
                continue
            ctime, name, cmd = meta_of(pid)
            # 进程级 CPU（sum of thread deltas；pid 复用靠 create_time 兜底）
            pcores = 0.0
            thr_out = {}
            for tid, t in info["threads"].items():
                pt = pv["threads"].get(tid)
                if pt is None:
                    continue
                c = ((t["u"] + t["k"]) - (pt["u"] + pt["k"])) / dt
                if c <= 0:
                    continue
                pcores += c
                if c >= a.min_thr_cores:
                    thr_out[str(tid)] = round(c, 3)
            if pcores < a.min_proc_cores and not thr_out:
                continue
            rss = 0.0
            try:
                rss = psutil.Process(pid).memory_info().rss / 1048576.0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            procs_out.append({
                "pid": pid, "name": name, "cores": round(pcores, 3),
                "rss_mb": round(rss, 1), "nt": info["nthreads"],
                "cmd": cmd, "ctime": round(ctime, 2),
                "threads": dict(sorted(thr_out.items(), key=lambda kv: -kv[1])),
            })
        procs_out.sort(key=lambda r: -r["cores"])

        rec = {"t": round(now - t0, 2), "wall": round(time.time(), 2), "ncpu": ncpu,
               "nproc_total": len(cur), "procs": procs_out}
        if n % a.gpu_every == 0:
            rec["gpu"] = gpu_snapshot()
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()

        # 线程元数据快照（起始地址/创建时间/上下文切换）——只写一次全量
        if not thread_meta_written:
            tm = {}
            for pid, info in cur.items():
                for tid, t in info["threads"].items():
                    tm[str(tid)] = {"pid": pid, "sa": f"0x{t['sa']:012X}",
                                    "ct": round(t["ct"], 2)}
            fh.write(json.dumps({"t": round(now - t0, 2), "thread_meta": tm},
                                ensure_ascii=False) + "\n")
            fh.flush()
            thread_meta_written = True

        prev, prev_t = cur, now
        n += 1

    fh.close()
    print(f"[full-census] {n} 样本 / {time.monotonic() - t0:.0f}s → {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
