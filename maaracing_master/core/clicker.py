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

import math
import time

from maaracing_master.core.logger import logger
from maaracing_master.core.window_utils import (
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
        self._shoo_cooldown_until_ts = 0.0  # 光标避让冷却截止（monotonic 秒）
        # 光标失踪期避让探测的下次放行时刻（见 SHOO_PROBE_INTERVAL_S）
        self._shoo_probe_next_ts = 0.0
        # 避让空转检测：同一 (压住区域, 避让点) 连续提交且光标始终没移开 → 升级告警
        self._shoo_repeat_sig: tuple = ()
        self._shoo_repeat_count = 0
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
        from maaracing_master.core.gamepad_cursor import GamepadClicker
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

    def swap_gamepad(self, new_gpad) -> None:
        """换绑底层手柄（设备重建后调用）。

        内部转调导航器的 swap_gpad——宿主不再 getattr 掏 `_gamepad` 私有成员（P5）。
        导航任务运行中换绑会抛 RuntimeError（导航器契约：worker 必须已完全退出
        设备访问），由调用方按「时机不对、下帧重试」处理。
        """
        if self._gamepad is None:
            raise RuntimeError("swap_gamepad: 手柄导航器未绑定")
        self._gamepad.swap_gpad(new_gpad)

    # 光标遮挡防线（2026-09-11 修订）：光标是已知遮挡物，按通路分治——
    # 图侧模板识别 = MaaRM_Template 的 mask_cursor 遮挡拒绝（gamepad_cursor_pos
    # 提供真值）；OCR 读数与决策段内联匹配 = 反应式避让 auto_shoo（P4c 曾以
    # mask_cursor 替代全部 shoo，但 mask_cursor 只装在图内节点上，OCR 通路
    # 无遮挡处理实机被读脏：出价按钮文字「出价.39,5」/输入框瞬空读振荡）+
    # 消费侧遮挡剔除/锚点防抖兜底。

    # ---------- 光标驻留看守（避让）----------

    SHOO_COOLDOWN_S = 0.3  # 避让冷却（monotonic 秒）。时长一律以时间为标准、不用帧数定义
    # ——帧率随通路变（v3 300ms/帧、v4 实测约 125ms/帧），帧数口径会被稀释。0.3s ≈ v4
    # 节奏下 2~3 帧：光标移开识别区后即可恢复检测/再次避让（避让导航本身已宽松，
    # 见 SHOO_TOL_PX）。2026-09-03 定案：原 4.0s 过长——点击后光标压住转移信号所在
    # 识别区需避让的窗口内，冷却期间识别区被挡也不避让 → 转移信号（如"匹配中"）漏检。
    SHOO_TOL_PX = 60.0    # 避让导航容差 px：只需把光标移出识别区（等效半径由宿主按
    # template_match.cursor_occlusion_radius_px 随帧宽算，1280 帧约 29px），
    # 无需精确到位；60px 大幅缩短同步导航耗时，避免避让阻塞吃掉转移信号窗口
    # 光标连续未识别跳过避让的阈值：游戏转场期（点「开始匹配」后匹配加载遮罩
    # 隐藏光标、面板开关动画等）光标短暂不可见属正常，此时 last_pos 是陈旧
    # 位置，盲导航既挪不动又白白阻塞主循环 → 跳过，等光标重现/阶段切换。
    SHOO_SKIP_MISS_STREAK = 3
    # 光标失踪期间的避让探测节流（秒）：miss_streak 清零只发生在导航任务的
    # read_pos 成功里，而 S1 等待态没有点击意图、避让是唯一导航提交者——
    # 永久 skip 会形成自锁死局（见 auto_shoo 内注释）。1s 一跳闸既不刷爆
    # 任务槽（保留 2026-09-03 转场期保护的本义），又能让光标重现即恢复。
    SHOO_PROBE_INTERVAL_S = 1.0
    # 避让候选方向（归一化偏移）：下→上→右→左→远上，取第一个不压区域者
    SHOO_DIRECTIONS = ((0.0, 0.14), (0.0, -0.14), (0.13, 0.0), (-0.13, 0.0), (0.0, -0.30))
    # 同一 (压住区域, 避让点) 连续提交达此数仍未移开 → WARNING（避让语义失效升级可见，
    # 多因游戏不接受该导航落点；DEBUG 时代的 95 连发空转没人看见，2026-09-14 教训）
    SHOO_REPEAT_WARN_AFTER = 8

    def auto_shoo(self, guard_rects, *, radius_px: float,
                  frame_size: tuple[int, int],
                  next_center: tuple[float, float] | None = None) -> dict | None:
        """光标驻留看守：光标压住宿主指定的「需保持可识别」区域时，自动导航到
        邻近空白处（不点击）。由宿主决策段**每帧**驱动——阶段状态每帧更新后，
        激活区域集合自然跟随新阶段；失败后冷却过期自动重试。

        **异步**：判定逻辑在决策段（本方法）执行，导航动作经 submit_move 提交
        后台线程，**不阻塞决策段**。

        guard_rects：[(key, (x1,y1,x2,y2))] 归一化区域（宿主的领域知识，带 key
          供触发诊断）。
        radius_px：光标遮挡等效半径（圆盘+环+hover 高亮，帧像素）。
        next_center：宿主下一个点击意图的中心（归一化，可选）——它在遮挡区外时
          跳过避让（下一次点击导航会自然把光标带离），省一次专门导航；它在遮挡
          区内（或无意图/等待态）才避让——光标已在目标上时按 A 即可，挪走反而
          多此一举，也避免"点锚点按钮后自己挡自己"的震荡。

        返回 {"key": 命中区域, "point": (nx, ny) 避让点}；未触发返回 None。
        防抖/互斥设计：
          • 冷却（monotonic 秒）：避让后 SHOO_COOLDOWN_S 内不再触发
          • 任务槽忙（点击/避让在跑或结果待消费）→ 跳过本次判定（click 优先）
          • 避让走 submit_move（intent=True），不产生点击
          • 光标位置取手柄导航器最近一次成功识别位；导航器未绑定/位置未知不触发
        """
        if self.mode != "gamepad" or self._gamepad is None:
            return None
        if self.is_busy():
            return None  # 任务槽忙（点击优先）→ 下帧再判定
        now = time.monotonic()
        if now < self._shoo_cooldown_until_ts:
            return None
        # 光标当前连续未识别（游戏转场期光标被遮罩隐藏，如匹配中加载画面）：
        # last_pos 是陈旧位置，盲导航只会白占任务槽；等光标重现再避让
        # （2026-09-03 用户实测：第二次匹配瞬间 PEEP 消失、程序空档）。
        # 但不得永久跳过：miss_streak 清零只发生在导航任务的 read_pos 成功里，
        # S1 等待态无点击意图时避让是唯一导航提交者——永久 skip = 自锁死局
        # （真机 2026-09-14：确认出价后光标残留按钮白字上识别失踪，避让任务
        # lost 收尾 miss_streak≥3，此后 40s 无一次避让提交、读侧拿陈旧位永远
        # 判「压住」等光标移开）。改 1s 节流探测：光标真隐藏的代价与 skip 同阶
        # （p 阶段快速失败），光标可见则立即恢复常规避让节奏。
        if getattr(self._gamepad, "miss_streak", 0) >= self.SHOO_SKIP_MISS_STREAK:
            if now < self._shoo_probe_next_ts:
                return None
            self._shoo_probe_next_ts = now + self.SHOO_PROBE_INTERVAL_S
        pos = self._gamepad.last_pos
        W, H = frame_size
        if not pos or W <= 0 or H <= 0 or not guard_rects:
            return None
        rx, ry = radius_px / W, radius_px / H
        nx, ny = pos[0] / W, pos[1] / H

        def _hit(x: float, y: float) -> str | None:
            for key, (x1, y1, x2, y2) in guard_rects:
                if x1 - rx <= x <= x2 + rx and y1 - ry <= y <= y2 + ry:
                    return key
            return None

        hit_key = _hit(nx, ny)
        if hit_key is None:
            return None  # 光标没压任何需识别区域
        if next_center is not None:
            ncx, ncy = float(next_center[0]), float(next_center[1])
            if _hit(ncx, ncy) is None:
                return None  # 下一个意图目标在干净区：点击导航自然带离，无需避让
        for dxn, dyn in self.SHOO_DIRECTIONS:
            px_ = min(0.97, max(0.03, nx + dxn))
            py_ = min(0.95, max(0.05, ny + dyn))
            if _hit(px_, py_) is None:
                # 候选必须离当前位置 > 导航「到位容差」：太近（含被屏幕边界 clamp 挤近）
                # 的点提交后会被导航判定「已到位」原地零移动——真机 2026-09-14 R5 死循环
                # （避让点 0.95 距光标真实位 50px < SHOO_TOL_PX 60px，32s 连提 95 次光标
                # 纹丝不动，label OCR 永远被遮挡 → S1 空等）。跳过让位下一方向。
                if math.hypot((px_ - nx) * W, (py_ - ny) * H) <= self.SHOO_TOL_PX:
                    continue
                self._shoo_cooldown_until_ts = now + self.SHOO_COOLDOWN_S
                # 宽松容差避让：只移出识别区即返回（无需精确微调）；异步提交，
                # 导航线程后台执行，决策段不被阻塞（转移信号窗口不丢失）。
                if self.submit_move(px_, py_, tol_px=self.SHOO_TOL_PX):
                    sig = (hit_key, round(px_, 3), round(py_, 3))
                    if sig == self._shoo_repeat_sig:
                        self._shoo_repeat_count += 1
                        if self._shoo_repeat_count == self.SHOO_REPEAT_WARN_AFTER:
                            logger.log(
                                f"[鉴宝点击] 避让空转：已连续 {self._shoo_repeat_count} 次导航到 "
                                f"({px_:.2f},{py_:.2f}) 光标仍压住 [{hit_key}]（当前位 "
                                f"({nx:.2f},{ny:.2f})）——游戏可能不接受该落点，需人工核查",
                                "WARNING")
                    else:
                        self._shoo_repeat_sig = sig
                        self._shoo_repeat_count = 1
                    return {"key": hit_key, "point": (px_, py_)}
                return None
        return None  # 邻域全是需识别区（少见），放弃避让保持现状

    def gamepad_cursor_pos(self) -> tuple[int, int] | None:
        """手柄导航器最近一次识别到的游戏光标位置（截图帧像素坐标）。

        遮挡过滤（MaaRM_Template mask_cursor）的光标真值来源；real 模式/未绑定/
        从未识别到光标时为 None。转场期光标隐藏时返回的是陈旧位——消费方
        （识别节点）自担时效，只有真压住命中框才生效，最坏多拒一帧。
        """
        if self._gamepad is None:
            return None
        return getattr(self._gamepad, "last_pos", None)

    def gamepad_cursor_age_s(self) -> float:
        """最近一次识别到光标距今的秒数（从未识别到 → inf）。

        PEEP 诊断层用：导航结束后仍要显示「上次光标在哪」，同时必须能说出
        这个位置有多旧，否则用户无法区分实时位与历史位。
        """
        if self._gamepad is None:
            return float("inf")
        ts = getattr(self._gamepad, "last_pos_ts", 0.0)
        return (time.monotonic() - ts) if ts else float("inf")

    def nav_progress(self) -> dict | None:
        """手柄导航最近进度快照（PEEP 渲染用）；real 模式/未绑定 → None。

        导航器是私有成员，宿主不该 getattr 去掏（Clicker 是这份数据的唯一主人）——
        与 gamepad_cursor_pos 同一取向：读侧一律走本类公开接口。
        """
        if self._gamepad is None:
            return None
        return self._gamepad.nav_progress()

    def cursor_candidates(self, *, max_age_s: float = 2.0) -> dict | None:
        """手柄光标识别候选快照（PEEP 诊断用）；非手柄/未绑定/从未识别 → None。

        max_age_s：新鲜度阈值。**超龄不再返回 None**——候选数据照常返回，只附
        `stale`/`age_s` 标记，由渲染层淡化显示。旧口径（超龄即 None）让叠加层在
        两次点击之间的大段空档里没有任何内容可看，恰是排查「光标卡住」最需要的
        时段（真机 2026-09-15：数字键链路空转 24s，预览全程无候选可看）。
        """
        gp = self._gamepad
        if gp is None or not getattr(gp, "last_cands", None):
            return None
        age = time.monotonic() - getattr(gp, "last_cands_ts", 0.0)
        return {"list": list(gp.last_cands), "sel": gp.last_cand_sel,
                "stale": age > max_age_s, "age_s": age}

    def press_button(self, button, duration: float = 0.15) -> bool:
        """经已绑定的手柄导航器按一个按钮（按下→update→按住→松开→update）。

        与点击共用同一个虚拟设备、同一条租约，不另开输入口子。未绑定手柄时
        返回 False——按键能力只存在于 gamepad 模式，调用方据此走失败分支，
        不得静默当成成功。
        """
        if self._gamepad is None:
            return False
        return bool(self._gamepad.press_confirm(button, duration=duration))

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