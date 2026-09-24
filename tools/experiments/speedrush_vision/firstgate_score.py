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


def collect(variant: str, labels) -> list[dict]:
    """一变体全金标 → 逐样本记录（P1/对比度/dev 同帧同侧同行可配对）。"""
    recs = []
    for r in labels:
        p = Path(r["path"])
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        m = load_variant_map(variant, p, rgb)
        rng = psd.road_range(m)
        g = psd.ground_model(m, rng)
        blobs = psd.region_inner(m, rng)
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


def main() -> None:
    ap = sys.argv[1:] and sys.argv[1] or ""
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

    allrecs: dict[str, list[dict]] = {}
    for v in VARIANTS:
        allrecs[v] = collect(v, labels)
        print(f"[score] {v}: {len(allrecs[v])} 样本")
    with (OUT / "firstgate_score.csv").open("w", newline="", encoding="utf-8") as f:
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
