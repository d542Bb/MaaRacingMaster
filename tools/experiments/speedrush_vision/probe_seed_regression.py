# -*- coding: utf-8 -*-
"""种子三候选竞争新旧对照回归（临时，2026-10-14）：坏帧语料全量 MoGe 复放。

旧实现从 git HEAD 抽出为独立模块（seed 单候选），新实现为工作区产线码。
对 legacy_control_traces 全部坏帧 jpg + 本次三帧（带 YOLO 掩码）逐帧推理，
对比 sides/rejects/coef/roll。判定变化逐帧打印，最后汇总。

用法（仓库根）：.venv/Scripts/python tools/experiments/speedrush_vision/probe_seed_regression.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DEPTH_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo as dg_new  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import Calib  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
LEGACY = Path(os.environ["APPDATA"]) / "MaaRacingMaster/debug/speedrush"
CUR = LEGACY / "20261014_112029_p2" / "depth_debug"
CUR_STEMS = {"d00002": "2783", "d00014": "3034", "d00021": "3178"}

gate = json.load(open(ROOT / "maaracing_master/plugins/speedrush/resources"
                           "/calibration/gate0.json", encoding="utf-8"))
CAL = Calib(**{k: gate[k] for k in Calib.__dataclass_fields__})


def load_old_module():
    src = subprocess.run(
        ["git", "-C", str(ROOT), "show",
         "HEAD:maaracing_master/plugins/speedrush/depth_geo.py"],
        capture_output=True, check=True).stdout
    p = Path(tempfile.gettempdir()) / "depth_geo_head.py"
    p.write_bytes(src)
    spec = importlib.util.spec_from_file_location("depth_geo_head", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["depth_geo_head"] = mod
    spec.loader.exec_module(mod)
    return mod


def collect_frames():
    """(标签, rgb, mask|None) 列表：坏帧语料 + 本次三帧。"""
    out = []
    for d in sorted((LEGACY / "legacy_control_traces").glob("badframes_*")):
        idx = d / "index.jsonl"
        stems = set()
        if idx.exists():
            for line in idx.open(encoding="utf-8"):
                try:
                    stems.add(str(json.loads(line).get("fid")))
                except Exception:
                    pass
        for f in sorted(d.glob("fid_*.jpg")):
            img = cv2.imread(str(f))
            if img is None:
                continue
            out.append((f"{d.name}/{f.stem}",
                        cv2.cvtColor(img, cv2.COLOR_BGR2RGB), None))
    for stem, fid in CUR_STEMS.items():
        f = CUR / f"{stem}_frame.jpg"
        img = cv2.imread(str(f))
        if img is None:
            continue
        mp = CUR / f"{stem}_mask.npy"
        mask = None
        if mp.exists():
            mask = np.unpackbits(np.load(mp))[: 1280 * 720] \
                .reshape(720, 1280).astype(bool)
        out.append((f"CUR/{stem}(fid{fid})",
                    cv2.cvtColor(img, cv2.COLOR_BGR2RGB), mask))
    return out


def snap(r):
    if r.coef is None:
        return ("None", None, tuple(r.rejects), r.sides)
    roll = float(np.degrees(np.arctan(r.coef[0])))
    return ("ok", roll, tuple(r.rejects), r.sides)


def main() -> None:
    old = load_old_module()
    sess = dg_new.load_session(DEPTH_MODEL_FILE)
    frames = collect_frames()
    print(f"帧数={len(frames)}")
    changed = 0
    for name, rgb, mask in frames:
        pts, fx, fy = dg_new.infer_points(sess, rgb)
        r_old = old.reading_from_points(pts, fx, CAL, mask, fy=fy)
        r_new = dg_new.reading_from_points(pts, fx, CAL, mask, fy=fy)
        so, sn = snap(r_old), snap(r_new)
        if so != sn:
            changed += 1
            print(f"[变化] {name}: 旧 {so} → 新 {sn}")
    print(f"== 判定变化 {changed}/{len(frames)}")


if __name__ == "__main__":
    main()
