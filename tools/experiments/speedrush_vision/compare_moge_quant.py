# -*- coding: utf-8 -*-
"""MoGe-2 本机量化档阶梯对比（同帧同网格，一页切源）。

维护者要能自己判断"更小那档能不能用"：一页里放 fp32 → fp16 → q4f16（现役，
烘常量）→ q4f16（拆常量）→ q3f16 → q2f16，每档各自做平面摆正与 HUD 读数
（相机高/半路宽/路面残差/滚转俯仰），网格与三轴共用、切源时不动，谁塌谁漂
一眼可见；每档标注**权重文件实际体积**与**本机推理 p50**。

查看器、摆正口径、网格参考物全部复用 compare_depth_sources（同一交互习惯：
拖=旋转、滚轮=缩放、右键拖=平移、切数据源 ▶、切色 RGB/高度）。

时延口径：同一进程内所有档同载、逐轮交错测量（首轮为预热不计），故是**相对
参考**——绝对时延请以单会话进程为准。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/compare_moge_quant.py --stem 000100
输出：<depth_review>/pc3d_compare/<stem>_moge_quant.html
"""

from __future__ import annotations

import argparse
import base64
import csv
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision import compare_depth_sources as cds  # noqa: E402
from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402
from tools.experiments.speedrush_vision import moge2_post as mp  # noqa: E402

OUT = cds.OUT
WEIGHTS = cds.WEIGHTS
STEP = cds.STEP

# 阶梯（由大到小）：label、权重文件名、说明
# 3-bit 缺档：onnxruntime 的 MatMulNBitsQuantizer 只支持 2/4/8 bits。
# 2-bit 缺档：DML 不收 2-bit MatMulNBits（回退 CPU 内核，该内核只支持 8-bit，
# 加载即报 nbits_ == 8 was false）——见 README「MoGe 量化档阶梯」节。
LADDER = [
    ("① fp32", "moge2_vits_static_336x598_t1032_unbake.onnx", "无量化（质量上限）"),
    ("② fp16", "moge2_vits_static_336x598_t1032_unbake_fp16.onnx", "半精度，无量化"),
    ("③ q4f16", "moge2_vits_static_336x598_t1032_q4f16.onnx", "int4 block32 · 产线现役"),
    ("④ q4f16", "moge2_vits_static_336x598_t1032_unbake_q4f16.onnx", "int4 block32 · 拆折叠常量"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stem", action="append", default=[])
    ap.add_argument("--rounds", type=int, default=6, help="交错测速轮数（首轮预热不计）")
    args = ap.parse_args()
    stems = args.stem or ["000100"]

    rows = {Path(r["path"]).stem: r for r in
            csv.DictReader(eph.GOLD.open(encoding="utf-8"))}
    ego_full = dg.DepthRoadObserver._load_ego_mask()
    band_full = np.zeros((720, 1280), bool)
    band_full[dg.Y0:dg.DIAG_Y1] = True
    gpts, gcols = eph._grid_axes(0.0)
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()

    # 建会话一次、全帧复用：DML 析构后再 run 会段错误，不能逐帧重建；
    # 多帧共用也避免显存里堆出 N 倍会话。
    srcs = []
    for label, fname, spec in LADDER:
        w = WEIGHTS / fname
        if not w.exists():
            print("skip", fname)
            continue
        srcs.append({"label": label, "spec": spec, "w": w,
                     "mb": w.stat().st_size / 1e6,
                     "sess": cds._moge_session(0, wpath=w),
                     "lat": [], "out": None})

    for stem in stems:
        if stem not in rows:
            print("skip", stem)
            continue
        frame = cv2.cvtColor(cv2.imread(rows[stem]["path"]),
                             cv2.COLOR_BGR2RGB).astype(np.uint8)
        blob = mp.preprocess(frame, dg.MOGE_IN_W, dg.MOGE_IN_H)
        for s in srcs:
            s["lat"] = []
        # 逐轮交错：同轮内各档相邻执行，抵消热漂移；首轮为预热
        for r in range(args.rounds + 1):
            for s in srcs:
                t0 = time.perf_counter()
                s["out"] = mp.forward(s["sess"], blob, 1032)
                if r:
                    s["lat"].append((time.perf_counter() - t0) * 1e3)

        out_src = []
        for s in srcs:
            points, mask, mscale = s["out"]
            res = mp.reconstruct(points, mask, mscale)
            pts = res["pts"].astype(np.float32)
            valid = res["valid"]
            if pts.shape[:2] != (720, 1280):          # 336-out 档：上采样回全幅
                pts = np.stack([cv2.resize(pts[..., k], (1280, 720),
                                           interpolation=cv2.INTER_LINEAR)
                                for k in range(3)], -1)
                valid = cv2.resize(valid.astype(np.float32), (1280, 720),
                                   interpolation=cv2.INTER_NEAREST).astype(bool)
            pts[~valid] = np.nan
            lat = float(np.percentile(s["lat"], 50))
            name = f"{s['label']} {s['mb']:.1f}MB"
            w = cds._worldize(name, pts[..., 0], pts[..., 1], pts[..., 2], frame,
                              ego_full, None, STEP, band_mask=band_full,
                              note=f"{s['spec']} · 推理p50={lat:.1f}ms · "
                                   f"mask剔除{1 - valid.mean():.0%}")
            out_src.append(w)
            if s["label"].startswith("③"):
                # 现役档加产线换装摆正对照（同一点云、特征种子+走廊收敛拟合）：
                # A/B 看效果——墙侧帧旧摆正整体歪、新摆正路平，只换摆正平面
                fy_full = float(res["fy"]) * 720.0
                out_src.append(cds._worldize(
                    f"{s['label']}′ 产线特征拟合", pts[..., 0], pts[..., 1],
                    pts[..., 2], frame, ego_full, None, STEP,
                    band_mask=band_full,
                    note="产线换装摆正（depth_geo 特征种子+走廊收敛）· "
                         "与上一源同点云同网格，只换摆正平面",
                    fit_fn=lambda X, Y, Z, excl: dg._fit_road_plane(
                        X, Y, Z, excl, fy=fy_full)))
            print(f"{name:22s} {s['spec']:24s} p50={lat:5.1f}ms  focal={res['focal']:.3f}")

        th = frame.copy()
        for yy in (dg.Y0, dg.DIAG_Y1):
            cv2.line(th, (0, yy), (1279, yy), (255, 60, 60), 2)
        th = cv2.cvtColor(cv2.resize(th, (480, 270)), cv2.COLOR_RGB2BGR)
        _, buf = cv2.imencode(".jpg", th, [cv2.IMWRITE_JPEG_QUALITY, 82])
        thumb_b64 = base64.b64encode(buf).decode()

        src_js = "[" + ",".join(
            '{name:"' + s["name"] + '",hud:`' + s["hud"] + '`,'
            'xyz:f32(`' + b64(s["xyz"]) + '`),rgb:u8(`' + b64(s["rgb"]) + '`),'
            'ht:f32(`' + b64(s["ht"]) + '`),bd:u8(`' + b64(s["bd"]) + '`)}'
            for s in out_src) + "]"
        html = (cds.HTML.replace("__STEM__", stem + " MoGe 量化档")
                .replace("__GP__", b64(gpts)).replace("__GC__", b64(gcols))
                .replace("__SRC__", src_js).replace("__TW__", "480"))
        html = html.replace('<img id="thumb" width="480">',
                            f'<img id="thumb" width="480" src="data:image/jpeg;base64,{thumb_b64}">')
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / f"{stem}_moge_quant.html"
        p.write_text(html, encoding="utf-8")
        print(f"{p}  {len(html) / 1e6:.2f} MB  {len(out_src)} 档")


if __name__ == "__main__":
    main()
