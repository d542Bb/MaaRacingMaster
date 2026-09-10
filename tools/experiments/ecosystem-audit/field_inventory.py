# -*- coding: utf-8 -*-
"""生态审计 B2：真源逐节点字段清点，对照官方 pipeline.schema.json 快照。

口径：
- 官方字段全集 = 遍历 schema 快照所有 properties 键名并集（含算法专属字段）；
- 枚举合法 = recognition/action 值必须落在官方 RecognitionEnum/ActionEnum；
- 节点根键：以 `_` 开头 = 编辑器扩展（合法，extras 承载）；以 `$` 开头 = 官方
  保留/编辑器产物（真源不得出现）；其余必须属于官方字段全集；
- v1 平铺 / v2 嵌套两种形态都解析，And/Or 内联子识别递归检查。
只读，不改真源。
"""
import json
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[3] / "tools/navkit/schema/pipeline.schema.json"
FILES = {
    "global": Path(__file__).resolve().parents[3] / "maaracing_assistant/core/resources/pipeline/global.json",
    "treasure": Path(__file__).resolve().parents[3] / "maaracing_assistant/plugins/treasure/resources/pipeline/treasure.json",
}


def collect_props(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "properties" and isinstance(v, dict):
                acc.update(v.keys())
            else:
                collect_props(v, acc)
    elif isinstance(obj, list):
        for it in obj:
            collect_props(it, acc)


def main():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    official: set[str] = set()
    collect_props(schema, official)
    defs = schema["$defs"]
    reco_enum = set(defs["RecognitionEnum"]["enum"])
    act_enum = set(defs["ActionEnum"]["enum"])
    print(f"官方字段全集规模: {len(official)}  识别枚举: {len(reco_enum)}  动作枚举: {len(act_enum)}")

    problems = []
    for label, path in FILES.items():
        doc = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n== {label}.json: {len(doc)} 节点 ==")
        used_keys: dict[str, int] = {}
        for name, node in doc.items():
            check_node(name, node, official, reco_enum, act_enum, problems, used_keys)
        print("根键使用分布:", json.dumps(used_keys, ensure_ascii=False, sort_keys=True))

    print("\n== 违规清单 ==")
    if problems:
        for p in problems:
            print("[X]", p)
    else:
        print("（无）")


def check_ref_list(key, val, name, problems):
    if isinstance(val, str):
        return
    if isinstance(val, list):
        for r in val:
            if isinstance(r, str):
                continue
            if isinstance(r, dict) and isinstance(r.get("name"), str):
                for rk in r:
                    if rk not in ("name", "jump_back", "anchor"):
                        problems.append(f"{name}.{key}: NodeAttr 未知键 {rk}")
                continue
            problems.append(f"{name}.{key}: 元素形态非法 {r!r}")
        return
    problems.append(f"{name}.{key}: 形态非法 {val!r}")


def check_recog_like(node, where, official, reco_enum, act_enum, problems):
    reco = node.get("recognition")
    if isinstance(reco, dict):  # v2
        if reco.get("type") not in reco_enum:
            problems.append(f"{where}: v2 recognition.type 非法 {reco.get('type')!r}")
        param = reco.get("param", {})
        for k in param:
            if k not in official:
                problems.append(f"{where}: recognition.param 未知键 {k}")
        for sub in param.get("any_of", []) + param.get("all_of", []):
            if isinstance(sub, dict):
                check_recog_like(sub, f"{where}.any_of/all_of", official,
                                 reco_enum, act_enum, problems)
            elif not isinstance(sub, str):
                problems.append(f"{where}: 子识别元素非法 {sub!r}")
    elif isinstance(reco, str):  # v1
        if reco not in reco_enum:
            problems.append(f"{where}: v1 recognition 非法 {reco!r}")
    for k in node:
        if k.startswith("_"):
            continue
        if k.startswith("$"):
            problems.append(f"{where}: 真源出现 $ 保留键 {k}（红线）")
        elif k not in official:
            problems.append(f"{where}: 非官方根键 {k}")
    if isinstance(node.get("rate_limit"), bool) or not isinstance(node.get("rate_limit", 0), int):
        problems.append(f"{where}: rate_limit 非整数 {node.get('rate_limit')!r}")
    if not isinstance(node.get("timeout", 0), int):
        problems.append(f"{where}: timeout 非整数 {node.get('timeout')!r}")
    for key in ("next", "on_error"):
        if key in node:
            check_ref_list(key, node[key], where, problems)


def check_node(name, node, official, reco_enum, act_enum, problems, used_keys):
    for k in node:
        used_keys[k] = used_keys.get(k, 0) + 1
    if not isinstance(node, dict):
        problems.append(f"{name}: 节点非 object")
        return
    act = node.get("action")
    if isinstance(act, dict):
        if act.get("type") not in act_enum:
            problems.append(f"{name}: v2 action.type 非法 {act.get('type')!r}")
        for k in act.get("param", {}):
            if k not in official:
                problems.append(f"{name}: action.param 未知键 {k}")
    elif isinstance(act, str):
        if act not in act_enum:
            problems.append(f"{name}: v1 action 非法 {act!r}")
    check_recog_like(node, name, official, reco_enum, act_enum, problems)


if __name__ == "__main__":
    main()
