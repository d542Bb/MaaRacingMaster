# -*- coding: utf-8 -*-
"""生态审计 B2/校验器第2条：真源引用的模板文件存在性 + 未引用模板清单。"""
import json
import re
from pathlib import Path

PACK = Path(__file__).resolve().parents[3] / "maaracing_assistant"
IMG_DIRS = [PACK / "core" / "resources" / "image",
            PACK / "plugins" / "treasure" / "resources" / "image"]
FILES = [PACK / "core" / "resources" / "pipeline" / "global.json",
         PACK / "plugins" / "treasure" / "resources" / "pipeline" / "treasure.json",
         PACK / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"]

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
