"""控制器指引弹窗出现时，既有大厅锚点还活不活？

背景：游戏在大厅弹出手柄操作指引时，画面**不是压暗而是高斯模糊**（弹窗自身清晰、
背景整片糊）。模板匹配内核是 TM_CCOEFF_NORMED（template_match._best_match），
它对整体亮度线性变化免疫，但对**边缘被抹平**不免疫——所以模糊会不会把分数打穿
阈值，只能实测。

用法：.venv\\Scripts\\python.exe -B tools\\experiments\\hall-popup\\probe_popup_blur.py 大厅 待机 弹窗
（三张图按顺序传路径；不传则取本目录 fixtures/{hall,idle,popup}.png。）
"""

import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
TPL_DIR = REPO / "maaracing_master" / "plugins" / "treasure" / "resources" / "image"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO))

from maaracing_master.core.template_match import best_match_score  # noqa: E402

DEFAULTS = [FIXTURES / "hall.png", FIXTURES / "idle.png", FIXTURES / "popup.png"]
# treasure.游戏大厅.dwell 的 rect（归一化 x1,y1,x2,y2）与其模板
HALL_RECT = (0.7603703703703705, 0.8037448559670782, 0.8962962962962963, 0.8911111111111112)
HALL_TPL = "hall_peak_appraise_card.png"


def load_bgr(p: Path):
    if not p.exists():
        raise SystemExit(f"缺图：{p}\n→ 把三张截图按 大厅/待机/弹窗 顺序传参，"
                         "或放进本目录 fixtures/{hall,idle,popup}.png")
    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读不到图：{p}")
    return img


def rect_px(norm, w, h):
    x1, y1, x2, y2 = norm
    return int(x1 * w), int(y1 * h), int((x2 - x1) * w), int((y2 - y1) * h)


def sharpness(img, box):
    """拉普拉斯方差：越大越锐。模糊会把边缘能量抹平。"""
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return float("nan")
    crop = img[max(0, y):min(img.shape[0], y + h), max(0, x):min(img.shape[1], x + w)]
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def main():
    paths = [Path(a) for a in sys.argv[1:4]] or DEFAULTS
    hall, idle, popup = (load_bgr(p) for p in paths)
    H, W = hall.shape[:2]
    print(f"尺寸 大厅={hall.shape[1]}x{hall.shape[0]} 弹窗={popup.shape[1]}x{popup.shape[0]}")

    tpl = load_bgr(TPL_DIR / HALL_TPL)
    roi = rect_px(HALL_RECT, W, H)
    print(f"\n[锚点生死] 模板 {HALL_TPL} 在 dwell 的 rect {roi} 内：")
    for tag, frame in (("大厅（基线）", hall), ("弹窗压住时", popup), ("待机", idle)):
        box, score = best_match_score(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB),
                                      cv2.cvtColor(tpl, cv2.COLOR_BGR2RGB), roi=roi)
        print(f"  {tag:<10} 最高分={score:.3f}  命中框={box}")

    print("\n[模糊量化] 拉普拉斯方差（同一片背景区，弹窗帧 vs 干净帧）：")
    regions = {
        "顶部货币栏": (int(0.60 * W), int(0.02 * H), int(0.35 * W), int(0.06 * H)),
        "底部导航四tab": (int(0.01 * W), int(0.90 * H), int(0.35 * W), int(0.06 * H)),
        "右下鉴宝/比赛区": (int(0.62 * W), int(0.78 * H), int(0.35 * W), int(0.18 * H)),
        "弹窗面板自身": (int(0.28 * W), int(0.32 * H), int(0.42 * W), int(0.30 * H)),
    }
    for name, box in regions.items():
        print(f"  {name:<14} 大厅={sharpness(hall, box):8.1f}   弹窗={sharpness(popup, box):8.1f}")

    print("\n[亮度量化] 同一片背景区的均值（判断是否只是压暗）：")
    for name, box in list(regions.items())[:3]:
        x, y, w, h = box
        a = hall[y:y + h, x:x + w].mean()
        b = popup[y:y + h, x:x + w].mean()
        print(f"  {name:<14} 大厅={a:6.1f}   弹窗={b:6.1f}   比值={b / a if a else float('nan'):.3f}")


if __name__ == "__main__":
    main()
