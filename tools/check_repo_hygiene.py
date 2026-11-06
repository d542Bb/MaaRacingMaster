"""仓库卫生检查：实验与计划产物的生命周期契约（AGENTS.md 红线 6 的机检）。

立法规约（2026-09-18 维护者裁定，四条决策全通过、第③项修正为"存量不批量盖 active 章"）：

  tools/experiments/ = **当前活跃实验区**，不是归档区。留痕 = 进 git 历史，不是目录永存。
  实验关闭走四步：结论迁 home → 仪器提升 → 拔引用 → 删目录。
  历史证据的指针语法 = commit（短 SHA 展示），正式文档不得引用具体实验路径或 plan 文件。

七条规则：
  R1 入口护栏：每个主题目录必须有 README.md 且含 `status: active` 行。
     （status 是给 agent 与检查器看的入口护栏，不是生命周期数据库——在树里即 active，
      退役即删除；不存在 "closed 但留在树里" 的状态。）
  R2 知识引用污染：正式知识文档（L0~L3：AGENTS / CONTRIBUTING / docs 顶层与 adr /
     各插件与 apps 的 md）不得出现 `tools/experiments/<具体主题>` 路径（尾斜杠可有可无）。
     类别指针（`tools/experiments/`、`<主题>` 占位、本区 README 与索引等区内文件）不算违规。
  R3 工作台反向引用：同范围文档不得引用 `docs/plan/` 下的具体文件（裸目录提及不算）。
  R4 运行依赖污染：生产代码 / 测试 / 脚本（*.py、apps 下 *.cs）不得把实验目录当
     import 路径、子进程命令或数据入口引用；`REPO / "tools" / "experiments" / …`
     这类分段拼出的路径同样算（去引号与空白后比对）。
  R5 死引用：任何文件引用 `tools/experiments/<主题>/` 而该主题目录已不存在 → 违规
     （退役后引用链必须已拔净，这是"删目录"一步的验收）。
  R6 工作台入口护栏：`docs/plan/*.md` 头部必须有三行——`> **状态**：` 取闭集
     {active, retiring}、`> **归属去向**：`、`> **退役条件**：`（均非空）。
     与 R1 同构：状态是入口护栏不是生命周期数据库——不存在 done / 已完成 / archived，
     plan 的"完成"就是删除；写了"已实施"却留在树里即本区唯一的病。
     另打印台账（未动天数，不影响退出码），让"没人删"持续可见而不是静默豁免。
  R7 隐私扫描（红线 4 的机检，2026-11-06 立规）：入库文件不得含本机绝对路径、
     用户名目录、会话 ID。模式刻意收窄防误报；本机不入库面（AGENTS.local.md、
     docs/plan/、archive/、build/、实验区 _out/ 与 weights/ 等）不扫。另支持本地
     词表 `tools/repo_privacy_terms.local.txt`（gitignore，每行一个正则）：只允许
     留在本机的词（逆向来源等）放那里——词表本身入库就违背了它的目的。

棘手机制：`tools/repo_hygiene_baseline.txt` 记录立规之日的存量违规（legacy，按触碰面
逐个分诊，见 tools/experiments/README.md）。基线**只减不增**——新违规直接红；
基线中已不再命中的条目会提示"可收缩"，随下一次触碰删除。

用法：
    python tools/check_repo_hygiene.py                 # 检查（CI 用）
    python tools/check_repo_hygiene.py --update-baseline   # 重写基线（仅立法/收缩时用）
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # 本脚本在 tools/ 直下

# 具体主题路径：tools/experiments/<name>；占位符（含 < >）与裸目录引用不匹配。
# 尾斜杠可有可无——`tools/experiments/foo` 与 `…/foo/` 指同一件事，只认前者的写法
# 会漏检（历史实例：插件 CODE_WIKI 引用主题名时没写尾斜杠，长期未被算违规）。
TOPIC_PATH = re.compile(r"tools/experiments/([\w.\-]+)")
PLAN_PATH = re.compile(r"docs/plan/[\w\-][\w\-./]*")
STATUS_LINE = re.compile(r"^\s*status:\s*active\b", re.I | re.M)

# R6：plan 头部三行（只在文件头部窗口内匹配，避免正文误命中）
PLAN_STATUS = re.compile(r"^>\s*\*\*状态\*\*\s*[:：]\s*(.+?)\s*$", re.M)
PLAN_HOME = re.compile(r"^>\s*\*\*归属去向\*\*\s*[:：]\s*(\S.*?)\s*$", re.M)
PLAN_RETIRE = re.compile(r"^>\s*\*\*退役条件\*\*\s*[:：]\s*(\S.*?)\s*$", re.M)
PLAN_STATUS_VALUES = {"active", "retiring"}
PLAN_HEAD_LINES = 60                          # 头部窗口：状态行必须写在开头
# 可选：实质推进日（台账算未动天数用；缺省回退文件 mtime，会因补头部等格式化动作失真）
PLAN_UPDATE = re.compile(r"^>\s*\*\*更新\*\*\s*[:：]\s*(\d{4})-(\d{1,2})-(\d{1,2})", re.M)
# 模板占位符：`<...>` 原样留着等于没填
PLAN_PLACEHOLDER = re.compile(r"^<.*>$")

FORMAL_GLOBS = ["AGENTS.md", "CONTRIBUTING.md", "docs/*.md", "docs/adr/*.md",
                "maaracing_master/**/*.md", "apps/**/*.md"]
# docs/README 自身要描述本规则——豁免；update_log 面向玩家、不引实验路径，同受引用纪律约束
FORMAL_EXCLUDE = {"docs/README.md"}
CODE_GLOBS = ["maaracing_master/**/*.py", "scripts/**/*.py", "tests/**/*.py",
              "apps/**/*.py", "apps/**/*.cs"]
# 检查器自己的测试在 tmp_path 里搭假树（`tmp_path / "tools" / "experiments" / …`），
# 那不是运行依赖，是测试素材；排除以免自指。
CODE_EXCLUDE = {"tests/test_repo_hygiene.py"}

# R7 隐私扫描（红线 4 机检）：只抓「无歧义的本机指纹」——盘符路径 / 用户名目录 /
# 会话 ID，模式收窄防误报。盘符路径放行系统标准安装位置（Windows / Program Files /
# PerfLogs / VS 安装根——人人的机器都长这样，不构成身份指纹），且根盘符后必须有
# 实质内容（根盘符后无内容不算）。红线 4 管的是「入库」，追踪面由 git 决定，这里按目录
# 近似：本机不入库面（AGENTS.local.md、docs/plan/、archive/、build/、实验区的
# _out/ 与 weights/ 与 _probe_data/、各 harness 目录）不扫，新增不入库目录要同步扩。
PRIVACY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("盘符绝对路径", re.compile(
        r"""\b[A-Za-z]:[\\/](?!(?:Windows|Program Files(?: \(x86\))?|PerfLogs|Microsoft Visual Studio)(?:[\\/]|$))"""
        r"""[^\\/\s"'<>|*?\[\]()][^\s"'<>|*?\[\]()]*""")),
    ("用户名目录", re.compile(r"\bUsers[\\/][\w.\- ]+")),
    ("会话ID", re.compile(r"\bsess_[0-9a-fA-F][0-9a-fA-F-]{15,}")),
]
PRIVACY_SUFFIXES = {".py", ".md", ".json", ".txt", ".cs", ".js", ".ts", ".html",
                    ".css", ".cmd", ".bat", ".ps1", ".yml", ".yaml", ".toml",
                    ".cfg", ".ini", ".xml", ".csv"}
PRIVACY_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "build",
                     "archive", "dataset", "training", "logs", "bin", "obj",
                     ".zcode", ".workbuddy-ai", ".trae", ".trae-cn", ".claude",
                     ".cursor", ".qoder", ".windsurf", ".codeium", ".gemini",
                     ".continue", ".junie", ".mimosa", ".idea", ".vscode"}
# 自指与本机不入库文件豁免：检查器测试在用例里写假路径（素材非依赖）；词表文件
# 本身含敏感词不能自扫；AGENTS.local.md 与 trae 配置分享是本机文件（gitignore）。
PRIVACY_EXCLUDE_FILES = {"tests/test_repo_hygiene.py", "AGENTS.local.md",
                         "tools/repo_privacy_terms.local.txt",
                         "docs/trae-dsh-config-share.md"}
PRIVACY_TERMS_FILE = "tools/repo_privacy_terms.local.txt"


class Checker:
    def __init__(self, root: Path):
        self.root = root
        self.exper = root / "tools" / "experiments"
        self.baseline_path = root / "tools" / "repo_hygiene_baseline.txt"

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def topics(self) -> set[str]:
        return {d.name for d in self.exper.iterdir() if d.is_dir()} if self.exper.is_dir() else set()

    def hits(self, text: str) -> list[str]:
        """文本里出现的实验主题名。

        指向**区内文件**的引用（本区 README、trace 读取器、索引 md）不是主题路径，
        按文件存在性排除——否则「类别指针不算违规」这条口径会被名字撞上。
        """
        out = []
        for m in TOPIC_PATH.findall(text):
            if "<" in m or ">" in m:
                continue
            if (self.exper / m).is_file():
                continue
            out.append(m)
        return out

    @staticmethod
    def code_refs_experiments(text: str) -> bool:
        """代码里是否把实验目录当 import 路径 / 子进程命令 / 数据入口。

        字面量可能被拆开写：`REPO / "tools" / "experiments" / "x"`——去掉引号与空白后
        路径片段重新连起来，字面匹配才看得见（历史实例：测试用分段拼路径加载实验区
        里的生成器，R4 一直是绿的）。`os.path.join("tools", "experiments")` 这类
        逗号拼接仍不在覆盖内，新增此类写法要顺手扩这里。
        """
        norm = re.sub(r"[\"'\s]", "", text)
        # 只认「目录 + 具体名字」：类别指针（裸 `tools/experiments/`、本区 README）
        # 不是依赖——R2 早就是这个口径，R4 与之对齐（历史误报：测试 docstring 里
        # 一句"识别率由 tools/experiments/ 的探针另行判定"被当成依赖）。
        return re.search(r"tools/experiments/[A-Za-z0-9_.\-]+", norm) is not None

    def formal_docs(self) -> list[Path]:
        out = []
        for g in FORMAL_GLOBS:
            for p in self.root.glob(g):
                r = self.rel(p)
                if p.is_file() and r not in FORMAL_EXCLUDE and "/plan/" not in r:
                    out.append(p)
        return sorted(set(out))

    def plan_docs(self) -> list[Path]:
        """工作台文件（gitignore 不入库，CI 上为空；本地跑才有意义）。"""
        d = self.root / "docs" / "plan"
        return sorted(p for p in d.glob("*.md") if p.is_file()) if d.is_dir() else []

    @staticmethod
    def head_text(p: Path) -> str:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         .splitlines()[:PLAN_HEAD_LINES])

    @staticmethod
    def status_value(raw: str) -> str:
        """`active（2026-10-28）` / `active | ...` 都取首段，便于手写。"""
        tok = re.split(r"[｜|（(\s，。；、,.;:：]", raw.strip(), maxsplit=1)[0]
        return tok.strip("*`_ ").lower()

    def plan_ledger(self) -> list[tuple[int, str, str]]:
        """台账：(未动天数, 状态, 路径)，按未动天数降序。只呈现，不判红。

        天数优先读头部 `**更新**`；缺失或非法则回退文件 mtime，并在状态后缀 `~` 标记，
        提醒这个数字会因补头部、改格式这类非推进动作而偏小。
        """
        rows = []
        for p in self.plan_docs():
            head = self.head_text(p)
            m = PLAN_STATUS.search(head)
            st = self.status_value(m.group(1)) if m else "未标"
            u = PLAN_UPDATE.search(head)
            days = None
            if u:
                try:
                    d = datetime.date(int(u.group(1)), int(u.group(2)), int(u.group(3)))
                    days = (datetime.date.today() - d).days
                except ValueError:
                    days = None
            if days is None:
                days = int((time.time() - p.stat().st_mtime) / 86400)
                st += "~"
            rows.append((days, st, self.rel(p)))
        return sorted(rows, key=lambda r: (-r[0], r[2]))

    def privacy_terms(self) -> list[tuple[int, re.Pattern[str]]]:
        """本地词表（gitignore，不入库）：每行一个正则，`#` 起头为注释。"""
        f = self.root / PRIVACY_TERMS_FILE
        if not f.exists():
            return []
        out = []
        for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append((i, re.compile(ln)))
        return out

    def _tracked_files(self) -> set[str] | None:
        """入库文件清单（git ls-files）；git 不可用（测试假树）返回 None。"""
        try:
            r = subprocess.run(["git", "ls-files", "-z"], cwd=self.root,
                               capture_output=True, timeout=60)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        return {s.decode("utf-8", "replace") for s in r.stdout.split(b"\0") if s}

    def privacy_files(self) -> list[Path]:
        """R7 扫描面：入库的文本文件。

        追踪面用 `git ls-files` 精确圈定——红线 4 管的是「入库」，未入库的本机
        刮擦产物（_probe_data/_probe_scripts、obj/bin、AGENTS.local.md 等）天然
        出局。git 不可用时回退全量走目录近似排除（PRIVACY_SKIP_DIRS 等三组）。
        """
        tracked = self._tracked_files()
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            here = Path(dirpath)
            rel = here.relative_to(self.root)
            in_exper = rel.parts[:2] == ("tools", "experiments")
            dirnames[:] = [d for d in dirnames
                           if d not in PRIVACY_SKIP_DIRS
                           and not d.startswith(".aider")
                           and not (in_exper and (d.endswith("_out") or d == "weights"
                                                  or d == "_probe_data" or d == "_probe_scripts"))]
            if rel.parts[:2] == ("docs", "plan"):
                dirnames[:] = []
                continue
            for fn in filenames:
                p = here / fn
                if p.suffix.lower() not in PRIVACY_SUFFIXES:
                    continue
                r = self.rel(p)
                if r in PRIVACY_EXCLUDE_FILES:
                    continue
                if tracked is not None and r not in tracked:
                    continue
                out.append(p)
        return sorted(out)

    def scan(self) -> tuple[set[str], set[str]]:
        viol: set[str] = set()
        topics = self.topics()

        for t in sorted(topics):                                    # R1
            rd = self.exper / t / "README.md"
            if not rd.exists():
                viol.add(f"R1|{t}|缺 README.md")
            elif not STATUS_LINE.search(rd.read_text(encoding="utf-8", errors="replace")):
                viol.add(f"R1|{t}|README 缺 status: active 行")

        for doc in self.formal_docs():                              # R2 / R3
            text = doc.read_text(encoding="utf-8", errors="replace")
            for t in self.hits(text):
                viol.add(f"R2|{self.rel(doc)}|{t}")
            for m in PLAN_PATH.findall(text):
                viol.add(f"R3|{self.rel(doc)}|{m[:60]}")

        for g in CODE_GLOBS:                                        # R4
            for p in self.root.glob(g):
                if self.rel(p) in CODE_EXCLUDE:
                    continue
                if self.code_refs_experiments(p.read_text(encoding="utf-8", errors="replace")):
                    viol.add(f"R4|{self.rel(p)}")

        if self.exper.is_dir():                                     # R5
            for p in sorted(self.exper.rglob("*")):
                if not p.is_file() or p.suffix not in (".md", ".py", ".json"):
                    continue
                for t in self.hits(p.read_text(encoding="utf-8", errors="replace")):
                    if t not in topics:
                        viol.add(f"R5|{self.rel(p)}|{t}")

        for p in self.plan_docs():                                  # R6
            r, head = self.rel(p), self.head_text(p)
            m = PLAN_STATUS.search(head)
            if not m:
                viol.add(f"R6|{r}|缺状态行（> **状态**：active）")
            else:
                v = self.status_value(m.group(1))
                if v not in PLAN_STATUS_VALUES:
                    viol.add(f"R6|{r}|状态值不在闭集 active|retiring：{v[:24]}")
            for label, rx in (("归属去向", PLAN_HOME), ("退役条件", PLAN_RETIRE)):
                mm = rx.search(head)
                if not mm:
                    viol.add(f"R6|{r}|缺{label}行")
                elif PLAN_PLACEHOLDER.match(mm.group(1).strip()):
                    viol.add(f"R6|{r}|{label}仍是模板占位符，未填")

        for p in self.privacy_files():                              # R7
            r = self.rel(p)
            text = p.read_text(encoding="utf-8", errors="replace")
            for name, rx in PRIVACY_PATTERNS:
                if rx.search(text):
                    viol.add(f"R7|{r}|{name}")
            for i, rx in self.privacy_terms():
                if rx.search(text):
                    viol.add(f"R7|{r}|本地词表#{i}")

        baseline: set[str] = set()
        if self.baseline_path.exists():
            baseline = {ln.strip() for ln in self.baseline_path.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.startswith("#")}
        return viol, baseline


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args(argv)
    ck = Checker(root or ROOT)
    viol, baseline = ck.scan()

    if args.update_baseline:
        ck.baseline_path.write_text(
            "# 仓库卫生基线（棘轮：只减不增）。立规 2026-09-18 的存量违规在此豁免，\n"
            "# 按触碰面分诊后删除对应行；新增违规不在本文件 = CI 红。\n"
            "# 键格式：规则|文件/主题|标识。语义见 tools/check_repo_hygiene.py 与 tools/experiments/README.md。\n"
            + "\n".join(sorted(viol)) + "\n", encoding="utf-8")
        print(f"基线已重写：{len(viol)} 条存量违规")
        return 0

    fresh = viol - baseline
    stale = baseline - viol
    print(f"违规 {len(viol)}（基线内 {len(viol & baseline)} / 新增 {len(fresh)}）")
    for v in sorted(fresh):
        print(f"  [新违规] {v}")
    if stale:
        print(f"基线可收缩 {len(stale)} 条（已不再命中，随下次触碰删除）：")
        for v in sorted(stale):
            print(f"  [可删] {v}")

    ledger = ck.plan_ledger()
    if ledger:
        print(f"\nplan 工作台：{len(ledger)} 个文件（未动天数 / 状态 / 路径，按天数降序）")
        for days, st, r in ledger:
            print(f"  [{days:>4}d] {st:<9} {r}")
        print("  判读：active = 还有未闭环的事；retiring = 产出已提炼、正在退役。"
              "没有 done/archived——plan 的完成就是删除；"
              "超过两周没动且状态仍是 active，先答一句「还差什么」再决定留或退役。"
              "带 ~ 的是无更新日期、按文件修改时间估算的。")

    if fresh:
        print("\n判读：新规则不允许新违规。实验退役四步（结论迁 home → 仪器提升 → 拔引用 → 删目录）"
              "见 tools/experiments/README.md；历史证据指针用 commit，不用实验路径。")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
