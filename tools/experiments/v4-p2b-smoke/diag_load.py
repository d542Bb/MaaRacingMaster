# -*- coding: utf-8 -*-
"""P2b 诊断：v4 真源 post_pipeline 逐文件定位失败点（真机首炸复现）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

from maa.resource import Resource
from maa.tasker import Tasker
from maa.controller import CustomController

CORE_NAV = Path("maaracing_assistant/core/resources/nav")
PLUGIN_NAV = Path("maaracing_assistant/plugins/treasure/resources/nav")


class _Noop(CustomController):
    """最小帧注入桩：只为了 bind 后资源校验完整链路（诊断用）。"""

    def screencap(self):
        import numpy as np
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "diag"

    def start_app(self, intent):
        return True

    def stop_app(self, intent):
        return True

    def click(self, x, y):
        return True

    def swipe(self, *a):
        return True

    def touch_down(self, *a):
        return True

    def touch_move(self, *a):
        return True

    def touch_up(self, *a):
        return True

    def click_key(self, k):
        return True

    def input_text(self, t):
        return True

    def key_down(self, k):
        return True

    def key_up(self, k):
        return True


def try_post(tag: str, res: Resource, target: Path) -> None:
    job = res.post_pipeline(str(target))
    ok = bool(job.wait().succeeded)
    print(f"[{tag}] {target} -> succeeded={ok}")


def main() -> None:
    res = Resource()
    # 逐文件
    try_post("single", res, CORE_NAV / "global.json")
    try_post("single", res, PLUGIN_NAV / "treasure.json")
    # 目录两连发（生产形态）
    try_post("dir", res, CORE_NAV)
    try_post("dir", res, PLUGIN_NAV)
    # 目录单发（只 plugin）
    res2 = Resource()
    try_post("dir-only", res2, PLUGIN_NAV)
    # bind + 校验完整链
    t = Tasker()
    t.bind(res, _Noop())
    t.add_context_sink(lambda d: None)
    print("[bind] ok")
    print("提示：MaaFramework 详细日志见 debug/maa.log（最近一次加载失败的 ERROR 行）")


if __name__ == "__main__":
    main()
