# -*- coding: utf-8 -*-
"""anchor-age 衰减实验：深度锚点随年龄退化多少，新帧 2D 重标能捞回多少。

**问题**（A/B 判决后的下一站，2026-10-07）：产线现状是拿 age≈250ms 的深度锚点
Z 直接当现在的距离用（hold）。问两个量：
1. hold 误差随锚点年龄怎么长（100/200/300ms 各剩多少准头）——直接量化时效墙；
2. 用**新帧**的 2D 读数给旧锚点重标距（Z ∝ 1/h 或 Z ∝ 1/(cy−y_h) 的比例式，
   只取比例不取绝对值——绝对标定仍是锚点的），比 hold 好多少。

**方法**：轨迹内枚举「锚点帧 i → 实帧 j」对，按实测 ts_ns 差分进年龄桶
（50~300ms，±25ms 容差），同对三算法误差（真值=当帧 MoGe Z）：
    hold:  Z_j − Z_i
    h:     Z_j − Z_i·(h_i/h_j)      （距离∝1/h，刚体车）
    cy:    Z_j − Z_i·((cy_i−y_h)/(cy_j−y_h))   （行距口径，域内才施）
**标尺独立**（四问合规）：误差单位同为米；锚点噪声 ε_i 对三算法同样在场
（重标只削接近项、不削锚点噪声）→ 静止窗（真值=零接近）给共同地板，
实战窗看谁随年龄长得慢。**真值质量门（对三算法盲，跑数前锁定）**：
entity_z 直方图主峰会在「车体↔框内背景」间翻跳（帧间数十米假跳，A/B 已
实证），逐帧真值不可直接采信——真值取**滚动中位平滑值**（窗 5，对跳变
稳健、对线性接近无偏），原始读数偏离平滑值 ±1.5m 外或出 2~90m 域的帧
无效；段级再加连贯门（过门帧的平滑序列要平或单调，簇来回翻整段作废）；
对有效 ⇔ 两端点帧都有效（锚点仍用原始读数，即生产实际持有的东西）。

**预注册判据（跑数前锁定）**：实战窗 age≈250ms 桶，h/cy 两法中 P50|err|
更低者若 < hold 同口径 P50|err|，且其静止窗地板 ≤ hold 静止地板 ×1.5
→ 「锚点+2D 重标」有效；否则挂账（物体层维持现状等深度专项）。
飞坡帧两端点剔除；币仅记录不进判决（h 语义不同）。

**自包含**：复用本目录 probe_approach_ab 的仪器（自建 YOLO/moge2_post/
关联轨迹），不 import `maaracing_master`。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_anchor_age.py --demo <dir> \
        [--start seq...] [--static-start seq] [--winlen 40]
产物：<demo>/../depth_review/jitter/anchor_age_<demo名>.{json,csv,jpg,_id.jpg}
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path

import cv2
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.experiments.speedrush_vision import probe_approach_ab as ab  # noqa: E402

AGES_MS = (50, 100, 150, 200, 250, 300)
AGE_TOL_MS = 25.0
MIN_TRACK = 12                            # 判决轨迹最少帧数（需覆盖 ≥250ms 桶）
FLOOR_INFLATE_MAX = 1.5                   # 静止窗地板膨胀上限（预注册判据）
Z_LO, Z_HI = 2.0, 90.0                    # 真值有效域（m）
Z_DD_LOCAL_MAX = 1.5                      # 真值局部二阶差门（m）：踢双峰跳变留平滑接近
RED, BLUE, GREEN, WHITE, GRAY = (60, 60, 230), (230, 130, 40), (80, 220, 80), (255, 255, 255), (160, 160, 160)
METHODS = ("hold", "h", "cy")


def seg_truth_coherent(med: list, ok: list[bool]) -> bool:
    """段级真值连贯门（对三算法盲，容忍单调接近）：过门帧的平滑序列要么平
    （|首尾差|<3m 时 P90|Δ|≤1m——簇内噪声主导），要么单调（符号一致率
    ≥0.8——真实接近/远离）。「车体↔背景」簇来回翻的段整段作废。"""
    idx = [k for k in range(len(med)) if ok[k] and med[k] is not None]
    if len(idx) < 10:
        return False
    ms = [med[k] for k in idx]
    total = ms[-1] - ms[0]
    if abs(total) < 3.0:
        dd = sorted(abs(b - a) for a, b in zip(ms, ms[1:]))
        return len(dd) >= 3 and dd[min(int(len(dd) * 0.9), len(dd) - 1)] <= 1.0
    sgn = 1.0 if total > 0 else -1.0
    cons = sum(1 for a, b in zip(ms, ms[1:]) if (b - a) * sgn >= -0.5) / (len(ms) - 1)
    return cons >= 0.8


def pair_errors(t: dict, ts_ns: dict[int, int], y_h: float, min_denom: float
                ) -> list[dict]:
    """轨迹 → 跨帧对误差行（连续段内、两端点均非飞坡帧）。"""
    obs = t["obs"]
    segs: list[list[dict]] = [[obs[0]]]
    for prev, cur in zip(obs, obs[1:]):
        if (cur["fid"] == prev["fid"] + 1 and not cur.get("jump")
                and not prev.get("jump")):
            segs[-1].append(cur)
        else:
            segs.append([cur])
    rows = []
    for seg in segs:
        med, ok = smooth_truth(seg)
        if not seg_truth_coherent(med, ok):
            continue
        for i in range(len(seg)):
            for j in range(i + 1, len(seg)):
                age = (ts_ns[seg[j]["fid"]] - ts_ns[seg[i]["fid"]]) / 1e6
                if age > AGES_MS[-1] + AGE_TOL_MS:
                    break
                bucket = min(AGES_MS, key=lambda a: abs(age - a))
                if abs(age - bucket) > AGE_TOL_MS:
                    continue
                z_i, z_j = seg[i].get("z"), med[j]
                if z_i is None or z_j is None:
                    continue
                if not (ok[i] and ok[j]):
                    continue
                bi, bj = seg[i]["box"], seg[j]["box"]
                h_i = max(1, bi[3] - bi[1]); h_j = max(1, bj[3] - bj[1])
                cy_i = (bi[1] + bi[3]) / 2.0; cy_j = (bj[1] + bj[3]) / 2.0
                rec = {"bucket": bucket, "age": round(age, 1),
                       "hold": z_j - z_i,
                       "h": z_j - z_i * h_i / h_j,
                       "cy": None}
                if (cy_i - y_h >= min_denom) and (cy_j - y_h >= min_denom):
                    rec["cy"] = z_j - z_i * (cy_i - y_h) / (cy_j - y_h)
                rows.append({"fid_i": seg[i]["fid"], "fid_j": seg[j]["fid"],
                             **rec})
    return rows


def smooth_truth(seg: list[dict], win: int = 5) -> tuple[list[float | None], list[bool]]:
    """滚动中位平滑真值（对「车体↔背景」主峰翻跳稳健、对线性接近无偏）。
    返回 (每帧平滑 Z, 每帧原始读数是否落主簇±门限内)。原始读数坏帧不牵连
    邻居——中位数吃掉孤跳；整段主峰五五开时窗内中位也会翻，此轨自然无对。"""
    zs = [o.get("z") for o in seg]
    n = len(zs)
    med: list[float | None] = [None] * n
    for k in range(n):
        w = [zs[t] for t in range(max(0, k - win // 2), min(n, k + win // 2 + 1))
             if zs[t] is not None]
        med[k] = statistics.median(w) if len(w) >= 3 else None
    ok = [med[k] is not None and zs[k] is not None
          and Z_LO <= zs[k] <= Z_HI and abs(zs[k] - med[k]) <= Z_DD_LOCAL_MAX
          for k in range(n)]
    return med, ok


def pooled_stats(recs: list[dict], method: str) -> dict | None:
    vals = [(r["age"], r[method]) for r in recs if r.get(method) is not None]
    if len(vals) < 5:
        return None
    errs = sorted(abs(v) for _, v in vals)
    signed = sorted(v for _, v in vals)
    p = lambda q: errs[min(int(len(errs) * q), len(errs) - 1)]
    return {"n": len(vals), "p50_abs": round(p(0.5), 3), "p90_abs": round(p(0.9), 3),
            "signed_med": round(statistics.median(signed), 3)}


def multi_chart(canvas, x0, y0, w, h, series: list[tuple], ages, title,
                hlines: list[tuple] | None = None):
    """P50|err| 对年龄的多曲线图；hlines=(值,颜色,文字) 画静止地板参考线。"""
    allv = [v for vals, _, _ in series for v in vals if v is not None]
    if hlines:
        allv += [v for v, _, _ in hlines]
    if not allv:
        return
    hi = max(allv) * 1.15
    cv2.putText(canvas, title, (x0, y0 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (30, 30, 30), 1)
    for k in range(5):
        gy = y0 + h - int(k / 4 * h)
        cv2.line(canvas, (x0, gy), (x0 + w, gy), (218, 218, 218), 1)
        cv2.putText(canvas, f"{hi * k / 4:.2f}", (x0 - 46, gy + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, GRAY, 1)
    for a_i, age in enumerate(ages):
        px = x0 + int(a_i / (len(ages) - 1) * w)
        cv2.putText(canvas, f"{age}", (px - 10, y0 + h + 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, GRAY, 1)
    for v, color, label in (hlines or []):
        gy = y0 + h - int(v / hi * h)
        cv2.line(canvas, (x0, gy), (x0 + w, gy), color, 1)
        cv2.putText(canvas, label, (x0 + w - 120, gy - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
    for vals, color, label in series:
        pts = [(x0 + int(i / (len(ages) - 1) * w),
                y0 + h - int(v / hi * h))
               for i, v in enumerate(vals) if v is not None and not math.isnan(v)]
        if len(pts) >= 2:
            cv2.polylines(canvas, [np.array(pts)], False, color, 2)
        for p in pts:
            cv2.circle(canvas, p, 4, color, -1)
    # 图例
    lx = x0 + 6
    for vals, color, label in series:
        cv2.putText(canvas, label, (lx, y0 + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)
        lx += 110


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True)
    ap.add_argument("--winlen", type=int, default=40)
    ap.add_argument("--start", type=int, nargs="*", help="实战窗起始 seq（必填或自动扫描）")
    ap.add_argument("--static-start", type=int, default=None)
    args = ap.parse_args()
    demo = Path(args.demo)
    t0 = time.time()

    g0 = json.loads(ab.GATE0_FILE.read_text(encoding="utf-8"))
    y_h, min_denom = float(g0["y_h"]), float(g0["min_denom"])
    rows = [json.loads(l) for l in
            (demo / "frames.jsonl").open(encoding="utf-8") if l.strip()]
    seq2idx = {r["seq"]: i for i, r in enumerate(rows)}
    ts_ns = {r["seq"]: r["ts_ns"] for r in rows}

    dsess = ab.depth_session()
    ysess, names = ab.yolo_session()
    print(f"[{time.time()-t0:5.1f}s] 会话就绪")

    # 静止窗 + 实战窗（飞坡标记开）
    s_start = seq2idx[args.static_start] if args.static_start is not None else None
    if s_start is None:
        scores = ab.motion_scores(demo, rows)
        s_start = ab.pick_low_motion(scores, args.winlen)
    stat = ab.run_window(dsess, ysess, names, demo, rows, s_start, args.winlen,
                         y_h, min_denom, mark_jump=True)
    print(f"[{time.time()-t0:5.1f}s] 静止窗 seq{rows[s_start]['seq']}~"
          f"{rows[s_start+args.winlen-1]['seq']}（飞坡帧 {stat['n_jump']}）")

    if args.start:
        drive_starts = [seq2idx[s] for s in args.start]
    else:
        scores = ab.motion_scores(demo, rows)
        drive_starts = ab.find_drive_windows(dsess, ysess, names, demo, rows,
                                             scores, args.winlen, 1, y_h, min_denom)
    drives = [(s, ab.run_window(dsess, ysess, names, demo, rows, s, args.winlen,
                                y_h, min_denom, mark_jump=True))
              for s in drive_starts]

    # ---------- 跨帧对 ----------
    verdict = {"meta": {"demo": demo.name, "winlen": args.winlen,
                        "ages_ms": list(AGES_MS), "tol_ms": AGE_TOL_MS,
                        "floor_inflate_max": FLOOR_INFLATE_MAX,
                        "prereg": ("实战 age≈250ms 桶：2D 重标（h/cy 取优）P50|err| < "
                                   "hold P50|err| 且静止地板 ≤ hold×1.5 → 重标有效")},
              "static": {}, "drive": {}, "per_track": []}

    def collect(res, kind) -> None:
        for t in res["tracks"]:
            if t["cls"] not in ("car", "coin") or len(t["obs"]) < MIN_TRACK:
                continue
            recs = pair_errors(t, ts_ns, y_h, min_denom)
            if not recs:
                continue
            verdict["per_track"].append({
                "kind": kind, "cls": t["cls"],
                "seq": [t["obs"][0]["fid"], t["obs"][-1]["fid"]],
                "n_obs": len(t["obs"]), "n_pairs": len(recs),
                "pairs": [{"bucket": r["bucket"], "age": r["age"],
                           "fid_i": r["fid_i"], "fid_j": r["fid_j"],
                           "hold": round(r["hold"], 3),
                           "h": round(r["h"], 3),
                           "cy": None if r["cy"] is None else round(r["cy"], 3)}
                          for r in recs],
                "by_bucket": {str(b): {m: pooled_stats(
                    [r for r in recs if r["bucket"] == b], m)
                    for m in METHODS} for b in AGES_MS}})
            for b in AGES_MS:
                sub = [r for r in recs if r["bucket"] == b]
                tgt = verdict[kind].setdefault(t["cls"], {}).setdefault(str(b), {})
                for m in METHODS:
                    st = pooled_stats(sub, m)
                    if st:
                        tgt.setdefault(m, {"n": 0, "errs": [], "signed": []})
                        tgt[m]["n"] += st["n"]
                        for r in sub:
                            if r.get(m) is not None:
                                tgt[m]["errs"].append(abs(r[m]))
                                tgt[m]["signed"].append(r[m])

    collect(stat, "static")
    for _, wres in drives:
        collect(wres, "drive")

    # 池化汇总（全部车轨同桶合并重算，防单轨挑样）
    for kind in ("static", "drive"):
        for cls, buckets in verdict[kind].items():
            for b, methods in buckets.items():
                for m in list(methods):
                    errs, signed = methods[m]["errs"], methods[m]["signed"]
                    errs.sort(); signed.sort()
                    p = lambda q: errs[min(int(len(errs) * q), len(errs) - 1)]
                    methods[m] = {"n": len(errs), "p50_abs": round(p(0.5), 3),
                                  "p90_abs": round(p(0.9), 3),
                                  "signed_med": round(statistics.median(signed), 3)}

    # ---------- 预注册判决（age≈250ms 桶，车类池化） ----------
    v = {"applicable": False, "why": None}
    d250 = verdict["drive"].get("car", {}).get("250", {})
    s250 = verdict["static"].get("car", {}).get("250", {})
    if not d250 or "hold" not in d250:
        v["why"] = "实战窗 250ms 桶车类对数不足"
    else:
        hold_d = d250["hold"]["p50_abs"]
        cand = {m: d250[m] for m in ("h", "cy")
                if m in d250 and (s250.get(m) is None or s250.get("hold") is None
                                  or s250[m]["p50_abs"] <= FLOOR_INFLATE_MAX * s250["hold"]["p50_abs"])}
        if not cand:
            v["why"] = "无 2D 法通过静止地板膨胀检查"
        else:
            bm = min(cand, key=lambda m: cand[m]["p50_abs"])
            v.update({"applicable": cand[bm]["p50_abs"] < hold_d,
                      "why": None, "method": bm,
                      "p50_250ms": {"hold": hold_d, "corrected": cand[bm]["p50_abs"]},
                      "age250_gain": round(1 - cand[bm]["p50_abs"] / hold_d, 3)
                      if hold_d > 0 else None})
    verdict["verdict"] = v

    # ---------- 落盘 ----------
    out_dir = demo.parent / "depth_review" / "jitter"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = demo.name
    jpath = out_dir / f"anchor_age_{tag}.json"
    jpath.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")

    cpath = out_dir / f"anchor_age_{tag}.csv"
    with cpath.open("w", encoding="utf-8") as f:
        f.write("kind,cls,track_seq0,track_seq1,bucket_ms,age_ms,hold,h,cy\n")
        for rec in verdict["per_track"]:
            for p in rec["pairs"]:
                f.write("%s,%s,%d,%d,%d,%.1f,%s,%s,%s\n" % (
                    rec["kind"], rec["cls"], rec["seq"][0], rec["seq"][1],
                    p["bucket"], p["age"],
                    "%.3f" % p["hold"], "%.3f" % p["h"],
                    "" if p["cy"] is None else "%.3f" % p["cy"]))

    # ---------- 图：池化车类 P50|err| 对年龄（实战）+ 静止地板参考线 ----------
    CW, CH = 620, 340
    canvas = np.full((CH + 70, CW * 2 + 40, 3), 245, np.uint8)
    for k, kind in enumerate(("drive", "static")):
        car = verdict[kind].get("car", {})
        series = []
        for m, color, label in (("hold", RED, "hold(stale Z)"),
                                ("h", BLUE, "Z * h_i/h_j"),
                                ("cy", GREEN, "Z * cy_ratio")):
            vals = [car.get(str(b), {}).get(m, {}).get("p50_abs") for b in AGES_MS]
            if any(v is not None for v in vals):
                series.append((vals, color, label))
        hlines = []
        s_car = verdict["static"].get("car", {})
        hold_floor = s_car.get("250", {}).get("hold", {}).get("p50_abs")
        if kind == "drive" and hold_floor is not None:
            hlines.append((hold_floor, GRAY, "static hold floor"))
        multi_chart(canvas, 70 + k * (CW + 40), 50, CW - 110, CH, series,
                    AGES_MS,
                    f"{kind}: car P50|err| (m) vs anchor age (ms)", hlines)
    cv2.imwrite(str(out_dir / f"anchor_age_{tag}.jpg"),
                cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))

    # 身份目检材料：每条实战车轨两张——整轨条（首/中/尾）+ 判决对条
    # （250ms 桶取前 2 对：锚点帧与实帧同图标注框，验证进判决的对本身）
    car_recs = [r for r in verdict["per_track"]
                if r["kind"] == "drive" and r["cls"] == "car"
                and r["n_pairs"] >= 10]
    for rec in car_recs:
        wt = None
        for s, wres in drives:
            if s <= seq2idx[rec["seq"][0]] <= s + args.winlen - 1:
                wt = next((t for t in wres["tracks"] if t["cls"] == "car"
                           and t["obs"][0]["fid"] == rec["seq"][0]), None)
                if wt is not None:
                    break
        if wt is None:
            continue
        ab.render_track_strip(
            demo, rows, wt, out_dir / f"anchor_age_{tag}_id_{rec['seq'][0]}.jpg")
        box_by_fid = {o["fid"]: o["box"] for o in wt["obs"]}
        prs = [p for p in rec["pairs"] if p["bucket"] == 250][:2]
        panels = []
        for p in prs:
            for fid in (p["fid_i"], p["fid_j"]):
                img = cv2.cvtColor(cv2.imread(
                    str(demo / "frames" /
                        next(r["file"] for r in rows if r["seq"] == fid))),
                    cv2.COLOR_BGR2RGB)
                x0, y0, x1, y1 = box_by_fid[fid]
                cv2.rectangle(img, (x0, y0), (x1, y1), RED, 2)
                cv2.putText(img, f"{fid}", (x0, max(14, y0 - 5)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, WHITE, 1)
                panels.append(cv2.resize(img, (640, 360)))
        if panels:
            cv2.imwrite(str(out_dir / f"anchor_age_{tag}_pairs_{rec['seq'][0]}.jpg"),
                        cv2.cvtColor(np.hstack(panels), cv2.COLOR_RGB2BGR))

    # ---------- 控制台摘要 ----------
    print(f"[{time.time()-t0:5.1f}s] 逐对真值门后，池化车类 P50|err|（米）对年龄：")
    for kind in ("drive", "static"):
        car = verdict[kind].get("car", {})
        line = f"    {kind:6s}"
        for b in AGES_MS:
            cell = car.get(str(b), {})
            line += f" {b}ms:" + "/".join(
                f"{m}={cell[m]['p50_abs']:.2f}(n={cell[m]['n']})"
                for m in METHODS if m in cell) + " |"
        print(line)
    if v["why"]:
        print(f"    ⇒ 判决未成立：{v['why']}")
    else:
        print(f"    ⇒ 预注册判决：{v['method']} 重标 P50={v['p50_250ms']['corrected']:.2f}m"
              f" vs hold {v['p50_250ms']['hold']:.2f}m（250ms 桶削 {v['age250_gain']*100:.0f}%）"
              f" → {'重标有效' if v['applicable'] else '未过砍线，挂账'}")
    print("wrote", jpath)


if __name__ == "__main__":
    main()
