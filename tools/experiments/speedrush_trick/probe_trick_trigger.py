"""极限动作（120 分）触发状态反查：超车 pass ↔ 事件时钟的分离度测量。

**要回答什么**（`docs/plan/speedrush-control-route.md` 阶段 C 的输入规格题）：
得分主通道是极限动作（≈半壁收入，见 speedrush_scoring/README 结论 11），
但它的触发条件在现有感知里是否可见？判据（先定后看）：
  ① 每个超车 pass 是否都产生分数事件（极限 120 / 普通 30）；
  ② 极限超车 vs 普通超车，在框级特征（横向贴近 min_gap、框高 max_h、
     框高增长率 dhdt）上能否分离——分离则"贴车刷分"有可观测的决策量，
     不分离则要么触发条件不是贴近，要么现有感知看不见它。

**事件时钟的选型**（这是本探针与上一轮配对验证的关键差异）：
  比分显示每 0.5 s 一跳，作事件时刻有 ±0.5 s 模糊；
  HUD 左上「极限超车 ×N」连击计数器的**翻转帧**是逐帧时钟（21 fps 分辨率），
  故标签通道 = banner 翻转（全帧 OCR），分数通道只用来兜底与交叉验证。

**素材**：`data/speedrush/demos/<session>/`（帧 + frames.jsonl）。
三通道全帧读取（比分 / banner / 检测），各自缓存于 %TEMP%/sr_trick/。

**基线（2026-09-18，220750_p2 / 220453_p1 / 200445_p1 三场合并，138 s）**：
  39 个超车 pass；banner 极限超车翻转 31 次，其中 22 个与 pass 配对
  （Δt 中位 −0.3 s，即翻转滞后超车完成约 0.3 s）；未配极限的 17 个 pass 里
  13 个对上 k=1~3 的比分跳（30 分档）→ **每次超车都得分，约 55~60% 升级为极限**。
  分离度（极限 vs 普通）：min_gap AUC 0.226（置换零模型 p=0.003）——**该分离同日撤回**：
  锚挪到 600 时 AUC 塌回 0.50、min 取在远处靠消失点那一拍（by 中位 388 vs 最近帧 494）、
  最近帧 cx 双峰且 640 附近为空——三证指向它测的是**透视收敛**不是贴近。无透视偏置的
  gap_close/dhdt/max_h 全部无信号（p≥0.25）。→ 触发条件在现有框级感知下测不出分离，
  判它需透视归一的横向距离（独立工程）。详见 README ②。

用法：
    python probe_trick_trigger.py <session>          # 单场全链路
    python probe_trick_trigger.py <session> --stage report   # 只重出分析
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tools" / "experiments" / "speedrush_vision"))
from probe_old_model import DEMOS, detect, DEFAULT_MODEL  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "probe_hud_ocr", REPO / "tools" / "experiments" / "speedrush_scoring" / "probe_hud_ocr.py")
hud = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hud)

REGS = json.loads((REPO / "maaracing_master" / "plugins" / "speedrush"
                   / "resources" / "policy" / "hud_regions.json").read_text(encoding="utf-8"))
CACHE = Path(os.environ.get("TEMP", "/tmp")) / "sr_trick"
CACHE.mkdir(exist_ok=True)
NUM = re.compile(r"[×xX]\s*(\d+)")

# 配对窗口：pass 结束 → 事件时刻（banner 翻转实测滞后 ~0.3 s，比分跳滞后 ≤0.5 s）
PAIR_LO, PAIR_HI = -0.8, 0.4


def _frames(sess: Path):
    return [json.loads(x) for x in
            (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]


def _cached(name: str, build):
    p = CACHE / name
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    data = build()
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def score_series(sess_name: str):
    """全帧读右上比分（显示跳的原始序列）。"""
    def build():
        sess = DEMOS / sess_name
        ocr = hud.HudOcr()
        out = []
        for r in _frames(sess):
            rgb = hud.load_rgb(sess / "frames" / r["file"])
            if rgb is None:
                continue
            sa, sb = hud.pair_sides(rgb, REGS["pair_a"], REGS["pair_b"])
            own = "a" if sa == "本机(蓝)" else ("b" if sb == "本机(蓝)" else None)
            if not own:
                continue
            v = hud._digits(ocr.read(rgb, REGS[f"score_blue_{own}"]))
            if v is not None:
                out.append({"ts": r["ts_ns"] / 1e9, "score": int(v)})
        return out
    return _cached(f"score_{sess_name}.json", build)


def banner_series(sess_name: str):
    """全帧读「极限超车/变道 ×N」banner，解析连击数与类型。"""
    def build():
        sess = DEMOS / sess_name
        ocr = hud.HudOcr()
        out = []
        for r in _frames(sess):
            rgb = hud.load_rgb(sess / "frames" / r["file"])
            if rgb is None:
                continue
            txt = ocr.read(rgb, REGS["event_banner"]) or ""
            m = NUM.search(txt)
            n = int(m.group(1)) if m else (1 if ("超车" in txt or "变道" in txt) else 0)
            kind = "超" if "超车" in txt else ("变" if "变道" in txt else "")
            out.append({"ts": r["ts_ns"] / 1e9, "n": n, "kind": kind})
        return out
    return _cached(f"banner_{sess_name}.json", build)


def detections(sess_name: str):
    """全帧旧模型街车框（car / bonus_car），原帧坐标。"""
    def build():
        import onnxruntime as ort
        sess = ort.InferenceSession(str(DEFAULT_MODEL), providers=["CPUExecutionProvider"])
        d = DEMOS / sess_name
        out = []
        for r in _frames(d):
            img = np.asarray(Image.open(d / "frames" / r["file"]).convert("RGB"))
            out.append({"seq": r["seq"], "ts": r["ts_ns"] / 1e9,
                        "cars": [[int(c), round(p, 3), (b[0] + b[2]) / 2, b[3], b[3] - b[1]]
                                 for c, p, b in detect(sess, img) if c in (1, 2)]})
        return out
    return _cached(f"dets_{sess_name}.json", build)


def score_ticks(rows, kmin=4, kmax=12):
    """显示跳分解 Δ = m0 + 30k（m0 按场估计），返回 [(ts, k)]（量子自洽的跳）。"""
    sc = sorted((r["ts"], float(r["score"])) for r in rows)
    ds = [(t1, v1 - v0) for (t0, v0), (t1, v1) in zip(sc, sc[1:]) if v1 != v0]
    if not ds:
        return []
    m0 = float(np.median([d for _, d in ds if d < 45])) if any(d < 45 for _, d in ds) else 21.0
    out = []
    for t, d in ds:
        k = round((d - m0) / 30.0)
        if abs(d - (m0 + 30 * k)) <= 3 and kmin <= k <= kmax:
            out.append((t, int(k)))
    return out


def combo_flips(rows, kind="超"):
    """banner 稳定段（连续 ≥3 帧同值）的连击翻转时刻。"""
    seg, buf = [], []
    for x in rows:
        if buf and (x["n"] != buf[-1]["n"] or x["kind"] != buf[-1]["kind"]):
            if len(buf) >= 3:
                kinds = [b["kind"] for b in buf if b["kind"]]
                seg.append((buf[0]["ts"], buf[0]["n"], kinds[0] if kinds else ""))
            buf = [x]
        else:
            buf.append(x)
    if len(buf) >= 3:
        seg.append((buf[0]["ts"], buf[0]["n"], buf[0]["kind"]))
    return [t1 for (t0, n0, k0), (t1, n1, k1) in zip(seg, seg[1:])
            if k1 == kind and ((n1 > n0 and 1 <= n1 - n0 <= 3) or (n0 == 0 and n1 >= 1))]


def track_passes(dets):
    """街车轨迹 → 超车 pass（终点贴近画面下部 = 被自车超过后出画/被遮挡）。"""
    tracks = []
    for row in dets:
        t = row["ts"]
        for cls, score, cx, by, h in row["cars"]:
            best, bd = None, 1e9
            for tr in tracks:
                if tr["dead"] or tr["cls"] != cls:
                    continue
                lt, _, lx, _, lh = tr["pts"][-1]
                if t - lt > 0.6:
                    tr["dead"] = True
                    continue
                d = abs(lx - cx) + abs(lh - h) * 1.5
                if d < bd:
                    best, bd = tr, d
            if best is not None and bd < 160:
                best["pts"].append((t, cls, cx, by, h))
                best["dead"] = False
                best["miss"] = 0
            else:
                tracks.append({"cls": cls, "pts": [(t, cls, cx, by, h)], "dead": False, "miss": 0})
        for tr in tracks:
            if not tr["dead"] and all(p[0] != t for p in tr["pts"][-1:]):
                tr["miss"] += 1
                if tr["miss"] > 4:
                    tr["dead"] = True
    passes = []
    for tr in tracks:
        pts = tr["pts"]
        if len(pts) < 6:
            continue
        if pts[-1][3] < 430 or max(p[4] for p in pts) < max(90, pts[0][4] * 1.4):
            continue
        near = [p for p in pts if p[3] > 380]
        closest = max(pts, key=lambda p: p[3])
        tail = pts[-6:]
        passes.append({
            "t_end": pts[-1][0], "cls": tr["cls"],
            "min_gap": min(abs(p[2] - 640) for p in near),
            "gap_close": abs(closest[2] - 640),
            "max_h": max(p[4] for p in pts),
            "dhdt": (tail[-1][4] - tail[0][4]) / max(tail[-1][0] - tail[0][0], 1e-6),
            "pts": pts})   # 2026-09-21 增补：透视归一重测（probe_trick_pxnear）需逐帧 (t,cls,cx,by,h)
    return passes


def auc(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if not len(a) or not len(b):
        return float("nan")
    return float(((a[:, None] > b[None, :]).sum()
                  + 0.5 * (a[:, None] == b[None, :]).sum()) / (len(a) * len(b)))


def report(sessions):
    ext, norm = [], []
    n_flip_all = n_tick_all = 0
    for s in sessions:
        P = track_passes(detections(s))
        flips = combo_flips(banner_series(s))
        tk = score_ticks(score_series(s), kmin=1)
        n_flip_all += len(flips)
        n_tick_all += sum(1 for _, k in tk if k >= 4)
        used = set()
        for f in flips:
            cand = [(abs(p["t_end"] - f), i) for i, p in enumerate(P)
                    if PAIR_LO <= p["t_end"] - f <= PAIR_HI and i not in used]
            if cand:
                used.add(min(cand)[1])
        rest = [p for i, p in enumerate(P) if i not in used]
        k13 = [t for t, k in tk if 1 <= k <= 3]
        paired30 = [p for p in rest if any(PAIR_LO <= p["t_end"] - t <= PAIR_HI for t in k13)]
        print(f"{s}: pass {len(P)}  极限翻转 {len(flips)}（配 pass {len(used)}）  "
              f"k≥4 跳 {sum(1 for _, k in tk if k >= 4)}  "
              f"剩余 pass {len(rest)} 中 {len(paired30)} 对上 30 分档")
        for i, p in enumerate(P):
            p["_ext"] = i in used
        ext += [p for p in P if p["_ext"]]
        norm += [p for p in P if not p["_ext"]]
    print(f"\n合计：极限 {len(ext)} / 普通 {len(norm)}（翻转 {n_flip_all}、k≥4 跳 {n_tick_all}）")
    lab = np.array([1] * len(ext) + [0] * len(norm))
    for feat in ("min_gap", "gap_close", "dhdt", "max_h"):
        g = np.array([p[feat] for p in ext + norm])
        obs = auc(g[lab == 1], g[lab == 0])
        rng = np.random.default_rng(3)
        null = np.array([auc(g[sh == 1], g[sh == 0])
                         for sh in (rng.permutation(lab) for _ in range(2000))])
        p2 = 2 * min((null <= obs).mean(), (null >= obs).mean())
        print(f"  {feat:9} 极限中位 {np.median(g[lab == 1]):5.0f} vs 普通 {np.median(g[lab == 0]):5.0f}"
              f"  AUC {obs:.3f}  置换 p={p2:.3f}")
    print("（AUC<0.5 = 极限组该值更小；min_gap 为最近接近时 |cx−画面中心|，px）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--stage", default="report", choices=["report"])
    args = ap.parse_args()
    report(args.sessions)
