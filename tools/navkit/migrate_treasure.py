#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S0 验收脚本：对真实 v2 配置产出 v3 迁移草稿 + 缺口报告（只读，不落运行时路径）。

用法：
    python tools/navkit/migrate_treasure.py [--out DIR]

- 草稿写到 --out 目录（缺省 tools/navkit/out/），文件名 treasure_assets.draft.json。
  这是评审用的草稿，不是运行时资产——运行时接入属 S1，另行审批。
- 缺口报告同时打印到 stdout（完整贴给用户过目）并写到 --out 下 gaps.txt。
- 全程只读：treasure_rois.json 与模板目录一个字节都不改。

semantic 输入源说明（§7.1）：本脚本只给最少的必要 semantic（module），其余
kind/owner/page/label/guarded_by 全部留给缺口清单——这正是 S0 的产出物：
先让"v2 里推不出来、必须人来定"的信息以清单形式现形，再谈 S1 的逐批上纸。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接以文件方式运行（不要求安装为包）
_PROJ = Path(__file__).resolve().parents[2]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from maaracing_assistant.core.navkit import diff_v2_v3, inspect_v2, migrate_v2_to_v3  # noqa: E402

MODULE = "treasure"
V2_PATH = (
    _PROJ / "archive" / "treasure_v2" / "treasure_rois.json"
)  # M4/E1：v2 已退役归档（只读历史对照）
IMAGE_DIR = _PROJ / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "image"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "out",
        help="草稿与缺口报告输出目录（缺省 tools/navkit/out/）",
    )
    args = parser.parse_args()

    import json

    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))

    rep = inspect_v2(doc, image_dirs=(IMAGE_DIR,))

    semantic = {"module": MODULE, "image_dirs": (IMAGE_DIR,)}
    v3, gaps = migrate_v2_to_v3(doc, semantic=semantic)
    diffs = diff_v2_v3(doc, v3)

    # ---- stdout：完整报告（给用户过目）----
    print("=" * 72)
    print("S0 迁移草稿 + 缺口报告（只读评审，不落运行时路径）")
    print("=" * 72)
    print(f"源文件      : {V2_PATH.relative_to(_PROJ)}")
    print(f"模板目录    : {IMAGE_DIR.relative_to(_PROJ)}")
    print()
    print("【v2 体检】")
    print(rep.summary())
    print()

    counts: dict[str, int] = {}
    for g in gaps:
        kind = g.split("]")[0].lstrip("[")
        counts[kind] = counts.get(kind, 0) + 1
    print(f"【缺口清单】共 {len(gaps)} 条，按分组统计：")
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<14s} {n}")
    print()
    for i, g in enumerate(gaps, 1):
        print(f"{i:>3}. {g}")

    print()
    print(f"【纯搬迁校验】diff_v2_v3 差异数 = {len(diffs)}")
    for d in diffs:
        print(f"  ! {d}")
    ok = not diffs
    print("  → 搬迁纯净" if ok else "  → 搬迁被污染！必须先修再评审")

    # ---- 落盘：草稿 + 报告（仅评审目录，不碰运行时）----
    args.out.mkdir(parents=True, exist_ok=True)
    draft_path = args.out / "treasure_assets.draft.json"
    draft_path.write_text(
        json.dumps(v3, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_lines = [
        "S0 迁移草稿缺口报告",
        f"源文件: {V2_PATH}",
        "",
        rep.summary(),
        "",
        f"缺口共 {len(gaps)} 条：",
        *[f"{i:>3}. {g}" for i, g in enumerate(gaps, 1)],
        "",
        f"diff_v2_v3 差异数 = {len(diffs)}（0 = 纯搬迁）",
    ]
    (args.out / "gaps.txt").write_text("\n".join(report_lines), encoding="utf-8")
    print()
    print(f"草稿  → {draft_path}")
    print(f"报告  → {args.out / 'gaps.txt'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
