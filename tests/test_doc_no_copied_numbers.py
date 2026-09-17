# -*- coding: utf-8 -*-
"""文档层真源守卫：入库 wiki 不得抄录可机检的真源数值。

判据出处：AGENTS.md「信源路由」的守则（2026-09-16，外部审查 §4.0 实证——
三处文档抄「12 阶段」而 policy.json 真源已 15 项）。守则若只靠 agent 记性
必然复发，本文件是其机检形态。

当前锁定两类已实证漂移的抄录：
- 「<数字> 阶段」——阶段数唯一真源是 policy.json perception.stages.order；
- 「NavKit v3」——v3 五段 schema 已于 P4a 整删，现存形态为 v4。

范围含全部知识文档（项目地图 / core 层 / 各活动域 CODE_WIKI）；update_log.md 与 RULES.md 不在内——
changelog 记录版本事实、RULES 记录游戏事实，各有裁决者（见分层判据）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = [
    REPO / "docs" / "CODE_WIKI.md",
    REPO / "maaracing_master" / "core" / "CODE_WIKI.md",
    REPO / "maaracing_master" / "plugins" / "treasure" / "CODE_WIKI.md",
    REPO / "maaracing_master" / "plugins" / "speedrush" / "CODE_WIKI.md",
]

# 阶段数抄录：数字紧邻「阶段」（允许中间一个空格）；(?<![\d.]) 排除
# 「### 2.2 阶段流程」这类节号——前导点/数字说明它是编号而非抄录值
STAGE_COUNT = re.compile(r"(?<![\d.])\d+\s*阶段")
# v3 资产表述：NavKit 与 v3 紧邻（「v3→v4 迁移史」类叙述用「→」隔开，不误伤）
V3_ASSET = re.compile(r"NavKit\s+v3(?!\s*→)")


def test_docs_carry_no_copied_stage_counts():
    offenders = []
    for doc in DOCS:
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if STAGE_COUNT.search(line):
                offenders.append(f"{doc.name}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "文档抄录了阶段数——改为指向 treasure.policy.json "
        "perception.stages.order：\n" + "\n".join(offenders)
    )


def test_docs_carry_no_v3_asset_claims():
    offenders = []
    for doc in DOCS:
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if V3_ASSET.search(line):
                offenders.append(f"{doc.name}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "文档仍以「NavKit v3 资产」描述现行形态（v3 schema 已整删）：\n"
        + "\n".join(offenders)
    )
