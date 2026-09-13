***

name: "project-update"
description: "Performs MaaRacingMaster project release preparations: clean temp files, determine next dev version, update docs/update\_log.md, commit to master, create git tag and push to trigger CI/CD (GitHub Actions Release). Also supports optional local assemble.ps1 package verification. Invoke when user says '发布', '更新项目', '发版', 'release', or wants to prepare a new version."
--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# 项目更新助手（MaaRacingMaster 发版流程）

为 MaaRacingMaster 项目执行发布前的完整准备流程。本文件反映**当前实际发布模式**
（2026-08 复核）：**直接 commit master + 打 dev tag + push 触发 CI**。

## 权威定义与安装

- **本文件** **`skills/project-update/SKILL.md`** **= 团队发布流程的唯一权威定义**，随仓库入库，协作者可见。

- 各 harness 若以安装副本方式使用技能，副本放自己的安装目录（位置随工具的约定而定，**不构成团队约定**），发版前以本仓库权威文件核对一致后再执行。

> 不做 agent 侧自动回写：方向无判据（「最新版本」缺可判定标准），且会把本机上下文带进仓库。

***

## 使用流程

当用户调用此 skill 时，先向用户确认意图（可多选）：

| 操作                   | 说明                                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------------- |
| ① 清理临时文件             | 删除 `__pycache__`、`*.pyc`、`*.egg-info`、`build/`、`dist/`、`.pytest_cache/` 等（**不含** `debug/` 调试截图） |
| ② 确定版本号              | 按「当前 dev 系列顺延 / minor 升级开新系列」规则计算，**用户指定优先**                                                    |
| ③ 更新文档               | 更新 `docs/update_log.md` 添加新版本条目（README 如需一并更新）                                                  |
| ④ 提交改动               | 直接 commit 到 master（当前实际模式）；仅当远程有 PR 保护时才走 `release/vX.Y.Z` 分支 + PR                              |
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

**当前实际模式（2026-08 复核，git tag 历史）：**

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

更新 `docs/update_log.md`（在文件顶部最新 `## YYYY-MM-DD` 之后插入新条目，参考已有风格）：

```markdown
## YYYY-MM-DD

### vX.Y.Z[-dev.N] 版本标题 🏷️
- **版本号：** `vX.Y.Z[-dev.N]`（基于 v上一版本）
- **变更内容：** 逐条列出本次发布的主要变更
- **其他说明：** 如有需要
```

- 让用户确认或编辑日志内容再写入

- 若功能列表/项目结构等涉及 README.md，询问用户是否一并更新

- 若本次是重写 skill/规范变更，同步更新 `AGENTS.md`「版本与交付」小节

### ④ 提交改动

**当前实际模式（v0.14/v0.15 起）：直接 commit 到 master 并 push**，不再走 PR。

1. `git status --short` 查看未提交改动；`git add <具体文件>`（按文件名添加，避免误入敏感/大文件）
2. `git commit -m "标题" -m "正文"`（PowerShell 不支持 heredoc，用 `-m` 参数）
3. `git push origin master`

**备选（仅当远程开启 PR 保护 / 用户要求）：**

- 基于最新 master 建 `release/vX.Y.Z` 分支 → commit → push → `gh pr create --base master --head release/vX.Y.Z`（body 多行先写临时文件用 `--body-file`）→ `gh pr merge <N> --squash --subject ... --body ...` → 清理远程/本地分支（`git push origin --delete` + `git branch -D` + `git remote prune origin`）

- 冲突提示用户手动解决，不自动解决

### ⑤ 更新版本标记 + 打 Tag 并推送（触发 CI/CD）

1. **更新** **`docs/latest_release.json`**（程序内「检查更新」的唯一信源）：`tag`、`version`、`published_at` 三项指向本次版本，`download_url` 保持不变。CI **不回写此文件**（master 受分支保护），漏写会造成「Release 页面已有新版，但玩家软件里永远收不到更新提示」的错位。该改动必须作为 commit 落在 **tag 之前**——tag 落点那份代码要同时含 changelog 小节与此标记文件（Release 正文由 `extract_changelog.py` 从 tag 那份 `docs/update_log.md` 抽取，改文案必须与 tag 落点一起前移）。
2. 确认工作区干净（`git status --porcelain` 无输出）；确认当前在 master 且已含全部改动
3. 本地创建 tag：`git tag v0.x.y-dev.N`（0.x 阶段通常为 pre-release）或 `git tag vX.Y.Z`（正式版）。本仓库自 v0.12 起统一用**轻量 tag**（`git cat-file -t refs/tags/<上一个>` 可验类型），沿用即可
4. push tag：`git push origin <tag>`
5. **push 前必须向用户确认**（发版动作不可逆）
6. 告知用户：push tag 触发 GitHub Actions（`.github/workflows/release.yml`），流水线 = `test`（pytest + check\_truth 发布前 gate）→ `release`（创建 GitHub Release + sdist/wheel + 校验 setuptools-scm 版本一致）→ `win-build`（assemble.ps1 组装解压即用 win-x64 zip/7z + sha256 补传 Release）→ `cnb-release`（同步到 CNB）
7. **tag 触发的工作流与 master push 的工作流 checkout 语义不同**：前者完整历史（拿得到 tag），后者 `--no-tags` 浅克隆。因此「master 的 Test 红、Release gate 绿」可以同时成立，排查时先分清是哪条流水线

### ⑥ 本地打包验证（可选）

```powershell
powershell -File scripts\release\assemble.ps1 -Version <版本号> -RepoRoot <仓库根> -OutRoot scripts\release
# 或复用已验证 runtime 加速：-SourceRuntimeDir <先前构建的 runtime\python 目录>
```

- 输出 `MaaRacingMaster-<版本>-win-x64.zip` + `.sha256`

- 自检：embedded Python import `maa/onnxruntime/cv2/numpy/rapidocr/windows_capture` + `maaracing_master` 打印 `__version__`

- 注意 `-HostPython` 必须为 3.11（cp311）以匹配 embedded runtime；`-SkipPublish` 仅当 GUI 源指纹一致时复用缓存

***

### 不发版模式（不动程序代码 → 不 tag，方案 C）

**适用：** 本次改动只涉及文档 / CI / 元数据，**不触碰运行时程序代码**——此时不需要新的安装包，**不打 tag、不触发 release**，只 commit + push master（push master 只会跑 `test.yml` 单测，不会触发 release 流水线）。

**判定"是否动程序代码"：** 用 `git diff --name-only` 检查本次即将提交的文件，凡落入下列**程序路径**即视为「需要出包」：

- `maaracing_master/**`（Python 运行时代码）

- `apps/**`（C# / XAML GUI）

- `assets/model/**`（模型权重）、`assets/resource/**`（模板资源）

- `config/**`、`requirements*.txt`、`scripts/release/requirements-runtime-lock.txt`

**反之（纯文档 / CI / 元数据，不发版）：** `*.md`、`docs/**`、`.github/**`、`pyproject.toml`（仅元数据 / extra 调整，不影响 win 包 runtime 的 lock 依赖）。README / update\_log / CONTRIBUTING / workflow 改动均属此类。

**不发版模式流程：**

1. commit 到 master
2. 在 `docs/update_log.md` 顶部补一条 **未发版变更** 条目（标题用「未发版变更：主题」，**不带版本号**，参考已有先例）
3. `git push origin master`（若需同步远程；只触发 test.yml 单测）
4. **不打 tag、不触发 release**

**技术背景：** GitHub Actions 的 `push` tag 事件**不支持** **`paths`** **/** **`paths-ignore`** **过滤**，无法在 trigger 声明层区分「纯文档 tag」与「程序 tag」，故本项目用\*\*流程约定（方案 C）\*\*而非 workflow 自动检测——纯文档改动一律不 tag，只有程序代码有实质改动、需要新产物时才走完整发版（① 清理 → ② 定版本 → ③ 日志 → ④ 提交 → ⑤ tag → push）。

***

## 版本管理规范（当前实际）

版本规则以本 skill 为准，与 [AGENTS.md](../../AGENTS.md)（仓库根，自本文件所在目录上溯两级）「版本与交付」小节同源于 git tag 事实：

| 变动                 | 版本格式                            | 示例                                |
| ------------------ | ------------------------------- | --------------------------------- |
| minor 升级（新功能）      | `v0.(x+1).0-dev.1`（开新系列，dev 顺延） | `v0.15.0-dev.8` → `v0.16.0-dev.1` |
| patch（同系列修复）       | `v0.x.y-dev.(N+1)`              | `v0.15.0-dev.8` → `v0.15.0-dev.9` |
| 正式版（dev 系列成熟，用户拍板） | `v0.x.y`                        | `v0.15.0`                         |

- **Git Tag 是唯一信源**：禁止手动改源码版本号，`setuptools-scm` 从 tag 自动推导（写入 `maaracing_master/_version.py`）

- **发布模式**：直接 commit master → 打 tag → push tag 触发 CI/CD（不再走 PR，除非远程有 PR 保护）

- tag 触发 CI 时 `test` 会先跑 pytest，测试失败不发布

***

## 参考文件

- **版本号来源：** Git Tag（`git tag --sort=-v:refname`）

- **版本号自动生成：** `pyproject.toml` → `setuptools-scm` → `maaracing_master/_version.py`

- **更新日志：** `docs/update_log.md`

- **发布 CI/CD：** `.github/workflows/release.yml`（push tag `v*` 触发：test → release → win-build → cnb-release）

- **版本标记：** `docs/latest_release.json`（程序「检测更新」信源，CNB raw 优先读取；由发版提交携带，CI 不回写）

- **CNB 镜像：** `.github/workflows/mirror-to-cnb.yml`（master push 自动 git 镜像同步到 cnb.cool）

- **本地打包脚本：** `scripts/release/assemble.ps1`

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

- push / 打 tag / merge 前必须向用户确认

