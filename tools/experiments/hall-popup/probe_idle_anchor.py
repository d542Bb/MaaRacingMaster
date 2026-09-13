"""待机锚点候选（左下角聊天框图标）的区分力实测。

假设：待机与大厅的差别是「底部左角图标组（排行榜/俱乐部/战队）消失，聊天框左移约
127px」，聊天框图标本身两图完全相同 → 区分力**只来自 ROI 收紧**，不来自模板。
本脚本用三张真机截图直接量这个假设：模板取自待机图左下图标，在三种画面的
「收紧 ROI」与「整幅底栏」上各跑一次。

用法：.venv\\Scripts\\python.exe -B tools\\experiments\\hall-popup\\probe_idle_anchor.py
（三张图取本目录 fixtures/{hall,idle,popup}.png，或按 大厅 待机 弹窗 顺序传参。）
"""

import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_master.core.template_match import best_match_score  # noqa: E402

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
argv = sys.argv[1:4]
P = {
    "大厅": Path(argv[0]) if argv else FIX / "hall.png",
    "待机": Path(argv[1]) if len(argv) > 1 else FIX / "idle.png",
    "弹窗": Path(argv[2]) if len(argv) > 2 else FIX / "popup.png",
}
TPL_BOX = (74, 602, 144, 640)          # 待机图上的旗帜+气泡图标
TIGHT_ROI = (58, 592, 120, 60)         # 收紧到左位：x 58..178
WIDE_ROI = (0, 560, 1281, 90)          # 整幅底栏：看它在大厅里会落在哪


def load(p: Path):
    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读不到图：{p}（三张截图是会话附件，过期需换路径重跑）")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def main():
    frames = {}
    for k, v in P.items():
        if v.exists():
            frames[k] = load(v)
        else:
            print(f"跳过 {k}：{v}")
    if "待机" not in frames:
        return
    x1, y1, x2, y2 = TPL_BOX
    tpl = frames["待机"][y1:y2, x1:x2]
    print(f"模板尺寸 {tpl.shape[1]}x{tpl.shape[0]}（取自待机图左下聊天图标）")
    for tag, roi in (("收紧 ROI", TIGHT_ROI), ("整幅底栏", WIDE_ROI)):
        print(f"\n[{tag}] roi={roi}")
        for k, f in frames.items():
            r = (roi[0], roi[1], roi[2], roi[3])
            box, score = best_match_score(f, tpl, scales=(1.0,), roi=r)
            print(f"  {k:<4} 最高分={score:.3f}  落点={box}")


if __name__ == "__main__":
    main()
