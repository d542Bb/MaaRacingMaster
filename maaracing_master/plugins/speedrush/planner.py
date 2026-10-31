# -*- coding: utf-8 -*-
"""运动规划与控制层：决策输出 → 手柄指令（planner 设计稿 v1，§九 step 1）。

**分层归属**（红线 1）：本模块消费 ``tracking.DecisionOutput``（决策层唯一出口），
产出 ``GamepadCommand`` 供接线层下发到 ``core.capabilities.Gamepad``（方向
plugin→core）。不读像素、不读检测框、不读 boundary——弯道/退化判据真源在
decision.ValidationWatch（不变量 C1：本层不重复造判据，只消费 FSM 状态）。

**跟踪律**：位置误差先换成**期望横速**（``k_lat_lane_s`` × 误差，闭环带宽
的设定值），再按在线车速换成目标车头角，最后按 ``(ψ_des−ψ)`` 与角速度阻尼
出杆。跟踪律里没有车速、车重的常数——车速是在线估的比例，环路每拍按实测
位置/朝向闭环。

**自车横向运动学**（不变量 C2，2026-10-30 架构重做）：横向状态只有**朝向**
——杆是车头角指令，横移 ``x' = v·tanψ/LANE_W``（v 为在线估的比例，随车随速
变）。朝向由标线角度观测（互补滤波：角度变化积分 + 横速反解收敛），角速度
``r`` 只做阻尼。旧的双积分模型（``a_lat_gain`` 积分、``v_lat_max`` 饱和、
两路横速融合、``v_head`` 与刹停预算）已删除：它在闭环数据里不可辨识（R²
0.01~0.17、阻尼符号逐局翻转），且预测与实际位移弱相关。

A1 归一下自车恒 0、**目标读数**推不出自车位移，但**路缘两侧读数**可以：
路中心相对自车 = 自车横向位置的反号——``road_offset`` 入参（step 2.5，
V2 复盘"开环虚胖提前松杆"的闭环解）在双侧缘距稳定拍经 alpha-beta 滤波器
钉住 ``executed_lane``；**参考系=道0 路中心**（2026-10-02 维护者裁定中轴
巡航：x_target=0 即守路轴，牙子级边界信号不可依赖、守轴是唯一可守的安全
面），大初差经新息门+陈旧重基消化；无观测拍按朝向模型自走。

**纯函数纪律**（C3）：update 只吃 (decision, dt_s, current_fid) + 内部状态，
喂同一输入流回放复现同一杆值流。
"""

from __future__ import annotations

import math
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
# 线为主（2026-10-31）：线严格可用时它就是位置原点，深度不参与位置修正。
# 线短暂丢失时先用模型外推这么久，之后才让深度重建——避免单拍线闪断就
# 把位置交给一个可能错 1 道的深度源。
_LINE_REBUILD_S = 0.4
_LINE_POS_GAIN = 0.7       # 线位置修正增益（检测 ~10Hz、每拍同一读数可重复收）

# 贴边逃生门（道）：外车道中心 ±1 再让 0.4；超过即按全权回带（gentle 的
# 0.45 杆幅在 1.4 道级误差下需要 1s+ 才能回带，期间车停在墙侧）。
_WALL_ESCAPE_LANE = 1.4

# 朝向与比例（2026-10-30 架构）：横向只有一条律——车头角决定横移。
# x' = v·tan(ψ)/LANE_W，ψ 由标线角度减慢基线量得，v 只随车/随速慢变、必须
# 在线估。旧的「杆值→横向加速度」双积分与两路横速融合已删除：它在闭环数据里
# 既不可辨识（R² 0.01~0.17、阻尼符号逐局翻转），预测也与实际位移不相关。
_HEAD_SLOPE_WINDOW_S = 0.6   # 标线横移斜率窗（比例估计的输入）
_HEAD_REL_MAX = 0.6          # ψ 物理上限（rad，对应 34°）
_HEAD_AGE_MAX_MS = 250.0     # 线角读数年龄门（超过即不是本拍证据）
_LINE_STALE_S = 0.3          # 标线横移 TTL（线锁定拍才会更新）
_LINE_JUMP_RESET = 0.8       # 线横位单拍跳超此值 = k 重锚/换线，不是横移

_PSI_MIN_LINES = 2          # 车头角只认 ≥2 条线融合的角度：单条线（含短黄线）
                            # 实测会给出 20°+ 的假角度（n_locked=1，theta 0.39 rad）
_PSI_STALE_S = 0.6          # 无新线角证据时 ψ 的保持上限（超过即无朝向）
_R_TAU_S = 0.15             # 角速度低通：ψ 只在检测拍更新，差分会放大单拍噪声
_R_STALE_S = 0.3            # 角速度 TTL：速率只在短时内有意义
# 朝向向「横速反解」收敛的时间常数：积分管 0.5s 级的快变，这条管稳态
# （相机装偏、稳态曲率不会被当成常值朝向攒下来）。
_PSI_CORR_TAU_S = 0.5
# 单拍角度速率的物理上限（rad/s ≈115°/s）：真车偏航率在 0.5 rad/s 量级，
# 越过它的只能是单帧坏角度（换线/误锁），不能让它一步把朝向拽走。
_HEAD_RATE_MAX = 2.0
_SPD_TAU_S = 20.0           # 比例的泄漏记忆（车速慢变；更长的记忆跟不上加速）
_SPD_DEN_MIN = 0.03         # Σtan²ψ 的发布下限（≈0.2 道/s 噪声下 σ≈4 m/s）
# 证据衰减到下限后，上一次的读数再保留这么久（秒）。比例只能靠**横移**观测：
# 车一直走直线时证据自然衰减，但车速在 20s 内不会变一倍——直线巡航十秒不该
# 让计划整个关掉（2026-10-30 两局：plan:no_speed 占 22~25%，且都是成段的
# 十秒窗口，那段里车只剩回中兜底）。保留的是**测过**的速度，不是兜底常数；
# 置信度照实归零，调用方看得到证据已过期。
_SPD_HOLD_S = 20.0
_SPD_DEN_FULL = 0.10        # 置信度到 1 的 Σtan²ψ（≈σ 2 m/s）
_SPD_MIN_TAN = 0.02         # 参与估计的最小 |tanψ|（1.1°）：更小的朝向上
                            # v_line 噪声压过信号，条件数不足
_SPD_MIN_V_LANE_S = 0.05    # 参与估计的最小 |v_lat|（道/s）
_SPD_MIN_MPS = 5.0          # 比例的物理域：越界不发布（宁缺勿错，不兜底）
_SPD_MAX_MPS = 120.0


class HeadingObserver:
    """朝向观测（横向状态的唯一测量源）。

    ψ = 线角速率的积分（积分掉相机装偏与稳态曲率）；r = 低通角速度；
    v_scale = 由「标线横移斜率 ÷ tanψ」在线估出的比例（≈车速，m/s），20s
    泄漏遗忘、越出物理域不发布。标线横移斜率 ``v_line`` 保留，作为比例估计
    的输入与「比例还没估出来时」的横速来源。
    """

    def __init__(self, p: Planner) -> None:
        self.p = p
        self.reset()

    def reset(self, keep_heading: bool = False) -> None:
        """清线横位观测链；``keep_heading=True`` 时保留朝向影子。

        线横位整道跳（换线/重锚）要清的是**线帧**的东西——样本窗、v_line、
        v_head 与偏置基线；ψ/r 来自线**角**，与线帧横位无关，比例的累积更是
        跨秒统计（20261030_181838 实测：共用一个 reset 时比例被线跳反复清零，
        置信度 0→1→0.3 来回跳，`p1h` 只有 9% 的拍有值）。所以两处线帧重置
        传 ``keep_heading=True``。"""
        if not keep_heading:
            self._t = 0.0
            # 朝向：ψ=线角速率的积分（rad，右正），r=低通角速度，
            # v_scale=由「横移速度 ÷ tanψ」在线估的比例（≈车速，m/s）。
            self.psi: float | None = None
            self._psi_t = -1e9
            self._psi_prev_th: float | None = None
            self._psi_prev_t = -1e9
            self.r: float | None = None
            self._r_t = -1e9
            self.v_scale: float | None = None
            self.v_scale_conf = 0.0      # 0~1：Σtan²ψ 相对发布下限的量级
            self._spd_num = 0.0
            self._spd_den = 0.0
            self._spd_t = -1e9          # 最近一次收到比例证据的时刻
            self._spd_pub_t = -1e9      # 最近一次发布有效比例的时刻（保留窗口起点）
        self._samples: list[dict] = []
        self._last_fid: int | None = None
        self._last_line = -1e9       # 标线横移上次更新时刻（v_line TTL）
        self.v_line: float | None = None

    def psi_age_s(self) -> float:
        """ψ 距上一次线角证据的秒数；无 ψ 时返回 inf（=不可用于推横速）。"""
        if self.psi is None:
            return math.inf
        return self._t - self._psi_t

    def update(self, dt: float, lane_x: float | None, lane_ok: bool,
               theta: float | None, theta_ok: bool, fid: int | None,
               age_ms: float | None, n_locked: int | None = None) -> None:
        if not (isinstance(dt, (int, float)) and math.isfinite(dt) and dt > 0):
            return
        self._t += dt
        if self.v_line is not None and self._t - self._last_line > _LINE_STALE_S:
            self.v_line = None
        # 朝向影子的 TTL 与比例的泄漏遗忘：每拍都走（无线拍也要遗忘）
        if self.psi is not None and self._t - self._psi_t > _PSI_STALE_S:
            self.psi = None
        if self.r is not None and self._t - self._r_t > _R_STALE_S:
            self.r = None
        decay = math.exp(-dt / _SPD_TAU_S)
        self._spd_num *= decay
        self._spd_den *= decay
        if self._spd_den < _SPD_DEN_MIN:
            # 证据衰减到下限：直线巡航没有横移、比例无法更新。上一次测到的
            # 读数仍是车速的测量（慢变），保留 _SPD_HOLD_S；过期才作废。
            # 置信度照实归零——调用方要能看出这条读数已经没有新证据。
            if self._t - self._spd_pub_t > _SPD_HOLD_S:
                self.v_scale = None
            self.v_scale_conf = 0.0
        else:
            self.v_scale_conf = min(1.0, self._spd_den / _SPD_DEN_FULL)
            sp = LANE_W * self._spd_num / self._spd_den
            self.v_scale = sp if _SPD_MIN_MPS <= sp <= _SPD_MAX_MPS \
                else None
            if self.v_scale is not None:
                self._spd_pub_t = self._t
        if (not lane_ok or lane_x is None
                or not math.isfinite(float(lane_x))):
            return
        if fid is not None and fid == self._last_fid:
            return                       # 同一检测帧被 20Hz 控制拍重复读取
        self._last_fid = fid
        x = float(lane_x)
        if self._samples and abs(x - self._samples[-1]["x"]) > _LINE_JUMP_RESET:
            # 线横位整道跳 = k 重锚/换线，不是位移：清窗，别把它积成横速。
            self._samples.clear()
            self.v_line = None
        th: float | None = None
        if (theta is not None and theta_ok and math.isfinite(float(theta))
                and age_ms is not None
                and float(age_ms) <= _HEAD_AGE_MAX_MS):
            th = float(theta)
        s = {"t": self._t, "x": x, "v": None}
        self._samples.append(s)
        cut = self._t - 2.0 * _HEAD_SLOPE_WINDOW_S
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
                self._last_line = self._t
        # 朝向：ψ/r 与本拍新测到的横速一起，更新在线比例。
        if th is not None:
            psi = self._head_state(th, n_locked)
            if psi is not None:
                self._scale_accum(psi)

    def _head_state(self, th: float, n_locked: int | None) -> float | None:
        """ψ 与 r：互补滤波——角速率积分管快变、横速反解管稳态。

        两路各自的病：线角里的常值成分（相机装偏、稳态曲率）被 2s 高通吃掉
        时，持续 1s 的车头角只剩六成、回正段还会先变号（控制器按它判停，
        实车仍偏，台架冲过目标 0.22 道）；纯积分 θ̇ 没有这个衰减，但稳态
        曲率会被当成常值朝向攒下来。所以：**本拍角度变化直接入 ψ**（无滤波、
        无偏），再以 ``_PSI_CORR_TAU_S`` 向「横速 ÷ 在线比例」反解的朝向收敛
        ——后者来自``v_lat = v·tanψ`` 这条物理关系本身，没有高通、稳态诚实，
        代价只是 0.6s 斜率窗的滞后。``r`` 仍是低通角速度，只做阻尼。

        单条线的角度不可用：20261030_171216 fid 1561 只锁到一条短黄线时给出
        0.39 rad（22°）。``n_locked`` 缺省（离线/单测未供）时不加这条门。"""
        if n_locked is not None and n_locked < _PSI_MIN_LINES:
            return None
        if self.psi is None:
            self.psi = 0.0
            self._psi_t = self._t
            self._psi_prev_th = th
            self._psi_prev_t = self._t
            self.r = None
            return self.psi
        gap = self._t - self._psi_prev_t
        if gap <= 1e-3:
            self._psi_prev_th = th
            return self.psi
        rate = (th - self._psi_prev_th) / gap
        rate = max(-_HEAD_RATE_MAX, min(_HEAD_RATE_MAX, rate))
        self._psi_prev_th = th
        self._psi_prev_t = self._t
        if self.r is None or gap > _R_STALE_S:
            self.r = rate            # 断档后的第一拍：差分会跨过缺测，不滤波
        else:
            a = 1.0 - math.exp(-gap / _R_TAU_S)
            self.r += a * (rate - self.r)
        self._r_t = self._t
        psi = self.psi + rate * gap
        kin = None
        if (self.v_line is not None and self.v_scale is not None
                and self.v_scale > 1.0
                and self._t - self._last_line <= _LINE_STALE_S):
            kin = math.atan(self.v_line * LANE_W / self.v_scale)
        if kin is not None:
            beta = 1.0 - math.exp(-gap / _PSI_CORR_TAU_S)
            psi += beta * (max(-_HEAD_REL_MAX, min(_HEAD_REL_MAX, kin)) - psi)
        self.psi = max(-_HEAD_REL_MAX, min(_HEAD_REL_MAX, psi))
        self._psi_t = self._t
        return self.psi

    def _scale_accum(self, rel: float) -> None:
        """在线比例 v（m/s）：由「本拍线速 ÷ tanψ」给的最小二乘增量。

        ``v_lat = (v/LANE_W)·tanψ`` 是物理关系本身，不是拟合的经验式；小角度
        时 v_lat 噪声压过信号，所以只收 |tanψ|≥``_SPD_MIN_TAN`` 的拍，且整个
        窗口按 ``_SPD_TAU_S`` 泄漏遗忘——车速慢变，短记忆跟不上加速、长记忆
        跟不上减速。线速是 0.6s 因果斜率（滞后约 0.3s），在「建立朝向」与
        「回正」两段上的偏差方向相反，整局互相抵消，残差留在置信度里。"""
        tan = math.tan(rel)
        v = self.v_line
        if v is None or abs(tan) < _SPD_MIN_TAN \
                or abs(v) < _SPD_MIN_V_LANE_S:
            return
        self._spd_num += tan * v
        self._spd_den += tan * tan
        self._spd_t = self._t

    def debug(self) -> dict:
        return {
            "v_line": None if self.v_line is None
            else round(self.v_line, 3),
            # 朝向影子：psi=相对车道车头角（rad）、r=角速度（rad/s）、
            # vsp=在线比例（m/s）、scf=置信度、pa=ψ 的龄（ms）。
            # 全部只进 trace，不参与控制。
            "psi": None if self.psi is None else round(self.psi, 4),
            "r": None if self.r is None else round(self.r, 3),
            "psi_age": (None if self.psi is None
                        else round((self._t - self._psi_t) * 1000.0, 0)),
            "vsp": None if self.v_scale is None
            else round(self.v_scale, 1),
            "scf": round(self.v_scale_conf, 2),
            "spa": (None if self._spd_t < 0
                    else round((self._t - self._spd_t) * 1000.0, 0)),
        }


def stick_of(psi_des: float, psi: float, r: float, p: Planner,
             gentle: bool = False, cap: float | None = None) -> float:
    """朝向跟踪：把「目标车头角」换成杆值（单一真源，处处同一条律）。

    杆不是横向加速度、也不是横向速度，是**车头角指令**：先按
    ``(ψ_des − ψ)`` 打，再用角速度 ``r`` 阻尼。停住靠的是「朝向回到 0」，
    不是刹车预算——横速按 ``x' = v·tanψ`` 由朝向给出，ψ 归零即不再横移。

    ``gentle`` 给维护性转向：目标朝向压到 ``heading_hold_rad``、杆幅
    ``hold_stick_max``。"""
    psi_lim = p.heading_hold_rad if gentle else p.heading_max_rad
    psi_des = max(-psi_lim, min(psi_lim, psi_des))
    u = p.k_psi * (psi_des - psi) - p.k_r * r
    lim = p.hold_stick_max if gentle else 1.0
    if cap is not None:
        lim = cap
    u = max(-lim, min(lim, u))
    return max(-1.0, min(1.0, u))


def heading_for_error(e: float, v_mps: float | None, p: Planner) -> float:
    """位置误差 → 目标车头角（先把误差换成**期望横速**，再按在线车速换算）。

    期望横速 = ``k_lat_lane_s × e``（道/s）——这是**闭环带宽**的设定值（一条
    道误差要在 ~1s 内收完），与车重、车速无关；由 ``x' = v·tanψ/LANE_W``
    反解出目标朝向 ``tanψ = 期望横速 × LANE_W / v``。所以同一套参数在
    30 m/s 和 100 m/s 下给出同样的收敛时间，不需要按车速插值增益。
    车速还没估出来时退回固定比例 ``k_head_lane``（保守小角度）。"""
    if v_mps is not None and v_mps > 1.0:
        return math.atan(p.k_lat_lane_s * e * LANE_W / v_mps)
    return p.k_head_lane * e


def law(e: float, psi: float, r: float, p: Planner, gentle: bool = False,
        deadband: float | None = None, cap: float | None = None,
        v_mps: float | None = None) -> float:
    """位置误差 → 目标朝向 → 杆值（见 ``heading_for_error``）。

    死区 ``hold_deadband_lane`` 只吃位置误差：带内不换朝向。"""
    db = (p.hold_deadband_lane if gentle else 0.0) \
        if deadband is None else deadband
    if abs(e) < db:
        e = 0.0
    return stick_of(heading_for_error(e, v_mps, p), psi, r, p, gentle, cap)


def step(x: float, psi: float, psi_cmd: float, v: float, dt: float,
         p: Planner) -> tuple[float, float]:
    """朝向运动学单一真源：朝向一阶接近指令，横位由朝向积分。

    ``ψ' = (ψ_cmd − ψ)/tau_heading_s``（把执行链的响应并入模型），
    ``x' = v·tanψ/LANE_W``。返回 ``(x, psi)``；生产控制拍与计划滚动逐位一致。
    没有前向速度（``v<=0``）时不产生横移——这是物理事实，不是兜底。"""
    a = 1.0 - math.exp(-dt / max(p.tau_heading_s, 1e-3))
    psi += a * (psi_cmd - psi)
    psi = max(-p.heading_max_rad, min(p.heading_max_rad, psi))
    if v > 0.0:
        x += (v / LANE_W) * math.tan(psi) * dt
    return x, psi


# 可达性换算的参考车速（m/s）：闭环律 ``v_lat = k_lat·e`` 在朝向不被限幅时
# 与车速无关（误差换期望横速、再由车速反解朝向，v 在两端抵消）。限幅只在
# 大误差低速时生效（45 m/s 下约 1.6 道以上），1.2 道以内任何车速同一答案。
_MOVE_TIME_V_REF_MPS = 45.0


def lane_move_time(dx_lane: float, *, k_lat_lane_s: float, tau_heading_s: float,
                   heading_max_rad: float, settle_lane: float = 0.1,
                   v_mps: float = _MOVE_TIME_V_REF_MPS,
                   dt: float = 0.02) -> float:
    """闭环律下横移 ``|dx|`` 道、残差收到 ``settle_lane`` 所需秒数。

    与 ``step`` 同一条律（ψ 一阶接近 ``atan(k_lat·e·LANE_W/v)``、横位由
    tanψ 积分、朝向限幅）：决策层的可达性判断必须和执行层同一套车——两套
    模型会让决策去够一个执行到不了的点（"决策说能、计划说不能"）。
    """
    e = abs(dx_lane)
    if e <= settle_lane:
        return 0.0
    if v_mps <= 1.0 or k_lat_lane_s <= 0.0 or tau_heading_s <= 0.0:
        return math.inf
    a = 1.0 - math.exp(-dt / tau_heading_s)
    psi, t = 0.0, 0.0
    for _ in range(int(10.0 / dt)):
        psi_des = math.atan(k_lat_lane_s * e * LANE_W / v_mps)
        psi_des = max(-heading_max_rad, min(heading_max_rad, psi_des))
        psi += a * (psi_des - psi)
        e -= (v_mps / LANE_W) * math.tan(psi) * dt
        t += dt
        if e <= settle_lane:
            return t
    return math.inf


@dataclass(frozen=True)
class GamepadCommand:
    """本层唯一出口：左摇杆 x（转向）+ 右扳机（恒油门，RULES §4.1 权威）。"""

    steer_x: int
    throttle: int


@dataclass
class EgoLateralState:
    """自车横向状态（本层持有，回执决策层）。

    **朝向是状态，速度是它的推论**：横移速度 = 在线比例 × tan(车头角)，
    不再有独立的「横速积分」状态。``psi``/``yaw_rate`` 由标线角度量得，
    ``v_mps`` 由「实测横移 ÷ tanψ」在线估（只随车速慢变，不写死）。"""

    executed_lane: float = 0.0   # 车道单位，A1 系（重锚拍被观测值覆写）
    heading: float = 0.0         # 相对车道的车头角（rad，右正）
    yaw_rate: float = 0.0        # 车头角速度（rad/s，低通）
    v_mps: float | None = None   # 在线比例（≈车速，m/s）；None=证据不足
    v_lat: float = 0.0           # 横向速度（道/s）：ψ×比例；缺比例时用标线斜率
    steer_norm: float = 0.0      # 低通后的当前杆值指令（归一域）


class LateralPlanner:
    """延迟补偿前瞻 PD + 开环积分 + 观测重锚。单线程 owner 使用。"""

    def __init__(self, p: Planner):
        self.p = p
        self.state = EgoLateralState()
        self._last_raw = 0            # 上一拍下发杆值（限幅起点）
        self._expired_ticks = 0
        self._abort_hold_lane: float | None = None
        self._frame_shift_ticks = 0   # 计划判参考系分歧的连续拍数
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
        # 线为主（2026-10-31）：线曾可作为位置原点时，短断供先模型外推
        self._line_recent = False
        self._line_gap_t = 0.0
        # 上一拍低通前杆值（负一~一；trace 标定用，行为不消费）
        self.last_steer_raw = 0.0
        # 本拍**实际执行**的重基分支（trace-only，行为不消费）：⑥b 的几条
        # 重基路径同拍互斥，只记真正跑的那一条，None=本拍没有重基。
        # form_align=形成拍贴边互证直接对齐；form_starve=形成门饿死重基；
        # jump_confirm=大新息确认重基；stale_reject=有锚持续被拒的陈旧重基；
        # hold_stale=保持拍陈旧重基。
        self.last_rebase: str | None = None
        # 上一拍计划前馈命令：换候选时限速用；计划中断后置空，重入首拍不限。
        self._last_plan_u: float | None = None
        # 计划短断供保持（_PLAN_HOLD 段）：最后一条有效航向指令 + 剩余拍数。
        # 计划的无效段 81% 只有 1~2 拍，逐段切回 PD 会让方向盘"走—停"来回。
        self._plan_hold_left = 0
        self._plan_hold_psi: float | None = None
        self._plan_hold_cap: float | None = None
        self._plan_hold_commit = False
        # 朝向观测（横向状态的唯一测量源：ψ / r / 在线比例）
        self.head = HeadingObserver(p)
        self._heading_stale = False
        # 横向位置证据新鲜度（2026-10-31）：距上一次新证据的秒数 + 不可信标志。
        # 不可信期内位置是外推值：冻结模型积分、停线融合、执行层把杆归零
        # （只按朝向回正，不积分旧转杆）、计划不接管（见 update 与
        # config.state_blind_s）。
        self._blind_t = 0.0
        self.state_unverified = False

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
        self.state.heading = 0.0
        self.state.yaw_rate = 0.0
        self.state.v_mps = None
        self.state.v_lat = 0.0
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
        """横向状态的可见面（trace）。

        ``h``=车头角（rad）、``rr``=角速度、``vmp``=在线比例（≈车速）、
        ``vl``=由「ψ×比例」导出的横速（道/s）、``pa``=ψ 的龄。
        这就是决策与执行共用的全部横向状态——不再有第二套速度估计。"""
        d = {"h": None if self.head.psi is None else round(self.head.psi, 4),
             "rr": None if self.head.r is None else round(self.head.r, 3),
             "vmp": self.state.v_mps,
             "vl": round(self.state.v_lat, 3),
             "rebase": self.last_rebase,
             # 位置证据面（2026-10-31）：unver=本拍位置是不可信外推值，
             # blind=距上一次新鲜位置证据的秒数（0=本拍有测量）。
             "unver": bool(self.state_unverified),
             "blind": round(self._blind_t, 2)}
        d.update(self.head.debug())
        return d

    def _sync_heading_state(self) -> None:
        """把观测写进状态，并导出横移速度。

        横移速度只有一条算式：``v_lat = v·tanψ/LANE_W``。比例还没估出来时
        退回标线横移斜率（同一物理量的另一种测量）；两者都没有就是 0——
        不猜、也不兜底常数。

        ψ 超过 ``heading_max_rad``（控制律自己的物理上限）时，这一拍推出的
        横速不可信：19° 在 45 m/s 上等于 5 道/s，实车做不到。不截断的话，
        一次朝向过冲就能把自车位置在无观测的拍上推出 2~3 道（实测帧 2360~
        2400：exec 从 −0.5 冲到 −3.1，随后 plan 判参考系分歧、整段停控）。
        截断只作用于**导出速度**（模型积分的输入），状态里的 ψ 原样保留。"""
        h = self.head
        self._heading_stale = h.psi is None
        if h.psi is not None:
            self.state.heading = h.psi
            self.state.yaw_rate = 0.0 if h.r is None else h.r
        self.state.v_mps = h.v_scale
        # ψ 状态可以按 _PSI_STALE_S 保留给控制律，但用它推**横速**要更严：
        # 旧朝向 × 车速就是一段时间内的位置外推，0.6s 的龄在该窗口里会把
        # 自车位置推出 0.7~1 道（20261031_104321_p2 137.5s 实测 1.2~1.8 道/s），
        # 而这段窗口恰好也是线与深度都没新证据的盲窗。横速只认新鲜 ψ。
        if (h.psi is not None and h.v_scale is not None
                and h.psi_age_s() <= self.p.heading_vel_max_age_s):
            psi_env = max(-self.p.heading_max_rad,
                          min(self.p.heading_max_rad, h.psi))
            self.state.v_lat = h.v_scale * math.tan(psi_env) / LANE_W
        elif h.v_line is not None:
            self.state.v_lat = h.v_line
        else:
            self.state.v_lat = 0.0

    @property
    def line_primary(self) -> bool:
        """线是否为位置原点（含 ≤_LINE_REBUILD_S 的短断供保持）。

        供数层据此把走廊中心也锚在线上：线失锁的那一拍若立刻回深度中心，
        场景参考系会在两个原点间来回换（frame_shift 反复否决）。"""
        return self._line_recent and self._line_gap_t <= _LINE_REBUILD_S

    # ---------- 主入口 ----------

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
               lane_age_ms: float | None = None,
               lane_n_locked: int | None = None,
               lane_pos_ok: bool = False) -> GamepadCommand:
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
        self.last_rebase = None          # 每拍清：只记本拍实际执行的重基分支

        # 线横位平滑（保持期运动观测）：只在锁线拍更新；COAST/无值保持上一值。
        # 重锁后的第一条锁线可能换了线/换了帧：重置平滑并停用融合；线自身
        # 单拍跳超阈值同理。等新鲜深度与线一致时由 _lane_rebias 恢复。
        if lane_relock:
            self._lane_relock_seen = True
            self.head.reset(keep_heading=True)
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
                self.head.reset(keep_heading=True)
            else:
                self._lane_sm += _LANE_SMOOTH_ALPHA * (lane_x - self._lane_sm)

        # 朝向观测：ψ/r 从标线角度量得，在线比例从「横移 ÷ tanψ」估；
        # 横移速度由两者导出（一条算式），决策、计划、控制共用。
        self.head.update(dt_s, lane_x, lane_ok, lane_theta, lane_theta_ok,
                         lane_fid, lane_age_ms, lane_n_locked)
        self._sync_heading_state()

        # 横向位置证据（2026-10-31）：位置证据=真实深度读数（非历史保持值；
        # 复用拍是同一帧深度的短驻留，仍算证据）或已对账锁线；运动证据=新鲜 ψ
        # 或新鲜标线横移（v_line，见 _sync_heading_state）。有位置证据就是测量；
        # 只有运动证据还能按同一套运动学短时外推（「知道车头朝向就能跟住」的
        # 开环面，台架锁着）；两者都没有才是真盲窗：超过 state_blind_s 就不
        # 假装知道车在哪，重获时一步重基到新证据。
        pos_fresh = ((road_offset is not None and not road_offset_hold)
                     or (lane_pos_ok and lane_x is not None)
                     or (lane_ok and self._lane_trusted))
        motion_fresh = (self.state.v_lat != 0.0
                        or (self.head.psi is not None
                            and self.head.psi_age_s()
                            <= self.p.heading_vel_max_age_s))
        was_unverified = self.state_unverified
        self._blind_t = 0.0 if (pos_fresh or motion_fresh) \
            else self._blind_t + dt_s
        self.state_unverified = self._blind_t > self.p.state_blind_s
        if (was_unverified and not self.state_unverified
                and road_offset is not None and not road_offset_hold
                and self._stale_t < self.p.anchor_stale_s):
            # 盲窗在陈旧预算内收尾：当拍重基，不等预算走满。预算已满的
            # 情况交给 ⑥b 的 stale_reject（同一条重基，诊断面保持原样）。
            self.last_rebase = "reacquire"
            self._stale_rebase(road_offset, lane_ok)

        # ① 过期判定（valid_until_fid 含边界；第 4 个连续过期拍起按 CONSERVE）
        fresh = current_fid <= decision.valid_until_fid
        self._expired_ticks = 0 if fresh else self._expired_ticks + 1
        stale_hold = (not fresh) and self._expired_ticks <= self.p.hold_max_ticks

        # ③ 控制律（状态消费表，设计稿 §四；优先级：CONSERVE/FAULT 接管 > 过期保持）
        st = decision.state
        # 参考系分歧计数（计划判 frame_shift）：场景帧目标与 planner 帧实测位
        # 不在同一坐标里，跨帧比较正是"目标被拽进错帧"的入口。
        frame_shift = (plan is not None and st is DecisionState.CRUISE
                       and getattr(plan, "reason", None) == "plan:frame_shift")
        self._frame_shift_ticks = (self._frame_shift_ticks + 1
                                   if frame_shift else 0)
        if st is not DecisionState.CONSERVE:
            self._conserve_hold = None
        plan_held = False           # 本拍是否走"计划短断供保持"
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
                ro_pred = (road_offset
                           + self.state.v_lat * self.p.lookahead_tau_s)
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
        elif st is DecisionState.CRUISE and self.state_unverified:
            # 位置不可信（线+深度都没有新证据，见 update 顶部）：本拍不知道
            # 车在哪，任何按位置算的回带都是拿假位置下判断（20261031_104321_p2：
            # 状态冻在 +1.39、画面已贴左墙）。
            # 但不能原样保持上一笔转杆：转角指令在开环下会一直积分成整道横移
            # （164953/170923：断供期保持 +0.26~+0.45 的杆几秒，车被带到对侧
            # 墙）。这里只把**朝向**拉回 0——它只用最近测得的车头角，不依赖
            # 位置；朝向也失效时杆自然归零（_law 内按 ψ=0 解）。
            self._abort_hold_lane = None
            steer_raw = self._law(0.0, gentle=True)
        elif (st is DecisionState.CRUISE and plan is not None
              and not plan.valid and self._plan_hold_left > 0
              and self._plan_hold_psi is not None
              and not self._plan_hold_commit
              and getattr(plan, "reason", None) != "plan:infeasible_corridor"):
            # 计划短断供：参考系/速度估计闪断时，上一拍的计划命令还是本拍
            # 最可信的意图——继续打它（同一套律＋限速），不切回 PD。PD 的
            # 目标是另一套口径（决策 x_target / 保持车道），每拍来回切会让
            # 方向盘在"走"和"停"之间跳（2026-10-30 实测切换 24~31 次/10s）。
            plan_held = True
            self._plan_hold_left -= 1
            self._abort_hold_lane = None
            steer_raw = self._law_psi(self._plan_hold_psi,
                                      cap=self._plan_hold_cap)
            if self._last_plan_u is not None:
                lim = self.p.plan_u_slew_s * dt_s
                steer_raw = max(self._last_plan_u - lim,
                                min(self._last_plan_u + lim, steer_raw))
            self._last_plan_u = steer_raw
        elif frame_shift and self._frame_shift_ticks <= self.p.frame_hold_max_ticks:
            # 参考系分歧：计划无效、决策目标在场景帧、实测位在 planner 帧，
            # 直接拿它们做 PD 是把车往错帧里拽（20261030_122416 s1 87s：假路心
            # 把目标拉到左缘，车贴左黄线）。只用 planner 自己的状态保持当前道，
            # 等参考系重新对齐（|fd|≤frame_delta_hold_m）再交还计划/目标；
            # 超过 frame_hold_max_ticks 仍未对齐才退回旧回退（长弯道里无限
            # 直开同样危险）。
            self._abort_hold_lane = None
            steer_raw = self._pd(self.state.executed_lane, gentle=True)
        elif decision.x_target is not None:
            self._abort_hold_lane = None
            # 场跟随：保持态=维护性定中心（gentle，16:44 局）；机动态
            # （目标距自车足够远）=全权——机动态死区/杆幅另配。选择层
            # maneuver 现在按安全区间（约 0.17 道）置位，但执行层全权仍要求
            # 误差超 maneuver_full_error_lane（0.3 道）：否则计划因参考系分歧
            # 退回 PD 时，0.2 道的日常修正会被当成机动打满舵
            # （20261027_150850_1 s2 实测满杆 122→227 的回归）。
            # 贴边逃生：|executed|超 _WALL_ESCAPE_LANE（保持车道 ±1 + 0.4）时
            # 也走全权——墙是硬约束，gentle 的 0.45 杆幅把 1.4 道级回带拖成
            # 1s+（20261026 局：贴边拍 53/99 是计划走廊不可行被否决、PD 以
            # gentle 回带，整段停在墙侧）。回带方向仍由目标给出，不改目标。
            if ((decision.maneuver and abs(
                    decision.x_target - self.state.executed_lane)
                   > self.p.maneuver_full_error_lane)
                    or abs(self.state.executed_lane) > _WALL_ESCAPE_LANE):
                steer_raw = self._pd(
                    decision.x_target, gentle=False,
                    deadband=self.p.maneuver_deadband_lane,
                    cap=self.p.maneuver_stick_max)
            else:
                steer_raw = self._pd(decision.x_target, gentle=True)
        else:
            steer_raw = self.state.steer_norm    # 防御：非保守态却无目标，保持

        # 计划接管（motion.mode=live 且计划有效）：计划给的是**车头角指令**，
        # 交给同一条朝向律执行——计划怎么预测就怎么打杆，没有第二套前馈/跟踪
        # 项，也就没有两套关于「车会怎么动」的说法。承诺并线例外：跟踪决策
        # 目标道（计划候选的视界是 2s，承诺道是决策层的事，不能让它静默换）。
        if (plan is not None and plan.valid and st is DecisionState.CRUISE
                and not self.state_unverified):
            merge_commit = (getattr(decision, "phase", "keep") == "merging"
                            and decision.x_target is not None)
            if merge_commit:
                steer_raw = self._pd(
                    decision.x_target, gentle=False,
                    cap=self.p.maneuver_stick_max)
                # 承诺并线的目标是决策层的道，不能拿来当"短断供保持"的
                # 航向——断供后并线该由 PD 继续跟目标道。
                self._plan_hold_left = 0
                self._plan_hold_commit = True
            else:
                # 维护层（意图在中心带内）不得用满杆追偏掉的路心；只有真的
                # 预测到碰撞（collision_cost>0）才放开全权——那是避让。"有距
                # 无速"的小代价不算：计划的 value 现在只有代价、恒 ≤0，用
                # value_total>=0 判维护会让维护态几乎永远全权（20261031 两局
                # 的摆头来源之一）。
                cap = (self.p.hold_stick_max
                       if getattr(plan, "layer", None) == 4
                       and getattr(plan, "collision_cost", 0.0) <= 0.0
                       else None)
                steer_raw = self._law_psi(plan.psi_feedforward, cap=cap)
                self._plan_hold_psi = plan.psi_feedforward
                self._plan_hold_cap = cap
                self._plan_hold_commit = False
                self._plan_hold_left = self.p.plan_hold_max_ticks
            # 换目标时航向指令会阶跃；相对上一拍计划命令限速，避免方向盘一步
            # 打半圈。首帧/计划重入（None）不限制——那是交接，不是候选抖动。
            if self._last_plan_u is not None:
                lim = self.p.plan_u_slew_s * dt_s
                steer_raw = max(self._last_plan_u - lim,
                                min(self._last_plan_u + lim, steer_raw))
            self._last_plan_u = steer_raw
        elif (plan is not None and st is DecisionState.CRUISE
              and not self.state_unverified
              and plan.reason == "plan:infeasible_corridor"):
            # 计划判「自车压在走廊边距内」：无效计划选的是"最快脱离侵入"
            # 候选，直接执行它的车头角指令，不套保持死区/维护杆幅
            # （20261026 局 s2#4 21.6s：目标 0.93、车位 1.19、杆 −0.18）。
            steer_raw = self._law_psi(plan.psi_feedforward)
            self._last_plan_u = None
        elif not plan_held:
            self._last_plan_u = None

        self.last_steer_raw = steer_raw
        # ⑤ 杆值低通（限速链与滑行预测的起点）；⑥ 横位由**朝向**积分：
        # x += v·tanψ·dt。ψ 是测量量，由观测器每拍更新，这里不再有速度状态。
        alpha = 1.0 - math.exp(-dt_s / max(self.p.tau_steer_s, 1e-3))
        self.state.steer_norm += alpha * (steer_raw - self.state.steer_norm)
        if not self.state_unverified:
            self.state.executed_lane += self.state.v_lat * dt_s

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
        # ⑥b 线为主（2026-10-31）：线严格可用时，它就是车的位置原点——深度
        # 不再参与位置修正（只在下面「线丢失 ≥_LINE_REBUILD_S」后重建）。
        # 这条分支必须排在深度之前：旧结构里深度新鲜拍是唯一 correct 步，
        # 线只在保持拍做次级融合，深度错 1 道时执行位就跟着错 1 道。
        line_pos = (float(lane_x) + self._lane_bias
                    if lane_pos_ok and lane_x is not None
                    and math.isfinite(lane_x)
                    and abs(lane_x) <= _LANE_X_MAX else None)
        if line_pos is not None:
            self._line_recent = True
            self._line_gap_t = 0.0
        else:
            self._line_gap_t += dt_s
        line_dropout = self._line_recent and self._line_gap_t <= _LINE_REBUILD_S
        if line_pos is not None:
            self._lane_trusted = True
            if was_unverified:
                # 盲窗由线收尾：直接重基到线的绝对横位（线已连续锁定 ≥5 拍）
                self.last_rebase = "line_reacquire"
                self.state.executed_lane = line_pos
            else:
                self.state.executed_lane += _LINE_POS_GAIN * (
                    line_pos - self.state.executed_lane)
            if self._road_anchor is None:
                self._road_anchor = 0.0
            self._stale_t = 0.0
            self._reset_jump()
        elif line_dropout:
            # 线短断（≤_LINE_REBUILD_S）：模型外推，不让深度当拍接管；
            # 陈旧计时照常推进，超时后落到下面的深度重建分支。
            self._stale_t += dt_s
        elif road_offset is not None and not road_offset_hold \
                and not self.state_unverified:
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
                        self.last_rebase = "form_align"
                        self.state.executed_lane = road_offset
                else:
                    # 形成门饿死也要重基（13:29 局开局：车在左缘 ro=−2.9，形成门
                    # 拒定锚 7s、hold:0=杆恒 0 蹭墙）——超 stale 预算说明"中带观测"
                    # 等不来，直接重基到路上（exec:=ro、anchor:=0，道0=路中心）。
                    self._stale_t += dt_s
                    if self._stale_t >= self.p.anchor_stale_s and \
                            not self._line_disagrees(road_offset, lane_ok):
                        self.last_rebase = "form_starve"
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
                        self.last_rebase = "jump_confirm"
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
                            self.last_rebase = "stale_reject"
                            self._stale_rebase(road_offset, lane_ok)
        elif self._road_anchor is not None and not self.state_unverified:
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
                    self.last_rebase = "hold_stale"
                    self._stale_rebase(road_offset, lane_ok,
                                       allow_bias=False)
                elif road_offset is None:
                    self._road_anchor = None
                    self._last_corr_sign = None
                    # 计时不清零：断供已计入陈旧预算，下一条新鲜缘读数
                    # 应直接重基，而不是再等一个完整 anchor_stale_s。
        elif self.state_unverified:
            # 盲窗：位置冻结（不融合、不积分），但参考帧卫生照常——断供
            # 超预算的旧锚必须作废，不能留到重获之后去拽新路上的车。
            self._stale_t += dt_s
            if self._stale_t >= self.p.anchor_stale_s:
                self._road_anchor = None
                self._last_corr_sign = None

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
                  + self.state.v_lat * self.p.lookahead_tau_s)
        return self._law(target - x_pred, gentle, deadband, cap)

    def _law(self, e: float, gentle: bool, deadband: float | None = None,
             cap: float | None = None) -> float:
        """位置误差入环：没有线角证据时按 ``ψ̂=0、无角速度`` 的比例段。

        退化不是第二套模型：它仍是同一式子对同一目标的解，只是把未知的
        车头角按 0 处理——线断供的拍里至少还按位置误差修，且不会假装知道
        车头朝哪。"""
        return law(e, self.state.heading if not self._heading_stale else 0.0,
                   self.state.yaw_rate if not self._heading_stale else 0.0,
                   self.p, gentle, deadband, cap, self.state.v_mps)

    def _law_psi(self, psi_des: float, cap: float | None = None) -> float:
        """目标朝向直接入环（计划/决策给出的是车头角时）。

        没有朝向证据时按 ``ψ̂=0、无角速度`` 解同一条式子。"""
        if self._heading_stale:
            return stick_of(psi_des, 0.0, 0.0, self.p, False, cap)
        return stick_of(psi_des, self.state.heading, self.state.yaw_rate,
                        self.p, False, cap)
