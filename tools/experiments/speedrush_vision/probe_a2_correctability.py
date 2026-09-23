"""A2-0：yolo26n 深度误差结构 / 可纠正性探针（C 类，2026-09-23，维护者裁决驱动）。

**要回答的问题**（维护者 2026-09-23 裁决：先验证"锚定为什么能修"，再谈融合）：
  yolo26n 相对 DA-S 的误差属于哪一种？
  - A 型 全局尺度/偏移漂移：低频 DA 锚定（全局仿射校正）有希望修 → 融合立项有据；
  - B 型 局部结构错误（分离度足够的点对远近序反了）：锚定救不了 → 融合按此方案不成立。
  附带：① M1 尾部（Pareto 表 p10=−1.00）逐框取证——偶发还是系统性；② 低频锚定
  保持性仿真：锚点每 N 帧刷新一次（参数冻结沿用），跨帧漂移能压到多少、锚频要求多少。

**方法**（全部为缓存图上的离线分析，不建融合、不改生产）：
  - yolo26n 输出 z 大=远（frame100 实测：行中位 y=340→18.7、y=700→2.79，与 DA
    视差大=近反向）；本探针缓存**原始 z**，分析侧在三个单调变换族 exp(−z)/1/z/−z
    （均把"大"翻成"近"）里取**中位 R² 最高的一族**做值域分析——给锚定最有利变换
    的宽容界：宽容界都不成立，融合即可否决；宽容界成立，才轮到真融合原型裁决。
    秩指标（Kendall/分离点对序错）对单调变换不变，与族选择无关。
  - 参照系 = DA-S 视差（大=近）。域1 地面轮廓 g(y)：probe_depth.ground_line 同口径
    （逐行中位、剔自车列带；**只取 Y_H+16 以下的已填行**，地平线以上的 NaN 不参与
    任何符号/秩比较）。域2 他车框：旧检测 cls∈{car,bonus_car} 框内中位，框坐标
    **夹紧到图界**（出画框负坐标会被 numpy 负索引回绕成空切片）。
  - 值域分析统一在**帧内 p1-p99 归一化空间**（两侧各自归一到 [0,1]，与生产指标同
    口径）：A 型读数 = Theil-Sen 仿射拟合 R²、校正前后 nRMSE 下降；B 型读数 =
    分离点对序错率（|Δx|>θ，θ∈{5%,10%,20%}；仿射不改秩，锚定修不了）+ Kendall τ。
  - 锚定仿真 = 每帧拟合 (a,b)，按 N∈{5,11,21,41} 帧冻结沿用，测跨帧框视差漂移
    （|Δv|/路面量程，DA/yolo 各按本帧归一），对照 DA 本底与 yolo 裸奔。

**M1 重算口径声明**：A1 矩阵的临时脚本已清，无法逐字节对齐；本脚本 diag 模式以
probe_depth 读数③同口径（框中位 vs cy 序 Spearman，≥3 框帧）干净重算——秩读数对
单调变换不变，重算与矩阵可直接对比：若复现 p10=−1.00 则矩阵读数有效，若不复现则
矩阵该格修正并披露。

用法（仓库根，.venv Python）：
    P=tools/experiments/speedrush_vision/probe_a2_correctability.py
    python $P infer --model da2s --sets bad,ctrl,win   # DA-S 全量缓存（约 2 分钟）
    python $P infer --model y26n --sets bad,ctrl,win   # yolo26n 原始 z 缓存（自验门槛）
    python $P diag      # M1 干净重算 + 尾部帧取证（图 + 逐框表）
    python $P struct    # A/B 型分解（bad+ctrl 103 帧，逐帧表落 CSV）
    python $P anchor    # 锚定保持性仿真（连续 161 帧窗）
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
from probe_old_model import detect

APP = pd.APP
OUT = pd.OUT
NPY = OUT / "npy"
DETS = OUT / "dets"
WEIGHTS = OUT / "weights"
Y26N = WEIGHTS / "yolo26n-depth.onnx"
DETECT_ONNX = pd.DETECT_ONNX

WIN = list(range(100, 261))          # 锚定仿真窗：连续 161 帧
THETAS = (0.05, 0.10, 0.20)
ANCHOR_NS = (5, 11, 21, 41)
KINDS = ("expneg", "inv", "neg")     # z→伪视差（大=近）的单调变换族
DA_MS, YOLO_MS, DET_MS = 435.0, 11.0, 5.4   # Pareto 表实测，用于锚频摊销估算


def frame_key(p: Path) -> str:
    return pd.frame_key(p)


def all_frames(sets: str) -> list[Path]:
    out: list[Path] = []
    for s in sets.split(","):
        if s == "bad":
            out += pd.set_frames("bad")
        elif s == "ctrl":
            out += pd.set_frames("ctrl")
        elif s == "win":
            out += [pd.CTRL_SESSION / f"{i:06d}.jpg" for i in WIN]
    seen, uniq = set(), []
    for p in out:
        k = frame_key(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def yolo_session() -> ort.InferenceSession:
    return ort.InferenceSession(str(Y26N), providers=["DmlExecutionProvider"])


def yolo_z(sess, rgb: np.ndarray) -> np.ndarray:
    """→ float32 原始 z 图（大=远），已上采样回原帧尺寸。"""
    im = cv2.resize(rgb, (768, 768), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    blob = im.transpose(2, 0, 1)[None]
    z = sess.run(None, {"images": blob})[0][0, 0]
    z = np.nan_to_num(z.astype(np.float32), nan=10.0, posinf=88.0, neginf=88.0)
    return cv2.resize(z, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def pseudo(z: np.ndarray, kind: str) -> np.ndarray:
    """z（大=远）→ 伪视差（大=近），三个单调变换族。"""
    if kind == "expneg":
        return np.exp(-np.clip(z, -80.0, 88.0))
    if kind == "inv":
        return 1.0 / np.clip(z, 1e-3, None)
    if kind == "neg":
        return -z
    raise ValueError(kind)


def norm01(v: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(v, [1, 99])
    return (v - lo) / max(hi - lo, 1e-9)


def da_session() -> ort.InferenceSession:
    return ort.InferenceSession(str(pd.MODELS["small"]), providers=["DmlExecutionProvider"])


def npy_path(k: str, tag: str) -> Path:
    return NPY / f"{k}__{tag}.npy"


def load_map(k: str, tag: str) -> np.ndarray:
    return np.load(npy_path(k, tag)).astype(np.float32)


def cmd_infer(args) -> None:
    NPY.mkdir(parents=True, exist_ok=True)
    frames = all_frames(args.sets)
    tag = args.model
    if tag == "y26n":
        # 自验（方向陷阱守卫）：z 应大=远，即轮廓随 y 递减，与 DA（递增）反向。
        # 单会话纪律：两个 DML 会话并存会在本机段错误，DA 轮廓一律读 npy。
        # 只比已填行（Y_H+16 以下）且双方差分非零处——NaN 区不参与。
        k0 = frame_key(pd.CTRL_SESSION / "000100.jpg")
        if not npy_path(k0, "da2s").exists():
            raise SystemExit("先跑 --model da2s 生成 DA 缓存，再做 y26n 自验")
        sess = yolo_session()
        rgb0 = cv2.cvtColor(cv2.imread(str(pd.CTRL_SESSION / "000100.jpg")), cv2.COLOR_BGR2RGB)
        gz = pd.ground_line(yolo_z(sess, rgb0))[int(pd.Y_H) + 16:]
        gd = pd.ground_line(load_map(k0, "da2s"))[int(pd.Y_H) + 16:]
        m = (np.sign(np.diff(gz)) != 0) & (np.sign(np.diff(gd)) != 0)
        ok = float((np.sign(np.diff(gz))[m] == -np.sign(np.diff(gd))[m]).mean())
        print(f"[自验] frame100 已填行方向反号率={ok:.2f}（z 大=远 ↔ DA 大=近）", flush=True)
        if ok < 0.85:
            raise SystemExit("yolo 方向/预处理自验失败：先修转换再全量跑")
    else:
        sess = da_session()
    t0 = time.perf_counter()
    done = 0
    for p in frames:
        k = frame_key(p)
        if npy_path(k, tag).exists():
            continue
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        d = yolo_z(sess, rgb) if tag == "y26n" else pd.depth_map(sess, rgb, 518)
        np.save(npy_path(k, tag), d.astype(np.float16))
        done += 1
        if done % 32 == 0:
            print(f"  ...{done}/{len(frames)} 已耗时 {time.perf_counter() - t0:.0f}s",
                  flush=True)
    print(f"[infer:{tag}] 新算 {done}/{len(frames)}，总耗时 {time.perf_counter() - t0:.0f}s",
          flush=True)


def ensure_dets(frames: list[Path]) -> None:
    """旧检测框缓存（diag/struct/anchor 共用）。"""
    DETS.mkdir(parents=True, exist_ok=True)
    todo = [p for p in frames if not (DETS / f"{frame_key(p)}.json").exists()]
    if not todo:
        return
    sess = ort.InferenceSession(str(DETECT_ONNX), providers=["DmlExecutionProvider"])
    for p in todo:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        dets = [(int(c), float(s), [float(v) for v in b]) for c, s, b in detect(sess, rgb)
                if c in (1, 2)]
        (DETS / f"{frame_key(p)}.json").write_text(json.dumps(dets))


def box_stats(k: str, kind: str = "expneg") -> list[tuple[int, float, float]]:
    """→ [(cy, da视差, 伪视差)]，框内中位（probe_depth 读数③口径）。"""
    dets = json.loads((DETS / f"{k}.json").read_text())
    out = []
    for cls, _sc, (x1, y1, x2, y2) in dets:
        # 夹紧到图边界（出画框 x1 可为负——负起点会被 numpy 回绕成空切片），
        # 内净尺寸 ≥8px 防空切片 median=NaN
        y1i, y2i = max(int(y1) + 2, 0), min(int(y2) - 2, 720)
        x1i, x2i = max(int(x1) + 2, 0), min(int(x2) - 2, 1280)
        if y2i - y1i < 8 or x2i - x1i < 8:
            continue
        sl = slice(y1i, y2i), slice(x1i, x2i)
        da = float(np.median(load_map(k, "da2s")[sl]))
        yd = float(np.median(pseudo(load_map(k, "y26n")[sl], kind)))
        out.append((int((y1 + y2) / 2), da, yd))
    return out


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    def _ranks(v):
        o = v.argsort()
        r = np.empty(len(v))
        r[o] = np.arange(len(v))
        return r
    n = len(a)
    return 1 - 6 * ((_ranks(a) - _ranks(b)) ** 2).sum() / (n * (n * n - 1))


def kendall(x: np.ndarray, y: np.ndarray) -> float:
    iu = np.triu_indices(len(x), 1)
    sx = np.sign(x[iu[1]] - x[iu[0]])
    sy = np.sign(y[iu[1]] - y[iu[0]])
    m = (sx != 0) & (sy != 0)
    if m.sum() == 0:
        return np.nan
    sx, sy = sx[m], sy[m]
    nc, nd = int((sx * sy > 0).sum()), int((sx * sy < 0).sum())
    return (nc - nd) / (nc + nd)


def sep_err(x: np.ndarray, y: np.ndarray, theta: float):
    """分离点对（|Δx|>θ·x 量程）符号不一致率 → (err, n_sel)。"""
    iu = np.triu_indices(len(x), 1)
    rng = float(np.percentile(x, 99) - np.percentile(x, 1))
    sel = np.abs(x[iu[1]] - x[iu[0]]) > theta * max(rng, 1e-9)
    if sel.sum() == 0:
        return np.nan, 0
    sx = np.sign(x[iu[1][sel]] - x[iu[0][sel]])
    sy = np.sign(y[iu[1][sel]] - y[iu[0][sel]])
    m = (sx != 0) & (sy != 0)
    if m.sum() == 0:
        return np.nan, 0
    return float((sx[m] != sy[m]).mean()), int(m.sum())


def theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    iu = np.triu_indices(len(x), 1)
    dx = x[iu[1]] - x[iu[0]]
    dy = y[iu[1]] - y[iu[0]]
    m = dx != 0
    a = float(np.median(dy[m] / dx[m]))
    b = float(np.median(y - a * x))
    return a, b


def profile_pair(k: str, kind: str) -> tuple[np.ndarray, np.ndarray]:
    """→ (x=DA 轮廓归一, y=yolo 伪视差轮廓归一)，只取已填行、剔 NaN。"""
    gz = pd.ground_line(load_map(k, "y26n"))[int(pd.Y_H) + 16:]
    gd = pd.ground_line(load_map(k, "da2s"))[int(pd.Y_H) + 16:]
    m = ~(np.isnan(gz) | np.isnan(gd))
    return norm01(gd[m]), norm01(pseudo(gz[m], kind))


def cmd_diag(args) -> None:
    frames = all_frames("bad,ctrl,win")
    ensure_dets(frames)
    for min_boxes in (3, 2):
        rows = {"da2s": [], "y26n": []}
        for p in frames:
            k = frame_key(p)
            bs = box_stats(k)
            if len(bs) < min_boxes:
                continue
            cy = np.array([b[0] for b in bs], float)
            for tag in ("da2s", "y26n"):
                dv = np.array([b[1] if tag == "da2s" else b[2] for b in bs])
                rows[tag].append((p, k, _spearman(cy, dv), len(bs)))
        for tag in ("da2s", "y26n"):
            rhos = np.array([r[2] for r in rows[tag]])
            print(f"[M1重算:{tag} ≥{min_boxes}框] n={len(rhos)}帧 "
                  f"Spearman 中位={np.median(rhos):.3f} p10={np.percentile(rhos, 10):.3f} "
                  f"ρ<0占比={(rhos < 0).mean():.0%}")
        if min_boxes == 3:
            bad_frames = [r for r in rows["y26n"] if r[2] < 0]
            print(f"[尾部取证] y26n ρ<0 帧 {len(bad_frames)} 个：")
            dumped = 0
            for p, k, rho, n in bad_frames:
                bs = box_stats(k)
                print(f"  {k} ρ={rho:+.2f} n框={n}")
                print("    cy      DA视差    yolo伪视差")
                for cy, da, yd in sorted(bs):
                    print(f"    {cy:5d}  {da:8.4f}  {yd:8.4f}")
                if dumped < 8:
                    _dump_tail(p, k)
                    dumped += 1


def _dump_tail(p: Path, k: str) -> None:
    rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
    panels = [cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)]
    for tag, kind in (("y26n", "expneg"), ("da2s", None)):
        d = load_map(k, tag)
        if kind:
            d = pseudo(d, kind)
        lo, hi = np.percentile(d, [1, 99])
        du = np.clip((d - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
        panels.append(cv2.applyColorMap(du, cv2.COLORMAP_JET))
    cv2.imwrite(str(OUT / f"a2tail_{k}.jpg"), np.hstack(panels))


def _frame_struct(k: str, kind: str) -> dict | None:
    x, y = profile_pair(k, kind)
    if len(x) < 50:
        return None
    tau = kendall(x, y)
    a, b = theil_sen(x, y)
    fit = a * x + b
    ss_res = float(((y - fit) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / max(ss_tot, 1e-9)
    e0 = float(np.sqrt(ss_tot / len(y)))
    e1 = float(np.sqrt(ss_res / len(y)))
    row = {"frame": k, "kind": kind, "tau": round(tau, 4), "R2": round(r2, 4),
           "a": round(a, 4), "b": round(b, 4),
           "nrmse_before": round(e0, 4), "nrmse_after": round(e1, 4)}
    for t in THETAS:
        err, n = sep_err(x, y, t)
        row[f"sep{t}"] = np.nan if not n else round(err, 4)
        row[f"sepn{t}"] = n
    return row


def cmd_struct(args) -> None:
    frames = pd.set_frames("bad") + pd.set_frames("ctrl")
    ensure_dets(frames)
    # 第一步：三个变换族各算值域读数，选中位 R² 最高的一族（给锚定最有利变换）
    per_kind: dict[str, list[float]] = {}
    for kind in KINDS:
        r2s = []
        for p in frames:
            row = _frame_struct(frame_key(p), kind)
            if row:
                r2s.append(row["R2"])
        per_kind[kind] = r2s
        print(f"[族 {kind}] R² 中位={np.median(r2s):.3f} p10={np.percentile(r2s, 10):.3f}")
    best = max(KINDS, key=lambda k: np.median(per_kind[k]))
    print(f"[选族] {best}（中位 R² 最高，作为后续值域分析与锚定仿真的变换）")
    # 第二步：最佳族全量出表（轮廓域 + 他车框域）
    csv_rows, taus, r2s, red = [], [], [], []
    pooled = {t: [0, 0] for t in THETAS}
    box_pooled = {t: [0, 0] for t in THETAS}
    box_taus = []
    for p in frames:
        k = frame_key(p)
        row = _frame_struct(k, best)
        if not row:
            continue
        csv_rows.append(row)
        taus.append(row["tau"])
        r2s.append(row["R2"])
        red.append(1 - row["nrmse_after"] / max(row["nrmse_before"], 1e-9))
        for t in THETAS:
            if row[f"sepn{t}"]:
                pooled[t][0] += int(round(row[f"sep{t}"] * row[f"sepn{t}"]))
                pooled[t][1] += row[f"sepn{t}"]
        bs = box_stats(k, best)
        if len(bs) >= 2:
            bx = np.array([v[1] for v in bs])
            by = np.array([v[2] for v in bs])
            gd = pd.ground_line(load_map(k, "da2s"))
            w = float(np.nanpercentile(gd[int(pd.Y_H) + 16:], 99) -
                      np.nanpercentile(gd[int(pd.Y_H) + 16:], 1))
            for t in THETAS:
                iu = np.triu_indices(len(bx), 1)
                sel = np.abs(bx[iu[1]] - bx[iu[0]]) > t * max(w, 1e-9)
                if sel.sum():
                    sx = np.sign(bx[iu[1][sel]] - bx[iu[0][sel]])
                    sy = np.sign(by[iu[1][sel]] - by[iu[0][sel]])
                    m = (sx != 0) & (sy != 0)
                    box_pooled[t][1] += int(m.sum())
                    box_pooled[t][0] += int((sx[m] != sy[m]).sum())
            if len(bs) >= 3:
                box_taus.append(kendall(bx, by))
    taus, r2s, red = map(np.array, (taus, r2s, red))
    print(f"[结构 n={len(taus)}帧 族={best}] Kendall τ p50={np.median(taus):.3f} "
          f"p10={np.percentile(taus, 10):.3f}（τ<0 占比={(taus < 0).mean():.0%}）")
    print(f"        R²(A型占比) p50={np.median(r2s):.3f} p10={np.percentile(r2s, 10):.3f} | "
          f"仿射校正后 nRMSE 降幅 p50={np.median(red):.0%}")
    for t in THETAS:
        e, n = pooled[t]
        eb, nb = box_pooled[t]
        print(f"        轮廓分离点对序错 θ={t:.0%}: {e / max(n, 1):.2%} (n={n}) | "
              f"他车框: {eb / max(nb, 1):.2%} (n={nb})")
    if box_taus:
        bt = np.array(box_taus)
        print(f"        框域 Kendall τ p50={np.median(bt):.3f} p10={np.percentile(bt, 10):.3f} "
              f"(n={len(bt)}帧)")
    with (OUT / "a2_struct_perframe.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(csv_rows[0]))
        w.writeheader()
        w.writerows(csv_rows)
    print(f"[落档] {OUT / 'a2_struct_perframe.csv'}")


def iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def cmd_anchor(args) -> None:
    kind = args.kind
    frames = [pd.CTRL_SESSION / f"{i:06d}.jpg" for i in WIN]
    ensure_dets(frames)
    keys = [frame_key(p) for p in frames]
    # 每帧仿射参数（归一轮廓域 Theil-Sen）
    ab = {}
    for p, k in zip(frames, keys):
        x, y = profile_pair(k, kind)
        if len(x) >= 50:
            ab[k] = theil_sen(x, y)
    ks = [k for k in keys if k in ab]
    a_arr = np.array([ab[k][0] for k in ks])
    b_arr = np.array([ab[k][1] for k in ks])
    print(f"[锚参漂移 n={len(ks)} 族={kind}] a: p5={np.percentile(a_arr, 5):.3f} "
          f"p50={np.median(a_arr):.3f} p95={np.percentile(a_arr, 95):.3f} | "
          f"b: p5={np.percentile(b_arr, 5):.3f} p50={np.median(b_arr):.3f} "
          f"p95={np.percentile(b_arr, 95):.3f}")
    da_jump = np.abs(np.diff(a_arr)) / max(abs(float(np.median(a_arr))), 1e-9)
    print(f"        a 逐帧相对跳变 p50={np.median(da_jump):.1%} p95={np.percentile(da_jump, 95):.1%}"
          f"（21fps；冻结 N 帧的参数陈化上限≈跳变×N）")
    # 每帧 cy→(da归一, yolo伪视差归一) 查找表（DA/yolo 各按本帧路面量程归一）
    lut = {}
    for k in ks:
        gd = pd.ground_line(load_map(k, "da2s"))[int(pd.Y_H) + 16:]
        gz = pd.ground_line(load_map(k, "y26n"))[int(pd.Y_H) + 16:]
        w_da = float(np.nanpercentile(gd, 99) - np.nanpercentile(gd, 1))
        w_y = float(np.nanpercentile(pseudo(gz, kind), 99) -
                    np.nanpercentile(pseudo(gz, kind), 1))
        lut[k] = {cy: ((da - np.nanpercentile(gd, 1)) / max(w_da, 1e-9),
                       (yd - np.nanpercentile(pseudo(gz, kind), 1)) / max(w_y, 1e-9))
                  for cy, da, yd in box_stats(k, kind)}
    base = pd.CTRL_SESSION.name

    def k_of(fid: int) -> str:
        return f"{base}__{fid:06d}"

    held = {n: [] for n in ANCHOR_NS}
    da_v, y_raw, y_orc = [], [], []
    nmatch = 0
    for i in range(len(frames) - 1):
        ka, kb = keys[i], keys[i + 1]
        if ka not in ab or kb not in ab:
            continue
        da_boxes = json.loads((DETS / f"{ka}.json").read_text())
        db_boxes = json.loads((DETS / f"{kb}.json").read_text())
        fid_a = int(ka.split("__")[-1])
        for ca, _sa, ba in da_boxes:
            best, bi = 0.3, -1
            for j, (cb, _sb, bb) in enumerate(db_boxes):
                if cb != ca:
                    continue
                v = iou(ba, bb)
                if v > best:
                    best, bi = v, j
            if bi < 0:
                continue
            bb = db_boxes[bi][2]
            cya, cyb = int((ba[1] + ba[3]) / 2), int((bb[1] + bb[3]) / 2)
            if cya not in lut[ka] or cyb not in lut[kb]:
                continue
            da_a, y_a = lut[ka][cya]
            da_b, y_b = lut[kb][cyb]
            nmatch += 1
            da_v.append(abs(da_b - da_a))
            y_raw.append(abs(y_b - y_a))
            at, bt = ab[ka]
            y_orc.append(abs((at * y_b + bt) - (at * y_a + bt)))
            idx = fid_a - 100
            for n in ANCHOR_NS:
                aa, bbt = ab[k_of(100 + (idx // n) * n)]
                held[n].append(abs((aa * y_b + bbt) - (aa * y_a + bbt)))
    print(f"[跨帧漂移 n={nmatch}匹配] 变体 → |Δv|/路面量程 p50/p95（归一空间）:")
    rows = [("da（本底）", da_v), ("y_raw", y_raw), ("y_oracle（逐帧锚）", y_orc)]
    rows += [(f"y_held@N={n}", held[n]) for n in ANCHOR_NS]
    for name, vals in rows:
        if not vals:
            print(f"    {name:18s} （无匹配）")
            continue
        v = np.array(vals)
        print(f"    {name:18s} p50={np.median(v):.4f} p95={np.percentile(v, 95):.4f}")
    print("[锚频摊销] 每N帧刷一次DA-S：摊销=435/N ms/帧，+检测5.4+yolo11，帧预算66ms：")
    for n in ANCHOR_NS:
        total = DA_MS / n + YOLO_MS + DET_MS
        print(f"    N={n:2d} → {total:5.1f} ms/帧 {'✓' if total < 66 else '✗ 超预算'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    si = sub.add_parser("infer")
    si.add_argument("--model", choices=("da2s", "y26n"), required=True)
    si.add_argument("--sets", default="bad,ctrl,win")
    sub.add_parser("struct")
    sc = sub.add_parser("anchor")
    sc.add_argument("--kind", choices=KINDS, default=None)
    sub.add_parser("diag")
    args = ap.parse_args()
    if args.cmd == "infer":
        cmd_infer(args)
    elif args.cmd == "diag":
        cmd_diag(args)
    elif args.cmd == "struct":
        cmd_struct(args)
    elif args.cmd == "anchor":
        if not args.kind:
            raise SystemExit("anchor 需要 --kind（先跑 struct 看选族结果）")
        cmd_anchor(args)


if __name__ == "__main__":
    main()
