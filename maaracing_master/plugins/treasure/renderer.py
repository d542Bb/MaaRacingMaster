#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
巅峰鉴宝调试渲染器（增强版）。

信息分区：
  • 左上  HUD 头部     : 帧号 / debug 序号 / 阶段 · 回合 · 排名 · 系统报价 · 估值 · 备注
  • 右上  H 走势面板   : R1~R5 5 个 H 值折线 + 当前 H 高亮
  • 左下  玩家出价表   : 4 玩家 × 5 回合矩阵（按 vs H 分档配色 + 当前回合框）
  • 右下  OCR 指标卡  : total/failures/dur_ms/age_ms
  • 底部  阶段进度条   : STAGE_ORDER 全阶段横向 bar，当前阶段填充+文字高亮
  • 背景  ROI 参考框   : 保留原浅色虚线（调试台标框）

颜色约定（玩家出价单元格）：
  • 浅绿  : 出价 ≥ H (偏高 / 智能价上方)
  • 浅蓝  : 出价 ∈ [H×0.9, H)  (接近 H，正常)
  • 米黄  : 出价 ∈ [H×0.6, H×0.9)  (偏低但合理)
  • 橙红  : 出价 < H×0.6 或出价>0且H缺失 (明显偏低或异常)
  • 灰底  : 0 (未识别)
"""

from __future__ import annotations

import numpy as np
import cv2
from typing import Any

from maaracing_master.core.debug import _put_text


# ---------- 关键 ROI 标定（归一化坐标 0~1，实际像素 = ×W / ×H） ----------
ROI_TEMPLATES = {
    "h_price_box":   (0.32, 0.58, 0.55, 0.72),
    "players_panel": (0.06, 0.22, 0.28, 0.86),
    "info_panel":    (0.30, 0.22, 0.58, 0.46),
    "round_label":   (0.38, 0.15, 0.54, 0.20),
}


# =============================================================
#  小工具
# =============================================================

def _rect(canvas, x1, y1, x2, y2, color, thickness=-1):
    cv2.rectangle(canvas, (int(x1), int(y1)), (int(x2), int(y2)), color, thickness)


def _put(canvas, text, x, y, scale=0.5, color=(255, 255, 255), stroke=1, align="left"):
    if align == "right":
        (tw, th), _ = cv2.getTextSize(str(text), cv2.FONT_HERSHEY_SIMPLEX, scale, stroke)
        x -= tw
    elif align == "center":
        (tw, th), _ = cv2.getTextSize(str(text), cv2.FONT_HERSHEY_SIMPLEX, scale, stroke)
        x -= tw // 2
    _put_text(canvas, str(text), (int(x), int(y)), scale=scale, color=color, stroke=stroke)


def _alpha_bg(canvas, x1, y1, x2, y2, alpha=0.55, color=(0, 0, 0)):
    """在 canvas 上画半透明背景矩形（就地修改）。"""
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = canvas[y1:y2, x1:x2]
    if roi.size == 0:
        return
    bg = np.full_like(roi, color, dtype=np.uint8)
    cv2.addWeighted(bg, alpha, roi, 1.0 - alpha, 0, roi)


def _grid_cell(canvas, gx, gy, gw, gh, fill_color, border_color=(60, 60, 60)):
    _rect(canvas, gx, gy, gx + gw, gy + gh, fill_color)
    _rect(canvas, gx, gy, gx + gw, gy + gh, border_color, 1)


# =============================================================
#  出价单元格：根据 **全程最大 H** 决定填充色
#
#  规则：
#  1. 所有玩家出价都和「5 回合中所有已读 H 的最大值」比比例（用户规则）
#  2. H 只取每回合第一次 set_h（第一次=智能报价，后续手动调不算到估值里）
#  3. 比例档位：灰底(0) / <0.6 橙红(显著偏低) / 0.6~0.9 橙黄(合理偏低) / 0.9~1.0 浅蓝(接近) / ≥1.0 浅绿(偏高/等于)
#  注意：h_max=0 （还没读到 H）→ 统一米黄底
# =============================================================

def _bid_color(bid: int, h_max: int):
    if not bid or bid <= 0:
        return (60, 60, 60)  # ⬛ 灰底（未识别）
    if not h_max or h_max <= 0:
        return (80, 60, 20)  # 🟨 米黄底（还没读到 Hmax）
    ratio = bid / h_max
    if ratio >= 1.00:
        return (120, 200, 120)   # 🟩 浅绿：出价 ≥ Hmax
    if ratio >= 0.90:
        return (200, 190, 110)   # 🟦 浅蓝：出价 ∈ [0.9Hmax, Hmax) → 接近 Hmax
    if ratio >= 0.60:
        return (60, 150, 220)    # 🟧 橙黄：出价 ∈ [0.6Hmax, 0.9Hmax) → 偏低但合理
    return (60, 60, 220)         # 🟥 橙红：出价 < 0.6Hmax → 显著偏低


def _slot_badge(slot: dict | None):
    """报价槽固化状态徽标（玩家表内嵌 + OCR 卡复用）。四态：
      未读=灰 / 读中=黄(稳N) / 固化=绿(✓值) / 清空预警=橙(漏N)。
    返回 (文本, BGR色)；无槽数据 → 未读灰。"""
    if not slot:
        return ("未读", (140, 140, 140))
    if slot.get("locked"):
        val = slot.get("val", 0) or 0
        if val >= 1_000_000:
            txt = f"✓{val/1_000_000:.1f}M"
        elif val >= 1_000:
            txt = f"✓{val/1_000:.1f}K"
        else:
            txt = f"✓{val:,}"
        return (txt, (120, 200, 120))   # 绿：已固化
    if slot.get("val", -1) != -1:
        miss = slot.get("miss", 0)
        if miss > 0:
            return (f"漏{miss}", (80, 140, 255))   # 橙：连续无输出预警（清空重读前）
        return (f"稳{slot.get('stable', 0)}", (120, 220, 255))  # 黄：读中（稳定计数）
    return ("未读", (140, 140, 140))   # 灰：未读到任何值


# =============================================================
#  ROI 参考框（浅色虚线）
# =============================================================

def _draw_roi_refs(frame_bgr):
    H, W = frame_bgr.shape[:2]
    for name, (x1, y1, x2, y2) in ROI_TEMPLATES.items():
        pt1 = (int(x1 * W), int(y1 * H))
        pt2 = (int(x2 * W), int(y2 * H))
        dash_color = (200, 200, 200)
        _dash = 6
        for xi in range(pt1[0], pt2[0], _dash * 2):
            cv2.line(frame_bgr, (xi, pt1[1]), (min(xi + _dash, pt2[0]), pt1[1]), dash_color, 1)
            cv2.line(frame_bgr, (xi, pt2[1]), (min(xi + _dash, pt2[0]), pt2[1]), dash_color, 1)
        for yi in range(pt1[1], pt2[1], _dash * 2):
            cv2.line(frame_bgr, (pt1[0], yi), (pt1[0], min(yi + _dash, pt2[1])), dash_color, 1)
            cv2.line(frame_bgr, (pt2[0], yi), (pt2[0], min(yi + _dash, pt2[1])), dash_color, 1)
        _put(frame_bgr, name, pt1[0] + 2, pt1[1] + 10, scale=0.35, color=(180, 180, 180))


# =============================================================
#  左上：HUD 头部（阶段 / 回合 / H / 估值 / 出价 / 排名 / 备注）
# =============================================================

def _draw_hud_top_left(canvas, frame_idx, debug_idx, label, stage, round_no, h_price,
                        sysmax_13, val_lo, val_hi, vhat, bids_ours, rank, note, balance=None,
                        h_max=None):
    H, W = canvas.shape[:2]
    hud_w, hud_h = int(min(640, W * 0.42)), 278
    x0, y0 = 10, 10
    _alpha_bg(canvas, x0, y0, x0 + hud_w, y0 + hud_h, alpha=0.58)
    _rect(canvas, x0, y0, x0 + hud_w, y0 + hud_h, (80, 80, 80), 1)

    x, y = x0 + 16, y0 + 32
    lg = 24
    header = (f"#{frame_idx:04d} raw  ·  #{debug_idx:02d} debug"
              f"   ·   鉴宝 · {label}")
    _put(canvas, header, x, y, scale=0.55, color=(120, 220, 255))
    y += lg + 4

    # 阶段 + 回合
    stage_txt = f"阶段 : {stage or '-'}"
    round_txt = f"第 {round_no} 回合" if round_no else "未进入回合"
    _put(canvas, stage_txt, x, y, scale=0.52, color=(255, 255, 255))
    _put(canvas, round_txt, x + 300, y, scale=0.52, color=(255, 255, 200))
    y += lg

    # 系统报价 H（当前）+ Hmax（全部历史最大值，与 H 并排）
    h_str = f"{h_price:,}" if h_price else "-"
    hm_str = f"{h_max:,}" if h_max else "-"
    _put(canvas, f"系统报价 H : {h_str}", x, y, scale=0.52, color=(180, 255, 180))
    _put(canvas, f"Hmax : {hm_str}", x + 300, y, scale=0.52, color=(120, 200, 120))
    y += lg

    # 估值：优先显示决策实际用的 V̂（VAL_COEF×sysmax，与 BidStrategy 同口径），区间作补充
    # 曾因 val_lo/hi 用 1.35/1.4 而决策用 1.28，图上看着该买、决策却判超估值 → 图和决策脱节。
    m13_str = f"{sysmax_13:,}" if sysmax_13 else "-"
    if vhat:
        _put(canvas,
             f"策略估值 V̂ : {vhat:,}   (sysmax_13={m13_str})",
             x, y, scale=0.44, color=(120, 240, 220))
    elif val_lo and val_hi:
        _put(canvas,
             f"估值区间 : {val_lo:,} ~ {val_hi:,}   (sysmax_13={m13_str})",
             x, y, scale=0.44, color=(120, 240, 220))
    else:
        _put(canvas, "估值区间 : -  (需 R1~R3 系统报价)",
             x, y, scale=0.44, color=(140, 140, 140))
    y += lg

    # 我方出价 + 排名
    bid_str = f"{bids_ours:,}" if bids_ours else "-"
    rank_color = (180, 255, 180) if rank and rank == 1 else (255, 220, 160)
    _put(canvas, f"我方出价 : {bid_str}", x, y, scale=0.52, color=(255, 220, 160))
    _put(canvas, f"排名 : 第{rank}名" if rank else "排名 : -",
         x + 280, y, scale=0.52, color=rank_color)
    y += lg

    # 我方余额（出价预算上限参考）
    if balance:
        _put(canvas, f"金币余额 : {balance:,}", x, y, scale=0.48, color=(255, 240, 140))
    else:
        _put(canvas, "金币余额 : -", x, y, scale=0.48, color=(120, 120, 120))
    y += lg

    if note:
        _put(canvas, f"备注 : {note}", x, y, scale=0.45, color=(255, 200, 255))


# =============================================================
#  右上：H 走势折线（5 回合 H 历史 + 当前值高亮）
# =============================================================

def _draw_h_chart(canvas, h_hist, current_h):
    H, W = canvas.shape[:2]
    cw, ch = 360, 220
    x0, y0 = W - cw - 10, 10
    _alpha_bg(canvas, x0, y0, x0 + cw, y0 + ch, alpha=0.55)
    _rect(canvas, x0, y0, x0 + cw, y0 + ch, (80, 80, 80), 1)

    _put(canvas, "H 走势（5 回合系统报价）", x0 + 14, y0 + 22, scale=0.5, color=(200, 220, 255))

    # 绘图区内边距
    px, py = x0 + 40, y0 + 46
    pw, ph = cw - 52, ch - 68
    # 坐标轴
    _rect(canvas, px, py + ph, px + pw, py + ph + 1, (160, 160, 160))
    _rect(canvas, px, py, px + 1, py + ph + 1, (160, 160, 160))
    # X 轴标签
    for i in range(5):
        cx = px + int((i + 0.5) * pw / 5)
        _put(canvas, f"R{i+1}", cx, py + ph + 16, scale=0.42, color=(200, 200, 200), align="center")

    # 5 个 H 值；>0 才算有效点
    hs = [int(x or 0) for x in (h_hist or [])]
    hs = (hs + [0, 0, 0, 0, 0])[:5]
    valid = [v for v in hs if v > 0]
    if valid:
        vmin, vmax = min(valid), max(valid)
        pad = (vmax - vmin) * 0.20 or max(vmax * 0.10, 1)
        lo, hi = max(0, vmin - pad), vmax + pad
    else:
        lo, hi = 0, 1

    # Y 轴刻度：2 个
    _put(canvas, f"{hi:,.0f}", px - 6, py + 10, scale=0.38, color=(180, 180, 180), align="right")
    _put(canvas, f"{lo:,.0f}", px - 6, py + ph - 2, scale=0.38, color=(180, 180, 180), align="right")

    def _to_xy(ri, val):
        cx = px + int((ri + 0.5) * pw / 5)
        if hi > lo and val > 0:
            t = (val - lo) / (hi - lo)
            cy = py + int(ph - t * ph)
        else:
            cy = py + ph  # 无效值落底
        return cx, cy

    # 折线连接有效点
    points = [(i, hs[i]) for i in range(5) if hs[i] > 0]
    if len(points) >= 2:
        for a, b in zip(points[:-1], points[1:]):
            x1, y1 = _to_xy(a[0], a[1])
            x2, y2 = _to_xy(b[0], b[1])
            cv2.line(canvas, (x1, y1), (x2, y2), (120, 220, 255), 2)

    # 每个点画圆点 + 金额文字
    for i, v in enumerate(hs):
        cx, cy = _to_xy(i, v)
        if v > 0:
            is_cur = (i + 1 == (int(current_h[0]) if isinstance(current_h, tuple) else -1)) or False
            # 当前回合 H 用红圈
            if current_h and v == current_h:
                cv2.circle(canvas, (cx, cy), 6, (80, 80, 255), -1)
                cv2.circle(canvas, (cx, cy), 7, (255, 255, 255), 1)
            else:
                cv2.circle(canvas, (cx, cy), 4, (120, 220, 255), -1)
            _put(canvas, f"{v:,}", cx, cy - 8, scale=0.38, color=(255, 255, 255), align="center")
        else:
            cv2.circle(canvas, (cx, cy), 3, (100, 100, 100), -1)
            _put(canvas, "-", cx, cy + 4, scale=0.38, color=(120, 120, 120), align="center")

    # 当前回合高亮（有 current_h 且知道是第几回合 -> 画 R列背景色）
    if current_h and current_h in hs:
        try:
            r_idx = hs.index(current_h)
            col_x1 = px + int(r_idx * pw / 5) + 1
            col_x2 = px + int((r_idx + 1) * pw / 5) - 1
            overlay = canvas[py:py + ph, col_x1:col_x2]
            if overlay.size:
                bg = np.full_like(overlay, (0, 90, 180), dtype=np.uint8)
                cv2.addWeighted(bg, 0.25, overlay, 0.75, 0, overlay)
        except ValueError:
            pass


# =============================================================
#  左下：玩家出价表（4×5 单元格 + 🔒 标记 + 当前回合列框）
# =============================================================

def _draw_player_bid_table(canvas, player_bids, current_round, h_max, my_rank=None, bid_slots=None):
    H, W = canvas.shape[:2]
    # 放在左下，宽度 ~ 屏幕左半 48%
    tw = int(W * 0.48)
    # 表头 + 4 行
    col_w = int((tw - 88) / 5)   # 5 列出价
    row_h = 44
    pad_x, pad_y = 96, 44        # 玩家名列宽、标题行高
    th = pad_y + row_h * 4 + 10
    x0, y0 = 10, H - th - 40     # 底部留 40 给阶段进度条

    _alpha_bg(canvas, x0, y0, x0 + tw, y0 + th, alpha=0.58)
    _rect(canvas, x0, y0, x0 + tw, y0 + th, (80, 80, 80), 1)

    # 表头标题 + 右侧图例（一行容纳不了就换行显示）
    _put(canvas, "玩家出价（左：玩家名；上：R1~R5）",
         x0 + 14, y0 + 20, scale=0.48, color=(220, 220, 220))
    # 图例：5 个色块 + 区间说明（放标题右侧，若有空间）
    legend_y = y0 + 20
    lx = x0 + tw - 8
    # 从右往左排，先算总宽
    legend_items = [
        ((60, 60, 60),   "0"),
        ((60, 60, 220),  "<0.6Hma"),
        ((60, 150, 220), "0.6~0.9"),
        ((200, 190, 110), "0.9~1.0"),
        ((120, 200, 120), "≥1.0"),
    ]
    lh = 14
    lgap = 60
    # 计算整条图例宽度（色块 12 + 间隔）
    total_w = sum(12 + 4 + len(txt) * 6 for _, txt in legend_items) + 4 * 6
    lx_start = lx - total_w
    if lx_start < x0 + 14 + 150:  # 太挤就放第二行
        legend_y = y0 + 20
        lx_start = x0 + 14
    else:
        legend_y = y0 + 20
    cx = lx_start
    for color, txt in legend_items:
        _rect(canvas, cx, legend_y - lh + 4, cx + 12, legend_y + 4, color)
        _put(canvas, txt, cx + 16, legend_y + 2, scale=0.34, color=(200, 200, 200))
        cx += 12 + 4 + len(txt) * 6 + 6

    # 表头 R1..R5
    hx = x0 + pad_x
    hy = y0 + pad_y - 20
    for ri in range(5):
        cx = hx + ri * col_w + col_w // 2
        color = (255, 255, 180) if (current_round and ri + 1 == current_round) else (200, 200, 200)
        _put(canvas, f"R{ri+1}", cx, hy, scale=0.44, color=color, align="center")

    # 当前回合列高亮（淡色竖条）
    if current_round and 1 <= current_round <= 5:
        cx1 = x0 + pad_x + (current_round - 1) * col_w
        cx2 = cx1 + col_w
        overlay = canvas[y0 + pad_y - 2: y0 + pad_y + row_h * 4 + 2, cx1:cx2]
        if overlay.size:
            bg = np.full_like(overlay, (60, 130, 60), dtype=np.uint8)
            cv2.addWeighted(bg, 0.28, overlay, 0.72, 0, overlay)

    rows = [(f"玩家{i}", f"P{i}") for i in range(1, 5)]
    for ri, (p_name, p_key) in enumerate(rows):
        pid = int(p_key[-1])
        cy = y0 + pad_y + ri * row_h
        # 玩家名列：我方槽位（my_rank，OCR 从「（我）」标记识别）标绿高亮，
        # 不能硬编码"玩家3"（不然换位后 debug 图仍标错）。
        name_color = (160, 255, 160) if (my_rank is not None and p_name == f"玩家{my_rank}") else (240, 240, 240)
        _grid_cell(canvas, x0 + 4, cy - 2, pad_x - 8, row_h - 4,
                   fill_color=(38, 38, 46))
        _put(canvas, p_name, x0 + 12, cy + 20, scale=0.48, color=name_color)
        # 玩家名下方第二行：槽固化状态徽标（灰/黄/绿/橙四态，一眼看出读得怎么样）。
        # 深色底块 + 徽标色边框 + 彩字，保证小字在深色 cell 上清晰可辨。
        slot = (bid_slots or {}).get(pid)
        badge_text, badge_color = _slot_badge(slot)
        if badge_text:
            (bw, bh), _ = cv2.getTextSize(badge_text, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
            _rect(canvas, x0 + 10, cy + 26, x0 + 14 + bw + 4, cy + 26 + bh + 8, (15, 15, 20))
            _rect(canvas, x0 + 10, cy + 26, x0 + 14 + bw + 4, cy + 26 + bh + 8, badge_color, 1)
            _put(canvas, badge_text, x0 + 12, cy + 34, scale=0.40, color=badge_color)

        lst = (player_bids or {}).get(p_name, [0] * 5)
        while len(lst) < 5:
            lst.append(0)
        for ci in range(5):
            bid = int(lst[ci] or 0)
            gx = x0 + pad_x + ci * col_w
            fill = _bid_color(bid, h_max)
            _grid_cell(canvas, gx + 1, cy - 2, col_w - 2, row_h - 4, fill_color=fill)
            if bid > 0:
                # 单元格文字：单位取 K / M，避免挤
                if bid >= 1_000_000:
                    txt = f"{bid/1_000_000:.1f}M"
                elif bid >= 1_000:
                    txt = f"{bid/1_000:.1f}K"
                else:
                    txt = f"{bid}"
                _put(canvas, txt, gx + col_w // 2, cy + 18,
                     scale=0.44, color=(18, 18, 18), align="center")
                # 再画一次原值小字体（不挤就好看，挤的话就 0.3 叠一层加阴影）
                _put(canvas, f"{bid:,}", gx + col_w // 2, cy + 36,
                     scale=0.30, color=(30, 30, 30), align="center")


# =============================================================
#  右下：OCR 指标卡
# =============================================================

def _draw_ocr_stats(canvas, ocr_stats, player_bids, bid_slots=None):
    H, W = canvas.shape[:2]
    # 放在右下；左边是玩家出价表（宽约 48%），所以从 x = W*0.51 起
    x0 = int(W * 0.51)
    w = W - x0 - 10
    # 高度和出价表相同（4 行玩家）
    th = 44 + 4 * 44 + 10
    y0 = H - th - 40

    _alpha_bg(canvas, x0, y0, x0 + w, y0 + th, alpha=0.58)
    _rect(canvas, x0, y0, x0 + w, y0 + th, (80, 80, 80), 1)

    _put(canvas, "OCR / 数据状态", x0 + 14, y0 + 20, scale=0.48, color=(220, 220, 255))

    lg = 22
    x, y = x0 + 16, y0 + 56
    if ocr_stats:
        t = ocr_stats.get("total", 0)
        f = ocr_stats.get("failures", 0)
        d = ocr_stats.get("dur_ms", 0)
        a = ocr_stats.get("age_ms", 0)
        ok_color = (180, 255, 180) if f == 0 else (80, 80, 255)
        _put(canvas, f"运行次数 : {t}",       x, y,      scale=0.45, color=(240, 240, 240))
        _put(canvas, f"失败 : {f}",            x + 200, y, scale=0.45, color=ok_color)
        y += lg
        dur_color = (180, 255, 180) if d <= 300 else ((255, 220, 120) if d <= 600 else (80, 80, 255))
        _put(canvas, f"最近耗时 : {d:.0f} ms / 13 区",  x, y, scale=0.45, color=dur_color)
        age_color = (180, 255, 180) if a <= 600 else ((255, 220, 120) if a <= 1500 else (80, 80, 255))
        _put(canvas, f"结果时效 : {a:.0f} ms", x + 280, y, scale=0.45, color=age_color)
        y += lg
    else:
        _put(canvas, "OCR 还未运行", x, y, scale=0.45, color=(160, 160, 160))
        y += lg * 2

    # 每槽识别统计：消费/输出/命中（用户拍板三口径全统计，显示如 100/12/8）
    _put(canvas, "槽 消费/输出/命中  状态", x, y, scale=0.42, color=(200, 200, 230))
    y += 18
    for pi in range(1, 5):
        slot = (bid_slots or {}).get(pi)
        if slot:
            line = f"P{pi}: {slot.get('consumed', 0)}/{slot.get('output', 0)}/{slot.get('hits', 0)}"
            badge_text, badge_color = _slot_badge(slot)
        else:
            line = f"P{pi}: -/-/-"
            badge_text, badge_color = "未读", (140, 140, 140)
        _put(canvas, line, x, y, scale=0.40, color=(230, 230, 230))
        if badge_text:
            _put(canvas, badge_text, x + 150, y, scale=0.36, color=badge_color)
        y += 18


# =============================================================
#  结算结果卡（中标结算 / 领取分红阶段，识别到任意项才显示）
#  位置：右下 OCR 指标卡右侧（OCR 卡右=W-10，结算卡与其同宽，叠在 OCR 卡上排或下方）
# =============================================================

def _draw_settle_panel(canvas, settle_final, settle_total, settle_profit, settle_my_income):
    """右侧结算结果卡，显示成交价/拍品总价/利润/本场收入。
    任一参数非 None 才绘制。"""
    if settle_final is None and settle_total is None and settle_profit is None and settle_my_income is None:
        return
    H, W = canvas.shape[:2]
    # 定位：右上 H 走势卡下方，H 走势卡 x0≈W*0.62 y≈10 w≈W*0.37 h≈250
    x0 = int(W * 0.62)
    w = W - x0 - 10
    # 6 行内容（标题+4 行数据+估值结论行）
    th = 44 + 5 * 36 + 8
    y0 = 10 + 230  # H 走势卡 y=10，高度约 220，贴在其下方
    if y0 + th > H - 50:
        y0 = max(10, H - 50 - th)

    _alpha_bg(canvas, x0, y0, x0 + w, y0 + th, alpha=0.62)
    _rect(canvas, x0, y0, x0 + w, y0 + th, (120, 100, 220), 1)

    _put(canvas, "结算结果（竞拍结束）", x0 + 14, y0 + 22, scale=0.52, color=(200, 180, 255))

    rows = [
        ("最终竞拍价", settle_final, (255, 230, 180)),
        ("拍品总价",   settle_total, (255, 255, 200)),
        ("利润",        settle_profit,
         (255, 120, 120) if isinstance(settle_profit, int) and settle_profit < 0 else (180, 255, 180)),
        ("本场收入",   settle_my_income,
         (255, 120, 120) if isinstance(settle_my_income, int) and settle_my_income < 0 else (255, 220, 140)),
    ]
    lg = 36
    x, y = x0 + 16, y0 + 60
    for label, val, color in rows:
        _put(canvas, f"{label} :", x, y + 4, scale=0.46, color=(200, 200, 200))
        if val is None:
            _put(canvas, "-", x + 140, y, scale=0.48, color=(140, 140, 140))
        else:
            sign = "-" if isinstance(val, int) and val < 0 else ""
            abs_v = abs(val) if isinstance(val, int) else val
            _put(canvas, f"{sign}{abs_v:,}", x + 140, y, scale=0.56, color=color)
        y += lg

    # 估值 vs 总价 对比：用总价直接判断估值算法准不准（总价 = sysmax × 1.33~1.44）
    if settle_total and settle_final:
        ratio = settle_final / settle_total if settle_total else 0
        acc = "✅" if 1.33 <= ratio <= 1.44 else ("⚠️ 偏低" if ratio < 1.33 else "⚠️ 偏高")
        _put(canvas, f"成交/总价 = {ratio:.3f}x   {acc}", x, y, scale=0.42, color=(220, 220, 255))


# =============================================================
#  底部：阶段进度条
# =============================================================

def _draw_stage_bar(canvas, stage_order, current_stage):
    H, W = canvas.shape[:2]
    bh = 30
    y0 = H - bh - 4
    _alpha_bg(canvas, 8, y0, W - 8, y0 + bh, alpha=0.65)
    _rect(canvas, 8, y0, W - 8, y0 + bh, (80, 80, 80), 1)

    order = list(stage_order or [])
    if not order:
        return
    n = len(order)
    bw = (W - 24) / n
    cur_i = order.index(current_stage) if current_stage in order else -1
    for i, s in enumerate(order):
        bx1 = 10 + int(i * bw)
        bx2 = 10 + int((i + 1) * bw) - 2
        if i == cur_i:
            fill = (70, 170, 255)
            tc = (255, 255, 255)
            # 当前阶段整段高亮
            overlay = canvas[y0 + 1: y0 + bh - 1, bx1: bx2]
            if overlay.size:
                bg = np.full_like(overlay, fill, dtype=np.uint8)
                cv2.addWeighted(bg, 0.75, overlay, 0.25, 0, overlay)
        elif cur_i >= 0 and i < cur_i:
            tc = (130, 255, 180)  # 已通过：绿灰
        else:
            tc = (160, 160, 160)  # 未到
        _rect(canvas, bx1, y0 + 1, bx2, y0 + bh - 1, (50, 50, 50) if i != cur_i else (255, 160, 60), 1)
        # 文字截断：太长只留首尾，或者缩小 scale
        label = s
        if len(label) > 10:
            label = label[:4] + "…" + label[-4:]
        cx = (bx1 + bx2) // 2
        cy = y0 + bh // 2 + 6
        _put(canvas, label, cx, cy, scale=0.40, color=tc, align="center")


# =============================================================
#  渲染器类
# =============================================================

class TreasureDebugRenderer:
    """巅峰鉴宝调试渲染器（信息增强版）"""

    def __init__(self, debug):
        self._d = debug

    # ---------- 内部：统一绘制 ----------

    def _draw(self, frame_bgr, state, draw_roi: bool):
        # 关键：在副本上绘制，避免就地修改传入帧。
        # save_frame 会先调 render_full 再调 render_peep，若 render_full 就地修改
        # frame_bgr，会导致 render_peep 拿到的原始画面已被 debug 面板覆盖，
        # 用户看到的 peep 预览就和 debug 存盘"串台"了。
        canvas = frame_bgr.copy()
        kw: dict[str, Any] = state.to_kwargs()
        label        = kw.get("label", "")
        stage        = kw.get("treasure_stage", None)
        round_no     = kw.get("treasure_round", None)
        h_price      = kw.get("treasure_h", None)
        sysmax_13    = kw.get("treasure_sysmax_13", None)
        val_lo       = kw.get("treasure_val_lo", None)
        val_hi       = kw.get("treasure_val_hi", None)
        vhat         = kw.get("treasure_vhat", None)
        our_bid      = kw.get("treasure_our_bid", None)
        rank         = kw.get("treasure_rank", None)
        note         = kw.get("treasure_note", None)
        roi          = kw.get("treasure_roi", draw_roi)

        h_hist       = kw.get("treasure_h_history", [0, 0, 0, 0, 0])
        player_bids  = kw.get("treasure_player_bids", {})
        bid_slots    = kw.get("treasure_bid_slots", {})  # 每槽 {val,stable,locked,miss,consumed,output,hits}
        frame_idx    = int(kw.get("treasure_frame_index", 0) or 0)
        debug_idx    = int(kw.get("treasure_debug_index", 0) or 0)
        stage_order  = kw.get("treasure_stage_order", [])
        ocr_stats    = kw.get("treasure_ocr_stats", None)
        # 结算页结果 + 我方余额
        settle_final = kw.get("treasure_settle_final", None)
        settle_total = kw.get("treasure_settle_total", None)
        settle_profit = kw.get("treasure_settle_profit", None)
        settle_my_income = kw.get("treasure_settle_my_income", None)
        balance      = kw.get("treasure_balance", None)

        H, W = canvas.shape[:2]
        # 小图（W<1000）退回精简版，避免所有 HUD 挤成一团
        small = W < 1000 or H < 600

        # --- 顺序：先 ROI（最底层，浅色虚线）---
        if roi:
            _draw_roi_refs(canvas)

        # h_max = 所有已读 H 的最大值，玩家出价全按 h_max 比例着色（用户规则：对比 Hmax，
        # 因为 H 只取每回合第一次智能报价）；左上 HUD 与 H 并排显示。
        h_max = max([h for h in h_hist if h > 0], default=0)

        # --- 左上 HUD 永远画（信息基础）---
        _draw_hud_top_left(canvas, frame_idx, debug_idx, label, stage, round_no,
                           h_price, sysmax_13, val_lo, val_hi, vhat, our_bid, rank, note,
                           balance, h_max=h_max)
        if small:
            # 小图：其他增强面板省略，保证至少 HUD 可看
            return canvas

        # --- 增强面板：H 走势 / 玩家出价表 / OCR 指标 / 阶段进度条 ---
        _draw_h_chart(canvas, h_hist, h_price)
        _draw_player_bid_table(canvas, player_bids, round_no, h_max, my_rank=rank, bid_slots=bid_slots)
        _draw_ocr_stats(canvas, ocr_stats, player_bids, bid_slots=bid_slots)
        # 结算结果卡：仅「领取分红」阶段显示。结算数据在中标结算阶段就可能被识别到，
        # 过早显示会与真实结算结果混淆（用户要求：只在此阶段激活）
        if stage == "领取分红":
            _draw_settle_panel(canvas, settle_final, settle_total, settle_profit, settle_my_income)
        _draw_stage_bar(canvas, stage_order, stage)

        return canvas

    # ---------- 对外接口 ----------

    def render_full(self, frame_bgr, state):
        """全量绘制（存盘用）：HUD + ROI 参考框"""
        return self._draw(frame_bgr, state, draw_roi=True)

    def render_peep(self, frame_bgr, state):
        """准星模式（PEEP 实时预览）：原图上叠加「程序想点击的位置」准星 + 顶部提示条。

        与 render_full（debug 存盘全量绘制）互相独立、可同时开启；本方法刻意不画
        任何 HUD 面板，只保留原画面 + 准星，方便用户对照真实游戏窗口手动点击。
        目标位置来自 state.treasure_action（treasure_module._resolve_action_target）。

        两种画面形态：
          - 有 center（真实点击目标）→ 画准星 + key 标签 + 顶部提示条
          - center 为 None（纯等待，如"等待出价按钮亮起…"）→ 只画顶部提示条，不画准星
        两套点击方式区分（state.treasure_click_mode）：
          - real（前台鼠标）→ 青黄准星 + [前台鼠标] 标签
          - gamepad（后台手柄+A）→ 紫粉准星 + [后台手柄+A] 标签
        手柄光标实时位置（state.treasure_gamepad_cursor，导航进度回调注入）：
        绿圈标出手柄光标当前位置 + 距目标距离；识别丢失时顶部提示。
        """
        canvas = frame_bgr.copy()
        kw = state.to_kwargs()
        act = kw.get("treasure_action")
        if not act:
            return canvas
        H, W = canvas.shape[:2]
        center = act.get("center")
        # 纯等待模式：无 center → 只画顶部文字条（无准星、无 key 标签），不画准星
        if center is None:
            hint = act.get("hint") or act.get("key") or "等待中..."
            stage_txt = kw.get("treasure_stage") or "-"
            _alpha_bg(canvas, 0, 0, W, 34, alpha=0.6)
            _put(canvas, f"[等待] {hint}   （阶段: {stage_txt}）", 10, 20, scale=0.55,
                 color=(200, 200, 220))
            return canvas
        # 健壮性守卫：None center / 非二元组 / 组件不是数 → 直接退回画布，不崩溃。
        # 历史出现过："center" 键存在但值为 None（透传 dict 前未过滤），
        # cx, cy = None 会抛 cannot unpack non-iterable NoneType object，把整个模块终止。
        try:
            cx, cy = center
            _ = float(cx), float(cy)
        except Exception:
            return canvas
        px, py = max(10, int(cx * W)), max(10, int(cy * H))
        # 两套点击方式区分显示（treasure_click_mode 由 _treasure_kwargs 注入；
        # 旧 state 缺字段回退 real）。主区分靠颜色 + 中文标签：
        # real 前台鼠标=青黄（历史配色）/ gamepad 后台手柄=紫粉。
        # 文案与 module.CLICK_MODE_LABELS 保持一致（renderer 不反向 import 防循环依赖；
        # cv2 5.0 putText 实测支持中文渲染）。
        mode = str(kw.get("treasure_click_mode") or "real")
        if mode == "gamepad":
            tag = "后台手柄+A"
            color = (255, 120, 255)
        else:
            tag = "前台鼠标"
            color = (0, 220, 255)
        # 十字线 + 中心圈
        cv2.line(canvas, (px - 34, py), (px - 9, py), color, 2)
        cv2.line(canvas, (px + 9, py), (px + 34, py), color, 2)
        cv2.line(canvas, (px, py - 34), (px, py - 9), color, 2)
        cv2.line(canvas, (px, py + 9), (px, py + 34), color, 2)
        cv2.circle(canvas, (px, py), 6, color, 2)
        # 按钮 key 标签 + 点击方式标签（准星右上方）
        _put(canvas, f"{act['key']} [{tag}]", px + 14, max(14, py - 14), scale=0.5, color=color)
        # 手柄光标实时位置（treasure_gamepad_cursor 由导航进度回调注入，pos=帧像素）：
        # 绿圈=手柄光标当前位置，与青黄/紫粉「点击目标准星」区分；pos=None=签名识别丢失
        gcur = kw.get("treasure_gamepad_cursor")
        if gcur:
            gpos = gcur.get("pos")
            if gpos:
                gx, gy = int(gpos[0]), int(gpos[1])
                gc = (80, 255, 80)  # 绿色（BGR）
                cv2.circle(canvas, (gx, gy), 14, gc, 2)
                cv2.circle(canvas, (gx, gy), 2, gc, -1)
                dist = gcur.get("dist")
                label = f"手柄[{gcur.get('stage', '')}]"
                if dist is not None:
                    label += f" 距离{dist:.0f}px"
                _put(canvas, label, gx + 16, gy + 16, scale=0.45, color=gc)
            else:
                stage = gcur.get("stage", "")
                _put(canvas, f"手柄光标丢失[{stage}]", W // 2 - 70, 54, scale=0.5,
                     color=(80, 80, 255), stroke=2)
        # 手柄光标识别候选（treasure_cursor_cands：GamepadClicker.read_pos 每帧快照，
        # 分数降序）：绿圈=本轮选中候选（与上方 gcur 光标圈同心套外环）；
        # 黄圈=次选（最多 5 个，含低于置信门槛 0.60 的拒识候选）——诊断
        # "为什么识别不到光标 / 为什么选错假候选"的分数与位置分布。
        cands = kw.get("treasure_cursor_cands")
        if cands:
            alt_n = 0
            for i, cd in enumerate(cands.get("list") or []):
                try:
                    cx_, cy_, sc_ = float(cd[0]), float(cd[1]), float(cd[2])
                except Exception:
                    continue  # 候选数据不完整跳过，不影响其余候选/准星绘制
                is_sel = (i == cands.get("sel"))
                if not is_sel:
                    if alt_n >= 5:
                        continue  # 次选最多画 5 个
                    alt_n += 1
                col = (80, 255, 80) if is_sel else (0, 255, 255)  # BGR：绿=选中 / 黄=次选
                cv2.circle(canvas, (int(cx_), int(cy_)), 20 if is_sel else 11, col, 2)
                _put(canvas, f"{sc_:.2f}{' 选中' if is_sel else ''}",
                     int(cx_) + 12, max(14, int(cy_) - 24), scale=0.42, color=col)
        # 顶部提示条
        hint = act.get("hint") or act["key"]
        stage_txt = kw.get("treasure_stage") or "-"
        _alpha_bg(canvas, 0, 0, W, 34, alpha=0.6)
        _put(canvas, f"[准星·{tag}] {hint}   （阶段: {stage_txt}）", 10, 20, scale=0.55, color=color)
        return canvas
