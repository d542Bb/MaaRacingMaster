# MaaRacingMaster — Code Wiki · 鉴宝域

> 《巅峰极速》"巅峰鉴宝"活动 —— **出价 / 估值 / OCR 全自动模块（treasure\_\*）** 专属文档。
> 聚焦鉴宝核心：阶段状态机（真源 = `resources/policy/treasure.policy.json` `perception.stages.order`）/ 准星意图 / 出价策略（bid\_strategy）/ 异步 OCR / ROI 三段分类。
>
> **信源等级**：L2 —— 可作「鉴宝域现在怎么工作、改它要注意什么」的直接依据；不得作为游戏规则（L0，见 RULES.md）与节点/策略取值（L0，见 `resources/`）的最终依据。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../../AGENTS.md)；本文只补充本域特有约束。
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

- 活动模块实现（`ActivityModule` 子类，`ID="treasure"`），阶段状态机（阶段清单读 policy.json `perception.stages.order`，文档不抄录数值）

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

- `_match_session_panel`：模板匹配「开始匹配」按钮 `session_start_match_btn`（stage 段）判"按钮是否在屏幕上"——按钮可见**不能**判"已选目标场次"（详情卡默认已打开、任意场次都带此按钮）

- **单次进阶段内序钉死「先点目标场次标签，再开始匹配」**（2026-09-15 修复）：闸门 `_session_badge_clicked` 由 `_apply_click_success` 在点中目标场次 badge 成功时置位，离开阶段/新一场清零。未过闸 → 意图恒为 badge（`session_intern_badge`/`session_expert_badge`/`session_master_badge`，静态中心）；过闸后 → 命中才给「开始匹配」（actions 段静态中心），未命中纯等待 `session_waiting`，阶段内 badge 不再出现。旧逻辑以"按钮模板命中"判已切目标场次 → 进大厅直接点开始匹配（没点场次）、点完在检测器确认「匹配中」前又倒序点 badge（01:08:26 事故；2026-08-16 按帧冷却治标不治本，机制与 tuning 键 `session_start_click_cooldown_frames` 一并移除）。点击落空收口交阶段切换重试框架（时间窗口重 arm、封顶抛错）。降级模式（无模板）过闸后直接给开始匹配意图。回归锁 `tests/test_treasure_session_badge_gate.py` 六条

- 按钮中心来自 `_load_action_centers`（同时扫 JSON 的 stage+actions 两段）

**每日循环收尾**（到限 → 「彩蛋任务」领取）：

- `_tick_once` 0.05 到限稳定 3 帧确认（且阶段=鉴宝大厅(选择场次)、本运行未进过）后不再直接停：进 `_run_egg_claim_chain()`——决策 tick 内**阻塞自包含顺序例程**（抓帧→匹配→同步点击→等待）。帧源仍是 WGC 中心缓存只读，不新起截图通路；Tasker 被占用 = 常驻图停摆，活动页「前往鉴宝」自动边没有触发机会，收尾方向冲突天然消解（因此不改图拓扑、不加旗标闸门）。

- **链内点击与主链路同一条出口协议**（2026-09-14 修复）：`_egg_chain_click` 提交前先 `_egg_chain_drain_slot` 消化任务槽遗留结果（链在决策段消费点**之前**触发，槽里必有上一帧的提交）、意图开关取自 `ctx.intent_mode`（不再硬编码覆盖共享 Clicker 的状态）、结果经 `_record_click` 统一落事件与日志、提交被拒或结果超时有界重试 `EGG_CHAIN_CLICK_RETRY_MAX` 次。此前链内自成一套出口，实测三处后果：仅意图开关对链内点击失效（鼠标模式一路真点击）、首次点击必被 `is_busy` 拒掉（返回键命中置信度 1.000 仍报「未点中」而放弃整链）、链内点击完全不可观测。回归锁 `tests/test_treasure_egg_claim.py` 七条（意图开关跟随 / 先消费后提交 / 有界重试 / 结果留痕 / drain 超时先中止再补drain / 重试前消化中止结果 / 链入口作废在途任务）。

- **在途任务作废（三道防线）**（2026-09-14 修复）：每日上限拦截带 3 帧确认窗，窗内主链路照常决策并可能已提交一次手柄点击，链启动时任务槽被在途点击占住——real 模式点击 0.4s 瞬时完成故从不触发，手柄模式则整链卡在「任务槽被占用未释放」。三道防线：① 链入口 `_egg_chain_abort_pending` 先 `cancel()` 作废在途点击再 drain（上限既到，那个点击已无意义）；② `_egg_chain_click` 的 drain 首窗超时后同样先 cancel 再补一轮 drain，不再直接放弃；③ 链内「结果迟迟不回」cancel 后必补 drain——任务槽对 DONE 态也算忙，不消化中止结果则后续重试的 submit 必被拒。回归锁同文件三条（drain 超时先中止再补 drain / 重试前消化中止结果 / 链入口作废在途任务）。

- **链等待循环忙旋饿死导航 worker——手柄链 12s 静默根因**（2026-09-14 定案修复）：链的 drain 与等结果两处轮询传 `lifecycle.sleep(0.05)`，而 `LifecycleAdapter.sleep` 旧实现按 `int(seconds/0.1)` 量化迭代，小于 0.1s 的入参**静默退化为一次都不睡**——决策线程以数十万次/秒空转（每迭代抢导航锁+发布快照），持 GIL 饿死导航 worker（真机：单任务 12s 不 DONE、abort 标志 6s 无人看一眼、光标 25px/11s 爬行）与观察线程（raw 帧间隔 0.16s→2.4–4.5s，链结束即恢复）。同症状在 pipeline 点击不出现，因链外等待用的是真 `time.sleep`——**双路径 diff 快于运行时取证**。判据签名：等待窗迭代数远超 入参/粒度 的期望（链两处实测 6s 窗 245 万次 vs 期望 ≈120）。原语修复与语义机检见 `docs/CODE_WIKI.md` §5.6 `ctx.lifecycle.sleep` 行与 `tests/test_capabilities_lifecycle_sleep.py`；曾据错误归因加的导航任务总时长上限（`NAV_TASK_TIMEOUT_S`）已随复验回撤，导航时长由步数上限天然有界。

- **链内进度写 trace**（`_egg_chain_trace`，事件名 `egg_chain`）：链阻塞在决策段内，`_tick_once` 的帧 trace 在链期间不再产生——不补记则整段领取过程在 trace 里是空白，事后无法回溯「走到哪一步、哪一步失败」。记 `start`（含 `click_mode`/`intent_mode`，可直接回答「为什么没领到」）/ `click` / `click_skipped` / `click_timeout` / `panel_opened` / `claim_done` / `give_up` / `error`。链内失败路径同时收口到统一出口 `_give_up`（日志 + trace + 离场），不再各写各的。PEEP 叠加层同理会被链冻结（`_last_debug_kwargs` 停止发布）——`_egg_chain_refresh_peep` 在 drain/click/wait 三处轮询中浅拷贝刷新两个手柄字段（导航进度 `treasure_gamepad_cursor`、候选快照 `treasure_cursor_cands`），其余字段维持链前快照；链恰是用户最需要看「导航在干嘛」的时段（真机 2026-09-14：链内点击三连失败，PEEP 全程无进度可看）。

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

  - 关联坑：`ocr._pin_to_p_cores` 依赖 psutil，而 psutil 曾不在依赖里 → **P-core 绑核从未生效**（曾只记 DEBUG，静默近一月，现提 WARNING；2026-09-14 psutil 补为正式依赖、绑核开始生效）。真机复验证明：不绑核、编码改 JPG 后丢弃率已回 0%、时效 p95 328ms，故绑核当前无可证明收益，暂不接 ctypes 实现。

- **帧供给独立于决策段（观察通路** **`treasure-observer`** **线程，2026-09-12）**：raw/rendered/PEEP 的产帧、帧号（`_saved_frames`/`_debug_saved` 唯一主人）与入队全在观察线程，节律 `OBSERVE_INTERVAL_MS=150`（存图）/ `PEEP_ONLY_INTERVAL_MS=50`（仅预览），帧只读 WGC 中心缓存。**为什么必须独立**：厅类 dwell（游戏大厅/活动页面/待机/控制器指引弹窗）按真机五炸定案不挂 `policy_loop`，而 `_tick_once` 只在 `policy_loop` 被路由时执行——帧工作若挂在 `_tick_once` 上，大厅类阶段会整段零帧（曾表现为「调试截图开关开着但保存帧数 0/0」）。观察线程的权限边界：不调 `_treasure_kwargs()`（内含 `_resolve_action_target()` 决策入口）、不碰检测/OCR/状态机；HUD 状态由决策段整体替换引用发布到 `_last_debug_kwargs`，观察线程只读引用，故两侧无锁。停止序：`_stop_observer()` → `_stop_io_worker()`（反了会漏收尾帧）。

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

[bid\_strategy.py](./strategy.py)（V3 秒杀火力基准，2026-09-05；V2 数据驱动 2026-08-16）

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

**结算双阶段 = 同一视觉页的两个状态**（2026-09-15 定案，勿再按"配置错误"处理）：「中标结算」与「领取分红」在 policy 里的感知集与 OCR 集**完全相等**（active 同为 settle\_title/result\_banner/daily\_high\_banner/egg\_reward\_title，ocr 同为 settle\_\* 四个金额信号）——结算页是一块连续画面，阶段语义由本帧命中的转移锚点裁决（`result_banner`（竞拍结果横幅）命中 → 中标结算；`settle_title`（结算页标题）命中 → 领取分红，判定归 detector 优先级扫描，两页切换由 transitions 表 `stage` 列约束）。**调共用锚点的 rect/阈值 = 同时改两页**，标定前两侧画面都要复验。锚点与阶段的 `page` 字段（如 settle/payout）只是 ROI Studio 的**视觉页分组真源**（分组展示 + E09 新增校验 + `check_truth.page_checks` 闭合机检），不参与运行时归属——「哪个阶段扫哪些信号/锚点」唯一真源 = `stages.definitions[*].active/ocr`（ADR-0002），代码中不得另存第二份。

***

## 4. treasure\_ocr 金额识别

[treasure\_ocr.py](./ocr.py)

**职责**（**业务薄层**：识别引擎的真源在 [core/ocr.py](../../maaracing_master/core/CODE_WIKI.md) §1.11，本文件不再持有第二份）：

- 识别区 `_regions`：读 policy.json `perception.spec` 筛 `kind == "ocr"` 的锚点 rect

- `recognize_amounts(frame, min_amounts=...)`：对 ocr 段 ROI 逐区识别 → 金额解析

- `recognize_single(frame, rect, min_amount=...)`：单 ROI 即时识别（调试台手拖选区、结算弹窗等临时读区；`eggs.py` 经构造参数注入同一个 ocr 对象复用）

- 金额提取加固：千分位逗号优先、重复逗号合并、中文「万」单位、7 位噪点前缀截首、`MIN_AMOUNT` 金额下限（下限值与区间口径见源码常量）

- 抠图 → 预处理 → RapidOCR 推理、以及关检测/关方向分类/ORT 线程数/P-core 绑核那套调参**已上提 core**：插件自包含契约禁止 speedrush import 本模块，故引擎只能住公共层；改参去 [core/ocr.py](../../maaracing_master/core/ocr.py)，识别结果的行为不变锁见 `tests/test_core_ocr.py`

***

## 5. treasure\_renderer HUD 渲染

[treasure\_renderer.py](./renderer.py)

**职责**：复用调试渲染器，绘制鉴宝专属 HUD：

- 阶段/回合号、系统报价 H、估值区间、我方出价、排名

- 5 回合 H 历史折线图、玩家出价表

- **准星渲染**：`treasure_action`（程序想点击的位置，`_resolve_action_target` 输出）画黄色准星 + 目标说明

- **手柄诊断层**（`_draw_gamepad_diag`）：光标实时位绿圈 + 识别候选圈（绿=选中 / 黄=次选），**与点击意图解耦**——`treasure_action` 为空（转场/未定义过渡）或 center 为空（纯等待）时照常绘制，只有该层整层消失才是 bug；快照带 `stale`/`age_s` 时改暗色并标「上次识别 N.Ns 前」「候选快照: 陈旧」（2026-09-15 定稿）

- 底部阶段进度条（前端按 RPC 下发的 stages 列表动态渲染，不写死数量）、OCR 性能指标（total/failures/dur\_ms/age\_ms）

***

## 6. 校准与编辑工具（v4 形态）

- **画布编辑**：`tools/navkit/mpe.cmd` 起 mpelb（root=仓库根）并在浏览器打开 MPE——节点/ROI/模板引用直接编辑 v4 真源 `resources/pipeline/treasure.json`（round-trip 保真 P3a 实证）。

- **策略表**：`mpe.cmd` 同批打开 Studio 壳页（127.0.0.1:26530），切到「策略表」标签编辑 `resources/policy/treasure.policy.json`。

- **ROI 校准台 / 模板截取**：`tools/navkit/studio.cmd` 一次起 mpelb + Studio 服务（两者均隐藏窗口，零控制台弹窗），壳页三标签互切——ROI 校准台离线回放会话帧（`debug/treasure/<ts>/raw/`），拖框改 spec 锚点 rect / pipeline 节点 rect / `tuning` rect，保存时 `check_truth` 各闸（图自洽/分层红线/交叉互洽/几何/页面清单等集/页面归属闭合 `page_checks`）随管线执行；锚点 `page` 是 Studio 侧视觉页分组真源（分组展示 + E09 新增校验），闭合性由 `page_checks` 机检（惰性锚点如 egg\_task 族可用独立页面，被阶段表/转移引用的不得逃逸）；模板截取页从截图裁模板图落 `resources/image/`。

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
| `_decide_action()`                    | 阶段驱动决策（返回 `{"key","hint"}`），覆盖全部阶段（以 policy.json stages 为准）  |
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

> 注：`hall_session_cards` 曾名 `hall_start_match_btn`；`hall_peak_appraise_card` 曾名 `hall_participation_card`（v0.13.0-dev.3/4 语义化改名）。
>
> ¹ 鉴宝师 `threshold` 已在真源 spec `appraiser_p*` 锚点校准为 0.80（代码回退默认 `_APPRAISER_MATCH_THRESHOLD=0.72`，MPE/薄页可逐项覆盖）。

***

## 9. 鉴宝坑点

| 坑点 | 说明 |
| --- | --- |
| 准星意图模式                                | 当前全部逻辑只算「程序想点击的位置」，经 `_decide_action → _resolve_action_target → _treasure_kwargs → debug.save_frame` 渲染 PEEP 准星，**不执行真实点击**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 锚点 kind 分类语义                          | policy.json `perception.spec` 按 `kind` 分类：**template = 模板匹配做阶段/状态判定**（如 `session_start_match_btn` 判详情卡出现、`is_matching_btn` 判匹配中）；**point = 纯 rect 中心点击按钮**（准星直接用中心，不挂模板，如 `session_master_badge`/`session_expert_badge`/`session_intern_badge`/`confirm_red_btn`）；**ocr = RapidOCR 识别区**。按钮位置固定就做成 point，别挂模板                                                                                                                                                                                                                                                                                                                                          |
| 鉴宝师/场次多尺度匹配                           | 0.70\~1.30× 共 13 档（步长 0.05），缩小时 `INTER_AREA`/放大 `INTER_CUBIC`（P4c 起该口径在 `template_match._best_match` 唯一实现）；中心/右边界用**缩放后模板尺寸**计算（不是原始尺寸）；鉴宝师代码默认 0.72，spec 逐项覆盖为 0.80；对勾默认 0.62                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| rgb 帧成本与按锚点色彩空间                       | 实测 rgb 匹配成本 ≈ 灰度 2.4×（4 段真机录帧 812 帧，`tools/experiments/v4-p4c-match/`）；大搜索区/横幅类锚点（鉴宝师头像、对勾、开始匹配、round\_big\_banner、result\_banner）在 spec 声明 `colorspace: "gray"` 保帧间隔与历史校准，其余缺省 rgb。灰度豁免集是**契约**（`test_navkit_truth.test_spec_colorspace_contract` 锁死）——增删任何锚点的色彩空间前先重跑对拍脚本。翻转风险：小目标彩图分对 jpg 色度噪声敏感（smart\_bid 最差 -0.22 但 100/100 命中保持），换阈值前先想 colorspace                                                                                                                                                                                                                                                                                              |
| 光标遮挡防线（P4c 定稿）                        | 反应式躲避已从架构退役：光标停在点击点属常态画面，识别可靠性靠 colorspace 校准 + 稳定帧判定（面板 `PANEL_OPEN_MIN_STABLE_FRAMES`、选师 `APPRAISER_SETTLE_FRAMES`、阶段防抖）；图侧锚点可按需开 `mask_cursor`（光标真值 = `Clicker.gamepad_cursor_pos` → `NavGraph.cursor_pos`，对局内外共享同一 Clicker 实例）。真机验收若发现遮挡漏检：优先录帧复现，再决定调阈或开 mask\_cursor，**不要回加反应式躲避**                                                                                                                                                                                                                                                                                                                                                               |
| 阶段感知动态激活                              | 非标准窗口（DPI 缩放）下画面模糊 → 单点匹配分不稳定（如 smart\_bid\_btn 多尺度仅 0.686，达不到 `_SESSION_MATCH_THRESHOLD` 0.90 → 面板判未开 → 不点智能出价）。激活集唯一真源 = policy.json `perception.stages.definitions[*].active`/`ocr`，经 `DetectionPlan` 以 `plan.active_for`/`ocr_for` 消费；全局锚点 `stages.global_anchors` 由**调用方**每帧全量并入（detector 不自动并），漏掉回退目标页的 ROI 会让阶段冻结（实测 `hall_session_cards` 事故）。阶段未登记 → 返回 None → 回退全量（安全兜底）。**新阶段必须登记 policy 感知清单**（含转移信号，如出价阶段必须含 settle\_title/result\_banner），否则只跑锚点 → 永不切换。smart\_bid\_btn 阈值已解耦：读 spec 锚点 `threshold`，缺省回退 `_SMART_BID_MATCH_THRESHOLD=0.72`（不可复用 0.90）                                                                                                                |
| 全局锚点并入曾被代码常量旁路（2026-09-15 定案）           | 观察线程 `_judge_stage_into_slot` 的扫描集并曾并入代码常量 `_GLOBAL_ANCHORS` 而非 `plan.global_anchors`，常量又落后于 policy（缺 `hall_chat_left`）→ 「命中 `hall_chat_left` → 待机」除开机首帧全量扫描外从任何阶段不可达，且无测试报红（数据面 `test_cross_truth_gates_pass` 与运行时并集各查各的，恰好照不到这条边）。处置：六个 v3 回退常量族（`_STAGE_PERCEPTION`/`_STAGE_OCR_KEYS`/`_GLOBAL_ANCHORS` 及 helper）整体退役，四处消费点（`_active_stage_rois`/`_collect_guard_rects`/`_judge_stage_into_slot`/`_treasure_ocr_dispatch`+`_bid_dynamic_ocr_keys`）直读 `detector.plan`；行为锁定见 `tests/test_treasure_perception_source.py`。**不要再引回退常量或"常量+一致性校验"**——模块类定义期对 `nav_source()` fail-closed，plan 缺失时插件根本不加载，回退是结构性死路（ADR-0002 真源单一）                                                                                                                                                                                                                           |
| 「已选中」对勾判定                             | `stage.appraiser_selected_check` 是横向长条 rect（覆盖三卡右上角对勾高度带），扫描黄色√；判定对勾中心 X ≈ 目标卡片命中框右边界（容差 0.09）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 鉴宝师搜索区                                | `_APPRAISER_SEARCH_ROI=(0.03,0.18,0.97,0.92)` 全屏范围（三卡位置/尺寸不固定），顺位 P1 卡洛琳→P2 章太郎，均未命中→准星指屏幕中心                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| 回合出价状态机                               | `_run_bidding_choice`：S0 转场期/S1 等待/S2 点主出价按钮/S3 面板内智能出价→确认出价；「等待/出价」用 OCR 文字判（`ocr.bid_main_btn_label`），面板是否打开用 `stage.smart_bid_btn` 模板判；等待状态 `key=None` → `_resolve_action_target` 返回 None 不出准星                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 出价按钮明暗                                | 主出价按钮「等待出价/出价」明暗态**不要用模板匹配**（禁用态透明渐变 + 亮度变化 → 置信度跳变，见 Experience 1112416），改用 OCR 文字判状态                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `_load_selected_check` 解包             | `_, fname = _SELECTED_CHECK_DEF` 是**二元组**；按三元组解包会报 `not enough values to unpack (expected 3, got 2)`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 调试台黑屏 | 截图正则 `_RAW_RE` 必须覆盖 `png` / `jpg` / `jpeg` / `webp`（原始存盘是 JPG，只认 png 会全黑） |
| 调试台框交互                                | 框显示开关 `showRois` 需同步 `hitTest()`（none→全部不响应；selected→仅选中项响应），否则隐藏的框仍可被点中/拖动                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 回合小字                                  | 已由模板像素差改为 OCR 识别（`round_label_area` 迁入 ocr 段）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| pyright 类型噪音                          | `tuple(float(n) for n in list)` 会被推断为 `tuple[float,...]`，赋给 `tuple[float,float,float,float]` 报错 → 用显式 4 元构造 `(float(r[0]), float(r[1]), float(r[2]), float(r[3]))`；多尺度 `best` 元组是 **6 元**（score,scale\_idx,x,y,th,tw）                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 主循环单帧异常兜底                             | `start()` 主循环**必须**对 `_tick_once()` 做 per-frame `try/except`，否则单帧未捕获异常会直接杀死整个主循环/模块线程（2026-08-19 核实原实现是 `try/finally` 无 `except`）。正确做法：单帧异常跳过继续并 `WARNING`，仅当连续 `_MAIN_CRASH_RETRY_MAX=30` 帧（≈9s）仍异常才上抛走 `finally` 清理终止，防"静默空转"掩盖真 bug                                                                                                                                                                                                                                                                                                                                                                                                                 |
| **按钮点击重试规范**（2026-09-05 定稿，新增按钮默认照此写） | 重试分三层，每层语义不同、不得混淆：**① 执行失败层**：ok=false（物理点击失败）→ 指纹不更新 → 下帧同意图自动重试（无限，直到成功/意图变化）；**② 无响应兜底层**：ok=true（物理成功）但「成功信号」未出现 → 超时后**清指纹重新 arm** 再点（防边沿触发锁死），**必须封顶** `_RETRY_MAX=3`（含首点共 4 次）；**③ 耗尽后果层**：重试封顶后**不得静默**，抛 `ClickRetryExhaustedError` 终止模块（用户可见、可干预）。关键点：每个 key 必须显式声明「成功信号」——阶段切换类 = 阶段名切走（`_maybe_retry_stage_click`）；面板内/数据类 = **指定 OCR 字段变化**（如智能出价 H 读出、出价面板关闭、本场收入读出）；弹窗类 = 离开弹窗阶段（per-key 帧数可覆盖 `CLICK_RETRY_FRAMES_BY_KEY`，上限统一）。**新增按钮加入** **`CLICK_RETRY_KEYS`** **时必须同步定义成功信号**，否则"ok=true 但无效果"仍会指纹锁死静默卡死                                                                                                                           |
| 领取分红「跳过动画」无响应兜底                       | `settle_collect_red_btn` 有两次点击语义：**跳过动画**（首次，成功信号=收入读出）与**真领取**（收入已读出，成功信号=阶段切走）。跳过动画点击物理成功但游戏无响应（实测：点后按钮/动画无反应、收入永远读不到）原逻辑静默卡死。2026-09-05 修复：`_decide_action` dividend\_waiting 分支加 `SETTLE_SKIP_RETRY_FRAMES=10` 超时 + `SETTLE_SKIP_RETRY_MAX=3` 封顶，超时清指纹重试，耗尽抛 `ClickRetryExhaustedError` 终止。重试指纹带 `clicked_once=True` 位与首点不同不撞指纹锁；`_apply_click_success` 每次点击成功重启计时防连点风暴；收入读出（OCR 写入）归零计数                                                                                                                                                                                                                                                           |
| 出价预测基准必须用「已证明火力」                      | 密封拍卖+秒杀成交（当回合第一/第二≥K\_r 即成交）下，对手**历史最高报价 = 可回放的支付意愿下限**；单轮报价含「钓鱼蓄力」噪声（实测 401 场：P3 报 748,900→降 500,300→末轮 766,810 秒杀成交赚 26.9 万，我方利润线 893,836 内本可反杀未杀）。任何"按对手价出牌"的逻辑，基准一律 `M = max(历史各轮对手最高, 上轮快照对手最高)`，禁止只取上轮价；更禁止为"上轮价回落"设计收缩补丁（V2 willingness 把降价读成撤退、双重低估火力，故被取代）。配套收入铁律：未拍中出价不花钱、分红仅在赢家亏钱时存在——能杀必杀（买入线内），杀不动卡第二吃分红彩票（strategy.py V3，2026-09-05）                                                                                                                                                                                                                                                                                                    |
| **转阶段交接：在途点击与导航**（2026-09-15 定稿） | 回合切换（`set_stage` 判定 raw 回合号变化）时两件事必须同时做，否则表现为「新回合半天不动 + 每次换回合多一条伪点击事件」：**①** `_abort_inflight_nav` 立即中止在途导航（仅 `clicker.mode == "gamepad"`；real 点击帧内同步完成，不参与）——旧回合目标已随界面消失，真机实测新回合首次点击被旧任务推迟约 5s，且旧导航可能在新界面误按 A；**②** `_consume_click_result` 对 `_pending_click is None` 的「无主结果」一律丢弃副作用（`set_stage` 换回合会清 pending 与指纹）——旧口径拿空 pending 走成功分支，落 `方式=? state=None key=None 归一化=(0.000,0.000)` 伪事件并按「无主成功」刷新指纹/点击时刻（真机同局第1→2、第2→3 回合各一条）  |
| **PEEP 手柄层的数据新鲜度**（2026-09-15 定稿） | 候选快照只在导航线程 `read_pos` 里刷新，导航空闲就变旧。**超龄不得返回 None**（旧口径 2s 闸让两次点击之间的空档叠加层无内容，真机数字键空转 24s 期间预览无候选可看）：`Clicker.cursor_candidates` 只附 `stale`/`age_s`；`last_pos_ts` + `Clicker.gamepad_cursor_age_s()` 给「上次识别」位置配年龄；由渲染层淡化标注。判定口径：只有「从未识别到候选/光标」才允许返回 None（不编造位置）  |
| 落盘编码抢占 OCR 时效 | 调试落盘的 rendered 编码曾定 WEBP q95：单帧 77.7ms（q50 仍 60ms，OpenCV webp 单线程且成本几乎不随质量下降）。观察通路把产帧节律固定成 150ms 后，IO worker 占空比冲到 60%、队列（上限 8）长期满并丢帧（真机 824 产 / 712 落盘 = 13.6%），与 OCR worker 争抢 CPU → OCR 第二段结果从 p50 27ms 被拖到 575\~783ms（**尾部放大器：`PIN_P_CORE_AFFINITY`** **绑核从未生效**——psutil 当时不在 `dependencies`，每个跑过 OCR 的会话都是 `亲和性绑定失败(No module named 'psutil')，忽略`，且只记 DEBUG，所以 E-core 漂移对策空转了一个月；psutil 2026-09-14 已补为正式依赖）→ 越过 `OCR_MAX_AGE_MS=800` 的超龄丢弃率 **0%→18.8%** → 公开报价窗口（游戏侧只有 \~1.5s）内 P1\~P3 整段丢光，表现为「debug 表历史回合列全空 + 快照建不出 + epoch 卡死数十秒」。判据看 `log_perf_summary` 的会话汇总行（丢弃率、报价窗口分桶、落盘丢帧、帧间隔分位一次给全），不必再手扫日志。**换 JPG q85（3.9ms）即解**；排查同类问题先看丢弃率，别看 p50 耗时（p50 全程没变，回归只体现在尾部与队列丢帧）  |
| **面板内数字键「点击无响应」兜底**（2026-09-15 定稿） | 出价面板数字键（`bid_numpad_*`，含 ✖ 清空）的**成功信号 = 面板读回变化**：数字键指纹里带着 `_bid_input_progress`（清空键是读数归零），所以「指纹不变」本身就是「无响应」，不必另接回调。口径 = 按钮点击重试规范第②/③层：同一意图持续超 `BID_DIGIT_RETRY_MS`（4s，给足一次性导航+按键+OCR；实测首个数字从出意图到读回推进约 3s）→ 清指纹重发，累计超 `BID_DIGIT_RETRY_MAX`（2 次）→ 抛 `ClickRetryExhaustedError` 终止模块。**计时基准只在换新指纹时归零**（重发→提交→归零 会变成无限重发，封顶失效）。真机依据：2026-09-15 07:17:34–07:17:58 点 '2' 后输入框 24s 无变化、光标原地不动，只能人工停止  |
| **页面令牌必须与像素同源**（2026-09-15 定稿） | 结算/分红 OCR 曾长期把大厅场次卡的「资产要求 ≥ 300,000」当成本场收入落盘（库内 455 场中 50 场；实习场对应 20,000）。两次修法同一根因：**页面身份不能跨捕获流搬运**——`_current_stage` / `_obs_slot` 是 `_accept_stage` 去抖产物，画面已切走仍停在旧页；改用未防抖的 `_last_raw_stage` 作令牌仍会漏，因它由观察线程按 `STAGE_JUDGE_INTERVAL_MS`（300ms）周期写，而帧是决策线程每 tick（~110ms）自截的，转场恰好落进窗口时令牌是旧页、像素已是新页（日志实证：OCR 帧 997 令牌=`'领取分红'` 且门控放行，同帧 `settle_my_income` 读到 300000，帧 998 令牌翻新页才拦下）。定案：worker 识别前对**同一帧**执行 `detector.probe_page(frame, stages)`，命中即得本帧页面、认不出即 None，随 payload `stage` 过闸②（`_ocr_filter_by_page` 的 `stages_for` 过滤逻辑不变，只是令牌产地换成本帧现场）。判定锚点 = `active_for(stage)` 里的**标志锚点**（`spec.stage` 归属本阶段者——`active` 混着邻页锚点；回合族认 `__round_phase__` 哨兵），零新增 policy 字段。**别在 worker 里做完整阶段判定**：全量 13 锚点 p50 140 / max 249 ms ≈ OCR 单帧（~110ms）的 1.3~2.3 倍，会打穿 worker 吞吐并触发 latest-only 丢帧；标志锚点集命中即短路，结算/出价页 ~2–3 ms。`probe_page` 走 `_scan(record=False)`，不写 `_last_hit_roi_key` / `_last_detect_scores` / `_last_round`（生产者是观察线程，决策线程正在读），**也不读其中任何一个**。**令牌按页族产出**：命中回合族锚点（`smart_bid_btn` / `round_big_banner`）返回 `__round_phase__` 哨兵本身，**不推导第几回合**——回合号在 detector 里由 `_last_round` 提供，那是观察线程的跨帧状态，探针读它等于把刚拆掉的跨线程陈旧结论从另一个门请回来；该字段为空时更会返回 None，并把自带回合号的横幅一并短路（`smart_bid_btn` 优先级 80 > 横幅 70）。实测同一批 515 帧：读 `_last_round` 只判出 18 帧局内页、不读判出 80 帧，局内读数被 fail-closed 大面积丢弃。门控侧 `_page_token_matches` 收下哨兵，「哪个阶段属回合族」与锚点归属共用 `detector.is_round_stage` 一处判据。**验收口径（排查同类照抄）**：探针与真值必须用**两个独立 detector 实例**——先用全量 `detect`「喂」同一个实例再跑探针，会把 `_last_round` 喂新鲜，测到的是生产不存在的口径（本坑就是这样漏过首轮验收）。成本（出价族 2 锚点）：命中 p50 2.1 ms、判不出 p50 ~20 ms；判不出帧占比 = 「`_current_stage` 与画面不一致」的帧占比（转场 + 管线滞后），并非只在转场——真机一局局内 540 心跳里 310 帧（57%）判不出，按帧加权 ~15–18 ms/帧，对照 OCR 单帧 p50 ~22 ms；**该耗时不计入上报的 `duration_ms`**（探针在 `t0` 之前调用），且**实测对读取时效净影响为零**（逐帧 `时效` 三版无差：改前 p50 111/p90 220、旧帧内 115/223、本版 110/211——仍远低于 110 ms 决策节拍，被节拍吸收）。性能汇总行的 `时效 p95` 跨会话抖动大（113/222/224），别拿它做版本对比。回归锁 `tests/test_treasure_ocr_page_gate.py`；基线与成本见 `tools/experiments/frame-page-token/`。**第二类同帧证据（2026-09-15 同日补）**：标志锚点只在回合内的**部分时段**可见（面板打开期与横幅闪现期），而 4 人报价数字是在「都已出价、面板关闭」之后才逐个显示的——那段窗口第一类证据恒为 None，整段公开报价被 fail-closed 丢掉（实测帧：OCR 已正确读出 122,100 / 250,000 / 163,100，令牌却是 None；会话 `20260915_201920` 本帧页=`None` 817 次、被丢键 `bid_player4` 与 `bid_result_amount_box` 各 482 次；四个报价槽「消费/输出/命中」全场命中≈0，13 条「快照构建等待」全部未固化，快照建不起来 → 相位停在 `wait_result`）。补法：第一类缺席**且**本批信号属回合族时，用**同一帧**的回合小字（`round_label_area`）解析出的回合号与**投递时快照的 `_round_no`**（即阶段标签当时的答案）互相印证，一致才产出 `__round_phase__`（`detector.confirm_round_page`；复用 `_detect_round_full` / `_round_no_from_text`，不另立第二套读法；第 6+ 附加回合按 `set_stage` 的 clamp 口径比较）。**两个来源互相印证、单方不说话**：阶段标签单独说过话不算数（它正是 300000 那次的撒谎方），本帧文字单独说话也不算数（它只证明画面上写着某个回合号，不证明这是哪一页）。非回合族批次不启用该兜底，结算/分红页仍只认标志锚点。离线复验（`tools/experiments/ocr-gate-2026-09-15/probe_fix.py`，调用生产函数本体，非复刻）：出价页 5 帧令牌 `None`→`__round_phase__`，其中 3 帧把 P1\~P3 读数从「收 0 条」变成「收 1\~3 条」；未中标横幅（小字读成「第40」，解析失败）、结算页、大厅页 3 帧仍为 `None`  |
| **假下降沿与按钮自愈的判据互斥**（2026-09-15 定稿） | 出价提交成功与否有**两个出口对同一事实各自为政**：「假下降沿」（`module.py:2172`）只看面板——`_bid_player_submitted[我方] is False` 且 1.5s 缓冲期内无任何槽读到报价；「按钮自愈」（`module.py:2232`）只看按钮文字——`"已出价" in label`。两者结论相反时进入**周期振荡**：假下降沿 → `wait_first` → 自愈 → `wait_result` →（`SUBMIT_ANIMATION_BUFFER_MS`=1500ms）→ 假下降沿。实测 `20260915_120953`：12:12:47/48/49/49/51、12:15:14/16/17、12:17:49/51（一圈 ≈2s），该局 19 次假下降沿 vs 14 个 epoch、6 次自愈。代价：每次自愈都把 `_wait_result_entered_ts` / `_wait_result_frames` 归零 → 快照构建被无限推迟；且 `wait_first` **不投递槽 OCR**（投递闸只认 `dec.state` 以 S3 开头或 `_bid_phase == "wait_result"`）→ 整个公开报价窗口空转。**上游诱因**：`_bid_player_submitted` 从不按回合重置（只在构造与 `module.py:1385` 初始化），且空读与被门控丢弃的帧不覆盖它（`module.py:5272` 的守卫是刻意为之）——读数进不来的窗口里该字段就是过期值，而假下降沿恰好依赖它（振荡窗口心跳 #1390 实测 `P2:未读 5/0/0`：消费 5 次、输出 0 次，同帧跨页丢弃 `bid_player1..3`）。**放大器**：`bid_main_btn_label` 旧 ROI 过窄，只在金额短（`已出价：0`）时恰好框得下、长金额（`已出价:353,200`）读成 `价：353,2`，自愈因此时灵时不灵（该局仅成功 6 次）；ROI 加宽后自愈稳定生效，振荡更易被点着。口径：**别让两个出口对同一事实各自为政**——假下降沿判定前应并采按钮文字，或让按钮「已出价」优先于面板三态。待处置  |
| **观察线程的启动判据**（2026-09-15 定案） | 观察线程（`treasure-observer`）是**阶段判定的唯一生产者**：`_judge_stage_into_slot` 写 `_obs_slot`，决策段 `_consume_stage_slot`（`module.py:4344`）读它并 `set_stage`——这是 `set_stage` 唯一的自动调用者，所以线程不起 = 阶段永不推进。它的启动判据**不得**绑 `debug.enabled` / `peep_enabled`：旧写法 `if debug or peep: _start_io_worker(); _start_observer()` 会让两个出口全关的会话阶段判定彻底停摆（`_obs_slot` 恒 None → 决策段直接 return），与同文件 `_observe_interval_s` 声明的 C6「阶段判定消费者恒在」直接冲突——**节拍判据改了新门，启动判据还守着旧门**。现口径收在 `_start_session_workers()`：观察线程无条件起、IO worker 仍按出口开关起；会话**中途**打开出口由观察循环 `_ensure_io_worker()` 每 tick 补启（否则要重开会话才有帧）。教训一般化：凡「某线程/组件是某职责的唯一生产者」，其启停判据必须盯那个职责，而不是盯历史上顺带服务的出口开关。真机证据 `MaaRM_20260915_173823`（全关会话无「观察通路已启动」行）与 `MaaRM_20260915_173229`（途中开 PEEP 全程无预览）；回归锁 `tests/test_treasure_observer.py` 四条——注意原有七条是**方法级**契约，照不到「线程有没有被启动」这条判据，所以修复前全绿  |
| **报价槽固化状态必须随场次清空**（2026-09-15 定稿） | 报价槽 `_bid_slots` 的回合级重置只认「回合号变了」（`_consume_ocr_result` 比对 `_bid_slots_round`），于是**新一场的第 1 回合与上一场最后一回合同为 1 时该条件不成立**，槽沿用上一场四槽——本场 R1 的报价一次都不读（`_bid_dynamic_ocr_keys` 已把固化槽剔除），R1 快照直接用上一场数字。真机 `20260915_211002` 实证：第 2 场 R1 开局（21:11:21）四槽仍是第 1 场的 `150900/208800/550000/178000`、`OCR 199次` 冻结不再增长，快照 `epoch#1 R1 H=216,600 我方=150,900 对手=(208800, 550000, 178000)`——H 是本场的、四槽是上一场的；该会话 5 场里第 2、3 场中招，第 3 场打到 R3 后第 4、5 场的 R1 因回合号不等而自行恢复。**教训一般化：「按局内序号重置」这类判据在场次边界会失效**——凡状态机的重置键取自局内序号（回合号 / epoch / 序号），跨场必须由场次边界显式清空，`_reset_round_state` 是那个唯一出口；清的时候复用既有 `_reset_bid_slots()` 而非清成空 dict，维持「四槽恒在」约定（消费侧 `_bid_slots[pid]` 按下标直取）。该缺陷在报价读数可读之前不可达（那时没有任何槽会固化），是随「出价页页面令牌补第二类证据」生效后暴露的既有问题。回归锁 `tests/test_treasure_bid_phase_recovery.py::test_reset_round_state_clears_locked_bid_slots`（已在旧行为下验红：`assert 1 is None`）  |
| 点击日志的留痕判据（模块侧） | 槽忙跳过按帧节流（`SLOT_BUSY_LOG_EVERY`），每次点击导航仍留 1~2 行（点击与避让各一）——这是**预期行为**不是异常；点击失败按**失败链**判定：某 key 连续失败链首次必记、其后按帧节流、点击成功清链（真机 2026-09-17 帧 24/46 的两次失败曾被纯帧号节流整段吞掉）。帧级日志的产出判据与取证口径见 [core §5.5](../../core/CODE_WIKI.md) |
| 帧边界与终止可见化 | `_tick_once` 只是边界壳，帧工作在 `_tick_frame_body`：`ClickRetryExhaustedError` → 一条 ERROR + `ctx.lifecycle.request_stop()`（置 `_frame_abort` 后后续帧不再执行帧工作）；其他异常 → 计数并按 `FRAME_ERROR_LOG_EVERY` 节流记 traceback，**不中断**。快照路径不得触发决策（`_treasure_kwargs` 只读决策段发布的 `_last_intent`），代价是 PEEP 准星滞后一帧 |