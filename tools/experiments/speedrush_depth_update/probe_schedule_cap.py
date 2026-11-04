"""对局 trace 的排期体检：撞锁相位、每次前向压住几拍、深度产出间隔与结果年龄。

用法（.venv python）：
    probe_schedule_cap.py --root <用户数据目录>/logs --sessions 5 --out sched.txt
不传 --root 时用 %APPDATA%/MaaRacingMaster/logs。输出为 ASCII，避免控制台编码问题。

口径：
- 前向窗 = [开工 + 预处理, 开工 + 预处理 + forward]，由 take 侧的分段计时反推
  （t_start = 消费拍时刻 − pub_ms − dgeo_ms）；控制拍的时间用帧时间戳，与真实
  取锁时刻最多差一个采集间隔（≤26ms），相位统计按此精度理解。
- "压住几拍"只数落在前向窗内的控制拍，不含等待造成的相邻拍间接影响。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np


def pct(a) -> str:
    a = [x for x in a if isinstance(x, (int, float))]
    if not a:
        return "-"
    a = np.asarray(a, float)
    return (f"p50={np.percentile(a,50):6.1f} p90={np.percentile(a,90):6.1f} "
            f"p99={np.percentile(a,99):6.1f} max={a.max():7.1f}")


def load(path: Path):
    ticks, news = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("dt") is None or "ts_ns" not in r:
            continue
        ticks.append(r)
        if r.get("dgeo_new"):
            news.append(r)
    return ticks, news


def analyze(path: Path) -> list[str]:
    ticks, news = load(path)
    out = [f"=== {path.parent.parent.name}  ticks={len(ticks)} "
           f"readings={len(news)} ==="]
    if len(ticks) < 50 or len(news) < 20:
        return out + ["sample too small"]

    tick_t = np.array([r["ts_ns"] / 1e6 for r in ticks], float)
    tick_w = np.array([r.get("yolo_wait_ms") or 0.0 for r in ticks], float)
    dts = np.array([r["dt"] for r in ticks], float) * 1000.0
    out.append(f"tick_dt        {pct(dts)}")
    out.append(f"  on-grid 50+-2ms={100*(np.abs(dts-50)<2).mean():.0f}%  "
               f">60ms={100*(dts>60).mean():.0f}%  >100ms={100*(dts>100).mean():.0f}%")
    out.append(f"yolo_wait      {pct(tick_w)}")
    out.append(f"  blocked >2ms={100*(tick_w>2).mean():.0f}%  "
               f">20ms={100*(tick_w>20).mean():.0f}%  "
               f"cum_delay={tick_w.sum()/ (dts.sum()/1000):.0f}ms/s")

    fw = []
    for r in news:
        seg = r.get("dgeo_seg") or {}
        st = seg.get("stages") or {}
        pub, dg = seg.get("pub_ms"), r.get("dgeo_ms")
        pre, fwd = st.get("pre"), st.get("forward")
        if None in (pub, dg, pre, fwd):
            continue
        t_consume = r["ts_ns"] / 1e6
        a = t_consume - float(pub) - float(dg) + float(pre)
        fw.append((a, a + float(fwd)))
    if len(fw) >= 20:
        fw = np.array(fw, float)
        idx = np.searchsorted(fw[:, 0], tick_t, side="right") - 1
        inside = np.zeros(len(tick_t), bool)
        for i, j in enumerate(idx):
            if j >= 0 and fw[j, 0] <= tick_t[i] <= fw[j, 1]:
                inside[i] = True
        out.append(f"forward_win    n={len(fw)}  dur {pct(fw[:,1]-fw[:,0])}")
        out.append(f"  ticks_inside={100*inside.mean():.0f}%  "
                   f"wait_inside_mean={tick_w[inside].mean() if inside.any() else 0:.1f}  "
                   f"wait_outside_mean={tick_w[~inside].mean() if (~inside).any() else 0:.1f}")
        h = np.histogram(tick_w[inside], bins=[0, 10, 20, 30, 40, 54, 1e9])[0]
        out.append("  wait_hist 0-10:{:>4} 10-20:{:>4} 20-30:{:>4} "
                   "30-40:{:>4} 40-54:{:>4} >54:{:>4}".format(*h))
        per = np.zeros(len(fw), int)
        for i, j in enumerate(idx):
            if inside[i]:
                per[j] += 1
        out.append(f"  caught_per_forward  0:{int((per==0).sum())} "
                   f"1:{int((per==1).sum())} 2:{int((per==2).sum())} "
                   f"3+:{int((per>=3).sum())}")
        ph = []
        for a, _b in fw:
            k = np.searchsorted(tick_t, a) - 1
            if k >= 0:
                ph.append(a - tick_t[k])
        if ph:
            ph = np.array(ph, float)
            out.append(f"  start_after_tick p10={np.percentile(ph,10):.0f} "
                       f"p50={np.percentile(ph,50):.0f} p90={np.percentile(ph,90):.0f}ms")

    segs = [(r.get("dgeo_seg") or {}) for r in news]
    gaps = np.diff([r["ts_ns"] / 1e6 for r in news])
    out.append(f"depth_gap      {pct(gaps)}")
    out.append(f"depth_ms       {pct([r.get('dgeo_ms') for r in news])}")
    out.append(f"depth_age      {pct([r.get('dgeo_age_ms') for r in ticks])}")
    for key in ("queue_ms", "pub_ms", "cap_ms"):
        out.append(f"  seg.{key:<9}{pct([s.get(key) for s in segs])}")
    src: dict[str, int] = {}
    for r in ticks:
        k = r.get("ro_source")
        src[k] = src.get(k, 0) + 1
    out.append("ro_source      " + " ".join(f"{k}:{v}" for k, v in src.items()))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(
        Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "logs"))
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    files = sorted(Path(args.root).glob("*/trace/speedrush.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    lines: list[str] = []
    for f in files[:args.sessions]:
        lines.extend(analyze(f))
        lines.append("")
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
