# ADR-0014（ownership-core）：大阶段/步的重试状态转移归 core 拥有，插件只喂 verdict、收 command

- **状态**：`accepted`
- **日期**：2026-11-01
- **决策者**：用户
- **相关**：宪法 §4「机制归引擎、策略归数据」（[PIPELINE_V4.md](../PIPELINE_V4.md)）· [ADR-0001](0001-arch-global-分层靠引用方向不靠目录.md)（core 不得引用 `<module>.` 节点名）· [ADR-0006](0006-meta-adr-决策粒度只收跨模块约束.md)（本条只记归属决策，机制现状载 [core/CODE_WIKI.md](../../maaracing_master/core/CODE_WIKI.md) 步循环件）· commit `71441c0` / `46df995` / `f8add47`

## 背景

活动插件在「一次 UI 点击 → 等后置条件成立 → 有限重发 → 耗尽即显式终止」这套编排上各自手搓：`treasure`（已退役，[RETIRED.md](../../maaracing_master/plugins/_archive/treasure/RETIRED.md)）有三条结构相同的点击重试链，`speedrush` 的 UI 步循环则点了没生效只能靠下一步识别不到、白等框架默认 20s 后中止整轮。同一语义多处各写一遍，偏差随时间积累会演变成"没人看得懂、改一处漏一处"的维护债。用户裁定：把"大阶段/步"这一层的重试**协议**统一进 core，现在就定，不延后。

## 决策

以下规则可被机检或人工判定：

1. 步级重试的**状态转移**——计数、时间窗、到期重发、耗尽、停止取消——由 core `StepMachine` **唯一解释**。迁移/接入后，插件侧与 `policy.json` 都不得再各自保留一份重试计数或时间状态（单一真源；否则即假统一）。
2. `StepMachine` 只认 `Verdict`（SUCCESS/PENDING/STOP）与 `Command`（WAIT/RETRY/DONE/FAIL/ABORT），**不出现阶段名、节点名、玩法语义**——遵守 [ADR-0001](0001-arch-global-分层靠引用方向不靠目录.md) 的引用方向红线；节点名由插件传入。
3. **"什么算一次尝试、何时 `arm`、用什么方式驱动"** 归插件（阻塞逐帧循环里喂，或逐帧回调里喂）。两家的差异只体现在调用 `arm` 的时机与是否喂 STOP，不体现在改机器语义。
4. 统一只到**大阶段/步**这一层。插件内小状态（指纹边沿锁、出价子状态、弹窗分类、回合哨兵）不进 core；**不做回合级自动重开**、**不把页面确认 / 阶段识别塞进 core**。

## 后果

**收益**：重试语义只有一处解释、可被纯逻辑单测覆盖；新活动插件复用同一协议而不再手搓；`speedrush` 借此长出"落地页后置确认"这一早该有、此前被缺的能力。

**代价与约束**：
- 未来任何活动插件做"大阶段之间的一次 UI 点击"的重试，**必须**走 core `StepMachine`，**不得**自存计数/时间。
- core 必须**保持域无关**——core 真源不得出现 `<module>.` 节点名（`truth` namespace 校验守此）。
- "统一调度"**不等于"core 来跑循环"**：逐帧决策（`PolicyBridge`）与阻塞步循环各留插件，进 core 的只有状态转移记账。禁止借本决策合并执行模型、下探到逐帧循环或感知层。

**被否方案**：
- **图原生 `on_error`/`timeout` 做重试**——`speedrush` 的步刻意不是连通图（同页多按钮 dwell 分不开、中间夹驾驶内环），`treasure` 是逐帧 + 指纹边沿锁（`on_error` 只在节点识别失败回退，管不了"已点但要用指纹挡连点"）。口径：`on_error` 是兜底不是重试机（见 MAAFW_GUIDE）。本件是图重试语义的**补充**，能用图 `timeout`/`on_error` 表达的线性回退仍该用图。
- **被动记账件**（调用方自己决定重发，机器只存数字）——不拥有状态转移，计数与计时易在插件与机器两处分裂，正是本决策要消灭的。
- **加 `PROGRESS` verdict**（非终态但重置窗口）——逐链核过四链都不需要：数字键"读回推进"是该位数字的终态 SUCCESS（下一位新机器），"再次送达重置窗口"靠重新 `arm`。留一个无使用者档 = 投机抽象。

## 证据

- 机检：`tools/pipeline/truth.py`——图自洽、`__confirm` 确认锚点零「跨锚点重复识别」告警；`tools/check_repo_hygiene.py` 通过。
- 测试：`tests/test_step_machine.py`（三 verdict × 五 command 转移表，逐链复刻 treasure 计数表：重发上限不含首点、耗尽为"达上限后再等一窗"、计时在重发/送达时重置、在途 RETRY 持续到送达且每窗只计一次）；`tests/test_speedrush_gating.py`（步级重发、窗内命中不重发、停止不记耗尽）。
- 一手核查：极速狂飙操作路径与各界面要素 [plugins/speedrush/RULES.md](../../maaracing_master/plugins/speedrush/RULES.md) §2 / §3；treasure 三条重试链计数表（见 commit `71441c0` 的 core 单测对照）。
- 落地：commit `71441c0`（core `StepMachine`）、`46df995`（speedrush 步级重试 + `__confirm` 确认锚点）、`f8add47`（重试剂量搬到 `decision.json`，可分步覆盖）。
