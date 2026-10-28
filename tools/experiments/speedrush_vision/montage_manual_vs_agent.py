# -*- coding: utf-8 -*-
"""人工局 vs 代理局同阶段蒙太奇（只读既有产物，不改生产）。

人工：manual_score_20261016（P1 3905 / P2 10080，影子决策同期记录）
代理：APPDATA debug 20261020_202758_p1 / 20261020_202843_p2（终局 2562 / 6389）

每列按阶段内时间对齐（5~35s），帧下条给出实际横位（道）；代理帧另标当前
选道目标（空心圈）。底部曲线为阶段内得分增量——消除阶段继承分的干扰。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

EXP = Path(__file__).parent
MANUAL = EXP / "manual_score_20261016"
# 两局原始帧（约 310MB）在数据目录；仓库只留 trace/日志/对账脚本
MANUAL_DATA = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "data" / \
    "speedrush" / "manual_score_20261016"
FRAME_ROOT = MANUAL if (MANUAL / "manual_20261016_160353_p1").exists() \
    else MANUAL_DATA
DBG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
OUT = EXP / "montage_manual_vs_agent_20261020.jpg"

SAMPLE_T = [5, 10, 15, 20, 25, 30, 35]
CELL_W, CELL_H = 320, 180
CAP_H = 64
ROW_BAR_H = 38
GAP = 12
MARGIN = 40
COLS = len(SAMPLE_T)

FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BD = "C:/Windows/Fonts/msyhbd.ttc"

BG = (16, 19, 26)
PANEL = (23, 27, 36)
GRID = (42, 48, 60)
TEXT = (232, 236, 244)
DIM = (154, 164, 180)
HUMAN_C = (77, 163, 255)
MACHINE_C = (255, 138, 61)
LANE_MIN, LANE_MAX = -2.0, 2.0


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BD if bold else FONT, size)


F = {
    "title": font(34, True),
    "sub": font(18),
    "row": font(22, True),
    "row_s": font(15),
    "cap": font(16),
    "cap_s": font(13),
    "tag": font(14, True),
    "axis": font(14),
    "chart_t": font(20, True),
    "chart_v": font(16, True),
    "chart_s": font(13),
}


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def fmt_score(v):
    return "—" if v is None else f"{int(round(v))}"


class Phase:
    """一个阶段的对局素材：帧、横位、得分、速度、事件、选道目标。"""

    def __init__(self, kind: str, phase: int):
        self.kind = kind
        self.phase = phase
        self.rows = []
        self.frames = []          # [(ts_ns, frame_id, path)]
        self.trace = []
        self.hud = []
        self.t0 = 0
        self.dur = 1.0
        self.final = 0
        self.start = 0
        if kind == "human":
            self._load_human()
        else:
            self._load_machine()

    # ---- 人工局：帧序列 + 影子 trace + HUD ----
    def _load_human(self):
        tag = "p1" if self.phase == 1 else "p2"
        d = FRAME_ROOT / ("manual_20261016_160353_p1" if self.phase == 1
                          else "manual_20261016_160437_p2")
        man = load_jsonl(d / "frames.jsonl")
        self.trace = load_jsonl(MANUAL / f"{tag}_trace.jsonl")
        self.hud = load_jsonl(d / "hud.jsonl")
        self.t0 = man[0]["ts_ns"]
        self.frames = [(m["ts_ns"], m["frame_id"], d / "frames" / m["file"]) for m in man]
        self.dur = (self.frames[-1][0] - self.t0) / 1e9
        self._build_series()

    # ---- 代理局：raw 帧按 trace 行等步映射 ----
    def _load_machine(self):
        name = "20261020_202758_p1" if self.phase == 1 else "20261020_202843_p2"
        d = DBG / name
        self.trace = load_jsonl(d / f"trace_{name}.jsonl")
        self.hud = load_jsonl(d / "hud" / "hud.jsonl")
        self.t0 = self.trace[0]["ts_ns"]
        self.dur = (self.trace[-1]["ts_ns"] - self.t0) / 1e9
        raw = sorted((d / "raw").glob("*_raw.jpg"))
        step = (len(self.trace) - 1) / max(1, len(raw) - 1)
        for k, p in enumerate(raw):
            r = self.trace[round(k * step)]
            self.frames.append((r["ts_ns"], r["fid"], p))
        self._build_series()

    def _build_series(self):
        # 得分：单调取历史最大，规避 OCR 掉位/归零；值取自 trace / hud
        raw = []
        if self.kind == "human":
            for r in self.hud:
                v = ((r["fields"].get("score_a") or {}).get("value"))
                if v is not None:
                    raw.append(((r["ts_ns"] - self.t0) / 1e9, float(v)))
        else:
            for r in self.trace:
                v = r.get("hud_score")
                if v is not None:
                    raw.append(((r["ts_ns"] - self.t0) / 1e9, float(v)))
        raw.sort()
        peak = 0.0
        self.score = []
        for t, v in raw:
            peak = max(peak, v)
            self.score.append((t, peak))
        if self.score:
            self.start = self.score[0][1]
            self.final = self.score[-1][1]
        # 事件横幅（人工取 hud，代理同样取 hud）
        self.banner = []
        for r in self.hud:
            txt = ((r["fields"].get("event_banner") or {}).get("text") or "").strip()
            if txt:
                self.banner.append(((r["ts_ns"] - self.t0) / 1e9, txt))

    def _nearest(self, seq, t, max_dt=None):
        if not seq:
            return None
        best = min(seq, key=lambda x: abs(x[0] - t))
        if max_dt is not None and abs(best[0] - t) > max_dt:
            return None
        return best[1]

    def score_at(self, t):
        if not self.score:
            return None
        # 优先取 t 之前最近的读数（得分只前进）
        prev = [p for p in self.score if p[0] <= t]
        return prev[-1][1] if prev else self.score[0][1]

    def banner_at(self, t, window=2.0):
        if not self.banner:
            return ""
        best = min(self.banner, key=lambda x: abs(x[0] - t))
        txt = best[1] if abs(best[0] - t) <= window else ""
        # HUD OCR 有碎字噪声，只保留赛事横幅词
        return txt if any(k in txt for k in ("极限", "超车", "变道", "金币", "退场")) else ""

    def trace_at(self, t):
        seq = [((r["ts_ns"] - self.t0) / 1e9, r) for r in self.trace]
        return self._nearest(seq, t)

    def frame_at(self, t):
        return self._nearest([((ts - self.t0) / 1e9, p) for ts, _, p in self.frames], t)

    def rate_at(self, t):
        row = self.trace_at(t)
        if row is None:
            return None
        r = (row.get("hud_rate") if self.kind == "machine" else None)
        if r is None:
            hud = min(self.hud, key=lambda h: abs((h["ts_ns"] - self.t0) / 1e9 - t)) if self.hud else None
            if hud:
                r = (hud["fields"].get("rate_a") or {}).get("value")
        return r

    def lane_at(self, t):
        row = self.trace_at(t)
        return None if row is None else row.get("executed_lane")

    def goal_at(self, t):
        row = self.trace_at(t)
        return None if row is None else row.get("goal")


def lane_x(x0, x1, lane):
    lane = max(LANE_MIN, min(LANE_MAX, lane))
    return x0 + (lane - LANE_MIN) / (LANE_MAX - LANE_MIN) * (x1 - x0)


def draw_lane_gauge(d, x0, y, w, lane, goal=None, color=TEXT):
    h = 6
    d.rounded_rectangle([x0, y, x0 + w, y + h], radius=3, fill=(52, 58, 70))
    for tick in (-2, -1, 0, 1, 2):
        tx = lane_x(x0, x0 + w, tick)
        d.line([tx, y - 2, tx, y + h + 2], fill=(90, 98, 112), width=1)
    if goal is not None:
        gx = lane_x(x0, x0 + w, goal)
        r = 6
        d.ellipse([gx - r, y + h / 2 - r, gx + r, y + h / 2 + r], outline=(235, 238, 245), width=2)
    if lane is not None:
        lx = lane_x(x0, x0 + w, lane)
        r = 5
        d.ellipse([lx - r, y + h / 2 - r, lx + r, y + h / 2 + r], fill=color)


def draw_cell(base, phase: Phase, t: int, x: int, y: int):
    path = phase.frame_at(t)
    if path is None:
        return
    img = Image.open(path).convert("RGB").resize((CELL_W, CELL_H), Image.LANCZOS)
    base.paste(img, (x, y))

    color = HUMAN_C if phase.kind == "human" else MACHINE_C

    # 帧内左上角时间签
    tag = f"{'P' + str(phase.phase)}  {t:02d}s"
    tw = int(ImageDraw.Draw(base).textlength(tag, font=F["tag"]))
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([x + 8, y + 8, x + 8 + tw + 14, y + 8 + 24], radius=5, fill=(0, 0, 0, 150))
    base.paste(Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB"), (0, 0))
    d = ImageDraw.Draw(base)
    d.text((x + 15, y + 10), tag, font=F["tag"], fill=TEXT)

    score = phase.score_at(t)
    rate = phase.rate_at(t)
    lane = phase.lane_at(t)
    goal = phase.goal_at(t) if phase.kind == "machine" else None
    banner = phase.banner_at(t)

    cy = y + CELL_H + 6
    head = f"{fmt_score(score)} 分"
    if rate is not None:
        head += f" · 速率 {rate}"
    d.text((x + 2, cy), head, font=F["cap"], fill=color)

    lane_txt = "横位 —" if lane is None else f"横位 {lane:+.2f}"
    if goal is not None:
        lane_txt += f" · 选道 {goal:+.2f}"
    d.text((x + 2, cy + 24), lane_txt, font=F["cap_s"], fill=DIM)
    if banner:
        bw = d.textlength(lane_txt, font=F["cap_s"])
        d.text((x + 10 + bw, cy + 24), f"  {banner}", font=F["cap_s"], fill=(255, 214, 102))

    draw_lane_gauge(d, x + 2, cy + 50, CELL_W - 4, lane, goal, color)


def draw_row_header(base, x, y, w, label, line, color):
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([x, y, x + w, y + ROW_BAR_H], radius=6, fill=PANEL)
    d.rounded_rectangle([x, y, x + 5, y + ROW_BAR_H], radius=3, fill=color)
    d.text((x + 14, y + 5), label, font=F["row"], fill=color)
    d.text((x + 14 + d.textlength(label, font=F["row"]) + 14, y + 11), line, font=F["row_s"], fill=DIM)


def draw_chart(base, phase_h, phase_m, x, y, w, h):
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=PANEL)
    d.text((x + 18, y + 12), f"阶段 {phase_h.phase} · 得分增量", font=F["chart_t"], fill=TEXT)

    top, bottom = y + 52, y + h - 36
    left, right = x + 54, x + w - 22

    gain_h = [max(0.0, s - phase_h.start) for s in
              (phase_h.score_at(tt) or phase_h.start for tt in [i * 0.5 for i in range(int(phase_h.dur * 2) + 1)])]
    gain_m = [max(0.0, s - phase_m.start) for s in
              (phase_m.score_at(tt) or phase_m.start for tt in [i * 0.5 for i in range(int(phase_m.dur * 2) + 1)])]
    ymax = max(1.0, max(gain_h + gain_m))
    ymax = ((int(ymax / 1000) + 1) * 1000)
    xmax = float(int(max(phase_h.dur, phase_m.dur) / 10 + 0.999) * 10)

    def px(t):
        return left + min(t, xmax) / xmax * (right - left)

    def py(v):
        return bottom - v / ymax * (bottom - top)

    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        gy = bottom - frac * (bottom - top)
        d.line([left, gy, right, gy], fill=GRID, width=1)
        d.text((left - 8, gy - 8), f"{int(ymax * frac)}", font=F["axis"], fill=DIM, anchor="ra")
    for sec in range(0, int(xmax) + 1, 10):
        gx = px(sec)
        d.line([gx, top, gx, bottom], fill=GRID, width=1)
        d.text((gx, bottom + 6), f"{sec}s", font=F["axis"], fill=DIM, anchor="ma")

    for ph, color in ((phase_h, HUMAN_C), (phase_m, MACHINE_C)):
        pts = []
        for i in range(int(ph.dur * 2) + 1):
            tt = i * 0.5
            s = ph.score_at(tt)
            if s is None:
                continue
            pts.append((px(tt), py(max(0.0, s - ph.start))))
        if len(pts) >= 2:
            d.line(pts, fill=color, width=3, joint="curve")

    gh = max(0.0, phase_h.final - phase_h.start)
    gm = max(0.0, phase_m.final - phase_m.start)
    d.text((left, y + 40), f"人工 +{gh:.0f}（{gh / phase_h.dur:.0f} 分/秒）", font=F["chart_v"], fill=HUMAN_C)
    d.text((x + w - 22, y + 40), f"机器 +{gm:.0f}（{gm / phase_m.dur:.0f} 分/秒）",
           font=F["chart_v"], fill=MACHINE_C, anchor="ra")
    longer = phase_m.dur > phase_h.dur
    note = (f"机器多跑 {phase_m.dur - phase_h.dur:.1f}s，增量仍少 {gh - gm:.0f}。" if longer
            else f"机器用时相近（{phase_m.dur:.1f}s vs {phase_h.dur:.1f}s），增量少 {gh - gm:.0f}。")
    d.text((left, bottom + 24), "曲线从阶段起始分起算；" + note, font=F["chart_s"], fill=DIM)


def main():
    p1h, p1m = Phase("human", 1), Phase("machine", 1)
    p2h, p2m = Phase("human", 2), Phase("machine", 2)
    rows = [
        ("人工 · 阶段 1 · 2026-10-16", p1h, HUMAN_C),
        ("机器 · 阶段 1 · 2026-10-20 20:27", p1m, MACHINE_C),
        ("人工 · 阶段 2 · 2026-10-16", p2h, HUMAN_C),
        ("机器 · 阶段 2 · 2026-10-20 20:28", p2m, MACHINE_C),
    ]

    width = MARGIN * 2 + COLS * CELL_W + (COLS - 1) * GAP
    header_h = 108
    row_block = ROW_BAR_H + 8 + CELL_H + CAP_H
    chart_h = 300
    height = header_h + 4 * row_block + 3 * GAP + 40 + chart_h + MARGIN

    base = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(base)

    d.text((MARGIN, 28), "SpeedRush 人工 vs 机器 · 同阶段蒙太奇", font=F["title"], fill=TEXT)
    d.text((MARGIN, 74),
           "人工局 2026-10-16 16:03（终局 10080） · 机器局 2026-10-20 20:27（终局 6389） · "
           "每列按阶段内时间对齐",
           font=F["sub"], fill=DIM)
    d.text((width - MARGIN, 30), "10080 : 6389", font=font(40, True), fill=HUMAN_C, anchor="ra")
    d.text((width - MARGIN, 82), "人工 : 机器（终局分）", font=F["cap_s"], fill=DIM, anchor="ra")

    col_x0 = MARGIN
    for idx, (label, ph, color) in enumerate(rows):
        block_y = header_h + idx * (row_block + GAP)
        gain = max(0, int(round(ph.final - ph.start)))
        line = (f"终局 {int(round(ph.final))} · 阶段增益 {gain} · "
                f"平均 {gain / ph.dur:.0f} 分/秒 · 用时 {ph.dur:.1f}s")
        draw_row_header(base, col_x0, block_y, width - MARGIN * 2, label, line, color)
        for c, t in enumerate(SAMPLE_T):
            x = col_x0 + c * (CELL_W + GAP)
            y = block_y + ROW_BAR_H + 8
            draw_cell(base, ph, t, x, y)

    chart_y = header_h + 4 * row_block + 3 * GAP + 28
    gap_c = 16
    cw = (width - MARGIN * 2 - gap_c) // 2
    draw_chart(base, p1h, p1m, MARGIN, chart_y, cw, chart_h)
    draw_chart(base, p2h, p2m, MARGIN + cw + gap_c, chart_y, cw, chart_h)

    base.save(OUT, quality=92)
    print(f"wrote {OUT} ({base.width}x{base.height})")


if __name__ == "__main__":
    main()
