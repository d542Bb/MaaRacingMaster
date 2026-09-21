"""街车检测充分性重测：旧 racing 模型在全部 speedrush demo 素材上的覆盖率/稳定性。

**要回答的问题**（C 类）：阶段 B 感知是否可直接复用 `archive/racing/resources/onnx/model.onnx`
的 `car` 类，还是需要重训。speedrush_vision 实验（2026-09-18）只给了 60 帧的"能不能认"
初判（car 最高分中位 0.765）；本探针回答另一半——**画面里有街车时是否稳定认得出、
跨场次是否一致**。

**方法**：逐帧全图推理（阈值口径 = 旧栈 CLASS_CONF 生效值 0.35、NMS iou 0.45，RGB 640
letterbox，走 `core.yolo_detector` 与阶段 B 同一条路），每帧记录 car 框
（cx, cy, bw, bh, conf）与 coin/bonus 计数。派生指标（analyze 模式）：
- 覆盖率代理：有 car 检出帧占比（按场）——低检出场次回看目视甄别"没目标"还是"认不出"；
- 稳定性：场间检出率离散度 + 贪心最近邻轨迹平均长度（帧间中心距关联）；
- 空间 sanity：框中心 y 分布（旧栈 ROI 语义带 y∈[201,561]）；自车嫌疑区
  （cx∈[600,680] 且 cy>600，追车相机把自车钉在画面中央）单独计数。

用法（在仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_streetcar_coverage.py scan
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_streetcar_coverage.py analyze

输出（数据目录，不入库）：`<APPDATA>/MaaRacingMaster/data/speedrush/streetcar_coverage.jsonl`
与终端汇总表。素材目录：同数据目录下 `demos/<session>/frames/*.jpg`（recorder 导出，
已裁好的 1280×720 客户区帧，无需再裁窗口偏移）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402

_DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = _DATA / "demos"
OUT = _DATA / "streetcar_coverage.jsonl"
MODEL = str(ROOT / "archive" / "racing" / "resources" / "onnx" / "model.onnx")
# 旧栈 CLASS_CONF 生效口径：三类同为 0.35（参数化后由 conf 对全部类别表达）
CONF = 0.35
BAND_Y = (201, 561)          # 旧栈 ROI 语义带（road 可见区）
EGO_X = (600, 680)           # 自车嫌疑区：cx 带 + 下半屏
EGO_Y_MIN = 600


def cmd_scan(only: str = "", limit: int = 0) -> None:
    sessions = sorted(d for d in DEMOS.iterdir() if (d / "frames").is_dir())
    if only:
        sessions = [s for s in sessions if only in s.name]
    print(f"场次: {len(sessions)} → {OUT}")
    det = YOLODetector(MODEL, conf=CONF, iou=0.45)
    t_all = time.time()
    with open(OUT, "w", encoding="utf-8") as f:
        for si, sess in enumerate(sessions):
            frames = sorted((sess / "frames").glob("*.jpg"))
            if limit:
                frames = frames[:limit]
            t0 = time.time()
            for seq, fp in enumerate(frames):
                img = cv2.imread(str(fp))
                if img is None:
                    continue
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                by_class, dets, _raw = det(rgb)
                cars = []
                for d in dets:
                    if d["class_name"] != "car":
                        continue
                    x1, y1, x2, y2 = d["box"]
                    cars.append([(x1 + x2) // 2, (y1 + y2) // 2, x2 - x1, y2 - y1,
                                 round(d["confidence"], 3)])
                rec = {"sess": sess.name, "seq": seq, "car": cars,
                       "n_coin": len(by_class["coin"]), "n_bonus": len(by_class["bonus_car"])}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{si+1}/{len(sessions)}] {sess.name}: {len(frames)} 帧 {time.time()-t0:.1f}s",
                  flush=True)
    print(f"全部完成 {time.time()-t_all:.0f}s")


def _track_lens(cars_by_seq: list[list]) -> list[int]:
    """贪心最近邻：帧间与既有轨迹末尾中心距 < 120px 则延续，否则新轨迹。返回轨迹长度列表。"""
    live: dict[int, tuple[int, int]] = {}   # tid -> (cx, cy)
    lens: dict[int, int] = {}
    tid = 0
    for cars in cars_by_seq:
        used = set()
        for (cx, cy, _w, _h, _c) in cars:
            best, bd = None, 120.0
            for t, (px, py) in live.items():
                if t in used:
                    continue
                dd = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
                if dd < bd:
                    best, bd = t, dd
            if best is None:
                live[tid] = (cx, cy)
                lens[tid] = 1
                used.add(tid)
                tid += 1
            else:
                live[best] = (cx, cy)
                lens[best] += 1
                used.add(best)
        # 本帧未关联到的旧轨迹自然消亡（保留长度证据）
        for t in list(live):
            if t not in used:
                del live[t]
    return list(lens.values())


def cmd_analyze() -> None:
    per_sess: dict[str, list[list]] = {}
    with open(OUT, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            per_sess.setdefault(r["sess"], []).append(r["car"])

    rows = []
    for sess, seq_cars in sorted(per_sess.items()):
        n = len(seq_cars)
        hit = sum(1 for c in seq_cars if c)
        allb = [b for c in seq_cars for b in c]
        confs = np.array([b[4] for b in allb]) if allb else np.array([0.0])
        in_band = sum(1 for b in allb if BAND_Y[0] <= b[1] <= BAND_Y[1])
        ego_susp = sum(1 for b in allb if EGO_X[0] <= b[0] <= EGO_X[1] and b[1] > EGO_Y_MIN)
        lens = _track_lens(seq_cars)
        rows.append({
            "sess": sess, "frames": n, "hit_rate": hit / n if n else 0,
            "boxes_per_frame": len(allb) / n if n else 0,
            "conf_med": float(np.median(confs)), "conf_max": float(confs.max()),
            "band_frac": in_band / len(allb) if allb else 0,
            "ego_frac": ego_susp / len(allb) if allb else 0,
            "track_mean": float(np.mean(lens)) if lens else 0,
            "track_max": int(max(lens)) if lens else 0,
        })

    print(f"{'场次':>18s} {'帧':>5s} {'有检出帧占比':>7s} {'框/帧':>5s} "
          f"{'conf中位':>7s} {'conf最大':>7s} {'带内占比':>6s} {'自车嫌疑':>6s} {'轨迹均值':>6s} {'轨迹最长':>6s}")
    for r in rows:
        print(f"{r['sess']:>18s} {r['frames']:>5d} {r['hit_rate']:>10.1%} {r['boxes_per_frame']:>7.2f} "
              f"{r['conf_med']:>8.3f} {r['conf_max']:>8.3f} {r['band_frac']:>8.1%} {r['ego_frac']:>8.1%} "
              f"{r['track_mean']:>8.1f} {r['track_max']:>8d}")
    hr = np.array([r["hit_rate"] for r in rows])
    print(f"\n汇总: 场 {len(rows)} 总帧 {sum(r['frames'] for r in rows)} | "
          f"检出率 均值 {hr.mean():.1%} 中位 {np.median(hr):.1%} 最低 {hr.min():.1%} "
          f"离散 p10-p90 {np.percentile(hr, 10):.1%}-{np.percentile(hr, 90):.1%}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan":
        cmd_scan(only=sys.argv[2] if len(sys.argv) > 2 else "",
                 limit=int(sys.argv[3]) if len(sys.argv) > 3 else 0)
    else:
        cmd_analyze()
