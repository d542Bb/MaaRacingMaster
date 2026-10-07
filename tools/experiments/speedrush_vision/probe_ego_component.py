# -*- coding: utf-8 -*-
"""自车点云分离探针：种子 + 特征门 BFS 生长替代固定矩形挖除。

对照产线 ego_mask 矩形（挖走车前真路面），验证"车中心种子 + above-plane
高度残差连通域生长"能否只挖车、保住路面。生长规则（联网调研后修订）：
  - 迟滞双阈值：种子须 hgt<=-0.3m，生长收录 hgt<=-0.15m（Canny hysteresis
    结构；注意 Y 轴向下为正，「高于路面」= hgt 为负）；
  - 特征门防粘连：邻居须 |dhgt|<0.3m 且深度不跳变 |dZ|<1.0m——裸 label() 会把
    图像上相邻的所有高点（护栏/他车）连成一坨；
  - 绝对上限：|hgt|<2.5m、Z<Z_CAP（挡远场偏差带被顺着长）。
两遍平面：第一遍用矩形先验排除车点拟合平面，再算残差生长。

产物 <demo>/../depth_review/ego_component/ego_component_<帧号>.jpg：
  A 矩形挖除（产线现状） | B 特征门组件（红=组件，黄=边界，绿=保留路面）
  C 裸二值 label 对照（看粘连） | D BEV 组件落位
并打印：远场偏差幅度 vs 阈值余量、矩形吃掉的路面对照、组件 Z/X 范围。

用法：python probe_ego_component.py --demo <dir> --frames 200 500 650
      python probe_ego_component.py --img <a.jpg> <b.jpg>   （贴墙等散帧）
"""
from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.experiments.speedrush_vision import moge2_post as mp  # noqa: E402
from scipy import ndimage  # noqa: E402

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402

PROD_ONNX = (Path(__file__).resolve().parents[3]
             / "maaracing_master/plugins/speedrush/resources/onnx/depth"
             / "moge2_vits_static_336x598_t1032_q4f16.onnx")
YOLO_ONNX = (Path(__file__).resolve().parents[3]
             / "maaracing_master/plugins/speedrush/resources/onnx/perception"
             / "model.onnx")
YOLO_CONF, YOLO_IOU, YOLO_MARGIN = 0.35, 0.45, 10   # 产线 perception.py 同口径
EGO_RECT_FULL = (351, 533, 512, 766)   # y0,y1,x0,x1（产线 ego_mask.json）
ROAD_X_MAX = 12.0
ROAD_H = 0.15          # 路面级判阈值（弱门/停）。注意 Y 轴向下为正：
SEED_H = 0.30          # 「高于路面」= hgt 为负（实测车顶 hgt≈-1.2m）
D_HGT = 0.30           # 邻居高度相似门
D_Z = 1.00             # 邻居深度不跳变门
HGT_CAP = 2.5          # 组件绝对高度上限（|hgt|）
Z_CAP = 12.0           # 组件 Z 上限（挡远场偏差带被顺着长）
UP_BINS = ((4, 8), (8, 10), (10, 12), (12, 16), (16, 20), (20, 30))


def plane_fit(pts: np.ndarray, valid: np.ndarray) -> np.ndarray | None:
    """Y = aX + bZ + c 最小二乘（路带内、矩形先验已从 valid 排除）。"""
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    m = (valid & np.isfinite(Y) & (np.abs(X) < ROAD_X_MAX)
         & (Z > 4.0) & (Z < 14.0))
    if m.sum() < 500:
        return None
    A = np.stack([X[m], Z[m], np.ones(m.sum())], 1)
    coef, *_ = np.linalg.lstsq(A, Y[m], rcond=None)
    return coef


def grow_component(hgt: np.ndarray, Z: np.ndarray, valid: np.ndarray,
                   seed_mask: np.ndarray) -> np.ndarray:
    """图像空间 8-连通 BFS，逐边特征门。返回组件布尔掩码。"""
    H, W = hgt.shape
    comp = np.zeros((H, W), bool)
    q = deque()
    ys, xs = np.nonzero(seed_mask & valid)
    for y, x in zip(ys.tolist(), xs.tolist()):
        comp[y, x] = True
        q.append((y, x))
    nb = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    while q:
        y, x = q.popleft()
        h0, z0 = hgt[y, x], Z[y, x]
        for dy, dx in nb:
            ny, nx = y + dy, x + dx
            if not (0 <= ny < H and 0 <= nx < W) or comp[ny, nx] or not valid[ny, nx]:
                continue
            h1, z1 = hgt[ny, nx], Z[ny, nx]
            if not np.isfinite(h1) or not np.isfinite(z1):
                continue
            if h1 > -ROAD_H or h1 < -HGT_CAP or z1 >= Z_CAP:
                continue
            if abs(h1 - h0) >= D_HGT or abs(z1 - z0) >= D_Z:
                continue
            comp[ny, nx] = True
            q.append((ny, nx))
    return comp


def seed_region(rect: tuple[int, int, int, int], gh: int, gw: int) -> tuple[slice, slice]:
    """EGO_RECT 内缩的核——种子只在确属车体的像素里投。"""
    y0, y1, x0, x1 = rect
    sy0 = int(y0 * gh / 720 + 0.2 * (y1 - y0) * gh / 720)
    sy1 = int(y1 * gh / 720 - 0.1 * (y1 - y0) * gh / 720)
    sx0 = int(x0 * gw / 1280 + 0.2 * (x1 - x0) * gw / 1280)
    sx1 = int(x1 * gw / 1280 - 0.2 * (x1 - x0) * gw / 1280)
    return slice(sy0, sy1), slice(sx0, sx1)


def panel_rect(img: np.ndarray, rect: tuple[int, int, int, int]) -> np.ndarray:
    vis = img.copy()
    y0, y1, x0, x1 = rect
    vis[y0:y1, x0:x1] = (vis[y0:y1, x0:x1] * 0.35).astype(np.uint8)
    cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 0, 255), 2)
    cv2.putText(vis, "A  rect mask (prod)", (10, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return vis


def up(m: np.ndarray, Wimg: int, Himg: int) -> np.ndarray:
    return cv2.resize(m.astype(np.uint8), (Wimg, Himg),
                      interpolation=cv2.INTER_NEAREST).astype(bool)


def panel_component(img: np.ndarray, comp: np.ndarray, road: np.ndarray,
                    seed_mask: np.ndarray, Wimg: int, Himg: int) -> np.ndarray:
    vis = img.copy()
    c, r, s = up(comp, Wimg, Himg), up(road, Wimg, Himg), up(seed_mask, Wimg, Himg)
    vis[r] = (0.35 * vis[r] + np.array([0, 160, 0]) * 0.65).astype(np.uint8)
    vis[c] = (0.55 * vis[c] + np.array([0, 0, 220]) * 0.45).astype(np.uint8)
    cnts, _ = cv2.findContours(c.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnts, -1, (0, 255, 255), 2)
    vis[s] = (0.4 * vis[s] + np.array([0, 255, 255]) * 0.6).astype(np.uint8)
    cv2.putText(vis, "B  gated BFS component (red) / kept road (green)",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return vis


def panel_binary(img: np.ndarray, above: np.ndarray, seed_mask: np.ndarray,
                 Wimg: int, Himg: int) -> tuple[np.ndarray, dict]:
    lab, n = ndimage.label(above, structure=np.ones((3, 3), int))
    seed_labels = np.unique(lab[seed_mask & above])
    seed_labels = seed_labels[seed_labels > 0]
    vis = img.copy()
    rng = np.random.default_rng(0)
    others = up(~np.isin(lab, seed_labels) & above & (lab > 0), Wimg, Himg)
    vis[others] = (0.5 * vis[others] + np.array([0, 160, 0]) * 0.5).astype(np.uint8)
    for lb in seed_labels:
        m = up(lab == lb, Wimg, Himg)
        col = tuple(int(v) for v in rng.integers(60, 255, 3))
        vis[m] = (0.45 * vis[m] + np.array(col) * 0.55).astype(np.uint8)
    stats = {"n_comp": int(n), "seed_comp_ids": [int(v) for v in seed_labels]}
    cv2.putText(vis, f"C  raw binary label: {n} comps, seed merges {len(seed_labels)}",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return vis, stats


def panel_bev(pts: np.ndarray, comp: np.ndarray, road: np.ndarray,
              valid: np.ndarray) -> np.ndarray:
    X, Z = pts[..., 0], pts[..., 2]
    PX, ZL, ZH, XR = 24, 2.0, 18.0, 10.0
    W, H = int(2 * XR * PX), int((ZH - ZL) * PX)
    bev = np.full((H, W, 3), 28, np.uint8)
    m = valid & (Z > ZL) & (Z < ZH) & (np.abs(X) < XR)
    iz = ((Z[m] - ZL) * PX).astype(int).clip(0, H - 1)
    ix = ((X[m] + XR) * PX).astype(int).clip(0, W - 1)
    bev[iz, ix] = (90, 90, 90)
    for sel, col in ((road, (0, 200, 0)), (comp, (0, 0, 255))):
        mm = sel & m
        if mm.any():
            bev[((Z[mm] - ZL) * PX).astype(int).clip(0, H - 1),
                ((X[mm] + XR) * PX).astype(int).clip(0, W - 1)] = col
    for z in (5, 10, 15):
        cv2.line(bev, (0, int((z - ZL) * PX)), (W, int((z - ZL) * PX)), (70, 70, 70), 1)
        cv2.putText(bev, f"{z}m", (4, int((z - ZL) * PX) - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (160, 160, 160), 1)
    cv2.putText(bev, "D  BEV red=comp green=road", (4, 14),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    return cv2.resize(bev, (640, int(640 * H / W)), interpolation=cv2.INTER_NEAREST)


def panel_yolo(img: np.ndarray, dets: list[dict], boxmask: np.ndarray,
               comp: np.ndarray, Wimg: int, Himg: int) -> np.ndarray:
    """E 面板：产线 YOLO 框挖除（矩形+10px 外扩）对照。红框=car，金框=coin/bonus，
    品红填充=框挖除区，黄描边=自车组件（对照）。"""
    vis = img.copy()
    bm = up(boxmask, Wimg, Himg)
    vis[bm] = (0.55 * vis[bm] + np.array([220, 0, 220]) * 0.45).astype(np.uint8)
    c = up(comp, Wimg, Himg)
    cnts, _ = cv2.findContours(c.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnts, -1, (0, 255, 255), 2)
    for d in dets:
        x0 = int(max(d["cx"] - d["w"] / 2 - YOLO_MARGIN, 0))
        x1 = int(min(d["cx"] + d["w"] / 2 + YOLO_MARGIN, 1279))
        y0 = int(max(d["cy"] - d["h"] / 2 - YOLO_MARGIN, 0))
        y1 = int(min(d["cy"] + d["h"] / 2 + YOLO_MARGIN, 719))
        col = (0, 0, 255) if d["class_name"] == "car" else (0, 200, 255)
        cv2.rectangle(vis, (x0, y0), (x1, y1), col, 2)
        cv2.putText(vis, f"{d['class_name']} {d['conf']:.2f}", (x0, max(y0 - 6, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
    cv2.putText(vis, "E  prod YOLO box mask (magenta) vs ego component (yellow contour)",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return vis


def run_frame(img_bgr: np.ndarray, out_path: Path, sess, yolo, tag: str) -> None:
    img = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    Himg, Wimg = img.shape[:2]
    x = mp.preprocess(img, 598, 336)
    points, mask, mscale = mp.forward(sess, x, 1032)
    res = mp.reconstruct(points, mask, mscale)
    pts = res["pts"].astype(np.float32)
    valid_full = res["valid"]
    gh, gw = valid_full.shape

    # 两遍平面：第一遍矩形先验排除车点
    valid_pf = valid_full.copy()
    y0, y1, x0, x1 = EGO_RECT_FULL
    valid_pf[y0 * gh // 720:(y1 * gh // 720) + 1,
             x0 * gw // 1280:(x1 * gw // 1280) + 1] = False
    coef = plane_fit(pts, valid_pf)
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    hgt = Y - (coef[0] * X + coef[1] * Z + coef[2])

    # 种子：内缩矩形核内的强高点
    ss, sx = seed_region(EGO_RECT_FULL, gh, gw)
    seed_core = np.zeros((gh, gw), bool)
    seed_core[ss, sx] = True
    seed_mask = seed_core & valid_full & (hgt <= -SEED_H) & (Z < 10.0)
    n_seed = int(seed_mask.sum())
    if n_seed < 30:
        print(f"[{tag}] seed too few ({n_seed}) — skip")
        return
    med_hgt = float(np.median(hgt[seed_mask]))
    med_z = float(np.median(Z[seed_mask]))

    comp = grow_component(hgt, Z, valid_full, seed_mask)
    road = valid_full & (np.abs(hgt) < ROAD_H)
    above = valid_full & (hgt <= -ROAD_H) & np.isfinite(hgt)

    # 指标
    print(f"\n== {tag} ==")
    print(f"seed pts={n_seed} med_hgt={med_hgt:.2f}m med_Z={med_z:.2f}m")
    print("far-band deviation (|X|<8 median hgt per Z bin; 负=高于路面):")
    for zl, zh in UP_BINS:
        m = valid_full & (np.abs(X) < 8) & (Z >= zl) & (Z < zh)
        if m.sum() >= 20:
            print(f"  Z {zl:>2}-{zh:<2}m: median hgt {np.median(hgt[m]):+.3f} m  (n={int(m.sum())})")
    in_rect = np.zeros((gh, gw), bool)
    in_rect[y0 * gh // 720:(y1 * gh // 720) + 1,
            x0 * gw // 1280:(x1 * gw // 1280) + 1] = True
    road_eaten = int((in_rect & road & ~comp).sum())
    road_in_rect = int((in_rect & road).sum())
    comp_out_rect = int((comp & ~in_rect).sum())
    zc, xc = Z[comp], X[comp]
    print(f"comp pts={int(comp.sum())} Z[{zc.min():.1f},{zc.max():.1f}] "
          f"X[{xc.min():.1f},{xc.max():.1f}]")
    print(f"rect eats {road_eaten}/{road_in_rect} road-level px; "
          f"comp covers {comp_out_rect} px outside rect")

    # 渲染：半宽 640 等比缩放，A|B 一行、C|D 一行、E 整行
    half_w = 640
    pa = panel_rect(img, EGO_RECT_FULL)
    pb = panel_component(img, comp, road, seed_mask, Wimg, Himg)
    pc, bin_stats = panel_binary(img, above, seed_mask, Wimg, Himg)
    print(f"binary label: {bin_stats['n_comp']} comps, "
          f"seed-region ids: {bin_stats['seed_comp_ids']}")
    pd = panel_bev(pts, comp, road, valid_full)

    # YOLO 框挖除对照（产线 perception+module._yolo_object_mask 同口径）
    _cls, dets, _raw = yolo(img)
    dets_fmt = [{"class_name": d["class_name"], "conf": d["confidence"],
                 "cx": (d["box"][0] + d["box"][2]) // 2,
                 "cy": (d["box"][1] + d["box"][3]) // 2,
                 "w": d["box"][2] - d["box"][0], "h": d["box"][3] - d["box"][1]}
                for d in dets]
    boxmask = np.zeros((gh, gw), bool)
    upx = gw / 1280
    upy = gh / 720
    for d in dets_fmt:
        bx0 = int(max(d["cx"] - d["w"] / 2 - YOLO_MARGIN, 0) * upx)
        bx1 = int(min(d["cx"] + d["w"] / 2 + YOLO_MARGIN, 1279) * upx)
        by0 = int(max(d["cy"] - d["h"] / 2 - YOLO_MARGIN, 0) * upy)
        by1 = int(min(d["cy"] + d["h"] / 2 + YOLO_MARGIN, 719) * upy)
        boxmask[by0:by1 + 1, bx0:bx1 + 1] = True
    n_by_cls = {}
    for d in dets_fmt:
        n_by_cls[d["class_name"]] = n_by_cls.get(d["class_name"], 0) + 1
    bm_road = int((boxmask & road).sum())
    bm_comp = int((boxmask & comp).sum())
    print(f"yolo dets={n_by_cls} boxmask px={int(boxmask.sum())} "
          f"(eats road-level {bm_road}, overlaps ego comp {bm_comp})")
    pe = panel_yolo(img, dets_fmt, boxmask, comp, Wimg, Himg)

    def half(p: np.ndarray) -> np.ndarray:
        return cv2.resize(p, (half_w, int(p.shape[0] * half_w / p.shape[1])),
                          interpolation=cv2.INTER_AREA)
    pa, pb, pc, pd = half(pa), half(pb), half(pc), half(pd)
    row1 = np.full((max(pa.shape[0], pb.shape[0]), half_w * 2 + 4, 3), 15, np.uint8)
    row1[:pa.shape[0], :half_w] = pa
    row1[:pb.shape[0], half_w + 4:] = pb
    row2 = np.full((max(pc.shape[0], pd.shape[0]), half_w * 2 + 4, 3), 15, np.uint8)
    row2[:pc.shape[0], :half_w] = pc
    row2[:pd.shape[0], half_w + 4:] = pd
    eh = int(pe.shape[0] * (half_w * 2) / pe.shape[1])
    pe2 = cv2.resize(pe, (half_w * 2, eh), interpolation=cv2.INTER_AREA)
    row3 = np.full((eh, half_w * 2 + 4, 3), 15, np.uint8)
    row3[:eh, :half_w * 2] = pe2
    canvas = np.vstack([row1, row2, row3])
    cv2.imwrite(str(out_path), cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    print("wrote", out_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo")
    ap.add_argument("--frames", type=int, nargs="+")
    ap.add_argument("--img", nargs="+", help="单张 1280x720 raw jpg 直读（贴墙等散帧）")
    args = ap.parse_args()

    import onnxruntime as ort
    providers = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
                 if p in ort.get_available_providers()]
    sess = ort.InferenceSession(str(PROD_ONNX), providers=providers)
    yolo = YOLODetector(str(YOLO_ONNX), conf=YOLO_CONF, iou=YOLO_IOU)

    if args.img:
        for p in args.img:
            p = Path(p)
            out_dir = p.parent.parent / "depth_review" / "ego_component"
            out_dir.mkdir(parents=True, exist_ok=True)
            run_frame(cv2.imread(str(p)), out_dir / f"{p.stem}_ego.jpg", sess, yolo, p.name)
        return

    demo = Path(args.demo)
    rows = {r["seq"]: r for r in
            (json.loads(l) for l in (demo / "frames.jsonl").open(encoding="utf-8") if l.strip())}
    out_dir = demo.parent / "depth_review" / "ego_component"
    out_dir.mkdir(parents=True, exist_ok=True)
    for seq in args.frames:
        row = rows[seq]
        img = cv2.imread(str(demo / "frames" / row["file"]))
        run_frame(img, out_dir / f"ego_component_{seq:04d}.jpg", sess, yolo, f"frame {seq}")


if __name__ == "__main__":
    main()
