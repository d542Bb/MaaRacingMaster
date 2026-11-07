"""speedrush 导航图真源生成器。

把 templates_roi.json 里的模板与 ROI 规格，装配成 pipeline 真源
（`plugins/speedrush/resources/pipeline/speedrush.json`）。

架构取舍（与 treasure 的差异，2026-09-16）：
    treasure 属"决策密集型"（每回合要算出价），故有 policy_loop + 大量 policy.rules
    的决策循环；speedrush 的 UI 流程是**线性**的，不需要决策引擎，因此真源只由两类
    节点构成：
      - 动作节点：识别（MaaRM_Template）+ 点击（MaaRM_Click），点完一律 next 回汇聚
      - 纯锚点节点：`_anchor_only`，零路由，页面感知规格——消费通道分两种：被模块
        代码按节点名直驱的标 `_module_driven`（真源机检承认的第二条消费通道）；
        无人消费的不许入表（孤儿真源会被 truth 当场拦下）
    出口方向统一为"回到 __boot 重判"，不各自维护下一跳（符合项目"入口锚点点击后
    一律交汇聚重判"的既有纪律）。

**真源必须可由本脚本复算**：节点参数一律由 ACTIONS / ANCHORS 与 `NODE_OVERRIDES`
装配，不存在"生成后再手工编辑"的字段。手工编辑会在下一次生成时被静默抹掉，而且
抹掉的是别的提交辛苦测出来的判据（见 `NODE_OVERRIDES` 的说明）。

用法（任意 CWD 均可，路径按本文件位置算）：
    .venv/Scripts/python.exe maaracing_master/plugins/speedrush/tools/build_truth.py

**位置**：本工具放在插件自己的 `tools/` 下，不进全局 `tools/`——它只服务本插件的
真源，且与真源、输入表同属一个可整体搬走的单元（`tools/` 是跨插件共用的工具位）。
此前它住在 `tools/experiments/`，而那是「在树即 active、退役即删除」的区域，却被
回归锁按路径加载——等于给下一次清理埋一个断测试的坑。
"""

from __future__ import annotations

import json
import os

ROI_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates_roi.json")
OUT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "resources", "pipeline", "speedrush.json"))

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
    "speedrush_store_banner": {
        # 进商店横幅锚点：主循环在锚点复查拍顺带问一次，命中即提前结束阶段 1。
        # 节拍同齿轮（零地板、零隐式等待）；超时留极短值——横幅在则首帧即命中，
        # 未命中时快速收尾不吃满复查间隔。阈值 0.7 系截图自匹配（1.0）起的保守
        # 起点，**待真机驾驶帧校准**（口径同上齿轮）。
        "rate_limit": 0,
        "pre_delay": 0,
        "post_delay": 0,
        "timeout": 50,
        "threshold": 0.7,
    },
}

# 动作节点的节拍覆盖：框架默认 rate_limit=1000ms、pre/post_delay 各 200ms
# （官方 3.1 任务流水线协议；本机 maafw 5.12.3 的 JPipelineData 同值），是
# 「每轮识别最低消耗」与进出节点的隐式等待，不是「等按钮出现」的时间。原子动作
# 节点识别命中即点一下收图，这层地板纯属固定开销：2026-11-06 实机日志里 p1→p2
# 两次点击步分别耗 4s / 2s，按默认值推算其中约 2.4s 落在这三项上。
# **timeout 没动**：官方协议里它约束的是「当前节点 next 列表识别的超时」，不是本
# 节点 recognition 的等待时间；本项目动作节点零路由、next 为空，等按钮晚出现由
# 入口识别循环与 Python 侧步级重试窗口承担，不靠这个字段。保留框架默认 20s，
# 不引入未经实机校准的新数值。
# **rate_limit 为什么是 100 而不是 0**：0 会把「按钮晚出现」场景变成框架在 20s
# 超时里全速截图找图（不失败，但白烧 CPU 与日志）。100ms 既去掉 1s 地板，又给
# 识别循环留出节流；相对游戏转场（实测 ≥1s）可忽略，不改变落地耗时或重发次数。
# pre/post_delay 保持 0：它们是进出节点的纯固定开销，没有空转风险。
# 就地赋、不进 NODE_OVERRIDES：那是按模板键生效的例外表，而这里是一整类节点的
# 共同节拍；确认探针与动作节点复用同一批模板，按模板写会把两者绑在一起。
ACTION_NODE_PACE = {"rate_limit": 100, "pre_delay": 0, "post_delay": 0}

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
    (f"{MODULE}.商店横幅锚点", "speedrush_store_banner", "drive", "驾驶页 进商店横幅（阶段1 结束信号，module 直驱提前结束本阶段）"),
]

# 声明「本锚点由模块代码按节点名直驱」——图级引用分析看不见这条消费通道，
# 真源机检（truth validate_graph）凭本声明豁免「孤儿真源」判定。
MODULE_DRIVEN: frozenset[str] = frozenset({
    f"{MODULE}.驾驶页锚点",
    f"{MODULE}.商店横幅锚点",
})

# 落地页确认探针（步级重试的"后置条件"）——(确认节点名, 复用模板, 说明)。
#
# 为什么确认节点复用**动作节点已在用的模板**却不触发第 6 条「跨锚点重复识别」：
# 校验器对节点名含 `.__confirm.` 的节点直接豁免该判定（`tools/pipeline/truth.py`
# validate_graph 第 6 条）。这是仓库为"确认节点"预留的标准扩展点——确认要认的是
# "落地页上此刻有哪个标记"，标记往往就是下一步要点的按钮本身（线性流程里第 N 步的
# 目的地页正显示着第 N+1 步的按钮），复用同图同区最省、也无需另采素材。
#
# 参数口径：确认节点是「只识别不点」的快探针（DoNothing），命中即回、未命中走**短超时**
# 快速返回，真正的"等到落地为止"由模块侧步级状态机的时间窗轮询决定——所以这些节点
# 不能像动作节点那样吃框架默认 20s 识别等待（那会把每次轮询拖成 20s）。阈值沿用同模板
# 动作节点的标定值（同图同区），此处不另拍数。`确认上阵 → 驾驶页` 的确认直接用现成的
# 驾驶页锚点（`speedrush.驾驶页锚点`），不在此列新建。
CONFIRMS: list[tuple[str, str, str]] = [
    (f"{MODULE}.__confirm.比赛页", "rank_fun_tab", "点比赛后落地比赛页：左侧「娱乐玩法」tab 在位"),
    (f"{MODULE}.__confirm.娱乐玩法页", "fun_speedrush_card", "点娱乐玩法后落地娱乐玩法页：「极速狂飙」卡片在位"),
    (f"{MODULE}.__confirm.活动页", "speedrush_career_award", "进活动页 / 结算继续回活动页：右上「生涯奖励」入口在位"),
    (f"{MODULE}.__confirm.回合开始页", "speedrush_find_opponent_btn", "开始挑战 / 回合结果继续进回合开始页：「寻找对手」在位"),
    (f"{MODULE}.__confirm.奖励弹窗", "speedrush_reward_hint", "点寻找对手后弹奖励弹窗：「点击屏幕继续」提示在位"),
    (f"{MODULE}.__confirm.策略商店页", "speedrush_store_sell_title", "关闭奖励弹窗后落地策略商店页：「售卖区」标题在位"),
    (f"{MODULE}.__confirm.放弃确认框", "speedrush_giveup_continue_btn", "点放弃本轮后弹确认框：「继续放弃」在位"),
    (f"{MODULE}.__confirm.段位分结算页", "speedrush_continue_btn", "点继续放弃后落地段位分结算页：「继续」在位"),
]

# 确认探针的快超时（毫秒）：未命中即快速判否，由模块步级机做真正的等待与重发。
CONFIRM_PROBE_TIMEOUT_MS = 400


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


def settle_from(reco: dict) -> dict:
    """点击后条件等待的判据规格 = 本节点 recognition 规格的**同源副本**。

    core 的 MaaRM_Click 拿它在 wait_after_ms 上限内轮询：「源按钮在它自己的 ROI 里
    认不出来」= 画面已切走，立即收工；始终不超过 800ms 兜底。判据只答「当前页是否
    已离开」，落地页是否到位仍由 __confirm 探针 + 步级重试窗口裁决。

    为什么不另写一张「动作节点 → 落地页标记」映射：那会和 module.py 的
    STEP_CONFIRM 形成第二份真源，且每个按钮都要人工配一次。按钮消失与页面切换在
    同一帧发生（2026-11-06 实机 nav 帧 894→895：按钮 0.814→0.250、齿轮 0.882），
    用现成的识别规格当边沿判据既零滞后也零维护。规格从 reco 复制，阈值改一处
    两边同步，不给「手工改一处忘了另一处」留缝。
    """
    spec = reco["param"]["custom_recognition_param"]
    return {
        "rect": spec["rect"],
        "templates": spec["templates"],
        "threshold": spec["threshold"],
        "colorspace": spec.get("colorspace", "rgb"),
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
    missing += [t for _, t, _ in CONFIRMS if t not in roi]
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
        reco = recog(tpl, roi[tpl]["rect"], _threshold_for(tpl))
        graph[name] = _with_overrides({
            "attach": {
                "_page": page,
                "_label": f"{label}（原子动作，零路由）",
                "_owner": MODULE,
                "_anchor_only": True,
            },
            "recognition": reco,
            "action": {"type": "Custom", "param": {
                "custom_action": "MaaRM_Click",
                "custom_action_param": {"settle": settle_from(reco)},
            }},
            **ACTION_NODE_PACE,
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

    # 确认探针节点：零路由纯识别容器，快超时 + 无节拍地板；参数**就地赋、不走
    # NODE_OVERRIDES**——因为确认节点复用的模板正是动作节点在用的模板，NODE_OVERRIDES
    # 按模板键生效会连带动作节点（动作节点要保留框架默认 20s 识别等待等"晚出现的按钮"，
    # 不能被探针的短超时污染）。节点名带 `__confirm` 使第 6 条重复识别判定豁免（见 CONFIRMS）。
    for name, tpl, label in CONFIRMS:
        graph[name] = {
            "attach": {"_page": "confirm", "_label": f"落地页确认探针：{label}",
                       "_owner": MODULE, "_anchor_only": True, "_module_driven": True},
            "recognition": recog(tpl, roi[tpl]["rect"], _threshold_for(tpl)),
            "action": {"type": "DoNothing", "param": {}},
            "rate_limit": 0, "pre_delay": 0, "post_delay": 0,
            "timeout": CONFIRM_PROBE_TIMEOUT_MS,
        }

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
    print(f"  动作节点 {len(ACTIONS)} / 纯锚点 {len(ANCHORS)} / 确认探针 {len(CONFIRMS)} / "
          f"汇聚 1 = 共 {len(graph)} 节点")
    bound = {t for _, t, _, _ in ACTIONS + ANCHORS} | {t for _, t, _ in CONFIRMS}
    print(f"  绑定模板 {len(bound)} 张"
          f"（默认阈值 {THRESHOLD}，节点级覆盖 {len(NODE_OVERRIDES)} 处）")


if __name__ == "__main__":
    main()