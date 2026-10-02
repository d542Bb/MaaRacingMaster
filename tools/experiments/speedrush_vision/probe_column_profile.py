# -*- coding: utf-8 -*-
"""列剖面路面特征提取（v2）+ 新旧平面拟合逐帧对比。

背景（2026-10-02 定案）：位置圈地拟合法（|X|<4m 走廊）在有侧墙/护栏的路段会被
墙体点拽歪——000660 实测路自身平面滚转约负 2.06 度（路种子 99% 内点），产线拟合
却被拽到约负 7.32 度（5.25 度误差，墙基与路面相连、迭代内点沿墙爬升）。维护者
裁定：不收窄常量，必须从原始点云里按**特征**识别路面。

路面特征（平地针孔几何）：同一列（固定 u）的深度剖面自底向上平滑递增，相邻有
效像素的增量 ≈ z²/fy（下一行比当前行远出的理论量）；墙体 = 列内深度近常数，
车辆 = 深度骤降，天空 = 深度跳变。自底向上逐列行走，违反即停（连续 7 次）。

与 2026-10-01 会话内 heredoc 原型的差异：
- 原型全幅逐列 Python 循环（约 0.4 s/帧）；本版列间并行、行内推进的向量化
  实现（产线档 4 列 2 行约 10~20 ms/帧），并在三个演示帧上与原型数值一致
  后入库。
- 原型跑全幅（行号 ≥200 起）；本版收敛到产线检测带（Y0~DIAG_Y1）内——产线
  拟合域就是检测带，带内行号全部 ≥340，原型的 200 行下限自动满足。

换装状态（2026-10-02）：本探针的行走/种子拟合已移植进产线
depth_geo._road_by_column_profile / _fit_road_plane（特征种子 + 走廊收敛混合
法），金标仓回归定案「混合法严格优于旧产线面（配对更优 48/更差 13）」。
本文件保留**参数化参考实现**（等价档与逐列原型逐位一致，异或差=0）与
fit 级语料对比工具（--corpus）；纯特征面（不混合走廊收敛）的配对中位更优
（1.42 vs 产线 2.37）但俯仰基线整体偏移 ~0.3°、近端墙基离地信号被阈值吃掉，
属「换基线 + gate0 重标定」的后续路线，见 memory 与 gate0 复核挂账。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/probe_column_profile.py --frame <jpg>
    python tools/experiments/speedrush_vision/probe_column_profile.py --corpus
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402

GOLD = eph.GOLD

# 产线档（2026-10-02 三帧实测定档 + 金标仓回归修档）：z_stop=30 收远列、
# 4 列 2 行采样——走+拟 18~37ms/帧，与旧走廊拟合单独开销（15~35ms）同量级；
# 拟合加横向走廊 |X|<ROAD_X_MAX（特征已把天空/车辆/路外挡在外面，走廊只剩
# 一个角色：压 MoGe 远场横向翘曲的俯仰偏置——全量回归实测干净帧俯仰偏陡
# 0.4~0.9°，加走廊后回到 0.2~0.3°，找边阈值量级）。等价档（全参默认）与
# 逐列原型逐位一致（三帧异或差=0、拟合系数一致，见模块 docstring）。
PROD_ROAD = {"z_stop": 30.0, "col_stride": 4, "row_stride": 2}
PROD_FIT = {"z_lo": 4.0, "z_hi": 30.0, "x_max": 4.0}

# 行走判据（000660/000100/000260 三帧定值，2026-10-01 会话）：上下界相对前一深度；
# 增量窗相对理论量 z²/fy，下限 5cm 防近平面（z 小时理论增量趋 0、噪声即越界）。
Z_BACK, Z_FWD = 0.85, 1.4      # z 允许区间 [0.85·prev, 1.4·prev]
INC_LO, INC_HI = -0.35, 3.0    # z−prev 允许区间（×理论增量）
INC_FLOOR = 0.05               # 理论增量下限（米）
MISS_STOP = 6                  # 连续违反次数 >6 即停（第 7 次断）
MIN_COL_PTS = 10               # 单列有效像素下限（少于则整列弃权）
Z_MIN = 0.5                    # 深度有效下限（米）


def road_by_column_profile(Z: np.ndarray, fy: float, bad: np.ndarray,
                           z_stop: float | None = None,
                           col_stride: int = 1, row_stride: int = 1
                           ) -> np.ndarray:
    """检测带深度图 → 路面点布尔图（列间并行的向量化行走）。

    ``Z`` (Hb,W) 检测带深度（米）；``fy`` 全幅像素焦距（带内焦距同值，等比采样）；
    ``bad`` (Hb,W) 预剔除掩码（自车/YOLO 挖除区与无效像素，行走时整格跳过、
    不计违反——语义与逐列原型一致）。``z_stop``：当前深度超过即收列（产线档
    30，拟合域用不到更远的点；None=不早停）。``col_stride``/``row_stride``：
    隔列/隔行采样（产线档 4/2；列间相互独立，跳列不影响别的列语义；跳行把
    逐步增量理论值放大 row_stride 倍——平地针孔几何下 Δz ∝ 步进行数——行走
    轮数同比减半；1/1=与原型逐位一致）。返回全幅布尔图，仅被采样的行列可能
    为真。"""
    Hb, W = Z.shape
    Zs = Z[::row_stride, ::col_stride]
    bads = bad[::row_stride, ::col_stride]
    fy_s = fy * row_stride      # 跳行后相邻采样行的深度增量理论值同比放大
    valid = np.isfinite(Zs) & (Zs > Z_MIN) & ~bads
    idx = np.arange(Zs.shape[0], dtype=np.int32)[:, None]

    # nxt[v,u] = 该列 v 上方（更小行号）最近的有效行；-1 = 无。行走自底向上
    # （行号递减）：where(valid, v, -1) 做行向 cumulative-max（携带最近一次
    # 有效行号=nearest-above）再下移一行。
    fill = np.maximum.accumulate(np.where(valid, idx, np.int32(-1)), axis=0)
    nxt = np.empty_like(fill)
    nxt[0] = -1
    nxt[1:] = fill[:-1]

    # road_s 是全幅 road 的跨步视图（基础切片必为视图），写穿免拷回
    road = np.zeros((Hb, W), bool)
    road_s = road[::row_stride, ::col_stride]
    cur = np.where(valid, idx, np.int32(-1)).max(0)
    active = valid.any(0) & (valid.sum(0) >= MIN_COL_PTS)
    if not active.any():
        return road

    # 行走循环压到活跃列上（列死亡即出队；墙列走得最久，后期数组大幅收缩）
    act = np.nonzero(active)[0]
    road_s[cur[act], act] = True
    cur = cur[act].astype(np.int32)
    prev_z = Zs[cur, act].astype(np.float32)
    miss = np.zeros(len(act), np.int32)
    z_stop_z = np.float32(z_stop) if z_stop is not None else None

    while len(act):
        nx = nxt[cur, act]
        cont = (cur >= 0) & (nx >= 0)
        if z_stop_z is not None:
            cont &= prev_z < z_stop_z          # 已远出拟合域，上面的点不要了
        if not cont.any():
            break
        z = Zs[nx, act]
        inc = np.maximum(prev_z * prev_z / np.float32(fy_s), np.float32(INC_FLOOR))
        d = z - prev_z
        good = cont & (z >= Z_BACK * prev_z) & (z <= Z_FWD * prev_z) \
            & (d >= INC_LO * inc) & (d <= INC_HI * inc)
        road_s[nx[good], act[good]] = True
        prev_z = np.where(good, z, prev_z)
        miss = np.where(cont & ~good, miss + 1, np.where(cont, 0, miss))
        cur = np.where(cont, nx, cur)
        keep = ~(cont & ~good & (miss > MISS_STOP))
        if not keep.all():
            act = act[keep]
            cur = cur[keep]
            prev_z = prev_z[keep]
            miss = miss[keep]
    return road


def fit_on_road(road: np.ndarray, X: np.ndarray, Y: np.ndarray, Z: np.ndarray,
                z_lo: float | None = None, z_hi: float | None = None,
                x_max: float | None = None) -> np.ndarray | None:
    """路面点上的平面拟合 Y=aX+bZ+c（生产同款：3 轮 15cm 内点重选 lstsq）。

    与 dg._fit_road_plane 的差别只在拟合域来源：位置圈地（|X|<4m 走廊）换成
    列剖面特征掩码；不再需要走廊/自车排除——特征行走已经把车与墙挡在外面。
    ``z_lo/z_hi``/``x_max`` 只是 lstsq 算力/远场限幅（默认不限=与原型逐位一致；
    产线档 z 取 ROAD_Z_LO/HI）。**x_max 不是摆设**：MoGe 远场横向翘曲会让全
    路宽拟合的俯仰系统性偏陡（金标仓回归 2026-10-02，干净帧 +0.4~0.9°，远端
    找边读数被顶飞），必要时收横向域对齐旧走廊口径。"""
    sel = road & np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z)
    if z_lo is not None:
        sel &= Z > z_lo
    if z_hi is not None:
        sel &= Z < z_hi
    if x_max is not None:
        sel &= np.abs(X) < x_max
    coef = None
    for _ in range(dg.PLANE_ITERS):
        if int(sel.sum()) < dg.MIN_PLANE_PTS:
            return None
        A = np.stack([X[sel], Z[sel], np.ones(int(sel.sum()))], 1)
        coef = np.linalg.lstsq(A, Y[sel], rcond=None)[0]
        res = Y - (coef[0] * X + coef[1] * Z + coef[2])
        sel = road & (np.abs(res) < dg.PLANE_TOL)
    return coef


def fit_hybrid_on_road(road: np.ndarray, X: np.ndarray, Y: np.ndarray, Z: np.ndarray,
                       dig: np.ndarray | None, z_lo: float = 4.0,
                       z_hi: float = 30.0, x_max: float = 4.0
                       ) -> np.ndarray | None:
    """特征面起步 + 走廊域 15cm 内点重选（2026-10-02 金标仓回归定案）。

    为什么不直接用 fit_on_road 的结果：MoGe 路面在点云里系统性不平面（中段
    隆起 4~5cm，000906 实测），旧产线面的俯仰是把这个弯摊平的操作性折中，
    找边阈值（EDGE_HT 量级 4cm）与它耦合；特征拟合的远场覆盖稀疏（行走提前
    停），加权偏近场 → 俯仰偏陡 0.3°，近端墙基离地信号被吃掉（000906 右缘
    读数偏 3.3m）。旧法的失败在**起步种子被墙劫持**，不在走廊重选本身——
    故特征面只当种子，随后按旧产线动力学（走廊域、15cm 重选、同迭代轮数）
    收敛：好种子不会被墙拽走（000660 保持救回），远场覆盖又全部找回（干净
    帧俯仰回到旧基准 0.1° 内）。"""
    seed = fit_on_road(road, X, Y, Z, z_lo=z_lo, z_hi=z_hi, x_max=x_max)
    if seed is None:
        return None
    corr = (np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z)
            & (np.abs(X) < x_max) & (Z > z_lo) & (Z < z_hi))
    if dig is not None:
        corr &= ~dig
    coef = seed
    sel = corr & (np.abs(Y - (coef[0] * X + coef[1] * Z + coef[2])) < dg.PLANE_TOL)
    for _ in range(dg.PLANE_ITERS):
        if int(sel.sum()) < dg.MIN_PLANE_PTS:
            return None
        A = np.stack([X[sel], Z[sel], np.ones(int(sel.sum()))], 1)
        coef = np.linalg.lstsq(A, Y[sel], rcond=None)[0]
        sel = corr & (np.abs(Y - (coef[0] * X + coef[1] * Z + coef[2])) < dg.PLANE_TOL)
    return coef


def _deg(x: float) -> float:
    return math.degrees(math.atan(float(x)))





def _flatness(coef, X, Y, Z):
    """路面平整度读数（|X|<4、Z 4~14、|h|<0.2 内 |h| 的 p50/p95，厘米）。"""
    if coef is None:
        return float("nan"), float("nan"), 0
    h = Y - (coef[0] * X + coef[1] * Z + coef[2])
    m = (np.abs(X) < 4) & (Z > 4) & (Z < 14) & (np.abs(h) < 0.2) & np.isfinite(h)
    if int(m.sum()) < 100:
        return float("nan"), float("nan"), int(m.sum())
    p50 = float(np.percentile(np.abs(h[m]), 50)) * 100
    p95 = float(np.percentile(np.abs(h[m]), 95)) * 100
    return p50, p95, int(m.sum())


def compare_frame(pts: np.ndarray, fx: float, fy: float, ego: np.ndarray | None) -> dict:
    """一帧点图 → 新旧拟合对比读数（拟合都在检测带内做，口径与产线一致）。"""
    X, Y, Z = (pts[dg.Y0:dg.DIAG_Y1, :, k] for k in (0, 1, 2))
    dig = None if ego is None else ego[dg.Y0:dg.DIAG_Y1]

    t0 = time.perf_counter()
    coef_old = dg._fit_road_plane(X, Y, Z, dig)
    t_old = (time.perf_counter() - t0) * 1000

    bad = ~np.isfinite(Z) | (Z <= Z_MIN)
    if dig is not None:
        bad = bad | dig
    t0 = time.perf_counter()
    road = road_by_column_profile(Z, fy, bad, **PROD_ROAD)
    t_road = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    coef_new = fit_hybrid_on_road(road, X, Y, Z, dig, **PROD_FIT)
    t_new = (time.perf_counter() - t0) * 1000

    out = {"road_px": int(road.sum()), "t_road_ms": t_road, "t_old_ms": t_old,
           "t_new_ms": t_new}
    for key, coef in (("old", coef_old), ("new", coef_new)):
        p50, p95, n = _flatness(coef, X, Y, Z)
        out[f"{key}"] = coef
        out[f"{key}_roll"] = float("nan") if coef is None else _deg(coef[0])
        out[f"{key}_pitch"] = float("nan") if coef is None else _deg(coef[1])
        out[f"{key}_p50"], out[f"{key}_p95"], out[f"{key}_n"] = p50, p95, n
    return out


def _print_row(name: str, r: dict) -> None:
    def f(k):
        return f"{r[k]:+.2f}" if np.isfinite(r[k]) else "  失败"
    print(f"{name}  旧滚转 {f('old_roll')}° 新滚转 {f('new_roll')}°"
          f"  旧俯仰 {f('old_pitch')}° 新俯仰 {f('new_pitch')}°"
          f"  路点 {r['road_px']}"
          f"  平整p50/p95 旧 {r['old_p50']:.1f}/{r['old_p95']:.1f}cm"
          f" 新 {r['new_p50']:.1f}/{r['new_p95']:.1f}cm"
          f"  耗时 走 {r['t_road_ms']:.1f} 拟合 旧{r['t_old_ms']:.1f}/新{r['t_new_ms']:.1f}ms")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default=None, help="单帧 jpg 路径")
    ap.add_argument("--corpus", action="store_true", help="金标帧仓全量对比")
    args = ap.parse_args()
    assert args.frame or args.corpus, "须给 --frame 或 --corpus"

    sess = dg.load_session(DEPTH_MODEL_FILE)
    ego = dg.DepthRoadObserver._load_ego_mask()
    frames: list[tuple[str, np.ndarray]] = []
    if args.frame:
        fp = Path(args.frame)
        img = cv2.imread(str(fp))
        assert img is not None, f"读不到 {fp}"
        frames.append((f"{fp.parent.parent.name}_{fp.stem}",
                       cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
    else:
        for r in csv.DictReader(GOLD.open(encoding="utf-8")):
            p = Path(r["path"])
            if not p.exists():
                print(f"[跳过] 帧缺失 {p.name}")
                continue
            img = cv2.imread(str(p))
            if img is None:
                print(f"[跳过] 读不出 {p.name}")
                continue
            frames.append((p.stem, cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))

    for name, rgb in frames:
        pts, fx, fy = dg.infer_points(sess, rgb)
        r = compare_frame(pts, fx, fy, ego)
        _print_row(name, r)


if __name__ == "__main__":
    main()
