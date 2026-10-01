# -*- coding: utf-8 -*-
"""玩家车动态监测：时间分割 → 高度量分离 → 车身轴 → 车身相对路面方向。

链路（2026-10-01，每环节带可视化验证，只输出可信结果）：
  1. 时间稳定性分割：自车与追车相机共刚体，其点云在相机系下跨帧几乎不动，
     世界点飞速流动——std(Z)<0.25m 分出玩家车（无 YOLO/训练，换车免疫）；
  2. 高度量分离：稳定簇里贴地的倒影/阴影（hgt≈0）与车体（hgt>0.25m）按
     「高出路面」分开（与找边共用同一 hgt 量）；
  3. 车身轴：跨帧累积 PCA（同像素各帧点同属车体，簇填满噪声互平均），
     长轴 = 最大延展轴（只假设长轴是最大延展轴，不用任何车型尺寸）；
  4. 路面方向：左右缘样本（找边几何）各自拟合 x=p·z+q，两缘斜率一致才可信，
     路面方向角 = atan(平均斜率)；
  5. 车身相对路面偏航 = 车身轴偏航 − 路面方向角。可信闸门：
     缘样本每侧≥2 且 |p_L−p_R|≤0.15；车簇点数≥8000 且 长轴/次轴≥1.2。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_ego_pose.py \
        --seq 20260916_215701_p2 --start 61 --n 60      # 漂移段
    python tools/experiments/speedrush_vision/probe_ego_pose.py \
        --seq 20260921_083222_p1 --start 300 --n 50     # 直行基线
输出：stdout 表 + <depth_review>/ego_pose_<seq>_<start>.png
（逐帧验证板：原图叠加[车身轴/缘线] + BEV 放大[车簇/轴线/缘样本/路面方向] + 迹线）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402
from tools.experiments.speedrush_vision import probe_boundary3d as pb  # noqa: E402

DEMO_ROOT = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/demos")
OUT = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/depth_review")
RECT = (335, 555, 470, 810)   # 搜索先验区 y0,y1,x0,x1（只限搜索范围，不挖点）
WIN = 2                       # 稳定性半窗（帧）；全窗 = 2*WIN+1 ≈ 0.25s
HSEP = 0.25                   # 车体/贴地分离阈：高出路面(m)
ZLO, ZHI = 2.0, 9.0           # 追车距离带（先验区内的粗限，非标尺）
MIN_PX = 8000                 # 车簇点数可信闸
MIN_ANISO = 1.2               # 长轴/次轴各向异性可信闸
EDGE_PAR = 0.15               # 左右缘斜率一致性闸


def load_ego3d() -> np.ndarray:
    """3D 用紧掩码（仅车身矩形）——列带下延是 2D 逐行扫描语义，3D 会挖掉可见路面。"""
    d = json.loads((Path(dg.__file__).parent / "resources" / "calibration"
                    / "ego_mask.json").read_text(encoding="utf-8"))
    m = np.zeros((720, 1280), bool)
    m[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
    return m


def plane_fit_excl_rect(X, Y, Z):
    """路面平面拟合，排除先验矩形（矩形语义只用于拟合排除，不挖点云）。"""
    y0, y1, x0, x1 = RECT
    excl = np.zeros((720, 1280), bool)
    excl[y0:y1, x0:x1] = True
    band = (slice(dg.Y0, dg.DIAG_Y1), slice(None))
    return eph.fit_road_plane(X[band], Y[band], Z[band], excl[band])


def road_dir(samples: dict):
    """左右缘样本 → 中心线斜率。两缘斜率须一致（平行），否则判不可信。
    返回 (方向角deg, {side:(p,q)}) 或 None。"""
    fits = {}
    for side in (1, -1):
        smp = sorted(s for s in samples.get(side, []) if 4.0 <= s[0] <= 14.0)
        if len(smp) < 2:
            return None
        z = np.array([s[0] for s in smp])
        x = np.array([s[1] for s in smp])
        fits[side] = np.polyfit(z, x, 1)
    if abs(fits[1][0] - fits[-1][0]) > EDGE_PAR:
        return None
    return float(np.degrees(np.arctan(0.5 * (fits[1][0] + fits[-1][0])))), fits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--n", type=int, required=True)
    args = ap.parse_args()

    fdir, pads, frames = load_seq_files(args.seq)
    p_ts = np.array([p["ts_ns"] for p in pads], np.float64)
    p_lx = np.array([p["lx"] / 32768.0 for p in pads], np.float64)
    f_ts = np.array([f["ts_ns"] for f in frames], np.float64)
    dt_ms = float(np.median(np.diff(f_ts)) / 1e6)
    ego3d = load_ego3d()
    y0, y1, x0, x1 = RECT
    sess = dg.load_session(DEPTH_MODEL_FILE)

    # 1) 逐帧：点云 → 平面（排除矩形）→ hgt → 找边样本；矩形裁剪留作分割
    lo, hi = max(0, args.start - WIN), min(len(frames), args.start + args.n + WIN)
    cache = OUT / f"ego_pose_cache_{args.seq}_{lo}_{hi}.npz"
    ny, nx = y1 - y0, x1 - x0
    if cache.exists():   # 缓存加速迭代；换段/换序列自动失效
        z = np.load(cache)
        crops, meta = [], {}
        for k, i in enumerate(range(lo, hi)):
            if not bool(z["valid"][k]):
                crops.append(None)
                continue
            sgn = float(z["sgns"][k])
            crops.append((z["Xs"][k], z["Ys"][k], z["Zs"][k], sgn, z["Hs"][k]))
            if np.isfinite(z["coefs"][k]).all():
                raw_s = json.loads(z["samples_json"].item()).get(str(i), {})
                meta[i] = {"coef": tuple(z["coefs"][k]),
                           "samples": {int(kk): vv for kk, vv in raw_s.items()},
                           "fx": float(z["fxs"][k]), "fy": float(z["fys"][k])}
        print(f"缓存命中: {cache.name}")
    else:
        crops, meta = [], {}
        Xs = np.full((hi - lo, ny, nx), np.nan, np.float32)
        Ys, Zs, Hs = Xs.copy(), Xs.copy(), Xs.copy()
        sgns = np.zeros(hi - lo, np.float32)
        coefs = np.full((hi - lo, 3), np.nan, np.float64)
        fxs = np.zeros(hi - lo, np.float64)
        fys = np.zeros(hi - lo, np.float64)
        samples_json = {}
        for i in range(lo, hi):
            img = cv2.cvtColor(cv2.imread(str(fdir / frames[i]["file"])), cv2.COLOR_BGR2RGB)
            X, Y, Z, fx_f, fy_f = pb.cloud_of(img, sess)
            pb._IMG_XZ = 0.5 / fx_f   # 画面横边界比随逐帧焦距（视锥线用）
            coef = plane_fit_excl_rect(X, Y, Z)
            if coef is None:
                crops.append(None)
                continue
            a, b, c = coef
            sgn = -1.0 if (b * 5.0 + c) > 0 else 1.0
            hgt = sgn * (Y - (a * X + b * Z + c))
            sky = np.isfinite(hgt) & (hgt > pb.SKY_HGT)
            samples = {}
            for z0, z1 in pb.ZBIN:
                m = (Z >= z0) & (Z < z1) & np.isfinite(X) & np.isfinite(hgt) \
                    & (~ego3d) & (~sky)
                if m.sum() < 300:
                    continue
                zc = 0.5 * (z0 + z1)
                for side in (1, -1):
                    ex, _wx, _px, _ph = pb.scan_side(X[m], hgt[m], side, zc)
                    if np.isfinite(ex):
                        samples.setdefault(side, []).append((zc, float(ex)))
            crops.append((X[y0:y1, x0:x1].copy(), Y[y0:y1, x0:x1].copy(),
                          Z[y0:y1, x0:x1].copy(), sgn, hgt[y0:y1, x0:x1].copy()))
            meta[i] = {"coef": coef, "samples": samples, "img": img,
                       "fx": fx_f, "fy": fy_f}
            k = i - lo
            Xs[k], Ys[k], Zs[k], Hs[k] = crops[-1][0], crops[-1][1], crops[-1][2], crops[-1][4]
            sgns[k], coefs[k], fxs[k], fys[k] = sgn, coef, fx_f, fy_f
            samples_json[str(i)] = samples
            print(f"\r点云 {k + 1}/{hi - lo}", end="", flush=True)
        print()
        np.savez(cache, Xs=Xs, Ys=Ys, Zs=Zs, Hs=Hs, sgns=sgns, coefs=coefs,
                 fxs=fxs, fys=fys,
                 valid=np.array([c is not None for c in crops]),
                 samples_json=np.array(json.dumps(samples_json)))

    # 2) 逐分析帧：分割 → 分离 → 跨帧累积 PCA → 可信闸门 → 相对偏航
    rows = []
    for i in range(args.start, args.start + args.n):
        if any(crops[j - lo] is None for j in range(i - WIN, i + WIN + 1)) \
                or i not in meta:
            continue
        Zw = np.stack([crops[j - lo][2] for j in range(i - WIN, i + WIN + 1)])
        fin = np.isfinite(Zw)
        med_z = np.nanmedian(Zw, 0)
        stable = (np.nanstd(Zw, 0) < 0.25) & (fin.sum(0) >= 2 * WIN) \
            & (med_z > ZLO) & (med_z < ZHI)
        if stable.sum() < 500:
            continue
        n_lab, lab, stats, _ = cv2.connectedComponentsWithStats(stable.astype(np.uint8), 8)
        car0 = lab == (1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA])))

        Xc, Yc, Zc, sgn, hgtc = crops[i - lo]
        a, b, c = meta[i]["coef"]
        finc = np.isfinite(Xc) & np.isfinite(Yc) & np.isfinite(Zc)
        car = car0 & (hgtc > HSEP) & finc

        # 跨帧累积：同像素各帧点同属车体（车在相机系不动）
        Ps = []
        for j in range(i - WIN, i + WIN + 1):
            Xk, Yk, Zk, _, hgtk = crops[j - lo]
            ak, bk, ck = meta[j]["coef"]
            sgk = -1.0 if (bk * 5.0 + ck) > 0 else 1.0
            hk = sgk * (Yk - (ak * Xk + bk * Zk + ck))
            mk = car0 & (hk > HSEP) & np.isfinite(Xk) & np.isfinite(Yk) & np.isfinite(Zk)
            Ps.append(np.stack([Xk[mk], Yk[mk], Zk[mk]], 1))
        P = np.concatenate(Ps).astype(np.float64)
        if len(P) < MIN_PX:
            continue
        ctr = P.mean(0)
        Pc = P - ctr
        _, _, vt = np.linalg.svd(Pc, full_matrices=False)
        exts = (Pc @ vt.T).max(0) - (Pc @ vt.T).min(0)
        jx = int(np.argmax(exts))
        axis = vt[jx].copy()
        if axis[2] < 0:
            axis = -axis                      # 车头朝远处（+Z）
        yaw = float(np.degrees(np.arctan2(axis[0], axis[2])))

        rd = road_dir(meta[i]["samples"])
        trust_edge = rd is not None
        trust_body = exts[jx] / max(exts[(jx + 1) % 3], 1e-6) >= MIN_ANISO \
            and int(car.sum()) >= MIN_PX
        rel = float("nan")
        if trust_edge and trust_body:
            rel = yaw - rd[0]
        rows.append({"i": i, "ts": f_ts[i], "steer": float(np.interp(f_ts[i], p_ts, p_lx)),
                     "yaw": yaw, "road": rd[0] if rd else float("nan"), "rel": rel,
                     "trust": bool(trust_edge and trust_body), "ext": exts,
                     "axis": axis, "ctr": ctr, "car": car, "coef": (a, b, c),
                     "fx": meta[i]["fx"], "fy": meta[i]["fy"],
                     "samples": meta[i]["samples"], "Xc": Xc, "Zc": Zc, "hgtc": hgtc})
        tg = "OK " if (trust_edge and trust_body) else "不可信"
        rs = f"{rel:+6.1f}" if np.isfinite(rel) else "   —  "
        print(f"帧{i:4d} steer={rows[-1]['steer']:+.2f} yaw={yaw:+6.1f}° "
              f"路向={rows[-1]['road']:+6.1f}° 相对={rs} {tg}")

    if len(rows) < 8:
        print("有效帧不足，退出")
        return

    # 3) 相对偏航与手柄转向的对照（漂移里 steer 有反打，只作段类型参照）
    relv = np.array([r["rel"] for r in rows])
    ok = np.isfinite(relv)
    print(f"\n可信帧 {int(ok.sum())}/{len(rows)}  "
          f"相对偏航 |中位|={np.median(np.abs(relv[ok])) if ok.any() else float('nan'):.1f}°")
    stv = np.array([r["steer"] for r in rows])
    best = (0, 0.0)
    ii = np.where(ok)[0]
    for k in range(-12, 13):
        jj = ii + k
        m = (jj >= 0) & (jj < len(rows))
        if m.sum() < 6:
            continue
        c_ = np.corrcoef(relv[ii[m]], stv[jj[m]])[0, 1]
        if np.isfinite(c_) and abs(c_) > abs(best[1]):
            best = (k, float(c_))
    print(f"相对偏航 vs steer 最佳互相关: 滞后 {best[0] * dt_ms:+.0f}ms r={best[1]:+.2f}"
          f"（漂移含反打，仅参照）")

    # 4) 逐帧验证板：按 |相对偏航| 分位取 6 帧，每帧 = 原图叠加 + BEV 放大
    tr = [r for r in rows if r["trust"]]
    pick = []
    if len(tr) >= 2:
        order = sorted(tr, key=lambda r: abs(r["rel"]))
        for q in np.linspace(0, len(order) - 1, min(6, len(order))):
            pick.append(order[int(round(q))])
    sheets = []
    for r in pick:
        sheets.append(render_sheet(r, fdir, frames, dt_ms))
        print(f"验证板: 帧{r['i']} 相对={r['rel']:+.1f}°")
    if not sheets:
        print("无可信帧，无法出验证板")
        return

    # 4.5) 连拍条：每 3 帧一格跟随连续运动（灰字=不可信帧）
    strip = draw_filmstrip(rows, fdir, frames, every=3)

    # 5) 迹线
    trace = draw_trace(rows, dt_ms)
    canvas = np.vstack([*sheets, strip, trace])
    out = OUT / f"ego_pose_{args.seq}_{args.start}.png"
    ok2, buf = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    out.write_bytes(buf.tobytes())
    print(f"图: {out}")


def load_seq_files(name: str):
    d = DEMO_ROOT / name
    pads = [json.loads(l) for l in (d / "pads.jsonl").open(encoding="utf-8")]
    frames = [json.loads(l) for l in (d / "frames.jsonl").open(encoding="utf-8")]
    return d / "frames", pads, frames


def render_sheet(r: dict, fdir: Path, frames: list, dt_ms: float) -> np.ndarray:
    """单帧验证板：左=原图（车身轴/缘线/分割轮廓），右=BEV 放大
    （车簇红/稳定簇灰、轴线箭头、缘样本点、路面方向箭头）。"""
    W, H = 960, 340
    sheet = np.zeros((H, W, 3), np.uint8)
    img = cv2.cvtColor(cv2.imread(str(fdir / next(
        f["file"] for f in frames if f["ts_ns"] == r["ts"]))), cv2.COLOR_BGR2RGB)
    im = cv2.resize(img, (480, 270))
    SC = 480.0 / 1280.0            # 原图 1280×720 → 缩略 480×270，叠加坐标必须同缩放

    def PIM(x, z):
        u, v = pb.project_point(float(x), float(z), r["coef"], r["fx"], r["fy"])
        return (int(u * SC), int(v * SC))

    a, b, c = r["coef"]
    # 车身轴只画前向半段（车尾端朝相机，近距投影会甩出画外误导判读）
    ctr, ax = r["ctr"], r["axis"]
    p0 = PIM(ctr[0], ctr[2])
    p1 = PIM(ctr[0] + 2.4 * ax[0], ctr[2] + 2.4 * ax[2])
    cv2.line(im, p0, p1, (0, 0, 0), 7)
    cv2.line(im, p0, p1, (120, 200, 255), 4)
    cv2.circle(im, p0, 4, (255, 255, 255), -1)
    # 分割轮廓（红）
    caru8 = r["car"].astype(np.uint8) * 255
    cnts, _ = cv2.findContours(caru8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    y0r, _y1r, x0r, _x1r = RECT
    for cn in cnts:
        cn = (cn.astype(np.float64) + np.array([x0r, y0r])) * SC
        cv2.polylines(im, [cn.astype(np.int32)], True, (230, 50, 50), 2)
    for side in (1, -1):
        smp = sorted(r["samples"].get(side, []))
        pl = [PIM(x, z) for z, x in smp]
        for k in range(len(pl) - 1):
            cv2.line(im, pl[k], pl[k + 1], (60, 220, 60), 2)
    sheet[30:300, 0:480] = im
    rel_s = f"rel={r['rel']:+.1f}" if np.isfinite(r["rel"]) else "rel=untrusted"
    cv2.putText(sheet, f"frame {r['i']}  steer={r['steer']:+.2f}  {rel_s}",
                (6, 22), 0, 0.42, (200, 200, 200), 1)

    # BEV 放大：x ±8m, z 2~12m, 30px/m
    BW, BH, PPM = 470, 300, 30
    ox, oy = 480, 0

    def BU(x, z):
        return (ox + int((x + 8.0) * PPM), BH - int((z - 2.0) * PPM))

    Xc, Zc, hgtc = r["Xc"], r["Zc"], r["hgtc"]
    finc = np.isfinite(Xc) & np.isfinite(Zc) & np.isfinite(hgtc)
    body = r["car"] & finc
    y0r, y1r, x0r, x1r = RECT
    for m_, col in ((finc & (~r["car"]), (110, 110, 110)), (body, (230, 50, 50))):
        vv, uu = np.where(m_)
        for v_, u_ in zip(vv[::3], uu[::3]):
            px, py = BU(float(Xc[v_, u_]), float(Zc[v_, u_]))
            if ox <= px < ox + BW and 0 <= py < BH:
                sheet[py, px] = col
    ctr, ax = r["ctr"], r["axis"]
    q0, q1 = BU(ctr[0], ctr[2]), BU(ctr[0] + 2.6 * ax[0], ctr[2] + 2.6 * ax[2])
    q2 = BU(ctr[0] - 1.4 * ax[0], ctr[2] - 1.4 * ax[2])
    cv2.arrowedLine(sheet, q2, q1, (120, 200, 255), 2, tipLength=0.12)
    cv2.circle(sheet, q0, 3, (255, 255, 255), -1)
    for side in (1, -1):
        for z, x in r["samples"].get(side, []):
            cv2.circle(sheet, BU(x, z), 3, (60, 220, 60), -1)
    rd = r["road"]
    if np.isfinite(rd):
        d0 = BU(0.0, 4.0)
        d1 = BU(float(np.tan(np.radians(rd))) * 2.0, 6.0)
        cv2.arrowedLine(sheet, d0, d1, (60, 220, 60), 2, tipLength=0.12)
    for gx in range(-5, 6, 5):
        cv2.line(sheet, BU(gx, 2), BU(gx, 12), (55, 55, 55), 1)
        cv2.putText(sheet, f"{gx}m", (BU(gx, 2)[0] - 10, BH - 6), 0, 0.35, (150, 150, 150), 1)
    for gz in (4, 8, 12):
        cv2.line(sheet, BU(-8, gz), BU(8, gz), (55, 55, 55), 1)
        cv2.putText(sheet, f"{gz}m", (ox + 4, BU(0, gz)[1] - 3), 0, 0.35, (150, 150, 150), 1)
    rel = f"{r['rel']:+.1f}" if np.isfinite(r["rel"]) else "不可信"
    cv2.putText(sheet, f"BEV zoom  body-yaw={r['yaw']:+.1f} road={r['road']:+.1f} "
                       f"rel={rel}", (ox + 4, 14), 0, 0.42, (200, 200, 200), 1)
    cv2.putText(sheet, "red=car-body  gray=stable-other  blue arrow=body axis  "
                       "green=road edges+dir", (ox + 4, 30), 0, 0.36, (160, 160, 160), 1)
    return sheet


def draw_filmstrip(rows: list, fdir: Path, frames: list, every: int = 3) -> np.ndarray:
    """连拍条：每 every 帧一格（分割轮廓+前向轴箭头），跟随连续运动；
    标题灰字=该帧未过可信闸门。"""
    sel = rows[::every]
    TW, TH, COLS = 240, 135, 4
    nrows = (len(sel) + COLS - 1) // COLS
    strip = np.zeros((nrows * (TH + 16), COLS * TW, 3), np.uint8)
    y0r, _y1r, x0r, _x1r = RECT
    sc = TW / 1280.0
    for k, r in enumerate(sel):
        img = cv2.cvtColor(cv2.imread(str(fdir / next(
            f["file"] for f in frames if f["ts_ns"] == r["ts"]))), cv2.COLOR_BGR2RGB)
        im = cv2.resize(img, (TW, TH))

        def PIM(x, z, coef=r["coef"], fx=r["fx"], fy=r["fy"]):
            u, v = pb.project_point(float(x), float(z), coef, fx, fy)
            return (int(u * sc), int(v * sc))

        ctr, ax = r["ctr"], r["axis"]
        p0 = PIM(ctr[0], ctr[2])
        p1 = PIM(ctr[0] + 2.4 * ax[0], ctr[2] + 2.4 * ax[2])
        cv2.line(im, p0, p1, (0, 0, 0), 4)
        cv2.line(im, p0, p1, (120, 200, 255), 2)
        caru8 = r["car"].astype(np.uint8) * 255
        cnts, _ = cv2.findContours(caru8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cn in cnts:
            cn = (cn.astype(np.float64) + np.array([x0r, y0r])) * sc
            cv2.polylines(im, [cn.astype(np.int32)], True, (230, 50, 50), 1)
        gr, gc = (k // COLS) * (TH + 16), (k % COLS) * TW
        strip[gr + 16:gr + 16 + TH, gc:gc + TW] = im
        rel_s = f"{r['rel']:+.0f}" if np.isfinite(r["rel"]) else "-"
        col = (200, 200, 200) if r["trust"] else (120, 120, 120)
        cv2.putText(strip, f"{r['i']} rel={rel_s}", (gc + 4, gr + 12), 0, 0.4, col, 1)
    return strip


def draw_trace(rows: list, dt_ms: float) -> np.ndarray:
    W, H = 960, 300
    panel = np.zeros((H, W, 3), np.uint8)
    idx = np.arange(len(rows))
    stv = np.array([r["steer"] for r in rows])
    rdv = np.array([r["road"] if np.isfinite(r["road"]) else np.nan for r in rows])
    rv = np.array([r["rel"] if np.isfinite(r["rel"]) else np.nan for r in rows])

    def PX(k):
        return 50 + int(k / max(1, idx[-1]) * (W - 70))

    def PY(v):
        return int(H / 2 - v * H * 0.4)

    cv2.line(panel, (50, H // 2), (W - 20, H // 2), (80, 80, 80), 1)
    pts = [(PX(k), PY(v)) for k, v in zip(idx, stv)]
    for p_, q_ in zip(pts, pts[1:]):
        cv2.line(panel, p_, q_, (255, 160, 60), 2)
    ok = np.isfinite(rdv)
    pts = [(PX(k), PY(v)) for k, v in zip(idx[ok], rdv[ok])]
    for p_, q_ in zip(pts, pts[1:]):
        cv2.line(panel, p_, q_, (60, 220, 60), 1)
    ok = np.isfinite(rv)
    pts = [(PX(k), PY(v)) for k, v in zip(idx[ok], rv[ok])]
    for p_, q_ in zip(pts, pts[1:]):
        cv2.line(panel, p_, q_, (120, 200, 255), 3)
    un = np.where(~np.isfinite(rv))[0]
    for k in un:
        cv2.circle(panel, (PX(k), H // 2), 3, (90, 90, 90), 1)
    cv2.putText(panel, "orange=steer(lx)  green=road-dir  blue=body-vs-road yaw "
                "(thick=trusted)  gray circles=untrusted",
                (10, 20), 0, 0.42, (200, 200, 200), 1)
    return panel


if __name__ == "__main__":
    main()
