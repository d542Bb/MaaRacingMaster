#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点击器：统一「前台(鼠标) / 后台(手柄)」两种点击方式。

重构背景（2026-09-02 用户拍板，替代原 intent/real/background 三态）：
  - 前台(鼠标) real：SetCursorPos（可见移动）+ SendInput 左键。需游戏在前台。
  - 后台(手柄) gamepad：手柄光标导航（签名剖面识别 + 闭环趋近到位）+ 按 A 键确认。
    cursor_refactor 实现已沉淀进 core.gamepad_cursor（与鼠标逻辑同层）。
  - 意图开关（仅显示意图，独立于点击方式）：
      开启时程序只「导航到目标位置」，不执行确认点击——由用户自己按下/操作。
      鼠标 → 只移光标到目标；手柄 → 只导航到位不按 A。同一开关两者共用。

用法：模块在"首部"持有 Clicker 并同步当前模式，所有点击统一走 click()；
切换只发生在设置页（sidecar set_click_mode → controller.click_mode → ctx.click_mode）。
"""
from __future__ import annotations

import time

from maaracing_assistant.core.window_utils import (
    norm_to_screen,
    send_left_click,
    set_cursor_visible,
    window_client_size,
)

# 合法点击方式（与 GUI 设置页 data-clickmode 保持一致）
CLICK_MODES = ("real", "gamepad")

# 手柄点击容差比例：落点在目标框中心 70% 区域内即可按 A（ROI 本对标整个可交互
# 区域，无需微调到中心像素级精确；防太宽松取 70% 而非全框）。
GAMEPAD_BOX_TOL_RATIO = 0.35

# 手柄光标长时间丢失自愈：连续 GAMEPAD_LOST_REBUILD 次 approach 丢失（转场/
# 面板动画期光标短暂不可见一般 1 次内恢复）→ 判定手柄/光标链路失效，调
# rebuild_cb 销毁并重建虚拟手柄（游戏侧设备断开重连后光标复位重现，比归中
# 盲拉复位可靠且快）；重建冷却防动画期反复拔插设备。
GAMEPAD_LOST_REBUILD = 2
GAMEPAD_REBUILD_COOLDOWN_S = 10.0


class Clicker:
    """统一点击执行器：按 mode 执行点击/光标意图。

    mode：real（前台=鼠标 SendInput）/ gamepad（后台=手柄导航+A 键确认）。
    intent：意图开关（仅显示意图）。置位时只导航到目标、不确认点击，由用户自己按。
            两种 mode 共用同一 intent 语义。
    所有调用点共用同一个实例（模块持有），模式只在"首部"（构造/设置页）切换。
    """

    def __init__(self, hwnd: int = 0, mode: str = "real", intent: bool = False):
        self.hwnd = hwnd
        self.set_mode(mode)
        self.intent = intent
        self._last_pos: tuple[int, int] | None = None  # 最近一次成功执行的屏幕坐标
        self._gamepad = None  # 手柄导航器（懒创建，绑定 controller 手柄/截图）
        self._gamepad_nav_cfg = None  # (capture, gpad, model_path)
        self._rebuild_gamepad_cb = None  # 手柄重建回调（宿主注入；None=不支持自愈）
        self._gamepad_lost_streak = 0  # 手柄光标连续丢失计数（approach lost 累加）
        self._rebuild_cooldown_until_ts = 0.0  # 重建冷却截止（monotonic 秒）
        # 异步点击（2026-09-03 导航线程化）：real 立即执行、结果入槽；gamepad 后台导航
        self._real_result: dict | None = None  # real 模式结果槽（submit 后立即可消费）
        self._last_norm: tuple[float, float] | None = None  # 最近提交归一化坐标（结果回填 last_pos 用）

    def bind_gamepad(self, capture, gpad, model_path=None, confirm_button=None,
                     rebuild_cb=None):
        """绑定后台(手柄)点击所需的能力：截图帧源 + 手柄 + 速度模型 + 确认按钮。

        由模块在需要手柄方式时按需注入（real 前台鼠标全程不触碰手柄能力，
        不创建虚拟手柄设备）；解除对自建手柄/截图的依赖。
        rebuild_cb：光标长时间丢失时的自愈回调（无参 → bool）：销毁并重建虚拟
          手柄后重新走 bind_gamepad 换绑；None=不支持重建（仅快速失败重试）。
        """
        from maaracing_assistant.core.gamepad_cursor import GamepadClicker
        self._gamepad_nav_cfg = (capture, gpad, model_path)
        self._gamepad = GamepadClicker(capture, gpad, model_path=model_path)
        if confirm_button is not None:
            self._gamepad.set_confirm_button(confirm_button)
        self._rebuild_gamepad_cb = rebuild_cb

    @property
    def gamepad_bound(self) -> bool:
        """后台(手柄)导航器是否已绑定（前台(鼠标)方式无需绑定，恒为 False）。"""
        return self._gamepad is not None

    # ---------- 异步执行协议（主循环拥有决策权，导航线程只拥有执行权）----------

    def submit_click(self, cx: float, cy: float, *,
                     box=None, tol_px=None,
                     down_up_gap_ms: int = 30, move_pause_s: float = 0.4) -> bool:
        """提交一次点击（非阻塞）。True=已入队；False=槽忙/未绑定。

        gamepad → 提交导航任务（后台线程闭环），结果经 consume_result 取；
        real → 立即执行，结果入 real 槽（同一协议：submit 后 consume 可取）。
        语义：True = 已入队（不是点击成功）；成功与否看 consume_result。
        """
        if self.mode == "gamepad":
            return self._submit_gamepad("click", cx, cy, intent=self.intent,
                                        box=box, tol_px=tol_px)
        ok = self._click_real(cx, cy, down_up_gap_ms, move_pause_s)
        self._real_result = {"type": "click", "ok": ok, "intent": self.intent}
        return True

    def submit_move(self, cx: float, cy: float, *,
                    box=None, tol_px=None,
                    down_up_gap_ms: int = 30, move_pause_s: float = 0.4) -> bool:
        """提交一次只移动（光标移到位但不点击）。语义同 submit_click，但 intent 恒 True（不点击）。"""
        if self.mode == "gamepad":
            return self._submit_gamepad("move", cx, cy, intent=True,
                                        box=box, tol_px=tol_px)
        prev = self.intent
        self.intent = True
        try:
            ok = self._click_real(cx, cy, down_up_gap_ms, move_pause_s)
        finally:
            self.intent = prev
        self._real_result = {"type": "move", "ok": ok, "intent": True}
        return True

    def _submit_gamepad(self, task_type: str, cx: float, cy: float, *,
                        intent: bool, box, tol_px) -> bool:
        """gamepad 提交：归一化坐标 → 像素 → GamepadClicker.submit（非阻塞）。"""
        if self._gamepad is None:
            return False
        size = window_client_size(self.hwnd)
        if size is None or size[0] <= 0 or size[1] <= 0:
            return False
        cw, ch = size
        px = min(cw - 1, max(0, round(cx * cw)))
        py = min(ch - 1, max(0, round(cy * ch)))
        if tol_px is None and box:
            try:
                bw, bh = float(box[0]), float(box[1])
                if bw > 0 and bh > 0:
                    tol_px = GAMEPAD_BOX_TOL_RATIO * min(bw * cw, bh * ch)
            except Exception:  # noqa: BLE001 —— 容差换算失败回退默认中心微调
                tol_px = None
        self._last_norm = (cx, cy)
        return self._gamepad.submit((px, py), intent=intent, tol_px=tol_px,
                                    task_type=task_type)

    def is_busy(self) -> bool:
        """任务槽忙（含结果待消费 DONE 态）。gamepad 查导航器；real 查 real 槽。"""
        if self.mode == "gamepad":
            return self._gamepad is not None and self._gamepad.is_busy()
        return self._real_result is not None

    def consume_result(self) -> dict | None:
        """取走最近完成结果（无则 None）。取走即清空 → 允许下一任务。

        gamepad 路径：取到后处理丢失累计/重建（主循环线程内，符合
        「device rebuild 主循环唯一所有者」契约）。
        """
        if self.mode == "gamepad":
            if self._gamepad is None:
                return None
            res = self._gamepad.consume_result()
            if res is not None:
                self._handle_gamepad_result(res)
            return res
        res = self._real_result
        self._real_result = None
        return res

    def _handle_gamepad_result(self, res: dict) -> None:
        """消费导航结果时处理：丢失累计/重建计数。

        device_lost=True 且为真实点击（intent=False）→ 累计丢失，达阈值重建；
        意图导航（intent=True：PEEP 意图模式/只移动任务）丢失不触发重建——
        转场期光标被遮罩隐藏是正常现象（2026-09-03 用户实测：丢失误触发重建
        会拔插设备制造空档），故 intent 保护为协议的固定语义。
        """
        if res.get("device_lost"):
            if not res.get("intent"):
                self._gamepad_lost_streak += 1
                self._maybe_rebuild_gamepad()
            return
        if res.get("ok"):
            self._gamepad_lost_streak = 0
            # 成功：回填 last_pos（屏幕坐标）供日志/事件
            if self._last_norm:
                size = window_client_size(self.hwnd)
                if size and size[0] > 0 and size[1] > 0:
                    self._last_pos = norm_to_screen(
                        self.hwnd, self._last_norm[0], self._last_norm[1],
                        size[0], size[1]) or None
        else:
            self._gamepad_lost_streak = 0  # 非丢失失败（超步/aborted）：链路正常

    def cancel(self) -> None:
        """中止在跑导航（模块停止即时生效：置 abort_event，worker 每步检查）。"""
        if self._gamepad is not None:
            self._gamepad.cancel()

    def shutdown(self) -> None:
        """停止导航线程（模块收尾调用）。"""
        if self._gamepad is not None:
            self._gamepad.shutdown()

    # 光标遮挡防线（宪法 §5，P4c 定稿）：光标是已知遮挡物，不做反应式躲避。
    # python 识别侧 = 按锚点 colorspace 校准 + 稳定帧/转场缓冲判定；
    # 图侧 = MRA_Template 的 mask_cursor 遮挡过滤按需启用（本类
    # gamepad_cursor_pos 提供光标真值，桥宿主 cursor_pos 已接线）。

    def gamepad_cursor_pos(self) -> tuple[int, int] | None:
        """手柄导航器最近一次识别到的游戏光标位置（截图帧像素坐标）。

        遮挡过滤（MRA_Template mask_cursor）的光标真值来源；real 模式/未绑定/
        从未识别到光标时为 None。转场期光标隐藏时返回的是陈旧位——消费方
        （识别节点）自担时效，只有真压住命中框才生效，最坏多拒一帧。
        """
        if self._gamepad is None:
            return None
        return getattr(self._gamepad, "last_pos", None)

    @property
    def last_pos(self) -> tuple[int, int] | None:
        """最近一次成功执行的屏幕坐标（供调用方日志/事件记录）。"""
        return self._last_pos

    # ---------- 模式 ----------

    def set_mode(self, mode: str) -> None:
        """切换点击方式（real / gamepad）。非法值直接报错，防止静默错点。"""
        if mode not in CLICK_MODES:
            raise ValueError(f"非法点击方式: {mode!r}，可选 {CLICK_MODES}")
        self.mode = mode

    def set_intent(self, intent: bool) -> None:
        """设置意图开关（仅显示意图）：置位后只导航不确认。"""
        self.intent = bool(intent)

    @property
    def need_foreground(self) -> bool:
        """前台(鼠标)点击需要窗口在前台；后台(手柄)点击不需要（导航在游戏画面内）。"""
        return self.mode == "real"

    # ---------- 执行 ----------

    def click(self, cx: float, cy: float, *,
              down_up_gap_ms: int = 30, move_pause_s: float = 0.4,
              on_progress=None, should_abort=None, box=None, tol_px=None) -> bool:
        """同步兼容壳（已弃用）：submit + 自旋 consume_result，语义与旧同步 click 一致。

        内部走导航线程执行；on_progress 参数不再生效（进度读 nav_progress()），
        should_abort 置位时 cancel 中止。推荐改用 submit_click + consume_result
        异步协议（主循环不被导航阻塞）。
        """
        if not self.submit_click(cx, cy, box=box, tol_px=tol_px,
                                 down_up_gap_ms=down_up_gap_ms,
                                 move_pause_s=move_pause_s):
            return False
        while True:
            if should_abort is not None and should_abort():
                self.cancel()
                time.sleep(0.02)
                continue
            res = self.consume_result()
            if res is not None:
                return bool(res.get("ok"))
            time.sleep(0.02)

    def move_only(self, cx: float, cy: float, *,
                  on_progress=None, should_abort=None, box=None, tol_px=None) -> bool:
        """同步兼容壳（已弃用）：submit_move + 自旋 consume_result，只移动不点击。

        推荐改用 submit_move + consume_result 异步协议。
        """
        if not self.submit_move(cx, cy, box=box, tol_px=tol_px):
            return False
        while True:
            if should_abort is not None and should_abort():
                self.cancel()
                time.sleep(0.02)
                continue
            res = self.consume_result()
            if res is not None:
                return bool(res.get("ok"))
            time.sleep(0.02)

    def _click_real(self, cx, cy, down_up_gap_ms, move_pause_s) -> bool:
        """前台(鼠标)：SetCursorPos + SendInput；意图模式只移光标。"""
        if not self.hwnd:
            return False
        size = window_client_size(self.hwnd)
        if size is None or size[0] <= 0 or size[1] <= 0:
            return False
        cw, ch = size
        pos = norm_to_screen(self.hwnd, cx, cy, cw, ch)
        if pos is None:
            return False
        sx, sy = pos
        if not set_cursor_visible(sx, sy):
            return False
        self._last_pos = (sx, sy)
        if self.intent:
            return True  # 意图模式：光标已就位，不点击（由用户自己按）
        time.sleep(move_pause_s)
        if not send_left_click(down_up_gap_ms):
            return False
        return True

    def _maybe_rebuild_gamepad(self) -> None:
        """光标连续丢失达阈值 → 触发重建虚拟手柄（自愈），带冷却防动画期反复拔插。

        冷却先落再回调（回调内部失败也计冷却）：重建失败说明环境性问题
        （驱动/能力缺失），下一丢失周期再试即可，不阻塞主循环下帧重试。
        """
        if self._rebuild_gamepad_cb is None:
            return
        if self._gamepad_lost_streak < GAMEPAD_LOST_REBUILD:
            return
        now = time.monotonic()
        if now < self._rebuild_cooldown_until_ts:
            return
        self._rebuild_cooldown_until_ts = now + GAMEPAD_REBUILD_COOLDOWN_S
        self._gamepad_lost_streak = 0
        self._rebuild_gamepad_cb()