"""speedrush 导航图真源生成器。

把 templates_roi.json 里的模板与 ROI 规格，装配成 NavKit v4 的 pipeline 真源
（`plugins/speedrush/resources/pipeline/speedrush.json`）。

架构取舍（与 treasure 的差异，2026-09-16）：
    treasure 属"决策密集型"（每回合要算出价），故有 policy_loop + 大量 policy.rules
    的决策循环；speedrush 的 UI 流程是**线性**的，不需要决策引擎，因此真源只由两类
    节点构成：
      - 动作节点：识别（MaaRM_Template）+ 点击（MaaRM_Click），点完一律 next 回汇聚
      - 纯锚点节点：`_anchor_only`，零路由，仅作页面感知规格，被汇聚节点的 Or 引用
    出口方向统一为"回到 __boot 重判"，不各自维护下一跳（符合项目"入口锚点点击后
    一律交汇聚重判"的既有纪律）。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_templates/build_truth.py
"""

from __future__ import annotations

import json
import os

ROI_JSON = "tools/experiments/speedrush_templates/templates_roi.json"
OUT = "maaracing_master/plugins/speedrush/resources/pipeline/speedrush.json"

MODULE = "speedrush"
THRESHOLD = 0.8
BOOT = f"{MODULE}.__boot.dwell"

# (节点名, 模板名, page, 标签)
ACTIONS: list[tuple[str, str, str, str]] = [
    (f"{MODULE}.点比赛", "hall_race_btn", "hall", "游戏大厅 → 点击比赛按钮"),
    (f"{MODULE}.点娱乐玩法", "rank_fun_tab", "rank", "比赛页 → 点击左侧 tab 娱乐玩法"),
    (f"{MODULE}.点极速狂飙卡片", "fun_speedrush_card", "fun", "娱乐玩法页 → 点击极速狂飙卡片"),
    (f"{MODULE}.点开始挑战", "speedrush_start_btn", "speedrush", "活动页 → 点击开始挑战"),
    (f"{MODULE}.点寻找对手", "speedrush_find_opponent_btn", "round", "回合开始页 → 点击寻找对手"),
    (f"{MODULE}.点放弃本轮", "speedrush_giveup_btn", "round", "回合开始页 → 点击放弃本轮"),
    (f"{MODULE}.关闭奖励弹窗", "speedrush_reward_hint", "reward", "奖励弹窗 → 点击任意处关闭"),
    (f"{MODULE}.点确认上阵", "speedrush_store_confirm_btn", "store", "策略商店 → 点击确认上阵"),
    (f"{MODULE}.点继续", "speedrush_continue_btn", "result", "回合结果页／段位分结算页 → 点击继续"),
    (f"{MODULE}.点继续放弃", "speedrush_giveup_continue_btn", "giveup", "放弃确认框 → 点击继续放弃"),
]

ANCHORS: list[tuple[str, str, str, str]] = [
    (f"{MODULE}.活动页锚点", "speedrush_title", "speedrush", "活动页主标题（页面感知）"),
    (f"{MODULE}.商店页锚点", "speedrush_store_sell_title", "store", "策略商店售卖区标题（页面感知）"),
    (f"{MODULE}.驾驶页锚点", "speedrush_drive_gear", "drive", "驾驶页 HUD 齿轮图标（页面感知）"),
    (f"{MODULE}.回合结果页锚点", "speedrush_round_end_detail_btn", "result", "回合结果页回合详情按钮（页面感知）"),
    (f"{MODULE}.段位分结算页锚点", "speedrush_rank_score_label", "settle", "段位分结算页段位分标签（页面感知）"),
    (f"{MODULE}.极速狂飙tab锚点", "speedrush_badge", "speedrush", "左侧 tab 极速狂飙条目（页面感知）"),
    (f"{MODULE}.生涯奖励入口", "speedrush_career_award", "speedrush", "活动页生涯奖励入口（步骤16 用，当前仅感知）"),
]


def recog(tpl: str, rect: list[float]) -> dict:
    """MaaRM_Template 识别规格（v2 归一形）。"""
    return {
        "type": "Custom",
        "param": {
            "custom_recognition": "MaaRM_Template",
            "custom_recognition_param": {
                "mode": "template",
                "colorspace": "rgb",
                "rect": rect,
                "templates": [f"{tpl}.png"],
                "threshold": THRESHOLD,
            },
        },
    }


def main() -> None:
    with open(ROI_JSON, encoding="utf-8") as fh:
        roi = json.load(fh)

    missing = [t for _, t, _, _ in ACTIONS + ANCHORS if t not in roi]
    if missing:
        raise SystemExit(f"以下模板缺少 ROI 数据：{missing}")

    graph: dict[str, dict] = {}

    # 动作节点：识别 + 点击，出口统一回汇聚重判
    for name, tpl, page, label in ACTIONS:
        graph[name] = {
            "attach": {"_page": page, "_label": label, "_owner": MODULE},
            "recognition": recog(tpl, roi[tpl]["rect"]),
            "action": {"type": "Custom", "param": {"custom_action": "MaaRM_Click"}},
            "next": [BOOT],
            "on_error": [BOOT],
            "rate_limit": 1000,
        }

    # 纯锚点节点：零路由，仅作页面感知规格
    for name, tpl, page, label in ANCHORS:
        graph[name] = {
            "attach": {"_page": page, "_label": label, "_owner": MODULE, "_anchor_only": True},
            "recognition": recog(tpl, roi[tpl]["rect"]),
            "action": {"type": "DoNothing", "param": {}},
        }

    # 汇聚节点：Or 全锚点信号并集 → next 全动作表（点击后一律回此处重判）
    graph[BOOT] = {
        "attach": {"_boot": True, "_entry": True, "_label": "起跑汇聚（任意页面自适应）"},
        "recognition": {
            "type": "Or",
            "param": {"any_of": [n for n, _, _, _ in ACTIONS + ANCHORS]},
        },
        "action": {"type": "DoNothing", "param": {}},
        "next": [n for n, _, _, _ in ACTIONS],
        "timeout": -1,
        "rate_limit": 600,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(graph, fh, ensure_ascii=False, indent=4)

    print(f"已写出 {OUT}")
    print(f"  动作节点 {len(ACTIONS)} / 纯锚点 {len(ANCHORS)} / 汇聚 1 = 共 {len(graph)} 节点")
    print(f"  绑定模板 {len(ACTIONS) + len(ANCHORS)} 张（阈值 {THRESHOLD}）")


if __name__ == "__main__":
    main()