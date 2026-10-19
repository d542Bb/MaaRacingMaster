# -*- coding: utf-8 -*-
"""阶段 B 跟踪层与世界观测契约（设计稿 v2 §一 / §八 step 2）。

**分层位置**：`PerceptionResult` ─▶〔本模块：近邻关联 + 状态保持〕─▶ `WorldObservation`
（决策层的唯一输入）。本模块**不评分、不选道、不发计划**——`DecisionOutput` 在此
只锁契约形状，其生产者属 §八 step 4。

**ID 与重识别规则（v1 最小实现）**：关联 = 逐类贪心最近邻（门限与代价值见
`_gate_cost`）；命中即延续（first_seen 不改），失联在宽限窗 `grace_ticks` 内保持
输出，**过期即退役、id 永不复用**——退役后同位再现按新检出处理（重识别口径是
§七.5 前置测量项，v1 不臆造关联）。

**帧号契约（step 5 回放修正）**：`frame_id` 允许**相等重复**、拒绝倒退——采集端
WGC 是中心缓存，主循环 ~21Hz 读 30Hz 画面合法地两拍读到同一帧（replay 实测
抓到，若不修实机接线第一天就炸）。重复 fid 的第二次输入**不重关联、不计老化、
不改 rel_approach**（同一帧喂两次会把速度估成 0），只按既有状态重发观测；
倒退（真乱序）仍然 fail-loud。

**外推纪律**（v2 §二）：遮挡期纵向（cy）按 `rel_approach` 匀速外推，横向（x_lane）
**不外推**——保持最后一次域内读数，`FarTarget.last_x_lane` 承载它。外推 cy 只用于
关联门控与距离序；每份输出都带 `validity_until_fid`，消费方自行处理有效期。

**不确定性模型**：`x_sigma = pos_err_px × a_x / (cy − y_h)`——透视除法把像素级
误差按分母放大。`pos_err_px` 起值 15px（§6 口径：场内 vpx 误差 MAD 12–19 为主项），
代入分母 20–50 得 σ ≈ 0.24–0.59 车道，与 §6 误差注记（±0.2–0.5）同量级、同来源。
参数经 `TrackerParams` 注入，§八 step 4 落 decision.json 后本文件不留可调字面量。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum, IntEnum
from typing import Any

from maaracing_master.plugins.speedrush.depth_geo import DepthAnchorSheet
from maaracing_master.plugins.speedrush.perception import PerceptionResult
from maaracing_master.plugins.speedrush.world_model import (
    KIND_BONUS, KIND_CAR, KIND_COIN, Calib, load_calib, t_meet_car_s,
    x_lane_of)

SCHEMA_VERSION = 1

_KIND_ATTRS = ((KIND_COIN, "coins"), (KIND_CAR, "cars"), (KIND_BONUS, "bonuses"))


class DecisionState(str, Enum):
    """状态集（2026-10-18 换血：CRUISE 场跟随 / ABORT 有界回稳 / CONSERVE /
    FAULT；CHANGE 与 ABORT_CHANGE 随承诺制退役删除）。"""

    CRUISE = "CRUISE"
    ABORT = "ABORT"
    CONSERVE = "CONSERVE"
    FAULT = "FAULT"


class LaneSide(IntEnum):
    """远处目标的粗方向档（cx 相对消失点，中性带 `neutral_px` 内算中列）。"""

    LEFT = -1
    MID = 0
    RIGHT = 1


@dataclass(frozen=True)
class PerceptionHealth:
    """五个独立可测信号（v2 §四：进保守态要的是合取，任何单项都不作数）。
    观测侧由本层如实填报；判定与进出保守态属校验层（step 4）。"""

    perception_alive: bool   # 本 tick detect() 完成且被消费
    frame_fresh: bool        # frame_age_ms ≤ fresh_max_ms
    geometry_valid: bool     # v1 = 调用方传入（边界层落地后由 BoundarySummary 推导）
    target_presence: bool    # 本帧观测含近场或远场目标
    stage_transition: bool   # 阶段切换/起步/过场窗口（调用方传入，锚点序列可判）


@dataclass(frozen=True)
class BoundarySummary:
    """边界感知层摘要契约。v1 **无生产者**（边界层未落地），
    `WorldObservation.boundary` 恒为 None；路缘不参与目标评分（v2 §三），
    其消费者只有校验层与横向位移代价项。"""

    schema_version: int
    left_x: float
    right_x: float
    road_width: float
    straight_residual: float
    vp_row: float | None
    validity: bool
    uncertainty: float
    # 稳定跟踪到的路缘侧数（0/1/2）。单侧容忍（step 6）：sides≥1 即 validity，
    # 但 sides==1 时 road_width/vp_row 不可用（需双侧）——消费者据 sides 决定是否
    # 用居中/路宽类量，不得假设单侧帧有完整路几何。
    sides: int = 2
    # 两边线交点的横向位置（需 sides==2；与 vp_row 同门控同缺省）。与校准 vpx 的
    # 横偏 ∝ 车头航向角——真机证据链（2026-09-22）：打舵画面是旋转不是平移，
    # 平移假设下边界检测恰在控制动作时失明；vp_x 是航向的免费观测量，v2 trace
    # 落列供 C1/C2 定档，planner 现值不消费（不建立第二份真相：消费时由观测层给）。
    vp_x: float | None = None
    # 左右路缘的 A1 读数（带顶行，左负右正；该侧稳定才有值，否则 None）。
    # 消费者=超车空间闸门（设计稿 §四补：决策默认左右对称，禁用某方向必须拿
    # "那侧没空间"的显式证据——None=证据不足=两侧都放行）。
    left_edge_lane: float | None = None
    right_edge_lane: float | None = None
    # 全部可见黄线簇的车道坐标（升序，schema 3，2026-09-28）——**车道栅格本身**:
    # 簇=车道边界线,间隙=车道,自车车道=包含 x=0 的间隙。配对(left/right)只是
    # 路宽级两簇的特例;配对宽度天然多模态(212734 局实证:拍间 325→204→518px
    # 乱跳),任何"路宽常数"先验对它都不成立——车道归属必须走间隙,不走配对宽度。
    clusters: tuple[float, ...] = ()


@dataclass(frozen=True)
class TrackedTarget:
    """适用域内（有横向读数）的目标。"""

    id: int
    kind: str                    # KIND_COIN / KIND_CAR / KIND_BONUS
    x_lane: float
    x_sigma: float
    cy: int                      # 遮挡延续时为外推值（见 last_seen 与 docstring 外推纪律）
    w: int
    h: int
    conf: float
    rel_approach: float          # px/fid（Δcy/Δfid 的 EMA），正 = 接近。
                                 # 非 px/tick：decision 侧乘 hz(20/s) 消费=
                                 # 恒定高估 TTC ~1.9x，经验阈值已吸收
                                 # （设计稿 §4），勿单独修单位
    first_seen_fid: int
    last_seen_fid: int
    validity_until_fid: int      # 宽限截止帧号（last_seen + grace）


@dataclass(frozen=True)
class FarTarget:
    """域外（归一发散不可信）的目标：只给粗方向与距离序，不给横向数值。"""

    id: int
    kind: str
    lane_side: int               # LaneSide 值：-1 / 0 / +1
    cy: int
    conf: float
    h: int                       # 框高（px）——混合测距门控的 2D 量（trace 供数；
                                 # 域外车恰是门控最需要覆盖的区段，契约加法扩展）
    last_x_lane: float | None    # 进域前的最后读数（从未进域则 None）
    first_seen_fid: int
    last_seen_fid: int
    validity_until_fid: int
    cx: int = 0                  # 像素位（R5 §10 契约加法：远场粗横向的 bearing
                                 # 源，_Track 已有直读；0=构造方未供，消费端自决）
    rel_approach: float = 0.0    # px/fid（同 TrackedTarget，全 kind 配对轨都更新；
                                 # 远场粗源的到站仪器输入，正=接近）


@dataclass(frozen=True)
class CoinGroup:
    """金币组（v2 §三）：由 coin_group.CoinGroupAggregator 聚合产出，
    经 attach 进 WorldObservation.coin_groups。评分器只吃组、不自行聚类。"""

    group_id: int
    member_ids: tuple[int, ...]        # 组内 TrackedTarget.id（跨帧由跟踪 id 维系）
    observed_count: int
    estimated_count: int               # v1 = observed（漏检折算待 §7.2 实测组内间距后升级）
    x_center: float
    x_span: float                      # 组横向占位（车道单位）
    cy_min: int                        # 远端
    cy_max: int                        # 近端
    conf_min: float
    partial_observation: bool          # 组内出现异常大间距 → 疑似漏检（宁可低估收益）
    first_seen_fid: int
    last_seen_fid: int
    validity_until_fid: int


@dataclass(frozen=True)
class ObjectDistance:
    """物体距离状态（混合测距，trace-only 不进决策）。
    契约见混合测距刀一 b385d4b：三态互斥完备；
    UNKNOWN 的 ``metric_distance_m`` 恒为 None（类型层防"有序无米"被当数用）；
    不设连续置信分——没有实验支持的分位数都是编的，弃权是硬边界。

    时间语义（2026-10-08 时间链台账钉死，全部 perf_counter_ns 同钟族）：
    ``anchor_age_ms`` 零点 = 深度帧**采集回调**时刻（sheet.ts=frame_ts_ns，
    非推理完成时刻、非 push 时刻）；``obs_age_ms`` 零点 = 本拍 2D 帧采集时刻。
    决策时刻米制年龄 ≈ anchor_age_ms + 本拍帧龄（trace 顶层 oage，拍级现算）
    ——两零点同族，相加无重复计账。注意与 dgeo_age_ms（零点=push 墙钟的
    服务年龄）不可相加。WGC 重复帧拍整个 obs 冻结（age 不再增长）——重复
    拍的年龄语义以 fid 判重后另行外推，契约不谎报新鲜。"""

    target_id: int
    source: DistanceSource
    metric_distance_m: float | None
    method: DistanceMethod | None
    anchor_age_ms: float | None   # 锚点年龄（深度帧采集距今）；UNKNOWN 无锚时为 None
    obs_age_ms: float             # 本拍 2D 观测年龄——与锚点年龄是两个参考帧（同时钟族）
    reason: DistanceReason | None  # UNKNOWN 时必填
    v_close_mps: float | None = None  # 门控区间速度估计（v̂>0=接近，z(t+Δ)≈m−v̂Δ）。
                                      # 与锚点同生命周期；未知为 None。**定位：已保存
                                      # 的运动观测，不是已验证的运动状态**——稳定性
                                      # 与投影地平线待带 v 的实机 trace 检验（设计稿
                                      # §4 刀二判据），消费面（分带/投影）刀二带闸。
                                      # source 有效 + None = 新鲜锚尚未开评（非异常）


class DistanceSource(str, Enum):
    """出处标签（设计稿 §2）：ANCHOR=锚点生命周期内从未确认接近，直接持锚；
    RESCALED_ANCHOR=曾确认接近，持续 h 重标（进 RESCALED 是单行道，退出唯一
    路径是新锚点重置基线）；UNKNOWN=无有效锚点。"""

    ANCHOR = "ANCHOR"
    RESCALED_ANCHOR = "RESCALED_ANCHOR"
    UNKNOWN = "UNKNOWN"


class DistanceMethod(str, Enum):
    """距离合成方式：HOLD（持锚）/ RANGE_PROP（锚+区间速度外推；2026-10-13
    替代 HEIGHT_RATIO 框高重标——框高抖动直接打进距离，实机生命表证伪）。
    cy 传播与融合仍是未来带实验的候选，没有实验支持的 fallback 不进契约。"""

    HOLD = "HOLD"
    RANGE_PROP = "RANGE_PROP"


class DistanceReason(str, Enum):
    """UNKNOWN 弃权原因（层一=测量有效性，层二=身份有效性，分开答）。"""

    NO_ANCHOR_YET = "NO_ANCHOR_YET"          # 从未锚定（含深度侧无有效清单条目）
    CLUSTER_SWITCH = "CLUSTER_SWITCH"        # 层一：主簇跳变（车体↔背景语义污染）
    ASSOC_FAIL = "ASSOC_FAIL"                # 层二：清单条目归不到轨道
    TRACK_LOST = "TRACK_LOST"                # 层二：本拍丢框（宽限期内锚点保留）
    EXPIRED = "EXPIRED"                      # 锚点超工程闸顶（非物理失效点）


@dataclass(frozen=True)
class MetricCarState:
    """一辆车的米制状态（C 线 2026-10-12：车世界模型搬米制系）。

    全部量在**车头系米制**（右正，x 自路面锚/路心换算由消费端做——本层只供
    自车系）。z/x 都不除以 (cy−y_h)：z 是点云锚实测（锚间框高重标），x_m 是
    同帧焦距反投影——地平线退化带不再特殊。None=该量弃权（原因见 source/
    reason），消费端回退像素仪器，不臆造。

    - z_m：当前米制距离（RESCALED=锚点按区间速度外推，ANCHOR=持锚原值）。
    - x_m = (cx−vpx)·z/fx：cx=当前框心、fx=锚点帧焦距、z=当前米制距离——
      三个量各差半拍（锚点帧 vs 当前帧），横向慢变量，误差二阶，如实记。
    - v_close_mps：锚点门控估计（锚龄 ≥ gate_eval_min_ms 才出，None=估计窗
      未开——消费端此时回退像素 t_meet，不算 pacing）。
    - t_meet_s = z/v_close（v_close ≤ 0 = 掠过/远离 → None，**与「估计窗未开」
      同值不同义**——区分靠 source：source 非 UNKNOWN 且 t None= pacing/远离
      占用语义；source=UNKNOWN=无锚，回退像素）。"""

    target_id: int
    source: DistanceSource
    z_m: float | None
    x_m: float | None
    v_close_mps: float | None
    t_meet_s: float | None
    anchor_age_ms: float | None
    # 速度像素桥（闸 metric_bridge.enabled，默认关=逐位回今天）：米制 t_meet
    # 缺席的拍（UNKNOWN 五因/估计窗未开/丢框保锚）以 rel_approach 纵向行运动
    # 桥接 t_meet（同一公式真源 t_meet_car_s）；bridged=True=本行 t_meet_s 是
    # 桥接值（不确定度走消费端降权/裕度），bridge_reason=进入桥接的原因
    # （UNKNOWN 五因值或 est_window=有锚估计窗未开）。契约加法扩展。
    bridged: bool = False
    bridge_reason: str | None = None


@dataclass(frozen=True)
class WorldObservation:
    schema_version: int
    frame_id: int
    ts_ns: int
    frame_age_ms: float
    stage: int
    health: PerceptionHealth
    boundary: BoundarySummary | None
    targets: tuple[TrackedTarget, ...]     # 近→远（cy 降序）
    far_targets: tuple[FarTarget, ...]     # 近→远
    coin_groups: tuple[CoinGroup, ...] = ()  # 契约扩展（加字段不破消费方，schema 仍 1）
    distances: tuple[ObjectDistance, ...] = ()  # 同上（混合测距刀一；车轨每拍一条）
    # 采集帧率估计（Hz，fid 时基；2026-10-11 车流 t_meet 换基引入）。rel_approach
    # 的单位是 px/fid，换算秒必须知道 fid 率——控制拍率(20)不是 fid 率，旧消费
    # 口径恒定高估 TTC。EMA 预热未达样本数前为 None，消费方自决退路。
    fid_rate_hz: float | None = None
    # 逐车米制状态（C 线 2026-10-12；与 distances 同序同 id 一一对应，只含车）。
    # 影子期消费面=道评分+ledger；行为路径切换属 V2。契约加法扩展，schema 仍 1。
    metric_cars: tuple[MetricCarState, ...] = ()


@dataclass(frozen=True)
class DecisionOutput:
    """决策层→规划层唯一出口（契约形状；v4：机动态权限位）。
    `reason` 是机器可读码串（如 "follow:reach" / "validate_fail:frame_stale"），
    用 str 不用 Enum——码集会随实现扩充，state 才是锁死的枚举。"""

    schema_version: int          # v5：删除 move_allowed（无消费者）
    state: DecisionState
    x_target: float | None
    reason: str
    emitted_fid: int
    valid_until_fid: int         # 含边界：current_fid ≤ 此值为有效（planner 设计稿 §一）
    # 机动态权限位（阶段 2）：目标离自车超过切换死区时置位，planner 据此放弃
    # 维护性死区/杆幅上限（不是承诺、不是完成判据——只作权限档位）
    maneuver: bool = False


@dataclass(frozen=True)
class TrackerParams:
    """跟踪关联参数（起值全部 [需实测·前置 §七.5]，step 4 落 decision.json）。"""

    grace_ticks: int = 15        # 消失宽限：名义 0.5s×30Hz；实测 21Hz 下 ≈0.7s，同量级
    max_cy_step: float = 80.0    # 对 cy 预测的失配门限（px/tick）——检闪烁用
    max_xlane_step: float = 0.5  # 横向跳变门限（车道）：21Hz 下真实目标 <0.05，留足容差
    neutral_px: float = 40.0     # lane_side 中列半宽
    pos_err_px: float = 15.0     # x_sigma 模型的分子（§6 场内 vpx 误差 MAD 12–19）
    fresh_max_ms: float = 100.0  # frame_fresh 判据（v2 §六 P95≤100ms 同源）
    ema_alpha: float = 0.5       # rel_approach 平滑系数
    # ---- 混合测距（trace-only；全部**实验候选参数**，trace 定版前不是系统真理
    # ---- ——依据与口径见混合测距刀一 b385d4b §3/§8.1）
    gate_v_mps: float = 16.0     # 门控阈值：估计接近速度 m/s（trace_213150 标定
                                 # Youden T≈16，物理口径吻合：16m/s×250ms≈4m 持锚误差；
                                 # 2026-10-13 v̂ 换锚间区间差分后为**再标定候选**）
    gate_eval_min_ms: float = 150.0  # 区间下限：短于 此的锚间差分被量化噪声支配
                                     # （实机实测 r≈-0.08），不发布速度
    gate_confirm: int = 2        # 连续确认拍数（与 decision._REEVAL_STREAK 同源纪律）
    anchor_max_age_ms: float = 600.0  # 锚点工程闸顶（非物理失效点；2026-10-13
                                      # 350→600：实测 worker 滞后 P50 250ms/P90
                                      # 334ms，350ms 闸下锚出生即半老、只剩 1~3 拍
                                      # 可用=覆盖率主犯；区间速度+传播后长锚是
                                      # 诚实的，z 误差由 v 精度界）
    cluster_switch_m: float = 20.0    # 层一有效性：锚点簇跳变弃权门（trace 标定候选）
    z_jump_base_m: float = 2.0   # 亚门限翻跳丢弃门（2026-10-13）：相邻锚 |Δz| 超
                                 # `base + mps×dt` 丢弃该锚保旧锚——门限必须随锚间
                                 # dt 缩放（MoGe 卡顿把 dt 拉到 0.4s 时真闭速
                                 # 15m 的正常锚会被定限误杀，轨道饿到超龄——
                                 # 2026-10-13 第三局 trace 实测覆盖率掉 40%→28%）
    z_jump_mps: float = 45.0     # 丢门的闭速界（m/s）：ego 极速量级；超界=量错不是真动
    assoc_window: int = 18       # detection→track 帧映射窗（拍数）：须盖过最大清单
                                 # 滞后（P90 334ms≈13fid@38fps + 余量）——12 曾让
                                 # P90 滞后的整张清单归不了轨（ASSOC_FAIL）

    def __post_init__(self) -> None:
        if (self.grace_ticks < 1 or self.max_cy_step <= 0 or self.max_xlane_step <= 0
                or self.neutral_px <= 0 or self.pos_err_px <= 0
                or self.fresh_max_ms <= 0 or not 0.0 <= self.ema_alpha < 1.0
                or self.gate_v_mps <= 0 or self.gate_eval_min_ms <= 0
                or self.gate_confirm < 1
                or self.anchor_max_age_ms <= 0 or self.cluster_switch_m <= 0
                or self.z_jump_base_m <= 0 or self.z_jump_mps <= 0
                or self.assoc_window < 1):
            raise ValueError(f"跟踪参数非法：{self}")


@dataclass
class _Obs:
    """本帧一条检出目标的几何量（内部结构，不出契约）。"""

    kind: str
    cy: int
    cx: float               # 框心 x（全幅 px，1280 基准）——米制 x_m 反投影的横向原料
    w: int
    h: int
    conf: float
    x_lane: float | None
    x_sigma: float
    lane_side: int
    det_key: tuple[str, int] = ("", -1)   # (kind, kind 内序号)——深度锚点帧映射表的 key


@dataclass
class _Track:
    id: int
    kind: str
    cy: int
    cx: float               # 最近框心 x（全幅 px；匹配拍刷新，遮挡期保持）
    w: int
    h: int
    conf: float
    x_lane: float | None    # 最近读数；遮挡期保持（不外推）
    x_known: float | None   # 历史上最后一次域内读数（进域后不清）
    x_sigma: float
    lane_side: int
    rel_approach: float
    first_seen: int
    last_seen: int
    matched: bool = False
    # ---- 混合测距锚点状态（语义见设计稿 §2/§4；C 线 2026-10-13 起为决策供数）----
    anchor_z: float | None = None        # 米制锚点（None=无有效锚；近簇修正+跳变门后的值）
    anchor_ts_ns: int = 0                # 锚点帧采集时刻（perf_counter_ns）
    anchor_fx: float | None = None       # 锚点帧全幅焦距（x_m 反投影换算锚；随锚生灭）
    v_inst: float | None = None          # 锚到锚区间差分速度（m/s，+ = 接近；publish
                                         # 闸后的 v_close 数据源——log-h 框高式已被
                                         # 实机生命表证伪退役，见 _ingest_anchors）
    approach_confirmed_at: int | None = None   # 本锚点生命周期内确认帧号（≠ is_approaching）
    gate_streak: int = 0                 # 估计接近速度连续超阈拍数
    v_close_mps: float | None = None     # 门控区间速度估计落轨（L2 状态载体）：
                                         # 与锚点同生命周期（新锚/簇跳/超龄即清），
                                         # 消费政策（新鲜度分带/预测投影）在刀二带闸
    last_dist_reason: DistanceReason | None = None
    # ---- 速度像素桥迟滞态（闸 metric_bridge.enabled，默认关不消费）----
    # src_mode=当前供数侧（metric=米制锚 / bridge=像素桥）；src_dwell=当前侧
    # 连续驻留拍数（初值=2：从未锚定的车没有米制驻留可言，首个缺席拍即进桥
    # ——追尾案"从未/刚锚定"场景恰好靠这个进入）；bridge_reason=进入原因，
    # 换回米制侧清零
    src_mode: str = "metric"
    src_dwell: int = 2
    bridge_reason: str | None = None


class Tracker:
    """跨帧目标关联与观测组装。每 tick `update()` 一次，喂一帧感知结果。"""

    def __init__(self, cal: Calib | None = None,
                 params: TrackerParams = TrackerParams(),
                 bridge_enabled: bool = False,
                 bridge_stay: int = 2):
        # cal 缺省读几何真源（gate0.json）；三源分立，代码不写标定字面量
        self.cal = cal if cal is not None else load_calib()
        self.p = params
        # 速度像素桥（闸 metric_bridge.enabled，默认关）：闸与迟滞驻留由
        # decision.json 传入（tracking 层不读配置，配置流向单点在链构建）
        self._bridge_on = bridge_enabled
        self._bridge_stay = bridge_stay
        self._tracks: list[_Track] = []
        self._next_id = 1
        self._last_fid: int | None = None
        self._stage: int | None = None
        self._last_obs: WorldObservation | None = None
        self._frame_tracks: dict[int, dict[tuple[str, int], int]] = {}  # fid → det_key→track_id
        self._sheet_orphan = False   # 本拍清单有条目但整张无法归轨（worker 滞后超窗）
        self._ingest_stats: dict | None = None   # 最近一次清单对账面（trace 消费）
        # 采集帧率估计（fid 时基，2026-10-11 车流 t_meet 换基）：fid 率是采集流
        # 属性，不随 track/stage 生命期走——reset 不清，阶段切换重卷后估计仍在。
        # 聚合式需要估计起点与"当前帧"两个锚，_last_*_seen 每 new frame 更新
        self._rate_fid: int | None = None
        self._rate_ts_ns: int | None = None
        self._last_fid_seen: int | None = None
        self._last_ts_seen: int | None = None
        self._fid_hz_n = 0

    def reset(self) -> None:
        """清空全部轨迹（阶段切换自动调用；校验层出口重置属 step 4 决定调不调）。
        id 计数器不回卷——回放里 id 全局单调才好对账。"""
        self._tracks.clear()
        self._frame_tracks.clear()
        self._sheet_orphan = False
        self._ingest_stats = None

    def update(self, per: PerceptionResult, frame_age_ms: float, stage: int,
               geometry_valid: bool = True, stage_transition: bool = False,
               anchors: DepthAnchorSheet | None = None) -> WorldObservation:
        fid = per.frame_id
        if self._last_fid is not None and fid < self._last_fid:
            raise ValueError(f"frame_id 倒退（真乱序输入）：{fid} < {self._last_fid}")
        if self._last_fid is not None and fid == self._last_fid:
            # WGC 中心缓存的合法重复：同一帧不重关联、不老化、不改速度——原样重发
            if self._last_obs is not None:
                return self._last_obs
        self._update_fid_rate(fid, per.ts_ns)
        if self._stage is not None and stage != self._stage:
            self.reset()
        self._stage = stage
        self._last_fid = fid

        obs = self._observe(per)
        for t in self._tracks:
            t.matched = False   # 上一帧的配对结果不作数：本帧重新关联
        matches = self._associate(obs, fid)

        for ti, oi in matches.items():
            t, o = self._tracks[ti], obs[oi]
            age = fid - t.last_seen
            inst = (o.cy - t.cy) / age if age > 0 else 0.0
            t.rel_approach = self.p.ema_alpha * t.rel_approach + (1 - self.p.ema_alpha) * inst
            # 混合测距门控：区间速度 v̂ 在锚点 ingest 时算好并**即时发布**到
            # v_close（见 _ingest_anchors）；本循环只做 N=2 连续确认（防单锚
            # 尖峰触发接近位）。
            if t.kind == KIND_CAR and t.anchor_z is not None and t.v_inst is not None:
                t.gate_streak = t.gate_streak + 1 if t.v_inst > self.p.gate_v_mps else 0
                if (t.gate_streak >= self.p.gate_confirm
                        and t.approach_confirmed_at is None):
                    t.approach_confirmed_at = fid
            else:
                t.gate_streak = 0
            t.kind, t.w, t.h, t.conf = o.kind, o.w, o.h, o.conf
            t.cy, t.cx = o.cy, o.cx
            t.x_lane, t.x_sigma, t.lane_side = o.x_lane, o.x_sigma, o.lane_side
            if o.x_lane is not None:
                t.x_known = o.x_lane
            t.last_seen = fid
            t.matched = True

        # 混合测距帧映射表（刀一）：detection → track id，供锚点清单归轨道——
        # 深度域只给 (fid, det_idx)，归轨道是本层自己的事（设计稿 §5 不变量：
        # 深度域不认识 track_id，永不该认识）
        frame_map: dict[tuple[str, int], int] = {}
        for ti, oi in matches.items():
            frame_map[obs[oi].det_key] = self._tracks[ti].id
        for oi, o in enumerate(obs):
            if any(v == oi for v in matches.values()):
                continue
            self._tracks.append(_Track(
                id=self._next_id, kind=o.kind, cy=o.cy, cx=o.cx, w=o.w, h=o.h,
                conf=o.conf, x_lane=o.x_lane, x_known=o.x_lane,
                x_sigma=o.x_sigma, lane_side=o.lane_side, rel_approach=0.0,
                first_seen=fid, last_seen=fid, matched=True))
            frame_map[o.det_key] = self._next_id
            self._next_id += 1
        if frame_map:
            self._frame_tracks[fid] = frame_map
            while len(self._frame_tracks) > self.p.assoc_window:
                self._frame_tracks.pop(next(iter(self._frame_tracks)))

        self._tracks = [t for t in self._tracks
                        if fid - t.last_seen <= self.p.grace_ticks]

        if anchors is not None:
            self._ingest_anchors(anchors)
            # 清单有条目却整张归不了轨 = 帧映射窗被 worker 滞后击穿：如实报
            # ASSOC_FAIL（与"深度侧根本没量出锚点"的 NO_ANCHOR_YET 分开）
            self._sheet_orphan = bool(anchors.entries
                                      and anchors.fid not in self._frame_tracks)
            if self._ingest_stats is not None:
                self._ingest_stats["orphan"] = self._sheet_orphan
        else:
            self._sheet_orphan = False
            self._ingest_stats = None       # 本拍无新清单：对账面如实缺位

        targets: list[TrackedTarget] = []
        fars: list[FarTarget] = []
        for t in self._tracks:
            age = fid - t.last_seen
            cy_out = t.cy if age == 0 else self._clamp_cy(t.cy + round(t.rel_approach * age))
            valid_until = t.last_seen + self.p.grace_ticks
            if t.x_lane is not None:
                targets.append(TrackedTarget(
                    id=t.id, kind=t.kind, x_lane=t.x_lane, x_sigma=t.x_sigma,
                    cy=cy_out, w=t.w, h=t.h, conf=t.conf,
                    rel_approach=t.rel_approach, first_seen_fid=t.first_seen,
                    last_seen_fid=t.last_seen, validity_until_fid=valid_until))
            else:
                fars.append(FarTarget(
                    id=t.id, kind=t.kind, lane_side=t.lane_side, cy=cy_out,
                    conf=t.conf, h=t.h, last_x_lane=t.x_known,
                    first_seen_fid=t.first_seen, last_seen_fid=t.last_seen,
                    validity_until_fid=valid_until,
                    cx=t.cx, rel_approach=t.rel_approach))
        targets.sort(key=lambda x: -x.cy)
        fars.sort(key=lambda x: -x.cy)

        health = PerceptionHealth(
            perception_alive=True,
            frame_fresh=frame_age_ms <= self.p.fresh_max_ms,
            geometry_valid=geometry_valid,
            target_presence=bool(targets or fars),
            stage_transition=stage_transition)
        distances = tuple(self._distance_of(t, per.ts_ns, frame_age_ms)
                          for t in self._tracks if t.kind == KIND_CAR)
        # 逐车米制状态（C 线）：与 distances 同序同 id，z/x/v/t 全从锚点链路
        # 直读（公式见 MetricCarState）；无锚=source UNKNOWN，z/x/v/t 全 None，
        # 消费端回退像素仪器。far 轨同样产出（米制态不依赖 x_lane 有效性）。
        by_id = {t.id: t for t in self._tracks if t.kind == KIND_CAR}
        metric_cars = tuple(self._metric_of(d, by_id[d.target_id])
                            for d in distances)
        self._last_fid_seen, self._last_ts_seen = fid, per.ts_ns
        fid_hz = (self.fid_rate_hz
                  if self._fid_hz_n >= self._FID_HZ_WARM_N else None)
        self._last_obs = WorldObservation(
            schema_version=SCHEMA_VERSION, frame_id=fid, ts_ns=per.ts_ns,
            frame_age_ms=frame_age_ms, stage=stage, health=health,
            boundary=None, targets=tuple(targets), far_targets=tuple(fars),
            distances=distances, metric_cars=metric_cars, fid_rate_hz=fid_hz)
        return self._last_obs

    # ---------- 内部 ----------

    # 采集帧率估计：**累计聚合式**（总帧数/总时长，自估计起点起算）——
    # 拍间 inst=Δfid/Δt 双峰（帧供数节律 33/50/66ms 混合）会把 EMA 钉在
    # 偏低值（2026-10-12 实测 EMA 中位 33 vs 聚合 38，t_meet 尺度差 15%），
    # 聚合比率对节律波动与卡顿间隙都稳健。预热未达样本数不外供（None=
    # 消费方自决退路，不半熟冒充）。
    _FID_HZ_WARM_N = 8

    def _update_fid_rate(self, fid: int, ts_ns: int) -> None:
        """采集帧率在线估计（聚合式）：fid 率是采集流属性，率计起点不随
        track/stage 生命期走——reset 不清；重复 fid 已在 update 头部拦截。"""
        if self._rate_fid is None:
            self._rate_fid, self._rate_ts_ns = fid, ts_ns
        self._fid_hz_n += 1

    @property
    def fid_rate_hz(self) -> float | None:
        """当前采集帧率估计（预热未达标 = None）。"""
        if self._fid_hz_n < self._FID_HZ_WARM_N or self._rate_ts_ns is None:
            return None
        dt = self._last_ts_seen - self._rate_ts_ns
        if dt <= 0:
            return None
        return (self._last_fid_seen - self._rate_fid) / (dt / 1e9)

    def _observe(self, per: PerceptionResult) -> list[_Obs]:
        out: list[_Obs] = []
        for kind, attr in _KIND_ATTRS:
            for ki, d in enumerate(getattr(per, attr)):
                denom = d.cy - self.cal.y_h
                if denom <= 0:
                    continue  # 地平线以上：几何无效，丢弃（归一发散口径，world_model.x_lane_of 同式）
                x = x_lane_of(d.cx, d.cy, self.cal) if denom >= self.cal.min_denom else None
                dx = d.cx - self.cal.vpx
                side = (LaneSide.MID if abs(dx) < self.p.neutral_px
                        else LaneSide.RIGHT if dx > 0 else LaneSide.LEFT)
                out.append(_Obs(
                    kind=kind, cy=d.cy, cx=float(d.cx), w=d.w, h=d.h, conf=d.conf,
                    x_lane=x, x_sigma=self.p.pos_err_px * self.cal.a_x / denom,
                    lane_side=int(side), det_key=(kind, ki)))
        return out

    # ---------- 混合测距（刀一 trace-only；语义与参数出处见设计稿） ----------

    def _ingest_anchors(self, sheet: DepthAnchorSheet) -> None:
        """detection-keyed 锚点清单 → 轨道锚点。归轨道走本层的帧映射表
        （深度域不认识 track_id）；本帧 fid 不在映射窗内的清单条目只能丢弃——
        那意味着 worker 滞后超过窗长，锚点年龄本来也要超闸。"""
        fmap = self._frame_tracks.get(sheet.fid)
        by_id = {t.id: t for t in self._tracks}
        mapped = 0
        for e in sheet.entries:
            tid = None if fmap is None else fmap.get((KIND_CAR, e.det_idx))
            t = by_id.get(tid) if tid is not None else None
            if t is None:
                continue
            mapped += 1
            if t.anchor_z is not None and abs(e.z_m - t.anchor_z) > self.p.cluster_switch_m:
                # 层一测量有效性：主簇跳变（实验实录帧间数十米假跳）——量错对象
                # 的锚点救不了，弃权等重拍；阈值是 trace 标定候选
                t.anchor_z = None
                t.anchor_fx = None              # 焦距锚与 z 锚同生命周期
                t.v_close_mps = None            # 速度与锚点同生命周期
                t.v_inst = None
                t.last_dist_reason = DistanceReason.CLUSTER_SWITCH
                continue
            if t.anchor_z is not None:
                dt_s = (sheet.ts_ns - t.anchor_ts_ns) / 1e9
                if abs(e.z_m - t.anchor_z) > (self.p.z_jump_base_m
                                              + self.p.z_jump_mps * dt_s):
                    # 亚门限翻跳（近簇修正的残余）：丢弃该锚保旧锚——量错不是
                    # 真动；门限随 dt 缩放（MoGe 卡顿拉长 dt 时真闭速锚不得
                    # 误杀），超龄（EXPIRED 清锚）后自然重基线
                    t.last_dist_reason = DistanceReason.CLUSTER_SWITCH
                    continue
                if dt_s > 0 and dt_s * 1000.0 >= self.p.gate_eval_min_ms:
                    # 锚间区间差分 v̂（2026-10-13 换装，替代 log-h 框高式——
                    # 实机生命表证伪：框高在"框头/框全车"间翻，v̂ 荒值 40~70m/s）。
                    # 到锚即发布：速度证据在区间完成瞬间就成立，再等锚龄是旧
                    # 逐帧锚时代的惯性（白丢 150ms，快会车整场等不到 v）
                    t.v_inst = (t.anchor_z - e.z_m) / dt_s
                    t.v_close_mps = t.v_inst
            t.anchor_z = e.z_m
            t.anchor_fx = sheet.fx              # 同帧焦距（x_m 反投影换算锚）
            t.anchor_ts_ns = sheet.ts_ns
            # v_close 不随新锚清零（区间估计跨锚有效：上一个区间是当前最佳
            # 速度证据，新锚到龄后由新 v_inst 顶替）——清零曾造成每锚 150ms
            # 的 t_meet 断供，far 轨威胁信号以 MoGe 节拍闪现（2026-10-12 局）
            t.approach_confirmed_at = None   # 新锚点 = 新生命周期（设计稿 §2 规则 2）
            t.last_dist_reason = None
        # 清单对账面（20261016 归因：零锚轨损失在检测→清单→轨道之间——条目数
        # vs 映射成功数 vs fid 滞后，trace asheet 列消费；orphan 由 update 补）
        self._ingest_stats = {"entries": len(sheet.entries), "mapped": mapped,
                              "fid_lag": None if self._last_fid is None
                              else self._last_fid - sheet.fid}

    def _distance_of(self, t: _Track, ts_ns: int, obs_age_ms: float) -> ObjectDistance:
        """三态合成（设计稿 §2 状态机）。RESCALED 是锚点生命周期状态：确认过
        接近的锚点，原始值永不重新暴露；退出唯一路径是新锚点重置基线。"""
        if t.anchor_z is None:
            return ObjectDistance(
                target_id=t.id, source=DistanceSource.UNKNOWN,
                metric_distance_m=None, method=None, anchor_age_ms=None,
                obs_age_ms=obs_age_ms, v_close_mps=None,
                reason=t.last_dist_reason or (
                    DistanceReason.ASSOC_FAIL if self._sheet_orphan
                    else DistanceReason.NO_ANCHOR_YET))
        if not t.matched:
            # 本拍丢框：h 不再更新，持锚/重标都失去当前证据——弃权但锚点保留
            #（复匹配后未超龄即恢复；超龄由下一分支收口）
            return ObjectDistance(
                target_id=t.id, source=DistanceSource.UNKNOWN,
                metric_distance_m=None, method=None,
                anchor_age_ms=(ts_ns - t.anchor_ts_ns) / 1e6,
                obs_age_ms=obs_age_ms, v_close_mps=t.v_close_mps,
                reason=DistanceReason.TRACK_LOST)
        age_ms = (ts_ns - t.anchor_ts_ns) / 1e6
        if age_ms > self.p.anchor_max_age_ms:
            t.anchor_z = None
            t.v_close_mps = None                # 速度与锚点同生命周期
            t.last_dist_reason = DistanceReason.EXPIRED
            return ObjectDistance(
                target_id=t.id, source=DistanceSource.UNKNOWN,
                metric_distance_m=None, method=None, anchor_age_ms=age_ms,
                obs_age_ms=obs_age_ms, v_close_mps=None,
                reason=DistanceReason.EXPIRED)
        if t.approach_confirmed_at is not None:
            # 当前米制距离 = 锚点值按区间速度外推（2026-10-13 换装，替代框高
            # 重标 Z_a·h_a/h——框高抖动直接打进距离，生命表实测 z 逐锚跳
            # 10~15m；v_close 为 None 时保守持锚不外推）
            age_s = age_ms / 1000.0
            metric = t.anchor_z - (t.v_close_mps or 0.0) * age_s
            return ObjectDistance(
                target_id=t.id, source=DistanceSource.RESCALED_ANCHOR,
                metric_distance_m=float(metric),
                method=DistanceMethod.RANGE_PROP, anchor_age_ms=age_ms,
                obs_age_ms=obs_age_ms, v_close_mps=t.v_close_mps, reason=None)
        return ObjectDistance(
            target_id=t.id, source=DistanceSource.ANCHOR,
            metric_distance_m=float(t.anchor_z), method=DistanceMethod.HOLD,
            anchor_age_ms=age_ms, obs_age_ms=obs_age_ms,
            v_close_mps=t.v_close_mps, reason=None)

    def _metric_of(self, d: ObjectDistance, t: _Track) -> MetricCarState:
        """ObjectDistance → 米制状态（C 线；z 复用 _distance_of 的当前米制值——
        RESCALED=锚+速度外推、ANCHOR=持锚，无第二套公式）。x_m=(cx−vpx)·z/fx：
        cx 当前框、fx 锚点帧焦距、z 当前米制——三者各差半拍（锚帧 vs 当前帧），
        横向慢变量误差二阶（MetricCarState docstring）。t_meet 只在 v_close>0
        时出：≤0=掠过/远离（占用语义），None（有锚）=估计窗未开（回退像素），
        None（UNKNOWN）=无锚（回退像素）——消费端靠 source 分流。

        速度像素桥（闸 metric_bridge.enabled，默认关=逐位回今天）：米制
        t_meet 缺席（UNKNOWN 五因/估计窗未开/丢框保锚——v_close 已发布但
        t=None 是占用语义，不桥）的拍以 rel_approach 纵向行运动桥接 t_meet
        （t_meet_car_s 同一公式真源，免二次标定）。进入/退出迟滞：意图换侧
        须当前侧驻留 ≥ stay 拍（防双源翻脸），从未锚定的车初驻留即满（首个
        缺席拍即进桥）；进入原因记 UNKNOWN 五因值或 est_window（有锚估计窗
        未开），换回米制清零。桥接段 fid 率未热/不接近（rel≤0）=无桥接值，
        下拍再试。"""
        z = d.metric_distance_m
        x_m = None
        if z is not None and t.anchor_fx and t.anchor_fx > 0:
            x_m = (t.cx - self.cal.vpx) * z / t.anchor_fx
        v = d.v_close_mps
        t_meet = z / v if (z is not None and v is not None and v > 0.0) else None
        bridged = False
        bridge_reason = None
        if self._bridge_on:
            meter = t_meet is not None
            if meter != (t.src_mode == "metric"):
                # 意图换侧：当前侧驻留满 stay 拍才换（防双源翻脸）
                if t.src_dwell >= self._bridge_stay:
                    t.src_mode = "metric" if meter else "bridge"
                    t.src_dwell = 1
                    t.bridge_reason = None if meter else (
                        d.reason.value if d.reason is not None
                        else "est_window")
                else:
                    t.src_dwell += 1
            else:
                t.src_dwell += 1
            if t.src_mode == "bridge":
                bridge_reason = t.bridge_reason
                rel, fid = t.rel_approach, self.fid_rate_hz
                if rel > 0.0 and fid:
                    t_meet = t_meet_car_s(rel, t.cy, self.cal, fid)
                    bridged = True
        return MetricCarState(
            target_id=t.id, source=d.source, z_m=z, x_m=x_m,
            v_close_mps=v, t_meet_s=t_meet, anchor_age_ms=d.anchor_age_ms,
            bridged=bridged, bridge_reason=bridge_reason)

    def _associate(self, obs: list[_Obs], fid: int) -> dict[int, int]:
        """逐对算代价值 → 全局按代价升序贪心 1:1 配对。
        代价 = 纵向失配归一 + 横向失配归一（双方都有横向读数时）。"""
        pairs: list[tuple[float, int, int]] = []
        for ti, t in enumerate(self._tracks):
            if t.matched or fid - t.last_seen > self.p.grace_ticks:
                continue
            for oi, o in enumerate(obs):
                if o.kind != t.kind:
                    continue
                cost = self._gate_cost(t, o, fid)
                if cost is not None:
                    pairs.append((cost, ti, oi))
        pairs.sort()
        used_t: set[int] = set()
        used_o: set[int] = set()
        matches: dict[int, int] = {}
        for _c, ti, oi in pairs:
            if ti in used_t or oi in used_o:
                continue
            used_t.add(ti)
            used_o.add(oi)
            matches[ti] = oi
        return matches

    def _gate_cost(self, t: _Track, o: _Obs, fid: int) -> float | None:
        age = max(fid - t.last_seen, 1)
        cy_pred = t.cy + t.rel_approach * age
        d_cy = o.cy - cy_pred
        if abs(d_cy) > self.p.max_cy_step:
            return None  # 跳变超物理上限：不按同一目标处理（校验层另有倒挂信号）
        cost = abs(d_cy) / self.p.max_cy_step
        if t.x_lane is not None and o.x_lane is not None:
            if abs(o.x_lane - t.x_lane) > self.p.max_xlane_step:
                return None
            cost += abs(o.x_lane - t.x_lane) / self.p.max_xlane_step
        elif t.lane_side != o.lane_side:
            # 任一侧无横向读数（far↔near 晋升/降级、遮挡重入）：只校粗方向。
            # 一侧为 MID 另一侧非 MID 同样拒——遮挡期间跨到别的方向带就不是它了。
            return None
        return cost

    def _clamp_cy(self, cy: int) -> int:
        return max(cy, int(self.cal.y_h) + 1)

    @property
    def ingest_stats(self) -> dict | None:
        """最近一次锚点清单对账面（trace asheet 列数据源；20261016 归因：零锚
        轨损失在检测→清单→轨道之间）。entries=清单条目数、mapped=成功映射到
        轨道数（差值=映射失败或滞后整帧丢弃）、fid_lag=清单帧落后当前帧数
        （>0=worker 滞后）、orphan=整张无法归轨。None=本拍无新清单（深度
        证据拍才有）。"""
        return self._ingest_stats


def to_jsonable(x: Any) -> Any:
    """契约对象 → json 可序列化结构（回放落盘与单测共用；Enum 取 value）。"""
    if is_dataclass(x) and not isinstance(x, type):
        return {f.name: to_jsonable(getattr(x, f.name)) for f in fields(x)}
    if isinstance(x, Enum):
        return x.value
    if isinstance(x, (list, tuple)):
        return [to_jsonable(i) for i in x]
    return x
