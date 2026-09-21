# 模型权重许可声明

本目录下的 `model.onnx` **单独适用 AGPL-3.0**（与项目其余代码的 Apache-2.0 许可不同）。

- **来源**：由 Ultralytics 官方预训练权重 `yolo11n.pt` + Ultralytics 训练代码
  在自有标注数据上微调导出（见根目录 `tools/training/train.py`）。
- **许可**：按 Ultralytics 的立场，本微调模型视为 AGPL-3.0 衍生作品，随发布包再分发需遵循
  [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)。
- **运行时不含 Ultralytics 代码**：YOLO 推理仅通过 ONNX Runtime 加载本 ONNX 图，
  **未再分发任何 Ultralytics / PyTorch / torchvision 代码**。
- **商业 / 闭源使用**：如需在不开放源码或商业场合使用本模型，须另行取得
  [Ultralytics Enterprise License](https://www.ultralytics.com/license)。
- 上游: <https://github.com/ultralytics/ultralytics>

项目其余代码遵循根目录 `LICENSE`（Apache-2.0），两者以文件边界区分，互不覆盖。