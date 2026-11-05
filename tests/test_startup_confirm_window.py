# -*- coding: utf-8 -*-
"""确认窗口的行为承诺：窗口内任何一段出现弹窗都要截胡，清掉后重新计数。

这条不能只靠静态断言（next 前缀一致只证明配置没写漏）。这里用一个极小的
解释器复刻协议的两条执行语义——**next 顺序检测、命中即中断**、
**动作失败/无候选超时走 on_error**——然后按帧序列推进，验证四件事：

1. 干净大厅：boot → confirm.1 → confirm.2 → confirm.3 → done；
2. 弹窗出现时，即使大厅锚点仍命中，也先走弹窗（排序即优先级）；
3. 弹窗在**任意**一段确认窗口内出现都会离开确认链、回到汇聚节点；
4. 清掉后从 confirm.1 重新累计，而不是接着 confirm.2/confirm.3。

解释器只认本文件声明的映射，不假装等同于 MaaFW 内部实现；结构一致性由
`test_startup_truth.py` 单独看护，两者互补。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

PIPELINE = (Path(__file__).resolve().parents[1]
            / "maaracing_master" / "core" / "resources" / "startup" / "pipeline.json")

BOOT = "startup.boot"
DONE = "startup.done"
CONFIRMS = ["startup.hall.confirm.1", "startup.hall.confirm.2", "startup.hall.confirm.3"]
POPUP_SCREEN = {
    "startup.popup.claim_daily": "popup_daily",
    "startup.popup.claim_signin": "popup_signin",
    "startup.popup.continue": "popup_continue",
    "startup.popup.close_reward": "popup_close_reward",
    "startup.popup.close_marketing": "popup_close_marketing",
}


@pytest.fixture(scope="module")
def graph() -> dict:
    assert PIPELINE.is_file(), "启动链真源不存在"
    return json.loads(PIPELINE.read_text(encoding="utf-8"))


def _matches(node: str, screen: frozenset[str]) -> bool:
    """节点识别在当前画面上是否命中（只实现本图用到的几类）。"""
    if node in (DONE, BOOT):
        return True
    if node in CONFIRMS or node in ("startup.anchor.hall_race",
                                    "startup.anchor.hall_manager"):
        return "hall" in screen
    if node == "startup.login.engine_start":
        return "login" in screen
    if node in POPUP_SCREEN:
        return POPUP_SCREEN[node] in screen
    return False


def _walk(graph: dict, screens: list[frozenset[str]], limit: int = 12) -> list[str]:
    """按每跳的画面序列推进图，返回经过的节点序列（最后一屏会一直沿用）。"""
    node, path = BOOT, [BOOT]
    for hop in range(limit):
        screen = screens[min(hop, len(screens) - 1)]
        nxt = graph[node].get("next") or []
        hit = next((c for c in nxt if _matches(c, screen)), None)
        if hit is None:
            break
        path.append(hit)
        node = hit
        if node == DONE:
            break
    return path


HALL = frozenset({"hall"})


def test_clean_hall_reaches_done_through_three_confirms(graph):
    path = _walk(graph, [HALL])
    assert path == [BOOT, *CONFIRMS, DONE]


@pytest.mark.parametrize("popup", sorted(POPUP_SCREEN))
def test_popup_wins_over_hall_when_both_visible(graph, popup):
    """弹窗叠在大厅上时大厅锚点仍可能命中——排序必须让弹窗先走。"""
    screen = frozenset({"hall", POPUP_SCREEN[popup]})
    path = _walk(graph, [screen])
    assert path[1] == popup, f"{popup} 被大厅确认截胡了: {path}"


@pytest.mark.parametrize("position", [0, 1, 2])
def test_popup_inside_any_confirm_window_restarts_count(graph, position):
    """在第 N 段确认窗口注入弹窗：必须离开确认链，清理后从第一段重新累计。"""
    popup = "startup.popup.claim_signin"
    popup_screen = frozenset({"hall", POPUP_SCREEN[popup]})
    # 前 position+1 跳都干净（boot→c1→…），随后弹窗出现并停留一跳，再恢复干净
    screens = [HALL] * (position + 1) + [popup_screen] * 2 + [HALL] * 6
    path = _walk(graph, screens, limit=12)

    assert path[position + 2] == popup, f"第 {position + 1} 段没有让弹窗截胡: {path}"
    assert path[position + 3] == BOOT, f"弹窗节点应回汇聚节点: {path}"
    # 清理后的第一个确认必须是第 1 段，不能续在 confirm.2/3 上
    assert path[position + 4] == CONFIRMS[0], f"计数没有重置: {path}"


def test_confirm_chain_ends_at_done_only_after_third(graph):
    path = _walk(graph, [HALL])
    assert path[-1] == DONE
    assert path.count(CONFIRMS[2]) == 1
