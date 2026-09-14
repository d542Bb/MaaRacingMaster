"""终判：用运行时同款 EggRewardRecognizer 全流程（候选→NMS→判色→计数区）跑奖励弹窗真帧。"""
import os
import sys
import numpy as np
from PIL import Image, ImageDraw

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, REPO)

from maaracing_master.plugins.treasure.eggs import EggRewardRecognizer
from maaracing_master.plugins.treasure.ocr import TreasureOcr

SRC = r"C:\Users\yomen\.trae-cn\attachments\6aa63e48755e399fe6bf9e35"
FIG = "a3d054d3-9841-4523-8b19-753537116e8f_4344ae7e-9dde-475d-bb61-ddb046c6acc1_46fe911ba860e8b3c788922d774f889f.png"
OUT = r"C:\Users\yomen\AppData\Local\Temp\egg_silver_check"
os.makedirs(OUT, exist_ok=True)

frame = np.array(Image.open(os.path.join(SRC, FIG)).convert("RGB"))
H, W = frame.shape[:2]

rec = EggRewardRecognizer(REPO, ocr=TreasureOcr(REPO))  # 真 OCR，读 ×N 计数
print("configured:", rec.configured)
res = rec.recognize(frame)
if res is None:
    print("recognize 返回 None（未配置）")
    raise SystemExit(0)
print("counts:", res["counts"])
vis = Image.fromarray(frame)
d = ImageDraw.Draw(vis)
for e in res["eggs"]:
    b = e["box"]
    c = e["count_rect"]
    print(f"  {e['color']:<6} score={e['score']:.3f} box=({b[0]:.3f},{b[1]:.3f},{b[2]:.3f},{b[3]:.3f}) "
          f"count_rect=({c[0]:.3f},{c[1]:.3f},{c[2]:.3f},{c[3]:.3f}) text={e['count_text']!r} -> {e['count']}")
    d.rectangle([b[0] * W, b[1] * H, b[2] * W, b[3] * H], outline=(0, 255, 0), width=2)
    d.rectangle([c[0] * W, c[1] * H, c[2] * W, c[3] * H], outline=(255, 0, 0), width=2)
    crop = frame[int(c[1] * H):int(c[3] * H), int(c[0] * W):int(c[2] * W)]
    Image.fromarray(crop).resize(((c[2] - c[0]) * W * 3, (c[3] - c[1]) * H * 3)).save(
        os.path.join(OUT, f"cnt_{e['color']}.png"))
vis.save(os.path.join(OUT, "egghits.png"))
print("saved", os.path.join(OUT, "egghits.png"))
