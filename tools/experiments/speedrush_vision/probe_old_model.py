"""旧检测模型在**新录帧**上的可用性探针（不改生产，只做一次测量）。

**要回答什么**：`archive/racing/resources/onnx/model.onnx`（旧赛车栈的 YOLO11n，3 类
`coin/car/bonus_car`）今天还能不能用——即新版本的画面是否还在它的域内。这是收益模型
（`docs/plan/speedrush-control-route.md` 阶段 C）之前的必答题：目标识别不出来，收益表就是空谈。

**为什么"不设阈值看原始分"**：出题是"模型不认"还是"画面里根本没币"，两者的处置完全不同
（前者要重训，后者只要换个采样）。只看 >0.35 的检出数会把这两件事混成一句"零检出"——
第一次冒烟 24 帧零币就是这么被误读的。故本探针按类统计 **class 原始分的 中位/P90/最大**。

**契约（与旧栈运行时一致，逐项对齐）**：旧栈 `core/yolo_detector.py` 喂的是 **RGB**、
640 letterbox（`min(640/h, 640/w)` 缩放 + 114 灰边）、`/255`、NCHW；推理 ROI =
`(0, 201, 1280, 561)`（旧栈 `loop.ROI`）。本探针自建同口径预处理，**不 import core**
（实验自包含约定），故也不碰仓库里的 ort 优化缓存。

用法：
    python tools/experiments/speedrush_vision/probe_old_model.py stats [--per-session 15]
    python tools/experiments/speedrush_vision/probe_old_model.py sheet --out <目录>
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw, ImageFont

DEMOS = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
         / "demos")
DEFAULT_MODEL = (Path(__file__).resolve().parents[3] / "archive" / "racing" / "resources"
                 / "onnx" / "model.onnx")

IMGSZ = 640
CLASSES = {0: "coin", 1: "car", 2: "bonus_car"}
# 阈值表沿用旧栈（core.yolo_detector.CLASS_CONF）：三类同为 0.35
CONF = {0: 0.35, 1: 0.35, 2: 0.35}
COLORS = {0: (255, 215, 0), 1: (60, 160, 255), 2: (220, 0, 220)}
ROI = (0, 201, 1280, 561)


def detect(sess, rgb: np.ndarray, roi=ROI):
    """返回 [(类别, 分数, 框)]，框在**原帧**坐标系（ROI 偏移已还原）。"""
    ox = oy = 0
    if roi is not None:
        x1, y1, x2, y2 = roi
        rgb, ox, oy = rgb[y1:y2, x1:x2], x1, y1
    h, w = rgb.shape[:2]
    scale = min(IMGSZ / h, IMGSZ / w)
    nh, nw = int(h * scale), int(w * scale)
    px, py = (IMGSZ - nw) // 2, (IMGSZ - nh) // 2
    padded = np.full((IMGSZ, IMGSZ, 3), 114, dtype=np.uint8)
    padded[py:py + nh, px:px + nw] = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    blob = padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    preds = sess.run(None, {"images": blob})[0][0].T          # (8400, 4 + n_cls)
    xywh, cls_conf = preds[:, :4], preds[:, 4:]
    scores, classes = cls_conf.max(1), cls_conf.argmax(1)
    out = []
    for cls in CLASSES:
        m = (classes == cls) & (scores > CONF[cls])
        if not m.any():
            continue
        b = xywh[m]
        xy = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2,
                       b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], axis=1)
        keep = cv2.dnn.NMSBoxes(xy.tolist(), scores[m].tolist(), 0.0, 0.45)
        for i in np.array(keep).ravel():
            x1, y1, x2, y2 = xy[int(i)]
            out.append((cls, float(scores[m][int(i)]),
                        (int((x1 - px) / scale + ox), int((y1 - py) / scale + oy),
                         int((x2 - px) / scale + ox), int((y2 - py) / scale + oy))))
    return out


def _springs(clip, every, n):
    """均匀取 n 帧索引（掐掉首尾各 10%，避开起步/结算页）。"""
    lo, hi = int(clip * 0.10), int(clip * 0.90)
    return np.linspace(lo, hi, n).astype(int) if hi > lo else np.array([clip // 2])


def _font(size: int = 15):
    for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def sessions(args) -> list[Path]:
    if args.session:
        return [Path(args.demos) / args.session]
    all_ = sorted(d for d in Path(args.demos).iterdir()
                  if d.is_dir() and (d / "frames.jsonl").exists())
    return all_[:args.limit] if args.limit else all_


def _frames(d: Path):
    return [json.loads(x) for x in
            (d / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]


def cmd_stats(args) -> None:
    sess = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
    top: dict[int, list[float]] = {c: [] for c in CLASSES}
    det = {c: 0 for c in CLASSES}
    det_low = {c: 0 for c in CLASSES}
    n = 0
    for d in sessions(args):
        fr = _frames(d)
        if len(fr) < 50:
            continue
        for i in _springs(len(fr), 1, args.per_session):
            img = np.asarray(Image.open(d / "frames" / fr[int(i)]["file"]).convert("RGB"))
            sub = img[ROI[1]:ROI[3], ROI[0]:ROI[2]]
            preds = sess.run(None, {"images": _blob(sub)})[0][0].T
            # 原始分（不看阈值）——用来区分"模型不认"与"画面里没有"
            sc, cl = preds[:, 4:].max(1), preds[:, 4:].argmax(1)
            n += 1
            for c in CLASSES:
                s = sc[cl == c]
                top[c].append(float(s.max()) if len(s) else 0.0)
                det[c] += int(((sc > CONF[c]) & (cl == c)).sum())
                det_low[c] += int(((sc > 0.15) & (cl == c)).sum())
    print(f"扫 {n} 帧（{len(sessions(args))} 会话 × {args.per_session}，ROI={ROI}，"
          f"阈值 {CONF}）\n")
    print(f"{'类别':>10} {'最高分中位':>10} {'P90':>7} {'最大':>7} "
          f"{'>0.35 框数':>11} {'>0.15 框数':>11}")
    for c, nm in CLASSES.items():
        a = np.array(top[c])
        print(f"{nm:>10} {np.median(a):>10.3f} {np.percentile(a, 90):>7.3f} "
              f"{a.max():>7.3f} {det[c]:>11} {det_low[c]:>11}")
    print("\n判读：某类「中位低 + P90 高 + 有高置信框」= 该类在**画面里有目标时**认得出，"
          "只是多数帧没目标——不是域漂移。")


def _blob(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = min(IMGSZ / h, IMGSZ / w)
    nh, nw = int(h * scale), int(w * scale)
    px, py = (IMGSZ - nw) // 2, (IMGSZ - nh) // 2
    padded = np.full((IMGSZ, IMGSZ, 3), 114, dtype=np.uint8)
    padded[py:py + nh, px:px + nw] = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0


def cmd_sheet(args) -> None:
    """出图供**目视复核**：框是否真的压在金币/街车上（读数正确 ≠ 语义正确）。"""
    sess = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
    out_dir = Path(args.out) if args.out else Path(args.demos).parent / "vision_review"
    out_dir.mkdir(parents=True, exist_ok=True)
    pick: list[tuple[float, Path, int, np.ndarray]] = []
    for d in sessions(args):
        fr = _frames(d)
        if len(fr) < 50:
            continue
        for i in _springs(len(fr), 1, args.per_session):
            img = np.asarray(Image.open(d / "frames" / fr[int(i)]["file"]).convert("RGB"))
            sub = img[ROI[1]:ROI[3], ROI[0]:ROI[2]]
            preds = sess.run(None, {"images": _blob(sub)})[0][0].T
            own = float(preds[:, 4:][:, :1].max())
            pick.append((own, d, int(fr[int(i)]["seq"]), img))
    pick.sort(key=lambda x: -x[0])
    tiles = []
    for s0, d, seq, img in pick[:args.limit_tiles]:
        dets = detect(sess, img)
        t = Image.fromarray(img[ROI[1]:ROI[3]]).convert("RGB")
        dr = ImageDraw.Draw(t)
        for c, s, b in dets:
            bb = (b[0] - ROI[0], b[1] - ROI[1], b[2] - ROI[0], b[3] - ROI[1])
            dr.rectangle(bb, outline=COLORS[c], width=2)
            dr.text((bb[0], max(0, bb[1] - 16)), f"{CLASSES[c]}{s:.2f}",
                    font=_font(), fill=COLORS[c])
        print(f"  {d.name[-8:]} seq={seq} coin原始分={s0:.3f} 框数={len(dets)}")
        tiles.append(t.resize((640, 180)))
    if tiles:
        sheet = Image.new("RGB", (640, 180 * len(tiles)), (15, 15, 15))
        for i, t in enumerate(tiles):
            sheet.paste(t, (0, i * 180))
        dest = out_dir / "probe_sheet.jpg"
        sheet.save(dest, quality=90)
        print(f"拼图：{dest}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["stats", "sheet"])
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--demos", default=str(DEMOS))
    ap.add_argument("--session", default=None)
    ap.add_argument("--limit", type=int, default=4, help="最多扫几个会话")
    ap.add_argument("--per-session", type=int, default=15)
    ap.add_argument("--out", default=None,
                    help="sheet 输出目录（缺省 = demos 同级 vision_review，不入库）")
    ap.add_argument("--limit-tiles", type=int, default=6)
    args = ap.parse_args()
    if not Path(args.model).exists():
        raise SystemExit(f"模型不存在：{args.model}（旧栈权重在 archive/racing/resources/onnx/）")
    (cmd_stats if args.mode == "stats" else cmd_sheet)(args)


if __name__ == "__main__":
    main()