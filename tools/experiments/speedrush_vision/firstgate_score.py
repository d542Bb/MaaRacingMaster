"""第一关原型评分：fp16 vs q4f16 台阶边界保真配对终选（2026-09-24，A3 收口）。

**问**（README 体积线复查节悬置 + A3 收口）：q4f16 的平滑漂移是否侵蚀金标台阶
对比度（台阶读数 h/H≈0.045 vs 量化地板 nRMSE≈0.014 同量级，必须实测）；两变体在
region 边界产出与位置上是否可互换——终选判据 = 台阶边界保真 + 时序抖动。

**配对设计**（金标 54 帧，同帧两变体都跑，README 金标节纪律）：
- P1 产出（成败，逐帧×侧）→ **McNemar 只看不一致对**，精确二项 p。
- dev（region 内沿 − 金标线，near/far 带，gold_score 同采样口径）→ 配对差值
  （q4f16−fp16）中位 + bootstrap 95% CI；**禁止**"fp16 失败 n 帧 vs q4f16 失败 m 帧"
  式非配对表述。
- 台阶对比度：金标线**外侧** 0~40px 窗的 (M−g)/g 中位（y∈{560,600,640}，g 用该
  变体自己的 ground_model），内侧 40~80px 窗作本底（应≈0）——信号与本底分开报。
- 时序抖动：金标连续段 curve_cont（906..920）/ curve_cont2（714..728）上 y=600
  内沿 x 的 1 阶/2 阶差分 std（px；2 阶去匀速后才是抖动）。

**工作点**：@336 = 主工作点（部署规格 p50≤22ms）；@518 = 离线参照档对照（分辨率
误差最小，量化地板最可见处）。fp32@518 参照缓存 `{key}__da2s.npy` 已有。

**常数校正口径**：near 带 dev 中位（−20~27px，README 金标首轮评分）逐变体带出，
同时报校正后 p90|dev−med|——第一关的残余散布才是保真读数。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/firstgate_score.py infer   # 出图缓存（~1 分钟）
    python tools/experiments/speedrush_vision/firstgate_score.py score
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402
import probe_step_detect as psd  # noqa: E402
from gold_score import OUT, YS, gold_x_at  # noqa: E402

YS_NEAR = (560, 600, 640)

# 变体 → (权重文件, 短边, 缓存模板)；fp32 参照沿用既有缓存名
VARIANTS = {
    "fp32@518": (None, 518, "{key}__da2s.npy"),
    "fp16@336": ("da2_small_fp16.onnx", 336, "{key}__da2s_fp16@336.npy"),
    "q4f16@336": ("oc_model_q4f16.onnx", 336, "{key}__da2s_q4f16@336.npy"),
    "fp16@392": ("da2_small_fp16.onnx", 392, "{key}__da2s_fp16@392.npy"),
    "fp16@518": ("da2_small_fp16.onnx", 518, "{key}__da2s_fp16@518.npy"),
    "q4f16@518": ("oc_model_q4f16.onnx", 518, "{key}__da2s_q4f16@518.npy"),
}
SESS_CACHE: dict = {}


def folded_sess(weights: str, short: int):
    """固定三维 free dim 的折叠会话（psd._folded_sess 的权重参数化版）。"""
    ck = (weights, short)
    if ck in SESS_CACHE:
        return SESS_CACHE[ck]
    import onnxruntime as ort
    so = ort.SessionOptions()
    nh, nw = pd._resize_hw(720, 1280, short)
    for n, v in (("batch_size", 1), ("height", nh), ("width", nw)):
        so.add_free_dimension_override_by_name(n, v)
    sess = ort.InferenceSession(str(pd.WEIGHTS / weights), so,
                                providers=["DmlExecutionProvider"])
    SESS_CACHE[ck] = sess
    return sess


def load_variant_map(variant: str, path: Path, rgb) -> np.ndarray:
    weights, short, tpl = VARIANTS[variant]
    cache = pcq.NPY / tpl.format(key=pcq.frame_key(path))
    if cache.exists():
        return np.load(cache).astype(np.float32)
    m = pd.depth_map(folded_sess(weights, short), rgb, short)
    pcq.NPY.mkdir(parents=True, exist_ok=True)
    np.save(cache, m.astype(np.float16))
    return m.astype(np.float32)


def gold_rows():
    return list(csv.DictReader((OUT / "gold_labels.csv").open(encoding="utf-8")))


def collect(variant: str, labels, gate: float | None = None) -> list[dict]:
    """一变体全金标 → 逐样本记录（P1/对比度/dev 同帧同侧同行可配对）。
    gate=None 用 @518 口径原门 2%；--selfgate 时传该档自洽门（README @392 节）。"""
    recs = []
    for r in labels:
        p = Path(r["path"])
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        m = load_variant_map(variant, p, rgb)
        rng = psd.road_range(m)
        g = psd.ground_model(m, rng)
        blobs = psd.region_inner(m, rng, G=gate if gate is not None else 0.02)
        for side in ("l", "r"):
            S = side.upper()
            rec = {"variant": variant, "stratum": r["stratum"], "frame": p.name,
                   "side": side, "p1": int(blobs[S] is not None)}
            xg = {y: gold_x_at(r, side, y) for y in YS_NEAR}
            valid = {y: v for y, v in xg.items() if v is not None and 0 <= v < 1280}
            if valid:
                # 台阶对比度（窗几何经 001220 横切面实测定死：墙面在外侧、0~20px 是
                # 过渡带）：信号窗 = 外侧 20~60px，本底窗 = 内侧 40~120px（应≈0）。
                outs, ins = [], []
                for y, xg0 in valid.items():
                    gy = g[y - psd.Y0]
                    if side == "l":   # L 侧：墙在 x<xg（外侧），路在 x>xg
                        s_lo, s_hi, b_lo, b_hi = xg0 - 60, xg0 - 20, xg0 + 40, xg0 + 120
                    else:             # R 侧：墙在 x>xg，路在 x<xg
                        s_lo, s_hi, b_lo, b_hi = xg0 + 20, xg0 + 60, xg0 - 120, xg0 - 40
                    s_lo, s_hi = max(int(s_lo), 0), min(int(s_hi), 1279)
                    b_lo, b_hi = max(int(b_lo), 0), min(int(b_hi), 1279)
                    if s_hi > s_lo and np.isfinite(gy) and gy > 1e-6:
                        outs.append(float(np.median((m[y, s_lo:s_hi] - gy) / gy)))
                    if b_hi > b_lo and np.isfinite(gy) and gy > 1e-6:
                        ins.append(float(np.median((m[y, b_lo:b_hi] - gy) / gy)))
                rec["contrast_out"] = float(np.median(outs)) if outs else None
                rec["contrast_in"] = float(np.median(ins)) if ins else None
            else:
                rec["contrast_out"] = rec["contrast_in"] = None
            b = blobs[S]
            rec["inner600"] = b[0].get(600) if b else None
            near = [y for y in sorted(b[0]) if 560 <= y <= 700] if b else []
            far = [y for y in sorted(b[0]) if 430 <= y < 560] if b else []
            for band, cand in (("near", near), ("far", far)):
                if not cand:
                    continue
                picks = cand[:: max(len(cand) // 3, 1)][:3]
                for y in picks:
                    x = gold_x_at(r, side, y)
                    if x is None:
                        continue
                    recs.append({**rec, "band": band, "y": y, "gold": x,
                                 "region": b[0][y], "dev": b[0][y] - x})
            if not near and not far:
                recs.append(rec)
    return recs


def mcnemar(b: int, c: int) -> float:
    """精确二项双侧 p（不一致对 b/c）。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2.0 ** n
    return min(1.0, 2.0 * p)


def boot_ci(v: np.ndarray, iters: int = 10000, seed: int = 7) -> tuple[float, float]:
    rg = np.random.default_rng(seed)
    meds = [np.median(rg.choice(v, v.size)) for _ in range(iters)]
    return float(np.percentile(meds, 2.5)), float(np.percentile(meds, 97.5))


def _load_seg_csv(name: str, variant: str) -> dict:
    rows = csv.DictReader((OUT / name).open(encoding="utf-8"))
    return {(r["stratum"], r["frame"], r["side"], r["y"]): r
            for r in rows if r["variant"] == variant and r.get("band") == "near"}


def _p90_curve2(name: str, variant: str) -> float:
    rows = csv.DictReader((OUT / name).open(encoding="utf-8"))
    d = np.array([float(r["dev"]) for r in rows
                  if r["variant"] == variant and r["stratum"] == "curve_cont2"
                  and r.get("band") == "near" and "dev" in r])
    med = float(np.median(d))
    return float(np.percentile(np.abs(d - med), 90))


def _fig(labels) -> None:
    """大白话四联图：①同样台阶低分辨率报数更大 ②自洽门救回误差
    ③剩余问题=双态认错块 ④721 帧实景桥位置。PIL 手画，零新依赖。"""
    from PIL import Image, ImageDraw, ImageFont

    def F(sz):
        return ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", sz)

    W, H, M = 800, 560, 16
    img = Image.new("RGB", (W * 2 + M * 3, H * 2 + 76 + M * 2), (250, 250, 248))
    dr = ImageDraw.Draw(img)
    dr.text((M, 12), "低分辨率的眼睛没花——是尺子刻度变了 + 挑块会粘连（depth_review/firstgate_plain.jpg）",
            font=F(24), fill=(30, 30, 30))

    # ── 面板 1：横切面（000714 帧 L 侧 y=600）──
    p1 = Image.new("RGB", (W, H), "white")
    d1 = ImageDraw.Draw(p1)
    d1.text((M, 8), "① 同样一个台阶，低分辨率下报的数更大（000714 帧横切面）", font=F(22), fill=(0, 0, 0))
    r714 = [r for r in labels if "000714" in r["path"]][0]
    pth = Path(r714["path"])
    xg = gold_x_at(r714, "l", 600)
    ox0, oy0, pw, ph = 90, 40, 660, 400
    ymax = 0.30
    def X(o): return ox0 + (o + 220) / 440 * pw
    def Y(v): return oy0 + ph - min(v, ymax) / ymax * ph
    d1.line([(ox0, oy0 + ph), (ox0 + pw, oy0 + ph)], fill=(120, 120, 120), width=2)
    for gate_v, col, lab in ((0.02, (220, 40, 40), "原门 2%（高清档标定）"),):
        d1.line([(ox0, Y(gate_v)), (ox0 + pw, Y(gate_v))], fill=col, width=2)
        d1.text((ox0 + pw - 240, Y(gate_v) - 26), lab, font=F(18), fill=col)
    for v, fn, col in (("@518 高清", "__da2s_fp16@518.npy", (30, 80, 220)),
                       ("@392", "__da2s_fp16@392.npy", (20, 140, 60)),
                       ("@336", "__da2s_fp16@336.npy", (230, 130, 20))):
        m = np.load(pcq.NPY / f"{pcq.frame_key(pth)}{fn}").astype(np.float32)
        gy = float(np.median(np.r_[m[600, 200:520], m[600, 760:1100]]))
        pts = []
        for o in range(-220, 220, 6):
            a, b = int(xg + o), int(xg + o + 6)
            a, b = max(a, 0), min(b, 1279)
            if b > a:
                pts.append((X(o), Y(float(np.median((m[600, a:b] - gy) / gy)))))
        d1.line(pts, fill=col, width=3, joint="curve")
        d1.text((pts[2][0], pts[2][1] - 30), v, font=F(20), fill=col)
    d1.text((ox0, oy0 + ph + 10), "← 墙（金标线左侧）      路面（应≈0）→", font=F(18), fill=(90, 90, 90))
    d1.text((ox0 + 8, oy0 + 44), "三条曲线形状相同=台阶都在；但橙/绿整体抬高\n⇒ 红色 2% 门在低分辨率下切到了『半山腰』甚至路面",
            font=F(18), fill=(60, 60, 60))
    img.paste(p1, (M, 64))

    # ── 面板 2：门刻度跟改 → 误差回位 ──
    p2 = Image.new("RGB", (W, H), "white")
    d2 = ImageDraw.Draw(p2)
    d2.text((M, 8), "② 门的刻度跟着分辨率改，误差立刻回到高清水平", font=F(22), fill=(0, 0, 0))
    bars = [("高清@518\n门 2%", _p90_curve2("firstgate_score.csv", "fp16@518"), (30, 80, 220)),
            ("@392\n门仍 2%", _p90_curve2("firstgate_score.csv", "fp16@392"), (220, 40, 40)),
            ("@392\n自洽门 4.2%", _p90_curve2("firstgate_score_selfgate.csv", "fp16@392"), (20, 140, 60)),
            ("@336\n自洽门 5.5%", _p90_curve2("firstgate_score_selfgate.csv", "fp16@336"), (20, 140, 60))]
    bx0, by0, bw, bh = 90, 90, 640, 360
    bmax = 420
    for i, (lab, v, col) in enumerate(bars):
        x = bx0 + i * (bw / 4) + 20
        h = v / bmax * bh
        d2.rectangle([x, by0 + bh - h, x + 90, by0 + bh], fill=col)
        d2.text((x - 4, by0 + bh - h - 30), f"{v:.0f}px", font=F(22), fill=col)
        d2.multiline_text((x - 12, by0 + bh + 12), lab, font=F(18), fill=(60, 60, 60), spacing=4)
    d2.text((bx0, by0 - 30), "边界误差（越小越好；柱上数字=像素）", font=F(18), fill=(90, 90, 90))
    img.paste(p2, (W + M * 2, 64))

    # ── 面板 3：双态序列 ──
    p3 = Image.new("RGB", (W, H), "white")
    d3 = ImageDraw.Draw(p3)
    d3.text((M, 8), "③ 剩下的问题：15 帧里 5 帧把『别的块』当成了边界（双态）", font=F(22), fill=(0, 0, 0))
    ox0, oy0, pw, ph = 90, 40, 660, 400
    def X2(i): return ox0 + i / 14 * pw
    def Y2(x): return oy0 + ph - x / 800 * ph
    d3.line([(ox0, oy0 + ph), (ox0 + pw, oy0 + ph)], fill=(120, 120, 120), width=2)
    for f in range(15):
        d3.text((X2(f) - 14, oy0 + ph + 8), str(714 + f), font=F(16), fill=(90, 90, 90))
    for v, col in (("fp16@518", (30, 80, 220)), ("fp16@392", None)):
        for f in range(15):
            r = _load_seg_csv("firstgate_score_selfgate.csv", v).get(("curve_cont2", f"{714+f:06d}.jpg", "l", "560"))
            if not r:
                continue
            x = float(r["region"])
            good = x < 500
            col = (30, 80, 220) if v == "fp16@518" else ((20, 140, 60) if good else (220, 40, 40))
            d3.ellipse([X2(f) - 7, Y2(x) - 7, X2(f) + 7, Y2(x) + 7], fill=col)
    d3.text((ox0 + 10, Y2(310) - 36), "高清@518（蓝）与锁对块的 @392（绿）：完全重叠的水平线", font=F(18), fill=(30, 80, 220))
    d3.text((ox0 + 150, Y2(690) - 36), "红：@392 有 5 帧把中央的近物粘进边界块", font=F(18), fill=(220, 40, 40))
    d3.text((ox0, oy0 + ph + 34), "帧号（连续 15 帧）→   纵轴 = 边界在画面里的横向位置", font=F(18), fill=(90, 90, 90))
    img.paste(p3, (M, 64 + H + M))

    # ── 面板 4：721 帧实景 ──
    p4 = Image.new("RGB", (W, H), "white")
    d4 = ImageDraw.Draw(p4)
    d4.text((M, 8), "④ 721 帧实景：红块的内沿被中央结构抢走，橙框=粘连的『桥』", font=F(22), fill=(0, 0, 0))
    p721 = [r for r in labels if "000721" in r["path"]][0]
    pth = Path(p721["path"])
    rgb = cv2.cvtColor(cv2.imread(str(pth)), cv2.COLOR_BGR2RGB)
    frame = Image.fromarray(rgb).resize((760, 428))
    d4r = ImageDraw.Draw(frame)
    m392 = np.load(pcq.NPY / f"{pcq.frame_key(pth)}__da2s_fp16@392.npy").astype(np.float32)
    m518 = np.load(pcq.NPY / f"{pcq.frame_key(pth)}__da2s_fp16@518.npy").astype(np.float32)
    G392 = json.loads((OUT / "firstgate_gate.json").read_text())["fp16@392"]
    bl392 = psd.region_inner(m392, psd.road_range(m392), G=G392)["L"][0]
    bl518 = psd.region_inner(m518, psd.road_range(m518))["L"][0]
    S = 760 / 1280
    def seg(drw, inner, col, w):
        pts = [(x * S, y * S) for y, x in sorted(inner.items()) if 340 <= y <= 714]
        drw.line(pts, fill=col, width=w)
    seg(d4r, bl392, (255, 40, 40), 6)
    seg(d4r, bl518, (40, 90, 255), 4)
    d4r.rectangle([561 * S, 528 * S, 700 * S, 572 * S], outline=(255, 150, 0), width=5)
    d4r.text((702 * S, 516 * S), "桥（把两块\n粘起来）", font=F(18), fill=(255, 150, 0))
    d4r.text((24, 396), "红线=@392 被拉走的块内沿  蓝线=高清真墙  白线=人工金标", font=F(17), fill=(255, 255, 255))
    xg721 = gold_x_at(p721, "l", 600)
    if xg721:
        nx, ny = float(p721["l_nx"]), float(p721["l_ny"])
        fx, fy = float(p721["l_fx"]), float(p721["l_fy"])
        d4r.line([(nx * S, ny * S), (fx * S, fy * S)], fill=(255, 255, 255), width=3)
    d4.text((W + M * 2 + 790, 90), "蓝线=高清档的边界（真墙）\n红线=@392 某帧选中的『块』内沿\n"
            "白线=人工金标\n橙框=把远带结构和真墙\n粘成一体的中央近物\n（读数放大后越门）",
            font=F(19), fill=(60, 60, 60))
    d4.text((W + M * 2, 64 + H + M - 34),
            "⇒ 修『挑块』的守卫（与档位无关的老毛病），不是修眼睛也不是修门", font=F(19), fill=(60, 60, 60))
    img.paste(frame, (W + M * 2, 64 + H + M + 6))
    out = OUT / "firstgate_plain.jpg"
    img.save(out, quality=92)
    print(f"[fig] 已写 {out}")


def _fig_steps(labels) -> None:
    """挑块四步流水线演示（721 帧 @392 自洽门）：涂色 → 去虚 → 粘块 → 挑块。
    中间产物为演示用重演（与 region_inner 同式），判据读数仍以 region_inner 为准。"""
    from PIL import Image, ImageDraw, ImageFont

    def F(sz):
        return ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", sz)

    r721 = [r for r in labels if "000721" in r["path"]][0]
    pth = Path(r721["path"])
    rgb = cv2.cvtColor(cv2.imread(str(pth)), cv2.COLOR_BGR2RGB)
    m = np.load(pcq.NPY / f"{pcq.frame_key(pth)}__da2s_fp16@392.npy").astype(np.float32)
    G = json.loads((OUT / "firstgate_gate.json").read_text())["fp16@392"]
    rng = psd.road_range(m)
    g = psd.ground_model(m, rng)
    rel = np.full(m.shape, np.nan, np.float32)
    rel[psd.Y0:psd.DIAG_Y1] = ((m[psd.Y0:psd.DIAG_Y1] - g[:, None])
                               / np.maximum(g[:, None], 1e-6))
    over = (np.nan_to_num(rel, nan=-1) > G).astype(np.uint8)
    em = psd.OUT / "ego_mask.npy"
    if em.exists():
        over[np.load(em).astype(bool)] = 0
    mask = (cv2.filter2D(over.astype(np.float32), -1, np.ones((1, 12), np.float32))
            >= 12).astype(np.uint8)
    ncc, lab = cv2.connectedComponents(mask, connectivity=8)[:2]

    # 每块 (行跨度, 触边) 表——谁被选中、为什么
    cands = []
    for c in range(1, ncc):
        ys, xs = np.nonzero(lab == c)
        touch = xs.min() <= 2
        if touch:
            cands.append((c, int(ys.min()), int(ys.max()), int(ys.max() - ys.min()),
                          float(np.median(xs)) if xs.size else 0))
    cands.sort(key=lambda t: -t[3])
    chosen = cands[0][0] if cands else None

    SW, SH = 620, 350
    S = SW / 1280
    tiles = []
    frame_small = Image.fromarray(rgb).resize((SW, int(720 * S)))
    ov_img = np.zeros((720, 1280, 3), np.uint8)
    ov_img[over > 0] = (255, 60, 60)
    ov_img[:psd.Y0] = (rgb[0] * 0 + np.array([40, 40, 40], np.uint8))[0]
    lab_img = np.zeros((720, 1280, 3), np.uint8)
    palette = [(60, 60, 200), (200, 60, 60), (60, 180, 60), (200, 160, 40),
               (150, 60, 200), (60, 180, 200), (200, 100, 160), (120, 120, 60)]
    for c in range(1, ncc):
        ys, xs = np.nonzero(lab == c)
        lab_img[ys, xs] = palette[c % len(palette)]
        if c == chosen:
            lab_img[ys, xs] = (255, 200, 0)
    for base, title, note in (
            (frame_small, "第 0 步：原始画面（721 帧）——真墙在左，中央的白车是自车",
             "白线=金标边界。注意：自车就是接下来的麻烦来源"),
            (Image.fromarray(cv2.addWeighted(rgb, 0.55, ov_img, 0.45, 0)).resize((SW, int(720 * S))),
             f"第 1 步·涂色：比路面高出 {G:.1%} 以上的像素涂红",
             "左墙涂红=真边界（对）。自车周围也涂红=车当然比路面近（也是真实读数）"),
            (Image.fromarray(cv2.addWeighted(rgb, 0.55,
             cv2.cvtColor(mask * 255, cv2.COLOR_GRAY2RGB), 0.45, 0)).resize((SW, int(720 * S))),
             "第 2 步·去虚：横向连续 12px 都超门才保留",
             "右侧远处的红块被滤掉（好）。但左墙和自车周围都留下、且连成一片"),
            (Image.fromarray(cv2.addWeighted(rgb, 0.5, lab_img, 0.5, 0)).resize((SW, int(720 * S))),
             "第 3+4 步·粘块+挑块：8 向连通成块，挑『摸到画面左缘、跨行最多』的块",
             "黄色=被选中的块=墙+自车粘连体。『挑最大』根本没得挑——粘连发生在第 3 步")):
        tile = Image.new("RGB", (SW, SH), "white")
        d = ImageDraw.Draw(tile)
        d.text((6, 4), title, font=F(17), fill=(0, 0, 0))
        tile.paste(base, (6, 34))
        d.text((6, 34 + int(720 * S) + 6), note, font=F(15), fill=(80, 80, 80))
        tiles.append(tile)

    img = Image.new("RGB", (SW * 2 + 30, (SH + 14) * 2 + 40), (250, 250, 248))
    img.paste(tiles[0], (10, 10))
    img.paste(tiles[1], (SW + 20, 10))
    img.paste(tiles[2], (10, SH + 24))
    img.paste(tiles[3], (SW + 20, SH + 24))
    dd = ImageDraw.Draw(img)
    sel = [c for c in cands if c[0] == chosen]
    if sel:
        dd.text((10, (SH + 14) * 2 + 22),
                f"触边候选只有 {len(cands)} 块（跨 {sel[0][3]} 行）：真墙与自车周围高区在第 3 步已粘死，『挑最大』没得挑。"
                "@518 上自车区读数低于 2% 门=隐形，跨档放大后显形——桥切断/跨帧连续性守卫是修法。",
                font=F(16), fill=(180, 40, 40))
    out = OUT / "firstgate_steps.jpg"
    img.save(out, quality=92)
    print(f"[fig_steps] 已写 {out}（触边候选块：{[(c[0], c[3]) for c in cands[:4]]}）")


def main() -> None:
    argv = sys.argv[1:]
    ap = argv[0] if argv else ""
    selfgate = "--selfgate" in argv
    labels = gold_rows()
    if ap == "infer":
        for v in VARIANTS:
            if VARIANTS[v][0] is None:
                continue
            for r in labels:
                p = Path(r["path"])
                rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
                load_variant_map(v, p, rgb)
            print(f"[infer] {v} 完成 {len(labels)} 帧")
        return

    if ap == "gate":
        # 自洽门标定：近带台阶读数比（配对同帧同侧，窗由金标线定位、不依赖
        # region 输出，无"用 A 拟合再用 A 评分"循环）→ G@档 = 2% × 比。
        # 只标 near 带（region 比较行带）；远带本底抬升门救不了，交本底守卫弃权。
        ref = "fp16@518"
        ref_con = {(r["frame"], r["side"]): r["contrast_out"]
                   for r in collect(ref, labels) if r.get("contrast_out") is not None}
        gates = {}
        for v in ("fp16@392", "fp16@336"):
            con = {(r["frame"], r["side"]): (r["contrast_out"], r["contrast_in"])
                   for r in collect(v, labels) if r.get("contrast_out") is not None}
            keys = sorted(set(con) & set(ref_con))
            ratios = np.array([con[k][0] / ref_con[k] for k in keys
                               if ref_con[k] and ref_con[k] > 1e-6])
            scale = float(np.median(ratios))
            G = round(0.02 * scale, 5)
            ins = np.array([con[k][1] for k in keys])
            flags = "本底>门半(守卫会弃权)" if np.median(ins) > G / 2 else "本底<门半"
            print(f"[gate] {v}: 台阶读数比中位 {scale:.2f}（n={len(keys)}）→ "
                  f"G={G:.4f}；本底中位 {np.median(ins):+.4f} vs 门半 {G / 2:.4f} → {flags}")
            gates[v] = G
        json.dump(gates, (OUT / "firstgate_gate.json").open("w"))
        print(f"[gate] 已写 {OUT / 'firstgate_gate.json'}")
        return

    if ap == "fig":
        _fig(labels)
        return
    if ap == "fig_steps":
        _fig_steps(labels)
        return

    gates = {}
    if selfgate:
        gj = OUT / "firstgate_gate.json"
        gates = json.loads(gj.read_text(encoding="utf-8")) if gj.exists() else {}
        print(f"[score] selfgate 口径：{gates}")

    allrecs: dict[str, list[dict]] = {}
    for v in VARIANTS:
        allrecs[v] = collect(v, labels, gate=gates.get(v))
        print(f"[score] {v}: {len(allrecs[v])} 样本")
    csv_name = "firstgate_score_selfgate.csv" if selfgate else "firstgate_score.csv"
    with (OUT / csv_name).open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=sorted({k for rs in allrecs.values() for r in rs for k in r}))
        wr.writeheader()
        for rs in allrecs.values():
            wr.writerows(rs)

    print("\n== P1 region 产出率（逐帧×侧）==")
    p1 = {v: {(r["frame"], r["side"]): r["p1"] for r in rs if "p1" in r}
          for v, rs in allrecs.items()}
    for v in VARIANTS:
        n = len(p1[v])
        k = sum(p1[v].values())
        print(f"{v:12s} {k:3d}/{n}（{k / max(n, 1):5.1%}）")
    for short in ("336", "518"):
        va, vb = f"fp16@{short}", f"q4f16@{short}"
        keys = sorted(set(p1[va]) & set(p1[vb]))
        b = sum(1 for k in keys if p1[va][k] == 0 and p1[vb][k] == 1)
        c = sum(1 for k in keys if p1[va][k] == 1 and p1[vb][k] == 0)
        print(f"McNemar {va} vs {vb}: 不一致对 b(仅{vb}成)={b} c(仅{va}成)={c}，"
              f"精确 p={mcnemar(b, c):.3f}")

    print("\n== dev 配对差值（q4f16 − fp16，同帧同侧同 y；正=q4f16 更靠外）==")
    for short in ("336", "518"):
        va, vb = f"fp16@{short}", f"q4f16@{short}"
        idx = {}
        for r in allrecs[vb]:
            if "dev" in r:
                idx[(r["frame"], r["side"], r["band"], r["y"])] = r["dev"]
        for band in ("near", "far"):
            d = np.array([idx[(r["frame"], r["side"], band, r["y"])] - r["dev"]
                          for r in allrecs[va]
                          if "dev" in r and r["band"] == band
                          and (r["frame"], r["side"], band, r["y"]) in idx])
            if d.size:
                lo, hi = boot_ci(d)
                print(f"@{short} {band:4s} n={d.size:3d}  中位 {np.median(d):+6.1f}px  "
                      f"bootstrap95% [{lo:+.1f}, {hi:+.1f}]")

    print("\n== near 带 dev 分层（含常数校正口径）==")
    for v in VARIANTS:
        rs = [r for r in allrecs[v] if "dev" in r and r["band"] == "near"]
        line = [f"{v:12s}"]
        for st in sorted({r["stratum"] for r in rs}):
            d = np.array([r["dev"] for r in rs if r["stratum"] == st])
            med = float(np.median(d))
            line.append(f"{st} n={len(d)} {med:+.0f}/校正后p90 "
                        f"{np.percentile(np.abs(d - med), 90):.0f}px")
        print("  " + "  |  ".join(line))

    print("\n== 台阶对比度（信号窗=外侧 20~60px，本底窗=内侧 40~120px）==")
    print(f"{'变体':12s} {'信号中位':>9s} {'p10':>7s} {'本底中位':>9s}  配对差(q4f16−fp16)")
    con = {v: {(r["frame"], r["side"]): r for r in rs if r.get("contrast_out") is not None}
           for v, rs in allrecs.items()}
    for v in VARIANTS:
        vals = np.array([r["contrast_out"] for r in con[v].values()])
        ins = np.array([r["contrast_in"] for r in con[v].values()])
        print(f"{v:12s} {np.median(vals):+9.4f} {np.percentile(vals, 10):+7.4f} "
              f"{np.median(ins):+9.4f}", end="")
        short = v.split("@")[1]
        vb = f"q4f16@{short}" if v.startswith("fp16@") else None
        if vb and con.get(vb):
            keys = sorted(set(con[v]) & set(con[vb]))
            d = np.array([con[vb][k]["contrast_out"] - con[v][k]["contrast_out"] for k in keys])
            print(f"   配对差中位 {np.median(d):+.4f}（n={d.size}）", end="")
        print()

    print("\n== 时序抖动（连续段：覆盖最全的 侧×行 内沿 x；1 阶含运动，2 阶才是抖动）==")
    from collections import Counter
    for v in VARIANTS:
        for st, f0, f1 in (("curve_cont", 906, 920), ("curve_cont2", 714, 728)):
            seg = [r for r in allrecs[v] if r["stratum"] == st and "region" in r
                   and f0 <= int(r["frame"][:-4].split("_")[-1]) <= f1]
            cnt = Counter((r["side"], r["y"]) for r in seg)
            if not cnt:
                continue
            (side, y), k = cnt.most_common(1)[0]
            seq = {}
            for r in seg:
                if (r["side"], r["y"]) != (side, y):
                    continue
                fid = int(r["frame"][:-4].split("_")[-1])
                seq[fid] = r["region"]
            xs = np.array([seq[f] for f in sorted(seq)], float)
            if xs.size >= 4:
                d1, d2 = np.diff(xs), np.diff(np.diff(xs))
                print(f"{v:12s} {st:12s} {side}/{y} n={xs.size:2d}  "
                      f"1阶std {np.std(d1):5.1f}px  2阶std {np.std(d2):5.1f}px")


if __name__ == "__main__":
    main()
