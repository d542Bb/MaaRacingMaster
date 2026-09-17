# -*- coding: utf-8 -*-
"""任务槽看门狗（P0-1）：卡槽必须留痕且能自行恢复，健康导航不得被打断。

真机背景（2026-09-16 会话 20260916_215905）：帧 901→1741 之间 840 帧约 85s
`trace.jsonl` 里没有任何 `intent_submitted` / `click_result`，同窗口「光标压住
出价按钮文字 ROI」每 10 帧一条共 76 条，而「避让导航到 …」零条——单槽被一个
导航占住，点击与避让的第一道闸都是 `is_busy()`，两道闸全静默 return，
决策段看起来「什么都没发生」。

判据分层（本文件逐条锁）：
  1. 进度停滞 → 判定卡住 → WARNING + cancel（可恢复）
  2. 占用过久但**仍在推进** → 只告警、不打断（P 趋近上限 200 步 + 微调 25 步，
     重负载下单次导航合法耗时可达数十秒，硬砍会误伤正常导航）
  3. 导航线程已退出但任务仍在 → 槽永久占用（无人再写 `_result`）→ ERROR +
     回收槽 + 尽力重启线程
  4. 正常导航跑完 → 槽正常释放、全程零告警（原本正常场景）
"""
from __future__ import annotations

import threading
import time

import pytest

try:
    from maaracing_master.core import clicker as clicker_mod
    from maaracing_master.core import gamepad_cursor as gpc
    from maaracing_master.core.clicker import Clicker
    from maaracing_master.core.gamepad_cursor import GamepadClicker
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"测试需要完整运行时依赖（maa/…）：{_ERR}"
)


class _RecordingLogger:
    """记录 logger.log 调用——「留痕」本身就是本缺陷的核心要求，必须可断言。"""

    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO", channel=None):  # noqa: ANN001 —— 与真 logger 同形
        self.lines.append((level, str(msg)))

    def messages(self, level: str | None = None) -> list[str]:
        return [m for lv, m in self.lines if level is None or lv == level]


class _FakePad:
    """vgamepad 手柄桩：导航闭环与避让判定只用到这几个方法。"""

    def __init__(self):
        self.miss_streak = 0

    def is_busy(self):
        return False

    def left_joystick(self, x_value=0, y_value=0):
        pass

    def press_button(self, *_a, **_k):
        pass

    def release_button(self, *_a, **_k):
        pass


@pytest.fixture
def recorder(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(clicker_mod, "logger", rec)
    monkeypatch.setattr(gpc, "logger", rec)
    return rec


@pytest.fixture
def nav():
    """真身导航器（导航线程空转）；用后收尾，避免线程泄漏到其他用例。"""
    clicker = GamepadClicker(capture=lambda: None, gpad=_FakePad())
    yield clicker
    clicker.shutdown()
    clicker._nav_thread.join(timeout=2.0)


def _occupy(nav: GamepadClicker, *, occupied_s: float, label: str = "bid_main_red_btn",
            abort_event: threading.Event | None = None) -> dict:
    """占住槽位（看门狗判据的输入面）：直接写内部状态，避免依赖真导航耗时。"""
    task = {
        "type": "click", "target": (100, 100), "intent": False, "tol_px": 20.0,
        "abort_event": abort_event or threading.Event(),
        "submitted_ts": time.monotonic() - occupied_s,
        "label": label, "budget_s": None,
    }
    with nav._nav_lock:
        nav._task = task
    return task


def _age_progress(nav: GamepadClicker, seconds: float) -> None:
    """把「最近一次进度」改老（模拟导航卡住：不再发布新进度）。"""
    with nav._nav_lock:
        nav._progress = dict(nav._progress, ts=time.monotonic() - seconds)


# ------------------------------------------------------------------
#  判据 1：进度停滞 → WARNING + cancel（能恢复）
# ------------------------------------------------------------------


def test_progress_stall_warns_and_cancels(nav, recorder):
    task = _occupy(nav, occupied_s=8.0, label="bid_confirm_red_btn")
    _age_progress(nav, gpc.SLOT_PROGRESS_STALL_S + 5.0)

    diag = nav.watchdog()

    assert diag is not None and diag["reason"] == "stalled"
    assert task["abort_event"].is_set(), "判定卡住必须取消该任务（否则槽不会释放）"
    warns = recorder.messages("WARNING")
    assert warns, "卡槽判定必须留痕——静默正是本次缺陷的核心"
    assert "停滞" in warns[0]
    # 告警必须点名是哪个 key/目标被卡住，否则运维读不懂
    assert "bid_confirm_red_btn" in warns[0] and "(100, 100)" in warns[0]


def test_stall_warning_is_throttled(nav, recorder):
    _occupy(nav, occupied_s=8.0)
    _age_progress(nav, gpc.SLOT_PROGRESS_STALL_S + 5.0)
    for _ in range(5):
        nav.watchdog()
    assert len(recorder.messages("WARNING")) == 1, \
        "同类告警在 SLOT_WARN_REPEAT_S 内只记一条（不刷屏）"


def test_healthy_slot_is_not_touched(nav, recorder):
    """槽为空 → 看门狗什么都不做（不得误报、不得节流状态残留）。"""
    assert nav.watchdog() is None
    assert recorder.messages() == []


# ------------------------------------------------------------------
#  判据 2：占用过久但在推进 → 只告警、不打断（不得误伤慢导航）
# ------------------------------------------------------------------


def test_long_but_progressing_nav_is_reported_not_cancelled(nav, recorder):
    """占用 40s 但进度刚刷新过 → 告警可见，但**不得**取消。

    对仍在推进的导航挥刀会把正常点击砍掉（点击失败不更新指纹 → 反复重试）。
    """
    task = _occupy(nav, occupied_s=gpc.SLOT_OCCUPIED_WARN_S + 10.0)
    _age_progress(nav, 0.1)

    diag = nav.watchdog()

    assert diag is not None and diag["reason"] == "occupied_warn"
    assert not task["abort_event"].is_set(), "在推进的导航不得被打断"
    warns = recorder.messages("WARNING")
    assert warns and "已占用" in warns[0] and "仍在推进" in warns[0]


def test_occupied_hard_cap_cancels(nav, recorder):
    """超过硬上限的导航空耗 → 取消（提交方下帧自动重试，不丢状态）。"""
    task = _occupy(nav, occupied_s=gpc.SLOT_MAX_OCCUPIED_S + 5.0)
    _age_progress(nav, 0.1)

    diag = nav.watchdog()

    assert diag is not None and diag["reason"] == "occupied_too_long"
    assert task["abort_event"].is_set()
    assert any("硬上限" in m for m in recorder.messages("WARNING"))


# ------------------------------------------------------------------
#  判据 3：导航线程已退出但任务仍在 → 槽永久占用 → 回收 + 重启
# ------------------------------------------------------------------


def test_dead_nav_thread_is_recovered_and_logged(nav, recorder):
    """线程退出后没人再写 `_result` ⇒ 主链路永远看到 busy。

    这是唯一必须旁路清 `_task` 的场合：保护「结果槽不覆盖」契约的当事人
    （导航线程）已经不在了，不回收就是永久卡死。
    """
    _occupy(nav, occupied_s=3.0, label="settle_collect_red_btn")
    nav.shutdown()                      # 让 _nav_loop 退出
    nav._nav_thread.join(timeout=2.0)
    assert not nav._nav_thread.is_alive()
    nav._shutdown.clear()               # 允许重启（模拟「意外退出」而非收尾）
    nav._thread_restart_ts = 0.0

    diag = nav.watchdog()

    assert diag is not None and diag["reason"] == "nav_thread_dead"
    assert nav.slot_diag()["task"] is None, "槽必须被回收，否则永久 busy"
    assert "导航线程已退出" in recorder.messages("ERROR")[0]
    assert nav._nav_thread.is_alive(), "回收后必须尽力重启线程，否则后续导航无人执行"


def test_thread_never_dies_on_result_overwrite_violation(nav, recorder, monkeypatch):
    """契约 1 违例不得杀线程：线程死 = 槽永久占用，比违例本身更糟。

    旧实现用裸 assert 保护「结果未消费前禁止覆盖」，断言抛出即线程死亡，
    且 `_task` 未被清理 → 槽永久占用（P0-1 硬化前的隐患之一）。
    """
    monkeypatch.setattr(nav, "_approach_sync",
                        lambda task: {"type": task.get("type"), "ok": False})
    old_result = {"type": "click", "ok": True}
    with nav._nav_lock:
        nav._result = old_result          # 未消费的旧结果
    # 绕过 submit 的前置条件，制造「旧结果未消费 + 新任务在飞」
    _occupy(nav, occupied_s=0.0, label="bid_main_red_btn")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with nav._nav_lock:
            if nav._task is None:
                break
        time.sleep(0.01)

    assert nav._nav_thread.is_alive(), "违例不得杀线程（线程死了槽会永久占用）"
    assert any("协议违例" in m for m in recorder.messages("ERROR")), "违例必须留痕"
    assert nav.consume_result() == old_result, "旧结果仍在，交主链路消化"
    assert nav.is_busy() is False, "违例后槽必须可恢复（不永久占用）"


# ------------------------------------------------------------------
#  判据 4（原本正常场景）：正常导航跑完 → 槽正常释放、零告警
# ------------------------------------------------------------------


def test_healthy_navigation_completes_and_releases_slot(nav, recorder, monkeypatch):
    """正常导航：跑完 → 结果可取 → 取走后槽空 → 下一任务可提交，全程零告警。"""
    def _fake_approach(task):
        nav._publish_progress(stage="done", pos=(100, 100), target=task["target"], ok=True)
        return {"type": task.get("type"), "target": list(task["target"]),
                "ok": True, "err": 2.0, "total_s": 0.4,
                "confirmed": True, "confirm_ms": 152}

    monkeypatch.setattr(nav, "_approach_sync", _fake_approach)

    assert nav.submit((100, 100), label="bid_main_red_btn") is True
    deadline = time.monotonic() + 5.0
    res = None
    while res is None and time.monotonic() < deadline:
        assert nav.watchdog() is None, "正常导航期间看门狗不得告警/取消"
        res = nav.consume_result()
        time.sleep(0.01)

    assert res is not None and res["ok"] is True
    assert res["err"] == 2.0 and res["confirm_ms"] == 152, "P1-9 取证字段须随结果发布"
    assert nav.is_busy() is False, "结果取走后槽必须为空"
    assert nav.watchdog() is None
    assert nav.submit((200, 200)) is True, "槽释放后必须能提交下一任务"
    assert recorder.messages("WARNING") == [], "正常路径不得产生任何告警"


# ------------------------------------------------------------------
#  Clicker 层：分派、real 模式不参与、避让短预算
# ------------------------------------------------------------------


def test_clicker_dispatch_and_real_mode_opt_out(nav):
    c = Clicker(hwnd=0, mode="gamepad")
    c._gamepad = nav
    _occupy(nav, occupied_s=8.0)
    _age_progress(nav, gpc.SLOT_PROGRESS_STALL_S + 5.0)
    assert c.watchdog_tick() is not None, "gamepad 模式应委派给导航器看门狗"
    assert c.slot_diag()["task"] is not None

    real = Clicker(hwnd=0, mode="real")
    assert real.watchdog_tick() is None, "real 模式的槽是同步结果槽，不存在「导航卡住」"
    assert real.slot_diag() is None


def test_auto_shoo_submits_with_short_budget_and_label(monkeypatch):
    """避让提交必须带短预算与 label：它不该与点击共用长预算（P1-5）。"""
    c = Clicker(hwnd=0, mode="gamepad")
    c._gamepad = _FakePad()
    c._gamepad.last_pos = (601, 634)          # 压在 guard 区内
    c._gamepad.miss_streak = 0
    submitted: list[dict] = []

    def _fake_submit_move(cx, cy, **kw):
        submitted.append({"cx": cx, "cy": cy, **kw})
        return True

    monkeypatch.setattr(c, "submit_move", _fake_submit_move)
    guard = [("bid_main_btn_label", (0.36, 0.795, 0.575, 0.905))]

    r = c.auto_shoo(guard, radius_px=29.0, frame_size=(1280, 720))

    assert r is not None, "光标压住识别区却没触发避让"
    assert submitted, "避让未提交导航"
    assert submitted[0]["budget_s"] == c.SHOO_BUDGET_S
    assert submitted[0]["budget_s"] <= 2.0, "避让预算须显著短于点击（只需挪出识别区）"
    assert submitted[0]["label"] == f"shoo:{r['key']}"


def test_gamepad_submit_propagates_budget_and_label(nav):
    """预算/标签经 Clicker → GamepadClicker 落到任务字典（计时起点=提交时刻）。"""
    assert nav.submit((10, 10), task_type="move", intent=True,
                      label="shoo:x", budget_s=1.5) is True
    diag = nav.slot_diag()
    assert diag["task"]["label"] == "shoo:x"
    assert diag["task"]["type"] == "move"
    with nav._nav_lock:
        assert nav._task["budget_s"] == 1.5
        assert nav._task["submitted_ts"] > 0.0