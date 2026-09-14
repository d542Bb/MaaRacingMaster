# -*- coding: utf-8 -*-
"""彩蛋链 12s 静默的协议层复现实验（2026-09-14，根因排查中）。

真机现象（21:33 轮）：链内每次点击 12s 静默（6s 等结果超时 + 6s 等槽释放）、
光标仅 ~25px/11s 缓慢爬行、raw 帧落盘间隔从 ~0.16s 劣化到 1.3-2.0s、CPU p50 801%。

本实验结论（已排除两个候选）：
  1. 导航线程 cancel→清槽→重提交协议无缺陷——正常与 CPU 饱和两组清槽均 <0.25s；
  2. 「miss 重试检测火力全开饿死线程」不成立——真帧上 detect_cursor 命中 0.968，
     链期间识别正常，未走 miss 路径。
仍开放的区分维度（待线程级检测方案定案）：导航 worker 实际步进速率、
游戏侧摇杆响应、GIL/调度争抢——本实验 B 组仅证明饱和下步进 25 倍劣化
（6s 窗口 68 步→5 步）是「步进被拉慢」一类形态，量级与真机吻合但未能归因到具体线程。

用法：`.venv\\Scripts\\python.exe tools/experiments/gamepad-chain-starvation/probe.py`
"""
from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

import cv2
import numpy as np

from maaracing_master.core.gamepad_cursor import GamepadClicker

W, H = 1280, 720
BG = (32, 46, 68)


class _Scene:
    """光标固定不动（复现「步进极慢、窗口内光标近乎静止」的真机形态——
    真机 21:33 帧证据实为 ~25px/11s 缓慢爬行，非完全不动）。"""

    def __init__(self, pos=(900, 470)):
        self.pos = [float(pos[0]), float(pos[1])]

    def draw(self) -> np.ndarray:
        f = np.zeros((H, W, 3), dtype=np.uint8)
        f[:, :] = BG
        cx, cy = int(round(self.pos[0])), int(round(self.pos[1]))
        cv2.circle(f, (cx, cy), 13, (133, 133, 133), -1)
        cv2.circle(f, (cx, cy), 8, (255, 255, 255), -1)
        return f

    def advance(self, dx, dy):
        pass  # 光标固定：复现「窗口内近乎静止」形态（真机为 ~25px/11s 缓慢爬行）


class _Cap:
    def __init__(self, scene):
        self._scene = scene

    def screenshot(self):
        return self._scene.draw()


class _Gpad:
    K = 0.023467622116800504
    DEADZONE = 5000.0
    FRAME_S = 1 / 60.0

    def __init__(self, scene):
        self._scene = scene
        self._stick = (0, 0)
        self.updates = 0

    def left_joystick(self, x_value=0, y_value=0):
        self._stick = (x_value, y_value)

    def right_joystick(self, x_value=0, y_value=0):
        pass

    def left_trigger(self, value=0):
        pass

    def right_trigger(self, value=0):
        pass

    def press_button(self, button):
        pass

    def release_button(self, button):
        pass

    def update(self):
        self.updates += 1
        x, y = self._stick
        mag = math.hypot(x, y)
        if mag < self.DEADZONE:
            return
        step = self.K * mag * self.FRAME_S
        # 场景不消费位移（scene.advance 为 no-op）：复现窗口内光标近乎静止的形态


def busy_threads(n, stop):
    def spin():
        while not stop.is_set():
            sum(i * i for i in range(10000))
    for _ in range(n):
        threading.Thread(target=spin, daemon=True).start()


def run_case(label, sat_threads):
    scene = _Scene()
    nav = GamepadClicker(_Cap(scene), _Gpad(scene))
    stop = threading.Event()
    if sat_threads:
        busy_threads(sat_threads, stop)
    time.sleep(0.3)
    print(f"--- {label} ---")
    t0 = time.monotonic()
    # 经真实 _nav_loop 后台线程执行（与生产同构），测步进耗时与清槽行为
    t_submit = time.monotonic()
    ok = nav.submit((56, 23), intent=False, tol_px=14.0, task_type="click")
    print(f"submit: {ok} @ +{time.monotonic()-t_submit:.2f}s")
    # 模拟 _egg_chain_click 的等结果循环（6s）
    res = None
    while time.monotonic() - t_submit < 6.0:
        res = nav.consume_result()
        if res is not None:
            break
        time.sleep(0.05)
    t_wait = time.monotonic() - t_submit
    print(f"等结果: {'超时' if res is None else '拿到'} @ +{t_wait:.2f}s"
          + (f" ok={res.get('ok')} reason={res.get('reason')}" if res else ""))
    # 模拟 cancel + drain（6s 窗）
    if res is None:
        nav.cancel()
        t_cancel = time.monotonic()
        while time.monotonic() - t_cancel < 6.0:
            nav.consume_result()
            if not nav.is_busy():
                break
            time.sleep(0.05)
        print(f"cancel 后清槽: {'成功' if not nav.is_busy() else '超时失败'} "
              f"@ +{time.monotonic()-t_cancel:.2f}s (cancel 后)")
        # resubmit
        ok2 = nav.submit((56, 23), intent=False, tol_px=14.0, task_type="click")
        print(f"resubmit: {ok2}")
    total = time.monotonic() - t0
    prog = nav.nav_progress()
    print(f"进度快照: stage={prog.get('stage')} dist={prog.get('dist')} "
          f"seq={prog.get('seq')} 总耗时 {total:.2f}s")
    stop.set()
    nav.shutdown()
    return total


if __name__ == "__main__":
    # 直接用 _nav_loop 线程形态跑（与生产同构）才有 cancel 检查点语义
    print("== 生产同构形态（_nav_loop 线程）==")
    run_case("A. 正常负载", 0)
    run_case("B. CPU 饱和（6 忙线程）", 6)
