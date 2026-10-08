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

**自车横向运动学**（不变量 C2，设计稿 §二；v2 2026-09-22 真机证据链修正）：
赛车机制下杆是**车头航向指令**不是平移速度指令——起步段 x∝t²（向心加速度），
故 ``v_lat`` 经 ``a_lat_gain`` 积分而来、经 ``tau_align_s`` 自回正、被 ``v_lat_max``
定圆饱和；**不存在**"杆值→稳态横向速度"的静态 K_v（原 v_lat_gain 前提证伪）。
A1 归一下自车恒 0、**目标读数**推不出自车位移，但**路缘两侧读数**可以：
路中心相对自车 = 自车横向位置的反号——``road_offset`` 入参（step 2.5，
V2 复盘"开环虚胖提前松杆"的闭环解）在双侧缘距稳定拍经 alpha-beta 滤波器
钉住 ``executed_lane``；**参考系=道0 路中心**（2026-10-02 维护者裁定中轴
巡航：x_target=0 即守路轴，牙子级边界信号不可依赖、守轴是唯一可守的安全
面），大初差经新息门+陈旧重基消化；无观测拍退回模型开环积分，CHANGE 完成
拍仍经 ``DecisionOutput.reanchor_lane`` 事件重锚（并重置路观测参考帧）。
旋转污染 ε≈−δ·a_x（5.7° 航向仅 0.06 车道，二阶量）暂不修正，vp_x 去偏留后续。

**纯函数纪律**（C3）：update 只吃 (decision, dt_s, current_fid) + 内部状态，
喂同一输入流回放复现同一杆值流。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from maaracing_master.plugins.speedrush.config import Planner
from maaracing_master.plugins.speedrush.tracking import (
    DecisionOutput, DecisionState)

_STICK_FULL = 32767  # XUSB 摇杆半幅（core.capabilities.Gamepad 契约）


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
        self.reset()

    def reset(self) -> None:
        """阶段切换/重入：全清（与 DecisionEngine.reset 同一时机调用）。"""
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

    # ---------- 主入口 ----------

    def update(self, decision: DecisionOutput, dt_s: float,
               current_fid: int,
               road_offset: float | None = None,
               road_offset_new: bool = True) -> GamepadCommand:
        """一个控制 tick。dt_s 非法即 fail-loud（帧号倒退同一纪律：
        静默吞掉病态输入比中断危险——时间基低通会被 0/NaN 污染成永久状态）。

        ``road_offset``（step 2.2.5 闭环）：自车相对路心的位置（车道单位，右正，
        −(左右缘 x_lane 均值)，双侧稳定拍才有值）。有值即对本拍积分结果做
        alpha-beta 修正——治 V2 复盘实锤的"开环虚胖提前松杆"。None=无观测，
        纯模型积分（旧行为逐拍不变）。

        ``road_offset_new``：本拍 road_offset 是否**新证据**（异步驻留协议下，
        保鲜槽复用拍喂的是同一套旧读数）。速度修正（β·r/dt）只在新证据拍生效，
        复用拍只收位置（α·r）——同一新息反复注入曾把单次找边翻跳放大成饱和
        侧滑（2026-10-06 局 id17 取证）。默认 True=旧调用方行为不变。"""
        if not (isinstance(dt_s, (int, float)) and math.isfinite(dt_s)) or dt_s <= 0:
            raise ValueError(f"dt_s 非法（需有限正数）：{dt_s!r}")
        if not math.isfinite(decision.x_target if decision.x_target is not None else 0.0) \
                or (decision.reanchor_lane is not None
                    and not math.isfinite(decision.reanchor_lane)) \
                or (road_offset is not None and not math.isfinite(road_offset)):
            raise ValueError(f"decision 携带非有限值：x_target={decision.x_target!r} "
                             f"reanchor={decision.reanchor_lane!r} road={road_offset!r}")
        # 速度修正时间基累计：本拍模型又跑过 dt_s（帧钟）。修正在 ⑥b（⑥ 之后）
        # 消费，重锚/形成/重基各自清零——见 ⑥b 注
        self._t_vcorr += dt_s

        # ① 重锚先于控制（设计稿 §二：锚点与指令同拍生效，不留旧状态发指令的窗口）
        #    事件重锚同时重置路观测参考（帧变了：executed 被覆写，路中心锚须重新懒定）
        if decision.reanchor_lane is not None:
            self.state.executed_lane = decision.reanchor_lane
            self.state.v_lat_est = 0.0
            self._road_anchor = None
            self._t_vcorr = 0.0               # v 已清零，速度基线重新起算

        # ② 过期判定（valid_until_fid 含边界；第 4 个连续过期拍起按 CONSERVE）
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
                ro_pred = road_offset + self.state.v_lat_est * self.p.lookahead_tau_s
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
        elif st is DecisionState.ABORT_CHANGE:
            # 冻结值归本层：首次观察到 ABORT 的拍写入当前有符号 executed_lane，
            # 之后跟踪它直到离开 ABORT——不消费决策层 x_target（其 ABORT 路径回落 0，
            # 与"回中不总是安全"不一致，维护者裁定 2026-09-22）
            if self._abort_hold_lane is None:
                self._abort_hold_lane = self.state.executed_lane
            steer_raw = self._pd(self._abort_hold_lane, gentle=True)
        elif decision.x_target is not None:
            self._abort_hold_lane = None
            # CHANGE=机动全权；CRUISE 的目标跟踪是维护性定中心——gentle（16:44 局）
            steer_raw = self._pd(decision.x_target,
                                 gentle=st is not DecisionState.CHANGE)
        else:
            steer_raw = self.state.steer_norm    # 防御：非保守态却无目标，保持

        # ⑤ 杆值低通（时间基 α，行为稿 §五纪律：不定值 α）
        alpha = 1.0 - math.exp(-dt_s / self.p.tau_steer_s)
        self.state.steer_norm += alpha * (steer_raw - self.state.steer_norm)

        # ⑥ 横向运动学（v2 双积分，2026-09-22 真机证据链证伪单积分 K_v）：
        #    杆→横向加速度（车头转角带来的向心效应），起步段 x∝t²；松杆后经
        #    tau_align_s 指数自回正（航向回零→横速归零），持续打舵有 v_lat_max 定圆饱和。
        #    k_d 阻尼项本就是为双积分对象写的（单积分无需阻尼），v1 实现与裁定矛盾，此处对齐。
        v_cmd = self.state.v_lat_est + (
            self.p.a_lat_gain * self.state.steer_norm
            - self.state.v_lat_est / self.p.tau_align_s) * dt_s
        self.state.v_lat_est = max(-self.p.v_lat_max, min(self.p.v_lat_max, v_cmd))
        self.state.executed_lane += self.state.v_lat_est * dt_s

        # ⑥b 路中心连续重锚（step 2.5，V2 复盘主修）：双侧缘距拍给 executed 真反馈。
        #    标准 alpha-beta 滤波器——⑥ 的模型积分是 predict 步，本段是 correct 步：
        #    新息 r=观测−预测，位置收 α·r、速度收 β·r/dt。恒位置误差下 r 随 x/v
        #    同步收敛（不是纯 β 直注的发散），模型虚胖被钉回、误差常驻→杆常驻，
        #    "自说自话收敛→提前松杆"从此不可能。无观测拍退回纯模型（旧行为不变）。
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
        if road_offset is not None:
            if self._road_anchor is None:
                if abs(road_offset) <= self.p.anchor_max_off:
                    self._road_anchor = 0.0          # 道0=路中心（中轴巡航）
                    self._stale_t = 0.0
                    self._t_vcorr = 0.0              # 滤波（重）初始化：速度基线起算
                else:
                    # 形成门饿死也要重基（13:29 局开局：车在左缘 ro=−2.9，形成门
                    # 拒定锚 7s、hold:0=杆恒 0 蹭墙）——超 stale 预算说明"中带观测"
                    # 等不来，直接重基到路上（exec:=ro、anchor:=0，道0=路中心）。
                    self._stale_t += dt_s
                    if self._stale_t >= self.p.anchor_stale_s:
                        self.state.executed_lane = road_offset
                        self.state.v_lat_est = 0.0
                        self._road_anchor = 0.0
                        self._stale_t = 0.0
                        self._t_vcorr = 0.0
            else:
                obs = road_offset - self._road_anchor
                r = obs - self.state.executed_lane
                if abs(r) <= self.p.obs_jump_max_lane:
                    self.state.executed_lane += self.p.obs_alpha * r
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
                        self.state.v_lat_est = max(
                            -self.p.v_lat_max, min(self.p.v_lat_max,
                            self.state.v_lat_est + self.p.obs_beta * r / t_e))
                        self._t_vcorr = 0.0
                    self._stale_t = 0.0
                else:
                    self._stale_t += dt_s
                    if self._stale_t >= self.p.anchor_stale_s:
                        # 时间判据重基（13:29 局教训：拍数门 40 次**喂入**拒绝≈2s+，
                        # 供数黑视期还根本不计数——车以 0 杆直开 5.5s 怼进右墙，
                        # ro=+1.7~+2.1 全程在喊）。陈旧即重基到路上：exec:=ro、
                        # anchor:=0——"道0"恢复路中心语义，闭环一拍复活。
                        self.state.executed_lane = road_offset
                        self.state.v_lat_est = 0.0
                        self._road_anchor = 0.0
                        self._stale_t = 0.0
                        self._t_vcorr = 0.0
        elif self._road_anchor is not None:
            # 断供计时：黑视本身=漂移温床，超预算则帧作废（下个中带观测重形成）
            self._stale_t += dt_s
            if self._stale_t >= self.p.anchor_stale_s:
                self._road_anchor = None
                self._stale_t = 0.0

        # ⑦ 下发链：归一 → 限幅（每 tick 变化上限）→ 死区（<256 归 0，256 保留）
        raw = int(round(self.state.steer_norm * _STICK_FULL))
        max_step = int(round(self.p.rate_limit_raw))
        raw = max(self._last_raw - max_step, min(self._last_raw + max_step, raw))
        if abs(raw) < self.p.stick_deadzone_raw:
            raw = 0
        self._last_raw = raw
        return GamepadCommand(steer_x=raw, throttle=self.p.throttle_raw)

    def _pd(self, target: float, gentle: bool = False) -> float:
        """延迟补偿前瞻 PD（设计稿 §三）：状态外推到 t+τ 再入环，阻尼项反向。

        gentle=维护性转向（定中心/保持车道，非变道机动）：误差过死区才发力、
        杆幅限 hold_stick_max——16:44 局：回中心误差动辄 ±1.5 车道，全权 PD
        满左满右交替 2-3s，33% 拍饱和、速度起不来（大杆=横向漂移，游戏掉速）。
        变道（CHANGE）保留全权：机动要快是设计本意。"""
        x_pred = (self.state.executed_lane
                  + self.state.v_lat_est * self.p.lookahead_tau_s)
        return self._law(target - x_pred, gentle)

    def _law(self, e: float, gentle: bool) -> float:
        if gentle and abs(e) < self.p.hold_deadband_lane:
            e = 0.0
        u = self.p.k_p * e - self.p.k_d * self.state.v_lat_est
        if gentle:
            u = max(-self.p.hold_stick_max, min(self.p.hold_stick_max, u))
        return max(-1.0, min(1.0, u))
