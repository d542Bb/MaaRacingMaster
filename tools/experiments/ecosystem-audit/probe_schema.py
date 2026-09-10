# 审计实验：提取官方 pipeline.schema.json 快照的关键结构（只读）
from pathlib import Path
import json
import sys

PATH = str(Path(__file__).resolve().parents[3] / "tools/navkit/schema/pipeline.schema.json")

def main():
    d = json.load(open(PATH, encoding="utf-8"))
    print("$comment:", d.get("$comment"))
    pp = d["patternProperties"]
    print("patternProperties keys:", list(pp.keys()))
    node = pp[list(pp.keys())[0]]
    props = node.get("properties", {})
    print("node top-level type:", node.get("type"))
    print("node props count:", len(props))
    print("recognition enum:", props.get("recognition", {}).get("enum"))
    print("action enum:", props.get("action", {}).get("enum"))
    print("prop names:", sorted(props.keys()))
    defs = d.get("$defs", {})
    print("defs:", sorted(defs.keys()))
    for k in ("RecognitionEnum", "ActionEnum"):
        if k in defs:
            print(k, "->", json.dumps(defs[k], ensure_ascii=False)[:3000])
    node = pp[list(pp.keys())[0]]
    print("node keys:", list(node.keys()))
    for comb in ("oneOf", "anyOf", "allOf"):
        if comb in node:
            print("node", comb, "count:", len(node[comb]))
    print("additionalProperties:", node.get("additionalProperties"))

if __name__ == "__main__":
    main()
