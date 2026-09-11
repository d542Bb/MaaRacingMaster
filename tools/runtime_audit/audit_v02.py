"""V0.2-alpha 主入口：只对 app/ 做增益归因分析，产出 runtime-audit-v02.json/.md。

不改任何文件、不发散到 V0.1 的 Oracle 校验（保持 v01 输出不动）。
"""
from __future__ import annotations

import json
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from audit.v02_app import AppAuditV02, APP_CLASS


def collect_app_files(app_dir: Path) -> list[tuple[str, int]]:
    """返回 app 下所有文件 (相对 app/, 大小)。"""
    out = []
    for p in app_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(app_dir).as_posix()
            if "__pycache__" in rel:
                continue
            out.append((rel, p.stat().st_size))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default=r"D:\maaracing_assistant\build\exp6\MaaRacingAssistant-0.19.0-win-x64")
    args = ap.parse_args()
    exp = Path(args.exp).resolve()
    app_dir = exp / "app"
    if not app_dir.exists():
        print(f"no app dir: {app_dir}")
        return 2

    a = AppAuditV02(app_dir)
    a.load_deps()
    files = collect_app_files(app_dir)

    # 归因每个文件
    results = []
    classes = defaultdict(lambda: {"mb": 0.0, "files": 0, "names": []})
    for rel, sz in files:
        if not rel.endswith(".dll"):
            # 非 dll：.exe -> NETCORE-REQUIRED(runtime)，其它按扩展名归类
            if rel.endswith((".exe", ".json", ".pdb")) and "host" in rel or rel.endswith(".exe"):
                cls = APP_CLASS["NETCORE_REQUIRED"]
            else:
                cls = APP_CLASS["UNKNOWN"]
            rec = {"name": rel, "size": sz, "pkg": "", "version": "", "parent": "", "class": cls}
        else:
            rec = a.classify_dll(Path(rel).name, sz)
            rec["name"] = rel
        results.append(rec)
        classes[rec["class"]]["mb"] += sz / (1024 * 1024)
        classes[rec["class"]]["files"] += 1
        classes[rec["class"]]["names"].append(rec["name"])

    # 每个分类 Top20 by size
    class_top20 = {}
    for cls, info in classes.items():
        names = sorted(info["names"], key=lambda n: _size_of(files, n), reverse=True)[:20]
        class_top20[cls] = names
        info["names"] = None

    # 汇总
    total_mb = sum(c["mb"] for c in classes.values())
    explained = total_mb - classes[APP_CLASS["UNKNOWN"]]["mb"]

    sdk = a.sdknet_report(
        sum(s for n, s in files if n.endswith("Microsoft.Windows.SDK.NET.dll")))

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_total_mb": round(total_mb, 2),
        "explained_mb": round(explained, 2),
        "unknown_mb": round(classes[APP_CLASS["UNKNOWN"]]["mb"], 2),
        "classes": {k: {"mb": round(v["mb"], 2), "files": v["files"]} for k, v in classes.items()},
        "class_top20": class_top20,
        "microsoft_windows_sdk_net": sdk,
        "package_dll_count": {k: len(v) for k, v in a.package_dlls.items()},
    }
    json_path = exp / "runtime-audit-v02.json"
    md_path = exp / "runtime-audit-v02.md"
    json_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_md(md_path, out)
    print(f"[v02] app total {total_mb:.2f}MB | explained {explained:.2f}MB | UNKNOWN {classes[APP_CLASS['UNKNOWN']]['mb']:.2f}MB")
    print(f"[v02] wrote {json_path}")
    print(f"[v02] wrote {md_path}")
    return 0


def _size_of(files: list, name: str) -> int:
    for n, s in files:
        if n == name:
            return s
    return 0


def _write_md(path: Path, out: dict) -> None:
    L = []
    L.append("# App Audit V0.2-alpha\n")
    L.append(f"- app total: {out['app_total_mb']:.2f} MB")
    L.append(f"- explained: {out['explained_mb']:.2f} MB")
    L.append(f"- **UNKNOWN: {out['unknown_mb']:.2f} MB**\n")
    L.append("## 分类\n")
    L.append("| class | MB | files |")
    L.append("|---:|---:|---:|")
    for k, v in sorted(out["classes"].items(), key=lambda kv: -kv[1]["mb"]):
        L.append(f"| {k} | {v['mb']:.2f} | {v['files']} |")
    L.append("")
    L.append("## SDK.NET.dll 专项\n")
    L.append(f"- size: `{out['microsoft_windows_sdk_net'].get('size_mb',0):.2f}` MB")
    L.append(f"- package: `{out['microsoft_windows_sdk_net'].get('package')}`")
    L.append(f"- version: `{out['microsoft_windows_sdk_net'].get('version')}`")
    L.append(f"- note: {out['microsoft_windows_sdk_net'].get('note')}\n")
    L.append("## 每类 Top20\n")
    for cls, names in out.get("class_top20", {}).items():
        L.append(f"### {cls}\n")
        L.append("```")
        for n in names:
            L.append(f"  {n}")
        L.append("```\n")
    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())