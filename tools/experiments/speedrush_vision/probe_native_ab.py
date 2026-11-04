# -*- coding: utf-8 -*-
"""NATIVE_GEO（几何块直吃原生点图）的同帧交替 A/B。

**为什么重做**（2026-10-17 第一次实验的两个方法缺陷）：

1. **顺序执行**：先跑完 A 臂 20 帧再跑 B 臂，B 臂吃满热缓存（forward 的权重
   驻留、OpenCV 线程池、页缓存），系统性偏快。实测 A 组第一帧 forward 比稳态
   慢 5~10ms，20 帧的中位数仍受影响。
2. **跨局比较**：离线帧取旧局、生产验证取新局，两批路面复杂度不同（「无穿越」
   拒绝率 21.4% vs 12.6%），把局间差异读成了改动效果。

本探针改为：**同一帧紧邻跑两臂**（A→B→A→B…），配对差即同帧同时刻的净差；
帧源用生产验证局，与实机同路面。两臂的 ORT binding 形状完全相同（模型输入
恒为 720×1280、输出恒为 336×598，`native` 只影响后处理是否上采样），切换不触发
binding 重建，无额外开销偏置。

**预登记判据**（先写后跑）——配对差口径：

1. observe 配对差 P50 ≤ −10ms（改动有效）；
2. 分段配对差：`upsize` ≤ −4ms，且 `plane`/`edges`/`pre` 均 ≥ −1ms
   （几何块不得变慢——上次跨局读到的「各慢 1ms」要在这里证伪或证实）；
3. `sides` 同判率 ≥ 95%；
4. 平面质量门拒绝次数：两臂差值 ≤ 总帧数的 1%。

四条全满足 → 改动有效且无质量退；任一条不满足 → 保持关闭。

用法：``.venv\\Scripts\\python.exe tools/experiments/speedrush_vision/probe_native_ab.py``
环境变量 ``SR_FRAMES``（逗号分隔局名）、``SR_N``（每局帧数，缺省 20）。"""
from __future__ import annotations

import glob
import os
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import maaracing_master.plugins.speedrush.depth_geo as D    # noqa: E402
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

RUNS = os.environ.get("SR_FRAMES", "20261017_154531_p1,20261017_154616_p2")
N = int(os.environ.get("SR_N", "20"))
SEG = ("pre", "forward", "reconstruct", "upsize", "plane", "edges", "grid")


def _frames() -> list:
    base = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
    out = []
    for run in [s for s in RUNS.split(",") if s]:
        d = (base / run).resolve()
        if base.resolve() not in d.parents:
            raise ValueError(f"路径越界：{run}")
        paths = sorted(glob.glob(str(d / "depth_debug" / "*_frame.jpg")))[:N]
        out += [(run, cv2.cvtColor(cv2.imread(p), cv2.COLOR_BGR2RGB))
                for p in paths]
    return out


def _p(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def main() -> None:
    frames = _frames()
    cal = load_calib()
    obs = D.DepthRoadObserver(D.load_session(DEPTH_MODEL_FILE), cal)
    # 预热两臂各一次（各自走一遍路径，进稳态后再计时）
    for arm in (False, True):
        D.NATIVE_GEO = arm
        obs.observe_debug(frames[0][1], fid=0, frame_ts_ns=None)
    print(f"帧 {len(frames)}（{RUNS}）  同帧紧邻交替 A/B")

    rows = {False: [], True: []}
    for i, (run, rgb) in enumerate(frames):
        for arm in (False, True):
            D.NATIVE_GEO = arm
            t = time.perf_counter()
            r, _ev = obs.observe_debug(rgb, fid=i + 1, frame_ts_ns=None)
            dt = (time.perf_counter() - t) * 1e3
            rows[arm].append({"run": run, "observe": dt, "rd": r,
                              "st": dict(D.LAST_STAGE_MS)})
    D.NATIVE_GEO = False   # 复位到关闭态（生产默认）

    for arm, label in ((False, "旧路径(upsize+GEO_DS)"), (True, "原生直吃")):
        rs = rows[arm]
        print(f"\n{label}: observe P50={_p([x['observe'] for x in rs], .5):5.1f} "
              f"P90={_p([x['observe'] for x in rs], .9):5.1f} | "
              + " ".join(f"{k}={_p([x['st'].get(k) for x in rs], .5):.1f}" for k in SEG))

    a, b = rows[False], rows[True]
    print("\n=== 同帧配对差（原生 − 旧路径，负=更快）===")
    d_obs = [y["observe"] - x["observe"] for x, y in zip(a, b)]
    print(f"  observe  P50={_p(d_obs, .5):+.1f}  P10={_p(d_obs, .1):+.1f}  "
          f"P90={_p(d_obs, .9):+.1f} ms")
    for k in SEG:
        d = [y["st"].get(k) - x["st"].get(k) for x, y in zip(a, b)
             if x["st"].get(k) is not None and y["st"].get(k) is not None]
        if d:
            print(f"  {k:12s} P50={_p(d, .5):+6.2f}  P90={_p(d, .9):+6.2f} ms")

    same = sum(1 for x, y in zip(a, b) if x["rd"].sides == y["rd"].sides)
    dl = [abs(x["rd"].left_edge_lane - y["rd"].left_edge_lane)
          for x, y in zip(a, b)
          if x["rd"].left_edge_lane is not None
          and y["rd"].left_edge_lane is not None]
    dr = [abs(x["rd"].right_edge_lane - y["rd"].right_edge_lane)
          for x, y in zip(a, b)
          if x["rd"].right_edge_lane is not None
          and y["rd"].right_edge_lane is not None]
    edge = dl + dr

    def _gate(rs):
        return sum(1 for x in rs
                   if any("平面质量门" in s for s in x["rd"].rejects))

    ga, gb = _gate(a), _gate(b)
    print(f"\n  sides 同判 {same}/{len(a)} | 边缘差 P50={_p(edge, .5):.3f} "
          f"P90={_p(edge, .9):.3f} lane")
    print(f"  平面质量门拒绝：旧 {ga} / 原生 {gb}（差 {gb - ga}，门槛 ≤1%×{len(a)}"
          f"={0.01 * len(a):.1f}）")
    print(f"  edge_pts 箱数 P50：旧 {_p([len(x['rd'].edge_pts) for x in a], .5):.0f} "
          f"/ 原生 {_p([len(x['rd'].edge_pts) for x in b], .5):.0f}")

    # 判据 2 的方向：配对差 = 原生 − 旧，负值表示原生更快。要求「几何块不得
    # 变慢」即差 ≤ +1ms（不是 ≥ −1——那会把「更快」判成失败）。
    ok = (_p(d_obs, .5) <= -10.0
          and _p([y["st"].get("upsize", 0) - x["st"].get("upsize", 0)
                  for x, y in zip(a, b)], .5) <= -4.0
          and all(_p([y["st"].get(k, 0) - x["st"].get(k, 0)
                      for x, y in zip(a, b)], .5) <= 1.0
                  for k in ("plane", "edges", "pre"))
          and same / len(a) >= 0.95
          and (gb - ga) <= 0.01 * len(a))
    print(f"\n  预登记四条件 → {'全部满足（改动有效且无质量退）' if ok else '未全满足（保持关闭）'}")


if __name__ == "__main__":
    main()
