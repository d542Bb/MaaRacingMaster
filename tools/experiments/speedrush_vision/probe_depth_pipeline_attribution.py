# -*- coding: utf-8 -*-
"""深度管线耗时归因（CPU 侧）：证据包离线重放，分离「帧拷贝/缩放」与「找边后处理」。

问（2026-10-04 性能归因调查）：worker 耗时 p50 130~153ms 里，MoGe 推理本体、
recover_focal_shift/点云后处理（moge_post）、帧拷贝/缩放、锁等待各占多少？

本探针只覆盖**不需要 GPU 的两段**，直接读产线落盘的证据包（*_evid.npz：原生
336×598 点图 fp16 + valid + 归一化焦距），逐位重放产线后处理链：
  (a) 证据包 → 全幅点图（3 通道 cv2.resize + valid 最近邻 resize + nan 置位）
      —— 对应 infer_points 里的上采样段（「帧拷贝/缩放」的 CPU 分量）；
  (b) reading_from_points（平面拟合 + 列剖面特征行走 + 3D 找边）。
两段计时用 perf_counter，逐帧独立测量后聚合 p50/p95。

**不覆盖**：preprocess（RGB→720×1280 fp32）、forward（DML run）、reconstruct
（recover_focal_shift + force_projection）——证据包存的是**重建之后**的点云，
原始模型输出（points/normal/mask/metric_scale）没有落盘，无法离线重放这三段；
它们由 GPU 侧 `probe_moge_stage_decompose.py` 在同一进程内测量（空载口径）。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_pipeline_attribution.py \
        --root "%APPDATA%/MaaRacingMaster/data/speedrush/control_traces"
    # 只跑指定目录
    ... --dir depth_debug_20261004_140724
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DIAG_Y1, Y0, reading_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as _dg  # noqa: E402


def _use_ref_module() -> str | None:
    """DEPTH_GEO_REF=<path> 时把 reading_from_points 换成该文件里的实现。

    改造前后同机对比用（`git show HEAD:<...>/depth_geo.py > ref.py` 即得改造前
    版本）——只换入口函数，参考实现自带它的全部辅助函数。"""
    p = os.environ.get("DEPTH_GEO_REF")
    if not p:
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("depth_geo_ref", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["depth_geo_ref"] = mod
    spec.loader.exec_module(mod)
    _dg.reading_from_points = mod.reading_from_points
    globals()["reading_from_points"] = mod.reading_from_points
    return p

FULL_W, FULL_H = 1280, 720
NAT_H, NAT_W = 336, 598


def load_ego_mask() -> np.ndarray | None:
    p = (Path(__file__).resolve().parents[3] / "maaracing_master" / "plugins"
         / "speedrush" / "resources" / "calibration" / "ego_mask.json")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        m = np.zeros((FULL_H, FULL_W), bool)
        m[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
        return m
    except Exception:
        return None


def load_case(stem: Path, ego: np.ndarray | None):
    """证据包 → (全幅点图组装函数, fx, fy, ego, obj)。

    返回组装函数而非成品，是为了把「组装（拷贝/缩放）」单独计时。"""
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: NAT_H * NAT_W].reshape(NAT_H, NAT_W).astype(bool)
    pts_n = z["pts"].astype(np.float32)
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H

    obj = None
    mask_p = Path(str(stem) + "_mask.npy")
    if ego is not None and mask_p.exists():
        merged = np.unpackbits(np.load(mask_p))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        obj = merged & ~ego

    def assemble():
        pts = np.stack([cv2.resize(pts_n[..., k], (FULL_W, FULL_H),
                                   interpolation=cv2.INTER_LINEAR)
                        for k in range(3)], -1)
        valid_full = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                                interpolation=cv2.INTER_NEAREST).astype(bool)
        pts = pts.copy()
        pts[~valid_full] = np.nan
        return pts, valid_full

    return assemble, fx, fy, ego, obj


def pct(xs, q):
    xs = [x for x in xs if x == x]
    return float(np.percentile(xs, q)) if xs else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(
        Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush/control_traces"))
    ap.add_argument("--dir", action="append", default=None,
                    help="只跑指定目录名（可重复）；缺省跑全部 depth_debug_*")
    ap.add_argument("--limit", type=int, default=0, help="每目录最多取 N 帧（0=全部）")
    a = ap.parse_args()
    ref = _use_ref_module()
    print(f"reading_from_points 实现 = {ref or '仓库当前实现'}")

    root = Path(a.root)
    dirs = ([root / d for d in a.dir] if a.dir
            else sorted(root.glob("depth_debug_*")))
    cal = load_calib()
    ego = load_ego_mask()

    rows = []
    for d in dirs:
        stems = sorted(d.glob("d*_evid.npz"))
        if a.limit:
            stems = stems[:a.limit]
        for ev in stems:
            stem = Path(str(ev)[:-len("_evid.npz")])
            try:
                assemble, fx, fy, eg, obj = load_case(stem, ego)
            except Exception as e:  # noqa: BLE001
                print(f"  跳过 {stem.name}: {type(e).__name__}: {e}")
                continue
            # 组装段
            t0 = time.perf_counter()
            pts, _ = assemble()
            t_asm = (time.perf_counter() - t0) * 1000.0
            # 后处理段（reading_from_points 自带 latency_ms）
            rd = reading_from_points(pts, fx, cal, ego_mask=eg, object_mask=obj, fy=fy)
            rows.append({"dir": d.name, "stem": stem.name, "assemble_ms": t_asm,
                         "reading_ms": rd.latency_ms, "sides": rd.sides,
                         "rejects": ";".join(rd.rejects)})

    if not rows:
        sys.exit("无样本")
    asm = [r["assemble_ms"] for r in rows]
    rdg = [r["reading_ms"] for r in rows]
    tot = [r["assemble_ms"] + r["reading_ms"] for r in rows]
    print(f"样本 {len(rows)} 帧 / {len(set(r['dir'] for r in rows))} 目录")
    print(f"  (a) 组装(拷贝/缩放) ms: p50={pct(asm,50):6.2f} p95={pct(asm,95):6.2f} "
          f"max={max(asm):6.2f}")
    print(f"  (b) reading_from_points ms: p50={pct(rdg,50):6.2f} p95={pct(rdg,95):6.2f} "
          f"max={max(rdg):6.2f}")
    print(f"  (a+b) 后处理合计 ms: p50={pct(tot,50):6.2f} p95={pct(tot,95):6.2f}")
    sides = [r["sides"] for r in rows]
    print(f"  在场侧数: 0={sides.count(0)} 1={sides.count(1)} 2={sides.count(2)}")
    # 逐目录（对齐六阶段实机）
    print("  逐目录 p50 (组装 / 找边 / 合计):")
    for d in sorted(set(r["dir"] for r in rows)):
        sub = [r for r in rows if r["dir"] == d]
        print(f"    {d}: n={len(sub):3d} "
              f"{pct([r['assemble_ms'] for r in sub],50):6.2f} / "
              f"{pct([r['reading_ms'] for r in sub],50):6.2f} / "
              f"{pct([r['assemble_ms']+r['reading_ms'] for r in sub],50):6.2f}")
    # 复算自检：与旧 jpg 左上角文本对照由 replay_debug_evid 负责，这里只打印一条
    r0 = rows[0]
    print(f"  自检样例 {r0['dir']}/{r0['stem']}: sides={r0['sides']} rejects={r0['rejects']}")


if __name__ == "__main__":
    main()
