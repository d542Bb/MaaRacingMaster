# -*- coding: utf-8 -*-
"""P2a-Q2（识别样本层）：raw 真帧上跑 v4 阶段信号，帧级合理性对拍。

口径：stage 变化后首帧上，**新 stage 的信号组至少一个模板命中**（dwell 的 Or
语义 = 任一信号确认阶段）。未命中帧 = v4 彩色匹配 vs v3 灰度检测的实现差异
档案（P2b gray→rgb 调参输入），不计为迁移失败。

顺带验证（Q1/Q2 间发现的兼容位）：模板名自带 .png 时 load_template 会二次拼
扩展名 → 本脚本剥扩展名绕行；生产桥修复归 Q3。

用法：.venv\\Scripts\\python.exe tools\\experiments\\v4-p2a-wiring\\recognition_sample.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_assistant.core.paths import debug_dir  # noqa: E402
from maaracing_assistant.core import template_match as tm  # noqa: E402

TREASURE_JSON = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "nav" / "treasure.json"
POLICY_JSON = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"
IMAGE_ROOT = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources"

MAX_SAMPLES_PER_SESSION = 6


def strip_ext(name: str) -> str:
    return name[:-4] if name.lower().endswith((".png", ".jpg")) else name


def stage_signal_nodes() -> dict[str, list[dict]]:
    """stage → [锚点识别参数]（仅 template 型信号；point 型无模板不进 dwell 判定）。"""
    graph = json.loads(TREASURE_JSON.read_text(encoding="utf-8"))
    graph.update(json.loads((REPO / "maaracing_assistant" / "core" / "resources" / "nav" / "global.json")
                            .read_text(encoding="utf-8")))
    pol = json.loads(POLICY_JSON.read_text(encoding="utf-8"))
    out: dict[str, list[dict]] = {}
    for stage, sig in pol["perception"]["stage_signals"].items():
        params = []
        for a in sig["anchors"]:
            n = graph.get(f"treasure.{a}") or graph.get(f"global.{a}")
            if not n:
                continue
            p = n.get("custom_recognition_param") or {}
            if p.get("mode") == "template" and p.get("templates"):
                params.append(p)
        out[stage] = params
    return out


def sample_frames(session: Path) -> list[tuple[int, str]]:
    """stage 变化后首个可用的 raw 帧号（空洞 +1..+3 补偿）→ [(frame, new_stage)]。"""
    rows = [json.loads(l) for l in (session / "trace.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    prev, want = None, []
    for r in rows:
        st = r.get("stage")
        if st and prev and st != prev:
            want.append((r["frame"], st))
        if st:
            prev = st
    raw = session / "raw"
    if not raw.is_dir():
        return []
    out = []
    for f, st in want:
        for d in range(4):
            p = raw / f"{f + d:04d}_raw.jpg"
            if p.exists():
                out.append((f + d, st, p))
                break
    return [(f, st, p) for f, st, p in out[:MAX_SAMPLES_PER_SESSION]]


def main() -> int:
    signals = stage_signal_nodes()
    image_dirs = sorted({p.parent for p in IMAGE_ROOT.rglob("*.png")})
    root = debug_dir() / "treasure"
    sessions = sorted([p for p in root.iterdir()
                       if (p / "trace.jsonl").is_file() and (p / "raw").is_dir()], reverse=True)
    hit = miss = 0
    miss_rows: list[str] = []
    sampled = 0
    for s in sessions:
        for f, st, path in sample_frames(s):
            sampled += 1
            bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
            frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            H, W = frame.shape[:2]
            best = 0.0
            for p in signals.get(st, []):
                x1, y1, x2, y2 = p["rect"]
                roi = (int(x1 * W), int(y1 * H), int((x2 - x1) * W), int((y2 - y1) * H))
                names = [strip_ext(t) for t in p["templates"]]
                box, score, _ = tm.find_any(frame, names, image_dirs,
                                            threshold=float(p.get("threshold", 0.75)),
                                            roi=roi)
                best = max(best, score if box else 0.0)
            if best > 0:
                hit += 1
            else:
                miss += 1
                miss_rows.append(f"{s.name} frame={f} stage={st} 最高分=0（组内全部未达阈）")
    print(f"[样本] 会话 {len(sessions)} 个，帧样本 {sampled}：命中 {hit} / 未命中 {miss}")
    for m in miss_rows[:15]:
        print(f"    未命中: {m}")
    rate = hit / sampled if sampled else 0
    print(f"[结论] dwell 信号帧级命中率 = {rate:.1%}（未命中归 P2b 彩色/灰度差异档案）")
    return 0 if rate >= 0.7 else 1


if __name__ == "__main__":
    sys.exit(main())
