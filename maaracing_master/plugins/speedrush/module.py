# -*- coding: utf-8 -*-
"""极速狂飙（speedrush）模块：地铁跑酷式车道选择玩法的自动化。

玩法与规则事实见本域 ``RULES.md``；识别锚点与点击资产见
``resources/pipeline/speedrush.json``。

流程编排（本文件的核心职责）
--------------------------
一轮循环 = 打一个回合（两个驾驶阶段）后主动放弃本轮、结算段位分，回到活动页再开始。
步骤序列见 ``FLOW``，与 RULES.md §2.2 的操作路径逐条对应。

**为什么流程写在 Python 而不是图的 next 边**：图的边表达不了「同一页面在不同时机
点不同按钮」——回合开始页首轮要点「寻找对手」、次轮要点「放弃本轮」，两页视觉相同，
差别只是「现在是第几个回合」，这是状态。若用两套节点表达，同一模板会被两个节点认领，
触发机检的跨锚点重复识别告警；若靠 ``__boot`` 自动择一，则永远先命中「寻找对手」。
故由本文件显式驱动每一步，复用同一批原子动作节点。

**单步驱动为何成立**：``NavGraph.run(entry, reached)`` 阻塞到图跑完、再用 reached
事后校验，所以动作节点必须是零路由叶子（见
``tools/experiments/speedrush_templates/build_truth.py`` 的注释），否则框架会顺边
跑下去而失控。

驾驶阶段是留给控制方案的接口，契约见 ``_drive`` 的 docstring。
"""

from __future__ import annotations

import time

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger
from maaracing_master.core.nav_graph import NavGraph
from maaracing_master.plugins.speedrush import IMAGE_DIR, PIPELINE_DIR

# 一轮完整流程。首三段（进入活动）只在首轮需要——每轮循环结束时会回到活动页，
# 故其后每轮从「开始挑战」起。
# 元素 = (阶段名, 图节点名, 驾驶阶段号)；节点名为 None 表示驾驶阶段。
FLOW: list[tuple[str, str | None, int]] = [
    ("进入比赛页", "speedrush.点比赛", 0),
    ("进入娱乐玩法", "speedrush.点娱乐玩法", 0),
    ("进入极速狂飙", "speedrush.点极速狂飙卡片", 0),
    ("开始挑战", "speedrush.点开始挑战", 0),
    ("寻找对手", "speedrush.点寻找对手", 0),
    ("关闭奖励弹窗", "speedrush.关闭奖励弹窗", 0),
    ("确认上阵", "speedrush.点确认上阵", 0),
    ("驾驶阶段1", None, 1),
    ("关闭奖励弹窗2", "speedrush.关闭奖励弹窗", 0),
    ("确认上阵2", "speedrush.点确认上阵", 0),
    ("驾驶阶段2", None, 2),
    ("回合结果继续", "speedrush.点继续", 0),
    ("放弃本轮", "speedrush.点放弃本轮", 0),
    ("继续放弃", "speedrush.点继续放弃", 0),
    ("结算继续", "speedrush.点继续", 0),
]

# 循环体起点：一轮跑完回到这里，入口链只在首轮走一次。
LOOP_START_INDEX = 3

# 驾驶页锚点节点名（图侧纯锚点，用于判定「当前是否还在对局中」）
DRIVE_STAGE_NODE = "speedrush.驾驶页锚点"

# 驾驶阶段等待上限。单局（含两阶段）实测一般 3 分钟、耗满可达 6~7 分钟
# （RULES.md §4.7），取 10 分钟留余量。
DRIVE_TIMEOUT_S = 600.0
# 轮询间隔：单次 run() 含 post_task，间隔过密会白耗框架开销。
DRIVE_POLL_S = 2.0
# 连续多少次识别不到驾驶页锚点才判定「已离开对局」。取 2 是给动态场景下的
# 偶发失配留一次容错——齿轮图标跨帧实测 0.92~1.00，阈值 0.8 本有余量。
DRIVE_MISS_TOLERANCE = 2


class SpeedRushModule(ActivityModule):
    """极速狂飙：地铁跑酷式车道选择玩法。"""

    ID = "speedrush"
    NAME = "极速狂飙"
    # GUI 断点选择用；与 FLOW 阶段名同源，不另抄一份清单
    STAGE_ORDER: list[str] = [stage for stage, _, _ in FLOW]
    # 驾驶阶段全程占用手柄（油门常踩 + 横向连续转向）
    REQUIRES_GAMEPAD_EXCLUSIVE = True
    REQUIRES: frozenset[str] = frozenset({"capture", "gamepad"})

    def __init__(self, ctx: ActivityContext | None) -> None:
        super().__init__(ctx)
        self._graph: NavGraph | None = None
        self._running = False
        self._stage_name: str | None = None

    @property
    def current_stage(self) -> str | None:
        return self._stage_name

    # ---------- 生命周期 ----------

    def start(self, start_from: str | None = None) -> None:
        """启动流程（阻塞；由外层 worker 线程调用）。"""
        assert self.ctx is not None, "离线模式（ctx=None）不可调用 start()"
        if not self.ctx.connect():
            logger.log("[极速狂飙] 窗口连接失败，模块终止", "ERROR")
            return
        # 模板与 ROI 均按 720p(16:9) 归一化，其他比例会识别错位
        if not self.ctx.check_window_aspect():
            logger.log(
                "游戏窗口不是 16:9 比例（模板与识别区域均按 720p(16:9) 设计，"
                "其他比例会识别错位）。请调整为 16:9 后重新开始，模块已终止",
                "ERROR",
            )
            return

        graph = NavGraph(self.ctx)
        graph.add_plugin(PIPELINE_DIR, IMAGE_DIR)
        if not graph.load():
            logger.log("[极速狂飙] 导航图加载失败，模块终止", "ERROR")
            return
        self._graph = graph

        index = resolve_start_index(start_from)
        if start_from and index == 0:
            logger.log(f"[极速狂飙] 未知断点「{start_from}」，从流程起点开始", "WARNING")

        self._running = True
        try:
            self._run_flow(index)
        finally:
            self._running = False
            self._stage_name = None

    def stop(self) -> None:
        """幂等停止：置停止标志并中断在跑的图。"""
        self._running = False
        if self._graph is not None:
            self._graph.stop()

    def cleanup(self) -> None:
        """幂等释放模块资源。"""
        if self._graph is not None:
            self._graph.shutdown()
            self._graph = None
        self._stage_name = None

    # ---------- 流程推进 ----------

    def _run_flow(self, index: int) -> None:
        """按 FLOW 推进；走完一轮回到循环体起点继续，直到收到停止信号。"""
        assert self._graph is not None
        while self._running:
            while index < len(FLOW):
                if not self._running:
                    return
                stage, node, phase = FLOW[index]
                self._stage_name = stage
                if node is None:
                    if not self._drive(phase):
                        logger.log(f"[极速狂飙] 「{stage}」未正常结束，本轮中止", "WARNING")
                        return
                elif not self._graph.run(node, node):
                    logger.log(f"[极速狂飙] 步骤「{stage}」未到位，本轮中止", "WARNING")
                    return
                index += 1
            index = LOOP_START_INDEX

    def _drive(self, phase: int) -> bool:
        """驾驶阶段（phase = 1/2）——驾驶控制方案的接口点。

        TODO(待定)：驾驶控制待与维护者讨论后实现。当前实现**不操纵车辆**，只等待
        本阶段结束（车不动时对局由游戏自身结束），用于先把流程骨架跑通。

        契约（任何实现都必须满足）：
          - 进入时画面已在驾驶中；退出时本阶段已结束、画面已切回 UI
          - 响应 ``self._running``，收到停止信号立即返回
          - 不自建、不销毁手柄设备（设备复位统一由外层 ``reset_device`` 处理）
          - 返回 False = 本阶段无法完成，调用方会中止本轮

        就绪判据用驾驶页锚点（齿轮图标）反查：仍能识别到 = 还在对局中。
        """
        assert self.ctx is not None
        assert self._graph is not None
        logger.log(f"[极速狂飙] 驾驶阶段 {phase}：等待阶段结束（驾驶控制尚未实现）", "INFO")
        deadline = time.monotonic() + DRIVE_TIMEOUT_S
        miss = 0
        while self._running and time.monotonic() < deadline:
            if self._graph.run(DRIVE_STAGE_NODE, DRIVE_STAGE_NODE):
                miss = 0
            else:
                miss += 1
                if miss >= DRIVE_MISS_TOLERANCE:
                    return True
            if not self._running:
                break
            if not self.ctx.lifecycle.sleep(DRIVE_POLL_S):
                break
        logger.log(f"[极速狂飙] 驾驶阶段 {phase} 未在 {DRIVE_TIMEOUT_S:.0f}s 内结束", "WARNING")
        return False


def resolve_start_index(start_from: str | None) -> int:
    """把 GUI 断点的阶段名换算为 FLOW 下标；缺省或未知值一律回退 0。"""
    if not start_from:
        return 0
    for i, (stage, _, _) in enumerate(FLOW):
        if stage == start_from:
            return i
    return 0