#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一步循环的重试状态机（纯标准库）。

treasure 与 speedrush 各手搓过一套「发一次动作 → 等后置条件 → 有限重发 → 耗尽显式终止」。
本模块把这套**状态转移**（计数 / 时间窗 / 到期重发 / 耗尽 / 停止取消）收进 core，由同一台
`StepMachine` 解释——插件不再各自持有一份计数或计时。

**单一真源**：迁移后插件侧与 `policy.json` 都不得再保留自己的重试计数或时间状态。机器是
唯一持有者；插件只供给三样——**动作**（怎么发这一下）、每拍 **verdict**（达成 / 未达成 /
停止）、**驱动时机**（阻塞步里循环喂，或逐帧喂）。

**"什么算一次尝试"由插件决定**，core 不假设（这正是两家的差异所在）：

- treasure 的尝试 = 一个**已送达**的点击（点击物理失败 `ok=false` 时插件**不 `arm`**——
  靠指纹边沿锁下帧自然重发，不吃预算，这是 treasure 独有的第①层）；
- speedrush 的尝试 = 一次 `run(action, confirm)` 调用（confirm 命中才算送达且生效）。

域无关（红线 1）：本模块不出现任何阶段名 / 节点名 / 玩法语义，只认 verdict 与 command；
调用方把「后置条件达成了没有」折成 `Verdict` 喂进来，机器把「下一步该干嘛」折成
`Command` 回吐。
"""
from __future__ import annotations

import time
from enum import Enum, auto
from typing import Callable

__all__ = [
    "StepExhaustedError",
    "Verdict",
    "Command",
    "StepMachine",
    "at",
]


class StepExhaustedError(RuntimeError):
    """统一步循环耗尽：动作已发出但反复未产生预期状态变化 → 系统性问题，显式终止。

    treasure 侧 `ClickRetryExhaustedError` 保留为本类的别名（不改捕获点与测试引用），
    使既有的「耗尽即显式终止」契约继续成立。
    """


class Verdict(Enum):
    """插件每拍报告：这次尝试的后置条件此刻成立了吗。"""

    SUCCESS = auto()  # 后置条件达成 → DONE（终态）
    PENDING = auto()  # 未达成 → 未到期 WAIT；到期重发；达上限后再等一窗仍如此 → FAIL
    STOP = auto()  # 用户停止 → ABORT（可选喂：外层已自管停止的调用方可不喂，见机器说明）


class Command(Enum):
    """机器每拍回答：调用方下一步该执行哪个动作。"""

    WAIT = auto()  # 继续等，勿重发
    RETRY = auto()  # 重发同一动作意图（不决定"点哪个"，只声明"再发一次这个意图"）
    DONE = auto()  # 后置条件达成，本步完成
    FAIL = auto()  # 重发预算耗尽 → 调用方按现状显式终止（抛异常 / break，各家适配）
    ABORT = auto()  # 用户停止 → 取消本步；与 FAIL 语义不同，不得合并


class StepMachine:
    """一步的重试状态机：唯一持有计数与时间窗，决定 WAIT/RETRY/DONE/FAIL/ABORT。

    时序口径（动作时序契约）：

    - `count` **只在"窗口到期、决定重发"时 ++，绝不在送达时 ++**；`arm()`（一次尝试发出 /
      送达）只重置窗口、不动计数。
    - 耗尽是「到上限后**再等一个窗口**仍 PENDING」才 `FAIL`，不是数满即抛——所以
      `max_retries` 一律取「**不含首点的重发上限**」，与 treasure 三链同口径。
    - `count++` **只在窗口到期时发生、且每窗一次**（同窗内每拍不重复 ++，慢送达不会多耗预算）。
      一次到期 `RETRY` 后置"重发在途"：未送达期间每拍仍 `RETRY`（结算据此把 `step_cmd` 持续保持
      `RETRY`、规则不中断重发），送达时 `arm()` 折叠它转 `WAIT`；窗口先到期仍未送达则视为该次无
      响应、推进一次预算。`arm()`（一次尝试发出 / 送达）只重置窗口、不动计数。
    - `STOP → ABORT` 是机器的**能力**、不是强制：speedrush 阻塞循环里查 `lifecycle.running`
      方便 → 喂 STOP 收 ABORT；treasure 帧循环在调机器之前已 `if not running: break` →
      不喂 STOP，机器只见 SUCCESS/PENDING。两者共用同一转移表。

    时钟可注入（`clock`），测试喂假钟做确定性验证。
    """

    def __init__(
        self,
        *,
        max_retries: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_retries < 0:
            raise ValueError(f"max_retries 不得为负：{max_retries}")
        if window_s <= 0:
            raise ValueError(f"window_s 必须为正：{window_s}")
        self._max_retries = max_retries
        self._window_s = window_s
        self._clock = clock
        self._count = 0
        # 计时基线在构造即设：忘了先 `arm` 时首个窗口内 PENDING 得 WAIT、下一窗才重发，
        # 不会"永远 WAIT"静默失效，也不会凭空多耗一次预算。
        self._since_ts: float = clock()
        # 一次 RETRY 已发出、尚未被"送达"折叠：重发资格存续（hold）期间每拍仍返回 RETRY，
        # 直到窗口再次到期（→ 推进一次预算）或送达 `arm()` / SUCCESS 折叠它。
        self._resend_in_flight = False

    @property
    def count(self) -> int:
        """本轮已重发次数（不含首点）——仅供日志 / 断言，不是调用方可自持的第二份状态。"""
        return self._count

    @property
    def resend_in_flight(self) -> bool:
        """是否有一次重发已发出、尚未确认送达（`step_cmd=RETRY` 应持续有效）。"""
        return self._resend_in_flight

    def arm(self) -> None:
        """标记一次尝试**发出 / 送达**（treasure：`ok=true`；speedrush：一次 `run(action, confirm)`）。

        折叠在途重发资格（`_resend_in_flight=False`）、把计时基线挪到此刻，**不动计数**。
        各链是否在每次送达都再 `arm` 是其差异：阶段切换 / 结算每次点击成功都 `arm`（窗口量到最近
        一次送达、送达后转 WAIT 等新后置条件）；面板数字键重发送达时不 `arm`（hold 持续到窗尾自然
        推进下一次预算）。机器两种喂法都支持，差别全在插件调不调 `arm`。
        """
        self._since_ts = self._clock()
        self._resend_in_flight = False

    def observe(self, verdict: Verdict) -> Command:
        """喂入本拍 verdict，机器据计数与窗口决定下一步命令。

        `count++` **只在窗口到期时发生、且每窗一次**（同窗内每拍不重复 ++）。一次到期 `RETRY`
        后进入"重发在途"：未送达期间每拍仍返回 `RETRY`（结算据此把 `step_cmd` 保持为 `RETRY`、
        规则持续重发，绝不因本拍 `WAIT` 而丢失重发资格）；该在途重发**送达**时插件 `arm()` 折叠它、
        转回 `WAIT`；若窗口先到期仍未送达，则视为该次无响应 → 推进一次预算再 `RETRY`。到上限后
        需**再等一个完整窗口**仍 `PENDING` 才 `FAIL`。
        """
        if verdict is Verdict.STOP:
            return Command.ABORT
        if verdict is Verdict.SUCCESS:
            # 后置条件达成 = 该次尝试送达且生效 → 折叠在途重发，转 DONE。
            self._resend_in_flight = False
            return Command.DONE
        # PENDING
        if self._expired():
            if self._count >= self._max_retries:
                return Command.FAIL
            self._count += 1
            self._since_ts = self._clock()
            self._resend_in_flight = True
            return Command.RETRY
        if self._resend_in_flight:
            return Command.RETRY  # 重发在途、尚未送达：持续重发，不重复 ++、不丢资格
        return Command.WAIT

    def _expired(self) -> bool:
        return (self._clock() - self._since_ts) >= self._window_s


def at(start: float = 0.0) -> tuple[Callable[[], float], Callable[[float], None]]:
    """受控假钟：返回 `(读值, 拨值)`，供单测做确定性计时验证。

    沿用 gating 受控时钟范式——把 `read` 作 `clock` 传给 `StepMachine`，用 `set_` 拨到
    指定时刻，不依赖真实 `time.monotonic` 推进。
    """
    state = {"t": start}

    def read() -> float:
        return state["t"]

    def set_(value: float) -> None:
        state["t"] = value

    return read, set_

