# -*- coding: utf-8 -*-
"""线主导横位的只读影子：用标线连续跟踪自车横位，深度只定一次锚。

**只读**：只读 trace，不写生产数据、不改控制。

## 口径

- 每条锁定线给出横向偏移 `L`（道，相对相机轴）；车道边界在固定网格上，
  自车横位 `e` 满足 `L + e ≈ 整数`。用线的残差拟合出 `e` 的小数部分
  （圆周中位），再按相邻拍连续性展开成整数道号。
- 深度只做一次锚：在第一个「来源=干净墙边 且 线锁定」的拍取
  `C = road_offset - e`；此后 `x = C + e`。线缺失/重锁的拍不产出估计。
- 与现状对比：`fd = (x - executed) * lane_w`；生产门槛 `|fd| > 1.0m` 即
  计划失效（`plan:frame_shift`）。影子统计：
  1. 失效拍里 `|fd_est| <= 1.0` 的比例（能救回多少）；
  2. 生产有效但影子失效（新增多少）；
  3. `|x_est - road_offset|` 分布（目标会挪多少）。
- 不重放规划器/选择器：只回答「横位参考换成线以后，失效量级与目标偏移」。
  目标是估计量，不是控制改动。

用法：
    .venv/Scripts/python.exe tools/experiments/lane_depth_fusion/probe_fusion.py \
        --tokens 20261029_133951,20261029_140039 [--json out.json]
    ... --selftest
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

LANE_W = 3.38
HOLD_M = 1.0


def _circ_median(vals: list[float]) -> float | None:
    """[-0.5, 0.5) 上的圆周中位：相对首值展开后取中位，再折回。"""
    if not vals:
        return None
    ref = vals[0]
    flat = [v + round(ref - v) for v in vals]
    flat.sort()
    n = len(flat)
    m = flat[n // 2] if n % 2 else 0.5 * (flat[n // 2 - 1] + flat[n // 2])
    while m >= 0.5:
        m -= 1.0
    while m < -0.5:
        m += 1.0
    return m


def _e_frac(lines: list[dict]) -> float | None:
    vals = []
    for ln in lines:
        if not ln.get("accepted") or ln.get("L") is None:
            continue
        L = float(ln["L"])
        vals.append(-(L - round(L)))
    return _circ_median(vals)


def _quantile(xs: list[float], q: float) -> float | None:
    ys = sorted(xs)
    if not ys:
        return None
    k = (len(ys) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (k - lo)


def run(rows: list[dict]) -> dict:
    # 纯标线展开（不锚道号）在实测里会漂 6~17 道：整数道号必须由深度锚定。
    # 影子估计改用生产线观测的横位（道号已由深度锚定），只检验连续量。
    out = {
        "ticks": 0, "est_ticks": 0,
        "dx_exec": [], "dro_exec": [], "dx_ro": [],
        "fs": 0, "rescue": 0, "new_bad": 0,
        "jumps_est": 0, "jumps_ro": 0,
        "anchor_src": None, "anchor_df": None,
    }
    prev_x = None
    prev_ro = None
    for r in rows:
        if r.get("type") is not None:
            continue
        dbg = r.get("sel_dbg")
        if not dbg:
            continue
        out["ticks"] += 1
        ro = r.get("road_offset")
        ex = r.get("executed_lane")
        lo = r.get("lane_obs") or {}
        x_est = None
        if lo.get("state") == "TRACK" and lo.get("x") is not None:
            x_est = float(lo["x"])
        if x_est is not None:
            out["est_ticks"] += 1
            if prev_x is not None and abs(x_est - prev_x) > 0.5:
                out["jumps_est"] += 1
            prev_x = x_est
            if ex is not None:
                out["dx_exec"].append(abs(x_est - ex))
            if ro is not None:
                out["dx_ro"].append(abs(x_est - ro))
        if ro is not None:
            if prev_ro is not None and abs(ro - prev_ro) > 0.5:
                out["jumps_ro"] += 1
            prev_ro = ro
            if ex is not None:
                out["dro_exec"].append(abs(ro - ex))
        pl = r.get("plan") or {}
        fd = pl.get("fd")
        if fd is None or x_est is None or ex is None:
            continue
        fd_est = (x_est - ex) * LANE_W
        fs = pl.get("reason") == "plan:frame_shift"
        if fs:
            out["fs"] += 1
            if abs(fd_est) <= HOLD_M:
                out["rescue"] += 1
        elif pl.get("valid") and abs(fd_est) > HOLD_M and abs(fd) <= HOLD_M:
            out["new_bad"] += 1
    return out


def _fmt(x, nd=2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _report(name: str, s: dict) -> None:
    n = s["est_ticks"]
    print(f"\n== {name} ==")
    print(f"  估计覆盖 {n}/{s['ticks']} 拍（{100 * n / max(1, s['ticks']):.0f}%）"
          f"（线锁定拍才有估计）")
    for key, label in (("dx_exec", "|线-执行|"), ("dro_exec", "|路心-执行|"),
                       ("dx_ro", "|线-路心|")):
        xs = s[key]
        print(f"  {label} 中位 {_fmt(_quantile(xs, .5))}  "
              f"p90 {_fmt(_quantile(xs, .9))}  道")
    print(f"  深度跳变 {s['jumps_ro']} 次 / 线估计跳变 {s['jumps_est']} 次")
    print(f"  生产失效拍 {s['fs']}；影子门槛内可救 {s['rescue']}"
          f"（{100 * s['rescue'] / max(1, s['fs']):.0f}%）；新增失效 {s['new_bad']}")


def _load(root: Path, token: str) -> list[dict]:
    p = root / token / "trace" / "speedrush.jsonl"
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def selftest() -> None:
    # 线在整数边界上（自车居中）：L=±0.5 → e=±0.5 的圆周中位为 0.5 或 -0.5
    lines = [{"L": -0.5, "accepted": True}, {"L": 0.5, "accepted": True}]
    e = _e_frac(lines)
    assert e is not None and abs(abs(e) - 0.5) < 1e-9, e
    # 自车右移 0.2 道：线相对左移 0.2
    lines2 = [{"L": -0.7, "accepted": True}, {"L": 0.3, "accepted": True}]
    e2 = _e_frac(lines2)
    assert e2 is not None and abs(e2 - e) > 0.15
    # 圆周中位跨 ±0.5 不应取平均到 0
    assert abs(_circ_median([0.49, -0.49]) + 0.5) < 1e-9
    print("selftest ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="logs 根（默认 %%APPDATA%%/MaaRacingMaster/logs）")
    ap.add_argument("--tokens", default="")
    ap.add_argument("--json", default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    root = Path(args.root) if args.root else (
        Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "logs")
    tokens = [t for t in args.tokens.split(",") if t]
    if not tokens:
        raise SystemExit("需要 --tokens")
    all_out = {}
    for tok in tokens:
        s = run(_load(root, tok))
        all_out[tok] = {k: v for k, v in s.items() if not isinstance(v, list)}
        all_out[tok]["quantiles"] = {
            key: [_quantile(s[key], q) for q in (0.5, 0.9)]
            for key in ("dx_exec", "dro_exec", "dx_ro")}
        _report(tok, s)
    if args.json:
        Path(args.json).write_text(json.dumps(all_out, ensure_ascii=False,
                                              indent=2), encoding="utf-8")
        print(f"\nJSON：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
