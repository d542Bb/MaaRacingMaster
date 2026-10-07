# -*- coding: utf-8 -*-
"""可行驶栅格探针：MoGe 单目点云 → BEV 三态栅格，离线调查「点云能否表达可行驶区域」。

**问题**（2026-10-04 调查）：speedrush 现役找边链（`depth_geo.reading_from_points`）
在近场视锥、超车挖洞、单侧/弯道场景下路缘经常不可测；但诊断显示**路面本身**（地面
高度一致性）在这些场景大多可见。本探针回答：把点云转成「可行驶栅格」是否成立——
即「看路是否只需要路面可见」。

**口径**（照抄现役实现，只 import 不改）：
- 证据包 → 全幅点云：`probe_depth_equiv_441.assemble()` 口径（336×598 fp16 点图 +
  valid + 归一化焦距 → 各通道 resize 到 1280×720 → invalid 置 nan；ego 掩码取资源
  文件，object 掩码 = *_mask.npy 与 ego 的差集）。
- 地面平面拟合：直接调 `depth_geo._fit_road_plane`（特征种子 + 走廊收敛，含 dig 挖除）。
- 高出路面 `hgt`：与 `reading_from_points` 同式（`sgn·(Y − (aX+bZ+c))`，`sgn` 按 z=5m
  处平面高度符号定翻转）。
- 金标帧：`depth_review/npy_moge/{000100,000340,000906}.npz`（全幅点云 + fx/fy），
  标签取 `depth_review/gold_labels.csv`（左右缘线像素坐标）。

**三态判据**（每格：检测带内、非挖洞、落窗内点的 hgt 中位）：
- 未知：有效点（非挖洞）数 < ``MIN_CELL_PTS``；
- 可走：中位 |hgt| ≤ ``TOL``；
- 不可走：中位 hgt > ``TOL``（显著高于地面）或 < −``TOL``（显著低于，少见）。

**输出**：`.workbuddy-ai/drivable_grid/` 下 stats.json + 每桶可视化 PNG；报告 md 见
同目录脚本旁的 `DRIVABLE_GRID_REPORT.md`。所有数字来自本脚本实际运行。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_drivable_grid.py
    ... --dirs depth_debug_20261004_140930 depth_debug_20261004_175855
    ... --no-figs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

FULL_W, FULL_H = 1280, 720
NAT_H, NAT_W = 336, 598
SPEEDRUSH = Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush"
TRACES = SPEEDRUSH / "control_traces"
GOLD_DIR = SPEEDRUSH / "depth_review" / "npy_moge"
GOLD_CSV = SPEEDRUSH / "depth_review" / "gold_labels.csv"
OUT = Path(".workbuddy-ai/drivable_grid")

# ── BEV 栅格 ────────────────────────────────────────────────────────────
CELL = 0.25
X_MIN, X_MAX = -8.0, 8.0
Z_MIN, Z_MAX = 3.0, 16.0
NX = int(round((X_MAX - X_MIN) / CELL))   # 64
NZ = int(round((Z_MAX - Z_MIN) / CELL))   # 52
TOL = 0.15               # 可走容差（米，= depth_geo.PLANE_TOL）
MIN_CELL_PTS = 3         # 单格判「已知」的最少有效点数
NEAR = (3.0, 9.0)        # 近场带
FAR = (9.0, 16.0)        # 远场带

UNKNOWN, DRIVABLE, NONDRIV = 0, 1, 2
COLORS = {DRIVABLE: (0, 190, 0), NONDRIV: (0, 0, 220), UNKNOWN: (128, 128, 128)}

# ── 场景分桶：代表帧（seq→fid 由调试图决策带人工读得，见报告「帧选取」节）──
# 决策带 = 现役 render_depth_debug 底栏，含 D[fid]/state/steer；本表是它的誊录。
SEQ2FID = {
    "depth_debug_20261004_175855": {
        1: None, 2: 2555, 3: 2572, 4: 2593, 5: 2616, 6: 2642, 7: 2666, 8: 2689,
        9: 2708, 10: 2732, 11: 2756, 12: 2778, 13: 2803, 14: 2828, 15: 2852,
        16: 2881, 17: 2907, 18: 2931, 19: 2956, 20: 2981, 21: 3005, 22: 3025,
        23: 3052, 24: 3079, 25: 3101, 26: 3125, 27: 3150, 28: 3179, 29: 3207,
        30: 3231, 31: 3255, 32: 3280, 33: 3306, 34: 3334, 35: 3360, 36: 3383,
        37: 3409, 38: 3436, 39: 3461, 40: 3482, 41: 3506, 42: 3533, 43: 3557,
        44: 3585, 45: 3606, 46: 3628, 47: 3654, 48: 3678, 49: 3699, 50: 3724,
        51: 3747, 52: 3773, 53: 3796, 54: 3818, 55: 3840, 56: 3868, 57: 3892,
        58: 3915, 59: 3939, 60: 3966, 61: 3992, 62: 4013, 63: 4040, 64: 4065},
    "depth_debug_20261004_140930": {
        1: None, 2: 5556, 3: 5583, 4: 5611, 5: 5635, 6: 5663, 7: 5688, 8: 5715,
        9: 5736, 10: 5754, 11: 5776, 12: 5804, 13: 5833, 14: 5861, 15: 5880,
        16: 5908, 17: 5933, 18: 5957, 19: 5982, 20: 6009, 21: 6035, 22: 6062,
        23: 6090, 24: 6119, 25: 6138, 26: 6167, 27: 6191, 28: 6217, 29: 6245,
        30: 6271, 31: 6299, 32: 6323, 33: 6342, 34: 6370, 35: 6394, 36: 6420,
        37: 6445, 38: 6471, 39: 6494, 40: 6521, 41: 6548, 42: 6575, 43: 6606,
        44: 6631, 45: 6659, 46: 6684, 47: 6712, 48: 6739, 49: 6764, 50: 6791,
        51: 6811, 52: 6838, 53: 6859, 54: 6883, 55: 6900, 56: 6917, 57: 6941,
        58: 6959, 59: 6986, 60: 7004},
    "depth_debug_20261002_211445": {
        1: None, 2: 2790, 3: 2874, 4: 2958, 5: 3039, 6: 3121, 7: 3199, 8: 3278,
        9: 3356, 10: 3442, 11: 3522, 12: 3604, 13: 3682, 14: 3767, 15: 3844,
        16: 3926, 17: 4008, 18: 4092, 19: 4173, 20: 4257, 21: 4334, 22: 4408},
}
# 场景分桶的判定口径（trace 列）：
#   弯道 = |steer_norm| > 0.35（与 README 语料分层格 3 同口径）
#   超车 = state == CHANGE 且 reason 以 moving:overtake 开头
#   雨天 = 会话（175812/175855，晚场雨） ；晴天宽路 = 140930 会话直行帧
RAIN_DIRS = {"depth_debug_20261004_175812", "depth_debug_20261004_175855"}
# 晴天会话（10-04 白天场，蓝空宽路）；10-02 晚场（211134/211402/211445）不计入天气桶
SUNNY_DIRS = {"depth_debug_20261004_140724", "depth_debug_20261004_140807",
              "depth_debug_20261004_140930", "depth_debug_20261004_141015",
              "depth_debug_20261004_141201", "depth_debug_20261004_141244"}

# 每桶代表帧（用于可视化；同桶多帧以覆盖不同子情形）
BUCKETS = {
    "晴天宽路": [("depth_debug_20261004_140930", 9),
                 ("depth_debug_20261004_140930", 11),
                 ("depth_debug_20261004_140930", 25),
                 ("depth_debug_20261004_140930", 54)],
    "雨天": [("depth_debug_20261004_175855", 5),
             ("depth_debug_20261004_175855", 20),
             ("depth_debug_20261004_175855", 33),
             ("depth_debug_20261004_175855", 45)],
    "弯道": [("depth_debug_20261002_211445", 6),
             ("depth_debug_20261002_211445", 11),
             ("depth_debug_20261002_211445", 17),
             ("depth_debug_20261002_211445", 18)],
    "超车": [("depth_debug_20261004_175855", 14),
             ("depth_debug_20261004_175855", 29),
             ("depth_debug_20261004_175855", 55),
             ("depth_debug_20261004_140930", 37),
             ("depth_debug_20261004_140930", 46)],
}
# 全量统计的会话（seq→fid 表已誊录的 3 个）
STAT_DIRS = list(SEQ2FID)


# ── 数据装载 ────────────────────────────────────────────────────────────
def load_ego() -> np.ndarray:
    p = Path(dg.__file__).parent / "resources" / "calibration" / "ego_mask.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    m = np.zeros((FULL_H, FULL_W), bool)
    m[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
    return m


def assemble(stem: Path):
    """证据包 → (全幅点图, fx, fy, ego, obj)。口径照抄 probe_depth_equiv_441.assemble。"""
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: NAT_H * NAT_W].reshape(NAT_H, NAT_W).astype(bool)
    pn = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pn[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts = pts.copy()
    pts[~vf] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H
    ego = load_ego()
    obj = None
    mp = Path(str(stem) + "_mask.npy")
    if mp.exists():
        merged = np.unpackbits(np.load(mp))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        obj = merged & ~ego
    return pts, fx, fy, ego, obj


def load_gold(name: str):
    """金标帧 npz（全幅点图 fp16 + 全幅 fx/fy）→ (pts, fx, fy, ego, None)。"""
    z = np.load(GOLD_DIR / f"{name}.npz")
    pts = z["pts"].astype(np.float32)
    pts[~np.isfinite(pts[..., 2])] = np.nan
    return pts, float(z["fx"]), float(z["fy"]), load_ego(), None


def load_gold_labels() -> dict[str, dict]:
    """gold_labels.csv → {stem: {L:(nx,ny,fx,fy,cls), R:(...), stratum}}。"""
    import csv
    out = {}
    for r in csv.DictReader(GOLD_CSV.open(encoding="utf-8")):
        stem = Path(r["path"]).stem
        if stem in ("000100", "000340", "000906"):
            out[stem] = {
                "stratum": r["stratum"],
                "L": (float(r["l_nx"]), float(r["l_ny"]), float(r["l_fx"]),
                      float(r["l_fy"]), r["lcls"]),
                "R": (float(r["r_nx"]), float(r["r_ny"]), float(r["r_fx"]),
                      float(r["r_fy"]), r["rcls"]),
            }
    return out


# ── BEV 三态栅格 ────────────────────────────────────────────────────────
def build_grid(pts: np.ndarray, fx: float, fy: float, ego, obj) -> dict | None:
    """点图 → 三态栅格 dict；平面拟合失败返回 None。"""
    dig = dg._dig_band(ego, FULL_W)
    if obj is not None:
        dig = dig | dg._dig_band(obj, FULL_W)
    Xb = pts[dg.Y0:dg.DIAG_Y1, :, 0]
    Yb = pts[dg.Y0:dg.DIAG_Y1, :, 1]
    Zb = pts[dg.Y0:dg.DIAG_Y1, :, 2]
    coef = dg._fit_road_plane(Xb, Yb, Zb, dig, fy=fy)
    if coef is None:
        return None
    a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Yb - (a * Xb + b * Zb + c))
    ok = np.isfinite(Xb) & np.isfinite(Yb) & np.isfinite(Zb) & np.isfinite(hgt)
    inb = ok & (Xb >= X_MIN) & (Xb < X_MAX) & (Zb >= Z_MIN) & (Zb < Z_MAX)

    def _flat(mask):
        ix = np.floor((Xb[mask] - X_MIN) / CELL).astype(np.int64)
        iz = np.floor((Zb[mask] - Z_MIN) / CELL).astype(np.int64)
        np.clip(ix, 0, NX - 1, out=ix)
        np.clip(iz, 0, NZ - 1, out=iz)
        return iz * NX + ix

    use = inb & (~dig)
    flat = _flat(use)
    hv = np.ascontiguousarray(hgt[use], dtype=np.float32)
    counts = np.bincount(flat, minlength=NX * NZ)
    med = np.full(NX * NZ, np.nan, np.float64)
    want = np.nonzero(counts > 0)[0]
    if want.size:
        med[want] = dg._binned_median(hv, flat, want, counts)

    duse = inb & dig
    dflat = _flat(duse)
    digcounts = np.bincount(dflat, minlength=NX * NZ)

    state = np.full(NX * NZ, UNKNOWN, np.int8)
    known = counts >= MIN_CELL_PTS
    state[known & (np.abs(med) <= TOL)] = DRIVABLE
    state[known & (np.abs(med) > TOL)] = NONDRIV

    return {"state": state.reshape(NZ, NX), "med": med.reshape(NZ, NX),
            "counts": counts.reshape(NZ, NX),
            "digcounts": digcounts.reshape(NZ, NX),
            "hgt": hgt, "coef": (a, b, c), "sgn": sgn, "fx": fx,
            "Xb": Xb, "Zb": Zb, "dig": dig}


def _band_rows(z0: float, z1: float) -> np.ndarray:
    iz = np.arange(NZ)
    zc = Z_MIN + (iz + 0.5) * CELL
    return (zc >= z0) & (zc < z1)


def _infov_mask(fx: float) -> np.ndarray:
    """逐格是否落在相机视锥内（|x| ≤ z·(W/2)/fx）；否则该格在画面外，无点属必然。"""
    xc = X_MIN + (np.arange(NX) + 0.5) * CELL
    zc = Z_MIN + (np.arange(NZ) + 0.5) * CELL
    return np.abs(xc)[None, :] <= (zc[:, None] * (FULL_W / 2.0) / fx)


def grid_counts(g: dict, rows: np.ndarray) -> dict:
    st = g["state"][rows]
    fov = _infov_mask(g["fx"])[rows]
    n = int(st.size)
    dr = int((st == DRIVABLE).sum())
    nd = int((st == NONDRIV).sum())
    un = int((st == UNKNOWN).sum())
    known = dr + nd
    return {"cells": n, "drivable": dr, "nondriv": nd, "unknown": un,
            "known": known, "infov_cells": int(fov.sum()),
            "unknown_infov": int(((st == UNKNOWN) & fov).sum()),
            "unknown_outfov": int(((st == UNKNOWN) & ~fov).sum()),
            "coverage_known": (dr / known if known else float("nan")),
            "drivable_frac": dr / n, "unknown_frac": un / n}


def dig_report(g: dict) -> dict:
    """挖洞检查：被挖掉全部证据（剩余有效点 < MIN_CELL_PTS）的格应为未知。"""
    dc = g["digcounts"]
    cnt = g["counts"]
    st = g["state"]
    dug = dc > 0
    dug_void = dug & (cnt < MIN_CELL_PTS)          # 挖后无证据
    viol = int((dug_void & (st == DRIVABLE)).sum())
    return {"dug_cells": int(dug.sum()),
            "dug_void_cells": int(dug_void.sum()),
            "dug_void_drivable": viol,
            "dug_cells_drivable": int((dug & (st == DRIVABLE)).sum()),
            "dug_cells_nondriv": int((dug & (st == NONDRIV)).sum()),
            "dug_cells_unknown": int((dug & (st == UNKNOWN)).sum())}


# ── 金标对照 ────────────────────────────────────────────────────────────
def _edge_metric_samples(g: dict, fx: float, line) -> tuple[np.ndarray, np.ndarray]:
    """金标缘线（像素 nx,ny→fx,fy）→ (z_m, x_m) 采样：沿线段取像素，查该像素 Z。"""
    nx, ny, fxx, fyy = line[:4]
    Zb = g["Zb"]
    hb = Zb.shape[0]
    ts = np.linspace(0.0, 1.0, 400)
    us = nx + ts * (fxx - nx)
    vs = ny + ts * (fyy - ny)
    col = np.clip(np.round(us).astype(int), 0, FULL_W - 1)
    row = np.clip(np.round(vs).astype(int) - dg.Y0, 0, hb - 1)
    Zv = Zb[row, col]
    good = np.isfinite(Zv) & (Zv > 0.1)
    if not good.any():
        return np.empty(0), np.empty(0)
    Zm = Zv[good]
    xm = (us[good] - FULL_W / 2.0) * Zm / fx
    return Zm, xm


def gold_compare(g: dict, fx: float, labels: dict, name: str) -> dict:
    """可走域 vs 真值车道：金标缘线→米制 (z,x)，与窗内可走域横向范围对比。

    金标缘线（wall/kerb）实测多在 ±10m 外（000100 L≈−10m、000340 L≈−11m），
    落在 ±8m 窗**之外**——该侧偏差不可比（记 nan 并给出金标位置）。"""
    rec: dict = {"name": name, "stratum": labels["stratum"]}
    edges = {}
    for side in ("L", "R"):
        z, x = _edge_metric_samples(g, fx, labels[side])
        edges[side] = (z, x)
        rec[f"gold_{side}_n"] = int(z.size)
        rec[f"gold_{side}_zspan"] = [round(float(z.min()), 1), round(float(z.max()), 1)] \
            if z.size else None
    rowz = Z_MIN + (np.arange(NZ) + 0.5) * CELL
    xc = X_MIN + (np.arange(NX) + 0.5) * CELL

    def _med(side, z0, z1):
        z, x = edges[side]
        m = (z >= z0) & (z < z1)
        return (float(np.median(x[m])) if m.any() else float("nan"), int(m.sum()))

    st = g["state"]
    per_band = {}
    for tag, (z0, z1) in (("near", NEAR), ("far", FAR)):
        gL, nL = _med("L", z0, z1)
        gR, nR = _med("R", z0, z1)
        rmask = (rowz >= z0) & (rowz < z1)
        dr_min, dr_max = [], []
        corridor = inter = driv = 0
        for iz in np.nonzero(rmask)[0]:
            dm = st[iz] == DRIVABLE
            driv += int(dm.sum())
            if dm.any():
                idx = np.nonzero(dm)[0]
                dr_min.append(xc[idx.min()])
                dr_max.append(xc[idx.max()])
            # 走廊 = 金标左右缘（带内中位）∩ 窗
            if np.isfinite(gL) and np.isfinite(gR):
                lo, hi = max(min(gL, gR), X_MIN), min(max(gL, gR), X_MAX)
                if hi > lo:
                    gm = (xc >= lo) & (xc <= hi)
                    corridor += int(gm.sum())
                    inter += int((gm & dm).sum())
        dL = float(np.median(dr_min)) if dr_min else float("nan")
        dR = float(np.median(dr_max)) if dr_max else float("nan")
        per_band[tag] = {
            "gold_L": gL, "gold_R": gR, "n_L": nL, "n_R": nR,
            "drivable_min_x": dL, "drivable_max_x": dR,
            "dev_left_m": (dL - gL) if (gL == gL and dL == dL) else float("nan"),
            "dev_right_m": (dR - gR) if (gR == gR and dR == dR) else float("nan"),
            "corridor_cells": corridor, "inter": inter, "drivable_cells": driv,
            "recall": (inter / corridor) if corridor else float("nan"),
            "rows": int(rmask.sum()),
        }
    rec["bands"] = per_band
    return rec


# ── 场景标注 ────────────────────────────────────────────────────────────
def load_traces() -> dict[str, dict[int, dict]]:
    out = {}
    for d in STAT_DIRS:
        ts = d[len("depth_debug_"):]
        f = next(TRACES.glob(f"trace_{ts}*.jsonl"), None)
        if f is None:
            continue
        out[d] = {r["fid"]: r for r in
                  (json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
                   if l.strip())}
    return out


def scenario_of(dirname: str, seq: int, traces) -> dict:
    fid = SEQ2FID.get(dirname, {}).get(seq)
    r = traces.get(dirname, {}).get(fid) if fid else None
    steer = abs(r["steer_norm"]) if (r and r["steer_norm"] is not None) else None
    state = r["state"] if r else None
    reason = r["reason"] if r else None
    overtake = bool(state == "CHANGE" and (reason or "").startswith("moving:overtake"))
    return {"fid": fid, "steer": steer, "state": state, "reason": reason,
            "overtake": overtake, "curve": bool(steer is not None and steer > 0.35),
            "rain": dirname in RAIN_DIRS, "sunny": dirname in SUNNY_DIRS}


def bucket_tags(sc: dict) -> list[str]:
    tags = []
    if sc["rain"]:
        tags.append("雨天")
    if sc["curve"]:
        tags.append("弯道")
    if sc["overtake"]:
        tags.append("超车")
    if sc["sunny"] and not sc["curve"] and not sc["overtake"] \
            and sc["state"] == "CRUISE":
        tags.append("晴天宽路")
    return tags


# ── 可视化 ──────────────────────────────────────────────────────────────
def _state_rgb(state2d: np.ndarray) -> np.ndarray:
    img = np.zeros(state2d.shape + (3,), np.uint8)
    for k, col in COLORS.items():
        img[state2d == k] = col
    return img


def render_frame(stem: Path, g: dict, fx: float, sc: dict, labels=None,
                 out_path: Path = None) -> np.ndarray:
    """上=原图叠三态像素着色，下=BEV 三态栅格 + hgt 图。"""
    st = g["state"]
    Xb, Zb = g["Xb"], g["Zb"]
    hb, wb = Zb.shape
    # 逐像素 → 格状态
    band = np.full((hb, wb), -1, np.int8)
    ok = np.isfinite(Xb) & np.isfinite(Zb) & (Xb >= X_MIN) & (Xb < X_MAX) \
        & (Zb >= Z_MIN) & (Zb < Z_MAX)
    ix = np.clip(np.floor((Xb[ok] - X_MIN) / CELL).astype(int), 0, NX - 1)
    iz = np.clip(np.floor((Zb[ok] - Z_MIN) / CELL).astype(int), 0, NZ - 1)
    band[ok] = st[iz, ix]
    tint = np.zeros((FULL_H, FULL_W, 3), np.uint8)
    for k, col in COLORS.items():
        tint[dg.Y0:dg.DIAG_Y1][band == k] = col

    frame_p = Path(str(stem) + "_frame.jpg")
    base = cv2.imread(str(frame_p)) if frame_p.exists() else None
    if base is None:
        base = np.full((360, 640, 3), 35, np.uint8)
    base = cv2.resize(base, (FULL_W, FULL_H))
    m = band >= 0
    full_m = np.zeros((FULL_H, FULL_W), bool)
    full_m[dg.Y0:dg.DIAG_Y1] = m
    out = base.copy()
    out[full_m] = (0.45 * base[full_m] + 0.55 * tint[full_m]).astype(np.uint8)

    # 金标缘线（若有）
    if labels is not None:
        for side, col in (("L", (0, 255, 255)), ("R", (255, 0, 255))):
            nx, ny, fxx, fyy = labels[side][:4]
            cv2.line(out, (int(nx), int(ny)), (int(fxx), int(fyy)), col, 2)
    cv2.putText(out, f"{stem.parent.name}/{stem.name}  fid={sc.get('fid')}  "
                     f"steer={'-' if sc.get('steer') is None else format(sc['steer'],'.2f')}  "
                     f"{sc.get('state')}  {sc.get('reason')}",
                (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    # BEV 三态（行 0=远 z16，行末=近 z3）
    bev = _state_rgb(st)[::-1]
    scale = 10
    bev = cv2.resize(bev, (NX * scale, NZ * scale), interpolation=cv2.INTER_NEAREST)
    bev = cv2.copyMakeBorder(bev, 26, 6, 40, 6, cv2.BORDER_CONSTANT, value=(25, 25, 25))
    cv2.putText(bev, "BEV  z16(远)↑", (44, 18), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (255, 255, 255), 1)
    cv2.putText(bev, "z3", (6, 26 + NZ * scale - 4), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (255, 255, 255), 1)
    r9 = int((Z_MAX - 9.0) / (Z_MAX - Z_MIN) * NZ * scale) + 26
    cv2.line(bev, (40, r9), (40 + NX * scale, r9), (0, 200, 255), 1)
    c0 = 40 + NX * scale // 2
    cv2.line(bev, (c0, 26), (c0, 26 + NZ * scale), (0, 200, 255), 1)
    cv2.putText(bev, "z=9m 近/远界", (44, r9 - 3), cv2.FONT_HERSHEY_SIMPLEX,
                0.42, (0, 200, 255), 1)
    BH = bev.shape[0]
    RW = FULL_W - bev.shape[1]

    # hgt 图 + 统计文字块（右列，与 BEV 同高）
    hm_h = 300
    hm = cv2.resize(dg._hgt_rgb(g["hgt"]), (RW, hm_h))
    txt = np.full((BH - hm_h, RW, 3), 25, np.uint8)
    near = grid_counts(g, _band_rows(*NEAR))
    far = grid_counts(g, _band_rows(*FAR))
    dg_rep = dig_report(g)
    lines = [
        f"近场3~9m  可走 {near['drivable']:4d}  不可走 {near['nondriv']:4d}"
        f"  未知 {near['unknown']:4d}",
        f"    可见即可走 {near['coverage_known']*100:5.1f}%"
        f"  可走占带 {near['drivable_frac']*100:5.1f}%"
        f"  未知 {near['unknown_frac']*100:5.1f}%",
        f"    未知里 视锥内 {near['unknown_infov']:4d}"
        f"  视锥外 {near['unknown_outfov']:4d}",
        f"远场9~16m 可走 {far['drivable']:4d}  不可走 {far['nondriv']:4d}"
        f"  未知 {far['unknown']:4d}",
        f"    可见即可走 {far['coverage_known']*100:5.1f}%"
        f"  可走占带 {far['drivable_frac']*100:5.1f}%"
        f"  未知 {far['unknown_frac']*100:5.1f}%",
        f"    未知里 视锥内 {far['unknown_infov']:4d}"
        f"  视锥外 {far['unknown_outfov']:4d}",
        f"挖洞格 {dg_rep['dug_cells']}  挖后无证据 {dg_rep['dug_void_cells']}"
        f"  误判可走 {dg_rep['dug_void_drivable']}",
        "绿=可走  红=不可走  灰=未知",
    ]
    for i, s in enumerate(lines):
        cv2.putText(txt, s, (6, 24 + i * 24), cv2.FONT_HERSHEY_SIMPLEX, 0.44,
                    (230, 230, 230), 1)
    right = np.vstack([hm, txt])
    if right.shape[0] != BH:
        right = cv2.resize(right, (RW, BH))
    bot = np.hstack([bev, right])
    if bot.shape[1] < FULL_W:
        bot = np.hstack([bot, np.zeros((bot.shape[0], FULL_W - bot.shape[1], 3),
                                       np.uint8)])
    bot = bot[:, :FULL_W]
    canvas = np.vstack([out, bot])
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), canvas)
    return canvas


# ── 主流程 ──────────────────────────────────────────────────────────────
def process_stem(stem: Path, traces, cal) -> dict | None:
    try:
        pts, fx, fy, ego, obj = assemble(stem)
    except Exception as e:  # noqa: BLE001
        print(f"  跳过 {stem.name}: {type(e).__name__}: {e}")
        return None
    g = build_grid(pts, fx, fy, ego, obj)
    seq = int(stem.name[1:])
    sc = scenario_of(stem.parent.name, seq, traces)
    if g is None:
        return {"dir": stem.parent.name, "seq": seq, "plane_fail": True, **sc}
    near = grid_counts(g, _band_rows(*NEAR))
    far = grid_counts(g, _band_rows(*FAR))
    rec = {"dir": stem.parent.name, "seq": seq, "plane_fail": False,
           "near": near, "far": far, "dig": dig_report(g),
           "cam_h": abs(g["coef"][2]), "tags": bucket_tags(sc), **sc}
    return rec


def agg(recs: list[dict]) -> dict:
    if not recs:
        return {}
    out = {}
    for tag in ("near", "far"):
        dr = sum(r[tag]["drivable"] for r in recs)
        nd = sum(r[tag]["nondriv"] for r in recs)
        un = sum(r[tag]["unknown"] for r in recs)
        uif = sum(r[tag]["unknown_infov"] for r in recs)
        uof = sum(r[tag]["unknown_outfov"] for r in recs)
        known = dr + nd
        out[tag] = {"drivable": dr, "nondriv": nd, "unknown": un,
                    "known": known, "unknown_infov": uif, "unknown_outfov": uof,
                    "coverage_known": dr / known if known else float("nan"),
                    "drivable_frac": dr / (dr + nd + un),
                    "unknown_frac": un / (dr + nd + un), "n": len(recs)}
    out["dig"] = {k: sum(r["dig"][k] for r in recs) for k in
                  ("dug_cells", "dug_void_cells", "dug_void_drivable")}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", nargs="*", default=None,
                    help="只跑这些 depth_debug_* 目录（默认 3 个已标注会话）")
    ap.add_argument("--no-figs", action="store_true")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    cal = load_calib()
    traces = load_traces()
    dirs = a.dirs or STAT_DIRS

    # ① 全量三态统计（已标注的 3 会话）
    recs = []
    for d in dirs:
        stems = sorted((TRACES / d).glob("d*_evid.npz"))
        print(f"[{d}] {len(stems)} 帧", flush=True)
        for ev in stems:
            stem = Path(str(ev)[: -len("_evid.npz")])
            r = process_stem(stem, traces, cal)
            if r is not None:
                recs.append(r)

    all_recs = [r for r in recs if not r.get("plane_fail")]
    print(f"\n有效帧 {len(all_recs)}/{len(recs)}（平面拟合失败 "
          f"{len(recs)-len(all_recs)}）")
    print(f"\n{'桶':<8} {'n':>4}  近场可见即可走%  近场未知%(视锥内)  远场可见即可走%  远场未知%(视锥内)")
    buckets = {}
    for tag in ("晴天宽路", "雨天", "弯道", "超车"):
        sub = [r for r in all_recs if tag in r["tags"]]
        ag = agg(sub)
        buckets[tag] = {"n": len(sub), **ag}
        if ag:
            ni, nu = ag['near']['unknown_infov'], ag['near']['unknown']
            fi, fu = ag['far']['unknown_infov'], ag['far']['unknown']
            print(f"{tag:<8} {len(sub):>4}  {ag['near']['coverage_known']*100:>12.1f}"
                  f"  {ag['near']['unknown_frac']*100:>8.1f}({100*ni/nu if nu else 0:.0f}%)"
                  f"  {ag['far']['coverage_known']*100:>14.1f}"
                  f"  {ag['far']['unknown_frac']*100:>8.1f}({100*fi/fu if fu else 0:.0f}%)")
    overall = agg(all_recs)
    print(f"\n全体 {len(all_recs)} 帧: 近场可见即可走 "
          f"{overall['near']['coverage_known']*100:.1f}%  未知 "
          f"{overall['near']['unknown_frac']*100:.1f}%（其中视锥内 "
          f"{overall['near']['unknown_infov']}）| 远场 "
          f"{overall['far']['coverage_known']*100:.1f}%  未知 "
          f"{overall['far']['unknown_frac']*100:.1f}%（其中视锥内 "
          f"{overall['far']['unknown_infov']}）")
    print(f"挖洞格 {overall['dig']['dug_cells']}  挖后无证据 "
          f"{overall['dig']['dug_void_cells']}  误判可走 "
          f"{overall['dig']['dug_void_drivable']}")

    # ③ 金标对照
    gold = {}
    labels = load_gold_labels()
    for name, lab in sorted(labels.items()):
        pts, fx, fy, ego, obj = load_gold(name)
        g = build_grid(pts, fx, fy, ego, obj)
        if g is None:
            print(f"金标 {name}: 平面拟合失败"); continue
        gc = gold_compare(g, fx, lab, name)
        gold[name] = gc
        print(f"金标 {name} ({lab['stratum']}): 近场 recall "
              f"{gc['bands']['near']['recall']*100:.1f}%  远场 recall "
              f"{gc['bands']['far']['recall']*100:.1f}%  近场左偏差 "
              f"{gc['bands']['near']['dev_left_m']:+.2f}m 右偏差 "
              f"{gc['bands']['near']['dev_right_m']:+.2f}m")

    # ② 每桶代表帧可视化
    slug = {"晴天宽路": "sunny_wide", "雨天": "rain", "弯道": "curve",
            "超车": "overtake", "金标": "gold"}
    figs = {}
    if not a.no_figs:
        for tag, frames in BUCKETS.items():
            paths = []
            for d, seq in frames:
                stem = TRACES / d / f"d{seq:05d}"
                if not Path(str(stem) + "_evid.npz").exists():
                    continue
                pts, fx, fy, ego, obj = assemble(stem)
                g = build_grid(pts, fx, fy, ego, obj)
                if g is None:
                    continue
                sc = scenario_of(d, seq, traces)
                p = OUT / f"{slug[tag]}_{d}_d{seq:05d}.png"
                render_frame(stem, g, fx, sc, None, p)
                paths.append(str(p))
            figs[tag] = paths
            print(f"图 {tag}: {len(paths)} 张")
        # 金标帧图（原图已不在树内：底为空白，叠金标缘线 + 三态 BEV）
        gpaths = []
        for name, lab in sorted(labels.items()):
            pts, fx, fy, ego, obj = load_gold(name)
            g = build_grid(pts, fx, fy, ego, obj)
            if g is None:
                continue
            sc = {"fid": name, "steer": None, "state": lab["stratum"],
                  "reason": "gold"}
            p = OUT / f"gold_{name}.png"
            render_frame(Path(f"gold_{name}"), g, fx, sc, lab, p)
            gpaths.append(str(p))
        figs["金标"] = gpaths
        print(f"图 金标: {len(gpaths)} 张")

    (OUT / "stats.json").write_text(json.dumps(
        {"overall": overall, "buckets": buckets, "gold": gold,
         "frames": recs, "figs": figs}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"\n→ {OUT / 'stats.json'}")


if __name__ == "__main__":
    main()
