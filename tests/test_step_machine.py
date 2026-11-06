#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一步循环重试状态机单测（纯逻辑，假钟，CI 轻量依赖集可跑）。

每条 treasure 链用一台 `StepMachine` 复刻其"计数表"逐格行为（重发次数 / 计时起止 /
耗尽时机），并核对三链共同的、迁移时最易错的三点：
① 重发上限**不含首点**；② 耗尽是"到上限后**再等一个窗口**"才抛、非数满即抛；
③ 计时在每次重发 / 相关事件时重置、非从首点累计。

各链差异只体现在**插件何时调 `arm()`**：阶段切换 / 结算每次送达都再 `arm`（窗口量到
最近一次送达）；面板数字键送达时不 `arm`（纯从上次重发计窗）。同一台机器两种喂法都能
复现——这正是"真统一"的证据。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.step_machine import Command, StepExhaustedError, StepMachine, Verdict
from maaracing_master.core.step_machine import at

W = 1.0  # 统一测试窗口（秒）


# ---- 机器自身契约：arm 只重置窗口、count++ 只在到期重发 ---------------------


def test_arm_resets_window_but_not_count():
    """点 1/点 2：`arm` 只重置窗口、绝不动计数；`count++` 只在窗口到期决定重发时发生。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    m.arm()
    set_(0.5)
    assert m.observe(Verdict.PENDING) is Command.WAIT  # 窗口内
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY  # 到期才 ++
    assert m.count == 1
    # 送达再 arm：窗口从头计，计数不回退、不 ++
    m.arm()
    set_(1.2)
    assert m.observe(Verdict.PENDING) is Command.WAIT
    assert m.count == 1
    set_(2.2)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 2


def test_first_window_frames_wait_without_recount():
    """首点送达后、尚未发生任何重发：首窗内每帧 PENDING 一律 WAIT，不预支预算。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    m.arm()
    for t in (0.1, 0.3, 0.5, 0.7, 0.9):
        assert m.observe(Verdict.PENDING) is Command.WAIT
    assert m.count == 0
    assert not m.resend_in_flight


def test_resend_in_flight_holds_retry_without_recounting():
    """点 3 核心时序（GPT 点名、旧实现漏锁）：一次到期 `RETRY` 后动作未送达，
    在途窗内每拍持续返回 `RETRY`（结算据此让 `step_cmd` 一直是 `RETRY`、规则不断重发），
    但 `count++` 仍**每窗一次**——多帧不重复消耗预算、也不丢重发资格。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    m.arm()
    # t=1.0 到期 → 第 1 次重发，进入在途
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 1
    assert m.resend_in_flight
    # 在途窗内密集喂 PENDING（模拟重发迟迟未送达）：全 RETRY、持续重发、不 ++
    for t in (1.1, 1.3, 1.5, 1.7, 1.9):
        assert m.observe(Verdict.PENDING) is Command.RETRY
        assert m.count == 1  # 每拍都没重复消耗预算
    # 窗尾再到期 → 才 ++ 到第 2 次预算（证明"每窗一次"，非"每拍一次"）
    set_(2.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 2


def test_delivery_arm_collapses_in_flight_hold():
    """送达（`arm()`）折叠在途 hold：重发被确认送达后转回 WAIT、以送达点为新窗口基准
    （阶段切换 / 结算形态：窗口量到最近一次送达）。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    m.arm()
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY  # 到期 → 重发，进入在途
    assert m.resend_in_flight
    m.arm()  # 该重发送达 → 折叠 hold、窗口重置到此刻
    assert not m.resend_in_flight
    set_(1.2)
    assert m.observe(Verdict.PENDING) is Command.WAIT  # 在途被送达解除，转等新后置条件
    assert m.count == 1  # 送达不动计数


def test_lazy_pending_before_first_arm_does_not_hang():
    """忘了先 arm 不能变成"永远 WAIT"：首次 PENDING 惰性设基准，下一窗照常重发。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    assert m.observe(Verdict.PENDING) is Command.WAIT  # 惰性起点，不立即吃预算
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 1


# ---- 三 verdict × 五 command：终态分道 ------------------------------------


def test_success_returns_done_midchain():
    """阶段切换中途真切走 = 后置条件达成 → DONE（终态），预算不再消耗。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    m.arm()
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY  # 第一次重发
    m.arm()
    set_(1.6)
    assert m.observe(Verdict.SUCCESS) is Command.DONE


def test_stop_returns_abort_without_consuming_budget():
    """点 3 / 边界"停止≠失败"：STOP→ABORT 不吃预算、不改计数，且幂等。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    m.arm()
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 1
    set_(1.2)
    assert m.observe(Verdict.STOP) is Command.ABORT
    assert m.count == 1
    set_(1.4)
    assert m.observe(Verdict.STOP) is Command.ABORT  # 重复停止仍是 ABORT


def test_exhaust_fail_is_distinct_from_stop_abort():
    """耗尽→FAIL 与停止→ABORT 分道：同为终态但收尾不同，不得合并。"""
    assert Command.FAIL is not Command.ABORT
    read, set_ = at(0.0)
    m = StepMachine(max_retries=1, window_s=W, clock=read)
    m.arm()
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY  # count=1（达上限的那次重发）
    m.arm()
    set_(2.0)
    assert m.observe(Verdict.PENDING) is Command.FAIL  # 达上限后再等一窗才 FAIL
    assert m.count == 1


def test_not_feeding_stop_machine_only_sees_success_pending():
    """treasure 接法（边界 3）：外层已自管停止、不喂 STOP 时，机器只见 SUCCESS/PENDING、永不产 ABORT。

    只喂 PENDING、从不 arm（无送达）：重发在途窗内持续 `RETRY`，`count++` 仍每窗一次；
    达上限后再等一窗才 `FAIL`（共 2 次重发预算）。
    """
    read, set_ = at(0.0)
    m = StepMachine(max_retries=2, window_s=W, clock=read)
    m.arm()
    cmds = []
    for t in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        set_(t)
        cmds.append(m.observe(Verdict.PENDING))
    assert Command.ABORT not in cmds
    assert cmds == [
        Command.WAIT,   # 首窗内
        Command.RETRY,  # t=1.0 到期 → 第 1 次重发（进入在途）
        Command.RETRY,  # 在途窗内持续重发，不 ++
        Command.RETRY,  # t=2.0 到期 → 第 2 次重发（达上限那次）
        Command.RETRY,  # 在途窗内持续重发
        Command.FAIL,   # t=3.0 到期、count>=max → FAIL
    ]
    assert m.count == 2  # 每窗一次，两窗共 ++ 到 2，未因在途帧重复计


# ---- 逐链复刻计数表（同一台机器，差异只在插件喂法） -----------------------


def test_stage_switch_chain_matches_counting_table():
    """阶段切换链（max=3，CLICK_RETRY_MAX）：首点 + 3 次重发 = 最多 4 次点击尝试；
    每次送达再 arm（窗口量到最近送达）；达上限后再等一窗才 FAIL。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=3, window_s=W, clock=read)
    attempts = 1  # 首点
    m.arm()
    # 第 1 次重发
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    attempts += 1
    m.arm()  # 重发送达再 arm
    # 第 2 次重发
    set_(2.2)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    attempts += 1
    m.arm()
    # 第 3 次重发（达上限那次）
    set_(3.4)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    attempts += 1
    m.arm()
    # 达上限后再等一窗：窗口内仍 WAIT
    set_(3.6)
    assert m.observe(Verdict.PENDING) is Command.WAIT
    # 到期 → FAIL
    set_(4.4)
    assert m.observe(Verdict.PENDING) is Command.FAIL
    assert m.count == 3
    assert attempts == 4  # 不含首点重发上限 3，总尝试 4


def test_numpad_chain_matches_counting_table():
    """面板数字键链（max=2，BID_DIGIT_RETRY_MAX）：预算 2 次重发 → 首点 + 2 = 最多 3 次有效尝试。

    数字键**重发送达不 arm**（②不用），故一次到期 `RETRY` 后 hold 持续到窗尾：窗内每拍仍 `RETRY`
    （规则据指纹持续重发同一数字），`count++` 仍每窗一次；达上限后再等一窗才 `FAIL`。与阶段切换
    用同一台机器——差别只在插件调不调 `arm`。
    """
    read, set_ = at(0.0)
    m = StepMachine(max_retries=2, window_s=W, clock=read)
    m.arm()  # 新数字意图
    set_(0.5)
    assert m.observe(Verdict.PENDING) is Command.WAIT
    # 第 1 次重发预算（t=1.0 到期 ++）；随后 hold 到窗尾，窗内持续 RETRY、不重复 ++
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 1
    set_(1.5)
    assert m.observe(Verdict.PENDING) is Command.RETRY  # 在途持续重发
    assert m.count == 1  # 未重复消耗预算
    # 第 2 次重发预算（达上限那次，t=2.0 到期 ++）
    set_(2.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 2
    set_(2.5)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 2
    # 达上限后再等一窗 → FAIL
    set_(3.0)
    assert m.observe(Verdict.PENDING) is Command.FAIL


def test_settle_skip_chain_matches_counting_table():
    """结算跳过链：跳过点击每次送达再 arm（对齐现状"每次点击成功都重启计时"，3515）；
    count 在到期重发时 ++（对齐"计数在重试决策时 ++、非点击成功时 ++"）；收入读出=SUCCESS。"""
    read, set_ = at(0.0)
    m = StepMachine(max_retries=2, window_s=W, clock=read)
    m.arm()  # 首点（跳过动画）送达
    set_(1.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY  # 无响应，重发
    m.arm()  # 重发送达再 arm
    set_(2.0)
    assert m.observe(Verdict.PENDING) is Command.RETRY
    assert m.count == 2
    m.arm()
    set_(3.0)
    assert m.observe(Verdict.PENDING) is Command.FAIL  # 达上限后再等一窗
    # 收入读出 → SUCCESS 走 DONE 分道（与 FAIL 互斥）
    read2, set2 = at(0.0)
    m2 = StepMachine(max_retries=2, window_s=W, clock=read2)
    m2.arm()
    set2(1.0)
    assert m2.observe(Verdict.SUCCESS) is Command.DONE


# ---- 参数校验与异常类型 ---------------------------------------------------


def test_constructor_validates_dose():
    with pytest.raises(ValueError):
        StepMachine(max_retries=-1, window_s=W)
    with pytest.raises(ValueError):
        StepMachine(max_retries=3, window_s=0)


def test_step_exhausted_is_runtime_error():
    """treasure 的 `ClickRetryExhaustedError` 保留为本类别名（迁移期不动捕获点）。"""
    assert issubclass(StepExhaustedError, RuntimeError)
