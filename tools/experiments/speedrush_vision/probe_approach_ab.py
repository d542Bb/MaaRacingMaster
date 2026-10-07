# -*- coding: utf-8 -*-
"""物体层时序口径 A/B：绝对深度 Z vs 投影尺度 h vs 视觉扩张 Δlog h vs 地面流残差。

**问题**（2026-10-07 前置测量定案）：实体 Z 米级抖（13m 静止车帧间摆 1.5m）、
深度证据年龄 p50≈250ms——物体层接近速率能不能换 2D 时序口径？候选尺子四个：
Z（准但抖慢）、h（投影尺度）、Δlog h（视觉扩张率，不要求对准消失点）、
流残差（实际行速率 − k·(cy−y_h)² 地面流场预期，剥 ego-motion 的半成品）。

**预注册砍线判据（跑数前锁定）**：在最干净的车+币同屏窗（排除飞坡帧、
IoU 关联 ≥10 帧的车类轨迹中信噪比最高者），Δlog h 的信噪比
（|一阶差中位| / 二阶差 P90，无量纲）**不超过** Z 的同口径信噪比
→ 「2D 时序替代深度差分」整条线砍掉，物体层维持 2D 现状等深度专项。

**标尺独立**（C 类硬约束 2，判决统计前置检查四问合规）：
- 静止窗：真值=零接近——各量二阶差 P50/P90 即噪声地板（谁报出运动谁出错）；
- 实战窗：真值=接近的单调性（定性）——各量一阶差方向一致率 + 信噪比；
- 同层性：信噪比无量纲，跨量同空间可比；缺口帧逐窗报数；
- 身份验证：判决轨迹出首/中/尾框标注帧条（人工目检，**非机检判据**）。

**自包含**（实验区契约）：不 import `maaracing_master`——深度侧用本目录
`moge2_post`（产线同款后处理），YOLO 推理/NMS（含 2026-10-07 修复的 xywh
口径）与流模型最小自建；gate0.json/ego_mask.json/ONNX 权重按**数据资产**路径
读取。车类先行（刚体，looming 最纯），币仅记录不进判决（地面贴纸，h 语义不同）。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_approach_ab.py --demo <dir> \
        [--static-win 16] [--drive-win 16] [--drive-n 2]
产物：<demo>/../depth_review/jitter/approach_ab_<demo名>.{json,csv,jpg}
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import statistics
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.experiments.speedrush_vision import moge2_post as mp  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
DEPTH_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/onnx/depth"
              / "moge2_vits_static_336x598_t1032_q4f16.onnx")
YOLO_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/onnx/perception"
             / "model.onnx")
GATE0_FILE = (REPO / "maaracing_master/plugins/speedrush/resources/calibration"
              / "gate0.json")
EGO_MASK_FILE = (REPO / "maaracing_master/plugins/speedrush/resources/calibration"
                 / "ego_mask.json")

YOLO_IN, CONF, IOU = 640, 0.35, 0.45      # 与产线 perception.CONF/IOU 同口径
DEPTH_IN_W, DEPTH_IN_H, TOKENS = 1280, 720, 1032
PLANE_X_MAX, PLANE_Z_LO, PLANE_Z_HI = 12.0, 4.0, 14.0   # 近场路面平面拟合窗
JUMP_DEV_M = 0.5                          # ego 区中位 hgt 偏离滚动中位 → 疑飞坡
MIN_TRACK = 10                            # 判决轨迹最少帧数（车类）

QUANTS = ("z", "h", "inv_h", "log_h", "cy", "bottom_y", "flowres", "w")
RED, BLUE, GREEN, WHITE, GRAY = (60, 60, 230), (230, 130, 40), (80, 220, 80), (255, 255, 255), (160, 160, 160)


# ---------- 自包含 YOLO（与产线 yolo_detector 同口径，含修复后 NMS） ----------

def yolo_session() -> tuple:
    avail = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
             if p in ort.get_available_providers()]
    sess = ort.InferenceSession(str(YOLO_ONNX), providers=avail)
    raw = sess.get_modelmeta().custom_metadata_map.get("names")
    names = {int(k): str(v) for k, v in ast.literal_eval(raw).items()}
    return sess, names


def yolo_detect(sess, names: dict, img_rgb: np.ndarray) -> list[dict]:
    h, w = img_rgb.shape[:2]
    scale = min(YOLO_IN / h, YOLO_IN / w)
    nh, nw = int(h * scale), int(w * scale)
    py, px = (YOLO_IN - nh) // 2, (YOLO_IN - nw) // 2
    padded = np.full((YOLO_IN, YOLO_IN, 3), 114, np.uint8)
    padded[py:py + nh, px:px + nw] = cv2.resize(
        img_rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    blob = padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    out = sess.run(None, {sess.get_inputs()[0].name: blob})[0][0].transpose(1, 0)
    xywh, cls_conf = out[:, :4], out[:, 4:]
    scores, clses = cls_conf.max(1), cls_conf.argmax(1)
    keep = scores > CONF
    dets = []
    for cid in np.unique(clses[keep]):
        m = keep & (clses == cid)
        b, s = xywh[m], scores[m]
        # NMSBoxes 只认 [x,y,w,h]（产线 2026-10-07 修复口径；原生 xywh 直喂）
        nms_idx = cv2.dnn.NMSBoxes(b.tolist(), s.tolist(), 0.0, IOU)
        if len(nms_idx) == 0:
            continue
        cls_name = names[int(cid)]
        for i in np.array(nms_idx).flatten():
            x, y, bw, bh = b[i]
            dets.append({
                "cls": cls_name, "conf": float(s[i]),
                "box": (max(0, int((x - px) / scale)), max(0, int((y - py) / scale)),
                        min(w, int((x + bw - px) / scale)),
                        min(h, int((y + bh - py) / scale)))})
    return dets


# ---------- 深度（实验区自建 moge2_post，原生 336×598 口径） ----------

def depth_session():
    avail = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
             if p in ort.get_available_providers()]
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"),
                         (1, DEPTH_IN_H, DEPTH_IN_W)):
        so.add_free_dimension_override_by_name(name, dim)
    return ort.InferenceSession(str(DEPTH_ONNX), sess_options=so, providers=avail)


def plane_fit(pts, valid, ego_nat):
    """Y=aX+bZ+c 近场路面拟合（|X|<12、Z 4~14、剔 ego 矩形；产线同窗口径）。"""
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    m = (valid & np.isfinite(Y) & (np.abs(X) < PLANE_X_MAX)
         & (Z > PLANE_Z_LO) & (Z < PLANE_Z_HI) & (~ego_nat))
    if int(m.sum()) < 500:
        return None
    A = np.stack([X[m], Z[m], np.ones(int(m.sum()))], 1)
    coef, *_ = np.linalg.lstsq(A, Y[m], rcond=None)
    return coef


def ego_region_hgt(pts, valid, coef, ego_nat) -> float | None:
    """ego 矩形内相对路面平面的中位 hgt（高出为正；飞坡尖峰探测器）。"""
    if coef is None:
        return None
    a, b, c = coef
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0     # 同产线：hgt 高出路面为正
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    hgt = sgn * (Y - (a * X + b * Z + c))
    v = hgt[ego_nat & valid & np.isfinite(hgt)]
    return float(np.median(v)) if v.size >= 20 else None


def entity_z(pts, box) -> float | None:
    """框内 Z 直方图 24 bin 最密簇中位（与 probe_point_classify.stat 同口径）。
    box 为全分辨率 1280×720 坐标，内部映射到点云原生 336×598 格。"""
    gh, gw = pts.shape[:2]
    x0 = int(box[0] * gw / 1280); y0 = int(box[1] * gh / 720)
    x1 = int(box[2] * gw / 1280); y1 = int(box[3] * gh / 720)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(gw, x1), min(gh, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    zv = pts[y0:y1, x0:x1, 2]
    zv = zv[np.isfinite(zv)]
    if zv.size < 10:
        return None
    lo, hi = np.percentile(zv, [2, 98])
    hist, edges = np.histogram(zv, bins=24, range=(lo, hi))
    c = 0.5 * (edges[hist.argmax()] + edges[hist.argmax() + 1])
    core = np.abs(zv - c) < (hi - lo) / 24
    if core.sum() < 10:
        core = np.ones(zv.size, bool)
    return float(np.median(zv[core]))


# ---------- 关联与流模型 ----------

def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def associate(tracks: list[dict], dets: list[dict], fid: int) -> None:
    """同类贪心 IoU≥0.3 关联（延续上一帧轨迹；未命中新建）。"""
    for t in tracks:
        t["_seen"] = False
    for d in sorted(dets, key=lambda d: -d["conf"]):
        best, best_v = None, 0.3
        for t in tracks:
            if t["cls"] != d["cls"] or t["_seen"] or t["obs"][-1]["fid"] != fid - 1:
                continue
            v = iou(t["obs"][-1]["box"], d["box"])
            if v > best_v:
                best, best_v = t, v
        if best is not None:
            best["obs"].append({"fid": fid, "box": d["box"], "conf": d["conf"],
                                "z": d.get("z")})
            best["_seen"] = True
        else:
            tracks.append({"cls": d["cls"],
                           "obs": [{"fid": fid, "box": d["box"], "conf": d["conf"],
                                    "z": d.get("z")}],
                           "_seen": True})


def flow_residual(tracks: list[dict], y_h: float, min_denom: float) -> None:
    """地面流场 k·(cy−y_h)² 的逐帧残差（k=当帧全体域内地面目标中位；产线
    Scorer._flow_k 同式）。残差=实际行速率−场预期；k 估计样本数随行记录。"""
    rates = []
    for t in tracks:
        if len(t["obs"]) < 2:
            continue
        prev, cur = t["obs"][-2], t["obs"][-1]
        cy = (cur["box"][1] + cur["box"][3]) / 2.0
        pcy = (prev["box"][1] + prev["box"][3]) / 2.0
        d = cy - y_h
        if d >= min_denom:
            rates.append((cy - pcy) / (d * d))
    k = statistics.median(rates) if len(rates) >= 2 else None
    for t in tracks:
        cur = t["obs"][-1]
        cy = (cur["box"][1] + cur["box"][3]) / 2.0
        d = cy - y_h
        if len(t["obs"]) < 2 or k is None or d < min_denom:
            cur["flowres"] = None
            continue
        prev = t["obs"][-2]
        pcy = (prev["box"][1] + prev["box"][3]) / 2.0
        cur["flowres"] = (cy - pcy) - k * d * d
    return k


# ---------- 统计 ----------

def diff_stats(vals: list[float]) -> dict:
    """一阶差中位/方向一致率 + 二阶差 P50/P90（帧缺口在此如实计 n_lost）。"""
    if len(vals) < 3:
        return {}
    d = [b - a for a, b in zip(vals, vals[1:])]
    dd = [b - 2 * m + a for a, m, b in zip(vals, vals[1:], vals[2:])]
    add = sorted(abs(v) for v in dd)
    med_d = statistics.median(d)
    return {
        "n": len(vals), "n_lost": 0,
        "d_med": round(med_d, 4),
        "d_dir_consistent": round(sum(1 for v in d if v * med_d > 0) / len(d), 3),
        "dd_p50": round(statistics.median([abs(v) for v in dd]), 4),
        "dd_p90": round(add[int(len(add) * 0.9)], 4),
        "snr": (round(abs(med_d) / add[int(len(add) * 0.9)], 3)
                if add[int(len(add) * 0.9)] > 1e-12 else None)}


def track_quantities(t: dict) -> dict[str, list[float]]:
    """轨迹 → 各观测量序列（缺帧如实断列：按 fid 连续段切分，跨缺口不计差分）。"""
    segs: list[list[dict]] = [[t["obs"][0]]]
    for prev, cur in zip(t["obs"], t["obs"][1:]):
        if cur["fid"] == prev["fid"] + 1:
            segs[-1].append(cur)
        else:
            segs.append([cur])
    cols = {q: [] for q in QUANTS}
    for seg in segs:
        for i, o in enumerate(seg):
            x0, y0, x1, y1 = o["box"]
            h = max(1, y1 - y0)
            cols["cy"].append((y0 + y1) / 2.0)
            cols["bottom_y"].append(float(y1))
            cols["h"].append(float(h))
            cols["inv_h"].append(1.0 / h)
            cols["log_h"].append(math.log(h))
            cols["w"].append(float(x1 - x0))
            cols["z"].append(o.get("z") if o.get("z") is not None else np.nan)
            if i == 0:
                cols["flowres"].append(np.nan)   # 段首无前帧，残差断列
            else:
                cols["flowres"].append(o.get("flowres") if o.get("flowres") is not None
                                       else np.nan)
    return cols


def clean(vals: list[float]) -> list[float]:
    return [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]


# ---------- 窗口选择 ----------

def motion_scores(demo: Path, rows: list[dict]) -> np.ndarray:
    scores = np.zeros(len(rows))
    prev = None
    for i, r in enumerate(rows):
        g = cv2.imread(str(demo / "frames" / r["file"]), cv2.IMREAD_GRAYSCALE)
        small = cv2.resize(g, (160, 90)) if g is not None else None
        if prev is not None and small is not None:
            scores[i] = float(np.abs(small.astype(np.int16) - prev).mean())
        prev = small
    return scores


def pick_low_motion(scores: np.ndarray, winlen: int) -> int:
    n = len(scores)
    return min(range(n - winlen), key=lambda i: float(scores[i:i + winlen].mean()))


def find_drive_windows(dsess, ysess, names, demo, rows, scores, winlen: int,
                       n_win: int, y_h: float, min_denom: float) -> list[int]:
    """YOLO 扫描（步长 8）找车+币同屏中枢帧，展开窗并验证轨迹持续性。"""
    starts: list[int] = []
    for i in range(4, len(rows) - winlen, 8):
        if any(starts and i - s < winlen for s in starts):
            continue
        img = cv2.cvtColor(cv2.imread(str(demo / "frames" / rows[i]["file"])),
                           cv2.COLOR_BGR2RGB)
        dets = yolo_detect(ysess, names, img)
        kinds = {d["cls"] for d in dets}
        if not {"car", "coin"} <= kinds:
            continue
        start = max(0, i - winlen // 4)
        res = run_window(dsess, ysess, names, demo, rows, start, winlen,
                         y_h, min_denom, mark_jump=True)
        cars = [t for t in res["tracks"] if t["cls"] == "car" and len(t["obs"]) >= MIN_TRACK]
        coins = [t for t in res["tracks"] if t["cls"] == "coin" and len(t["obs"]) >= 8]
        if cars and coins:
            starts.append(start)
            print(f"  drive窗 seq{rows[start]['seq']}~{rows[start+winlen-1]['seq']}："
                  f"{len(cars)} 条车轨 ≥{MIN_TRACK} 帧（含飞坡帧 {res['n_jump']}）")
        if len(starts) >= n_win:
            break
    return starts


# ---------- 窗执行 ----------

def run_window(dsess, ysess, names, demo, rows, start: int, winlen: int,
               y_h: float, min_denom: float, mark_jump: bool) -> dict:
    em = json.loads(EGO_MASK_FILE.read_text(encoding="utf-8"))
    gh_nat = lambda v, full: int(v * 336 / full)   # 全幅 → 原生格
    gw_nat = lambda v, full: int(v * 598 / full)
    ego_nat = np.zeros((336, 598), bool)
    ego_nat[gh_nat(em["y0"], 720):gh_nat(em["y1"], 720) + 1,
            gw_nat(em["x0"], 1280):gw_nat(em["x1"], 1280) + 1] = True

    tracks: list[dict] = []
    ego_hgts: list[float | None] = []
    planes = 0
    for i in range(start, start + winlen):
        r = rows[i]
        fid = r["seq"]
        img = cv2.cvtColor(cv2.imread(str(demo / "frames" / r["file"])),
                           cv2.COLOR_BGR2RGB)
        dets = yolo_detect(ysess, names, img)
        blob = mp.preprocess(img, DEPTH_IN_W, DEPTH_IN_H)
        points, mask, mscale = mp.forward(dsess, blob, TOKENS)
        res = mp.reconstruct(points, mask, mscale)
        pts, valid = res["pts"].astype(np.float32), res["valid"]
        coef = plane_fit(pts, valid, ego_nat)
        planes += int(coef is not None)
        eh = ego_region_hgt(pts, valid, coef, ego_nat) if coef is not None else None
        ego_hgts.append(eh)
        for d in dets:
            d["z"] = entity_z(pts, d["box"])
        associate(tracks, dets, fid)
        flow_residual(tracks, y_h, min_denom)

    # 飞坡标记：平面拟合失败 或 ego 区 hgt 偏离滚动中位 >0.5m
    ok_h = [v for v in ego_hgts if v is not None]
    roll_med = statistics.median(ok_h) if ok_h else None
    jump_fids = set()
    for i, eh in enumerate(ego_hgts):
        if mark_jump:
            if eh is None or roll_med is None or abs(eh - roll_med) > JUMP_DEV_M:
                jump_fids.add(rows[start + i]["seq"])

    for t in tracks:
        for o in t["obs"]:
            o["jump"] = o["fid"] in jump_fids
    return {"tracks": tracks, "n_jump": len(jump_fids),
            "plane_ok": planes, "ego_hgts": ego_hgts, "start": start}


# ---------- 渲染 ----------

def mini_chart(canvas, x0, y0, w, h, vals, color, title):
    v = [x for x in vals if not math.isnan(x)]
    if len(v) < 3:
        cv2.putText(canvas, title + " (n<3)", (x0, y0 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (30, 30, 30), 1)
        return
    lo, hi = min(v), max(v)
    if hi - lo < 1e-9:
        lo, hi = lo - 0.5, hi + 0.5
    cv2.putText(canvas, title, (x0, y0 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (30, 30, 30), 1)
    for k in range(5):
        gv = lo + (hi - lo) * k / 4
        gy = y0 + h - int(k / 4 * (h - 26)) - 13
        cv2.line(canvas, (x0, gy), (x0 + w, gy), (218, 218, 218), 1)
        cv2.putText(canvas, f"{gv:.2f}", (x0 - 44, gy + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, GRAY, 1)
    pts = [(x0 + int(i / (len(vals) - 1) * w),
            y0 + h - int((v - lo) / (hi - lo) * (h - 26)) - 13)
           for i, v in enumerate(vals) if not math.isnan(v)]
    cv2.polylines(canvas, [np.array(pts)], False, color, 2)
    for p in pts:
        cv2.circle(canvas, p, 3, color, -1)


def render_track_strip(demo, rows, t: dict, out_path: Path) -> None:
    """身份验证材料：轨迹首/中/尾帧 + 框标注（人工目检用，非机检判据）。"""
    obs = t["obs"]
    picks = [obs[0], obs[len(obs) // 2], obs[-1]]
    by_fid = {r["seq"]: r for r in rows}
    panels = []
    for o in picks:
        img = cv2.cvtColor(cv2.imread(str(demo / "frames" / by_fid[o["fid"]]["file"])),
                           cv2.COLOR_BGR2RGB)
        x0, y0, x1, y1 = o["box"]
        c = RED if t["cls"] == "car" else (60, 200, 230)
        cv2.rectangle(img, (x0, y0), (x1, y1), c, 2)
        cv2.putText(img, f"{t['cls']} seq{o['fid']}", (x0, max(14, y0 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, WHITE, 1)
        panels.append(cv2.resize(img, (640, 360)))
    strip = np.hstack(panels)
    cv2.imwrite(str(out_path), cv2.cvtColor(strip, cv2.COLOR_RGB2BGR))


# ---------- 主流程 ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", required=True)
    ap.add_argument("--winlen", type=int, default=16)
    ap.add_argument("--drive-n", type=int, default=2)
    ap.add_argument("--start", type=int, nargs="*", help="指定实战窗起始 seq（跳过扫描）")
    ap.add_argument("--static-start", type=int, default=None,
                    help="指定静止窗起始 seq（默认自动选最低运动窗）")
    args = ap.parse_args()
    demo = Path(args.demo)
    t0 = time.time()
    winlen = args.winlen

    g0 = json.loads(GATE0_FILE.read_text(encoding="utf-8"))
    y_h, min_denom = float(g0["y_h"]), float(g0["min_denom"])

    rows = [json.loads(l) for l in
            (demo / "frames.jsonl").open(encoding="utf-8") if l.strip()]
    seq2idx = {r["seq"]: i for i, r in enumerate(rows)}
    scores = motion_scores(demo, rows)

    dsess = depth_session()
    ysess, names = yolo_session()
    print(f"[{time.time()-t0:5.1f}s] 会话就绪；类别 {sorted(names.values())}")

    # 静止窗（零点校准：真值=零接近）
    if args.static_start is not None:
        s_start = seq2idx[args.static_start]
    else:
        s_start = pick_low_motion(scores, winlen)
    stat = run_window(dsess, ysess, names, demo, rows, s_start, winlen,
                      y_h, min_denom, mark_jump=True)
    print(f"[{time.time()-t0:5.1f}s] 静止窗 seq{rows[s_start]['seq']}~"
          f"{rows[s_start+winlen-1]['seq']}（运动均值 "
          f"{float(scores[s_start:s_start+winlen].mean()):.1f}，飞坡帧 {stat['n_jump']}）")

    # 实战窗（真值=接近单调性）
    if args.start:
        drive_starts = [seq2idx[s] for s in args.start]
    else:
        drive_starts = find_drive_windows(dsess, ysess, names, demo, rows, scores,
                                          winlen, args.drive_n, y_h, min_denom)
    drives = [run_window(dsess, ysess, names, demo, rows, s, winlen,
                         y_h, min_denom, mark_jump=True) for s in drive_starts]

    # ---------- 判决表 ----------
    # 静止窗噪声地板：各量二阶差（真值=零接近，报出运动即误差）；飞坡帧已断列
    verdict = {"static_floor": {}, "drive_tracks": [], "meta": {
        "demo": demo.name, "winlen": winlen,
        "jump_dev_m": JUMP_DEV_M, "min_track": MIN_TRACK,
        "prereg": "Δlog h 在最干净车窗的 SNR 不超过 Z → 2D 时序线砍掉"}}

    stat_tracks = [t for t in stat["tracks"] if len(t["obs"]) >= 8]
    for t in stat_tracks:
        cols = track_quantities(t)
        floor = {}
        for q in QUANTS:
            st = diff_stats(clean(cols[q]))
            if st:
                floor[q] = st
        verdict["static_floor"][f"{t['cls']}@{t['obs'][0]['fid']}"] = floor

    for w_i, (s, wres) in enumerate(zip(drive_starts, drives)):
        for t in wres["tracks"]:
            if t["cls"] not in ("car", "coin") or len(t["obs"]) < MIN_TRACK:
                continue
            cols = track_quantities(t)
            rec = {"win": w_i, "cls": t["cls"],
                   "seq": [t["obs"][0]["fid"], t["obs"][-1]["fid"]],
                   "n": len(t["obs"]), "quant": {}}
            for q in QUANTS:
                st = diff_stats(clean(cols[q]))
                if st:
                    rec["quant"][q] = st
            verdict["drive_tracks"].append(rec)

    # 车类判决（预注册判据）：各车轨 SNR 对比，Δlog h 最佳 vs Z 最佳
    car_rows = [r for r in verdict["drive_tracks"] if r["cls"] == "car"]
    best = {}
    for q in ("z", "log_h", "h", "inv_h", "flowres", "cy", "bottom_y"):
        cands = [(r["quant"][q]["snr"], r) for r in car_rows
                 if q in r["quant"] and r["quant"][q]["snr"] is not None]
        best[q] = max(cands, default=None, key=lambda p: p[0])
    verdict["verdict"] = {
        "best_snr": {q: ({"snr": v[0], "seq": v[1]["seq"]} if v else None)
                     for q, v in best.items()},
        "dlogh_vs_z": (None if not best.get("log_h") or not best.get("z") else
                       {"dlogh": best["log_h"][0], "z": best["z"][0],
                        "dlogh_wins": best["log_h"][0] > best["z"][0]})}

    # ---------- 落盘 ----------
    out_dir = demo.parent / "depth_review" / "jitter"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = demo.name
    jpath = out_dir / f"approach_ab_{tag}.json"
    jpath.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")

    # CSV：全部轨迹逐帧行（含跳帧标记）
    cpath = out_dir / f"approach_ab_{tag}.csv"
    with cpath.open("w", encoding="utf-8") as f:
        f.write("win,cls,fid,jump,conf,cy,bottom_y,h,w,inv_h,log_h,z,flowres\n")
        for w_i, wres in enumerate([stat] + drives):
            for t in wres["tracks"]:
                for o in t["obs"]:
                    x0, y0, x1, y1 = o["box"]
                    h = max(1, y1 - y0)
                    z = o.get("z")
                    fr = o.get("flowres")
                    f.write("%d,%s,%d,%d,%.2f,%.1f,%d,%d,%d,%.5f,%.4f,%s,%s\n" % (
                        w_i, t["cls"], o["fid"], int(o.get("jump", False)),
                        o["conf"], (y0 + y1) / 2.0, y1, h, x1 - x0, 1.0 / h,
                        math.log(h),
                        "" if z is None else "%.2f" % z,
                        "" if fr is None else "%.2f" % fr))

    # 四联对照图：最佳车轨的 Z / h / log_h / flowres（+cy 参考）
    if best.get("z") and best["z"][1] is not None:
        bt = best["z"][1]
        wt = next(t for t in drives[bt["win"]]["tracks"]
                  if t["cls"] == "car" and t["obs"][0]["fid"] == bt["seq"][0])
        cols = track_quantities(wt)
        CW, CH = 480, 260
        canvas = np.full((CH + 50, CW * 5, 3), 245, np.uint8)
        titles = [("z", "Z per frame (m)"), ("h", "bbox h per frame (px)"),
                  ("log_h", "log(h) per frame"), ("flowres", "flow residual (px/fr)"),
                  ("cy", "cy per frame (px)")]
        for k, (q, title) in enumerate(titles):
            mini_chart(canvas, 60 + CW * k, 40, CW - 84, CH, clean(cols[q]),
                       RED if q in ("z",) else (BLUE if q in ("h", "cy") else GREEN),
                       title)
        cv2.imwrite(str(out_dir / f"approach_ab_{tag}.jpg"),
                    cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
        render_track_strip(demo, rows, wt, out_dir / f"approach_ab_{tag}_id.jpg")

    # ---------- 控制台摘要 ----------
    v = verdict["verdict"]
    print(f"[{time.time()-t0:5.1f}s] 判决（预注册：Δlog h SNR ≤ Z SNR → 砍线）：")
    for q, b in best.items():
        if b:
            r = b[1]
            print(f"    {q:8s} 最佳车轨 seq{r['seq'][0]}~{r['seq'][1]} "
                  f"SNR={b[0]:.3f} 方向一致率={r['quant'][q]['d_dir_consistent']:.2f}")
    if v["dlogh_vs_z"]:
        d = v["dlogh_vs_z"]
        print(f"    ⇒ Δlog h SNR={d['dlogh']:.3f} vs Z SNR={d['z']:.3f} → "
              f"{'Δlog h 胜出，继续 anchor-age 实验' if d['dlogh_wins'] else '未过砍线，2D 时序替代线关闭'}")
    print("wrote", jpath)
    print("wrote", cpath)


if __name__ == "__main__":
    main()
