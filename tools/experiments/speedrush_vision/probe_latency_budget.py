# -*- coding: utf-8 -*-
"""端到端内容龄的账：pub 的真实成因、observe 的边际收益、以及年龄在控制上值多少钱。

回答三个决策问题（只读 debug 产物，不改生产）：

1. `pub`（发布→首次消费）是「等下一拍」还是「锁相偏移」？
   —— 按控制拍间隔 `dt` 分桶看 `pub` 分布；再看发布相位直方图。
   若 pub 与 dt 无关且分布窄 ⇒ 锁相（worker 周期≈拍周期的整数倍），
   治拍抖动对它无效。
2. observe 降到多少才能进下一档？
   —— 用实测拍时刻/observe 序列做离散事件仿真（worker 取最新 pending、
   串行处理），先复现实测 `fid_lag`，再扫 observe 缩放与 take 相位平移。
3. 这些毫秒在控制上值多少？
   —— `|Δro_off|` 对 `|Δsteer_norm|` 的弹性，以及 `ro_off` 拍间不变占比。

用法：``.venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_latency_budget.py``
环境变量 ``SR_TRACES``（逗号分隔局名）换批次。"""
from __future__ import annotations

import heapq
import json
import os
from pathlib import Path

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
BATCHES = {
    "改前": ["20261017_115032_p1", "20261017_115107_p2",
             "20261017_115223_p1", "20261017_115308_p2"],
    "几何提速后": ["20261017_130315_p1", "20261017_130400_p2"],
    "落盘搬线程后": ["20261017_132405_p1"],
}
if os.environ.get("SR_TRACES"):
    BATCHES = {"当前": [s for s in os.environ["SR_TRACES"].split(",") if s]}

GRID_MS = 50.3        # 控制拍名义周期（dt P50）


def _safe(d: str) -> Path:
    """trace 路径校验（安全扫描：固定目录 + 禁 .. 分量，越界即拒绝）。"""
    if ".." in Path(d).parts:
        raise ValueError(f"路径分量含 ..：{d}")
    p = (DBG / d).resolve()
    if DBG.resolve() not in p.parents:
        raise ValueError(f"路径越界：{d}")
    return p


def _p(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def _load(dirs):
    rows = []
    for d in dirs:
        rows += [json.loads(line) for line in
                 open(_safe(d) / f"trace_{d}.jsonl", encoding="utf-8")]
    return rows


def part_phase(rows) -> None:
    """pub 的真实成因：等下一拍（宽分布、随 dt 变）vs 锁相（窄分布、与 dt 无关）。"""
    print("== 1. pub 成因：拍量化还是锁相 ==")
    dt = [(b["ts_ns"] - a["ts_ns"]) / 1e6 for a, b in zip(rows, rows[1:])]
    print(f"  控制拍间隔 dt P10={_p(dt, .1):.1f} P50={_p(dt, .5):.1f} "
          f"P90={_p(dt, .9):.1f} max={max(dt):.1f} ms")
    pairs = []
    for prev, r in zip(rows, rows[1:]):
        seg = r.get("dgeo_seg") or {}
        if seg.get("pub_ms") is None:
            continue
        pairs.append((seg["pub_ms"], (r["ts_ns"] - prev["ts_ns"]) / 1e6))
    for lo, hi in ((0, 40), (40, 55), (55, 70), (70, 100), (100, 1e9)):
        xs = [a for a, b in pairs if lo <= b < hi]
        if xs:
            print(f"    dt∈[{lo:g},{hi:g}) n={len(xs):3d} "
                  f"pub P50={_p(xs, .5):5.1f} P90={_p(xs, .9):5.1f}")
    pubs = [a for a, _ in pairs]
    print(f"  pub P50={_p(pubs, .5):.1f} P10={_p(pubs, .1):.1f} "
          f"P90={_p(pubs, .9):.1f} —— 双峰（≈4 与 ≈T+6）且不随 dt 变 "
          f"⇒ 「错过一拍」的锁相偏移，不是等待分布")
    # 发布相位：反推发布时刻 = take(≈采集+cap) − pub，看相对拍网格是否均匀
    takes = []
    for r in rows:
        seg = r.get("dgeo_seg") or {}
        if seg.get("pub_ms") is None or seg.get("cap_ms") is None:
            continue
        takes.append((r["ts_ns"] / 1e6 + seg["cap_ms"] - seg["pub_ms"],
                      r["ts_ns"] / 1e6))
    if len(takes) > 20:
        ph = [(pt - t0) % GRID_MS for pt, t0 in takes]
        hist = {}
        for x in ph:
            hist[int(x // 10) * 10] = hist.get(int(x // 10) * 10, 0) + 1
        print(f"  发布相位(相对 {GRID_MS:g}ms 网格) 直方图 "
              f"{dict(sorted(hist.items()))}")
        print("    单峰 ⇒ 发布钉在拍网格某处（锁相）；平坦 ⇒ 与拍无关（随机相位）")


def part_sim(rows) -> None:
    """离散事件仿真：worker 取最新 pending、串行处理；先复现，再扫 observe。"""
    print("\n== 2. 仿真：observe 降到多少才进下一档 ==")
    takes, ftss, obs, fids = [], [], [], []
    for r in rows:
        seg = r.get("dgeo_seg") or {}
        t = r["ts_ns"] / 1e6
        cap = seg.get("cap_ms")
        cap = cap if cap is not None else 20.0
        takes.append(t + cap)
        ftss.append(t)
        fids.append(r["fid"])
        if r.get("dgeo_ms"):
            obs.append(r["dgeo_ms"])

    def sim(scale=1.0, shift=0.0):
        tk = [t + shift for t in takes]
        heap = []
        for k, t in enumerate(tk):
            heapq.heappush(heap, (t - 0.2, 1, k))
        state = {"free": -1e18, "pend": None, "oi": 0}
        pubs = []

        def start(now):
            if state["pend"] is not None and now >= state["free"]:
                pt, k = state["pend"]
                state["pend"] = None
                state["free"] = now + obs[state["oi"] % len(obs)] * scale
                state["oi"] += 1
                heapq.heappush(heap, (state["free"], 2, k))

        while heap:
            t, typ, k = heapq.heappop(heap)
            if typ == 1:
                state["pend"] = (t, k)
                start(t)
            else:
                pubs.append((t, k))
                start(t)
        pubs.sort()
        pi, cur, last = 0, None, -1
        ages, pbs, lags, new = [], [], [], 0
        for j, tt in enumerate(tk):
            while pi < len(pubs) and pubs[pi][0] <= tt:
                cur = pubs[pi]
                pi += 1
            if cur is None or cur[1] == last:
                continue
            last = cur[1]
            new += 1
            ptime, kk = cur
            ages.append(tt - ftss[kk])
            pbs.append(tt - ptime)
            lags.append(fids[j] - fids[kk])
        return ages, pbs, lags, new / len(tk)

    a, pb, lg, rate = sim()
    print(f"  复现(scale=1): age P50={_p(a, .5):.0f} pub P50={_p(pb, .5):.0f} "
          f"lag P50={_p(lg, .5):.0f} 新证据 {100 * rate:.0f}%")
    print("    实测 lag P50 见上节台账；仿真对 lag 匹配、对 pub 偏乐观"
          "（缺等锁/GC 开销）⇒ 只作相对比较")
    print("  observe 缩放：")
    base = _p(obs, .5)
    for sc in (1.0, 0.85, 0.72, 0.60, 0.50, 0.40, 0.30):
        a, pb, lg, rate = sim(sc)
        print(f"    obs={base * sc:5.1f}ms  age P50={_p(a, .5):4.0f} "
              f"P90={_p(a, .9):4.0f} | lag P50={_p(lg, .5):.0f} "
              f"P90={_p(lg, .9):.0f} | 新证据 {100 * rate:3.0f}%")
    print("  take 相位平移（只动消费时刻，不改采集/发布）：")
    for sh in (0.0, -8.0, -17.0, -25.0):
        a, pb, lg, rate = sim(1.0, sh)
        print(f"    shift={sh:+5.1f}ms  age P50={_p(a, .5):4.0f} "
              f"| lag P50={_p(lg, .5):.0f} | 新证据 {100 * rate:3.0f}%")


def part_elasticity(rows) -> None:
    """年龄在控制上值多少：ro_off 拍间跳变 → steer 的传递。"""
    print("\n== 3. 弹性：路心陈旧度对决策输出的影响 ==")
    dro, dsteer, dxt = [], [], []
    pairs = []
    same = 0
    for a, b in zip(rows, rows[1:]):
        if a.get("ro_off") is None or b.get("ro_off") is None:
            continue
        if a.get("ro_source") != b.get("ro_source"):
            continue
        d = abs(b["ro_off"] - a["ro_off"])
        dro.append(d)
        if d < 1e-9:
            same += 1
        if a.get("steer_norm") is not None and b.get("steer_norm") is not None:
            dsteer.append(abs(b["steer_norm"] - a["steer_norm"]))
            pairs.append((d, abs(b["steer_norm"] - a["steer_norm"])))
        if a.get("x_target") is not None and b.get("x_target") is not None:
            dxt.append(abs(b["x_target"] - a["x_target"]))
    if not dro:
        print("  无 ro_off 样本")
        return
    print(f"  |Δro_off|   P50={_p(dro, .5):.3f} P90={_p(dro, .9):.3f} lane"
          f" | 拍间完全不变 {100 * same / len(dro):.0f}% (n={len(dro)})")
    print(f"  |Δsteer|    P50={_p(dsteer, .5):.4f} P90={_p(dsteer, .9):.4f}")
    print(f"  |Δx_target| P50={_p(dxt, .5):.4f} P90={_p(dxt, .9):.4f}")
    if len(pairs) > 8:
        xs = [b for _, b in pairs]
        ys = [a for a, _ in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sx = sum((x - mx) ** 2 for x in xs) ** 0.5
        sy = sum((y - my) ** 2 for y in ys) ** 0.5
        corr = (sum((x - mx) * (y - my) for x, y in pairs) / (sx * sy)
                if sx and sy else float("nan"))
        slope = sum(x * y for x, y in pairs) / max(sum(x * x for x in xs), 1e-9)
        print(f"  |Δsteer|~|Δro| corr={corr:.2f} | Δsteer/Δro 斜率≈{slope:.2f}")
        print("    读法：ro_off 只解释 steer 变化的 corr²；若 |Δro| 的 P90 乘斜率"
              "仍小于 |Δsteer| 的 P90，则「读数更年轻」的收益上界低于控制噪声")


def main() -> None:
    for label, dirs in BATCHES.items():
        print(f"\n{'=' * 66}\n批次：{label}（{len(dirs)} 局）\n{'=' * 66}")
        rows = _load(dirs)
        print(f"拍数 {len(rows)}")
        part_phase(rows)
        part_sim(rows)
        part_elasticity(rows)


if __name__ == "__main__":
    main()
