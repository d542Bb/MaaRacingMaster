"""裁剪 × 分辨率预算探针：路肩裁剪是否改善延迟/质量（C 类，2026-09-23，维护者提问驱动）。

**要回答的问题**（维护者 2026-09-23）：
  ① yolo26n vs DA-S 的质量差距能否可视化（对比拼图）；
  ② 裁掉画面上部 UI/非路面区域（保留 y≥190，远带 y≈213 与地平线 324 全保留）能否
     改善性能？质量是否仍够用？
  ③ 低分辨率下质量是否够用（尸检只测了延迟 @280=110/@392=204/@518=355ms，质量未测）。

**关键口径**：
  - 裁剪线 y=190：旧检测 ROI 顶=201、远带 y≈213，故 190 是"保留全部车辆内容"的
    最激进裁剪；UI 在其上。
  - 结构陷阱（先验推演，lat 模式实测验证）：DA 类"短边定长"缩放下，**同短边裁剪
    反而更慢**（1280×720→518×924=479k px；1280×530→518×1252=648k px）；裁剪的
    正确用法是**同延迟预算下比较**（crop@446 ≈ full@518 的 token 数）——分辨率
    预算花在路面上还是花在天空上。
  - yolo26n 输入固定 768×768 方形，裁剪**不改变延迟**，只可能改变质量（纵向拉伸
    1.83× vs 全帧 1.07×，几何失真影响待测）。
  - 参照系 = DA-S 全帧@518（既有缓存）。无真值，全部读数是"对参照的保真度" +
    不依赖参照的绝对指标（M1 负率 / M2 单调 / M3 自稳定）。yolo26n 轮廓经 A2-0
    选定的 1/z 变换 + 逐帧 Theil-Sen 仿射校正后比 nRMSE（同 A2-0 口径）。

**帧集**：bad(72)+ctrl(31)+stab(41) = 144 帧（与前序实验同帧集）；stab 连续帧缓存
完整图（M3 需要），bad+ctrl 只落逐帧指标 CSV（可复算，不堆中间产物）。

用法（仓库根，.venv Python）：
    P=tools/experiments/speedrush_vision/probe_crop_quality.py
    python $P lat      # 全组合 25 次纯推理 p50/p95（含"同短边裁剪更慢"验证）
    python $P infer    # 144 帧 × 9 质量组合：stab 图缓存 + 逐帧指标 CSV
    python $P stats    # 汇总表（M3 + CSV 聚合）
    python $P cmp      # 对比拼图：RGB | yolo26n | DA-S | DA裁剪@336（6 代表帧）
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_depth as pd

APP = pd.APP
OUT = pd.OUT
NPY = OUT / "npy"
CROP_TOP = 190                       # 裁剪线：保留 y≥190（远带 213、地平线 324 均在）
CROP_H = 720 - CROP_TOP
REF_KEY = "da2s"                     # 参照 = DA-S 全帧@518（既有缓存）
EGO_X = (540, 740)
FAR_BAND = (340, 450)                # 路面轮廓最远三分之一（轮廓口径 y≥Y_H+16=340 起）

# 质量组合（family, crop_top, short）；yolo short=None（固定 768 方形）
QUALITY_COMBOS = [
    ("da", 0, 392), ("da", 0, 336), ("da", 0, 280),
    ("da", CROP_TOP, 446), ("da", CROP_TOP, 392), ("da", CROP_TOP, 336),
    ("da", CROP_TOP, 280),
    ("y26n", 0, None), ("y26n", CROP_TOP, None),
    ("y26s", 0, None), ("y26s", CROP_TOP, None),
    ("y26m", 0, None), ("y26m", CROP_TOP, None),
]
# 延迟组合 = 质量组合 + 仅测速的对照（同短边裁剪更慢 的直接验证）
LAT_ONLY = [("da", 0, 518), ("da", CROP_TOP, 518)]
MS = {  # 尸检/A 实验既有实测（全帧），用于对照
    ("da", 0, 518): 355.0, ("da", 0, 392): 204.0, ("da", 0, 280): 110.0,
    ("y26n", 0, None): 11.0,
}


def frame_key(p: Path) -> str:
    return pd.frame_key(p)


def all_frames() -> list[Path]:
    frames = pd.set_frames("bad") + pd.set_frames("ctrl")
    frames += [pd.CTRL_SESSION / f"{i:06d}.jpg" for i in pd.CTRL_STAB]
    seen, uniq = set(), []
    for p in frames:
        k = frame_key(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def combo_key(family: str, crop: int, short) -> str:
    return f"{family}_c{crop}" + (f"@{short}" if short else "")


def combo_npy(k: str, family: str, crop: int, short) -> Path:
    return NPY / f"{k}__{combo_key(family, crop, short)}.npy"


def load_map(k: str, family: str, crop: int, short) -> np.ndarray:
    return np.load(combo_npy(k, family, crop, short)).astype(np.float32)


def profile_of(d: np.ndarray, crop: int) -> np.ndarray:
    """地图（已上采样到 crop/全帧坐标）→ 逐行地面中位轮廓（y∈[Y_H+16,715] 帧坐标）。

    返回长度 720 的数组，crop_top 以上的行为 NaN——所有比较都在帧坐标对齐。
    """
    h = CROP_H if crop else 720
    full = np.full(720, np.nan, np.float32)
    lo = max(int(pd.Y_H) + 16, crop)
    for fy in range(lo, 716):
        my = fy - crop
        if my < 0 or my >= h:
            continue
        row = np.concatenate([d[my, :EGO_X[0]], d[my, EGO_X[1]:]])
        full[fy] = float(np.median(row))
    return full


def norm01(v: np.ndarray) -> np.ndarray:
    lo, hi = np.nanpercentile(v, [1, 99])
    return (v - lo) / max(hi - lo, 1e-9)


def _ranks(v: np.ndarray) -> np.ndarray:
    o = v.argsort()
    r = np.empty(len(v))
    r[o] = np.arange(len(v))
    return r


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    n = len(a)
    return 1 - 6 * ((_ranks(a) - _ranks(b)) ** 2).sum() / (n * (n * n - 1))


def theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    iu = np.triu_indices(len(x), 1)
    dx, dy = x[iu[1]] - x[iu[0]], y[iu[1]] - y[iu[0]]
    m = dx != 0
    a = float(np.median(dy[m] / dx[m]))
    return a, float(np.median(y - a * x))


def run_map(family: str, sess, rgb: np.ndarray, crop: int, short):
    """→ (map, crop) map 已上采样：全帧 (720,1280) 或裁剪 (CROP_H,1280)。"""
    img = rgb[CROP_TOP:] if crop else rgb
    if family == "da":
        blob = pd.preprocess(img, short)
        d = sess.run(None, {"pixel_values": blob})[0][0]
        d = np.nan_to_num(d.astype(np.float32), nan=0.0)
        h, w = img.shape[:2]
        return cv2.resize(d, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32), crop
    im = cv2.resize(img, (768, 768), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    z = sess.run(None, {"images": im.transpose(2, 0, 1)[None]})[0][0, 0]
    z = np.clip(np.nan_to_num(z.astype(np.float32), nan=10.0, posinf=88.0, neginf=88.0), -80, 88)
    h, w = img.shape[:2]
    return cv2.resize(z, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32), crop


def to_ref_space(d: np.ndarray, family: str, crop: int) -> np.ndarray:
    """→ 帧坐标 720 行"伪视差（大=近）"：yolo26n/s 经 1/z（A2-0 选族），da 原样。"""
    if family.startswith("y26"):
        with np.errstate(over="ignore", divide="ignore"):
            v = 1.0 / np.clip(d, 1e-3, None)
        return v
    return d


def make_session(family: str):
    if family == "da":
        return ort.InferenceSession(str(pd.MODELS["small"]), providers=["DmlExecutionProvider"])
    wname = {"y26n": "yolo26n-depth.onnx", "y26s": "yolo26s-depth.onnx",
             "y26m": "yolo26m-depth.onnx"}[family]
    return ort.InferenceSession(str(pd.WEIGHTS / wname), providers=["DmlExecutionProvider"])


def cmd_lat(args) -> None:
    rgb = cv2.cvtColor(cv2.imread(str(pd.CTRL_SESSION / "000100.jpg")), cv2.COLOR_BGR2RGB)
    print("family crop short   p50_ms  p95_ms  (尸检既有: full@518=355 @392=204 @280=110, yolo=11)")
    for family, crop, short in LAT_ONLY + QUALITY_COMBOS:
        if args.only and combo_key(family, crop, short).find(args.only) < 0:
            continue
        img = rgb[CROP_TOP:] if crop else rgb
        if family == "da":
            blob = pd.preprocess(img, short)
            feed = {"pixel_values": blob}
        else:
            im = cv2.resize(img, (768, 768), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
            feed = {"images": im.transpose(2, 0, 1)[None]}
        sess = make_session(family)
        sess.run(None, feed)
        ts = []
        for _ in range(25):
            t0 = time.perf_counter()
            sess.run(None, feed)
            ts.append(time.perf_counter() - t0)
        del sess
        print(f"{family:6s} {crop:4d} {str(short):>5s}  {np.median(ts) * 1000:6.1f}  "
              f"{np.percentile(ts, 90) * 1000:6.1f}", flush=True)


def cmd_infer(args) -> None:
    NPY.mkdir(parents=True, exist_ok=True)
    frames = all_frames()
    stab_keys = {frame_key(p) for p in pd.set_frames("stab")}
    for family, crop, short in QUALITY_COMBOS:
        if getattr(args, "only", None) and combo_key(family, crop, short).find(args.only) < 0:
            continue
        ck = combo_key(family, crop, short)
        done = sum(1 for p in frames if combo_npy(frame_key(p), family, crop, short).exists()
                   and frame_key(p) in stab_keys)
        need_stats = (OUT / f"cropq_{ck}.csv").exists()
        if done == len(stab_keys) and need_stats:
            print(f"[skip {ck}] 缓存与 CSV 已齐", flush=True)
            continue
        sess = make_session(family)
        rows = []
        t0 = time.perf_counter()
        for i, p in enumerate(frames):
            k = frame_key(p)
            rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
            d, _ = run_map(family, sess, rgb, crop, short)
            if k in stab_keys:
                np.save(combo_npy(k, family, crop, short), d.astype(np.float16))
            rows.append(frame_metrics(k, family, crop, d))
        del sess
        with (OUT / f"cropq_{ck}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"[infer {ck}] n={len(rows)} 耗时 {time.perf_counter() - t0:.0f}s", flush=True)


def frame_metrics(k: str, family: str, crop: int, d: np.ndarray) -> dict:
    """单帧指标（对参照 full@518 DA-S）：轮廓 ρ / nRMSE（仿射校正前后）/ 远带 / M2 / 框序。"""
    ref = np.load(NPY / f"{k}__{REF_KEY}.npy").astype(np.float32)
    v = to_ref_space(d, family, crop)
    g_ref = profile_of(ref, 0)
    g_c = profile_of(v, crop)
    m = ~np.isnan(g_ref) & ~np.isnan(g_c)
    rho = spearman(g_ref[m], g_c[m])
    rn, cn = norm01(g_ref[m]), norm01(g_c[m])
    a, b = theil_sen(rn, cn)
    nrmse = float(np.sqrt(((cn - rn) ** 2).mean()))
    nrmse_af = float(np.sqrt(((a * cn + b - rn) ** 2).mean()))
    fb = (g_ref > 0) & (g_c > 0)
    fb[FAR_BAND[1]:] = False
    fb[:FAR_BAND[0]] = False
    fbf = fb & ~np.isnan(g_ref) & ~np.isnan(g_c)
    if fbf.sum() > 20:
        frn, fcn = norm01(g_ref[fbf]), norm01(g_c[fbf])
        fa, fb_ = theil_sen(frn, fcn)
        far = float(np.sqrt(((fa * fcn + fb_ - frn) ** 2).mean()))
    else:
        far = np.nan
    dif = np.diff(g_c)
    mm = ~np.isnan(dif)
    m2 = float((np.sign(dif[mm]) > 0).mean())
    nbox, box_rho = 0, np.nan
    dets = json.loads((OUT / "dets" / f"{k}.json").read_text()) if (OUT / "dets" / f"{k}.json").exists() else []
    pts = []
    for cls, _sc, (x1, y1, x2, y2) in dets:
        if y1 < crop + 2 or int(y2) - int(y1) < 12 or int(x2) - int(x1) < 12:
            continue
        my1, my2 = int(y1) - crop + 2, int(y2) - crop - 2
        mx1, mx2 = max(int(x1) + 2, 0), min(int(x2) - 2, 1280)
        if my2 <= my1 or mx2 <= mx1:
            continue
        sub = v[my1:my2, mx1:mx2]
        pts.append(((y1 + y2) / 2, float(np.median(sub))))
    if len(pts) >= 2:
        cy = np.array([a_ for a_, _ in pts])
        dv = np.array([b_ for _, b_ in pts])
        nbox = len(pts)
        box_rho = spearman(cy, dv)
    return {"frame": k, "rho": round(rho, 4), "nrmse": round(nrmse, 4),
            "nrmse_af": round(nrmse_af, 4), "far_nrmse_af": round(far, 4),
            "m2": round(m2, 4), "box_rho": np.nan if np.isnan(box_rho) else round(box_rho, 4),
            "n_box": nbox}


def cmd_stats(args) -> None:
    stab = pd.set_frames("stab")
    print(f"{'组合':18s} {'ρ p50/p10':13s} {'nRMSE_af':8s} {'远带af':7s} {'M2':6s} "
          f"{'框序<0':6s} {'M3 IoU p50/p10':14s}")
    for family, crop, short in QUALITY_COMBOS:
        if args.only and combo_key(family, crop, short).find(args.only) < 0:
            continue
        ck = combo_key(family, crop, short)
        f = OUT / f"cropq_{ck}.csv"
        if not f.exists():
            print(f"{ck:18s} （缺 CSV，先 infer）")
            continue
        rows = list(csv.DictReader(f.open()))
        def col(name):
            v = np.array([float(r[name]) for r in rows if r[name] not in ("", "nan")])
            return v
        rho, nr, fr = col("rho"), col("nrmse_af"), col("far_nrmse_af")
        m2 = col("m2")
        br = col("box_rho")
        neg = (br < 0).mean() if len(br) else np.nan
        ious = []
        for a, b in zip(stab, stab[1:]):
            ka, kb = frame_key(a), frame_key(b)
            fa, fb_ = combo_npy(ka, family, crop, short), combo_npy(kb, family, crop, short)
            if not (fa.exists() and fb_.exists()):
                continue
            da = np.load(fa).astype(np.float32)
            db = np.load(fb_).astype(np.float32)
            if family == "y26n":
                da, db = to_ref_space(da, family, crop), to_ref_space(db, family, crop)
            if crop == 0:
                # M3 域对齐：全帧组合也只取帧坐标 y≥190，与裁剪组合同域可比
                da, db = da[CROP_TOP:], db[CROP_TOP:]
            ma = da > np.nanpercentile(da, 50)
            mb = db > np.nanpercentile(db, 50)
            ious.append((ma & mb).sum() / max((ma | mb).sum(), 1))
        iou_s = np.array(ious) if ious else np.array([np.nan])
        print(f"{ck:18s} {np.median(rho):.3f}/{np.percentile(rho, 10):.3f}   "
              f"{np.median(nr):8.4f} {np.median(fr):7.4f} {np.median(m2):6.3f} "
              f"{neg:6.0%} {np.median(iou_s):.3f}/{np.percentile(iou_s, 10):.3f}")


def cmd_cmp(args) -> None:
    from PIL import Image, ImageDraw, ImageFont
    picks = [("frames__000100", pd.CTRL_SESSION / "000100.jpg"),
             ("frames__000180", pd.CTRL_SESSION / "000180.jpg"),
             ("frames__000260", pd.CTRL_SESSION / "000260.jpg"),
             ("badframes_20260922_201847_p1__fid_1418",
              APP / "control_traces" / "badframes_20260922_201847_p1" / "fid_1418.jpg"),
             ("badframes_20260922_204519_p1__fid_1295",
              APP / "control_traces" / "badframes_20260922_204519_p1" / "fid_1295.jpg"),
             ("badframes_20260922_204603_p2__fid_3426",
              APP / "control_traces" / "badframes_20260922_204603_p2" / "fid_3426.jpg")]
    pw, ph = 640, 360
    panels, labels = [], ["RGB", "yolo26n 全帧", "DA-S 全帧@518", f"DA-S 裁剪@336(y≥{CROP_TOP})"]
    s_da = make_session("da")
    s_y = make_session("y26n")
    for k, p in picks:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        row = [cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), (pw, ph))]
        maps = []
        d_y, _ = run_map("y26n", s_y, rgb, 0, None)
        maps.append(("y26n", to_ref_space(d_y, "y26n", 0)))
        maps.append((REF_KEY, np.load(NPY / f"{k}__{REF_KEY}.npy").astype(np.float32)))
        d_c, _ = run_map("da", s_da, rgb, CROP_TOP, 336)
        maps.append(("da_crop", d_c))
        for name, d in maps:
            valid = d[np.isfinite(d)]
            lo, hi = np.percentile(valid, [1, 99])
            du = np.nan_to_num(np.clip((d - lo) / max(hi - lo, 1e-6) * 255, 0, 255)).astype(np.uint8)
            if name == "da_crop":
                canvas = np.zeros((720, 1280), np.uint8)
                canvas[CROP_TOP:] = du
                du = canvas
            row.append(cv2.resize(cv2.applyColorMap(du, cv2.COLORMAP_JET), (pw, ph)))
        panels.append(row)
    del s_da, s_y
    sheet = Image.new("RGB", (4 * pw, len(picks) * (ph + 20)), "white")
    dr = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, row in enumerate(panels):
        for j, im in enumerate(row):
            sheet.paste(Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB)), (j * pw, i * (ph + 20)))
        dr.text((4, i * (ph + 20) + ph + 4), f"{picks[i][0]}   |   " + "   |   ".join(labels),
                fill="black", font=font)
    o = OUT / "cmp_models.jpg"
    sheet.save(o, quality=85)
    print(f"[cmp] {o}（6 帧 × RGB|yolo26n|DA-S|DA裁剪@336；JET 红=近 蓝=远，各自 p1-p99 归一）")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("lat", "infer", "stats", "cmp"):
        s = sub.add_parser(name)
        if name != "cmp":
            s.add_argument("--only", default=None, help="子串过滤组合键（如 y26s）")
    args = ap.parse_args()
    if args.cmd == "lat":
        cmd_lat(args)
    elif args.cmd == "infer":
        cmd_infer(args)
    elif args.cmd == "stats":
        cmd_stats(args)
    else:
        cmd_cmp(args)


if __name__ == "__main__":
    main()
