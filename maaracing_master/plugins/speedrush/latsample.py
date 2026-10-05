# -*- coding: utf-8 -*-
"""横向轨迹采样评分器（阶段二 §二，设计稿
``docs/plan/speedrush-stage2-trajectory-design.md`` 为唯一口径）。

**决策输出形式的换血**（survey §B 四病根同源处）：从「x_target 位置常量」
改为**从当前状态出发、对外推负责、可滚动重算的时间参数化横向轨迹**——
每拍重采样重评分（Werling 2010 §IV quintic 终点采样），选中轨迹交执行层
跟踪；破判（候选全灭）即按 FSM 语义 ABORT。

**d 坐标系=路心系**（道0=路中心，中轴巡航定案 2026-10-02）：d0=road_offset
（自车相对路心）、边界=±W/2（W 未成形不钳制）、目标/街车入参一律路心系
（调用侧负责 A1→路心 +ro 换算——本模块纯函数，不读观测）。纵向恒速假设
（油门恒踩，survey §B 依据），不做纵向采样。

**碰撞过滤=静态 veto**（第六/七轮定案，与 LateralSafety 同式）：街车按当前
车位参与判距、危险窗按逐车到站时间截断（rel≤0 全视野）、**机动使距离恶化
才拦**（min_gap < 接触界且 < 当前距——保持/远离不自否，切入/贴掠才拦）。
跟踪层无 σ 源，设计稿的 σ 门如实退化为静态+截断（CV 外推已证伪：读数噪声
统计上不可分辨真漂移，外推=幻影碰撞）。轨迹 T 后驻停 d1 也参与判距（不因
轨迹结束豁免）。

**初态续接**：d(0)=d0、d'(0)=vd0、d''(0)=ad0（上一拍解的终端状态）——
逐拍重采样不重起变道，无跳变。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from maaracing_master.plugins.speedrush.depth_geo import (
    GRID_BLOCKED, GRID_CELL, GRID_NX, GRID_UNKNOWN, GRID_X_MAX, GRID_Z_LO,
    DrivableGrid)

# 终点网格（道，路心系）与时长网格（s）——设计稿 §2.1 起值，回放校准
D_GRID = (0.0, -1.0, 1.0, -2.0, 2.0, -3.0, 3.0)
T_GRID = (0.8, 1.2, 1.8, 2.5)
COLL_DT_S = 0.2       # 碰撞判距的轨迹采样步
W_MARGIN_LANE = 0.15  # 终点钳制的边界内缩（道）

# 代价权重起值（设计稿 §2.2，回放校准）：jerk 积分量级实测 1 道/1.2s≈274、
# 2 道/2.5s≈30（∫j²∝Δ²/T⁶，天然偏好慢变）。k_jerk 按参考机动归一到 O(1)；
# **k_offset 必须压过 jerk+k_t 的机动成本**——收益侧（吃币/超车）以「终点贴
# 目标」进入，权重失衡时采样器永远躺平不追（2026-10-03 接线测试实证：
# k_offset=2 时 0.2→0 的"逃向轴"以 7.25 胜"到目标"的 8.74）。k_time 与
# jerk 抗衡防全选最慢档。
K_JERK = 0.01
K_TIME = 2.0
K_OFFSET = 20.0

# 栅格裁决（S2-B，三检验 2026-10-05 全绿后接线）：裁决域=近场带 z∈[3,9]
# （检验 A/B/C 的证据口径域；产线无车速读数，不做 z=速度×t 的造源映射）；
# 横向=候选轨迹的采样包络 ± (车半宽+内收)——按包络而非逐时刻，偏保守方向
# 与碰撞裁决一致。blocked 一票否决（检验 B：近场无成片误侵入，可作硬约束）；
# unknown 每格小代价（检验 A：黑视 unknown≈42%，是供数常态不是异常）。
GRID_VETO_MARGIN_LANE = 0.5   # 内收下限（道）＝检验 C 的边界抖动量化
GRID_CAR_HALF_LANE_M = 0.9    # 车半宽（米）——
GRID_K_UNKNOWN = 0.05         # 每格 unknown 代价（与 k_jerk×274≈2.7 同量级标定）


NEAR_IZ = int(6.0 / GRID_CELL)   # 近场裁决带行数（z 3~9m，三检验证据域）


def _near_hole(dig: np.ndarray, y: int, x: int) -> bool:
    """格 (y,x) 的 8 邻域（含自身）是否存在挖洞格（dig 布尔掩码，True=挖洞）。"""
    h, w = dig.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            yy, xx = y + dy, x + dx
            if 0 <= yy < h and 0 <= xx < w and dig[yy, xx]:
                return True
    return False


def _effective_blocked(grid: DrivableGrid) -> np.ndarray:
    """近场带有效 blocked 掩码：4 连通分片后，与**挖洞格**（ego/object 掩码
    挖除，dig_mask；旧构造无掩码回退 counts==0 近似）8 邻域相邻的片**整片**
    除名——洞边平面中位失真不是墙。只清真挖洞邻域，不清视锥外无点区（真墙
    自然延伸进视锥边缘，按 counts==0 清会把墙也清掉——首跑回放实证）。
    每拍一次，全候选共享。"""
    blk = (grid.state[:NEAR_IZ] == GRID_BLOCKED).copy()
    dig = grid.dig_mask[:NEAR_IZ] if grid.dig_mask is not None \
        else grid.counts[:NEAR_IZ] == 0
    h, w = blk.shape
    seen = np.zeros_like(blk, bool)
    for i in range(h):
        for j in range(w):
            if not blk[i, j] or seen[i, j]:
                continue
            stack, cells = [(i, j)], []
            seen[i, j] = True
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < h and 0 <= xx < w and blk[yy, xx] \
                                and not seen[yy, xx]:
                            seen[yy, xx] = True
                            stack.append((yy, xx))
            if any(_near_hole(dig, cy, cx) for cy, cx in cells):
                for cy, cx in cells:
                    blk[cy, cx] = False
    return blk


def quintic(d0: float, vd0: float, ad0: float, d1: float, T: float
            ) -> tuple[float, float, float, float, float, float]:
    """五次多项式系数（升幂 a0..a5）：d(0)=d0, d'(0)=vd0, d''(0)=ad0,
    d(T)=d1, d'(T)=d''(T)=0。T>0 由调用方保证（可达域过滤先行）。"""
    a0, a1, a2 = d0, vd0, ad0 / 2.0
    T2, T3, T4, T5 = T*T, T**3, T**4, T**5
    D = d1 - (d0 + vd0*T + 0.5*ad0*T2)          # 终端位置残差
    B = -(vd0 + ad0*T)                           # 终端速度残差
    C = -ad0                                     # 终端加速度残差
    a5 = (C - 6.0*B/T + 12.0*D/T2) / (2.0*T3)
    a4 = 7.0*B/T3 - C/T2 - 15.0*D/T4
    a3 = D/T3 - a4*T - a5*T2
    return (a0, a1, a2, a3, a4, a5)


def jerk_integral(c: tuple[float, ...], T: float) -> float:
    """∫₀ᵀ d⃛² dt 闭式（jerk = 6a3 + 24a4 t + 60a5 t²，平方展开逐项积分）：
    36a3²T + 144a3a4T² + 192a4²T³ + 240a3a5T³ + 720a4a5T⁴ + 720a5²T⁵。"""
    a3, a4, a5 = c[3], c[4], c[5]
    T2, T3, T4, T5 = T*T, T**3, T**4, T**5
    return (36.0*a3*a3*T + 144.0*a3*a4*T2 + 192.0*a4*a4*T3
            + 240.0*a3*a5*T3 + 720.0*a4*a5*T4 + 720.0*a5*a5*T5)


def eval_traj(c: tuple[float, ...], t: float) -> float:
    """轨迹在 t 时刻的 d 值（升幂系数）。"""
    return c[0] + t*(c[1] + t*(c[2] + t*(c[3] + t*(c[4] + t*c[5]))))


@dataclass(frozen=True)
class SampledTraj:
    """一条中选轨迹（执行层按 coeffs 跟踪，dt 用 elapsed 内插）。"""

    d1: float                        # 终点（路心系，道）
    T: float                         # 时长（s）
    cost: float
    coeffs: tuple[float, ...]


@dataclass(frozen=True)
class SampleReport:
    ok: bool
    best: SampledTraj | None
    why: str = ""                    # 全灭原因（调试面；中选时为空）
    n_eval: int = 0                  # 进入评分的候选数
    n_reject_collision: int = 0
    n_reject_reach: int = 0
    n_reject_bound: int = 0
    n_reject_grid: int = 0           # 栅格 blocked 一票否决数（S2-B）


class LatTrajectorySampler:
    """每拍重采样重评分。纯函数体（状态仅初态由调用方喂入），单线程使用。"""

    def __init__(self, v_lat_max: float, v_ego_row: float, horizon_s: float,
                 gap_lane: float, *,
                 d_grid: tuple[float, ...] = D_GRID,
                 t_grid: tuple[float, ...] = T_GRID,
                 k_jerk: float = K_JERK, k_time: float = K_TIME,
                 k_offset: float = K_OFFSET,
                 coll_dt_s: float = COLL_DT_S,
                 w_margin_lane: float = W_MARGIN_LANE,
                 grid_margin_lane: float = GRID_VETO_MARGIN_LANE,
                 grid_car_half_m: float = GRID_CAR_HALF_LANE_M,
                 k_grid_unknown: float = GRID_K_UNKNOWN) -> None:
        self.v_lat_max = v_lat_max
        self.v_ego_row = v_ego_row
        self.horizon_s = horizon_s
        self.gap_lane = gap_lane
        self.d_grid = d_grid
        self.t_grid = t_grid
        self.k_jerk = k_jerk
        self.k_time = k_time
        self.k_offset = k_offset
        self.coll_dt_s = coll_dt_s
        self.w_margin = w_margin_lane
        self.grid_margin_lane = grid_margin_lane
        self.grid_car_half_m = grid_car_half_m
        self.k_grid_unknown = k_grid_unknown

    def update(self, d0: float, vd0: float, ad0: float, d_target: float,
               cars, width: float | None, *,
               ro: float = 0.0, hz: float = 20.0,
               grid: DrivableGrid | None = None,
               lane_w_m: float | None = None) -> SampleReport:
        """一拍采样评分。

        ``cars``：CarView 鸭子（x_lane= A1 系读数，本方法按 ro 换算到路心系；
        cy=行号、rel_approach=px/tick）。``width``：路宽 W（道，碰撞边界到
        碰撞边界）；None=未成形不钳制。返回中选轨迹或诚实弃权。
        ``grid``：同拍可行驶栅格（None=不触栅格代价，闸关路径）；``lane_w_m``
        车道米宽（道↔米换算锚，栅格裁决启用时必传）。"""
        # 终点集：网格 ∪ 精确目标（收益侧「贴目标」的直接实现——纯网格的
        # 1 道分辨率会系统性丢币，设计稿网格为起值，此处扩展，回放校准）
        ends = list(self.d_grid)
        if all(abs(d_target - e) > 1e-9 for e in ends):
            ends.append(d_target)
        lo = hi = None
        if width is not None:
            lo, hi = -width/2.0 + self.w_margin, width/2.0 - self.w_margin

        best: SampledTraj | None = None
        best_cost = math.inf
        n_eval = n_col = n_reach = n_bound = n_grid = 0
        grid_blk = None if grid is None else _effective_blocked(grid)
        for d1 in ends:
            if lo is not None and not (lo <= d1 <= hi):
                n_bound += 1
                continue                       # 终点出界：钳制的淘汰形态
            for T in self.t_grid:
                if abs(d1 - d0) > self.v_lat_max * T:
                    n_reach += 1
                    continue                   # 超横向可达域
                c = quintic(d0, vd0, ad0, d1, T)
                if self._vetoed(c, T, d1, d0, cars, ro, hz):
                    n_col += 1
                    continue
                cost = (self.k_jerk * jerk_integral(c, T) + self.k_time * T
                        + self.k_offset * (d1 - d_target)**2)
                if grid is not None:
                    vetoed_g, unknown_n = self._grid_penalty(
                        c, T, d1, grid_blk, grid, ro, lane_w_m)
                    if vetoed_g:
                        n_grid += 1
                        continue               # 近场压墙：一票否决
                    cost += self.k_grid_unknown * unknown_n
                n_eval += 1
                if cost < best_cost:
                    best_cost = cost
                    best = SampledTraj(d1=d1, T=T, cost=cost, coeffs=c)
        if best is None:
            why = ("all_collision" if n_col and not n_bound and not n_reach
                   else "all_out_of_bounds" if n_bound and not n_col
                   else "all_unreachable" if n_reach and not n_col
                   else "no_candidate")
            return SampleReport(False, None, why=why, n_eval=n_eval,
                                n_reject_collision=n_col,
                                n_reject_reach=n_reach,
                                n_reject_bound=n_bound,
                                n_reject_grid=n_grid)
        return SampleReport(True, best, n_eval=n_eval,
                            n_reject_collision=n_col, n_reject_reach=n_reach,
                            n_reject_bound=n_bound, n_reject_grid=n_grid)

    def _grid_penalty(self, c: tuple[float, ...], T: float, d1: float,
                      blk: np.ndarray, grid: DrivableGrid, ro: float,
                      lane_w_m: float | None) -> tuple[bool, int]:
        """候选轨迹 × 栅格 → (是否压 blocked, unknown 格数)。

        ``blk``=近场带有效 blocked 掩码（洞边污染片已整片除名，见
        _effective_blocked）。裁决域=近场带 z∈[GRID_Z_LO, +6m]（三检验证据
        域）。横向取轨迹采样包络（含终点驻停 d1）± (车半宽+内收)，按 ro 换
        到相机系米（栅格 x 原点=相机光轴=自车）。lane_w_m 缺失=无法换算，
        弃权不判（不否决不加代价——证据换算不了就不装懂）。"""
        if lane_w_m is None or lane_w_m <= 0 or grid.coef is None:
            return False, 0
        n = max(1, int(math.ceil(T / self.coll_dt_s)))
        ds = [eval_traj(c, min(k * self.coll_dt_s, T)) for k in range(n + 1)]
        ds.append(d1)
        half = (self.grid_car_half_m / lane_w_m) + self.grid_margin_lane
        x_lo = (min(ds) - ro) * lane_w_m - half * lane_w_m
        x_hi = (max(ds) - ro) * lane_w_m + half * lane_w_m
        ix_lo = max(0, int(math.ceil((x_lo + GRID_X_MAX) / GRID_CELL)))
        ix_hi = min(GRID_NX, int(math.ceil((x_hi + GRID_X_MAX) / GRID_CELL)))
        if ix_lo >= ix_hi:
            return False, 0
        if blk[:, ix_lo:ix_hi].any():
            return True, 0
        sub = grid.state[:blk.shape[0], ix_lo:ix_hi]
        return False, int((sub == GRID_UNKNOWN).sum())

    def _t_pass_s(self, car, hz: float) -> float:
        """该车到自车行的秒数（与 LateralSafety 同一口径）；不接近→永不到站。"""
        rate = car.rel_approach * hz
        return max(0.0, self.v_ego_row - car.cy) / rate if rate > 1e-9 \
            else math.inf

    def _vetoed(self, c: tuple[float, ...], T: float, d1: float, d0: float,
                cars, ro: float, hz: float) -> bool:
        """拦截语义=**机动使距离恶化才拦**（第七轮裁定，与 LateralSafety
        同式）：候选轨迹逐 COLL_DT_S 采样（T 后驻停 d1）对每辆车在危险窗内
        取最小距；min_gap < gap_lane 且 **min_gap < 当前距** 才拦——保持/远离
        不自否，切入/贴掠才拦。rel≤0（不接近）按全视野算（与生产同）。"""
        for car in cars:
            if car.x_lane is None or car.x_lane != car.x_lane:
                continue                       # nan（存续组）同跳过
            t_eff = min(self.horizon_s, self._t_pass_s(car, hz))
            if t_eff <= 0.0:
                continue                       # 已过自车行：pass 制语义接管
            d_road = ro + car.x_lane           # A1→路心系
            cur = abs(d0 - d_road)
            n = max(1, int(math.ceil(t_eff / self.coll_dt_s)))
            for k in range(n + 1):
                t = min(k * self.coll_dt_s, t_eff)
                d = eval_traj(c, t) if t <= T else d1
                gap = abs(d - d_road)
                if gap < self.gap_lane and gap < cur:
                    return True
        return False
