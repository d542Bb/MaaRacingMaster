#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B2/B3 深探：MaaFW 对真源各形态的实际解析结果。

关注四点：
  1. v2 嵌套对象形态 recognition={type:Or,param:{any_of:[...]}} 是否被接受、
     子项 custom_recognition / custom_recognition_param 是否保留；
  2. 根级 `_` 前缀扩展字段是否被框架保留（attach / 丢弃）；
  3. custom_action_param / custom_recognition_param 是否黑盒透传；
  4. timeout / rate_limit 缺省值如何补齐（对照协议默认值）。
"""
from __future__ import annotations

import json
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ecoaudit-probe-"))
    for d in (CORE_PIPE, TREASURE_PIPE):
        for f in d.rglob("*.json*"):
            dest = tmp / f.relative_to(d)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
    try:
        res = Resource()
        res.register_custom_recognition("MRA_Template", _StubReco())
        res.register_custom_action("MRA_Click", _StubAction())
        res.register_custom_action("MRA_Policy", _StubAction())
        job = res.post_pipeline(str(tmp)).wait()
        print(f"post_pipeline failed={job.failed}")

        print("\n### 1. Or/any_of 对象形态节点")
        data = res.get_node_data("treasure.第1回合出价.dwell")
        print(json.dumps(data.get("recognition"), ensure_ascii=False, indent=1))

        print("\n### 2. 根级 `_` 扩展字段是否保留 —— 对比原始 JSON 与框架解析")
        raw = json.loads((TREASURE_PIPE / "treasure.json").read_text(encoding="utf-8"))
        node_raw = raw["treasure.第1回合出价.dwell"]
        print("  原始键:", sorted(node_raw.keys()))
        print("  框架键:", sorted(data.keys()))
        print("  `_` 字段是否出现在框架解析结果中:",
              [k for k in data if k.startswith("_")])
        print("  attach 字段内容:", data.get("attach"))

        print("\n### 3. custom param 黑盒透传")
        d2 = res.get_node_data("global.hall_peak_appraise_card.rhall_to_treasure.0")
        print("  原始 recognition_param:",
              json.dumps(node_raw.get("custom_recognition_param"), ensure_ascii=False))
        print("  框架 action:", json.dumps(d2.get("action"), ensure_ascii=False))
        d3 = res.get_node_data("treasure.policy_loop")
        print("  policy_loop action:", json.dumps(d3.get("action"), ensure_ascii=False))
        print("  policy_loop recognition:", json.dumps(d3.get("recognition"), ensure_ascii=False))

        print("\n### 4. timeout / rate_limit 缺省补齐（对照协议默认 timeout=20000, rate_limit=1000）")
        for n in sorted(res.node_list):
            d = res.get_node_data(n)
            print(f"  {n:52s} timeout={d.get('timeout')} rate_limit={d.get('rate_limit')} "
                  f"pre_delay={d.get('pre_delay')} post_delay={d.get('post_delay')} "
                  f"inverse={d.get('inverse')} enabled={d.get('enabled')} max_hit={d.get('max_hit')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
