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

**几何真源**：常数全部住 `resources/calibration/gate0.json`（三源分立：游戏事实
RULES / 几何标定 gate0 / 行为参数 decision），经 `load_calib()` fail-loud 读取，
本模块不写字面量默认值——写了就是第二份真相。口径与升级路径注记随文件 `_notes` 走。

**适用域**（随输出如实携带，消费方必须处理）：
- 远处框（分母 (cy − y_h) < cal.min_denom）：`x_lane=None`——归一发散不可信，
  距离序仍有效（回放实测：不设此闸远处车框读出 +3 车道，超出路面半宽）；
- 弯道帧：直道口径失效（§6），归一量有系统偏差——本层不判弯道（判据属
  校验层，设计决定 10），决策层接保守态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from maaracing_master.plugins.speedrush import GATE0_FILE
from maaracing_master.plugins.speedrush.perception import PerceptionResult


@dataclass(frozen=True)
class Calib:
    """几何标定常数（真源 = resources/calibration/gate0.json，三源分立裁定：
    游戏事实 RULES / 几何标定 gate0 / 行为参数 decision——本类只是文件载体的形状，
    禁止在此写字面量默认值：写了就是第二份真相）。

    字段口径见 gate0.json 的 _notes；来源与误差注记见本模块 docstring 与 CODE_WIKI。
    """

    vpx: float        # 消失点 x
    y_h: float        # 地平线行
    a_x: float        # 横向尺子：归一量 → 车道单位（A1 globalL 冻结）
    v_ego: float      # 自车接地点行（②c 主档）
    ego_cx: float     # 追车相机自车列（trick 硬事实：自车钉画面中央）
    min_denom: float  # 归一适用域下限：分母 (cy − y_h) 低于此 → x_lane=None


def _require(d: dict, key: str) -> float:
    v = d.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"gate0.json 键「{key}」缺失或非数值：{v!r}")
    return float(v)


def _read_gate0(path) -> Calib:
    """gate0.json 的实际解析（fail-loud：缺键/非数值/依赖矛盾即拒载并写明原因）。
    与 load_calib 拆开只为测试可注入临时文件；进程读法只有带缓存的那一个。"""
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("schema_version") != 1:
        raise ValueError(f"gate0.json schema_version 非 1：{d.get('schema_version')!r}")
    cal = Calib(
        vpx=_require(d, "vpx"), y_h=_require(d, "y_h"), a_x=_require(d, "a_x"),
        v_ego=_require(d, "v_ego"), ego_cx=_require(d, "ego_cx"),
        min_denom=_require(d, "min_denom"))
    # 依赖矛盾检查：尺子分母与自车项都不能退化
    if not (cal.a_x > 0 and cal.min_denom > 0 and cal.vpx > 0 and cal.ego_cx > 0):
        raise ValueError(f"gate0.json 数值非法：{cal}")
    if cal.v_ego - cal.y_h <= 0:
        raise ValueError(f"自车行 v_ego={cal.v_ego} 必须低于地平线 y_h={cal.y_h}（ego 项分母退化）")
    return cal


@lru_cache(maxsize=1)
def load_calib() -> Calib:
    return _read_gate0(GATE0_FILE)


@dataclass(frozen=True)
class WorldTarget:
    """一个候选横向目标：观测量齐备，收益/风险由决策层叠加。"""

    kind: str            # "coin" | "car" | "bonus"
    x_lane: float | None # 归一横向（车道单位，本车=0，右为正）；适用域外=None
    cy: int              # 距离代理：行越大越近（设计决定 6）
    w: int
    h: int
    conf: float


# 类别名唯一真源在此（感知层 PerceptionResult 字段 ↔ 域内 kind 字符串），
# tracking.py 与测试一律从这里取，不再各自写字面量。
KIND_COIN = "coin"
KIND_CAR = "car"
KIND_BONUS = "bonus"

_KINDS = ((KIND_COIN, "coins"), (KIND_CAR, "cars"), (KIND_BONUS, "bonuses"))


def x_lane_of(cx: int, cy: int, cal: Calib) -> float:
    """A1 归一尺子（本模块 docstring 的公式）。公开供 tracking 层复用——
    同一公式只许有一份，禁止复制第二份（禁止建立第二份真相）。"""
    return ((cx - cal.vpx) / (cy - cal.y_h)
            - (cal.ego_cx - cal.vpx) / (cal.v_ego - cal.y_h)) * cal.a_x


def build_world(per: PerceptionResult, cal: Calib | None = None) -> list[WorldTarget]:
    """感知结果 → 候选目标列表，按距离**由近及远**排序（cy 降序）。
    cal 缺省读几何真源（gate0.json）。

    两档处理（§6 适用域纪律）：
    - cy ≤ y_h（地平线以上）：几何无效，直接丢弃；
    - y_h < cy 且分母 (cy − y_h) < min_denom（远处）：目标保留（距离序仍有效），
      但 **x_lane=None**——归一量发散，禁止带病数值参与横向选择（②c by>380 同族）。
    """
    if cal is None:
        cal = load_calib()
    out: list[WorldTarget] = []
    for kind, attr in _KINDS:
        for d in getattr(per, attr):
            denom = d.cy - cal.y_h
            if denom <= 0:
                continue
            x = x_lane_of(d.cx, d.cy, cal) if denom >= cal.min_denom else None
            out.append(WorldTarget(
                kind=kind, x_lane=x, cy=d.cy, w=d.w, h=d.h, conf=d.conf))
    out.sort(key=lambda t: -t.cy)
    return out
