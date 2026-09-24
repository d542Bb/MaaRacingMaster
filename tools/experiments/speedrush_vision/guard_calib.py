"""射线收敛守卫阈值标定：da_q20 块的内沿几何统计（122 帧，2026-09-24）。

**为什么有这个文件**：tri_exam 裁决后架构落地需要把两件守卫装进生产 region：
射线收敛（边界线过 VP；杀 518 R1062 出租车伪块、kerb 001160 R 远带背景块）与
侧别一致（内沿近带读数须在自己侧）。阈值不拍脑袋——本脚本在 tri_exam 同卷
（金标 54 + 骑缘窗 + 贴墙窗）上，对每帧每侧选中块算：

  - 行数 n、y 跨度；
  - 两轮稳健拟合 x(y)（首轮 polyfit 后剔 |残差|>30px 再拟合）；
  - 外推 x(Y_H)（Y_H=324.2，gate0 冻结）与收敛偏移 |x(Y_H)−VPX|；
  - 中位 |残差|；
  - 近带 [550,700] 内沿中位 x（px）与 x_lane（A1 尺子）。

已知伪块锚点（wallhit/q20rescue 取证）：518 R=出租车块（收敛偏移应大）、
kerb 001160 R=远带背景块；真块锚点：518 L 内沿 472→384 直线（应收敛）。
用法：python tools/experiments/speedrush_vision/guard_calib.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import probe_depth as pd  # noqa: E402
import probe_step_detect as psd  # noqa: E402
from firstgate_score import _blocks_with_g, _g_q20, gold_rows  # noqa: E402
from tri_exam import DEMOS, da_depth  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

OUT = psd.OUT
Y_H, VPX = pd.Y_H, pd.VPX
NEAR_LO, NEAR_HI = 550, 700          # 近带读数窗（生产读数行口径）
FIT_RESID = 30.0                     # 稳健拟合剔点门（px）


def fit_stats(inner: dict[int, int]) -> dict:
    ys = np.array(sorted(inner), float)
    xs = np.array([inner[int(y)] for y in ys], float)
    if len(ys) < 20:
        return {"n": len(ys), "ymin": int(ys.min()) if len(ys) else -1,
                "ymax": int(ys.max()) if len(ys) else -1}
    sl, ic = np.polyfit(ys, xs, 1)
    keep = np.abs(xs - (sl * ys + ic)) <= FIT_RESID
    if keep.sum() >= 20:
        sl, ic = np.polyfit(ys[keep], xs[keep], 1)
    resid = np.median(np.abs(xs - (sl * ys + ic)))
    x_yh = sl * Y_H + ic
    near = ys[(ys >= NEAR_LO) & (ys <= NEAR_HI)]
    med_near = float(np.median([inner[int(y)] for y in near])) if len(near) else np.nan
    return {"n": len(ys), "ymin": int(ys.min()), "ymax": int(ys.max()),
            "slope": float(sl), "resid": float(resid), "x_yh": float(x_yh),
            "conv": abs(float(x_yh) - VPX), "x_near": med_near}


def main() -> None:
    cal = load_calib()
    sess = psd._folded_sess(518)
    recs: list[dict] = []
    frames: list[tuple[str, Path]] = [(Path(r["path"]).parent.name + "__"
                                       + Path(r["path"]).stem, Path(r["path"]))
                                      for r in gold_rows()]
    for wname, sname, lo, hi in (("straddle", "20260922_113932_p1", 500, 536),
                                 ("wallhug", "20260922_113610_p2", 422, 452)):
        for fid in range(lo, hi + 1):
            p = DEMOS / sname / "frames" / f"{fid:06d}.jpg"
            if p.exists():
                frames.append((f"{wname}:{fid}", p))
    for tag, p in frames:
        m = da_depth(p, sess)
        rng = psd.road_range(m)
        bl = _blocks_with_g(m, rng, _g_q20)
        for side in ("L", "R"):
            blk = bl[side]
            rec = {"tag": tag, "side": side, "present": blk is not None}
            if blk is not None:
                st = fit_stats(blk[0])
                rec.update(st)
                if np.isfinite(st.get("x_near", np.nan)):
                    rec["lane_near"] = x_lane_of(int(round(st["x_near"])),
                                                 625, cal)   # 近带中位行的代表 y
            recs.append(rec)
    out_csv = OUT / "guard_calib.csv"
    cols = ["tag", "side", "present", "n", "ymin", "ymax", "slope", "resid",
            "x_yh", "conv", "x_near", "lane_near"]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(recs)
    print(f"[CSV] {out_csv}（{len(recs)} 条）")
    det = [r for r in recs if r["present"]]
    for side in ("L", "R"):
        d = [r for r in det if r["side"] == side and "conv" in r]
        if not d:
            continue
        conv = np.array([r["conv"] for r in d])
        res = np.array([r["resid"] for r in d])
        lane = np.array([r["lane_near"] for r in d if "lane_near" in r])
        q = np.quantile(conv, [0.5, 0.9, 0.95, 1.0])
        qr = np.quantile(res, [0.5, 0.9, 0.95, 1.0])
        sign = (lane < 0).mean() if side == "L" else (lane > 0).mean()
        print(f"\n== {side} 在场 {len(d)}（含伪块，看分位数选阈值）==")
        print(f"  收敛偏移 |x(Y_H)−VPX| px：p50={q[0]:.0f} p90={q[1]:.0f} "
              f"p95={q[2]:.0f} max={q[3]:.0f}")
        print(f"  拟合中位残差 px：       p50={qr[0]:.1f} p90={qr[1]:.1f} "
              f"p95={qr[2]:.1f} max={qr[3]:.1f}")
        print(f"  近带 x_lane 侧别正确率：{sign:.1%}（lane p50="
              f"{np.median(lane):+.2f}）")
    print("\n== 锚点帧读数（守卫必须分开它们）==")
    for want in ("straddle:518", "frames__000437", "wallhug:437"):
        for r in recs:
            if want in r["tag"] and r["present"]:
                print(f"  {r['tag']} {r['side']}: n={r['n']} y[{r['ymin']},{r['ymax']}]"
                      + (f" conv={r['conv']:.0f} resid={r['resid']:.1f} "
                         f"lane_near={r.get('lane_near', float('nan')):+.2f}"
                         if "conv" in r else "  <20 行"))


if __name__ == "__main__":
    main()
