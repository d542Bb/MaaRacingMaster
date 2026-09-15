# -*- coding: utf-8 -*-
"""光标避让 auto_shoo 的候选选择与闸口契约（真机 2026-09-14 两型失效回归）。

事故形态一（2026-09-14 14:16 局 R5）：手柄光标停在出价按钮文字 ROI 上；下方向
避让候选被屏幕底边界 clamp 后距光标仅 ~50px，而导航「到位容差」SHOO_TOL_PX=60px
——提交即被判「已到位」原地零移动，避让 32s 空转 95 次，label OCR 永远被遮挡，
决策卡在 S1 等待人工停止。契约：避让候选必须离当前位置 > 到位容差，否则让位下一
方向；同一目标连提 SHOO_REPEAT_WARN_AFTER 次仍未移开要升级 WARNING（不能再静默
DEBUG 刷圈）。

事故形态二（同日 14:42 局第二回合）：避让任务以 lost 收尾使 miss_streak≥3
（光标盘叠在按钮白字上时 detect_cursor 连续失踪，末帧 raw 显示光标实际仍在按钮
上可见），而该计数清零只能靠导航任务里的 read_pos 成功——S1 等待态没有点击意图、
避让是唯一导航提交者，「失踪即永久跳过」形成自锁死局，40s 零避让。
契约：失踪计数高时按 SHOO_PROBE_INTERVAL_S 节流放行探测，光标重现即恢复。
"""
from __future__ import annotations

import pytest

try:
    from maaracing_master.core.clicker import Clicker
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"测试需要完整运行时依赖（maa/…）：{_ERR}"
)

_W, _H = 1280, 720
# 事故同款：bid_main_btn_label 归一化 rect（含 29px 遮挡等效半径的判定即可复现）
_GUARD = [("bid_main_btn_label", (0.36, 0.795, 0.575, 0.905))]
_RADIUS = 29.0


class _FakePad:
    def __init__(self, last_pos):
        self.last_pos = last_pos
        self.miss_streak = 0

    def is_busy(self):
        return False


def _clicker_at(px_xy):
    c = Clicker(hwnd=0, mode="gamepad")
    c._gamepad = _FakePad(px_xy)
    c.moves = []

    def _fake_submit_move(cx, cy, **kw):
        c.moves.append((round(cx, 3), round(cy, 3), kw.get("tol_px")))
        return True

    c.submit_move = _fake_submit_move
    return c


def test_shoo_skips_candidate_within_stop_tolerance():
    """光标 (601,634)（盘心压 rect 内、离底部 clamp 避让点仅 50px）：
    不得再提交会被判「已到位」的 0.95，应让位「上」方向 (≈0.74)。"""
    c = _clicker_at((601, 634))
    r = c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H))
    assert r is not None, "光标仍压识别区却没触发避让"
    assert r["point"][1] < 0.9, f"避让点仍是贴边空转位 {r['point']}"
    assert c.moves and c.moves[0][0] == round(601 / _W, 3)
    import math
    dist = math.hypot(r["point"][0] * _W - 601, r["point"][1] * _H - 634)
    assert dist > Clicker.SHOO_TOL_PX, f"避让点距当前位置仅 {dist:.0f}px ≤ 容差"


def test_shoo_takes_first_clean_far_candidate_normally():
    """无 clamp 干涉的常规场景（光标压 rect 中部）：下方向被底边界截到 0.95 但
    距当前位置 72px > 容差 60px → 照选（闸门只拦「提交即空转」的候选）。"""
    c = _clicker_at((601, 612))  # rect 中部
    r = c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H))
    assert r is not None
    assert abs(r["point"][1] - 0.95) < 0.005


def test_shoo_repeat_counter_escalates():
    """同一 (区域, 避让点) 反复提交计数累加；换目标即重置。"""
    c = _clicker_at((601, 634))
    for _ in range(Clicker.SHOO_REPEAT_WARN_AFTER):
        c._shoo_cooldown_until_ts = 0.0  # 逐帧驱动模拟：绕开 0.3s 冷却时间闸
        r = c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H))
        assert r is not None
    assert c._shoo_repeat_count >= Clicker.SHOO_REPEAT_WARN_AFTER
    c2 = _clicker_at((601, 612))
    c2.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H))
    assert c2._shoo_repeat_count == 1


def test_shoo_noop_when_cursor_clean():
    """光标在干净区：不提交任何导航。"""
    c = _clicker_at((601, 200))
    r = c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H))
    assert r is None
    assert c.moves == []


def test_shoo_probes_through_miss_streak_gate():
    """miss_streak≥3 不得永久封死避让（真机 2026-09-14 自锁死局回归）：
    失踪计数清零只发生在导航任务 read_pos 成功里，而 S1 等待态避让是唯一
    导航提交者——首帧必须放行探测、随后 1s 节流、节流到期再放行。"""
    c = _clicker_at((601, 634))
    c._gamepad.miss_streak = 9  # 事故形态：避让任务 lost 收尾
    r = c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H))
    assert r is not None, "miss_streak 高时探测被永久封死（自锁死局复现）"
    assert len(c.moves) == 1
    assert c._shoo_probe_next_ts > 0.0, "探测放行后未推进节流时刻"
    c._shoo_cooldown_until_ts = 0.0  # 绕开 0.3s 冷却，单验探测节流
    assert c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H)) is None
    assert len(c.moves) == 1, "节流窗口内重复提交"
    c._shoo_probe_next_ts = 0.0  # 模拟 1s 到期
    assert c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H)) is not None
    assert len(c.moves) == 2


def test_shoo_probe_no_submit_when_cursor_clean():
    """探测只放开闸，不放开其余判定：失踪计数高但光标在干净区 → 不提交。"""
    c = _clicker_at((601, 200))
    c._gamepad.miss_streak = 9
    r = c.auto_shoo(_GUARD, radius_px=_RADIUS, frame_size=(_W, _H))
    assert r is None
    assert c.moves == []
