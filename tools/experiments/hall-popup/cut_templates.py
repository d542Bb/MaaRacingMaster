"""从三张真机截图裁出待机/弹窗所需的识别模板，并逐张量区分力。

产出（写到 --out，默认本目录 out/）：
  hall_chat_left.png        待机位聊天图标（旗帜+气泡），区分「底部左角图标组消失」
  hall_controller_popup.png 控制器指引弹窗面板（清晰前景，不受背景高斯模糊影响）
  hall_popup_close_x.png    弹窗右上角 X（关闭动作的落点）

每张模板都给出「收紧 ROI」与「整幅画面」两组分数——ROI 决定区分力的例子已经出现过一次
（聊天框不限 ROI 时大厅 0.963），所以每张都必须两个口径都看。

用法：.venv\\Scripts\\python.exe -B tools\\experiments\\hall-popup\\cut_templates.py [大厅 待机 弹窗]
"""

import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from maaracing_master.core.template_match import best_match_score  # noqa:401,E402

FIX = HERE / "fixtures"
argv = sys.argv[1:4]
SRC = {
    "大厅": Path(argv[0]) if argv else FIX / "hall.png",
    "待机": Path(argv[1]) if len(argv) > 1 else FIX / "idle.png",
    "弹窗": Path(argv[2]) if len(argv) > 2 else FIX / "popup.png",
}
OUT = HERE / "out"

CHAT_BOX = (74, 602, 144, 640)        # 待机图：旗帜+气泡
CHAT_ROI = (58, 592, 120, 60)         # 收紧到左位 x 58..178
PANEL_SEARCH = (300, 180, 700, 360)   # 弹窗面板所在的中央搜索窗


def load(p):
    if not p.exists():
        raise SystemExit(f"缺图：{p}（按 大厅 待机 弹窗 顺序传参，或放 fixtures/）")
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def find_panel(img):
    """弹窗面板＝中央大片**低饱和高亮度**近白区。

    只按亮度会连落日天空一起圈进来（实测会把包围盒顶到搜索窗边界），所以加饱和度
    约束：max-min 通道差 < 40 才算白。取最大连通域的包围盒。
    """
    x, y, w, h = PANEL_SEARCH
    sub = img[y:y + h, x:x + w].astype(int)
    mx = sub.max(axis=2)
    sat = mx - sub.min(axis=2)
    mask = ((sat < 40) & (mx > 165)).astype(np.uint8)
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        raise SystemExit("没找到白色面板，检查 PANEL_SEARCH")
    best = 1 + int(np.argmax([stats[i, cv2.CC_STAT_AREA] for i in range(1, n)]))
    bx, by, bw, bh, area = stats[best]
    if bw < 200 or bh < 100:
        raise SystemExit(f"最大白色连通域过小 {bw}x{bh}（面积 {area}），判据不可信")
    return (x + int(bx), y + int(by), x + int(bx + bw), y + int(by + bh))


def grid(img, box, fname, step=25):
    """带坐标标注的裁片，供人工核对模板取位。"""
    x1, y1, x2, y2 = box
    crop = img[y1:y2, x1:x2].copy()
    for gx in range(0, crop.shape[1], step):
        cv2.line(crop, (gx, 0), (gx, crop.shape[0]), (0, 255, 0), 1)
        cv2.putText(crop, str(x1 + gx), (gx + 1, 12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.3, (0, 0, 255), 1)
    for gy in range(0, crop.shape[0], step):
        cv2.line(crop, (0, gy), (crop.shape[1], gy), (0, 255, 0), 1)
        cv2.putText(crop, str(y1 + gy), (1, gy + 12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.3, (0, 0, 255), 1)
    cv2.imencode(".png", crop)[1].tofile(str(OUT / fname))


def main():
    frames = {k: load(v) for k, v in SRC.items()}
    popup = frames["弹窗"]
    OUT.mkdir(exist_ok=True)

    px1, py1, px2, py2 = find_panel(popup)
    print(f"弹窗面板包围盒 = ({px1},{py1})-({px2},{py2})  尺寸 {px2 - px1}x{py2 - py1}")

    xbox = (px2 - 58, py1 + 12, px2 - 22, py1 + 48)
    print(f"X 按钮候选框   = {xbox}")
    grid(popup, (px2 - 170, py1 - 12, px2 + 24, py1 + 120), "popup_topright.png")
    grid(popup, (px1 - 10, py1 - 10, px2 + 10, py2 + 10), "popup_panel.png", step=50)

    crops = {
        "hall_chat_left.png": (frames["待机"], CHAT_BOX),
        "hall_controller_popup.png": (popup, (px1 + 40, py1 + 40, px1 + 280, py1 + 150)),
        "hall_popup_close_x.png": (popup, xbox),
    }
    specs = {
        "hall_chat_left.png": (CHAT_ROI, (0, 540, 1281, 130)),
        "hall_controller_popup.png": ((px1 + 40, py1 + 40, 240, 110), (300, 180, 700, 360)),
        "hall_popup_close_x.png": ((xbox[0] - 25, xbox[1] - 25, 100, 100),
                                   (px2 - 160, py1 - 40, 200, 160)),
    }
    for fname, (src, box) in crops.items():
        x1, y1, x2, y2 = box
        tpl = src[y1:y2, x1:x2]
        cv2.imencode(".png", tpl)[1].tofile(str(OUT / fname))
        red = int(((tpl[:, :, 2].astype(int) > 140) & (tpl[:, :, 0] < 90)
                   & (tpl[:, :, 1] < 90)).sum())
        tight, wide = specs[fname]
        print(f"\n{fname}  尺寸 {tpl.shape[1]}x{tpl.shape[0]}  红色标注像素={red}")
        for tag, roi in (("收紧", tight), ("宽域", wide)):
            row = []
            for k, f in frames.items():
                _b, s = best_match_score(cv2.cvtColor(f, cv2.COLOR_BGR2RGB),
                                         cv2.cvtColor(tpl, cv2.COLOR_BGR2RGB),
                                         scales=(1.0,), roi=roi)
                row.append(f"{k}={s:.3f}")
            print(f"   [{tag}] roi={roi} → " + "  ".join(row))


if __name__ == "__main__":
    main()
