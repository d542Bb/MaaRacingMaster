"""Gate 2 前置：免标定直道门控 → Gate 1b 深度项复验 + Gate 2 车道号 histogram。

**为什么先做门控（维护者裁定 2026-09-19）**：Gate 1b 已证明「存在与深度相关
的系统性项」（VPx/零点假说被排除），但曲率/接地点/y_h 在含弯道上不可辨识
（1/d 与 1/d² 窄范围退化）。直接把曲率自由度拿掉——只在直道帧上做绝对检验，
比拟合更复杂的模型更可辨识。

**直道判据（免标定、不读 X、不用 VP/y_h/A_x）**：三维直线在针孔投影下仍是
直线——直路上的车道漆线在图像中是直的，弯道中是弯的。操作化：陡线段按
近端截距（外推到 V_REF 行，线段自身斜率）聚成线族，取最靠近画面中心
（640，追车相机把自车钉在画面中央——trick 实验确立的观测事实，非标定输出）
的两侧线族，各自端点做线性拟合，**矢高**（端点到拟合直线的最大垂距，px）
≤ SAG_MAX ⇒ 该线族直；两族皆直 ⇒ 帧直。自车变道/滑动是平移+旋转，不破坏
直线 ⇒ 门控只管路的曲率，不管自车机动（banner 剔除仍另行生效）。

**复验一（Gate 1b 解锁）**：直道帧上重建车辆轨（AI 车横向真值恒定·维护者
确认），逐轨拟合 X = a + b/d + c·d——若 b 的系统性负偏随曲率自由度消失而
坍缩 → 深度项确为曲率；若仍 9/10 负、量级不减 → 曲率解释被否，回到零点/
y_h/接地点之争。

**复验二（Gate 2）**：直道帧 + 非变道窗内全部车辆接地点 X 的直方图——
判据「多峰、峰距 ≈ 1 车道宽」（刻度 = 物理 A_x；s_med≠1.0 的尺度疑点在此
直接显形）。自车横向偏移 e(t) 会整体平移车道结构，直方图另报 c_f 消偏版本。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_gate2_hist.py <session>...
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import (CACHE, DEMOS, TRICK_CACHE, V_REF, analyze_series,  # noqa: E402
                            load_base, score_intervals, staged_filter)
from probe_gate1_invariance import (LANE_CHANGE_POST, LANE_CHANGE_PRE,  # noqa: E402
                                    TRACK_MIN_OBS, build_tracks, flanking_centers,
                                    lane_change_times, road_segs, track_metrics)

# ---------- 先验固定的阈值 ----------
# 矢高上限（px）。2026-09-19 十场扫描定标：直道场中位 24~30px（Hough 端点噪声
# × 斜率误差在 500px v 跨度上的放大底），弯道场（trick 高架）73~237px——
# 40px 落在两峰谷底；原 5px 拍在噪声底以下（全 0% 判直，诊断叠图实锤）。
SAG_MAX = 40.0
CLUSTER_GAP = 60.0     # 截距断簇间隙（px，Gate 0 S1b 同参）
MIN_CLUSTER_SEGS = 4   # 参与矢高检验的线族最少线段数
MIN_LONG_DV = 100.0    # 参与截距聚类的线段纵向跨度（短线外推截距不可信）
SCREEN_CX = 640.0      # 画面中心（自车钉中央的观测事实，非标定输出）
HIST_BIN = 0.1         # Gate 2 直方图箱宽（车道单位）
PEAK_PROM = 3.0        # 峰判定：比两侧邻箱至少高 3 个计数


def line_families(rgb) -> list[np.ndarray]:
    """截距聚类线族：每族 = 该物理线所有 ≥MIN_LONG_DV 线段的端点集 (N,2)。"""
    cuts = []
    for s in road_segs(rgb):
        dv = s[3] - s[1]
        if abs(dv) < MIN_LONG_DV:
            continue
        u_cut = s[0] + (s[2] - s[0]) * (V_REF - s[1]) / dv
        if 0 < u_cut < 1280:
            cuts.append((u_cut, s))
    cuts.sort(key=lambda p: p[0])
    fams, cur = [], [cuts[0]] if cuts else []
    for c in cuts[1:]:
        if c[0] - cur[-1][0] < CLUSTER_GAP:
            cur.append(c)
        else:
            fams.append(cur)
            cur = [c]
    if cur:
        fams.append(cur)
    out = []
    for f in fams:
        pts = np.array([[e, v] for _, s in f for e, v in ((s[0], s[1]), (s[2], s[3]))])
        out.append((float(np.median([c for c, _ in f])), pts, len(f)))
    return out


def sagitta(pts: np.ndarray) -> float:
    """端点到最优直线（u=αv+β 最小二乘）的最大垂距（px，水平度量）。"""
    if len(pts) < 4:
        return float("inf")
    v, u = pts[:, 1], pts[:, 0]
    alpha, beta = np.polyfit(v, u, 1)
    return float(np.max(np.abs(u - (alpha * v + beta))))


def frame_straight(rgb) -> bool:
    """两侧主族（截距最靠近 SCREEN_CX 的左/右各一）矢高皆 ≤SAG_MAX。"""
    fams = [f for f in line_families(rgb) if f[2] >= MIN_CLUSTER_SEGS]
    left = [f for f in fams if f[0] < SCREEN_CX]
    right = [f for f in fams if f[0] > SCREEN_CX]
    if not left or not right:
        return False
    l = max(left, key=lambda f: f[0])
    r = min(right, key=lambda f: f[0])
    return sagitta(l[1]) <= SAG_MAX and sagitta(r[1]) <= SAG_MAX


def straight_mask(sess_name: str, rows: list[dict]) -> np.ndarray:
    p = CACHE / f"straight_{sess_name}.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("stride_ok", True):
            return np.array(d["mask"])
    sess = DEMOS / sess_name
    mask = []
    t0 = time.perf_counter()
    for i, r in enumerate(rows):
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        mask.append(frame_straight(rgb))
        if i % 100 == 0:
            print(f"  [{sess_name}] 直道门控 {i}/{len(rows)}"
                  f"（{time.perf_counter() - t0:.0f}s）", flush=True)
    p.write_text(json.dumps({"mask": mask}), encoding="utf-8")
    return np.array(mask)


def main(sessions: list[str], stride: int = 2) -> None:
    base = load_base()
    if not base:
        print("无冻结基准——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    all_b, all_a, report = [], [], {}
    for s in sessions:
        rows, _ = analyze_series(s, stride)
        rows = staged_filter(rows, base["base"], score_intervals(s))
        race = [r for r in rows if r["stage"] == 4]
        if len(race) < 15:
            print(f"== {s}: 比赛态帧不足，跳过")
            continue
        g1 = CACHE / "gate1.json"
        if g1.exists() and s in json.loads(g1.read_text(encoding="utf-8"))["sessions"]:
            cal = json.loads(g1.read_text(encoding="utf-8"))["sessions"][s]["cal"]
        else:
            from probe_gate1_invariance import (ax_physical, session_calib)
            cal = session_calib(s, race)
            if not cal:
                print(f"== {s}: 场级标定失败，跳过")
                continue
            phys = ax_physical(s, race, cal["vpx"], cal["y_h"])
            if phys and abs(cal["A_x"] - phys["A_x_phys"]) / phys["A_x_phys"] > 0.25:
                cal["A_x_used"] = phys["A_x_phys"]
            else:
                cal["A_x_used"] = cal["A_x"]
            print(f"   场级标定：VP=({cal['vpx']:.1f},{cal['y_h']:.1f}) "
                  f"A_x={cal['A_x_used']:.3f}")
        ivs = score_intervals(s)
        if not ivs:   # 非 trick 场无 score 缓存：用 stage4 帧时域聚段回退
            tss = sorted(r["ts"] for r in race)
            ivs, a = [], tss[0]
            for g, t in zip(np.diff(tss), tss[1:]):
                if g > 1.0:
                    ivs.append((a, t))
                    a = t
            ivs.append((a, tss[-1]))
        lc = lane_change_times(s)
        mask = straight_mask(s, rows)
        # 直道且非变道窗
        good = np.array([m and not any(r["ts"] >= t - LANE_CHANGE_PRE
                                       and r["ts"] <= t + LANE_CHANGE_POST
                                       for t in lc)
                         for m, r in zip(mask, rows)])
        print(f"== {s}: 直道帧 {mask.mean():.0%}（{mask.sum()}/{len(mask)}），"
              f"直道∧非变道 {good.mean():.0%}")
        # --- 复验一：Gate 1b 深度项（直道帧重建轨） ---
        # 直道帧稀疏（stride2 网格上 ~5% 全场/35% 比赛段），容差 ±0.35s 把
        # 相邻 dets 帧都挂上最近直道判定；轨门槛降到 12 观测（直道场内连续段）。
        dets = json.loads((TRICK_CACHE / f"dets_{s}.json").read_text(encoding="utf-8"))
        t_straight = sorted(r["ts"] for r, g in zip(rows, good) if g)

        def near_straight(ts: float) -> bool:
            # ±0.7s：直道帧在时间上连续（矢高是场景量，相邻直道帧之间不会
            # 突然变弯），0.35s 覆盖出现空洞 → 轨被切碎（首跑实证 0 条过门槛）
            i = np.searchsorted(t_straight, ts)
            lo = t_straight[i - 1] if i > 0 else -1e9
            hi = t_straight[i] if i < len(t_straight) else 1e9
            return min(ts - lo, hi - ts) <= 0.7
        ivs_str = [(a, b) for a, b in ivs]
        import probe_gate1_invariance as g1
        g1.TRACK_MIN_OBS = 8   # 非 trick 场车流稀（最长轨 31 观测），24 门槛不适用
        tracks = build_tracks(dets, ivs_str, lc, cal)
        ms = []
        for t in tracks:
            o = [x for x in t["obs"] if near_straight(x["ts"])]
            if len(o) < 12:
                continue
            t2 = dict(t, obs=o)
            m = track_metrics(t2)
            if m["d_ratio"] < 1.3:      # 真的在接近，b 才有判除力（距离量，非 X）
                continue
            m["session"] = s
            ms.append(m)
        bs = []
        for m in ms:
            o = sorted(m["obs"], key=lambda x: x["ts"])
            d = np.array([x["d"] for x in o])
            X = np.array([x["X"] for x in o])
            A = np.stack([np.ones_like(d), 1.0 / d, d], 1)
            (a, b, c), *_ = np.linalg.lstsq(A, X, rcond=None)
            r = X - A @ np.array([a, b, c])
            bs.append((b, a))
            m_fit = {"id": m["id"], "n": len(o), "a": float(a), "b": float(b),
                     "c": float(c), "rms": float(np.sqrt(np.mean(r ** 2))),
                     "drift": m["drift"]}
            m["fit"] = m_fit
        if bs:
            b_arr = np.array([x[0] for x in bs])
            neg = int((b_arr < 0).sum())
            print(f"   复验一（直道轨 {len(bs)} 条）：b(X~1/d) 负号 {neg}/{len(bs)}，"
                  f"中位 {np.median(b_arr):+.1f} 道·px（曲率假说预期：坍缩向 0）"
                  f"；漂移中位 {np.median([m['drift'] for m in ms]):.3f} 道")
            from probe_gate1_invariance import attribution
            per = [{"id": m["id"], "n": m["n"], "a": m["fit"]["a"],
                    "b": m["fit"]["b"], "c": m["fit"]["c"],
                    "rms": m["fit"]["rms"], "drift": m["drift"]} for m in ms]
            at = attribution(per)
            if at.get("lane_reg"):
                lr = at["lane_reg"]
                print(f"     直道 lane 回归：零点偏 {lr['zero_bias']:+.3f} 道  "
                      f"尺度 α = {lr['scale_alpha']:.3f}  max|bias| "
                      f"{lr['max_abs_bias']:.2f} 道（n={lr['n']}）")
            if at.get("dz_over_az") is not None:
                print(f"     直道 Δz/A_z = {at['dz_over_az']:+.4f}")
            all_b += [(s, *x) for x in bs]
            all_a += [(s, m["fit"]["a"], m["fit"]["b"], m["fit"]["rms"])
                      for m in ms]
        # --- 复验二：Gate 2 histogram（直道帧接地点 X） ---
        c_ts, c_val = flanking_centers(s, rows, ivs, cal, lc)
        xs, xs_cm = [], []
        for f in dets:
            if not any(a <= f["ts"] <= b for a, b in ivs_str):
                continue
            if not near_straight(f["ts"]):
                continue
            if any(f["ts"] >= t - LANE_CHANGE_PRE and f["ts"] <= t + LANE_CHANGE_POST
                   for t in lc):
                continue
            for c in f["cars"]:
                if c[0] != 1:
                    continue
                u, v = float(c[2]), float(c[3])
                d = v - cal["y_h"]
                if d < 10 or v > 690:
                    continue
                X = cal["A_x_used"] * (u - cal["vpx"]) / d
                xs.append(X)
                if len(c_val) >= 3:
                    cf = np.interp(f["ts"], c_ts, c_val - np.median(c_val))
                    xs_cm.append(X - cf)
        rep = hist_report(xs, "raw")
        rep2 = hist_report(xs_cm, "c_f 消偏") if len(c_val) >= 3 else None
        print(f"   复验二（Gate 2，直道接地点 n={len(xs)}）：{rep}")
        if rep2:
            print(f"     c_f 消偏版（n={len(xs_cm)}）：{rep2}")
        report[s] = {"straight_frac": float(mask.mean()),
                     "tracks": [m.get("fit") for m in ms],
                     "hist_raw": rep, "hist_cm": rep2}
    (CACHE / "gate2.json").write_text(json.dumps(report, ensure_ascii=False),
                                       encoding="utf-8")
    if len(all_b) >= 5:
        b_arr = np.array([x[1] for x in all_b])
        print(f"\n== 跨场复验一：直道轨 {len(all_b)} 条，b 负号 {int((b_arr < 0).sum())}"
              f"/{len(all_b)}，中位 {np.median(b_arr):+.1f} 道·px")
        print(f"   判读：|b| 中位较 Gate 1 全样本（−41）{'坍缩' if abs(np.median(b_arr)) < 20 else '未坍缩'}"
              f" → 曲率假说{'支持' if abs(np.median(b_arr)) < 20 else '被否，回到零点/y_h/接地点之争'}")
        from probe_gate1_invariance import attribution
        pooled = attribution([{"a": a, "b": b, "rms": r} for _, a, b, r in all_a])
        if pooled.get("lane_reg"):
            lr = pooled["lane_reg"]
            print(f"   跨场直道 lane 回归：零点偏 {lr['zero_bias']:+.3f} 道  "
                  f"尺度 α = {lr['scale_alpha']:.3f}  max|bias| "
                  f"{lr['max_abs_bias']:.2f} 道（n={lr['n']}）")


def hist_report(xs: list[float], tag: str) -> dict:
    """直方图多峰 + 峰距（判据：峰距≈1 车道宽）。"""
    if len(xs) < 30:
        return {"n": len(xs), "peaks": [], "note": "样本不足"}
    lo, hi = -3.0, 3.0
    h, edges = np.histogram(np.clip(xs, lo, hi), bins=int((hi - lo) / HIST_BIN))
    pk = [i for i in range(1, len(h) - 1)
          if h[i] >= h[i - 1] + PEAK_PROM and h[i] >= h[i + 1] + PEAK_PROM]
    centers = [round(float(0.5 * (edges[i] + edges[i + 1])), 2) for i in pk]
    gaps = [round(b - a, 2) for a, b in zip(centers, centers[1:])]
    return {"n": len(xs), "peaks": centers, "counts": [int(h[i]) for i in pk],
            "peak_gaps": gaps}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    args = ap.parse_args()
    main(args.sessions)
