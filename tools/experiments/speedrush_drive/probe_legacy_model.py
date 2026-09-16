"""speedrush 驾驶感知可行性探针：旧 racing YOLO 模型在 speedrush 真机帧上的表现。

**要回答的问题**（C 类：外部系统行为，禁止靠猜）：`archive/racing/resources/onnx/model.onnx`
是在 racing 画面训练出的 3 类检测器（coin/car/bonus_car）。speedrush 的车辆外观、金币
形态、视角、光照是否与 racing 相近到**可直接复用**，此前无任何证据。本探针用真机帧实测。

**判读口径**：驾驶帧里若能稳定检出 coin（>0 且置信度 > 0.35），说明模型对 speedrush
的金币外观仍有响应，可作复用起点；全为 0 而 raw 也接近 0，说明模型对该画面域无响应，
需重新标注训练（工具链 `tools/training/` 已在）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_legacy_model.py <帧通配>
    # 例：... probe_legacy_model.py "D:/frames/*.png"

输入：speedrush 驾驶窗口截图（1281×759，需裁出 720p 客户区）。帧目录由命令行给出，
不入库本机路径。输出：逐帧各类检测数与最高置信度。
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402

FRAME_GLOB = sys.argv[1] if len(sys.argv) > 1 else str(
    ROOT / "tools" / "experiments" / "speedrush_drive" / "frames" / "*.png")
MODEL = str(ROOT / "archive" / "racing" / "resources" / "onnx" / "model.onnx")

# 窗口截图 → 720p 客户区（实测偏移：x=1 左边框，0~37 行标题栏）
OFFSET_X, OFFSET_Y = 1, 38
CONTENT_W, CONTENT_H = 1280, 720


def main() -> None:
    if not Path(MODEL).exists():
        print(f"模型不存在: {MODEL}")
        return
    det = YOLODetector(MODEL, conf=0.25, iou=0.5)
    frames = sorted(glob.glob(FRAME_GLOB))
    print(f"模型: {MODEL}")
    print(f"帧数: {len(frames)}\n")
    header = f"{'帧':>34s} {'coin':>5s} {'c_max':>6s} {'car':>4s} {'r_max':>6s} {'bonus':>6s} {'raw':>4s}"
    print(header)
    print("-" * len(header))
    for f in frames:
        img = cv2.imread(f)
        if img is None:
            continue
        content = img[OFFSET_Y:OFFSET_Y + CONTENT_H, OFFSET_X:OFFSET_X + CONTENT_W]
        if content.shape[0] != CONTENT_H or content.shape[1] != CONTENT_W:
            print(f"{Path(f).name[:30]:>34s} 尺寸异常 {content.shape}")
            continue
        rgb = cv2.cvtColor(content, cv2.COLOR_BGR2RGB)
        coins, cars, bonus, dets, raw = det(rgb)
        by_cls: dict[str, list[float]] = {"coin": [], "car": [], "bonus_car": []}
        for d in dets:
            by_cls[d["class_name"]].append(d["confidence"])

        def cmax(k: str) -> str:
            v = by_cls[k]
            return f"{max(v):.2f}" if v else "-"

        print(f"{Path(f).name[:30]:>34s} {len(coins):>5d} {cmax('coin'):>6s} "
              f"{len(cars):>4d} {cmax('car'):>6s} {len(bonus):>6d} {len(raw):>4d}")


if __name__ == "__main__":
    main()