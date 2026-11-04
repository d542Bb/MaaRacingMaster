"""地面静态对应点的人工审计探针（论文方法开工前的信息量闸门）。

**要回答的唯一问题**：下一拍的图像里究竟有多少**可被人确认是同一处场景点**的静态地面
对应信息。没有 → 观测条件存在信息瓶颈；有而算法找不到 → 该换估计器。两种解释指向相反
的下一步，而任何自制置信度都分不开它们（`calcOpticalFlowPyrLK` 的 status=1 只表示
「算法认为找到了」，不表示对应成立）。

**基线口径（本轮纠正）**：对应检查用 **1 拍 / 2 拍**（≈0.05 / 0.10 s），独立真值窗口
仍 ≥0.3 s——两者不是一回事，混用会把「点还在不在视野里」当成「算法找不找得到」。
41 m/s 下 0.3 秒车已前进 12.3 m，近处点全部被碾过，长基线审计自变量不干净。
所有时间用实际采集时间戳，不把帧数机械当成 50/100 ms。

**车身排除（本轮新增）**：追尾镜头里自车占住画面下方，且 MoGe 给车身表面的深度恰好
落在地面平面容差内——只用平面残差选点会把车身当路面。这里按**画面实测轮廓**分档标定：
车身 = 下方区域里亮且低饱和、且与底边连成一片的最大连通块；分档变量取该连通块的
**质心横向位置**（观测值，不是转向指令——车会滑移，指令不代表画面姿态）。每档取该档
所有标定帧轮廓的**并集**再膨胀，宁可多挡不误漏；轮廓匹不上档的帧，其候选带标为不可用。
同时记录**被车身掩膜杀掉的候选点数**，防止「掩膜误杀」被读成「图像缺信息」。

**选点纪律**：候选点由检测器在地面域里自动选，按深度分层后横向均匀取；**不得**用
LK 是否成功、模型预测是否漂亮、人工是否好认来筛——那等于只留下好跟的点。凑不满 20 个
就报数量不足；选中但第二帧不可见的点保留并标原因，不换点。

模式：
    calib    车身轮廓分档标定 + 叠加核验图
    scan     逐帧指标（地面纹理占比 / 俯仰 / 侧倾 / 机位高）→ 选帧依据可审计
    audit    对指定帧跑审计，出拼版 + 逐点数据

用法：
    python tools/experiments/speedrush_geom/probe_ground_corr_audit.py 20261022_172231_p2 calib --every 10
    python tools/experiments/speedrush_geom/probe_ground_corr_audit.py 20261022_172231_p2 audit \
        --frames 670,250,640 --gaps 1,2 --out-dir <临时目录>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "experiments" / "speedrush_geom"))

import probe_repeat_and_flow as R  # noqa: E402  共用裁判与低频标定
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    EGO_ABS_X, EGO_ABS_Z, PLANE_TOL, infer_points, load_session, reading_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

CAM_W, CAM_H = 1280, 720
BODY_TOP = 380         # 车身检测只看第 380 行以下（车尾翼实测从 ~400 行进入画面）
LUM_MIN = 150          # 车身是浅色：亮度下限
SAT_MAX = 70           # 低饱和（排除被夕阳/护栏染色的路面）
BODY_MIN_PX = 40000    # 连通块小于此 → 轮廓匹不上
BODY_DILATE = 18       # 并集后再膨胀（像素），宁可多挡
CROP = 26              # 每格半边长（全幅像素）
SCALE = 3
V_TRUTH = 41.0         # 裁判区间平均速度，仅用于算「模型该落在哪」
BANDS_M = ((3.0, 8.0), (8.0, 15.0), (15.0, 25.0))
PER_BAND = 7
TEXTURE_MIN = 30.0
LK_WIN = (61, 61)      # 搜索域要盖得住预期位移：6 m 处一帧就走 94 px
LK_LEVELS = 5
FONT = next((ImageFont.truetype(p, 14) for p in
             ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf") if Path(p).exists()),
            ImageFont.load_default())


# ------------------------------------------------------------------ 车身轮廓分档

def _body_blob(rgb):
    """画面实测车身连通块：下方区域里「亮 + 低饱和 + 与底边相连」的最大块。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    m = ((hsv[:, :, 2] > LUM_MIN) & (hsv[:, :, 1] < SAT_MAX)).astype(np.uint8) * 255
    m[:BODY_TOP, :] = 0
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    best, best_area = None, 0
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if y + h < CAM_H - 5 or area < best_area:    # 必须贴住底边（车身连着画面下沿）
            continue
        if area <= best_area:
            continue
        best, best_area = k, area
    if best is None or best_area < BODY_MIN_PX:
        return None, None
    blob = (lab == best).astype(np.uint8) * 255
    return blob, float(cent[best][0])


def _bin_of(cx: float, edges) -> int:
    return int(np.clip(np.searchsorted(edges, cx), 0, len(edges)))


def calibrate(name: str, every: int, out_dir: Path) -> dict:
    """扫一遍素材：每帧量车身质心 → 按质心三分位定档 → 每档取轮廓并集再膨胀。"""
    sess = load_session(DEPTH_MODEL_FILE)
    rows = R._rows(name)
    seen, blobs = [], []
    for i in range(0, min(len(rows), 950), every):
        rgb = R._frame(name, i)
        if rgb is None:
            continue
        blob, cx = _body_blob(rgb)
        if blob is None:
            continue
        seen.append((i, cx))
        blobs.append((i, cx, blob))
    if len(blobs) < 6:
        raise SystemExit(f"车身连通块只匹上 {len(blobs)} 帧，分档标定不成立")
    xs = sorted(c for _, c, _ in blobs)
    edges = [xs[len(xs) // 3], xs[2 * len(xs) // 3]]
    union = {b: np.zeros((CAM_H, CAM_W), np.uint8) for b in range(3)}
    for i, cx, blob in blobs:
        union[_bin_of(cx, edges)] |= blob
    masks = {b: cv2.dilate(m, np.ones((BODY_DILATE * 2 + 1,) * 2, np.uint8))
             for b, m in union.items()}
    cnt = {b: int((m > 0).sum()) for b, m in masks.items()}
    print(f"\n===== 车身轮廓分档 {name}  匹上 {len(blobs)}/{len(seen) + len(blobs)} 帧"
          f"  质心分档边界 x={edges[0]:.0f}/{edges[1]:.0f}")
    print(f"  各档并集面积（膨胀后）：左 {cnt[0]} 中 {cnt[1]} 右 {cnt[2]} px"
          f"  占画面 {max(cnt.values()) / (CAM_W * CAM_H):.0%}")
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / f"body_bins_{name}.npz",
                        edges=np.array(edges), **{f"b{b}": masks[b] for b in masks})
    _overlay_check(name, blobs, edges, masks, out_dir)
    return {"edges": edges, "masks": masks}


def _overlay_check(name, blobs, edges, masks, out_dir) -> None:
    """把各档并集叠在真帧上出核验图——掩膜挡了多少路面必须看得见，不能靠说。"""
    picks = [blobs[k] for k in np.linspace(0, len(blobs) - 1, 12).round().astype(int)]
    sheet = Image.new("RGB", (CAM_W // 2 * 4 + 20, CAM_H // 2 * 3 + 30), (18, 18, 22))
    d = ImageDraw.Draw(sheet)
    for n, (i, cx, _blob) in enumerate(picks):
        rgb = R._frame(name, i)
        ov = rgb.copy()
        b = _bin_of(cx, edges)
        m = masks[b] > 0
        ov[m] = (0.55 * ov[m] + 0.45 * np.array([255, 40, 40])).astype(np.uint8)
        im = Image.fromarray(ov).resize((CAM_W // 2, CAM_H // 2))
        x, y = (n % 4) * (CAM_W // 2 + 5), (n // 4) * (CAM_H // 2 + 10)
        sheet.paste(im, (x, y))
        d.text((x + 3, y + CAM_H // 2 - 14), f"i{i} 质心x={cx:.0f} → 档{b}", font=FONT,
               fill=(255, 230, 120))
    dest = out_dir / f"body_overlay_{name}.png"
    sheet.save(dest)
    print(f"  轮廓叠加核验图：{dest}")


def load_bins(path: Path) -> dict:
    z = np.load(path)
    return {"edges": z["edges"], "masks": {b: z[f"b{b}"] for b in range(3)}}


# ------------------------------------------------------------------ 地面域与候选点

def _ground(rgb, sess, cal):
    pts, fx, fy = infer_points(sess, rgb, native=True)
    ds = CAM_W / pts.shape[1]
    rd = reading_from_points(pts.copy(), fx, cal, fy=fy, ds=ds)
    if rd.coef is None:
        return None
    a, b, c = rd.coef
    X, Y, Z = pts[:, :, 0], pts[:, :, 1], pts[:, :, 2]
    m = (np.isfinite(Z) & (np.abs(Y - (a * X + b * Z + c)) <= PLANE_TOL)
         & (Z >= BANDS_M[0][0]) & (Z <= BANDS_M[-1][1]))
    m &= ~((np.abs(X) < EGO_ABS_X) & (Z < EGO_ABS_Z))
    full = cv2.resize(m.astype(np.uint8) * 255, (CAM_W, CAM_H), interpolation=cv2.INTER_NEAREST)
    return {"mask": full, "pts": pts, "ds": ds, "coef": rd.coef, "h": abs(c)}


def _candidates(g, mask):
    p = cv2.goodFeaturesToTrack(g, maxCorners=800, qualityLevel=0.002, minDistance=6, mask=mask)
    return [] if p is None else [(float(x), float(y)) for x, y in p.reshape(-1, 2)]


def _stratify(cands, zmap, ds):
    bands = {i: [] for i in range(len(BANDS_M))}
    for x, y in cands:
        iy, ix = int(y / ds), int(x / ds)
        if not (0 <= iy < zmap.shape[0] and 0 <= ix < zmap.shape[1]):
            continue
        z = zmap[iy, ix]
        if not np.isfinite(z):
            continue
        for bi, (lo, hi) in enumerate(BANDS_M):
            if lo <= z < hi:
                bands[bi].append((x, y, z))
                break
    out = []
    for _bi, lst in bands.items():
        lst.sort(key=lambda t: t[0])
        idx = sorted(set(np.linspace(0, len(lst) - 1, min(PER_BAND, len(lst))).round().astype(int).tolist()))
        out += [lst[i] for i in idx]
    return out


def _pred_pt(x, y, v_h, fH, V, dt):
    """地面点在精确透视映射下的落点（纯前进）；被碾过/奇点附近返回 None。"""
    k = V * dt / fH
    d = y - v_h
    if d <= 0 or 1 - k * d <= 0.05:
        return None, None
    d2 = d / (1 - k * d)
    return 640.0 + (x - 640.0) * d2 / d, v_h + d2


# ------------------------------------------------------------------ 审计

def _tile(rgb, x, y, marks):
    x0, y0 = int(round(x)) - CROP, int(round(y)) - CROP
    xa, ya = max(0, x0), max(0, y0)
    patch = rgb[ya:ya + 2 * CROP, xa:xa + 2 * CROP]
    im = Image.fromarray(patch).convert("RGB").resize((CROP * 2 * SCALE, CROP * 2 * SCALE), Image.NEAREST)
    d = ImageDraw.Draw(im)
    for mx, my, col in marks:
        px, py = (mx - xa) * SCALE, (my - ya) * SCALE
        d.line([px - 7, py, px + 7, py], fill=col, width=2)
        d.line([px, py - 7, px, py + 7], fill=col, width=2)
    return im


def audit(name: str, frames: list[int], gaps: list[int], out_dir: Path, bins: dict) -> None:
    sess = load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    rows = R._rows(name)
    fH = 1041.0
    recs = []
    for i in frames:
        rgb = R._frame(name, i)
        g = R._gray(rgb)
        gd = _ground(rgb, sess, cal)
        if gd is None:
            print(f"  i{i}: 平面无解，跳过")
            continue
        blob, cx = _body_blob(g, rgb)
        b = _bin_of(cx, bins["edges"]) if blob is not None else None
        body = bins["masks"][b] if b is not None else np.full((CAM_H, CAM_W), 255, np.uint8)
        all_c = _candidates(g, gd["mask"])
        picked = _stratify(all_c, gd["pts"][:, :, 2], gd["ds"])
        killed = [p for p in picked if body[int(p[1]), int(p[0])] > 0]
        kept = [p for p in picked if body[int(p[1]), int(p[0])] == 0]
        print(f"  i{i}: 地面候选 {len(all_c)} → 分层 {len(picked)} → 车身掩膜杀掉 {len(killed)}"
              f" → 送审 {len(kept)} 点（档 {b if b is not None else '匹不上'}，质心 x={cx if cx else -1:.0f}）"
              f" 机位高 {gd['h']:.2f}m 俯仰 {np.degrees(np.arctan(gd['coef'][1])):+.2f}°")
        if len(kept) < 20:
            print(f"    ⚠ 数量不足 20，按实际 {len(kept)} 点送审（不人工补齐）")
        grad = R._grad(g)
        tgts = {}
        for gp in gaps:
            dt = (rows[i - 1 + gp]["ts_ns"] - rows[i - 1]["ts_ns"]) / 1e9
            rgb2 = R._frame(name, i - 1 + gp)
            p0 = np.array([[x, y] for x, y, _ in kept], np.float32).reshape(-1, 1, 2)
            p1, st, er = cv2.calcOpticalFlowPyrLK(g, R._gray(rgb2), p0, None,
                                                  winSize=LK_WIN, maxLevel=LK_LEVELS)
            tgts[gp] = (dt, rgb2, p1.reshape(-1, 2), st.ravel(), er.ravel())
            for n, (x, y, z) in enumerate(kept):
                px, py = _pred_pt(x, y, cal.y_h, fH, V_TRUTH, dt)
                recs.append({"i": i, "gap": gp, "n": n, "x": x, "y": y, "z": z,
                             "grad": float(grad[int(y), int(x)]), "dt": dt,
                             "travel_m": V_TRUTH * dt,
                             "lk_x": float(p1[n][0][0]), "lk_y": float(p1[n][0][1]),
                             "lk_status": int(st[n][0]), "lk_err": float(er[n][0]),
                             "pred_x": px, "pred_y": py,
                             "lk_off": bool(not (0 <= p1[n][0][0] < CAM_W and 0 <= p1[n][0][1] < CAM_H))})
        _sheets(name, i, gaps, rgb, grad, kept, tgts, cal.y_h, fH, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"audit_{name}.json").write_text(
        json.dumps(recs, ensure_ascii=False, default=float), encoding="utf-8")
    print(f"  逐点数据：{out_dir / f'audit_{name}.json'}（{len(recs)} 条）")


def _sheets(name, i, gaps, rgb, grad, kept, tgts, v_h, fH, out_dir) -> None:
    """每点一行：基准帧 | 各基线目标帧（红=实际落点 绿=模型预测）。10 点一版防降采样。"""
    tw = CROP * 2 * SCALE
    lab = 300
    for chunk in range(0, len(kept), 10):
        part = kept[chunk:chunk + 10]
        sheet = Image.new("RGB", (lab + tw * (1 + len(gaps)), (tw + 30) * len(part)), (20, 20, 24))
        d = ImageDraw.Draw(sheet)
        d.text((lab + 4, 2), "  ".join(f"第{g}格=+{g}拍" for g in gaps) + "   红=LK落点 绿=模型预测落点",
               font=FONT, fill=(160, 220, 255))
        for n, (x, y, z) in enumerate(part):
            row = 16 + n * (tw + 30)
            txt = [f"#{chunk + n} z={z:.1f}m 像素({x:.0f},{y:.0f}) 梯度{grad[int(y), int(x)]:.0f}"]
            for j, gp in enumerate(gaps):
                dt, rgb2, p1, st, er = tgts[gp]
                lx, ly = p1[chunk + n]
                px, py = _pred_pt(x, y, v_h, fH, V_TRUTH, dt)
                txt.append(f" +{gp}拍 dt={dt * 1000:.0f}ms LK({'中' if st[chunk + n] else '失'})"
                           f"Δ({lx - x:+.0f},{ly - y:+.0f})e{er[chunk + n]:.0f}"
                           f" 预Δ({(px or 0) - x:+.0f},{(py or 0) - y:+.0f})")
                sheet.paste(_tile(rgb2, x, y, [(lx, ly, (255, 60, 60)),
                                               ((px or -99), (py or -99), (60, 255, 60))]),
                            (lab + tw * (j + 1), row))
            d.text((4, row + 2), "\n".join(txt), font=FONT, fill=(255, 230, 120))
            sheet.paste(_tile(rgb, x, y, [(x, y, (255, 255, 0))]), (lab, row))
        dest = out_dir / f"audit_{name}_i{i}_c{chunk}.png"
        out_dir.mkdir(parents=True, exist_ok=True)
        sheet.save(dest)
        print(f"    拼版 {dest}")


def scan(name: str, every: int, limit: int) -> None:
    sess = load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    rows = R._rows(name)
    print(f"\n===== 选帧扫描 {name}  每 {every} 帧")
    print(f"  {'帧':>5} {'纹理占比':>7} {'梯度p50':>7} {'转向':>6} {'俯仰°':>6} {'侧倾°':>6} {'机位m':>6}")
    for i in range(0, min(len(rows), limit), every):
        rgb = R._frame(name, i)
        if rgb is None:
            continue
        gd = _ground(rgb, sess, cal)
        if gd is None:
            print(f"  {i:5d} 平面无解")
            continue
        gr = R._grad(R._gray(rgb))
        sel = gd["mask"] > 0
        print(f"  {i:5d} {float((gr[sel] >= TEXTURE_MIN).mean()):>7.1%} "
              f"{float(np.percentile(gr[sel], 50)):>7.1f} {rows[i].get('steer_norm') or 0.0:>6.2f} "
              f"{np.degrees(np.arctan(gd['coef'][1])):>6.2f} {np.degrees(np.arctan(gd['coef'][0])):>6.2f} "
              f"{gd['h']:>6.2f}")


def flow(name: str, lo: int, hi: int, step: int, truth: float) -> None:
    """免人工的聚合判据：逐点位移反推前进速度，与独立裁判对表。

    行映射用的是 **fy·机位高**（行只由 fy 决定，之前拿 fx 换算是错的）：
        v − v_h = fy·c / Z，  v' = v_h + fy·c/(Z − V·dt)
    近处一帧就走 100~230 像素，任何窗口都跟不住，所以按深度分带报，
    让「哪一段距离上图像还能给出可信运动」自己浮出来，而不是靠我先猜一个带。
    """
    sess = load_session(DEPTH_MODEL_FILE)
    cal = load_calib()
    rows = R._rows(name)
    acc: dict = {}
    for i in range(lo, min(hi, len(rows) - 2), step):
        rgb = R._frame(name, i)
        g = R._gray(rgb)
        pts, fx, fy = infer_points(sess, rgb, native=True)
        ds = CAM_W / pts.shape[1]
        rd = reading_from_points(pts.copy(), fx, cal, fy=fy, ds=ds)
        if rd.coef is None:
            continue
        a, b, c = rd.coef
        v_h = CAM_H / 2 + fy * b
        fyc = fy * abs(c)
        X, Y, Z = pts[:, :, 0], pts[:, :, 1], pts[:, :, 2]
        m = (np.isfinite(Z) & (np.abs(Y - (a * X + b * Z + c)) <= PLANE_TOL) & (Z > 2.0))
        mask = cv2.resize(m.astype(np.uint8) * 255, (CAM_W, CAM_H), interpolation=cv2.INTER_NEAREST)
        p = cv2.goodFeaturesToTrack(g, maxCorners=400, qualityLevel=0.002, minDistance=8, mask=mask)
        if p is None or len(p) < 10:
            continue
        p0 = p.reshape(-1, 2)
        for gap in (1, 2):
            dt = (rows[i + gap]["ts_ns"] - rows[i]["ts_ns"]) / 1e9
            if dt <= 0:
                continue
            g2 = R._gray(R._frame(name, i + gap))
            p1, st, _ = cv2.calcOpticalFlowPyrLK(g, g2, p0.reshape(-1, 1, 2).astype(np.float32),
                                                 None, winSize=LK_WIN, maxLevel=LK_LEVELS)
            q = p1.reshape(-1, 2)
            d = p0[:, 1] - v_h
            dd = q[:, 1] - v_h
            ok = (st.ravel() == 1) & (d > 12) & (dd > d) & (np.abs(q[:, 0] - p0[:, 0]) < 400)
            for bi, (zlo, zhi) in enumerate(((6, 10), (10, 16), (16, 26), (26, 45))):
                sel = ok & (fyc / np.maximum(d, 1) >= zlo) & (fyc / np.maximum(d, 1) < zhi)
                if sel.sum() < 3:
                    continue
                V = fyc * (dd[sel] - d[sel]) / (d[sel] * dd[sel]) / dt
                acc.setdefault((gap, zlo, zhi), []).append((V, fyc / d[sel], dt))
            break
    print(f"\n===== 逐点位移反推速度 {name}  裁判 {truth:.1f} m/s  地平线由当拍平面给出")
    print(f"  {'基线':>6} {'深度带':>9} {'点数中位':>8} {'V 中位':>7} {'V p25-p75':>13} {'相对误差':>8} {'散度':>6}")
    for (gap, zlo, zhi), lst in sorted(acc.items()):
        med = [float(np.median(v)) for v, _z, _dt in lst]
        n = [len(v) for v, _z, _dt in lst]
        iqr = [float(np.percentile(v, 75) - np.percentile(v, 25)) for v, _z, _dt in lst]
        print(f"  {gap}拍+{int(lst[0][2] * 1000)}ms {f'{zlo}-{zhi}m':>9} {int(np.median(n)):>8} "
              f"{np.median(med):>7.1f} {f'{_q(med,25):.0f}-{_q(med,75):.0f}':>13} "
              f"{abs(np.median(med) - truth) / truth:>8.0%} {np.median(iqr) / np.median(med):>6.2f}")


def _q(a, p):
    a = sorted(a)
    return a[min(len(a) - 1, int(p / 100 * (len(a) - 1)))] if a else float("nan")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("mode", choices=["scan", "calib", "audit", "flow"])
    ap.add_argument("--every", type=int, default=10)
    ap.add_argument("--limit", type=int, default=950)
    ap.add_argument("--lo", type=int, default=40)
    ap.add_argument("--hi", type=int, default=900)
    ap.add_argument("--step", type=int, default=4)
    ap.add_argument("--truth", type=float, default=36.0)
    ap.add_argument("--frames", default=None)
    ap.add_argument("--gaps", default="1,2")
    ap.add_argument("--out-dir", default=os.path.join(os.environ.get("TEMP", "."), "corr_audit"))
    a = ap.parse_args(argv[1:])
    out = Path(a.out_dir)
    if a.mode == "scan":
        scan(a.session, a.every, a.limit)
    elif a.mode == "flow":
        flow(a.session, a.lo, a.hi, a.step, a.truth)
    elif a.mode == "calib":
        calibrate(a.session, a.every, out)
    else:
        if not a.frames:
            raise SystemExit("audit 需要 --frames")
        bins = load_bins(out / f"body_bins_{a.session}.npz")
        audit(a.session, [int(x) for x in a.frames.split(",")],
              [int(x) for x in a.gaps.split(",")], out, bins)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
