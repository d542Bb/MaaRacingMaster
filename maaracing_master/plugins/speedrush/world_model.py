# -*- coding: utf-8 -*-
"""阶段 B 世界模型层：感知框流 → 候选横向目标列表。

**职责边界**（control-route §一 分层表「世界模型」行）：只做两件事——
框**按 y 排距离**（设计决定 6：相机固定时 y 与距离单调对应，不做单目深度）、
框**按 x 排横向**（A1 透视归一尺子，CODE_WIKI §6 定档口径）。
收益/风险/迟滞属决策层，金币组聚合属 §7#3 `[需实测]` 结掉之后——本层不做、不预置。

**归一口径**（保险杠机位；标定样本见 gate0 `_source`/`_notes`）：

    x_lane = (cx − vpx)/(cy − y_h) × a_x

即"目标相对道路消失点方向、按行归一、换算成车道单位"。相机装在车头且钉在
自车中心，自车横向原点就是消失点列，式内不需要自车行项。未经本式归一的
像素横向差**禁止**当距离用（§6 锁的纪律，trick 首轮撤回的直接教训）。

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
import math
from dataclasses import dataclass
from functools import lru_cache

from maaracing_master.plugins.speedrush import GATE0_FILE


@dataclass(frozen=True)
class Calib:
    """几何标定常数（真源 = resources/calibration/gate0.json，三源分立裁定：
    游戏事实 RULES / 几何标定 gate0 / 行为参数 decision——本类只是文件载体的形状，
    禁止在此写字面量默认值：写了就是第二份真相）。

    字段口径见 gate0.json 的 _notes；来源与误差注记见本模块 docstring 与 CODE_WIKI。
    """

    vpx: float        # 消失点 x
    y_h: float        # 地平线行
    focal_px: float   # 线角几何的全幅相机焦距（≠ MoGe 逐帧 fx，见 _notes）
    a_x: float        # 横向尺子：归一量 → 车道单位（保险杠机位重标）
    min_denom: float  # 归一适用域下限：分母 (cy − y_h) 低于此 → x_lane=None
    lane_w_m: float   # 一条车道的米制宽（3D 找边的车道量换算锚；出处见 _notes）
    depth_scale: float  # MoGe 米制点云全局缩放（metric_scale × 此值）


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
        vpx=_require(d, "vpx"), y_h=_require(d, "y_h"),
        focal_px=_require(d, "focal_px"), a_x=_require(d, "a_x"),
        min_denom=_require(d, "min_denom"), lane_w_m=_require(d, "lane_w_m"),
        depth_scale=_require(d, "depth_scale"))
    # 依赖矛盾检查：尺子分母与换算锚都不能退化
    if not (cal.a_x > 0 and cal.min_denom > 0 and cal.vpx > 0
            and cal.lane_w_m > 0 and cal.depth_scale > 0):
        raise ValueError(f"gate0.json 数值非法：{cal}")
    return cal


@lru_cache(maxsize=1)
def load_calib() -> Calib:
    return _read_gate0(GATE0_FILE)


# 类别名唯一真源在此（感知层 Detection 类别 ↔ 域内 kind 字符串），
# tracking/plan/coin_group 一律从这里取，不再各自写字面量。
KIND_COIN = "coin"
KIND_CAR = "car"
KIND_BONUS = "bonus"

# 捕获几何（WGC 固定 1280×720）。行率到站仪器的终点改用**画面下缘**而不是
# 追车机位的自车接地点行：保险杠相机在车头，自车不在画面里，自车行不是可
# 观测几何；下缘是相机无关的边界（该行地面约在相机前 3m，像素粗仪器的固有
# 偏置，只作无深度锚目标的粗回退）。
IMAGE_H = 720


def x_lane_of(cx: int, cy: int, cal: Calib) -> float:
    """A1 归一尺子（保险杠机位，无自车项；公式见本模块 docstring）。公开供
    tracking 层复用——同一公式只许有一份，禁止复制第二份（禁止建立第二份真相）。"""
    return (cx - cal.vpx) / (cy - cal.y_h) * cal.a_x


def t_meet_car_s(rel_px_per_fid: float, cy: float, cal: Calib,
                 fid_rate_hz: float) -> float:
    """车流到画面下缘的秒数（地面流积分式，2026-10-11 V0 归因后 cars 换基；
    2026-10-23 保险杠机位终点从自车行改为画面下缘——自车不在画面里）。

    线性行率式 (D−cy)/(rel·hz) 有两处契约错误：① rel 单位是 px/fid
    不是 px/tick（fid 率 ≠ 控制拍率，消费偏差恒定高估 TTC）；② 行速率随
    行号二次增长（针孔投影下恒定相对纵向速度的必然形状 r ∝ (cy−y_h)²），
    线性式把剩余行程当匀速，远车到站时间虚高一个量级。本式对 1/r 沿行
    积分：k = rel/(cy−y_h)²（**per-car**——rel 本就是相对接近率，他车非
    静止的修正天然在内，他车加减速经 EMA 在数拍内进入 k），

        t = ∫ dy/(k(y−y_h)²) = (1/d − 1/D)/k = d(D−d)/(D·rel·f_fid)

    其中 d=cy−y_h、D=IMAGE_H−y_h、f_fid=采集帧率（px/fid→秒的换算锚，
    由 Tracker 逐拍在线估计随 obs 下发）。不接近（rel≤0）或已到下缘
    （cy≥IMAGE_H 积分为负）→ 0/inf 语义与旧式一致；fid_rate_hz 必须>0
    （供数缺位时调用方自决退路，本函数不代作主张）。"""
    if fid_rate_hz <= 0:
        raise ValueError(f"fid_rate_hz={fid_rate_hz} 非正（px/fid→秒无换算锚）")
    d = cy - cal.y_h
    if rel_px_per_fid <= 0 or d <= 0:
        return math.inf if rel_px_per_fid <= 0 else 0.0
    D = IMAGE_H - cal.y_h
    t_fid = d * (D - d) / (D * rel_px_per_fid)
    return max(0.0, t_fid) / fid_rate_hz

