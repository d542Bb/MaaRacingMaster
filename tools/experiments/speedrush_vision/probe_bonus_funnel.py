# -*- coding: utf-8 -*-
"""奖励车漏斗重放（R5 层 1 A/B 归因）：实机局 raw 帧过感知层（YOLO 同源），
统计奖励车检出落点分布——横位适用域（denom≥min_denom，即 cy≥y_h+20）、
地平线以上丢弃带（denom≤0）、中间弱读带——判别「层 1 一次不生效」死在哪级。

背景：20261017 1824~1829 六局 cr_bonus 全 0。YOLO 检出奖励车（27/76 帧）
≠ 横位可读——x_lane 只在 cy≥y_h+min_denom 才给，奖励车若整个生命周期都在
远场，决策的 obs.targets 循环永远见不到 bonus kind。

只读 debug 产物，不改生产。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    CLASS_BONUS, StreetPerception)
from maaracing_master.plugins.speedrush.world_model import (  # noqa: E402
    KIND_BONUS, load_calib)

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
SESS = ["182430_p1", "182514_p2", "182633_p1", "182717_p2", "182839_p1",
        "182923_p2", "170531_p1", "170616_p2"]


def main() -> None:
    cal = load_calib()
    perc = StreetPerception(str(
        Path("D:/maaracing_assistant/maaracing_master/plugins/speedrush")
        / "resources" / "onnx" / "perception" / "model.onnx"))
    y_h, min_denom = cal.y_h, cal.min_denom
    print(f"y_h={y_h:.1f} min_denom={min_denom} 横位门槛 cy>={y_h + min_denom:.0f}")
    for s in SESS:
        raw = DBG / f"20261017_{s}" / "raw"
        if not raw.is_dir():
            continue
        n_frame = n_bonus = 0
        bands = {"地平线上(denom<=0)": 0, "弱读带(0<denom<min)": 0,
                 "横位域(>=min_denom)": 0}
        cys = []
        for jpg in sorted(raw.glob("*_raw.jpg")):
            img = cv2.imread(str(jpg))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            res = perc.detect(rgb)
            n_frame += 1
            for d in res.bonuses:
                n_bonus += 1
                denom = d.cy - y_h
                if denom <= 0:
                    bands["地平线上(denom<=0)"] += 1
                elif denom < min_denom:
                    bands["弱读带(0<denom<min)"] += 1
                else:
                    bands["横位域(>=min_denom)"] += 1
                cys.append(d.cy)
        cys.sort()
        p50 = cys[len(cys) // 2] if cys else "-"
        print(f"{s}: 帧={n_frame} bonus检出={n_bonus} cy_P50={p50} "
              f"分布={bands}")


if __name__ == "__main__":
    main()
