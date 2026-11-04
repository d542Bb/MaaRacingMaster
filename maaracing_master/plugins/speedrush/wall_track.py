# -*- coding: utf-8 -*-
"""每侧逐 z 箱的墙假设跟踪（walls-only 第 5 步，2026-10-09）。

**它是什么**：把 ``DepthRoadReading.edge_pts`` 的逐箱墙观测组织成跨帧的
墙假设——每个 (side, zc) 箱一个槽位，同箱观测在紧容差内重复才累积置信；
观测缺失（车挡/无点）的箱标遮挡（occluded），位置沿用、置信衰减。

**它不是什么**：不做跨帧运动补偿的轨迹拼接。控制回路没有任何实际位移量
（HUD 刻意不读里程/车速，无遥测——2026-10-09 核实，控制指令不是实际位移）；
前进运动更替 z 箱里的墙段，dz 无来源。83 包探针实测同箱观测漂移 p50=0.14 道
（直行抖动，2842→2866 目检证实直路自车未动、−0.6 道"漂移"是平面倾斜波动+
浅结构穿越位置的测量抖动，不是 ego 运动）——超容差一律**重置不拼接**：
测量抖动与场景切换同归于重置，墙假设知道自己何时不再可信。

**置信语义**（评审门禁）：置信只来自新鲜一致证据，无观测按 CONF_DECAY 衰减、
低于 CONF_FLOOR 失效——不存在靠历史无限延续的高置信度；遮挡标记必须有
历史证据（conf ≥ OCCL_MIN_CONF），单帧观测杀不死已攒置信的墙，但墙也
不会假装自己还在场。"""
from __future__ import annotations

from dataclasses import dataclass

from maaracing_master.plugins.speedrush.world_model import Calib

# 同箱重复容差（米；探针 p50=0.14 道≈0.47m，0.3 道≈1.0m 覆盖 76%）——
# 容差内的重复攒置信，之外的观测重置槽位（不拼接）。
TOL_M = 1.0
CONF_DECAY = 0.97      # 无观测衰减（**每 fid**，按 fid 差缩放：衰减是时间的函数
                       # 不是更新次数的函数——离线重放 20 fid 包距下逐 update
                       # 衰减会把置信在包间隙里衰减到死，2026-10-09 实测记案）。
                       # 满置信遮挡存活约 2s，与车经过遮挡段时间同量级
CONF_FLOOR = 0.5       # 置信下限（低于即失效，槽位作废）
CONF_CAP = 4.0         # 置信上限（几十帧的连续一致不该压过场景切换的重置）
OCCL_MIN_CONF = 1.5    # 遮挡标记的最少历史置信（评审门禁：有历史证据才标 occluded）

MEASURED, OCCLUDED = "measured", "occluded"


@dataclass
class _BinSlot:
    """一个 (side, zc) 箱的墙假设：位置（米）、置信、最近在场 fid、状态。"""
    pos_m: float
    conf: float
    last_fid: int
    state: str = MEASURED


class WallTrack:
    """逐箱墙假设（每侧单实体 per 箱；fid 单调，倒退即重置——同 Tracker 纪律）。"""

    def __init__(self, cal: Calib) -> None:
        self._cal = cal
        self._bins: dict[tuple[int, float], _BinSlot] = {}
        self._last_fid = -1

    def update(self, reading, fid: int) -> None:
        """一帧墙观测进账（reading.edge_pts 或直接给 (side, zc, x_m) 序列；
        fid 重复帧跳过）。复用/弃权拍给空序列=只走衰减（置信是时间的函数）。"""
        if fid <= self._last_fid:
            return
        frames = reading.edge_pts if hasattr(reading, "edge_pts") else reading
        seen: set[tuple[int, float]] = set()
        for s, zc, x_m in frames:
            key = (s, zc)
            slot = self._bins.get(key)
            if slot is None or abs(x_m - slot.pos_m) > TOL_M:
                self._bins[key] = _BinSlot(float(x_m), 1.0, fid)
            else:
                slot.pos_m = float(x_m)
                slot.conf = min(slot.conf + 1.0, CONF_CAP)
                slot.last_fid = fid
                slot.state = MEASURED       # 重观测=重新在场（遮挡解除）
            seen.add(key)
        for key, slot in self._bins.items():
            if key in seen:
                continue
            # 遮挡判定用衰减前置信：攒到门槛的被挡箱当拍即标遮挡，随后衰减
            slot.state = OCCLUDED if slot.conf >= OCCL_MIN_CONF else ""
            slot.conf *= CONF_DECAY ** (fid - slot.last_fid)
            if slot.conf < CONF_FLOOR:
                slot.state = ""
        self._last_fid = fid

    def bins(self, side: int) -> list[tuple[float, float, float, str, int]]:
        """该侧的墙假设清单 [(zc, pos_m, conf, state, last_fid)]；state 为 ''
        的不吐（last_fid=证据年龄，走廊标注"证据有多新"用）。"""
        return sorted((zc, sl.pos_m, sl.conf, sl.state, sl.last_fid)
                      for (s, zc), sl in self._bins.items()
                      if s == side and sl.state and sl.conf >= CONF_FLOOR)
