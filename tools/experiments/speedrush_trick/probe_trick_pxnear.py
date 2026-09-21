"""④ 贴近重测（动作序④，A1 尺子定档后的第一个下游使用）：透视归一的横向距离。

**背景**：首轮「极限 vs 普通超车分离在横向贴近」（min_gap AUC 置换 p=0.003）同日
**撤回**——锚挪塌 AUC、min 取在远处靠消失点一拍、cx 双峰三证指向那是**透视收敛假象**
（README ②）。预注册留话「留作透视归一后第一件要重看的事」（README :94）。标定实验
「A1 定档轮」（2026-09-21）落档尺子后开工本条。

**预注册判据（跑前写死）**：
  特征 pxn_min = 近距帧（by>380，与首轮同域）上 |ΔX_norm| 的最小值，
    ΔX_norm(t) = (cx − vpx)/(by − y_h) − (X_ego)，X_ego = (640 − vpx)/(v_ego − y_h)
    ——纯归一量（车道/A_x 单位）；v_ego 取 {716, 680} 两档做敏感性（自车接地点行，
    相机钉 640 系 trick 硬事实）。
  主检验 = v_ego=716 档的 AUC + 置换 p（与首轮同法，rng(3)、2000 次、双侧）；
  分离成立 = p<0.05 ∧ AUC<0.5（极限组更近）；两档 v_ego 须同向，否则记「不稳」。
  车道换算 A_x=0.587（来源=标定实验 globalL 冻结口径，symmetric 轨实证 ±0.9↔±0.5 车道，
  见 `97e5859` 与「A1 定档轮」）——只影响阈值数值，**不影响分离检验**（等比缩放）。
  结论分支：成立 → 以极限组 pxn 分位数报「多近」候选区间，供 RULES §7#2 回填（人工复核）；
  不成立 → 「升级与否」在此观测量上无信号，触发条件候选转向别处（速度差代理已被 RULES
  :411 记无信号），§7#2 判「现有框级感知不可判」结案转观察。

**常数真源**：vpx/y_h 逐场取 %TEMP%/sr_calib/analyze_<session>.json 在场行中位（Gate 0
仪器输出，仅读缓存不改它）；缺失即停并报，不猜常数。该缓存由**已收杆归档的标定实验**的
`probe_gate0_vp.py` 生成——溯源 commit [`db7a3fe`](https://github.com/d542Bb/MaaRacingMaster/commit/db7a3fe78973d9d155d350feac581d7530f5138a)，缓存失效时从该 commit 检出其实验目录物化后重跑（目录名见该 commit 文件清单），本探针不复制标定逻辑。

用法（仓库根，需先跑过首轮三通道缓存）：
    .venv\\Scripts\\python.exe tools\\experiments\\speedrush_trick\\probe_trick_pxnear.py <session>...
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_trick_trigger import (PAIR_HI, PAIR_LO, auc, banner_series, combo_flips,  # noqa: E402
                                 detections, track_passes)

CAL_CACHE = Path(os.environ.get("TEMP", ".")) / "sr_calib"
A_X = 0.587           # 冻结口径，见 docstring——仅用于报告单位，不入场检验
V_EGOS = (716.0, 680.0)


def scene_constants(session: str) -> tuple[float, float]:
    p = CAL_CACHE / f"analyze_{session}.json"
    if not p.exists():
        raise SystemExit(f"缺标定缓存 {p}——先在标定实验跑 probe_gate0_vp 该场")
    rows = json.loads(p.read_text(encoding="utf-8"))["rows"]
    vpx = np.median([r["vpx"] for r in rows if r.get("vpx") is not None])
    y_h = np.median([r["y_h"] for r in rows if r.get("y_h") is not None])
    mad_v = np.median(np.abs([r["vpx"] for r in rows if r.get("vpx") is not None] - vpx))
    mad_h = np.median(np.abs([r["y_h"] for r in rows if r.get("y_h") is not None] - y_h))
    print(f"  {session}: vpx={vpx:.1f}(MAD {mad_v:.1f}) y_h={y_h:.1f}(MAD {mad_h:.1f})")
    return float(vpx), float(y_h)


def label_passes(sessions):
    ext, norm = [], []
    for s in sessions:
        P = track_passes(detections(s))
        flips = combo_flips(banner_series(s))
        used = set()
        for f in flips:
            cand = [(abs(p["t_end"] - f), i) for i, p in enumerate(P)
                    if PAIR_LO <= p["t_end"] - f <= PAIR_HI and i not in used]
            if cand:
                used.add(min(cand)[1])
        for i, p in enumerate(P):
            p["_ext"] = i in used
            p["_sess"] = s
        ext += [p for p in P if p["_ext"]]
        norm += [p for p in P if not p["_ext"]]
    return ext, norm


def pxn_min(p, vpx, y_h, v_ego) -> float:
    x_ego = (640.0 - vpx) / (v_ego - y_h)
    best = np.inf
    for _t, _cls, cx, by, _h in p["pts"]:
        if by > 380.0 and by > y_h + 1.0:
            best = min(best, abs((cx - vpx) / (by - y_h) - x_ego))
    return best


def separation(g_ext, g_norm):
    obs = auc(g_ext, g_norm)
    g = np.concatenate([g_ext, g_norm])
    lab = np.r_[np.ones(len(g_ext)), np.zeros(len(g_norm))]
    rng = np.random.default_rng(3)
    null = np.array([auc(g[sh == 1], g[sh == 0])
                     for sh in (rng.permutation(lab) for _ in range(2000))])
    p2 = 2 * min((null <= obs).mean(), (null >= obs).mean())
    return obs, float(p2)


if __name__ == "__main__":
    sessions = sys.argv[1:]
    if not sessions:
        raise SystemExit(__doc__)
    cals = {s: scene_constants(s) for s in sessions}
    ext, norm = label_passes(sessions)
    print(f"\n配对：极限 {len(ext)} / 普通 {len(norm)}")
    res = {}
    for v_ego in V_EGOS:
        ge = np.array([pxn_min(p, *cals[p["_sess"]], v_ego) for p in ext])
        gn = np.array([pxn_min(p, *cals[p["_sess"]], v_ego) for p in norm])
        obs, p2 = separation(ge, gn)
        res[v_ego] = (obs, p2)
        print(f"v_ego={v_ego:g}: 极限中位 {np.median(ge)*A_X:.3f} 车道 vs 普通 "
              f"{np.median(gn)*A_X:.3f} 车道  AUC {obs:.3f}  置换 p={p2:.3f}"
              f"  （极限组 q25/q75 = {np.quantile(ge,0.25)*A_X:.2f}/{np.quantile(ge,0.75)*A_X:.2f} 车道）")
    (o1, p1), (o2, p2) = res[V_EGOS[0]], res[V_EGOS[1]]
    verdict = ("成立" if (p1 < 0.05 and o1 < 0.5 and o2 < 0.5) else
               "不稳（两档方向/显著性不一致）" if (o1 < 0.5) != (o2 < 0.5) else "无信号")
    print(f"\n预注册判据 → 贴近分离「{verdict}」"
          f"（主检验 p={p1:.3f}，敏感性档同向={((o1<0.5)==(o2<0.5))}）")
