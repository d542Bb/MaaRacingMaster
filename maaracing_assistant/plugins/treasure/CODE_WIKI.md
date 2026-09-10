# MaaRacingAssistant — Code Wiki · 鉴宝域

> 《巅峰极速》"巅峰鉴宝"活动 —— **出价 / 估值 / OCR 全自动模块（treasure\_\*）** 专属文档。
> 聚焦鉴宝核心：12 阶段状态机 / 准星意图 / 出价策略（bid\_strategy）/ 异步 OCR / ROI 三段分类。
>
> 配套文档：
>
> - 主文档：[docs/CODE\_WIKI.md](../../docs/CODE_WIKI.md)（架构 / 导航引擎 / 配置 / 调试 / GUI）

***

## 目录

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

## 1. treasure\_module 巅峰鉴宝模块

[treasure\_module.py](file:///d:/maaracing_assistant/maaracing_assistant/plugins/treasure/module.py)（v0.13.0 主战场）

**职责**：

- 活动模块实现（`ActivityModule` 子类，`ID="treasure"`），12 阶段状态机

- **准星意图模式**：当前只算「程序想点击的位置」，不执行真实点击

- 鉴宝师选择自动化 / 场次选择自动化（模板匹配 + 静态按钮中心）

- 异步 OCR worker（latest-only 丢帧 + 关键 ROI 优先通道）

- 估值算法：全 5 回合系统报价最大值 `sysmax_13`（H=智能出价填入的输入框值，只取每回合第一次）×1.35(求稳)/1.4(激进) = 真实估值区间

- **落盘子域**：结构化落盘已拆出到同目录 [store.py](file:///d:/maaracing_assistant/maaracing_assistant/plugins/treasure/store.py)（`TreasureStore`：SQLite 场次明细 + 当日汇总 + 会话总结），模块主循环只做编排与委托

- **资源随插件**：鉴宝模板位于同目录 `resources/image/`；识别与 ROI 的唯一真源为 `resources/config/treasure_assets.json`（v3），detector/module/ocr/eggs 一律读它，v2 的 `treasure_rois.json` 已于 M4 退役。插件以 `__init__.py` 的 `IMAGE_DIR`/`CONFIG_DIR`/`v3_assets()` 统一引用，不依赖主程序 `assets/`。

- **NavKit 底座**：`core/navkit` 负责 v3 资产模型、E/W 校验、DetectionPlan、路由编译、trace；`tools/navkit` 是结构树/编辑/回放控制台。固定坐标点击件不强制配模板，必须由 v3 `guarded_by` 担保（D2）。

**阶段链路（`treasure_assets.json`** **的** **`stages.order`，仍与** **`STAGE_ORDER`** **保持 GUI 断点兼容）**：

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

**回合出价**（`第N回合出价` 阶段，`_run_bidding_choice`）：

- 状态机：S0 转场期（`round_elapsed < SWITCH_CONFIRM_FRAMES`）→ 不出准星；S1 等待出价 → 不出准星；S2 出价亮起 → 准星指 `bid_main_red_btn`；S3 面板已开 → H 未读点 `smart_bid_btn`（智能出价）、H 已读进 `_run_bidding_execute`（策略决策 → 输入子状态机 → 确认出价）；提交后 S4 wait\_result（等公开报价，OCR 读 4 槽构建快照）

- 面板已开判定：`stage.smart_bid_btn` 模板匹配（`bid_smart_btn.png`，面板内「智能出价」按钮，只有面板打开才出现 = 强信号）

- 主按钮状态（等待出价/出价）走 **OCR 文字**（`ocr.bid_main_btn_label`）——按钮明暗态模板匹配不稳（见 Experience 1112416），用 OCR 文字「等待出价」→「出价」切换判 S1/S2，比模板稳

- `_load_action_centers` 同时扫 stage+actions，`smart_bid_btn`（stage）与 `bid_main_red_btn`（actions）自动进 center 表

**OCR worker（异步，`_ocr_worker_loop`）**：

- 两段式 + **双结果槽**（P4 双通道覆盖 bug 已修，2026-08-20）：第一段关键 ROI `OCR_CRITICAL_KEYS=('bid_result_amount_box','bid_player4')` 识别 → `_ocr_publish_result(..., critical=True)` 写**关键槽** `_ocr_result_critical`（H+P4 独立、不被覆盖）；第二段识别 `阶段keys − _OCR_CRITICAL_SET`（**剔除 H/P4**，同帧不重复识别）→ 写全量槽 `_ocr_result`。主线程 `_apply_ocr_result` 每帧 take 关键槽+全量槽、各自过 provenance/时效闸门后**合并成一份 res 消费**——H/P4 恒来自关键通道（时效最低），P1\~P3/玩家名来自全量通道

- **wait\_result 帧率翻倍（真双通道，用户拍板）**：主循环帧间隔 `WAIT_RESULT_FAST_MS=150`（正常 `FRAME_INTERVAL_MS=300`）——仅报价等待阶段 OCR 投递频率×2，配合动态 keys 剔除已固化槽 → 未固化槽（尤其 P4）读取频率真正翻倍。⚠️ 注意：双通道（第一段）**不改变投递频率**（主线程每帧投递一帧、worker latest-only），只保证 P4 时效最低、不被全量超龄拖死、消除同帧重复识别；「P4 相对其他槽 2× 采样」在 latest-only 单帧架构下物理不可达（报价刷新是时间函数），采样密度提升靠帧率翻倍 + IO 异步化（见下）

- **debug 落盘 IO worker（`_io_worker_loop`，2026-08-20）**：渲染 HUD/ROI/PEEP + raw JPG + rendered WebP 全部移出主线程（生产-消费者，`_debug_enqueue_frame`/`_debug_enqueue_peep` 入队，有界队列满丢帧不阻塞）。原每帧 \~67-100ms 同步存盘曾把 wait\_result 实际帧率从 150ms 拖回 \~240ms；异步化后主循环只剩截图+检测+OCR 消费+心跳

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

[bid\_strategy.py](file:///d:/maaracing_assistant/maaracing_assistant/plugins/treasure/strategy.py)（V3 秒杀火力基准，2026-09-05；V2 数据驱动 2026-08-16；设计文档 `docs/treasure_bid_strategy.md` + `docs/treasure_tick_dynamic_step_report.md`）

- 数据结构：`RoundSnapshot`（上一轮完整公开快照，策略唯一对手信息源）/ `BidContext`（决策输入，含 `opp_high_history` 对手逐轮最高）/ `BidDecision`（决策输出）/ `LureState`（逼价基线，V3 未启用）

- **V3 决策树（decide）**：收入铁律「钱只在第一名利润和亏钱第一名的 15%/10%/5% 分红里」推出双分支——
  ① R1/R2 observe：出 `min(H,余额)`（H 恒在利润线内，撞上低 K 线即低价拍中）；
  ② **对手已证明火力 M = max(历史各轮对手最高, 上轮快照对手最高)**——出价可回放，历史峰值才是真实上限（V2 用上轮价在 401 场被钓鱼降价 748900→500300→766810 骗掉反杀机会）；
  ③ M≤0（无快照/对手全 0）→ observe 式 `min(H,余额)` 等捡漏（**原"嘲讽 250"已删除**：0 与 250 都无分红顺位价值，H 价反而保留捡漏可能）；
  ④ 杀价 `P_win = ceil(K_r × (M + 缓冲))`，缓冲=价格桶×利润强度缩放（**willingness 意愿收缩模型已删除**——它是为上轮价基准打的补丁，M 基准天然免疫降价钓鱼）；
  ⑤ `P_win ≤ 买入线 且 ≤ 余额` → **win**（profit 线=0.9×V̂，egg 线=V̂+risk\_cap；「捡漏」不再是独立分支，M 低自然杀价低）；绝不裁剪后买入（2026-08-16 教训：裁剪买入=赌接盘）；
  ⑥ 杀不动 → **target\_second 卡第二吃分红彩票**（未拍中出价不花钱；分红仅当赢家亏钱才有），upper=min(M−u, cap, balance) 安全垫防对手 30% 退出率把我方顶成第一意外接盘；区间挤不下 → 紧贴价 → pass（仅剩余额 0 等场景，T=0 走通用输入链=合法弃权）。

- **phase 门控**（`_bid_phase`：wait\_first/wait\_next/bidding/wait\_result）：面板「关→开」上升沿只在等待相位有效才建新 bidding epoch，防模板抖动制造假 epoch；提交后 wait\_result，OCR 4 槽全部「固化」（见上槽级固化）才构建快照并放行 wait\_next

- **输入子状态机**（`_run_bidding_execute`，画面驱动）：输入框当前值 B（OCR `bid_result_amount_box` 实时读）对比目标价 T——B==T 点 `bid_confirm_red_btn`；B==0 或前缀不匹配点 `bid_numpad_clear`；前缀匹配输下一位 `bid_numpad_{d}`。不依赖「我点过了」内部标记，用户任何遗漏/改价都能自动纠正

- **附加回合**：`_extract_round_from_stage` 正则提取任意「第N回合」，`set_stage` clamp 到 5（附加回合数据统一写进第5回合槽），用原始数字判断回合切换以正确重置转场期

- `_bid_input_latest` 无条件更新：OCR 读到无数字（已清空/占位）→ 0，避免输入子状态机反复点✖死循环

***

## 3. treasure\_detector 阶段检测器

[treasure\_detector.py](file:///d:/maaracing_assistant/maaracing_assistant/plugins/treasure/detector.py)

**职责**：

- 按优先级扫描 `_ROI_STAGE` 映射的 stage ROI（`priority` 决定顺序）

- 同 ROI 多模板聚合匹配（TM\_CCOEFF\_NORMED，默认阈值 0.75）

- 匹配强度弱告警节流（同 ROI 每 30s 一次）

- 回合识别：roundN\_banner 模板 → 文件名解析回合号；横幅未命中时 OCR 兜底读「第N回合」小字

**核心接口**：`detect(frame_rgb) -> DetectResult`；结果支持旧式 `stage, round_no = detect(...)` 解包，同时提供 `scores`、`hit_anchor`、`active_used` 供 trace 还原。

v3 唯一真源：detector 从 `treasure_assets.json` 编译 `DetectionPlan`；M4 后运行时不再读 `treasure_rois.json`（v2 已退役归档），v3 缺失/损坏时阶段检测降级为空（不读 v2）。模板缓存按文件 `mtime_ns + size` 失效，控制台替换模板后不会永久命中旧图。

**自定义阈值**：`result_banner=0.900`、`is_matching_btn=0.900`（v3 `anchors.*.threshold` 字段；result_banner 另有 `arbitration.template_thresholds.result_auction_win_banner=0.60`）

***

## 4. treasure\_ocr 金额识别

[treasure\_ocr.py](file:///d:/maaracing_assistant/maaracing_assistant/plugins/treasure/ocr.py)

**职责**：

- RapidOCR（rapidocr\_onnxruntime）薄封装，懒加载引擎、失败降级

- `recognize_amounts(frame, min_amounts=...)`：对 ocr 段 ROI 逐区识别 → 金额解析

- 金额提取加固：千分位逗号优先、重复逗号合并、`MIN_AMOUNT=10000` 过滤、7 位噪点前缀处理

- **CPU 亲和性**：`PIN_P_CORE_AFFINITY=[0..7]` 绑定 P-core（本机 Intel Alder Lake 8P+4E，E-core 推理慢 \~2.15 倍，详见 OCR\_LATENCY\_SPIKE\_ANALYSIS.md）

- `USE_CLS=False` 关闭方向分类

***

## 5. treasure\_renderer HUD 渲染

[treasure\_renderer.py](file:///d:/maaracing_assistant/maaracing_assistant/plugins/treasure/renderer.py)

**职责**：复用调试渲染器，绘制鉴宝专属 HUD：

- 阶段/回合号、系统报价 H、估值区间、我方出价、排名

- 5 回合 H 历史折线图、玩家出价表

- **准星渲染**：`treasure_action`（程序想点击的位置，`_resolve_action_target` 输出）画黄色准星 + 目标说明

- 底部 12 阶段进度条、OCR 性能指标（total/failures/dur\_ms/age\_ms）

***

## 6. 校准与编辑工具（v4 形态）

> v3 校准台（`tools/navkit/server.py` + React 前端 + adapter 投影）已于 P4a 整体退役。

- **画布编辑**：`tools/navkit/mpe.cmd` 起 mpelb（root=仓库根）并在浏览器打开 MPE——节点/ROI/模板引用直接编辑 v4 真源 `resources/pipeline/treasure.json`（round-trip 保真 P3a 实证）。
- **策略表**：`mpe.cmd` 同批打开 `policy_server.py` 薄页（127.0.0.1:26530），编辑 `resources/policy/treasure.policy.json`。
- **运行时数据面（过渡态）**：detector/决策栈/ROI 仍读 v3 `resources/config/treasure_assets.json`——P4b 数据源切换后 v3 真源退役，本节届时更新。
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

| 方法                           | 说明                                                                       |
| ---------------------------- | ------------------------------------------------------------------------ |
| `detect(frame_rgb)`          | 返回 `DetectResult`（兼容二元组解包）：按 v3 DetectionPlan 扫描 + scores/hit\_anchor 明细 |
| `_round_from_template(name)` | roundN\_banner 文件名 → 回合号                                                 |
| `_round_no_from_text(text)`  | OCR 文本提取回合号（1\~5 之外视为噪声）                                                 |
| `_round_label_rect()`        | 回合小字 OCR 区 rect（优先 ocr.round\_label\_area）                               |

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

配置源 v3 `treasure_assets.json`（`anchors`，rect/threshold 逐项；M4 后 `treasure_rois.json` 退役归档）；匹配阈值：全局默认 0.75，鉴宝师代码回退默认 0.72（v3 已校准 0.8）、对勾默认 0.62、智能出价按钮默认 0.72（锚点 `threshold` 逐项覆盖）：

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
> ¹ 鉴宝师 `threshold` 已在 v3 `anchors.appraiser_p*.threshold` 校准为 0.80（代码回退默认 `_APPRAISER_MATCH_THRESHOLD=0.72`，控制台「偏好鉴宝师」分类可逐项覆盖）。

***

## 9. 鉴宝坑点

| 坑点                                    | 说明                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | <br /> | <br /> | <br />                       |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----- | :----- | :--------------------------- |
| 准星意图模式                                | 当前全部逻辑只算「程序想点击的位置」，经 `_decide_action → _resolve_action_target → _treasure_kwargs → debug.save_frame` 渲染 PEEP 准星，**不执行真实点击**（已删除 `_click_norm`）                                                                                                                                                                                                                                                                                                                                                                                          | <br /> | <br /> | <br />                       |
| 模板 ROI 分类语义                           | `treasure_rois.json` 分三段：**stage = 模板匹配做阶段/状态判定**（如 `session_start_match_btn` 判详情卡出现、`is_matching_btn` 判匹配中）；**actions = 纯 rect 中心点击按钮**（准星直接用中心，不挂模板，如 `session_master_badge`/`session_expert_badge`/`session_intern_badge`/`session_start_match_btn`/`confirm_red_btn`）；**ocr = RapidOCR 识别区**。按钮位置固定就别放 stage 段挂模板                                                                                                                                                                                                                   | <br /> | <br /> | <br />                       |
| 鉴宝师/场次多尺度匹配                           | 0.70\~1.30× 共 13 档（步长 0.05），缩小时 `INTER_AREA`/放大 `INTER_CUBIC`；中心/右边界用**缩放后模板尺寸**计算（不是原始尺寸）；鉴宝师代码默认 0.72，JSON 逐项覆盖为 0.80；对勾默认 0.62                                                                                                                                                                                                                                                                                                                                                                                                       | <br /> | <br /> | <br />                       |
| 阶段感知动态激活                              | 非标准窗口（DPI 缩放）下画面模糊 → 单点匹配分不稳定（如 smart\_bid\_btn 多尺度仅 0.686，达不到 `_SESSION_MATCH_THRESHOLD` 0.90 → 面板判未开 → 不点智能出价）。`treasure_module._STAGE_PERCEPTION` 按阶段只激活「当前画面必然出现/相关」的 stage ROI，`detect(active_rois)` 只扫交集；全局锚点 `_GLOBAL_ANCHORS`（`hall_peak_appraise_card` 掉回大厅兜底）始终全量并入。阶段未登记 → 回退全量（安全兜底）。OCR 同理按 `_STAGE_OCR_KEYS` 裁剪 worker 第二段 keys。**新阶段必须登记感知清单**（含转移信号，如出价阶段必须含 settle\_title/result\_banner），否则只跑锚点 → 永不切换。smart\_bid\_btn 阈值已解耦：读 JSON `stage.smart_bid_btn.threshold`，缺省回退 `_SMART_BID_MATCH_THRESHOLD=0.72`（不可复用 0.90） | <br /> | <br /> | <br />                       |
| 「已选中」对勾判定                             | `stage.appraiser_selected_check` 是横向长条 rect（覆盖三卡右上角对勾高度带），扫描黄色√；判定对勾中心 X ≈ 目标卡片命中框右边界（容差 0.09）                                                                                                                                                                                                                                                                                                                                                                                                                                          | <br /> | <br /> | <br />                       |
| 鉴宝师搜索区                                | `_APPRAISER_SEARCH_ROI=(0.03,0.18,0.97,0.92)` 全屏范围（三卡位置/尺寸不固定），顺位 P1 卡洛琳→P2 章太郎，均未命中→准星指屏幕中心                                                                                                                                                                                                                                                                                                                                                                                                                                            | <br /> | <br /> | <br />                       |
| 回合出价状态机                               | `_run_bidding_choice`：S0 转场期/S1 等待/S2 点主出价按钮/S3 面板内智能出价→确认出价；「等待/出价」用 OCR 文字判（`ocr.bid_main_btn_label`），面板是否打开用 `stage.smart_bid_btn` 模板判；等待状态 `key=None` → `_resolve_action_target` 返回 None 不出准星                                                                                                                                                                                                                                                                                                                                       | <br /> | <br /> | <br />                       |
| 出价按钮明暗                                | 主出价按钮「等待出价/出价」明暗态**不要用模板匹配**（禁用态透明渐变 + 亮度变化 → 置信度跳变，见 Experience 1112416），改用 OCR 文字判状态                                                                                                                                                                                                                                                                                                                                                                                                                                                  | <br /> | <br /> | <br />                       |
| `_load_selected_check` 解包             | `_, fname = _SELECTED_CHECK_DEF` 是**二元组**；按三元组解包会报 `not enough values to unpack (expected 3, got 2)`                                                                                                                                                                                                                                                                                                                                                                                                                                    | <br /> | <br /> | <br />                       |
| 调试台黑屏                                 | 截图正则 `_RAW_RE` 必须覆盖 \`png                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | jpg    | jpeg   | webp\`（原始存盘是 JPG，只认 png 会全黑） |
| 调试台框交互                                | 框显示开关 `showRois` 需同步 `hitTest()`（none→全部不响应；selected→仅选中项响应），否则隐藏的框仍可被点中/拖动                                                                                                                                                                                                                                                                                                                                                                                                                                                             | <br /> | <br /> | <br />                       |
| 回合小字                                  | 已由模板像素差改为 OCR 识别（`round_label_area` 迁入 ocr 段），`round_label_*.png` 模板已删除                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | <br /> | <br /> | <br />                       |
| pyright 类型噪音                          | `tuple(float(n) for n in list)` 会被推断为 `tuple[float,...]`，赋给 `tuple[float,float,float,float]` 报错 → 用显式 4 元构造 `(float(r[0]), float(r[1]), float(r[2]), float(r[3]))`；多尺度 `best` 元组是 **6 元**（score,scale\_idx,x,y,th,tw）                                                                                                                                                                                                                                                                                                                   | <br /> | <br /> | <br />                       |
| 主循环单帧异常兜底                             | `start()` 主循环**必须**对 `_tick_once()` 做 per-frame `try/except`，否则单帧未捕获异常会直接杀死整个主循环/模块线程（2026-08-19 核实原实现是 `try/finally` 无 `except`）。正确做法：单帧异常跳过继续并 `WARNING`，仅当连续 `_MAIN_CRASH_RETRY_MAX=30` 帧（≈9s）仍异常才上抛走 `finally` 清理终止，防"静默空转"掩盖真 bug                                                                                                                                                                                                                                                                                                  | <br /> | <br /> | <br />                       |
| **按钮点击重试规范**（2026-09-05 定稿，新增按钮默认照此写） | 重试分三层，每层语义不同、不得混淆：**① 执行失败层**：ok=false（物理点击失败）→ 指纹不更新 → 下帧同意图自动重试（无限，直到成功/意图变化）；**② 无响应兜底层**：ok=true（物理成功）但「成功信号」未出现 → 超时后**清指纹重新 arm** 再点（防边沿触发锁死），**必须封顶** `_RETRY_MAX=3`（含首点共 4 次）；**③ 耗尽后果层**：重试封顶后**不得静默**，抛 `ClickRetryExhaustedError` 终止模块（用户可见、可干预）。关键点：每个 key 必须显式声明「成功信号」——阶段切换类 = 阶段名切走（`_maybe_retry_stage_click`）；面板内/数据类 = **指定 OCR 字段变化**（如智能出价 H 读出、出价面板关闭、本场收入读出）；弹窗类 = 离开弹窗阶段（per-key 帧数可覆盖 `CLICK_RETRY_FRAMES_BY_KEY`，上限统一）。**新增按钮加入** **`CLICK_RETRY_KEYS`** **时必须同步定义成功信号**，否则"ok=true 但无效果"仍会指纹锁死静默卡死            | <br /> | <br /> | <br />                       |
| 领取分红「跳过动画」无响应兜底                       | `settle_collect_red_btn` 有两次点击语义：**跳过动画**（首次，成功信号=收入读出）与**真领取**（收入已读出，成功信号=阶段切走）。跳过动画点击物理成功但游戏无响应（实测：点后按钮/动画无反应、收入永远读不到）原逻辑静默卡死。2026-09-05 修复：`_decide_action` dividend\_waiting 分支加 `SETTLE_SKIP_RETRY_FRAMES=10` 超时 + `SETTLE_SKIP_RETRY_MAX=3` 封顶，超时清指纹重试，耗尽抛 `ClickRetryExhaustedError` 终止。重试指纹带 `clicked_once=True` 位与首点不同不撞指纹锁；`_apply_click_success` 每次点击成功重启计时防连点风暴；收入读出（OCR 写入）归零计数                                                                                                                                            | <br /> | <br /> | <br />                       |
| 出价预测基准必须用「已证明火力」                      | 密封拍卖+秒杀成交（当回合第一/第二≥K\_r 即成交）下，对手**历史最高报价 = 可回放的支付意愿下限**；单轮报价含「钓鱼蓄力」噪声（实测 401 场：P3 报 748,900→降 500,300→末轮 766,810 秒杀成交赚 26.9 万，我方利润线 893,836 内本可反杀未杀）。任何"按对手价出牌"的逻辑，基准一律 `M = max(历史各轮对手最高, 上轮快照对手最高)`，禁止只取上轮价；更禁止为"上轮价回落"设计收缩补丁（V2 willingness 把降价读成撤退、双重低估火力，V3 已删）。配套收入铁律：未拍中出价不花钱、分红仅在赢家亏钱时存在——能杀必杀（买入线内），杀不动卡第二吃分红彩票（strategy.py V3，2026-09-05）                                                                                                                                                                                     | <br /> | <br /> | <br />                       |

## 10. 遗留问题清单（v1.0 发布前核查）

| 项                   | 类别      | 结论 / 处理                                                                                                                                                                               |
| ------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 主循环无 per-frame 异常兜底 | 稳定性 bug | ✅ 已修复：`start()` 对 `_tick_once` 包 try/except，单帧异常跳过，连续 30 帧才终止（见坑点表）。`py_compile` 通过                                                                                                   |
| 阶段切换类点击卡死           | 稳定性     | ✅ 已核实无需改：`_maybe_retry_stage_click` 用 `CLICK_RETRY_MAX=3` 封顶，达上限 WARNING 停止，换 key/切阶段归零，有界无死循环                                                                                        |
| 结算后弹窗连点/跳过          | 稳定性     | ✅ 已修复（既有）：`POPUP_CLICK_COOLDOWN_FRAMES=5` 冷却 + `POPUP_LOOPBACK_STABLE_FRAMES=3` 连续稳定帧确认，详见坑点表弹窗链相关条目                                                                                  |
| 领取分红跳过动画点击无响应卡死     | 稳定性     | ✅ 已修复（2026-09-05）：`dividend_waiting` 加 `SETTLE_SKIP_RETRY_FRAMES=10` 超时 + `SETTLE_SKIP_RETRY_MAX=3` 封顶，超时清指纹重试，耗尽抛 `ClickRetryExhaustedError` 终止（见坑点表「领取分红跳过动画无响应兜底」）。`py_compile` 通过 |
| 按钮重试规范未成文           | 规范      | ✅ 已定稿（2026-09-05）：三层重试语义（执行失败无限 / 无响应封顶 3 次 / 耗尽终止）+ 每 key 显式成功信号，见坑点表「按钮点击重试规范」，新增按钮默认照此写                                                                                            |

