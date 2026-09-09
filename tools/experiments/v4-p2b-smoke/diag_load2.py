# -*- coding: utf-8 -*-
"""P2b 诊断 v2：Toolkit 日志初始化 + Resource 冷启动时序假设验证。

假设：post_pipeline 失败与内容无关，与「Resource 首次加载」时序有关。
验证：全新 Resource 对同一目录连续 post 三次，观察首败后继成？
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

from maa.toolkit import Toolkit

Toolkit.init_option(str(Path(__file__).resolve().parent / "logs"), "")

from maa.resource import Resource  # noqa: E402

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")


def post(tag: str, res: Resource, target: Path) -> bool:
    ok = bool(res.post_pipeline(str(target)).wait().succeeded)
    print(f"[{tag}] {target.name} -> {ok}")
    return ok


def main() -> None:
    res = Resource()
    for i in (1, 2, 3):
        post(f"cold-{i}", res, CORE_NAV)
    post("warm-plugin", res, PLUGIN_NAV)

    res2 = Resource()
    post("cold2-plugin", res2, PLUGIN_NAV)
    post("warm2-core", res2, CORE_NAV)
    print("日志：tools/experiments/v4-p2b-smoke/logs/maa.log")


if __name__ == "__main__":
    main()
