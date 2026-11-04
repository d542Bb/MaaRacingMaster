# -*- coding: utf-8 -*-
"""横向指标的噪声估计（工作台，不入库）。

回答「两组 4 局的差值是改动还是车流噪声」：把每局按时间切成连续块
（默认 5 s），在每个会话内部按局分层、对块做有放回重采样，重复 B 次，
得到该会话指标的 95% 区间；两组各抽一次相减，得到差值区间。区间跨 0
即该差值落在噪声内。

口径（与 probe_lateral_ab 一致）：
- 墙边 = 线横位+bias 的 |x|>1.4；压墙 >1.8；只用 TRACK 拍。
- 带内 = |x−x_target| ≤ 0.35。
- 离缘 = scene.ego_m 到最近路缘的米数（几何口径，比线换算可信）。
- 状态滞后 = |road_offset−executed_lane| > 0.8（有路心读数拍）。
- 自举保留每局块数：同一会话的 4 局始终凑齐，跨组比较的是「同赛道集、
  不同车流」。

用法（仓库根，.venv）:
    .venv\\Scripts\\python.exe tools/experiments/merge_contract/probe_lateral_noise.py \
        --sessions 20261026_130014_1,20261026_143454_1,20261026_152046_1,20261026_155119_1
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys

import probe_lateral_state as P

BLOCK_S = 5.0
BOOT = 2000
SEED = 20261026
METRICS = ("wall", "hard", "band", "p10", "p15", "lag")
NAMES = {"wall": "墙边>1.4", "hard": "压墙>1.8", "band": "带内",
         "p10": "离缘<1.0m", "p15": "离缘<1.5m", "lag": "状态滞后"}


def tick_vals(row):
    lo = row.get("lane_obs") or {}
    lf = row.get("lane_fuse") or {}
    x = lo.get("x")
    # 与 probe_lateral_ab 同口径：有 x 即计（COAST/RELOCK 沿用上一锁值）
    line = None if x is None else float(x) + float(lf.get("bias") or 0.0)
    sc = row.get("scene") or {}
    road = sc.get("road") or {}
    ego, l, r = sc.get("ego_m"), road.get("l"), road.get("r")
    gap = None if (ego is None or l is None or r is None) \
        else min(abs(ego - l), abs(r - ego))
    xt, ex, ro = row.get("x_target"), row.get("executed_lane"), \
        row.get("road_offset")
    return {"t": row.get("ts_ns"), "line": line, "gap": gap,
            "dev": None if (xt is None or line is None) else abs(line - xt),
            "lag": None if (ro is None or ex is None) else abs(ro - ex)}


def metrics(ticks):
    def share(pred, key):
        vals = [v[key] for v in ticks if v[key] is not None]
        return sum(1 for v in vals if pred(v)) / max(1, len(vals))
    return {"wall": share(lambda v: abs(v) > 1.4, "line"),
            "hard": share(lambda v: abs(v) > 1.8, "line"),
            "band": share(lambda v: v <= 0.35, "dev"),
            "p10": share(lambda v: v < 1.0, "gap"),
            "p15": share(lambda v: v < 1.5, "gap"),
            "lag": share(lambda v: v > 0.8, "lag")}


def blocks_of(rows, block_s):
    out, cur, t0 = [], [], None
    for row in rows:
        t = row.get("ts_ns")
        if t0 is None:
            t0 = t
        cur.append(tick_vals(row))
        if (t - t0) / 1e9 >= block_s:
            out.append(cur)
            cur, t0 = [], t
    if cur:
        out.append(cur)
    return out


def boot_session(blocks, rng, n):
    out = []
    for _ in range(n):
        pool = []
        for game in blocks:
            k = len(game)
            for _ in range(k):
                pool.extend(game[rng.randrange(k)])
        out.append(metrics(pool))
    return out


def ci(vals):
    vals = sorted(vals)
    return vals[int(len(vals) * 0.025)], vals[int(len(vals) * 0.975)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", required=True,
                    help="逗号分隔的会话 token")
    ap.add_argument("--logs", default=P.DEFAULT_LOGS)
    ap.add_argument("--block-s", type=float, default=BLOCK_S)
    ap.add_argument("--boot", type=int, default=BOOT)
    ap.add_argument("--pairs", default="",
                    help="要比较的会话对，如 a:b,c:d；缺省相邻两两比较")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    tokens = [t.strip() for t in args.sessions.split(",") if t.strip()]
    games, boots = {}, {}
    for tok in tokens:
        gs = P.load_session(args.logs, tok)
        games[tok] = gs
        blocks = [blocks_of(rows, args.block_s) for _label, rows in gs]
        rng = random.Random(SEED)
        boots[tok] = boot_session(blocks, rng, args.boot)
    print(f"# 横向指标噪声（块自举：块 {BLOCK_S:.1f}s，B={args.boot}）\n")
    head = " | ".join(NAMES[m] for m in METRICS)
    print(f"| 会话 | 局数 | {head} |")
    print("|---|---|" + "---|" * len(METRICS))
    for tok in tokens:
        allt = [tick_vals(r) for _l, rows in games[tok] for r in rows]
        base = metrics(allt)
        cells = []
        for m in METRICS:
            lo, hi = ci([b[m] for b in boots[tok]])
            cells.append(f"{base[m] * 100:.1f} [{lo * 100:.1f},{hi * 100:.1f}]")
        print(f"| `{tok}` | {len(games[tok])} | " + " | ".join(cells) + " |")
    print("\n逐局（未自举）：")
    for tok in tokens:
        for label, rows in games[tok]:
            m = metrics([tick_vals(r) for r in rows])
            cells = " ".join(f"{NAMES[k]}={m[k] * 100:.1f}" for k in METRICS)
            print(f"- {label}: {cells}")
    if args.pairs:
        pairs = [tuple(x.split(":")) for x in args.pairs.split(",")]
    else:
        pairs = [(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)]
    print("\n## 组间差（95% 区间；跨 0 = 噪声内）\n")
    print(f"| 对比 | {head} |")
    print("|---|" + "---|" * len(METRICS))
    for a, b in pairs:
        cells = []
        for m in METRICS:
            d = sorted(x[m] - y[m] for x, y in zip(boots[b], boots[a]))
            lo, hi = ci(d)
            cells.append(f"{statistics.median(d) * 100:+.1f} "
                         f"[{lo * 100:+.1f},{hi * 100:+.1f}]")
        print(f"| {a} → {b} | " + " | ".join(cells) + " |")
    print("\n判据：4 局一组时，差值区间宽度的一半≈该指标的可检出下限；"
          "小于它不要归因于改动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
