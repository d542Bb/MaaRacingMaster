# -*- coding: utf-8 -*-
"""运动规划与控制层：决策输出 → 手柄指令（planner 设计稿 v1，§九 step 1）。

**分层归属**（红线 1）：本模块消费 ``tracking.DecisionOutput``（决策层唯一出口），
产出 ``GamepadCommand`` 供接线层下发到 ``core.capabilities.Gamepad``（方向
plugin→core）。不读像素、不读检测框、不读 boundary——弯道/退化判据真源在
decision.ValidationWatch（不变量 C1：本层不重复造判据，只消费 FSM 状态）。

**跟踪律命名**（维护者裁定 2026-09-22）：*延迟补偿前瞻 PD*——Pure Pursuit 取的是
"提前量"精神（追视点外推到 t+τ 再入环，Autoware 式），**不是**几何 κ 公式
（黑箱物理下没有曲率可代）。控制律形式不随运动学改——`k_d` 速度阻尼本就是为
双积分对象配的，v1 的误处在把 **plant（运动学）** 写成了单积分"杆→稳态速度"。

**自车横向运动学**（不变量 C2，设计稿 §二；v2 2026-09-22 真机证据链修正，
v3 2026-10-26 人工局手柄流重辨识）：
赛车机制下杆是**车头航向指令**不是平移速度指令——起步段 x∝t²（向心加速度），
故 ``v_lat`` 经 ``a_lat_gain`` 积分而来、被 ``v_lat_max`` 定圆饱和；**不存在**
"杆值→稳态横向速度"的静态 K_v（原 v_lat_gain 前提证伪）。**松杆后横速几乎
不衰减**（人工局真实摇杆 11 段拟合 tau≥12s 撞上界；旧 0.5s 是程序闭环+贴墙
段污染的拟合产物）——停住横移靠反打杆主动刹，刹停减速用
``brake_decel_lane_s2``（人工反打有效减速 p50 1.3，取保守 0.8）做停止点预算；
``tau_align_s`` 只留作数值稳定的长尾项。
A1 归一下自车恒 0、**目标读数**推不出自车位移，但**路缘两侧读数**可以：
路中心相对自车 = 自车横向位置的反号——``road_offset`` 入参（step 2.5，
V2 复盘"开环虚胖提前松杆"的闭环解）在双侧缘距稳定拍经 alpha-beta 滤波器
钉住 ``executed_lane``；**参考系=道0 路中心**（2026-10-02 维护者裁定中轴
巡航：x_target=0 即守路轴，牙子级边界信号不可依赖、守轴是唯一可守的安全
面），大初差经新息门+陈旧重基消化；无观测拍退回模型开环积分。
旋转污染 ε≈−δ·a_x（5.7° 航向仅 0.06 车道，二阶量）暂不修正，vp_x 去偏留后续。

**纯函数纪律**（C3）：update 只吃 (decision, dt_s, current_fid) + 内部状态，
喂同一输入流回放复现同一杆值流。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

from maaracing_master.plugins.speedrush.config import Planner
from maaracing_master.plugins.speedrush.lane_obs import LANE_W
from maaracing_master.plugins.speedrush.tracking import (
    DecisionOutput, DecisionState)

if TYPE_CHECKING:
    from maaracing_master.plugins.speedrush.motion_plan import MotionPlan

_STICK_FULL = 32767  # XUSB 摇杆半幅（core.capabilities.Gamepad 契约）
# 持续大新息的"同向同量级"容差（道）：相邻两拍观测差在此内才算同一偏移，
# 防真位移过程中的大新息被误判成持续偏移。
_JUMP_SAME_TOL_LANE = 0.5
# 车道线连续横位（S1 线观测）作保持期的运动观测：深度路心在 memory/
# wall_occl 期冻结，模型自走会把"打杆量"当位移——贴墙时车实际不动、模型
# 仍按 v 漂走（20261025 局 p2 496~537：线报 −1.9 不动，模型从 −1.0 漂到
# −0.24，2.3s 后新鲜值回 −1.79）。线在同一深度锚点内逐帧跟随车辆横移，
# 是保持期唯一连续的运动信号；四局离线：新鲜拍新息 p90 0.78→0.62、
# p99 2.4→1.6，保持期与线的偏离均值 <0.1 道。
_LANE_SMOOTH_ALPHA = 0.2   # 线横位 EMA：压单帧噪声（±0.3~0.5 道）
_LANE_FUSE_GAIN = 0.3      # 保持期向"线+帧偏置"收敛的每拍增益（~0.3s 时间常数）
_LANE_BIAS_AGREE = 0.6     # 深度/线一致门：差异在此内才用深度帧校准线帧
_LANE_BIAS_MAX = 0.25      # 帧偏置物理上限：墙边实测 p50 0.0~0.12，>0.3 多为
                           # 单侧/绿区坏深度把线帧往错误方向拽（会抵消贴墙信号）
_LANE_BIAS_EMA = 0.25      # 偏置慢滤波：单帧 0.5 道差异不能一步坐实
_LANE_VETO_LANE = 0.5      # 已对账锁线与新鲜深度差超此值：深度本拍不拉位置
_LANE_JUMP_RESET = 0.8     # 线自身单拍跳超此值 = 换线/重锁：重置并停用融合
# 线横位的物理上限（道）：路面总宽 12.8~16.0 m，半宽 1.9~2.4 道；超过此值
# 不可能是本车所在车道线，是远端边线/邻道错锁。贴墙段实测这类读数（3.1~3.5）
# 连续 0.8s 把平滑值拖到 3.2，随后又当成"锁线"与深度打架（20261026 局 s1#3）。
_LANE_X_MAX = 2.6
# 短断链内（COAST）的线读数仍作"反证"：平滑值还是最近实测，深度若与它
# 差超过一致门就不重基。超过此龄视为无对照，深度单独说了算。
_LANE_BLOCK_AGE_MS = 300.0
# 贴边逃生门（道）：外车道中心 ±1 再让 0.4；超过即按全权回带（gentle 的
# 0.45 杆幅在 1.4 道级误差下需要 1s+ 才能回带，期间车停在墙侧）。
_WALL_ESCAPE_LANE = 1.4

# 航向+线速观测（2026-10-26 离线 12 局）：模型横速与线速相关 0.4~0.6、幅值
# 中位只有线速的 1/4 左右，且相位超前约 0.45s；航向横速滞后 0.2~0.3s。两者
# 各半融合后 0.25s 横位预测误差中位降 10~20%、相位回到 0 附近。前向速度由
# v_line*LANE_W/tan(theta_rel) 的 5s 中位在线自标定；theta_rel 的慢基线吸收
# 弯道里线拟合的常值弦角，且速度与航向同源同尺度，整体增益不敏感。
_HEAD_SLOPE_WINDOW_S = 0.6
_HEAD_SPEED_WINDOW_S = 5.0
_HEAD_SPEED_MIN_N = 8
_HEAD_MIN_V_LANE_S = 0.15
_HEAD_MIN_TAN = 0.01
_HEAD_THETA_TAU_S = 2.0
_HEAD_REL_MAX = 0.6
_HEAD_AGE_MAX_MS = 250.0
_HEAD_STALE_S = 0.5
_HEAD_V_MAX_FACTOR = 2.0   # 观测横速上限 = v_lat_max 的倍数（阻尼允许越过模型饱和）
_HEAD_SPEED_DEFAULT_MPS = 45.0   # 与 [coarse].range_speed_mps 同量级，仅起步兜底


class HeadingObserver:
    """航向+线速的横向速度观测（只喂控制律，不改模型状态 v_lat_est）。

    线横位斜率给出横向速度；线角相对 2s 慢基线给出航向。前向速度用两者
    的比值中位在线估计（尺度自洽，不需要新的米制真源），换算成航向横速后
    与模型横速融合。线未锁定、证据龄超限或断供 >0.5s 时一律退回模型。
    """

    def __init__(self, p: Planner) -> None:
        self.p = p
        self.reset()

    def reset(self) -> None:
        self._t = 0.0
        self._samples: list[dict] = []
        self._base: float | None = None
        self._last_fid: int | None = None
        self._last_seen = -1e9
        self.v_line: float | None = None
        self.speed_mps: float | None = None
        self.v_head: float | None = None

    def update(self, dt: float, lane_x: float | None, lane_ok: bool,
               theta: float | None, theta_ok: bool, fid: int | None,
               age_ms: float | None) -> None:
        if not (isinstance(dt, (int, float)) and math.isfinite(dt) and dt > 0):
            return
        self._t += dt
        if self.v_head is not None and self._t - self._last_seen > _HEAD_STALE_S:
            self.v_head = None
        if (not lane_ok or lane_x is None
                or not math.isfinite(float(lane_x))):
            return
        if (theta is None or not theta_ok or not math.isfinite(float(theta))
                or age_ms is None or float(age_ms) > _HEAD_AGE_MAX_MS):
            return
        if fid is not None and fid == self._last_fid:
            return                       # 同一检测帧被 20Hz 控制拍重复读取
        self._last_fid = fid
        th = float(theta)
        if self._base is None:
            self._base = th
        else:
            gap = (self._t - self._samples[-1]["t"]
                   if self._samples else dt)
            a = 1.0 - math.exp(-min(max(gap, 1e-3), 0.5) / _HEAD_THETA_TAU_S)
            self._base += a * (th - self._base)
        rel = max(-_HEAD_REL_MAX, min(_HEAD_REL_MAX, th - self._base))
        self._last_seen = self._t
        s = {"t": self._t, "x": float(lane_x), "rel": rel, "v": None}
        self._samples.append(s)
        cut = self._t - _HEAD_SPEED_WINDOW_S - _HEAD_SLOPE_WINDOW_S
        while self._samples and self._samples[0]["t"] < cut:
            self._samples.pop(0)
        # 因果线斜率：只用本拍及过去 0.6s（离线同一口径）
        win = [q for q in self._samples
               if self._t - q["t"] <= _HEAD_SLOPE_WINDOW_S]
        if len(win) >= 4 and win[-1]["t"] - win[0]["t"] >= 0.3:
            mt = sum(q["t"] for q in win) / len(win)
            mx = sum(q["x"] for q in win) / len(win)
            vt = sum((q["t"] - mt) ** 2 for q in win)
            if vt > 1e-9:
                s["v"] = sum((q["t"] - mt) * (q["x"] - mx)
                             for q in win) / vt
                self.v_line = s["v"]
        vals = []
        for q in self._samples:
            if q["v"] is None or self._t - q["t"] > _HEAD_SPEED_WINDOW_S:
                continue
            tan = math.tan(q["rel"])
            if abs(q["v"]) < _HEAD_MIN_V_LANE_S or abs(tan) < _HEAD_MIN_TAN:
                continue
            if (q["v"] > 0) != (tan > 0):
                continue
            vals.append(q["v"] * LANE_W / tan)
        if len(vals) >= _HEAD_SPEED_MIN_N:
            self.speed_mps = statistics.median(vals)
        elif self.speed_mps is None:
            self.speed_mps = _HEAD_SPEED_DEFAULT_MPS
        lim = _HEAD_V_MAX_FACTOR * self.p.v_lat_max
        v = self.speed_mps * math.tan(rel) / LANE_W
        self.v_head = max(-lim, min(lim, v))

    def fused(self, v_model: float, gain: float) -> float:
        if gain <= 0.0 or self.v_head is None:
            return v_model
        v = (1.0 - gain) * v_model + gain * self.v_head
        lim = _HEAD_V_MAX_FACTOR * self.p.v_lat_max
        return max(-lim, min(lim, v))

    def debug(self) -> dict:
        return {
            "v_line": None if self.v_line is None
            else round(self.v_line, 3),
            "speed": None if self.speed_mps is None
            else round(self.speed_mps, 1),
            "v_head": None if self.v_head is None
            else round(self.v_head, 3),
        }


def law(e: float, v_lat: float, p: Planner, gentle: bool = False,
        deadband: float | None = None, cap: float | None = None,
        v_ref: float = 0.0) -> float:
    """跟踪律单一真源（规划器与离线轨迹仿公共用）。

    ``v_ref`` 是前馈速度参考：默认 0 = 旧行为（只阻尼实测横速）。"""
    db = (p.hold_deadband_lane if gentle else 0.0) \
        if deadband is None else deadband
    if abs(e) < db:
        e = 0.0
    u = p.k_p * e - p.k_d * (v_lat - v_ref)
    lim = p.hold_stick_max if gentle else None
    if cap is not None:
        lim = cap
    if lim is not None:
        u = max(-lim, min(lim, u))
    return max(-1.0, min(1.0, u))


def step(x: float, v: float, steer: float, u: float, dt: float,
         p: Planner) -> tuple[float, float, float]:
    """横向运动学单一真源：杆值低通 + 双积分 + 定圆饱和。

    返回 ``(x, v, steer)``；顺序与生产控制拍逐位一致（⑤ 低通 → ⑥ 积分）。"""
    alpha = 1.0 - math.exp(-dt / p.tau_steer_s)
    steer += alpha * (u - steer)
    v += (p.a_lat_gain * steer - v / p.tau_align_s) * dt
    v = max(-p.v_lat_max, min(p.v_lat_max, v))
    x += v * dt
    return x, v, steer


@dataclass(frozen=True)
class GamepadCommand:
    """本层唯一出口：左摇杆 x（转向）+ 右扳机（恒油门，RULES §4.1 权威）。"""

    steer_x: int
    throttle: int


@dataclass
class EgoLateralState:
    """自车横向状态（本层持有，回执决策层；设计稿 §一）。"""

    executed_lane: float = 0.0   # 车道单位，A1 系（重锚拍被观测值覆写）
    v_lat_est: float = 0.0       # 车道单位/s
    steer_norm: float = 0.0      # 低通后的当前杆值指令（归一域）


class LateralPlanner:
    """延迟补偿前瞻 PD + 开环积分 + 观测重锚。单线程 owner 使用。"""

    def __init__(self, p: Planner):
        self.p = p
        self.state = EgoLateralState()
        self._last_raw = 0            # 上一拍下发杆值（限幅起点）
        self._expired_ticks = 0
        self._abort_hold_lane: float | None = None
        self._conserve_hold: float | None = None  # CONSERVE 入拍冻结的车道位（③）
        self._road_anchor: float | None = None   # 路观测参考帧懒定（⑥b）
        self._stale_t = 0.0                      # 距上次被接受修正的累计秒（⑥b 陈旧重基）
        # 自上次速度修正（或滤波重置）以来模型积分跑过的帧钟累计——⑥b 速度
        # 修正的时间基（2026-10-08 判据冻结，台账 §4A）
        self._t_vcorr = 0.0
        # 上次被接受的修正方向（阶段 2 符号一致性门；None=尚无）
        self._last_corr_sign: bool | None = None
        # 持续大新息判定（2026-10-23）：同向且量级相近的连续拒绝拍数——
        # 连续 N 拍=真位移，放行 α 位置修正（单帧坏检测仍被拒）。
        self._jump_streak = 0
        self._jump_sign: bool | None = None
        self._jump_last_r: float | None = None
        self._jump_confirmed = False
        # 线横位融合（保持期运动观测）：平滑值/深度-线帧偏置/是否可信
        self._lane_sm: float | None = None
        self._lane_bias = 0.0
        self._lane_trusted = False
        self._lane_relock_seen = False   # 已见 RELOCK，等下一条锁线重置
        self._lane_age_ms: float | None = None
        self._lane_gated = False         # 本拍线读数越出物理范围（按无线处理）
        # 上一拍低通前杆值（负一~一；trace 标定用，行为不消费）
        self.last_steer_raw = 0.0
        # 上一拍计划前馈命令：换候选时限速用；计划中断后置空，重入首拍不限。
        self._last_plan_u: float | None = None
        # 航向+线速观测（只喂控制律；模型状态 v_lat_est 仍是积分真源）
        self.head = HeadingObserver(p)
        self._v_ctrl = 0.0
        self._v_ctrl_model = 0.0

    def _reset_jump(self) -> None:
        """重置持续大新息判定（正常接受/重基/断供时调用——连续性被打断）。"""
        self._jump_streak = 0
        self._jump_sign = None
        self._jump_last_r = None
        self._jump_confirmed = False

    def _lane_rebias(self, obs: float, lane_ok: bool,
                     allow_bias: bool = True) -> None:
        """新鲜深度拍校准线帧偏置：两者一致才把偏置钉到深度帧。

        不一致时保留旧偏置（单侧墙 + W/2 反推、错箱都会给出 0.5~1 道偏差，
        见 20261025 局 p2 490：墙读 −1.14，线与截图都在 −1.9），等下一次
        一致再校正；重基拍由调用方先置 trusted=False，一致性恢复前不融合。
        线未锁定（COAST/RELOCK）时线值不可比，不校准。
        偏置限幅 ±0.25 并做慢滤波：墙边偏置会涨到 0.5~0.6，把线帧往错误的
        单侧/绿区深度拽，正好抵消贴墙信号；单帧差异也不能一步坐实。"""
        if not lane_ok or self._lane_sm is None or not allow_bias:
            return
        d = obs - self._lane_sm
        if abs(d) <= _LANE_BIAS_AGREE:
            d = max(-_LANE_BIAS_MAX, min(_LANE_BIAS_MAX, d))
            self._lane_bias = max(
                -_LANE_BIAS_MAX,
                min(_LANE_BIAS_MAX,
                    self._lane_bias + _LANE_BIAS_EMA * (d - self._lane_bias)))
            self._lane_trusted = True

    def _line_disagrees(self, obs: float, lane_ok: bool) -> bool:
        """单侧墙反推的深度会错 1 道以上；线仍可用且差超一致门时，
        不拿这份深度做陈旧重基（等两源收敛，旧行为）。

        可用 = 本拍锁定，或短断链内（COAST ≤`_LANE_BLOCK_AGE_MS`，平滑值
        仍是最近实测）。线越界被挡或已断供超龄时，深度是唯一来源，不挡。
        """
        if self._lane_gated or self._lane_sm is None:
            return False
        recent = (lane_ok or (self._lane_age_ms is not None
                              and self._lane_age_ms <= _LANE_BLOCK_AGE_MS))
        if not recent:
            return False
        return abs(obs - (self._lane_sm + self._lane_bias)) > _LANE_BIAS_AGREE

    def _stale_rebase(self, road_offset: float, lane_ok: bool,
                      allow_bias: bool = True) -> None:
        """陈旧重基：exec:=ro、锚回道0、速度归零，等新证据重新收敛。"""
        self.state.executed_lane = road_offset
        self.state.v_lat_est = 0.0
        self._road_anchor = 0.0
        self._stale_t = 0.0
        self._t_vcorr = 0.0
        self._last_corr_sign = None
        self._reset_jump()
        self._lane_trusted = False
        self._lane_rebias(road_offset, lane_ok, allow_bias=allow_bias)

    def lane_debug(self) -> dict:
        """保持期线融合的可见面（trace）：trusted=偏置是否已对账、bias=深度-
        线帧偏置、sm=线横位平滑值。三者合起来才能离线判断融合是否在工作。"""
        return {"trusted": self._lane_trusted,
                "bias": round(self._lane_bias, 3),
                "sm": None if self._lane_sm is None
                else round(self._lane_sm, 3)}

    def ctrl_debug(self) -> dict:
        """控制横速的可见面（trace）：v_ctrl=控制律实际使用的横速、
        v_model=模型状态、v_head=航向横速、speed=在线前向速度估计。"""
        d = {"gain": self.p.heading_fuse_gain,
             "v_ctrl": round(self._v_ctrl, 3),
             "v_model": round(self._v_ctrl_model, 3)}
        d.update(self.head.debug())
        return d

    # ---------- 主入口 ----------

    def _brake_overshoot(self, target: float) -> bool:
        """刹停预算：按模型横速预测的停止点越过目标带远侧缘。

        速度源用 ``v_lat_est``（模型积分+深度 β）：航向横速在贴墙段噪声大
        （线角抖动），预算要的是最可信的速度。松杆不衰减（v3 重辨识），
        gentle 杆幅 0.45 刹不住 1 道/s 级横速（0.8 道/s² 下还要滑 0.6+ 道）
        ——必须立即满杆反打。横速归零后停止点收敛到当前位置，条件自动
        退出，不锁死反打。"""
        v = self.state.v_lat_est
        if abs(v) < 1e-3 or self.p.brake_decel_lane_s2 <= 0.0:
            return False
        sgn = math.copysign(1.0, v)
        stop = (self.state.executed_lane
                + sgn * v * v / (2.0 * self.p.brake_decel_lane_s2))
        edge = target + math.copysign(self.p.hold_deadband_lane, v)
        return (stop - edge) * sgn > 0.0

    def update(self, decision: DecisionOutput, dt_s: float,
               current_fid: int,
               road_offset: float | None = None,
               road_offset_new: bool = True,
               road_offset_hold: bool = False,
               lane_x: float | None = None,
               lane_ok: bool = False,
               lane_relock: bool = False,
               plan: "MotionPlan | None" = None,
               lane_theta: float | None = None,
               lane_theta_ok: bool = False,
               lane_fid: int | None = None,
               lane_age_ms: float | None = None) -> GamepadCommand:
        """一个控制 tick。dt_s 非法即 fail-loud（帧号倒退同一纪律：
        静默吞掉病态输入比中断危险——时间基低通会被 0/NaN 污染成永久状态）。

        ``road_offset``（step 2.2.5 闭环）：自车相对路心的位置（车道单位，右正，
        −(左右缘 x_lane 均值)，双侧稳定拍才有值）。有值即对本拍积分结果做
        alpha-beta 修正——治 V2 复盘实锤的"开环虚胖提前松杆"。None=无观测，
        纯模型积分（旧行为逐拍不变）。

        ``road_offset_new``：本拍 road_offset 是否**新证据**（异步驻留协议下，
        保鲜槽复用拍喂的是同一套旧读数）。速度修正（β·r/dt）只在新证据拍生效，
        复用拍只收位置（α·r）——同一新息反复注入曾把单次找边翻跳放大成饱和
        侧滑（2026-10-06 局 id17 取证）。默认 True=旧调用方行为不变。

        ``road_offset_hold``：本拍值来自**历史保持**（memory/wall_occl/
        pair_slot），不是本拍测量。这类值在自车系里已经过期：车按 2~4 道/s
        横移时，0.8s 的保持值落后 1~2 道。把它当观测收 α 等于每拍把模型拽回
        旧位置（20261025 局实测：保持期车已横移 2 道，exec 只走了 0.2 道）。
        故保持拍不进 ⑥b：模型自走、陈旧计时照常推进，等新鲜实测回来再锚。

        ``lane_x`` / ``lane_ok``：车道线观测的连续横位（与 road_offset 同帧）
        及是否锁定。深度保持期它是唯一连续的运动测量——车贴墙不动时模型会
        按打杆量虚走，线不会。融合只改保持/断供拍的位置（向"线+深度-线帧
        偏置"收敛，增益 `_LANE_FUSE_GAIN`）；新鲜深度拍会校准帧偏置，两者
        差超 `_LANE_BIAS_AGREE` 时保留旧偏置（不把单侧墙反推的错值带进线帧）。
        ``lane_relock``：本拍线观测处于重锁（换线可能换帧）；下一条 TRACK
        线重置平滑值并停用融合，等新鲜深度与线一致后再启用。"""
        if not (isinstance(dt_s, (int, float)) and math.isfinite(dt_s)) or dt_s <= 0:
            raise ValueError(f"dt_s 非法（需有限正数）：{dt_s!r}")
        if not math.isfinite(decision.x_target if decision.x_target is not None else 0.0) \
                or (road_offset is not None and not math.isfinite(road_offset)):
            raise ValueError(f"decision 携带非有限值：x_target={decision.x_target!r} "
                             f"road={road_offset!r}")
        # 速度修正时间基累计：本拍模型又跑过 dt_s（帧钟）。修正在 ⑥b（⑥ 之后）
        # 消费，形成/重基各自清零——见 ⑥b 注
        self._t_vcorr += dt_s

        # 线横位平滑（保持期运动观测）：只在锁线拍更新；COAST/无值保持上一值。
        # 重锁后的第一条锁线可能换了线/换了帧：重置平滑并停用融合；线自身
        # 单拍跳超阈值同理。等新鲜深度与线一致时由 _lane_rebias 恢复。
        if lane_relock:
            self._lane_relock_seen = True
            self.head.reset()
        # 线横位越出物理范围即按"本拍无线"处理：不参与平滑/融合/互证，
        # 也不污染航向观测。深度仍可经 ⑥b 自行重基。
        self._lane_age_ms = lane_age_ms
        self._lane_gated = bool(
            lane_ok and (lane_x is None or not math.isfinite(lane_x)
                         or abs(lane_x) > _LANE_X_MAX))
        if self._lane_gated:
            lane_ok = False
            lane_theta_ok = False
        if lane_ok and lane_x is not None and math.isfinite(lane_x):
            if self._lane_relock_seen:
                self._lane_sm = lane_x
                self._lane_trusted = False
                self._lane_relock_seen = False
            elif self._lane_sm is None:
                self._lane_sm = lane_x
            elif abs(lane_x - self._lane_sm) > _LANE_JUMP_RESET:
                self._lane_sm = lane_x
                self._lane_trusted = False
                self.head.reset()
            else:
                self._lane_sm += _LANE_SMOOTH_ALPHA * (lane_x - self._lane_sm)

        # 航向+线速观测：控制阻尼/前瞻用的横速 = 模型与航向融合；模型状态
        # v_lat_est 不动（计划生成与线滑行预测仍用模型）。gain=0 时逐位不变。
        # 注意 v_lat_est 的速度观测唯一通路是 ⑥b 的深度 β 修正——不要用
        # 线斜率速度直改状态：0.6s 因果斜率窗在 bang-bang 反打处结构性反号
        # （223955 局 s2 4.0~5.5s：线速 −1.8 顶真实 +3，把满杆建立的模型
        # 加速度吃掉七成，刹停预算失明直至撞栏）。
        self.head.update(dt_s, lane_x, lane_ok, lane_theta, lane_theta_ok,
                         lane_fid, lane_age_ms)
        self._v_ctrl_model = self.state.v_lat_est
        self._v_ctrl = self.head.fused(self.state.v_lat_est,
                                       self.p.heading_fuse_gain)

        # ① 过期判定（valid_until_fid 含边界；第 4 个连续过期拍起按 CONSERVE）
        fresh = current_fid <= decision.valid_until_fid
        self._expired_ticks = 0 if fresh else self._expired_ticks + 1
        stale_hold = (not fresh) and self._expired_ticks <= self.p.hold_max_ticks

        # ③ 控制律（状态消费表，设计稿 §四；优先级：CONSERVE/FAULT 接管 > 过期保持）
        st = decision.state
        if st is not DecisionState.CONSERVE:
            self._conserve_hold = None
        if st is DecisionState.CONSERVE:
            # CONSERVE=**回到并停在路中心**（13:02 局：归零直开弯道怼护栏，badframe
            # fid_2325 侧滑胎烟；13:29 局二次修正：入拍冻结 executed 的"保持车道"
            # 依赖车道参考系诚实，而参考系陈旧时 hold=−0.13 与 ro=+1.9 打架、杆恒 0
            # 直开 5.5s 又怼右墙）。ro 是路相对的**直接观测**，不经参考系——有 ro
            # 的拍就直连路中心（感知半死时"半死"的是金币检测，路几何活着；中心是
            # 路上最安全的位置）；无 ro 拍退回入拍冻结 executed 走 PD（直道无观测
            # 时平衡点仍是归零，旧行为兼容）。
            self._abort_hold_lane = None
            if road_offset is not None:
                ro_pred = road_offset + self._v_ctrl * self.p.lookahead_tau_s
                steer_raw = self._law(0.0 - ro_pred, gentle=True)
            else:
                if self._conserve_hold is None:
                    self._conserve_hold = self.state.executed_lane
                steer_raw = self._pd(self._conserve_hold, gentle=True)
        elif st is DecisionState.FAULT:
            steer_raw = 0.0                      # 几何不可信：直开是诚实兜底
            self._abort_hold_lane = None
        elif stale_hold:
            steer_raw = self.state.steer_norm    # 过期保持：杆值不动（不归零，C4）
        elif st is DecisionState.ABORT:
            # 冻结值归本层：首次观察到 ABORT 的拍写入当前有符号 executed_lane，
            # 之后跟踪它直到离开 ABORT——不消费决策层 x_target（其 ABORT 路径回落 0，
            # 与"回中不总是安全"不一致，维护者裁定 2026-09-22）
            if self._abort_hold_lane is None:
                self._abort_hold_lane = self.state.executed_lane
            steer_raw = self._pd(self._abort_hold_lane, gentle=True)
        elif decision.x_target is not None:
            self._abort_hold_lane = None
            # 刹停预算（最高优先）：横速的预测停止点已越过目标带远侧缘 →
            # 直接满杆反打。人工局的手法就是 bang-bang：满杆进、提前满杆反；
            # 走 PD 会被 k_p 的前瞻项稀释（假横速把预测位置送到目标跟前时
            # k_p 项归零、只剩 k_d 慢慢磨——215313 局 s2#4 骑线躺平 2.5s 的
            # 机制）。横速归零后停止点收敛到当前位置，条件自退出。
            if self._brake_overshoot(decision.x_target):
                steer_raw = -math.copysign(
                    self.p.maneuver_stick_max, self.state.v_lat_est)
            # 场跟随：保持态=维护性定中心（gentle，16:44 局）；机动态
            # （目标距自车超过切换死区）=全权——机动态死区/杆幅另配。
            # 贴边逃生：|executed|超 _WALL_ESCAPE_LANE（保持车道 ±1 + 0.4）时
            # 也走全权——墙是硬约束，gentle 的 0.45 杆幅把 1.4 道级回带拖成
            # 1s+（20261026 局：贴边拍 53/99 是计划走廊不可行被否决、PD 以
            # gentle 回带，整段停在墙侧）。回带方向仍由目标给出，不改目标。
            elif (decision.maneuver
                    or abs(self.state.executed_lane) > _WALL_ESCAPE_LANE):
                steer_raw = self._pd(
                    decision.x_target, gentle=False,
                    deadband=self.p.maneuver_deadband_lane,
                    cap=self.p.maneuver_stick_max)
            else:
                steer_raw = self._pd(decision.x_target, gentle=True)
        else:
            steer_raw = self.state.steer_norm    # 防御：非保守态却无目标，保持

        # 轨迹预览接管（motion.mode=live 且计划有效）：前馈 + 预览位/速度误差。
        # 计划帧→planner 道帧的换算已在 MotionPlan 内完成。
        if plan is not None and plan.valid and st is DecisionState.CRUISE:
            x_pred = (self.state.executed_lane
                      + self._v_ctrl * self.p.lookahead_tau_s)
            u = (plan.u_feedforward
                 + self.p.k_p * (plan.x_preview_lane - x_pred)
                 + self.p.k_d * (plan.v_preview_lane_s - self._v_ctrl))
            steer_raw = max(-1.0, min(1.0, u))
            # 刹停预算同样压住计划路径：计划用同一套（对称、乐观的）车模
            # 仿真，横速饱和的并线它以为收得住（215313 局 s1 12.3s：承诺期
            # v=−2.5 冲过目标道压路肩）。计划杆已在满反打方向且不低于预算
            # 杆幅时让计划继续——预算只补"没在刹"的缺口。触发即清 slew 链：
            # 人工的 bang-bang 不等限速。
            if self._brake_overshoot(decision.x_target):
                u_brake = -math.copysign(
                    self.p.maneuver_stick_max, self.state.v_lat_est)
                if not (plan.u_feedforward * u_brake > 0
                        and abs(plan.u_feedforward) >= abs(u_brake)):
                    steer_raw = u_brake
                    self._last_plan_u = None
                else:
                    self._last_plan_u = steer_raw
            else:
                # 换候选时前馈会阶跃；相对上一拍计划命令限速，避免方向盘一步打半圈。
                # 首帧/计划重入（None）不限制——那是交接，不是候选抖动。
                if self._last_plan_u is not None:
                    cap = self.p.plan_u_slew_s * dt_s
                    steer_raw = max(self._last_plan_u - cap,
                                    min(self._last_plan_u + cap, steer_raw))
                self._last_plan_u = steer_raw
        elif (plan is not None and st is DecisionState.CRUISE
              and plan.reason == "plan:infeasible_corridor"):
            # 计划已判定「自车压在走廊边距内」，但它无效、不接管，PD 的保持
            # 死区随即把 0.26 道的误差当"带内不修正"——车停在离栏 0.25m
            # （20261026 局 s2#4 21.6s：目标 0.93、车位 1.19、杆 −0.18）。
            # 无效计划选的是"最快脱离侵入"候选（空可行集第一项），直接执行
            # 它的杆值，不用保持死区/维护杆幅。
            steer_raw = plan.u_feedforward
            self._last_plan_u = None
        else:
            self._last_plan_u = None

        self.last_steer_raw = steer_raw
        # ⑤⑥ 杆值低通 + 横向运动学（单一真源：模块级 step）
        self.state.executed_lane, self.state.v_lat_est, self.state.steer_norm = \
            step(self.state.executed_lane, self.state.v_lat_est,
                 self.state.steer_norm, steer_raw, dt_s, self.p)

        # ⑥b 路中心连续重锚（step 2.5，V2 复盘主修）：双侧缘距拍给 executed 真反馈。
        #    标准 alpha-beta 滤波器——⑥ 的模型积分是 predict 步，本段是 correct 步：
        #    新息 r=观测−预测，位置收 α·r、速度收 β·r/dt。恒位置误差下 r 随 x/v
        #    同步收敛（不是纯 β 直注的发散），模型虚胖被钉回、误差常驻→杆常驻，
        #    "自说自话收敛→提前松杆"从此不可能。无观测拍与历史保持拍退回纯模型
        #    （保持拍的自车系读数已过期，见 road_offset_hold）。
        #    参考帧（2026-10-02 维护者裁定「中轴巡航」）：executed 与 x_target 同为
        #    "绝对车道位"系，**道0=路中心**——首个过形成门的观测拍令 anchor=0，
        #    executed 从此收敛到 road_offset（自车相对路心），x_target=0 即守中轴，
        #    两侧到边界台阶的余量同时最大（牙子级信号不可依赖，守轴是唯一可守的
        #    安全面——d00008 点云取证定案）。大初差由新息门+陈旧重基消化；事件重锚
        #    重置 anchor=None，下一观测拍按同语义重形成（executed 向路心收敛）。
        #    形成门（12:39 局教训）：|ro|>anchor_max_off 的骑缘拍不定锚，防垃圾
        #    观测定义参考系。
        #    陈旧重基（13:29 局二次教训）：漂移复位原按"连续 40 次喂入拒绝"计数——
        #    供数黑视期不计数、跳变门收紧后喂入更少，车以 0 杆直开 5.5s 怼进右墙时
        #    阈值还差 9 次。判据从"次数"换成"时间"：距上次被接受修正超 anchor_stale_s
        #    即重基（exec:=ro、anchor:=0），断供同样计时。
        if road_offset is not None and not road_offset_hold:
            if self._road_anchor is None:
                # 形成门外的贴边拍：深度与锁线独立一致（≤`_LANE_BIAS_AGREE`）
                # 即视为可信，直接建锚并对齐位置。否则断供丢锚后，车在 |ro|>1
                # 的贴边段永远过不了形成门，线融合也因无参考系停摆——实拍 p1
                # 202~246：锚在断供期被丢，深度连续报 +1.87 却等不到 1s 新鲜
                # 时间（保持拍不计数），状态从 −0.34 漂到 −1.27。
                corroborated = (lane_ok and self._lane_sm is not None
                                and abs(road_offset - self._lane_sm)
                                <= _LANE_BIAS_AGREE)
                if abs(road_offset) <= self.p.anchor_max_off or corroborated:
                    self._road_anchor = 0.0          # 道0=路中心（中轴巡航）
                    self._stale_t = 0.0
                    self._t_vcorr = 0.0              # 滤波（重）初始化：速度基线起算
                    self._last_corr_sign = None
                    self._reset_jump()
                    self._lane_rebias(road_offset, lane_ok)
                    if corroborated and abs(road_offset) > self.p.anchor_max_off:
                        self.state.executed_lane = road_offset
                else:
                    # 形成门饿死也要重基（13:29 局开局：车在左缘 ro=−2.9，形成门
                    # 拒定锚 7s、hold:0=杆恒 0 蹭墙）——超 stale 预算说明"中带观测"
                    # 等不来，直接重基到路上（exec:=ro、anchor:=0，道0=路中心）。
                    self._stale_t += dt_s
                    if self._stale_t >= self.p.anchor_stale_s and \
                            not self._line_disagrees(road_offset, lane_ok):
                        self._stale_rebase(road_offset, lane_ok)
            else:
                obs = road_offset - self._road_anchor
                r = obs - self.state.executed_lane
                # 已对账锁线可以否决不一致的新鲜深度：贴墙时单侧/绿区深度
                # 会报错 1 道以上，若照收 α 会把状态从墙边拽回路中，杆值
                # 跟着贴 0。线刚跳变/未对账时不否决；深度持续不一致仍由
                # 3 次新证据重基兜底（下面 large-innovation 路径）。
                line_obs = (float(lane_x) + self._lane_bias
                            if lane_ok and lane_x is not None else None)
                line_gap = (abs(obs - line_obs)
                            if line_obs is not None and self._lane_trusted
                            else None)
                veto = line_gap is not None and line_gap > _LANE_VETO_LANE
                if abs(r) <= self.p.obs_jump_max_lane and not veto:
                    self.state.executed_lane += self.p.obs_alpha * r
                    self._lane_rebias(obs, lane_ok)
                    # β 速度修正只认新证据拍：异步驻留协议下一套读数被多拍复用，
                    # 复用拍对同一新息重复注入曾把 0.36 道的找边翻跳 3 拍内放大
                    # 成 v_lat 饱和 ±2.5（2026-10-06 局 id17：实拍数据+产线增益
                    # 手算复现）——复用拍只收位置，速度修正留给下一套新读数。
                    # 时间基（2026-10-08 判据冻结）：r 的速度承载项=模型速度误差
                    # ×新证据间隔（obs 差携带真位移 u·Δt_e、模型同期走 v·Δt_e），
                    # 分母=自上次速度修正以来模型实际跑过的帧钟累计 T_e——驻留
                    # 协议下新证据 ~200ms 一套，旧口径按控制拍 33~66ms 归账放大
                    # 4~6×（v_lat_est ±1 道/s 拍级振荡根因）。逐拍新证据（测试/
                    # 合成模式）T_e=dt，数学与旧口径逐位一致。β 注入仍在
                    # v_lat_max 饱和内（16:44 局红线）。
                    if road_offset_new:
                        t_e = max(self._t_vcorr, dt_s)
                        # 位置-only（2026-10-18 走廊重建）：路心观测只修位置，
                        # 速度交给双积分模型——β 的三个抑制补丁（死区/反号/
                        # 加速度上限）都是为了压住噪声放大，收成一套规则。
                        # 回滚=obs_position_only=false（旧 α-β 逐位恢复）。
                        if not self.p.obs_position_only:
                            if abs(r) >= self.p.obs_beta_deadband_lane:
                                agree = (self._last_corr_sign is None
                                         or (r > 0) == self._last_corr_sign)
                                if agree or not self.p.obs_beta_sign_agree:
                                    dv = self.p.obs_beta * r / t_e
                                    lim = self.p.obs_beta_dv_rate_lane_s2 * t_e
                                    if lim > 0.0:
                                        dv = max(-lim, min(lim, dv))
                                    self.state.v_lat_est = max(
                                        -self.p.v_lat_max,
                                        min(self.p.v_lat_max,
                                            self.state.v_lat_est + dv))
                                self._last_corr_sign = r > 0
                        self._t_vcorr = 0.0
                    self._stale_t = 0.0
                    self._reset_jump()
                else:
                    # 被跳变门拒绝 ≠ 深度与线不一致：单帧坏检测/参考系跳变
                    # 同样可以校准线帧。20261025 局 p1 206 的坑：深度报 +1.87
                    # 被跳变门拒，线报 +1.32（同帧、差 0.55 道），偏置没更新、
                    # 信任一直为假，随后 40 拍保持期融合全关、状态漂到 −1.3。
                    line_trusted_before = self._lane_trusted
                    self._lane_rebias(obs, lane_ok, allow_bias=not veto)
                    # 持续偏移放行：只认新证据巡次。复用拍是同一读数，不重复
                    # 计数，也不清零——缺证据不是反证；hold/断供拍同理（墙边
                    # 150/227 拍是 hold，旧逻辑每遇一次就把 streak 清零，1 道级
                    # 偏移永远等不到 3 拍确认，控制器以为已在目标带）。
                    same = (self._jump_sign is not None
                            and (r > 0) == self._jump_sign
                            and abs(r - self._jump_last_r) <= _JUMP_SAME_TOL_LANE)
                    if road_offset_new:
                        self._jump_streak = self._jump_streak + 1 if same else 1
                        if not same:
                            self._jump_confirmed = False
                        self._jump_sign = r > 0
                        self._jump_last_r = r
                    # 锁线与深度独立一致的大新息：两源互证，立即重基。墙边
                    # fresh 拍 75~87% 深度与线在 0.6 道内一致；只等 3 拍确认
                    # 会把贴墙偏移拖到 1s+，期间目标误差被位置假象吃掉。
                    # 只认本拍开始前就已对账的线：线刚跳变、本拍才被
                    # `_lane_rebias` 重新信任时不能当互证（先融合几拍再说）。
                    corroborated = (line_obs is not None and line_trusted_before
                                    and abs(obs - line_obs)
                                    <= _LANE_VETO_LANE)
                    if self._jump_confirmed or corroborated or \
                            self._jump_streak >= self.p.obs_jump_confirm_ticks:
                        self._jump_confirmed = True
                        self.state.executed_lane = obs
                        self._stale_t = 0.0
                        self._reset_jump()
                        self._lane_trusted = False
                        self._lane_rebias(obs, lane_ok)
                    else:
                        self._stale_t += dt_s
                        # 时间判据重基（13:29 局教训：拍数门 40 次**喂入**
                        # 拒绝≈2s+，供数黑视期还根本不计数——车以 0 杆直开
                        # 5.5s 怼进右墙，ro=+1.7~+2.1 全程在喊）。陈旧即
                        # 重基到路上：exec:=ro、anchor:=0——"道0"恢复路中心
                        # 语义，闭环一拍复活。
                        if self._stale_t >= self.p.anchor_stale_s and \
                                not self._line_disagrees(road_offset, lane_ok):
                            self._stale_rebase(road_offset, lane_ok)
        elif self._road_anchor is not None:
            # 断供/保持计时：黑视本身=漂移温床。历史保持值仍算"有几何"，锚点
            # 保留——否则新鲜大读数回来要先过形成门（|ro|≤anchor_max_off）再
            # 等 1s，白白多一段盲开；超预算则按下面规则重基或丢锚。
            self._stale_t += dt_s
            # 不重置大新息确认：hold/断供是缺证据，不是反证。墙边大部分拍
            # 是 hold，旧逻辑在这里清零 streak，持续偏移永远等不到确认。
            # 保持期的运动观测：车道线连续横位。模型自走在没有位移证据时会
            # 按打杆量虚走（贴墙/被挡），线是唯一真实的位移来源；帧偏置由
            # 新鲜深度拍校准。线未锁定/重锁后未对账时不融合，退回模型。
            if lane_ok and self._lane_sm is not None and self._lane_trusted:
                self.state.executed_lane += _LANE_FUSE_GAIN * (
                    (self._lane_sm + self._lane_bias)
                    - self.state.executed_lane)
            # 陈旧重基在保持拍同样生效（"断供同样计时"）：墙边新鲜深度只占
            # ~1/4 拍，跳变门后的 3 次确认常常等不到；只计时不重基，状态会
            # 带着 1.3 道的假位置继续按目标向外打杆 1s+（20261026 局 s1#3：
            # 深度一直报 +1.5~+2.0，状态停在 +0.7，杆值把车按在墙上）。
            # 保持值本身是过期证据，但它是手边唯一的路心读数，重基后由后续
            # 新鲜拍与线融合继续修正。
            if self._stale_t >= self.p.anchor_stale_s:
                if road_offset is not None and \
                        not self._line_disagrees(road_offset, lane_ok):
                    self._stale_rebase(road_offset, lane_ok,
                                       allow_bias=False)
                elif road_offset is None:
                    self._road_anchor = None
                    self._last_corr_sign = None
                    # 计时不清零：断供已计入陈旧预算，下一条新鲜缘读数
                    # 应直接重基，而不是再等一个完整 anchor_stale_s。

        # ⑦ 下发链：归一 → 限幅（每 tick 变化上限）→ 死区（<256 归 0，256 保留）
        raw = int(round(self.state.steer_norm * _STICK_FULL))
        max_step = int(round(self.p.rate_limit_raw))
        raw = max(self._last_raw - max_step, min(self._last_raw + max_step, raw))
        if abs(raw) < self.p.stick_deadzone_raw:
            raw = 0
        self._last_raw = raw
        return GamepadCommand(steer_x=raw, throttle=self.p.throttle_raw)

    def _pd(self, target: float, gentle: bool = False,
            deadband: float | None = None,
            cap: float | None = None) -> float:
        """延迟补偿前瞻 PD（设计稿 §三）：状态外推到 t+τ 再入环，阻尼项反向。

        gentle=维护性转向（定中心/保持车道）：误差过死区才发力、杆幅限
        hold_stick_max（16:44 局：全权维护 PD 满左满右交替、速度起不来）。
        机动态（maneuver）传显式 deadband/cap：默认全权（0/None），阶段 2
        起用 maneuver_deadband_lane 与 maneuver_stick_max。"""
        x_pred = (self.state.executed_lane
                  + self._v_ctrl * self.p.lookahead_tau_s)
        return self._law(target - x_pred, gentle, deadband, cap)

    def _law(self, e: float, gentle: bool, deadband: float | None = None,
             cap: float | None = None) -> float:
        return law(e, self._v_ctrl, self.p, gentle, deadband, cap)
