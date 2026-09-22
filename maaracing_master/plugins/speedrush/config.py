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
    # 超车模式总闸（阶段 C 设计稿 §七：V2 开关，默认关；与 allow_car_graze 语义分离——
    # 后者管"贴车是否算风险否决"，本闸管"超车是否进候选集"）
    allow_overtake: bool = False


@dataclass(frozen=True)
class Overtake:
    """超车候选评分参数（阶段 C 设计稿 §二/§四；[需实测] 项回放定档只改 json）。"""

    value_base: float        # 单次超车分（RULES §4.11 游戏事实镜像 30）
    value_limit: float       # 极限超车动作分（同上镜像 120）
    p_center_lane: float     # P_limit logistic 中心（~0.73，两点锚 0.65/0.81 的中点）[C4]
    p_scale_lane: float      # logistic 尺度（0.06：0.65→≈0.79，0.81→≈0.21 起步）[C4]
    d_hold_lane: float       # 贴邻保持窗中心（[0.55,0.70] 起值中点）——窗下沿是硬地板
    lane_band_lo: float      # 候选车横向带下沿（<此值=本车道车/正在掠过，不作候选）
    lane_band_hi: float      # 上沿（对向斜穿/杂框自卫）
    t_pass_max_s: float      # 超车计划兜底超时（pass 事件迟迟不落定 → 强制 done）[C5]
    space_margin_lane: float  # 空间闸门余量：该侧缘距 < d_hold+此值 才判"没空间"禁用该向
                              # （决策默认左右对称，禁用必须有显式证据——维护者裁定）


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
class Planner:
    """运动规划/控制层参数（planner 设计稿 §六）。命名按"延迟补偿前瞻 PD"口径——
    黑箱物理下无车辆模型，不叫 Pure Pursuit 以免读者找曲率公式（设计稿 §三）。"""

    lookahead_tau_s: float     # 外推提前量 = τ_resp + 惯性时间常数（下界由矛盾检查锁）
    k_p: float                 # 车道误差 → 归一杆值 比例增益
    k_d: float                 # 横向速度阻尼增益（≥0）
    tau_steer_s: float         # 杆值低通时间基常数（α=1−exp(−dt/τ)，行为稿 §五纪律）
    rate_limit_raw: float      # 每 tick 杆值变化上限（±32767 系）
    stick_deadzone_raw: float  # |杆值| 低于此归 0（游戏手柄死区）
    throttle_raw: int          # 右扳机恒值（RULES §4.1 权威：全程油门）
    # —— 双积分横向运动学（v2，2026-09-22 真机证据链：杆是航向指令不是平移速度指令，
    #    满杆按住 x∝t²；单积分 K_v 口径的 v_lat_gain/inertia_tau_s 已证伪删除）——
    a_lat_gain: float          # 杆→横向加速度（车道/s²），起步段恒加速 [需实测 C1]
    tau_align_s: float         # 松杆后横向速度自回正衰减时间常数（航向回正）[需实测 C2]
    v_lat_max: float           # 横向漂移速度上限（车道/s）：最大车头角的定圆饱和 [需实测 C3]
    hold_max_ticks: int        # 决策过期后最多保持拍数，第 +1 拍起按 CONSERVE
    # —— 路中心连续重锚（step 2.5，2026-09-22：开环虚胖的闭环解）——
    obs_alpha: float           # 位置修正增益（alpha-beta 滤波），有路缘观测拍生效 [需实测]
    obs_beta: float            # 速度修正增益：r/dt 注入 v_lat，治"模型自说自话收敛" [需实测]
    obs_jump_max_lane: float   # 新息门：|观测−预测| 超此值判坏检测，本拍弃观测


@dataclass(frozen=True)
class Traffic:
    """车流观测层参数（阶段 C 设计稿 §三；起值 [需实测 C5]，回放定档只改 json）。"""

    exit_margin_px: float      # 下带判据：last cy ≥ v_ego − 此值 → 从车尾侧消失
    min_obs_ticks: int         # 观测次数不足不发事件（检测噪声自卫）
    ghost_max_age_ticks: int   # 超龄且低速的"底边消失"判 ghost 不判 pass（191 帧案）
    ghost_rel_eps: float       # "低速"的相对速率界（px/tick）


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
    planner: Planner
    overtake: Overtake
    traffic: Traffic


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


def _bool(d: dict, sec: str, key: str, default: bool | None = None) -> bool:
    v = d.get(sec, {}).get(key, default)
    if v is None:
        raise ValueError(f"decision.json [{sec}].{key} 缺失")
    if not isinstance(v, bool):
        raise ValueError(f"decision.json [{sec}].{key} 非布尔：{v!r}")
    return v


def _int(d: dict, sec: str, key: str, *, lo: int, hi: int | None = None) -> int:
    v = d.get(sec, {}).get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"decision.json [{sec}].{key} 缺失或非整数：{v!r}")
    if v < lo or (hi is not None and v > hi):
        raise ValueError(f"decision.json [{sec}].{key}={v} 越界（需 ∈[{lo},{hi}]）")
    return v


def _read_decision(path: Path) -> DecisionConfig:
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"decision.json schema_version={d.get('schema_version')!r}，"
            f"代码只认 {SCHEMA_VERSION}")
    for sec in ("mode", "control", "timing", "scoring", "hysteresis", "validate",
                "planner"):
        if not isinstance(d.get(sec), dict):
            raise ValueError(f"decision.json 缺段「{sec}」")

    cfg = DecisionConfig(
        mode=Mode(
            allow_all_moves=_bool(d, "mode", "allow_all_moves"),
            allow_car_graze=_bool(d, "mode", "allow_car_graze"),
            # allow_overtake 缺省 False：老 json 不带键也安全（超车候选默认不进集）
            allow_overtake=_bool(d, "mode", "allow_overtake", default=False)),
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
                                       lo=0, lo_open=False)),
        planner=Planner(
            lookahead_tau_s=_num(d, "planner", "lookahead_tau_s", lo=0),
            k_p=_num(d, "planner", "k_p", lo=0),
            k_d=_num(d, "planner", "k_d", lo=0, lo_open=False),
            tau_steer_s=_num(d, "planner", "tau_steer_s", lo=0),
            rate_limit_raw=_num(d, "planner", "rate_limit_raw", lo=0),
            stick_deadzone_raw=_num(d, "planner", "stick_deadzone_raw",
                                    lo=0, lo_open=False),
            throttle_raw=_int(d, "planner", "throttle_raw", lo=0, hi=255),
            a_lat_gain=_num(d, "planner", "a_lat_gain", lo=0),
            tau_align_s=_num(d, "planner", "tau_align_s", lo=0, lo_open=False),
            v_lat_max=_num(d, "planner", "v_lat_max", lo=0, lo_open=False),
            hold_max_ticks=_int(d, "planner", "hold_max_ticks", lo=1),
            obs_alpha=_num(d, "planner", "obs_alpha", lo=0, hi=1),
            obs_beta=_num(d, "planner", "obs_beta", lo=0, hi=1),
            obs_jump_max_lane=_num(d, "planner", "obs_jump_max_lane", lo=0,
                                   lo_open=False)),
        overtake=Overtake(
            value_base=_num(d, "overtake", "value_base", lo=0),
            value_limit=_num(d, "overtake", "value_limit", lo=0),
            p_center_lane=_num(d, "overtake", "p_center_lane", lo=0),
            p_scale_lane=_num(d, "overtake", "p_scale_lane", lo=0, lo_open=False),
            d_hold_lane=_num(d, "overtake", "d_hold_lane", lo=0, lo_open=False),
            lane_band_lo=_num(d, "overtake", "lane_band_lo", lo=0),
            lane_band_hi=_num(d, "overtake", "lane_band_hi", lo=0),
            t_pass_max_s=_num(d, "overtake", "t_pass_max_s", lo=0, lo_open=False),
            space_margin_lane=_num(d, "overtake", "space_margin_lane", lo=0)),
        traffic=Traffic(
            exit_margin_px=_num(d, "traffic", "exit_margin_px", lo=0),
            min_obs_ticks=_int(d, "traffic", "min_obs_ticks", lo=1),
            ghost_max_age_ticks=_int(d, "traffic", "ghost_max_age_ticks", lo=2),
            ghost_rel_eps=_num(d, "traffic", "ghost_rel_eps", lo=0)))

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
    p, t = cfg.planner, cfg.timing
    if p.lookahead_tau_s < t.tau_resp_s:
        raise ValueError(
            f"[planner] lookahead_tau_s={p.lookahead_tau_s} 低于实测响应延迟 "
            f"tau_resp_s={t.tau_resp_s}（提前量无视延迟，planner 设计稿 §三）")
    full_stroke_ticks = 2 * 32767 / p.rate_limit_raw if p.rate_limit_raw > 0 else 0
    if full_stroke_ticks < 2:
        raise ValueError(
            f"[planner] rate_limit_raw={p.rate_limit_raw} 满行程仅 "
            f"{full_stroke_ticks:.1f} tick（<2 tick 限幅形同虚设，是失效档位）")
    ov = cfg.overtake
    if not ov.value_base < ov.value_limit:
        raise ValueError(
            f"[overtake] value_base={ov.value_base} 应 < value_limit={ov.value_limit}"
            "（极限超车是超车的进阶类型，RULES §4.2——镜像倒置即改错）")
    if ov.d_hold_lane > ov.lane_band_lo or ov.d_hold_lane >= ov.lane_band_hi:
        raise ValueError(
            f"[overtake] 贴窗中心 {ov.d_hold_lane} 不得超出候选带下沿 "
            f"{ov.lane_band_lo}（带缘车的内贴目标会越过自车道中心，判据矛盾）")
    return cfg


@lru_cache(maxsize=1)
def load_decision() -> DecisionConfig:
    """读行为参数真源（进程内一次；改文件需重启或 clear cache——回放/实机都是
    开局读取，无热更需求，设计稿 §五）。"""
    return _read_decision(DECISION_FILE)
