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
    allow_car_graze: bool


@dataclass(frozen=True)
class Selection:
    """四层选择层参数（2026-10-18 走廊重建；数值真源 decision.json selection）。

    横向量全为米制；带 `_lane` 的键在进选择层时 ×lane_w。``horizon_s`` 是
    短视界（含中间机会），``center_band_lane`` 是 L4 的不修正容差带。"""

    horizon_s: float = 3.0
    max_waypoints: int = 4
    graze_gap_lane: float = 0.65
    center_band_lane: float = 0.3
    opportunity_tol_lane: float = 0.75
    coin_pick_tol_lane: float = 1.2
    coin_hold_s: float = 0.6
    t_move_base_s: float = 0.3
    t_move_per_lane_s: float = 2.0
    reaction_s: float = 0.13
    margin_s: float = 0.15
    aligned_lane: float = 0.1
    reward_aligned_lane: float = 0.2
    wall_margin_lane: float = 0.35
    center_lookahead_m: float = 0.0
    preposition_enabled: bool = True
    preposition_horizon_s: float = 6.0
    preposition_max_lane: float = 2.5
    preposition_bias_lane: float = 1.0
    switch_dwell_s: float = 0.2
    reward_tol_lane: float = 1.5
    car_half_lane: float = 0.27
    ego_half_lane: float = 0.27


@dataclass(frozen=True)
class GridXcheck:
    """路心供数主人闸（2026-10-06 两局定案：找边锁「以车为中心的走廊」——
    骑黄线段 L=−1.32/R=+0.53 报"正中"，对宽 2.68 道像真路所以宽门/中值窗
    全失明；与栅格路心的分歧是系统性单边（Δ p50=+0.45、57% 超容差、符号
    几乎不翻），**否决闸治不了系统性偏差**（会否决 57% 新鲜对→黑视循环，
    veto 模式只为回放对比保留）。整段可选，老 json 不带段=全默认=off。

    mode 语义：
    - ``off``＝找边对当主人（旧行为逐位不变；栅格路心只进审计面）。
    - ``master``＝**绿区边界当主人**（depth_geo.region_reading：路=可行驶
      区域的物理边界，左缘/右缘/区宽一个语义出三个量；找边降为交叉验证
      审计 ro_edge 列）。绿区人行道因高出路平面被判 blocked，想跟着车跑
      都做不到——这正是找边走廊锁错的对症语义。区弃权拍走保鲜槽/诚实
      None（13:29 黑视纪律同前），**不回退找边**（双主人切换=0.8 道级
      ro 台阶）。
    - ``veto``＝旧否决语义（分歧超 tol_lane 拒供、bypass_streak 放行），
      保留作回放对比，生产不开。"""

    mode: str = "off"           # off | master | veto
    tol_lane: float = 0.45      # veto/审计容差（道）：|找边路心−栅格路心|
    bypass_streak: int = 8      # veto 模式连续分歧放行预算（≈2s，防黑视）


@dataclass(frozen=True)
class Control:
    frame_rate_hz: float
    t_change_max_s: float
    # ABORT 速率落稳界（车道/s）：executed 拍间变化率低于此判回稳——v1 注释里
    # "有反馈等横向速率归零信号"的补装（19:44 复盘：此前从未实现，丢目标白冻 2s）
    abort_settle_v_lane_s: float


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
    coin_group_min_count: float  # 组下限（RULES 2026-10-14 定案）：观测 1~2 枚是
                                 # 残缺视图，价值按整组 ≥3 枚计，不按枚数折算
    conf_floor: float      # 置信折扣=0 的下界（低于此不给分）
    conf_hi: float         # 置信折扣=1 的下界（实测正常检测值区满权，§7.2/重测 conf 中位 0.87）
    shift_cost_per_lane: float
    life_tau_s: float


@dataclass(frozen=True)
class Hysteresis:
    # 输出目标 slew（道/s，0=关）：决策输出每拍变化上限——选择目标单拍跳变
    # 不直接变成打舵指令（阶段 1a，2026-10-18）
    target_slew_lane_s: float = 0.0


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
    # —— 路锚卫生（2026-09-26 12:39/13:29 两局复盘：骑缘拍毒化参考系；陈旧帧+
    #    拍数门复位太慢=0 杆直开怼墙）——
    anchor_max_off: float      # 锚形成门：|road_offset| ≤ 此值才形成参考系 [需实测]
    anchor_stale_s: float      # 距上次被接受修正超此秒数（含断供）→ 陈旧重基到路上 [需实测]
    # —— 维护性转向权限（2026-09-26 16:44 局复盘：定中心全权 PD=满舵绕桩，
    #    257/267 饱和拍在保持态、速度起不来）——
    hold_deadband_lane: float  # 死区：|前瞻误差| 小于此不发维护力（车道单位）[需实测]
    hold_stick_max: float      # 维护性转向（定中心/保持/回稳）杆幅上限 [需实测]
    # ---- 机动态全权（阶段 2，2026-10-18：decision.maneuver 置位时）----
    maneuver_deadband_lane: float = 0.0   # 机动态死区（道，0=无死区）
    maneuver_stick_max: float = 0.45      # 机动态杆幅上限（0.45=与保持态相同）
    # ---- 路心观测速度注入抑制（阶段 2，2026-10-18）----
    obs_beta_deadband_lane: float = 0.0   # |r| 低于此只做位置修正（0=关）
    obs_beta_sign_agree: bool = False     # r 与上次被接受修正反号时不做速度修正
    obs_beta_dv_rate_lane_s2: float = 0.0  # β 注入的速度变化率上限（0=关）
    # 路心观测位置-only（2026-10-18 走廊重建）：true=只做 α 位置修正，不再
    # 注入 β 速度（速度交给双积分模型）；false=旧 α-β 逐位恢复（回滚）
    obs_position_only: bool = True


@dataclass(frozen=True)
class Traffic:
    """车流观测层参数（阶段 C 设计稿 §三；起值 [需实测 C5]，回放定档只改 json）。"""

    exit_margin_px: float      # 下带判据：last cy ≥ v_ego − 此值 → 从车尾侧消失
    presumed_cy_px: float      # 推定超越：真检贴身阈值（绝对画面纵坐标；检测下界
                               # ≈575 高于 636 判据线，慢速超越在死区内记 lost 的
                               # 修复判据，2026-10-08 取证预演定档，设计稿 §4A）
    presumed_lane_max: float   # 推定超越：邻道横向界（|x_lane|，道）
    min_obs_ticks: int         # 观测次数不足不发事件（检测噪声自卫）
    ghost_max_age_ticks: int   # 超龄且低速的"底边消失"判 ghost 不判 pass（191 帧案）
    ghost_rel_eps: float       # "低速"的相对速率界（px/fid，与 rel_approach 同单位）
    v_lat_ema_alpha: float     # 街车横向速率 EMA α（帧间差分平滑，车道/tick 口径；
                               # 遮挡延续拍不投毒——阶段一 CV 外推数据源）
    # —— 车道成员资格投票（2026-10-11 前置三件套②；起值 [需实测]，回放定档只改 json）——
    lane_vote_decay: float = 0.7    # 票仓逐真实读数拍的质量衰减系数：真横移连续
                                    # 新票数拍内压过衰减旧票（及时响应边界）
    lane_memb_min_weight: float = 1.5  # 成员位成立下限：最大票质量低于此=证据不足，
                                       # CarView.x_memb=None 消费方退回单拍读数
    lane_vote_sigma_max: float = 0.6   # 读数 σ 超此不投票（远场/弯道失真不进票仓，
                                       # 近场保守——σ 模型见 tracking docstring）
    # —— 宽度有效性（2026-10-11 前置三件套③；起值 [需实测]）——
    width_ttl_ticks: int = 6   # 宽度遮挡沿用有效期（tick）：过期显式转 unknown
                               # 不冒充有效；0.3s@20Hz 起值，遮挡崩坏 TTL 界
    # —— 决策边界的时间平滑（2026-10-19 晚：车米制 x/z 逐拍跳 ±1~4m，
    # —— 与 0.5m 擦车带同量级；在决策层平滑不碰跟踪层语义）——
    metric_x_ema_alpha: float = 0.35
    metric_z_ema_alpha: float = 0.5


@dataclass(frozen=True)
class Validate:
    t_empty_s: float
    t_recover_s: float
    t_conserve_max_s: float
    x_lane_abs_max: float
    cy_jump_max_px: float


@dataclass(frozen=True)
class Motion:
    """场景快照与轨迹评估参数（motion_plan 设计稿）。"""

    mode: str = "off"                 # off / shadow / live
    horizon_s: float = 2.0
    preview_s: float = 0.2
    dt_s: float = 0.05
    steer_levels: tuple[float, ...] = (
        -0.85, -0.55, -0.35, -0.2, -0.1, 0.0, 0.1, 0.2, 0.35, 0.55, 0.85)
    near_z_m: float = 6.0
    # 走廊约束只验前 1.0s：恒定杆 2s 必然驶出近场包络，硬验全程会把必要的
    # 避让判成不可行（影子实测 p2 8% 全灭假象）
    corridor_check_s: float = 1.0
    collision_margin_m: float = 0.2
    # 反应时内到站的车：横向动作已无法改变结果，不计入硬约束（防贴身会车
    # 全灭后误触 CONSERVE）
    collision_min_lead_s: float = 0.35
    # 擦车目标距车心：物理包络+σ+余量约 2.18m，0.65 道只剩 2.5cm 带、
    # 低于采样分辨率（整层 0 触发）；0.75 道留约 0.36m 带。
    graze_gap_lane: float = 0.75
    coin_collect_half_lane: float = 0.35
    # 大车（宽 > 此值的车）不作擦边目标：真实半宽会吃进"擦边"带，
    # 且卡车的极限超车判定与轿车不同口径。仍按真实半宽计入碰撞。
    graze_max_width_m: float = 2.2
    # 无纵深车"纵向近"的像素判据：cy 距自车接地点行不足此值才可当障碍。
    # 远处（cy≈330）的 t=None 车不再被当成贴身静态墙。
    near_cy_margin_px: float = 120.0
    # 同组金币到站时间只允许按时间递减：突然变大超过此值=看到了组里更远的一枚，
    # 沿用旧时间并前移（否则计划在 1.5s/3.0s 之间反复进出视界）。
    coin_time_jump_tol_s: float = 0.25
    # 价值模型（RULES §4.2/§4.11）：擦车走量、金币按组、奖励车偶发高值、
    # 普通碰撞只损失时间——用价值相加替代四层字典序。
    value_graze: float = 120.0        # 极限超车 120 分/辆
    value_coin_group: float = 90.0    # 平地金币组 90 分/组
    value_coin_air: float = 120.0     # 空中金币组（观测 ≥4 枚）
    value_reward: float = 240.0       # 动作 120 + 一次退场约 180，按偶发折扣
    cost_collision: float = 60.0      # 每次普通车碰撞的时间/节奏损失
    cost_collision_depth_m: float = 120.0  # 重叠每深 1m 追加成本
    # 中心目标死区与维护性杆幅：带内不修正、只在带外追近侧带边，
    # 防无机会时的画龙（旧选择层同口径 0.3 道 / 0.45 杆幅）。
    center_band_lane: float = 0.3
    center_stick_max: float = 0.45
    # 视界外预置：2s 外的金币/奖励车按折扣价计入“2s 末端更靠近”，
    # 避免只在目标进入视界后才动作（旧选择层 preposition 的价值版）。
    preposition_horizon_s: float = 4.0
    preposition_discount: float = 0.5
    preposition_radius_lane: float = 2.5
    max_obj_age_s: float = 0.25      # 对象超龄丢弃
    corridor_age_max_s: float = 0.4  # 走廊超龄标 stale（实测深度年龄 p99≈0.34s）
    frame_delta_max_m: float = 0.2
    # 参考系分歧时走廊最多加宽到此值（实测分歧可达 ±4.5m，全额加宽=走廊失效）
    uncertain_widen_max_m: float = 0.6
    sigma_obj0_m: float = 0.15       # 对象横向不确定度基值
    sigma_obj_rate_mps: float = 0.6  # 随年龄增长
    sigma_ego0_m: float = 0.0        # fit_plant 标定
    sigma_ego_rate_mps: float = 0.0  # fit_plant 标定
    sigma_ego_max_m: float = 1.2     # 超此缩短时域
    switch_margin: float = 15.0      # 价值差门槛（分）
    switch_immediate_margin: float = 100.0  # 大价值差（避撞/高值）立即切
    switch_geom_margin: float = 0.03  # 同价值时中心/动作改善门槛（道）
    switch_dwell_s: float = 0.2
    score_epsilon: float = 1e-3
    calibrated: bool = False         # 标定通过才允许 live


@dataclass(frozen=True)
class CorridorReach:
    """走廊记忆回退与奖励车源闸。

    运行态（2026-10-18 走廊重建后）：加性场与轨迹采样器已删除；只剩
    `enabled`（_EgoRoadObserver 走廊记忆回退）、`coin_confirm_ticks`（币组
    确认窗）与 `bonus_value`（奖励车源总闸）被消费。"""

    enabled: bool = False
    coin_confirm_ticks: int = 1          # 币组进场的观测确认拍数（1=旧行为）：
                                         #   首次出现当拍不参与选择，防单帧闪烁拉偏
    bonus_value: bool = False            # 奖励车源总闸（false=逐位回今天）


@dataclass(frozen=True)
class MetricCars:
    """车米制状态供数闸（2026-10-12 C 线：车世界模型搬米制系，计划稿 §5C）。

    语义：`enabled` 开闸后 `WorldObservation.metric_cars` 喂场车源链
    （米制优先、像素回退）；米制量本身由锚点链路自况有效性（无锚=UNKNOWN
    弃权），本闸只管供不供。"""

    enabled: bool = True


@dataclass(frozen=True)
class MetricBridge:
    """速度像素桥闸（证据闪 2026-10-17 两局实测：簇跳变清零 7.7~16.2% 拍 +
    无锚段 P90=36 拍，米制 t_meet 在关键逼近段段状缺席——追尾案速度在撞击前
    0.3s 才成形）。

    语义：`enabled` 开闸后，米制 t_meet 缺席的拍（UNKNOWN 五因/有锚估计窗
    未开/丢框保锚）以 rel_approach 纵向行运动桥接 t_meet（同一公式真源
    t_meet_car_s，免二次标定），桥接段不确定度走 threat 权重与可达裕度
    （消费面 corridor_reach）；`stay` 是每侧最短驻留（防双源翻脸，与
    commitment k_stay 同族纪律）。闸关=逐位回今天（桥接行无桥接量）。"""

    enabled: bool = False
    stay: int = 2                # 每侧最短驻留（拍）
    weight: float = 0.5          # 威胁降权初值（碰撞/遮挡时间放大 1/weight）
    margin_add_s: float = 0.15   # 桥接车可达门裕度放宽量（实机标定候选）


@dataclass(frozen=True)
class Geometry:
    """几何供数（2026-10-18 走廊重建）：wall_line_master=true 时墙线拟合当
    走廊主人（region 降为兜底）；false=影子只落 trace。cross_check 是与同帧
    绿区中心的互证容差（道），超差判 degraded 回退。"""

    wall_line_master: bool = False
    wall_line_cross_check_lane: float = 1.0


@dataclass(frozen=True)
class DebugDump:
    """调试图证据包三态闸（2026-10-12 设计裁决：消费者=时刻配图核验）。

    off：深度观测器不建 debug_dir，证据包零落盘。events：worker 内存 ring
    留最近 ``ring`` 次捕获，push 快照（滞后 1 拍）报 state=CONSERVE 事件时
    落盘 ring+后续 ``event_window`` 帧；恒开全量是探针期残留。full：现状
    全量 0.5s 节流（感知排错期用）。"""

    mode: str = "events"
    ring: int = 10
    event_window: int = 20


@dataclass(frozen=True)
class DecisionConfig:
    mode: Mode
    control: Control
    timing: Timing
    scoring: Scoring
    hysteresis: Hysteresis
    validate: Validate
    planner: Planner
    traffic: Traffic
    motion: Motion = Motion()
    grid_xcheck: GridXcheck = GridXcheck()
    # （lane_scoring 段已退役删除 20261015：被 corridor_reach 取代）
    corridor_reach: CorridorReach = CorridorReach()
    selection: Selection = Selection()
    geometry: Geometry = Geometry()
    metric_cars: MetricCars = MetricCars()
    metric_bridge: MetricBridge = MetricBridge()
    debug_dump: DebugDump = DebugDump()


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


def _floats_def(d: dict, sec: str, key: str,
                default: tuple[float, ...]) -> tuple[float, ...]:
    """可选数值数组：缺键回默认；元素非数值/越出 [-1,1] 即拒载。"""
    v = d.get(sec, {}).get(key, list(default))
    if not isinstance(v, list) or not v:
        raise ValueError(f"decision.json [{sec}].{key} 需非空数组：{v!r}")
    out = []
    for x in v:
        if not isinstance(x, (int, float)) or isinstance(x, bool) \
                or not -1.0 <= float(x) <= 1.0:
            raise ValueError(f"decision.json [{sec}].{key} 元素非法：{x!r}")
        out.append(float(x))
    return tuple(out)


def _int(d: dict, sec: str, key: str, *, lo: int, hi: int | None = None) -> int:
    v = d.get(sec, {}).get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"decision.json [{sec}].{key} 缺失或非整数：{v!r}")
    if v < lo or (hi is not None and v > hi):
        raise ValueError(f"decision.json [{sec}].{key}={v} 越界（需 ∈[{lo},{hi}]）")
    return v


def _num_def(d: dict, sec: str, key: str, default: float, *,
             lo: float | None = None, hi: float | None = None,
             lo_open: bool = True) -> float:
    """可选数值键：段/键缺失回默认（行为闸整段可选的兼容面），存在则按 _num 校验。"""
    if key not in d.get(sec, {}):
        return default
    return _num(d, sec, key, lo=lo, hi=hi, lo_open=lo_open)


def _int_def(d: dict, sec: str, key: str, default: int, *,
             lo: int, hi: int | None = None) -> int:
    """可选整数键：段/键缺失回默认，存在则按 _int 校验。"""
    if key not in d.get(sec, {}):
        return default
    return _int(d, sec, key, lo=lo, hi=hi)


def _str_def(d: dict, sec: str, key: str, default: str,
             choices: tuple[str, ...]) -> str:
    """可选枚举键：段/键缺失回默认，存在则必须在枚举内（fail-loud）。"""
    v = d.get(sec, {}).get(key, default)
    if v not in choices:
        raise ValueError(
            f"decision.json [{sec}].{key}={v!r} 不在枚举 {choices} 内")
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
    _dump_mode = str((d.get("debug_dump") or {}).get("mode") or "events")
    if _dump_mode not in ("off", "events", "full"):
        raise ValueError(
            f"[debug_dump] mode={_dump_mode!r} 不在 off/events/full"
            "（off=零落盘，events=事件触发 ring+窗口，full=恒开全量）")

    cfg = DecisionConfig(
        mode=Mode(
            allow_car_graze=_bool(d, "mode", "allow_car_graze")),
        control=Control(
            frame_rate_hz=_num(d, "control", "frame_rate_hz", lo=0),
            t_change_max_s=_num(d, "control", "t_change_max_s", lo=0),
            abort_settle_v_lane_s=_num(d, "control", "abort_settle_v_lane_s",
                                       lo=0, lo_open=False)),
        timing=Timing(
            tau_resp_s=_num(d, "timing", "tau_resp_s", lo=0, lo_open=False),
            lane_change_base_s=_num(d, "timing", "lane_change_base_s", lo=0),
            lane_change_k_s_per_lane=_num(d, "timing", "lane_change_k_s_per_lane",
                                          lo=0, lo_open=False),
            margin_s=_num(d, "timing", "margin_s", lo=0, lo_open=False)),
        scoring=Scoring(
            coin_value_per_unit=_num(d, "scoring", "coin_value_per_unit", lo=0),
            coin_group_min_count=_num(d, "scoring", "coin_group_min_count",
                                      lo=0),
            conf_floor=_num(d, "scoring", "conf_floor", lo=0, hi=0.999),
            conf_hi=_num(d, "scoring", "conf_hi", lo=0, hi=0.999),
            shift_cost_per_lane=_num(d, "scoring", "shift_cost_per_lane",
                                     lo=0, lo_open=False),
            life_tau_s=_num(d, "scoring", "life_tau_s", lo=0)),
        hysteresis=Hysteresis(
            target_slew_lane_s=_num_def(d, "hysteresis", "target_slew_lane_s",
                                        0.0, lo=0)),
        validate=Validate(
            t_empty_s=_num(d, "validate", "t_empty_s", lo=0),
            t_recover_s=_num(d, "validate", "t_recover_s", lo=0),
            t_conserve_max_s=_num(d, "validate", "t_conserve_max_s", lo=0),
            x_lane_abs_max=_num(d, "validate", "x_lane_abs_max", lo=0),
            cy_jump_max_px=_num(d, "validate", "cy_jump_max_px", lo=0)),
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
                                   lo_open=False),
            anchor_max_off=_num(d, "planner", "anchor_max_off", lo=0,
                                lo_open=False),
            anchor_stale_s=_num(d, "planner", "anchor_stale_s", lo=0,
                                lo_open=False),
            hold_deadband_lane=_num(d, "planner", "hold_deadband_lane", lo=0,
                                    lo_open=False),
            hold_stick_max=_num(d, "planner", "hold_stick_max", lo=0, hi=1),
            maneuver_deadband_lane=_num_def(
                d, "planner", "maneuver_deadband_lane", 0.0, lo=0),
            maneuver_stick_max=_num_def(
                d, "planner", "maneuver_stick_max", 0.45, lo=0, hi=1),
            obs_beta_deadband_lane=_num_def(
                d, "planner", "obs_beta_deadband_lane", 0.0, lo=0),
            obs_beta_sign_agree=_bool(d, "planner", "obs_beta_sign_agree",
                                      default=False),
            obs_beta_dv_rate_lane_s2=_num_def(
                d, "planner", "obs_beta_dv_rate_lane_s2", 0.0, lo=0),
            obs_position_only=_bool(d, "planner", "obs_position_only",
                                    default=True)),
        traffic=Traffic(
            exit_margin_px=_num(d, "traffic", "exit_margin_px", lo=0),            presumed_cy_px=_num(d, "traffic", "presumed_cy_px", lo=0),
            presumed_lane_max=_num(d, "traffic", "presumed_lane_max", lo=0),
            min_obs_ticks=_int(d, "traffic", "min_obs_ticks", lo=1),
            ghost_max_age_ticks=_int(d, "traffic", "ghost_max_age_ticks", lo=2),
            ghost_rel_eps=_num(d, "traffic", "ghost_rel_eps", lo=0),
            v_lat_ema_alpha=_num(d, "traffic", "v_lat_ema_alpha", lo=0, hi=1),
            lane_vote_decay=_num_def(d, "traffic", "lane_vote_decay", 0.7,
                                     lo=0, hi=1, lo_open=False),
            lane_memb_min_weight=_num_def(d, "traffic", "lane_memb_min_weight",
                                          1.5, lo=0),
            lane_vote_sigma_max=_num_def(d, "traffic", "lane_vote_sigma_max",
                                         0.6, lo=0, lo_open=False),
            width_ttl_ticks=_int_def(d, "traffic", "width_ttl_ticks", 6, lo=0),
            metric_x_ema_alpha=_num_def(d, "traffic", "metric_x_ema_alpha",
                                        0.35, lo=0, hi=1, lo_open=True),
            metric_z_ema_alpha=_num_def(d, "traffic", "metric_z_ema_alpha",
                                        0.5, lo=0, hi=1, lo_open=True)),
        motion=Motion(
            mode=_str_def(d, "motion", "mode", "off",
                          ("off", "shadow", "live")),
            horizon_s=_num_def(d, "motion", "horizon_s", 2.0, lo=0),
            preview_s=_num_def(d, "motion", "preview_s", 0.2, lo=0),
            dt_s=_num_def(d, "motion", "dt_s", 0.05, lo=0),
            steer_levels=_floats_def(
                d, "motion", "steer_levels",
                (-0.85, -0.55, -0.35, -0.2, -0.1, 0.0,
                 0.1, 0.2, 0.35, 0.55, 0.85)),
            near_z_m=_num_def(d, "motion", "near_z_m", 6.0, lo=0),
            corridor_check_s=_num_def(
                d, "motion", "corridor_check_s", 1.0, lo=0),
            collision_margin_m=_num_def(
                d, "motion", "collision_margin_m", 0.2, lo=0),
            collision_min_lead_s=_num_def(
                d, "motion", "collision_min_lead_s", 0.35, lo=0),
            graze_gap_lane=_num_def(
                d, "motion", "graze_gap_lane", 0.75, lo=0),
            coin_collect_half_lane=_num_def(
                d, "motion", "coin_collect_half_lane", 0.35, lo=0),
            graze_max_width_m=_num_def(
                d, "motion", "graze_max_width_m", 2.2, lo=0),
            near_cy_margin_px=_num_def(
                d, "motion", "near_cy_margin_px", 120.0, lo=0),
            coin_time_jump_tol_s=_num_def(
                d, "motion", "coin_time_jump_tol_s", 0.25, lo=0),
            value_graze=_num_def(d, "motion", "value_graze", 120.0, lo=0),
            value_coin_group=_num_def(
                d, "motion", "value_coin_group", 90.0, lo=0),
            value_coin_air=_num_def(
                d, "motion", "value_coin_air", 120.0, lo=0),
            value_reward=_num_def(d, "motion", "value_reward", 240.0, lo=0),
            cost_collision=_num_def(
                d, "motion", "cost_collision", 60.0, lo=0),
            cost_collision_depth_m=_num_def(
                d, "motion", "cost_collision_depth_m", 120.0, lo=0),
            center_band_lane=_num_def(
                d, "motion", "center_band_lane", 0.3, lo=0),
            center_stick_max=_num_def(
                d, "motion", "center_stick_max", 0.45, lo=0),
            preposition_horizon_s=_num_def(
                d, "motion", "preposition_horizon_s", 4.0, lo=0),
            preposition_discount=_num_def(
                d, "motion", "preposition_discount", 0.5, lo=0),
            preposition_radius_lane=_num_def(
                d, "motion", "preposition_radius_lane", 1.5, lo=0),
            max_obj_age_s=_num_def(d, "motion", "max_obj_age_s", 0.25, lo=0),
            corridor_age_max_s=_num_def(
                d, "motion", "corridor_age_max_s", 0.25, lo=0),
            frame_delta_max_m=_num_def(
                d, "motion", "frame_delta_max_m", 0.2, lo=0),
            uncertain_widen_max_m=_num_def(
                d, "motion", "uncertain_widen_max_m", 0.6, lo=0),
            sigma_obj0_m=_num_def(d, "motion", "sigma_obj0_m", 0.15, lo=0),
            sigma_obj_rate_mps=_num_def(
                d, "motion", "sigma_obj_rate_mps", 0.6, lo=0),
            sigma_ego0_m=_num_def(
                d, "motion", "sigma_ego0_m", 0.0, lo=0, lo_open=False),
            sigma_ego_rate_mps=_num_def(
                d, "motion", "sigma_ego_rate_mps", 0.0, lo=0,
                lo_open=False),
            sigma_ego_max_m=_num_def(
                d, "motion", "sigma_ego_max_m", 1.2, lo=0),
            switch_margin=_num_def(d, "motion", "switch_margin", 15.0, lo=0),
            switch_immediate_margin=_num_def(
                d, "motion", "switch_immediate_margin", 100.0, lo=0),
            switch_geom_margin=_num_def(
                d, "motion", "switch_geom_margin", 0.03, lo=0),
            switch_dwell_s=_num_def(
                d, "motion", "switch_dwell_s", 0.2, lo=0),
            score_epsilon=_num_def(
                d, "motion", "score_epsilon", 1e-3, lo=0),
            calibrated=_bool(d, "motion", "calibrated", default=False)),
        grid_xcheck=GridXcheck(
            mode=_str_def(d, "grid_xcheck", "mode", "off",
                          ("off", "master", "veto")),
            tol_lane=_num_def(d, "grid_xcheck", "tol_lane", 0.45, lo=0),
            bypass_streak=_int_def(d, "grid_xcheck", "bypass_streak", 8, lo=1)),
        corridor_reach=CorridorReach(
            enabled=_bool(d, "corridor_reach", "enabled", default=False),
            coin_confirm_ticks=_int_def(d, "corridor_reach",
                                        "coin_confirm_ticks", 1, lo=1),
            bonus_value=_bool(d, "corridor_reach", "bonus_value",
                              default=False)),
        selection=Selection(
            horizon_s=_num_def(d, "selection", "horizon_s", 3.0, lo=0),
            max_waypoints=_int_def(d, "selection", "max_waypoints", 4,
                                   lo=1, hi=4),
            graze_gap_lane=_num_def(d, "selection", "graze_gap_lane", 0.65,
                                    lo=0),
            center_band_lane=_num_def(d, "selection", "center_band_lane", 0.3,
                                      lo=0),
            opportunity_tol_lane=_num_def(
                d, "selection", "opportunity_tol_lane", 0.75, lo=0),
            coin_pick_tol_lane=_num_def(
                d, "selection", "coin_pick_tol_lane", 1.2, lo=0),
            coin_hold_s=_num_def(d, "selection", "coin_hold_s", 0.6, lo=0),
            t_move_base_s=_num_def(d, "selection", "t_move_base_s", 0.3, lo=0),
            t_move_per_lane_s=_num_def(
                d, "selection", "t_move_per_lane_s", 2.0, lo=0),
            reaction_s=_num_def(d, "selection", "reaction_s", 0.13, lo=0),
            margin_s=_num_def(d, "selection", "margin_s", 0.15, lo=0),
            aligned_lane=_num_def(d, "selection", "aligned_lane", 0.1, lo=0),
            reward_aligned_lane=_num_def(
                d, "selection", "reward_aligned_lane", 0.2, lo=0),
            wall_margin_lane=_num_def(
                d, "selection", "wall_margin_lane", 0.35, lo=0),
            center_lookahead_m=_num_def(
                d, "selection", "center_lookahead_m", 0.0, lo=0,
                lo_open=False),
            preposition_enabled=_bool(
                d, "selection", "preposition_enabled", default=True),
            preposition_horizon_s=_num_def(
                d, "selection", "preposition_horizon_s", 6.0, lo=0),
            preposition_max_lane=_num_def(
                d, "selection", "preposition_max_lane", 2.5, lo=0),
            preposition_bias_lane=_num_def(
                d, "selection", "preposition_bias_lane", 1.0, lo=0),
            switch_dwell_s=_num_def(
                d, "selection", "switch_dwell_s", 0.2, lo=0),
            reward_tol_lane=_num_def(
                d, "selection", "reward_tol_lane", 1.5, lo=0),
            car_half_lane=_num_def(d, "selection", "car_half_lane", 0.27, lo=0),
            ego_half_lane=_num_def(d, "selection", "ego_half_lane", 0.27, lo=0)),
        geometry=Geometry(
            wall_line_master=_bool(d, "geometry", "wall_line_master",
                                   default=False),
            wall_line_cross_check_lane=_num_def(
                d, "geometry", "wall_line_cross_check_lane", 1.0, lo=0,
                lo_open=False)),
        metric_cars=MetricCars(
            enabled=_bool(d, "metric_cars", "enabled", default=True)),
        metric_bridge=MetricBridge(
            enabled=_bool(d, "metric_bridge", "enabled", default=False),
            stay=_int_def(d, "metric_bridge", "stay", 2, lo=1),
            weight=_num_def(d, "metric_bridge", "weight", 0.5,
                            lo=0, lo_open=False),
            margin_add_s=_num_def(d, "metric_bridge", "margin_add_s", 0.15,
                                  lo=0, lo_open=False)),
        debug_dump=DebugDump(
            mode=_dump_mode,
            ring=_int_def(d, "debug_dump", "ring", 10, lo=0),
            event_window=_int_def(d, "debug_dump", "event_window", 20, lo=0)))

    # 段间依赖矛盾：单条范围过不了的联合错误，在这里拦
    v = cfg.validate
    if v.t_recover_s >= v.t_conserve_max_s:
        raise ValueError(
            f"[validate] t_recover_s={v.t_recover_s} 必须 < t_conserve_max_s="
            f"{v.t_conserve_max_s}（否则保守态永远先升级 FAULT，恢复通道死锁）")
    if not cfg.scoring.conf_floor < cfg.scoring.conf_hi:
        raise ValueError(
            f"[scoring] conf_floor={cfg.scoring.conf_floor} 必须 < conf_hi="
            f"{cfg.scoring.conf_hi}（折扣区倒置）")
    if cfg.mode.allow_car_graze:
        raise ValueError(
            "mode.allow_car_graze=true 在 v1 拒绝启用（coin-only 保守基线；"
            "复议条件=近场 0.05 车道级精度实测，设计稿 §三）")
    s = cfg.selection
    if s.preposition_enabled:
        if s.preposition_horizon_s <= s.horizon_s:
            raise ValueError(
                f"[selection] preposition_horizon_s={s.preposition_horizon_s} "
                f"必须 > horizon_s={s.horizon_s}（预置位只接视界外机会）")
        if not 0.0 < s.preposition_bias_lane <= s.preposition_max_lane:
            raise ValueError(
                f"[selection] preposition_bias_lane="
                f"{s.preposition_bias_lane} 需 ∈(0, "
                f"preposition_max_lane={s.preposition_max_lane}]")
    p, t = cfg.planner, cfg.timing
    if p.lookahead_tau_s < t.tau_resp_s:
        raise ValueError(
            f"[planner] lookahead_tau_s={p.lookahead_tau_s} 低于实测响应延迟 "
            f"tau_resp_s={t.tau_resp_s}（提前量无视延迟，planner 设计稿 §三）")
    if not 0.0 < p.maneuver_stick_max <= 1.0:
        raise ValueError(
            f"[planner] maneuver_stick_max={p.maneuver_stick_max} 需 ∈(0,1]")
    if p.obs_beta_dv_rate_lane_s2 > p.a_lat_gain:
        raise ValueError(
            f"[planner] obs_beta_dv_rate_lane_s2="
            f"{p.obs_beta_dv_rate_lane_s2} > a_lat_gain={p.a_lat_gain}"
            "（速度注入比车辆可产生的加速度还快=观测噪声直接变成打舵）")
    full_stroke_ticks = 2 * 32767 / p.rate_limit_raw if p.rate_limit_raw > 0 else 0
    if full_stroke_ticks < 2:
        raise ValueError(
            f"[planner] rate_limit_raw={p.rate_limit_raw} 满行程仅 "
            f"{full_stroke_ticks:.1f} tick（<2 tick 限幅形同虚设，是失效档位）")
    m = cfg.motion
    if m.preview_s >= m.horizon_s:
        raise ValueError(
            f"[motion] preview_s={m.preview_s} 必须 < horizon_s={m.horizon_s}")
    if m.corridor_check_s > m.horizon_s:
        raise ValueError(
            f"[motion] corridor_check_s={m.corridor_check_s} 不得 > "
            f"horizon_s={m.horizon_s}")
    if m.dt_s <= 0.0:
        raise ValueError(f"[motion] dt_s={m.dt_s} 需 >0")
    if m.mode == "live" and not m.calibrated:
        raise ValueError(
            "[motion] mode=live 但 calibrated=false（先用 fit_plant 标定"
            "并通过报告闸再写入本键）")
    return cfg


@lru_cache(maxsize=1)
def load_decision() -> DecisionConfig:
    """读行为参数真源（进程内一次；改文件需重启或 clear cache——回放/实机都是
    开局读取，无热更需求，设计稿 §五）。"""
    return _read_decision(DECISION_FILE)
