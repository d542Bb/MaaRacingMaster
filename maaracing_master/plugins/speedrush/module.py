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

import contextlib
import json
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger
from maaracing_master.core.nav_graph import NavGraph
from maaracing_master.core.paths import data_dir
from maaracing_master.plugins.speedrush import (
    DEPTH_MODEL_FILE, DEPTH_MODEL_REL, IMAGE_DIR, PERCEPTION_MODEL_FILE,
    PERCEPTION_MODEL_REL, PIPELINE_DIR)
from maaracing_master.plugins.speedrush.coin_group import CoinGroupAggregator
from maaracing_master.plugins.speedrush.config import load_decision
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.depth_geo import (
    AsyncDepthRoadObserver, DepthRoadObserver, load_session)
from maaracing_master.plugins.speedrush.hud import HudObserver
from maaracing_master.plugins.speedrush.perception import PerceptionResult, StreetPerception
from maaracing_master.plugins.speedrush.planner import LateralPlanner
from maaracing_master.plugins.speedrush.recorder import DriveRecorder, make_session_dir
from maaracing_master.plugins.speedrush.traffic import OUTCOME_PASS, TrafficObserver
from maaracing_master.plugins.speedrush.tracking import DecisionState, Tracker
from maaracing_master.plugins.speedrush.world_model import load_calib

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

# 驾驶页锚点节点名（图侧纯锚点，用于判定「当前是否还在对局中」）。
# 图里该节点的 rate_limit 已显式置零：驾驶循环每轮都要问它一次，而框架默认的
# 「每轮识别最低消耗」是 1s——吃默认值时实测主循环被压到约 1Hz，采集帧率与阶段
# 响应双双不可用。置零后单轮只剩框架开销与匹配本身。
#
# 阈值取 0.5 的实测依据（两轮录制帧复算）：驾驶途中齿轮分随场景从 0.93 缓降到 0.80，
# 贴着阈值 0.8 会抖动成假失配——实机已因此误判过一次"阶段结束"（随后流程去找奖励
# 弹窗，而画面还在驾驶中，白等 20 秒后中止）；而真正离开驾驶页时它掉到 0.02 量级。
# 两端分离度极大，阈值取中间，两侧都留足余量。
DRIVE_STAGE_NODE = "speedrush.驾驶页锚点"

# 驾驶阶段等待上限。单局（含两阶段）实测一般 3 分钟、耗满可达 6~7 分钟
# （RULES.md §4.7），取 10 分钟留余量。
DRIVE_TIMEOUT_S = 600.0
# 连续多少次识别不到驾驶页锚点才判定「已离开对局」。取 2 是给动态场景下的
# 偶发失配留一次容错；阈值余量与两端的实测分离度见 DRIVE_STAGE_NODE 的说明。
DRIVE_MISS_TOLERANCE = 2

# ---------- 驾驶主循环节拍 ----------

# 主循环目标频率。与后续模型输入契约的采样率一致（"K 帧堆叠"的时间跨度按此定义）。
DRIVE_TICK_HZ = 30.0
DRIVE_TICK_S = 1.0 / DRIVE_TICK_HZ

# 驾驶页锚点复查间隔。锚点识别走框架（post_task + 等待，单轮毫秒级），不必每帧跑；
# 采集/控制节拍与它解耦——帧各自带真实时间戳，离线按时间戳对齐，不受此频率污染。
DRIVE_ANCHOR_CHECK_S = 0.5

# 进入驾驶的就绪判据：连续命中驾驶页锚点多少次才认为"已在对局中"。
# **待收紧**：起步动画的真实特征尚未实测，本判据只保证"驾驶页已稳定出现"，
# 可能早于"车辆可操控"。取得动画样本后须按其特征加判据（见 docs/plan
# speedrush-drive-plan.md 第十节门控）。
DRIVE_READY_HITS = 2
# 等待驾驶就绪的上限：动画再长也不该无限等。
DRIVE_READY_TIMEOUT_S = 30.0
# 就绪等待的轮询间隔
DRIVE_READY_POLL_S = 0.3


def _frame_note(fid: int, age_ms: float) -> str:
    """帧证据锚点，形如 ``frame=1234 age=12ms``，随门控判定的日志一起打出。

    **为什么需要它**：logger 的时间戳只到秒且是墙钟（``%H:%M:%S``），而门控判据的
    窗口是 0.3~1s 级；录制器的时间戳是单调时钟（``perf_counter_ns``）——两个时基
    无法直接对齐。**帧号是唯一的共同坐标**：拿日志里的 ``frame=`` 去该会话的
    ``frames.jsonl`` 里查，就得到那一刻画面所在的文件。没有这根针，"门控准不准"
    只能靠猜，改判据也就无从验证。

    ``age_ms`` 一并打出：它是判定所用帧的新鲜度，帧过旧本身就是门控误判的来源之一。
    """
    return f"frame={fid} age={age_ms:.0f}ms"


# ---------- 结构化日志组归属（契约 §8 第 7 步同款，A 案显式句柄） ----------
# 机制与形态照抄 treasure（见其「结构化日志组归属」段）：模块级函数 + 类上薄包装。
# _log_grp 仅生命周期 owner 线程（start / _run_flow / _drive / run finally）读写；
# worker 线程（recorder/HUD 观察线程）不得读取——本插件的 worker 内日志有意保持
# 无组（落盘失败/写帧失败属跨切面），需要归组时在派发点捕获不可变 group_id
# 显式 logger.log(group_id=...) 传参，迟到旧 id 由 logger 自动降级并计数。
# 跨切面日志（启动前提/设备/落盘）不塞组：见结构锁的白名单。

def _tlog(self, msg: str, level: str = "INFO", fields: dict | None = None) -> None:
    """阶段流日志：开组期间归当前组，无组落 legacy 通道。"""
    g = getattr(self, "_log_grp", None)
    if g is not None:
        g.log(msg, level, fields=fields)
    elif fields is not None:
        logger.log(msg, level, fields=fields)
    else:
        logger.log(msg, level)


def _open_grp(self, title: str, kind: str) -> None:
    """开业务组（GUI 卡片）；上一组先收尾（终态自动推导）。

    logger 无 group()（测试桩）时回退 legacy 锚点行，行为与现网一致。
    """
    if getattr(logger, "group", None) is None:
        logger.log(title)
        return
    _end_grp(self)
    self._log_grp = logger.group(title, kind=kind)


def _end_grp(self, outcome: str | None = None) -> None:
    """收尾当前组；outcome 缺省时按组内 ERROR/WARNING 自动推导终态。"""
    g = getattr(self, "_log_grp", None)
    if g is None:
        return
    self._log_grp = None
    g.end(outcome)


class SpeedRushModule(ActivityModule):
    """极速狂飙：地铁跑酷式车道选择玩法。"""

    ID = "speedrush"
    NAME = "极速狂飙"
    # GUI 断点选择用；与 FLOW 阶段名同源，不另抄一份清单
    STAGE_ORDER: list[str] = [stage for stage, _, _ in FLOW]
    # 驾驶阶段全程占用手柄（油门常踩 + 横向连续转向）
    REQUIRES_GAMEPAD_EXCLUSIVE = True
    REQUIRES: frozenset[str] = frozenset({"capture", "gamepad"})
    # 插件自带必需资源：两模型权重（sidecar 启动前检查，缺失拦截并给出具体路径）
    REQUIRED_ASSETS: tuple[str, ...] = (PERCEPTION_MODEL_REL, DEPTH_MODEL_REL)
    # 录制开关的初值（False = 不录制，驾驶阶段只等本阶段结束）
    DEFAULT_RECORD_MODE = False
    # 感知开关的初值（True = 驾驶阶段逐帧跑 YOLO 检测并记账；不操纵车辆，
    # 与录制可并存。控制环未接线前，它是"感知在场"的验证形态）
    DEFAULT_PERCEPTION_MODE = False
    # 控制开关的初值（True = 驾驶阶段跑全链并接管手柄）。V0/V1 两级不在此开关——
    # 由 decision.json 的 mode.allow_all_moves 决定（false=V0 只发油门走直线，
    # true=V1 开横向）。默认 False：接线首启不接管车辆，显式开才动（实机安全边界）。
    DEFAULT_CONTROL_MODE = False
    # 直行基线档的初值（False = 正常深度几何驾驶）。True = 驾驶阶段接管手柄后
    # 方向归零、油门恒踩，感知与深度都不加载（V0 语义：决策禁动恒 x_target=0）
    # ——「全程直行」基线金币率的测量仪器（control-route 验收②的分母）。
    DEFAULT_STRAIGHT_MODE = False
    # 配置面声明（GUI 配置项的键与初值；也是 profile 回填的白名单——不加进这里就不会被保存）
    DEFAULT_MODULE_CONFIG: dict = {
        "record_mode": DEFAULT_RECORD_MODE,
        "perception_mode": DEFAULT_PERCEPTION_MODE,
        "control_mode": DEFAULT_CONTROL_MODE,
        "straight_mode": DEFAULT_STRAIGHT_MODE,
    }

    # ---------- 启动约束（按本次配置求值，见基类说明）----------

    @classmethod
    def required_capabilities(cls, config: dict | None = None) -> frozenset[str]:
        """录制模式不需要手柄能力：该模式不操纵车辆，也不创建虚拟设备。

        不这么分的话，机器上没装 ViGEmBus 驱动时连"只录不控"都启动不了，
        而录制恰恰是拿到训练数据的前提。
        """
        caps = set(cls.REQUIRES)
        if bool((config or {}).get("record_mode")):
            caps.discard("gamepad")
        return frozenset(caps)

    @classmethod
    def requires_exclusive_gamepad(cls, config: dict | None = None) -> bool:
        """录制模式不独占手柄——演示数据就是维护者用物理手柄开出来的。

        代价为零：录制期模块不创建虚拟设备（见模块 docstring 的不变量），所以不存在
        "物理 + 虚拟"双输入冲突。真正驾驶时该约束照旧生效（那时程序接管输入，
        物理手柄必须断开）。
        """
        if bool((config or {}).get("record_mode")):
            return False
        return cls.REQUIRES_GAMEPAD_EXCLUSIVE

    def __init__(self, ctx: ActivityContext | None) -> None:
        super().__init__(ctx)
        self._graph: NavGraph | None = None
        self._running = False
        self._stage_name: str | None = None
        # 结构化日志当前组句柄：仅生命周期 owner 线程读写（见「结构化日志组归属」段）
        self._log_grp = None
        # 录制模式：开启后驾驶阶段只采集（不操纵车辆），供维护者手动驾驶产出演示数据。
        self._record_mode = self.DEFAULT_RECORD_MODE
        self._recorder: DriveRecorder | None = None
        # HUD 读数观察者：挂在录制会话上（见 _begin_hud 为何必须如此）
        self._hud: HudObserver | None = None
        # 感知模式：驾驶阶段逐帧物品检测（阶段 B 控制栈的第一路输入）。
        # 实例跨阶段复用——模型加载秒级，一局两阶段只付一次。
        self._perception_mode = self.DEFAULT_PERCEPTION_MODE
        self._perception: StreetPerception | None = None
        self._perception_failed = False
        # 控制模式：驾驶阶段跑全链并接管手柄（V0/V1 由 decision.json 决定，见类常量）
        self._control_mode = self.DEFAULT_CONTROL_MODE
        # 直行基线档：接管手柄后只踩油门（感知/深度不加载，见类常量注释）
        self._straight_mode = self.DEFAULT_STRAIGHT_MODE
        # road_offset 证据源=黄线簇间隙（v3 唯一语义，2026-09-28 维护者裁定
        # 删除 hsv 档）；深度退役为标定职责（照跑记账，不进实时回路）。
        # 深度几何的 ORT 会话：实例跨阶段复用（模型加载秒级，一局只付一次）；
        # 加载失败置 _depth_failed 后不再重试（降级为纯模型积分，不碰主循环）。
        self._depth_sess = None
        self._depth_failed = False
        # 控制回路耗时记账（验收判据 §二.3 的 P50/P95 数据源），按阶段清零
        self._infer_times: list[float] = []
        self._control_times: list[float] = []
        self._last_perception: PerceptionResult | None = None
        # 控制实况（GUI 展示：最近一拍的决策状态与下发杆值；控制关闭时为 None）
        self._control_last: dict | None = None

    @property
    def current_stage(self) -> str | None:
        return self._stage_name

    # ---------- 模块配置（GUI 读写；未定义时 RPC 层兜底为占位 dict） ----------

    def get_module_config(self) -> dict:
        """读配置。运行中由 sidecar 路由到本实例，故 ``_state`` 是实况而非快照。"""
        rec = self._recorder
        stats = rec.stats if rec is not None else None
        hud = self._hud
        hud_stats = hud.stats if hud is not None else None
        lp = self._last_perception
        return {
            "record_mode": bool(self._record_mode),
            "perception_mode": bool(self._perception_mode),
            "control_mode": bool(self._control_mode),
            "straight_mode": bool(self._straight_mode),
            "_state": {
                "recording": bool(rec is not None and rec.running),
                "frames": int(stats["frames_written"]) if stats else 0,
                "frames_dropped": int(stats["frames_dropped"]) if stats else 0,
                "pad_samples": int(stats["pad_samples"]) if stats else 0,
                # HUD 读数（与录制器同一时机启停；行数即"这个阶段的读数有没有产出"）
                "hud_recording": bool(hud is not None and hud.running),
                "hud_rows": int(hud_stats["rows_written"]) if hud_stats else 0,
                "demos_dir": str(_demos_root()),
                # 感知实况：是否在跑 + 最近一帧的检出与耗时（GUI 展示用）
                "perception_on": bool(lp is not None and self._perception is not None),
                "perception_last": None if lp is None else {
                    "frame_id": lp.frame_id,
                    "cars": len(lp.cars), "coins": len(lp.coins), "bonuses": len(lp.bonuses),
                    "infer_ms": round(lp.infer_ms, 1),
                },
                # 控制实况：最近一拍决策状态/下发杆值/自车横向估计（控制关则 None）
                "control_last": self._control_last,
            },
        }

    def set_module_config(self, config: dict) -> dict:
        """写配置。两条调用路径：controller 在 start 时注入实例、GUI 经 RPC 改缓存。

        非法值一律静默修正并返回最终值（与 treasure 同约定），不抛给调用方。
        """
        if isinstance(config, dict) and "record_mode" in config:
            self._record_mode = bool(config["record_mode"])
        if isinstance(config, dict) and "perception_mode" in config:
            self._perception_mode = bool(config["perception_mode"])
        if isinstance(config, dict) and "control_mode" in config:
            self._control_mode = bool(config["control_mode"])
        if isinstance(config, dict) and "straight_mode" in config:
            self._straight_mode = bool(config["straight_mode"])
        return self.get_module_config()

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

        # 录制模式的输入前提：点击方式为「前台鼠标」时全程不创建虚拟手柄；选了「后台手柄」
        # 的话，导航步骤会创建并常驻一个虚拟手柄（见 nav_graph 的 gamepad 分支），可能与其
        # 物理手柄冲突——而录制恰恰要他用物理手柄驾驶。冲突与否未实测，故只提示不拦截：
        # 真冲突的话他驾驶时会立刻发现手柄不响应，不会默默录坏数据。
        if self._record_mode and self.ctx.click_mode != "real":
            logger.log(
                "[极速狂飙] 录制模式提示：当前点击方式为「后台手柄」，导航步骤会创建虚拟手柄，"
                "可能与你的物理手柄冲突（驾驶时手柄可能不响应）。建议在设置页改为「前台鼠标」再录。",
                "WARNING")

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
        _open_grp(self, f"[极速狂飙] 模块启动（录制{'开' if self._record_mode else '关'}"
                        f"·感知{'开' if self._perception_mode else '关'}"
                        f"·直行{'开' if self._straight_mode else '关'}）", "session")
        try:
            self._run_flow(index)
        finally:
            # 契约 §2：speedrush 的 start 无"自然完成"出口（循环至停止），
            # 走到这里只有被停/阶段故障中止/异常三种，一律落 incomplete。
            _end_grp(self, "incomplete")
            self._running = False
            self._stage_name = None

    def stop(self) -> None:
        """幂等停止：置停止标志并中断在跑的图。"""
        self._running = False
        if self._graph is not None:
            self._graph.stop()

    def cleanup(self) -> None:
        """幂等释放模块资源。"""
        # HUD 观察者先停：它从采集缓存取帧，不该在录制器 drain 帧队列时还在读帧
        if self._hud is not None:
            self._hud.stop("cleanup")
            self._hud = None
        # 录制器先于图释放：停录制要 drain 帧队列，不应与导航图拆卸竞争
        if self._recorder is not None:
            self._recorder.stop("cleanup")
            self._recorder = None
        if self._graph is not None:
            self._graph.shutdown()
            self._graph = None
        self._stage_name = None

    # ---------- 结构化日志组归属（类上薄包装，与模块级函数同源，treasure 同形态） ----------

    def _tlog(self, msg: str, level: str = "INFO", fields: dict | None = None):
        _tlog(self, msg, level, fields)

    def _open_grp(self, title: str, kind: str) -> None:
        _open_grp(self, title, kind)

    def _end_grp(self, outcome: str | None = None) -> None:
        _end_grp(self, outcome)

    # ---------- 流程推进 ----------

    def _run_flow(self, index: int) -> None:
        """按 FLOW 推进；走完一轮回到循环体起点继续，直到收到停止信号。"""
        assert self._graph is not None
        round_no = 0
        while self._running:
            round_no += 1  # 场景分层用：录制 meta 要记"这一帧属于第几轮"
            # 组边界即场次边界（迁移裁定 2）：一"轮" = 一局完整对局（两个驾驶阶段+结算）
            _open_grp(self, f"[极速狂飙] 第 {round_no} 场开始", "session")
            while index < len(FLOW):
                if not self._running:
                    return
                stage, node, phase = FLOW[index]
                self._stage_name = stage
                if node is None:
                    if not self._drive(phase, round_no):
                        _tlog(self, f"[极速狂飙] 「{stage}」未正常结束，本轮中止", "WARNING")
                        return
                elif not self._graph.run(node, node):
                    _tlog(self, f"[极速狂飙] 步骤「{stage}」未到位，本轮中止", "WARNING")
                    return
                index += 1
            index = LOOP_START_INDEX

    def _drive(self, phase: int, round_no: int = 1) -> bool:
        """驾驶阶段（phase = 1/2）——驾驶控制方案的接口点。

        当前实现是**采集模式**：不操纵车辆，只等本阶段结束并按需录制演示数据
        （录制模式下同时起 HUD 读数观察线程，见 ``_begin_hud``）。
        驾驶控制接入后，本方法内部多一条"正常态交给模型"的分支，对调用方无感。

        契约（任何实现都必须满足）：
          - 进入时画面应在驾驶页（可能仍在起步动画，须自行等到就绪）
          - 退出时本阶段已结束、画面已切回 UI
          - 响应 ``self._running``，收到停止信号立即返回
          - 不自建、不销毁手柄设备（设备复位统一由外层 ``reset_device`` 处理）
          - 返回 False = 本阶段无法完成，调用方会中止本轮

        **就绪与结束分离**是本实现的关键：把"被调用"当成"已在驾驶中"，起步动画
        期间就开始动作，会在过场画面上误操作。故进入先等就绪、退出靠连续失配去抖。
        """
        assert self.ctx is not None
        assert self._graph is not None

        # 阶段组开在等就绪之前：就绪判定的每条日志都属于这场驾驶（卡片要完整）
        _open_grp(self, f"[极速狂飙] 驾驶阶段 {phase}"
                        f"（{'录制采集' if self._record_mode else '等待阶段结束'}）", "phase")
        try:
            if not self._wait_drive_ready(phase):
                return False

            recorder = self._begin_recording(phase, round_no) if self._record_mode else None
            # HUD 读数挂在录制会话上：对齐目标就是同会话的 frames.jsonl（见 _begin_hud）
            observer = self._begin_hud(recorder.out_dir, phase, round_no) if recorder is not None else None
            try:
                return self._drive_loop(phase, recorder, round_no)
            finally:
                if observer is not None:
                    observer.stop("phase_end" if self._running else "stopped")
                    self._hud = None
                if recorder is not None:
                    recorder.stop("phase_end" if self._running else "stopped")
                    self._recorder = None
        finally:
            # 收口在录制器/HUD 线程都停干净之后（它们的落盘尾行不进组，属跨切面）；
            # 停止信号打断的阶段归 incomplete，正常结束按组内 WARNING/ERROR 自动推导。
            _end_grp(self, "incomplete" if not self._running else None)

    def _wait_drive_ready(self, phase: int) -> bool:
        """等到驾驶页稳定出现；连续命中 ``DRIVE_READY_HITS`` 次才算就绪（去抖）。

        每次轮询都取一次帧（只为拿帧号，不落盘）：判定成立/超时的日志里带 ``frame=``，
        事后才能在录制帧里复查"判早了还是判晚了"。
        """
        assert self.ctx is not None
        assert self._graph is not None
        _tlog(self, f"[极速狂飙] 驾驶阶段 {phase}：等待进入驾驶页", "INFO")
        hits = 0
        deadline = time.monotonic() + DRIVE_READY_TIMEOUT_S
        fid, age_ms = 0, 0.0
        while self._running and time.monotonic() < deadline:
            _, fid, _, age_ms = self.ctx.capture.frame_with_age()
            if self._graph.run(DRIVE_STAGE_NODE, DRIVE_STAGE_NODE):
                hits += 1
                if hits >= DRIVE_READY_HITS:
                    _tlog(self,
                          f"[极速狂飙] 驾驶阶段 {phase}：已进入驾驶页"
                          f"（{_frame_note(fid, age_ms)}）", "INFO")
                    return True
            else:
                if hits:
                    # 只在"连击被中断"时记：这是起步动画/过场造成的瞬时丢失，
                    # 是校准就绪判据最直接的证据；一直是 0 的等待期不必逐条刷。
                    logger.log(
                        f"[极速狂飙] 驾驶阶段 {phase}：就绪连击中断"
                        f"（{_frame_note(fid, age_ms)}）", "DEBUG")
                hits = 0  # 去抖：中间一次失配就重新计数
            if not self.ctx.lifecycle.sleep(DRIVE_READY_POLL_S):
                return False
        if self._running:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：{DRIVE_READY_TIMEOUT_S:.0f}s 内未进入驾驶页"
                  f"（最后 {_frame_note(fid, age_ms)}）", "WARNING")
        return False

    def _drive_loop(self, phase: int, recorder: DriveRecorder | None,
                    round_no: int = 1) -> bool:
        """采集/控制主循环：按目标节拍取帧，锚点降频复查阶段是否已结束。

        节拍与锚点复查**故意解耦**——锚点识别走框架（单轮耗时不定），若每帧都塞一次
        识别，帧间隔会随它抖动；帧自带真实时间戳，离线按时间戳对齐即可。退出时报一次
        实际节拍（``_log_loop_pace``）：那是"采集能不能支撑训练"的第一手判据。

        **每 tick 都取帧，不论是否录制**：这一帧是门控判定的证据锚点（判定日志里带
        ``frame=``）。读取本身是 WGC 中心缓存的共享引用（帧号不变则不重算），未录制时
        的增量成本可忽略；反过来，缺了它，门控日志就只能记"判定发生了"，记不清
        "判定发生在哪一帧"，校准判据时无从复查。
        """
        assert self.ctx is not None
        assert self._graph is not None
        # 控制接管的前提：非录制模式（录制期人驾，程序不碰车）+ control_mode 开关
        # 或直行基线档（该档本身就是接管形态）。控制依赖感知产出观测，故正常控制
        # 隐含逐帧检测（不必另开 perception_mode）；直行档例外——感知/深度都不加载。
        straight = self._straight_mode and recorder is None
        control = (self._control_mode or straight) and recorder is None
        if recorder is not None:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：请开始手动驾驶（正在录制演示数据）", "INFO")
        elif straight:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：全程直行基线（感知/深度不加载，只踩油门）",
                  "INFO")
        elif control:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：控制环接管"
                  f"（V0/V1 由 decision.json 的 allow_all_moves 决定）", "INFO")
        else:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：等待阶段结束（未接管车辆）", "INFO")
        # 感知耗时记账按阶段清零（P50/P95 在循环出口随实际节拍一起报，见 _log_loop_pace）
        self._infer_times = []
        self._control_times = []
        self._control_last = None
        chain = self._build_control_chain() if (control and not straight) else None
        # 直行档 trace（stub chain 复用 _flush_control_trace 的落盘协议）；
        # 油门取 decision.json 的 throttle_raw 单一真源，不另抄数值。
        straight_chain = ({"trace": [], "t_start": time.time(), "bad_frames": {}}
                          if straight else None)
        straight_throttle = load_decision().planner.throttle_raw if straight else 0
        # 控制两档（智能/直行）同点起 HUD 读数：分数流是验收②「智能 vs 直行基线」
        # 对比的数据面。目录与 trace 同时刻命名，事后按 fid/ts_ns 对齐（见
        # _begin_hud）。失败降级为无读数，不阻断。
        drive_hud = None
        if control:
            src = chain if chain is not None else straight_chain
            hud_dir = _control_trace_root() / (
                "hud_" + time.strftime(
                    "%Y%m%d_%H%M%S", time.localtime(src["t_start"])) + f"_p{phase}")
            drive_hud = self._begin_hud(hud_dir, phase, round_no)

        deadline = time.monotonic() + DRIVE_TIMEOUT_S
        loop_start = time.monotonic()
        next_anchor = 0.0
        miss = 0
        frames = 0
        fid, ts_ns, age_ms = 0, 0, 0.0
        # 手柄租约覆盖整个控制阶段：进入取设备、退出归零归还（摇杆归中+松扳机）。
        # 循环内多处 return/raise 都经 with 收口，不留"退出后车还踩着油门"的尾巴。
        # 控制 trace 在 finally 一次性 flush（正常结束/停止/异常都落盘，守热路径不逐帧写）。
        try:
            with contextlib.ExitStack() as stack:
                gpad = stack.enter_context(self.ctx.gamepad.acquire()) if control else None
                while self._running and time.monotonic() < deadline:
                    t0 = time.monotonic()
                    # 取帧走 frame_with_age：录制要的是「帧到达采集回调的时刻」，不是本循环
                    # 读取它的时刻——两者差一个帧龄，直接进训练标签的时序。
                    frame, fid, ts_ns, age_ms = self.ctx.capture.frame_with_age()
                    if recorder is not None and frame is not None:
                        recorder.record_frame(frame, frame_id=fid, ts_ns=ts_ns, age_ms=age_ms)
                    result: PerceptionResult | None = None
                    if not straight and (self._perception_mode or control) \
                            and frame is not None:
                        perc = self._ensure_perception()
                        if perc is not None:
                            result = perc.detect(frame, frame_id=fid, ts_ns=ts_ns)
                            self._last_perception = result
                            self._infer_times.append(result.infer_ms)
                    if gpad is not None:
                        if straight:
                            self._straight_tick(gpad, fid, ts_ns,
                                                straight_chain["trace"],
                                                straight_throttle)
                        elif result is not None:
                            self._control_tick(
                                chain, gpad, frame, result, fid, ts_ns, age_ms, phase)
                    frames += 1

                    now = time.monotonic()
                    if now >= next_anchor:
                        next_anchor = now + DRIVE_ANCHOR_CHECK_S
                        if self._graph.run(DRIVE_STAGE_NODE, DRIVE_STAGE_NODE):
                            miss = 0
                        else:
                            miss += 1
                            # 每次失配都记（DEBUG）：连续的失配序列正是"过场动画被误判为结束"
                            # 与"真的离开了对局"的区别所在，只记最后一条就看不出这个区别。
                            logger.log(
                                f"[极速狂飙] 驾驶阶段 {phase}：锚点失配第 {miss} 次"
                                f"（{_frame_note(fid, age_ms)}）", "DEBUG")
                            if miss >= DRIVE_MISS_TOLERANCE:
                                _tlog(self,
                                      f"[极速狂飙] 驾驶阶段 {phase}：已离开对局"
                                      f"（{_frame_note(fid, age_ms)}）", "INFO")
                                self._log_loop_pace(phase, frames, loop_start)
                                return True

                    rest = DRIVE_TICK_S - (time.monotonic() - t0)
                    if rest > 0 and not self.ctx.lifecycle.sleep(rest):
                        break
        finally:
            if control and chain:
                self._flush_control_trace(chain, phase)
                self._stop_depth_observer(chain, phase)
            elif straight and straight_chain is not None:
                self._flush_control_trace(straight_chain, phase)
            if drive_hud is not None:
                drive_hud.stop("phase_end" if self._running else "stopped")
                self._hud = None
        self._log_loop_pace(phase, frames, loop_start)
        if not self._running:
            return False
        _tlog(self,
              f"[极速狂飙] 驾驶阶段 {phase} 未在 {DRIVE_TIMEOUT_S:.0f}s 内结束"
              f"（最后 {_frame_note(fid, age_ms)}）", "WARNING")
        return False

    # ---------- 控制链（实机闭环，planner 设计稿 §九 step 3）----------

    def _build_control_chain(self) -> dict:
        """按阶段新建一份干净世界：跟踪/聚合/决策/规划各一，配置开局读一次。

        阶段间不共享状态（行为稿 §九：阶段信息只进过渡窗判据，两阶段同一决策器），
        新建即天然清空——与 DecisionEngine.reset 的"冷启动"语义一致。
        """
        cfg = load_decision()
        return {"cfg": cfg, "tracker": Tracker(), "agg": CoinGroupAggregator(),
                "traffic_obs": TrafficObserver(cfg.traffic),
                "ego_road": _EgoRoadObserver(),
                # 深度几何观测器（架构裁决 2026-09-24：深度区域当几何主人；同日
                # 解耦：异步 worker，控制拍只 push+take，observe 成本移出热路径）。
                # 会话为 None（权重缺失/加载失败）时不起线程、take 恒 None——
                # road_offset 退纯模型积分，黄线层不再补位（退役为骨架）。
                # 生命周期=chain：这里 start，驾驶循环 finally 里 stop（见
                # _stop_depth_observer 的收口与健康报警）。
                "depth_geo": _started_async_depth(
                    DepthRoadObserver(self._ensure_depth_session(), load_calib()),
                    debug_dir=_control_trace_root() / ("depth_debug_"
                    + time.strftime("%Y%m%d_%H%M%S"))),
                    "engine": DecisionEngine(cfg), "planner": LateralPlanner(cfg.planner),
                "prev_ts": None, "disabled": False, "trace": [],
                "bad_frames": {"next_at": 0.0, "saved": 0, "dir": None},
                "t_start": time.time()}

    def _control_tick(self, chain: dict, gpad, frame, result: PerceptionResult,
                      fid: int, ts_ns: int, age_ms: float, phase: int) -> None:
        """一拍全链：感知→跟踪→聚合→边界→车流派生→决策→规划→下发。

        链上任一异常（如 frame_id 真乱序 fail-loud）按"停控保平安"处理：记一次
        WARNING、本阶段退出控制（转观测态），主循环继续跑阶段结束判定——绝不因
        控制故障把车 strand 在油门上，也绝不静默吞掉 fail-loud（日志留证）。
        """
        if chain["disabled"]:
            return
        t_c = time.perf_counter()
        try:
            prev = chain["prev_ts"]
            dt = DRIVE_TICK_S if prev is None or ts_ns <= prev else (ts_ns - prev) / 1e9
            chain["prev_ts"] = ts_ns
            obs = chain["tracker"].update(result, frame_age_ms=age_ms, stage=phase)
            obs = chain["agg"].update(obs)
            # 深度几何（几何主人，v4 2026-10-01：MoGe 逐帧焦距点云 3D 找边当
            # road_offset 供数源；黄线 2D 链路整体退役）。异步解耦协议同前：
            # 本帧交 worker 后台推理，take() 只消费过了 age 闸的最新结果——
            # 控制拍不再付 observe 成本。协议本身不抛，异常闸按观测件惯例
            # 保留（fail-safe）。
            dgeo = None
            dgeo_new = False
            try:
                # note=上一拍决策快照（state/杆/喂入路心/EMA/槽）：随帧进调试图
                # 的决策带，供人工逐帧核对"大脑当时拿了什么"。不可变小 dict，
                # 主线程每拍新建、不改旧对象，跨线程无竞争。
                snap = chain["ego_road"].last
                last = self._control_last or {}
                chain["depth_geo"].push(
                    frame, object_mask=_yolo_object_mask(result),
                    note={"fid": last.get("frame_id"), "state": last.get("state"),
                          "reason": last.get("reason"), "steer": last.get("steer"),
                          "elane": last.get("executed_lane"),
                          "xt": last.get("x_target"),
                          "ro": last.get("road_offset"), "src": snap.get("source"),
                          "ro_raw": snap.get("off_raw"),
                          "age": last.get("dgeo_age"), "new": last.get("dgeo_new")})
                dgeo, dgeo_new = chain["depth_geo"].take()
            except Exception as exc:  # noqa: BLE001 —— 观测件故障不碰主循环
                _tlog(self, f"[极速狂飙] 深度几何观测异常（{exc!r}）", "WARNING")
            planner: LateralPlanner = chain["planner"]
            tviews, tevents = chain["traffic_obs"].update(obs)
            # road_offset 供数源=深度几何读数（路心合成+保鲜槽在 _EgoRoadObserver
            # 内）；dgeo 弃权拍=None，退纯模型积分——一个 job 一个主人。
            # 提前于 engine.update：轨迹模式（阶段二）决策层同吃路心锚与 W
            # 兜底宽度；legacy 模式消费序不变（engine 不吃这两路）。
            road_offset = chain["ego_road"].update(dgeo, new=dgeo_new)
            out = chain["engine"].update(
                obs, dt, executed_lane=planner.state.executed_lane,
                traffic=(tviews, tevents),
                road_offset=road_offset,
                road_width=chain["ego_road"].width)
            # 供数门撤销（80e9a1c 曾 pair-only，13:29 局证伪）：单侧/slot 合成虽带
            # 半宽记忆噪声，但挡掉它们=制造供数黑视——死亡螺旋段 87/118 拍被挡，
            # 陈旧重基（planner ⑥b）与跳变门都吃"喂入"，黑视让两把安全锁同时失效。
            # 噪声交给 0.8 新息门+1.0s 陈旧重基消化；trace road_offset 列=实际喂入。
            cmd = planner.update(out, dt, current_fid=fid, road_offset=road_offset)
        except Exception as exc:  # noqa: BLE001 —— 控制故障降级为观测，不崩主循环
            chain["disabled"] = True
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：控制链异常，本阶段停控转观测"
                  f"（{exc!r}，{_frame_note(fid, age_ms)}）", "WARNING")
            return
        gpad.left_joystick(x_value=cmd.steer_x, y_value=0)
        gpad.right_trigger(value=cmd.throttle)
        gpad.update()
        self._control_times.append((time.perf_counter() - t_c) * 1000.0)
        # 坏帧取证采样（内部自带异常吞噬与自禁，绝不把驾驶链拖下水）
        chain["bad_frames"]["last_steer"] = cmd.steer_x
        if not obs.health.stage_transition:
            _maybe_save_bad_frame(chain, frame, dgeo, fid, phase,
                                  time.monotonic(), ts_ns)
        self._control_last = {
            "state": out.state.value, "reason": out.reason, "steer": cmd.steer_x,
            "frame_id": fid, "executed_lane": round(planner.state.executed_lane, 3),
            "x_target": out.x_target,
            "road_offset": None if road_offset is None else round(road_offset, 3),
            "dgeo_age": round(chain["depth_geo"].last_age_ms), "dgeo_new": dgeo_new}
        # 逐拍控制 trace（内存攒、阶段出口一次性 flush）：C1 定档 K_v 与 §七.1 复测的
        # 数据源。boundary 黄线列已随 2D 链路退役（v4 换装），路缘观测量看
        # dgeo_left/dgeo_right 两列。
        # 候选诊断三列：金币组数 + 最高分 + 该组横向偏移——直接回答"有没有币/差多少分/
        # 在几车道外"。评分器是纯函数、每拍已算，这里只多读一次结果，不改决策。
        best: tuple[float, float] | None = None
        for g in obs.coin_groups:
            s = chain["engine"].scorer.score(g, obs)
            if s is not None and (best is None or s.score > best[0]):
                best = (s.score, g.x_center)
        chain["trace"].append({
            "fid": fid, "ts_ns": ts_ns, "dt": round(dt, 4),
            "state": out.state.value, "reason": out.reason,
            "x_target": out.x_target, "target_id": out.target_id,
            "reanchor_lane": out.reanchor_lane,
            "steer_norm": round(planner.state.steer_norm, 4), "steer_x": cmd.steer_x,
            "executed_lane": round(planner.state.executed_lane, 4),
            "v_lat_est": round(planner.state.v_lat_est, 4),
            "n_groups": len(obs.coin_groups),
            "best_score": None if best is None else round(best[0], 1),
            "best_x": None if best is None else round(best[1], 2),
            # step 2.5 闭环列：实际喂入 planner 的路中心观测（None=本拍弃权/垃圾对）
            # + 修正后 executed/v
            "road_offset": None if road_offset is None else round(road_offset, 4),
            # YOLO×深度融合（2026-09-25）：本拍检测框数（物体掩码随帧入深度路径）
            "yolo_cars": len(result.cars),
            # road_offset 供数面（路心合成调试）：来源 / 归属读数
            "ro_source": chain["ego_road"].last.get("source"),
            "ro_off": None if chain["ego_road"].last.get("off") is None
            else round(chain["ego_road"].last["off"], 4),
            # 深度几何列（几何主人 v4）：在场侧数 + 两侧读数 + 时延 + 弃权原因
            "dgeo_sides": None if dgeo is None else dgeo.sides,
            "dgeo_left": None if dgeo is None or dgeo.left_edge_lane is None
            else round(dgeo.left_edge_lane, 3),
            "dgeo_right": None if dgeo is None or dgeo.right_edge_lane is None
            else round(dgeo.right_edge_lane, 3),
            # dgeo_ms=worker 侧 observe 耗时（解耦后不再是控制拍成本）；
            # dgeo_age_ms=消费时刻帧龄（age 闸的读数面，滞后与丢弃都在这列显形）；
            # dgeo_new=本拍是否新结果（False=驻留复用，驻留协议的离线审计列）
            "dgeo_ms": None if dgeo is None else round(dgeo.latency_ms, 1),
            "dgeo_age_ms": None if dgeo is None
            else round(chain["depth_geo"].last_age_ms, 1),
            "dgeo_new": dgeo is not None and dgeo_new,
            "dgeo_rejects": None if dgeo is None or not dgeo.rejects
            else ";".join(dgeo.rejects),
            # 车流观测两列（阶段 C 的 C4/C5 回放数据源）：在途车数 + 本拍 pass 的 d_min
            # + 街车最大横向速率读数（噪声主导，仅供观测——见 decision.json _sources）
            # + 最近车的横向距/行号（本车道前车追尾类碰撞的回放诊断面）
            "car_views": len(tviews),
            "car_vlat": None if not tviews else round(
                max(abs(v.v_lat) for v in tviews), 3),
            "near_dx": None if not tviews else round(
                min(abs(v.x_lane - planner.state.executed_lane) for v in tviews), 2),
            "near_cy": None if not tviews else min(v.cy for v in tviews),
            "passes": [round(e.d_min, 3) for e in tevents
                       if e.outcome == OUTCOME_PASS],
            "fresh": obs.health.frame_fresh, "geom": obs.health.geometry_valid,
            "presence": obs.health.target_presence})

    def _straight_tick(self, gpad, fid: int, ts_ns: int, rows: list[dict],
                       throttle_raw: int) -> None:
        """直行基线一拍：方向归零、油门恒踩（V0 语义：决策禁动恒 x_target=0）。

        感知与深度都不进本路径——基线要度量的正是"无驾驶智能"的地板收益
        （control-route 验收②的分母）。trace 只记时间轴与常量列：事后把 HUD
        分数事件按 fid/ts_ns 对齐到拍，金币率才有分母。
        """
        gpad.left_joystick(x_value=0, y_value=0)
        gpad.right_trigger(value=throttle_raw)
        gpad.update()
        rows.append({"fid": fid, "ts_ns": ts_ns, "state": "STRAIGHT",
                     "x_target": 0.0, "steer_x": 0, "throttle": throttle_raw})

    def _flush_control_trace(self, chain: dict, phase: int) -> None:
        """阶段出口一次性落控制 trace（C1/§七.1 标定数据源）。

        写运行数据目录（与 demos 同构，不污染仓库工作树）；空 trace 不落盘。
        落盘失败只记 WARNING——标定数据缺失不该中止对局收尾（与录制器同一姿态）。
        """
        rows = chain.get("trace") or []
        if not rows:
            return
        try:
            root = _control_trace_root()
            root.mkdir(parents=True, exist_ok=True)
            name = f"trace_{time.strftime('%Y%m%d_%H%M%S', time.localtime(chain['t_start']))}_p{phase}.jsonl"
            with open(root / name, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：控制 trace {len(rows)} 拍 → {name}", "INFO")
            bf = chain.get("bad_frames") or {}
            if bf.get("saved"):
                _tlog(self,
                      f"[极速狂飙] 驾驶阶段 {phase}：坏帧（双侧路缘皆不稳）采样 "
                      f"{bf['saved']} 张 → {bf['dir']}", "INFO")
        except Exception as exc:  # noqa: BLE001 —— trace 落盘失败不阻断对局收尾
            _tlog(self, f"[极速狂飙] 驾驶阶段 {phase}：控制 trace 落盘失败: {exc!r}", "WARNING")

    def _stop_depth_observer(self, chain: dict, phase: int) -> None:
        """停深度 worker + 阶段出口报一次健康——不达标报警的落点（判据移植 treasure）。

        回归只发生在尾部，故判据用比值与 p95、不用均值：stale 丢弃率 ≥5% 或
        age p95 超预算 75% → WARNING；push 了却整场零结果（worker 没产出，
        实机 0/330 那类症状）→ WARNING；其余 INFO 汇总一行。
        """
        dg = chain.get("depth_geo")
        if dg is None:
            return
        if not dg.stop():
            _tlog(self, f"[极速狂飙] 驾驶阶段 {phase}：深度 worker 2s 内未退出（daemon 兜底）",
                  "WARNING")
        h = dg.health()
        if not h["pushed"]:
            return  # session 缺失/未跑过一帧：无数据面可报（缺失已在会话加载处 WARNING）
        examined = h["applied"] + h["stale_drops"]
        ratio = (h["stale_drops"] / examined) if examined else 0.0
        line = (f"[极速狂飙] 驾驶阶段 {phase}：深度 worker 应用 {h['applied']}"
                f"/超龄丢弃 {h['stale_drops']}（{ratio:.0%}）/异常 {h['failures']}"
                f"/让锁跳帧 {h['busy_skips']}"
                f"/时效 p50 {h['age_p50']:.0f} p95 {h['age_p95']:.0f}ms"
                f"（预算 {h['max_age_ms']:.0f}ms）"
                f"/耗时 p50 {h['dur_p50']:.0f} p95 {h['dur_p95']:.0f}ms")
        bad = (not examined) or ratio >= 0.05 or h["age_p95"] > h["max_age_ms"] * 0.75
        _tlog(self, line, "WARNING" if bad else "INFO")

    def _ensure_perception(self) -> StreetPerception | None:
        """懒加载感知模型（实例跨阶段复用，一局只付一次模型加载）；失败则本轮禁用。

        失败不阻断驾驶流程（与录制器同一姿态：采集/观测件的故障不碰主循环）。
        """
        if self._perception is not None or self._perception_failed:
            return self._perception
        try:
            t0 = time.monotonic()
            self._perception = StreetPerception(str(PERCEPTION_MODEL_FILE))
            _tlog(self, f"[极速狂飙] 感知模型就绪（{time.monotonic() - t0:.1f}s）", "INFO")
        except Exception as exc:  # noqa: BLE001 —— 感知故障不阻断对局流程
            self._perception_failed = True
            _tlog(self, f"[极速狂飙] 感知初始化失败，本轮禁用感知: {exc!r}", "WARNING")
            return None
        return self._perception

    def _ensure_depth_session(self):
        """懒加载深度几何的 ORT 会话（模式同 _ensure_perception）。

        权重随包分发入库（MoGe q4f16，Apache-2.0）；缺失/加载失败时深度层
        禁用、road_offset 走纯模型积分（几何主人缺席=无供数，不回退 2D——
        黄线链路已随 v4 换装整体退役）。"""
        if self._depth_sess is not None or self._depth_failed:
            return self._depth_sess
        try:
            t0 = time.monotonic()
            self._depth_sess = load_session(DEPTH_MODEL_FILE)
            _tlog(self, f"[极速狂飙] 深度几何模型就绪（{time.monotonic() - t0:.1f}s）",
                  "INFO")
        except Exception as exc:  # noqa: BLE001 —— 深度故障不阻断对局流程
            self._depth_failed = True
            _tlog(self, f"[极速狂飙] 深度几何初始化失败，road_offset 退纯模型积分"
                        f"（权重: {DEPTH_MODEL_FILE}）: {exc!r}", "WARNING")
        return self._depth_sess

    def _log_loop_pace(self, phase: int, frames: int, loop_start: float) -> None:
        """报一次本次循环的实际节拍。

        这是"采集能不能支撑训练"的第一手判据：训练侧按 30Hz 的时间窗重采样，实际节拍
        掉到几 Hz 时录下的帧根本喂不进那条契约；而日志里原先只有帧数、没有速率，
        这种偏差看不出来。三个出口（离开对局 / 停止 / 超时）都要报。

        感知开启时同点报感知耗时的 P50/P95（验收判据 §二.3 的记账要求：
        单帧控制回路耗时在帧预算内，且有 P50/P95）。
        """
        if self._infer_times:
            srt = sorted(self._infer_times)
            n = len(srt)
            p50 = srt[n // 2]
            p95 = srt[min(n - 1, int(round(0.95 * (n - 1))))]
            _tlog(self,
                f"[极速狂飙] 驾驶阶段 {phase}：感知 n={n} "
                f"P50={p50:.1f}ms P95={p95:.1f}ms（帧预算 {DRIVE_TICK_S * 1000:.0f}ms）",
                "INFO")
        if self._control_times:
            srt = sorted(self._control_times)
            n = len(srt)
            p50 = srt[n // 2]
            p95 = srt[min(n - 1, int(round(0.95 * (n - 1))))]
            _tlog(self,
                f"[极速狂飙] 驾驶阶段 {phase}：控制链（跟踪+聚合+边界+决策+规划，不含感知） "
                f"n={n} P50={p50:.1f}ms P95={p95:.1f}ms（验收判据 ≤2ms/tick）",
                "INFO")
        elapsed = time.monotonic() - loop_start
        rate = frames / elapsed if elapsed > 0 else 0.0
        _tlog(self,
            f"[极速狂飙] 驾驶阶段 {phase}：循环结束，{frames} 轮 / {elapsed:.1f}s"
            f"（实际 {rate:.1f}Hz，目标 {DRIVE_TICK_HZ:.0f}Hz）", "INFO")

    def _begin_recording(self, phase: int, round_no: int) -> DriveRecorder | None:
        """建会话目录并启动录制器；失败返回 None（录制失败不该中止对局）。"""
        try:
            # manual=人工驾驶语料（录制模式必然人驾）：目录名一眼可分历史会话，
            # 阶段二参考线语料按 manual_ 前缀取（2026-10-03 维护者裁定）
            session = make_session_dir(_demos_root(), tag="manual")
            # 阶段后缀：一局两个驾驶阶段各成一个会话，便于按阶段筛数据
            session = session.with_name(f"{session.name}_p{phase}")
            rec = DriveRecorder(session, phase=phase, round_no=round_no)
            rec.start()
        except Exception as exc:  # noqa: BLE001 —— 采集失败不阻断流程
            _tlog(self, f"[极速狂飙] 录制器启动失败: {exc!r}", "WARNING")
            return None
        self._recorder = rec
        return rec

    def _begin_hud(self, out_dir: Path, phase: int, round_no: int) -> HudObserver | None:
        """在指定目录起 HUD 读数观察线程；失败返回 None（读数失败不该中止对局）。

        两个调用形态，取帧源同一（``frame_with_age``，fid/ts_ns 对齐链路不变）：
        录制模式传录制会话目录（对齐目标=同会话 ``frames.jsonl``）；直行基线档传
        control trace 根下的 ``hud_<时刻>_p<阶段>`` 目录（对齐目标=同时刻命名的
        ``trace_*.jsonl``——直行档没有录制帧，时间轴锚点就是 trace 的时间轴）。
        """
        assert self.ctx is not None
        try:
            obs = HudObserver(
                out_dir,
                frame_source=self.ctx.capture.frame_with_age,
                phase=phase,
                round_no=round_no,
            )
            obs.start()
        except Exception as exc:  # noqa: BLE001 —— 读数启动失败不阻断流程
            _tlog(self, f"[极速狂飙] HUD 读数启动失败: {exc!r}", "WARNING")
            return None
        self._hud = obs
        return obs


def _demos_root() -> Path:
    """演示数据根目录。

    落用户数据目录（与 treasure 的 ``data/treasure/`` 同构）：与安装目录解耦，
    更新不丢数据，也不污染仓库工作树。
    """
    return data_dir() / "speedrush" / "demos"


# 坏帧采样器（三局复盘 2026-09-22 悬案取证：sides==0 占 58% 是场景褪色还是检测参数，
# 唯一证据=真帧；录制与控制互斥是既有安全不变量，不动它——由控制回路自己按节流存帧，
# fid 与 trace jsonl 对账。热路径纪律：≥2s 一张、每阶段封顶，写失败即自禁不重试刷屏）
YOLO_MASK_MARGIN = 10             # 检测框外扩（px）：框缘深度过渡带不留在候选区
BAD_FRAME_MIN_INTERVAL_S = 2.0
BAD_FRAME_MAX_PER_PHASE = 40


def _yolo_object_mask(result: PerceptionResult) -> np.ndarray | None:
    """YOLO 检测框（车/金币/奖励，外扩 YOLO_MASK_MARGIN）→ 布尔掩码；
    无检测返回 None（深度路径零成本）。目标架构：物体身份由语义说，
    深度块在源头让位（穿车读墙假缘不再产生）。"""
    dets = result.cars + result.coins + result.bonuses
    if not dets:
        return None
    mask = np.zeros((720, 1280), bool)
    for d in dets:
        x0 = int(max(d.cx - d.w / 2 - YOLO_MASK_MARGIN, 0))
        x1 = int(min(d.cx + d.w / 2 + YOLO_MASK_MARGIN, 1279))
        y0 = int(max(d.cy - d.h / 2 - YOLO_MASK_MARGIN, 0))
        y1 = int(min(d.cy + d.h / 2 + YOLO_MASK_MARGIN, 719))
        mask[y0:y1 + 1, x0:x1 + 1] = True
    return mask


def _maybe_save_bad_frame(chain: dict, frame, dgeo, fid: int, phase: int,
                          now: float, ts_ns: int) -> None:
    """深度几何双侧皆弃权（sides==0）的帧按节流存 JPEG + index.jsonl 行。

    ``dgeo`` 是异步驻留读数（慢变量）：sides==0 = 最近一次在场结果双侧皆
    无穿越，取证语义=「引擎睁眼却找不到边」的真帧留证；dgeo None（超龄/
    无结果）不算——那是链路缺席，不是找边失败。"""
    if dgeo is None or dgeo.sides != 0:
        return
    s = chain["bad_frames"]
    if s["saved"] >= BAD_FRAME_MAX_PER_PHASE or now < s["next_at"]:
        return
    s["next_at"] = now + BAD_FRAME_MIN_INTERVAL_S
    try:
        if s["dir"] is None:
            s["dir"] = _control_trace_root() / (
                "badframes_"
                + time.strftime("%Y%m%d_%H%M%S", time.localtime(chain["t_start"]))
                + f"_p{phase}")
            s["dir"].mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(s["dir"] / f"fid_{fid}.jpg"),
                    cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        with open(s["dir"] / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "fid": fid, "ts_ns": ts_ns,
                "left_x": None if dgeo.left_x is None else round(dgeo.left_x, 1),
                "right_x": None if dgeo.right_x is None else round(dgeo.right_x, 1),
                "rejects": ";".join(dgeo.rejects) or None,
                "steer_x": s.get("last_steer", 0)}, ensure_ascii=False) + "\n")
        s["saved"] += 1
    except Exception:  # noqa: BLE001 —— 取证仪器失败绝不影响驾驶，且自禁
        s["saved"] = BAD_FRAME_MAX_PER_PHASE


class _EgoRoadObserver:
    """road_offset 推导（供数源=深度几何 v4 读数，2026-10-01 主人切换）。

    planner 契约：``road_offset`` = **自车相对路心的位置**（车道单位，右正）。
    深度几何读数的 left/right_edge_lane 是 3D 找边的两侧缘（车道单位，左负右
    正，原点=车），双侧间隙中心 = 路心，``off = −(左缘+右缘)/2``——与黄线
    v3.2 同式（供数源换成 3D 找边，消费语义不变）。

    v3 事故链的总缺口（2026-09-30 复核裁决）：v3 把输出改成了「目标的自车系
    坐标」（负=目标在左），与 planner 契约**互为相反数且中间无换算**——两侧
    各自有符号测试、端到端只断言过 0.0（0 是符号不变量），三轮补丁全部从
    这个缺口漏过。跨层极性契约测试（test_cross_layer_polarity_and_mirror）
    自此为门禁。

    - 门：``sides==2`` 且两侧读数在场（3D 找边契约：单侧下间隙归属几何
      不可判定——102200 局「贴死左墙」的肇因之一就是把单侧读数当完整
      栅格用）；
    - 单侧/缺失：0.4s 内最近有效对保鲜兜底（13:29 供数黑视教训：黑视比
      噪声致命），过期退纯模型积分；单侧安全包线是 P1 议题；
    - |off|>OFF_MAX 垃圾不喂（宁弃不喂假路心）。
    """

    SLOT_TTL_S = 0.4                 # 有效对保鲜预算（兜底单侧/缺失拍）
    OFF_MAX = 3.0                    # 路面半宽级（4 车道 ≈ ±2 道）+ 机动余量
    # 路宽 W 统计（阶段二 §一.3 兜底界）：找边对宽 (R−L) 的中位数——语义=
    # 碰撞边界到碰撞边界（RULES：黄线外台阶），车道单位。缓变量统计：
    # 双侧对在场率 ~50% 无妨，几十个样本即成形；±40% 门把找边锁错结构的
    # 污染对挡在统计外。**离线回放两轮证伪的深度前瞻几何线不在此处**——
    # 远场（z≥10）点云 X 被深度噪声涂抹（图像上几像素行高的条带），前瞻
    # 线不可测；参考线=路心锚（本类 off）+ 车头向轴 + W 兜底界。
    W_MIN_SAMPLES = 30               # 成形所需最少对宽数
    W_KEEP_SAMPLES = 200             # 滑动样本窗上限（路型缓变，旧样本滚出）
    W_GUARD_FRAC = 0.4               # 对宽超出 W±此比例 → 污染，不进统计

    def __init__(self) -> None:
        self._slot: dict[str, tuple] = {}     # 最近有效对缓存 (el, er, 时刻)
        self._win: deque = deque(maxlen=3)    # 近 3 对原始读数（中值滤波窗口，
                                              # 第七轮：ro 单对野值 ±0.8 道跳变
                                              # 直注规划层=抽风转向的供数面根因）
        self._w_samples: deque = deque(maxlen=self.W_KEEP_SAMPLES)
        self.last: dict = {}                  # 实机 debug 数据面（ro_* 列）

    def _fresh(self, now: float) -> tuple[float, float] | None:
        s = self._slot.get("pair")
        if s is None or now - s[2] > self.SLOT_TTL_S:
            return None
        return s[0], s[1]

    def update(self, bnd, now: float | None = None, *,
               new: bool = True) -> float | None:
        """``bnd``：DepthRoadReading（或鸭子同构：sides + 两侧 edge_lane）。

        ``new``：本拍读数是否新证据（异步驻留协议 take() 的 is_new）。中值窗、
        保鲜槽与 W 统计只认新证据（使用次数≠学习次数，同 take() 的纪律）——
        驻留复用拍走槽值；否则同一读数 age 闸内被逐拍重复计入窗，窗内独立
        读数退化为 1~2 个：真变化滞后 ~0.3s 后台阶跳变、单对野值凭重复入窗
        赢得中值（实机 10-04 两局取证：79 次 >0.3 道 ro 台阶 0 次在新读数拍）。"""
        if now is None:
            now = time.monotonic()
        ok = (bnd is not None and bnd.sides == 2
              and bnd.left_edge_lane is not None
              and bnd.right_edge_lane is not None)
        src = "pair"
        off_raw = None
        if ok and new:
            el, er = bnd.left_edge_lane, bnd.right_edge_lane
            off_raw = -((el + er) / 2.0)   # 本拍原始对（滤波前），debug 数据面
            if abs((el + er) / 2.0) > self.OFF_MAX:
                self.last = self._snapshot(now, src, None)
                return None                 # 垃圾对：不喂、不入槽、不入窗
            # 中值滤波（窗口未满时直喂最新对，前两拍行为与旧版逐值同形）：
            # 单对野值（一侧找边偶发锁错结构）被近 3 对中值杀掉，真漂移经
            # 2~3 对（约 0.5s）跟进——比 α/β 直注噪声便宜得多。
            self._win.append((el, er))
            if len(self._win) >= 3:
                el = sorted(w[0] for w in self._win)[1]
                er = sorted(w[1] for w in self._win)[1]
            self._slot["pair"] = (el, er, now)
            self._feed_width(el, er)
        else:
            fresh = self._fresh(now)
            if fresh is None:
                self.last = self._snapshot(now, "none", None)
                return None
            el, er = fresh
            src = "pair_slot"
        center = (el + er) / 2.0        # 路心（自车系）
        off = -center                   # 契约：自车相对路心（右正）
        if abs(off) > self.OFF_MAX:
            self.last = self._snapshot(now, src, None)
            return None                 # 垃圾信号不喂（宁弃不喂假路心）
        self.last = self._snapshot(now, src, off)
        return off

    def _feed_width(self, el: float, er: float) -> None:
        """中值滤波后的双侧对宽进 W 统计（垃圾对/单侧/保鲜拍不进——与供数门
        同一入口纪律）。成形前收全部对宽；成形后 ±40% 门。"""
        w = er - el
        formed = self.width
        if formed is not None and abs(w - formed) > self.W_GUARD_FRAC * formed:
            return
        self._w_samples.append(w)

    @property
    def width(self) -> float | None:
        """成形路宽 W（道，碰撞边界到碰撞边界）；样本不足 None。"""
        if len(self._w_samples) < self.W_MIN_SAMPLES:
            return None
        return float(np.median(self._w_samples))

    def _snapshot(self, now: float, src: str, off: float | None,
                  off_raw: float | None = None) -> dict:
        return {"source": src, "off": off, "off_raw": off_raw,
                "width": self.width}


def _started_async_depth(obs: DepthRoadObserver,
                         debug_dir: Path | None = None) -> AsyncDepthRoadObserver:
    """构造即启动的异步深度观测器（session 不可用时 start 自为 no-op，见其 docstring）。

    debug_dir 给定时，worker 按 DEBUG_INTERVAL_S 节流落实机调试图（三行堆叠：
    看了什么/算了什么/判了什么）。生命周期与 chain 同呼吸：这里 start，
    驾驶循环 finally 的 _stop_depth_observer 收口。"""
    a = AsyncDepthRoadObserver(obs, debug_dir=debug_dir)
    a.start()
    return a


def _control_trace_root() -> Path:
    """控制 trace 根目录（与 demos 同构，落用户数据目录、不污染仓库）。"""
    return data_dir() / "speedrush" / "control_traces"


def resolve_start_index(start_from: str | None) -> int:
    """把 GUI 断点的阶段名换算为 FLOW 下标；缺省或未知值一律回退 0。"""
    if not start_from:
        return 0
    for i, (stage, _, _) in enumerate(FLOW):
        if stage == start_from:
            return i
    return 0