# MaaRacingAssistant — Code Wiki（主文档）

> 《巅峰极速》模块化游戏自动化平台 —— 完整代码架构文档
>
> **文档导航（Code Wiki 已按功能域拆分，共 2 份）**：
>
> - **本文件（主文档）**：架构总览 / 目录结构 / 主程核心模块 / 依赖 / 运行流程 / 配置常量 / 开发调试 / 主程坑点 / GUI 选型
>
> - [鉴宝域 CODE\_WIKI（plugins/treasure）](../maaracing_assistant/plugins/treasure/CODE_WIKI.md)（treasure\_\* 全模块 / 出价策略 / 鉴宝模板 / 鉴宝坑点）

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
11. [GUI 宿主选型](#11-gui-宿主选型winui-3-定案)
    附录：类速查表

***

## 1. 项目概述

### 1.1 项目定位

MaaRacingAssistant 是一款基于**计算机视觉**与**虚拟手柄控制**的模块化游戏自动化平台，以统一模块框架承载《巅峰极速》各类重复性活动的自动化。当前入库可用的活动插件为**巅峰鉴宝**（treasure）；**极速狂飙**将基于新的模块化插件架构（`core/navkit` + 插件自包含契约）重写，当前未入库。

### 1.2 核心技术栈

| 层级     | 技术组件                                     | 用途                   |
| ------ | ---------------------------------------- | -------------------- |
| 流程编排   | MAA Framework 5.12.x                     | UI 流程编排 + 窗口控制 + 截图  |
| 视觉识别   | YOLO11 + ONNX Runtime (DirectML)         | 实时目标检测（金币/障碍车/跳板车）   |
| 手柄模拟   | vgamepad 0.1.x                           | Xbox 360 虚拟手柄，摇杆精确控制 |
| 图像处理   | OpenCV 5.x                               | 模板匹配、Hough 直线检测、可视化  |
| OCR    | RapidOCR 3.9.x                           | 鉴宝金额 / 出价按钮文字识别      |
| GUI 框架 | WinUI 3 (Windows App SDK 1.8) + WebView2 | 原生窗口 + HTML 三 Tab 前端 |
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
│               GUI 层 (apps/mra_shell/)                          │
│        WinUI 3 窗口 + HTML 前端 + sidecar 进程托管                │
├─────────────────────────────────────────────────────────────────┤
│                      主控层 (controller.py)                     │
│      能力门面 ActivityContext + 模块生命周期 + 全局设置           │
│      （MAA 对象 Tasker/Resource 归插件模块创建，主控不再持有）     │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐  ┌──────────────────────────────────┐  │
│  │  导航引擎 (core/)    │  │  treasure 插件                   │  │
│  │  nav_graph/clicker   │  │  (plugins/treasure/)            │  │
│  │  - NavKit v3资产     │  │  - 12阶段状态机                │  │
│  │  - 光标导航/虚拟手柄   │  │  - RapidOCR 金额识别            │  │
│  │  - 多尺度模板匹配     │  │  - 智能出价策略                 │  │
│  │  - 意图/真实点击     │  │  - 结算/彩蛋/分红               │  │
│  └─────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐  ┌──────────────────────────────────┐  │
│  │  YOLO 检测器        │  │  调试可视化                      │  │
│  │  (yolo_detector.py) │  │  (debug.py)                      │  │
│  │  - ONNX Runtime     │  │  - PEEP 实时预览窗口             │  │
│  │  - DirectML GPU     │  │  - 每帧截图标注存盘              │  │
│  │  - per-class NMS    │  │  - 导航/活动双模式渲染           │  │
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

- **巅峰鉴宝**（treasure 插件）：12 阶段状态机（游戏大厅 → 活动页 → 鉴宝大厅 → 场次 → 鉴宝师 → 出价 → 结算 → 分红），详见 [鉴宝文档 §1](../maaracing_assistant/plugins/treasure/CODE_WIKI.md)。

主控不再持有活动流程编排：`MaaRacingAssistantController` 仅负责窗口连接、能力门面（`ActivityContext`）、模块生命周期与全局设置，活动阶段流转全部在模块内部。

***

## 3. 目录结构详解

```
├── pyproject.toml                            # setuptools-scm 包配置
├── requirements.txt                          # Python 依赖清单
├── AGENTS.md                                 # AI 助手项目配置
├── README.md                                 # 用户说明文档
│
├── maaracing_assistant/                      # 📦 核心应用包
│   ├── __init__.py                           # 版本号导出（setuptools-scm 自动生成）
│   ├── __main__.py                           # python -m 入口
│   ├── core/                                 # 主程序（应用层）
│   │   ├── controller.py                     # 主控编排（生命周期 + 能力门面 ActivityContext，已不直接持有 MAA 对象）
│   │   ├── sidecar.py                        # JSONL RPC 业务后端（供 mra_shell.exe 托管）
│   │   ├── registry.py                       # 插件自动扫描注册表（扫 plugins/*/manifest.py）
│   │   ├── base.py                           # ActivityContext / ActivityModule 基类
│   │   ├── capabilities.py                   # typed capability 窄接口 + adapter
│   │   ├── clicker.py / gamepad_cursor.py / audio_volume.py    # 点击方式 / 手柄光标 / 静音
│   │   ├── render_plan.py / stage_tracker.py / roi_config.py   # 渲染计划 / 阶段记录 / ROI 底座
│   │   ├── debug.py / debug_io.py            # PEEP 预览 + 调试落盘 IO worker
│   │   ├── module_config.py / registry.py    # 模块配置契约 / 插件扫描注册
│   │   ├── paths.py                          # 用户数据目录（%APPDATA%/MaaRacingAssistant，五目录）
│   │   ├── opencv_utf8_patch.py / vgamepad_lazy.py / wgcap.py
│   │   └── yolo_detector.py                  # 跨活动视觉基础设施
│   └── plugins/                              # 活动插件（一活动 = 一自包含目录，放入即装/删除即卸）
│       └── treasure/                         # 巅峰鉴宝
│           ├── CODE_WIKI.md                  # 鉴宝域文档
│           ├── manifest.py                   # ID + MODULE_CLASS（registry 扫描用）
│           ├── __init__.py                   # PLUGIN_DIR / RES_DIR / IMAGE_DIR / CONFIG_DIR 资源常量
│           ├── module.py / detector.py / ocr.py / strategy.py
│           ├── eggs.py / renderer.py / store.py
│           └── resources/                    # 插件专属资源（自包含）
│               ├── image/                    # 全部鉴宝模板
│               ├── config/treasure_assets.json # NavKit schema v3 唯一检测/路由真源
│               └── config/treasure_rois.json # v2 回退/迁移输入（调试台兼容）
│
├── assets/                                   # 应用级资产（插件素材已全部内聚到各自 plugins/<id>/resources/）
│   ├── config/maa_option.json                # MAA 框架配置
│   ├── demo/                                 # 界面截图（shot_*.png）
│   ├── icon.ico                              # 应用图标
│   └── mra_icon.png                          # README 展示图标
│
├── apps/
│   │   └── mra_shell/                        # 🖥️ 正式 GUI（WinUI 3 shell + WebView2）
│   │       ├── MainWindow.xaml(.cs)          # 窗口 + sidecar 生命周期 + 消息转发
│   │       ├── PythonSidecar.cs              # JSONL transport 契约实现
│   │       ├── App.xaml(.cs)                 # 应用入口（DISABLE_XAML_GENERATED_MAIN）
│   │       └── frontend/                     # HTML 前端（三 Tab：控制/调试/关于）
│   │           ├── index.html                # 页面结构 + 元素 id
│   │           ├── style.css                 # 纯 CSS 样式（无 CDN）
│   │           └── app.js                    # mra.call RPC + 页面交互逻辑
│   └── mra_launcher/                         # C 启动器（提权 + 定位 shell）
│
├── tools/                                    # 开发工具脚本（按用途分组）
│   ├── mouse_overlay.py                      # 独立 Overlay 工具（屏幕十字准星）
│   ├── navkit/                               # NavKit 控制台（结构树/资产编辑/回放）
│   ├── training/                             # 模型训练与数据准备
│   │   ├── train.py                          # YOLO 训练 + ONNX 导出脚本
│   │   ├── dataset.yaml                      # 数据集类别配置
│   │   └── auto_label.py                     # 自动标注工具
│
├── tests/                                    # 单元测试（纯逻辑，CI 矩阵 3.11）
├── scripts/                                  # 发布打包 / 启动脚本
│   ├── release/assemble.ps1                  # 发布打包脚本
│   └── start_navkit.ps1                      # NavKit 启动
│
├── .github/workflows/                        # CI：test.yml（单测）、release.yml（发布）
├── docs/
│   ├── update_log.md                         # 版本更新日志
│   ├── MAAFW_GUIDE.md / SELF_CHECK.md / announcement.md
│   └── CODE_WIKI.md                          # 本文档（主文档）；鉴宝域文档随插件（plugins/&lt;id&gt;/CODE_WIKI.md）
│
# 运行期数据（自动生成，gitignore）：已迁至 %APPDATA%/MaaRacingAssistant/
# ├── config/                                 # profile.json、maa_option.json
# ├── data/                                   # data/treasure/treasure.db
# ├── logs/                                   # MRA_*.log
# ├── framework/                              # MAA 框架自产物（maafw.log、cache）
# └── debug/                                  # debug/<module>/<会话>/（调试台契约）
```

***

## 4. 核心模块说明

> 本文档覆盖**主程核心模块**。按功能域拆分：
>
> - **鉴宝域**（treasure\_module / treasure\_detector / treasure\_ocr / treasure\_renderer / bid\_strategy）→ [鉴宝域文档](../maaracing_assistant/plugins/treasure/CODE_WIKI.md)

### 4.1 [controller.py](file:///d:/maaracing_assistant/maaracing_assistant/core/controller.py) — 主控编排器

**职责**（v0.14+ 已去流程化，专注生命周期与能力门面）：

- 窗口连接（幂等，仅创建 `Win32Controller`，MAA Tasker/Resource 归活动模块）

- 能力门面 `ActivityContext`（capture / gamepad / debug\_renderer 等窄接口 + ExitStack 生命周期托管）

- 模块生命周期（`start_module(module_id, start_from)` 分发到插件 `ActivityModule.start`）

- 点击方式（`real` 前台鼠标 / `background` 后台手柄导航+A / `intent` 仅意图）与独立真实点击开关

- 运行时静音游戏（audio\_volume）、运行结束自动关游戏/退出程序、急停循环

- 虚拟手柄租约管理（`_get_gpad` 懒创建 / `_reset_gpad` / `_destroy_gpad` ctypes 从总线拔除）

**核心类**：`MaaRacingAssistantController`

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

### 4.2 [gamepad\_cursor.py](file:///d:/maaracing_assistant/maaracing_assistant/core/gamepad_cursor.py) — 手柄光标导航引擎

**职责摘要**：签名剖面法识别游戏内白色圆盘光标（normal / interactive 两态）、摇杆-光标速度模型 + 闭环趋近导航、到位后确认点击（意图模式只导航不确认）；供 `core.clicker` 的「后台(手柄)」点击方式复用，与「前台(鼠标)」SendInput 同层。底座与手柄均依赖注入（复用 controller 的 `_gpad` / 模块的 capture），本模块不自建，避免手柄/截图冲突。

> 算法细节（光标识别三态、连续 P 趋近、速度模型标定）源自 cursor\_refactor 探针沉淀（工具归档于 `archive/cursor_refactor/`），运行时不依赖该目录；速度模型真源为 `core/resources/stick_speed_model.json`。

***

### 4.3 [yolo\_detector.py](file:///d:/maaracing_assistant/maaracing_assistant/core/yolo_detector.py) — YOLO 检测器

**职责**：

- ONNX Runtime 会话初始化（DirectML优先 → CUDA → CPU）

- 图优化 + DirectML 内核缓存

- 640×640 letterbox 预处理

- YOLOv8 输出解析（xywh → xyxy）

- **per-class NMS**：按类别分别做非极大值抑制，避免跨类压制（如car压掉bonus\_car）

- 双阈值输出：正式检测（高置信度，供决策用）+ 全量低阈值检测（供debug可视化）

**核心类**：`YOLODetector`

**类别映射**：

| 类别ID | 名称         | 说明       | 置信度阈值 |
| ---- | ---------- | -------- | ----- |
| 0    | coin       | 金币       | 0.35  |
| 1    | car        | 障碍车      | 0.35  |
| 2    | bonus\_car | 跳板车（奖励车） | 0.35  |

**性能指标**（参考 RTX 4060）：\~3.7ms/帧，跳帧后GPU负载降至1/3

***

### 4.4 [mra\_shell](file:///d:/maaracing_assistant/apps/mra_shell) — GUI 宿主（WinUI 3 + HTML 前端）

> v0.13.0 起 GUI 定案为 WinUI 3 shell + WebView2 HTML 前端（详见 §11）。旧 ttkbootstrap GUI（`gui.py` MRAGUI）已在重构时移除，以下历史记录仅供参考。

**进程模型**：

- `mra_shell.exe`（C# WinUI 3）：唯一 GUI，只做窗口 + sidecar 进程生命周期 + 消息转发

- `sidecar.py`（Python）：JSONL RPC 业务后端（stdin=request / stdout=response / stderr=日志）

- 前端 HTML 通过 `window.chrome.webview.postMessage` → C# → Python 通信，封装为 `mra.call(method, params)`

**前端文件**（[frontend/](file:///d:/maaracing_assistant/apps/mra_shell/frontend)）：

| 文件           | 职责                                         |
| ------------ | ------------------------------------------ |
| `index.html` | 三 Tab 页面结构（控制面板/调试/关于），所有 UI 元素 id 在此定义    |
| `style.css`  | 纯 CSS 设计 token + 组件样式（无 CDN，WebView2 离线可用） |
| `app.js`     | 通信层 + Tab 切换 + 日志轮询 + 调试页开关/截图方式交互         |

**窗口细节**：自定义标题栏 52px（进入 drag rect，右侧留 140px 给系统按钮）、最小尺寸 1000×700、系统按钮失焦配色、icon.ico。

**启动流程**：

1. 双击根目录 `MaaRacingAssistant.lnk`（定位 `mra_shell.exe`，exe manifest 自动 UAC 提权）
2. shell 启动 Python `sidecar.py`，建立 JSONL 双向管道
3. WebView2 加载 `frontend/index.html`，前端 `mra.call` 初始化状态
4. 用户操作 → 前端 RPC → sidecar → 模块执行

#### 4.4.1 旧 ttkbootstrap GUI 历史记录（已归档，仅存档）

> 原 `gui.py` MRAGUI（ttkbootstrap）的改进历史，代码已在重构时移除，此处仅存档。

| 改进项            | 说明                                                                                                                    | 核心实现                                          |
| -------------- | --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| 窗口可拖拽可调大小      | 原 `resizable(False, False)` → 改为 `resizable(True, True)`                                                              | `gui.py MRAGUI.__init__()` toplevel / root 配置 |
| 安全最小尺寸保护       | `minsize(480, 400)`，防止窗口缩小到 UI 元素互相重叠不可点                                                                              | 同上 init 阶段设置                                  |
| 日志按级别过滤        | `logger.get_lines(min_level=...)` 拉取；GUI 默认只显示 INFO/WARNING/ERROR 三级；DEBUG/TRACE 仅文件 + 显式勾选 DEBUG 存盘开关时显示             | `gui.py _poll_logs()`；配合 §4.6.1 日志分级约定        |
| 物理手柄检测阻止运行     | 非记录模式下调用 `has_physical_controller()` → 返回 True 时阻止 "开始" 并弹对话框提醒拔手柄                                                    | `gui.py _on_start_clicked()` 前置检查；§9.6 / §4.7 |
| 弹窗图标修正（不继承父窗口） | `messagebox.showerror` 默认丢 root.ico → 改用 `tk.Toplevel` + 手动 `dlg.iconbitmap(icon_path)` 设置独立应用图标；agents.md / §10 均有记录 | 物理手柄弹窗 / 模型缺失弹窗 / 连接失败弹窗                      |

***

### 4.5 [debug.py](file:///d:/maaracing_assistant/maaracing_assistant/core/debug.py) — 调试可视化

**职责**：

- 两套渲染模式：全量存盘（enabled）/ 精简PEEP预览（peep\_enabled）

- PEEP独立线程OpenCV窗口（\~30fps刷新，锁保护最新帧）

- 导航模式标注：光标(红)/候选(绿)/拉黑(紫)/过滤(黑)/按钮(蓝)/模板(青)

- 赛车模式标注：YOLO框(金/红/紫)/透视车道线/远中近分区/HUD状态栏

- 同类别重叠框去重（避免虚线框堆叠）

- 文字带黑色阴影描边（保证任何背景可读性）

**核心类**：`NavigationDebugger`

**颜色约定**：

| 颜色   | BGR值        | 含义                   |
| ---- | ----------- | -------------------- |
| 🔴 红 | (0,0,220)   | 选中的光标 / 障碍车car       |
| 🟡 金 | (0,215,255) | 金币coin / 按钮目标        |
| 🟣 紫 | (220,0,220) | 跳板车bonus\_car / 拉黑候选 |
| 🟢 绿 | (0,200,0)   | 入围光标候选 / 车道中线        |
| 🔵 青 | (255,255,0) | 模板匹配框 / 标线 / 距离分区线   |
| ⚫ 黑  | (0,0,0)     | 被硬过滤的轮廓              |
| 🟧 橙 | (0,140,255) | 左标线边缘散点              |
| 🔵 蓝 | (255,140,0) | 右标线边缘散点              |

**赛车HUD内容**：

- 左上：帧号 + raw/filt检测统计 + 各类数量

- 底部：摇杆位置条（←/→，彩色点）+ 数值

- 底部居中：决策原因（彩色）+ 详细说明

- 底部摇杆上方：±stop\_zone 死区宽度条（半透明绿填充 + 边界细线 + 中心线）

- 右上：前馈调试信息（off/stop/dx/dy/moving/in\_center/reason）

- 右上第四行：ff\_extra 预见性衰减原因说明（\[提前收敛…]/\[近区回摆…]/\[无预见性衰减]）

- 画面中部：CENTER\_L / CENTER\_R 半透明红竖线（中心区边界，L2c/R2c 标签）

**v0.12.0 新增 HUD 字段**：

- `dy`: 目标纵向接近速度（px/帧），右上第二行

- `ff_extra`: 预见性衰减原因（提前收敛ETA/近区回摆），右上第四行

- 中心区竖线：`_draw_racing_zones` 中追加，与透视车道线共享 overlay

- 死区条：`_draw_racing_hud` 底部，半宽 = stop\_zone × w/2，α=0.30

***

### 4.6 [logger.py](file:///d:/maaracing_assistant/maaracing_assistant/logger.py) — 日志系统

**职责**：

- 内存+文件双写日志

- 5个日志级别：TRACE < DEBUG < INFO < WARNING < ERROR

- GUI 默认只显示 INFO 及以上

- 按时间戳命名日志文件（`MRA_YYYYMMDD_HHMMSS.log`）

- 级别过滤提取（get\_lines）

**全局单例**：`logger = Logger(logs_dir)`

#### 4.6.1 日志分级速查

| 级别      | 用途      | 典型示例                                                           |
| ------- | ------- | -------------------------------------------------------------- |
| TRACE   | 超细节开发追踪 | 中间变量、帧级内部状态、循环内计数器步进                                           |
| DEBUG   | 详细调试信息  | 模板匹配各尺度置信度结果、保存调试图路径、第 N 次按 B、摇杆方向值（lx,ly）、死区判定细节              |
| INFO    | 关键业务里程碑 | 归位完成、返回主界面、开始循环、本轮完成、导航按钮点击成功、活动循环启动/结束、决策最终输出（如出价决策）          |
| WARNING | 警告但流程继续 | 截图快速方式失败降级MAA、归位超时、模板不存在、按钮未找到光标丢失、基准测试发现YOLO离群值（P95/P90>1.8×） |
| ERROR   | 错误需关注   | 模板加载失败、连接窗口失败、Pipeline异常、模型文件不存在、手柄创建失败、连续重试耗尽                 |

> **约定**：所有可继续运行的降级/兜底必须打 WARNING（不能静默）。不能恢复的故障打 ERROR 并配合 stop。

***

### 4.7 [window\_utils.py](file:///d:/maaracing_assistant/maaracing_assistant/window_utils.py) — 窗口与手柄检测

**职责**：

- `find_game_hwnd()`: 查找游戏窗口（优先UnrealWindow类名 → 标题关键词 → PID）

- `has_physical_controller()`: XInput API 检测物理手柄（xinput1\_4/1\_3/9\_1\_0）

- `hwnd_from_pid()`: EnumWindows 回调按PID找窗口句柄

**窗口查找关键词**："巅峰极速"、"g112"、"Racing Master"

**物理手柄检测**：遍历XInput端口0-3，`XInputGetState(i, buf) == 0` 表示已连接

***

### 4.8 [pipeline\_logger.py](file:///d:/maaracing_assistant/maaracing_assistant/pipeline_logger.py) — MAA Pipeline日志

**职责**：继承 `ContextEventSink`，监听Pipeline节点识别/动作事件，输出中文友好日志。

***

### 4.9 [opencv\_utf8\_patch.py](file:///d:/maaracing_assistant/maaracing_assistant/core/opencv_utf8_patch.py) — 中文路径补丁

**职责**：Monkey-patch `cv2.imread`/`cv2.imwrite`，支持中文Windows路径。ASCII路径走原生API，中文路径用 `np.frombuffer`/`cv2.imencode`+Python文件IO绕过。程序启动时import一次即全局生效。

***

### 4.10 [wgcap.py](file:///d:/maaracing_assistant/maaracing_assistant/core/wgcap.py) — WGC 持久化后台截图

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

**架构决策（v0.14 截图收敛后）**：

- **生产默认后端 = MAA FramePool**（`capture.screenshot()` 统一能力接口，截图帧由插件经 `ctx.capture` 消费）

- WGC（`wgcap.py` `WgcCapture`）曾作为 racing 生产截图后端（`capture_backend` 分派），**已随 v0.14 截图收敛移除**，此模块保留为可选工具（后台/遮挡场景备选）

- 线程安全：锁内仅交换 Python 引用和整数，NumPy 操作在锁外

- 帧所有权：NativeMappedFrame → bytes → ndarray，回调结束后 ndarray 地址复用但仍安全

***

## 5. 关键类与函数索引

> 鉴宝域（treasure\_\*）类速查见 [鉴宝域文档 §7](../maaracing_assistant/plugins/treasure/CODE_WIKI.md)。

### 5.1 controller.MaaRacingAssistantController

| 方法                                        | 说明                                                                      |
| ----------------------------------------- | ----------------------------------------------------------------------- |
| `__init__(capture_backend="wgc_latest")`  | 初始化能力门面、点击方式、急停等                                                        |
| `connect()`                               | 幂等窗口连接：仅创建 `Win32Controller(hWnd=...)`，连接超时 10s 保护 + 720p 窗口统一 + 屏幕内校验  |
| `start_module(module_id, start_from)`     | 分发到插件模块并启动（断点 `start_from` 由模块自己解析）                                     |
| `stop()`                                  | 停止模块、中断 Pipeline、销毁手柄                                                   |
| `set_click_mode(mode)` / `intent_mode`    | 点击方式切换：前台鼠标 / 后台手柄 / 仅意图                                                |
| `set_auto_shutdown(close_game, exit_mra)` | 运行结束后自动关游戏 / 退出程序（仅自然完成时）                                               |
| `set_mute_game(enabled)`                  | 运行时静音游戏（音频音量控制）                                                         |
| `set_emergency_stop(enabled)`             | 急停开关（后台轮询回路）                                                            |
| `set_auto_close_game` / `set_auto_exit`   | 自动收尾开关                                                                  |
| `gamepad_available()`                     | vgamepad / ViGEmBus 驱动可用性探测（缓存）                                         |
| `_get_gpad()`                             | 懒创建并返回虚拟手柄（复用，不销毁重建）                                                    |
| `_reset_gpad()`                           | 摇杆归零+按钮释放（不销毁）                                                          |
| `_destroy_gpad()`                         | 销毁虚拟手柄：显式 ctypes `vigem_target_remove` 从总线拔除（确定性）                       |
| `_screencap()`                            | 截图（FramePool → BGR→RGB），失败返回 None（主编排层；模块侧走 `ctx.capture.screenshot()`） |
| `_interruptible_sleep(s)`                 | 可中断睡眠（每 0.1s 检查 `_running`）                                             |

### 5.4 yolo\_detector.YOLODetector

| 方法                                                 | 说明                                                       |
| -------------------------------------------------- | -------------------------------------------------------- |
| `__init__(model_path, conf, iou)`                  | 初始化ONNX会话（DirectML/CUDA/CPU降级）                           |
| `__call__(img_rgb, roi)`                           | 推理入口：返回(coins, cars, bonus, debug\_dets, all\_raw\_dets) |
| `_nms_per_class(xyxy, scores, classes, mask, ...)` | 按类别分别做NMS，返回原始下标                                         |
| `_to_dets(xyxy, scores, classes, ... indices)`     | 索引转结构化检测结果dict                                           |
| `CLASS_CONF`                                       | 类属性：各类别置信度阈值字典                                           |

### 5.4 mra\_shell（WinUI 3 shell + sidecar）

| 类/文件                  | 职责                                                                       |
| --------------------- | ------------------------------------------------------------------------ |
| `MainWindow.xaml.cs`  | 窗口生命周期、WebView2 加载前端、AppWindowTitleBar drag rects                        |
| `PythonSidecar.cs`    | JSONL transport：stdin 串行写 + 唯一 stdout reader + pending 匹配 + 超时/Kill 树    |
| `App.xaml.cs`         | 应用入口（DISABLE\_XAML\_GENERATED\_MAIN），UAC 提权环境变量注入                        |
| `sidecar.py`          | Python 侧 RPC handler（get\_initial\_state / start / stop / set\_peep ...） |
| `frontend/app.js`     | 前端逻辑：`mra.call()` 通信 + 三 Tab 切换 + 日志/状态轮询                                |
| `frontend/index.html` | 页面结构，所有 UI 元素 id（改 UI 先改这里）                                              |
| `frontend/style.css`  | 设计 token + 组件样式                                                          |

### 5.6 debug.NavigationDebugger

| 方法                                 | 说明                     |
| ---------------------------------- | ---------------------- |
| `__init__(proj_dir)`               | 初始化                    |
| `enable_peep()` / `disable_peep()` | 开关PEEP实时预览窗口           |
| `start_session(label)`             | 开始一次调试会话（创建存盘子目录）      |
| `save_frame(img, **kwargs)`        | 统一入口：存盘全量绘制 + PEEP精简绘制 |
| `_render_full(img, **kw)`          | 全量标注绘制（存盘用）            |
| `_render_peep(img, **kw)`          | 精简绘制（PEEP用）            |

### 5.6 基础工具方法速查（core 共享）

跨模块高频工具函数，本表统一索引：

| 方法                              | 所属模块                           | 说明                                                     | 关键参数/坑点                                                 |
| ------------------------------- | ------------------------------ | ------------------------------------------------------ | ------------------------------------------------------- |
| `_screencap()`                  | Controller（模块侧走 `ctx.capture`） | 截图 RGB ndarray（FramePool → BGR→RGB）                    | 主编排层方法；模块经 `capture.screenshot()` 能力接口统一取值              |
| `_interruptible_sleep(seconds)` | Controller                     | 每 0.1 s 轮询检查 `_running` 的可中断 sleep                     | stop 能 0.1 s 级响应；**不要用** **`time.sleep(>0.2)`**         |
| `NavigationDebugger(proj_dir)`  | debug.py                       | PEEP 实时预览 / debug 截图标注，支持 template\_rects + detections | §5.5；§9.3 调试模式说明                                        |
| `has_physical_controller()`     | window\_utils.py               | XInput API 遍历 4 端口，任一连接返回 True                         | DLL 回退 xinput1\_4 → xinput9\_1\_0 → xinput1\_3；§10.4 坑点 |

***

## 6. 模块依赖关系

### 6.1 导入关系图

```
core/sidecar.py（JSONL RPC handler）
  ├── core.controller.MaaRacingAssistantController
  ├── core.logger.logger
  ├── core.window_utils.has_physical_controller
  └── core.registry（插件自动扫描注册）

mra_shell（C#，不导入 Python）
  └── PythonSidecar（stdin/stdout JSONL 通信）
        └── 子进程：python -m maaracing_assistant（core/sidecar.py）

core/controller.py
  ├── core.sidecar（handler） / core.registry
  ├── core.base.ActivityContext / core.base.ActivityModule
  ├── core.clicker（点击方式）/ core.gamepad_cursor / core.audio_volume
  ├── core.window_utils.find_game_hwnd / resize_game_window_720p / is_window_on_screen
  ├── core.logger.logger
  └── plugins.<id>.module（ActivityModule 启动分发）

plugins/treasure/module.py
  └── treasure_detector / treasure_ocr / strategy / eggs / renderer / store（同目录）

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
core/sidecar.py（mra_shell 托管）
  └── controller: MaaRacingAssistantController
        ├── debug: NavigationDebugger
        ├── tasker: Tasker
        │     └── context_sink: PipelineLogger
        ├── resource: Resource
        ├── controller: Win32Controller
        └── 活动模块实例（start_module 经 registry.create_module 按需创建，
              经 ActivityContext 门面访问 tasker/resource/capture/gamepad 等能力）
```

***

## 7. 运行流程详解

### 7.1 启动流程

```
双击根目录 mra_shell.exe / MaaRacingAssistant.lnk（exe manifest 自动 UAC 提权）
  → WinUI 3 shell 创建窗口（AppWindowTitleBar）并展示 HTML 前端
  → shell 拉起 Python sidecar（python -m maaracing_assistant）
    → sidecar 初始化 Controller，等待 stdin JSONL RPC
  → 前端通过 mra.call(method, params) 与 sidecar 通信
```

独立调试 sidecar（不经 GUI）：`python -u -m maaracing_assistant.core.sidecar`（等待 stdin JSONL RPC）。

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

### 8.1 图像与分辨率

| 常量    | 值        | 说明                             |
| ----- | -------- | ------------------------------ |
| 游戏分辨率 | 1280×720 | 所有坐标基于此（全局约定，各插件 ROI/按钮均按此归一化） |

### 8.2 版本管理

- 版本号由 `setuptools-scm` 从 Git Tag 自动生成

- 格式：`vX.Y.Z`（SemVer 2.0.0）；0.x 阶段全部为 pre-release（`v0.x.y-dev.N`）

- 入口：`git tag vX.Y.Z && git push origin vX.Y.Z` 触发CI Release

**双轨版本机制（v0.13.0-dev.5 起，`maaracing_assistant/__init__.py`）**：

- 打包/安装产物（无 `.git`）：读 `_version.py` 构建快照（setuptools-scm 构建时写入）→ 版本固化，**旧版本不会被仓库后续新 tag 带歪**

- 源码直接运行（目录下有 `.git`）：忽略可能过期的 `_version.py`，启动时 `git describe --tags --long` 按**当前 checkout** 动态推导（checkout 到旧 tag 就显示旧版本号）

- 兜底：`"0.0.0.dev"`

- **sidecar 必须读包级** **`__version__`**（`from maaracing_assistant import __version__`），**不能读** **`_version.__version__`**——后者是 `_version.py` 文件里的构建时硬编码，与 `__init__.py` 动态推导的包级 `__version__` 不是同一个对象（v0.13.0-dev.5 真实踩过：sidecar 一直返回过期版本号）

***

## 9. 开发与调试

### 9.1 安装开发环境

```bash
git clone https://github.com/d542Bb/MaaRacingAssistant.git
cd MaaRacingAssistant
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 9.2 运行

```bash
python -u -m maaracing_assistant.core.sidecar  # 独立调试 sidecar（等待 stdin JSONL RPC）
```

### 9.3 调试模式

1. **DEBUG 存盘模式**：GUI 勾选"DEBUG 每帧截图"，每帧全量标注保存到 `%APPDATA%/MaaRacingAssistant/debug/<module>/<会话>/` 目录
2. **PEEP 实时预览**：GUI 勾选"PEEP 实时预览"，弹出 OpenCV 窗口实时显示精简标注画面（\~30fps）
3. **断点调试**：GUI 断点列表双击选择起始阶段，跳过前面的导航步骤
4. **NavKit 控制台**：`tools/navkit`（`python tools/navkit/server.py --module treasure`）浏览会话、查看 v3 结构树、校准资产；`/api/assets` 保存前校验，`/api/trace` 读取决策流水

### 9.4 YOLO模型训练

`tools/training/train.py` 提供 YOLO 训练→ONNX 导出链路（Ultralytics yolo11n 微调）；导出目标为归档插件目录 `archive/racing/resources/onnx/model.onnx`。当前版本不随发行包分发模型权重，含检测的插件启用时再由插件自带并声明 `REQUIRED_ASSETS`。

### 9.5 日志位置（%APPDATA%/MaaRacingAssistant/）

用户数据目录 `user_data_dir()` 五目录结构（[paths.py](file:///d:/maaracing_assistant/maaracing_assistant/core/paths.py)）：

- `config/`：`profile.json`（用户偏好）、`maa_option.json`

- `data/`：结构化业务数据（`data/treasure/treasure.db`）

- `logs/`：运行日志 `MRA_YYYYMMDD_HHMMSS.log`（含 DEBUG 级全量）

- `framework/`：MAA 框架自产物（`maafw.log`、cache）

- `debug/`：调试截图会话 `debug/<module>/<会话>/`（DebugStudio 契约）

GUI 只显示 INFO 及以上；记录数据（历史 CSV）同随用户数据目录管理。

### 9.6 物理手柄检测

- 前台点击（real）/ 后台手柄（background）模式检测到物理手柄可能冲突；intent 模式仅算意图不干扰

- 检测方法：XInputGetState遍历端口0-3（`XInputGetState(i, buf) == 0` 表示已连接）

- 历史"记录模式"（读取物理手柄采集数据）已随 v0.14 重构移除，不再适用

### 9.7 NavKit 模板图采集工作流（v3 资产，2026-09-07 定案）

> 适用：为 `*_assets.json` 锚点采集模板图。分工：**人工截图+裁剪标注 → 导出
> regions.json + PNG → 工具侧换算入库**（rect 归一化/JSON/校验/提交均为工具侧职责）。

**来源纪律**：只从 1280×720 运行帧截图裁剪（`window_utils` 启动即统一 720p），
1:1 像素裁剪、**永不缩放**（模板与运行帧同尺度，匹配单尺度 1.0）。

**裁剪纪律**：只装"一年后还长这样的像素"——角标/数字/倒计时/红点/限时横幅一律
留在模板外。变化在模板图**外**（哪怕紧贴）不影响匹配得分；在图**内**才失效
（阈值 0.75 容忍渲染抖动，不容忍结构性变化）。

**命名**：`<页面>_<元素>[_限定词].png`，全小写下划线；首段页面名与 assets 的
`pages` 注册表一致（`hall_` / `rank_` / `speedrush_`…），看文件名即知归属。

**位置跟 owner 走**：global 锚点 → `core/resources/image/`；模块锚点 →
`plugins/<id>/resources/image/`。归属与物理位置矛盾由 W04 盯。

**回传**：标注工具导出 regions.json（像素区域 x/y/w/h）+ PNG → 工具侧换算
归一化 rect（外扩 15% 宽 / 25% 高、4 位小数）写入 assets；**JSON 永远工具侧维护，
人工不手改**（改资产必须重编译，CI `--all --check` + `source_hash` 守着）。

**守卫闭环（已有机具，无需新造）**：W01 = 死模板探测器（锚点下线删引用后，
孤儿图自动浮出 → 连文件一起删）；W02 悬空引用；W04 归属错位；E20/CI 防手改
生成物。**体积账**：单张 5\~60KB、全部模板预期 <1MB（对比 rapidocr 模型 30MB、
onnx 12MB 不成量级）——要防的是"图死"（W01 管）不是"图多"。

**MAA 对照**：MaaFramework 侧仅约定"720p 无损原图裁剪勿缩放 + `roi`/`box`/`target`
三概念分离"（本指南 §5.1/§5.2）；社区靠 ImageCropper 类工具 + 人工纪律，无结构化
工作流。MRA 在其上加 regions 机器可读导出 + 校验守卫闭环。

***

## 10. 已知坑点与注意事项

> 鉴宝坑点见 [鉴宝文档 §9](../maaracing_assistant/plugins/treasure/CODE_WIKI.md)。

### 10.1 系统层

| 坑点                        | 说明                                            | 解决方案                                           |
| ------------------------- | --------------------------------------------- | ---------------------------------------------- |
| 截图需要管理员权限                 | PrintWindow/BitBlt需要提升权限                      | mra\_shell.exe manifest 自动 UAC 提权（一次，child 继承） |
| ~~ttkbootstrap 相关坑~~（已移除） | 旧 GUI 遗留                                      | 代码已重构移除，不再适用                                   |
| cv2不支持中文路径                | imread/imwrite在中文路径下失败                        | opencv\_utf8\_patch.py monkey-patch            |
| messagebox不继承图标           | tk.messagebox弹窗无图标                            | 用tk.Toplevel自行创建+iconbitmap                    |
| YOLO ONNX 导出              | `onnx.export(simplify=True)` 可能产生损坏模型（推理结果错乱） | 导出时关闭 simplify，或导出后校验精度                        |

### 10.2 MAA Framework API

| API                               | 正确用法                                    | 常见错误                  |
| --------------------------------- | --------------------------------------- | --------------------- |
| Toolkit.init\_option              | `init_option(path, "")` 第二个参数传空字符串      | 不要传None               |
| Win32Controller                   | `Win32Controller(hWnd=hwnd)` 参数名驼峰hWnd  | 不要写成hwnd=             |
| Tasker.bind                       | `bind(resource, controller)` resource在前 | 参数顺序反了会崩溃             |
| Resource.post\_bundle             | `post_bundle(path)`                     | 不是post\_path          |
| Resource.register\_custom\_action | `register_custom_action(name, action)`  | action需继承CustomAction |
| MAA PostScreencap返回               | BGR格式（OpenCV默认）                         | 需要手动cvtColor转RGB      |

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

### 10.4 物理手柄XInput

- `XInputGetState(i, buf) == 0` 表示第i号物理手柄已连接

- 尝试加载顺序：xinput1\_4.dll → xinput1\_3.dll → xinput9\_1\_0.dll

- 非记录模式必须断开所有物理手柄，否则虚拟手柄被游戏忽略或冲突

***

## 11. GUI 宿主选型（WinUI 3 定案）

> 2026-08 定案。目标：HTML/WebView2 前端 + 原生 Windows 窗口行为（DWM 动画/系统按钮/Snap），Python 保持唯一业务后端（sidecar 模式）。**已落地**：正式 GUI 为 `apps/mra_shell/`（WinUI 3 shell + HTML 前端），旧 ttkbootstrap GUI（`gui/`、`gui_webview/`）与历史 spike 已归档至 `archive/`。

### 11.1 选型历程（三个 Spike 实测结论）

| 候选                              | 结论     | 根因                                                                                                                                                                              |
| ------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| pywebview frameless             | 出局     | `FormBorderStyle.None` 无 WS\_CAPTION → 无 DWM 动画；官方无 custom titlebar（master 分支新增 `drag_region.py` 示例但动画无解）                                                                       |
| Tauri v2 (2.11.5)               | 出局     | `titleBarStyle` 是 **macOS-only**：builder 标 `#[cfg(target_os="macos")]`、`set_title_bar_style` 注释「macOS only」、tao Windows 无实现、wry 无 WCO 代码                                        |
| WPF + WindowChrome              | 出局     | WebView2 **airspace**：WebView2 是独立 HWND 铺满客户区，把 WindowChrome 的拖动区（CaptionHeight）/模拟 caption buttons（UseAeroCaptionButtons）/resize 边缘（ResizeBorderThickness）全部遮挡、鼠标被 WebView2 吞掉 |
| **WinUI 3 + AppWindowTitleBar** | **定案** | drag rects / caption buttons 是**系统级 NC 处理**，不被 WebView2 遮挡；HTML 铺顶 + 系统按钮 overlay + DWM 动画 + 拖动/双击/圆角全部通过                                                                       |

### 11.2 锁定版本与依赖

- Windows App SDK **1.8.260710003**（NuGet；meta 包，依赖拆成 9 个子包，restore 自动拉）

- WebView2 SDK **1.0.4129.50**（仅 WPF spike 用到；WinUI 3 的 `Microsoft.UI.Xaml.Controls.WebView2` 随 WindowsAppSDK 提供）

- .NET SDK 8.0.123（本机已装 8/9/10）

- WinUI 3 未打包应用：`WindowsPackageType=None` + `WindowsAppSDKSelfContained=true`（免装 Windows App Runtime）

- spike 原型：`archive/`（已归档，`prototypes/` 已迁移至 `apps/`）

### 11.3 NuGet 网络坑（本机）

| 坑点                                 | 说明                                | 解法                                                                   |
| ---------------------------------- | --------------------------------- | -------------------------------------------------------------------- |
| nuget.org 被网络阻断                    | dotnet 报 SSL EOF；curl 能 302 到 CDN | 用 **Azure CN 镜像** `https://nuget.azure.cn/v3/index.json`（restore 可用） |
| `dotnet restore --source <源名>` 当路径 | 源名被解析为相对目录                        | 用**项目级** **`NuGet.Config`**（`<clear/>` + 指定源），restore 不带 --source    |
| 本地包源                               | curl 手动下载 nupkg 到目录               | `dotnet nuget add source <dir>`                                      |

### 11.4 WinUI 3 关键 API（AppWindowTitleBar）

| API                                                    | 用途                            | 注意                                                             |
| ------------------------------------------------------ | ----------------------------- | -------------------------------------------------------------- |
| `AppWindow.TitleBar.ExtendsContentIntoTitleBar = true` | 内容延伸到标题栏，保留系统 caption buttons | 关闭系统标题栏视觉，按钮仍在右上 overlay                                       |
| `AppWindow.TitleBar.SetDragRectangles(RectInt32[])`    | HTML 拖拽区                      | **物理像素**；窗口尺寸变化（`AppWindow.Changed` + `args.DidSizeChange`）需重设 |
| `AppWindow.TitleBar.PreferredHeightOption`             | 系统按钮高度                        | `Standard` / `Tall`                                            |
| `Window.AppWindow`                                     | 获取 AppWindow                  | Windows App SDK 1.4+                                           |

- **drag region 交互区挖孔（v0.13.0-dev.5）**：顶部 52px 整条设为 drag rect 时，双击 tab/品牌按钮区会触发最大化（按钮被「标题栏」行为吃掉）。方案：前端 `reportDragExcludes()` 测量 `.brand`+`.tabs` 合并矩形 → `postMessage({type:'drag-exclude', rect})` → C# 收到后存 DIP 矩形，`UpdateDragRects()` 按 DPI 换算挖孔（左段+右段+按钮下方段三段），坐标基准 = 窗口左上角（HTML 延伸进标题栏后 DOM (0,0) 即窗口左上角）

- **单实例互斥（v0.13.0-dev.5，`Program.cs`）**：`AcquireSingleInstance()` 用命名 Mutex（`Global\MRA_SingleInstance`，权限异常降级会话级）检测已有实例 → `MessageBoxW` 询问「启动新进程（taskkill /T 连 sidecar 杀旧进程）或取消保留旧进程」；旧进程被强杀后接管 Mutex 需捕获 `AbandonedMutexException`

- 系统按钮颜色跟随系统主题（native 正常表现，非 bug）

- Snap Layout hover 在 AppWindowTitleBar 下未出现（用户确认不在乎；疑似系统 SnapAssist 设置，未深究）

### 11.5 WinUI 3 构建坑

| 坑点                                            | 解决                                                                                         |
| --------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 手写 `Program.cs` 与 XAML 自动生成 Main 冲突（CS0101）   | csproj 加 `DefineConstants=$(DefineConstants);DISABLE_XAML_GENERATED_MAIN`                  |
| `Application.Start` 回调参数用 `_` 与丢弃赋值冲突（CS0029） | 参数命名 `p`                                                                                   |
| 未打包应用入口样板                                     | `WinRT.ComWrappersSupport.InitializeComWrappers()` + DispatcherQueueSynchronizationContext |

### 11.6 后续步骤（sidecar 架构）

- 进程模型：`MRA.exe`（C# WinUI 3 shell）+ `maaracing_backend.exe`（PyInstaller sidecar）

- IPC：stdin/stdout **JSONL**（stdin=request / stdout=response+event / stderr=日志），消息带 `type` 字段；C# 侧单一 reader task + `pending` map + timeout

- 管理员权限：只放最外层 exe manifest，一次 UAC，child 继承

- Rust 三原则平移为 C#：**C# 只做窗口/启停 Python/转发消息**，Controller 业务不进入 shell

### 11.7 sidecar transport 契约测试（Step 2 完成）

> `archive/sidecar_spike/`（已归档）：`PythonSidecar.cs` + `fake_sidecar.py` + `Program.cs`。**11/11 通过**（2026-08），正式 shell 的 transport 直接复用。

**契约要点（C# PythonSidecar）**：

| 项                | 实现                                                                                            |
| ---------------- | --------------------------------------------------------------------------------------------- |
| 唯一 stdout reader | 一个常驻 `ReaderLoopAsync`，按 `response.id` 匹配 `ConcurrentDictionary<ulong, TCS>`                  |
| stdin 串行         | `SemaphoreSlim` 写锁                                                                            |
| 超时               | `Task.WaitAsync(timeout)`，超时清理 pending，只影响单请求                                                 |
| backend 断开       | reader EOF → 所有 pending 立即抛 `BackendDisconnectedException`                                    |
| malformed stdout | 忽略并记 stderr，不 crash 整个 IPC                                                                    |
| stderr drain     | 独立 task 持续读，防 OS pipe 填满卡死 Python                                                             |
| shutdown         | `ShutdownAsync(grace)`：发 shutdown → 等进程自退 → 超时 `Kill(entireProcessTree:true)`；返回退出码，不 Dispose |
| 防孤儿              | `Dispose()` 对存活进程 KillTree                                                                    |

**契约测试坑点（必记）**：

| 坑点                  | 说明                                                                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `JsonDocument` 生命周期 | reader 中 `using var doc` 循环末释放，`SetResult` 必须传 `root.Clone()`（深拷贝），否则调用方访问即 ObjectDisposedException                              |
| Python worker 线程退出  | `sys.exit()` 在非主线程只抛 SystemExit 不退出进程，必须 `os._exit(n)`                                                                           |
| Dispose 后访问 Process | `_process.Dispose()` 后访问属性抛「No process is associated」；验证进程存活用 `ProcessId` + `Process.GetProcessById(pid)` 捕获 `ArgumentException` |

> 正式 sidecar：[sidecar.py](file:///d:/maaracing_assistant/maaracing_assistant/core/sidecar.py)（Step 4 完成，已命令行验证）。入口强制 `sys.stdout = _StdoutGuard`（一切误写转 stderr）。**坑**：handler 线程必须非 daemon——stdin EOF 后主线程退出会杀 daemon，导致 shutdown 等响应丢失。

***

## 附录：类速查表

> 主程类速查见下表；鉴宝类（TreasureModule / TreasureStageDetector / TreasureOcr / TreasureDebugRenderer）见 [鉴宝文档 §7](../maaracing_assistant/plugins/treasure/CODE_WIKI.md)。

| 类名                                   | 文件                                                                                           | 核心职责                                 |
| ------------------------------------ | -------------------------------------------------------------------------------------------- | ------------------------------------ |
| `MaaRacingAssistantController`       | core/controller.py                                                                           | 主控编排：能力门面 + 模块生命周期 + 全局设置            |
| `ActivityModule` / `ActivityContext` | core/base.py                                                                                 | 模块基类 / 能力门面（窄接口 + ExitStack 生命周期）    |
| `Registry`                           | core/registry.py                                                                             | 插件自动扫描注册（扫 `plugins/*/manifest.py`）  |
| `TreasureModule`                     | plugins/treasure/module.py → [鉴宝文档 §1](../maaracing_assistant/plugins/treasure/CODE_WIKI.md) | 巅峰鉴宝活动模块（12阶段状态机）                    |
| `MRAGUI`                             | ~~gui.py~~（已归档移除）                                                                            | 旧 ttkbootstrap 图形界面（已废弃，代码已删）        |
| `Sidecar`                            | core/sidecar.py                                                                              | JSONL RPC 业务后端（mra\_shell 托管）        |
| `NavigationDebugger`                 | core/debug.py                                                                                | PEEP预览、截图标注（存盘走 debug\_io IO worker） |
| `Logger`                             | core/logger.py                                                                               | 内存+文件双写日志（用户数据目录）                    |

