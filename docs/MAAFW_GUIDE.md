# MaaFW 能力与 Python 开发规范

> 用途：本仓库后续开发"类方舟日常"式**离散步骤流程**（新活动、清理日常、页面切换链等）时，
> 直接复用本文档的**可复用范式**，避免从头查文档/从零写代码。
> 信息基于 MaaFramework 官方文档 + Python binding 源码（`source/binding/Python/maa`）核对。
> 官方全套文档：<https://maafw.com/docs/>（中文）；仓库：<https://github.com/MaaXYZ/MaaFramework>

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
res.register_custom_recognition("MyReco", inst)  # 注册自定义识别器
res.register_custom_action("MyAction", inst)     # 注册自定义动作
res.override_pipeline({...})                   # 运行时覆盖 pipeline
res.override_next("节点", ["A","B"])           # 运行中改 next 列表（节点不存在也会创建）
res.override_image("img.png", ndarray)         # 覆盖图片数据
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

- `recognition` 默认 `DirectHit`（不识别直接执行）；常用 `TemplateMatch` / `OCR`。

- `action` 默认 `DoNothing`；常用 `Click` / `Swipe` / `Custom`。

- `roi`(识别区) / `box`(命中框) / `target`(动作点) 三个概念分离；`target` 默认 `true`=用命中框。

- `timeout`（识别 next 的超时，默认 20s，`-1`=无限）、`rate_limit`（每轮识别最低 ms，默认 1000）。

- 用 `pre/post_delay`、`pre/post_wait_freezes`(等画面静止) 控制节奏；\*\*少用硬 delay，多用
  "中间过程节点"\*\*会让流程更稳。

- `next` 列表顺序识别、**命中即中断**执行第一个 —— 天然表达"多选一"分支。

- `on_error`：next 全未命中且超时 / 动作失败时走的分支（重试/告警）。

- `anchor` + `[JumpBack]`：动态锚点回跳，实现**循环/重试**（如"没拿到→跳回再领"）。

- `repeat` / `max_hit` / `enabled` / `inverse`：动作重复/命中上限/开关/反逻辑。

- `default_pipeline.json`：放资源包根目录，统一给所有节点/某算法/某动作设默认参数，减少重复。

### 5.3 识别算法速查

`DirectHit | TemplateMatch | FeatureMatch | ColorMatch | OCR | NeuralNetworkClassify |
NeuralNetworkDetect | And | Or | Custom`。

- `TemplateMatch`：找图，`template` 相对 `image/`，支持多模板、`threshold`、`method`。

- `OCR`：内置 PaddleOCR(ONNX)，`expected` 关键词/正则，支持 `color_filter`。

- `And`/`Or`：复合识别（"A 且 B" / "A 或 B"）——很适合做多条件到站判定。

- `Custom`：接自研识别（见 §6）。

### 5.4 何时用 Custom、何时纯 JSON

- 纯"点已知按钮/等已知文字"→ 纯 JSON。

- 需要"复杂判断、算法、跨帧状态、自定义计算"→ `CustomRecognition`（识别）+ `CustomAction`（动作）。

- 实时高频控制 → 只许用 `CustomAction`，别拆成 Pipeline 节点。

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

***

## 7. 调试与诊断

- `Toolkit.init_option(path)`（maafw 5.12.3 起第二参可省，binding 把 `None` 归一为 `{}`；旧版 binding 需显式传空串 `""`）读取/生成 `config/maa_option.json`：
  `logging`(存 maafw\.log)、`save_draw`(存识别可视化到 vision/)、`stdout_level`(0无\~7全)、
  `save_on_error`(失败存图)、`draw_quality`。

- Tasker 全局选项可设 `DebugMode`(所有任务当 focus 产生回调，RecoDetail 含 raw/draws)。

- 监听日志：`tasker.add_context_sink(PipelineLogger类)`（项目已有 `core/pipeline_logger.py`）。

- 生态工具：MaaDebugger(Pipeline 调试器)、VSCode 插件(maa-support)、MaaPipelineEditor(可视化)、
  MaaCommonAssets(预转 OCR 模型)、MaaPracticeBoilerplate(空模板脚手架)。

***

## 8. 本项目可复用结论（速查）

| 事项                        | 结论                                    |
| ------------------------- | ------------------------------------- |
| 截图（Win32 FramePool / WGC） | 保留现有（含 `PostScreencapCapture` RGB 封装） |
| 实时控制 / 光标导航 / YOLO / OCR  | 自研，保留为 CustomAction / 自研引擎，**勿迁**     |
| 新增离散流水线（日常、活动代刷）          | **用本文档 §5 范式**，JSON + Custom          |
| 自研识别若要进 Pipeline          | 包层 `CustomRecognition` 壳（§6），不动算法     |
| 通用 GUI / 可视化调试            | 需要时写 `interface.json`，接通用 UI 生态       |

***

## 9. 高频红线/坑（背下来）

1. `Tasker.bind(resource, controller)` — **resource 在前**。
2. `Resource.post_bundle(path)` — 是 `post_bundle`，**不是** `post_path`。
3. `Toolkit.init_option(path)` — 5.12.3 起第二参可省（binding 源码 `None→{}` 后必传 JSON 串）；低于 5.12 的旧 binding 必须显式传空串 `""`。
4. `Win32Controller(hWnd=hwnd, ...)` — 参数名**驼峰** **`hWnd`**。
5. 截图返回 `Image`，`img.numpy()` 是 **BGR**；要 RGB 手动转。
6. `post_*` 都是异步 → 用 `.wait()` / `.get()`；取结果前先 `wait()`。
7. `CustomRecognition.image` 是 BGR；返回 4 元组/`None`/`AnalyzeResult` 三选一。
8. `robot`/template 图需 720p 无损原图裁剪。
9. 新功能默认走"范式二 JSON + Custom"（官方推荐），全代码只做宿主编排。
10. `Toolkit.find_desktop_windows()` 返回 `DesktopWindow` 对象列表，属性为 `hwnd` / `class_name` / `window_name`（下划线命名；用法见 `core/window_utils.py`）。

