# -*- coding: utf-8 -*-
"""A0-1：B 链路最小验证（CustomController 精确喂帧）。

验「闭合点 2」每一段：
  1. post_task(入口, pipeline_override) 能否把 custom_action_param 覆盖进节点
  2. CustomAction.run(context, argv) 里 context 是否可得
  3. context.run_recognition(节点名, 传入帧) 是否用「我传的帧」而不是 controller 截图
  4. RecognitionDetail.hit 是否可读
  5. 通道契约：analyze 内无条件 BGR→RGB，故传入帧必须 BGR

不碰生产代码。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

REPO = Path(__file__).resolve().parents[3]
CORE_PIPELINE = REPO / "maaracing_master/core/resources/pipeline"
SPEEDRUSH_PIPELINE = REPO / "maaracing_master/plugins/speedrush/resources/pipeline"

RESULTS: dict[str, dict] = {}
TRACE: list[str] = []


class FrameController(CustomController):
    """截图固定返回 self.frame；所有输入直接成功。"""

    def __init__(self, frame: np.ndarray) -> None:
        self.frame = frame
        self.caps = 0
        super().__init__()

    def connect(self) -> bool:
        return True

    def request_uuid(self) -> str:
        return "a0-frame-controller"

    def start_app(self, intent: str) -> bool:
        return True

    def stop_app(self, intent: str) -> bool:
        return True

    def screencap(self) -> np.ndarray:
        self.caps += 1
        return self.frame

    def click(self, x: int, y: int) -> bool:
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int) -> bool:
        return True

    def touch_down(self, contact: int, x: int, y: int, pressure: int) -> bool:
        return True

    def touch_move(self, contact: int, x: int, y: int, pressure: int) -> bool:
        return True

    def touch_up(self, contact: int) -> bool:
        return True

    def click_key(self, keycode: int) -> bool:
        return True

    def input_text(self, text: str) -> bool:
        return True

    def key_down(self, keycode: int) -> bool:
        return True

    def key_up(self, keycode: int) -> bool:
        return True

    def scroll(self, dx: int, dy: int) -> bool:
        return True

    def relative_move(self, dx: int, dy: int) -> bool:
        return True

    def shell(self, cmd: str, timeout: int):
        return None

    def inactive(self) -> bool:
        return True


class ProbeAction(CustomAction):
    """只读桥：一次取帧 → run_recognition → 写回本 probe_id。"""

    def __init__(self) -> None:
        super().__init__()
        self.frame: np.ndarray | None = None   # 桥自取的帧（模拟 graph.frame()）

    def run(self, context, argv):
        raw = argv.custom_action_param
        p = json.loads(raw) if raw else {}
        if isinstance(p, str):        # override 传字符串会被二次编码，兜底再解一层
            p = json.loads(p)
        pid, node = p.get("probe_id"), p.get("node")
        TRACE.append(f"bridge pid={pid} node={node} ctx={type(context).__name__}")
        if context is None:
            RESULTS[pid] = {"error": "context is None"}
            return True
        try:
            frame = self.frame if self.frame is not None else None
            detail = context.run_recognition(node, frame)
            hit = bool(detail.hit) if detail is not None else False
            box = getattr(detail, "box", None) if detail is not None else None
            RESULTS[pid] = {
                "hit": hit,
                "box": [int(v) for v in box] if box is not None else None,
                "detail": type(detail).__name__,
                "frame_passed": frame is not None,
            }
            TRACE.append(f"bridge pid={pid} hit={hit} box={box}")
        except Exception as exc:  # noqa: BLE001
            RESULTS[pid] = {"error": f"{type(exc).__name__}: {exc}"}
            TRACE.append(f"bridge pid={pid} EXC {exc}")
        return True


def build_resource(bridge: ProbeAction) -> Resource:
    res = Resource()
    res.register_custom_action("A0_Probe", bridge)
    tmp = Path(tempfile.mkdtemp(prefix="a0-res-"))
    for d in (CORE_PIPELINE, SPEEDRUSH_PIPELINE):
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            (tmp / f.name).write_bytes(f.read_bytes())
    job = res.post_pipeline(str(tmp)).wait()
    print(f"[res] nodes={len(res.node_list)} post_ok={job.succeeded}")
    return res


def run_once(tag: str, node: str, cap_frame: np.ndarray, bridge_frame: np.ndarray) -> dict:
    bridge = ProbeAction()
    bridge.frame = bridge_frame
    res = build_resource(bridge)
    ctrl = FrameController(cap_frame)
    tasker = Tasker()
    tasker.bind(res, ctrl)
    entry = {
        "A0.Entry": {
            "recognition": {"type": "DirectHit"},
            "action": {
                "type": "Custom",
                "param": {
                    "custom_action": "A0_Probe",
                    # 实测：这里传 dict（结构化）才不会被二次编码
                    "custom_action_param": {"probe_id": "p1", "node": node},
                },
            },
            "rate_limit": 100,
            "timeout": 3000,
        }
    }
    t0 = time.monotonic()
    tj = tasker.post_task("A0.Entry", pipeline_override=entry)
    detail = tj.wait()
    dt = time.monotonic() - t0
    print(f"\n=== {tag} ===")
    print(f"[task] {detail} elapsed={dt:.3f}s caps={ctrl.caps}")
    for line in TRACE:
        print(" ", line)
    TRACE.clear()
    r = RESULTS.pop("p1", None)
    print(f"[result] {json.dumps(r, ensure_ascii=False)}")
    return r or {}


def main() -> None:
    nav = Path(os.environ.get("MAAR_A0_NAV")
               or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
               / "MaaRacingMaster" / "debug" / "20261106_185832" / "speedrush" / "nav")
    drive = cv2.imread(str(nav / "000895.jpg"), cv2.IMREAD_COLOR)   # 驾驶画面
    store = cv2.imread(str(nav / "000894.jpg"), cv2.IMREAD_COLOR)   # 商店页
    print(f"[frame] drive={drive.shape} store={store.shape}")

    # 1) 驾驶帧 + 驾驶页锚点，桥传 BGR 帧，controller 也喂驾驶帧
    a = run_once("A 驾驶帧/BGR/驾驶页锚点", "speedrush.驾驶页锚点", drive, drive)
    # 2) 商店帧 + 驾驶页锚点（应 miss）
    b = run_once("B 商店帧/BGR/驾驶页锚点", "speedrush.驾驶页锚点", store, store)
    # 3) 关键：controller 喂「商店帧」，桥传「驾驶帧」——若 hit，证明用的是传入帧
    c = run_once("C cap=商店帧 但桥传驾驶帧", "speedrush.驾驶页锚点", store, drive)
    # 4) 通道契约：桥传 RGB（错误通道）
    d = run_once("D 驾驶帧/RGB(错误通道)", "speedrush.驾驶页锚点", drive,
                 cv2.cvtColor(drive, cv2.COLOR_BGR2RGB))
    # 5) 反向：商店页锚点 + 商店帧
    e = run_once("E 商店帧/商店横幅锚点", "speedrush.商店横幅锚点", store, store)

    print("\n=== 汇总 ===")
    for name, r in (("A drive/BGR", a), ("B store/BGR", b), ("C 传帧生效", c),
                    ("D RGB 通道", d), ("E 商店锚点", e)):
        print(f"  {name:16s} {r}")


if __name__ == "__main__":
    main()
