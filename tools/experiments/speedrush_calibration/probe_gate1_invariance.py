"""Gate 1：不变道不变性——标定常数接受世界模型检验（README 闸门 1）。

**要回答什么**：场级冻结常数（比赛态帧 VP 中位 + A_x 刻度）下，不变道目标的
车道坐标不随距离/时间漂移。判据（先验固定）：**漂移中位 < 0.25 车道宽**；
红时归因 = y_h 错 / pitch 漂 / 接地点错（README 闸门表）。两问分开答：

**相对几何仪器 = 框内夹车虚线间距 s(d)**。漆面线天然不变道；陡线段取两端点
（同一地面 X 的独立观测），按 d 三分位箱独立聚类配「夹 0 两侧」簇对：夹车
两虚线间距恒等于 1 车道，y_h 错 ε 使 s(d)=s_true·d/(d−ε) 随 d 变化。
**口径限制（维护者裁定 2026-09-19）：整体横向平移在间距中完全相消——
s(d) 平坦只证相对车道几何/尺度一致，不证绝对横向零点。**

**绝对横向仪器 = 车辆轨迹真值分析**。AI 目标车不变道、不转向（维护者确认），
横向真值恒定 ⇒ X_t 的一切系统性漂移都是标定信号，不得解释为目标行为：
① 自车换道暂态（banner 窗内帧剔除，独立 OCR 信号——窗外的自车横移泄漏
   会伪装成目标斜坡，正是首跑所见 1~1.7 车道斜坡的来源之一）；
② 标定误差按逐轨拟合 X_obs = a + b/d + c·d 归因（见 lane_truth_analysis）：
   b 的跨轨常数分量 → VPx/零点；b 的 ∝a 分量 → y_h 误差 ε；
   c = −a·Δz/A_z（接地点=后轴）；a 对整数车道回归 → 尺度 α 与残余零点
   （绝对 lane bias）。样本选择只用 n/时长/距离等画面几何量，禁止用 X_t。

**素材**：Gate 0 analyze 缓存 + 分级筛帧（场级 VP/A_x 由 stage4 帧定，score
覆盖段为时窗）；副仪器用 `%TEMP%/sr_trick/dets_<session>.json`（旧模型街车框
[class, conf, u_cx, v_bottom, h]，probe_trick_trigger.py 同格式）。
输出 `%TEMP%/sr_calib/gate1.json`、`gate1_dash_<session>.png`（X–d 散点）、
`gate1_xseries.png`、`gate1_overlay_<session>.png`。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_gate1_invariance.py <session>...
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_ground_calib import estimate_ax, estimate_vp, _steep  # noqa: E402
from probe_gate0_vp import (CACHE, CANNY, DEMOS, DIAG_Y0, DIAG_Y1, HOUGH, ITERS,  # noqa: E402
                            TRICK_CACHE, V_REF, W, Y_TOP, analyze_series,
                            load_base, score_intervals, staged_filter)

# ---------- 先验固定的判据与门槛 ----------
DRIFT_GATE = 0.25      # 闸门判据：合格轨迹 X_t 漂移中位 < 0.25 车道宽
D_MIN = 10.0           # 地面距离下限（px）：d→0 处 X 发散，贴地平线观测无意义
V_BOTTOM_MAX = 690.0   # 框底贴近画面下缘 = 接地点被裁掉，不可信
# 有效轨迹门槛（2026-09-19 首次全量跑后按物理修正：高速跟驰车 |dv|≈1~3px/帧、
# 连续可见窗仅 1.2~1.7 s，原 30 帧门槛每场只活 1 条轨，中位数无统计意义。
# 判据仍是「在接近的足时长轨」，帧数下限由 20fps×1.2 s 导出，非按结果调）：
TRACK_MIN_OBS = 24
TRACK_MIN_SPAN = 1.2
TRACK_D_RATIO = 1.3
LANE_CHANGE_PRE, LANE_CHANGE_POST = 1.0, 2.0   # 自车变道 banner 剔除窗
DT_PRED = 0.05         # dets 中位帧间隔（实测 20fps）
MATCH_TOL = 40.0       # 轨迹关联预测容差（px）
GAP_MAX = 3            # 允许遮挡丢帧数
EGO_V_MIN, EGO_H_MIN = 520.0, 100.0   # 自车框启发（trick analyze.py 同口径）


def road_segs(rgb: np.ndarray) -> list[tuple[float, float, float, float]]:
    """前端与 Gate 0 同源（裁剪→Canny→Hough→_steep），保留线段供 A_x 汇集。"""
    gray = cv2.cvtColor(rgb[Y_TOP:], cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, *CANNY)
    raw = cv2.HoughLinesP(edges, 1, np.pi / 180, **HOUGH)
    flat = raw.reshape(-1, 4) if raw is not None else []
    segs = [(float(a), float(b) + Y_TOP, float(c), float(d) + Y_TOP)
            for a, b, c, d in flat]
    return [s for s in segs if _steep(s)]


def session_calib(sess_name: str, race: list[dict]) -> dict | None:
    """比赛态帧 → 场级常数：VP 取中位；A_x 主口径 = 全部内点线段汇集后
    estimate_ax（Gate −1 同法），备口径 = 逐帧 A_x 中位（稳定性核对）。"""
    med_vpx = float(np.median([r["vpx"] for r in race]))
    med_yh = float(np.median([r["y_h"] for r in race]))
    sess = DEMOS / sess_name
    pooled, per_frame = [], []
    t0 = time.perf_counter()
    for i, r in enumerate(race):
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        cand = road_segs(rgb)
        if len(cand) < 2:
            continue
        vp = estimate_vp(cand, rng_seed=r["seq"], iters=ITERS,
                         y_band=(DIAG_Y0, DIAG_Y1))
        if not vp:
            continue
        pooled += vp["inliers"]
        ax = estimate_ax(vp)
        if ax:
            per_frame.append(ax["A_x"])
        if i % 20 == 0:
            print(f"  [{sess_name}] 标定 {i}/{len(race)} 帧"
                  f"（累计 {time.perf_counter() - t0:.1f}s）", flush=True)
    ax_pool = estimate_ax({"vpx": med_vpx, "y_h": med_yh, "inliers": pooled})
    if not ax_pool or not per_frame:
        return None
    return {"vpx": med_vpx, "y_h": med_yh, "A_x": ax_pool["A_x"],
            "n_lines": ax_pool["n_lines"], "n_inl": len(pooled),
            "ax_frame_med": float(np.median(per_frame)),
            "ax_frame_iqr": [float(np.percentile(per_frame, q))
                             for q in (25, 75)], "n_ax_frames": len(per_frame)}


def lane_change_times(sess_name: str) -> list[float]:
    p = TRICK_CACHE / f"banner_{sess_name}.json"
    if not p.exists():
        return []
    return [b["ts"] for b in json.loads(p.read_text(encoding="utf-8"))
            if b["kind"] == "变"]


def ax_physical(sess_name: str, race: list[dict], vpx: float,
                y_h: float) -> dict | None:
    """车道宽度的物理基准：近端截距间距（不依赖晶格/锚推断）。

    比赛态帧的车道线外推到参考行 V_REF=650 截距、聚类后，取夹住 VPx 的
    左右最近簇 = 自车两侧虚线（RULES §4：夹自车两虚线恰距 1 车道）——
    间距即该行的车道像素宽，A_x = (V_REF − y_h)/lane_px。逐帧中位。
    用途：Gate 1 漂移的「车道单位」刻度。晶格拟合与它偏差 >25% 时，
    X 投影以本口径为准并如实记录（首跑实证：220750_p2 晶格给 1.325，
    物理 0.49，按物理刻度 Xmed=+2.50(拟合)→+0.92≈邻车道中央）。
    """
    sess = DEMOS / sess_name
    vals = []
    for r in race:
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        cuts = []
        for s in road_segs(rgb):
            dv = s[3] - s[1]
            if abs(dv) < 100:
                continue
            cuts.append(s[0] + (s[2] - s[0]) * (V_REF - s[1]) / dv)
        cuts = sorted(c for c in cuts if 0 < c < W)
        if len(cuts) < 2:
            continue
        clu, cur = [], [cuts[0]]
        for c in cuts[1:]:
            if c - cur[-1] < 40:
                cur.append(c)
            else:
                clu.append(float(np.median(cur)))
                cur = [c]
        clu.append(float(np.median(cur)))
        l = [c for c in clu if c < vpx]
        rr = [c for c in clu if c > vpx]
        if not l or not rr:
            continue
        lane = min(rr) - max(l)
        if 100 <= lane <= 900:
            vals.append((V_REF - y_h) / lane)
    if len(vals) < 8:
        return None
    return {"A_x_phys": float(np.median(vals)), "n": len(vals),
            "iqr": [float(np.percentile(vals, q)) for q in (25, 75)]}


def build_tracks(dets: list[dict], ivs: list[tuple[float, float]],
                 lc_ts: list[float], cal: dict) -> list[dict]:
    """比赛时窗内（剔除自车变道窗）关联目标框。
    接地点 = 框底中点 (u_cx, v_bottom)；X = A_x·(u−vpx)/(v−y_h)（车道单位）。"""
    vpx, yh = cal["vpx"], cal["y_h"]
    ax = cal.get("A_x_used") or cal["A_x"]   # get 的默认值先求值，不能用 cal["A_x"] 兜底
    tracks: list[dict] = []
    live: dict[int, dict] = {}
    next_id = 0

    for f in dets:
        ts, seq = f["ts"], f["seq"]
        in_race = any(a <= ts <= b for a, b in ivs)
        in_lc = any(ts >= t - LANE_CHANGE_PRE and ts <= t + LANE_CHANGE_POST
                    for t in lc_ts)
        for tid, t in list(live.items()):
            t["miss"] += 1
            if t["miss"] > GAP_MAX:
                tracks.append(live.pop(tid))
        if not in_race or in_lc:
            continue
        ego_c = [c for c in f["cars"] if c[0] == 1 and c[3] > EGO_V_MIN
                 and c[4] > EGO_H_MIN]
        ego = max(ego_c, key=lambda c: c[4]) if ego_c else None
        obs = []
        for c in f["cars"]:
            if c[0] != 1:
                continue
            u, v = float(c[2]), float(c[3])
            if ego is not None and abs(u - ego[2]) < 30 and v > ego[3] - 60:
                continue                    # 自车自身（及紧贴自车下缘的误框）
            d = v - yh
            if d < D_MIN or v > V_BOTTOM_MAX:
                continue
            obs.append({"u": u, "v": v, "d": d, "seq": seq, "ts": ts,
                        "X": float(ax * (u - vpx) / d)})
        used = set()
        for tid, t in list(live.items()):
            o = t["obs"][-1]
            if len(t["obs"]) >= 2:
                p = t["obs"][-2]
                kx = DT_PRED / max(o["ts"] - p["ts"], 1e-3)
                vel = ((o["u"] - p["u"]) * kx, (o["v"] - p["v"]) * kx)
            else:
                vel = (0.0, 0.0)
            pu, pv = o["u"] + vel[0], o["v"] + vel[1]
            best, bd = None, MATCH_TOL
            for j, ob in enumerate(obs):
                if j in used:
                    continue
                dd = np.hypot(ob["u"] - pu, ob["v"] - pv)
                if dd < bd:
                    best, bd = j, dd
            if best is not None:
                used.add(best)
                t["miss"] = 0
                t["obs"].append(obs[best])
        for j, ob in enumerate(obs):
            if j not in used:
                live[next_id] = {"id": next_id, "obs": [ob], "miss": 0}
                next_id += 1
    tracks += list(live.values())
    return [t for t in tracks if len(t["obs"]) >= TRACK_MIN_OBS]


def track_metrics(t: dict) -> dict:
    o = sorted(t["obs"], key=lambda x: x["ts"])
    ts = np.array([x["ts"] for x in o])
    X = np.array([x["X"] for x in o])
    d = np.array([x["d"] for x in o])
    dev = np.abs(X - np.median(X))
    k = max(1, len(o) // 3)
    d_f, d_l = float(np.median(d[:k])), float(np.median(d[-k:]))
    return {"id": t["id"], "session": None, "obs": o, "n": len(o),
            "span": float(ts[-1] - ts[0]),
            "drift": float(np.median(dev)), "p95_dev": float(np.percentile(dev, 95)),
            "X_med": float(np.median(X)), "d_first": d_f, "d_last": d_l,
            "d_ratio": d_l / max(d_f, 1.0),
            "elig": (len(o) >= TRACK_MIN_OBS and ts[-1] - ts[0] >= TRACK_MIN_SPAN
                     and d_l >= TRACK_D_RATIO * max(d_f, 1.0))}


# ---------- 主仪器：框内夹车虚线间距不变性 ----------
DASH_MIN_DV = 60.0     # 线段纵向跨度下限：中点接地点误差 ∝ 厚度/跨度，短线不取
DASH_D_MIN = 12.0      # 中点地面距离下限
DASH_GAP = 0.35        # X 聚类断簇间隙（车道间距 1.0，护栏簇独立）
DASH_BIN_MIN = 4       # 每三分位箱每侧最少端点数
DASH_SP_LO, DASH_SP_HI = 0.6, 1.4   # 夹车两虚线间距合法带（车道单位）


def dash_invariance(sess_name: str, rows: list[dict], ivs: list[tuple[float, float]],
                    cal: dict, lc_ts: list[float]) -> tuple[list[dict], list]:
    """漆面线 = 天然未变道目标（画在路面上）。只答相对几何一问——
    整体横向平移在间距中相消，绝对零点由车辆轨真值分析回答。

    每条 ≥DASH_MIN_DV 的陡线段取**两端点**：线段两端同在一条过 VP 的像直线上
    ⇒ 各自都是同一地面 X 的独立观测（中点口径密度减半，固定分箱下簇心噪声底
    ~0.2 车道压在判据上）。

    判据在**框内**做：夹车两虚线间距恒等于 1 车道，与自车横向位置、VPx 无关
    （共模相消），只剩标定误差可测——y_h 错 ε 使观测间距 s(d) = s_true·d/(d−ε)
    随 d 变化，正确标定下 s(d) 平坦。**按 d 三分位箱独立聚类配夹 0 对**（远箱
    护栏不污染近箱——整帧配对的版本被 #647 式误配证伪），drift = 各箱 s 的
    max−min；跨帧中位 <0.25 车道为绿。输入 = 时窗内全部帧（帧内差分不依赖
    帧间一致性，stage4 稀疏集只用于定常数；池化跨帧检验被自车横向漂移与密集
    结构帧污染，首跑实证）。"""
    sess = DEMOS / sess_name
    frames, pts_all = [], []
    for r in rows:
        if not any(a <= r["ts"] <= b for a, b in ivs):
            continue
        if any(r["ts"] >= t - LANE_CHANGE_PRE and r["ts"] <= t + LANE_CHANGE_POST
               for t in lc_ts):
            continue                    # 自车换道暂态：相机平移/偏摆，剔除
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        pts = []
        for s in road_segs(rgb):
            if abs(s[3] - s[1]) < DASH_MIN_DV:
                continue
            # 端点采样：线段两端同在一条过 VP 的像直线上 ⇒ 各自都是该地面
            # X 的独立观测（中点口径证据密度只有一半，固定分箱下噪声底 ~0.2
            # 车道压在判据上——改端点+三分位箱提密度，判据不变）。
            for u_e, v_e in ((s[0], s[1]), (s[2], s[3])):
                d = v_e - cal["y_h"]
                if d < DASH_D_MIN:
                    continue
                X = cal["A_x_used"] * (u_e - cal["vpx"]) / d
                pts.append((X, d))
                pts_all.append((X, d))
        if len(pts) < 6:
            continue
        ds = np.sort([p[1] for p in pts])
        q1, q2 = ds[len(ds) // 3], ds[2 * len(ds) // 3]
        sbins = []
        for lo, hi in ((DASH_D_MIN, q1), (q1, q2), (q2, 1e9)):
            bp = np.array([p for p in pts if lo <= p[1] < hi])
            if len(bp) < 2 * DASH_BIN_MIN:
                continue
            bp = bp[np.argsort(bp[:, 0])]
            clu, cur = [], [bp[0]]
            for p in bp[1:]:
                if p[0] - cur[-1][0] > DASH_GAP:
                    clu.append(np.array(cur))
                    cur = [p]
                else:
                    cur.append(p)
            clu.append(np.array(cur))
            pairs = [(l, rr) for l in clu for rr in clu
                     if l[:, 0].mean() < 0 < rr[:, 0].mean()
                     and len(l) >= DASH_BIN_MIN and len(rr) >= DASH_BIN_MIN
                     and DASH_SP_LO <= rr[:, 0].mean() - l[:, 0].mean() <= DASH_SP_HI]
            if not pairs:
                continue
            l, rr = min(pairs, key=lambda p: abs(
                p[1][:, 0].mean() - p[0][:, 0].mean() - 1.0))
            sbins.append((float(np.median(bp[:, 1])),
                          float(rr[:, 0].mean() - l[:, 0].mean()),
                          len(l), len(rr)))
        if len(sbins) < 2:
            continue
        sv = [b[1] for b in sbins]
        frames.append({"seq": r["seq"], "ts": r["ts"],
                       "drift": float(max(sv) - min(sv)),
                       "s_med": float(np.median(sv)),
                       "sbins": [[round(a, 1), round(b, 3), c, k]
                                 for a, b, c, k in sbins]})
    return frames, pts_all




def plot_dash(sess_name: str, cal: dict, pts: list[tuple[float, float]],
              out: Path) -> None:
    """X–d 散点（对数 d 轴 + 整格参考线）：目视「同一簇是否随 d 平移」。"""
    W_, Hh = 900, 500
    img = np.full((Hh, W_, 3), 25, np.uint8)
    a = np.array(pts)
    lo, hi = min(a[:, 0].min(), -3.0), max(a[:, 0].max(), 3.0)
    for lane in np.arange(-3, 3.1, 0.5):
        x = int(50 + (lane - lo) / (hi - lo) * (W_ - 70))
        col = (70, 70, 70) if lane % 1 else (100, 100, 100)
        cv2.line(img, (x, 20), (x, Hh - 30), col, 1)
        if lane % 1 == 0.5 or lane % 1 == -0.5:
            cv2.putText(img, f"{lane:+.1f}", (x - 12, Hh - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
    for X, d in pts:
        x = int(50 + (X - lo) / (hi - lo) * (W_ - 70))
        y = int(Hh - 30 - (np.log10(max(d, 1)) - 1) /
                (np.log10(450) - 1) * (Hh - 50))
        cv2.circle(img, (x, y), 2, (0, 255, 255), -1)
    cv2.putText(img, f"{sess_name}  dash X vs d (log)  A_x={cal['A_x_used']:.3f}",
                (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.imwrite(str(out), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))


def _xclusters(pts: list[tuple[float, float]]) -> list[np.ndarray]:
    """按 X 排序、gap>DASH_GAP 断簇。"""
    if not pts:
        return []
    arr = np.array(sorted(pts))
    clu, cur = [], [arr[0]]
    for p in arr[1:]:
        if p[0] - cur[-1][0] > DASH_GAP:
            clu.append(np.array(cur))
            cur = [p]
        else:
            cur.append(p)
    clu.append(np.array(cur))
    return clu


def flanking_centers(sess_name: str, rows: list[dict],
                     ivs: list[tuple[float, float]], cal: dict,
                     lc_ts: list[float]) -> tuple[np.ndarray, np.ndarray]:
    """逐帧（stride 网格）夹车虚线中心 c_f = (X_l + X_r)/2。

    漆面线与自车共相机位姿：c_f 直接测自车相对车道中心的横向偏移（道单位），
    是自车横移/VPx 漂移的**共模信号**——不经过任何估计器（逐帧 VP 首跑实证
    被估计噪声主导：用它重算 X 漂移反升至 0.35~0.56 道，不可作位姿真值）。
    目标 X 减去 (c_f − median c_f) 即消去自车平移分量，剩下的漂移只可能是
    乘性标定误差（y_h、接地点 Δz）或噪声。"""
    sess = DEMOS / sess_name
    ts_out, c_out = [], []
    for r in rows:
        if not any(a <= r["ts"] <= b for a, b in ivs):
            continue
        if any(r["ts"] >= t - LANE_CHANGE_PRE and r["ts"] <= t + LANE_CHANGE_POST
               for t in lc_ts):
            continue
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        pts = []
        for s in road_segs(rgb):
            if abs(s[3] - s[1]) < DASH_MIN_DV:
                continue
            for u_e, v_e in ((s[0], s[1]), (s[2], s[3])):
                d = v_e - cal["y_h"]
                if d >= DASH_D_MIN:
                    pts.append((cal["A_x_used"] * (u_e - cal["vpx"]) / d, d))
        if len(pts) < 6:
            continue
        clu = _xclusters(pts)
        pairs = [(l, rr) for l in clu for rr in clu
                 if l[:, 0].mean() < 0 < rr[:, 0].mean()
                 and len(l) >= DASH_BIN_MIN and len(rr) >= DASH_BIN_MIN
                 and DASH_SP_LO <= rr[:, 0].mean() - l[:, 0].mean() <= DASH_SP_HI]
        if not pairs:
            continue
        l, rr = min(pairs, key=lambda p: abs(
            p[1][:, 0].mean() - p[0][:, 0].mean() - 1.0))
        ts_out.append(r["ts"])
        c_out.append(0.5 * float(l[:, 0].mean() + rr[:, 0].mean()))
    return np.array(ts_out), np.array(c_out)


def lane_truth_analysis(ms: list[dict], cal: dict,
                        c_ts: np.ndarray, c_val: np.ndarray) -> dict:
    """固定车道 AI 车 = 横向真值恒定（维护者确认 2026-09-19）下的绝对横向检验。
    样本选择只用 n/时长/距离（画面几何量），禁止用 X_t 本身筛选。

    逐轨最小二乘拟合 X_obs = a + b/d + c·d（d = v_bottom − y_h）：
      a  = 该轨的横向真值估计（含零点与尺度误差）
      b  = A_x·δ_vpx + X_true·ε_yh 的一阶合成——跨轨对 a 回归：
           常数项 → VPx/绝对零点偏移；斜率 → y_h 误差 ε（px）
      c  = −X_true·Δz/A_z（接地点=后轴，落后车中心 Δz）——跨轨对 a 回归
           斜率 → Δz/A_z（与尺度 α 无关；A_z 定标归 Gate 3）
    绝对 lane bias：真值应落整数车道（自车在道中央），a 对 round(a) 回归：
    斜率 = 尺度误差 α，截距 = 残余零点。轨迹内波动 = 拟合残差 rms。

    自车平移共模先消（flanking_centers）：X_cm = X_obs − (c_f(t) − med c_f)，
    加法斜坡（banner 未覆盖的自车滑动/换道泄漏）由此消除；消共模前后各报
    drift（drift_raw / drift_cm），归因拟合在 X_cm 上做。"""
    per = []
    for t in ms:
        o = sorted(t["obs"], key=lambda x: x["ts"])
        if len(o) < 8:
            continue
        d = np.array([x["d"] for x in o])
        X = np.array([x["X"] for x in o])
        if len(c_ts) >= 3:
            Xc = X - np.interp(np.array([x["ts"] for x in o]),
                               c_ts, c_val - np.median(c_val))
        else:
            Xc = X
        A = np.stack([np.ones_like(d), 1.0 / d, d], 1)
        (a, b, c), *_ = np.linalg.lstsq(A, Xc, rcond=None)
        r = Xc - A @ np.array([a, b, c])
        per.append({"id": t["id"], "n": len(o), "a": float(a), "b": float(b),
                    "c": float(c), "rms": float(np.sqrt(np.mean(r ** 2))),
                    "drift": float(np.median(np.abs(Xc - np.median(Xc)))),
                    "drift_raw": float(np.median(np.abs(X - np.median(X)))),
                    "d_min": float(d.min()), "d_max": float(d.max())})
    out = {"per": per,
           "drift_med": float(np.median([p["drift"] for p in per])) if per else None,
           "drift_raw_med": float(np.median([p["drift_raw"] for p in per])) if per else None,
           "rms_med": float(np.median([p["rms"] for p in per])) if per else None}
    out.update(attribution(per))
    return out


def attribution(per: list[dict]) -> dict:
    """跨轨归因：b = p0 + q·a → q = ε_yh（px），p0 = α·A_x·δ_vpx（零点）；
    c = s·a → s = −Δz/A_z；a 对 round(a) 回归 → 尺度 α 与残余零点。"""
    out: dict = {}
    if len(per) < 3:
        return out
    a_ = np.array([p["a"] for p in per])
    b_ = np.array([p["b"] for p in per])
    c_ = np.array([p.get("c", 0.0) for p in per])
    M = np.stack([np.ones_like(a_), a_], 1)
    (p0, q), *_ = np.linalg.lstsq(M, b_, rcond=None)
    res = b_ - M @ np.array([p0, q])
    out["b_split"] = {"eps_yh": float(q), "vpx_const": float(p0),
                      "fit_rms": float(np.sqrt(np.mean(res ** 2)))}
    s = float(np.sum(a_ * c_) / np.sum(a_ * a_)) if np.abs(a_).max() > 1e-6 else None
    out["dz_over_az"] = s
    lane = np.round(a_)
    keep = np.abs(a_ - lane) < 0.5          # round 未跨半格者才可信
    if keep.sum() >= 3:
        M2 = np.stack([np.ones(keep.sum()), lane[keep]], 1)
        (z0, alpha), *_ = np.linalg.lstsq(M2, a_[keep], rcond=None)
        out["lane_reg"] = {"zero_bias": float(z0), "scale_alpha": float(alpha),
                           "max_abs_bias": float(np.abs(a_[keep] - lane[keep]).max()),
                           "n": int(keep.sum())}
    return out


def sheet_tracks(sess: str, picks: list[dict], cal: dict, out: Path) -> None:
    """每条轨迹远/中/近 3 帧叠图（框 + X 值），末帧画 VP 十字与 y_h 线。"""
    meta = [json.loads(x) for x in
            (DEMOS / sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]
    by_seq = {m["seq"]: m for m in meta}
    tiles = []
    for t in picks:
        o = t["obs"]
        for i in (0, len(o) // 2, len(o) - 1):
            ob = o[i]
            m = by_seq.get(ob["seq"])
            if not m:
                continue
            rgb = np.array(Image.open(DEMOS / sess / "frames" / m["file"])
                           .convert("RGB"))
            u, v = int(ob["u"]), int(ob["v"])
            cv2.rectangle(rgb, (u - 30, max(v - 60, 0)), (u + 30, v), (0, 255, 255), 2)
            cv2.putText(rgb, f"#{t['id']} X={ob['X']:.2f} d={ob['d']:.0f}",
                        (max(u - 60, 5), max(v - 70, 30)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            if i == len(o) - 1:
                cv2.drawMarker(rgb, (int(cal["vpx"]), int(cal["y_h"])), (255, 0, 0),
                               cv2.MARKER_CROSS, 24, 2)
                cv2.line(rgb, (0, int(cal["y_h"])), (1279, int(cal["y_h"])), (255, 0, 0), 1)
            tiles.append(rgb)
    if not tiles:
        return
    cols, th, tw = 3, 180, 320
    rows = (len(tiles) + cols - 1) // cols
    img = np.zeros((rows * (th + 22), cols * tw, 3), np.uint8)
    for i, t in enumerate(tiles):
        r, c = i // cols, i % cols
        img[r * (th + 22) + 22:(r + 1) * (th + 22), c * tw:(c + 1) * tw] = \
            cv2.resize(t, (tw, th))
        cv2.putText(img, f"tile {i}", (c * tw + 4, r * (th + 22) + 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.imwrite(str(out), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))


def plot_x_series(tracks: list[dict], out: Path) -> None:
    """逐轨迹 X_t 曲线板（横轴相对秒，纵轴车道 + 整格参考线）：判台阶 vs 爬行。"""
    cols, W_, Hh = 4, 460, 300
    rows = (len(tracks) + cols - 1) // cols
    img = np.full((rows * Hh, cols * W_, 3), 25, np.uint8)
    for i, t in enumerate(tracks):
        o = t["obs"]
        ts = np.array([x["ts"] for x in o]) - o[0]["ts"]
        X = np.array([x["X"] for x in o])
        x0, y0 = (i % cols) * W_, (i // cols) * Hh
        lo, hi = min(X.min(), -2.2), max(X.max(), 2.2)
        px = x0 + 40 + ts / max(ts[-1], 1e-3) * (W_ - 60)
        py = y0 + Hh - 30 - (X - lo) / (hi - lo) * (Hh - 60)
        for lane in np.arange(-2.0, 2.1, 1.0):
            yy = int(y0 + Hh - 30 - (lane - lo) / (hi - lo) * (Hh - 60))
            cv2.line(img, (x0 + 40, yy), (x0 + W_ - 20, yy), (70, 70, 70), 1)
            cv2.putText(img, f"{lane:+.0f}", (x0 + 6, yy + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (120, 120, 120), 1)
        cv2.polylines(img, [np.stack([px, py], 1).astype(np.int32)], False,
                      (0, 255, 255), 2)
        cv2.putText(img,
                    f"#{t['id']} {t['session'][-6:]} drift={t['drift']:.2f} "
                    f"Xmed={t['X_med']:.2f} d {t['d_first']:.0f}->{t['d_last']:.0f}",
                    (x0 + 8, y0 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1)
    cv2.imwrite(str(out), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))


def run(sessions: list[str], stride: int = 2) -> dict:
    base = load_base()
    if not base:
        print("无冻结基准 yh_base.json——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    all_tracks, session_rep = [], {}
    all_dash: list[dict] = []
    all_lt: list[dict] = []
    per_sess_tracks: dict[str, list[dict]] = {}
    for s in sessions:
        rows, _ = analyze_series(s, stride)
        rows = staged_filter(rows, base["base"], score_intervals(s))
        race = [r for r in rows if r["stage"] == 4]
        if len(race) < 15:
            print(f"== {s}: 比赛态帧 {len(race)} 不足，跳过")
            continue
        cal = session_calib(s, race)
        if not cal:
            print(f"== {s}: A_x 拟合失败（内点/晶格不足），跳过")
            continue
        print(f"== {s}: 比赛态 {len(race)} 帧  VP=({cal['vpx']:.1f},{cal['y_h']:.1f})"
              f"  A_x={cal['A_x']:.3f}（格线 {cal['n_lines']} 条，逐帧中位 "
              f"{cal['ax_frame_med']:.3f} IQR {cal['ax_frame_iqr'][0]:.3f}~"
              f"{cal['ax_frame_iqr'][1]:.3f}，n={cal['n_ax_frames']}）")
        phys = ax_physical(s, race, cal["vpx"], cal["y_h"])
        if phys:
            rel = abs(cal["A_x"] - phys["A_x_phys"]) / phys["A_x_phys"]
            cal["A_x_phys"], cal["ax_rel_dev"] = phys, rel
            cal["A_x_used"] = phys["A_x_phys"] if rel > 0.25 else cal["A_x"]
            src = "物理" if rel > 0.25 else "晶格"
            print(f"   A_x 物理口径（近端截距间距）{phys['A_x_phys']:.3f} "
                  f"IQR {phys['iqr'][0]:.3f}~{phys['iqr'][1]:.3f}（n={phys['n']}）"
                  f"，与晶格偏差 {rel:.0%} → 投影刻度取{src}")
        else:
            cal["A_x_used"] = cal["A_x"]
            print("   A_x 物理口径证据不足（可用帧 <8）→ 投影刻度取晶格")
        # 跟踪时窗用 score 覆盖段（S3 同源，覆盖整场）；stage4 帧是稀疏标定
        # 证据点，拿它们聚段会把比赛时窗打碎成 10 段、轨迹无法成轨（实证）。
        ivs = score_intervals(s)
        if not ivs:
            tss = sorted(r["ts"] for r in race)
            ivs, a = [], tss[0]
            for g, t in zip(np.diff(tss), tss[1:]):
                if g > 1.0:
                    ivs.append((a, t))
                    a = t
            ivs.append((a, tss[-1]))
        lc_ts = lane_change_times(s)
        # 主仪器：漆面线不变性
        dash, pts = dash_invariance(s, rows, ivs, cal, lc_ts)
        if dash:
            dr = np.array([c["drift"] for c in dash])
            sm = np.array([c["s_med"] for c in dash])
            print(f"   框内夹车虚线：有效帧 {len(dash)}（线采样 {len(pts)}），"
                  f"间距 s 中位 {np.median(sm):.3f} 车道（刻度核对≈1.0）、"
                  f"s(d) 漂移中位 {np.median(dr):.3f}  max {dr.max():.3f}"
                  f"（判据 <{DRIFT_GATE}）")
            for c in sorted(dash, key=lambda c: -c["drift"])[:4]:
                print(f"     #{c['seq']} s_med {c['s_med']:.2f} drift {c['drift']:.2f} "
                      f"sbins(d,s,nl,nr) {c['sbins']}")
        if pts:
            plot_dash(s, cal, pts, CACHE / f"gate1_dash_{s}.png")
        all_dash += dash
        # 绝对横向仪器：车辆轨迹（AI 车横向真值恒定，维护者确认 2026-09-19）
        dp = TRICK_CACHE / f"dets_{s}.json"
        if not dp.exists():
            print(f"   无 dets 缓存（{dp.name}）——车辆轨绝对检验跳过")
            session_rep[s] = {"cal": cal, "n_race": len(race), "dash": dash,
                              "n_track": 0, "n_elig": 0, "drift_med": None}
            continue
        dets = json.loads(dp.read_text(encoding="utf-8"))
        tracks = build_tracks(dets, ivs, lc_ts, cal)
        ms = []
        for t in tracks:
            m = track_metrics(t)
            m["session"] = s
            ms.append(m)
        per_sess_tracks[s] = ms
        elig = [m for m in ms if m["elig"]]
        print(f"   车辆轨：n≥{TRACK_MIN_OBS} 共 {len(ms)} 条（不按 X 筛），"
              f"其中足接近量（span≥{TRACK_MIN_SPAN}s、d 比≥{TRACK_D_RATIO}）"
              f"{len(elig)} 条")
        c_ts, c_val = flanking_centers(s, rows, ivs, cal, lc_ts)
        if len(c_val):
            print(f"   夹车虚线中心 c_f：{len(c_ts)} 帧（共模信号，中位 "
                  f"{np.median(c_val):+.2f} 道、IQR "
                  f"{np.percentile(c_val,25):+.2f}~{np.percentile(c_val,75):+.2f}）")
        lt = lane_truth_analysis(ms, cal, c_ts, c_val)
        if lt.get("per"):
            print(f"     漂移中位：消共模 {lt['drift_med']:.3f} 道（消前 "
                  f"{lt['drift_raw_med']:.3f}）  模型残差 rms 中位 "
                  f"{lt['rms_med']:.3f} 道（n={len(lt['per'])}）")
            print("     轨  n  d范围   X远端→X近端  漂移raw→cm  a(真值)  b(X~1/d) c(X~d)   rms")
            by_id = {m["id"]: m for m in ms}
            for p in sorted(lt["per"], key=lambda p: -p["drift_raw"]):
                o = sorted(by_id[p["id"]]["obs"], key=lambda x: x["d"])
                print(f"     #{p['id']:<3d}{p['n']:3d} {p['d_min']:3.0f}-{p['d_max']:3.0f} "
                      f"{o[0]['X']:+6.2f}→{o[-1]['X']:+6.2f}  "
                      f"{p['drift_raw']:.2f}→{p['drift']:.2f}  {p['a']:+.2f}   "
                      f"{p['b']:+6.1f}  {p['c']:+.4f}  {p['rms']:.2f}")
            for p in lt["per"]:
                p["session"] = s
            all_lt += lt["per"]
            if lt.get("b_split"):
                bs = lt["b_split"]
                print(f"     X~1/d 斜率分解：ε_yh = {bs['eps_yh']:+.1f}px  "
                      f"VPx/零点常数项 = {bs['vpx_const']:+.3f} 道·px"
                      f"（跨轨 fit rms {bs['fit_rms']:.3f}）")
            if lt.get("dz_over_az") is not None:
                print(f"     X~d 斜率 ∝ −Δz/A_z = {lt['dz_over_az']:+.4f}"
                      f"（接地点=后轴深度差，A_z 定标归 Gate 3）")
            if lt.get("lane_reg"):
                lr = lt["lane_reg"]
                print(f"     lane 回归（a~round(a)）：零点偏 {lr['zero_bias']:+.3f} 道  "
                      f"尺度 α = {lr['scale_alpha']:.3f}  max|bias| "
                      f"{lr['max_abs_bias']:.2f} 道（n={lr['n']}）")
        session_rep[s] = {"cal": cal, "n_race": len(race), "n_track": len(ms),
                          "n_elig": len(elig), "dash": dash, "lane_truth": lt,
                          "drift_med": float(np.median([m["drift"] for m in elig]))
                          if elig else None}
        all_tracks += ms
        (CACHE / f"gate1_tracks_{s}.json").write_text(
            json.dumps(ms, ensure_ascii=False), encoding="utf-8")
    elig = [m for m in all_tracks if m["elig"]]
    verdict_rel = verdict_abs = None
    out_pooled: dict = {}
    if all_dash:
        dr = np.array([c["drift"] for c in all_dash])
        verdict_rel = bool(np.median(dr) < DRIFT_GATE)
        print(f"\n== Gate 1 相对几何（框内夹车虚线间距 s(d)）：{len(all_dash)} 帧，"
              f"s(d) 漂移中位 {np.median(dr):.3f} 车道  p75 {np.percentile(dr, 75):.3f}"
              f"  max {dr.max():.3f}  → {'通过' if verdict_rel else '红'}"
              f"（判据 <{DRIFT_GATE}；注意：间距对整体横向平移不变，不证绝对零点）")
    if all_lt:
        vd = np.array([p["drift"] for p in all_lt])
        vr = np.array([p["drift_raw"] for p in all_lt])
        verdict_abs = bool(np.median(vd) < DRIFT_GATE)
        print(f"== Gate 1 绝对横向（车辆轨真值分析，AI 车横向真值恒定·维护者确认）："
              f"{len(all_lt)} 轨（不按 X 筛），消共模漂移中位 {np.median(vd):.3f} 车道"
              f"（消前 {np.median(vr):.3f}）  p75 {np.percentile(vd, 75):.3f}"
              f"  max {vd.max():.3f}  → {'通过' if verdict_abs else '红'}"
              f"（判据 <{DRIFT_GATE}）")
        pooled = attribution(all_lt)
        if pooled.get("b_split"):
            bs = pooled["b_split"]
            print(f"   跨场归因（{len(all_lt)} 轨）：ε_yh = {bs['eps_yh']:+.1f}px  "
                  f"零点常数项 = {bs['vpx_const']:+.3f} 道·px（b 拟合 rms "
                  f"{bs['fit_rms']:.3f}）")
        if pooled.get("dz_over_az") is not None:
            print(f"   Δz/A_z = {pooled['dz_over_az']:+.4f}（接地点=后轴深度差）")
        if pooled.get("lane_reg"):
            lr = pooled["lane_reg"]
            print(f"   lane 回归：零点偏 {lr['zero_bias']:+.3f} 道  尺度 α = "
                  f"{lr['scale_alpha']:.3f}  max|bias| {lr['max_abs_bias']:.2f} 道"
                  f"（n={lr['n']}）")
        out_pooled = pooled
    out = {"verdict_rel": verdict_rel, "verdict_abs": verdict_abs,
           "n_dash_frames": len(all_dash),
           "dash_drift_med": float(np.median([c["drift"] for c in all_dash]))
           if all_dash else None,
           "n_tracks": len(all_tracks), "n_elig": len(elig),
           "vehicle_drift_med": float(np.median([p["drift"] for p in all_lt])) if all_lt else None,
           "vehicle_drift_raw_med": float(np.median([p["drift_raw"] for p in all_lt])) if all_lt else None,
           "pooled_lane_truth": out_pooled,
           "gate": DRIFT_GATE, "sessions": session_rep}
    (CACHE / "gate1.json").write_text(json.dumps(out, ensure_ascii=False),
                                      encoding="utf-8")
    # 目视抽检：漂移最大 4 + 最小 2 条出 X_t 曲线板；各场高漂移前 2 出叠图
    order = sorted(all_tracks, key=lambda m: -m["drift"])
    plot_x_series(order[:4] + order[-2:], CACHE / "gate1_xseries.png")
    print(f"   X_t 曲线板 → {CACHE / 'gate1_xseries.png'}")
    for s, ms in per_sess_tracks.items():
        picks = sorted(ms, key=lambda m: -m["drift"])[:3]
        if picks:
            sheet_tracks(s, picks, session_rep[s]["cal"],
                         CACHE / f"gate1_overlay_{s}.png")
            print(f"   高漂移叠图 → {CACHE / f'gate1_overlay_{s}.png'}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--stride", type=int, default=2)
    args = ap.parse_args()
    run(args.sessions, args.stride)
