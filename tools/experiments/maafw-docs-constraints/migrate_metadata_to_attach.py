# -*- coding: utf-8 -*-
"""一次性迁移（已执行，留档可复跑——对 attach 形态幂等）：

pipeline 真源节点级 `_xxx` 元数据键 → 官方文档化扩展位 `attach`。

背景：实测（本目录 README + probe_parse.py）证实 MaaFW 5.12.3 解析器丢弃节点级
`_xxx` 未文档化键（get_node_data 读不回），而 `attach` 是 3.1 协议唯一文档化的
节点扩展字段（dict merge、框架保留可回读）。
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FILES = [
    REPO / "maaracing_assistant/core/resources/pipeline/global.json",
    REPO / "maaracing_assistant/plugins/treasure/resources/pipeline/treasure.json",
]


def main() -> int:
    total = 0
    for p in FILES:
        raw = p.read_text(encoding="utf-8")
        doc = json.loads(raw)
        out = {}
        moved = 0
        for name, body in doc.items():
            if not isinstance(body, dict):
                out[name] = body
                continue
            meta = {k: v for k, v in body.items() if k.startswith("_")}
            if not meta:
                out[name] = body
                continue
            moved += len(meta)
            nb = {k: v for k, v in body.items() if not k.startswith("_")}
            existing = nb.get("attach") or {}
            nb["attach"] = {**meta, **existing}
            out[name] = nb
        text = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True)
        if raw.endswith("\n"):
            text += "\n"
        p.write_text(text, encoding="utf-8")
        total += moved
        print(f"{p.name}: 迁移 {moved} 个元数据键，节点数 {len(out)}")
    print(f"合计 {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
