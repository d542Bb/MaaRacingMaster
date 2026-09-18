# MaaRacingMaster — Code Wiki（项目地图）

> **本文是什么**：项目**地图与导航**——整体架构、各层职责、东西在哪、要改什么该去哪。
> **本文不是什么**：不是代码百科。core 模块实现细节、类索引与坑点见 [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md)；活动域知识见 `plugins/<id>/CODE_WIKI.md`；架构决策的理由与状态见 [docs/adr/](adr/README.md)；MaaFW 协议口径见 [MAAFW_GUIDE.md](MAAFW_GUIDE.md)；子系统施工事实见各 `README.md`。
> **信源等级**：L3 —— 可作「东西在哪、从哪进、整体怎么分层」的直接依据；**不得**作为实现细节、取值或规则的最终依据（路由到该事实的 home 核对）。
> **真源路由**：按问题类型查出处——见 [`AGENTS.md`](../AGENTS.md)「信源路由」与「信源等级与文档头」。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../AGENTS.md)；本文只补充导航职责。

## 分域文档

| 域 | 文档 | 覆盖 |
|---|---|---|
| 项目地图 | **本文** | 整体架构 / 目录 / 运行流程 / 改动导航 |
| 平台核心层 | [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md) | controller / 导航光标 / 采集 / 日志 / 调试 / 坑点 / 类索引 |
| 巅峰鉴宝 | [plugins/treasure/CODE_WIKI.md](../maaracing_master/plugins/treasure/CODE_WIKI.md) | 阶段状态机 / 出价策略 / OCR / 渲染 / 域坑点 |
| 极速狂飙 | [plugins/speedrush/CODE_WIKI.md](../maaracing_master/plugins/speedrush/CODE_WIKI.md) | 流程编排 / 驾驶门控与节拍 / 演示录制 |
| MaaFW 协议 | [MAAFW_GUIDE.md](MAAFW_GUIDE.md) | 能力签名 / Pipeline 协议 / Custom 契约 / 真源组织 / 红线 |
| 架构决策 | [adr/](adr/README.md) | 为什么这么定 + 状态（是否仍生效） |
| 版本宪法 | [NAVKIT_V4_PLAN.md](NAVKIT_V4_PLAN.md) | §1 六条不变量（最高规则）+ 节点模型与迁移史 |
| GUI 壳工程 | [apps/MaaRacingMaster.Shell/README.md](../apps/MaaRacingMaster.Shell/README.md) | 锁定版本 / WinUI 3 API / 构建坑 / IPC 契约 |
| 前端与图标 | [apps/MaaRacingMaster.Shell/frontend/README.md](../apps/MaaRacingMaster.Shell/frontend/README.md) | 图标真源 / file:// 约束 / 前端规范 |
| NavKit 工具链 | [tools/navkit/README.md](../tools/navkit/README.md) | 入口命令 / 模板采集工作流 / 校验器护栏 |
| 环境自检 | [SELF_CHECK.md](SELF_CHECK.md) | 分步验收清单（新环境 / 跨机器） |
| 变更记录 | [update_log.md](update_log.md) | 对外变更（Release 正文由此抽取） |

***

## 1. 项目概述

### 1.1 项目定位

MaaRacingMaster 是一款基于**计算机视觉**与**虚拟手柄控制**的模块化游戏自动化平台，以统一模块框架承载《巅峰极速》各类重复性活动的自动化。活动以插件形式装载：一活动 = 一自包含目录，放入即装、删除即卸。

### 1.2 核心技术栈

| 层级     | 技术组件                                     | 用途                   |
| ------ | ---------------------------------------- | -------------------- |
| 流程编排   | MAA Framework                              | UI 流程编排 + 窗口控制 + 截图  |
| 视觉识别   | YOLO + ONNX Runtime (DirectML)           | 跨活动目标检测            |
| 手柄模拟   | vgamepad                                 | Xbox 360 虚拟手柄，摇杆精确控制 |
| 图像处理   | OpenCV                                   | 模板匹配、Hough 直线检测、可视化  |
| OCR    | RapidOCR                                 | 游戏内金额 / 按钮文字识别       |
| GUI 框架 | WinUI 3 (Windows App SDK) + WebView2     | 原生窗口 + HTML 前端       |
| 系统交互   | XInput API (Win32)                       | 物理手柄检测，避免冲突          |

> 各依赖的锁定版本与安装边界见 [requirements.txt](../requirements.txt) 与 [pyproject.toml](../pyproject.toml)；框架能力签名与红线见 [MAAFW_GUIDE.md](MAAFW_GUIDE.md)。

***

## 2. 整体架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│               GUI 层 (apps/MaaRacingMaster.Shell/)               │
│        WinUI 3 窗口 + HTML 前端 + sidecar 进程托管                │
├─────────────────────────────────────────────────────────────────┤
│                      主控层 (core/controller.py)                 │
│      能力门面 ActivityContext + 模块生命周期 + 全局设置            │
│      （MAA 对象 Tasker/Resource 归插件模块创建，主控不再持有）      │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐  ┌──────────────────────────────────┐  │
│  │  导航引擎 (core/)    │  │  活动插件                        │  │
│  │  nav_graph/clicker   │  │  (plugins/<id>/)                │  │
│  │  - NavKit v4 资产    │  │  - 本活动阶段状态机              │  │
│  │  - 光标导航/虚拟手柄   │  │  - 自带模板/图/policy 真源        │  │
│  │  - 多尺度模板匹配     │  │  - 自带调试渲染器                │  │
│  │  - 意图/真实点击     │  │  - 能力经 ActivityContext 取用    │  │
│  └─────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐  ┌──────────────────────────────────┐  │
│  │  YOLO 检测器        │  │  调试可视化                      │  │
│  │  (yolo_detector.py) │  │  (debug.py)                      │  │
│  │  - ONNX Runtime     │  │  - PEEP 精简预览                 │  │
│  │  - DirectML GPU     │  │  - 每帧截图标注存盘              │  │
│  │  - per-class NMS    │  │  - 导航/检测框标注               │  │
│  └─────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│                      基础设施层                                  │
│   日志系统 / Pipeline 日志 / 窗口与手柄检测 / 中文路径补丁         │
└─────────────────────────────────────────────────────────────────┘
```

各模块的职责、类索引与坑点见 [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md)。

### 2.2 模块插件化

主控（controller）只做**生命周期与能力门面**，活动流程由插件模块承载：

- **插件自包含**：一活动 = 一目录（`manifest.py` + `module.py` + `resources/`），由 `registry` 扫描 `plugins/*/manifest.py` 发现；放入即装、删除即卸。开发新插件从 [templates/plugin/](../templates/plugin/README.md) 样板起步。
- **能力经窄接口**：插件只能通过 `capture` / `gamepad` / `debug_renderer` 等 capability 接触宿主，拿不到高权限宿主对象；资源所有权经租约与 `ExitStack` 治理。
- **阶段流转在模块内**：活动阶段状态机、策略与资产全部属于插件；阶段清单的唯一真源是该插件 `resources/policy/<id>.policy.json` 的 `perception.stages.order`，语义见对应域文档。

***

## 3. 目录结构

```
MaaRacingMaster/
├── maaracing_master/          #  Python 应用包
│   ├── core/                  # 主程序：应用编排 + 共享能力（见 core/CODE_WIKI.md）
│   ├── plugins/<id>/          #  活动插件（一活动 = 一自包含目录，含 resources/ 与 CODE_WIKI.md）
├── apps/                      # 🖥️ GUI（MaaRacingMaster.Shell：WinUI 3 shell + WebView2 前端；Launcher）
├── tools/                     # 开发工具（navkit 工作台 / training / experiments / runtime_audit）
├── tests/                     # 单元测试与契约锁（CI 矩阵 3.11）
├── templates/plugin/          # 插件开发样板（复制到 plugins/ 即成新模块）
├── scripts/release/           # 发布打包（assemble.ps1）与 changelog 抽取
├── assets/                    # 应用级资产（图标 / 演示素材 / 配置）
├── docs/                      # 信源文档根（信源路由见 AGENTS.md）
│   ├── CODE_WIKI.md           # 本文（项目地图）· MAAFW_GUIDE.md · NAVKIT_V4_PLAN.md · SELF_CHECK.md
│   ├── adr/                   # 架构决策记录（决策 + 理由 + 状态，只增不改）
│   ├── announcement.md/.json  # 公告通知卡规范（关于页）
│   ├── design/                # 前端设计稿
│   └── update_log.md          # 对外变更记录（Release 正文由此抽取）
└── .github/workflows/         # CI：test.yml（单测）、release.yml（发布）、mirror-to-cnb.yml
```

**文件级清单不在此维护**——目录是重构中最易失效的镜像。要找某个模块/类，用 `core/CODE_WIKI.md` 的类索引或直接 grep 代码。

运行期数据（自动生成，gitignore）不住仓库：`%APPDATA%/MaaRacingMaster/` 下 `config/`、`data/`、`logs/`、`framework/`、`debug/` 五目录，见 [core/CODE_WIKI.md §调试操作](../maaracing_master/core/CODE_WIKI.md)。

***

## 4. 运行流程

### 4.1 启动

```
双击发布包根目录 MaaRacingMaster.exe（薄 Launcher → app\ 下 Shell；exe manifest 自动 UAC 提权）
  → WinUI 3 shell 创建窗口并加载 HTML 前端
  → shell 拉起 Python sidecar（python -m maaracing_master）
    → sidecar 初始化 Controller，等待 stdin JSONL RPC
  → 前端通过 mra.call(method, params) 与 sidecar 通信
```

独立调试 sidecar（不经 GUI）：`.venv\Scripts\python.exe -u -m maaracing_master.core.sidecar`。

### 4.2 点击"开始"后

```
前端按钮（app.js）
  → mra.call('start', {module_id, start_from}) → C# → sidecar 的 start handler
  → controller.start_module(module_id, start_from)
    → connect()：幂等窗口连接（超时保护 + 720p 统一 + 屏幕内校验）
    → 按 module_id 从 registry 选插件模块
    → module.start(start_from)：模块内部解析断点并进入活动循环（worker 线程）
       └─ 模块自己：装渲染器 → 初始化检测/OCR → 主循环；exit 时 ctx 托管资源自动释放
```

活动循环细节见对应域文档；主控层不编排阶段。

***

## 5. 关键约定

| 约定 | 取值 | 说明 |
|---|---|---|
| 游戏分辨率 | 1280×720 | 所有坐标基于此（各插件 ROI / 按钮均按此归一化） |
| 截图通道 | WGC 中心缓存（唯一） | 引擎永不自截帧、无回退通道，见 [ADR-0003](adr/0003-运行时拓扑硬约束.md) |
| 业务数值 | 插件自带 `resources/policy/<id>.policy.json` | **不在代码内硬编码**，是各插件 ROI / 阈值 / 仲裁的唯一数据面真源 |
| 版本号 | setuptools-scm 从 Git Tag 推导 | **git tag 是唯一信源**，禁止手改源码版本号——规则与发版流程见 [`AGENTS.md`](../AGENTS.md) 与 [`skills/project-update/SKILL.md`](../skills/project-update/SKILL.md) |

***

## 6. 我要改 X，应该去哪里

| 想改什么 | 去哪 |
|---|---|
| 某个活动的流程 / 策略 / 按钮 | `plugins/<id>/`（模块实现 + `resources/pipeline` 图 + `policy` 数据面），域知识见其 `CODE_WIKI.md` |
| 活动怎么运转（游戏规则） | `plugins/<id>/RULES.md`（外部事实，修改须人工复核） |
| 主控生命周期、能力门面、点击方式 | `core/controller.py` + [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md) |
| 光标导航 / 虚拟手柄 | `core/gamepad_cursor.py` + `core/resources/stick_speed_model.json` |
| 截图采集 / 帧通路 | `core/wgcap.py`（拓扑约束见 [ADR-0003](adr/0003-运行时拓扑硬约束.md)，不得新起截图通路） |
| 日志级别 / 通道 / 落盘 | `core/logger.py` + [core/CODE_WIKI.md §1.5](../maaracing_master/core/CODE_WIKI.md) |
| 模板图与锚点（采集 / ROI） | [tools/navkit/README.md](../tools/navkit/README.md)（采集工作流 + 校验器） |
| 文字识别（OCR）引擎与调参 | `core/ocr.py`（跨插件共享底座的唯一真源）+ [core/CODE_WIKI.md §1.11](../maaracing_master/core/CODE_WIKI.md)；插件侧只留「文本怎么解释」的领域口径 |
| 页面图节点 / 连线 | `plugins/<id>/resources/pipeline/*.json`，用 MPE 编辑（入口见 navkit README） |
| GUI 窗口 / 前端 / 图标 | [apps/MaaRacingMaster.Shell/README.md](../apps/MaaRacingMaster.Shell/README.md) 与其 [frontend/README.md](../apps/MaaRacingMaster.Shell/frontend/README.md) |
| MaaFW 用法 / 协议行为 | [MAAFW_GUIDE.md](MAAFW_GUIDE.md)；冲突时以官方文档为准 |
| 新活动模块 | [templates/plugin/](../templates/plugin/README.md) 样板起步 |
| 环境跑不起来 | [SELF_CHECK.md](SELF_CHECK.md) |

改动前的影响面自查与验证要求见 [`AGENTS.md`](../AGENTS.md)「改动影响自查」。

***

## 7. GUI 宿主

正式 GUI = `apps/MaaRacingMaster.Shell/`（WinUI 3 shell + WebView2 HTML 前端）+ `core/sidecar.py`（Python 业务后端，JSONL RPC）。

宿主选型的理由、被否方案与硬约束（不得回退到 WPF `WindowChrome` 或 `FormBorderStyle.None` 类方案）见 [ADR-0004](adr/0004-GUI宿主定案.md)；**窗口与标题栏形态及其取舍理由**（移除系统标题栏，窗口控制由 HTML 自绘）见壳工程 [README](../apps/MaaRacingMaster.Shell/README.md)「窗口形态与标题栏」——原 ADR-0005 已按 [ADR-0006](adr/0006-决策粒度只收跨模块约束.md)（子系统形态不进 ADR）迁入该处；壳工程结构、窗口 API 与构建坑同样见其 README。