#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C 类实验：MaaFW 里「不复制识别规格」到底有哪几条路。

背景：MaaFW 无节点继承（probe_inherit.py 实证），节点名全城唯一。那么"公共锚点
被各模块引用"这件事，协议给了哪些机制、各自在解析期还是运行期绑定、对 Custom
识别是否可用？逐条探：

  R1 Or/And 的 any_of/all_of 按节点名引用（v5.7）——解析期就展开成子识别，还是留名字？
  R2 被引用的"纯信号节点"（无 next 无 action）能否独立存在、能否从不带 Custom 的节点引用？
  R3 roi / target 填节点名（字符串）——引用的是"定义"还是"该节点上次跑出来的框"？
  R4 anchor + [Anchor] 引用——未设置的锚点按协议应"跳过"，加载期是否报不闭合？
  R5 default_pipeline.json 的类型默认值能下沉哪些字段（attach 是 dict merge 还是覆盖？）

用法：.venv\\Scripts\\python.exe tools/experiments/pipeline-inheritance/probe_refreuse.py
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

CUSTOM = "MaaRM_Template"


class _StubReco(CustomRecognition):
    def analyze(self, context, argv):
        return self.AnalyzeResult(box=None, detail={})


class _StubAction(CustomAction):
    def run(self, context, argv) -> bool:
        return True


def _sig(tag: str) -> dict:
    """一个"公共锚点"该长什么样：只有识别规格，没有动作、没有出口。"""
    return {
        "recognition": {
            "type": "Custom",
            "param": {
                "custom_recognition": CUSTOM,
                "custom_recognition_param": {"mode": "template", "templates": [f"{tag}.png"],
                                             "rect": [0.1, 0.1, 0.2, 0.2]},
            },
        },
        "attach": {"_signal": True, "_tag": tag},
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-refreuse-"))
    try:
        graph = {
            "common.idle_signal": _sig("hall_idle"),          # R2 纯信号节点
            "common.back_action": {                            # 公共动作节点（有动作、无出口）
                "recognition": {"type": "Custom", "param": {
                    "custom_recognition": CUSTOM,
                    "custom_recognition_param": {"mode": "template",
                                                 "templates": ["back_btn.png"]}}},
                "action": {"type": "Custom", "param": {"custom_action": "MaaRM_Click"}},
                "attach": {"_action": True},
            },
            "mod_a.dwell": {                                   # R1：Or 按名引用公共信号
                "recognition": {"type": "Or", "param": {"any_of": ["common.idle_signal"]}},
                "next": ["mod_a.tail"],
                "attach": {"_dwell": True},
            },
            "mod_a.tail": {"recognition": "DirectHit", "action": "DoNothing"},
            "mod_b.dwell": {                                   # R1：And 按名引用 + 与内联混用
                "recognition": {"type": "And", "param": {"all_of": [
                    "common.idle_signal",
                    {"recognition": {"type": "Custom", "param": {
                        "custom_recognition": CUSTOM,
                        "custom_recognition_param": {"mode": "template",
                                                     "templates": ["own.png"]}}}},
                ]}},
                "next": ["mod_a.tail"],
            },
            "mod_c.roi_ref": {                                 # R3：roi 填节点名
                "recognition": {"type": "TemplateMatch", "param": {
                    "template": ["x.png"], "roi": "common.idle_signal"}},
                "action": {"type": "Click", "param": {"target": "[Anchor]myanchor"}},
                "next": ["mod_a.tail"],
            },
            "mod_d.anchor_set": {                              # R4：设锚点 + [Anchor] 引用
                "recognition": "DirectHit", "action": "DoNothing",
                "anchor": "myanchor",
                "next": ["[Anchor]myanchor"],
            },
            "mod_e.missing_anchor": {                          # R4：引用从未设置的锚点
                "recognition": "DirectHit", "action": "DoNothing",
                "next": ["mod_a.tail"], "on_error": ["[Anchor]never_set"],
            },
            "mod_f.bogus_ref": {                               # R6：any_of 里引用不存在的节点名
                "recognition": {"type": "Or", "param": {"any_of": ["nope.does_not_exist"]}},
                "next": ["mod_a.tail"],
            },
        }
        (tmp / "graph.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
        (tmp / "default_pipeline.json").write_text(json.dumps({
            "Default": {"rate_limit": 1234, "timeout": 4321},
            "Custom": {"recognition": "Custom", "custom_recognition": CUSTOM},
        }, ensure_ascii=False), encoding="utf-8")

        res = Resource()
        res.register_custom_recognition(CUSTOM, _StubReco())
        res.register_custom_action("MaaRM_Click", _StubAction())
        job = res.post_pipeline(str(tmp)).wait()
        print(f"[load] failed={job.failed}  节点数={len(res.node_list)}")
        print(f"[load] node_list={sorted(res.node_list)}")

        print("\n### R1/R2 Or/And 按名引用被解析成什么（解析期展开 or 留名字）")
        for n in ("mod_a.dwell", "mod_b.dwell"):
            d = res.get_node_data(n)
            print(f"  {n}.recognition = {json.dumps(d.get('recognition'), ensure_ascii=False)}")

        print("\n### R3 roi 字符串 / target [Anchor] 字符串的解析结果")
        d = res.get_node_data("mod_c.roi_ref")
        print("  mod_c.roi_ref.recognition =", json.dumps(d.get("recognition"), ensure_ascii=False))
        print("  mod_c.roi_ref.action      =", json.dumps(d.get("action"), ensure_ascii=False))

        print("\n### R4 anchor 与 [Anchor] 引用是否过 PipelineChecker")
        for n in ("mod_d.anchor_set", "mod_e.missing_anchor"):
            d = res.get_node_data(n)
            print(f"  {n}: anchor={d.get('anchor')} next={json.dumps(d.get('next'), ensure_ascii=False)}"
                  f" on_error={json.dumps(d.get('on_error'), ensure_ascii=False)}")

        print("\n### R5 default_pipeline.json 下沉效果（对照 rate_limit=1234/timeout=4321）")
        for n in sorted(res.node_list):
            d = res.get_node_data(n)
            print(f"  {n:22s} rate_limit={d.get('rate_limit')} timeout={d.get('timeout')} "
                  f"attach={json.dumps(d.get('attach'), ensure_ascii=False)}")

        print("\n### 纯信号节点被 get_node_data 读回时，next/on_error 是什么（'无出口'形态确认）")
        d = res.get_node_data("common.idle_signal")
        print("  common.idle_signal: next=", d.get("next"), " on_error=", d.get("on_error"),
              " action=", json.dumps(d.get("action"), ensure_ascii=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
