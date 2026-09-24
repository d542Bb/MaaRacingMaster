# 第三方组件许可证声明（Third-Party Notices）

本文件汇总 MaaRacingMaster 在其源码、运行时发布包（`MaaRacingMaster-<ver>-win-x64.zip`）中
再分发或使用的第三方组件的许可证与归属。请随发布包一并保留本声明。

## 项目自身许可（分层）
- **本项目源码**（`maaracing_master/`、`apps/MaaRacingMaster.Shell/` 等）采用 **Apache-2.0**（见根目录 `LICENSE`）。
- **模型权重**：随发布包分发，真源在各插件目录内（声明文件随文件走）；本文件「模型
  权重」一节仅做许可类别登记与指向，不复制细节。AGPL 仅作用于对应权重文件本身，
  本项目其余代码非其衍生作品，保留 Apache-2.0。

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

## 二、模型权重（随包分发，真源在各插件目录内）

插件自带权重随插件目录入库、随发布包分发；**每个权重的来源、上游与许可细节只写
在插件目录内的许可 README（真源），本文件仅做随包许可的登记与指向**（登记行不含
细节，细节改动不需要同步本文件）：

| 权重文件（插件内相对路径） | 许可证 | 声明真源 |
|---|---|---|
| speedrush `resources/onnx/perception/model.onnx` | AGPL-3.0（Ultralytics 微调衍生；商业闭源需 Enterprise License） | [plugins/speedrush/resources/onnx/README.md](maaracing_master/plugins/speedrush/resources/onnx/README.md) |
| speedrush `resources/onnx/depth/depth_small_q4f16.onnx` | Apache-2.0 | 同上 |

AGPL 义务仅挂在对应权重文件本身，本项目其余代码非其衍生作品，保留 Apache-2.0；
发布包保留本文件与插件内声明文件，义务即满足。

---

## 三、GUI / 构建工具链（不随发布包分发）

- WinUI 3 / Windows App SDK：Microsoft 专有许可，构建产物自包含分发受微软条款约束；本项目不包含其源码。
- .NET Runtime：由 `dotnet publish --self-contained` 随 `MaaRacingMaster.Shell.exe` 附带，遵循 .NET 开源许可。
- **7-Zip**（`7za.exe` standalone）：**LGPL**，用于生成发行包 `.7z` 主推档；二进制入库于 `scripts/release/tools/7za.exe`，许可原文随附 `scripts/release/tools/7za_License.txt`，使用说明见其 [README](scripts/release/tools/README.md)。上游源码：<https://www.7-zip.org/>

---

## 四、前端图标资源（随发布包再分发）

以下组件的数据/代码随发布包内 `apps/MaaRacingMaster.Shell/frontend/` 一并分发。

| 组件 | 许可证 | 归属与用途 |
|---|---|---|
| Lucide（图标 path 数据） | **ISC** | <https://lucide.dev> / <https://github.com/lucide-icons/lucide>；`icons.js` 的 `LUCIDE` 分区拷贝自 lucide v1.45.0 图标几何数据（`github` 为 lucide 1.x 移除品牌图标前 0.x 版本数据）。ISC 为宽松许可，义务仅为保留版权声明与本许可标识。 |
| morphicons | **MIT** | <https://github.com/guillermolg00/morphicons>；`vendor.morphicons.js` 为 v1.7.1 上游 dist 的 ESM→经典脚本摊平版（未改函数体），用于图标变形动画。 |

Lucide 许可原文（ISC）：

```
ISC License

Copyright (c) Lucide Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

morphicons 为 MIT 许可，许可原文随代码副本携带于 `frontend/vendor.morphicons.js` 文件头注释块。

---

_本声明不构成法律意见；有疑问请与上游组件方或专业律师确认。_