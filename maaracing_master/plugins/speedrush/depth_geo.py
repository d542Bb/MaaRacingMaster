# -*- coding: utf-8 -*-
"""深度几何观测层 v4（2026-10-01 几何真源统一：MoGe 逐帧自估焦距点云当米制主人）。

**职责**：帧 → MoGe-2 q4f16 点云（逐帧 fx/fy，官方 recover_focal_shift 链路，
后处理见 moge_post）→ 倾角感知平面拟合分地/障 → 高出路面 hgt → Z 分箱横向
中位剖面离地穿越找边 → 两侧缘读数（车道单位）。输出与 BoundarySummary 的
``left_edge_lane / right_edge_lane`` 字段鸭子同构，消费方原样可用。

**为什么是这套读法**（金标直路帧全 ≤1m，探针实证 2026-10-01；旧 DA 视差链
同日整体退役，用户拍板「只留 3D 链路」）：
- **逐帧自估焦距是仲裁结论**：旧链静态内参（FY 中位）下算出的相机高度跨帧漂移
  2.5→3.9m（物理不可能）；MoGe 逐帧自估焦距稳定（cam_h 1.8~2.2m）——游戏相机
  等效 FOV 逐帧在变，自估焦距吸收它。点云、反投影、回投一律同帧 fx/fy，
  静态内参不再存在；**3D 量的像素往返禁用任何写死焦距**。
- **米制直读**：旧 DA 相对视差缺米制，靠路半宽 W_REF 逐帧自标定；MoGe 输出
  米制点云（物理自洽：cam_h≈2m、路宽≈13.5m≈四车道），整套尺度自标定机制随
  旧链退役。
- **「边」= 可行驶路面消失处**（维护者口径 2026-10-01）：检出判据=离地穿越
  （0.03m@3m 起每米 +5mm，两格持续 + 先见地面 + 尾部持续——缘后是缘体，
  影子 bump/路面渐变穿过后回落），kerb 与护栏两类通用；宽肩帧
  的墙脚与检出差是口径差非误差。**弯道不做特殊优化**：逐 Z 箱独立读数无直线
  假设，天然描出弯道边线。
- **车道量换算**：lane = x_m / cal.lane_w_m（gate0.json 几何标定真源，左负右
  正，与退役像素尺同号约定）。
- **空中结构剔除**：SKY_HGT=1.2m——桥/天空域点 hgt 实测 ≥7.5m，护栏 ≤1m，
  间隙巨大；不剔会把远端 Z 箱中位整体抬高、穿越在桥带内缘误触发。
- **ego 掩码=紧矩形**（ego_mask.json 车身矩形）：旧「列带下延」是 2D 逐行
  扫描语义（「下延挖多无伤」），搬进 3D 分箱会把近处整段可见路面挖走（实证）。

**标定常量出处**：平面拟合阈值=55 帧口径沿用（2026-09-26 定案）；找边阈值
（EDGE_HT/ZBIN 等）=金标帧 MoGe 点云校准（2026-10-01，探针定案后随产线实现
在金标集上回归确认）；lane_w_m=金标 BEV 实测（gate0.json _notes 记出处）。

**降级路径**：权重缺失/推理异常 → observe 返回 None，road_offset 链路退回
纯模型积分；平面拟合失败/无穿越 → 该侧弃权（None，rejects 留因）。

**上拍形态**：生产经 AsyncDepthRoadObserver（异步 worker，协议同 treasure OCR）
使用本层——控制拍只付 push+take，observe 成本在后台线程；本模块的同步
observe 保留为纯函数面（测试与离线复算直接调用）。

**离线复算**：debug 证据包（*_evid.npz）存 336×598 原生点图 fp16 + valid +
归一化焦距（reconstruct 口径 cx=cy=0.5）；复算 = load → 各通道 cv2.resize 到
1280×720（pts 线性/valid 最近邻）→ 焦距 ×(1280, 720) 得全幅像素焦距 →
invalid 置 nan → reading_from_points，与在线逐位一致。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush import moge_post
from maaracing_master.plugins.speedrush.world_model import Calib

# ── 检测带（与既有捕获几何 1280×720 绑定；推理吃全帧，天空/远景是焦距锚）──
Y0, DIAG_Y1 = 340, 715
MOGE_IN_W, MOGE_IN_H = 1280, 720    # 折叠静态输入（free-dim 覆盖，见 load_session）

# ── 平面拟合（路面走廊域，Y = aX + bZ + c）──────────────────────────────
# 拟合域只收「路面点」是定案口径（2026-09-30 点云指正、2026-10-01 金标双平面
# 对照实锤）：拟合域含墙/肩/远景时平面被拽歪，路面残差"随距离增长"的假象；
# 走廊域下路面残差实测 3~9cm 且不随距离增长。宽域+距离缩放容差的旧实现同日
# 退役（对质误差中位 2.5m vs 走廊域 1.1m，000906 双侧 0.0x m）。
PLANE_ITERS = 3       # 走廊迭代轮数
PLANE_TOL = 0.15      # 走廊残差容差（固定；路面本身是平的，不随 Z 放宽）
ROAD_X_MAX = 4.0      # 路面点横向域（米）
ROAD_Z_LO, ROAD_Z_HI = 4.0, 30.0
EGO_ABS_X, EGO_ABS_Z = 1.3, 10.0   # 自车区排除（车是路面上的隆起物）
MIN_PLANE_PTS = 500

# ── 列剖面路面特征（2026-10-02 定案：拟合起步种子由「位置圈地」换成「特征
#    识别路面」）─────────────────────────────────────────────────────────
# 针孔几何（dv/dz = −fy·h/z²，与坡度无关——坡度只平移路面在画面里的位置）：
# 同列深度自底向上平滑递增，逐行增量 ≈ z²/fy；墙=列内近恒深、车=深度骤降、
# 天空=深度跳变。自底向上逐列行走，违反即停。判据定值出自 000660/000100/
# 000260 三帧（2026-10-01 会话），并经金标仓 122 帧回归 + 合成坡道（±10%
# 恢复精确）/墙体干扰验证定案（2026-10-02，证据指针见当次提交）。旧法的病理
# 在起步种子被墙劫持（墙面
# 点把 000660 平面拽歪 5.25°，路自身平面实测 -2.06°/99% 内点），不在走廊重选
# 本身——故特征只做种子，走廊重选动力学原样保留。
WALK_Z_MIN = 0.5        # 深度有效下限（米）
WALK_Z_BACK = 0.85      # 深度允许下界（×前一有效深度）
WALK_Z_FWD = 1.4        # 深度允许上界（×前一有效深度）
WALK_INC_LO = -0.35     # 逐行增量下界（×理论增量）
WALK_INC_HI = 3.0       # 逐行增量上界（×理论增量）
WALK_INC_FLOOR = 0.05   # 理论增量下限（米；z 小时理论增量趋 0，噪声即越界）
WALK_MISS_STOP = 6      # 连续违反 >6 次断列（第 7 次停）
WALK_MIN_COL_PTS = 10   # 单列有效像素下限（少于则整列弃权）
WALK_COL_STRIDE = 4     # 隔列采样（特征逐列独立，跳列不影响别的列；算力档）
WALK_ROW_STRIDE = 2     # 隔行采样（增量理论值同比放大，行走轮数减半；算力档）

# ── 3D 找边（Z 分箱横向剖面离地穿越；金标 MoGe 点云校准 2026-10-01）─────
ZBIN = ((3, 5), (5, 7), (7, 9), (9, 12), (12, 16))  # 深度箱（米）；弯道无直线
#                                                   # 假设，逐箱独立读数
XBIN_DX = 0.25        # 横向格宽（米，路心两侧各 12m）
X_MAX_M = 12.0        # 剖面横向域（米）
# 扫描格心表（−X_MAX 起 DX 步进，共 N_XBIN 个）——分箱向量化后按索引取格心。
# 直接算式与旧 while 的逐次累加**逐位同值**：0.125=2⁻³ 的整数倍，且 |x|<16 时
# ulp≤2⁻²⁰ 整除 2⁻³，故累加/直算都无舍入（这是「格边界判定不变」的前提）。
N_XBIN = int(2 * X_MAX_M / XBIN_DX)
XBIN_CENTERS = (-X_MAX_M + XBIN_DX / 2
                + XBIN_DX * np.arange(N_XBIN, dtype=np.float64))
MIN_BIN_PTS = 15      # 单格剖面最少点数
MIN_SIDE_PTS = 150    # 单箱单侧最少点数
MIN_BOX_PTS = 300     # 单箱（双侧合计）最少点数
EDGE_HT = 0.03        # 离地穿越阈值 @3m（金标校准：0.03~0.05 间最优）
EDGE_HT_SLOPE = 0.005  # 阈值随 Z 放宽速率（米/米，跟平面残差与远场噪声同尺度）
EDGE_TAIL_FRAC = 0.6  # 尾部持续占比（穿越后剖面须仍高于阈值的格数占比，
                      # 2026-10-02 真机剖面 d00003/17 取证换装）：缘后是缘体
                      # （墙脸/kerb 台面=第二级平台，剖面持续高），影子 bump/
                      # 路面渐变穿过后回落路面。**缓坡类假缘（平面残差单调爬
                      # 坡，d00003 L 实测）尾部也持续，本门杀不掉**——其与真
                      # kerb 的剖面形状不可分（000340 右 6.2m 真 kerb 阶差仅
                      # 0.08 与缓坡同形），需离线语料级判据（跨箱稳定性/平台
                      # 度）另立，见 CODE_WIKI 挂账。
# ── 缘体证据门（2026-10-03 语料普查换装，杀缓坡类假缘）──────────────────
# 普查实证（63 帧=三局证据包+3 金标帧，tools/experiments census_crossbin）：
# 真缘剖面在穿越后必有「脸」（骤升：单格增量 ≥0.04，金标 kerb 0.075~0.09、
# 墙 0.3~0.5）或「平台」（到顶：连续 ≥3 格 |增量| ≤0.004 且高度 ≥1.5×阈，
# kerb 台面 0.14~0.21=1.7~4.7×阈）；缓坡假缘两者皆无——逐格增量 ≤0.026
# （d00003 L 陡缓坡 0.085/m 实测）、永不到顶（高度只爬到 1.1~1.5×阈的肩台，
# 000340 L/000906 R 实证），单帧形状判据（阶差门）在此判据下重新可分。
# 跨箱稳定性作过主判据候选被否：陡缓坡跨箱仅漂 0.79m（真缘 ≤0.6 缓坡
# ≥3.0 的分离只对浅缓坡成立），见 CODE_WIKI 第九轮。
EDGE_FACE_MIN = 0.04  # 脸：单格增量下限（米）；脸窗=穿阈上升+对后两格
EDGE_PLAT_EPS = 0.004  # 平台：单格 |增量| 上限（米/格）
EDGE_PLAT_K = 1.5      # 平台高度下限（×阈）：肩台 1.1~1.49× 不算缘体，
                       # 真 kerb 远场平台 1.7×（000340 R zc14 0.145/0.085）
EDGE_MATCH_M = 0.8     # 「最近一致边界」的邻箱确认窗（米）：边界近于平行行进
#                       方向，相邻箱 x 漂移实测 ≤0.5m；超窗=两箱看到不同结构
SKY_HGT = 1.2         # 空中结构剔除阈：桥/天空域点 hgt≥7.5m，护栏 ≤1m

# ── 可行驶栅格（2026-10-04 离线调查落产线）──────────────────────────────
# 与找边读数并行、互不影响的可行驶域观测：点云 → BEV 三态格。判据=格内非挖洞点
# 的 hgt 中位（|中位|≤容差 → 可走；>容差 → 不可走）；挖洞（ego/物体框）覆盖的
# 格一律判未知——证据被主动移除处不得声明可走。离线调查（2026-10-04，三会话
# 149 帧）实测：有有效点的格近场 85.8%/远场 80.3% 判可走，挖空格 0 误判
# （证据指针见当次提交，不引实验路径）。
#
# 已知局限（如实记录，本次不修）：① 远场可走覆盖率有坏尾——148 帧中 21 帧远场
# 「可见即可走」<60%（无深度点的涂抹区）；② 0.15m 容差未做敏感性扫描，与 kerb
# 顶（0.14~0.21m）在同量级，缘顶格可能落进可走带。
GRID_CELL = 0.25        # 格边长（米）
GRID_X_MAX = 10.5       # 横向半窗（米）：覆盖金标墙基（实测 ±10m）
GRID_Z_LO, GRID_Z_HI = 3.0, 16.0   # 纵向窗（米，车前方）
GRID_TOL = PLANE_TOL    # 可走容差（米，= 平面内点阈）
GRID_MIN_PTS = 3        # 单格判「已知」的最少有效点数
GRID_NX = int(round(2.0 * GRID_X_MAX / GRID_CELL))          # 84
GRID_NZ = int(round((GRID_Z_HI - GRID_Z_LO) / GRID_CELL))   # 52
GRID_UNKNOWN, GRID_DRIVABLE, GRID_BLOCKED = 0, 1, 2


@dataclass(frozen=True)
class DrivableGrid:
    """一帧的可行驶栅格（BEV 三态）。**消费契约**（2026-10-04 定）：

    - ``state == GRID_BLOCKED`` (2) = **硬否决**：高于路面的静态结构（墙/路缘/
      桥）。近场带轨迹裁决（``latsample._grid_penalty``）压到即一票否决——
      消费语义以 latsample 实代码与 decision.json grid_veto._note 为准，本文
      曾写"软代价不否决"是 S2-B 定案前的旧稿，已按实装更正（2026-10-06）。
    - ``state == GRID_UNKNOWN`` (0) = **无证据**：无有效点，或被挖洞（ego/物体框）
      覆盖。既不得当可走用，也不得当障碍用。
    - ``state == GRID_DRIVABLE`` (1) = **唯一正向证据**：格内有地面点且高度对路面
      平面残差在容差内。只有这一态可作为「可走」的正向依据。

    坐标：``state[iz, ix]``；x = −GRID_X_MAX + (ix+0.5)·GRID_CELL（左负右正），
    z = GRID_Z_LO + (iz+0.5)·GRID_CELL（车前方）。``coef`` 为路面平面
    Y=aX+bZ+c；拟合失败（域内点不足）时为 None，此时 ``state`` 全 0（与
    reading 的弃权语义同型，不抛异常）。"""

    state: np.ndarray        # (GRID_NZ, GRID_NX) int8：0 未知 / 1 可走 / 2 不可走
    coef: tuple[float, float, float] | None
    counts: np.ndarray       # (GRID_NZ, GRID_NX) int32：逐格非挖洞有效点数
    dig_cells: int           # 被挖洞覆盖的格数（这些格判未知）
    latency_ms: float
    dig_mask: np.ndarray | None = None   # (GRID_NZ, GRID_NX) bool：挖洞格掩码
    # （S2-B 裁决的洞边清洗用；None=旧构造/测试桩，消费方回退 counts==0 近似）


@dataclass(frozen=True)
class DepthRoadReading:
    """一帧的深度几何读数。edge_lane 车道单位（原点=车，左负右正），弃权侧
    为 None。``edge_pts``=各箱原始检出 ((side, z_m, x_m), ...)，诊断与调试图
    复用（side +1 右 / -1 左）。"""

    left_edge_lane: float | None
    right_edge_lane: float | None
    left_x: float | None          # 最近箱检出缘的像素列（诊断）
    right_x: float | None
    sides: int                    # 在场侧数（0~2）
    latency_ms: float
    rejects: tuple[str, ...] = field(default_factory=tuple)
    edge_pts: tuple[tuple[int, float, float], ...] = ()
    coef: tuple[float, float, float] | None = None   # 路面平面 Y=aX+bZ+c；供
    # drivable_grid 共享（同带同掩码拟合，逐位同值），省一次列剖面重算
    fx: float | None = None    # 同帧全幅焦距（调试图把检测框反投影回 BEV 用；
    fy: float | None = None    #  离线缓存路径 fy 缺=回退 fx，与平面拟合同约定）


def load_session(weights: Path) -> ort.InferenceSession:
    """MoGe q4f16 会话（DML 优先逐级回退 CPU；输入 720×1280 折叠静态）。

    **折叠是时延成立的前提，不是优化项**（机理同 DA 链验证）：batch/height/
    width 三个自由维全部固定触发常量折叠、DML 整图融合；输出点图为 336×598
    原生分辨率（权重名即档位），由 infer_points 升采样回全幅。捕获几何一变，
    infer 立即抛形状错——observe 捕获后返回 None，road_offset 退回纯模型积分。"""
    avail = set(ort.get_available_providers())
    providers = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider",
                             "CPUExecutionProvider") if p in avail]
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"),
                         (1, MOGE_IN_H, MOGE_IN_W)):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(weights), sess_options=so, providers=providers)


def infer_points(sess: ort.InferenceSession, rgb: np.ndarray,
                 with_evidence: bool = False):
    """一帧 → (全幅点图, fx, fy[, 证据包])。点图 (720,1280,3) float32，无效像素
    nan；fx/fy 为全幅像素焦距（原生 336×598 焦距 × 上采样倍率，见 moge_post）。

    ``with_evidence`` 时第三返回值为原生证据 dict（pts=336×598 点图 fp32、
    valid、fx/fy=归一化焦距，复算 ×(W,H)）——离线复算与实机渲染用，比全幅小 6 倍且复算逐位
    一致（复算口径见模块 docstring）。

    DML 互斥（core.dml_lock）：感知会话与深度会话并发 run 会段错误杀进程
    （2026-09-25 实机 + 双线程复现），故 run 本体抢锁、抢不到抛 Busy 让调用方
    跳帧——本层不等待，控制拍的感知优先。"""
    # 分段计时（单 worker 线程调用，无竞争）：observe_debug 取走后进观测面。
    # 离线分解见 probe_moge_stage_decompose；在线口径盯 forward 漂移（显存
    # 吃紧时普通路径会翻倍，IO binding 定案的实机哨）。
    t_pre = time.perf_counter()
    blob = moge_post.preprocess(rgb, MOGE_IN_W, MOGE_IN_H)
    LAST_STAGE_MS["pre"] = (time.perf_counter() - t_pre) * 1000.0
    if not dml_lock.LOCK.acquire(blocking=False):
        raise dml_lock.Busy("DML 被感知推理占用，本帧放弃（latest-only 下一帧再来）")
    try:
        t_fwd = time.perf_counter()
        points, mask, metric_scale = moge_post.forward(sess, blob, 1032)
        LAST_STAGE_MS["forward"] = (time.perf_counter() - t_fwd) * 1000.0
    finally:
        dml_lock.LOCK.release()
    t_rec = time.perf_counter()
    res = moge_post.reconstruct(points, mask, metric_scale)
    LAST_STAGE_MS["reconstruct"] = (time.perf_counter() - t_rec) * 1000.0
    pts = res["pts"]
    ow, oh = points.shape[1], points.shape[0]
    evidence = None
    t_up = time.perf_counter()
    if (oh, ow) != (MOGE_IN_H, MOGE_IN_W):
        if with_evidence:
            evidence = {"pts": pts, "valid": res["valid"],
                        "fx": res["fx"], "fy": res["fy"]}
        pts = np.stack([cv2.resize(pts[..., k], (MOGE_IN_W, MOGE_IN_H),
                                   interpolation=cv2.INTER_LINEAR)
                        for k in range(3)], -1)
        valid = cv2.resize(res["valid"].astype(np.float32), (MOGE_IN_W, MOGE_IN_H),
                           interpolation=cv2.INTER_NEAREST).astype(bool)
    else:
        valid = res["valid"]
    LAST_STAGE_MS["upsize"] = (time.perf_counter() - t_up) * 1000.0
    pts = pts.copy()
    pts[~valid] = np.nan
    # 归一化焦距（u∈[0,1] 口径）→ 全幅像素焦距：u_full = (X/Z)·fx_norm·W_full + W/2
    fx = res["fx"] * MOGE_IN_W
    fy = res["fy"] * MOGE_IN_H
    return (pts, float(fx), float(fy), evidence) if with_evidence \
        else (pts, float(fx), float(fy))


# infer_points 最近一帧的分段耗时（ms，pre/forward/reconstruct/upsize）——
# observe_debug 取走后进 _stage_win 滑窗。单 worker 线程调用，无竞争。
LAST_STAGE_MS: dict[str, float] = {}


def _road_by_column_profile(Z: np.ndarray, fy: float, bad: np.ndarray) -> np.ndarray:
    """检测带深度图 → 路面点布尔图（列剖面特征行走，列间并行向量化）。

    每列自最底有效像素起、沿最近有效行向上走：深度落在 [WALK_Z_BACK,
    WALK_Z_FWD]×前值 且逐行增量落在 [WALK_INC_LO, WALK_INC_HI]×理论量
    （z²/fy·WALK_ROW_STRIDE，下限 WALK_INC_FLOOR）内即判路面并续行；连续
    违反 WALK_MISS_STOP+1 次断列。``bad``（挖除区/无效像素）整格跳过且不计
    违反。实现在列间并行（每步一个 (W,) 向量运算，活跃列递减出队），
    nxt 表 = where(valid, v, -1) 沿行向 cumulative-max 下移一行（最近上方
    有效行）。采样 WALK_COL_STRIDE/WALK_ROW_STRIDE，返回全幅布尔图（仅被
    采样的行列可能为真）。耗 时 ~10ms/帧（产线预算内，2026-10-02 实测）。"""
    Hb, W = Z.shape
    Zs = Z[::WALK_ROW_STRIDE, ::WALK_COL_STRIDE]
    bads = bad[::WALK_ROW_STRIDE, ::WALK_COL_STRIDE]
    fy_s = fy * WALK_ROW_STRIDE     # 跳行后相邻采样行的深度增量理论值同比放大
    valid = np.isfinite(Zs) & (Zs > WALK_Z_MIN) & ~bads
    idx = np.arange(Zs.shape[0], dtype=np.int32)[:, None]
    fill = np.maximum.accumulate(np.where(valid, idx, np.int32(-1)), axis=0)
    nxt = np.empty_like(fill)
    nxt[0] = -1
    nxt[1:] = fill[:-1]

    road = np.zeros((Hb, W), bool)
    road_s = road[::WALK_ROW_STRIDE, ::WALK_COL_STRIDE]   # 基础切片视图，写穿
    cur = np.where(valid, idx, np.int32(-1)).max(0)
    active = valid.any(0) & (valid.sum(0) >= WALK_MIN_COL_PTS)
    if not active.any():
        return road
    act = np.nonzero(active)[0]
    road_s[cur[act], act] = True
    cur = cur[act].astype(np.int32)
    prev_z = Zs[cur, act].astype(np.float32)
    miss = np.zeros(len(act), np.int32)

    while len(act):
        nx = nxt[cur, act]
        # cur≥0 是不变量（初值取有效行最大下标，循环内只在 cont⊆(nx≥0) 时改写），
        # 故省掉该比较——与旧式 `(cur>=0)&(nx>=0)&…` 逐位同结果。
        cont = (nx >= 0) & (prev_z < np.float32(ROAD_Z_HI))
        if not cont.any():
            break
        z = Zs[nx, act]
        inc = np.maximum(prev_z * prev_z / np.float32(fy_s),
                         np.float32(WALK_INC_FLOOR))
        d = z - prev_z
        good = cont & (z >= WALK_Z_BACK * prev_z) & (z <= WALK_Z_FWD * prev_z) \
            & (d >= WALK_INC_LO * inc) & (d <= WALK_INC_HI * inc)
        road_s[nx[good], act[good]] = True
        prev_z = np.where(good, z, prev_z)
        bad_step = cont & ~good          # 复用（miss 与 keep 各用一次）
        miss = np.where(bad_step, miss + 1, np.where(cont, 0, miss))
        cur = np.where(cont, nx, cur)
        keep = ~(bad_step & (miss > WALK_MISS_STOP))
        if not keep.all():
            act = act[keep]
            cur = cur[keep]
            prev_z = prev_z[keep]
            miss = miss[keep]
    return road


def _fit_road_plane_seed(road: np.ndarray, X: np.ndarray, Y: np.ndarray,
                         Z: np.ndarray) -> np.ndarray | None:
    """特征路面点上的种子平面拟合（15cm 内点重选，域窗=走廊域常量）。

    走廊窗（ROAD_*）在这里只是远场翘曲限幅——路面识别已由特征行走完成，
    与旧法「走廊=识别手段」语义不同。域点集静态（road 窗内逐点判定），一次
    抽取后域内迭代，避免每轮全幅残差广播。"""
    sel = (road & np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z)
           & (Z > ROAD_Z_LO) & (Z < ROAD_Z_HI) & (np.abs(X) < ROAD_X_MAX))
    n = int(sel.sum())
    if n < MIN_PLANE_PTS:
        return None
    xs, ys, zs = X[sel], Y[sel], Z[sel]
    ones = np.ones(n, np.float32)
    m = np.ones(n, bool)
    coef = None
    for _ in range(PLANE_ITERS):
        if int(m.sum()) < MIN_PLANE_PTS:
            return None
        A = np.stack([xs[m], zs[m], ones[m]], 1)
        coef = np.linalg.lstsq(A, ys[m], rcond=None)[0]
        m_next = np.abs(ys - (coef[0] * xs + coef[1] * zs + coef[2])) < PLANE_TOL
        # 内点集不动 ⇒ 下一轮 lstsq 吃同一矩阵、出同一 coef、再得同一内点集，
        # 提前收（逐位等价：只是跳过若干次结果相同的迭代，不改返回值）。
        if np.array_equal(m_next, m):
            break
        m = m_next
    return coef


def _fit_road_plane(X: np.ndarray, Y: np.ndarray, Z: np.ndarray,
                    dig: np.ndarray | None, fy: float | None = None
                    ) -> np.ndarray | None:
    """路面平面拟合 Y=aX+bZ+c → coef；域内点不足判失败（None，诚实弃权）。

    两段式（2026-10-02 换装）：**特征种子**——列剖面特征识别的路面点先拟合
    出起步平面（墙/天空/车辆/路外不进种子）；**走廊收敛**——从种子平面起
    按旧动力学（走廊域 15cm 内点重选 lstsq）收敛。旧法病理在起步种子被墙
    劫持（墙面点与路面相连、迭代内点沿墙爬升，000660 被拽 5.25°），不在
    走廊重选本身；好种子进不了坏盆地，远场覆盖由走廊重选全数找回。
    ``fy``=None 走纯旧位置圈地路径（回退开关）；特征种子失败同样回退。
    走廊域逐点静态，一次抽取域点集后域内迭代（布尔掩码保序，与逐轮全幅
    重选逐位等价）。"""
    ok = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z) & (Z > 0)
    road = (ok & (np.abs(X) < ROAD_X_MAX) & (Z > ROAD_Z_LO) & (Z < ROAD_Z_HI)
            & ~((np.abs(X) < EGO_ABS_X) & (Z < EGO_ABS_Z)))
    if dig is not None:
        road = road & ~dig
    n = int(road.sum())
    if n < MIN_PLANE_PTS:
        return None
    xs, ys, zs = X[road], Y[road], Z[road]
    ones = np.ones(n, np.float32)
    m = None
    if fy is not None:
        bad = ~np.isfinite(Z) | (Z <= WALK_Z_MIN)
        if dig is not None:
            bad = bad | dig
        seed = _fit_road_plane_seed(_road_by_column_profile(Z, fy, bad), X, Y, Z)
        if seed is not None:
            m = np.abs(ys - (seed[0] * xs + seed[1] * zs + seed[2])) < PLANE_TOL
    if m is None:
        m = np.ones(n, bool)        # 旧法起步：全域进第一轮
    coef = None
    for _ in range(PLANE_ITERS):
        if int(m.sum()) < MIN_PLANE_PTS:
            return None
        A = np.stack([xs[m], zs[m], ones[m]], 1)
        coef = np.linalg.lstsq(A, ys[m], rcond=None)[0]
        m_next = np.abs(ys - (coef[0] * xs + coef[1] * zs + coef[2])) < PLANE_TOL
        if np.array_equal(m_next, m):   # 同 _fit_road_plane_seed：内点集不动即收
            break
        m = m_next
    return coef


def _edge_body_evidence(ph: np.ndarray, i: int, thr: float,
                        genuine: bool) -> bool:
    """穿越候选 (i, i+1) 的缘体证据：脸（骤升）或平台（到顶）。

    残差缓坡的剖面以缓斜率单调爬升、永不到顶——脸与平台皆无即判缓坡。
    脸窗含真穿越对的穿阈上升本身（一格脸的 kerb：0→0.15 后顶缓升，合成
    用例；缓坡穿阈上升仅 0.005~0.026 不会误入）；平台高度下限
    （EDGE_PLAT_K×阈）把缓坡肩台与真缘顶分开。"""
    lo = i - 1 if genuine else i
    face = np.diff(ph[lo:min(i + 3, len(ph))])
    if face.size and face.max() >= EDGE_FACE_MIN:
        return True
    steps = np.diff(ph[i:])
    body = ph[i + 1:]
    need = EDGE_PLAT_K * thr
    run = 0
    for k, s in enumerate(steps):
        run = run + 1 if abs(s) <= EDGE_PLAT_EPS else 0
        if run >= 3 and body[k - 2:k + 1].min() >= need:
            return True
    return False


def _binned_median(values: np.ndarray, bin_idx: np.ndarray, want: np.ndarray,
                   counts: np.ndarray) -> np.ndarray:
    """按格分组的中位数（float32 值 → float64 返回），与逐格 ``np.median`` 逐位同值。

    实现：把「格号 | 顺序保持的 float32 位模式」拼成一个 int64 复合键，**一次
    排序**即得「按格分组、格内升序」；中位取段中项，偶数格取两中项均值——均值
    在 float32 内做，与 ``np.median`` 的 ``mean(part[i-1:i+1])``（float32 累加后
    除 2，除 2 精确）逐位同值。位模式变换是双射且保序（负数段整体映射到低位
    段），故排序结果与按数值排序同序；排序只改排列不改多重集，中位值与之无关。
    ``counts`` 为全格点数（调用方已算好）。"""
    bits = values.view(np.uint32).astype(np.int64)
    key = (bin_idx.astype(np.int64) << 32) | np.where(
        bits >= 0x80000000, 0xFFFFFFFF - bits, bits + 0x80000000)
    key.sort()
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    mid = starts[want] + (counts[want] >> 1)

    def _decode(k: np.ndarray) -> np.ndarray:
        m = k & 0xFFFFFFFF
        b = np.where(m >= 0x80000000, m - 0x80000000, 0xFFFFFFFF - m)
        return b.astype(np.uint32).view(np.float32)

    out = _decode(key[mid])
    even = (counts[want] & 1) == 0
    if even.any():
        out = out.copy()
        out[even] = (_decode(key[mid[even] - 1]) + out[even]) * np.float32(0.5)
    return out.astype(np.float64)


def _scan_side(xb: np.ndarray, hb: np.ndarray, side: int, zc: float,
               extent: float) -> float:
    """单 Z 箱单侧的离地穿越位置（米）；找不到为 nan。side=+1 右 / -1 左。

    从路心向外扫 0.25m 格高度中位剖面（路缘在点云里是缓坡爬升不是竖直台阶）：
    先找地面（内侧格可能被自车/阴影抬高，跳过），穿越取阈值两格持续 + 相邻格
    线性插值 + **尾部持续门**（EDGE_TAIL_FRAC：影子 bump/路面渐变穿过后回落
    路面，真缘后是缘体；未过门的穿越跳过继续向外找，全不入门=该箱该侧弃权）
    + **缘体证据门**（脸或平台，见 EDGE_* 注释：缓坡类假缘穿越跳过继续向外
    找——缓坡后方常接真墙，d00003 L/000906 R 语料实证）。对内台阶对（前格
    已在阈上，缘基在 i→i+1 间）穿越取对首格 px[i]，禁 (i−1→i) 线性外插——
    外插在平缓对上放大噪声出幽灵位置（000906 R zc10 实测 −5.6/+30.6）。
    画外格（|x|>extent·zc，u 越界）不参与。

    分箱与中位走向量化（2026-10-04）：格 = [−X_MAX+k·DX, −X_MAX+(k+1)·DX)，索引
    由 ``floor((x+X_MAX)/DX)`` 一次算出（float64 下 +X_MAX 精确、DX=0.25 是 2 的
    幂故除法精确，与旧码逐格 ``(x>=lo)&(x<hi)`` 逐位同判定）；格中位按索引一次
    分组求取。格心侧别/画界门只依赖 k，先算成掩码再取点。"""
    # 选点化简（逐位等价）：side=±1 下 |xb|≤X_MAX ⟺ xb·side≤X_MAX（另一半由
    # xb·side>0.2 蕴含），且 NaN/±inf 经两次比较自然出局——故 isfinite(xb) 与
    # abs() 都可省。isfinite(hb) 保留（NaN 会污染 median，语义需要）。
    s = xb * side
    sel = (s > 0.2) & (s <= X_MAX_M) & np.isfinite(hb)
    if sel.sum() < MIN_SIDE_PTS:
        return np.nan
    xs, hs = xb[sel], hb[sel]
    bi = np.floor((xs.astype(np.float64) + X_MAX_M) / XBIN_DX).astype(np.int64)
    inb = (bi >= 0) & (bi < N_XBIN)           # x==+X_MAX 恰落域外（末格右开）
    bi = np.where(inb, bi, 0)
    k_ok = (XBIN_CENTERS * side > 0) & (np.abs(XBIN_CENTERS) <= extent * zc)
    keep = inb & k_ok[bi]
    counts = np.bincount(bi[keep], minlength=N_XBIN)
    want = np.nonzero(k_ok & (counts >= MIN_BIN_PTS))[0]
    if want.size < 3:
        return np.nan
    px = XBIN_CENTERS[want]
    ph = _binned_median(hs[keep], bi[keep], want, counts)
    srt = np.argsort(px * side)  # 路心 → 外
    px, ph = px[srt], ph[srt]
    thr = EDGE_HT + EDGE_HT_SLOPE * max(0.0, zc - 3.0)
    g = -1
    for i in range(len(px)):  # 先找到地面
        if ph[i] <= thr:
            g = i
            break
    if g < 0:
        return np.nan
    for i in range(g + 1, len(px) - 1):
        if ph[i] > thr and ph[i + 1] > thr:  # 两格持续
            # 尾部持续门（取第一个过门的穿越=最近可信缘；未过门的假缘跳过、
            # 继续向外找——d00017：−1.5m 影子 bump 被拒后 −5.1m 真墙接棒）：
            tail = ph[i + 2:]
            if len(tail) and (tail > thr).mean() < EDGE_TAIL_FRAC:
                continue
            # 缘体证据门：脸或平台皆无=残差缓坡，跳过继续向外找
            genuine = ph[i - 1] <= thr
            if not _edge_body_evidence(ph, i, thr, genuine):
                continue
            if genuine:  # 真越阈对：i−1→i 线性插值
                f = (thr - ph[i - 1]) / (ph[i] - ph[i - 1])
                return float(px[i - 1] + f * (px[i] - px[i - 1]))
            return float(px[i])  # 对内台阶对：缘基取对首格
    return np.nan


def reading_from_points(pts: np.ndarray, fx: float, cal: Calib,
                        ego_mask: np.ndarray | None = None,
                        object_mask: np.ndarray | None = None,
                        fy: float | None = None,
                        ) -> DepthRoadReading:
    """点图 → 读数（纯函数，回归锁可直接喂缓存点云；observe=推理+本函数）。

    ``pts`` 全幅 (720,1280,3)、无效像素 nan；``fx`` 同帧全幅焦距（横向剖面画外
    守卫与诊断回投共用）。``fy`` 同帧纵向焦距（列剖面特征行走的增量理论值用）；
    None 时回退 fx——MoGe 归一化内参约定下横纵焦距差实测 ~0.1%，远小于行走
    容差窗（离线点云缓存只存了 fx，故回退合法；产线 observe 传真值）。
    ``ego_mask``（紧矩形）与 ``object_mask``（YOLO 框）都从点云挖除（断车身
    →路面粘连）。"""
    t0 = time.perf_counter()

    def _ret(lane_l, lane_r, u_l, u_r, rejects, edge_pts=()):
        return DepthRoadReading(
            left_edge_lane=lane_l, right_edge_lane=lane_r,
            left_x=u_l, right_x=u_r,
            sides=int(lane_l is not None) + int(lane_r is not None),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            rejects=tuple(rejects), edge_pts=edge_pts,
            coef=(None if coef is None
                  else (float(coef[0]), float(coef[1]), float(coef[2]))),
            fx=float(fx), fy=float(fy if fy is not None else fx))

    dig = _dig_band(ego_mask, pts.shape[1])
    if object_mask is not None:
        dig = dig | _dig_band(object_mask, pts.shape[1])

    X, Y, Z = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
    coef = _fit_road_plane(X, Y, Z, dig, fy=fx if fy is None else fy)
    if coef is None:
        return _ret(None, None, None, None, ["平面拟合失败"])
    a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
    # Y 向下为正 → 残差符号随平面高度（c）走：取 z=5m 处路面 Y 的符号定翻转，
    # hgt 统一为「高出路面为正」。
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Y - (a * X + b * Z + c))
    sky = np.isfinite(hgt) & (hgt > SKY_HGT)
    extent = (pts.shape[1] / 2.0) / fx   # 画面横边界比 |X|/Z（同帧焦距，禁写死）

    edge_pts: list[tuple[int, float, float]] = []
    rejects: list[str] = []
    # 与 Z 无关的掩码提出循环（布尔与可交换结合，逐位同结果）：5 个 ZBIN 各
    # 少算 3 遍全带布尔运算。
    base = np.isfinite(X) & np.isfinite(hgt) & (~dig) & (~sky)
    for z0, z1 in ZBIN:
        m = base & (Z >= z0) & (Z < z1)
        if m.sum() < MIN_BOX_PTS:
            continue
        zc = (z0 + z1) / 2
        for side in (1, -1):
            ex = _scan_side(X[m], hgt[m], side, zc, extent)
            if np.isfinite(ex):
                edge_pts.append((side, zc, ex))

    def _side(side: int) -> tuple[float | None, float | None, str | None]:
        smp = sorted((zc, x) for s, zc, x in edge_pts if s == side)
        if not smp:
            return None, None, f"{'L' if side < 0 else 'R'}:无穿越"
        # 最近一致边界（2026-10-02 真机 d00003/08 取证后换装）：跨箱中位数把
        # 「车道级」与「护栏级」两种语义的边界混进一个统计，箱可用性随点云
        # 空洞翻转时读数参照系跟着翻（左缘在 −1 与 −3.4 车道间逐帧跳）。改为
        # 取「被次近箱确认的最近箱穿越」——最近的双箱一致边界 = 最近的可信
        # 约束；单箱孤证（阴影/漆线的假穿越，d00010 z6-8 箱 −1.26m 实证）被
        # 排除；无一致对退回中位数（弯道逐箱漂移时保持旧行为）。
        pick: float | None = None
        for i in range(len(smp) - 1):
            if abs(smp[i][1] - smp[i + 1][1]) <= EDGE_MATCH_M:
                pick = smp[i][1]
                break
        if pick is None:
            pick = float(np.median([x for _, x in smp]))
        lane = float(pick / cal.lane_w_m)
        zn, xn = smp[0]                        # 最近箱为像素诊断锚
        return lane, float(pts.shape[1] / 2.0 + xn * fx / zn), None

    lane_l, u_l, rej_l = _side(-1)
    lane_r, u_r, rej_r = _side(1)
    if rej_l:
        rejects.append(rej_l)
    if rej_r:
        rejects.append(rej_r)
    # 侧别门：车道量须在自己一侧（与退役像素尺同号约定，L 负 R 正）——
    # 对侧"缘"混进配对会让路心合成翻转（历史实证，2026-09-27）。
    if lane_l is not None and lane_l > -0.15:
        rejects.append(f"L:lane{lane_l:+.2f}")
        lane_l = u_l = None
    if lane_r is not None and lane_r < 0.15:
        rejects.append(f"R:lane{lane_r:+.2f}")
        lane_r = u_r = None
    return _ret(lane_l, lane_r, u_l, u_r, rejects, tuple(edge_pts))


def drivable_grid_from_points(pts: np.ndarray, fx: float, fy: float,
                              ego: np.ndarray | None = None,
                              obj: np.ndarray | None = None,
                              coef: tuple[float, float, float] | None = None
                              ) -> DrivableGrid:
    """点图 → 可行驶栅格（纯函数；与 ``reading_from_points`` 并行、互不影响）。

    ``pts``/``fx``/``fy``/``ego``/``obj`` 口径与 ``reading_from_points`` 一致
    （全幅点图、同帧全幅焦距、ego 紧矩形与物体框掩码）。复用 ``_fit_road_plane``
    与 ``_binned_median``（只调用不复制）：平面与 reading 同源，格中位与找边剖面
    同值。``coef`` 传 reading 的 ``DepthRoadReading.coef``（同带同掩码拟合出的
    同一平面）即跳过本函数内的重复拟合——调用点必须传，单独调用才允许缺省。

    **耗时**（2026-10-04 实测，本机空载）：自拟合口径 p50 ≈ 34ms / 帧（6 帧
    33~74ms，随机器负载浮动），主因是列剖面平面拟合（占 ~60%），共享 coef 后
    剩格中位与逐格归约。调用点在异步 worker 线程内，不占控制拍。

    **失败语义**：平面拟合失败（域内点不足）→ ``coef=None``、``state`` 全 0、
    ``counts`` 全 0、``dig_cells=0``；不抛异常（与 reading 的诚实弃权同型）。
    """
    t0 = time.perf_counter()

    def _ret(state, coef, counts, dig_cells, dig_mask=None):
        return DrivableGrid(state=state, coef=coef, counts=counts,
                            dig_cells=dig_cells,
                            latency_ms=(time.perf_counter() - t0) * 1000.0,
                            dig_mask=dig_mask)

    dig = _dig_band(ego, pts.shape[1])
    if obj is not None:
        dig = dig | _dig_band(obj, pts.shape[1])
    Xb, Yb, Zb = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
    if coef is None:
        coef = _fit_road_plane(Xb, Yb, Zb, dig, fy=fy)
        if coef is None:
            return _ret(np.zeros((GRID_NZ, GRID_NX), np.int8), None,
                        np.zeros((GRID_NZ, GRID_NX), np.int32), 0)
    a, b, c = (float(coef[0]), float(coef[1]), float(coef[2]))
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0      # 同 reading：hgt 以高出路面为正
    hgt = sgn * (Yb - (a * Xb + b * Zb + c))
    ok = (np.isfinite(Xb) & np.isfinite(Yb) & np.isfinite(Zb) & np.isfinite(hgt)
          & (Xb >= -GRID_X_MAX) & (Xb < GRID_X_MAX)
          & (Zb >= GRID_Z_LO) & (Zb < GRID_Z_HI))
    # 格号：−GRID_X_MAX 起 0.25 步进（GRID_CELL=2⁻² 的幂，+X_MAX 与除法均精确；
    # ok 已保证落窗，格号必在 [0,NX)×[0,NZ) 内，无需 clip）。
    ncell = GRID_NX * GRID_NZ
    ix = np.floor((Xb[ok] + GRID_X_MAX) / GRID_CELL).astype(np.int64)
    iz = np.floor((Zb[ok] - GRID_Z_LO) / GRID_CELL).astype(np.int64)
    flat = (iz * GRID_NX + ix)
    dug = dig[ok]
    hv = np.ascontiguousarray(hgt[ok], np.float32)

    nod = ~dug
    flat_nod = flat[nod]
    counts = np.bincount(flat_nod, minlength=ncell).astype(np.int32)
    med = np.full(ncell, np.nan, np.float64)
    want = np.nonzero(counts > 0)[0]
    if want.size:
        med[want] = _binned_median(hv[nod], flat_nod, want, counts)
    dug_mask = np.zeros(ncell, bool)
    if dug.any():
        dug_mask[np.unique(flat[dug])] = True
    state = np.full(ncell, GRID_UNKNOWN, np.int8)
    known = (counts >= GRID_MIN_PTS) & (~dug_mask)
    state[known & (np.abs(med) <= GRID_TOL)] = GRID_DRIVABLE
    state[known & (np.abs(med) > GRID_TOL)] = GRID_BLOCKED
    return _ret(state.reshape(GRID_NZ, GRID_NX), (a, b, c),
                counts.reshape(GRID_NZ, GRID_NX), int(dug_mask.sum()),
                dug_mask.reshape(GRID_NZ, GRID_NX))


def grid_center_lane(grid: DrivableGrid, lane_w_m: float) -> float | None:
    """可行驶栅格全带路心（车道单位，右正，原点=相机光轴/自车）。

    口径=离线基线胜出的「宽度加权中位」（2026-10-05 两场 25 帧人工判读，
    证据 commit f9a9a80：mean 0.31 道/大错 8%/覆盖 100%，同尺对照找边
    0.52/22%/72%）：逐行取绿区 [min,max] **范围中点**（非质心——ego 挖洞楔
    居中挖走中央证据，范围中点对它免疫），行中点按绿区宽度加权取中位数。
    无绿行（全 unknown/blocked 或 coef 拟合失败的全 0 栅格）→ None=弃权。

    极性契约（v3 事故链门禁同款）：绿区居 X=+1m → 返回 +1/lane_w_m（正值=
    路心在右）；消费方 `_EgoRoadObserver` 取 off=−值，与 off=−(L+R)/2 同式。
    栅格与找边共用 MoGe 点云/平面/挖洞掩码——本读数不是独立传感器，只作
    「找边锁错结构」的互证面（消费闸见 decision.json grid_xcheck）。"""
    if not (lane_w_m > 0):
        return None
    drivable = grid.state == GRID_DRIVABLE
    has = drivable.any(axis=1)
    if not has.any():
        return None
    cols = np.arange(grid.state.shape[1])
    lo_ix = np.where(drivable, cols, grid.state.shape[1]).min(axis=1)[has]
    hi_ix = np.where(drivable, cols, -1).max(axis=1)[has]
    lo_m = -GRID_X_MAX + lo_ix * GRID_CELL
    hi_m = -GRID_X_MAX + (hi_ix + 1) * GRID_CELL
    mids = (lo_m + hi_m) / 2.0
    weights = hi_m - lo_m
    order = np.argsort(mids)
    cw = np.cumsum(weights[order])
    mid = float(mids[order][np.searchsorted(cw, cw[-1] / 2.0)])
    return mid / lane_w_m


def _dig_band(dig: np.ndarray | None, width: int) -> np.ndarray:
    """全帧掩码 → 检测带切片（None 给全假）。"""
    if dig is None:
        return np.zeros((DIAG_Y1 - Y0, width), bool)
    return np.asarray(dig, bool)[Y0:DIAG_Y1]


class DepthRoadObserver:
    """深度几何观测器（阶段生命期=chain；ORT session 跨阶段复用、外部注入）。

    session 为 None（权重缺失/加载失败）时 observe 恒 None——road_offset 退回
    纯模型积分，与"无边界"路径行为一致。"""

    def __init__(self, session: ort.InferenceSession | None,
                 cal: Calib) -> None:
        self._sess = session
        self._cal = cal
        self._ego_mask = self._load_ego_mask()
        # observe 分段滑窗（worker 单线程写；分段口径见 LAST_STAGE_MS）
        self._stage_win: dict[str, deque] = {
            k: deque(maxlen=200)
            for k in ("pre", "forward", "reconstruct", "upsize", "edges", "grid")}

    @staticmethod
    def _load_ego_mask() -> np.ndarray | None:
        """3D 挖除区 = 自车高区矩形（ego_mask.json 的 y0~y1/x0~x1）。

        只取紧矩形：矩形下缘之下是可见路面，多挖一寸都是把真路面从点云里
        抹掉（3D 分箱口径下的实证）；旧「列带下延」是 2D 逐行扫描的防漏
        语义，随旧链退役，不再共用一个构造。"""
        p = Path(__file__).resolve().parent / "resources" / "calibration" / "ego_mask.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            mask = np.zeros((720, 1280), bool)
            mask[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
            return mask
        except Exception:
            return None

    def observe(self, frame_rgb: np.ndarray,
                object_mask: np.ndarray | None = None) -> DepthRoadReading | None:
        """一帧 → 读数（两侧可各自弃权）；推理失败 → None（不抛，控制链不因感知停摆）。

        ``object_mask``：YOLO 检测框（车/金币/奖励，外扩后）布尔掩码，与 ego
        掩码同通道挖除——物体从点云源头消失，"穿车读墙"假缘不再产生。"""
        reading, _ = self.observe_debug(frame_rgb, object_mask)
        return reading

    def observe_debug(self, frame_rgb: np.ndarray, object_mask: np.ndarray | None = None,
                      ) -> tuple[DepthRoadReading | None, dict | None]:
        """同 observe，另返回原生证据包（离线复算+实机渲染用；与 reading 同源同拍）。

        证据包 dict：pts=336×598 原生点图 fp32、valid、fx/fy=原生归一化焦距。
        复算口径见模块 docstring「离线复算」。"""
        if self._sess is None:
            return None, None
        t0 = time.perf_counter()
        try:
            pts, fx, fy, evid = infer_points(self._sess, frame_rgb, with_evidence=True)
        except dml_lock.Busy:
            raise    # 让锁跳帧是协议行为（DML 互斥），不算推理失败——worker 侧单独计数
        except Exception:
            return None, None
        stage = dict(LAST_STAGE_MS)
        t_e = time.perf_counter()
        reading = reading_from_points(pts, fx, self._cal, ego_mask=self._ego_mask,
                                      object_mask=object_mask, fy=fy)
        stage["edges"] = (time.perf_counter() - t_e) * 1000.0
        # 可行驶栅格：与读数同源同拍、并行产出（不替换、不接决策层）。在 worker
        # 线程内算，不占控制拍；平面共享 reading 的拟合结果（同带同掩码，逐位
        # 同值），避免列剖面重算（自拟合口径 ~34ms/帧）。
        t_g = time.perf_counter()
        grid = drivable_grid_from_points(pts, fx, fy, self._ego_mask, object_mask,
                                         coef=reading.coef)
        stage["grid"] = (time.perf_counter() - t_g) * 1000.0
        for k, v in stage.items():
            self._stage_win[k].append(v)
        evid["grid"] = grid
        return replace(reading,
                       latency_ms=(time.perf_counter() - t0) * 1000.0), evid

    @property
    def session_ready(self) -> bool:
        return self._sess is not None


def _hgt_rgb(h: np.ndarray) -> np.ndarray:
    """高出路面(m) → RGB：蓝=低于路面，灰=路面同高，
    灰→橙(0.3m)→红(0.7m)=抬升值，暗红=更高。"""
    h = np.asarray(h, np.float32)
    out = np.zeros(h.shape + (3,), np.uint8)
    lo = h < -0.06
    road = (~lo) & (np.abs(h) <= 0.06)
    out[lo] = (60, 60, 220)
    out[road] = (150, 150, 150)
    ab = (~lo) & (~road)
    if ab.any():
        ha = h[ab][:, None]
        t1 = np.clip((ha - 0.06) / 0.24, 0, 1)
        t2 = np.clip((ha - 0.30) / 0.40, 0, 1)
        g2o = np.array([150, 150, 150]) + t1 * (np.array([255, 120, 60]) - np.array([150, 150, 150]))
        o2r = np.array([255, 120, 60]) + t2 * (np.array([230, 30, 30]) - np.array([255, 120, 60]))
        out[ab] = np.where(ha <= 0.3, g2o, o2r).astype(np.uint8)
    out[h > 0.7] = (150, 20, 20)
    return out


def _grid_rgb(state: np.ndarray) -> np.ndarray:
    """三态栅格 → BGR（绿=可走/红=不可走/灰=未知；与离线探针同配色）。"""
    out = np.zeros(state.shape + (3,), np.uint8)
    out[state == GRID_UNKNOWN] = (128, 128, 128)
    out[state == GRID_DRIVABLE] = (0, 190, 0)
    out[state == GRID_BLOCKED] = (0, 0, 220)
    return out


def _grid_panel(grid: DrivableGrid, width: int, scale: int = 4) -> np.ndarray:
    """可行驶栅格小图（BEV 俯视，z16 在上、z3 在下；x 左负右正）+ 图例。"""
    g = _grid_rgb(grid.state)[::-1]          # 行 0 = z16（远）
    img = cv2.resize(g, (GRID_NX * scale, GRID_NZ * scale),
                     interpolation=cv2.INTER_NEAREST)
    head = 22
    panel = np.full((img.shape[0] + head, width, 3), 25, np.uint8)
    panel[head:head + img.shape[0], :img.shape[1]] = img
    r9 = int((GRID_Z_HI - 9.0) / (GRID_Z_HI - GRID_Z_LO) * GRID_NZ * scale) + head
    cv2.line(panel, (0, r9), (GRID_NX * scale, r9), (0, 200, 255), 1)
    c0 = GRID_NX * scale // 2
    cv2.line(panel, (c0, head), (c0, head + img.shape[0]), (0, 200, 255), 1)
    cv2.putText(panel,
                f"drivable grid  z3(bottom)..z16(top)  x{GRID_X_MAX:.1f}m  "
                f"绿=可走 红=不可走 灰=未知  dig_cells={grid.dig_cells}  "
                f"{grid.latency_ms:.1f}ms",
                (GRID_NX * scale + 10, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (220, 220, 220), 1)
    return panel


def render_depth_debug(frame_rgb: np.ndarray, evid: dict | None,
                       reading: DepthRoadReading, ego_mask: np.ndarray | None,
                       object_mask: np.ndarray | None = None,
                       note: dict | None = None,
                       grid: DrivableGrid | None = None) -> np.ndarray:
    """实机可视化判据（三行堆叠，BGR）：程序看了什么、算了什么、判了什么。

    ① 画面帧：ego 掩码橙描边 / YOLO 物体掩码蓝描边 / 检出缘像素锚黄点 + L/R 车道量；
    ② 高出路面图（蓝=低于、灰=路面、橙红=抬高）+ 各箱检出缘黄圈；
    ③ 弃权原因 + 焦距/相机高度诊断带。``grid`` 非 None 时在②③之间插一行 BEV
    可行驶栅格小图（绿=可走/红=不可走/灰=未知，与离线探针同配色）。纯函数只渲染
    不落盘——落盘归异步 worker 节流。evid=None（推理失败的空拍）时只出①③。"""
    h, w = frame_rgb.shape[:2]
    f = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    for mask, col in ((ego_mask, (255, 128, 0)), (object_mask, (255, 0, 0))):
        if mask is None:
            continue
        cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(f, cnts, -1, col, 2)
    lt = "-" if reading.left_edge_lane is None else f"{reading.left_edge_lane:+.2f}"
    rt = "-" if reading.right_edge_lane is None else f"{reading.right_edge_lane:+.2f}"
    cv2.putText(f, f"L {lt}  R {rt}  sides={reading.sides}", (8, 26),
                cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 2)

    hm = np.zeros((DIAG_Y1 - Y0, w, 3), np.uint8)
    fx_f = float("nan")
    cam_h: float | None = None
    if evid is not None:
        pts_n, valid_n = evid["pts"], evid["valid"]
        fx_f = float(evid["fx"]) * w   # 归一化焦距 → 全幅像素焦距
        pts = np.stack([cv2.resize(pts_n[..., k], (w, h),
                                   interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
        valid = cv2.resize(valid_n.astype(np.float32), (w, h),
                           interpolation=cv2.INTER_NEAREST).astype(bool)
        pts = pts.copy()
        pts[~valid] = np.nan
        X, Y, Z = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
        dig = _dig_band(ego_mask, w)
        if object_mask is not None:
            dig = dig | _dig_band(object_mask, w)
        coef = _fit_road_plane(X, Y, Z, dig)
        if coef is not None:
            a_c, b_c, c_c = (float(coef[0]), float(coef[1]), float(coef[2]))
            sgn = -1.0 if b_c * 5.0 + c_c > 0 else 1.0
            hgt = sgn * (Y - (a_c * X + b_c * Z + c_c))
            okm = np.isfinite(hgt)
            hm[okm] = _hgt_rgb(hgt[okm])
            cam_h = abs(c_c)                  # 平面在相机正下方的高度≈相机离地高
            for _side, zc, xm in reading.edge_pts:
                # 锚点画在「检出穿越真实所在像素」：按 (side, zc, x_m) 反查带内
                # 点云像素取中位，①原图与②高度图同位各画一圈。不做解析回投——
                # 归一化/原生像素两套焦距单位在这里打过架（fy·h/oh 把黄圈全体
                # 压到带顶，2026-10-02 挂账实证），像素真值零单位约定、零主点
                # 假设，锚点语义=「这些像素的离地穿越被判成缘」。
                # 近地面过滤（hgt≤0.15）：缘底在地面爬升起始处，X 窗内还有整面
                # 缘体（墙脸/车身 hgt≥0.3），不过滤中位会被拽到结构中部。
                m = ((Z >= zc - 1.0) & (Z < zc + 1.0) & (_side * X > 0.2)
                     & (np.abs(X - xm) < 0.4) & np.isfinite(X) & np.isfinite(Z))
                if m.any():
                    mg = m & np.isfinite(hgt) & (hgt <= 0.15)
                    if mg.any():
                        m = mg
                if not m.any():
                    continue
                rr, cc = np.nonzero(m)
                u, v = int(round(np.median(cc))), int(round(np.median(rr)))
                cv2.circle(hm, (u, v), 5, (0, 255, 255), 2)
                cv2.circle(f, (u, v + Y0), 6, (0, 255, 255), 2)
    rej = ";".join(reading.rejects)[:110]
    if rej:
        cv2.putText(hm, rej, (8, hm.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX,
                    .55, (255, 255, 255), 1)
    body = np.vstack([f, hm])
    if grid is not None:
        body = np.vstack([body, _grid_panel(grid, w)])
    if note is None:
        return body
    # 决策带：消费端一拍快照（state/reason/杆/喂入路心/来源/帧龄）。
    band = np.full((56, w, 3), 30, np.uint8)
    st = note.get("state", "?"); rs = note.get("reason", "?")
    sn = note.get("steer"); ro = note.get("ro"); el = note.get("elane")

    def _f(v):
        return f"{v:+.2f}" if isinstance(v, (int, float)) else "-"
    l1 = (f"D[{note.get('fid', '?')}] {st} {rs}  steer={sn:+.3f}"
          if isinstance(sn, (int, float)) else f"D[{note.get('fid', '?')}] {st} {rs}")
    l1 += f"  elane={el:+.2f}" if isinstance(el, (int, float)) else ""
    l1 += f"  xt={_f(note.get('xt'))}"
    l2 = (f"ro={_f(ro)}[{note.get('src', '-')}]"
          f" raw={_f(note.get('ro_raw'))}"
          f" age={note.get('age', '-')}ms new={note.get('new', '-')}"
          f"  fx={fx_f:.0f} cam_h={_f(cam_h)}")
    cv2.putText(band, l1, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 255, 255), 1)
    cv2.putText(band, l2, (8, 46), cv2.FONT_HERSHEY_SIMPLEX, .55, (200, 255, 200), 1)
    return np.vstack([body, band])


class AsyncDepthRoadObserver:
    """深度几何异步观测器（2026-09-24 解耦落地，协议与 treasure OCR worker 同构）。

    为什么解耦这一层：observe 成本（MoGe q4f16 推理 + 点云重建 + 平面拟合/找边
    后处理）同步形态把控制拍预算吃光；路缘是**慢变量**——滞后 1~2 拍（50~100ms）
    读数仍有效；金币/街车是快变目标、必须与本拍像素同源，所以感知与黄线层不做
    异步（treasure「令牌与读数字节同源」的教训：跨线程搬运必有窗口，快变对象上
    窗口=脏读）。

    协议（latest-only，无队列不积压）：
    - push：主线程拷帧覆盖 pending 槽 + wakeup；worker 慢则丢中间帧（丢的是输入，
      不是结果——路缘下一拍还会再来）。captured_ts 用 perf_counter（与耗时/时效
      同族时钟；monotonic 在本机粒度 ~16ms 会把 age 量化）。
    - worker：节流窗（INFER_MIN_INTERVAL_S，开工时刻起算——产出间隔=max(窗,
      observe 耗时)，压 DML 锁占空比）→ pop latest →
      同步 observe（run 本体受 core.dml_lock 互斥：与控制拍感知并发 run 会段错误
      杀进程，2026-09-25 实证；抢不到锁抛 Busy → 计 busy_skips 弃本帧，不排队）
      → **非 None 才发布**结果槽（整包替换，不原地改已发布对象）；
      顶层 try/except 计 failures，单帧异常不杀 daemon。
    - take：主线程消费结果槽（**驻留**：闸内同一结果可被多拍反复消费，取走不清；
      is_new=本拍是否首次消费该结果——消费端保鲜槽与学习只认新证据）；
      age = now − captured_ts 超 max_age_ms → 计 stale、清槽、本拍返回 None
      （road_offset 退纯模型积分——与 session 缺失同一降级路径，宁旧不如无）。
    - health：applied/stale/failures/busy_skips 计数 + age/duration 滑窗——
      不达标报警数据面。
    """

    MAX_AGE_MS = 350.0    # 结果时效预算=复用上限：闸内读数才喂控制。**口径含
                          # worker 自己的推理时延**（2026-10-01 实测定案）：单帧
                          # 135ms + 排队 ~50ms 下 150ms 闸注定 78% 超龄丢弃（健康
                          # 日志「应用 49/超龄丢弃 176」），road_offset 覆盖率只剩
                          # 43%——转向间歇的直接病灶。350 与消费端保鲜槽 TTL
                          # 0.4s 对齐（入口比出口严本就矛盾）；读数语义=v4 几何
                          # 路心（缓变量），非黄线时代的间隙中心——1830 局「300ms
                          # 复用窗方波踢 planner」的教训在旧语义+无保鲜槽下成立，
                          # 若实机翻转率反升（trace steer 拍间反打）再回撤
    INFER_MIN_INTERVAL_S = 0.07   # worker 出工下间隔：锁内只有 run 本体，控制拍
                                  # 感知（同锁）的碰撞等待有上界即可（见 dml_lock）
    PERF_WINDOW = 200     # age/duration 滑窗（与 treasure 同族口径：判据只看尾部）

    DEBUG_INTERVAL_S = 0.5   # 调试图节流（2026-10-02 从 2.0 收紧：2s 一帧在找边
                             # 假设漂移的取证节奏下漏帧——漂移拍与调试图对不上；
                             # 磁盘代价 ≈90 帧/局 ×1.3MB，取证局可承受）

    def __init__(self, observer: DepthRoadObserver,
                 max_age_ms: float = MAX_AGE_MS,
                 debug_dir: Path | None = None,
                 infer_min_interval_s: float = INFER_MIN_INTERVAL_S) -> None:
        self._obs = observer
        self._max_age_ms = float(max_age_ms)
        self._infer_interval_s = float(infer_min_interval_s)
        self._debug_dir = Path(debug_dir) if debug_dir is not None else None
        self._debug_last = float("-inf")   # 「从未出工」：0 会在进程早期(monotonic<窗)误判窗内
        self._debug_seq = 0
        self._lock = threading.Lock()
        self._pending: tuple[int, np.ndarray, float, np.ndarray | None] | None = None
        self._result: tuple[int, DepthRoadReading, DrivableGrid | None,
                            float] | None = None
        self._last_seq = -1               # 消费端已见结果序号（仅主线程触碰）
        self._pushed = 0
        self._applied = 0
        self._stale_drops = 0
        self._stale_new_drops = 0   # 首次消费即超龄（=真浪费，读数从未喂过控制拍）；
                                    # 与 _stale_drops 的差 = 先用后超龄的复用拍数
        self._failures = 0
        self._busy_skips = 0
        self._last_infer = float("-inf")   # 同上：进程早期不得被节流窗拦下首拍
        self._age_win: deque[float] = deque(maxlen=self.PERF_WINDOW)
        self._dur_win: deque[float] = deque(maxlen=self.PERF_WINDOW)
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_age_ms = 0.0

    # ---------- 生命周期（阶段=chain：建即 start，收口 stop） ----------

    def start(self) -> None:
        """session 不可用则不起线程（push/take 全程空转，行为=session 缺失）。"""
        if self._thread is not None or not self._obs.session_ready:
            return
        self._thread = threading.Thread(
            target=self._loop, name="speedrush-depth-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """停 worker；返回是否干净退出（False=超时未退，调用方决定怎么报警——
        本层不引 logger，日志归 module）。"""
        if self._thread is None:
            return True
        self._stop.set()
        self._wakeup.set()   # 唤醒阻塞在 wait 的 worker 立即退出
        self._thread.join(timeout=timeout)
        alive = self._thread.is_alive()
        self._thread = None
        return not alive

    # ---------- 主线程面 ----------

    def push(self, frame_rgb: np.ndarray,
             object_mask: np.ndarray | None = None,
             note: dict | None = None) -> None:
        """object_mask：本帧 YOLO 检测框掩码（与帧同源同拍，见 observe 注）。
        note：消费端上一拍决策快照（只读小 dict，主线程每拍新建）——随帧进
        调试图决策带，供人工核对。无 worker（session 缺失/start 未调）时直接
        空转：不拷帧、不计数——阶段出口的"零结果"报警以 pushed>0 为前提，
        计数了就会误报。"""
        if self._thread is None:
            return
        with self._lock:
            self._pushed += 1
            self._pending = (self._pushed, frame_rgb.copy(), time.perf_counter(),
                             None if object_mask is None else object_mask.copy(),
                             note)
        self._wakeup.set()

    def take(self) -> tuple[DepthRoadReading | None, DrivableGrid | None, bool]:
        """读最新驻留结果 → (reading | None, grid | None, is_new)。

        栅格与读数同帧同拍（同一证据包产出），随结果槽驻留复用——闸内旧
        栅格对轨迹裁决仍是可用证据（与读数同一条慢变量纪律）。超龄返回
        (None, None, False)、清槽并计 stale（每结果至多一次）。

        驻留语义（2026-09-28，"每结果只喂一拍"是闭眼主因之一）：结果不再
        取走即清——同一结果在 age 闸内可被多个控制拍反复消费（路缘是慢
        变量，闸内旧读数仍可用），无新结果的拍不再退纯模型积分。is_new
        标记本拍是否首次消费该结果：路心合成可复用旧结果，但消费端的
        保鲜槽与半宽学习只认新证据（**使用次数 ≠ 学习次数**——合成不是
        新证据的同一条纪律）。"""
        with self._lock:
            item = self._result
            if item is None:
                return None, None, False
            reading, grid, captured_ts = item[1], item[2], item[3]
        age_ms = (time.perf_counter() - captured_ts) * 1000.0
        self.last_age_ms = age_ms
        is_new = item[0] != self._last_seq
        if is_new:
            self._last_seq = item[0]
            self._age_win.append(age_ms)
            self._dur_win.append(reading.latency_ms)
        if age_ms > self._max_age_ms:
            with self._lock:   # seq 比对防误删 worker 刚发布的新结果
                if self._result is not None and self._result[0] == item[0]:
                    self._result = None
                self._stale_drops += 1
                if is_new:   # 发布后一拍都没喂上就超龄：时效链路的真浪费口径
                    self._stale_new_drops += 1
            return None, None, False
        if is_new:
            with self._lock:
                self._applied += 1
        return reading, grid, is_new

    def health(self) -> dict:
        with self._lock:
            pushed, applied, stale, stale_new, failures, busy = (
                self._pushed, self._applied, self._stale_drops,
                self._stale_new_drops, self._failures, self._busy_skips)
        ages, durs = list(self._age_win), list(self._dur_win)

        def _p(xs: list[float], q: float) -> float:
            return 0.0 if not xs else sorted(xs)[min(len(xs) - 1, int(q * (len(xs) - 1)))]

        # observe 分段 p50（离线分解的在线哨：盯 forward 漂移=显存压力，
        # 分段口径见 infer_points.LAST_STAGE_MS；桩 observer 无窗则缺省）
        stage = {f"{k}_p50": _p(list(w), 0.5)
                 for k, w in getattr(self._obs, "_stage_win", {}).items()}

        return {"pushed": pushed, "applied": applied, "stale_drops": stale,
                "stale_new_drops": stale_new,
                "failures": failures, "busy_skips": busy,
                "age_p50": _p(ages, 0.5), "age_p95": _p(ages, 0.95),
                "dur_p50": _p(durs, 0.5), "dur_p95": _p(durs, 0.95),
                "max_age_ms": self._max_age_ms, **stage}

    # ---------- worker 线程 ----------

    def _write_debug(self, frame, evid, reading, object_mask, note=None) -> None:
        """节流落实机调试图（三行堆叠）+ 可复现证据包（336×598 原生点图 fp16 +
        valid + 归一化焦距 + 生效掩码）。

        只有渲染图时读数故障无法离线复现（色标有损、看不到平面钉住了什么）——
        证据包按模块 docstring「离线复算」口径可逐位重放 reading_from_points。
        失败静默吞掉——debug 绝不干扰主路。"""
        now = time.monotonic()
        if now - self._debug_last < self.DEBUG_INTERVAL_S:
            return
        self._debug_last = now
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            self._debug_seq += 1
            stem = self._debug_dir / f"d{self._debug_seq:05d}"
            img = render_depth_debug(frame, evid, reading,
                                     self._obs._ego_mask, object_mask, note,
                                     grid=evid.get("grid") if evid else None)
            cv2.imwrite(str(stem) + ".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
            # 原图全幅落盘（人工金标标注/离线重渲染叠锚点都要全分辨率底图；
            # 证据包只有点云没有帧寸步难行，2026-10-02 取证实证）
            cv2.imwrite(str(stem) + "_frame.jpg",
                        cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 85])
            np.savez(str(stem) + "_evid.npz",
                     pts=evid["pts"].astype(np.float16),
                     valid=np.packbits(evid["valid"].ravel()),
                     valid_shape=np.array(evid["valid"].shape, np.int64),
                     fx=np.float64(evid["fx"]), fy=np.float64(evid["fy"]),
                     fid=np.int64(note["fid"]) if note and note.get("fid") is not None
                     else np.int64(-1))
            merged = self._obs._ego_mask
            if object_mask is not None:
                merged = object_mask if merged is None else (merged | object_mask)
            if merged is not None:
                np.save(str(stem) + "_mask.npy", np.packbits(merged))
            if self._obs._ego_mask is not None:
                np.save(str(stem) + "_ego.npy",
                        np.packbits(self._obs._ego_mask))
        except Exception:  # noqa: BLE001 —— 可视化判据失败不碰主路
            pass

    def _pop_latest(self) -> tuple[int, np.ndarray, float, np.ndarray | None] | None:
        with self._lock:
            item, self._pending = self._pending, None
            return item

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 节流窗按「开工时刻」计时（2026-10-06 实测定案）：产出间隔 =
            # max(窗, observe 耗时)。旧基准（observe 返回后才起算）在 observe
            # 耗时 > 窗时每周期白付一个窗的死等——实机 age p50 188ms ≈ 窗 70
            # + observe 139，白付占结果年龄近四成。窗内不取帧的理由不变
            # （取了也只能弃——age 闸会丢），push 唤醒只提前醒来重新看窗。
            # 时钟用 perf_counter：monotonic 步长 ~15.6ms 会把窗过冲成
            # 粗粒度台阶（capabilities.sleep 同案，2026-10-06）。
            rest = self._infer_interval_s - (time.perf_counter() - self._last_infer)
            if rest > 0:
                self._wakeup.wait(timeout=rest)
                self._wakeup.clear()
                continue
            try:
                item = self._pop_latest()
                if item is None:
                    self._wakeup.wait(timeout=0.5)
                    self._wakeup.clear()
                    continue
            except Exception:  # noqa: BLE001 —— 取帧异常（理论不可达）不杀 daemon
                self._failures += 1
                continue
            # 开工即起算节流窗：Busy/异常路径也吃窗（自然退避——感知持锁期间
            # 不再每拍白付一次 preprocess 重试；代价是锁释放瞬间不再立刻补拍）
            self._last_infer = time.perf_counter()
            try:
                reading, evid = self._obs.observe_debug(item[1], object_mask=item[3])
            except dml_lock.Busy:
                # DML 被控制拍感知占用：弃本帧不排队（latest-only，下一帧再来），
                # 单独计数——这是让锁的常规代价，不是故障。
                self._busy_skips += 1
                continue
            except Exception:  # noqa: BLE001 —— 单帧异常计数后继续（与 treasure 同姿态）
                self._failures += 1
                continue
            if reading is None:  # session 中途失效/推理异常：本帧无结果，不发布
                continue
            with self._lock:
                # (push 序号, 读数, 可行驶栅格, captured_ts)——栅格与读数同帧
                # 同拍（observe_debug 内共享平面拟合产出），S2-B 轨迹裁决消费
                self._result = (item[0], reading,
                                evid.get("grid") if evid else None, item[2])
            # 落盘在发布之后：写盘的几十 ms 不该加进 debug 帧的 age（captured_ts
            # 从 push 起算，落盘挡在发布前 = 每张调试图凭空变旧一段）
            if self._debug_dir is not None:
                self._write_debug(item[1], evid, reading, item[3], item[4])
