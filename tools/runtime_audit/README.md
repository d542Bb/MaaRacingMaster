# Runtime Closure Auditor (V0.1)

> **本文是什么**：Runtime Closure Auditor 的用法——安装、命令、输出分类与删除信心分级。
> **本文不是什么**：不是发行包体积账的结论（见 [docs/update_log.md](../../docs/update_log.md)）；不是发布流程（见 [skills/project-update/SKILL.md](../../skills/project-update/SKILL.md)）。
> **信源等级**：L2 —— 可作「怎么跑这个审计工具」的直接依据。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../AGENTS.md)。

自动分析发行目录中每个文件「为什么需要」，给出分类与删除信心，避免继续人工猜哪一刀该砍。

## 安装

```bash
.venv\Scripts\python.exe -m pip install pefile
```

## 用法

```bash
.venv\Scripts\python.exe tools/runtime_audit/audit.py \
    --exp build/exp6/MaaRacingAssistant-0.19.0-win-x64 \
    --out build/exp6/runtime-audit.json
```

参数：

- `--exp` 发行根目录（默认 `build/exp6/...`）

- `--out` 输出 JSON 路径（默认 `<exp>/runtime-audit.json`，同名 `.md` 一并生成）

- `--skip-trace` 跳过 Layer2（Python runtime trace）

输出：

- `<exp>/runtime-audit.json` — 全量结构化结果

- `<exp>/runtime-audit.md` — 分类汇总 + TOP 候选 + oracle 对照

## 四层分析

| Layer | 内容                 | 证据                                                                   |
| ----- | ------------------ | -------------------------------------------------------------------- |
| L1    | Python 静态 import 图 | AST 解析 `.py` 的 import / from / importlib.import\_module / __import__ |
| L2    | Python 运行时 trace   | 发行版 runtime python 执行 sidecar，快照 `sys.modules.__file__`              |
| L3    | Native PE 依赖图      | pefile 解析 `.exe/.dll/.pyd` 导入表（含延迟加载）                                |
| L4    | 真实 native 加载       | python311/解释器隐式 DLL + sys.modules 暴露的 native                         |

## 文件分类

`REQUIRED` / `RUNTIME-LOADED` / `STATIC-ONLY` / `DEV-ONLY` / `UNUSED-CANDIDATE` / `UNKNOWN`

> `STATIC-ONLY` 与 `UNKNOWN` 绝不等价于可删（存在懒加载/WinRT/反射/插件/配置驱动）。删除信心单独为 `HIGH/MEDIUM/LOW/UNKNOWN`。

## 已知限制（V0.1）

1. **cv2 类扩展 loader**（`__init__.py` 里 `import_module("cv2")` 加载 `.pyd`）：baseline trace 捕获不到 `.pyd` 直接路径，靠 KEEP 顶层包白名单兜底。
2. **app/.NET/WinUI** 一律按 `UNKNOWN` + dynamic 标记处理，不强判（WinRT activation / XAML reflection 无法纯静态判定）。
3. `STATIC-ONLY` 较大（36MB/1071 文件）是已知保守结果：baseline 只覆盖无 GUI 入口，未触发懒加载路径。
4. MaaAgentBinary 的 minicap/\*.so（ADB/Android 截图二进制）逐个列为 UNUSED/HIGH，应整包归因聚合。

