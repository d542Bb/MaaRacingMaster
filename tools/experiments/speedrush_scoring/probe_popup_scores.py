#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""speedrush 得分浮字读数探针：把画面上的 `+30 / +90 / +120 …` 读出来，定「分从哪来」。

**要回答什么**：右上面板每秒进账约 2.8 个「30 分单位」，但这些单位**是金币、普通超车还是
极限动作**——三者的面额分别是 30 / 30 / 120，光看分值分不出（`RULES.md` §4.8）。浮字串的
**字形数**只能把面额分成 3 字形 `{30,60,90}` 与 4 字形 `{120,150,180}` 两类；要落到具体数值，
必须真的把数字**读**出来。本探针就是干这个的。

**怎么读**（复用 `speedrush_scoring` 的既有形态，零新依赖）：
  ① 亮黄掩码（浮字是亮黄 + 深描边）→ 横向闭运算把字形连成串；
  ② 宽串按**列投影的空白间隙**切开（避免把并排的两个 `+30` 连成一个 `+3030`）；
  ③ 每个串交 `HudOcr`（RapidOCR、关 det、与 HUD 读数同一套预处理）读文本；
  ④ 取数字后**吸附到合法面额**（30 的整数倍，`RULES.md` §4.2/§4.11 的面额是封闭离散集），
     偏离超过半个面额就丢弃——被车身遮末位的残串（如 `+12`）由此被拒，而不是被当成 12 分；
  ⑤ 跨帧用质心最近邻去重（同一浮字可见约 0.13~0.4 s），一条轨迹只记一次。

**自检口径（这是本探针的价值所在）**：把各面额按次数加权求和得「分/秒」，与**比分侧的非里程收入**
（面板速率 − 里程速率）对照。两者必须同量级——**对不上就说明数重了或漏了**，比任何"看起来合理"都硬。
`--score-series` 给出比分序列 JSONL（`{"seq","ts","score"}`，由 `probe_hud_ocr.py` 的读数产物提供）
时，脚本会自己把两侧都算出来。

**模式**
  scan   整段会话读数 + 面额直方图 + 与比分侧对账。
  sheet  只跑若干帧，把候选裁片与 OCR 结果拼成图，供**目视复核**（读数准不准，人眼必须先看一遍）。

用法：
    python tools/experiments/speedrush_scoring/probe_popup_scores.py sheet --session 20260917_220750_p2
    python tools/experiments/speedrush_scoring/probe_popup_scores.py scan  --session 20260917_220750_p2 \
        --score-series <path.jsonl> --out <dir>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

_HERE = Path(__file__).resolve().parent
DEFAULT_DEMOS = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
                 / "demos")

# 浮字区：自车周围 + 前方路面（覆盖玩家侧与世界侧浮字），避开左上卡片与右上比分面板所在的顶部。
ZONE = (60, 205, 1220, 690)
# 亮黄掩码（与 speedrush_planning 的浮字成本实测同源）
H_MIN, H_MAX, S_MIN, V_MIN = 14, 42, 120, 180
GLYPH_GAP = 24            # 同一串内相邻字形的最大水平间隙
MATCH_PX = 90.0           # 跨帧同一浮字的质心容差
MIN_FRAMES = 2            # 存活 ≥ 该帧数才算真浮字（滤单帧噪点）
LEGAL = tuple(range(30, 301, 30))   # 合法面额：30 的整数倍（金币 30 / 动作 120 / 退场 180 …）


def load_ocr():
    """复用 speedrush_scoring 的最小 OCR 引擎副本（同一目录、同一口径，不 import 插件）。"""
    spec = importlib.util.spec_from_file_location("hud_ocr", _HERE / "probe_hud_ocr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HudOcr(), mod._digits


def yellow_mask(rgb: np.ndarray) -> np.ndarray:
    """亮黄掩码。**只做最小闭运算**——把字形间的小间隙留着，那是判"这是文字串"的关键特征。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = (((h > H_MIN) & (h < H_MAX) & (s > S_MIN) & (v > V_MIN)).astype(np.uint8)) * 255
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))




def _group_glyphs(boxes, gap=GLYPH_GAP):
    """把"同一行、相邻"的连通域并成串（文字是多个字形拼的，金币/车道线是单块）。

    判据：竖直重叠 ≥ 较矮者的 50%，且水平间隙 ≤ `gap` px。
    """
    boxes = sorted(boxes, key=lambda b: b[0])
    groups: list[list] = []
    for b in boxes:
        hit = None
        for g in groups:
            gx = max(t[0] for t in g)
            gy0, gy1 = min(t[1] for t in g), max(t[1] + t[3] for t in g)
            ov = min(gy1, b[1] + b[3]) - max(gy0, b[1])
            if ov >= 0.5 * min(gy1 - gy0, b[3]) and 0 <= b[0] - (gx + g[-1][2]) <= gap:
                hit = g
                break
        (hit.append(b) if hit is not None else groups.append([b]))
    return groups


def candidates(rgb: np.ndarray):
    """浮字候选：[(x, y, w, h, 字形数, 填充率)]（原帧坐标）。

    为什么要这些形状特征：单靠"亮黄色"会把**金币**（约 70×70 的实心金盘，见实验记录目视拼图）、
    **黄色车道线**、**车灯反光**一起捞进来。真正的浮字是**文字串**——三个可判据：
      ① **字形数 ≥ 2**（`+30` 是三块；金币/车道线/反光是**一块**）；
      ② 宽高比 ≥ 1.2（横排文字）；
      ③ 填充率有上下界（字形细、且不是零星噪点）。
    """
    x0, y0, x1, y1 = ZONE
    band = rgb[y0:y1, x0:x1]
    m = yellow_mask(band)
    n, lab, st, ce = cv2.connectedComponentsWithStats(m, 8)
    glyphs = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if area < 60 or h < 12 or h > 70 or w > 90:
            continue
        if w / max(h, 1) > 1.6:                 # 太扁的横条 = 车道线/路缘
            continue
        glyphs.append((x, y, w, h, area))
    out = []
    for g in _group_glyphs(glyphs):
        gx0 = min(t[0] for t in g)
        gy0 = min(t[1] for t in g)
        gx1 = max(t[0] + t[2] for t in g)
        gy1 = max(t[1] + t[3] for t in g)
        w, h = gx1 - gx0, gy1 - gy0
        fill = sum(t[4] for t in g) / max(w * h, 1)
        if len(g) < 2 or not (50 <= w <= 240) or not (20 <= h <= 70):
            continue
        if w / max(h, 1) < 1.2 or not (0.06 <= fill <= 0.55):
            continue
        out.append((gx0 + x0, gy0 + y0, w, h, len(g), fill))
    return out


def snap(val: int | None) -> int | None:
    """吸附到合法面额；偏出半个面额即拒（残串不进统计）。"""
    if val is None or not (LEGAL[0] - 15 <= val <= LEGAL[-1] + 15):
        return None
    best = min(LEGAL, key=lambda v: abs(v - val))
    return best if abs(best - val) <= 15 else None


def read_popups(rgb: np.ndarray, ocr, digits):
    """一帧的浮字读数 [{cx, cy, w, h, nglyph, fill, text, val}]。"""
    out = []
    H, W = rgb.shape[:2]
    for (x, y, w, h, nglyph, fill) in candidates(rgb):
        pad = 4
        rx1, ry1 = max(0, x - pad), max(0, y - pad)
        rx2, ry2 = min(W, x + w + pad), min(H, y + h + pad)
        text = ocr.read(rgb, (rx1 / W, ry1 / H, rx2 / W, ry2 / H))
        out.append({"cx": x + w / 2, "cy": y + h / 2, "w": w, "h": h, "nglyph": nglyph,
                    "fill": fill, "text": text, "val": snap(digits(text))})
    return out


def track(per_frame, every: int):
    """跨帧最近邻去重：[cx, cy, last_seq, n, w, h, [(text, val)]]。"""
    live, dead = [], []
    for seq, items in per_frame:
        used = set()
        for it in sorted(items, key=lambda t: t["cy"]):
            cx, cy, w, h = it["cx"], it["cy"], it["w"], it["h"]
            text, val = it["text"], it["val"]
            best, bestd = None, MATCH_PX
            for k, t in enumerate(live):
                if k in used:
                    continue
                d = ((cx - t[0]) ** 2 + (cy - t[1]) ** 2) ** 0.5
                if d < bestd:
                    best, bestd = k, d
            if best is None:
                live.append([cx, cy, seq, 1, w, h, [(text, val)]])
            else:
                t = live[best]
                t[0], t[1], t[2], t[3] = cx, cy, seq, t[3] + 1
                t[6].append((text, val))
                used.add(best)
        for k in sorted([k for k, t in enumerate(live) if seq - t[2] > 3 * every],
                        reverse=True):
            dead.append(live.pop(k))
    dead.extend(live)
    return [t for t in dead if t[3] >= MIN_FRAMES]


def modal_value(texts_vals):
    vals = [v for _, v in texts_vals if v is not None]
    if not vals:
        return None, ""
    val = Counter(vals).most_common(1)[0][0]
    raw = Counter(t for t, v in texts_vals if v is not None).most_common(1)[0][0]
    return val, raw


def frames_of(sess: Path):
    return [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]


def cmd_sheet(args) -> None:
    ocr, digits = load_ocr()
    sess = Path(args.demos) / args.session
    fr = frames_of(sess)
    picks = fr[:: max(1, len(fr) // args.n)][: args.n]
    crops, font = [], ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 13)
    for r in picks:
        img = Image.open(sess / "frames" / r["file"]).convert("RGB")
        rgb = np.asarray(img)
        for it in read_popups(rgb, ocr, digits):
            w, h, text, val = it["w"], it["h"], it["text"], it["val"]
            x, y = int(it["cx"] - w / 2), int(it["cy"] - h / 2)
            crops.append((r["seq"], img.crop((max(0, x - 3), max(0, y - 3),
                                              x + w + 3, y + h + 3)), w, h, text, val))
    print(f"{args.session}: {len(picks)} 帧 → 候选裁片 {len(crops)}")
    for c in crops[:40]:
        print(f"   seq{c[0]:>4} {c[2]}x{c[3]}  OCR={c[4]!r} → 面额 {c[5]}")
    if crops:
        W, H, cols = 200, 64, 5
        rows = (min(len(crops), 40) + cols - 1) // cols
        sheet = Image.new("RGB", (W * cols, H * rows), (18, 18, 18))
        dr = ImageDraw.Draw(sheet)
        for i, (seq, crop, w, h, text, val) in enumerate(crops[:40]):
            big = crop.resize((crop.width * 2, crop.height * 2), Image.NEAREST)
            big.thumbnail((W - 8, 42))
            x, y = (i % cols) * W, (i // cols) * H
            sheet.paste(big, (x + 4, y + 3))
            dr.text((x + 4, y + 46), f"s{seq} {w}x{h} {text!r}->{val}", font=font,
                    fill=(255, 220, 120))
        out = Path(args.out or _HERE) / f"popup_read_{args.session}.jpg"
        sheet.save(out, quality=90)
        print(f"裁片拼图（目视复核用）→ {out}")


def cmd_scan(args) -> None:
    ocr, digits = load_ocr()
    sess = Path(args.demos) / args.session
    fr = frames_of(sess)
    t0 = fr[0]["ts_ns"] / 1e9
    per_frame = []
    for r in fr[:: args.every]:
        rgb = np.asarray(Image.open(sess / "frames" / r["file"]).convert("RGB"))
        per_frame.append((r["seq"], read_popups(rgb, ocr, digits)))
    span = (fr[-1]["ts_ns"] - fr[0]["ts_ns"]) / 1e9
    tracks = track(per_frame, args.every)
    cnt, total, unread = Counter(), 0, 0
    for t in tracks:
        val, raw = modal_value(t[6])
        if val is None:
            unread += 1
            continue
        cnt[val] += 1
        total += val
    print(f"{sess.name}: {len(fr)} 帧（每 {args.every} 帧取 1）  跨度 {span:.1f}s")
    print(f"浮字轨迹 {len(tracks)} 条（存活≥{MIN_FRAMES} 帧）  读出面额 {sum(cnt.values())} 条"
          f"  读不出 {unread} 条")
    print("\n面额直方图：")
    for v in sorted(cnt):
        print(f"  +{v:<4} {cnt[v]:>3} 条   = {cnt[v] * v:>6} 分   （{cnt[v] / span:.2f} 次/秒）")
    print(f"\n面额合计 {total} 分 → **{total / span:.0f} 分/秒**")
    units = sum(k * (v // 30) for v, k in cnt.items())
    print(f"折算「30 分单位」= {units} 个 → {units / span:.2f} 单位/秒")
    if args.score_series:
        rows = [json.loads(x) for x in
                Path(args.score_series).read_text(encoding="utf-8").splitlines() if x]
        sc = sorted((r["ts"] - t0, float(r["score"])) for r in rows
                    if r.get("score") is not None)
        ds = np.diff([v for _, v in sc])
        idx = np.where(ds != 0)[0]
        m0 = float(np.median(ds[idx][ds[idx] < 45]))
        k = np.array([int(round((ds[i] - m0) / 30.0)) for i in idx])
        dur = sc[-1][0] - sc[0][0]
        panel = (sc[-1][1] - sc[0][1]) / dur
        base = m0 / float(np.median(np.diff([sc[i + 1][0] for i in idx])))
        print(f"\n比分侧独立对账（{args.score_series}）：")
        print(f"  面板速率 {panel:.0f} 分/秒  里程底值 {base:.0f} 分/秒  "
              f"→ 非里程收入 {panel - base:.0f} 分/秒")
        print(f"  面板侧「30 分单位」{int(k[k > 0].sum())} 个 / {dur:.1f}s = "
              f"{k[k > 0].sum() / dur:.2f} 单位/秒")
        print(f"  → 对账：浮字侧 {units / span:.2f} 单位/秒 vs 面板侧 "
              f"{k[k > 0].sum() / dur:.2f} 单位/秒；面额和 {total / span:.0f} vs "
              f"{panel - base:.0f} 分/秒")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "sheet"])
    ap.add_argument("--session", required=True)
    ap.add_argument("--demos", default=str(DEFAULT_DEMOS))
    ap.add_argument("--every", type=int, default=1, help="scan：每 N 帧取 1")
    ap.add_argument("--n", type=int, default=12, help="sheet：抽样帧数")
    ap.add_argument("--score-series", default=None, help="scan：比分序列 JSONL，用于对账")
    ap.add_argument("--out", default=None, help="sheet 输出目录（缺省 = 本脚本目录）")
    args = ap.parse_args()
    (cmd_scan if args.mode == "scan" else cmd_sheet)(args)


if __name__ == "__main__":
    main()