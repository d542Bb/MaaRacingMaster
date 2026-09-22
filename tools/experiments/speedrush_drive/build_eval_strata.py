# -*- coding: utf-8 -*-
"""评测集分层器 v2（只读）：按**回合**判天气，天气从 **p1 开头**读（维护者口径：
p1/p2 共用一个天气，且 p1 最开始最稳——中帧/ p2 可能已进隧道或转场，判歪）。

产出：
  - eval_strata.jsonl：每回合 {round(p1会话), p2会话, 开头帧代理量, 天气桶}
  - _peek/weather_rounds.jpg：每回合 p1 开头帧一格，供目视逐回合确认分桶。

代理量（下采样算）：整体亮度 / 天空区亮度 / 饱和 / 暖色占比(黄昏) / 高亮反光占比 /
镜头水珠占比(雨：路面上半区孤立亮斑)。**分桶阈值是起值，以目视拼图为准校正**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core.paths import data_dir

START_LO, START_HI = 3, 12   # 跳过开头淡入，取第 3~11 帧判天气


def lighting_proxy(img: np.ndarray) -> dict:
    h, w = img.shape[:2]
    nw = 320
    small = cv2.resize(img, (nw, int(h * nw / w)))
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV).astype(np.float32)
    v, s, hue = hsv[:, :, 2], hsv[:, :, 1], hsv[:, :, 0]
    sky = cv2.cvtColor(small[: int(small.shape[0] * 0.35)],
                       cv2.COLOR_RGB2HSV)[:, :, 2].mean()
    warm = float(np.mean((hue >= 5) & (hue <= 30) & (v > 180)))
    glare = float(np.mean(v > 235))
    # 镜头水珠（雨）：路面上半区里孤立、近白、小而圆的亮斑——用形态学找小白点
    road = hsv[int(small.shape[0] * 0.35):, :, :]
    drop_mask = ((road[:, :, 2] > 200) & (road[:, :, 1] < 45)).astype(np.uint8)
    n, *_ = cv2.connectedComponentsWithStats(drop_mask)
    drop_frac = float(np.mean(drop_mask))
    return {"mean_v": round(float(v.mean()), 1), "sky_v": round(float(sky), 1),
            "mean_s": round(float(s.mean()), 1), "warm_frac": round(warm, 3),
            "glare_frac": round(glare, 3), "drop_frac": round(drop_frac, 4),
            "n_drops": n - 1}


def bucket(p: dict) -> str:
    # 顺序：雨（水珠+偏暗）→ 夜(若有) → 黄昏(暖) → 阴(低饱和) → 白天
    if p["drop_frac"] > 0.004 or (p["mean_v"] < 95 and p["mean_s"] < 60 and p["warm_frac"] < 0.03):
        return "rain"
    if p["mean_v"] < 55:
        return "night"
    if p["warm_frac"] > 0.06:
        return "dusk"
    if p["mean_s"] < 55:
        return "overcast"
    return "day"


def main() -> None:
    demos = data_dir() / "speedrush" / "demos"
    p1s = sorted(d for d in demos.iterdir() if d.name.endswith("_p1") and (d / "frames").is_dir())
    rows, thumbs = [], []
    for sd in p1s:
        p2 = sd.with_name(sd.name[:-3] + "_p2")
        frames = sorted((sd / "frames").glob("*.jpg"))
        seg = frames[START_LO:START_HI] or frames[:3]
        feats = []
        for fp in seg:
            img = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
            if img is not None:
                feats.append(lighting_proxy(img))
        if not feats:
            continue
        agg = {k: round(float(np.mean([f[k] for f in feats])), 4) for k in feats[0]}
        b = bucket(agg)
        rows.append({"round": sd.name, "p2": p2.name if p2.is_dir() else None,
                     "bucket": b, "n_frames": len(frames), **agg})
        mid = cv2.imread(str(seg[len(seg) // 2]))
        thumbs.append((sd.name, b, mid))
        print(f"  {sd.name:26} {b:9} v={agg['mean_v']:5.0f} s={agg['mean_s']:4.0f} "
              f"warm={agg['warm_frac']:.2f} drop={agg['drop_frac']:.3f} ndrop={agg['n_drops']:.0f}")

    from collections import Counter
    c = Counter(r["bucket"] for r in rows)
    print("\n=== 回合级天气覆盖 ===")
    for k in ("day", "overcast", "dusk", "rain", "night"):
        print(f"  {k:9} {c.get(k, 0)}")
    out = Path(__file__).parent / "eval_strata.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n回合清单 → {out}")

    # 拼图：每回合一格（p1 开头帧），标序号+桶
    peek = Path(__file__).parent / "_peek"
    peek.mkdir(exist_ok=True)
    cols = 6
    n = len(thumbs)
    rws = (n + cols - 1) // cols
    cw, ch = 320, 180
    canvas = np.full((rws * ch, cols * cw, 3), 30, np.uint8)
    for k, (nm, b, im) in enumerate(thumbs):
        if im is None:
            continue
        y, x = divmod(k, cols)
        canvas[y * ch:(y + 1) * ch, x * cw:(x + 1) * cw] = cv2.resize(im, (cw, ch))
        cv2.putText(canvas, f"{k} {b}", (x * cw + 4, y * ch + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
    mp = peek / "weather_rounds.jpg"
    cv2.imwrite(str(mp), canvas)
    print(f"回合拼图 → {mp}")


if __name__ == "__main__":
    sys.exit(main())
