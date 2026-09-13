#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真框架加载实测：本轮新增的三种形态，MaaFW 认不认。

复现生产姿势（core/nav_graph.py NavKitV4.load）：合并 pipeline 目录后一次 post，
注册名与生产一致（MaaRM_Template / MaaRM_Click / MaaRM_Input / MaaRM_Policy）。

要验的三件事：
  ① `action: Custom + MaaRM_Input` 的节点能否加载（框架是否校验 custom 动作名）；
  ② `recognition: Or + any_of[纯锚点节点]` 能否被解析成识别定义（按名引用不要求
     被引节点有 next，_anchor_only 形态在框架眼里必须只是"一段识别"）；
  ③ 节点总数与 get_node_data 回读是否与新真源一致。

用法：.venv\\Scripts\\python.exe -B tools\\experiments\\hall-popup\\probe_load.py
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

PACK = REPO / "maaracing_master"
CORE_PIPE = PACK / "core" / "resources" / "pipeline"
TREASURE_PIPE = PACK / "plugins" / "treasure" / "resources" / "pipeline"


class _StubReco(CustomRecognition):
    def analyze(self, context, argv):
        return None


class _StubAction(CustomAction):
    def run(self, context, argv) -> bool:
        return True


def merge(dirs: list[Path]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="hallpopup-load-"))
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.rglob("*.json*"):
            dest = tmp / f.relative_to(d)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
    return tmp


def new_resource() -> Resource:
    res = Resource()
    res.register_custom_recognition("MaaRM_Template", _StubReco())
    for name in ("MaaRM_Click", "MaaRM_Input", "MaaRM_Policy"):
        res.register_custom_action(name, _StubAction())
    return res


def main() -> int:
    print(f"core 目录在场 = {CORE_PIPE.is_dir()}（缺席是合法状态）")
    tmp = merge([CORE_PIPE, TREASURE_PIPE])
    try:
        res = new_resource()
        job = res.post_pipeline(str(tmp)).wait()
        print(f"post: failed={job.failed} succeeded={job.succeeded}")
        nodes = sorted(res.node_list or [])
        print(f"框架可见节点数 = {len(nodes)}")
        for n in nodes:
            if "待机" in n or "弹窗" in n or "聊天框" in n:
                print(f"    本轮新增 → {n}")

        print("\nget_node_data 回读（None/异常 = 该形态没被解析出来）：")
        for name in ("treasure.待机.dwell", "treasure.待机.wake",
                     "treasure.控制器指引弹窗.dwell", "treasure.控制器指引弹窗.close",
                     "treasure.大厅聊天框.anchor", "treasure.控制器指引弹窗.anchor"):
            try:
                data = res.get_node_data(name)
                print(f"  {name}\n      {str(data)[:300]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {name} -> 异常 {exc!r}")
        rc = 0 if not job.failed else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n临时目录已清理 = {not tmp.exists()}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
