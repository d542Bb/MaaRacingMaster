# -*- coding: utf-8 -*-
"""speedrush 运行时全景采样：进程 / 线程 / GPU。

问：speedrush 真的在跑时，都有哪些进程和图像有关、开销多大、频率多快。

口径（全部外部观测，零产线侵入）：
- 进程级：psutil 全表快照，CPU% = (utime+stime) 增量 / 墙钟 / 逻辑核数 × 100，
  即"整机占用率"口径（单核跑满 = 100/核数）。
- 线程级：psutil ``Process.threads()`` 逐 tid 增量。Python 3.11 不写原生线程名
  （3.14 才默认写），故归因靠 **CPU 特征 + 首次出现顺序** 对照已知线程结构
  （主控制拍 20Hz / 深度 worker ~5Hz / HUD 0.5s 周期 / WGC 回调 / 框架线程）。
- GPU：nvidia-smi 查询。温度 / SM 时钟 / 功耗是「同一模型越跑越慢是否热降频」
  的直接判据，故一并入账。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_runtime_census.py \
        --watch --out "$APPDATA/MaaRacingMaster/data/speedrush/runtime_census.jsonl"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil

GPU_QUERY = "utilization.gpu,memory.used,temperature.gpu,clocks.sm,power.draw"
GPU_KEYS = ["util_pct", "mem_used_mb", "temp_c", "sm_clk_mhz", "power_w"]


def classify(name: str, cmdline: list[str]) -> str | None:
    """按角色归类；None = 不在关注组（只在 CPU 超门槛时入账）。

    角色依据（2026-10-08 核实）：
    - sidecar：shell 以 ``python -u -m maaracing_master.core.sidecar`` 启动
      （apps/MaaRacingMaster.Shell/MainWindow.xaml.cs:148），**全部图像处理在此进程内**；
    - shell：MaaRacingMaster.Shell.exe，GUI + sidecar 生命周期，不碰游戏画面；
    - emulator：游戏跑在 MuMu 安卓模拟器里（MuMuVMMHeadless 等）；
    - game：模拟器内的游戏本体（UE 打包名 *-Shipping.exe）。
    """
    nm = name.lower()
    cmd = " ".join(cmdline).lower()
    # sidecar 的 exe 名是 python.exe，只能靠命令行判定（工作区路径也叫 maaracing，
    # 故 shell 一律按 **exe 名** 判，不看命令行——否则 node/python 全被误吞）
    if "maaracing_master.core.sidecar" in cmd:
        return "sidecar"
    if "maaracing" in nm:
        return "shell"
    if "shipping" in nm:
        return "game"
    if "mumu" in nm or "vmm" in nm or "nemu" in nm or "mumu" in cmd:
        return "emulator"
    return None


def nvidia_smi() -> dict:
    """一次 GPU 快照；失败返回 {"error": ...}，不抛。"""
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3)
        line = (r.stdout or "").strip().splitlines()
        if not line:
            return {}
        vals = [v.strip() for v in line[0].split(",")]
        return dict(zip(GPU_KEYS, vals))
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)}


def snapshot() -> dict:
    """全表进程快照（原始累计量，增量在调用侧算）。"""
    procs = {}
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if p.pid == 0:          # System Idle Process：其"CPU"是空闲率，语义相反
            continue
        try:
            with p.oneshot():
                ct = p.cpu_times()
                name = p.info["name"] or "?"
                cmdline = p.info["cmdline"] or []
                role = classify(name, cmdline)
                procs[p.pid] = {
                    "name": name,
                    "cpu": ct.user + ct.system,
                    "rss_mb": p.memory_info().rss / 1048576.0,
                    "role": role,
                    # 关注组才留命令行摘要（判重名进程身份用；全表留会撑爆体积）
                    "cmd": " ".join(cmdline)[:90] if role else "",
                    # create_time 用于识别 **pid 复用**：Windows 上 pid 回收快，
                    # 旧进程的累计 CPU 会被算进新进程头上（首版爆出 6510416%）。
                    "ct": p.create_time(),
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def thread_snapshot(pid: int) -> dict:
    """逐 tid 累计 CPU 秒；失败记因返回空（权限/退出竞态）。"""
    try:
        return {t.id: t.user_time + t.system_time
                for t in psutil.Process(pid).threads()}
    except Exception as exc:  # noqa: BLE001 —— 诊断期：记全因，不静默
        THR_ERR[pid] = f"{type(exc).__name__}: {exc}"
        return {}


THR_ERR: dict[int, str] = {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="输出 JSONL 路径")
    ap.add_argument("--interval", type=float, default=1.0, help="采样周期（秒）")
    ap.add_argument("--duration", type=float, default=1800.0, help="最长时长（秒）")
    ap.add_argument("--watch", action="store_true",
                    help="等待 MRA 相关进程出现才开始记（先启动本脚本、再启动 MRA）")
    ap.add_argument("--min-cpu", type=float, default=1.0,
                    help="非关注组进程的入账 CPU%% 门槛")
    a = ap.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ncpu = psutil.cpu_count(logical=True) or 1

    # 预热：首次 cpu_times 无基准
    prev = snapshot()
    prev_t = time.monotonic()
    prev_thr: dict[int, dict] = {}
    t0 = time.monotonic()

    if a.watch:
        print(f"[census] 等待 sidecar 进程出现（最长 {a.duration:.0f}s）…", flush=True)
        while time.monotonic() - t0 < a.duration:
            if any(v["role"] == "sidecar" for v in snapshot().values()):
                break
            time.sleep(1.0)
        else:
            print("[census] 超时未见 sidecar，退出", flush=True)
            return 2
        print("[census] 检测到 sidecar，开始采样", flush=True)
        prev = snapshot()
        prev_t = time.monotonic()

    n = 0
    with out.open("w", encoding="utf-8") as fh:
        while time.monotonic() - t0 < a.duration:
            t_sleep = time.monotonic() + a.interval
            now = time.monotonic()
            # dt 下限：watch 退出后第一轮的 prev_t 与本轮 now 几乎同时（快照间隙），
            # 不设下限会把微小增量除成天文数字（首版实测爆出 390625%）。
            dt = max(a.interval * 0.5, now - prev_t)
            cur = snapshot()

            rows = []
            for pid, c in cur.items():
                p0 = prev.get(pid)
                same = p0 is not None and p0["ct"] == c["ct"]   # 同一进程实例
                cpu_pct = ((c["cpu"] - p0["cpu"]) / dt / ncpu * 100.0) if same else 0.0
                if not c["role"] and cpu_pct < a.min_cpu:
                    continue
                rows.append({"pid": pid, "name": c["name"], "role": c["role"],
                             "cpu": round(cpu_pct, 1),
                             "rss_mb": round(c["rss_mb"], 1),
                             "cmd": c["cmd"]})
            rows.sort(key=lambda r: -r["cpu"])

            # 线程级：只对 sidecar 做（图像处理全在它进程内，见 classify 注释）
            thr_out = {}
            thr_count = {}
            for r in rows:
                if r["role"] != "sidecar":
                    continue
                cur_t = thread_snapshot(r["pid"])
                thr_count[str(r["pid"])] = len(cur_t)
                # 基准存在性按 **键在不在** 判，不按"值是否为空"——sidecar 启动首轮
                # threads() 可能返回空，空基准会让后续每轮都误判成"首轮"而永不产出
                # （首版实机 threads 全空的真凶）。
                p_t = prev_thr.get(r["pid"])
                prev_thr[r["pid"]] = cur_t
                if p_t is None:
                    continue
                d = {}
                for tid, v in cur_t.items():
                    if tid in p_t:
                        pct = (v - p_t[tid]) / dt / ncpu * 100.0
                        if pct >= 0.01:
                            d[str(tid)] = round(pct, 2)
                if d:
                    thr_out[str(r["pid"])] = dict(
                        sorted(d.items(), key=lambda kv: -kv[1]))

            rec = {"t": round(now - t0, 2), "wall": round(time.time(), 2),
                   "gpu": nvidia_smi(),
                   "procs": rows, "threads": thr_out, "thr_n": thr_count,
                   "thr_err": dict(THR_ERR)}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            n += 1

            prev, prev_t = cur, now
            time.sleep(max(0.0, t_sleep - time.monotonic()))
    print(f"[census] 采样 {n} 条 → {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
