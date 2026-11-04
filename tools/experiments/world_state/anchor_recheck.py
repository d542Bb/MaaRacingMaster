# -*- coding: utf-8 -*-
"""补给车锚定失败离线复算（20261103_094900 局1 fid 2140 车45）。

假设：发光补给车的特效区域深度点大量无效（NaN），_box_anchor_z 的
≥10 有限点门槛永远过不去 → 逐框锚定静默弃权 → NO_ANCHOR_YET 全生命
周期 → 避让层拿不到横位 → 直直撞上去。

复算：对存盘原帧跑 MoGe 推理，数补给车框内有限点比例，并与同帧正常
轿车框对比；再跑 _box_anchor_z 本体看它返回什么。

用法::

    python tools/experiments/world_state/anchor_recheck.py \
        --frame <raw.jpg> [--box x0 y0 x1 y1]
"""

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import cv2  # noqa: E402

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    load_session, infer_points, _box_anchor_z)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", required=True)
    ap.add_argument("--box", required=True,
                    help="x0,y0,x1,y1 全幅像素（1280x720）")
    args = ap.parse_args()
    rgb = cv2.imread(args.frame)[..., ::-1]
    sess = load_session(DEPTH_MODEL_FILE)
    pts, fx, fy, evid = infer_points(sess, rgb, with_evidence=True,
                                     native=True, scale=1.0)
    print(f"点图 {pts.shape}  fx {fx:.1f} fy {fy:.1f}")
    x0, y0, x1, y1 = (int(v) for v in args.box.split(","))
    gh, gw = pts.shape[:2]
    bx0, by0 = int(x0 * gw / 1280), int(y0 * gh / 720)
    bx1, by1 = int(x1 * gw / 1280), int(y1 * gh / 720)
    zv = pts[by0:by1, bx0:bx1, 2]
    fin = zv[np.isfinite(zv)]
    print(f"框({x0},{y0},{x1},{y1})→点图[{by0}:{by1},{bx0}:{bx1}] "
          f"总点 {zv.size} 有限 {fin.size} ({(fin.size / max(zv.size, 1)):.0%})")
    if fin.size:
        print(f"  有限点 Z: p2 {np.percentile(fin, 2):.1f} "
              f"p50 {np.percentile(fin, 50):.1f} p98 {np.percentile(fin, 98):.1f} m")
    z = _box_anchor_z(pts, (x0, y0, x1, y1))
    print(f"_box_anchor_z → {z}")
    # 全帧有效率作对照
    allz = pts[..., 2]
    print(f"全帧有限点率 {(np.isfinite(allz).sum() / allz.size):.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
