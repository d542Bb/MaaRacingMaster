# -*- coding: utf-8 -*-
"""speedrush 真源「可由生成器复算」的回归锁。

真源（``plugins/speedrush/resources/pipeline/speedrush.json``）是**生成**出来的：
模板与 ROI 规格在 ``tools/experiments/speedrush_templates/templates_roi.json``，
节点装配在 ``build_truth.py``。这条链一旦断掉——有人手工改真源，或生成器漏掉某个
字段——下一次生成就会把改动静默抹掉。

**实测过一次**（2026-09-16）：驾驶页锚点的阈值与节拍（0.5 / rate_limit 0 /
timeout 400）只活在"生成之后手工编辑"里，跑一次生成器就会把阈值回到 0.8、节拍
回到框架默认；而那两处正是实机测出来的修复——阈值 0.8 会在漂移段贴线抖动、被误判
"阶段结束"，默认节拍把驾驶主循环压到约 1Hz。生成器当时既没输出它们，也就没人会
在改生成器时想起它们。

故本测试断言 **生成器输出 == 入库真源**（逐行相等，忽略换行符差异）。它不锁任何
具体数值：要改数值就改生成器或它的输入表，改完两边自然一致；只想手工改真源则会红。
本文件不含重依赖，CI 上照常执行（与 ``test_speedrush_gating.py`` 不同，后者整文件
依赖 maa 运行时）。
"""

from __future__ import annotations

import difflib
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TPL_DIR = REPO / "tools" / "experiments" / "speedrush_templates"
TRUTH = (REPO / "maaracing_master" / "plugins" / "speedrush"
         / "resources" / "pipeline" / "speedrush.json")


def _builder():
    """按路径加载生成器脚本（它不在包内，只能这样取）。"""
    spec = importlib.util.spec_from_file_location(
        "speedrush_build_truth", TPL_DIR / "build_truth.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_truth_matches_generator_output(tmp_path) -> None:
    """入库真源必须与生成器当前输出一致。

    不一致有两个正当修法：跑一遍生成器（手工改的是错的那份），或把手工改动搬进
    生成器 / ``templates_roi.json``（生成器落后了）。没有第三个——留着手工差异，
    下一次生成就会把它抹掉。
    """
    mod = _builder()
    # 生成器的两个路径常量是相对仓库根写的；这里改成绝对路径，使测试与 CWD 无关
    mod.ROI_JSON = str(TPL_DIR / "templates_roi.json")
    mod.OUT = str(tmp_path / "speedrush.json")
    mod.main()

    raw = (tmp_path / "speedrush.json").read_text(encoding="utf-8")
    cur = TRUTH.read_text(encoding="utf-8")
    if raw.replace("\r\n", "\n") != cur.replace("\r\n", "\n"):
        diff = "\n".join(list(difflib.unified_diff(
            cur.splitlines(), raw.splitlines(), "入库真源", "生成器输出",
            lineterm="", n=1))[:40])
        raise AssertionError(
            "入库真源与生成器输出不一致——手工改动会被下一次生成静默抹掉；"
            "真源里凡有「生成后再手工编辑」的字段，请在生成器（NODE_OVERRIDES）"
            "或 templates_roi.json 里表达：\n" + diff)


def test_generator_writes_lf(tmp_path) -> None:
    """生成器必须按仓库约定写 LF（``.gitattributes`` 里真源是 ``eol=lf``）。

    Windows 上文本模式默认写 CRLF：真源会在 diff 里整文件翻一遍，把真正的改动盖掉
    （不是内容错，但审查时看不见改了什么）。
    """
    mod = _builder()
    mod.ROI_JSON = str(TPL_DIR / "templates_roi.json")
    mod.OUT = str(tmp_path / "speedrush.json")
    mod.main()
    assert b"\r\n" not in (tmp_path / "speedrush.json").read_bytes()