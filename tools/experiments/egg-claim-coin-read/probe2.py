"""实验 v2：银币 vs 积分 的稳健区分。

假设：银币卡=紫底、积分卡=灰底。测三条：
A) 正确坐标下，各自 medal 模板匹配自己卡 vs 对卡的分数（区分度）。
B) 两卡图标区周围背景的平均 HSV（紫 vs 灰 能否拉开）。
C) 计数区 OCR 取数是否可行（打印 ×N 裁剪区）。
"""
import os
import cv2
import numpy as np
from PIL import Image

SRC = r"C:\Users\yomen\.trae-cn\attachments\6aa63e48755e399fe6bf9e35"
FIG = "a3d054d3-9841-4523-8b19-753537116e8f_4344ae7e-9dde-475d-bb61-ddb046c6acc1_46fe911ba860e8b3c788922d774f889f.png"
OUT = r"C:\Users\yomen\AppData\Local\Temp\egg_silver"
os.makedirs(OUT, exist_ok=True)

frame = np.array(Image.open(os.path.join(SRC, FIG)).convert("RGB"))
gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
H, W = frame.shape[:2]

# 卡中心（从放大图判读）：积分卡 coin≈(505,330) 银币卡 coin≈(905,330)，×N 文本在下方 y≈375
SCORE_COIN = (470, 300, 545, 365)
SILVER_COIN = (868, 300, 945, 365)


def multi_best(tpl_box, roi_box):
    tx1, ty1, tx2, ty2 = tpl_box
    tpl = gray[ty1:ty2, tx1:tx2]
    rx1, ry1, rx2, ry2 = roi_box
    sub = gray[ry1:ry2, rx1:rx2]
    th0, tw0 = tpl.shape[:2]
    best = 0.0
    for i in range(13):
        s = 0.7 + i * 0.05
        nw, nh = int(round(tw0 * s)), int(round(th0 * s))
        if nh > sub.shape[0] or nw > sub.shape[1]:
            continue
        t = cv2.resize(tpl, (nw, nh), interpolation=cv2.INTER_CUBIC)
        try:
            res = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
        except cv2.error:
            continue
        best = max(best, float(res.max()))
    return best


row = (300, 290, 1010, 375)   # 整个奖励卡行图标带
print("== A) medal 模板区分度 ==")
print(f"积分模板→积分卡 {multi_best(SCORE_COIN, (450, 290, 560, 375)):.3f} | 积分模板→银卡带 {multi_best(SCORE_COIN, (850, 290, 960, 375)):.3f}")
print(f"银币模板→银卡   {multi_best(SILVER_COIN, (850, 290, 960, 375)):.3f} | 银币模板→积分带 {multi_best(SILVER_COIN, (450, 290, 560, 375)):.3f}")
print(f"积分模板→整行最高 {multi_best(SCORE_COIN, row):.3f} @需看落点")
print(f"银币模板→整行最高 {multi_best(SILVER_COIN, row):.3f}")

print("== B) 背景 HSV（图标框上下左右各取一圈边带，避开源币本体）==")


def bg_hue(box):
    x1, y1, x2, y2 = box
    # 取卡左右两侧竖条背景
    pad = 20
    left = hsv[max(0, y1):y2, max(0, x1 - pad):x1]
    right = hsv[y1:y2, x2:min(W, x2 + pad)]
    ring = np.concatenate([left.reshape(-1, 3), right.reshape(-1, 3)], axis=0)
    hmean = float(ring[:, 0].mean())
    smean = float(ring[:, 1].mean())
    vmean = float(ring[:, 2].mean())
    return hmean, smean, vmean


sh, ss, sv = bg_hue(SCORE_COIN)
vh, vs, vv = bg_hue(SILVER_COIN)
print(f"积分卡背景 H={sh:.0f} S={ss:.0f} V={sv:.0f}")
print(f"银币卡背景 H={vh:.0f} S={vs:.0f} V={vv:.0f}")
print(f"→ 饱和度差 {abs(vs - ss):.0f}（紫底应远高于灰底），色相 银{vh:.0f} vs 积{sh:.0f}")

print("== C) 计数区裁剪（供 OCR 目视）==")
for tag, box in [("cnt_score", (450, 368, 560, 398)), ("cnt_silver", (845, 368, 975, 398))]:
    x1, y1, x2, y2 = box
    Image.fromarray(frame[y1:y2, x1:x2]).resize(((x2 - x1) * 3, (y2 - y1) * 3)).save(os.path.join(OUT, tag + ".png"))
    print(tag, box)
print("done")
