# 巅峰鉴宝插件（已退役 · 完整示范实现）

> **本文是什么**：本插件的当前状态、作为示范的价值、它与 core 的依赖边界、复活步骤。
> **本文不是什么**：不是玩法规则（见 [RULES.md](./RULES.md)）；不是域内实现知识（见 [CODE_WIKI.md](./CODE_WIKI.md)）；不是插件平台架构（见 [docs/CODE_WIKI.md](../../../../docs/CODE_WIKI.md) 与 [core/CODE_WIKI.md](../../../core/CODE_WIKI.md)）。
> **信源等级**：L2 —— 可作「本插件为何退役、依赖哪些 core 能力、如何复活」的直接依据。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../../../AGENTS.md)。

## 当前状态

已退役，**归档于 `plugins/_archive/treasure/`**，保留在仓库树内作为完整示范实现。

归档状态由**目录位置**表达（`plugins/` 顶层只放现役插件），四处消费者按同一位置判据工作：

- **运行期**：`core/registry` 只扫描 `plugins/` 的直接子目录，本模块不注册、不出现在模块列表，也不再有置灰项。[manifest.py](./manifest.py) 的 `VALID_UNTIL` 保留为「移回顶层后按活动窗口置灰」的兜底判据，不再是归档判据。
- **分发**：`assemble.ps1` 的 robocopy 以 `/XD _archive` 整目录排除，并在剔除段末尾带反向守卫（stage 里出现该目录即拒绝发布）；不依赖有效期判定。
- **仓库树**：代码、资源、测试、修复脚本全部保留；`check_truth` 的插件扫描面只含 `plugins/` 顶层带 `module.py` 的目录，本插件归档后不在其内——真源文件仍在树内，复活时随目录回迁重新纳入。
- **测试**：回归锁位于本插件 `tests/` 目录，带 `retired` 标记；默认收集（`testpaths = ["tests"]`）不含归档目录，考古时显式运行：
  `.venv\Scripts\python.exe -m pytest maaracing_master/plugins/_archive/treasure/tests -m retired`。
  理由见该标记的说明：避免死模块的回归锁反向约束 core 演进。**复活时若不摘标记，回归锁会静默不跑**。
- **修复脚本**：脏数据修复工具在 `tools/repair_treasure_settle_data.py`。
- **随插件下沉的 core 小件**：`core/cpu_time.py`、`core/trace.py`——二者原先在 `maaracing_master/core/`，使用者只有本插件，故随归档按类下沉；复活时随目录回迁，或按需改回 core。

> **为什么归档区仍是一个可导入的包**：`pytest` 在收集阶段就会导入测试模块，退役用例的
> import 若失效，`-m 'not retired'` 也救不了——整个默认测试会以收集错误告终。故
> `plugins/_archive/__init__.py` 存在，归档模块保持可导入；「不注册、不分发」由位置与
> 打包排除保证，不靠让 import 失败来实现。

## 为什么保留为示范

本插件是**唯一没有后继的完整活动插件实现**（对比：racing 被后继插件取代、能力已转移，故整体移出活动代码），因此是下列能力的**唯一完整落地样本**：

- **NavKit v4 通路**——图节点、策略真源与运行期执行底座三者的完整接线；
- **策略表驱动的感知与决策**——`resources/policy/` 下的策略文件作为单一真源，感知规格与引擎契约同源；
- **插件自包含**——代码、模板图、感知/决策真源、资源全部位于本目录，拷走即卸载、放入即安装。

## 与 templates/plugin/ 的分工

| 问题 | 看哪里 |
|---|---|
| 新插件怎么起步（最小可运行骨架） | [`templates/plugin/`](../../../../templates/plugin/README.md) |
| 完整实现长什么样（真实活动的复杂度） | 本插件 |

样板演示平台契约本身；本插件演示契约在真实活动里的用法。样板给不了完整实现的复杂度，本插件是它的对照面。

## 依赖边界（护栏）

本插件仍在 import 的 core 能力**不因本插件退役而删除**。按「是否为 treasure 而生」重新归位：

- **随插件下沉**：`cpu_time`、`trace`、`stage_tracker`、`policy`、`v4_source`——仅本插件使用，已移入本目录 `core/`，不再占 core 的维护面。
- **有第二消费者**：`core/ocr.py`、`core/paths.py`、`core/nav_graph.py`、`core/debug.py`、`core/logger.py`、`core/base.py`（speedrush 在用）。
- **core 内部消费者**：无。策略引擎、装配器与阶段追踪器的唯一调用方就是本插件；原 navkit 包已拆解。
- **通用能力**：`core/template_match.py`、`core/capabilities.py`、`core/clicker.py`。

完整依赖清单以本插件源码的 import 语句为准（L0 真源）；本节只记判据与易误删项，不复制清单。

## 复活步骤

1. 把本目录整体移回 `plugins/treasure/`（归档区不参与注册与分发，移回即恢复发现）。
2. 改 [manifest.py](./manifest.py) 的 `VALID_UNTIL` 到新的活动窗口（或删除该行 = 永久有效）。
3. 确认 GUI 默认模块不再硬编码本插件（见 `core/sidecar.py` 的默认模块常量）。
4. 把本插件的 `tests/` 移回仓库 `tests/`、`tools/repair_treasure_settle_data.py` 移回仓库 `scripts/`，摘掉测试的 `retired` 标记（`pytest -m retired` 能跑不等于会被跑），再跑 `pytest` 与分层机检——否则回归锁静默跳过，复活后第一轮就没有防线。
5. 全仓搜索 `plugins._archive.treasure` / `plugins/_archive/treasure`，改回顶层路径
   （测试导入、工具脚本、文档指针都已按归档路径写死）。

无需改 registry、无需改发布脚本（归档区排除与顶层剔除各自独立）。
