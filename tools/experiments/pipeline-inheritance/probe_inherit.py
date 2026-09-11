#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C 类实验：MaaFW pipeline 的跨文件合并与节点名继承算子到底是什么行为。

要回答的三个问题（决定"通用大厅要不要单独写一份 json"能不能靠协议自带机制成立）：
  Q1 同一目录（或合并目录）下多个 JSON 是否汇成一张扁平图、可跨文件按名引用？
  Q2 节点名继承算子 @ # * + ^ 各自对 next / 其他字段做什么？子节点能否只改出口？
  Q3 两个文件里出现同名节点，框架是报错、静默覆盖，还是合并？

用法：.venv\\Scripts\\python.exe tools/experiments/pipeline-inheritance/probe_inherit.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.resource import Resource  # noqa: E402

FIELDS = ("next", "on_error", "timeout", "rate_limit", "pre_delay", "post_delay",
          "max_hit", "enabled", "inverse", "attach")


def dump(res: Resource, name: str) -> None:
    data = res.get_node_data(name)
    if data is None:
        print(f"    {name}: <get_node_data 返回 None>")
        return
    brief = {k: data.get(k) for k in FIELDS}
    print(f"    {name}: {json.dumps(brief, ensure_ascii=False)}")


def probe_q1_q2() -> None:
    """跨文件引用 + 继承算子。"""
    tmp = Path(tempfile.mkdtemp(prefix="probe-inherit-"))
    try:
        base = {
            "hall.base": {
                "recognition": "DirectHit",
                "action": "DoNothing",
                "next": ["hall.entry_a", "hall.entry_b"],
                "on_error": ["hall.fallback"],
                "timeout": 5000,
                "rate_limit": 111,
                "pre_delay": 7,
                "post_delay": 8,
                "attach": {"_dwell": True, "_from": "base"},
            },
            "hall.entry_a": {"recognition": "DirectHit", "action": "DoNothing"},
            "hall.entry_b": {"recognition": "DirectHit", "action": "DoNothing"},
            "hall.fallback": {"recognition": "DirectHit", "action": "DoNothing",
                              "next": ["hall.base"]},
        }
        # 子文件：只声明出口差异，其余靠继承（引用 hall.base，定义在另一个文件）
        child = {
            "mod_a@hall.base": {"next": ["hall.entry_a"]},
            "mod_b#hall.base": {"next": ["hall.entry_b"]},
            "mod_c*hall.base": {"next": ["hall.entry_a"]},
            "mod_d+hall.base": {"next": ["hall.entry_a"]},
            "mod_e^hall.base": {"next": ["hall.entry_a"]},
            # 纯跨文件引用（无继承）：证明扁平命名空间
            "mod_f": {"recognition": "DirectHit", "action": "DoNothing",
                      "next": ["hall.base"]},
        }
        (tmp / "a_base.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        (tmp / "b_child.json").write_text(json.dumps(child, ensure_ascii=False), encoding="utf-8")

        res = Resource()
        job = res.post_pipeline(str(tmp)).wait()
        print(f"[Q1] 两文件合并 post_pipeline failed={job.failed}")
        names = sorted(res.node_list)
        print(f"[Q1] 装载节点总数={len(names)}  node_list={names}")
        print("[Q2] 各形态解析结果：")
        for n in names:
            dump(res, n)
        print("[Q2] 用「算子左侧裸名」回读（框架是否把裸名注册为节点）：")
        for bare in ("mod_a", "mod_b", "mod_c", "mod_d", "mod_e"):
            dump(res, bare)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe_q3() -> None:
    """同名节点出现在两个文件：报错 / 静默覆盖 / 合并？"""
    tmp = Path(tempfile.mkdtemp(prefix="probe-dup-"))
    try:
        (tmp / "one.json").write_text(json.dumps({
            "dup.node": {"recognition": "DirectHit", "action": "DoNothing",
                         "next": ["tail"], "timeout": 1000,
                         "attach": {"_src": "one"}}}, ensure_ascii=False), encoding="utf-8")
        (tmp / "two.json").write_text(json.dumps({
            "dup.node": {"recognition": "DirectHit", "action": "DoNothing",
                         "on_error": ["tail"], "timeout": 2000,
                         "attach": {"_src": "two"}}}, ensure_ascii=False), encoding="utf-8")
        (tmp / "three.json").write_text(json.dumps({
            "tail": {"recognition": "DirectHit", "action": "DoNothing"}},
            ensure_ascii=False), encoding="utf-8")
        res = Resource()
        job = res.post_pipeline(str(tmp)).wait()
        print(f"[Q3] 同名节点跨文件 post_pipeline failed={job.failed}")
        dump(res, "dup.node")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    probe_q1_q2()
    print()
    probe_q3()
    return 0


if __name__ == "__main__":
    sys.exit(main())
