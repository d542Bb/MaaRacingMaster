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


def get_cal(s: str, race: list[dict]) -> dict | None:
    """场级常数：vpx/y_h 冻结（gate1.json 优先），A_x_used 一律物理口径重算
    ——Gate 1 连带定论：晶格锚在真帧被边线/护栏污染、跨场差达 5 倍
    （0.413~2.049），用它当刻度会把 α 估计污染成刻度噪声；物理口径跨场
    稳定（CV≈1.8%）。仅物理不可用时退回晶格。"""
    from probe_gate1_invariance import (ax_physical, grid_A_x, session_calib)
    g1 = CACHE / "gate1.json"
    if g1.exists() and s in json.loads(g1.read_text(encoding="utf-8"))["sessions"]:
        cal = json.loads(g1.read_text(encoding="utf-8"))["sessions"][s]["cal"]
    else:
        cal = session_calib(s, race)
        if not cal:
            return None
    phys = ax_physical(s, race, cal["vpx"], cal["y_h"])
    gs = grid_A_x(s, cal["y_h"])
    if gs:
        cal["A_x_used"], cal["ax_source"] = gs["A_x"], "grid"
    elif phys:
        cal["A_x_used"] = phys["A_x_phys"]
        cal["ax_source"] = "physical"
    else:
        cal["A_x_used"] = cal["A_x"]
        cal["ax_source"] = "lattice"
    return cal


def session_pipeline(s: str, stride: int, base: dict, verbose: bool = True):
    """单场：筛帧→场级标定→直道门控→直道轨拟合→直道接地点。返回数据 dict。"""
    rows, _ = analyze_series(s, stride)
    rows = staged_filter(rows, base["base"], score_intervals(s))
    race = [r for r in rows if r["stage"] == 4]
    if len(race) < 15:
        if verbose:
            print(f"== {s}: 比赛态帧不足，跳过")
        return None
    cal = get_cal(s, race)
    if not cal:
        if verbose:
            print(f"== {s}: 场级标定失败，跳过")
        return None
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


# ---------- 维护者裁定 2026-09-19：a 轨迹卫生 + b 有界道内偏移模型 ----------
# 原则：A_x 冻结为物理口径，AI 轨**不再反调尺度**——只承担交叉验证：
#   a) 卫生：邻帧 u 跳变 = tracker 换车（#42 实锤），断轨取最长连续段；
#   b) 真值模型 X_i = bias + α·k_i + δ_i（k_i 整数车道、|δ_i|≤0.5 道有界
#      道内偏移——「固定车道」只保证横向恒定，不保证在道中心）；
#   c) 审计：δ 对 k 回归斜率 c1 ≈ (α_true−α)/α——α 若错，道内偏移会随
#      车道号系统性漂移。|c1| 小 = AI 轨独立佐证几何刻度；这不是反调，
#      α 一个字都不改。
DELTA_BOUND = 0.35     # 道内偏移物理界：车不压线 ⇒ |δ|≤(1−W_car)/2，
                       # 游戏车宽 ≈0.3~0.4 道 → 0.35。取 0.5 时相邻 k 格
                       # 互简并（格距 1.0、两侧各伸 0.5 恰好接满），且交替
                       # 拟合小样本崩解（首跑实证 bias 飞到 +1.8、δ=±2.6）
K_MAX = 3              # 车道号物理界：4 车道路相对自车最远 ±2，留 1 道余量。
                       # k 无界时交替拟合有尺度简并——远车配假高阶 k 补偿 δ，
                       # δ~k 审计被污染（首跑实证 k=11/12 而斜率仍 ≈0）
AUDIT_SLOPE = 0.05     # δ~k 斜率审计阈（≈5% 尺度失配）
JUMP_U0, JUMP_UD = 80.0, 0.5   # 断轨阈：|Δu| > max(80, 0.5·d) px @ Δt≤0.2s


def hygiene_longest_run(ms: list[dict]) -> list[dict]:
    """邻帧 u 跳变断轨，只留最长连续段（数据卫生，不碰任何标定参数）。"""
    out = []
    for m in ms:
        o = sorted(m["obs"], key=lambda x: x["ts"])
        runs, cur = [], [o[0]]
        for p, q in zip(o, o[1:]):
            if (q["ts"] - p["ts"] <= 0.2
                    and abs(q["u"] - p["u"]) > max(JUMP_U0, JUMP_UD * p["d"])):
                runs.append(cur)
                cur = [q]
            else:
                cur.append(q)
        runs.append(cur)
        best = max(runs, key=len)
        if len(best) >= 12 and len(best) < len(o):
            m2 = dict(m, obs=best)
            mm = track_metrics(m2)
            if mm["d_ratio"] >= 1.3:
                mm["fit"] = m["fit"]
                mm["id"] = m["id"]
                mm["session"] = m["session"]
                mm["n_hygiene_cut"] = len(o) - len(best)
                out.append(mm)
                continue
        out.append(m)
    return out


def bounded_check(ms: list[dict], alpha: float) -> dict:
    """X_i = bias + α·k_i + δ_i 拟合（α 冻结、k 整数、δ 有界）+ δ~k 审计。"""
    a = np.array([m["fit"]["a"] for m in ms])
    if len(a) < 3:
        return {"n": len(a)}
    # 全局网格搜索 bias（交替局部解小样本崩解，见 DELTA_BOUND 注）：
    # 每 bias 下 k 精确归属 = clip(round((a−bias)/α))，取 Σδ² 最小者。
    best = None
    for bias in np.arange(-0.6, 0.601, 0.02):
        k = np.clip(np.round((a - bias) / alpha), -K_MAX, K_MAX)
        delta = a - bias - alpha * k
        s = float(np.sum(delta ** 2))
        if best is None or s < best[0]:
            best = (s, float(bias), k, delta)
    _, bias, k, delta = best
    viol = int((np.abs(delta) > DELTA_BOUND).sum())
    M = np.stack([np.ones_like(k), k], 1)
    (c0, c1), *_ = np.linalg.lstsq(M, delta, rcond=None)
    return {"n": len(a), "bias": bias, "delta_med": float(np.median(delta)),
            "delta_max": float(np.abs(delta).max()), "viol": viol,
            "audit_slope": float(c1), "k": [int(x) for x in k],
            "delta": [round(float(x), 3) for x in delta]}


def validate(sessions: list[str], stride: int = 2) -> None:
    """Gate 2 正式口径（维护者裁定）：尺度判定 = 道路几何（框内夹车虚线
    s_med≈1.0，AI 无关）；AI 轨 = 交叉验证（卫生后过有界模型 + δ~k 审计
    + bias 零点）。判据先写死：s_med∈[0.9,1.1] ∧ viol=0 ∧ |audit_slope|
    <0.05 ∧ |bias|<0.25。A_x 全程冻结，任何结果都不回写标定。"""
    base = load_base()
    if not base:
        print("无冻结基准——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    from probe_gate1_invariance import dash_invariance
    rows_cache, report = {}, {}
    for s in sessions:
        d = session_pipeline(s, stride, base)
        if not d:
            continue
        ms0 = d["ms"]
        ms = hygiene_longest_run(ms0)
        cut = len(ms0) - len([m for m in ms if "n_hygiene_cut" not in m])
        bc = bounded_check(ms, d["cal"]["A_x_used"])
        # 几何判定：框内夹车虚线间距 s_med（物理 A_x 下应 ≈1.0）
        rows, _ = analyze_series(s, stride)
        rows_f = staged_filter(rows, base["base"], score_intervals(s))
        race = [r for r in rows_f if r["stage"] == 4]
        dash, _pts = dash_invariance(s, rows_f,
                                     score_intervals(s) or
                                     [(race[0]["ts"], race[-1]["ts"])],
                                     d["cal"], lane_change_times(s))
        smed = float(np.median([f["s_med"] for f in dash])) if dash else None
        # 三件套（维护者裁定 2026-09-19）：统计量必须与产生它的观测集合一起报告
        att = sum(f["n_att"] for f in dash)
        tig = sum(len(f["sbins"]) for f in dash)
        avail = (tig / att) if att else None
        bins_flat = [(b[0], b[1]) for f in dash for b in f["sbins"]]
        dband = {}
        for lo, hi in ((150, 250), (250, 400), (400, 700)):
            v = [sv for d, sv in bins_flat if lo <= d < hi]
            if v:
                dband[f"{lo}-{hi}"] = round(float(np.median(v)), 3)
        ok_geo = smed is not None and 0.9 <= smed <= 1.1
        ok_ai = (bc.get("n", 0) >= 3 and bc.get("viol", 1) == 0
                 and abs(bc.get("audit_slope", 1)) < AUDIT_SLOPE
                 and abs(bc.get("bias", 1)) < 0.25)
        print(f"== {s}: 轨 {len(ms0)}→卫生后 {len(ms)}"
              f"（断/弃 {len(ms0) - len(ms)}）")
        if bc.get("n", 0) >= 3:
            print(f"   AI 交叉验证：bias {bc['bias']:+.3f} 道  δ 中位 "
                  f"{bc['delta_med']:+.2f} max {bc['delta_max']:.2f} 越界 "
                  f"{bc['viol']}  δ~k 斜率 {bc['audit_slope']:+.3f}"
                  f"（审计阈 {AUDIT_SLOPE}）  k={bc['k']} δ={bc['delta']}")
        print(f"   几何判定：框内虚线 s_med = {smed if smed is None else round(smed, 3)}"
              f"（判据 [0.9,1.1]）  紧致对可用率 "
              f"{avail if avail is None else f'{avail:.0%}'}（{tig}/{att} 箱）  "
              f"s(d) 分带中位 {dband or '—'}→ 几何 {'过' if ok_geo else '不过'} ∧ "
              f"AI {'过' if ok_ai else '不过'} → Gate 2 本场 "
              f"{'✅' if ok_geo and ok_ai else '🟡/❌'}")
        report[s] = {"bounded": bc, "s_med": smed, "avail": avail,
                     "d_band": dband, "geo_ok": ok_geo,
                     "ai_ok": ok_ai, "n_track": len(ms)}
    (CACHE / "gate2_validate.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")


# ---------- 仪器审计（维护者裁定 2026-09-19：不动任何标定参数） ----------
# 问题收缩为：同一「车道宽」概念在两个公式下稳定差 ~9%——
#   ax_physical：v=650 单行两拟合线截距差；
#   s_med：dash 实际深度范围内逐 d 中点 X 差的中位。
# 审计三问（判据先写死）：
#   A 固定行扫描：Δx(v)/Δx(650) 在 v=550~700 漂移 >±5% → 固定行锚不稳定，
#     ax_physical 需重定义；同时记录 v=650 是否落在两族真实支持区间内。
#   B 同深度对比：各 d 分箱 du_obs（实测点中位差）/ du_fit（拟合线差），
#     中位偏离 1 超 ±5% → 分歧在检测/聚类层；≈1 → 在几何映射链（y_h/vpx）。
#   C 外部锚（RULES 车道宽/车宽）：A/B 排除内部管线错之前不引入。
AUDIT_VS = [550.0, 575.0, 600.0, 625.0, 650.0, 675.0, 700.0]
AUDIT_DBINS = [12, 100, 250, 400, 700]   # d=v−y_h 分箱（粗箱，见 B 注）


def audit_spacing(sessions: list[str], stride: int = 2) -> None:
    base = load_base()
    if not base:
        print("无冻结基准——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    all_A, all_B, all_sup = [], [], []
    report = {}
    for s in sessions:
        rows, _ = analyze_series(s, stride)
        rows = staged_filter(rows, base["base"], score_intervals(s))
        race = [r for r in rows if r["stage"] == 4]
        if len(race) < 15:
            continue
        cal = get_cal(s, race)
        if not cal:
            continue
        ivs = score_intervals(s) or [(race[0]["ts"], race[-1]["ts"])]
        lc = lane_change_times(s)
        mask = straight_mask(s, rows)
        sess = DEMOS / s
        recs = []
        for i, r in enumerate(rows):
            if not (any(a <= r["ts"] <= b for a, b in ivs)
                    and not any(r["ts"] >= t - LANE_CHANGE_PRE
                                and r["ts"] <= t + LANE_CHANGE_POST
                                for t in lc)):
                continue
            if not mask[i]:
                continue
            rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
            fams = [f for f in line_families(rgb) if f[2] >= MIN_CLUSTER_SEGS]
            if any(abs(f[0] - SCREEN_CX) < STRADDLE_PX for f in fams):
                continue
            left = [f for f in fams if f[0] < SCREEN_CX]
            right = [f for f in fams if f[0] > SCREEN_CX]
            if not left or not right:
                continue
            lf = max(left, key=lambda f: f[0])
            rf = min(right, key=lambda f: f[0])
            if sagitta(lf[1]) > SAG_MAX or sagitta(rf[1]) > SAG_MAX:
                continue
            pl, pr = lf[1], rf[1]                 # (u,v) 点集
            al, bl = np.polyfit(pl[:, 1], pl[:, 0], 1)
            ar, br = np.polyfit(pr[:, 1], pr[:, 0], 1)
            dx = {v: (ar * v + br) - (al * v + bl) for v in AUDIT_VS}
            sup = (float(pl[:, 1].min()), float(pl[:, 1].max()),
                   float(pr[:, 1].min()), float(pr[:, 1].max()))
            # B：同 d 分箱 du_obs vs du_fit（每侧 ≥2 点、粗箱——8 点/侧的细箱
            # 在虚线碎块密度下无数据，首跑实证）
            bins = []
            for lo, hi in zip(AUDIT_DBINS, AUDIT_DBINS[1:]):
                ml = (pl[:, 1] - cal["y_h"] >= lo) & (pl[:, 1] - cal["y_h"] < hi)
                mr = (pr[:, 1] - cal["y_h"] >= lo) & (pr[:, 1] - cal["y_h"] < hi)
                if ml.sum() < 2 or mr.sum() < 2:
                    continue
                vm = float(np.median(np.r_[pl[ml, 1], pr[mr, 1]]))
                du_obs = float(np.median(pr[mr, 0]) - np.median(pl[ml, 0]))
                du_fit = (ar * vm + br) - (al * vm + bl)
                if du_fit > 20:
                    bins.append((vm - cal["y_h"], du_obs / du_fit))
            recs.append({"seq": r["seq"], "dx": dx, "sup": sup, "bins": bins})
        if not recs:
            print(f"== {s}: 可审计帧 0")
            continue
        # 聚合
        ratios = np.array([[rec["dx"][v] / rec["dx"][650.0] for v in AUDIT_VS]
                           for rec in recs])
        med_ratio = np.median(ratios, 0)
        in_sup = np.mean([[rec["sup"][0] <= 650 <= rec["sup"][1]
                           and rec["sup"][2] <= 650 <= rec["sup"][3]]
                          for rec in recs])
        bb = {}
        for rec in recs:
            for d, r in rec["bins"]:
                for k in (100, 250, 400, 700):
                    if d < k:
                        bb.setdefault(k, []).append(r)
                        break
        bmed = {k: (float(np.median(v)), len(v)) for k, v in sorted(bb.items())
                if len(v) >= 5}
        print(f"== {s}: 审计帧 {len(recs)}")
        print(f"   A Δx(v)/Δx(650) 中位 @ {AUDIT_VS}:\n"
              f"     {[round(float(x), 3) for x in med_ratio]}"
              f"  （v=650 双族支持内占比 {in_sup:.0%}）")
        print(f"   B du_obs/du_fit 中位 @d<{{100,250,400,700}}: "
              + "  ".join(f"d<{k}: {v:.3f}(n={n})" for k, (v, n) in bmed.items()))
        all_A.append(med_ratio)
        all_B.append(bmed)
        all_sup.append(in_sup)
        report[s] = {"n_frame": len(recs),
                     "A_ratio": [round(float(x), 4) for x in med_ratio],
                     "B": {str(k): v for k, v in bmed.items()},
                     "in_support_frac": float(in_sup)}
    if all_A:
        A = np.median(np.array(all_A), 0)
        print(f"\n== 跨场 A：Δx(v)/Δx(650) = "
              f"{[round(float(x), 3) for x in A]}  最大漂移 "
              f"{(A.max() - A.min()) * 100:.1f}%（阈 ±5%）→ "
              f"{'固定行锚不稳，ax_physical 需重定义' if abs(A - 1).max() > 0.05 else '固定行锚稳定'}")
        print(f"   v=650 在双族支持区间内占比中位 {np.median(all_sup):.0%}")
        flatB = {}
        for b in all_B:
            for k, (v, n) in b.items():
                flatB.setdefault(k, []).append(v)
        print("== 跨场 B：du_obs/du_fit = "
              + "  ".join(f"d<{k}: {np.median(v):.3f}(n={len(v)})"
                          for k, v in sorted(flatB.items())))
    (CACHE / "gate2_audit.json").write_text(json.dumps(report, ensure_ascii=False),
                                             encoding="utf-8")


# ---------- 全局车道格阵 L 估计（维护者裁定 2026-09-19 冻结版） ----------
# 假设类批准：等距车道周期 L 跨观测联合恢复；φ_f 为逐帧 nuisance 相位——
# 不设全局 φ（弯道/航向变化会让同一物理边界的参考行位置逐帧漂移），不预设
# 左右对称（边界 ∈ φ_f+(k+1/2)·L 半整数格，k 整数，道路拓扑不进算法）。
# 输入冻结：audit 同款筛帧（比赛态 + score 窗 + 变道窗剔除 + 直道门控 +
# 跨线剔除）；帧内取**全部**合格线族（≥MIN_CLUSTER_SEGS 段、矢高≤SAG_MAX、
# v 支持覆盖 V_REF±10px，否则外推不可信）的拟合线 u@650——只取左右最近族
# 会把「逐帧配对」假设从输入端带回，与「缺失线/伪线/单边可见下恢复 L」的
# 目标矛盾（几何必要性，非过闸调参）。
# 估计规则（先写死，禁止为过闸搜索阈值）：
#   候选 L：仅同帧线对 × m∈{1..4}（跨帧对含未知相位差，不构成 L 样本），
#     L=|Δu|/m ∈ [350,1100]px（A_x∈[0.30,0.93] 几何合理域），1px 去重；
#   残差阈 τ=15px：审计 B 实测 du_obs/du_fit 偏离 ±2%×~670px≈±13px 的
#     散布底，不得再调；
#   得分：n_obs≥2 的帧上，逐帧最优相位（t 扫 {c_i, c_i±τ} 断点集，c=u mod L；
#     并列取内点残差和最小）下的内点率 r_f，跨帧取均值——按帧去重，碎片簇
#     不得放大投票权；单线帧对任意 L 恒可解释，不计分；
#   谐波破简并：argmax 只会落在真值的某个约分数 L/n 上（L/n 格 ⊇ L 格，
#     得分随格细化单调不减）；在 L_top 的整数倍栈 m·L_top（m≤6）上取
#     **最后一个**得分落差 ≤TIE 的倍数。TIE=0.05 两侧有余量：过走风险=
#     2L 格上相邻边界对内点率 ≤0.5（落差 ≥0.15）；欠走风险=细格伪线意外
#     内点膨胀 ≈2τ/L·n_伪/n_obs ≈ 0.01~0.03/帧；
#   细化：L* 下同帧双内点对，m̂=round(|Δu|/L*)∈[1,6] 且 ||Δu|/m̂−L*|≤2τ，
#     L_final=中位。
# 输出：A_x = (V_REF − y_h)/L（与 ax_physical 同一源公式，修 source 不修
#   output），source 优先级格阵 > 物理 > 晶格；668px 人工配对只做事后审计
#   锚，不参与拟合、不参与参数选择、不参与阈值确定。
GLOBAL_L_BAND = (350.0, 1100.0)
GLOBAL_L_TAU = 15.0
GLOBAL_L_TIE = 0.05
GLOBAL_L_MS = (1, 2, 3, 4)
GLOBAL_L_MWALK = 6
V_SUP_MARGIN = 10.0


def _frame_u650(fams) -> list[float]:
    """帧内全部合格线族的拟合 u@650（v 支持须覆盖 V_REF±10px）。"""
    out = []
    for _, pts, nseg in fams:
        if nseg < MIN_CLUSTER_SEGS or sagitta(pts) > SAG_MAX:
            continue
        if pts[:, 1].min() > V_REF - V_SUP_MARGIN \
                or pts[:, 1].max() < V_REF + V_SUP_MARGIN:
            continue
        a, b = np.polyfit(pts[:, 1], pts[:, 0], 1)
        u = float(a * V_REF + b)
        if 0 < u < 1280:
            out.append(u)
    return out


def _frame_fit(us: list[float], L: float) -> tuple[int, np.ndarray]:
    """帧内最优相位 →（内点数, 内点掩码）。t 只扫 c_i±τ 断点集即完备。"""
    c = np.mod(us, L)
    tc = np.unique(np.round(np.r_[c, c - GLOBAL_L_TAU, c + GLOBAL_L_TAU] % L, 3))
    best = (0, None, float("inf"))
    for t in tc:
        d = np.abs(c - t)
        d = np.minimum(d, L - d)
        mk = d <= GLOBAL_L_TAU
        n_in, s = int(mk.sum()), float(d[mk].sum())
        if n_in > best[0] or (n_in == best[0] and s < best[2]):
            best = (n_in, mk, s)
    return best[0], best[1]


def _grid_score(frames_us: list[list[float]], L: float) -> float:
    """按帧去重的格一致性得分：n_obs≥2 帧的内点率均值。"""
    r = [_frame_fit(us, L)[0] / len(us) for us in frames_us if len(us) >= 2]
    return float(np.mean(r)) if r else 0.0


def global_L(sessions: list[str], stride: int = 2) -> None:
    base = load_base()
    if not base:
        print("无冻结基准——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    report = {}
    for s in sessions:
        t0 = time.perf_counter()
        rows, _ = analyze_series(s, stride)
        rows = staged_filter(rows, base["base"], score_intervals(s))
        race = [r for r in rows if r["stage"] == 4]
        if len(race) < 15:
            print(f"== {s}: 比赛态帧 {len(race)} 不足，跳过")
            continue
        ivs = score_intervals(s) or [(race[0]["ts"], race[-1]["ts"])]
        lc = lane_change_times(s)
        mask = straight_mask(s, rows)
        sess = DEMOS / s
        frames_us: list[list[float]] = []
        for i, r in enumerate(rows):
            if not (any(a <= r["ts"] <= b for a, b in ivs)
                    and not any(r["ts"] >= t - LANE_CHANGE_PRE
                                and r["ts"] <= t + LANE_CHANGE_POST
                                for t in lc)):
                continue
            if not mask[i]:
                continue
            rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
            fams = line_families(rgb)
            if any(abs(f[0] - SCREEN_CX) < STRADDLE_PX for f in fams):
                continue
            us = _frame_u650(fams)
            if us:
                frames_us.append(us)
        n2 = sum(len(us) >= 2 for us in frames_us)
        print(f"== {s}: 格阵帧 {len(frames_us)}（≥2 线 {n2}）", flush=True)
        if n2 < 10:
            print("   计分帧不足，跳过")
            continue
        cand = set()
        for us in frames_us:
            for a in range(len(us)):
                for b2 in range(a + 1, len(us)):
                    for m in GLOBAL_L_MS:
                        L = abs(us[b2] - us[a]) / m
                        if GLOBAL_L_BAND[0] <= L <= GLOBAL_L_BAND[1]:
                            cand.add(round(L))
        if not cand:
            print("   无候选 L，跳过")
            continue
        scored = [(_grid_score(frames_us, float(L)), float(L))
                  for L in sorted(cand)]
        s_top, L_top = max(scored)
        L_star, m_walk = L_top, 1
        for m in range(2, GLOBAL_L_MWALK + 1):
            Lm = L_top * m
            if Lm > GLOBAL_L_BAND[1]:
                break
            if _grid_score(frames_us, Lm) >= s_top - GLOBAL_L_TIE:
                L_star, m_walk = Lm, m
        # 细化：同帧双内点对
        pairs = []
        for us in frames_us:
            if len(us) < 2:
                continue
            _, mk = _frame_fit(us, L_star)
            iu = [u for u, k in zip(us, mk) if k]
            for a in range(len(iu)):
                for b2 in range(a + 1, len(iu)):
                    du = abs(iu[b2] - iu[a])
                    mh = round(du / L_star)
                    if 1 <= mh <= GLOBAL_L_MWALK \
                            and abs(du / mh - L_star) <= 2 * GLOBAL_L_TAU:
                        pairs.append(du / mh)
        L_final = float(np.median(pairs)) if pairs else L_star
        harm = {f"{k}": round(_grid_score(frames_us, L_final * k), 3)
                for k in (0.5, 1.0, 1.5, 2.0)}
        rec = {"n_frame": len(frames_us), "n_frame_scored": n2,
               "n_obs_med": float(np.median([len(us) for us in frames_us])),
               "L_top": round(L_top, 1), "score_top": round(s_top, 3),
               "m_walk": m_walk, "L_star": round(L_star, 1),
               "n_pairs": len(pairs),
               "L_final": round(L_final, 1),
               "L_iqr": [round(float(np.percentile(pairs, q)), 1)
                         for q in (25, 75)] if pairs else None,
               "harmonic_score": harm,
               "anchor_668_ratio": round(L_final / 668.0, 4),
               "sec": round(time.perf_counter() - t0, 1)}
        print(f"   L_top={L_top:.0f}(score {s_top:.3f}) → 栈上 m={m_walk} "
              f"L*={L_star:.0f} → 细化 L={L_final:.1f}"
              f"（n={len(pairs)}，IQR {rec['L_iqr']}）")
        print(f"   谐波审计 score@L{{/2,·1,·1.5,·2}}={harm}  "
              f"668 锚比 {rec['anchor_668_ratio']:.3f}（事后审计，不入拟合）"
              f"  [{rec['sec']}s]")
        report[s] = rec
    if report:
        Ls = [r["L_final"] for r in report.values()]
        print(f"\n== 跨场 L：{[round(x) for x in Ls]}  中位 {np.median(Ls):.1f}"
              f"  CV {np.std(Ls) / np.mean(Ls):.1%}")
    (CACHE / "gate2_globalL.json").write_text(json.dumps(report,
                                                         ensure_ascii=False),
                                              encoding="utf-8")
    print(f"缓存 → {CACHE / 'gate2_globalL.json'}")


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
                    help="（历史模式，维护者裁定后不再用于定级）")
    ap.add_argument("--holdout", nargs="+", metavar="VAL",
                    help="留场验证场次（冻结参数，只验不改）")
    ap.add_argument("--validate", action="store_true",
                    help="正式口径：几何定尺度 + AI 轨交叉验证（A_x 冻结）")
    ap.add_argument("--audit", action="store_true",
                    help="仪器审计：ax_physical 与 s_med 的 9% 分歧定位（不动参数）")
    ap.add_argument("--globalL", action="store_true",
                    help="全局车道格阵 L 估计（维护者裁定冻结版：φ_f 逐帧、"
                         "m∈1..4 枚举、按帧去重；输出 A_x=(V_REF−y_h)/L 新 source）")
    args = ap.parse_args()
    if args.globalL:
        global_L(args.sessions)
    elif args.audit:
        audit_spacing(args.sessions)
    elif args.validate:
        validate(args.sessions)
    elif args.rescale:
        rescale_holdout(args.rescale, args.holdout or args.sessions)
    else:
        main(args.sessions)
