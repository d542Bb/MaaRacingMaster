# 模型权重许可声明

本目录按**用途分子目录**存放插件模型权重；**发布包默认不带这些权重**，
由可选模型包提供（安装见下节）。各权重许可以文件边界区分、互不覆盖
（项目其余代码遵循根目录 `LICENSE`，Apache-2.0）：

```
models/
├── perception/model.onnx                                    # 物品感知（YOLO，AGPL-3.0 衍生）
├── depth/moge2_vits_static_336x598_t1032_q4f16.onnx         # 深度几何（MoGe-2 q4f16，Apache-2.0）
└── lane/plus_small_384x640.onnx                             # 线观测（TwinLiteNet+ Small，BDD100K 非商业/科研）
```

## 安装（深度模式与线观测）

发布包默认只带直行与宏录制；要用深度几何，需要先安装模型包：

1. 从 GitHub 或 CNB 的 Release 下载 `MaaRacingMaster-speedrush-models-<版本>.zip`。
2. 打开 zip，把里面的 `perception/`、`depth/` 与 `lane/` 文件夹复制到用户数据目录的
   `data/speedrush/models/`（默认 `%APPDATA%\MaaRacingMaster\data\speedrush\models\`）。
   Windows 的「全部解压」会多套一层以 zip 命名的目录——直接从 zip 里拖文件夹过去更稳。
3. 重启 MaaRM；深度几何选项恢复可用，直行与宏录制不受影响。

`lane` 是线观测模型：装了它，线可用时作为供数链的位置原点；没装或校验失败时
线观测自动跳过，控制退回深度/墙，深度档本身仍可用。该模型按 BDD100K 非商业/科研
许可分发，完整条款见包内 `LICENSE-BDD100K.txt`；商业使用需另行授权。

校验值（代码 `model_assets.MODEL_MANIFEST` 同源）：

- `perception/model.onnx`：10,605,615 字节，SHA256
  `f12b5958637932f3a01f413c93a8a5ceba86693a8133a52fa32224ec8f5e3fe4`
- `depth/moge2_vits_static_336x598_t1032_q4f16.onnx`：71,913,448 字节，SHA256
  `2e5c58b4bd6367593a8ed6039ef729a89c4b19f3a64e5954713f10a36ec0f116`
- `lane/plus_small_384x640.onnx`：543,153 字节，SHA256
  `45044c268d54ee34cbd097abcf19fb0d53858a8815e0024018db0df6137289e4`

缺失或校验失败时，深度模式会在连接游戏前终止并提示重新下载；直行与宏录制照常可用。

## perception/model.onnx —— 物品感知（AGPL-3.0）

- **来源**：由 Ultralytics 官方预训练权重 `yolo11n.pt` + Ultralytics 训练代码
  在自有标注数据上微调导出（见 `maaracing_master/plugins/speedrush/tools/training/train.py`）。
- **许可**：按 Ultralytics 的立场，本微调模型视为 AGPL-3.0 衍生作品，随发布包
  再分发需遵循 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)。
- **运行时不含 Ultralytics 代码**：YOLO 推理仅通过 ONNX Runtime 加载本 ONNX 图，
  **未再分发任何 Ultralytics / PyTorch / torchvision 代码**。
- **商业 / 闭源使用**：如需在不开放源码或商业场合使用本模型，须另行取得
  [Ultralytics Enterprise License](https://www.ultralytics.com/license)。
- 上游: <https://github.com/ultralytics/ultralytics>

## depth/moge2_vits_static_336x598_t1032_q4f16.onnx —— 深度几何（Apache-2.0）

- **来源**：Microsoft MoGe-2（ViT-S）官方 ONNX 导出权重的 q4f16 量化、
  720×1280 输入折叠静态图（输出点图 336×598）；量化折叠产物经离线同卷
  验证与金标回归后入库（2026-10-01 深度几何 v4 换装，取代 DA-S 视差权重）。
- **许可**：Apache-2.0（上游权重与转换产物同许可），可随发布包自由再分发。
- **运行时不依赖上游仓库代码**：推理仅通过 ONNX Runtime 加载本 ONNX 图；
  focal/shift 恢复与点图重建的后处理为本仓库复刻（`moge_post.py`，公式
  对齐官方 geometry_numpy / v2.py）。
- 上游: <https://github.com/microsoft/MoGe>

## lane/plus_small_384x640.onnx —— 线观测（许可待核对）

- **来源**：TwinLiteNet+ Small 车道分割模型，训练自 BDD100K；离线核验记录见提交历史。
- **许可**：上游 TwinLiteNet+ 代码为 MIT；BDD100K 数据/标签适用 UC Regents 定制许可——
  教育、科研与非营利目的可复制、修改、分发（须随附版权与许可声明），商业用途仅限
  BDD/BAIR 成员。许可正文未单列“训练所得权重”，本项目按衍生作品处理：随可选模型包
  分发时附完整许可 `LICENSE-BDD100K.txt`；**商业用途须先取得授权**。
- **运行时不依赖上游仓库代码**：推理仅通过 ONNX Runtime 加载本 ONNX 图。
- 缺失时线观测自动跳过，供数退回深度/墙，不影响深度档启动。
