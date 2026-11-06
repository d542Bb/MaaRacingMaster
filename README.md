<p align="center">
  <img src="assets/maarm_icon.png" width="128" alt="MaaRacingMaster logo">
</p>

<h1 align="center">MaaRacingMaster</h1>

<p align="center">
  <em>模块化游戏自动化平台 —— MAA Framework × 计算机视觉 × 虚拟手柄</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/MaaFramework-5.12.3-green" alt="MaaFramework">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License">
  <img src="https://github.com/d542Bb/MaaRacingMaster/actions/workflows/test.yml/badge.svg" alt="Test">
  <img src="https://img.shields.io/badge/status-development-yellow" alt="Status">
  <a href="https://afdian.com/a/MaaRacingMaster">
    <img src="https://img.shields.io/badge/爱发电-赞助-blue?logo=afdian&logoColor=white" alt="爱发电 赞助">
  </a>
  <!-- 正式 v1.0.0 发版时换用 release badge：
  <img src="https://github.com/d542Bb/MaaRacingMaster/actions/workflows/release.yml/badge.svg" alt="Release"> -->
</p>

***

> \[!NOTE]
> **维护说明（个人项目）**
>
> 本仓库为**个人开发者在业余时间维护的开源项目**，**不提供长期维护承诺**，也不对未来兼容性、稳定性或功能演进做任何保证。
>
> - **打 tag 即出正式 Release 包**；未打 tag 的改动在开发分支（`dev/d542Bb`）上，稳定收口后经 PR 合入 master——master 上每个提交都是一个已收口主题，可能尚未打包。
>
> - 遇到问题请优先提交 [Issue](https://github.com/d542Bb/MaaRacingMaster/issues)；修复取决于个人时间与精力，**不保证响应与修复时限（无 SLA）**。
>
> - 源码完全开源，**欢迎任何人 fork / 提 PR / 参与维护**——项目的延续依靠社区而非单一开发者。

> \[!WARNING]
> ⚠️ **免责与合规声明**
>
> 本项目为**游戏自动化技术研究与学习项目**，基于图像识别与虚拟手柄模拟，**仅供个人学习、技术交流与教学演示**，请勿用于任何商业或营利性目的。
>
> 本项目**仅进行画面识别与模拟输入操作**，**不读取、不篡改任何游戏数据**，**不注入进程、不修改游戏文件、不抓取通信数据包**。
>
> **严禁**将本项目或其源码用于：
>
> - **代练 / 代打** 等商业代练服务；
>
> - 制作、发布、传播或使用**外挂、作弊软件、宏脚本**等违规工具；
>
> - 任何**有意影响服务器排名、刷榜或破坏游戏平衡**的行为。
>
> 使用本工具操作游戏账号存在违反游戏用户协议的风险，可能导致账号处罚、封禁等后果，**所有后果由使用者自行承担，与项目及开发者无关**。
>
> 请严格遵守游戏规则与相关法律法规；若未获得游戏官方授权，请勿将本项目用于对应游戏。

> \[!IMPORTANT]
> 🚧 **当前内容**
>
> 现役活动插件为 **极速狂飙**（`speedrush`），支持全程直行、深度几何与宏录制三种驾驶方式；
> 此前的 **巅峰鉴宝** 已随活动结束归档（`plugins/_archive/`），不再随安装包分发。
> 各插件的功能细节与完成度以其自述文档为准；如遇异常，请提交 Issue 或参考开发文档自诊。

***

> **本文是什么**：面向使用者与访客的项目门面——定位、快速开始、环境要求与文档入口。
> **本文不是什么**：不是架构与实现说明（见 [docs/CODE_WIKI.md](docs/CODE_WIKI.md)）；不是行为守则（见 [AGENTS.md](AGENTS.md)）。
> **信源等级**：L3（导航与摘要）。

## 简介

**MaaRacingMaster** 是一款面向《巅峰极速》的模块化游戏自动化工具。

基于画面识别与模拟输入，自动完成活动流程：启动游戏 → 匹配 → 驾驶 → 结算 → 关闭游戏，可循环执行。

每个活动是一个独立插件，放入即用、删除即卸。现役插件是「极速狂飙」，提供全程直行、深度几何与宏录制三种驾驶方式；需要深度几何时，按提示下载可选的模型包即可。

## 演示

### 界面截图

| 主控（任务卡与运行日志） | 数据（性能监控与看板） |
| --- | --- |
| ![主控](https://github.com/user-attachments/assets/d6428f44-dfe2-494d-8d1f-9d27e5082768) | ![数据](https://github.com/user-attachments/assets/b212b56e-a9c9-4a62-8cf0-6c58d89c79bb) |

| 设置（调试与环境中心） | 关于（版本与更新源） |
| --- | --- |
| ![设置](https://github.com/user-attachments/assets/75f35cb8-4a16-4c12-a8a0-bb68ee481597) | ![关于](https://github.com/user-attachments/assets/76c6bc13-d526-41a5-8963-24a8e105f48a) |

***

## 支持本项目

如果本项目对你有帮助，欢迎[在爱发电上赞助](https://afdian.com/a/MaaRacingMaster)，
支持持续的开发与维护。您的每一份支持都对本项目的成长意义重大。

***

## 亮点功能

- **活动全链路自动化**：启动游戏 → 匹配 → 驾驶 → 结算 → 关闭游戏，可循环执行
- **三种驾驶方式**：全程直行（默认）、深度几何（识别赛道并自动选道、避车、超车）、宏录制（录制并回放手柄 / 键盘操作）
- **轻量主包 + 可选模型包**：安装包不含大体积权重；需要深度几何时从 Release 下载模型包，按提示放入数据目录即可
- **多模态识别**：模板匹配 / OCR / 目标检测 / 单目深度 / 车道线观测，按活动需求组合
- **任务队列与断点**：多个任务顺序执行，可从任意阶段起步，卡片实时显示排队与进度
- **PEEP 实时预览**：不打断运行，实时观察画面与决策
- **可视化调试台**：会话回放 + ROI 调整（Studio）；图结构与模板编辑走 MPE
- **插件化架构**：一个活动 = 一个自包含目录，复制即装、删除即卸
- **解压即用**：发行包自带 Python 运行时与全部依赖，无需配置环境
- **更新源自选**：关于页可在 CNB / GitHub 之间选择「检查更新」的来源

***

## 快速开始

分两类入口，按你的目的二选一即可：

- **只想直接使用**（不写代码）→ 跳到下方「下载即用包」一节

- **要改代码 / 参与开发** → 继续往下，走「从源码构建」四步

> 当前处于**开发阶段**：GitHub [Releases](https://github.com/d542Bb/MaaRacingMaster/releases) 上已提供 pre-release 打包，正式 v1.0.0 尚未发布。

### 下载即用包（普通用户，无需编译）

1. 到 GitHub [Releases](https://github.com/d542Bb/MaaRacingMaster/releases)（或 [CNB 镜像](https://cnb.cool/MaaRacingMaster/MAIN/-/releases)）下载最新 **`MaaRacingMaster-<版本>-win-x64.zip`**（或 `.7z`）。
2. 用资源管理器把它**解压到任意本地目录**。
3. 打开该文件夹，**双击根目录的** **`MaaRacingMaster.exe`** 启动（包内唯一入口，薄 Launcher 会拉起 `app\` 下的 GUI；已自带 Python 运行时与全部依赖，启动时自动弹出 UAC 提权）。
4. 若使用后台点击或虚拟手柄，需先安装 [ViGEmBus 虚拟手柄驱动](https://github.com/nefarius/ViGEmBus/releases)；也可在「设置 → 环境中心 → 可选依赖」里检测并打开下载页。

> **深度几何需要额外下载模型包：** 主包默认只带「全程直行 / 宏录制」；要用「深度几何」，从同一 Release 下载 `MaaRacingMaster-speedrush-models-<版本>.zip`，解压到用户数据目录的 `data/speedrush/models/`，重启后在任务卡里切换。线观测模型也在同一包内；缺包时线观测自动退回深度/墙。安装细节与校验值见 [模型权重说明](maaracing_master/plugins/speedrush/resources/models/README.md)。

> 成功标志：GUI 窗口出现，主控页任务卡正常加载，底栏状态球显示「系统就绪」（后端已连接）。

***

以下是**从源码构建**方式（面向开发者 / 贡献者）。

从零到跑通，按下面四步走；每步都有明确的「成功标志」与「失败自查」入口。完整清单见 [docs/SELF\_CHECK.md](docs/SELF_CHECK.md)。

### 0. 准备

- 确认系统满足[环境要求](#环境要求)（Windows 10/11 64-bit、**管理员权限**、游戏窗口 **1280×720**）。

- 若所用插件要求虚拟手柄独占，需先断开物理手柄。

### 1. 拉取代码

```bash
git clone https://github.com/d542Bb/MaaRacingMaster.git
cd MaaRacingMaster
```

> 成功标志：目录下有 `README.md`、`maaracing_master/`、`apps/MaaRacingMaster.Shell/`。

### 2. 安装 Python 环境

```bash
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> 成功标志：`.venv\Scripts\python.exe` 存在，且 `pip show maafw` 能查到版本。
>
> 前台 `MaaRacingMaster.Shell.exe` 会从自身基目录向上定位仓库根（`pyproject.toml`）并拼接 `.venv`，**不再硬编码本机路径**，仓库可 clone 到任意磁盘位置运行。

### 3. 可行性自检（推荐）

```bash
# 依赖就位验证（导入冒烟 + 单元测试）
.venv\Scripts\python.exe -m pytest tests -q
```

> 成功标志：`0 failed`。失败时逐条对照 [docs/SELF\_CHECK.md](docs/SELF_CHECK.md) 的「常见失败与处理」。

### 4. 编译 GUI 并启动

```bash
# 首次使用需先编译前台 shell（管理员提权由 exe 自身 manifest 承担，无需再手动以管理员运行）
dotnet build apps\MaaRacingMaster.Shell\MaaRacingMaster.Shell.csproj -c Debug
```

编译成功后，运行编译产物 **`apps\MaaRacingMaster.Shell\bin\Debug\net8.0-windows10.0.19041.0\win-x64\MaaRacingMaster.Shell.exe`** 启动 GUI（exe manifest `requireAdministrator` 会自动弹出 UAC 提权）。

> 成功标志：GUI 窗口出现，主控页任务卡正常加载，底栏状态球显示「系统就绪」（后端已连接）。

**独立调试 sidecar**（不经 GUI，等待 stdin JSONL RPC）：`python -u -m maaracing_master.core.sidecar`。

### 日常跑源码（推荐入口）

上面四步走通之后，日常迭代不必再手动编译与找 exe——仓库根 **`dev-shell.cmd`** 一条命令搞定：

```bash
.\dev-shell.cmd                 # 按需编译 + 启动，窗口跑的就是仓库当前源码
.\dev-shell.cmd --check-deps    # 顺带验证 .venv 依赖完整（import maa / cv2 / numpy）
.\dev-shell.cmd --rebuild       # 强制重新编译 .NET shell
.\dev-shell.cmd --no-launch     # 只编译与自检，不开窗口
```

它按四步走：预检（`.venv`、dotnet SDK）→ 仅当 C#/前端资源比现有 exe 更新时才 `dotnet build` → 检出冲突实例并列出其完整路径（单实例互斥体会让第二个窗口直接退出）→ 启动 `bin\` 下的 shell，最后**查进程自证**本次后端由哪个 python 拉起。

- **改 Python / pipeline JSON / policy 存盘即生效**，不必重新打包；只有改 C# 或前端资源才需要编译，脚本按文件时间戳自行判断。

- 源码模式下左上角版本号形如 `v<最近 tag>.devN+<领先提交数>（源码模式）`：基线取最近的 tag，`+N` 是距该 tag 领先的 commit 数。打包版显示的是包内固化的版本号，不带此后缀。

- 判断"这次跑的到底是哪份代码"看输出末尾 `[verify]`：`源码模式 ✓` 表示后端来自仓库 `.venv`；若打印成其它目录下的 `runtime\python\python.exe`，则说明起的是打包产物。

***

## 环境要求

| 项目      | 要求                             |
| ------- | ------------------------------ |
| 操作系统    | Windows 10/11 **64-bit**       |
| 权限      | **管理员权限**（窗口截图必需）              |
| Python  | 3.11（源码构建需要；发行包自带）             |
| 游戏窗口分辨率 | 1280×720                       |
| GPU（可选） | NVIDIA / AMD 独立显卡（DirectML 加速） |

***

## 使用说明

1. 打开游戏到主界面（分辨率 **1280×720**）
2. 运行解压根目录的 `MaaRacingMaster.exe`（启动时自动请求管理员权限）
3. 在主控页用任务卡编排执行顺序（如：启动游戏 → 极速狂飙 → 关闭游戏）；点卡片展开可设置驾驶模式、宏与起始阶段
4. 点击「开始运行」；勾选多个任务时按列表顺序连续执行

> **驾驶方式**：默认「全程直行」；「深度几何」需要先安装模型包；「宏录制」先在卡片里录制或选择宏。缺少依赖时，设置页的「环境中心」会给出检测与下载引导。

> **调试功能**：PEEP 实时预览调试帧；DEBUG 存盘模式将标注截图保存至用户数据目录。

***

## 模块化架构

平台以「控制依赖传播 + 治理资源所有权」为核心原则：

- **插件经能力接口接触宿主**：插件只能通过 `capture` / `gamepad` / `debug_renderer` 等窄接口办事，**无法获得高权限宿主对象**。

- **资源所有权统一治理**：虚拟手柄用租约（context manager）表达借还；渲染器由 Context 的 `ExitStack` 托管生命周期，插件退出（含异常）时自动释放，不泄漏、不双释放。

- **克制不滥用抽象**：高权限对象必须隔离、有生命周期的资源必须治理；而稳定纯函数与只读数据（如窗口工具）直接依赖即可，不强行封装。

## 项目结构

```
MaaRacingMaster/
├── pyproject.toml                 # 项目配置（setuptools-scm 版本推导）
├── requirements.txt               # 源码运行的依赖范围
├── dev-shell.cmd                  # 源码模式一键编译 + 启动 GUI
├── maaracing_master/              # 📦 Python 应用包
│   ├── core/                      # 应用编排、JSONL RPC、识别与日志等共享能力
│   └── plugins/
│       ├── speedrush/             # 现役插件：极速狂飙（含模型清单、策略与文档）
│       └── _archive/              # 已退役插件（不随安装包分发）
├── apps/MaaRacingMaster.Shell/    # 🖥️ WinUI 3 壳 + WebView2 前端
├── templates/plugin/              # 🧪 插件开发样板（复制到 plugins/ 即成新模块）
├── assets/                        # 应用图标与运行资源
├── tools/                         # 跨插件工具（pipeline 工具链、MaaFw 审计、活跃实验）
├── tests/                         # 单元测试
├── scripts/release/               # 打包、模型包与发布脚本
├── skills/                        # 协作与发版流程说明
└── docs/                          # 架构 / Wiki / 更新日志
```

## 技术栈

| 分类   | 组件                                                                     | 用途                 |
| ---- | ---------------------------------------------------------------------- | ------------------ |
| 流程编排 | [MAA Framework](https://github.com/MaaAssistantArknights/MaaFramework) | 窗口截图 + Pipeline 驱动 |
| 视觉识别 | YOLO + ONNX Runtime (DirectML)                                         | 实时目标检测             |
| 深度与车道 | MoGe-2 / TwinLiteNet+（可选模型包）                                           | 单目深度几何与车道线观测       |
| 文字识别 | RapidOCR                                                               | 游戏内数字/文本识别         |
| 虚拟手柄 | vgamepad                                                               | Xbox 360 手柄模拟      |
| 图像处理 | OpenCV 5.x                                                             | 模板匹配 / 可视化         |
| GUI  | WinUI 3（Windows App SDK）+ WebView2                                     | 原生窗口 + HTML 前端     |
| 运行时  | Python 3.11（发行包内嵌）                                                    | 应用后端               |

***

## 开发指南

- **开发新活动插件**：从 [templates/plugin/](templates/plugin/README.md) 样板开始，复制即装

- **极速狂飙域**：规则事实见 [RULES.md](maaracing_master/plugins/speedrush/RULES.md)，实现与坑点见 [CODE_WIKI.md](maaracing_master/plugins/speedrush/CODE_WIKI.md)

- **模型权重与安装**：[模型说明](maaracing_master/plugins/speedrush/resources/models/README.md)

- 快速诊断环境/依赖问题：[docs/SELF\_CHECK.md](docs/SELF_CHECK.md)

- 项目地图与「我要改 X 应该去哪里」：[docs/CODE\_WIKI.md](docs/CODE_WIKI.md)

- 平台核心层实现细节与坑点：[maaracing\_master/core/CODE\_WIKI.md](maaracing_master/core/CODE_WIKI.md)

- 架构决策与理由：[docs/adr/](docs/adr/README.md)

- 更新说明（每个版本改了什么）：[docs/update\_log.md](docs/update_log.md)

- 提交纪律与发版流程：[skills/project-update/SKILL.md](skills/project-update/SKILL.md)

***

## 许可证

本项目源码采用 [Apache-2.0](LICENSE) © ZRY。本项目为非营利开源项目，不提供商业授权。

> 分层许可说明：项目源码为 Apache-2.0；模型权重按文件边界各自沿用上游许可——物品感知为 AGPL-3.0 衍生，深度几何为 Apache-2.0，线观测按 BDD100K 非商业/科研许可分发。主安装包不携带权重，模型包内随附完整许可与来源说明。运行依赖各自保留其许可证（含 LGPL-3.0 的 MaaFramework）。
> 详见 [THIRD\_PARTY\_LICENSES.md](THIRD_PARTY_LICENSES.md) 与 [模型权重说明](maaracing_master/plugins/speedrush/resources/models/README.md)。

<br />
