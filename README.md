<p align="center">
  <img src="assets/mra_icon.png" width="128" alt="MaaRacingMaster logo">
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
> - **打 tag 即出正式 Release 包**；未打 tag 的改动仅提交到 master，可能尚未打包或有中途变化。
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
> 🚧 **开发状态**
>
> 项目处于积极开发中：当前内置活动插件见 [maaracing\_master/plugins/](maaracing_master/plugins/)，
> 各插件的功能细节与完成度以其自述文档为准。
> 如遇异常，请提交 Issue 或参考开发文档自诊。

***

> **本文是什么**：面向使用者与访客的项目门面——定位、快速开始、环境要求与文档入口。
> **本文不是什么**：不是架构与实现说明（见 [docs/CODE_WIKI.md](docs/CODE_WIKI.md)）；不是行为守则（见 [AGENTS.md](AGENTS.md)）。
> **信源等级**：L3（导航与摘要）。

## 简介

**MaaRacingMaster** 是一款模块化游戏自动化平台。

基于图像识别与模拟输入，把游戏中重复的日常劳作交给电脑！

每个活动是一个独立插件，放入即装、删除即卸。

## 演示

<p align="center">
  <video src="https://github.com/user-attachments/assets/9bf47361-2773-447c-9900-bdf70d4b2af0" width="640" controls muted></video>
  <br>
  <em>无人值守自动运行：实时预览 → 智能决策 → 结算收尾</em>
</p>

### 界面截图

| GUI 主控                              | 今日看板                                  | PEEP 实时预览                        |
| ----------------------------------- | ------------------------------------- | -------------------------------- |
| ![主控](assets/demo/shot_control.png) | ![看板](assets/demo/shot_dashboard.png) | ![预览](assets/demo/shot_peep.png) |

***

## 支持本项目

如果本项目对你有帮助，欢迎[在爱发电上赞助](https://afdian.com/a/MaaRacingMaster)，
支持持续的开发与维护。您的每一份支持都对本项目的成长意义重大。

***

## 亮点功能

- **活动全链路自动化**：进入活动 → 完成玩法 → 领取奖励，一站到底

- **多模态识别**：模板匹配 / OCR / 目标检测，按活动需求组合

- **可视化调试台**：ROI 校准、结构树、决策流水回放（NavKit）

- **PEEP 实时预览**：无需打断运行，实时观察每一步识别与决策

- **断点续跑**：从任意阶段开始，分阶段调试

- **插件化架构**：一活动 = 一自包含目录，复制即装、删除即卸

- **解压即用**：发行包自带 Python 运行时与全部依赖，无需配置环境

***

## 快速开始

分两类入口，按你的目的二选一即可：

- **只想直接使用**（不写代码）→ 跳到下方「下载即用包」一节

- **要改代码 / 参与开发** → 继续往下，走「从源码构建」四步

> 当前处于**开发阶段**：GitHub [Releases](https://github.com/d542Bb/MaaRacingMaster/releases) 上已提供 pre-release 打包，正式 v1.0.0 尚未发布。

### 下载即用包（普通用户，无需编译）

1. 到 GitHub [Releases](https://github.com/d542Bb/MaaRacingMaster/releases) 下载最新 **`MaaRacingMaster-<版本>-win-x64.zip`**。
2. 用资源管理器把它**解压到任意本地目录**。
3. 打开该文件夹，**双击根目录的** **`MaaRacingMaster.exe`** 启动（包内唯一入口，薄 Launcher 会拉起 `app\` 下的 GUI；已自带 Python 运行时与全部依赖，启动时自动弹出 UAC 提权）。
4. 若所用插件依赖虚拟手柄，需先安装 [ViGEmBus 虚拟手柄驱动](https://github.com/nefarius/ViGEmBus/releases)；插件对依赖的要求见其自述文档。

> 成功标志：GUI 窗口出现，左上角版本号显示当前 `v*`，活动模块列表正常加载（后端已连接）。

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

> 成功标志：GUI 窗口出现，左上角版本号显示当前 `v*`，活动模块列表正常加载（后端已连接）。

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

- 源码模式下左上角版本号形如 `v0.21.0.dev2+102（源码模式）`：基线取最近的 tag，`+102` 是距该 tag 领先的 commit 数。打包版显示的是包内固化的版本号，不带此后缀。

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
3. 选择活动模块与起始阶段（支持断点），点击「开始运行」

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
├── maaracing_master/           # 📦 Python 应用包
│   ├── core/                      # 主程序：应用编排 + 共享能力
│   │   ├── controller.py          # 总控编排（生命周期 + 能力门面 ActivityContext）
│   │   ├── sidecar.py             # JSONL RPC 业务后端
│   │   ├── registry.py            # 插件自动扫描注册表
│   │   ├── capabilities.py        # 能力 Protocol + adapter
│   │   ├── base.py                # ActivityContext / ActivityModule 基类
│   │   ├── yolo_detector.py / wgcap.py / window_utils.py / debug.py
│   │   └── logger.py / paths.py / ...
│   └── plugins/                   # 🧩 活动插件（一活动 = 一自包含目录，各含 CODE_WIKI.md）
├── apps/MaaRacingMaster.Shell/                # 🖥️ WinUI 3 图形界面（含 .sln）
├── templates/plugin/              # 🧪 插件开发样板（复制到 plugins/ 即成新模块）
├── assets/                        # 应用图标 / 演示素材 / 配置
├── tools/                         # 辅助开发工具（NavKit 调试台/训练/审计）
├── tests/                         # 单元测试
├── scripts/                       # 发布打包脚本
└── docs/                          # 架构 / Wiki / 更新日志
```

## 技术栈

| 分类   | 组件                                                                     | 用途                 |
| ---- | ---------------------------------------------------------------------- | ------------------ |
| 流程编排 | [MAA Framework](https://github.com/MaaAssistantArknights/MaaFramework) | 窗口截图 + Pipeline 驱动 |
| 视觉识别 | YOLO + ONNX Runtime (DirectML)                                         | 实时目标检测             |
| 文字识别 | RapidOCR                                                               | 游戏内数字/文本识别         |
| 虚拟手柄 | vgamepad                                                               | Xbox 360 手柄模拟      |
| 图像处理 | OpenCV 4.x                                                             | 模板匹配 / 可视化         |
| GUI  | WinUI 3（Windows App SDK）                                               | 原生窗口 + HTML 前端     |

***

## 开发指南

- **开发新活动插件**：从 [templates/plugin/](templates/plugin/README.md) 样板开始，复制即装

- 快速诊断环境/依赖问题：[docs/SELF\_CHECK.md](docs/SELF_CHECK.md)

- 项目地图与「我要改 X 应该去哪里」：[docs/CODE\_WIKI.md](docs/CODE_WIKI.md)

- 平台核心层实现细节与坑点：[maaracing\_master/core/CODE\_WIKI.md](maaracing_master/core/CODE_WIKI.md)

- 架构决策与理由：[docs/adr/](docs/adr/README.md)

- 更新说明（每个版本改了什么）：[docs/update\_log.md](docs/update_log.md)

***

## 许可证

本项目源码采用 [Apache-2.0](LICENSE) © ZRY。

> 分层许可说明：项目源码为 Apache-2.0（宽松许可）；当前版本不随包分发模型权重，若启用的插件自带 YOLO 等模型，
> 其权重单独沿用 AGPL-3.0；运行时依赖各自保留其许可证
> （含 LGPL-3.0 的 MaaFramework）。详见 [THIRD\_PARTY\_LICENSES.md](THIRD_PARTY_LICENSES.md)。

<br />
