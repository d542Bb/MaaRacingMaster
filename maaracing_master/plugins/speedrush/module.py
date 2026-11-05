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
事后校验，所以动作节点必须是零路由叶子（见真源生成器复算的提交 0603d8c 注释），
否则框架会顺边跑下去而失控。

驾驶阶段是留给控制方案的接口，契约见 ``_drive`` 的 docstring。
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, cast

import cv2
import numpy as np

from maaracing_master.core import xinput
from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.dashboard_contract import DASHBOARD_CONTRACT_VERSION
from maaracing_master.core.logger import logger
from maaracing_master.core.nav_graph import NavGraph
from maaracing_master.core.navkit import Command, StepMachine, Verdict
from maaracing_master.core.paths import debug_dir
from maaracing_master.core.trace_sink import get_trace_sink
from maaracing_master.plugins.speedrush import (
    DEPTH_MODEL_REL, IMAGE_DIR, PERCEPTION_MODEL_REL, PIPELINE_DIR)
from maaracing_master.plugins.speedrush import dashboard_store, model_assets
from maaracing_master.plugins.speedrush.coin_group import CoinGroupAggregator
from maaracing_master.plugins.speedrush.config import load_decision
from maaracing_master.plugins.speedrush.corridor import (
    Corridor,
    build_corridor, far_hint_lane, far_veto_lanes, far_veto_tick)
from maaracing_master.plugins.speedrush.decision import DecisionEngine
from maaracing_master.plugins.speedrush.debugview import (
    DEBUG_INTERVAL_S, DebugPairWriter, render_hud)
from maaracing_master.plugins.speedrush.depth_geo import (
    AsyncDepthRoadObserver, DepthRoadObserver, DrivableGrid, grid_center_lane,
    blocked_inner_reading, load_session, region_reading,
    last_slow_forward)
from maaracing_master.plugins.speedrush.wall_track import (
    MEASURED, OCCLUDED, OCCL_MIN_CONF, WallTrack)
from maaracing_master.plugins.speedrush.hud import (
    HudObserver, load_hud_regions, read_hud_scores)
from maaracing_master.plugins.speedrush.macro import (
    MacroError, MacroPlayer, MacroRecorder, list_groups, load_phase, macro_root,
    resolve_group, valid_group_name)
from maaracing_master.plugins.speedrush.lane_obs import (
    LaneObsConfig, LaneObsWorker, OFF_MAX as LANE_OFF_MAX)
from maaracing_master.plugins.speedrush.perception import PerceptionResult, StreetPerception
from maaracing_master.plugins.speedrush.planner import LateralPlanner
from maaracing_master.plugins.speedrush.route import (
    ATTR_SHIFT_HELD, PositionLineage)
from maaracing_master.plugins.speedrush.motion_plan import (
    EgoState, PlanGate, plan_motion)
from maaracing_master.plugins.speedrush.recorder import DriveRecorder, make_session_dir
from maaracing_master.plugins.speedrush.evidence import archive_session_evidence
from maaracing_master.plugins.speedrush.traffic import OUTCOME_PASS, TrafficObserver
from maaracing_master.plugins.speedrush.tracking import DecisionState, Tracker
from maaracing_master.plugins.speedrush.world_model import KIND_CAR, load_calib
from maaracing_master.plugins.speedrush.view_guard import ViewGuard

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

# 进商店横幅锚点节点（图侧纯锚点）。阶段 1 结束前画面中央会弹「进入策略商店 /
# 即将开始阶段2」横幅（RULES.md §3.6，弹出时机即进商店前），比齿轮锚点消失早到
# 约 1~2 秒。主循环在锚点复查拍顺带问它一次：命中即判本阶段结束、提前退出——省掉
# 齿轮消失后「等满 0.5s 复查 × 2 次失配」这段滞后里喂进感知/决策链与录制器的过场帧。
# 阈值 0.7 为截图自匹配（1.0）起的保守起点，**待真机驾驶帧校准**（口径同齿轮锚点 0.5）。
STORE_BANNER_NODE = "speedrush.商店横幅锚点"

# 驾驶阶段等待上限。单局（含两阶段）实测一般 3 分钟、耗满可达 6~7 分钟
# （RULES.md §4.7），取 10 分钟留余量。
DRIVE_TIMEOUT_S = 600.0
# 连续多少次识别不到驾驶页锚点才判定「已离开对局」。取 2 是给动态场景下的
# 偶发失配留一次容错；阈值余量与两端的实测分离度见 DRIVE_STAGE_NODE 的说明。
DRIVE_MISS_TOLERANCE = 2

# ---------- 步级重试（大阶段之间的一次 UI 点击） ----------
# 现状：某个 UI 步 run(node, node) 的 True 只证明「按钮被识别并点了一次」，不含落地页
# 后置确认；点了没生效只能靠下一步识别不到、白等框架默认 20s 后中止本轮。这里给每个
# 点击步套 core StepMachine：点击 → 按时间窗轮询落地页确认锚点 → 未落地则有限重发同一
# 点击（动作节点自带识别闸门，按钮已消失就空跑、不误点）→ 耗尽按现状终止本轮、停止即退。
# 机器唯一持有计数与时间窗，speedrush 只供给「动作 + 每拍 verdict + 驱动时机」。
# 剂量（max_retries / window_s / poll_s / 分步覆盖 by_stage）在数据面 decision.json 的
# step_retry 段——_run_step 每步经 load_decision().step_retry.dose(stage) 取，可按阶段调。

# 每个点击步的落地页确认锚点：由 RULES.md §2 操作路径 + §3 各界面要素逐格推得；确认节点
# 复用落地页标记的模板（节点名带 __confirm，见 build_truth CONFIRMS 说明）。「确认上阵 →
# 驾驶页」直接复用现成的驾驶页锚点。key = FLOW 的阶段名；驾驶阶段（node None）不在此表。
STEP_CONFIRM: dict[str, str] = {
    "进入比赛页": "speedrush.__confirm.比赛页",
    "进入娱乐玩法": "speedrush.__confirm.娱乐玩法页",
    "进入极速狂飙": "speedrush.__confirm.活动页",
    "开始挑战": "speedrush.__confirm.回合开始页",
    "寻找对手": "speedrush.__confirm.奖励弹窗",
    "关闭奖励弹窗": "speedrush.__confirm.策略商店页",
    "确认上阵": DRIVE_STAGE_NODE,
    "关闭奖励弹窗2": "speedrush.__confirm.策略商店页",
    "确认上阵2": DRIVE_STAGE_NODE,
    "回合结果继续": "speedrush.__confirm.回合开始页",
    "放弃本轮": "speedrush.__confirm.放弃确认框",
    "继续放弃": "speedrush.__confirm.段位分结算页",
    "结算继续": "speedrush.__confirm.活动页",
}

# ---------- 驾驶主循环节拍 ----------

# 主循环目标频率。与后续模型输入契约的采样率一致（"K 帧堆叠"的时间跨度按此定义）。
# 20Hz 定案（2026-10-06，运动学正推）：贴身目标容差 80px（=车道分离尺度 320px×¼）
# 对相对横向速度 p50 788px/s 给出数据年龄预算 ~101ms；τ 转向死区 130ms（n=51）
# 要求 T ≤ τ/2——两判据交汇 15~20Hz，30Hz 只多买 8ms 余量却多付感知锁占空比，
# 10Hz 余量仅 14% 撑不住 p95 抖动。与帧源 20fps 一致互为佐证（非匹配关系）。
# 挤出的预算（锁占空比 ~8%、年龄余量 ~40ms）留作新鲜度与低性能设备冗余，
# 不回填别的频率。
DRIVE_TICK_HZ = 20.0
DRIVE_TICK_S = 1.0 / DRIVE_TICK_HZ

# —— 路心读数"参考系跳变"标注（2026-10-27，仅记录，不参与控制）——
# 一次快横移与一次参考系换档在单帧上长得一样（都是路心读数一步跳大），
# 但离线辨识车模、算观测真值时必须区分：横移期的换档会把相邻读数的差
# 污染成 6 道/s 级的假速度（223955 局 s3 25.4s：0.17s 跳 1.07 道）。
# 判据：|Δ路心| > 物理可动位移 + 单次读数噪声地板。3.2 道/s 是录制里
# 真实横移的上沿（人工满杆约 2.4~3.0）；0.25 道是找边/对宽抖动的地板。
HOP_V_MAX_LANE_S = 3.2
HOP_NOISE_LANE = 0.25
HOP_BLACKOUT_S = 1.0


def classify_ro_hop(t0: float, ro0: float, src0: str | None,
                    t1: float, ro1: float, src1: str | None) -> dict:
    """标注相邻两次新鲜路心读数之间的变化性质（0=运动 / 1=换档 / 2=黑视）。

    返回 ``{gap, d, v, hop, src_chg}``：d=读数差（道，右正=车右移）、v=隐含
    横速（道/s，右正；黑视期 None）、src_chg=来源是否变化（换源但位移在
    包络内仍算运动——来源切换本身不是跳变证据，只是上下文）。"""
    gap = max(t1 - t0, 1e-3)
    d = ro1 - ro0
    if gap > HOP_BLACKOUT_S:
        return {"gap": gap, "d": d, "v": None, "hop": 2,
                "src_chg": int(src0 != src1)}
    v = d / gap
    hop = 1 if abs(d) > HOP_V_MAX_LANE_S * gap + HOP_NOISE_LANE else 0
    return {"gap": gap, "d": d, "v": v, "hop": hop,
            "src_chg": int(src0 != src1)}


def _note_c_event(chain: dict, kind: str, fid: int, ts_ns: int,
                  **detail) -> None:
    """记一次 C 相关事件（阶段收尾据此冻结证据）。首次事件保留现场。"""
    ev = chain.get("c_evidence")
    if ev is None:
        ev = {"first": None, "kinds": {}}
        chain["c_evidence"] = ev
    ev["kinds"][kind] = ev["kinds"].get(kind, 0) + 1
    if ev["first"] is None:
        ev["first"] = {"kind": kind, "fid": fid, "ts_ns": ts_ns, **detail}


def _c_events(eng, position) -> list[tuple[str, dict]]:
    """本拍是否出现 C 相关事件（只读；不含 blkfin-only 的普通拦截拍）。

    冻结触发面：路线相位/定案/参考道分歧（真正的 C 效果）、选择影子分歧或
    同输入不一致（下游差异/信任根故障）、换档读数上的完成（C 的目标样本）。
    """
    out: list[tuple[str, dict]] = []
    rv, sv = eng.route_verdict, eng.route_shadow_verdict
    if rv is not None and sv is not None and (
            (rv.phase, rv.verdict, rv.lane_ref)
            != (sv.phase, sv.verdict, sv.lane_ref)):
        out.append(("route_div", {"prod": rv.verdict, "shadow": sv.verdict}))
    sst = eng.selection_shadow_trace
    if sst is not None:
        out.append(("sel_bug" if sst.get("bug") else "sel_div",
                    {"layer": sst.get("l"), "req": sst.get("req")}))
    if rv is not None and rv.verdict == "finish" and position is not None \
            and getattr(position, "kind", None) == ATTR_SHIFT_HELD:
        out.append(("c_finish", {"target": rv.lane_ref}))
    return out


# 驾驶页锚点复查间隔。锚点识别走框架（post_task + 等待，单轮毫秒级），不必每帧跑；
# 采集/控制节拍与它解耦——帧各自带真实时间戳，离线按时间戳对齐，不受此频率污染。
DRIVE_ANCHOR_CHECK_S = 0.5

# 进入驾驶的就绪判据：连续命中驾驶页锚点多少次才认为"已在对局中"。
# **待收紧**：起步动画的真实特征尚未实测，本判据只保证"驾驶页已稳定出现"，
# 可能早于"车辆可操控"。取得动画样本后须按其特征加判据。
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
    _sync_log_group(self)


def _end_grp(self, outcome: str | None = None) -> None:
    """收尾当前组；outcome 缺省时按组内 ERROR/WARNING 自动推导终态。"""
    g = getattr(self, "_log_grp", None)
    if g is None:
        return
    self._log_grp = None
    g.end(outcome)
    _sync_log_group(self)


def _sync_log_group(self) -> None:
    """把当前组号同步给 nav 图动作桥（派发时刻快照；worker 不读线程局部当前组）。"""
    gid = getattr(getattr(self, "_log_grp", None), "id", None)
    graph = getattr(self, "_graph", None)
    sync = getattr(graph, "set_log_group", None)
    if sync is not None:
        sync(gid)


def _build_config_ui(models_ready: bool) -> dict:
    """配置表单：深度权重缺失时给选项加下载徽章，并在字段下方挂安装提示。"""
    depth_option: dict = {"value": "depth", "label": "深度几何"}
    drive_field: dict = {
        "key": "drive_mode", "type": "segmented", "label": "驾驶模式",
        "options": [
            {"value": "straight", "label": "全程直行"},
            {"value": "macro", "label": "宏录制"},
            depth_option,
        ],
    }
    if not models_ready:
        depth_option["badge"] = {"text": "↓", "level": "warn"}
        drive_field["notice"] = {
            "text": "深度模型未安装：安装包不含权重，下载后解压到 data/speedrush/models/",
            "level": "warn",
            "actions": [
                {"label": "下载模型包", "action": "download_release"},
                {"label": "打开数据目录", "action": "open_user_data_folder",
                 "subpath": "data/speedrush/models"},
            ],
        }
    return {
        "groups": [{
            "fields": [
                drive_field,
                {
                    "key": "macro_group", "type": "select", "label": "回放宏",
                    "description": "关闭「录制」时生效；缺阶段文件的那一段会降级为全程直行",
                    "choices": "macro_group",
                    "visible_if": {"key": "drive_mode", "op": "equals", "value": "macro"},
                },
                {
                    "key": "record_mode", "type": "switch", "label": "录制演示数据",
                    "description": "开启后本段只采集画面与手柄数据、不操纵车辆，用于训练语料"
                                   "（存到调试目录）。请自行驾驶；建议点击方式保持「前台鼠标」，"
                                   "避免虚拟手柄与你的手柄冲突。",
                    "visible_if": {"key": "drive_mode", "op": "not_equals", "value": "straight"},
                    "variants": [{
                        "when": {"key": "drive_mode", "op": "equals", "value": "macro"},
                        "label": "录制宏",
                        "description": "开启后本段只采集你的手柄/键盘操作，录成可回放的宏"
                                       "（存到用户数据目录）；关闭录制则回放所选宏。"
                                       "键盘 A/D 或 ←/→ 满幅转向，W/↑ 右扳机，S/↓ 左扳机。",
                    }],
                },
            ],
        }],
    }


class SpeedRushModule(ActivityModule):
    """极速狂飙：地铁跑酷式车道选择玩法。"""

    ID = "speedrush"
    NAME = "极速狂飙"
    # GUI 断点选择用；与 FLOW 阶段名同源，不另抄一份清单
    STAGE_ORDER: list[str] = [stage for stage, _, _ in FLOW]
    # 驾驶阶段全程占用手柄（油门常踩 + 横向连续转向）
    REQUIRES_GAMEPAD_EXCLUSIVE = True
    REQUIRES: frozenset[str] = frozenset({"capture", "gamepad"})
    # 深度权重是可选模型包（发布包默认不带）：模块级资产门禁不再要求它们，
    # 启动时按驾驶模式校验；直行/宏录制不依赖任何权重。
    REQUIRED_ASSETS: tuple[str, ...] = ()
    # 模块封面（相对本插件 resources/）：只声明，定位与 URL 解析在 core/covers.py
    COVER = "cover.jpg"
    # 帧证据走本模块的取证目录（raw + 双联图），统一调试流只记段边界（契约 I7）
    DEBUG_SAVE = "self"
    # 回合战绩看板：读本地战绩库（dashboard_store），类方法、不建实例。
    DASHBOARD_CONTRACT = 1
    # 录制开关的初值（False = 不录制，驾驶阶段只等本阶段结束）
    DEFAULT_RECORD_MODE = False
    # 感知开关的初值（True = 驾驶阶段逐帧跑 YOLO 检测并记账；不操纵车辆，
    # 与录制可并存。控制环未接线前，它是"感知在场"的验证形态）
    DEFAULT_PERCEPTION_MODE = False
    # 控制开关的初值（True = 驾驶阶段跑全链并接管手柄）。默认 False：
    # 接线首启不接管车辆，显式开才动（实机安全边界）。
    DEFAULT_CONTROL_MODE = False
    # 驾驶模式（唯一档位键）：depth=正常深度几何驾驶 | straight=全程直行基线
    # （接管手柄后方向归零、油门恒踩，感知与深度都不加载，control-route 验收②
    # 的分母）| macro=宏录制/回放（开录制采玩家操作到 data/，关录制回放所选宏）。
    DRIVE_MODES = ("depth", "straight", "macro")
    DRIVE_LABELS = {"depth": "深度几何", "straight": "全程直行", "macro": "宏录制"}
    # 默认直行：发布包默认不带深度权重，首启不能让默认档直接撞缺模型。
    # 装好模型包的用户可自行切到深度；旧存档里的显式选择不受影响。
    DEFAULT_DRIVE_MODE = "straight"
    # 要回放的宏组（空=最新一组）；只在宏模式且未开录制时生效
    DEFAULT_MACRO_GROUP = ""

    # 性能卡领域项阈值：拍率降级档位
    PERF_TICK_WARN_HZ = 15.0
    PERF_TICK_ERROR_HZ = 10.0

    # 性能面板声明（契约见 core/perf_contract.py）：模块只声明「报哪几行」与
    # 判级阈值，等级由 core 按阈值算——模块不再自定 ok/warn/error。
    # 拍率口径：1000 / 最近 240 拍间隔中位数（约 12–20 秒窗，随拍率变化），
    # 是「典型拍间隔的倒数」，不是平均拍率；窗口由本模块自定，core 不感知。
    PERF_CONTRACT = 1
    PERF_ROWS = (
        dict(key="tick_rate", label="驾驶拍率", kind="rate",
             stat="p50_gap_inverse", category="player", precision=1,
             warn_at=PERF_TICK_WARN_HZ, error_at=PERF_TICK_ERROR_HZ,
             worse_when="below",
             hint="反应变慢：关掉调试落盘，或降低游戏画质"),
        dict(key="decision", label="决策实况", kind="state", category="player"),
    )
    # 线程中文别名（线程本身由 core 嗅探，模块只贴标签）。
    # 深度链的找边/栅格是每帧新建即退的临时线程，存活几毫秒，不进清单。
    THREAD_LABELS = {
        "speedrush-depth-worker": "深度链",
        "speedrush-depth-dbg": "深度调试",
        "speedrush-hud-sample": "HUD 采样",
        "speedrush-hud-write": "HUD 落盘",
        "speedrush-debug-io": "调试落盘",
        "lane-obs": "车道观测",
        "speedrush-model-preload": "模型预载",
        "speedrush-rec-frames": "录制帧",
        "speedrush-rec-pad": "录制手柄",
        "speedrush-macro-rec": "宏录制",
        "speedrush-macro-play": "宏回放",
    }
    # 配置面声明（GUI 配置项的键与初值；也是 profile 回填的白名单——不加进这里就不会被保存）
    DEFAULT_MODULE_CONFIG: dict = {
        "record_mode": DEFAULT_RECORD_MODE,
        "perception_mode": DEFAULT_PERCEPTION_MODE,
        "control_mode": DEFAULT_CONTROL_MODE,
        "drive_mode": DEFAULT_DRIVE_MODE,
        "macro_group": DEFAULT_MACRO_GROUP,
    }
    # 配置表单（契约见 core/config_ui_contract.py）：默认值/动态候选分别来自
    # DEFAULT_MODULE_CONFIG 与 config_choices()，这里只描述「怎么呈现」。
    CONFIG_UI_CONTRACT = 1
    CONFIG_UI = _build_config_ui(not model_assets.missing())

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

    @classmethod
    def config_choices(cls) -> dict:
        """宏组下拉的可选值（空值=最新一组，排在最前）。不创建实例。"""
        return {"macro_group": [{"value": "", "label": "最新录制的宏"}] + list_groups()}

    @classmethod
    def config_summary(cls, config: dict) -> list[str]:
        """任务卡摘要：录制开关只在非直行档有意义；宏档未录制时补一行回放组。"""
        mode_raw = config.get("drive_mode")
        mode = mode_raw if isinstance(mode_raw, str) else ""
        mode_text = cls.DRIVE_LABELS.get(mode, "深度几何")
        if mode == "depth" and model_assets.missing():
            mode_text += "（模型未安装）"
        out: list[str] = []
        if mode != "straight":
            out.append("录制: " + ("开" if config.get("record_mode") else "关"))
        out.append("驾驶模式: " + mode_text)
        if mode == "macro" and not config.get("record_mode"):
            out.append("宏组: " + str(config.get("macro_group") or "最新录制的宏"))
        return out

    def config_status(self) -> dict | None:
        """面板状态行：宏录制/回放、演示录制进度；无内容返回 None。"""
        mrec = self._macro_rec
        if mrec is not None and mrec.running:
            samples = int(mrec.stats["samples"])
            return {"text": f"宏录制中：已写 {samples} 条；目录 {macro_root()}",
                    "level": "ok" if samples > 0 else ""}
        mpl = self._macro_player
        if mpl is not None:
            m = mpl.stats
            total = int(m.get("total") or 0)
            sent = int(m.get("sent") or 0)
            pct = (sent * 100 // total) if total else 0
            states = {"waiting": "等待计时对齐", "playing": "回放中",
                      "finished": "已播完", "stopped": "已停止"}
            state_text = str(m.get("state"))
            text = (f"宏回放：{states.get(state_text, state_text)}"
                    f" · {sent}/{total}（{pct}%）")
            offset = m.get("align_offset_ms")
            if offset:
                text += f"；对齐偏移 {round(offset)}ms"
            return {"text": text, "level": "ok"}
        rec = self._recorder
        if rec is not None and rec.running:
            st = rec.stats
            return {"text": f"录制中：已写 {st['frames_written']} 帧 · 丢帧 "
                            f"{st['frames_dropped']} · 手柄样本 {st['pad_samples']}；"
                            f"目录 {_record_root()}", "level": "ok"}
        if self._running:
            if self._record_mode:
                return {"text": "运行中：录制已开启，进入驾驶阶段后开始写入", "level": ""}
            return {"text": "运行中：本次未开启录制（开关改动在下次「开始」时生效）",
                    "level": "warn"}
        return None

    @classmethod
    def read_dashboard(cls) -> dict:
        """回合战绩看板：只读本地库，不创建模块实例。

        列表行用短模式名（深度/直行/人工/宏）；胜场按终盘比分比较。读库失败
        返回 warn 态兜底，不把异常抛给 sidecar 的轮询。
        """
        labels = {"depth": "深度", "straight": "直行", "manual": "人工",
                  "macro": "宏", "control": "自动", "unknown": "—"}
        try:
            return dashboard_store.read_dashboard_payload(labels)
        except Exception as exc:  # noqa: BLE001 —— 轮询入口不得打崩数据页
            logger.log(f"[极速狂飙] 读取回合战绩失败: {exc!r}", "DEBUG")
            return {
                "version": DASHBOARD_CONTRACT_VERSION,
                "title": "回合战绩",
                "badge": {"text": "读取失败", "tone": "warn"},
                "blocks": [{"type": "text", "text": "稍后自动重试。"}],
            }

    @classmethod
    def normalize_config(cls, config: dict) -> dict:
        """旧键折算 + 档位不变量：直行档接管车辆，没有录制面。

        旧存档的直行/宏两个布尔键按「宏优先」折算成单键（与旧互斥收口同序）；
        直行档残留的录制开关一律关掉——界面上该开关在直行档不显示，留着会
        让直行档退化成人工录制。
        """
        if not isinstance(config, dict):
            return config
        out = dict(config)
        if "drive_mode" not in out and ("straight_mode" in out or "macro_mode" in out):
            if bool(out.get("macro_mode")):
                out["drive_mode"] = "macro"
            elif bool(out.get("straight_mode")):
                out["drive_mode"] = "straight"
            else:
                out["drive_mode"] = "depth"
        out.pop("straight_mode", None)
        out.pop("macro_mode", None)
        if out.get("drive_mode") == "straight":
            out["record_mode"] = False
        return out

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
        # 驾驶模式：depth 正常全链 / straight 直行基线 / macro 宏录制回放
        self._drive_mode = self.DEFAULT_DRIVE_MODE
        self._macro_group = self.DEFAULT_MACRO_GROUP
        self._macro_rec: MacroRecorder | None = None
        self._macro_player: MacroPlayer | None = None
        # 人工驾驶期标记（紧急停止作用域）：录制阶段按键是正常操作，不能当急停
        self._manual_drive = False
        # road_offset 证据源=深度几何区读数（v4 几何主人，供数门优先级链见
        # _EgoRoadObserver）；深度几何的 ORT 会话：实例跨阶段复用（模型加载
        # 秒级，一局只付一次）；加载失败置 _depth_failed 后不再重试（降级为
        # 纯模型积分，不碰主循环）。
        self._depth_sess = None
        self._depth_failed = False
        # 模型预加载（2026-10-23）：UI 阶段空档后台加载，不让模型加载吃开局
        # 倒计时。每模型一把锁——预加载线程与驾驶线程并发进入时只构造一次
        # （防双份模型占内存/显存）；预加载失败不置 failed，懒加载仍可重试。
        self._perception_lock = threading.Lock()
        self._depth_lock = threading.Lock()
        self._preload_started = False
        self._preload_thread: threading.Thread | None = None
        self._preload_done = threading.Event()
        self._preload_stop = threading.Event()
        # 视角守卫：同场两阶段沿用一次确认；每场开始重新确认（游戏重置
        # 进度会把机位打回追尾视角，见 _run_flow 的场边界）
        self._view_ok = False
        # 控制回路耗时记账（验收判据 §二.3 的 P50/P95 数据源），按阶段清零
        self._infer_times: list[float] = []
        self._control_times: list[float] = []
        self._last_perception: PerceptionResult | None = None
        # 控制实况（GUI 展示：最近一拍的决策状态与下发杆值；控制关闭时为 None）
        self._control_last: dict | None = None
        # 玩法流水汇（进程级注册，随日志会话自动开合）：落点
        # logs/<会话>/trace/speedrush.jsonl，落盘受「日志记录」开关管辖
        self._trace = get_trace_sink("speedrush")
        # 性能卡领域项（资源面由 core 采样器供数）：驾驶环拍间隔滑窗 + 事件节流
        self._tick_gaps: deque[float] = deque(maxlen=240)

    @property
    def current_stage(self) -> str | None:
        return self._stage_name

    def _resolve_drive_mode(self) -> str:
        """本次驾驶模式（唯一派生入口）。"""
        return self._drive_mode

    def manual_input_active(self) -> bool:
        """录制阶段人工驾驶：紧急停止的按键检测暂停（急停只对程序操作期生效）。"""
        return bool(self._manual_drive)

    # ---------- 模块配置（GUI 读写；未定义时 RPC 层兜底为占位 dict） ----------

    def get_module_config(self) -> dict:
        """读配置。运行中由 sidecar 路由到本实例，故 ``_state`` 是实况而非快照。"""
        rec = self._recorder
        stats = rec.stats if rec is not None else None
        hud = self._hud
        hud_stats = hud.stats if hud is not None else None
        mrec = self._macro_rec
        mpl = self._macro_player
        lp = self._last_perception
        return {
            "record_mode": bool(self._record_mode),
            "perception_mode": bool(self._perception_mode),
            "control_mode": bool(self._control_mode),
            "drive_mode": self._drive_mode,
            "macro_group": self._macro_group,
            "_state": {
                "recording": bool(rec is not None and rec.running),
                "frames": int(stats["frames_written"]) if stats else 0,
                "frames_dropped": int(stats["frames_dropped"]) if stats else 0,
                "pad_samples": int(stats["pad_samples"]) if stats else 0,
                # HUD 读数（与录制器同一时机启停；行数即"这个阶段的读数有没有产出"）
                "hud_recording": bool(hud is not None and hud.running),
                "hud_rows": int(hud_stats["rows_written"]) if hud_stats else 0,
                "record_dir": str(_record_root()),
                # 宏模式实况：录制条数 / 回放进度（非宏模式为 None/0）
                "macro_recording": bool(mrec is not None and mrec.running),
                "macro_samples": int(mrec.stats["samples"]) if mrec is not None else 0,
                "macro": mpl.stats if mpl is not None else None,
                "macro_dir": str(macro_root()),
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
        if isinstance(config, dict):
            config = type(self).normalize_config(config)
        if isinstance(config, dict) and "record_mode" in config:
            self._record_mode = bool(config["record_mode"])
        if isinstance(config, dict) and "perception_mode" in config:
            self._perception_mode = bool(config["perception_mode"])
        if isinstance(config, dict) and "control_mode" in config:
            self._control_mode = bool(config["control_mode"])
        if isinstance(config, dict) and "drive_mode" in config:
            mode = config["drive_mode"]
            if mode in self.DRIVE_MODES:
                self._drive_mode = mode
            else:
                logger.log(f"[极速狂飙] 非法驾驶模式 {mode!r}，已忽略", "WARNING")
        if isinstance(config, dict) and "macro_group" in config:
            group = config["macro_group"]
            if not isinstance(group, str):
                logger.log("[极速狂飙] 宏组名必须是字符串，已忽略", "WARNING")
            elif group == "" or valid_group_name(group):
                self._macro_group = group
            else:
                logger.log(f"[极速狂飙] 非法宏组名 {group!r}（只允许单段目录名），已忽略",
                           "WARNING")
        return self.get_module_config()

    # ---------- 生命周期 ----------

    def start(self, start_from: str | None = None) -> None:
        """启动流程（阻塞；由外层 worker 线程调用）。"""
        assert self.ctx is not None, "离线模式（ctx=None）不可调用 start()"
        # 深度模式先过模型包校验：缺失/损坏在连窗口之前就明确终止，
        # 不把用户带进一局没有几何供数的驾驶。
        if self._resolve_drive_mode() == "depth":
            pack_error = model_assets.check_pack()
            if pack_error:
                _tlog(self, f"[极速狂飙] 深度模式无法启动：{pack_error}", "ERROR")
                return
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

        self._running = True
        # 本次运行的身份（流水行头 run_id；一局内各阶段共享，跨 run 不重置序号）。
        # 优先用队列轮次的运行编号：与日志、调试目录、性能样本同源，可按时间对齐。
        self._trace.set_run_id(logger.current_run_id
                               or logger.session_token
                               or time.strftime("%Y%m%d_%H%M%S"))
        # 启动组开在图加载之前：图加载与断点修正也是这次运行的启动正文，不能散成裸行。
        _open_grp(self, f"[极速狂飙] 模块启动（录制{'开' if self._record_mode else '关'}"
                        f"·感知{'开' if self._perception_mode else '关'}"
                        f"·驾驶模式:{self.DRIVE_LABELS[self._drive_mode]}）", "session")
        flow_ok = False
        try:
            graph = NavGraph(self.ctx)
            graph.add_plugin(PIPELINE_DIR, IMAGE_DIR)
            self._graph = graph
            _sync_log_group(self)   # 图对象晚于开组创建：补一次组号同步
            if not graph.load():
                _tlog(self, "[极速狂飙] 导航图加载失败，模块终止", "ERROR")
                self._graph = None   # 未加载成功不留给 cleanup 当已装配图处理
                return

            index = resolve_start_index(start_from)
            if start_from and index == 0:
                _tlog(self, f"[极速狂飙] 未知断点「{start_from}」，从流程起点开始", "WARNING")

            # 宏回放的前提在校验阶段就检查：组不存在/非法直接终止，不空跑一整轮导航。
            if self._resolve_drive_mode() == "macro" and not self._record_mode:
                try:
                    group_dir = resolve_group(self._macro_group)
                except MacroError as exc:
                    _tlog(self, f"[极速狂飙] 宏不可用：{exc}。"
                                f"请先录制宏，或切回其他驾驶模式", "ERROR")
                    return
                missing = [ph for ph in (1, 2)
                           if not (group_dir / f"p{ph}.jsonl").is_file()]
                _tlog(self,
                      f"[极速狂飙] 宏回放组「{group_dir.name}」"
                      + (f"（阶段 {'/'.join(map(str, missing))} 缺文件，将降级直行）"
                         if missing else ""),
                      "WARNING" if missing else "INFO")

            flow_ok = True
            self._run_flow(index)
        finally:
            # 契约 §2：进入主流程后无"自然完成"出口（循环至停止），一律落 incomplete；
            # 启动段失败（图加载/断点解析）落 failure——卡片的终态要如实反映失败阶段。
            _end_grp(self, "incomplete" if flow_ok else "failure")
            self._running = False
            self._stage_name = None

    def stop(self) -> None:
        """幂等停止：置停止标志并投递中断请求（不阻塞调用方线程）。

        这里由 GUI 的请求线程调用，等收摊会把这个线程占住；真正的等待在跑图
        线程的中断路径上做（它紧接着就要销毁框架句柄）。
        """
        self._running = False
        if self._graph is not None:
            self._graph.request_stop()

    def cleanup(self) -> None:
        """幂等释放模块资源。"""
        # 预加载线程：置停止位并短等一次；模型加载本身不可中断，超时就放手
        # （daemon 线程会自行结束，_preload_started 保证不会再起第二个）。
        self._preload_stop.set()
        th = self._preload_thread
        if th is not None and th.is_alive():
            th.join(timeout=1.5)
        # HUD 观察者先停：它从采集缓存取帧，不该在录制器 drain 帧队列时还在读帧
        if self._hud is not None:
            self._hud.stop("cleanup")
            self._hud = None
        # 宏回放先停（它持有自己的手柄租约）；宏录制次之
        if self._macro_player is not None:
            self._macro_player.stop()
            self._macro_player = None
        if self._macro_rec is not None:
            self._macro_rec.stop("cleanup")
            self._macro_rec = None
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

    # ---------- 性能卡领域项（资源面由 core 采样器经 sidecar 聚合，此处只给域内读数） ----------

    @staticmethod
    def _pct_of(vals, q: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * q))]

    # 决策状态与原因码 → 玩家可读文本（契约要求状态文本面向玩家，不是机器码）。
    # 未知码回退原码——不猜、不静默，漏了映射也还看得见原始值。
    _STATE_TEXT = {"CRUISE": "巡航", "ABORT": "回稳",
                   "CONSERVE": "保守", "FAULT": "故障"}
    _REASON_TEXT = {
        "plan:ok": "规划正常",
        "plan:ok_uncertain": "规划正常·不确定",
        "plan:ok_shortened": "规划正常·缩短",
        "plan:infeasible_corridor": "走廊不可行",
        "no_scene": "无场景",
        "unsure": "不确定",
    }
    _REASON_PREFIX = {"follow": "跟车", "validate_fail": "校验未过",
                      "infeasible": "不可行", "plan": "规划"}

    def _decision_text(self, cl: dict) -> str:
        """把「状态枚举 + 原因码」翻成一句话；未知原因码保留原码。"""
        state_raw = cl.get("state")
        state = self._STATE_TEXT.get(str(state_raw), str(state_raw))
        raw = str(cl.get("reason") or "")
        reason = self._REASON_TEXT.get(raw)
        if reason is None and raw:
            reason = self._REASON_PREFIX.get(raw.split(":", 1)[0], raw)
        return f"{state} · {reason}" if reason else state

    def read_perf_snapshot(self) -> dict:
        """领域行取值（纯读、无锁、常数时间）。

        只给值：等级由 core 按 PERF_ROWS 的阈值算（契约要求判级权单一）。
        没有数据就不给键——core 按「缺失即缺失」显示「—」，不拿 0 冒充。
        """
        p50 = self._pct_of(list(self._tick_gaps), 0.50)
        cl = self._control_last
        snap: dict = {
            "decision": ({"level": "idle", "text": "等待驾驶"} if cl is None
                         else {"level": "ok", "text": self._decision_text(cl)}),
        }
        if p50 > 0:
            snap["tick_rate"] = round(1000.0 / p50, 2)
        return snap

    def _open_grp(self, title: str, kind: str) -> None:
        _open_grp(self, title, kind)

    def _end_grp(self, outcome: str | None = None) -> None:
        _end_grp(self, outcome)

    # ---------- 流程推进 ----------

    def _run_flow(self, index: int) -> None:
        """按 FLOW 推进；走完一轮回到循环体起点继续，直到收到停止信号。"""
        assert self._graph is not None
        # 模型预加载：UI 导航阶段有数十秒空档，后台先把深度/感知模型加载好，
        # 驾驶页出现时不再付冷启动时间（失败由驾驶阶段懒加载重试）。
        self._start_preload()
        round_no = 0
        last_stamp = ""
        stamp_dup = 0
        while self._running:
            round_no += 1  # 场景分层用：录制 meta 要记"这一帧属于第几轮"
            # 每局一个产物前缀：同一局的两个驾驶阶段共用它（p1/p2 才能配对），
            # 不同局必然不同。日志会话编号只标识"一次启动"，同一次启动连跑多局时
            # 它会重复，后一局因此覆盖前一局的图（2026-10-30 实机踩到）。
            # 目录名格式不变，仍是 <前缀>_p<阶段>，历史会话照旧可读。
            round_stamp = time.strftime("%Y%m%d_%H%M%S")
            if round_stamp == last_stamp:
                # 同秒内连开两局（快速重开、系统时间回拨）：加序号，不让两局共前缀
                stamp_dup += 1
                round_stamp = f"{round_stamp}_{stamp_dup}"
            else:
                last_stamp = round_stamp
                stamp_dup = 0
            # 机位确认按"场"失效：声望归零会重置整轮进度，游戏把视角打回
            # 追尾；上一场的确认不能带进新一场。
            self._view_ok = False
            # 组边界即场次边界（迁移裁定 2）：一"轮" = 一局完整对局（两个驾驶阶段+结算）
            _open_grp(self, f"[极速狂飙] 第 {round_no} 场开始", "session")
            while index < len(FLOW):
                if not self._running:
                    return
                stage, node, phase = FLOW[index]
                self._stage_name = stage
                if node is None:
                    if not self._drive(phase, round_no, round_stamp):
                        _tlog(self, f"[极速狂飙] 「{stage}」未正常结束，本轮中止", "WARNING")
                        return
                elif not self._run_step(node, stage):
                    return
                index += 1
            index = LOOP_START_INDEX

    def _run_step(self, action_node: str, stage: str) -> bool:
        """发一次 UI 点击并按「落地页确认锚点」做有限重发（步级重试）。

        返回 True = 该步已落地（确认锚点命中）；False = 停止或重发耗尽（调用方中止本轮）。

        speedrush 一步 = 一次尝试：点击是阻塞式完成、没有独立"送达"信号，故整步算一次
        （点击发出即 `arm`，只重置时间窗）。确认后喂 SUCCESS、未落地喂 PENDING；停止由
        循环守卫 `self._running / lifecycle.running` 直接退出，不喂 STOP（对应机器"停止能力
        可选"，treasure 式逐帧自管与 speedrush 式阻塞自管共用同一转移表）。耗尽按现状终止
        本轮，**不做回合级自动重开**（步失败不证明画面回到可重开处）。
        """
        assert self._graph is not None      # 调用方 _run_flow 已建图
        assert self.ctx is not None         # 运行期编排层总是注入实 ctx
        confirm_node = STEP_CONFIRM.get(stage)
        if confirm_node is None:
            # 漏配确认锚点：退回旧的单步语义（不新增行为，也不把整条流程带崩）。
            return self._graph.run(action_node, action_node)
        max_retries, window_s, poll_s = load_decision().step_retry.dose(stage)
        machine = StepMachine(max_retries=max_retries, window_s=window_s,
                              clock=time.monotonic)
        send_click = True
        while self._running and self.ctx.lifecycle.running:
            if send_click:
                machine.arm()                                   # 一次尝试发出：只重置窗口、不动计数
                self._graph.run(action_node, action_node)       # 点击；动作节点自带识别闸门→按钮不在则空跑不误点
                send_click = False
            cmd = machine.observe(
                Verdict.SUCCESS if self._graph.run(confirm_node, confirm_node) else Verdict.PENDING
            )
            if cmd is Command.DONE:
                return True
            if cmd is Command.RETRY:
                send_click = True                               # 重发同一动作（下一次点击即新窗口）
                continue
            if cmd is Command.WAIT:
                if not self.ctx.lifecycle.sleep(poll_s):        # 窗内轮询；睡眠兼作停止信号
                    break
                continue
            if cmd is Command.FAIL:
                _tlog(self, f"[极速狂飙] 步骤「{stage}」重发 {max_retries} 次仍未落地"
                            f"（确认锚点「{confirm_node}」始终未命中），本轮中止", "WARNING")
                return False
            return False                                        # ABORT 或未知命令：保守退出
        return False                                            # 停止（_running / lifecycle 已假）

    def _drive(self, phase: int, round_no: int = 1,
               stamp: str | None = None) -> bool:
        """驾驶阶段（phase = 1/2）——驾驶控制方案的接口点。

        三条驾驶模式共用本入口：深度几何/全程直行（``_resolve_drive_mode``）、
        宏录制（采集玩家操作）与宏回放（按时间轴下发）。宏模式不加载感知/深度，
        只保留驾驶页锚点判定；宏录制的 t=0 = 驾驶页就绪时刻。

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

        mode = self._resolve_drive_mode()
        macro = mode == "macro"
        macro_recording = macro and self._record_mode
        macro_replay = macro and not self._record_mode
        degraded = False
        samples: list[dict] | None = None
        g0: float | None = None
        group_name = ""
        if macro_replay:
            try:
                group_dir = resolve_group(self._macro_group)
                group_name = group_dir.name
                samples, g0 = load_phase(group_dir, phase)
            except MacroError as exc:
                degraded = True
                _tlog(self, f"[极速狂飙] 驾驶阶段 {phase}：宏不可用（{exc}），"
                            f"本阶段降级全程直行", "WARNING")

        # 录制阶段是人工驾驶：急停检测暂停（键盘驾驶 W+A 就是两键）
        self._manual_drive = bool(self._record_mode)
        if macro_recording:
            label = "宏录制"
        elif macro_replay and not degraded:
            label = "宏回放"
        elif degraded:
            label = "降级直行"
        elif self._record_mode:
            label = "录制采集"
        else:
            label = "等待阶段结束"
        # 阶段组开在等就绪之前：就绪判定的每条日志都属于这场驾驶（卡片要完整）
        _open_grp(self, f"[极速狂飙] 驾驶阶段 {phase}（{label}）", "phase")
        try:
            if self._record_mode:
                _tlog(self, "[极速狂飙] 人工驾驶阶段：紧急停止的按键检测暂停", "INFO")
            if not self._wait_drive_ready(phase):
                return False

            recorder = None
            macro_rec = None
            macro_player = None
            observer = None
            hud_clock = None
            if macro_recording:
                macro_rec = self._begin_macro_recording(phase, stamp)
                if macro_rec is None:
                    degraded = True
                else:
                    hud_clock = self._begin_hud_clock(phase, round_no)
            elif macro_replay and not degraded:
                # 回放要记入战绩库：计时之外再读双方比分（只读内存）。
                hud_clock = self._begin_hud_clock(phase, round_no, with_scores=True)
                clock = ((lambda: self._hud_clock_row(hud_clock))
                         if hud_clock is not None else None)
                try:
                    macro_player = MacroPlayer(
                        samples or [], g0,
                        pad_cm=self.ctx.gamepad.acquire,
                        phase=phase,
                        group_id=getattr(self._log_grp, "id", None),
                        clock=clock,
                    )
                except Exception as exc:  # noqa: BLE001 —— 构造失败按本阶段降级
                    degraded = True
                    _tlog(self, f"[极速狂飙] 驾驶阶段 {phase}：宏回放启动失败"
                                f"（{exc!r}），降级全程直行", "WARNING")
                else:
                    self._macro_player = macro_player
                    macro_player.start()
            else:
                recorder = (self._begin_recording(phase, round_no, stamp)
                            if self._record_mode else None)
                # HUD 读数挂在本阶段会话上（人工局=录制会话、控制局=控制会话），
                # 两模式同一落点：<会话>/hud/（对齐目标=同会话 frames.jsonl）
                observer = (self._begin_hud(recorder.out_dir / "hud", phase, round_no)
                            if recorder is not None else None)
            try:
                return self._drive_loop(
                    phase, recorder, round_no, stamp,
                    macro_rec=macro_rec, macro_player=macro_player,
                    hud_clock=hud_clock, degraded_straight=degraded,
                    macro_group=group_name)
            finally:
                if macro_player is not None:
                    macro_player.stop()
                    self._macro_player = None
                if macro_rec is not None:
                    macro_rec.stop("phase_end" if self._running else "stopped")
                    self._macro_rec = None
                if hud_clock is not None:
                    hud_clock.stop("phase_end" if self._running else "stopped")
                if observer is not None:
                    observer.stop("phase_end" if self._running else "stopped")
                    self._hud = None
                if recorder is not None:
                    recorder.stop("phase_end" if self._running else "stopped")
                    self._recorder = None
        finally:
            self._manual_drive = False
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
                    round_no: int = 1, stamp: str | None = None, *,
                    macro_rec: MacroRecorder | None = None,
                    macro_player: MacroPlayer | None = None,
                    hud_clock: HudObserver | None = None,
                    degraded_straight: bool = False,
                    macro_group: str = "") -> bool:
        """采集/控制主循环：按目标节拍取帧，锚点降频复查阶段是否已结束。

        节拍与锚点复查**故意解耦**——锚点识别走框架（单轮耗时不定），若每帧都塞一次
        识别，帧间隔会随它抖动；帧自带真实时间戳，离线按时间戳对齐即可。退出时报一次
        实际节拍（``_log_loop_pace``）：那是"采集能不能支撑训练"的第一手判据。

        **每 tick 都取帧，不论是否录制**：这一帧是门控判定的证据锚点（判定日志里带
        ``frame=``）。读取本身是 WGC 中心缓存的共享引用（帧号不变则不重算），未录制时
        的增量成本可忽略；反过来，缺了它，门控日志就只能记"判定发生了"，记不清
        "判定发生在哪一帧"，校准判据时无从复查。

        宏分支不建控制链、不跑感知；``hud_clock`` 只读倒计时：录制时喂 g0，回放时
        同时做“计时归零”的阶段结束兜底。
        """
        assert self.ctx is not None
        assert self._graph is not None
        # 本局产物前缀：调用方（_drive）按局传入，同一局两阶段共用；
        # 直接调本方法时退回当前时刻（一阶段一个目录，不与其他局混）。
        # record_round 只认调用方显式给的前缀：测试/离线直调不落用户战绩库。
        stamp_given = stamp is not None
        stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
        macro = self._resolve_drive_mode() == "macro"
        if macro:
            # 宏模式：回放/录制都不进控制链；仅“缺阶段文件”的降级走直行档
            straight = bool(degraded_straight)
            control = bool(degraded_straight)
            shadow = False
        else:
            # 控制接管的前提：非录制模式（录制期人驾，程序不碰车）+ control_mode 开关
            # 或直行基线档（该档本身就是接管形态）。控制依赖感知产出观测，故正常控制
            # 隐含逐帧检测（不必另开 perception_mode）；直行档例外——感知/深度都不加载。
            straight = self._drive_mode == "straight" and recorder is None
            control = (self._control_mode or straight) and recorder is None
            # 录制模式影子观察（2026-10-15 对照臂接线）：人驾期间决策链照跑但不下发
            # （不接入手柄）——trace 与智能局同列同语义（cr_best=收益场本拍选点、
            # steer=本应下发的反事实杆值、executed=深度路心观测重锚的人工实际横位），
            # 人工对照局才有决策面可比。零下发：gpad 不 acquire，车只听人的。
            shadow = recorder is not None
        if recorder is not None:
            run_mode = "manual"
        elif degraded_straight:
            run_mode = "straight"      # 宏不可用降级：实际驾驶形态是直行
        elif macro:
            run_mode = "macro"
        elif straight:
            run_mode = "straight"
        else:
            run_mode = "depth"
        # 战绩库写自动控制局：深度几何、全程直行、宏回放。人工录制与宏录制不进统计。
        macro_replay = macro and macro_player is not None and not degraded_straight
        record_round = stamp_given and (
            run_mode in ("depth", "straight") or macro_replay)
        if recorder is not None:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：请开始手动驾驶"
                  f"（正在录制演示数据，决策链影子观察中）", "INFO")
        elif macro_rec is not None:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：请开始手动驾驶"
                  f"（正在录制宏，按 HUD 计时对齐起点）", "INFO")
        elif macro_player is not None:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：宏回放中"
                  f"（组 {macro_group}，等待计时对齐后下发）", "INFO")
        elif degraded_straight:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：宏不可用，本阶段降级全程直行", "WARNING")
        elif straight:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：全程直行基线（感知/深度不加载，只踩油门）",
                  "INFO")
        elif control:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：控制环接管", "INFO")
        else:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：等待阶段结束（未接管车辆）", "INFO")
        # 感知耗时记账按阶段清零（P50/P95 在循环出口随实际节拍一起报，见 _log_loop_pace）
        self._infer_times = []
        self._wait_times = []   # 等锁/run 分段（环频归因：锁等 vs 推理本身慢）
        self._run_times = []
        self._pace_body = []    # 环拍分解：环体 / 睡眠 / 睡醒空隙（13ms 悬案探针）
        self._pace_sleep = []
        self._pace_wake = []
        self._control_times = []
        self._control_last = None
        # 人工驾驶阶段的产物目录=录制会话目录：语料与证据同家，不再另建
        # ``debug/speedrush/<时刻>_p<阶段>/``（同一段画面落两份，2026-10-26 收口）
        run_dir = recorder.out_dir if recorder is not None else None
        chain = self._build_control_chain(phase, run_dir=run_dir,
                                          round_no=round_no, stamp=stamp) \
            if ((control and not straight) or shadow) else None
        if chain is not None:
            chain["drive_mode"] = run_mode
            chain["record_round"] = record_round
        # 人工局的程序叠加图由录制器自己落（同一份原帧 + 同一拍快照），链路只
        # 负责把快照交回；控制局仍走 DebugPairWriter（chain["debug"]）。
        if chain is not None and recorder is not None \
                and recorder.hud_pairs_enabled:
            chain["hud_rec"] = recorder
        # 人工局的 HUD 读数也进叠加图读数带（与自动局同一展示面）
        if chain is not None and self._hud is not None:
            chain["hud_ref"] = self._hud
        # 直行档 trace（stub chain 与控制链同协议：行进流水汇 + 阶段收尾汇总）；
        # 油门取 decision.json 的 throttle_raw 单一真源，不另抄数值。
        straight_chain = ({"trace": [], "trace_sink": self._trace,
                           "t_start": time.time(), "bad_frames": {},
                           "round_no": round_no or 1, "round_stamp": stamp,
                           "drive_mode": run_mode, "record_round": record_round,
                           "hud_ref": None}
                          if straight else None)
        # 宏回放：控制链不跑，但用 HUD 读数在每拍记一条结果行，阶段收尾走
        # 同一套 summary/战绩库落盘（模式=宏）。宏录制（人驾）不建本链。
        macro_chain = ({"trace": [], "trace_sink": self._trace,
                        "t_start": time.time(), "bad_frames": {},
                        "round_no": round_no or 1, "round_stamp": stamp,
                        "drive_mode": "macro", "record_round": record_round,
                        "hud_ref": hud_clock}
                       if macro_replay else None)
        straight_throttle = load_decision().planner.throttle_raw if straight else 0
        # 控制两档（智能/直行）同点起 HUD 读数：分数流是验收②「智能 vs 直行基线」
        # 对比的数据面。**读数与 debug 落盘解耦**：读数始终跑（战绩库要末分与
        # 对手分），debug 只决定要不要把 hud.jsonl/meta 写进调试目录。失败降级
        # 为无读数，不阻断。
        drive_hud = None
        if control:
            debug_on = self._debug_enabled()
            hud_dir = _run_dir(stamp, phase) / "hud" if debug_on else None
            drive_hud = self._begin_hud(hud_dir, phase, round_no, persist=debug_on)
            if chain is not None:
                chain["hud_ref"] = drive_hud   # 调试图读数带吃 HUD 最新行
            elif straight_chain is not None:
                straight_chain["hud_ref"] = drive_hud
        # 阶段边界入流水（受保护通道）：无论接管与否，每阶段起止各一条
        self._trace.begin_stage(phase)

        # 只有两个正常出口置 True；停止/超时/视角未确认/异常一律 False。
        completed = False
        deadline = time.monotonic() + DRIVE_TIMEOUT_S
        loop_start = time.monotonic()
        next_anchor = 0.0
        miss = 0
        frames = 0
        fid, ts_ns, age_ms = 0, 0, 0.0
        # 宏时钟：只认新的 HUD 样本；计时归零连续三次即判阶段结束（锚点之外的兜底）
        last_clock_ts = 0
        zero_hits = 0
        # 手柄租约覆盖整个控制阶段：进入取设备、退出归零归还（摇杆归中+松扳机）。
        # 循环内多处 return/raise 都经 with 收口，不留"退出后车还踩着油门"的尾巴。
        # 数据行随拍进流水汇（异步落盘）；finally 只收阶段边界与局汇总。
        try:
            with contextlib.ExitStack() as stack:
                gpad = stack.enter_context(self.ctx.gamepad.acquire()) if control else None
                # 环拍分解探针（2026-10-06）：拍间隔 = 环体 + 睡眠 + 睡醒空隙。
                # 首轮实测揪出 20Hz 之谜——monotonic 粗钟（步长 ~15.6ms）把环体
                # 量化成 0、又让 sleep deadline 过冲（33ms 请求实睡 46.8ms）。
                # pacing 全线改 perf_counter 后本探针留作常驻节拍观测。
                # 记账记在上一拍头上（下一拍 t0 才能闭合拍间隔），随
                # _log_loop_pace 出口汇报。
                prev_t0: float | None = None
                prev_body = 0.0
                prev_sleep = 0.0
                while self._running and time.monotonic() < deadline:
                    t0 = time.perf_counter()
                    pace_ms: tuple | None = None
                    if prev_t0 is not None:
                        body_ms = prev_body * 1000.0
                        sleep_ms = prev_sleep * 1000.0
                        # 环拍三列（ms）随行进 trace：dt 是帧时戳间隔、三列是
                        # 环迭代墙钟——同拍同区间对照，dt 三模态 33/50/66 的
                        # 逐拍归因面（帧供数节律 vs 环体超预算 vs 睡醒空隙）
                        pace_ms = (body_ms, sleep_ms,
                                   max(0.0, (t0 - prev_t0 - prev_body
                                             - prev_sleep) * 1000.0))
                        self._pace_body.append(pace_ms[0])
                        self._pace_sleep.append(pace_ms[1])
                        self._pace_wake.append(pace_ms[2])
                        # 拍间隔滑窗（性能卡 response 项）。超阈性能事件上报
                        # 已收（2026-11-01）：游戏场景切换必然造成 ~450ms 拍间隔
                        # （根因归游戏侧，38/38 实证），事件只会在每局头尾
                        # 误报，已无异常信号价值。
                        gap_ms = pace_ms[0] + pace_ms[1] + pace_ms[2]
                        self._tick_gaps.append(gap_ms)
                    prev_t0 = t0
                    # 取帧走 frame_with_age：录制要的是「帧到达采集回调的时刻」，不是本循环
                    # 读取它的时刻——两者差一个帧龄，直接进训练标签的时序。
                    frame, fid, ts_ns, age_ms = self.ctx.capture.frame_with_age()
                    t_pick = time.perf_counter()
                    if recorder is not None and frame is not None:
                        recorder.record_frame(frame, frame_id=fid, ts_ns=ts_ns, age_ms=age_ms)
                        t_rec: float | None = time.perf_counter()
                    else:
                        t_rec = None
                    result: PerceptionResult | None = None
                    t_det: float | None = None
                    if not straight and (self._perception_mode or control
                                         or shadow) \
                            and frame is not None:
                        perc = self._ensure_perception()
                        if perc is not None:
                            result = perc.detect(frame, frame_id=fid, ts_ns=ts_ns)
                            t_det = time.perf_counter()
                            self._last_perception = result
                            self._infer_times.append(result.infer_ms)
                            self._wait_times.append(result.wait_ms)
                            self._run_times.append(result.run_ms)
                    if hud_clock is not None:
                        clk = self._hud_clock_row(hud_clock)
                        if clk is not None:
                            if macro_rec is not None:
                                macro_rec.note_clock(clk[0], clk[1])
                            if clk[1] != last_clock_ts:
                                last_clock_ts = clk[1]
                                if macro_player is not None and clk[0] <= 0:
                                    zero_hits += 1
                                    if zero_hits >= 3:
                                        macro_player.stop()
                                        _tlog(self,
                                              f"[极速狂飙] 驾驶阶段 {phase}：HUD 计时归零，"
                                              f"停止宏回放并松手", "INFO")
                                elif clk[0] > 0:
                                    zero_hits = 0
                    if macro_chain is not None:
                        # 宏回放不跑控制链：每拍从 HUD 取一次双方比分，作为
                        # 阶段结果行；阶段收尾与自动局走同一套 summary/战绩库。
                        h_score, h_rate = _ego_hud_readout(hud_clock)
                        _note_hud_scores(macro_chain)
                        row = {"fid": fid, "ts_ns": ts_ns, "state": "MACRO",
                               "hud_score": h_score, "hud_rate": h_rate}
                        macro_chain["trace"].append(row)
                        sink = macro_chain.get("trace_sink")
                        if sink is not None:
                            sink.append(row)
                    if gpad is not None:
                        if straight:
                            assert straight_chain is not None   # straight 档恒建 stub chain
                            self._straight_tick(gpad, fid, ts_ns,
                                                straight_chain,
                                                straight_throttle)
                        elif result is not None:
                            # cap 段内段原料：拍内时间戳链（拾取/录制/检测），
                            # 掩码段与拷贝段在 _control_tick/push 内补齐——
                            # 同钟伸缩 Σ内段 ≡ cap（见 depth_geo._timing_segments）
                            assert chain is not None   # control 档恒建控制链
                            self._control_tick(
                                chain, gpad, frame, result, fid, ts_ns, age_ms,
                                phase, pace_ms=pace_ms,
                                cap_ts={"t0": t0, "pick": t_pick,
                                        "rec": t_rec, "det": t_det})
                    elif shadow and result is not None:
                        # 影子拍：同链不接入手柄（gpad=None→tick 内不下发），
                        # trace 记反事实杆值与 cr_best——对照局的决策数据面
                        assert chain is not None       # shadow 档恒建控制链
                        self._control_tick(
                            chain, None, frame, result, fid, ts_ns, age_ms,
                            phase, pace_ms=pace_ms,
                            cap_ts={"t0": t0, "pick": t_pick,
                                    "rec": t_rec, "det": t_det})
                    if recorder is not None:
                        # 快照按帧号交回录制器（同拍同帧）：没跑控制拍的帧交 None，
                        # 叠加图索引写 null 而不是挂一个空指针
                        snap_dbg = None
                        if chain is not None and chain.get("last_snap_fid") == fid:
                            snap_dbg = chain.get("last_snap")
                        recorder.attach_hud_snap(fid, snap_dbg)
                    frames += 1
                    if chain is not None and chain.get("abort_phase"):
                        _tlog(self,
                              f"[极速狂飙] 驾驶阶段 {phase}：视角未确认，中止本阶段"
                              f"（{_frame_note(fid, age_ms)}）", "WARNING")
                        return False

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
                                completed = True
                                return True
                        # 阶段 1 结束前弹「进入策略商店」横幅（齿轮仍在、画面还在驾驶）：
                        # 命中即提前结束本阶段，不等齿轮消失后的失配滞后——省掉那段滞后里
                        # 喂进感知/决策链与录制器的过场帧。横幅文案专指阶段 2，故只查阶段 1。
                        if phase == 1 and self._graph.run(STORE_BANNER_NODE,
                                                          STORE_BANNER_NODE):
                            _tlog(self,
                                  f"[极速狂飙] 驾驶阶段 {phase}：检测到进商店横幅，"
                                  f"提前结束本阶段（{_frame_note(fid, age_ms)}）", "INFO")
                            self._log_loop_pace(phase, frames, loop_start)
                            completed = True
                            return True

                    t_sl = time.perf_counter()
                    rest = DRIVE_TICK_S - (t_sl - t0)
                    prev_body = t_sl - t0
                    prev_sleep = 0.0
                    if rest > 0:
                        if not self.ctx.lifecycle.sleep(rest):
                            break
                        # 与 t_sl 同钟（perf_counter）：monotonic 差 ~163ms 固定
                        # 偏移，混用会把睡眠读成负值、空隙虚高同量级（首局实证）
                        prev_sleep = time.perf_counter() - t_sl
        finally:
            # 阶段收边界（先于其余收口：此后不再有数据行）
            self._trace.end_stage(phase)
            self._trace.flush(1.0)
            if control and chain:
                dbg = chain.get("debug")
                if dbg is not None:
                    dbg.stop("chain_end")
                self._flush_control_trace(chain, phase, completed=completed)
                self._stop_depth_observer(chain, phase)
            elif straight and straight_chain is not None:
                self._flush_control_trace(straight_chain, phase,
                                          completed=completed)
            elif shadow and chain is not None:
                # 影子链收口与控制链同协议：调试图/深度观察器停干净再 flush
                dbg = chain.get("debug")
                if dbg is not None:
                    dbg.stop("chain_end")
                self._flush_control_trace(chain, phase, completed=completed)
                self._stop_depth_observer(chain, phase)
            elif macro_chain is not None:
                self._flush_control_trace(macro_chain, phase, completed=completed)
            if drive_hud is not None:
                drive_hud.stop("phase_end" if self._running else "stopped")
                self._hud = None
            # 证据冻结放在最后：trace 已 flush、汇总已写、深度观察器已停，
            # 复制出来的是这一阶段的完整素材。失败显式告警（WARNING）。
            if chain is not None and chain.get("c_evidence"):
                self._archive_c_evidence(chain, phase)
        self._log_loop_pace(phase, frames, loop_start)
        if not self._running:
            return False
        _tlog(self,
              f"[极速狂飙] 驾驶阶段 {phase} 未在 {DRIVE_TIMEOUT_S:.0f}s 内结束"
              f"（最后 {_frame_note(fid, age_ms)}）", "WARNING")
        return False

    # ---------- 控制链（实机闭环，planner 设计稿 §九 step 3）----------

    def _debug_enabled(self) -> bool:
        """debug/ 产物面总闸（ctx.debug.enabled；ctx 无该面=关）。

        管整个 debug/speedrush/<会话>/：raw/hud 叠加图（两种局同一套布局）、
        hud 读数、badframes 取证、depth_debug 证据包（密度另受 debug_dump
        三态闸 off/events/full）。流水与局汇总不在此门——它们随「日志记录」
        开关落 ``logs/<会话>/trace/``（ADR-0010）。开关
        关=调试图与深度证据零产物；**录制语料不受此门**（人工局的原帧与手柄是
        录制模式的产出，见 _begin_recording）。data/（业务数据 DB）与 logs/
        也不受此门。"""
        return bool(getattr(getattr(self.ctx, "debug", None), "enabled", False))

    def _build_control_chain(self, phase: int = 0, *,
                             run_dir: Path | None = None,
                             round_no: int | None = None,
                             stamp: str | None = None) -> dict:
        """按阶段新建一份干净世界：跟踪/聚合/决策/规划各一，配置开局读一次。

        阶段间不共享状态（行为稿 §九：阶段信息只进过渡窗判据，两阶段同一决策器），
        新建即天然清空——与 DecisionEngine.reset 的"冷启动"语义一致。

        ``run_dir``：产物目录（证据帧/坏帧/深度证据包；逐拍流水在
        ``logs/<会话>/trace/``，不在此目录）。人工驾驶阶段由调用方
        传入录制会话目录，与语料同家；缺省按时刻新建 ``debug/speedrush/<时刻>_p<N>/``。
        """
        cfg = load_decision()
        cal = load_calib()   # 标定真源一次读入（深度观测器同源）
        wt = WallTrack(cal)  # 墙实体（第 5 步）：逐箱跨帧假设，ego_road 持有
        t_start = time.time()
        manual = run_dir is not None   # 人工驾驶阶段（语料目录已由录制器建好）
        run_dir = run_dir or _run_dir(stamp or time.strftime("%Y%m%d_%H%M%S"), phase)
        dbg_on = self._debug_enabled()
        lane_obs = LaneObsWorker(
            # config.LaneObs 与 lane_obs.LaneObsConfig 字段同构（worker 只读
            # enabled/weights/hz/provider/threads/detect_every），两份配置类
            # 各自独立声明，故此处按契约收窄
            cast(LaneObsConfig, cfg.lane_obs),
            off_max=cfg.validate.x_lane_abs_max)
        lane_obs.start()
        if lane_obs.error:
            level = "INFO" if lane_obs.optional_missing else "WARNING"
            _tlog(self, f"[极速狂飙] 线观测未启动：{lane_obs.error}", level)
        elif lane_obs.note:
            _tlog(self, f"[极速狂飙] 线观测：{lane_obs.note}", "WARNING")
        return {"cfg": cfg,
                "lane_obs": lane_obs,
                # 速度像素桥（闸 metric_bridge.enabled，默认关=逐位回今天）：
                # 闸与迟滞驻留由链构建传入，tracking 层不读配置
                "tracker": Tracker(bridge_enabled=cfg.metric_bridge.enabled,
                                   bridge_stay=cfg.metric_bridge.stay,
                                   box_bridge_enabled=cfg.box_bridge.enabled,
                                   box_bridge_trace_only=cfg.box_bridge.trace_only,
                                   anchor_box_gate_enabled=cfg.box_bridge.anchor_gate,
                                   lane_line_enabled=cfg.lane_line.enabled),
                "agg": CoinGroupAggregator(),
                # conf_floor：宽度有效性门的置信地板（真源=scoring.conf_floor，
                # 传入不抄第二份）
                "traffic_obs": TrafficObserver(cfg.traffic,
                                               conf_floor=cfg.scoring.conf_floor),
                # lane_w_m 供栅格区域读数的米→道换算（world_model 同源锚）
                "lane_w_m": cal.lane_w_m,
                # 线角相机的全幅焦距（gate0 标定；不是 MoGe 逐帧 fx 的口径）
                "focal_px": cal.focal_px,
                "wall_track": wt,
                "ego_road": _EgoRoadObserver(
                    cfg.grid_xcheck, cal.lane_w_m, wt,
                    mem_fallback=cfg.corridor_reach.enabled,
                    far_ground_supply=cfg.geometry.far_ground_supply,
                    off_max=cfg.validate.x_lane_abs_max),
                # 深度几何观测器（架构裁决 2026-09-24：深度区域当几何主人；同日
                # 解耦：异步 worker，控制拍只 push+take，observe 成本移出热路径）。
                # 会话为 None（权重缺失/加载失败）时不起线程、take 恒 None——
                # road_offset 退纯模型积分，黄线层不再补位（退役为骨架）。
                # 生命周期=chain：这里 start，驾驶循环 finally 里 stop（见
                # _stop_depth_observer 的收口与健康报警）。
                "depth_geo": _started_async_depth(
                    DepthRoadObserver(self._ensure_depth_session(), cal),
                    # 证据包落点=run_dir/depth_debug；双闸：debug 开关（debug/
                    # 产物面总闸）×debug_dump 三态（off=零落盘；events=事件触发
                    # ring+窗口；full=恒开全量，感知排错期）。文件格式三态同构
                    debug_dir=(None if not dbg_on or cfg.debug_dump.mode == "off"
                               else run_dir / "depth_debug"),
                    dump=None if cfg.debug_dump.mode == "off" else cfg.debug_dump),
                    "engine": DecisionEngine(cfg), "planner": LateralPlanner(cfg.planner),
                    # 线↔深度互证闸（货车遮挡防线）：每局一份状态机
                    "lane_agree": _LaneAgreeGate(),
                    # 到达位置谱系（路心读数序列；影子/未来到达门消费）
                    "pos_lineage": PositionLineage(),
                    # 轨迹计划（motion.mode=off 时恒不调用）：候选时稳门随阶段重建
                    "gate": PlanGate(cfg.motion.switch_margin,
                                     cfg.motion.switch_dwell_s,
                                     cfg.motion.score_epsilon,
                                     cfg.motion.switch_geom_margin,
                                     cfg.motion.switch_immediate_margin,
                                     cfg.motion.switch_infeasible_grace_s),
                    "last_plan": None,
                    "last_plan_ego": None,
                    "last_committed": False,
                # raw/hud 调试图落盘器（10fps；debug 开关闸，测试 ctx=None → None →
                # tick 空转）。生命周期=chain，finally 收口。人工驾驶阶段不建：
                # 录制器既写原帧又写同一套叠加图与索引，再建 writer 就是同一段
                # 画面落两份（2026-10-30 布局收口，两模式共用 session_layout）。
                "debug": (DebugPairWriter(run_dir, cal=cal, phase=phase,
                                          round_no=round_no)
                          if dbg_on and not manual else None),
                # 人工局的叠加快照收件人（_drive_loop 建链后注入；控制局为 None）
                "hud_rec": None,
                "hud_ref": None,
                # 开局视角守卫（闸 view_guard.enabled）：本进程确认过保险杠
                # 机位后不再建；成功/失败即终态。abort_phase 由守卫置位，
                # 主循环看到后中止本阶段（不是停控滑行）。
                "view_guard": (ViewGuard(cfg.view_guard, time.perf_counter())
                               if cfg.view_guard.enabled and not self._view_ok
                               and self._control_mode and self._drive_mode != "straight"
                               else None),
                "abort_phase": False,
                "prev_ts": None, "disabled": False,
                "trace": [], "trace_sink": self._trace,
                # 跨会话残留的慢 forward 锁存不写进新会话（同钟起点过滤）
                "slow_seen_ns": int(time.perf_counter() * 1e9),
                "bad_frames": {"next_at": 0.0, "saved": 0, "dir": None},
                # 产物目录的唯一真源：坏帧/证据包都按它落，不各自再算一遍时刻目录
                # （否则人工驾驶阶段会另建一个非 manual_ 目录，等于又落一份）
                "run_dir": run_dir,
                "round_no": round_no or 1,
                "round_stamp": stamp,
                "t_start": t_start}

    # ---------- 开局视角守卫（2026-10-23） ----------

    def _tick_view_guard(self, chain: dict, gpad, dgeo, dgeo_new: bool) -> None:
        """喂一拍相机高度；坏视角按 RB。异常一律吞掉，守卫不炸驾驶链。"""
        vg = chain.get("view_guard")
        if vg is None:
            return
        try:
            cam_h = None
            if dgeo is not None and dgeo.coef is not None:
                try:
                    cam_h = abs(float(dgeo.coef[2]))
                except (TypeError, ValueError, IndexError):
                    cam_h = None
            action = vg.update(time.perf_counter(), cam_h, bool(dgeo_new))
            if action == ViewGuard.WAIT:
                return
            if action == ViewGuard.OK:
                _tlog(self, f"[极速狂飙] 视角确认：相机高度中位 {vg.median:.2f}m"
                            f"（{len(vg.samples)} 样本，第 {vg.views} 个视角）",
                      "INFO")
                self._view_ok = True
                chain["view_guard"] = None
            elif action == ViewGuard.SWITCH:
                self._press_view_button(gpad)
                med = "无有效读数" if vg.median is None else f"{vg.median:.2f}m"
                _tlog(self, f"[极速狂飙] 相机高度中位 {med}，按 RB 切换视角"
                            f"（第 {vg.views}/{vg.cfg.max_views} 个）", "WARNING")
            elif action == ViewGuard.FAIL:
                chain["view_guard"] = None
                chain["abort_phase"] = True
                med = "无" if vg.median is None else f"{vg.median:.2f}m"
                _tlog(self, f"[极速狂飙] {vg.cfg.max_views} 个视角都未确认保险杠"
                            f"机位（最后中位 {med}），本阶段中止", "WARNING")
        except Exception as exc:  # noqa: BLE001 —— 守卫故障不碰驾驶链
            chain["view_guard"] = None
            _tlog(self, f"[极速狂飙] 视角守卫异常，跳过检查: {exc!r}", "WARNING")

    def _press_view_button(self, gpad) -> None:
        """短按 RB 切视角：press→update→60ms→release→update（release 必达）。"""
        from maaracing_master.core.vgamepad_lazy import vg
        btn = vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER
        gpad.press_button(btn)
        gpad.update()
        try:
            time.sleep(0.06)
        finally:
            gpad.release_button(btn)
            gpad.update()

    def _control_tick(self, chain: dict, gpad, frame, result: PerceptionResult,
                      fid: int, ts_ns: int, age_ms: float, phase: int,
                      pace_ms: tuple | None = None,
                      cap_ts: dict | None = None) -> None:
        """一拍全链：感知→跟踪→聚合→边界→车流派生→决策→规划→下发。

        影子拍（``gpad=None``，录制模式对照臂）：同链照跑不接入手柄——
        trace 与实况照记（steer=本应下发的反事实杆值，executed=深度路心观测
        重锚的人工实际横位），不下发，车只听人。

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
            last_fid = chain.get("last_fid")
            fid_jump = last_fid is not None and fid - last_fid > 2
            chain["last_fid"] = fid
            # 深度几何（几何主人，v4 2026-10-01：MoGe 逐帧焦距点云 3D 找边当
            # road_offset 供数源；黄线 2D 链路整体退役）。异步解耦协议同前：
            # 本帧交 worker 后台推理，take() 只消费过了 age 闸的最新结果——
            # 控制拍不再付 observe 成本。协议本身不抛，异常闸按观测件惯例
            # 保留（fail-safe）。push/take 前移到 tracker 之前：锚点清单随
            # take 面世（dgeo_new 才有效，复用拍不重复供数），供本拍
            # tracker.update 合成混合测距读数（米制优先，metric_cars 闸）。
            dgeo = None
            dgrid = None
            dgeo_new = False
            anchor_sheet = None
            try:
                # note=上一拍决策快照（state/杆/喂入路心/EMA/槽）：随帧进调试图
                # 的决策带，供人工逐帧核对"大脑当时拿了什么"。不可变小 dict，
                # 主线程每拍新建、不改旧对象，跨线程无竞争。
                snap = chain["ego_road"].last
                last = self._control_last or {}
                # 掩码段终点戳：检测段（det）之后 tick 前奏+掩码构建到此为止，
                # push 锁内拷贝+入队为终点段（cap_copy），见 depth_geo._timing_segments
                mask = _yolo_object_mask(result)
                boxes = _yolo_car_boxes(result)
                if cap_ts is not None:
                    cap_ts = dict(cap_ts, mask=time.perf_counter())
                chain["depth_geo"].push(
                    frame, object_mask=mask,
                    frame_ts_ns=ts_ns,
                    detections=boxes, fid=fid, cap_pre=cap_ts,
                    note={"fid": last.get("frame_id"), "state": last.get("state"),
                          "reason": last.get("reason"), "steer": last.get("steer"),
                          "elane": last.get("executed_lane"),
                          "xt": last.get("x_target"),
                          "ro": last.get("road_offset"), "src": snap.get("source"),
                          "ro_raw": snap.get("off_raw"),
                          "age": last.get("dgeo_age"), "new": last.get("dgeo_new")})
                dgeo, dgrid, dgeo_new = chain["depth_geo"].take()
                if dgeo_new:
                    anchor_sheet = chain["depth_geo"].last_sheet
            except Exception as exc:  # noqa: BLE001 —— 观测件故障不碰主循环
                _tlog(self, f"[极速狂飙] 深度几何观测异常（{exc!r}）", "WARNING")
            if chain.get("view_guard") is not None and gpad is not None:
                self._tick_view_guard(chain, gpad, dgeo, dgeo_new)
            # 线观测先取（供数也要用）：线严格可用时它就是走廊中心原点，
            # 深度只填边界/宽度。用上一拍的执行位（本拍 planner.update 还没跑），
            # 与决策拿到的 executed 同源，场景参考系因此一致。
            # 读取前移到 tracker.update 之前（车道线定道 phase 1）：同一拍
            # 快照供跟踪层做框底行车道成员插值。
            lw = chain.get("lane_obs")
            lane = lw.latest() if lw is not None else None
            lane_usable = (lane is not None and bool(lane.get("usable"))
                           and (lane.get("det_age_ms") or 0.0)
                           <= LANE_POS_AGE_MS)
            obs = chain["tracker"].update(result, frame_age_ms=age_ms, stage=phase,
                                          anchors=anchor_sheet,
                                          lane=lane if lane_usable else None)
            obs = chain["agg"].update(obs)
            planner: LateralPlanner = chain["planner"]
            tviews, tevents = chain["traffic_obs"].update(obs)
            # road_offset 供数源=分带走廊：供数主人选择合并进 _EgoRoadObserver
            # 内部的一条优先级链，此处不再两条供数路互相顶替；找边对心保留为
            # 审计列（ro_edge）。走廊 None=诚实弃权拍，退纯模型积分。
            # 供数先于 engine.update：选择层消费同拍 corridor 带。
            # dgrid：同拍栅格，红块内缘/trace 用；栅格绝对否决已删除。
            edge_off = None
            if dgeo is not None and dgeo.sides == 2 \
                    and dgeo.left_edge_lane is not None \
                    and dgeo.right_edge_lane is not None:
                edge_off = -((dgeo.left_edge_lane + dgeo.right_edge_lane)
                             / 2.0)   # 找边对心（审计面）
            # 原点保持：线失锁的短断供拍仍用线原点（与 planner 的
            # _LINE_REBUILD_S 同一预算），否则走廊中心会在线与深度之间
            # 来回换，场景参考系跟着抖。
            line_origin = bool(lane_usable or planner.line_primary)
            corridor = chain["ego_road"].update(
                dgeo, new=dgeo_new, grid=dgrid, fid=fid,
                far_extents=(dgeo.far_extents if dgeo is not None else ()),
                center_override=(-planner.state.executed_lane
                                 if line_origin else None),
                v_lat=planner.state.v_lat)
            road_offset = None if corridor is None else -corridor.center_lane
            # 线↔深度互证（2026-11-02 货车段）：深度路心家族本拍读数对线
            # 的新鲜横位；wall 不参与（8750 拍错在 wall）。分歧喂互证闸，
            # 结果并入 lane_pos_ok——线整体错位时规划器不再 0.7 增益跟随。
            depth_ro = None
            road_snap = chain["ego_road"].last or {}
            if road_snap.get("src") in ("region", "pair_slot", "memory"):
                depth_ro = road_snap.get("off")
            ldis = None
            if lane is not None and lane.get("x") is not None:
                lx = float(lane.get("x"))
                if depth_ro is not None:
                    ldis = lx - float(depth_ro)
                elif dgeo is not None and dgeo.sides == 2 \
                        and edge_off is not None:
                    # 家族缺位（src=line 回声段）时的第二见证：找边对心。
                    # 2026-11-03 局贴墙段帧证：记忆路心被信念横速积分污染
                    # （说 −0.5，标线 −2.08），只有边对还在独立说话。
                    ldis = lx - float(edge_off)
            lane_pos_ok = bool(lane_usable
                               and chain["lane_agree"].update(ldis))
            # 供数门撤销（80e9a1c 曾 pair-only，13:29 局证伪）：单侧/slot 合成虽带
            # 半宽记忆噪声，但挡掉它们=制造供数黑视——死亡螺旋段 87/118 拍被挡，
            # 陈旧重基（planner ⑥b）与跳变门都吃"喂入"，黑视让两把安全锁同时失效。
            # 噪声交给 0.8 新息门+1.0s 陈旧重基消化；trace road_offset 列=实际喂入。
            # road_offset_new：复用拍喂的是旧证据、遮挡墙/记忆是历史证据，
            # β 速度修正只认新鲜实测（pair/wall/region 新拍）；planner 侧语义
            # 同前（重复记账曾把单次找边翻跳放大成饱和侧滑，2026-10-06 局）。
            road_offset_hold = (corridor is not None
                                and corridor.src in (
                                    "pair_slot", "wall_occl", "memory"))
            road_offset_new = (dgeo_new and corridor is not None
                               and not road_offset_hold)
            # 参考系跳变标注（仅记录，不参与控制）：每次新鲜路心读数与上一次
            # 新鲜读数比，按物理可动包络判"运动 / 换档 / 黑视"——离线辨识
            # 车模、算观测真值先按此列过滤，避免把换档当成 6 道/s 的横移。
            ro_hop = None
            if road_offset_new and road_offset is not None:
                _prev = chain.get("ro_hop_prev")
                _src = chain["ego_road"].last.get("source")
                _t_s = ts_ns / 1e9
                if _prev is not None:
                    ro_hop = classify_ro_hop(_prev[0], _prev[1], _prev[2],
                                             _t_s, road_offset, _src)
                chain["ro_hop_prev"] = (_t_s, road_offset, _src)
            # 到达位置归因（2026-10-28）：与 ro_hop 同源，但在引擎推进**之前**
            # 算出——路线影子/未来到达门要在同一拍消费。只描述位置来自哪份
            # 读数，不评价读数本身；缺读数记 no_reading，不静默当 NORMAL。
            _lineage = chain.get("pos_lineage")
            position_evidence = None if _lineage is None else _lineage.step(
                fresh=(road_offset_new if road_offset is not None else False),
                hop=None if ro_hop is None else ro_hop["hop"],
                ro=road_offset, fid=fid)
            out = chain["engine"].update(
                obs, dt, executed_lane=planner.state.executed_lane,
                traffic=(tviews, tevents),
                road_offset=road_offset,
                corridor=corridor,
                position=position_evidence,
                # 深占据伪车源（2026-11-03）：YOLO 全盲拍里近距大车按栅格
                # 路内窄块簇入账，正面撞的主根通道。
                depth_grid=dgrid,
                # 位置可信度直供决策：执行层本拍外推（线+深度都无新证据）时
                # 决策不重建目标——场景全部由 executed 平移，假位置下的选择
                # 会给出假目标。判据与冻结见 planner.update。
                state_unverified=planner.state_unverified,
                # 持久车道图的时间维：前进/横移预测用执行层在线速度
                ego_speed_mps=planner.state.v_mps,
                ego_v_lat_lane_s=planner.state.v_lat)
            cfg = chain["cfg"]
            # 承诺期切换时清候选时稳门：否则并线第一拍可能把上一拍的
            # 机会层候选按“短暂不可行宽限”保持进来，等于承诺期静默改道。
            committed = bool(cfg.route.control and out.phase == "merging")
            if committed and not chain.get("last_committed"):
                chain["gate"].reset()
            chain["last_committed"] = committed
            plan = None
            if cfg.motion.mode != "off" and chain["engine"].scene is not None \
                    and out.state is DecisionState.CRUISE \
                    and out.x_target is not None:
                plan_ego = EgoState(planner.state.executed_lane,
                                    planner.state.heading,
                                    planner.state.v_mps,
                                    planner.state.steer_norm)
                plan = plan_motion(
                    chain["engine"].scene, plan_ego,
                    cfg.motion, cfg.selection, cfg.planner, chain["gate"],
                    # 唯一意图：决策本拍的目标（planner 车道系）。计划不再自己
                    # 挑机会——它只回答"怎么安全地去"，避免两套目标轮流打方向。
                    intent_lane=out.x_target)
                chain["last_plan_ego"] = plan_ego
            else:
                chain["last_plan_ego"] = None
            chain["last_plan"] = plan
            # lane 已在供数前取过（同一对象、同一拍）：这里只判语义门。
            lane_ok = (lane is not None and lane.get("state") == "TRACK"
                       and int(lane.get("n_locked") or 0) >= 1
                       and lane.get("x") is not None)
            lane_relock = (lane is not None
                           and lane.get("state") == "RELOCK")
            cmd = planner.update(out, dt, current_fid=fid, road_offset=road_offset,
                                 road_offset_new=road_offset_new,
                                 road_offset_hold=road_offset_hold,
                                 lane_x=None if lane is None else lane.get("x"),
                                 lane_ok=lane_ok,
                                 lane_pos_ok=lane_pos_ok,
                                 lane_relock=lane_relock,
                                 lane_theta=None if lane is None
                                 else lane.get("theta"),
                                 lane_theta_ok=lane_ok,
                                 lane_fid=None if lane is None
                                 else lane.get("det_fid"),
                                 lane_age_ms=None if lane is None
                                 else lane.get("det_age_ms"),
                                 # 线角只认 ≥2 条线融合的角度（单条线会给出
                                 # 20°+ 假值）；只影响朝向影子列，不改控制。
                                 lane_n_locked=None if lane is None
                                 else int(lane.get("n_locked") or 0),
                                 plan=(plan if cfg.motion.mode == "live" else None))
            if lw is not None:
                cam_h = (None if dgeo is None or dgeo.coef is None
                         else abs(float(dgeo.coef[2])))
                # 线观测的绝对锚点=同帧新鲜**独立深度**读数换算的自车横位
                # （复用/历史拍不给，让线退回上一拍横位做子道细化，不让旧深度
                # 冒充新锚）。线作原点时 corridor 的中心是线，不能回喂；但覆盖
                # 前的深度候选仍在 depth_anchor_ego 里，用它锚道号即可——既不
                # 循环，也不剥夺线在短断/重锁时的独立参照。
                anchor = (chain["ego_road"].depth_anchor_ego
                          if road_offset_new else None)
                lw.push(frame, fid, cam_h, planner.state.v_lat, dt,
                        fid_jump,
                        focal=chain.get("focal_px"),
                        anchor=anchor)
        except Exception as exc:  # noqa: BLE001 —— 控制故障降级为观测，不崩主循环
            chain["disabled"] = True
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：控制链异常，本阶段停控转观测"
                  f"（{exc!r}，{_frame_note(fid, age_ms)}）", "WARNING")
            return
        if gpad is not None:   # 影子拍 gpad=None：不下发（对照臂只记录）
            gpad.left_joystick(x_value=cmd.steer_x, y_value=0)
            gpad.right_trigger(value=cmd.throttle)
            gpad.update()
        self._control_times.append((time.perf_counter() - t_c) * 1000.0)
        # 坏帧取证采样（内部自带异常吞噬与自禁，绝不把驾驶链拖下水）；
        # debug/ 产物面总闸：开关关=取证也停
        chain["bad_frames"]["last_steer"] = cmd.steer_x
        if not obs.health.stage_transition and self._debug_enabled():
            _maybe_save_bad_frame(chain, frame, dgeo, fid,
                                  time.monotonic(), ts_ns)
        eng = chain["engine"]
        self._control_last = {
            "state": out.state.value, "reason": out.reason, "steer": cmd.steer_x,
            "frame_id": fid, "executed_lane": round(planner.state.executed_lane, 3),
            "x_target": out.x_target,
            "road_offset": None if road_offset is None else round(road_offset, 3),
            "dgeo_age": round(chain["depth_geo"].last_age_ms), "dgeo_new": dgeo_new,
            # 场跟随可观测面（阶段 0）：原始胜出/执行分支
            "goal": eng.goal,
            "exec_branch": eng.exec_branch,
            # 四层选择观测面（layered；legacy 全 None）：层号/原因/路点数
            "sel_layer": None if eng.selection_report is None
            else eng.selection_report.layer,
            "sel_why": None if eng.selection_report is None
            else eng.selection_report.why,
            "sel_nwp": None if eng.selection_report is None
            else len(eng.selection_report.waypoints),
            "maneuver": out.maneuver}
        # 逐拍控制 trace（C1 定档 K_v 与 §七.1 复测的数据源）。数据行一式两份：
        # 内存全量行供局汇总聚合（_write_game_summary），流水汇行异步落会话目录。
        # boundary 黄线列已随 2D 链路退役（v4 换装），路缘观测量看
        # dgeo_left/dgeo_right 两列。
        # 候选诊断三列：金币组数 + 最高分 + 该组横向偏移——直接回答"有没有币/差多少分/
        # 在几车道外"。评分器是纯函数、每拍已算，这里只多读一次结果，不改决策。
        best: tuple[float, float] | None = None
        for g in obs.coin_groups:
            s = chain["engine"].scorer.score(g, obs)
            if s is not None and (best is None or s.score > best[0]):
                best = (s.score, g.x_center)
        # 红块内缘读数（"整块"边界候选，方向 3 任务 2 前置数据面）：绿区逐帧
        # 边界在护栏根部逐帧摆动（对宽门拒绝的根因），红块是大结构跨帧稳；
        # 栅格已含挖除（YOLO 框挖除在栅格构建前）。trace-only 数据面，
        # 生产判定路径不消费。
        blk = (blocked_inner_reading(dgrid, chain["lane_w_m"])
               if dgrid is not None else None)
        h_score, h_rate = _ego_hud_readout(chain.get("hud_ref"))
        _note_hud_scores(chain)
        # 卡顿归因（2026-10-22）：本拍墙钟拆成 取帧等待/录制/检测/掩码/控制剩余。
        # 与 pace_body 同拍对照，区分“游戏/采集停摆”与“决策链阻塞”。
        _cap = cap_ts or {}
        _now = time.perf_counter()

        def _seg(a, b):
            return None if a is None or b is None \
                else round((b - a) * 1000.0, 1)

        cap_wait_ms = _seg(_cap.get("t0"), _cap.get("pick"))
        rec_ms = _seg(_cap.get("pick"), _cap.get("rec"))
        det_ms = _seg(_cap.get("pick"), _cap.get("det"))
        mask_ms = _seg(_cap.get("det"), _cap.get("mask"))
        ctrl_ms = _seg(_cap.get("det"), _now)
        _dbg = chain.get("debug")
        _dst = _dbg.stats() if _dbg is not None else {}
        _slow = last_slow_forward()
        if _slow is not None and _slow.get("ts_ns", 0) > chain.get(
                "slow_seen_ns", 0):
            chain["slow_seen_ns"] = _slow["ts_ns"]
        else:
            _slow = None
        _plan_ego = chain.get("last_plan_ego")   # plan 非 None 时必为本拍 EgoState
        _row = {
            "fid": fid, "ts_ns": ts_ns, "dt": round(dt, 4),
            # pace 三列（环体/睡眠/睡醒空隙，环迭代墙钟 ms）——dt（帧时戳间隔）
            # 三模态 33/50/66 的逐拍归因面；首拍无上区间为 None
            "pace_body": None if pace_ms is None else round(pace_ms[0], 1),
            "pace_sleep": None if pace_ms is None else round(pace_ms[1], 1),
            "pace_wake": None if pace_ms is None else round(pace_ms[2], 1),
            # 本拍内段（ms）：取帧等待/录制/检测（含录制）/掩码/控制剩余；
            # 卡顿拍看哪一段占满 pace_body。yolo 三段来自 PerceptionResult。
            "cap_wait_ms": cap_wait_ms, "rec_ms": rec_ms, "det_ms": det_ms,
            "mask_ms": mask_ms, "ctrl_ms": ctrl_ms,
            "yolo_infer_ms": None if result is None
            else round(result.infer_ms, 1),
            "yolo_wait_ms": None if result is None
            else round(result.wait_ms, 1),
            "yolo_run_ms": None if result is None
            else round(result.run_ms, 1),
            # 调试 IO 侧观测（上一拍统计）：写盘/丢旧/队列深度/单对耗时
            "dbg_saved": _dst.get("saved"), "dbg_dropped": _dst.get("dropped"),
            "dbg_q": _dst.get("q"), "dbg_ms": _dst.get("ms"),
            "state": out.state.value, "reason": out.reason,
            "x_target": out.x_target,
            # 行为契约观测面（阶段 0，只记录不控制）：意图所有者/权限/目标/
            # 计划角色/物理否决——离线判断“谁在决定、权限是否接到执行”。
            "authority": out.authority,
            "intent_lane": None if out.intent_lane is None
            else round(out.intent_lane, 3),
            "intent_phase": out.phase,
            "intent_deadline_s": None if out.deadline_s is None
            else round(out.deadline_s, 2),
            "intent_owner": "route" if out.intent_lane is not None else "decision",
            "steer_owner": ("plan" if cfg.motion.mode == "live"
                            and plan is not None and plan.valid else "planner"),
            "plan_role": ("none" if plan is None else
                          "shadow" if cfg.motion.mode != "live" else
                          "executor" if plan.valid else "veto"),
            "veto_reason": (None if plan is None or plan.valid else plan.reason),
            # 场跟随可观测面（阶段 0）：输出目标/执行分支
            "goal": eng.goal,
            "exec_branch": eng.exec_branch,
            # 四层选择观测面（layered；legacy 全 None）：层号/原因/路点数
            "sel_layer": None if eng.selection_report is None
            else eng.selection_report.layer,
            "sel_why": None if eng.selection_report is None
            else eng.selection_report.why,
            "sel_nwp": None if eng.selection_report is None
            else len(eng.selection_report.waypoints),
            # 选择帧快照（录制对照/参数校准）：ego/中心/左右缘/路点/机会
            # 全部在同一个选择层坐标（道），离线可直接和人工 executed 对齐
            "sel_dbg": eng.selection_debug,
            # 粗层影子（2026-10-19）：绿区净空 + 2D 占用选方向；控制链不消费
            "coarse": None if eng.coarse_report is None
            else eng.coarse_report.as_trace(),
            # 路线承诺（2026-10-23 V1）：阶段/参考道/定案与拒绝原因/门槛数值。
            # 闸关时这一列是**影子对照面**——离线能量化"承诺拦掉了哪些翻面"
            "route": None if eng.route_verdict is None
            else eng.route_verdict.as_trace(),
            # 到达位置归因 + 到达门影子（2026-10-28）：同一拍同一输入再跑一份
            # 带门的路线，只落 trace，不接控制；门关时生产 route 列不变。
            "attribution": None if eng.position_evidence is None
            else eng.position_evidence.as_trace(),
            "route_shadow": None if eng.route_shadow_verdict is None
            else eng.route_shadow_verdict.as_trace(),
            # 选择层影子（2026-10-28）：None=两路承诺输入相同且选择一致；
            # div=影子路线开始走不同承诺态（此后差异是预期）；bug=同输入
            # 不同结果（影子接线/选择器确定性），带首次现场取证。
            "sel_shadow": eng.selection_shadow_trace,
            # 场景快照（motion_plan 输入面）：自车/走廊年龄/对象年龄与 σ
            "scene": None if eng.scene is None else eng.scene.as_trace(),
            # 对象表（第 1 期观测面）：身份键/年龄/本拍可见/安全标记与原因；
            # 超龄条目会在这里比 scene 多留一会儿，safety_ok 已失效。
            "otab": eng.object_table_trace(),
            # 持久车道图（第 2 期观测面）：支撑距离/采样点/预测误差样本；
            # 本阶段只记录，路线与预置尚未消费。
            "map": eng.lane_map_trace(),
            # 开度/密度与 60m 锚（第 4 期观测面）：只记录，分级时域尚未消费。
            "open": eng.openness_trace(),
            "anchor": eng.anchor_trace(),
            # 拟议视界（影子）与重算触发：只记录；选择层仍用固定视界。
            "h_shadow": eng.horizon_trace(),
            "trig": eng.trigger_trace(),
            # 轨迹计划（shadow/live 才有）：valid/layer/预览/候选
            "plan": None if plan is None else plan.as_trace(),
            # 本拍两层名义视界（秒，观测面）：今天都是常量，分级时域落地后
            # 用同一列看它是否真的在动。
            "plan_h": round(cfg.motion.horizon_s, 3),
            "sel_h": round(cfg.selection.horizon_s, 3),
            # 计划输入状态（本拍 planner.update 前）与 gate 状态（离线逐位重放）
            "plan_ego": None if plan is None or _plan_ego is None else [
                round(_plan_ego.x_lane, 6),
                round(_plan_ego.heading, 6),
                round(_plan_ego.steer_norm, 6)],
            "plan_gate": None if plan is None else list(
                chain["gate"].snapshot()),
            "maneuver": out.maneuver,
            "steer_norm": round(planner.state.steer_norm, 4),
            "steer_raw": round(planner.last_steer_raw, 4),
            "steer_x": cmd.steer_x,
            "executed_lane": round(planner.state.executed_lane, 4),
            # 在线前向速度（m/s）：持久车道图的前进预测输入，离线复算同一
            # 张图要用它；None=估计窗未开（图本拍不前进，只衰减）。
            "v_ego": None if planner.state.v_mps is None
            else round(planner.state.v_mps, 3),
            "v_lat": round(planner.state.v_lat, 4),
            "heading": round(planner.state.heading, 4),
            "n_groups": len(obs.coin_groups),
            # 红块内缘读数三列（"整块"边界候选）：护栏块逐行内缘的加权中位
            # （车道单位，左负右正）+ 双侧红块行数（覆盖面，低=可信度低）——
            # 与绿区读数（ro_*）同拍对照，红块跨帧稳即边界来源候选成立
            "blk_l": None if blk is None or blk.left_lane is None
            else round(blk.left_lane, 3),
            "blk_r": None if blk is None or blk.right_lane is None
            else round(blk.right_lane, 3),
            "blk_rows": None if blk is None else blk.rows,
            # 币组逐组审计面（2026-10-17）：逐组 conf_min/本帧观测成员数/末见
            # 帧/部分观测——存续组（本帧无成员观测，conf_min=0）评分断供拍
            # 占比离线可算（宽限窗内"组在、评分断"是币组证据闪独立路径）
            "cgroups": [
                {"id": g.group_id, "cm": round(g.conf_min, 2),
                 "obs": g.observed_count, "last": g.last_seen_fid,
                 "part": g.partial_observation,
                 # 成员轨道 id 与 lead id（2026-11-01，观测面）：组号是聚合
                 # 编号空间，跨帧对账/拆分合并审计要靠轨道 id 集合。
                 "mids": list(g.member_ids), "lead": g.lead_id}
                for g in obs.coin_groups] or None,
            "best_score": None if best is None else round(best[0], 1),
            "best_x": None if best is None else round(best[1], 2),
            # step 2.5 闭环列：实际喂入 planner 的路中心观测（None=本拍弃权/垃圾对）
            # + 修正后 executed/v。ro_hold=该值来自历史保持（memory/wall_occl/
            # pair_slot），planner 不把它当本拍测量——离线看"供数在跳"与
            # "执行状态在跳"必须分账。
            "road_offset": None if road_offset is None else round(road_offset, 4),
            "ro_hold": road_offset_hold,
            # YOLO×深度融合（2026-09-25）：本拍检测框数（物体掩码随帧入深度路径）
            "yolo_cars": len(result.cars),
            # 混合测距刀一（trace-only，不进决策）：距离读数+出处——验收六项
            # （回弹/churn/污染漏网/UNKNOWN 恢复/gate lead-lag/零回归）的数据源
            "dist": [
                {"id": d.target_id, "src": d.source.value,
                 "m": None if d.metric_distance_m is None
                 else round(d.metric_distance_m, 2),
                 "method": None if d.method is None else d.method.value,
                 "age": None if d.anchor_age_ms is None
                 else round(d.anchor_age_ms, 1),
                 "v": None if d.v_close_mps is None else round(d.v_close_mps, 2),
                 "bz": None if d.box_bridge_m is None
                 else round(d.box_bridge_m, 2),
                 "why": None if d.reason is None else d.reason.value}
                for d in obs.distances] or None,
            # 门控重标定供数：逐车轨框高流（与 dist 同 id 同拍 join）——
            # 轨道 id 天然绑定 h 与锚值，免离线复算的对齐/翻转/混叠三坑。
            # seen=距最后一次 2D 命中的 fid 差（0=本拍命中；>0=框已驻留，
            # 冻结框不再当新鲜观测——近场断供复盘 2026-10-22）。
            "car_h": [{"id": t.id, "h": t.h, "conf": round(t.conf, 2),
                       "seen": fid - t.last_seen_fid}
                      for t in (*obs.targets, *obs.far_targets)
                      if t.kind == KIND_CAR] or None,
            # 锚点清单对账面（20261016 归因：零锚轨损失在检测→清单→轨道之间
            # ——映射失败/worker 滞后整帧丢弃，dist.why 留不下痕迹）：条目数/
            # 映射成功数/fid 滞后/orphan，与 dist/car_h 同拍 join 即逐拍复算
            # "条目→映射→锚"漏斗；None=本拍无新清单（深度证据拍才有）
            "asheet": chain["tracker"].ingest_stats,
            # TTC 消费端准入供数（刀二）：逐车横向道/行号/接近率。与 car_h 同 id
            # join 后可拆 looming-TTC 对实际掠过时刻的系统性高估（探针
            # probe_ttc_looming：p50 +60~90%）里「估计器误差」与「回避成功的
            # 反事实差」各占多少——前者才拦换装，后者恰是安全消费端要的值。
            # 域外轨无横向数值与接近率（FarTarget 契约如此），如实落 None。
            "car_pos": [
                {"id": t.id, "x": round(t.x_lane, 3), "cy": t.cy,
                 "rel": round(t.rel_approach, 2),
                 "xl": None if t.x_lane_line is None
                 else round(t.x_lane_line, 3)}
                for t in obs.targets if t.kind == KIND_CAR] + [
                {"id": g.id,
                 "x": None if g.last_x_lane is None else round(g.last_x_lane, 3),
                 "cy": g.cy, "rel": None,
                 "xl": None if g.x_lane_line is None
                 else round(g.x_lane_line, 3)}
                for g in obs.far_targets if g.kind == KIND_CAR] or None,
            # road_offset 供数面（路心合成调试）：来源 / 归属读数
            # 与影子参考系同拍消费的是同一个快照（不是事后再取 latest——
            # 那会让 trace 的线/锚点和影子实际用的差一拍，离线无法对账）。
            "lane_obs": None if chain.get("lane_obs") is None else lane,
            # 保持期线融合可见面：trusted=深度-线帧偏置已对账、bias=帧偏置、
            # sm=线横位平滑值——判断"状态没跟上"是融合没工作还是线本身错。
            "lane_fuse": planner.lane_debug(),
            # 控制横速可见面：v_ctrl=控制律实际使用者，v_model=模型状态，
            # v_line=标线横移（直接测量）、v_head/speed=航向与在线前向速度估计
            # （speed=None 表示没估出来：v_head 不兜底，也不进控制律）。
            "lat_ctrl": planner.ctrl_debug(),
            "ro_source": chain["ego_road"].last.get("source"),
            # 走廊中心实际原点：line=线为主（深度只供边界），其余=深度来源。
            # 与 ro_source 分开：线为主时 ro_source 仍显示深度候选的侧效来源。
            "ro_origin": chain["ego_road"].last.get("origin"),
            "ro_off": None if chain["ego_road"].last.get("off") is None
            else round(chain["ego_road"].last["off"], 4),
            # 线↔深度路心家族横位分歧（道，线−深度；None=本拍无可比证据）。
            # 与 lat_ctrl.lpok 同源：lpok=False 且 ldis 持续超阈=互证闸在拦。
            "ldis": None if ldis is None else round(ldis, 3),
            # 路心互证审计列（2026-10-06）：同拍栅格 wmid 路心（off 系，右正）
            # + 新鲜对互证判定（agree/veto/bypass；复用/闸关/栅格弃权=None）。
            # ro_edge=找边对心（off 系）——master 下找边对只做审计，ro_off
            # 来自绝对链（wall/wall_occl/region/memory），两列的分离即分歧面。
            "ro_grid": None if chain["ego_road"].last.get("grid_off") is None
            else round(chain["ego_road"].last["grid_off"], 4),
            "ro_agree": chain["ego_road"].last.get("xcheck"),
            "ro_edge": None if edge_off is None else round(edge_off, 4),
            # 主候选供数门分账列（2026-10-17 归因：走廊缺席拍 9 成以上=主候选
            # 被供数门拒绝，垃圾对/对宽门/路心超限三道门未分账）：本拍原始对
            # + 对宽（二元定一对）+ W 真值 + 拒绝原因——与 ro_source 拒绝拍
            # 同拍 join 即复算三者的缺席拍占比，门标定（OFF_MAX/对宽容忍）有数。
            # 复算口径：master 的对宽门锚在规则路宽 4.0 道；回放链的判据是
            # ro_w（W=近 200 过门对的中值）配 ro_pair_w（本拍对宽）。cor_w 是
            # 走廊表示面的当拍滤波宽——拿 cor_w 比对宽门会得出假结论；被拒
            # 本拍对宽仍进走廊表示面（门只拦供数路）
            "ro_off_raw": None if chain["ego_road"].last.get("off_raw") is None
            else round(chain["ego_road"].last["off_raw"], 4),
            "ro_w": None if chain["ego_road"].last.get("width") is None
            else round(chain["ego_road"].last["width"], 4),
            "ro_pair_w": None if chain["ego_road"].last.get("pair_w") is None
            else round(chain["ego_road"].last["pair_w"], 4),
            "ro_reject": chain["ego_road"].last.get("reject"),
            # 同定义对照审计面（2026-10-27，只记录不参与任何判定/供数）：
            # ro_region = 本拍绿区读数的触窗比例与绿行数（region_reading 原值，
            # 非新证据拍/坏平面拍=None）；ro_wallbin = 墙双侧同箱且都当拍在场。
            # 用途：把"墙到墙中点"与"绿区左右缘中点"两个**不同物理定义**的读数
            # 限制在二者应当重合的条件下比较（双侧不触窗 + 同箱双侧 + 直路）。
            # 它不保证两路误差相同——只是排除已知定义差异。
            "ro_region": chain["ego_road"].last.get("audit") or None,
            # 参考系跳变标注（2026-10-27，仅记录）：ro_hop 0=运动/1=换档/
            # 2=黑视，ro_v_obs=相邻新鲜读数隐含横速（道/s，右正，黑视期
            # None），ro_gap_s=两读数间隔，ro_src_chg=来源变化（换源不等于跳变）。
            "ro_hop": None if ro_hop is None else ro_hop["hop"],
            "ro_v_obs": None if ro_hop is None or ro_hop["v"] is None
            else round(ro_hop["v"], 2),
            "ro_gap_s": None if ro_hop is None else round(ro_hop["gap"], 3),
            "ro_src_chg": None if ro_hop is None else ro_hop["src_chg"],
            # 平面尺度门（master）：0=本拍 cam_h 出物理带，缘点/绿区已被丢弃
            "ro_plane": 1 if chain["ego_road"].last.get("plane_ok", True) else 0,
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
            # dgeo_seg=新证据拍端到端分段（ms dict|None）：cap=采集回调→入队
            # （age 起点之外、此前不可见的段；内段 cap_pick/rec/det/mask/copy_ms
            # =拾取/录制/检测/掩码/入队拷贝，同钟伸缩 Σ内段≡cap，2026-10-08
            # 性能第二批插桩）、queue=入队→worker 开工、
            # pub=发布→本拍消费，与 dgeo_ms 相加≈age；stages=MoGe 四段快照。
            # 仅新证据拍记录（驻留复用拍重复无信息），见 _timing_segments
            "dgeo_seg": None if not (dgeo is not None and dgeo_new)
            else chain["depth_geo"].last_segments,
            # dgeo_slow=worker 慢 forward 锁存（>200ms，同钟 ts_ns/pre/forward）：
            # latest-only 会覆盖卡住帧的 dgeo_seg，这列保证那次尝试仍可见。
            "dgeo_slow": _slow,
            "dgeo_rejects": None if dgeo is None or not dgeo.rejects
            else ";".join(dgeo.rejects),
            # 尺度观测三列（尺度审计 2026-10-10：ms 瞬态段 1.3~1.7× 名义值静默
            # 通过平面门，先落盘可见再谈防护，只写不读）：MoGe 尺度头原始输出
            # + 全幅焦距 + 平面 c（=cam_h，名义 ~1.9m，瞬态段 3.9~5.7）。
            # 列名 dgeo_metric（2026-10-10 焦距治理修正）：曾与上方耗时列重名
            # dgeo_ms——同 dict 后键胜出，耗时列被静默覆盖。
            "dgeo_metric": None if dgeo is None or dgeo.metric_scale is None
            else round(dgeo.metric_scale, 3),
            "dgeo_focal": None if dgeo is None or dgeo.fx is None
            else round(dgeo.fx, 1),
            # 焦距求解器诊断（焦距漂移治理 2026-10-10，观测面只写不读）：
            # shift=深度偏移解、resid=逐样本平均残差（uv² 单位，跨帧可比）、
            # nsol=64×64 采样有效样本数（GPT 审查 P0：残差必须随 n 归一）
            "dgeo_shift": None if dgeo is None or not dgeo.solver
            else round(float(dgeo.solver.get("shift", float("nan"))), 4),
            "dgeo_resid": None if dgeo is None or not dgeo.solver
            else round(float(dgeo.solver.get("resid_mean", float("nan"))), 6),
            "dgeo_nsol": None if dgeo is None or not dgeo.solver
            else int(dgeo.solver.get("n", -1)),
            "dgeo_cam_h": None if dgeo is None or dgeo.coef is None
            else round(abs(dgeo.coef[2]), 3),
            # 走廊审计两列（第 6 步）：供数走廊宽 + 平面健康面——实机对账
            # 与影子重放的验收面（带来源明细在 debugview/offline 重放）。
            # cor_w=当拍滤波对宽（走廊表示面），与对宽门判据（master=规则
            # 4.0 道；回放链=ro_w 中值）不是同一量——两套量并存见 ro_w 注释
            "cor_w": None if corridor is None
            else round(corridor.width_lane, 3),
            "cor_ph": None if corridor is None else corridor.plane_health,
            # 走廊供数来源（region/pair_slot/wall/memory 等）：
            # memory/none——朝向记忆使用频次（memory 出现率）与黑视拍从这里读
            "cor_src": None if corridor is None else corridor.src,
            # 中远场粗带（地面密度水管影子期，2026-10-21）：逐带 [车头系 z0,z1,
            #   左缘, 右缘, 地面点数]——车道级消费面不读 coarse_bands，本列是
            #   影子对账数据面（找边远箱 vs 地面密度同帧对账；交接门触发率）
            "cor_far": None if corridor is None or not corridor.coarse_bands
            else [[round(b.z0, 1), round(b.z1, 1),
                   None if b.left is None or b.left.x_m is None
                   else round(b.left.x_m, 2),
                   None if b.right is None or b.right.x_m is None
                   else round(b.right.x_m, 2),
                   b.n_pts] for b in corridor.coarse_bands],
            # 中远场方向共享结果 [道, 龄ms]（新远带证据拍更新一次，其余拍
            # 共享同一值；消费端只读——验收面看它而不是逐拍重算）
            "cor_hint": None if corridor is None
            or corridor.far_hint_lane is None
            else [round(corridor.far_hint_lane, 3),
                  None if corridor.far_hint_age_ms is None
                  else round(corridor.far_hint_age_ms, 1)],
            # 深塌缩否决道集（单向认证观测列）：当拍持续深塌缩的绝对道号，
            # 闸消费在路线请求口（route.far_veto_enabled）；本列只供离线回放
            "cor_veto": None if corridor is None or not corridor.far_veto
            else sorted(corridor.far_veto),
            # 车流观测两列（阶段 C 的 C4/C5 回放数据源）：在途车数 + 本拍 pass 的 d_min
            # + 街车最大横向速率读数（噪声主导，仅供观测——见 decision.json _sources）
            # + 最近车的横向距/行号（本车道前车追尾类碰撞的回放诊断面）
            "car_views": len(tviews),
            # 奖励车源计数（漏斗观测）：0=源为空（近场+远场粗源都没进来）——
            # 区分"奖励车看不见"与"看见但没被选中"
            "cr_bonus_src": eng.bonus_src_count,
            # 车米制状态快照（横向位/纵向距/到站/接近速/锚龄，与 dist 同 id
            # 同拍 join）：机会漏点（可达门 vs 碰撞判据）的离线复算输入
            "mcars": chain["engine"].metric_cars_snap or None,
            # 存在性占用影子（只读观测）：None=本拍无占用；有值时含
            # blocked/ids/m/modes/veto/sel_*——选道影子的逐事件供数面
            "occ": chain["engine"].occ_snap(),
            # HUD 本机读数（hud_ref 挂载；rate 只在蓝卡识别=有 rate 的槽位即
            # 本机）：局结局程序化取数面（得分/得分速度），缺失拍诚实 None
            # （对手卡读数是背景假读，按槽位猜会静默读错）；血量格未读另案
            "hud_score": h_score, "hud_rate": h_rate,
            "car_vlat": None if not tviews else round(
                max(abs(v.v_lat) for v in tviews), 3),
            "near_dx": None if not tviews else round(
                min(abs(v.x_lane - planner.state.executed_lane) for v in tviews), 2),
            "near_cy": None if not tviews else min(v.cy for v in tviews),
            # passes 带 track_id：决策拍 target_id 与 pass track_id 同 id 空间，
            # 跨拍 join 即可把结局归因到具体那辆车（米制决策价值验证的归因键；
            # 2026-10-07 前的旧 trace 此列为纯浮点列表，离线分析按类型兼容）
            "passes": [{"id": e.track_id, "d_min": round(e.d_min, 3)}
                       for e in tevents if e.outcome == OUTCOME_PASS],
            "fresh": obs.health.frame_fresh, "geom": obs.health.geometry_valid,
            "presence": obs.health.target_presence,
            # 原始检出分类计数：目标流枯竭判据只认车；这两列用来区分
            # “空街”与“有币但没跟踪”（2026-10-20 阶段 1 误冻结归因）。
            "raw_cars": obs.health.raw_cars,
            "raw_coins": obs.health.raw_coins,
            "raw_bonuses": obs.health.raw_bonuses,
            # oage：本拍 2D 帧龄（frame_with_age 抓取时现算，拍级新鲜）——
            # 决策时刻米制真实年龄 ≈ dist[].age（锚龄，零点=深度帧采集）+
            # oage（零点=2D 帧采集）；两零点同钟族（perf_counter_ns），相加
            # 无重复计账。WGC 重复帧拍 obs 整体冻结，本列不受影响（拍级）。
            "oage": round(age_ms, 1)}
        # C 相关事件标记（只记账，阶段收尾据此冻结证据）：路线相位/定案分歧、
        # 选择影子分歧/同输入不一致、换档读数上的完成。
        for _kind, _detail in _c_events(eng, position_evidence):
            _note_c_event(chain, _kind, fid, ts_ns, **_detail)
        chain["trace"].append(_row)
        _ts = chain.get("trace_sink")
        if _ts is not None:
            _ts.append(_row)
        # 调试图快照（控制局=debugview 节流写盘；人工局=交录制器渲染叠加图）
        dbg = chain.get("debug")
        hud_rec = chain.get("hud_rec")
        if dbg is None and hud_rec is None:
            return
        # 人工对照局（影子拍 gpad=None）读物理手柄实际杆量：调试图底部
        # 输入带用它和程序反事实杆值同屏，直接判「车是人打歪还是程序带歪」。
        # 自动局不读——那时游戏输入是虚拟手柄下发值，物理手柄不参与。
        pad = None
        if gpad is None:
            pad_st = xinput.read_first_connected()
            if pad_st is not None:
                pad = {"lx": int(pad_st.left_stick[0]),
                       "ly": int(pad_st.left_stick[1]),
                       "rt": int(pad_st.right_trigger)}
        snap_dbg = {
            "fid": fid, "ts_ns": ts_ns, "age_ms": round(age_ms, 1),
            "phase": phase,
            "result": result, "reading": dgeo, "grid": dgrid,
            "lane": lane,               # 线观测（叠加回画面 + 底部摘要）
            "pad": pad,                 # 物理手柄实际杆量（人工局）
            "road_offset": road_offset,
            "grid_off": chain["ego_road"].last.get("grid_off"),
            "corridor": corridor,   # 分带走廊（debugview 逐带来源叠层）
            "selection": eng.selection_report,   # 本拍四层选择
            "sel_objects": eng.selection_objects,  # 选择器眼里的机会
            "sel_debug": eng.selection_debug,      # 选择帧快照
            "scene": eng.scene,                    # 场景快照（motion 输入）
            "plan": chain.get("last_plan"),        # 本拍轨迹计划
            "decision": self._control_last,   # 本拍决策/供数快照（新建不改旧）
            "hud": (chain["hud_ref"].latest()
                    if chain.get("hud_ref") is not None else None),
            "n_groups": len(obs.coin_groups)}
        if dbg is not None:
            dbg.tick(frame, snap_dbg)
        if hud_rec is not None:
            # 快照只交给录制器按帧号取；主循环不再改这个 dict
            chain["last_snap"] = snap_dbg
            chain["last_snap_fid"] = fid

    def _straight_tick(self, gpad, fid: int, ts_ns: int, chain: dict,
                       throttle_raw: int) -> None:
        """直行基线一拍：方向归零、油门恒踩（V0 语义：决策禁动恒 x_target=0）。

        感知与深度都不进本路径——基线要度量的正是"无驾驶智能"的地板收益
        （control-route 验收②的分母）。trace 记时间轴与常量列，另带本拍 HUD
        读数：战绩库与局汇总都从这列取末分，直行局因此也有得分。
        """
        gpad.left_joystick(x_value=0, y_value=0)
        gpad.right_trigger(value=throttle_raw)
        gpad.update()
        h_score, h_rate = _ego_hud_readout(chain.get("hud_ref"))
        _note_hud_scores(chain)
        row = {"fid": fid, "ts_ns": ts_ns, "state": "STRAIGHT",
               "x_target": 0.0, "steer_x": 0, "throttle": throttle_raw,
               "hud_score": h_score, "hud_rate": h_rate}
        chain["trace"].append(row)
        ts = chain.get("trace_sink")
        if ts is not None:
            ts.append(row)

    def _flush_control_trace(self, chain: dict, phase: int, *,
                             completed: bool = False) -> None:
        """阶段收尾：流水已随拍进汇，这里落局汇总与回合战绩。

        局汇总随日志开关（与流水同家）；战绩库独立落盘，人工/宏由
        ``chain['record_round']`` 排除。两条落盘各自兜底，互不阻断对局收尾。
        """
        rows = chain.get("trace") or []
        if not rows:
            return
        stats = dashboard_store.summarize_trace_rows(rows)
        self._record_round_phase(chain, phase, stats, completed)
        try:
            self._write_game_summary(chain, phase, rows, stats=stats)
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：控制 trace {len(rows)} 拍"
                  f"（流水汇{'会话内' if self._trace.stats()['token'] else '未开写盘'}）",
                  "INFO")
            bf = chain.get("bad_frames") or {}
            if bf.get("saved"):
                _tlog(self,
                      f"[极速狂飙] 驾驶阶段 {phase}：坏帧（双侧路缘皆不稳）采样 "
                      f"{bf['saved']} 张 → {bf['dir']}", "INFO")
        except Exception as exc:  # noqa: BLE001 —— trace 落盘失败不阻断对局收尾
            _tlog(self, f"[极速狂飙] 驾驶阶段 {phase}：控制 trace 落盘失败: {exc!r}", "WARNING")

    @staticmethod
    def _cfg_view(chain: dict) -> dict | None:
        """冻结用的配置快照；不可序列化就放弃——配置缺失不阻断证据保存。"""
        cfg = chain.get("cfg")
        if cfg is None:
            return None
        try:
            return asdict(cfg)
        except (TypeError, ValueError):
            return None

    def _archive_c_evidence(self, chain: dict, phase: int) -> None:
        """C 相关事件证据冻结（logs 之外的 evidence 目录）。

        失败必须显式告警：归档触发不等于素材已保存。日志写盘未开时没有源
        文件，同样按失败处理——不能让"以为存了"进入后续分析。
        """
        ev = chain.get("c_evidence") or {}
        root = logger.session_dir
        if root is None:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：C 事件 {ev.get('first')} "
                  "但日志写盘未开，无素材可冻结", "WARNING")
            return
        res = archive_session_evidence(root, phase, ev, config=self._cfg_view(chain))
        if res["ok"]:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：C 事件证据已冻结 → "
                  f"{res['trace']}（{res['bytes']} B）", "INFO")
        else:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：C 事件证据冻结失败: "
                  f"{res['error']}", "WARNING")

    def _record_round_phase(self, chain: dict, phase: int,
                            stats: dashboard_store.PhaseStats,
                            completed: bool) -> None:
        """自动控制局的阶段结果写战绩库；人工/宏不写（record_round=False）。

        与日志、debug 两个开关解耦：只要 record_round 为真就落库；失败只记
        WARNING，不阻断对局收尾。对手分取本阶段最后一对可信读数。
        """
        if not chain.get("record_round") or not chain.get("round_stamp"):
            return
        try:
            t_start = chain.get("t_start")
            started = (
                datetime.fromtimestamp(t_start).isoformat(timespec="seconds")
                if t_start else datetime.now().isoformat(timespec="seconds")
            )
            opponent = dashboard_store.pick_opponent(
                chain.get("hud_pairs"), stats.score)
            err = dashboard_store.write_phase(
                session_token=getattr(logger, "session_token", None) or "",
                round_stamp=str(chain["round_stamp"]),
                round_no=int(chain.get("round_no") or 1),
                drive_mode=str(chain.get("drive_mode") or "unknown"),
                phase=phase,
                stats=stats,
                completed=completed,
                opponent=opponent,
                started_at=started,
                ended_at=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as exc:  # noqa: BLE001 —— 落库失败不阻断对局收尾
            err = repr(exc)
        if err:
            _tlog(self,
                  f"[极速狂飙] 驾驶阶段 {phase}：回合战绩落库失败: {err}",
                  "WARNING")

    def _write_game_summary(self, chain: dict, phase: int, rows: list[dict],
                            *, stats: dashboard_store.PhaseStats | None = None) -> None:
        """局汇总一行/阶段，追加写会话目录 ``trace/speedrush_summary.jsonl``。

        消费者=局级验收/A/B：得分/得分速度/时长/事件计数程序化取数。
        血量格 HudObserver 未读、胜负=回合结束屏，均不在此列。失败只记
        WARNING——汇总缺失不该中止收尾。"""
        try:
            if stats is None:
                stats = dashboard_store.summarize_trace_rows(rows)
            root = logger.session_dir
            if root is None:
                return      # 日志记录开关未开：无会话目录，汇总不落（与流水同闸）
            root = root / "trace"
            root.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": time.strftime("%Y%m%d_%H%M%S",
                                    time.localtime(chain["t_start"])),
                "phase": phase, "ticks": stats.ticks, "score": stats.score,
                "rate": stats.rate, "dur_s": stats.dur_s,
                "conserve_segments": stats.conserve_segments,
            }
            with open(root / "speedrush_summary.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 —— 汇总缺失不阻断收尾
            _tlog(self, f"[极速狂飙] 驾驶阶段 {phase}：局汇总落盘失败: {exc!r}", "WARNING")

    def _stop_depth_observer(self, chain: dict, phase: int) -> None:
        """停深度 worker + 阶段出口报一次健康——不达标报警的落点（判据移植 treasure）。

        回归只发生在尾部，故判据用比值与 p95、不用均值：stale 丢弃率 ≥5% 或
        age p95 超预算 75% → WARNING；push 了却整场零结果（worker 没产出，
        实机 0/330 那类症状）→ WARNING；其余 INFO 汇总一行。
        """
        lw = chain.get("lane_obs")
        if lw is not None:
            lw.stop()
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
                f"/超龄丢弃 {h['stale_drops']}（{ratio:.0%}）"
                f"/首次消费即超龄 {h['stale_new_drops']}"
                f"/异常 {h['failures']}"
                f"/让锁跳帧 {h['busy_skips']}"
                f"/时效 p50 {h['age_p50']:.0f} p95 {h['age_p95']:.0f}ms"
                f"（预算 {h['max_age_ms']:.0f}ms）"
                f"/耗时 p50 {h['dur_p50']:.0f} p95 {h['dur_p95']:.0f}ms")
        if h.get("forward_p50"):
            line += (f"/分段 p50 前向 {h['forward_p50']:.0f} 找边 {h['edges_p50']:.0f}"
                     f" 栅格 {h['grid_p50']:.0f}ms")
        bad = (not examined) or ratio >= 0.05 or h["age_p95"] > h["max_age_ms"] * 0.75
        _tlog(self, line, "WARNING" if bad else "INFO")

    def _ensure_perception(self, soft: bool = False) -> StreetPerception | None:
        """懒加载感知模型（实例跨阶段复用，一局只付一次模型加载）；失败则本轮禁用。

        失败不阻断驾驶流程（与录制器同一姿态：采集/观测件的故障不碰主循环）。
        ``soft=True`` 只用于后台预加载：失败不置 failed、不封死本轮，留给驾驶
        阶段的懒加载重试。锁保证预加载线程与驾驶线程不会各构造一份模型。
        """
        with self._perception_lock:
            if self._perception is not None or self._perception_failed:
                return self._perception
            try:
                t0 = time.monotonic()
                path, err = model_assets.select(PERCEPTION_MODEL_REL)
                if path is None:
                    raise RuntimeError(err)
                self._perception = StreetPerception(str(path))
                _tlog(self, f"[极速狂飙] 感知模型就绪（{time.monotonic() - t0:.1f}s）",
                      "INFO")
            except Exception as exc:  # noqa: BLE001 —— 感知故障不阻断对局流程
                if soft:
                    _tlog(self, f"[极速狂飙] 感知预加载失败，驾驶阶段重试: {exc!r}",
                          "DEBUG")
                    return None
                self._perception_failed = True
                _tlog(self, f"[极速狂飙] 感知初始化失败，本轮禁用感知: {exc!r}",
                      "WARNING")
                return None
            return self._perception

    def _ensure_depth_session(self, soft: bool = False):
        """懒加载深度几何的 ORT 会话（模式同 _ensure_perception）。

        权重来自可选模型包（用户数据目录优先，安装包兜底；缺失/加载失败时深度层
        禁用、road_offset 走纯模型积分（几何主人缺席=无供数，不回退 2D——
        黄线链路已随 v4 换装整体退役）。``soft`` 语义同感知预加载。
        """
        with self._depth_lock:
            if self._depth_sess is not None or self._depth_failed:
                return self._depth_sess
            try:
                t0 = time.monotonic()
                path, err = model_assets.select(DEPTH_MODEL_REL)
                if path is None:
                    raise RuntimeError(err)
                self._depth_sess = load_session(path)
                _tlog(self, f"[极速狂飙] 深度几何模型就绪（{time.monotonic() - t0:.1f}s）",
                      "INFO")
            except Exception as exc:  # noqa: BLE001 —— 深度故障不阻断对局流程
                if soft:
                    _tlog(self, f"[极速狂飙] 深度模型预加载失败，驾驶阶段重试: "
                                f"{exc!r}", "DEBUG")
                    return None
                self._depth_failed = True
                _tlog(self, f"[极速狂飙] 深度几何初始化失败，road_offset 退纯模型积分"
                            f"（权重: {model_assets.pack_path(DEPTH_MODEL_REL)}）: {exc!r}",
                      "WARNING")
            return self._depth_sess

    # ---------- 模型预加载（UI 阶段空档，2026-10-23） ----------

    PRELOAD_DELAY_S = 0.5   # 先让第一拍 UI 识别跑起来，再开加载
    PRELOAD_GAP_S = 0.3     # 两个模型之间让出 CPU（顺序加载，不并发放大占用）

    def _preload_wanted(self) -> bool:
        """直行档与宏模式都不加载感知/深度；录制且不感知时也不需要。"""
        if self._drive_mode == "macro":
            return False
        return (self._drive_mode != "straight"
                and (self._control_mode or self._perception_mode))

    def _start_preload(self) -> None:
        """启动一次后台预加载；重复调用/不需要时直接返回（不建第二个线程）。"""
        if self._preload_started or not self._preload_wanted():
            return
        self._preload_started = True
        th = threading.Thread(target=self._preload_models,
                              name="speedrush-model-preload", daemon=True)
        self._preload_thread = th
        th.start()

    def _preload_models(self) -> None:
        """顺序加载模型；任何异常都只记日志，不封死懒加载。

        深度模型只在控制模式需要（录制+感知不建控制链）；记录模式只预加载
        感知，避免白占一份模型内存。
        """
        try:
            time.sleep(self.PRELOAD_DELAY_S)
            if self._preload_stop.is_set():
                return
            if self._control_mode:
                self._ensure_depth_session(soft=True)
                time.sleep(self.PRELOAD_GAP_S)
                if self._preload_stop.is_set():
                    return
            self._ensure_perception(soft=True)
        except Exception as exc:  # noqa: BLE001 —— 预加载失败不阻断对局
            _tlog(self, f"[极速狂飙] 模型预加载异常（驾驶阶段重试）: {exc!r}",
                  "DEBUG")
        finally:
            self._preload_thread = None
            self._preload_done.set()

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
            # 等锁/run 分段：环频瓶颈归因的直接判据（等锁大→调度问题；
            # run 大→推理本身慢，调度层无解）
            for label, xs in (("等锁", self._wait_times), ("run", self._run_times)):
                if not xs:
                    continue
                srt = sorted(xs)
                n = len(srt)
                p50 = srt[n // 2]
                p95 = srt[min(n - 1, int(round(0.95 * (n - 1))))]
                _tlog(self,
                    f"[极速狂飙] 驾驶阶段 {phase}：感知{label}分段 "
                    f"n={n} P50={p50:.1f}ms P95={p95:.1f}ms", "INFO")
        if self._pace_body:
            for label, xs in (("环体", self._pace_body), ("睡眠", self._pace_sleep),
                              ("睡醒空隙", self._pace_wake)):
                srt = sorted(xs)
                n = len(srt)
                p50 = srt[n // 2]
                p95 = srt[min(n - 1, int(round(0.95 * (n - 1))))]
                _tlog(self,
                    f"[极速狂飙] 驾驶阶段 {phase}：环拍分解-{label} "
                    f"n={n} P50={p50:.1f}ms P95={p95:.1f}ms", "INFO")
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

    def _begin_recording(self, phase: int, round_no: int,
                         stamp: str | None = None) -> DriveRecorder | None:
        """建会话目录并启动录制器；失败返回 None（录制失败不该中止对局）。

        程序叠加图（BEV/决策叠层）与调试图同一闸：debug 开关开时注入渲染器，
        复用的是控制链同一个渲染函数；关时只录原帧与手柄。"""
        try:
            # manual 标签标出人工驾驶语料（录制模式必然人驾）；目录名与自动阶段同规则：
            # <局前缀>_manual_p<阶段>。局前缀按局生成，同一局两阶段共用、不同局不同。
            stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
            session = make_session_dir(_record_root(), tag="manual", stamp=stamp)
            # 阶段后缀：一局两个驾驶阶段各成一个会话，便于按阶段筛数据
            session = session.with_name(f"{session.name}_p{phase}")
            session = _fresh_session_dir(session)
            hud_render: Callable[[Any, dict], Any] | None = None
            if self._debug_enabled():
                cal = load_calib()

                def _render_hud(frame, snap):
                    return render_hud(frame, snap, cal)

                hud_render = _render_hud
            rec = DriveRecorder(session, phase=phase, round_no=round_no,
                                group_id=getattr(self._log_grp, "id", None),
                                hud_render=hud_render,
                                hud_interval_s=DEBUG_INTERVAL_S)
            rec.start()
        except Exception as exc:  # noqa: BLE001 —— 采集失败不阻断流程
            _tlog(self, f"[极速狂飙] 录制器启动失败: {exc!r}", "WARNING")
            return None
        self._recorder = rec
        return rec

    def _begin_hud(self, out_dir: Path | None, phase: int, round_no: int,
                   *, persist: bool = True) -> HudObserver | None:
        """起 HUD 读数观察线程；失败返回 None（读数失败不该中止对局）。

        取帧源同一（``frame_with_age``，fid/ts_ns 对齐链路不变）。``persist``
        只决定要不要落 ``hud.jsonl``/meta：人工局写录制会话目录；控制两档在
        debug 开时写 ``_run_dir`` 的 ``hud/``，debug 关时只读内存——战绩库的
        比分与对手分不随 debug 开关丢失。
        """
        assert self.ctx is not None
        try:
            obs = HudObserver(
                out_dir,
                frame_source=self.ctx.capture.frame_with_age,
                phase=phase,
                round_no=round_no,
                group_id=getattr(self._log_grp, "id", None),
                persist=persist,
            )
            obs.start()
        except Exception as exc:  # noqa: BLE001 —— 读数启动失败不阻断流程
            _tlog(self, f"[极速狂飙] HUD 读数启动失败: {exc!r}", "WARNING")
            return None
        self._hud = obs
        return obs

    def _begin_macro_recording(self, phase: int,
                               stamp: str | None = None) -> MacroRecorder | None:
        """建宏组目录并启动宏录制；失败返回 None（该阶段降级直行）。

        宏落在 ``data/speedrush/macros/<局前缀>/``，与深度录制的 ``debug/`` 分家：
        它是用户资产，不是可随手清的调试产物。
        """
        assert self.ctx is not None
        ctx = self.ctx   # 闭包按局部名读：assert 收窄对 lambda 不生效
        try:
            stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
            group = _macro_group_dir(stamp)
            rec = MacroRecorder(
                group,
                phase=phase,
                group_id=getattr(self._log_grp, "id", None),
                foreground=lambda: ctx.window_foreground,
            )
            rec.start()
        except Exception as exc:  # noqa: BLE001 —— 采集失败不阻断流程
            _tlog(self, f"[极速狂飙] 宏录制启动失败: {exc!r}", "WARNING")
            return None
        self._macro_rec = rec
        return rec

    def _begin_hud_clock(self, phase: int, round_no: int, *,
                         with_scores: bool = False) -> HudObserver | None:
        """只读内存的 HUD 时钟（不落盘）。

        ``with_scores=False`` 只读倒计时：宏录制用它对齐 g0；``True`` 读完整
        HUD：宏回放除计时外还要双方比分，作为战绩库的阶段结果。
        """
        assert self.ctx is not None
        try:
            timer = load_hud_regions().get("timer")
            if timer is None:
                raise ValueError("HUD 区域真源缺少 timer")
            obs = HudObserver(
                None,
                frame_source=self.ctx.capture.frame_with_age,
                phase=phase,
                round_no=round_no,
                regions=None if with_scores else {"timer": timer},
                persist=False,
                group_id=getattr(self._log_grp, "id", None),
            )
            obs.start()
        except Exception as exc:  # noqa: BLE001 —— 时钟不可用只降级，不阻断
            _tlog(self, f"[极速狂飙] 宏计时读数启动失败（回放退化为纯墙钟）: {exc!r}",
                  "WARNING")
            return None
        return obs

    @staticmethod
    def _hud_clock_row(observer) -> tuple[float, int] | None:
        """从 HUD 观察者取最新可信计时：``(剩余秒, 帧时戳)``；无可用样本返回 None。"""
        if observer is None:
            return None
        row = observer.latest()
        if not row:
            return None
        entry = (row.get("fields") or {}).get("timer") or {}
        value = entry.get("value")
        ts_ns = row.get("ts_ns")
        if not entry.get("trusted") or value is None or not ts_ns:
            return None
        return float(value), int(ts_ns)


def _record_root() -> Path:
    """录制语料根目录：``debug/speedrush/``（与调试产物同家）。

    与安装目录解耦、更新不丢数据，也不污染仓库工作树。人工驾驶阶段只产一个
    会话目录（``<局前缀>_manual_p<阶段>``）：原帧、叠加图、索引、HUD 读数与深度
    证据同家（2026-10-30 起与控制局共用 ``session_layout`` 一套布局）。宏等用户
    资产单独落 ``data/speedrush/macros/``，不混进调试目录。
    """
    return debug_dir() / "speedrush"


# 坏帧采样器（三局复盘 2026-09-22 悬案取证：sides==0 占 58% 是场景褪色还是检测参数，
# 唯一证据=真帧；录制与控制互斥是既有安全不变量，不动它——由控制回路自己按节流存帧，
# fid 与 trace jsonl 对账。热路径纪律：≥2s 一张、每阶段封顶，写失败即自禁不重试刷屏）
# 线为位置原点的检测龄上限（ms）：更旧的线值只能做运动观测，不能当位置。
LANE_POS_AGE_MS = 200.0


class _LaneAgreeGate:
    """线位置权威的互证闸（2026-11-02，223948 局货车段实证）。

    大车遮挡会让标线整体错位（左线被挡→最左可见线被当左线，横位读数
    偏 1.3 道，帧证），而线的可用门只看条数与新鲜度，拦不住。本闸拿
    线的新鲜横位与深度路心家族（region/pair/memory，不含 wall——8750
    拍错在 wall）比：持续分歧超阈暂停线的位置权威（lane_pos_ok=False，
    规划器退深度修正路径，其中已有 0.8 新息门与陈旧重基消化噪声）；贴
    回（滞回）才恢复。只动位置证据门，不动走廊原点——深度全断时线原
    点仍供走廊存在（中心=执行位），黑视纪律不变。"""

    ON_LANE = 0.7      # 暂停阈值（道；健康局 p90 0.38、病态段持续 0.75+）
    ON_TICKS = 5       # 持续分歧拍数
    OFF_LANE = 0.5     # 恢复阈值（滞回）
    OFF_TICKS = 3      # 持续贴回拍数

    def __init__(self) -> None:
        self._dis_t = 0        # 连续超阈分歧拍数
        self._vetoed = False   # 线位置权威当前被暂停
        self._agree_t = 0      # 暂停中连续贴回拍数

    def update(self, diff: float | None) -> bool:
        """喂本拍分歧（线横位 − 深度家族横位，道）；None=本拍无可比证据，
        计数冻结。返回本拍线位置权威是否成立。"""
        if diff is None:
            return not self._vetoed
        if not self._vetoed:
            if abs(diff) > self.ON_LANE:
                self._dis_t += 1
                if self._dis_t >= self.ON_TICKS:
                    self._vetoed = True
                    self._dis_t = 0
                    self._agree_t = 0
            else:
                self._dis_t = 0
        else:
            if abs(diff) <= self.OFF_LANE:
                self._agree_t += 1
                if self._agree_t >= self.OFF_TICKS:
                    self._vetoed = False
                    self._agree_t = 0
                    self._dis_t = 0
            else:
                self._agree_t = 0
        return not self._vetoed
YOLO_MASK_MARGIN = 10             # 检测框外扩（px）：框缘深度过渡带不留在候选区
BAD_FRAME_MIN_INTERVAL_S = 2.0
BAD_FRAME_MAX_PER_PHASE = 40


def _yolo_object_mask(result: PerceptionResult) -> np.ndarray | None:
    """YOLO 检测框（车/金币/奖励，外扩 YOLO_MASK_MARGIN）→ 布尔掩码；
    无检测返回 None（深度路径零成本）。目标架构：物体身份由语义说，
    深度块在源头让位（穿车读墙假缘不再产生）。剃本体（2026-10-09）：
    矩形掩码只是候选区，depth_geo 在消费端按 hgt 刀只挖矩形内高出路面的
    本体点（OBJ_KNIFE_HGT），矩形内路面像素留在找边/栅格（格子不再饿死）。"""
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


def _yolo_car_boxes(result: PerceptionResult) -> tuple | None:
    """车框清单（**原始框**，不外扩）→ 深度侧逐框锚点度量（混合测距刀一）。
    刀一只收车（刚体 looming 最纯；币/奖励不进锚点契约，设计稿 §5）。序号
    = result.cars 内下标，与 Tracker 的 det_key (KIND_CAR, i) 对齐——深度域
    只见 (fid, det_idx)，归轨道是 Tracker 的事。无车 → None（零成本路径）。"""
    boxes = tuple(
        (i, int(d.cx - d.w / 2), int(d.cy - d.h / 2),
         int(d.cx + d.w / 2), int(d.cy + d.h / 2))
        for i, d in enumerate(result.cars))
    return boxes or None


def _maybe_save_bad_frame(chain: dict, frame, dgeo, fid: int,
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
            s["dir"] = chain["run_dir"] / "badframes"
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


class _EdgePtsView:
    """update() 喂墙实体的鸭子面：原始 (side, zc, x_m) 序列包成 edge_pts。"""

    __slots__ = ("edge_pts",)

    def __init__(self, pts) -> None:
        self.edge_pts = pts


class _NoPlaneView:
    """平面尺度门拒绝帧的鸭子面：坏平面上的缘点/绿区整帧不进走廊表示。

    中心走墙/记忆保持，分带边界落空气墙（center±W/2）；coef=None 让
    plane_health=degraded，坏帧不冒充健康几何。

    ``sides``/``left_edge_lane``/``right_edge_lane`` 与 DepthRoadReading 同形，
    恒取"没有可信对"（0/None）——本视图不供对读数，供数链走墙/记忆。"""

    __slots__ = ("edge_pts", "coef", "rejects",
                 "sides", "left_edge_lane", "right_edge_lane")

    def __init__(self) -> None:
        self.edge_pts = ()
        self.coef = None
        self.rejects = ("平面尺度门拒绝",)
        self.sides = 0
        self.left_edge_lane: float | None = None
        self.right_edge_lane: float | None = None


class _EgoRoadObserver:
    """分带走廊供数（第 6 步统一供数路，2026-10-09）。

    planner 契约不变：``road_offset`` = **自车相对路心的位置**（车道单位，
    右正）= −corridor.center_lane，与旧 off=−(左缘+右缘)/2 同式——跨层极性
    契约测试（test_cross_layer_polarity_and_mirror）门禁不豁免。

    **master（生产）＝绝对路心链**：所有读数都在自车系，上一拍的读数在自车
    横移后就不再成立——复用相对读数当路心正是「车把走廊带偏」的根因（实拍
    trace 365~376：车贴左黄线时 pair_slot/memory 报路心 0，墙实体 −1.75 道，
    目标横摆 ±6m）。绝对路心只认物理边界与宏观先验：

    - line（2026-10-31 起为主）：车道线严格可用时，走廊中心由线给出
      （``center_override``，等于 −执行层横位）；深度只供边界/宽度，来源
      跳变不再把中心整道拽走。线断供时深度按下面的链重建。
    - wall：双侧同箱新鲜在场且宽与规则路宽相容 → 中点 + 墙到墙宽。**单侧
      墙不再反推中心**（只作该侧边界）；双侧宽矛盾 → 诚实弃权（不猜哪一侧
      锁错结构），交绿区/记忆。
    - region：绿区物理边界（区弃权/墙未成形时的双侧宏观读数）。
    - memory：只存绝对来源（wall/region）成功拍，原值保持；TTL 0.25s 内
      桥接极短遮挡，并按当拍横速做位移补偿，过期诚实 None。pair/pair_slot
      永不进 master 路心。
    - 门禁：平面尺度门（坏平面整帧不进供数）、来源跳变门（>0.4 道需连续
      2 拍同向一致）、|center|>物理横位上限（policy.validate.x_lane_abs_max）、
      双侧宽偏离规则路宽超 0.7 道。

    off/veto 为回放对比保留旧链（pair → wall → pair_slot → region → memory），
    逐位不复用 master 的门禁；W 统计口径也按链分流（master 锚规则先验，
    回放链保持旧相对门），共享的只有样本窗与 debug 数据面。

    **拒绝与缺席分两条路**：候选对被门禁拒绝（垃圾/污染/互证否决）=当帧证据
    坏，诚实弃权不兜底（宁弃不喂假路心）；候选缺席（单侧/无效/复用拍过期）
    才走候选链——单侧墙 + W 先验把 sides=1 型从全盲变半盲供数。
    """

    SLOT_TTL_S = 0.4                 # 有效对保鲜预算（仅回放对比链）
    # 路宽 W 统计（阶段二 §一.3 兜底界）：双侧物理读数宽 (R−L) 的中位数，
    # 车道单位。master 只认双侧墙箱宽与绿区区宽（同一物理语义，不混尺）；
    # 回放链仍吃找边对宽。W 是单侧墙定位走廊中心的先验（wall 路径的 W/2）。
    #
    # 先验是规则事实（RULES：全赛道固定 4 条等宽车道，路肩不额外占宽），
    # 不是从读数里估出来的：墙到墙在车道单位下恒为 4.0 道，与米制标定的
    # 绝对误差无关（分子分母同一把尺）。宽门必须锚在它上面——早期版本用
    # 「学习中位数 ±20%」当门，先验一旦被近场窄读数拖到 3.3，真 4.0 读数
    # 反而全被判污染，2.6 道的假缘继续喂数，形成自锁死循环。
    W_PRIOR_LANE = 4.0
    # 门宽按实测定：三局 wall 双侧宽中位 3.79~4.06 道、p10~p90 约 3.3~4.45
    # （逐局系统偏置最高 0.25 道）。±0.35 会拒 14~28% 合法读数；±0.7 实测
    # 拒绝率 3.3%/3.3%/14.9%——第三局的低尾 3.0~3.3 道是右缘锁白虚线/尺度
    # 压缩段（同帧深度双侧宽同步缩水，中心也随之偏移），本就该拒。
    W_GUARD_LANE = 0.7
    W_CENTER_STEP_LANE = 0.15        # 相邻新证据拍中点位移上限（实测正常 <0.05 道）
    W_MIN_SAMPLES = 30               # 成形所需最少对宽数（master 不足时用先验）
    W_KEEP_SAMPLES = 200             # 滑动样本窗上限（路型缓变，旧样本滚出）
    # 回放链（off/veto）仍用旧的相对门，逐位保持旧行为；master 不用。
    W_GUARD_FRAC = 0.2               # 对宽超出 W±此比例 → 污染
    # W 成形地板（道，仅回放链）：保险杠近场视锥裁切/假缘会给出 2.5~2.7 道的
    # "窄走廊"——不能让它定义整局路宽先验。
    W_MIN_LANE = 3.0
    # 绝对路心记忆（2026-10-31 起线为主）：记忆只桥接「线也断供」的极短窗口。
    # 0.8s 太长——实测一次变道 1.4 道/s，0.8s 后旧值已错 1 道以上，成了
    # memory↔单侧墙来回跳的种子；缩到 0.25s，并按当拍横速做位移补偿
    # （自车右移 Δ → 路心在自车系左移 Δ）。记忆保持最后采信值、不做 EMA。
    MEM_TTL_S = 0.25
    # 平面尺度门（master）：2026-10-29 尺度修正后 bumper 机位 cam_h≈1.26~1.29m；
    # 离线 0200 帧拟合出 0.81m（正常 1.26~1.29）而质量门未拦——坏平面的缘点/
    # 绿区整帧不进供数。
    PLANE_CAM_H_LO = 1.10
    PLANE_CAM_H_HI = 1.60
    # 来源切换/单帧跳变门：绝对路心帧间跳超 0.4 道视为可疑，连续 2 拍同向
    # 一致才采信（一帧野墙/换源不把目标拽飞）。正常变道/弯道每拍 <0.1 道。
    JUMP_TOL_LANE = 0.4
    JUMP_CONFIRM = 2
    # 遮挡墙的位置是最后一次观测时的自车系坐标（无运动补偿）：只允许近龄
    # 遮挡墙单独定位中心（16 拍 ≈ 0.8s @20Hz），更老的只留置信不复用位置。
    OCCL_MAX_AGE_FIDS = 16
    # 中远场方向共享滤波（2026-10-21）：只在**新远带证据**更新一次 EMA，
    # 其余拍共享同一结果——所有消费端读同一个值，不再各自去抖。深度证据
    # 约 10Hz，α=0.5 的时间常数约一个证据间隔（~0.1s），不叠决策拍延迟。
    FAR_HINT_ALPHA = 0.5

    def __init__(self, xcheck=None, lane_w_m: float | None = None,
                 wall_track=None, mem_fallback: bool = False,
                 far_ground_supply: bool = True,
                 off_max: float = LANE_OFF_MAX) -> None:
        self._xc = xcheck            # GridXcheck | None（None/闸关=互证不参与供数）
        self._lane_w_m = lane_w_m
        # 物理横位上限（道）：车心离路心不可能超过路缘；生产值来自
        # policy.validate.x_lane_abs_max，与线观测的深度锚门同一条尺。
        self.OFF_MAX = float(off_max)
        self._wt = wall_track        # WallTrack | None（None=无墙实体路径）
        self._mem_fallback = mem_fallback  # 朝向记忆兜底（闸开才生效）
        self._far_ground_supply = far_ground_supply  # 中远场粗带（coarse_bands）闸
        self._far_extents: tuple = ()    # 本拍同帧地面横向范围（update 传入）
        self._far_hint_pending = False   # 新远带证据待消费（滤波只在此时更新）
        self._far_hint: float | None = None   # 共享方向结果（EMA，道，右正）
        self._far_hint_t: float | None = None
        self._far_veto_runs: dict[int, int] = {}  # 深塌缩游程（道号→连续拍数）
        self._far_veto: frozenset[int] = frozenset()  # 持续深塌缩否决道集
        self._xc_streak = 0          # 连续互证 veto 计数（达 bypass 预算放行）
        self._master = xcheck is not None and xcheck.mode == "master"
        # 本拍是否 master 链（W 口径按链分流；update 每拍按模式重写）
        self._slot: dict[str, tuple] = {}     # 最近有效对缓存 (el, er, 时刻)
        self._win: deque = deque(maxlen=3)    # 近 3 对原始读数（中值滤波窗口）
        self._w_samples: deque = deque(maxlen=self.W_KEEP_SAMPLES)
        self._w_center: float | None = None   # 最近一次喂宽读数拍的中点（跳变确认）
        self._center_override: float | None = None  # 线为原点时的走廊中心
        # 覆盖前的独立深度读数的**自车横位**（右正，与线观测 x=k−L、road_offset
        # 同号）；走廊 center 是路心相对自车，取反后才对齐线观测的锚点口径。
        self.depth_anchor_ego: float | None = None
        self._v_lat = 0.0                     # 当拍横速（记忆位移补偿用）
        self._mem: tuple | None = None        # 朝向记忆 (center, width, 时刻)
        self._last_center: float | None = None   # 最近一次采信的路心（跳变门基线）
        self._jump_pending: tuple | None = None  # 待确认的跳变 (center, 连续拍数)
        self._frame_gate_used = False         # 本拍跳变门只判一次（候选链共享）
        self._plane_ok = True                 # 本拍平面尺度门（master）
        # 同定义对照审计面（2026-10-27，只记录不参与判定）：绿区触窗与绿行数
        # （`region_reading` 的产数口）+ 墙双侧同箱是否在场（`_wall_center` 的
        # common）。缺证据即 None——不拿 0 冒充"没触窗"。
        self._audit: dict = {}
        self.last: dict = {}                  # 实机 debug 数据面（ro_* 列）

    def update(self, bnd, now: float | None = None, *,
               new: bool = True,
               grid: DrivableGrid | None = None,
               fid: int | None = None,
               far_extents: tuple = (),
               center_override: float | None = None,
               v_lat: float = 0.0) -> Corridor | None:
        """``bnd``：DepthRoadReading（或鸭子同构：sides + 两侧 edge_lane）。

        ``new``：本拍读数是否新证据（异步驻留协议 take() 的 is_new）。中值窗、
        W 统计、记忆时间戳只认新证据——复用拍走墙/记忆，不刷新统计。

        ``grid``：同拍可行驶栅格（区兜底/互证审计面）。``fid``：墙实体进账
        的帧号（新证据帧喂观测，复用/弃权拍只走衰减——置信是时间的函数）。

        ``far_extents``：同帧中远场粗带地面横向范围（depth_geo.ground_far_extents
        产出，随读数驻留复用——与 grid 同一条慢变量纪律）。

        返回分带走廊（None=诚实弃权，交 planner 既有 machinery 消化，不造
        观测）；planner 吃 road_offset=−corridor.center_lane，选择层直接用
        带边界做目标夹取。"""
        if now is None:
            now = time.monotonic()
        self._far_extents = far_extents   # _build 消费（各拼装路径同帧同源）
        self._center_override = center_override  # 线可用时走廊以线为原点
        # 本拍独立深度自车横位（覆盖前），供线观测做道号锚点；线回退/保持值不给。
        self.depth_anchor_ego = None
        self._v_lat = float(v_lat) if math.isfinite(v_lat) else 0.0
        self._frame_gate_used = False
        self._audit = {}                  # 本拍审计面重置（None=本拍无该证据）
        if new:
            # 共享方向滤波只在新远带证据更新一次；本拍若未建成走廊，
            # 证据留到下一次建成时消费（不因弃权拍丢掉一次更新）。
            self._far_hint_pending = True
        grid_off = self._grid_off(grid)     # 互证审计面：闸关也常算
        master = self._xc is not None and self._xc.mode == "master"
        self._master = master
        # 平面尺度门只挂 master（回放链逐位保持旧行为）：坏平面的缘点/绿区
        # 是整帧几何垃圾，墙箱只走衰减、当拍不供新中心。复用拍同样判——
        # 同一坏读数会在后续控制拍被反复消费，不能只在 new 拍隔离。
        self._plane_ok = (not master) or self._plane_gate(bnd)
        # 墙实体证据先记账（master 分支提前返回也不能断墙箱寿命）
        if self._wt is not None and fid is not None:
            pts = (list(getattr(bnd, "edge_pts", ()) or ())
                   if (new and bnd is not None and self._plane_ok) else [])
            self._wt.update(_EdgePtsView(pts), fid)
        if not self._plane_ok:
            # 坏平面帧的缘点/绿区/远带范围整帧隔离：中心靠墙/记忆保持，
            # 分带边界落空气墙；坏几何不得同时污染中心与走廊表示。
            bnd = _NoPlaneView()
            grid = None
            self._far_extents = ()

        # master：绝对路心链（墙 → 绿区 → 绝对记忆），pair/slot 不接棒
        if master:
            pair = None
            if new and self._plane_ok and grid is not None \
                    and self._lane_w_m is not None:
                reg = region_reading(grid, self._lane_w_m)
                self._audit = {"l_clip": round(float(reg.left_clip_frac), 3),
                               "r_clip": round(float(reg.right_clip_frac), 3),
                               "n_rows": int(reg.n_rows),
                               "sides": int(reg.sides)}
                if reg.sides == 2 \
                        and reg.left_edge_lane is not None \
                        and reg.right_edge_lane is not None:
                    pair = (reg.left_edge_lane, reg.right_edge_lane)
            return self._fallback(now, grid, bnd, fid, master=True, new=new,
                                  grid_off=grid_off, region_pair=pair)
        if (new and bnd is not None and bnd.sides == 2
                and bnd.left_edge_lane is not None
                and bnd.right_edge_lane is not None):
            return self._pair_tick(bnd.left_edge_lane, bnd.right_edge_lane,
                                   now, grid_off, "pair", bnd, grid, fid)
        return self._fallback(now, grid, bnd, fid, master=master, new=new,
                              grid_off=grid_off)

    def _plane_gate(self, bnd) -> bool:
        """平面尺度门：|coef[2]|（=cam_h）在 bumper 机位物理带内；无平面证据
        （coef=None/异常）不冒充坏帧——深度层的质量门已管那一档。"""
        coef = getattr(bnd, "coef", None) if bnd is not None else None
        if coef is None:
            return True
        try:
            cam_h = abs(float(coef[2]))
        except (TypeError, ValueError, IndexError):
            return True
        return self.PLANE_CAM_H_LO <= cam_h <= self.PLANE_CAM_H_HI

    def _pair_tick(self, el: float, er: float, now: float,
                   grid_off: float | None, src: str, bnd, grid,
                   fid: int | None) -> Corridor | None:
        """回放链主候选一拍（pair，mode=off/veto）：门禁→中值窗→槽/W→走廊。
        拒绝返回 None（诚实弃权，无兜底），接受返回走廊。master 不走这里
        （绝对路心链见 _abs_tick/_wall_center）。"""
        off_raw = -((el + er) / 2.0)   # 本拍原始对（滤波前），debug 数据面
        pair_w = er - el               # 本拍对宽（供数门分账审计面，滤波前）
        if abs((el + er) / 2.0) > self.OFF_MAX:
            self.last = self._snapshot(now, src, None, off_raw, grid_off,
                                       pair_w=pair_w, reject="off_max")
            return None                 # 垃圾对：不喂、不入槽、不入窗
        # 对宽一致性门（实机 2026-10-05 案）：W 成形后对宽超出 W±20% 的对=
        # 找边锁错结构的污染对（车流走廊对宽 1.7 道、邻车侧面当"边"），同
        # 垃圾对路径不供数。
        formed = self.width
        if formed is not None and \
                abs((er - el) - formed) > self.W_GUARD_FRAC * formed:
            self.last = self._snapshot(now, src, None, off_raw, grid_off,
                                       pair_w=pair_w, reject="width_guard")
            return None                 # 污染对：不喂、不入槽、不入窗
        # 路心互证（grid_xcheck.mode=veto 专用）：新鲜对与同拍栅格 wmid 路心
        # 分歧超容限 → 疑似找边锁错结构，同污染对路径不供数；连续 veto 达
        # bypass_streak 放行并清零（黑视比噪声致命，闸不得制造供数黑视）。
        xcheck = None
        if self._xc is not None and self._xc.mode == "veto" \
                and grid_off is not None:
            if abs(off_raw - grid_off) > self._xc.tol_lane:
                self._xc_streak += 1
                if self._xc_streak < self._xc.bypass_streak:
                    self.last = self._snapshot(now, src, None, off_raw,
                                               grid_off, "veto",
                                               pair_w=pair_w, reject="veto")
                    return None
                xcheck = "bypass"       # 连续分歧达预算：放行，防黑视
            else:
                xcheck = "agree"
            self._xc_streak = 0
        # 中值滤波（窗口未满时直喂最新对）：单对野值被近 3 对中值杀掉，
        # 真漂移经 2~3 对（约 0.5s）跟进。
        self._win.append((el, er))
        if len(self._win) >= 3:
            el = sorted(w[0] for w in self._win)[1]
            er = sorted(w[1] for w in self._win)[1]
        self._slot["pair"] = (el, er, now)
        center = (el + er) / 2.0        # 路心（自车系）
        self._feed_width(er - el, center)
        if abs(center) > self.OFF_MAX:
            self.last = self._snapshot(now, src, None, off_raw, grid_off,
                                       pair_w=pair_w, reject="center_max")
            return None                 # 垃圾信号不喂（中值后复查，宁弃不喂假路心）
        return self._emit(center, er - el, src, bnd, grid, fid, now, grid_off,
                          off_raw=off_raw, pair_w=pair_w, xcheck=xcheck,
                          jump=False)

    def _abs_tick(self, el: float, er: float, now: float,
                  grid_off: float | None, src: str, bnd, grid,
                  fid: int | None) -> Corridor | None:
        """master 双侧宏观读数一拍（绿区物理边界）：垃圾/污染门 → 中值窗/W →
        绝对路心（跳变门在 _emit 内）。拒绝诚实 None，交候选链。"""
        off_raw = -((el + er) / 2.0)
        pair_w = er - el
        if abs((el + er) / 2.0) > self.OFF_MAX:
            self.last = self._snapshot(now, src, None, off_raw, grid_off,
                                       pair_w=pair_w, reject="off_max")
            return None
        # master 的宽门锚在规则先验（4 条等宽车道），不随学习中位数漂移：
        # 先验被读数拖偏时，相对门会把真路宽判成污染，形成自锁死循环。
        if abs((er - el) - self.W_PRIOR_LANE) > self.W_GUARD_LANE:
            self.last = self._snapshot(now, src, None, off_raw, grid_off,
                                       pair_w=pair_w, reject="width_guard")
            return None
        self._win.append((el, er))
        if len(self._win) >= 3:
            el = sorted(w[0] for w in self._win)[1]
            er = sorted(w[1] for w in self._win)[1]
        center = (el + er) / 2.0
        self._feed_width(er - el, center)
        if abs(center) > self.OFF_MAX:
            self.last = self._snapshot(now, src, None, off_raw, grid_off,
                                       pair_w=pair_w, reject="center_max")
            return None
        return self._emit(center, er - el, src, bnd, grid, fid, now, grid_off,
                          off_raw=off_raw, pair_w=pair_w)

    def _emit(self, center: float, width: float, src: str, bnd, grid,
              fid: int | None, now: float, grid_off: float | None, *,
              off_raw: float | None = None, pair_w: float | None = None,
              xcheck: str | None = None, jump: bool = True,
              mem_update: bool = True) -> Corridor | None:
        """采信一拍绝对路心：跳变门（本拍只判一次）→ 建走廊 → 记基线/记忆。

        ``jump=False`` 用于记忆/保鲜槽（它们的值不是新证据）；``mem_update=
        False`` 用于复用拍与记忆本身——记忆的时间戳只由真实新证据刷新，
        否则 TTL 永远不到期。"""
        if jump:
            ok = self._jump_ok(center, advance=not self._frame_gate_used)
            self._frame_gate_used = True
            if not ok:
                self.last = self._snapshot(now, src, None, off_raw, grid_off,
                                           xcheck, pair_w=pair_w,
                                           reject="jump")
                return None
        corridor = self._build(bnd, grid, fid, center, width, src, now)
        self._last_center = center
        if mem_update:
            self._mem = self._mem_update(center, width, now)
        self.last = self._snapshot(now, src, -center, off_raw, grid_off,
                                   xcheck, pair_w=pair_w)
        return corridor

    def _jump_ok(self, center: float, advance: bool = True) -> bool:
        """来源切换/单帧跳变门：与最近采信路心差 ≤0.4 道直接放行；更大的
        变化要有连续 JUMP_CONFIRM 拍同向一致（一帧野值/换源不拽目标）。
        ``advance=False``＝本拍已有候选计过数（候选链同帧多源不重复计数）。"""
        last = self._last_center
        if last is None or abs(center - last) <= self.JUMP_TOL_LANE:
            self._jump_pending = None
            return True
        if self._jump_pending is not None \
                and abs(center - self._jump_pending[0]) <= self.JUMP_TOL_LANE:
            if not advance:
                return False
            n = self._jump_pending[1] + 1
            if n >= self.JUMP_CONFIRM:
                self._jump_pending = None
                return True
            self._jump_pending = (center, n)
            return False
        if not advance:
            return False
        self._jump_pending = (center, 1)
        return False

    def _fallback(self, now: float, grid, bnd, fid: int | None,
                  master: bool, new: bool = True,
                  grid_off: float | None = None,
                  region_pair: tuple[float, float] | None = None) -> Corridor | None:
        """候选链（候选缺席/复用拍）：master=墙（绝对）→ 绿区（新鲜双侧）
        → 绝对记忆；回放链=墙 → 保鲜槽 → 区兜底（只在新鲜拍）→ 记忆。
        链尾记忆 TTL 内按 EMA 延伸，超时诚实 None。复用拍的栅格是旧证据，
        不得冒充新鲜（黑视纪律：宁可 None）。"""
        def slot_c():
            fresh = self._fresh(now)
            if fresh is None:
                return None
            el, er = fresh
            center = (el + er) / 2.0
            if abs(center) > self.OFF_MAX:
                return None
            return self._emit(center, er - el, "pair_slot", bnd, grid, fid,
                              now, grid_off, jump=False, mem_update=False)

        def mem_c():
            if not self._mem_fallback or self._mem is None:
                return None
            c, w, t = self._mem
            age = now - t
            if age > self.MEM_TTL_S:
                return None
            # 位移补偿：记忆存的是自车系坐标；自车右移 Δ 后路心在自车系左移 Δ。
            c = c - self._v_lat * age
            if abs(c) > self.OFF_MAX:
                return None
            return self._emit(c, w, "memory", bnd, grid, fid, now, grid_off,
                              jump=False, mem_update=False)

        def wall_c():
            wc = self._wall_center(fid, new)
            if wc is None:
                return None
            return self._emit(wc[0], wc[1], wc[2], bnd, grid, fid, now,
                              grid_off, mem_update=(new and master))

        def region_c():
            if region_pair is None:
                return None
            return self._abs_tick(*region_pair, now, grid_off, "region",
                                  bnd, grid, fid)

        def legacy_region_c():
            rc = self._region_center(grid)
            if rc is None:
                return None
            return self._emit(rc[0], rc[1], "region", bnd, grid, fid, now,
                              grid_off, mem_update=False)

        if master:
            order = (wall_c, region_c, mem_c)
        else:
            # 回放链：墙（新证据）→ 槽 → 区（区兜底只在新鲜拍）→ 朝向记忆
            order = ((wall_c, slot_c, legacy_region_c, mem_c) if new
                     else (wall_c, slot_c, mem_c))
        for f in order:
            out = f()
            if out is not None:
                return out
        if master and self._center_override is not None:
            # 线为原点、深度全断：走廊仍要存在（中心=线，边界缺证据落空气墙）。
            # 否则线明明知道车在哪，感知层却因「深度没有候选」整拍弃权。
            # 基线随线走（2026-11-02，8750 拍实证）：line 路径原不更新
            # _last_center，跳变门拿陈旧基线比对，wall 错值差得小直接放行。
            self._last_center = self._center_override
            corr = self._build(bnd, grid, fid, self._center_override,
                               self.width or self.W_PRIOR_LANE, "line", now)
            self.last = self._snapshot(now, "line", -self._center_override,
                                       None, grid_off)
            return corr
        self.last = self._snapshot(now, "none", None, None, grid_off)
        return None

    def _mem_update(self, center: float, width: float, now: float) -> tuple:
        """绝对路心记忆：存最后采信的绝对路心（wall/region/pair），原值保持。

        只由真实新证据拍刷新（复用拍与记忆本身不刷新时间戳）；相对读数
        （pair_slot）永不写入。不做时间滤波——过滤滞后让记忆在墙值恢复时
        慢半拍，正是 memory↔wall 来回摆的来源。"""
        return (center, width, now)

    def _wall_candidates(self, side: int,
                         fid: int | None) -> dict[float, tuple]:
        """该侧可用的墙箱 {zc: (lane, conf, live)}。

        live=measured（当拍重观测）；遮挡箱的位置是最后一次观测时的自车系
        坐标，只允许近龄（OCCL_MAX_AGE_FIDS）参与，更老的只留置信不复用位置。
        |lane|<0.15 的贴身墙不定位走廊（车底下/正侧方）。"""
        out: dict[float, tuple] = {}
        if self._wt is None or not (self._lane_w_m and self._lane_w_m > 0):
            return out
        for zc, pos, conf, st, last_fid in self._wt.bins(side):
            lane = pos / self._lane_w_m
            if abs(lane) < 0.15:
                continue
            if st == OCCLUDED:
                if conf < OCCL_MIN_CONF:
                    continue
                if fid is not None and fid - last_fid > self.OCCL_MAX_AGE_FIDS:
                    continue
            out[round(zc, 6)] = (lane, conf, st == MEASURED)
        return out

    def _wall_center(self, fid: int | None = None,
                     new: bool = True) -> tuple[float, float, str] | None:
        """墙/空气墙绝对路心。

        - 双侧同箱、两侧都当拍在场、墙到墙宽与规则路宽（4.0 道）相容 → 中点
          + 墙到墙宽（宏观结构，车已被 YOLO 挖除；弯道上错箱中点会被弯曲拽偏，
          故要求同 z 箱）。
        - 双侧同箱但与规则路宽差超 0.7 道（回放链：学习中位数的 ±20%）
          → **诚实弃权**：至少一侧锁错结构（实拍：真左墙 + 假右缘宽 2.6 道），
          不猜是哪一侧；交绿区（远行中心）/记忆接棒。
        - 无同箱双侧 → **不给中心**（单侧墙只作该侧边界）。单侧反推 ∓W/2 的
          偏置跟方向/遮挡走（实测 0.7 道级），线为主后不再让它当原点；线也
          丢失时交绿区/记忆/None。
        |center|>OFF_MAX 的远墙假设弃权。"""
        left = self._wall_candidates(-1, fid)
        right = self._wall_candidates(1, fid)
        formed = self.width
        live_l = {zc: v for zc, v in left.items() if v[2]}
        live_r = {zc: v for zc, v in right.items() if v[2]}
        common = sorted(set(live_l) & set(live_r), key=abs)
        self._audit["wall_same_bin"] = bool(common)   # 双侧同箱且都当拍在场
        for zc in common:
            lane_l, _cl, _ = live_l[zc]
            lane_r, _cr, _ = live_r[zc]
            w = lane_r - lane_l
            center = 0.5 * (lane_l + lane_r)
            if abs(center) > self.OFF_MAX:
                continue
            if not self._width_ok(w, formed):
                continue             # 与路宽先验矛盾：不猜哪一侧
            if new:
                self._feed_width(w, center)
            return center, w, "wall"
        if common:
            return None              # 同箱双侧对全部矛盾：诚实弃权
        if self._master:
            # 线为主（2026-10-31）：单侧墙只作该侧边界（带边界仍由
            # build_corridor 从墙箱/绿区填），不再 ∓W/2 反推路心——单侧反推
            # 的偏差跟方向和遮挡状态走（实测左右墙交替差 0.7 道），正是
            # memory↔wall_occl 来回整道跳的来源。线也丢失时交绿区/记忆/None。
            return None
        # 回放链（off/veto）保留旧的单侧墙反推，逐位保持旧行为。
        if formed is None:
            return None
        best: dict[bool, tuple] = {}
        for zc, (lane, conf, live) in (list(left.items())
                                       + list(right.items())):
            center = lane + formed / 2.0 if lane < 0 else lane - formed / 2.0
            if abs(center) > self.OFF_MAX:
                continue
            key = (not live, -conf, abs(zc))
            side = lane < 0
            if side not in best or key < best[side][0]:
                best[side] = (key, (center, live))
        if not best:
            return None
        if len(best) == 2:
            (ca, la), (cb, lb) = best[False][1], best[True][1]
            if la and lb and abs(ca - cb) > self._width_tol(formed):
                return None
        key, (center, live) = min(best.values(), key=lambda kv: kv[0])
        return center, formed, ("wall" if live else "wall_occl")

    def _region_center(self, grid) -> tuple[float, float] | None:
        """绿区中心（区读数语义收进走廊兜底）：sides==2 才给（clip 假缘诚实
        降级），|center|>OFF_MAX 同垃圾门。"""
        if grid is None or not (self._lane_w_m and self._lane_w_m > 0):
            return None
        reg = region_reading(grid, self._lane_w_m)
        if reg.sides != 2 or reg.left_edge_lane is None \
                or reg.right_edge_lane is None:
            return None
        center = (reg.left_edge_lane + reg.right_edge_lane) / 2.0
        if abs(center) > self.OFF_MAX:
            return None
        return center, reg.right_edge_lane - reg.left_edge_lane

    def _build(self, bnd, grid, fid: int | None, center: float,
               width: float, src: str, now: float) -> Corridor:
        # 线观测的道号锚点必须是**独立深度读数**：覆盖前把候选留下来。
        # 保持值（memory/wall_occl/pair_slot）与线回退（line）不给——前者过期、
        # 后者就是线自己的输出，拿它取整 k 等于自洽锁住错网格。
        # 符号：center 是路心相对自车（左负右正），线观测的 k=round(x+L) 与
        # x=k−L 用的是自车横位（右正），两者差一个负号——直接喂 center 会让
        # 整张网格按「自车在路心另一侧」取整，开局就错一道且随线锁固化。
        if src in ("wall", "region", "pair"):
            self.depth_anchor_ego = -float(center)
        # 线为原点时，走廊中心用线的位置（center_override），深度的墙/记忆只
        # 供边界证据与宽度统计；深度来源跳变不再把走廊中心整道拽走。
        if self._center_override is not None:
            center = self._center_override
        corr = build_corridor(
            bnd, grid, fid,
            center_lane=center, width_lane=width, src=src,
            lane_w_m=self._lane_w_m,
            far_extents=self._far_extents,
            far_ground_supply=self._far_ground_supply)
        return self._far_veto_to(self._far_hint_tick(corr, now))

    def _far_veto_to(self, corr: Corridor) -> Corridor:
        """深塌缩持续跟踪：当拍塌缩道集逐拍推进游程（纯函数在 corridor），
        持续 ≥FAR_VETO_TICKS 拍的道进否决集，随走廊挂出供消费端只读。"""
        self._far_veto_runs, self._far_veto = far_veto_tick(
            self._far_veto_runs,
            far_veto_lanes(corr.coarse_bands, corr.center_lane,
                           self._lane_w_m))
        return replace(corr, far_veto=self._far_veto)

    def _far_hint_tick(self, corr: Corridor, now: float) -> Corridor:
        """中远场方向共享滤波：新远带证据拍更新一次，其余拍共享同一结果。

        原始坐标直接算（不经选择帧），executed/走廊原点不得漏进方向信号；
        消费端只读 ``corridor.far_hint_lane``，不再各自去抖或重算。"""
        pending = self._far_hint_pending
        self._far_hint_pending = False
        raw = far_hint_lane(corr.bands, corr.coarse_bands, self._lane_w_m)
        if raw is None:
            if pending:
                self._far_hint = None
                self._far_hint_t = None
        elif pending:
            self._far_hint = raw if self._far_hint is None else (
                self._far_hint + self.FAR_HINT_ALPHA * (raw - self._far_hint))
            self._far_hint_t = now
        if self._far_hint is None:
            return corr
        age_ms = (None if self._far_hint_t is None
                  else (now - self._far_hint_t) * 1000.0)
        return replace(corr, far_hint_lane=self._far_hint,
                       far_hint_age_ms=age_ms)

    def _fresh(self, now: float) -> tuple[float, float] | None:
        s = self._slot.get("pair")
        if s is None or now - s[2] > self.SLOT_TTL_S:
            return None
        return s[0], s[1]

    def _grid_off(self, grid: DrivableGrid | None) -> float | None:
        """同拍栅格 → 栅格路心观测（off 系，右正）：off=−grid_center_lane，
        与 off=−(L+R)/2 同式。无栅格/无标定/无绿行=弃权。"""
        if grid is None or self._lane_w_m is None:
            return None
        c = grid_center_lane(grid, self._lane_w_m)
        return None if c is None else -c

    def _width_ok(self, width: float, formed: float | None) -> bool:
        """宽门：master 锚在规则先验（绝对道数）；回放链沿用成形地板 + 相对门。"""
        if self._master:
            return abs(width - self.W_PRIOR_LANE) <= self.W_GUARD_LANE
        if width < self.W_MIN_LANE:
            return False
        return formed is None or abs(width - formed) <= self.W_GUARD_FRAC * formed

    def _width_tol(self, formed: float | None) -> float:
        """与宽门同尺的容差（道）。"""
        return (self.W_GUARD_LANE if self._master
                else self.W_GUARD_FRAC * (formed or 0.0))

    def _feed_width(self, width: float, center: float | None = None) -> None:
        """中值滤波后的双侧物理宽进 W 统计（垃圾对/单侧/复用拍不进——与供数
        门同一入口纪律）。master：只吃与规则先验相容的读数，且要求相邻新证据
        拍中点位移 ≤W_CENTER_STEP_LANE——单拍锁错结构（一对边里混进邻车/假缘）
        往往伴随中点跳变，先等一拍确认再进统计。回放链保持旧口径。"""
        if not self._master:
            if not self._width_ok(width, self.width):
                return
            self._w_samples.append(width)
            return
        if not self._width_ok(width, None):
            return
        if center is not None:
            if self._w_center is not None \
                    and abs(center - self._w_center) > self.W_CENTER_STEP_LANE:
                self._w_center = center
                return
            self._w_center = center
        self._w_samples.append(width)

    @property
    def width(self) -> float | None:
        """路宽 W（道，碰撞边界到碰撞边界）。

        master 在样本不足时直接返回规则先验（4 条等宽车道）：先验是事实，
        不是估值，单侧墙/空气墙从第一拍就有正确半宽可用。回放链保持 None。
        """
        if len(self._w_samples) < self.W_MIN_SAMPLES:
            return self.W_PRIOR_LANE if self._master else None
        return float(np.median(self._w_samples))

    def _snapshot(self, now: float, src: str, off: float | None,
                  off_raw: float | None = None,
                  grid_off: float | None = None,
                  xcheck: str | None = None,
                  pair_w: float | None = None,
                  reject: str | None = None) -> dict:
        """debug/trace 数据面（ro_* 列）：来源、喂入值、原始对、同拍栅格路心
        与互证判定（agree/veto/bypass；复用拍、闸关、栅格弃权=None）。
        pair_w=本拍对宽（与 off_raw 二元定一对）；reject=主候选供数门拒绝
        原因（off_max/width_guard/veto/center_max）——拒绝拍分账审计面。"""
        return {"source": src, "off": off, "off_raw": off_raw,
                # origin=走廊中心实际用的原点（line=线为主；其余=深度来源）
                "origin": ("line" if self._center_override is not None
                           else src),
                "width": self.width, "grid_off": grid_off, "xcheck": xcheck,
                "pair_w": pair_w, "reject": reject, "plane_ok": self._plane_ok,
                "audit": dict(self._audit)}


def _started_async_depth(obs: DepthRoadObserver,
                         debug_dir: Path | None = None,
                         dump=None) -> AsyncDepthRoadObserver:
    """构造即启动的异步深度观测器（session 不可用时 start 自为 no-op，见其 docstring）。

    debug_dir 给定时，worker 按 dump 三态闸落盘（off 不建 dir=零落盘；
    events=事件触发 ring+窗口；full=0.5s 节流全量）。生命周期与 chain 同
    呼吸：这里 start，驾驶循环 finally 的 _stop_depth_observer 收口。"""
    a = AsyncDepthRoadObserver(obs, debug_dir=debug_dir, dump=dump)
    a.start()
    return a


def _ego_hud_readout(hud) -> tuple[int | None, int | None]:
    """HUD 读数的本机（蓝卡）得分与得分速度（局结局程序化取数面）。

    判据与 ``hud.read_hud_scores`` 同源：有 rate 的槽位才是本机槽位；rate
    缺失拍不臆造归属（诚实 None）。None 输入=未挂载/无样本；对象缺 latest()
    （读数线程退化的鸭子）同样返回 None——本函数在控制拍热路径，不得因
    读数侧异常炸驾驶循环。"""
    if hud is None:
        return None, None
    latest_fn = getattr(hud, "latest", None)
    if latest_fn is None:
        return None, None
    try:
        latest = latest_fn() or {}
        scores = read_hud_scores(latest.get("fields") or {})
        return scores.ego_score, scores.ego_rate
    except Exception:  # noqa: BLE001 —— 读数侧异常不炸驾驶循环（诚实 None）
        return None, None


def _note_hud_scores(chain: dict) -> None:
    """把本拍可读的（我方, 对手）比分对收进本阶段的小环。

    速度行缺失即弃权（第二局段 1 那条双方 trusted 但串位的假读没有速度行）。
    假读不在这里猜：阶段收尾时以汇总末分为锚，选「我方分不超过末分 + 一个
    采样余量」的最大一对——串位虚高的比分（实测 4878 / 888）自然被排除。
    """
    hud = chain.get("hud_ref")
    if hud is None:
        return
    try:
        latest = hud.latest() or {}
        scores = read_hud_scores(latest.get("fields") or {})
    except Exception:  # noqa: BLE001 —— 读数侧异常不炸驾驶循环
        return
    if scores.ego_score is None or scores.opponent_score is None:
        return
    chain.setdefault("hud_pairs", deque(maxlen=64)).append(
        (scores.ego_score, scores.opponent_score))




# 一局内固定产物目录：同一 (局前缀, 阶段) 只解析一次。缓存是必须的——
# 同阶段的目录会被 chain 与 hud 两处先后取用，第二次若重新判重会看到刚建好的
# 目录、误判"撞名"而多加一个序号后缀，同阶段的产物就分家了。
_run_dirs: dict[tuple[str, int], Path] = {}
# 宏组目录：同一局前缀只解析一次——phase 1 建好的组目录若在 phase 2 重新判重，
# 会被判成"撞名"而分出一个新组，p1/p2 就配不成一组了。
_macro_groups: dict[str, Path] = {}


def _macro_group_dir(stamp: str) -> Path:
    """本局宏组目录（两个驾驶阶段共用）；同秒连开两局时追加序号。"""
    hit = _macro_groups.get(stamp)
    if hit is not None:
        return hit
    path = _fresh_session_dir(macro_root() / stamp)
    _macro_groups[stamp] = path
    return path


def _fresh_session_dir(path: Path) -> Path:
    """已存在且非空的目录换一个带序号的名字，绝不给既有会话续写。

    目录名按局前缀生成，正常不会重名；同秒重启、或人工改过系统时间时仍可能撞。
    撞上时**换名**而不是复用：复用会让新一局覆盖旧图、索引被追加成两局混合
    （2026-10-30 实机踩到，第一局的图被第二局整批覆盖）。"""
    if not path.exists() or not any(path.iterdir()):
        return path
    for i in range(2, 100):
        cand = path.with_name(f"{path.name}_{i}")
        if not cand.exists() or not any(cand.iterdir()):
            logger.log(f"[极速狂飙] 会话目录 {path.name} 已存在且非空，"
                       f"改写到 {cand.name}", "WARNING")
            return cand
    raise RuntimeError(f"会话目录 {path.name} 的重名后缀已用尽")


def _run_dir(stamp: str, phase: int) -> Path:
    """一局一产物目录（2026-10-12 布局收口，paths.py 五类契约）：debug/<module>/<会话>/。

    speedrush 帧证据（证据包/hud/badframes/索引）全在
    ``debug/speedrush/<局前缀>_p<阶段>/``——一局一目录，清当批=删一个目录；
    逐拍流水与局汇总随日志会话走（ADR-0010），在
    ``logs/<会话>/trace/``。``data/`` 放用户资产与业务数据（宏、DB），不放调试
    产物。整个目录受 debug 开关管辖（关=零产物）；帧证据包密度另受 debug_dump
    三态闸（off/events/full）。

    局前缀由调用方按**局**生成（``_run_flow`` 每轮一个），同一局的两个驾驶阶段
    共用它、不同局必然不同。不能用日志会话编号：它标识的是"一次启动"，同一次
    启动连跑多局时重复，后一局会覆盖前一局的图。"""
    key = (stamp, phase)
    hit = _run_dirs.get(key)
    if hit is not None:
        return hit
    path = _fresh_session_dir(debug_dir() / "speedrush" / f"{stamp}_p{phase}")
    _run_dirs[key] = path
    return path


def resolve_start_index(start_from: str | None) -> int:
    """把 GUI 断点的阶段名换算为 FLOW 下标；缺省或未知值一律回退 0。"""
    if not start_from:
        return 0
    for i, (stage, _, _) in enumerate(FLOW):
        if stage == start_from:
            return i
    return 0