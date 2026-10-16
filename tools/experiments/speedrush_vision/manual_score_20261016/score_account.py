# -*- coding: utf-8 -*-
"""人工局分数总账（20261016_160317，我方终局 10080）。

模型（RULES §4.11 + 本局 rate 桶实证）：
  每个显示刷新（0.5s 网格）累计：里程（1 分/米，巡航 41 m/s → 每刷新 ≈20.5 分）+ 30×k（k=30 分单位数）
  读数间隔 ~0.63s → 每次读数跨 1~2 个刷新：d = 20.5×n + 30×k（巡航），起步段按 v̂ 折算
  rate 桶 = 上一整秒总增量 = 41 + 30×K（巡航），K=该秒单位数
事件价：超车 30 / 极限(超车·变道·木桶) 120 / 社会车退场 180 / 金币 30 —— 连击是否另计分 = 对账残差
"""
import json
from pathlib import Path

T = Path(__file__).parent
PH = {"P1": T / "manual_20261016_160353_p1", "P2": T / "manual_20261016_160437_p2"}
CRUISE = 41.0          # 分/秒（= m/s）
REFRESH = 0.5          # 显示刷新网格
UNIT = 30


def load_hud(ph):
    rows = [json.loads(l) for l in (PH[ph] / "hud.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out = []
    for r in rows:
        f = r["fields"]
        out.append({
            "seq": r["seq"], "ts": r["ts_ns"],
            "score": (f.get("score_a") or {}).get("value"),
            "rate": (f.get("rate_a") or {}).get("value"),
            "banner": ((f.get("event_banner") or {}).get("text") or "").strip(),
        })
    return out


def load_passes(ph):
    ev = []
    p = T / f"{ph.lower()}_trace.jsonl"
    if not p.exists():
        return ev
    for l in p.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        d = json.loads(l)
        for ps in (d.get("passes") or []):
            ev.append({"ts": d["ts_ns"], "fid": d["fid"], "id": ps.get("id"), "d_min": ps.get("d_min")})
    return ev


def fit_read(d, dt, v_now):
    """d = mileage + 30k；mileage = v_now*REFRESH*n（n=跨过的刷新数）。
    返回 [(n, k, err)] 按 err 升序。"""
    n_max = max(1, int(round(dt / REFRESH)) + 1)
    cands = []
    for n in range(1, n_max + 1):
        m = v_now * REFRESH * n
        r = d - m
        k = round(r / UNIT)
        if k < 0 or k > 12:
            continue
        err = abs(r - k * UNIT)
        if err <= 4:
            cands.append((n, k, err))
    cands.sort(key=lambda c: (c[2], c[1]))
    return cands


def decompose_phase(name, rows, v_profile=None):
    """v_profile(ts_s)->巡航前速度估计；None=全程巡航。"""
    print(f"\n===== {name} 逐拍分解 =====")
    print(f"{'t':>7} {'dt':>5} {'d':>5} {'fit(n,k)':>9} {'err':>4}  mileage  k   banner")
    tot_d = tot_m = tot_k = 0
    bad = []
    per_read = []
    prev = None
    for r in rows:
        if r["score"] is None:
            prev = None
            continue
        if prev is None:
            prev = r
            continue
        dt = (r["ts"] - prev["ts"]) / 1e9
        d = r["score"] - prev["score"]
        ts_s = (r["ts"] - rows[0]["ts"]) / 1e9
        v = CRUISE if v_profile is None else v_profile(ts_s)
        c = fit_read(d, dt, v)
        if not c:
            bad.append((ts_s, dt, d))
            print(f"{ts_s:7.2f} {dt:5.2f} {d:5d}   NO-FIT")
            per_read.append({"ts": ts_s, "dt": dt, "d": d, "n": None, "k": None})
        else:
            n, k, err = c[0]
            m = v * REFRESH * n
            tot_d += d; tot_m += m; tot_k += k
            b = f"  [{r['banner']}]" if r["banner"] else ""
            flag = "" if err <= 2 else f" err={err:.0f}"
            print(f"{ts_s:7.2f} {dt:5.2f} {d:5d}   ({n},{k})    {err:2.0f}  {m:6.1f} {k:3d}{b}{flag}")
            per_read.append({"ts": ts_s, "dt": dt, "d": d, "n": n, "k": k, "m": m, "err": err})
        prev = r
    print(f"-- {name}: Σd={tot_d} Σmileage≈{tot_m:.0f} Σk={tot_k} → 事件分≈{tot_k*UNIT}；"
          f"里程核对 Σm/Σdt={tot_m / sum(p['dt'] for p in per_read if p.get('n')):.1f} 分/秒")
    if bad:
        print(f"   未拟合拍 {len(bad)}: {bad}")
    return per_read, tot_d, tot_m, tot_k


def rate_buckets(rows):
    """rate 桶去重（按值变化计一次）：rate=41+30K → K"""
    seq, last = [], None
    for r in rows:
        v = r["rate"]
        if v is None:
            continue
        if v != last:
            K, rem = divmod(v - 41, UNIT)
            seq.append((v, K, rem))
            last = v
    return seq


def main():
    hud = {ph: load_hud(ph) for ph in PH}
    passes = {ph: load_passes(ph) for ph in PH}

    # 起步段速度剖面：rate=6@2.1s、rate=41@9.2s，线性充当中段
    def v_p1(ts):
        if ts >= 9.2:
            return CRUISE
        return max(3.0, 6.0 + (41.0 - 6.0) * (ts - 2.1) / (9.2 - 2.1)) if ts > 2.1 else max(2.0, 3.0 * ts)

    p1 = decompose_phase("P1", hud["P1"], v_profile=v_p1)
    p2 = decompose_phase("P2", hud["P2"], v_profile=None)

    for ph in ("P1", "P2"):
        print(f"\n-- {ph} rate 桶（K=单位数）--")
        bs = rate_buckets(hud[ph])
        print("  " + " ".join(f"{v}(K{K})" if rem == 0 else f"{v}!" for v, K, rem in bs))
        s = sum(v for v, _, _ in bs)
        print(f"  Σbucket={s}  K总数={sum(K for _, K, _ in bs)}")

    for ph in ("P1", "P2"):
        print(f"\n-- {ph} passes（{len(passes[ph])} 次，按 d_min 升序）--")
        for e in sorted(passes[ph], key=lambda x: x["d_min"]):
            print(f"  d_min={e['d_min']:.3f} t={(e['ts'] - hud[ph][0]['ts']) / 1e9:7.2f}s id={e['id']}")

    print("\n===== 相位衔接 =====")
    p1_last = next(r for r in reversed(hud["P1"]) if r["score"] is not None)
    p2_first = next(r for r in hud["P2"] if r["score"] is not None)
    print(f"P1 末读 {p1_last['score']} → P2 首读 {p2_first['score']}：衔接 Δ={p2_first['score'] - p1_last['score']}")

    print("\n===== 归因（d_min 单位=车道；极限阈值经验分界 ≈1.0）=====")
    for ph in ("P1", "P2"):
        close = [e for e in passes[ph] if e["d_min"] < 1.0]
        far = [e for e in passes[ph] if e["d_min"] >= 1.0]
        print(f"{ph}: passes<1.0（极限超车候选）={len(close)}  ≥1.0（普通超车）={len(far)}")
        print(f"  <1.0 的 d_min: {sorted(round(e['d_min'],3) for e in close)}")
        print(f"  ≥1.0 的 d_min: {sorted(round(e['d_min'],3) for e in far)}")


if __name__ == "__main__":
    main()
