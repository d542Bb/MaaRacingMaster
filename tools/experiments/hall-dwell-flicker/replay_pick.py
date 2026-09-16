# -*- coding: utf-8 -*-
"""回放回路：逐帧复算「常驻图这一轮会选中哪个 dwell」，用于大厅/待机抖动定位。

不重造识别算法——直接调用生产同款 `nav_graph.TemplateRecognizer.analyze()`，
参数取自盘上真源（`__boot.dwell.next` 的顺序 = 框架求值 next 列表的顺序，
Or 型 dwell 先解析 any_of 引用的锚点节点），帧取自会话 `raw/` 的原图（无 HUD 覆盖）。

用法：
    .venv\\Scripts\\python.exe tools/experiments/hall-dwell-flicker/replay_pick.py <会话目录>

退出码：选中序列里出现「同一对页面往复 ≥2 次」→ 1（红），否则 0（绿）。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.define import RectType  # noqa: E402

from maaracing_master.core.nav_graph import TemplateRecognizer  # noqa: E402

PIPELINE_DIR = REPO / "maaracing_master/plugins/treasure/resources/pipeline"
BOOT = "treasure.__boot.dwell"


class _GraphStub:
    """TemplateRecognizer 需要的最小宿主：只用到 image_dirs（帧由本回路直接喂）。"""

    def __init__(self, image_dirs: list[Path]) -> None:
        self.image_dirs = list(image_dirs)
        self.cursor_pos = None


def load_nodes() -> dict:
    nodes: dict = {}
    for f in sorted(PIPELINE_DIR.glob("*.json")):
        for k, v in json.loads(f.read_text(encoding="utf-8")).items():
            if not k.startswith("$"):
                nodes[k] = v
    return nodes


def reco_param(nodes: dict, name: str) -> list[dict]:
    """取该节点识别用的 MaaRM_Template 参数表。

    Or/And 子项有两种合法形态（真源里两种都在用）：字符串=引用另一个节点名，
    对象=内联一份 recognition 定义。只处理字符串会把内联形态静默丢掉，
    对局内画面就会整片报「无命中」——本工具初版即踩此坑（2026-09-13 实测暴露）。
    """
    return _expand(nodes, (nodes.get(name) or {}).get("recognition"))


def _expand(nodes: dict, rec) -> list[dict]:
    if not isinstance(rec, dict):
        return []
    rtype = rec.get("type")
    param = rec.get("param") or {}
    if rtype == "Custom":
        crp = param.get("custom_recognition_param") or {}
        return [crp] if crp else []
    if rtype in ("Or", "And"):
        out: list[dict] = []
        for sub in (param.get("any_of") or param.get("all_of") or []):
            if isinstance(sub, str):
                out += _expand(nodes, (nodes.get(sub) or {}).get("recognition"))
            elif isinstance(sub, dict):
                out += _expand(nodes, sub.get("recognition") or sub)
        return out
    return []


class _Arg:
    def __init__(self, node_name: str, param: dict, image: np.ndarray) -> None:
        self.task_detail = None
        self.node_name = node_name
        self.custom_recognition_name = "MaaRM_Template"
        self.custom_recognition_param = json.dumps(param, ensure_ascii=False)
        self.image = image
        self.roi: RectType = (0, 0, 0, 0)


def pick(recognizer: TemplateRecognizer, nodes: dict, order: list[str],
         bgr: np.ndarray) -> tuple[str | None, dict]:
    """按框架求值 next 的顺序返回第一个命中的 dwell + 逐 dwell 明细。"""
    detail = {}
    for dwell in order:
        hit = None
        for p in reco_param(nodes, dwell):
            got = recognizer.analyze(None, _Arg(dwell, p, bgr))
            box = got[0] if isinstance(got, tuple) else getattr(got, "box", got)
            if box is not None:
                hit = box
                break
        detail[dwell] = bool(hit)
        if hit is not None:
            return dwell, detail
    return None, detail


def flickers(seq: list[str | None]) -> list[str]:
    """同一对页面往复 ≥2 次即判抖动。"""
    pairs = Counter()
    for a, b in zip(seq, seq[1:]):
        if a and b and a != b:
            pairs[frozenset((a, b))] += 1
    return ["/".join(sorted(p)) for p, n in pairs.items() if n >= 2]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    session = Path(sys.argv[1])
    frames = sorted((session / "raw").glob("*_raw.jpg"))
    if not frames:
        print(f"[FAIL] {session}/raw 下无 *_raw.jpg")
        return 2
    nodes = load_nodes()
    order = nodes[BOOT].get("next") or []
    recognizer = TemplateRecognizer(
        _GraphStub([REPO / "maaracing_master/plugins/treasure/resources/image"]),
        cursor_pos_provider=lambda: None)
    seq, rows = [], []
    for f in frames:
        bgr = cv2.imread(str(f))
        if bgr is None:
            continue
        idx = int(re.match(r"(\d+)", f.name).group(1))
        winner, detail = pick(recognizer, nodes, order, bgr)
        seq.append(winner)
        rows.append((idx, winner, [k for k, v in detail.items() if v]))
    print(f"帧数={len(rows)}  boot.next 顺序={len(order)} 项")
    print("\n帧号  选中 dwell（+ 同帧其他也命中的 dwell）")
    for idx, winner, hits in rows:
        extra = [h for h in hits if h != winner]
        print("  %4d  %-40s %s" % (idx, (winner or "（无命中）"),
                                   "同时命中: " + ", ".join(x.replace("treasure.", "").replace(".dwell", "")
                                                            for x in extra) if extra else ""))
    print("\n分布:", dict(Counter(seq)))
    fl = flickers(seq)
    if fl:
        print(f"\n[RED] 抖动往复：{fl}")
        return 1
    print("\n[GREEN] 无往复抖动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
