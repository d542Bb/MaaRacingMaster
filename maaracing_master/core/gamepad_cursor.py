#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手柄光标导航 + 确认点击（后台点击执行单元）。

由 cursor_refactor/ 的探针实现（cursor_monitor 三态识别 + approach_validate 连续 P 趋近 +
train_stick_speed 速度模型）沉淀而来，供 core.clicker 的「后台(手柄)」点击方式复用，
与「前台(鼠标)」SendInput 点击同层。**运行时不再依赖 cursor_refactor 脚本目录。**

职责：
  - 识别：签名剖面法，识别游戏内白色圆盘光标（normal / interactive 两态）。
  - 导航：以「摇杆-光标速度模型 + 闭环趋近」把光标从当前位收敛到目标像素坐标。
  - 确认：导航到位后按 A 键触发点击；（意图模式）只导航不确认，由用户自己按。

底座与手柄均依赖注入：截图帧源（WgcCapture 或回环帧）与虚拟手柄由调用方提供
（复用 controller 的 _gpad / treasures 的 capture），本模块不自建，避免手柄/截图冲突。
"""
from __future__ import annotations

import math
from typing import Any
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import cv2

# core 自带的摇杆-光标速度模型（cursor_refactor 标定产物，k/deadzone/resolution）。
# GamepadClicker(model_path=None) 时默认加载；数值由离线探针 cursor_refactor 标定得到。
_DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "resources" / "stick_speed_model.json"


# ======================================================================
#  光标三态签名（实测色值，容差见 *_TOL）——从 cursor_monitor 迁移
# ======================================================================
GRAY_DIFF = 18            # 三通道两两最大差 ⇐ 此值 → 中性灰像素（掩码层，宽松）
SAT_THRES = 40            # 饱和度过高的彩色排除
PURE_GRAY_TOL = 10        # 射线解析层纯灰门槛

# 三态签名（BGR 顺序读取；剖面在 RGB 空间比对则转换）。pressed 态不识别
# （按下由程序自身触发，程序知道按下时机）。
STATE_SIGNATURES = {
    "normal":     {"center": (255, 255, 255), "radius": (6, 13),   "ring": (133, 133, 133), "ring_thick": (2, 7)},
    "interactive": {"center": (192, 192, 192), "radius": (6, 12),  "ring": (250, 250, 250), "ring_thick": (2, 6)},
}
CENTER_TOL = 30      # 内盘色容差（每通道）
RING_TOL = 30        # 环色容差（每通道）
THRESH_SCORE = 0.60  # 归一化匹配分下限，低于此视为非光标

# 种子定位参数
SEED_CORE_TOL = 25
SEED_RING_TOL = 20
CORE_AREA = (80, 900)
RING_AREA = (120, 900)
CORE_MIN_CIRC = 0.45
RING_MIN_CIRC = 0.25
SEED_MERGE_DIST = 16

# 时间连续性先验
JUMP_DIST = 80.0
JUMP_MIN_SCORE = 0.85
NEAR_SCORE_REL = 0.15
MISS_STREAK_RESET = 15

# 趋近控制参数（从 approach_validate 迁移）
KP = 2.0             # P 增益(1/s)：v_des = KP·dist；dist=375px 时满速
LAG_S = 0.07         # 视觉反馈延迟估计(2帧≈67ms)，停靠提前量
STOP_MARGIN = 6.0    # 停靠额外余量 px
MICRO_MAG = 5000     # 微调基础幅度=最小有效幅度（硬开关死区值）
MICRO_T = 1 / 60.0   # 微调脉冲 1 帧 ≈16.7ms
MICRO_EFFECTIVE_S = 0.034  # 单次微调脉冲的有效时长（脉冲 1/60s + 游戏 ~1 帧采样拖尾）
# 距离-力度挡位：按剩余距离选杆量（每步位移 ≈ mag·k·0.034s），远挡大步减少逼近
# 次数，近挡小步防过冲；配合单步位移上限（≤剩余距离 60%）双向保险。
MICRO_GEAR_TABLE = (
    (15.0, 16000),         # 距离 ≥15px：约 13px/步
    (8.0, 10000),          # 距离 ≥8px：约 8px/步
    (0.0, MICRO_MAG),      # 其余：约 4px/步（最小有效杆量）
)
TOL = 5.0            # 最终误差容差 px（缺省中心精确微调；手柄模式按目标框 70% 放宽）
MAX_P_STEPS = 200
MAX_MICRO_STEPS = 25
MAX_P_MISS = 8       # P 趋近连续识别丢失上限（约1.8s）：超限快速失败，交外层下帧重试
MAX_MICRO_MISS = 6   # 微调连续识别丢失上限（约3s；按A后面板动画期光标可能短暂不可见）

MAX_AXIS = 32767
POS_MED_FRAMES = 3


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _is_gray_px(b, g, r):
    return (abs(b - g) <= GRAY_DIFF and abs(g - r) <= GRAY_DIFF
            and abs(b - r) <= GRAY_DIFF)


def _is_pure_gray(b, g, r):
    return (abs(b - g) <= PURE_GRAY_TOL and abs(g - r) <= PURE_GRAY_TOL
            and abs(b - r) <= PURE_GRAY_TOL)


def _color_dist(c1, c2):
    return math.sqrt((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2)


def radial_profile(frame, cx, cy, n_rays=4, max_r=32):
    """沿多个方向从质心逐像素采样色值序列（遇到越界立即截断）。"""
    h, w = frame.shape[:2]
    dirs = [(-1, -1), (1, -1), (-1, 1), (1, 1)]
    prof = []
    for dx, dy in dirs:
        seq = []
        for k in range(0, max_r + 1):
            x = int(round(cx + dx * k))
            y = int(round(cy + dy * k))
            if not (0 <= x < w and 0 <= y < h):
                break
            b, g, r = int(frame[y, x, 0]), int(frame[y, x, 1]), int(frame[y, x, 2])
            seq.append((b, g, r))
        if len(seq) >= 4:
            prof.append(seq)
    return prof


def _ray_segments(seq, max_tol=40):
    if not seq:
        return []
    segments = []
    cur_start = 0
    for i in range(1, len(seq)):
        if _color_dist(seq[i], seq[i - 1]) > max_tol:
            segments.append((cur_start, i))
            cur_start = i
    segments.append((cur_start, len(seq)))
    out = []
    for s, e in segments:
        if e - s >= 2:
            seg = seq[s:e]
            avg = tuple(int(round(sum(c[k] for c in seg) / len(seg))) for k in range(3))
            out.append((avg, s, e))
    return out


def _ray_parse(seq):
    segs = _ray_segments(seq)
    if not segs:
        return None, None, None, 0
    center_color, s0, e0 = segs[0]
    if not _is_pure_gray(*center_color):
        return center_color, e0, None, 0
    center_r = e0
    for avg, s, e in segs[1:]:
        if _is_pure_gray(*avg) and _color_dist(avg, center_color) > CENTER_TOL * 0.8:
            return center_color, center_r, avg, e - s
    return center_color, center_r, None, 0


def _color_mask(frame, ref, tol):
    ref = np.array(ref, dtype=np.int16)
    diff = frame.astype(np.int16) - ref
    dist2 = (diff * diff).sum(axis=2)
    return (dist2 <= tol * tol).astype(np.uint8) * 255


def _seeds_from_mask(mask, area_lo, area_hi, min_circ):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    seeds = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < area_lo or area > area_hi:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter < 1e-6:
            continue
        circularity = 4 * math.pi * area / (perimeter * perimeter)
        if circularity < min_circ:
            continue
        x, y, cw, ch = cv2.boundingRect(cnt)
        seeds.append((x + cw // 2, y + ch // 2, area, circularity))
    return seeds


def _dedup_seeds(all_seeds):
    all_seeds = sorted(all_seeds, key=lambda s: s[2], reverse=True)
    kept = []
    for s in all_seeds:
        if all((s[0] - k[0]) ** 2 + (s[1] - k[1]) ** 2 > SEED_MERGE_DIST ** 2
               for k in kept):
            kept.append(s)
    return kept


def build_seeds(frame_rgb):
    R, G, B = (frame_rgb[:, :, i].astype(np.int16) for i in range(3))
    rgb = np.stack([R, G, B], axis=2)
    seeds = []
    seeds += _seeds_from_mask(_color_mask(rgb, STATE_SIGNATURES["interactive"]["center"],
                                          SEED_CORE_TOL),
                              *CORE_AREA, CORE_MIN_CIRC)
    seeds += _seeds_from_mask(_color_mask(rgb, STATE_SIGNATURES["normal"]["ring"],
                                          SEED_RING_TOL),
                              *RING_AREA, RING_MIN_CIRC)
    max_diff = np.maximum(np.maximum(np.abs(R - G), np.abs(G - B)), np.abs(B - R))
    gray_mask = (max_diff <= GRAY_DIFF).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(gray_mask, cv2.MORPH_OPEN, kernel, iterations=1)
    seeds += _seeds_from_mask(mask, 120, 2600, 0.55)
    return _dedup_seeds(seeds)


class CursorCandidate:
    __slots__ = ("pos", "area", "radius_est", "circularity", "aspect",
                 "score", "state", "center_color", "ring_color", "ring_thick")

    def __init__(self):
        self.pos = (0, 0)
        self.area = 0.0
        self.radius_est = 0.0
        self.circularity = 0.0
        self.aspect = 1.0
        self.score = 0.0
        self.state = "reject"
        # 显式 Optional 标注：恒从 None 初始化会让推断器把槽位锁死为 None 型
        self.center_color: tuple | None = None
        self.ring_color: tuple | None = None
        self.ring_thick = 0


def _score_against_signature(cand, sig):
    if cand.center_color is None:
        return 0.0
    center = cand.center_color
    s_center = sig["center"]
    center_err = _color_dist(center, s_center)
    center_score = max(0.0, 1.0 - center_err / (CENTER_TOL * 3.0))
    r_est = cand.radius_est
    r_lo, r_hi = sig["radius"]
    if r_lo <= r_est <= r_hi:
        r_score = 1.0
    else:
        r_score = max(0.0, 1.0 - abs(r_est - (r_lo + r_hi) / 2) / max(1, (r_hi - r_lo) or 1))
    if sig["ring"] is None:
        if cand.ring_color is None or cand.ring_thick < 1:
            ring_score = 1.0
        else:
            ring_score = max(0.0, 1.0 - cand.ring_thick / 6.0)
    else:
        if cand.ring_color is None:
            ring_score = 0.2
        else:
            ring_err = _color_dist(cand.ring_color, sig["ring"])
            ring_score = max(0.0, 1.0 - ring_err / (RING_TOL * 3.0))
            if sig["ring_thick"]:
                thick_lo, thick_hi = sig["ring_thick"]
                if not (thick_lo <= cand.ring_thick <= thick_hi):
                    ring_score *= max(0.3, 1.0 - abs(cand.ring_thick - (thick_lo + thick_hi) / 2) / 6.0)
    shape_score = min(1.0, cand.circularity / 0.9)
    score = center_score * 0.45 + r_score * 0.15 + ring_score * 0.30 + shape_score * 0.10
    return float(max(0.0, min(1.0, score)))


def detect_cursor(frame_rgb):
    """识别光标，返回 (列表[CursorCandidate]，选中或None)。"""
    frame = frame_rgb  # RGB 空间
    seeds = build_seeds(frame)
    if not seeds:
        return [], None
    targets = []
    for sx, sy, s_area, s_circ in seeds:
        c = CursorCandidate()
        c.pos = (sx, sy)
        c.area = float(s_area)
        c.radius_est = math.sqrt(s_area / math.pi)
        c.circularity = s_circ
        prof = radial_profile(frame, sx, sy)
        parsed = [_ray_parse(s) for s in prof]
        valid = [p for p in parsed if p[0] is not None and _is_pure_gray(*p[0])]
        if not valid:
            c.center_color = None
            c.score = 0.0
            c.state = "reject"
            targets.append(c)
            continue
        centers = [p[0] for p in valid]
        c.center_color = tuple(int(round(sum(x[k] for x in centers) / len(centers))) for k in range(3))
        radii = sorted(p[1] for p in valid)
        c.radius_est = radii[len(radii) // 2] if len(radii) else 0
        ring_rays = [p for p in valid if p[2] is not None]
        if ring_rays:
            ring_colors = [p[2] for p in ring_rays]
            c.ring_color = tuple(int(round(sum(x[k] for x in ring_colors) / len(ring_colors))) for k in range(3))
            c.ring_thick = sorted(p[3] for p in ring_rays)[len(ring_rays) // 2]
        else:
            c.ring_color, c.ring_thick = None, 0
        best_state = "reject"
        best_score = 0.0
        for name, sig in STATE_SIGNATURES.items():
            sc = _score_against_signature(c, sig)
            if sc > best_score:
                best_state = name
                best_score = sc
        c.state = "interactive" if best_state == "interactive" else best_state
        c.score = best_score
        targets.append(c)
    targets.sort(key=lambda c: c.score, reverse=True)
    sel = select_cursor(targets, None, 0)
    return targets, sel


def select_cursor(targets, last_pos, miss_streak):
    if not targets:
        return None
    top = targets[0]
    if top.score < THRESH_SCORE:
        return None
    if (last_pos is None or miss_streak >= MISS_STREAK_RESET):
        return top
    def dist(c):
        return math.hypot(c.pos[0] - last_pos[0], c.pos[1] - last_pos[1])
    near = [c for c in targets if c.score >= THRESH_SCORE and dist(c) <= JUMP_DIST]
    if near:
        best_near = max(near, key=lambda c: c.score)
        if top.score - best_near.score <= NEAR_SCORE_REL:
            return best_near
        return top
    return top if top.score >= JUMP_MIN_SCORE else None


# ======================================================================
#  手柄导航 + 确认点击执行器
# ======================================================================

class GamepadClicker:
    """手柄光标导航 + 确认点击（**导航线程化**）。

    注入：
      - capture: 截图能力对象（提供 .screenshot()，即 core.capabilities.
        CaptureCapability）、返回 RGB ndarray 帧的 callable，或 .get_latest() 裸帧源。
      - gpad: 提供 .left_joystick(x,y)/.press_button/.release_button/.update 的手柄对象。
      - model_path: stick_speed_model.json 路径（速度模型 k / deadzone / resolution）。
    意图模式：intent 开关置位时只导航到位、不按 A 确认（由用户手动按下）。

    并发模型（2026-09-03 用户拍板：主循环拥有决策权，导航线程只拥有执行权）：
      - 独立导航线程跑 approach 闭环（33ms/步读光标+推摇杆），**主循环不再被阻塞**。
      - 任务槽/结果槽经 `_nav_lock` 保护；状态机：IDLE → submit → RUNNING →
        worker complete → DONE(结果待消费，仍 busy) → consume_result → IDLE。
      - 契约：
        1. `_result` 未消费前禁止覆盖（worker 写入前断言 `_result is None`）。
        2. `is_busy()` = 任务在跑 **或** 结果待消费（DONE 也算 busy）。
        3. 共享快照采用发布-订阅（snapshot publication）：生产线程只整体替换
           引用（last_pos/last_cands/_progress），消费线程只读，不原地修改。
        4. 取消用 `threading.Event`（abort_event），不靠跨线程改普通 bool。
        5. 光标丢失 → 结果带 `device_lost: True` → worker 回到等待态后，
           主循环 consume 时才允许重建设备（swap_gpad）。
      - vgamepad 唯一所有者是导航线程；主循环经 submit/cancel/swap_gpad 控制。
    """

    def __init__(self, capture, gpad, model_path: Path | None = None,
                 resolution: tuple[int, int] | None = None):
        self._capture = capture
        self._gpad = gpad
        self._model = self._load_model(model_path)
        self.res = tuple(self._model.get("resolution", [1282, 759])) if self._model else \
            (resolution or (1280, 720))
        self.k = self._model["k"] if self._model else 0.02
        self.deadzone = self._model["deadzone"] if self._model else 4260.0
        self._confirm_btn = None  # 确认按钮（bind 时经 set_confirm_button 注入，缺省 A）
        # 共享快照（snapshot publication：worker 整体替换，主循环只读）
        self.last_pos: tuple | None = None
        self.miss_streak = 0
        # 最近一次成功识别的时刻（monotonic）：导航空闲/结束后 PEEP 仍要标出
        # 「上次光标在哪、多久前」，没有它只能显示陈旧位却说不出新鲜度。
        self.last_pos_ts: float = 0.0
        # 识别候选快照（PEEP 诊断用，read_pos 每帧刷新）：候选按分数降序截前 8 个
        # [(x, y, score, state)]，含低于置信门槛的拒识候选；sel=选中候选下标或 None。
        self.last_cands: tuple = ()
        self.last_cand_sel: int | None = None
        self.last_cands_ts: float = 0.0  # 快照时间戳（monotonic，消费方做陈旧丢弃）
        # ---- 任务/结果槽 + 进度快照（经 _nav_lock 保护）----
        self._nav_lock = threading.Lock()
        self._task: dict | None = None    # {type, target, intent, tol_px, abort_event}
        self._result: dict | None = None  # 最近完成结果（DONE 态，consume 后清空）
        self._progress = {                # 导航进度快照（PEEP 渲染用）
            "seq": 0, "stage": None, "pos": None, "target": None, "dist": None, "ts": 0.0,
        }
        self._shutdown = threading.Event()
        self._nav_thread = threading.Thread(target=self._nav_loop,
                                            name="gamepad-nav", daemon=True)
        self._nav_thread.start()

    @staticmethod
    def _load_model(model_path: Path | None):
        if model_path is None:
            model_path = _DEFAULT_MODEL_PATH  # 未指定 → 尝试 core 自带标定模型
        if not model_path.is_file():
            return None
        import json
        try:
            return json.loads(model_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 模型损坏回退线性估计
            return None

    # ---------- 任务/结果槽（主循环↔导航线程协议）----------

    def submit(self, target, *, intent: bool = False, tol_px: float | None = None,
               task_type: str = "click") -> bool:
        """提交导航任务（非阻塞）。返回 True=已入队；False=槽忙/结果未消费。

        准入条件：`_task is None and _result is None`（DONE 态也算忙）。
        """
        with self._nav_lock:
            if self._task is not None or self._result is not None:
                return False
            self._task = {
                "type": task_type,
                "target": tuple(target),
                "intent": intent,
                "tol_px": tol_px,
                "abort_event": threading.Event(),
            }
            return True

    def consume_result(self) -> dict | None:
        """取走最近完成结果（无则 None）。取走即清空 → 允许下一任务。"""
        with self._nav_lock:
            res = self._result
            self._result = None
            return res

    def is_busy(self) -> bool:
        """是否有任务在跑 **或** 结果待消费（DONE 态也算 busy，防止覆盖/抢占）。"""
        with self._nav_lock:
            return self._task is not None or self._result is not None

    def nav_progress(self) -> dict:
        """读最近进度快照（PEEP 渲染用）。返回副本，seq 递增=有新进展。"""
        with self._nav_lock:
            return dict(self._progress)

    def cancel(self):
        """置中止标记（导航线程每步检查 abort_event）。"""
        with self._nav_lock:
            if self._task is not None:
                self._task["abort_event"].set()

    def swap_gpad(self, new_gpad):
        """替换底层手柄（重建设备后换绑）。**仅允许任务槽空闲时调用**——
        worker 已完全退出设备访问（回到等待态）才可替换，否则抛错。
        """
        with self._nav_lock:
            if self._task is not None:
                raise RuntimeError("swap_gpad: 导航任务运行中，禁止替换设备")
            self._gpad = new_gpad

    def shutdown(self):
        """停止导航线程（模块收尾调用）。置中止 + 唤醒。"""
        self._shutdown.set()
        with self._nav_lock:
            if self._task is not None:
                self._task["abort_event"].set()

    def _publish_progress(self, stage, pos=None, target=None, dist=None, ok=None,
                          done=False):
        with self._nav_lock:
            self._progress = {
                "seq": self._progress["seq"] + 1,
                "stage": stage,
                "pos": pos,
                "target": target,
                "dist": dist,
                "ok": ok,
                "done": done,
                "ts": time.monotonic(),
            }

    def _nav_loop(self):
        """导航线程主循环：等任务 → 跑闭环 → 发布结果 → 回等待态。"""
        while not self._shutdown.is_set():
            with self._nav_lock:
                task = self._task
            if task is None:
                time.sleep(0.005)
                continue
            try:
                res = self._approach_sync(task)
            except Exception as e:  # noqa: BLE001 —— 导航异常不杀线程
                res = {"type": task.get("type"), "ok": False,
                       "reason": f"导航线程异常: {e!r}"}
            with self._nav_lock:
                # 契约1：结果未消费前禁止覆盖（绝不允许 A 完成后 B 又完成覆盖 A）
                assert self._result is None, "result 未消费前禁止覆盖"
                self._task = None
                self._result = res

    # ---------- 帧/光标 ----------

    def _frame(self) -> np.ndarray | None:
        """读一帧（三种注入形态都吃：callable / 截图能力对象 / WgcCapture 裸帧源）。

        截图能力对象这条分支（`CaptureCapability.screenshot()`）是 v4 装配的注入形态
        （`bind_gamepad(self.ctx.capture, ...)`）；漏认它会让闭环静默拿到 None，
        表现为「摇杆从未推出 + PEEP 无光标内容」却被判成 device_lost（2026-09-10）。
        """
        cap: Any = self._capture  # 注入形态三选一（callable / 截图能力对象 / 裸帧源），无统一协议
        if callable(cap):
            # callable() 守卫会把 Any 收窄为「返回 object 的可调用」，帧类型只能由约定保证
            return cap()  # pyright: ignore[reportReturnType]
        shot: Any = getattr(cap, "screenshot", None)
        if shot is not None:
            return shot()
        getter: Any = getattr(cap, "get_latest", None)
        if getter is not None:
            frame, *_ = getter()
            return frame
        return None

    def read_pos(self, n: int = POS_MED_FRAMES, timeout: float = 1.0) -> tuple | None:
        """读当前光标位置（带跨帧连续性先验）。

        detect_cursor 本身无状态（每帧独立识别）：光标接近按钮时，按钮上的白色
        高亮元素（hover 态图标/圆点）签名分可能反超真光标，无先验时闭环会追着
        假候选跑（"快到目标突然飞走"）。连续性由 select_cursor(last_pos,
        miss_streak) 保护：last_pos 附近(≤JUMP_DIST)候选优先；无附近候选时要求
        ≥JUMP_MIN_SCORE 高分才接受位置跳变——等价 cursor_monitor 时代的
        "假光标跨帧对比"防拉飞机制（迁移时先验断线，此处接回）。
        """
        hist = []
        t0 = time.perf_counter()
        while len(hist) < n and time.perf_counter() - t0 < timeout:
            frame = self._frame()
            if frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB) if frame.shape[2] == 4 else frame
                targets, _top = detect_cursor(rgb)
                sel = select_cursor(targets, self.last_pos, self.miss_streak)
                # 候选快照（PEEP 诊断）：分数降序前 8 个（含拒识低分候选）+ 选中下标。
                # tuple 整体替换（snapshot publication：生产线程只整体替换引用）
                self.last_cands = tuple((t.pos[0], t.pos[1], t.score, t.state)
                                        for t in targets[:8])
                self.last_cand_sel = next(
                    (i for i, t in enumerate(targets[:8]) if t is sel), None)
                self.last_cands_ts = time.monotonic()
                if sel is not None:
                    hist.append(sel.pos)
                    continue
            time.sleep(0.02)
        if not hist:
            self.miss_streak += 1  # 连续失踪计数：≥MISS_STREAK_RESET 后先验失效重信任最高分
            return None
        hist.sort()
        pos = hist[len(hist) // 2]
        self.last_pos = pos
        self.last_pos_ts = time.monotonic()
        self.miss_streak = 0
        return pos

    # ---------- 摇杆 ----------

    def _speed(self, mag: int) -> float:
        return self.k * mag if mag >= self.deadzone else 0.0

    def set_stick(self, mag: int, dir_xy: tuple):
        dx, dy = dir_xy
        self._gpad.left_joystick(x_value=int(round(dx * mag)),
                                 y_value=int(round(-dy * mag)))
        gpad_update(self._gpad)

    def stick_zero(self):
        self._gpad.left_joystick(x_value=0, y_value=0)
        gpad_update(self._gpad)

    @property
    def confirm_button(self):
        """当前确认按钮对象；未注入时的默认值。"""
        return self._confirm_btn

    def set_confirm_button(self, button):
        """注入确认按钮对象（如 vg.XUSB_BUTTON.XUSB_GAMEPAD_A）。"""
        self._confirm_btn = button

    def press_confirm(self, button=None, duration: float = 0.15):
        """按确认按钮触发点击（后台点击的「确认」动作）。

        button 为手柄按钮对象（如 vg.XUSB_BUTTON.XUSB_GAMEPAD_A）；为 None 时默认用 A。
        """
        try:
            from maaracing_master.core.vgamepad_lazy import vg
            default_btn = vg.XUSB_BUTTON.XUSB_GAMEPAD_A
        except Exception:  # noqa: BLE001 —— 无手柄驱动时确认按钮不可用
            default_btn = None
        btn = button if button is not None else (self._confirm_btn or default_btn)
        if btn is None:
            return False
        self._gpad.press_button(btn)
        gpad_update(self._gpad)
        time.sleep(duration)
        self._gpad.release_button(btn)
        gpad_update(self._gpad)
        return True

    def _aborted_evt(self, abort_event: threading.Event | None) -> bool:
        """中止检查（导航各循环每 tick 调用，导航线程内执行）。

        abort_event 置位（cancel/shutdown）时立即摇杆归中，返回 True 表示应中止。
        背景：导航已线程化，但「停止」必须仍即时生效——模块停止走
        shutdown/cancel 置位 Event，worker 每步检查后快速退出，不拖满导航自然结束。
        """
        if abort_event is not None and abort_event.is_set():
            self.stick_zero()
            return True
        return False

    # ---------- 趋近 ----------

    def _phase_p(self, target, abort_event: threading.Event | None = None,
                 tol_px: float | None = None) -> dict:
        n_frames = 0
        miss = 0
        mag = 0
        last_pos = None
        overshoot_flip = 0
        while n_frames < MAX_P_STEPS:
            if self._aborted_evt(abort_event):
                return {"p_frames": n_frames, "osc_flip": overshoot_flip, "aborted": True}
            pos = self.read_pos(1, timeout=0.2)
            if pos is None:
                # 连续丢失计入独立上限（不占 n_frames）：超限快速失败返回 lost，
                # 由 approach → 结果槽 → 主循环 consume → 指纹不更新 → 下帧重试。
                # 无上限的 continue 会在光标持续不可见时无限空转（重试机制空洞）。
                miss += 1
                self._publish_progress(stage="p", pos=None, target=target)
                if miss > MAX_P_MISS:
                    # lost 路径不经过循环末尾的 stick_zero：先归零摇杆再快速
                    # 失败，否则残余杆量会让光标持续漂飞（自愈重建雪上加霜）
                    self.stick_zero()
                    return {"p_frames": n_frames, "osc_flip": overshoot_flip, "lost": True}
                time.sleep(0.02)
                continue
            miss = 0
            n_frames += 1
            dx, dy = target[0] - pos[0], target[1] - pos[1]
            dist = math.hypot(dx, dy)
            self._publish_progress(stage="p", pos=pos, target=target, dist=dist)
            cur_speed = self._speed(mag)
            stop_dist = cur_speed * LAG_S + STOP_MARGIN
            if tol_px is not None:
                # 已进入按 A 容差（目标框中心 70% 区）→ 提前停止趋近，交 micro
                # 即刻按 A。意图目标仍是按钮正中心，此处只是"足够近就不再挪"。
                stop_dist = max(stop_dist, tol_px)
            if dist <= stop_dist:
                break
            if last_pos is not None:
                d_before = math.hypot(last_pos[0] - target[0], last_pos[1] - target[1])
                if d_before < dist:
                    overshoot_flip += 1
            last_pos = pos
            v_des = KP * dist
            mag = int(max(self.deadzone, min(MAX_AXIS, v_des / self.k))) \
                if self.k else 0
            if dist < 1e-6:
                break
            self.set_stick(mag, (dx / dist, dy / dist))
            time.sleep(0.033)
        self.stick_zero()
        return {"p_frames": n_frames, "osc_flip": overshoot_flip}

    def _phase_micro(self, target, abort_event: threading.Event | None = None,
                     tol_px: float | None = None) -> dict:
        # 按 A 容差：调用方提供 tol_px（目标框中心 70% 区域半径）时放宽，
        # 缺省用 TOL（中心 5px 精确微调）
        tol = tol_px if tol_px is not None else TOL
        time.sleep(0.1)
        micro_steps = 0
        miss = 0
        while micro_steps < MAX_MICRO_STEPS:
            if self._aborted_evt(abort_event):
                return {"micro_steps": micro_steps, "err": None, "ok": False, "aborted": True}
            pos = self.read_pos(3, timeout=0.5)
            if pos is None:
                # 连续丢失独立计数（不占 micro_steps）：超限快速失败交外层重试，
                # 防止光标持续不可见（如按 A 后面板动画期）时无限空转
                miss += 1
                self._publish_progress(stage="micro", pos=None, target=target)
                if miss > MAX_MICRO_MISS:
                    return {"micro_steps": micro_steps, "err": None, "ok": False, "lost": True}
                time.sleep(0.02)
                continue
            miss = 0
            dx, dy = target[0] - pos[0], target[1] - pos[1]
            dist = math.hypot(dx, dy)
            self._publish_progress(stage="micro", pos=pos, target=target, dist=dist)
            if dist <= tol:
                return {"micro_steps": micro_steps, "err": dist, "ok": True}
            if abs(dx) < 2.0:
                ux, uy = 0.0, (1.0 if dy > 0 else -1.0)
            elif abs(dy) < 2.0:
                ux, uy = (1.0 if dx > 0 else -1.0), 0.0
            else:
                ux, uy = dx / dist, dy / dist
            # 距离-力度挡位：远挡大步减少逼近次数；单步位移上限 = 剩余距离 60%
            # （mag ≤ 0.6·dist / (k·有效时长)），速度模型偏差下也不会来回振荡；
            # 下限 clamp 到最小有效杆量（低于死区推不动）。
            gear_mag = max(m for th, m in MICRO_GEAR_TABLE if dist >= th)
            cap = int(0.6 * dist / (self.k * MICRO_EFFECTIVE_S)) if self.k else gear_mag
            step_mag = max(MICRO_MAG, min(gear_mag, cap))
            self.set_stick(step_mag, (ux, uy))
            time.sleep(MICRO_T)
            self.stick_zero()
            time.sleep(0.06)
            micro_steps += 1
        pos = self.read_pos(3)
        err = math.hypot(target[0] - pos[0], target[1] - pos[1]) if pos else float("nan")
        return {"micro_steps": micro_steps, "err": err, "ok": False}

    def _approach_sync(self, task: dict) -> dict:
        """导航闭环执行体（**导航线程内运行**）。

        task：submit 时的任务字典 {type, target, intent, tol_px, abort_event}。
        同步跑完 P 趋近 + 微调 +（非意图）按 A；进度经 _publish_progress 发布，
        中止经 abort_event（cancel/shutdown 置位）即时生效。
        光标丢失（lost）快速失败返回 device_lost=True —— 由主循环 consume 后
        累计并触发设备重建（worker 已回到等待态，重建不冲突）。
        """
        target = tuple(task["target"])
        intent = bool(task.get("intent"))
        tol_px = task.get("tol_px")
        abort_event = task.get("abort_event")
        if self._aborted_evt(abort_event):
            return {"type": task.get("type"), "target": list(target),
                    "ok": False, "reason": "aborted"}
        t0 = time.perf_counter()
        self._publish_progress(stage="start", pos=self.last_pos, target=target)
        p = self._phase_p(target, abort_event, tol_px=tol_px)
        if p.get("aborted"):
            self._publish_progress(stage="abort", pos=None, target=target, done=True)
            return {"type": task.get("type"), "target": list(target), **p,
                    "ok": False, "reason": "aborted"}
        if p.get("lost"):
            # 光标持续不可见：快速失败 → 主循环 consume 后累计丢失 + 重建设备
            self._publish_progress(stage="lost", pos=None, target=target, done=True)
            return {"type": task.get("type"), "target": list(target), **p,
                    "ok": False, "reason": "光标丢失", "device_lost": True}
        m = self._phase_micro(target, abort_event, tol_px=tol_px)
        if m.get("aborted"):
            self._publish_progress(stage="abort", pos=None, target=target, done=True)
            return {"type": task.get("type"), "target": list(target), **m,
                    "ok": False, "reason": "aborted"}
        if m.get("lost"):
            self._publish_progress(stage="lost", pos=None, target=target, done=True)
            return {"type": task.get("type"), "target": list(target), **m,
                    "ok": False, "reason": "光标丢失", "device_lost": True}
        dt = time.perf_counter() - t0
        ok = m.get("ok", False)
        self._publish_progress(stage="done", pos=None, target=target, ok=ok)
        if ok and not intent:
            self.press_confirm()
        return {"type": task.get("type"), "target": list(target), **p, **m,
                "total_s": round(dt, 2), "ok": ok}

    def approach(self, target: tuple, intent: bool = False,
                 on_progress: Callable | None = None,
                 should_abort: Callable | None = None,
                 tol_px: float | None = None) -> dict:
        """同步兼容壳（已弃用）：submit + 自旋 consume_result，语义与旧 approach 一致。

        仅供外部同步调用方（本项目已改 submit/consume 异步协议）。
        内部走导航线程执行，on_progress/should_abort 参数已不生效——进度读
        nav_progress()，中止走 cancel()。
        """
        if not self.submit(target, intent=intent, tol_px=tol_px, task_type="click"):
            return {"ok": False, "reason": "busy"}
        if should_abort is not None and should_abort():
            self.cancel()
        while True:
            res = self.consume_result()
            if res is not None:
                return res
            if should_abort is not None and should_abort():
                self.cancel()
            time.sleep(0.02)


def gpad_update(gpad):
    """手柄 update 兼容包装（XInput 物理手柄无 update 方法时静默跳过）。"""
    upd = getattr(gpad, "update", None)
    if upd is not None:
        upd()