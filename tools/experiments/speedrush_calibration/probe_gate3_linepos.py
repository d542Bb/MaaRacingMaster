"""Gate 3 下一里程碑 · 线位机械化（终审裁定方案 (a)，2026-09-20，v3）。

目标：把「目测 a≈0.486 压在自车右虚线上」变成**可机检的逐帧线位读数**。

方案演进（如实留痕——三轮换的是假设的种类，不是参数）：
  v1 直道∧比赛态窗：22 帧/场，种子附近 ≥3 段为 0——直道与「有虚线检出的窗」
     在本素材近乎不相交（1268 窗直道占比 9%）；
  v2 比赛态窗 + 帧内/滑窗 a 带聚类跟踪：跟踪滑进右黄族（a(t) 中位 0.755、
     val 残差 10.8px，Q2 不验收）——终审「黄族主导拟合池」预警兑现；黄边邻接
     排除不可行：eR 全窗 VP 校验失败（dVP 356~544，⑤c「断裂」是常态非偶发）；
  v3（本文件）**逐轨整段 VP 约束拟合**：材料=四条语义检出段（预注册对象语义，
     3R-0）经 `link_tracks` 运动关联成的轨（同一对象的持续存在；关联器只用
     已认证对象的连续性，不用流动形态筛选对象——不违 3R-0 禁令）。每轨 ≥4
     观测：过 VP 最小二乘 a + 残差 px；**族归属交给②双峰性 + Q2 压线图，
     不靠 a 带宽**。

终审四防偏要求落点：① 拟合/验证=轨内按 d 分半（robust_halves）；② 窗=比赛态
连续窗（先于本仪器存在，非按检出筛）；③ 留出=fit/val 分半 + VP 权重扰动（逐轨
重拟合报 |Δa|）；④ 不确定度=段散布(MAD/√n) 与 val 半段散布取大，轨级区间非单值。

附带剖面法六步序之②③（对每条轨的线采样：BC/谷隙/实测带长）与 `--intersect`
的④模型预测（P×A_z 网格；非实测）。

用法：.venv\\Scripts\\python.exe tools/experiments/speedrush_calibration/probe_gate3_linepos.py <session> [seq_start] [n_frames] [--intersect]
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import DEMOS, CACHE, analyze_series, load_base, score_intervals, staged_filter
from probe_gate2_hist import get_cal, straight_mask
import probe_gate3_0_object as o3
import probe_gate3_scroll as ps
import probe_gate3_az as g3

D_MIN_FIT = 25.0
MIN_TRACK = 4           # 轨最少观测数（与 scroll 判决轨同门槛）
MAD2STD = 1.4826
BANDS = [(50, 110), (110, 180), (180, 250), (250, 350), (350, 500)]
A_ANCHOR = 0.486        # 预注册目测锚（⑤c）——只作压线对照，不作筛选


def robust_halves(obs, rng=None):
    """obs: [(a,d)] 段级 → (a_fit, unc, val_resid_px, n_fit, n_val)。①③④。"""
    obs = sorted(obs, key=lambda x: x[1])
    if rng is not None:
        obs = list(obs)
        rng.shuffle(obs)
    half = max(1, len(obs) // 2)
    fit_s, val_s = obs[:half], obs[half:]
    a_fit = float(np.median([a for a, _ in fit_s]))
    a_all = np.array([a for a, _ in obs])
    mad = MAD2STD * float(np.median(np.abs(a_all - np.median(a_all))))
    unc = mad / np.sqrt(max(len(obs), 1))
    if val_s:
        resid_px = float(np.median([abs(a - a_fit) * d for a, d in val_s]))
        unc = max(unc, MAD2STD * float(np.median([abs(a - a_fit) for a, _ in val_s])))
    else:
        resid_px = float("nan")
    return a_fit, unc, resid_px, len(fit_s), len(val_s)


def fit_track_vp(pts, y_h, vpx):
    """pts: [(u,v)] → VP 约束最小二乘 a + 残差 px + d 分半留出读数。"""
    vv = np.array([v - y_h for _, v in pts], float)
    uu = np.array([u - vpx for u, _ in pts], float)
    sel = vv >= D_MIN_FIT
    vv, uu = vv[sel], uu[sel]
    if len(vv) < 2:
        return None
    a = float(np.dot(uu, vv) / np.dot(vv, vv))
    resid = np.abs(uu - a * vv)
    obs = [(u / v, v) for u, v in zip(uu, vv)]
    a_h, unc, r_h, nf, nv = robust_halves(obs)
    return {"a": a, "res_med": float(np.median(resid)), "res_p75": float(np.percentile(resid, 75)),
            "n": int(len(vv)), "a_halves": a_h, "unc": unc, "resid_halves_px": r_h}


def profile_along(gr, y_h, vpx, a, v0, v1):
    vs = np.arange(int(v0), int(v1))
    us = np.clip((vpx + a * (vs - y_h)).astype(int), 2, gr.shape[1] - 3)
    return np.array([gr[v, u - 1:u + 2].mean() for v, u in zip(vs, us)])


def lane_v_in(a, y_h, vpx):
    if a > 1e-6:
        return min(716.0, y_h + (1264 - vpx) / a)
    if a < -1e-6:
        return min(716.0, y_h + (16 - vpx) / a)
    return 716.0


def bimod(vals):
    k = 15
    pad = np.pad(vals, (k // 2, k // 2), mode="edge")
    d = vals - np.array([np.median(pad[i:i + k]) for i in range(len(vals))])
    sd = float(np.std(d))
    if sd < 1e-6 or len(d) < 20:
        return float("nan"), float("nan")
    c = d - float(np.mean(d))
    m3 = float(np.mean(c ** 3) / sd ** 3)
    m4 = float(np.mean(c ** 4) / sd ** 4)
    bc = (m3 * m3 + 1.0) / m4 if m4 > 0 else float("nan")
    q = np.quantile(d, [0.02, 0.20, 0.80, 0.98])
    return bc, float((q[2] - q[1]) / max(q[3] - q[0], 1e-6))


def window_race(sess, start, n):
    base = load_base()
    rows = staged_filter(g3.analyze_series1(sess), base["base"], score_intervals(sess))
    race = [r for r in rows if r["stage"] == 4]
    seqs = [r["seq"] for r in race]
    groups, cur = [], [seqs[0]]
    for s in seqs[1:]:
        if s - cur[-1] > 4:
            groups.append(cur); cur = [s]
        else:
            cur.append(s)
    groups.append(cur)
    if start is not None:
        for g in groups:
            if g[-1] >= start:
                return g[next(i for i, s in enumerate(g) if s >= start):][:n], rows, race
        return [], rows, race
    for g in groups:
        if len(g) >= min(n, 30):
            return g[:n], rows, race
    return max(groups, key=len)[:n], rows, race


def main(sess, start=None, n=60, intersect=False):
    win, rows, race = window_race(sess, start, n)
    if len(win) < 10:
        sys.exit(f"窗不足：{len(win)}")
    win = sorted(win)
    bys = {r["seq"]: r for r in rows}
    # 直道占比如实报（剖面法 Q4 输入；掩码按 seq 桥接，与 stride2 缓存对齐）
    base = load_base()
    rows2, _ = analyze_series(sess, 2)
    rows2 = staged_filter(rows2, base["base"], score_intervals(sess))
    sm2 = straight_mask(sess, rows2)
    sms = {r["seq"]: bool(m) for r, m in zip(rows2, sm2)}
    frac = float(np.mean([sms.get(s, False) for s in win]))
    cal = get_cal(sess, race)
    y_h, vpx, ax = cal["y_h"], cal["vpx"], cal["A_x_used"]
    print(f"== 线位机械化(v3 逐轨) {sess}｜窗 seq {win[0]}~{win[-1]} n={len(win)}｜"
          f"直道占比 {frac:.0%}（Q4 输入）｜y_h={y_h:.1f} vpx={vpx:.1f} A_x={ax:.3f} ==")

    grays, segs_by_frame = [], []
    for s in win:
        rgb = np.array(Image.open(DEMOS / sess / "frames" / bys[s]["file"]).convert("RGB"))
        grays.append(rgb.mean(2))
        recs = [r for r in o3.classify(rgb, y_h, vpx, frontend="dash", ax=ax) if r["selected"]]
        segs_by_frame.append([{"um": float(np.mean([r["sg"][0], r["sg"][2]])),
                               "vm": float(np.mean([r["sg"][1], r["sg"][3]]))} for r in recs])
    tracks = ps.link_tracks(segs_by_frame, y_h)
    print(f"轨数（≥{MIN_TRACK} 观测，max_gap=3 同 scroll 口径）：{len(tracks)}")
    fits = []
    for tr in tracks:
        ft = fit_track_vp([(u, v) for _, u, v in tr], y_h, vpx)
        if ft and ft["n"] >= MIN_TRACK:
            ft["tr"] = tr
            fits.append(ft)
    fits.sort(key=lambda f: -f["n"])
    print("逐轨（a=VP约束LS；halves=d分半留出不确定性）：")
    print("  n  a      a_half unc    res_med res_p75 d跨度→  首seq 末seq |a-锚0.486|")
    for ft in fits:
        tr = ft["tr"]
        print(f"  {ft['n']:3d} {ft['a']:+.3f} {ft['a_halves']:+.3f} {ft['unc']:.3f}  "
              f"{ft['res_med']:6.1f} {ft['res_p75']:6.1f} {tr[0][2]-y_h:3.0f}→{tr[-1][2]-y_h:3.0f}  "
              f"{win[tr[0][0]]:5d} {win[tr[-1][0]]:5d}  {abs(ft['a']-A_ANCHOR):.3f}")

    # ③ VP 权重扰动（逐轨重拟合）
    vpd = []
    for dv, du in ((0, 2.0), (0, -2.0), (2.0, 0), (-2.0, 0)):
        for ft in fits:
            f2 = fit_track_vp([(u - du, v - dv) for _, u, v in ft["tr"]], y_h, vpx)
            if f2:
                vpd.append(abs(f2["a"] - ft["a"]))
    print(f"VP±2px 扰动：|Δa| max {np.nanmax(vpd) if vpd else float('nan'):.4f}（Q2 需≤0.01）")

    # ②③ 观测门：逐轨×带（覆盖帧中位）
    print("观测门②③（BC/谷隙/带长px(n覆盖帧)）：")
    band_meas = {}
    for ft in fits[:8]:
        a0 = ft["a"]
        fis = sorted({f for f, _, _ in ft["tr"]})
        line = f"  a={a0:+.3f} n={ft['n']:3d} |"
        for lo, hi in BANDS:
            bcs, gaps, lens = [], [], []
            for fi in fis:
                v0, v1 = y_h + lo, min(y_h + hi, lane_v_in(a0, y_h, vpx))
                if v1 - v0 < 25:
                    continue
                bc, gap = bimod(profile_along(grays[fi], y_h, vpx, a0, v0, v1))
                if np.isfinite(bc):
                    bcs.append(bc); gaps.append(gap); lens.append(v1 - v0)
            if bcs:
                line += f" {lo}-{hi}: {np.median(bcs):.2f}/{np.median(gaps):.2f}/{np.median(lens):.0f}({len(bcs)}) |"
                band_meas.setdefault((lo, hi), []).append(float(np.median(lens)))
            else:
                line += f" {lo}-{hi}: 出屏 |"
        print(line)

    # 压线图：前 4 轨彩色 + 预注册锚对照（身份判定交人眼/Q2）
    fi_rep = max(range(len(win)), key=lambda i: len(segs_by_frame[i]))
    im = Image.open(DEMOS / sess / "frames" / bys[win[fi_rep]]["file"]).convert("RGB")
    dr = ImageDraw.Draw(im)
    cols = [(0, 200, 0), (0, 140, 255), (255, 120, 0), (255, 0, 0)]
    for j, ft in enumerate(fits[:4]):
        a0 = ft["a"]
        v0, v_hi = y_h + D_MIN_FIT, lane_v_in(a0, y_h, vpx)
        col = cols[j % 4]
        dr.line([(vpx + a0 * (v0 - y_h), v0), (vpx + a0 * (v_hi - y_h), v_hi)], fill=col, width=2)
        vt = min(v_hi, y_h + 380)
        dr.text((vpx + a0 * (vt - y_h) + 6, min(vt, 700)), f"#{j} a={a0:+.3f} n={ft['n']}", fill=col)
    dr.line([(vpx + A_ANCHOR * 8, y_h + 8), (vpx + A_ANCHOR * (716 - y_h), 716)],
            fill=(255, 255, 0), width=1)
    p = CACHE / f"linepos_tracks_{sess}_{win[fi_rep]}.png"
    im.save(p)
    print(f"压线图（轨线彩色，黄点线=预注册目测锚 0.486，seq {win[fi_rep]}）：{p}")

    if intersect and band_meas:
        bl = {k: float(np.median(v)) for k, v in band_meas.items()}
        mids = np.array([(l + h) / 2 for (l, h) in bl]); lens = np.array([bl[k] for k in bl])
        order = np.argsort(mids); mids, lens = mids[order], lens[order]
        print("④有效区间模型预测（非实测；带长=③实测中位，period=P·d²/A_z，period≥9px=3px行×3 假设）：")
        for Az in (1500, 3000, 6000):
            row = f"  A_z={Az:5d}｜"
            for P in (10.0, 15.0, 20.0):
                d_ok = [d for d in np.arange(45, 560, 5.0)
                        if P * d * d / Az >= 9.0
                        and np.interp(d, mids, lens) >= 3 * P * d * d / Az]
                row += f" P={P:2.0f}: " + (f"[{d_ok[0]:.0f},{d_ok[-1]:.0f}]" if d_ok else "空") + "｜"
            print(row)
        print("  任一格非空 ⇒ 剖面法可放行至 period(d)；全空 ⇒ 弃用（红线不调参救）")
    elif intersect:
        print("④无实测带长可输入——观测门已空，本身即弃用信号")


if __name__ == "__main__":
    argv = sys.argv[1:]
    flags = [a for a in argv if a.startswith("--")]
    pos = [a for a in argv if not a.startswith("--")]
    sess = pos[0]
    main(sess, start=int(pos[1]) if len(pos) > 1 else None,
         n=int(pos[2]) if len(pos) > 2 else 60, intersect="--intersect" in flags)
