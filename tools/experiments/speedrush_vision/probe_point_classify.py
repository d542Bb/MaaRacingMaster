# -*- coding: utf-8 -*-
"""点云归类探针：用「后处理贴标签」替代「源头挖除」。

产线现状（depth_geo.reading_from_points）：ego 矩形 ∪ YOLO 框先 OR 成 dig
掩码，平面拟合/找边之前整块挖掉——框落在远处走廊时，远场路面信息随之
丢失（2026-10-07 维护者指正）。本探针演示同帧的替代结构：
  不删任何点 → 全点云贴标签 → 消费方自选。

标签：ego（种子+特征门 BFS）/ car / coin / bonus_car（above-plane 组件与
YOLO 框重叠≥30% 归类，框只当标签源）/ 抬升未知（YOLO 漏检的物体，如强
模糊敌车——仍是独立元素，只是没名字）/ 路面近（|hgt|<0.15 且 Z<14，可靠）/
路面远·弱标（Z≥14 且 |hgt|<0.6，受远场偏差影响只作参考）。
平面拟合用两遍稳健拟合（剔除外点），无任何先验挖除。

用法：python probe_point_classify.py --img <a.jpg> <b.jpg>
产物：<img所在session>/depth_review/ego_component/<stem>_classify.jpg
打印：今天会被挖掉的点的构成 vs 归类方案的标签构成。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.experiments.speedrush_vision import moge2_post as mp  # noqa: E402
from scipy import ndimage  # noqa: E402
from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402
from tools.experiments.speedrush_vision.probe_ego_component import (  # noqa: E402
    grow_component, seed_region, EGO_RECT_FULL, YOLO_ONNX,
    YOLO_CONF, YOLO_IOU, YOLO_MARGIN)

ROAD_X_MAX = 12.0
ROAD_H = 0.15        # 路面级带（近场可靠）
FAR_H = 0.60         # 远场弱标带
FAR_Z = 14.0
YOLO_OVERLAP = 0.30  # 组件划给 YOLO 框的重叠率门

# 标签 → RGB
COL = {
    "ego":        (0, 220, 220),
    "car":        (0, 0, 255),
    "coin":       (0, 200, 255),
    "bonus_car":  (0, 160, 255),
    "unknown":    (150, 150, 150),
    "road_near":  (0, 180, 0),
    "road_far":   (0, 90, 0),
}


def robust_plane_fit(pts: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Y=aX+bZ+c 两遍稳健拟合：拟合→剔 |残差−中位|>0.15 →重拟合×3。无先验挖除。"""
    X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
    m = valid & np.isfinite(Y) & (np.abs(X) < ROAD_X_MAX) & (Z > 4.0) & (Z < FAR_Z)
    A = np.stack([X[m], Z[m], np.ones(int(m.sum()))], 1)
    yv = Y[m]
    coef, *_ = np.linalg.lstsq(A, yv, rcond=None)
    for _ in range(3):
        r = yv - (coef[0] * A[:, 0] + coef[1] * A[:, 1] + coef[2])
        keep = np.abs(r - np.median(r)) < 0.15
        coef, *_ = np.linalg.lstsq(A[keep], yv[keep], rcond=None)
    return coef


def up(m: np.ndarray, Wimg: int, Himg: int) -> np.ndarray:
    return cv2.resize(m.astype(np.uint8), (Wimg, Himg),
                      interpolation=cv2.INTER_NEAREST).astype(bool)


def tint(vis: np.ndarray, m: np.ndarray, col, k=0.5) -> None:
    vis[m] = ((1 - k) * vis[m] + np.array(col) * k).astype(np.uint8)


def panel_today(img, del_full, rect, boxes, Wimg, Himg) -> np.ndarray:
    vis = img.copy()
    vis[up(del_full, Wimg, Himg)] = 0
    y0, y1, x0, x1 = rect
    cv2.rectangle(vis, (x0, y1), (x1, y0), (0, 0, 255), 2)
    for b in boxes:
        cv2.rectangle(vis, (b["x0"], b["y0"]), (b["x1"], b["y1"]), (255, 255, 255), 1)
    cv2.putText(vis, "A  TODAY: dug out before plane fit (black = gone)",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return vis


def panel_classify(img, lab_img, lab_up_idx, boxes, comp_ego, Wimg, Himg) -> np.ndarray:
    vis = img.copy()
    for i, k in enumerate(COL, 1):
        mm = lab_up_idx == i
        if mm.any():
            tint(vis, mm, COL[k], 0.55 if k != "road_far" else 0.35)
    cnts, _ = cv2.findContours(up(comp_ego, Wimg, Himg).astype(np.uint8),
                               cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnts, -1, (0, 255, 255), 2)
    for b in boxes:
        cv2.rectangle(vis, (b["x0"], b["y0"]), (b["x1"], b["y1"]), (255, 255, 255), 1)
    cv2.putText(vis, "B  CLASSIFY: nothing deleted, all labeled (white box = label source)",
                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return vis


def bev(pts, lab, valid, W) -> np.ndarray:
    X, Z = pts[..., 0], pts[..., 2]
    PX, ZL, ZH, XR = 24, 2.0, 18.0, 10.0
    Hh = int((ZH - ZL) * PX)
    bev = np.full((Hh, int(2 * XR * PX), 3), 28, np.uint8)
    m = valid & (Z > ZL) & (Z < ZH) & (np.abs(X) < XR)
    iz = ((Z[m] - ZL) * PX).astype(int).clip(0, Hh - 1)
    ix = ((X[m] + XR) * PX).astype(int).clip(0, W - 1)
    names = {k: np.array(v) for k, v in COL.items()}
    for name, col in names.items():
        mm = m & (lab == name)
        if mm.any():
            bev[((Z[mm] - ZL) * PX).astype(int).clip(0, Hh - 1),
                ((X[mm] + XR) * PX).astype(int).clip(0, W - 1)] = col
    for z in (5, 10, 15):
        cv2.line(bev, (0, int((z - ZL) * PX)), (bev.shape[1], int((z - ZL) * PX)), (70, 70, 70), 1)
    cv2.putText(bev, "left: today (dug)   right: classify (all labeled)", (4, 14),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
    return cv2.resize(bev, (640, int(640 * Hh / bev.shape[1])), interpolation=cv2.INTER_NEAREST)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", nargs="+", required=True)
    args = ap.parse_args()

    import onnxruntime as ort
    providers = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
                 if p in ort.get_available_providers()]
    sess = ort.InferenceSession(str(YOLO_ONNX.parent.parent / "depth"
                                    / "moge2_vits_static_336x598_t1032_q4f16.onnx"),
                                providers=providers)
    yolo = YOLODetector(str(YOLO_ONNX), conf=YOLO_CONF, iou=YOLO_IOU)

    for p in args.img:
        p = Path(p)
        img = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        Himg, Wimg = img.shape[:2]
        x = mp.preprocess(img, 598, 336)
        points, mask, mscale = mp.forward(sess, x, 1032)
        res = mp.reconstruct(points, mask, mscale)
        pts = res["pts"].astype(np.float32)
        valid = res["valid"]
        gh, gw = valid.shape
        X, Y, Z = pts[..., 0], pts[..., 1], pts[..., 2]
        coef = robust_plane_fit(pts, valid)
        hgt = Y - (coef[0] * X + coef[1] * Z + coef[2])

        # 归类
        road_near = valid & (np.abs(hgt) < ROAD_H) & (Z < FAR_Z)
        road_far = valid & (np.abs(hgt) < FAR_H) & (Z >= FAR_Z)
        above = valid & (hgt <= -ROAD_H) & np.isfinite(hgt)
        ss, sx = seed_region(EGO_RECT_FULL, gh, gw)
        seed_core = np.zeros((gh, gw), bool)
        seed_core[ss, sx] = True
        seed = seed_core & valid & (hgt <= -0.30) & (Z < 10.0)
        comp_ego = grow_component(hgt, Z, valid, seed)

        lab, n = ndimage.label(above & ~comp_ego, structure=np.ones((3, 3), int))
        lab_img = np.full((gh, gw), "", dtype=object)
        lab_img[road_near] = "road_near"
        lab_img[comp_ego] = "ego"

        # YOLO 框：只当标签源。远场物体的高度被远场偏差抵消、进不了
        # above-plane（实测 20m 外卡车整体落回路面级带），所以框内非近场
        # 路面的点直接按框贴类别；框的矩形边不修正（探针口径）。
        _cls, dets, _raw = yolo(img)
        boxes = []
        boxmask_g = np.zeros((gh, gw), bool)
        n_obj = {"car": 0, "coin": 0, "bonus_car": 0, "unknown": 0}
        for d in dets:
            bx0 = int(max(d["box"][0] - YOLO_MARGIN, 0))
            bx1 = int(min(d["box"][2] + YOLO_MARGIN, 1279))
            by0 = int(max(d["box"][1] - YOLO_MARGIN, 0))
            by1 = int(min(d["box"][3] + YOLO_MARGIN, 719))
            boxes.append({"cls": d["class_name"], "conf": d["confidence"],
                          "x0": bx0, "y0": by0, "x1": bx1, "y1": by1})
            boxmask_g[int(by0 * gh / 720):int(by1 * gh / 720) + 1,
                      int(bx0 * gw / 1280):int(bx1 * gw / 1280) + 1] = True
            if d["class_name"] in COL:
                bm = np.zeros((gh, gw), bool)
                bm[int(by0 * gh / 720):int(by1 * gh / 720) + 1,
                   int(bx0 * gw / 1280):int(bx1 * gw / 1280) + 1] = True
                sel = bm & (lab_img == "") & valid
                lab_img[sel] = d["class_name"]
                if sel.any():
                    n_obj[d["class_name"]] += 1

        # 剩余 above-plane 组件 = 抬升未知（含 YOLO 漏检的敌车——仍是元素，只是没名字）
        for lb in range(1, n + 1):
            cm = lab == lb
            if cm.sum() < 8 or (lab_img[cm] != "").any():
                continue
            lab_img[cm] = "unknown"
            n_obj["unknown"] += 1
        lab_img[road_far & (lab_img == "")] = "road_far"

        # 实体化：每个标签主体给三维统计（未来 3D 规划器的坐标距离数据）。
        # ego=一个主体；每个 YOLO 框=一个主体（框内有效点的中位 Z/X，框本身
        # 就是模型的物体假设）；近场漏检物体（above-plane 无框组件）也成主体。
        def stat(sel: np.ndarray) -> tuple | None:
            n = int(sel.sum())
            if n < 10:
                return None
            zv = Z[sel]
            # Z 直方图取最密簇（框内常混入框缘外的天空/背景远点，纯中位被拖飞）
            lo, hi = np.percentile(zv, [2, 98])
            hist, edges = np.histogram(zv, bins=24, range=(lo, hi))
            c = 0.5 * (edges[hist.argmax()] + edges[hist.argmax() + 1])
            core = sel & (np.abs(Z - c) < (hi - lo) / 24)
            n2 = int(core.sum())
            if n2 < 10:
                core, n2 = sel, n
            return (n2, float(np.median(Z[core])), float(np.median(X[core])),
                    float(Z[core].min()), float(Z[core].max()))

        entities = []
        st = stat(comp_ego)
        if st:
            entities.append(("ego", 1.0, st))
        for b in boxes:
            if b["cls"] not in COL:
                continue
            bm = np.zeros((gh, gw), bool)
            bm[int(b["y0"] * gh / 720):int(b["y1"] * gh / 720) + 1,
               int(b["x0"] * gw / 1280):int(b["x1"] * gw / 1280) + 1] = True
            st = stat(bm & valid & ~comp_ego)
            if st:
                entities.append((b["cls"], b["conf"], st))
        for lb in range(1, n + 1):
            cm = lab == lb
            if cm.sum() < 30 or (lab_img[cm] != "").any():
                continue
            st = stat(cm & valid)
            if st and st[1] < 16.0:
                entities.append(("unknown", 0.0, st))
        print("entities (cls, conf, px, Z_med m, X_med m, Z_range m):")
        for name, conf, st in sorted(entities, key=lambda e: e[2][1]):
            print(f"  {name:<10} conf={conf:.2f} px={st[0]:>6} "
                  f"Z={st[1]:5.1f}m X={st[2]:+5.1f}m Z[{st[3]:.1f},{st[4]:.1f}]")

        # 指标：今天被挖的 vs 归类的
        rect_g = np.zeros((gh, gw), bool)
        y0, y1, x0, x1 = EGO_RECT_FULL
        rect_g[y0 * gh // 720:(y1 * gh // 720) + 1,
               x0 * gw // 1280:(x1 * gw // 1280) + 1] = True
        del_today = (rect_g | boxmask_g) & valid
        print(f"\n== {p.name} ==")
        print(f"today digs {int(del_today.sum())} pts of {int(valid.sum())}: "
              f"road_near {int((del_today & road_near).sum())}, "
              f"road_far(weak) {int((del_today & road_far).sum())}, "
              f"above-plane(object) {int((del_today & above).sum())}")
        cnt = {k: int((lab_img == k).sum()) for k in COL}
        print(f"classify labels px: {cnt}")
        print(f"components: ego=1, car={n_obj['car']}, coin={n_obj['coin']}, "
              f"bonus_car={n_obj['bonus_car']}, unknown(YOLO漏检)={n_obj['unknown']} "
              f"(raw comps={n})")

        # 渲染：A|B 一行，C|D 一行（BEV 左=挖除口径 右=归类口径）
        del_full = del_today
        pa = panel_today(img, del_full, EGO_RECT_FULL, boxes, Wimg, Himg)
        lab_up = up(np.isin(lab_img, list(COL)).astype(np.uint8), Wimg, Himg)
        # 逐标签上色图（用索引图避免 object 数组 resize）
        lab_idx = np.zeros((gh, gw), np.uint8)
        for i, k in enumerate(COL, 1):
            lab_idx[lab_img == k] = i
        lab_up_idx = cv2.resize(lab_idx, (Wimg, Himg), interpolation=cv2.INTER_NEAREST)
        pb = img.copy()
        for i, k in enumerate(COL, 1):
            mm = lab_up_idx == i
            if mm.any():
                tint(pb, mm, COL[k], 0.55 if k != "road_far" else 0.35)
        cnts, _ = cv2.findContours(up(comp_ego, Wimg, Himg).astype(np.uint8),
                                   cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(pb, cnts, -1, (0, 255, 255), 2)
        for b in boxes:
            cv2.rectangle(pb, (b["x0"], b["y0"]), (b["x1"], b["y1"]), (255, 255, 255), 1)
        cv2.putText(pb, "B  CLASSIFY: nothing deleted, all labeled",
                    (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        legend = "  ".join(f"{k}={int((lab_img == k).sum())}" for k in COL)
        cv2.putText(pb, legend, (10, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1)

        # BEV 对：左=今天挖除后剩的点，右=归类全点
        Xb, Zb = X, Z
        PX, ZL, ZH, XR = 24, 2.0, 18.0, 10.0
        Wb, Hb = int(2 * XR * PX), int((ZH - ZL) * PX)

        def bev_one(sel_valid, use_del):
            b = np.full((Hb, Wb, 3), 28, np.uint8)
            m = sel_valid & (Zb > ZL) & (Zb < ZH) & (np.abs(Xb) < XR)
            if use_del:
                m = m & ~del_today
            iz = ((Zb[m] - ZL) * PX).astype(int).clip(0, Hb - 1)
            ix = ((Xb[m] + XR) * PX).astype(int).clip(0, Wb - 1)
            b[iz, ix] = (90, 90, 90)
            for i, k in enumerate(COL, 1):
                mm = m & (lab_idx == i)
                if mm.any():
                    b[((Zb[mm] - ZL) * PX).astype(int).clip(0, Hb - 1),
                      ((Xb[mm] + XR) * PX).astype(int).clip(0, Wb - 1)] = COL[k]
            for z in (5, 10, 15):
                cv2.line(b, (0, int((z - ZL) * PX)), (Wb, int((z - ZL) * PX)), (70, 70, 70), 1)
            return cv2.resize(b, (640, int(640 * Hb / Wb)), interpolation=cv2.INTER_NEAREST)

        pc = bev_one(valid, use_del=True)
        cv2.putText(pc, "C  TODAY BEV: dug points gone", (6, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        pd_ = bev_one(valid, use_del=False)
        cv2.putText(pd_, "D  CLASSIFY BEV: all points kept", (6, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        half_w = 640
        ph = int(half_w * Himg / Wimg)

        def half(pn: np.ndarray) -> np.ndarray:
            return cv2.resize(pn, (half_w, int(pn.shape[0] * half_w / pn.shape[1])),
                              interpolation=cv2.INTER_AREA)
        pa, pb = half(pa), half(pb)
        row1 = np.full((max(pa.shape[0], pb.shape[0]), half_w * 2 + 4, 3), 15, np.uint8)
        row1[:pa.shape[0], :half_w] = pa
        row1[:pb.shape[0], half_w + 4:] = pb
        row2 = np.full((max(pc.shape[0], pd_.shape[0]), half_w * 2 + 4, 3), 15, np.uint8)
        row2[:pc.shape[0], :half_w] = pc
        row2[:pd_.shape[0], half_w + 4:] = pd_
        canvas = np.vstack([row1, row2])
        out_dir = p.parent.parent / "depth_review" / "ego_component"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{p.stem}_classify.jpg"
        cv2.imwrite(str(out), cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
        print("wrote", out)


if __name__ == "__main__":
    main()
