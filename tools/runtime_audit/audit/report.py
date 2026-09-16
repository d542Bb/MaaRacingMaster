"""report：生成 runtime-audit.json 与 runtime-audit.md。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .model import AuditResult, FileRecord


def build_audit_result(exp_root: Path, files: dict[str, FileRecord], pkgs: list,
                       oracle_stats: dict) -> AuditResult:
    res = AuditResult(exp_root=str(exp_root))
    res.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    res.files = list(files.values())
    res.packages = pkgs
    res.oracle = oracle_stats
    # 分类汇总
    summary = {}
    by_cls: dict[str, list[FileRecord]] = {}
    for f in res.files:
        by_cls.setdefault(f.file_class, []).append(f)
    for cls, lst in by_cls.items():
        summary[cls] = {
            "files": len(lst),
            "mb": sum(x.size for x in lst) / (1024 * 1024),
        }
    res.classes_summary = summary
    # Top candidates：UNUSED-CANDIDATE + 高 confidence，按大小排序
    cand = [f for f in res.files if f.file_class == "UNUSED-CANDIDATE"]
    cand.sort(key=lambda x: -x.size)
    res.top_candidates = [
        {"path": f.path, "mb": f.size / (1024 * 1024), "confidence": f.confidence,
         "pkg": f.pkg, "runtime_loaded": f.runtime_loaded}
        for f in cand[:100]
    ]
    return res


def write_json(res: AuditResult, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "exp_root": res.exp_root,
        "generated_at": res.generated_at,
        "classes_summary": res.classes_summary,
        "files": [f.to_dict() for f in res.files],
        "packages": [p.__dict__ for p in res.packages],
        "top_candidates": res.top_candidates,
        "oracle": res.oracle,
        "warnings": res.warnings,
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_md(res: AuditResult, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    L = []
    L.append("# Runtime Closure Audit\n")
    L.append(f"- exp_root: `{res.exp_root}`")
    L.append(f"- generated: {res.generated_at}")
    L.append(f"- files audited: {len(res.files)}\n")

    # 1 总览
    L.append("## 1. 总览\n")
    L.append("| 项 | 大小(MB) | 文件数 |")
    L.append("|---:|---:|---:|")
    total = sum(f.size for f in res.files) / (1024 * 1024)
    L.append(f"| total | {total:.2f} | {len(res.files)} |")
    # 按 rel_category 聚合近似（runtime/app/assets/other）
    from collections import defaultdict
    cat = defaultdict(lambda: {"mb": 0, "n": 0})
    for f in res.files:
        c = f.rel_category
        cat[c]["mb"] += f.size / (1024 * 1024)
        cat[c]["n"] += 1
    for c, v in cat.items():
        L.append(f"| {c} | {v['mb']:.2f} | {v['n']} |")
    L.append("")

    # 2 分类
    L.append("## 2. 分类\n")
    L.append("| 分类 | MB | 文件数 |")
    L.append("|---:|---:|---:|")
    for cls, v in sorted(res.classes_summary.items(), key=lambda kv: -kv[1]["mb"]):
        L.append(f"| {cls} | {v['mb']:.2f} | {v['files']} |")
    L.append("")

    # 3 Top100
    L.append("## 3. TOP 候选（UNUSED-CANDIDATE 按大小）\n")
    if res.top_candidates:
        L.append("| MB | path | confidence | pkg | runtime |")
        L.append("|---:|---|---|---:|---:|")
        for c in res.top_candidates[:100]:
            L.append(f"| {c['mb']:.2f} | `{c['path']}` | {c['confidence']} | {c['pkg']} | {c['runtime_loaded']} |")
    else:
        L.append("（无）")
    L.append("")

    # 7 oracle
    L.append("## 7. 实验 oracle 对照\n")
    o = res.oracle
    L.append(f"- 已知 KEEP：识别正确 {o.get('keep_correct', 0)} / {o.get('keep_total', 0)}")
    L.append(f"- 已知 REMOVE：识别正确 {o.get('remove_correct', 0)} / {o.get('remove_total', 0)}")
    L.append(f"- 误判（应 KEEP 但判可删 / 应 REMOVE 但判 KEEP）：{o.get('wrong', '[]')}")
    L.append("")

    # 4 python graph sample
    L.append("## 4. Python dependency graph（采样）\n")
    L.append("```")
    L.append("(见 runtime-audit.json，含 full static edges)")
    L.append("```\n")

    L.append("## 5. Native dependency graph\n")
    L.append("```")
    L.append("(见 runtime-audit.json，含 full PE edges)")
    L.append("```\n")

    L.append("## 6. Dynamic loading candidates\n")
    dyn = [f for f in res.files if f.dynamic_kinds]
    L.append(f"- 数量: {len(dyn)}")
    for f in dyn[:30]:
        L.append(f"  - `{f.path}` -> {','.join(f.dynamic_kinds)}")
    L.append("")

    out.write_text("\n".join(L), encoding="utf-8")