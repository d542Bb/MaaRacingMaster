# -*- coding: utf-8 -*-
"""行为参数真源的读取（设计稿 v2 §五/§六；step 4）。

三源分立中的**行为参数**入口：``resources/policy/decision.json``（游戏事实归 RULES.md、
几何标定归 world_model.load_calib，本模块不碰那两源）。加载 **fail-loud**：缺键、
类型错、数值越界、依赖矛盾（如恢复窗必须短于升级窗）一律拒载并写明原因——
错误配置静默进 FSM 比不开决策层危险得多。

起值的证据注记随文件 ``_sources`` 走（真源里注释，代码不抄第二份说明）。
读侧缓存：lru_cache 一次读盘；测试经 ``_read_decision`` 直接喂临时文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from maaracing_master.plugins.speedrush import DECISION_FILE

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Mode:
    allow_all_moves: bool
    allow_car_graze: bool


@dataclass(frozen=True)
class Control:
    frame_rate_hz: float
    target_grace_s: float
    group_grace_s: float
    t_change_max_s: float


@dataclass(frozen=True)
class Timing:
    tau_resp_s: float
    lane_change_base_s: float
    lane_change_k_s_per_lane: float
    margin_s: float

    def lane_change_duration_s(self, dx_lane: float) -> float:
        """变道耗时模型（v1 线性起值，[需实测 §7.1] 后只改参数不换式）：
        起步开销 + 单位位移耗时。dx=0 时为 base——"原地收敛"仍要花时间，非零。"""
        return self.lane_change_base_s + self.lane_change_k_s_per_lane * abs(dx_lane)


@dataclass(frozen=True)
class Scoring:
    coin_value_per_unit: float
    conf_floor: float      # 置信折扣=0 的下界（低于此不给分）
    conf_hi: float         # 置信折扣=1 的下界（实测正常检测值区满权，§7.2/重测 conf 中位 0.87）
    shift_cost_per_lane: float
    life_tau_s: float


@dataclass(frozen=True)
class Hysteresis:
    switch_margin: float
    min_score: float
    t_cool_s: float
    tau_filter_s: float
    dead_zone_lane: float


@dataclass(frozen=True)
class Validate:
    t_empty_s: float
    t_recover_s: float
    t_conserve_max_s: float
    x_lane_abs_max: float
    cy_jump_max_px: float
    straight_residual_max: float


@dataclass(frozen=True)
class DecisionConfig:
    mode: Mode
    control: Control
    timing: Timing
    scoring: Scoring
    hysteresis: Hysteresis
    validate: Validate


def _num(d: dict, sec: str, key: str, *, lo: float | None = None,
         hi: float | None = None, lo_open: bool = True) -> float:
    v = d.get(sec, {}).get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"decision.json [{sec}].{key} 缺失或非数值：{v!r}")
    v = float(v)
    if lo is not None and (v < lo or (lo_open and v == lo)):
        raise ValueError(f"decision.json [{sec}].{key}={v} 越界（需 {'>' if lo_open else '≥'}{lo}）")
    if hi is not None and v > hi:
        raise ValueError(f"decision.json [{sec}].{key}={v} 越界（需 ≤{hi}）")
    return v


def _bool(d: dict, sec: str, key: str) -> bool:
    v = d.get(sec, {}).get(key)
    if not isinstance(v, bool):
        raise ValueError(f"decision.json [{sec}].{key} 缺失或非布尔：{v!r}")
    return v


def _read_decision(path: Path) -> DecisionConfig:
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"decision.json schema_version={d.get('schema_version')!r}，"
            f"代码只认 {SCHEMA_VERSION}")
    for sec in ("mode", "control", "timing", "scoring", "hysteresis", "validate"):
        if not isinstance(d.get(sec), dict):
            raise ValueError(f"decision.json 缺段「{sec}」")

    cfg = DecisionConfig(
        mode=Mode(
            allow_all_moves=_bool(d, "mode", "allow_all_moves"),
            allow_car_graze=_bool(d, "mode", "allow_car_graze")),
        control=Control(
            frame_rate_hz=_num(d, "control", "frame_rate_hz", lo=0),
            target_grace_s=_num(d, "control", "target_grace_s", lo=0),
            group_grace_s=_num(d, "control", "group_grace_s", lo=0),
            t_change_max_s=_num(d, "control", "t_change_max_s", lo=0)),
        timing=Timing(
            tau_resp_s=_num(d, "timing", "tau_resp_s", lo=0, lo_open=False),
            lane_change_base_s=_num(d, "timing", "lane_change_base_s", lo=0),
            lane_change_k_s_per_lane=_num(d, "timing", "lane_change_k_s_per_lane",
                                          lo=0, lo_open=False),
            margin_s=_num(d, "timing", "margin_s", lo=0, lo_open=False)),
        scoring=Scoring(
            coin_value_per_unit=_num(d, "scoring", "coin_value_per_unit", lo=0),
            conf_floor=_num(d, "scoring", "conf_floor", lo=0, hi=0.999),
            conf_hi=_num(d, "scoring", "conf_hi", lo=0, hi=0.999),
            shift_cost_per_lane=_num(d, "scoring", "shift_cost_per_lane",
                                     lo=0, lo_open=False),
            life_tau_s=_num(d, "scoring", "life_tau_s", lo=0)),
        hysteresis=Hysteresis(
            switch_margin=_num(d, "hysteresis", "switch_margin",
                               lo=0, lo_open=False),
            min_score=_num(d, "hysteresis", "min_score", lo=0),
            t_cool_s=_num(d, "hysteresis", "t_cool_s", lo=0, lo_open=False),
            tau_filter_s=_num(d, "hysteresis", "tau_filter_s", lo=0),
            dead_zone_lane=_num(d, "hysteresis", "dead_zone_lane", lo=0)),
        validate=Validate(
            t_empty_s=_num(d, "validate", "t_empty_s", lo=0),
            t_recover_s=_num(d, "validate", "t_recover_s", lo=0),
            t_conserve_max_s=_num(d, "validate", "t_conserve_max_s", lo=0),
            x_lane_abs_max=_num(d, "validate", "x_lane_abs_max", lo=0),
            cy_jump_max_px=_num(d, "validate", "cy_jump_max_px", lo=0),
            straight_residual_max=_num(d, "validate", "straight_residual_max",
                                       lo=0, lo_open=False)))

    # 段间依赖矛盾：单条范围过不了的联合错误，在这里拦
    v, h = cfg.validate, cfg.hysteresis
    if v.t_recover_s >= v.t_conserve_max_s:
        raise ValueError(
            f"[validate] t_recover_s={v.t_recover_s} 必须 < t_conserve_max_s="
            f"{v.t_conserve_max_s}（否则保守态永远先升级 FAULT，恢复通道死锁）")
    if h.dead_zone_lane >= v.x_lane_abs_max:
        raise ValueError(
            f"死区 {h.dead_zone_lane} ≥ 越界门 {v.x_lane_abs_max}（收敛与越界同时成立，判据矛盾）")
    if not cfg.scoring.conf_floor < cfg.scoring.conf_hi:
        raise ValueError(
            f"[scoring] conf_floor={cfg.scoring.conf_floor} 必须 < conf_hi="
            f"{cfg.scoring.conf_hi}（折扣区倒置）")
    if cfg.mode.allow_car_graze:
        raise ValueError(
            "mode.allow_car_graze=true 在 v1 拒绝启用（coin-only 保守基线；"
            "复议条件=近场 0.05 车道级精度实测，设计稿 §三）")
    return cfg


@lru_cache(maxsize=1)
def load_decision() -> DecisionConfig:
    """读行为参数真源（进程内一次；改文件需重启或 clear cache——回放/实机都是
    开局读取，无热更需求，设计稿 §五）。"""
    return _read_decision(DECISION_FILE)
