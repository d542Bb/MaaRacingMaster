# -*- coding: utf-8 -*-
"""平面持拍反事实：同拍共模跳变里，平面坡度占几成、点云占几成。

**要回答什么**（离线，只读回放帧，不动产线）：区域路心逐拍跳 1~2 m 时，两个
独立读数（绿区边界 / 逐箱找边）会同拍一起跳——共模说明问题在两者共享的上游。
上游只有两个：路面平面（a/b/c）与点云本身。本探针把每对相邻帧的跳变按
Shapley 拆成两份：

    A = f(点云_{t-1}, 平面_{t-1})      观测的上一拍
    B = f(点云_t,   平面_t)            观测的当前拍
    C = f(点云_t,   平面_{t-1})        点云换、平面冻结（=「锁平面」反事实）
    D = f(点云_{t-1}, 平面_t)          平面换、点云冻结
    平面贡献 = ½[(D−A) + (B−C)]        点云贡献 = ½[(C−A) + (B−D)]

「平面贡献大」→ 平面是估计器可锁的量（刚体机位应连续），改平面连续性；
「点云贡献大」→ 锁平面无用，改「从可行驶区求中心」的取法。两种改法不同文件。

**选帧纪律**：连续回放一段录像的全部帧，对**每一对**相邻帧做拆解并按类别汇总，
不按结果挑样本。类别沿用 trace 侧同一判据（common/single/plane/base），只用于
分组报告。顺带逐帧记平面侧倾/俯仰与机位高——刚体机位下机位高能不能当常数，
看这列。

**与产线对账**：产线深度结果是异步的（消费拍比深度帧晚 2~5 拍），所以逐 fid
对不上；本探针用整段相关（在 0~6 拍范围内找最佳滞后）确认回放复现了产线序列。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_geom/probe_plane_hold.py 20261022_225245_p2 \
        --start 100 --count 120 [--out-dir %TEMP%/plane_hold]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE)
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    GRID_BLOCKED, GRID_DRIVABLE, GRID_UNKNOWN, GRID_X_MAX, GRID_Z_LO,
    NEAR_Z, _band_rows, _fit_road_plane, _row_intervals, _weighted_median,
    drivable_grid_from_points, infer_points, load_session, reading_from_points,
    region_reading)
from maaracing_master.plugins.speedrush.module import _yolo_object_mask  # noqa: E402
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DEBUG = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster"
         / "debug" / "speedrush")
CAM_W = 1280
T = 0.15          # 单侧「动了」的判据（道）


def _rows(name: str) -> list[dict]:
    f = DEBUG / name / f"trace_{name}.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x]


def _frame(name: str, ridx: int) -> np.ndarray | None:
    f = DEBUG / name / "raw" / f"{ridx + 1:04d}_raw.jpg"
    return (np.asarray(Image.open(f).convert("RGB")) if f.exists() else None)


class _Tee:
    """stdout 双写：控制台照常，另存一份 UTF-8 报告（控制台编码不参与）。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def _q(a, p):
    a = np.asarray([x for x in a if x is not None and np.isfinite(x)], float)
    return float(np.percentile(a, p)) if a.size else float("nan")


def _f(x, nd=2):
    return "—" if x is None else f"{x:+.{nd}f}"


def _ab(d):
    if d is None or d.get("ab", (None, None))[0] is None:
        return "—"
    return f"{d['ab'][0]:+.2f}/{d['ab'][1]:+.2f}"


# ---------------------------------------------------------------- 单帧重放

def replay(sess, perc, cal, rgb: np.ndarray) -> dict:
    res = perc.detect(rgb)
    mask = _yolo_object_mask(res)
    pts, fx, fy = infer_points(sess, rgb, native=True)
    ds = CAM_W / float(pts.shape[1])
    mask_g = (None if mask is None else
              cv2.resize(mask.astype(np.uint8), (pts.shape[1], pts.shape[0]),
                         interpolation=cv2.INTER_NEAREST).astype(bool))
    y0, y1 = _band_rows(pts.shape[0])
    X, Y, Z = pts[y0:y1, :, 0], pts[y0:y1, :, 1], pts[y0:y1, :, 2]
    coef = _fit_road_plane(X, Y, Z, fy=fy / ds)
    return {"pts": pts, "fx": fx, "fy": fy, "ds": ds, "mask": mask_g,
            "coef": None if coef is None else tuple(float(v) for v in coef),
            "ncar": len(res.cars)}


class _GridShim:
    """只带 state 的栅格替身（_row_intervals 只读 state）。"""

    def __init__(self, state):
        self.state = state


def center_win(state, lane_w: float, z0: float, z1: float) -> float | None:
    """只用 z∈[z0,z1) 的行求路心（米，右正）；行集固定=不随覆盖漂移。"""
    sh = _GridShim(state.copy())
    zc = GRID_Z_LO + (np.arange(sh.state.shape[0]) + 0.5) * 0.25
    sh.state[(zc < z0) | (zc >= z1)] = GRID_UNKNOWN
    iv = _row_intervals(sh)
    if iv is None:
        return None
    lo_m, hi_m = iv
    w = (hi_m - lo_m) / lane_w
    left = _weighted_median(lo_m / lane_w, w)
    right = _weighted_median(hi_m / lane_w, w)
    return -((left + right) / 2.0 * lane_w)


def eval_plane(rec: dict, coef, cal, lane_w: float) -> dict:
    """点云 + 指定平面 → 区域路心 / 找边对心（米，右正）。"""
    if coef is None:
        return {"off": None, "w": None, "edge": None, "sides": 0,
                "ab": (None, None), "c": None, "grid": None}
    grid = drivable_grid_from_points(rec["pts"], rec["fx"], rec["fy"],
                                     rec["mask"], coef=coef, ds=rec["ds"])
    reg = region_reading(grid, lane_w)
    rd = reading_from_points(rec["pts"], rec["fx"], cal,
                             object_mask=rec["mask"], fy=rec["fy"],
                             coef=coef, ds=rec["ds"])
    sides = reg.sides
    off = (None if sides != 2 else
           -((reg.left_edge_lane + reg.right_edge_lane) / 2.0 * lane_w))
    edge = (None if (rd.left_edge_lane is None or rd.right_edge_lane is None)
            else -((rd.left_edge_lane + rd.right_edge_lane) / 2.0 * lane_w))
    return {"off": off,
            "w": (None if sides != 2 else
                  (reg.right_edge_lane - reg.left_edge_lane) * lane_w),
            "edge": edge, "sides": sides,
            "ab": (math.degrees(math.atan(coef[0])),
                   math.degrees(math.atan(coef[1]))),
            "c": abs(coef[2]), "grid": grid.state,
            "off_win": center_win(grid.state, lane_w, 6.0, 9.0)}


# ---------------------------------------------------------------- 可视化

def bev_image(state, title: str) -> Image.Image:
    iz = int(round((16.0 - 3.0) / 0.25))
    ix0 = int(round((GRID_X_MAX - 10.5) / 0.25))
    ix1 = int(round((GRID_X_MAX + 10.5) / 0.25))
    sub = state[:iz, ix0:ix1]
    img = np.zeros(sub.shape + (3,), np.uint8)
    img[sub == GRID_DRIVABLE] = (40, 190, 90)
    img[sub == GRID_BLOCKED] = (200, 60, 60)
    im = Image.fromarray(img).resize((sub.shape[1] * 3, sub.shape[0] * 3),
                                     Image.NEAREST)
    ImageDraw.Draw(im).text((6, 4), title, fill=(255, 255, 255))
    return im


def overlay(own: dict, held: dict | None, path: Path, cap: str) -> None:
    ims = [bev_image(own["grid"], "own plane")]
    if held is not None and held.get("grid") is not None:
        ims.append(bev_image(held["grid"], "held plane"))
    w = sum(i.width for i in ims) + 10 * (len(ims) - 1)
    out = Image.new("RGB", (w, max(i.height for i in ims) + 42), (0, 0, 0))
    x = 0
    for im in ims:
        out.paste(im, (x, 36))
        x += im.width + 10
    d = ImageDraw.Draw(out)
    d.text((6, 4), cap, fill=(230, 230, 230))
    d.text((6, 20), "green=drivable red=blocked black=unknown  near band 3-16 m",
           fill=(170, 170, 170))
    out.save(path)


# ---------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--out-dir", default=str(Path(os.environ.get("TEMP", "."))
                                            / "plane_hold"))
    a = ap.parse_args()
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sys.stdout = _Tee(sys.stdout,
                      open(out_dir / "report.txt", "w", encoding="utf-8"))

    cal = load_calib()
    lane_w = cal.lane_w_m
    sess = load_session(DEPTH_MODEL_FILE)
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    rows = _rows(a.session)
    idxs = list(range(a.start, min(a.start + a.count, len(rows)), a.stride))
    seg = [rows[i] for i in idxs]
    fids = [r["fid"] for r in seg]
    print(f"回放 {a.session} 第 {a.start}~{idxs[-1]} 行，步长 {a.stride}"
          f"（fid {fids[0]}~{fids[-1]}），{len(seg)} 帧")

    frames: list[dict] = []
    prev = None
    pair_rows = []
    for i, (r, ridx) in enumerate(zip(seg, idxs)):
        rgb = _frame(a.session, ridx)
        if rgb is None:
            prev = None
            continue
        rec = replay(sess, perc, cal, rgb)
        own = eval_plane(rec, rec["coef"], cal, lane_w)
        cr = {"fid": r["fid"], "ts": r["ts_ns"] / 1e9, **rec, "own": own}
        frames.append(cr)
        if prev is not None and prev["coef"] is not None \
                and rec["coef"] is not None:
            C = eval_plane(rec, prev["coef"], cal, lane_w)
            D = eval_plane(prev, rec["coef"], cal, lane_w)
            pair_rows.append({"a": prev, "b": cr, "C": C, "D": D})
        prev = cr
        if (i + 1) % 20 == 0:
            print(f"  已重放 {i+1}/{len(seg)}")

    with open(out_dir / "frames.jsonl", "w", encoding="utf-8") as f:
        for fr in frames:
            f.write(json.dumps({
                "fid": fr["fid"], "ts": fr["ts"],
                "off": fr["own"]["off"], "w": fr["own"]["w"],
                "edge": fr["own"]["edge"], "c": fr["own"]["c"],
                "roll": fr["own"]["ab"][0], "pitch": fr["own"]["ab"][1],
                "ncar": fr["ncar"]}, ensure_ascii=False) + "\n")

    # ---- 与产线序列对账（异步：找最佳滞后）
    prod = {r["fid"]: r.get("ro_off_raw") for r in seg}
    best = (None, 0.0, 0)
    rep_off = {f["fid"]: (None if f["own"]["off"] is None
                          else f["own"]["off"] / lane_w) for f in frames}
    ridx = {r["fid"]: i for i, r in enumerate(rows)}
    for lead in range(0, 9):        # 产线消费拍在深度帧之后 N 拍（异步驻留）
        xs, ys = [], []
        for f in frames:
            j = ridx.get(f["fid"])
            if j is None or j + lead >= len(rows):
                continue
            p = rows[j + lead].get("ro_off_raw")
            v = rep_off.get(f["fid"])
            if p is not None and v is not None:
                xs.append(p)
                ys.append(v)
        if len(xs) > 20:
            c = float(np.corrcoef(xs, ys)[0, 1])
            if abs(c) > abs(best[1]):
                best = (lead, c, len(xs))
    print(f"\n对账：回放序列 vs 产线新鲜读数 最佳滞后 {best[0]} 拍"
          f"  corr {best[1]:+.2f}  n={best[2]}"
          f"（产线消费拍比深度帧晚 2~5 拍属正常）")
    rs = [f["own"]["off"] / lane_w for f in frames if f["own"]["off"] is not None]
    ps = [v for v in (prod.get(f) for f in fids) if v is not None]
    print(f"  回放新鲜读数 n={len(rs)} p5 {_q(rs,5):+.2f} p50 {_q(rs,50):+.2f}"
          f" p95 {_q(rs,95):+.2f} 道 | 产线 n={len(ps)} p5 {_q(ps,5):+.2f}"
          f" p50 {_q(ps,50):+.2f} p95 {_q(ps,95):+.2f} 道")
    dv_rep = np.abs(np.diff([v for v in rs if np.isfinite(v)])) * lane_w
    print(f"  回放 |Δ| p50 {_q(dv_rep,50):.2f} p90 {_q(dv_rep,90):.2f} m；"
          f">1 m 占 {100*np.mean(dv_rep>1.0):.0f}%")

    # ---- 平面自身逐拍抖动
    ar = [f["own"]["ab"] for f in frames if f["own"]["ab"][0] is not None]
    droll = np.abs(np.diff([x[0] for x in ar]))
    dpitch = np.abs(np.diff([x[1] for x in ar]))
    ch = [f["own"]["c"] for f in frames if f["own"]["c"] is not None]
    print(f"\n平面逐拍：|Δroll| p50 {_q(droll,50):.2f}° p90 {_q(droll,90):.2f}°"
          f"   |Δpitch| p50 {_q(dpitch,50):.2f}° p90 {_q(dpitch,90):.2f}°")
    print(f"  机位高 p5 {_q(ch,5):.2f} p50 {_q(ch,50):.2f} p95 {_q(ch,95):.2f}"
          f" p99 {_q(ch,99):.2f} m")

    # ---- Shapley 拆解
    med = float(np.median(ch)) if ch else float("nan")
    stats = {c: {"plane": [], "pts": [], "total": [], "hold": []}
             for c in ("common", "single", "plane", "base", "all")}
    fail = 0
    fail_ab = []
    overlays = []
    for p in pair_rows:
        A = p["a"]["own"]
        B = p["b"]["own"]
        C, D = p["C"], p["D"]
        if None in (A["off"], B["off"], C["off"], D["off"]):
            fail += 1
            pa, pb = p["a"]["own"]["ab"], p["b"]["own"]["ab"]
            if pa[0] is not None and pb[0] is not None:
                fail_ab.append((abs(pb[0] - pa[0]), abs(pb[1] - pa[1])))
            continue
        plane = 0.5 * ((D["off"] - A["off"]) + (B["off"] - C["off"]))
        pts = 0.5 * ((C["off"] - A["off"]) + (B["off"] - D["off"]))
        total = B["off"] - A["off"]
        hold = C["off"] - A["off"]
        wA, wB = A["w"], B["w"]
        if wA is None or wB is None:
            continue
        eL_a, eL_b = -A["off"] - wA / 2, -B["off"] - wB / 2
        eR_a, eR_b = -A["off"] + wA / 2, -B["off"] + wB / 2
        dLs, dRs = abs(eL_b - eL_a), abs(eR_b - eR_a)
        d_off, t_m = abs(total), T * lane_w
        if (abs(A["c"] - med) > 0.2) or (abs(B["c"] - med) > 0.2):
            cls = "plane"
        elif d_off > 0.5 and ((dLs > t_m) != (dRs > t_m)):
            cls = "single"
        elif d_off > 1.0:
            cls = "common"
        elif d_off < 0.15:
            cls = "base"
        else:
            continue
        p["cls"] = cls
        for k in (cls, "all"):
            stats[k]["plane"].append(plane)
            stats[k]["pts"].append(pts)
            stats[k]["total"].append(total)
            stats[k]["hold"].append(1.0 - abs(hold) / abs(total)
                                    if abs(total) > 1e-6 else 1.0)
        if abs(total) > 1.0 and len(overlays) < 8:
            overlays.append((p, cls))

    print(f"\n拆解（{len(pair_rows)} 对；组合弃权 {fail} 对）")
    hdr = ("类别", "n", "Var(总)", "平面占比", "点云占比", "|平面|p50",
           "|点云|p50", "锁平面消掉p50")
    print("  " + " ".join(f"{h:>11s}" for h in hdr))
    for k in ("common", "single", "plane", "base", "all"):
        st = stats[k]
        if len(st["total"]) < 3:
            continue
        t = np.asarray(st["total"])
        pl = np.asarray(st["plane"])
        pt = np.asarray(st["pts"])
        vt = float(np.var(t))
        sh_p = float(np.cov(pl, t)[0, 1] / vt) if vt > 1e-9 else float("nan")
        sh_q = float(np.cov(pt, t)[0, 1] / vt) if vt > 1e-9 else float("nan")
        print("  " + " ".join(f"{v:>11s}" for v in (
            k, str(len(t)), f"{vt:.2f}", f"{100*sh_p:.0f}%", f"{100*sh_q:.0f}%",
            f"{_q(np.abs(pl),50):.2f}", f"{_q(np.abs(pt),50):.2f}",
            f"{100*_q(st['hold'],50):.0f}%")))

    def jit(key, only_cls=None):
        vals = []
        for p in pair_rows:
            if only_cls is not None and p.get("cls") != only_cls:
                continue
            a = p["a"]["own"].get(key)
            b = p["b"]["own"].get(key)
            if a is not None and b is not None:
                vals.append(abs(b - a))
        return _q(vals, 50), _q(vals, 90), len(vals)

    print("\n估计器对比（同一帧、同一栅格，只换用哪些行）")
    for lab, key in (("全带 3-16m（产线口径）", "off"), ("固定 6-9m 行", "off_win")):
        m50, m90, n = jit(key)
        c50, c90, cn = jit(key, "common")
        print(f"  {lab:22s} 全体 |Δ| p50 {m50:.2f} p90 {m90:.2f} m (n={n})"
              f"   common 组 p50 {c50:.2f} p90 {c90:.2f} m (n={cn})")

    if fail_ab:
        dr = [x[0] for x in fail_ab]
        dp = [x[1] for x in fail_ab]
        tri = [x for x in pair_rows
               if None not in (x["a"]["own"]["off"], x["b"]["own"]["off"],
                               x["C"]["off"], x["D"]["off"])]
        dri = [abs(x["b"]["own"]["ab"][0] - x["a"]["own"]["ab"][0]) for x in tri]
        dpi = [abs(x["b"]["own"]["ab"][1] - x["a"]["own"]["ab"][1]) for x in tri]
        print(f"  弃权对被排除时平面动了多少：|Δroll| p50 {_q(dr,50):.2f}° "
              f"|Δpitch| p50 {_q(dp,50):.2f}°（纳入对 {_q(dri,50):.2f}°/"
              f"{_q(dpi,50):.2f}°）——若弃权对平面动得更多，说明结果对平面偏乐观")

    for p, cls in overlays:
        cap = (f"{cls} fid {p['a']['fid']}->{p['b']['fid']}  "
               f"off {_f(p['a']['own']['off'])}->{_f(p['b']['own']['off'])}  "
               f"held {_f(p['C']['off'])}")
        overlay(p["b"]["own"], p["C"],
                out_dir / f"pair_{p['a']['fid']}_{p['b']['fid']}.png", cap)
    print(f"\n叠加图：{out_dir}（{len(overlays)} 张）")


if __name__ == "__main__":
    main()
