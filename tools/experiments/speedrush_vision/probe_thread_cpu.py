"""sidecar 每线程 CPU 归属探针（免提权）。

背景：MRA 以管理员权限启动，sidecar 继承 elevated。此时
  - psutil.Process.threads() 静默返回空（不报错，会误导结论）
  - OpenThread / GetThreadTimes 一律 err=5（拒绝访问）
  - py-spy 只能看见 Python 线程，看不到 BLAS/OpenCV/onnxruntime 的原生线程池
     （受控实验：psutil 1.45 核 vs py-spy 1.02 核，漏掉的全在原生线程）

本探针改用 NtQuerySystemInformation(SystemProcessInformation)：内核返回全系统
进程/线程表，每线程带 UserTime/KernelTime，**非提权即可读 elevated 进程**。
逐 tid 累计量差分 → 每线程实占核数。配合 py-spy 的 tid→线程名，即可把
"sidecar 吃几个核"精确落到具体线程。
"""
import argparse
import ctypes
import json
import struct
import sys
import time

import psutil

ntdll = ctypes.WinDLL("ntdll")
SystemProcessInformation = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


def _query_raw() -> bytes:
    size = 1 << 22
    for _ in range(8):
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong()
        st = ntdll.NtQuerySystemInformation(
            SystemProcessInformation, buf, size, ctypes.byref(ret))
        if st == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size, ret.value + 65536)
            continue
        if st != 0:
            raise OSError(f"NtQuerySystemInformation 失败 0x{st & 0xffffffff:08X}")
        return buf.raw[: ret.value]
    raise RuntimeError("缓冲区扩容失败")


def snapshot_threads() -> dict:
    """{pid: {tid: (user_s, kernel_s)}}，全系统一次快照。"""
    data = _query_raw()
    out: dict[int, dict[int, tuple]] = {}
    off, n = 0, len(data)
    while off < n:
        nextoff = struct.unpack_from("<I", data, off)[0]
        nthreads = struct.unpack_from("<I", data, off + 4)[0]
        pid = struct.unpack_from("<Q", data, off + 0x50)[0]
        base = off + 0x100
        d: dict[int, tuple] = {}
        for i in range(nthreads):
            t = base + i * 0x50
            kt = struct.unpack_from("<q", data, t)[0]
            ut = struct.unpack_from("<q", data, t + 8)[0]
            tid = struct.unpack_from("<Q", data, t + 0x30)[0]
            d[tid] = (ut / 1e7, kt / 1e7)
        out[pid] = d
        if nextoff == 0:
            break
        off += nextoff
    return out


def classify(name: str, cmdline: list[str]) -> str | None:
    """进程角色识别（与 probe_runtime_census 同口径）。"""
    cmd = " ".join(cmdline)
    hay = (name + " " + cmd).lower()
    # sidecar 真实解释器与 venv 启动器都带模块名；启动器 CPU≈0，无碍
    if "maaracing_master.core.sidecar" in hay or "maaracing_master\\core\\sidecar" in hay:
        return "sidecar"
    if name.lower() == "maaracingmaster.shell.exe":
        return "shell"
    # 游戏本体命令行含 --MuMuLauncher，须先于模拟器判定
    if "shipping" in hay or "g112-win64" in hay:
        return "game"
    if "mumu" in hay or "nemu" in hay or "vmm" in hay:
        return "emulator"
    return None


def find_targets(want: str) -> dict:
    """返回 {role: [psutil.Process,...]}。"""
    roles: dict[str, list] = {}
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            role = classify(p.info["name"] or "", p.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not role:
            continue
        if want != "all" and role != want:
            continue
        roles.setdefault(role, []).append(p)
    return roles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="sidecar", help="sidecar|shell|game|emulator|all")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=300.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    nproc = psutil.cpu_count(logical=True)
    t0 = time.monotonic()
    fh = open(a.out, "w", encoding="utf-8")
    prev = snapshot_threads()
    prev_t = time.monotonic()
    prev_proc = {}
    for role, procs in find_targets(a.role).items():
        for p in procs:
            try:
                ct = p.cpu_times()
                prev_proc[p.pid] = (ct.user + ct.system, p.create_time(), role, p.info["name"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    n = 0
    while time.monotonic() - t0 < a.duration:
        nxt = prev_t + a.interval
        slp = nxt - time.monotonic()
        if slp > 0:
            time.sleep(slp)
        now = time.monotonic()
        dt = max(1e-6, now - prev_t)
        cur = snapshot_threads()
        cur_proc = {}
        for role, procs in find_targets(a.role).items():
            for p in procs:
                try:
                    ct = p.cpu_times()
                    cur_proc[p.pid] = (ct.user + ct.system, p.create_time(), role, p.info["name"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        procs_out = []
        for pid, (c, ctime, role, name) in cur_proc.items():
            pc = prev_proc.get(pid)
            # create_time 必须一致，否则是 pid 复用（Windows 回收快）
            if pc and abs(pc[1] - ctime) < 0.001:
                cores = (c - pc[0]) / dt
            else:
                cores = 0.0
            thr = {}
            pv = prev.get(pid, {})
            cv = cur.get(pid, {})
            for tid, (u, k) in cv.items():
                if tid in pv:
                    cu = (u + k) - (pv[tid][0] + pv[tid][1])
                    if cu > 1e-4:
                        thr[str(tid)] = round(cu / dt, 3)   # 核
            procs_out.append({"pid": pid, "role": role, "name": name,
                              "cores": round(cores, 3),
                              "nthreads": len(cv),
                              "threads": dict(sorted(thr.items(), key=lambda kv: -kv[1]))})

        rec = {"t": round(now - t0, 2), "wall": round(time.time(), 2),
               "ncpu": nproc, "procs": procs_out}
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        prev, prev_t, prev_proc = cur, now, cur_proc
        n += 1

    fh.close()
    print(f"[thread-cpu] {n} 条样本 → {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
