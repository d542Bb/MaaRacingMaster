#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DirectML 推理互斥：同进程内任意两个 DML ``session.run`` 不得同时在飞。

本机实证（2026-09-25，onnxruntime-directml 1.24.4 / NVIDIA 驱动 32.0.15.9649）：
感知 model.onnx 与深度权重（时为 depth_small_q4f16.onnx，2026-10-01 换装
moge2 q4f16，锁协议不变）各建一个 DML 会话——
- 两线程并发 run：数秒内段错误杀进程（0xc0000005 @ nvwgf2umx.dll，NVIDIA 用户态
  驱动；2026-09-25 21:00 实机对局同型崩溃，进程带虚拟手柄一起消失）；
- 同两会话单线程交替 run：1874 对 / 60s 干净；
- 单会话独跑：深度 400 次、感知 2545 次均干净。
判据因此是「并发」而非「多会话」：跨线程使用 DML 会话的一方必须让锁，抢不到就
放弃本次（深度帧是 latest-only 慢变量，弃一帧无损）；控制拍侧（感知）阻塞持有
——感知不能跳拍。

持锁范围只圈 ``session.run`` 本体：前后处理不碰 GPU，留在锁外。
"""

from __future__ import annotations

import threading

LOCK = threading.Lock()


class Busy(RuntimeError):
    """DML 已被其他线程占用；调用方应放弃本次推理（让锁），不得等待。"""
