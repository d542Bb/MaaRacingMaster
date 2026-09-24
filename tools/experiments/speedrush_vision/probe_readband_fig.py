# -*- coding: utf-8 -*-
"""读数带失败的可视化取证（probe_readband 的 follow-up，2026-09-24）。

维护者判断：肉眼看深度图边界清晰 ⇒ 低覆盖必有管线丢信号。本探针把失败帧的
每一级中间产物画出来对齐裁决：
P1 原帧 + 金标线（L 绿 / R 青）+ 候选拟合线（红=当选，橙=次选）
P2 相对偏离 r 伪彩（clip −2%~12%，JET）+ 金标线
P3 门掩码链：r>gate（白）→ hold 收紧后（蓝）→ 当选块（绿）+ ego 挖除框（黄）
P4 金标线外侧 ±(10~60px) 条带的 r 剖面（y 轴=行号）——"边在不在门上方"的
   直接证据：若剖面 >gate 而管线无读数，丢在块/守卫；若剖面 <gate，门吃掉了信号。

每侧打印候选块判决链（按跨度降序）：span/touch/n_src/fit/conv/resid/lane/floor/dev。
输出：depth_review/readband_fail_<key>.jpg；用法：
    python tools/experiments/speedrush_vision/probe_readband_fig.py [--limit 24]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib, x_lane_of  # noqa: E402

import probe_crop_quality as pcq  # noqa: E402
import probe_depth as pd  # noqa: E402
from probe_readband import NPY, EGO, fit2pass, gold_x_at  # noqa: E402

CAL = load_calib()
FLOOR, N, CONV = 50, 20, dg.CONV_MAX_PX


def side_chain(rmap, blocks, side, r):
    """该侧候选块的完整判决链（生产选择序：跨度降序，首个全过者当选）。"""
    tkey = 4 if side == "L" else 5
    chain = []
    winner = None
    for b in sorted((b for b in blocks if b[tkey]), key=lambda b: b[1] - b[0], reverse=True):
        y0, y1, il, ir = b[0], b[1], b[2], b[3]
        inner = il if side == "L" else ir
        src = [y for y in inner if y - CAL.y_h >= FLOOR]
        rec = {"span": y1 - y0, "n_src": len(src)}
        if len(src) < N:
            rec["fail"] = f"源行不足({len(src)}<{N})"
            chain.append(rec)
            continue
        f = fit2pass({y: inner[y] for y in src})
        if f is None:
            rec["fail"] = "拟合行数不足"
            chain.append(rec)
            continue
        sl, ic, resid, nu = f
        conv = abs(sl * CAL.y_h + ic - CAL.vpx)
        y_ref = float(np.median(src))
        x_ref = sl * y_ref + ic
        lane = x_lane_of(int(round(x_ref)), int(round(y_ref)), CAL)
        rec.update(conv=round(conv), resid=round(resid, 1), lane=round(lane, 2),
                   sl=round(sl, 3), ic=round(ic))
        if conv > CONV or resid > dg.RESID_MAX_PX:
            rec["fail"] = "conv/resid"
        elif abs(lane) < dg.LANE_SIDE_MIN or (side == "L") != (lane < 0):
            rec["fail"] = "侧别"
        else:
            from probe_readband import baseline_ok_at
            if not baseline_ok_at(rmap, inner, side, y_ref, dg.GATE):
                rec["fail"] = "门本底"
            else:
                rec["fail"] = None
                gx = gold_x_at(r, side.lower(), y_ref)
                if gx is not None:
                    rec["dev"] = round(lane - x_lane_of(int(round(gx)), int(round(y_ref)), CAL), 2)
                winner = rec
        chain.append(rec)
        if winner:
            break
    return winner, chain


def gold_profile(rmap, label_row, side):
    """金标线外侧条带的 r 剖面：每 10 行取 (edge+10 .. edge+60) 的 r 中位。"""
    out = []
    for y in range(dg.Y0, dg.DIAG_Y1, 10):
        gx = gold_x_at(label_row, side, y)
        if gx is None:
            continue
        lo = int(gx + (10 if side == "r" else -60))
        hi = lo + 50
        lo, hi = max(lo, 0), min(hi, 1280)
        if hi - lo < 10:
            continue
        v = rmap[y, lo:hi]
        v = v[np.isfinite(v)]
        if len(v):
            out.append((y, float(np.median(v)), gx))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=24)
    args = ap.parse_args()
    labels = [x for x in csv.DictReader((pd.OUT / "gold_labels.csv").open(encoding="utf-8"))]
    picked = []
    for x in labels:
        p = Path(x["path"])
        cache = NPY / f"{pcq.frame_key(p)}__d336q4f16.npy"
        if cache.exists():
            picked.append((x, p, cache))
    print(f"金标×缓存帧 {len(picked)}，逐帧判决链（新规则 FLOOR={FLOOR} N={N}）")
    n_none = n_lonly = 0
    for x, p, cache in picked:
        m = np.load(cache).astype(np.float32)
        rmap, blocks = dg._rel_and_blocks(m, EGO)
        wl, cl = side_chain(rmap, blocks, "L", x)
        wr, cr = side_chain(rmap, blocks, "R", x)
        if not wl and not wr:
            n_none += 1
        if wl and not wr:
            n_lonly += 1
        tag = ("双侧成" if (wl and wr) else "仅L" if wl else "仅R" if wr else "双侧败")
        print(f"\n== {x['path'].split(chr(92))[-2:]} {tag}")
        for side, ch in (("L", cl), ("R", cr)):
            for i, rec in enumerate(ch[:3]):
                print(f"  {side}#{i} span={rec['span']} nsrc={rec['n_src']} "
                      + " ".join(f"{k}={v}" for k, v in rec.items() if k in
                                 ("conv", "resid", "lane", "dev")) + f" → {rec['fail'] or '当选'}")
        prof = gold_profile(rmap, x, "r")
        above = [v for _y, v, _gx in prof if v > dg.GATE]
        print(f"  R金标外条带 r：{len(above)}/{len(prof)} 行 >gate(5%)，"
              f"中位 {np.median([v for _y, v, _g in prof]) * 100:.1f}%" if prof else "  R剖面无行")
    print(f"\n汇总：双侧败 {n_none} / 仅L {n_lonly} / 共 {len(picked)}")

    # 出图：双侧败 + 仅L 各取前若干
    done = 0
    for x, p, cache in picked:
        if done >= args.limit:
            break
        m = np.load(cache).astype(np.float32)
        rmap, blocks = dg._rel_and_blocks(m, EGO)
        wl, cl = side_chain(rmap, blocks, "L", x)
        wr, cr = side_chain(rmap, blocks, "R", x)
        if wl and wr:
            continue
        done += 1
        key = pcq.frame_key(p)
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        ya, yb = dg.Y0, dg.DIAG_Y1

        def draw_fit(img, rec, col, thick=2):
            cv2.line(img, (int(rec["sl"] * ya + rec["ic"]), ya),
                     (int(rec["sl"] * yb + rec["ic"]), yb), col, thick)

        # P1 原帧(RGB) + 金标 + 拟合线（红=当选，蓝=被拒首候选）
        p1 = rgb.copy()
        for side, col in (("l", (0, 200, 0)), ("r", (255, 200, 0))):
            g0, g1 = gold_x_at(x, side, ya), gold_x_at(x, side, yb)
            if g0 is not None and g1 is not None:
                cv2.line(p1, (int(g0), ya), (int(g1), yb), col, 2)
        for w, ch in ((wl, cl), (wr, cr)):
            if w:
                draw_fit(p1, w, (255, 0, 0))
            elif ch and "sl" in ch[0]:
                draw_fit(p1, ch[0], (0, 0, 255))
        # P2 r 伪彩
        rc = np.clip((rmap + 0.02) / 0.14, 0, 1)
        rc[~np.isfinite(rmap)] = 0
        p2 = cv2.applyColorMap((rc * 255).astype(np.uint8), cv2.COLORMAP_JET)
        for side, col in (("l", (0, 255, 0)), ("r", (255, 255, 0))):
            g0, g1 = gold_x_at(x, side, ya), gold_x_at(x, side, yb)
            if g0 is not None and g1 is not None:
                cv2.line(p2, (int(g0), ya), (int(g1), yb), col, 2)
        # P3 门掩码链（BGR 语义：灰=过门，橙=hold 后，绿点=入选级块内沿，
        # 红/蓝线=当选/被拒首候选拟合，紫/青线=金标 L/R，黄框=ego 挖除）
        over = (np.nan_to_num(rmap, nan=-1) > dg.GATE).astype(np.uint8) * 60
        hold = cv2.filter2D((over > 0).astype(np.float32), -1,
                            np.ones((1, dg.HOLD), np.float32)) >= dg.HOLD
        p3 = np.zeros((720, 1280, 3), np.uint8)
        p3[over > 0] = (90, 90, 90)
        p3[hold] = (0, 165, 255)
        for b in blocks:
            for d in (b[2], b[3]):
                for yy, xx in d.items():
                    cv2.circle(p3, (xx, yy), 1, (0, 255, 0), -1)
        for w, ch in ((wl, cl), (wr, cr)):
            if w:
                draw_fit(p3, w, (0, 0, 255))
            elif ch and "sl" in ch[0]:
                draw_fit(p3, ch[0], (255, 0, 0))
        for side, col in (("l", (255, 0, 255)), ("r", (0, 255, 255))):
            g0, g1 = gold_x_at(x, side, ya), gold_x_at(x, side, yb)
            if g0 is not None and g1 is not None:
                cv2.line(p3, (int(g0), ya), (int(g1), yb), col, 1)
        if EGO is not None:
            ys, xs = np.nonzero(EGO)
            cv2.rectangle(p3, (int(xs.min()), int(ys.min())),
                          (int(xs.max()), int(ys.max())), (0, 255, 255), 2)
        # P4 金标外侧条带 r 剖面（x=r 值，y=行号；白竖线=gate 5%）
        p4 = np.full((720, 1280, 3), 30, np.uint8)
        for i, side in enumerate(("l", "r")):
            for y, v, _gx in gold_profile(rmap, x, side):
                px = 200 + i * 560 + int(np.clip(v, -0.02, 0.15) / 0.17 * 460)
                cv2.circle(p4, (px, y), 3, (0, 0, 255) if side == "r" else (0, 255, 0), -1)
            gx0 = 200 + i * 560 + int(0.05 / 0.17 * 460)
            cv2.line(p4, (gx0, ya), (gx0, yb), (255, 255, 255), 1)
            cv2.putText(p4, f"{side.upper()} gold-outside r (white=gate 5%)",
                        (160 + i * 560, 330), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1)
        p1b = cv2.cvtColor(p1, cv2.COLOR_RGB2BGR)
        for pan, txt in ((p1b, "P1 rgb+gold+fit"), (p2, "P2 r-jet"),
                         (p3, "P3 mask-chain"), (p4, "P4 gold-outside r vs gate")):
            cv2.putText(pan, txt, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 2)
        g = np.vstack([np.hstack([p1b, p2]), np.hstack([p3, p4])])
        cv2.putText(g, f"{key} L={'OK' if wl else 'FAIL'} R={'OK' if wr else 'FAIL'}",
                    (8, 44), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 2)
        out = pd.OUT / f"readband_fail_{key}.jpg"
        cv2.imwrite(str(out), g)
    print(f"图已出：depth_review/readband_fail_*.jpg（{done} 张，P1 原帧+金标 P2 r伪彩 "
          f"P3 门掩码链 P4 金标外 r 剖面）")


if __name__ == "__main__":
    main()
