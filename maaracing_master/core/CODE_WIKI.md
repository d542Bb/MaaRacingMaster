# MaaRacingMaster — Code Wiki · core（平台核心层）

> **本文是什么**：`maaracing_master/core/` 的当前实现知识——各模块职责、依赖与持有关系、类索引、core 侧坑点、调试操作。与 `plugins/<id>/CODE_WIKI.md` 同层对称：对象是 core，文档住 core。
> **本文不是什么**：不是项目导航与架构总览（见 [docs/CODE_WIKI.md](../../docs/CODE_WIKI.md)）；不是架构决策的理由与状态（见 [docs/adr/](../../docs/adr/README.md)）；不是 MaaFW 协议口径与红线（见 [docs/MAAFW_GUIDE.md](../../docs/MAAFW_GUIDE.md)）；不是活动域知识（见 `plugins/<id>/CODE_WIKI.md`）；不是 GUI 壳与前端施工事实（见 [apps/MaaRacingMaster.Shell/README.md](../../apps/MaaRacingMaster.Shell/README.md)）。
> **信源等级**：L2 —— 可作「core 现在怎么工作、改它要注意什么」的直接依据；不得作为架构规则（L1）或类、签名、常量的最终依据。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../AGENTS.md)；本文只补充 core 特有约束。

## 真源路由

| 信息 | 真源 |
|---|---|
| 主控生命周期与能力门面 | [`core/controller.py`](./controller.py) |
| 光标识别、趋近与速度模型 | [`core/gamepad_cursor.py`](./gamepad_cursor.py) + [`core/resources/stick_speed_model.json`](./resources/stick_speed_model.json) |
| 帧采集（单生产者） | [`core/wgcap.py`](./wgcap.py)（拓扑为何如此见 [ADR-0003](../../docs/adr/0003-运行时拓扑硬约束.md)） |
| 日志级别、通道与落盘形态 | [`core/logger.py`](./logger.py) |
| 能力窄接口与契约 | [`core/capabilities.py`](./capabilities.py) |
| 用户数据目录结构 | [`core/paths.py`](./paths.py) |
| 游戏分辨率 720p | [`core/window_utils.py`](./window_utils.py) `resize_game_window_720p`（连接时统一窗口，各插件 ROI / 按钮据此归一化） |
| 文字识别引擎（跨插件共享） | [`core/ocr.py`](./ocr.py)（§1.11：调参、抠图边界与预处理口径的唯一真源） |
| 远程元数据信任边界（公告/版本标记校验、外链目标白名单） | [`core/remote_meta.py`](./remote_meta.py)（§1.12） |
| MaaFW 对象签名与红线 | [docs/MAAFW_GUIDE.md](../../docs/MAAFW_GUIDE.md) |

***

## 1. 核心模块说明

### 1.1 [controller.py](./controller.py) — 主控编排器

**职责**（v0.14+ 已去流程化，专注生命周期与能力门面）：

- 窗口连接（幂等，仅创建 `Win32Controller`，MAA Tasker/Resource 归活动模块）

- 能力门面 `ActivityContext`（capture / gamepad / debug_renderer 等窄接口 + ExitStack 生命周期托管）

- 模块生命周期（`start_module(module_id, start_from)` 分发到插件 `ActivityModule.start`）

- 点击方式（`real` 前台鼠标 / `background` 后台手柄导航+A / `intent` 仅意图）与独立真实点击开关

- 运行时静音游戏（audio_volume）、运行结束自动关游戏/退出程序、急停循环

- 虚拟手柄租约管理（`_get_gpad` 懒创建 / `_reset_gpad` / `_destroy_gpad` ctypes 从总线拔除）

**核心类**：`MaaRacingMasterController`

**关键属性**：

| 属性                  | 类型              | 说明                                                  |
| ------------------- | --------------- | --------------------------------------------------- |
| `controller`        | Win32Controller | 游戏窗口控制器（connect 时创建，幂等）                             |
| `ctx`               | ActivityContext | 能力门面（模块经窄接口办事）                                      |
| `active_module`     | ActivityModule  | 当前活动模块                                              |
| `click_mode`        | str             | 点击方式：`real`（前台） / `background`（后台手柄）/ `intent`（仅意图） |
| `intent_mode`       | bool            | 是否仅准星意图（不真实点击）                                      |
| `gamepad_available` | bool            | vgamepad 驱动是否可用（ViGEmBus）                           |
| `module_active`     | bool            | 模块是否在运行                                             |

***

### 1.2 [gamepad_cursor.py](./gamepad_cursor.py) — 手柄光标导航引擎

**职责摘要**：签名剖面法识别游戏内白色圆盘光标（normal / interactive 两态）、摇杆-光标速度模型 + 闭环趋近导航、到位后确认点击（意图模式只导航不确认）；供 `core.clicker` 的「后台(手柄)」点击方式复用，与「前台(鼠标)」SendInput 同层。底座与手柄均依赖注入（复用 controller 的 `_gpad` / 模块的 capture），本模块不自建，避免手柄/截图冲突。

> **速度模型真源 = [`core/resources/stick_speed_model.json`](./resources/stick_speed_model.json)**；光标识别三态、连续 P 趋近与速度模型标定的参数均出自该文件。

***

### 1.3 [yolo_detector.py](./yolo_detector.py) — YOLO 检测器

**职责**：

- ONNX Runtime 会话初始化（DirectML优先 → CUDA → CPU）

- 图优化 + DirectML 内核缓存

- 640×640 letterbox 预处理

- YOLOv8 输出解析（xywh → xyxy）

- **per-class NMS**：按类别分别做非极大值抑制，避免高置信类别压掉相邻类别

- 双阈值输出：正式检测（高置信度，供决策用）+ 全量低阈值检测（供 debug 可视化）

**核心类**：`YOLODetector`

**类别映射与阈值（2026-09-21 参数化，commit `1c695ed`）**：检测器为跨活动基础设施，
**类别集由所服务的活动定义**、core 不内置任何游戏的类别名与阈值（旧 `CLASS_CONF`
类属性覆写通道已删）。类别表默认读模型 ONNX 元数据（Ultralytics 导出的 `names`），
构造参数 `classes` 可显式覆盖（显式给定时校验前置于模型加载，fail-fast）；逐类阈值走
`class_conf`（按类别名键，未覆盖类回退 `conf`）。返回 `(by_class, detections, all_raw_dets)`，
`by_class` 对每个已声明类别有键。**当前使用者**：speedrush 感知层
（[`plugins/speedrush/CODE_WIKI.md`](../plugins/speedrush/CODE_WIKI.md) §7）。

**坑（测试侧）**：onnxruntime-directml 1.24.4 原生缺陷——任一 session 析构后再对另一
存活 session `run()` 即段错误（CPU provider 无此问题）；多 detector 测试须全部保活至
进程结束（见 `tests/test_yolo_detector.py` 的 `_KEEP_ALIVE`）。生产单实例常驻不触发。

**模型训练 / 导出**：`tools/training/train.py`（Ultralytics yolo11n 微调 → ONNX），`dataset.yaml` 配类别、`auto_label.py` 自动标注。**模型权重不随发行包分发**：含检测的插件启用时自带并声明 `REQUIRED_ASSETS`。

***

### 1.4 [debug.py](./debug.py) — 调试可视化

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

### 1.5 [logger.py](./logger.py) — 日志系统

**职责**：

- 内存+文件双写日志；内存缓冲为**有界环形 deque**（`BUFFER_CAPACITY=5000`），长跑不随日志量增长

- GUI 增量读取用**单调序列号游标**（`get_lines_since(seq, ...)`），环形回绕后不重不漏、落后过多时返回 `truncated`

- 落盘**单流全量**：一次开启新建一个**会话目录** `logs/<MaaRM时间戳>/`，日志落其中的 `MaaRM_<ts>.log`，各级别按发生顺序写入；伴随产物（鉴宝决策流水 `trace.jsonl`）**共用同一个开关、同放该目录**（模块侧取 `logger.session_dir`，取到 `None` 即表示本次不落盘）。按大小轮转（`MAX_BYTES`/`BACKUP_COUNT`）+ 启动时按会话保留清理（`KEEP_SESSIONS`，会话目录整目录回收，旧版平铺的 `MaaRM_<8位日期>_<6位时间>` 文件同样归组回收）。**不按级别分档**——诊断是顺序的（「失败之前发生了什么」），按级别切分会切断时序且不可逆：切出的两份文件各自都读不通（DEBUG 档无业务锚点、INFO 档无诊断细节）。级别过滤只发生在读侧（GUI）与导出侧

- 5个日志级别：TRACE < DEBUG < INFO < WARNING < ERROR

- GUI 默认只显示 INFO 及以上

- **通道机制**：`log(channel=...)` 支持按子系统调级（`set_channel_level`/`clear_channel_level`）；`channel=None` 回落 `DEFAULT_CHANNEL`（app）。通道名是不透明字符串，core 不枚举/校验（红线 I7）

- 线程安全：写盘持单句柄 + `Lock`；`close()` 关句柄 flush 尾部

**全局单例**：`logger = Logger(logs_dir)`

#### 1.5.1 日志分级速查

| 级别      | 用途      | 典型示例                                                           |
| ------- | ------- | -------------------------------------------------------------- |
| TRACE   | 超细节开发追踪 | 中间变量、帧级内部状态、循环内计数器步进                                           |
| DEBUG   | 详细调试信息  | 模板匹配各尺度置信度结果、保存调试图路径、**框架节点生命周期**（`[Pipeline]` 识别成功/动作开始/动作成功）、第 N 次按 B、摇杆方向值（lx,ly）、死区判定细节 |
| INFO    | 关键业务里程碑 | 归位完成、返回主界面、开始循环、本轮完成、导航按钮点击成功、活动循环启动/结束、决策最终输出（如出价决策）。**框架内部节点跳转不算里程碑**，见 DEBUG 行 |
| WARNING | 警告但流程继续 | 截图快速方式失败降级MAA、归位超时、模板不存在、按钮未找到光标丢失、基准测试发现YOLO离群值（P95/P90>1.8×） |
| ERROR   | 错误需关注   | 模板加载失败、连接窗口失败、Pipeline异常、模型文件不存在、手柄创建失败、连续重试耗尽                 |

> **约定**：所有可继续运行的降级/兜底必须打 WARNING（不能静默）。不能恢复的故障打 ERROR 并配合 stop。

> **产出侧约定**（与上一条正交）：**不带结论的行不记**（无 `hit` 的识别、无结果的动作生命周期）、**稳态重复改边沿触发**（逐模板/逐节点未命中只在状态翻转时记一次，命中即重新武装）、**失败链首次必记**（其后按帧节流、成功清链）。判据与取证口径见 [§5.5](#55-日志判据与取证口径)。

> **通道维度**：级别与通道是正交的两件事——级别管「读的时候要不要看」（GUI 默认 INFO 及以上，导出可另设阈值）；通道管「这一行要不要打」（调级）。两者都**不影响写侧**：落盘是单流全量。未设置通道级别时全记录（兼容旧行为）。

#### 1.5.2 组协议（结构化日志，ADR-0007）

环形缓冲存 `(seq, record)`，**记录是唯一事实源**，文件与 GUI 是它的两份投影：

- **事件三型**：`log` / `group_start` / `group_end`；record 字段含 `schema_version, seq, event_type, group_id, session_id, ts, channel, level, kind, title, message, fields, outcome, has_warning, has_error, duration_ms`。
- **API**：`logger.group(title, kind, channel)` → `GroupHandle`（`.log()` / `.end(outcome=None)` / `with` 糖）；`log(msg, level, channel, *, group_id=, fields=)` 返回 seq。`end` **首调定终态、重复调幂等**（墓碑 + `double_end` 计数）；野 `group_id` 降级为无组 + 计数。跨线程传**捕获的 group_id**（句柄不跨线程传递，方案 A 裁定）。
- **状态模型**：开组即 `running`；`end` 缺省按组内 ERROR/WARNING **自动推导** failure/warning/success；显式 success 可与 has_warning 并存（WARNING=可恢复降级约定）；`Logger.close()`（sidecar 唯一收尾路径）把开着的组补 `incomplete`，只执行一次。
- **双投影**：文件=全事件流，组以 GH 风格 `::group:: 标题` / `::endgroup:: outcome` 标记（`%`→`%25`、`:`→`%3A`、换行→`%0A`）；GUI 文本行=仅 `log` 事件（`fetch_logs` 过渡期同返 `lines` 与 `events`，含 `next_seq/truncated/gap/schema_version/session_id`）。
- **fields 先净化后入库**：JSON 标量、≤16 键 / 键 32 字符 / 串 200 字符、NaN/Inf→None、敏感键（password/token/secret/api_?key/cookie/authorization）剔除；违规只进 `internal_counters` 不记日志（防递归）、不向业务线程抛异常。
- **channel 是写闸不是组归属**：组事件豁免级别门（结构完整性 > 体量控制，防无头组）；被门掉的行不参与终态推导。
- **插件用法**：模块级 `_tlog/_open_grp/_end_grp` 三函数 + 类上薄包装 + `_log_grp` 句柄槽（照抄 treasure 形态）；跨切面日志有意保持无组。结构锁：`tests/test_treasure_log_groups.py`、`tests/test_speedrush_log_groups.py`（裸 INFO 注入必红）。

***

### 1.6 [window_utils.py](./window_utils.py) — 窗口与手柄检测

**职责**：

- `find_game_hwnd()`: 查找游戏窗口（优先UnrealWindow类名 → 标题关键词 → PID）

- `has_physical_controller()`: XInput API 检测物理手柄（xinput1\_4/1\_3/9\_1\_0）

- `hwnd_from_pid()`: EnumWindows 回调按PID找窗口句柄

**窗口查找关键词**："巅峰极速"、"g112"、"Racing Master"

**物理手柄检测**：遍历XInput端口0-3，`XInputGetState(i, buf) == 0` 表示已连接

***

### 1.7 [pipeline_logger.py](./pipeline_logger.py) — MAA Pipeline日志

**职责**：继承 `ContextEventSink`，监听Pipeline节点识别/动作事件，输出中文友好日志。唯一例外是**动作失败**：那是「可继续运行的降级」，按本文 §1.5.1 的约定打 WARNING。

***

### 1.8 [opencv_utf8_patch.py](./opencv_utf8_patch.py) — 中文路径补丁

**职责**：Monkey-patch `cv2.imread`/`cv2.imwrite`，支持中文Windows路径。ASCII路径走原生API，中文路径用 `np.frombuffer`/`cv2.imencode`+Python文件IO绕过。程序启动时import一次即全局生效。

***

### 1.9 [wgcap.py](./wgcap.py) — WGC 持久化后台截图

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

- GUI 不提供「截图方式」选择。

- 线程安全：锁内仅交换 Python 引用和整数，NumPy 操作在锁外

- 帧所有权：NativeMappedFrame → bytes → ndarray，回调结束后 ndarray 地址复用但仍安全

***

### 1.10 [nav_graph.py](./nav_graph.py) — 导航图层与真源装配

**职责**：多真源文件载入同一张 MAA 图（多目录 → 临时目录 → 一次 `post_pipeline`，同名文件冲突直接拒绝，`_post_pipeline_merged`）；节点/锚点桥（`MRA_Template` / `MRA_Click`）；policy 数据面装配（`DetectionPlan`）。

**两段真源（v4 文件形态）**：

- **模块段** = `plugins/<id>/resources/pipeline/<id>*.json`（对局图 + 本模块的入口链与页面锚点，按业务域分文件：鉴宝 = `treasure.json` + `treasure.entry.json`）+ `plugins/<id>/resources/policy/<id>.policy.json`（感知/决策数据面）。

- **共用段** = `core/resources/pipeline/*.json`，**当前为空目录不存在**；出现真跨模块链（如"任何模块开工前先回游戏大厅"）时再建。它能承载的只有**无出口的公共锚点**（一份识别规格）与**公共动作节点**（识别 + 动作、不带出口），由各模块在自己的 `next`/`any_of` 里引用；它**不指向任何模块节点**，"回哪个模块"这条边永远由模块自己声明。

**分界线**：这个元素**是否跨玩法长期共享**（爆炸半径）——是则归 `core/`，否则归 `plugins/<id>/`。与"某个玩法是否从这个按钮进入"无关：鉴宝入口卡片是鉴宝专属知识，即使它物理上长在大厅里。

**通电位置（关键契约，不变项）**：

- **图侧原生并入**——多真源文件载入同一张图，模块图可原生引用共用段节点而不必复制一份大厅识别（当前共用段为空，模块图自包含）。

- **检测侧绝不并入**——运行时阶段检测只扫 policy.json `perception.spec` 装配的 `DetectionPlan`（模块自有锚点集），图节点不进每帧检测环。原因：共用段锚点无 `order` → `stage_priority=1000`，一旦进扫描集会在局内帧抢先短路、破坏逐帧等价回归。贴 MAA：首页/入口识别属**导航段**，不进**每帧检测环**。

- **全页面清单的唯一真源 = `<id>.__boot.dwell`**（`recognition.type=Or` 全 stage 信号并集 → `next` 全 dwell 表，`timeout=-1` 未知画面驻留重判）。入口锚点点击后一律 `next: [<id>.__boot.dwell]` 交汇聚重判，不得各自再抄一份清单。

- **版本化用时间表**，不给每个资源挂版本号（`activity_window` / `schedule.json`），与 MAA `activity_pool` 同构。

> 归属由命名空间前缀表达、引用方向红线、"引用"的完整口径与协议为何没有命名空间——权威版见 [MAAFW_GUIDE §5.6](../../docs/MAAFW_GUIDE.md#56-真源组织与分层协议没有命名空间分离只能靠引用方向)；决策与状态见 [ADR-0001](../../docs/adr/0001-分层靠引用方向不靠目录.md)。

**回归护栏（校验器）与编辑入口**：见 [tools/navkit/README.md](../../tools/navkit/README.md)。

***

### 1.11 [ocr.py](./ocr.py) — RapidOCR 文字识别引擎（插件间共享底座）

**职责**：单 ROI 抠图 → 预处理（自适应放大 + 轻度对比度增强）→ RapidOCR 推理 → 交出文本（`OcrText`：逐块 `lines` 与合并 `text` 两种取法）。**抠图边界、引擎调参（关检测/关方向分类/ORT 线程数/绑核）、预处理口径的唯一真源 = 本模块的常量与 docstring**——具体数值不在本文抄录（可机检数值只写指针，见 [docs/README.md](../../docs/README.md)），要改／要核对请直接读 [ocr.py](./ocr.py)。

**为什么住 core**：插件自包含契约（见 `plugins/<id>/__init__.py`）禁止插件互相 import，跨插件复用的引擎只能在公共层落地。**今天的使用者**：`plugins/treasure/ocr.py`（业务薄层：识别区真源 + 金额解析）、`tools/navkit/studio_server.py`（ROI 校准台单区识别）；speedrush 的 HUD 读数将接同一入口。

**分工**：「文本怎么解释」（金额解析、数蛋计数、记分口径）属领域知识，留在各业务层，不上提 core。

**坑点**：

| 坑点 | 说明 |
| --- | --- |
| 关 det 的口径必须保住 | ROI 全是固定 HUD 文字框，关文本检测是性能前提（det 占单 ROI 耗时的绝对大头，开关两个取值差两个数量级）；把它换回默认值会直接打爆每帧读数预算。关方向分类同理。二者**不是可随意调的性能旋钮**，实测依据写在 [ocr.py](./ocr.py) 对应常量注释里 |
| 绑核必须在引擎构造前 | `_pin_to_p_cores()` 只在 ORT 线程池创建之前调用才有意义（worker 线程创建后继承进程亲和性）；引擎已构造再绑核是空动作。失败按 WARNING 降级——曾记 DEBUG 级，导致绑核结论近一个月静默未生效（psutil 当时不在依赖里） |
| 通道序只走 image_io | 本模块对外收 RGB（标准语义），交 RapidOCR 前由 [`image_io.to_bgr`](./image_io.py) 翻一次；不要在 OCR 侧自写切片翻转（通道写反在灰白画面上看不出来） |
| 抠图边界是 int 截断 | x1/y1/x2/y2 一律 `int()` 截断后夹紧到帧内，**不是** [`roi_config.to_pixel`](./roi_config.py) 的 floor/ceil——边界差一个像素就换掉喂给模型的像素，识别文本随之漂移，而下游把读数当稳定值用。边界与语义由 `tests/test_core_ocr.py` 机检 |
| 引擎懒加载 + 失败固化 | `rapidocr` 在函数内导入（模块导入期不拉重依赖）；加载失败置 `_engine_failed`、不再重试，识别返回 None 不抛——调用方按「引擎不可用」降级，不得改成抛异常 |

***

### 1.12 [remote_meta.py](./remote_meta.py) — 远程元数据信任边界

**职责摘要**：判定「远程拿到的公告 / 版本标记字段能不能信、能信到什么程度」，并把「打开外链」
收敛成一张逻辑目标表。不做网络、不碰渲染。

| 出口 | 作用 |
| --- | --- |
| `parse_announcement(data)` | 公告 payload → 归一化 dict；必填字段不合规返回 `None`（整条作废） |
| `parse_release(data)` | 版本标记 payload → `{tag, published_at, download_url}`；tag 不合规返回 `None`（该源不可用） |
| `EXTERNAL_TARGETS` | 外链逻辑目标 → 官方地址（`home` / `issue` / `docs` / `release` / `vigembus`） |
| `is_official_url` / `is_openable_url` | 远程数据可携带的链接白名单 / 交给浏览器前的最终放行判据 |
| `is_date` / `is_expired` | 严格日期判定与过期比较 |

**接法**：`sidecar.check_update` / `fetch_announcement` 的**每个源都过同一个校验器**（不合规即跳过
该源、继续 fallback）；解析通过的地址记进 `SidecarService._remote_urls`，由 `open_external_url`
按目标名 `announcement` / `download` 取用——前端只报「打开哪个目标」，不报「打开哪里」。字段级
规范（必填/可选、长度、域白名单）真源在 [`docs/announcement.md`](../../docs/announcement.md)。

| 坑点 | 说明 |
| --- | --- |
| 必须只依赖标准库 | sidecar 的导入链拉 cv2 / maa / vgamepad，挂在那边的校验逻辑在 Linux CI 上只能整文件 SKIP。本模块保持零重依赖，测试才在 CI 真跑 |
| 内容像 HTML 不是本层的判据 | 本层只管字段形状；防 XSS 靠渲染层 `textContent`。在这里加「过滤尖括号」的假防线，只会掩盖真防线失效 |
| 动作型字段从严、展示型字段从宽 | `url` / `download_url` 一旦被采信就有副作用（打开浏览器），不合规必须清空；`published_at` 格式不对只省略，不牵连整条更新提示 |
| 白名单按路径**段边界**比对 | 纯 `startswith` 会把 `/MaaRacingMaster-evil` 放行；`is_official_url` 用 `== 前缀` 或 `前缀 + "/"` 判定 |
| 仓库 slug 只有一份 | `GITHUB_REPO` 同时喂官方地址表、链接白名单与 sidecar 的更新源 URL——两处各写一遍，改一处漏一处就是白名单静默失效 |

**机检**：[`tests/test_remote_meta.py`](../../tests/test_remote_meta.py)（字段口径 + 仓库实际投放数据）、
[`tests/test_sidecar_external_rpc.py`](../../tests/test_sidecar_external_rpc.py)（端到端：构造出的恶意数据
最终有没有被交给浏览器）、[`tests/test_frontend_remote_render.py`](../../tests/test_frontend_remote_render.py)
（前端渲染路径静态锁）。

***

## 2. 模块依赖与持有关系

### 2.1 导入关系图

```
core/sidecar.py（JSONL RPC handler）
  ├── core.controller.MaaRacingMasterController
  ├── core.logger.logger
  ├── core.window_utils.has_physical_controller
  ├── core.remote_meta（远程元数据校验与外链目标表，§1.12）
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

core/ocr.py（§1.11，插件间共享的识别引擎）
  ├── core.image_io.to_bgr（通道序唯一边界）
  ├── core.logger.logger
  └── rapidocr（函数内延迟导入，不在模块导入期拉重依赖）

core/debug.py / core/debug_io.py
  └── （纯 OpenCV/numpy，IO worker 生产-消费者）

core/window_utils.py
  ├── maa.toolkit.Toolkit
  └── core.logger.logger

core/remote_meta.py（§1.12，只依赖标准库：被 sidecar 引用，但不经它拉任何重依赖）
  └── （无 core 内部依赖）
```

### 2.2 运行时对象持有关系

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

> MAA 对象（`Tasker` / `Resource`）由**活动模块实例**创建并持有，主控不再持有；模块经 `ActivityContext` 窄接口访问 capture / gamepad / debug\_renderer 等能力。拓扑约束（帧从中心缓存来、动作从队列走）见 [ADR-0003](../../docs/adr/0003-运行时拓扑硬约束.md)。

***

## 3. 关键类与函数索引

### 3.1 controller.MaaRacingMasterController

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

### 3.2 yolo_detector.YOLODetector

| 方法                                                 | 说明                                                       |
| -------------------------------------------------- | -------------------------------------------------------- |
| `__init__(model_path, conf, iou)`                  | 初始化ONNX会话（DirectML/CUDA/CPU降级）                           |
| `__call__(img_rgb, roi)`                           | 推理入口：返回(coins, cars, bonus, debug\_dets, all\_raw\_dets) |
| `_nms_per_class(xyxy, scores, classes, mask, ...)` | 按类别分别做NMS，返回原始下标                                         |
| `_to_dets(xyxy, scores, classes, ... indices)`     | 索引转结构化检测结果dict                                           |
| `CLASS_CONF`                                       | 类属性：各类别置信度阈值字典                                           |

### 3.3 debug.NavigationDebugger

| 方法                                 | 说明                     |
| ---------------------------------- | ---------------------- |
| `__init__(proj_dir)`               | 初始化                    |
| `enable_peep()` / `disable_peep()` | 开关 PEEP 预览（置 `peep_enabled`；窗口由 GUI 侧承担）   |
| `start_session(label)`             | 开始一次调试会话（创建存盘子目录）      |
| `save_frame(img, **kwargs)`        | 统一入口：存盘全量绘制 + PEEP精简绘制 |
| `_render_full(img, **kw)`          | 全量标注绘制（存盘用）            |
| `_render_peep(img, **kw)`          | 精简绘制（PEEP用）            |

### 3.4 基础工具方法速查（core 共享）

跨模块高频工具函数，本表统一索引：

| 方法                              | 所属模块                             | 说明                                                     | 关键参数/坑点                                                 |
| ------------------------------- | -------------------------------- | ------------------------------------------------------ | ------------------------------------------------------- |
| `ctx.capture.screenshot()`      | capabilities.py `CaptureAdapter` | 截图 RGB ndarray（**只读 WGC 中心缓存**，无帧返回 None）              | 唯一取帧入口；不得回退同步截图，返回 None 按"采集链路故障"处理，不得当作"画面无变化"         |
| `ctx.lifecycle.sleep(seconds)`  | capabilities.py `LifecycleAdapter` | 可中断睡眠：≤0.1s 分片检查停止信号，**墙钟时长 ≥ 入参**（deadline 分片+余数补齐）    | 曾按 `int(s/0.1)` 量化迭代，小于 0.1s 的入参静默退化为零睡眠忙旋（真机 2026-09-14 饿死导航 worker，见 treasure 域 CODE_WIKI §9）；语义由 `tests/test_capabilities_lifecycle_sleep.py` 机检 |
| `NavigationDebugger(proj_dir)`  | debug.py                         | PEEP 实时预览 / debug 截图标注，支持 template\_rects + detections | §3.3；调试模式说明见 §5                     |
| `has_physical_controller()`     | window\_utils.py                 | XInput API 遍历 4 端口，任一连接返回 True                         | DLL 回退 xinput1\_4 → xinput9\_1\_0 → xinput1\_3；§4.3 坑点 |

***

## 4. 类速查表

| 类名                                   | 文件                                                                                        | 核心职责                                 |
| ------------------------------------ | ----------------------------------------------------------------------------------------- | ------------------------------------ |
| `MaaRacingMasterController`          | core/controller.py                                                                        | 主控编排：能力门面 + 模块生命周期 + 全局设置            |
| `ActivityModule` / `ActivityContext` | core/base.py                                                                              | 模块基类 / 能力门面（窄接口 + ExitStack 生命周期）    |
| `Registry`                           | core/registry.py                                                                          | 插件自动扫描注册（扫 `plugins/*/manifest.py`；含有效期门与自动选中过滤）  |
| `Sidecar`                            | core/sidecar.py                                                                           | JSONL RPC 业务后端（Shell 托管）        |
| `NavigationDebugger`                 | core/debug.py                                                                             | PEEP预览、截图标注（存盘走 debug\_io IO worker） |
| `Logger`                             | core/logger.py                                                                            | 内存+文件双写日志；会话目录 `logs/<ts>/`（日志 + 伴随产物同放）    |
| `RapidOcrEngine` / `OcrText`         | core/ocr.py                                                                               | 单 ROI 文字识别（懒加载 + 失败降级）；逐块/合并两种文本取法（§1.11） |

> 活动域类（`TreasureModule` / `SpeedRushModule` 等）见对应 `plugins/<id>/CODE_WIKI.md`；GUI 壳类见 [apps/MaaRacingMaster.Shell/README.md](../../apps/MaaRacingMaster.Shell/README.md)。

***

## 5. 坑点与调试

### 5.1 系统层

| 坑点                        | 说明                                            | 解决方案                                           |
| ------------------------- | --------------------------------------------- | ---------------------------------------------- |
| 截图需要管理员权限                 | PrintWindow/BitBlt需要提升权限                      | Shell exe manifest 自动 UAC 提权（一次，child 继承） |
| cv2不支持中文路径                | imread/imwrite在中文路径下失败                        | opencv\_utf8\_patch.py monkey-patch            |
| YOLO ONNX 导出              | `onnx.export(simplify=True)` 可能产生损坏模型（推理结果错乱） | 导出时关闭 simplify，或导出后校验精度                        |

### 5.2 光标导航

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
| 丢失判据的读序（自举死锁） | `_phase_p` / `_phase_micro` 都是**先读位置、读到才推摇杆**：读到之前**不发任何手柄输入**（只有丢失收尾的 `stick_zero()` 零幅报告）。若游戏要收到手柄输入才绘制光标 / 切到手柄 UI，模块**无法自举**，且此后每次重试都是**纯重复**（无新输入、无状态改变）——加次数、加看门狗都救不了，该动的是边界的输入来源。真机 2026-09-17 两处标本：会话 `20260917_113520` 的 run #1 三个任务全丢（帧 24/46 与一个在途），手动停止重启后首次点击即成功；`20260916_215905` 的 `bid_confirm_red_btn` 连续十次丢失约二十秒，最终由用户手动确认收尾 |
| 「光标丢失」排查顺序 | 判据唯一入口是导航线程 `read_pos` 的连续读位失败（上限见文件顶部 `MAX_P_MISS`）→ 结果带 `device_lost=True`。排查先排除三条：① 取帧失败（看同一 WGC 缓存是否仍在供给识别器）；② 设备死亡（看设备创建 / 销毁是否与丢失窗口重合）；③ 连续性先验误拒——**可由代码排除**：`select_cursor` 在 `last_pos is None` 或连续失踪达 `MISS_STREAK_RESET` 时**直接接受** top（门槛 `THRESH_SCORE`），而 `miss_streak` 只在 `__init__` 与成功读位时清零、**跨尝试累加**，故「多次尝试全丢」必然发生在先验已旁路之后。余下两支（游戏侧没画 vs 画了但分数低于门槛）的取证口径见 §5.5 |

### 5.3 物理手柄 XInput

- `XInputGetState(i, buf) == 0` 表示第i号物理手柄已连接

- 尝试加载顺序：xinput1\_4.dll → xinput1\_3.dll → xinput9\_1\_0.dll

- 会**接管输入**的运行要求断开所有物理手柄，否则虚拟手柄被游戏忽略或冲突。**是否要求由
  模块的启动约束声明**（`ActivityModule.requires_exclusive_gamepad(config)`）；模块可因
  运行模式而放宽——如"只采集不控车"的录制模式反而需要物理手柄在位

### 5.4 MAA Framework API

API 签名、参数名与高频误用清单的权威版见 [MAAFW\_GUIDE §9「高频红线/坑」](../../docs/MAAFW_GUIDE.md#9-高频红线坑背下来)（完整签名见其 §3.3 与 §6）。本文不复述。

***

### 5.5 日志判据与取证口径

- **产出判据是「稳态重复 vs 状态变化」，不是按级别砍**：级别分布（GUI 默认 INFO 及以上）与「该不该产出」是正交的两件事；降噪改动必须同时验证「原本正常场景仍记」。三件套判据的出处见 §1.5 的产出侧约定，契约锁 `tests/test_log_noise_contract.py`（含反向用例）。

- **比较日志量用「行/帧」，不用总行数**：两场时长不同，总行数比值随时长漂移。真机实测（改前 `20260917_113520` → 改后 `20260917_141507`）总产出降至约五分之一，而 INFO / WARNING 的**每帧**产出基本不变——这才是「砍掉的是重复、不是信号」的判据。

- **`trace.jsonl` 计数必须解析 JSON，不能 grep**：布尔/枚举字段每条记录都带（`device_lost`、`ok` 等），grep 数的是「字段出现次数」而非「为真的次数」（真机 2026-09-17：grep 得 159，解析后实际 2 次）。

- **「光标丢失」的证据面**：丢失按失败链首次必记留痕（带 `原因=光标丢失`），记录里带 `p_frames`（该次点击成功读位的帧数，恒零即从头到尾没读到）。但**判据过程数据尚未落盘**：`last_cands` / `last_cand_sel`（按分数降序的 top-8，含坐标 / 分数 / 状态）每次读位都算好，却只进 `cursor_candidates()` 供 PEEP 叠加层。要定论 §5.2 留下的两支（游戏侧没画 vs 画了但分数低于门槛），需在失败链首次丢失时落一行「种子数 + top3 候选（分数 / 状态 / 中心色 / 半径）+ 取帧失败计数 + `last_pos` / `miss_streak` + 绑定后是否发过手柄输入」，并配一帧存图（复用 [debug.py](./debug.py) 的 `save_frame`）。

***

## 6. 调试操作

1. **DEBUG 存盘模式**：GUI 勾选"DEBUG 每帧截图"，每帧全量标注保存到 `%APPDATA%/MaaRacingMaster/debug/<module>/<会话>/` 目录
2. **PEEP 实时预览**：GUI 勾选"PEEP 实时预览"，显示精简标注画面（~30fps；窗口由 GUI 侧承担，`debug.py` 不开窗口）
3. **断点调试**：GUI 断点列表双击选择起始阶段，跳过前面的导航步骤
4. **MPE Studio**：`tools/navkit/studio.cmd` 起完整工作台（MPE 画布编辑 v4 真源 + ROI 校准台 / 策略表 / 模板截取），见 [tools/navkit/README.md](../../tools/navkit/README.md)

**用户数据目录**（`%APPDATA%/MaaRacingMaster/`，`user_data_dir()` 五目录，[paths.py](./paths.py)）：

- `config/`：`profile.json`（用户偏好）、`maa_option.json`
- `data/`：结构化业务数据（`data/treasure/treasure.db`）
- `logs/`：会话记录 `logs/<MaaRM时间戳>/`——运行日志 `MaaRM_YYYYMMDD_HHMMSS.log`（含 DEBUG 级全量）与伴随产物 `trace.jsonl`（共用「日志记录」开关）
- `framework/`：MAA 框架自产物（`maafw.log`、cache）
- `debug/`：调试截图会话 `debug/<module>/<会话>/`（DebugStudio 契约）

GUI 只显示 INFO 及以上；记录数据（历史 CSV）同随用户数据目录管理。

**独立调试 sidecar**（不经 GUI）：`.venv\Scripts\python.exe -u -m maaracing_master.core.sidecar`（等待 stdin JSONL RPC）。