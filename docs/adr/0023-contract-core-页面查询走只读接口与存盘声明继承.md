# ADR-0023（contract-core）：页面查询走只读接口，内层段继承运行项存盘声明

- **状态**：`accepted`
- **日期**：2026-11-07
- **决策者**：用户（提出「驾驶时段只希望 speedrush 存图」并拍板范围）
- **相关**：ADR-0019（运行级统一）、ADR-0021（会话优先取证布局，决策 4）、[`core/CODE_WIKI.md`](../../maaracing_master/core/CODE_WIKI.md) 取证段、[`docs/MAAFW_GUIDE.md`](../MAAFW_GUIDE.md) 的 `pipeline_override` 与 Custom 上下文

## 背景

一次驾驶阶段里同时存在两套取证写手：Core DebugHub 的 `nav` 段（每次跑图开段，按段策略落帧）和 speedrush 自己的 `DebugPairWriter`（`drive` 行为目录）。两者共用同一个 WGC 帧源，在控制链运行的整段时间里并行落盘，同一段画面被写两份。

根因不是「缺一套运行期归属机制」——模块早已声明 `DEBUG_SAVE = "self"`（统一流只记段边界、不复制帧），控制器也把它传给运行项级的栈底 scope。缺口在两处：

1. `NavGraph._debug_scope` 开内层 `nav` 段时**没有传 `save`**，吃 `open_segment` 的默认 `"on"`；而采样判定看的是**栈顶段**自己的策略，栈底 scope 的声明管不到内层段——既有契约没有执行到位。
2. `NavGraph.run` 的接口只表达「跑一张图」，驾驶主循环却用它问「页面在不在」，于是纯查询被记成一次导航行为（开段、可持手柄租约）。

## 决策

1. **调用语义由接口表达**：`run()` 只用于导航执行（开调试段、持手柄租约、可执行节点动作）；`probe()` 只用于只读页面识别（不开调试段、不持手柄租约、只调 `Context.run_recognition`）。二者不可互相替代——真需要点击的导航即使发生在驾驶时段，也仍是 `run()`。
2. **取证归属由模块声明、core 只读，调用方显式声明优先**：内层段**默认继承**栈底 scope 的存盘策略（`DebugHub.scope_save()` 的返回值），不得用 `open_segment` 的默认值覆盖。例外是调用方显式传入 `save`——模块据此做到「UI 导航要图、驾驶不要图」；覆盖必须来自**显式参数**，不得来自默认值（否则「无声明」与「显式 on」无法区分，模块的归属声明又会被架空）。栈空且未显式声明时走「无上层声明」分支并留 DEBUG，不把 `None` 当 `"on"` 静默传下去。导航段的**落盘**频率统一取 `NAV_SAVE_FPS`（3Hz），与预览采样（`RUN_SAMPLE_HZ`，10Hz）分开。
3. **`not_ready` 与 `miss` 必须分开**：框架在停止收摊期间会忽略新任务。此时 `probe()` 在入口直接返回 `not_ready`，**不建结果记录、不提交任务**；调用方不得把它当「页面不在」去重试，否则只是空耗步级重试次数。
4. **只读查询的入口不得依赖框架默认等待**：`probe()` 的入口节点 override 必须显式零掉 `pre_delay` / `post_delay` / `rate_limit`。

## 后果

**收益**：控制链运行期间，导航查询退出 Core DebugHub 的存盘帧路径，模块自己的取证保持完整；纯页面查询不再被记成导航动作；两套写手不再同时落同一段画面。

**代价与约束**：

- `core` 的 `_debug_scope` 及今后任何内层段都不得**默认** `save="on"`，必须继承运行项声明；显式覆盖只能由调用方传参给出。新增段类型时这是硬要求。
- 存盘归属是**运行项级**的（`DEBUG_SAVE` 整局一个值，控制器开 scope 时读一次），因此「一半落、一半不落」不能靠改 `DEBUG_SAVE` 实现——那会让驾驶期间栈顶的 scope 也落图，既污染取证又与模块 writer 双写。分界必须落在接口上：`run` 开段且可声明 `save`，`probe` 不开段。
- 上层不得再用 `run()` 做纯页面查询；新增页面查询点必须走 `probe()`，并按三态处理返回。
- `probe()` 是同步阻塞调用（在调用线程里 `post_task` 并等待）。**同实例上 `run()` / `probe()` 不重叠由调用方保证**，core 不加串行锁——这条是调用方契约，不是运行时保证。
- 结果提交必须带有效性检查（只接受仍为 `pending` 的本次 id），收摊未确认时保留墓碑、不删除。只标记不检查挡不住迟到回写。
- **不宣称普遍单一写手授权**：本决策只覆盖 Core DebugHub 与模块私有 writer 这两条已知写盘路径；新增 writer 的准入协议是另一个议题。
- 只读查询的固定开销必须可忽略：入口一旦吃到框架默认等待，单次查询会从毫秒级变成数百毫秒，直接压垮驾驶主循环。

**被否方案**：

- **在每个调用点加 bool 开关**（「驾驶时段就不开段」）：把判断散到调用点，下次新增任何页面查询都会忘，且会造出第二份「当前是否在驾驶」的状态。
- **只在索引层分流**（帧照落、索引不记）：双写成本还在。
- **只用 `stale` 标记失效、桥侧不检查状态**：实测迟到回写会把 `stale` 记录复活成 `completed`，墓碑形同虚设。
- **超时后让调用方继续发下一次查询**：框架在 `stopping` 期间忽略新任务，新调用根本不会执行，只会让上层看到连续失败并空耗重试。
- **用节点级 `inverse` 表达「按钮已消失」**：需要另建探针节点、由 Python 再驱动一次跑图，还受节点 `rate_limit` 节流；点击动作内部的轻量轮询更贴合需求。
- **重写 DebugHub 的段栈 / 队列 / 排空语义**：已有段级仲裁够用，重写会牵动 ADR-0019 / ADR-0021 的既有契约。

## 证据

- **一手核查**：本机 maafw 5.12.3 的 `Context.run_recognition(entry, image, override) -> RecognitionDetail`、`Tasker.post_task(entry, pipeline_override)`、`Tasker.stopping`；`CustomAction.RunArg` 无 `image` 字段（帧须由桥自取）；`docs/MAAFW_GUIDE.md` 的 Context 能力表与节点字段默认值。
- **机检**：`tests/test_nav_probe.py`（三态、入口门控、提交闸门、墓碑、通道、结构约束、UI 导航段声明落图而驾驶控制链不声明）、`tests/test_nav_debug_scope.py`（内层段继承、`scope_save` 读栈底、无声明走默认、显式覆盖优先、nav 段 `save_fps` 为 3Hz）、`tests/test_debug_stream_contract.py`（`save="self"` 零存盘帧、PEEP 保留）。
- **实证**：`tools/maafw_audit/probe_a0/` —— 同帧等价性（框架识别轮 vs `run_recognition` 逐帧全等）、提交闸门对照（无检查时 `stale` 被复活 / 有检查时 `DROPPED`）、成本对照（已注册节点 vs override 新建节点）、离线重放与端到端。
- **真机**：会话 `20261107_110409`（控制链期间 nav 帧 0、内层 nav 段 `save` 全为 `self`、drive 取证索引与帧文件一一对应且 `frame_id` 全量命中控制链 tick）。
- **同族**：ADR-0021 决策 4「谁产帧谁写索引」——本决策只补齐它的运行期执行条件，不重新设计取证目录。
