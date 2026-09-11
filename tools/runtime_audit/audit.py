"""Runtime Closure Auditor V0.1 主入口。

用法：
    python audit.py [--exp <exp_root>] [--out <json_path>] [--skip-trace]

流程：
    L1 python 静态图 -> L2 runtime trace (可选) -> L3 PE 图 ->
    attribution -> classify(+oracle flag) -> 统计 -> report(json/md)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from audit import config
from audit.model import FileRecord, PackageRecord, Confidence
from audit.layer1_static import PyStaticGraph
from audit import layer2_trace, layer4_native
from audit.layer3_pe import PeGraph
from audit.attribution import Attriber
from audit.classify import Classifier
from audit import oracle
from audit import report


def collect_files(exp_root: Path) -> dict[str, FileRecord]:
    """枚举发行根下全部文件，建立相对路径->FileRecord。"""
    files: dict[str, FileRecord] = {}
    for p in exp_root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(exp_root).as_posix()
            if "/__pycache__/" in rel or rel.startswith("__pycache__"):
                continue
            if "/.git/" in rel:
                continue
            relcat = _rel_category(rel)
            files[rel] = FileRecord(path=rel, size=p.stat().st_size, rel_category=relcat)
    return files


def _rel_category(rel: str) -> str:
    if rel.startswith("runtime/"):
        return "runtime"
    if rel.startswith("app"):
        return "app"
    if rel.startswith("assets"):
        return "assets"
    if rel.startswith("maaracing_master"):
        return "sidecar"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default=str(config.DEFAULT_EXP_ROOT))
    ap.add_argument("--out", default="")
    ap.add_argument("--skip-trace", action="store_true", help="跳过 Layer2 runtime trace（用空集）")
    args = ap.parse_args()

    exp_root = Path(args.exp).resolve()
    if not exp_root.exists():
        print(f"[audit] exp root not found: {exp_root}", file=sys.stderr)
        return 2
    out_json = Path(args.out) if args.out else exp_root / "runtime-audit.json"
    out_md = out_json.with_suffix(".md")

    files = collect_files(exp_root)
    print(f"[audit] files enumerated: {len(files)}")

    # L1 python 静态图
    py_graph = PyStaticGraph(exp_root)
    py_graph.build()
    print(f"[audit] L1 python static graph: {len(py_graph.refs)} modules")

    # L2 runtime trace
    if not args.skip_trace:
        trace_out = exp_root / ".audit-trace.json"
        try:
            loaded = layer2_trace.run_runtime_trace(exp_root, trace_out)
            layer2_trace.mark_py_runtime(files, loaded)          # .py/.pyd/.so 统一标记
            layer4_native.mark_native_runtime(files, loaded, trace_out)
            print(f"[audit] L2 runtime trace modules: {len(loaded)}")
        except Exception as e:
            print(f"[audit] L2 trace failed: {e!r}", file=sys.stderr)

    # L3 PE 图
    pe_graph = PeGraph(exp_root)
    pe_graph.scan_root()
    print(f"[audit] L3 PE scanned: {len(pe_graph.edges)} native files; errors {len(pe_graph.errors)}")

    # attribution + classify
    attr = Attriber(exp_root)
    attr.scan_pkg_meta()
    clf = Classifier(exp_root, py_graph, pe_graph)
    for rel, rec in files.items():
        rec.pkg = attr.attribute(rel)
        clf.classify_file(rec)
        clf.flag_dynamic(rec)

    # oracle 统计
    oracle_stats = _oracle_stats(files)

    # packages 归因汇总
    pkgs = _package_rollup(files, attr)

    result = report.build_audit_result(exp_root, files, pkgs, oracle_stats)
    result.warnings = py_graph.errors[:50]
    report.write_json(result, out_json)
    report.write_md(result, out_md)
    print(f"[audit] wrote {out_json}")
    print(f"[audit] wrote {out_md}")
    return 0


def _oracle_stats(files: dict[str, FileRecord]) -> dict:
    keep_correct = keep_total = remove_correct = remove_total = 0
    wrong = []
    for rec in files.values():
        rel = rec.path
        # KEEP 断言
        for prefix, cls, _ in oracle.KNOWN["KEEP"]:
            if rel.startswith(prefix):
                keep_total += 1
                if rec.file_class in ("REQUIRED", "RUNTIME-LOADED"):
                    keep_correct += 1
                else:
                    wrong.append(f"KEEP:{prefix}->{rec.file_class}")
        for prefix, cls, _ in oracle.KNOWN["REMOVE"]:
            if rel.startswith(prefix):
                remove_total += 1
                if rec.file_class in ("DEV-ONLY", "UNUSED-CANDIDATE", "UNKNOWN"):
                    remove_correct += 1
                else:
                    wrong.append(f"REMOVE:{prefix}->{rec.file_class}")
    return {
        "keep_total": keep_total, "keep_correct": keep_correct,
        "remove_total": remove_total, "remove_correct": remove_correct,
        "wrong": wrong[:50],
    }


def _package_rollup(files: dict[str, FileRecord], attr: Attriber) -> list[PackageRecord]:
    from collections import defaultdict
    agg = defaultdict(lambda: {"size": 0, "native": [], "cls": set(), "conf": set(), "loaded": False})
    for rec in files.values():
        a = agg[rec.pkg]
        a["size"] += rec.size
        a["cls"].add(rec.file_class)
        if rec.confidence != "UNKNOWN":
            a["conf"].add(rec.confidence)
        if rec.runtime_loaded == "YES":
            a["loaded"] = True
        if rec.path.endswith((".dll", ".exe", ".pyd", ".so")):
            a["native"].append(rec.path)
    out = []
    for pkg, a in agg.items():
        p = PackageRecord(name=pkg, version=attr.version(pkg), size=a["size"],
                          native_files=sorted(a["native"])[:40])
        p.runtime_loaded = "YES" if a["loaded"] else "NO"
        p.file_class = _dominant(a["cls"])
        p.confidence = _dominant(a["conf"]) or Confidence.UNKNOWN.value
        p.conclusion = _conclusion(p)
        out.append(p)
    out.sort(key=lambda x: -x.size)
    return out


def _dominant(s: set):
    if not s:
        return ""
    return sorted(s, key=lambda c: sum(1 for _ in range(c.count("LOADED"))), reverse=True)[0]


def _conclusion(p: PackageRecord) -> str:
    if p.file_class in ("UNUSED-CANDIDATE", "DEV-ONLY"):
        if p.confidence in ("HIGH", "MEDIUM"):
            return "REMOVE"
        return "CANDIDATE"
    if p.file_class in ("REQUIRED", "RUNTIME-LOADED"):
        return "KEEP"
    return "UNKNOWN"


if __name__ == "__main__":
    raise SystemExit(main())