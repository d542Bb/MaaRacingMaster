# -*- coding: utf-8 -*-
"""T2 离线对拍：新深度读法（平面拟合+度量标定）vs hsv 黄线层，同帧流双链对比。

双链各自独立喂 _EgoRoadObserver（消费端与生产同构）：
- depth 链：DA 会话 infer → depth_geo.reading_from_map → _EgoRoadObserver.update(dgeo)
- hsv   链：boundary.detect_boundary → validity 过滤 → _EgoRoadObserver.update(bnd)

产出判据（交接 T2）：双侧读数相关系数、road_offset 相关系数、相位滞后
（互相关峰）、异常率（hsv 已知病灶应消失：物理不可能读数 / 单侧翻面 /
帧间跳变）、可用率。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_a2b_hsv_depth.py \
        [--demo 20260922_113724_p1] [--stride 3]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush import boundary  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (
    DepthRoadObserver, reading_from_map)  # noqa: E402
from maaracing_master.plugins.speedrush.module import _EgoRoadObserver  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402

DEMOS = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/demos")


def corr(a: list[float], b: list[float]) -> float:
    if len(a) < 3:
        return float("nan")
    x, y = np.array(a), np.array(b)
    if x.std() < 1e-9 or y.std() < 1e-9:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def xcorr_lag(a: list[float], b: list[float], max_lag: int = 15) -> int:
    """a 领先 b 的拍数（互相关峰；a、b 均值方差归一后滑窗）。"""
    x, y = np.array(a), np.array(b)
    x = (x - x.mean()) / max(x.std(), 1e-9)
    y = (y - y.mean()) / max(y.std(), 1e-9)
    best, lag = -2.0, 0
    for k in range(-max_lag, max_lag + 1):
        if k < 0:
            c = float(np.mean(x[-k:] * y[:k]))
        elif k > 0:
            c = float(np.mean(x[:-k] * y[k:]))
        else:
            c = float(np.mean(x * y))
        if c > best:
            best, lag = c, k
    return lag


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", default="20260922_113724_p1")
    ap.add_argument("--stride", type=int, default=3)
    args = ap.parse_args()

    cal = load_calib()
    ego = DepthRoadObserver._load_ego_mask()
    sess = dg.load_session(DEPTH_MODEL_FILE)
    obs_d = _EgoRoadObserver()
    obs_h = _EgoRoadObserver()
    rec: dict[str, list] = {k: [] for k in (
        "fid", "dl", "dr", "hl", "hr", "off_d", "off_h", "jd", "jh")}

    frames = [json.loads(line) for line in
              (DEMOS / args.demo / "frames.jsonl").open(encoding="utf-8")]
    t_start = time.perf_counter()
    n = 0
    for fr in frames[::args.stride]:
        img = cv2.imread(str(DEMOS / args.demo / "frames" / fr["file"]))
        if img is None:
            continue    # 录制缺口（frames.jsonl 有索引、文件缺失）：跳过
        rgb = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        fid = fr["frame_id"]
        # depth 链
        m = dg.infer_map(sess, rgb, dg.DEFAULT_SHORT)
        rd = reading_from_map(m, cal, ego_mask=ego)
        off_d = obs_d.update(rd)
        # hsv 链
        bnd = boundary.detect_boundary(rgb, cal)
        src = bnd if bnd.validity else None
        off_h = obs_h.update(src)
        # 帧间跳变（|Δlane| > 0.5 计一次，物理上车缘是慢变量）
        for side, val, key in (("dl", rd.left_edge_lane, "jd"),
                               ("dr", rd.right_edge_lane, "jd"),
                               ("hl", None if src is None else src.left_edge_lane, "jh"),
                               ("hr", None if src is None else src.right_edge_lane, "jh")):
            prev = rec[side][-1] if rec[side] else None
            if val is not None and prev is not None and abs(val - prev) > 0.5:
                rec[key].append(fid)
        rec["fid"].append(fid)
        rec["dl"].append(rd.left_edge_lane)
        rec["dr"].append(rd.right_edge_lane)
        rec["hl"].append(None if src is None else src.left_edge_lane)
        rec["hr"].append(None if src is None else src.right_edge_lane)
        rec["off_d"].append(off_d)
        rec["off_h"].append(off_h)
        n += 1
    dt = time.perf_counter() - t_start

    def pair(key_d, key_h):
        both = [(d, h) for d, h in zip(rec[key_d], rec[key_h])
                if d is not None and h is not None]
        return [x for x, _ in both], [y for _, y in both]

    print(f"demo={args.demo} stride={args.stride} 帧={n} 总耗时={dt:.0f}s "
          f"(depth {dt / n * 1000:.0f}ms/帧)")
    for side in ("dl", "dr"):
        hs = "hl" if side == "dl" else "hr"
        n_d = sum(v is not None for v in rec[side])
        n_h = sum(v is not None for v in rec[hs])
        a, b = pair(side, hs)
        print(f"{side}: depth在场率={n_d / n:.0%} hsv在场率={n_h / n:.0%} "
              f"同帧对={len(a)} 相关系数={corr(a, b):.3f}")
    a, b = pair("off_d", "off_h")
    print(f"road_offset: depth在场率={sum(v is not None for v in rec['off_d']) / n:.0%} "
          f"hsv在场率={sum(v is not None for v in rec['off_h']) / n:.0%} "
          f"同帧对={len(a)} 相关={corr(a, b):.3f} 相位滞后(depth领先hsv)={xcorr_lag(a, b)}拍")
    print(f"帧间跳变(|Δ|>0.5): depth={len(rec['jd'])} hsv={len(rec['jh'])} 次")
    # 物理不可能读数（消费端绝对界外即被判弃，但侧读数本身越界可查）
    for name, key in (("depth", ("dl", "dr")), ("hsv", ("hl", "hr"))):
        bad = sum(1 for k in key for v in rec[k] if v is not None and abs(v) > 3.5)
        tot = sum(1 for k in key for v in rec[k] if v is not None)
        print(f"{name} |lane|>3.5 物理界外: {bad}/{tot}")


if __name__ == "__main__":
    main()
