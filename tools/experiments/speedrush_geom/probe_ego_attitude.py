"""自车姿态与界面污染探针（第一步：地图的坐标变换到底有没有可观测来源）。

**要回答什么**（全部离线，不改生产）：

1. **平面姿态每拍可用吗**：路面平面 Y=aX+bZ+c 给侧倾 a、俯仰 b、机位高 c 与同帧
   焦距——这是**相机**相对路面的姿态。报逐帧抖动对动态范围之比（抖动≥量程即不可用）。
2. **相机↔车身刚性吗**：追尾相机若刚性绑车身，车身点云在相机系的朝向应逐帧恒定；
   若随驾驶动作摆，说明相机浮动——那么「相机姿态」与「车身姿态」是两个量，地图
   变换要先测相机、再用车身点云把两者接起来。
   车身点云口径：车箱内 + 高出路面一把刀 + 限高限深（只按 3D 箱筛会把车底路面
   算进来，实测 2.3 万点、主轴被路面主导）。
3. **界面污染有多大**——分两层测，机理不同：
   - **几何层**：拟合带（第 340~715 行）里唯一落得下界面的块（事件横幅）遮掉后
     重算平面，差值即污染量。左上/右上面板在 250 行以上，本来就在带外。
   - **模型层**：把全部界面矩形在**输入像素上**抠掉（Telea 修复）再推理一次，比
     MoGe 的焦距/俯仰/机位高——界面盖住天空与远景，会改变模型对整幅场景的全局
     判断，这一层是后处理遮罩挡不住的。

用法：
    python tools/experiments/speedrush_geom/probe_ego_attitude.py \
        20261022_172231_p2 [--every 8] [--limit 120] [--out x.json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    EGO_ABS_X, Y0, DIAG_Y1, infer_points, load_session, reading_from_points)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

DEBUG = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster"
         / "debug" / "speedrush")
REGIONS = (ROOT / "maaracing_master" / "plugins" / "speedrush"
           / "resources" / "policy" / "hud_regions.json")
CAM_W, CAM_H = 1280, 720
BAND = (Y0, DIAG_Y1)
BODY_HGT_LO = 0.30      # 车身点：高出路面 0.3m（拟合容差 0.15m 的两倍）
BODY_HGT_HI = 2.20      # 限高：切掉前车车顶与误入车箱的远处结构
BODY_Z = (4.0, 9.2)     # 车箱纵深（着地点 z≈5.0m、车尾 4.5m 为下界佐证）
# 对手距离标记不在 hud_regions.json（它不是读数格），按帧实测近似框
EXTRA_HUD = [("rival_marker", 540, 630, 580, 670)]


def _hud_rects() -> list[tuple[str, int, int, int, int]]:
    """全部界面矩形（全幅像素，含带外——模型层要的是输入干净）。"""
    out = []
    for name, (a, b, c, d) in json.loads(REGIONS.read_text(encoding="utf-8")).items():
        out.append((name, int(a * CAM_W), int(c * CAM_W), int(b * CAM_H), int(d * CAM_H)))
    return out + EXTRA_HUD


def _in_band(rects) -> list[tuple[str, int, int, int, int]]:
    out = []
    for name, x1, x2, y1, y2 in rects:
        iy1, iy2 = max(y1, BAND[0]), min(y2, BAND[1])
        if iy2 > iy1:
            out.append((name, x1, x2, iy1, iy2))
    return out


def _strip_hud(rgb: np.ndarray, rects) -> np.ndarray:
    """输入像素层抠界面：矩形内 Telea 修复（模型层对照用）。"""
    m = np.zeros(rgb.shape[:2], np.uint8)
    for _n, x1, x2, y1, y2 in rects:
        m[y1:y2, x1:x2] = 255
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    return np.ascontiguousarray(cv2.inpaint(bgr, m, 6, cv2.INPAINT_TELEA)[:, :, ::-1])


def _att(rd):
    if rd is None or rd.coef is None:
        return None, None, None
    a, b, c = rd.coef
    return (math.degrees(math.atan(a)), math.degrees(math.atan(b)), abs(c))


def _body_pose(pts: np.ndarray, coef, ds: float) -> dict | None:
    """车身点云在相机系的偏航：远段横位减负段横位，除以纵深差。"""
    if coef is None:
        return None
    a, b, c = coef
    X, Y, Z = pts[:, :, 0], pts[:, :, 1], pts[:, :, 2]
    sgn = -1.0 if b * 5.0 + c > 0 else 1.0
    hgt = sgn * (Y - (a * X + b * Z + c))
    m = (np.isfinite(X) & np.isfinite(Z) & np.isfinite(hgt)
         & (np.abs(X) < EGO_ABS_X)
         & (Z > BODY_Z[0]) & (Z < BODY_Z[1])
         & (hgt > BODY_HGT_LO) & (hgt < BODY_HGT_HI))
    m[: int(400 / ds)] = False
    if m.sum() < 400:
        return None
    x, z = X[m].astype(np.float64), Z[m].astype(np.float64)
    near, far = z < 5.6, z > 7.6
    if near.sum() < 60 or far.sum() < 60:
        return None
    dx = float(x[far].mean() - x[near].mean())
    dz = float(z[far].mean() - z[near].mean())
    return {"yaw": math.degrees(math.atan2(dx, dz)), "n": int(m.sum()),
            "dx": dx, "dz": dz, "x_mean": float(x.mean())}


def _series(name: str, every: int, limit: int) -> list[dict]:
    rows = [json.loads(x) for x in
            (DEBUG / name / f"trace_{name}.jsonl").read_text(encoding="utf-8").splitlines()
            if x]
    cal = load_calib()
    sess = load_session(DEPTH_MODEL_FILE)
    rects = _hud_rects()
    band = _in_band(rects)
    print(f"界面块共 {len(rects)} 个；落在拟合带 {BAND} 内的："
          f"{[(n, f'x{x1}-{x2}', f'y{y1}-{y2}') for n, x1, x2, y1, y2 in band]}")
    out: list[dict] = []
    for i in range(0, min(len(rows), limit * every), every):
        f = DEBUG / name / "raw" / f"{i + 1:04d}_raw.jpg"
        if not f.exists():
            continue
        rgb = np.asarray(Image.open(f).convert("RGB"))
        pts, fx, fy = infer_points(sess, rgb, native=True)
        ds = CAM_W / pts.shape[1]
        rd = reading_from_points(pts.copy(), fx, cal, fy=fy, ds=ds)
        pm = np.ascontiguousarray(pts.copy())
        for _n, x1, x2, y1, y2 in band:      # 几何层：带内界面遮掉，不重新推理
            pm[max(0, int(y1 / ds)):int(y2 / ds),
               max(0, int(x1 / ds)):int(x2 / ds)] = np.nan
        rd_b = reading_from_points(pm, fx, cal, fy=fy, ds=ds)
        p2, f2, y2f = infer_points(sess, _strip_hud(rgb, rects), native=True)
        rd_s = reading_from_points(p2.copy(), f2, cal, fy=y2f, ds=CAM_W / p2.shape[1])
        r0, r1, r2 = _att(rd), _att(rd_b), _att(rd_s)
        rr = rows[i]
        out.append({"i": i, "fid": rr["fid"],
                    "roll": r0[0], "pitch": r0[1], "height": r0[2], "fx": fx,
                    "roll_band": r1[0], "pitch_band": r1[1], "height_band": r1[2],
                    "roll_strip": r2[0], "pitch_strip": r2[1], "height_strip": r2[2],
                    "fx_strip": f2, "body": _body_pose(pts, rd.coef, ds),
                    "steer": rr.get("steer_norm"), "vlat": rr.get("v_lat_est")})
    return out


def _q(a, p):
    a = sorted(a)
    return a[min(len(a) - 1, int(p * (len(a) - 1)))] if a else float("nan")


def _jit(v):
    return statistics.median([abs(b - a) for a, b in zip(v, v[1:])]) if len(v) > 2 else float("nan")


def _corr(x, y):
    if len(x) < 10 or len(set(np.round(x, 6))) < 4:
        return float("nan")
    return float(np.corrcoef(np.array(x), np.array(y))[0, 1])


def report(name: str, s: list[dict]) -> None:
    print(f"\n===== {name}  采样 {len(s)} 帧")
    g = [x for x in s if None not in (x["roll"], x["roll_band"], x["roll_strip"])]
    print(f"  三版平面都出解：{len(g)}/{len(s)}")
    for key, label, unit in (("roll", "侧倾", "°"), ("pitch", "俯仰", "°"),
                             ("height", "机位高", "m")):
        v = [x[key] for x in g]
        rng = max(v) - min(v)
        j0 = _jit(v)
        print(f"  {label:>4} 现状: p50={statistics.median(v):+.3f}{unit} 全距{rng:.3f}"
              f" 逐帧抖{j0:.3f}（噪声/全距={j0/max(1e-9,rng):.0%}）")
        for suf, tag in (("band", "带内遮横幅"), ("strip", "输入抠全部界面")):
            d = [abs(x[key] - x[f"{key}_{suf}"]) for x in g]
            j2 = _jit([x[f"{key}_{suf}"] for x in g])
            print(f"        {tag}: 位移 p50={statistics.median(d):.3f} p90={_q(d,0.9):.3f}{unit}"
                  f" | 抖动 {j2:.3f}（{(1-j2/max(1e-9,j0)):+.0%}）")
    fv = [x["fx"] for x in g]
    fs = [x["fx_strip"] for x in g]
    print(f"  焦距: 现状 p50={statistics.median(fv):.1f}px 抖{_jit(fv):.1f}"
          f" | 抠界面 p50={statistics.median(fs):.1f}px 抖{_jit(fs):.1f}"
          f" | 同帧位移 p50={statistics.median([abs(a-b) for a,b in zip(fv,fs)]):.1f}px")
    e = [x for x in g if x["body"]]
    if e:
        y = [x["body"]["yaw"] for x in e]
        st = [x["steer"] for x in e]
        vl = [abs(x["vlat"]) for x in e if x["vlat"] is not None]
        n = min(len(y), len(st))
        print(f"\n  车身点云（n={len(e)}，均 {statistics.mean([x['body']['n'] for x in e]):.0f} 点）"
              f"在相机系偏航: p50={statistics.median(y):+.2f}° 全距[{min(y):+.2f},{max(y):+.2f}]"
              f" 逐帧抖 {_jit(y):.2f}°")
        print(f"    偏航 vs 转向杆量 r={_corr(y[:n], st[:n]):+.2f}"
              f" | vs |横向速度| r={_corr(y[:len(vl)], vl):+.2f}"
              f" | 横向中心 vs 转向杆量 r={_corr([x['body']['x_mean'] for x in e][:n], st[:n]):+.2f}")
    else:
        print("\n  车身点云样本不足（<400 点）——非刚性判断需更宽车箱或换车验证")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--every", type=int, default=8)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv[1:])
    s = _series(a.session, a.every, a.limit)
    report(a.session, s)
    if a.out:
        Path(a.out).write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
