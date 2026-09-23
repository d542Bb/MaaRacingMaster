"""金标评分：金标边界线 vs 无锚 region 输出的逐行偏差（第一关判据底座）。

金标 = 每侧两点的直线（3D 直线投影）。region = 触边跨行最大非地面块的内沿
（唯一实现：probe_step_detect.region_inner，不在本文件复刻）。
偏差 dev = region 内沿 x − 金标线 x（同 y；正 = 传感器在金标外侧）。
比较行带 y∈{560,600,640}（近带，深度可靠区 d>2）。
口径提醒（README 金标节）：30+ 帧只能证伪不能证明；按层报中位/p90，
逐帧 |dev|>120px 列嫌疑（传感器错或标注错，人眼裁决）。

用法：python tools/experiments/speedrush_vision/gold_score.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_depth as pd  # noqa: E402
import probe_crop_quality as pcq  # noqa: E402
import probe_step_detect as psd  # noqa: E402

OUT = pd.OUT
YS = (560, 600, 640)


def gold_x_at(r, side, y):
    if r[side + "cls"] == "skip":
        return None
    nx, ny = float(r[side + "_nx"]), float(r[side + "_ny"])
    fx, fy = float(r[side + "_fx"]), float(r[side + "_fy"])
    if abs(ny - fy) < 1:
        return None
    return nx + (y - ny) * (fx - nx) / (fy - ny)


def main() -> None:
    labels = list(csv.DictReader((OUT / "gold_labels.csv").open(encoding="utf-8")))
    sess = None
    rows = []
    for r in labels:
        p = Path(r["path"])
        key = pcq.frame_key(p)
        cache = psd.NPY / f"{key}__da2s.npy"
        if cache.exists():
            m = np.load(cache).astype(np.float32)
        else:
            if sess is None:
                sess = psd._folded_sess(518)
            m = pd.depth_map(sess, cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB), 518)
            np.save(cache, m.astype(np.float16))
        blobs = psd.region_inner(m, psd.road_range(m))
        for side in ("l", "r"):
            b = blobs[side.upper()]
            if b is None:
                continue
            near = [y for y in sorted(b[0]) if 560 <= y <= 700]
            far = [y for y in sorted(b[0]) if 430 <= y < 560]
            for band, cand in (("near", near), ("far", far)):
                if not cand:
                    continue
                picks = cand[:: max(len(cand) // 3, 1)][:3]
                for y in picks:
                    x = gold_x_at(r, side, y)
                    if x is None:
                        continue
                    rows.append({"stratum": r["stratum"], "frame": p.name, "side": side,
                                 "band": band, "y": y, "gold": x, "region": b[0][y],
                                 "dev": b[0][y] - x})
    with (OUT / "gold_score.csv").open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    print(f"== 金标 vs region（{len(rows)} 样本，dev=region−金标，正=传感器在外侧）==")
    for band in ("near", "far"):
        sub = [r for r in rows if r["band"] == band]
        if not sub:
            continue
        print(f"-- {band} 带（near=控制相关；far=暴露远带污染）--")
        for st in sorted({r["stratum"] for r in sub}):
            v = np.array([r["dev"] for r in sub if r["stratum"] == st])
            print(f"{st:12s} n={len(v):4d}  dev中位={np.median(v):+6.0f}px  "
                  f"p90|dev|={np.percentile(np.abs(v), 90):5.0f}px")
    sus = {}
    for r in rows:
        if r["band"] == "near" and abs(r["dev"]) > 120:
            sus.setdefault((r["frame"], r["side"]), []).append(r["dev"])
    print(f"-- near 带 |dev|>120px 嫌疑（{len(sus)} 个 帧×侧）--")
    for (fr, side), ds in sorted(sus.items()):
        print(f"  {fr} {side.upper()}: dev={np.median(ds):+.0f}px (n={len(ds)})")


if __name__ == "__main__":
    main()
