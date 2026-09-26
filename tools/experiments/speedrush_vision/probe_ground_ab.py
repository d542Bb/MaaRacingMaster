"""地面基线 A/B：逐行 q20（现产码）vs 侧倾校正基线场（候选修复原型）。

根因（2026-09-25 22:04 局证据包实证）：追逐相机随转向侧倾（急转帧 iso-深度
线斜 ~2-4°，行内视差左近右远差五成）后，逐行 q20 钉住行内最远列，其余路面
成假隆起块——实机读数 25% 非空 + 垃圾缘、车横撞马路牙子。原型：t 网格搜
「地面解释率」（rel 落在晕影带内的像素占比）显著最大者（TILT_MARGIN 余量，
平路帧宁取 t=0），基线沿等深度线取逐行 q20；t=0 与产码严格同形。

金标卷裁决（45 帧有标，gate=产码 0.08/0.10，2026-09-25）：
- 旧（row-q20）      L 43/45 dev -0.050 p90 0.73 坏6 | R 16/45 dev -0.045
  p90 0.44 坏1 | 双侧 15
- 新（margin=0.05）  L 43/45 dev -0.077 p90 1.17 坏6 | R 19/45 dev -0.062
  p90 1.34 坏3 | 双侧 18
覆盖大涨（R +19%、双侧 +20%）、中位 dev 持平，但 dev 尾部与 R 坏读变差
——0.08/0.10 门是对旧基线标定的，**落产码前须对新基线重过门网格**，并复查
518 骑缘锚点（新基线读 L -1.67 vs 金标 -0.394，骑缘姿态与侧倾耦合待查）。

用法：python tools/experiments/speedrush_vision/probe_ground_ab.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
NPY_DIR = pd.OUT / "npy"
TILT_GRID = tuple(np.linspace(-0.12, 0.12, 13))
TILT_PIVOT_X = 640.0
TILT_MARGIN = 0.05
GROUND_RESID_LO, GROUND_RESID_HI = -0.04, 0.03


def _tilt_field(m: np.ndarray) -> np.ndarray:
    """候选修复原型：侧倾校正基线场（t 网格 + 地面解释率 + 余量门槛）。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    h, w = band.shape
    cols = np.r_[0:dg.EGO_COLS[0], dg.EGO_COLS[1]:w]
    ys = np.arange(h, dtype=np.float32)[:, None]
    xs = (np.arange(w, dtype=np.float32)[None, :] - TILT_PIVOT_X)
    sub = (slice(None, None, 3), slice(None, None, 4))
    profs, scores = {}, {}
    for t in TILT_GRID:
        if t == 0.0:
            prof = np.quantile(band[:, cols], dg.Q_GROUND, axis=1)
        else:
            mmat = np.float32([[1, 0, 0], [t, 1, -TILT_PIVOT_X * t]])
            sheared = cv2.warpAffine(band, mmat, (w, h), flags=cv2.INTER_LINEAR)
            prof = np.quantile(sheared[:, cols], dg.Q_GROUND, axis=1)
        v = np.clip(ys + xs * t, 0, h - 1).astype(np.int32)
        g = prof[v]
        rel = (band[sub] - g[sub]) / np.maximum(g[sub], 1e-6)
        profs[t] = prof
        scores[t] = float(((rel > GROUND_RESID_LO) & (rel < GROUND_RESID_HI)).mean())
    best_t = 0.0
    for t, s in scores.items():
        if t != 0.0 and s > scores[0.0] + TILT_MARGIN and s > scores[best_t]:
            best_t = t
    v = np.clip(ys + xs * best_t, 0, h - 1).astype(np.int32)
    return profs[best_t][v]


def _row_q20_field(m: np.ndarray) -> np.ndarray:
    """现产码行为：逐行 q20（基线 A/B 的对照侧）。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    cols = np.r_[0:dg.EGO_COLS[0], dg.EGO_COLS[1]:band.shape[1]]
    prof = np.quantile(band[:, cols], dg.Q_GROUND, axis=1)
    return np.repeat(prof[:, None], band.shape[1], axis=1)


def _pct(xs, q):
    if not xs:
        return float("nan")
    return float(np.percentile(np.abs(xs), q))


def _block_rate(m, field, gate):
    """带内假块率：rel>门 的像素占比（侧倾破产的直接症状量）。"""
    band = m[dg.Y0:dg.DIAG_Y1].astype(np.float32)
    g = field(m)
    r = (band - g) / np.maximum(g, 1e-6)
    return float((r > gate).mean())


def _blocks_with_field(m, gl, gr, g):
    """产码 _rel_and_blocks 的场注入版（g=预计算 2D 基线场）——落产形态原型。
    除 g 的取法外逐行等价产码：over→HOLD→纵向桥→连通块→触边跨行→内沿归约。"""
    r = np.full(m.shape, np.nan, np.float32)
    r[dg.Y0:dg.DIAG_Y1] = (m[dg.Y0:dg.DIAG_Y1] - g) / np.maximum(g, 1e-6)
    gate_row = np.where(np.arange(m.shape[1], dtype=np.float32) < m.shape[1] // 2,
                        np.float32(gl), np.float32(gr))
    over = (r > gate_row).astype(np.float32)
    mask = (cv2.filter2D(over, -1, np.ones((1, dg.HOLD), np.float32))
            >= dg.HOLD).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((dg.V_HOLD, 1), np.uint8))
    ncc, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for c in range(1, ncc):
        x0, y0, w, h = (int(v) for v in stats[c, :4])
        touchL, touchR = x0 <= 2, x0 + w - 1 >= 1277
        if not (touchL or touchR) or h - 1 < dg.MIN_SPAN:
            continue
        sel = lab[y0:y0 + h, x0:x0 + w] == c
        grid = np.broadcast_to(np.arange(x0, x0 + w, dtype=np.float32), (h, w))
        lcol = cv2.reduce(np.where(sel & (grid < 640), grid, np.float32(-1.0)),
                          1, cv2.REDUCE_MAX).reshape(-1)
        rows = np.nonzero(lcol >= 0)[0]
        innerL = dict(zip((rows + y0).tolist(), lcol[rows].astype(np.int32).tolist()))
        rcol = cv2.reduce(np.where(sel & (grid >= 640), grid, np.float32(1e9)),
                          1, cv2.REDUCE_MIN).reshape(-1)
        rows = np.nonzero(rcol <= 1279)[0]
        innerR = dict(zip((rows + y0).tolist(), rcol[rows].astype(np.int32).tolist()))
        out.append((y0, y0 + h - 1, innerL, innerR, touchL, touchR))
    return out


def _select(blocks, m, side, gl, gr):
    """reading_from_map 逐侧选择环复刻（返回 f 以拿 y_ref 做同排金标对比）。"""
    tkey = 4 if side == "L" else 5
    gate = gl if side == "L" else gr
    for _, _, il, ir, _, _ in sorted((b for b in blocks if b[tkey]),
                                     key=lambda b: b[1] - b[0], reverse=True):
        inner = il if side == "L" else ir
        if not inner:
            continue
        f = dg._fit(inner, CAL)
        if f is None:
            break
        if not dg._pass(f, side):
            continue
        if not dg._baseline_ok(m, inner, side, f["y_ref"], gate):
            continue
        return f
    return None


def _gold_x_at(r, side, y):
    if r[side.lower() + "cls"] == "skip":
        return None
    nx, ny = float(r[side.lower() + "_nx"]), float(r[side.lower() + "_ny"])
    fx, fy = float(r[side.lower() + "_fx"]), float(r[side.lower() + "_fy"])
    if abs(ny - fy) < 1:
        return None
    return nx + (y - ny) * (fx - nx) / (fy - ny)


def _gold_grid(frames, fields, gls, grs, bad_lane=1.0):
    """(field × gate_l × gate_r) 金标卷：dev=读数 lane − 金标 lane（同 y_ref），
    |dev|>bad_lane 记坏；双侧=同帧两侧均出合格读数。"""
    print(f"\n{'field':>9} {'gl':>5} {'gr':>5} | {'L n':>4} {'dev':>6} {'p90':>5}"
          f" {'坏':>2} | {'R n':>4} {'dev':>6} {'p90':>5} {'坏':>2} | {'双':>3}")
    for ftag, field in fields:
        cache = [(r, m, field(m)) for r, m in frames]   # 场与门无关，每帧算一次
        for gl, gr in [(a, b) for a in gls for b in grs]:
            devs = {"L": [], "R": []}
            both = 0
            for r, m, g in cache:
                blocks = _blocks_with_field(m, gl, gr, g)
                got = {}
                for side in ("L", "R"):
                    f = _select(blocks, m, side, gl, gr)
                    if f is None:
                        continue
                    gx = _gold_x_at(r, side, f["y_ref"])
                    if gx is None:
                        continue
                    got[side] = f
                    devs[side].append(f["lane"] - dg.x_lane_of(
                        int(round(gx)), int(round(f["y_ref"])), CAL))
                both += len(got) == 2
            cells = [ftag, f"{gl:>5.2f}", f"{gr:>5.2f}"]
            for side in ("L", "R"):
                d = devs[side]
                cells += [f"{len(d):>4}",
                          f"{np.median(d):>+6.2f}" if d else "   n/a",
                          f"{np.percentile(np.abs(d), 90):>5.2f}" if d else "  n/a",
                          f"{sum(1 for v in d if abs(v) > bad_lane):>2}"]
            cells += [f"{both:>3}"]
            print(" ".join(cells))


def main() -> None:
    labels = list(csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8")))
    frames = []
    for r in labels:
        key = pcq.frame_key(Path(r["path"]))
        p = NPY_DIR / f"{key}__d336q4f16.npy"
        if p.exists():
            frames.append((r, np.load(p).astype(np.float32)))
    print(f"金标缓存 {len(frames)} 帧（gate={dg.GATE}）")
    for tag, field in (("B_rowq20_产码", _row_q20_field), ("A_tilt_原型", _tilt_field)):
        rates = [_block_rate(m, field, dg.GATE) for _, m in frames]
        hot = sum(v > 0.5 for v in rates)
        print(f"[{tag}] 假块率 p50 {np.percentile(rates, 50):.0%} "
              f"p90 {np.percentile(rates, 90):.0%}  >50% 热帧 {hot}")

    # ── 落产前置①：新基线门网格（旧门 0.08/0.10 是对 row-q20 残差分布标定的）──
    _gold_grid(frames, (("B_rowq20", _row_q20_field), ("A_tilt", _tilt_field)),
               (0.08, 0.12, 0.16, 0.20), (0.10, 0.14, 0.18, 0.22))

    # ── 落产前置②：518 骑缘锚点复查（原型 L −1.67 vs 金标 −0.394 待查）──
    # 该帧无 d336 缓存，用 da2s（518 短边）缓存直接评。
    r518 = next((r for r in labels if "000518" in Path(r["path"]).name), None)
    p518 = NPY_DIR / "frames__000518__da2s.npy"
    if r518 is not None and p518.exists():
        m = np.load(p518).astype(np.float32)
        print("\n[518 复查]")
        for tag, field in (("rowq20", _row_q20_field), ("tilt", _tilt_field)):
            g = field(m)
            blocks = _blocks_with_field(m, dg.GATE_L, dg.GATE_R, g)
            cells = [f"  {tag:7s}"]
            for side in ("L", "R"):
                f = _select(blocks, m, side, dg.GATE_L, dg.GATE_R)
                gx = _gold_x_at(r518, side, f["y_ref"]) if f else None
                glane = None if gx is None else dg.x_lane_of(
                    int(round(gx)), int(round(f["y_ref"])), CAL)
                cells.append(f"{side}={None if f is None else round(f['lane'], 3)}"
                             f"(金标{None if glane is None else round(glane, 3)})")
            print(" ".join(cells) + f"  stratum={r518['stratum']}"
                  f" lcls={r518['lcls']} rcls={r518['rcls']}")


if __name__ == "__main__":
    main()
