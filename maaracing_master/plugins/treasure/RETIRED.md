# 巅峰鉴宝插件（已退役 · 完整示范实现）

> **本文是什么**：本插件的当前状态、作为示范的价值、它与 core 的依赖边界、复活步骤。
> **本文不是什么**：不是玩法规则（见 [RULES.md](./RULES.md)）；不是域内实现知识（见 [CODE_WIKI.md](./CODE_WIKI.md)）；不是插件平台架构（见 [docs/CODE_WIKI.md](../../../docs/CODE_WIKI.md) 与 [core/CODE_WIKI.md](../../core/CODE_WIKI.md)）。
> **信源等级**：L2 —— 可作「本插件为何退役、依赖哪些 core 能力、如何复活」的直接依据。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../../AGENTS.md)。

## 当前状态

已退役，**保留在仓库树内**作为完整示范实现。

退役状态由 [manifest.py](./manifest.py) 的有效期（`VALID_UNTIL`）表达，与运行期置灰、发布分发剔除共用同一判据（`core/module_validity`），不建第二真相：

- **运行期**：GUI 列出但置灰、不自动选中；显式选择需带 `force`。
- **分发**：打包时刻已失效，不随包分发（`scripts/release/list_ship_excluded_modules.py` → `assemble.ps1`）。
- **仓库树**：代码、资源、测试全部保留；真源机检（`check_truth`）照常覆盖。
- **测试**：回归锁保留但带 `retired` 标记，`pytest` 默认跳过（`pyproject.toml` 的
  `addopts = -m 'not retired'`），需要时 `pytest -m retired` 显式跑。理由见该标记的
  说明：避免死模块的回归锁反向约束 core 演进。**这不是「照常覆盖」**——复活时若不
  摘标记，回归锁会静默不跑。

> **为什么不用目录位置表达退役**：状态只由 manifest 有效期表达。若把本目录移出 `plugins/` 的直接子目录层级，发布剔除工具会扫不到它（它按 `plugins/*/manifest.py` 发现插件），剔除清单变空，而打包阶段的整包复制已把退役代码与资源带进发布包——「位置」会变成与有效期并存的第二套判据。

## 为什么保留为示范

本插件是**唯一没有后继的完整活动插件实现**（对比：racing 被后继插件取代、能力已转移，故整体移出活动代码），因此是下列能力的**唯一完整落地样本**：

- **NavKit v4 通路**——图节点、策略真源与运行期执行底座三者的完整接线；
- **策略表驱动的感知与决策**——`resources/policy/` 下的策略文件作为单一真源，感知规格与引擎契约同源；
- **插件自包含**——代码、模板图、感知/决策真源、资源全部位于本目录，拷走即卸载、放入即安装。

## 与 templates/plugin/ 的分工

| 问题 | 看哪里 |
|---|---|
| 新插件怎么起步（最小可运行骨架） | [`templates/plugin/`](../../../templates/plugin/README.md) |
| 完整实现长什么样（真实活动的复杂度） | 本插件 |

样板演示平台契约本身；本插件演示契约在真实活动里的用法。样板给不了完整实现的复杂度，本插件是它的对照面。

## 依赖边界（护栏）

本插件 import 的 core 能力**都不可因本插件退役而删除**。判据不是「谁在用」，而是**「它是否为 treasure 而生」——都不是**；本插件只是它们各自的第一个使用者。易被误判为「treasure 专属」而误删的：

- **有意预留的公共骨架**：`core/stage_tracker.py`、`core/cpu_time.py`——二者 docstring 均写明「等第二个使用者」的设计意图，**当前无第二消费者不等于该下沉**。
- **有第二消费者**：`core/ocr.py`、`core/paths.py`、`core/nav_graph.py`、`core/debug.py`、`core/logger.py`、`core/base.py`（speedrush 在用）。
- **有 core 内部消费者**：`core/navkit`（被 `core/nav_graph.py` 引用）。
- **通用能力**：`core/template_match.py`、`core/capabilities.py`、`core/clicker.py`。

完整依赖清单以本插件源码的 import 语句为准（L0 真源）；本节只记判据与易误删项，不复制清单。

## 复活步骤

1. 改 [manifest.py](./manifest.py) 的 `VALID_UNTIL` 到新的活动窗口（或删除该行 = 永久有效）。
2. 确认 GUI 默认模块不再硬编码本插件（见 `core/sidecar.py` 的默认模块常量）。
3. 摘掉测试的 `retired` 标记（`pytest -m retired` 能跑不等于会被跑），再跑 `pytest`
   与分层机检——否则回归锁静默跳过，复活后第一轮就没有防线。

无需移动目录、无需改 registry、无需改发布脚本。
