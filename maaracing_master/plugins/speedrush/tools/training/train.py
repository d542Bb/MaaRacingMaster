#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLO11n 训练脚本：自动训练并导出 ONNX 到 speedrush 插件的感知权重位
（plugins/speedrush/resources/models/perception/model.onnx——插件自包含：
模块自己用的东西住自己目录，本脚本只是往那个真源位覆盖产物）。

许可证注意：本脚本用 Ultralytics 官方预训练权重 yolo11n.pt 微调导出，
产出的 model.onnx 视为 AGPL-3.0 衍生作品（声明见插件 models/README.md）。
发布冒烟/CI 之外，请勿把该模型用于不开放源码的商业场合而未取得
Ultralytics Enterprise License。
"""
from ultralytics import YOLO
from pathlib import Path
import shutil

import yaml


def main():
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parents[4]
    # 训练产物落 build/（已 gitignore），不在工具目录留下未跟踪的大文件
    project_dir = repo_root / "build" / "train_output"
    project_dir.mkdir(parents=True, exist_ok=True)

    # dataset.yaml 的 path 相对仓库根声明；解析成绝对路径，避免依赖启动目录
    data_cfg = yaml.safe_load((script_dir / "dataset.yaml").read_text(encoding="utf-8"))
    data_cfg["path"] = str((script_dir / data_cfg["path"]).resolve())
    resolved_yaml = project_dir / "dataset.resolved.yaml"
    resolved_yaml.write_text(
        yaml.safe_dump(data_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    model = YOLO("yolo11n.pt")

    model.train(
        data=str(resolved_yaml),
        epochs=100,
        imgsz=640,
        batch=16,
        device=0,
        project=str(project_dir.parent),
        name=project_dir.name,
        patience=20,
        exist_ok=True,
    )

    best = YOLO(str(project_dir / "weights" / "best.pt"))
    best.export(format="onnx", imgsz=640, simplify=True, opset=12)
    onnx_path = project_dir / "weights" / "best.onnx"

    dst = repo_root / "maaracing_master" / "plugins" / "speedrush" \
        / "resources" / "models" / "perception" / "model.onnx"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, dst)
    print(f"导出完成: {onnx_path}")
    print(f"已复制到: {dst}")


if __name__ == "__main__":
    main()
