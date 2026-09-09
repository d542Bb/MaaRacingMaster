# -*- coding: utf-8 -*-
"""P2b 诊断 v16（画面指纹）：真机场景3画面（鉴宝大厅-选择场次）的信号归属判定。

run15 当前画面 = 鉴宝大厅(选择场次)，但 boot 8s 零命中——与真机场景3
hall_session_cards 命中×4 矛盾。对这张画面逐项跑：
  1. 游戏大厅 dwell 三信号（hall_peak_appraise_card/act_goto_appraise_btn/hall_session_cards）
  2. 活动页面 dwell 两信号
  3. 鉴宝大厅 dwell 信号（hall_session_cards）
  4. 锚点节点 hall_session_cards 的 rect（点击目标）
全用 DEFAULT_SCALES 全尺度 + 0.75 阈值，打印每个 rect 内最高分与 box——
钉死「误检」还是「漏检」。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")

from maaracing_assistant.core.template_match import find_any_cs
from maaracing_assistant.core.wgcap import WgcCapture

user32 = ctypes.WinDLL("user32", use_last_error=True)

FRAME = Path("tools/navkit/out/diag_run15_frame.png")


def find_hwnd() -> int:
    h = user32.FindWindowW(None, "巅峰极速")
    return int(h) if h else 0


def main() -> None:
    import cv2
    bgr = cv2.imread(str(FRAME), cv2.IMREAD_COLOR)
    if bgr is None:
        print("[abort] 无存证帧")
        return
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    H, W = rgb.shape[:2]
    print(f"[frame] {W}x{H} 来自 {FRAME.name}（run15 存证，鉴宝大厅-选择场次画面）")

    trea = json.loads(Path(
        "maaracing_assistant/plugins/treasure/resources/nav/treasure.json"
    ).read_text(encoding="utf-8"))
    glob = json.loads(Path(
        "maaracing_assistant/core/resources/nav/global.json"
    ).read_text(encoding="utf-8"))
    image_dirs = [
        Path("maaracing_assistant/core/resources/image"),
        Path("maaracing_assistant/plugins/treasure/resources/image"),
    ]

    def probe_rect(tag: str, rect, names: list[str]):
        x1, y1, x2, y2 = rect
        roi = (int(x1 * W), int(y1 * H), int((x2 - x1) * W), int((y2 - y1) * H))
        box, score, name = find_any_cs(
            np.ascontiguousarray(rgb), [Path(n).stem for n in names], image_dirs,
            colorspace="rgb", threshold=0.75, roi=roi)
        flag = "HIT " if box else "    "
        print(f"[{flag}] {tag:46} rect=({x1:.2f},{y1:.2f},{x2:.2f},{y2:.2f}) "
              f"score={score:.3f} {name or ''} {box or ''}")

    # 1/2/3: dwell 信号组
    for fname, nodes in (("global", glob), ("treasure", trea)):
        for nname, nd in nodes.items():
            if not nd.get("_dwell"):
                continue
            r = nd.get("recognition")
            if isinstance(r, dict) and r.get("type") == "Or":
                for i, sub in enumerate(r["param"]["any_of"]):
                    p = sub.get("custom_recognition_param") or {}
                    probe_rect(f"{nname} 信号[{i}]", p.get("rect") or [0, 0, 1, 1],
                               p.get("templates") or [])
            elif isinstance(r, dict) and "custom_recognition" in r:
                p = r.get("custom_recognition_param") or {}
                probe_rect(f"{nname} 信号", p.get("rect") or [0, 0, 1, 1],
                           p.get("templates") or [])

    # 4: 锚点节点本体（点击目标）
    for nname, nd in {**glob, **trea}.items():
        if nname.endswith("hall_session_cards") and ".dwell" not in nname:
            p = nd.get("custom_recognition_param") or {}
            probe_rect(f"锚点 {nname}", p.get("rect") or [0, 0, 1, 1],
                       p.get("templates") or [])


if __name__ == "__main__":
    main()
