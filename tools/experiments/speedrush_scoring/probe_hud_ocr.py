"""speedrush 驾驶 HUD 文字定位与读数探针（区域标定用）。

**要回答什么**：驾驶页 HUD 上哪些文字能被 OCR 出来、各自在画面上的什么位置，
从而为「HUD 读数」定 ROI——用实测坐标，不靠目测。

**两种模式**
  discover  启用文本检测（det），在整帧/条带上找出所有文字块与其归一化坐标。
            用于**标定**：先把文字找出来，再据此定紧贴的 ROI。
  read      用给定的归一化 ROI 直接识别（关 det，与生产口径一致）——ROI 是固定
            HUD 文字框时 det 是冗余计算，实测关 det 后单 ROI 从 ~1062ms 降到 ~12ms。

**为什么要走 tools/experiments/**：speedrush 插件不能 import treasure 的 OCR
（插件自包含契约：整个目录拷走即卸载）。实验脚本不受该约束——仓库已有 6 个实验
脚本直接 import TreasureOcr 的先例。规则实测只需要**离线**读数，故本探针就是
这一轮的读数入口；实时读数（模块内观察线程）要先把 OCR 引擎抽到 core 或复制，
那是独立决策，不在本探针职责内。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_scoring/probe_hud_ocr.py discover [--frame <jpg>]
    .venv/Scripts/python.exe tools/experiments/speedrush_scoring/probe_hud_ocr.py read --rects hud_regions.json [--session <dir>]

不带 --frame 时自动取 demos 下最近会话的中段帧。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.core.image_io import read_rgb, to_bgr  # noqa: E402
from maaracing_master.core.paths import data_dir  # noqa: E402

DEMOS = data_dir() / "speedrush" / "demos"


def latest_mid_frame() -> Path:
    """最近会话的中段帧（避开起步动画与结束画面）。"""
    sessions = sorted((d for d in DEMOS.iterdir() if d.is_dir()), reverse=True)
    for s in sessions:
        files = sorted((s / "frames").glob("*.jpg"))
        if len(files) > 20:
            return files[len(files) // 2]
    raise SystemExit(f"在 {DEMOS} 下找不到可用会话帧")


def _engine_det():
    """带 det 的引擎（仅标定用）：能同时给出文字内容与框。"""
    from rapidocr import RapidOCR
    return RapidOCR()


def cmd_discover(args) -> None:
    frame_path = Path(args.frame) if args.frame else latest_mid_frame()
    rgb = read_rgb(frame_path)
    assert rgb is not None, frame_path
    H, W = rgb.shape[:2]
    print(f"帧 {frame_path.name}  {W}x{H}")

    # 只扫上带与下半左带（HUD 分布）：全帧扫一遍即可，这里按条带切是为了看清归属
    strips = {
        "上带 y0-0.33": (0, 0, W, int(0.33 * H)),
        "左下 y0.33-1.0 x0-0.35": (0, int(0.33 * H), int(0.35 * W), H),
        "右下 y0.33-1.0 x0.65-1": (int(0.65 * W), int(0.33 * H), W, H),
    }
    engine = _engine_det()
    for tag, (x1, y1, x2, y2) in strips.items():
        patch = rgb[y1:y2, x1:x2]
        out = engine(to_bgr(patch))
        txts = getattr(out, "txts", None) or []
        boxes = getattr(out, "boxes", None)
        scores = getattr(out, "scores", None) or []
        print(f"\n=== {tag}  检出 {len(txts)} 块 ===")
        for i, t in enumerate(txts):
            sc = float(scores[i]) if i < len(scores) else float("nan")
            bx = boxes[i] if boxes is not None and i < len(boxes) else None
            if bx is None:
                print(f"  {t!r} score={sc:.2f}")
                continue
            xs = [p[0] for p in bx]
            ys = [p[1] for p in bx]
            # 换算回整帧归一化坐标（条带偏移要加回来）
            nx1, nx2 = (x1 + min(xs)) / W, (x1 + max(xs)) / W
            ny1, ny2 = (y1 + min(ys)) / H, (y1 + max(ys)) / H
            print(f"  {t!r:28s} score={sc:.2f}  归一化=({nx1:.4f},{ny1:.4f},{nx2:.4f},{ny2:.4f})")


def cmd_read(args) -> None:
    from maaracing_master.plugins.treasure.ocr import TreasureOcr
    frame_path = Path(args.frame) if args.frame else latest_mid_frame()
    rgb = read_rgb(frame_path)
    assert rgb is not None, frame_path
    ocr = TreasureOcr(ROOT / "maaracing_master" / "plugins" / "treasure")
    regs = json.loads(Path(args.rects).read_text(encoding="utf-8"))
    print(f"帧 {frame_path.name}")
    for name, rect in regs.items():
        got = ocr.recognize_single(rgb, rect)
        text = (got or {}).get("text", "")
        print(f"  {name:16s} rect={rect} → {text!r}")


def cmd_scan(args) -> None:
    """跨会话抽样若干帧，**按位置**统计 HUD 元素——ROI 标定的依据。

    为什么不能按文本聚合：数值字段（总分、得分速度、里程、血量）每帧都不同，
    按文本分组会让它们全部塌成 count=1（实测 `我方总分` 就这样被漏掉的）。
    故按「框中心量化到 0.02 网格」分桶，报每桶命中率、样本值与所属会话——
    命中率区分「一直有/有时有/偶尔」，会话归属区分「某阶段特有」。
    """
    engine = _engine_det()
    sessions = sorted((d for d in DEMOS.iterdir() if d.is_dir()))
    per = max(1, args.per_session)
    buckets: dict[tuple[int, int], dict] = {}
    n = 0
    for s in sessions:
        files = sorted((s / "frames").glob("*.jpg"))
        if len(files) < per + 4:
            continue
        # 掐头去尾：首帧贴在"已进入驾驶页"上（可能还在起步动画），末帧已在结果页/弹窗上
        # ——两者混进来会污染区域标定（实测扫到过 '回合1结束挑战成功'、'10/10'）。
        lo, hi = round(len(files) * 0.12), round(len(files) * 0.88)
        span = files[lo:hi]
        idxs = [round(i * (len(span) - 1) / max(1, per - 1)) for i in range(per)]
        for i in idxs:
            rgb = read_rgb(span[i])
            if rgb is None:
                continue
            H, W = rgb.shape[:2]
            out = engine(to_bgr(rgb))
            n += 1
            txts = getattr(out, "txts", None)
            boxes = getattr(out, "boxes", None)
            if txts is None or boxes is None:
                continue  # 该帧无检出（空旷画面/全暗）
            for t, bx in zip(txts, boxes):
                xs = [p[0] for p in bx]
                ys = [p[1] for p in bx]
                box = (min(xs) / W, min(ys) / H, max(xs) / W, max(ys) / H)
                key = (round((box[0] + box[2]) / 2 / 0.02),
                       round((box[1] + box[3]) / 2 / 0.02))
                b = buckets.setdefault(key, {"boxes": [], "texts": [], "sess": set()})
                b["boxes"].append(box)
                b["texts"].append(str(t))
                b["sess"].add(s.name[-2:])
    print(f"共扫 {n} 帧（{len(sessions)} 会话 × {per}）  按框中心 0.02 网格分桶\n")
    for _key, b in sorted(buckets.items(), key=lambda kv: -len(kv[1]["boxes"])):
        if len(b["boxes"]) < 2 and not args.show_all:
            continue
        arr = np.array(b["boxes"], dtype=float)
        med = arr.mean(axis=0)
        print(f"{len(b['boxes']):3d}/{n} 归一化≈({med[0]:.4f},{med[1]:.4f},{med[2]:.4f},{med[3]:.4f}) "
              f"会话={sorted(b['sess'])}  样本={b['texts'][:6]}")


def _digits(text: str) -> int | None:
    """取文本里的整数（'1,234' / '238' / '+120' 都能吃；无数字返回 None）。"""
    m = re.findall(r"\d[\d,]*", text or "")
    if not m:
        return None
    try:
        return int(m[0].replace(",", ""))
    except ValueError:
        return None


def _mmss(text: str) -> int | None:
    m = re.search(r"(\d{1,2}):(\d{2})", text or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


# 左侧信息面板（里程/超车/合计分值）的**逐字段**在场判据：面板是半透明暗底，天空是亮的。
#
# 为什么必须有这道闸门，以及为什么是"逐字段"而不是"整面板"：
#   1. 该面板会闪烁（ON/OFF 交替），OFF 时整块移出画面，只剩进度条。早期版本直接对
#      ROI 做 OCR，读到的其实是天空，于是三列出现 [2901, 1, 337, 1] 这类垃圾值。
#   2. 面板还有**矮版变体**：它不显示"合计分值"（实测 frame 480 与 350 对比），此时
#      按整面板判在场会放行合计那一格——它落在面板下方的天空上，读出来是 '1'。
#      逐字段判暗底同时覆盖这两种情况，且纯 numpy、不消耗 OCR。
FIELD_DARK_MIN = 0.35
PANEL_FIELDS = ("mileage", "overtake", "total_left")


def field_on_dark(frame_rgb: np.ndarray, rect) -> bool:
    """该 ROI 是否主要压在暗底上（面板在该字段处是否在场）。"""
    h, w = frame_rgb.shape[:2]
    x1, y1 = max(0, int(rect[0] * w)), max(0, int(rect[1] * h))
    x2, y2 = min(w, int(rect[2] * w)), min(h, int(rect[3] * h))
    reg = frame_rgb[y1:y2, x1:x2]
    if reg.size == 0:
        return False
    lum = reg.mean(axis=2) if reg.ndim == 3 else reg
    return float((lum < 90).mean()) > FIELD_DARK_MIN


def _sample_rows(session: Path, regs: dict, every: int):
    """顺序读会话的 HUD（关 det，与生产口径一致）→ [(帧索引行, 各区域文本)]。

    左侧三列先过逐字段暗底闸门；不在场记 None，不拿天空凑数。
    """
    from maaracing_master.plugins.treasure.ocr import TreasureOcr

    rows = [json.loads(x) for x in
            (session / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]
    ocr = TreasureOcr(ROOT / "maaracing_master" / "plugins" / "treasure")
    out = []
    for r in rows[::max(1, every)]:
        rgb = read_rgb(session / "frames" / r["file"])
        if rgb is None:
            continue
        vals: dict[str, str | None] = {}
        for n, rect in regs.items():
            if n in PANEL_FIELDS and not field_on_dark(rgb, rect):
                vals[n] = None
                continue
            vals[n] = (ocr.recognize_single(rgb, rect) or {}).get("text", "")
        out.append((r, vals))
    return rows, out


def cmd_timeline(args) -> None:
    """顺序读一整段会话，出记分时间线与**自洽性检验**。

    自洽性检验为什么关键：它把「读数可不可信」与「规则是什么」分开。若
    `d(合计分值)/dt` 与画面上的「得分速度」读数在多数区间吻合，则三个字段互相印证，
    读数可信；此时**分值增量与该区间内里程/超车增量之比**就直接给出 §7#4 的每单位
    权重——不必额外做一次实机实验（这会自动带入"读数噪声"作为分母，故要注意剔除
    里程/超车读数为空的区间）。
    """
    session = Path(args.session)
    regs = json.loads(Path(args.rects).read_text(encoding="utf-8"))
    rows, samples = _sample_rows(session, regs, args.every)
    print(f"会话 {session.name}  帧 {len(rows)}  采样 {len(samples)}（每 {args.every} 帧）\n")
    print(f"{'seq':>5} {'t(s)':>6} {'计时':>6} {'里程':>5} {'超车':>5} {'合计':>6} "
          f"{'我方分':>7} {'速度':>5}  事件/浮字")
    seq = []
    t0 = samples[0][0]["ts_ns"] if samples else 0
    for r, v in samples:
        cur = {
            "t": (r["ts_ns"] - t0) / 1e9,
            "timer": _mmss(v["timer"]), "mileage": _digits(v["mileage"]),
            "overtake": _digits(v["overtake"]), "total": _digits(v["total_left"]),
            "score": _digits(v["score_self"]), "rate": _digits(v["rate_self"]),
        }
        seq.append(cur)
        ev = f"{v['event_banner'].strip()} {v['center_popup'].strip()}".strip()
        print(f"{r['seq']:>5} {cur['t']:>6.1f} {str(cur['timer']):>6} "
              f"{str(cur['mileage']):>5} {str(cur['overtake']):>5} {str(cur['total']):>6} "
              f"{str(cur['score']):>7} {str(cur['rate']):>5}  {ev}")

    print("\n=== 单调性（计时应递减，其余应递增） ===")
    for k, dec in (("timer", True), ("mileage", False), ("overtake", False),
                   ("total", False), ("score", False)):
        xs = [s[k] for s in seq if s[k] is not None]
        if len(xs) < 3:
            print(f"  {k:9s} 有效 {len(xs):3d}/{len(seq)} —— 太少，ROI 需重标")
            continue
        bad = sum(1 for a, b in zip(xs, xs[1:]) if (b > a if dec else b < a))
        print(f"  {k:9s} 有效 {len(xs):3d}/{len(seq)}  范围 {min(xs)}~{max(xs)}  "
              f"违例 {bad}/{len(xs) - 1}")

    pairs = [(a, b) for a, b in zip(seq, seq[1:])
             if None not in (a["total"], b["total"], b["rate"]) and b["t"] > a["t"]]
    if pairs:
        rel = [(b["total"] - a["total"]) / (b["t"] - a["t"]) / b["rate"]
               for a, b in pairs if b["rate"]]
        if rel:
            arr = np.array(rel)
            print(f"\n=== d(合计)/dt ÷ 得分速度 ===  n={len(rel)} 中位={np.median(arr):.3f} "
                  f"均值={arr.mean():.3f}（≈1 即两读数互证）")
            print(f"  前 12 个样本: {[round(x, 2) for x in rel[:12]]}")
    # 分值增量 vs 里程/超车增量：剔除读数缺失的区间后给每单位权重
    print("\n=== 分值增量 与 里程/超车增量（仅取两端都有读数的区间）===")
    for a, b in zip(seq, seq[1:]):
        if None in (a["total"], b["total"], a["mileage"], b["mileage"],
                    a["overtake"], b["overtake"]):
            continue
        d_total = b["total"] - a["total"]
        d_mile, d_over = b["mileage"] - a["mileage"], b["overtake"] - a["overtake"]
        if d_total or d_mile or d_over:
            print(f"  t={a['t']:5.1f}→{b['t']:5.1f}  Δ合计={d_total:+4d}  "
                  f"Δ里程={d_mile:+4d}  Δ超车={d_over:+4d}")


def cmd_formula(args) -> None:
    """跨会话验证「合计分值 = 里程 + w×超车」，并给出 w 与残差。

    §7#4 要的是各加分项折算多少分。左侧面板把 里程/超车/合计 三者同时摆在画面上，
    于是 w 可以直接从「合计−里程 对 超车」的线性关系读出来——**不必额外做一次实机
    实验**。只在三者同时有效（面板在场且都读出）的采样点上比较，避免拿噪声当分母。
    """
    regs = json.loads(Path(args.rects).read_text(encoding="utf-8"))
    pts = []
    for s in sorted((d for d in DEMOS.iterdir() if d.is_dir())):
        files = sorted((s / "frames").glob("*.jpg"))
        if len(files) < 50:
            continue
        _rows, samples = _sample_rows(s, regs, args.every)
        for r, v in samples:
            mi, ov, to = _digits(v["mileage"]), _digits(v["overtake"]), _digits(v["total_left"])
            if None in (mi, ov, to):
                continue
            pts.append((s.name, r["seq"], mi, ov, to, to - mi))
    print(f"三者同时有效 {len(pts)} 个采样点\n")
    if not pts:
        return
    arr = np.array([(p[3], p[5]) for p in pts], dtype=float)
    ov, extra = arr[:, 0], arr[:, 1]
    ratio = extra / np.where(ov == 0, np.nan, ov)
    print(f"超车=0 的点: extra(合计−里程) = {sorted({int(e) for o, e in arr if o == 0})}  （应恒为 0）")
    print(f"超车>0 的点: extra/超车 取值 = {sorted({round(x, 1) for x in ratio if not np.isnan(x)})}")
    for name, seq, mi, o, to, e in pts[:40]:
        print(f"  {name[-2:]} seq={seq:>4} 里程={mi:>4} 超车={o:>2} 合计={to:>4} 差={e:>4}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["discover", "read", "scan", "timeline", "formula"])
    ap.add_argument("--frame", default=None)
    ap.add_argument("--rects", default=None)
    ap.add_argument("--session", default=None)
    ap.add_argument("--per-session", type=int, default=6)
    ap.add_argument("--every", type=int, default=5)
    ap.add_argument("--show-all", action="store_true")
    args = ap.parse_args()
    if args.mode == "discover":
        cmd_discover(args)
    elif args.mode == "scan":
        cmd_scan(args)
    elif args.mode == "formula":
        if not args.rects:
            raise SystemExit("formula 需要 --rects")
        cmd_formula(args)
    elif args.mode == "timeline":
        if not args.session or not args.rects:
            raise SystemExit("timeline 需要 --session 与 --rects")
        cmd_timeline(args)
    else:
        if not args.rects:
            raise SystemExit("read 模式需要 --rects")
        cmd_read(args)


if __name__ == "__main__":
    main()