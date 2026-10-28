# -*- coding: utf-8 -*-
"""离线可视化：碰撞窗口的原始帧 + 程序视角俯视图（撞车复盘实验 2/8 佐证）。

只读 debug 帧与 trace，不接控制。输出到
%APPDATA%/MaaRacingMaster/data/speedrush/crash_viz/：
  crash_frames.jpg   —— 1550/1563/1578 三张 HUD 帧 + 当拍程序数字
  crash_bev.jpg      —— 1550/1554/1556/1563/1568 五拍俯视图（保持影子）

俯视图是**程序视角**：车位置取 trace 的 mcars x/z（车头系），失去纵深后
画最后已知位置并标 “last”。黄=生产选道，青=保持后选道，红线=最近前车。
"""

from __future__ import annotations

import glob
import json
import os
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_unknown_hold as H  # noqa: E402

SESS = "20261021_150545_p1"
LANE_W = H.LANE_W_M
FONT = r"C:\Windows\Fonts\msyh.ttc"
OUT = os.path.join(os.path.expandvars(r"%APPDATA%\MaaRacingMaster\data\speedrush"),
                   "crash_viz")


def _font(sz):
    return ImageFont.truetype(FONT, sz)


def load_rows():
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    path = glob.glob(os.path.join(base, SESS, "trace_*.jsonl"))[0]
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    t = 0.0
    for r in rows:
        r["_t"] = t
        t += r.get("dt") or 0.05
    return rows


def row_at(rows, fid):
    return min(rows, key=lambda r: abs(r["fid"] - fid))


def last_known(rows, fid, cid):
    for r in rows:
        if r["fid"] > fid:
            break
        for m in r.get("mcars") or []:
            if m["id"] == cid and m.get("z") is not None:
                return {"fid": r["fid"], "z": m["z"], "x": m.get("x"),
                        "v": m.get("v")}
    return None


def hold_table(rows):
    table = {}
    stats, rows_out, _details = H.replay(
        rows, 0.4, (1540, 1580), preempt_pass=True, pred_pass=True, v_cap=60.0)
    for fid, l0, h0, prod, base, hold, *_ in rows_out:
        table[fid] = {"l0": None if l0 is None else l0[1],
                      "h0": None if h0 is None else h0[1],
                      "prod": prod, "base": base, "hold": hold}
    return table, stats


def frame_panel(path, fid, tr, note=None):
    img = cv2.imread(path)
    crop = img[:, :1200]                      # 左半游戏画面，右半是 BEV
    crop = cv2.resize(crop, (600, 360))
    canvas = Image.new("RGB", (600, 452), (18, 18, 20))
    canvas.paste(Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)), (0, 0))
    d = ImageDraw.Draw(canvas)
    d.text((10, 368), f"fid {fid}（HUD 帧）", font=_font(20), fill=(240, 240, 240))
    d.text((10, 396),
           f"本车道净空 {fmt(tr['l0'])} → 保持 {fmt(tr['h0'])} m；"
           f"生产选道 {tr['prod']}；保持后 {tr['hold']}",
           font=_font(16), fill=(255, 210, 90))
    if note:
        d.text((10, 422), note, font=_font(13), fill=(150, 200, 255))
    return canvas


def fmt(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def bev_panel(fid, r, tr, last, w=340, h=440):
    img = Image.new("RGB", (w, h), (16, 18, 22))
    d = ImageDraw.Draw(img)
    d.text((8, 6), f"fid {fid}", font=_font(20), fill=(240, 240, 240))
    plot = (34, 36, w - 14, h - 90)
    x0, y0, x1, y1 = plot
    zmax = 32.0
    xmax = 7.0

    def px(x):
        return x0 + (x + xmax) / (2 * xmax) * (x1 - x0)

    def py(z):
        return y1 - max(0.0, min(z, zmax)) / zmax * (y1 - y0)

    ego = (r.get("coarse") or {}).get("ego") or 0.0
    prod = tr["prod"]
    hold = tr["hold"]
    # 自车轴（横向不画车道线：中心线在目标纵深处的偏移不在 trace 里，
    # 按 z=0 路心画线会把车归错道）
    d.line([(px(0), py(5)), (px(0), y1)], fill=(55, 75, 55), width=1)
    d.text((px(0) + 4, y0 + 4), "自车轴 x=0", font=_font(11), fill=(90, 140, 90))
    # 纵深刻度（相机纵深 z，车头基准在 5m）
    for z in (5, 10, 20, 30):
        d.line([(x0, py(z)), (x1, py(z))], fill=(45, 45, 50), width=1)
        d.text((4, py(z) - 7), f"{z}", font=_font(12), fill=(150, 150, 150))
    # 自车（相机基准 5m = 车头着地点）
    d.polygon([(px(0), py(5) - 12), (px(0) - 9, py(5) + 8),
               (px(0) + 9, py(5) + 8)], fill=(80, 230, 120))
    # 车
    for m in r.get("mcars") or []:
        cid = m["id"]
        x, z = m.get("x"), m.get("z")
        lost = z is None
        if lost:
            lk = last.get(cid)
            if not lk:
                continue
            x, z = lk["x"], lk["z"]
        if x is None or z is None:
            continue
        if not (5.0 <= z <= zmax) or not (-xmax <= x <= xmax):
            continue
        col = (235, 70, 70) if cid == 10 else (180, 180, 180)
        wpx = max(6, 1.8 / (2 * xmax) * (x1 - x0))
        lpx = max(8, 4.5 / zmax * (y1 - y0))
        box = [px(x) - wpx / 2, py(z) - lpx, px(x) + wpx / 2, py(z)]
        if lost:
            d.line([(px(0), py(z)), (px(x), py(z))], fill=(90, 90, 90), width=1)
            d.rectangle(box, outline=col, width=2)
            d.text((px(x) + 6, py(z) - 12),
                   f"{cid} 最后{z:.1f}m x={x:+.1f}", font=_font(12), fill=col)
        else:
            d.line([(px(0), py(z)), (px(x), py(z))], fill=(90, 90, 90), width=1)
            d.rectangle(box, fill=col)
            d.text((px(x) + 6, py(z) - 12),
                   f"{cid} 跟踪{z:.1f}m x={x:+.1f}", font=_font(12), fill=col)
    # 底栏：各道净空 + 选道
    d.text((8, h - 84),
           f"0道净空 {fmt(tr['l0'])} → 保持 {fmt(tr['h0'])} m",
           font=_font(15), fill=(255, 210, 90))
    d.text((8, h - 62), "各道净空（粗层平滑纵深−5m）",
           font=_font(11), fill=(160, 160, 160))
    band = " · ".join(
        f"{int(c[0]):+d}:{fmt(c[1])}" for c in
        ((r.get("coarse") or {}).get("cand") or []) if c[1] is not None)
    d.text((8, h - 46), band, font=_font(10), fill=(190, 190, 190))
    d.rectangle([8, h - 30, 20, h - 18], fill=(255, 200, 60))
    d.text((24, h - 32), f"生产选道 {prod}", font=_font(13), fill=(230, 230, 230))
    d.rectangle([128, h - 30, 140, h - 18], fill=(80, 220, 230))
    d.text((144, h - 32), f"保持后 {hold}", font=_font(13), fill=(230, 230, 230))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = load_rows()
    table, stats = hold_table(rows)
    dbg = os.path.join(os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush"), SESS)
    frames = [(1550, os.path.join(dbg, "0032_hud.jpg")),
              (1563, os.path.join(dbg, "0033_hud.jpg")),
              (1578, os.path.join(dbg, "0034_hud.jpg"))]
    notes = {
        1550: "白车在正前方（横向约 +0.1m）",
        1563: "腾空白车 = 1550 前车；程序此刻已无它的纵深",
        1578: "右侧蓝车：纵向 0.7m，横向约 2.4 道，粗层未计入本道",
    }
    fpanels = []
    for fid, path in frames:
        r = row_at(rows, fid)
        tr = table.get(r["fid"]) or table.get(fid)
        if tr is None:
            cand = (r.get("coarse") or {}).get("cand") or []
            l0 = next((c[1] for c in cand if abs(c[0]) < 1e-6), None)
            tr = {"l0": l0, "h0": l0,
                  "prod": (r.get("coarse") or {}).get("lane"),
                  "base": None, "hold": None}
        fpanels.append(frame_panel(path, fid, tr, notes.get(fid)))
    W = sum(p.width for p in fpanels) + 20 * (len(fpanels) - 1) + 40
    Hh = 492
    canvas = Image.new("RGB", (W, Hh), (10, 10, 12))
    d = ImageDraw.Draw(canvas)
    d.text((20, 8), "碰撞窗口：原始画面 vs 程序数字（左=1550 前车完好；中=1563 已撞；右=1578 撞后）",
           font=_font(20), fill=(240, 240, 240))
    x = 20
    for p in fpanels:
        canvas.paste(p, (x, 40))
        x += p.width + 20
    out1 = os.path.join(OUT, "crash_frames.jpg")
    canvas.save(out1, quality=92)

    # 俯视图时间线
    fids = [1550, 1554, 1556, 1563, 1568]
    panels = []
    last = {}
    for fid in fids:
        r = row_at(rows, fid)
        # 该拍之前每车最后已知
        for m in r.get("mcars") or []:
            if m.get("z") is not None:
                last[m["id"]] = {"z": m["z"], "x": m.get("x")}
        tr = table.get(r["fid"]) or {}
        tr = {**{"l0": None, "h0": None, "prod": None, "hold": None}, **tr}
        panels.append(bev_panel(fid, r, tr, dict(last)))
    W = sum(p.width for p in panels) + 16 * (len(panels) - 1) + 40
    Hh = 540
    canvas = Image.new("RGB", (W, Hh), (10, 10, 12))
    d = ImageDraw.Draw(canvas)
    d.text((20, 8), "程序视角俯视图：1550 正常 → 1554 最后纵深 → 1556 丢纵深（保持生效）→ 1563 已撞 → 1568 释放",
           font=_font(19), fill=(240, 240, 240))
    d.text((20, 34), "横轴=自车帧 x(m)；纵轴=相机纵深 z(m)，绿三角=车头(5m)；"
                     "红车=碰撞前车（空心=已失去纵深，画最后已知位置）；"
                     "底栏=各道净空（无保持→保持）；黄=生产选道，青=保持后选道",
           font=_font(13), fill=(180, 180, 180))
    x = 20
    for p in panels:
        canvas.paste(p, (x, 62))
        x += p.width + 16
    out2 = os.path.join(OUT, "crash_bev.jpg")
    canvas.save(out2, quality=92)
    print(out1)
    print(out2)
    print("hold stats:", {k: stats[k] for k in
                          ("protect", "unnecessary", "hold_ticks", "lane_changed")})


if __name__ == "__main__":
    main()
