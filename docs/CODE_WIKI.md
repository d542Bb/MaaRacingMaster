# MaaRacingMaster — Code Wiki（项目逻辑指示牌）

> **本文是什么**：项目**逻辑指示牌**——分域导航（东西在哪）、改动去哪找（我要改 X 去哪）、关键约定去哪核对（真源指向）。架构图、目录树、技术栈与一切取值均不在此复制，只在真源处维护。
> **本文不是什么**：不是项目定位与技术栈（[README.md](../README.md)）、不是代码百科（core 模块实现细节、类索引与坑点见 [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md)）、不是活动域知识（`plugins/<id>/CODE_WIKI.md`）、不是架构决策理由（[docs/adr/](adr/README.md)）、不是 MaaFW 协议口径（[MAAFW_GUIDE.md](MAAFW_GUIDE.md)）、不是子系统施工事实（各 `README.md`）。
> **信源等级**：L3 —— 可作「东西在哪、从哪进、去哪核对」的直接依据；**不得**作为实现细节、取值或规则的最终依据（路由到该事实的 home 核对）。
> **真源路由**：按问题类型查出处——见 [`AGENTS.md`](../AGENTS.md)「信源路由」与「信源等级与文档头」。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../AGENTS.md)；本文只承担指示牌职责。

## 分域文档

| 域 | 文档 | 覆盖 |
|---|---|---|
| 项目地图 | **本文** | 分域导航 / 改动去哪 / 约定真源 |
| 平台核心层 | [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md) | controller / 导航光标 / 采集 / 日志 / 调试 / 坑点 / 类索引 |
| 巅峰鉴宝（**已退役 · 完整示范实现**，见 [RETIRED.md](../maaracing_master/plugins/treasure/RETIRED.md)） | [plugins/treasure/CODE_WIKI.md](../maaracing_master/plugins/treasure/CODE_WIKI.md) | 阶段状态机 / 出价策略 / OCR / 渲染 / 域坑点 |
| 极速狂飙 | [plugins/speedrush/CODE_WIKI.md](../maaracing_master/plugins/speedrush/CODE_WIKI.md) | 流程编排 / 驾驶门控与节拍 / 演示录制 |
| MaaFW 协议 | [MAAFW_GUIDE.md](MAAFW_GUIDE.md) | 能力签名 / Pipeline 协议 / Custom 契约 / 真源组织 / 红线 |
| 架构决策 | [adr/](adr/README.md) | 为什么这么定 + 状态（是否仍生效） |
| 版本宪法 | [NAVKIT_V4_PLAN.md](NAVKIT_V4_PLAN.md) | §1 六条不变量（最高规则）+ 节点模型与迁移史 |
| GUI 壳工程 | [apps/MaaRacingMaster.Shell/README.md](../apps/MaaRacingMaster.Shell/README.md) | 锁定版本 / WinUI 3 API / 构建坑 / IPC 契约 / 宿主选型硬约束 |
| 前端与图标 | [apps/MaaRacingMaster.Shell/frontend/README.md](../apps/MaaRacingMaster.Shell/frontend/README.md) | 图标真源 / file:// 约束 / 前端规范 |
| NavKit 工具链 | [tools/navkit/README.md](../tools/navkit/README.md) | 入口命令（`--module` 按活动模块参数化）/ 模板采集工作流 / 校验器护栏 |
| 环境自检 | [SELF_CHECK.md](SELF_CHECK.md) | 分步验收清单（新环境 / 跨机器） |
| 更新说明 | [update_log.md](update_log.md) | 面向玩家的更新说明（Release 正文由此抽取） |

***

## 4. 运行流程

```
MaaRacingMaster.exe（薄 Launcher → app\ 下 Shell；exe manifest 自动 UAC 提权）
  → WinUI 3 shell 创建窗口并加载 HTML 前端
  → 拉起 Python sidecar（python -m maaracing_master）
  → sidecar 初始化 Controller，等待 stdin JSONL RPC
```

前端经 `mra.call(method, params)` 与 sidecar 通信；独立调试 sidecar（不经 GUI）：`.venv\Scripts\python.exe -u -m maaracing_master.core.sidecar`。

「开始」之后的内部编排不在此复述，按需查真源：窗口连接与 `start_module` 分发见 [core/CODE_WIKI.md §3.1](../maaracing_master/core/CODE_WIKI.md)（连接幂等、超时保护、窗口统一、屏幕内校验）；前端 `mra.call` → C# → sidecar handler 的跨进程跳转见壳工程 [README](../apps/MaaRacingMaster.Shell/README.md)（`RpcBridge.cs` 与 sidecar transport 契约）；模块内断点解析、主循环与资源释放见各域 `CODE_WIKI.md`（主控只做生命周期与能力门面，不编排活动流程）。

***

## 关键约定（取值不在此维护，只指真源）

| 约定 | 真源 |
|---|---|
| 游戏分辨率 | `core/window_utils.py` `resize_game_window_720p`（连接时统一窗口）+ `core/module_config.py` `reference_size`；各插件 ROI / 按钮据此归一化 |
| 截图通道 | WGC 中心缓存唯一、无回退通道，见 [ADR-0003](adr/0003-运行时拓扑硬约束.md) |
| 业务数值（ROI / 阈值 / 仲裁） | 各插件 `resources/policy/` 目录（命名不统一：treasure 为 `<id>.policy.json`，speedrush 为 `decision.json` / `hud_regions.json`） |
| 版本号 | git tag 为唯一信源，规则见 [AGENTS.md](../AGENTS.md) 与 [skills/project-update/SKILL.md](../skills/project-update/SKILL.md) |

***

## 我要改 X，应该去哪里

| 想改什么 | 去哪 |
|---|---|
| 某个活动的流程 / 策略 / 按钮 | `plugins/<id>/`（模块实现 + `resources/pipeline` 图 + `policy` 数据面），域知识见其 `CODE_WIKI.md` |
| 活动怎么运转（游戏规则） | `plugins/<id>/RULES.md`（外部事实，修改须人工复核） |
| 主控生命周期、能力门面、点击方式 | `core/controller.py` + [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md) |
| 光标导航 / 虚拟手柄 | `core/gamepad_cursor.py` + `core/resources/stick_speed_model.json` |
| 截图采集 / 帧通路 | `core/wgcap.py`（拓扑约束见 [ADR-0003](adr/0003-运行时拓扑硬约束.md)，不得新起截图通路） |
| 日志级别 / 通道 / 落盘 | `core/logger.py` + [core/CODE_WIKI.md §1.5](../maaracing_master/core/CODE_WIKI.md) |
| 模板图与锚点（采集 / ROI） | [tools/navkit/README.md](../tools/navkit/README.md)（采集工作流 + 校验器） |
| 文字识别（OCR）引擎与调参 | `core/ocr.py`（跨插件共享底座的唯一真源）+ [core/CODE_WIKI.md §1.11](../maaracing_master/core/CODE_WIKI.md)；插件侧只留「文本怎么解释」的领域口径 |
| 页面图节点 / 连线 | `plugins/<id>/resources/pipeline/*.json`，用 MPE 编辑（入口见 navkit README） |
| GUI 窗口 / 前端 / 图标 | [apps/MaaRacingMaster.Shell/README.md](../apps/MaaRacingMaster.Shell/README.md) 与其 [frontend/README.md](../apps/MaaRacingMaster.Shell/frontend/README.md) |
| MaaFW 用法 / 协议行为 | [MAAFW_GUIDE.md](MAAFW_GUIDE.md)；冲突时以官方文档为准 |
| 新活动模块 | [templates/plugin/](../templates/plugin/README.md) 样板起步 |
| 环境跑不起来 | [SELF_CHECK.md](SELF_CHECK.md) |

改动前的影响面自查与验证要求见 [`AGENTS.md`](../AGENTS.md)「改动影响自查」。

***

## 导航说明

- **文件级清单不在此维护**——目录是重构中最易失效的镜像。要找某个模块/类，用 `core/CODE_WIKI.md` 的类索引或直接 grep 代码。
- 运行期数据（自动生成，gitignore）不住仓库：`%APPDATA%/MaaRacingMaster/` 下 `config/`、`data/`、`logs/`、`framework/`、`debug/` 五目录，见 [core/CODE_WIKI.md](../maaracing_master/core/CODE_WIKI.md)。
