#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巅峰鉴宝活动模块（阶段一：Debug 观察 + 记录模式）。

当前功能：
  • Debug 截图循环：每 ~500ms 截一帧，通过 TreasureDebugRenderer 绘制 HUD+ROI
  • 独立调试目录：debug/treasure/<时间戳>/ 下保存 0001.png / 0002.png ...（渲染后 + 原始备份）
  • 状态机：阶段(STAGE) / 回合(ROUND) / 系统价 H / 我方出价 / 排名 记录
  • 事件日志：阶段切换 → INFO 日志；关键事件（如 record_event()）→ 额外命名截图 + INFO 日志
  • 手动阶段切换：set_stage() / set_round() / set_h() / set_rank()，方便 OCR 注入 + 外部控制
  • 估值公式：前 3 回合系统报价最大值 sysmax_13 → 真实估值 range = [sysmax*1.33, sysmax*1.44]
    （求稳用 ×1.35，激进用 ×1.4）
  • 纯观察：不做任何点击 / 手柄操作，全程只记录

后续：待 OCR + 出价算法完成，替换「纯观察循环」为「识别→决策→出价」的自动循环。
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

import cv2
import numpy as np

from maaracing_assistant.core.base import ActivityContext, ActivityModule
from maaracing_assistant.core.stage_tracker import StageTracker
from maaracing_assistant.plugins.treasure.store import TreasureStore
from maaracing_assistant.plugins.treasure.strategy import (
    BALANCE_UNKNOWN,
    BidContext,
    BidStrategy,
    DECISION_OBSERVE,
    RoundSnapshot,
    STRATEGY_LABEL,
    VAL_COEF,
)
from maaracing_assistant.plugins.treasure.detector import TreasureStageDetector
from maaracing_assistant.core.nav_graph import NavGraph
from maaracing_assistant.core.navkit import (
    DecisionFacts,
    DecisionSnapshot,
    FrameTrace,
    StateSnapshot,
    TraceWriter,
    compile_plan,
)
from maaracing_assistant.plugins.treasure.eggs import EggRewardRecognizer
from maaracing_assistant.plugins.treasure.ocr import TreasureOcr
from maaracing_assistant.plugins.treasure.renderer import TreasureDebugRenderer
from maaracing_assistant.core.paths import data_dir, debug_dir
from maaracing_assistant.core.window_utils import (
    check_game_window_aspect,
    is_foreground,
    verify_frame_client,
)
from maaracing_assistant.core.logger import logger
from maaracing_assistant.plugins.treasure import CONFIG_DIR, IMAGE_DIR, v3_assets


class ClickRetryExhaustedError(RuntimeError):
    """阶段切换类点击重试耗尽：点击无法生效、页面不切换，判定为系统性问题，终止模块。

    抛出后由主循环单帧兜底识别并【穿透】（不进入"连续帧异常"计数兜底），
    沿 start() 上抛 → start_module 记 ERROR → finally 恢复音量并收尾，流程停止。
    """


@lru_cache(maxsize=1)
def _policy_tuning() -> dict[str, Any]:
    """P1 收编：v3 资产 `policies.tuning` 全量（perception/policy/execution）。

    加载失败 / v2 模式 / 资产无 policies → 返回 {}（调用方回落代码常量）。
    """
    if os.environ.get("NAVKIT_SOURCE", "v3").lower() == "v2":
        return {}
    v3_path = CONFIG_DIR / "treasure_assets.json"
    if not v3_path.exists():
        return {}
    try:
        from maaracing_assistant.core.navkit import Assets
        assets = Assets.load(v3_path, module="treasure")
        if assets.policies is None:
            return {}
        return dict(assets.policies.tuning)
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _perception_tuning() -> dict[str, Any]:
    """P1 收编：v3 资产 `policies.tuning.perception`（匹配阈值/ROI 单一真源）。"""
    return dict(_policy_tuning().get("perception") or {})


def _load_action_centers(proj: Path) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """读 v3 资产（anchors）的动作/模板 rect → 归一化中心点 + 归一化宽高。

    动作按钮分布在 point 与 template 两类锚点；宽高供手柄模式点击容差用（落点在框中心
    70% 区域内即可按 A——ROI 本对标整个可交互区域，无需像素级精确到中心）。
    E1：v3 为唯一真源，无 v3 资产时返回空 dict（不再回读 treasure_rois.json）。
    """
    assets = v3_assets()
    if assets is None:
        return {}, {}
    try:
        out: dict[str, tuple[float, float]] = {}
        sizes: dict[str, tuple[float, float]] = {}
        for key, anchor in assets.anchors.items():
            if anchor.kind not in ("point", "template"):
                continue
            rect = anchor.rect.as_list()
            x1, y1, x2, y2 = rect
            out[key] = ((x1 + x2) / 2, (y1 + y2) / 2)
            sizes[key] = (x2 - x1, y2 - y1)
        # 兼容 module 尚未改名的消费点；v3 的物理资产 id 仍保留可审计的新名。
        if "session_start_match_click" in out:
            out["session_start_match_btn"] = out["session_start_match_click"]
            sizes["session_start_match_btn"] = sizes["session_start_match_click"]
        return out, sizes
    except Exception as exc:
        logger.log(f"[鉴宝] v3 actions rect 读取失败: {exc}", "WARNING")
        return {}, {}


# 鉴宝师搜索 ROI（归一化）：头像卡片通常分布在屏幕中部。
# 放宽 ROI：顶部标题栏 < 0.18、底部按钮 > 0.92 都切掉；左右只留 3% 边框。
# 这样三卡片在左/中/右任一位置 + 尺寸浮动 30% 以内都不会跑出搜索范围。
_APPRAISER_SEARCH_ROI = (0.03, 0.18, 0.97, 0.92)
_APPRAISER_MATCH_THRESHOLD = 0.72  # TM_CCOEFF_NORMED（和阶段检测同一数量级）
# 多尺度匹配：缩放系数覆盖 0.70× ~ 1.30×（步长 0.05），容忍尺寸偏差 ±30%。
# 两个模板尺寸 244~271px，对应实际渲染尺寸 ~170px ~ 350px 全部覆盖。
_APPRAISER_MATCH_SCALES: tuple[float, ...] = (
    0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30,
)
# 按顺位排列的模板定义：顺序决定优先级，p1 > p2。key 用于日志/准星显示，file 是资源目录内文件名。
# 定义源优先取 treasure_rois.json 的 appraisers 段（调试台可编辑/逐项校准 rect+threshold）；
# JSON 段缺失/损坏时回退到这里的代码常量（全卡统一搜索区 _APPRAISER_SEARCH_ROI）。
_APPRAISER_TEMPLATE_DEFS: list[tuple[int, str, str]] = [
    (1, "appraiser_p1_caroline", "appraiser_p1_caroline.png"),
    (2, "appraiser_p2_shotaro",  "appraiser_p2_shotaro.png"),
]

# 鉴宝师「已选中」对勾（卡片右上角黄色√）：用于判定目标鉴宝师是否已被选中。
# 搜索区复用 _APPRAISER_SEARCH_ROI（覆盖左/中/右三张卡片），不绑定单卡 rect。
_SELECTED_CHECK_DEF = ("appraiser_selected_check", "appraiser_selected_check.png")
_CHECK_MATCH_THRESHOLD = 0.62  # 对勾√：黄色高对比+小尺寸模板，光影/压缩对分影响比人物头像更大，降 0.10
_CHECK_MATCH_SCALES: tuple[float, ...] = _APPRAISER_MATCH_SCALES

# 场次选择阶段（鉴宝大厅(选择场次)）内的判定/动作按钮：
#   • session_start_match_btn     —— 「开始匹配」按钮（stage 段模板：判定按钮是否已出现在屏幕上，
#                                    即右侧详情卡已切到目标场次；命中后点同一 key 的 actions 段 rect 中心）
#   • session_{master,expert,intern}_badge —— 地图上对应场次标签（点击切换场次，actions 段静态 rect 中心）
# 主流程由 GUI 的 target_session 配置驱动：未识别到「开始匹配」按钮 → 先点目标 badge 切详情卡；
# 识别到 → 直接点「开始匹配」。
_SESSION_PANEL_DEFS: list[tuple[int, str, str]] = [
    (0, "session_start_match_btn", "session_start_match_btn.png"),
]
_SESSION_MATCH_THRESHOLD = 0.90   # 与 treasure_rois.json stage.session_start_match_btn.threshold 一致。
                                  # 「开始匹配」按钮模板特征明显，CCOEFF 稳定在 0.9+；
                                  # 低阈值会在实习/专家场详情卡、地图背景文字上产生假命中（0.72~0.78）
_SESSION_MATCH_SCALES: tuple[float, ...] = (
    0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30,
)
# 点击「开始匹配」后的冷却帧数：按钮已消失但检测器还没切阶段（匹配中）时，
# 冷却期内不再产出新意图，防止回退点 badge（时序 bug，实测 01:08:26 连点事故）。
SESSION_START_CLICK_COOLDOWN_FRAMES = 3
# GUI 可选的目标场次 → actions 段 badge key + 中文名（用于日志/HUD显示）。
# badge key 对应 treasure_rois.json 的 actions 段：session_intern_badge / session_expert_badge / session_master_badge。
TARGET_SESSION_OPTIONS: dict[str, tuple[str, str]] = {
    "intern": ("session_intern_badge",  "实习场"),
    "expert": ("session_expert_badge",  "专家场"),
    "master": ("session_master_badge",  "大师场"),
}
DEFAULT_TARGET_SESSION: str = "master"

# 运行时阶段名 → policies 稳定 ID（P1）。bid 回合（第N回合出价）统一为 "bid"，
# 由 _stage_id 特判处理；本表覆盖其余阶段，v2 回退路径（无 stage_map）兜底用。
_STAGE_TO_STABLE: dict[str, str] = {
    "游戏大厅": "hall",
    "活动页面": "activity",
    "鉴宝大厅(选择场次)": "session",
    "匹配中": "matching",
    "选择鉴宝师": "appraiser",
    "中标结算": "auction_result",
    "领取分红": "settle",
    "结算弹窗": "popup",
}


def _load_appraiser_templates(
    proj: Path,
) -> list[tuple[int, str, np.ndarray, tuple[float, float, float, float], float]]:
    """加载偏好鉴宝师头像模板（灰度），按顺位升序返回。加载失败的模板自动剔除。

    定义源：treasure_rois.json 的 appraisers 段（调试台「偏好鉴宝师」分类可编辑）：
        { "<key>": {"prio": n, "rect": [x1,y1,x2,y2], "templates": ["xxx.png"], "threshold": 0.72} }
      - rect = 该鉴宝师的卡片搜索区（匹配时按各自 rect 裁剪）
      - threshold = 该鉴宝师命中阈值（TM_CCOEFF_NORMED）
    JSON 段缺失/损坏 → 回退 _APPRAISER_TEMPLATE_DEFS（全卡统一搜索区/阈值）。

    返回: [(priority, key, gray_ndarray, rect, threshold), ...]，至少 0 项，不崩溃。
    """
    defs: list[tuple[int, str, str, tuple[float, float, float, float], float]] = []
    per = _perception_tuning()
    search_roi = tuple(per["appraiser_search_roi"]) if per.get("appraiser_search_roi") else _APPRAISER_SEARCH_ROI
    match_th = per.get("appraiser_match_threshold", _APPRAISER_MATCH_THRESHOLD)
    for prio, key, fname in _APPRAISER_TEMPLATE_DEFS:
        defs.append((prio, key, fname, search_roi, match_th))
    assets = v3_assets()
    if assets is not None:
        # v3 优先真源：_APPRAISER_TEMPLATE_DEFS 供「有哪些鉴宝师」身份，rect/threshold/prio 逐卡取
        # v3 锚点（prio↔order）；缺锚点或非 template 时回退该卡代码默认值（search_roi/match_th/prio_default）。
        v3_defs: list[tuple[int, str, str, tuple[float, float, float, float], float]] = []
        for prio_default, key, fname_default in _APPRAISER_TEMPLATE_DEFS:
            anchor = assets.anchors.get(key)
            if anchor is None or anchor.kind != "template":
                v3_defs.append((prio_default, key, fname_default, search_roi, match_th))
                continue
            try:
                prio = int(anchor.order) if anchor.order is not None else prio_default
            except (TypeError, ValueError):
                prio = prio_default
            tpls = anchor.templates
            fname = (tpls[0] if tpls and isinstance(tpls[0], str) else "") or fname_default
            r4 = anchor.rect.as_list()
            rect = (float(r4[0]), float(r4[1]), float(r4[2]), float(r4[3]))
            threshold = float(anchor.threshold) if anchor.threshold is not None else match_th
            v3_defs.append((prio, key, fname, rect, threshold))
        if v3_defs:
            defs = v3_defs
    # E1：无 v3 资产（缺失/损坏 / NAVKIT_SOURCE=v2）时保留 _APPRAISER_TEMPLATE_DEFS 代码常量兜底
    # （全卡统一搜索区/阈值），**不再回读 treasure_rois.json**。
    out: list[tuple[int, str, np.ndarray, tuple[float, float, float, float], float]] = []
    for prio, key, fname, rect, threshold in defs:
        if not fname:
            continue
        p = IMAGE_DIR / fname
        if not p.exists():
            continue
        img = cv2.imread(str(p))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.size == 0 or gray.shape[0] < 4 or gray.shape[1] < 4:
            continue
        out.append((prio, key, gray, rect, threshold))
    # 顺位越小编号越优先，排序保证遍历顺序 = 优先级顺序
    out.sort(key=lambda x: x[0])
    return out


def _load_selected_check(
    proj: Path,
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    """加载「已选中」对勾模板（灰度）+ 扫描区域 rect。

    rect 从 treasure_rois.json 的 stage.appraiser_selected_check 读取（调试台可调）：
     应框住三张卡片右上角的对勾高度带（横向长条，X 覆盖左/中/右三卡）。
     文件缺失/损坏/rect 非法返回 None（选中判定自动跳过）。
    """
    _, fname = _SELECTED_CHECK_DEF
    p = IMAGE_DIR / fname
    if not p.exists():
        return None
    img = cv2.imread(str(p))
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.size == 0 or gray.shape[0] < 4 or gray.shape[1] < 4:
        return None
    # rect：v3 唯一真源读锚点 appraiser_selected_check；E1：无 v3 时不再回读 treasure_rois.json，
    # rect 保持 None → 本函数返回 None（选中判定自动跳过）。
    rect: tuple[float, float, float, float] | None = None
    assets = v3_assets()
    if assets is not None:
        anchor = assets.anchors.get("appraiser_selected_check")
        if anchor is not None:
            r4 = anchor.rect.as_list()
            rect = (float(r4[0]), float(r4[1]), float(r4[2]), float(r4[3]))
    if rect is None:
        return None
    return (gray, rect)


def _load_session_panel(
    proj: Path,
) -> list[tuple[int, str, np.ndarray, tuple[float, float, float, float]]]:
    """加载「开始匹配」按钮模板（详情卡已切到目标场次的判定用）。

    rect 从 treasure_assets.json（v3 优先）或 treasure_rois.json（v2 回退）读取
    （key: session_start_match_btn）；返回: [(priority, key, gray, rect_norm)]，
    缺失则返回空列表（判定降级为未匹配 → 始终先点目标场次 badge，再点开始匹配位置）。
    """
    rois: dict[str, tuple[float, float, float, float]] = {}
    assets = v3_assets()
    if assets is not None:
        anchor = assets.anchors.get("session_start_match_btn")
        if anchor is not None:
            rois["session_start_match_btn"] = tuple(anchor.rect.as_list())
    # E1：无 v3 资产（缺失/损坏 / NAVKIT_SOURCE=v2）→ rois 为空，判定降级为「始终先点目标 badge」，
    # 不再回读 treasure_rois.json。
    out: list[tuple[int, str, np.ndarray, tuple[float, float, float, float]]] = []
    for prio, key, fname in _SESSION_PANEL_DEFS:
        rect = rois.get(key)
        if rect is None:
            continue
        p = IMAGE_DIR / fname
        if not p.exists():
            continue
        img = cv2.imread(str(p))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.size == 0 or gray.shape[0] < 4 or gray.shape[1] < 4:
            continue
        out.append((prio, key, gray, rect))
    out.sort(key=lambda x: x[0])
    return out


# 出价面板「智能出价」按钮（截图3 右下角面板内数字键盘最后一排）：
# 只有出价面板打开才出现，模板命中 = 面板已开（S3 强信号），中心即点击目标。
# 注意与主界面底部「出价」按钮（bid_main_red_btn，走 OCR 文字判状态）是两回事。
_SMART_BID_KEY = "smart_bid_btn"

# 智能出价按钮匹配阈值（面板开 S3 强信号）：优先读 JSON stage.smart_bid_btn.threshold
# （调试台可校准），缺省回退本值。注意不可复用 _SESSION_MATCH_THRESHOLD（0.90 是「开始匹配」
# 按钮的）——智能出价按钮小、模糊/非标准窗口下匹配分偏低（实测 0.686），0.90 会永远判
# 「面板未开」→ 卡 S2 反复点主出价按钮、不点智能出价。阶段感知裁剪后该 ROI 只在出价阶段
# 参与匹配，放宽阈值不会误识别其它阶段的背景。
_SMART_BID_MATCH_THRESHOLD = 0.72

# ==================================================================
#  阶段感知清单（动态激活）
#  ------------------------------------------------------------------
#  背景：非标准窗口（DPI 缩放/分辨率变化）下画面模糊 → 单点模板匹配分
#  不稳定（如 smart_bid_btn 多尺度最优仅 0.686 < 0.90 → 面板判未开 →
#  不点智能出价）。全局高阈值防误识别，但小目标按钮够不到阈值。
#  方案：按阶段只激活「当前画面必然出现/相关的 ROI」，把「放宽阈值」
#  和「防误识别」解耦 —— 其它阶段的背景元素根本不参与匹配。
#
#  使用规则（改这里前必读）：
#   - 键：STAGE_ORDER 中的阶段名；值：该阶段要激活的 stage ROI 键
#     （来自 treasure_detector._ROI_STAGE，不含模块独立匹配的
#     appraiser_selected_check / session_start_match_btn / 鉴宝师模板）。
#   - 值只写「本阶段画面会出现/需要感知」的 ROI；全局锚点（_GLOBAL_ANCHORS）
#     运行时自动并入，不必重复写。
#   - 转移信号必须包含：本阶段画面里可能出现的「下一阶段/结算」信号，
#     否则阶段切换会漏检（如出价阶段必须含 settle_title/result_banner）。
#   - 新阶段忘了登记 → 运行时回退全量检测（安全兜底，不会静默卡死）。
# ==================================================================
# 全局锚点：任何阶段都可能异常掉回大厅（游戏大厅 / 鉴宝大厅选择场次）→
# 这些「大厅锚点」始终全量匹配。注意：感知裁剪（active_rois）的求解范围 = 阶段感知清单
# ∪ 本锚点，若漏掉回退目标页的识别 ROI，阶段会冻结（实测：结算弹窗点关闭后已回鉴宝
# 大厅，但检测器只扫弹窗 ROI+游戏大厅卡片，看不到 hall_session_cards → 永远停在结算弹窗）。
_GLOBAL_ANCHORS: frozenset[str] = frozenset({
    "hall_peak_appraise_card",  # 游戏大厅「巅峰鉴宝」卡片
    "hall_session_cards",       # 鉴宝大厅(选择场次) 场次卡片（弹窗链回退的常见落点）
})

# 回合出价阶段共用的激活集合（第1~5回合一致）。
# 注意：这是「出价完成（wait_result/wait_next）」后的完整激活集。出价面板
# 打开交互期（bidding，拨号盘输入中）由 _active_stage_rois 动态收窄为仅
# smart_bid_btn —— round_big_banner/result_banner/settle_title 是"出价完成后"
# 才可能出现的转移信号，拨号盘还在时必然不出现（见 _active_stage_rois 注释）。
_ROUND_PERCEPTION_ROIS: frozenset[str] = frozenset({
    "round_big_banner",   # 回合横幅：回合号权威来源 + 下一回合转移信号
    "smart_bid_btn",      # 智能出价按钮：面板开（S3）强信号
    "settle_title",       # 结算转移锚点：出价结束 → 领取分红
    "result_banner",      # 结算结果锚点：出价结束 → 中标结算
})

# 出价面板 OCR keys（worker 第二段按阶段裁剪）。
_BID_OCR_KEYS: frozenset[str] = frozenset({
    "bid_result_amount_box",  # H 值（输入框当前值，智能出价填入）
    "bid_player1", "bid_player2", "bid_player3", "bid_player4",  # 公开报价（快照）
    "player_name1", "player_name2", "player_name3", "player_name4",  # 玩家名（槽位定位）
    "round_label_area",       # 回合小字（附加回合兜底）
})
# 结算/分红 OCR keys。
_SETTLE_OCR_KEYS: frozenset[str] = frozenset({
    "settle_final_price", "settle_total_price", "settle_profit", "settle_my_income",
})

_STAGE_PERCEPTION: dict[str, frozenset[str]] = {
    "游戏大厅": frozenset({
        "hall_peak_appraise_card",  # 大厅卡片（点击进活动页/鉴宝大厅）
        "goto_appraise_btn",        # 活动页「前往鉴宝」（若已切到活动页）
        "hall_session_cards",       # 鉴宝大厅场次卡片（若已切到大厅）
    }),
    "活动页面": frozenset({
        "goto_appraise_btn",        # 「前往鉴宝」按钮（点击进鉴宝大厅）
        "hall_session_cards",       # 已进鉴宝大厅的转移信号
    }),
    "鉴宝大厅(选择场次)": frozenset({
        "hall_session_cards",       # 场次卡片区（「开始匹配」为模块独立匹配，不走 detect）
        "is_matching_btn",          # 点「开始匹配」后 → 匹配中 的转移信号（缺失会卡死在大厅反复点 badge）
    }),
    "匹配中": frozenset({
        "is_matching_btn",          # 匹配中按钮
        "appraiser_title",          # 匹配完成 → 选择鉴宝师 的转移信号
    }),
    "选择鉴宝师": frozenset({
        "appraiser_title",          # 选师页标题
        "round_big_banner",         # 确认后进回合的强信号（immediate 切换）
        "is_matching_btn",          # 仍处匹配中的转移信号
        # 对勾 / 鉴宝师模板为模块独立匹配，不走 detect
    }),
    "第1回合出价": _ROUND_PERCEPTION_ROIS,
    "第2回合出价": _ROUND_PERCEPTION_ROIS,
    "第3回合出价": _ROUND_PERCEPTION_ROIS,
    "第4回合出价": _ROUND_PERCEPTION_ROIS,
    "第5回合出价": _ROUND_PERCEPTION_ROIS,
    "中标结算": frozenset({
        "result_banner",            # 竞拍结果横幅（win/fail）
        "settle_title",             # 点领取后 → 领取分红 的转移信号
        "daily_high_banner",        # 弹窗链入口（今日最高）
        "egg_reward_title",         # 弹窗链入口（彩蛋）
    }),
    "领取分红": frozenset({
        "settle_title",             # 结算页标题
        "result_banner",            # 结算结果（若结果横幅仍在）
        "daily_high_banner",        # 弹窗链入口
        "egg_reward_title",         # 弹窗链入口
    }),
    "结算弹窗": frozenset({
        "daily_high_banner",        # 今日最高积分弹窗
        "egg_reward_title",         # 彩蛋弹窗
        "settle_title",             # 弹窗链未命中时结算页兜底
        "result_banner",            # 弹窗链未命中时结果横幅兜底
    }),
}

# OCR 感知清单：阶段 → 需要投递识别的 OCR keys（worker 第二段按此裁剪；None=全量）。
# 仅列出走异步 worker 的阶段；鉴宝大厅/结算弹窗走同步单 ROI，不在此表。
_STAGE_OCR_KEYS: dict[str, frozenset[str]] = {
    "第1回合出价": _BID_OCR_KEYS,
    "第2回合出价": _BID_OCR_KEYS,
    "第3回合出价": _BID_OCR_KEYS,
    "第4回合出价": _BID_OCR_KEYS,
    "第5回合出价": _BID_OCR_KEYS,
    "中标结算": _SETTLE_OCR_KEYS,
    "领取分红": _SETTLE_OCR_KEYS,
}


def _load_smart_bid_btn(
    proj: Path,
) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    """加载出价面板「智能出价」按钮模板（灰度）+ 扫描 rect。

    rect 从 treasure_rois.json 的 stage.smart_bid_btn 读取（调试台可调）。
    文件缺失/损坏/rect 非法返回 None（面板已开判定自动降级 → 依赖主按钮 OCR 兜底）。
    """
    p = IMAGE_DIR / "bid_smart_btn.png"
    if not p.exists():
        return None
    img = cv2.imread(str(p))
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.size == 0 or gray.shape[0] < 4 or gray.shape[1] < 4:
        return None
    rect: tuple[float, float, float, float] | None = None
    assets = v3_assets()
    if assets is not None:
        anchor = assets.anchors.get(_SMART_BID_KEY)
        if anchor is not None:
            r4 = anchor.rect.as_list()
            rect = (float(r4[0]), float(r4[1]), float(r4[2]), float(r4[3]))
    # E1：无 v3 时不再回读 treasure_rois.json；rect 保持 None → 返回 None（面板已开判定降级依赖主按钮 OCR 兜底）。
    if rect is None:
        return None
    return (gray, rect)


class TreasureModule(ActivityModule):
    """巅峰鉴宝：Debug 观察记录模式（阶段一）"""

    ID = "treasure"
    NAME = "巅峰鉴宝"
    REQUIRES = frozenset({"capture"})

    # 阶段顺序：GUI 断点选择用，OCR/自动化阶段会细化
    STAGE_ORDER = [
        "游戏大厅",
        "活动页面",
        "鉴宝大厅(选择场次)",
        "匹配中",
        "选择鉴宝师",
        "第1回合出价",
        "第2回合出价",
        "第3回合出价",
        "第4回合出价",
        "第5回合出价",
        "中标结算",
        "领取分红",
        # 结算后弹窗链（合并为单一阶段）：领取分红后可能依次弹出 ①今日最高积分上涨
        # ②鉴宝等级提升(无 ROI) ③奖励结算(彩蛋)，弹几个是随机的（也可能一个不弹）。
        # 弹窗只会遮满全屏 → 弹窗存在时检测器一定匹配不到大厅；弹窗全关后大厅才可见。
        # 具体是哪个弹窗由检测器 _last_hit_roi_key 区分（daily_high_banner=今日最高 /
        # egg_reward_title=彩蛋 / 无命中=等级提升盲点）。_accept_stage 放行「结算弹窗→大厅」。
        "结算弹窗",
    ]

    REQUIRES_GAMEPAD_EXCLUSIVE = False

    # 两套点击方式的日志/peep 标签（与 core.clicker.CLICK_MODES 对齐）
    CLICK_MODE_LABELS = {"real": "前台鼠标", "gamepad": "后台手柄+A"}

    # 光标避让：主循环每帧调用 _maybe_shoo_cursor，光标（圆盘+hover 高亮）压住
    # 「当前阶段需识别的 ROI」时让 core.clicker.auto_shoo 挪到邻近空白处，
    # 防止模板匹配/OCR 掉分失效。半径按圆盘+环+高亮扩散保守取值。
    SHOO_CURSOR_RADIUS_PX = 30.0

    # --------- 可调参数 ---------
    FRAME_INTERVAL_MS     = 300    # 截图周期（毫秒）：主循环 ~3.3Hz，满足「≥3 次/秒」画面采集
    WAIT_RESULT_FAST_MS   = 150    # wait_result 阶段帧间隔（用户拍板「帧率翻倍真双通道」）：
                                   # 报价读取频率 ×2，配合动态 keys 剔除已固化槽 → 未固化槽（尤其 P4）
                                   # 刷新率翻倍。仅报价等待阶段加速，不影响其他阶段。
    DEBUG_LOG_INTERVAL    = 1      # 验证期全量日志：每帧打一条 DEBUG 心跳（含阶段/H/出价/OCR指标）。
                                   # 验证完 OCR 尖峰修复后再考虑瘦身（如恢复 20 帧一次）
    CHANGE_PIXEL_THRESH   = 40     # 画面变化判定：平均像素差 > 该值 → 认为有显著变化（原25→40）
    CHANGE_AREA_RATIO     = 0.05   # 变化像素比例 > 该值 → 保存事件截图（原0.01→0.05）
    CHANGE_COOLDOWN_S     = 5.0    # 画面变化事件冷却（秒），抑制同屏动画反复触发
    STABLE_FRAMES         = 1      # 阶段切换防抖：连续 N 帧一致才采纳（回合单调+强特征双保险）
    SWITCH_CONFIRM_FRAMES = 5      # 回合切换转场延迟：新回合前 N 帧视为转场期（动画残缺值高发期）。
                                    # 期间 bid_player 不写当前回合槽，避免 R3→R4 切换瞬间识别到的
                                    # 残缺数字污染新回合槽。
    BID_CONFIRM_STABLE_FRAMES = 3  # 输入完成后确认态防抖：B==T 已就位后，OCR 单帧误读 B'≠T
                                    # 不立刻清空重输；连续 N 帧都读不到 T 才判定"输入被破坏/用户改价"
                                    # 并重置。避免"已输完、准星正指确认"被一次 OCR 抖动打回重输。
    PANEL_OPEN_MIN_STABLE_FRAMES = 3   # 面板打开连续稳定帧：连续 N 帧命中 smart_bid_btn 才认为"面板真的开了"。
                                        # 否则单帧闪中（转场期画面乱）会制造假上升沿 → 紧接着假下降沿
                                        # → phase 误切 wait_result，用户看起来在"一直等"。
    SUBMIT_ANIMATION_BUFFER_FRAMES = 5   # 出价区"出价中→已出价"过渡动画缓冲帧（≈1.5s，主循环 300ms/帧）。
                                        # 用户点确认后出价区有切换动画，动画期 OCR 读到的可能是
                                        # 旧"出价中"或乱帧；此时若用"已出价"硬门槛判断会误拒正常提交。
                                        # wait_result 内前 N 帧不校验，动画期过后仍"出价中"才算假下降沿。
    SUBMIT_ANIMATION_BUFFER_MS = 1500    # 假下降沿缓冲的**时间**口径（v4 定案）：帧数口径隐含依赖
                                        # 主循环帧率（v3 的 wait_result 150ms/帧），v4 节奏交还框架后
                                        # 实际帧间隔 ~125ms，10 帧缓冲被稀释到 1.25s → 对手未齐报价时
                                        # 误判假下降沿、phase 退 wait_first、OCR 停投 → 错过整个公开
                                        # 报价窗口（2026-09-09 20:13 实机 log 实锤）。计时与帧率解耦。
    # 真实点击（v0.4，方案见 docs/treasure_real_click_plan.md）：
    #   可见鼠标移动到目标 → 停顿 → SendInput 左键。所有参数化，便于按阶段调整。
    CLICK_MOVE_PAUSE_S = 0.4     # 鼠标移到目标后的停顿（让用户看清；后续出价 S3 可单独降 0.1~0.2s）
    CLICK_DOWN_UP_GAP_MS = 30    # LEFT_DOWN → LEFT_UP 之间间隔
    CLICK_COOLDOWN_S = 0.2       # 不同意图间最小物理点击间隔（限速；< 主循环 300ms 不阻塞推进）
    # 阶段切换类点击重试：指纹锁是边沿触发（同意图只点一次），若点击落空/被鼠标干扰
    # 导致页面没切换（如游戏大厅点卡片没进活动页），阶段不变 → 意图不变 → 永不重试 → 卡死。
    # 这里对「点击后预期离开当前阶段」的 key 加超时重试：阶段仍停在原地则重新 arm 指纹。
    # settle_collect_red_btn 特殊：仅「真领取」（本场收入已读出）才重试，见 _maybe_retry_stage_click。
    # 结算后弹窗（今日最高 / 奖励彩蛋）的「点击关闭」key。它们不是真实按钮（无 JSON rect），
    # 点击位置复用 confirm_red_btn 底部中心（弹窗底部「点击空白/屏幕继续」都吃这个位置）。
    # 独立 key 是必须的：_execute_click 指纹锁是全局的 (key,state,center)，若复用 confirm_red_btn
    # 会撞上「选择鉴宝师」阶段点过的指纹 → 弹窗首次点击被锁死（2026-08-15 预演洞1）。
    POPUP_HIGH_CONTINUE_KEY   = "popup_high_continue"    # 今日最高弹窗 → 点穿（含②等级提升不识别弹窗）
    POPUP_REWARD_CONTINUE_KEY = "popup_reward_continue"  # 奖励彩蛋弹窗 → 点关闭
    CLICK_RETRY_KEYS: frozenset = frozenset({
        "hall_peak_appraise_card",   # 游戏大厅 → 活动页（鼠标干扰事故）
        "goto_appraise_btn",         # 活动页 → 鉴宝大厅
        "session_start_match_btn",   # 选择场次 → 进入匹配
        "confirm_red_btn",           # 选择鉴宝师 → 进入出价
        "settle_collect_red_btn",    # 领取分红（真领取）→ 离开领取分红页
        "bid_main_red_btn",          # 出价按钮 → 打开出价面板（点击落空/动画期面板未开时重试）
        POPUP_HIGH_CONTINUE_KEY,     # 今日最高弹窗 → 点穿（含不识别弹窗，短周期连点）
        POPUP_REWARD_CONTINUE_KEY,   # 奖励彩蛋弹窗 → 点关闭（短周期连点）
    })
    CLICK_RETRY_FRAMES = 10        # 点击后等多少帧仍未切换 → 判定失败（主循环 ~300ms/帧 ≈ 3s，给足转场动画时间）
    CLICK_RETRY_MAX = 3            # 同一意图最多重试次数（含首点共 4 次），仍失败则停手并 WARNING
    # 领取分红「跳过动画」点击无响应兜底：跳过动画点击成功（ok=true）后，本场收入
    # （_settle_my_income）应被 OCR 读出（动画跳过 → 数据出现）。若等待
    # SETTLE_SKIP_RETRY_FRAMES 帧仍无收入 → 判定点击落空/游戏无响应（实测场景：
    # 点领取后按钮/动画无反应，收入永远读不到而静默卡死）→ 清指纹重新点击，
    # 最多 SETTLE_SKIP_RETRY_MAX 次仍无响应 → 抛 ClickRetryExhaustedError 终止模块。
    # 注意与「真领取」重试的区分（成功信号不同）：
    #   - 跳过动画：成功信号 = 收入读出（OCR 字段变化），在本常量独立实现
    #   - 真领取：  成功信号 = 阶段切走，复用 _maybe_retry_stage_click 的 stage 判定
    SETTLE_SKIP_RETRY_FRAMES = 10  # 点击后等多少帧仍未读到收入 → 判定无响应（≈3s，给足动画+OCR 时间）
    SETTLE_SKIP_RETRY_MAX = 3      # 最多重试次数（含首点共 4 次），仍无响应则终止模块
    # 弹窗连点用短周期（3帧 ≈ 0.9s）：模板 1 帧出、OCR 2 帧稳（用户实测）。
    # 与阶段切换类 key（CLICK_RETRY_FRAMES=10，要等转场动画）目的相反——弹窗要"连续关多个弹窗"，
    # 故 per-key 覆盖重试帧数，不动全局常量（全局降 3 会误伤卡片/领取等转场点击，2026-08-15 洞3）。
    POPUP_CONTINUE_RETRY_FRAMES = 3
    # 结算后弹窗（今日最高/彩蛋）点击关闭后的冷却帧数：点关闭后弹窗有消失动画，
    # 动画期模板匹配不上 → 检测器可能误判已回大厅/选场次 → "点了直接跳过"（下一弹窗
    # 未识别就点穿/直接开新场）。点击成功后的 N 帧内不产出新点击意图，等动画稳定
    # 再识别（2026-08-16 反馈：最后两个阶段缺冷却）。
    # 注意：领取分红「真领取」点击成功后也要进入冷却（弹窗链出现前的动画期），
    # 否则领取按钮刚消失、今日最高/彩蛋弹窗还没完全出现的帧，模板匹配不上弹窗，
    # 会直接匹配到大厅 → 合法回退被接受 → 最后两阶段跳过。
    POPUP_CLICK_COOLDOWN_FRAMES = 5
    # 弹窗链「回退到大厅/选场次」的连续稳定帧确认（独立于 _popup_click_cooldown）：
    #   弹窗链阶段（领取分红/结算弹窗）→ 大厅/选场次 的 idx 合法回退，需要连续 N 帧
    #   都识别到回退目标才放行。弹窗只会遮满全屏 → 弹窗存在时大厅模板一定匹配不到，
    #   单帧放行仍可能踩中「弹窗消失动画期 1~2 帧」的误判 → 提前切大厅；连续 3 帧
    #   确认则既挡住动画期误判（1~2 帧不足），也不卡真回大厅（大厅模板稳定可见）。
    POPUP_LOOPBACK_STABLE_FRAMES = 3
    # 今日最高积分弹窗：数值读取超时兜底帧数（≈2.4s）。读不到积分值（ROI 未校准/OCR 失败）
    # 时若一直等会卡死弹窗链 → N 帧后照点跳过（数值记录为 None，highest_score 不更新）。
    DAILY_HIGH_TIMEOUT_FRAMES = 8
    # 我方槽位（排名）识别防抖：player_nameN 带「（我）」标记 → 行序即槽号。刚进入对局时
    # 玩家名区域有转场动画，单帧 OCR 可能误读（如把别的行读成带"我"），且 set_rank 只在
    # 值变化时更新 → 一次误读就锁死错误排名。连续 RANK_STABLE_FRAMES 帧读到同一槽号才采纳。
    RANK_STABLE_FRAMES = 3
    # 奖励结算点击门控：蛋 OCR 读完（_egg_read_done）或超时兜底 → 才点关闭，防提前关掉丢计数。
    # 竞态修复（2026-08）：识别投递与点击都通过 _egg_reading 状态绑定到「彩蛋弹窗进行中」，
    # 而不再依赖单个 title ROI 是否命中——title 有转场/闪断，仅凭单帧命中会让识别投递被断、
    # 且 title 失配时点击线程走「盲点跳过」抢在识别完成前关掉屏幕。用 _egg_reading 解耦后，
    # 只要处于彩蛋识别，就持续投递并严格等 _egg_read_done（或真正超时）才点关闭。
    EGG_OCR_STABLE_FRAMES = 3     # 彩蛋弹窗进入后等 N 帧（转场/动画稳定）再投递识别
    EGG_RESULT_CONFIRM_FRAMES = 2 # 连续 N 帧命中蛋数与已知最优持平判定「读完」（防首帧不完整被当真完成）
    EGG_OCR_TIMEOUT_FRAMES = 8    # 蛋识别失败/未配置时兜底：N 帧后照点关闭（≈2.4s），防卡死
    CLICK_RETRY_FRAMES_BY_KEY: dict[str, int] = {
        POPUP_HIGH_CONTINUE_KEY: POPUP_CONTINUE_RETRY_FRAMES,
        POPUP_REWARD_CONTINUE_KEY: POPUP_CONTINUE_RETRY_FRAMES,
    }
    APPRAISER_SETTLE_FRAMES = 5    # 进入「选择鉴宝师」后等 N 帧画面稳定再匹配判定（转场动画期卡片模糊，
                                   # 立即匹配分会低于阈值 → 误判未命中 → 直接 fallback 点中间卡）
    # ---------- 每日循环（GUI 控制面板配置项，运行期由 sidecar 注入）----------
    #   max_daily_loops: 今日刷到第几场为止。0 = 不指定（按游戏默认 50）；有效值 1~50。
    #       实际生效上限 = min(游戏每日上限 50, 本配置)；二者取更严。
    #       判定双保险：① 状态机回到「大厅/选场次」时 done 自增；② 场次选择页 OCR 读「日已参与 X/50」。
    #       任何一路先达上限 → 一律不点「开始匹配」。
    #       每日划分以凌晨 5 点为界（_refresh_daily_bucket：跨 5 点即新一天，计数清零）。
    #   出价策略：当前唯一「最大利润（刷单日计分）」，见 bid_strategy.STRATEGY_LABEL。
    DEFAULT_MAX_DAILY_LOOPS: int = 50
    DEFAULT_TREASURE_RISK_CAP: int = 50000   # 每局最多接受亏多少（兜底上限，GUI 可调）
    DEFAULT_TREASURE_MODE: str = "profit"    # profit=赚钱 / egg=赚蛋
    # 到限自动停止防抖：连续 N 帧在鉴宝大厅且判定到限，才视为可信并自动停止模块。
    # 防止单帧 OCR 误读（如「日已参与 X/50」瞬时多读）或阶段抖动造成提前停机。
    DAILY_LIMIT_STOP_STABLE_FRAMES = 3
    # OCR 异步化：主循环投递最新帧给 worker（latest-only 丢帧），识别在 worker 线程进行，
    # 不阻塞主循环。识别一轮 ~117ms（关 det + intra_op=4），worker 天然限速，无需节流。
    # 关键 ROI 优先通道（D 层兜底，见 docs/OCR_LATENCY_SPIKE_ANALYSIS.md 5.4）：
    # worker 先单独识别「最小高优先级集」，再跑全量。窗口期（偶发系统级慢）单 ROI 识别
    # 即使慢 15 倍也仅 ~200ms，age 仍在阈值内；全量 18 ROI 累加会超龄被丢弃。
    # 集取最小：bid_result_amount_box 驱动出价策略（R1 H 丢失元凶），必须保住；
    # bid_player4 = P4 双通道：报价从 P1→P4 逐条展示，P4 最晚出现、完整值稳定窗口最短
    #   （实测 486,70→486,700 仅隔 1 帧），必须最高刷新率——与 H 同走关键通道先落地。
    # 结算/分红金额属观测验证类，丢了影响小，不占关键通道。
    OCR_CRITICAL_KEYS: tuple[str, ...] = (
        "bid_result_amount_box",  # H 值（智能出价填入输入框）
        "bid_player4",            # P4 双通道（见上）
    )
    # 关键通道 ROI 集合（供 worker 第二段剔除，避免同帧 H/P4 被全量重复识别 + 覆盖关键结果）。
    _OCR_CRITICAL_SET: frozenset[str] = frozenset(OCR_CRITICAL_KEYS)
    # debug 落盘 IO worker 有界队列容量：IO 线程渲染+写盘 ~70-100ms/帧，主循环 wait_result
    # ~150ms/帧 → 队列几乎不积压；maxsize=8 足够缓冲瞬时尖峰。满则丢新任务（观测降密度）。
    IO_QUEUE_MAX = 8
    OCR_MAX_AGE_MS = 800.0   # 结果时效阈值：age = consume_time - frame_capture_time 超限即丢弃
    # 报价槽级固化（wait_result 阶段读 4 槽报价）：
    #   报价从上往下逐条展示（P1→P4），且所有人同时出价 → 报价数字会先显示「已出价」，
    #   再逐位刷新到完整值（实测 P4 从 486,70 → 486,700 只隔 1 帧，完整值稳定窗口仅 2 帧）。
    #   旧实现「读到值即写槽、四槽齐即固化」会把刷新动画的中间态（486,70）当最终报价固化。
    #   现改为「槽级固化」：每槽连续 BID_SLOT_STABLE_FRAMES 次读取一致 + 前置槽已有数据才
    #   固化该槽；已固化槽停止识别（OCR 资源集中给未固化槽，尤其最后展示的 P4）。
    #   未固化槽连续 BID_SLOT_MISS_LIMIT 次无输出 → 清空重读（防误读残留，-1 未读不激活）。
    BID_SLOT_STABLE_FRAMES = 3   # 槽固化：连续 N 次读取一致
    BID_SLOT_MISS_LIMIT = 3      # 槽清空：未固化连续 N 次无输出（已有值才计数）
    # 金额下限允许为 0 的字段：bid_result_amount_box 点击✖后输入框显示"0"是合法清空值，
    # settle_my_income / settle_profit = 0 也是合法值（0 分红 / 0 盈亏）
    # 默认 _extract_amount 的 MIN_AMOUNT=1万 会把 "0" 误滤成 None → 渲染显示 "-"
    OCR_ZERO_ALLOWED_KEYS: tuple[str, ...] = (
        "bid_result_amount_box", "settle_my_income", "settle_profit",
        "my_balance",
        # 玩家出价允许 0：掉线玩家的报价框显示 0，若 MIN_AMOUNT 默认 1万 把它滤成
        # None → _player_bids 该槽永远是初始值 → 快照 4 槽永远凑不齐 → 整场死锁（2026-08-19）。
        "bid_player1", "bid_player2", "bid_player3", "bid_player4",
    )

    def __init__(self, ctx: ActivityContext | None = None):
        # ctx=None → 离线模式：只初始化状态机，不启动运行时（worker/截图/主循环）
        if ctx is not None:
            super().__init__(ctx)
        else:
            self.ctx = None
        self._clicker = None  # 点击器（懒创建，绑定 ctx.hwnd；模式每次执行前同步自 ctx.click_mode）
        self._gp_bind_tried = False  # 手柄能力绑定只尝试一次（real 前台鼠标从不尝试，不呼出虚拟手柄）
        self._last_frame_rgb = None  # 最近一主循环帧（异步导航下主循环 PEEP 用，浅引用）
        self._action_centers: dict[str, tuple[float, float]] = {}  # 动作按钮归一化中心（运行态加载，离线为空）
        self._action_rect_sizes: dict[str, tuple[float, float]] = {}  # 动作按钮归一化宽高（手柄点击容差，运行态加载）
        self._current_stage: str | None = None
        # 落盘子域（DB 连接管理 + 场次/汇总写入 + 会话总结）
        self._store = TreasureStore(self)

        # --------- 观察状态（全部初始为 None/空，OCR 模块逐步填充）---------
        self._round_no: int | None = None            # 1~5 或 None(未进入回合)
        self._h_prices: list[int] = []               # 各回合「系统报价」：点「智能出价」后弹窗中心填入的金额，OCR 读到就 set_h
        self._our_bids: list[int] = []               # 各回合我方出价（由 _player_bids 我方槽位回退提供实测数据）
        self._player_bids: dict[str, list[int]] = {} # 其他玩家出价 {"玩家1":[R1,R2...], ...}
        self._my_rank: int | None = None             # 当前排名（1最高 / 4最低）
        # 我方槽位识别防抖：候选槽号 + 连续帧数（连续 RANK_STABLE_FRAMES 帧一致才 set_rank）
        self._rank_candidate: int | None = None
        self._rank_candidate_frames: int = 0
        self._note: str = ""                         # 备注（可手动设置，显示在HUD上）

        # --------- 结算页结果（竞拍结束/领取分红阶段识别）---------
        self._settle_final_price: int | None = None   # 最终竞拍价（谁出价最高拿下的实际金额）
        self._settle_total_price: int | None = None   # 拍品总价（系统给出的估值锚，通常 = sysmax×1.33~1.4）
        self._settle_profit: int | None = None        # 利润（中标者的盈亏，负数=中标者亏钱）
        self._settle_my_income: int | None = None     # 本场收入（我方分红/收益，正数=我赚）
        self._daily_high_score: int | None = None     # 结算弹窗①「今日最高积分上涨」的积分值（单日最高利润刷新时记录，仅记录不参与决策）
        self._auction_result: str | None = None       # 中标/未中标（result_banner 横幅模板命中："win"/"fail"，落盘用）

        # --------- 我方金币余额（出价面板右上角 HUD）---------
        # 规则：进入 R1 后识别到第一次有效正数就锁定，之后不再覆盖。
        # 原因：出价操作不扣除余额（余额在结算时才变动），程序不需要关心余额怎么变。
        self._my_balance: int | None = None           # 当前可用金币，作为出价预算上限参考
        self._balance_locked = False                  # True 后：任何 my_balance OCR 值都不再写入

        # --------- OCR 识别（异步 worker）---------
        self._ocr: TreasureOcr | None = None
        # --- worker 生命周期 ---
        self._ocr_stop = threading.Event()    # worker 停止信号（shutdown）
        self._ocr_wakeup = threading.Event()  # 新帧到达唤醒信号（无 queue，latest-only）
        self._ocr_thread: threading.Thread | None = None
        # --- 投递槽（主线程写 / worker 取）---
        self._ocr_lock = threading.Lock()     # 保护两个槽：latest 帧 + 结果
        self._ocr_pending: tuple[int, int | None, np.ndarray, float, str, frozenset[str] | None] | None = None
        # (frame_id, round_no, frame, captured_ts, task, keys)，frame 为副本，captured_ts=投递时刻≈帧捕获时刻
        self._ocr_frame_id = 0                # 单调递增投递序号（仅主线程写）
        # --- 结果槽（worker 写 / 主线程消费）---
        # 双槽：关键通道（_ocr_result_critical，第一段 H+P4）与全量通道（_ocr_result，第二段
        # 其余 ROI）独立发布、独立消费。修复 P4 双通道覆盖 bug：第一段结果不再被第二段整体覆盖，
        # P4 每帧由关键通道独立识别、优先落地（docs/P4_DUAL_CHANNEL_ANALYSIS.md §3 困难二）。
        self._ocr_result_critical: dict | None = None  # {frame_id, round_no, captured_ts, ..., data}
        self._ocr_result: dict | None = None  # {frame_id, round_no, captured_ts, completed_ts, duration_ms, data}
        # --- 可观测指标（worker 写 / 主线程 DEBUG 读）---
        self._ocr_total_runs = 0
        self._ocr_failures = 0
        self._ocr_duration_ms = 0.0           # 最近一次识别耗时
        self._ocr_source_frame_id = 0         # 最近一次应用结果的来源帧
        self._ocr_result_age_ms = 0.0         # 最近一次应用结果时的时效（丢帧/延迟观测）

        # --- debug 落盘 IO worker（生产-消费者，渲染+imwrite 移出主线程）---
        # 目标：wait_result 段主循环帧间隔真正逼近 WAIT_RESULT_FAST_MS（渲染 ~30ms +
        # webp 存盘 ~63ms 曾把实际帧率拖回 ~240ms，见 docs/P4_DUAL_CHANNEL_ANALYSIS.md §3 困难一）。
        # 主线程只打包 (frame copy + 当帧 state 快照) 入队，渲染/写盘全部在 IO 线程执行。
        self._io_queue: Queue | None = None   # 有界队列；满则丢新任务（观测降密度，不阻塞主循环）
        self._io_stop = threading.Event()
        self._io_thread: threading.Thread | None = None

        # --------- debug 目录 & 元数据 ---------
        self._debug_root: Path | None = None         # debug/treasure/
        self._session_dir: Path | None = None        # debug/treasure/<ts>/
        self._raw_dir: Path | None = None            # debug/treasure/<ts>/raw/
        self._trace_writer: TraceWriter | None = None
        self._nav_graph: NavGraph | None = None
        self._saved_frames = 0                       # 已保存的 raw 帧数（全量，每帧 +1）
        self._debug_saved = 0                        # 已保存的 rendered（debug 图）帧数（全量，每帧 +1）

        # --------- 回合切换转场期计数 ---------
        # 每帧自增，set_stage 回合号变化时重置为 0；< SWITCH_CONFIRM_FRAMES 视为转场期
        self._round_elapsed = 0

        # --------- 画面变化检测 ---------
        self._prev_gray: np.ndarray | None = None
        self._last_change_ts: float = 0.0

        # --------- 日志限流 ---------
        self._frame_counter = 0
        self._last_stage_logged: str | None = None

        # --------- 主循环单帧异常兜底 ---------
        # _tick_once 任一帧抛异常不得直接杀死主循环（截图/识别单帧偶发失败很常见），
        # 跳过该帧继续；但连续失败过多说明是系统性问题（窗口关闭/帧彻底无响应），
        # 达到上限 _MAIN_CRASH_RETRY_MAX 则照常上抛，让模块停止，避免"静默空转"掩盖真 bug。
        self._main_crash_frames = 0   # 连续抛异常的帧计数（任一正常帧归零）

        # --------- 阶段切换类点击重试状态 ---------
        self._click_retry_key: str | None = None    # 正在等待"切换阶段"的 key
        self._click_retry_stage: str | None = None  # 点击时所在阶段（阶段切走即成功）
        self._click_retry_since_ts: float = 0.0     # 最近一次点击成功的时刻（重试超时计时，与帧率解耦）
        self._click_retry_count: int = 0            # 已重试次数（达 CLICK_RETRY_MAX 停手）
        # 进入「选择鉴宝师」阶段时的帧计数（转场稳定缓冲用，见 APPRAISER_SETTLE_FRAMES）
        self._appr_enter_frame: int = 0

        # --------- 阶段检测器 + 过滤层状态 ---------
        self._detector: TreasureStageDetector | None = None
        self._det_stage: str | None = None        # 过滤后阶段
        self._det_round: int | None = None        # 过滤后回合
        self._cand_stage: str | None = None       # 候选阶段（防抖计数）
        self._cand_round: int | None = None
        self._cand_count = 0
        # 最新一帧 detector 原始返回（未经过滤层处理）：用于区分"当前帧真的在该阶段"
        # vs "检测器没认出任何阶段，_current_stage 沿用旧值"（典型：选择主题阶段 →
        #   _current_stage 仍停在"选择鉴宝师"，但 _last_raw_stage = None）。
        self._last_raw_stage: str | None = None
        self._last_raw_round: int | None = None
        self._last_detection_result = None

        # --------- 统一底座接入点（P2b+，保留运行时供给，不替换主路径）---------
        # StageTracker：阶段记录/断点换算的单一事实来源。运行时仍用既有 STAGE_ORDER +
        # set_stage，本 tracker 供断点解析与未来的 DebugStudio/调试统一口径（P4/P5 迁移）。
        self._stage_tracker: StageTracker | None = None

        # --------- 鉴宝师选择自动化 ---------
        # 模板缓存：[(priority, key, gray_ndarray)]，顺位升序；空列表 = 未加载或无可用模板
        self._appr_tpls: list[tuple[int, str, np.ndarray, tuple[float, float, float, float], float]] = []
        # 「已选中」对勾模板（黄色√，卡片右上角）：灰度图或 None（未配置/加载失败）
        self._check_tpl: np.ndarray | None = None
        # 对勾扫描区域 rect（归一化，来自 stage.appraiser_selected_check）；随模板一起加载
        self._check_rect: tuple[float, float, float, float] | None = None
        # 上一次"点击意图"结果：供 peep 准星显示 {"key","center","hint","score"}|None，
        # 中心为归一化坐标 (cxn, cyn)。由 _run_appraiser_choice 每帧重算。
        self._appr_last_decision: dict | None = None

        # --------- 场次选择（鉴宝大厅(选择场次)）---------
        # 「开始匹配」按钮模板缓存：[(priority, key, gray, rect_norm)]，顺位升序；
        # 用于判定"详情卡已切到目标场次、按钮已出现在屏幕上"。
        # 命中 → 点 session_start_match_btn（actions 段 rect 中心）；未命中 → 先点目标场次 badge。
        self._session_panel: list[tuple[int, str, np.ndarray, tuple[float, float, float, float]]] = []
        # 上一次"点击意图"结果：{"key","center","hint","score"}|None，由 _run_session_choice 每帧重算
        self._session_last_decision: dict | None = None
        # 点击「开始匹配」后的冷却帧计数：点完 N 帧内不产出新意图，避免检测器还没切阶段
        # 就回退点 badge（时序问题：按钮已消失但阶段仍是鉴宝大厅）。
        self._session_start_cooldown: int = 0
        # 结算后弹窗（今日最高/彩蛋）点击关闭后的冷却帧计数：点关闭后弹窗消失动画期
        # 模板匹配不上，冷却帧内不产出新点击意图，等动画稳定再识别（2026-08-16）。
        self._popup_click_cooldown: int = 0
        # 决策策略（P1：policies 决策规则数据化；P1e：policies 是唯一决策源）：
        #   _policy_plan = v3 assets policies → PolicyPlan（启动编译不可变，缺失/非法 = 启动失败）
        self._policy_plan = None
        self._policy_snapshot: dict | None = None  # 最近一帧 DecisionSnapshot（trace 决策契约）
        # 帧内意图缓存：_resolve_action_target 每帧只允许真正决策一次（主循环 +
        # _treasure_kwargs 的 peep 准星字段都会调用；同帧二次调用返回缓存）。
        # 无缓存时同帧双决策会把重试意图覆盖成等待（副作用已执行、intent 丢弃），
        # 实测领取分红跳过动画重试链被整体吞掉 → 卡动画不重试（2026-09-06）。
        self._intent_cache_frame: int = -1
        self._intent_cache: dict | None = None

        # P1 收编：感知匹配阈值/ROI 真源 = policies.tuning.perception（缺省回落代码常量）。
        _per = _perception_tuning()
        self._appraiser_search_roi = (
            tuple(_per["appraiser_search_roi"]) if _per.get("appraiser_search_roi") else _APPRAISER_SEARCH_ROI
        )
        self._appraiser_match_threshold = float(
            _per.get("appraiser_match_threshold", _APPRAISER_MATCH_THRESHOLD)
        )
        self._check_match_threshold = float(
            _per.get("check_match_threshold", _CHECK_MATCH_THRESHOLD)
        )
        self._session_match_threshold = float(
            _per.get("session_match_threshold", _SESSION_MATCH_THRESHOLD)
        )
        self._smart_bid_match_threshold = float(
            _per.get("smart_bid_match_threshold", _SMART_BID_MATCH_THRESHOLD)
        )
        _exec = _policy_tuning().get("execution") or {}
        self._click_cooldown_s = float(
            _exec.get("click_cooldown_s", self.CLICK_COOLDOWN_S)
        )
        _pol = _policy_tuning().get("policy") or {}
        self._session_start_click_cooldown_frames = int(
            _pol.get("session_start_click_cooldown_frames", SESSION_START_CLICK_COOLDOWN_FRAMES)
        )
        self._click_retry_frames = int(_pol.get("click_retry_frames", self.CLICK_RETRY_FRAMES))
        self._click_retry_max = int(_pol.get("click_retry_max", self.CLICK_RETRY_MAX))
        self._popup_click_cooldown_frames = int(
            _pol.get("popup_click_cooldown_frames", self.POPUP_CLICK_COOLDOWN_FRAMES)
        )
        self._popup_continue_retry_frames = int(
            _pol.get("popup_continue_retry_frames", self.POPUP_CONTINUE_RETRY_FRAMES)
        )
        self._retry_frames_by_key = {
            self.POPUP_HIGH_CONTINUE_KEY: self._popup_continue_retry_frames,
            self.POPUP_REWARD_CONTINUE_KEY: self._popup_continue_retry_frames,
        }
        # 弹窗链回退连续稳定帧计数（_accept_stage 用，独立于冷却）：
        # 累计识别到「弹窗链阶段 → 大厅/选场次」的帧数，达 POPUP_LOOPBACK_STABLE_FRAMES
        # 才放行回退；弹窗链外（非回退场景）重置为 0。
        self._popup_loopback_frames: int = 0

        # --------- 回合出价（第N回合出价）---------
        # 出价面板「智能出价」按钮模板 + 扫描 rect（stage.smart_bid_btn）：
        # 面板打开 → 该按钮出现 → 模板命中 = 面板已开（S3）；中心作点击目标。
        # 主按钮状态（等待出价/出价）走 OCR 文字识别（ocr.bid_main_btn_label），不挂模板
        # （按钮明暗态模板匹配不稳，见 Experience 1112416）。
        self._bid_smart_tpl: np.ndarray | None = None
        self._bid_smart_rect: tuple[float, float, float, float] | None = None
        # 上一次"点击意图"结果：{"state","key","center","hint","score"}|None，由 _run_bidding_choice 每帧重算
        self._bidding_last_decision: dict | None = None

        # --------- 出价策略（v0.3.5）---------
        # 上一轮完整公开快照（策略唯一对手信息源；附加回合覆盖 _player_bids 槽不影响它）
        self._last_round_snapshot: RoundSnapshot | None = None
        # 出价策略决策器（内部维护逼价基线 _lure_state）
        self._strategy: BidStrategy | None = None
        # 待生效的策略配置：set_module_config 在策略实例创建前被调用（controller.start_module
        # 注入早于 module.start() 创建 BidStrategy），此时 _strategy 为 None，
        # 直接写实例字段避免 mode/risk_cap 被静默丢弃回退默认值。
        self._treasure_mode: str = self.DEFAULT_TREASURE_MODE
        self._treasure_risk_cap: int = self.DEFAULT_TREASURE_RISK_CAP
        # bidding epoch 时序（phase 门控，见文档 §13）：
        #   wait_first  = 等待第 1 次出价（无快照，R1 首轮）
        #   wait_next   = 等待下一次出价（已有完整快照，含附加回合）
        #   bidding     = 出价中（决策 → 改输入框 → 确认）
        #   wait_result = 已提交，等待公开结果
        self._bid_epoch: int = 0
        self._bid_phase: str = "wait_first"
        self._panel_open: bool = False        # 上一帧出价面板是否可交互（稳定判定后）
        self._panel_stable_frames: int = 0    # 连续命中 smart_bid_btn 的帧数；< PANEL_OPEN_MIN_STABLE_FRAMES 不认面板真开
        # 4 个玩家出价区状态（OCR bid_playerX 区域 text 判定）：pid → True=已出价/False=出价中
        # 仅作 wait_result 内「假下降沿」兜底校验，不作进入 wait_result 的硬门槛
        # （出价区切换有动画，动画期会误读，硬门槛会误拒正常提交）。
        self._bid_player_submitted: dict[int, bool] = {}
        self._wait_result_frames: int = 0     # 进入 wait_result 后的累计帧数（日志/统计用）
        self._wait_result_entered_ts: float = 0.0  # 进入 wait_result 的时刻（假下降沿缓冲计时，见 SUBMIT_ANIMATION_BUFFER_MS）
        # 报价槽级固化状态（wait_result 读 4 槽报价）：pid → {val, stable, locked, miss,
        # consumed, output, hits}
        #   val=-1 未读；stable=连续一致帧数；locked=已固化（停止该槽 OCR）；
        #   miss=连续无输出帧数（未固化+已读值 才计数，≥BID_SLOT_MISS_LIMIT 清空重读）；
        #   consumed/output/hits = 本回合该槽「消费/输出/命中」三口径（debug 图显示，见 _reset_bid_slots）。
        # 每回合首次消费时由 _consume_ocr_result 重置（对比 _bid_slots_round）。
        self._bid_slots: dict[int, dict] = {}
        self._bid_slots_round: int | None = None   # _bid_slots 对应的回合号（回合变化即重置）
        # 输入子状态：进入输入流程后的推进（clear → 逐位输入 → 确认）
        self._bid_input_progress: int = 0
        # 输入框当前值（bid_result_amount_box 最新读值，不锁定；供输入子状态机对比目标价 T）
        self._bid_input_latest: int | None = None
        # 输入进度锚点：已正确输入的目标价前缀长度。OCR 中间态短读（少读一位）不回退锚点，
        # 避免"已输 2633 → OCR 读 263 → 重复输 T[3]"的错乱；锚点只在 B 前缀真正变长时前进。
        # 仅在准星意图模式（程序不能执行点击、必须靠 OCR 反馈确认用户动作）下才有意义——
        # 程序自记"应输到第几位"，OCR 只负责"前进确认"和"完成后校验"（输入完成后再识别数字）。
        self._bid_input_progress: int = 0
        # 确认态防抖计数：B==T 已就位后 OCR 连续读到 ≠T 的帧数（≥BID_CONFIRM_STABLE_FRAMES 才重置）
        self._bid_confirm_streak: int = 0
        # --------- 问题1：选鉴宝师过场静默标记（点过确认鉴宝师后，过场动画不再发 fallback 准星）---------
        self._appraiser_confirmed_once: bool = False
        # --------- 问题5：领取分红"跳过动画点一次"标记，防连点 ---------
        self._settle_collect_clicked_once: bool = False
        # 跳过动画点击无响应兜底状态（点击成功 → 计时；收入读出/换场/切阶段归零）：
        self._settle_skip_since: int = 0            # 最近一次跳过动画点击成功的帧号
        self._settle_skip_retry_count: int = 0      # 无响应重试次数（达 SETTLE_SKIP_RETRY_MAX 终止）
        # --------- 结算后弹窗（今日最高/奖励彩蛋）状态 ----------
        self._egg_counts: dict[str, int] | None = None  # 本场彩蛋 {red,yellow,blue}（仅记录，Phase2 填充）
        self._egg_read_done: bool = False              # 彩蛋数量已读完（稳定确认后置位）
        self._egg_best_result: dict | None = None      # 历史最优彩蛋识别结果（按命中蛋种数取优）
        self._egg_best_streak: int = 0                 # 命中蛋数与最优持平的连续帧数（EGG_RESULT_CONFIRM_FRAMES 判定用）
        self._egg_reading: bool = False                # 彩蛋识别进行中（进入彩蛋弹窗后置位，解耦投递/点击与 title 实物命中）
        self._reward_enter_frame: int = 0              # 进入奖励结算的帧号（点击门控计时基准）
        self._egg_recognizer: EggRewardRecognizer | None = None  # 彩蛋识别器（start 时懒加载）
        # 彩蛋识别结果槽（worker 写 / 主线程 _apply_egg_result 消费）：
        # {frame_id, captured_ts, completed_ts, duration_ms, data}
        # data = recognize() 返回值 | None（识别异常）。复用 OCR worker 线程执行（task="egg"），
        # 主线程零阻塞；结果仅记录用途，超时兜底在 _decide_action（EGG_OCR_TIMEOUT_FRAMES）。
        self._egg_result: dict | None = None
        # --------- 结构化落盘（%APPDATA%/MaaRacingAssistant/data/treasure/treasure.db，凌晨5点日界）----------
        self._data_dir: Path | None = None            # 用户数据目录 treasure/（start 时初始化）
        # --------- 真实点击（v0.4）：边沿触发指纹锁 + 限速 ---------
        # 指纹 = (key, state, 归一化中心四舍五入[, 输入位锚点])；点击成功后才更新，
        # 相同意图持续存在时只点一次；数字键带输入位锚点区分连续相同数字（如 11 的第二个 1）。
        self._last_click_fingerprint: tuple | None = None
        self._last_click_time: float = 0.0
        # 异步点击（2026-09-03 导航线程化）：已提交但结果未消费的点击上下文
        # {key, state, fp, center, mode_label, stage}；consume 成功/失败后清空。
        self._pending_click: dict | None = None

        # --------- 每日循环次数限制（GUI 配置，可 1~50 / 0=不指定）----------
        # 双保险：① done_count（完成结算→回到大厅 自增）② OCR 读「日已参与 X/50」。
        # 任一 ≥ effective_limit 就不再点「开始匹配」。每日以凌晨 5 点为界。
        self._max_daily_loops: int = self.DEFAULT_MAX_DAILY_LOOPS
        self._target_session: str = DEFAULT_TARGET_SESSION  # GUI 选的目标场次（intern/expert/master）
        self._session_daily_done_count: int = 0   # 状态机侧计数（完成场次回到大厅时自增）
        self._session_daily_ocr_count: int | None = None  # OCR 侧读到的「日已参与 X」
        # 上一次阶段（用于 set_stage "结算→大厅/选场次" 跳变时计数 +1；
        # 同一新场次多次 set_stage 相同不重复计数）。
        self._prev_stage_for_loop_count: str | None = None
        # 当前"日"桶（凌晨 5 点为界）：跨桶 → 当日计数清零重计。
        self._daily_bucket: str | None = None
        # 到限自动停止防抖计数：连续 N 帧在鉴宝大厅且判定到限才 request_stop（见 _tick_once）
        self._daily_limit_streak: int = 0

    # ==================================================================
    #  对外：配置接口（GUI → sidecar → module；运行中可改，立即生效到下一轮决策/下一次点开始匹配）
    # ==================================================================

    def get_module_config(self) -> dict:
        """返回当前 treasure 模块配置（给 GUI 读显 + 初值回填）。"""
        self._refresh_daily_bucket()
        tgt = self._target_session if self._target_session in TARGET_SESSION_OPTIONS else DEFAULT_TARGET_SESSION
        _, tgt_label = TARGET_SESSION_OPTIONS[tgt]
        return {
            "max_daily_loops": int(self._max_daily_loops),
            "target_session": tgt,                     # intern/expert/master
            "target_session_label": tgt_label,         # 中文名（前端显示）
            "bid_strategy_label": STRATEGY_LABEL,      # 当前唯一策略显示名（前端只读展示）
            "treasure_risk_cap": int(getattr(self._strategy, "risk_cap", self._treasure_risk_cap) or self._treasure_risk_cap),
            "treasure_mode": getattr(self._strategy, "mode", self._treasure_mode),
            "_state": {
                # 运行时实况（只读）：已完成多少场 / 上限值，用于 HUD 展示
                "daily_bucket": self._daily_bucket,
                "done_count_state": int(self._session_daily_done_count),
                "done_count_ocr": (
                    int(self._session_daily_ocr_count) if self._session_daily_ocr_count is not None else None
                ),
                "effective_limit": int(self._effective_daily_loop_limit()),
            },
        }

    def set_module_config(self, config: dict) -> dict:
        """写配置（做参数钳制，非法值静默修正并返回最终值）。"""
        if not isinstance(config, dict):
            return self.get_module_config()
        # max_daily_loops: 0=不指定(默认50), 1~50 有效；负数→0；超过 50→50。
        if "max_daily_loops" in config:
            try:
                v = int(config["max_daily_loops"])
            except (TypeError, ValueError):
                v = self.DEFAULT_MAX_DAILY_LOOPS
            if v < 0:
                v = 0
            if v > 50:
                v = 50
            self._max_daily_loops = v
        # target_session: 仅允许 TARGET_SESSION_OPTIONS 的 key；非法→回退默认。
        if "target_session" in config:
            v = config["target_session"]
            if isinstance(v, str) and v in TARGET_SESSION_OPTIONS:
                self._target_session = v
            else:
                self._target_session = DEFAULT_TARGET_SESSION
        # treasure_risk_cap: 每局最多接受亏多少（兜底上限）；正整数，非法→默认 5 万。
        if "treasure_risk_cap" in config:
            try:
                v = int(config["treasure_risk_cap"])
            except (TypeError, ValueError):
                v = self.DEFAULT_TREASURE_RISK_CAP
            if v < 0:
                v = self.DEFAULT_TREASURE_RISK_CAP
            if v > 10_000_000:
                v = 10_000_000
            self._treasure_risk_cap = v                    # 先存实例字段（策略实例可能尚未创建）
            if self._strategy is not None:                 # 运行中则立即同步到当前策略
                self._strategy.risk_cap = v
        # treasure_mode: profit（赚钱）/ egg（赚蛋）；非法→profit。
        if "treasure_mode" in config:
            v = config["treasure_mode"]
            if isinstance(v, str) and v in ("profit", "egg"):
                self._treasure_mode = v                    # 先存实例字段（策略实例可能尚未创建）
                if self._strategy is not None:             # 运行中则立即同步到当前策略
                    self._strategy.mode = v
        return self.get_module_config()

    # ---------- 内部：每日循环上限（0=不限 时返回 50，因为游戏本身也有 50 场天花板）----------
    def _effective_daily_loop_limit(self) -> int:
        cfg = self._max_daily_loops if self._max_daily_loops and self._max_daily_loops > 0 else 50
        return min(50, max(0, cfg))

    def _daily_loop_limit_reached(self) -> bool:
        """是否已到「今日不再点开始匹配」的阈值。双保险任一到线即 True。"""
        self._refresh_daily_bucket()   # 跨凌晨5点自动开新一天，计数清零
        lim = self._effective_daily_loop_limit()
        # ① 状态机侧：本场结束 + 回大厅/选场次 计过的数量
        if self._session_daily_done_count >= lim:
            return True
        # ② OCR 侧：屏幕上读到「日已参与 X/50场」的 X
        if self._session_daily_ocr_count is not None and self._session_daily_ocr_count >= lim:
            return True
        return False

    # ==================================================================
    #  ActivityModule 基类抽象实现
    # ==================================================================

    @property
    def current_stage(self) -> str | None:
        return self._current_stage

    # ---------------- v4 执行通路（P2a-Q4，NAVKIT_SOURCE=v4 启用） ----------------

    _V4_ENTRY = "treasure.__boot.dwell"  # 起跑汇聚节点：任意 stage 自适应（v3 语义）

    @staticmethod
    def _v4_enabled() -> bool:
        """v4 通路开关：沿用 NAVKIT_SOURCE env（与 v2/v3 切换同款机制）。"""
        import os
        return os.environ.get("NAVKIT_SOURCE", "v3").lower() == "v4"

    def _run_v4_loop(self) -> None:
        """v4 执行通路：帧工作全部在 MaaFW Tasker 线程（PolicyBridge 桥内
        `_tick_once`，帧节律 = policy_loop.rate_limit=300ms），本线程退化为
        健康守护——常驻图意外退出时告警并重启。"""
        from pathlib import Path as _Path
        from maaracing_assistant.core.nav_graph import NavKitV4
        from maaracing_assistant.plugins.treasure.policy_bridge import (
            POLICY_ACTION_NAME, PolicyBridge,
        )
        plugin_root = _Path(__file__).resolve().parent  # module.py 直接父目录 = plugins/treasure
        repo_root = plugin_root.parents[2]  # 仓库根：treasure → plugins → 包目录 → 根
        # C 句柄对象（Resource/Tasker/CustomController）全程驻留、跨重启复用：
        # binding __del__ 不等 C++ worker 收摊，模块结束即 GC 会竞态崩溃
        # （0xC0000005，P2b 实验 run13 定案）。
        runner = getattr(self, "_v4_runner", None)
        if runner is None:
            runner = self._v4_runner = NavKitV4(
                self.ctx,
                pipeline_dirs=[
                    repo_root / "maaracing_assistant" / "core" / "resources" / "pipeline",
                    plugin_root / "resources" / "pipeline",
                ],
                image_dirs=[plugin_root / "resources" / "image"],
                bridges=[(POLICY_ACTION_NAME, PolicyBridge(self))],
            )
        if not runner.start(self._V4_ENTRY):
            raise RuntimeError("[鉴宝][v4] 常驻图加载失败，模块终止")
        logger.log("[鉴宝][v4] 帧工作已移交 MaaFW Tasker 线程（policy 闭环桥）")
        from contextlib import ExitStack
        from maaracing_assistant.core.capabilities import BUTTON_A
        with ExitStack() as stack:
            # v4 常驻图永不退出：手柄租约全程持有（与 v3「跑图期间持、结束归还」
            # 不同——桥线程点击依赖手柄，中途归还即断点击）。runner.stop() 在
            # 租约归还前执行，确保 Tasker 线程先停。
            if self.ctx.click_mode == "gamepad":
                gpad = stack.enter_context(self.ctx.gamepad.acquire())
                runner._graph._ensure_clicker().bind_gamepad(
                    self.ctx.capture, gpad, confirm_button=BUTTON_A)
                logger.log("[鉴宝][v4] 手柄租约已绑定（常驻持有）")
            try:
                while self.ctx.lifecycle.running:
                    if not runner.poll():
                        logger.log("[鉴宝][v4] 常驻图已退出，尝试重启", "WARNING")
                        runner.start(self._V4_ENTRY)
                    self.ctx.lifecycle.sleep(1.0)
            finally:
                runner.stop()

    def start(self, start_from: str | None = None) -> None:
        """启动鉴宝模块（观察模式）：持续截图 + 日志，不做任何操作"""
        # 0. 离线模式（ctx=None 只初始化状态机）不允许启动
        assert self.ctx is not None, "离线模式（ctx=None）不可调用 start()"
        # 1. 连接窗口
        if not self.ctx.connect():
            logger.log("[鉴宝] 窗口连接失败，模块终止", "ERROR")
            return

        # 1.05 只校验比例、不调整窗口/分辨率：客户区应大致 16:9（模板与 ROI 均按
        #     720p(16:9) 归一化，其他比例如 16:10 / 21:9 / 4:3 会识别错位）→ 不符报错退出
        if not check_game_window_aspect(self.ctx.hwnd):
            logger.log(
                "游戏窗口不是 16:9 比例（模板与识别区域均按 720p(16:9) 设计，其他比例会识别错位）。"
                "请将游戏窗口调整为 16:9 后重新开始，模块已终止", "ERROR",
            )
            return

        # 2. 安装调试渲染器（生命周期由 Context 的 ExitStack 接管）
        self.ctx.enter_context(
            self.ctx.debug_renderer.renderer(TreasureDebugRenderer(self.ctx.debug)))

        # 2.5 初始化 OCR 识别器（懒加载引擎，失败自动降级）——先于检测器创建，
        #     检测器的回合小字兜底（OCR 读回合数）需要复用同一引擎实例
        self._ocr = TreasureOcr(self.ctx.proj)

        # 2.51 初始化阶段检测器（回合小字兜底走 OCR）
        self._detector = TreasureStageDetector(self.ctx.proj, ocr=self._ocr)

        # 2.52 初始化彩蛋识别器（奖励结算(彩蛋)弹窗：图标匹配 + 下方 OCR 计数）。
        # 模板/rect 未配置时识别器内部跳过 → recognize 返回 None → 奖励结算走超时点关闭，
        # 不会卡流程（与检测器同样的"缺失即降级"约定）。
        self._egg_recognizer = EggRewardRecognizer(self.ctx.proj, ocr=self._ocr)

        # 2.53 结构化落盘：%APPDATA%/MaaRacingAssistant/data/treasure/treasure.db
        # （games 明细 + daily_summary 汇总，SQLite 标准库零依赖；用户数据目录与安装目录解耦，更新不丢数据）
        self._data_dir = data_dir() / "treasure"
        self._store.ensure_db()

        # 2.55 加载动作按钮（准星模式用）：JSON rect → 归一化中心点 + 宽高（手柄容差用）
        self._action_centers, self._action_rect_sizes = _load_action_centers(self.ctx.proj)
        if not self._action_centers:
            logger.log("[鉴宝] 未加载到动作按钮 rect，准星模式将不可用", "WARNING")

        # 2.551 决策策略（P1：policies 数据化）。v3 资产缺 policies = 启动失败（P1e）；
        # v2 回退路径（NAVKIT_SOURCE=v2 或 v3 缺失）下策略栈降级为 LegacyPolicy。
        self._init_policy_stack()

        # 2.56 加载鉴宝师头像模板（顺位匹配用；定义源=JSON appraisers 段，调试台可调）
        self._appr_tpls = _load_appraiser_templates(self.ctx.proj)
        if self._appr_tpls:
            names = ", ".join(f"P{p}={k}" for p, k, _, _, _ in self._appr_tpls)
            logger.log(f"[鉴宝] 已加载鉴宝师模板: {names}", "DEBUG")
        else:
            logger.log("[鉴宝] 未加载任何鉴宝师头像模板（选择鉴宝师阶段将用点中心兜底）", "WARNING")

        # 2.561 加载「已选中」对勾模板 + 扫描 rect（选中判定用；缺失则跳过选中判定）
        _ck = _load_selected_check(self.ctx.proj)
        if _ck is not None:
            self._check_tpl, self._check_rect = _ck
            logger.log(f"[鉴宝] 已加载「已选中」对勾模板（扫描 rect={self._check_rect}）", "DEBUG")
        else:
            self._check_tpl, self._check_rect = None, None
            logger.log("[鉴宝] 未加载「已选中」对勾模板（选中判定禁用，仅指向目标头像）", "DEBUG")

        # 2.57 加载场次选择「开始匹配」按钮模板（详情卡出现判定用；
        #     命中 → 点 session_start_match_btn；未命中 → 先点目标场次 badge 切换详情卡）
        self._session_panel = _load_session_panel(self.ctx.proj)
        if self._session_panel:
            names = ", ".join(f"P{p}={k}" for p, k, _, _ in self._session_panel)
            logger.log(f"[鉴宝] 已加载「开始匹配」按钮模板: {names}", "DEBUG")
        else:
            logger.log("[鉴宝] 未加载「开始匹配」按钮模板（降级：始终先点目标场次 badge，再点开始匹配位置）", "WARNING")

        # 2.58 加载出价面板「智能出价」按钮模板（面板打开判定用，截图3）
        _sb = _load_smart_bid_btn(self.ctx.proj)
        if _sb is not None:
            self._bid_smart_tpl, self._bid_smart_rect = _sb
            logger.log(f"[鉴宝] 已加载智能出价按钮模板（扫描 rect={self._bid_smart_rect}）", "DEBUG")
        else:
            self._bid_smart_tpl, self._bid_smart_rect = None, None
            logger.log("[鉴宝] 未加载智能出价按钮模板（面板已开判定降级：依赖主按钮 OCR 兜底）", "WARNING")

        # 2.59 初始化出价策略决策器（V2：数据驱动双层缓冲 + 兜底上限 + 赚钱/赚蛋模式）
        # 用 _treasure_* 暂存字段而非 DEFAULT：set_module_config 可能在实例创建前注入
        # （controller.start_module 早于 module.start()），若回落默认会让 GUI 选的模式被静默丢弃。
        self._strategy = BidStrategy(
            risk_cap=self._treasure_risk_cap,
            mode=self._treasure_mode,
        )
        logger.log(
            f"[鉴宝] 出价策略决策器已初始化: 策略={STRATEGY_LABEL} "
            f"(VAL_COEF={self._strategy.VAL_COEF:.2f}, 利润线={self._strategy._profit_floor():.2f}, "
            f"兜底上限={self._strategy.risk_cap:,}, 模式={self._strategy.mode})；"
            f"每日循环上限={self._effective_daily_loop_limit()}场",
            "DEBUG",
        )

        # 2.6 启动异步 OCR worker
        self._start_ocr_worker()

        # 3. 建立本次会话调试目录
        self._prepare_debug_dirs()

        # 3.1 启动 debug 落盘 IO worker（仅 debug/peep 开启时有任务；全关不启动空转线程）。
        #     渲染 + raw/rendered 写盘移出主线程，wait_result 段帧率不再被存盘拖慢。
        if self.ctx.debug.enabled or self.ctx.debug.peep_enabled:
            self._start_io_worker()

        # 4. 解析断点（换算收敛到统一底座 StageTracker，先 in 判断保护非法值回退 0）
        self._stage_tracker = StageTracker(self.STAGE_ORDER)
        if start_from and start_from in self.STAGE_ORDER:
            skip_until_idx = self._stage_tracker.resolve_start_from(start_from)
            self._current_stage = self.STAGE_ORDER[skip_until_idx]
            raw_r = self._extract_round_from_stage(self._current_stage)
            self._round_no = min(raw_r, 5) if raw_r is not None else None
            logger.log(f"[鉴宝] 从断点开始: 「{self._current_stage}」(跳过前{skip_until_idx}个阶段)")
        else:
            self._current_stage = self.STAGE_ORDER[0]
            self._round_no = None

        self._log_stage_changed(self._current_stage, "<启动>")

        logger.log("[鉴宝] 模块启动：截图 + 记录 + 选鉴宝师自动化（其余阶段不操作）")
        if self._session_dir:
            logger.log(f"[鉴宝] 调试截图目录: {self._session_dir}", "DEBUG")
        else:
            logger.log("[鉴宝] 调试模式未开启（可在GUI打开Debug开关），仅运行日志 + PEEP（如果开启）", "DEBUG")

        # 5. 主循环（纯观察）
        # 单帧异常兜底：_tick_once 抛异常不杀死主循环，跳过该帧继续；连续失败达
        # _MAIN_CRASH_RETRY_MAX 帧才照常上抛，让异常走 finally 清理后终止模块（防静默空转）。
        # 例外：ClickRetryExhaustedError（阶段切换点击重试耗尽）直接穿透终止，
        # 不等待连续帧计数——重试失败是明确的系统性问题，应立即停止而非跳过。
        _MAIN_CRASH_RETRY_MAX = 30   # 30 帧 ≈ 9s（主循环 ~300ms/帧）
        try:
            if self._v4_enabled:
                self._run_v4_loop()
                return
            while self.ctx.lifecycle.running:
                try:
                    self._tick_once()
                except ClickRetryExhaustedError:
                    raise  # 重试耗尽：穿透单帧兜底，终止模块
                except Exception as e:
                    self._main_crash_frames += 1
                    if self._main_crash_frames == 1:
                        logger.log(f"[鉴宝] 单帧异常（已跳过，继续运行）: {e!r}", "WARNING")
                    if self._main_crash_frames > _MAIN_CRASH_RETRY_MAX:
                        logger.log(
                            f"[鉴宝] 连续 {_MAIN_CRASH_RETRY_MAX} 帧异常，判定为系统性问题，模块终止: {e!r}",
                            "ERROR",
                        )
                        raise
                    self.ctx.lifecycle.sleep(self._frame_interval_s)
                    continue
                self._main_crash_frames = 0
                self.ctx.lifecycle.sleep(self._frame_interval_s)
        finally:
            if self._trace_writer is not None:
                self._trace_writer.close()
                self._trace_writer = None
            self._stop_io_worker()     # 先停 IO 落盘 worker（排空队列，保证最后几帧落盘）
            self._stop_ocr_worker()
            if self._clicker is not None:
                self._clicker.shutdown()  # 停导航线程（异步导航收尾，daemon 不阻塞退出）
            self._store.close_db()  # 提交未完成事务并关闭落盘连接
            self._store.log_session_summary()

    def stop(self) -> None:
        assert self.ctx is not None  # 仅运行态调用
        self.ctx.lifecycle.request_stop()

    def cleanup(self) -> None:
        assert self.ctx is not None  # 仅运行态调用
        # renderer 由 Context.close 释放，无需在此手动归还

    # ==================================================================
    #  对外：状态设置接口（后续 OCR / 人工注入用）
    # ==================================================================

    def _reset_round_state(self, reason: str = "新一场") -> None:
        """新一场开始时清空上一场拍卖状态，防串场污染。

        实测事故（2026-08-15 003722 日志）：第二循环进入第1回合出价后，
        上一场残留的 H=261,100 让 set_h 判定「回合1 已锁定智能报价」，
        真实 H=167,100 被丢弃 → 不点智能出价、按残留价直接确认出价、
        对手快照全是上一场旧数据。这里在回到大厅/选场次时整体清空，
        _current_h/_sysmax_13/_valuation_lo/_valuation_hi 为派生 property，
        清空 _h_prices 后自动归零。
        """
        self._round_no = None
        self._h_prices = []               # 各回合系统报价（set_h 依赖，残留会误锁）
        self._our_bids = []               # 我方各回合出价
        self._player_bids = {}            # 对手出价（残留会构建错快照）
        # 我方槽位防抖状态：新一场重置（槽号可能随换位变化，重新识别）
        self._rank_candidate = None
        self._rank_candidate_frames = 0
        self._my_rank = None
        self._my_balance = None           # 余额进入 R1 后重新识别锁定
        self._balance_locked = False
        # bidding 状态机
        self._bid_epoch = 0
        self._bid_phase = "wait_first"
        self._panel_open = False
        self._panel_stable_frames = 0
        self._bid_player_submitted = {}
        self._wait_result_frames = 0
        self._bid_input_progress = 0
        self._bid_input_latest = None
        self._bid_confirm_streak = 0
        self._bidding_last_decision = None
        self._last_round_snapshot = None
        # 出价策略：重建实例清逼价基线等内部状态，保留已设的 risk_cap/mode
        if self._strategy is not None:
            rc = getattr(self._strategy, "risk_cap", self.DEFAULT_TREASURE_RISK_CAP)
            md = getattr(self._strategy, "mode", self.DEFAULT_TREASURE_MODE)
            self._strategy = BidStrategy(risk_cap=rc, mode=md)
        # 选鉴宝师确认标记 / 点击指纹锁：新一场重新走流程
        self._appraiser_confirmed_once = False
        self._last_click_fingerprint = None
        self._pending_click = None  # 异步点击：新一场无在途点击
        # 阶段切换重试状态：新一场清零（防残留重试配额/等待态）
        self._click_retry_key = None
        self._click_retry_stage = None
        self._click_retry_since_ts = 0.0
        self._click_retry_count = 0
        # 场次选择「开始匹配」冷却：新一场清零（防残留）
        self._session_start_cooldown = 0
        # 弹窗链状态：新一场清零（防跨场残留触发误判定）
        self._popup_click_cooldown = 0
        self._popup_loopback_frames = 0
        logger.log(f"[鉴宝] {reason}：已清空上一场拍卖状态（H/出价/对手/回合/bidding）", "DEBUG")

    def set_stage(self, stage_name: str, reason: str = "OCR", raw_round: int | None = None) -> bool:
        """切换阶段，stage_name 必须在 STAGE_ORDER 中。切换成功返回 True

        raw_round：检测器识别的原始回合号（可选）。附加回合（第6+，平局追加）时 stage 名
        clamp 成"第5回合出价"，从 stage 名提取只会得到 5，无法感知真实切换；传入原始号
        （如 6/7）才能让转场期（_round_elapsed）正确重置。
        """
        if stage_name not in self.STAGE_ORDER:
            logger.log(f"[鉴宝] set_stage 失败，未知阶段: {stage_name}", "WARNING")
            return False
        if stage_name != self._current_stage:
            old = self._current_stage
            old_raw = self._extract_round_from_stage(old)
            self._current_stage = stage_name
            raw_r = raw_round if raw_round is not None else self._extract_round_from_stage(stage_name)
            # 附加回合（第6+回合）clamp 到 5：数据统一写进第5回合槽（见决策文档 §10）
            new_r = min(raw_r, 5) if raw_r is not None else None
            self._round_no = new_r
            # 附加回合 raw>5 被 clamp 后 old_r==new_r 但实际已切回合 →
            # 用原始数字判断切换，保证转场期（_round_elapsed）正确重置
            if raw_r is not None and raw_r != old_raw:
                self._round_elapsed = 0  # 切到新回合 → 转场期开始
                # 回合切换 → 清指纹锁 + 重试状态，避免上一回合的「出价按钮」指纹（S2_bid）
                # 残留到新回合，导致新回合出价按钮亮起后永远不点击（时序问题根源）。
                self._last_click_fingerprint = None
                self._pending_click = None  # 异步点击：回合切换无在途点击
                self._click_retry_key = None
                self._click_retry_stage = None
                self._click_retry_since_ts = 0.0
                self._click_retry_count = 0
            # 进入「领取分红」阶段：重置"跳过动画点一次"标记（仅第一次准星指领取按钮，
            # 跳过数据加载动画，防止连点把结算页直接关掉退出去）
            if stage_name == "领取分红":
                self._settle_collect_clicked_once = False
                self._settle_skip_since = 0
                self._settle_skip_retry_count = 0
            # 离开「结算弹窗」阶段 → 彩蛋识别窗口结束，清 _egg_reading
            if stage_name != "结算弹窗":
                self._egg_reading = False
            # 进入「结算弹窗」阶段（弹窗链：今日最高/等级提升/彩蛋任一出现）：重置本场
            # 蛋计数 + 记录进入帧号（蛋 OCR/今日最高数值读取的稳定/超时计时基准）。
            if stage_name == "结算弹窗":
                self._egg_counts = None
                self._egg_read_done = False
                self._egg_best_result = None
                self._egg_best_streak = 0
                self._egg_reading = False
                self._reward_enter_frame = self._frame_counter
            # 进入「选择鉴宝师」：记录进入帧号。转场稳定缓冲（APPRAISER_SETTLE_FRAMES）内
            # 不判定匹配/不 fallback——转场动画期卡片模糊，立即匹配分会低于阈值，
            # 误判"未识别到目标" → 直接兜底点中间卡（2026-08-15 多循环事故）。
            if stage_name == "选择鉴宝师":
                self._appr_enter_frame = self._frame_counter
            # 新一场开始（回到大厅/选场次）→ 清空上一场拍卖状态 + 结算数据，防串场污染。
            # 实测事故：上场 H/对手出价残留会让下一场"已锁定智能报价"误判、快照用旧数据
            # （2026-08-15 003722 日志：第二循环 R1 真实 H=167,100 被残留 261,100 拦截丢弃）。
            if stage_name in ("游戏大厅", "鉴宝大厅(选择场次)"):
                # 完成一场判定：上一阶段是"真正开始对局"的阶段（出价/结算/分红/弹窗链）
                # 才算完整走完一场；从大厅/活动页/匹配中进入选场次都不算（还没开打）。
                # 实测事故：游戏大厅→活动页面→鉴宝大厅 会被误判为"完成一场"，状态机 +1
                # 虚增，导致"选3场只玩2场"提前拦截（2026-08-16 日志：状态机3 vs OCR2）。
                # 启动首帧 set_stage 大厅时 _prev_stage_for_loop_count 为 None → 不算。
                finished_a_game = (
                    self._prev_stage_for_loop_count is not None
                    and self._prev_stage_for_loop_count
                    not in ("游戏大厅", "活动页面", "匹配中", "鉴宝大厅(选择场次)")
                )
                # 先落盘再清空：本场结算/彩蛋/积分字段此刻仍完整（防丢数据）。
                if finished_a_game:
                    self._store.flush_game_record()
                self._reset_round_state(reason=f"进入{stage_name}")
                self._settle_my_income = None
                self._settle_skip_since = 0
                self._settle_skip_retry_count = 0
                self._settle_final_price = None
                self._settle_total_price = None
                self._settle_profit = None
                self._daily_high_score = None  # 弹窗①积分随弹窗消失失效，回大厅清空防串场
                self._auction_result = None    # 竞拍结果（win/fail）随本场结束失效
                # 回大厅 → 清空本场彩蛋计数（防串场带到下一场）
                self._egg_counts = None
                self._egg_read_done = False
                self._egg_best_result = None
                self._egg_best_streak = 0
                # --------- 每日循环计数：从"非大厅"阶段跳到大厅/选场次 → 上场完整走完，done+1 ---------
                # 只在跳变方向（结算/回合→大厅）+1；同阶段多次 set_stage 不重复 +1。
                # 起点：启动后立即 set_stage 大厅也不计（_prev_stage_for_loop_count==None 触发）。
                if finished_a_game:
                    self._refresh_daily_bucket()   # 跨凌晨5点先开新一天再计数
                    self._session_daily_done_count += 1
                    lim = self._effective_daily_loop_limit()
                    reached = self._daily_loop_limit_reached()
                    logger.log(
                        f"[鉴宝循环] 完成 1 场: 状态机侧累计 {self._session_daily_done_count} 场"
                        f"（上限 {lim}，OCR侧={self._session_daily_ocr_count or '--'}，"
                        f"已达上限停止开新场 = {'是' if reached else '否'}）",
                        "INFO",
                    )
                    if reached:
                        # 立即刷一条明显的 STOP 级提示到日志，GUI 运行日志会红字可见。
                        # 自动停止由 _tick_once 的连续 3 帧确认触发（回大厅即开始计数）。
                        logger.log(
                            f"[鉴宝循环] 已到每日循环上限 {lim} 场，"
                            f"本场为最后一场，回鉴宝大厅确认后模块将自动停止。",
                            "WARNING",
                        )
                # 记下来，下次跳变用
                self._prev_stage_for_loop_count = stage_name
            else:
                # 非大厅阶段：记录 prev，但不改动 done_count（防"第N→第N+1 回合"之类的重复计数）
                self._prev_stage_for_loop_count = stage_name
            # 阶段切换 → 重置附加回合小字兜底开关。附加回合只在"第5回合出价"stage 内
            # 保持激活（stage 名不变不触发此处）；进入结算/分红/大厅即关闭，新场次从零开始。
            if self._detector is not None:
                self._detector._allow_label_fallback = False
            self._log_stage_changed(stage_name, f"{reason}（{old}→{stage_name}）")
            self.record_event(f"stage_change_{stage_name}")
        return True

    def set_round(self, r: int) -> None:
        """手动指定回合号（1~5），并同步切换阶段到「第N回合出价」"""
        if not (1 <= r <= 5):
            return
        stage_name = f"第{r}回合出价"
        self.set_stage(stage_name, "手动回合")

    def set_h(self, value: int) -> None:
        """记录当前回合的「系统报价」（点智能出价后，弹窗中心金额显示区 OCR 读到的值）。
        前 5 回合的最大值 Hmax × 1.35/1.4 = 藏品真实估值区间。

        规则：
          1. H 只取每回合 **第一次** 合法 set_h（第一次 = 点智能出价弹出的基准价），
             同一回合后续 OCR 读到的值（用户手动调价）不再覆盖——只有智能报价参与估值/
             Hmax 计算，手工改动不参与。
          2. **相对历史防呆**：藏品价格量级跨几万~几十万不等，绝对阈值会误伤真实低价
             藏品；改用相对对比——若已有历史 H 且新值 < 历史 Hmax 的 1/10，则几乎
             必然是 OCR 裁位残缺（如 248,000 被裁成 24,800 已是极端，1/10 以下必是乱帧），
             拒绝写入；首个 H（无历史参照）不做此判断，避免拒绝真实低价藏品。
          3. 采集时机由调用方把控（_consume_ocr_result 仅在 bidding 面板打开 + 非转场期
             调 set_h），本函数只做值域层面的最后兜底。"""
        if self._round_no is None:
            logger.log(f"[鉴宝] set_h({value}) 忽略：未指定回合", "DEBUG")
            return
        while len(self._h_prices) < self._round_no:
            self._h_prices.append(0)
        # 相对历史防呆：已有历史 H 时，新值 < Hmax 的 1/10 → OCR 裁位残缺，丢弃
        prev_locked = self._h_prices[self._round_no - 1]
        valid_hist = [v for v in self._h_prices if v and v > 0]
        if valid_hist and value * 10 < max(valid_hist):
            logger.log(
                f"[鉴宝] set_h({value}) 丢弃：< 历史Hmax {max(valid_hist):,} 的 1/10，"
                f"判定为 OCR 裁位误读（回合{self._round_no}未锁定/已锁定={prev_locked > 0}）",
                "WARNING",
            )
            return
        # 该回合已有值 → 忽略后续（只保留第一次智能报价）
        if prev_locked > 0:
            if self._h_prices[self._round_no - 1] != value:
                logger.log(
                    f"[鉴宝] set_h({value}) 忽略：回合{self._round_no} 已锁定智能报价 "
                    f"{self._h_prices[self._round_no - 1]:,}（只取第一次，手动调价不覆盖）",
                    "DEBUG",
                )
            return
        self._h_prices[self._round_no - 1] = value
        extra = ""
        m13 = self._sysmax_13
        if m13:
            extra = f"  (sysmax_13={m13:,} → 估值 {int(m13*1.35):,} ~ {int(m13*1.4):,})"
        logger.log(f"[鉴宝] 回合{self._round_no} 系统报价 = {value:,}{extra}", "INFO")

    def set_rank(self, rank: int) -> None:
        """设置我方所在面板槽号（1~4，OCR 从带「（我）」标记的玩家名行提取）。
        槽号即行序，_maybe_build_snapshot 用它排除我方槽。同值去重，避免刷屏。"""
        if self._my_rank == rank:
            return
        self._my_rank = rank
        logger.log(f"[鉴宝] 我方槽位 = 槽{rank}", "DEBUG")

    # ==================================================================
    #  鉴宝师选择自动化：模板匹配 + 顺位抉择 + 点击
    # ==================================================================

    def _match_appraisers(
        self, frame_rgb: np.ndarray,
    ) -> list[tuple[int, str, float, float, float, float, float, float]]:
        """在 _APPRAISER_SEARCH_ROI 区域内做多尺度顺位匹配。

        对每个模板（按 P1→P2 顺序）遍历 _APPRAISER_MATCH_SCALES（0.70×~1.30×
        共 13 档），取该模板所有尺度下的「最高得分命中」作为该模板最终结果；
        只有最高分 ≥ _APPRAISER_MATCH_THRESHOLD 的模板才进入返回列表。

        返回按顺位升序（prio 小在前）的命中列表：
            [(priority, key, score, cxn, cyn, x2n, bw, bh), ...]
        x2n = 命中框右边界归一化 X（选中判定用：对勾贴在卡片右上角，对勾中心 X
        应≈命中框右边界）。
        bw/bh = 命中框归一化宽高（头像模板缩放后尺寸）——手柄点击容差用：
        落点在「头像命中框中心 70% 区域」内即按 A（头像在卡片内，点进头像区
        必然点进卡片，超大选框无需精确微调到中心）。
        空列表 = 一个都没匹配到。匹配异常静默跳过，不抛异常。
        """
        results: list[tuple[int, str, float, float, float, float, float, float]] = []
        if not self._appr_tpls:
            return results
        H, W = frame_rgb.shape[:2]
        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return results
        # 按顺位遍历（hits 本身就按 P1→P2 顺序产出，不用再排序）；
        # 每个模板用自己的搜索区 rect + 阈值（来自 JSON appraisers 段，调试台可逐项校准）。
        for prio, key, tpl, rect, threshold in self._appr_tpls:
            x1n, y1n, x2n, y2n = rect
            x1 = max(0, int(x1n * W))
            y1 = max(0, int(y1n * H))
            x2 = min(W, int(x2n * W))
            y2 = min(H, int(y2n * H))
            if x2 <= x1 or y2 <= y1:
                continue
            roi = gray[y1:y2, x1:x2]
            rh, rw = roi.shape[:2]
            best: tuple[float, int, int, int, int, int] | None = None
            # best = (score, scale_idx, match_x_in_roi, match_y_in_roi, scaled_th, scaled_tw)
            th0, tw0 = tpl.shape[:2]
            for s_idx, s in enumerate(_APPRAISER_MATCH_SCALES):
                # 缩放模板：整数尺寸，宽高同比例
                nw = max(4, int(round(tw0 * s)))
                nh = max(4, int(round(th0 * s)))
                if nh > rh or nw > rw:
                    # 缩放过大会让模板比 ROI 大，跳过
                    continue
                if nw == tw0 and nh == th0:
                    tpl_s = tpl
                else:
                    try:
                        # 缩小时用 AREA（避免锯齿），放大时用 CUBIC
                        interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
                        tpl_s = cv2.resize(tpl, (nw, nh), interpolation=interp)
                    except Exception:
                        continue
                try:
                    res = cv2.matchTemplate(roi, tpl_s, cv2.TM_CCOEFF_NORMED)
                except Exception:
                    continue
                _, smax, _, lmax = cv2.minMaxLoc(res)
                smax = float(smax)
                if best is None or smax > best[0]:
                    best = (smax, s_idx, lmax[0], lmax[1], nh, nw)
            # 全部尺度跑完：看最佳分数过该模板自己的阈值没
            if best is None:
                continue
            score = best[0]
            if score < threshold:
                continue
            # 用"该最佳匹配对应的缩放后模板尺寸"算中心（而不是原始尺寸！）
            _, _, mx_roi, my_roi, sth, stw = best
            cx_px = x1 + mx_roi + stw // 2
            cy_px = y1 + my_roi + sth // 2
            cxn = max(0.0, min(1.0, cx_px / W))
            cyn = max(0.0, min(1.0, cy_px / H))
            # 命中框右边界（归一化），选中判定用
            rx2 = max(0.0, min(1.0, (x1 + mx_roi + stw) / W))
            # 命中框宽高（归一化）——手柄点击容差 box 用（头像在卡片内）
            bw = max(0.001, min(1.0, stw / W))
            bh = max(0.001, min(1.0, sth / H))
            results.append((prio, key, score, cxn, cyn, rx2, bw, bh))
        return results

    def _match_selected_check(self, frame_rgb: np.ndarray) -> tuple[float, float, float] | None:
        """在 JSON 配置的对勾扫描区（stage.appraiser_selected_check.rect）匹配「已选中」对勾。

        扫描区应为覆盖三张卡片右上角对勾高度带的横向长条（调试台可调），
        对勾出现在左/中/右任一卡片右上角都能命中。多尺度匹配取最高分，
        分数 ≥ self._check_match_threshold 才返回 (score, cxn, cyn)；
        模板/rect 缺失或未命中返回 None（选中判定自动跳过）。
        """
        if self._check_tpl is None or self._check_rect is None:
            return None
        H, W = frame_rgb.shape[:2]
        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return None
        x1n, y1n, x2n, y2n = self._check_rect
        x1 = max(0, int(x1n * W))
        y1 = max(0, int(y1n * H))
        x2 = min(W, int(x2n * W))
        y2 = min(H, int(y2n * H))
        if x2 <= x1 or y2 <= y1:
            return None
        roi = gray[y1:y2, x1:x2]
        rh, rw = roi.shape[:2]
        th0, tw0 = self._check_tpl.shape[:2]
        best: tuple[float, int, int, int, int, int] | None = None
        for s_idx, s in enumerate(_CHECK_MATCH_SCALES):
            nw = max(4, int(round(tw0 * s)))
            nh = max(4, int(round(th0 * s)))
            if nh > rh or nw > rw:
                continue
            if nw == tw0 and nh == th0:
                tpl_s = self._check_tpl
            else:
                try:
                    interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
                    tpl_s = cv2.resize(self._check_tpl, (nw, nh), interpolation=interp)
                except Exception:
                    continue
            try:
                res = cv2.matchTemplate(roi, tpl_s, cv2.TM_CCOEFF_NORMED)
            except Exception:
                continue
            _, smax, _, lmax = cv2.minMaxLoc(res)
            smax = float(smax)
            if best is None or smax > best[0]:
                best = (smax, s_idx, lmax[0], lmax[1], nh, nw)
        if best is None:
            return None
        score = best[0]
        if score < self._check_match_threshold:
            return None
        _, _, mx_roi, my_roi, sth, stw = best
        cx_px = x1 + mx_roi + stw // 2
        cy_px = y1 + my_roi + sth // 2
        cxn = max(0.0, min(1.0, cx_px / W))
        cyn = max(0.0, min(1.0, cy_px / H))
        return (score, cxn, cyn)

    def _run_appraiser_choice(self, frame_rgb: np.ndarray) -> None:
        """选择鉴宝师阶段：顺位匹配 + 选中判定 → 计算「点击意图」供 PEEP 准星显示。

        **只算意图，不执行任何真实点击。** 每帧都重算一次（保证准星跟随画面）：
          1) 顺位匹配目标鉴宝师（P1 卡洛琳 → P2 章太郎）
          2) 在整个搜索区匹配「已选中」对勾（黄色√），用 X 坐标关联判定
             —— 对勾中心 X ≈ 目标命中框右边界 → 目标已被选中
          3) 已选中 → 准星指向底部「确认」按钮（confirm_red_btn，静态 rect 中心）
          4) 未选中 / 对勾在别的卡片上 → 准星指向目标头像卡片（先点选）
          5) 目标都没识别到 → 兜底：对勾命中（已有卡被选中）→ 点确认；
             否则准星指向中间卡（屏幕中心，凑合选一个）
        结果写入 _appr_last_decision，供 _decide_action 消费。
        """
        if self._current_stage != "选择鉴宝师":
            return
        # 关键门控：防止"选择主题"（选集装箱）阶段因 detector 未定义阶段、_current_stage
        # 沿用"选择鉴宝师"旧值而触发选师准星乱飘。必须：当前帧 detector 明确命中 appraiser_title
        # （= 原始 raw_stage == "选择鉴宝师"），不允许"检测器没认出任何阶段"时沿用旧阶段。
        if self._last_raw_stage != "选择鉴宝师":
            self._appr_last_decision = None
            return

        # 转场稳定缓冲：进入「选择鉴宝师」后的前 APPRAISER_SETTLE_FRAMES 帧，
        # 画面还在转场动画（卡片加载/模糊），此时匹配分不可信 → 不判定、不 fallback，
        # 避免"未识别到目标 → 直接点中间卡"的误兜底（2026-08-15 多循环事故）。
        if self._frame_counter - self._appr_enter_frame < self.APPRAISER_SETTLE_FRAMES:
            self._appr_last_decision = {
                "key": None, "center": None,
                "hint": "选择鉴宝师转场中，等待画面稳定...", "score": 0.0,
            }
            return

        hits = self._match_appraisers(frame_rgb)
        check = self._match_selected_check(frame_rgb)

        if hits:
            # 顺位优先：按 prio 升序取第一个（hits 已按 prio 升序）
            _, key, score, cxn, cyn, rx2, bw, bh = hits[0]
            # 选中判定：对勾贴在该卡片右上角（对勾中心 X ≈ 命中框右边界）
            # 容差 0.22（归一化比例）：
            #   4 列布局下相邻列宽 = (搜索区宽) / 4 ≈ (0.94) / 4 ≈ 0.235
            #   同卡偏差 ≤0.14（头像模板命中框 rx2 只覆盖头像，距卡右上对勾约 8% 屏宽）
            #   0.22 可容纳同卡偏差，同时隔不开隔壁卡（相邻卡对勾离 rx2 ≈ 0.235）。
            if check is not None:
                ck_score, ck_cxn, _ = check
                if abs(ck_cxn - rx2) < 0.22:
                    confirm = self._action_centers.get("confirm_red_btn")
                    if confirm is None:
                        logger.log("[鉴宝选师] 确认按钮(confirm_red_btn)未配置 rect，选中判定降级为指向头像", "WARNING")
                    else:
                        self._appraiser_confirmed_once = True
                        self._appr_last_decision = {
                            "key": "confirm_red_btn", "center": confirm,
                            "hint": f"意图: 已选中 {key}（√S={ck_score:.2f}）→ 点确认",
                            "score": ck_score,
                        }
                        logger.log(
                            f"[鉴宝选师] 点击意图: 已选中 {key}（√S={ck_score:.2f}）→ 点确认 "
                            f"目标=({confirm[0]:.3f},{confirm[1]:.3f})",
                            "INFO",
                        )
                        return
                # check 匹配上但位置不贴目标卡 → 可能在别的卡上（用户自己点了别的），
                # 也可能匹配的是 UI 伪影，打 DEBUG 方便定位。
                elif self._frame_counter % 5 == 0:
                    logger.log(
                        f"[鉴宝选师] 对勾√命中(S={ck_score:.2f} @X={ck_cxn:.3f})但离目标{key} "
                        f"rx2={rx2:.3f} 距离={abs(ck_cxn - rx2):.3f} ≥ 0.22 → 视为未选中，先点目标卡",
                        "DEBUG",
                    )
            elif self._frame_counter % 5 == 0:
                # check 为 None → 对勾匹配本身没超过阈值，方便定位是阈值问题还是模板/扫描区问题。
                logger.log(
                    f"[鉴宝选师] 目标命中{key}(S={score:.2f})但对勾√未命中 "
                    f"(<{self._check_match_threshold:.2f}) → 先点目标卡选上",
                    "DEBUG",
                )
            hint_msg = f"命中 {key}（S={score:.2f}），顺位决策"
        else:
            # 兜底：目标鉴宝师（卡洛琳/章太郎）都没识别到 → 凑合点中间卡（屏幕中心）。
            # 但若已有卡片被选中（对勾命中，大概率是刚点的中间卡）→ 直接点确认，形成闭环，
            # 避免「点了中间卡 → 对勾出现 → 仍指中间卡」的死循环（中间卡不在模板里，hits 恒为空）。
            if check is not None:
                ck_score, ck_cxn, _ = check
                confirm = self._action_centers.get("confirm_red_btn")
                if confirm is None:
                    logger.log(
                        "[鉴宝选师] 确认按钮(confirm_red_btn)未配置 rect，兜底降级为指向中间卡",
                        "WARNING",
                    )
                else:
                    self._appraiser_confirmed_once = True   # 问题1：过场期不再发 fallback
                    self._appr_last_decision = {
                        "key": "confirm_red_btn", "center": confirm,
                        "hint": f"意图: 未识别到目标但已有卡被选中（√S={ck_score:.2f}）→ 点确认",
                        "score": ck_score,
                    }
                    logger.log(
                        f"[鉴宝选师] 点击意图: 未识别到目标但已有卡被选中（√S={ck_score:.2f}）→ 点确认 "
                        f"目标=({confirm[0]:.3f},{confirm[1]:.3f})",
                        "INFO",
                    )
                    return
            # 问题1：用户反馈——点确认后有过场动画（卡片画面还在但细节变了，hits=空且 check=空），
            # 此时 fallback_center 准星会误闪到屏幕中心干扰用户。
            # 一旦发过确认意图（_appraiser_confirmed_once=True），就静默等待横幅/阶段跳转，
            # 不再发任何 fallback 准星。
            if self._appraiser_confirmed_once:
                self._appr_last_decision = {
                    "key": None, "center": None,
                    "hint": "已确认鉴宝师，等待过场动画...", "score": 0.0,
                }
                return
            # 兜底：无模板命中 → 找「第 2 列（从左数 1 index）卡片中心」而不是屏幕中心：
            #   - 3 列 → 中心在 (1+0.5)/3 = 0.500（刚好屏幕中心）
            #   - 4 列 → 中心在 (1+0.5)/4 = 0.375（屏幕中心 0.5 在第 2、3 列之间的缝，用户点不到）
            #   - 5 列 → 中心在 (2+0.5)/5 = 0.500
            # 所以列数 N = 4（最常见）时，必须把准星指到 0.375（第 2 列卡片正中），而不是 0.500 缝。
            # 怎么估 N？看卡片搜索 ROI x2n-x1n：如果搜索区 < 0.90 宽度（3 列）、0.90-0.97（4 列）...，
            # 简化：优先假设 4 列（用户反馈 4 列情况），兜底点 (0.375, 0.38)（卡片头像大致 Y 范围）
            key = "appraiser_fallback_center"
            score = 0.0
            cxn, cyn = 0.375, 0.38
            hint_msg = "未识别到目标鉴宝师，意图指向第 2 列卡片（4 列布局 fallback）"

        self._appr_last_decision = {
            "key": key, "center": (cxn, cyn),
            "hint": f"意图: {hint_msg}", "score": score,
            # 手柄点击容差 box：命中框宽高（头像在卡片内，框中心 70% 内即按 A，
            # 超大选框无需精确微调到中心）；fallback（无匹配框）无此键 → 精确中心。
            **({"box": (bw, bh)} if hits else {}),
        }
        logger.log(
            f"[鉴宝选师] 点击意图: {hint_msg} 目标=({cxn:.3f},{cyn:.3f})",
            "INFO",
        )

    def _match_session_panel(
        self, frame_rgb: np.ndarray,
    ) -> list[tuple[int, str, float, float, float]]:
        """在「鉴宝大厅(选择场次)」内匹配「开始匹配」按钮（仅做状态判定）。

        对 session_start_match_btn 在其 JSON rect ROI 内做多尺度匹配
        （0.70×~1.30×），取最高分命中；分数 ≥ _SESSION_MATCH_THRESHOLD 才算命中。
        返回命中列表（只有一个候选）：[(priority, key, score, cxn, cyn)]。
        """
        results: list[tuple[int, str, float, float, float]] = []
        if not self._session_panel:
            return results
        H, W = frame_rgb.shape[:2]
        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return results
        for prio, key, tpl, rect_norm in self._session_panel:
            x1n, y1n, x2n, y2n = rect_norm
            x1 = max(0, int(x1n * W))
            y1 = max(0, int(y1n * H))
            x2 = min(W, int(x2n * W))
            y2 = min(H, int(y2n * H))
            if x2 <= x1 or y2 <= y1:
                continue
            roi = gray[y1:y2, x1:x2]
            rh, rw = roi.shape[:2]
            best: tuple[float, int, int, int, int, int] | None = None
            th0, tw0 = tpl.shape[:2]
            for s_idx, s in enumerate(_SESSION_MATCH_SCALES):
                nw = max(4, int(round(tw0 * s)))
                nh = max(4, int(round(th0 * s)))
                if nh > rh or nw > rw:
                    continue
                if nw == tw0 and nh == th0:
                    tpl_s = tpl
                else:
                    try:
                        interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
                        tpl_s = cv2.resize(tpl, (nw, nh), interpolation=interp)
                    except Exception:
                        continue
                try:
                    res = cv2.matchTemplate(roi, tpl_s, cv2.TM_CCOEFF_NORMED)
                except Exception:
                    continue
                _, smax, _, lmax = cv2.minMaxLoc(res)
                smax = float(smax)
                if best is None or smax > best[0]:
                    best = (smax, s_idx, lmax[0], lmax[1], nh, nw)
            if best is None:
                continue
            score = best[0]
            if score < self._session_match_threshold:
                continue
            _, _, mx_roi, my_roi, sth, stw = best
            cx_px = x1 + mx_roi + stw // 2
            cy_px = y1 + my_roi + sth // 2
            cxn = max(0.0, min(1.0, cx_px / W))
            cyn = max(0.0, min(1.0, cy_px / H))
            results.append((prio, key, score, cxn, cyn))
        return results

    def _run_session_choice(self, frame_rgb: np.ndarray) -> None:
        """鉴宝大厅(选择场次)阶段：GUI 配置目标场次 → 点对应 badge 切场次 → 识别到「开始匹配」就按。

        **只算意图，不执行任何真实点击。** 每帧重算，供 PEEP 准星显示：
          1) 先做每日循环上限检查（到限就不给「开始匹配」意图，停止开新场）
          2) 模板匹配右侧详情卡底部的「开始匹配」按钮（stage 段 session_start_match_btn 模板）
             • 命中 → 详情卡已切到目标场次 → 准星指向 session_start_match_btn 中心
             • 未命中 → 详情卡未切换到目标场次 → 准星指向 GUI 配置目标场次的 badge
               （session_intern_badge / session_expert_badge / session_master_badge）
          3) 模板缺失（未配置）→ 降级：交替点目标 badge / 直接点 start_match_btn
             （指纹锁独立 key，不会相互阻挡；多点一次 badge 无害，详情卡停留在目标场次）
        结果写入 _session_last_decision，供 _decide_action 消费。
        """
        if self._current_stage != "鉴宝大厅(选择场次)":
            # 离开该阶段 → 冷却清零（防残留）
            self._session_start_cooldown = 0
            return

        # 点击「开始匹配」后的冷却期：按钮已消失但检测器还没切阶段（匹配中），
        # 若此时回退点 badge 会点到场次标签（时序 bug）。冷却帧内不产出新意图，
        # 让检测器有时间确认切走；冷却期间每帧递减。
        if self._session_start_cooldown > 0:
            self._session_start_cooldown -= 1
            self._session_last_decision = {
                "key": "session_start_cooldown",
                "hint": f"已点开始匹配，等待界面切换（冷却剩 {self._session_start_cooldown} 帧）...",
                "score": 0.0,
                # 无 center：不指向任何可点击物，指纹锁不会误匹配
            }
            return

        # --- 每日循环上限检查：到上限就不再给「开始匹配」意图，只提示"已到上限"
        if self._daily_loop_limit_reached():
            lim = self._effective_daily_loop_limit()
            msg = (
                f"已到每日循环上限 {lim} 场"
                f"（状态机累计 {self._session_daily_done_count}，"
                f"OCR读 {self._session_daily_ocr_count if self._session_daily_ocr_count is not None else '--'}）"
                "，停止开新场"
            )
            logger.log(f"[鉴宝循环] 场次选择拦截: {msg}", "WARNING")
            self._session_last_decision = {
                "key": "session_daily_limit_reached",
                "hint": msg,
                "score": 0.0,
                # 无 center：准星不显示（不指向任何可点击物），指纹锁也不会误匹配
            }
            return

        # 取 GUI 配置的目标场次 → badge key + 中文名
        tgt = self._target_session if self._target_session in TARGET_SESSION_OPTIONS else DEFAULT_TARGET_SESSION
        badge_key, session_label = TARGET_SESSION_OPTIONS[tgt]

        # 判定「开始匹配」按钮是否已出现在屏幕上（stage 段模板匹配）
        panel_hits = self._match_session_panel(frame_rgb)
        if panel_hits:
            # 命中 → 详情卡已切到目标场次，直接点「开始匹配」（actions 段 rect 中心）
            score = float(panel_hits[0][2])
            target_key = "session_start_match_btn"
            # 冷却：点完「开始匹配」后给检测器留帧数确认切走，避免下帧回退点 badge
            self._session_start_cooldown = SESSION_START_CLICK_COOLDOWN_FRAMES
            status = f"目标场次「{session_label}」→ 已识别到开始匹配按钮（S={score:.2f}），点击进入匹配"
        elif self._session_panel:
            # 有模板但没命中 → 详情卡未切到目标场次 → 先点目标场次 badge（切换详情卡）
            target_key = badge_key
            score = 0.0
            status = f"目标场次「{session_label}」→ 未识别到开始匹配按钮，先点击场次标签切换详情卡"
        else:
            # ———— 降级模式：session_panel 模板未加载（缺模板/rect）————
            # 无法用模板判断"按钮是否出现"，采用「每 6 帧交替目标」+ 指纹锁去重的推进方案：
            #   • 周期 [0,5] → 点目标 badge（切场次）
            #   • 周期 [6,11] → 点开始匹配位置
            # 指纹锁保证每个 key 同阶段只点一次：点成功后就不再重复；
            # 若某一帧点击落空（如前台校验拦截/画面抖动），下一帧仍会给出同样的意图，直到真正点击成功。
            # 当 start 点击成功后，下一阶段会变到"匹配中"，set_stage 会重置降级步骤到 0。
            phase = (self._frame_counter // 6) % 2
            if phase == 0:
                target_key = badge_key
                score = 0.0
                status = (
                    f"目标场次「{session_label}」→「开始匹配」模板未配置（降级模式），"
                    f"本轮（第{self._frame_counter}帧）先点击场次标签切换详情卡"
                )
            else:
                target_key = "session_start_match_btn"
                score = 0.0
                status = (
                    f"目标场次「{session_label}」→「开始匹配」模板未配置（降级模式），"
                    f"本轮（第{self._frame_counter}帧）直接点击开始匹配位置"
                )

        center = self._action_centers.get(target_key)
        if center is None:
            logger.log(
                f"[鉴宝场次] 动作按钮 {target_key} 未在 treasure_rois.json 配置 rect，准星跳过",
                "DEBUG",
            )
            self._session_last_decision = None
            return

        self._session_last_decision = {
            "key": target_key, "center": center,
            "hint": f"意图: {status}", "score": score,
        }
        logger.log(
            f"[鉴宝场次] 点击意图: {status} 目标=({center[0]:.3f},{center[1]:.3f})",
            "INFO",
        )

    # ==================================================================
    #  回合出价自动化：主按钮 OCR（等待出价/出价）+ 面板判定 → 点击意图
    # ==================================================================

    # 主界面底部出价按钮（截图1「等待出价」→截图2「出价」）文字识别区 key（ocr 段）
    _BID_MAIN_LABEL_KEY = "bid_main_btn_label"
    # 主界面底部出价按钮整面（截图2 亮红「出价」）点击目标 key（actions 段）
    _BID_MAIN_BTN_KEY = "bid_main_red_btn"

    def _match_bid_smart_btn(self, frame_rgb: np.ndarray) -> tuple[float, float, float] | None:
        """在 stage.smart_bid_btn 的 rect 内匹配出价面板「智能出价」按钮模板。

        面板打开 → 该按钮出现 → 模板命中 = 面板已开（S3 强信号）。
        多尺度 0.70~1.30×，阈值优先读 JSON stage.smart_bid_btn.threshold（调试台可校准），
        缺省回退 _SMART_BID_MATCH_THRESHOLD（0.72）；取最高分；返回 (score, cxn, cyn) | None。
        """
        if self._bid_smart_tpl is None or self._bid_smart_rect is None:
            return None
        H, W = frame_rgb.shape[:2]
        try:
            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            return None
        x1n, y1n, x2n, y2n = self._bid_smart_rect
        x1 = max(0, int(x1n * W))
        y1 = max(0, int(y1n * H))
        x2 = min(W, int(x2n * W))
        y2 = min(H, int(y2n * H))
        if x2 <= x1 or y2 <= y1:
            return None
        roi = gray[y1:y2, x1:x2]
        rh, rw = roi.shape[:2]
        tpl = self._bid_smart_tpl
        th0, tw0 = tpl.shape[:2]
        best: tuple[float, int, int, int, int, int] | None = None
        for s_idx, s in enumerate(_SESSION_MATCH_SCALES):
            nw = max(4, int(round(tw0 * s)))
            nh = max(4, int(round(th0 * s)))
            if nh > rh or nw > rw:
                continue
            if nw == tw0 and nh == th0:
                tpl_s = tpl
            else:
                try:
                    interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
                    tpl_s = cv2.resize(tpl, (nw, nh), interpolation=interp)
                except Exception:
                    continue
            try:
                res = cv2.matchTemplate(roi, tpl_s, cv2.TM_CCOEFF_NORMED)
            except Exception:
                continue
            _, smax, _, lmax = cv2.minMaxLoc(res)
            smax = float(smax)
            if best is None or smax > best[0]:
                best = (smax, s_idx, lmax[0], lmax[1], nh, nw)
        if best is None:
            return None
        score = best[0]
        # 阈值：优先 JSON stage.smart_bid_btn.threshold（调试台校准，与 detect() 同源），
        # 缺省回退 self._smart_bid_match_threshold（tuning.perception 收编）。
        threshold: float = self._smart_bid_match_threshold
        if self._detector is not None and self._detector.roi_thresholds:
            roi_th = self._detector.roi_thresholds.get(_SMART_BID_KEY)
            if isinstance(roi_th, float):
                threshold = roi_th
        if score < threshold:
            return None
        _, _, mx_roi, my_roi, sth, stw = best
        cx_px = x1 + mx_roi + stw // 2
        cy_px = y1 + my_roi + sth // 2
        cxn = max(0.0, min(1.0, cx_px / W))
        cyn = max(0.0, min(1.0, cy_px / H))
        return (score, cxn, cyn)

    def _read_bid_main_btn_label(self, frame_rgb: np.ndarray) -> str:
        """同步 OCR 主界面底部出价按钮文字（等待出价/出价）。

        只用 text（不做金额解析），返回去空格后的文本；识别失败/引擎不可用返回 ""。
        仅在面板未开（S1/S2）时调用，避免面板遮挡干扰 + 省 CPU。
        """
        if self._ocr is None:
            return ""
        rect = self._ocr._regions.get(self._BID_MAIN_LABEL_KEY)
        if rect is None:
            return ""
        info = self._ocr.recognize_single(frame_rgb, rect)
        if info is None:
            return ""
        return "".join(str(t) for t in info.get("raw_lines") or []).replace(" ", "").replace("\u3000", "")

    def _run_bidding_choice(self, frame_rgb: np.ndarray) -> None:
        """回合出价阶段：主按钮状态（等待出价/出价）+ 面板判定 → 点击意图。

        **只算意图，不执行任何真实点击。** 每帧重算，供 PEEP 准星显示。
        状态流转（S0/S1/S2/S3）：
          S0 转场期（round_elapsed < SWITCH_CONFIRM_FRAMES）→ 不出准星（等待回合动画稳定）
          S3 面板已开：stage.smart_bid_btn 模板命中（「智能出价」按钮出现）
              ├─ H 已读（OCR 到 bid_result_amount_box 金额）→ 准星指面板内「确认出价」（bid_confirm_red_btn）
              └─ H 未读 → 准星指「智能出价」（smart_bid_btn 中心，点它拿 H）
          S1/S2 面板未开：OCR 主按钮文字
              ├─ 含「等待出价」→ S1：不出准星（等亮）
              ├─ 含「出价」且不含「等待」→ S2：准星指主出价按钮（bid_main_red_btn 中心）
              └─ 读不出 / 不匹配 → 保守归 S1：不出准星（等待）
        结果写入 _bidding_last_decision，供 _decide_action 消费。
        """
        stage = self._current_stage
        if stage is None or not (stage.startswith("第") and "回合" in stage):
            self._bidding_last_decision = None
            return
        # S0：回合切换转场期，动画残缺高发，不判定
        if self._round_elapsed < self.SWITCH_CONFIRM_FRAMES:
            self._bidding_last_decision = {
                "state": "S0_transition", "key": None, "center": None,
                "hint": "回合转场中，等待动画稳定...", "score": 0.0,
            }
            return
        # S3：面板是否已开（「智能出价」按钮模板命中 = 强信号）
        smart = self._match_bid_smart_btn(frame_rgb)
        # 稳定帧判定：连续 PANEL_OPEN_MIN_STABLE_FRAMES 帧命中，才算「面板真的开了」。
        # 单帧闪中（转场画面、横幅 ROI 遮挡）→ stable_frames 清零或 < 阈值 → _panel_open=False，
        # 避免假上升沿+紧接着假下降沿把 phase 卡进 wait_result（一直等不出来）。
        if smart is not None:
            self._panel_stable_frames = min(self.PANEL_OPEN_MIN_STABLE_FRAMES + 2,
                                            self._panel_stable_frames + 1)
        else:
            self._panel_stable_frames = 0
        panel_open_now = self._panel_stable_frames >= self.PANEL_OPEN_MIN_STABLE_FRAMES

        rising_edge = panel_open_now and not self._panel_open
        falling_edge = (not panel_open_now) and self._panel_open
        self._panel_open = panel_open_now

        # phase 门控（v0.3.2）：上升沿只在等待出价机会相位有效，防模板抖动制造假 epoch
        if rising_edge and self._bid_phase in ("wait_first", "wait_next"):
            self._bid_epoch += 1
            self._bid_phase = "bidding"
            self._wait_result_frames = 0
            # 面板打开时，清空"上次读的输入框值"（新面板输入框可能是空，避免把上帧旧残留 T
            # 当成"已就位"误点确认）
            self._bid_input_latest = None
            # 新面板 = 新的输入会话：重置输入进度锚点 + 确认防抖计数
            self._bid_input_progress = 0
            self._bid_confirm_streak = 0
            logger.log(f"[鉴宝出价] 新 bidding epoch #{self._bid_epoch}（面板打开，输入框值已重置）")

        # 下降沿（phase==bidding 时面板从稳定开到稳定关）= 用户点了确认出价，面板关闭 → 推进 wait_result。
        # 关键1：不跟"生成确认意图"绑定——否则准星指了一下确认就 phase→wait_result，下帧准星空。
        # 关键2：不跟"出价区已出价"同时判断——出价区"出价中→已出价"有切换动画，
        #        动画期 OCR 读到的可能是旧"出价中"或乱帧，同时判断会误拒正常提交（用户提过动画问题）。
        #        假下降沿的识别挪到 wait_result 内部（动画缓冲期过后仍"出价中"才回退）。
        if falling_edge and self._bid_phase == "bidding":
            self._bid_phase = "wait_result"
            self._wait_result_frames = 0
            self._wait_result_entered_ts = time.time()
            logger.log(
                f"[鉴宝出价] epoch#{self._bid_epoch} 检测到面板关闭（用户已确认出价）"
                "（phase→wait_result），等待公开报价，OCR 读 4 槽构建快照...",
                "INFO",
            )

        # wait_result：已提交，等待公开报价（快照构建在 OCR 消费后由 _maybe_build_snapshot 完成）
        if self._bid_phase == "wait_result":
            self._wait_result_frames += 1
            # 前置强信号：4 槽都明确"已出价"（submitted=True，含金额）→ 4 人全提交 = 我方必已提交。
            # 此时无论此前 OCR 读到过什么"出价中"（提交后"出价中→已出价"过渡动画的误读帧），
            # 都直接跳过假下降沿判定，进入正常回合记录阶段（继续读 4 槽）。这是用户拍板方案：
            # "识别到四槽都显示已出价就直接跳过到正常回合记录阶段"。
            all_four_submitted = all(
                self._bid_player_submitted.get(i) is True for i in (1, 2, 3, 4)
            )
            if all_four_submitted:
                # 正常等待公开报价，继续读 4 槽构建快照；无 center → 渲染器只画文字提示条
                self._bidding_last_decision = {
                    "state": "S4_wait_result", "key": None, "center": None,
                    "hint": f"等待公布第 {self._round_no} 回合报价...（epoch#{self._bid_epoch}）", "score": 0.0,
                }
                return
            # 假下降沿判定（4 槽不齐"已出价"时才走到这里）：
            # 缓冲期（出区切换动画 + OCR 异步延迟）过后，我方槽仍被 OCR 明确读到"出价中"
            # → 真·未提交（面板误开误关 / 网卡提交失败），回退 wait_first 放行重新报价。
            # 三态化保证：网卡/动画残缺的空读取不写键（保持上次状态），不会把"没读到"当"出价中"误判。
            # 用户拍板「读到报价即禁用」：本回合任意槽已读到过报价（固化 或 hits>0）即证明
            # 报价已开始展示、我方必已提交 → 禁用假下降沿，避免"读不到已出价状态"误判重报。
            # 缓冲用时间口径（SUBMIT_ANIMATION_BUFFER_MS），与帧率解耦——帧数口径在
            # v4 节奏（policy_loop 自驱）下会被稀释。即使仍误回退 wait_first，按钮
            # OCR 读到「已出价」会自愈回 wait_result（见下方 S1/S2 分支）。
            any_bid_read = any(
                s.get("locked") or s.get("hits", 0) > 0 for s in self._bid_slots.values()
            )
            if (not any_bid_read
                    and (time.time() - self._wait_result_entered_ts) * 1000
                    >= self.SUBMIT_ANIMATION_BUFFER_MS
                    and self._my_rank is not None
                    and self._bid_player_submitted.get(self._my_rank) is False):
                self._bid_phase = "wait_first"
                self._wait_result_frames = 0
                logger.log(
                    f"[鉴宝出价] epoch#{self._bid_epoch} wait_result 动画缓冲期过后，"
                    f"4 槽未齐'已出价'且我方(槽位{self._my_rank})出价区仍明确'出价中' → "
                    "判定假下降沿（未提交成功），phase→wait_first 等面板重开重新报价",
                    "WARNING",
                )
                self._bidding_last_decision = {
                    "state": "S4_fake_fallback", "key": None, "center": None,
                    "hint": "误判的提交已回退，等待重新报价...", "score": 0.0,
                }
                return
            # 纯等待：无 center → 渲染器只画文字提示条，不画准星
            self._bidding_last_decision = {
                "state": "S4_wait_result", "key": None, "center": None,
                "hint": f"等待公布第 {self._round_no} 回合报价...（epoch#{self._bid_epoch}）", "score": 0.0,
            }
            return

        if smart is not None:
            s_score, s_cxn, s_cyn = smart
            if not self._current_h:
                # H 未读 → 点「智能出价」拿 H
                center = self._action_centers.get(_SMART_BID_KEY)
                if center is None:
                    logger.log("[鉴宝出价] 智能出价按钮(smart_bid_btn)未配置 rect，准星跳过", "WARNING")
                    self._bidding_last_decision = None
                    return
                self._bidding_last_decision = {
                    "state": "S3_smart", "key": _SMART_BID_KEY, "center": center,
                    "hint": f"意图: 面板已开 H 未读 → 点智能出价（√S={s_score:.2f}）",
                    "score": s_score,
                }
                logger.log(
                    f"[鉴宝出价] 点击意图: 面板已开 H 未读 → 点智能出价 目标=({center[0]:.3f},{center[1]:.3f})",
                    "INFO",
                )
                return
            # H 已读 → 若处于 bidding 相位，执行策略决策 + 输入链路
            if self._bid_phase == "bidding":
                self._run_bidding_execute(frame_rgb, s_score)
                return
            # H 已读但 phase 未进入 bidding（理论不该发生，保守等待）→ 纯等待文字
            self._bidding_last_decision = {
                "state": "S3_wait_phase", "key": None, "center": None,
                "hint": f"面板已开 H={self._current_h:,} 已读，等待相位推进...", "score": s_score,
            }
            return
        # S1/S2：面板未开 → OCR 主按钮文字
        label = self._read_bid_main_btn_label(frame_rgb)
        # 「已出价」= 提交成功的铁证（按钮显示 已出价:金额）。wait_first 若由假下降沿
        # 回退而来，按钮仍读「已出价」说明实际已提交、只是对手未齐报价 → 回
        # wait_result 继续读 4 槽等公开报价。没有这一步，wait_first 不投递槽 OCR，
        # 会错过整个公开报价窗口 → 快照缺失 → 后续回合策略退化为 observe。
        if "已出价" in label and self._bid_phase == "wait_first" and self._bid_epoch > 0:
            self._bid_phase = "wait_result"
            self._wait_result_frames = 0
            self._wait_result_entered_ts = time.time()
            logger.log(
                f"[鉴宝出价] epoch#{self._bid_epoch} wait_first 中按钮 OCR 读到「已出价」→ "
                "判定我方提交实际已成功（对手未齐报价），phase→wait_result 继续读公开报价",
                "INFO",
            )
            self._bidding_last_decision = {
                "state": "S4_wait_result", "key": None, "center": None,
                "hint": f"已提交，等待公布第 {self._round_no} 回合报价...（epoch#{self._bid_epoch}）",
                "score": 0.0,
            }
            return
        main_btn = self._action_centers.get(self._BID_MAIN_BTN_KEY)
        if main_btn is None:
            logger.log("[鉴宝出价] 主出价按钮(bid_main_red_btn)未配置 rect，准星跳过", "WARNING")
            self._bidding_last_decision = None
            return
        if "等待出价" in label:
            self._bidding_last_decision = {
                "state": "S1_waiting", "key": None, "center": None,
                "hint": f"等待出价按钮亮起...（OCR={label or '?'}）", "score": 0.0,
            }
            return
        if "出价" in label and "等待" not in label and "已出价" not in label:
            self._bidding_last_decision = {
                "state": "S2_bid", "key": self._BID_MAIN_BTN_KEY, "center": main_btn,
                "hint": f"意图: 出价按钮已亮（OCR={label or '?'}）→ 点出价",
                "score": 0.0,
            }
            logger.log(
                f"[鉴宝出价] 点击意图: 出价按钮已亮（OCR={label or '?'}）→ 点出价 "
                f"目标=({main_btn[0]:.3f},{main_btn[1]:.3f})",
                "INFO",
            )
            return
        # 读不出 / 不匹配 → 保守等待（纯等待文字，无准星）
        self._bidding_last_decision = {
            "state": "S1_waiting", "key": None, "center": None,
            "hint": f"等待出价按钮亮起（OCR={label or '?'}）...", "score": 0.0,
        }

    # ------------------------------------------------------------------
    #  出价策略执行（v0.3.5）：决策 → 输入子状态机 → 点击意图
    # ------------------------------------------------------------------

    def _build_bid_context(self) -> BidContext | None:
        """构建策略决策输入（不可变快照），数据不足返回 None。"""
        if self._strategy is None or self._round_no is None:
            return None
        h_seen = tuple(h for h in self._h_prices if h and h > 0)
        our_last = (
            self._our_bids[self._round_no - 1]
            if len(self._our_bids) >= self._round_no
            and self._our_bids[self._round_no - 1] > 0
            else None
        )
        # 对手最高价史（逐回合，已完成回合）：V3 策略据此算对手已证明火力
        # M = max(历史各轮最高, 上轮快照对手最高)（出价可回放，历史峰值才是真实上限）。
        # 我方槽位 _my_rank 从对手集合排除；某回合任一对空缺读则跳过该回合（不参与峰值）。
        opp_high: list[int] = []
        my_slot = self._my_rank
        if my_slot is not None and self._player_bids:
            opp_keys = [f"玩家{p}" for p in (1, 2, 3, 4) if p != my_slot]
            # 只统计到当前回合之前（已完成）的报价
            max_r = min(self._round_no - 1, 5) if self._round_no else 0
            for r in range(1, max_r + 1):
                vals = []
                for k in opp_keys:
                    lst = self._player_bids.get(k)
                    if not lst or len(lst) < r or lst[r - 1] < 0:  # -1=未读；0（掉线）也算已读
                        break
                    vals.append(lst[r - 1])
                else:
                    opp_high.append(max(vals))
        return BidContext(
            round_no=self._round_no,
            h_seen=h_seen,
            last_round=self._last_round_snapshot,
            balance=self._my_balance if self._my_balance is not None else BALANCE_UNKNOWN,
            our_last_bid=our_last,
            opp_high_history=tuple(opp_high),
        )

    def _maybe_build_snapshot(self) -> None:
        """wait_result 阶段：OCR 4 槽（我方 + 3 对手）全部「固化」后构建上一轮快照。

        - 只有 4 槽全部 locked 才替换 _last_round_snapshot（不发布半成品）
        - 构建成功才放行下一 epoch（phase → wait_next）
        """
        if self._bid_phase != "wait_result":
            return
        if self._round_no is None or self._my_rank is None:
            return
        r = self._round_no
        my_slot = self._my_rank
        missing: list[int] = [
            pid for pid in (1, 2, 3, 4)
            if not self._bid_slots.get(pid, {}).get("locked")
        ]
        if missing:
            # 任一槽未固化 → 保留旧快照（DEBUG 级别，带每槽状态帮助定位卡在哪个槽）
            def _slot_desc(pid: int) -> str:
                s = self._bid_slots.get(pid, {})
                if s.get("locked"):
                    return f"✓{s.get('val', -1):,}"
                st = "读中" if s.get("val", -1) != -1 else "未读"
                return f"{st}(稳{s.get('stable', 0)}/漏{s.get('miss', 0)}, {s.get('consumed', 0)}/{s.get('output', 0)}/{s.get('hits', 0)})"
            logger.log(
                f"[鉴宝] 快照构建等待: 槽{missing} 第{r}回合未固化（epoch#{self._bid_epoch}）: "
                + " ".join(f"P{pid}={_slot_desc(pid)}" for pid in (1, 2, 3, 4)),
                "DEBUG",
            )
            return
        slot_bids = {pid: self._bid_slots[pid]["val"] for pid in (1, 2, 3, 4)}
        h = self._current_h
        if not h:
            return
        others = [pid for pid in (1, 2, 3, 4) if pid != my_slot]
        opponent_ids = (others[0], others[1], others[2])
        opponent_bids = tuple(slot_bids[pid] for pid in opponent_ids)
        snap = RoundSnapshot(
            epoch=self._bid_epoch,
            round_no=r,
            h=h,
            our_bid=slot_bids[my_slot],
            opponent_bids=opponent_bids,
            opponent_ids=opponent_ids,
        )
        self._last_round_snapshot = snap
        self._bid_phase = "wait_next"
        self._wait_result_frames = 0   # 快照构建成功 → 清缓冲计数
        logger.log(
            f"[鉴宝] 上一轮快照已构建: epoch#{snap.epoch} R{r} H={h:,} 我方={snap.our_bid:,} "
            f"对手={tuple(b for b in opponent_bids)}",
            "INFO",
        )
        # 附加回合激活（收敛规则）：第 5 回合 4 人报价读全后，若第一名=第二名（平局）
        # 且未进入结算，游戏会追加第 6+ 回合（横幅模板只有 round1~5，识别不到 6+）→
        # 激活 detector 的回合小字兜底识别真实附加回合号。非平局 → 进结算，不激活。
        if snap.round_no == 5:
            top2 = sorted(slot_bids.values(), reverse=True)[:2]
            if len(top2) == 2 and top2[0] == top2[1]:
                if self._detector is not None:
                    self._detector._allow_label_fallback = True
                    logger.log(
                        f"[鉴宝] 第5回合平局（{top2[0]:,}={top2[1]:,}）→ "
                        "激活附加回合小字兜底识别",
                        "INFO",
                    )

    def _run_bidding_execute(self, frame_rgb: np.ndarray, s_score: float) -> None:
        """H 已读 + bidding 相位：策略决策 → 画面驱动输入子状态机 → 点击意图。

        输入推进完全由画面可观测变化驱动（输入框当前值 _bid_input_latest），
        不依赖"我点过了"的内部标记 —— 用户任何操作遗漏/错误都能自动纠正。
        """
        ctx = self._build_bid_context()
        if ctx is None:
            logger.log("[鉴宝出价] 决策上下文不可用（strategy 未初始化或回合号缺失）", "WARNING")
            self._bidding_last_decision = None
            return
        if not ctx.h_seen:
            self._bidding_last_decision = {
                "state": "S3_need_h", "key": None, "center": None,
                "hint": "等待 H 数据（OCR 中）...", "score": s_score,
            }
            return
        dec = self._strategy.decide(ctx)
        T = dec.price

        # ---------- 余额不足钳制：出价不能超过余额，否则游戏会重置输入框 → 无限编辑循环 ----------
        # 仅当余额已知（真实 0 或正数）才钳制；余额未知（BALANCE_UNKNOWN）不钳制（策略已在 cap 约束内）。
        # 同时自动调整兜底上限：最大可接受亏损 = max(0, 余额 - 估值)。
        if ctx.balance != BALANCE_UNKNOWN and T > ctx.balance:
            original_T = T
            T = ctx.balance
            if self._strategy is not None and dec.vhat > 0:
                max_afford = max(0, ctx.balance - int(dec.vhat))
                if max_afford < self._strategy.risk_cap:
                    old_cap = self._strategy.risk_cap
                    self._strategy.risk_cap = max_afford
                    logger.log(
                        f"[鉴宝出价] 余额不足: 目标 {original_T:,} > 余额 {ctx.balance:,}，"
                        f"钳制至 {T:,}；兜底上限自动下调 {old_cap:,} → {max_afford:,}",
                        "WARNING"
                    )

        # 输入框当前值（智能出价填入后，OCR bid_result_amount_box 实时读值）
        B = self._bid_input_latest
        if B is None:
            # 从未读到输入框值：等 OCR（面板打开初期 ROI 可能还没识别到）
            self._bidding_last_decision = {
                "state": "S3_edit_wait_ocr", "key": None, "center": None,
                "hint": f"等待输入框当前值（OCR）... 目标 {T:,}", "score": s_score,
            }
            return

        # ---------- observe 策略：完全跳过输入子状态机，按界面默认值出价 ----------
        # observe = "观察对手 / 接受系统建议 / 不玩花活"，核心是「绝不✖清空」。
        # 输入框当前值 B 就是用户刚点完智能出价的界面默认值（或者上一帧剩下的值），
        # 不管它等于/大于/小于策略价 T（=H=min(H,余额)），只要 B>0 就直接点确认出价，
        # 避免"把智能出价 H=152,800 先清空再重输 34,000 点✖"这种让用户感觉倒退的行为。
        if dec.decision == DECISION_OBSERVE:
            if B > 0:
                confirm = self._action_centers.get("bid_confirm_red_btn")
                if confirm is None:
                    logger.log("[鉴宝出价] 面板确认按钮(bid_confirm_red_btn)未配置 rect", "WARNING")
                    self._bidding_last_decision = None
                    return
                self._bidding_last_decision = {
                    "state": "S3_confirm_price", "key": "bid_confirm_red_btn", "center": confirm,
                    "hint": f"意图: [observe] 接受界面当前价 {B:,}（策略建议 {T:,}）→ 点确认出价（{dec.reason}）",
                    "score": s_score,
                }
                return
            # B=0 说明清空过没默认值 → 退回到「输 T=H」路径（不强制清空）
            self._bidding_last_decision = {
                "state": "S3_need_h", "key": None, "center": None,
                "hint": f"[observe] 输入框为空，等待填入建议价或默认值...（策略建议 {T:,}）", "score": s_score,
            }
            return

        if B == T:
            # 目标价已就位（数值完全相等 = 输入完成后的最终值）→ 准星指「确认出价」。
            # —— 注意：phase→wait_result 的推进发生在「面板真实关闭下降沿」，绝不跟"生成意图"绑定。
            #    跟意图绑定会导致下帧进入 wait_result → key=None → 准星消失，用户根本没机会点。
            #    下降沿检测在 _run_bidding_choice 主流程进行。
            # —— 输入进度锚点同步到位：B==T 意味着已正确输完目标价（int 相等隐含位数对齐，
            #    OCR 多读/裁位都造不出假相等），后续若 OCR 再读到别的值，靠 _bid_confirm_streak 防抖。
            self._bid_input_progress = max(self._bid_input_progress, len(str(T)))
            self._bid_confirm_streak = 0
            confirm = self._action_centers.get("bid_confirm_red_btn")
            if confirm is None:
                logger.log("[鉴宝出价] 面板确认按钮(bid_confirm_red_btn)未配置 rect", "WARNING")
                self._bidding_last_decision = None
                return
            self._bidding_last_decision = {
                "state": "S3_confirm_price", "key": "bid_confirm_red_btn", "center": confirm,
                "hint": f"意图: [{dec.decision}] 目标价 {T:,} 已就位 → 点确认出价（{dec.reason}）",
                "score": s_score,
            }
            logger.log(
                f"[鉴宝出价] 点击意图: [{dec.decision}] 目标价 {T:,} 已就位（B={B:,}）"
                f"→ 点确认出价 目标=({confirm[0]:.3f},{confirm[1]:.3f}) | {dec.reason}",
                "INFO",
            )
            return

        # 程序认为输入已完成（锚点到位）但 OCR 读到 ≠T：确认态防抖。
        # 单帧 OCR 抖动/裁位不立刻清空重输；连续 BID_CONFIRM_STABLE_FRAMES 帧都读不到 T，
        # 才判定"输入被破坏 / 用户改价"，重置锚点走编辑链路重输。
        if self._bid_input_progress >= len(str(T)):
            self._bid_confirm_streak += 1
            if self._bid_confirm_streak < self.BID_CONFIRM_STABLE_FRAMES:
                confirm = self._action_centers.get("bid_confirm_red_btn")
                if confirm is None:
                    logger.log("[鉴宝出价] 面板确认按钮(bid_confirm_red_btn)未配置 rect", "WARNING")
                    self._bidding_last_decision = None
                    return
                self._bidding_last_decision = {
                    "state": "S3_confirm_price", "key": "bid_confirm_red_btn", "center": confirm,
                    "hint": f"意图: [{dec.decision}] 目标价 {T:,} 已就位 → 点确认出价"
                            f"（OCR 读到 {B:,} 不符，防抖第 {self._bid_confirm_streak} 帧）",
                    "score": s_score,
                }
                return
            # 稳定不匹配 → 输入确实被改动，重置锚点进入编辑链路
            self._bid_input_progress = 0
            logger.log(
                f"[鉴宝出价] 确认态连续 {self.BID_CONFIRM_STABLE_FRAMES} 帧读到 B={B:,}≠{T:,}，"
                f"判定输入被改动，重置后重输", "WARNING",
            )

        # 需要修改输入框：清空（前缀不匹配）→ 逐位输入
        # 输入推进由「程序自记的进度锚点 _bid_input_progress」驱动，而非每帧 OCR 读到的 B：
        #   - 前缀匹配且 B 变长 → 锚点前进（确认新数字进框）
        #   - OCR 少读（B 变短）→ 锚点不回退，继续指 T[锚点]，不会重复输已输过的位
        #   - B==T 之外的值不做"清空/重输"的即时决策（防抖已挡住）
        ts = str(T)
        bs = str(B) if B > 0 else ""
        if bs and ts.startswith(bs):
            # 前缀匹配：输入在推进。锚点只前进不回退（OCR 短读挡在门外）
            if len(bs) > self._bid_input_progress:
                self._bid_input_progress = len(bs)
            if self._bid_input_progress >= len(ts):
                # 防御兜底：前缀匹配且锚点到位 → 理论已走 B==T 分支；此处指确认
                confirm = self._action_centers.get("bid_confirm_red_btn")
                if confirm is None:
                    logger.log("[鉴宝出价] 面板确认按钮(bid_confirm_red_btn)未配置 rect", "WARNING")
                    self._bidding_last_decision = None
                    return
                self._bidding_last_decision = {
                    "state": "S3_confirm_price", "key": "bid_confirm_red_btn", "center": confirm,
                    "hint": f"意图: [{dec.decision}] 目标价 {T:,} 已就位 → 点确认出价（{dec.reason}）",
                    "score": s_score,
                }
                return
            next_digit = ts[self._bid_input_progress]
            key = f"bid_numpad_{next_digit}"
            center = self._action_centers.get(key)
            if center is None:
                logger.log(f"[鉴宝出价] 数字键({key})未配置 rect", "WARNING")
                self._bidding_last_decision = None
                return
            self._bidding_last_decision = {
                "state": "S3_edit_type", "key": key, "center": center,
                "hint": f"意图: [{dec.decision}] 输入 {next_digit}（已输 {self._bid_input_progress} 位 → 目标 {T:,}）",
                "score": s_score,
            }
            logger.log(
                f"[鉴宝出价] 点击意图: [{dec.decision}] 输入数字 {next_digit}（进度 {self._bid_input_progress}/{len(ts)} 位 → 目标 {ts}）"
                f"目标=({center[0]:.3f},{center[1]:.3f}) | {dec.reason}",
                "INFO",
            )
            return
        if B > 0:
            # 前缀不匹配（智能出价初始 H ≠ 目标 / 用户输错）→ 点 ✖ 清空，锚点归零
            self._bid_input_progress = 0
            clear = self._action_centers.get("bid_numpad_clear")
            if clear is None:
                logger.log("[鉴宝出价] 重置按钮(bid_numpad_clear)未配置 rect", "WARNING")
                self._bidding_last_decision = None
                return
            self._bidding_last_decision = {
                "state": "S3_edit_clear", "key": "bid_numpad_clear", "center": clear,
                "hint": f"意图: [{dec.decision}] 目标价 {T:,} 残留输入框 {B:,} 前缀不匹配 → 点✖清空",
                "score": s_score,
            }
            logger.log(
                f"[鉴宝出价] 点击意图: [{dec.decision}] 目标价 {T:,} 残留输入框 {B:,} 前缀不匹配 → 点✖清空 "
                f"目标=({clear[0]:.3f},{clear[1]:.3f}) | {dec.reason}",
                "INFO",
            )
            return
        # 空（B==0）→ 输第一位
        next_digit = ts[0]
        key = f"bid_numpad_{next_digit}"
        center = self._action_centers.get(key)
        if center is None:
            logger.log(f"[鉴宝出价] 数字键({key})未配置 rect", "WARNING")
            self._bidding_last_decision = None
            return
        self._bidding_last_decision = {
            "state": "S3_edit_type", "key": key, "center": center,
            "hint": f"意图: [{dec.decision}] 输入 {next_digit}（已 空 → 目标 {T:,}）",
            "score": s_score,
        }
        logger.log(
            f"[鉴宝出价] 点击意图: [{dec.decision}] 输入数字 {next_digit}（已 '' → 目标 {ts}）"
            f"目标=({center[0]:.3f},{center[1]:.3f}) | {dec.reason}",
            "INFO",
        )

    # ==================================================================
    #  准星模式：程序「下一步想点击的位置」（peep 覆层用，不做真实点击）
    # ==================================================================

    def _init_policy_stack(self) -> None:
        """P1：编译 policies → PolicyPlan（唯一决策源）。

        - v3 资产存在但缺 `policies` 段 → 启动失败（P1e 硬约束）。
        - 编译/校验失败（P01-P09 阻断项）→ 启动失败。
        """
        v3_path = CONFIG_DIR / "treasure_assets.json"
        try:
            from maaracing_assistant.core.navkit import Assets
            assets = Assets.load(v3_path, module="treasure")
            if assets.policies is None:
                raise RuntimeError(
                    "treasure_assets.json 缺少 policies 段"
                    "（决策策略缺失 = 启动失败，请先迁移策略到资产文件）"
                )
            self._policy_plan = compile_plan(assets.policies, assets.anchors)
        except Exception as exc:
            logger.log(f"[鉴宝] policies 编译失败：{exc}", "ERROR")
            raise
        logger.log(
            f"[鉴宝] 决策策略就绪: v3 policies（rules={len(self._policy_plan.rules)}）",
            "INFO",
        )

    def _stage_id(self) -> str | None:
        """运行时阶段名 → policies 稳定 ID（stage_map 反向；bid 回合归一）。"""
        stage = self._current_stage
        if stage is None:
            return None
        if stage.startswith("第") and "回合" in stage:
            return "bid"
        if self._policy_plan is not None:
            rev = {v: k for k, v in self._policy_plan.stage_map.items()}
            stable = rev.get(stage)
            if stable is not None:
                return stable
        return _STAGE_TO_STABLE.get(stage)

    def _capture_decision_facts(self) -> DecisionFacts:
        """P0-6：本帧上游事实全部生产完后统一冻结（PolicyEngine 全程只读）。

        前置副作用（等价旧代码）：结算弹窗阶段感知提前解锁冷却——
        弹窗已稳定出现说明冷却目的达成，清零后不再白白等待剩余帧。
        注意：因此「cooldown>0 × 阶段=结算弹窗」帧会直接按弹窗分支决策
        （冷却规则只拦截弹窗外的跨阶段冷却窗口，如领取分红后/弹窗链之间）。
        """
        if self._current_stage == "结算弹窗":
            self._popup_click_cooldown = 0
        state = StateSnapshot.projection({
            "frame_counter": self._frame_counter,
            "settle_income": self._settle_my_income,
            "clicked_once": self._settle_collect_clicked_once,
            "retry_count": self._settle_skip_retry_count,
            "settle_skip_since": self._settle_skip_since,
            "cooldown": self._popup_click_cooldown,
            "daily_high_score": self._daily_high_score,
            "egg_reading": self._egg_reading,
            "egg_read_done": self._egg_read_done,
            "reward_enter_frame": self._reward_enter_frame,
        })
        outputs = {
            "stage": self._stage_id(),
            "popup_kind": (
                self._detector._last_hit_roi_key if self._detector is not None else None
            ),
            "session_decision": self._session_last_decision,
            "appraiser_decision": self._appr_last_decision,
            "bidding_decision": self._bidding_last_decision,
        }
        return DecisionFacts.freeze(
            state_snapshot=state,
            outputs=outputs,
            frame_counter=self._frame_counter,
        )

    def _apply_decision_effects(self, decision) -> None:
        """P0-6：决策后的引擎状态副作用（更新逻辑留码，JSON 只选择 effect）。

        fatal 为终止指令：与旧 `_decide_action` 直接 raise 的路径对齐。
        """
        for fx in decision.side_effects:
            if fx == "popup_cooldown_decr":
                self._popup_click_cooldown -= 1
            elif fx == "settle_skip_retry":
                self._settle_skip_retry_count += 1
                self._settle_skip_since = self._frame_counter
                self._last_click_fingerprint = None
        if decision.fatal:
            raise ClickRetryExhaustedError(decision.fatal)

    def _decide_action(self) -> dict | None:
        """P1：决策策略驱动（policies 数据化）。返回 {"key","hint","center"?} | None。

        链：`_capture_decision_facts`（上游事实全部产出后冻结）→ PolicyPlan 决策
        → 引擎副作用（`_apply_decision_effects`）→ 意图 dict（与旧结构一致，
        `_resolve_action_target` 消费逻辑不变）。
        """
        if self._current_stage is None:
            return None
        facts = self._capture_decision_facts()
        if self._policy_plan is None:
            return None
        decision = self._policy_plan.decide(facts)
        self._policy_snapshot = DecisionSnapshot.from_decision(facts, decision).as_dict()
        self._apply_decision_effects(decision)
        out = {"key": decision.key, "hint": decision.hint}
        if decision.payload.get("center") is not None:
            out["center"] = decision.payload["center"]
        return out

    def _resolve_action_target(self) -> dict | None:
        """帧内缓存包装：同帧多次调用返回同一意图（一帧一决策，P0-6 语义）。

        主循环（真实点击）与 _treasure_kwargs（peep 准星显示）都会调用本函数。
        决策流水带引擎副作用（冷却递减/重试计数/skip_since），且重试类决策
        「一帧内只在第一次出现」——同帧第二次决策时 skip_since 已被重置、
        条件不再满足，会产出等待意图并覆盖快照，导致重试 intent 被丢弃：
        实测领取分红跳过动画无响应重试链整体失效，卡动画直至 fatal（2026-09-06）。
        """
        if self._intent_cache_frame == self._frame_counter:
            return self._intent_cache
        result = self._resolve_action_target_uncached()
        self._intent_cache_frame = self._frame_counter
        self._intent_cache = result
        return result

    def _resolve_action_target_uncached(self) -> dict | None:
        """_decide_action 结果 → 补充归一化中心点，供渲染器画准星。

        三类来源：
          (A) 选择鉴宝师阶段：center 直接取自 _appr_last_decision 实时匹配结果
          (B) 鉴宝大厅(选择场次)阶段：center 取自 _session_last_decision 实时匹配结果
              （均为动态位置，JSON 无法静态配置）
          (D) 回合出价阶段：center 取自 _bidding_last_decision 实时匹配结果
              （智能出价/确认出价/主出价按钮均在匹配时定了中心；等待状态无 center 不出准星）
          (C) 其他阶段：查 _action_centers（treasure_rois.json 的按钮中心点）
        """
        a = self._decide_action()
        if not a:
            return None
        # (A) 选择鉴宝师阶段：优先使用实时匹配 + 兜底中心
        if self._current_stage == "选择鉴宝师":
            dec = self._appr_last_decision
            # 注意：用「dec.get("center") is not None」而不是「"center" in dec」——
            # 过场静默分支会写 {key:None, center:None, hint:...}，键存在但值为 None，
            # 用键存在判断会把 None center 透传给渲染器，导致
            # cx, cy = None → cannot unpack non-iterable NoneType object 崩溃。
            if dec and dec.get("center") is not None:
                payload = {
                    "key": a["key"], "center": dec["center"],
                    "hint": a.get("hint") or a["key"],
                }
                # 动态匹配目标（鉴宝师卡片）：决策已带命中框 → 直接透传 box
                # （_attach_box 保留显式 box）；点确认（confirm_red_btn 等静态
                # rect key）由 _attach_box 查表补上。
                if dec.get("box"):
                    payload["box"] = dec["box"]
                return self._attach_box(payload)
            # 已点过确认 → 选师 UI 已消失（后续是"选择主题"等未定义过渡阶段），
            # 既不给准星也不给等待文字，完全静默。
            if self._appraiser_confirmed_once:
                return None
            # 未确认的纯等待：只给文字提示（"等待识别..."），无 center 不画准星
            if a["key"] == "appraiser_waiting":
                return {"key": a["key"], "hint": a.get("hint") or a["key"]}
            return None
        # (B) 鉴宝大厅(选择场次)：优先使用实时匹配
        if self._current_stage == "鉴宝大厅(选择场次)":
            dec = self._session_last_decision
            if dec and dec.get("center") is not None:
                return self._attach_box({
                    "key": a["key"], "center": dec["center"],
                    "hint": a.get("hint") or a["key"],
                })
            if a["key"] == "session_waiting":
                # 纯等待：只给文字提示，不画准星
                return {"key": a["key"], "hint": a.get("hint") or a["key"]}
            return None
        # (B2) 结算弹窗（今日最高/等级提升/彩蛋）：center 由 _decide_action 直接给出
        #      （复用 confirm_red_btn 底部中心，非真实按钮、无 JSON rect，不能走 (C) 静态表）。
        #      纯等待（popup_waiting）无 center → 只给文字提示，不画准星。
        if self._current_stage == "结算弹窗":
            if a.get("center") is not None:
                return self._attach_box({"key": a["key"], "center": a["center"],
                                         "hint": a.get("hint") or a["key"]})
            return {"key": a["key"], "hint": a.get("hint") or a["key"]}
        # (D) 回合出价阶段：优先用 _bidding_last_decision 的动态 center
        #     （智能出价/确认出价/主出价按钮均在匹配时定了中心，不走静态表）
        if self._current_stage and self._current_stage.startswith("第") and "回合" in self._current_stage:
            dec = self._bidding_last_decision
            if dec and dec.get("center") is not None:
                return self._attach_box({
                    "key": a["key"], "center": dec["center"],
                    "hint": a.get("hint") or a["key"],
                })
            # 等待 / 转场期：无 center → 只给文字提示（"等待出价按钮亮起..."等），不画准星
            if dec:
                return {"key": dec.get("key") or "bid_waiting",
                        "hint": dec.get("hint") or a.get("hint") or "等待中..."}
            if a["key"] == "bid_waiting":
                return {"key": a["key"], "hint": a.get("hint") or "等待出价按钮亮起..."}
            return None
        # (C) 其他阶段：查静态按钮 center；没配置时返回纯文字（center=None → 渲染器只画提示条）
        # 盲点穿（popup_high_continue，疑似弹窗遮挡时）等由 _decide_action 直接给出 center
        # 的非静态按钮 key：优先透传，避免查 _action_centers 返回 None 把 center 丢掉
        # （2026-08-16：游戏大厅阶段升级弹窗盲点穿曾因 center 丢失而无法执行）。
        if a.get("center") is not None:
            # 决策直接给 center 的非静态按钮（如出价主按钮）同样附加 box——
            # 否则手柄容差只在"查 _action_centers"分支生效（实测出价按钮漏配）。
            return self._attach_box(
                {"key": a["key"], "center": a["center"], "hint": a.get("hint") or a["key"]})
        center = self._action_centers.get(a["key"])
        if center is None:
            # "stage_waiting / session_waiting / appraiser_waiting / dividend_waiting 等
            # 都是纯等待 key，根本没有按钮；不打 debug 日志直接走纯文字。
            wait_keys = {"stage_waiting", "session_waiting", "appraiser_waiting",
                         "bid_waiting", "dividend_waiting"}
            if a["key"] in wait_keys:
                return {"key": a["key"], "hint": a.get("hint") or a["key"]}
            logger.log(f"[鉴宝] 动作按钮 {a['key']} 未在 treasure_rois.json 配置 rect，准星跳过", "DEBUG")
            return None
        return self._attach_box({"key": a["key"], "center": center, "hint": a["hint"]})

    def _attach_box(self, payload: dict) -> dict:
        """给点击意图附加目标框归一化宽高（box）——手柄模式按「框中心 70% 区域」
        容差按 A 用；鼠标模式精确点击不使用。

        查表顺序：调用方已显式带 box（动态匹配目标，如鉴宝师卡片命中框）→ 直接
        保留；否则查 JSON 静态表（stage/actions 两段 rect 宽高）。两者都没有
        （个别动态 key 无 rect）→ 原样返回（容差缺省 → 精确微调到中心）。
        """
        if payload.get("box"):
            return payload
        size = self._action_rect_sizes.get(payload.get("key"))
        if size:
            payload["box"] = size
        return payload

    def _get_clicker(self):
        """懒创建点击器（只绑定窗口句柄；手柄能力按需在 _ensure_gamepad_bound 绑定）。

        模式不在创建时固定：每次 _execute_click 前会从 ctx.click_mode 同步，
        因此设置页切换点击方式后立即生效，无需重启模块。
        前台(鼠标)方式全程不触碰 gamepad 能力——不创建虚拟手柄设备（不呼出手柄）。
        """
        if self._clicker is None:
            from maaracing_assistant.core.clicker import Clicker

            self._clicker = Clicker(self.ctx.hwnd, self.ctx.click_mode)
        return self._clicker

    def _ensure_gamepad_bound(self) -> None:
        """后台(手柄)方式首次点击前按需绑定手柄能力（截图帧源 + 手柄 + A 键确认）。

        关键设计：只在 click_mode == "gamepad" 时执行，且整个生命周期只尝试一次
        （_gp_bind_tried 锁）——real(前台鼠标) 从不进入本函数，系统里不会出现
        ViGEm 虚拟手柄设备；绑定失败不重试（手柄方式返回失败不静默错点，
        前台鼠标方式不受影响），避免每帧重复尝试刷异常日志。
        """
        if self._clicker is None or self._gp_bind_tried or self._clicker.gamepad_bound:
            return
        if self.ctx.click_mode != "gamepad":
            return
        self._gp_bind_tried = True
        from maaracing_assistant.core.capabilities import VGamepadAdapter
        from maaracing_assistant.core.vgamepad_lazy import vg

        try:
            # 自 ctx 提取截图帧源（CaptureAdapter.screenshot），供手柄闭环导航读帧
            cap = self.ctx.capture
            frame_src = cap.screenshot  # 帧源 callable → RGB ndarray
            # 手柄：复用 controller 底层手柄（首次取用即创建虚拟手柄设备）
            gpad = VGamepadAdapter(self.ctx.gamepad._app._get_gpad())
            confirm_btn = vg.XUSB_BUTTON.XUSB_GAMEPAD_A
            self._clicker.bind_gamepad(frame_src, gpad,
                                       model_path=None,
                                       confirm_button=confirm_btn,
                                       rebuild_cb=self._rebuild_gamepad_device)
            logger.log("[鉴宝点击] 后台手柄方式：已绑定手柄导航能力（首次点击按需创建虚拟手柄）", "INFO")
        except Exception as e:  # noqa: BLE001 —— 无 gamepad 能力/手柄不可用时降级
            logger.log(
                f"[鉴宝点击] 手柄绑定失败，后台手柄方式不可用（前台鼠标方式不受影响）: {e}",
                "WARNING")

    def _rebuild_gamepad_device(self) -> bool:
        """光标长时间丢失的自愈（Clicker 连续丢失达阈值时回调）：销毁并重建虚拟手柄。

        用户拍板（2026-09-03）：光标长时间丢失不再归中盲拉复位，直接重建虚拟手柄——
        游戏把手柄断开/重连后光标复位重现。导航线程化后：GamepadClicker 导航线程
        **常驻不重建**，这里只 reset_device 拔除旧设备 + swap_gpad 换绑新设备
        （worker 已回等待态、不再持有旧设备，主循环 consume 后才调用本方法）。
        """
        clicker = self._clicker
        if clicker is None or not clicker.gamepad_bound:
            return False
        nav = getattr(clicker, "_gamepad", None)
        if nav is None:
            return False
        try:
            self.ctx.gamepad.reset_device()  # 确定性拔除旧设备（含活跃租约保护）
            from maaracing_assistant.core.capabilities import VGamepadAdapter

            gpad = VGamepadAdapter(self.ctx.gamepad._app._get_gpad())  # 懒创建新设备
            nav.swap_gpad(gpad)  # 换绑到常驻导航线程（任务槽空闲才允许）
            logger.log("[鉴宝点击] 光标长时间丢失：已重建虚拟手柄并换绑导航器", "INFO")
            return True
        except Exception as e:  # noqa: BLE001 —— 重建失败不阻塞主循环，下帧重试
            logger.log(f"[鉴宝点击] 虚拟手柄重建失败（下帧重试）: {e}", "WARNING")
            return False

    def _gamepad_nav_progress_kwargs(self) -> dict | None:
        """手柄导航最近进度快照（PEEP 渲染用；主循环每帧读，导航线程发布）。

        异步导航后不再用 on_progress 回调（跨线程渲染不安全），改为主循环从
        GamepadClicker.nav_progress() 拉取快照注入 PEEP kwargs。
        导航结束（stage=done）后不再显示——避免 PEEP 持续提示"光标丢失[done]"。
        """
        clicker = self._clicker
        if clicker is None or not clicker.gamepad_bound:
            return None
        nav = getattr(clicker, "_gamepad", None)
        if nav is None:
            return None
        prog = nav.nav_progress()
        if not prog or not prog.get("stage") or prog.get("stage") == "done":
            return None
        return dict(prog)

    def _active_stage_rois(self, stage: str | None) -> frozenset[str] | None:
        """当前阶段的「激活感知 ROI」：阶段检测与避让守卫同源（谁激活就保护谁）。

        返回语义与 _STAGE_PERCEPTION.get(stage) 一致：None=未登记 → 消费方回退
        全量检测（安全兜底）；非 None=frozenset 激活集。
        出价阶段按子状态动态裁剪：
          - bidding（面板打开、拨号盘输入/确认中）：只激活 smart_bid_btn。
            回合横幅/结算横幅/结算标题是"出价完成后"才可能出现的转移信号——
            拨号盘还在 = 本回合还没出价 = 它们必然不出现，激活纯属静态占坑；
            且这些横幅 ROI 的坐标与数字键盘重叠（round_big_banner 压 numpad 1/2/3、
            result_banner 压 numpad 1~6），面板期激活会导致光标点完数字被误判
            "压住识别区"反复避让（2026-09-03 用户实测根治）。
          - 其余出价子态（wait_first / wait_result / wait_next / 转场）：完整
            激活集（含转移信号，wait_result/wait_next 正是等它们出现的时刻）。
        """
        if stage is None:
            return None
        if (stage.startswith("第") and "回合" in stage
                and self._bid_phase == "bidding"):
            return frozenset({_SMART_BID_KEY})
        # S1：v3 DetectionPlan 是感知清单真源；NAVKIT_SOURCE=v2 时保留旧常量回退。
        plan = getattr(self._detector, "plan", None)
        if plan is not None:
            return plan.active_for(stage)
        return _STAGE_PERCEPTION.get(stage)

    def _collect_guard_rects(self) -> list[tuple[str, tuple[float, float, float, float]]]:
        """收集当前阶段「需要保持可识别」的 ROI：[(key, rect)]。

        口径：阶段感知激活的 stage 判定锚点 ∪ 全局锚点（被挡 = 阶段判定/回退失效）
        ∪ 当前阶段 OCR 区。由主循环**每帧**驱动——阶段状态每帧更新，激活集合自然
        跟随新阶段（修正：此前"点击后一次性 + 全量锚点"导致槽位复用误判——出价面板
        的智能出价按钮与匹配中的取消匹配按钮是同一屏幕槽位，全量锚点让出价阶段也
        被误避让；每帧驱动下按当前阶段裁剪即正确）。
        """
        rects: list[tuple[str, tuple[float, float, float, float]]] = []
        plan = getattr(self._detector, "plan", None)
        global_anchors = plan.global_anchors if plan is not None else _GLOBAL_ANCHORS
        keys = set(global_anchors)
        if self._current_stage:
            perception = self._active_stage_rois(self._current_stage)
            if perception:
                keys |= set(perception)
        for k in keys:
            r = self._detector.ROI.get(k)
            if r:
                rects.append((k, tuple(float(n) for n in r)))
        schema_ocr = self._detector.schema.get("ocr") or {}
        ocr_keys = (
            self._detector.plan.ocr_for(self._current_stage)
            if getattr(self._detector, "plan", None) is not None
            else _STAGE_OCR_KEYS.get(self._current_stage, ())
        )
        for k in ocr_keys or ():
            val = schema_ocr.get(k)
            rect = val.get("rect") if isinstance(val, dict) else None
            if isinstance(rect, list) and len(rect) == 4:
                rects.append((k, tuple(float(n) for n in rect)))
        # 出价主按钮文字 OCR 区（bid_main_btn_label，S1/S2 同步读取，不在异步
        # _STAGE_OCR_KEYS 里）：确认出价后光标恰好停在 (0.463,0.805) 落在该
        # label 区内，若不被守卫，下一轮 S1 读「出价」文字被光标挡住 → OCR 空 →
        # 永远等不到按钮亮起（2026-09-03 用户实测：出价 OCR 被挡但不避让）。
        # 面板开（bidding 相位）期间不守卫：面板覆盖主按钮且确认按钮 rect 与
        # 该 label 重叠，守卫会让"点确认前光标停确认按钮"被误判压住识别区，
        # 反复避让反而打断确认点击。
        if (self._current_stage and "回合" in self._current_stage
                and self._bid_phase != "bidding"):
            val = schema_ocr.get(self._BID_MAIN_LABEL_KEY)
            rect = val.get("rect") if isinstance(val, dict) else None
            if isinstance(rect, list) and len(rect) == 4:
                rects.append((self._BID_MAIN_LABEL_KEY, tuple(float(n) for n in rect)))
        return rects

    def _maybe_shoo_cursor(self, intent: dict | None) -> None:
        """光标驻留看守（主循环每帧调用，决策更新后）：光标压识别区则让核心避让。

        仅组装宿主领域知识（guard 区域 + 下一意图中心），判定与执行在
        core.clicker.auto_shoo（异步提交导航，主循环不被阻塞——2026-09-03
        导航线程化）。触发时打 DEBUG 日志（带命中区域 key）。
        """
        if self.ctx.click_mode != "gamepad" or self._last_frame_rgb is None:
            return
        H, W = self._last_frame_rgb.shape[:2]
        rects = self._collect_guard_rects()
        if not rects:
            return
        center = intent.get("center") if intent else None
        result = self._get_clicker().auto_shoo(
            rects, radius_px=self.SHOO_CURSOR_RADIUS_PX, frame_size=(W, H),
            next_center=(float(center[0]), float(center[1])) if center else None)
        if result:
            logger.log(
                f"[鉴宝点击] 光标压住识别区[{result['key']}]，"
                f"避让导航到 ({result['point'][0]:.2f},{result['point'][1]:.2f})（不点击）",
                "DEBUG")

    def _consume_click_result(self) -> None:
        """消费上一导航任务结果（点击/避让共用单槽），应用成功/失败副作用。

        异步导航协议（2026-09-03 导航线程化）：主循环每帧**先 consume 再决策**——
        consume → decision → submit。结果类型：
          - click ok    → 更新指纹/时刻 + 成功副作用（重试状态/领取标记/弹窗冷却/日志）
          - click 失败  → 指纹不更新（下帧同意图重试），节流打失败日志
          - move（避让）→ 不涉及点击状态，忽略（避让失败下帧重新判定即可）
        """
        clicker = self._get_clicker() if self._clicker is not None else None
        if clicker is None:
            return
        res = clicker.consume_result()
        if res is None:
            return
        if res.get("type") != "click":
            return  # 避让(move)结果：无点击副作用
        pending = self._pending_click
        self._pending_click = None
        if self._trace_writer is not None:
            self._trace_writer.write({
                "frame": self._frame_counter,
                "event": "click_result",
                "stage": self._current_stage,
                "click_result": {"ok": bool(res.get("ok")),
                                 "key": (pending or {}).get("key"),
                                 "device_lost": bool(res.get("device_lost", False))},
            })
        if res.get("ok"):
            self._apply_click_success(pending or {})
        else:
            self._apply_click_failure(res, pending or {})

    def _apply_click_success(self, pending: dict) -> None:
        """点击成功副作用（consume 时应用）：指纹/时刻/重试状态/领取标记/弹窗冷却/日志。"""
        key = pending.get("key")
        state = pending.get("state")
        fp = pending.get("fp")
        center = pending.get("center") or (0, 0)
        mode_label = pending.get("mode_label") or "?"
        clicker = self._get_clicker()
        sx, sy = clicker.last_pos or (0, 0)
        # 点击成功：更新指纹与时刻（失败时不更新 → 下帧意图相同会重试）
        self._last_click_fingerprint = fp
        self._last_click_time = time.time()
        # 结算后弹窗（今日最高/彩蛋）点击关闭后进入冷却；领取分红「真领取」也需冷却
        # （弹窗/结算页转场动画期模板匹配不上，冷却帧内不产出新意图防点穿/跳过阶段）。
        if (key in (self.POPUP_HIGH_CONTINUE_KEY, self.POPUP_REWARD_CONTINUE_KEY)
                or (key == "settle_collect_red_btn" and self._current_stage == "领取分红"
                    and self._settle_my_income is not None)):
            self._popup_click_cooldown = self._popup_click_cooldown_frames
        # 阶段切换类点击：进入"等待切换"状态（阶段切走即成功；超时未切走 → _maybe_retry 重新 arm）
        if key in self.CLICK_RETRY_KEYS:
            if self._click_retry_key != key or self._click_retry_stage != self._current_stage:
                self._click_retry_count = 0
            self._click_retry_key = key
            self._click_retry_stage = self._current_stage
            self._click_retry_since_ts = time.time()
        # 领取分红"跳过动画"首次点击成功后才置位（失败时意图持续，下帧重试）
        if (key == "settle_collect_red_btn" and self._current_stage == "领取分红"
                and self._settle_my_income is None):
            # 每次点击成功都重启无响应计时（含重试点击），防"重试后立即再重试"的连点风暴
            self._settle_skip_since = self._frame_counter
            if not self._settle_collect_clicked_once:
                self._settle_collect_clicked_once = True
                logger.log("[鉴宝分红] 已点击领取（跳过动画），标记置位，等 OCR 读本场收入...", "INFO")
        self.record_event("click",
                          extra_msg=f"方式={mode_label} state={state} key={key} 屏幕=({sx},{sy})")
        logger.log(
            f"[鉴宝点击] {state} key={key} 方式={mode_label} "
            f"目标=({sx},{sy}) 归一化=({center[0]:.3f},{center[1]:.3f})",
            "DEBUG",
        )

    def _apply_click_failure(self, res: dict, pending: dict) -> None:
        """点击失败副作用：指纹不更新（下帧同意图重试），节流打失败日志。"""
        if not self.ctx.lifecycle.running:
            return  # 停止信号中止导航：不算执行失败，主循环即将退出
        if self._frame_counter % 10 == 0:
            key = pending.get("key", "?")
            state = pending.get("state", "?")
            center = pending.get("center") or (0, 0)
            mode_label = pending.get("mode_label") or "?"
            logger.log(
                f"[鉴宝点击] 执行失败（将自动重试）key={key} state={state} "
                f"方式={mode_label} "
                f"归一化=({center[0]:.3f},{center[1]:.3f})", "WARNING")

    def _execute_click(self, target: dict | None) -> None:
        """把当前点击意图提交为一次点击：异步协议（submit → consume）。

        主循环拥有决策权、导航线程只拥有执行权（2026-09-03 导航线程化）：
          - submit_click 只入队（非阻塞），导航后台闭环，**主循环不被阻塞**——
            匹配中等转移信号窗口不再被导航占用吃掉。
          - 成功/失败副作用在下一帧 _consume_click_result 时应用（指纹更新等）。
        安全机制：
          • 指纹锁（边沿触发）：持续相同意图只点一次；consume 成功才更新指纹
          • cooldown：不同意图之间最小物理点击间隔（限速）
          • 前台校验：仅 real 模式要求目标窗口在前台（不抢前台）；gamepad 不需要
          • 意图开关（ctx.intent_mode）：置位后只导航不确认，由用户自己按
          • 统一走 core.clicker.Clicker：real=SetCursorPos+SendInput / gamepad=手柄导航+A键
        """
        if not target:
            return
        key = target.get("key")
        center = target.get("center")
        if not key or not center:
            return  # 纯等待意图（无按钮目标）
        # 出价阶段决策带 state（如 S3_edit_type），用于指纹区分动作类型
        state = "auto"
        if self._current_stage and self._current_stage.startswith("第") and "回合" in self._current_stage:
            dec = self._bidding_last_decision
            if dec and dec.get("key") == key:
                state = dec.get("state", "auto")
        # 指纹：数字键带输入位锚点；领取分红两次点击用 clicked_once 区分。
        fp = (key, state, round(center[0], 3), round(center[1], 3))
        if state.startswith("S3_edit_type"):
            fp = fp + (self._bid_input_progress,)
        elif key == "settle_collect_red_btn" and self._current_stage == "领取分红":
            fp = fp + (self._settle_collect_clicked_once,)
        # 模式在"首部"（设置页）切换：这里每次执行前同步，运行中切换即时生效。
        clicker = self._get_clicker()
        clicker.set_mode(self.ctx.click_mode)
        clicker.set_intent(self.ctx.intent_mode)
        self._ensure_gamepad_bound()  # 仅 gamepad 模式实际绑定；real 模式不触碰手柄能力
        # 任务槽忙（上一任务在跑 / 结果未消费）→ 本帧不再提交（consume 先行已消费）
        if clicker.is_busy():
            return
        # 前台校验：仅前台(鼠标)模式需要（点后台(手柄)不需要前台）
        if clicker.need_foreground and not is_foreground(self.ctx.hwnd):
            if self._frame_counter % 10 == 0:
                logger.log(
                    f"[鉴宝点击] 前台校验失败：游戏窗口非前台，取消本次点击 key={key}"
                    f"（方式={self.CLICK_MODE_LABELS.get(clicker.mode, clicker.mode)}；"
                    f"安全策略：前台鼠标点击不抢前台）", "WARNING",
                )
            return
        # 持续相同意图：只点一次（边沿触发，等意图变化/消失后重新 arm）；
        # 阶段切换类 key 例外：点击后 N 帧页面没切走 → 在这里重新 arm（_maybe_retry_stage_click）
        self._maybe_retry_stage_click(key)
        if fp == self._last_click_fingerprint:
            return
        # 不同意图间最小物理点击间隔（限速）
        now = time.time()
        if now - self._last_click_time < self._click_cooldown_s:
            return
        # 提交（非阻塞）。失败 → 指纹不更新，下帧同意图自动重试。
        if clicker.submit_click(
                center[0], center[1],
                down_up_gap_ms=self.CLICK_DOWN_UP_GAP_MS,
                move_pause_s=self.CLICK_MOVE_PAUSE_S,
                box=target.get("box")):
            self._pending_click = {
                "key": key, "state": state, "fp": fp, "center": center,
                "mode_label": self.CLICK_MODE_LABELS.get(clicker.mode, clicker.mode),
            }
            if self._trace_writer is not None:
                self._trace_writer.write({
                    "frame": self._frame_counter,
                    "event": "intent_submitted",
                    "stage": self._current_stage,
                    "intent": {"key": key, "state": state, "center": center,
                               "box": target.get("box")},
                })

    def _maybe_retry_stage_click(self, key: str) -> None:
        """阶段切换类点击的失败重试：点击后 N 帧页面没切走 → 重新 arm 指纹，下帧重试。

        背景：指纹锁是边沿触发（点击成功即更新指纹，相同意图不再点）。若点击落空/被鼠标
        干扰导致页面没切换（实测：游戏大厅点「巅峰鉴宝」卡片，鼠标一动没进活动页），
        阶段不变 → 意图不变 → 指纹不变 → 永不重试，程序卡死干等。这里只对
        CLICK_RETRY_KEYS 里「点击后预期离开当前阶段」的 key 做超时重试：
          - 阶段已切走 → 本次点击生效，清空等待态；
          - 仍在原地超 CLICK_RETRY_FRAMES 帧 → 清指纹重新点击（最多 CLICK_RETRY_MAX 次）。
        仅限阶段切换类，出价数字/确认、领取分红等「阶段不变型」点击不受影响（防误连点）。
        """
        if key not in self.CLICK_RETRY_KEYS:
            return
        # settle_collect_red_btn 例外：领取分红有「跳过动画」与「真领取」两次点击语义，
        # 只有真领取（本场收入已读出）才预期离开页面；跳动画/等待收入期阶段不变是正常的，
        # 不进入重试（否则会把结算页直接连点关掉）。
        if key == "settle_collect_red_btn" and self._settle_my_income is None:
            return
        rk = self._click_retry_key
        if rk is None or rk != key or self._click_retry_stage is None:
            return
        # 成功判定：
        #   - 阶段切换类（大厅卡片/前往鉴宝/开始匹配/选师确认/真领取）：阶段切走 = 成功
        #   - bid_main_red_btn：阶段不变（仍在出价阶段），成功 = 出价面板已打开
        #     （意图从 S2_bid 推进到 S3_*；面板开后再点出价按钮就点错位置了，必须收尾）。
        if self._current_stage != self._click_retry_stage:
            # 已切走：点击成功，收尾等待态
            self._click_retry_key = None
            self._click_retry_stage = None
            self._click_retry_count = 0
            return
        if key == "bid_main_red_btn":
            dec = self._bidding_last_decision
            if dec and str(dec.get("state", "")).startswith("S3"):
                # 面板已判定打开：本次点击生效，收尾
                self._click_retry_key = None
                self._click_retry_stage = None
                self._click_retry_count = 0
                return
        # 仍停在点击时的阶段：超时则重新 arm。
        # 重试帧数 per-key（弹窗连点 3 帧 / 阶段切换 10 帧），见 tuning.policy；
        # 配置口径是 v3 主循环帧数（FRAME_INTERVAL_MS/帧），运行时按**时间**判定——
        # v4 节奏由框架驱动、实际帧间隔不再是 FRAME_INTERVAL_MS，帧数口径会被稀释。
        retry_frames = self._retry_frames_by_key.get(key, self._click_retry_frames)
        retry_ms = retry_frames * self.FRAME_INTERVAL_MS
        if (time.time() - self._click_retry_since_ts) * 1000 < retry_ms:
            return
        if self._click_retry_count >= self._click_retry_max:
            logger.log(
                f"[鉴宝点击] key={key} 重试 {self._click_retry_max} 次后仍无法切换页面"
                f"（停留在「{self._click_retry_stage}」），判定点击失效，终止模块", "ERROR",
            )
            raise ClickRetryExhaustedError(
                f"阶段切换类点击 key={key} 重试 {self._click_retry_max} 次仍无法切换页面"
                f"（停留在「{self._click_retry_stage}」），请检查游戏界面/点击方式后重新开始"
            )
        self._click_retry_count += 1
        self._last_click_fingerprint = None      # 重新 arm → 本帧同一意图可再次点击
        self._click_retry_since_ts = time.time()
        logger.log(
            f"[鉴宝点击] key={key} 点击后 {retry_ms}ms 仍在「{self._click_retry_stage}」，"
            f"第 {self._click_retry_count}/{self._click_retry_max} 次重试", "WARNING",
        )

    def _treasure_kwargs(self, *, extra_note: str = "") -> dict:
        """统一构造 save_frame / DebugState 的鉴宝状态字段，避免 3 处手工同步漏字段。"""
        note = self._note or extra_note
        # 5 个回合的 H 历史（self._h_prices 按下标 0=R1）；缺失补 0，渲染器据此做折线图
        h_hist = [int(self._h_prices[i] or 0) if i < len(self._h_prices) else 0 for i in range(5)]
        # OCR 性能指标（心跳日志同款）
        ocr_stats = None
        if self._ocr_total_runs:
            ocr_stats = dict(
                total=int(self._ocr_total_runs),
                failures=int(self._ocr_failures),
                dur_ms=float(self._ocr_duration_ms),
                age_ms=float(self._ocr_result_age_ms),
            )
        return dict(
            treasure_stage=self._current_stage,
            treasure_round=self._round_no,
            treasure_h=self._current_h,
            treasure_sysmax_13=self._sysmax_13,
            treasure_val_lo=self._valuation_lo,
            treasure_val_hi=self._valuation_hi,
            treasure_vhat=self._vhat_strategy,
            treasure_our_bid=self._current_our_bid,
            treasure_rank=self._my_rank,
            treasure_note=note,
            # 增强 debug 图新增字段
            treasure_h_history=h_hist,
            treasure_player_bids=dict(self._player_bids),  # {"玩家1": [R1,R2,R3,R4,R5]}
            # 报价槽级固化状态（每槽 {val, stable, locked, miss, consumed, output, hits}），
            # debug 图玩家表徽标 + OCR 卡三口径统计用。
            treasure_bid_slots={pid: dict(s) for pid, s in self._bid_slots.items()},
            treasure_frame_index=int(self._saved_frames),  # raw 帧号（全局累计）
            treasure_debug_index=int(self._debug_saved),   # rendered(debug 图) 编号
            treasure_stage_order=list(self.STAGE_ORDER),   # 底部阶段进度条参考
            treasure_ocr_stats=ocr_stats,                  # OCR 指标 or None
            # 结算页结果
            treasure_settle_final=self._settle_final_price,
            treasure_settle_total=self._settle_total_price,
            treasure_settle_profit=self._settle_profit,
            treasure_settle_my_income=self._settle_my_income,
            treasure_daily_high=self._daily_high_score,  # 结算弹窗①今日最高积分（仅记录）
            # 我方余额
            treasure_balance=self._my_balance,
            # 准星模式：程序想点击的位置（peep 覆层用）
            treasure_action=self._resolve_action_target(),  # {"key","center","hint"} | None
            # 当前点击方式（peep 准星按方式区分显示：前台鼠标=青黄 / 后台手柄+A=紫粉）
            treasure_click_mode=getattr(self.ctx, "click_mode", "real"),
            # 手柄光标识别候选快照（peep 诊断：选中绿圈 / 次选黄圈，含低分拒识候选；
            # 2s 内有效——陈旧快照丢弃，避免主循环渲染显示上次导航的旧候选）
            treasure_cursor_cands=self._cursor_cands_snapshot(),
            # 手柄导航最近进度快照（peep 绿圈/距离；异步导航下主循环每帧读）
            treasure_gamepad_cursor=self._gamepad_nav_progress_kwargs(),
        )

    def _cursor_cands_snapshot(self) -> dict | None:
        """读取手柄导航器最近一次识别的候选快照（选中 + 次选，PEEP 诊断用）。

        快照由 GamepadClicker.read_pos 每帧刷新（分数降序前 8 个 + 选中下标）；
        超过 2s 未刷新（非手柄方式/导航已久未跑）返回 None 不显示。
        """
        gp = getattr(self._clicker, "_gamepad", None) if self._clicker is not None else None
        if gp is None or not gp.last_cands:
            return None
        if time.monotonic() - gp.last_cands_ts > 2.0:
            return None
        return {"list": list(gp.last_cands), "sel": gp.last_cand_sel}

    def record_event(self, name: str, extra_msg: str | None = None) -> Path | None:
        """
        记录一个「事件」：仅打 DEBUG 日志（不再单独截图）。
        raw 与 rendered（debug 图）均已全量存盘，无需 event 子目录截图。
        返回 None（兼容旧调用方）。

        事件（stage_change / click）属诊断细节：与「进入阶段」「点击意图」重复，
        降为 DEBUG 仅进文件，保持 GUI 故事线干净。
        """
        msg = f"[鉴宝] 事件: {name}"
        if extra_msg:
            msg += f" — {extra_msg}"
        logger.log(msg, "DEBUG")
        return None

    # ==================================================================
    #  内部：Debug 落盘 IO worker（生产-消费者，异步渲染+写盘）
    # ==================================================================

    def _debug_enqueue_frame(self, frame_rgb: np.ndarray, *, idx: int, didx: int,
                              label: str = "鉴宝观察", extra_note: str = "") -> None:
        """主线程→IO worker 入队：debug 全量存盘帧（raw + rendered）。
        有界队列满时静默丢帧（观测降密度，不阻塞主循环）。"""
        if self._io_queue is None:
            return
        kwargs = self._treasure_kwargs(extra_note=extra_note)
        frame_copy = frame_rgb.copy()
        try:
            self._io_queue.put_nowait(
                ("frame", frame_copy, idx, didx, label, kwargs)
            )
        except Full:
            pass  # 队列满 → 丢帧（观测降密度）

    def _debug_enqueue_peep(self, frame_rgb: np.ndarray, *,
                             label: str = "鉴宝观察", extra_note: str = "") -> None:
        """主线程→IO worker 入队：仅 PEEP 预览（无落盘）。
        主线程不阻塞，PEEP 预览帧由 IO 线程渲染更新。"""
        if self._io_queue is None:
            return
        kwargs = self._treasure_kwargs(extra_note=extra_note)
        frame_copy = frame_rgb.copy()
        try:
            self._io_queue.put_nowait(
                ("peep", frame_copy, 0, 0, label, kwargs)
            )
        except Full:
            pass

    def _start_io_worker(self) -> None:
        """启动 debug 落盘 IO worker（daemon 线程）。"""
        if self._io_thread is not None and self._io_thread.is_alive():
            return
        self._io_queue = Queue(maxsize=self.IO_QUEUE_MAX)
        self._io_stop.clear()
        self._io_thread = threading.Thread(
            target=self._io_worker_loop, name="treasure-io-worker", daemon=True
        )
        self._io_thread.start()
        logger.log("[鉴宝] IO worker（落盘）已启动", "DEBUG")

    def _stop_io_worker(self) -> None:
        """停止 IO worker：set 停止信号 + 排空队列 + join。"""
        if self._io_thread is None:
            return
        self._io_stop.set()
        # 排空剩余任务（保证最后几帧落盘）
        self._drain_io_queue()
        self._io_thread.join(timeout=3.0)
        if self._io_thread.is_alive():
            logger.log("[鉴宝] IO worker 3s 内未退出", "WARNING")
        self._io_thread = None
        self._io_queue = None

    def _drain_io_queue(self) -> None:
        """排空 IO 队列直到空或超时（停止时调用，保证最后几帧不丢）。"""
        if self._io_queue is None:
            return
        deadline = time.time() + 3.0
        while time.time() < deadline and not self._io_queue.empty():
            try:
                self._process_io_task(self._io_queue.get(timeout=0.5))
            except Empty:
                break

    def _io_worker_loop(self) -> None:
        """IO worker 主循环：取任务 → 渲染 → 写盘 / 更新 PEEP。
        顶层 try/except：单次任务异常不杀死 daemon，计数后继续。"""
        while not self._io_stop.is_set():
            try:
                try:
                    task = self._io_queue.get(timeout=0.5)
                except Empty:
                    continue
                self._process_io_task(task)
            except Exception as e:
                logger.log(f"[鉴宝] IO worker 异常: {e}", "WARNING")

    def _process_io_task(self, task: tuple) -> None:
        """处理单帧 IO 任务：渲染 → 写盘（raw + rendered webp）或 PEEP 更新。"""
        cmd, frame_rgb, idx, didx, label, kwargs = task
        img_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        if cmd == "frame":
            # raw JPG 存盘
            cv2.imwrite(
                str(self._raw_dir / f"{idx:04d}_raw.jpg"),
                img_bgr,
                [cv2.IMWRITE_JPEG_QUALITY, 95],
            )
            # rendered webp 存盘
            renderer = self.ctx.debug_renderer.current() if self.ctx else None
            if renderer is not None:
                from maaracing_assistant.core.debug import DebugState
                state = DebugState(label=label, **kwargs)
                full_img = renderer.render_full(img_bgr.copy(), state)
            else:
                full_img = img_bgr
            cv2.imwrite(
                str(self._session_dir / f"{didx:04d}.webp"),
                full_img,
                [cv2.IMWRITE_WEBP_QUALITY, 95],
            )
            # debug 开启 + peep 也开：同帧同时维护 PEEP 预览（与 save_frame 行为一致）
            if renderer is not None and getattr(self.ctx.debug, "peep_enabled", False):
                peep_img = renderer.render_peep(img_bgr.copy(), state)
                self.ctx.debug.update_peep(peep_img)
        elif cmd == "peep":
            # 仅 PEEP 预览：渲染精简视图并更新预览（经公开接口，避免直接访问私有成员 P5）
            renderer = self.ctx.debug_renderer.current() if self.ctx else None
            if renderer is not None:
                from maaracing_assistant.core.debug import DebugState
                state = DebugState(label=label, **kwargs)
                peep_img = renderer.render_peep(img_bgr, state)
                self.ctx.debug.update_peep(peep_img)

    def _prepare_debug_dirs(self):
        """创建 debug/treasure/<ts>/ 目录结构（含 raw/）。

        仅当 debug 存图开启（ctx.debug.enabled）时建立；未开启则 _session_dir/_raw_dir 置 None，
        对应 tick 不存盘、会话总结不显示「保存帧数/调试目录」。
        """
        assert self.ctx is not None  # 仅运行态调用
        self._debug_root = debug_dir() / "treasure"
        self._session_dir = None
        self._raw_dir = None
        if self.ctx.debug.enabled:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._session_dir = self._debug_root / ts
            self._raw_dir = self._session_dir / "raw"
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._saved_frames = 0
        self._debug_saved = 0

    # ==================================================================
    #  内部：每帧 tick
    # ==================================================================

    def _tick_once(self):
        assert self.ctx is not None  # 仅运行态调用
        self._frame_counter += 1
        self._round_elapsed += 1  # 每帧自增；set_stage 回合变化时重置

        frame_rgb = self.ctx.capture.screenshot()
        if frame_rgb is None:
            if self._frame_counter % 10 == 0:
                logger.log("[鉴宝] 截图失败（可能窗口未就绪）", "DEBUG")
            return
        self._last_frame_rgb = frame_rgb  # 手柄导航（同步阻塞）期间进度回调渲染 PEEP 用

        # S2：常开决策流水。debug 会话开着时与 raw 帧同目录对齐（帧号可互查）；
        # 未开启 debug 时落 debug/treasure/session_*/（独立 trace 会话）。
        if self._trace_writer is None:
            if self._session_dir is not None:
                self._trace_writer = TraceWriter(self._session_dir.parent, keep_sessions=10,
                                                 session_dir=self._session_dir)
            else:
                self._trace_writer = TraceWriter(debug_dir() / "treasure", keep_sessions=10)

        # 首帧校验：截图帧尺寸 vs 客户区物理尺寸（坐标映射 1:1 前提，偏差时 WARNING）
        if self._frame_counter == 1:
            verify_frame_client(self.ctx.hwnd, frame_rgb.shape[1], frame_rgb.shape[0])

        # --------- 0. 阶段检测 → 过滤 → 同步状态机 ---------
        self._run_stage_detection(frame_rgb)
        if self._trace_writer is not None:
            detection = self._last_detection_result
            self._trace_writer.write(FrameTrace(
                frame=self._frame_counter,
                stage=self._current_stage,
                round_no=self._round_no,
                scores=getattr(detection, "scores", {}),
                hit_anchor=getattr(detection, "hit_anchor", None),
                active_used=getattr(detection, "active_used", ()),
                plan_version=("v4" if self._v4_enabled()
                              else "v3" if getattr(self._detector, "plan", None) is not None else "v2"),
            ))

        # --------- 0.05 每日循环上限：连续 3 帧确认后自动停止 ---------
        # 到限后模块在鉴宝大厅空转（不点开始匹配）无意义：截图/OCR/存盘/心跳白耗资源。
        # 连续 DAILY_LIMIT_STOP_STABLE_FRAMES 帧在鉴宝大厅且判定到限 → request_stop 收尾
        # （主循环 finally 会停 OCR worker + 输出会话总结）。
        # 3 帧防抖：单帧 OCR 误读「日已参与 X/50」或阶段抖动不触发提前停机；
        # 未到限 / 离开大厅 / 跨日凌晨 5 点（计数清零后判定为 False）→ 计数清零重计。
        if self._daily_loop_limit_reached():
            self._daily_limit_streak += 1
            if self._daily_limit_streak == self.DAILY_LIMIT_STOP_STABLE_FRAMES:
                logger.log(
                    f"[鉴宝循环] 连续 {self.DAILY_LIMIT_STOP_STABLE_FRAMES} 帧确认已到每日循环上限"
                    f"（状态机 {self._session_daily_done_count} 场 / OCR"
                    f" {self._session_daily_ocr_count if self._session_daily_ocr_count is not None else '--'}），"
                    f"停止开新场并自动停止模块",
                    "WARNING",
                )
                self.ctx.lifecycle.request_stop()
        else:
            self._daily_limit_streak = 0

        # --------- 0.1 鉴宝师选择自动化（顺位匹配 + 意图）---------
        # 在「选择鉴宝师」阶段自动执行；其它阶段立即返回。所以放在 save_frame 之前，
        # 便于 save_frame 渲染时 peep 准星能拿到本 tick 的决策位置。
        self._run_appraiser_choice(frame_rgb)

        # --------- 0.2 场次选择自动化（大师场 → 开始匹配 意图）---------
        # 在「鉴宝大厅(选择场次)」阶段执行；其它阶段立即返回。
        self._run_session_choice(frame_rgb)

        # --------- 0.3 回合出价自动化（等待/出价按钮 + 面板判定 → 意图）---------
        # 在「第N回合出价」阶段执行；其它阶段立即返回。
        self._run_bidding_choice(frame_rgb)

        # --------- 0.5 OCR：出价面板阶段识别 H价 / 4个玩家出价 ---------
        self._run_ocr(frame_rgb)

        # --------- 1. 画面变化检测 ---------
        significant_change = self._detect_change(frame_rgb)

        # --------- 2+3. 调试存盘：渲染 + raw/rendered 落盘全部异步到 IO worker ---------
        # 主线程只做：分配帧号 + 打包 (frame copy + 当帧 state 快照) 入队，不等落盘完成。
        # 渲染（HUD/ROI/PEEP ~20-40ms）+ raw JPG + rendered WebP（~63ms）由 IO 线程执行，
        # 主循环帧间隔不再被存盘拖慢（docs/P4_DUAL_CHANNEL_ANALYSIS.md §3 困难一）。
        # debug 开启 → raw 全量 + rendered 全量；debug 关 + peep 开 → 仅维护 PEEP 预览。
        if self._session_dir is not None and self._raw_dir is not None:
            self._saved_frames += 1
            self._debug_saved += 1
            self._debug_enqueue_frame(
                frame_rgb,
                idx=self._saved_frames,
                didx=self._debug_saved,
                label="鉴宝观察",
                extra_note=("画面变化" if significant_change else ""),
            )
        elif getattr(self.ctx.debug, "peep_enabled", False):
            # peep-only：save_frame 走 IO 线程渲染并更新 _latest_frame，主循环不阻塞。
            self._debug_enqueue_peep(
                frame_rgb,
                label="鉴宝观察",
                extra_note=("画面变化" if significant_change else ""),
            )

        # --------- 4. 画面显著变化 → 事件日志（不再单独截图，raw 已全量覆盖）---------
        # 说明：screen_change 事件日志已移除——它不携带"变化到哪个阶段"的信息（阶段切换
        # 已有 stage_change_* / 进入阶段 日志），且 record_event 已不截图，只剩一条低价值
        # INFO 噪音。significant_change 仍用于 save_frame 的 extra_note（debug 图 HUD
        # 备注标出"画面变化"帧，逐帧回溯定位转场用）。

        # --------- 5. DEBUG 心跳日志 ---------
        if self._frame_counter % self.DEBUG_LOG_INTERVAL == 0:
            h_str = f", H={self._current_h:,}" if self._current_h else ""
            rank_str = f", 槽位={self._my_rank}" if self._my_rank else ""
            ocr_str = ""
            if self._ocr_total_runs:
                ocr_str = (f" | OCR {self._ocr_total_runs}次(失败{self._ocr_failures}, "
                           f"最近{self._ocr_duration_ms:.0f}ms, 时效{self._ocr_result_age_ms:.0f}ms)")
            # _player_bids 汇总：直接打印每个玩家5个回合槽的出价（主通道：用户能直接看"主程序记没记到"）
            # 格式：P1=[164800,182300,0,0,0] P2=[] ...
            if self._player_bids:
                def _fmt_list(lst):
                    return "[" + ",".join(f"{v:,}" if v > 0 else "0" for v in lst) + "]"
                bids_str = " | 出价 " + " ".join(
                    f"P{k[-1]}={_fmt_list(lst)}" for k, lst in sorted(self._player_bids.items())
                )
            else:
                bids_str = ""
            # 槽级固化状态概览：P{pid}:{徽标}{消费}/{输出}/{命中}（仅 wait_result 有数据时）
            slot_str = ""
            if self._bid_slots:
                parts = []
                for pid in sorted(self._bid_slots):
                    s = self._bid_slots[pid]
                    if s["locked"]:
                        badge = f"✓{s['val']:,}"
                    elif s["val"] != -1:
                        badge = f"读中{s['val']:,}稳{s['stable']}"
                    else:
                        badge = "未读"
                    parts.append(f"P{pid}:{badge} {s['consumed']}/{s['output']}/{s['hits']}")
                slot_str = " | 槽 " + " ".join(parts)
            logger.log(
                f"[鉴宝] 心跳 #{self._frame_counter}: 阶段={self._current_stage}"
                f"{h_str}{rank_str} | 已存 {self._saved_frames} 帧{ocr_str}{bids_str}{slot_str}",
                "DEBUG",
            )

        # --------- 6. 真实点击：把当前点击意图执行成可见鼠标移动 + 停顿 + 点击 ---------
        # 意图由各阶段决策（_resolve_action_target 统一）给出，含归一化 center；
        # 安全机制（指纹锁/限速/前台校验/坐标换算）见 _execute_click 文档。
        # P2a-Q3b：此段抽为 _decision_phase()，供 v4 MRA_Policy 桥复用（调用序
        # 行为位一致：意图解析 → consume → submit → 避让 → 决策契约落盘）。
        self._decision_phase()

    def _decision_phase(self) -> None:
        """单帧决策-动作段：v3 主循环与 v4 policy 闭环节点共用的调用序。

        v3：_tick_once 每帧调用；v4：MRA_Policy 桥（PolicyBridge）每次
        CustomAction.run 调用一次（一帧决策），[JumpBack] 回 dwell 重判。
        """
        intent = self._resolve_action_target()
        # 光标驻留看守（手柄模式）：每帧在决策更新后检查——光标若压住本阶段需识别
        # 的 ROI 且下一意图目标不能自然带离，先避让导航到空白处（不点击）再走点击。
        # 异步导航协议（2026-09-03）：consume → click 决策/提交 → shoo（click 优先）。
        self._consume_click_result()      # 先消费上一任务结果，应用指纹/时刻等副作用
        self._execute_click(intent)       # click 决策 + 提交（非阻塞，导航后台闭环）
        self._maybe_shoo_cursor(intent)   # 无 click 任务时（任务槽空闲）才尝试避让
        # P1：决策契约落盘（facts 投影 + policy 输出）。
        if self._policy_snapshot is not None and self._trace_writer is not None:
            self._trace_writer.write({
                "frame": self._frame_counter,
                "event": "decision",
                "decision_snapshot": self._policy_snapshot,
            })
            self._policy_snapshot = None

    # ==================================================================
    #  内部：阶段检测（模板匹配 → 过滤层 → 同步 set_stage）
    # ==================================================================

    def _run_stage_detection(self, frame_rgb: np.ndarray) -> None:
        """运行 TreasureStageDetector，套用过滤层（回合单调 + 防抖 + 强特征立即切），
        再把结果同步到 set_stage()，让 HUD / 日志 / 状态机推进到真实游戏阶段。

        动态感知裁剪：按当前阶段只匹配感知清单 ∪ 全局锚点；当前阶段未登记清单
        （或尚未进入任何阶段）时回退全量检测（安全兜底，不会静默漏检）。"""
        if self._detector is None:
            return
        # 阶段感知裁剪：active = 动态激活集 ∪ 全局锚点；未登记阶段 → None（全量）
        perception = self._active_stage_rois(self._current_stage)
        active_rois = None
        if perception is not None:
            active_rois = set(perception) | set(_GLOBAL_ANCHORS)
        try:
            detection = self._detector.detect(frame_rgb, active_rois)
            raw_stage, raw_r = detection
            self._last_detection_result = detection
        except Exception:
            return
        # 记录 detector 原始结果（未经过滤层），供选师/场次准星判断"是否真的在该阶段"
        # —— 选择主题阶段 detector 不会命中任何 ROI（STAGE_ORDER 无定义），raw_stage=None，
        # 但 _current_stage 会沿用"选择鉴宝师"旧值；选师准星必须对此敏感，避免乱发。
        self._last_raw_stage = raw_stage
        self._last_raw_round = raw_r

        # 「第N回合」强信号（回合横幅/小字命中）→ 立即切换，不防抖
        if raw_r is not None and raw_stage is not None and raw_stage.startswith("第"):
            is_big_jump = (self._det_round is None) or (raw_r > self._det_round)
        else:
            is_big_jump = False

        det_stage, det_r = self._accept_stage(raw_stage, raw_r, immediate=is_big_jump)

        # 同步到状态机（det_stage 为 None 时保持当前阶段不变）
        if det_stage is not None:
            # 中标结算阶段：判断竞拍结果横幅（中标/未中标）供落盘记录。
            # 转场帧可能匹配不到 → 只在拿到 win/fail 时写，避免把已记录结果覆盖成 None。
            if det_stage == "中标结算" and self._detector is not None:
                r = self._detector.banner_result(frame_rgb)
                if r is not None:
                    self._auction_result = r
            self.set_stage(det_stage, "检测器", raw_round=det_r)

    def _run_egg_ocr(self, frame_rgb: np.ndarray) -> None:
        """结算弹窗（彩蛋）阶段：异步投递彩蛋识别（worker 线程后台跑，主线程零阻塞）。

        进入彩蛋弹窗后（_egg_reading=True）每帧持续投递 latest-only（task="egg"），
        不再依赖本帧 title 是否命中（title 有转场/闪断，单帧失配不应断识别）。
        先等 EGG_OCR_STABLE_FRAMES 帧动画稳定缓冲，再真正投递。
        worker 结果由 _apply_egg_result 按「历史最优+连续稳定」积累，置 _egg_read_done
        才解锁点击（_decide_action 同步受 _egg_reading 门控，避免点抢先于识别）。
        未配置/识别失败/结果迟到 → 不置位 → 彩蛋走 EGG_OCR_TIMEOUT_FRAMES 超时兜底点关闭
        （落盘用最优结果），不卡流程、不阻塞主循环。_egg_reading 期间由 _run_ocr 每帧调用。
        """
        if self._egg_recognizer is None or self._current_stage != "结算弹窗":
            return
        if self._egg_read_done:
            return
        self._apply_egg_result()  # 先消费上一轮 worker 结果（若有）
        if self._egg_read_done:
            return
        if self._frame_counter - self._reward_enter_frame < self.EGG_OCR_STABLE_FRAMES:
            return  # 弹窗转场/动画稳定缓冲：前 N 帧先不投递（识别结果不可信）
        self._egg_push(frame_rgb)

    # ---------- 彩蛋识别异步（复用 OCR worker 线程，task="egg"）----------
    def _egg_push(self, frame_rgb: np.ndarray) -> None:
        """投递彩蛋识别帧（latest-only，复用 OCR worker 槽）。识别器未初始化则跳过。"""
        if self._egg_recognizer is None:
            return
        self._ocr_push(frame_rgb, task="egg")

    def _egg_publish_result(self, res, frame_id: int, captured_ts: float, t0: float) -> None:
        """worker 写彩蛋结果槽（完整新 dict 替换，不原地修改已发布对象）。"""
        with self._ocr_lock:
            self._egg_result = {
                "frame_id": frame_id,
                "captured_ts": captured_ts,
                "completed_ts": time.time(),
                "duration_ms": (time.time() - t0) * 1000,
                "data": res,
            }

    def _egg_take_result(self) -> dict | None:
        """主线程消费彩蛋结果槽（取走即清空，避免重复应用）。"""
        with self._ocr_lock:
            res = self._egg_result
            self._egg_result = None
            return res

    def _apply_egg_result(self) -> None:
        """消费彩蛋 worker 结果：历史最优累积 + 连续稳定确认后才判定「读完」。

        修复（2026-08）：彩蛋蛋卡是逐帧飞入动画——实测 1150 只进蓝蛋、1151 蓝黄齐、
        1152 起整体消失，完整帧仅 1 帧。原实现「首个非空结果立即 _egg_read_done=True」
        会被早期不完整帧（只有蓝蛋）锁死，黄蛋漏记 → 只落盘 1 个蓝蛋。
        现改为：
          1) 每帧按「命中蛋种数」择优累积到 _egg_best_result（更多蛋覆盖更少蛋，绝不降级）；
          2) 只有连续 EGG_RESULT_CONFIRM_FRAMES 帧命中蛋数不低于最优，才置位「读完」；
          3) 若一直未能稳定（完整帧仅 1 帧的极短窗口），则依赖 _decide_action 超时兜底
             点关闭，落盘 _egg_counts 用的是已累积的最优值（红/黄/蓝齐全）。
        彩蛋结果仅记录用途，不影响决策，晚到也安全。
        """
        if self._egg_read_done:
            return
        result = self._egg_take_result()
        if not result:
            return
        res = result.get("data")
        if not res:
            return  # 识别失败/未配置 → 不更新最优，走超时兜底
        counts = res.get("counts") or {"red": 0, "yellow": 0, "blue": 0}
        new_hits = sum(1 for v in counts.values() if v > 0)
        if new_hits == 0:
            return  # 本帧 0 命中（蛋未飞到 / 已消失）→ 不作为择优与稳定依据
        best_hits = 0
        if self._egg_best_result is not None:
            best_hits = sum(
                1 for v in (self._egg_best_result.get("counts") or {}).values() if v > 0
            )
        if new_hits > best_hits:
            # 出现更多蛋 → 采用为最优，并重新计连续稳定帧
            self._egg_best_result = res
            self._egg_best_streak = 1
        elif new_hits == best_hits:
            self._egg_best_streak += 1
        # 若 new_hits < best_hits（早期不完整帧迟到）→ 不降级，保留已见最优，也计一帧
        if self._egg_best_result is not None:
            self._egg_counts = (
                self._egg_best_result.get("counts") or {"red": 0, "yellow": 0, "blue": 0}
            )
        # 连续稳定达到阈值 → 判定读完
        if (
            self._egg_best_result is not None
            and self._egg_best_streak >= self.EGG_RESULT_CONFIRM_FRAMES
        ):
            self._egg_read_done = True
            eggs = self._egg_best_result.get("eggs") or []
            detail = "、".join(f"{e['color']}x{e['count']}" for e in eggs) or "无"
            logger.log(
                f"[鉴宝彩蛋] 奖励结算识别完成: 红={self._egg_counts.get('red', 0)} "
                f"黄={self._egg_counts.get('yellow', 0)} 蓝={self._egg_counts.get('blue', 0)}"
                f"（最优命中 {len(eggs)} 张卡: {detail}）", "INFO",
            )

    @property
    def _frame_interval_s(self) -> float:
        """主循环帧间隔（秒）。wait_result 阶段报价读取需要高频——用户拍板「帧率翻倍真双通道」：
        帧间隔从 300ms 降到 150ms，未固化槽（尤其 P4）的 OCR 投递频率 ×2；
        其余阶段维持 FRAME_INTERVAL_MS。"""
        if self._bid_phase == "wait_result":
            return self.WAIT_RESULT_FAST_MS / 1000.0
        return self.FRAME_INTERVAL_MS / 1000.0

    def _run_ocr(self, frame_rgb: np.ndarray) -> None:
        """主线程先消费上一轮 worker 结果应用业务状态，再投递最新帧给 worker。
        识别在 worker 线程进行，本方法 O(1) 不阻塞主循环。
        投递阶段：① 出价面板（第 X 回合出价，且面板已开=识别到智能出价按钮）
                  ② 中标结算 ③ 领取分红。
        出价阶段仅在面板已开（S3）时投递：H 就是输入框当前值（智能出价填入），
        面板未开（S1等待/S2出价）输入框区域是别的 UI，投递既浪费性能又可能误判 H。
        阶段 ②/③ 的 OCR ROI 都在 treasure_rois.json 里配置（settle_final/settle_total/settle_profit/
        settle_my_income），离线脚本同步验证过。不投递其他界面避免误识别。"""
        if self._ocr is None:
            return
        self._apply_ocr_result()
        s = self._current_stage or ""
        if s == "鉴宝大厅(选择场次)":
            # 每日循环计数：场次页「日已参与 X/50」同步识别（单 ROI）。
            # 不走 worker/异步（停留短，异步返回时阶段已切走会被门控丢弃），
            # 直接在这里读场次页帧 → 更新计数 → 上限判断生效。
            self._ocr_consume_daily_count_sync(frame_rgb)
            return
        if s == "结算弹窗":
            # 结算弹窗阶段：可能包含今日最高积分上涨 / 彩蛋弹窗 / 等级提升。
            # 检测器 _last_hit_roi_key 区分具体弹窗，各走各自识别逻辑。
            hit_key = self._detector._last_hit_roi_key if self._detector else None
            if hit_key == "daily_high_banner":
                # 今日最高积分同步识别（单 ROI，快）。弹窗停留短 → 不走 worker，
                # 同步单 ROI ~10ms 不阻塞主循环；已读到则不再重跑。
                self._ocr_consume_daily_high_sync(frame_rgb)
            elif hit_key == "egg_reward_title":
                # 进入彩蛋弹窗 → 标记「彩蛋识别进行中」。此后投递/点击不再依赖本帧
                # title 是否命中（title 有转场/闪断，单帧失配不应断识别或被点击抢跑）。
                self._egg_reading = True
            # 彩蛋识别进行中：持续投递识别（复用 OCR worker 线程，task="egg"），主线程零阻塞
            if self._egg_reading:
                self._run_egg_ocr(frame_rgb)
            # 等级提升弹窗（无 ROI，hit_key is None 且非彩蛋识别中）→ 无数据要读，直接盲点跳过
            return
        if s == "中标结算" or s == "领取分红":
            plan = getattr(self._detector, "plan", None)
            keys = plan.ocr_for(s) if plan is not None else _STAGE_OCR_KEYS.get(s)
            self._ocr_push(frame_rgb, keys=keys)
            return
        if not (s.startswith("第") and "回合" in s):
            return
        # 出价阶段：面板已开（S3，读 H/输入框）或已提交等结果（wait_result，读公开报价）
        # 才投递 OCR。S1/S2 面板未开不投递（输入框区域是别的 UI，投了浪费且可能误判 H）。
        dec = self._bidding_last_decision
        if (dec and dec.get("state", "").startswith("S3")) or self._bid_phase == "wait_result":
            plan = getattr(self._detector, "plan", None)
            base_keys = plan.ocr_for(s) if plan is not None else _STAGE_OCR_KEYS.get(s)
            if self._bid_phase == "wait_result":
                # wait_result 阶段：动态剔除已固化槽 → OCR 资源集中给未固化槽，
                # 尤其是最后展示的 P4（配合 P4 双通道，未固化槽刷新率自动提升≈两倍）。
                dynamic_keys = self._bid_dynamic_ocr_keys() if base_keys else base_keys
                self._ocr_push(frame_rgb, keys=dynamic_keys)
            else:
                # bidding 阶段只有 H 需要识别，报价槽不投递，用原 base_keys。
                self._ocr_push(frame_rgb, keys=base_keys)


    # ==================================================================
    #  内部：OCR 异步 worker（latest-only + provenance + 原子结果槽）
    # ==================================================================

    def _start_ocr_worker(self) -> None:
        """启动 OCR worker（daemon 线程）。RapidOCR 引擎为 _get_engine() 懒初始化，
        真正加载发生在 worker 首次 recognize_amounts() 时，即引擎冷启动在 worker 线程内，
        不阻塞主循环启动。"""
        if self._ocr_thread is not None and self._ocr_thread.is_alive():
            return
        self._ocr_thread = threading.Thread(
            target=self._ocr_worker_loop, name="treasure-ocr-worker", daemon=True
        )
        self._ocr_thread.start()
        logger.log("[鉴宝] OCR worker 已启动", "DEBUG")

    def _stop_ocr_worker(self) -> None:
        """停止 worker：set 停止 + 唤醒信号，join 等待退出。超时仅告警，不阻塞主循环退出。"""
        if self._ocr_thread is None:
            return
        self._ocr_stop.set()
        self._ocr_wakeup.set()  # 唤醒阻塞在 wait 的 worker 立即退出
        self._ocr_thread.join(timeout=2.0)
        if self._ocr_thread.is_alive():
            logger.log("[鉴宝] OCR worker 2s 内未退出", "WARNING")
        self._ocr_thread = None

    def _ocr_worker_loop(self) -> None:
        """worker 主循环：latest-only 取帧 → 关键 ROI 优先识别发布 → 阶段感知识别发布。
        顶层 try/except：单次识别异常不杀死 daemon，计数后继续。"""
        while not self._ocr_stop.is_set():
            try:
                item = self._ocr_pop_latest()
                if item is None:
                    # 无新帧：等待唤醒（wakeup/stop 会立即返回；timeout 是防 Event 丢失的兜底）
                    self._ocr_wakeup.wait(timeout=0.5)
                    self._ocr_wakeup.clear()
                    continue
                frame_id, round_no, frame, captured_ts, task, ocr_keys = item
                # 彩蛋识别任务：复用本 worker 线程串行执行（避免两个线程并发调同一 OCR 引擎）。
                # 识别器内部已含模板匹配+颜色+OCR，耗时几十 ms~百 ms 级，放后台不阻塞主循环。
                if task == "egg":
                    if self._egg_recognizer is not None:
                        t0 = time.time()
                        try:
                            egg_res = self._egg_recognizer.recognize(frame)
                        except Exception:
                            egg_res = None
                        self._egg_publish_result(egg_res, frame_id, captured_ts, t0)
                    continue
                # 第一段：关键 ROI（bid_result_amount_box + bid_player4 双通道）单独识别、立即发布。
                # 窗口期（偶发系统级慢）单 ROI 即使慢 15 倍也仅 ~200ms，age 仍低于
                # OCR_MAX_AGE_MS，保证 H 等关键数值先于全量结果落地，不被 18 ROI 长循环拖死。
                # bid_result_amount_box 必须允许 0：用户点✖清空后画面显示"¥0"，若 MIN_AMOUNT
                # 默认>0 把 0 滤成 None → _bid_input_latest 不更新 → 输入子状态机反复点✖死循环。
                # bid_player4 允许 0：掉线玩家的报价框显示 0 合法。
                # critical=True → 写独立关键槽，不被第二段全量覆盖（P4 双通道覆盖 bug 修复）。
                t0 = time.time()
                if self._ocr is not None:
                    res_crit = self._ocr.recognize_amounts(
                        frame, keys=self.OCR_CRITICAL_KEYS,
                        min_amounts={"bid_result_amount_box": 0, "bid_player4": 0},
                    )
                    self._ocr_publish_result(res_crit, frame_id, round_no, t0, captured_ts,
                                             critical=True)
                # 第二段：阶段感知 keys（投递时按阶段裁剪；None=全量，尽力而为）。
                # 窗口期超龄的结果会被主线程丢弃，此时关键 ROI 结果已由第一段保住。
                # 结算收入/利润允许 0 值。
                # 剔除关键通道 ROI（H/P4）：同帧 H/P4 已由第一段识别发布，第二段不再重复
                # 识别（省 ~20ms/帧），也不会覆盖关键槽结果。
                t0 = time.time()
                second_keys = (ocr_keys - self._OCR_CRITICAL_SET) if ocr_keys else None
                if self._ocr is not None:
                    res_full = self._ocr.recognize_amounts(
                        frame, keys=second_keys,
                        min_amounts={k: 0 for k in self.OCR_ZERO_ALLOWED_KEYS}
                    )
                else:
                    res_full = {}
                self._ocr_total_runs += 1
                self._ocr_duration_ms = (time.time() - t0) * 1000
                self._ocr_publish_result(res_full, frame_id, round_no, t0, captured_ts)
            except Exception as e:
                self._ocr_failures += 1
                logger.log(f"[鉴宝] OCR worker 异常: {e}", "WARNING")

    def _ocr_push(self, frame_rgb: np.ndarray, task: str = "ocr",
                  keys: frozenset[str] | None = None) -> None:
        """主线程投递最新帧（latest-only：覆盖旧帧，worker 慢时丢中间帧）。
        captured_ts = 投递时刻 ≈ 帧捕获时刻（同 tick 内 screencap 后立即投递），
        供时效老化 age = consume_time - captured_ts。
        frame 所有权：立即 copy，worker 与主线程不共享 buffer（不依赖 screencap
        返回新数组的隐含约束）。1280×720 RGB copy ~1ms，远小于 OCR 开销。
        task：任务类型。"ocr"=常规 ROI 识别；"egg"=彩蛋识别（复用同一 worker 线程，
        彩蛋阶段与其他 OCR 阶段互斥，同刻 pending 槽只会有一种任务）。
        keys：第二段识别的 OCR keys（阶段感知裁剪，见 _STAGE_OCR_KEYS）；None=全量。"""
        with self._ocr_lock:
            self._ocr_frame_id += 1
            self._ocr_pending = (
                self._ocr_frame_id,
                self._round_no,
                frame_rgb.copy(),
                time.time(),
                task,
                keys,
            )
        self._ocr_wakeup.set()  # 唤醒 worker 立即处理（无 queue，不积压）

    def _ocr_pop_latest(self) -> tuple[int, int | None, np.ndarray, float, str, frozenset[str] | None] | None:
        """worker 取走最新帧并清槽（latest-only）。"""
        with self._ocr_lock:
            item = self._ocr_pending
            self._ocr_pending = None
            return item

    def _ocr_publish_result(
        self, res: dict, frame_id: int, round_no: int | None, t0: float, captured_ts: float,
        critical: bool = False,
    ) -> None:
        """worker 写结果槽：完整新 dict 替换，不原地修改已发布对象。
        captured_ts = 帧捕获时刻（_ocr_push 记录），供主线程时效老化。
        critical=True → 写关键通道槽（第一段 H+P4，独立于全量槽，不被第二段覆盖）；
        critical=False → 写全量槽（第二段其余 ROI）。"""
        with self._ocr_lock:
            payload = {
                "frame_id": frame_id,
                "round_no": round_no,
                "captured_ts": captured_ts,
                "completed_ts": time.time(),
                "duration_ms": (time.time() - t0) * 1000,
                "data": res,
            }
            if critical:
                self._ocr_result_critical = payload
            else:
                self._ocr_result = payload

    def _ocr_take_result(self, critical: bool = False) -> dict | None:
        """主线程消费结果槽（取走即清空，避免重复应用）。"""
        with self._ocr_lock:
            if critical:
                res = self._ocr_result_critical
                self._ocr_result_critical = None
            else:
                res = self._ocr_result
                self._ocr_result = None
            return res

    def _apply_ocr_result(self) -> None:
        """消费 worker 双结果槽并应用业务状态。两道闸门，通过后委托给 _consume_ocr_result：
        ① provenance：round_no 与当前回合不匹配 → 丢弃（防旧回合结果串写新回合）；
        ② 时效老化：age = consume_time - captured_ts 超 OCR_MAX_AGE_MS → 丢弃
          （尖峰窗口期算出的陈旧帧不被当作当前状态；此时关键 ROI 已由优先通道保住）。

        双槽合并（P4 双通道覆盖 bug 修复）：关键槽（第一段 H+P4）与全量槽（第二段其余）
        各自独立过闸门，通过后合并成一份 res 再消费——H/P4 恒来自关键通道（时效最低、
        不被全量覆盖），P1~P3/玩家名等来自全量通道。帧元信息优先取关键槽。"""
        results: list[dict] = []
        for critical in (True, False):
            result = self._ocr_take_result(critical)
            if not result:
                continue
            self._ocr_source_frame_id = result["frame_id"]
            # 时效 = 捕获时刻 → 消费时刻（captured_ts 在投递时记录，≈帧捕获时刻）
            self._ocr_result_age_ms = (time.time() - result["captured_ts"]) * 1000
            if result["round_no"] != self._round_no:
                if self._round_no is None:
                    pass
                else:
                    logger.log(
                        f"[鉴宝] OCR 结果过期丢弃(结果R{result['round_no']}≠当前R{self._round_no}, "
                        f"耗时{result['duration_ms']:.0f}ms, 时效{self._ocr_result_age_ms:.0f}ms)",
                        "DEBUG",
                    )
                continue
            # 时效老化：陈旧帧不当作当前状态（窗口期全量 18 ROI 结果常在此被拦）
            if self._ocr_result_age_ms > self.OCR_MAX_AGE_MS:
                logger.log(
                    f"[鉴宝] OCR 结果超龄丢弃(R{result['round_no']} 帧{result['frame_id']} "
                    f"时效{self._ocr_result_age_ms:.0f}ms>{self.OCR_MAX_AGE_MS:.0f}ms, "
                    f"耗时{result['duration_ms']:.0f}ms)",
                    "DEBUG",
                )
                continue
            results.append(result)

        if not results:
            return
        # 帧元信息优先取关键槽（先 take、先入列），缺失时用全量槽
        meta = results[0]
        res: dict = {}
        for result in results:
            res.update(result["data"])
        if not res:
            return
        self._consume_ocr_result(res)
        # 快照构建：wait_result 阶段 4 槽完整后替换 _last_round_snapshot（§3.1 v0.3.5）
        self._maybe_build_snapshot()
        # 逐帧 OCR 指标日志：与 debug 图 OCR 卡同源（total/dur_ms/age_ms）。
        # 心跳日志每 DEBUG_LOG_INTERVAL 帧才打一次，尖峰（如 1900ms）会被跳过，
        # 这里每次消费都记录，保证日志能看到 debug 图显示的每一个 OCR 耗时。
        logger.log(
            f"[鉴宝] OCR 结果 R{meta['round_no']} 帧{meta['frame_id']} 已应用: "
            f"耗时{meta['duration_ms']:.0f}ms, 时效{self._ocr_result_age_ms:.0f}ms, "
            f"累计{self._ocr_total_runs}次",
            "DEBUG",
        )

    # ---------- 每日划分（凌晨 5 点为界）----------
    def _refresh_daily_bucket(self, now: datetime | None = None) -> None:
        """跨「日」重置当日循环计数。凌晨 5 点为新一天起点：
        05:00 ~ 次日 04:59:59 属于同一天；跨桶时状态机/OCR 计数清零重计。
        首次调用只记桶不重置（启动时本就为 0）。
        """
        now = now or datetime.now()
        day = now.date() if now.hour >= 5 else now.date() - timedelta(days=1)
        bucket = day.isoformat()
        if self._daily_bucket is None:
            self._daily_bucket = bucket
        elif bucket != self._daily_bucket:
            self._daily_bucket = bucket
            self._session_daily_done_count = 0
            self._session_daily_ocr_count = None
            logger.log(
                f"[鉴宝] 跨日（凌晨5点计日）：{bucket} 起新一轮，今日已完成场数重置为 0",
                "INFO",
            )

    # ---------- 辅助：OCR 解析「日已参与次数」文本 → int|None ----------
    @staticmethod
    def _parse_daily_session_count(raw_text: str) -> int | None:
        """解析 `X/50` / `13 / 50 场` / `已参与13场` 等文本 → 左侧整数 X ∈ [0,50]。

        识别优先级：
          1) 斜杠分数：匹配 `(\\d+)\\s*/\\s*\\d+`（X/50 / X / 50 / X/50场 都命中），返回 X
          2) 「X场」字面：`(\\d+)\\s*场`
          3) 兜底：取整串中第一段"无符号/非数字夹着的连续数字"，∈[0,50] 就接受。
             严格限定：数字段的左侧不能是 -（避免 `-1` 读成 1），左右不能是其它数字（避免从中间截断）。
        任何情况 X 不在 0~50 区间 → 返回 None（宁可丢也不乱记）。
        """
        if not raw_text:
            return None
        s = raw_text.strip()
        # 1) 斜杠分数
        m = re.search(r"(\d+)\s*/\s*\d+", s)
        if m:
            x = int(m.group(1))
            return x if 0 <= x <= 50 else None
        # 2) X场
        m = re.search(r"(\d+)\s*场", s)
        if m:
            x = int(m.group(1))
            return x if 0 <= x <= 50 else None
        # 3) 纯数字兜底（数字前后不接 - 或其它数字；优先匹配左侧非数字的第一串）
        m = re.search(r"(?<![\-\d])(\d+)(?!\d)", s)
        if m:
            x = int(m.group(1))
            return x if 0 <= x <= 50 else None
        return None

    def _update_daily_count_from_text(self, raw_text: str) -> None:
        """解析「日已参与 X/50」文本并更新计数（单调 + 交叉追平状态机侧）。

        供两处调用：worker 全量结果（_consume_ocr_result，仅鉴宝大厅阶段）与
        鉴宝大厅阶段的同步单 ROI 识别（_ocr_consume_daily_count_sync）。
        """
        daily = self._parse_daily_session_count(raw_text)
        if daily is None:
            return
        # 单调 + 合理范围：只有比旧值大/相等 才写（防止跳帧读到 1 又读成 0 的乱帧）
        if (self._session_daily_ocr_count is None) or (daily >= self._session_daily_ocr_count):
            if self._session_daily_ocr_count != daily:
                prev = self._session_daily_ocr_count
                self._session_daily_ocr_count = daily
                logger.log(
                    f"[鉴宝循环] OCR 日已参与次数 = {daily}（原始: {raw_text!r}"
                    f"，旧值={prev if prev is not None else '空'}）",
                    "DEBUG",
                )
                # 交叉修正：OCR 读到 10，但状态机侧才记 8 → 说明中间有手动参与/断点启动，
                # 直接让状态机侧追上到 OCR 数（避免"状态机落后，算不出已达上限"）。
                if daily > self._session_daily_done_count:
                    self._session_daily_done_count = daily
                    logger.log(
                        f"[鉴宝循环] 状态机侧计数 已追平 OCR={daily}",
                        "DEBUG",
                    )

    def _ocr_consume_daily_count_sync(self, frame_rgb: np.ndarray) -> None:
        """鉴宝大厅(选择场次)阶段的每日计数同步识别（单 ROI，快）。

        为什么同步不走 worker：鉴宝大厅每场只停留 1~2 秒，worker 异步识别返回时
        阶段往往已切走（匹配中/出价），会被 _apply_ocr_result 的 round_no/时效门控
        以及 _consume_ocr_result 的 stage 门控丢弃 → 计数永远读不到（日志 OCR侧=--）。
        单 ROI 同步识别 ~20-50ms，不阻塞 300ms 主循环。
        """
        if self._ocr is None:
            return
        try:
            rect = self._ocr._regions.get("session_daily_count")
            if rect is None:
                return
            info = self._ocr.recognize_single(frame_rgb, rect) or {}
        except Exception:
            return  # 单 ROI 识别异常不致命，下帧重试
        raw_text = str(info.get("text") or "").strip()
        if raw_text:
            self._update_daily_count_from_text(raw_text)

    def _ocr_consume_daily_high_sync(self, frame_rgb: np.ndarray) -> None:
        """结算弹窗①「今日最高积分上涨」的积分同步识别（单 ROI，快）。

        弹窗靠 3 帧连点点穿、停留短 → 不走 worker（异步返回时阶段已切走），
        同步单 ROI ~10ms 不阻塞 300ms 主循环；已读到积分则不再重复识别。
        仅记录新纪录值，不影响弹窗点穿决策（用户 2026-08-15 确认要识别）。
        """
        if self._ocr is None or self._daily_high_score is not None:
            return
        try:
            rect = self._ocr._regions.get("daily_high_score")
            if rect is None:
                return
            # min_amount=0：积分值可能小于通用金额下限 MIN_AMOUNT(10000)，放行小值
            info = self._ocr.recognize_single(frame_rgb, rect, min_amount=0) or {}
        except Exception:
            return  # 单 ROI 识别异常不致命，下帧重试
        amt = info.get("amount")
        if amt is None:
            return  # 没读到数字（转场动画期字未稳），下帧重试
        self._daily_high_score = amt
        logger.log(f"[鉴宝弹窗①] 今日最高积分上涨: {amt:,}", "INFO")

    def _reset_bid_slots(self) -> None:
        """每回合首次消费报价时重置 4 槽固化状态（由 _consume_ocr_result 对比 _bid_slots_round 触发）。

        槽状态机三口径统计（debug 图显示，用户拍板「消费/输出/命中」三口径全统计）：
          consumed = 本回合该槽被 OCR 消费过的帧数（单调上涨，判断「读了多少帧」）
          output   = 该槽在 OCR 结果中出现过的次数（是否有输出）
          hits     = 读到有效数字的次数（识别命中率，排除空读）
        """
        self._bid_slots = {
            pid: {
                "val": -1,          # 当前值（-1=未读；0=读到掉线/空报价 合法）
                "stable": 0,        # 连续一致帧数（固化条件）
                "locked": False,    # 是否固化（固化后停止该槽 OCR）
                "miss": 0,          # 连续无输出帧数（≥ BID_SLOT_MISS_LIMIT 清空重读）
                "consumed": 0,      # 消费次数
                "output": 0,        # 输出次数
                "hits": 0,          # 命中次数（读到有效数字）
            }
            for pid in (1, 2, 3, 4)
        }

    def _bid_dynamic_ocr_keys(self) -> frozenset[str]:
        """出价阶段动态 OCR keys：剔除已固化槽（用户规则：固化→停止该回合该槽 OCR），
        OCR 资源集中给未固化槽，尤其最后展示的 P4（配合 P4 双通道提升刷新率）。
        H/玩家名/回合小字等非报价槽恒在。无固化槽时直接复用全量 _BID_OCR_KEYS（避免每帧重建 frozenset）。"""
        locked = {pid for pid, s in self._bid_slots.items() if s.get("locked")}
        plan = getattr(self._detector, "plan", None)
        bid_keys = plan.ocr_for(self._current_stage) if plan is not None else _BID_OCR_KEYS
        if not locked:
            return bid_keys or frozenset()
        return frozenset(
            k for k in (bid_keys or ())
            if not (k.startswith("bid_player") and k[-1].isdigit() and int(k[-1]) in locked)
        )

    def _consume_ocr_result(self, res: dict) -> None:
        """纯业务逻辑：消费 OCR 识别结果，更新 H/出价/结算/余额等状态。
        主程序通过 _apply_ocr_result（worker 异步）调用；离线脚本同步 OCR 后直接调用。
        不依赖 ctx / worker / 线程，只依赖 self 的状态字段。"""
        r = self._round_no

        # 每日以凌晨5点为界：跨日先重置当日计数，避免把昨天的累计带到新一天
        self._refresh_daily_bucket()

        # --------- 场次选择页左下角「日已参与次数 X/50场」----------
        # 仅在「鉴宝大厅(选择场次)」阶段才消费：非该阶段 ROI 可能读相邻页的乱字符（大厅/活动页等）。
        # 识别正则：优先匹配 "X/50" / "X 场" / "X / 50"；都没命中但提取到纯数字 X∈[0,50] 也接受（OCR 把 /50 读丢时兜底）。
        if self._current_stage == "鉴宝大厅(选择场次)":
            info = res.get("session_daily_count")
            if isinstance(info, dict):
                raw_text = str(info.get("text") or "").strip()
                if raw_text:
                    self._update_daily_count_from_text(raw_text)

        # 弹窗中心金额 = 智能出价填入的系统报价（R1~R3 用来推真实估值）→ set_h
        box = res.get("bid_result_amount_box")
        if box is not None:
            amt = box.get("amount") or 0
            # 输入框当前值无条件更新：有数字取数字，OCR 明确读到无数字（已清空/占位）→ 0。
            # 0 值也必须更新——清空后若不更新，输入子状态机会以为旧值还在，反复点✖死循环。
            self._bid_input_latest = amt
            # H 只在「面板已打开 + 非转场期」采集：H 的定义 = 点智能出价后系统填入输入框的报价，
            # 只有面板打开（bidding phase）且画面稳定（非转场动画乱帧）时才存在。
            # 实测事故：R5 进入后 phase 仍是 wait_result（上回合快照未构建完），转场动画中
            # 输入框 ROI 读到残缺数字 106 被当成 H 锁定 → 估值崩成 146。
            # 面板未开（wait_result/wait_first 等）时输入框显示的是"已出价 X"或乱帧，绝不能当 H。
            if amt and self._bid_phase == "bidding" and not self._in_transition:
                self.set_h(amt)

        # 4 个玩家出价（读面积记忆）—— 仅出价阶段（r 非 None）消费。
        # 槽级固化状态机（用户拍板规则）：
        #   · 读到数字：同值→stable+1；异值→val=新值,stable=1（误读稳定不了没关系，反正连续3次一致才固化）
        #   · 固化：stable≥BID_SLOT_STABLE_FRAMES 且前置槽已固化 → locked，写 _player_bids，停止该槽 OCR
        #   · 读到过任何值(val≠-1) 且未固化，本帧无输出 → miss+1；≥BID_SLOT_MISS_LIMIT → 清空重读(val=-1)
        #   · 三口径统计（debug 图显示）：consumed=本帧被消费 / output=有输出 / hits=命中有效数字
        # 关键：只遍历 res 出现的 key 会让"无输出槽"永远不进循环 → miss 加不上；
        # 必须每次消费对全部未固化槽统一做「本帧有无输出」判定（用户规则：连续3帧识别不到东西就清空该槽）。
        if r is not None:
            # 回合变化（或首帧）→ 重置 4 槽（每回合报价独立）
            if self._bid_slots_round != r or not self._bid_slots:
                self._reset_bid_slots()
                self._bid_slots_round = r
            for pid in (1, 2, 3, 4):
                slot = self._bid_slots[pid]
                if slot["locked"]:
                    continue  # 已固化：停止识别该槽（动态 keys 已剔除，这里兜底跳过）
                slot["consumed"] += 1  # 本帧被 OCR 消费（三口径之消费）
                info = res.get(f"bid_player{pid}")
                amt = info.get("amount") if isinstance(info, dict) else None
                text = str(info.get("text") or "") if isinstance(info, dict) else ""
                # 三态提交判定（原逻辑保留）：只有 OCR 明确读到状态（"出价中"/"已出价"/金额）
                # 才写 _bid_player_submitted；空读取（text 空且无金额，网卡/动画残缺常见）不覆盖，
                # 保持上次状态。这样 wait_result 的假下降沿判定不会被"空读取=未提交"误触发
                # （网卡导致画面卡住时 OCR 连续读到空，若当成"出价中"会误判提交失败 → 丢数据）。
                if "出价中" in text or "已出价" in text or (amt is not None and amt > 0):
                    self._bid_player_submitted[pid] = ("已出价" in text) or (amt is not None and amt > 0)
                # 本帧无输出（识别不到东西）：未固化 + 已读到过值 → miss+1，连续超限清空重读。
                if not (text or amt is not None):
                    if slot["val"] != -1:
                        slot["miss"] += 1
                        if slot["miss"] >= self.BID_SLOT_MISS_LIMIT:
                            logger.log(
                                f"[鉴宝] 槽{pid} 连续 {self.BID_SLOT_MISS_LIMIT} 次无输出，"
                                f"清空重读（旧值 {slot['val']:,}，"
                                f"消费{slot['consumed']}/输出{slot['output']}/命中{slot['hits']}）",
                                "DEBUG",
                            )
                            slot["val"] = -1
                            slot["stable"] = 0
                            slot["miss"] = 0
                    continue
                slot["output"] += 1  # 有输出（三口径之输出）
                slot["miss"] = 0
                # 只有「真没读到数字」才跳过（空读=None，保持 -1 哨兵）；读到数字 0 也要落盘，
                # 否则玩家掉线报价=0 会被当成未读，快照 4 槽永远凑不齐 → 整场锁死（2026-08-19）。
                if amt is None or (not amt and amt != 0):
                    continue
                slot["hits"] += 1  # 命中有效数字（三口径之命中）
                # 前置槽约束（用户规则「前一槽位有数据」）：前置槽读到过任何值（val≠-1）
                # 才放行本槽推进。不要求前置槽已固化——报价逐条展示，前置槽开始显示即代表
                # 轮到本槽；若要求前置 locked，前置槽误读不稳定会拖死后续槽（用户确认
                # 「误读稳定不了没关系」，每槽独立 3 次一致才固化）。
                prev_pid = pid - 1
                if prev_pid >= 1 and self._bid_slots[prev_pid]["val"] == -1:
                    continue
                # 回合切换转场期：SWITCH_CONFIRM_FRAMES 帧内不推进稳定计数。
                # R3→R4 等切换瞬间画面旧数字收缩淡出，会短暂识别出"末尾缺0"残缺值
                # （如 209,500→20,950）；残缺值不干扰固化，动画稳定后再正常累积。
                if self._in_transition:
                    continue
                # 稳定计数：同值累积；异值重置为1（误读稳定不了没关系，连续3次一致才固化）
                if slot["val"] == amt:
                    slot["stable"] += 1
                    if slot["stable"] >= self.BID_SLOT_STABLE_FRAMES:
                        slot["locked"] = True
                        lst = self._player_bids.setdefault(f"玩家{pid}", [-1] * 5)  # -1=未读；0=掉线合法
                        if 1 <= r <= len(lst):
                            lst[r - 1] = amt
                        logger.log(
                            f"[鉴宝] 槽{pid} 固化第{r}回合出价 = {amt:,}"
                            f"（连续{slot['stable']}次一致，"
                            f"消费{slot['consumed']}/输出{slot['output']}/命中{slot['hits']}）",
                            "INFO",
                        )
                else:
                    slot["val"] = amt
                    slot["stable"] = 1

            # bid_history1~4 已于 2026-08 删除：历史回合出价在各自回合由 bid_playerX 实时写入
            # _player_bids（累积表），bid_history 仅提供稳定补全/锁定，删掉省 4 个 ROI 识别。

        # 我方名次：玩家名区域带「（我）」标记，行序即名次。
        # 防抖：连续 RANK_STABLE_FRAMES 帧读到同一槽号才采纳（set_rank 只在值变化时更新，
        # 单帧误读会锁死错误排名；进入对局动画期尤其容易读错行，2026-08-16 反馈）。
        for key, info in res.items():
            if not (key.startswith("player_name") and key[-1].isdigit()):
                continue
            if "我" in info.get("text", ""):
                cand = int(key[-1])
                if cand == self._rank_candidate:
                    self._rank_candidate_frames += 1
                else:
                    self._rank_candidate = cand
                    self._rank_candidate_frames = 1
                if self._rank_candidate_frames >= self.RANK_STABLE_FRAMES:
                    self.set_rank(cand)
                break

        # --------- 结算页 4 项（竞拍失败/成功 → 领取分红） ---------
        # settle_final_price = 最终竞拍价（最高出价人拿下的实际金额）
        # settle_total_price = 拍品总价（= 真实估值，用这个直接验证 sysmax_13×1.35/1.4 准不准）
        # settle_profit = 利润（中标者的盈亏，负数=中标者亏钱=上头秒杀亏）
        # settle_my_income = 本场收入（我方分红，正数=我赚）
        #
        # 防呆规则：
        #   1) 0 是合法值（未分红 / 0 利润），按 OCR_ZERO_ALLOWED_KEYS 约定保留；
        #   2) final_price / total_price / profit：
        #        正数最小 MIN_SETTLE_AMOUNT，相对 sysmax_ref 的 1/20 下限，避免 OCR 裁位残缺；
        #   3) settle_my_income（我方分红）：独立规则
        #        - 绝对下限 1（元），允许小额正数（如 1,880 元的分红是合法的）；
        #        - 不套 sysmax_ref × 1/20 相对下限（分红本来就是总价的很小比例）。
        # （结算页金额 ROI 右侧非常窄，极易裁掉最后几位；用户实测 settle_my_income
        #   曾从 200,000 被裁成 2 → 直接触发结算阶段语义误判 + 后续阶段跳回大厅）。
        # 大金额三项共用：
        big_settle_map = [
            ("settle_final_price", "_settle_final_price"),
            ("settle_total_price", "_settle_total_price"),
            ("settle_profit",      "_settle_profit"),
        ]
        MIN_SETTLE_AMOUNT = 5000
        valid_hist = [v for v in self._h_prices if v and v > 0]
        sysmax_ref = max(valid_hist) if valid_hist else None
        for key, attr in big_settle_map:
            info = res.get(key)
            if not info:
                continue
            # amount 走 _extract_amount：优先千分位逗号金额，负数保留符号（利润/收入会为负）
            amt = info.get("amount")
            if amt is None:
                continue
            # 1) 绝对下限（0 允许，正数最小 5000）
            if amt != 0 and 0 < abs(amt) < MIN_SETTLE_AMOUNT:
                logger.log(
                    f"[鉴宝] OCR {key} = {amt:,} 丢弃：正数<{MIN_SETTLE_AMOUNT:,}，"
                    f"判定为 OCR 裁位残缺", "WARNING",
                )
                continue
            # 2) 相对历史 H 下限（sysmax_ref 存在时：最终竞拍价/总价/利润绝对值
            #    任一都不该比 Hmax 的 1/20 还低）
            if sysmax_ref is not None and amt != 0:
                rel_floor = sysmax_ref // 20
                if abs(amt) < rel_floor:
                    logger.log(
                        f"[鉴宝] OCR {key} = {amt:,} 丢弃：< 历史Hmax {sysmax_ref:,} 的 1/20 "
                        f"（阈值 {rel_floor:,}），判定为 OCR 裁位残缺", "WARNING",
                    )
                    continue
            prev = getattr(self, attr)
            if prev == amt:
                continue
            setattr(self, attr, amt)
            logger.log(f"[鉴宝] OCR {key} = {amt:,}" + (f"（覆盖旧值{prev:,}）" if prev else ""), "DEBUG")
        # settle_my_income 单独判定（本场收入/收益，正数=赚，负数=亏，0=未分红）：
        # 只防 1 类误读：裁位残缺 → 个位/十位数字（|amt|<10 基本不可能是真实收入）。
        # 注意：负数 = 我方拍下且亏损（结算页 ROI 显示的就是我方的收入，负值合法），
        # 不能像 settle_profit 那样当串位丢弃——否则亏损场 _settle_my_income 恒 None，
        # 领取分红阶段永远等不到"数据已加载"而不再点第二次领取。
        # 用户截图本场收入 = 1,880 元，远 < 5000，原来 MIN_SETTLE_AMOUNT=5000 会误丢。
        info = res.get("settle_my_income")
        if info:
            amt = info.get("amount")
            if amt is not None:
                if amt != 0 and abs(amt) < 10:
                    logger.log(
                        f"[鉴宝] OCR settle_my_income = {amt:,} 丢弃：|amt| < 10，"
                        f"判定为 OCR 裁位残缺", "WARNING",
                    )
                else:
                    prev = self._settle_my_income
                    # 同场结算页内突变过滤：本场收入不会从 311 跳到 300,000（>5 倍）。
                    # 实测事故：settle_my_income=311（真）→ 300,000（ROI 串位把相邻金额读进来，
                    # 与同场 final_price 230,000 / total 223,773 都对不上）。
                    mutated = (
                        prev is not None and prev != 0 and amt != 0
                        and (abs(amt) > abs(prev) * 5 or (abs(amt) > 0 and abs(amt) < abs(prev) * 0.2))
                    )
                    if mutated:
                        logger.log(
                            f"[鉴宝] OCR settle_my_income = {amt:,} 丢弃：相对已有值 "
                            f"{prev:,} 突变超过 5 倍，判定为 ROI 串位错值", "WARNING",
                        )
                    elif prev != amt:
                        self._settle_my_income = amt
                        # 收入读出 = 跳过动画已生效 → 无响应计时/重试计数归零
                        self._settle_skip_since = 0
                        self._settle_skip_retry_count = 0
                        logger.log(
                            f"[鉴宝] OCR settle_my_income = {amt:,}"
                            + (f"（覆盖旧值{prev:,}）" if prev is not None else ""),
                            "DEBUG",
                        )

        # --------- 我方金币余额（出价面板右上角 HUD，出价阶段才显示） ---------
        # 锁定规则：只在回合阶段（round_no>=1）才消费 my_balance。
        #   进入 R1 前（大厅/匹配/选鉴宝师等）不出价，读到的全局总值无意义，直接忽略不写。
        #   三态语义（重要）：
        #     - _my_balance = None  → OCR 未读到数字（ROI 未识别/无输出）＝余额未知，需兜底
        #     - _my_balance = 0     → OCR 明确读到 0 ＝ 真实没钱（合法值，不能当"未读到"）
        #     - _my_balance > 0     → 正常余额
        #   锁定规则：进入 R1 后首次读到数字（含 0，OCR 有 amount 输出即锁定）；锁定后不覆盖。
        #   原因：出价操作不扣余额（余额在结算时才变动），程序不需要关心余额怎么变。
        info = res.get("my_balance")
        if info is not None and not self._balance_locked and (self._round_no or 0) >= 1:
            amt = info.get("amount")
            # amt 是 int（含 0）→ 真实读到数字；amt 是 None → 该 ROI 识别到文本但无数字（视作未读到）
            if isinstance(amt, int):
                self._balance_locked = True
                if self._my_balance != amt:
                    prev_bal = self._my_balance
                    self._my_balance = amt
                    logger.log(
                        f"[鉴宝] OCR my_balance = {amt:,}（首回合识别，锁定）"
                        + (f"（覆盖旧值{prev_bal:,}）" if prev_bal is not None else ""),
                        "DEBUG",
                    )

    def _accept_stage(self, new_stage, new_round, *, immediate=False):
        """过滤层：返回 (accepted_stage, accepted_round)。
        1. 回合号单调递增，禁止回退
        2. **阶段名单调**：按 STAGE_ORDER 索引，只允许保持或前进（结算→分红→下一场大厅的循环除外）；
           典型非法：「第2回合出价」→「鉴宝大厅(选择场次)」（索引从 7 退到 2）→ 拒绝。
        3. 候选需连续 STABLE_FRAMES 帧一致才采纳（防抖）
        4. immediate=True 的强特征立即接受（重置候选）"""
        # --- 阶段顺序约束（只前进 / 或特殊合法跳转），防止 OCR 残缺导致越级回退 ---
        cur_stage = self._det_stage
        if cur_stage is not None and new_stage is not None and cur_stage != new_stage:
            try:
                cur_idx = self.STAGE_ORDER.index(cur_stage)
                new_idx = self.STAGE_ORDER.index(new_stage)
            except ValueError:
                cur_idx, new_idx = -1, -1
            if cur_idx >= 0 and new_idx >= 0:
                # 特殊合法回退：领取分红/结算弹窗 → 游戏大厅 / 鉴宝大厅(选择场次)。
                # 下一场开始：点领取后可能回游戏大厅开新场，也可能直接回场次选择页继续打；
                # 弹窗链（今日最高/等级提升/彩蛋）可能只触发部分/不触发，跳过某 stage 直接回大厅。
                # 若不放行 → 会被当非法回退拒绝 → stage 卡死在弹窗（2026-08-15 预演洞2）。
                #
                # ---- 弹窗链回退连续稳定帧确认（2026-08-16 组合场景洞修复）----：
                # 弹窗链阶段 → 大厅/选场次 的回退不能单帧放行：
                #   ① 动画期（真领取/点穿/彩蛋关闭后 1~2 帧）模板匹配不上弹窗，
                #      检测器可能读到大厅背景 → 单帧放行 = 弹窗链被跳过（上一条实测洞）；
                #   ② 弹窗链内弹窗切换（今日最高→彩蛋等）的间隙，检测器可能短暂读到
                #      大厅背景 → 单帧放行 = 弹窗链被跳过。
                # 故回退需连续 POPUP_LOOPBACK_STABLE_FRAMES 帧都识别到回退目标才放行。
                # 用独立计数器而非 _popup_click_cooldown：_decide_action 的阶段感知清零
                # 会让冷却在弹窗链阶段恒为 0，门控失效。
                allow_popup_loopback = (
                    cur_stage in ("领取分红", "结算弹窗")
                    and new_stage in ("游戏大厅", "鉴宝大厅(选择场次)")
                )
                if allow_popup_loopback:
                    # 累计回退确认帧：达阈值才放行，未达则拒绝（保持当前弹窗阶段）
                    self._popup_loopback_frames += 1
                    if self._popup_loopback_frames >= self.POPUP_LOOPBACK_STABLE_FRAMES:
                        self._popup_loopback_frames = 0
                        allow_popup_loopback = True
                    else:
                        allow_popup_loopback = False
                else:
                    # 非弹窗链回退场景：重置确认计数
                    self._popup_loopback_frames = 0
                if new_idx < cur_idx and not allow_popup_loopback:
                    # 「第X回合」→ 中标结算 / 领取分红（R5 结束的正常跳转）按 idx 是前进的，新_idx
                    #   10/11 必然 > 回合阶段的 idx（6~9），所以无例外，直接按 "前进" 自然过。
                    # 弹窗链回退确认期（动画/不识别弹窗）被挡下时高频触发 → 节流为每 10 帧一次 WARNING。
                    if self._frame_counter % 10 == 0:
                        logger.log(
                            f"[鉴宝] 阶段候选回退被拒绝：{cur_stage}(idx{cur_idx}) → "
                            f"{new_stage}(idx{new_idx})，保持当前阶段不变", "WARNING",
                        )
                    return (self._det_stage, self._det_round)

        # 回合单调约束
        if self._det_round is not None and new_round is not None and new_round < self._det_round:
            return (self._det_stage, self._det_round)  # 回退 → 拒绝

        if immediate:
            self._cand_stage, self._cand_round, self._cand_count = None, None, 0
            if new_stage is not None:
                self._det_stage = new_stage
            if new_round is not None:
                self._det_round = new_round
            return (self._det_stage, self._det_round)

        # 候选一致 → 累计；不一致 → 重新数
        if new_stage == self._cand_stage and new_round == self._cand_round and new_stage is not None:
            self._cand_count += 1
        else:
            self._cand_stage, self._cand_round, self._cand_count = \
                new_stage, new_round, 1 if new_stage is not None else 0

        if self._cand_count >= self.STABLE_FRAMES and self._cand_stage is not None:
            self._det_stage, self._det_round = self._cand_stage, self._cand_round
            self._cand_stage, self._cand_round, self._cand_count = None, None, 0
        return (self._det_stage, self._det_round)

    # ==================================================================
    #  内部：画面变化检测（基于灰度直方图差的简化方案）
    # ==================================================================

    def _detect_change(self, frame_rgb: np.ndarray) -> bool:
        """检测画面是否有显著变化。返回 True 时保存事件截图。
        加入 GaussianBlur 去噪 + 冷却期，抑制同屏动画/轮播反复触发。"""
        try:
            # 缩小到 320×180 加速 + 高斯模糊去噪
            small = cv2.resize(frame_rgb, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
            gray = cv2.GaussianBlur(gray, (3, 3), 0)
            if self._prev_gray is None:
                self._prev_gray = gray
                return False
            diff = cv2.absdiff(gray, self._prev_gray)
            changed_pixels = int(np.sum(diff > self.CHANGE_PIXEL_THRESH))
            total = gray.size
            ratio = changed_pixels / total
            self._prev_gray = gray
            changed = ratio > self.CHANGE_AREA_RATIO
            if changed:
                # 冷却期：5 秒内不重复触发
                if time.time() - self._last_change_ts < self.CHANGE_COOLDOWN_S:
                    return False
                self._last_change_ts = time.time()
            return changed
        except Exception:
            return False

    # ==================================================================
    #  内部：日志 & 辅助
    # ==================================================================

    @staticmethod
    def _extract_round_from_stage(stage: str | None) -> int | None:
        """解析「第N回合」原始数字（N 任意，附加回合第6+回合也能提取；不 clamp，
        clamp 由调用方按「附加回合统一写进第5回合槽」的约定处理）。"""
        m = re.search(r"第(\d+)回合", stage or "")
        return int(m.group(1)) if m else None

    @property
    def _in_transition(self) -> bool:
        """新回合切换后的前 SWITCH_CONFIRM_FRAMES 帧 = 转场期（动画残缺值高发期）。
        转场期内 bid_player 不写当前回合槽。"""
        return self._round_elapsed < self.SWITCH_CONFIRM_FRAMES

    @property
    def _current_h(self) -> int | None:
        if self._round_no and 1 <= self._round_no <= len(self._h_prices):
            v = self._h_prices[self._round_no - 1]
            return v if v > 0 else None
        return None

    @property
    def _sysmax_13(self) -> int | None:
        """全 5 回合系统报价最大值 → 估值基准（×1.35 求稳 / ×1.4 激进）。
        5 个回合的 H（智能出价填入的输入框值）都参与最大值判断（不只前 3 回合）；
        只要任意回合已记录 >0 即返回最大值；尚未有任何报价返回 None。"""
        vals = [v for v in self._h_prices if v and v > 0]
        return max(vals) if vals else None

    @property
    def _valuation_lo(self) -> int | None:
        """求稳估值：sysmax_13 × 1.35"""
        m = self._sysmax_13
        return int(m * 1.35) if m else None

    @property
    def _valuation_hi(self) -> int | None:
        """激进估值：sysmax_13 × 1.4"""
        m = self._sysmax_13
        return int(m * 1.4) if m else None

    @property
    def _vhat_strategy(self) -> int | None:
        """策略决策实际用的估值：VAL_COEF × sysmax_13（与 BidStrategy._vhat 同口径）。

        debug 图必须显示这个值，否则图和决策对不上（曾出现 val_lo/hi 用 1.35/1.4，
        决策却用 1.28，图上看着该买、决策却判超估值）。"""
        m = self._sysmax_13
        return int(m * VAL_COEF) if m else None

    @property
    def _current_our_bid(self) -> int | None:
        # 优先：_our_bids 已记录（本应经 set_our_bid 手动注入，但那方法真实执行路径无调用点，
        # 详见下——实际是靠 _player_bids 我方槽位回退提供数据）。
        if self._round_no and 1 <= self._round_no <= len(self._our_bids):
            v = self._our_bids[self._round_no - 1]
            if v > 0:
                return v
        # 回退：我方出价从 _player_bids 我方槽位读（bid_playerX ROI 累积表，含自己）。
        # 实测：_our_bids 在生产中恒为空（无写入调用），若不回退，debug 图"我方出价"一直 "-"。
        # 4 个玩家出价都 OCR 到了 _player_bids（f"玩家{my_rank}" = 我方槽位），直接用它。
        if self._round_no and self._my_rank and 1 <= self._my_rank <= 4:
            lst = self._player_bids.get(f"玩家{self._my_rank}")
            if lst and 1 <= self._round_no <= len(lst):
                v = lst[self._round_no - 1]
                return v if v > 0 else None
        return None

    def _log_stage_changed(self, new_stage: str, reason: str):
        if new_stage == self._last_stage_logged:
            return
        self._last_stage_logged = new_stage
        logger.log(f"[鉴宝] 进入阶段: {new_stage} [{reason}]", "INFO")


