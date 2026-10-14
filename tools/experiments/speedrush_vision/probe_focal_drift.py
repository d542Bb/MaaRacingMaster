# -*- coding: utf-8 -*-
"""焦距链路诊断探针（临时）：扫 3 局 depth_debug 包的 fx_norm/fy_norm/ms 与
掩码覆盖（求解器输入规模），出分布与帧间跳变统计。用完即清。"""
import json
from pathlib import Path
import numpy as np

ROOT = Path(r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\data\speedrush\control_traces")
RUNS = ["depth_debug_20261010_151742", "depth_debug_20261010_192334",
        "depth_debug_20261010_195509"]

for run in RUNS:
    d = ROOT / run
    stems = sorted(d.glob("d*_evid.npz"))
    rows = []
    for st in stems:
        z = np.load(str(st))
        fx, fy, ms = float(z["fx"]), float(z["fy"]), float(z["ms"])
        valid = np.unpackbits(z["valid"])
        n = int(z["valid_shape"][0] * z["valid_shape"][1])
        cov = valid.sum() / n
        # 求解器输入规模：64x64 masked nearest 的有效格（近似：1280x720 下采样比）
        rows.append((int(st.name.split("_")[0][1:]), fx, fy, ms, cov))
    rows.sort()
    fx = np.array([r[1] for r in rows])
    fy = np.array([r[2] for r in rows])
    ms = np.array([r[3] for r in rows])
    cov = np.array([r[4] for r in rows])
    print(f"\n=== {run}  ({len(rows)} 帧) ===")
    print(f"fx_norm  p5={np.percentile(fx,5):.3f} p50={np.percentile(fx,50):.3f} "
          f"p95={np.percentile(fx,95):.3f} min={fx.min():.3f} max={fx.max():.3f}")
    print(f"fy_norm  p50={np.percentile(fy,50):.3f}  fy/fx p50={np.percentile(fy/fx,50):.4f}")
    print(f"ms       p50={np.percentile(ms,50):.3f} min={ms.min():.3f} max={ms.max():.3f}")
    print(f"掩码覆盖 p50={np.percentile(cov,50):.3f} min={cov.min():.3f} max={cov.max():.3f}")
    j = np.abs(np.diff(fx))
    k = int(np.argmax(j))
    print(f"帧间 |Δfx| p50={np.percentile(j,50):.4f} p95={np.percentile(j,95):.4f} "
          f"max={j.max():.4f} (d{k}→d{k+1})")
    # 大跳变帧列表（|Δfx| > 0.03）
    big = [(rows[i][0], rows[i+1][0], float(j[i])) for i in np.where(j > 0.03)[0]]
    print(f"|Δfx|>0.03 跳变 {len(big)} 处: {big[:12]}")
    # fx 与 ms/覆盖的相关性（Spearman 秩相关，手算）
    def _spear(a, b):
        ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b))
        return float(np.corrcoef(ra, rb)[0, 1])
    print(f"spearman(fx,ms)={_spear(fx, ms):+.3f}  spearman(fx,cov)={_spear(fx, cov):+.3f}")
    # 极端帧清单（fx 最小/最大各 3 帧）
    lo = np.argsort(fx)[:3]; hi = np.argsort(fx)[-3:][::-1]
    for tag, idx in (("fx最小", lo), ("fx最大", hi)):
        for i in idx:
            print(f"  {tag} d{rows[i][0]:05d} fx={fx[i]:.3f}({fx[i]*1280:.0f}px) "
                  f"ms={ms[i]:.2f} cov={cov[i]:.3f}")

