"""最小复现：跑图框架（MaaFramework）收尾析构是否可能崩溃。

自包含实验脚本：只用 maa 绑定 + numpy/cv2，不 import 业务代码。
判据 = 子进程退出码（0xC0000005 即复现崩溃），不采信被测对象自报状态。

两条通道组合（--channels）：
  bare  只挂帧注入控制器
  full  控制器 + 自定义识别 + 自定义动作 + 事件接收器 + 取帧抛异常
        （= 生产现场的形态：C 层持有多个 Python 对象地址，回收时可能撞上回调）

两种收尾（--mode）：
  gc        对象互相钉住、留给垃圾回收一次全收（生产现状）
  explicit  有序销毁：任务 → 执行器 → 控制器 → 资源，每步单独回收（修复设想）

用法：
    python probe.py --mode gc --channels full --iters 20
    python probe.py --mode explicit --channels full --iters 20
"""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

# 0xC0000005：Windows 退出码经 subprocess 出来是无符号 3221225477，
# 部分环境返回有符号 -1073741819，两者都算崩溃
CRASH_CODES = {3221225477, -1073741819}
PIPELINE_NODE = "probe.dwell"
RECOG_NAME = "probe.recog"
ACTION_NAME = "probe.act"


def _write_bundle(root: Path, full: bool) -> None:
    """造一个最小 bundle：识别永远不命中 → 框架驻留重试，停不下来的那种活。"""
    import cv2
    import numpy as np

    (root / "pipeline").mkdir(parents=True, exist_ok=True)
    (root / "image").mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(root / "image" / "probe_never.png"),
                np.full((20, 20, 3), 255, dtype=np.uint8))

    if full:
        node = {
            "recognition": {
                "type": "Custom",
                "param": {"custom_recognition": RECOG_NAME,
                          "custom_recognition_param": {}},
            },
            "action": {"type": "Custom",
                       "param": {"custom_action": ACTION_NAME}},
        }
    else:
        node = {
            "recognition": {
                "type": "TemplateMatch",
                "param": {"template": ["probe_never.png"],
                          "threshold": 0.99, "roi": [0, 0, 64, 64]},
            },
            "action": {"type": "DoNothing"},
        }
    node.update({"next": [PIPELINE_NODE], "timeout": 60000, "rate_limit": 200})
    (root / "pipeline" / "probe.json").write_text(
        json.dumps({PIPELINE_NODE: node}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _make_controller(full: bool):
    """帧注入控制器。full 模式下取帧抛异常——生产现场持续出现的那种异常。"""
    import numpy as np
    from maa.controller import CustomController

    class DummyController(CustomController):
        def connect(self):
            return True

        def connected(self):
            return True

        def request_uuid(self):
            return "mra-probe-dummy"

        def start_app(self, intent):
            return True

        def stop_app(self, intent):
            return True

        def screencap(self):
            if full:
                raise RuntimeError("probe: 帧缺失或过期（模拟生产取帧异常）")
            return np.zeros((720, 1280, 3), dtype=np.uint8)

        def click(self, x, y):
            return True

        def swipe(self, x1, y1, x2, y2, duration):
            return True

        def touch_down(self, contact, x, y, pressure):
            return True

        def touch_move(self, contact, x, y, pressure):
            return True

        def touch_up(self, contact):
            return True

        def click_key(self, keycode):
            return True

        def input_text(self, text):
            return True

        def key_down(self, keycode):
            return True

        def key_up(self, keycode):
            return True

    return DummyController()


def _make_recognition():
    from maa.custom_recognition import CustomRecognition

    class DummyRecognition(CustomRecognition):
        def analyze(self, context, argv):
            return None  # 永远未命中

    return DummyRecognition()


def _make_action():
    from maa.custom_action import CustomAction

    class DummyAction(CustomAction):
        def run(self, context, argv):
            return True

    return DummyAction()


def _make_sink():
    from maa.context import ContextEventSink

    class DummySink(ContextEventSink):
        def __init__(self):
            super().__init__()
            self.seen = 0

        def on_node_recognition(self, context, noti_type, detail):
            self.seen += 1

    return DummySink()


def _leak(box: list) -> None:
    """把对象塞进自引用环并让函数返回：引用计数不再为 0，只能靠 gc.collect 回收。

    这正是生产现场的形态——模块↔点击器↔控制器互相钉住，收尾时一次全堆回收
    才真正触发析构。
    """
    box.append(box)


def run_once(mode: str, channels: str, race: bool = False,
             settle_ms: int = 200, stop_only: bool = False) -> int:
    from maa.resource import Resource
    from maa.tasker import Tasker

    full = channels == "full"
    with tempfile.TemporaryDirectory(prefix="mra_probe_") as tmp:
        root = Path(tmp)
        _write_bundle(root, full)

        resource = Resource()
        resource.post_bundle(root).wait()
        recog = act = sink = None
        if full:
            recog = _make_recognition()
            act = _make_action()
            sink = _make_sink()
            resource.register_custom_recognition(RECOG_NAME, recog)
            resource.register_custom_action(ACTION_NAME, act)

        ctl = _make_controller(full)
        tasker = Tasker()
        if sink is not None:
            tasker.add_context_sink(sink)
        tasker.bind(resource, ctl)
        job = tasker.post_task(PIPELINE_NODE)

        time.sleep(0.5)  # 让框架真正进入驻留重试
        if race:
            # 抢跑：不停任务、不等收摊，直接销毁（检验「销毁时机」是否就是根因）
            pass
        elif stop_only:
            # 生产跳转图的停止方式：只等「停止请求」完成，不等任务本身结束
            tasker.post_stop().wait()
        else:
            tasker.post_stop()
            try:
                job.wait()
            except Exception:  # noqa: BLE001 —— 停止路径异常不改变本实验的判据
                pass
            time.sleep(settle_ms / 1000.0)  # 收摊宽限：检验「等多久才安全」

        if mode == "gc":
            box = [tasker, resource, ctl, job, recog, act, sink]
            _leak(box)
            del tasker, resource, ctl, job, recog, act, sink
            gc.collect()  # ← 生产现场的触发点
        else:
            job = None
            tasker = None
            gc.collect()
            ctl = None
            gc.collect()
            recog = act = sink = None
            resource = None
            gc.collect()

        time.sleep(0.2)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["gc", "explicit"], default="gc")
    ap.add_argument("--channels", choices=["bare", "full"], default="full")
    ap.add_argument("--iters", type=int, default=10)
    ap.add_argument("--settle-ms", type=int, default=200,
                    help="停止后到销毁之间的宽限毫秒数")
    ap.add_argument("--stop-only", action="store_true",
                    help="只等停止请求完成（生产跳转图的方式），不等任务结束")
    ap.add_argument("--race", action="store_true",
                    help="抢跑：不停任务、不等收摊就直接销毁")
    ap.add_argument("--once", action="store_true", help="内部用：只跑一次")
    args = ap.parse_args()

    if args.once:
        try:
            return run_once(args.mode, args.channels, args.race,
                            args.settle_ms, args.stop_only)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 1

    here = str(Path(__file__).resolve())
    codes = []
    for i in range(1, args.iters + 1):
        proc = subprocess.run(
            [sys.executable, here, "--mode", args.mode,
             "--channels", args.channels, "--once"]
            + (["--race"] if args.race else [])
            + (["--stop-only"] if args.stop_only else [])
            + ["--settle-ms", str(args.settle_ms)],
            capture_output=True, text=True, timeout=120,
        )
        code = proc.returncode
        codes.append(code)
        tag = "CRASH" if code in CRASH_CODES else ("ok" if code == 0 else f"exit={code}")
        print(f"[{args.mode}/{args.channels}{'/race' if args.race else ''}"
              f"{'/stop-only' if args.stop_only else ''}] "
              f"iter {i:02d}/{args.iters}: {tag}")
        if code not in CRASH_CODES and code != 0:
            for line in (proc.stderr or "").strip().splitlines()[-3:]:
                print("    " + line)

    crashes = sum(1 for c in codes if c in CRASH_CODES)
    other = sum(1 for c in codes if c not in CRASH_CODES and c != 0)
    print(f"\n[{args.mode}/{args.channels}{'/race' if args.race else ''}] 崩溃 {crashes}/{len(codes)}，"
          f"异常退出 {other}，正常 {codes.count(0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
