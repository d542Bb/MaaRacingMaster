# -*- coding: utf-8 -*-
"""S1 参考系影子：模型预测 + 新鲜观测一次性限速校正 + 线一致性桥接。

**只写 trace，不接控制。** 规则来自方案 §12.2 的观测规则：历史不是测量，
单拍跳变不是运动，校正有速率上限。具体：

- ``x`` 是参考系里的自车横位（车道单位，右正，与 road_offset 同系）。
  首条新鲜观测形成参考系；``x`` 之前纯开环积分（``planner.step``，与生产
  控制同一运动学），之后每拍先预测、再用新鲜观测校正。
- 只有新鲜 ``wall`` / ``region`` / ``pair`` 观测可改绝对位；``memory`` /
  ``wall_occl`` / ``pair_slot`` 不冒充新测量，只作**慢锚**：按 ``hold_alpha``
  限速靠拢，防止模型在长遮挡段漂走，绝不阶跃写入。
- 新息 ≤ ``jump_tol_lane`` 时按 ``fresh_alpha`` 做一次限速校正；超过阈值
  视为参考系跳变，保持模型预测，等连续 ``confirm_n`` 次同向一致后才建立
  重锚目标，并按 ``max_corr_lane_s`` 靠过去——不阶跃写入。
- 复用拍（``new=False``）不是新证据，只能由模型继续预测。
- **线一致性桥接**：线观测不直接当绝对锚。只有连续 ``confirm_n`` 次与新鲜
  深度一致（差 ≤ ``jump_tol_lane``）、自身单拍不跳、已锁线且证据龄
  ≤ ``LANE_AGE_MAX_MS``，才允许在深度空窗里按 ``hold_alpha`` 慢锚；
  与新鲜深度不一致、线自己跳、或超过 ``LANE_TRUST_TTL_S`` 未再确认，
  立即撤销信任，回到保持/模型。实拍反例：新鲜墙体 0.30 道、线报 −1.53 道，
  盲接线会把参考系拉错。
- **航向慢状态**：车道线角按 ``theta_alpha`` 限速更新，减掉
  ``theta_curve_tau_s`` 慢基线（弯道里线拟合的常值弦角进基线，不驱动横速），
  按 ``speed_proxy_mps``（米制代理，不是新真源）换算横向速度，再以
  ``theta_gain`` 缓慢修正模型横速；``theta_ttl_s`` 内没有新线角就停止修正。
  只写 trace，不进控制。
"""
from __future__ import annotations

import math

from maaracing_master.plugins.speedrush.config import (
    LateralFrame as LateralFrameConfig, Planner)
from maaracing_master.plugins.speedrush.lane_obs import LANE_W
from maaracing_master.plugins.speedrush.planner import step

# 绝对位证据 vs 历史保持：两套语义分开，消费点不各自解释来源字符串。
FRESH_SOURCES = frozenset({"wall", "region", "pair"})
HOLD_SOURCES = frozenset({"memory", "wall_occl", "pair_slot"})
# 线桥接的卫生条件：证据龄、信任有效期、单次最多允许拉动的横位。
LANE_AGE_MAX_MS = 250.0
LANE_TRUST_TTL_S = 2.5
LANE_PULL_MAX_LANE = 1.0
# 远场宏观锚：慢滤波 + 对新鲜近场的慢偏差校准 + 跳变丢弃 + 信任期。
FAR_ALPHA = 0.15
FAR_BIAS_ALPHA = 0.03
FAR_BIAS_MAX_LANE = 1.5
FAR_JUMP_TOL_LANE = 0.5
FAR_TRUST_TTL_S = 1.5
FAR_PULL_MAX_LANE = 1.0


class LateralFrameShadow:
    """参考系状态机（纯逻辑，无线程/无 IO，便于离线单测）。"""

    def __init__(self, cfg: LateralFrameConfig, p: Planner) -> None:
        self.cfg = cfg
        self.p = p
        self.x: float | None = None
        self.x_model: float | None = None
        self.v = 0.0
        self.steer = 0.0
        self.target: float | None = None
        self.cand: float | None = None
        self.cand_dir = 0
        self.cand_n = 0
        self.r: float | None = None
        self.ev = "idle"
        self.src: str | None = None
        self.n_fresh = 0
        self.n_accept = 0
        self.n_hold = 0
        self.n_jump = 0
        self.n_reanchor = 0
        self.n_model_only = 0
        self.n_init_reject = 0
        # 线一致性桥接状态
        self.lane_ok = False
        self.lane_age_s = 1e9
        self.lane_agree_n = 0
        self.lane_diff: float | None = None
        self.lane_jump = False
        self._lane_prev: float | None = None
        self.n_lane = 0
        self.n_lane_skip = 0
        # 航向慢状态（相对车道线，rad；右正）。theta_ok=证据仍在信任期内。
        # theta_base 是 2 s 级慢基线：弯道里线拟合的常值弦角进基线，不驱动横速。
        self.theta = 0.0
        self.theta_base = 0.0
        self.theta_age_s = 1e9
        self.theta_ok = False
        self.v_theta = 0.0
        self.n_theta = 0
        # 远场宏观锚状态
        self.far_lane: float | None = None
        self.macro: float | None = None
        self.far_bias: float | None = None
        self.macro_age_s = 1e9
        self.far_jump = False
        self._far_prev: float | None = None
        self.n_macro = 0
        self.n_macro_skip = 0

    def update(self, dt: float, steer_raw: float, steer_norm: float,
               road_offset: float | None, source: str | None,
               new: bool, lane_x: float | None = None,
               lane_state: str | None = None, lane_n_locked: int = 0,
               lane_age_ms: float | None = None,
               far_lane: float | None = None,
               far_new: bool = False,
               lane_theta: float | None = None) -> dict:
        """喂一拍；返回本拍快照。参数非法只标记事件，不抛进控制链。"""
        self.ev = "model"
        self.src = source
        if not (isinstance(dt, (int, float)) and math.isfinite(dt) and dt > 0):
            self.ev = "bad_dt"
            return self.snapshot()

        # 线自身跳变：单拍超阈值立即撤销信任；随后按证据龄累计。
        self.lane_jump = (lane_x is not None and self._lane_prev is not None
                          and abs(float(lane_x) - self._lane_prev)
                          > self.cfg.jump_tol_lane)
        if lane_x is not None:
            self._lane_prev = float(lane_x)
        if self.lane_jump:
            self.lane_ok = False
            self.lane_agree_n = 0
            self.n_lane_skip += 1
        self.lane_age_s = min(self.lane_age_s + dt, 1e9)
        if self.lane_age_s > LANE_TRUST_TTL_S:
            self.lane_ok = False

        fresh = (bool(new) and source in FRESH_SOURCES
                 and road_offset is not None
                 and math.isfinite(float(road_offset)))
        self.macro_age_s = min(self.macro_age_s + dt, 1e9)
        self._far_update(far_lane, bool(far_new), fresh,
                         float(road_offset) if fresh else None)
        lane_usable = self._lane_usable(lane_x, lane_state, lane_n_locked,
                                        lane_age_ms)
        self._theta_update(dt, lane_theta, lane_state, lane_n_locked,
                           lane_age_ms)
        if self.x is None:
            if fresh:
                obs = float(road_offset)
                if abs(obs) <= self.p.anchor_max_off:
                    self.x = self.x_model = obs
                    self.v = 0.0
                    self.steer = float(steer_norm)
                    self.ev = "init"
                    self.n_fresh += 1
                    self._lane_agree(obs, lane_usable, lane_x)
                else:
                    self.n_init_reject += 1
                    self.ev = "init_hold"
            elif source in HOLD_SOURCES:
                self.n_hold += 1
                self.ev = "hold"
            else:
                self.n_model_only += 1
            return self.snapshot()

        # ① 模型预测（与生产 planner 同一 step 真源）+ 航向慢修正
        self.x, self.v, self.steer = step(
            self.x, self.v, self.steer, float(steer_raw), dt, self.p)
        self._apply_heading(dt)
        self.x_model = self.x

        # ② 新鲜观测：一次校正 / 跳变候选 / 重锚目标更新；顺带校准线信任
        if fresh:
            obs = float(road_offset)
            self.r = obs - self.x
            self.n_fresh += 1
            self._lane_agree(obs, lane_usable, lane_x)
            if self.target is not None:
                if abs(obs - self.target) <= self.cfg.jump_tol_lane:
                    self.target = obs          # 重锚中：新证据连续一致，跟随
                    self.ev = "reanchor"
                else:
                    self.target = None         # 重锚目标被推翻，按新证据重判
            if self.target is None:
                if abs(self.r) <= self.cfg.jump_tol_lane:
                    self.x = self._shift(self.x, self.cfg.fresh_alpha * self.r,
                                         dt)
                    self.n_accept += 1
                    self.ev = "accept"
                    self.cand = None
                    self.cand_n = 0
                else:
                    self.n_jump += 1
                    self.ev = "jump"
                    self._candidate(obs, self.r)
        elif self._lane_can_pull(lane_usable, lane_x):
            r = float(lane_x) - self.x
            if abs(r) <= LANE_PULL_MAX_LANE:
                self.r = r
                self.x = self._shift(self.x, self.cfg.hold_alpha * r, dt)
                self.n_lane += 1
                self.ev = "lane"
            else:
                self.n_lane_skip += 1
                if self._hold_anchor(dt, road_offset):
                    self.ev = "hold"
                else:
                    self.n_model_only += 1
        elif self._macro_can_pull():
            target = self.macro - (0.0 if self.far_bias is None
                                   else self.far_bias)
            r = target - self.x
            if abs(r) <= FAR_PULL_MAX_LANE:
                self.r = r
                self.x = self._shift(self.x, self.cfg.hold_alpha * r, dt)
                self.n_macro += 1
                self.ev = "macro"
            else:
                self.n_macro_skip += 1
                if self._hold_anchor(dt, road_offset):
                    self.ev = "hold"
                else:
                    self.n_model_only += 1
        elif source in HOLD_SOURCES:
            self.n_hold += 1
            self.ev = "hold"
            self._hold_anchor(dt, road_offset)
        else:
            self.n_model_only += 1

        # ③ 已确认的重锚：按限速靠拢，不阶跃
        if self.target is not None:
            d = self.target - self.x
            cap = self.cfg.max_corr_lane_s * dt
            if abs(d) <= cap:
                self.x = self.target
                self.target = None
            else:
                self.x += math.copysign(cap, d)
            if self.ev == "model":
                self.ev = "reanchor"
        return self.snapshot()

    def _shift(self, x: float, dx: float, dt: float) -> float:
        cap = self.cfg.max_corr_lane_s * dt
        return x + max(-cap, min(cap, dx))

    def _theta_update(self, dt: float, lane_theta: float | None,
                      state: str | None, n_locked: int,
                      age_ms: float | None) -> None:
        """航向慢状态：线角限速更新，换算成横向速度（米制代理）。"""
        if not self.cfg.theta_enabled:
            return
        self.theta_age_s = min(self.theta_age_s + dt, 1e9)
        usable = (lane_theta is not None and state == "TRACK"
                  and int(n_locked or 0) > 0
                  and age_ms is not None
                  and float(age_ms) <= LANE_AGE_MAX_MS)
        if usable:
            obs = float(lane_theta)
            if math.isfinite(obs) and abs(obs) <= 0.6:
                if self.n_theta == 0:
                    self.theta = self.theta_base = obs
                else:
                    d = self.cfg.theta_alpha * (obs - self.theta)
                    cap = self.cfg.theta_max_rate_rad_s * dt
                    self.theta += max(-cap, min(cap, d))
                    alpha = 1.0 - math.exp(
                        -dt / max(1e-3, self.cfg.theta_curve_tau_s))
                    self.theta_base += alpha * (self.theta - self.theta_base)
                self.n_theta += 1
                self.theta_age_s = 0.0
        self.theta_ok = (self.theta_age_s <= self.cfg.theta_ttl_s
                         and self.n_theta > 0)
        rel = max(-0.6, min(0.6, self.theta - self.theta_base))
        self.v_theta = (self.cfg.speed_proxy_mps * math.tan(rel) / LANE_W
                        if self.theta_ok else 0.0)

    def _apply_heading(self, dt: float) -> None:
        """把航向换算的横速缓慢并入模型横速；不替换 steer 模型，只做慢修正。"""
        if not (self.cfg.theta_enabled and self.theta_ok
                and self.cfg.theta_gain > 0.0):
            return
        v_t = max(-self.p.v_lat_max, min(self.p.v_lat_max, self.v_theta))
        v_new = self.v + self.cfg.theta_gain * (v_t - self.v)
        self.x += (v_new - self.v) * dt
        self.v = v_new

    def _lane_usable(self, lane_x, state, n_locked,
                     age_ms) -> bool:
        return (lane_x is not None and state == "TRACK"
                and int(n_locked or 0) > 0
                and age_ms is not None and float(age_ms) <= LANE_AGE_MAX_MS
                and not self.lane_jump)

    def _lane_agree(self, obs: float, usable: bool,
                    lane_x: float | None) -> None:
        """新鲜深度拍：线与其一致才积累信任；不一致立即撤销。"""
        if not usable or lane_x is None:
            return
        diff = abs(float(lane_x) - obs)
        self.lane_diff = diff
        if diff <= self.cfg.jump_tol_lane:
            self.lane_agree_n += 1
            if self.lane_agree_n >= self.cfg.confirm_n:
                self.lane_ok = True
                self.lane_age_s = 0.0
        else:
            self.lane_ok = False
            self.lane_agree_n = 0
            self.n_lane_skip += 1

    def _lane_can_pull(self, usable: bool, lane_x: float | None) -> bool:
        return (usable and self.lane_ok
                and self.lane_age_s <= LANE_TRUST_TTL_S
                and self.cfg.hold_alpha > 0.0
                and self.target is None and lane_x is not None)

    def _far_update(self, far_lane: float | None, far_new: bool,
                    fresh: bool, obs: float | None) -> None:
        """远场慢滤波与偏差校准；跳变时不更新，等下一组稳定远带。"""
        if not self.cfg.macro_enabled or far_lane is None:
            return
        self.far_lane = float(far_lane)
        self.far_jump = (self._far_prev is not None
                         and abs(self.far_lane - self._far_prev)
                         > FAR_JUMP_TOL_LANE)
        if far_new:
            if self.far_jump:
                # 远带跳变=参考系可能换了结构；旧宏观锚作废，等稳定远带。
                self.n_macro_skip += 1
                self.macro = None
                self.macro_age_s = 1e9
            elif self.macro is None:
                self.macro = self.far_lane
                self.macro_age_s = 0.0
            else:
                self.macro += FAR_ALPHA * (self.far_lane - self.macro)
                self.macro_age_s = 0.0
        self._far_prev = self.far_lane
        if fresh and obs is not None and not self.far_jump:
            b = self.far_lane - float(obs)
            if abs(b) <= FAR_BIAS_MAX_LANE:
                if self.far_bias is None:
                    self.far_bias = b
                else:
                    self.far_bias += FAR_BIAS_ALPHA * (b - self.far_bias)

    def _macro_can_pull(self) -> bool:
        return (self.cfg.macro_enabled and self.macro is not None
                and self.macro_age_s <= FAR_TRUST_TTL_S
                and self.cfg.hold_alpha > 0.0 and self.target is None)

    def _hold_anchor(self, dt: float, road_offset: float | None) -> bool:
        if (self.target is not None or road_offset is None
                or not math.isfinite(float(road_offset))
                or self.cfg.hold_alpha <= 0.0):
            return False
        self.r = float(road_offset) - self.x
        self.x = self._shift(self.x, self.cfg.hold_alpha * self.r, dt)
        return True

    def _candidate(self, obs: float, r: float) -> None:
        d = 1 if r > 0 else -1
        if (self.cand is not None and self.cand_dir == d
                and abs(obs - self.cand) <= self.cfg.jump_tol_lane):
            self.cand_n += 1
        else:
            self.cand = obs
            self.cand_dir = d
            self.cand_n = 1
        if self.cand_n >= self.cfg.confirm_n:
            self.target = obs
            self.n_reanchor += 1
            self.ev = "reanchor"
            self.cand = None
            self.cand_n = 0

    def snapshot(self) -> dict:
        return {
            "x": None if self.x is None else round(self.x, 3),
            "x_model": None if self.x_model is None
            else round(self.x_model, 3),
            "v": round(self.v, 3),
            "steer": round(self.steer, 3),
            "target": None if self.target is None
            else round(self.target, 3),
            "r": None if self.r is None else round(self.r, 3),
            "ev": self.ev, "src": self.src,
            "cand_n": self.cand_n,
            "fresh": self.n_fresh, "accept": self.n_accept,
            "hold": self.n_hold, "jump": self.n_jump,
            "reanchor": self.n_reanchor, "model": self.n_model_only,
            "init_reject": self.n_init_reject,
            "lane_ok": self.lane_ok,
            "lane_age": round(min(self.lane_age_s, 99.0), 2),
            "lane_diff": None if self.lane_diff is None
            else round(self.lane_diff, 3),
            "lane_n": self.n_lane, "lane_skip": self.n_lane_skip,
            "lane_jump": self.lane_jump,
            "theta": round(self.theta, 4),
            "theta_rel": round(self.theta - self.theta_base, 4),
            "theta_age": round(min(self.theta_age_s, 99.0), 2),
            "theta_ok": bool(self.theta_ok),
            "v_theta": round(self.v_theta, 3),
            "theta_n": self.n_theta,
            "far": None if self.far_lane is None
            else round(self.far_lane, 3),
            "far_bias": None if self.far_bias is None
            else round(self.far_bias, 3),
            "macro": None if self.macro is None else round(self.macro, 3),
            "macro_age": round(min(self.macro_age_s, 99.0), 2),
            "macro_n": self.n_macro, "macro_skip": self.n_macro_skip,
            "far_jump": self.far_jump,
        }
