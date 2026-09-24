# -*- coding: utf-8 -*-
"""读数带重定探针：源行/评估行拆分后，外推读数的覆盖率与精度（第 1 步，离线先行）。

问题（实机复核）：生产把"边在哪几行可见"（源行）和"读数取在哪个 y"（评估行）
绑死在固定带 [550,700]——但金标边界线 R 51/54、L 28/54 帧在 y<550 就出画，
带内物理无边可读。根治假设：车道尺 x_lane 沿 3D 直线不变 ⇒ 拟合线在**任意**
源行上求值都换算成同一个车道量，只要角精度够；发散区（y−y_h 太小）用下限拦。

本探针在 122 帧考卷的金标 54 帧上、用生产块提取（depth_geo._rel_and_blocks）
量三件事（逐侧、逐参数格点 FLOOR×N）：
1. 物理上限：金标线画面内行数 ≥ N 的帧占比（新读法可达覆盖率的天花板）；
2. 管线覆盖：块拟合存在且过守卫（conv/resid/侧别，本底窗随源行走）的帧占比；
3. 精度：dev_lane = 拟合线在参考行的车道量 − 金标线同参考行的车道量，
   与旧规则（reading_from_map 的 edge_lane，仅在其非空的帧上）对照。

刹车判据（README 计划）：外推 dev 不可接受 → 深度层降级为辅助信号，几何主人
让回黄线。用法：python tools/experiments/speedrush_vision/probe_readband.py
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402

NPY = pd.OUT / "npy"
CAL = load_calib()
EGO = dg.DepthRoadObserver._load_ego_mask()
FLOORS = (30, 50, 80)     # 源行发散区下限：y − y_h ≥ FLOOR（px）
NS = (20, 40)             # 源行数门槛（拟合最少行数沿用 FIT_MIN_ROWS=20）


def _folded_sess(short: int):
    import onnxruntime as ort
    probe = dg.preprocess(np.zeros((720, 1280, 3), np.uint8), short)
    so = ort.SessionOptions()
    for n, v in zip(("batch_size", "height", "width"),
                    (probe.shape[0], probe.shape[2], probe.shape[3])):
        so.add_free_dimension_override_by_name(n, int(v))
    return ort.InferenceSession(str(DEPTH_MODEL_FILE), sess_options=so,
                                providers=["DmlExecutionProvider"])


def map_for(key: str, path: Path, variant: str, sess) -> np.ndarray | None:
    """变体缓存读取；@518 缺则现场推理补缓存（一次性 91ms/帧）。"""
    suffix = "d336q4f16" if variant == "336" else "d518q4f16"
    cache = NPY / f"{key}__{suffix}.npy"
    if cache.exists():
        return np.load(cache).astype(np.float32)
    if variant == "336":
        return None
    frame = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
    m = dg.infer_map(sess, frame, 518)
    np.save(cache, m.astype(np.float16))
    return m


def gold_x_at(r, side, y):
    if r[side + "cls"] == "skip":
        return None
    nx, ny = float(r[side + "_nx"]), float(r[side + "_ny"])
    fx, fy = float(r[side + "_fx"]), float(r[side + "_fy"])
    if abs(ny - fy) < 1:
        return None
    return nx + (y - ny) * (fx - nx) / (fy - ny)


def fit2pass(rows: dict[int, int]):
    """生产 _fit 的两轮稳健拟合复刻（返回 sl,ic, resid, n_used；<FIT_MIN_ROWS 不评）。"""
    ys = np.array(sorted(rows), float)
    xs = np.array([rows[int(y)] for y in ys], float)
    if len(ys) < dg.FIT_MIN_ROWS:
        return None
    sl, ic = np.polyfit(ys, xs, 1)
    keep = np.abs(xs - (sl * ys + ic)) <= dg.RESID_MAX_PX
    if keep.sum() >= dg.FIT_MIN_ROWS:
        sl, ic = np.polyfit(ys[keep], xs[keep], 1)
        ys, xs = ys[keep], xs[keep]
    resid = float(np.median(np.abs(xs - (sl * ys + ic))))
    return sl, ic, resid, len(ys)


def baseline_ok_at(rmap, inner, side, y_ref, gate):
    """本底守卫随源行迁移：窗仍量在内沿内侧 40~120px，但行集取参考行邻域
    （旧实现固定扫 NEAR_LO..NEAR_HI——新读法下带内无行，守卫会被整体跳过，
    这里把它钉回"有边的地方"）。"""
    vals = []
    for y in range(int(y_ref) - 60, int(y_ref) + 60, 5):
        x0 = inner.get(y)
        if x0 is None:
            continue
        lo, hi = ((x0 + dg.BASE_LO_PX, x0 + dg.BASE_HI_PX) if side == "L"
                  else (x0 - dg.BASE_HI_PX, x0 - dg.BASE_LO_PX))
        lo, hi = max(lo, 0), min(hi, rmap.shape[1])
        if hi - lo < 8:
            continue
        v = rmap[y, lo:hi]
        v = v[np.isfinite(v)]
        if len(v):
            vals.append(float(np.median(v)))
    if len(vals) < 3:
        return True
    return abs(float(np.median(vals))) <= gate / 2.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=("336", "518"), default="336")
    ap.add_argument("--gate", type=float, default=None,
                    help="缺省：@336 用生产门 0.05，@518 用参照门 0.02")
    args = ap.parse_args()
    gate = args.gate if args.gate is not None else (dg.GATE if args.variant == "336" else 0.02)
    sess = _folded_sess(518) if args.variant == "518" else None
    labels = [r for r in csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))]
    frames = []
    for r in labels:
        p = Path(r["path"])
        m = map_for(pcq.frame_key(p), p, args.variant, sess)
        if m is not None:
            frames.append((r, m))
    print(f"金标帧 × @{args.variant}q4f16 缓存：{len(frames)}/{len(labels)}（gate={gate}）")

    # 旧规则基线（生产 reading_from_map）
    old = {("L",): None}
    old_dev = {"L": [], "R": []}
    old_cov = Counter()
    per_frame = []
    for r, m in frames:
        rd = dg.reading_from_map(m, CAL, EGO, gate=gate)
        blocks_cache = dg._rel_and_blocks(m, EGO, gate=gate)
        per_frame.append((r, m, rd, blocks_cache))
        for side, lane in (("L", rd.left_edge_lane), ("R", rd.right_edge_lane)):
            if lane is None:
                continue
            old_cov[side] += 1
            gx = gold_x_at(r, side.lower(), 625)
            if gx is not None and 0 <= gx <= 1279:
                old_dev[side].append(lane - x_lane_of(int(round(gx)), 625, CAL))

    def pct(xs, q):
        xs = sorted(xs)
        return 0.0 if not xs else xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]

    print("\n旧规则（固定带 550~700）：覆盖 L {} / R {}（/{}）；"
          "dev_lane p50/p90 L {:+.2f}/{:.2f} R {:+.2f}/{:.2f}".format(
              old_cov["L"], old_cov["R"], len(frames),
              pct(old_dev["L"], .5), pct([abs(x) for x in old_dev["L"]], .9),
              pct(old_dev["R"], .5), pct([abs(x) for x in old_dev["R"]], .9)))

    print("\n新规则格点（FLOOR=源行发散区下限 / N=画面内源行数门槛）：")
    hdr = f"{'侧':<3}{'FLOOR':>6}{'N':>4}{'物理上限':>10}{'管线覆盖':>10}" \
          f"{'dev p50':>9}{'dev p90':>9}{'坏>0.5车道':>10}"
    print(hdr)
    for side, tkey in (("L", 4), ("R", 5)):
        for floor in FLOORS:
            for n in NS:
                phys = 0
                devs = []
                bad = 0
                for r, m, _rd, (rmap, blocks) in per_frame:
                    s = side.lower()
                    # 物理上限：金标线在检测带内、画面内、且离开发散区的行数
                    vis = [y for y in range(dg.Y0, dg.DIAG_Y1)
                           if y - CAL.y_h >= floor
                           and (gx := gold_x_at(r, s, y)) is not None and 0 <= gx <= 1279]
                    if len(vis) >= n:
                        phys += 1
                    cands = sorted((b for b in blocks if b[tkey]),
                                   key=lambda b: b[1] - b[0], reverse=True)
                    for _y0, _y1, il, ir, _tl, _tr in cands:
                        inner = il if side == "L" else ir
                        src = [y for y in inner if y - CAL.y_h >= floor]
                        if len(src) < n:
                            continue
                        sub = {y: inner[y] for y in src}
                        f = fit2pass(sub)
                        if f is None:
                            continue
                        sl, ic, resid, _nu = f
                        conv = abs(sl * CAL.y_h + ic - CAL.vpx)
                        if conv > dg.CONV_MAX_PX or resid > dg.RESID_MAX_PX:
                            continue
                        y_ref = float(np.median(src))
                        lane = x_lane_of(int(round(sl * y_ref + ic)), int(round(y_ref)), CAL)
                        if abs(lane) < dg.LANE_SIDE_MIN or (side == "L") != (lane < 0):
                            continue
                        if not baseline_ok_at(rmap, inner, side, y_ref, gate):
                            continue
                        gx = gold_x_at(r, s, y_ref)
                        if gx is None:
                            break
                        dev = lane - x_lane_of(int(round(gx)), int(round(y_ref)), CAL)
                        devs.append(dev)
                        bad += abs(dev) > 0.5
                        break
                nn = len(devs)
                print(f"{side:<4}{floor:>5}{n:>5}{phys/len(frames):>9.0%}{nn/len(frames):>10.0%}"
                      f"{pct(devs,.5):>+9.2f}{pct([abs(d) for d in devs],.9):>9.2f}"
                      f"{(bad/nn if nn else 0):>9.0%}")
    print("\n（dev=拟合−金标 的车道量差；±0.3 车道≈半个车身，>0.5 车道算坏；"
          "管线覆盖 < 物理上限 的差额 = 块/拟合/守卫丢的）")

    # ── 扫 1：conv 收紧能否砍尾（FLOOR=50, N=20）──────────────────────
    def run_grid(gate, conv_max, floor=50, n=20):
        out = {}
        for side, tkey in (("L", 4), ("R", 5)):
            devs = []
            for r, m, _rd, _bc in per_frame:
                rmap, blocks = dg._rel_and_blocks(m, EGO, gate=gate)
                for _y0, _y1, il, ir, _tl, _tr in sorted(
                        (b for b in blocks if b[tkey]),
                        key=lambda b: b[1] - b[0], reverse=True):
                    inner = il if side == "L" else ir
                    src = [y for y in inner if y - CAL.y_h >= floor]
                    if len(src) < n:
                        continue
                    f = fit2pass({y: inner[y] for y in src})
                    if f is None:
                        continue
                    sl, ic, resid, _ = f
                    if abs(sl * CAL.y_h + ic - CAL.vpx) > conv_max or resid > dg.RESID_MAX_PX:
                        continue
                    y_ref = float(np.median(src))
                    lane = x_lane_of(int(round(sl * y_ref + ic)), int(round(y_ref)), CAL)
                    if abs(lane) < dg.LANE_SIDE_MIN or (side == "L") != (lane < 0):
                        continue
                    if not baseline_ok_at(rmap, inner, side, y_ref, gate):
                        continue
                    gx = gold_x_at(r, side.lower(), y_ref)
                    if gx is None:
                        break
                    devs.append(lane - x_lane_of(int(round(gx)), int(round(y_ref)), CAL))
                    break
            out[side] = devs
        return out

    print("\n扫1 conv 收紧（gate=0.05, FLOOR=50, N=20）：")
    for cm in (350, 150, 80, 50):
        g = run_grid(gate, cm)
        for side in ("L", "R"):
            d = g[side]
            bad = sum(1 for x in d if abs(x) > 0.5) / len(d) if d else 0
            print(f"  conv≤{cm:>3} {side}: 覆盖 {len(d)/len(frames):>4.0%}"
                  f" dev p50 {pct(d,.5):+.2f} p90 {pct([abs(x) for x in d],.9):.2f} 坏 {bad:.0%}")

    print("\n扫2 R 侧门重标（conv=350, FLOOR=50, N=20；R 弱台阶 +4.4% 贴门）：")
    for gt in (0.05, 0.035, 0.025):
        g = run_grid(gt, dg.CONV_MAX_PX)
        d = g["R"]
        bad = sum(1 for x in d if abs(x) > 0.5) / len(d) if d else 0
        print(f"  gate={gt} R: 覆盖 {len(d)/len(frames):>4.0%}"
              f" dev p50 {pct(d,.5):+.2f} p90 {pct([abs(x) for x in d],.9):.2f} 坏 {bad:.0%}")


if __name__ == "__main__":
    main()
