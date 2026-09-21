#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLO 预标脚本：用现有模型自动生成标签文件（.txt），再手动补漏

用法：
    python tools/training/auto_label.py

输出：在图片同目录下生成同名的 .txt 标签文件
      格式：class_id x_center y_center width height（归一化 0~1）

然后打开 labelImg：
    labelImg <图片目录> --labels coin,car,bonus_car
"""
import sys
from pathlib import Path

# 把项目根目录加入 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import cv2
from maaracing_master.core.yolo_detector import YOLODetector


def main():
    # ── 配置 ──
    img_dir = Path(__file__).resolve().parent.parent.parent / "training"
    model_path = (Path(__file__).resolve().parent.parent.parent
                  / "archive" / "racing" / "resources" / "onnx" / "model.onnx")

    if not model_path.exists():
        print(f"模型不存在: {model_path}")
        return

    # 预标用较低阈值（宁可多标假阳性，回头删比手标省事）
    # car 走 conf 回退，coin / bonus_car 单独再降低
    detector = YOLODetector(str(model_path), conf=0.30, iou=0.5,
                            class_conf={"coin": 0.15, "bonus_car": 0.15})
    name_to_id = {name: cid for cid, name in detector.classes.items()}

    images = sorted(img_dir.glob("*.jpg")) + sorted(img_dir.glob("*.png"))
    print(f"找到 {len(images)} 张图片")

    total_labels = {name: 0 for name in name_to_id}
    skipped = 0
    auto_labeled = 0

    for i, img_path in enumerate(images):
        # 跳过已有标注的图片
        label_path = img_path.with_suffix(".txt")
        if label_path.exists():
            skipped += 1
            continue

        img_bgr = cv2.imread(str(img_path))
        if img_bgr is None:
            print(f"  跳过（无法读取）: {img_path.name}")
            skipped += 1
            continue
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

        h, w = img_rgb.shape[:2]
        _by_class, debug_dets, _all_raw = detector(img_rgb)

        # 合并所有检测结果，转 YOLO 格式
        lines = []
        for d in debug_dets:
            x1, y1, x2, y2 = d["box"]
            cls_name = d["class_name"]
            cls_id = name_to_id.get(cls_name, -1)
            if cls_id < 0:
                continue

            # YOLO 格式：x_center y_center width height（归一化 0~1）
            cx = (x1 + x2) / 2 / w
            cy = (y1 + y2) / 2 / h
            bw = (x2 - x1) / w
            bh = (y2 - y1) / h
            # 裁剪到 [0, 1] 避免越界
            cx, cy = max(0, min(1, cx)), max(0, min(1, cy))
            bw, bh = max(0, min(1, bw)), max(0, min(1, bh))
            lines.append(f"{cls_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
            total_labels[cls_name] += 1

        # 写入 .txt
        with open(label_path, "w") as f:
            f.write("\n".join(lines))

        auto_labeled += 1
        if (i + 1) % 50 == 0:
            print(f"  进度: {i+1}/{len(images)}")

    print(f"\n完成！已有标注跳过 {skipped} 张，新预标 {auto_labeled} 张")
    print("标注统计（可手动修改 .txt 增删改）:")
    for cid, name in sorted(detector.classes.items()):
        print(f"  {name}({cid}):{' ' * max(1, 12 - len(name))}{total_labels[name]}")
    print(f"  总计:{' ' * 11}{sum(total_labels.values())}")
    print("\n下一步:")
    print("  1. 用 labelImg 打开检查/补标:")
    print(f"     labelImg {img_dir} --labels {','.join(sorted(detector.classes.values()))}")
    print("  2. 补标完后复制到 dataset/images/train/ 和 dataset/labels/train/")


if __name__ == "__main__":
    main()
