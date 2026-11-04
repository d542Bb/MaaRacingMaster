# -*- coding: utf-8 -*-
"""LifecycleAdapter.sleep 墙钟语义测试：可中断睡眠必须真的在睡。

背景（真机 2026-09-14 彩蛋链 12s 空转）：旧实现按 int(seconds / 0.1) 量化迭代，
小于 0.1s 的入参静默退化为**一次都不睡**的忙旋（决策线程 41 万次/6s 空转、持 GIL
饿死导航 worker 与观察线程），非整数倍入参（如 0.25s）也向下截断缩水。本测试锁住
「实际睡眠时长 ≥ 入参」这条底线，防量化回潮；改 sleep 实现后必须仍然全绿。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from maaracing_master.core.capabilities import LifecycleAdapter  # noqa: E402


class _FakeApp:
    """最小宿主：只给 LifecycleAdapter 读的两个属性。"""

    def __init__(self):
        self._running = True
        self.stop_event = threading.Event()


def _sleep_wallclock(lc: LifecycleAdapter, seconds: float) -> tuple[bool, float]:
    # 计时必须用 perf_counter：monotonic 在 Windows 步长 ~15.6ms（GetTickCount64），
    # 睡满 50ms 会被量化读成 47ms，制造"睡眠不足"假红（2026-10-06 实证）。
    t0 = time.perf_counter()
    ret = lc.sleep(seconds)
    return ret, time.perf_counter() - t0


def test_sleep_sub_granularity_meets_wall_clock():
    """小于旧量化粒度的入参不得退化为零睡眠（真机事故的原样回归）。"""
    ret, elapsed = _sleep_wallclock(LifecycleAdapter(_FakeApp()), 0.05)
    assert ret is True
    assert elapsed >= 0.05, f"sleep(0.05) 实际只等了 {elapsed:.4f}s——忙旋回潮"


def test_sleep_remainder_meets_wall_clock():
    """非 0.1s 整数倍入参不得向下截断（0.25 旧实现只睡 0.2）。"""
    ret, elapsed = _sleep_wallclock(LifecycleAdapter(_FakeApp()), 0.25)
    assert ret is True
    assert elapsed >= 0.25, f"sleep(0.25) 实际只等了 {elapsed:.4f}s——余数被截断"


def test_sleep_is_interruptible():
    """睡眠中途收到停止信号必须提前返回 False，等待不得拖过下一个分片粒度。"""
    app = _FakeApp()
    lc = LifecycleAdapter(app)

    def _stop_later():
        time.sleep(0.15)
        app.stop_event.set()

    t = threading.Thread(target=_stop_later, daemon=True)
    t.start()
    try:
        ret, elapsed = _sleep_wallclock(lc, 5.0)
    finally:
        t.join(timeout=2.0)
    assert ret is False
    assert elapsed < 1.0, f"停止信号 0.15s 时置位，却等了 {elapsed:.2f}s——不可中断性劣化"


def test_sleep_zero_and_negative_return_immediately():
    lc = LifecycleAdapter(_FakeApp())
    ret, elapsed = _sleep_wallclock(lc, 0.0)
    assert ret is True and elapsed < 0.05
    ret, elapsed = _sleep_wallclock(lc, -1.0)
    assert ret is True and elapsed < 0.05


def test_sleep_quantization_floor():
    """高分辨率等待：36ms 请求（控制拍 rest 的典型值）过冲 <8ms。

    旧量化形态：time.sleep 落在 OS 定时器网格（~15.6ms 台阶），36ms 实睡
    46.8ms（过冲 10.8ms）——控制拍 33/50/66ms 三模态的根子。本断言在默认
    定时器实现下必红（46.8 > 44），高分辨率定时器下绿（~36.5ms）。
    负载极重的机器可能假红——语义测试（上面四条）在任何环境都必须绿。"""
    ret, elapsed = _sleep_wallclock(LifecycleAdapter(_FakeApp()), 0.036)
    assert ret is True
    assert elapsed < 0.036 + 0.008, \
        f"sleep(0.036) 实睡 {elapsed*1000:.1f}ms——OS 定时器量化回潮"
