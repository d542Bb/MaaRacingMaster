# -*- coding: utf-8 -*-
"""物理手柄采样与槽位锁定（speedrush 录制器 / 宏录制器共用）。

**为什么单独成件**：XInput 有四个槽位，虚拟手柄在程序不驱动时恒为静止全零；
"锁错槽位"会把整局录成全零——文件看着完好，比没有数据更坏。挑选规则（唯一
槽位直接锁、多槽位挑有输入活动者、超时兜底锁首个）经过实机复盘，两个录制器
必须共用同一份，不得各写一遍。

本模块只读 XInput，不创建/驱动虚拟设备。
"""

from __future__ import annotations

import time
from typing import Callable

from maaracing_master.core import xinput

# 摇杆/扳机超过此幅度才算"有明显输入"。阈值远低于 XInput 摇杆死区（约 7800），
# 轻微碰杆即算；扳机按 >10 计（0~255），按键非零也算。静止的虚拟设备恒为全零。
PAD_ACTIVE_THRESHOLD = 2000
# 多槽位且始终无输入时的兜底等待：超时后锁首个并告警，不无限期不采样本
PAD_SLOT_PROBE_TIMEOUT_S = 5.0

LogFn = Callable[[str, str], None]


def pad_active(st: xinput.PadState) -> bool:
    """该槽位是否出现"明显输入"——多槽位时用它把物理手柄从静止的虚拟手柄里挑出来。"""
    lx, ly = st.left_stick
    rx, ry = st.right_stick
    return (abs(lx) > PAD_ACTIVE_THRESHOLD or abs(ly) > PAD_ACTIVE_THRESHOLD
            or abs(rx) > PAD_ACTIVE_THRESHOLD or abs(ry) > PAD_ACTIVE_THRESHOLD
            or st.left_trigger > 10 or st.right_trigger > 10 or st.buttons != 0)


class PadSampler:
    """按"锁定一个槽位后只读它"的语义采样物理手柄。

    未锁定（``slot=None``）时轮询全部槽位并按证据锁定：

    - 只有一个已连接槽位 → 直接锁定，不必等输入（无歧义）；
    - 多个槽位 → 锁定**第一个出现明显输入**的。虚拟手柄在程序不驱动时恒为
      静止全零，永远不满足条件，故不会被误锁；
    - 多槽位且超时仍无任何输入 → 锁首个并告警（XInput 层面无法再区分物理
      与虚拟，只能留一条可查的痕迹）。

    探测期返回 ``(None, False)``：不写样本，也不计入"连续读不到"。
    """

    def __init__(self, *, slot: int | None = None,
                 log: LogFn | None = None) -> None:
        self._slot = slot
        self._log = log or (lambda _msg, _level: None)

    @property
    def slot(self) -> int | None:
        return self._slot

    def poll(self, probe_deadline: float) -> tuple[xinput.PadState | None, bool]:
        """读当前该采样的手柄；返回 ``(状态, 是否计入"读不到")``。

        ``probe_deadline`` 是调用方在建线程时算好的探测截止时刻（monotonic）。
        """
        if self._slot is not None:
            return xinput.read_state(self._slot), True
        states = [st for st in (xinput.read_state(i) for i in range(4))
                  if st is not None]
        if not states:
            return None, True
        if len(states) == 1:
            self._slot = states[0].index
            self._log(f"[极速狂飙] 手柄槽位锁定 {self._slot}（唯一已连接）", "INFO")
            return states[0], True
        active = [st for st in states if pad_active(st)]
        if active:
            self._slot = active[0].index
            self._log(
                f"[极速狂飙] 手柄槽位锁定 {self._slot}"
                f"（{len(states)} 个候选槽位，此槽有输入活动）", "INFO")
            return active[0], True
        if time.monotonic() >= probe_deadline:
            self._slot = states[0].index
            self._log(
                f"[极速狂飙] {len(states)} 个手柄槽位在"
                f" {PAD_SLOT_PROBE_TIMEOUT_S:.0f}s 内均无输入活动，锁定首个"
                f" {self._slot}——若本局杆值恒为零，检查是否锁到了虚拟手柄",
                "WARNING")
            return states[0], True
        return None, False
