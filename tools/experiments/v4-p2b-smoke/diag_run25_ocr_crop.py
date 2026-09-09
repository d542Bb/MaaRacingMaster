# -*- coding: utf-8 -*-
"""run25：槽位 OCR 0 命中取证——用真机 raw 帧按 assets rect 裁剪，肉眼核对位置。

背景：20:13 复跑日志实锤「智能出价后只静默确认、从不输数字」，链上
bid_player1-4 槽三口径 hits=0（输出有、命中无），bid_result_amount_box 能读到。
假设：raw 帧坐标系与 rect 校准坐标系不一致（WGC 修复前后 759 高 vs 720 高）。
本脚本不跑 OCR，只裁剪存图：rect 若盖错位置，图上一看即知。
"""
from __future__ import annotations

import os
import json
import sys
from pathlib import Path

import cv2
import numpy as np

DEBUG_DIR = Path(os.environ["APPDATA"]) / "MaaRacingAssistant" / "debug/treasure/20260909_201348"
ASSETS = Path(__file__).resolve().parents[3] / "maaracing_assistant/plugins/treasure/resources/config/treasure_assets.json"
OUT = Path(__file__).parent / "run25_out"
KEYS = ["bid_result_amount_box", "bid_player1", "bid_player2", "bid_player3", "bid_player4"]


def crop_rect(img, rect, pad=0.02):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = rect
    m = ((int((x1 - pad) * w), int((y1 - pad) * h)), (int((x2 + pad) * w), int((y2 + pad) * h)))
    return img[max(m[0][1], 0):m[1][1], max(m[0][0], 0):m[1][0]], m


def main():
    frames = sys.argv[1:] or ["1276"]
    assets = json.loads(ASSETS.read_text(encoding="utf-8"))
    rois = assets["anchors"]
    print(f"assets.reference_size = {assets.get('reference_size')}")
    OUT.mkdir(exist_ok=True)
    for fid in frames:
        raw = DEBUG_DIR / "raw" / f"{int(fid):04d}_raw.jpg"
        if not raw.exists():
            print(f"[skip] {raw} 不存在")
            continue
        img = cv2.imread(str(raw))
        h, w = img.shape[:2]
        print(f"帧#{fid}: {w}x{h}")
        # 全帧标注 rect 框（红=rect 位置，框内应有目标文字）
        ann = img.copy()
        for k in KEYS:
            x1, y1, x2, y2 = rois[k]["rect"]
            cv2.rectangle(ann, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (0, 0, 255), 2)
            cv2.putText(ann, k, (int(x1 * w), max(int(y1 * h) - 4, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        cv2.imwrite(str(OUT / f"{fid}_annot.jpg"), ann)
        for k in KEYS:
            c, m = crop_rect(img, rois[k]["rect"])
            c = cv2.resize(c, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
            cv2.imwrite(str(OUT / f"{fid}_{k}.jpg"), c)
            print(f"  {k}: 裁剪区 y={m[0][1]}..{m[1][1]} x={m[0][0]}..{m[1][0]} 尺寸={c.shape[1]}x{c.shape[0]}")
    print(f"输出目录: {OUT}")


if __name__ == "__main__":
    main()
