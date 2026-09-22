# 贡献指南（Contributing Guide）

> **本文是什么**：面向贡献者的流程指南——怎么找任务、开发约定、提交与 PR 流程、发布规则。
> **本文不是什么**：不是架构与实现说明（见 [docs/CODE_WIKI.md](docs/CODE_WIKI.md)）；不是 AI 协作守则（见 [AGENTS.md](AGENTS.md)）。
> **信源等级**：L3（导航与摘要）—— 可作「怎么参与」的依据；架构与规则细节一律回到其 home。

欢迎来到 **MaaRacingMaster** 社区！本项目是一个**模块化游戏自动化平台**，当前提供 **巅峰鉴宝**，采用可扩展插件框架，新活动以独立插件目录形式贡献。

在贡献之前，请先阅读 [README.md](README.md) 与 [docs/CODE_WIKI.md](docs/CODE_WIKI.md)（项目地图与导航）；协作守则与信源路由见 [AGENTS.md](AGENTS.md)。

> **⚠️ 合规红线（必须遵守）**
> 本项目为游戏自动化技术研究与学习项目，**严禁**用于代练、外挂、作弊、刷榜或任何影响游戏公平与服务器排名的用途。所有贡献必须服务于"技术学习与个人效率工具"这一定位。详见 [README 免责声明](README.md#免责与合规声明)。

---

## 我能帮上什么忙？

| 方向 | 说明 | 适合谁 |
|------|------|--------|
| 🐛 报 Bug | 使用中遇到异常，先查 [已知问题](https://github.com/d542Bb/MaaRacingMaster/issues) 是否已有报告 | 所有用户 |
| 💡 提需求 | 描述你想要的鉴宝 / 平台能力，说明使用场景 | 所有用户 |
| 🧹 修 Bug | 认领 Issue，修复后提 PR | 初级贡献者 |
| 🧩 新模块 | 复用能力接口（capability）开发新活动模块 | 进阶贡献者 |
| 📝 文档 | 完善 README / CODE_WIKI / 快速开始指南 | 所有贡献者 |
| 🎨 素材 | 优化模板匹配图片、UI 界面、调试工具 | 设计师 / 前端 |

> 找不到方向？看 Issue 里带 `good first issue` 标签的任务，或直接开 Issue 描述你想做的事。

---

## 环境准备

### 运行环境

- **Windows 10/11 64-bit**
- **Python 3.11**
- 游戏窗口分辨率 **1280×720**
- 管理员权限（窗口截图必需）

### 开发环境搭建

```bash
git clone https://github.com/d542Bb/MaaRacingMaster.git
cd MaaRacingMaster
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

- 启动 GUI：运行编译产物 `apps\MaaRacingMaster.Shell\bin\x64\Debug\net8.0-windows10.0.19041.0\win-x64\MaaRacingMaster.Shell.exe`（exe 自身 manifest 自动 UAC 提权）
- 独立调试 sidecar（不经 GUI）：`.venv\Scripts\python.exe -u -m maaracing_master.core.sidecar`
- 调试工具位于 `tools/`（训练 / 分析 / 调试分类）

---

## 架构速览（改代码前必读）

平台核心原则：**控制依赖传播 + 治理资源所有权**。

- **模块经能力接口（capability）接触宿主**：模块只能通过 `capture` / `gamepad` / `debug_renderer` 等窄接口办事，**无法获得高权限宿主对象**。
- **资源所有权统一治理**：虚拟手柄用租约（`with ctx.gamepad.acquire()`）表达借还；渲染器由 `ActivityContext` 的 `ExitStack` 托管生命周期，退出（含异常）自动释放。
- **克制不滥用抽象**：高权限对象必须隔离、有生命周期的资源必须治理；稳定纯函数直接依赖即可。

关键文件位置：

关键文件位置（完整导航见 [docs/CODE_WIKI.md](docs/CODE_WIKI.md) 的「我要改 X，应该去哪里」）：

| 文件 | 职责 |
|------|------|
| `maaracing_master/core/controller.py` | 主控编排（生命周期 + 能力门面 `ActivityContext`） |
| `maaracing_master/core/capabilities.py` | 能力 Protocol + 最薄 adapter |
| `maaracing_master/core/base.py` | `ActivityContext` / `ActivityModule` 基类 |
| `maaracing_master/core/registry.py` | 插件注册表 |
| `maaracing_master/plugins/<id>/` | 活动插件（自包含目录，各含 `manifest.py` + `resources`） |
| `maaracing_master/core/gamepad_cursor.py` | 光标导航引擎 |
| `apps/MaaRacingMaster.Shell/` | WinUI 3 图形界面（Python sidecar 承载业务） |

**改代码前请先读对应对象的知识文档**：平台核心层见 [core/CODE_WIKI.md](maaracing_master/core/CODE_WIKI.md)，活动域见 `plugins/<id>/CODE_WIKI.md`，MaaFW 协议与红线见 [docs/MAAFW_GUIDE.md](docs/MAAFW_GUIDE.md)。

---

## 贡献流程

### 1. 找任务 / 开 Issue

- 先在 [Issues](https://github.com/d542Bb/MaaRacingMaster/issues) 搜索是否已有相关讨论，避免重复。
- 报 Bug 请使用 **Bug 模板**，尽量包含：复现步骤、期望行为、实际行为、日志 / 截图、环境信息（Python 版本、系统版本、分辨率）。
- 提需求请使用 **Feature 模板**，说明使用场景与期望效果。

**维护者台账（`label: self-todo`）：** 开发方向与未定性疑点记入常驻台账 issue——一行一条，只写「一句话 + 涉及位置」。满足任一条件即升级为独立 issue 并从台账删行：① 需跨会话追踪或与 commit 关联（`closes #N`）；② 需他人复现或讨论；③ 已进入实施、有独立验收面。已定性且另有归宿的直接迁走，不留副本。

### 2. 开发

```bash
# 从最新 master 拉分支（功能分支 / 修复分支）
git checkout -b feat/your-feature origin/master
# 或 git checkout -b fix/your-bugfix origin/master
```

**开发约定：**

- 新增模块必须走 `core/registry.py` 的插件发现（扫 `plugins/*/manifest.py`）+ 能力接口，不直接引高权限宿主对象。
- 涉及活动流程逻辑，先阅读对应域文档（鉴宝见 [plugins/treasure/CODE_WIKI.md](maaracing_master/plugins/treasure/CODE_WIKI.md)，平台核心层见 [core/CODE_WIKI.md](maaracing_master/core/CODE_WIKI.md)）。
- 遵守 [AGENTS.md](AGENTS.md) 的协作守则（面向用户的输出、代码注释与交付文档使用简体中文）；代码风格为 4 空格缩进。

### 3. 提交与推送

```bash
git add <具体文件>
git commit -m "feat: 简述改动内容"
git push -u origin feat/your-feature
```

提交信息建议使用约定式提交：`feat:` / `fix:` / `refactor:` / `docs:` / `chore:` 等前缀。

**提交前跑什么（跑触碰面，不跑全仓）：**

- 改动相关的 `pytest` + 改动文件的 `ruff`；
- 动到真源（`plugins/*/resources/` 的图与锚点）时加跑 `python tools/navkit/check_truth.py`；
- 动到 `tools/experiments/`、正式文档中的实验/plan 指针、或卫生基线时加跑 `python tools/check_repo_hygiene.py`（AGENTS 红线 6；基线只减不增，退役四步见 [tools/experiments/README.md](tools/experiments/README.md)）；
- 改到**跨模块共享契约**（core 能力接口、RPC 白名单、分层边）时建议自愿加跑全仓——影响面跨模块，早发现比等 CI 划算。
- 新增了抽象（core 目录、共用 pipeline、通用协议、新通路 / 新页面）时，先答一句「**第二个使用者是谁、今天是否真实存在**」——判据见 [AGENTS.md](AGENTS.md)「Git 与操作纪律」中「抽公共层」那一条；答不出就不抽。

全仓 `ruff` / `pyright` / 全量 `pytest` **不在提交路径上**：CI（`test.yml`，push 与 PR 触发）与发版（`release.yml` 发布前 gate）已经是全仓门禁，本地重跑一遍只是把等待时间翻倍。

### 4. 提 PR

- 使用 **PR 模板**，说明改动内容、动机、验证方式。
- 请在描述中**勾选自测项**（至少运行通过 / 无回归）。
- 关联对应 Issue：`Closes #123`。

### 5. Review 与合并

- 维护者会尽快 review；如果 PR 较大，建议先开 Issue 讨论设计再实现。
- 合并采用 **Squash merge** 保持 master 历史整洁。

---

## 版本与发布

- 严格遵循 **SemVer 2.0.0**（`主版本.次版本.修订号`）。
- **Git Tag 是唯一信源**：`setuptools-scm` 从 tag 自动推导版本号，**禁止手动改源码版本号**。
- 0.x 开发阶段 tag 为 pre-release `v0.x.y-dev.N`。
- 发布流程由维护者通过 `project-update` skill 执行（清理 → 版本号 → 更新日志 → PR → tag → CI/CD 自动建 Release）。

---

## 行为准则

参与本项目即表示同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)：互相尊重、聚焦技术、不对游戏公平性做任何破坏。

---

## 感谢

每一份 Issue、每一个 PR 都是对项目的支持。技术交流、踩坑分享、架构讨论同样欢迎——这正是这个项目的初衷。🎉
