"""Gate 3 · #4 速度流：累计距离回归 + (v0, A_z, Z0) 联合拟合。

模型（地面点静止、自车前进 s）：Z(t)=Z0−s(t)，d=v−v0=A_z/Z
  ⇒ 1/d(t) = (Z0 − s)/A_z，对 s 严格线性，斜率 = −1/A_z。
x 轴用累计距离 s=∫V dt（V 取 HUD 本机「得分速度」，兼容加减速）；v0 作未知量联合拟合
（顺手标定地平线行）；地面纹理 KLT 光流跟踪（车辆会成离群点，稳健拟合踢掉）。
A_z 是常量 → 靠池化上百条轨迹取胜，不靠每帧检出率。
用法：.venv\\Scripts\\python.exe tools/experiments/speedrush_calibration/probe_gate3_4_speedflow.py <session>
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


def ego_speed_series(sess):
    """hud.jsonl 本机(蓝) rate → (ts, V) 单调表。"""
    ts, vs = [], []
    for line in (sess / "hud.jsonl").read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        h = json.loads(line)
        f = h["fields"]
        v = None
        for k in ("rate_a", "rate_b"):
            r = f.get(k) or {}
            if r.get("side") == "本机(蓝)" and r.get("trusted") and isinstance(r.get("value"), (int, float)):
                v = float(r["value"])
        if v is not None:
            ts.append(h["ts_ns"] / 1e9); vs.append(v)
    return np.array(ts), np.array(vs)


def frames(sess):
    return [json.loads(x) for x in (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]


def track_ground(sess_name, y_h, vpx, max_new=100, min_len=20):
    """KLT 跟踪路面纹理点。返回 trajs{tid:[(frame_i,u,v)]} 与 (tss, s)。"""
    sess = DEMOS / sess_name
    meta = frames(sess)
    vts, vs = ego_speed_series(sess)
    tss = np.array([m["ts_ns"] / 1e9 for m in meta])
    V = np.interp(tss, vts, vs)
    # 得分速度 = 真车速 + 得分连击尖峰（尖峰是瞬态上冲）→ 取下包络（滚动最小）还原真车速
    win = max(3, int(3.0 / np.median(np.diff(tss))))       # ~3s 窗
    V = np.array([np.min(V[max(0, i - win):i + 1]) for i in range(len(V))])
    dt = np.diff(tss, prepend=tss[0])
    s = np.cumsum(V * dt)
    good = V > 3.0
    tracks = {}
    nid = 0
    edge_fail = [0]
    prev_gray = prev_pts = prev_ids = None
    for fi, m in enumerate(meta):
        if not good[fi]:
            prev_gray = prev_pts = prev_ids = None
            continue
        img = np.array(Image.open(sess / "frames" / m["file"]).convert("RGB"))
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        # 只跟路面内部（两条黄边界之间、地平线下）——排除两侧护栏立面（不遵地面平面流）
        eL, eR = o3._fit_yellow_edges(img, y_h, vpx)
        if eL is None or eR is None:
            edge_fail[0] += 1        # 黄边拟合失败→掩膜整幅放开→路边景物进跟踪（候选污染源）
        mask = np.zeros_like(gray)
        for v in range(int(y_h + 40), 719):
            lo = int(eL[0] * v + eL[1]) + 10 if eL else 0
            hi = int(eR[0] * v + eR[1]) - 10 if eR else 1280
            lo = max(0, min(1279, lo)); hi = max(0, min(1279, hi))
            if hi > lo:
                mask[v, lo:hi] = 255
        mask[int(y_h + 40):, 440:820] = 0          # 排除自车
        cur_pts, cur_ids = [], []
        if prev_pts is not None and len(prev_pts):
            nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, prev_pts, None)
            nxt2 = nxt.reshape(-1, 2)
            for k, ok in enumerate(st.ravel()):
                if ok and y_h + 40 < nxt2[k][1] < 719 and 0 <= nxt2[k][0] < 1280:
                    tid = prev_ids[k]
                    tracks[tid].append((fi, float(nxt2[k][0]), float(nxt2[k][1])))
                    cur_pts.append(nxt2[k]); cur_ids.append(tid)
        new = cv2.goodFeaturesToTrack(gray, maxCorners=max_new, qualityLevel=0.01,
                                      minDistance=12, mask=mask)
        if new is not None:
            taken = np.array(cur_pts).reshape(-1, 2) if cur_pts else np.zeros((0, 2))
            for pt in new.reshape(-1, 2):
                if len(taken) and np.min(np.hypot(taken[:, 0] - pt[0], taken[:, 1] - pt[1])) < 10:
                    continue
                tracks[nid] = [(fi, float(pt[0]), float(pt[1]))]
                cur_pts.append(pt); cur_ids.append(nid); nid += 1
        if cur_pts:
            prev_gray = gray
            prev_pts = np.array(cur_pts, np.float32).reshape(-1, 1, 2)
            prev_ids = cur_ids
        else:
            prev_gray = prev_pts = prev_ids = None
    trajs = {}
    for t, tr in tracks.items():
        if len(tr) < min_len:
            continue
        dv = tr[-1][2] - tr[0][2]                       # 净向下流（近处 v 大）
        ds = s[tr[-1][0]] - s[tr[0][0]]                 # 该点存续期内自车走过的距离
        if dv >= 60 and 15 <= ds <= 200:                # 材质点：单调下移、寿命有限
            trajs[t] = tr
    return trajs, tss, s, edge_fail[0], int(good.sum())


def fit(trajs, tss, s, y_h, max_tracks=3000):
    """联合拟合 1/(v−v0) = a_track − s/A_z。用「组内去均值」消掉每轨截距，
    只剩共享斜率 → O(N) 内存（避免 N×T 稠密设计矩阵爆内存）。网格搜 v0。"""
    trajs = list(trajs.items())[:max_tracks]
    obs = []   # (tid, s, v_pix)
    for tid, tr in trajs:
        for fi, u, vp in tr:
            obs.append((tid, s[fi], vp))
    if len(obs) < 50:
        return None
    tids = sorted({o[0] for o in obs}); tidx = {t: i for i, t in enumerate(tids)}
    T = len(tids)
    sv = np.array([o[1] for o in obs]); vv = np.array([o[2] for o in obs])
    ti = np.array([tidx[o[0]] for o in obs])
    # 组内去均值（对 s）：s_c = s − mean_track(s)
    sbar = np.zeros(T); cnt = np.zeros(T)
    np.add.at(sbar, ti, sv); np.add.at(cnt, ti, 1)
    sbar /= np.maximum(cnt, 1)
    s_c = sv - sbar[ti]
    best = None
    for v0 in np.arange(180.0, 345.0, 1.0):
        d = vv - v0
        if (d < 20).any():
            continue
        y = 1.0 / d
        ybar = np.zeros(T); np.add.at(ybar, ti, y)
        ybar /= np.maximum(cnt, 1)
        y_c = y - ybar[ti]
        denom = np.sum(s_c * s_c)
        if denom <= 0:
            continue
        slope = np.sum(y_c * s_c) / denom          # = −1/A_z
        if slope >= 0:
            continue
        az = -1.0 / slope
        pred_c = slope * s_c
        ss_res = np.sum((y_c - pred_c) ** 2)
        ss_tot = np.sum((y_c - y_c.mean()) ** 2)
        r2 = 1 - ss_res / ss_tot
        resid = y_c - pred_c
        if best is None or r2 > best[0]:
            best = (r2, v0, az, resid, ti, sv, vv, T)
    return best


def main(sess_name):
    base = load_base()
    rows = staged_filter(g3.analyze_series1(sess_name), base["base"], score_intervals(sess_name))
    cal = get_cal(sess_name, [r for r in rows if r["stage"] == 4])
    y_h, vpx = cal["y_h"], cal["vpx"]
    trajs, tss, s, edge_fail, n_good = track_ground(sess_name, y_h, vpx)
    print(f"{sess_name}: 轨迹 {len(trajs)} 条（≥20帧），时长 {tss[-1]-tss[0]:.0f}s，"
          f"累计 s={s[-1]:.0f}（HUD分·s，标度[三方来源·待复核]）", flush=True)
    print(f"  黄边拟合失败 {edge_fail}/{n_good} 帧（{edge_fail/max(n_good,1):.0%}）——"
          f"失败帧掩膜整幅放开，路边景物可进跟踪（裁定三(b)候选污染源）", flush=True)
    b = fit(trajs, tss, s, y_h)
    if not b:
        print("拟合失败（样本不足）"); return
    r2, v0, az, resid, ti, sv, vv, T = b
    N = len(resid)
    # 组内去均值后的斜率标准误
    sbar = np.zeros(T); cnt = np.zeros(T)
    np.add.at(sbar, ti, sv); np.add.at(cnt, ti, 1); sbar /= np.maximum(cnt, 1)
    s_c = sv - sbar[ti]
    se_slope = np.sqrt(np.sum(resid**2) / max(N - T, 1) / max(np.sum(s_c**2), 1))
    se_az = az * az * se_slope
    print(f"\n== #4 速度流（{sess_name}）==")
    print(f"  A_z = {az:.0f} ± {se_az:.0f}   （先验 4500，比 {az/4500:.2f}×）")
    print(f"  v0  = {v0:.1f}   （现用 y_h={y_h:.1f}，差 {v0-y_h:+.1f}px）")
    print(f"  R²  = {r2:.4f}   轨迹 {T}   观测 {N}")
    print("  注：A_z=f·H 的乘积可辨识，f 与 H 单独不可辨识（维护者裁定 2026-09-20，"
          "h=1.98 系被拒绝的 3.5m 车道先验换皮，已撤）")
    Z = az / (vv - v0)
    order = np.argsort(Z)
    bins = np.array_split(order, 6)
    print("  残差(1/d)按Z分箱中位:", [round(float(np.median(resid[bi])), 5) for bi in bins])
    print("  Z分箱中位(m):", [round(float(np.median(Z[bi])), 1) for bi in bins])


def qstat(sess_name):
    """裁定三(b)：#4 的 KLT 轨投进轨内加速度比 q 坐标系（物理=1/滚动=0，dt 归一），
    并逐轨出身份图。只报身份结论，不报 A_z（v0 打网格边界 ⟹ A_z 是边界解非估计值）。"""
    import probe_gate3_scroll as ps
    base = load_base()
    rows = staged_filter(g3.analyze_series1(sess_name), base["base"], score_intervals(sess_name))
    cal = get_cal(sess_name, [r for r in rows if r["stage"] == 4])
    y_h, vpx = cal["y_h"], cal["vpx"]
    trajs, tss, s, ef, ng = track_ground(sess_name, y_h, vpx)
    recs = []
    for tid, tr in trajs.items():
        if len(tr) < 6:
            continue
        st = ps.shape_test(tr, tss, y_h)
        recs.append((tid, tr, st["slope_vd"] if st["power"] else float("nan"), st))
    qs = np.array([r[2] for r in recs])
    ok = ~np.isnan(qs)
    print(f"== #4 KLT 轨内 log v–log d 斜率（物理=2/滚动=0）n_轨={len(qs)}，"
          f"有判别力 {int(ok.sum())}（跨度≥1.5×）==")
    qv = qs[ok]
    if len(qv):
        print(f"  分位: p10={np.percentile(qv,10):.2f} p25={np.percentile(qv,25):.2f} "
              f"median={np.median(qv):.2f} p75={np.percentile(qv,75):.2f} p90={np.percentile(qv,90):.2f}")
        print(f"  <0.8 滚动族 {np.mean(qv<0.8):.0%} | 0.8~1.2 不可分 "
              f"{np.mean((qv>=0.8)&(qv<1.2)):.0%} | 1.2~4 物理向 {np.mean((qv>=1.2)&(qv<=4)):.0%} "
              f"| >4 超物理·异常 {np.mean(qv>4):.0%}")
    sess = DEMOS / sess_name
    meta = frames(sess)
    for tag, group in (("lowq", sorted([r for r in recs if not np.isnan(r[2]) and r[2] < 0.8],
                                       key=lambda r: -len(r[1]))[:4]),
                       ("highq", sorted([r for r in recs if not np.isnan(r[2]) and 1.2 <= r[2] <= 4],
                                        key=lambda r: -len(r[1]))[:4])):
        if not group:
            print(f"{tag}: 无轨"); continue
        ims = []
        for tid, tr, q, st in group:
            im = Image.open(sess / "frames" / meta[tr[0][0]]["file"]).convert("RGB")
            dr = ImageDraw.Draw(im)
            pts = [(u, v) for _, u, v in tr]
            dr.line(pts, fill=(255, 0, 0), width=2)
            dr.ellipse([pts[-1][0] - 6, pts[-1][1] - 6, pts[-1][0] + 6, pts[-1][1] + 6],
                       outline=(0, 255, 0), width=3)
            crop = im.crop((0, int(y_h), 1280, 719))
            ImageDraw.Draw(crop).text(
                (6, 6), f"tid={tid} slope_vd={q:.2f} len={len(tr)} d {st['d_span'][0]:.0f}->{st['d_span'][1]:.0f}",
                fill=(255, 255, 255))
            ims.append(crop)
        canvas = Image.new("RGB", (1280, sum(i.height for i in ims) + 8 * len(ims)), (16, 16, 16))
        yy = 0
        for i in ims:
            canvas.paste(i, (0, yy)); yy += i.height + 8
        p = CACHE / f"gate3_4_{tag}_{sess_name}.png"
        canvas.save(p)
        print(f"身份图 {tag}（红线=轨全程、绿圈=轨末点）：{p}")


if __name__ == "__main__":
    sess = sys.argv[1] if len(sys.argv) > 1 else "20260919_211329_p2"
    if len(sys.argv) > 2 and sys.argv[2] == "qstat":
        qstat(sess)
    else:
        main(sess)
