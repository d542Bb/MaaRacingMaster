# -*- coding: utf-8 -*-
"""P4c 分歧帧取证：单帧上对照三种口径的 settle/round 等锚点分数。

  A) 旧灰度算法（2026-09-06 生产语义：单通道 + AREA/CUBIC）——应复现 trace 分数；
  B) 新生产 detector（policy 阈值 + 按锚点 colorspace）——看 hit_anchor 与分数；
  C) 同锚点强制 rgb 分数——量化色彩空间单独的影响。

用法：.venv\\Scripts\\python.exe diag_p4c_frame_probe.py <sess#frame>[ ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_assistant.core.navkit.v4_source import load_nav_source  # noqa: E402
from maaracing_assistant.core.template_match import best_match_score, load_template  # noqa: E402
from maaracing_assistant.plugins.treasure.detector import TreasureStageDetector  # noqa: E402

DEBUG_ROOT = Path.home() / "AppData" / "Roaming" / "MaaRacingAssistant" / "debug" / "treasure"
POLICY_PATH = (ROOT / "maaracing_assistant" / "plugins" / "treasure"
               / "resources" / "policy" / "treasure.policy.json")
IMAGE_DIR = ROOT / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "image"

nav = load_nav_source(POLICY_PATH)
plan = nav.plan
det = TreasureStageDetector(None)


def old_gray_score(tpl_name, rect, gray, W, H):
    x1, y1, x2n, y2n = rect
    px1, py1 = max(0, int(x1 * W)), max(0, int(y1 * H))
    px2, py2 = min(W, int(x2n * W)), min(H, int(y2n * H))
    crop = gray[py1:py2, px1:px2]
    img = cv2.imread(str(IMAGE_DIR / f"{Path(tpl_name).stem}.png"))
    if img is None or crop.size == 0:
        return 0.0
    tpl = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    th0, tw0 = tpl.shape[:2]
    best = 0.0
    for s in plan.scales:
        nw = max(4, int(round(tw0 * s)))
        nh = max(4, int(round(th0 * s)))
        if nh > crop.shape[0] or nw > crop.shape[1]:
            continue
        t = tpl if (nw == tw0 and nh == th0) else cv2.resize(
            tpl, (nw, nh), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC)
        _, mx, _, _ = cv2.minMaxLoc(cv2.matchTemplate(crop, t, cv2.TM_CCOEFF_NORMED))
        best = max(best, float(mx))
    return best


def main():
    specs = sys.argv[1:] or ["20260906_181301#820"]
    for ref in specs:
        sess, fno = ref.split("#")
        fno = int(fno)
        sdir = DEBUG_ROOT / sess
        trace = {}
        for line in (sdir / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("frame") == fno and "scores" in rec:
                trace = rec
                break
        if not trace:
            print(f"[{ref}] trace 无此帧")
            continue
        bgr = cv2.imread(str(sdir / "raw" / f"{fno:04d}_raw.jpg"))
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        H, W = rgb.shape[:2]
        active = set(trace.get("active_used") or ())
        print(f"\n===== {ref} =====")
        print(f"old: stage={trace.get('stage')} hit={trace.get('hit_anchor')} active={sorted(active)}")
        for name in sorted(active | {trace.get("hit_anchor")}):
            a = plan.spec.get(name)
            if a is None or not a.templates:
                continue
            ths = []
            for t in a.templates:
                og = old_gray_score(t, tuple(a.rect), gray, W, H)
                tpl = load_template(t, [IMAGE_DIR])
                _b, rg = best_match_score(rgb, tpl, scales=plan.scales,
                                          roi=(max(0, int(a.rect[0] * W)), max(0, int(a.rect[1] * H)),
                                               min(W, int(a.rect[2] * W)) - max(0, int(a.rect[0] * W)),
                                               min(H, int(a.rect[3] * H)) - max(0, int(a.rect[1] * H)))) \
                    if tpl is not None else (None, 0.0)
                _b2, gg = best_match_score(gray, cv2.cvtColor(tpl, cv2.COLOR_RGB2GRAY),
                                           scales=plan.scales,
                                           roi=(max(0, int(a.rect[0] * W)), max(0, int(a.rect[1] * H)),
                                                min(W, int(a.rect[2] * W)) - max(0, int(a.rect[0] * W)),
                                                min(H, int(a.rect[3] * H)) - max(0, int(a.rect[1] * H)))) \
                    if tpl is not None else (None, 0.0)
                ths.append((Path(t).stem, trace.get("scores", {}).get(name), og, gg, float(rg), a.colorspace))
            for stem, ts, og, gg, rg, cs in ths:
                print(f"  {name}/{stem:32} trace={_fmt(ts)} 旧算={og:.3f} 新灰={gg:.3f} 新彩={rg:.3f} cs={cs}")
        res = det.detect(rgb, active or None)
        print(f"new: stage={res.stage} hit={res.hit_anchor} scores={ {k: round(v,3) for k, v in res.scores.items()} }")


def _fmt(v):
    return f"{v:.3f}" if isinstance(v, float) else "--"


if __name__ == "__main__":
    main()
