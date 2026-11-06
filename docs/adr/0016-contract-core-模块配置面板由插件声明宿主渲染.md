# ADR-0016（contract-core）：模块配置面板由插件声明、宿主通用渲染

- **状态**：`accepted`
- **日期**：2026-11-04
- **决策者**：用户（方向与契约形状两轮确认；评审补强异步时序与契约状态）
- **相关**：`core/config_ui_contract.py`（契约实现）、`core/CODE_WIKI.md` §5.3.1、`plugins/speedrush/CODE_WIKI.md`「驾驶模式档」、`templates/plugin/README.md`；先例 ADR-0011（模块声明行、core 判级）

## 背景

活动插件对外承诺「复制即装、删除即卸」，但模块专属的配置面板、任务卡摘要、面板状态行全部写死在壳前端：新增插件没有 UI，删除插件留下死代码，归档的鉴宝面板至今挂在壳里。后端早已有半套契约（`DEFAULT_MODULE_CONFIG` 给键与默认值、`config_choices()` 给动态候选），缺的是「字段怎么呈现」这一层；把展示语义继续留在壳里，等于把插件差异转移成宿主分支。

## 决策

1. **模块用类属性声明表单**：`CONFIG_UI_CONTRACT`（版本，当前 1）+ `CONFIG_UI`（有序分组字段）。字段只描述呈现：键、控件类型（开关 / 分段 / 下拉 / 数字 / 文本）、标签、说明、提示、静态选项、动态候选引用、数字约束、单条件显隐（`equals` / `not_equals` / `in`）、按另一字段取值覆盖文案；分段选项可带徽章（`{text, level}`），字段可挂内联提示（`{text, level, actions}`），提示动作只认宿主固定的 `download_release` 与 `open_user_data_folder`（后者可带用户数据目录内的相对 `subpath`）。字段键必须在本模块 `DEFAULT_MODULE_CONFIG` 内；默认值不回写进表单；动态候选只写 `config_choices()` 的引用键，不复制清单。
2. **重建式白名单**：契约规范化只复制认可的成员，未知成员、非法条件、越界选项一律丢弃；结构上不可能夹带 HTML / JS / URL。**不提供插件自带标记或脚本的扩展口**——前端是 file:// 静态单页、无构建链与沙箱，信任边界只能靠白名单 + `textContent`；缺口优先扩控件类型并升契约版本。
3. **契约状态可区分**：`none`（未声明，按设计无面板）/ `ok` / `partial`（部分字段被丢弃）/ `invalid`（声明不可用）/ `unsupported_version`（模块版本与宿主不符）。后三态由宿主生成一句提示、日志按模块去重记录；契约问题只让面板失联，不阻断配置读写。
4. **摘要与状态行同归模块**：`config_summary(config)` 产出任务卡 chips，入参是宿主投影后的有效配置（默认值已合并、旧键已折算、只含声明键、无 `_` 元数据）；`config_status()` 产出面板状态行（仅运行中的实例，形状 `{text, level}`）。宿主只做类型、长度与白名单校验。
5. **配置修订号**：sidecar 每次成功写入配置 `_rev+1`，`get_module_config` 两条读取路径都携带；前端按卡比较，旧轮询响应一律丢弃。保存同卡串行、快速连改合并为 pending；列表重建递增代际，旧回调作废。这三条是前端渲染器的行为契约，不引入通用状态管理库。

## 后果

**收益**：配置面板、摘要、状态行随插件目录走——复制即装、删除即卸；归档插件的面板跟着 `_archive` 一起保留，壳里不再出现任何模块字段名；新增控件类型只需扩契约并升版本，宿主只加一个渲染分支。

**代价与约束**：壳里保留一套通用渲染器与契约校验；插件声明受字段/分组/长度上限约束；复杂交互不能靠插件写代码解决，必须论证后扩 schema。内置步骤（启动/关闭游戏）的面板仍由宿主 `BUILTIN_FIELDS` 渲染，不在本契约内。配置热更新仍不做：保存只写缓存，下次「开始」注入。

**被否方案**：① 引入完整 JSON Schema（`$ref`、组合器与校验器依赖，前端无构建链；只借「数据与呈现分离」的思想）；② 插件自带 HTML/JS（注入面与视觉一致性都不可控）；③ 每个模块在壳里加一块静态面板（即现状，正是要消灭的耦合）；④ 摘要/状态行继续留在壳里（删除插件仍留死代码，只迁一半）。

## 证据

- 契约与校验：`maaracing_master/core/config_ui_contract.py`；机检 `tests/test_config_ui_contract.py`（状态矩阵、字段/条件降级、版本不符、摘要输入边界、`_rev` 单调）。
- 接线：`core/sidecar.py` `_attach_config_meta`（静态与 live 两路统一附加）；`core/base.py` 四个可选钩子。
- 迁移：`plugins/speedrush/module.py`（三档分段、宏组显隐、录制开关按档换文案）；`plugins/_archive/treasure/module.py`（归档目录自包含，退役测试 `plugins/_archive/treasure/tests/test_config_ui_treasure_retired.py` 直接导入校验）。
- 前端：`frontend/js/config-panel.js`（按卡渲染、按 rev / 代际守卫）、`config-panel-state.js`（纯逻辑，`tests/js/config_panel_state.test.mjs` 用 node --test 覆盖旧响应、保存串行与代际作废）；静态锁 `tests/test_frontend_config_panel.py`。
- 外部参照：MaaFramework ProjectInterface V2（option / case / input 词汇）、VS Code `contributes.configuration`（声明式字段 + 宿主渲染）、JSON Forms（数据与 UI 两层分离、规则式显隐）。
