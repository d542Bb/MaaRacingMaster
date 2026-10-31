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
from maaracing_master.plugins.speedrush.view_guard import ViewGuardConfig

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Mode:
    allow_car_graze: bool


@dataclass(frozen=True)
class Selection:
    """四层选择层参数（2026-10-18 走廊重建；数值真源 decision.json selection）。

    横向量全为米制；带 `_lane` 的键在进选择层时 ×lane_w。``horizon_s`` 是
    短视界（含中间机会）；L4 不修正区间由 ``lane_clearance_lane`` 推导
    （车道内物理余量），``center_band_lane`` 只作带宽上限。"""

    horizon_s: float = 3.0
    max_waypoints: int = 4
    graze_gap_lane: float = 0.68
    center_band_lane: float = 0.3
    opportunity_tol_lane: float = 0.75
    coin_pick_tol_lane: float = 1.2
    coin_hold_s: float = 0.6
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
    lane_clearance_lane: float = 0.06


@dataclass(frozen=True)
class GridXcheck:
    """路心供数主人闸：选哪条路心链（2026-10-06 定案，2026-10-23 扩为绝对链）。

    背景：找边单帧对会把「以车为中心的走廊」当路（骑黄线段 L=−1.32/R=+0.53
    报"正中"，对宽 2.68 道像真路，宽门/中值窗全失明）；一切读数都在自车系，
    上一拍的对在自车横移后就不再成立——复用相对读数当路心是 ±6m 横摆的根因。

    mode 语义：
    - ``master``＝**绝对路心链**（生产）：墙/空气墙（双侧同箱中点、单侧
      墙±W/2，W=物理碰撞边界宽）→ 绿区物理边界 → 绝对记忆（0.8s）→ None。
      找边对/保鲜槽不接棒；坏平面拍（cam_h 出物理带）整帧不进供数；来源
      跳变 >0.4 道需连续 2 拍一致才采信。
    - ``off``＝找边对当主人（旧链序，回放对比用）。
    - ``veto``＝旧分歧否决语义（对系统性偏差会否决 57% 新鲜对→黑视循环，
      只留回放对比，生产不开）。"""

    mode: str = "off"           # master | off | veto
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

    lookahead_tau_s: float     # 外推提前量（位置误差的前瞻时间）
    # —— 航向跟踪律（2026-10-30 架构：杆是航向指令，不是横向加速度）——
    # 位置误差 → 目标车头角 → 杆值，横移由 x' = v·tanψ 给出。旧的横速双积分
    # （a_lat_gain/tau_align 的积分、v_lat_max 饱和、两路横速融合）已删除。
    k_head_lane: float         # 车速未知时的退化比例（rad/道）
    k_lat_lane_s: float        # 闭环带宽：期望横速 = 此值 × 车道误差（道/s）
    k_psi: float               # 车头角误差 → 杆值（杆/rad）
    k_r: float                 # 角速度阻尼（杆/(rad/s)）
    tau_heading_s: float       # 车头角响应时间常数（执行链并入模型）
    heading_max_rad: float     # 机动态车头角硬上限（「大变向」写成约束）
    heading_hold_rad: float    # 维护态车头角上限（定中心只需小角度）
    tau_steer_s: float         # 杆值低通时间基常数（α=1−exp(−dt/τ)）
    rate_limit_raw: float      # 每 tick 杆值变化上限（±32767 系）
    stick_deadzone_raw: float  # |杆值| 低于此值归 0（游戏手柄死区）
    throttle_raw: int          # 右扳机恒值（RULES §4.1 权威：全程油门）
    hold_max_ticks: int        # 决策过期后最多保持拍数，第 +1 拍起按 CONSERVE
    # —— 路中心连续重锚（step 2.5，2026-09-22：开环虚胖的闭环解）——
    obs_alpha: float           # 位置修正增益（alpha-beta 滤波），有路缘观测拍生效 [需实测]
    obs_jump_max_lane: float   # 新息门：|观测−预测| 超此值判坏检测，本拍弃观测
    # —— 路锚卫生（2026-09-26 12:39/13:29 两局复盘：骑缘拍毒化参考系；陈旧帧+
    #    拍数门复位太慢=0 杆直开怼墙）——
    anchor_max_off: float      # 锚形成门：|road_offset| ≤ 此值才形成参考系 [需实测]
    anchor_stale_s: float      # 距上次被接受修正超此秒数（含断供）→ 陈旧重基到路上 [需实测]
    # —— 维护性转向权限（2026-09-26 16:44 局复盘：定中心全权 PD=满舵绕桩，
    #    257/267 饱和拍在保持态、速度起不来）——
    hold_deadband_lane: float  # 死区：|前瞻误差| 小于此不发维护力（道；0.15=车道内物理余量，2026-10-27 从 0.5 收紧）
    # 维护性转向（定中心/保持/回稳）杆幅上限。只截「追目标朝向」那一项：
    # 车自己的朝向/角速度阻尼不截——车带着大车头角进带时，刹那就是唯一
    # 可取的动作（20261031_172015 fid 1789~1801：ψ=0.15~0.22、横速 1.7
    # 道/s，杆恒 −0.45，回正被拖到 0.9s，车从带心冲到邻道）。[需实测]
    hold_stick_max: float
    # ---- 机动态全权（阶段 2，2026-10-18：decision.maneuver 置位时）----
    maneuver_deadband_lane: float = 0.0   # 机动态死区（道，0=无死区）
    maneuver_stick_max: float = 0.45      # 机动态杆幅上限（0.45=与保持态相同）
    # 执行层全权的误差门槛（道）：选择层 maneuver 现在按安全区间（约 0.17 道）
    # 置位，但 0.2 道的日常修正不该用满舵——全权仍要求 |目标−车位| 超此值。
    # 20261027_150850_1 s2：计划因参考系分歧退回 PD 的拍占 37%，PD 把 0.2 道
    # 误差当机动，实际满杆 122→227；加此门后回到旧全权触发量级。
    maneuver_full_error_lane: float = 0.3
    # 持续偏移放行（2026-10-23）：同向、量级相近的大新息连续 N 拍 = 参考系
    # 跳变（0.15s 内 2 道级新息不可能是本车位移），不是单帧坏检测——确认后
    # 一步重基（exec:=obs），不按 α 逐拍追。旧行为要等 anchor_stale_s（1s）
    # 重基，1s 内规划器按错位帧打杆（实拍 180900_p2：ro=−1.74 持续 1.4s，
    # exec 从 −0.71 漂到 −0.29 才重基）。
    obs_jump_confirm_ticks: int = 3
    # 计划前馈的杆值变化率上限（归一杆值/秒）：计划换候选时前馈会阶跃，
    # 不限制会把候选切换直接变成方向盘每拍打半圈。首帧/计划重入不限制。
    plan_u_slew_s: float = 4.0
    # 计划判参考系分歧（plan:frame_shift）时，只用 planner 帧状态保持当前道的
    # 最长拍数；超过后退回旧的跨帧回退（长弯道里无限直开同样危险）
    frame_hold_max_ticks: int = 25
    # 计划**短断供**保持：上一拍计划还正常、本拍因参考系/速度估计闪断而无效时，
    # 继续打最后一条航向指令的最长拍数。计划有效段的中断 81% 只有 1~2 拍
    # （2026-10-30 两局：145 段里 81 段），每段都切一次 PD 会让方向盘在"走"
    # 和"停"之间来回——实测控制来源切换 24~31 次/10s，切换拍杆值跳变约为
    # 普通拍的 2 倍。超过此拍数才交还 PD。
    plan_hold_max_ticks: int = 6
    # 横向状态「不可信」判据（2026-10-31）：距上一次新鲜位置证据（新深度帧，
    # 或已对账锁线）超过此秒数，位置就是外推值而不是测量值——模型积分冻结、
    # 停融合、执行层把杆归零（只按朝向回正，不积分旧转杆）、计划不得接管，
    # 等证据回来再按重获重基。
    state_blind_s: float = 0.4
    # ψ 龄门：ψ 无新线角证据时保留 _PSI_STALE_S=0.6s 给阻尼，但超过此龄
    # 它就不再是「本拍测量」——横速归零（旧朝向乘车速就是位置外推，实测
    # 1.2~1.8 道/s），执行层也只给维护级杆幅（盲打满舵会把车头角叠到设计
    # 上限之外：20261031_172015 fid 1755~1776 用 0.4s 前的 ψ=−0.035 持续
    # 右打 +0.76，车头角真实已转到 +0.08）。
    heading_vel_max_age_s: float = 0.25


@dataclass(frozen=True)
class Traffic:
    """车流观测层参数（阶段 C 设计稿 §三；起值 [需实测 C5]，回放定档只改 json）。"""

    exit_margin_px: float      # 下带判据：last cy ≥ 画面下缘 − 此值 → 从车尾侧消失
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
    # 车宽物理域（米，全宽）：框宽地面尺换算结果越出此域视为坏读数，
    # 不出 w_m、消费方回落标称半宽（2026-10-30；域为乘用车+厢式的实测范围）
    min_width_m: float = 1.2
    max_width_m: float = 2.6
    # —— 决策边界的时间平滑（2026-10-19 晚：车米制 x/z 逐拍跳 ±1~4m，
    # —— 与 0.5m 擦车带同量级；在决策层平滑不碰跟踪层语义）——
    metric_x_ema_alpha: float = 0.35
    metric_z_ema_alpha: float = 0.5
    # 米制横位硬跳变门：单拍横跳超过此值先拒（真实横移不可能这么快），
    # 连续 hold_ticks 拍同向才接受——防止一次深度跳变把超车净距挪到车身上。
    metric_x_jump_max_m: float = 1.5
    metric_x_jump_hold_ticks: int = 3
    # 米制纵深跳变门：超过此值视为真实通过/换锚，直接重置；小抖动才走 EMA。
    # 70m/s 通过时 0.05s 真实变化可达 3.5m，旧 EMA 会把并列车拖成 13m 前方。
    metric_z_jump_max_m: float = 6.0


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
    # 候选的**绝对**车头角指令（rad）：执行层按同一条朝向律把杆打到这个
    # 朝向上。0.14 ≈ 8°，与人工变道实测峰值（6~9°）同量级；细档在 5° 以内。
    # 绝对档位（不随当前朝向平移）+ 单独的「当前朝向」候选：把车从 +0.05
    # 带回 0 需要「反打到 −0.03」，偏移量档给不出这一档。
    heading_levels: tuple[float, ...] = (
        -0.14, -0.09, -0.05, -0.03, -0.015, -0.005,
        0.0, 0.005, 0.015, 0.03, 0.05, 0.09, 0.14)
    near_z_m: float = 6.0
    # 有距无速域：本车道前方有纵深、但速度窗口未开的车，在此距离内按
    # 距离给提前横移的小分（不参与命中/擦车奖励，只避让）。
    range_hazard_m: float = 70.0
    # 无速度目标折算到站时刻用的保守闭速（m/s）：宁可早动，不可晚动。
    range_hazard_speed_mps: float = 45.0
    # 有距无速域的权重：必须高过 PlanGate 的切换余量，才能在大约 0.8s
    # 之外的首次判断就换道（小于余量会被“保持上一动作”吃掉）。
    range_hazard_gain: float = 2.0
    # 走廊约束只验前 1.0s：恒定杆 2s 必然驶出近场包络，硬验全程会把必要的
    # 避让判成不可行（影子实测 p2 8% 全灭假象）
    corridor_check_s: float = 1.0
    collision_margin_m: float = 0.05
    # 擦车计分带下界 = 物理包络（含对象 σ）+ 此余量。对象 σ（实测 0.24~0.34m）
    # 已经是定位不确定度的安全垫，再叠 0.2m 会把带下界推到选择层瞄准点之外——
    # 候选要么「贴包络」要么「过远」，正好跳过计分带（2026-10-30 实测：擦边
    # 拍只有 21%，选择层 45% 的 pass 拍在计划层落到回中）。0.05 只留贴包络的
    # 分辨余量。
    # 走廊两层（2026-10-27）：软边界用 selection.wall_margin_lane（车心离墙
    # 0.35 道，车身留 0.08 道），只收代价；硬边界=车半宽+此间隙，只有越过
    # 它才判不可行。旧实现把软边界当硬约束：车贴边行驶时 1~10cm 的余量侵入
    # 就让可行集全灭，兜底选「侵入最小」= 满杆逃离（20261027 局 134222_1
    # 有 21 个满杆拍，平均侵入只有 1~10cm）。
    corridor_hard_clearance_m: float = 0.15
    corridor_soft_cost_per_m: float = 120.0
    # 目标位候选的生成律用生产跟踪律（k_head_lane）：计划怎么预测，控制
    # 就怎么执行——第二套生成律会让「计划说能到、车到不了」。
    # 半程回正候选的最小档位：低于此值的档位 2s 扫不过一道，回正没意义。
    reverse_min_rad: float = 0.03
    # 硬不可行回退的侵入分桶：同一桶内按价值/动作选，不为厘米级差异满杆。
    corridor_escape_tol_m: float = 0.05
    # 反应时内到站的车：横向动作已无法改变结果，不计入硬约束（防贴身会车
    # 全灭后误触 CONSERVE）
    collision_min_lead_s: float = 0.35
    # 无到站时间的车只有在横向确实近（一车宽量级）时才当障碍；更远的同帧车
    # 不当贴身静态墙。
    near_untimed_lane: float = 0.75
    # 大车（宽 > 此值）在「有距无速」域按真实宽度判，不作提前横移的诱因。
    graze_max_width_m: float = 2.2
    # 无纵深车"纵向近"的像素判据：cy 距画面下缘（保险杠机位）不足此值才可当障碍。
    # 远处（cy≈330）的 t=None 车不再被当成贴身静态墙。
    near_cy_margin_px: float = 120.0
    # 同组金币到站时间只允许按时间递减：突然变大超过此值=看到了组里更远的一枚，
    # 沿用旧时间并前移（否则计划在 1.5s/3.0s 之间反复进出视界）。
    coin_time_jump_tol_s: float = 0.25
    # 安全成本（决策给去哪，计划只付安全代价）：普通碰撞只损失时间，价值
    # 模型不在这里加权得分机会——那是选择层的事。
    cost_collision: float = 60.0      # 每次普通车碰撞的时间/节奏损失
    cost_collision_depth_m: float = 120.0  # 重叠每深 1m 追加成本
    # 中心目标死区与维护性杆幅：带内不修正、只在带外追近侧带边，
    # 防无机会时的画龙（旧选择层同口径 0.3 道 / 0.45 杆幅）。
    center_band_lane: float = 0.3
    center_heading_rad: float = 0.03   # 维护态车头角上限（≈1.7°）：决定横速
    max_obj_age_s: float = 0.25      # 对象超龄丢弃
    corridor_age_max_s: float = 0.4  # 走廊超龄标 stale（实测深度年龄 p99≈0.34s）
    frame_delta_max_m: float = 0.2
    # 参考系分歧超过此值（米）时计划不再接管：场景走廊和自车状态已经不在
    # 同一个横向参考系里，按场景判「贴墙」会把车按到幻影墙上（20261027 局
    # 134222_1 s1 34.2s：fd=5.9m，场景把车放到右缘 0.5m 内，实际 exec=0.35
    # 道、目标 0.28 道；计划满左逃离）。计划退回无效 → PD 用同一参考系的
    # 目标保持，比按错帧打满杆安全。
    frame_delta_hold_m: float = 1.0
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
    # 现任候选短暂不可行的宽限（秒）：供数来源逐拍切换时可行集闪一下，
    # 立即让位会把计划输出打成每拍一个状态；宽限期内保持上一候选。
    switch_infeasible_grace_s: float = 0.1
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
    t_meet_car_s，免二次标定）；`stay` 是每侧最短驻留（防双源翻脸，与
    commitment k_stay 同族纪律）。闸关=逐位回今天（桥接行无桥接量）。"""

    enabled: bool = False
    stay: int = 2                # 每侧最短驻留（拍）


@dataclass(frozen=True)
class Geometry:
    """几何供数：far_ground_supply=中远场粗带地面横向范围进走廊 coarse_bands
    （2026-10-21 方案定案；false=逐位回空白粗带——车道级消费面不读
    coarse_bands，开关只管影子数据面）。"""

    far_ground_supply: bool = True


@dataclass(frozen=True)
class Coarse:
    """粗层选道（2026-10-19）：绿区净空 + 2D 占用选方向。

    lookahead_m 是粗层观察窗（绿区实测域内）；max_offset_lane 是候选车道
    左右范围；collision_margin_m 是车宽包络外的安全余量；range_speed_mps
    只用于把「无纵深但有到站时间」的车折算成粗纵深；switch_margin_m 是
    换道迟滞（现任净空不差于最优这么多时保持）。control=粗层是否接管
    L4 回中兜底（false=只落 trace 的影子期）。far_direction=方向级消费
    （2026-10-21 方案步骤 4）：远粗带不对称预置机会侧——净空与占用打平时
    字典序回中项换成「离远带更开的一侧最近」（false=逐位回回中）。"""

    control: bool = True
    lookahead_m: float = 24.0
    max_offset_lane: float = 2.0
    collision_margin_m: float = 0.5
    range_speed_mps: float = 45.0
    switch_margin_m: float = 4.0
    align_tol_lane: float = 0.4
    far_direction: bool = False


@dataclass(frozen=True)
class Route:
    """路线承诺层（2026-10-23 落地；2026-10-24 实机两局后收敛）。

    control=闸：false 时 `coarse_lane` 逐语句回今天（粗层自选道，含它的两道
    迟滞例外）；true 时接控制的道号取自路线的参考道。其余键只喂定案判据——
    净空门槛复用 `[coarse].switch_margin_m`、米→秒代理复用
    `[coarse].range_speed_mps`、完成位置容差复用 `[selection].center_band_lane`，
    都不在本节复制数值。定案规则只剩"逃生换道 + 并线承诺 + 撤销冷却"：原
    「收益门槛 + 承诺期 + 整段并线时间窗」在 24 m 观察窗下结构上不可达，已删除
    （证据见 CODE_WIKI §22）。soft_car=普通车不构成撤销理由（车是时间成本；
    0.35 s 迎车门在计划层；目标道 `space` 仍按参考道失去几何重建），false=旧行为。"""

    control: bool = False
    urgent_confirm_ticks: int = 3
    opportunity_confirm_ticks: int = 6
    cancel_unsafe_ticks: int = 5
    cancel_cooldown_s: float = 2.0
    finish_v_tol_lane_s: float = 0.4
    merge_max_s: float = 4.0
    soft_car: bool = False
    reject_shift_arrival: bool = False
    urgent_ttc_s: float = 1.0        # 紧急逃生时间门：当前道最近到站时间≤此值
    urgent_blind_m: float = 6.0      # 无到站时间时的近距离盲区（米）
    merge_safe_ttc_s: float = 0.8    # 逃生目标道上车的最早到站时间下限（秒）
    pass_hold: bool = True           # 超车动作进行中不抢方向（见 RouteParams）
    overtake: bool = True            # 提前超车并线（见 RouteParams）
    overtake_ttc_s: float = 2.5      # 提前超车的到站时间上界（秒）


@dataclass(frozen=True)
class LaneObs:
    """S1 影子：轻量车道线观测（TwinLiteNet+ Small）。

    enabled=闸：false 时不建线程、零开销；true 时在独立 CPU 线程跑，
    **只写 trace 的 lane_obs 列，不接控制**。weights 指向外部 ONNX
    （不入库：权重训练自 BDD100K，分发条款需单独核对）；hz 是推送节流上限
    （0=每拍推；队列深度 1 丢旧帧，真正的处理节拍由 worker 吞吐决定），
    detect_every 决定处理帧里做完整检测的比例。二者相乘才是检测频率。"""

    enabled: bool = False
    weights: str = ""
    hz: float = 0.0
    provider: str = "cpu"
    threads: int = 4
    detect_every: int = 1


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
class BoxBridge:
    """锚间框高比桥接与锚点框高一致门（2026-10-23 四局离线验证）。

    语义：`enabled` 开闸后 Tracker 每拍用 `z锚·h锚/h当前` 在锚点之间插值；
    `trace_only=true` 只把桥接值写进 trace（bz 列），`metric_distance_m`
    保持今日口径——首局验证用。`anchor_gate=true` 时新锚是否可信改由框高比
    残差裁决（残差 ≤30% 直接接受，连续两个一致候选锚才重基），替代固定的
    45 m/s 跳变门——后者会误杀 60~70 m/s 的真实高速接近。"""

    enabled: bool = False
    trace_only: bool = True
    anchor_gate: bool = False


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
    coarse: Coarse = Coarse()
    route: Route = Route()
    geometry: Geometry = Geometry()
    metric_cars: MetricCars = MetricCars()
    metric_bridge: MetricBridge = MetricBridge()
    box_bridge: BoxBridge = BoxBridge()
    view_guard: ViewGuardConfig = ViewGuardConfig()
    debug_dump: DebugDump = DebugDump()
    lane_obs: LaneObs = LaneObs()


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
            tau_resp_s=_num(d, "timing", "tau_resp_s", lo=0, lo_open=False)),
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
            k_head_lane=_num(d, "planner", "k_head_lane", lo=0),
            k_lat_lane_s=_num(d, "planner", "k_lat_lane_s", lo=0),
            k_psi=_num(d, "planner", "k_psi", lo=0),
            k_r=_num(d, "planner", "k_r", lo=0, lo_open=False),
            tau_heading_s=_num(d, "planner", "tau_heading_s", lo=0),
            heading_max_rad=_num(d, "planner", "heading_max_rad", lo=0),
            heading_hold_rad=_num(d, "planner", "heading_hold_rad", lo=0),
            tau_steer_s=_num(d, "planner", "tau_steer_s", lo=0),
            rate_limit_raw=_num(d, "planner", "rate_limit_raw", lo=0),
            stick_deadzone_raw=_num(d, "planner", "stick_deadzone_raw",
                                    lo=0, lo_open=False),
            throttle_raw=_int(d, "planner", "throttle_raw", lo=0, hi=255),
            hold_max_ticks=_int(d, "planner", "hold_max_ticks", lo=1),
            obs_alpha=_num(d, "planner", "obs_alpha", lo=0, hi=1),
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
            maneuver_full_error_lane=_num_def(
                d, "planner", "maneuver_full_error_lane", 0.3, lo=0),
            obs_jump_confirm_ticks=_int_def(
                d, "planner", "obs_jump_confirm_ticks", 3, lo=1),
            plan_u_slew_s=_num_def(
                d, "planner", "plan_u_slew_s", 4.0, lo=0, lo_open=False),
            plan_hold_max_ticks=_int_def(
                d, "planner", "plan_hold_max_ticks", 6, lo=0),
            frame_hold_max_ticks=_int_def(
                d, "planner", "frame_hold_max_ticks", 25, lo=1),
            state_blind_s=_num_def(
                d, "planner", "state_blind_s", 0.4, lo=0, lo_open=False),
            heading_vel_max_age_s=_num_def(
                d, "planner", "heading_vel_max_age_s", 0.25, lo=0)),
        traffic=Traffic(
            exit_margin_px=_num(d, "traffic", "exit_margin_px", lo=0),            presumed_cy_px=_num(d, "traffic", "presumed_cy_px", lo=0),
            presumed_lane_max=_num(d, "traffic", "presumed_lane_max", lo=0),
            min_width_m=_num_def(d, "traffic", "min_width_m", 1.2, lo=0),
            max_width_m=_num_def(d, "traffic", "max_width_m", 2.6, lo=0),
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
                                        0.5, lo=0, hi=1, lo_open=True),
            metric_x_jump_max_m=_num_def(
                d, "traffic", "metric_x_jump_max_m", 1.5, lo=0),
            metric_x_jump_hold_ticks=_int_def(
                d, "traffic", "metric_x_jump_hold_ticks", 3, lo=1),
            metric_z_jump_max_m=_num_def(
                d, "traffic", "metric_z_jump_max_m", 6.0, lo=0)),
        motion=Motion(
            mode=_str_def(d, "motion", "mode", "off",
                          ("off", "shadow", "live")),
            horizon_s=_num_def(d, "motion", "horizon_s", 2.0, lo=0),
            preview_s=_num_def(d, "motion", "preview_s", 0.2, lo=0),
            dt_s=_num_def(d, "motion", "dt_s", 0.05, lo=0),
            heading_levels=_floats_def(
                d, "motion", "heading_levels",
                (-0.14, -0.09, -0.05, -0.03, -0.015, -0.005,
                 0.0, 0.005, 0.015, 0.03, 0.05, 0.09, 0.14)),
            near_z_m=_num_def(d, "motion", "near_z_m", 6.0, lo=0),
            range_hazard_m=_num_def(
                d, "motion", "range_hazard_m", 70.0, lo=0),
            range_hazard_speed_mps=_num_def(
                d, "motion", "range_hazard_speed_mps", 45.0, lo=0),
            range_hazard_gain=_num_def(
                d, "motion", "range_hazard_gain", 2.0, lo=0),
            corridor_check_s=_num_def(
                d, "motion", "corridor_check_s", 1.0, lo=0),
            collision_margin_m=_num_def(
                d, "motion", "collision_margin_m", 0.05, lo=0),
            corridor_hard_clearance_m=_num_def(
                d, "motion", "corridor_hard_clearance_m", 0.15, lo=0),
            corridor_soft_cost_per_m=_num_def(
                d, "motion", "corridor_soft_cost_per_m", 120.0, lo=0),
            reverse_min_rad=_num_def(
                d, "motion", "reverse_min_rad", 0.03, lo=0),
            corridor_escape_tol_m=_num_def(
                d, "motion", "corridor_escape_tol_m", 0.05, lo=0),
            collision_min_lead_s=_num_def(
                d, "motion", "collision_min_lead_s", 0.35, lo=0),
            near_untimed_lane=_num_def(
                d, "motion", "near_untimed_lane", 0.75, lo=0),
            graze_max_width_m=_num_def(
                d, "motion", "graze_max_width_m", 2.2, lo=0),
            near_cy_margin_px=_num_def(
                d, "motion", "near_cy_margin_px", 120.0, lo=0),
            coin_time_jump_tol_s=_num_def(
                d, "motion", "coin_time_jump_tol_s", 0.25, lo=0),
            cost_collision=_num_def(
                d, "motion", "cost_collision", 60.0, lo=0),
            cost_collision_depth_m=_num_def(
                d, "motion", "cost_collision_depth_m", 120.0, lo=0),
            center_band_lane=_num_def(
                d, "motion", "center_band_lane", 0.3, lo=0),
            center_heading_rad=_num_def(
                d, "motion", "center_heading_rad", 0.03, lo=0),
            max_obj_age_s=_num_def(d, "motion", "max_obj_age_s", 0.25, lo=0),
            corridor_age_max_s=_num_def(
                d, "motion", "corridor_age_max_s", 0.25, lo=0),
            frame_delta_max_m=_num_def(
                d, "motion", "frame_delta_max_m", 0.2, lo=0),
            frame_delta_hold_m=_num_def(
                d, "motion", "frame_delta_hold_m", 1.0, lo=0),
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
            switch_infeasible_grace_s=_num_def(
                d, "motion", "switch_infeasible_grace_s", 0.1, lo=0),
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
            graze_gap_lane=_num_def(d, "selection", "graze_gap_lane", 0.68,
                                    lo=0),
            center_band_lane=_num_def(d, "selection", "center_band_lane", 0.3,
                                      lo=0),
            opportunity_tol_lane=_num_def(
                d, "selection", "opportunity_tol_lane", 0.75, lo=0),
            coin_pick_tol_lane=_num_def(
                d, "selection", "coin_pick_tol_lane", 1.2, lo=0),
            coin_hold_s=_num_def(d, "selection", "coin_hold_s", 0.6, lo=0),
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
            ego_half_lane=_num_def(d, "selection", "ego_half_lane", 0.27, lo=0),
            lane_clearance_lane=_num_def(
                d, "selection", "lane_clearance_lane", 0.06, lo=0)),
        coarse=Coarse(
            control=_bool(d, "coarse", "control", True),
            lookahead_m=_num_def(d, "coarse", "lookahead_m", 24.0, lo=0),
            max_offset_lane=_num_def(
                d, "coarse", "max_offset_lane", 2.0, lo=0),
            collision_margin_m=_num_def(
                d, "coarse", "collision_margin_m", 0.5, lo=0),
            range_speed_mps=_num_def(
                d, "coarse", "range_speed_mps", 45.0, lo=0),
            switch_margin_m=_num_def(
                d, "coarse", "switch_margin_m", 4.0, lo=0),
            align_tol_lane=_num_def(
                d, "coarse", "align_tol_lane", 0.4, lo=0),
            far_direction=_bool(d, "coarse", "far_direction",
                                default=False)),
        route=Route(
            control=_bool(d, "route", "control", default=False),
            urgent_confirm_ticks=_int_def(d, "route", "urgent_confirm_ticks",
                                          3, lo=1),
            opportunity_confirm_ticks=_int_def(
                d, "route", "opportunity_confirm_ticks", 6, lo=1),
            cancel_unsafe_ticks=_int_def(d, "route", "cancel_unsafe_ticks",
                                         5, lo=1),
            cancel_cooldown_s=_num_def(d, "route", "cancel_cooldown_s", 2.0,
                                       lo=0, lo_open=False),
            finish_v_tol_lane_s=_num_def(d, "route", "finish_v_tol_lane_s",
                                         0.4, lo=0),
            merge_max_s=_num_def(d, "route", "merge_max_s", 4.0, lo=0),
            soft_car=_bool(d, "route", "soft_car", default=False),
            reject_shift_arrival=_bool(
                d, "route", "reject_shift_arrival", default=False),
            urgent_ttc_s=_num_def(d, "route", "urgent_ttc_s", 1.0, lo=0),
            urgent_blind_m=_num_def(d, "route", "urgent_blind_m", 6.0, lo=0),
            merge_safe_ttc_s=_num_def(
                d, "route", "merge_safe_ttc_s", 0.8, lo=0),
            pass_hold=_bool(d, "route", "pass_hold", default=True),
            overtake=_bool(d, "route", "overtake", default=True),
            overtake_ttc_s=_num_def(
                d, "route", "overtake_ttc_s", 2.5, lo=0)),
        geometry=Geometry(
            far_ground_supply=_bool(d, "geometry", "far_ground_supply",
                                    default=True)),
        metric_cars=MetricCars(
            enabled=_bool(d, "metric_cars", "enabled", default=True)),
        metric_bridge=MetricBridge(
            enabled=_bool(d, "metric_bridge", "enabled", default=False),
            stay=_int_def(d, "metric_bridge", "stay", 2, lo=1)),
        box_bridge=BoxBridge(
            enabled=_bool(d, "box_bridge", "enabled", default=False),
            trace_only=_bool(d, "box_bridge", "trace_only", default=True),
            anchor_gate=_bool(d, "box_bridge", "anchor_gate", default=False)),
        view_guard=ViewGuardConfig(
            enabled=_bool(d, "view_guard", "enabled", default=False),
            cam_h_lo_m=_num_def(d, "view_guard", "cam_h_lo_m", 1.12,
                                lo=0, lo_open=False),
            cam_h_hi_m=_num_def(d, "view_guard", "cam_h_hi_m", 1.55,
                                lo=0, lo_open=False),
            sample_s=_num_def(d, "view_guard", "sample_s", 0.7,
                              lo=0, lo_open=False),
            settle_s=_num_def(d, "view_guard", "settle_s", 0.8,
                              lo=0, lo_open=False),
            max_views=_int_def(d, "view_guard", "max_views", 3, lo=1),
            min_samples=_int_def(d, "view_guard", "min_samples", 3, lo=1),
            deadline_s=_num_def(d, "view_guard", "deadline_s", 10.0,
                                lo=0, lo_open=False),
            arm_timeout_s=_num_def(d, "view_guard", "arm_timeout_s", 8.0,
                                   lo=0, lo_open=False)),
        debug_dump=DebugDump(
            mode=_dump_mode,
            ring=_int_def(d, "debug_dump", "ring", 10, lo=0),
            event_window=_int_def(d, "debug_dump", "event_window", 20, lo=0)),
        lane_obs=LaneObs(
            enabled=_bool(d, "lane_obs", "enabled", default=False),
            weights=str(d.get("lane_obs", {}).get("weights", "") or ""),
            hz=_num_def(d, "lane_obs", "hz", 0.0, lo=0, lo_open=False),
            provider=str(d.get("lane_obs", {}).get("provider", "cpu") or "cpu"),
            threads=_int_def(d, "lane_obs", "threads", 4, lo=1),
            detect_every=_int_def(d, "lane_obs", "detect_every", 1, lo=1)))

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
    if cfg.route.control and not cfg.coarse.control:
        raise ValueError(
            "[route] control=true 要求 [coarse].control=true：路线的候选来自粗层"
            "逐道净空读数，闸关时定案没有输入（承诺层空转，控制链却已换源）")
    if cfg.lane_obs.enabled and not cfg.lane_obs.weights:
        raise ValueError(
            "[lane_obs] enabled=true 需要 weights 指向 ONNX 权重"
            "（权重不入库，放在本机数据目录并由配置给出路径）")
    if cfg.lane_obs.provider not in ("dml", "cpu"):
        raise ValueError(
            f"[lane_obs] provider={cfg.lane_obs.provider!r} 需为 dml 或 cpu")
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
