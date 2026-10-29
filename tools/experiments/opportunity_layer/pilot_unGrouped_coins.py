# -*- coding: utf-8 -*-
"""未分组金币的离线定位试点：录制帧重跑感知，按帧号 join 回 trace 分类。

**只读**：只读 trace 与录制帧，不写生产数据、不改控制。
**覆盖**：只有同时有 `frames.jsonl` 与 trace 的会话可跑；当前仅
``debug/speedrush/20261028_225902_manual_p{1,2}``（2 个 stage）。

分类（每框）：
  边缘   框触画面边界；
  远      ``cy - y_h < 20px``（标定口径，约 50m 外，无横向读数）；
  走廊内  ``x_lane_of(cx,cy) - ego`` 落在同拍 ``sel_dbg.left/right`` 内；
  走廊外  其余（有横向读数但在走廊外）。
一拍多框取「走廊内 > 走廊外 > 远 > 边缘」；连续 gap 拍合成一段，按段统计。

**例外**：import 感知与标定（模型重放），与 speedrush_drive/probe_tracking_replay.py
同口径；分类只用标定公式与 trace 的走廊边缘，不读任何下游决策结果。

用法：
    .venv/Scripts/python.exe tools/experiments/opportunity_layer/pilot_unGrouped_coins.py
    ... --sessions 20261028_225902_manual_p1 --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import PERCEPTION_MODEL_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    StreetPerception)
from maaracing_master.plugins.speedrush.world_model import (  # noqa: E402
    IMAGE_H, load_calib, x_lane_of)

IMAGE_W = 1280
NEAR_DENOM_PX = 20.0
_ORDER = {"inside": 0, "outside": 1, "far": 2, "edge": 3}


def classify(coin, ego, left, right, cal) -> str:
    cx, cy, w, h, _conf = coin
    if cx - w / 2 <= 2 or cx + w / 2 >= IMAGE_W - 2 or \
            cy + h / 2 >= IMAGE_H - 2 or cy - h / 2 <= 2:
        return "edge"
    if cy - cal.y_h < NEAR_DENOM_PX:
        return "far"
    x = x_lane_of(int(cx), int(cy), cal) - ego
    if left is not None and right is not None and left <= x <= right:
        return "inside"
    return "outside"


def _token(session: str) -> str:
    return "_".join(session.split("_")[:2])


def _scan(d: Path, perc: StreetPerception) -> dict[int, list[list]]:
    idx = [json.loads(x) for x in
           (d / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x]
    out: dict[int, list[list]] = {}
    for r in idx:
        im = cv2.imread(str(d / "frames" / r["file"]))
        if im is None:
            continue
        res = perc.detect(cv2.cvtColor(im, cv2.COLOR_BGR2RGB),
                          r["frame_id"], r["ts_ns"])
        out[r["frame_id"]] = [[int(c.cx), int(c.cy), int(c.w), int(c.h),
                               round(c.conf, 3)] for c in res.coins]
    return out


def run(sessions: list[str], root: Path) -> None:
    cal = load_calib()
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    for sess in sessions:
        d = root / "debug" / "speedrush" / sess
        if not (d / "frames.jsonl").is_file():
            print(f"[skip] {sess}: 没有 frames.jsonl")
            continue
        trace = (root / "logs" / _token(sess) / "trace" / "speedrush.jsonl")
        if not trace.is_file():
            print(f"[skip] {sess}: 没有 trace")
            continue
        scan = _scan(d, perc)
        rows = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
        rows = [r for r in rows if r.get("type") is None and r.get("sel_dbg")]
        # 同一 trace 覆盖同局多个 stage：只保留本会话有帧的拍
        rows = [r for r in rows if r["fid"] in scan]
        runs: list[dict] = []
        cur = None
        gap_n = gap_t = found = 0
        exact = tested = 0
        cls_t: Counter = Counter()
        grp_n = grp_t = grp_found = 0
        grp_cls: Counter = Counter()
        for r in rows:
            dbg = r["sel_dbg"]
            dt = float(r.get("dt") or 0.0)
            classes = [classify(c, dbg["ego"], dbg.get("left"),
                                dbg.get("right"), cal)
                       for c in scan.get(r["fid"], [])]
            best = min(classes, key=lambda k: _ORDER[k]) if classes else None
            tested += 1
            if len(scan.get(r["fid"], [])) == (r.get("raw_coins") or 0):
                exact += 1
            gap = (r.get("raw_coins") or 0) > 0 and not dbg.get("coin")
            if gap:
                gap_n += 1
                gap_t += dt
                if best:
                    found += 1
                    cls_t[best] += dt
                if cur is None:
                    cur = {"t": 0.0, "cls": Counter(), "fid": r["fid"]}
                cur["t"] += dt
                if best:
                    cur["cls"][best] += dt
                continue
            if cur:
                runs.append(cur)
                cur = None
            if dbg.get("coin"):
                grp_n += 1
                grp_t += dt
                if best:
                    grp_found += 1
                    grp_cls[best] += dt
        if cur:
            runs.append(cur)
        print(f"\n== {sess}（{len(rows)} 拍，join 帧 {len(scan)}）==")
        print(f"逐拍检出计数一致 {exact}/{tested}"
              f"（{100.0 * exact / max(1, tested):.0f}%）")
        print(f"gap 拍 {gap_n} / {gap_t:.1f}s；重放找回 {found}"
              f"（{100.0 * found / max(1, gap_n):.0f}%）")
        print("  分类时间：" + "、".join(
            f"{k} {v:.1f}s" for k, v in cls_t.most_common()) or "  无")
        print(f"成组金币拍 {grp_n} / {grp_t:.1f}s；找回 {grp_found}"
              f"（{100.0 * grp_found / max(1, grp_n):.0f}%）；分类："
              + "、".join(f"{k} {v:.1f}s" for k, v in grp_cls.most_common()))
        print(f"连续段 {len(runs)}；主导类：" + str(Counter(
            c["cls"].most_common(1)[0][0] for c in runs if c["cls"])))
        for c in sorted(runs, key=lambda x: -x["t"])[:5]:
            print(f"  最长段 {c['t']:.2f}s fid={c['fid']} "
                  f"{dict(c['cls'])}")


def selftest() -> None:
    cal = load_calib()
    # 中心像素、地平线以下 → 有横向读数；走廊边缘 ±2 道时 inside
    far_box = [640, int(cal.y_h) + 10, 20, 10, 0.5]
    near_box = [640, 500, 20, 20, 0.5]
    edge_box = [2, 500, 20, 20, 0.5]
    assert classify(far_box, 0.0, -2.0, 2.0, cal) == "far"
    assert classify(near_box, 0.0, -2.0, 2.0, cal) == "inside"
    assert classify(near_box, 3.0, -2.0, 2.0, cal) == "outside"
    assert classify(edge_box, 0.0, -2.0, 2.0, cal) == "edge"
    print("selftest ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="MaaRacingMaster 运行根（默认 %%APPDATA%%/MaaRacingMaster）")
    ap.add_argument("--sessions", default="20261028_225902_manual_p1,"
                                         "20261028_225902_manual_p2")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    root = Path(args.root) if args.root else (
        Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster")
    sess = [s for s in args.sessions.split(",") if s]
    run(sess, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
