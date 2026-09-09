"""run17：八炸修复离线形态检查（真源重生成后的静态验证）。

验证点（真机八炸：场次页被「活动页面 dwell」共享信号截胡 → timeout=-1 永久静默）：
1. dwell 信号专属化：游戏大厅只认巅峰卡、活动页只认前往按钮、鉴宝厅只认场次卡
2. boot 大 Or 去重（共享模板只出现一次）
3. 纯导航 dwell 无 timeout=-1 且 on_error=[boot]
4. 决策阶段 dwell 保留 timeout=-1
5. route 链节点 on_error=[boot]
6. 游戏大厅 dwell.next 链头前置
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
glob = json.loads((ROOT / "maaracing_assistant/core/resources/nav/global.json").read_text(encoding="utf-8"))
trea = json.loads((ROOT / "maaracing_assistant/plugins/treasure/resources/nav/treasure.json").read_text(encoding="utf-8"))
full = {**glob, **trea}

fails: list[str] = []


def templates_of(node) -> set[str]:
    r = node.get("recognition")
    out: set[str] = set()
    if isinstance(r, dict) and r.get("type") == "Or":
        for sub in r["param"]["any_of"]:
            out |= set(sub.get("custom_recognition_param", {}).get("templates") or [])
    elif r == "Custom":
        # 单信号 dwell：识别参数在节点顶层
        out |= set(node.get("custom_recognition_param", {}).get("templates") or [])
    return out


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


# 1. 信号专属化
hall = full["global.游戏大厅.dwell"]
act = full["global.活动页面.dwell"]
sess = full["treasure.鉴宝大厅(选择场次).dwell"]
check("游戏大厅识别=巅峰卡专属", templates_of(hall) == {"hall_peak_appraise_card.png"},
      str(sorted(templates_of(hall))))
check("活动页面识别=前往按钮专属", templates_of(act) == {"act_goto_appraise_btn.png"},
      str(sorted(templates_of(act))))
check("鉴宝厅识别=场次卡专属", templates_of(sess) == {"hall_session_cards.png"},
      str(sorted(templates_of(sess))))

# 2. boot 去重
boot = full["treasure.__boot.dwell"]
boot_subs = boot["recognition"]["param"]["any_of"]
boot_tpls = [t for s in boot_subs for t in s.get("custom_recognition_param", {}).get("templates", [])]
check("boot 子项无重复模板", len(boot_tpls) == len(set(boot_tpls)),
      f"{len(boot_tpls)} 项（去重前 46）")
check("boot 覆盖全部专属信号",
      {"hall_peak_appraise_card.png", "act_goto_appraise_btn.png", "hall_session_cards.png",
       "is_matching_btn.png", "select_appraiser_title.png", "round1_banner.png",
       "bid_smart_btn.png", "result_auction_win_banner.png", "settle_final_price_title.png",
       "daily_high_banner.png", "egg_reward_title.png"} <= set(boot_tpls),
      str(len(set(boot_tpls))))

# 3/4. timeout 与 on_error 兜底
for name in ("global.游戏大厅.dwell", "global.活动页面.dwell"):
    n = full[name]
    check(f"{name} 无 timeout=-1", n.get("timeout") != -1, str(n.get("timeout")))
    check(f"{name} on_error=[boot]", n.get("on_error") == ["treasure.__boot.dwell"],
          str(n.get("on_error")))
for name in ("treasure.鉴宝大厅(选择场次).dwell", "treasure.第1回合出价.dwell"):
    check(f"{name} 保留 timeout=-1", full[name].get("timeout") == -1,
          str(full[name].get("timeout")))
    check(f"{name} 无 on_error", "on_error" not in full[name], str(full[name].get("on_error")))

# 5. route 链节点 on_error
chain_nodes = [k for k in full if ".r" in k or ".__confirm." in k]
for name in chain_nodes:
    check(f"链节点 {name} on_error=[boot]", full[name].get("on_error") == ["treasure.__boot.dwell"],
          str(full[name].get("on_error")))

# 6. 链头前置
hall_next = [x if isinstance(x, str) else x["name"] for x in hall["next"]]
check("游戏大厅 next 链头首位", hall_next[0] == "global.hall_peak_appraise_card.rhall_to_treasure.0",
      str(hall_next[:3]))

# 7. 决策周期节拍（P2b 延迟实测定案）：policy_loop pre/post_delay=0（去框架默认
#    400ms 空转）、决策阶段 dwell rate_limit=300（对齐 policy 300ms）
pol = full["treasure.policy_loop"]
check("policy_loop pre_delay=0", pol.get("pre_delay") == 0, str(pol.get("pre_delay")))
check("policy_loop post_delay=0", pol.get("post_delay") == 0, str(pol.get("post_delay")))
dyn = {"treasure.鉴宝大厅(选择场次).dwell", "treasure.第1回合出价.dwell",
       "treasure.选择鉴宝师.dwell"}
for n in dyn:
    check(f"决策 dwell {n} rate_limit=300", full[n].get("rate_limit") == 300,
          str(full[n].get("rate_limit")))
nav = {"treasure.匹配中.dwell", "treasure.中标结算.dwell"}
for n in nav:
    check(f"非决策 dwell {n} 保持 600", full[n].get("rate_limit") == 600,
          str(full[n].get("rate_limit")))

print()
if fails:
    print(f"共 {len(fails)} 项失败")
    sys.exit(1)
print("全部通过")
