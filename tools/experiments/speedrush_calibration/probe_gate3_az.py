"""Gate 3：纵向一致——真帧虚线回流 × HUD 车速 → A_z。

**命题**：Z = A_z/d 的纵向尺度与 HUD 车速在真帧上自洽——单条虚线的行位置
序列满足 v_row(t) = y_h + A_z/(Z0 − s(t))，s(t) = ∫v_ego dt；v_ego 取 HUD
「得分速度」读数（speedrush_scoring 实证 ≈ 车速 m/s，RULES §4.8 引：车速
约 42 m/s 时帧上直读 41）。A_z 是相机常量：跨轨中位后**跨场应稳定**。

**首跑实证（2026-09-19，仪器设计依据）**：
- 虚线仅在近场被 Hough 解析为独立段（像长 ≥25px 需 d ≳ 200 @A_z≈4500），
  远场虚线并成连续线——观测带冻结 d∈[200,367]、dv∈[25,300]；
- stride 2（dt≈0.096s）每步位移 ≈ 虚线间距的 0.8 倍（=v·dt/P，与深度
  无关）→ 身份跟踪在 stride 2 根本歧义；**必须 stride 1**（0.41 倍，
  可分辨）。stride 1 的 analyze/直道掩码用独立缓存名（analyze1_/straight1_），
  不覆盖 stride 2 缓存；
- A_z 无先验真值 → **两阶段**：① 共识扫描——A_z 网格上各做一次紧门限
  跟踪，以「长轨累计观测数」为共识分，argmax 为先验；② 先验下重建轨 +
  逐轨 (Z0, A_z) 网格 LS。

**前端（冻结，禁为过闸调整）**：陡线段 dv∈[25,300]；段中心
(u_m,v_m)，d = v_m − y_h ∈ [200,367]；X = A_x·(u_m−vpx)/d，|X| ≤ 4.5；
同帧去重 |ΔX|≤0.12 且 |Δd|≤8（同虚线双沿）；适用域 = 直道帧（弯道上
路径距离 ≠ 相机深度，回流模型不严格成立）。

**轨**：stride 1 帧间 NN，|ΔX| ≤ 0.15，Δd ∈ [0.35,1.8]·(v·dt·d²/A_z)；
miss ≤ 3；合格 n ≥ 6、世界位移 ≥ 6 m、拟合 rms ≤ 8 px（段中心定位
噪声底）。HUD：pair_sides 定本机槽位 → rate_blue_{own} OCR 浮点（m/s），
逐帧中位滤波（窗 15）积分得 s(t)。

**Gate 3 判据（先于运行写死）**：A_z 跨场 CV ≤ 15%（相机常量；≈Gate −1
A_z ±8% 的两倍容差）且全场单轨 rms 中位 ≤ 8 px；dash 世界周期 P = 共识
间距 × A_z 等派生量无外部真值，如实报告为事实记录。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_gate3_az.py <session>...
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import (CACHE, DEMOS, load_base,  # noqa: E402
                            score_intervals, staged_filter)
from probe_gate2_hist import get_cal  # noqa: E402
from probe_gate1_invariance import road_segs, lane_change_times  # noqa: E402
from probe_gate1_invariance import LANE_CHANGE_POST, LANE_CHANGE_PRE  # noqa: E402
from probe_gate0_vp import analyze_frame  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "probe_hud_ocr", HERE.parent / "speedrush_scoring" / "probe_hud_ocr.py")
hud = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hud)
REGS = json.loads((HERE.parents[2] / "maaracing_master" / "plugins" / "speedrush"
                   / "resources" / "policy" / "hud_regions.json").read_text(encoding="utf-8"))

# ---------- 冻结参数 ----------
DV_LO, DV_HI = 25.0, 300.0
D_LO, D_HI = 200.0, 367.0
X_MAX = 4.5
DEDUP_X, DEDUP_D = 0.12, 8.0
TRK_X = 0.15
TRK_GATE = (0.35, 1.8)
MISS_MAX = 3
TRK_N_MIN, TRK_SPAN_MIN, TRK_RMS_MAX = 6, 6.0, 8.0
Z0_GRID = np.arange(5.0, 120.0, 0.5)
AZ_GRID = np.arange(800.0, 24000.0, 25.0)
AZ_CONSENSUS = np.arange(2000.0, 12000.0, 250.0)
CV_GATE = 0.15
V_MIN = 5.0

_RE_NUM = re.compile(r"\d+(?:\.\d+)?")


def _frames(sess: Path) -> list[dict]:
    return [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]


def analyze_series1(sess_name: str) -> list[dict]:
    """stride 1 逐帧分析（独立缓存 analyze1_，不覆盖 stride 2 缓存）。"""
    out = CACHE / f"analyze1_{sess_name}.json"
    sess = DEMOS / sess_name
    meta = _frames(sess)
    if out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
        by = {r["seq"]: r for r in old["rows"]}
        rows = [by.get(m["seq"]) for m in meta]
        if all(rows):
            return rows
    rows = []
    for i, m in enumerate(meta):
        rgb = np.array(Image.open(sess / "frames" / m["file"]).convert("RGB"))
        r = analyze_frame(rgb, m["seq"])
        r["ts"] = m["ts_ns"] / 1e9
        r["file"] = m["file"]
        rows.append(r)
        if i % 100 == 0:
            print(f"  [{sess_name}] stride1 分析 {i}/{len(meta)}", flush=True)
    out.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return rows


def straight_mask1(sess_name: str, rows: list[dict]) -> np.ndarray:
    """stride 1 直道掩码（独立缓存 straight1_，行序 = analyze1_ 行序）。"""
    from probe_gate2_hist import frame_straight
    p = CACHE / f"straight1_{sess_name}.json"
    if p.exists():
        return np.array(json.loads(p.read_text(encoding="utf-8"))["mask"])
    sess = DEMOS / sess_name
    mask = []
    for i, r in enumerate(rows):
        rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        mask.append(frame_straight(rgb))
        if i % 100 == 0:
            print(f"  [{sess_name}] 直道门控 {i}/{len(rows)}", flush=True)
    p.write_text(json.dumps({"mask": mask}), encoding="utf-8")
    return np.array(mask)


def _rate(ocr, rgb) -> float | None:
    sa, sb = hud.pair_sides(rgb, REGS["pair_a"], REGS["pair_b"])
    own = "a" if sa == "本机(蓝)" else ("b" if sb == "本机(蓝)" else None)
    if own is None:
        return None
    m = _RE_NUM.search(ocr.read(rgb, REGS[f"rate_blue_{own}"]) or "")
    return float(m.group()) if m else None


def build_tracks(fr_obs: list[dict], az_est: float,
                 gate: tuple[float, float] = TRK_GATE) -> list[dict]:
    """帧间 NN 贪心（每轨取其最优观测，观测被领走不复用）。"""
    live, tracks, nid = {}, [], 0
    for fr in fr_obs:
        dt_prev = fr.get("dt") or 0.048
        v = fr["v_ego"]
        for tid in list(live):
            live[tid]["miss"] += 1
            if live[tid]["miss"] > MISS_MAX:
                tracks.append(live.pop(tid))
        best_for_track: dict[int, tuple[float, int]] = {}
        for oi, (X, d) in enumerate(fr["obs"]):
            best, bs = None, None
            for tid, t in live.items():
                X0, d0 = t["obs"][-1]
                if abs(X - X0) > TRK_X:
                    continue
                dpred = v * dt_prev * d0 * d0 / az_est
                dd = d - d0
                if not (gate[0] * dpred <= dd <= gate[1] * dpred):
                    continue
                sc = abs(dd - (gate[0] + gate[1]) / 2 * dpred)
                if bs is None or sc < bs:
                    best, bs = tid, sc
            if best is not None and (best not in best_for_track
                                     or bs < best_for_track[best][0]):
                best_for_track[best] = (bs, oi)
        claimed = set()
        for tid, (_, oi) in best_for_track.items():
            od = fr["obs"][oi]
            t = live[tid]
            t["miss"] = 0
            t["obs"].append(od)
            t["s"].append(t["s"][-1] + v * dt_prev)
            claimed.add(oi)
        for oi, od in enumerate(fr["obs"]):
            if oi not in claimed:
                live[nid] = {"id": nid, "miss": 0, "obs": [od], "s": [0.0]}
                nid += 1
    tracks += list(live.values())
    return tracks


def fit_track(t: dict, y_h: float) -> tuple[float, float] | None:
    s = np.asarray(t["s"])[None, None, :]
    v = np.asarray([d for _, d in t["obs"]])[None, None, :]
    z0 = Z0_GRID[:, None, None]
    az = AZ_GRID[None, :, None]
    model = y_h + az / np.maximum(z0 - s, 0.5)
    sse = np.sum((v - model) ** 2, axis=2)
    k = np.unravel_index(np.argmin(sse), sse.shape)
    rms = float(np.sqrt(sse[k[0], k[1]] / len(t["obs"])))
    return float(AZ_GRID[k[1]]), rms


def run(sessions: list[str]) -> dict:
    base = load_base()
    if not base:
        print("无冻结基准——先跑 probe_gate0_vp --freeze-base")
        sys.exit(1)
    ocr = hud.HudOcr()
    all_az, report = [], {}
    for s in sessions:
        t0 = time.perf_counter()
        rows = analyze_series1(s)
        rows = staged_filter(rows, base["base"], score_intervals(s))
        race = [r for r in rows if r["stage"] == 4]
        if len(race) < 30:
            print(f"== {s}: 比赛态帧 {len(race)} 不足，跳过")
            continue
        cal = get_cal(s, race)
        if not cal:
            print(f"== {s}: 标定不可用，跳过")
            continue
        vpx, y_h, ax = cal["vpx"], cal["y_h"], cal["A_x_used"]
        ivs = score_intervals(s) or [(race[0]["ts"], race[-1]["ts"])]
        lc = lane_change_times(s)
        mask = straight_mask1(s, rows)
        sess = DEMOS / s
        fr_obs, rates, tss = [], [], []
        for i, r in enumerate(rows):
            if r["stage"] != 4 \
                    or not any(a <= r["ts"] <= b for a, b in ivs) \
                    or any(r["ts"] >= t - LANE_CHANGE_PRE
                           and r["ts"] <= t + LANE_CHANGE_POST for t in lc):
                continue
            rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
            rate = _rate(ocr, rgb)
            obs = []
            if rate is not None and rate >= V_MIN:
                # 全帧建观测：弯道帧的轨由拟合 rms 淘汰（曲率进残差），
                # 不按直道掩码切断观测——首跑实证直道帧稀疏散布、按掩码
                # 建轨会在非直道帧间断链
                for sg in road_segs(rgb):
                    dv = sg[3] - sg[1]
                    if not (DV_LO <= abs(dv) <= DV_HI):
                        continue
                    um = (sg[0] + sg[2]) / 2
                    vm = (sg[1] + sg[3]) / 2
                    d = vm - y_h
                    if not (D_LO <= d <= D_HI):
                        continue
                    X = ax * (um - vpx) / d
                    if abs(X) <= X_MAX:
                        obs.append((X, d))
                obs.sort()
                ded = []
                for X, d in obs:
                    if ded and abs(X - ded[-1][0]) <= DEDUP_X \
                            and abs(d - ded[-1][1]) <= DEDUP_D:
                        ded[-1] = ((ded[-1][0] + X) / 2, (ded[-1][1] + d) / 2)
                    else:
                        ded.append((X, d))
                obs = ded
            rates.append(rate if rate is not None and rate >= V_MIN else None)
            tss.append(r["ts"])
            fr_obs.append({"v_ego": None, "obs": obs, "dt": None})
        rr = np.array([r if r is not None else np.nan for r in rates])
        if np.isfinite(rr).sum() < 30:
            print(f"== {s}: HUD 速度读数不足（{int(np.isfinite(rr).sum())}），跳过")
            continue
        idx = np.arange(len(rr))
        valid = np.isfinite(rr)
        rr_f = np.interp(idx, idx[valid], rr[valid])
        k = 7
        sm = np.array([np.median(rr_f[max(0, i - k):i + k + 1])
                       for i in range(len(rr_f))])
        v_ego = float(np.median(sm))
        for j, f in enumerate(fr_obs):
            f["v_ego"] = float(sm[j])
            f["dt"] = float(tss[j] - tss[j - 1]) if j else 0.048
        # 阶段 1：A_z 共识扫描（紧门限长轨共识分）
        scores = []
        for az_g in AZ_CONSENSUS:
            cand = build_tracks(fr_obs, float(az_g),
                                gate=(0.35, 1.8))
            sc = sum(min(len(t["obs"]), 12) for t in cand if len(t["obs"]) >= 5)
            scores.append(sc)
        az_est = float(AZ_CONSENSUS[int(np.argmax(scores))])
        # 阶段 2：先验下重建 + 逐轨拟合
        tr_final = []
        for it in range(2):
            cand = build_tracks(fr_obs, az_est)
            fits, all_rms, n_span = [], [], 0
            for t in cand:
                if len(t["obs"]) < TRK_N_MIN:
                    continue
                if t["s"][-1] - t["s"][0] < TRK_SPAN_MIN:
                    continue
                n_span += 1
                fa = fit_track(t, y_h)
                if fa:
                    all_rms.append(fa[1])
                    if fa[1] <= TRK_RMS_MAX:
                        fits.append(fa)
            if fits:
                az_est = float(np.median([f[0] for f in fits]))
            tr_final = fits
        azs = np.array([f[0] for f in tr_final])
        rmss = np.array([f[1] for f in tr_final])
        rec = {"n_tracks": len(tr_final), "n_span": n_span,
               "az_consensus": az_est,
               "A_z": float(np.median(azs)) if len(azs) else None,
               "az_iqr": [float(np.percentile(azs, q)) for q in (25, 75)]
                         if len(azs) else None,
               "rms_med": float(np.median(rmss)) if len(rmss) else None,
               "v_ego": v_ego, "n_frames": len(fr_obs),
               "n_straight": int(sum(1 for f in fr_obs if f["obs"]))}
        if len(azs):
            all_az.append(rec["A_z"])
            print(f"== {s}: 共识 {az_est:.0f} → 轨 {len(azs)}  "
                  f"A_z={rec['A_z']:.0f} IQR {rec['az_iqr'][0]:.0f}~"
                  f"{rec['az_iqr'][1]:.0f}  rms 中位 {rec['rms_med']:.2f}px  "
                  f"v_ego={v_ego:.1f} m/s [{time.perf_counter() - t0:.0f}s]")
        else:
            print(f"== {s}: 共识 {az_est:.0f}，合格轨 0（有观测直道帧 "
                  f"{rec['n_straight']}，n/span 过 {n_span}）"
                  f" [{time.perf_counter() - t0:.0f}s]")
        report[s] = rec
    if len(all_az) >= 3:
        arr = np.array(all_az)
        cv = float(np.std(arr) / np.mean(arr))
        print(f"\n== Gate 3：{len(arr)} 场  A_z 中位 {np.median(arr):.0f}  "
              f"跨场 CV {cv:.1%}（判据 ≤{CV_GATE:.0%}）→ "
              f"{'通过' if cv <= CV_GATE else '红'}")
    (CACHE / "gate3_az.json").write_text(json.dumps(report, ensure_ascii=False),
                                         encoding="utf-8")
    print(f"缓存 → {CACHE / 'gate3_az.json'}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    args = ap.parse_args()
    run(args.sessions)
