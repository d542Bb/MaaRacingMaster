# MaaFW 能力与 Python 开发规范

> 用途：本仓库后续开发"类方舟日常"式**离散步骤流程**（新活动、清理日常、页面切换链等）时，
> 直接复用本文档的**可复用范式**，避免从头查文档/从零写代码。
> 信息基于 MaaFramework 官方文档（2.2–4.2 全节，2026-09-10 校核）+ **本项目** **`.venv`** **锁定的
> MaaFw 5.12.3** binding 源码与运行时实测核对——文档站跟随 main（v5.13+），凡行为论断以
> 实测为准（差异处文中显式标注版本口径）。研究底稿：`tools/experiments/maafw-docs-constraints/`。
> 官方全套文档：<https://maafw.com/docs/>（中文）；仓库：<https://github.com/MaaXYZ/MaaFramework>；
> 社区经验信源：MaaHub <https://hub.maafw.com/>（Skills/Customs/Experiences）、
> MaaPracticeBoilerplate、create-maa-project、M9A 仓库内规范。

***

## 1. 核心定位与适用边界（先读）

MaaFramework = 基于图像识别的自动化黑盒框架，核心是 **Pipeline 声明式状态机**（JSON 描述
节点：识别→动作→next）。

**它擅长（适合做成 Pipeline）**：

- 离散步骤：进页面、点按钮、等某个模板/文字出现、按条件分支/跳转

- 固定/可复识的静态 UI 目标

- 秒级节奏、点击型操作

**它不擅长（别硬塞 Pipeline）**：高频实时闭环（连续摇杆控制、15\~30fps 决策）。这类应作为
`CustomAction`（自定义动作）注入，由你自己的代码实时跑，见 §6。

> 本项目结论（详见记忆库 `MaaRacingAssistant迁移MaaFW适配评估`）：
> 实时闭环控制、光标/摇杆导航、YOLO/OCR 推理都是自研实时链路，**保留为 CustomAction / 自研**；
> 新增**离散流水线**（日常清理等）用本文档的范式，直接吃 MaaFW 的 Pipeline。

***

## 2. Python 包结构与导入

`pip install MaaFw`。顶层 `__init__.py` 只负责 `Library.open(...)` 加载底层动态库，所有 API 从子模块导入：

```
mmaa/
├── tasker.py            # Tasker：调度入口
├── resource.py          # Resource：资源/自定义注册
├── controller.py        # Controller：各平台控制器 + post_* 操作
├── context.py           # Context：Custom 内运行时上下文
├── custom_action.py     # CustomAction：自定义动作基类
├── custom_recognition.py# CustomRecognition：自定义识别基类
├── pipeline.py          # JPipelineData/JActionType/JRecognitionType 等数据类
├── buffer.py            # ImageBuffer / RectBuffer / StringBuffer 等
├── event_sink.py        # EventSink / NotificationType
├── job.py               # Job / TaskJob 异步句柄
├── toolkit.py           # Toolkit：设备发现、init_option 等工具
├── define.py            # 枚举/类型（MaaWin32ScreencapMethodEnum 等）
├── library.py           # Library：底层动态库加载
└── agent/agent_server.py# AgentServer（自定义识别/动作的服务端注册）
```

常用示例：`from maa.tasker import Tasker`、`from maa.resource import Resource`、
`from maa.controller import Win32Controller`、`from maa.context import Context`、
`from maa.custom_action import CustomAction`、`from maa.custom_recognition import CustomRecognition`、
`from maa.define import MaaWin32ScreencapMethodEnum`。

***

## 3. 核心对象与真实签名

### 3.1 Tasker（调度器）

```python
from maa.tasker import Tasker

tasker = Tasker()                          # 无参创建自有实例
tasker.bind(resource, controller)          # ★顺序：resource 在前，controller 在后！（红线）
tasker.inited                              # bool，是否正确初始化
tasker.running / tasker.stopping

job = tasker.post_task("入口节点", pipeline_override={})   # -> TaskJob
tasker.post_stop()                         # -> Job，中断当前任务
tasker.override_pipeline(task_id, {...})   # 运行期改某个任务的 pipeline
tasker.add_context_sink(sink) / add_sink(sink) / remove_sink(id) / clear_sinks()
tasker.get_task_detail(task_id) / get_recognition_detail(reco_id)
tasker.get_action_detail(act_id) / get_node_detail(node_id) / get_latest_node(name)
tasker.resource   # 绑定后的 Resource
tasker.controller # 绑定后的 Controller
```

### 3.2 TaskJob / Job（异步句柄）

```python
job = tasker.post_task("入口")
job.wait()                # 阻塞到完成，链式返回自身
status = job.status       # 枚举状态
job.done / job.succeeded / job.failed / job.pending / job.running
result = job.get(wait=True)  # wait 后取结果（TaskDetail，可能 None）
job.override_pipeline({...})# 任务执行中动态改 pipeline
```

规则：所有异步操作都是 `post_*` 返回句柄，用 `.wait()` / `.get()` / `.status` 收尾；
取结果前先 `wait()`（或 `get(wait=True)`），否则可能拿到未完成数据。

### 3.3 Resource（资源）

```python
from maa.resource import Resource
res = Resource()
res.post_bundle("path/to/resource").wait()   # 异步加载资源包（★post_bundle 不是 post_path）
# 除 bundle 整包外，官方还有三个细粒度入口（2.3 资源加载类型枚举；5.12.3 binding 实测齐备）：
res.post_pipeline("pipeline目录或单个json").wait()   # 只注入节点（本项目 v4 真源通路）
res.post_image("image目录或单图").wait()             # 只注入图片
res.post_ocr_model("model目录").wait()               # 只注入 OCR 模型
res.clear()                              # 加载中会失败返回 false —— 重载流程必须先 wait() 再 clear
res.register_custom_recognition("MyReco", inst)  # 注册自定义识别器（红线见 §6 注册命名空间）
res.register_custom_action("MyAction", inst)     # 注册自定义动作
res.override_pipeline({...})                   # 运行时覆盖 pipeline（同名节点=顶级键整体替换）
res.override_next("节点", ["A","B"])           # ★Resource 侧：节点不存在也会创建（与 Context 侧相反，见 §3.5）
res.override_image("img.png", ndarray)         # 覆盖图片数据（官方注"此方法总是成功"）
res.get_node_data("节点") / res.node_list      # node_list 是属性（maafw 5.12.3 实测；旧名 get_node_list() 已不存在）
res.set_inference(...) / set_cpu / set_gpu / set_auto_device   # 推理设备/推理库（旧名 set_option 已不存在）
```

### 3.4 Controller（Win32 常用）

```python
from maa.controller import Win32Controller
from maa.define import MaaWin32ScreencapMethodEnum

ctrl = Win32Controller(
    hWnd=hwnd,
    screencap_method=MaaWin32ScreencapMethodEnum.FramePool,  # 截图方式
    # mouse_method=..., keyboard_method=...                  # 可选输入方式
)
ctrl.post_connection().wait()      # 连接（可多线程+超时守护，防无限阻塞）
ctrl.post_screencap().wait().get() # -> Image（BGR），numpy 取数组 img.numpy()
ctrl.post_click(x, y).wait()       # 点击
ctrl.post_swipe(x1,y1,x2,y2,ms).wait()
ctrl.post_click_key(key).wait()    # 虚拟键码
ctrl.post_touch_down/move/up(contact,x,y,pressure)
ctrl.post_input_text(text) / post_start_app / post_stop_app
ctrl.post_scroll(dx, dy) / post_relative_move(dx, dy)  # Win32 支持
```

截图返回的 `Image`：`img.numpy()` → `np.ndarray`（**BGR** 顺序，OpenCV 默认）。
需要 RGB 时手动 `cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)`。项目里已有 `PostScreencapCapture`
封装好此转换，见 `core/capabilities.py`。

**Win32 平台事实（2.2/2.4 + 5.12.3 实测，与本项目平台层同域）**：

- **截图缩放语义**：截图按目标尺寸设置（`set_screenshot_target_long_side/short_side` /
  `set_screenshot_use_raw_size` / `set_screenshot_resize_method`）缩放；识别与模板坐标
  一律在**缩放后**坐标系。`ctrl.resolution` 是设备**原始**分辨率且**首帧截图后才有效**。
  "缩放坐标→设备坐标"的自动反算原文只写在 Android Native 小节，**不得外推为对
  CustomController 也成立**——自研控制器通路必须自证坐标域一致。

- **后台可用截图只有两种**：`FramePool(2)` / `PrintWindow(16)`；且二者内置**伪最小化**
  ——目标窗口最小化时会被改写扩展样式与不透明度（透明+点击穿透）来恢复渲染。
  与本项目的窗口隐藏/置顶/WGC 遮罩是**同一批窗口状态的写入方，混用即竞态**。

- **`post_inactive()`** **被 Tasker 自动调用**：所有任务链执行完毕后框架自动 inactive
  （Win32 侧恢复置顶/解除输入阻塞/还原光标与窗口位置）。自研收尾逻辑与其语义要显式
  定案谁负责，避免双写。

- **`post_relative_move`（Win32）有前置条件**：必须先 `set_mouse_lock_follow(True)`，
  且仅 MessageInput 系列输入方式可用。自研 XInput/ViGEm 通路不在 MaaFW 语义内，
  要经 pipeline 节点复用只能在 `CustomController` 内自己实现。

- **`set_background_managed_keys`**：声明后 pipeline 的 `ClickKey/LongPressKey/KeyDown/KeyUp`
  对命中键自动改走后台守护路径（仅 Win32）。用了它，按键动作语义会变。

- **Gamepad 官方编码**（与本项目 ViGEm 链路对齐用）：数字键 `MaaGamepadButton_*`
  （A=4096/B=8192/X=16384/Y=32768/LB=256/RB=512/DPAD 1/2/4/8…）；摇杆 =
  `touch_down/move/up` + `contact 0`(左)/`1`(右)，x/y ∈ ±32768；扳机 = `contact 2`(LT)/
  `3`(RT)，pressure 0\~255。

- 5.12.3 **无** Expand 截图选项（`set_screenshot_target_expand` 是 5.13+）；
  需要 Expand 语义（`scale = max(w/原生w, h/原生h)`）须升级或在 CustomController 内自实现。

### 3.5 Context（Custom 内运行时）

Custom 代码里拿到的 `context` 提供执行/覆盖能力：

```python
context.tasker                     # 当前 Tasker（有 .controller / .resource）
context.run_task("入口", override)    # 同步跑一个子任务 -> TaskDetail|None
context.run_recognition("节点", image, override)  # 只识别不执行 -> RecognitionDetail|None，用 .hit 判命中
context.run_action("节点", box, reco_detail, override)  # 只动作 -> ActionDetail|None，用 .success
context.override_pipeline({...}) / context.override_next(节点, 列表) / context.override_image(name, img)
context.get_node_data(节点) / context.get_node_object(节点)
context.set_anchor(name, node) / context.get_anchor(name)
context.get_hit_count(node) / context.clear_hit_count(node)
context.wait_freezes(time_ms, ...) # 等待画面静止
context.get_task_job() / context.clone()  # clone 可复制上下文做分支
```

Context 侧要点（2.2 实测口径）：

- **`context.override_next`** **与 Resource 侧语义相反**：节点**不存在 → 返回 false**（不创建）。
  运行时改路由拼错节点名，这里的失败方式是静默 false，不是凭空造节点（§3.3）。

- `run_recognition` / `run_recognition_direct` **不执行动作、不执行 next**；
  `run_action` / `run_action_direct` **不执行 next**；`run_task` **同步**执行、失败返回 InvalidId。
  这是 Custom 内部"借用官方识别器"的安全边界。

- `wait_freezes(time, wait_freezes_param)`：`time` 与 `param.time` **互斥**——不能同时非零、
  也不能同时为零（binding 层是否强校验未文档化，两参只给一个）。

- 对象的 `is` 比较不可靠：`context.tasker` / `tasker.resource` 按 4.2 封装约定可能每次返回
  新包装对象，判同用身份属性（如 handle）而非 `is`。

***

## 4. 三种集成范式（按需选）

### 范式一：纯 JSON 低代码（简单离散流）

只写 `resource/pipeline/*.json`，节点字段 `recognition/action/next`。零代码。

### 范式二：JSON + 自定义扩展（推荐，官方主推）

核心流程用 Pipeline JSON 声明（可视化、可调试），复杂逻辑放进 `CustomRecognition`/`CustomAction`。
**本项目新功能默认走这个范式。**

```jsonc
// resource/pipeline/daily.json
{
  "日常清理": {
    "next": ["进入活动页", "领体力", "弹窗关闭"]
  },
  "进入活动页": {
    "recognition": "TemplateMatch",
    "template": "activity_btn.png",   // resource/image/ 下
    "action": "Click",
    "next": ["等加载完成"]
  },
  "等加载完成": {
    "recognition": "OCR",
    "expected": "加载完成",
    "timeout": 10000,
    "next": ["领体力"]
  },
  "领体力": {
    "action": "Custom",
    "custom_action": "MyCollect"       // 复杂逻辑放这里
  }
}
```

```python
from maa.resource import Resource
from maa.tasker import Tasker
from myModule import MyCollect          # class MyCollect(CustomAction): ...

res = Resource()
res.register_custom_action("MyCollect", MyCollect())
res.post_bundle("resource").wait()

tasker = Tasker()
tasker.bind(res, controller)
tasker.post_task("日常清理").wait()
```

### 范式三：全代码（不推荐作默认）

直接 `controller.post_click(...)` + `tasker.post_task(...)` 手写流程。灵活但失去 JSON
可视化/调试器/通用 UI 生态。仅用于深度定制或宿主 UI 编排。本项目 `controller.py` 就属于这种，
作为自定义层保留，新离散流程不要一律走这种。

***

## 5. 离散流水线开发规范（"方舟日常式"新功能模板）

> 新增活动/日常等**离散步骤流**时，按此模板落地，Quality 高且可维护。

### 5.1 目录结构

```
你的插件资源/<活动>/resource/
├── image/            # 模板/特征图（从 720p 无损原图裁剪，勿随意缩放）
│   ├── main.png
│   └── btn_ok.png
├── model/ocr/        # OCR 模型（可选；需 det.onnx + keys.txt + rec.onnx）
│   └── ...
├── pipeline/         # 任务定义，递归读取所有 json
│   └── main.json
└── interface.json    # 供通用 UI 描述资源/任务（开放给 GUI 时写）
```

### 5.2 JSON 节点书写规范（血泪要点）

**执行语义（先记牢，最容易误判的四条）**：

- 节点**不"识别自己再执行"**——命中发生在**父节点**对 next 列表的识别轮里；
  `timeout`/`on_error` 是"上一节点等我的时间"。**要调当前节点的识别等待，改的是上一节点
  的** **`timeout`**。

- 每轮识别前才截图（`post_delay` 之后），识别到的一定是新帧。

- next **顺序检测、命中即中断** ⇒ 数组顺序就是优先级：优先抢占的（哪怕命中频率低的弹窗）
  排前面，避免"主界面也能匹配、弹窗永远轮不到"。

- **`rate_limit` 管不住 `jump_back` 回弹闭环**（5.12.3 实测，`tools/experiments/v4-frame-pacing/`）：
  `rate_limit` 的作用域是「节点自己等后继命中的那段轮询」，`pre/post_delay` 只在**进入该节点执行时**
  付一次；一旦下一跳命中了挂 `jump_back` 的兜底节点，父节点的 `rate_limit`/`pre_delay`/`post_delay`
  **全部旁路**——实测把父 `rate_limit` 设 50 / 600 / 2000、`pre/post_delay` 设 0 / 200 / 500 任意组合，
  相邻决策帧间隔恒等于「兜底节点动作自身耗时 + ≈3.5ms 框架开销」；动作耗时归零即飙到 **291 次/秒**，
  而把动作耗时设为 105ms 时三组限速档读数一字不差（108.9ms）。同图改成"next 全 miss 驻留"（不走回弹）
  则间隔立刻精确托住 62 / 606 / ≈2000ms，证明 `rate_limit` 本身工作正常。
  ⇒ 两条结论：**① 想改"每帧重判"的节拍，改图上的 `rate_limit` 是无效操作，必须在决策段按时间戳自节流；
  ② 决策段一旦变轻（少跑一轮 OCR 之类），回弹闭环会自动放大成几百 Hz 空转——CPU、trace 落盘、决策契约
  一起被打爆，这是结构性风险而非偶发故障。**

**字段与默认值对表（3.1 权威值，勿靠记忆）**：

| 字段                                           | 默认                           | 备注                                                |
| -------------------------------------------- | ---------------------------- | ------------------------------------------------- |
| `recognition` / `action`                     | DirectHit / DoNothing        | 常用 `TemplateMatch`/`OCR`；`Click`/`Swipe`/`Custom` |
| `rate_limit`                                 | **1000ms**                   | 每轮识别最低消耗，不足则 sleep                                |
| `timeout`                                    | **20000ms**                  | `-1`=永不超时                                         |
| `pre_delay` / `post_delay`                   | **200ms**                    | 不需要时**显式写 0**，省略字段=吃隐式等待                          |
| `pre/post/repeat_wait_freezes`               | 0                            | 对象参默认 `threshold=0.95`、`method=5`                 |
| `repeat` / `max_hit` / `enabled` / `inverse` | 1 / UINT\_MAX / true / false | repeat 中单次动作失败不中止，以最后一次为准                         |
| `attach`                                     | {}                           | **节点元数据唯一官方位**（下）                                 |

- `roi`(在哪儿找) / `box`(找到了哪儿) / `target`(对哪儿动手) 三分，各有 `*_offset`；
  `target` 默认 `true`=用命中框，也可填节点名/`[Anchor]名`/坐标。

- `inverse=true` 时"点击自身"失效（实际没识别到东西），必须显式 `target`。

- `[JumpBack]`（替代已废弃 is\_sub/interrupt）= 状态回退原语；**错误处理路径（on\_error）
  不回跳**（v5.9 起，5.12.3 已含）。`[Anchor]` = 运行时才定的"上一站"，官方自评
  "类似给 pipeline 引入变量机制"。

- `next` 列表要**覆盖操作后所有可能画面**，争取一次命中；`on_error` 是兜底不是重试机。

**元数据与目录规则（本项目定案形态）**：

- 节点级附加配置**只放** **`attach`**（3.1 唯一文档化扩展位；dict merge、框架保留、
  `get_node_data` 可回读）。本项目节点元数据（`_entry/_dwell/_signals/_stage/_page/_label` 等）
  统一承载于 `attach` 内，键名保留 `_` 前缀标识项目私有语义，由 `check_truth` 机检引用闭合。
  **实测教训**：顶层 `_xxx` 键会被框架解析器**静默丢弃**（5.12.3 probe 实锤）——放错位置
  的元数据对经框架接口读数据的工具（MPE 高级用法、自研 Inspector）是不可见的。

- `.` 开头的目录/JSON **不读取**、root 级 `$` 开头字段**不解析**（官方逃生位：临时片段、
  文件级元数据）；必选字段可留空由接口注入（"真源 + override"合法）。

- `default_pipeline.json`：放 **Bundle 根**（与 `pipeline/` 同级）；默认值在**节点首次加载时
  冻结**，多 Bundle 后到的 default **不影响已加载节点** ⇒ 多目录真源必须**合并单次 post**
  （本项目 `_post_pipeline_merged` 的协议依据），避免"加载顺序决定运行参数"。

- 同名节点合并 = **顶级键整体替换**（数组不逐元素合并），`attach` 是唯一 dict merge 例外。

- **少用硬 delay，多用中间过程节点**（官方原话"不然既慢还不稳定"）；失败要重试先定位
  哪个节点哪次识别错，绝不盲目加重试。

### 5.3 识别器选型决策表（协议清单 + 生态实战倾向）

`DirectHit | TemplateMatch | FeatureMatch | ColorMatch | OCR | NeuralNetworkClassify |
NeuralNetworkDetect | And | Or | Custom`。

| 场景               | 选什么                                                                           | 关键参数与坑                                                                                          |
| ---------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| 找按钮/图标           | `TemplateMatch`（默认选择）                                                         | `method` 默认 5=TM\_CCOEFF\_NORMED（官方"推荐"）；模板必须 720p 无损原图裁剪；`threshold` 默认 0.7，数组长度须与 template 一致 |
| 抗透视/尺寸变化         | `FeatureMatch`                                                                | 模板 ≥64×64、纹理足、避免重复条纹；detector 默认 SIFT（最准最慢），ORB 无尺度不变性                                          |
| 找色/状态灯           | `ColorMatch`，优先 **HSV(40) 或 GRAY(6)**                                         | 社区明确不推荐直接 RGB(4)（显卡渲染差异）；`connected=true` 只数相连最大块                                               |
| 读文字/数字           | `OCR`                                                                         | `expected` 支持正则但要**写完整文本而非片段**；混淆字用 `replace`；`only_rec` 需精确 `roi`                              |
| 彩色/复杂背景文字        | `OCR` + `color_filter`(v5.8) 或「`ColorMatch` 定色域→`only_rec` 精识别」组合             | 设了 `color_filter` 的 OCR 节点**不参与 batch 优化**（帧预算敏感场景注意）                                           |
| 固定位置类别判定 vs 找目标框 | `NeuralNetworkClassify`（位置固定判类别）vs `NeuralNetworkDetect`（找目标，YOLOv8/v11 ONNX） | Detect 的 `expected` 在 5.12.3 只承诺**整数下标**；labels 自动从模型 metadata 读                                |
| 多条件到站判定          | `And`/`Or`(v5.3)：`all_of`/`any_of` 可内联或按节点名引用(v5.7)                           | `box_index` 决定输出哪个子框；`sub_name` 让后续子识别以前一子结果为 ROI                                               |
| 同屏多目标取哪个         | `order_by`(Horizontal/Vertical/Score/Area/Length/Random/Expected) + `index`   | `index` 支持负数（Python 规则），越界=无结果                                                                  |
| 内置组合仍表达不了        | 才 `Custom`（见 §5.4/§6）                                                         | 官方判据：逻辑复杂到 JSON 堆不动、或涉及算法；能用 JSON 不用 Custom                                                     |

### 5.4 何时用 Custom、何时纯 JSON

- 纯"点已知按钮/等已知文字"→ 纯 JSON。

- 需要"复杂判断、算法、跨帧状态、自定义计算"→ `CustomRecognition`（识别）+ `CustomAction`（动作）。

- 实时高频控制 → 只许用 `CustomAction`，别拆成 Pipeline 节点。

- 社区共识（M9A/脚手架同调）：**跨页面流程优先纯 JSON 状态机，不要写 Python 编排**
  （自己 for/while 调 run\_task 模拟状态机）；只有复杂运行时分支才用 Flag 节点 + CustomAction。

### 5.5 命名与结构规范（未来新条目立规；定案于 2026-09 生态调研）

生态实证结论：**命名风格无协议约束，"项目内一致"即可**（boilerplate 用 snake\_case
Custom 名，M9A 用 PascalCase，MAA 用中文名）。据此本项目定案：

- **存量冻结**：现有节点名/图名（中文 + snake\_case 混合、`<模块>.<名>` 命名空间前缀）
  不改——重命名波及 next 引用、policy anchors、trace 历史，零行为收益。

- **节点名（新）**：沿用所在真源文件的既有风格，**前缀 = 该节点的归属命名空间**
  （`<插件id>.`；`global.` 仅留给真跨模块共用链，当前项目无此类链，见 §5.6）；
  纯内部工具节点用 `__` 前缀 + 不写 next（`[JumpBack]` 目标形态）；同一基锚的链复制节点
  用 `.r<route>.<n>` 后缀（现行约定，`_base_name` 机检依赖此形态）。

- **Custom 注册名（新）**：与 pipeline 中 `custom_recognition`/`custom_action` 字段值
  **逐字相同**（大小写敏感）；本项目用 `MaaRM_*` 命名空间前缀防止与未来 Agent 组件撞名。

- **模板图（新）**：与节点语义同名（`.png`）、放所属插件 `resources/image/`；
  一个 pipeline JSON 对应一组图（M9A 惯例）；必须 720p 无损原图裁剪；路径分隔符一律正斜杠。

- **attach 元数据键（新）**：`_` 前缀 + snake\_case 语义名（如 `_dwell/_signals`）；
  新键必须同时有消费方（check\_truth 或编译层），**禁止只写不读的键**（防注释腐化成假契约）；
  协议键（`next/recognition/...`）永不进 attach。

- **policy/数据面 JSON 不在 pipeline 协议管辖内**（框架不解析它们），其 `_` 前缀注释字段
  （如 `_schema_ver`、`_v3` 随行注记）不受 attach 规则约束，维持现状。

### 5.6 真源组织与分层（协议没有命名空间，分离只能靠引用方向）

**协议事实**（一手：1.2 术语解释、3.1 任务流水线协议）：Pipeline = 一个 `pipeline`
目录内 Node 全体；Resource = 多个 Bundle **按序加载**；跨文件连接靠 `next`/`on_error`
里的**节点名字符串**。→ 节点名**全城唯一、没有命名空间**，所有 Bundle 汇成一张扁平图。

三条直接推论：

1. **文件摆放不产生隔离**。把模块的东西放进 `core/` 目录，运行时它照样在同一个
   命名空间里被所有图看见。想靠目录"分开"是幻觉（MPE 为此自己造了文件级前缀）。
2. **一旦互指，必须合并单次 post**。`post_pipeline` 时 C++ PipelineChecker 即校验
   引用闭合，分次 post 时先加载者必失败。本项目由 `nav_graph._post_pipeline_merged`
   承担（多目录→临时目录→一次 post，同名文件冲突直接拒绝）。
3. **所以"core/plugin 分离"的唯一可实现形态 = 引用方向单向**。本项目定案红线两条，
   由 `check_truth.namespace_checks`（校验器第 7 条）机检：

   - core 真源**不得占用** `<module>.` 前缀（模块命名空间由 `plugins/*/module.py` 自动发现）；

   - core 真源的 `next`/`on_error` **不得引用** `<module>.` 节点。
     合法方向只有一个：**业务层引用通用层锚点**。通用层要"知道"业务层，唯一允许的
     接口是各模块的**汇聚入口**（`<id>.__boot.dwell` 这类），不是模块内部页面。

**过早抽象同样是债**（本节起因，2026-09-10 归位）：`core/resources/pipeline/global.json`
曾以"大厅骨架"名义装着 7 个节点，其中 6 个带 `_owner: treasure`、模板图是鉴宝入口
卡片，并抄了 `treasure.__boot.dwell` 已有的 13 条全页面清单两遍——**只有一个实现时
抽出来的"公共层"，其内容必然全是那个实现的私货**。判据：抽公共层前先回答"第二个
使用者是谁、今天是否真实存在"；答不出就不抽（目录可以不存在，加载路径按存在性纳入）。

**生态怎么做大规模真源**（一手：MaaAssistantArknights `dev-v2` 源码与协议文档）：

- **拆分依据是业务域，不是行数**：M9A（官方点名的最佳实践）= `base/pipeline/<域>/<功能>.json`；
  MAA 单文件 3529 行不强拆，`Roguelike/BlackFlow.json` 261 节点。

- **地狱决策不住 pipeline**：页面导航归图，策略归**声明式领域协议 JSON**
  （copilot / sss / roguelike `strategy.json` + `node_execution.json` / base-scheduling，
  各带 `schema_version`），搜索与时间轴归 C++；**规划器的输出仍是 pipeline 任务名**——
  这是两套真源不交叉的关键。

- **"什么该出 pipeline"六条判据**（从 MAA 落点反推，命中任一即出）：① 跨节点状态/持久
  记忆；② 约束求解或带预算的搜索；③ 优化目标；④ 帧/毫秒级实时性；⑤ 递归+预算核算；
  ⑥ 需解析非界面数据。留在图里的只有：识别与跳转、线性流程、错误回退、重试/次数上限、
  模板继承复用、跨服资源叠加。

- **协议自带的复用/变体位，优先于自研**：`baseTask` + `@ # * + ^` 组合代数（继承与
  列表合并/差集）、`interface.json` 的 `resource.path` 数组（基座 Bundle + 差分 Bundle
  叠加）、`pipeline_override`（节点级补丁 + `{占位}` 参数拼接）、`default_pipeline.json`
  （参数下沉，四级优先级）、`.` 前缀文件不加载 / `$` 前缀 root field 不解析（片段与
  工具元数据逃生位）、`[Anchor]`/`[JumpBack]`/`enabled`/`max_hit`/And-Or 按名引用子条件。

**协议形态：v1 平铺 vs v2 归一（本项目 2026-09-10 起统一 v2）**

一手口径（[3.1 协议](https://github.com/MaaXYZ/MaaFramework/blob/main/docs/zh_cn/3.1-%E4%BB%BB%E5%8A%A1%E6%B5%81%E6%B0%B4%E7%BA%BF%E5%8D%8F%E8%AE%AE.md) + [PipelineParser.cpp](https://github.com/MaaXYZ/MaaFramework/blob/main/source/MaaFramework/Resource/PipelineParser.cpp)）：

- v2 自 **v4.4.0** 起支持，官方原文"**同时兼容 v1**"，文档里 v2 一节写的是"可选 v2 格式（**与 v1 等效**）"；文档字段全集仍以 v1 叙述。

- **v1 未被废弃**：解析器唯一的废弃硬报错是 `is_sub`/`interrupt`（v5.1），与形态无关。

- **新字段不是 v2 独占**：`param_input = input`（v1）或 `reco_opt->get("param", *reco_opt)`（v2）之后**同一套** `parse_*_param`；节点级通用字段（`attach`/`anchor`/`max_hit`/`repeat*`/`focus`）从顶层直读，与形态无关。

- **但 v2 是框架的内部规范形**：`get_node_data`（官方 PipelineDumper）**只吐 v2** 并补齐默认值（实测 5.12.3：v1 写的 `policy_loop` 回读成 `{'action': {'param': {...,'target': True,'target_offset': [...]}, 'type': 'Custom'}, 'enabled': True, 'max_hit': 4294967295, ...}`）；MPE 保存亦产 v2。→ 生态真实分工是"**人写侧默认 v1、机器产出侧默认 v2**"，不是新旧交替。

字段对照（读官方文档/sample 时用得上）：

| v1 平铺                                                                         | v2 归一                                                                                            |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `"recognition": "TemplateMatch"` + 顶层 `template`/`roi`/`threshold`            | `"recognition": {"type":"TemplateMatch","param":{"template":…,"roi":…,"threshold":…}}`           |
| `"recognition": "Custom"` + `custom_recognition` + `custom_recognition_param` | `"recognition": {"type":"Custom","param":{"custom_recognition":…,"custom_recognition_param":…}}` |
| `"action": "DoNothing"`                                                       | 可整键省略（框架默认 `DoNothing`）或 `{"type":"DoNothing","param":{}}`                                       |
| —                                                                             | `param` 键本身可省略：v2 里 parser 会退回用 `recognition` 对象当参数容器                                            |

**形态风险点不在新字段，在 And/Or 子项**：官方 PipelineDumper 曾产出自己 parser 读不回来的子项形（`failed to parse sub recognition in 'all_of'`，maafw 5.10.0b2，样本 1364 task 中 116 个≈8.5% 受影响），由 [issue #1314](https://github.com/MaaXYZ/MaaFramework/issues/1314) → [PR #1423](https://github.com/MaaXYZ/MaaFramework/pull/1423) 修复。本项目有 7 个 `Or` 节点（起跑汇聚 + 回合 dwell），升 MaaFw 版本时**必须重跑** **`tools/experiments/ecosystem-audit/load_truth.py`**（含 `get_node_data` 回读对照）而不是只看加载成功。未取证项：v1 平铺下 And/Or 子项的完整合法写法（官方"算法类型"章节原文未取到）。

***

## 6. Custom 开发契约（真实签名）

### 自定义识别 CustomRecognition

```python
from maa.custom_recognition import CustomRecognition
from maa.context import Context

class MyReco(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # argv.image 是 BGR ndarray；argv.roi 是识别区；argv.custom_recognition_param 是自定义 JSON 字符串
        box = (x, y, w, h)                      # 命中框；None=未命中
        detail = {"my_key": "value"}            # 记录进识别结果
        return CustomRecognition.AnalyzeResult(box=box, detail=detail)
        # 也可以直接 return box(4元组/None)；None 表示未识别到
```

注册：`res.register_custom_recognition("MyReco", MyReco())`；Pipeline 里
`"recognition": "Custom", "custom_recognition": "MyReco"`。
注意 `image` 是 **BGR**，项目内部如要 RGB 需自己转。

### 自定义动作 CustomAction

```python
from maa.custom_action import CustomAction
from maa.context import Context

class MyAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # argv.box 是前序识别命中框；argv.custom_action_param 是自定义 JSON 字符串
        context.tasker.controller.post_click(100, 100).wait()
        return True          # 返回 bool / RunResult(success=bool) / None(视为 True)
```

注册：`res.register_custom_action("MyAction", MyAction())`；Pipeline 里
`"action": "Custom", "custom_action": "MyAction"`。
项目现有示例见 `core/nav_graph.py` 的 `ClickAction`（继承 `CustomAction`，经 `register_custom_action` 注册）。

**注册命名空间（文档口径 vs 5.12.3 实测，实验见** **`tools/experiments/maafw-docs-constraints/`）**：

| 行为                    | v5.13 文档口径         | **5.12.3 实测（本项目在用）**              |
| --------------------- | ------------------ | --------------------------------- |
| 识别器同名二次注册             | 返回 false，**保留旧注册** | 返回 True，**新实现直接覆盖生效**（热重载安全）      |
| 识别与动作跨类型同名            | 共享命名空间，互斥          | 各自注册成功，可并存                        |
| 空名注册                  | 返回 false           | **返回 True 但 C 侧拒绝**（只打日志）——返回值不可信 |
| 覆盖后旧实例被 GC            | （潜在悬垂）             | 稳定命中新实现，无悬垂                       |
| unregister → register | 生效                 | 生效；unregister 不存在的名也返回 True（幂等）   |

**防御性纪律（跨版本安全，两版行为下都正确）**：

1. **替换注册一律先** **`unregister_custom_*`** **再** **`register_custom_*`**，不依赖同名覆盖——
   升级 MaaFw 前重跑 `v1_duplicate_register.py` 校准。
2. 注册名必须是非空字面量/常量（空名失败不经返回值暴露）；注册名与 pipeline 引用名的
   一致性由 `check_truth` 机检兜底。
3. binding 的 `_custom_{action,recognition}_holder` 负责防 GC（4.2 约定）——**不要**绕过
   binding 直接 ctypes 注册，也不要注册后丢弃实例假定安全。
4. Custom 动作要拿"任意区域"用节点 `target`/`target_offset` 声明（会以 `box` 传给
   `run()`），不自造字段；`custom_*_param` 是 *any* 黑盒透传（本项目 policy 桥即 `table` 引用形）。
5. 将来若把 Custom 逻辑挪进 **AgentServer 子进程**：注册走 `AgentServer.register_custom_*`

   - `RegisterResourceSink` 等回流通道，且必须遵守 PI v2.5.0 的 `PI_*` 环境变量约定
     （子进程做容错、勿假定全部存在；`child_exec` 的 CWD = interface.json 所在目录）。

***

## 7. 调试与诊断

- `Toolkit.init_option(path)`（maafw 5.12.3 起第二参可省，binding 把 `None` 归一为 `{}`；旧版 binding 需显式传空串 `""`）读取/生成 `config/maa_option.json`：
  `logging`(存 maafw\.log)、`save_draw`(存识别可视化到 vision/)、`stdout_level`(0无\~7全)、
  `save_on_error`(失败存图)、`draw_quality`。

- Tasker 全局选项可设 `DebugMode`(所有任务当 focus 产生回调，RecoDetail 含 raw/draws)。
  `raw`/`draws` **只在 DebugMode/SaveDraw 下可得**——诊断代码不得假设 RecoDetail 一定有 draws。
  高频流水线留意 `RecoImageCacheLimit`（识别图像缓存，默认 4096 帧）。

- **回调四条纪律（2.3 原文，对** **`pipeline_logger.py`** **这类 Sink 直接生效）**：
  ① `details_json` 保证是合法 JSON（但消息族会增，**必须容忍未知 message 类型**）；
  ② 回调**可能来自不同线程**；③ 必须**尽快返回**（重活转投自己的队列，不阻塞框架）；
  ④ 必须**自吞异常**（防影响框架运行）。GUI 从回调直接刷 WebView2 属原文明确警告的模式。

- 回调消息族速查（`add_sink`/`add_context_sink`）：`Resource.Loading.*`（type=
  Bundle/OcrModel/Pipeline/Image）、`Controller.Action.*`、`Tasker.Task.*`、
  `Node.NextList.*`（含 jump\_back/anchor 标记）、`Node.{Recognition,Action,WaitFreezes}.*`、
  `Node.{PipelineNode,RecognitionNode,ActionNode}.*`（分别对应 post/run\_task、run\_recognition、
  run\_action 三条通路）。**只有配了** **`focus`** **的节点（或开 DebugMode）才产** **`Node.*`** **细粒度回调**；
  2.3 示例里的 `MaaTaskerAddNodeSink` 是遗留写法，实际只有 `add_sink`/`add_context_sink`。

- 监听日志：`tasker.add_context_sink(PipelineLogger类)`（项目已有 `core/pipeline_logger.py`，
  其实现须满足上文回调四条纪律）。

- 排障默认顺序（社区共识）：按任务链逐步比对（一次只改一处）→ 先看 override 参数错误
  （列表/阈值长度不一致在 post 阶段就报 PipelineParser ERR）→ 再查缺模板/text 错/roi 偏，
  用 `save_draw` + 失败截图 + 日志交叉验证。

- 生态工具：MaaDebugger(Pipeline 调试器)、VSCode 插件(maa-support，含截图裁剪素材与
  Agent socket 调试)、MaaPipelineEditor(可视化)、MaaCommonAssets(预转 OCR 模型)、
  MaaPracticeBoilerplate(空模板脚手架)。

- **文档口径纪律**：官方文档站跟随 main（现 v5.13+），引用任何"框架行为"论断前，
  先对**当前安装版本**实测（V-1 实验即反例：文档"重名返回 false 保留旧注册"在 5.12.3
  行为相反）。binding 与 C API 表面对不上时按 4.2 封装原则理解（SetOption 拆独立方法、
  Job 封装异步 id），**不要**绕过 binding 直接 ctypes。

***

## 8. 本项目可复用结论（速查）

| 事项                        | 结论                                                     |
| ------------------------- | ------------------------------------------------------ |
| 截图（Win32 FramePool / WGC） | 保留现有（含 `PostScreencapCapture` RGB 封装）                  |
| 实时控制 / 光标导航 / YOLO / OCR  | 自研，保留为 CustomAction / 自研引擎，**勿迁**                      |
| 新增离散流水线（日常、活动代刷）          | **用本文档 §5 范式**，JSON + Custom                           |
| 自研识别若要进 Pipeline          | 包层 `CustomRecognition` 壳（§6），不动算法                      |
| 通用 GUI / 可视化调试            | 需要时写 `interface.json`（清单见 §8.1），接通用 UI 生态              |
| 节点元数据承载位                  | **只放** **`attach`**（§5.2 定案；顶层 `_xxx` 被框架丢弃）           |
| 目录结构定位                    | 本项目是"宿主 App 内嵌 MaaFW"形态（非 Bundle 分发），资源布局自洽即可，PI 属对外契约 |

### 8.1 interface.json（PI v2）补写时的验收清单（定案：暂不建，用到再落）

3.2（PI v1）**已废弃**，将来只能按 3.3 写。硬条目：

- `interface_version` **固定为 2 且必须设置**；`name` = 永不改的 ID（kebab-case），展示走
  `label`（`$` 前缀 i18n 键）。

- `resource.path` 是**数组依次加载、后者覆盖前者**，且**不得直指 pipeline 目录**——指 Bundle 根
  （image/model/pipeline + default\_pipeline.json 一并生效）。本项目运行期走
  `post_pipeline(<pipeline 目录>)` 细粒度注入，两条通路语义不同，混用前确认模板/模型由哪次加载提供。

- `task.entry` = pipeline 起点节点名；`task[].resource/controller`、`option` 键等**引用一律用 name**；
  `pipeline_override` 结构须与 pipeline JSON 完全一致（含任务名层）。

- option 合并优先级 `task > controller > resource > global_option`；不满足当前 controller/resource
  条件的 option **不得产生 override**。已记录的坑：override 把 `expected/template` 列表改短时，
  继承的 `threshold` 长度不匹配 → post 直接失败——override 要么只改目标不改数量，要么显式带等长 threshold。

- Agent 子进程（若有）：`child_exec` CWD = interface.json 同目录；`PI_*` 环境变量八项做容错。

- `input.password` 类字段强制加密存储、禁入日志/遥测/preset；`welcome` 公告为 Markdown。

- 更新边界（社区惨痛教训）：通用 UI 只更新本 release 整体，**不提供单独升 UI/MaaFW**；
  发布以 git tag 为准（与本项目"tag 唯一版本信源"同向）。

***

## 9. 高频红线/坑（背下来）

1. `Tasker.bind(resource, controller)` — **resource 在前**。
2. `Resource.post_bundle(path)` — 是 `post_bundle`，**不是** `post_path`；细粒度还有
   `post_pipeline/post_image/post_ocr_model` 三个官方入口（本项目 v4 用前者）；
   `Resource.clear()` 在加载中会失败——重载先 `wait()`。
3. `Toolkit.init_option(path)` — 5.12.3 起第二参可省（binding 源码 `None→{}` 后必传 JSON 串）；低于 5.12 的旧 binding 必须显式传空串 `""`。
4. `Win32Controller(hWnd=hwnd, ...)` — 参数名**驼峰** **`hWnd`**。
5. 截图返回 `Image`，`img.numpy()` 是 **BGR**；要 RGB 手动转。
6. `post_*` 都是异步 → 用 `.wait()` / `.get()`；取结果前先 `wait()`。
7. `CustomRecognition.image` 是 BGR；返回 4 元组/`None`/`AnalyzeResult` 三选一。
8. `robot`/template 图需 720p 无损原图裁剪。
9. 新功能默认走"范式二 JSON + Custom"（官方推荐），全代码只做宿主编排。
10. `Toolkit.find_desktop_windows()` 返回 `DesktopWindow` 对象列表，属性为 `hwnd` / `class_name` / `window_name`（下划线命名；用法见 `core/window_utils.py`）。
11. **节点元数据只放** **`attach`**——节点顶层 `_xxx` 会被框架解析器静默丢弃（5.12.3 实测）；
    文件根级 `$` 前缀字段不被解析、`.` 开头目录/文件不被读取（可作安全的非加载片段位）。
12. **`override_next`** **两侧语义相反**：Resource 侧"节点不存在也创建"，Context 侧
    "节点不存在返回 false"。运行时改路由先想清楚拿的是哪一侧。
13. **Custom 注册替换先 unregister**（5.12.3 同名注册实测=覆盖生效、与 v5.13 文档"保留旧注册"
    相反；升版前重跑 `tools/experiments/maafw-docs-constraints/` 实验）；空名注册返回 True
    但未注册——返回值不可全信。
14. **默认值在节点首次加载时冻结**——多真源目录必须合并单次 post（`_post_pipeline_merged`），
    否则"后到的 default\_pipeline.json 不影响已加载节点"会形成加载顺序耦合。
15. Win32 `post_relative_move` 需先开 `set_mouse_lock_follow` 且仅 MessageInput 系列可用；
    `FramePool/PrintWindow` 的**伪最小化会改写目标窗口样式与不透明度**——与自研窗口管理
    是同一批状态的写入方，不得混用；Tasker 任务链结束**自动** **`post_inactive()`**，收尾归属要定案。
16. **文档口径 ≠ 当前版本行为**：maafw\.com 跟随 main（v5.13+），本项目锁 5.12.3——凡引用
    文档论断作设计依据，先做最小实验校准当前安装版本（本条由 V-1 注册语义反转实证）。
17. **协议无命名空间 ⇒ 分层靠引用方向，不靠目录**：节点名全城唯一，core 真源不得占用
    也不得引用 `<module>.` 节点（`check_truth.namespace_checks` 机检）；通用层若必须
    指向业务层，只允许指向其汇聚入口 `<id>.__boot.dwell`。互指即须合并单次 post（见 §5.6）。

