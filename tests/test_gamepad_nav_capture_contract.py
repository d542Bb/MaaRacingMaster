# -*- coding: utf-8 -*-
"""后台(手柄)导航的帧源契约回归（2026-09-10 用户实测症状锁）。

症状：手柄操作方式下程序不推动光标、PEEP 预览无内容。

契约面：core 的截图能力 `CaptureAdapter` 暴露 `screenshot()` / `frame_with_age()`
（`CaptureCapability` Protocol 同形），v4 装配（`_run_v4_loop` / `NavGraph.run`）
按能力对象注入 `bind_gamepad(self.ctx.capture, ...)`。导航器必须能从这一形态读帧——
读不到帧时闭环从未见过画面，表现为：摇杆一次未推（程序不操作光标）+ 光标识别
候选快照恒空（PEEP 无绿圈/无进度可画），且被误判成 device_lost。
"""
from __future__ import annotations

import math
import time

import cv2
import numpy as np
import pytest

# 完整运行时依赖（core.clicker 经 window_utils 拉 maa；gamepad_cursor 拉 cv2）。
# CI 轻依赖环境（只装 pytest+numpy+opencv-headless）下整文件跳过，不误红——
# 口径同 test_navkit_runtime_golden.py。
try:
    from maaracing_master.core import clicker as clicker_mod
    from maaracing_master.core.clicker import GAMEPAD_BOX_TOL_RATIO, Clicker
    from maaracing_master.core.gamepad_cursor import GamepadClicker
    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"手柄导航契约测试需要完整运行时依赖（maa/cv2）：{_RUNTIME_ERR}"
)

W, H = 1280, 720
BG = (32, 46, 68)  # 非中性色背景（不参与灰/白种子提取）
TARGET = (700, 420)
BOX_PX = (200, 86)  # 目标框像素宽高 → 手柄按 A 容差 = 0.35 * min(w, h)


class _Scene:
    """合成游戏画面：白色圆盘 + 灰环光标，位置由手柄报告驱动。"""

    def __init__(self, pos=(420, 420)):
        self.pos = [float(pos[0]), float(pos[1])]

    def draw(self) -> np.ndarray:
        f = np.zeros((H, W, 3), dtype=np.uint8)
        f[:, :] = BG
        cx, cy = int(round(self.pos[0])), int(round(self.pos[1]))
        cv2.circle(f, (cx, cy), 13, (133, 133, 133), -1)  # 环（含内盘覆盖区）
        cv2.circle(f, (cx, cy), 8, (255, 255, 255), -1)   # 内盘
        cv2.rectangle(f, (60, 620), (240, 680), (70, 90, 130), -1)
        return f

    def advance(self, dx: float, dy: float) -> None:
        self.pos[0] = min(W - 1, max(0, self.pos[0] + dx))
        self.pos[1] = min(H - 1, max(0, self.pos[1] + dy))

    def dist_to_target(self) -> float:
        return math.hypot(self.pos[0] - TARGET[0], self.pos[1] - TARGET[1])


class CaptureStub:
    """与 core.capabilities.CaptureAdapter 同形的截图能力（无 get_latest、不可调用）。"""

    def __init__(self, scene: _Scene):
        self._scene = scene
        self.calls = 0

    def screenshot(self) -> np.ndarray:
        self.calls += 1
        return self._scene.draw()

    def frame_with_age(self):
        return (self._scene.draw(), self.calls, 0, 0.0)


class GpadStub:
    """手柄桩：记录摇杆/按钮输入，并按 60Hz 定步长消费报告驱动合成光标。

    速度口径取 stick_speed_model.json 的 k/deadzone（游戏侧同一模型），位移只随
    「报告数」而不是墙钟推进——闭环判定与宿主调度无关，终点位置可复现。
    """

    K = 0.023467622116800504
    DEADZONE = 5000.0
    FRAME_S = 1 / 60.0

    def __init__(self, scene: _Scene):
        self._scene = scene
        self._stick = (0, 0)
        self.stick_moves = 0
        self.buttons: list[str] = []

    def left_joystick(self, x_value: int = 0, y_value: int = 0) -> None:
        self._stick = (x_value, y_value)
        if x_value or y_value:
            self.stick_moves += 1

    def right_joystick(self, x_value: int = 0, y_value: int = 0) -> None:
        pass

    def left_trigger(self, value: int = 0) -> None:
        pass

    def right_trigger(self, value: int = 0) -> None:
        pass

    def press_button(self, button) -> None:
        self.buttons.append("press")

    def release_button(self, button) -> None:
        self.buttons.append("release")

    def update(self) -> None:
        x, y = self._stick
        mag = math.hypot(x, y)
        if mag < self.DEADZONE:
            return
        step = self.K * mag * self.FRAME_S
        self._scene.advance(x / mag * step, -y / mag * step)


@pytest.fixture()
def scene():
    return _Scene()


def test_frame_source_capability_shape_readable(scene):
    """导航器必须能吃下 CaptureAdapter 形态的帧源（能力对象，非 callable）。"""
    cap = CaptureStub(scene)
    nav = GamepadClicker(cap, GpadStub(scene))
    try:
        assert nav._frame() is not None, "CaptureAdapter 形态帧源读不到帧"
        pos = nav.read_pos(1, timeout=2.0)
        assert pos is not None, "真实形态帧源下光标识别失败（闭环从未见帧）"
        assert nav.last_cands, "识别候选快照为空 → PEEP 无内容可画"
    finally:
        nav.shutdown()


def test_gamepad_click_through_clicker_drives_stick(scene, monkeypatch):
    """端到端：Clicker(gamepad) 提交一次点击 → 摇杆真的推出、到位后按 A。"""
    monkeypatch.setattr(clicker_mod, "window_client_size", lambda _hwnd: (W, H))
    cap = CaptureStub(scene)
    gpad = GpadStub(scene)
    click = Clicker(hwnd=0, mode="gamepad")
    click.bind_gamepad(cap, gpad)  # 与 v4 装配一致：注入能力对象
    tol_px = GAMEPAD_BOX_TOL_RATIO * min(BOX_PX)
    try:
        assert click.submit_click(TARGET[0] / W, TARGET[1] / H,
                                  box=(BOX_PX[0] / W, BOX_PX[1] / H)) is True
        deadline = time.monotonic() + 25.0
        res = None
        while res is None and time.monotonic() < deadline:
            res = click.consume_result()
            if res is None:
                time.sleep(0.02)
        assert res is not None, "导航任务从未产出结果（线程未执行闭环）"
        assert not res.get("device_lost"), f"闭环读不到帧即判手柄丢失：{res}"
        assert res.get("ok") is True, f"导航未到位：{res}"
        assert gpad.stick_moves > 0, "摇杆从未推出 → 程序不会操作光标"
        assert "press" in gpad.buttons, "到位后未按确认键"
        assert scene.dist_to_target() <= tol_px + 0.5, \
            f"光标未收敛到按 A 容差内：{scene.pos} 距离{scene.dist_to_target():.1f}px"
    finally:
        click.shutdown()
