# 模型权重许可声明

本目录按**用途分子目录**存放插件自带模型权重，随包分发入库；两权重两许可，
均以文件边界区分，互不覆盖（项目其余代码遵循根目录 `LICENSE`，Apache-2.0）：

```
onnx/
├── perception/model.onnx            # 物品感知（YOLO，AGPL-3.0 衍生）
└── depth/depth_small_q4f16.onnx     # 深度几何（DA-S q4f16，Apache-2.0）
```

`REQUIRED_ASSETS` 按相对路径声明两个权重，sidecar 启动前检查存在性。

## perception/model.onnx —— 物品感知（AGPL-3.0）

- **来源**：由 Ultralytics 官方预训练权重 `yolo11n.pt` + Ultralytics 训练代码
  在自有标注数据上微调导出（见根目录 `tools/training/train.py`）。
- **许可**：按 Ultralytics 的立场，本微调模型视为 AGPL-3.0 衍生作品，随发布包
  再分发需遵循 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)。
- **运行时不含 Ultralytics 代码**：YOLO 推理仅通过 ONNX Runtime 加载本 ONNX 图，
  **未再分发任何 Ultralytics / PyTorch / torchvision 代码**。
- **商业 / 闭源使用**：如需在不开放源码或商业场合使用本模型，须另行取得
  [Ultralytics Enterprise License](https://www.ultralytics.com/license)。
- 上游: <https://github.com/ultralytics/ultralytics>

## depth/depth_small_q4f16.onnx —— 深度几何（Apache-2.0）

- **来源**：Depth Anything V2 Small 官方权重的 ONNX q4f16 量化转换
  （onnx-community 转换产物），经离线同卷验证（@336 落档）后入库。
- **许可**：Apache-2.0（上游权重与转换产物同许可），可随发布包自由再分发。
- **运行时不依赖上游仓库代码**：推理仅通过 ONNX Runtime 加载本 ONNX 图。
- 上游: <https://github.com/DepthAnything/Depth-Anything-V2>
  （转换参考: <https://huggingface.co/onnx-community>）
