# -*- coding: utf-8 -*-
"""真机调试帧 → 3D 点云 HTML（可行驶边界判定取证用）。

输入 = depth_debug 目录的 dXXXXX 证据包（*_evid.npz 原生点图 fp16 + 打包 valid
+ 焦距；ego/物体掩码同为打包位图）。复算口径与 depth_geo.render_depth_debug
一致：解包 → 各通道 resize 全幅 → 焦距按幅缩放 → invalid 置 nan。RGB 取调试
渲染图顶部 720 行（原图带掩码描边，取证时留意描边颜色不是场景色）。

世界系 = 产线换装摆正（dg._fit_road_plane fy 路径），与 compare_moge_quant
的「③′ 产线特征拟合」同一注入写法；查看器与网格复用 compare_depth_sources。

用法（仓库根 .venv）：
    python tools/experiments/speedrush_vision/export_debug_cloud.py \
        --dir "C:/Users/yomen/AppData/Roaming/MaaRacingMaster/data/speedrush/control_traces/depth_debug_20261002_162102" \
        --d 3 --d 8 --d 10
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision import compare_depth_sources as cds  # noqa: E402
from tools.experiments.speedrush_vision import export_pointcloud_html as eph  # noqa: E402

OUT = cds.OUT.parent / "pc3d_debug"


def _unpack(packed: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    n = int(np.prod(shape))
    return np.unpackbits(packed)[:n].reshape(shape).astype(bool)


def _load_frame(stem: Path) -> np.ndarray:
    """调试渲染图顶部 720 行 = 原帧（含掩码描边）。"""
    bgr = cv2.imread(str(stem) + ".jpg")
    return cv2.cvtColor(bgr[:720], cv2.COLOR_BGR2RGB)


def load_cloud(stem: Path):
    d = np.load(str(stem) + "_evid.npz")
    pts_n = d["pts"].astype(np.float32)
    oh, ow = pts_n.shape[:2]
    valid = _unpack(d["valid"], pts_n.shape[:2])
    pts = np.stack([cv2.resize(pts_n[..., k], (1280, 720),
                               interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid = cv2.resize(valid.astype(np.float32), (1280, 720),
                       interpolation=cv2.INTER_NEAREST).astype(bool)
    pts[~valid] = np.nan
    fy = float(d["fy"]) * (720 / oh)
    ego = _unpack(np.load(str(stem) + "_ego.npy"), (720, 1280))
    objm = _unpack(np.load(str(stem) + "_mask.npy"), (720, 1280))
    obj = objm & ~ego
    return pts, fy, ego, obj, _load_frame(stem)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--d", type=int, nargs="+", required=True)
    args = ap.parse_args()
    root = Path(args.dir)

    srcs = []
    for i in args.d:
        stem = root / f"d{i:05d}"
        pts, fy, ego, obj, frame = load_cloud(stem)
        srcs.append(cds._worldize(
            f"d{i:05d}", pts[..., 0], pts[..., 1], pts[..., 2],
            frame, ego | obj, None, cds.STEP,
            note="产线换装摆正 · 调试帧离线复算",
            fit_fn=lambda X, Y, Z, excl: dg._fit_road_plane(
                X, Y, Z, excl, fy=fy)))
        print(f"d{i:05d}: done")

    gpts, gcols = eph._grid_axes(0.0)
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()
    src_js = "[" + ",".join(
        '{name:"' + s["name"] + '",hud:`' + s["hud"] + '`,'
        'xyz:f32(`' + b64(s["xyz"]) + '`),rgb:u8(`' + b64(s["rgb"]) + '`),'
        'ht:f32(`' + b64(s["ht"]) + '`),bd:u8(`' + b64(s["bd"]) + '`)}'
        for s in srcs) + "]"
    stem = root.name
    html = (cds.HTML.replace("__STEM__", stem).replace("__GP__", b64(gpts))
            .replace("__GC__", b64(gcols)).replace("__SRC__", src_js)
            .replace("__TW__", "480"))
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{stem}_debug.html"
    out.write_text(html, encoding="utf-8")
    print(f"{out}  {len(html)/1e6:.2f} MB  {len(srcs)} 源")


if __name__ == "__main__":
    main()
