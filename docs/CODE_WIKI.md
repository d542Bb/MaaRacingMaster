# MaaRacingMaster — Code Wiki（主文档）

> 《巅峰极速》模块化游戏自动化平台 —— 完整代码架构文档
>
> **文档导航（Code Wiki 已按功能域拆分，共 2 份）**：
>
> - **本文件（主文档）**：架构总览 / 目录结构 / 主程核心模块 / 依赖 / 运行流程 / 配置常量 / 开发调试 / 主程坑点 / GUI 选型
>
> - [鉴宝域 CODE\_WIKI（plugins/treasure）](../maaracing_master/plugins/treasure/CODE_WIKI.md)（treasure\_\* 全模块 / 出价策略 / 鉴宝模板 / 鉴宝坑点）

***

## 目录

1. [项目概述](#1-项目概述)
2. [整体架构](#2-整体架构)
3. [目录结构详解](#3-目录结构详解)
4. [核心模块说明](#4-核心模块说明)
5. [关键类与函数索引](#5-关键类与函数索引)
6. [模块依赖关系](#6-模块依赖关系)
7. [运行流程详解](#7-运行流程详解)
8. [关键配置与常量](#8-关键配置与常量)
9. [开发与调试](#9-开发与调试)
10. [已知坑点与注意事项](#10-已知坑点与注意事项)
11. [GUI 宿主定案](#11-gui-宿主定案winui-3)
    附录：类速查表

***

## 1. 项目概述

### 1.1 项目定位

MaaRacingMaster 是一款基于**计算机视觉**与**虚拟手柄控制**的模块化游戏自动化平台，以统一模块框架承载《巅峰极速》各类重复性活动的自动化。当前入库可用的活动插件为**巅峰鉴宝**（treasure）。

### 1.2 核心技术栈

| 层级     | 技术组件                                     | 用途                   |
| ------ | ---------------------------------------- | -------------------- |
| 流程编排   | MAA Framework 5.12.x                     | UI 流程编排 + 窗口控制 + 截图  |
| 视觉识别   | YOLO11 + ONNX Runtime (DirectML)         | 跨活动目标检测（当前无模块启用）     |
| 手柄模拟   | vgamepad 0.1.x                           | Xbox 360 虚拟手柄，摇杆精确控制 |
| 图像处理   | OpenCV 5.x                               | 模板匹配、Hough 直线检测、可视化  |
| OCR    | RapidOCR 3.9.x                           | 鉴宝金额 / 出价按钮文字识别      |
| GUI 框架 | WinUI 3 (Windows App SDK 1.8) + WebView2 | 原生窗口 + HTML 四 Tab 前端 |
| 系统交互   | XInput API (Win32)                       | 物理手柄检测，避免冲突          |

### 1.3 核心工作流

```
启动 → 连接游戏窗口 → 按 module_id 分发活动插件 → 插件内部阶段状态机循环 → 完成/急停收尾
```

***

## 2. 整体架构

### 2.1 分层架构图

```
┌─────────────────────────────────────────────────────────────────┐
│               GUI 层 (apps/MaaRacingMaster.Shell/)                          │
│        WinUI 3 窗口 + HTML 前端 + sidecar 进程托管                │
├─────────────────────────────────────────────────────────────────┤
│                      主控层 (controller.py)                     │
│      能力门面 ActivityContext + 模块生命周期 + 全局设置           │
│      （MAA 对象 Tasker/Resource 归插件模块创建，主控不再持有）     │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐  ┌──────────────────────────────────┐  │
│  │  导航引擎 (core/)    │  │  活动插件                        │  │
│  │  nav_graph/clicker   │  │  (plugins/<id>/)                │  │
│  │  - NavKit v4资产     │  │  - 本活动阶段状态机            │  │
│  │  - 光标导航/虚拟手柄   │  │  - 自带模板/图/policy 真源      │  │
│  │  - 多尺度模板匹配     │  │  - 自带调试渲染器               │  │
│  │  - 意图/真实点击     │  │  - 能力经 ActivityContext 取用  │  │
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
│  ┌────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────┐  │
│  │ 日志系统    │ │ Pipeline日志 │ │ 窗口/手柄检测 │ │ UTF8补丁  │  │
│  │ (logger.py)│ │(pipeline_    │ │(window_utils)│ │(opencv_  │  │
│  │            │ │  logger.py)  │ │              │ │ utf8_    │  │
│  │            │ │              │ │              │ │ patch.py)│  │
│  └────────────┘ └──────────────┘ └──────────────┘ └──────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 阶段流程

项目采用**模块插件化**架构：主控（controller）只做生命周期与能力门面，活动流程由插件模块承载。

- **巅峰鉴宝**（treasure 插件）：阶段状态机（游戏大厅 → 活动页 → 鉴宝大厅 → 场次 → 鉴宝师 → 逐回合出价 → 结算 → 分红），阶段清单唯一真源 = 该插件 `resources/policy/treasure.policy.json` 的 `perception.stages.order`，语义见 [鉴宝文档 §1](../maaracing_master/plugins/treasure/CODE_WIKI.md)。

主控不再持有活动流程编排：`MaaRacingMasterController` 仅负责窗口连接、能力门面（`ActivityContext`）、模块生命周期与全局设置，活动阶段流转全部在模块内部。

***

## 3. 目录结构详解

```
├── pyproject.toml                            # setuptools-scm 包配置
├── requirements.txt                          # Python 依赖清单
├── AGENTS.md                                 # AI 助手项目配置
├── README.md                                 # 用户说明文档
│
├── maaracing_master/                      # 📦 核心应用包
│   ├── __init__.py                           # 版本号导出（setuptools-scm 自动生成）
│   ├── __main__.py                           # python -m 入口
│   ├── core/                                 # 主程序（应用层）
│   │   ├── controller.py                     # 主控编排（生命周期 + 能力门面 ActivityContext，已不直接持有 MAA 对象）
│   │   ├── sidecar.py                        # JSONL RPC 业务后端（供 MaaRacingMaster.Shell.exe 托管）
│   │   ├── registry.py                       # 插件自动扫描注册表（扫 plugins/*/manifest.py，含有效期门）
│   │   ├── module_validity.py                # 模块有效期判定（manifest VALID_FROM / VALID_UNTIL，纯函数）
│   │   ├── base.py                           # ActivityContext / ActivityModule 基类
│   │   ├── capabilities.py                   # typed capability 窄接口 + adapter
│   │   ├── clicker.py / gamepad_cursor.py / audio_volume.py    # 点击方式 / 手柄光标 / 静音
│   │   ├── render_plan.py / stage_tracker.py / roi_config.py   # 渲染计划 / 阶段记录 / ROI 底座
│   │   ├── debug.py / debug_io.py            # PEEP 预览 + 调试落盘 IO worker
│   │   ├── module_config.py                  # 模块配置契约（module.config.json 加载器）
│   │   ├── paths.py                          # 用户数据目录（%APPDATA%/MaaRacingMaster，五目录）
│   │   ├── opencv_utf8_patch.py / vgamepad_lazy.py / wgcap.py
│   │   └── yolo_detector.py                  # 跨活动视觉基础设施
│   └── plugins/                              # 活动插件（一活动 = 一自包含目录，放入即装/删除即卸）
│       └── treasure/                         # 巅峰鉴宝
│           ├── CODE_WIKI.md                  # 鉴宝域文档
│           ├── manifest.py                   # ID + MODULE_CLASS + 可选 VALID_FROM/VALID_UNTIL（registry 扫描用）
│           ├── __init__.py                   # PLUGIN_DIR / RES_DIR / IMAGE_DIR / PIPELINE_DIR / POLICY_PATH / nav_source()
│           ├── module.py / detector.py / ocr.py / strategy.py
│           ├── policy_bridge.py / eggs.py / renderer.py / store.py
│           └── resources/                    # 插件专属资源（自包含）
│               ├── image/                    # 全部鉴宝模板
│               ├── pipeline/treasure.json    # v4 图节点真源
│               └── policy/treasure.policy.json # 感知 spec + 决策 + tuning 唯一数据面真源
│
├── assets/                                   # 应用级资产（插件素材已全部内聚到各自 plugins/<id>/resources/）
│   ├── config/maa_option.json                # MAA 框架配置
│   ├── demo/                                 # 界面截图（shot_*.png）
│   ├── icon.ico                              # 应用图标
│   └── mra_icon.png                          # README 展示图标
│
├── apps/
│   │   └── MaaRacingMaster.Shell/                        # 🖥️ 正式 GUI（WinUI 3 shell + WebView2）
│   │       ├── MainWindow.xaml(.cs)          # 窗口 + sidecar 生命周期 + 消息转发
│   │       ├── PythonSidecar.cs              # JSONL transport 契约实现
│   │       ├── App.xaml(.cs)                 # 应用入口（DISABLE_XAML_GENERATED_MAIN）
│   │       └── frontend/                     # HTML 前端（四 Tab：主控/数据/设置/关于，规范见其 README.md）
│   │           ├── index.html                # 页面结构 + 元素 id
│   │           ├── style.css                 # 纯 CSS 样式（无 CDN）
│   │           ├── app.js                    # mra.call RPC + 页面交互逻辑
│   │           ├── icons.js                  # 图标真源（Lucide v1.45.0 数据，MRAIcons）
│   │           ├── vendor.morphicons.js      # morphicons v1.7.1 vendor 摊平（图标变形动画）
│   │           └── README.md                 # 前端与图标规范权威文档
│   └── MaaRacingMaster.Launcher/                         # C 启动器（提权 + 定位 shell）
│
├── tools/                                    # 开发工具脚本（按用途分组）
│   ├── mouse_overlay.py                      # 独立 Overlay 工具（屏幕十字准星）
│   ├── navkit/                               # NavKit v4 工具链（mpe.cmd 起 MPE Studio + 迁移校验器）
│   ├── training/                             # 模型训练与数据准备
│   │   ├── train.py                          # YOLO 训练 + ONNX 导出脚本
│   │   ├── dataset.yaml                      # 数据集类别配置
│   │   └── auto_label.py                     # 自动标注工具
│
├── tests/                                    # 单元测试（纯逻辑，CI 矩阵 3.11）
├── scripts/                                  # 发布打包脚本
│   └── release/assemble.ps1                  # 发布打包脚本
│
├── .github/workflows/                        # CI：test.yml（单测）、release.yml（发布）
├── docs/                                     # 信源文档根（信源路由见 AGENTS.md）
│   ├── CODE_WIKI.md                          # 本文档（主文档）；鉴宝域文档随插件（plugins/&lt;id&gt;/CODE_WIKI.md）
│   ├── MAAFW_GUIDE.md / SELF_CHECK.md
│   ├── NAVKIT_V4_PLAN.md                     # 版本宪法（§1 六条不变量）
│   ├── adr/                                  # 架构决策记录（决策 + 理由 + 状态，只增不改）
│   ├── announcement.md / announcement.json   # 公告通知卡规范（关于页）
│   ├── design/                               # 前端设计稿（pages/*.html、colors_and_type.css）
│   ├── latest_release.json                   # 最近发布元数据
│   └── update_log.md                         # 对外变更记录（Release 正文由此抽取）
│
# 运行期数据（自动生成，gitignore）：已迁至 %APPDATA%/MaaRacingMaster/
# ├── config/                                 # profile.json、maa_option.json
# ├── data/                                   # data/treasure/treasure.db
# ├── logs/                                   # logs/<会话>/（MaaRM_*.log + trace.jsonl）
# ├── framework/                              # MAA 框架自产物（maafw.log、cache）
# └── debug/                                  # debug/<module>/<会话>/（调试台契约）
```

***

## 4. 核心模块说明

> 本文档覆盖**主程核心模块**。按功能域拆分：
>
> - **鉴宝域**（treasure\_module / treasure\_detector / treasure\_ocr / treasure\_renderer / bid\_strategy）→ [鉴宝域文档](../maaracing_master/plugins/treasure/CODE_WIKI.md)

### 4.1 [controller.py](../maaracing_master/core/controller.py) — 主控编排器

**职责**（v0.14+ 已去流程化，专注生命周期与能力门面）：

- 窗口连接（幂等，仅创建 `Win32Controller`，MAA Tasker/Resource 归活动模块）

- 能力门面 `ActivityContext`（capture / gamepad / debug\_renderer 等窄接口 + ExitStack 生命周期托管）

- 模块生命周期（`start_module(module_id, start_from)` 分发到插件 `ActivityModule.start`）

- 点击方式（`real` 前台鼠标 / `background` 后台手柄导航+A / `intent` 仅意图）与独立真实点击开关

- 运行时静音游戏（audio\_volume）、运行结束自动关游戏/退出程序、急停循环

- 虚拟手柄租约管理（`_get_gpad` 懒创建 / `_reset_gpad` / `_destroy_gpad` ctypes 从总线拔除）

**核心类**：`MaaRacingMasterController`

**关键属性**：

| 属性                  | 类型              | 说明                                                  |
| ------------------- | --------------- | --------------------------------------------------- |
| `controller`        | Win32Controller | 游戏窗口控制器（connect 时创建，幂等）                             |
| `ctx`               | ActivityContext | 能力门面（模块经窄接口办事）                                      |
| `active_module`     | ActivityModule  | 当前活动模块（当前入库：treasure）                               |
| `click_mode`        | str             | 点击方式：`real`（前台） / `background`（后台手柄）/ `intent`（仅意图） |
| `intent_mode`       | bool            | 是否仅准星意图（不真实点击）                                      |
| `gamepad_available` | bool            | vgamepad 驱动是否可用（ViGEmBus）                           |
| `module_active`     | bool            | 模块是否在运行                                             |

***

### 4.2 [gamepad\_cursor.py](../maaracing_master/core/gamepad_cursor.py) — 手柄光标导航引擎

**职责摘要**：签名剖面法识别游戏内白色圆盘光标（normal / interactive 两态）、摇杆-光标速度模型 + 闭环趋近导航、到位后确认点击（意图模式只导航不确认）；供 `core.clicker` 的「后台(手柄)」点击方式复用，与「前台(鼠标)」SendInput 同层。底座与手柄均依赖注入（复用 controller 的 `_gpad` / 模块的 capture），本模块不自建，避免手柄/截图冲突。

> **速度模型真源 = `maaracing_master/core/resources/stick_speed_model.json`**；光标识别三态、连续 P 趋近与速度模型标定的参数均出自该文件。

***

### 4.3 [yolo\_detector.py](../maaracing_master/core/yolo_detector.py) — YOLO 检测器

**职责**：

- ONNX Runtime 会话初始化（DirectML优先 → CUDA → CPU）

- 图优化 + DirectML 内核缓存

- 640×640 letterbox 预处理

- YOLOv8 输出解析（xywh → xyxy）

- **per-class NMS**：按类别分别做非极大值抑制，避免高置信类别压掉相邻类别

- 双阈值输出：正式检测（高置信度，供决策用）+ 全量低阈值检测（供debug可视化）

**核心类**：`YOLODetector`

**类别映射与阈值**：检测器为跨活动基础设施，**类别集由所服务的活动定义**，不在 core 文档写死。当前无入库模块使用本检测器。阈值可在 `YOLODetector.CLASS_CONF` 覆盖。

***

### 4.4 [mra\_shell](../apps/MaaRacingMaster.Shell) — GUI 宿主（WinUI 3 + HTML 前端）

> v0.13.0 起 GUI 定案为 WinUI 3 shell + WebView2 HTML 前端（定案理由见 §11）。壳工程结构、锁定版本与 WinUI 3 坑点见 [apps/MaaRacingMaster.Shell/README.md](../apps/MaaRacingMaster.Shell/README.md)；前端与图标规范见其 [frontend/README.md](../apps/MaaRacingMaster.Shell/frontend/README.md)。

**进程模型**：

- `MaaRacingMaster.Shell.exe`（C# WinUI 3）：唯一 GUI，只做窗口 + sidecar 进程生命周期 + 消息转发

- `sidecar.py`（Python）：JSONL RPC 业务后端（stdin=request / stdout=response / stderr=日志）

- 前端 HTML 通过 `window.chrome.webview.postMessage` → C# → Python 通信，封装为 `mra.call(method, params)`

**前端文件**（[frontend/](../apps/MaaRacingMaster.Shell/frontend)）：

| 文件                      | 职责                                                         |
| ----------------------- | ---------------------------------------------------------- |
| `index.html`            | 四 Tab 页面结构（主控/数据/设置/关于），所有 UI 元素 id 在此定义                     |
| `style.css`             | 纯 CSS 设计 token + 组件样式（无 CDN，WebView2 离线可用）                  |
| `app.js`                | 通信层 + Tab 切换 + 日志轮询 + 数据/设置页交互                              |
| `icons.js`              | 图标唯一真源（`MRAIcons`，Lucide v1.45.0 数据；规范见 `README.md`）         |
| `vendor.morphicons.js`  | morphicons v1.7.1 vendor 摊平版（图标变形动画，MIT 原文内嵌文件头）             |
| `README.md`             | 前端与图标规范权威文档（新增图标流程、morph 用法、file:// 约束）                     |

**窗口细节**：自定义标题栏 52px（进入 drag rect，右侧留 140px 给系统按钮）、最小尺寸 1000×700、系统按钮失焦配色、icon.ico。

**启动流程**：

1. 双击发布包根目录 `MaaRacingMaster.exe`（包内唯一入口：薄 Launcher 拉起 `app\` 下的 Shell，exe manifest 自动 UAC 提权）
2. shell 启动 Python `sidecar.py`，建立 JSONL 双向管道
3. WebView2 加载 `frontend/index.html`，前端 `mra.call` 初始化状态
4. 用户操作 → 前端 RPC → sidecar → 模块执行

***

### 4.5 [debug.py](../maaracing_master/core/debug.py) — 调试可视化

**职责**：

- 两套渲染模式：全量存盘（enabled）/ 精简 PEEP 预览（peep\_enabled）

- PEEP 产出为 JPEG 交 GUI 侧渲染（`update_peep` 写与 `get_peep_jpeg` 读共用一把锁防撕裂；主界面预览卡与独立 PEEP 悬浮窗互斥消费）——本模块不开窗口

- 渲染器可被模块安装替换（renderer token）：模块自带 `render_full` / `render_peep`，未安装时回落内置默认视图

- 导航标注：光标 / 候选（入围、拉黑、被过滤）/ 按钮目标 / 模板匹配框

- 检测框：调用方传 `detections` 时绘制（core 不认识类别语义，类别配色由模块渲染器自定）

- 同类别重叠框去重（避免虚线框堆叠）

- 文字带黑色阴影描边（保证任何背景可读性）

**核心类**：`NavigationDebugger`

**颜色约定（导航侧，BGR）**：

| 颜色  | BGR值           | 含义      |
| --- | ------------- | ------- |
| 红   | (0,0,220)     | 选中的光标   |
| 绿   | (0,200,0)     | 入围光标候选  |
| 紫红  | (255,0,255)   | 拉黑候选    |
| 黑   | (0,0,0)       | 被硬过滤的轮廓 |
| 天蓝  | (235,206,135) | 按钮目标    |
| 青   | (255,255,0)   | 模板匹配框   |

***

### 4.6 [logger.py](../maaracing_master/core/logger.py) — 日志系统

**职责**：

- 内存+文件双写日志；内存缓冲为**有界环形 deque**（`BUFFER_CAPACITY=5000`），长跑不随日志量增长

- GUI 增量读取用**单调序列号游标**（`get_lines_since(seq, ...)`），环形回绕后不重不漏、落后过多时返回 `truncated`

- 落盘**单流全量**：一次开启新建一个**会话目录** `logs/<MaaRM时间戳>/`，日志落其中的 `MaaRM_<ts>.log`，各级别按发生顺序写入；伴随产物（鉴宝决策流水 `trace.jsonl`）**共用同一个开关、同放该目录**（模块侧取 `logger.session_dir`，取到 `None` 即表示本次不落盘）。按大小轮转（`MAX_BYTES`/`BACKUP_COUNT`）+ 启动时按会话保留清理（`KEEP_SESSIONS`，会话目录整目录回收，旧版平铺的 `MaaRM_<8位日期>_<6位时间>` 文件同样归组回收）。**不按级别分档**——诊断是顺序的（「失败之前发生了什么」），按级别切分会切断时序且不可逆：切出的两份文件各自都读不通（DEBUG 档无业务锚点、INFO 档无诊断细节）。级别过滤只发生在读侧（GUI）与导出侧

- 5个日志级别：TRACE < DEBUG < INFO < WARNING < ERROR

- GUI 默认只显示 INFO 及以上

- **通道机制**：`log(channel=...)` 支持按子系统调级（`set_channel_level`/`clear_channel_level`）；`channel=None` 回落 `DEFAULT_CHANNEL`（app）。通道名是不透明字符串，core 不枚举/校验（红线 I7）

- 线程安全：写盘持单句柄 + `Lock`；`close()` 关句柄 flush 尾部

**全局单例**：`logger = Logger(logs_dir)`

#### 4.6.1 日志分级速查

| 级别      | 用途      | 典型示例                                                           |
| ------- | ------- | -------------------------------------------------------------- |
| TRACE   | 超细节开发追踪 | 中间变量、帧级内部状态、循环内计数器步进                                           |
| DEBUG   | 详细调试信息  | 模板匹配各尺度置信度结果、保存调试图路径、**框架节点生命周期**（`[Pipeline]` 识别成功/动作开始/动作成功）、第 N 次按 B、摇杆方向值（lx,ly）、死区判定细节 |
| INFO    | 关键业务里程碑 | 归位完成、返回主界面、开始循环、本轮完成、导航按钮点击成功、活动循环启动/结束、决策最终输出（如出价决策）。**框架内部节点跳转不算里程碑**，见 DEBUG 行 |
| WARNING | 警告但流程继续 | 截图快速方式失败降级MAA、归位超时、模板不存在、按钮未找到光标丢失、基准测试发现YOLO离群值（P95/P90>1.8×） |
| ERROR   | 错误需关注   | 模板加载失败、连接窗口失败、Pipeline异常、模型文件不存在、手柄创建失败、连续重试耗尽                 |

> **约定**：所有可继续运行的降级/兜底必须打 WARNING（不能静默）。不能恢复的故障打 ERROR 并配合 stop。

> **通道维度**：级别与通道是正交的两件事——级别管「读的时候要不要看」（GUI 默认 INFO 及以上，导出可另设阈值）；通道管「这一行要不要打」（调级）。两者都**不影响写侧**：落盘是单流全量。未设置通道级别时全记录（兼容旧行为）。

***

### 4.7 [window\_utils.py](../maaracing_master/core/window_utils.py) — 窗口与手柄检测

**职责**：

- `find_game_hwnd()`: 查找游戏窗口（优先UnrealWindow类名 → 标题关键词 → PID）

- `has_physical_controller()`: XInput API 检测物理手柄（xinput1\_4/1\_3/9\_1\_0）

- `hwnd_from_pid()`: EnumWindows 回调按PID找窗口句柄

**窗口查找关键词**："巅峰极速"、"g112"、"Racing Master"

**物理手柄检测**：遍历XInput端口0-3，`XInputGetState(i, buf) == 0` 表示已连接

***

### 4.8 [pipeline\_logger.py](../maaracing_master/core/pipeline_logger.py) — MAA Pipeline日志

**职责**：继承 `ContextEventSink`，监听Pipeline节点识别/动作事件，输出中文友好日志。

***

### 4.9 [opencv\_utf8\_patch.py](../maaracing_master/core/opencv_utf8_patch.py) — 中文路径补丁

**职责**：Monkey-patch `cv2.imread`/`cv2.imwrite`，支持中文Windows路径。ASCII路径走原生API，中文路径用 `np.frombuffer`/`cv2.imencode`+Python文件IO绕过。程序启动时import一次即全局生效。

***

### 4.10 [wgcap.py](../maaracing_master/core/wgcap.py) — WGC 持久化后台截图

**职责**：

- Windows Graphics Capture (WGC) 持久化后台截图，替代 MAA FramePool 的同步截图

- 零拷贝帧访问：WGC → D3D11 CopyResource → Map → memoryview → ndarray

- 独立捕获线程，回调驱动帧更新，业务线程无等待获取最新帧

- 客户区裁剪（排除标题栏/边框），支持窗口后台/遮挡/失焦时持续捕获

- 帧元数据追踪：frame\_id, capture\_ts\_ns, source\_timespan, frame\_age

**核心类**：`WgcCapture`

**关键指标**（参考 RTX 4060）：

- `get_latest()` P50 ≈ 3μs（仅引用交换，无拷贝）

- 颜色转换（BGR→RGB）P50 ≈ 0.33ms

- 完整 `_cap()` P50 ≈ 0.5ms

- callback interval P50 ≈ 14ms（\~70Hz 游戏帧率）

- 帧缓存重复率 \~48.5%（正常现象：consumer 比 producer 快）

**架构决策（v4 截图收敛：只留 WGC，无回退通道）**：

- **唯一截图通道 = WGC 中心缓存**（宪法 6「帧只从中心缓存来，引擎永不自截帧」）。`capture.screenshot()` 与 `capture.frame_with_age()` 都只读 `WgcCapture`；采集器未就绪/读帧异常一律返回 None，调用方按"采集链路故障"处理，**不再回退 MAA 同步截图**（回退会造成双时间线：消费者各持不同时刻的帧，且回退路径在调用线程内阻塞等截图）。

- `Win32Controller`（`screencap_method=FramePool`）**仅保留连接校验用途**（`connect()` 里的 `post_connection()`），不再作为任何取帧来源；`ctx.bind_tasker()` 给插件绑的是 `WgcapController`（帧注入控制器），插件侧不得持有同步截图通道。

- **唯一截图通道 = WGC 中心缓存，无后端分派、无回退通道**：GUI 不提供「截图方式」选择，`Win32Controller` 只承担连接校验，插件侧不得持有同步截图通道。

- 线程安全：锁内仅交换 Python 引用和整数，NumPy 操作在锁外

- 帧所有权：NativeMappedFrame → bytes → ndarray，回调结束后 ndarray 地址复用但仍安全

***

## 5. 关键类与函数索引

> 鉴宝域（treasure\_\*）类速查见 [鉴宝域文档 §7](../maaracing_master/plugins/treasure/CODE_WIKI.md)。

### 5.1 controller.MaaRacingMasterController

| 方法                                             | 说明                                                                     |
| ---------------------------------------------- | ---------------------------------------------------------------------- |
| `__init__()`                                   | 初始化能力门面、点击方式、急停等（截图通道唯一，无后端选择项）                                        |
| `connect()`                                    | 幂等窗口连接：仅创建 `Win32Controller(hWnd=...)`，连接超时 10s 保护 + 720p 窗口统一 + 屏幕内校验 |
| `start_module(module_id, start_from)`          | 分发到插件模块并启动（断点 `start_from` 由模块自己解析）                                    |
| `stop()`                                       | 停止模块、中断 Pipeline、销毁手柄                                                  |
| `set_click_mode(mode)` / `intent_mode`         | 点击方式切换：前台鼠标 / 后台手柄 / 仅意图                                               |
| `set_auto_shutdown(close_game, exit_mra)`      | 运行结束后自动关游戏 / 退出程序（仅自然完成时）                                              |
| `set_mute_game(enabled)`                       | 运行时静音游戏（音频音量控制）                                                        |
| `set_emergency_stop(enabled)`                  | 急停开关（后台轮询回路）                                                           |
| `set_auto_close_game` / `set_auto_exit`        | 自动收尾开关                                                                 |
| `gamepad_available()`                          | vgamepad / ViGEmBus 驱动可用性探测（缓存）                                        |
| `_get_gpad()`                                  | 懒创建并返回虚拟手柄（复用，不销毁重建）                                                   |
| `_reset_gpad()`                                | 摇杆归零+按钮释放（不销毁）                                                         |
| `_destroy_gpad()`                              | 销毁虚拟手柄：显式 ctypes `vigem_target_remove` 从总线拔除（确定性）                      |
| `_start_wgc_capture()` / `_stop_wgc_capture()` | 启动/停止 WGC 中心采集器（幂等）；失败即"截图链路不可用"，无 MAA 回退                              |

### 5.2 yolo\_detector.YOLODetector

| 方法                                                 | 说明                                                       |
| -------------------------------------------------- | -------------------------------------------------------- |
| `__init__(model_path, conf, iou)`                  | 初始化ONNX会话（DirectML/CUDA/CPU降级）                           |
| `__call__(img_rgb, roi)`                           | 推理入口：返回(coins, cars, bonus, debug\_dets, all\_raw\_dets) |
| `_nms_per_class(xyxy, scores, classes, mask, ...)` | 按类别分别做NMS，返回原始下标                                         |
| `_to_dets(xyxy, scores, classes, ... indices)`     | 索引转结构化检测结果dict                                           |
| `CLASS_CONF`                                       | 类属性：各类别置信度阈值字典                                           |

### 5.3 mra\_shell（WinUI 3 shell + sidecar）

| 类/文件                  | 职责                                                                       |
| --------------------- | ------------------------------------------------------------------------ |
| `Program.cs`          | 入口（`DISABLE_XAML_GENERATED_MAIN`）+ 单实例互斥                                  |
| `App.xaml.cs`         | 应用生命周期、UAC 提权环境变量注入                                                      |
| `MainWindow.xaml.cs`  | 窗口生命周期、WebView2 加载前端、`AppWindowTitleBar` drag rects                       |
| `PeepWindow.xaml.cs`  | PEEP 悬浮窗（工具窗 + 置顶 + 无系统边框，标题栏 HTML 自绘）                                    |
| `RpcBridge.cs`        | HTML ↔ C# ↔ sidecar 的 RPC 转发（主窗口与 PEEP 窗共用；回发按 `sender.CoreWebView2` 定位，互不串台） |
| `PythonSidecar.cs`    | JSONL transport：stdin 串行写 + 唯一 stdout reader + pending 匹配 + 超时/Kill 树    |
| `sidecar.py`          | Python 侧 RPC handler（get_initial_state / start / stop / set_peep ...）   |
| `frontend/app.js`     | 前端逻辑：`mra.call()` 通信 + 四 Tab 切换 + 日志/状态轮询                                |
| `frontend/index.html` | 页面结构，所有 UI 元素 id（改 UI 先改这里）                                              |
| `frontend/style.css`  | 设计 token + 组件样式                                                          |
| `frontend/icons.js`   | 图标唯一真源（Lucide 数据 / `MRAIcons`），规范见 `frontend/README.md`                    |
| `frontend/peep.*`     | PEEP 悬浮窗页面与帧消费（`peep.html` / `peep.js` / `peep.css` / `peep-consumer.js`） |

### 5.4 debug.NavigationDebugger

| 方法                                 | 说明                     |
| ---------------------------------- | ---------------------- |
| `__init__(proj_dir)`               | 初始化                    |
| `enable_peep()` / `disable_peep()` | 开关 PEEP 预览（置 `peep_enabled`；窗口由 GUI 侧承担）   |
| `start_session(label)`             | 开始一次调试会话（创建存盘子目录）      |
| `save_frame(img, **kwargs)`        | 统一入口：存盘全量绘制 + PEEP精简绘制 |
| `_render_full(img, **kw)`          | 全量标注绘制（存盘用）            |
| `_render_peep(img, **kw)`          | 精简绘制（PEEP用）            |

### 5.5 基础工具方法速查（core 共享）

跨模块高频工具函数，本表统一索引：

| 方法                              | 所属模块                             | 说明                                                     | 关键参数/坑点                                                 |
| ------------------------------- | -------------------------------- | ------------------------------------------------------ | ------------------------------------------------------- |
| `ctx.capture.screenshot()`      | capabilities.py `CaptureAdapter` | 截图 RGB ndarray（**只读 WGC 中心缓存**，无帧返回 None）              | 唯一取帧入口；不得回退同步截图，返回 None 按"采集链路故障"处理，不得当作"画面无变化"         |
| `ctx.lifecycle.sleep(seconds)`  | capabilities.py `LifecycleAdapter` | 可中断睡眠：≤0.1s 分片检查停止信号，**墙钟时长 ≥ 入参**（deadline 分片+余数补齐）    | 曾按 `int(s/0.1)` 量化迭代，小于 0.1s 的入参静默退化为零睡眠忙旋（真机 2026-09-14 饿死导航 worker，见 treasure 域 CODE_WIKI §9）；语义由 `tests/test_capabilities_lifecycle_sleep.py` 机检 |
| `NavigationDebugger(proj_dir)`  | debug.py                         | PEEP 实时预览 / debug 截图标注，支持 template\_rects + detections | §5.4；§9.3 调试模式说明                                        |
| `has_physical_controller()`     | window\_utils.py                 | XInput API 遍历 4 端口，任一连接返回 True                         | DLL 回退 xinput1\_4 → xinput9\_1\_0 → xinput1\_3；§10.4 坑点 |

***

## 6. 模块依赖关系

### 6.1 导入关系图

```
core/sidecar.py（JSONL RPC handler）
  ├── core.controller.MaaRacingMasterController
  ├── core.logger.logger
  ├── core.window_utils.has_physical_controller
  └── core.registry（插件自动扫描注册）

MaaRacingMaster.Shell（C#，不导入 Python）
  └── PythonSidecar（stdin/stdout JSONL 通信）
        └── 子进程：python -m maaracing_master（core/sidecar.py）

core/controller.py
  ├── core.sidecar（handler） / core.registry
  ├── core.base.ActivityContext / core.base.ActivityModule
  ├── core.clicker（点击方式）/ core.gamepad_cursor / core.audio_volume
  ├── core.window_utils.find_game_hwnd / resize_game_window_720p / is_window_on_screen
  ├── core.logger.logger
  └── plugins.<id>.module（ActivityModule 启动分发）

core/registry.py（插件真源入口：manifest = ID + MODULE_CLASS + 可选有效期）
  ├── core.base.ActivityContext / core.base.ActivityModule（注册类型与实例化）
  ├── core.module_validity（有效期解析与判定，纯函数）
  └── core.logger.logger

core/yolo_detector.py
  └── core.logger.logger

core/debug.py / core/debug_io.py
  └── （纯 OpenCV/numpy，IO worker 生产-消费者）

core/window_utils.py
  ├── maa.toolkit.Toolkit
  └── core.logger.logger
```

### 6.2 运行时对象持有关系

```
core/sidecar.py（MaaRacingMaster.Shell 托管）
  └── controller: MaaRacingMasterController
        ├── debug: NavigationDebugger
        ├── controller: Win32Controller（仅连接校验用途，不作取帧来源）
        ├── ctx: ActivityContext（能力门面；持 WGC 采集器、手柄租约等托管资源）
        └── 活动模块实例（start_module 经 registry.create_module 按需创建）
              └── tasker: Tasker（bind 了 WgcapController）
                    ├── context_sink: PipelineLogger
                    └── resource: Resource
```

> MAA 对象（`Tasker` / `Resource`）由**活动模块实例**创建并持有，主控不再持有（见 §2.2、§4.1）；模块经 `ActivityContext` 窄接口访问 capture / gamepad / debug\_renderer 等能力。

***

## 7. 运行流程详解

### 7.1 启动流程

```
双击发布包根目录 MaaRacingMaster.exe（薄 Launcher → app\ 下 Shell；exe manifest 自动 UAC 提权）
  → WinUI 3 shell 创建窗口（AppWindowTitleBar）并展示 HTML 前端
  → shell 拉起 Python sidecar（python -m maaracing_master）
    → sidecar 初始化 Controller，等待 stdin JSONL RPC
  → 前端通过 mra.call(method, params) 与 sidecar 通信
```

独立调试 sidecar（不经 GUI）：`python -u -m maaracing_master.core.sidecar`（等待 stdin JSONL RPC）。

### 7.2 用户点击"开始"后流程

```
前端按钮 #btn-start（app.js）
  → mra.call('start', {module_id, start_from}) → C# → sidecar 的 start handler
  → controller.start_module(module_id, start_from)
    → 模块 ActivityModule.start(start_from)：模块内部解析断点并进入活动循环
```

活动循环细节（巅峰鉴宝）见插件 CODE\_WIKI，主控层不再编排阶段。

### 7.3 controller.start\_module() 主机编排

```
start_module(module_id, start_from)
  ├─ connect()：幂等窗口连接（Win32Controller(hWnd=...)；超时10s保护；
  │   Window 720p 统一（resize_game_window_720p）；屏幕内校验失败→ERROR终止）
  ├─ 每次启动执行静音/自动收尾等全局设置
  ├─ 按 module_id 从 registry 选插件模块
  └─ module.start(start_from)（worker 线程运行，阻塞直到模块 stop/完成）
     └─ 模块自己：装渲染器 → 初始化检测/OCR → 主循环；exit 时 ctx 托管资源自动释放
```

***

## 8. 关键配置与常量

### 8.1 全局约定

| 常量    | 值        | 说明                             |
| ----- | -------- | ------------------------------ |
| 游戏分辨率 | 1280×720 | 所有坐标基于此（全局约定，各插件 ROI/按钮均按此归一化） |

core 侧全局常量仅此一项。各插件的 ROI / 阈值 / 仲裁等数值**不在代码内硬编码**，一律读插件自带的 `resources/policy/<id>.policy.json`（唯一数据面真源）；资产归属判据见 §9.8。

### 8.2 版本管理

- 版本号由 `setuptools-scm` 从 Git Tag 自动生成

- 格式：`vX.Y.Z`（SemVer 2.0.0）；0.x 阶段全部为 pre-release（`v0.x.y-dev.N`）

- 入口：`git tag vX.Y.Z && git push origin vX.Y.Z` 触发CI Release

**双轨版本机制（v0.13.0-dev.5 起，`maaracing_master/__init__.py`）**：

- 打包/安装产物（无 `.git`）：读 `_version.py` 构建快照（setuptools-scm 构建时写入）→ 版本固化，**旧版本不会被仓库后续新 tag 带歪**

- 源码直接运行（目录下有 `.git`）：忽略可能过期的 `_version.py`，启动时 `git describe --tags --long` 按**当前 checkout** 动态推导（checkout 到旧 tag 就显示旧版本号）

- 兜底：`"0.0.0.dev"`

- **sidecar 必须读包级** **`__version__`**（`from maaracing_master import __version__`），**不能读** **`_version.__version__`**——后者是 `_version.py` 文件里的构建时硬编码，与 `__init__.py` 动态推导的包级 `__version__` 不是同一个对象（v0.13.0-dev.5 真实踩过：sidecar 一直返回过期版本号）

***

## 9. 开发与调试

### 9.1 安装开发环境

```bash
git clone https://github.com/d542Bb/MaaRacingMaster.git
cd MaaRacingMaster
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 9.2 运行

```bash
python -u -m maaracing_master.core.sidecar  # 独立调试 sidecar（等待 stdin JSONL RPC）
```

### 9.3 调试模式

1. **DEBUG 存盘模式**：GUI 勾选"DEBUG 每帧截图"，每帧全量标注保存到 `%APPDATA%/MaaRacingMaster/debug/<module>/<会话>/` 目录
2. **PEEP 实时预览**：GUI 勾选"PEEP 实时预览"，弹出 OpenCV 窗口实时显示精简标注画面（\~30fps）
3. **断点调试**：GUI 断点列表双击选择起始阶段，跳过前面的导航步骤
4. **MPE Studio**：`tools/navkit/mpe.cmd` 起 LocalBridge 并在浏览器打开 MPE（画布编辑 v4 真源）+ 策略表薄页

### 9.4 YOLO模型训练

`tools/training/train.py` 提供 YOLO 训练→ONNX 导出链路（Ultralytics yolo11n 微调），`dataset.yaml` 配置数据集类别，`auto_label.py` 为自动标注工具。**模型权重不随发行包分发**：含检测的插件启用时自带并声明 `REQUIRED_ASSETS`。

### 9.5 日志位置（%APPDATA%/MaaRacingMaster/）

用户数据目录 `user_data_dir()` 五目录结构（[paths.py](../maaracing_master/core/paths.py)）：

- `config/`：`profile.json`（用户偏好）、`maa_option.json`

- `data/`：结构化业务数据（`data/treasure/treasure.db`）

- `logs/`：会话记录 `logs/<MaaRM时间戳>/`——运行日志 `MaaRM_YYYYMMDD_HHMMSS.log`（含 DEBUG 级全量）与伴随产物 `trace.jsonl`（鉴宝决策流水，共用「日志记录」开关）

- `framework/`：MAA 框架自产物（`maafw.log`、cache）

- `debug/`：调试截图会话 `debug/<module>/<会话>/`（DebugStudio 契约）

GUI 只显示 INFO 及以上；记录数据（历史 CSV）同随用户数据目录管理。

### 9.6 物理手柄检测

- 前台点击（real）/ 后台手柄（background）模式检测到物理手柄可能冲突；intent 模式仅算意图不干扰

- 检测方法：XInputGetState遍历端口0-3（`XInputGetState(i, buf) == 0` 表示已连接）


### 9.7 NavKit 模板图采集工作流（2026-09-07 定案，v4 载体沿用）

> 适用：为图节点 / policy `perception.spec` 锚点采集模板图。分工：**人工截图+裁剪标注 → 导出
> regions.json + PNG → 工具侧换算入库**（rect 归一化/JSON/校验/提交均为工具侧职责）。

**来源纪律**：只从 1280×720 运行帧截图裁剪（`window_utils` 启动即统一 720p），
1:1 像素裁剪、**永不缩放**（模板与运行帧同尺度，匹配单尺度 1.0）。

**人工截图通道的坐标系换算（2026-09-16 实测）**：截图工具产出的是**带窗口边框的窗口
截图**，实测 1281×759——比客户区多出左侧 1 px 边框与顶部 38 px 标题栏。此类 PNG 必须先
裁出客户区（`img[38:758, 1:1281]`，恰 720×1280）再定位、算 rect，否则 ROI 整体偏移
38 px、运行期全部失配。校验办法：取一张来自真正运行帧的既有模板（如鉴宝域的
`hall_peak_appraise_card.png`）在该客户区上匹配，分数应达 ~1.0000。

**裁剪纪律**：只装"一年后还长这样的像素"——角标/数字/倒计时/红点/限时横幅一律
留在模板外。变化在模板图**外**（哪怕紧贴）不影响匹配得分；在图**内**才失效
（阈值 0.75 容忍渲染抖动，不容忍结构性变化）。

**半透明元素不可作锚点（2026-09-16 实测）**：压在动态画布上的 HUD 文字与进度条，
其像素是逐帧与背景混合的结果——实测驾驶页"阶段"二字在两种天光下跨帧匹配分
0.94 → 0.71，跌破阈值即失配。此类位置改取**不透明图标**替代（同区域齿轮图标替代后
跨帧 0.93 稳定），或退为多锚点 Or 组合兜底。

**命名**：`<页面>_<元素>[_限定词].png`，全小写下划线；首段页面名与页注册表一致
（`hall_` / `rank_` / `speedrush_`…），看文件名即知归属。

**位置跟 owner 走**：global 锚点 → `core/resources/image/`；模块锚点 →
`plugins/<id>/resources/image/`。归属与物理位置矛盾由编辑评审盯。

**回传**：标注工具导出 regions.json（像素区域 x/y/w/h）+ PNG → 换算
归一化 rect（外扩 15% 宽 / 25% 高、4 位小数）写入真源。**JSON 真源由 MPE 画布 /
Studio 工具链**（`tools/navkit/studio.cmd`：ROI 校准台 / 策略表 / 模板截取）**维护，人工不手改裸文件**；agent/脚本直改 JSON 后
`tools/navkit/check_truth.py` 机检 + 提交。

**守卫闭环（现状）**：CI = `check_truth.py`（next/on\_error 引用闭合、入口可达、
疑似重复识别告警、policy 数据面可装配、图↔spec 交叉互洽）+ `test_navkit_truth.py`
节点/锚点计数见证。模板文件缺失由运行时 `load_template` 返回 None 降级 +
装载器 WARNING 暴露。**体积账**：单张 5\~60KB、全部模板预期 <1MB（对比 rapidocr
模型 30MB、onnx 12MB 不成量级）——要防的是"图死"（孤儿图定期对照 spec/节点引用
清一次）不是"图多"。

**MAA 对照**：MaaFramework 侧仅约定"720p 无损原图裁剪勿缩放 + `roi`/`box`/`target`
三概念分离"（本指南 §5.1/§5.2）；社区靠 ImageCropper 类工具 + 人工纪律，无结构化
工作流。MaaRM 在其上加 regions 机器可读导出 + 校验守卫闭环。

### 9.8 全局资产分层与同图命名空间（v4 形态）

> **分界线**：这个元素**是否跨玩法长期共享**（爆炸半径）——是则归 `core/`，否则归
> `plugins/<id>/`。与"某个玩法是否从这个按钮进入"无关：鉴宝入口卡片是鉴宝专属知识，
> 即使它物理上长在大厅里。
>
> **现状**：项目尚无第二个模块接图，**core 侧没有任何 pipeline 真源**。

**两段真源（v4 文件形态）**：

- **模块段** = `plugins/<id>/resources/pipeline/<id>*.json`（对局图 + 本模块的入口链与
  页面锚点，按业务域分文件：鉴宝 = `treasure.json` + `treasure.entry.json`）
  \+ `plugins/<id>/resources/policy/<id>.policy.json`（感知/决策数据面）。

- **共用段** = `core/resources/pipeline/*.json`，**当前为空目录不存在**；出现真跨模块
  链（如"任何模块开工前先回游戏大厅"）时再建。它能承载的只有**无出口的公共锚点**
  （一份识别规格）与**公共动作节点**（识别 + 动作、不带出口），由各模块在自己的
  `next`/`any_of` 里引用；它**不指向任何模块节点**，"回哪个模块"这条边永远由模块自己声明。

**归属由命名空间前缀表达**：core 真源不得占用、也不得引用 `<module>.` 节点，由
`check_truth.namespace_checks` 机检；模块命名空间由 `plugins/*/module.py` 自动发现。
协议为何没有命名空间、"引用"的完整口径（`next`/`on_error` + And/Or 按名子项 +
`anchor` 对象 value）与"什么该出 pipeline"的六条判据，权威版见
[MAAFW\_GUIDE §5.6](MAAFW_GUIDE.md#56-真源组织与分层协议没有命名空间分离只能靠引用方向)。

**通电位置（关键契约，不变项）**：

- **图侧原生并入**——多真源文件载入同一张图，模块图可原生引用共用段节点而不必复制
  一份大厅识别（当前共用段为空，模块图自包含）。

- **检测侧绝不并入**——运行时阶段检测只扫 policy.json `perception.spec` 装配的
  `DetectionPlan`（模块自有锚点集），图节点不进每帧检测环。原因：共用段锚点无 `order`
  → `stage_priority=1000`，一旦进扫描集会在局内帧抢先短路、破坏逐帧等价回归。贴 MAA：
  首页/入口识别属**导航段**，不进**每帧检测环**。

- **全页面清单的唯一真源 = `<id>.__boot.dwell`**（`recognition.type=Or` 全 stage 信号
  并集 → `next` 全 dwell 表，`timeout=-1` 未知画面驻留重判）。入口锚点点击后一律
  `next: [<id>.__boot.dwell]` 交汇聚重判，不得各自再抄一份清单。

- **版本化用时间表**，不给每个资源挂版本号（`activity_window` / `schedule.json`），
  与 MAA `activity_pool` 同构。

**回归护栏**：CI = `check_truth.py`（图闭合（含 And/Or 按名子项——框架只校验
`next`/`on_error`，子项拼错要到运行期才 `Bad sub ref` 静默失败）+ 数据面装配 + 图↔spec
交叉互洽 + 方向红线 + **两面同图**：templates 相同的图侧参数与 spec 锚点逐字段比对
`rect`/`threshold`/`arbitration`/`mode↔kind`/`colorspace`，任一面不等即拦）
+ `test_navkit_truth.py`（锁归位形态：真源全在模块命名空间、core 侧零节点、plugin
文件集；锁红线活性：合成违规图必须报；spec/节点计数见证防漂）。颜色口径定案：
**默认 gray，灰度拉不开差距才转 rgb**（2026-09-11 两面统一；依据 = 检测面 detector 早已按
`spec.arbitration.margin` 做领先判定，P4C 对拍 812 帧两引擎命中数相等、零翻转，gray 快 2.8 倍）。

**编辑真源**：`mpe.cmd` 打开的 MPE 文件面板列出 plugin 的两个 pipeline 文件，直接编辑
保存；一个视口 = 一个文件，跨文件被引用节点显示为"外部节点"虚影（MPE 只补**被本文件
引用到的**别处节点，不摊开全城——所以图越干净，编辑器越安静）。

***

## 10. 已知坑点与注意事项

> 鉴宝坑点见 [鉴宝文档 §9](../maaracing_master/plugins/treasure/CODE_WIKI.md)。

### 10.1 系统层

| 坑点                        | 说明                                            | 解决方案                                           |
| ------------------------- | --------------------------------------------- | ---------------------------------------------- |
| 截图需要管理员权限                 | PrintWindow/BitBlt需要提升权限                      | mra\_shell.exe manifest 自动 UAC 提权（一次，child 继承） |
| cv2不支持中文路径                | imread/imwrite在中文路径下失败                        | opencv\_utf8\_patch.py monkey-patch            |
| YOLO ONNX 导出              | `onnx.export(simplify=True)` 可能产生损坏模型（推理结果错乱） | 导出时关闭 simplify，或导出后校验精度                        |

### 10.2 MAA Framework API

API 签名、参数名与高频误用清单的权威版见 [MAAFW\_GUIDE §9「高频红线/坑」](MAAFW_GUIDE.md#9-高频红线坑背下来)（完整签名见其 §3.3 与 §6）。

### 10.3 光标导航

| 坑点                     | 说明                                                  |
| ---------------------- | --------------------------------------------------- |
| 光标面积评分中心               | 真光标面积310（常态）/420（选中态），不是1200                        |
| 双中心面积评分公式              | `max(1-abs(area-310)/300, 1-abs(area-420)/300)`     |
| 面积硬过滤                  | area<240必须排除，假光标\~206-221                           |
| 游戏摇杆死区                 | \~13%（约4260/32767），非零轴必须抬升到死区以上                     |
| 销毁手柄复位光标               | `del gpad` 游戏自动把光标复位到左上角，比摇杆归中可靠                    |
| 不要加微轴归零阈值              | abs(dx)\<N→lx=0会阻止目标附近±Npx死区的最终修正                   |
| 假光标静止拉黑                | 用\_prev\_frame\_positions集合跨帧对比，不依赖last\_known\_pos |
| \_press\_and\_verify失败 | 不要清空\_last\_stick，保留下一帧运动评分依赖                       |
| 收缩保底公式                 | `max(5, int(close_th×0.65))` 不是max(30,-15)          |
| stop\_distance自适应      | `max(8, close_th×0.55)` 不是硬编码25px                   |
| 微调脉冲                   | <35px用25ms+80ms刹车，40ms仍过冲                           |
| 模板匹配正反逻辑               | True=匹配到算成功；False=模板消失算成功                           |
| 避让候选必须超出「到位容差」        | auto\_shoo 的候选若离当前位置 ≤SHOO\_TOL\_PX(60px) 必须跳过：导航在容差内判「已到位」零移动——被屏幕边界 clamp 的避让点最易刚好贴出遮挡圈（2026-09-14 R5 实机：避让点距光标 50px，32s 空提 95 次光标纹丝不动，label OCR 永远被挡 → S1 死等）。同意图连提 8 次未移开升级 WARNING；契约锁 tests/test\_clicker\_auto\_shoo.py |
| 避让闸口不得自锁                | 任何以运行期计数为条件的避让闸，其计数清零路径若依赖避让自己提交的动作，永久 skip 即成死局：miss\_streak 只在导航任务 read\_pos 成功时清零，而等待态（S1，OCR 读不到 → 无点击意图）避让是唯一导航提交者（2026-09-14 实机第二型停摆：避让任务 lost 收尾 miss\_streak≥3 → 40s 零避让，读侧拿陈旧 last\_pos 永远判「压住」）。现按 SHOO\_PROBE\_INTERVAL\_S(1s) 节流放行探测，光标重现即恢复；契约锁同上测试文件 |

### 10.4 物理手柄XInput

- `XInputGetState(i, buf) == 0` 表示第i号物理手柄已连接

- 尝试加载顺序：xinput1\_4.dll → xinput1\_3.dll → xinput9\_1\_0.dll

- 运行前必须断开所有物理手柄，否则虚拟手柄被游戏忽略或冲突

***

## 11. GUI 宿主定案（WinUI 3）

正式 GUI = `apps/MaaRacingMaster.Shell/`（WinUI 3 shell + WebView2 HTML 前端）+ `sidecar.py`（Python 业务后端，JSONL RPC）。

**硬约束（不得回退到以下两条路）**：要同时拿到 HTML 前端、原生窗口行为（DWM 动画 / 系统 caption buttons / Snap）与可用的拖动区，只有 `AppWindowTitleBar` 的**系统级 NC 处理**能做到——WebView2 是独立 HWND 铺满客户区，任何在客户区内模拟标题栏的方案（如 WPF `WindowChrome`）都会被它遮挡并吞掉鼠标；`FormBorderStyle.None` 类方案无 `WS_CAPTION`，拿不到 DWM 动画。

壳工程结构、锁定版本、WinUI 3 API 与构建坑、sidecar transport 契约见 [apps/MaaRacingMaster.Shell/README.md](../apps/MaaRacingMaster.Shell/README.md)。

***

## 附录：类速查表

> 主程类速查见下表；鉴宝类（TreasureModule / TreasureStageDetector / TreasureOcr / TreasureDebugRenderer）见 [鉴宝文档 §7](../maaracing_master/plugins/treasure/CODE_WIKI.md)。

| 类名                                   | 文件                                                                                        | 核心职责                                 |
| ------------------------------------ | ----------------------------------------------------------------------------------------- | ------------------------------------ |
| `MaaRacingMasterController`          | core/controller.py                                                                        | 主控编排：能力门面 + 模块生命周期 + 全局设置            |
| `ActivityModule` / `ActivityContext` | core/base.py                                                                              | 模块基类 / 能力门面（窄接口 + ExitStack 生命周期）    |
| `Registry`                           | core/registry.py                                                                          | 插件自动扫描注册（扫 `plugins/*/manifest.py`；含有效期门与自动选中过滤）  |
| `TreasureModule`                     | plugins/treasure/module.py → [鉴宝文档 §1](../maaracing_master/plugins/treasure/CODE_WIKI.md) | 巅峰鉴宝活动模块（阶段数真源见其 policy.json stages.order）                    |
| `Sidecar`                            | core/sidecar.py                                                                           | JSONL RPC 业务后端（mra\_shell 托管）        |
| `NavigationDebugger`                 | core/debug.py                                                                             | PEEP预览、截图标注（存盘走 debug\_io IO worker） |
| `Logger`                             | core/logger.py                                                                            | 内存+文件双写日志；会话目录 `logs/<ts>/`（日志 + 伴随产物同放）    |
