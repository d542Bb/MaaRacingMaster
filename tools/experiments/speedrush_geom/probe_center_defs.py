# -*- coding: utf-8 -*-
"""三种「路心」取法对比（全量回放，只读录像，不动产线）。

**要回答什么**：同一个可行驶栅格，换一种求路心的方式，抖动/弯道偏差/与找边
读数的分歧各是多少。三种取法：

  1. 全带中位（产线口径）：3~16 m 全部行，逐行取 [min,max]，对缘位置按行宽
     加权取中位，两缘再平均；
  2. 固定窄窗：只取 6~9 m 的行，其余同上（把「量哪一段路」钉住）；
  3. 逐行中心直线拟合：逐行取区间中点，按行宽加权对 (z, x) 拟合直线，
     在自车处（z=0）取值——把整条带子当成一条直线，读车在路里的横向位置。

**判据**：没有独立横向真值（裁判只有纵向速度），所以不比「准」，只比三件可测的事：
  · 抖动：相邻新鲜帧（100 ms 间隔）的 |Δ|；
  · 弯道依赖：按「远端与近端的中点差」分弯度组，看各取法的值是否随弯度系统平移
    （真值若存在，车在路里的横向位置不该因为前方弯道而整体平移）；
  · 与找边对心的分歧（另一台独立读数，同一份点云）。

**对账**：与产线新鲜读数做异步对齐（产线消费拍晚 2~5 拍），确认回放口径一致。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_geom/probe_center_defs.py \
        20261022_225245_p2 20261022_172231_p2 [--stride 2 --out-dir %TEMP%/center_defs]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE)
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    GRID_DRIVABLE, GRID_UNKNOWN, GRID_X_MAX, GRID_Z_LO, NEAR_Z,
    REGION_X_MAX, load_session, region_reading)
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from probe_plane_hold import _frame, _q, _rows, eval_plane, replay  # noqa: E402

DEBUG = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster"
         / "debug" / "speedrush")


class _Shim:
    def __init__(self, state):
        self.state = state


def _rows_state(state, z0: float, z1: float):
    sh = _Shim(state.copy())
    zc = GRID_Z_LO + (np.arange(sh.state.shape[0]) + 0.5) * 0.25
    sh.state[(zc < z0) | (zc >= z1)] = GRID_UNKNOWN
    return sh


def _median_center(state, lane_w: float, z0: float, z1: float):
    """窗口内行 → 产线同一公式（region_reading）→ 路心（米，右正）。"""
    reg = region_reading(_rows_state(state, z0, z1), lane_w)
    if reg.sides != 2:
        return None
    return -((reg.left_edge_lane + reg.right_edge_lane) / 2.0 * lane_w)


def _win_stats(state, lane_w: float, z0: float, z1: float, fx: float):
    """窗口内：带宽（米）与「被视锥截断」的行占比。

    视锥在距离 z 处的半宽 = (640/fx)·z。窗口里若缘位置贴住视锥界（差 <0.5 m），
    该行看到的是画面边缘而不是路缘——此时路心读数退化成相机轴，抖动小但没有信息。"""
    iv = _intervals_z(state)
    if iv is None:
        return None, None
    zc, lo, hi = iv
    m = (zc >= z0) & (zc < z1)
    if m.sum() < 3:
        return None, None
    zc, lo, hi = zc[m], lo[m], hi[m]
    fov = (640.0 / fx) * zc
    lim = (np.abs(lo) >= fov - 0.5) | (hi >= fov - 0.5)
    return float(np.median(hi - lo)), float(lim.mean())


def _intervals_z(state):
    """逐行绿区 [min,max] 与行 z（与 depth_geo._row_intervals 同口径的本地副本：
    同一 NEAR_Z / REGION_X_MAX / 格宽裁剪；多返回行 z，供直线拟合用）。"""
    d = state == GRID_DRIVABLE
    zc = GRID_Z_LO + (np.arange(state.shape[0]) + 0.5) * 0.25
    d[zc >= NEAR_Z] = False
    xs = -GRID_X_MAX + (np.arange(state.shape[1]) + 0.5) * 0.25
    d[:, np.abs(xs) > REGION_X_MAX] = False
    has = d.any(axis=1)
    if not has.any():
        return None
    cols = np.arange(state.shape[1])
    lo_ix = np.where(d, cols, state.shape[1]).min(axis=1)[has]
    hi_ix = np.where(d, cols, -1).max(axis=1)[has]
    return (zc[has], -GRID_X_MAX + lo_ix * 0.25,
            -GRID_X_MAX + (hi_ix + 1) * 0.25)


def _line_center(state, lane_w: float, z_eval: float = 0.0):
    """逐行区间中点 → 按行宽加权最小二乘直线 → z_eval 处的横位（米，右正）。

    返回 (x_at_z_eval, slope, nrows)；行数不足/退化 → (None, None, n)。"""
    iv = _intervals_z(state)
    if iv is None:
        return None, None, 0
    zc, lo_m, hi_m = iv
    mid = (lo_m + hi_m) / 2.0
    w = hi_m - lo_m
    keep = w > 0.5
    zc, mid, w = zc[keep], mid[keep], w[keep]
    n = int(keep.sum())
    if n < 8:
        return None, None, n
    w = w / w.sum()
    zbar = float(np.sum(w * zc))
    xbar = float(np.sum(w * mid))
    dz = zc - zbar
    sxx = float(np.sum(w * dz * dz))
    if sxx < 1e-6:
        return None, None, n
    slope = float(np.sum(w * dz * (mid - xbar)) / sxx)
    # 与其余估计器统一为 off 口径（off = −center，米，右正）
    return -(xbar + slope * (z_eval - zbar)), -slope, n


def _quad_center(state, lane_w: float, z_eval: float = 0.0):
    """逐行区间中点 → 加权二次拟合 → z_eval 处横位（off 口径，米）。

    直线拟合对「弯带」的截距会过冲（见报告：斜率 −0.45）；路面在自车系里的
    中心近似 x(z)=½κz²，二次项正好吸收曲率。返回 (off, κ, n)。"""
    iv = _intervals_z(state)
    if iv is None:
        return None, None, 0
    zc, lo_m, hi_m = iv
    mid = (lo_m + hi_m) / 2.0
    w = hi_m - lo_m
    keep = w > 0.5
    zc, mid, w = zc[keep], mid[keep], w[keep]
    n = int(keep.sum())
    if n < 12:
        return None, None, n
    w = w / w.sum()
    A = np.stack([zc ** 2, zc, np.ones_like(zc)], 1)
    Aw = A * np.sqrt(w)[:, None]
    coef = np.linalg.lstsq(Aw, mid * np.sqrt(w), rcond=None)[0]
    x = float(coef[0] * z_eval ** 2 + coef[1] * z_eval + coef[2])
    return -x, float(coef[0]), n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--out-dir", default=str(Path(os.environ.get("TEMP", "."))
                                            / "center_defs"))
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    class Tee:
        def __init__(self, *s):
            self.s = s

        def write(self, t):
            for x in self.s:
                x.write(t)
            return len(t)

        def flush(self):
            for x in self.s:
                x.flush()

    sys.stdout = Tee(sys.stdout, open(out / "report.txt", "w", encoding="utf-8"))

    cal = load_calib()
    lw = cal.lane_w_m
    sess = load_session(DEPTH_MODEL_FILE)
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))

    for name in a.sessions:
        rows = _rows(name)
        idxs = list(range(0, len(rows), a.stride))
        recs = []
        for k, i in enumerate(idxs):
            rgb = _frame(name, i)
            if rgb is None:
                continue
            rec = replay(sess, perc, cal, rgb)
            ev = eval_plane(rec, rec["coef"], cal, lw)
            if ev["grid"] is None or ev["off"] is None:
                continue
            st = ev["grid"]
            ob = ev["off"]
            ow = ev["off_win"]
            ol, slope, nrow = _line_center(st, lw, 0.0)
            ol8, _, _ = _line_center(st, lw, 8.0)
            w_win, fov_win = _win_stats(st, lw, 6.0, 9.0, rec["fx"])
            w_band, fov_band = _win_stats(st, lw, 3.0, 16.0, rec["fx"])
            oq, kap, _ = _quad_center(st, lw, 0.0)
            near = _median_center(st, lw, 3.0, 6.0)
            far = _median_center(st, lw, 12.0, 16.0)
            bend = (None if near is None or far is None else far - near)
            r = rows[i]
            recs.append({"fid": r["fid"], "ts": r["ts_ns"] / 1e9,
                         "band": ob, "win": ow, "line": ol, "line8": ol8,
                         "quad": oq, "kappa": kap,
                         "win_w": w_win, "win_fov": fov_win,
                         "band_w": w_band, "band_fov": fov_band,
                         "edge": ev["edge"], "bend": bend, "slope": slope,
                         "nrow": nrow, "c": ev["c"],
                         "steer": abs(r.get("steer_norm") or 0.0),
                         "dgeo_new": bool(r.get("dgeo_new"))})
            if (k + 1) % 100 == 0:
                print(f"  {name}: {k+1}/{len(idxs)}")

        # 只取「新证据」拍（与产线一致的 10 Hz 采样）；其余为保持拍
        fresh = recs
        print("\n" + "=" * 100)
        print(f"{name}：回放 {len(recs)} 帧，新鲜拍 {len(fresh)}，"
              f"段长 {recs[-1]['ts']-recs[0]['ts']:.0f}s")

        # 对账
        by_fid = {r["fid"]: r for r in rows}
        rid = {r["fid"]: i for i, r in enumerate(rows)}
        best = (0, 0.0, 0)
        for lead in range(0, 8):
            xs, ys = [], []
            for r in fresh:
                j = rid[r["fid"]]
                if j + lead >= len(rows):
                    continue
                p = rows[j + lead].get("ro_off_raw")
                if p is not None:
                    xs.append(p * lw)
                    ys.append(r["band"])
            if len(xs) > 20:
                c = float(np.corrcoef(xs, ys)[0, 1])
                if abs(c) > abs(best[1]):
                    best = (lead, c, len(xs))
        print(f"  对账：产线消费拍晚 {best[0]} 拍，corr {best[1]:+.2f} n={best[2]}")

        # 抖动
        print("\n  ── 抖动（相邻新鲜拍，间隔约 100 ms）")
        print(f"  {'估计器':<16s}{'可用':>7s}{'|Δ|p50':>9s}{'|Δ|p90':>9s}"
              f"{'|Δ|p99':>9s}{'与找边差p50':>12s}{'p90':>8s}")
        for key, lab in (("band", "全带中位(产线)"), ("win", "固定窗 6-9m"),
                         ("line", "直线拟合@自车"), ("line8", "直线拟合@8m"),
                         ("quad", "二次拟合@自车"),
                         ("edge", "找边对心(参照)")):
            v = [r[key] for r in fresh]
            ok = [x for x in v if x is not None]
            d = [abs(b - a) for a, b in zip(v, v[1:])
                 if a is not None and b is not None]
            dv = [abs(r[key] - r["edge"]) for r in fresh
                  if key != "edge" and r[key] is not None
                  and r["edge"] is not None]
            tail = (f"{_q(dv, 50):12.2f}{_q(dv, 90):8.2f}" if dv else " " * 20)
            print(f"  {lab:<16s}{100*len(ok)/max(1,len(fresh)):6.0f}%"
                  f"{_q(d,50):9.2f}{_q(d,90):9.2f}{_q(d,99):9.2f}{tail}")

        # 弯道依赖
        print("\n  ── 弯度分组（弯度 = 12~16m 中点 − 3~6m 中点，米）")
        bd = [(r, abs(r["bend"])) for r in fresh if r["bend"] is not None]
        if bd:
            cuts = np.percentile([b for _, b in bd], [50, 80, 95])
            groups = [("<中位", 0, cuts[0]), ("中位~p80", cuts[0], cuts[1]),
                      ("p80~p95", cuts[1], cuts[2]), (">p95", cuts[2], 1e9)]
            print(f"  {'弯度组':<10s}{'n':>5s}{'弯度p50':>9s}"
                  f"{'带中位 值p50/抖动p50':>24s}{'固定窗 值p50/抖动p50':>24s}"
                  f"{'拟合@0':>24s}{'二次@0':>24s}")
            for lab, lo, hi in groups:
                g = [r for r, b in bd if lo <= b < hi]
                if len(g) < 10:
                    continue
                cells = []
                for key in ("band", "win", "line", "quad"):
                    vals = [r[key] for r in g]
                    d = [abs(b - a) for a, b in zip(vals, vals[1:])
                         if a is not None and b is not None]
                    cells.append(f"{np.median([v for v in vals if v is not None]):+6.2f}"
                                 f"/{_q(d,50):5.2f}")
                bm = np.median([abs(r["bend"]) for r in g])
                print(f"  {lab:<10s}{len(g):5d}{bm:9.2f}"
                      + "".join(f"{c:>24s}" for c in cells))

        # 弯度与读数的系统关系（回归斜率）
        print("\n  ── 值随弯度的系统平移（对弯度做线性回归的斜率，米/米）")
        for key, lab in (("band", "全带中位"), ("win", "固定窗"),
                         ("line", "拟合@0"), ("quad", "二次@0")):
            xs, ys = [], []
            for r in fresh:
                if r["bend"] is not None and r[key] is not None:
                    xs.append(r["bend"])
                    ys.append(r[key])
            if len(xs) > 30:
                k = float(np.polyfit(xs, ys, 1)[0])
                cc = float(np.corrcoef(xs, ys)[0, 1])
                print(f"  {lab:<8s} 斜率 {k:+.3f}  相关 {cc:+.2f}  n={len(xs)}")

        # 拟合斜率本身
        sl = [abs(r["slope"]) for r in fresh if r["slope"] is not None]
        print(f"\n  直线斜率 |s| p50 {_q(sl,50):.3f} p90 {_q(sl,90):.3f}"
              f" p99 {_q(sl,99):.3f}（米/米）")

        with open(out / f"frames_{name}.jsonl", "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n数据：{out}")


if __name__ == "__main__":
    main()
