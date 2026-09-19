# NavKit V4 改造方案

> 状态：已评审通过（2026-09-08）；**P0–P4 全部完成（2026-09-10）**——v4 为唯一执行通路，v3 全家已退役（各期明细见 §9 表）。遗留人工补齐项见 §10。
> 一句话：**真源只有一种——节点 JSON；Studio 画布编辑它，引擎直接跑它，中间什么都没有。**

>
> **本文是什么**：NavKit v4 的**版本宪法（§1 六条不变量）**与 v4 规格——节点模型（§2）、文件布局（§3）、运行时（§4）、校验器（§8）与迁移路线（§9）。
> **本文不是什么**：不是当前实现说明（见 [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md) 与各域文档）；不是决策记录与状态（见 [docs/adr/](adr/README.md)）；不是工具用法（见 [tools/navkit/README.md](../tools/navkit/README.md)）。
> **信源等级**：**§1 = L1（本项目最高规则）**；§2–§8 为 v4 规格（L1/L2）；**§0 与 §9–§12 为迁移过程与历史（L4）**——历史章节不等于当前形态。
> **真源路由**：不变量 = 本文 §1（其他文档引用它，不复制）；节点与策略取值 = `plugins/*/resources/**`；
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../AGENTS.md)。

***

## 0. 背景与结论

### 0.1 v3 的问题

v3 真源是 anchors / stages / transitions / routes / policies 五套概念 + 一层编译映射（编译回 MAA pipeline 节点）。由此产生：

- 图投影是手写的第三份逻辑（graph\_api 自己解析 JSON，routes 边无节点、policies 不进图、global 段不在数据里）；

- 编辑面割裂（锚点在校准页、policies 在策略页、stages/routes 只能手改 JSON）；

- 运行时/图/真源三份结构不一致，全靠 parity 测试维持。

### 0.2 V4 的答案

MaaFramework 生态已经给出标准答案：**pipeline 节点即真源**，且已有成熟的可视化编辑器 **MaaPipelineEditor (MPE)**（MIT 协议，react-flow，在线/自部署，LocalBridge 本地能力协议，2026-09-04 v1.9.3）。V4 = 回归标准 + 把力气花在别人给不了的地方。

### 0.3 继承 vs 废弃

**继承（资产不废）**：模板图、ROI、OCR 逻辑、策略规则、wgcap 中心采集器、clicker、gamepad\_cursor、nav\_graph.py 的 MRA\_Template / MRA\_Click 桥、校准数据、preview/base\_hash 原子落盘管线。

**废弃**：五段概念（anchors/stages/routes/transitions/policies 作为 schema）、编译器、graph\_api、E/P 校验系列、auto\_shoo 避让补丁、detector 灰度匹配与 module.py 内联匹配、parity 测试（实际拓扑=先接线后删，拆 a/b/c/d 四步）。

> **auto\_shoo 一条已于 2026-09-11 翻案**（其余仍成立）：P4c 当时以图内节点的 `mask_cursor` 遮挡过滤替代全部避让，但 `mask_cursor` 只装在模板识别桥上，**OCR 通路没有遮挡处理**，实机被读脏（出价按钮文字读成「出价.39,5」、输入框瞬空读振荡）。现按新证据重接为三层防线：主动避让（`Clicker.auto_shoo`，异步 `submit_move` 不点击）+ 消费侧读数剔除 + 判定精确化，口径见 treasure CODE\_WIKI。原「防反应式躲避被悄悄加回」的反向锁已改为正向锁并写明依据。

***

## 1. 宪法（六条不变量）

1. **真源单一**：逻辑只存在于节点 JSON（+ 策略表文件）。生成物禁止落盘再被运行时读取，只允许内存临时存在。
2. **零中间层**：禁止编译器、禁止投影层。加载时内存内透传映射可以，落盘转换不行。
3. **加逻辑 = 加节点 / 改连线 / 改策略表**，不写 Python。写 Python 仅限引擎能力本身（新识别、新动作），且引擎代码永远不感知业务语义。
4. **机制归引擎，策略归数据**：重试、超时、截图、点击是引擎的事；页面怎么走、点什么是数据的事。
5. **新概念准入制**：任何人想引入"节点/策略表"以外的概念，必须写清它替代什么、何时能删。
6. **运行时拓扑硬约束**：帧只从中心缓存来（WGC 单生产者），动作只从队列走（submit/结果槽协议），引擎只做调度。引擎永不自截帧、永不直连手柄。

***

## 2. 节点模型（schema v4）

存储格式 = MAA pipeline 的直接超集，编辑器字段一律 `_` 前缀，加载时内存剥离。三类元素：

| 元素      | 说明                                                                                                   |
| ------- | ---------------------------------------------------------------------------------------------------- |
| **节点**  | 识别（模板/OCR/坐标点/DirectHit）+ 动作（点击/按键/DoNothing/Custom）+ 出口（next / on\_error）+ 节奏（rate\_limit）+ 遮挡/颜色参数 |
| **边**   | next（有序多选一，命中即走）、on\_error（失败出口）、JumpBack（循环/重试）                                                     |
| **策略表** | 局内复杂决策 = 一个 `action: "Custom"` 节点指向 `<module>.policy.json`，表格编辑                                      |

```jsonc
{
  "hall.enter_treasure": {
      "_label": "大厅 → 鉴宝入口",
      "_page": "hall",
      "recognition": "Custom",                    // 自定义识别必须走 Custom + custom_recognition
      "custom_recognition": "MRA_Template",       // （MaaFW 官方协议方案二；MPE 对类型值
      "custom_recognition_param": {               //   做严格枚举白名单，直写自定义名导入即抛，
        "templates": ["hall_peak_appraise_card.png"], //  P0 实测）
        "roi": [0.76, 0.80, 0.90, 0.89],
        "threshold": 0.8,
        "colorspace": "rgb_strict",               // gray | rgb | rgb_strict
        "color_assert": {"rect": [0.1,0.1,0.3,0.3], "hue": [35,60]},
        "mask_cursor": true,
        "max_occlusion": 0.4
      },
      "action": "Custom",
      "custom_action": "MRA_Click",
      "_park": [0.5, 0.05],                       // L2 驻留点（缺省引擎按候选 ROI 自动算）
      "critical": true,
      "next": ["treasure.hall"],
      "on_error": "hall.recover",
      "rate_limit": 400,
      "timeout": 45000
  }
}
```

> 文件根 = 纯节点映射（MaaFW/MPE 原生形态，P0 金样印证）；**不设** **`_schema_ver`/`nodes`** **包装**——版本承载走仓库级迁移脚本与 CI，嵌进 pipeline 文件反而会被加载器当节点。

### 编辑器与加载器字段规则（P0 源码 + 金样 round-trip 实证）

- **类型值白名单**：`recognition`/`action` 只接受 MaaFW 枚举；自定义识别/动作以 **v2 归一形**书写——`"recognition": {"type": "Custom", "param": {"custom_recognition": <注册名>, "custom_recognition_param": {...}}}`，动作同理（`custom_action` / `custom_action_param` 进 `action.param`）。v1 平铺协议仍合法（框架同一解析路径），但不再是项目形态。

- **根级扩展字段双向无损**：节点根级的未知字段（含 `_` 前缀）由 MPE `extras` 机制原样保留并导出散回；`custom_recognition_param` / `custom_action_param` 内容整体黑盒透传。**扩展字段禁止写进标准字段的 param 深处**（那里 MPE 会丢未知键）。

- **协议形态 = v2 归一（2026-09-10 定案，取代旧"v1 优先"）**：依据不是"v1 被废弃"（源码否证：`PipelineParser` 唯一废弃硬报错是 `is_sub`/`interrupt`，且 v1/v2 共用同一套参数解析路径，新字段两边同时生效），而是 **v2 是框架内部的规范表示**——`get_node_data`（官方 PipelineDumper）只吐 v2 并补齐默认值，MPE 保存亦产 v2；强制 v1 = 每次画布存盘与工具对赌、diff 反复翻转。2026-09-10 已把两真源 44 个槽位一次性归一为 v2，**语义等价由"框架回读快照转换前后逐字段全等"证明**（21 节点零差异）。机检：`test_truth_source_normalized_to_v2` 锁仓库形态，`custom_recognitions()` + `test_custom_recognitions_covers_all_protocol_shapes` 锁读取面双形态兼容（人手写/历史文件/官方示例仍可能是 v1，框架都吃，故兼容不许删）。

- **默认值省写**：`DirectHit`/`DoNothing` 等协议默认值 MPE 会省略导出，加载器按 MaaFW 默认值语义补齐即可，不算数据丢失。

- **编辑器产物跳过**：`$__mpe_config_*` 伪节点与节点内 `$__mpe_code`（内嵌方案的布局字段）加载器必须容忍并跳过；MaaFW 官方 schema 的根节点正则为 `^(?!\$).*`（`$` 开头 root field 官方不解析，schema 层背书），运行时同样行为**仍留 P2a 实机确认一次**。

- **Schema 工具链**：`tools/navkit/schema/pipeline.schema.json`（官方快照）+ `custom.recognition.schema.json` / `custom.action.schema.json`（MRA\_\* 参数契约，官方扩展槽格式）。编辑器侧按 MaaFW 官方"连同 custom.\* 一起复制"姿势挂接；CI 机检由 `tools/navkit/migrate_v4.py` 的校验器承担（不引 jsonschema/Node 依赖），round-trip 自动机检归 P3 薄壳。

关键语义：

- **stage / anchor 消失**："当前在哪个阶段"由图结构表达；识别参数内嵌节点，节点自包含可复制。

- **跨文件互指**：节点名带命名空间（`global.*` / `<module>.*`），所有 JSON 载入同一张图，global 寻路 → 模块寻路天然连成一条链。

- **策略节点**：图上一个框，内部是自带的帧循环闭环（帧计数/冷却/OCR 读数/仲裁），出口只有一种：带着结果退出到 next。这是与 MAA 的本质区别——我们的节点可以是一个闭环。

- **画布坐标分离**：采用 MPE 官方**分离持久化方案**（LocalBridge `SaveSeparatedRequest`，pipeline 与编辑器配置双文件；官方 README"集成与分离两种方案"）——排版永不污染逻辑 diff，机制不自造。

***

## 3. 文件布局

```
core/resources/nav/          global.json          # 大厅骨架节点（共享）
plugins/<id>/resources/nav/  <id>.json            # 模块节点（真源）
plugins/<id>/resources/policy/<id>.policy.json    # 策略表（可选）——禁止与 nav/ 同目录
                             <id>.mpe.json        # 画布坐标/视口（MPE 分离方案的编辑器配置）
```

> **布局红线（P2a-Q1 实验实证）**：`nav/` 目录会被 `Resource.post_pipeline` 递归加载，目录内**只能放节点图**；策略表/编辑器配置放这里会被 MaaFW 当 pipeline 解析，**整目录加载失败**。policy 表由运行时/桥按文件名显式读取，不进加载根。

v3 的 `anchors / stages / transitions / routes / policies` 五段、`<id>_assets.json`、`generated/pipeline/` 全部退役。

***

## 4. 运行时

### 4.1 引擎

- **首选 MaaFramework Pipeline + CustomController**：nav\_graph.py 的识别/动作桥（MRA\_Template / MRA\_Click）成为唯一路径；`screencap()` 实现为"读 wgcap 缓存"。

- **降级自研 mini 引擎**（\~250 行）：截图→识别→动作→走边循环。因为真源是 schema 而非引擎，此选择可逆，业务零改动。

### 4.2 线程拓扑（宪法第六条）

- **WGC 采集线程**：常驻单生产者，写 immutable 帧快照（wgcap.py 已是此形态）。

- **引擎线程**：读缓存调度（零阻塞），同一轮所有候选识别共享同一帧，禁止逐候选取帧。

- **导航线程**：vgamepad 独占，摇杆 + A 键，主循环只经 submit/结果槽控制（gamepad\_cursor.py 模式原样保留）。

- **帧新鲜度守卫**：帧时间戳超过阈值（默认 500ms）报"采集链路故障"，绝不拿旧帧识别。

- **停止** = abort\_event 沿全链传播。

### 4.3 性能指标（P2 验收）

- 一轮一帧硬指标；每轮只识别当前节点 next 候选（通常 1\~3 个 ROI），感知面收窄是主要性能红利。

- 节奏归数据（rate\_limit），原散在 Python 循环的 sleep/cooldown 全部上图。

- 线程数零增长；OCR 沿用现有通道。

***

## 5. 光标遮挡三层防线（替代 auto\_shoo 全家桶）

核心转念：光标是**已知遮挡物**（位置由我们移动，图案固定渲染），不做反应式躲避，而是评分扣除 + 偶发握手。

| 层                 | 机制                                               | 生效时机              |
| ----------------- | ------------------------------------------------ | ----------------- |
| L1 掩膜评分（默认）       | 识别时从匹配区扣除当前光标矩形，用剩余区域打分                          | 每轮识别，零协商          |
| L2 驻留握手（critical） | 节点标 `critical` → 请求导航线程停到 `_park` 点 → 收到 ACK 再识别 | 阶段转换、结算读数等必须满置信识别 |
| L3 挪开重拍（兜底）       | 被遮面积 > max\_occlusion 时挪一次再拍                     | 罕见                |

- **删除清单**：`auto_shoo`、`SHOO_COOLDOWN_S / SHOO_TOL_PX / SHOO_SKIP_MISS_STREAK / SHOO_DIRECTIONS`、避让冷却、"避让丢失不触发重建"特例。

- 引擎分得清"观察"和"动作"：acting 相位时目标 ROI 天然移出守卫集；转移信号识别走 L1 掩膜，光标压着也照常识别——"点击后避让窗口漏检"问题类直接消失。

- 掩膜走自研"光标矩形像素扣除后评分"（OpenCV mask 参数只支持 SQDIFF/CCORR，不依赖）。

- 仅 gamepad 模式启用；real 鼠标模式 WGC 不采 OS 光标，整层不激活。

***

## 6. 颜色匹配

- **收敛成一份实现**：只留 template\_match.py（已是彩色 RGB 匹配）；detector 灰度匹配 + module.py 四处内联匹配全部退役。

- **按节点声明** **`colorspace`**：

  - `rgb_strict`：分通道 NCC 取最低分，对"结构对但颜色错"零容忍（激活态/稀有度判定专用）；

  - `rgb`：三通道标准匹配，默认；

  - `gray`：历史兼容 + 明确声明才用（MAA 生态互操作）。

- **`color_assert`**：模板命中后子区域均值色相校验，近零成本二次闸门。

- 彩色 3 倍开销在"每轮 1\~3 个候选"下无所谓，**默认即可彩色**。

- 迁移期逐节点评估 gray → rgb\_strict，是人工调参项。

***

## 7. Studio V4：嵌入 MPE + 薄壳

**不魔改 MPE**。MPE 官方支持被集成（Iframe 模块、前后端完全分离、LocalBridge 本地能力协议、内置识别小工具与流程化调试）。默认路线：自研薄壳 + LocalBridge 插件，fork 只当保险。

### 7.1 集成堆栈

```
NavKit Studio（自研薄壳）
├── 画布区        iframe 嵌入 MPE（自部署 stable）
├── 策略表编辑器   policy.json 表格
└── 回放 / 校准    trace 叠加层
        │ LocalBridge 协议
NavKit LocalBridge 服务端（自研）
  wgcap 帧源 · 点击器 · 文件读写 · 校验 · 引擎桥
        │
nodes.json · policy.json（真源）
```

### 7.2 `_` 字段 round-trip 策略（P0 已判结）

**实测结论：对策 1 成立**——根级 `_` 字段与 custom 参数黑盒均无损往返（MPE `extras` 机制，源码 + v1.9.3 在线实例金样双证），**零 fork**。约束转写为 schema 规则（见 §2 字段规则）：扩展字段只放节点根级或 `custom_*_param` 黑盒，不进标准字段 param 深处。fork 保险条款保留：若未来 MPE 行为回退，优先提 ISSUE/PR（作者活跃、接需求），合不进去再谈浅 fork。

### 7.3 能力分工（P0 协议面实证）

- **iframe embed 协议（mpe-embed 1.4.0）** 管编辑链路：`mpe:loadPipeline / mpe:save / saveResult`，内置文档冲突状态机（`document_changed` + canForce，与 v3 base\_hash 乐观锁同构思路）与 dirty 管理；在线实例无 X-Frame-Options/CSP 嵌入限制（实测）。

- **LocalBridge 协议**（`{path, data}` WebSocket + `/system/handshake` 版本协商）管本地能力：file（含 watcher 与 `ContentHash` 乐观锁、`SaveFileRequest` 保字段序、`SaveSeparatedRequest` 分离保存）、utility（截图/裁剪←wgcap 帧源）、mfw/debug（运行与流程化调试，命令面大，P2a/P3 按需实现）。

- **MaaMCP**（MAA-AI，AGPL-3.0）定位是"agent 执行运维 pipeline"标准件（connect/screencap/run\_pipeline 等），不进编辑与解析链路；P2b 真机调试阶段可挂载使用，**AGPL 传染性要求独立进程隔离**。

***

## 8. 校验器（7 条）

1. next / on\_error 引用闭合；
2. 模板文件存在（附未引用模板清单）；
3. ROI ∈ \[0,1] 合法；
4. 纯坐标点击必须有 guard 模板（保险丝）；
5. 死胡同 / 不可达节点 / 无出口环；
6. 疑似重复识别（同模板不同 ROI）告警；
7. **编辑器往返（round-trip）**：真源文件必须能被 MPE 解析器无损导入再导出（逻辑字段等价），且无键侵入 MaaFW 协议保留字段——此条进 CI，用 Node 直调 `core/parser` 纯函数固化。

E 级不落盘闸门逻辑保留。

***

## 9. 迁移路线

| 阶段  | 内容                                                                                                                                                                    | 验收                                     | 状态                                                                                                                                                                                                                                                                                                                                                                |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0  | **MPE 选型验证**：`_` 字段 round-trip、param 透传、LocalBridge 对接 wgcap、iframe 嵌入可行性；schema v4 定稿 + JSON Schema 文件                                                               | 验证项全绿 + schema 评审                      | **验证三项全绿（2026-09-09）**；JSON Schema 文件与映射细则随 P1 落地                                                                                                                                                                                                                                                                                                                 |
| P1  | v3→v4 迁移器（treasure + global），策略段拆表；继承 v3 资产（模板/ROI/OCR/策略规则/wgcap/clicker/gamepad\_cursor/nav\_graph 桥）                                                               | 生成图逐节点核对 ROI / next                    | **完成（2026-09-09）**：M0-M4 全绿，迁移器 + 真源图（global 7 + treasure 24）+ schema 三件套（commit 6bfb7f0）                                                                                                                                                                                                                                                                         |
| P2a | 引擎接线：MaaFW 成唯一执行路径，CustomController 读 wgcap，MRA 桥落地遮挡 L1/L2/L3 + rgb\_strict + color\_assert                                                                          | 离线等价回放一致 + `$__mpe` 容忍与 `$` 字段忽略范围实机确认 | **完成（2026-09-09）**：Q1-Q5 全绿，全仓 464 测试                                                                                                                                                                                                                                                                                              |
| P2b | 真机性能验收（帧预算/CPU/导航线程不被饿）+ 光标与颜色真机调参                                                                                                                                    | 指标达标                                   | **完成（2026-09-09 用户真机验收）**：完整局端到端通过（寻路/出价/快照/落盘/结算全链），决策帧率实测优于 v3（OCR 108ms/13 区、帧间隔 \~125ms）；排障九炸记录于当期运行日志（过程材料已清）。L2 `_park` 标定依赖 P3 可视化，随 P3 落地                                                                                                                                                                                                          |
| P3  | Studio：**C 形态 = MPE 即 Studio**（无壳）——`studio.cmd` 起 mpelb+开 MPE 满幅；`policy_server.py` 策略表独立薄页；真源目录 `pipeline/` 生态惯例；rgb\_strict 评估收口（13 节点全 gray 足够）。主 GUI/sidecar 零改动 | 编辑→保存→运行闭环（round-trip 保真已证）            | **完成（2026-09-09）**：P3a 实证（mpelb/MPE 保真）+ P3b 闭环 + P3c 策略表薄页 + P3d 交集归零                                                                                                                                                                                                                                                               |
| P4  | **删除**：v3 解析器、编译器、E/P 校验、graph\_api、旧 trace、auto\_shoo、detector 灰度匹配与 module.py 内联匹配、parity 测试                                                                        | 仓库搜不到 v3                               | **完成（2026-09-10）**：默认通路切 v4（6621107）；P4a 整删批（b9e3b3f，-18757 行）；P4b 数据源切换（c963836，双跑对拍等价、金标直过、迁移器退役、check\_truth 接管 CI 闸门）；P4c 匹配收敛与遮挡替代（576e757：单一 template\_match 引擎+按锚点 colorspace，shoo 全家退役，859 帧同政策直跑 99.9% 一致；真机完整局验收通过）；P4d 开关删除与终局验收（NAVKIT\_SOURCE 本体+v3 主循环死路径+v2 回退常量清除，执行通路唯一；trace 经生态核查后用户拍板保留设施、删 plan\_version 字段；全仓去 v3 措辞，终局 grep 零活代码命中，243 全绿） |

**P4 是最关键一步**——不删干净，V4 就是叠加在屎山上的新屎山。

***

## 10. 需要人工补齐的

- ~~P2b：真机验证帧预算、轮均识别耗时、导航线程不被饿~~ 已达成（2026-09-09 用户真机验收，决策帧率优于 v3）。新旧路径 trace 逐帧对拍降级为按需——九炸排障期已逐行为对拍通过，剩余分歧出现时再启用 `_trace_writer` 取证。

- 光标遮挡一轮（**三件合并成一次真机/录屏回放跑完**，同一批光标数据、同一套回放工具，
  分三回折腾代价更高）：

  1. **盘形状修正（前置，改完才有调阈值的意义）**

     - 根因：`template_match.cursor_box_norm` 把同一个归一化标量分别乘宽和高
       （`w, h = size_norm * frame_w, size_norm * frame_h`）。归一化单位在横轴是 1280px、
       纵轴是 720px，所以必然画出 **38.4×21.6 的扁框**——不是模型选择，是单位混淆。

     - 后果：图侧 `mask_cursor` 的遮挡判定在**垂直方向系统性偏松一半**（真实半高 10.8px
       vs 应有 20px），"光标压在 ROI 下缘"会放行命中。

     - 真值出处（已量过，只是没回流）：`gamepad_cursor.STATE_SIGNATURES` 盘半径 6\~13px +
       环厚 2\~7px → 外径 ≤ 约 20px；检测器本身按圆建模（`radius_est = sqrt(area/π)`）。
       旧常量 `CURSOR_SIZE_NORM = 0.03` 的注释写着"P2b 真机校准**前**"，水平方向 19.2px
       恰好对上真实半径，所以这个偏差一直没暴露。

     - 修法：提单一像素真值 `CURSOR_OCCLUSION_RADIUS_PX = 20`（从 STATE\_SIGNATURES 派生），
       `cursor_box_norm` 改边长 = 2R 的**正方形像素框**（两轴同值）；2026-09-11 新加的
       `cursor_occlusion_radius_px(frame_w)` 随之去掉帧宽因子，退化成 `R + PAD`。

     - 影响面：所有开 `mask_cursor` 的节点垂直遮挡从 21px 收紧到 40px，可能把原本放行的
       命中判成遮挡拒绝 → **必须与下面第 2 件同批验证**，单改形状不调阈值会让节点变哑。

  2. `max_occlusion` 阈值用录屏回放调一轮（逐节点标定，与第 1 件互为因果）。

  3. 各页面 `_park` 驻留点标定（Studio 画布可视化标点）。

  - 验收：录屏回放对拍"改前/改后"逐节点命中差异清单（谁从放行变拒绝、拒绝得对不对）；
    `check_truth` 与 `pytest` 全绿；差异清单归档 `tools/experiments/`。

  - ~~光标贴图尺寸入引擎常量~~ 已落（2026-09-11：`CURSOR_SIZE_NORM` +
    `cursor_occlusion_radius_px()`，避让侧不再自标魔数）。**注意该函数本轮还要再改一次**
    （见第 1 件），当前形态是"乘帧宽"，形状修正后不再乘。

- ~~颜色：逐节点评估 gray → rgb\_strict~~ 定案收口（2026-09-11）：**默认 gray，灰度拉不开差距才转 rgb**，两面（图节点 / policy spec）由 `check_truth.anchor_face_checks` 锁成 error 级、盘上零分叉。依据 = 检测面 `detector` 早已按 `spec.arbitration.margin` 做领先判定，P4C 对拍 812 帧两引擎命中数相等、零翻转，gray 快 2.8 倍。

- 迁移校图：P1 生成物逐节点核对。

- 策略表 schema：出价规则字段需模块作者确认。

***

## 11. 明确不做

节点嵌套/子图、可视化脚本编排（节点只有三个出口）、多人协作、引入 MaaFramework 生态 GUI 替代我们的壳、自研节点画布。

***

## 12. 参考

- MaaFramework：<https://github.com/MaaXYZ/MaaFramework> （协议文档 <https://maafw.com/docs/3.1-PipelineProtocol> ）

- MaaPipelineEditor：<https://github.com/kqcoxn/MaaPipelineEditor> （文档 <https://mpe.codax.site/docs/> ，嵌入协议 guide/other/emb-protocol，LocalBridge guide/server/）

- MaaMCP：<https://github.com/MAA-AI/MaaMCP> （AGPL-3.0，进程隔离使用）

- MaaInspector：<https://github.com/TanyaShue/MaaInspector>

- 本仓库 docs/MAAFW\_GUIDE.md（§4 范式二 = 本 plan 的 Custom 形态依据；§9 API 红线）

- **P0 验证（2026-09-09 完成，过程材料已清）：金样对拍、源码行号证据、实测响应头全数核验**

