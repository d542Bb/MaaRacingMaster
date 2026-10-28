"""极限超车事件的 HUD 直读 + 2 秒窗口特征对照（2026-10-22）。

**要回答什么**：游戏里「极限超车」连击计数每 +1 就是一次成功触发。把这一刻从
逐拍原始帧的 `event_banner` 区域读出来，再和同拍 trace 的超车事件/米制观测量
对齐，就能问「触发的那次超车，前 2 秒长什么样」以及「没触发的超车差在哪」。

**判据口径**：
- 横幅读数只在含「极限」「超车」时算在场；带 `×N` 取 N，不带 `×` 记 0（首档/清档）。
- 事件 = 计数相对上一读数发生变化，且与上一事件间隔 ≥0.3s（防 OCR 抖字）。
- 配对 = 事件后 0~1.2s 内最近的超车事件（每个超车只用一次）；实测事件领先超车
  事件 0.37~0.52s——游戏在并排瞬间判极限，跟踪链要等框出下带才发事件。

**窗口特征**（事件前 2s、后 0.5s）：配对车的米制横距/纵深/接近速/锚龄出处、
自车道位与目标横位的变化幅度、选择层分布、占用影子在场拍。分成「触发极限」
与「未触发」两组给分位数，判据差异从对照里读，不预设阈值。

依赖 `probe_hud_ocr.py` 的最小 OCR 引擎与区域真源（同目录，插件 `hud_regions.json`）。

用法：
    python tools/experiments/speedrush_scoring/probe_extreme_events.py \
        20261022_172231_p2 20261022_172146_p1 [--cache <json>] [--dump-windows]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_hud_ocr import DEFAULT_RECTS, HudOcr  # noqa: E402

ROOT = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster"
        / "debug" / "speedrush")
BANNER_KEYS = ("极限", "超车")
LEAD_MIN_S = -0.2   # 超车事件相对极限事件的允许滞后窗
LEAD_MAX_S = 1.2
EVENT_GAP_S = 0.3
WIN_BEFORE_S = 2.0
WIN_AFTER_S = 0.5


def _session_rows(name: str) -> list[dict]:
    p = ROOT / name / f"trace_{name}.jsonl"
    if not p.exists():
        raise SystemExit(f"找不到 {p}")
    return [json.loads(line) for line in p.open(encoding="utf-8")]


def _parse_banner(text: str) -> tuple[bool, int | None]:
    """→ (在场, 计数)。不带 ×N 的「极限超车」记计数 None（首档）。"""
    if not all(k in text for k in BANNER_KEYS):
        return False, None
    clean = text.replace("O", "0").replace("l", "1").replace("I", "1")
    m = re.search(r"(\d+)\s*$", clean)
    return True, int(m.group(1)) if m else None


def banner_timeline(name: str, cache: Path | None) -> list[tuple[bool, int | None]]:
    """逐拍读横幅；`cache` 命中则不重跑 OCR（一张 jpg 一帧，20Hz）。"""
    if cache and cache.exists():
        stored = json.loads(cache.read_text(encoding="utf-8"))
        if name in stored:
            return [(bool(h), v) for h, v in stored[name]]
    rows = _session_rows(name)
    rect = json.loads(Path(DEFAULT_RECTS).read_text(encoding="utf-8"))["event_banner"]
    raw_dir = ROOT / name / "raw"
    ocr = HudOcr()
    out: list[tuple[bool, int | None]] = []
    for i in range(len(rows)):
        f = raw_dir / f"{i + 1:04d}_raw.jpg"
        if not f.exists():
            out.append((False, None))
            continue
        rgb = np.asarray(Image.open(f).convert("RGB"))
        out.append(_parse_banner(ocr.read(rgb, rect)))
    if cache:
        stored = (json.loads(cache.read_text(encoding="utf-8"))
                  if cache.exists() else {})
        stored[name] = [[int(h), v] for h, v in out]
        cache.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return out


def detect_events(rows: list[dict],
                  tl: list[tuple[bool, int | None]]) -> list[dict]:
    """计数变化即事件；`None` 计档（无 ×N）折成 0，与清档同值但不重复计。"""
    ts = [r["ts_ns"] / 1e9 for r in rows]
    events: list[dict] = []
    last: int | None = None
    last_t = -9e9
    for i, (present, v) in enumerate(tl):
        if not present:
            continue
        cur = 0 if v is None else v
        if cur != last and ts[i] - last_t >= EVENT_GAP_S:
            events.append({"ts": ts[i], "fid": rows[i]["fid"],
                           "seq": i + 1, "value": cur, "prev": last})
            last_t = ts[i]
        last = cur
    return events


def _passes(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        for e in (r.get("passes") or []):
            out.append({"ts": r["ts_ns"] / 1e9, "fid": r["fid"],
                        "id": int(e["id"]), "d_min": float(e["d_min"])})
    return out


def pair(events: list[dict], passes: list[dict]) -> tuple[list[dict], list[dict]]:
    """极限事件 → 滞后窗内最近的未用超车；返回 (配对表, 未配对超车)。"""
    used: set[int] = set()
    pairs: list[dict] = []
    for ev in events:
        cands = [p for p in passes
                 if p["id"] not in used and LEAD_MIN_S <= p["ts"] - ev["ts"] <= LEAD_MAX_S]
        if not cands:
            pairs.append({**ev, "pass": None})
            continue
        best = min(cands, key=lambda p: abs(p["ts"] - ev["ts"]))
        used.add(best["id"])
        pairs.append({**ev, "pass": best})
    return pairs, [p for p in passes if p["id"] not in used]


def _car_window(rows: list[dict], ts_lo: float, ts_hi: float,
                tid: int) -> dict:
    """配对车在窗口内的米制读数（mcars）与出处。"""
    xs: list[float] = []          # |x| with z>2（自车相机系横距）
    zs: list[float] = []
    v_close: list[float] = []
    src: dict[str, int] = {}
    for r in rows:
        t = r["ts_ns"] / 1e9
        if not (ts_lo <= t <= ts_hi):
            continue
        for m in (r.get("mcars") or []):
            if int(m["id"]) != tid:
                continue
            src[m["src"]] = src.get(m["src"], 0) + 1
            z, x, v = m.get("z"), m.get("x"), m.get("v")
            if z is not None:
                zs.append(float(z))
                if z > 2.0 and x is not None:
                    xs.append(abs(float(x)))
            if v is not None:
                v_close.append(float(v))
    return {"x_abs": xs, "z": zs, "v": v_close, "src": src}


def _ego_window(rows: list[dict], ts_lo: float, ts_hi: float) -> dict:
    lanes: list[float] = []
    targets: list[float] = []
    layers: dict[int, int] = {}
    occ_ticks = 0
    ticks = 0
    for r in rows:
        t = r["ts_ns"] / 1e9
        if not (ts_lo <= t <= ts_hi):
            continue
        ticks += 1
        if r.get("executed_lane") is not None:
            lanes.append(float(r["executed_lane"]))
        if r.get("x_target") is not None:
            targets.append(float(r["x_target"]))
        o = r.get("occ")
        if o:
            occ_ticks += 1
        sl = r.get("sel_layer")
        if sl:
            layers[int(sl)] = layers.get(int(sl), 0) + 1
    spread = (max(lanes) - min(lanes)) if len(lanes) >= 2 else 0.0
    return {"ticks": ticks, "lane_spread": spread,
            "target_spread": (max(targets) - min(targets))
            if len(targets) >= 2 else 0.0,
            "layers": layers, "occ_ticks": occ_ticks}


def windows(rows: list[dict], pairs: list[dict], rest: list[dict]) -> dict:
    """两组（触发/未触发）超车各自的 2 秒窗口特征表。"""
    ts = [r["ts_ns"] / 1e9 for r in rows]
    out = {"extreme": [], "plain": []}
    items = [("extreme", [p for p in pairs if p.get("pass")]),
             ("plain", rest)]
    for key, group in items:
        for p in group:
            anchor = p["pass"]["ts"] if key == "extreme" else p["ts"]
            tid = p["pass"]["id"] if key == "extreme" else p["id"]
            d_min = (p["pass"]["d_min"] if key == "extreme" else p["d_min"])
            car = _car_window(rows, anchor - WIN_BEFORE_S,
                              anchor + WIN_AFTER_S, tid)
            ego = _ego_window(rows, anchor - WIN_BEFORE_S,
                              anchor + WIN_AFTER_S)
            out[key].append({"id": tid, "d_min": d_min,
                             "lead_s": (p["pass"]["ts"] - p["ts"])
                             if key == "extreme" else None,
                             "car": car, "ego": ego})
        _ = ts
    return out


def _q(vals: list[float]) -> str:
    if not vals:
        return "无"
    s = sorted(vals)
    pick = lambda q: s[min(len(s) - 1, int(q * (len(s) - 1)))]
    return (f"n={len(s)} min={s[0]:.2f} p50={pick(0.5):.2f} "
            f"p90={pick(0.9):.2f} max={s[-1]:.2f}")


def report(name: str, rows: list[dict], tl, pairs, rest, wins) -> None:
    events = [p for p in pairs]
    matched = [p for p in events if p.get("pass")]
    print(f"\n===== {name}  拍 {len(rows)}  极限事件 {len(events)}"
          f"  配对 {len(matched)}  超车事件 {len(matched) + len(rest)}"
          f"  未触发 {len(rest)}")
    for p in events:
        ps = p.get("pass")
        print(f"  ×{p['value']}(前{p['prev']}) seq{p['seq']:04d} fid{p['fid']}"
              + (f" → 车{ps['id']} d_min={ps['d_min']:.2f}"
                 f" 滞后{ps['ts'] - p['ts']:+.2f}s" if ps else " → 无配对超车"))

    for key, label in (("extreme", "触发极限"), ("plain", "未触发")):
        g = wins[key]
        if not g:
            continue
        print(f"\n  [{label}] n={len(g)}")
        print("    超车最近横向间距 d_min : " + _q([x["d_min"] for x in g]))
        print("    窗口内米制横距 |x| : "
              + _q([min(x["car"]["x_abs"]) for x in g if x["car"]["x_abs"]]))
        print("    窗口内最大接近速 v : "
              + _q([max(x["car"]["v"]) for x in g if x["car"]["v"]]))
        print("    自车道位摆幅(道) : "
              + _q([x["ego"]["lane_spread"] for x in g]))
        print("    目标横位摆幅(道) : "
              + _q([x["ego"]["target_spread"] for x in g]))
        print("    占用影子在场拍占比 : "
              + _q([x["ego"]["occ_ticks"] / max(1, x["ego"]["ticks"])
                    for x in g]))
        lay: dict[int, int] = {}
        for x in g:
            for k, v in x["ego"]["layers"].items():
                lay[k] = lay.get(k, 0) + v
        tot = sum(lay.values()) or 1
        print("    选择层占比 : " + " ".join(
            f"L{k}={lay[k] / tot:.0%}" for k in sorted(lay)))
        src: dict[str, int] = {}
        for x in g:
            for k, v in x["car"]["src"].items():
                src[k] = src.get(k, 0) + v
        print("    该车距离出处拍 : " + (" ".join(
            f"{k}={v}" for k, v in sorted(src.items(), key=lambda kv: -kv[1]))
            or "无"))


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--cache", default=None, help="横幅读数缓存 json（跨次复用）")
    args = ap.parse_args(argv[1:])
    cache = Path(args.cache) if args.cache else None
    agg = {"extreme": [], "plain": []}
    for name in args.sessions:
        rows = _session_rows(name)
        tl = banner_timeline(name, cache)
        events = detect_events(rows, tl)
        pairs, rest = pair(events, _passes(rows))
        wins = windows(rows, pairs, rest)
        report(name, rows, tl, pairs, rest, wins)
        for k in agg:
            agg[k].extend(wins[k])
    if len(args.sessions) > 1:
        print("\n===== 合计")
        for key, label in (("extreme", "触发极限"), ("plain", "未触发")):
            g = agg[key]
            if not g:
                continue
            print(f"  [{label}] n={len(g)} d_min: " + _q([x["d_min"] for x in g])
                  + " | 窗口米制横距 min|x|: "
                  + _q([min(x["car"]["x_abs"]) for x in g if x["car"]["x_abs"]]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
