"""路线③ · 沿线亮度剖面相位法（README「待裁定三路」预注册路线，维护者裁定 2026-09-20）。

不做逐对象检测：沿过 VP 的直线积分亮度 → 高通 → 分 d 带互相关测流速 c(d)。
结构性优势（裁定原文）：碎片化不再致命（碎块合成周期剖面）、模糊不再致命（剖面更平滑）、
实线边缘白高光不再是对象（线框内是不随时间平移的偏置，不是移动相位）。

执行顺序（维护者裁定 2026-09-20 二次，替代初版「三条必修」并列实现）：
① 门标定（gatest）：已知实线 vs 已知虚线的 Hp.std 分布 → 定阈，阈值必须高于噪声底
   （初版 GATE_STD=5.0 低于噪声底，71/71 全过门=门未实现）；阴性对照随之产出。
② HP_WIN 随带自适应（∝ 局部周期 ∝ d²；固定窗对 d 50–500 跨 ~100× 周期不可能都对；
   注意：固定 25px 高通伤的是近带长周期，远带反而保留——初版归因方向已纠正）。
③ SEARCH 随带自适应（< 局部周期/2；初版 SEARCH=40 在 7–15px 周期带上跨 3–6 个周期，
   c=0.0/−135.7 是该缺陷的必然签名，不是噪声）。
④ 静态选线：线存在性 + Hp 自身周期调制强度（static_modulation，零跨帧量）。
   「用运动读数选线」已删除——与读数共用赢家诅咒。
⑤ 阳性 + 阴性对照：虚线线 c(d) 与 1268 窗物理向轨同量级；黄实线位置被门挡掉（不出读数）。

指标：判决窗内覆盖 d 段的 c(d) 曲线 + 每条读数附剖面证据图；不设轨数/检出率指标。
用法：.venv\\Scripts\\python.exe tools/experiments/speedrush_calibration/probe_gate3_profile.py <session> run <seq_start> <n> [all]
"""
from __future__ import annotations
import sys
import numpy as np
from PIL import Image, ImageDraw

HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import DEMOS, CACHE
from probe_gate2_hist import get_cal
import probe_gate3_scroll as ps

A_MIN, A_MAX, A_STEP = -3.5, 3.5, 0.10      # 候选线斜率（过 VP）
HP_WIN = 25                                  # 高通窗（px，沿剖面）
GATE_STD = 5.0                               # 对比度门：高通剖面 std 低于此不出读数
CORR_MIN = 0.35                              # 互相关最低采信
BANDS = [(50, 110), (110, 180), (180, 250), (250, 350), (350, 500)]
SEARCH = 40                                  # 位移搜索上限（px）
# 阳性对照（1268 窗有判别力轨的读数，px/s @ d）：
POS_CTRL = [(63, (20, 60)), (250, (120, 350))]


def profiles_for_line(frames_gray, y_h, vpx, a):
    """沿斜率 a 的收敛线逐帧取亮度剖面 → 高通。返回 (v_rows, Hp) 或 None（门挡）。"""
    v_hi = ps.lane_v_hi(y_h, vpx, a)
    v_rows = np.arange(int(y_h) + 8, int(min(v_hi, 716)))
    if len(v_rows) < 60:
        return None
    us = np.clip((vpx + a * (v_rows - y_h)).astype(int), 3, 1276)
    P = np.empty((len(frames_gray), len(v_rows)))
    for t, gr in enumerate(frames_gray):
        P[t] = [gr[v, u - 2:u + 3].mean() for v, u in zip(v_rows, us)]
    kern = np.ones(HP_WIN) / HP_WIN
    Hp = P - np.apply_along_axis(lambda x: np.convolve(x, kern, "same"), 1, P)
    if float(np.median(Hp.std(axis=1))) < GATE_STD:
        return None                            # 必修①：近常量剖面（实线/空域）不出读数
    return v_rows, Hp


def band_shift(hp_a, hp_b, sel, dt):
    """d 带内高通剖面互相关 → (c px/s, corr)。正位移=图案向大 v 移动。"""
    a, b = hp_a[sel], hp_b[sel]
    if len(a) < 40:
        return None
    best = (-9.0, None)
    for sh in range(-SEARCH, SEARCH + 1):
        x, y = (a[:len(a) - sh], b[sh:]) if sh >= 0 else (a[-sh:], b[:len(b) + sh])
        if len(x) < 25:
            continue
        sx, sy = x.std(), y.std()
        if sx < 1e-6 or sy < 1e-6:
            continue
        c = float(np.corrcoef(x, y)[0, 1])
        if c > best[0]:
            best = (c, sh)
    if best[1] is None or best[0] < CORR_MIN:
        return None
    return best[1] / dt, best[0]


def gatest(sess_name, start, n=54, lines=None):
    """新顺序步骤 1（维护者裁定 2026-09-20）：静态门标定。
    在已知实线（黄边线）与已知虚线（自车右虚线）位置分别测 Hp.std 分布，
    阈值取两分布之间；同时产出阴性对照（实线位置必须被门挡掉）。
    注：HP_WIN 尚未随带自适应，本标定是当前窗口的存在性证明；自适应窗落地后重标。"""
    race, by_seq = ps.race_frames(sess_name)
    cal = get_cal(sess_name, race)
    y_h, vpx = cal["y_h"], cal["vpx"]
    seqs_all = [r["seq"] for r in race]
    i0 = next(i for i, s in enumerate(seqs_all) if s >= start)
    win = [seqs_all[i0]]
    for s in seqs_all[i0 + 1:]:
        if s - win[-1] > 4 or len(win) >= n:
            break
        win.append(s)
    grays = [np.array(Image.open(DEMOS / sess_name / "frames" / by_seq[s]["file"]).convert("RGB")).mean(2)
             for s in win]
    lines = lines or [("右黄实线", 2.53), ("自车右虚线", 0.64), ("左黄实线", -0.84)]
    print(f"== 门标定（{sess_name} seq {win[0]}~{win[-1]}，HP_WIN={HP_WIN} 未自适应）==")
    for label, a in lines:
        pr = profiles_for_line(grays, y_h, vpx, a)
        if pr is None:
            print(f"  {label} a={a:+.2f}: 剖面构建失败（屏内行数不足）")
            continue
        v_rows, Hp = pr
        stds = Hp.std(axis=1)
        print(f"  {label} a={a:+.2f}: Hp.std 中位 {np.median(stds):5.1f} "
              f"p25 {np.percentile(stds,25):5.1f} p75 {np.percentile(stds,75):5.1f} "
              f"min {stds.min():5.1f} max {stds.max():5.1f}")


def run(sess_name, start, n=60, all_frames=False):
    race, by_seq = ps.race_frames(sess_name)
    cal = get_cal(sess_name, race)
    y_h, vpx = cal["y_h"], cal["vpx"]
    if all_frames:
        from probe_gate0_vp import load_base, score_intervals, staged_filter
        import probe_gate3_az as g3
        base = load_base()
        rows = staged_filter(g3.analyze_series1(sess_name), base["base"], score_intervals(sess_name))
        by_seq = {r["seq"]: r for r in rows}
        seqs_all = sorted(by_seq)
    else:
        seqs_all = [r["seq"] for r in race]
    i0 = next(i for i, s in enumerate(seqs_all) if s >= start)
    win = [seqs_all[i0]]
    for s in seqs_all[i0 + 1:]:
        if s - win[-1] > 4 or len(win) >= n:
            break
        win.append(s)
    tss = np.array([by_seq[s]["ts"] for s in win])
    grays = [np.array(Image.open(DEMOS / sess_name / "frames" / by_seq[s]["file"]).convert("RGB")).mean(2)
             for s in win]
    print(f"== 路线③ 剖面法（{sess_name} seq {win[0]}~{win[-1]}，y_h={y_h:.1f}，帧 {len(win)}）==")

    # 全候选线 → 门 → 读数
    readings, kept = {}, {}
    n_pass = 0
    for a in np.arange(A_MIN, A_MAX + 1e-9, A_STEP):
        pr = profiles_for_line(grays, y_h, vpx, float(a))
        if pr is None:
            continue
        n_pass += 1
        v_rows, Hp = pr
        rd = []
        for lo, hi in BANDS:
            sel = (v_rows - y_h >= lo) & (v_rows - y_h <= hi)
            if sel.sum() < 40:
                continue
            dm = float(np.mean(v_rows[sel]) - y_h)
            for t in range(len(Hp) - 1):
                dt = tss[t + 1] - tss[t]
                if dt <= 0:
                    continue
                r = band_shift(Hp[t], Hp[t + 1], sel, dt)
                if r:
                    rd.append((dm, r[0], r[1], float(a), t))
        if rd:
            readings[float(a)] = rd
    print(f"过对比度门的线 {n_pass}/{len(np.arange(A_MIN, A_MAX + 1e-9, A_STEP))}；"
          f"有读数 {len(readings)} 条，读数总数 {sum(len(v) for v in readings.values())}")

    # 选线与判决链（维护者裁定 2026-09-20 二次）：**「用运动读数选线」已删除**——
    # line_slope 与读数共用同一赢家诅咒，在噪声主导读数上选极值必选出最噪声的线。
    # 新顺序：①门标定(gatest) → ②HP_WIN 随带自适应 → ③SEARCH 随带自适应(<局部周期/2)
    # → ④静态选线（线存在性+Hp 自身周期调制强度，不碰跨帧量）→ ⑤阳性+阴性对照。
    # 本 run() 现仅保留读数普查（供 ②③ 标定用），判决链在自适应落地后重建。
    print("判决链：未运行（等门标定+双自适应；旧版运动选线已删）")


def period_est(grays, y_h, vpx, a, tss):
    """步骤 0（维护者裁定 2026-09-20）：在已知虚线上测局部周期 period(d)——
    每带取 Hp 逐帧自相关（正滞后区首个显著主峰位置），零跨帧位移量。
    输出供 ②HP_WIN∝period、③SEARCH<period/2 导出，不拍参数。"""
    pr = profiles_for_line(grays, y_h, vpx, a)
    if pr is None:
        return None
    v_rows, Hp = pr
    out = []
    for lo, hi in BANDS:
        sel = (v_rows - y_h >= lo) & (v_rows - y_h <= hi)
        if sel.sum() < 50:
            continue
        peaks = []
        for row in Hp[:, sel]:
            x = row - row.mean()
            e0 = float((x * x).sum())
            if e0 <= 0:
                continue
            ac = np.correlate(x, x, "full")[len(x) - 1:]
            # 峰选取（维护者裁定 2026-09-20）：首个显著峰（>10% 能量）+ 谐波判别
            # （半周期处同样显著 → 当前峰是谐波，基频取半）
            for lag in range(4, min(len(ac) - 1, 120)):
                if ac[lag] > 0.1 * ac[0] and ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]:
                    p = lag
                    half = p // 2
                    if half >= 4 and ac[half] >= 0.8 * ac[p]:
                        p = half
                    peaks.append(p)
                    break
        if len(peaks) >= 8:
            p = np.array(peaks, float)
            out.append((0.5 * (lo + hi), float(np.median(p)),
                        float(np.percentile(p, 25)), float(np.percentile(p, 75)), len(p)))
    return out


def periodmode(sess_name, start, n=54, a_line=0.64):
    """步骤 0 入口：已知虚线（自车右虚线 a≈0.64，1268 窗身份实测）上跑 period(d)。"""
    race, by_seq = ps.race_frames(sess_name)
    cal = get_cal(sess_name, race)
    y_h, vpx = cal["y_h"], cal["vpx"]
    seqs_all = [r["seq"] for r in race]
    i0 = next(i for i, s in enumerate(seqs_all) if s >= start)
    win = [seqs_all[i0]]
    for s in seqs_all[i0 + 1:]:
        if s - win[-1] > 4 or len(win) >= n:
            break
        win.append(s)
    grays = [np.array(Image.open(DEMOS / sess_name / "frames" / by_seq[s]["file"]).convert("RGB")).mean(2)
             for s in win]
    tss = np.array([by_seq[s]["ts"] for s in win])
    res = period_est(grays, y_h, vpx, a_line, tss)
    print(f"== 步骤0 period(d)（{sess_name} seq {win[0]}~{win[-1]}，虚线 a={a_line}）==")
    if not res:
        print("无有效带"); return
    print("  d中位   period中位  p25-p75   n帧  带长  可测(带长≥3×period)")
    blen = {0.5 * (lo + hi): hi - lo for lo, hi in BANDS}
    for dm, pm, p25, p75, nf in res:
        L = blen.get(dm, 0)
        print(f"  {dm:5.0f}   {pm:8.1f}   {p25:4.1f}-{p75:4.1f}  {nf:4d}  {L:4d}  "
              f"{'✓' if L >= 3 * pm else '✗ 超带长不可测'}")
    d = np.array([r[0] for r in res]); p = np.array([r[1] for r in res])
    if len(d) >= 3:
        sl = np.polyfit(np.log10(d), np.log10(p), 1)[0]
        print(f"  log-log 斜率 = {sl:.2f}（地面固定周期预言 2；虚线对象身份+②③输入同时成立）")


def static_modulation(Hp):
    """④ 静态选线的构件：剖面自身的周期调制强度（零跨帧量）。
    对每帧 Hp 求自相关，取正滞后区主峰面积占比；实线≈0，虚线>0。"""
    out = []
    for row in Hp:
        x = row - row.mean()
        ac = np.correlate(x, x, "full")[len(x) - 1:]
        e0 = ac[0] if ac[0] > 0 else 1e-9
        pos = ac[4:60]
        out.append(float(np.sum(np.clip(pos, 0, None)) / e0 / len(pos)))
    return np.array(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    sess = args[0]
    if len(args) >= 4 and args[1] == "run":
        run(sess, int(args[2]), int(args[3]), "all" in args[4:])
    elif len(args) >= 4 and args[1] == "gatest":
        gatest(sess, int(args[2]), int(args[3]))
    elif len(args) >= 4 and args[1] == "period":
        periodmode(sess, int(args[2]), int(args[3]))
    else:
        sys.exit("用法: <session> {run|gatest|period} <seq_start> <n> [all]")
