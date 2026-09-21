"""仓库卫生检查：实验与计划产物的生命周期契约（AGENTS.md 红线 6 的机检）。

立法规约（2026-09-18 维护者裁定，四条决策全通过、第③项修正为"存量不批量盖 active 章"）：

  tools/experiments/ = **当前活跃实验区**，不是归档区。留痕 = 进 git 历史，不是目录永存。
  实验关闭走四步：结论迁 home → 仪器提升 → 拔引用 → 删目录。
  历史证据的指针语法 = commit（短 SHA 展示），正式文档不得引用具体实验路径或 plan 文件。

五条规则：
  R1 入口护栏：每个主题目录必须有 README.md 且含 `status: active` 行。
     （status 是给 agent 与检查器看的入口护栏，不是生命周期数据库——在树里即 active，
      退役即删除；不存在 "closed 但留在树里" 的状态。）
  R2 知识引用污染：正式知识文档（L0~L3：AGENTS / CONTRIBUTING / docs 顶层与 adr /
     各插件与 apps 的 md）不得出现 `tools/experiments/<具体主题>/` 路径。
     类别指针（`tools/experiments/`、`<主题>` 占位、本区 README 自身）不算违规。
  R3 工作台反向引用：同范围文档不得引用 `docs/plan/` 下的具体文件（裸目录提及不算）。
  R4 运行依赖污染：生产代码 / 测试 / 脚本（*.py、apps 下 *.cs）不得把实验目录当
     import 路径、子进程命令或数据入口引用。
  R5 死引用：任何文件引用 `tools/experiments/<主题>/` 而该主题目录已不存在 → 违规
     （退役后引用链必须已拔净，这是"删目录"一步的验收）。

棘手机制：`tools/repo_hygiene_baseline.txt` 记录立规之日的存量违规（legacy，按触碰面
逐个分诊，见 tools/experiments/README.md）。基线**只减不增**——新违规直接红；
基线中已不再命中的条目会提示"可收缩"，随下一次触碰删除。

用法：
    python tools/check_repo_hygiene.py                 # 检查（CI 用）
    python tools/check_repo_hygiene.py --update-baseline   # 重写基线（仅立法/收缩时用）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # 本脚本在 tools/ 直下

# 具体主题路径：tools/experiments/<name>/；占位符（含 < >）与裸目录引用不匹配
TOPIC_PATH = re.compile(r"tools/experiments/([\w.\-]+)/")
PLAN_PATH = re.compile(r"docs/plan/[\w\-][\w\-./]*")
STATUS_LINE = re.compile(r"^\s*status:\s*active\b", re.I | re.M)

FORMAL_GLOBS = ["AGENTS.md", "CONTRIBUTING.md", "docs/*.md", "docs/adr/*.md",
                "maaracing_master/**/*.md", "apps/**/*.md"]
# docs/README 自身要描述本规则——豁免；update_log 面向玩家、不引实验路径，同受引用纪律约束
FORMAL_EXCLUDE = {"docs/README.md"}
CODE_GLOBS = ["maaracing_master/**/*.py", "scripts/**/*.py", "tests/**/*.py",
              "apps/**/*.py", "apps/**/*.cs"]


class Checker:
    def __init__(self, root: Path):
        self.root = root
        self.exper = root / "tools" / "experiments"
        self.baseline_path = root / "tools" / "repo_hygiene_baseline.txt"

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def topics(self) -> set[str]:
        return {d.name for d in self.exper.iterdir() if d.is_dir()} if self.exper.is_dir() else set()

    @staticmethod
    def hits(text: str) -> list[str]:
        return [m for m in TOPIC_PATH.findall(text) if "<" not in m and ">" not in m]

    def formal_docs(self) -> list[Path]:
        out = []
        for g in FORMAL_GLOBS:
            for p in self.root.glob(g):
                r = self.rel(p)
                if p.is_file() and r not in FORMAL_EXCLUDE and "/plan/" not in r:
                    out.append(p)
        return sorted(set(out))

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
                if "tools/experiments/" in p.read_text(encoding="utf-8", errors="replace"):
                    viol.add(f"R4|{self.rel(p)}")

        if self.exper.is_dir():                                     # R5
            for p in sorted(self.exper.rglob("*")):
                if not p.is_file() or p.suffix not in (".md", ".py", ".json"):
                    continue
                for t in self.hits(p.read_text(encoding="utf-8", errors="replace")):
                    if t not in topics:
                        viol.add(f"R5|{self.rel(p)}|{t}")

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
    if fresh:
        print("\n判读：新规则不允许新违规。实验退役四步（结论迁 home → 仪器提升 → 拔引用 → 删目录）"
              "见 tools/experiments/README.md；历史证据指针用 commit，不用实验路径。")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
