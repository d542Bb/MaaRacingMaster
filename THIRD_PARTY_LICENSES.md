# 第三方组件许可证声明（Third-Party Notices）

本文件汇总 MaaRacingMaster 在其源码、运行时发布包（`MaaRacingMaster-<ver>-win-x64.zip`）中
再分发或使用的第三方组件的许可证与归属。请随发布包一并保留本声明。

## 项目自身许可（分层）
- **本项目源码**（`maaracing_master/`、`apps/MaaRacingMaster.Shell/` 等）采用 **Apache-2.0**（见根目录 `LICENSE`）。
- **模型权重**：当前版本不随发布包分发任何模型权重；若某活动插件自带 YOLO 等模型权重，则该权重单独沿用 **AGPL-3.0**
  （见下方「模型权重」一节）。AGPL 仅作用于权重文件本身，本项目其余代码非其衍生作品，保留 Apache-2.0。

---

## 一、运行时依赖（随发布包再分发的二进制 / Python 包）

| 组件 | 许可证 | 备注 |
|---|---|---|
| MaaFramework (`maafw`) / `MaaAgentBinary` | **LGPL-3.0** | 见下方「LGPL-3.0 说明」 |
| onnxruntime-directml | MIT | Microsoft ONNX Runtime |
| opencv-python | Apache-2.0 | |
| rapidocr | Apache-2.0 | |
| numpy / shapely | BSD-3-Clause | |
| vgamepad | MIT | 依赖 ViGEmBus（另行分发） |
| windows-capture | MIT | |
| pillow / pyclipper / 其他纯 Python 传递依赖 | MIT / BSD-2-Clause / Apache-2.0 / PSF | 以各 `*.dist-info/METADATA` 声明为准 |

上述许可证均允许随包再分发。对应许可证原文见各组件发布元数据或官方仓库。

### LGPL-3.0 说明（MaaFramework）
本项目通过 Python 绑定**动态加载**且**未修改** MaaFramework 本体（源码、接口、二进制均保持上游原样），
满足 LGPL-3.0 的"作为独立库经接口链接"豁免，因此本项目代码可保留 Apache-2.0。
义务：保留本声明、提供 MaaFramework 源码获取途径。
- 上游源码: <https://github.com/MaaXYZ/MaaFramework> （LGPL-3.0）

### ViGEmBus
虚拟手柄驱动 `ViGEmBus` 为独立内核驱动，**不在发布包内分发**，由用户在安装时另行下载，
遵循其自身许可：<https://github.com/ViGEm/ViGEmBus>

---

## 二、模型权重（AGPL-3.0）

当前版本发布包**不随包分发模型权重**。若后续活动插件自带 YOLO 等模型权重并随包分发，则适用以下条款：

- 此类权重由 **Ultralytics 官方预训练权重** `yolo11n.pt` + Ultralytics 训练代码在自有标注数据上微调导出（见 `tools/training/train.py`）。
- 按 Ultralytics 的许可立场，该微调模型视为 **AGPL-3.0 衍生作品**，随本发布包再分发需遵循 AGPL-3.0。
- 本项目运行时**未再分发任何 Ultralytics 软件代码**（YOLO 推理仅通过 ONNX Runtime 加载 ONNX 图）。
- AGPL 义务仅挂在权重文件本身，本项目其余代码保留 Apache-2.0。
- 上游: <https://github.com/ultralytics/ultralytics>　许可: <https://www.gnu.org/licenses/agpl-3.0.html>

> 如需把此类模型用于**不开放源码 / 商业闭源**的场合，需另行取得 Ultralytics Enterprise License，
> 见 <https://www.ultralytics.com/license>。

---

## 三、GUI / 构建工具链（非再分发，仅构建期引用）
- WinUI 3 / Windows App SDK：Microsoft 专有许可，构建产物自包含分发受微软条款约束；本项目不包含其源码。
- .NET Runtime：由 `dotnet publish --self-contained` 随 `MaaRacingMaster.Shell.exe` 附带，遵循 .NET 开源许可。

---

_本声明不构成法律意见；有疑问请与上游组件方或专业律师确认。_