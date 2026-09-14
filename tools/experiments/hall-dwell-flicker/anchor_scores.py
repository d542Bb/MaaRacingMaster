# -*- coding: utf-8 -*-
"""逐帧报某个图节点（含 Or 引用的锚点节点）在各帧上的最高匹配分——锚点标定用。

与 replay_pick.py 的分工：replay 回答「这帧会被判成哪个页面」，本脚本回答
「为什么、差多少」，用于给误命中的锚点重新选区或加守卫。

用法：
    .venv\\Scripts\\python.exe tools/experiments/hall-dwell-flicker/anchor_scores.py \\
        <会话目录> <节点名> [<节点名> ...] [--frames 1,10,26,29]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_master.core.template_match import best_match_score, load_template  # noqa: E402

PIPELINE_DIR = REPO / "maaracing_master/plugins/treasure/resources/pipeline"
IMG_DIR = REPO / "maaracing_master/plugins/treasure/resources/image"
SCALES = (0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3)


def load_nodes() -> dict:
    nodes: dict = {}
    for f in sorted(PIPELINE_DIR.glob("*.json")):
        for k, v in json.loads(f.read_text(encoding="utf-8")).items():
            if not k.startswith("$"):
                nodes[k] = v
    return nodes


def reco_params(nodes: dict, name: str) -> list[dict]:
    """取该节点识别用的模板参数；Or 展开为其引用的锚点节点。"""
    out: list[dict] = []
    stack = [nodes.get(name) or {}]
    while stack:
        rec = (stack.pop().get("recognition") or {})
        param = rec.get("param") or {}
        if isinstance(param, dict) and param.get("custom_recognition_param"):
            out.append(param["custom_recognition_param"])
        for sub in (param.get("any_of") or []) if isinstance(param, dict) else []:
            if isinstance(sub, str) and sub in nodes:
                stack.append(nodes[sub])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("names", nargs="+")
    ap.add_argument("--frames", default="1,10,26,29,50,100")
    args = ap.parse_args()

    raw = Path(args.session) / "raw"
    frames = [int(x) for x in args.frames.split(",")]
    nodes = load_nodes()
    print(f"会话 {Path(args.session).name}  模板目录 {IMG_DIR.name}（阈值缺省=0.75）")
    for name in args.names:
        print(f"\n[{name}]")
        for p in reco_params(nodes, name):
            rect = p.get("rect") or []
            if len(rect) != 4:
                continue
            for t in p.get("templates") or []:
                tpl = load_template(t, [IMG_DIR])
                if tpl is None:
                    print("   模板缺失", t)
                    continue
                rows = []
                for fn in frames:
                    img = cv2.imread(str(raw / f"{fn:04d}_raw.jpg"))
                    if img is None:
                        continue
                    h, w = img.shape[:2]
                    roi = img[int(rect[1] * h):int(rect[3] * h),
                              int(rect[0] * w):int(rect[2] * w)]
                    _, score = best_match_score(roi, tpl, SCALES)
                    rows.append((fn, round(float(score), 3)))
                print("   rect", [round(v, 3) for v in rect], "阈", p.get("threshold", "默认0.75"))
                print("   ", t, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
