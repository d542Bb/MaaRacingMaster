#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性引导脚本：由 v2 真源生成鉴宝 v3 资产 `treasure_assets.json`。

**这是一次性工具，不是构建步骤。**
生成之后 `treasure_assets.json` 就是"唯一人写文件"，由控制台（S4b）编辑维护，
本脚本不再参与。保留它的唯一理由是可审计：任何人都能重跑它，确认上纸过程
没有偷偷改过一个 rect / threshold / templates（用 `diff_v2_v3` 校验）。

为什么用脚本生成而不是手抄 JSON
--------------------------------
v2 的 53 个 rect 是 17 位有效数字的归一化浮点（例如 0.2829629629629631）。
手工转录必错，而 §9.1 的合入闸门要求"搬迁与改动分开提交"——一旦 rect 抄错，
回归失败就无法归因到底是搬迁错了还是调参了。走迁移器搬运，rect 由代码逐位复制。

语义（kind / owner / page / label / order / arbitration / guarded_by / transitions）
在这里**显式给出**，来源全部是 §2.2 的 Python 常量与本次人工判定，逐条带出处注释。

用法：
    python tools/navkit/build_assets.py            # 生成（覆盖）
    python tools/navkit/build_assets.py --check    # 只校验现有文件与 v2 等价，不写
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_PROJ = Path(__file__).resolve().parents[2]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from maaracing_assistant.core.navkit import (  # noqa: E402
    Assets,
    diff_v2_v3,
    migrate_v2_to_v3,
    validate_assets,
)

MODULE = "treasure"
V2_PATH = (
    _PROJ / "maaracing_assistant" / "plugins" / "treasure"
    / "resources" / "config" / "treasure_rois.json"
)
IMAGE_DIR = _PROJ / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "image"
OUT_PATH = (
    _PROJ / "maaracing_assistant" / "plugins" / "treasure"
    / "resources" / "config" / "treasure_assets.json"
)

# ==================================================================
# 语义表（人工判定，出处见每条注释）
# ==================================================================

# 页面划分：v2 只有"用途段"，页面是 v3 树的分组层，由人按游戏实际界面划分。
# 判据 = "这堆元素是否出现在同一屏且共同构成一次可停留的界面状态"。
PAGES: dict[str, dict[str, str]] = {
    "hall":      {"label": "游戏大厅"},
    "activity":  {"label": "活动页面"},
    "session":   {"label": "鉴宝大厅(选择场次)"},
    "matching":  {"label": "匹配中"},
    "appraiser": {"label": "选择鉴宝师"},
    "bidding":   {"label": "出价面板"},
    "settle":    {"label": "中标结算"},
    "payout":    {"label": "领取分红"},
    "popup":     {"label": "结算后弹窗"},
}

# 阶段顺序：与 module.STAGE_ORDER 逐项一致（GUI 断点契约，§0.5 不可改名/重排）
STAGE_ORDER = [
    "游戏大厅", "活动页面", "鉴宝大厅(选择场次)", "匹配中", "选择鉴宝师",
    "第1回合出价", "第2回合出价", "第3回合出价", "第4回合出价", "第5回合出价",
    "中标结算", "领取分红", "结算弹窗",
]

# 全局锚点：与 module._GLOBAL_ANCHORS 一致（不变量 I-1：漏并入会导致阶段冻结）
GLOBAL_ANCHORS = ["hall_peak_appraise_card", "hall_session_cards"]

# 唯一匹配口径（G3）：
#   scales  —— 与 detector.MATCH_SCALES / _APPRAISER_MATCH_SCALES /
#              _CHECK_MATCH_SCALES / _SESSION_MATCH_SCALES / reader.MATCH_SCALES 同值
#              （五处重复定义的历史约定，从本文件起收敛为一处）
#   threshold —— 与 detector.MATCH_THRESHOLD（0.75）同值；无 ROI 级覆盖时用它
MATCH = {
    "scales": [0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30],
    "threshold": 0.75,
    "margin_default": 0.0,
}


def _detector_anchor(kind: str, page: str, label: str, order: int, **extra: Any) -> dict:
    """detector 扫描锚点的语义模板。

    `order` 同时表达两件事（v3 §4.2）：
      - 同页内的展示顺序（树视图）
      - 检测优先级：运行时按 `1000 - order` 降序扫描（替代 v2 `_ROI_STAGE.priority`）
    故下面这些数值是**刻意保留 v2 优先级语义**的换算结果，不是随意编号：

        v2 priority → order
        110 (每日最高弹窗)  → 890
        105 (彩蛋弹窗)      → 895
        100 (结算标题)      → 900
         90 (结果横幅)      → 910
         80 (智能出价按钮)  → 920
         70 (回合横幅)      → 930
         60 (选师标题/匹配中) → 940
         50 (大厅卡片/前往鉴宝/场次卡片) → 950
    """
    out = {"kind": kind, "owner": MODULE, "page": page, "label": label, "order": order}
    out.update(extra)
    return out


# ------------------------------------------------------------------
# 锚点语义
# ------------------------------------------------------------------
ANCHORS: dict[str, dict[str, Any]] = {
    # ===== stage 段：阶段判定锚点（原 _ROI_STAGE 的 11 项 + 模块独立匹配的 2 项）=====
    "stage.daily_high_banner": _detector_anchor(
        "template", "popup", "今日最高积分上涨横幅", 890,
        comment="结算后弹窗①：单日最高利润刷新时出现；点空白继续"),
    "stage.egg_reward_title": _detector_anchor(
        "template", "popup", "奖励彩蛋弹窗标题", 895,
        comment="结算后弹窗③：拍品含蛋时出现；点屏幕继续"),
    "stage.settle_title": _detector_anchor(
        "template", "payout", "结算最终竞拍价格标题", 900),
    "stage.result_banner": _detector_anchor(
        "template", "settle", "竞拍结果横幅", 910,
        arbitration={"template_thresholds": {"result_auction_win_banner": 0.60}},
        comment="中标横幅带彩条特效，匹配分偏低，单独放宽阈值防误判"),
    "stage.smart_bid_btn": _detector_anchor(
        "template", "bidding", "智能出价按钮", 920,
        comment="出价面板开的强信号；常亮但无回合号，回合号走横幅"),
    "stage.round_big_banner": _detector_anchor(
        "template", "bidding", "回合巨型横幅", 930,
        arbitration={"margin": 0.03, "round_from_template": True},
        comment="5 张互斥模板取最高分且领先次高 ≥ 0.03；回合号权威来源"),
    "stage.appraiser_title": _detector_anchor(
        "template", "appraiser", "选择鉴宝师标题", 940),
    "stage.is_matching_btn": _detector_anchor(
        "template", "matching", "匹配中按钮", 940),
    "stage.hall_peak_appraise_card": _detector_anchor(
        "template", "hall", "大厅巅峰鉴宝卡片", 950),
    "stage.goto_appraise_btn": _detector_anchor(
        "template", "activity", "活动页前往鉴宝按钮", 950),
    "stage.hall_session_cards": _detector_anchor(
        "template", "session", "鉴宝大厅场次卡片", 950),
    # 模块独立匹配（不走 detector.detect）：判定用，模板 + rect
    "stage.session_start_match_btn": _detector_anchor(
        "template", "session", "开始匹配按钮（判定）", 960,
        comment="判定按钮是否已出现（详情卡已切到目标场次）；点击用 actions 版的同名锚点"),
    "stage.appraiser_selected_check": _detector_anchor(
        "template", "appraiser", "鉴宝师已选中对勾", 961,
        comment="卡片右上角黄色√，用于判定目标鉴宝师是否已被选中"),

    # ===== appraisers 段：偏好鉴宝师（prio 小的优先）=====
    "appraisers.appraiser_p1_caroline": _detector_anchor(
        "template", "appraiser", "偏好鉴宝师① Caroline", 1),
    "appraisers.appraiser_p2_shotaro": _detector_anchor(
        "template", "appraiser", "偏好鉴宝师② Shotaro", 2),

    # ===== ocr 段：只读取值区（不参与点击）=====
    "ocr.bid_result_amount_box": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                                  "label": "出价输入框当前值(H)", "order": 10},
    "ocr.bid_player1": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                        "label": "公开报价 P1", "order": 11},
    "ocr.bid_player2": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                        "label": "公开报价 P2", "order": 12},
    "ocr.bid_player3": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                        "label": "公开报价 P3", "order": 13},
    "ocr.bid_player4": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                        "label": "公开报价 P4", "order": 14},
    "ocr.player_name1": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                         "label": "玩家名(槽1)", "order": 15},
    "ocr.player_name2": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                         "label": "玩家名(槽2)", "order": 16},
    "ocr.player_name3": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                         "label": "玩家名(槽3)", "order": 17},
    "ocr.player_name4": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                         "label": "玩家名(槽4)", "order": 18},
    "ocr.round_label_area": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                             "label": "回合小字区域", "order": 19},
    "ocr.bid_main_btn_label": {"kind": "ocr", "owner": MODULE, "page": "bidding",
                               "label": "出价主按钮文字", "order": 20},
    "ocr.settle_final_price": {"kind": "ocr", "owner": MODULE, "page": "payout",
                               "label": "最终竞拍价格", "order": 1},
    "ocr.settle_total_price": {"kind": "ocr", "owner": MODULE, "page": "payout",
                               "label": "合计价格", "order": 2},
    "ocr.settle_profit": {"kind": "ocr", "owner": MODULE, "page": "payout",
                          "label": "本场利润", "order": 3},
    "ocr.settle_my_income": {"kind": "ocr", "owner": MODULE, "page": "payout",
                             "label": "我的收入", "order": 4},
    "ocr.my_balance": {"kind": "ocr", "owner": MODULE, "page": "session",
                       "label": "我的余额", "order": 5},
    "ocr.session_daily_count": {"kind": "ocr", "owner": MODULE, "page": "session",
                                "label": "今日已参与场次", "order": 6},
    "ocr.daily_high_score": {"kind": "ocr", "owner": MODULE, "page": "popup",
                             "label": "今日最高积分", "order": 1},

    # ===== eggs 段：彩蛋（灰度模板 + HSV/NMS 领域参数透传）=====
    "eggs.egg": {"kind": "template", "owner": MODULE, "page": "popup", "label": "彩蛋",
                 "order": 2,
                 "comment": "通用蛋模板多尺度匹配 + NMS(IoU=0.5) + 中心 40% 区域 HSV 判色"},

    # ===== actions 段：点击目标 =====
    # 有模板图的 3 个保持 template（判据 §4.2：有证据）；其余按 D2 担保制归 point。
    "actions.bid_confirm_red_btn": {"kind": "template", "owner": MODULE, "page": "bidding",
                                    "label": "出价确认(红)", "order": 21},
    "actions.confirm_red_btn": {"kind": "template", "owner": MODULE, "page": "appraiser",
                                "label": "通用确认(红)", "order": 3},
    "actions.settle_collect_red_btn": {"kind": "template", "owner": MODULE, "page": "payout",
                                       "label": "领取分红(红)", "order": 5},
    # 出价主按钮：面板未开时点击打开面板 → 担保人 = 回合横幅（证明人在回合内）
    "actions.bid_main_red_btn": {"kind": "point", "owner": MODULE, "page": "bidding",
                                 "label": "出价主按钮(红)", "order": 22,
                                 "guarded_by": "round_big_banner"},
    # 拨打盘：只在面板打开（智能出价按钮可见）时存在 → 担保人 = smart_bid_btn
    "actions.bid_numpad_1": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 1", "order": 23, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_2": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 2", "order": 24, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_3": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 3", "order": 25, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_4": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 4", "order": 26, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_5": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 5", "order": 27, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_6": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 6", "order": 28, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_7": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 7", "order": 29, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_8": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 8", "order": 30, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_9": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 9", "order": 31, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_0": {"kind": "point", "owner": MODULE, "page": "bidding",
                             "label": "数字键 0", "order": 32, "guarded_by": "smart_bid_btn"},
    "actions.bid_numpad_clear": {"kind": "point", "owner": MODULE, "page": "bidding",
                                 "label": "清空键", "order": 33, "guarded_by": "smart_bid_btn"},
    # 场次标签：地图上的三个场次 badge → 担保人 = 场次卡片（证明人在鉴宝大厅）
    "actions.session_master_badge": {"kind": "point", "owner": MODULE, "page": "session",
                                     "label": "鉴宝大师场标签", "order": 7,
                                     "guarded_by": "hall_session_cards"},
    "actions.session_expert_badge": {"kind": "point", "owner": MODULE, "page": "session",
                                     "label": "鉴宝专家场标签", "order": 8,
                                     "guarded_by": "hall_session_cards"},
    "actions.session_intern_badge": {"kind": "point", "owner": MODULE, "page": "session",
                                     "label": "鉴宝实习场标签", "order": 9,
                                     "guarded_by": "hall_session_cards"},
    # 跨段同名：v2 里 stage/actions 各有一份 session_start_match_btn，
    # v3 anchors 是扁平 map 不允许重名 → actions 版改名（点击区），stage 版保留（判定）
    "actions.session_start_match_btn": {"kind": "point", "owner": MODULE, "page": "session",
                                        "label": "开始匹配按钮(点击区)", "order": 10,
                                        "rename": "session_start_match_click",
                                        "guarded_by": "hall_session_cards"},
}

# ------------------------------------------------------------------
# 阶段感知清单与 OCR 清单（原 _STAGE_PERCEPTION / _STAGE_OCR_KEYS）
# ------------------------------------------------------------------
_ROUND_ANCHORS = ["round_big_banner", "smart_bid_btn", "settle_title", "result_banner"]
_BID_OCR = [
    "bid_result_amount_box", "bid_player1", "bid_player2", "bid_player3", "bid_player4",
    "player_name1", "player_name2", "player_name3", "player_name4", "round_label_area",
]
_SETTLE_OCR = ["settle_final_price", "settle_total_price", "settle_profit", "settle_my_income"]

STAGE_DEFS: dict[str, dict[str, Any]] = {
    "游戏大厅": {"page": "hall",
                "anchors": ["hall_peak_appraise_card", "goto_appraise_btn", "hall_session_cards"]},
    "活动页面": {"page": "activity",
                "anchors": ["goto_appraise_btn", "hall_session_cards"]},
    "鉴宝大厅(选择场次)": {"page": "session",
                          "anchors": ["hall_session_cards", "is_matching_btn"]},
    "匹配中": {"page": "matching", "anchors": ["is_matching_btn", "appraiser_title"]},
    "选择鉴宝师": {"page": "appraiser",
                  "anchors": ["appraiser_title", "round_big_banner", "is_matching_btn"]},
    "中标结算": {"page": "settle",
                "anchors": ["result_banner", "settle_title", "daily_high_banner", "egg_reward_title"],
                "ocr": list(_SETTLE_OCR)},
    "领取分红": {"page": "payout",
                "anchors": ["settle_title", "result_banner", "daily_high_banner", "egg_reward_title"],
                "ocr": list(_SETTLE_OCR)},
    "结算弹窗": {"page": "popup",
                "anchors": ["daily_high_banner", "egg_reward_title", "settle_title", "result_banner"]},
}
for _i in range(1, 6):
    STAGE_DEFS[f"第{_i}回合出价"] = {
        "page": "bidding",
        "anchors": list(_ROUND_ANCHORS),
        "ocr": list(_BID_OCR),
        # 上不了纸的动态裁剪只留指针（E16）；实际逻辑在 module._active_stage_rois
        "dynamic_narrow": {
            "by": "code:_active_stage_rois",
            "note": "拨号盘打开期（bidding）收窄为仅 smart_bid_btn：横幅 ROI 与数字键盘重叠",
        },
    }

# ------------------------------------------------------------------
# transitions：让树有走向（D1）
# ------------------------------------------------------------------
# (a) 通配边：锚点 → 它**无条件声明**的阶段（等价 v2 `_ROI_STAGE` 的 stage 字段）。
#     运行时检测阶段就来自这里；`*` 表示不限当前阶段，故不参与 E17/E18 纸码互查。
_WILDCARD_EDGES = [
    ("*", "daily_high_banner", "结算弹窗"),
    ("*", "egg_reward_title", "结算弹窗"),
    ("*", "settle_title", "领取分红"),
    ("*", "result_banner", "中标结算"),
    ("*", "smart_bid_btn", "$round"),
    ("*", "round_big_banner", "$round"),
    ("*", "appraiser_title", "选择鉴宝师"),
    ("*", "is_matching_btn", "匹配中"),
    ("*", "hall_peak_appraise_card", "游戏大厅"),
    ("*", "goto_appraise_btn", "活动页面"),
    ("*", "hall_session_cards", "鉴宝大厅(选择场次)"),
]

# (b) 具体边：由 `_STAGE_PERCEPTION` 客观派生——每个阶段感知清单里"不属于本阶段
#     签名"的锚点，就是从本阶段出发的一条边。这些是 CODE_EDGES 互查的对象。
_SPECIFIC_EDGES: list[tuple[str, str, str]] = [
    ("游戏大厅", "goto_appraise_btn", "活动页面"),
    ("游戏大厅", "hall_session_cards", "鉴宝大厅(选择场次)"),
    ("活动页面", "hall_session_cards", "鉴宝大厅(选择场次)"),
    ("鉴宝大厅(选择场次)", "is_matching_btn", "匹配中"),
    ("匹配中", "appraiser_title", "选择鉴宝师"),
    ("选择鉴宝师", "round_big_banner", "$round"),
    ("选择鉴宝师", "is_matching_btn", "匹配中"),
    ("中标结算", "settle_title", "领取分红"),
    ("中标结算", "daily_high_banner", "结算弹窗"),
    ("中标结算", "egg_reward_title", "结算弹窗"),
    ("领取分红", "result_banner", "中标结算"),
    ("领取分红", "daily_high_banner", "结算弹窗"),
    ("领取分红", "egg_reward_title", "结算弹窗"),
    ("结算弹窗", "settle_title", "领取分红"),
    ("结算弹窗", "result_banner", "中标结算"),
]
for _i in range(1, 6):
    _SPECIFIC_EDGES.append((f"第{_i}回合出价", "settle_title", "领取分红"))
    _SPECIFIC_EDGES.append((f"第{_i}回合出价", "result_banner", "中标结算"))

TRANSITIONS = [
    {"stage": s, "on": on, **({"to": to} if to != "$round" else {"to": "$round",
                                                                 "when": "round_from_template"})}
    for s, on, to in (*_WILDCARD_EDGES, *_SPECIFIC_EDGES)
]


# ------------------------------------------------------------------
# 已确认缺口白名单
# ------------------------------------------------------------------
# 迁移器对"v2 里推不出来或本身有毛病"的事项一律进缺口清单（缺口非空是常态）。
# 上纸时逐条给出处置结论后，这里显式登记——**不是静默忽略**：
#   · 每条必须写明处置理由与后继动作
#   · 未登记的缺口一律阻断生成（保证新出现的缺口一定被人看见）
ACCEPTED_GAPS: dict[str, str] = {
    "冲突/同名|v2.session_start_match_btn": (  # noqa: E501
        "已处置：stage 版保留原名 `session_start_match_btn`（模板判定用），"
        "actions 版改名 `session_start_match_click`（点击用，见 _resolve_session_target）。"
        "这是 v2 靠段名消歧的遗留，v3 扁平 map 下必须分家——属于改名而非新增。"
        "后继：S1c 把 module 里 3 处点击取 key 改为新名。"
    ),
    "孤儿/模板|templates": (
        "已确认（session_master_badge.png）：F9 类 v2 数据缺陷——模板图在目录里，"
        "但 v2 的 actions.session_master_badge 只给了 rect 没给 templates。"
        "v3 保持与 v2 严格等价（不补登记），否则搬迁提交会混入数据改动，"
        "回归失败无从归因。后继：S1.5 单独提交补登记（那时有 §9.1 回归保护）。"
    ),
    "缺失/迁移边|routes": (
        "已排期：跨页面跳转链属 S3（compile_route + 生成物），本步只做检测真源上纸。"
        "注：MAA pipeline 通路（core/nav_graph.py）未接线，鉴宝导航真源为 v3 资产。"
    ),
}


def _check_gaps(gaps: list[str]) -> None:
    """缺口必须被显式登记，否则阻断生成（新缺口一定被人看见）。"""
    unknown: list[str] = []
    for g in gaps:
        kind, _, rest = g.partition("]")
        target = rest.strip().split(":")[0].strip()
        key = f"{kind.lstrip('[')}|{target}"
        if key not in ACCEPTED_GAPS:
            unknown.append(g)
    if unknown:
        print(f"[build_assets] 出现 {len(unknown)} 条未登记缺口，先给出处置结论：",
              file=sys.stderr)
        for g in unknown:
            print("  " + g, file=sys.stderr)
        raise SystemExit(2)

    print(f"[build_assets] 已确认缺口 {len(gaps)} 条（逐条登记了处置结论）：")
    for g in gaps:
        kind, _, rest = g.partition("]")
        target = rest.strip().split(":")[0].strip()
        reason = ACCEPTED_GAPS.get(f"{kind.lstrip('[')}|{target}", "")
        print(f"  · {kind.lstrip('[')} / {target} — {reason}")


def build() -> dict:
    semantic: dict[str, Any] = {
        "module": MODULE,
        "order": STAGE_ORDER,
        "global_anchors": GLOBAL_ANCHORS,
        "pages": PAGES,
        "match": MATCH,
        "anchors": ANCHORS,
        "stage_defs": STAGE_DEFS,
        "transitions": TRANSITIONS,
        "routes": {
            "hall_to_treasure": {
                "entry": True,
                "start_stage": "游戏大厅",
                "steps": [
                    {"target": "hall_peak_appraise_card", "action": "click",
                     "confirm": "goto_appraise_btn", "timeout_ms": 45000, "rate_limit_ms": 600},
                    {"target": "goto_appraise_btn", "action": "click",
                     "confirm": "hall_session_cards", "timeout_ms": 45000, "rate_limit_ms": 600},
                ],
            },
            "session_to_matching": {
                "entry": False,
                "start_stage": "鉴宝大厅(选择场次)",
                "steps": [
                    {"target": "session_start_match_click", "action": "click",
                     "confirm": "is_matching_btn", "timeout_ms": 45000, "rate_limit_ms": 600},
                ],
            },
        },
        "image_dirs": (IMAGE_DIR,),
    }
    v2 = json.loads(V2_PATH.read_text(encoding="utf-8"))
    v3, gaps = migrate_v2_to_v3(v2, semantic=semantic)

    # 缺口必须逐条登记处置结论；未登记的阻断生成
    _check_gaps(gaps)

    return v3


def _strip_draft_metadata(doc: dict) -> dict:
    """剥离草稿专用字段，得到"人写资产文件"的最终形态。

    必须在 `diff_v2_v3` **之后**调用：追溯字段 `_v2` 是跨段同名条目唯一能回配的
    依据（v2 靠段名消歧，v3 扁平后必须改名，丢了来源就配不回去）。
    """
    out = dict(doc)
    out["anchors"] = {
        name: {k: v for k, v in entry.items() if k != "_v2"}
        for name, entry in doc["anchors"].items()
    }
    out.pop("_generated_by", None)
    # eggs 的领域参数由迁移器挂到 domain 上，此处补一句说明
    if "egg" in out["anchors"]:
        out["anchors"]["egg"].setdefault("comment", "彩蛋识别（HSV/NMS 领域参数见 domain）")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="只校验现有 assets 与 v2 等价并通过校验器，不写文件")
    args = parser.parse_args()

    v2 = json.loads(V2_PATH.read_text(encoding="utf-8"))
    draft = build()          # 带 `_v2` 追溯字段的草稿

    # 1) 纯搬迁校验：rect / threshold / templates 必须逐位相同（靠追溯字段回配）
    diffs = diff_v2_v3(v2, draft)
    if diffs:
        print(f"[build_assets] 搬迁不等价，{len(diffs)} 处差异：", file=sys.stderr)
        for d in diffs:
            print("  " + d, file=sys.stderr)
        return 1

    doc = _strip_draft_metadata(draft)

    # 2) 校验器：E/W 全表
    assets = Assets.from_document(doc, module=MODULE, image_dirs=(IMAGE_DIR,))
    report = validate_assets(assets)
    print(report.text())
    if not report.ok:
        return 1

    if args.check:
        # 磁盘上的文件必须与本次生成结果一致（人改过就报，避免"改了没同步回语义表"）
        if not OUT_PATH.exists():
            print(f"[build_assets] 资产文件不存在：{OUT_PATH}", file=sys.stderr)
            return 2
        current = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if current != doc:
            print("[build_assets] --check 失败：磁盘资产与本脚本生成结果不一致。\n"
                  "  说明有人直接改了 assets.json。请二选一：\n"
                  "  (a) 把改动同步回本脚本的语义表后重新生成；\n"
                  "  (b) 确认改动有效后，更新本脚本语义表并重新生成。", file=sys.stderr)
            return 1
        print("[build_assets] --check 通过：与 v2 等价、校验器无 error、与语义表一致")
        return 0

    OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[build_assets] 已写入 {OUT_PATH.relative_to(_PROJ)}"
          f"（{len(doc['anchors'])} 锚点 / {len(doc['transitions'])} 迁移边）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
