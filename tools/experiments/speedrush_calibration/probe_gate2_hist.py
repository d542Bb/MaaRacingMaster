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


STRADDLE_PX = 80.0     # 跨线判定：近端截距落画面中心 ±80px 内 = 车压虚线


def frame_straight(rgb) -> bool:
    """两侧主族（截距最靠近 SCREEN_CX 的左/右各一）矢高皆 ≤SAG_MAX，
    且**无跨线族**——自车处半道位时夹车虚线仍在 ±0.5 道、c_f 中点仍报 0
    （c_f 对 ±0.5 道有周期歧义，扩样轮实证：A 场 2/4 轨、163152 场 4/9 轨
    frac 稳定 0.38~0.58 且轨内 rms≤0.10——偏移稳定非噪声）。跨线帧直接剔除：
    免标定、不读 X，与直道判据同族（纯像素几何）。"""
    fams = [f for f in line_families(rgb) if f[2] >= MIN_CLUSTER_SEGS]
    if any(abs(f[0] - SCREEN_CX) < STRADDLE_PX for f in fams):
        return False
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
        if d.get("v") == 2:      # v2：含跨线帧剔除（v1 缓存作废重算）
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
    p.write_text(json.dumps({"mask": mask, "v": 2}), encoding="utf-8")
    return np.array(mask)


def session_pipeline(s: str, stride: int, base: dict, verbose: bool = True):
    """单场：筛帧→场级标定→直道门控→直道轨拟合→直道接地点。返回数据 dict。"""
    rows, _ = analyze_series(s, stride)
    rows = staged_filter(rows, base["base"], score_intervals(s))
    race = [r for r in rows if r["stage"] == 4]
    if len(race) < 15:
        if verbose:
            print(f"== {s}: 比赛态帧不足，跳过")
        return None
    g1 = CACHE / "gate1.json"
    if g1.exists() and s in json.loads(g1.read_text(encoding="utf-8"))["sessions"]:
        cal = json.loads(g1.read_text(encoding="utf-8"))["sessions"][s]["cal"]
    else:
        from probe_gate1_invariance import (ax_physical, session_calib)
        cal = session_calib(s, race)
        if not cal:
            if verbose:
                print(f"== {s}: 场级标定失败，跳过")
            return None
        # 刻度源 = 物理口径（近端截距间距）优先——Gate 1 连带定论：晶格锚在
        # 真帧被边线/护栏污染、跨场差达 5 倍（0.413~2.049），用它当刻度会把
        # α 估计污染成刻度噪声（第二轮交叉验证实证）；物理口径四场
        # 0.563/0.560/0.580/0.574，CV≈1.8%。仅物理不可用时退回晶格。
        phys = ax_physical(s, race, cal["vpx"], cal["y_h"])
        cal["A_x_used"] = phys["A_x_phys"] if phys else cal["A_x"]
        if verbose:
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
    good = np.array([m and not any(r["ts"] >= t - LANE_CHANGE_PRE
                                   and r["ts"] <= t + LANE_CHANGE_POST
                                   for t in lc)
                     for m, r in zip(mask, rows)])
    if verbose:
        print(f"== {s}: 直道帧 {mask.mean():.0%}（{mask.sum()}/{len(mask)}），"
              f"直道∧非变道 {good.mean():.0%}")
    dets = json.loads((TRICK_CACHE / f"dets_{s}.json").read_text(encoding="utf-8"))
    t_straight = sorted(r["ts"] for r, g in zip(rows, good) if g)

    def near_straight(ts: float) -> bool:
        # ±0.7s：直道帧在时间上连续（矢高是场景量，相邻直道帧之间不会
        # 突然变弯），0.35s 覆盖出现空洞 → 轨被切碎（首跑实证 0 条过门槛）
        i = np.searchsorted(t_straight, ts)
        lo = t_straight[i - 1] if i > 0 else -1e9
        hi = t_straight[i] if i < len(t_straight) else 1e9
        return min(ts - lo, hi - ts) <= 0.7
    import probe_gate1_invariance as g1m
    g1m.TRACK_MIN_OBS = 8   # 非 trick 场车流稀（最长轨 31 观测），24 门槛不适用
    tracks = build_tracks(dets, ivs, lc, cal)
    ms = []
    for t in tracks:
        o = [x for x in t["obs"] if near_straight(x["ts"])]
        if len(o) < 12:
            continue
        m = track_metrics(dict(t, obs=o))
        if m["d_ratio"] < 1.3:      # 真的在接近，b 才有判除力（距离量，非 X）
            continue
        m["session"] = s
        ms.append(m)
    for m in ms:
        o = sorted(m["obs"], key=lambda x: x["ts"])
        d = np.array([x["d"] for x in o])
        X = np.array([x["X"] for x in o])
        A = np.stack([np.ones_like(d), 1.0 / d, d], 1)
        (a, b, c), *_ = np.linalg.lstsq(A, X, rcond=None)
        r = X - A @ np.array([a, b, c])
        m["fit"] = {"id": m["id"], "n": len(o), "a": float(a), "b": float(b),
                    "c": float(c), "rms": float(np.sqrt(np.mean(r ** 2))),
                    "drift": m["drift"]}
    c_ts, c_val = flanking_centers(s, rows, ivs, cal, lc)
    xs_pt = []
    for f in dets:
        if not any(a <= f["ts"] <= b for a, b in ivs) or not near_straight(f["ts"]):
            continue
        if any(f["ts"] >= t - LANE_CHANGE_PRE and f["ts"] <= t + LANE_CHANGE_POST
               for t in lc):
            continue
        for c in f["cars"]:
            if c[0] != 1:
                continue
            u, v = float(c[2]), float(c[3])
            if v - cal["y_h"] < 10 or v > 690:
                continue
            xs_pt.append((u, v, f["ts"]))
    return {"s": s, "cal": cal, "ms": ms, "xs_pt": xs_pt,
            "c_ts": c_ts, "c_val": c_val, "straight_frac": float(mask.mean())}


def main(sessions: list[str], stride: int = 2) -> None:
    base = load_base()
    if not base:
        print("无冻结基准——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    all_a, report = [], {}
    for s in sessions:
        d = session_pipeline(s, stride, base)
        if not d:
            continue
        ms = d["ms"]
        if ms:
            b_arr = np.array([m["fit"]["b"] for m in ms])
            print(f"   复验一（直道轨 {len(ms)} 条）：b(X~1/d) 负号 "
                  f"{int((b_arr < 0).sum())}/{len(ms)}，中位 {np.median(b_arr):+.1f} 道·px"
                  f"（曲率假说预期：坍缩向 0）；漂移中位 "
                  f"{np.median([m['drift'] for m in ms]):.3f} 道")
            from probe_gate1_invariance import attribution
            per = [{"a": m["fit"]["a"], "b": m["fit"]["b"],
                    "c": m["fit"]["c"], "rms": m["fit"]["rms"]} for m in ms]
            at = attribution(per)
            if at.get("lane_reg"):
                lr = at["lane_reg"]
                print(f"     直道 lane 回归：零点偏 {lr['zero_bias']:+.3f} 道  "
                      f"尺度 α = {lr['scale_alpha']:.3f}  max|bias| "
                      f"{lr['max_abs_bias']:.2f} 道（n={lr['n']}）")
            all_a += per
        cal = d["cal"]
        xs = [cal["A_x_used"] * (u - cal["vpx"]) / (v - cal["y_h"])
              for u, v, _ in d["xs_pt"]]
        rep = hist_report(xs, "raw")
        print(f"   复验二（Gate 2，直道接地点 n={len(xs)}）：{rep}")
        report[s] = {"straight_frac": d["straight_frac"],
                     "tracks": [m["fit"] for m in ms], "hist_raw": rep}
    (CACHE / "gate2.json").write_text(json.dumps(report, ensure_ascii=False),
                                       encoding="utf-8")
    if len(all_a) >= 5:
        b_arr = np.array([p["b"] for p in all_a])
        from probe_gate1_invariance import attribution
        print(f"\n== 跨场复验一：直道轨 {len(all_a)} 条，b 负号 "
              f"{int((b_arr < 0).sum())}/{len(all_a)}，中位 {np.median(b_arr):+.1f} 道·px")
        print(f"   判读：|b| 中位较 Gate 1 全样本（−41）"
              f"{'坍缩' if abs(np.median(b_arr)) < 20 else '未坍缩'}"
              f" → 曲率假说{'支持' if abs(np.median(b_arr)) < 20 else '被否，回到零点/y_h/接地点之争'}")
        pooled = attribution(all_a)
        if pooled.get("lane_reg"):
            lr = pooled["lane_reg"]
            print(f"   跨场直道 lane 回归：零点偏 {lr['zero_bias']:+.3f} 道  "
                  f"尺度 α = {lr['scale_alpha']:.3f}  max|bias| "
                  f"{lr['max_abs_bias']:.2f} 道（n={lr['n']}）")


# ---------- 重定标 + 留场验证（维护者设计 2026-09-19） ----------
# 判据先写死：留场（未参与 α 估计）上 lane 回归 α_val ∈ [0.95,1.05] 且
# |zero_bias_val| < 0.25 道 → Gate 2 重定标通过；histogram 峰距如实报告为
# 辅助材料。标定侧与验证侧都冻结 vpx/y_h/零点——只允许 A_x 一个参数动，
# 防止「拿同一批数据修参数再拿同一批数据验参数」。

def a_centered(m: dict, d: dict) -> float:
    """轨 a 消自车横向偏移：减去该轨时间窗内 c_f（夹车虚线中心，像素共模
    信号）的中位。lane 回归前提 = 自车在道中央；自车换道未计分则 banner 不
    剔除（213104 实证：4 轨中 3 轨 a 落半道位，α 被拉至 0.754——非尺度错，
    是前提破坏）。c_f 窗中位噪声 ~±0.08 道，半道位（0.5）可分。"""
    if len(d["c_val"]) < 3:
        return m["fit"]["a"]
    ts = np.array([x["ts"] for x in m["obs"]])
    return m["fit"]["a"] - float(np.median(
        np.interp(ts, d["c_ts"], d["c_val"])))


def rescale_holdout(calib: list[str], val: list[str], stride: int = 2) -> None:
    base = load_base()
    if not base:
        print("无冻结基准——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    from probe_gate1_invariance import attribution
    per_c = []
    for s in calib:
        d = session_pipeline(s, stride, base)
        if not d:
            return
        per_c += [{"a": a_centered(m, d), "b": m["fit"]["b"],
                   "c": m["fit"]["c"], "rms": m["fit"]["rms"]} for m in d["ms"]]
    at = attribution(per_c)
    if not at.get("lane_reg"):
        print("标定侧直道轨不足，无法定 α")
        return
    alpha = at["lane_reg"]["scale_alpha"]
    print(f"\n== 标定侧（{len(per_c)} 条直道轨，场 {calib}）：α = {alpha:.3f}"
          f" → A_x ← A_x/α（×{1 / alpha:.3f}）；标定侧零点偏 "
          f"{at['lane_reg']['zero_bias']:+.3f} 道")
    for s in val:
        d = session_pipeline(s, stride, base)
        if not d:
            continue
        cal = d["cal"]
        r = 1.0 / alpha                      # X_new = X_old · r（线性缩放）
        per_v = [{"a": a_centered(m, d) * r, "b": m["fit"]["b"] * r,
                  "c": m["fit"]["c"] * r, "rms": m["fit"]["rms"] * r}
                 for m in d["ms"]]
        atv = attribution(per_v)
        xs = []
        for u, v, t in d["xs_pt"]:
            x = cal["A_x_used"] * r * (u - cal["vpx"]) / (v - cal["y_h"])
            if len(d["c_val"]) >= 3:
                x -= float(np.interp(t, d["c_ts"], d["c_val"] - np.median(d["c_val"])))
            xs.append(x)
        rep = hist_report(xs, "val")
        lr = atv.get("lane_reg")
        if lr:
            ok = (0.95 <= lr["scale_alpha"] <= 1.05
                  and abs(lr["zero_bias"]) < 0.25)
            print(f"   留场 {s}（冻结 vpx/y_h，A_x {cal['A_x_used']:.3f}→"
                  f"{cal['A_x_used'] * r:.3f}，n={lr['n']}）：α_val = "
                  f"{lr['scale_alpha']:.3f}  零点偏 {lr['zero_bias']:+.3f} 道  "
                  f"max|bias| {lr['max_abs_bias']:.2f} 道；hist 峰 "
                  f"{rep['peaks']} 距 {rep['peak_gaps']} → "
                  f"{'通过' if ok else '不通过'}")
        else:
            print(f"   留场 {s}：直道轨不足（n<3），无法回归")
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
    ap.add_argument("sessions", nargs="*", default=[])
    ap.add_argument("--rescale", nargs="+", metavar="CALIB",
                    help="标定侧场次（估 α、修 A_x）")
    ap.add_argument("--holdout", nargs="+", metavar="VAL",
                    help="留场验证场次（冻结参数，只验不改）")
    args = ap.parse_args()
    if args.rescale:
        rescale_holdout(args.rescale, args.holdout or args.sessions)
    else:
        main(args.sessions)
