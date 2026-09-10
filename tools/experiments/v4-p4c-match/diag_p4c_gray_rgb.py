# -*- coding: utf-8 -*-
"""P4c 对拍：detector/内联匹配 灰度引擎 vs template_match rgb 引擎（真机录帧离线回放）。

目的（v4-p4-retire.md §2 P4c 风险缓解条款）：
  A. 逐锚点分数对照——灰度→rgb 后每个模板锚点的最高分分布、命中翻转清单
     （gray 命中而 rgb 未命中 = 需要调阈/换图的红线信号）；
  B. 成本对照——逐锚点 ms（按生产 active-set 汇总成阶段帧成本，校验 125ms 预算）；
  C. 光标遮挡探针——采样帧上逐帧 detect_cursor 定位光标圆盘，凡光标压住某锚点 ROI，
     把该帧归入「遮挡样本」桶，对照两引擎在遮挡下的命中差（auto_shoo 删除后的
     常态画面就是"光标停在点击点"，此处直接量它的影响面）。

帧源：%APPDATA%\\MaaRacingAssistant\\debug\\treasure\\<session>\\raw\\NNNN_raw.jpg
     （jpg 有损压缩对两引擎同等影响；生产为 WGC 无损帧——离线为保守代理）。
锚点/阈值真源：policy.json perception 数据面（load_nav_source），阈值解析逻辑
     与 detector._detect_legacy 逐行同构（per-模板 arb → spec.threshold → 默认）。

用法：
  .venv\\Scripts\\python.exe tools/experiments/v4-p4c-match/diag_p4c_gray_rgb.py \
      [--sessions 20260906_181301,20260906_203109] [--per-anchor 25] [--miss 50] [--smoke]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_assistant.core.navkit.v4_source import load_nav_source  # noqa: E402
from maaracing_assistant.core.gamepad_cursor import detect_cursor  # noqa: E402

POLICY_PATH = ROOT / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"
IMAGE_DIR = ROOT / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "image"
DEBUG_ROOT = Path.home() / "AppData" / "Roaming" / "MaaRacingAssistant" / "debug" / "treasure"

_gray_tpl_cache: dict[str, np.ndarray | None] = {}
_rgb_tpl_cache: dict[str, np.ndarray | None] = {}


def _stem(name: str) -> str:
    return Path(name).stem


def load_gray(name: str) -> np.ndarray | None:
    if name in _gray_tpl_cache:
        return _gray_tpl_cache[name]
    img = cv2.imread(str(IMAGE_DIR / f"{_stem(name)}.png"))
    g = None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _gray_tpl_cache[name] = g
    return g


def load_rgb(name: str) -> np.ndarray | None:
    if name in _rgb_tpl_cache:
        return _rgb_tpl_cache[name]
    img = cv2.imread(str(IMAGE_DIR / f"{_stem(name)}.png"))
    r = None if img is None else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    _rgb_tpl_cache[name] = r
    return r


def crop_box(rect, W, H):
    x1, y1, x2n, y2n = rect
    x1 = max(0, int(x1 * W))
    y1 = max(0, int(y1 * H))
    x2 = min(W, int(x2n * W))
    y2 = min(H, int(y2n * H))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def gray_score(gray_big, tpl, box, W, H, scales) -> float:
    """复刻 detector._match_local：crop 灰度全帧 + AREA/CUBIC 缩放模板。"""
    x1, y1, x2, y2 = box
    crop = gray_big[y1:y2, x1:x2]
    ch, cw = crop.shape[:2]
    th0, tw0 = tpl.shape[:2]
    best = 0.0
    for s in scales:
        nw = max(4, int(round(tw0 * s)))
        nh = max(4, int(round(th0 * s)))
        if nh > ch or nw > cw:
            continue
        tpl_s = tpl if (nw == tw0 and nh == th0) else cv2.resize(
            tpl, (nw, nh), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC)
        try:
            _, mx, _, _ = cv2.minMaxLoc(cv2.matchTemplate(crop, tpl_s, cv2.TM_CCOEFF_NORMED))
        except cv2.error:
            continue
        if float(mx) > best:
            best = float(mx)
    return best


def rgb_score(rgb_big, tpl, box, W, H, scales) -> float:
    """复刻 template_match.find_template 的 rgb 语义：INTER_LINEAR + fx/fy 浮点缩放。"""
    x1, y1, x2, y2 = box
    search = rgb_big[y1:y2, x1:x2]
    sh, sw = search.shape[:2]
    best = 0.0
    for s in scales:
        resized = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
        if resized.shape[0] > sh or resized.shape[1] > sw:
            continue
        try:
            _, mx, _, _ = cv2.minMaxLoc(cv2.matchTemplate(search, resized, cv2.TM_CCOEFF_NORMED))
        except cv2.error:
            continue
        if float(mx) > best:
            best = float(mx)
    return best


def resolve_th(anchor, tpl_name, default_th) -> float:
    arb = (anchor.arbitration or {}).get("template_thresholds") or {}
    v = arb.get(tpl_name)
    if v is None:
        v = arb.get(_stem(tpl_name))
    if v is not None:
        return float(v)
    if isinstance(anchor.threshold, float):
        return anchor.threshold
    return float(default_th)


def scan_frame(rgb, gray, W, H, anchors, scales, timing=None):
    """逐锚点双引擎扫描。返回 {name: (g_best, g_second, g_ok, r_best, r_second, r_ok)}

    timing 传入 {"g": {name: s}, "r": {name: s}} 时累计逐锚点秒数。
    """
    out = {}
    for name, a, th_by_tpl in anchors:
        g_best, g_second, g_hit = 0.0, 0.0, None
        r_best, r_second, r_hit = 0.0, 0.0, None
        for t in a.templates:
            box = crop_box(a.rect.as_list(), W, H)
            if box is None:
                break
            th = th_by_tpl[t]
            t0 = time.perf_counter()
            gt = load_gray(t)
            if gt is not None:
                s = gray_score(gray, gt, box, W, H, scales)
                if s > g_best:
                    g_second = g_best
                    g_best = s
                elif s > g_second:
                    g_second = s
                if s >= th and g_hit is None:
                    g_hit = t
            if timing is not None:
                timing["g"][name] = timing["g"].get(name, 0.0) + (time.perf_counter() - t0)
            t0 = time.perf_counter()
            rt = load_rgb(t)
            if rt is not None:
                s = rgb_score(rgb, rt, box, W, H, scales)
                if s > r_best:
                    r_second = r_best
                    r_best = s
                elif s > r_second:
                    r_second = s
                if s >= th and r_hit is None:
                    r_hit = t
            if timing is not None:
                timing["r"][name] = timing["r"].get(name, 0.0) + (time.perf_counter() - t0)
        margin = float((a.arbitration or {}).get("margin", 0.0))
        g_ok = g_hit is not None and (margin <= 0 or (g_best - g_second) >= margin)
        r_ok = r_hit is not None and (margin <= 0 or (r_best - r_second) >= margin)
        out[name] = (g_best, g_second, g_ok, r_best, r_second, r_ok)
    return out


def parse_trace(session_dir: Path):
    frames = {}   # frame_no -> hit_anchor
    clicks = []   # (frame_no, key, center)
    for line in (session_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "scores" in rec:
            frames[rec["frame"]] = rec.get("hit_anchor")
        elif rec.get("event") == "intent_submitted":
            it = rec.get("intent") or {}
            if it.get("center") and it.get("key"):
                clicks.append((rec["frame"], it["key"], it["center"]))
    return frames, clicks


def even_sample(items, k):
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default="20260906_181301")
    ap.add_argument("--per-anchor", type=int, default=25)
    ap.add_argument("--miss", type=int, default=50)
    ap.add_argument("--no-cursor", action="store_true", help="跳过逐帧光标定位")
    ap.add_argument("--smoke", action="store_true", help="只跑 6 帧验证脚本本身")
    args = ap.parse_args()

    nav = load_nav_source(POLICY_PATH)
    plan = nav.plan
    scales = tuple(plan.scales)
    anchors = []
    for name, a in nav.spec.items():
        if a.kind != "template" or not a.templates:
            continue
        if crop_box(a.rect.as_list(), 1280, 720) is None:
            continue
        ths = {t: resolve_th(a, t, plan.default_threshold) for t in a.templates}
        anchors.append((name, a, ths))
    detect_set = set(plan.detect_anchors)
    print(f"锚点数: {len(anchors)}（阶段检测 {len(detect_set)}，内联/独立 {len(anchors)-len(detect_set)}），"
          f"尺度表 {len(scales)} 档，默认阈 {plan.default_threshold}")

    stats = {name: {"n": 0, "g_hit": 0, "r_hit": 0, "flip_g2r": [], "flip_r2g": [],
                    "delta": []} for name, _, _ in anchors}
    occ = {name: {"n": 0, "g_hit": 0, "r_hit": 0, "g_scores": [], "r_scores": [],
                  "flip_g2r": []} for name, _, _ in anchors}
    timing = {"g": {}, "r": {}}
    n_total = 0
    cursor_seen = cursor_none = 0
    for sess in [s.strip() for s in args.sessions.split(",") if s.strip()]:
        session_dir = DEBUG_ROOT / sess
        if not session_dir.is_dir():
            print(f"[skip] 会话目录不存在: {session_dir}")
            continue
        frames, _clicks = parse_trace(session_dir)
        by_anchor = defaultdict(list)
        for fno, ha in frames.items():
            if ha:
                by_anchor[ha].append(fno)
        miss_frames = [f for f, ha in frames.items() if not ha]
        sample = set()
        if args.smoke:
            sample.update(sorted(frames)[:6])
        else:
            for ha, lst in by_anchor.items():
                sample.update(even_sample(sorted(lst), args.per_anchor))
            sample.update(even_sample(sorted(miss_frames), args.miss))
        sample = sorted(sample)
        print(f"会话 {sess}: trace 帧 {len(frames)}，采样 {len(sample)}")
        for i, fno in enumerate(sample):
            raw = session_dir / "raw" / f"{fno:04d}_raw.jpg"
            if not raw.exists():
                continue
            bgr = cv2.imread(str(raw))
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            H, W = rgb.shape[:2]
            res = scan_frame(rgb, gray, W, H, anchors, scales, timing)
            n_total += 1
            cur = None
            if not args.no_cursor and not args.smoke:
                _, cur = detect_cursor(rgb)
            for name, (gb, gs, gk, rb, rs, rk) in res.items():
                st = stats[name]
                st["n"] += 1
                st["g_hit"] += int(gk)
                st["r_hit"] += int(rk)
                st["delta"].append(rb - gb)
                if gk and not rk:
                    st["flip_g2r"].append(f"{sess}#{fno}")
                if rk and not gk:
                    st["flip_r2g"].append(f"{sess}#{fno}")
                if cur is not None:
                    a = next(x[1] for x in anchors if x[0] == name)
                    box = crop_box(a.rect.as_list(), W, H)
                    r = max(12.0, float(cur.radius_est) + float(getattr(cur, "ring_thick", 0) or 0) + 4.0)
                    cx, cy = cur.pos
                    if box and box[0] - r <= cx <= box[2] + r and box[1] - r <= cy <= box[3] + r:
                        o = occ[name]
                        o["n"] += 1
                        o["g_hit"] += int(gk)
                        o["r_hit"] += int(rk)
                        o["g_scores"].append(gb)
                        o["r_scores"].append(rb)
                        if gk and not rk:
                            o["flip_g2r"].append(f"{sess}#{fno}")
            if (i + 1) % 60 == 0:
                print(f"  [{sess}] ... {i+1}/{len(sample)} 帧")
        if not args.no_cursor:
            pass
    if n_total == 0:
        print("[FAIL] 无可用帧")
        return 1

    print("\n===== A：逐锚点 灰度 vs rgb（全部采样帧）=====")
    print(f"{'anchor':38} {'kind':6} {'n':>4} {'gHit':>5} {'rHit':>5} {'g→r翻转':>7} {'r→g翻转':>7} "
          f"{'Δ均值':>7} {'Δ最差':>7}")
    report = {"anchors": {}, "occ": {}, "ms": {}}
    for name, a, _ in anchors:
        st = stats[name]
        if not st["n"]:
            continue
        dmin = min(st["delta"]) if st["delta"] else 0.0
        dmean = statistics.fmean(st["delta"]) if st["delta"] else 0.0
        kind = "det" if name in detect_set else "inline"
        print(f"{name:38} {kind:6} {st['n']:>4} {st['g_hit']:>5} {st['r_hit']:>5} "
              f"{len(st['flip_g2r']):>7} {len(st['flip_r2g']):>7} {dmean:>+7.3f} {dmin:>+7.3f}")
        if st["flip_g2r"]:
            print(f"    g→r 翻转帧: {st['flip_g2r'][:12]}")
        report["anchors"][name] = {
            "kind": kind, "n": st["n"], "gray_hits": st["g_hit"], "rgb_hits": st["r_hit"],
            "flip_gray_only": st["flip_g2r"][:40], "flip_rgb_only": st["flip_r2g"][:40],
            "delta_mean": round(dmean, 4), "delta_worst": round(dmin, 4),
        }

    print("\n===== B：逐锚点单帧成本（ms，采样均值）=====")
    ms = {}
    for name, _, _ in anchors:
        g = timing["g"].get(name, 0.0) / n_total * 1000
        r = timing["r"].get(name, 0.0) / n_total * 1000
        ms[name] = (g, r)
        report["ms"][name] = {"gray_ms": round(g, 2), "rgb_ms": round(r, 2)}
    for name, (g, r) in sorted(ms.items(), key=lambda kv: -kv[1][1]):
        print(f"{name:38} 灰度 {g:6.2f}  rgb {r:6.2f}  ({r/max(1e-9, g):4.2f}×)")
    tot_g = sum(g for g, _ in ms.values())
    tot_r = sum(r for _, r in ms.values())
    print(f"全量合计/帧: 灰度 {tot_g:.1f}ms → rgb {tot_r:.1f}ms（{tot_r/max(1e-9,tot_g):.2f}×）")
    # 生产阶段口径：detector 只扫 active ∪ global，逐阶段估成本
    print("\n-- 生产阶段口径（detector active∪global 的 rgb 成本估算）--")
    for stage, act in sorted(plan.active.items()):
        keys = set(act) | set(plan.global_anchors)
        cost_r = sum(ms.get(k, (0, 0))[1] for k in keys)
        cost_g = sum(ms.get(k, (0, 0))[0] for k in keys)
        print(f"  {stage:20} 锚点{len(keys):>2}  灰度 {cost_g:6.1f}ms → rgb {cost_r:6.1f}ms")

    if not args.no_cursor:
        print("\n===== C：光标压 ROI 的遮挡样本（逐帧 detect_cursor 归桶）=====")
        print(f"{'anchor(被压住)':38} {'n':>4} {'gHit':>5} {'rHit':>5} {'g分均值':>8} {'r分均值':>8} "
              f"{'r分最差':>8} {'g→r翻转':>7}")
        any_occ = False
        for name, o in occ.items():
            if not o["n"]:
                continue
            any_occ = True
            report["occ"][name] = {
                "n": o["n"], "gray_hits": o["g_hit"], "rgb_hits": o["r_hit"],
                "g_mean": round(statistics.fmean(o["g_scores"]), 4),
                "r_mean": round(statistics.fmean(o["r_scores"]), 4),
                "r_min": round(min(o["r_scores"]), 4),
                "flip_gray_only": o["flip_g2r"][:20],
            }
            print(f"{name:38} {o['n']:>4} {o['g_hit']:>5} {o['r_hit']:>5} "
                  f"{statistics.fmean(o['g_scores']):>8.3f} {statistics.fmean(o['r_scores']):>8.3f} "
                  f"{min(o['r_scores']):>8.3f} {len(o['flip_g2r']):>7}")
        if not any_occ:
            print("（采样帧中光标未压在任一锚点 ROI 上——shoo 在线时属正常）")

    out = Path(__file__).with_name("report_gray_rgb.json")
    out.write_text(json.dumps({"frames": n_total, **report}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n[OK] 报告落盘 {out}（共 {n_total} 帧）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
