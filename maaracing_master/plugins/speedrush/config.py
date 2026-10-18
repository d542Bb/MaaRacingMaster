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
    # 横向轨迹采样评分闸（阶段二设计稿 §2.3，默认关=现役栈逐拍不变）：开闸后
    # 决策输出从 x_target 位置常量换为采样轨迹的前瞻点（滚动重评，破判 ABORT）
    trajectory_sampling: bool = False


@dataclass(frozen=True)
class GridVeto:
    """S2-B 栅格裁决行为闸（三检验 2026-10-05 全绿后接线；整段可选，老 json
    不带段=全默认=关，与 trajectory_sampling 同一条兼容纪律）。

    语义：开闸后轨迹采样器对每条候选按「近场带 z 3~9m × 轨迹横向包络」查
    可行驶栅格——压 blocked 一票否决、unknown 每格小代价、drivable 零。
    margin_lane 是检验 C 量出的边界抖动内收下限；数值回放校准只改 json。"""

    enabled: bool = False
    margin_lane: float = 0.5      # 内收下限（道）＝检验 C 边界抖动量化
    car_half_lane_m: float = 0.9  # 车半宽（米）
    k_unknown: float = 0.05       # 每格 unknown 代价（回放校准起值）


@dataclass(frozen=True)
class PlanLayer:
    """三类字典序计划层行为闸（设计稿计划层落地 f9d4944
    v2；整段可选，老 json 不带段=全默认=关，与 grid_veto 同一条兼容纪律）。

    语义：开闸后 decision._select 分流到 plan.PlanSelector——bonus＞极限超车＞
    coin 字典序选计划、CHANGE 改判走慢拍+持续性门槛；min_score/life_tau/
    switch_margin 退出选择链。数值（K）是整数持续性计数，不是标定权重。"""

    enabled: bool = False
    switch_streak: int = 2        # 换计划持续性门槛 K（连续 K 次慢拍重评居其位）


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
    t_cool_pass_s: float     # pass 落定后的短冷却（19:44 复盘：pass 高频，一律 1s 会
                              # 烧掉 ~30% 对局时间且诱发左右横跳；金币完成仍用 1s）
    ttc_dodge_s: float       # 本车道前车紧急回避的到站时间界（第七轮 2029 局：
                              # 冷却/无候选盲持期间同车道逼近的车是纯碰撞等待，
                              # near_dx 实测 0.00~0.04 道；绕过评分与冷却强制变道）


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
    # —— 路锚卫生（2026-09-26 12:39/13:29 两局复盘：骑缘拍毒化参考系；陈旧帧+
    #    拍数门复位太慢=0 杆直开怼墙）——
    anchor_max_off: float      # 锚形成门：|road_offset| ≤ 此值才形成参考系 [需实测]
    anchor_stale_s: float      # 距上次被接受修正超此秒数（含断供）→ 陈旧重基到路上 [需实测]
    # —— 维护性转向权限（2026-09-26 16:44 局复盘：定中心全权 PD=满舵绕桩，
    #    257/267 饱和拍在保持态、速度起不来）——
    hold_deadband_lane: float  # 死区：|前瞻误差| 小于此不发维护力（车道单位）[需实测]
    hold_stick_max: float      # 维护性转向（定中心/保持/回稳）杆幅上限，CHANGE 全权 [需实测]


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


@dataclass(frozen=True)
class Validate:
    t_empty_s: float
    t_recover_s: float
    t_conserve_max_s: float
    x_lane_abs_max: float
    cy_jump_max_px: float
    # —— 横向几何安全 veto（阶段一 2026-10-02，RSS 形状外挂判据）——
    lat_veto_gap_lane: float        # 接触界：预测最小车距低于此即 veto（车身中心距）
    lat_veto_horizon_s: float       # 自车扫掠包络的判距视野（街车静态，无外推视野）


@dataclass(frozen=True)
class CorridorReach:
    """走廊收益场选路径参数（2026-10-14 设计稿 §3.2-§3.4：米制连续坐标综合
    收益评估，解耦车道定义；整段可选，老 json 不带段=全默认=关，与
    plan_layer 同一条兼容纪律）。

    语义：`enabled` 开闸后引擎逐拍算 ReachReport（corridor_reach.evaluate）
    落 trace cr_* 对照列；`control` 是行为闸——CRUISE 选择来源换血为走廊
    收益场（select:reach_win），回滚=false 即回 legacy band/feas 链（逐位）。
    车道换算件（timing 变道耗时 need_of_dx_m、lane_w_m）在 decision.py 进
    决策边界注入，本表不带车道单位。"""

    enabled: bool = False
    control: bool = False
    trigger_dead_zone_lane: float = 0.35  # reach_win 触发死区（道）——独立于
    step_m: float = 0.5                  # 候选步进（米，与栅格格宽同量级）
    horizon_s: float = 2.0               # 收益前瞻（s）
    crash_t_s: float = 0.8               # 到站短于此判碰撞（s）
    crash_penalty: float = 100.0         # 碰撞候选负额（分）
    pass_value: float = 30.0             # 错身保底（分）
    far_coin_weight: float = 0.3         # 前瞻窗外币的远权
    tie_margin: float = 20.0             # 平手阈（分）：差 < 此值偏向走廊中心
    # ---- R3 贴身猎车（2026-10-16 设计稿 §2.2/§3；默认关=逐位回今天）----
    overtake_hunting: bool = False       # 贴身猎车总闸（false=逐位回今天）
    hug_value: bool = False              # R3 贴身收益分开关（shadow→实机 A/B 分刀验收）
    hug_half_width_lane: float = 0.075   # 贴身平台窗半宽（道）：d_hold_lo/hi =
                                         #   d_hold_lane ∓ 半宽（派生值，非独立真源）；
                                         #   窗整体落在归属半宽（0.75 道）内 = in-path 区，
                                         #   占用/遮挡/碰撞判据对窗内前车照旧，只有价值面翻转
    # ---- R2 前瞻外方向权（2026-10-16 设计稿 §2.4；默认关；R3 的 hug 参数是
    #      其 E 曲线前提，两分开关独立验收）----
    far_direction: bool = False          # R2 远车方向权分开关
    far_car_weight: float = 0.0          # 远车方向权（标定对象随胜出聚合语义定，
                                         #   不预先单选）
    hunt_horizon_s: float = 6.0          # 前瞻外作用域上界（s，[需实测]；近场
                                         #   horizon_s 内是 R3 的地盘不双计）
    far_aggregation: str = "max"         # 多车聚合三语义：max | sum | capped
                                         #   （capped 上限=value_limit+pass_value）；
                                         #   影子对比后定档
    # ---- R5 四层优先级（2026-10-17 设计稿 §9；默认关=逐位回今天）----
    bonus_value: bool = False            # 层 1 奖励车项分开关（false=逐位回今天）
    bonus_car_value: float = 120.0       # 奖励车收益分（起值=一次极限动作分 [需实测]）
    hug_visual: bool = False             # 层 2 擦边倾向：hug 分支扩到像素仪器车
                                         #   （t_overtake 缺位回退 t_meet_s 过同一门）
    bonus_tol_m: float = 0.75            # 奖励车归属半容差（米，近远场同源，R5 §10；
                                         #   起值≈0.22 道 [需实测]：宽了平价 120 摊成
                                         #   平台被平手阈拉回路心，窄了平台只剩 2~3
                                         #   候选、胜出位落源 ±0.5m 内且 30>20 真胜出）


@dataclass(frozen=True)
class MetricCars:
    """车米制状态供数闸（2026-10-12 C 线：车世界模型搬米制系，计划稿 §5C）。

    语义：`enabled` 开闸后 `WorldObservation.metric_cars` 喂道评分与 ledger
    米制列；`fusion` 是 V2 行为闸（计划稿 §6）——dodge ttc 与横向 veto 危险
    窗吃米制融合（dodge 取两仪器较小到站、veto 截断取较晚到站，各自保守向），
    回滚=false 即纯像素旧行为。闸关=影子列回像素仪器，与旧 trace 逐位同构。
    米制量本身由锚点链路自况有效性（无锚=UNKNOWN 弃权），本闸只管供不供。"""

    enabled: bool = True
    fusion: bool = False
    # 米制-only 回避闸（2026-10-18 用户定案）：像素检测死区/雨天让近车掉出
    # 像素车轨（trace 151206_p2 实测 45% 拍无车轨、正前 1.6m 车仅米制位、
    # 到站 0.03s 回避零触发），回避盲区恰在最近处。开闸=米制-only 车（无
    # 像素轨、有米制横位+到站）进回避候选；关闸=只落 trace 对照列，行为
    # 逐拍不变。
    dodge: bool = False


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
class DebugDump:
    """调试图证据包三态闸（2026-10-12 设计裁决：消费者=时刻配图核验）。

    off：深度观测器不建 debug_dir，证据包零落盘。events：worker 内存 ring
    留最近 ``ring`` 次捕获，push 快照（滞后 1 拍）报事件——state=CONSERVE
    或 reason 含 dodge——时落盘 ring+后续 ``event_window`` 帧；真正消费的
    是 dodge/conserving 时刻±窗口，恒开全量是探针期残留。full：现状全量
    0.5s 节流（感知排错期用）。"""

    mode: str = "events"
    ring: int = 10
    event_window: int = 20


@dataclass(frozen=True)
class Commitment:
    """决策承诺制闸（2026-10-15 设计稿 v2 §3 + §8 需求面承诺制）。
    整段可选，老 json 不带段=全默认=关（与 grid_veto 同一条兼容纪律）。

    pause 语义（Track 1，v2 §3）：traj 采样连续破判达 _REEVAL_STREAK 后，
    预算内**暂停**——保留原改道任务、x_target 冻结在 executed（绕开平滑器，
    防朝旧 goal 漂移）；预算=单次连续阻塞时长（dt_s 累计，traj 恢复一拍清零、
    新集清零），超预算才作废（旧行为）。安全不承诺（硬边界不变量 §3.2）：
    lat_veto/致命校验/目标过期判定先行，不受预算延迟；executed 缺席拍不进
    暂停。

    demand/memory 语义（§8 需求面承诺制，只作用于 corridor_reach 集）：
    demand_enabled 锁定追逐意图（select 拍场选点 d0），完成/放弃判据改吃
    意图而非滚动选点——显式放弃门=收益依据死亡（带内空窗 basis_grace_s 且
    场选点带外 k_out 拍）与坐标系断裂（reanchor_break）；升级门=带外更优目
    的地连续 k_up 拍超在场带候选 tie_margin。memory_enabled=报告缺席记忆
    （v2 §4 Track 2 并入）：缺席预算内保留意图、x_target 冻结 executed，
    **生效前提=意图在场**（无意图无可保留，缺席照旧单拍取消）。全部安全
    判定（fatal/veto/validate）不承诺、逐拍先行，与 pause 同一条红线。"""

    pause_enabled: bool = False
    pause_budget_s: float = 1.0     # [需实测] 起值≈一个币窗；先例（Nav2 恢复
                                    # 阶梯）只支撑「等临时遮挡」方向，不支撑
                                    # 数值——A/B 后归位只改本 json
    demand_enabled: bool = False    # 需求面承诺制总闸（§8.2 意图生命周期）
    memory_enabled: bool = False    # 缺席记忆（§8.4.4；前提=意图在场）
    memory_budget_s: float = 0.5    # [需实测] 记忆许可上限不是风险证明：
                                    # ~150km/h 下 0.5s ≈ 20.8m 纵向（v2 §4）
    basis_grace_s: float = 0.5      # [需实测] 收益依据空窗容忍（N_basis）；
                                    # 依据=币组流 ~1 组/s 的组间空窗观测
    reanchor_eps_lane: float = 0.2  # [需实测] 同坐标系噪声阈（低于不换版）
    reanchor_break_lane: float = 0.5  # [需实测] 坐标系断裂界（超=放弃不追）
    k_stay: int = 2                 # [需实测] 次判据贴车拍数（_REEVAL_STREAK
                                    # 同源防抖纪律）
    k_out: int = 2                  # [需实测] 场选点带外拍数（与空窗合判死）
    k_up: int = 2                   # [需实测] 升级连续拍数（防滚动抖动搬进
                                    # 升级判据）


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
    grid_veto: GridVeto = GridVeto()
    plan_layer: PlanLayer = PlanLayer()
    grid_xcheck: GridXcheck = GridXcheck()
    # （lane_scoring 段已退役删除 20261015：被 corridor_reach 取代）
    corridor_reach: CorridorReach = CorridorReach()
    metric_cars: MetricCars = MetricCars()
    metric_bridge: MetricBridge = MetricBridge()
    debug_dump: DebugDump = DebugDump()
    commitment: Commitment = Commitment()


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
            allow_all_moves=_bool(d, "mode", "allow_all_moves"),
            allow_car_graze=_bool(d, "mode", "allow_car_graze"),
            # allow_overtake 缺省 False：老 json 不带键也安全（超车候选默认不进集）
            allow_overtake=_bool(d, "mode", "allow_overtake", default=False),
            # trajectory_sampling 缺省 False：老 json 不带键也安全（现役栈逐拍不变）
            trajectory_sampling=_bool(d, "mode", "trajectory_sampling",
                                      default=False)),
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
            lat_veto_gap_lane=_num(d, "validate", "lat_veto_gap_lane", lo=0),
            lat_veto_horizon_s=_num(d, "validate", "lat_veto_horizon_s", lo=0)),
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
            hold_stick_max=_num(d, "planner", "hold_stick_max", lo=0, hi=1)),
        overtake=Overtake(
            value_base=_num(d, "overtake", "value_base", lo=0),
            value_limit=_num(d, "overtake", "value_limit", lo=0),
            p_center_lane=_num(d, "overtake", "p_center_lane", lo=0),
            p_scale_lane=_num(d, "overtake", "p_scale_lane", lo=0, lo_open=False),
            d_hold_lane=_num(d, "overtake", "d_hold_lane", lo=0, lo_open=False),
            lane_band_lo=_num(d, "overtake", "lane_band_lo", lo=0),
            lane_band_hi=_num(d, "overtake", "lane_band_hi", lo=0),
            t_pass_max_s=_num(d, "overtake", "t_pass_max_s", lo=0, lo_open=False),
            space_margin_lane=_num(d, "overtake", "space_margin_lane", lo=0),
            t_cool_pass_s=_num(d, "overtake", "t_cool_pass_s", lo=0),
            ttc_dodge_s=_num(d, "overtake", "ttc_dodge_s", lo=0)),
        traffic=Traffic(
            exit_margin_px=_num(d, "traffic", "exit_margin_px", lo=0),
            presumed_cy_px=_num(d, "traffic", "presumed_cy_px", lo=0),
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
            width_ttl_ticks=_int_def(d, "traffic", "width_ttl_ticks", 6, lo=0)),
        grid_veto=GridVeto(
            enabled=_bool(d, "grid_veto", "enabled", default=False),
            margin_lane=_num_def(d, "grid_veto", "margin_lane", 0.5,
                                 lo=0, lo_open=False),
            car_half_lane_m=_num_def(d, "grid_veto", "car_half_lane_m", 0.9,
                                     lo=0),
            k_unknown=_num_def(d, "grid_veto", "k_unknown", 0.05, lo=0)),
        plan_layer=PlanLayer(
            enabled=_bool(d, "plan_layer", "enabled", default=False),
            switch_streak=_int_def(d, "plan_layer", "switch_streak", 2, lo=1)),
        grid_xcheck=GridXcheck(
            mode=_str_def(d, "grid_xcheck", "mode", "off",
                          ("off", "master", "veto")),
            tol_lane=_num_def(d, "grid_xcheck", "tol_lane", 0.45, lo=0),
            bypass_streak=_int_def(d, "grid_xcheck", "bypass_streak", 8, lo=1)),
        corridor_reach=CorridorReach(
            enabled=_bool(d, "corridor_reach", "enabled", default=False),
            control=_bool(d, "corridor_reach", "control", default=False),
            trigger_dead_zone_lane=_num_def(d, "corridor_reach",
                                            "trigger_dead_zone_lane", 0.35,
                                            lo=0),
            step_m=_num_def(d, "corridor_reach", "step_m", 0.5, lo=0),
            horizon_s=_num_def(d, "corridor_reach", "horizon_s", 2.0, lo=0),
            crash_t_s=_num_def(d, "corridor_reach", "crash_t_s", 0.8, lo=0),
            crash_penalty=_num_def(d, "corridor_reach", "crash_penalty",
                                   100.0, lo=0),
            pass_value=_num_def(d, "corridor_reach", "pass_value", 30.0, lo=0),
            far_coin_weight=_num_def(d, "corridor_reach", "far_coin_weight",
                                     0.3, lo=0, hi=1, lo_open=False),
            tie_margin=_num_def(d, "corridor_reach", "tie_margin", 20.0, lo=0),
            overtake_hunting=_bool(d, "corridor_reach", "overtake_hunting",
                                   default=False),
            hug_value=_bool(d, "corridor_reach", "hug_value", default=False),
            hug_half_width_lane=_num_def(d, "corridor_reach",
                                         "hug_half_width_lane", 0.075, lo=0),
            far_direction=_bool(d, "corridor_reach", "far_direction",
                                default=False),
            far_car_weight=_num_def(d, "corridor_reach", "far_car_weight",
                                    0.0, lo=0, lo_open=False),
            hunt_horizon_s=_num_def(d, "corridor_reach", "hunt_horizon_s",
                                    6.0, lo=0),
            far_aggregation=_str_def(d, "corridor_reach", "far_aggregation",
                                     "max", ("max", "sum", "capped")),
            bonus_value=_bool(d, "corridor_reach", "bonus_value",
                              default=False),
            bonus_car_value=_num_def(d, "corridor_reach", "bonus_car_value",
                                     120.0, lo=0),
            hug_visual=_bool(d, "corridor_reach", "hug_visual",
                             default=False),
            bonus_tol_m=_num_def(d, "corridor_reach", "bonus_tol_m",
                                 0.75, lo=0)),
        metric_cars=MetricCars(
            enabled=_bool(d, "metric_cars", "enabled", default=True),
            fusion=_bool(d, "metric_cars", "fusion", default=False),
            dodge=_bool(d, "metric_cars", "dodge", default=False)),
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
            event_window=_int_def(d, "debug_dump", "event_window", 20, lo=0)),
        commitment=Commitment(
            pause_enabled=_bool(d, "commitment", "pause_enabled", default=False),
            pause_budget_s=_num_def(d, "commitment", "pause_budget_s", 1.0,
                                    lo=0, lo_open=False),
            demand_enabled=_bool(d, "commitment", "demand_enabled", default=False),
            memory_enabled=_bool(d, "commitment", "memory_enabled", default=False),
            memory_budget_s=_num_def(d, "commitment", "memory_budget_s", 0.5,
                                     lo=0, lo_open=False),
            basis_grace_s=_num_def(d, "commitment", "basis_grace_s", 0.5,
                                   lo=0, lo_open=False),
            reanchor_eps_lane=_num_def(d, "commitment", "reanchor_eps_lane",
                                       0.2, lo=0, lo_open=False),
            reanchor_break_lane=_num_def(d, "commitment", "reanchor_break_lane",
                                         0.5, lo=0, lo_open=False),
            k_stay=_int_def(d, "commitment", "k_stay", 2, lo=1),
            k_out=_int_def(d, "commitment", "k_out", 2, lo=1),
            k_up=_int_def(d, "commitment", "k_up", 2, lo=1)))

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
    if cfg.validate.lat_veto_gap_lane >= ov.d_hold_lane:
        raise ValueError(
            f"[validate] lat_veto_gap_lane={cfg.validate.lat_veto_gap_lane} "
            f"≥ 贴窗中心 d_hold_lane={ov.d_hold_lane}"
            f"（veto 接触界不小于贴窗距离=一切超车都被拦，判据矛盾）")
    cmt = cfg.commitment
    if cmt.reanchor_break_lane <= cmt.reanchor_eps_lane:
        raise ValueError(
            f"[commitment] reanchor_break_lane={cmt.reanchor_break_lane} "
            f"必须 > reanchor_eps_lane={cmt.reanchor_eps_lane}"
            f"（eps~break 之间重锚、超 break 断裂——倒置则断裂门永不触发，判据矛盾）")
    cr = cfg.corridor_reach
    if cr.overtake_hunting and cr.hug_value \
            and not 0 < cfg.overtake.d_hold_lane - cr.hug_half_width_lane:
        raise ValueError(
            f"[corridor_reach] 贴身窗半宽 {cr.hug_half_width_lane} ≥ 平台中心 "
            f"d_hold_lane={cfg.overtake.d_hold_lane}"
            f"（窗下沿=d_hold−half 必须 >0，0 < lo < center ≤ hi，设计稿 §2.2）")
    return cfg


@lru_cache(maxsize=1)
def load_decision() -> DecisionConfig:
    """读行为参数真源（进程内一次；改文件需重启或 clear cache——回放/实机都是
    开局读取，无热更需求，设计稿 §五）。"""
    return _read_decision(DECISION_FILE)
