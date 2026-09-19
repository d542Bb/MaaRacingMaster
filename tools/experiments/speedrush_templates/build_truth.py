"""speedrush 导航图真源生成器。

把 templates_roi.json 里的模板与 ROI 规格，装配成 NavKit v4 的 pipeline 真源
（`plugins/speedrush/resources/pipeline/speedrush.json`）。

架构取舍（与 treasure 的差异，2026-09-16）：
    treasure 属"决策密集型"（每回合要算出价），故有 policy_loop + 大量 policy.rules
    的决策循环；speedrush 的 UI 流程是**线性**的，不需要决策引擎，因此真源只由两类
    节点构成：
      - 动作节点：识别（MaaRM_Template）+ 点击（MaaRM_Click），点完一律 next 回汇聚
      - 纯锚点节点：`_anchor_only`，零路由，页面感知规格——消费通道分两种：被模块
        代码按节点名直驱的标 `_module_driven`（真源机检承认的第二条消费通道）；
        无人消费的不许入表（孤儿真源会被 check_truth 当场拦下）
    出口方向统一为"回到 __boot 重判"，不各自维护下一跳（符合项目"入口锚点点击后
    一律交汇聚重判"的既有纪律）。

**真源必须可由本脚本复算**：节点参数一律由 ACTIONS / ANCHORS 与 `NODE_OVERRIDES`
装配，不存在"生成后再手工编辑"的字段。手工编辑会在下一次生成时被静默抹掉，而且
抹掉的是别的提交辛苦测出来的判据（见 `NODE_OVERRIDES` 的说明）。

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

# 节点级参数覆盖：默认所有节点一律 THRESHOLD + 框架默认节拍，例外只在本表声明。
#
# **为什么必须有这张表**：真源是生成出来的，凡是"生成之后手工改上去"的字段，下一次
# 生成就会静默回到默认值。驾驶页锚点的阈值与节拍曾只活在手工编辑里——跑一次本脚本
# 会把 threshold 回到 0.8、把 rate_limit / timeout 回到框架默认，而那两处恰恰是实机
# 测出来的判据：阈值 0.8 会在漂移段贴着线抖动、连续两次失配被误判成"阶段结束"（随后
# 流程去找还不存在的奖励弹窗，白等 20 秒后中止）；默认节拍把驾驶主循环压到约 1Hz，
# 采集帧率不可用。两者都由实机复测确认修好，不能让生成器一笔勾销。
NODE_OVERRIDES: dict[str, dict] = {
    "speedrush_drive_gear": {
        # 纯页面判定：驾驶循环每轮都要问它一次，不需要"每轮识别最低消耗"的地板，
        # 也不要隐式等待；超时留短值，未命中时快速收尾。
        "rate_limit": 0,
        "pre_delay": 0,
        "post_delay": 0,
        "timeout": 400,
        # 实测（录制帧复算）：驾驶途中 0.93~0.99、漂移段降到 0.80、真正离开驾驶页掉到
        # 0.02 量级——两端分离度极大，阈值取中间，两侧都留足余量。
        "threshold": 0.5,
    },
}

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
    # 这一个节点服务两个页面，而两页的按钮位置差约 18px：它的 rect 因此比
    # "单帧模板外扩"口径宽（见 templates_roi.json）。按外扩口径收紧会在另一页
    # 整段失配——2026-09-16 实机就卡在这里（结算页得分 0.568、阈值 0.8）。
    (f"{MODULE}.点继续", "speedrush_continue_btn", "result", "回合结果页／段位分结算页 → 点击继续"),
    (f"{MODULE}.点继续放弃", "speedrush_giveup_continue_btn", "giveup", "放弃确认框 → 点击继续放弃"),
]

# 页面感知锚点（name, template, page, label）。**只收有真实消费者的条目**：
# 消费通道 = 模块代码按节点名直驱（post_task/等待，须进 MODULE_DRIVEN 声明）或图内
# 引用。曾有 6 个"先造出来等以后用"的锚点（商店页/回合结果页/极速狂飙tab/段位分
# 结算页/活动页/生涯奖励入口）全仓零消费，2026-09-19 分发机检收编时按孤儿真源清除
# ——要用时从本文件历史找回一行重新生成即可（步骤16 落地时同理）。
ANCHORS: list[tuple[str, str, str, str]] = [
    (f"{MODULE}.驾驶页锚点", "speedrush_drive_gear", "drive", "驾驶页 HUD 齿轮图标（页面感知，module 直驱判定是否在对局中）"),
]

# 声明「本锚点由模块代码按节点名直驱」——图级引用分析看不见这条消费通道，
# 真源机检（check_truth validate_graph）凭本声明豁免「孤儿真源」判定。
MODULE_DRIVEN: frozenset[str] = frozenset({f"{MODULE}.驾驶页锚点"})


def recog(tpl: str, rect: list[float], threshold: float = THRESHOLD) -> dict:
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
                "threshold": threshold,
            },
        },
    }


def _overrides(tpl: str) -> dict:
    return NODE_OVERRIDES.get(tpl) or {}


def _threshold_for(tpl: str) -> float:
    """该模板的识别阈值：覆盖表优先，缺省走模块统一的 THRESHOLD。"""
    return float(_overrides(tpl).get("threshold", THRESHOLD))


def _with_overrides(node: dict, tpl: str) -> dict:
    """把 NODE_OVERRIDES 里该模板的非识别类参数并到节点上。

    `threshold` 归 recognition 内部，由 recog() 直接取，故这里排除——同一件事放两个
    地方，改的人就不知道该改哪个。
    """
    node.update({k: v for k, v in _overrides(tpl).items() if k != "threshold"})
    return node


def main() -> None:
    with open(ROI_JSON, encoding="utf-8") as fh:
        roi = json.load(fh)

    missing = [t for _, t, _, _ in ACTIONS + ANCHORS if t not in roi]
    if missing:
        raise SystemExit(f"以下模板缺少 ROI 数据：{missing}")

    graph: dict[str, dict] = {}

    # 动作节点：识别 + 点击，**原子叶子**（零路由）。
    # 出口为何不写在节点上：NavGraph.run(entry, reached) 的语义是"阻塞到图跑完，
    # 再用 reached 事后校验"——节点若自带 next，Python 调 run(单节点) 会被框架
    # 顺着边继续跑下去而失控。把出口交给调用方（Python 显式走下一段，或 run(__boot)
    # 让框架按当前页面择一），单步驱动才成立。
    # 标 _anchor_only 是因为本批节点"不参与路由"（零路由 + 被 __boot 引用），
    # 借此豁免机检的「无出口节点」告警——告警本身不 fail，但十个节点一起报会淹没
    # CI 输出。语义注释见 _label。
    for name, tpl, page, label in ACTIONS:
        graph[name] = _with_overrides({
            "attach": {
                "_page": page,
                "_label": f"{label}（原子动作，零路由）",
                "_owner": MODULE,
                "_anchor_only": True,
            },
            "recognition": recog(tpl, roi[tpl]["rect"], _threshold_for(tpl)),
            "action": {"type": "Custom", "param": {"custom_action": "MaaRM_Click"}},
        }, tpl)

    # 纯锚点节点：零路由，页面感知规格；模块直驱的按声明补 _module_driven
    for name, tpl, page, label in ANCHORS:
        attach = {"_page": page, "_label": label, "_owner": MODULE, "_anchor_only": True}
        if name in MODULE_DRIVEN:
            attach["_module_driven"] = True
        graph[name] = _with_overrides({
            "attach": attach,
            "recognition": recog(tpl, roi[tpl]["rect"], _threshold_for(tpl)),
            "action": {"type": "DoNothing", "param": {}},
        }, tpl)

    # 汇聚节点：DirectHit 恒命中 → next 全动作表。
    # 出口语义：每个动作节点点完一律回此处重判，由"页面上此刻存在哪个按钮"决定下一步。
    # 需要区分"同一页面在不同时机点击不同按钮"时（回合开始页：首轮点寻找对手、次轮点
    # 放弃本轮），由 Python 侧用 NavGraph.run(entry, reached) 显式驱动，而非复制节点——
    # 复制节点会让同一模板被两个节点认领，触发机检的跨锚点重复识别告警。
    graph[BOOT] = {
        "attach": {"_boot": True, "_entry": True, "_label": "起跑汇聚（按当前页面存在的按钮择一）"},
        "recognition": {"type": "DirectHit", "param": {}},
        "action": {"type": "DoNothing", "param": {}},
        "next": [n for n, _, _, _ in ACTIONS],
        "timeout": -1,
        "rate_limit": 600,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # newline="\n"：真源在仓库里按 LF 存（.gitattributes 的 eol=lf）。Windows 上文本
    # 模式默认写 CRLF，会让整文件在 diff 里翻一遍、盖掉真正的改动。
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(graph, fh, ensure_ascii=False, indent=4)

    print(f"已写出 {OUT}")
    print(f"  动作节点 {len(ACTIONS)} / 纯锚点 {len(ANCHORS)} / 汇聚 1 = 共 {len(graph)} 节点")
    print(f"  绑定模板 {len(ACTIONS) + len(ANCHORS)} 张"
          f"（默认阈值 {THRESHOLD}，节点级覆盖 {len(NODE_OVERRIDES)} 处）")


if __name__ == "__main__":
    main()