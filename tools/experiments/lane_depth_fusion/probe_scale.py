# -*- coding: utf-8 -*-
"""米制尺度只读复算：线间距 / 相机高度 / 宽度 / 速度口径。

只读 ``logs/<会话>/trace/speedrush.jsonl``，不跑模型、不改生产。用于回答
「控制道的米制尺度是否偏大、偏多少」，为重新标定提供可复现证据。

口径（全部来自 trace 同拍字段）：
- 相邻线间距：同一 fid 的近场拟合线（``y0>=550``，全幅行），按 ``L`` 排序取
  相邻差，0.7~1.6 道内的对计入；``L = slope*cam_h/LANE_W``，所以
  ``ΔL·LANE_W/cam_h = Δslope``（像素斜率差，与相机高度假设无关）。
- 金标车道 3.5 m（``baked_track_data.lua`` laneWidth=350）下，
  真实相机高度 ``h = 3.5/Δslope = 3.5·cam_h/(ΔL·LANE_W)``。
- 宽度：``scene.road.w``（米）、``ro_w``（道）、``dgeo_left/right``（道）——
  三者定义不同，只作交叉参照，不直接当墙距。
- 速度：``lat_ctrl.speed`` 等于默认常数 45.0 的拍占比；非默认值另报。
  它由线几何推出（``v_line*LANE_W/tan``），与线间距同尺度，不是独立尺子。
- 车高：``car_h.h``（框高像素）× ``dist.m``（米）/ ``dgeo_focal``（像素），
  中位车高用来做独立尺度旁证（需车型真值才能定论）。

用法：``.venv/Scripts/python.exe probe_scale.py [--root LOGS_ROOT]``
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
from pathlib import Path

LANE_W = 3.38          # gate0.json lane_w_m（当前生产值）
GOLD_LANE_M = 3.5      # 逆向金标 laneWidth=350cm
SPEED_DEFAULT = 45.0   # planner._HEAD_SPEED_DEFAULT_MPS


def _q(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    a = int(k)
    b = min(a + 1, len(xs) - 1)
    return xs[a] + (xs[b] - xs[a]) * (k - a)


def _f(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def _session(root: Path, token: str) -> dict | None:
    p = root / token / "trace" / "speedrush.jsonl"
    if not p.is_file():
        return None
    dl, dl_acc, dl_q = [], [], []
    cam, speed, roadw, ro_w, dspan, car_h = [], [], [], [], [], []
    for line in p.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        lo = r.get("lane_obs") or {}
        ch = lo.get("cam_h")
        if ch:
            cam.append(float(ch))
        c = r.get("lat_ctrl") or {}
        if c.get("speed") is not None:
            speed.append(float(c["speed"]))
        sc = r.get("scene")
        if isinstance(sc, dict):
            w = (sc.get("road") or {}).get("w")
            if isinstance(w, (int, float)):
                roadw.append(float(w))
        rw = r.get("ro_w")
        if isinstance(rw, (int, float)):
            ro_w.append(float(rw))
        left_edge, right_edge = r.get("dgeo_left"), r.get("dgeo_right")
        if isinstance(left_edge, (int, float)) \
                and isinstance(right_edge, (int, float)):
            dspan.append(float(right_edge) - float(left_edge))
        f = r.get("dgeo_focal")
        chs = {q["id"]: q for q in (r.get("car_h") or []) if q.get("h")}
        for d in (r.get("dist") or []):
            i = d.get("id")
            m = d.get("m")
            if i in chs and isinstance(m, (int, float)) and m > 3 and f:
                hpx = chs[i]["h"]
                if 10 < hpx < 400:
                    car_h.append(hpx * m / f)
        near = [x for x in (lo.get("lines") or [])
                if x.get("y0", 0) >= 550 and x.get("L") is not None]
        near.sort(key=lambda x: x["L"])
        for a, b in zip(near, near[1:]):
            d = b["L"] - a["L"]
            if 0.7 < d < 1.6:
                dl.append(d)
                if a.get("accepted") and b.get("accepted"):
                    dl_acc.append(d)
                if a.get("rms", 9) < 1.5 and b.get("rms", 9) < 1.5:
                    dl_q.append(d)
    if not dl:
        return None
    cam_med = st.median(cam) if cam else None
    h_imp = (GOLD_LANE_M * cam_med / (st.median(dl) * LANE_W)
             if cam_med else None)
    return {
        "token": token, "n": len(dl), "dl": st.median(dl),
        "dl_lo": _q(dl, .1), "dl_hi": _q(dl, .9),
        "dl_acc": st.median(dl_acc) if dl_acc else None,
        "dl_q": st.median(dl_q) if dl_q else None,
        "cam": cam_med, "h_imp": h_imp,
        "speed_n": len(speed),
        "speed_def": (sum(1 for x in speed if x == SPEED_DEFAULT) / len(speed)
                      if speed else None),
        "speed_nd": st.median([x for x in speed if x != SPEED_DEFAULT]) if any(
            x != SPEED_DEFAULT for x in speed) else None,
        "road_w": st.median(roadw) if roadw else None,
        "ro_w": st.median(ro_w) if ro_w else None,
        "dspan": st.median(dspan) if dspan else None,
        "car_h": st.median(car_h) if car_h else None,
        "car_n": len(car_h),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="logs 根（默认 %%APPDATA%%/MaaRacingMaster/logs）")
    args = ap.parse_args()
    root = Path(args.root) if args.root else (
        Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" / "logs")
    rows = []
    for d in sorted(root.iterdir()):
        s = _session(root, d.name)
        if s:
            rows.append(s)
    if not rows:
        print(f"没有可读 trace：{root}")
        return 1
    print(f"logs 根：{root}")
    print(f"口径：ΔL 为近场相邻线间距（控制道）；h_imp=3.5·cam_h/(ΔL·{LANE_W})")
    print()
    print("| 会话 | 线对 | ΔL 中位 | p10–p90 | 仅锁定 | rms<1.5 "
          "| cam_h | h_imp | 宽 road.w | 宽 ro_w/span | 速度默认 | 非默认速度 | 车高 |")
    print("| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: "
          "| --- | ---: | ---: | ---: |")
    for s in rows:
        print(f"| {s['token']} | {s['n']} | {_f(s['dl'], 3)} "
              f"| {_f(s['dl_lo'], 2)}–{_f(s['dl_hi'], 2)} "
              f"| {_f(s['dl_acc'], 3)} | {_f(s['dl_q'], 3)} "
              f"| {_f(s['cam'], 3)} | {_f(s['h_imp'], 3)} "
              f"| {_f(s['road_w'], 2)} "
              f"| {_f(s['ro_w'], 2)}/{_f(s['dspan'], 2)} "
              f"| {_f(s['speed_def'], 2)} | {_f(s['speed_nd'], 1)} "
              f"| {_f(s['car_h'], 2)} |")
    all_dl = [s["dl"] for s in rows]
    cams = [s["cam"] for s in rows if s["cam"]]
    hs = [s["h_imp"] for s in rows if s["h_imp"]]
    print()
    print(f"8 局汇总：ΔL 中位的中位 {_f(st.median(all_dl), 3)}；"
          f"cam_h 中位 {_f(st.median(cams), 3)}；"
          f"h_imp 中位 {_f(st.median(hs), 3)}")
    if cams and hs:
        print(f"深度平面 cam_h / 线几何 h_imp = "
              f"{_f(st.median(cams) / st.median(hs), 3)}")
    print("注意：ΔL 只用像素斜率差与同拍 cam_h，宽度的物理含义（画线/墙/可行驶面）"
          "需与逆向真值对齐后再解释。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
