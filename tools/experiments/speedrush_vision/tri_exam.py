"""三方同卷：HSV 现行层 / DA 区域(q20) / 分割区域——几何主人的裁决考试（2026-09-24）。

**为什么有这个文件**：维护者裁定从根重审（"黄线就是最没用的"）后，两个候选表征
（分割 road 区域 vs 深度区域）与现行黄线层在同一批帧、同一组尺上同场考试。
维护者补充口径：**现有样本即考卷**（只跑过一回合，后续场景本来就没采到）——
夜/隧道/大雨格缺失如实标注，不宣称覆盖。

**帧集**：金标 54 帧（wall/kerb/curve_cont/curve_cont2/obstacle/wallhug 六层）
+ 骑缘窗 113932_p1 fid 500~536 + 贴墙窗 113610_p2 fid 422~452（抖动尺用连续帧）。

**候选**（各按各的方法原样出读数，谁也不替谁修）：
  hsv    生产 detect_boundary（left_x/right_x=簇顶行读数，行不定 ⇒ 只进中心尺）
  da_cur 深度区域，现行 ground_model（region_inner 原样）
  da_q20 深度区域，全局低分位基线（q20rescue 探针形态）
  seg    SegFormer-b0 ADE20K（optimum 转换，cls==6 road）逐行 min/max（含路肩语义外偏）

**尺**（中心量为主——控制闭环实际消费的量）：
  R1 金标中心误差 @y600（双侧在场帧；HSV 中心=簇顶行左右读数中点，行不同的
     偏差由线近线性抵消，如实注记）
  R2 单侧位置误差 @y600（da/seg 有逐行内沿，报分层 dev；hsv 不参与）
  R3 相邻帧抖动（两窗，双侧在场对：|Δ中心| 与 |Δ宽|，y=600）
  R4 在场率（每候选每帧集，双侧/单侧/弃权）
  R5 特例帧 518/437 逐候选读数（对照 wallhit 取证事实）

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/tri_exam.py
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
import probe_step_detect as psd  # noqa: E402
from gold_score import gold_x_at  # noqa: E402
from firstgate_score import _blocks_with_g, _g_q20, gold_rows  # noqa: E402

OUT = psd.OUT
W = psd.NPY / ".." / "weights"
DEMOS = pd.APP / "demos"
YROW = 600
ROWS = (560, 600, 640)


def seg_session():
    import onnxruntime as ort
    return ort.InferenceSession(str(W / "segformer-b0-ade.onnx"),
                                providers=["DmlExecutionProvider"])


def seg_road_edges(sess, rgb) -> dict[int, tuple[int, int]]:
    """road(cls==6) 掩码逐行 (min,max)；语义=可行区域外沿（untuned，含路肩外偏）。"""
    import math
    im = cv2.resize(rgb, (512, 512), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    im = (im - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    x = im.transpose(2, 0, 1)[None]
    logits = sess.run(None, {"pixel_values": x})[0][0]          # (150,128,128)
    cls = np.argmax(logits, axis=0)
    mask = cls == 6
    mask = cv2.resize(mask.astype(np.uint8) * 255, (1280, 720), interpolation=cv2.INTER_NEAREST) > 0
    out: dict[int, tuple[int, int]] = {}
    for y in ROWS:
        xs = np.flatnonzero(mask[y])
        if len(xs) >= 50:
            out[y] = (int(xs.min()), int(xs.max()))
    return out


def da_depth(p: Path, sess) -> np.ndarray:
    key = pcq.frame_key(p)
    cache = psd.NPY / f"{key}__da2s.npy"
    if cache.exists():
        return np.load(cache).astype(np.float32)
    rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
    m = pd.depth_map(sess, rgb, 518)
    psd.NPY.mkdir(parents=True, exist_ok=True)
    np.save(cache, m.astype(np.float16))
    return m.astype(np.float32)


def collect() -> list[dict]:
    """每帧每候选每侧 @ROWS 的边界读数 + 中心/宽 @YROW。"""
    from maaracing_master.plugins.speedrush.boundary import detect_boundary
    da_sess = psd._folded_sess(518)
    seg_sess = seg_session()
    recs: list[dict] = []

    def one(frameset: str, p: Path, gold: dict | None) -> None:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        rec = {"frameset": frameset, "fid": p.name, "gold": gold is not None}
        # hsv（生产原样）
        b = detect_boundary(rgb)
        hsv_ok = b.sides == 2 and b.left_x == b.left_x and b.right_x == b.right_x
        rec["hsv_L"] = b.left_x if hsv_ok else None
        rec["hsv_R"] = b.right_x if hsv_ok else None
        rec["hsv_sides"] = b.sides
        # 深度（缓存/推理一次，两版基线共用）
        m = da_depth(p, da_sess)
        rng = psd.road_range(m)
        for tag, gfn in (("da_cur", None), ("da_q20", "_q20")):
            if gfn is None:
                bl = _blocks_with_g(m, rng, psd.ground_model)
            else:
                bl = _blocks_with_g(m, rng, _g_q20)
            for side in ("L", "R"):
                blk = bl[side]
                for y in ROWS:
                    rec[f"{tag}_{side}{y}"] = blk[0].get(y) if blk else None
        # seg
        edges = seg_road_edges(seg_sess, rgb)
        for y in ROWS:
            e = edges.get(y)
            rec[f"seg_L{y}"], rec[f"seg_R{y}"] = (e[0], e[1]) if e else (None, None)
        # 金标（若有）：gold=是否金标帧，goldrow=标签行本身
        if gold is not None:
            for side in ("l", "r"):
                gx = gold_x_at(gold, side, YROW)
                rec[f"gold_{side}"] = gx
            rec["goldrow"] = gold
        else:
            rec["goldrow"] = None
        recs.append(rec)

    labels = gold_rows()
    for r in labels:
        one("gold", Path(r["path"]), r)
    for frameset, sess_name, lo, hi in (
            ("straddle", "20260922_113932_p1", 500, 536),
            ("wallhug", "20260922_113610_p2", 422, 452)):
        for fid in range(lo, hi + 1):
            p = DEMOS / sess_name / "frames" / f"{fid:06d}.jpg"
            if p.exists():
                one(frameset, p, None)
    return recs


def center_width(rec: dict, tag: str, y: int = YROW):
    l, r = rec.get(f"{tag}_L{y}" if tag != "hsv" else "hsv_L"), \
           rec.get(f"{tag}_R{y}" if tag != "hsv" else "hsv_R")
    if l is None or r is None or l != l or r != r:
        return None, None
    return (l + r) / 2.0, r - l


def main() -> None:
    recs = collect()
    with (OUT / "tri_exam.csv").open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=sorted({k for r in recs for k in r}))
        wr.writeheader()
        wr.writerows(recs)
    print(f"[tri] {len(recs)} 帧 → tri_exam.csv")

    cands = ["hsv", "da_cur", "da_q20", "seg"]
    print("\n== R4 在场率（双侧@600 / 帧数）==")
    for fs in ("gold", "straddle", "wallhug"):
        sub = [r for r in recs if r["frameset"] == fs]
        cells = []
        for c in cands:
            k = sum(1 for r in sub if center_width(r, c)[0] is not None)
            cells.append(f"{c} {k}/{len(sub)}")
        print(f"{fs:9s} " + "  ".join(cells))

    print("\n== R2 单侧位置误差（px；dev=候选−金标，负=候选偏内）==")
    print("比较行 = 逐帧取金标线段（ny~fy）与固定行集的交集；金标外推出画面的行不计。")
    for c in ("da_cur", "da_q20", "seg"):
        for side, pre in (("L", "l"), ("R", "r")):
            by_st: dict[str, list] = {}
            for r in recs:
                g = r.get("goldrow")
                if not g:
                    continue
                ny, fy = float(g[pre + "_ny"]), float(g[pre + "_fy"])
                lo_, hi_ = min(ny, fy), max(ny, fy)
                for y in ROWS:
                    if not (lo_ <= y <= hi_):
                        continue
                    gx = gold_x_at(g, pre, y)
                    if gx is None or not (-20 <= gx <= 1299):
                        continue
                    x = r.get(f"{c}_{side}{y}")
                    if x is not None:
                        by_st.setdefault(g["stratum"], []).append(x - gx)
            cells = [f"{st} 中位{np.median(v):+6.0f}/p90|{np.percentile(np.abs(v), 90):4.0f}|(n={len(v)})"
                     for st, v in sorted(by_st.items())]
            print(f"{c:7s} {side}: " + ("  ".join(cells) if cells else "（无在场样本）"))

    print("\n== R2c 中心误差（da/seg 有双侧逐行的帧；行=两侧线段交集）==")
    for c in ("da_cur", "da_q20", "seg"):
        devs = []
        for r in recs:
            g = r.get("goldrow")
            if not g:
                continue
            for y in ROWS:
                ok = True
                gc = []
                for side, pre in (("L", "l"), ("R", "r")):
                    ny, fy = float(g[pre + "_ny"]), float(g[pre + "_fy"])
                    if not (min(ny, fy) <= y <= max(ny, fy)):
                        ok = False
                        break
                    gx = gold_x_at(g, pre, y)
                    if gx is None or not (-20 <= gx <= 1299):
                        ok = False
                        break
                    gc.append(gx)
                if not ok:
                    continue
                l_, r_ = r.get(f"{c}_L{y}"), r.get(f"{c}_R{y}")
                if l_ is None or r_ is None:
                    continue
                devs.append((l_ + r_) / 2.0 - sum(gc) / 2.0)
                break
        if devs:
            v = np.array(devs)
            print(f"{c:7s} n={len(v):3d}  中位 {np.median(v):+7.1f}  p90|dev| {np.percentile(np.abs(v), 90):6.1f}")
        else:
            print(f"{c:7s} n=0")

    print("\n== R3 相邻帧抖动（两窗，双侧在场相邻对，y=600，px）==")
    for fs in ("straddle", "wallhug"):
        sub = [r for r in recs if r["frameset"] == fs]
        for c in cands:
            dc, dw = [], []
            prev = None
            for r in sub:
                cc, ww = center_width(r, c)
                if cc is not None and prev is not None:
                    dc.append(abs(cc - prev[0]))
                    dw.append(abs(ww - prev[1]))
                prev = (cc, ww) if cc is not None else None
            if dc:
                v = np.array(dc)
                print(f"{fs:9s} {c:7s} n={len(v):3d}  |Δ中心| 中位 {np.median(v):6.1f}  p95 {np.percentile(v, 95):6.1f}  max {v.max():6.1f}  |Δ宽|max {max(dw) if dw else 0:.0f}")

    print("\n== R5 特例帧（@600 读数；L 内沿 / R 内沿）==")
    for fs, fid in (("straddle", "000518.jpg"), ("wallhug", "000437.jpg")):
        r = next((x for x in recs if x["frameset"] == fs and x["fid"] == fid), None)
        if not r:
            continue
        cells = []
        for c in cands:
            if c == "hsv":
                cells.append(f"hsv L{r['hsv_L']} R{r['hsv_R']}")
            else:
                cells.append(f"{c} L{r.get(f'{c}_L{YROW}')} R{r.get(f'{c}_R{YROW}')}")
        print(f"{fs} {fid}: " + " | ".join(cells))


if __name__ == "__main__":
    main()
