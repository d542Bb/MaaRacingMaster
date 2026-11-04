# -*- coding: utf-8 -*-
"""实验 A：近场首次建锚反事实（§8 预登记）——深度侧框内 Z 产出门槛重放。

**对象**（L0 修正后）：`anchor_sheet_from_detections` 的静默弃权三条件
（框 <3px / 有效点 <10 / Z 出域 [2,90]m）+ 近簇选择。零锚轨有框高观测
但深度侧从未产出条目——重放记录局 evid 包的深度点云，问：现行门槛与
放宽门槛各能产出多少锚、产出的锚质量如何。

**候选规则**（预登记，一次只动一个）：
  - A0 基线：现行三条件；
  - A1 Z_LO 2.0 → 1.5：近场贴身距离不弃权；
  - A2 有效点 10 → 6：近场框点云稀疏不弃权。

**框重构近似**（诚实地记）：检测框不在 dump 里，用 car_pos（x=框心 x、
cy=框心 y）+ car_h（h=框高）重构，**宽 = h×1.33 近似**——Z 抽取窗宽向
误差一阶，产出率与质量数字带此近似读。
**质量参照**：现行有锚轨的同距离桶投影残差 |cy−(y_h+vpx·cam_h/z)| 分布。

**否定条件（预登记）**：A1/A2 产出的锚投影残差同距离桶 P50 恶化 >30%
（相对现行有锚轨同桶分布）→ 否定该候选。
只读 debug 产物，不改生产。"""
from __future__ import annotations

import collections
import glob
import json
import os
import re
from pathlib import Path

import numpy as np

DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
TRACES = ["20261016_224328_p1", "20261016_224412_p2"]

Y_H, VPX = 324.2, 640.7
Z_LO, Z_HI = 2.0, 90.0
NEAR_FRAC = 0.4                      # _NEAR_CLUSTER_FRAC（与生产同值）


def box_anchor_z(pts: np.ndarray, box, z_lo=Z_LO, z_hi=Z_HI, min_pts=10):
    """与 depth_geo._box_anchor_z 同式重放（门槛参数化）。"""
    gh, gw = pts.shape[:2]
    x0 = max(0, int(box[0] * gw / 1280))
    y0 = max(0, int(box[1] * gh / 720))
    x1 = min(gw, int(box[2] * gw / 1280))
    y1 = min(gh, int(box[3] * gh / 720))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None, "框过小"
    zv = pts[y0:y1, x0:x1, 2]
    zv = zv[np.isfinite(zv)]
    if zv.size < min_pts:
        return None, f"有效点不足({zv.size})"
    lo, hi = np.percentile(zv, [2, 98])
    hist, edges = np.histogram(zv, bins=24, range=(lo, hi))
    densest = hist.max()
    pick = int(hist.argmax())
    for i in range(len(hist)):
        if hist[i] >= NEAR_FRAC * densest:
            pick = i
            break
    c = 0.5 * (edges[pick] + edges[pick + 1])
    core = np.abs(zv - c) < (hi - lo) / 24
    if core.sum() < 10:
        core = np.ones(zv.size, bool)
    z = float(np.median(zv[core]))
    if not z_lo <= z <= z_hi:
        return None, f"Z出域({z:.1f})"
    return z, None


VARIANTS = {                         # (z_lo, min_pts)
    "A0现行": (2.0, 10),
    "A1:Z_LO1.5": (1.5, 10),
    "A2:有效点6": (2.0, 6),
}

cnt = collections.Counter()          # (变体, 结果) -> n
prod_res = collections.defaultdict(list)   # 变体 -> [投影残差]
ref_res = collections.defaultdict(list)    # 现行有锚轨参照（分桶）
n_frames = 0
for d in TRACES:
    rows = [json.loads(l) for l in
            open(DBG / d / f"trace_{d}.jsonl", encoding="utf-8")]
    by_fid = {r.get("fid"): r for r in rows if r.get("fid") is not None}
    # 现行有锚轨参照分布
    for r in rows:
        cam_h = r.get("dgeo_cam_h")
        if cam_h is None:
            continue
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        for x in (r.get("dist") or []):
            m = x.get("m")
            c = cp.get(x.get("id"))
            if m is None or m < 3 or c is None or c.get("cy") is None:
                continue
            b = "5-10m" if m < 10 else ("10-20m" if m < 20 else "20m+")
            ref_res[b].append(abs(c["cy"] - (Y_H + VPX * cam_h / m)))
    # 逐 evid 帧重放（对齐键=npz 内部 fid，文件名序号是深度 take 序号不是
    # trace fid——215xxx 局 fid 全局、记录局 fid 局内计数，两套空间不对齐）
    dd = DBG / d / "depth_debug"
    for npz_path in sorted(glob.glob(str(dd / "d*_evid.npz"))):
        data = np.load(npz_path)
        fid = int(data["fid"])
        if fid < 0:
            continue                     # 证据包缺 fid：无法对齐，跳过
        r = by_fid.get(fid)
        if r is None:
            continue
        cam_h = r.get("dgeo_cam_h")
        if cam_h is None:
            continue
        data = np.load(npz_path)
        pts = data["pts"].astype(np.float32)
        anchored = {x["id"] for x in (r.get("dist") or [])
                    if x.get("m") is not None}
        ch = {x["id"]: x for x in (r.get("car_h") or [])}
        cp = {c["id"]: c for c in (r.get("car_pos") or [])}
        for tid, hinfo in ch.items():
            if tid in anchored:
                continue               # 只重放零锚轨的框
            c = cp.get(tid)
            h = hinfo.get("h")
            if c is None or not h or h < 3 or c.get("cy") is None:
                continue
            cx, cy = c.get("x"), c["cy"]
            if cx is None:
                continue
            w = h * 1.33               # 宽近似（诚实标注）
            box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            n_frames += 1
            for name, (z_lo, min_pts) in VARIANTS.items():
                z, why = box_anchor_z(pts, box, z_lo, Z_HI, min_pts)
                if z is None:
                    cnt[(name, why)] += 1
                    continue
                cnt[(name, "产出")] += 1
                prod_res[name].append(
                    (abs(cy - (Y_H + VPX * cam_h / z)),
                     "5-10m" if z < 10 else ("10-20m" if z < 20 else "20m+")))
print(f"=== 实验 A 反事实（重放零锚框 {n_frames} 个） ===")
for name in VARIANTS:
    total = sum(v for (n2, _), v in cnt.items() if n2 == name)
    ok = cnt.get((name, "产出"), 0)
    print(f"\n{name}: 产出 {ok}/{total} ({ok/max(total,1)*100:.0f}%)")
    for (n2, why), v in cnt.most_common():
        if n2 == name and why != "产出":
            print(f"  弃权 {why}: {v}")
    # 产出的锚质量（分桶 vs 现行有锚轨参照）
    for b in ("5-10m", "10-20m", "20m+"):
        a = sorted(r2 for r2, bb in prod_res[name] if bb == b)
        ref = sorted(ref_res.get(b, []))
        if a and ref:
            p = a[len(a)//2]
            rp = ref[len(ref)//2]
            delta = (p - rp) / max(rp, 1) * 100
            print(f"  {b}: 产出 {len(a)} 残差 P50={p:.0f}px vs 参照 {rp:.0f}px "
                  f"({delta:+.0f}%)")
