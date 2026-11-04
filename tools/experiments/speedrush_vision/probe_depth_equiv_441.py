# -*- coding: utf-8 -*-
"""441 帧证据包逐位等价复算器：改造前/后 reading_from_points 全字段对比 + 分段计时。

用途（2026-10-04 后处理向量化）：先把「改造前」的读数与耗时 dump 下来，改完再 dump
一次，逐帧逐字段比对——`left/right_edge_lane`、`left_x/right_x`、`sides`、`rejects`、
`edge_pts` 必须**逐位一致**（latency_ms 除外，它是计时不是读数）。

证据包口径与 `probe_depth_pipeline_attribution.py` 一致（ego 掩码取资源文件，object
掩码 = *_mask.npy 与 ego 的差集）。

用法（仓库根，.venv）：
    # 改造前
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_equiv_441.py \
        dump --out .workbuddy-ai/depth_equiv/before.pkl
    # 改造后
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_equiv_441.py \
        dump --out .workbuddy-ai/depth_equiv/after.pkl
    # 对比（逐位 + ulp 分布 + 耗时对比）
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_equiv_441.py \
        check --ref .workbuddy-ai/depth_equiv/before.pkl --cur .workbuddy-ai/depth_equiv/after.pkl
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import struct
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402


def _use_ref_module() -> str | None:
    """DEPTH_GEO_REF=<path> 时把 reading_from_points 换成该文件里的实现。

    改造前 dump 用（`git show <改动前提交>:<...>/depth_geo.py > ref.py`）；只换
    入口函数，参考实现自带它的全部辅助函数。"""
    p = os.environ.get("DEPTH_GEO_REF")
    if not p:
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("depth_geo_ref", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["depth_geo_ref"] = mod
    spec.loader.exec_module(mod)
    dg.reading_from_points = mod.reading_from_points
    return p

FULL_W, FULL_H = 1280, 720
NAT_H, NAT_W = 336, 598
TRACES = (Path.home() / "AppData/Roaming/MaaRacingMaster/data/speedrush"
          / "control_traces")


def load_ego() -> np.ndarray | None:
    p = (Path(dg.__file__).parent / "resources" / "calibration" / "ego_mask.json")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        m = np.zeros((FULL_H, FULL_W), bool)
        m[d["y0"]:d["y1"], d["x0"]:d["x1"]] = True
        return m
    except Exception:
        return None


def assemble(stem: Path):
    """证据包 → (全幅点图, fx, fy, ego, obj)；口径同 probe_depth_pipeline_attribution。"""
    z = np.load(Path(str(stem) + "_evid.npz"))
    valid = np.unpackbits(z["valid"])[: NAT_H * NAT_W].reshape(NAT_H, NAT_W).astype(bool)
    pn = z["pts"].astype(np.float32)
    pts = np.stack([cv2.resize(pn[..., k], (FULL_W, FULL_H),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    vf = cv2.resize(valid.astype(np.float32), (FULL_W, FULL_H),
                    interpolation=cv2.INTER_NEAREST).astype(bool)
    pts = pts.copy()
    pts[~vf] = np.nan
    fx = float(z["fx"]) * FULL_W
    fy = float(z["fy"]) * FULL_H
    ego = load_ego()
    obj = None
    mp = Path(str(stem) + "_mask.npy")
    if ego is not None and mp.exists():
        merged = np.unpackbits(np.load(mp))[: FULL_W * FULL_H] \
            .reshape(FULL_H, FULL_W).astype(bool)
        obj = merged & ~ego
    return pts, fx, fy, ego, obj


def frames(dirs: list[str] | None):
    if dirs:
        roots = [TRACES / d for d in dirs]
    else:
        roots = sorted(TRACES.glob("depth_debug_*"))
    out = []
    for r in roots:
        for ev in sorted(r.glob("d*_evid.npz")):
            out.append(Path(str(ev)[: -len("_evid.npz")]))
    return out


def _control_ms() -> float:
    """机器状态漂移标尺：一段与本改造无关的固定 CPU 活（resize×3 + 50 次 median）。

    改造前后两次 dump 的读数耗时必须用它归一——同一会话内先后跑，绝对值仍受
    后台负载/频率漂移影响，比值才是可比量。"""
    rng = np.random.default_rng(0)
    a = rng.random(336 * 598, dtype=np.float32)
    b = rng.random((336, 598, 3), dtype=np.float32)
    t = time.perf_counter()
    for _ in range(50):
        np.median(a)
        for k in range(3):
            cv2.resize(b[..., k], (1280, 720), interpolation=cv2.INTER_LINEAR)
    return (time.perf_counter() - t) * 1e3


def dump(out: Path, dirs: list[str] | None, repeat: int) -> None:
    cal = load_calib()
    stems = frames(dirs)
    print(f"帧 {len(stems)} 张，repeat={repeat}")
    ctl_ms = float(np.median([_control_ms() for _ in range(3)]))
    print(f"控制标尺（漂移归一用）: {ctl_ms:.1f}ms")
    recs = []
    for i, stem in enumerate(stems):
        pts, fx, fy, ego, obj = assemble(stem)
        rd = dg.reading_from_points(pts, fx, cal, ego_mask=ego, object_mask=obj, fy=fy)
        ts = []
        for _ in range(repeat):
            t = time.perf_counter()
            r2 = dg.reading_from_points(pts, fx, cal, ego_mask=ego, object_mask=obj, fy=fy)
            ts.append((time.perf_counter() - t) * 1e3)
            assert r2.left_edge_lane == rd.left_edge_lane, "repeat 读数不一致"
        recs.append({"stem": str(stem), "dir": stem.parent.name,
                     "left": rd.left_edge_lane, "right": rd.right_edge_lane,
                     "left_x": rd.left_x, "right_x": rd.right_x, "sides": rd.sides,
                     "rejects": rd.rejects, "edge_pts": rd.edge_pts,
                     "ms": float(np.median(ts))})
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(stems)}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        pickle.dump({"recs": recs, "n": len(recs), "ctl_ms": ctl_ms}, f)
    ms = np.array([r["ms"] for r in recs])
    print(f"dump → {out}")
    print(f"  耗时 ms: p50={np.percentile(ms,50):.2f} p95={np.percentile(ms,95):.2f} "
          f"mean={ms.mean():.2f}")
    sides = [r["sides"] for r in recs]
    print(f"  在场侧数: 0={sides.count(0)} 1={sides.count(1)} 2={sides.count(2)}")


def _ulp(a: float, b: float) -> int:
    ia = struct.unpack("<q", struct.pack("<d", a))[0]
    ib = struct.unpack("<q", struct.pack("<d", b))[0]
    if (ia < 0) != (ib < 0):
        ia = -(ia if ia >= 0 else -(ia + 1)) - 1
        ib = -(ib if ib >= 0 else -(ib + 1)) - 1
    return abs(ia - ib)


def _cmp_scalar(a, b, name: str, stem: str, bad: list, ulps: list) -> None:
    if a is None or b is None:
        if a is not b:
            bad.append(f"{stem} {name}: {a!r} vs {b!r}")
        return
    # 逐位（bitwise）比较，比 == 更严：±0 的符号差也算不一致。
    if struct.pack("<d", float(a)) == struct.pack("<d", float(b)):
        ulps.append((name, 0))
        return
    ulps.append((name, _ulp(float(a), float(b))))
    bad.append(f"{stem} {name}: {a!r} vs {b!r} (ulp={_ulp(float(a), float(b))})")


def check(ref_p: Path, cur_p: Path) -> int:
    ref = pickle.loads(ref_p.read_bytes())
    cur = pickle.loads(cur_p.read_bytes())
    R, C = ref["recs"], cur["recs"]
    if len(R) != len(C):
        print(f"帧数不一致 {len(R)} vs {len(C)}")
        return 2
    bad: list[str] = []
    ulps: list[tuple[str, int]] = []
    for r, c in zip(R, C):
        assert r["stem"] == c["stem"]
        s = Path(r["stem"]).name
        for k in ("left", "right", "left_x", "right_x"):
            _cmp_scalar(r[k], c[k], k, s, bad, ulps)
        if r["sides"] != c["sides"]:
            bad.append(f"{s} sides: {r['sides']} vs {c['sides']}")
        if r["rejects"] != c["rejects"]:
            bad.append(f"{s} rejects: {r['rejects']} vs {c['rejects']}")
        ep_a, ep_b = r["edge_pts"], c["edge_pts"]
        if len(ep_a) != len(ep_b) or any(
                sa != sb or struct.pack("<d", za) != struct.pack("<d", zb)
                or struct.pack("<d", xa) != struct.pack("<d", xb)
                for (sa, za, xa), (sb, zb, xb) in zip(ep_a, ep_b)):
            bad.append(f"{s} edge_pts: {ep_a} vs {ep_b}")
    n = len(R)
    print(f"帧数 {n}")
    nz = [u for u in ulps if u[1] > 0]
    print(f"标量比对 {len(ulps)} 项；非零 ulp {len(nz)} 项"
          + (f"（最大 ulp={max(u[1] for u in nz)}）" if nz else "（全部逐位一致）"))
    if bad:
        print(f"❌ 不一致 {len(bad)} 项，前 20：")
        for b in bad[:20]:
            print("   ", b)
    else:
        print("✅ 全部字段逐位一致")
    mr = np.array([r["ms"] for r in R])
    mc = np.array([c["ms"] for c in C])
    rp50, cp50 = np.percentile(mr, 50), np.percentile(mc, 50)
    rp95, cp95 = np.percentile(mr, 95), np.percentile(mc, 95)
    print(f"耗时 p50: before={rp50:.2f} after={cp50:.2f} "
          f"({(cp50/rp50-1)*100:+.1f}%)")
    print(f"耗时 p95: before={rp95:.2f} after={cp95:.2f} "
          f"({(cp95/rp95-1)*100:+.1f}%)")
    print(f"耗时 mean: before={mr.mean():.2f} after={mc.mean():.2f}")
    cr, cc = ref.get("ctl_ms"), cur.get("ctl_ms")
    if cr and cc:
        print(f"控制标尺: before={cr:.1f} after={cc:.1f} "
              f"（机器状态比 {cc/cr:.3f}）")
        print(f"标尺归一后 p50 变化: {((cp50/cc)/(rp50/cr)-1)*100:+.1f}%")
    return 0 if not bad else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--out", required=True)
    d.add_argument("--dir", action="append", default=None)
    d.add_argument("--repeat", type=int, default=3)
    c = sub.add_parser("check")
    c.add_argument("--ref", required=True)
    c.add_argument("--cur", required=True)
    a = ap.parse_args()
    ref = _use_ref_module()
    if ref:
        print(f"reading_from_points 实现 = {ref}")
    if a.mode == "dump":
        dump(Path(a.out), a.dir, a.repeat)
    else:
        sys.exit(check(Path(a.ref), Path(a.cur)))


if __name__ == "__main__":
    main()
