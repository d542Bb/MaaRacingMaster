"""仓库卫生检查：实验与计划产物的生命周期契约（AGENTS.md 红线 6 的机检）。

立法规约（2026-09-18 维护者裁定，四条决策全通过、第③项修正为"存量不批量盖 active 章"）：

  tools/experiments/ = **当前活跃实验区**，不是归档区。留痕 = 进 git 历史，不是目录永存。
  实验关闭走四步：结论迁 home → 仪器提升 → 拔引用 → 删目录。
  历史证据的指针语法 = commit（短 SHA 展示），正式文档不得引用具体实验路径或 plan 文件。

七条规则：
  R1 入口护栏：每个主题目录必须有 README.md 且含 `status: active` 行。
     （status 是给 agent 与检查器看的入口护栏，不是生命周期数据库——在树里即 active，
      退役即删除；不存在 "closed 但留在树里" 的状态。区内下划线目录
      （`_probe_data`、`_probe_scripts`）不是实验主题，不参与本条判定。）
  R2 知识引用污染：正式知识文档（L0~L3：AGENTS / CONTRIBUTING / docs 顶层与 adr /
     各插件与 apps 的 md）不得出现 `tools/experiments/<具体主题>` 路径（尾斜杠可有可无）。
     类别指针（`tools/experiments/`、`<主题>` 占位、本区 README 与索引等区内文件）不算违规。
  R3 工作台反向引用：正式文档**与生产/测试/运维代码**不得引用 `docs/plan/` 下的具体文件
     （裸目录提及不算）。两种拼法都算：字面 `docs/plan/…` 与相对拼写 `../plan/…`——
     只认字面时，ADR 里的相对写法会整类绕过本条（历史实例：ADR-0005 三处指向已清空的
     工作台文件，检查器长期绿）。
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

三条引用完整性规则（2026-11-06 立规，来源：仓库结构审计——18 处指向不存在路径的引用
全部溜过了 R1~R7，说明既有规则的口径是「不该引用什么」，缺「引用了的东西在不在」）：
  R8 路径可解析（文档面存在性）：正式知识文档与本区 templates 的 markdown 里，每个仓库
     路径引用（markdown 链接目标与反引号路径两种拼法）必须能解析到实际文件/目录，否则
     改写成 commit 指针。**只扫文档面**：代码注释里的第三方包内路径与运行期落点无法与
     「本仓库的某个文件」机检区分，硬扫只会把闸门训成噪音源；代码面的引用需求由 R3 与
     R9 的字面禁令承担。解析按基准顺序试（见 LINK_BASES），试得开就不算违规——判定基准
     的可解析性来自仓库既有书写口径（包根相对、子系统根相对），不是新约定。
  R9 本机不入库面不得被引用：文档不得指向「没有任何入库文件的顶层目录」（当前为
     `archive/`、`build/`、`dataset/`、`training/`）。判据动态取自 `git ls-files`：某顶层
     目录一个入库文件都没有，照引用去翻的人必然打不开——盘上凑巧存在也不构成合法引用，
     这条与 R8 的存在性判据是两回事，不可合并。两处口径与 R8 不同：
     ① 面更宽——脚本与工具目录的文档也扫（那两处最爱拿 `build/` 当证据），第三方 vendored
        文档、工作台与实验区除外；② 代码不扫——代码把不入库目录当**输出落点**是合法用法。
     反斜杠拼写（`build\\release\\x.md`）与斜杠同判：Windows 上手写的文档多用前者。
  R10 脚手架只描述现役世代：插件脚手架 `templates/plugin/**` 下不得出现退役数据面的文件名
     与 API 字样（`_assets.json` 结尾的文件名、`Assets.load`、`global_assets`、
     `generated/pipeline`）。判据限插件脚手架：`templates/plan` 是方案模板、不描述数据面，
     ADR 背景/证据章与宪法历史章提退役世代是合法的历史叙述，都不在本条面内。
  R11 契约文档正文腐化（issue #9，2026-11-07 立规）：现役 CODE_WIKI（docs 与
     maaracing_master 下，归档区除外）正文不得含过程指纹——日期 / 会话戳 / 轮次（第N轮）/
     阶段代号（阶段 B）/ step 编号 / 迁移期号（P4a）/ 计数快照（N passed、N 节点）。这些是
     「写作当时有意义、日后变噪音」的过程残留，历史证据应指向 commit。模式刻意收窄防误报：
     commit SHA、配置阈值、P50/P95 分位、游戏阶段数字（阶段 1/2）都不命中。ADR 日期字段、
     update_log 与宪法历史章不在本条面内（合法历史叙述）。走棘轮：存量入基线、新增即红。

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
import posixpath
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]   # 本脚本在 tools/ 直下

# 具体主题路径：tools/experiments/<name>；占位符（含 < >）与裸目录引用不匹配。
# 尾斜杠可有可无——`tools/experiments/foo` 与 `…/foo/` 指同一件事，只认前者的写法
# 会漏检（历史实例：插件 CODE_WIKI 引用主题名时没写尾斜杠，长期未被算违规）。
TOPIC_PATH = re.compile(r"tools/experiments/([\w.\-]+)")
# R3：工作台引用的两种拼法都算。只认字面 `docs/plan/` 时，`../plan/…` 整类绕过——
# 历史实例：ADR-0005 三处相对写法指向已清空的工作台文件，检查器一直是绿的。
PLAN_PATH = re.compile(r"(?:docs/plan|(?:\.\./)+plan)/[\w\-][\w\-./]*")
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

FORMAL_GLOBS = ["AGENTS.md", "CONTRIBUTING.md", "README.md", "docs/*.md", "docs/adr/*.md",
                "maaracing_master/**/*.md", "apps/**/*.md"]
# docs/README 自身要描述本规则——豁免；update_log 面向玩家、不引实验路径，同受引用纪律约束
FORMAL_EXCLUDE = {"docs/README.md"}
CODE_GLOBS = ["maaracing_master/**/*.py", "scripts/**/*.py", "tests/**/*.py",
              "apps/**/*.py", "apps/**/*.cs"]
# 检查器自己的测试在 tmp_path 里搭假树（`tmp_path / "tools" / "experiments" / …`），
# 那不是运行依赖，是测试素材；排除以免自指。
CODE_EXCLUDE = {"tests/test_repo_hygiene.py"}

# ---- R8 / R9 / R10：引用完整性（2026-11-06 立规）------------------------------
# 提取两种拼法：markdown 链接目标、反引号内的路径。URL、锚点、纯文件名（不含 /）
# 与占位符句式都不构成「本仓库的某个位置」这一声明。
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s#]+)")
BACKTICK_PATH = re.compile(r"`([^`\s]+)`")
# 类型表按「仓库里存在过的源码与资产类型」定。2026-11-06 对抗复核补了 svg/whl/xaml/
# sln/rc/mjs/ico/jsonl/tsx/jsx：当时探针证实 `…/logo.svg`、`…-0.1.0.whl` 这类死引用
# 因类型不在表内被判成「不是声明」而溜走；补齐后对现有仓库零新增命中。
# 刻意不收 .log .db .npy .zip .7z .exe .dll——它们说的是运行期落点、下载来的模型包与
# 构建产物，文档提到它们不构成「仓库里某个位置」的声明。
LINK_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".yaml", ".yml", ".cs", ".ts", ".tsx",
                 ".js", ".jsx", ".mjs", ".toml", ".txt", ".csv", ".tsv", ".html", ".css",
                 ".cmd", ".ps1", ".sh", ".ini", ".cfg", ".xml", ".onnx", ".ico", ".png",
                 ".jpg", ".jpeg", ".webp", ".svg", ".whl", ".xaml", ".sln", ".rc", ".manifest"}
# 以顶层目录名起头 = 声明「仓库根相对」，只按仓库根与本文档目录解析；不这么限的话，
# `tests/test_x.py` 只要在任何子树里同名就算存在，恰好放走本条要抓的那一类漂移。
REPO_TOP_DIRS = {"apps", "archive", "assets", "build", "dataset", "docs", "scripts",
                 "skills", "templates", "tests", "tools", "training", "maaracing_master"}
# 运行期落点（用户数据目录五类、框架目录、发布 stage 布局）与外部信源（官方文档、
# 依赖包内路径、社区参考实现）不是仓库路径。口径同 AGENTS「知识路由」的 L0 外部信源。
NON_REPO_PREFIXES = ("framework/", "runtime/", "packages/", "app/", "config/", "data/",
                     "logs/", "debug/", "RacingMaster/", "docs/zh_cn/", "maa/",
                     "maafw/", "site-packages/", "ch_ppocr", "depth_anything", "dinov2/",
                     "ultralytics/", "Roguelike/", "M9A/")
# 「子系统根相对」写法基准：`js/log.js`、`studio/server.py`、`core/wgcap.py` 这类
# 按宿主根书引用是本仓库既有口径（各 CODE_WIKI 通篇如此），不是新约定。试得开即存在；
# 换不来的是真被删掉的东西——它在任何基准下都不存在。
LINK_BASES = ("maaracing_master", "apps/MaaRacingMaster.Shell",
              "apps/MaaRacingMaster.Shell/frontend", "tools/pipeline")
# 判定面限「带已知扩展名的位置声明」：裸目录提及（尾斜杠、无扩展名）不参与存在性判定——
# 目录种类的说法太多（会话产物布局、产物根、退役世代结构），扫它只会把闸门训成噪音源。

# R9：没有任何入库文件的顶层目录 = 本机面，指向它的人必然打不开（盘上凑巧存在也不合法）。
LOCAL_ONLY_DIRS_DEFAULT = {"archive", "build", "dataset", "training"}
# 目录名集合随树变（tracked 为准），故按模板现拼正则；前置边界避免
# `plugins/*/tools/training/` 这类仓库内同名子目录被误判成根级本机面。
LOCAL_ONLY_TEMPLATE = (r"(?:^|[\s`'\"(【（，。；:])({})/[\w.\-]+(?:/[\w.\-]+)*/?(?![\w.\-])")
# R9 的面比 R8 宽：脚本与工具目录的文档最容易把本机产物根（build/）当证据引用。
# R8 的面刻意不扩——实测纳入这两处会命中 29 条，其中 26 条是打包时带进来的第三方库
# 自带 README（例如第三方包的 `node_modules/**`），那是噪音不是债务。
R9_EXTRA_GLOBS = ["scripts/**/*.md", "tools/**/*.md"]
R9_SKIP_MARKERS = ("/node_modules/", "/experiments/", "/plan/")

# R10：退役世代字样，限**插件脚手架**（templates/plan 是给方案用的模板，不描述数据面；
# ADR 背景与宪法历史章提退役世代是合法叙述，同样不在本条面内）。
RETIRED_TERMS = ("_assets.json", "Assets.load", "global_assets", "generated/pipeline")
TEMPLATE_PLUGIN_GLOBS = ["templates/plugin/**/*.md", "templates/plugin/**/*.py"]

# R11 契约文档正文腐化（issue #9）：现役 CODE_WIKI 正文的过程指纹，面排除归档区
# （历史快照）。模式收窄防误报——迁移期号只认小写字母尾（P4a 命中、P50/P95 分位不命中），
# 阶段代号只认大写字母（阶段 B 命中、游戏阶段 1/2 不命中）；可选空白用 [ \t] 不用 \s，
# 避免跨行匹配把换行带进基线键。
R11_GLOBS = ["docs/CODE_WIKI.md", "maaracing_master/**/CODE_WIKI.md"]
R11_PATTERNS = [
    ("日期", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("会话戳", re.compile(r"\b\d{8}_\d{6}\b")),
    ("轮次", re.compile(r"第[ \t]*[0-9一二三四五六七八九十]+[ \t]*轮")),
    ("阶段代号", re.compile(r"阶段[ \t]*[A-Z]\b")),
    ("step编号", re.compile(r"\bstep[ \t]*[0-9]+\b", re.I)),
    ("迁移期号", re.compile(r"\bP[0-9]+[a-z]\b")),
    ("计数快照", re.compile(r"\b\d+[ \t]*passed\b")),
    ("计数快照", re.compile(r"\b\d+[ \t]*节点")),
]

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
        """实验主题名。下划线前缀是区内工作目录（`_probe_data`、`_probe_scripts`），
        不是主题——把它们当主题发 `status: active` 章，等于给生命周期数据库开了个洞。"""
        if not self.exper.is_dir():
            return set()
        return {d.name for d in self.exper.iterdir() if d.is_dir() and not d.name.startswith("_")}

    def intra_dirs(self) -> set[str]:
        """区内目录名（主题 + 下划线工作目录）——R5 判「引用还在不在」用它。

        下划线工作目录**不入库**（`_probe_data`、`_probe_scripts` 在干净检出里根本不存在），
        只看盘上有什么就会「本地绿、CI 红」——实证：干净检出里 R5 把
        `speedrush_planning/README.md` 对 `_probe_data` 的正常引用报成死引用。
        所以除盘上目录外，再认区内 `.gitignore` 里声明过的那些名字：声明是入库的，
        名字打错仍然会红，覆盖不降级。"""
        out: set[str] = set()
        if self.exper.is_dir():
            out = {d.name for d in self.exper.iterdir() if d.is_dir()}
        gi = self.exper / ".gitignore"
        if gi.is_file():
            for ln in gi.read_text(encoding="utf-8", errors="replace").splitlines():
                name = ln.strip().rstrip("/")
                if name.startswith("_") and "/" not in name:
                    out.add(name)
        return out

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

    # ---- R8 / R9 / R10 --------------------------------------------------------

    def link_docs(self) -> list[Path]:
        """R8/R9 的文档扫描面：正式知识文档 + 脚手架文档。"""
        out = list(self.formal_docs())
        for p in self.root.glob("templates/**/*.md"):
            if p.is_file():
                out.append(p)
        return sorted(set(out))

    def link_candidates(self, doc_rel: str, token: str) -> list[str]:
        """按仓库既有书写口径列出候选路径（已剔除越界候选）。

        顶层目录起头 = 「仓库根相对」声明，只试仓库根与本文档目录；其余允许各子系统根
        相对写法（LINK_BASES，各 CODE_WIKI 通篇如此，不是新约定）。越出仓库根的候选
        不试：上级目录在盘上存在，试它会把「跳出仓库」误判成「解析成功」。"""
        tok = token.rstrip("/")
        top = tok.split("/")[0]
        doc_dir = posixpath.dirname(doc_rel)
        if tok.startswith(("./", "../")):
            # 显式相对写法是「相对于本文档」的声明，只按本文档目录解析——同时试子系统根
            # 会让少写一级的链接换个基准蒙过去（真实形态：ADR-0009 的 ../maaracing_master/
            # 从 docs/adr 出发少了一级，却能在包根基准下凑巧试开）。
            bases = [doc_dir] if doc_dir and doc_dir != "." else [""]
        else:
            bases = ([doc_dir] if doc_dir and doc_dir != "." else [])
            bases += [""] if top in REPO_TOP_DIRS else ["", *LINK_BASES]
        out = []
        for b in bases:
            cand = posixpath.normpath(posixpath.join(b, tok)) if b else posixpath.normpath(tok)
            if cand.startswith("..") or cand.startswith("/"):
                continue
            out.append(cand)
        return out

    def link_claims(self, doc_rel: str, text: str) -> list[str]:
        """文本里指「本仓库某个位置」却解析不到的引用，两种拼法都认：markdown 链接目标、
        反引号路径。返回的是原样 token（越界的候选已在 link_candidates 里剔除）。

        不算声明的（这一层决定误报率，每一条都是真实出现过的书写形态）：
          - URL、锚点、`%APPDATA%` 这类环境变量根、绝对路径、`~/`；
          - 通配与并列写法（`plugins/*/module.py`、`{module,hud}.py`）——那是模式不是位置；
          - 含 `<…>` 占位符的模板句式；
          - 尾段不带已知扩展名的散文提及（目录种类，不是位置声明）；
          - 既非「仓库根相对起头」、父目录在任何基准下也不存在的路径：`hud/hud_meta.json`、
            `raw/000001_raw.jpg`、`lane/plus_small_384x640.onnx` 说的是运行期会话产物与
            下载来的模型包内部布局，仓库里根本没有 `hud/`、`raw/`、`lane/` 这些目录；
          - 工作台引用（归 R3 判，不在此重复计）。
        """
        out: list[str] = []
        seen: set[str] = set()
        for raw in MD_LINK.findall(text) + BACKTICK_PATH.findall(text):
            t = unquote(raw.strip().rstrip(".,;:!?)]】》'\"`")).replace("\\", "/")
            if not t or "<" in t or ">" in t or "/" not in t or t in seen:
                continue
            if t.startswith(("http://", "https://", "mailto:", "file://", "#", "//",
                             "%", "/", "~")):
                continue
            if any(c in t for c in "*{}$"):
                continue
            if t.startswith(NON_REPO_PREFIXES) or PLAN_PATH.match(t):
                continue
            tail = t.split("/")[-1]
            if "." not in tail or "." + tail.rsplit(".", 1)[-1].lower() not in LINK_SUFFIXES:
                continue
            cands = self.link_candidates(doc_rel, t)
            # 声明成立的判据：要么它按仓库根相对起头（顶层目录名），要么它的**父目录**
            # 在某个基准下确实存在。`hud/hud_meta.json`、`raw/000001_raw.jpg` 这类
            # 会话产物布局两条都不满足——仓库里没有 `hud/`、`raw/`，写它的人指的不是
            # 仓库位置，报它属误判。反之 `tests/`、`tools/`、`archive/` 起头的即使整条
            # 都不存在也照报：那是明确的仓库根相对声明。
            top = t.split("/")[0]
            stated = (top in REPO_TOP_DIRS or t.startswith(("./", "../")) or any(
                (self.root / posixpath.dirname(c)).is_dir() for c in cands if posixpath.dirname(c)))
            # 显式相对写法（`./`、`../` 起头）本身就是位置声明，不看父目录：
            # 历史实例——ADR-0009 少写一级的 `../maaracing_master/plugins/…` 在任何基准下
            # 父目录都不成立，按上一条判会整类溜走。
            if stated and not any((self.root / c).exists() for c in cands):
                out.append(t)
                seen.add(t)
        return out

    def local_only_dirs(self) -> set[str]:
        """树里没有任何入库文件的顶层目录（红线 6 的「本机不入库面」）。

        名单来源必须是**入库声明**而不是盘上有什么：干净检出里 `build/`、`archive/`
        这些目录根本不存在，只看盘就把本条在 CI 上整条空转（2026-11-06 对抗复核实证：
        本地红、CI 不红），而「已提交的 build/ 类回归」恰恰需要 CI 拦。
        判据取根 `.gitignore` 里声明过的顶层目录名 ∪ 盘上顶层目录，再减去有入库文件的
        那些；git 不可用（测试假树）时回退缺省集合，与 R7 的回退口径一致。"""
        declared: set[str] = set()
        gi = self.root / ".gitignore"
        if gi.is_file():
            for ln in gi.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                # 只认目录声明（以 / 结尾）：`docs/trae-dsh-config-share.md` 这类
                # 文件级忽略项不是目录，取它首段会把 docs/ 整个变成"本机目录"，
                # 于是所有指向 docs 的正常引用集体误报（模拟检出于 2026-11-06 抓到）。
                if not ln or ln.startswith("#") or ln.startswith("!") or not ln.endswith("/"):
                    continue
                head = ln.lstrip("/").split("/")[0].rstrip("/")
                if head and not head.startswith(".") and "*" not in head:
                    declared.add(head)
        tracked = self._tracked_files()
        if not tracked:
            return declared | set(LOCAL_ONLY_DIRS_DEFAULT)
        tracked_tops = {t.split("/")[0] for t in tracked if "/" in t}
        on_disk = {p.name for p in self.root.iterdir()
                   if p.is_dir() and not p.name.startswith(".")}
        return (declared | on_disk) - tracked_tops

    def r9_docs(self) -> list[Path]:
        """R9 的扫描面：R8 的面 + 脚本与工具目录的文档（第三方 vendored 文档、
        工作台与实验区除外——见 R9_EXTRA_GLOBS 的取舍理由）。"""
        out = self.link_docs()
        for g in R9_EXTRA_GLOBS:
            for p in self.root.glob(g):
                if p.is_file() and not any(k in self.rel(p) for k in R9_SKIP_MARKERS):
                    out.append(p)
        return sorted(set(out))

    @staticmethod
    def local_only_hits(text: str, dirs: set[str]) -> list[str]:
        if not dirs:
            return []
        rx = re.compile(LOCAL_ONLY_TEMPLATE.format("|".join(sorted(dirs))))
        out = []
        for m in rx.finditer(text):
            hit = m.group(0).lstrip(" \t`'\"(【（，。；:")
            # 用户数据目录那几类（logs/ data/ debug/ config/ framework/…）与根级
            # 不入库目录同名时，说的是运行期落点，不是「仓库里的本机目录」——与 R8
            # 的豁免表同口径，否则两条规则会互相打脸。
            if hit.startswith(NON_REPO_PREFIXES):
                continue
            out.append(hit)
        return out

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

        for g in CODE_GLOBS:                                        # R4 / R3（代码面）
            for p in self.root.glob(g):
                if self.rel(p) in CODE_EXCLUDE:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
                if self.code_refs_experiments(text):
                    viol.add(f"R4|{self.rel(p)}")
                for m in PLAN_PATH.findall(text):
                    viol.add(f"R3|{self.rel(p)}|{m[:60]}")

        for p in self.root.glob("templates/**/*.py"):                # R2 / R3（脚手架）
            text = p.read_text(encoding="utf-8", errors="replace")
            r = self.rel(p)
            for t in self.hits(text):
                viol.add(f"R2|{r}|{t}")
            for m in PLAN_PATH.findall(text):
                viol.add(f"R3|{r}|{m[:60]}")

        for doc in self.link_docs():                                 # R8 路径可解析
            r = self.rel(doc)
            for bad in self.link_claims(r, doc.read_text(encoding="utf-8", errors="replace")):
                viol.add(f"R8|{r}|{bad[:70]}")

        local_dirs = self.local_only_dirs()                          # R9 本机不入库面
        # 只判文档面：代码把不入库目录当**输出落点**是合法用法（打包与训练脚本就往
        # build/ 写产物），违规的是把它当**证据来源**叙述——那一类只可能出现在文档里。
        # 匹配前把反斜杠归一：Windows 上写文档的人用 `build\release\x.md`，只认斜杠会让
        # 本条在自己最该抓的拼法上空手而归（实测同一引用两种写法，斜杠命中、反斜杠漏）。
        if local_dirs:
            for doc in self.r9_docs():
                text = doc.read_text(encoding="utf-8", errors="replace").replace("\\", "/")
                for hit in self.local_only_hits(text, local_dirs):
                    viol.add(f"R9|{self.rel(doc)}|{hit[:70]}")

        for g in TEMPLATE_PLUGIN_GLOBS:                              # R10 脚手架退役世代
            for p in self.root.glob(g):
                if not p.is_file():
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
                for term in RETIRED_TERMS:
                    if term in text:
                        viol.add(f"R10|{self.rel(p)}|{term}")

        for g in R11_GLOBS:                                       # R11 正文腐化
            for p in self.root.glob(g):
                r = self.rel(p)
                if not p.is_file() or "/_archive/" in r:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
                for name, rx in R11_PATTERNS:
                    for m in rx.finditer(text):
                        viol.add(f"R11|{r}|{name}|{m.group(0)}")

        if self.exper.is_dir():                                     # R5
            area = self.topics() | self.intra_dirs()
            for p in sorted(self.exper.rglob("*")):
                if not p.is_file() or p.suffix not in (".md", ".py", ".json"):
                    continue
                for t in self.hits(p.read_text(encoding="utf-8", errors="replace")):
                    if t not in area:
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
        # 立法/收缩时用：保留基线里已有的注释（各条存量的分诊来历），只重写键行。
        # 丢掉这些注释等于丢掉「为什么这一行可以豁免」，下一次分诊只能重猜。
        old_comments: list[str] = []
        if ck.baseline_path.exists():
            old_comments = [ln for ln in ck.baseline_path.read_text(encoding="utf-8").splitlines()
                            if ln.startswith("#")]
        header = ["# 仓库卫生基线（棘轮：只减不增）。立规 2026-09-18 的存量违规在此豁免，",
                  "# 按触碰面分诊后删除对应行；新增违规不在本文件 = CI 红。",
                  "# 键格式：规则|文件/主题|标识。语义见 tools/check_repo_hygiene.py 与 tools/experiments/README.md。"]
        body = "\n".join(sorted(viol))
        extra = [c for c in old_comments if c not in header]
        text = "\n".join(header + extra) + ("\n" + body if body else "") + "\n"
        ck.baseline_path.write_text(text, encoding="utf-8")
        print(f"基线已重写：{len(viol)} 条存量违规（保留注释 {len(extra)} 行）")
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
