# -*- coding: utf-8 -*-
"""原始帧 → 3D 点云 HTML（2026-10-10）：对 debug/speedrush/<局>/raw/ 里的原始帧
跑完整 MoGe 推理（产线同链：load_session → infer_points），按 export_debug_cloud
的 evid 格式落临时目录，再调它的 main 生成 pc3d_debug HTML——供维护者目检
「重建是否翘曲/场景是否弯道」。

用法（仓库根，.venv）：
    .venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_raw_cloud.py \
        "C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/speedrush/20261010_212349_p2/raw/0119_raw.jpg"
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from tools.experiments.speedrush_vision import compare_depth_sources as cds  # noqa: E402

OUT = cds.OUT.parent / "pc3d_debug"
MODEL = Path(ROOT) / "maaracing_master" / "plugins" / "speedrush" / "resources" / "onnx" / "depth" / "moge2_vits_static_336x598_t1032_q4f16.onnx"


def main() -> None:
    raw = Path(sys.argv[1])
    name = raw.parent.parent.name + "_" + raw.stem.split("_")[0]
    tmp = raw.parents[2] / "raw_replay_tmp" / name
    tmp.mkdir(parents=True, exist_ok=True)

    frame = cv2.cvtColor(cv2.imread(str(raw)), cv2.COLOR_BGR2RGB)
    sess = dg.load_session(MODEL)
    pts_n, fx, fy, evid = dg.infer_points(sess, frame, with_evidence=True)
    valid = evid["valid"]
    np.savez(str(tmp / "d00001_evid.npz"),
             pts=pts_n.astype(np.float16), valid=np.packbits(valid.ravel()),
             valid_shape=np.array(valid.shape, np.int64),
             fx=fx, fy=fy, ms=float(evid["ms"]), fid=0)
    cv2.imwrite(str(tmp / "d00001_frame.jpg"), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
    zeros = np.packbits(np.zeros(720 * 1280, bool))
    np.save(str(tmp / "d00001_ego.npy"), zeros)
    np.save(str(tmp / "d00001_mask.npy"), zeros)
    print(f"推理完成: fx={fx:.4f} fy={fy:.4f} ms={evid['ms']:.3f} "
          f"solver={evid.get('solver')} 有效占比={valid.mean():.2f}")

    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()
    import tools.experiments.speedrush_vision.export_debug_cloud as edc
    pts_f = np.stack([cv2.resize(pts_n.astype(np.float32)[..., k], (1280, 720),
                                 interpolation=cv2.INTER_LINEAR) for k in range(3)], -1)
    valid_f = cv2.resize(valid.astype(np.float32), (1280, 720),
                         interpolation=cv2.INTER_NEAREST).astype(bool)
    pts_f[~valid_f] = np.nan
    fy_full = fy * (720 / pts_n.shape[0])
    s = cds._worldize(name, pts_f[..., 0], pts_f[..., 1], pts_f[..., 2],
                      frame, np.zeros((720, 1280), bool), None, cds.STEP,
                      note="产线换装摆正 · 原始帧在线推理",
                      fit_fn=lambda X, Y, Z, excl: dg._fit_road_plane(
                          X, Y, Z, fy=fy_full))
    js = ('{name:"' + s["name"] + '",hud:`' + s["hud"] + '`,'
          'xyz:f32(`' + b64(s["xyz"]) + '`),rgb:u8(`' + b64(s["rgb"]) + '`),'
          'ht:f32(`' + b64(s["ht"]) + '`),bd:u8(`' + b64(s["bd"]) + '`)}')
    html = (cds.HTML.replace("__STEM__", name).replace("__SRC__", "[" + js + "]")
            .replace("__TW__", "480"))
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{name}_debug.html"
    out.write_text(html, encoding="utf-8")
    print(f"{out}  {len(html)/1e6:.2f} MB")


if __name__ == "__main__":
    main()
