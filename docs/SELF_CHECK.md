# 可行性自检清单（SELF CHECK）

> 从零跑通 MaaRacingMaster 的**分步验收清单**：每步给你「怎么验、成功是什么样、失败怎么查」。
> 适合新环境部署、跨机器迁移、或运行异常时定位「是环境问题还是功能 bug」。
>
> **信源等级**：L3 —— 可作「环境与启动怎么验」的操作依据；判定标准以实际命令输出与测试为准（本文不复制其取值）。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../AGENTS.md)。

所有命令默认在**仓库根目录**（含 `README.md` 的那一层）执行。本文与 [README 快速开始](../README.md#快速开始) 配套。

---

## 0. 总览

| 层级 | 检查点 | 一句话目的 |
|------|--------|-----------|
| A. 系统 | 系统版本 / 权限 / 分辨率 | 前置是否满足 |
| B. Python | 版本 + 虚拟环境 + 全依赖 | 解释器与依赖是否就位 |
| C. 资源 | 模型 / 模板 / 配置文件 | 运行必需文件是否齐 |
| D. 逻辑 | 单元测试（pytest） | 核心算法是否通过回归 |
| E. 启动 | 导入冒烟 + GUI 快捷方式 / sidecar | 代码能否真正起来 |
| F. 运行期 | 窗口连接 / 前台 / OCR 心跳 | 跑起来后是否健康 |

**快速结论法**：先只跑 D（最快、最确定），过了基本可排除「纯逻辑缺陷」；再跑 E；最后 F 需真实游戏画面。

---

## A. 系统前置

| 检查 | 命令 | 预期 |
|------|------|------|
| 系统版本 | 右键我的电脑 → 属性 | Windows 10/11 64-bit |
| 管理员权限 | 任务管理器 → 账户 | 本账户在 Administrators 组 |
| 游戏窗口分辨率 | 游戏中查看设置 | 1280×720 |

> 截图基于窗口客户区，分辨率不符会导致坐标换算偏差 → 优先用 1280×720。

---

## B. Python 环境

### B1. Python 版本

```bash
python --version
```

预期：`Python 3.11.x`。版本下限由 `pyproject.toml` 的 `requires-python = ">=3.11"` 锁死，低于 3.11 时 `pip install` 会直接拒绝安装本项目。

> **多版本共存时 `python` 未必是 3.11**：Windows 上 PATH 里的 `python` 常指向另一个版本（本机即存在 3.9 与 3.11 并存）。用 `py -3.11 --version` 确认 3.11 可用，并按 B2 显式指定版本创建虚拟环境，否则 venv 会继承 PATH 上的旧版本。

### B2. 创建并激活虚拟环境

```bash
py -3.11 -m venv .venv
.venv\Scripts\activate
python --version   # 应显示 3.11.x（venv 内的 python 已是所选版本）
```

预期：命令行前缀出现 `(.venv)`，且 `.venv\Scripts\python.exe` 存在。

> **GUI 自动定位 venv**：前台 `MaaRacingMaster.Shell.exe` 会从自身基目录向上定位仓库根（`pyproject.toml`）并拼接 `.venv`，**不再硬编码本机路径**，仓库可 clone 到任意磁盘位置运行。若仓库根 `.venv` 缺失，GUI 后端可能连不上，可先用 D/E 自检确认逻辑层可用，详见 README 快速开始第 2 步的警告。

### B3. 安装依赖

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

预期：无 `ERROR`，末尾提示 requirements 已满足。抽查几项：

```bash
pip show maafw vgamepad onnxruntime-directml rapidocr numpy opencv-python
```

| 依赖 | 缺失时的现象 |
|------|--------------|
| `maafw` | 截图/窗口连接不可用 |
| `rapidocr` | 鉴宝金额识别失效 |
| `onnxruntime-directml` | YOLO 推理能力不可用（供含检测的插件使用） |
| `vgamepad` | 虚拟手柄能力不可用（供需要手柄的插件使用） |

> `ultralytics`/`torch` 仅训练用（`pip install -e .[train]`），不属于运行时依赖，无需在此验证。

---

## C. 资源文件

| 文件 | 路径 | 缺失后果 |
|------|------|---------|
| 模型权重 | 无（当前版本不随包分发模型） | — |

> 若后续启用含 YOLO 检测的插件，模型训练导出见 `tools/training/train.py`。

---

## D. 逻辑回归（最快、最确定）

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

预期：`==== 21 passed, 0 failed ====` 之类（以 `0 failed` 为准）。

- 只测纯逻辑模块（出价策略等），不拉入 maa/opencv 重依赖，秒级完成。
- 失败时若有断言信息，基本可判断是刻意的行为基线变化，请提交 Issue 并附失败用例。

---

## E. 启动

### E1. 导入冒烟（验证 sidecar 可 import）

```bash
.venv\Scripts\python.exe -c "import maaracing_master; print('OK', maaracing_master.__version__)"
```

预期：`OK <v版本号>`。若报 `ModuleNotFoundError`，说明包未能在仓库根解析（确认 cwd 是仓库根）。

### E2. 启动 GUI（推荐入口）

首次需先编译前台 shell（构建产物 `apps/MaaRacingMaster.Shell/bin/` 已 gitignore，需本地构建）：

```bash
dotnet build apps\MaaRacingMaster.Shell\MaaRacingMaster.Shell.csproj -c Debug
```

编译成功后，双击根目录 **`MaaRacingMaster.lnk`**（若存在，本机快捷方式，指向编译产物路径，`.gitignore` 排除），或在命令行：

```bash
start apps\MaaRacingMaster.Shell\bin\x64\Debug\net8.0-windows10.0.19041.0\win-x64\MaaRacingMaster.Shell.exe
```

预期：exe 自身 manifest `requireAdministrator` 自动触发 UAC 提权；GUI 左上角显示版本号，后端连接成功后「巅峰鉴宝」模块与阶段列表正常加载。

> 该快捷方式指向**本机**编译产物路径，仅本地使用（`.gitignore` 排除）；仓库迁移后需按上述命令重建。

### E3. 独立调试 sidecar（不经 GUI，可选）

```bash
.venv\Scripts\python.exe -u -m maaracing_master.core.sidecar
```

预期：进程保持运行等待 stdin。可另开终端发一行 JSONL 测试吞吐：

```bash
echo {"id":1,"method":"get_initial_state","params":{}}
```

（管道输入后应看到 JSONL response。）

---

## F. 运行期自诊

启动 GUI → 选择「巅峰鉴宝」→ 开始运行，观察：

| 现象 | 说明 / 判断 |
|------|-------------|
| 日志出现「窗口连接失败」 | 游戏未开 / 未前台 / 游戏窗口标题不符 → 确认游戏已到主界面 |
| 日志频繁「前台校验失败」 | 游戏窗口不在前台（安全策略不抢前台）→ 把游戏切到前台 |
| OCR 心跳正常、阶段正常流转 | 截图 + 识别链路健康 |
| 模块中途静默退出 | 参考 [鉴宝文档 CODE_WIKI](../maaracing_master/plugins/treasure/CODE_WIKI.md) 遗留问题与坑点 |

> 单帧异常已被主循环兜底（忽略并继续），只有连续多帧系统性问题才会终止——若持续异常，请收集 `Debug` 存盘截图 + 日志后提 Issue。

---

## 常见失败与处理

| 现象 | 可能原因 | 处理 |
|------|---------|------|
| `python -m venv` 报错 | Python 未安装 / 未加入 PATH / 是 Store 别名 | 安装 Python 3.11 并勾选「Add to PATH」 |
| `pip install` 网络失败 | 代理 / 镜像问题 | 换国内镜像 `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt` |
| pytest 报 `No module named pytest` | 未装 test 依赖 | `pip install "pytest>=7.0"` |
| GUI 版本号一直不出现 / 后端 unavailable | 仓库根 `.venv` 缺失或不在仓库根 | 见 B2 警告 | 
| 鉴宝一直「等待」、阶段不动 | 前台校验失败或窗口未就绪 | 切游戏到前台；确认分辨率 1280×720 |

---

## 自动化回归（CI 已接入）

本仓库在 GitHub Actions 上对 `push`（master / release）与 `pull_request` 自动跑 D 步单测（Python 3.11）；发布流程前置测试 gate——**打 tag 发布前必须单测通过**。本地无需重复配置，C 步的 pytest 命令与 CI 完全一致。