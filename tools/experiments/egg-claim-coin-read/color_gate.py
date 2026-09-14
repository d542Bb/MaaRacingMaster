"""验证：奖励弹窗五张卡的「中心40% HSV」在 EggRewardRecognizer 判色口径下的落点。

风险场景：通用蛋模板对币章灰度命中 0.943（studio 实测），若币章中心均值 S≥60
且色相落进红/黄/蓝区间 → 数蛋会把币当蛋。此脚本用 eggs.py 同一函数直接判。
"""
import os
import numpy as np
from PIL import Image
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from maaracing_master.plugins.treasure.eggs import _classify_egg_color, COLOR_CENTER_RATIO

SRC = r"C:\Users\yomen\.trae-cn\attachments\6aa63e48755e399fe6bf9e35"
FIG = "a3d054d3-9841-4523-8b19-753537116e8f_4344ae7e-9dde-475d-bb61-ddb046c6acc1_46fe911ba860e8b3c788922d774f889f.png"

frame = np.array(Image.open(os.path.join(SRC, FIG)).convert("RGB"))
H, W = frame.shape[:2]
import cv2
hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)

# 五张卡的蛋/币图标框（像素 x1y1x2y2，studio 网格标定）
CARDS = {
    "红蛋": (315, 268, 400, 352),
    "积分币章": (468, 268, 552, 352),
    "蓝蛋": (585, 268, 668, 352),
    "黄蛋": (703, 268, 790, 352),
    "银币币章": (862, 268, 948, 352),
}
for tag, (x1, y1, x2, y2) in CARDS.items():
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    bw, bh = (x2 - x1) * COLOR_CENTER_RATIO, (y2 - y1) * COLOR_CENTER_RATIO
    block = frame[int(cy - bh / 2):int(cy + bh / 2), int(cx - bw / 2):int(cx + bw / 2)]
    m = block.reshape(-1, 3).mean(axis=0)
    h, s, v = hsv[int(cy - bh / 2):int(cy + bh / 2), int(cx - bw / 2):int(cx + bw / 2)].reshape(-1, 3).mean(axis=0)
    cls = _classify_egg_color(block)
    print(f"{tag}: 中心均值HSV=({h:.0f},{s:.0f},{v:.0f}) 判色={cls}")
