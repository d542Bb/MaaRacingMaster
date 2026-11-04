# -*- coding: utf-8 -*-
"""阶段二数据源支撑度探针：手动语料上逐帧统计参考线 v2 链的可养活性。

回答三个设计稿假设（2026-10-03，产线化前最后一道数据源验证）：
1. 覆盖率——v2 中点链逐帧齐备箱数分布、按箱弃权原因（像素不足/跨度超限）；
2. 宽度 W——非弃权箱路面跨度分布、±40% 污染占比、近场视锥截断占比
   （近箱道路超出画面 → 跨度被削，不进 W 统计）；
3. 时间连续性——相邻采样帧（~0.19s）同箱 |Δmid| 分布 + 链断帧段长度。

用法（仓库根，.venv）：
    python tools/experiments/speedrush_vision/probe_refline_support.py \
        --session <录制会话目录> [--every 4] [--render-every 40] [--out ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    DIAG_Y1, Y0, ZBIN, _dig_band, _fit_road_plane, infer_points, load_session,
    render_depth_debug)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402
from render_refline import _overlay  # noqa: E402

IMG_W = 1280


def v2_chain(pts: np.ndarray, fx: float, fy: float, ego, obj) -> dict:
    """逐箱 v2 路面统计；与 render_refline._overlay 同式，另带弃权原因与截断标记。

    截断判据（2026-10-03 看图战役定）：边界对跨度 ≥ 0.95×该 z 视锥全宽
    （视锥全宽 = 2·z·(W/2)/fx，由 X=(u−cx)·Z/fx 导出，尺度约掉）→ 边界贴
    画面左右缘，测的不是路缘；远箱不截断时量的是护栏走廊宽。"""
    Xa, Ya, Za = pts[Y0:DIAG_Y1, :, 0], pts[Y0:DIAG_Y1, :, 1], pts[Y0:DIAG_Y1, :, 2]
    digm = _dig_band(ego, pts.shape[1])
    if obj is not None:
        digm = digm | _dig_band(obj, pts.shape[1])
    coef = _fit_road_plane(Xa, Ya, Za, digm, fy=fy)
    out: dict = {"bins": [], "plane_ok": coef is not None}
    if coef is None:
        return out
    a_, b_, c_ = map(float, coef)
    sgn = -1.0 if b_ * 5.0 + c_ > 0 else 1.0
    road_y = sgn * (Ya - (a_ * Xa + b_ * Za + c_))
    for z0, z1 in ZBIN:
        m = ((Za >= z0) & (Za < z1) & (~digm) & np.isfinite(road_y)
             & (road_y < 0.15) & np.isfinite(Xa))
        rec = {"z": (z0 + z1) / 2, "n": int(m.sum())}
        if m.sum() < 200:
            rec["why"] = "low_px"
        else:
            xs = Xa[m]
            lo, hi = (float(v) for v in np.percentile(xs, [2, 98]))
            rr, cc = np.nonzero(m)
            if hi - lo > 25.0:
                rec["why"] = "wide_span"
            else:
                frustum = 2.0 * rec["z"] * (IMG_W / 2.0) / fx
                clip = (hi - lo) >= 0.95 * frustum
                rec.update(lo=lo, hi=hi, mid=(lo + hi) / 2,
                           span=hi - lo, clipped=bool(clip))
        out["bins"].append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--every", type=int, default=4)
    ap.add_argument("--render-every", type=int, default=40)
    ap.add_argument("--out", default="../../.workbuddy-ai/refline_support")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cal = load_calib()

    from maaracing_master.plugins.speedrush.module import (
        DEPTH_MODEL_FILE, PERCEPTION_MODEL_FILE, _yolo_object_mask)
    from maaracing_master.plugins.speedrush.perception import StreetPerception
    sess = load_session(DEPTH_MODEL_FILE)
    perc = StreetPerception(str(PERCEPTION_MODEL_FILE))
    from maaracing_master.plugins.speedrush.depth_geo import DepthRoadObserver
    ego = DepthRoadObserver._load_ego_mask()

    frames = sorted(Path(a.session, "frames").glob("*.jpg"))[::a.every]
    tag = Path(a.session).name
    print(f"{tag}: 采样 {len(frames)}/{len(sorted(Path(a.session, 'frames').glob('*.jpg')))} 帧（每 {a.every}）")

    recs: list[dict] = []
    for i, jpg in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(jpg)), cv2.COLOR_BGR2RGB)
        result = perc.detect(rgb, frame_id=0, ts_ns=0)
        obj = _yolo_object_mask(result)
        pts, fx, fy, _ev = infer_points(sess, rgb, with_evidence=True)
        ch = v2_chain(pts, fx, fy, ego, obj)
        ch["frame"] = jpg.stem
        recs.append(ch)
        if i % a.render_every == 0:
            try:
                from maaracing_master.plugins.speedrush.depth_geo import \
                    reading_from_points
                rd = reading_from_points(pts, fx, cal, ego_mask=ego,
                                         object_mask=obj, fy=fy)
                img = render_depth_debug(
                    rgb, {"pts": _ev["pts"], "valid": _ev["valid"],
                          "fx": _ev["fx"], "fy": _ev["fy"]},
                    rd, ego, obj, None)
                _overlay(img, pts, fy, rd, ego, obj)
                cv2.imwrite(str(out / f"{tag}_{jpg.stem}_sup.jpg"), img,
                            [cv2.IMWRITE_JPEG_QUALITY, 88])
            except Exception as exc:  # noqa: BLE001 —— 渲染失败不挡统计
                print(f"  render {jpg.stem}: FAIL {exc!r}")
        if (i + 1) % 50 == 0:
            print(f"  … {i + 1}/{len(frames)}")

    # ---- 聚合 ----
    n = len(recs)
    plane_fail = sum(1 for r in recs if not r["plane_ok"])
    bins_ok = [sum(1 for b in r["bins"] if "mid" in b) for r in recs]
    usable2 = sum(1 for k in bins_ok if k >= 2)
    usable3 = sum(1 for k in bins_ok if k >= 3)
    why = Counter(b.get("why") for r in recs for b in r["bins"] if "mid" not in b)

    spans, clip_n = [], 0
    per_bin_spans: dict[float, list[float]] = {b["z"]: [] for b in recs[0]["bins"]} if recs else {}
    per_bin_clip: dict[float, int] = {b["z"]: 0 for b in recs[0]["bins"]} if recs else {}
    mids_hist: dict[float, list[float]] = {b["z"]: [] for b in recs[0]["bins"]} if recs else {}
    for r in recs:
        for b in r["bins"]:
            if "mid" in b:
                if b["clipped"]:
                    clip_n += 1
                    per_bin_clip[b["z"]] += 1
                else:
                    spans.append(b["span"])
                    per_bin_spans[b["z"]].append(b["span"])
                mids_hist[b["z"]].append(b["mid"])
    far_spans = [s for z, v in per_bin_spans.items() if z >= 10 for s in v]
    W = float(np.median(far_spans)) if far_spans else float("nan")
    pollution = (float(np.mean([(s < 0.6 * W) | (s > 1.4 * W) for s in far_spans]))
                 if far_spans and np.isfinite(W) else float("nan"))

    # 时间连续性：相邻采样帧同箱 |Δmid|（两侧均未截断）
    dmid = []
    for r0, r1 in zip(recs, recs[1:]):
        b0 = {b["z"]: b for b in r0["bins"]}
        b1 = {b["z"]: b for b in r1["bins"]}
        for z, b in b1.items():
            if "mid" in b and "mid" in b0.get(z, {}) \
                    and not b0[z]["clipped"] and not b["clipped"]:
                dmid.append(abs(b["mid"] - b0[z]["mid"]))
    # 链断段：连续 bins_ok<2 的采样帧串长
    breaks, run = [], 0
    for k in bins_ok:
        if k < 2:
            run += 1
        elif run:
            breaks.append(run)
            run = 0
    if run:
        breaks.append(run)

    summary = {
        "session": tag, "sampled": n, "every": a.every,
        "plane_fail": plane_fail,
        "bins_ok_hist": {str(k): bins_ok.count(k) for k in sorted(set(bins_ok))},
        "chain_usable_ge2": usable2 / n, "chain_usable_ge3": usable3 / n,
        "abstain_why": dict(why),
        "spans_n": len(spans), "clipped_span_n": clip_n,
        "clipped_ratio": clip_n / max(1, clip_n + len(spans)),
        "per_bin_clip_n": {str(z): v for z, v in per_bin_clip.items()},
        "W_far_median_z_ge10": W, "pollution_pm40_far": pollution,
        "per_bin_span_median": {str(z): float(np.median(v))
                                for z, v in per_bin_spans.items() if v},
        "per_bin_span_iqr": {str(z): [round(float(np.percentile(v, 25)), 2),
                                      round(float(np.percentile(v, 75)), 2)]
                             for z, v in per_bin_spans.items() if v},
        "per_bin_span_n": {str(z): len(v) for z, v in per_bin_spans.items()},
        "mid_offset_ego_median": {str(z): float(np.median(np.abs(v)))
                                  for z, v in mids_hist.items() if v},
        "dmid_n": len(dmid),
        "dmid_p50_p90_p99": [round(float(np.percentile(dmid, p)), 3)
                             for p in (50, 90, 99)] if dmid else None,
        "chain_break_runs": Counter(breaks) or {},
    }
    (out / f"{tag}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"输出 → {out.resolve()}")


if __name__ == "__main__":
    main()
