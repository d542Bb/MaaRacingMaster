# Skill · 编辑 NavKit v4 Pipeline 真源（与 MPE 协调）

> 用途：让 coding agent 正确、安全地编辑/维护 v4 pipeline 真源，并知道何时该交给
> 人用 MPE 画布、何时该直接改 JSON。简体中文，可被 agent 在相关任务时读取。
> 一手依据：P3a/P3b 实证（2026-09-09）——mpelb v1.9.3 round-trip 保真、MPE Iframe
> 协议、scanner 索引规则。

## 0. 核心结论（先读这段）

- **v4 真源是 pipeline 节点 JSON，就是事实本身。** agent 编辑真源 = 直接改 JSON +
  跑校验器；不需要也无推荐用 MPE 的浏览器 UI 改。

- **MPE 是面向「人」的画布**，用于可视化审阅、给人拖拽编辑、调试；agent 的角色是把
  真源改对 + 校验，把「要不要看图画」的判断留给用户。

- MPE 满幅即 Studio（C 形态），`start studio.cmd` 拉起（起 mpelb + 开 MPE）。

## 1. 真源位置与契约

| 内容       | 路径                                                                           |
| -------- | ---------------------------------------------------------------------------- |
| core 导航图 | `maaracing_assistant/core/resources/pipeline/global.json`                    |
| 鉴宝业务图    | `maaracing_assistant/plugins/treasure/resources/pipeline/treasure.json`      |
| 决策策略表    | `maaracing_assistant/plugins/treasure/resources/policy/treasure.policy.json` |

- **目录必须叫** **`pipeline`**：mpelb / MSE / MaaMCP 等生态工具按 ProjectInterface 惯例
  只索引名为 `pipeline` 的目录或 `interface.json`（P3a 一手：scanner.go）。不要改成别的名字。

- policy 表不放在 pipeline 目录（MaaFW 递归加载会把它当 pipeline 解析致整目录失败，Q1 实证）。

## 2. agent 编辑真源的正确姿势

1. 直接改 JSON（用文件编辑，注意 UTF-8、`\n`、有序键——键序人读友好，migrate\_v4 已对齐 MPE 序）。
2. 改完跑校验：`.venv\Scripts\python.exe tools\navkit\migrate_v4.py --full --check`
3. 需要运行时态确认时：`NavKitV4.load`（见 tests / experiments），节点数基线=21。
4. 约定字段：

   - 标准字段（recognition/action/next/timeout/rate\_limit/on\_error…）按 MaaFW 协议。

   - 扩展字段用根级 `_` 前缀（`_page` `_route` `_dwell` `_boot` `_park`…）——MPE 无损往返。

   - custom 识别/动作：`type:"Custom"` + `param:{custom_recognition/custom_action, *_param}`（MPE 归一化形态，协议标准）。

   - `next` 跳转可用 `"[JumpBack]<node>"` 写 jump\_back（MaaFW 原生语法）。

## 3. 何时把真源交给 MPE（人的画布）

- 用户要看图画 / 手动微调 / 调试 FlowScope → 让用户 `start studio.cmd`（起 mpelb
  root=仓库根 + 浏览器开 MPE 满幅，文件面板自动列出真源）。

- MPE 保存 round-trip 保真已验证（键序重排、Custom 归一化、`$__mpe_*` 附加键运行时
  容忍、JumpBack、空值省略），agent 不要因为这些"漂移"去改回 MPE 的输出。

- MPE **无插件/侧栏面板注入点**（Iframe 目录只承载 loadPipeline/save/document\_changed）——
  不要承诺把自研 UI 挂进 MPE。

## 4. 不要做

- 不要直接在真源里手写非标准字段冒充 MPE 配置。

- 不要让 MPE 用「分离导出」（会生成 `.xxx.mpe.json`，不在 pipeline 目录、不被加载）。

- 不要在 policy 规则里重复发明 MaaFW 已有概念——决策规则用 policy JSON，代码逻辑留 `strategy.py`。

