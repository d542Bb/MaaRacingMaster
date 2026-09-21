# -*- coding: utf-8 -*-
"""阶段 B 世界模型层：感知框流 → 候选横向目标列表。

**职责边界**（control-route §一 分层表「世界模型」行）：只做两件事——
框**按 y 排距离**（设计决定 6：相机固定时 y 与距离单调对应，不做单目深度）、
框**按 x 排横向**（A1 透视归一尺子，CODE_WIKI §6 定档口径）。
收益/风险/迟滞属决策层，金币组聚合属 §7#3 `[需实测]` 结掉之后——本层不做、不预置。

**归一口径**（与 trick ②c 预注册判据同族，证据 commit `3847fae`）：

    x_lane = ( (cx − vpx)/(cy − y_h) − (ego_cx − vpx)/(v_ego − y_h) ) × a_x

即"目标与本车的横向差，以消失点为原点、按行归一、换算成车道单位"。
未经本式归一的像素横向差**禁止**当距离用（§6 锁的纪律，trick 首轮撤回的直接教训）。

**冻结常数**（运行期口径，`[实测·待复核]`）：vpx/y_h 取 38 场 Gate 0 在场行中位的
总中位（640.7 / 324.2；新录 8 场离散小：vpx 637–648、y_h 323–328）；a_x=0.587 为
A1 定档 globalL 冻结口径（CODE_WIKI §6）；v_ego=716 为 ②c 主档（自车接地点行，
追车相机把自车钉在 cx=640 系 trick 硬事实）。若决策层联调发现系统性偏差，
升级路径是逐场在线校正，不是改判据。

**适用域**（随输出如实携带，消费方必须处理）：
- 远处框（分母 (cy − y_h) < MIN_DENOM）：`x_lane=None`——归一发散不可信，
  距离序仍有效（回放实测：不设此闸远处车框读出 +3 车道，超出路面半宽）；
- 弯道帧：直道口径失效（§6），归一量有系统偏差——本层不判弯道（判据属
  校验层，设计决定 10），决策层接保守态。
"""

from __future__ import annotations

from dataclasses import dataclass

from maaracing_master.plugins.speedrush.perception import PerceptionResult

# 追车相机自车列（trick 硬事实：自车钉画面中央）
EGO_CX = 640.0
# 归一适用域：分母 (cy − y_h) 的最小值（回放定档：目标行集中在 cy 327–366，
# 金币组全在远处——②c 的 by>380 是"贴近时刻"判据，不作全局闸）。分母 <20 时
# vpx 误差（场间 ±8px、场内 MAD 12–19）放大到 ±0.5 车道以上，横向量不可信：
# 置 None，目标仍参与距离序。**误差量级注记**：分母 20–50 区间 x_lane 误差约
# ±0.2–0.5 车道——只够"车道级"决策（选哪条道），连续精控须等目标进入近处
# （贴近判据天然发生在近处，②c 同域）。
MIN_DENOM = 20.0


@dataclass(frozen=True)
class Calib:
    """世界模型标定常数（来源与口径见模块 docstring）。"""

    vpx: float = 640.7     # 消失点 x
    y_h: float = 324.2     # 地平线行
    a_x: float = 0.587     # 横向尺子：归一量 → 车道单位（A1 globalL 冻结）
    v_ego: float = 716.0   # 自车接地点行（②c 主档）


@dataclass(frozen=True)
class WorldTarget:
    """一个候选横向目标：观测量齐备，收益/风险由决策层叠加。"""

    kind: str            # "coin" | "car" | "bonus"
    x_lane: float | None # 归一横向（车道单位，本车=0，右为正）；适用域外=None
    cy: int              # 距离代理：行越大越近（设计决定 6）
    w: int
    h: int
    conf: float


_KINDS = (("coin", "coins"), ("car", "cars"), ("bonus", "bonuses"))


def _x_lane(cx: int, cy: int, cal: Calib) -> float:
    return ((cx - cal.vpx) / (cy - cal.y_h)
            - (EGO_CX - cal.vpx) / (cal.v_ego - cal.y_h)) * cal.a_x


def build_world(per: PerceptionResult, cal: Calib = Calib()) -> list[WorldTarget]:
    """感知结果 → 候选目标列表，按距离**由近及远**排序（cy 降序）。

    两档处理（§6 适用域纪律）：
    - cy ≤ y_h（地平线以上）：几何无效，直接丢弃；
    - y_h < cy 且分母 (cy − y_h) < MIN_DENOM（远处）：目标保留（距离序仍有效），
      但 **x_lane=None**——归一量发散，禁止带病数值参与横向选择（②c by>380 同族）。
    """
    out: list[WorldTarget] = []
    for kind, attr in _KINDS:
        for d in getattr(per, attr):
            denom = d.cy - cal.y_h
            if denom <= 0:
                continue
            x = _x_lane(d.cx, d.cy, cal) if denom >= MIN_DENOM else None
            out.append(WorldTarget(
                kind=kind, x_lane=x, cy=d.cy, w=d.w, h=d.h, conf=d.conf))
    out.sort(key=lambda t: -t.cy)
    return out
