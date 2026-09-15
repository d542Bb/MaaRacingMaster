# MaaRacingMaster 修改日志

> 按时间顺序记录每次重大修改。
>
> **暂存条目前缀「暂存」**：内容已定稿但版本号未定，`extract_changelog.py` 不会抽取它；
> 发版时把「暂存」小节并入对应的 `### v<版本>` 标题后再出 Release 正文。

## 2026-09-15

### 暂存（未发布 · 待并入下一版本）出价面板数字键「点击无响应」兜底 🔧

- **性质：** 未发版变更（`plugins/treasure/module.py`、`tests/test_treasure_bid_digit_retry.py`（新）、域 CODE_WIKI；master 直接提交、不 tag）

- **问题：** 数字键指纹里带着输入位锚点（清空键的成功信号是输入框读数归零），点击后读数不推进 ⇒ 指纹不变 ⇒ 边沿触发永不重发 ⇒ 光标原地不动。真机 2026-09-15（07:17:34–07:17:58）：程序把光标导航到数字键「2」并按了 A，输入框 24 秒始终为空，只能人工停止；同面板「智能出价」「✖ 清空」按下均生效。

- **口径（沿用按钮点击重试规范第②/③层）：** 成功信号 = 面板读回变化——它已编码在指纹里，故「指纹不变」就是「无响应」，不必另接回调。同一数字意图持续超 `BID_DIGIT_RETRY_MS`（4s，给足一次性导航+按键+OCR）→ 清指纹重发；累计超 `BID_DIGIT_RETRY_MAX`（2 次，含首点共 3 次）→ 抛 `ClickRetryExhaustedError` 终止模块（不得静默）。计时基准只在换新指纹时归零，重发自身不重置（否则封顶形同虚设）。

- **回归锁：** `tests/test_treasure_bid_digit_retry.py` 7 条（换指纹归零 / 未超时不重发 / 超时清指纹并计数 / 封顶抛错 / 清空键同覆盖 / 其它 key 不介入 / `_execute_click` 接线）。全量 `pytest 504 passed`。

- **验证边界（真机待复验）：** 若重发也耗尽，日志直接给出「该面板不接受本次输入」的 ERROR（用户可见、可干预）；届时再用 `tools/experiments/bid-numpad-keypress/` 区分「游戏不接受数字键」与「按键未送达」。

### 暂存（未发布 · 待并入下一版本）转阶段交接与 PEEP 手柄诊断层修复 🔧

- **性质：** 未发版变更（`core/clicker.py`、`core/gamepad_cursor.py`、`plugins/treasure/module.py`、`plugins/treasure/renderer.py`、`tests/test_clicker_cursor_snapshot.py`（新）、`tests/test_treasure_stage_handoff.py`（新）、`tools/experiments/bid-numpad-keypress/`（新）、域 CODE_WIKI；master 直接提交、不 tag）

- **PEEP 手柄诊断层与点击意图解耦：** `render_peep` 原先在 `treasure_action` 为空（转场/未定义过渡）或 center 为空（纯等待）时提前返回，把手柄光标实时位绿圈与识别候选圈一并丢掉——转场期预览只剩原图（真机 2026-09-15 日志在结算弹窗转场反复打「动作按钮 popup_click_cooldown 未配置 rect，准星跳过」，即命中该分支）。现抽出 `_draw_gamepad_diag` 在函数开头无条件绘制，颜色/半径/文案口径不变。

- **空闲期不再丢快照：** 候选快照超 2s 曾直接返回 None、导航结束（stage=done）时进度也返回 None，两次点击之间的大段空档叠加层无内容可看。现 `Clicker.cursor_candidates` 超龄仍返回数据并附 `stale`/`age_s`；新增 `GamepadClicker.last_pos_ts` 与 `Clicker.gamepad_cursor_age_s()`，`_gamepad_nav_progress_kwargs` 空闲态返回最后一次识别位。渲染层用暗色圈 + 「上次识别 N.Ns 前」「候选快照: 陈旧」标注，实时位与历史位一眼可辨。

- **回合切换交接：** ① 在途导航立即中止（`_abort_inflight_nav`，仅手柄方式）——旧回合目标已随界面消失，实测新回合首次点击被旧任务推迟约 5s，且可能在新界面误按 A；② `_consume_click_result` 对无主结果（`_pending_click` 已被 `set_stage` 清）一律丢弃副作用，不再拿空 pending 走成功分支——旧口径会在每次回合切换落一条 `方式=? state=None key=None 归一化=(0.000,0.000)` 伪点击事件（真机第1→2、第2→3 回合各一条），并按「无主成功」刷新指纹与点击时刻。

- **回归锁：** 新增 16 条（`tests/test_clicker_cursor_snapshot.py` 5 条；`tests/test_treasure_stage_handoff.py` 11 条：无主结果丢弃 / 正常结果不变式 / 回合切换中止且 real 不参与 / 空闲保底与活跃透传 / 渲染层无点击意图仍出层）；全量 `pytest 497 passed`。

- **待定性（真机实验已就位）：** 数字键 `bid_numpad_*` 提交后游戏输入框不变化（真机 07:17:34–07:17:58 空转 24s，同面板「智能出价」「✖ 清空」按下均生效，光标正确停在 '2' 键中心）。新增 `tools/experiments/bid-numpad-keypress/`：自包含探针（基线 / 目标键 / 对照键三组前后帧差异）+ 判定矩阵（数字键需前置激活 / 导航落点 / 输入通道），结论待真机执行后回填主题 README。

## 2026-09-14

### 暂存（未发布 · 待并入下一版本）日志观测改造：有界缓冲 + 单流落盘 + 级别口径修正 🔧

- **性质：** 未发版变更（`core/logger.py`、`core/sidecar.py`、`core/pipeline_logger.py`、`apps/.../frontend/app.js`、`tests/test_logger_channels.py`（新）、`docs/CODE_WIKI.md`；master 直接提交、不 tag）

- **有界环形缓冲：** 进程内日志缓冲由无界 list 改为有界 `deque`（容量 5000），长跑内存不随日志量增长。

- **序列号增量游标：** GUI 拉取改用单调序列号（`get_lines_since`），环形回绕后不重不漏；GUI 落后于缓冲覆盖范围时插入一行显式「自动截断」提示，不再静默丢行。

- **单流落盘 + 保留：** 一次开启只建一个 `MaaRM_<ts>.log`，各级别按发生顺序写入。诊断是顺序的（「失败之前发生了什么」），把级别切到不同文件会打断时序；分级过滤留给读侧（GUI）与导出侧。大小轮转（16MB × 4 份上限）+ 启动时按会话组保留清理（默认最近 20 组，只清 `MaaRM_<ts>` 形态、不碰 sidecar_stderr.log、不删活动会话）。

- **级别口径修正：** `[Pipeline]` 记的是框架内部节点生命周期（识别成功 → 动作开始 → 动作成功），一律降为 DEBUG——真机上每个节点跳转连打三行，按 INFO 记会占掉日志一半以上篇幅（实测 62 行里 33 行），把业务里程碑淹没。动作失败仍打 WARNING（可继续运行的降级不得静默）。日志随之变得可直接阅读。

- **通道调整：** `log()` 新增 `channel` 参数 + `set_channel_level`/`clear_channel_level`，支持按子系统独立调级；旧调用零改动。通道与级别都不影响写侧落点。

- **写盘正确性：** 每条日志独立 open/close 改为持单句柄 + `Lock` + 行缓冲，省掉每行两次系统调用且跨线程写行完整；退出路径 `close()` 关句柄 flush 尾部。

- **新增单测：** `tests/test_logger_channels.py` 18 条（环形回绕不重不漏 / 截断标志与边界 / 多线程并发写行完整 / 单流落盘时序 / 轮转与备份计数 / 会话保留只清旧会话 / 通道级别覆盖与回落）。

- **真机端到端（GUI 区块卡片渲染、长跑 RSS、开两局保留验证）** 依赖游戏 + ViGEmBus 驱动，待用户真机复验；单元/契约层面已全绿。

### 暂存（未发布 · 待并入下一版本）彩蛋收尾链手柄模式失败修复（根因：链等待忙旋）+ psutil 依赖补齐 🔧

- **性质：** 未发版变更（`plugins/treasure/module.py`、`core/capabilities.py`、`core/controller.py`、`plugins/treasure/ocr.py`、`requirements.txt`、`tests/`、域 CODE_WIKI；master 直接提交、不 tag）

- **修复（手柄模式必现的整链失败与链内点击 12 秒静默）：** 两层成因、两层修。协议层：每日到限拦截带 3 帧确认窗，窗内主链路照常决策并可能刚提交一次手柄点击，链启动时任务槽被在途点击占住、以「任务槽被占用未释放」放弃——现三道防线：链入口先作废在途点击并消化（上限既到，该点击已无意义）；链内等待首窗超时先中止在途任务再补一轮等待，不再直接放弃；单次点击中止后的重试先消化中止结果再提交（任务槽对「结果待消费」态也算忙，不消化则重试必被拒）。资源层（12s 静默根因，定案）：链的等结果/等槽两处轮询传 0.05 秒，而可中断睡眠原语 `ctx.lifecycle.sleep` 旧实现按 0.1 秒量化迭代，亚粒度入参**静默退化为一次都不睡**——决策线程以数十万次/秒空转、持 GIL 饿死导航 worker 与产帧通路（真机：单任务 12s 不落地、光标 25px/11s 爬行、raw 帧间隔 0.16s→2.4–4.5s，链结束即恢复）。前台鼠标模式点击瞬时完成，两层问题都从不显形，行为不变。

- **原语修复（可中断睡眠的墙钟语义）：** `LifecycleAdapter.sleep` 改为 deadline 分片——每片 ≤0.1 秒保持停止信号可中断，末片补齐余数，任何入参的实际睡眠时长 ≥ 请求值（旧版非整数倍入参也会向下截断，如 0.25 实睡 0.2）。语义由新文件 `tests/test_capabilities_lifecycle_sleep.py` 四条锁死（亚粒度不得零睡眠 / 余数不得截断 / 中断及时返回 False / 零与负入参即回）。同型量化实现的 `Controller._interruptible_sleep` 为全仓零调用死码，一并删除。

- **依赖补齐：** `psutil` 补为正式运行时依赖并安装（此前缺失导致 OCR 的 P-core 亲和性绑定从未生效、每个跑 OCR 的会话失败一次仅 WARNING），绑定现已真正落到运行时（真机日志可见「已绑定进程 CPU 亲和性到 P-core [0-7]」）。

- **PEEP 链期间叠加层停更修复（可观测性）：** 收尾链独占决策段期间 `_last_debug_kwargs` 停止发布，PEEP 的准星、导航进度、光标候选叠加层全程冻结在链前状态——恰是用户最需要看「导航在干嘛」的时段。现链内 drain/click/wait 三处轮询实时刷新两个手柄字段（导航进度、候选快照），其余字段维持链前快照；链内导航过程从此在 PEEP 上连续可见，真机排查不再需要逐帧翻 raw 帧复盘。（注：链期间产帧节拍劣化不在此条——那是等待循环忙旋抢占 GIL 所致，随原语修复一并消失。）

- **回归锁：** `tests/test_treasure_egg_claim.py` 新增 3 条（drain 超时先中止再补等 / 重试前消化中止结果 / 链入口作废在途任务）+ PEEP 链内刷新 1 条；`tests/test_capabilities_lifecycle_sleep.py`（新）4 条锁睡眠墙钟与可中断语义。全量 `pytest 481 passed`。

- **验证面：** 真机（手柄模式）复验通过（2026-09-15）：链完整成功路径走通（聚合奖励弹窗正常弹出并逐蛋读出），12s 静默与链内产帧节拍劣化随原语修复消失，PEEP 全程流畅。单元与契约 481 passed、忙旋对照实验（同构桩下忙旋使 worker 步进劣化 7 倍、真 sleep 零劣化）、psutil 绑核进程级实测。

### 暂存（未发布 · 待并入下一版本）鉴宝彩蛋收尾链点击通路修复 + 手柄能力接口收敛 🔧

- **性质：** 未发版变更（`plugins/treasure/module.py`、`core/capabilities.py`、`core/clicker.py`、`core/base.py`、`tests/test_treasure_egg_claim.py`、域 CODE_WIKI；master 直接提交、不 tag）

- **修复：** 每日到限后的「彩蛋任务」领取链此前自带一套点击出口，与主链路不共享护栏与意图语义——「仅意图」开关对它无效、首次点击会被上一帧遗留的点击任务判忙而拒掉（真机实证：大厅返回键命中置信度满分却报「未点中」而放弃整条链）、链内点击不留事件与日志。现统一到与主链路同一条出口协议：先消化任务槽遗留结果再提交、意图开关取自 `ctx.intent_mode`、结果统一记录、提交被拒或结果超时有界重试。

- **行为变化（须知）：** 链内点击从此遵守「仅意图」开关——开启时链只导航不点击、等不到按钮消失即超时跳过，不会自动领奖。需要自动领取请关闭该开关。

- **链契约补齐：** 45 秒全链预算此前只写在日志文案里、各步各自计时，现由链入口算成墙钟截止时间并逐步收窄；领取循环加尝试次数上限，消除「红钮常驻而点击持续失败」时的空转；单次点击超时与重试次数提为类常量。

- **可观测性：** 链独占 Tasker 期间帧 trace 不产生，新增链内进度 trace（事件 `egg_chain`：start / click / click_skipped / click_timeout / panel_opened / claim_done / give_up / error），其中 start 带点击方式与意图开关，可直接回答「为什么没领到」；链内失败路径收口到统一出口。

- **接口收敛（内部）：** 插件不再穿透私有成员——新增 `GamepadAdapter.persistent_adapter()`、`Clicker.nav_progress()` / `cursor_candidates()` / `swap_gamepad()`；Win32 窗口事实（前台状态、帧尺寸校验、宽高比）收进 `ActivityContext` 窄接口，插件不再直接 import `window_utils`；A 键统一走 `BUTTON_A` 语义常量。

- **回归锁：** `tests/test_treasure_egg_claim.py` 新增 5 条（意图开关跟随 / 先消费后提交 / 有界重试 / 结果留痕 / 大厅锚点语义）；链内锚点清单改为直接引用 `TreasureModule.EGG_CHAIN_ANCHORS`，不再手抄副本。全量 `pytest 455 passed`。

- **验证边界（如实）：** 手柄模式下的链内点击通路仍待真机复验（现有真机日志均为前台鼠标模式）。

### 暂存（未发布 · 待并入下一版本）鉴宝新增「彩蛋任务」每日收尾领取链 🥚

- **性质：** 未发版变更（`plugins/treasure/`（含 9 张新模板与真源）、`core/sidecar.py`、GUI 前端、`tests/`；master 直接提交、不 tag）

- **新能力：** 每日循环刷到上限后不再原地停手——在鉴宝大厅触发一次「彩蛋任务」收尾链：点返回键到活动页 → 「获取银币」拉出任务抽屉 → 在彩蛋 tab 领可领礼物 → 逐个读聚合奖励弹窗 → 关抽屉 → 回大厅 → 停止模块。链自带 45 秒总预算，任一步超时或异常都只记警告后照常走停止路径，「有就领，没有就结束」，绝不阻塞停止。触发条件：到限判定连续 3 帧稳定 + 当前在鉴宝大厅 + 本次运行未进过。

- **奖励读数走通用卡框几何：** 弹窗里蛋卡种类/数量不定、固定 OCR 区不可行，改由「图标命中框 → 外扩找卡体白描边 → 框内按比例切数字带」推导读数区（`eggs.py` 代码常量），蛋卡与银币/积分 medal 卡共用同一套几何；真源 policy 不携带任何偏移参数（校准与消费端契约见 `tools/experiments/egg-claim-coin-read/`）。

- **收益口径变更（须知）：** 彩蛋收益不再依赖逐局拍中读数——「赚蛋模式」退役、GUI 策略下拉移除该选项，出价策略只剩单一「赚钱」口径；`games` 表的蛋列退役（历史行保留、不再写）；新增 `daily_summary.egg_coin` / `egg_score` 记录当日领取数，三色蛋列改记「今日领取数」。

- **数据页改版：** 银币盈亏（竞拍净利 + 领银币）、今日领取彩蛋、今日积分合并展示，胜负并入场次标签。读侧按 `PRAGMA table_info` 取列交集并对缺列兜底——旧库尚未迁移的窗口期内看板不再整体失效。

- **实现要点：** 收尾链锚点族走惰性 spec（在 `perception.spec` 登记，但不进 transitions / stages.active / global_anchors，检测器零扫描、不参阶段判定），由 `tests/test_treasure_egg_claim.py` 机检这条惰性；链在决策 tick 内阻塞跑完，Tasker 被占用期间常驻图停摆，收尾方向冲突天然消解（不改图拓扑、不加旗标闸门）。

- **验证边界（如实）：** 真机已跑通一次完整领取（红蛋 ×1 入账）；手柄模式下的链路行为尚未复验。

### 未发版变更：新增架构决策记录（ADR）目录并接入信源路由 📐

- **性质：** 未发版变更（纯文档：`docs/adr/`（新增）、`AGENTS.md`、`docs/update_log.md`；master 直接提交、不 tag）

- **设立缘由：** 仓库的架构决策此前散在宪法、MAAFW_GUIDE 定案、各域 CODE_WIKI 与 `docs/plan/archive/` 中，其中**只有宪法带状态标注**，其余是无状态断言——读者无法判断"这条还成立吗"。实证代价：`docs/plan/doc-knowledge-baseline-plan.md` 中一条已失效的定论（`file:///` 链接为"IDE 可跳转约定"）因缺少状态标注被当作有效前提转述，导出错误结论。

- **准入硬门槛（用户定）：** 进入 ADR 的决策必须**充分验证**（一手协议核查 / 机检背书 / 真机实证三者之一，且可复核）**且板上钉钉**（已落地实施，非提案待定），并属**架构级**（影响跨模块结构、分层、协议或运行时拓扑）。实现细节、域内坑点、行为守则、方案过程一律不收，各有归宿（README 内列明对照表）。

- **不复制真源：** ADR 只写"决策 + 理由 + 后果（含被否方案与代价）"，事实细节引用不重述；与宪法 / CODE_WIKI 冲突时以那两者为准并回头修 ADR——ADR 是决策的索引与状态，不构成平行真源。

- **状态机：** `accepted` / `superseded by ADR-NNNN` / `deprecated`；推翻旧决策的唯一方式是新写一条并标注取代，禁止原地改写已生效正文（历史理由即决策记录的价值）。文件名 `NNNN-短横线小写标题.md`，序号只增不改。

- **首条记录：** `docs/adr/0001-分层靠引用方向不靠目录.md` —— 收录 core / plugin 分离的唯一可实现形态（协议无命名空间，分离只能靠引用方向单向），附证据链四项：MAAFW_GUIDE §5.6 协议事实、`check_truth.namespace_checks` 机检、`tests/test_navkit_truth.py` 活性锁、`global.json` 归位实证与实施 commit `5d6c320`。

- **收入宪法三条款：** `0002-真源单一禁止生成物回读.md`（宪法第 1/2 条）—— 收录 v3 五段 schema 加编译层的整代返工实证（P4a 一次删除 18,757 行，commit `b9e3b3f`）与"加载时内存透传可以、落盘转换不行"的边界；`0003-运行时拓扑硬约束.md`（宪法第 6 条）—— 收录帧只从中心缓存来、动作只从队列走、引擎永不自截帧/永不直连手柄四条约束，附帧供给解耦实证（大厅类 dwell 零帧事故 → `treasure-observer` 独立产帧）与 `action_checks` 机检（内置输入 action 在 v4 控制器全为成功占位）。宪法第 3/4/5 条按准入条件不收（属工作方式、分工原则与流程规则），README 索引内注明归宿。

- **信源路由接入：** `AGENTS.md` 新增「架构决策『为什么这么定』与其状态」行，指向 `docs/adr/`（标注准入与状态机见其 README）。

### 未发版变更：游戏规则事实独立成文并纳入信源路由 📜

- **性质：** 未发版变更（纯文档：`maaracing_master/plugins/treasure/RULES.md`（新增）、`AGENTS.md`、`maaracing_master/plugins/treasure/CODE_WIKI.md`；master 直接提交、不 tag）

- **规则知识此前无独立落点：** 活动规则（回合结构、成交条件、收入与分红机制、计分目标、对手行为约束）散落在 `strategy.py` 模块注释、`module.py` 头部注释、鉴宝 CODE_WIKI 与外部记忆实体中，任一页都无法完整回答"这个游戏怎么算钱"。新增 `plugins/treasure/RULES.md` 作为唯一落点，按规则与实现分离的原则只陈述游戏客观机制——策略推导（出价怎么打）仍归 `strategy.py` 与 CODE_WIKI §2，实现参数（K 常量、缓冲分桶、估值系数口径）不进规则文件。

- **每条附证据等级与验证例证：** 规则逐条标注 `[权威]`（用户确认+落盘验证）、`[实测]`（真机数据验证）、`[推定]`（推导未直接验证）；分红 15%/10%/5%、成交 K 值表附四场落盘例证（g8/g6/g12/g5 与 g9/g12/g10/g4），全部经直除验算吻合。

- **修改须人工复核定为文件级硬约束：** 规则是外部事实（代码错了改代码没用），文件顶部明列三条约束——新事实须有真机验证或用户权威确认、不得由代码实现反推、无法验证的疑点写入未知区而不得补全为断言；违反约束会让防幻觉文件自身成为幻觉源。

- **未知区显式留白：** 提前成交精确触发时机、成交顺位并列处理、对手撤价频率、最小加价下限、彩蛋发放条件等未确证项单列 §6，并附"已证伪"清单（如"相邻差价 100 → 货币单位 100"的推导不成立），防止 agent 凭空白推断。

- **信源路由与域文档双向接入：** `AGENTS.md` 信源路由表新增"游戏规则事实"行（指向 RULES.md，标注修改须人工复核、未知项见其 §6）；鉴宝 CODE_WIKI 头部加"先读"指引并新增 §0 指针，明确"规则与本文冲突时以 RULES.md 为准，并回头修本文"。

## 2026-09-13

### 未发版变更：GUI 图标统一 Lucide 真源并引入变形动画 🎨

- **性质：** 未发版变更（GUI 前端：`apps/MaaRacingMaster.Shell/frontend/`、`THIRD_PARTY_LICENSES.md`；master 直接提交、不 tag）

- **图标真源：** 新增 `frontend/icons.js`（`window.MRAIcons`）作为全部 UI 图标的唯一真源，数据拷自 Lucide v1.45.0（ISC 许可）；此前散落在 `index.html` / `app.js` 的约 30 处内联 SVG（含 play×3、camera×2 等重复拷贝）全部迁入，经占位水合（`<i data-icon>` → `MRAIcons.hydrate()`）或模板函数（`MRAIcons.svg()`）渲染。金币检测的自拼复合图形换用 Lucide 现成 `circle-dollar-sign`；窗口标题栏自绘 chrome 与性能走势图 polyline 按规范排除在图标系统外。

- **变形动画：** 引入 morphicons v1.7.1（MIT）作为图标状态过渡动画，以 vendor 摊平单文件（`vendor.morphicons.js`）适配 WebView2 的 `file://` 加载（无构建链、无 CDN）；首个用例为数据页「实时预览」放大/还原按钮——两按钮合并为一个，scan ↔ shrink 图标弹簧变形，尊重系统减少动态设置。后续成就系统的图标动画复用同一套用法。

- **规范与许可：** 新增 `frontend/README.md`（图标来源、新增流程、渲染方式、排除项的权威规范）；`THIRD_PARTY_LICENSES.md` 补第四节登记 Lucide（ISC）与 morphicons（MIT）随包分发，许可原文分别内嵌于 `icons.js` 与 `vendor.morphicons.js` 文件头。

### 未发版变更：长线文档与团队技能对齐现行基线 📄

- **性质：** 未发版变更（纯文档：`AGENTS.md`、`README.md`、`skills/`；master 直接提交、不 tag）

- **守则与 README 对齐现行名称与打包布局：** 守则标题与角色设定使用 `MaaRacingMaster`；README「下载即用包」与「使用说明」指向发行包根目录唯一入口 `MaaRacingMaster.exe`（薄 Launcher 拉起 `app\` 下的 GUI），插件目录引用对齐现行包名。

- **守则只留跨环境共识：** 协议引用口径与过早抽象案例统一载于 `docs/MAAFW_GUIDE.md` §5.6；v3 返工与归位的记录由宪法废弃清单与 §5.6 案例段承载；记忆服务等 harness 机制由各工具自行配置，仓库文档不承载。

- **团队技能补齐入库：** `skills/anti-sycophancy/` 与 `skills/no-negative-echo/` 以逻辑文件（SKILL.md、参考文档、校验脚本）入库，上游来源与抓取哈希保留在 provenance 清单中按需可取回；`skills/project-update/SKILL.md` 对安装关系的表述改为 harness 无关（仓库为权威定义，副本归属各工具的本地配置）。

### 未发版变更：文档基线收敛为团队共有基线 📄

- **性质：** 未发版变更（纯文档：`AGENTS.md`、`docs/NAVKIT_V4_PLAN.md`、`skills/project-update/SKILL.md`；master 直接提交、不 tag）

- **行为守则与版本宪法纳入仓库基线（协作者可见）：** `AGENTS.md`（行为守则 + 信源路由）与 `docs/NAVKIT_V4_PLAN.md`（版本宪法六条不变量）随仓库入库，与 `docs/CODE_WIKI.md` / `docs/MAAFW_GUIDE.md` 同级安置。clone 后仅凭入库文档即可对齐行为守则、架构信源与版本宪法；信源路由中的本机记忆库已在该行标注对协作者不可达。

- **发布流程以技能形式入库：** 团队发布流程的权威定义落在 `skills/project-update/SKILL.md`（版本号判定、changelog 更新、commit / tag / push 顺序、不发版模式），保留 SKILL.md frontmatter 以支持 skills harness 直接加载；本机安装副本的同步命令写在该文件内。

- **共识层内容对无本机环境的读者可执行：** 行为守则与发布技能只承载跨环境成立的规则（行为守则、信源路由、不变量、发版流程），机器特化事实与本机路径留在 git 之外；版本规则统一以 git tag 为唯一信源——0.x 默认发 `v0.x.y-dev.N`，dev 系列成熟后经用户拍板可发正式版 `v0.x.y`（先例 `v0.13.0` / `v0.19.0` / `v0.19.1`）。

- **引用与安置规则单一：** 信源路由与各文档内的文件引用与实际路径一致（鉴宝域信源指向 `maaracing_master/plugins/treasure/CODE_WIKI.md`，宪法内 `.md` 引用逐条可解析）；`docs/plan/` 保持「AI 生成过程产物」的单一语义，`docs/` 为信源根。

### 暂存（未发布 · 待并入下一版本）大厅新增两处自动处理：手柄指引弹窗与待机唤醒 🕹️

- **手柄操作指引弹窗自动关闭：** 游戏每次启动会在大厅弹一次手柄操作指引。它不是把背景压暗而是**整片高斯模糊**，鉴宝入口卡片在模糊背景上仍以 0.86 的分数命中，于是程序会对着弹窗画面继续判"大厅"。现在在大厅判定之前先认出这张弹窗，点掉面板右上角的 X 再回主流程。

- **大厅待机自动唤醒：** 挂机久了大厅 UI 收起、进入展车轮播的待机画面，此时底部图标组全部消失、没有任何可点目标。程序以「聊天框移到最左端」为唯一稳定证据识别待机，点左上角空白处唤醒，并留 1.5 秒消化「待机 → 大厅一闪 → 弹窗」的过渡帧。

- **质量门禁：** `pytest` 426 passed（新增 8 条：输入原语 5 条 + 真源机检 3 条）· `check_truth` 图 27 节点自洽。

- **验证状态：** 两条路径的真机触发（弹窗实际命中、待机唤醒确实有效）尚待复验——今天的真机场次里弹窗未出现、大厅也未进待机，目前支撑来自离线实验与静态门禁。

### 暂存（未发布 · 待并入下一版本）鉴宝历史回合报价录入修复 + 数据页性能卡 🔧

- **历史回合报价不再丢（用户可见）：** 开着「调试落盘」跑鉴宝时，玩家出价表的历史回合列此前会整列空白、上一轮对手快照建不出来，并连带让程序在下一回合迟迟不出价——真机实测有一场第 1、2 两回合完全没录到、第 2 回合白过。同一台机器同一设置下现在五回合报价全部录入，识别结果丢弃率 18.8% → **0%**。根因是调试图的编码开销在满负荷时挤掉了识别线程的采样时间窗，与出价策略本身无关。

- **调试图改为 JPG（用户可见）：** `debug/treasure/<会话>/` 下的 debug 图（叠了 HUD 的那张）从 `.webp` 改为 `.jpg`（q85），与 `raw/` 同族。体积同级（141KB vs 138KB）而单帧编码从 77.7ms 降到 3.9ms；小字（槽计数、时效读数）与原先同样清晰。老会话目录里的 `.webp` 原样保留，调试台与 ROI Studio 两种扩展名都认。

- **鉴宝数据页「性能监控」卡换成一版（用户可见）：** 原先是按竞速/YOLO 形状留的占位（帧率、YOLO 推理、截图耗时恒显示 `--`，而鉴宝根本不用 YOLO）。现在三项，只放看得懂且看完能行动的内容：
  - **画面响应** —— 每秒查看游戏画面的次数 + 近 30 秒走势 + 流畅/偏慢/卡顿；变慢时提示关掉调试落盘或降低游戏画质。
  - **机器负载** —— 本进程占整机 CPU 比例（按逻辑核数归一）+ 走势 + 正常/偏高/过载。
  - **识别健康** —— 一盏灯加一句人话：`报价读取正常 / 偏慢 / 读不到`。
  
  竞速模块保持原卡不变。

- **日志新增会话性能汇总行：** 每场结束与每次停止时各打一条 `[鉴宝][性能]`，一行给全：识别结果应用/丢弃数与丢弃率、报价窗口内的读取情况、时效与识别耗时的 p50/p95、决策帧间隔、调试落盘丢帧数与队列峰值、识别健康三态。**反馈问题时把这一行带上即可定位**，不必描述现象。

- **CPU 亲和性不再静默跳过：** 该优化依赖可选组件 psutil，缺失时以前只记一条 DEBUG；现在改记 WARNING 并写明后果。当前不装它也没有实测损失，属可选增强项。

- **质量门禁：** `pytest` 426 passed（本轮新增 31 条：性能仪表与三态阈值校准 28 条、CPU 时间原语与标准库对拍 3 条）· `check_truth` 图 27 节点自洽 · 真机三场复验：R1\~R5 报价全录入、丢弃率 0%、调试落盘零丢帧、识别健康全程 ok。

## 2026-09-11

### 暂存（未发布 · 待并入下一版本）NavKit Studio 三页合一（ROI 校准台 / 策略表 / 模板截取）🔧

- **新入口** **`tools\navkit\studio.cmd`（零控制台弹窗）：** 一次拉起 MPE 本地桥与 Studio 服务，浏览器开 MPE + Studio 两个标签；`--stop` 把两者一并停掉。`mpe.cmd` 的启动目标同步切到 Studio 服务，「MPE + 策略表」的原有用法不变。

- **ROI 校准台回归（离线调参闭环）：** 会话帧下拉（`debug/treasure/<会话>/raw/`）回放 + 拖框改选区 + 单帧测分 / 跨帧分布 / OCR 识别 / 从截图裁剪模板图。测分与跨帧直接调用运行时同一套匹配引擎，台里看到的分数就是跑图时的分数。

- **三页同端口互切：** 壳页统一标签栏承载 ROI 校准台 / 策略表 / 模板截取，ROI 页未保存的编辑在标签上显示提示点；策略表由独立小服务并入本服务，旧书签 `http://127.0.0.1:26530/` 直达壳页。

- **ROI 数据面以 v4 真源为准：** 读面按锚点类型分三组（模板 / 点位 / OCR），另列出 pipeline 节点里逐处 rect 与调参段 rect；保存时先跑真源自洽校验三闸（图闭合 + 交叉互洽 + 几何）再原子落盘，写盘沿用各真源既有格式，重复保存字节级幂等。节点 rect 与 spec 锚点同值处默认联动写两面，可勾「仅改此面」单独改。

- **关闭页面后自动退出：** 页面开着保活连接，关掉页签后 Studio 服务在 15 秒内自动收摊（`--idle-exit 0` 可改为常驻）；脚本或 curl 直接调 API 时服务保持常驻。

- **LocalBridge 协议版本自检：** MPE 在线版每次大版本更新都会抬高本地桥的协议要求，本地 `mpelb` 落后时会在打开页面几秒后自行退出。新增自检脚本 `tools/experiments/v4-p3-studio/diag_lb_protocol_handshake.py`（隔离端口握手，不打扰正在用的实例），README 补对应排错条目与「两者必须同代」的口径；`mpelb` 查找的兜底路径修正为 `%APPDATA%`。

- **质量门禁：** `pytest` 362 passed（本机完整依赖）· LF 检出副本 354 passed · `check_truth` 图 21 节点自洽。

### 暂存（未发布 · 待并入下一版本）截图通道收敛为 WGC 单一来源 + 决策帧自节流闸门 🧹

- **截图通道唯一化（用户可见）：** 全部取帧统一由 WGC 中心采集缓存承担，取消 MAA 同步截图兜底通道；设置页「截图方式」选项随之移除（WGC / MAA FramePool 二选一不再存在，该选项在此前的截图收敛后已不参与实际取帧路径，移除不改变运行行为）。**后果：** WGC 采集器不可用时一律判为「采集链路故障」并停止消费旧帧，不再退化为另一种截图方式。

- **插件契约变化（写新模块时必看）：** `ctx.bind_tasker(tasker, resource)` 绑定的对象改为帧注入控制器（读 WGC 中心缓存），前置条件由「窗口已连接」改为「WGC 采集器就绪」，不就绪时抛 `ModuleIntegrationError`——插件侧不再可能持有同步截图通道。`connect()` 内的 MAA 窗口控制器仅保留连接校验用途（`post_connection()`），不再产出任何帧。

- **决策帧自节流闸门：** `MaaRM_Policy` 桥内新增相邻决策帧最小间隔下限（默认 100ms，时钟与睡眠可注入以便确定性测试；下限传 0 即关闭，供压测复现上限）。真机决策段自身耗时约 125ms，故**今天不改变任何节奏**；它拦的是「决策段变轻后回弹闭环以每秒数百次自旋」这条结构性风险（CPU、trace 与决策契约落盘会被打爆）。停止信号分片中断，尾延迟 ≤20ms。

- **为什么只能做在桥里（实测依据，结论已入** **`docs/MAAFW_GUIDE.md`** **§5.2 执行语义第四条）：** MaaFW 5.12.3 离线探针（`tools/experiments/v4-frame-pacing/`）实测——`rate_limit` 的作用域只是「节点自己等后继命中的那段轮询」（全 miss 驻留路径下 50/600/2000 精确对应 62/606/≈2000ms）；而一旦下一跳命中挂 `jump_back` 的兜底节点，父 `dwell` 的 `rate_limit`/`pre_delay`/`post_delay` **全部旁路**，相邻决策帧间隔恒等于「兜底节点动作自身耗时 + ≈3.5ms 框架开销」（把三项限速拉到 2000/2000/500，间隔仍是 3.4ms）。⇒ **改图上的** **`rate_limit`** **调不动每帧重判的节奏**，节律只能在决策段自己按时间戳守。附带否掉一项担心：识别帧与决策帧的帧号漂移中位为 0（最大 1），不存在「一帧内两个取帧时刻」。

- **清理清单：** 移除内部截图方法两处及其调用链、MAA 截图适配壳 `PostScreencapCapture`、`capture_backend` 字段与对外属性、sidecar RPC `set_capture_backend`、前端截图卡片与状态回填；文档同步 `docs/CODE_WIKI.md` §4.10/§5.1/§5.6、`docs/MAAFW_GUIDE.md` §5.2、插件脚手架 `templates/plugin/README.md` 能力表。

- **质量门禁：** `pytest` 261 passed（新增 4 条闸门用例：轻帧补足、重帧不介入、停止即返回、下限可关）· `check_truth` 图 21 节点自洽 · 运行代码内截图后端触点仅剩 `connect()` 一处连接校验声明。

### v0.22.0-dev.1 项目更名 MaaRacingMaster（缩写 MaaRM）+ 内部架构收敛 🏷️

- **版本号：** `v0.22.0-dev.1`（预发布；基于 v0.21.0-dev.2 新开 minor 系列）

- **⚠️ 升级前必读——两处名称会变，且数据不自动搬迁：**
  - **软件更名：** 全称 `MaaRacingAssistant` → `MaaRacingMaster`，缩写 `MRA` → `MaaRM`。入口 exe、发行包名、窗口标题、日志文件前缀一并变更（完整同步面见下条）。
  - **用户数据目录换名：** 运行期数据根改为 Roaming 下的 `MaaRacingMaster` 文件夹。**新版本不会读取旧目录**：直接升级会让偏好回到默认、鉴宝历史记录重新起算。**需要保留历史的用户，请先用文件管理器把旧文件夹整体改名或复制为 `MaaRacingMaster`，再启动新版本。**

- **本版重心在内部收敛：** 识别真源迁移到 pipeline 节点 + policy 数据面（NavKit v4）、截图统一为 WGC 单一来源并加决策帧自节流、进程内时钟按用途分族、调试存图与决策段解耦、构建期体积闸门与包内自检前移、CI 单测门禁转绿，并补上一套源码直跑的开发入口。对玩家的可见交互与活动流程无实质改动。

- **验证状态：** 上述改动已通过单元测试与真机冒烟；覆盖全部活动流程的完整真机回归仍在进行中，欢迎试用并反馈问题。

- **项目更名（一次覆盖全部标识面）：** 全称 `MaaRacingAssistant` → `MaaRacingMaster`，缩写 `MRA` → `MaaRM`。同步范围：产品名与窗口标题、发行包 `MaaRacingMaster-<ver>-win-x64.{zip,7z}` 与根入口 `MaaRacingMaster.exe`、Python 包 `maaracing_master`、C# 工程 `MaaRacingMaster.Shell` / `MaaRacingMaster.Launcher`、MAA 自定义识别与动作注册名 `MaaRM_Template` / `MaaRM_Click` / `MaaRM_Policy`、日志文件名前缀 `MaaRM_*.log`、AppRoot 环境变量与单实例互斥体名。界面短名位显示 `MaaRM`，关于页与文档用全称；源码仓库与 CNB 镜像仓库同步更名，程序内「检查更新」与「公告」读取源随之切换

- **用户数据目录换名（升级需手动搬迁）：** 运行期数据根目录改为 Roaming 下的 `MaaRacingMaster`，内含五类——`config/profile.json`（偏好与可选项）、`data/treasure/treasure.db`（鉴宝历史落盘库）、`logs/`、`framework/`（MAA 框架自产物）、`debug/`（调试截图会话）。**本版本不做自动搬迁**：升级后偏好回到默认、鉴宝历史库重新起算；需要保留历史数据的用户，请把旧目录整体改名或复制为 `MaaRacingMaster` 后再启动

- **CI 单测门禁转绿：** master 分支的 `Test` 工作流自 2026-09-10 起连红三次。根因是 5 个运行时测试文件在 CI 轻依赖环境（只装 pytest + numpy + opencv-headless）下于收集期 ImportError，导致 pytest 整体中断、实际零个测试被执行。已按仓内既有口径补 try-import + 整文件跳过守卫。轻依赖模拟 216 passed / 41 skipped / 0 error，完整依赖本机 257 passed

- **质量门禁：** `pytest` 381 passed · `dotnet build` 0 警告 0 错误 · `check_truth` 图 21 节点自洽 · 被改 JSON 全部合法

## 2026-09-05

### v0.21.0-dev.2 发行包 7z 双产物落地 + 体积优化收尾 + 交付账本 📦

- **版本号：** `v0.21.0-dev.2`（预发布；基于 v0.21.0-dev.1 顺延；本轮为形态落地 + 体积优化收尾，内容侧无 payload 改动）

- **7z 主推档 + zip 保底双产物（assemble.ps1 新增** **`-SevenZ`）：** 产出 `MaaRacingAssistant-<ver>-win-x64.7z`（solid LZMA2 256M，参数 `-mx=9 -m0=LZMA2:d=256m -ms=on`，与 C1 基准一致）+ `.7z.sha256`；zip 保底始终产出（§7 不变）。CI（release.yml win-build）加 `-SevenZ` 并强校验双产物，GitHub/CNB Release 各挂 4 件资产（zip+7z+各 sha256）

- **体积收益:** 内容侧 **Δ = 0**（本轮不改 payload，Installed 468.86 / Download zip 198.79 均与 0.20.0 收尾一致）；形态侧新增 7z 主推档 **198.79 → 136.59 MiB（−62.20 / −31.3%）**。累计链条（自 v0.20.0-dev.2 起点）：zip 211.37 → 198.79（0.20.0 exp8+exp9 收尾，−12.58 → 136.59（本轮 7z 形态，−62.20）。解压峰值内存：solid 342 MiB vs zip 10.7 MiB——内存敏感用户取 zip 保底档

- **Win10 兼容说明（用户可见）：** 主推档 `.7z` 需 Windows 11 23H2+ 原生解压（22H2 经 KB5031455 支持），或安装 7-Zip/兼容解压软件；`.zip` 任何 Windows 双击即解压、零依赖。→ **保底档恒有** **`.zip`** **兜底**

- **7-Zip LGPL 合规：** `scripts/release/tools/7za.exe`（7-Zip 26.03 x64, standalone 1.27MB）入库，随附 `7za_License.txt` + `tools/README.md`。本项目使用 7-Zip 部分文件（7za.exe），7-Zip 以 GNU LGPL 发布，源码见 `https://www.7-zip.org/`

- **体积优化收尾（0.20.0 系列 exp8+exp9 正式编入，经评审签字）：** exp8 ORT offline tooling（google/protobuf + flatbuffers，−0.96 MiB）+ exp9 cv2 videoio ffmpeg backend（opencv\_videoio\_ffmpeg500\_64.dll，−29.45 MiB），合计 Installed −30.41 MiB（499.24 → 468.86）。B4′-0 发布完整性核验通过（rapidocr 三模型齐全 + 离线构造成功）；EXP-9 JSONL 污染专测通过（stdout 非协议行 0）

- **账本交付：** `runtime-pruning-policy.md` 负结果区 + `release-size-report.md`（0.20.0/0.21.0 双档）结构化落地；CLOSED-ABSENT/NEGATIVE/UNSAFE/KEEP 分类；size gate 增设 `sevenz` 记录（`baseline_7z_mb`/`delta_7z_mb`）——**7z 基线自 v0.21.0-dev.2 起建立，本期无 delta**；zip 仍为 baseline 主判据（468.86/198.79），`delta_total`/`delta_zip` 本期 = 0

- **细节修复：** `$sevenz` 产物体积变量与 `$SevenZ` switch 的 PowerShell 变量名大小写不敏感冲突导致 String→SwitchParameter——改名 `$sevenzFile` 规避；7za 打包改 cwd+`.` 避免 `\*` 通配符被 PS 预展开

- **验证：** assemble 复用缓存产出 zip 198.79 + 7z 136.59，Size gate no-regression；7z 与 zip 解压产物 **2267 文件逐字节 hash 全一致**（0 缺失/0 独有，证 7z 只改 packaging 不改 payload）

- **PublishTrimmed：** 留作下轮（最贵，需 rebuild + 完整 GUI L2/L3）

## 2026-09-04

### v0.21.0-dev.1 注册表权限优化中心 + 启动体检 + ms-gamebar 协议弹窗修复 🔧

- **版本号：** `v0.21.0-dev.1`（预发布；基于 v0.20.0-dev.2 新开 minor 系列）

- **注册表权限优化中心（设置页新增入口）：** 数据驱动的优化项注册表 `_REGISTRY_OPTIMIZATIONS`，新增泛化 RPC `get_registry_optimizations` / `set_registry_optimization`；设置页右侧新增「权限优化」卡片 → 打开优化中心弹窗，每项展示状态徽章/影响性质/值名/可选值/完整注册表路径与后果（灰字），可单独优化或恢复系统默认，操作后自动刷新状态

- **三项优化项：** ① Xbox GameDVR 后台捕获（`AppCaptureEnabled`，杜绝 ms-gamebar 抢焦点）；② 手柄 UI 导航（`ControllerToVKMapping\Enabled`，杜绝打字弹手柄虚拟键盘）；③ ms-gamebar 协议弹窗（写 `NoOpenWith` 屏蔽「获取打开此链接的应用」对话框，支持多路径标记值 kind）

- **启动体检弹窗：** 启动时检测未优化且未忽略项并弹一键优化引导；新增「下次不再提醒」按项忽略（持久化 profile `ignored_optimization_prompts`，未来新增优化项不受影响），忽略项可在优化中心「恢复启动提醒」

- **优化项类型体系：** 区分 `dword`（写数值）与 `noopenwith`（写/删 `NoOpenWith` 标记值，多路径）；可选值与按钮文案由后端下发，前端不硬编码语义

- **UI 通用修复：** `openModal` 组件升级支持弹窗内容滚动（卡片限高 86vh，标题/按钮固定，长内容只滚内容区）+ 长串强制断行（注册表路径 `break-all` 防溢出）

- **本机已修复：** 三项优化均已生效（GameDVR=0、ControllerToVKMapping=0、ms-gamebar 协议 NoOpenWith 写入并回读确认）

- **RPC 实测：** `py_compile` / `node --check` 通过；`get`/`set`/忽略链路（写→新进程读→恢复）JSONL 全流程验证通过，本机无测试残留

## 2026-09-04

### v0.20.0-dev.2 发行包体积优化：618 → 499 MB（-19.3%）📦

- **版本号：** `v0.20.0-dev.2`（预发布；基于 v0.20.0-dev.1，11 轮单变量实验 + Runtime Closure Auditor 双工具）

- **10 项 SAFE 裁剪正式入 release pipeline（assemble.ps1** **`-Configuration Release`** **默认全启用）：** ① WinAppSDK AI/ML 死链 -43.65MB（exp1）；② Widgets 死链 -2.49MB（exp2）；③ Python ORT `capi\onnxruntime.dll` -20.13MB（exp3，pyd 自带 ORT 引擎）；④ PIL `_avif` native ext -7.52MB（exp4A，惰性零路径）；⑤ .NET crash diagnostics（createdump/mscordaccore/DiaSymReader）-4.73MB（exp4B，**SAFE FOR NORMAL OPERATION**：降低崩溃转储/SOS 能力，保留 mscordbi）；⑥ NumPy dev/build 目录 -1.87MB（exp5A，不 patch numpy 源码）；⑦ 全部 `.pyi` typing stubs -1.13MB（exp5B-1，267 个）；⑧ console wrappers `packages\bin\*.exe` -0.83MB（exp5E-1，8 个 pip wrapper，f2py.exe 为孤儿）；⑨ SymPy -25.37MB（exp6，**SAFE FOR CURRENT MRA**：仅 ORT 离线 symbolic\_shape\_infer/transformers 工具需要，全链 360 模块 trace 证实运行零加载）；⑩ MaaAgentBinary -12.53MB（exp7，**SAFE FOR CURRENT MRA**：Android/ADB 代理二进制，MRA 仅用 Win32Controller）

- **assemble.ps1 管线收敛：** 新增 `-Configuration Release|Experimental`（默认 Release=10 项裁剪全自动启用，10 个 `Remove*` 实验开关降为内部不再暴露）与 `-DisableReleaseOptimizations`；新增 Production Guard（依赖敏感项白名单失效即阻断发布）+ 反向清理验证（应删不存在/应保留存在）+ Release Size Gate（对比 baseline 503.23/212.98，超 ±5MB 报 SIZE REGRESSION，自动生成 `release-size-report.json/.md`）

- **裁剪白名单文档：** 新增 `scripts/release/runtime-pruning-policy.md`（来源实验/收益/验证状态/代价说明；明确不纳入：pygrun 6KB、mpmath、mscordbi、numpy.typing、dist-info RECORD、INSTALLER/WHEEL/REQUESTED、PublishTrimmed 等）

- **Runtime Closure Auditor（新增** **`tools/runtime_audit/`）：** 四层分析（Python AST 静态 import 图 / 发行 runtime trace / pefile PE 依赖图 / native 加载采集）+ 6 类文件分类 + 删除信心分级 + 人工结论 oracle 回归（KEEP 394/394、REMOVE 88/88 全绿）；V0.2 完成 app/154.58MB 归因（.NET 63.11 + WinUI/XAML 45.13 + WinRT 投影 25.56 + WinAppSDK 6.94 + WebView2 1.54，UNKNOWN 184MB→12.29MB，证实无第二个 20-40MB 大洞，剩余为架构成本）

- **关键发现：** 新版 pip 在 `--target`+host 3.11 下会预编译 `__pycache__`（3076 个/74.89MB，历史构建为 0），assemble 新增统一清理段保持发行口径；onnxruntime dist-info 为 `onnxruntime_directml-*` 命名，`importlib.metadata.version('onnxruntime')` 本就 PackageNotFoundError（非裁剪导致）

- **正式产物验证：** 从源码 + `build\runtime-full`（Python 3.11.9 embed + lock 全量 pip）全新构建，未裁剪验尸全过；total 499.24MB / zip 211.37MB（vs exp7 baseline delta −3.99/−1.61，no regression）；smoke test 全 PASS（NumPy/OpenCV 5.0.0/ORT-DML 真实推理/RapidOCR/MaaFramework 含 Win32Controller/Racing/Treasure 业务逻辑/截图构造/手柄）；GUI 与真实窗口抓帧未实机验证（requireAdministrator 触发 UAC）；**Production Release = READY**

- **其他：** `.gitignore` 补 `build/exp*/`、`build/runtime-full/`；实验产物 exp1-exp7 已清理归档

### v0.20.0-dev.1 架构底座重构 + 截图/导航线程化 + CNB 双源分发 🚀

- **版本号：** `v0.20.0-dev.1`（预发布；基于 v0.19.1 新开 minor 系列，累计 24 项提交）

- **架构底座重构（不拥有策略的通用能力层）：** ① ROI 统一配置底座（`ROIConfig` + 坐标契约 + schema 校验）；② `StageTracker` 阶段记录器/校验器（断点换算收敛，racing 观测等价迁移）；③ `RenderPlan` + `LayerRegistry` 渲染计划底座（能力选择器 + 通用调度）；④ DebugIO worker 底座 + `FrameSource`/`DebugSink` 接口；⑤ DebugStudio 通用 server + 领域 adapter 注册端点（整合旧 treasure studio，会话路径与 `%APPDATA%` 对齐）

- **截图 WGC 中心化（根治多线程争抢截图通道）：** `core/wgcap.py` 重写为单生产者/多消费者中心采集——Windows Graphics Capture 独立于 MAA FramePool，60fps 上限节流，DWM 客户区精确裁剪（渲染链判定：帧尺寸≈DWM 边界→裁剪窗口装饰链，否则独立交换链整帧即内容），`get_latest_rgb()` 惰性 720p 标准帧缓存；实测 35fps、单次读取 17-27µs、多线程并发读取零争抢；`CaptureAdapter.screenshot` 路由 WGC 优先 + MAA 兜底，controller 生命周期托管（失败回退不阻断）

- **手柄导航线程化（根治导航阻塞吃识别窗口）：** `GamepadClicker` 常驻导航线程 + 任务/结果单槽（submit/consume\_result/is\_busy/nav\_progress/cancel/swap\_gpad/shutdown），五项并发契约（结果不可覆盖、busy 含 DONE 态、快照发布、Event 取消、设备重建先退出 worker）；treasure 主循环改异步点击协议（consume → decision → submit，click > shoo 优先级），光标驻留看守异步化（避让识别冷却缩至 1 帧）

- **点击/避让体验修复：** 拨号盘误避让根因修复（`_active_stage_rois` 按出价阶段收窄守卫激活集，bidding 期仅激活出价按钮）；按钮/超大选框统一按框 70% 容差停靠；PEEP 叠加层显示置信度与五个次选候选（选中绿/未选中黄）

- **CNB（cnb.cool）双源分发体系：** ① GitHub → CNB git 镜像 workflow（concurrency 串行化防发版竞态）；② 程序检测更新/公告改 CNB raw 优先 + GitHub 兜底（`docs/latest_release.json` 版本标记由 release CI 自动生成回写 master 随镜像同步）；③ CNB Release 资产同步 job（CNB 无 Windows 构建节点，GitHub 构建 zip 后经 CNB OpenAPI 三步上传：预签名 URL → PUT → verify），国内用户下载直链匿名可达；④ 修复 CNB API GET 调用缺 `Accept: application/json` 必 406 的隐患

- **Python 版本统一 3.11：** pyproject / README / CONTRIBUTING / SELF\_CHECK / CODE\_WIKI / CI 矩阵统一 3.11（与发布链路 embedded 3.11 对齐，消除 3.9/3.10/3.13 混杂）；CI 测试补装 numpy/opencv-headless 修复 collect 失败

- **用户数据目录规范化：** 五目录结构（logs/treasure/profile 等）统一 `%APPDATA%/MaaRacingAssistant`，DebugStudio 会话路径对齐

- **racing/treasure 手柄收敛：** 两插件私有手柄/截图逻辑收敛到 core 能力层，消除第二设备冲突

- **质量门禁：** `pytest` 178 passed（本地 venv Python 3.11.9）

## 2026-08-31

### v0.19.1 全面 debug 与冗余剔除（Python 3.9 兼容 + 三处真 bug）🔧

- **版本号：** `v0.19.1`（正式版补丁；基于 v0.19.0，本次为修复+清理，无新功能）

- **修复 Python 3.9 兼容性崩溃：** `__init__.py` / `core/opencv_utf8_patch` / `core/sidecar` / `core/wgcap` / `core/window_utils` / `core/yolo_detector` / `plugins/racing/loop` / `plugins/racing/module` 共 8 个文件补充 `from __future__ import annotations`。原在 `requires-python >=3.9` 声明下限的 Python 3.9 下整个包 import 即崩（`TypeError: unsupported operand type(s) for |`，注解定义时求值），现已与其余 19 个文件风格统一

- **修复** **`_SHCORE`** **大小写 bug：** `core/window_utils.py` 模块级定义 `_SHcore`（小写 h），`ensure_dpi_aware()` 却调用 `_SHCORE`（大写）→ NameError 被 try/except 吞掉，**Per-Monitor DPI 首选分支永远静默失效**回退 System DPI，已改回 `_SHcore`

- **修复** **`DEFAULT_TREASURE_RISK_CAP`** **漏** **`self.`：** `plugins/treasure/module.py` `set_module_config` 内裸引用类属性常量（实为 `self.DEFAULT_TREASURE_RISK_CAP`），`treasure_risk_cap` 配置值非法（非 int/负数）时 NameError，已补 `self.`

- **修复 racing 基准测试悬空指标：** `plugins/racing/loop.py` benchmark 采集 `_maa_get_us` 从未被赋值（重构截图链路后遗留），`capture_backend=maa` 时必 AttributeError；该指标已无数据源，删除 `maa_get` 采集与输出段

- **冗余剔除：** `loop.py` 删除已废弃死代码（`_use_fast_cap`/`_fast_cap_mode` 字段、`_cap_fast` 空壳方法及其调用分支）；删除 5 处未使用导入（logger `sys`、window\_utils `Path`、loop `typing.Any`、treasure module `BidDecision`+`DECISION_TARGET_SECOND`、strategy `dataclasses.field`）

- **类型标注失真修正：** treasure/module.py `_appr_tpls`（3→5 元组）、`_ocr_pending`（4→6 元组）、`_extract_round_from_stage` 参数 `str→str|None`、`opponent_ids` 显式构造 3 元组、`_load_appraiser_templates` 内 rect 显式 4 元组；treasure/eggs.py `_entry` rect 显式 4 元组；core/base.py `ActivityContext.capture/gamepad` 属性标注去除 `|None`（getter 首次访问即装配恒非 None，消除 racing 域 9 处 pyright 误报）

- **公告规范：** 新增 `docs/announcement.md`（发布时机 + JSON 格式 + 三条红线），`AGENTS.md` 加指针；`docs/announcement.json` 补缺失的 `date` 字段

- **文档同步：** 赛车 `CODE_WIKI.md` §5.1/§5.2/§6 与主文档 `_cap` 方法索引删除 `_use_fast_cap`/`_cap_fast` 过时描述，改为当前截图后端（`wgc_latest`/`maa` 按 `capture_backend` 分派）；`pyrightconfig.json` 增 `extraPaths` 消除测试导入误报

- **质量门禁：** `pytest` 22 passed · `compileall` 0 错误 · `pyright` 67→22（余 22 个为已验证运行安全的推断误报，刻意不加 `type: ignore`）

## 2026-08-24

### v0.19.0 正式版：入口体验三处修复（提权 / 图标 / 解压目录）🏷️

- **版本号：** `v0.19.0`（正式版；基于 v0.19.0-dev.1 系列成熟，标记为稳定可用交付）

- **修复启动错误码 740（权限不足）：** 根因 Launcher `CreateProcessW` 拉起声明 `requireAdministrator` 的 `app/mra_shell.exe` 时不弹 UAC，直接返回 `ERROR_ELEVATION_REQUIRED (740)`。为 `apps/mra_launcher/launcher.manifest` 声明 `requireAdministrator`，入口即提权，子进程继承同一 token，单次 UAC 后正常启动

- **修复入口图标缺失：** 新增 `apps/mra_launcher/launcher.rc`，一次嵌入 `assets/icon.ico`（图标）与内嵌 application manifest；MSVC 编译先 `rc.exe` 生成 `.res` 再 `cl.exe` 链接，消除发布包入口 `MaaRacingAssistant.exe` 无图标、与解压包层级不一致的问题

- **修复解压多套一层同名目录：** `assemble.ps1 §7` 由「包一层版本目录」改为「用目录内容作 zip 顶层」，用户选「解压到文件名文件夹」不再双重嵌套，解压后 `MaaRacingAssistant.exe` 直接在解压根

- **构建侧加固：** `rc.exe` 对带引号的 `/fo` 及含环境变量路径存在 RC1109 坑，assemble 改为先 `Push-Location` 到 `apps/mra_launcher`、用无引号相对名编译后删除临时 `.res`，规避本地/CI 差异

- **发版说明：** 本版本经实机验证为可正常使用的稳定交付（对外/引导用户指向本版本）

## 2026-08-23

### v0.19.0-dev.1 原生 Launcher + app/ 产品目录布局 🏗️

- **版本号：** `v0.19.0-dev.1`（预发布；基于 v0.18.0-dev.2 新开 minor 系列）

- **根目录清爽（产品目录/实现目录分离）：** 发布包根目录只保留唯一入口 `MaaRacingAssistant.exe`（native Launcher，111KB 零 runtime 依赖）+ LICENSE/pyproject/第三方许可，GUI 的 dotnet publish 全部 dll 收进黑盒 `app/` 子目录（600+ 文件不再平铺在 exe 旁）；`assemble.ps1 §2` 复制目标整目录进 `StageRoot\app\`，`§2.5` 语言包/WebView2 清理作用于 `app\`

- **新增 native Launcher：** `apps/mra_launcher/launcher.c`，`CreateProcessW` 启动 `app\mra_shell.exe`；定位自身目录为 AppRoot → 设 `MRA_APP_ROOT` 环境变量（transport，不经命令行 quoting，中文/空格路径安全）→ `cwd=AppRoot` → 等待子进程退出并回传退出码；不传透用户参数

- **AppRoot 路径协议：** `MainWindow.ResolveRepoRoot()` 优先级 `--app-root` > `MRA_APP_ROOT` > 开发回退向上找 `pyproject.toml`，均 `Path.GetFullPath` 归一化（不变量：启动期只解析一次）；GUI/Python/frontend 全部相对 AppRoot 锚定

- **CI 集成：** `release.yml` win-build job 增「设置 MSVC」（`mlocati/setup-msvc@v1`）编译 Launcher；assemble.ps1 §2.6 双路径（PATH 里的 cl 优先，否则 vcvarsall 探测）

- **可安装性：** PoC 验证通过（双击/含空格中文路径/外部 cwd/sidecar/frontend/e2e）；既有 `runtime-cache`/`publish-cache` 缓存指纹机制不受影响

### v0.18.0-dev.2 插件资源自包含 + 720p 窗口统一 🏗️

- **版本号：** `v0.18.0-dev.2`（预发布；基于 v0.18.0-dev.1 同系列顺延）

- **racing 资源随插件自包含：** 5 张模板 jpg（settings/activity/find\_opponent/store\_popup/round1\_end）与 `pipeline/tasks.json` 迁入 `plugins/racing/resources/`；`module.py` 增 `_RES_DIR` 常量使 `post_bundle(str(_RES_DIR))` 自引用（不再依赖主程序 `assets/resource/`）；`loop.py` 结束模板、`navigation.py` `_load_template` 均改 `__file__` 相对定位到模块资源目录（顺带修掉 loop.py 原 `parent.parent` 层级错误隐患）；原 `assets/resource/` 已删除

- **CODE\_WIKI 随插件：** `docs/CODE_WIKI_RACING.md` → `plugins/racing/CODE_WIKI.md`、`docs/CODE_WIKI_TREASURE.md` → `plugins/treasure/CODE_WIKI.md`；主文档保留在 `docs/CODE_WIKI.md`，AGENTS.md / 主文档跨域链接 / ARCHITECTURE / SELF\_CHECK 均已同步更新为插件相对路径

- **720p 窗口统一：** `window_utils.py` 新增 `resize_game_window_720p`，controller 连接游戏时统一将客户区调为 1280×720（截图/模板/ROI 归一化基础），调整失败不阻断

- **onnx 能力按需校验：** `base.py` 增 `onnx` capability；`sidecar.py` 启动校验改为仅对申明 `REQUIRES` 含 `onnx` 的模块（如 racing）检查本地模型，鉴宝等无需模型的模块不再被无条件拦截

- **打包适配：** `assemble.ps1` 白名单移除 `assets\resource`（插件资源由 robocopy 整个包带上），保留 `assets\model`（YOLO 模型主程序共享）；文档遗留过时路径注释（detector.py / extract\_treasure\_templates.py）修正

## 2026-08-22

### v0.18.0-dev.1 模块化架构分离（core/ + plugins/ 插件体系）🏗️

- **版本号：** `v0.18.0-dev.1`（预发布；基于 v0.17.0-dev.1 新开 minor 系列）

- **主程序抽** **`core/`** **包：** `controller / sidecar / registry / base / capabilities / logger / window_utils / paths / debug / wgcap / yolo_detector / vgamepad_lazy / opencv_utf8_patch / pipeline_logger` 全部迁入 `maaracing_assistant/core/`，`modules/` 包移除

- **活动插件化：** racing / treasure 下沉 `maaracing_assistant/plugins/<id>/`（各含 `manifest.py` + 模块文件）；`core/registry.py` 自动扫描 `plugins/*/manifest.py` 注册，**删目录即剥离、丢目录即安装**，GUI 列表自动随之变化；sidecar 默认模块改 id 引用（解耦具体插件包）

- **treasure 拆落盘子域：** 结构化落盘 / DB 连接 / 会话总结迁入 `plugins/treasure/store.py`（`TreasureStore`），`module.py` 减负为「状态机 + 编排」

- **资源随插件：** 鉴宝模板与 `treasure_rois.json` 迁入 `plugins/treasure/resources/`，模块以 `_RES_DIR` 自引用（不依赖主程序 `assets/`）

- **根目录收敛：** `.design/` → `docs/design/`；`config/` → `assets/config/`；`maaracing_assistant.sln` → `apps/`；`assemble.ps1` 白名单 / `.gitignore` / `CODE_WIKI` 目录树同步

- **禁用字节码写入：** 入口 `sys.dont_write_bytecode = True`（含 pytest conftest），运行不再散落 `__pycache__`

- **README 更新：** 项目结构节改为 `core/ + plugins/` 结构；MaaFramework 徽章版本同步；移除本地演示视频素材（README 改用 GitHub 远程 URL）

## 2026-08-21

### v0.17.0-dev.1 鉴宝偏好持久化 + 开始倒计时 + 掉线捡漏 🚀

- **版本号：** `v0.17.0-dev.1`（预发布；基于 v0.16.0-dev.2 新开 minor 系列）

- **鉴宝"可选项"持久化（sidecar.py）：** 刷几场 `max_daily_loops`、目标场次 `target_session`、每局亏多少 `treasure_risk_cap`、策略模式 `treasure_mode`，以及调试 `debug_mode` / `peep_enabled` 开关，写入 `%APPDATA%/MaaRacingAssistant/profile.json`（与数据库同目录）。改动时即时落盘，sidecar 启动自动回填下次会话。

  - 容错：只读写本程序白名单键；损坏 / 非 dict / 未知键 / 非法类型一律忽略，不因历史残留崩溃

- **开始按钮三秒倒计时（frontend/app.js）：** 点击「开始运行」按钮显示 `3·再点取消` 逐秒递减，倒计时期间再点一次即取消，倒计时结束才真正启动（给玩家切到游戏窗口 / 就位时间）。纯前端实现，后端未动

- **对手掉线视为捡漏价（bid\_strategy.py）：** 对手槽读值为 `0`（掉线 / 没出价）由「按信息缺失处理」改为「视为有效最低价」，可参与捡漏 / 赚蛋判定；仅 `-1`（未读到）才算信息缺失

## 2026-08-20

### 未发版变更：README 结构调整（快速开始前移 + 顶部个人维护说明）📄

- **性质：** 未发版变更（纯文档，README.md 调整；master 直接提交、不 tag）

- **快速开始前移：**「快速开始」整节（下载即用包 + 从源码构建）前移到「支持本项目」之后，目录同步更新，让首次访客先看到"怎么直接用"

- **顶部新增个人维护说明：** 免责 WARNING 上方加「维护说明（个人项目）」块——个人业余维护、无长期维护承诺 / 无 SLA、打 tag 即出正式 release 包、欢迎 fork/提 PR 参与维护

### 未发版变更：开源就绪完善（下载即用引导 / Python 版本统一 / CI 注释澄清）📦

- **性质：** 未发版变更（docs+ci 为主，不含运行时模块代码）；master 直接提交、不 tag，触发 release 的时机另行决定

- **README 增加「下载即用包」引导：** 普通用户从 GitHub Releases 下载 `MaaRacingAssistant-<版本>-win-x64.zip` → 解压 → 双击 `mra_shell.exe`（自带 embedded Python/依赖，无需编译）；快速开始按「下载即用 / 从源码构建」两类入口分流，并标注当前为开发阶段 pre-release

- **启动引导改指编译产物：** README / CONTRIBUTING 原引导「双击 MaaRacingAssistant.lnk」（被 .gitignore 排除，clone 后并不存在），改为 `apps\mra_shell\bin\x64\Debug\net8.0-windows10.0.19041.0\win-x64\mra_shell.exe`

- **Python 版本统一：** `pyproject.toml` `requires-python` 由 `>=3.9` 改为 `>=3.10`，与 README / CONTRIBUTING 的 3.10+ 一致（代码已使用 `int | None` 等 3.10+ 语法）

- **CI 依赖关系澄清：** 给 `[test]` extra 与 test.yml / release.yml 补注释，说明 CI 不采用 `.[test]` 的原因（会连带安装 onnxruntime-directml / windows-capture 等仅 Windows 有 wheel 的主依赖，Linux runner 装不上）

- **开源隐私红线扫描：** 确认当前快照无绝对路径 / 密钥 / 真实 IP / 真实作者信息，可安全开源

### v0.16.0-dev.2 鉴宝结算弹窗回退修复（感知锚点补齐）🐛

- **版本号：** `v0.16.0-dev.2`（预发布；基于 v0.16.0-dev.1 顺延）

- **根因：** v0.16.0-dev.1 引入 active\_rois 阶段感知裁剪后，`_GLOBAL_ANCHORS` 只含游戏大厅锚点 `hall_peak_appraise_card`，缺「鉴宝大厅(选择场次)」识别 ROI `hall_session_cards`。结算弹窗点关闭后画面已回鉴宝大厅，但检测器只扫弹窗 ROI + 游戏大厅卡片，永远看不到 `hall_session_cards` → 阶段冻结在「结算弹窗」，无法触发 `_accept_stage` 的弹窗链回退（`POPUP_LOOPBACK_STABLE_FRAMES` 连续帧确认）。

- **修复：** `treasure_module.py` 把 `hall_session_cards` 并入 `_GLOBAL_ANCHORS`，任何阶段（尤其结算弹窗）都能识别回退落点的鉴宝大厅页，循环恢复正常。

### v0.16.0-dev.1 鉴宝报价双通道修复 + debug 落盘异步化 🚀

- **版本号：** `v0.16.0-dev.1`（预发布；基于 v0.15.0-dev.8 新开 minor 系列）

- **P4 双通道覆盖 bug 修复：** 结果槽拆双槽（关键通道 H+P4 / 全量通道其余），worker 第二段剔除 H/P4 不再同帧重复识别+覆盖关键结果；主线程每帧合并双槽消费 → P4 独立识别、时效最低

- **debug 落盘 IO worker（异步化）：** 渲染 HUD/ROI/PEEP + raw JPG + WebP 写盘移出主线程（生产-消费者，有界队列满丢帧不阻塞）→ wait\_result 段帧率不再被 \~67-100ms 同步存盘拖慢，真正逼近 150ms

- **报价槽级固化：** 连续 3 帧一致 + 前置槽约束才固化；未固化槽连续 3 帧无输出清空重读；wait\_result 帧率翻倍 + 假下降沿修复（读到报价即禁用重报）

- **窗口比例校验：** `ensure_game_window_min`（自动调窗）改为 `check_game_window_aspect`（只读校验 16:9，不符报错终止）

- **日志优化：** OCR 初始化日志降 DEBUG；GUI 会话总结日志分类

- **文档：** CODE\_WIKI\_TREASURE 同步双结果槽/IO worker；删除 docs/PRESENTATION.md

### 未发版工作区改动：数据目录迁移 + 窗口匹配调整 + 窗口准备机制

- **数据存储迁移（与安装目录解耦）：** 鉴宝落盘库从 `<项目根>/data/treasure/treasure.db` 迁到 `%APPDATA%/MaaRacingAssistant/treasure/treasure.db`，更新/覆盖安装不再影响历史数据

  - 新增 `maaracing_assistant/paths.py`：`user_data_dir()` 统一解析用户数据根目录（无 APPDATA 时回退包根 `data/`，兼容源码运行）

  - `treasure_module.py` 落盘与 `sidecar.py` 今日看板读取走同一函数，读写路径一致

  - 不做旧 data 目录迁移（项目早期，无痛感）

- **窗口匹配调整：** `find_game_hwnd()` 改为标题关键词（巅峰极速/g112/Racing Master）子串匹配第一优先，去掉 UnrealWindow 类名匹配（多 UE 窗口场景会歧义连错）

- **窗口准备机制（按下开始后）：** `controller.connect()` 连接成功后 ① `activate_window(hwnd)` 把游戏窗口切到前台（还原最小化 + SendInput 注入 F13 解除前台锁定 + AttachThreadInput + SetForegroundWindow + 轮询确认异步切换）；② `is_window_on_screen(hwnd)` 校验窗口完整可见（四角均在显示器内），部分/完全拖出屏幕 → ERROR 报错并终止模块

  - 切前台失败仅 WARNING 提示（逻辑继续运行）；运行中点击仍保持「不抢前台」安全策略不变

***

### v0.15.0-dev.8 README 演示视频原生播放 + 爱发电徽章修复 + 发布流水线两处修复 + 多尺寸图标 🎬

- **版本号：** `v0.15.0-dev.8`（预发布；0.x 系列 tag 全部为 pre-release，基于 v0.15.0-dev.7）

- **③ README 演示视频原生播放（GitHub user-attachments）：** GitHub README 渲染器会剥离普通仓库文件路径的 `<video>` 标签（实测确认），必须用 GitHub 官方 user-attachments 机制——经 issue 上传 ≤10MB MP4 获得稳定 URL，`<video src="https://github.com/user-attachments/assets/9bf47361-2773-447c-9900-bdf70d4b2af0">` 内联播放（带控件）。已通过 issue #1（演示素材托管）上传压缩版（5.77MB）落地

- **④ 爱发电徽章修复：** 原 `api.swo.moe/stats/afdian/MaaRacingAssistant` 返回 `count:"**"`（第三方服务获取不到真实粉丝数），改为静态 shields.io 徽章「爱发电 赞助」链接到主页，已验证返回正常 SVG

- **① sdist/wheel 版本号回退 0.0.0（历史 bug，根因在 pyproject）：** `pyproject.toml` 的 `[tool.setuptools-scm]` 用了**连字符**，但 setuptools-scm v10（vcs-versioning）解析器用下划线 `[tool.setuptools_scm]` **精确匹配** tool 段 → 段识别失败（`section_present=False`）→ 版本推断被静默跳过 → sdist/wheel 版本号回退 `0.0.0`（自 v0.13.0 起一直存在；win-x64.zip 的 `_version.py` 由 assemble.ps1 直接生成，不受影响，故未暴露）

  - **修复：** ① `[tool.setuptools-scm]` 改 `[tool.setuptools_scm]`（根因）② 构建分发包改 `python -m build --no-isolation` 让 setuptools-scm 直读 git tag（在 tag 提交点 distance=0，验证通过），`SETUPTOOLS_SCM_PRETEND_VERSION` 保留兜底 ③ 安装依赖补 `wheel`

- **② Release 变更记录为空（公开仓单 tag 场景）：** changelog 逻辑 `head -n 2 | tail -n 1` 在**只有一个 tag** 时会误取当前 tag 自身，导致 `git log tag..tag` 空范围 → 变更记录为空

  - **修复：** 用 `grep -v` 排除当前 tag 再取最新一个，为空则回退列出最近 50 条提交

- **⑤ 应用图标改多尺寸合并 ICO：** `assets/icon.ico` 由单尺寸改为 16/32/48/64/128/256 六尺寸合并（32bpp），窗口/任务栏/高 DPI 各场景均清晰；`ApplicationIcon`（csproj）与 `AppWindow.SetIcon`（运行期）均验证兼容

- **验证方法：** 打 tag 后确认 ① whl/tar.gz 版本号为对应版本（如 `0.15.0.dev8`）② Release body 变更记录非空 ③ README 演示区视频可播放 ④ exe 图标多尺寸清晰

***

## 2026-08-20

### v0.15.0-dev.6 README 发布物料就绪 + 开发文档防上传 📦

- **版本号：** `v0.15.0-dev.6`（预发布；0.x 系列 tag 全部为 pre-release）

- **发布物料就绪（面向 v1.0.0 鉴宝单模块主打）：**

  - README 演示小节：引用 `assets/demo/mra_preview.mp4`（`<video>` 播放控件），去掉"素材录制中"占位

  - README 开发状态明确：巅峰鉴宝 = v1.0.0 主打（已闭环 + CI 单测回归）；极速狂飙 = 开发中，不纳入 v1.0.0

  - `assets/demo/README.md` 同步为实际素材清单（mp4 + 3 张截图已实拍入库）

  - `docs/PRESENTATION.md` 删除内部「发布衔接」节，头部改社区向表述

- **开发文档防上传：** `.trae/` 下 13 个历史遗留已跟踪文件（4 组 spec + 1 个 skill）`git rm --cached` 移出索引（本地保留），配合 `.gitignore` 的 `.trae/` 规则确保不再上传

***

## 2026-08-19

### v0.15.0-dev.5 内置 vgamepad wheel + 网络连通性自检（根治 CI sdist 卡死） 🐍

- **版本号：** `v0.15.0-dev.5`（预发布；0.x 系列 tag 全部为 pre-release）

- **确诊并根治 vgamepad 卡死：** `vgamepad=0.1.0` 的 sdist 在 CI windows runner 持续卡在 `Preparing metadata (pyproject.toml)`，两次复现（`dev.4` 原 job + 重跑 job），确认与网络无关

  - 方案：本地生成 `vgamepad-0.1.0-py3-none-any.whl` 入库 `scripts/release/wheels/`（纯 Python wheel，极小）；`assemble.ps1` 的 pip 加 `--find-links` 优先用本地 wheel，CI 不再触发 sdist 构建

- **`release.yml`** **新增网络连通性自检：** 组装前对 PyPI / files.pythonhosted / 清华 / 阿里多源做 `HEAD` 连通测试，提前暴露网络问题（不阻断，仅诊断）

- **`assemble.ps1`** **下载超时：** embedded 下载 `-TimeoutSec 120`、pip 加 `--timeout 60 --retries 2`、去除 `-q`（逐包输出定位）

***

## 2026-08-19

### v0.15.0-dev.4 CI 构建去静默 + pip 缓存兜底（定位卡点） 🔍

- **版本号：** `v0.15.0-dev.4`（预发布，针对 CI 组装仍静默无输出的定位优化；0.x 系列 tag 全部为 pre-release）

- `assemble.ps1` 的 `pip install` 去掉 `-q`，改用 `--progress-bar off`——逐包实时输出，能直接看到卡在哪个依赖；保留 `--timeout 60 --retries 2` 防无限挂

- `release.yml` 的 `setup-python` 加 `cache: 'pip'`，缓存 pip 下载缓存，作为 `build/runtime-cache` 之外的第二层兜底（首次即便失败，已下载的 wheel 也不会重下）

***

## 2026-08-19

### v0.15.0-dev.3 CI 构建缓存 + 下载超时（修复卡死） 🕐

- **版本号：** `v0.15.0-dev.3`（预发布，修复 v0.15.0-dev.2 首次全量可卡死后重发；0.x 系列 tag 全部为 pre-release）

- **修复 win-build 首次全量构建可无限挂起：**

  - `assemble.ps1` 给 embedded Python 下载加 `-TimeoutSec 120`，`pip install` 加 `--timeout 60 --retries 2`——首次在 CI 全新 runner 全量下载大依赖（torch 等）时，网络卡死不再无限挂起，超时即报错退出

- **CI 增加 runtime 缓存：** `release.yml` 以 `requirements-runtime-lock.txt` hash 缓存 `build/runtime-cache`，避免每次重新下载几百 MB runtime 依赖（与 assemble 本地缓存机制自然衔接）

***

## 2026-08-19

### v0.15.0-dev.2 构建失败修复 + 本地打包缓存提速 🔧

- **版本号：** `v0.15.0-dev.2`（预发布，修复 v0.15.0-dev.1 构建失败后重发；0.x 系列 tag 全部为 pre-release）

- **修复 CI「构建解压即用 Windows 包」失败：**

  - `scripts/release/assemble.ps1` 修复 `-OutRoot` 指向的 `build/release` 目录不存在时 `Resolve-Path` 抛 `Cannot find path ... because it does not exist` 报错 —— 先在 Resolve 前 `New-Item -Force` 创建输出目录

  - 修复 WinUI3 GUI 编译错误：禁用右键菜单改用 WebView2 正确初始化事件 `CoreWebView2Initialized`（原误用 WinForms/WPF 的 `CoreWebView2InitializationCompleted`，导致 `CS1061` + XAML 编译器连锁 `WMC9999`；该错误此前被 OutRoot 提前失败掩盖，未在 CI 暴露）

- **本地构建提速（复用缓存，仅本机）：**

  - runtime 缓存 `build/runtime-cache`：embedded Python + pip 依赖复用，跳过重复下载/安装

  - GUI 编译缓存 `build/publish-cache` + `-SkipPublish`：未改 C# 时复用已编译产物，跳过 dotnet publish

  - 两套缓存均以源指纹（源码 / lock hash）自动校验，跨分支或改代码后自动失效重建，杜绝误用旧产物

  - `.gitignore` 忽略本地打包产物（`scripts/release/MaaRacingAssistant-*`）与缓存目录（`build/runtime-cache`、`build/publish-cache`）

***

## 2026-08-19

### v0.15.0-dev.1 净机适配 + GUI 打磨 + 解压即用发布流水线 📦

- **版本号：** `v0.15.0-dev.1`（预发布，0.x 系列 tag 全部为 pre-release）

- **净机适配（新机开箱即用）：**

  - vgamepad 懒加载（`maaracing_assistant/vgamepad_lazy.py`）：无 ViGEmBus 驱动时不再 import 阶段崩溃，controller 暴露 `gamepad_available()` 供 GUI 判断

  - GUI 缺少 ViGEmBus 驱动时弹引导框（含下载入口，sidecar 新增 `open_vigembus_download`）

  - 发布包自带 embedded Python3.11 runtime + 预装依赖，解压即用

  - 依赖收敛：运行时去掉 `ultralytics`（推理改走 onnxruntime-directml），换入 `windows-capture`；训练依赖拆到可选 `[train]` extra，避免把 torch 拽进发布包

- **解压即用发布流水线：** CI 新增 `win-build` job —— dotnet publish GUI + `scripts/release/assemble.ps1` 组装 `MaaRacingAssistant-<ver>-win-x64.zip`(runtime+publish+白名单+自检) + sha256，并补传至同一 Release

- **GUI 重构与打磨：**

  - tab 切换新增左右滑动动画（仅相邻 tab 播放）+ 底部滑块（宽度 85%、水平居中、点击当前 tab 不重播）

  - 主控 tab 左右栏套用数据 tab 的列变换逻辑；运行日志卡高度撑满窗口（四周留白）

  - 数据 tab 实时预览放大重做：16:9 等比 + 四周留白 + 居中 + 随窗自动缩放 + 隐藏背景卡片；放大态只留「还原」按钮

  - 关于 tab：文案对齐现状、logo 换为 `assets/icon.ico`（去除红色底框）、底部「项目主页 / 报告问题 / 使用文档」三按钮（sidecar `open_external_url` 走默认浏览器）

  - GUI 全局禁用右键默认菜单（WebView2 `AreDefaultContextMenusEnabled=false`）

- **仓库与规范：** 移除 `tools/analysis/` 下一次性实验脚本（OCR/回放/性能分析等，3149 行）；新增 `LICENSE`、`THIRD_PARTY_LICENSES.md`；`tools/training/train.py` 对应训练 extra 调整

***

## 2026-08-19

### v0.14.0-dev.3 鉴宝策略 V3 意愿缓冲 + 彩蛋竞态修复 + GUI 今日看板与内嵌预览 🎲

- **版本号：** `v0.14.0-dev.3`（预发布，基于 v0.14.0-dev.2；0.x 系列 tag 全部为 pre-release）

- **鉴宝出价策略 V3（对手加价意愿 + 兜底修复）：**

  - 对手最高价史（`opp_high_history`，逐回合、排除我方槽位）→ 加价意愿系数（涨幅 ≤0 → ×0.3 / <30% → ×0.6 / ≥30% → ×1.0）动态收缩预测缓冲，防止过度抬价

  - 修复全局兜底上限 bug：`max(risk_cap, V̂×0.15)` → `V̂+risk_cap`（原实现把第二层卡钳死在 risk\_cap，对手价一高区间即走空弃权，见 log 20260818\_000240 R3）

  - PASS 弃权改为「嘲讽出价 250」：不再静默死等，走输入确认链路，既送出报价不浪费回合又维持严格兜底

  - 玩家掉线报价 0 也正常落盘（-1 哨兵区分未读），修复 4 槽快照凑不齐导致整场锁死

  - debug 图 HUD 显示与决策同口径的策略估值 V̂（VAL\_COEF×sysmax），图和决策不再脱节

- **彩蛋识别竞态修复：**

  - 彩蛋蛋卡逐帧飞入动画、完整帧仅 1 帧 → 原「首个非空结果即读完」会被不完整帧锁死、漏记黄蛋

  - 改为 `_egg_reading` 门控（进入弹窗持续投递识别，不依赖单帧 title 命中）+ 历史最优累积（更多蛋覆盖更少蛋，绝不降级）+ 连续稳定确认才判定读完；超时兜底落盘用已累积最优值

- **games 表策略模式落盘：** 新增 `strategy_mode` 列（profit=赚钱 / egg=赚蛋，以策略实例实际 mode 为准），旧库自动迁移

- **GUI 重构（用户端体验）：**

  - 主控 tab「断点选择」改名「当前阶段」，去掉双击跳转，▶ 指示器由 selectStage 统一管理随当前阶段移动（修复不跟随 bug）

  - 日志 MAA 风格区块化：锚点分段（进入阶段/会话/场次）+ 折叠聚合 + 级别色点，次要细节折叠、含错误自动展开并加红/黄告警徽章

  - 数据页新增「今日看板」card：读 treasure.db 今日统计（凌晨 5 点日界），场次/胜/负/我方利润/收入/最高单场 + 今日蛋总数（红黄蓝分色），3s 轮询

  - PEEP 实时预览内嵌 16:9：debug 改 headless（不再独立 OpenCV 弹窗），sidecar 新增 `get_peep_frame`（JPEG base64），数据页内嵌轮询 \~10fps；右栏固定、左栏自由变换

  - GUI 进入默认选中鉴宝模块

- **文档与社区规范：**

  - CODE\_WIKI 按功能域拆分为主文档 + CODE\_WIKI\_RACING（赛车域）+ CODE\_WIKI\_TREASURE（鉴宝域）

  - 新增 CODE\_OF\_CONDUCT.md、CONTRIBUTING.md、Issue 模板（bug/feature）、PR 模板

  - README / pyproject 定位更新：巅峰鉴宝为主打模块（全链路自动化闭环），极速狂飙为开发中状态

***

## 2026-08-16

### v0.14.0-dev.2 鉴宝出价策略 V2 + 结算彩蛋识别 + GUI 重构 🎲

- **版本号：** `v0.14.0-dev.2`（预发布，基于 v0.14.0-dev.1；0.x 系列 tag 全部为 pre-release）

- **鉴宝出价策略 V2（核心）：**

  - 双层动态缓冲：基础缓冲（价格分桶）× 利润强度缩放（clamp(强度/15%, 0.5, 1.5)），替代固定 +1000 步长；兜底上限 risk\_cap（GUI 可调，默认 5 万）防意外接盘

  - 决策分流：对手烧钱（对手最高出价 > 预估实价）→ 卡第二吃 15% 分红；冷静 → 贴底捡漏；估值系数校准 1.38 → 1.28（逐场实测 median=1.265）

  - 策略双模式：赚钱（吃分红/捡漏）/ 赚蛋（搏拍中彩蛋，买入上限 = 估值 + 兜底），GUI 小字提示含赚蛋免责声明

  - 余额三态：OCR 未读到（哨兵 -1，视为充足）/ 真实 0（pass）/ 正常；钱不够不卡死、自动钳制单局可承受亏损

  - PASS 死循环防护：决策 pass 时不编辑输入框、不点确认（DECISION\_PASS 分支）；单测重写覆盖观察/捡漏/卡第二/pass/模式切换/余额三态

- **结算与彩蛋识别：**

  - 新增「结算弹窗」阶段：今日最高积分上涨 / 奖励结算彩蛋合并识别（daily\_high\_banner / egg\_reward\_title），等级提升无 ROI 盲点跳过

  - 新增 treasure\_eggs.py 彩蛋结算识别 + egg.png / egg\_reward\_title / daily\_high\_banner 等资源

  - banner\_result()：中标结算阶段判定「中标/未中标」写入落盘；结构化落盘 data/treasure/treasure.db（games + daily\_summary）

  - 调试渲染按 OCR「（我）」槽位高亮我方出价，不再硬编码「玩家3」；场次选择判定改「开始匹配」按钮模板（支持实习/专家/大师场 badge 动态定位）

- **GUI 重构：**

  - 顶部导航「控制面板/调试/关于」→「主控/数据/设置/关于」；调试页拆分为「数据」（性能监控/当前检测/实时预览）与「设置」（调试选项/截图方式/快捷工具）

  - 数据/设置页卡片按模块渲染：MODULE\_PAGE\_DEFS 注册表，treasure/racing 各一套独立 id 前缀 DOM，切换模块自动刷新，便于后续模块差异化

  - 鉴宝配置项：策略模式下拉（赚钱/赚蛋）+ 兜底上限输入框 + 策略小字提示动态切换

  - 紧急停止未运行不报错（避免满屏 ERROR）；运行中仅锁定可选项

- **文档与仓库净化：** CODE\_WIKI 同步场次选择/结算弹窗机制并吸收策略分析结论（多轮临时分析报告不入库、结论沉淀进 CODE\_WIKI）；`.claude/`、`docs/trae-dsh-config-share.md` 忽略、`.trae/` 已跟踪文件移出索引（harness 个性化配置与记忆不入库）

***

## 2026-08-15

### v0.14.0-dev.1 模块化架构改造（能力接口 + 资源所有权）🏗️

- **版本号：** `v0.14.0-dev.1`（预发布，基于 v0.13.0；0.x 系列 tag 全部为 pre-release）

- **模块化架构基线（借鉴 DSH/Cordis 思想，克制落地）：**

  - 引入 typed capability（capture / gamepad / lifecycle / debug\_renderer），模块经 `ActivityContext` 窄接口接触宿主，斩断对 controller 私有接口的反向引用

  - `GamepadCapability` 租约语义：`acquire()` 归零归还 + `reset_device()` 断开重建，业务层不再手动 destroy（每个手柄实例任一时刻唯一 owner）

  - `ActivityContext` 引入 `ExitStack` 接管 renderer 生命周期，删除 token 机制，模块退出（含异常）自动释放不泄漏

  - `REQUIRES` 能力声明 + 启动前 fail-fast 校验（`ModuleDependencyError`）

  - `ctx.bind_tasker` 收口 MAA 集成，删除公开 `controller` 暴露（`ModuleIntegrationError`）

- **README 重写：** 定位改为「模块化游戏自动化平台」（目标扩展到全部重复劳作活动），精简结构 + 完善合规声明（严禁代练 / 外挂 / 影响服务器排名）

- **其他说明：** 本次为内部架构重构，无新增用户玩法功能，未充分验证，发 dev 版

***

## 2026-08-15

### v0.13.0 巅峰鉴宝每日循环上限 + 出价策略配置（正式版）🏷️

- **版本号：** `v0.13.0`（正式版，基于 v0.13.0-dev.5；0.x 系列首个正式版 tag）

- **每日循环上限「刷到第几场」：**

  - 控制面板活动卡片新增「刷到第几场」输入框（0-50，0=不指定），每日以凌晨 5 点为日界

  - 识别场次选择页「日已参与 X/50 场」计数：鉴宝大厅阶段同步单 ROI 识别 + 状态机 done\_count 双保险，单调更新 + 交叉追平

  - 达到上限后拦截「开始匹配」，自动停止本日循环

- **出价策略 GUI 下拉：** 单一策略「最大利润（刷单日计分）」，`BidStrategy` 回退单一逻辑，后续策略按需求扩展

- **运行中配置锁定：** 模块运行期间 GUI 可选项调灰禁改，sidecar 仅写缓存不热更新

- **鉴宝师偏好 JSON 化：** appraiser\_p1/p2（偏好卡洛琳/章太郎）配置迁入 treasure\_rois.json（prio/rect/templates/threshold），调试台新增「偏好鉴宝师」分类（`_` 前缀元数据不参与匹配校验）

- **多循环健壮性修复：**

  - 新场次残留数据污染 → 回合状态重置 `_reset_round_state`（H 价/玩家出价/竞拍状态机/策略基线）

  - 页面切换 / 面板打开不重试 → 阶段点击超时重试（CLICK\_RETRY）

  - 选择鉴宝师转场误兜底 → 5 帧转场缓冲（APPRAISER\_SETTLE\_FRAMES）

  - 鉴宝大厅每日计数 OCR 不投递（异步被阶段门控丢弃）→ 同步单 ROI 识别修复

- **其他：** 场次计数 ROI 框经调试台校准，OCR 输出正常（如「24/50场」→ 24）

***

## 2026-08-14

### v0.13.0-dev.5 鉴宝全链路自动化准星 + 软件健壮性增强 🎯

- **版本号：** `v0.13.0-dev.5`（预发布，基于 v0.13.0-dev.4；0.x 系列 tag 全部为 pre-release）

- **鉴宝师选择自动化：** 进入「选择鉴宝师」阶段延迟 3 帧识别，全屏多尺度匹配（0.70\~1.30×13 档）按顺位抉择 P1 卡洛琳 → P2 章太郎，均未识别到 → 准星指屏幕中心

  - 「已选中」对勾判定：`stage.appraiser_selected_check` 横向长条 rect 扫描黄色 √，对勾中心 X ≈ 卡片右边界即判定已选中 → 准星指确认按钮

- **全链路准星意图（不真实点击）：** 游戏大厅 → 活动页 → 鉴宝大厅(选择场次) → 选择鉴宝师 → 回合出价 → 领取分红 各阶段经 `_decide_action → _resolve_action_target` 渲染 PEEP 准星，显示程序「想点击的位置」

- **场次选择自动化：** 详情卡标题（session\_master\_panel\_title）模板判定 → 命中点「开始匹配」/ 未命中点「鉴宝大师场」标签；`session_master_badge`、`session_start_match_btn` 迁入 actions 段（静态 rect 中心）

- **调试台（treasure\_debug\_studio）增强：**

  - 修复黑屏：截图正则放宽支持 jpg/jpeg/webp

  - 匹配命中显示：黄色高亮框 + 中心十字 + 分数（showHit 开关）

  - 框显示开关：all / selected / none；隐藏的框不再响应点击/拖动（hitTest 过滤）

- **软件健壮性：**

  - 单实例互斥：多开弹窗询问「启动新进程（关闭旧进程）/ 取消保留旧进程」

  - 窗口以最小安全尺寸启动（1000×700 DIP）

  - 标题栏交互区挖孔：双击 tab/品牌区不再触发最大化（drag region 动态排除）

- **版本号双轨机制：** 打包产物读 setuptools-scm 构建快照（旧版本不会被新 tag 带歪）；源码运行按当前 checkout 动态 git describe 推导；sidecar 改用包级 `__version__`

- **回合小字 OCR 化：** 删除 `round_label_*.png` 模板，`round_label_area` 迁入 ocr 段

- **编译级清理：** pyright 核心包 48→0、调试台/诊断工具清零（best 元组标注、Optional 断言、类型收窄等）

***

## 2026-08-13

### v0.13.0-dev.4 项目文件结构与命名规范化 🧹

- **版本号：** `v0.13.0-dev.4`（预发布，基于 v0.13.0-dev.3；0.x 系列 tag 全部为 pre-release）

- **文件结构与命名规范化：** 按用途对工具脚本分类，采用 ASCII snake\_case 命名，消除历史命名不规范

  - `tools/` 按 `training/`、`analysis/`、`debug/` 分组，清理混杂脚本（如 `analyze_record.py` → `analyze_records_input.py` 等）

  - `AGENTS.md` 统一大写（git mv 经临时名规避大小写不敏感），保证 trae 自动接入上下文

  - `mra_shell` 由 `prototypes/` 迁移至 `apps/`，同步更新 `.sln`、`start.bat`、`MainWindow.xaml.cs` 前端路径

  - 修复 `apps/mra_shell/NuGet.Config` 失效的本地源引用

- **文档同步：** `README.md`、`docs/CODE_WIKI.md` 对齐项目结构；新增 `docs/structure_plan.md` 记录规范化方案

- **project-update skill 优化：** 0.x 阶段版本号规则优化与 release 版本校验修复

***

## 2026-08-13

### v0.13.0-dev.3 巅峰鉴宝全链路阶段检测 + RapidOCR 金额识别 🏷️

- **版本号：** `v0.13.0-dev.3`（预发布，基于 v0.13.0-dev.2；0.x 系列 tag 全部为 pre-release）

- **鉴宝全链路阶段检测（新增游戏大厅→活动页面断点）：**

  - 阶段链路完整化：游戏大厅(participation\_card) → 活动页面(goto\_appraise\_btn) → 鉴宝大厅(hall\_session\_cards) → 选择鉴宝师 → 回合出价 → 中标结算 → 领取分红

  - `treasure_detector._ROI_STAGE` 新增 3 个前置阶段映射，`STAGE_ORDER` 同步扩充

  - 删除废弃的 `hall_car_show_card` 模板与引用

- **赢局结算横幅阈值放宽：** `result_auction_win_banner` 单独阈值降至 0.60（横幅带彩条特效，匹配分偏低），避免漏检

- **调试台（treasure\_debug\_studio）分类动态化：**

  - 分类 tab 由硬编码数组改为从 JSON 动态生成（CAT\_KEYS 白名单：stage/round\_labels/actions/ocr）

  - OCR 分类区域隐藏模板控件，仅保留矩形编辑

- **OCR 识别区扩充（treasure\_rois.json）：**

  - 新增 `bid_player1~4`（本轮各玩家出价）、`player_name1~4`（玩家名，含「（我）」标记→名次）

  - `bid_result_amount_box`（弹窗中心金额）→ 注入 `set_h` 系统报价

- **OCR 金额提取加固（treasure\_ocr.py）：** 千分位逗号格式优先、重复逗号合并、MIN\_AMOUNT=10000 过滤、7 位噪点前缀处理

- **系统报价→估值链路：** 前 3 回合系统报价最大值 `sysmax_13` ×1.35/1.4 = 真实估值区间，HUD 新增「系统报价 / 估值区间」行

- **OCR 重构（RapidOCR）：** `tools/_analyze_treasure_game.py::DigitOCR` 由模板匹配整体 fallback 为 RapidOCR 薄封装，`read_number` 接口不变；删除 38 个模板 OCR 临时脚本与 6 个模板目录；requirements 新增 `rapidocr_onnxruntime>=1.4.4`

- **启动入口统一：** 删除 `run.py`，改 `python -m maaracing_assistant`；新增 `start.bat` 相对定位 `mra_shell.exe`；README / CODE\_WIKI 同步

***

## 2026-08-12

### v0.13.0-dev.2 GUI 迁移：WinUI 3 shell + JSONL sidecar + HTML 三 Tab 🖥️

- **版本号：** `v0.13.0-dev.2`（预发布，基于 v0.13.0-dev.1）

- **GUI 宿主定案 WinUI 3：** Tauri v2 / WPF WindowChrome 实测判负（WebView2 airspace 遮挡、Windows 无 Overlay 实现），WinUI 3 `AppWindowTitleBar` 全能力实测通过

- **进程模型：** `mra_shell.exe`（唯一 GUI，只做窗口 + sidecar 生命周期 + 消息转发）+ `sidecar.py`（JSONL RPC 业务后端，stdin=request / stdout=response / stderr=日志）

- **契约测试 11/11：** PythonSidecar 进程生命周期（并发按 id 匹配 / 超时 / crash 全 disconnected / grace shutdown / 不孤儿）

- **HTML 三 Tab 前端：** 控制面板（模块/断点/日志）、调试（PEEP/性能监控/截图方式）、关于，纯 CSS 无 CDN（WebView2 离线可用）

- **窗口细节：** 自定义标题栏 52px（描边不被系统按钮遮挡）、最小尺寸 1000×700（WM\_GETMINMAXINFO）、系统按钮失焦配色、icon.ico 应用图标

- **乱码修复：** UAC 提权后 `PYTHONUTF8` 环境变量不继承 → shell 侧强制写入，Python stdout 恒为 UTF-8

- **关闭卡死修复：** `OnClosed` 同步 await UI SynchronizationContext 死锁 → `Task.Run` 隔离

- **清理：** 旧 ttkbootstrap GUI（`gui/`、`gui_webview/`）归档至 `archive/`；spike 目录全删只留 mra\_shell；requirements/pyproject 移除 ttkbootstrap；入口改走 sidecar

***

## 2026-08-11

### v0.13.0 活动模块化架构 + 巅峰鉴宝模块 ⚙️

- **版本号：** `v0.13.0`（次版本+1，基于 v0.12.0）

- **模块化框架：** 引入 `ActivityModule` 抽象基类和 `ActivityContext` 共享资源封装，定义统一模块接口（start/stop/cleanup/current\_stage）

- **Module Registry：** 集中管理模块元数据与实例创建，支持 GUI 动态切换活动模块

- **RacingModule 提取：** 将"极速狂飙"完整流程从原 controller.py 提取为独立模块，模块内持有自有 MAA Resource/Tasker

- **TreasureModule 桩：** 新增"巅峰鉴宝"活动模块基础框架，支持后续扩展

- **DebugManager 重构：** 引入 `DebugRenderer` 协议，支持模块注入自定义渲染器，renderer 生命周期绑定 token 避免竞态

- **GUI 升级：** 模块选择下拉框，动态断点列表跟随模块切换

- **记录模式删除：** 彻底移除 Record Mode 相关代码（已无用）

- **WGC 截图裁剪：** 底部锚定 16:9 裁剪，避免状态栏干扰

***

## 2026-08-10

### v0.11.1 AIM 死区修正 + 延迟基准抗离群 + 快速截图兜底 🔧

- **版本号：** `v0.11.1`（SemVer 修订号+1，纯 bugfix + 性能优化，基于 v0.11.0）

- **AIM off\_center 死区顺序修复：** `_aim_at` 先判死区再判保底的顺序问题，off\_center=True（目标偏离中心车道）时 `effective_stop` 从膨胀的 `0.01 + area_ratio × 30` 收缩为 `0.01`；远/中/近区保底力度 15%/25%/40%（远区也加保底），解决帧 239-253"偏航明显仍直行"

- **快速截图（`_cap_fast`）三重兜底：** 句柄获取按 `ctrl.hWnd → ctrl.hwnd → find_game_hwnd()` 三级降级；每步 GDI 调用（GetClientRect/GetDC/CreateCompatibleDC/CreateCompatibleBitmap/GetBitmapBits）加返回值检查；失败日志从 DEBUG 升级为 WARNING 并指明失效环节（之前静默失败看不到）

- **延迟基准抗离群调优：** `_benchmark_latency` 自动调优从"原始 P95"改为"剔除 YOLO 帧中最慢 1 帧后取 P90"，加 1.8× 离群比告警（`P95/P90>1.8` 输出 ⚠），避免一次 Windows 线程调度抖动（如 YOLO 推理 115ms/P95）把帧率从 30FPS 卡死到 15FPS 下限

- **基准测试分帧统计：** 奇偶分离 YOLO 帧 / 非 YOLO 帧，分别报告 P50/P90/P95（10帧样本），分离截图/YOLO/标线/决策单项耗时，快速定位瓶颈

- **主循环全动态化：** `YOLO_INTERVAL = round(fps / 10)`（≈10 Hz YOLO），`SLOW_CHECK = fps`（≈1 Hz 结束检测），`sleep = 1.0/fps - elapsed` 精准节奏；替换原硬编码 `sleep(1/15)` + 固定 `YOLO_INTERVAL=2`

***

## 2026-07-24

### v0.11.0 贪婪决策 + 前馈瞄准 + 记录模式 🎯

- **版本号：** `v0.11.0`

- **贪婪决策优先级：** 金币+奖励车优先（面积优先，面积近时选离中线近的）→ C区防撞 → 障碍车避让 → 无目标，撞车无惩罚所以防撞降级

- **前馈瞄准（`_aim_at`）：** 根据目标大小/深度预测提前停止，动态 stop\_zone = 0.01 + min(0.10, area\_ratio × 30)，减少转向过度

- **记录模式（Record Mode）：** GUI 勾选后读取物理 XInput 手柄输入，CSV 记录帧号/时间/摇杆/目标/决策数据，用于分析人工操作规律

- **车道保持优化：** `_calc_drift` 工具函数复用漂移计算（d/dd/cum3），变化率检测 `abs(d) < 5px` 提前停止修正

- **删除变道后激活车道保持：** 移除 `force_init` 和 `_prev_reason` 逻辑，只在无目标时激活车道保持

- **防碰撞优先级调整：** C区防撞从第1位降到第2位，金币组从第2位升到第1位

- **Debug 前馈信息：** 右上角显示 offset/stop\_zone/dx/移动方向/in\_center/停止原因

## 2026-07-23

### v0.10.0 转向平滑校准 + 防碰撞优化 + 阴影标线检测 🎯

- **版本号：** `v0.10.0`

- **转向平滑系统：** 指数平滑 `smoothed = smoothed × alpha + target × (1-alpha)`，消除镜头惯性导致的摆动

- **alpha 校准状态机：** baseline→steer→settle 三阶段嵌入主循环，dd 加速度检测转向响应，自动计算 alpha = 0.5^(1/settle)

- **校准四区域策略：** 检测 L/R 标线 + 中线估测 → 决定先往中线打还是先往标线打，保证全程可见标线且不撞墙

- **校准数据验证：** settle 后检查标线位移 ≥15px，不够则重试（最多 2 次，每次转向帧数 +4），全部失败回退 alpha=0.6

- **C 区防碰撞 cum3 位移过滤：** 3 帧累计位移 >10px 才触发 C 区，防止车道 1 正常行驶误触（pos\~500 触发旧阈值）

- **HSV 阴影标线检测：** S/V 下限从 150 降至 80，可识别 #7f7200 等阴影下的黄色标线

- **道路中线估测（`_estimate_road_center`）：** 从单侧标线推断中线位置，-50/+50 修正偏向中心

- **Debug 实时值追踪：** `_apply_trigger` / `_steer` 封装手柄操作并自动记录 `_last_rt` / `_last_stick`，debug 帧显示真实油门和摇杆值（不再硬编码）

- **Debug 校准可视化：** 校准帧 `save_to_disk=True`，label 带 frame\_id，可查看完整校准过程

## 2026-07-23

### v0.9.0 赛车决策系统重构 + NMS 跨类抑制修复 + 车道保持 🔄

- **版本号：** `v0.9.0`

- **NMS 按类分别处理（`_nms_per_class`）：** 避免 YOLO 跨类 NMS 压掉 bonus\_car（car 0.89 压 bonus\_car 0.86），索引映射链 `mask_indices[cls_local[nms_idx]]`

- **三区变力度瞄准（`_aim_at`）：** 远区 50% / 中区 100% / 近区 0%，水平死区 ±0.06，替换旧的简单左/中/右三档

- **避障框重叠检测：** 车框左沿\<R2c 且右沿>L2c 才触发躲避，不用中心点；`_avoid` 返回 0 时穿透到金币逻辑

- **闭环车道保持（`_lane_keep`）：** 漂移趋势检测（3 帧跨度 diff）+ 自适应力度调节（50%\~100%），force\_init 切回直行时立即回正

- **车道保持方向修复：** 右标线侧方向符号取反修复（`new_dir = 1 if diff > 0 else -1` 统一左右侧）

- **动态地平线推断（`_detect_horizon`）：** 从 YOLO 低置信度小车群（area<400, conf≤0.25）推测地平线，首次 ≥3 车锁死整局

- **透视车道分界线（`_lane_boundaries_at_y`）：** 梯形透视投影 `bound()` 线性外推，6 条线（LE/L12/L2c/R2c/R12/RE）

- **动态油门（`_calc_throttle`）：** 防撞 120 / 避障 180 / 金币&跳板车 200 / 直行 255

- **标线单边选择：** `_detect_lane` `side_score` 择优选一侧，返回 `{side, pos}` 替代旧 `{left, right, center}`

- **防碰撞重写：** 单边标线 `_wall_pos_history` 替代旧左右双历史，切换侧自动清空

- **标线丢失 C 区延续：** 无标线但有 `_wall_memory` 时直接进 C 区强制修正，不再等待记忆回带

- **Debug 可视化全面升级：** 区域分割线（地平线/远中近）、决策详情、动态油门值、透视车道线；虚线框去重 `_dedup_overlapping` + 实线框重叠隐藏

- **帧日志重写：** 统一 `[DECIDE]` 格式（帧号/决策/详情/标线/车况/金币/方向/油门），每 2 帧输出一次

***

## 2026-07-22

### v0.7.1 HoughLinesP 标线检测 + 三区防碰撞 + 反打修正 🛞

- **版本号：** `__version__ = "0.7.1"`

- **标线检测改为 HoughLinesP：** 从像素扫描改为 Hough 直线检测，y>50% 区域找最黄最直的线，断裂自动延长对齐，HSV H:20-30 S:150-255 V:150-255 严格滤波

- **三区防碰撞替代车道归中：** 移除 `_keep_center`，新增 `_wall_avoidance` 三区系统（A 区安全无干预 / B 区二阶导识别加速贴墙趋势 / C 区硬边界强制修正）

- **反打修正（突发+归中）：** C 区不再持续满打方向，改为"突发修正 2 帧（改变车头指向）→ 强制归中 5 帧（滑行远离墙）→ 重评估"的类人驾驶策略

- **不推断缺失侧标线：** 移除单侧推断代码，`_detect_lane` 只返回真实检测到的标线，防碰撞只信任真实侧

- **标线丢失记忆回带：** 新增 `_wall_memory` 机制，标线丢失但有历史记忆时（无 YOLO 目标）轻柔回带

- **`_aim_at`/`_avoid`** **移除边界约束：** 去掉了标线边界约束，防碰撞由独立模块负责，变道吃金币不再受阻

- **Debug 摇杆状态条：** 底部方向文字 `<< LEFT` / `RIGHT >>` 替换为摇杆滑条指示器 + 数值显示

- **debug.py KeyError 修复：** `lane['right']` / `lane['left']` 改为 `.get()` 安全访问

- **CLAUDE.md 更新：** 新增防碰撞参数表，更新决策优先级和坑点

***

## 2026-07-21

### v0.7.0 黄色标线车道检测 + 全局路径规划 + PEEP/存盘双模式可视化 🎉

- **版本号：** `__version__ = "0.7.0"`

- **黄色标线车道检测：** `_detect_lane` HSV 黄色标线检测，提供道路边界和中心参考线

- **全局路径规划重写** **`_decide`：** 边缘修正 > bonus\_car 对准 > 车道约束避让 > 金币链式评分 > 归中，替代原简单优先级逻辑

- **车道中心替代画面中心：** `_keep_center` / `_avoid` / `_aim_at` 全部以车道中心为参考

- **YOLO ROI 区域裁剪：** `yolo_detector.py` 新增 `roi` 参数，y28%\~78% 区域裁剪推理，减少天空/仪表盘干扰

- **导航百分比阈值：** `navigation.py` 硬编码像素阈值改为 `min_dim` 百分比（FAR/MID/NEAR/BASE/ALIGN\_PX），适配不同分辨率

- **PEEP/存盘双模式渲染：** `debug.py` 拆分 `_render_full`（全量存盘）和 `_render_peep`（精简预览）两套独立渲染，PEEP 仅显示 YOLO 框/标线/方向指示器

- **双手柄冲突修复：** `controller.py` racing 开始前销毁导航手柄，解决双手柄冲突

- **YOLO11n 模型训练：** 从 yolov8n 升级到 yolo11n，753 张标注图片训练，mAP50=0.771

- **auto\_label.py 预标脚本：** 用训练模型自动预标未标注图片，低阈值宁可多标不漏标

- **train.py 路径修复：** 导出路径从相对路径改为绝对路径，避免 `best.pt` 找不到

***

## 2026-07-20

### v0.6.0 DirectML GPU 推理 + 性能优化 + 流程重构 🚀

- **版本号保持 v0.6.0**（未升级版本号）

- **onnxruntime-directml 替代 CPU-only onnxruntime**：YOLO 推理从 \~33ms 降到 \~3.7ms（9×加速），解决 GPU 4060 未被使用的问题。无需安装 CUDA Toolkit，DirectX 12 即可

- **ONNX Session 缓存**：图优化（`ORT_ENABLE_ALL`）+ DirectML 内核缓存 + `model_optimized.onnx` 持久化到 `__pycache__/ort_cache/`

- **跳帧推理**：YOLO 每 3 帧推理一次，中间帧复用缓存结果，GPU 负载降到 1/3

- **`save_frame`** **磁盘控制**：新增 `save_to_disk` 参数，PEEP 预览每帧更新（标注渲染仅 \~1-2ms），磁盘 `cv2.imwrite` 每 15 帧一次

- **`_is_end`** **统一模板匹配**：去掉不可靠的白色区域检测，改用 `store_popup_template.jpg` + `round1_end_template.jpg` 模板匹配（阈值 0.55），`_is_shop` 逻辑合并进 `_is_end`

- **新增模板** **`round1_end_template.jpg`**：用户截取的回合1结束画面

- **`_in_match`** **对局标记**：导航二成功后标记已进入对局，此后所有失败不回退大厅，直接停止流程

- **RacingLoop 异常重试**：运行 < 3 秒判定异常，最多重试 3 次，全部异常停止

- **关闭 handle\_store\_popup 后的光标复位**：直接进入确认上阵导航

- **`requirements.txt`** **/** **`pyproject.toml`**：`onnxruntime` → `onnxruntime-directml`

- **删除** **`profile_racing.py`**：临时性能剖析脚本已清理

***

## 2026-07-19

### v0.6.0 包结构重构 🏗️

- **版本号：** `__version__ = "0.6.0"`

- **创建包目录：** 将根目录全部源码移入 `maaracing_assistant/` 包目录

- **main.py 拆分：** 880 行上帝文件拆分为 6 个单一职责模块（`logger.py` / `window_utils.py` / `yolo_detector.py` / `pipeline_logger.py` / `racing_loop.py` / `controller.py`）

- **根目录精简：** 7 个 .py 文件减为 1 个（`run.py` 快捷入口）

- **pyproject.toml：** 添加 setuptools 项目配置，支持 `pip install -e .`

- **新增** **`__main__.py`：** 支持 `python -m maaracing_assistant`

- **导入链验证：** 全部 9 个模块通过导入检查，零循环导入

- **环境清理：** 删除 milo 环境，maazs 重命名为 maaracing\_assistant

***

## 2026-07-17

### v0.5.0 导航三+PEEP实时预览+YOLO可视化 🎉

- **版本号：** `__version__ = "0.5.0"`

- **导航三（寻找对手按钮）：** `find_opponent_template.jpg` (374×195) 模板匹配，等待页面加载（超时15s）→ 光标导航到按钮 → 模板消失验证。重试×3，失败回外层循环从头开始

- **Pipeline 重构：** 移除 OCR 预任务（极速狂飙入口/回合1准备），Python 主循环驱动全部导航，Pipeline 只做 RacingLoop + 结束/放弃

- **PEEP 实时预览模式：** GUI 独立开关 "PEEP 实时预览"，OpenCV 独立线程 (\~30fps) 实时显示调试帧，不依赖 DEBUG 存盘

- **YOLO 检测可视化：** `YOLODetector.__call__()` 新增第4返回值 `debug_dets`（框坐标+置信度+类名），PEEP 窗口每帧显示金色/红色/紫色检测框

- **模板匹配可视化：** `_check_page_by_template()` 每帧传 template\_rects（青色矩形+置信度）到 PEEP 窗口

- **归位可视化：** `homing()` 直接调用 `_find_template`，每帧显示模板匹配位置

- **扩充 scales 范围：** `_check_page_by_template` 的模板匹配 scales 从 \[0.8~~1.2] 扩展到 \[0.5~~1.8]，阈值降到 0.55

- **`_wait_for_template()`** **新增：** 通用轮询等待模板出现方法，可配超时和间隔

- **PEEP 不依赖 DEBUG：** 即使不勾选"每帧截图"，PEEP 也能独立工作

### v0.4.0 光标识别重构+假光标拉黑+debug可视化 🎉

- **版本号：** `__version__ = "0.4.0"`

- **双中心面积评分：** `_find_cursor_by_shape` 改用双中心评分（常态 310 / 变形 420），同时覆盖两种光标形态，不再依赖单一面积中心

- **面积硬过滤：** `area < 240` 直接排除假光标（\~206-221），不再进入候选池

- **运动 Y 轴校正：** vgamepad ly 正=上 vs 屏幕 Y 正=下，点积改用 `sy = -ly/stick_len` 修正

- **假光标静止拉黑：** 跨帧位置对比（`_prev_frame_positions: set[tuple]`），推摇杆时不动的候选累计静止帧，`cnt ≥ 3` 直接 `continue` 拉黑，切页面清空

- **`_last_stick`** **保留：** `_press_and_verify` 失败后不再清空 `_last_stick`，保留推杆方向供下帧静止惩罚/运动评分用（修复原 bug：清空后运动评分块整个跳过，假光标不扣分）

- **close\_threshold 12px：** 第二个按钮阈值 25→12，收缩公式 `max(30, -15)` → `max(5, ×0.65)`

- **自适应 stop\_distance：** `max(8, close_th × 0.55)` 替代硬编码 25px，确保收缩后光标能推到足够近

- **微调移动档位：** < 35px 增加 25ms 脉冲微调档（原 120ms 65% 在死区 4260 下一推就飞）+ 刹车自适应（<35px 时 80ms 刹车替代 50ms）

- **debug.py 创建：** `NavigationDebugger` 四色标注（红=选中光/绿=入围/黑=拉黑/蓝=按钮），每帧保存到 `debug/navigate/`

- **GUI debug 开关：** 主界面 Checkbutton 控制每帧截图，同步到 controller.debug.enabled

- **假光标减速/刹车/评分参数依据 1080p 重新校准**（原基于 1440p）

***

## 2026-07-14

### v0.3.0 导航重构+物理手柄检测+第二个按钮通过 🎉

- **版本号：** `__version__ = "0.3.0"`

- **导航重构：** `ButtonDef` 配置类统一管理按钮（`name`/`pct`/`page_template`/`template_should_match`/`close_threshold`），新增按钮只需一行定义

- **模板匹配正反逻辑：** `template_should_match=True` 匹配到模板=成功，`False` 模板消失=成功，同时支持"进入页面"和"离开页面"两种场景

- **代码瘦身：** 提取 `_press_and_verify`/`_stop_stick`/`_ensure_cursor`/`_blind_move` 等方法，`navigate_to_button` 从 \~220 行精简到 \~80 行

- **物理手柄检测：** `has_physical_controller()` 通过 XInput API 遍历 4 端口，GUI 检测到手柄时弹自定义对话框阻止运行（带 icon.ico）

- **弹窗图标修复：** `messagebox.showerror` → 自定义 `tk.Toplevel + iconbitmap`，正确继承应用图标

- **第二个按钮测试通过：** "开始挑战" 25px 阈值成功命中，模板消失验证通过

- **新增模板：** `activity_page_template.jpg` (1100×550) 活动页面模板

- **清理：** 删除 `diagnose_coords.py` 调试文件

- **文档更新：** HANDOVER.md 全面反映重构后架构，CLAUDE.md 更新状态

### 光标导航首次打通 🎉

- **问题：** 彩色模板匹配归位正常（0.706），但光标导航卡在最后 \~50px 到不了按钮

- **根因：** 摇杆幅度低于游戏死区（4192 < 4260 阈值）+ 面积评分中心 1200 误识别为 470 面积的假光标

- **修复：**

  1. **光标面积评分中心 1200→260**，470 面积的假光标被扣到零分，不再误识别（`_find_cursor_by_shape`）
  2. **摇杆最低速度 0.5→0.6**，保证幅度 4800 > 4260 游戏死区，光标能推到最后（`_move_cursor_to_target`）
  3. **光标丢失 ≥2 秒 → 放弃导航**，利用 `finally` 销毁手柄触发游戏自动复位光标（`navigate_to_button`）

- **版本号：** 添加 `__version__ = "0.2.0"`

### 更新 HANDOVER.md 标明未完成状态

- 标记光标导航为 ❌ 未完成

- 新增"未完成任务"章节，详细说明光标追踪导航的问题

- 更新模板表格，标注各模板状态

- 更新参数表，加入状态列

- 添加 MAA 截图坐标映射未验证的已知坑点

### 导航盲推尝试

- 按钮位置改为百分比硬编码 (89.8%, 75.1%)，不再用模板匹配

- 光标匹配阈值 0.70→0.60，启用灰度匹配

- 摇杆幅值 32767→8000 防过冲

- 归中推摇杆值 20000→6000

- **结果：光标模板假阳性，导航仍未通过**

***

## 2026-07-13

### 启动归位 + 光标追踪导航（大重构）

- **问题：** stop 后多跑一轮、B 键无反应、阈值太高、模板误匹配

- **修复：** `_press_button(duration=0.3)`、`_interruptible_sleep()`、阈值 0.55

- **新增：** `_move_cursor_to_target()`、`navigate_to_button()`、光标归中

- **新增：** `_load_template()`、`_find_template()`（多尺度 + ROI + 灰度匹配）

- **新增：** `_screencap_ctypes()` 备用截图

- 规范化图片命名：`settings_page_template.jpg`、`cursor_template.jpg`、`button_main_template.jpg`

### 日志分级 + 文件名变更

- 新增日志级别：DEBUG / INFO / WARNING / ERROR

- GUI 仅显示 INFO+

- 文件名 `maazs_*` → `MRA_*`

- `Logger.get_lines(min_level)` 实现级别过滤

***

## 2026-07-12

### Pipeline 日志 + RT 加速 + YOLO 决策日志

- **PipelineLogger：** `ContextEventSink` 监听每步识别/动作成功状态

- **RT 加速：** `RacingLoop.run()` 起步 `right_trigger(255)`

- **YOLO 决策日志：** `_decide()` 打印每种决策的中文日志

### 虚拟手柄生命周期管理

- `__init__` 不再创建手柄，改为 `_create_pad()` / `_destroy_pad()` 对

- 每次 `run()` 新创建 + 3 次归零握手清理驱动偏置

- `_steer()` 增加右摇杆归中 + 空指针保护

### GUI 窗口可拖拽

- `resizable(True, True)` + `minsize(480, 400)`

### Pipeline 优雅中断

- `MaaRacingAssistantController.stop()` 增加 `tasker.post_stop()`

### 项目重命名

- `MaaRM-Alpha` → `MaaRacingAssistant`

***

## 2026-07-11 及之前（初始构建）

### 项目初始化

- MAA Framework 5.11.1 集成

- YOLOv8 + ONNX Runtime 视觉识别

- vgamepad 虚拟手柄控制

- ttkbootstrap GUI

- 数据集 188 张标注（3 类：coin / car / bonus\_car）

- YOLO 训练 mAP50≈0.92

- Pipeline 6 步闭环：`入口→回合1准备→比赛→结束→回合2放弃→确认→循环`

