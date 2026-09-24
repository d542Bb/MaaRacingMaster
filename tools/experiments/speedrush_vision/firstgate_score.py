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


def _row_line_fit(m: np.ndarray, y: int, rng: float) -> tuple[float, float, np.ndarray]:
    """单行路面视差的行内线性拟合（透视修正演示）：剔自车列带；
    迭代 = 粗中位先剔 |rel|>6% 的墙/车（25% 量程剔不掉它们——墙偏离 <25% 量程），
    再行内线性拟合 + 25% 量程收尾。→ (a, b, 参与像素掩码)"""
    xs = np.r_[np.arange(0, psd.EGO[0]), np.arange(psd.EGO[1], 1280)]
    v = m[y, xs]
    med0 = float(np.median(v))
    keep = np.abs(v - med0) < 0.06 * max(med0, 1e-6)
    if keep.sum() < 0.3 * xs.size:      # 粗门剔过头（大面积非路面行）则放宽
        keep = np.ones(xs.size, bool)
    a = b = 0.0
    for _ in range(2):
        cf = np.polyfit(xs[keep], v[keep], 1)
        a, b = float(cf[1]), float(cf[0])
        res = np.abs(v - (a + b * xs))
        keep = keep & (res < 0.25 * rng)
    return a, b, keep


def _fig_ground(labels) -> None:
    """路面判定可视化：g(y) 行内常数模型 vs 行内线性（透视修正）。
    ① 行内视差剖面：同帧 @518 平 vs @392 斜（弯道透视+放大）
    ② 行内梯度的帧间稳定性（弯道 vs 直道）
    ③ 车辆剔除与稳健化（参与中位的像素）
    ④ 修正预演：行内线性拟合后路面本底回零"""
    from PIL import Image, ImageDraw, ImageFont

    def F(sz):
        return ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", sz)

    W, H, M = 800, 560, 16
    img = Image.new("RGB", (W * 2 + M * 3, H * 2 + 76 + M * 2), (250, 250, 248))
    dr = ImageDraw.Draw(img)
    dr.text((M, 12), "路面判定（g）解剖：行内常数模型漏了弯道透视，低分辨率把它放大 2~4 倍",
            font=F(24), fill=(30, 30, 30))

    r721 = [r for r in labels if "000721" in r["path"]][0]
    pth = Path(r721["path"])
    ms = {tag: np.load(pcq.NPY / f"{pcq.frame_key(pth)}{fn}").astype(np.float32)
          for tag, fn in (("@518", "__da2s_fp16@518.npy"), ("@392", "__da2s_fp16@392.npy"))}
    y = 560

    # ── 面板 A：行内视差剖面 ──
    p1 = Image.new("RGB", (W, H), "white")
    d1 = ImageDraw.Draw(p1)
    d1.text((M, 8), "① 同一帧 y=560 行的路面视差剖面：高清平（直/缓弯），@392 斜", font=F(21), fill=(0, 0, 0))
    ox0, oy0, pw, ph = 90, 46, 650, 380
    vmin, vmax = 3.8, 7.2
    def X(x): return ox0 + x / 1279 * pw
    def Y(v): return oy0 + ph - (v - vmin) / (vmax - vmin) * ph
    d1.line([(ox0, oy0 + ph), (ox0 + pw, oy0 + ph)], fill=(150, 150, 150), width=2)
    d1.line([(ox0, oy0), (ox0, oy0 + ph)], fill=(150, 150, 150), width=2)
    for tag, col in (("@518", (30, 80, 220)), ("@392", (20, 140, 60))):
        m = ms[tag]
        pts = [(X(x), Y(float(np.median(m[y, max(x - 10, 0):x + 10]))))
               for x in range(200, 1279, 24)]
        d1.line(pts, fill=col, width=3, joint="curve")
        a, b, _ = _row_line_fit(m, y, psd.road_range(m))
        gx = [x for x in range(200, 1279, 24)]
        d1.line([(X(x), Y(a + b * x)) for x in gx], fill=col, width=1)
        d1.text((pts[-1][0] - 150, pts[-1][1] - 26), f"{tag}（斜率 {b * 1000:+.1f}px/千列）",
                font=F(19), fill=col)
    d4r = d1
    d1.text((X(560) - 40, oy0 + ph + 8), "← 自车列带（g 剔除）→", font=F(16), fill=(200, 120, 0))
    d1.line([(X(540), oy0 + ph + 2), (X(540), oy0 + ph + 8)], fill=(200, 120, 0), width=2)
    d1.line([(X(740), oy0 + ph + 2), (X(740), oy0 + ph + 8)], fill=(200, 120, 0), width=2)
    d1.text((ox0 + 8, oy0 + 4), "路面视差（视差大=近）", font=F(17), fill=(90, 90, 90))
    d1.text((M, 470), "两条曲线都是『路面本身』：@518 几乎平（行内 ±1%）；@392 左高右低差 ~5%。\n"
            "同帧同场景 ⇒ 不是场景几何，是低分辨率模型把行内梯度放大（直道无此现象）。",
            font=F(18), fill=(60, 60, 60))
    img.paste(p1, (M, 64))

    # ── 面板 B：梯度稳定性 ──
    p2 = Image.new("RGB", (W, H), "white")
    d2 = ImageDraw.Draw(p2)
    d2.text((M, 8), "② 行内梯度（左端−右端，相对 g）：弯道帧间极稳 ⇒ 可拟合修正", font=F(21), fill=(0, 0, 0))
    ox0, oy0, pw, ph = 90, 46, 650, 380
    vmin2, vmax2 = -0.04, 0.12
    def Y2(v): return oy0 + ph - (min(max(v, vmin2), vmax2) - vmin2) / (vmax2 - vmin2) * ph
    d2.line([(ox0, Y2(0)), (ox0 + pw, Y2(0))], fill=(120, 120, 120), width=1)
    d2.line([(ox0, Y2(0.02)), (ox0 + pw, Y2(0.02))], fill=(220, 40, 40), width=2)
    d2.text((ox0 + pw - 210, Y2(0.02) - 24), "2% 门（原高清门）", font=F(17), fill=(220, 40, 40))
    seqs = {"@518": [], "@392": [], "@336": []}
    for f in range(714, 729):
        k = [r["path"] for r in labels if r["path"].endswith(f"{f:06d}.jpg")]
        if not k:
            continue
        for tag, fn in (("@518", "__da2s_fp16@518.npy"), ("@392", "__da2s_fp16@392.npy"),
                        ("@336", "__da2s_fp16@336.npy")):
            m = np.load(pcq.NPY / f"{pcq.frame_key(Path(k[0]))}{fn}").astype(np.float32)
            L = np.median(m[y, 200:540]); R = np.median(m[y, 900:1279])
            seqs[tag].append(((L - R) / np.median(m[y, 200:1279])))
    for tag, col, dx in (("@518", (30, 80, 220), -10), ("@392", (20, 140, 60), 0),
                         ("@336", (230, 130, 20), 10)):
        for i, v in enumerate(seqs[tag]):
            cx = ox0 + (i + 0.5) / 15 * pw + dx
            d2.ellipse([cx - 5, Y2(v) - 5, cx + 5, Y2(v) + 5], fill=col)
        d2.text((ox0 + pw + 4, Y2(seqs[tag][-1]) - 8), tag, font=F(18), fill=col)
    d2.text((ox0, 470), "弯道段（714~728）y=560：@518 +1.5~2.2%（模型先验漂移，非相机几何——相机恒不转头）；"
            "@392 +3.8~4.8%、\n@336 +7.4~8.1%（低分辨率放大 2~4 倍）。帧间变化 <±0.5% ⇒ 行内线性拟合可吸收。",
            font=F(18), fill=(60, 60, 60))
    img.paste(p2, (W + M * 2, 64))

    # ── 面板 C：车辆剔除与稳健化 ──
    p3 = Image.new("RGB", (W, H), "white")
    d3 = ImageDraw.Draw(p3)
    d3.text((M, 8), "③ 车辆剔除：g 只用自车列带以外的像素 + 两轮 25% 量程稳健化", font=F(21), fill=(0, 0, 0))
    rgb = cv2.cvtColor(cv2.imread(str(pth)), cv2.COLOR_BGR2RGB)
    m = ms["@518"]
    rng = psd.road_range(m)
    a, b, keep = _row_line_fit(m, y, rng)
    frame = Image.fromarray(rgb).resize((760, 428))
    d3r = ImageDraw.Draw(frame)
    S3 = 760 / 1280
    xs_all = np.r_[np.arange(0, psd.EGO[0]), np.arange(psd.EGO[1], 1280)]
    for x, kp in zip(xs_all, keep):
        col = (60, 200, 60) if kp else (200, 200, 60)
        d3r.line([(x * S3, y * S3), (x * S3, (y + 6) * S3)], fill=col, width=3)
    d3r.line([(0, y * S3), (1279 * S3, y * S3)], fill=(255, 255, 255), width=1)
    d3r.rectangle([psd.EGO[0] * S3, 0, psd.EGO[1] * S3, 428], outline=(200, 120, 0), width=2)
    d3r.text((psd.EGO[0] * S3 + 6, 8), "自车列带（g 不用）", font=F(16), fill=(255, 200, 80))
    d3.text((M, 492), "绿=参与拟合的像素，黄=被 25% 量程稳健化剔除（离群：车/墙/天空侧）。他车偏离 >25% 量程即被剔；\n"
            "占行宽 <50% 时中位数本身也稳健，贴身占宽 >50% 才会污染（g 是一切涂色的分母，它错则全错）。",
            font=F(17), fill=(60, 60, 60))
    p3.paste(frame, (M + 20, 46 + 8))
    img.paste(p3, (M, 64 + H + M))

    # ── 面板 D：修正预演 ──
    p4 = Image.new("RGB", (W, H), "white")
    d4 = ImageDraw.Draw(p4)
    d4.text((M, 8), "④ 修正预演：行内基线修正（@392，y=560）——线性不够，滑动低分位近带有效", font=F(21), fill=(0, 0, 0))
    m = ms["@392"]
    gy = float(np.median(m[y, 200:1279]))
    a, b, _ = _row_line_fit(m, y, psd.road_range(m))
    xs = range(330, 1051, 12)
    ox0, oy0, pw, ph = 90, 46, 650, 380
    vmin4, vmax4 = -0.06, 0.07
    def X4(x): return ox0 + (x - 330) / (1050 - 330) * pw
    def Y4(v): return oy0 + ph - (min(max(v, vmin4), vmax4) - vmin4) / (vmax4 - vmin4) * ph
    d4.line([(ox0, Y4(0)), (ox0 + pw, Y4(0))], fill=(120, 120, 120), width=1)
    d4.line([(ox0, Y4(0.02)), (ox0 + pw, Y4(0.02))], fill=(220, 40, 40), width=1)
    d4.line([(ox0, Y4(0.0423)), (ox0 + pw, Y4(0.0423))], fill=(220, 40, 40), width=2)
    d4.text((ox0 + pw - 230, Y4(0.0423) - 24), "自洽门 4.23%", font=F(17), fill=(220, 40, 40))
    pts_med = [(X4(x), Y4((float(np.median(m[y, x:x + 12])) - gy) / gy)) for x in xs]
    pts_lin = [(X4(x), Y4((float(np.median(m[y, x:x + 12])) - (a + b * x)) / (a + b * x))) for x in xs]
    d4.line(pts_med, fill=(220, 40, 40), width=3, joint="curve")
    d4.line(pts_lin, fill=(20, 140, 60), width=3, joint="curve")
    d4.text((pts_med[2][0], pts_med[2][1] - 26), "行中位 g（现状）：路面本底 ±2% 斜坡", font=F(18), fill=(220, 40, 40))
    d4.text((pts_lin[2][0], pts_lin[2][1] + 10), "行内线性 g(y,x)：剖面是曲线，直线只贴一头（350~700 仍超门）",
            font=F(18), fill=(20, 140, 60))
    d4.text((M, 470), "红=现状（行中位）：路面本底 ±2% 斜坡。绿=行内线性：不够——@392 行内剖面是曲线。\n"
            "滑动低分位基线（实测）：近带有效（右路面 −2.0%→+0.7%、左墙 +12% 仍超门），\n"
            "远带失效（路面占行宽不足，低分位被背景拖走 +3.8%）⇒ 修近带、远带交本底守卫弃权。",
            font=F(17), fill=(60, 60, 60))
    img.paste(p4, (W + M * 2, 64 + H + M))

    out = OUT / "firstgate_ground.jpg"
    img.save(out, quality=92)
    print(f"[fig_ground] 已写 {out}")


def _g_rows(m: np.ndarray, cols, rng: float) -> np.ndarray:
    """ground_model 的列掩码参数化版（同一两轮 25% 稳健化，不抄第二份逻辑）。"""
    p = m[psd.Y0:psd.DIAG_Y1][:, cols].astype(np.float32)
    med = np.nanmedian(p, axis=1)
    for _ in range(2):
        dev = np.abs(p - med[:, None])
        med = np.nanmedian(np.where(dev < 0.25 * rng, p, np.nan), axis=1)
    return med


def _car_span(rel: np.ndarray, gate: float = 0.05) -> tuple[int, int] | None:
    """该行含画面中心（600~680 任一命中）的连续 rel>gate 区段 = 车身轮廓。

    车身读数显著高于路面（自车守卫 r>0.25 的派生依据），路面时间噪声 σ≈0.4%，
    门取 5% 远离两者。无命中行返回 None（车顶反光弱等），调用方跳过。
    """
    on = rel > gate
    if not on[600:681].any():
        return None
    c0 = 600 + int(np.argmax(on[600:681]))
    a = c0
    while a > 0 and on[a - 1]:
        a -= 1
    b = c0
    while b < rel.shape[0] - 1 and on[b + 1]:
        b += 1
    return a, b


def _runs(ymrow) -> list:
    """一行 HSV 掩码里的连续命中区段 [[a,b],...]（间隙 >4px 断开）。"""
    idx = np.where(ymrow > 0)[0]
    if len(idx) == 0:
        return []
    runs, s, prev = [], idx[0], idx[0]
    for x in idx[1:]:
        if x - prev > 4:
            runs.append([int(s), int(prev)])
            s = x
        prev = x
    runs.append([int(s), int(prev)])
    return runs


def _ego_scan(labels) -> None:
    """自车列带 (540,740) 体检（维护者质疑：固定竖条剔车，换车/贴边怎么办）。

    实测前提（先于一切结论）：自车在 @518 深度里的足迹 = 上半身高区
    y[351,532] x[512,765]（ego_mask 即其数据派生），下半身 y≥560 与路面齐平。
    因此本扫描量三件事：
    ① g 敏感性：现状剔带 / 全列不剔（车全泄漏的最坏界）/ 全列纯中位（隔离
       25% 稳健化的贡献）/ 剔带±60（带过宽的成本），行覆盖远带 440~520（高区
       泄漏的真正考验）与近带 560~700。判读尺度：门 G@518=2.0%（gate 标定只给 @392=4.23%/@336=5.52%，@518 为参照门）。
    ② 高区足迹剖面：中央带 rel 的跨帧 p95 包络随 y 的走向（=车在深度里的形状）。
    ③ wallhug 6 帧：金标线 y=600/688 与 [列带 ∪ mask 实测范围] 的相对位置——
       追车相机恒置中央，贴边时线是被挤到画面边（C 类单侧）还是被车身遮死。
    """
    rows_y = (440, 480, 520, 560, 600, 640, 700)
    em = np.load(OUT / "ego_mask.npy").astype(bool) if (OUT / "ego_mask.npy").exists() else None
    em_x = (int(np.where(em.any(0))[0].min()), int(np.where(em.any(0))[0].max())) if em is not None else None
    recs = []
    for r in labels:
        p = Path(r["path"])
        m = np.load(pcq.NPY / f"{pcq.frame_key(p)}__da2s.npy").astype(np.float32)
        rng = psd.road_range(m)
        g = _g_rows(m, psd.cols_of(m), rng)
        g_all = _g_rows(m, np.arange(m.shape[1]), rng)
        g_wide = _g_rows(m, np.r_[0:psd.EGO[0] - 60, psd.EGO[1] + 60:m.shape[1]], rng)
        rec = {"frame": p.name, "stratum": r["stratum"], "obstacle": r["obstacle"]}
        for y in rows_y:
            a = float(g[y - psd.Y0])
            rec[f"dB{y}"] = round((float(g_all[y - psd.Y0]) - a) / a, 5)
            rec[f"dC{y}"] = round((float(np.median(m[y])) - a) / a, 5)
            rec[f"dD{y}"] = round((float(g_wide[y - psd.Y0]) - a) / a, 5)
        recs.append(rec)

    print(f"== ① g 敏感性（Δg/g，%；门 G@518=2.0%；n={len(recs)} 帧）==")
    print("行   |     B 全列不剔(车全泄)     |     C 全列纯中位      |     D 剔带±60")
    for y in rows_y:
        cells = []
        for t in ("B", "C", "D"):
            v = np.array([rec[f"d{t}{y}"] for rec in recs]) * 100
            cells.append(f"中位{np.median(v):+6.2f} p95|Δ|{np.percentile(np.abs(v), 95):5.2f}")
        print(f"{y}  |  {cells[0]}  |  {cells[1]}  |  {cells[2]}")

    print("\n== ② 高区足迹（中央带 x[450,830] rel 的跨帧包络，30 帧样本）==")
    prof = {}
    for y in range(360, 711, 25):
        vals = []
        for r in labels[:30]:
            pth = Path(r["path"])
            m = np.load(pcq.NPY / f"{pcq.frame_key(pth)}__da2s.npy").astype(np.float32)
            g = _g_rows(m, psd.cols_of(m), psd.road_range(m))
            rel = (m[y] - g[y - psd.Y0]) / np.maximum(g[y - psd.Y0], 1e-6)
            vals.append(float(np.percentile(rel[450:831], 95)))
        prof[y] = (float(np.median(vals)), float(np.percentile(vals, 90)))
        print(f"  y={y}: 跨帧p95中位 {prof[y][0]*100:+5.1f}%   90分位 {prof[y][1]*100:+5.1f}%")
    print(f"  ego_mask 实测范围: x{em_x}（列带 540–740 之外各漏 ~25px）")

    print("\n== ③ wallhug 帧：金标线 vs 车身（列带∪mask）==")
    lo = min(psd.EGO[0], em_x[0]) - 20
    hi = max(psd.EGO[1], em_x[1]) + 20
    for rec, lab in zip(recs, labels):
        if rec["stratum"] != "wallhug":
            continue
        p = Path(lab["path"])
        cells = []
        for side in ("l", "r"):
            for y in (600, 688):
                gx = gold_x_at(lab, side, y)
                if gx is not None:
                    inside = lo <= gx <= hi
                    cells.append(f"{side}@{y}: gold={gx:.0f}{'·在车身带内!' if inside else ''}")
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        ym = cv2.inRange(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV), pd.HSV_LO, pd.HSV_HI)
        print(f"{p.name}  {'  '.join(cells)}  黄线@600={_runs(ym[600])}")

    with (OUT / "firstgate_ego.csv").open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(recs[0]))
        wr.writeheader()
        wr.writerows(recs)
    print(f"\n[ego] 已写 {OUT / 'firstgate_ego.csv'}")
    _fig_ego(labels, recs, prof, em_x)


def _fig_ego(labels, recs, prof, em_x) -> None:
    """自车剔除体检四联图：①②帧实拍（列带 vs mask 实测足迹）③高区剖面 ④Δg 扰动。"""
    from PIL import Image, ImageDraw, ImageFont

    def F(sz):
        return ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", sz)

    W, H, M = 800, 560, 16
    img = Image.new("RGB", (W * 2 + M * 3, H * 2 + 76 + M * 2), (250, 250, 248))
    dr = ImageDraw.Draw(img)
    dr.text((M, 12), "自车剔除体检：车在深度里只有上半身高区（ego_mask 管它），g 的固定列带几近无关",
            font=F(23), fill=(30, 30, 30))
    S = 760 / 1280

    def overlay(lab, note):
        """帧实拍 + 橙框=固定列带 + 红框=ego_mask 实测范围 + 白线=金标。"""
        pth = Path(lab["path"])
        rgb = cv2.cvtColor(cv2.imread(str(pth)), cv2.COLOR_BGR2RGB)
        frame = Image.fromarray(rgb).resize((760, 428))
        d = ImageDraw.Draw(frame)
        d.rectangle([psd.EGO[0] * S, 0, psd.EGO[1] * S, 428], outline=(230, 140, 20), width=3)
        d.rectangle([em_x[0] * S, 351 * S, em_x[1] * S, 532 * S], outline=(230, 30, 30), width=3)
        for side in ("l", "r"):
            if lab[side + "cls"] != "skip":
                d.line([(float(lab[side + "_nx"]) * S, float(lab[side + "_ny"]) * S),
                        (float(lab[side + "_fx"]) * S, float(lab[side + "_fy"]) * S)],
                       fill=(255, 255, 255), width=2)
        d.text((8, 8), note, font=F(17), fill=(255, 255, 255),
               stroke_width=2, stroke_fill=(0, 0, 0))
        return frame

    # ── 面板 A：普通帧 ──
    p1 = Image.new("RGB", (W, H), "white")
    d1 = ImageDraw.Draw(p1)
    d1.text((M, 8), "① 车在深度里的真实足迹：红=ego_mask 高区 y[351,532]（数据派生）", font=F(20), fill=(0, 0, 0))
    la = labels[0]
    fr = overlay(la, Path(la["path"]).name + f"（{la['stratum']}）")
    p1.paste(fr, (M + 20, 40))
    d1.text((M, 40 + 428 + 6), f"橙=固定列带 540–740（只影响 g/rng 统计）；红框=高区实测 x{em_x}，比列带宽 ~25px/侧。\n"
            "下半身（y≥560）读数与路面齐平（±1.4%）——深度里车的下半身『不存在』。",
            font=F(16), fill=(60, 60, 60))
    img.paste(p1, (M, 64))

    # ── 面板 B：wallhug 帧 ──
    p2 = Image.new("RGB", (W, H), "white")
    d2 = ImageDraw.Draw(p2)
    d2.text((M, 8), "② wallhug（贴边）帧：线被挤向画面边，不是被车身遮死", font=F(20), fill=(0, 0, 0))
    lb = [l for l in labels if l["stratum"] == "wallhug"][3]
    fr = overlay(lb, Path(lb["path"]).name + "（wallhug）")
    d = ImageDraw.Draw(fr)
    rgb_b = cv2.cvtColor(cv2.imread(str(Path(lb["path"]))), cv2.COLOR_BGR2RGB)
    ym = cv2.inRange(cv2.cvtColor(rgb_b, cv2.COLOR_RGB2HSV), pd.HSV_LO, pd.HSV_HI)
    for a, b in _runs(ym[600]):
        d.line([(a * S, 600 * S), (b * S, 600 * S)], fill=(255, 0, 255), width=4)
    p2.paste(fr, (M + 20, 40))
    d2.text((M, 40 + 428 + 6), "白=金标线，品红=HSV 黄线@600。追车相机恒置中央 ⇒ 贴边时线被压到画面边缘（单侧消失=C 类），\n"
            "车身永远在屏幕中央，不构成遮挡。", font=F(16), fill=(60, 60, 60))
    img.paste(p2, (W + M * 2, 64))

    # ── 面板 C：高区足迹剖面 ──
    p3 = Image.new("RGB", (W, H), "white")
    d3 = ImageDraw.Draw(p3)
    d3.text((M, 8), "③ 自车在深度里的剖面（中央带 rel 跨帧 p95 包络，30 帧样本）", font=F(20), fill=(0, 0, 0))
    ox0, oy0, pw, ph = 90, 40, 620, 400
    ys = sorted(prof)
    vmin, vmax = -0.50, 0.30
    def Y(v): return oy0 + ph - (min(max(v, vmin), vmax) - vmin) / (vmax - vmin) * ph
    def YX(y): return ox0 + (y - 340) / (720 - 340) * pw
    d3.line([(YX(560), oy0), (YX(560), oy0 + ph)], fill=(150, 150, 150), width=1)
    d3.text((YX(560) + 4, oy0 + 4), "近带 560~", font=F(15), fill=(120, 120, 120))
    pts_m = [(YX(y), Y(prof[y][0])) for y in ys]
    pts_w = [(YX(y), Y(prof[y][1])) for y in ys]
    d3.line(pts_w, fill=(230, 150, 60), width=2)
    d3.line(pts_m, fill=(200, 40, 40), width=3, joint="curve")
    d3.line([(ox0, Y(0.0276)), (ox0 + pw, Y(0.0276))], fill=(220, 40, 40), width=1)
    pk = max(ys, key=lambda y: prof[y][0])
    d3.text((YX(pk) + 8, oy0 + 2), f"峰 {prof[pk][0]*100:+.0f}%（超出图）", font=F(15), fill=(200, 40, 40))
    d3.text((YX(645) + 6, Y(0.0276) - 22), "近带读数 ~0.5% ≈ 门的 1/4", font=F(15), fill=(200, 40, 40))
    d3.text((M, 492), "上半身（y≈385~510）读数 +25%~+144% ⇒ ego_mask 挖掉的就是它（防『车→护栏→墙』合并桥）；\n"
            "y≥560 与路面齐平（红线=门 2.0%）：下半身『不存在』，贴边也不产生伪边界。",
            font=F(16), fill=(60, 60, 60))
    img.paste(p3, (M, 64 + H + M))

    # ── 面板 D：Δg 扰动柱状 ──
    p4 = Image.new("RGB", (W, H), "white")
    d4 = ImageDraw.Draw(p4)
    d4.text((M, 8), "④ 若把车放进 g 的统计里，g 会动多少？（Δg/g，%）", font=F(20), fill=(0, 0, 0))
    ox0, oy0, pw, ph = 90, 46, 620, 380
    vmin4, vmax4 = -1.0, 8.0
    def Y4(v): return oy0 + ph - (min(max(v, vmin4), vmax4) - vmin4) / (vmax4 - vmin4) * ph
    d4.line([(ox0, Y4(0)), (ox0 + pw, Y4(0))], fill=(120, 120, 120), width=1)
    d4.line([(ox0, Y4(2.0)), (ox0 + pw, Y4(2.0))], fill=(220, 40, 40), width=2)
    d4.text((ox0 + pw - 160, Y4(2.0) - 22), "门 G@518=2.0%", font=F(15), fill=(220, 40, 40))
    bw = 30
    for gi, y in enumerate((440, 480, 520, 560, 600, 640, 700)):
        cx = ox0 + (gi + 0.5) / 7 * pw
        for bi, (t, col) in enumerate((("B", (230, 140, 20)), ("D", (20, 140, 170)))):
            v = np.array([r[f"d{t}{y}"] for r in recs]) * 100
            med, p5, p95 = float(np.median(v)), float(np.percentile(v, 5)), float(np.percentile(v, 95))
            bx = cx - bw / 2 + bi * bw - 4
            y0, y1 = sorted((Y4(0), Y4(med)))
            d4.rectangle([bx - bw / 2 + 10, y0, bx + bw / 2 + 10, y1], fill=col)
            d4.line([(bx + 10, Y4(p5)), (bx + 10, Y4(p95))], fill=(60, 60, 60), width=2)
        d4.text((cx - 16, oy0 + ph + 6), f"y={y}", font=F(15), fill=(60, 60, 60))
    d4.text((M, 492), "橙=B 全列不剔（车 100% 泄漏最坏界）、青=D 剔带±60；灰须=p5~p95。近带 560~700 全变体 |Δg|≤0.3%；\n"
            "远带 y=440 须线尖峰（p95 7.2%，最大 +17.8%）全来自他车帧——纯自车帧仅 0.14%。",
            font=F(16), fill=(60, 60, 60))
    img.paste(p4, (W + M * 2, 64 + H + M))

    out = OUT / "firstgate_ego.jpg"
    img.save(out, quality=92)
    print(f"[fig_ego] 已写 {out}")


def _wallhit(args) -> None:
    """撞墙/贴边实景三链取证（维护者给帧发问：「撞在墙上还能搞清楚吗」）。

    帧 → ① 生产 detect_boundary + _EgoRoadObserver 窗口逐帧重放（sides/Lx/Rx/
    road_offset）② 目标帧 HSV 黄线 run 与左右缘读数落点 ③ 深度 @518
    region_inner 兜底块（真边界 / 遮挡窄条 / 他车伪结构的区分）。出图
    firstgate_wallhit.jpg：品红=HSV run、白圈=黄线层左右缘读数、青/橙=深度
    region 右/左块内沿。"""
    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from maaracing_master.plugins.speedrush.boundary import detect_boundary
    from maaracing_master.plugins.speedrush.module import _EgoRoadObserver

    specs = [(s.split(":")[0], int(s.split(":")[1]))
             for s in args.spec.split(",") if s.strip()]
    demos = pd.APP / "demos"
    out_rows = []
    for sess_name, fid0 in specs:
        frames = demos / sess_name / "frames"
        obs = _EgoRoadObserver()
        win = []
        for fid in range(fid0 - 15, fid0 + 16):
            p = frames / f"{fid:06d}.jpg"
            if not p.exists():
                continue
            rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
            b = detect_boundary(rgb)
            off = obs.update(b)
            win.append({"fid": fid, "sides": b.sides, "valid": int(b.validity),
                        "Lx": b.left_x, "Rx": b.right_x, "off": off})
        n_none = sum(1 for w in win if w["off"] is None)
        print(f"== {sess_name} fid{fid0} 窗口 ±15：road_offset None {n_none}/{len(win)} ==")
        for w in win:
            if abs(w["fid"] - fid0) <= 6:
                print(f"  fid{w['fid']} sides={w['sides']} Lx={w['Lx']} Rx={w['Rx']} off={w['off']}")
        # 深度 region 兜底
        p0 = frames / f"{fid0:06d}.jpg"
        key = pcq.frame_key(p0)
        cache = pcq.NPY / f"{key}__da2s.npy"
        rgb0 = cv2.cvtColor(cv2.imread(str(p0)), cv2.COLOR_BGR2RGB)
        if cache.exists():
            m = np.load(cache).astype(np.float32)
        else:
            sess = psd._folded_sess(518)
            m = pd.depth_map(sess, rgb0, 518)
            pcq.NPY.mkdir(parents=True, exist_ok=True)
            np.save(cache, m.astype(np.float16))
        blobs = psd.region_inner(m, psd.road_range(m), G=0.02)
        hsv_row = cv2.inRange(cv2.cvtColor(rgb0, cv2.COLOR_RGB2HSV), pd.HSV_LO, pd.HSV_HI)
        b0 = detect_boundary(rgb0)
        out_rows.append({"sess": sess_name, "fid": fid0, "win": win, "blobs": blobs,
                         "rgb": rgb0, "ym": hsv_row, "bnd": b0})
        fmt = lambda blk: "无" if blk is None else f"y[{blk[1]},{blk[2]}]"
        print(f"  深度 region：L={fmt(blobs['L'])}  R={fmt(blobs['R'])}")

    _fig_wallhit(out_rows)


def _fig_wallhit(rows) -> None:
    from PIL import Image, ImageDraw, ImageFont

    def F(sz):
        return ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", sz)

    S = 760 / 1280
    pw, ph, cap, M = 760, 428, 108, 16
    img = Image.new("RGB", (len(rows) * (pw + M) + M, ph + cap + 64 + M * 2), (250, 250, 248))
    dr = ImageDraw.Draw(img)
    dr.text((M, 12), "撞墙/贴边实景取证：黄线层读数逐帧乱跳被物理护栏拒掉（弃权≠受骗），深度兜底现状如实画",
            font=F(22), fill=(30, 30, 30))
    for i, r in enumerate(rows):
        fr = Image.fromarray(r["rgb"]).resize((pw, ph))
        d = ImageDraw.Draw(fr)
        y = 600
        for a, b in _runs(r["ym"][y]):
            d.line([(a * S, y * S), (b * S, y * S)], fill=(255, 0, 255), width=4)
        for side, col in (("L", (255, 120, 30)), ("R", (0, 220, 220))):
            blk = r["blobs"][side]
            if blk is None:
                continue
            inner = blk[0]
            for yy in range(430, 701, 10):
                x = inner.get(yy)
                if x is not None:
                    d.ellipse([x * S - 3, yy * S - 3, x * S + 3, yy * S + 3], fill=col)
        for xx in (r["bnd"].left_x, r["bnd"].right_x):
            if xx == xx:
                d.ellipse([xx * S - 7, y * S - 7, xx * S + 7, y * S + 7], outline=(255, 255, 255), width=2)
        d.text((8, 8), f"{r['sess']} fid{r['fid']}", font=F(17), fill=(255, 255, 255),
               stroke_width=2, stroke_fill=(0, 0, 0))
        x0 = M + i * (pw + M)
        img.paste(fr, (x0, 56))
        d2 = ImageDraw.Draw(img)
        n_none = sum(1 for w in r["win"] if w["off"] is None)
        d2.text((x0, 56 + ph + 6),
                f"品红=HSV 黄线@600  白圈=黄线层左右缘读数（fid{r['fid']}："
                f"L={r['bnd'].left_x:.0f} R={r['bnd'].right_x:.0f}）\n"
                f"青/橙点=深度 region 右/左块内沿；窗口 ±15 帧 road_offset None {n_none}/{len(r['win'])}",
                font=F(16), fill=(60, 60, 60))
    out = OUT / "firstgate_wallhit.jpg"
    img.save(out, quality=92)
    print(f"[fig_wallhit] 已写 {out}")


def _g_q20(m: np.ndarray, rng: float, q: float = 0.20) -> np.ndarray:
    """逐行全局低分位基线（ground_model v2 探针）：地面 = 该行最低的连片地面群体。

    与滑动低分位的区别：滑窗是局部的，跨不过车身——骑路缘帧上人行道自己的下尾
    就成了基线（修正失效）；全局 q20 才能把路面（少数群体）钉成地面。"""
    cols = psd.cols_of(m)
    out = np.full(m.shape[0], np.nan, np.float32)
    for y in range(psd.Y0, psd.DIAG_Y1):
        out[y] = np.quantile(m[y, cols], q)
    return out[psd.Y0:psd.DIAG_Y1]


def _blocks_with_g(m: np.ndarray, rng: float, gfn):
    """以注入的 ground_model 跑 region_inner（唯一实现不改，探针用完即还原）。"""
    orig = psd.ground_model
    psd.ground_model = gfn
    try:
        return psd.region_inner(m, rng, G=0.02)
    finally:
        psd.ground_model = orig


def _q20rescue(args) -> None:
    """骑路缘帧的深度解锁探针（维护者追问：「为什么这帧不能用深度」）。

    ① 三群体读数 vs g：证明行中位 g 被人行道捕获（车骑上 ⇒ 人行道占近半行宽）、
       真边界成负台阶而现行只认正偏差。② 全局 q20 基线下 region_inner 的 L 块
       复活情况与内沿轨迹 vs HSV 黄线。③ 金标抽查（curve/kerb/wall 各一帧）
       q20 vs 现行的块范围/内沿差——回归风险如实列出。出图 firstgate_q20.jpg。"""
    sess_name, fid0 = args.spec.split(":")[0], int(args.spec.split(":")[1])
    p = pd.APP / "demos" / sess_name / "frames" / f"{fid0:06d}.jpg"
    m = np.load(pcq.NPY / f"{pcq.frame_key(p)}__da2s.npy").astype(np.float32)
    if not (pcq.NPY / f"{pcq.frame_key(p)}__da2s.npy").exists():
        raise SystemExit(f"缺深度缓存 {pcq.frame_key(p)}__da2s.npy（先跑 ego/wallhit 补）")
    rng = psd.road_range(m)
    g_cur = psd.ground_model(m, rng)

    print(f"== ① 三群体读数 vs 现行 g（{sess_name} fid{fid0}）==")
    print("行   g(y)   左区[30,380]  右路[790,850]  远右[900,1250]（相对 g）")
    for y in (560, 600, 640, 680):
        gy = float(g_cur[y - psd.Y0])
        z = [float(np.median(m[y, a:b])) for a, b in ((30, 381), (790, 851), (900, 1251))]
        print(f"{y}  {gy:.3f}  " + "  ".join(f"{v:.3f} ({(v - gy) / gy:+.1%})" for v in z))

    print("\n== ② 全局 q20：L 块内沿 vs HSV 黄线 ==")
    cur = psd.ground_model
    bl_q = _blocks_with_g(m, rng, _g_q20)
    bl_c = _blocks_with_g(m, rng, cur)
    rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
    ym = cv2.inRange(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV), pd.HSV_LO, pd.HSV_HI)
    inner_q = bl_q["L"][0] if bl_q["L"] else {}
    rng_of = lambda x: "无" if x is None else f"y[{x[1]},{x[2]}]"
    print(f"L 块：现行 {rng_of(bl_c['L'])} → q20 {rng_of(bl_q['L'])}")
    for y in range(560, 711, 15):
        idx = np.where(ym[y] > 0)[0]
        runs = []
        if len(idx):
            s = prev = idx[0]
            for x in idx[1:]:
                if x - prev > 4:
                    runs.append((int(s), int(prev)))
                    s = x
                prev = x
            runs.append((int(s), int(prev)))
        print(f"  y{y}: 内沿{inner_q.get(y)}  HSV{runs}")

    print("\n== ③ 金标抽查（q20 vs 现行）==")
    labels = gold_rows()
    seen = {}
    for r in labels:
        if r["stratum"] in ("curve_cont2", "kerb", "wall") and r["stratum"] not in seen:
            seen[r["stratum"]] = r
    spot = []
    for st, r in seen.items():
        pp = Path(r["path"])
        mm = np.load(pcq.NPY / f"{pcq.frame_key(pp)}__da2s.npy").astype(np.float32)
        rr = psd.road_range(mm)
        a = _blocks_with_g(mm, rr, cur)
        b = _blocks_with_g(mm, rr, _g_q20)
        line = f"{st:11s} {pp.name}: "
        for side in ("L", "R"):
            line += f"{side} {rng_of(a[side])}→{rng_of(b[side])}  "
            if a[side] and b[side]:
                d = [f"y{y}:{(b[side][0].get(y) or 0) - (a[side][0].get(y) or 0):+d}"
                     for y in (560, 600, 640)
                     if a[side][0].get(y) is not None and b[side][0].get(y) is not None]
                line += f"Δ{d} "
        print(line)
        spot.append((pp, m if pp == p else mm, a, b, ym if pp == p else None))

    _fig_q20(p, m, bl_q, bl_c, ym)


def _fig_q20(p, m, bl_q, bl_c, ym) -> None:
    from PIL import Image, ImageDraw, ImageFont

    def F(sz):
        return ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", sz)

    S = 760 / 1280
    W, H = 1620, 640
    img = Image.new("RGB", (W, H), (250, 250, 248))
    d = ImageDraw.Draw(img)
    d.text((16, 10), "骑路缘帧的深度解锁：现行 g 被人行道捕获（左块死在 y564）→ 全局 q20 把地面钉回路面，边界复活",
           font=F(22), fill=(30, 30, 30))
    fr = Image.fromarray(cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)).resize((760, 428))
    dd = ImageDraw.Draw(fr)
    if bl_c["L"] is not None:
        for yy, xx in bl_c["L"][0].items():
            if 430 <= yy <= 564 and xx is not None:
                dd.ellipse([xx * S - 2, yy * S - 2, xx * S + 2, yy * S + 2], fill=(170, 170, 170))
    if bl_q["L"] is not None:
        for yy, xx in bl_q["L"][0].items():
            if 540 <= yy <= 710 and xx is not None:
                dd.ellipse([xx * S - 4, yy * S - 4, xx * S + 4, yy * S + 4], fill=(255, 120, 20))
    for yy in range(560, 711, 20):
        for a, b in _runs(ym[yy]):
            dd.line([(a * S, yy * S), (b * S, yy * S)], fill=(255, 0, 255), width=2)
    img.paste(fr, (16, 56))
    d.text((16, 56 + 428 + 8),
           "灰点=现行 g 的 L 块（死在 y564，近带只剩边缘窄条）；橙点=全局 q20 的 L 块内沿 y[340,714]，"
           "570~710 干净直线轨迹 472→384=人行道右缘；\n品红=HSV 黄线/砖区（线碎段在缘外 10~30px）。"
           "y540~560 内沿被车身中段粘连（ego_mask 只盖到 y532）。回归抽查见 stdout：q20 非免费，"
           "须配行内直线拟合+金标全量回归锁（ground_model v2，待裁）。",
           font=F(16), fill=(60, 60, 60))
    out = OUT / "firstgate_q20.jpg"
    img.save(out, quality=92)
    print(f"\n[fig_q20] 已写 {out}")


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
    if ap == "fig_ground":
        _fig_ground(labels)
        return
    if ap == "ego":
        _ego_scan(labels)
        return
    if ap == "wallhit":
        class _A:
            spec = next((a.split("=", 1)[1] for a in argv if a.startswith("--spec=")),
                        "20260922_113932_p1:518,20260922_113610_p2:437")
        _wallhit(_A)
        return
    if ap == "q20rescue":
        class _B:
            spec = next((a.split("=", 1)[1] for a in argv if a.startswith("--spec=")),
                        "20260922_113932_p1:518")
        _q20rescue(_B)
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
