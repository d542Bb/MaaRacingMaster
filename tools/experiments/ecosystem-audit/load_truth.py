#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B1 官方加载实测：对两真源目录做 Resource.post_pipeline。

复现生产实现 core/nav_graph.py NavKitV4.load 的做法：core/pipeline 与
plugins/treasure/pipeline 互指，分次 post 先加载者必失败 → 合并临时目录
一次 post。结束后清理临时目录。

同时对照：
  A. 只注册 MRA_Template/MRA_Click 后加载（生产姿势）
  B. 不注册任何 Custom 直接加载（判断框架是否校验 custom 名）
  C. 分次 post（验证"先加载者必失败"的已知坑是否仍成立）
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.custom_action import CustomAction  # noqa: E402
from maa.custom_recognition import CustomRecognition  # noqa: E402
from maa.resource import Resource  # noqa: E402

CORE_PIPE = REPO / "maaracing_assistant" / "core" / "resources" / "pipeline"
TREASURE_PIPE = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "pipeline"


class _StubReco(CustomRecognition):
    def analyze(self, context, argv):
        return None


class _StubAction(CustomAction):
    def run(self, context, argv) -> bool:
        return True


def merge(dirs: list[Path]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ecoaudit-pipeline-"))
    for d in dirs:
        for f in d.rglob("*.json*"):
            rel = f.relative_to(d)
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
    return tmp


def main() -> int:
    print("=" * 70)
    print("B1-A  合并临时目录一次性 post（生产姿势：先注册 MRA_Template/MRA_Click）")
    print("=" * 70)
    tmp = merge([CORE_PIPE, TREASURE_PIPE])
    try:
        res = Resource()
        res.register_custom_recognition("MRA_Template", _StubReco())
        res.register_custom_action("MRA_Click", _StubAction())
        res.register_custom_action("MRA_Policy", _StubAction())
        job = res.post_pipeline(str(tmp)).wait()
        print(f"  job.failed = {job.failed}  job.succeeded = {job.succeeded}")
        print(f"  job.status = {job.status}")
        nodes = res.node_list
        print(f"  框架可见节点数 = {len(nodes)}")
        for n in sorted(nodes):
            print(f"    - {n}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"  临时目录已清理: {tmp.exists() is False}")

    print()
    print("=" * 70)
    print("B1-B  不注册任何 Custom 直接加载（看框架是否校验 custom 名）")
    print("=" * 70)
    tmp = merge([CORE_PIPE, TREASURE_PIPE])
    try:
        res2 = Resource()
        job2 = res2.post_pipeline(str(tmp)).wait()
        print(f"  job.failed = {job2.failed}  job.succeeded = {job2.succeeded}")
        print(f"  框架可见节点数 = {len(res2.node_list)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 70)
    print("B1-C  分次 post（验证互指目录的已知坑）")
    print("=" * 70)
    res3 = Resource()
    res3.register_custom_recognition("MRA_Template", _StubReco())
    res3.register_custom_action("MRA_Click", _StubAction())
    res3.register_custom_action("MRA_Policy", _StubAction())
    j1 = res3.post_pipeline(str(CORE_PIPE)).wait()
    print(f"  post(core/pipeline)      failed={j1.failed}")
    j2 = res3.post_pipeline(str(TREASURE_PIPE)).wait()
    print(f"  post(treasure/pipeline)  failed={j2.failed}")
    print(f"  框架可见节点数 = {len(res3.node_list)}")

    print()
    print("=" * 70)
    print("B1-D  检查 Or/any_of 对象形态节点是否被框架接受")
    print("=" * 70)
    tmp = merge([CORE_PIPE, TREASURE_PIPE])
    try:
        res4 = Resource()
        res4.register_custom_recognition("MRA_Template", _StubReco())
        res4.register_custom_action("MRA_Click", _StubAction())
        res4.register_custom_action("MRA_Policy", _StubAction())
        res4.post_pipeline(str(tmp)).wait()
        for name in ("treasure.__boot.dwell", "treasure.第1回合出价.dwell",
                     "treasure.policy_loop"):
            try:
                data = res4.get_node_data(name)
                print(f"  {name} -> {str(data)[:220]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {name} -> get_node_data 异常 {exc!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
