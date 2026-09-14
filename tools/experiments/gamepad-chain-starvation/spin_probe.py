# -*- coding: utf-8 -*-
"""链等待循环「零睡眠忙旋」的杀伤力量化实验（2026-09-15）。

读码发现的疑点：core/capabilities.py 的 Lifecycle.sleep 是 0.1s 粒度
（`for _ in range(int(seconds / 0.1))`），而彩蛋链等待/drain 循环传 0.05
→ int(0.05/0.1)=0 → **一次都不睡**。即链内点击的 6s 等结果 + 6s drain
全程是忙旋（每迭代 2 次 _nav_lock + dict 浅拷贝发布），而 pipeline 点击
等待（nav_graph.click）用的是真 time.sleep(0.05)。这解释了为何只有链内
点击劣化、为何 probe.py（等待循环用真 sleep）复现不出真机形态。

本实验用生产同构的 GamepadClicker（桩截图+桩手柄、光标静止）对照三组，
各 6s 窗口，t=3s 时 cancel，量四个指标：
  1. worker 步进速率（gpad.update 计数 / nav_progress seq）
  2. cancel 后 abort 响应时延（is_busy 清除时刻）
  3. 观察线程模拟器（150ms 节拍 + dict 拷贝）的实际 tick 间隔 p50/p95
  4. 进程 CPU 时间与 gc gen0 计数增量（检验 GC 风暴假说）

用法：`.venv/Scripts/python.exe tools/experiments/gamepad-chain-starvation/spin_probe.py`
"""
from __future__ import annotations

import gc
import math
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

from maaracing_master.core.gamepad_cursor import GamepadClicker

W, H = 1280, 720
BG = (32, 46, 68)
WINDOW_S = 6.0
CANCEL_AT = 3.0


class _Scene:
    """光标固定不动（真机 23:00 轮形态：worker 全窗步进但光标不动）。"""

    def __init__(self, pos=(900, 470)):
        self.pos = [float(pos[0]), float(pos[1])]

    def draw(self) -> np.ndarray:
        f = np.zeros((H, W, 3), dtype=np.uint8)
        f[:, :] = BG
        cx, cy = int(round(self.pos[0])), int(round(self.pos[1]))
        cv2_circle(f, cx, cy)
        return f


def cv2_circle(f, cx, cy):
    import cv2
    cv2.circle(f, (cx, cy), 13, (133, 133, 133), -1)
    cv2.circle(f, (cx, cy), 8, (255, 255, 255), -1)


class _Cap:
    def __init__(self, scene):
        self._scene = scene

    def screenshot(self):
        return self._scene.draw()


class _Gpad:
    def __init__(self):
        self.updates = 0

    def left_joystick(self, x_value=0, y_value=0):
        pass

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


def lifecycle_sleep(seconds: float) -> bool:
    """1:1 复刻 core/capabilities.py:240 的 Lifecycle.sleep（0.1s 粒度）。"""
    for _ in range(int(seconds / 0.1)):
        time.sleep(0.1)
    return True


class _ObserverSim:
    """观察线程模拟器：150ms Event.wait + 一帧观察的轻量 dict 工作。"""

    def __init__(self, stop: threading.Event):
        self._stop = stop
        self.ticks: list[float] = []
        self._base = {"k%d" % i: i for i in range(24)}

    def run(self):
        last = time.perf_counter()
        while not self._stop.wait(0.150):
            now = time.perf_counter()
            self.ticks.append(now - last)
            last = now
            # _observe_kwargs 的形状：dict(base) 浅拷贝 + 少量键写
            snap = dict(self._base)
            snap["treasure_frame_index"] = len(self.ticks)


def _refresh_peep_equiv(nav: GamepadClicker, base: dict) -> dict:
    """1:1 复刻 _egg_chain_refresh_peep 的数据形状（真实锁流量）。"""
    snap = dict(base)
    cands = nav.last_cands
    snap["treasure_cursor_cands"] = {"list": list(cands), "sel": nav.last_cand_sel}
    prog = nav.nav_progress()  # _nav_lock
    snap["treasure_gamepad_cursor"] = dict(prog) if prog else None
    return snap


def run_case(label: str, wait_mode: str) -> dict:
    """wait_mode: 'none'=无决策线程 | 'sleep'=真 time.sleep(0.05) | 'spin'=lifecycle 零睡眠"""
    scene = _Scene()
    gpad = _Gpad()
    nav = GamepadClicker(_Cap(scene), gpad)
    stop = threading.Event()
    obs = _ObserverSim(stop)
    obs_thread = threading.Thread(target=obs.run, daemon=True)
    obs_thread.start()

    base_kwargs = {"k%d" % i: i for i in range(24)}
    spin_iters = [0]
    submit_at = time.monotonic() + 0.1

    def decision_wait_loop():
        """1:1 复刻 _egg_chain_click 的等结果循环（提交由主线程做，这里只等）。"""
        time.sleep(0.1)
        deadline = time.monotonic() + WINDOW_S
        while time.monotonic() < deadline:
            res = nav.consume_result()
            if res is not None:
                return
            _refresh_peep_equiv(nav, base_kwargs)
            spin_iters[0] += 1
            if wait_mode == "sleep":
                time.sleep(0.05)
            elif wait_mode == "spin":
                lifecycle_sleep(0.05)  # int(0.05/0.1)=0 → 零睡眠

    dec_thread = None
    if wait_mode != "none":
        dec_thread = threading.Thread(target=decision_wait_loop, daemon=True)
        dec_thread.start()

    time.sleep(0.1)
    nav.submit((56, 23), intent=False, tol_px=14.0, task_type="click")
    t0 = time.monotonic()
    cpu0 = time.process_time()
    gc0 = gc.get_stats()[0]["collections"]
    time.sleep(CANCEL_AT)
    nav.cancel()
    t_cancel = time.monotonic()
    # 等abort被响应（is_busy 清除），上限 3s
    abort_s = None
    while time.monotonic() - t_cancel < 3.0:
        nav.consume_result()
        if not nav.is_busy():
            abort_s = time.monotonic() - t_cancel
            break
        time.sleep(0.01)
    remaining = WINDOW_S - (time.monotonic() - t0)
    if remaining > 0:
        time.sleep(remaining)
    cpu1 = time.process_time()
    gc1 = gc.get_stats()[0]["collections"]
    stop.set()
    obs_thread.join(timeout=1)

    ticks = obs.ticks[2:]  # 丢弃头两个（启动瞬态）
    prog = nav.nav_progress()
    out = {
        "worker_steps": prog.get("seq", 0),
        "gpad_updates": gpad.updates,
        "abort_s": abort_s,
        "obs_p50_ms": statistics.median(ticks) * 1000 if ticks else -1,
        "obs_p95_ms": (sorted(ticks)[int(len(ticks) * 0.95)] * 1000) if ticks else -1,
        "obs_n": len(ticks),
        "spin_iters": spin_iters[0],
        "cpu_s": cpu1 - cpu0,
        "gc_gen0": gc1 - gc0,
    }
    nav.shutdown()
    print(f"--- {label} ---")
    print(f"  worker步进(seq): {out['worker_steps']}  gpad.update: {out['gpad_updates']}")
    print(f"  cancel后abort响应: {out['abort_s'] if out['abort_s'] is not None else '>3s 未响应!'}")
    print(f"  观察tick p50/p95: {out['obs_p50_ms']:.0f}ms / {out['obs_p95_ms']:.0f}ms (n={out['obs_n']})")
    print(f"  决策循环迭代数: {out['spin_iters']}  进程CPU: {out['cpu_s']:.2f}s  gc_gen0: {out['gc_gen0']}")
    return out


if __name__ == "__main__":
    print("== 零睡眠忙旋杀伤力对照（窗口 6s，t=3s cancel）==")
    run_case("A. 无决策线程（基线）", "none")
    gc.collect()
    time.sleep(0.5)
    run_case("B. 真睡眠 time.sleep(0.05)（pipeline 同款）", "sleep")
    gc.collect()
    time.sleep(0.5)
    run_case("C. lifecycle_sleep(0.05)（链内零睡眠忙旋）", "spin")
