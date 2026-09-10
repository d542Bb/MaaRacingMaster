# -*- coding: utf-8 -*-
"""P4b 等价对拍：v3 路径（Assets+compile_detection）vs v4 路径（policy.json→v4_source）。

判定 DetectionPlan / Policies / spec 消费面三层逐字段等价——通过后才允许切换
消费点并删除 v3 真源。金标测试（runtime_equiv）随后固化新路径输出值。

用法：.venv\\Scripts\\python.exe tools\\experiments\\v4-p4b-source\\diag_p4b_plan_equiv.py
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_assistant.core.navkit import Assets, compile_detection  # noqa: E402
from maaracing_assistant.core.navkit.v4_source import load_nav_source  # noqa: E402

V3_PATH = REPO / "maaracing_assistant/plugins/treasure/resources/config/treasure_assets.json"
POLICY_PATH = REPO / "maaracing_assistant/plugins/treasure/resources/policy/treasure.policy.json"
IMAGE_DIR = REPO / "maaracing_assistant/plugins/treasure/resources/image"

diffs: list[str] = []


def cmp(tag: str, old, new) -> None:
    if old != new:
        diffs.append(f"{tag}: old={old!r} new={new!r}")


def main() -> int:
    assets = Assets.load(V3_PATH, module="treasure", image_dirs=(IMAGE_DIR,))
    old_plan = compile_detection(assets)
    nav = load_nav_source(POLICY_PATH)
    new_plan = nav.plan

    # ---- DetectionPlan 顶层（frozenset/tuple 归一后比） ----
    for f in ("stage_order", "global_anchors", "scales", "default_threshold",
              "margin_default", "dynamic_narrow", "stage_stage"):
        cmp(f"plan.{f}", asdict(old_plan)[f], asdict(new_plan)[f])
    cmp("plan.active", {k: sorted(v) for k, v in old_plan.active.items()},
        {k: sorted(v) for k, v in new_plan.active.items()})
    cmp("plan.ocr_keys", {k: sorted(v) for k, v in old_plan.ocr_keys.items()},
        {k: sorted(v) for k, v in new_plan.ocr_keys.items()})
    cmp("plan.detect_anchors", sorted(old_plan.detect_anchors),
        sorted(new_plan.detect_anchors))

    # ---- AnchorSpec 逐锚点 ----
    cmp("plan.spec 键集", sorted(old_plan.spec), sorted(new_plan.spec))
    for name in old_plan.spec:
        o, n = old_plan.spec[name], new_plan.spec.get(name)
        if n is None:
            continue
        for k in ("kind", "stage", "stage_priority", "templates", "threshold",
                  "scales", "guarded_by"):
            cmp(f"spec.{name}.{k}", getattr(o, k), getattr(n, k))
        cmp(f"spec.{name}.rect", [round(x, 12) for x in o.rect],
            [round(x, 12) for x in n.rect])
        # arbitration：v3 形态是 dataclass asdict 全默认值形，v4 透传只含显式键；
        # detector 消费全部走 .get(key, default)（L247-249/279/300/312/380），
        # 与缺键等价——按「非默认值键」比对。
        arb = {k: v for k, v in dict(o.arbitration or {}).items()
               if v not in (0.0, False, {}, None)}
        cmp(f"spec.{name}.arbitration", arb,
            {k: v for k, v in dict(n.arbitration or {}).items()
             if v not in (0.0, False, {}, None)})

    # ---- Policies（决策引擎输入） ----
    # 条件 tuple 在 migrate 落盘时按键序重排过；runtime 求值为 AND 语义与
    # 顺序无关 → 按「when 条件集合 + 决策字段」的语义形态比对，不裸比 tuple 序。
    def norm_rule(r):
        d = asdict(r)
        d["when"] = sorted(json.dumps(c, sort_keys=True, ensure_ascii=False, default=str)
                           for c in d["when"])
        d.pop("raw", None)
        return d

    cmp("policies.stage_map", dict(assets.policies.stage_map), dict(nav.policies.stage_map))
    cmp("policies.rules 数", len(assets.policies.rules), len(nav.policies.rules))
    cmp("policies.rules 语义", [norm_rule(r) for r in assets.policies.rules],
        [norm_rule(r) for r in nav.policies.rules])
    cmp("policies.tuning", dict(assets.policies.tuning), dict(nav.policies.tuning))
    cmp("policies.schema_ver", assets.policies.schema_ver, nav.policies.schema_ver)

    # ---- spec 消费面（module 私有装载器形态：.rect.as_list()/.kind/.domain/.order） ----
    cmp("spec(Anchor) 键集", sorted(assets.anchors), sorted(nav.spec))
    for name, oa in assets.anchors.items():
        na = nav.spec.get(name)
        if na is None:
            continue
        cmp(f"anchor.{name}.kind", oa.kind, na.kind)
        cmp(f"anchor.{name}.rect", oa.rect.as_list(), na.rect.as_list())
        cmp(f"anchor.{name}.templates", tuple(oa.templates), tuple(na.templates))
        cmp(f"anchor.{name}.threshold", oa.threshold, na.threshold)
        cmp(f"anchor.{name}.order", oa.order, na.order)
        cmp(f"anchor.{name}.domain", oa.domain, na.domain)
        cmp(f"anchor.{name}.guarded_by", oa.guarded_by, na.guarded_by)

    if diffs:
        print(f"[FAIL] {len(diffs)} 处不一致：")
        for d in diffs[:40]:
            print("  -", d)
        return 1
    print(f"[OK] 全字段等价：plan.spec={len(old_plan.spec)} 锚点、"
          f"rules={len(assets.policies.rules)} 条、detect_anchors={len(old_plan.detect_anchors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
