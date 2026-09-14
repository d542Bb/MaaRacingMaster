# -*- coding: utf-8 -*-
"""C 类实验：奖励卡通用几何 —— 命中框(红) → 外扩找卡描边(蓝) → 框内数字/名称区(绿)。

真值帧：claim_popup_0609.png（1281×720 聚合「奖励」弹窗，与运行期 debug 帧同规格）
  卡行 5 张（从左到右）：红色彩蛋×2 / 鉴宝积分×30000 / 蓝色彩蛋×2 / 黄色彩蛋×1 / 鉴宝银币×250000

要回答的三个问题：
  1) 「命中图标框 → 外扩找矩形描边」能不能一套轮廓法通吃（蛋卡+银币卡+积分卡）？
  2) 数字行 / 名称行相对卡框的位置比例是否稳定（det=True 拿行级真值）？
  3) 按推导出的数字带做 rec-only OCR，能不能读对全部 5 个 ×N？
"""
import os
import re
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, REPO)

from maaracing_master.core.template_match import load_template, match_template_cs
from maaracing_master.plugins.treasure.eggs import EggRewardRecognizer, _nms, NMS_IOU_THRESHOLD, MAX_EGGS
from maaracing_master.plugins.treasure.ocr import TreasureOcr

FRAME = cv2.cvtColor(cv2.imread(os.path.join(HERE, "claim_popup_0609.png")), cv2.COLOR_BGR2RGB)
H, W = FRAME.shape[:2]
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

EXPECT = {"红色彩蛋": 2, "鉴宝积分": 30000, "蓝色彩蛋": 2, "黄色彩蛋": 1, "鉴宝银币": 250000}


# ---------------------------------------------------------------------------
# 1) 拿命中框：蛋走运行时同款识别器；medal 走 spec rect + 单模板匹配
# ---------------------------------------------------------------------------
def collect_hits():
    hits = []  # (label, (x1,y1,x2,y2) 像素)
    rec = EggRewardRecognizer(REPO)
    nav = __import__("maaracing_master.plugins.treasure", fromlist=["nav_source"]).nav_source()
    gray = cv2.cvtColor(FRAME, cv2.COLOR_RGB2GRAY)
    tpl, rect, th = rec._entry
    cands = _nms(rec._match_candidates(gray, tpl, rect, W, H, th), NMS_IOU_THRESHOLD, MAX_EGGS)
    for score, box in cands:
        rgb_c, avg = rec._sample_center_rgb(FRAME, box, W, H)
        from maaracing_master.plugins.treasure.eggs import _classify_egg_color
        color = _classify_egg_color(rgb_c)
        if color is None:
            continue
        px = (int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H))
        hits.append((f"egg_{color}", px, score))
    for name, tpl_file, rect in (
        ("medal_coin", "claim_coin_medal.png", (0.2, 0.28, 0.8, 0.6)),
        ("medal_score", "claim_score_medal.png", (0.2, 0.28, 0.8, 0.6)),
    ):
        t = load_template(tpl_file, [os.path.join(REPO, "maaracing_master", "plugins", "treasure", "resources", "image")])
        px_roi = tuple(int(v) for v in (rect[0] * W, rect[1] * H, rect[2] * W, rect[3] * H))
        box, score = match_template_cs(FRAME, t, colorspace="rgb", threshold=0.72, roi=px_roi)
        if box is None:
            print(f"[hits] {name} 未命中")
            continue
        b = (box[0], box[1], box[2], box[3])  # match_template_cs 返回的 box 口径先看打印
        hits.append((name, b, score))
    return hits


# ---------------------------------------------------------------------------
# 2) 外扩找卡描边：Canny + 膨胀闭缝 + 轮廓包围盒 → 枚举全部候选，供选型
# ---------------------------------------------------------------------------
def card_rect_candidates(hit_px, min_ratio=1.6, max_ratio=20.0):
    x1, y1, x2, y2 = hit_px
    hw, hh = x2 - x1, y2 - y1
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    pad = int(max(hw, hh) * 2.2)
    wx1, wy1 = max(0, int(cx) - pad), max(0, int(cy) - pad)
    wx2, wy2 = min(W, int(cx) + pad), min(H, int(cy) + pad)
    win = gray_full[wy1:wy2, wx1:wx2]
    edges = cv2.Canny(win, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    hit_area = hw * hh
    out = []
    for c in contours:
        rx, ry, rw, rh = cv2.boundingRect(c)
        rx += wx1; ry += wy1
        area = rw * rh
        ratio = area / hit_area
        if not (min_ratio <= ratio <= max_ratio):
            continue
        # 命中框中心必须在候选内，且候选不能横向"吞并"邻卡（宽度 ≤ 1.9× 命中框宽）
        if not (rx <= cx <= rx + rw and ry <= cy <= ry + rh):
            continue
        if rw > hw * 1.9:
            continue
        out.append((rx, ry, rw, rh))
    out.sort(key=lambda r: r[2] * r[3])
    return out


gray_full = cv2.cvtColor(FRAME, cv2.COLOR_RGB2GRAY)

hits = collect_hits()
print(f"命中 {len(hits)} 个图标框:")
cards = []
vis = Image.fromarray(FRAME).convert("RGB")
d = ImageDraw.Draw(vis)
for label, (x1, y1, x2, y2), score in hits:
    d.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=2)
    cands = card_rect_candidates((x1, y1, x2, y2))
    print(f"  {label:<12} hit=({x1},{y1},{x2},{y2}) score={score:.3f}")
    for (rx, ry, rw, rh) in cands:
        ar = "宽×高=%d×%d 高宽比=%.2f 面积比=%.1f" % (
            rw, rh, rh / rw, (rw * rh) / ((x2 - x1) * (y2 - y1)))
        print(f"      cand=({rx},{ry})-({rx+rw},{ry+rh}) {ar}")
    if cands:
        rx, ry, rw, rh = cands[0]
        d.rectangle([rx, ry, rx + rw, ry + rh], outline=(0, 120, 255), width=2)
        cards.append((label, (x1, y1, x2, y2), (rx, ry, rw, rh)))
vis.save(os.path.join(OUT, "probe_ab.png"))
for label, hit, r in cards:
    crop = FRAME[max(0, r[1] - 6):r[1] + r[3] + 34, max(0, r[0] - 6):r[0] + r[2] + 6]
    Image.fromarray(crop).resize((crop.shape[1] * 3, crop.shape[0] * 3), Image.LANCZOS).save(
        os.path.join(OUT, f"card_{label}.png"))


# ---------------------------------------------------------------------------
# 3) det=True 拿行级真值：数字行/名称行相对卡框的位置比例
# ---------------------------------------------------------------------------
from rapidocr import RapidOCR

det_engine = RapidOCR(params={"Global.use_det": True, "Global.use_cls": False})
out = det_engine(cv2.cvtColor(FRAME, cv2.COLOR_RGB2BGR))
lines = []
if out.txts is not None:
    for txt, box in zip(out.txts, out.boxes):
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        lines.append((str(txt), min(xs), min(ys), max(xs), max(ys)))

print("\nOCR(det) 全帧行：")
for t, lx, ly, rx2, ry2 in lines:
    print(f"  ({lx:7.1f},{ly:6.1f})-({rx2:7.1f},{ry2:6.1f}) {t!r}")

print("\n行→卡 归属与相对比例（y 相对卡顶、x 相对卡左；w/h 相对卡宽高）：")
for label, hit, (rx, ry, rw, rh) in cards:
    print(f"  [{label}] card=({rx},{ry},{rw}×{rh})")
    for t, lx, ly, rx2, ry2 in lines:
        cx_l, cy_l = (lx + rx2) / 2, (ly + ry2) / 2
        if rx <= cx_l <= rx + rw and ry <= cy_l <= ry + rh:
            print(f"    y=(ly-ry)/rh={(ly-ry)/rh:.3f}..{(ry2-ry)/rh:.3f} "
                  f"x=(lx-rx)/rw={(lx-rx)/rw:.3f}..{(rx2-rx)/rw:.3f}  {t!r}")

# ---------------------------------------------------------------------------
# 4) 生产口径验证：卡框(蓝) → 数字带/名称带(绿) 比例 → rec-only OCR 读数
#    蓝框选定规则=候选中高宽比≥1.2 的最小者；带比例来自第 3 步真值
# ---------------------------------------------------------------------------
COUNT_BAND = (0.02, 0.62, 0.98, 0.805)   # 相对卡框 (dx1, dy1, dx2, dy2)
NAME_BAND = (0.02, 0.805, 0.98, 0.985)


def pick_card_rect(hit_px):
    cands = card_rect_candidates(hit_px)
    hw, hh = hit_px[2] - hit_px[0], hit_px[3] - hit_px[1]
    geo = [r for r in cands if 1.2 <= r[3] / r[2] <= 2.2]
    geo.sort(key=lambda r: r[2] * r[3])
    return geo[0] if geo else None


ocr = TreasureOcr(REPO)
print("\n生产口径读数（rec-only 数字带 / 名称带）：")
expect_by_label = {
    "egg_red": 2, "egg_blue": 2, "egg_yellow": 1,
    "medal_coin": 250000, "medal_score": 30000,
}
n_ok = 0
for label, hit, _ in cards:
    r = pick_card_rect(hit)
    if r is None:
        print(f"  {label:<12} 蓝框选定失败")
        continue
    rx, ry, rw, rh = r
    cx1, cy1, cx2, cy2 = hit
    cnt = (
        (rx + rw * COUNT_BAND[0]) / W, (ry + rh * COUNT_BAND[1]) / H,
        (rx + rw * COUNT_BAND[2]) / W, (ry + rh * COUNT_BAND[3]) / H,
    )
    nme = (
        (rx + rw * NAME_BAND[0]) / W, (ry + rh * NAME_BAND[1]) / H,
        (rx + rw * NAME_BAND[2]) / W, (ry + rh * NAME_BAND[3]) / H,
    )
    info_c = ocr.recognize_single(FRAME, cnt) or {}
    info_n = ocr.recognize_single(FRAME, nme) or {}
    raw = str(info_c.get("text") or "").replace(",", "").replace(" ", "")
    m = re.search(r"[x×X]\s*(\d+)", raw) or re.search(r"(\d+)", raw)
    got = int(m.group(1)) if m else None
    exp = expect_by_label.get(label)
    ok = got == exp
    n_ok += ok
    print(f"  {label:<12} card=({rx},{ry},{rw}×{rh}) 数字带={raw!r}→{got} 名称带="
          f"{str(info_n.get('text') or '')!r} 期望={exp} {'✓' if ok else '✗'}")
print(f"\n{__file__}: {n_ok}/5 读数正确")
