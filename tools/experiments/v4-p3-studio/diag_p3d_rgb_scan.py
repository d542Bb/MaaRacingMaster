# -*- coding: utf-8 -*-
"""P3d 收口：真源 MRA_Template 节点 rgb_strict 候选评估扫描（评审用，不改真源）。

依据引擎能力（schema custom.recognition.schema.json）：
  colorspace ∈ [gray, rgb, rgb_strict]；color_assert={rect, hue:[lo,hi]}（色相断言）。

评估判据（代词）：
  颜色强判别 → 建议 colorspace=rgb_strict 或加 color_assert 的节点，通常是：
    - 模板名/语义含 红/蓝/红黄蓝/彩蛋/横幅 且状态靠颜色区分；
    - 按钮颜色态（可点=红 vs 置灰）等灰阶匹配易混淆的对象。
  灰判存在性 → 保持 gray：纯结构 HUD、白底文字、装帧框（颜色不是判别量）。

输出：每节点 name / mode / rect / templates / colorspace(现状) / suggestion。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = [
    ROOT / "maaracing_assistant" / "core" / "resources" / "pipeline" / "global.json",
    ROOT / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "pipeline" / "treasure.json",
]

# 强颜色判别关键词：名字/模板含这些意为"靠颜色区分状态"
_COLOR_HINT = re.compile(r"red|blue|green|yellow|color|banner|egg|彩蛋|红|蓝|黄|绿", re.I)


def scan() -> None:
    for f in FILES:
        doc = json.loads(f.read_text(encoding="utf-8"))
        print(f"\n===== {f.relative_to(ROOT)} （{len(doc)} 节点）=====")
        for name, node in doc.items():
            rec = node.get("recognition")
            if not isinstance(rec, dict):
                continue
            # MPE 归一化形态：{"type":"Custom","param":{...}}
            param = rec.get("param") if isinstance(rec.get("param"), dict) else rec
            if not isinstance(param, dict) or param.get("mode") != "template":
                continue
            cs = param.get("colorspace", "rgb")  # plan §6：引擎默认彩色 rgb
            ca = "yes" if param.get("color_assert") else "no"
            tpls = param.get("templates", [])
            tplstr = ",".join(tpls) if isinstance(tpls, list) else str(tpls)
            hint_hit = bool(_COLOR_HINT.search(name)) or bool(_COLOR_HINT.search(tplstr))
            sug = "rgb_strict/color_assert 候选" if hint_hit else "gray 足够"
            rect = param.get("rect", "")
            print(f"  {name}")
            print(f"      colorspace={cs} color_assert={ca} rect={rect}")
            print(f"      tpls={tplstr}")
            print(f"      -> {sug}")


if __name__ == "__main__":
    scan()