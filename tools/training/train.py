#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLO11n 训练脚本：自动训练并导出 ONNX 到归档的 racing 插件资源目录
（archive/racing/resources/onnx/，racing 插件的归档资源目录；模型随插件自包含分发，重写 racing 时从这里取用）。

许可证注意：本脚本用 Ultralytics 官方预训练权重 yolo11n.pt 微调导出，
产出的 model.onnx 视为 AGPL-3.0 衍生作品（见同目录 resources/onnx/README.md）。
发布冒烟/CI 之外，请勿把该模型用于不开放源码的商业场合而未取得
Ultralytics Enterprise License。
"""
from ultralytics import YOLO
from pathlib import Path
import shutil


def main():
    script_dir = Path(__file__).resolve().parent          # tools/training
    tools_dir = script_dir.parent                        # tools
    project_dir = tools_dir / "train_output"             # 保持原输出位置 tools/train_output

    model = YOLO("yolo11n.pt")

    model.train(
        data=str(script_dir / "dataset.yaml"),
        epochs=100,
        imgsz=640,
        batch=16,
        device=0,
        project=str(project_dir.parent),
        name="train_output",
        patience=20,
        exist_ok=True,
    )

    best = YOLO(str(project_dir / "weights" / "best.pt"))
    best.export(format="onnx", imgsz=640, simplify=True, opset=12)
    onnx_path = project_dir / "weights" / "best.onnx"

    dst = tools_dir.parent / "archive" / "racing" / "resources" / "onnx" / "model.onnx"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, dst)
    print(f"导出完成: {onnx_path}")
    print(f"已复制到: {dst}")


if __name__ == "__main__":
    main()
