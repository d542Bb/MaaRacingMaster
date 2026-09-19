"""Gate 0：真帧逐帧 VP 提取 + 分级筛帧 + 留出验证 + 叠图（README 实施顺序第 2 步）。

**要回答什么**（README 闸门 0）：标定器在真实游戏帧上的 VP 是否
「VPx 稳定、y_h 带内且帧间稳」——线段族是否纯净到能撑起标定。

**筛帧 v2（2026-09-19，回应维护者循环筛选质疑）**：v1 的 race_frames 用
y_h 众数当入场券（用结果定义有效帧），被要求补证。v2 每级判据只用该帧
画面内容的可观察量，y_h 一致性降级为最后检查：
    S1 证据充分性：候选纵向线 ≥10；长线（|Δv|≥150）≥3；
       近端截距（外推 v=650，用线段自身斜率，不碰 VP）聚类 ≥4 簇；
    S2 共点性：强内点（σ_d≤12px）≥4 且其中位 |d|/σ_d ≤2.5
       （「线族确实共点」的几何前提，与共点在哪儿无关）；
    S3 比赛态（辅助）：score OCR 覆盖处要求有分值；覆盖不全时如实降级跳过；
    S4 一致性检查（最后，只做检查）：y_h 距众数 ≤25、滑窗 ±15 帧
       中位残差 |dx|≤12、|dy|≤8。被此级淘汰的帧单独计数。
**留出验证**：跨场留一（B 场用 A 场定的 y_h 基准跑全管线，不重新找众数）
+ 场内时间留出（前 70% 定基准、后 30% 直接套用）。

**素材**：`%APPDATA%/MaaRacingMaster/data/speedrush/demos/<session>/`
（与 speedrush_vision/probe_old_model.py 的 DEMOS 同源；本探针不碰 ONNX）。
缓存 `%TEMP%/sr_calib/`：analyze_<session>.json（逐帧统计）、
gate0_overlay/<session>/sheet.png（比赛态帧）、rejected_<session>.png（随机
被拒帧 + 淘汰原因标注，供核对淘汰依据是否独立于结果）。

用法（仓库根）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_calibration\\probe_gate0_vp.py <session>...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_ground_calib import (  # noqa: E402
    H, W, _line_coef, _seg_sigma_d, _steep, estimate_vp,
)

DEMOS = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data"
         / "speedrush" / "demos")
TRICK_CACHE = Path(os.environ.get("TEMP", "/tmp")) / "sr_trick"
CACHE = Path(os.environ.get("TEMP", "/tmp")) / "sr_calib"
Y_TOP = 170          # 路面带裁剪线：HUD 全部在顶带，路面带实测起于 y≈180
V_REF = 650.0        # 近端截距参考行
CANNY = (50, 150)
HOUGH = dict(threshold=40, minLineLength=40, maxLineGap=6)
ITERS = 1200
SHEET_N = 12

# ---- 筛选阈值（先验固定；S1/S2 定位是「挡垃圾」不选精英，正确性由 S4 裁决） ----
S1_MIN_CAND = 8      # 候选纵向线下限：合成/真帧正常画面 ≥15，垃圾画面个位数
S1_MIN_LONG = 2      # 长线（|Δv|≥150，跨 ~27% 路面带）：真车道线族必有，纯树/建筑线族难凑
S1_LONG_DV = 150.0
S1_MIN_CLUSTERS = 3  # 近端截距簇：3 条分隔线可见即可判（虚线有空档，单行截距会缺位，
                     # v1 取 4 曾把虚线空档帧整批误杀）
S1_CLUSTER_GAP = 60.0
S2_MIN_STRONG = 3    # 强内点（σ_d≤12px）：过 VP 的长证据至少 3 条
S2_STRONG_SIG = 12.0
S2_MAX_ZMED = 4.0    # 强内点中位标准化残差：真帧 Canny/Hough 噪声下 2.5 过紧
                     # （v1 实证把正常直道帧整批拒掉），4 仍要求「强线确实过点」
S4_YH_TOL = 25.0
S4_WIN = 15
S4_DX, S4_DY = 12.0, 8.0
DIAG_Y0, DIAG_Y1 = 150.0, 420.0
QUALITY_SIG = 5.0
QUALITY_INL = 12


def _frames(sess: Path):
    return [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]


def analyze_frame(rgb: np.ndarray, seq: int) -> dict:
    """单帧全管线：线段 → S1 证据统计 → VP（含 S2 共点统计）。不依赖任何其他帧。"""
    gray = cv2.cvtColor(rgb[Y_TOP:H], cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, *CANNY)
    raw = cv2.HoughLinesP(edges, 1, np.pi / 180, **HOUGH)
    flat = raw.reshape(-1, 4) if raw is not None else []
    segs = [(float(a), float(b) + Y_TOP, float(c), float(d) + Y_TOP)
            for a, b, c, d in flat]
    cand = [s for s in segs if _steep(s)]
    out = {"seq": seq, "n_cand": len(cand), "n_segs": len(segs)}
    # S1a 长线：|Δv|≥S1_LONG_DV 的候选数
    longs = [s for s in cand if abs(s[3] - s[1]) >= S1_LONG_DV]
    out["n_long"] = len(longs)
    # S1b 近端截距簇：|Δv|≥100 的线外推到 V_REF（线段自身斜率，不碰 VP）
    cuts = []
    for s in cand:
        dv = s[3] - s[1]
        if abs(dv) < 100:
            continue
        cuts.append(s[0] + (s[2] - s[0]) * (V_REF - s[1]) / dv)
    n_clu = 0
    if cuts:
        cuts = sorted(cuts)
        clu = [cuts[0]]
        for c in cuts[1:]:
            if c - clu[-1] < S1_CLUSTER_GAP:
                clu.append(c)
            else:
                n_clu += 1
                clu = [c]
        n_clu += 1
    out["n_cut_clusters"] = n_clu
    vp = estimate_vp(cand, rng_seed=seq, iters=ITERS, y_band=(DIAG_Y0, DIAG_Y1)) \
        if len(cand) >= 2 else None
    out["vpx"] = vp["vpx"] if vp else None
    out["y_h"] = vp["y_h"] if vp else None
    out["sig_vpx"] = vp["sig_vpx"] if vp else None
    out["sig_yh"] = vp["sig_yh"] if vp else None
    out["n_inl"] = len(vp["inliers"]) if vp else 0
    # S2 共点性：强内点（σ_d≤S2_STRONG_SIG）数 + 其中位标准化残差
    n_strong, zmed = 0, None
    if vp:
        coefs = np.stack([_line_coef(s) for s in cand])
        lens = np.array([np.hypot(s[2] - s[0], s[3] - s[1]) for s in cand])
        mids = np.array([[(s[0] + s[2]) / 2, (s[1] + s[3]) / 2] for s in cand])
        sig = _seg_sigma_d(coefs, lens, mids, (vp["vpx"], vp["y_h"]))
        d = np.abs(coefs[:, :2] @ np.array([vp["vpx"], vp["y_h"]]) + coefs[:, 2])
        strong = (d < sig * 3 + 3) & (sig <= S2_STRONG_SIG)
        n_strong = int(strong.sum())
        if n_strong:
            zmed = float(np.median(d[strong] / sig[strong]))
    out["n_strong"] = n_strong
    out["zmed"] = zmed
    return out


def analyze_series(sess_name: str, stride: int) -> tuple[list[dict], list[dict]]:
    """逐帧分析（缓存）。返回 (rows, frames_meta)；meta 供叠图取文件。"""
    out = CACHE / f"analyze_{sess_name}.json"
    sess = DEMOS / sess_name
    meta = _frames(sess)
    if out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
        if old.get("stride") == stride:
            by = {r["seq"]: r for r in old["rows"]}
            rows = [by.get(m["seq"]) for m in meta[::stride]]
            if all(rows):
                return rows, meta
    rows, t0 = [], time.perf_counter()
    for i, m in enumerate(meta[::stride]):
        rgb = np.array(Image.open(sess / "frames" / m["file"]).convert("RGB"))
        r = analyze_frame(rgb, m["seq"])
        r["ts"] = m["ts_ns"] / 1e9
        r["file"] = m["file"]
        rows.append(r)
        if i % 50 == 0:
            dt = time.perf_counter() - t0
            print(f"  [{sess_name}] {i}/{len(meta[::stride])} 帧，"
                  f"均 {dt / (i + 1):.2f}s（逐帧 Canny+Hough+RANSAC）", flush=True)
    out.write_text(json.dumps({"stride": stride, "rows": rows}), encoding="utf-8")
    return rows, meta


def score_intervals(sess_name: str) -> list[tuple[float, float]]:
    """比赛态信号（S3，辅助）：trick 缓存的 score 序列覆盖时段。

    score 是独立 OCR 信号（与 VP 无关），但覆盖不全——只在使用处生效，
    覆盖不到的时段如实跳过该级，不作为淘汰依据。
    """
    p = TRICK_CACHE / f"score_{sess_name}.json"
    if not p.exists():
        return []
    sc = json.loads(p.read_text(encoding="utf-8"))
    if len(sc) < 10:
        return []
    ts = [r["ts"] for r in sc]
    gaps = np.diff(ts)
    ivs, a = [], ts[0]
    for g, t in zip(gaps, ts[1:]):
        if g > 2.0:                      # OCR 断档 >2s 视为不连续
            ivs.append((a, t - g))
            a = t
    ivs.append((a, ts[-1]))
    return ivs


def staged_filter(rows: list[dict], yh_base: float | None,
                  ivs: list[tuple[float, float]]) -> list[dict]:
    """分级筛帧。yh_base=None 时由本组数据自定（A 段）；给定则直接套用（B 段）。

    每帧带 stage（通过的最深级）与 reason（首个未过级的原因+数值）。
    S4 前的各级都是画面内容前提；S4 是结果一致性检查（单独归类）。
    """
    q = [r for r in rows if r["vpx"] is not None and r["sig_vpx"] is not None
         and np.isfinite(r["sig_vpx"]) and r["sig_vpx"] <= QUALITY_SIG
         and r["n_inl"] >= QUALITY_INL and DIAG_Y0 <= r["y_h"] <= DIAG_Y1]
    for r in rows:
        r["stage"], r["reason"] = 0, "质量口径（σ/内点/带）"
    if yh_base is None:
        yh = np.array([r["y_h"] for r in q])
        if len(yh):
            hist, edges = np.histogram(yh, bins=np.arange(DIAG_Y0, DIAG_Y1 + 1, 10))
            yh_base = float(edges[np.argmax(hist)] + 5)
    # S1/S2 在质量口径内逐帧判
    s12 = []
    for r in q:
        if r["n_cand"] < S1_MIN_CAND:
            r["reason"] = f"S1 候选线少({r['n_cand']}<{S1_MIN_CAND})"
        elif r["n_long"] < S1_MIN_LONG:
            r["reason"] = f"S1 长线不足({r['n_long']}<{S1_MIN_LONG})"
        elif r["n_cut_clusters"] < S1_MIN_CLUSTERS:
            r["reason"] = f"S1 截距簇不足({r['n_cut_clusters']}<{S1_MIN_CLUSTERS})"
        elif r["n_strong"] < S2_MIN_STRONG:
            r["reason"] = f"S2 强内点不足({r['n_strong']}<{S2_MIN_STRONG})"
        elif r["zmed"] is None or r["zmed"] > S2_MAX_ZMED:
            r["reason"] = f"S2 强内点不共点(zmed={r['zmed']})"
        else:
            r["stage"] = 2
            s12.append(r)
    # S3 比赛态（辅助，覆盖处生效）
    def in_race(r):
        return any(a - 1.0 <= r["ts"] <= b + 1.0 for a, b in ivs)
    s3 = []
    for r in s12:
        if ivs and not in_race(r):
            r["reason"] = "S3 非比赛态(score 缺失)"
        else:
            r["stage"] = 3
            s3.append(r)
    # S4 一致性检查（最后；淘汰单独归类）
    ok = [r for r in s3 if r["y_h"] is not None]
    arr = {r["seq"]: r for r in ok}
    seqs = [r["seq"] for r in ok]
    for i, s_ in enumerate(seqs):
        r = arr[s_]
        win = seqs[max(0, i - S4_WIN):i + S4_WIN + 1]
        dx = r["vpx"] - float(np.median([arr[w]["vpx"] for w in win]))
        dy = r["y_h"] - float(np.median([arr[w]["y_h"] for w in win]))
        r["dx"], r["dy"] = dx, dy
        if abs(r["y_h"] - yh_base) > S4_YH_TOL:
            r["reason"] = f"S4 y_h 离基准({r['y_h']:.0f} vs {yh_base:.0f})"
        elif abs(dx) > S4_DX or abs(dy) > S4_DY:
            r["reason"] = f"S4 滑窗离群(dx={dx:+.0f},dy={dy:+.0f})"
        else:
            r["stage"] = 4
    return rows


def holdout_report(all_rows: dict[str, list[dict]], ivs_map: dict) -> dict:
    """留出验证：跨场留一（B 场用 A 场基准）+ 场内时间留出（前 70% 定基准）。

    返回各设置下比赛态（stage=4）帧的 VPx/y_h 统计。A 场学习量只有
    y_h 众数一个标量；阈值全部先验固定，不存在按结果调参。
    """
    res = {}
    names = list(all_rows)
    for b in names:                              # 跨场留一
        a_names = [n for n in names if n != b]
        a_yh = np.concatenate([[r["y_h"] for r in all_rows[n]
                                if r["vpx"] is not None] for n in a_names])
        hist, edges = np.histogram(a_yh, bins=np.arange(DIAG_Y0, DIAG_Y1 + 1, 10))
        base = float(edges[np.argmax(hist)] + 5)
        rows = staged_filter(all_rows[b], base, ivs_map[b])
        race = [r for r in rows if r["stage"] == 4]
        res[f"留出B={b[-9:]}(基准{base:.0f})"] = _stat(race)
    for n in names:                              # 场内时间留出
        rows = all_rows[n]
        mid = rows[len(rows) * 7 // 10]
        a, b = [r for r in rows if r["ts"] <= mid["ts"]], \
               [r for r in rows if r["ts"] > mid["ts"]]
        yh = np.array([r["y_h"] for r in a if r["vpx"] is not None])
        hist, edges = np.histogram(yh, bins=np.arange(DIAG_Y0, DIAG_Y1 + 1, 10))
        base = float(edges[np.argmax(hist)] + 5)
        race = [r for r in staged_filter(b, base, ivs_map[n]) if r["stage"] == 4]
        res[f"时间留出{n[-9:]}后30%"] = _stat(race)
    return res


def _stat(race: list[dict]) -> dict:
    if not race:
        return {"n": 0}
    vx = np.array([r["vpx"] for r in race])
    yh = np.array([r["y_h"] for r in race])
    return {"n": len(race), "vpx": [round(float(np.median(vx)), 1),
                                    round(float(np.std(vx)), 1)],
            "y_h": [round(float(np.median(yh)), 1), round(float(np.std(yh)), 1)]}


def _draw_tile(sess: Path, r: dict, tag: str, color) -> np.ndarray:
    rgb = np.array(Image.open(sess / "frames" / r["file"]).convert("RGB"))
    vp, segs = extract_vp_light(rgb, r["seq"])
    if vp:
        inl = set(map(tuple, vp["inliers"]))
        for s_ in segs:
            c = (0, 220, 0) if tuple(s_) in inl else (150, 150, 150)
            cv2.line(rgb, (int(s_[0]), int(s_[1])), (int(s_[2]), int(s_[3])), c, 1)
        x, y = int(round(vp["vpx"])), int(round(vp["y_h"]))
        cv2.line(rgb, (x - 18, y), (x + 18, y), (255, 60, 60), 2)
        cv2.line(rgb, (x, y - 18), (x, y + 18), (255, 60, 60), 2)
        cv2.line(rgb, (x, 0), (x, H), (255, 60, 60), 1)
        cv2.line(rgb, (0, y), (W, y), (60, 120, 255), 1)
    # 标签垫黑底，保证被拒原因可读
    cv2.rectangle(rgb, (6, 6), (6 + 9 * len(tag) + 8, 40), (0, 0, 0), -1)
    cv2.putText(rgb, tag, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.75, color,
                2, cv2.LINE_AA)
    return rgb


def extract_vp_light(rgb: np.ndarray, seq: int):
    """叠图用：与 analyze_frame 同前端，返回 (vp, segs)。"""
    gray = cv2.cvtColor(rgb[Y_TOP:H], cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, *CANNY)
    raw = cv2.HoughLinesP(edges, 1, np.pi / 180, **HOUGH)
    flat = raw.reshape(-1, 4) if raw is not None else []
    segs = [(float(a), float(b) + Y_TOP, float(c), float(d) + Y_TOP)
            for a, b, c, d in flat]
    cand = [s for s in segs if _steep(s)]
    vp = estimate_vp(cand, rng_seed=seq, iters=ITERS, y_band=(DIAG_Y0, DIAG_Y1)) \
        if len(cand) >= 2 else None
    return vp, segs


def sheet(tiles: list[np.ndarray], path: Path):
    cols, rows_n = 3, (len(tiles) + 2) // 3
    tw, th = W // 2, H // 2
    img = np.full((rows_n * th, cols * tw, 3), 20, np.uint8)
    for i, t in enumerate(tiles):
        t = cv2.resize(t, (tw, th))
        img[i // cols * th:(i // cols + 1) * th,
            i % cols * tw:(i % cols + 1) * tw] = t
    cv2.imwrite(str(path), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))


def report(sessions, stride, rng_seed: int = 7):
    all_rows, ivs_map, metas = {}, {}, {}
    for s in sessions:
        rows, meta = analyze_series(s, stride)
        all_rows[s], ivs_map[s], metas[s] = rows, score_intervals(s), meta
        ivs = ivs_map[s]
        rows = staged_filter(rows, None, ivs)
        by_stage = {k: sum(1 for r in rows if r["stage"] == k) for k in range(5)}
        rej = [r for r in rows if r["stage"] < 4 and r["vpx"] is not None]
        print(f"\n== {s}：{len(rows)} 帧（stride {stride}）  S3 覆盖 "
              f"{sum(b - a for a, b in ivs):.0f}s / 场长 "
              f"{rows[-1]['ts'] - rows[0]['ts']:.0f}s")
        print(f"   通过：质量口径 {by_stage[0] + sum(by_stage.values()) - by_stage[0]}"
              f" → S1/S2 共点 {by_stage[2] + by_stage[3] + by_stage[4]}"
              f" → S3 比赛态 {by_stage[3] + by_stage[4]}"
              f" → S4 一致性 {by_stage[4]}")
        race = [r for r in rows if r["stage"] == 4]
        st = _stat(race)
        if st["n"]:
            print(f"   比赛态 VPx {st['vpx'][0]}±{st['vpx'][1]}  "
                  f"y_h {st['y_h'][0]}±{st['y_h'][1]} (n={st['n']})")
        # 被拒帧随机抽样（固定种子）——淘汰原因标注，供核对是否独立于结果
        sess = DEMOS / s
        picks = [rej[i] for i in np.random.default_rng(rng_seed).permutation(
            len(rej))[:SHEET_N]] if rej else []
        tiles = [_draw_tile(sess, r, r["reason"][:38], (0, 200, 255)) for r in picks]
        if tiles:
            sheet(tiles, CACHE / f"rejected_{s}.png")
            print(f"   被拒帧随机抽检 {len(tiles)} 张 → {CACHE / f'rejected_{s}.png'}")
        # 比赛态叠图
        od = CACHE / "gate0_overlay" / s
        od.mkdir(parents=True, exist_ok=True)
        step = max(1, len(race) // SHEET_N)
        tiles = [_draw_tile(sess, r, f"#{r['seq']} inl {r['n_inl']}/{r['n_cand']}",
                            (0, 255, 255)) for r in race[::step][:SHEET_N]]
        if tiles:
            sheet(tiles, od / "sheet.png")
            print(f"   比赛态叠图 → {od / 'sheet.png'}")
    print("\n== 留出验证（B 段不参与基准/阈值，阈值先验固定）==")
    for k, v in holdout_report(all_rows, ivs_map).items():
        print(f"   {k}: {v}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--stride", type=int, default=2)
    args = ap.parse_args()
    report(args.sessions, args.stride)
