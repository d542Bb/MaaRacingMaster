import os
import cv2
import numpy as np
from PIL import Image

SRC = r"C:\Users\yomen\.trae-cn\attachments\6aa63e48755e399fe6bf9e35"
FIG = "a3d054d3-9841-4523-8b19-753537116e8f_4344ae7e-9dde-475d-bb61-ddb046c6acc1_46fe911ba860e8b3c788922d774f889f.png"
DST = r"D:\maaracing_assistant\maaracing_master\plugins\treasure\resources\image"
OUT = r"C:\Users\yomen\AppData\Local\Temp\egg_silver"

frame = np.array(Image.open(os.path.join(SRC, FIG)).convert("RGB"))
gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

# 精裁：银币 medal 与积分 medal（取币心，避开计数文字与角标）
SILVER_MEDAL = (862, 268, 948, 352)
SCORE_MEDAL = (468, 268, 552, 352)

for tag, box in [("claim_coin", SILVER_MEDAL), ("claim_score", SCORE_MEDAL)]:
    x1, y1, x2, y2 = box
    Image.fromarray(frame[y1:y2, x1:x2]).save(os.path.join(DST, f"{tag}_medal.png"))
    big = cv2.resize(gray[y1:y2, x1:x2], None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(os.path.join(OUT, f"v_{tag}.png"), cv2.cvtColor(big, cv2.COLOR_GRAY2BGR))
    print(tag, box, "saved")

# 自匹配 + 互斥复核（各自 ROI 内）
def self_cross(tpl_box, name):
    tx1, ty1, tx2, ty2 = tpl_box
    tpl = gray[ty1:ty2, tx1:tx2]
    def best(roi):
        rx1, ry1, rx2, ry2 = roi
        sub = gray[ry1:ry2, rx1:rx2]
        b = 0.0
        for i in range(13):
            s = 0.7 + i * 0.05
            nw, nh = int(round(tpl.shape[1]*s)), int(round(tpl.shape[0]*s))
            if nh > sub.shape[0] or nw > sub.shape[1]:
                continue
            t = cv2.resize(tpl, (nw, nh), interpolation=cv2.INTER_CUBIC)
            b = max(b, float(cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED).max()))
        return b
    self = best((tx1-15, ty1-15, tx2+15, ty2+15))
    other = best((SILVER_MEDAL if name == "score" else SCORE_MEDAL))
    print(f"{name}: self={self:.3f} other={other:.3f} margin={self-other:.3f}")

self_cross(SILVER_MEDAL, "silver")
self_cross(SCORE_MEDAL, "score")
