# -*- coding: utf-8 -*-
"""生态审计 B1 实测：官方 Resource.post_pipeline 能否加载两真源目录。

复现 core/nav_graph.py NavKitV4.load 的生产姿势：
  1) 对照实验：两目录分次 post（各自全新 Resource），验证"先加载者必失败"；
  2) 实验组：合并临时目录一次 post，记录 job 状态、节点数、C++ 层告警；
  3) finally 清理临时目录，不触碰真源。
只读审计，不写生产代码。
"""
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.resource import Resource  # noqa: E402

CORE_DIR = REPO / "maaracing_assistant" / "core" / "resources" / "pipeline"
TR_DIR = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "pipeline"


def post_one(label: str, path: Path) -> None:
    res = Resource()
    job = res.post_pipeline(str(path)).wait()
    status = ("succeeded" if job.succeeded else
              "failed" if job.failed else "other")
    nodes = res.node_list or []
    print(f"[{label}] {path.name}: job={status} loaded={res.loaded} "
          f"node_count={len(nodes)}")


def merged_load() -> None:
    merged_tmp = Path(tempfile.mkdtemp(prefix="navkit-audit-pipeline-"))
    try:
        seen: dict[str, Path] = {}
        for d in (CORE_DIR, TR_DIR):
            for f in d.rglob("*.json*"):
                rel = f.relative_to(d).as_posix()
                if rel in seen:
                    print(f"[merged] 真源文件冲突: {rel}")
                    return
                seen[rel] = d
                dest = merged_tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        res = Resource()
        job = res.post_pipeline(str(merged_tmp)).wait()
        status = ("succeeded" if job.succeeded else
                  "failed" if job.failed else "other")
        nodes = res.node_list or []
        print(f"[merged] job={status} loaded={res.loaded} node_count={len(nodes)}")
        for n in sorted(nodes):
            print("  -", n)
    finally:
        shutil.rmtree(merged_tmp, ignore_errors=True)
        print(f"[merged] 临时目录已清理: {not merged_tmp.exists()}")


if __name__ == "__main__":
    print("== 对照实验：分次 post（已知坑复现）==")
    post_one("core-only", CORE_DIR)
    post_one("treasure-only", TR_DIR)
    print("== 实验组：合并临时目录一次 post（生产姿势复现）==")
    merged_load()
