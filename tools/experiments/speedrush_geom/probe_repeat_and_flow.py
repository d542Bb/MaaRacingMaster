"""第二步前置：独立速度真值（裁判）+ 点云重复性的三种口径。

**为什么分两个阶段**：运动估计的两条通道都靠「图像自己的位移」换算成米/秒，需要一个
既不吃深度也不吃光流的独立真值；游戏自己的里程面板就是这一路。上一版把它当现成
读数用，结果裁判本身不合格（面板闪断时读的是天空），于是两条通道谁也没被判出来。
裁判没验证合格之前，flow 阶段的数字不进任何结论。

**阶段 referee —— 把真值修到可信**
逐帧读里程，三道判据：
  1. 逐字段暗底闸门（面板会闪断，OFF 时整块移出画面，那块是亮的天空）；
  2. 必须是裸整数（碎字、粘连一律丢）；
  3. **增量**自洽：合计分值 = 里程 + 30×超车 是游戏自己的记账（见 RULES §4），
     故 Δ合计 − Δ里程 必为非负且被 30 整除——违例即至少一边读错，比单帧格式判据强。
读数单调、冻结段（值不变而时间在走）切成干净段，段内用**两端读数与真实时间差**做
区间平均速度；跨冻结/跨违例的区间一律丢弃，不拿相邻读数凑。另出裁剪图拼版，人工
核验裁判自身的错读率。

**阶段 flow —— 点云重复性 + 两条运动通道**
A 三种口径分开报，不再混为一谈：
  · 固定像素：同像素位置、灰度几乎没变（外观相似 ≠ 同一三维点，是乐观界）；
  · 固定像素 + 纹理门：再加梯度能量达标，压低「两块不同低纹理沥青长得一样」；
  · 同一场景点：先做前后校验的稀疏跟踪拿到图像对应，再比较**对应两端**的深度。
B 两条通道都按透视模型逐行换算（行速度 = V·(v−v_h)²/(f·H)，随行号变化，不是常数位移）：
  · 稀疏特征：LK + 前后一致校验 + 纹理门；
  · 整带模型流：对候选 V 把上一帧按各自的行位移 warp 后与当前帧求归一化互相关，取峰。
  低频 f、H 用整段的稳健均值（不吃当拍深度），真值窗口与估计窗口严格对齐（段内取均值）。

产物只打印 + 临时目录拼版，不落生产代码。

用法：
    python tools/experiments/speedrush_geom/probe_repeat_and_flow.py \
        20261022_172231_p2 referee [--every 1 --limit 950]
    python tools/experiments/speedrush_geom/probe_repeat_and_flow.py \
        20261022_172231_p2 flow [--stride 2 --limit 200]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "experiments" / "speedrush_scoring"))

from probe_hud_ocr import HudOcr, field_on_dark  # noqa: E402
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    Y0, DIAG_Y1, infer_points, load_session, reading_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DEBUG = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster"
         / "debug" / "speedrush")
REGIONS = (ROOT / "maaracing_master" / "plugins" / "speedrush"
           / "resources" / "policy" / "hud_regions.json")
CAM_W = 1280
CAM_H = 720
HUD_OCR = HudOcr()
REGS = json.loads(REGIONS.read_text(encoding="utf-8"))
PANEL = ("mileage", "overtake", "total_left")
OVERTAKE_WEIGHT = 30.0       # 合计 = 里程 + 30×超车（RULES §4 记账，用作增量自洽判据）

MIN_TRUTH_DT = 0.4           # 区间平均速度的最短窗口（米制读数不能按单拍差分）
MAX_TRUTH_V = 80.0           # 物理合理域（m/s）
FROZEN_S = 0.30              # 读数不变而时间走过这么多 = 冻结段

PIX_DIFF = 12                # 「同一块表面」判据：灰度逐像素差上限
GRAD_MIN = 30.0              # 纹理门：梯度能量下限（低沥青上 PIX_DIFF 单独偏松）
BANDS = (("近 600-715", 600, 715), ("中 450-600", 450, 600), ("远 340-450", 340, 450))
FLOW_ROWS = (360, 432)       # 地面流带：360 以上贴地平线收的是护栏基部，520 以下是自车
SRC_ROWS = (320, 432)        # warp 取样源带（要容得下向上的位移量）
V_GRID = np.arange(0.0, 91.0, 1.5)   # 候选前进速度（m/s）


def _gray(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _rows(name: str) -> list[dict]:
    return [json.loads(x) for x in
            (DEBUG / name / f"trace_{name}.jsonl").read_text(encoding="utf-8").splitlines()
            if x]


def _frame(name: str, i: int) -> np.ndarray | None:
    f = DEBUG / name / "raw" / f"{i + 1:04d}_raw.jpg"
    return np.asarray(Image.open(f).convert("RGB")) if f.exists() else None


def _q(a, p):
    a = sorted(a)
    return a[min(len(a) - 1, int(p * (len(a) - 1)))] if a else float("nan")


# ---------------------------------------------------------------- 裁判（独立真值）

def reads(name: str, every: int, limit: int) -> list[dict]:
    """逐帧读左侧三格（暗底闸门 + 裸整数），带回闸门原文与真实时间戳。"""
    rows = _rows(name)
    out = []
    for i in range(0, min(len(rows), limit * every), every):
        rgb = _frame(name, i)
        if rgb is None:
            continue
        rec = {"i": i, "t": rows[i]["ts_ns"] / 1e9, "raw": {}, "val": {}}
        for k in PANEL:
            if not field_on_dark(rgb, REGS[k]):
                rec["raw"][k] = None          # 闸门判不在场：不拿天空凑数
                rec["val"][k] = None
                continue
            txt = HUD_OCR.read(rgb, REGS[k]).strip()
            rec["raw"][k] = txt
            m = re.fullmatch(r"(\d{3,4})", txt)
            rec["val"][k] = int(m.group(1)) if m else None
        out.append(rec)
    return out


def segments(rs: list[dict]) -> tuple[list[list[dict]], dict]:
    """切成干净段：段内里程单调、且每步都通过「合计 = 里程 + 30×超车」增量自洽。

    冻结（里程不变而时间走过 FROZEN_S）也断段——段两端仍按**真实时间差**算速度，
    不把被丢弃的时间当成没发生。
    """
    brk = {"不在场": 0, "碎字": 0, "违例": 0, "冻结": 0}
    segs: list[list[dict]] = []
    cur: list[dict] = []
    for r in rs:
        ok, why = True, None
        if r["val"]["mileage"] is None:
            ok = False
            why = "不在场" if r["raw"]["mileage"] is None else "碎字"
        elif cur:                      # cur 内里程必有值（坏读数不入段）
            p = cur[-1]
            d_mi = r["val"]["mileage"] - p["val"]["mileage"]
            both = all(x["val"][k] is not None for x in (r, p)
                       for k in ("total_left", "overtake"))
            if d_mi < 0:
                ok, why = False, "违例"
            elif d_mi == 0 and r["t"] - p["t"] > FROZEN_S:
                ok, why = False, "冻结"
            elif both:
                extra = (r["val"]["total_left"] - p["val"]["total_left"]) - d_mi
                if extra < 0 or extra % OVERTAKE_WEIGHT:
                    ok, why = False, "违例"
        if why:
            brk[why] += 1
        if not ok:
            if len(cur) >= 2:
                segs.append(cur)
            cur = []
        if r["val"]["mileage"] is not None:
            cur.append(r)
    if len(cur) >= 2:
        segs.append(cur)
    return segs, brk


def truth(segs: list[list[dict]], win: float = MIN_TRUTH_DT) -> list[tuple[float, float, float]]:
    """干净段内所有满足最短窗口的区间平均速度 → [(t0, t1, V)]。

    段尾的**顶格平台**先切掉：里程爬到本段上限（~342）后会停住不走，这段读数
    不再是距离的函数，留着它就把真值分布的下半截污染成零速（实测 0.4s 窗口
    42% 的样本速度 <1 m/s）。切法是「末尾连续同值一律丢」，不看具体数值。
    """
    out = []
    for seg in segs:
        while len(seg) > 2 and seg[-1]["val"]["mileage"] == seg[-2]["val"]["mileage"]:
            seg = seg[:-1]
        for a in seg:
            for b in seg:
                if b["i"] <= a["i"]:
                    continue
                dt = b["t"] - a["t"]
                if dt < win:
                    continue
                v = (b["val"]["mileage"] - a["val"]["mileage"]) / dt
                if 0.0 <= v <= MAX_TRUTH_V:
                    out.append((a["t"], b["t"], v))
                break          # 每个起点只取最短合格窗口，避免重叠样本虚增 n
    return out


def dump_crops(name: str, rs: list[dict], dest: Path, tbase: float) -> Path:
    """把采样帧的里程格放大拼版（标注闸门/原文/解析值），供人工核验裁判错读率。"""
    h, w = CAM_H, CAM_W
    r = REGS["mileage"]
    boxes = (int(r[0] * w), int(r[1] * h), int(r[2] * w), int(r[3] * h))
    picks = rs[::max(1, len(rs) // 24)][:24]
    scale, pad, lab = 4, 6, 20
    cw = (boxes[2] - boxes[0]) * scale
    tile_h = (boxes[3] - boxes[1]) * scale + lab + pad
    sheet = Image.new("RGB", (cw * 4 + pad * 5, (tile_h + pad) * ((len(picks) + 3) // 4) + pad),
                      (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    font = next((ImageFont.truetype(p, 14) for p in
                 ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf")
                 if Path(p).exists()), ImageFont.load_default())
    for n, rec in enumerate(picks):
        rgb = _frame(name, rec["i"])
        if rgb is None:
            continue
        crop = Image.fromarray(rgb[boxes[1]:boxes[3], boxes[0]:boxes[2]]).resize(
            (cw, (boxes[3] - boxes[1]) * scale), Image.NEAREST)
        col, row_i = n % 4, n // 4
        x = pad + col * (cw + pad)
        y = pad + row_i * (tile_h + pad)
        sheet.paste(crop, (x, y))
        v = rec["val"]["mileage"]
        tag = (f"i{rec['i']} t{rec['t'] - tbase:.2f} → {v if v is not None else '✗'} "
               f"| raw={rec['raw']['mileage']!r}")
        d.text((x + 2, y + crop.height + 2), tag, font=font, fill=(255, 230, 120))
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"mile_review_{name}.png"
    sheet.save(out)
    return out


def stage_referee(name: str, every: int, limit: int, review: Path | None) -> None:
    rs = reads(name, every, limit)
    tb = rs[0]["t"] if rs else 0.0
    n_gate = sum(1 for r in rs if r["raw"]["mileage"] is None)
    n_frag = sum(1 for r in rs if r["raw"]["mileage"] is not None and r["val"]["mileage"] is None)
    segs, brk = segments(rs)
    tr = truth(segs)
    cap = max((r["val"]["mileage"] for r in rs if r["val"]["mileage"] is not None), default=0)
    print(f"  里程上界（顶格值）{cap}  读数步长 p50="
          f"{statistics.median([b['val']['mileage'] - a['val']['mileage'] for s in segs for a, b in zip(s, s[1:]) if b['val']['mileage'] > a['val']['mileage']] or [0])}m")
    print(f"  窗口长度扫描（切掉顶格平台后；读数分辨率 ±1m 决定可用下限）：")
    for w in (0.4, 0.8, 1.5):
        vs = [x[2] for x in truth(segs, w)]
        if len(vs) < 5:
            print(f"    {w:>4}s 只有 {len(vs)} 个区间")
            continue
        print(f"    {w:>4}s n={len(vs):4d}  p10={_q(vs, .1):5.1f} p50={_q(vs, .5):5.1f} "
              f"p90={_q(vs, .9):5.1f} m/s   低速(<5)占比={sum(1 for x in vs if x < 5) / len(vs):.0%}")
    t_span = rs[-1]["t"] - rs[0]["t"] if len(rs) > 1 else 0.0
    print(f"\n===== 裁判 {name}  读 {len(rs)} 帧（每 {every} 帧）  时长 {t_span:.1f}s")
    print(f"  闸门判不在场 {n_gate} 帧（{n_gate / max(1, len(rs)):.0%}）"
          f"  在场但碎字 {n_frag} 帧  → 可用读数 {len(rs) - n_gate - n_frag} 帧")
    cov = sum(s[-1]["t"] - s[0]["t"] for s in segs)
    print(f"  断段原因：{brk}   干净段 {len(segs)} 段，最长 {max((len(s) for s in segs), default=0)} 帧"
          f"  合计覆盖 {cov:.1f}s / {t_span:.1f}s")
    for s in sorted(segs, key=lambda x: -len(x))[:5]:
        mi0, mi1 = s[0]["val"]["mileage"], s[-1]["val"]["mileage"]
        ov0, ov1 = s[0]["val"]["overtake"], s[-1]["val"]["overtake"]
        to0, to1 = s[0]["val"]["total_left"], s[-1]["val"]["total_left"]
        res = ("—" if None in (ov0, ov1, to0, to1) else
               f"{(to1 - to0) - (mi1 - mi0) - OVERTAKE_WEIGHT * (ov1 - ov0):+.0f}")
        print(f"    段 t{s[0]['t'] - tb:6.2f}~{s[-1]['t'] - tb:6.2f}s"
              f" 里程 {mi0}→{mi1}  超车 {ov0}→{ov1}  合计 {to0}→{to1}  记账残差 {res}")
    if tr:
        v = [x[2] for x in tr]
        dt = [x[1] - x[0] for x in tr]
        print(f"  区间平均速度（独立于深度与光流）：n={len(v)}  p50={statistics.median(v):.1f} m/s"
              f"（{statistics.median(v) * 3.6:.0f} km/h）"
              f" p5-p95=[{_q(v, 0.05):.1f},{_q(v, 0.95):.1f}]  全距[{min(v):.1f},{max(v):.1f}]")
        print(f"  窗口长度 p50={statistics.median(dt):.2f}s   读数分辨率 ±1m → "
              f"p50 速度相对误差 ±{100 / (statistics.median(v) * statistics.median(dt)):.0f}%")
    else:
        print("  没有合格区间——裁判仍不可用")
    if review:
        print(f"  人工核验拼版：{dump_crops(name, rs, review, tb)}")


# ---------------------------------------------------- A 点云重复性 / B 运动通道

def _grad(g: np.ndarray) -> np.ndarray:
    return cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3),
                         cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))


def _fb_tracks(prev_g: np.ndarray, g: np.ndarray, rows: tuple[int, int],
               grad_min: float) -> tuple[np.ndarray, np.ndarray]:
    """稀疏跟踪 + 前后一致校验 + 纹理门 → 高置信图像对应（前向点, 后向点）。"""
    mask = np.zeros(prev_g.shape, np.uint8)
    mask[rows[0]:rows[1], 60:CAM_W - 60] = 255
    p0 = cv2.goodFeaturesToTrack(prev_g, maxCorners=400, qualityLevel=0.004,
                                 minDistance=10, mask=mask)
    if p0 is None or len(p0) < 10:
        return np.empty((0, 2)), np.empty((0, 2))
    p1, st, _ = cv2.calcOpticalFlowPyrLK(prev_g, g, p0, None, winSize=(41, 41), maxLevel=4)
    if p1 is None:
        return np.empty((0, 2)), np.empty((0, 2))
    p2, st2, _ = cv2.calcOpticalFlowPyrLK(g, prev_g, p1, None, winSize=(41, 41), maxLevel=4)
    if p2 is None:
        return np.empty((0, 2)), np.empty((0, 2))
    a = p0.reshape(-1, 2)
    b = p1.reshape(-1, 2)
    c = p2.reshape(-1, 2)
    ok = ((st.ravel() == 1) & (st2.ravel() == 1)
          & (np.abs(a - c).max(axis=1) <= 1.5)
          & (np.abs(b - a).max(axis=1) <= 80))
    if grad_min > 0:
        gi = np.clip(np.round(a[:, 1]).astype(int), 0, prev_g.shape[0] - 1)
        gj = np.clip(np.round(a[:, 0]).astype(int), 0, prev_g.shape[1] - 1)
        ok &= _grad(prev_g)[gi, gj] >= grad_min
    return a[ok], b[ok]


def _z_at(zmap: np.ndarray, ds: float, xy: np.ndarray) -> np.ndarray:
    iy = np.clip(np.round(xy[:, 1] / ds).astype(int), 0, zmap.shape[0] - 1)
    ix = np.clip(np.round(xy[:, 0] / ds).astype(int), 0, zmap.shape[1] - 1)
    return zmap[iy, ix]


def _flow_track(prev_g, g, fH: float, v_h: float, dt: float):
    """通道 B1：稀疏特征逐点按自己的行号换算前进速度（行速度随行号平方变化）。"""
    a, b = _fb_tracks(prev_g, g, FLOW_ROWS, GRAD_MIN)
    if len(a) < 10:
        return None, 0, len(a)
    dv = b[:, 1] - a[:, 1]
    den = (a[:, 1] - v_h) ** 2
    keep = (den > 400) & (dv > 0)
    if keep.sum() < 10:
        return None, 0, int(keep.sum())
    per = dv[keep] * den[keep] / fH / dt          # V = ṽ·(v−v_h)²/(f·H·dt)
    return float(np.median(per)), float((np.percentile(per, 90) - np.percentile(per, 10))), int(keep.sum())


def _flow_model(prev_g, g, fH: float, v_h: float, dt: float):
    """通道 B2：整带模型流——候选 V 下逐行位移 warp 上一帧，与当前帧求 NCC 取峰。

    不用「整带一个常数位移」：透视下行位移 ∝ (v−v_h)²，同一条带内差近十倍，
    常数位移假设会把峰冲掉（上一版就是这么废的）。
    """
    src = prev_g[SRC_ROWS[0]:SRC_ROWS[1], 60:CAM_W - 60].astype(np.float32)
    tgt = g[FLOW_ROWS[0]:FLOW_ROWS[1], 60:CAM_W - 60].astype(np.float32)
    rows_abs = np.arange(FLOW_ROWS[0], FLOW_ROWS[1], dtype=np.float32)
    prof = []
    for V in V_GRID:
        s = V * dt * (rows_abs - v_h) ** 2 / fH     # 每行的向下位移（px）
        ys = rows_abs - s - SRC_ROWS[0]             # 换成 src 带内的行号
        valid = (ys >= 0) & (ys <= src.shape[0] - 2)
        if valid.sum() < 24:
            continue
        i0 = np.clip(np.floor(ys), 0, src.shape[0] - 2).astype(int)
        frac = (ys - np.floor(ys))[:, None]
        a = src[i0] * (1 - frac) + src[i0 + 1] * frac
        b = tgt[valid]
        a = a[valid]
        a = a - a.mean(axis=1, keepdims=True)
        b = b - b.mean(axis=1, keepdims=True)
        den = float(np.sqrt((a * a).sum() * (b * b).sum()))
        prof.append((float(V), float((a * b).sum() / max(den, 1e-6))))
    if not prof:
        return None, 0.0, 0
    arr = np.array(prof)
    k = int(arr[:, 1].argmax())
    if arr[k, 1] < 0.15 or k == 0 or k == len(arr) - 1:
        return None, float(arr[k, 1]), len(prof)
    near = arr[arr[:, 1] >= arr[k, 1] - 0.02]
    return float(arr[k, 0]), float(near[:, 0].max() - near[:, 0].min()), len(prof)


def stage_flow(name: str, stride: int, limit: int, truth_every: int) -> None:
    rows = _rows(name)
    cal = load_calib()
    sess = load_session(DEPTH_MODEL_FILE)
    seqs = list(range(1, min(len(rows), limit * stride) + 1, stride))
    fx_l: list[float] = []
    h_l: list[float] = []
    rep = {k: {lbl: [] for lbl, _, _ in BANDS} for k in ("固定像素", "像素+纹理门", "同一场景点")}
    spd: dict[str, list[tuple[float, float, int]]] = {"特征流": [], "模型流": []}
    t_of = {i: rows[i]["ts_ns"] / 1e9 for i in range(len(rows))}
    prev = prev_g = None
    t0 = t_of.get(seqs[0], 0.0)
    for s in seqs:
        rgb = _frame(name, s - 1)
        if rgb is None:
            continue
        g = _gray(rgb)
        pts, fx, fy = infer_points(sess, rgb, native=True)
        ds = CAM_W / pts.shape[1]
        rd = reading_from_points(pts.copy(), fx, cal, fy=fy, ds=ds)
        if rd.coef is not None:
            fx_l.append(fx)
            h_l.append(abs(rd.coef[2]))
        gn = cv2.resize(g, (pts.shape[1], pts.shape[0]), interpolation=cv2.INTER_AREA)
        Z = pts[:, :, 2]
        if prev is not None and prev_g is not None:
            pZ, pgn, pds = prev
            t = t_of.get(s - 1, 0.0) - t0
            pt = t_of.get(s - 1 - stride, 0.0) - t0
            dt = t - pt
            same = (np.abs(gn.astype(np.int16) - pgn.astype(np.int16)) <= PIX_DIFF) & \
                np.isfinite(Z) & np.isfinite(pZ) & (Z > 0.5) & (pZ > 0.5)
            grad_ok = (_grad(gn) >= GRAD_MIN) & (_grad(pgn) >= GRAD_MIN)
            for key, base in (("固定像素", same), ("像素+纹理门", same & grad_ok)):
                for lbl, y0, y1 in BANDS:
                    m = base.copy()
                    m[:int(y0 / ds), :] = False
                    m[int(y1 / ds):, :] = False
                    if m.sum() < 200:
                        continue
                    rel = np.abs(Z - pZ)[m] / np.maximum(Z[m], pZ[m])
                    rep[key][lbl].append((float(np.median(rel)), float(np.percentile(rel, 90)),
                                          int(m.sum()), float(np.median(np.abs(Z - pZ)[m]))))
            # 同一场景点：图像对应（前后校验的稀疏跟踪）两端各自的深度
            a, b = _fb_tracks(prev_g, g, (Y0, DIAG_Y1), GRAD_MIN)
            if len(a) >= 20:
                z1 = _z_at(pZ, pds, a)
                z2 = _z_at(Z, ds, b)
                okp = np.isfinite(z1) & np.isfinite(z2) & (z1 > 0.5) & (z2 > 0.5)
                for lbl, y0, y1 in BANDS:
                    sel = okp & (a[:, 1] >= y0) & (a[:, 1] < y1)
                    if sel.sum() < 12:
                        continue
                    rel = np.abs(z2[sel] - z1[sel]) / np.maximum(z1[sel], z2[sel])
                    rep["同一场景点"][lbl].append((float(np.median(rel)), float(np.percentile(rel, 90)),
                                                  int(sel.sum()), float(np.median(np.abs(z2[sel] - z1[sel])))))
            # B 两条运动通道（低频 f、H，不吃当拍深度）
            if len(fx_l) > 20 and len(h_l) > 20 and dt > 0.02:
                fH = float(np.median(fx_l)) * float(np.median(h_l))
                for key, fn in (("特征流", _flow_track), ("模型流", _flow_model)):
                    v, spread, n = fn(prev_g, g, fH, cal.y_h, dt)
                    if v is not None:
                        spd[key].append(((t + pt) / 2.0, v, n))
        prev = (Z.copy(), gn, ds)
        prev_g = g
    fH = float(np.median(fx_l)) * float(np.median(h_l)) if fx_l else float("nan")
    print(f"\n===== flow {name}  对 {len(seqs) - 1} 组（间隔 {stride} 拍 ≈ "
          f"{stride * 0.05:.2f}s）  低频 f·H={fH:.0f} px·m")
    print("\nA 点云重复性（三种口径，别再混为一谈）：")
    print(f"  {'口径':>12} {'距离段':>10} {'|Δz|/z 中位':>11} {'p90':>6} {'|Δz| 中位':>9} {'样本':>7}")
    for key in ("固定像素", "像素+纹理门", "同一场景点"):
        for lbl, _, _ in BANDS:
            a = rep[key][lbl]
            if not a:
                print(f"  {key:>12} {lbl:>10} 无有效样本")
                continue
            print(f"  {key:>12} {lbl:>10} {statistics.median([x[0] for x in a]):>11.1%} "
                  f"{statistics.median([x[1] for x in a]):>5.1%} "
                  f"{statistics.median([x[3] for x in a]):>8.2f}m "
                  f"{int(statistics.median([x[2] for x in a])):>7}")
    print("\nB 不吃 MoGe 逐像素深度的前进速度：")
    for key, src in spd.items():
        if not src:
            print(f"  {key}: 一次也没估出（全被置信/位移校验挡掉）")
            continue
        v = [x[1] for x in src]
        print(f"  {key}: 出解 {len(src)}/{len(seqs) - 1} 拍  p50={statistics.median(v):.1f} m/s"
              f"（{statistics.median(v) * 3.6:.0f} km/h） 全距[{min(v):.1f},{max(v):.1f}]"
              f"  逐拍抖 p50={statistics.median([abs(q - p) for p, q in zip(v, v[1:])]):.1f} m/s"
              f"  组内点数 p50={int(statistics.median([x[2] for x in src]))}")
    rs = reads(name, truth_every, max(2, int(len(seqs) * stride / truth_every)))
    tr = [(a - t0, b - t0, v) for a, b, v in truth(segments(rs)[0])]
    if not tr:
        print("\n  裁判仍没给出合格区间 → B 的数字无从判定，不作任何结论")
        return
    tv = [x[2] for x in tr]
    print(f"\n  裁判（里程差分，每 {truth_every} 帧读一次，干净段 {len(rs)} 帧读 → {len(tr)} 个区间）："
          f"p50={statistics.median(tv):.1f} m/s p5-p95=[{_q(tv, 0.05):.1f},{_q(tv, 0.95):.1f}]")
    for key, src in spd.items():
        pairs = []
        for a, b, v in tr:
            est = [y[1] for y in src if a <= y[0] <= b]
            if len(est) >= 2:                       # 与真值同窗口取均值，不比瞬时对平均
                pairs.append((float(np.mean(est)), v, len(est)))
        if len(pairs) < 5:
            print(f"  {key}: 与真值同窗口的对照只有 {len(pairs)} 组，太少")
            continue
        e = np.array([abs(x[0] - x[1]) / max(x[1], 1.0) for x in pairs])
        p = np.array([x[0] for x in pairs])
        q = np.array([x[1] for x in pairs])
        print(f"  {key}: 对照 {len(pairs)} 组  p50={statistics.median(list(p)):.1f} m/s"
              f"  |相对误差| p50={float(np.median(e)):.0%} p90={float(np.percentile(e, 90)):.0%}"
              f"  相关 r={float(np.corrcoef(p, q)[0, 1]):+.2f}"
              f"  每窗口样本 p50={int(statistics.median([x[2] for x in pairs]))}")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("stage", choices=["referee", "flow"])
    ap.add_argument("--stride", type=int, default=2, help="flow：深度刷新对间隔（拍）")
    ap.add_argument("--every", type=int, default=1, help="referee：读数采样间隔（拍）")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--truth-every", type=int, default=4, help="flow：裁判读数采样间隔（拍）")
    ap.add_argument("--review", default=None, help="referee：裁剪拼版输出目录")
    a = ap.parse_args(argv[1:])
    if a.stage == "referee":
        stage_referee(a.session, a.every, a.limit, Path(a.review) if a.review else None)
    else:
        stage_flow(a.session, a.stride, a.limit, a.truth_every)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
