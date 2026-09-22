"""K_v（杆值→横向速度增益）从历史录制场次离线反推（planner §七 C1，免实机）。

**要回答的问题**（C 类）：planner 的 `v_lat_gain` 现在拍脑袋 1.5，是"推满杆 → 几车道/秒"。
不标定它，任何横向动作都过冲或不到位——追币也好追车也好，先把执行器尺子立住。

**为什么不用强制打杆实机**（维护者 2026-09-22 指正）：持杆 2-3s 必上墙、且撞墙污染读数。
历史场次里人驾本就打了杆（pads.jsonl 有全幅 lx），帧里路缘在动——(输入,输出) 对现成的，
零新跑、零撞墙。

**方法**（单条边即可，维护者点破"sides 不是门槛"）：
- 直道上，一条路缘是过消失点的定线 → 其 x_lane（固定扫描带上沿行）只随**自车横移**变化：
  d(edge_x_lane)/dt = −v_lat_ego。弯道会让它漂，故只取 straight_residual 低的段。
- 取 |杆值| 持稳 ≥HOLD_S 的段（人变道激励），段内对跟踪到的那条边 x_lane 线性回归 → 斜率
  → v_lat = −slope；K_v_inst = v_lat / mean_stick_norm（同号才计，反向=误检/回正段，弃）。
- 跨场跨事件池化，报中位/分位/样本数。x_lane 是归一单位（非游戏车道，a_x 有尺度误差），
  K_v 单位=「x_lane 单位/秒 每 满杆」——planner 吃的就是这个单位，尺度自洽。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_kv_from_history.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.boundary import (  # noqa: E402
    BAND_TOP_OFF, detect_boundary)
from maaracing_master.plugins.speedrush.world_model import (  # noqa: E402
    load_calib, x_lane_of)

DEMOS = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush" / "demos"
CAL = load_calib()
TOP_Y = int(CAL.y_h) + BAND_TOP_OFF

STICK_MIN = 0.25       # 计入激励的最小 |杆值|（归一）
HOLD_S = 0.4           # 持稳最短时长（够量到稳态速度，又短到弯道漂可忽略）
RESID_MAX = 0.15       # 直道门：段内路缘 x_lane 跨行散布超此当弯道弃
STICK_PLATEAU_TOL = 0.18   # 段内杆值波动容差（"持稳"的稳）


def _edge_xlane(img_bgr) -> tuple[float | None, float]:
    """一帧：取稳定跟踪的那条边（左优先，缺则右）在带上沿的 x_lane + 该帧残差。"""
    s = detect_boundary(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB), CAL)
    if not s.validity:
        return None, s.straight_residual
    px = s.left_x if not np.isnan(s.left_x) else s.right_x
    if np.isnan(px):
        return None, s.straight_residual
    return x_lane_of(int(round(px)), TOP_Y, CAL), s.straight_residual


def _events(pads: list[dict]):
    """从 pads 提持稳段：返回 (t_start, t_end, mean_stick_norm_signed)。"""
    t0 = pads[0]["ts_ns"]
    ts = np.array([(p["ts_ns"] - t0) / 1e9 for p in pads])
    lx = np.array([p["lx"] / 32767.0 for p in pads])
    out = []
    i, n = 0, len(pads)
    while i < n:
        if abs(lx[i]) < STICK_MIN:
            i += 1
            continue
        j = i
        while j < n and abs(lx[j]) >= STICK_MIN:
            j += 1
        seg = slice(i, j)
        if ts[j - 1] - ts[i] >= HOLD_S and lx[seg].std() <= STICK_PLATEAU_TOL:
            out.append((ts[i], ts[j - 1], float(np.mean(lx[seg]))))
        i = j
    return out


def _measure_session(sess: Path):
    pads_p, frames_p = sess / "pads.jsonl", sess / "frames.jsonl"
    if not (pads_p.is_file() and frames_p.is_file()):
        return []
    pads = [json.loads(x) for x in pads_p.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not pads:
        return []
    t0 = pads[0]["ts_ns"]
    frames = [json.loads(x) for x in frames_p.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not frames:
        return []
    ft = np.array([(f["ts_ns"] - t0) / 1e9 for f in frames])
    rows = []
    for t_a, t_b, stick in _events(pads):
        idx = np.where((ft >= t_a) & (ft <= t_b))[0]
        if idx.size < 4:
            continue
        xl, res, tt = [], [], []
        for k in idx:
            img = cv2.imread(str(sess / "frames" / frames[k]["file"]))
            if img is None:
                continue
            v, r = _edge_xlane(img)
            if v is not None:
                xl.append(v)
                res.append(r)
                tt.append(ft[k])
        if len(xl) < 4 or np.median(res) > RESID_MAX:
            continue
        slope = np.polyfit(np.asarray(tt), np.asarray(xl), 1)[0]
        v_lat = -slope                       # 边 x_lane 降 → 自车右移（正）
        if v_lat * stick <= 0:               # 反向：回正/误检段，弃
            continue
        rows.append({"sess": sess.name, "dur": round(t_b - t_a, 2),
                     "stick": round(stick, 3), "v_lat": round(v_lat, 3),
                     "kv": round(v_lat / stick, 3)})
    return rows


def main() -> None:
    all_rows = []
    for sess in sorted(d for d in DEMOS.iterdir() if (d / "frames.jsonl").is_file()):
        r = _measure_session(sess)
        all_rows += r
    if not all_rows:
        print("无可用事件（历史场次缺 pads 或直道持稳段不足）")
        return
    for x in all_rows:
        print(f"  {x['sess']:>22} dur={x['dur']:.2f}s stick={x['stick']:+.2f} "
              f"v_lat={x['v_lat']:+.2f} K_v={x['kv']:+.2f}")
    kv = np.array([x["kv"] for x in all_rows])
    print(f"\n事件 n={len(kv)} | K_v 中位 {np.median(kv):.2f} "
          f"p25 {np.percentile(kv,25):.2f} p75 {np.percentile(kv,75):.2f} "
          f"min {kv.min():.2f} max {kv.max():.2f}（x_lane单位/秒 每满杆）")
    print("对照：planner 现拍脑袋 v_lat_gain=1.5")


if __name__ == "__main__":
    main()
