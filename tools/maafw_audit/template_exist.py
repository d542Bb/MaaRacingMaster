# -*- coding: utf-8 -*-
"""MaaFw 生态审计：真源引用的模板文件存在性 + 未引用模板清单。"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "maaracing_master"
IMG_DIRS = [PACK / "core" / "resources" / "image",
            PACK / "core" / "resources" / "startup" / "image",
            PACK / "plugins" / "speedrush" / "resources" / "image"]
FILES = [PACK / "core" / "resources" / "startup" / "pipeline.json",
         PACK / "plugins" / "speedrush" / "resources" / "pipeline" / "speedrush.json",
         PACK / "plugins" / "speedrush" / "resources" / "policy" / "decision.json",
         PACK / "plugins" / "speedrush" / "resources" / "policy" / "hud_regions.json"]

used: set[str] = set()
for f in FILES:
    doc = json.loads(f.read_text(encoding="utf-8"))
    for t in re.findall(r'"([^"]+\.(?:png|jpg))"', json.dumps(doc, ensure_ascii=False)):
        used.add(t)

have: dict[str, Path] = {}
for d in IMG_DIRS:
    for p in d.glob("*.png"):
        have[p.name] = d
    for p in d.glob("*.jpg"):
        have[p.name] = d

missing = sorted(t for t in used if t not in have)
orphans = sorted(set(have) - used)
print(f"真源引用模板: {len(used)}")
print(f"缺失: {missing or '（无）'}")
print(f"未引用模板（磁盘有图但真源无引用）: {orphans or '（无）'}")
