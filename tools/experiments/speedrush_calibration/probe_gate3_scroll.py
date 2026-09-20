"""3R-1 前置 · 滚动分叉终审（README「待裁定三路」之 ①人眼终审 + 形状检验复验）。

要裁定的分叉（README 假设分叉）：虚线的时间流动是
  [物理透视]  地面点 ḋ = (V/A_z)·d² ⇒ 1/d 对 t 线性、d 对 t 超线性（凸）
  [屏幕滚动]  贴图传送带      ⇒ v（或 d）对 t 严格线性
形状检验与 v、A_z 的具体取值无关（纯模型族判别）。README 那条铁证
（d 序列 203→352 步长 ~30 线性 rms 1-2px）出自 probe_gate3_az 的 **Hough 前端**，
而 3R-0 已证 Hough 候选混入实线——本探针在**已认证虚线对象**（dash 前端 + 四条
冻结语义）上复验，顺带产出人眼终审素材：
  ① 相邻帧大图对照（选中虚线绿框 + 序号）；
  ② 沿车道线采样的时空图（虚线轨迹直 = 滚动 / 凸 = 物理，人眼可辨）；
  ③ 逐轨判别表：d-线性 vs 1/d-线性 vs 物理族拟合 rms 对比。

用法：.venv\\Scripts\\python.exe tools/experiments/speedrush_calibration/probe_gate3_scroll.py <session> [run <seq_start> <n>]
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
import cv2
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import DEMOS, CACHE, load_base, score_intervals, staged_filter
from probe_gate2_hist import get_cal
import probe_gate3_0_object as o3
import probe_gate3_az as g3


def race_frames(sess_name):
    base = load_base()
    rows = g3.analyze_series1(sess_name)
    rows = staged_filter(rows, base["base"], score_intervals(sess_name))
    race = [r for r in rows if r["stage"] == 4]
    by_seq = {r["seq"]: r for r in race}
    return race, by_seq


def dash_obs(rgb, y_h, vpx, ax):
    recs = o3.classify(rgb, y_h, vpx, frontend="dash", ax=ax)
    return [r for r in recs if r["selected"]]


def link_tracks(segs_by_frame, y_h, max_gap=3):
    """跨帧贪心关联（允许遮挡缺口≤max_gap 帧；流速随 d² 增长故 dv 上限随 gap 放大）。

    返回 [(frame_i, um, vm)] 轨列表（观测不必逐帧连续，ts 由调用方按 frame_i 复原）。
    """
    tracks, live = [], []      # live: [last_fi, um, vm, obslist]
    for fi, segs in enumerate(segs_by_frame):
        live = [t for t in live if fi - t[0] <= max_gap]
        used, pairs = set(), []
        for ti, t in enumerate(live):
            g = fi - t[0]
            for si, s in enumerate(segs):
                dv = s["vm"] - t[2]
                du = abs(s["um"] - t[1])
                if 1.0 <= dv <= 130 * g and du <= 0.35 * dv + 18 * g:
                    pairs.append((du / (18 * g) + abs(dv) / (130 * g), ti, si))
        pairs.sort()
        matched_t = set()
        for cost, ti, si in pairs:
            if ti in matched_t or si in used or ti >= len(live):
                continue
            matched_t.add(ti); used.add(si)
            t = live[ti]
            t[0] = fi; t[1] = segs[si]["um"]; t[2] = segs[si]["vm"]
            t[3].append((fi, segs[si]["um"], segs[si]["vm"]))
        for si, s in enumerate(segs):
            if si not in used:
                seed = [(fi, s["um"], s["vm"])]
                live.append([fi, s["um"], s["vm"], seed])
                tracks.append(seed)
    return [tr for tr in tracks if len(tr) >= 4]


def shape_test(tr, tss, y_h):
    """逐轨三模型判别：物理族(ḋ=k d²)、屏幕滚动(d 线性)、自由二次。"""
    t = np.array([tss[i] - tss[tr[0][0]] for i, _, _ in tr])
    v = np.array([x[2] for x in tr])
    d = v - y_h
    out = {}
    for name, y in (("lin_d", d), ("lin_1/d", 1.0 / d)):
        c = np.polyfit(t, y, 1)
        out[name] = float(np.sqrt(np.mean((y - np.polyval(c, t)) ** 2)))
    c = np.polyfit(t, v, 2)
    out["quad_v"] = float(np.sqrt(np.mean((v - np.polyval(c, t)) ** 2)))
    k = np.polyfit(t, 1.0 / d, 1)[0]        # 物理族唯一自由参数 k=V/A_z
    pred = d[0] / np.maximum(1.0 - k * d[0] * t, 1e-6)
    out["phys_rms"] = float(np.sqrt(np.mean((d - pred) ** 2)))
    # 判决量（维护者裁定 2026-09-20 二次修正）：滚动族期望不是 0 而是 (d_s/d_e)²，
    # q 与跨度耦合 → 主判决量换为轨内 log v–log d 斜率（物理=2 / 滚动=0，跨度无关，
    # 与 vel 同层）。跨度 <1.5× 时斜率分母过小、无判别力（power=False，不入判决）。
    dt_s, dd_s = np.diff(t), np.diff(d)
    m = dt_s > 0                        # 重复时间戳步（KLT 轨会出现）掩掉
    vel = dd_s[m] / dt_s[m]
    dm = (d[:-1] + 0.5 * dd_s)[m]
    nf = max(1, min(2, len(vel) // 3))
    v_s, v_e = float(np.mean(vel[:nf])), float(np.mean(vel[-nf:]))
    d_s, d_e = float(np.mean(dm[:nf])), float(np.mean(dm[-nf:]))
    out["accel_ratio"] = v_e / max(v_s, 1e-9)
    out["phys_expect_ratio"] = (d_e / max(d_s, 1e-9)) ** 2
    out["d_span_ratio"] = d_e / max(d_s, 1e-9)
    out["slope_vd"] = (float(np.log(max(v_e, 1e-9) / max(v_s, 1e-9)) /
                             np.log(max(out["d_span_ratio"], 1.0001)))
                       if d_e > d_s else float("nan"))
    out["power"] = bool(out["d_span_ratio"] >= 1.5)
    fis = np.array([f for f, _, _ in tr])
    gd = np.diff(fis)
    out["gap_steps"] = [(int(i), int(g)) for i, g in enumerate(gd) if g > 1]
    out["d_span"] = (float(d[0]), float(d[-1]))
    out["len"] = len(tr)
    return out


def lane_v_hi(y_h, vpx, a):
    """收敛线在屏内（u∈[16,1264]）的最大 v 行。"""
    if a > 0:
        return min(716.0, y_h + (1264 - vpx) / a)
    if a < 0:
        return min(716.0, y_h + (16 - vpx) / a)
    return 716.0


def lane_profile(gr, y_h, vpx, a, half=3):
    """沿过 VP、斜率 a 的收敛线采样逐行亮度 → 返回 (v 数组, 亮度数组)。"""
    v_hi = lane_v_hi(y_h, vpx, a)
    v_rows = np.arange(int(y_h) + 8, int(v_hi)).astype(float)
    us = (vpx + a * (v_rows - y_h)).astype(int)
    prof = np.array([gr[int(vy), ux - half:ux + half + 1].mean()
                     for vy, ux in zip(v_rows, us)])
    return v_rows, prof


def st_image(frames_gray, y_h, vpx, a):
    """逐帧沿车道线亮度 → 时空图（T 行 × v 列）。虚线轨迹直=滚动、凸=物理透视。"""
    rows = [lane_profile(gr, y_h, vpx, a)[1] for gr in frames_gray]
    arr = np.vstack(rows)
    arr = (arr - arr.min()) / max(arr.max() - arr.min(), 1) * 255
    return arr.astype(np.uint8), None


def v_rows_global(y_h):
    return np.arange(int(y_h) + 8, 716)


def pair_shift(gray_a, gray_b, roi):
    """ROI 刚性互相关最佳平移 (dv, du) 与峰值强度（matchTemplate）。"""
    u0, u1, v0, v1 = roi
    a = gray_a[v0:v1, u0:u1].astype(np.float32)
    pad = 60
    bb = gray_b[max(v0 - pad, 0):min(v1 + pad, 719), u0:u1].astype(np.float32)
    if bb.shape[0] <= a.shape[0] + 4:
        return None
    res = cv2.matchTemplate(bb, a, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    dv = (v0 - pad) + loc[1] - v0          # 图案向下移为正
    return dv, float(mx)


def bin_flow(prof_a, prof_b, v_rows, d_lo, d_hi, y_h, search=80):
    """沿车道线亮度剖面的 d 带内最佳位移（px，向下游为正）。"""
    sel = (v_rows - y_h >= d_lo) & (v_rows - y_h <= d_hi)
    a = prof_a[sel]
    b = prof_b[sel]
    if len(a) < search + 10:
        return None
    best, bshift = -9, None
    for sh in range(-search, search + 1):
        if sh >= 0:
            x, yv = a[:len(a) - sh or None], b[sh:]
        else:
            x, yv = a[-sh:], b[:len(b) + sh]
        if len(x) < 20:
            continue
        cc = np.corrcoef(x, yv)[0, 1]
        if cc > best:
            best, bshift = cc, sh
    return bshift, float(best)


def pairs_image(sess_name, by_seq, s1, s2, obs1, obs2, title, path):
    ims = []
    for s, obs in ((s1, obs1), (s2, obs2)):
        im = Image.open(DEMOS / sess_name / "frames" / by_seq[s]["file"]).convert("RGB")
        dr = ImageDraw.Draw(im)
        for i, r in enumerate(obs):
            u1, v1, u2, v2 = r["sg"]
            dr.rectangle([(min(u1, u2) - 5, min(v1, v2) - 5),
                          (max(u1, u2) + 5, max(v1, v2) + 5)], outline=(0, 255, 0), width=2)
            dr.text((u2 + 8, (v1 + v2) / 2 - 8), str(i), fill=(0, 255, 255))
        dr.rectangle([(0, 0), (im.width, 20)], fill=(0, 0, 0))
        dr.text((4, 4), f"seq {s}", fill=(255, 255, 255))
        ims.append(im)
    canvas = Image.new("RGB", (1280 * 2 + 8, 720), (24, 24, 24))
    canvas.paste(ims[0], (0, 0)); canvas.paste(ims[1], (1288, 0))
    d2 = ImageDraw.Draw(canvas)
    d2.rectangle([(0, 720 - 20), (2568, 720)], fill=(0, 0, 0))
    d2.text((6, 720 - 16), title, fill=(255, 255, 255))
    canvas.save(path)


def main(sess_name, start=None, n=60, all_frames=False):
    race, by_seq = race_frames(sess_name)
    cal = get_cal(sess_name, race)
    y_h, vpx, ax = cal["y_h"], cal["vpx"], cal["A_x_used"]
    if all_frames:                       # 旧铁证窗多为非比赛态帧：用全帧（az 直线管线口径）
        import json as _json
        base = load_base()
        rows = staged_filter(g3.analyze_series1(sess_name), base["base"],
                             score_intervals(sess_name))
        by_seq = {r["seq"]: r for r in rows}
        seqs_all = sorted(by_seq)
    else:
        seqs_all = [r["seq"] for r in race]

    def window_at(i0):                      # 从第 i0 个比赛态帧起，缺口≤4 取满 n 帧
        w = [seqs_all[i0]]
        for s in seqs_all[i0 + 1:]:
            if s - w[-1] > 4:
                break
            w.append(s)
            if len(w) >= n:
                break
        return w

    if start is None:                       # 自动选窗：第一个能取满 ≥45 帧的窗口
        win = []
        for i in range(len(seqs_all) - 45):
            w = window_at(i)
            if len(w) >= 45:
                win = w
                break
        if not win:
            sys.exit("无 ≥45 帧连续比赛态窗口")
    else:
        i0 = next(i for i, s in enumerate(seqs_all) if s >= start)
        win = window_at(i0)
    win = sorted(win)
    meta = [by_seq[s] for s in win]
    tss = np.array([m["ts"] for m in meta])

    grays, rgbs, segs_by_frame, obs_store = [], [], [], {}
    for fi, m in enumerate(meta):
        rgb = np.array(Image.open(DEMOS / sess_name / "frames" / m["file"]).convert("RGB"))
        obs = dash_obs(rgb, y_h, vpx, ax)
        grays.append(rgb.mean(2)); rgbs.append(rgb)
        segs_by_frame.append(obs); obs_store[win[fi]] = obs

    print(f"== 3R-1 滚动终审（{sess_name} seq {win[0]}~{win[-1]}，y_h={y_h:.1f}，帧 {len(win)}，"
          f"dt≈{np.median(np.diff(tss)):.3f}s）==")

    # 车道线斜率投票：选中虚线（排除自车框内伪检）按 a=(um-vpx)/(vm-y_h) 聚类，
    # 取支持数最多的那条收敛线（自车右虚线或邻车道线，总之是画在路面上的图案线）
    from collections import Counter
    cnt = Counter()
    for obs in segs_by_frame:
        for r in obs:
            if r["d"] > 50 and not (440 <= r["um"] <= 820):
                cnt[round((r["um"] - vpx) / r["d"], 2)] += 1
    if not cnt:
        sys.exit("窗口内无框外虚线，换窗口")
    a_line, n_line = cnt.most_common(1)[0]
    print(f"主车道线斜率 a={a_line}（支持 n={n_line}）；全部簇 {cnt.most_common(4)}")

    # ① 逐轨形状（能成轨的部分：lin_d vs lin_1/d vs 物理族 rms 对比）
    tracks = link_tracks(segs_by_frame, y_h)
    print(f"虚线轨迹（长度≥4）：{len(tracks)}")
    for tr in sorted(tracks, key=len, reverse=True)[:12]:
        st = shape_test(tr, tss, y_h)
        sl = st["slope_vd"]
        verdict = ("无判别力(跨度<1.5)" if not st["power"] else
                   "超物理·异常单列" if sl > 4 else
                   "物理向" if sl >= 1.2 else
                   "滚动向" if sl < 0.8 else "不可分")
        obs_s = " ".join(f"{win[f]}:({u:.0f},{v:.0f})" for f, u, v in tr)
        print(f"  len={st['len']:3d} d {st['d_span'][0]:3.0f}→{st['d_span'][1]:3.0f}  "
              f"rms lin_d={st['lin_d']:5.1f} 物理族={st['phys_rms']:5.1f} quad_v={st['quad_v']:5.1f}  "
              f"slope_vd={sl:5.2f} 跨度={st['d_span_ratio']:.2f} → {verdict}")
        print(f"      {obs_s}")

    # ② 整带刚性互相关（屏幕滚动的定义性特征：全局平移峰）
    v_hi = lane_v_hi(y_h, vpx, a_line)
    u_c = int(np.clip(vpx + a_line * (v_hi - 20 - y_h), 130, 1150))
    roi = (u_c - 120, u_c + 120, int(y_h) + 40, int(v_hi))
    shifts = [pair_shift(grays[i], grays[i + 1], roi) for i in range(len(grays) - 1)]
    shifts = [x for x in shifts if x]
    if shifts:
        dvs = np.array([x[0] for x in shifts]); mxs = np.array([x[1] for x in shifts])
        strong = mxs > 0.5
        print(f"整带刚性相关：|Δv|中位 {np.median(dvs):.1f}px  峰值corr中位 {np.median(mxs):.2f}  "
              f"corr>0.5 占比 {strong.mean():.0%}（滚动=高占比；透视流=低占比/无全局峰）")

    # ③ 分 d 带沿车道线流速 c(d)：物理 c∝d²，滚动 c 与 d 无关
    print("  d带      c中位px  corr中位   物理期望相对c=(d/d带1)²")
    profs = [lane_profile(g, y_h, vpx, a_line) for g in grays]
    bins = [(50, 110), (110, 180), (180, 250)]
    d1 = np.mean(bins[0])
    for (lo, hi) in bins:
        cs = [bin_flow(profs[i][1], profs[i + 1][1], profs[i][0], lo, hi, y_h)
              for i in range(len(profs) - 1)]
        cs = [x for x in cs if x and x[1] > 0.3]
        if cs:
            cm = np.median([x[0] for x in cs])
            print(f"  {lo:3d}-{hi:3d}   {cm:6.1f}   {np.median([x[1] for x in cs]):.2f}   "
                  f"{((lo+hi)/2/d1)**2:5.1f}×")
        else:
            print(f"  {lo:3d}-{hi:3d}   无有效相关（流速超半周期→混叠或噪声）")

    # ④ 判决量（维护者裁定 2026-09-20 二次修正）：轨内 log v–log d 斜率
    #    （物理=2 / 滚动=0，跨度无关）；仅跨度≥1.5× 的轨有判别力、入判决。
    #    跨度分布必须随判决报出——无判别力轨不得计入任何一族。
    rmeta = [(tr, shape_test(tr, tss, y_h)) for tr in tracks]
    powr = [(tr, st) for tr, st in rmeta if st["power"] and not np.isnan(st["slope_vd"])]
    print(f"跨度分布（全轨）: " + " ".join(f"{st['d_span_ratio']:.2f}" for _, st in rmeta))
    if powr:
        s = np.array([st["slope_vd"] for _, st in powr])
        rng = np.random.default_rng(7)
        bs = [float(np.mean(rng.choice(s, len(s), replace=True))) for _ in range(2000)]
        se = float(np.std(bs))
        n_phys = int(np.sum((s >= 1.2) & (s <= 4))); n_roll = int(np.sum(s < 0.8))
        n_abn = int(np.sum(s > 4)); n_mid = len(s) - n_phys - n_roll - n_abn
        print(f"轨内 log v–log d 斜率（物理=2/滚动=0）：有判别力 n_轨={len(s)}  "
              f"median={np.median(s):.2f}  mean={s.mean():.2f}±{se:.2f}（bootstrap 按轨）")
        print(f"  分类：物理向 {n_phys} | 不可分 {n_mid} | 滚动向 {n_roll} | 超物理·异常 {n_abn}")
        for tr, st in powr:
            nst = st["len"] - 1
            gpos = ",".join(f"步{i}{'(首)' if i < 2 else '(尾)' if i >= nst - 2 else '(中)'}×{g}"
                            for i, g in st["gap_steps"]) or "无缺口"
            print(f"  {win[tr[0][0]]}→{win[tr[-1][0]]} d {st['d_span'][0]:.0f}→{st['d_span'][1]:.0f} "
                  f"len={st['len']} slope_vd={st['slope_vd']:.2f} 跨度={st['d_span_ratio']:.2f}  缺口[{gpos}]")
    else:
        print("无有判别力轨（全部跨度<1.5×）——本窗对分叉不发言")
    # 辅助图：c–d 双对数散点按轨分色 + 逐轨拟合线（池化 OLS 标注不作判决）
    pts = []
    for tr in tracks:
        for (f1, u1, v1), (f2, u2, v2) in zip(tr, tr[1:]):
            dt, dv = tss[f2] - tss[f1], v2 - v1
            vmid = (v1 + v2) / 2
            if 0.02 <= dt <= 0.25 and 2 <= dv <= 120 and \
                    not (440 <= (u1 + u2) / 2 <= 820 and 420 <= vmid <= 560):
                d = vmid - y_h
                if d >= 40:
                    pts.append((tr, d, dv / dt))
    if len(pts) >= 4:
        W, H = 720, 480
        im = Image.new("RGB", (W, H), (255, 255, 255)); dr = ImageDraw.Draw(im)
        ld = np.log10([p[1] for p in pts]); lc = np.log10([p[2] for p in pts])
        x0, x1 = min(ld) - 0.1, max(ld) + 0.1; y0, y1 = min(lc) - 0.3, max(lc) + 0.3
        def px(x, y): return (int((x - x0) / (x1 - x0) * (W - 90) + 70),
                              int(H - 50 - (y - y0) / (y1 - y0) * (H - 90)))
        for m, col in ((0, (150, 150, 150)), (2, (150, 150, 150))):
            ya, yb = lc.mean() + m * (x0 - ld.mean()), lc.mean() + m * (x1 - ld.mean())
            dr.line([px(x0, ya), px(x1, yb)], fill=col, width=3)
        id2c, ci = {}, 0
        for tr, x, y in pts:
            if id(tr) not in id2c:
                ci += 1
                id2c[id(tr)] = ((40 + 60 * ci) % 200, (30 * ci) % 180, (255 - 40 * ci) % 220)
            c = id2c[id(tr)]
            dr.ellipse([px(x, y)[0] - 4, px(x, y)[1] - 4, px(x, y)[0] + 4, px(x, y)[1] + 4], fill=c)
        sl, ic = np.polyfit(ld, lc, 1)
        dr.text((10, 8), f"c vs d log-log, per-track colors. pooled OLS slope={sl:.2f} "
                         f"(pseudo-replication, NOT verdict)", fill=(0, 0, 0))
        dr.text((10, 24), "grey lines: slope 0 = scroll | slope 2 = perspective", fill=(120, 120, 120))
        p = CACHE / f"gate3_scroll_slope_{sess_name}_{win[0]}.png"
        im.save(p)
        print(f"辅助散点图（按轨分色；池化 OLS 仅标注不作判决）：{p}")
    else:
        print(f"有效观测对不足（{len(pts)}），不出辅助图")
    dt_med = float(np.median(np.diff(tss)))
    k = max(2, int(round(0.66 / dt_med)))
    if len(grays) > k + 8:
        i0 = 8
        crops = []
        for i in (i0, i0 + k):
            im = Image.fromarray(rgbs[i].astype(np.uint8)).crop((400, 330, 900, 480))
            im = im.resize((im.width * 3, im.height * 3), Image.NEAREST)
            crops.append(im)
        canvas = Image.new("RGB", (crops[0].width, crops[0].height * 2 + 44), (16, 16, 16))
        canvas.paste(crops[0], (0, 22)); canvas.paste(crops[1], (0, crops[0].height + 44))
        d3 = ImageDraw.Draw(canvas)
        d3.text((6, 4), f"上 seq {win[i0]}  下 seq {win[i0+k]}（Δt={tss[i0+k]-tss[i0]:.2f}s，3×放大）", fill=(255, 255, 255))
        d3.text((6, crops[0].height + 26), "虚线整列下移且间距随 d 外扩=物理透视；整列纹丝不动=无流；刚性同距下移=屏幕滚动", fill=(255, 255, 0))
        p = CACHE / f"gate3_scroll_hz_{sess_name}_{win[i0]}_{win[i0+k]}.png"
        canvas.save(p)
        print(f"地平线带对照（人眼终审主图）：{p}")
    fi0 = max(range(len(segs_by_frame) - 6), key=lambda i: len(segs_by_frame[i]))
    s1, s2 = win[fi0], win[fi0 + 5]
    p = CACHE / f"gate3_scroll_pairs_{sess_name}_{s1}_{s2}.png"
    pairs_image(sess_name, by_seq, s1, s2, obs_store[s1], obs_store[s2],
                f"相邻帧对照 seq{s1} vs seq{s2}（绿框=选中虚线，直=滚动，外扩加速=物理）", p)
    print(f"相邻帧对照图：{p}")


if __name__ == "__main__":
    args = sys.argv[1:]
    sess = args[0]
    if len(args) >= 4 and args[1] == "run":
        main(sess, start=int(args[2]), n=int(args[3]), all_frames="all" in args[4:])
    else:
        main(sess)
