# -*- coding: utf-8 -*-
"""四闸多会话对比：xl 覆盖、擦边预判触发与方向、振荡闸接管、风暴重叠。

用法::

    python tools/experiments/world_state/gate_compare.py \
        20261102_110128 20261102_115653 20261102_125414
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter
from pathlib import Path


def load(session: str) -> list[dict]:
    p = (Path(os.environ.get("APPDATA", str(Path.home())))
         / "MaaRacingMaster" / "logs" / session / "trace" / "speedrush.jsonl")
    return [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]


def ts_of(r: dict) -> float:
    for k in ("ts_mono_ns", "ts_wall_ns", "ts_ns"):
        v = r.get(k)
        if v is not None:
            return v / 1e9
    return 0.0


def main(argv=None) -> int:
    sessions = argv or ["20261102_110128", "20261102_115653",
                        "20261102_125414"]
    for ses in sessions:
        rows = [r for r in load(ses) if r.get("ts_mono_ns")]
        ts = [ts_of(r) for r in rows]
        span = ts[-1] - ts[0]
        print(f"\n== {ses}  {len(rows)} 拍  {span:.1f}s")

        # xl 覆盖
        xl = sum(1 for r in rows
                 if any(isinstance(c.get("xl"), (int, float))
                        for c in (r.get("car_pos") or [])))
        print(f"car_pos.xl 非空拍: {xl} ({xl / len(rows):.1%})")

        # 擦边预判：触发数 + 方向（车道中心 = round(target) 框架，车行 [x,t]）
        pp = 0
        dir_ok = dir_bad = 0
        for r in rows:
            if r.get("sel_why") != "center:pp":
                continue
            pp += 1
            sd = r.get("sel_dbg") or {}
            t = sd.get("target")
            ego = sd.get("ego")
            if t is None or ego is None:
                continue
            center = round(t)
            qual = [c for c in (sd.get("car") or [])
                    if isinstance(c, (list, tuple)) and len(c) >= 2
                    and c[1] is not None and 0 < c[1] <= 3.0
                    and 0.54 < abs(c[0] - ego) <= 1.5]
            if not qual:
                continue
            dsign = 1 if qual[0][0] - ego > 0 else -1
            if (t - center) * dsign < 0:
                dir_ok += 1
            else:
                dir_bad += 1
        print(f"sel_why=center:pp: {pp} 拍 ({pp / span:.2f}/s)"
              f"  背车侧 {dir_ok} / 同车侧 {dir_bad}")

        # 振荡闸
        osc_a = 0
        osc_times = []
        for r in rows:
            cd = r.get("lat_ctrl") or {}
            if cd.get("osc_a"):
                osc_a += 1
                osc_times.append(ts_of(r))
        print(f"lat_ctrl osc_a True: {osc_a} 拍 ({osc_a / span:.2f}/s)")

        # 风暴与振荡闸重叠
        flips = [i for i in range(1, len(rows))
                 if isinstance(rows[i - 1].get("steer_norm"), (int, float))
                 and isinstance(rows[i].get("steer_norm"), (int, float))
                 and rows[i - 1]["steer_norm"] * rows[i]["steer_norm"] < 0]
        seg = []
        for i in flips:
            cnt = sum(1 for j in flips if 0 <= ts[i] - ts[j] <= 1.0)
            if cnt >= 4:
                if seg and ts[i] - ts[seg[-1][1]] < 2.0:
                    seg[-1] = (seg[-1][0], i)
                else:
                    seg.append((i, i))
        overlap = 0
        for a, b in seg:
            if any(ts[a] - 1.0 <= ot <= ts[b] + 1.0 for ot in osc_times):
                overlap += 1
        rate = len(seg) / span
        print(f"风暴段: {len(seg)} ({rate:.3f}/s)  与振荡闸激活±1s重叠: "
              f"{overlap}/{len(seg)}")

        # 风暴构成：每段看主导状态（并线中 / x_target 翻面 / plan 失效）
        comp = Counter()
        for a, b in seg:
            osc_hit = any(ts[a] - 1.0 <= ot <= ts[b] + 1.0
                          for ot in osc_times)
            merging = any((rows[j].get("route") or {}).get("vd")
                          in ("merge_start", "keep") and (rows[j].get(
                              "route") or {}).get("why") == "merging"
                          for j in range(max(a - 5, 0), min(b + 5, len(rows))))
            # x_target 在段内翻面次数（符号翻转且幅值 >0.3）
            xt_flips = 0
            for j in range(max(a - 2, 1), min(b + 2, len(rows))):
                x1, x2 = rows[j - 1].get("x_target"), rows[j].get("x_target")
                if isinstance(x1, (int, float)) and isinstance(x2, (int, float)) \
                        and x1 * x2 < 0 and abs(x1) > 0.3 and abs(x2) > 0.3:
                    xt_flips += 1
            if merging:
                comp["并线中"] += 1
            elif xt_flips >= 2:
                comp["x_target翻面"] += 1
            elif osc_hit:
                comp["振荡闸接管中"] += 1
            else:
                comp["其他"] += 1
        print(f"  风暴构成: {dict(comp)}")

        # 并线漏斗
        vd = Counter((r.get("route") or {}).get("vd") for r in rows)
        ms, cc = vd.get("merge_start", 0), vd.get("cancel", 0)
        if ms:
            print(f"并线: start {ms}  cancel {cc}  撤销率 {cc / ms:.0%}")

        # 逐段（按 hud reset 分局）
        bounds = [0]
        prev_s = None
        for i, r in enumerate(rows):
            s = r.get("hud_score")
            if isinstance(s, (int, float)):
                if prev_s is not None and s < 100 <= prev_s:
                    bounds.append(i)
                prev_s = s
        bounds.append(len(rows))
        for k in range(len(bounds) - 1):
            a, b = bounds[k], bounds[k + 1]
            if b - a < 50:
                continue
            sub = [rows[j] for j in range(a, b)]
            st = sum(1 for i in flips if a <= i < b)
            print(f"  段{k}: 行{a}-{b} {ts[b-1]-ts[a]:.0f}s 风暴{st} "
                  f"({st / max(ts[b-1]-ts[a], 1):.3f}/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
