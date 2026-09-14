# MaaRacingMaster — Code Wiki · 鉴宝域

> 《巅峰极速》"巅峰鉴宝"活动 —— **出价 / 估值 / OCR 全自动模块（treasure\_\*）** 专属文档。
> 聚焦鉴宝核心：12 阶段状态机 / 准星意图 / 出价策略（bid\_strategy）/ 异步 OCR / ROI 三段分类。
>
> **先读**：[RULES.md](./RULES.md) —— 游戏规则事实（成交条件 / 分红机制 / 计分目标 / 对手行为约束）。
> 本域一切策略与实现都以游戏规则为前提；**规则与本文冲突时以 RULES.md 为准**，并回头修本文。
>
> 配套文档：
>
> - 主文档：[docs/CODE\_WIKI.md](../../../docs/CODE_WIKI.md)（架构 / 导航引擎 / 配置 / 调试 / GUI）

***

## 目录

0. [游戏规则（独立文件 RULES.md）](#0-游戏规则独立文件-rulesmd)
1. [treasure\_module 巅峰鉴宝模块](#1-treasure_module-巅峰鉴宝模块)
2. [bid\_strategy 出价策略](#2-bid_strategy-出价策略)
3. [treasure\_detector 阶段检测器](#3-treasure_detector-阶段检测器)
4. [treasure\_ocr 金额识别](#4-treasure_ocr-金额识别)
5. [treasure\_renderer HUD 渲染](#5-treasure_renderer-hud-渲染)
6. [NavKit ROI 校准与结构树控制台](#6-navkit-roi-校准与结构树控制台)
7. [鉴宝类速查](#7-鉴宝类速查)
8. [鉴宝模板清单](#8-鉴宝模板清单)
9. [鉴宝坑点](#9-鉴宝坑点)

***

## 0. 游戏规则（独立文件 RULES.md）

游戏怎么运转（回合结构 / 成交判定 / 收入与分红 / 计分目标 / 对手行为约束）唯一载于
[RULES.md](./RULES.md)，
**本文不复制**。规则是外部事实（代码错了改代码没用），修改须人工复核（见该文件顶部硬约束）；
未知项列于其 §6，**禁止当作已知规则使用**。

本文及本域代码中一切策略推导，前提均为 RULES.md；两者冲突时以规则文件为准。

***

## 1. treasure\_module 巅峰鉴宝模块

[treasure\_module.py](./module.py)（v0.13.0 主战场）

**职责**：

- 活动模块实现（`ActivityModule` 子类，`ID="treasure"`），12 阶段状态机

- **准星意图模式**：当前只算「程序想点击的位置」，不执行真实点击

- 鉴宝师选择自动化 / 场次选择自动化（模板匹配 + 静态按钮中心）

- 异步 OCR worker（latest-only 丢帧 + 关键 ROI 优先通道）

- 估值算法：全 5 回合系统报价最大值 `sysmax_13`（H=智能出价填入的输入框值，只取每回合第一次）×1.35(求稳)/1.4(激进) = 真实估值区间

- **落盘子域**：结构化落盘已拆出到同目录 [store.py](./store.py)（`TreasureStore`：SQLite 场次明细 + 当日汇总 + 会话总结），模块主循环只做编排与委托

- **资源随插件**：鉴宝模板位于同目录 `resources/image/`；识别与 ROI 的唯一真源为 `resources/policy/treasure.policy.json`（`perception.spec` 锚点 + `policy` 段），detector/module/ocr/eggs 一律读它。插件以 `__init__.py` 的 `IMAGE_DIR`/`PIPELINE_DIR`/`POLICY_PATH`/`nav_source()` 统一引用，不依赖主程序 `assets/`。

- **NavKit 底座**：`core/navkit` = v4 数据面 loader（v4\_source：policy.json → NavSource/DetectionPlan）+ 决策引擎（policy.py）+ trace 落盘记录器；`tools/navkit` 是 MPE 桥入口 + 策略表薄页 + check\_truth 校验闸门。固定坐标点击件不强制配模板，必须由 spec 锚点 `guarded_by` 担保（D2）。

**阶段链路（policy.json** **`perception.stages`** **序，仍与** **`STAGE_ORDER`** **保持 GUI 断点兼容）**：

```
游戏大厅 → 活动页面 → 鉴宝大厅(选择场次) → 匹配中 → 选择鉴宝师
→ 第1~5回合出价 → 中标结算 → 领取分红
```

**准星意图链路**：`_match_appraisers`/`_match_selected_check`（匹配）→ `_run_appraiser_choice`/`_run_session_choice`（算意图写 `_appr_last_decision`/`_session_last_decision`）→ `_decide_action`（阶段驱动决策）→ `_resolve_action_target`（补归一化 center）→ `_treasure_kwargs` → `debug.save_frame`（渲染准星）

**鉴宝师选择**（`选择鉴宝师` 阶段）：

- `_match_appraisers`：全屏搜索区 `_APPRAISER_SEARCH_ROI=(0.03,0.18,0.97,0.92)` 内多尺度匹配（0.70\~1.30×13 档），顺位 P1 卡洛琳 → P2 章太郎

- `_match_selected_check`：`stage.appraiser_selected_check` 横向长条 rect 扫黄色√，对勾中心 X ≈ 目标卡片命中框右边界（容差 0.09）→ 已选中 → 准星指 `confirm_red_btn`

- 目标均未识别到 → 兜底：对勾命中（已有卡被选中，大概率是刚点的中间卡）→ 准星指 `confirm_red_btn`；否则准星指屏幕中心 (0.5, 0.5)（凑合点中间卡）—— 避免「点中间卡→对勾出现→仍指中间卡」死循环

**场次选择**（`鉴宝大厅(选择场次)` 阶段）：

- `_match_session_panel`：模板匹配「开始匹配」按钮 `session_start_match_btn`（stage 段）判定"详情卡已切到目标场次"

- 命中 → 准星指 `session_start_match_btn`（静态中心）；未命中 → 准星指 GUI 目标场次 badge（`session_intern_badge`/`session_expert_badge`/`session_master_badge`，静态中心）

- 按钮中心来自 `_load_action_centers`（同时扫 JSON 的 stage+actions 两段）

**每日循环收尾**（到限 → 「彩蛋任务」领取，stage-from-node-plan §10 定稿形态 A′）：

- `_tick_once` 0.05 到限稳定 3 帧确认（且阶段=鉴宝大厅(选择场次)、本运行未进过）后不再直接停：进 `_run_egg_claim_chain()`——决策 tick 内**阻塞自包含顺序例程**（抓帧→匹配→同步点击→等待）。帧源仍是 WGC 中心缓存只读，不新起截图通路；Tasker 被占用 = 常驻图停摆，活动页「前往鉴宝」自动边没有触发机会，收尾方向冲突天然消解（因此不改图拓扑、不加旗标闸门）。

- **链内点击与主链路同一条出口协议**（2026-09-14 修复）：`_egg_chain_click` 提交前先 `_egg_chain_drain_slot` 消化任务槽遗留结果（链在决策段消费点**之前**触发，槽里必有上一帧的提交）、意图开关取自 `ctx.intent_mode`（不再硬编码覆盖共享 Clicker 的状态）、结果经 `_record_click` 统一落事件与日志、提交被拒或结果超时有界重试 `EGG_CHAIN_CLICK_RETRY_MAX` 次。此前链内自成一套出口，实测三处后果：仅意图开关对链内点击失效（鼠标模式一路真点击）、首次点击必被 `is_busy` 拒掉（返回键命中置信度 1.000 仍报「未点中」而放弃整链）、链内点击完全不可观测。回归锁 `tests/test_treasure_egg_claim.py` 四条（意图开关跟随 / 先消费后提交 / 有界重试 / 结果留痕）。

- 链路线：屏幕返回键「D」→ 活动页 → 「获取银币」→ 任务抽屉（tab1 找红按钮 → 无则切「彩蛋任务」tab 再找）→ 聚合奖励弹窗 → `EggRewardRecognizer` 数蛋 + `claim_coin_medal`/`claim_score_medal` 匹配后按通用几何取「×N」读银币/积分 → `store.record_egg_claim` 累加落盘 → 点屏幕任意处继续 → 复查轮次 ≤3 → 点左侧空白关抽屉 → 点房子回大厅 → `request_stop()`。总预算 45s 由链入口算成墙钟 deadline、各步等待按剩余预算 clamp（`_egg_chain_wait` 的 `budget_deadline`——此前该常量只出现在日志文案里、各步各自计时，链整体实际无上界），领取循环另有尝试次数上限（轮次上限×2，防红钮常驻＋点击持续失败时 `continue` 空转）；每步超时/异常只记 WARNING 后仍走停止路径——「有就领，没有就结束」，绝不阻塞停止。medal 区分度经真匹配器实测（单币章 vs 币堆章 margin≥0.68，见 `tools/experiments/egg-claim-coin-read/`）。

- 「×N」读数区 = **奖励卡通用几何推导**（用户拍板的设计，蛋卡/medal 卡同一套，`eggs.py::find_card_body/CARD_COUNT_BAND`）：红=命中图标框 → 外扩找整卡白色描边（Canny+轮廓，高宽比≥1.2 的最小包含候选；图标卡体内描边≈1.12 被天然排除）→ 蓝框内按比例切数字带（y 0.62–0.805）与名称带（y 0.805–0.985）。**几何参数只在代码常量，真源 policy 不携带偏移参数，Studio 也不做其预览**（校准台只管 ROI 与模板匹配——为推导区单开预览被用户明确否决）。校准与消费端契约：`tools/experiments/egg-claim-coin-read/card_geom_probe.py`（5 卡 5/5，含 det=True 行级真值对照）与 `tests/test_treasure_egg_claim.py::test_egg_recognize_golden_frame_counts`（真帧数蛋锁）。教训：计数区若做成「锚点+固定偏移」参数，偏移不可见不可调，历次校准全是猜——上一版交付就栽在未验证的偏移参数上。

- 惰性锚点真源 = spec 的 `egg_claim_red_btn`/`egg_claim_title`/`egg_panel_tabbar`/`egg_task_tab3`/`act_get_silver_btn`/`hall_back_btn`/`hall_home_btn`/`claim_coin_medal`/`claim_score_medal` 族（= `TreasureModule.EGG_CHAIN_ANCHORS`，测试直接 import 该常量、不再手抄副本），**不进 transitions/stages.active/global\_anchors**（detector 零扫描、不参阶段判定；`tests/test_treasure_egg_claim.py` 机检这条惰性）。链尾确认「已回大厅」另用全局锚点 `EGG_CHAIN_LOBBY_ANCHOR`（`hall_peak_appraise_card`，检测面本就每帧扫描），与惰性清单语义分开、不混。GUI 底部阶段条在收尾期间（约 30s）不更新——链不是阶段，以「\[彩蛋收尾]」日志补偿；日后要可视化按 §10.1 升级路径补 dwell+stage，属独立改动。

- 蛋落盘语义：`games` 蛋列退役（不再写、列保留）；`daily_summary.egg_red/yellow/blue` = 「今日领取数」，写入点=链内弹窗数蛋成功；`egg` 数蛋锚点 rect 已搬家到聚合奖励弹窗蛋卡行（旧结算链业务上不再产蛋；真帧实测红/黄/蓝蛋 0.837–0.920 命中、币章卡被中心 S≈7 判色拒掉）。
- GUI 看板读侧契约（`core/sidecar.get_today_stats`）：新列的 ALTER 迁移由鉴宝模块（写侧）惰性建连时补，升级后天然存在「GUI 开着、模块没跑过」的窗口——读侧**按 PRAGMA 实有列取交集查询、缺列兜底 0/None**，绝不硬编码全列 SELECT（真机实证：曾 378 次/83min `no such column: egg_coin` 看板全灭）。回归锁 `tests/test_sidecar_today_stats.py`。

**回合出价**（`第N回合出价` 阶段，`_run_bidding_choice`）：

- **pass 二级确认弹窗（真机 2026-09-14 事故后新增）**：输入 0 点「确认出价」必弹「是否确认本轮放弃出价？」（用户确证；>0 确认无弹窗）。弹窗压暗面板把 smart_bid 打到 0.736——正落在**决策路兜底 0.72 与告警路 spec 0.75 的阈值缝**里，被判「面板仍开」→ 相位卡 bidding 空转人工停。处置：红「确认」钮表示成惰性 spec 锚点 `bid_pass_confirm_btn`（素材真帧量测裁切，detector 不扫），`_run_bidding_choice` 入口先于 S0-S3 一切判定查它，命中即点「确认」落实 pass（点「取消」会回面板再弹成死循环）。阈值缝本身不统一：0.72 兜底为模糊窗实测 0.686 而设（有独立正当性），弹窗表示出现后不再需要靠缝内分数做判断。契约锁 `tests/test_treasure_pass_dialog.py`（命中+串环境负例）。
- **快照完整性语义**：`RoundSnapshot.is_complete` 里 0 是合法公开事实（我方=0=放弃/掉线同样有效，对手位次不得被连坐作废；仅 -1 未读到算缺失）。缺完整快照时卡第二分支**退回 observe**，绝不把「没数据」当「对手全 0」出 pass——pass 只允许出现在确证竞争者存在且压不动时（回归锁 `test_bid_strategy` 两条 2026-09-14 用例）。

- 状态机：S0 转场期（`round_elapsed < SWITCH_CONFIRM_FRAMES`）→ 不出准星；S1 等待出价 → 不出准星；S2 出价亮起 → 准星指 `bid_main_red_btn`；S3 面板已开 → H 未读点 `smart_bid_btn`（智能出价）、H 已读进 `_run_bidding_execute`（策略决策 → 输入子状态机 → 确认出价）；提交后 S4 wait\_result（等公开报价，OCR 读 4 槽构建快照）

- 面板已开判定：`stage.smart_bid_btn` 模板匹配（`bid_smart_btn.png`，面板内「智能出价」按钮，只有面板打开才出现 = 强信号）

- 主按钮状态（等待出价/出价）走 **OCR 文字**（`ocr.bid_main_btn_label`）——按钮明暗态模板匹配不稳（见 Experience 1112416），用 OCR 文字「等待出价」→「出价」切换判 S1/S2，比模板稳。OCR 通路光标遮挡三层防线（2026-09-11 重新接线）：① 主动避让——决策段每帧 `_maybe_shoo_cursor` → `Clicker.auto_shoo`（光标压住 `_collect_guard_rects` 的识别区且下一意图不能自然带离 → submit\_move 避让不点击，0.3s 冷却/miss\_streak 闸；P4c 退役 shoo 时 mask\_cursor 只覆盖了图内模板节点，OCR 通路实机被读脏「出价.39,5」）；② 消费侧剔除——`_read_bid_main_btn_label` 光标压 ROI 时读数按不可信返回 ""（避让冷却窗兜底）；③ 判定精确化——S2 只认剥非中文后恰为「出价」的读数（「等得出价」误读不再点灰按钮）

- `_load_action_centers` 同时扫 stage+actions，`smart_bid_btn`（stage）与 `bid_main_red_btn`（actions）自动进 center 表

**OCR worker（异步，`_ocr_worker_loop`）**：

- 两段式 + **双结果槽**（P4 双通道覆盖 bug 已修，2026-08-20）：第一段关键 ROI `OCR_CRITICAL_KEYS=('bid_result_amount_box','bid_player4')` 识别 → `_ocr_publish_result(..., critical=True)` 写**关键槽** `_ocr_result_critical`（H+P4 独立、不被覆盖）；第二段识别 `阶段keys − _OCR_CRITICAL_SET`（**剔除 H/P4**，同帧不重复识别）→ 写全量槽 `_ocr_result`。主线程 `_apply_ocr_result` 每帧 take 关键槽+全量槽、各自过 provenance/时效闸门后**合并成一份 res 消费**——H/P4 恒来自关键通道（时效最低），P1\~P3/玩家名来自全量通道

- **报价等待阶段的高频投递（口径已随 v4 变更，勿按旧数字理解）**：设计意图是 `WAIT_RESULT_FAST_MS=150` 对 `FRAME_INTERVAL_MS=300` 的「仅报价等待阶段帧率翻倍」，配合动态 keys 剔除已固化槽 → 未固化槽（尤其 P4）读取频率翻倍。**v4 实况**：帧节奏由 `PolicyBridge` 驱动（`MIN_FRAME_INTERVAL_MS=100` 地板 + `_tick_once` 自身耗时），真机出价段实测 115\~290ms 随负载浮动——**"翻倍"不是可依赖的保证，报价窗口能否读满取决于当帧负载**（tick 够快时自然接近 110ms，见坑点表「落盘编码抢占 OCR 时效」）。旧常量 `WAIT_RESULT_FAST_MS` 与 `_frame_interval_s` 属性已删除（2026-09-13；后者自 09-10 `e55a571` 删 v3 主循环起就无调用方）；`FRAME_INTERVAL_MS` 保留但**只剩帧数↔时间换算基准**一个用途，不再代表实际间隔。⚠️ 另注意：双通道（第一段）**不改变投递频率**（主线程每帧投递一帧、worker latest-only），只保证 P4 时效最低、不被全量超龄拖死、消除同帧重复识别；「P4 相对其他槽 2× 采样」在 latest-only 单帧架构下物理不可达（报价刷新是时间函数），采样密度提升靠帧率翻倍 + IO 异步化（见下）

- **debug 落盘 IO worker（`_io_worker_loop`，2026-08-20）**：渲染 HUD/ROI/PEEP + raw JPG + rendered JPG 全部移出主线程（生产-消费者，`_io_submit` 入队，有界队列满丢帧不阻塞）。原每帧 \~67-100ms 同步存盘曾把 wait\_result 实际帧率从 150ms 拖回 \~240ms；异步化后主循环只剩截图+检测+OCR 消费+心跳。**编码口径定为 JPG（rendered q85）**：1280x720 实测 WEBP q95 单帧 77.7ms 且降到 q50 仍要 60ms（OpenCV webp 单线程、成本几乎不随质量下降），JPG q85 只要 3.9ms、体积同级（141KB vs 138KB）——webp 会让本 worker 在 150ms 产帧节律下占满 60% 单核并与 OCR worker 争抢 CPU（见坑点表「落盘编码抢占 OCR 时效」）

- **性能仪表（`read_perf_snapshot`** **/** **`read_recognition_health`** **/** **`log_perf_summary`，2026-09-13）**：把「只能手扫日志算」的定性量变成进程内读数。计数项：OCR 过双闸被采纳 / 超龄丢弃 / 过期丢弃（**另按** **`wait_result`** **分桶一套**，见下）、落盘入队 / 丢帧 / 队列峰值（`_io_submit` 的 `except Full` 曾完全静默）、决策帧间隔滑窗（`_tick_once` 相邻 tick 的 perf\_counter 差）。窗口 `PERF_WINDOW=200`，导出 p50/p95 线性插值分位。

  - **两条消费者，改快照 key 前先想它们**：① `core/sidecar.get_status` 以鸭子类型取 `read_perf_snapshot`（core 不点名本模块），随 250ms 轮询带出 `perf` 字段喂 GUI「性能监控」卡——鉴宝模板是 `画面响应 / 机器负载 / 识别健康` 三项 + 内联 SVG 走势图（无 CDN），竞速沿用原 YOLO 形状；前端只读 `response.{fps,level}`、`cpu.{available,cores,load_p50,level}`、`health.{level,text}`，取不到时渲染"无此项"而不是猜默认值。② `log_perf_summary` 写日志。仪表失败不得影响轮询（sidecar 侧 try/except，有测试锁）。

  - **主判据必须按报价窗口分桶**：`识别健康` 三态（idle/ok/warn/error）优先用 `wait_result` 内的丢弃率，窗口未出现才退回全局。原因：真机那次「第1、2回合报价完全没录到」全局丢弃率只有 18.8%（按全局判只到 warn，太松），而报价窗口内接近全丢。阈值 `PERF_DROP_WARN_RATIO=0.05` / `PERF_DROP_ERROR_RATIO=0.25`，尾部时效 p95 超闸值 3/4 也提前报 warn（别等数据丢了才说）。

  - **汇总时机**：`_reset_round_state`（场次边界）先出「上一场」汇总再清零——整场不清零会被 50 场摊薄到看不见；模块 `finally` 收尾再出一次终值。`_ocr_total_runs/_ocr_failures` 是 debug HUD「运行次数/失败」的整轮口径，**仪表清零刻意不动它们**。

  - 写侧不变量：io 三项只由观察线程写、ocr/tick 项只由 Tasker 线程写（各为单写者），`read_perf_snapshot` 可被 sidecar handler 线程直读不加锁。回归锁见 `tests/test_treasure_perf_instrument.py`（含变异检验：退回全局口径会恰使两条校准测试变红）。

  - **进程 CPU 占用**：原语在 [core/cpu\_time.py](../../core/cpu_time.py)（ctypes `GetProcessTimes`，零依赖、非 Windows 返回 None），core 只给无状态累计秒数，**差分留在本域仪表**（`_sample_cpu` 每 tick 采一次，相邻差 ÷ 墙钟差 → 滑窗 p50/p95/max；多核可 >100%）。取不到时快照 `cpu.available=False`，GUI 按"无此项"渲染，**绝不拿 0% 冒充空闲**。单位坑（FILETIME tick = 100ns = 1e-7 秒，写成 `100e-7` 会放大 100 倍）由 `tests/test_cpu_time.py` 与 `time.process_time()` 对拍锁住——该文件只依赖标准库，CI 上真实执行（不像鉴宝测试整文件跳过）。

  - 空汇总噪音已消除：场次边界的汇总**只认 OCR 活动**（预热期常只有几帧落盘、OCR 全 0），但计数无条件在边界清零，防串场。

  - 关联坑：`ocr._pin_to_p_cores` 依赖 psutil，而 psutil 不在依赖里 → **P-core 绑核从未生效**（曾只记 DEBUG，静默近一月，现提 WARNING）。真机复验证明：不绑核、编码改 JPG 后丢弃率已回 0%、时效 p95 328ms，故绑核当前无可证明收益，暂不接 ctypes 实现。

- **帧供给独立于决策段（观察通路** **`treasure-observer`** **线程，2026-09-12）**：raw/rendered/PEEP 的产帧、帧号（`_saved_frames`/`_debug_saved` 唯一主人）与入队全在观察线程，节律 `OBSERVE_INTERVAL_MS=150`（存图）/ `PEEP_ONLY_INTERVAL_MS=50`（仅预览），帧只读 WGC 中心缓存。**为什么必须独立**：厅类 dwell（游戏大厅/活动页面/待机/控制器指引弹窗）按真机五炸定案不挂 `policy_loop`，而 `_tick_once` 只在 `policy_loop` 被路由时执行——帧工作若挂在 `_tick_once` 上，大厅类阶段会整段零帧（曾表现为「调试截图开关开着但保存帧数 0/0」）。观察线程的权限边界：不调 `_treasure_kwargs()`（内含 `_resolve_action_target()` 决策入口）、不碰检测/OCR/状态机；HUD 状态由决策段整体替换引用发布到 `_last_debug_kwargs`，观察线程只读引用，故两侧无锁。停止序：`_stop_observer()` → `_stop_io_worker()`（反了会漏收尾帧）。契约与验收见 `docs/plan/observe-split-plan.md`

- **投递时机**：出价阶段仅面板已开（S3，识别到智能出价按钮）才投递——H 就是输入框当前值（智能出价填入），面板未开（S1/S2）输入框区域是别的 UI，投递既浪费又误判

- 时效老化：`age = consume_time - captured_ts`，超 `OCR_MAX_AGE_MS=800` 丢弃

- 结果槽双槽（关键/全量）各自完整 dict 替换，不原地修改

**报价槽级固化（wait\_result 读 4 槽，`_bid_slots`** **状态机）**：

- 每槽 `{val, stable, locked, miss, consumed, output, hits}`：val=-1 未读；stable=连续一致帧数；locked=已固化（停止该槽 OCR）；miss=连续无输出帧数

- 固化：读数字同值→stable+1，异值→val=新值,stable=1（误读稳定不了没关系，反正连续 3 次一致才固化，`BID_SLOT_STABLE_FRAMES=3`）；**前置槽约束**=`前置槽读到过任何值（val≠-1）`放行本槽推进（不要求前置 locked，否则前置槽误读不稳定会拖死后续槽）

- 清空重读：未固化 + 已读值 + 连续 3 帧无输出（`BID_SLOT_MISS_LIMIT=3`）→ val 回 -1 重读。关键实现点：**必须对全部未固化槽统一做「本帧有无输出」判定**（只遍历 res 出现的 key 会让无输出槽 miss 永远加不上）

- 已固化槽停止识别：`_bid_dynamic_ocr_keys()` 剔除 locked 槽（固化→停止该回合该槽 OCR）

- 三口径统计：consumed=本帧被消费 / output=有输出 / hits=命中有效数字，debug 图 OCR 卡显示 `消费/输出/命中`（如 100/12/8）

- 快照构建：**4 槽全部 locked** 才替换 `_last_round_snapshot` 并放行 wait\_next（不发布半成品）

- 回合变化（`_bid_slots_round != r`）→ `_reset_bid_slots()` 重置

- **假下降沿误判坑（已修，用户拍板「读到报价即禁用」）**：wait\_result 后报价展示前（实测 \~7s），我方槽 OCR 读到"出价中"（submitted=False）→ 原逻辑判"未提交"回退 wait\_first 重报，每回合浪费 \~30 帧且压缩报价读取窗口。修复：本回合任意槽读到过报价（locked 或 hits>0）即证明我方已提交 → 禁用假下降沿判定；缓冲帧数按 wait\_result 帧率翻倍补偿（×2 保持 \~1.5s 动画缓冲时间）

**关键配置**：`FRAME_INTERVAL_MS=300`（主循环 \~3.3Hz）、`OCR_ZERO_ALLOWED_KEYS=('settle_my_income','settle_profit')`（0 值合法）

***

## 2. bid\_strategy 出价策略

[bid\_strategy.py](./strategy.py)（V3 秒杀火力基准，2026-09-05；V2 数据驱动 2026-08-16；设计文档 `docs/treasure_bid_strategy.md` + `docs/treasure_tick_dynamic_step_report.md`）

- 数据结构：`RoundSnapshot`（上一轮完整公开快照，策略唯一对手信息源）/ `BidContext`（决策输入，含 `opp_high_history` 对手逐轮最高）/ `BidDecision`（决策输出）/ `LureState`（逼价基线，V3 未启用）

- **V3 决策树（decide）**：收入铁律「钱只在第一名利润和亏钱第一名的 15%/10%/5% 分红里」推出双分支——
  ① R1/R2 observe：出 `min(H,余额)`（H 恒在利润线内，撞上低 K 线即低价拍中）；
  ② **对手已证明火力 M = max(历史各轮对手最高, 上轮快照对手最高)**——出价可回放，历史峰值才是真实上限（V2 用上轮价在 401 场被钓鱼降价 748900→500300→766810 骗掉反杀机会）；
  ③ M≤0（无快照/对手全 0）→ observe 式 `min(H,余额)` 等捡漏（**原"嘲讽 250"已删除**：0 与 250 都无分红顺位价值，H 价反而保留捡漏可能）；
  ④ 杀价 `P_win = ceil(K_r × (M + 缓冲))`，缓冲=价格桶×利润强度缩放（**willingness 意愿收缩模型已删除**——它是为上轮价基准打的补丁，M 基准天然免疫降价钓鱼）；
  ⑤ `P_win ≤ 买入线 且 ≤ 余额` → **win**（profit 线=0.9×V̂，egg 线=V̂+risk\_cap；「捡漏」不再是独立分支，M 低自然杀价低）；绝不裁剪后买入（2026-08-16 教训：裁剪买入=赌接盘）；
  ⑥ 杀不动 → **target\_second 卡第二吃分红彩票**（未拍中出价不花钱；分红仅当赢家亏钱才有），upper=min(M−u, cap, balance) 安全垫防对手 30% 退出率把我方顶成第一意外接盘；区间挤不下 → 紧贴价 → pass（仅剩余额 0 等场景，T=0 走通用输入链=合法弃权）。

- **phase 门控**（`_bid_phase`：wait\_first/wait\_next/bidding/wait\_result）：面板「关→开」上升沿只在等待相位有效才建新 bidding epoch，防模板抖动制造假 epoch；提交后 wait\_result，OCR 4 槽全部「固化」（见上槽级固化）才构建快照并放行 wait\_next

- **输入子状态机**（`_run_bidding_execute`，画面驱动）：输入框当前值 B（OCR `bid_result_amount_box` 实时读）对比目标价 T——B==T 点 `bid_confirm_red_btn`；B==0 或前缀不匹配点 `bid_numpad_clear`；前缀匹配输下一位 `bid_numpad_{d}`。不依赖「我点过了」内部标记，用户任何遗漏/改价都能自动纠正。**瞬空读防抖（2026-09-11 实机振荡）**：B==0 且锚点 `_bid_input_progress>0` 时不回头重输首位，按锚点指 `ts[锚点]`（与前缀分支同形，指纹锁天然去重）；B==0 持续超 `BID_ZERO_STABLE_MS=1500`（时间口径）才判真空清空重输。教训：输 8 后 OCR 瞬时读空 → 旧代码重输首位 8（fp 带 progress=1≠0 挡不住）→ B=88 → 清空 → 重输，一回合 19 秒振荡，用户视角=「不点出价」；回归 `tests/test_treasure_bid_phase_recovery.py::test_blink_zero_read_advances_by_anchor_not_restart`

- **附加回合**：`_extract_round_from_stage` 正则提取任意「第N回合」，`set_stage` clamp 到 5（附加回合数据统一写进第5回合槽），用原始数字判断回合切换以正确重置转场期

- `_bid_input_latest` 无条件更新：OCR 读到无数字（已清空/占位）→ 0，避免输入子状态机反复点✖死循环。注意"读到 0"不等于"框里是空"——输入中途数字弹起动画/ROI 残缺会瞬空读，消费侧须走锚点防抖（见上"瞬空读防抖"）

***

## 3. treasure\_detector 阶段检测器

[treasure\_detector.py](./detector.py)

**职责**：

- 按优先级（`stage_priority`）扫描 policy.json `perception` 数据面编译的 `DetectionPlan.detect_anchors`

- 同 ROI 多模板聚合匹配——统一走 `core.template_match`（TM\_CCOEFF\_NORMED 多尺度，默认阈值 0.75），**色彩空间按锚点声明**（`spec.<锚点>.colorspace`：默认 rgb；round\_big\_banner/result\_banner 等高成本锚点声明 gray，标定依据 `tools/experiments/v4-p4c-match/` 对拍报告）

- 匹配强度弱告警节流（同 ROI 每 30s 一次）

- 回合识别：roundN\_banner 模板 → 文件名解析回合号；横幅未命中时 OCR 兜底读「第N回合」小字

**核心接口**：`detect(frame_rgb) -> DetectResult`；结果支持旧式 `stage, round_no = detect(...)` 解包，同时提供 `scores`、`hit_anchor`、`active_used` 供 trace 还原。

P4c 起 detector 内不再有独立匹配实现与常量兜底：真源 = policy.json 数据面（P4b），plan 缺失（真源不可用）→ 阶段检测降级为空。模板读盘/热修（`mtime_ns + size` 指纹失效）收敛在 `template_match.load_template`，控制台替换模板后不会永久命中旧图。

**自定义阈值**：`result_banner=0.900`、`is_matching_btn=0.900`（spec 锚点 `threshold` 字段；result\_banner 另有 `arbitration.template_thresholds.result_auction_win_banner=0.60`）

**结算后弹窗链的区分口径**：领取分红后可能依次弹出 ①今日最高积分上涨 ②鉴宝等级提升 ③奖励结算（彩蛋），弹几个是随机的（也可能一个不弹）。弹窗会遮满全屏 → 弹窗存在期间检测器一定匹配不到大厅，弹窗全关后大厅才可见，因此"看到大厅"就是"弹窗已关"的可靠证据。三者中只有 ①② 有 ROI，**具体是哪个弹窗由 detector 的** **`_last_hit_roi_key`** **区分**：`daily_high_banner`=今日最高 / `egg_reward_title`=彩蛋 / 无命中=等级提升盲点。这三页在阶段表里合并为单一「结算弹窗」，`_accept_stage` 为此放行「结算弹窗→大厅」的回退。

***

## 4. treasure\_ocr 金额识别

[treasure\_ocr.py](./ocr.py)

**职责**：

- RapidOCR（rapidocr\_onnxruntime）薄封装，懒加载引擎、失败降级

- `recognize_amounts(frame, min_amounts=...)`：对 ocr 段 ROI 逐区识别 → 金额解析

- 金额提取加固：千分位逗号优先、重复逗号合并、`MIN_AMOUNT=10000` 过滤、7 位噪点前缀处理

- **CPU 亲和性**：`PIN_P_CORE_AFFINITY=[0..7]` 绑定 P-core（本机 Intel Alder Lake 8P+4E，E-core 推理慢 \~2.15 倍，详见 OCR\_LATENCY\_SPIKE\_ANALYSIS.md）

- `USE_CLS=False` 关闭方向分类

***

## 5. treasure\_renderer HUD 渲染

[treasure\_renderer.py](./renderer.py)

**职责**：复用调试渲染器，绘制鉴宝专属 HUD：

- 阶段/回合号、系统报价 H、估值区间、我方出价、排名

- 5 回合 H 历史折线图、玩家出价表

- **准星渲染**：`treasure_action`（程序想点击的位置，`_resolve_action_target` 输出）画黄色准星 + 目标说明

- 底部 12 阶段进度条、OCR 性能指标（total/failures/dur\_ms/age\_ms）

***

## 6. 校准与编辑工具（v4 形态）

- **画布编辑**：`tools/navkit/mpe.cmd` 起 mpelb（root=仓库根）并在浏览器打开 MPE——节点/ROI/模板引用直接编辑 v4 真源 `resources/pipeline/treasure.json`（round-trip 保真 P3a 实证）。

- **策略表**：`mpe.cmd` 同批打开 Studio 壳页（127.0.0.1:26530），切到「策略表」标签编辑 `resources/policy/treasure.policy.json`。

- **ROI 校准台 / 模板截取**：`tools/navkit/studio.cmd` 一次起 mpelb + Studio 服务（两者均隐藏窗口，零控制台弹窗），壳页三标签互切——ROI 校准台离线回放会话帧（`debug/treasure/<ts>/raw/`），拖框改 spec 锚点 rect / pipeline 节点 rect / `tuning` rect，保存时 `check_truth` 三闸随管线执行；模板截取页从截图裁模板图落 `resources/image/`。

- **运行时数据面**：detector/决策栈/ROI/感知裁剪全部读 `resources/policy/treasure.policy.json` 数据面（P4b 起）；决策引擎消费的域白名单、推导与副作用形收在同文件 `engine_contract` 段（改鉴宝事实/等待 key/tuning 键 = 改契约段，不改 core/navkit/policy.py）；编辑后用 `tools/navkit/check_truth.py` 机检。

- **校准截图来源**：`debug/treasure/<ts>/raw/`（GUI debug 图落盘），匹配行为离线验证可用 `tools/experiments/` 系列脚本。

***

## 7. 鉴宝类速查

### treasure\_module.TreasureModule

| 方法                                    | 说明                                                          |
| ------------------------------------- | ----------------------------------------------------------- |
| `start(start_from)`                   | 启动：连接窗口 → 装渲染器 → 初始化 OCR/检测器/模板 → 主循环 `_tick_once`（\~3.3Hz） |
| `_tick_once()`                        | 每帧：截图 → 阶段检测同步 → 鉴宝师/场次意图 → OCR 投递 → 变化检测 → save\_frame     |
| `_match_appraisers(frame)`            | 多尺度顺位匹配鉴宝师（P1→P2），返回 `[(prio,key,score,cxn,cyn,rx2)]`       |
| `_match_selected_check(frame)`        | 对勾扫描区匹配黄色√，返回 `(score,cxn,cyn)`                             |
| `_run_appraiser_choice(frame)`        | 选择鉴宝师阶段：匹配+选中判定 → 写 `_appr_last_decision` 意图                |
| `_match_session_panel(frame)`         | 详情卡标题匹配（状态判定用）                                              |
| `_run_session_choice(frame)`          | 鉴宝大厅阶段：标题判定 → 静态按钮中心意图写 `_session_last_decision`            |
| `_decide_action()`                    | 阶段驱动决策（返回 `{"key","hint"}`），全部 12 阶段准星覆盖                    |
| `_resolve_action_target()`            | 决策 → 补归一化 center（动态匹配/静态按钮/兜底中心）                            |
| `_treasure_kwargs()`                  | 统一构造 save\_frame/DebugState 字段（含 `treasure_action` 准星）      |
| `_ocr_push/pop_latest/publish_result` | 异步 OCR worker 投递/取帧/发布（latest-only + 两段式）                   |
| `set_h / set_our_bid / set_rank`      | 状态注入（系统报价/我方出价/排名），H 只取每回合第一次                               |

### treasure\_detector.TreasureStageDetector

| 方法                           | 说明                                                                                                         |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `detect(frame_rgb)`          | 返回 `DetectResult`（兼容二元组解包）：按 v4 DetectionPlan 扫描（template\_match 引擎，按锚点 colorspace）+ scores/hit\_anchor 明细 |
| `_round_from_template(name)` | roundN\_banner 文件名 → 回合号                                                                                   |
| `_round_no_from_text(text)`  | OCR 文本提取回合号（1\~5 之外视为噪声）                                                                                   |
| `_round_label_rect()`        | 回合小字 OCR 区 rect（优先 ocr.round\_label\_area）                                                                 |

### treasure\_ocr.TreasureOcr

| 方法                                          | 说明                               |
| ------------------------------------------- | -------------------------------- |
| `recognize_amounts(frame, min_amounts=...)` | ocr 段 ROI 逐区识别 → `{key: amount}` |
| `_extract_amount(...)`                      | 金额解析（千分位/逗号合并/MIN\_AMOUNT 过滤）    |
| `_get_engine()`                             | RapidOCR 懒加载（失败降级，绑定 P-core）     |

### treasure\_renderer.TreasureDebugRenderer

| 方法                            | 说明                             |
| ----------------------------- | ------------------------------ |
| `render_full(img_bgr, state)` | 全量 HUD + 准星绘制（save\_frame 存盘用） |
| `_draw_appraiser_peep(...)`   | 选择鉴宝师阶段准星（目标头像/确认按钮/中心兜底）      |

***

## 8. 鉴宝模板清单

配置源 policy.json `perception.spec`（rect/threshold/colorspace 逐项）；匹配阈值：全局默认 0.75，鉴宝师代码回退默认 0.72（真源已校准 0.8）、对勾默认 0.62、智能出价按钮默认 0.72（锚点 `threshold` 逐项覆盖）：

| ROI 键                      | 模板文件                                                     | 阶段/用途                                                                                         | 阈值       |
| -------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------- | -------- |
| `settle_title`             | settle\_final\_price\_title.png                          | 结算页标题                                                                                         | 0.75     |
| `result_banner`            | result\_auction\_fail/win\_banner.png                    | 中标结算横幅（自定义 0.90）                                                                              | **0.90** |
| `smart_bid_btn`            | bid\_smart\_btn.png                                      | 智能出价按钮（面板开强信号，JSON 可覆盖/回退 0.72）                                                               | **0.72** |
| `round_big_banner`         | round1\~5\_banner.png                                    | 回合大横幅（文件名解析回合号）                                                                               | 0.75     |
| `appraiser_title`          | select\_appraiser\_title.png                             | 选择鉴宝师页标题                                                                                      | 0.75     |
| `hall_peak_appraise_card`  | hall\_peak\_appraise\_card.png                           | 游戏大厅「巅峰鉴宝」入口卡片                                                                                | 0.75     |
| `goto_appraise_btn`        | act\_goto\_appraise\_btn.png                             | 活动页「前往鉴宝」按钮                                                                                   | 0.75     |
| `hall_session_cards`       | hall\_session\_cards.png                                 | 鉴宝大厅场次卡片区                                                                                     | 0.75     |
| `is_matching_btn`          | is\_matching\_btn.png                                    | 匹配中按钮（自定义 0.90）                                                                               | **0.90** |
| `session_start_match_btn`  | session\_start\_match\_btn.png                           | 「开始匹配」按钮（详情卡出现判定，自定义 0.90）                                                                    | **0.90** |
| `appraiser_selected_check` | appraiser\_selected\_check.png                           | 已选中黄色√（对勾判定）                                                                                  | 0.62     |
| —（actions 段）               | —                                                        | session\_master\_badge / session\_start\_match\_btn / confirm\_red\_btn 等纯 rect 中心按钮，**不挂模板** | —        |
| —（鉴宝师模板）                   | appraiser\_p1\_caroline.png / appraiser\_p2\_shotaro.png | 选择鉴宝师顺位匹配（全屏多尺度，JSON 可逐项覆盖）                                                                   | 0.80¹    |

> 注：`hall_session_cards` 曾名 `hall_start_match_btn`；`hall_peak_appraise_card` 曾名 `hall_participation_card`（v0.13.0-dev.3/4 语义化改名）。已删除 `round_label_*.png`（回合小字改 OCR）。
>
> ¹ 鉴宝师 `threshold` 已在真源 spec `appraiser_p*` 锚点校准为 0.80（代码回退默认 `_APPRAISER_MATCH_THRESHOLD=0.72`，MPE/薄页可逐项覆盖）。

***

## 9. 鉴宝坑点

| 坑点                                    | 说明                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | <br /> | <br /> | <br />                       |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----- | :----- | :--------------------------- |
| 准星意图模式                                | 当前全部逻辑只算「程序想点击的位置」，经 `_decide_action → _resolve_action_target → _treasure_kwargs → debug.save_frame` 渲染 PEEP 准星，**不执行真实点击**（已删除 `_click_norm`）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | <br /> | <br /> | <br />                       |
| 锚点 kind 分类语义                          | policy.json `perception.spec` 按 `kind` 分类：**template = 模板匹配做阶段/状态判定**（如 `session_start_match_btn` 判详情卡出现、`is_matching_btn` 判匹配中）；**point = 纯 rect 中心点击按钮**（准星直接用中心，不挂模板，如 `session_master_badge`/`session_expert_badge`/`session_intern_badge`/`confirm_red_btn`）；**ocr = RapidOCR 识别区**。按钮位置固定就做成 point，别挂模板                                                                                                                                                                                                                                                                                                                                         | <br /> | <br /> | <br />                       |
| 鉴宝师/场次多尺度匹配                           | 0.70\~1.30× 共 13 档（步长 0.05），缩小时 `INTER_AREA`/放大 `INTER_CUBIC`（P4c 起该口径在 `template_match._best_match` 唯一实现）；中心/右边界用**缩放后模板尺寸**计算（不是原始尺寸）；鉴宝师代码默认 0.72，spec 逐项覆盖为 0.80；对勾默认 0.62                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | <br /> | <br /> | <br />                       |
| rgb 帧成本与按锚点色彩空间                       | 实测 rgb 匹配成本 ≈ 灰度 2.4×（4 段真机录帧 812 帧，`tools/experiments/v4-p4c-match/`）；大搜索区/横幅类锚点（鉴宝师头像、对勾、开始匹配、round\_big\_banner、result\_banner）在 spec 声明 `colorspace: "gray"` 保帧间隔与历史校准，其余缺省 rgb。灰度豁免集是**契约**（`test_navkit_truth.test_spec_colorspace_contract` 锁死）——增删任何锚点的色彩空间前先重跑对拍脚本。翻转风险：小目标彩图分对 jpg 色度噪声敏感（smart\_bid 最差 -0.22 但 100/100 命中保持），换阈值前先想 colorspace                                                                                                                                                                                                                                                                                             | <br /> | <br /> | <br />                       |
| 光标遮挡防线（P4c 定稿）                        | 反应式躲避已从架构退役：光标停在点击点属常态画面，识别可靠性靠 colorspace 校准 + 稳定帧判定（面板 `PANEL_OPEN_MIN_STABLE_FRAMES`、选师 `APPRAISER_SETTLE_FRAMES`、阶段防抖）；图侧锚点可按需开 `mask_cursor`（光标真值 = `Clicker.gamepad_cursor_pos` → `NavGraph.cursor_pos`，对局内外共享同一 Clicker 实例）。真机验收若发现遮挡漏检：优先录帧复现，再决定调阈或开 mask\_cursor，**不要回加反应式躲避**                                                                                                                                                                                                                                                                                                                                                              | <br /> | <br /> | <br />                       |
| 阶段感知动态激活                              | 非标准窗口（DPI 缩放）下画面模糊 → 单点匹配分不稳定（如 smart\_bid\_btn 多尺度仅 0.686，达不到 `_SESSION_MATCH_THRESHOLD` 0.90 → 面板判未开 → 不点智能出价）。`treasure_module._STAGE_PERCEPTION` 按阶段只激活「当前画面必然出现/相关」的 stage ROI，`detect(active_rois)` 只扫交集；全局锚点 `_GLOBAL_ANCHORS`（`hall_peak_appraise_card` 掉回大厅兜底）始终全量并入。阶段未登记 → 回退全量（安全兜底）。OCR 同理按 `_STAGE_OCR_KEYS` 裁剪 worker 第二段 keys。**新阶段必须登记感知清单**（含转移信号，如出价阶段必须含 settle\_title/result\_banner），否则只跑锚点 → 永不切换。smart\_bid\_btn 阈值已解耦：读 JSON `stage.smart_bid_btn.threshold`，缺省回退 `_SMART_BID_MATCH_THRESHOLD=0.72`（不可复用 0.90）                                                                                                               | <br /> | <br /> | <br />                       |
| 「已选中」对勾判定                             | `stage.appraiser_selected_check` 是横向长条 rect（覆盖三卡右上角对勾高度带），扫描黄色√；判定对勾中心 X ≈ 目标卡片命中框右边界（容差 0.09）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | <br /> | <br /> | <br />                       |
| 鉴宝师搜索区                                | `_APPRAISER_SEARCH_ROI=(0.03,0.18,0.97,0.92)` 全屏范围（三卡位置/尺寸不固定），顺位 P1 卡洛琳→P2 章太郎，均未命中→准星指屏幕中心                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | <br /> | <br /> | <br />                       |
| 回合出价状态机                               | `_run_bidding_choice`：S0 转场期/S1 等待/S2 点主出价按钮/S3 面板内智能出价→确认出价；「等待/出价」用 OCR 文字判（`ocr.bid_main_btn_label`），面板是否打开用 `stage.smart_bid_btn` 模板判；等待状态 `key=None` → `_resolve_action_target` 返回 None 不出准星                                                                                                                                                                                                                                                                                                                                                                                                                                                     | <br /> | <br /> | <br />                       |
| 出价按钮明暗                                | 主出价按钮「等待出价/出价」明暗态**不要用模板匹配**（禁用态透明渐变 + 亮度变化 → 置信度跳变，见 Experience 1112416），改用 OCR 文字判状态                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | <br /> | <br /> | <br />                       |
| `_load_selected_check` 解包             | `_, fname = _SELECTED_CHECK_DEF` 是**二元组**；按三元组解包会报 `not enough values to unpack (expected 3, got 2)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | <br /> | <br /> | <br />                       |
| 调试台黑屏                                 | 截图正则 `_RAW_RE` 必须覆盖 \`png                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | jpg    | jpeg   | webp\`（原始存盘是 JPG，只认 png 会全黑） |
| 调试台框交互                                | 框显示开关 `showRois` 需同步 `hitTest()`（none→全部不响应；selected→仅选中项响应），否则隐藏的框仍可被点中/拖动                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | <br /> | <br /> | <br />                       |
| 回合小字                                  | 已由模板像素差改为 OCR 识别（`round_label_area` 迁入 ocr 段），`round_label_*.png` 模板已删除                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | <br /> | <br /> | <br />                       |
| pyright 类型噪音                          | `tuple(float(n) for n in list)` 会被推断为 `tuple[float,...]`，赋给 `tuple[float,float,float,float]` 报错 → 用显式 4 元构造 `(float(r[0]), float(r[1]), float(r[2]), float(r[3]))`；多尺度 `best` 元组是 **6 元**（score,scale\_idx,x,y,th,tw）                                                                                                                                                                                                                                                                                                                                                                                                                                 | <br /> | <br /> | <br />                       |
| 主循环单帧异常兜底                             | `start()` 主循环**必须**对 `_tick_once()` 做 per-frame `try/except`，否则单帧未捕获异常会直接杀死整个主循环/模块线程（2026-08-19 核实原实现是 `try/finally` 无 `except`）。正确做法：单帧异常跳过继续并 `WARNING`，仅当连续 `_MAIN_CRASH_RETRY_MAX=30` 帧（≈9s）仍异常才上抛走 `finally` 清理终止，防"静默空转"掩盖真 bug                                                                                                                                                                                                                                                                                                                                                                                                                | <br /> | <br /> | <br />                       |
| **按钮点击重试规范**（2026-09-05 定稿，新增按钮默认照此写） | 重试分三层，每层语义不同、不得混淆：**① 执行失败层**：ok=false（物理点击失败）→ 指纹不更新 → 下帧同意图自动重试（无限，直到成功/意图变化）；**② 无响应兜底层**：ok=true（物理成功）但「成功信号」未出现 → 超时后**清指纹重新 arm** 再点（防边沿触发锁死），**必须封顶** `_RETRY_MAX=3`（含首点共 4 次）；**③ 耗尽后果层**：重试封顶后**不得静默**，抛 `ClickRetryExhaustedError` 终止模块（用户可见、可干预）。关键点：每个 key 必须显式声明「成功信号」——阶段切换类 = 阶段名切走（`_maybe_retry_stage_click`）；面板内/数据类 = **指定 OCR 字段变化**（如智能出价 H 读出、出价面板关闭、本场收入读出）；弹窗类 = 离开弹窗阶段（per-key 帧数可覆盖 `CLICK_RETRY_FRAMES_BY_KEY`，上限统一）。**新增按钮加入** **`CLICK_RETRY_KEYS`** **时必须同步定义成功信号**，否则"ok=true 但无效果"仍会指纹锁死静默卡死                                                                                                                          | <br /> | <br /> | <br />                       |
| 领取分红「跳过动画」无响应兜底                       | `settle_collect_red_btn` 有两次点击语义：**跳过动画**（首次，成功信号=收入读出）与**真领取**（收入已读出，成功信号=阶段切走）。跳过动画点击物理成功但游戏无响应（实测：点后按钮/动画无反应、收入永远读不到）原逻辑静默卡死。2026-09-05 修复：`_decide_action` dividend\_waiting 分支加 `SETTLE_SKIP_RETRY_FRAMES=10` 超时 + `SETTLE_SKIP_RETRY_MAX=3` 封顶，超时清指纹重试，耗尽抛 `ClickRetryExhaustedError` 终止。重试指纹带 `clicked_once=True` 位与首点不同不撞指纹锁；`_apply_click_success` 每次点击成功重启计时防连点风暴；收入读出（OCR 写入）归零计数                                                                                                                                                                                                                                                          | <br /> | <br /> | <br />                       |
| 出价预测基准必须用「已证明火力」                      | 密封拍卖+秒杀成交（当回合第一/第二≥K\_r 即成交）下，对手**历史最高报价 = 可回放的支付意愿下限**；单轮报价含「钓鱼蓄力」噪声（实测 401 场：P3 报 748,900→降 500,300→末轮 766,810 秒杀成交赚 26.9 万，我方利润线 893,836 内本可反杀未杀）。任何"按对手价出牌"的逻辑，基准一律 `M = max(历史各轮对手最高, 上轮快照对手最高)`，禁止只取上轮价；更禁止为"上轮价回落"设计收缩补丁（V2 willingness 把降价读成撤退、双重低估火力，V3 已删）。配套收入铁律：未拍中出价不花钱、分红仅在赢家亏钱时存在——能杀必杀（买入线内），杀不动卡第二吃分红彩票（strategy.py V3，2026-09-05）                                                                                                                                                                                                                                                                                                   | <br /> | <br /> | <br />                       |
| 落盘编码抢占 OCR 时效                         | 调试落盘的 rendered 编码曾定 WEBP q95：单帧 77.7ms（q50 仍 60ms，OpenCV webp 单线程且成本几乎不随质量下降）。观察通路把产帧节律固定成 150ms 后，IO worker 占空比冲到 60%、队列（上限 8）长期满并丢帧（真机 824 产 / 712 落盘 = 13.6%），与 OCR worker 争抢 CPU → OCR 第二段结果从 p50 27ms 被拖到 575\~783ms（**尾部放大器：`PIN_P_CORE_AFFINITY`** **绑核从未生效**——psutil 不在 `dependencies`，每个跑过 OCR 的会话都是 `亲和性绑定失败(No module named 'psutil')，忽略`，且只记 DEBUG，所以 E-core 漂移对策空转了一个月）→ 越过 `OCR_MAX_AGE_MS=800` 的超龄丢弃率 **0%→18.8%** → 公开报价窗口（游戏侧只有 \~1.5s）内 P1\~P3 整段丢光，表现为「debug 表历史回合列全空 + 快照建不出 + epoch 卡死数十秒」。判据看 `log_perf_summary` 的会话汇总行（丢弃率、报价窗口分桶、落盘丢帧、帧间隔分位一次给全），不必再手扫日志。**换 JPG q85（3.9ms）即解**；排查同类问题先看丢弃率，别看 p50 耗时（p50 全程没变，回归只体现在尾部与队列丢帧） | <br /> | <br /> | <br />                       |

## 10. 遗留问题清单（v1.0 发布前核查）

| 项                   | 类别      | 结论 / 处理                                                                                                                                                                               |
| ------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 主循环无 per-frame 异常兜底 | 稳定性 bug | ✅ 已修复：`start()` 对 `_tick_once` 包 try/except，单帧异常跳过，连续 30 帧才终止（见坑点表）。`py_compile` 通过                                                                                                   |
| 阶段切换类点击卡死           | 稳定性     | ✅ 已核实无需改：`_maybe_retry_stage_click` 用 `CLICK_RETRY_MAX=3` 封顶，达上限 WARNING 停止，换 key/切阶段归零，有界无死循环                                                                                        |
| 结算后弹窗连点/跳过          | 稳定性     | ✅ 已修复（既有）：`POPUP_CLICK_COOLDOWN_FRAMES=5` 冷却 + `POPUP_LOOPBACK_STABLE_FRAMES=3` 连续稳定帧确认，详见坑点表弹窗链相关条目                                                                                  |
| 领取分红跳过动画点击无响应卡死     | 稳定性     | ✅ 已修复（2026-09-05）：`dividend_waiting` 加 `SETTLE_SKIP_RETRY_FRAMES=10` 超时 + `SETTLE_SKIP_RETRY_MAX=3` 封顶，超时清指纹重试，耗尽抛 `ClickRetryExhaustedError` 终止（见坑点表「领取分红跳过动画无响应兜底」）。`py_compile` 通过 |
| 按钮重试规范未成文           | 规范      | ✅ 已定稿（2026-09-05）：三层重试语义（执行失败无限 / 无响应封顶 3 次 / 耗尽终止）+ 每 key 显式成功信号，见坑点表「按钮点击重试规范」，新增按钮默认照此写                                                                                            |

<br />
