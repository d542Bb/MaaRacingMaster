# -*- coding: utf-8 -*-
"""素材生成：从真帧 0641_raw（放弃出价二级确认弹窗）量测「确认」红钮并裁模板。

量法：在弹窗下半区扫高饱和红（HSV），取最大连通红块的外接框 → 1:1 裁 png。
"""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("PASS_FRAME", os.path.join(HERE, "pass_dialog_0641.jpg"))
OUT_DIR = os.path.join(HERE, "..", "..", "..",
                      "maaracing_master", "plugins", "treasure", "resources", "image")
frame = cv2.imread(SRC)
if frame is None:
    sys.exit(f"读不到真帧: {SRC}")
H, W = frame.shape[:2]
print("帧尺寸", W, H)

# 扫域=整张裁剪（弹窗区）， HSV 红阈值由量测确定（按钮 221×58px，S≥150 V≥140）
hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
red = (((hsv[:, :, 0] <= 10) | (hsv[:, :, 0] >= 170)) &
       (hsv[:, :, 1] >= 150) & (hsv[:, :, 2] >= 140)).astype(np.uint8) * 255
n, labels, stats, _ = cv2.connectedComponentsWithStats(red, 8)
if n <= 1:
    sys.exit("没找到红块")
i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
rx, ry, rw, rh, area = stats[i]
print(f"最大红块 x={rx} y={ry} w={rw} h={rh} area={area} "
      f"裁剪内归一化=({rx/W:.3f},{ry/H:.3f})-({(rx+rw)/W:.3f},{(ry+rh)/H:.3f})")
for j in range(1, n):
    if stats[j, cv2.CC_STAT_AREA] > 300:
        print(f"  其余红块: x={stats[j,0]} y={stats[j,1]} w={stats[j,2]} h={stats[j,3]} area={stats[j,4]}")

tpl = frame[ry:ry + rh, rx:rx + rw]
dst = os.path.normpath(os.path.join(OUT_DIR, "bid_pass_confirm_btn.png"))
cv2.imwrite(dst, tpl)
print("模板已裁", dst, tpl.shape)
prev = cv2.resize(tpl, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
cv2.imwrite(os.path.join(HERE, "tpl_preview.png"), prev)
