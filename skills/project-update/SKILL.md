---
name: "project-update"
description: "MaaRacingMaster 提交纪律与发版手册：commit 粒度、amend 边界、共享检出目录（并行会话）纪律，以及完整发版流程（清理临时文件 → 定版本号 → 更新 docs/update_log.md → 提交 → 沉淀 PR（压扁假冲突走集成提交）→ 打 tag 并 push 触发 CI/CD）。Invoke when the user says '发布', '更新项目', '发版', 'release', wants to prepare a new version, or whenever the task will touch the remote repository — push / push tag / push branch / open or merge PR / publish."
---

# 项目更新助手（MaaRacingMaster 发版流程）

为 MaaRacingMaster 项目执行发布前的完整准备流程。本文件反映**当前实际发布模式**
（2026-11 复核）：**双分支模型——日常 commit 落个人开发分支（命名自定），
稳定线（默认分支 master）只经沉淀 PR 收入，发版打 tag 触发 CI**。

## 权威定义与安装

- **本文件** **`skills/project-update/SKILL.md`** **= 团队发布流程的唯一权威定义**，随仓库入库，协作者可见。

- 各 harness 若以安装副本方式使用技能，副本放自己的安装目录（位置随工具的约定而定，**不构成团队约定**），发版前以本仓库权威文件核对一致后再执行。

> 不做 agent 侧自动回写：方向无判据（「最新版本」缺可判定标准），且会把本机上下文带进仓库。

***

## 提交纪律（任何会写远端历史的操作都适用）

本节是**操作手册**——口径细则在此，[AGENTS.md](../../AGENTS.md) 只保留红线声明。适用于**任何会写远端历史的操作**：push 分支、push 稳定线、打 tag、开 / 合 PR、发布，不限于发版。

### 分支模型（稳定线 / 日常线）

- **稳定线**（默认分支 `master`）只收**沉淀**：一个主题收口（实现+回归+验收齐了）才经 PR 合入，合并时 CI 自动全量测试——稳定线上每个点都可运行、可发布。稳定线受 **GitHub 规则集（`stable-master-pr-only`）保护**：直接 push 一律被远端拒绝，只允许 **squash 压扁合并**（每个主题恰好一个 commit，master 历史保持一行主题列表），**任何人（含仓库所有者）不可绕过**。2026-10-17 重构前的完整开发历史保留在 master 祖先里，此后的 master 历史每个点是一个收口主题。
- **日常线**：日常开发分支，**命名自定**（个人名/主题名皆可，本仓库实例为 `dev/d542Bb`）。进行中的工作、实验、细碎提交全部落这里，保留全部详细历史；多条日常线并存合法。日常线**允许 agent 直接推送**（云端智能体独立提交与审查，不需逐次确认），也**允许对已推送历史改写**（`rebase` / squash / force push，一律 `--force-with-lease`）——只限日常线自身，永不触碰稳定线；改写优先走独立 worktree 或远端操作，不掀共享检出里并行会话的工作树（见「共享检出目录」）。
- **共享检出目录（并行会话）下**：所有会话的 HEAD 停在同一份工作树的日常线上，日常提交照常直提；**禁止在共享检出里切稳定线**（切分支改写工作区文件会掀他人未提交内容）。需要动稳定线时走远端（push 日常线 → GitHub 开 PR），不经本地 checkout。
- **压扁的代价 = 假冲突**：稳定线每个点是压扁产物，与日常线的详细历史之间不再有可自动判定的合并基线——**上个 tag 之后首次沉淀的 PR 会在几十个文件上报冲突，而内容并不冲突**（稳定线树与日常线的上个发布点树是同一棵树）。判据与处置见 ④「沉淀」的冲突分流。
- 发版顺序随之固定：**先沉淀（PR 合入稳定线）→ 再在稳定线上打 tag**。tag 必须打在稳定线含全部改动的那份上（见发版节）。

### commit 粒度

- **一个 commit 是一个有意义的历史节点**：能独立解释、能独立回滚即可；开发期允许细粒度提交以便回溯、Review 与实验。
- **交付面**（tag、`docs/update_log.md` 小节、Release 正文）才要求语义完整。
- **稳定线 push 是历史整理的不可逆分界**：进稳定线（沉淀 PR）的内容必须语义完整，**归并发生在开 PR 之前（先整理、后沉淀）**；稳定线合并由规则集锁定为压扁，历史天然一个主题一个 commit。
- **稳定线禁止对已推送历史 rebase / squash / force push**（AGENTS.md 红线），修正一律新 commit 前进。
- **日常线不受上述限制**：允许 agent 直接推送；允许对已推送的日常线历史 rebase / squash / force push（`--force-with-lease`），改写只落日常线，不触碰稳定线。

### 退役、改名、目录瘦身类提交

提交前全仓 grep 旧名——模块名、节点前缀、章节号、目录名都算。旧名残留会以幽灵登记（日志站点、注册表条目、文档指针）的形式继续被机检当真，或让指向虚空的指针静默通过。grep 范围含生产代码、测试、工具与 L1~L3 文档；L4 历史记录（ADR、`update_log.md`）按定义保留旧名。


### amend 的边界

仅当**三条同时成立**才可 `git commit --amend` 并入上一提交：① 修改仍属于该提交同一逻辑任务的补充 / 修正；② 该提交**未推送**；③ **无并行会话**。其余情形一律新 commit 前进。

### 共享检出目录（并行会话）纪律

多会话共用同一工作区时（AGENTS.md 红线）：

1. **禁止一切改写历史或丢弃未提交内容的操作**——`amend` / `rebase` / `reset --hard`。共享索引与工作树下，改史会掀掉他人未提交内容、或使其提交脱链。上面「未推送可自由归并」的许可在并行期一并收回。
2. **提交一律 pathspec 直提**：`git commit -m "…" -- 路径`，不做「先 add 再裸 `git commit`」——裸提交与 amend 都吃整个索引，会卷入他人暂存内容。
3. **提交前核对暂存区**：`git diff --cached --name-only` 只应含自己的文件。
4. **需要真隔离用 `git worktree`** 独立工作树——只切分支不换工作树隔离不了暂存区，切分支本身还会改写工作区文件。

### 对外动作先取得确认

稳定线写入（开 / 合沉淀 PR）、打 tag、发布、改远端配置一律先向用户确认（AGENTS.md 红线）；发版动作不可逆。**已授权豁免**：日常线（`dev/**`）push 与历史改写 agent 可直接执行、不需逐次确认（AGENTS.md「对外可见的动作」豁免条款）；本次发版的 `release/**` 集成分支按同一条对待——它只由集成提交生成、无并行写者，`--force-with-lease` 重建不需逐次确认。**逐次确认不可让的三件**：`--admin` 绕过规则集（批准审查 / 必需检查）、tag 推送、稳定线历史改写。

***

## 使用流程

当用户调用此 skill 时，先向用户确认意图（可多选）：

| 操作                   | 说明                                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------------- |
| ① 清理临时文件             | 删除 `__pycache__`、`*.pyc`、`*.egg-info`、`build/`、`dist/`、`.pytest_cache/` 等（**不含** `debug/` 调试截图） |
| ② 确定版本号              | 按「当前 dev 系列顺延 / minor 升级开新系列」规则计算，**用户指定优先**                                                    |
| ③ 更新文档               | 更新 `docs/update_log.md` 添加新版本条目（README 如需一并更新）                                                  |
| ④ 提交改动               | commit 到个人开发分支（命名自定）；稳定线只经沉淀 PR 收入，见「分支模型」                      |
| ⑤ 更新版本标记 + 打 Tag 并推送 | 更新 `docs/latest_release.json`，创建 `vX.Y.Z[-dev.N]` tag 并 push，触发 CI/CD Release                   |
| ⑥ 本地打包验证（可选）         | 跑 `scripts/release/assemble.ps1` 生成 zip+sha256 本地校验                                             |
| ⑦ 全部执行               | 按 ①→②→③→④→⑤ 顺序（⑥ 可选插入 ⑤ 前）                                                                      |

**顺序依赖：** ③ 需先定 ②（日志条目含版本号）；④ 应包含 ②③ 的改动；⑤ 必须晚于 ④
（tag 打在含全部改动的 master 上）。

***

## 操作详解

### ① 清理临时文件

1. 删除所有 `__pycache__` 目录（递归）
2. 删除所有 `*.pyc` 文件
3. 删除 `*.egg-info/`、`build/`、`dist/`、`.pytest_cache/`（若存在）

**注意：** 不删除 `logs/`、`dataset/`、`assets/`、`config/`、`.venv/`、`debug/`。
`debug/` 下的调试截图（ROI 框选定位用）有保留价值，一律不清理。

### ② 确定版本号

**当前实际模式（2026-11 复核，git tag 历史）：**

- 每次 minor 升级后开新 dev 系列：`v0.X.0-dev.1`、`-dev.2` …（如 `v0.15.0-dev.1` → `v0.15.0-dev.8`）

- dev 系列成熟后（用户拍板）才发正式版 tag（如 `v0.13.0`）

- 因此：**dev 序号在当前 minor 系列内检测顺延**，不是「基于最近正式版 tag 算」

| 场景                 | 版本格式                    | 示例                                      |
| ------------------ | ----------------------- | --------------------------------------- |
| 新功能（minor 升级，开新系列） | `v0.(x+1).0-dev.1`      | `v0.15.0-dev.8` → 新功能 → `v0.16.0-dev.1` |
| 同系列 Bug 修复         | `v0.x.y-dev.(N+1)`      | `v0.15.0-dev.8` → 修复 → `v0.15.0-dev.9`  |
| 系列成熟发正式版（用户拍板）     | `v0.x.y`                | `v0.15.0`（SemVer 正式版）                   |
| 正式版后 minor 升级      | 开新系列 `v0.(x+1).0-dev.1` | `v0.13.0` → `v0.14.0-dev.1`             |

**规则：**

1. `git tag --sort=-v:refname` 列出全部 tag（不只看第一条）
2. 判断本次变更类型（minor / patch / 用户指定），按上表计算候选版本号
3. **用户指定版本号优先**：如用户已明确（如 `v0.16.0-dev1`），以用户为准
4. 格式建议 `v0.x.y-dev.N`（pre-release 段用点，与现有 `v0.15.0-dev.8` 及 CI SemVer 校验一致）
5. 用正则校验 SemVer：`^v\d+\.\d+\.\d+(-[\w.]+)?$`
6. 输出版本号让用户确认后再继续

### ③ 更新文档

在 `docs/update_log.md` 顶部（最新 `## YYYY-MM-DD` 之后）插入新版本小节。**这是面向玩家的更新说明，不是开发日志**——收录判据与禁止项见 [`docs/README.md`](../../docs/README.md)「update_log 收录判据」。

```markdown
## YYYY-MM-DD

### vX.Y.Z[-dev.N] 一句话主题 🏷️

- **新增：** …
- **改进：** …
- **修复：** …
- **移除：** …
```

- 每条都过一遍判据：**玩家能看到什么不同？** 答不出的删掉；没有内容的分类不写
- 变更来源是 `git log <上个 tag>..HEAD`——过程细节已在 commit message，不重复搬进本文
- 让用户确认或编辑日志内容再写入

- 若功能列表/项目结构等涉及 README.md，询问用户是否一并更新

- 若本次是重写 skill/规范变更，同步更新 `AGENTS.md`「版本交付与协作」小节

### ④ 提交改动

**当前实际模式（2026-10 起）：日常 commit 落个人开发分支**（命名自定，本仓库实例 `dev/d542Bb`），稳定线只经沉淀 PR 收入。

1. `git status --short` 查看未提交改动；`git add <具体文件>`（按文件名添加，避免误入敏感/大文件）
2. `git commit -m "标题" -m "正文"`（PowerShell 不支持 heredoc，用 `-m` 参数）
3. push 日常分支：`git push origin <日常分支>`——push 只同步/备份，不触发 CI（test 镜像只盯稳定线）

**沉淀（稳定线收入，用户认定主题收口时）：**

1. **先跑全仓门禁再开 PR**：`.venv` 下 `ruff check .` → `pyright maaracing_master` → 全量 `pytest -q`。CONTRIBUTING 把这三项留在 CI 侧（提交路径只跑触碰面），但沉淀 PR 的红挡在稳定线门口，而它们是**串行露出**的：ruff 不绿看不到 pyright，pyright 不绿看不到测试。
2. push 日常分支后 `gh pr create --base master --head <日常分支>`（body 多行先写临时文件用 `--body-file`）。
3. **冲突分流**（`gh pr view <N> --json mergeable`）：
   - **假冲突**（压扁的结构性后果，见「分支模型」）——先证两件事：稳定线 HEAD 的树 == 日常线上个发布点的树（`git diff <上个发布点> <稳定线 HEAD>` 为空），且那个发布点是日常线 HEAD 的直系祖先。成立就走下面的**集成提交**；正确答案全是「取日常线那份」，逐文件手工裁决只增加出错面。
   - **真冲突**（两边内容确实分叉）——交用户裁决，agent 不自动解决。
   - 冲突态的 PR 建不出 merge ref，`pull_request` 流水线**整个跳过**——「Test 一条都没跑」先查 PR 是否 CONFLICTING，再怀疑 workflow 配置。
4. 合并走 `gh pr merge <N> --squash --subject ... --body ...`——合并进稳定线即触发 CI 全量测试；规则集只允许压扁合并（`--merge` / `--rebase` 会被远端拒绝），master 上一个主题恰好一个 commit。规则集另要求 1 次批准审查，**作者无法自批**：检查全绿仍 BLOCKED 时报告用户，取得明确指示后才 `--admin` 绕过。
5. **沉淀是稳定线写入的唯一通道**：不在共享检出里 checkout 稳定线本地操作（见「分支模型」）。

**集成提交（假冲突的处置）**：把日常线当前的树挂成一个提交，父是稳定线 HEAD——发布集成用一份干净的共同历史，日常线的详细提交留在原分支不动。

```bash
git fetch origin master
P=$(git rev-parse origin/master); T=$(git rev-parse HEAD^{tree})
C=$(git commit-tree "$T" -p "$P" -m "release: <版本> <一句话主题>")
git branch -f release/<版本> "$C" && git push --force-with-lease origin release/<版本>
```

四项验证缺一不推：① 推前重取 `origin/master`，父仍等于刚记录的 `$P`（稳定线在你核树时又动了就整轮重来）；② 新提交的树 == 日常线 HEAD 树；③ `git diff --quiet $C HEAD` 为空；④ `git diff --stat $P..$C | tail -1` 的文件数与增删行数即 PR 净增量，写进 PR 正文前用 `git diff --name-status` 点一遍 A / D / M / R 的构成，重命名与删除要各自归到具体提交。日常线此后每有新提交，重跑这四步重建集成提交（`--force-with-lease` 只落 `release/**` 自身）。

PR 正文承担集成方式的说明：为什么只有一个提交、核过的事实、门禁红的处置——读者没有本次会话的上下文，凭这些才能复核。

`release/**` 也在 test.yml 的 push 分支表里，所以推上去就自带一轮全量 CI。PR 流水线与分支流水线各跑一次，**同一提交两边结论可以不同**（计时类断言吃不住负载差），红的那个先在本地复现再定性——是代码脆还是机器抖，决定它进不进本次发版。

### ⑤ 更新版本标记 + 打 Tag 并推送（触发 CI/CD）

1. **更新** **`docs/latest_release.json`**（程序内「检查更新」的唯一信源）：`tag`、`version`、`published_at` 三项指向本次版本，`download_url` 保持不变。CI **不回写此文件**（master 受分支保护），漏写会造成「Release 页面已有新版，但玩家软件里永远收不到更新提示」的错位。该改动必须作为 commit 落在 **tag 之前**——tag 落点那份代码要同时含 changelog 小节与此标记文件（Release 正文由 `extract_changelog.py` 从 tag 那份 `docs/update_log.md` 抽取，改文案必须与 tag 落点一起前移）。
2. 确认工作区干净（`git status --porcelain` 无输出）；确认待发内容**已沉淀进稳定线**（PR 已合入 master、CI 绿），tag 打在稳定线上。合入后核一次 `git rev-parse origin/master^{tree}` 等于日常线 HEAD 的树——走过集成提交的话，这一步抓的是「合进去的不是你以为的那份」。
3. 本地创建 tag：`git tag v0.x.y-dev.N`（0.x 阶段通常为 pre-release）或 `git tag vX.Y.Z`（正式版）。本仓库自 v0.12 起统一用**轻量 tag**（`git cat-file -t refs/tags/<上一个>` 可验类型），沿用即可
4. push tag：`git push origin <tag>`（tag 在稳定线上——若本地 HEAD 停在开发分支，先 `git fetch origin master` 后对远端稳定线打 tag：`git tag <tag> origin/master`，避免在共享检出里切分支）
5. **push 前必须向用户确认**（发版动作不可逆）
6. 告知用户：push tag 触发 GitHub Actions（`.github/workflows/release.yml`），流水线 = `test`（pytest + check\_truth 发布前 gate）→ `release`（创建 GitHub Release + sdist/wheel + 校验 setuptools-scm 版本一致）→ `win-build`（assemble.ps1 组装解压即用 win-x64 zip/7z + sha256，并生成可选模型包，一并补传 Release）→ `cnb-release`（同步 zip/7z/模型包到 CNB）
7. **tag 触发的工作流与 master push 的工作流 checkout 语义不同**：前者完整历史（拿得到 tag），后者 `--no-tags` 浅克隆。因此「master 的 Test 红、Release gate 绿」可以同时成立，排查时先分清是哪条流水线

### ⑥ 本地打包验证（可选）

```powershell
powershell -File scripts\release\assemble.ps1 -Version <版本号> -RepoRoot <仓库根> -OutRoot scripts\release
# 或复用已验证 runtime 加速：-SourceRuntimeDir <先前构建的 runtime\python 目录>
```

- 输出 `MaaRacingMaster-<版本>-win-x64.zip` + `.sha256`

- 自检：embedded Python import `maa/onnxruntime/cv2/numpy/rapidocr/windows_capture` + `maaracing_master` 打印 `__version__`

- 注意 `-HostPython` 必须为 3.11（cp311）以匹配 embedded runtime；`-SkipPublish` 仅当 GUI 源指纹一致时复用缓存

- 默认包不含深度/线观测权重（约 82MB），由可选模型包分发；要出离线全量包加 `-WithModels`

- 本步需要 MSVC（编译 native Launcher）。没有本机 MSVC 时改用云端 `package-test.yml` 验证同一脚本（Windows runner + `mlocati/setup-msvc`），产物只上 artifact、不发布

***

### 不发版模式（不动程序代码 → 不 tag，方案 C）

**适用：** 本次改动只涉及文档 / CI / 元数据，**不触碰运行时程序代码**——此时不需要新的安装包，**不打 tag、不触发 release**；改动先落日常分支，随下次沉淀 PR 进稳定线（稳定线受规则集保护，不能直推）。

**判定"是否动程序代码"：** 用 `git diff --name-only` 检查本次即将提交的文件，凡落入下列**程序路径**即视为「需要出包」：

- `maaracing_master/**`（Python 运行时代码）

- `apps/**`（C# / XAML GUI）

- `maaracing_master/plugins/*/resources/models/**`（模型权重）、`maaracing_master/**/resources/**`（识别资源与模板）

- `config/**`、`requirements*.txt`、`scripts/release/requirements-runtime-lock.txt`

**反之（纯文档 / CI / 元数据，不发版）：** `*.md`、`docs/**`、`.github/**`、`pyproject.toml`（仅元数据 / extra 调整，不影响 win 包 runtime 的 lock 依赖）。README / update\_log / CONTRIBUTING / workflow 改动均属此类。

**不发版模式流程：**

1. commit 到个人开发分支
2. **不写 update_log**——纯文档 / CI / 元数据改动玩家感知不到，按收录判据一律不进（过程已在 commit message）
3. `git push origin <日常分支>`（若需同步远程；push 日常分支不触发 CI）
4. **不打 tag、不触发 release**；文档类改动随下次沉淀 PR 自然进稳定线

**技术背景：** GitHub Actions 的 `push` tag 事件**不支持** **`paths`** **/** **`paths-ignore`** **过滤**，无法在 trigger 声明层区分「纯文档 tag」与「程序 tag」，故本项目用\*\*流程约定（方案 C）\*\*而非 workflow 自动检测——纯文档改动一律不 tag，只有程序代码有实质改动、需要新产物时才走完整发版（① 清理 → ② 定版本 → ③ 日志 → ④ 提交 → ⑤ tag → push）。

***

## 版本管理规范（当前实际）

版本规则以本 skill 为准，与 [CONTRIBUTING.md](../../CONTRIBUTING.md)（仓库根，自本文件所在目录上溯两级）「版本与发布」小节同源于 git tag 事实：

| 变动                 | 版本格式                            | 示例                                |
| ------------------ | ------------------------------- | --------------------------------- |
| minor 升级（新功能）      | `v0.(x+1).0-dev.1`（开新系列，dev 顺延） | `v0.15.0-dev.8` → `v0.16.0-dev.1` |
| patch（同系列修复）       | `v0.x.y-dev.(N+1)`              | `v0.15.0-dev.8` → `v0.15.0-dev.9` |
| 正式版（dev 系列成熟，用户拍板） | `v0.x.y`                        | `v0.15.0`                         |

- **Git Tag 是唯一信源**：禁止手动改源码版本号，`setuptools-scm` 从 tag 自动推导（写入 `maaracing_master/_version.py`）

- **发布模式**：日常 commit 落个人开发分支（命名自定）→ 主题收口经 PR 沉淀进稳定线 → 在稳定线上打 tag → push tag 触发 CI/CD（见「分支模型」）

- tag 触发 CI 时 `test` 会先跑 pytest，测试失败不发布

***

## 参考文件

- **版本号来源：** Git Tag（`git tag --sort=-v:refname`）

- **版本号自动生成：** `pyproject.toml` → `setuptools-scm` → `maaracing_master/_version.py`

- **更新说明：** `docs/update_log.md`（面向玩家，Release 正文信源）

- **发布 CI/CD：** `.github/workflows/release.yml`（push tag `v*` 触发：test → release → win-build → cnb-release）

- **版本标记：** `docs/latest_release.json`（程序「检测更新」信源，CNB raw 优先读取；由发版提交携带，CI 不回写）

- **CNB 镜像：** `.github/workflows/mirror-to-cnb.yml`（master push 自动 git 镜像同步到 cnb.cool）

- **本地打包脚本：** `scripts/release/assemble.ps1`

- **云端打包验证：** `.github/workflows/package-test.yml`（push `dev/**-pkgtest` 或手动触发，只上传 artifact，不创建 Release；版本号读 `docs/latest_release.json`）

- **项目规范：** `AGENTS.md`

- **README：** `README.md`

***

## PowerShell 兼容性注意事项

本项目的运行环境是 Windows PowerShell，与类 Unix shell 有显著差异，执行命令时特别注意：

| 场景                   | 不要用                   | 改用                                                      |
| -------------------- | --------------------- | ------------------------------------------------------- |
| git commit 多行消息      | `cat <<'EOF'` heredoc | `git commit -m "标题" -m "正文"`                            |
| gh pr create 多行 body | `cat <<'EOF'`         | 先写入临时文件，用 `--body-file` 传入                              |
| 管道取前 N 条             | `head -N`             | `Select-Object -First N`                                |
| 管道取后 N 条             | `tail -N`             | `Select-Object -Last N`                                 |
| 管道计数                 | `wc -l`               | `Measure-Object \| Select-Object -ExpandProperty Count` |
| 环境变量引用               | `$VAR` 或 `${VAR}`     | `$env:VAR`                                              |
| 布尔值                  | `true` / `false`      | `$true` / `$false`                                      |

**禁止项（安全）：**

- 不写死代理到 git config（`git config http.proxy ...`）——按需临时环境变量 `$env:HTTP_PROXY`/`$env:HTTPS_PROXY` 或提示用户自行配置

- 不用 `git reset --hard` / `git clean -f` 等破坏性命令；同步 master 用 `git pull --rebase origin master` 或仅在用户明确要求时操作

- 稳定线写入（push / 开合 PR）、打 tag、merge、发布前必须向用户确认；日常线（`dev/**`）push 与历史改写已授权，不需逐次确认

