"""离线回放全链路（设计稿 v2 §八 step 5）：录制帧流 → 感知 → Tracker → 金币组
→ BoundarySummary → DecisionEngine，逐帧决策输出落 jsonl。

**要回答的问题**（C 类）：
① 链路端到端能不能跑（真实帧流下的异常/降级路径暴露）；
② 决策层在真实局里的行为画像——每 tick 状态/理由分布、变道事件密度、
   保守态/故障触发率（应罕见，出现即是有信号）；
③ §七.3 一致率基线的原料——决策输出的横向意图 vs 人驾 pads lx，交给
   probe_agreement.py 对齐分析（本探针只负责把两侧时间戳同流的原料落盘）；
④ 边界摘要在真实素材上的有效率（straight_residual 阈值的定档数据，顺带）。

**与实机的差异（如实声明）**：实机 21Hz 主循环、帧龄由采集侧给；回放按
frames.jsonl 的真实 ts_ns 差算 dt_s（到场轮 ≈15.8Hz 行率），engine 用时间基
参数不吃帧率亏。感知用与实机同一 StreetPerception（插件自带模型 conf 0.35）。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_replay_chain.py [--only 20260921_17] [--limit 0]
输出（数据目录，不入库）：`<APPDATA>/MaaRacingMaster/data/speedrush/replay_chain.jsonl`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.boundary import detect_boundary  # noqa: E402
from maaracing_master.plugins.speedrush.coin_group import (  # noqa: E402
    CoinGroupAggregator)
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.decision import DecisionEngine  # noqa: E402
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.tracking import Tracker, to_jsonable  # noqa: E402

_DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = _DATA / "demos"
OUT = _DATA / "replay_chain.jsonl"
MODEL = ROOT / "maaracing_master" / "plugins" / "speedrush" / "resources" / "onnx" / "model.onnx"


def run_session(perc: StreetPerception, sess: Path, f_out, limit: int) -> dict:
    cfg = load_decision()
    tracker = Tracker()
    agg = CoinGroupAggregator()
    engine = DecisionEngine(cfg)
    index = [json.loads(x) for x in
             (sess / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if limit:
        index = index[:limit]
    meta = json.loads((sess / "meta.json").read_text(encoding="utf-8"))
    stage = int(meta.get("phase", 1))
    st = {"sess": sess.name, "frames": 0, "states": {}, "reasons": {},
          "change_events": [], "boundary_valid": 0, "boundary_rows": 0,
          "residuals": [], "dt_sum": 0.0, "err": None}
    prev_ts = None
    t0 = time.perf_counter()
    try:
        for item in index:
            img = cv2.imread(str(sess / "frames" / item["file"]))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            per = perc.detect(rgb, frame_id=item["frame_id"], ts_ns=item["ts_ns"])
            obs = tracker.update(per, frame_age_ms=item.get("age_ms", 0.0), stage=stage)
            obs = agg.update(obs)
            bsum = detect_boundary(rgb)
            obs = replace(obs, boundary=bsum)
            dt_s = 0.05
            if prev_ts is not None and item["ts_ns"] > prev_ts:
                dt_s = (item["ts_ns"] - prev_ts) / 1e9
            prev_ts = item["ts_ns"]
            out = engine.update(obs, dt_s)
            st["frames"] += 1
            st["dt_sum"] += dt_s
            st["states"][out.state.value] = st["states"].get(out.state.value, 0) + 1
            code = out.reason.split(":")[0]
            st["reasons"][code] = st["reasons"].get(code, 0) + 1
            if out.state.value == "CHANGE" and not st.get("_in_change"):
                st["_in_change"] = True
                st["change_events"].append({"fid": out.emitted_fid, "ts_ns": item["ts_ns"],
                                            "dir": 1 if (out.x_target or 0) > 0 else -1})
            if out.state.value != "CHANGE":
                st["_in_change"] = False
            st["boundary_rows"] += 1
            st["boundary_valid"] += int(bsum.validity)
            if bsum.validity:
                st["residuals"].append(round(bsum.straight_residual, 3))
            f_out.write(json.dumps({
                "sess": sess.name, "stage": stage,
                "fid": item["frame_id"], "ts_ns": item["ts_ns"], "dt_s": round(dt_s, 4),
                "decision": to_jsonable(out),
                "groups": [{k: v for k, v in asdict(g).items()} for g in obs.coin_groups],
                "near": len(obs.targets), "far": len(obs.far_targets),
                "presence": obs.health.target_presence, "fresh": obs.health.frame_fresh,
                "boundary": {"valid": bsum.validity, "width": round(bsum.road_width, 1)
                             if bsum.road_width == bsum.road_width else None},
            }, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 —— 单场炸了要留现场继续跑其余场
        st["err"] = repr(exc)
    el = time.perf_counter() - t0
    st["hz"] = st["frames"] / el if el else 0.0
    return st


def main() -> None:
    ap = argparse.ArgumentParser(description="离线回放全链路")
    ap.add_argument("--only", default="", help="会话名子串过滤")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    sessions = sorted(d for d in DEMOS.iterdir() if (d / "frames.jsonl").is_file())
    if a.only:
        sessions = [s for s in sessions if a.only in s.name]
    perc = StreetPerception(str(MODEL))
    print(f"场次 {len(sessions)} → {OUT}", flush=True)
    with open(OUT, "w", encoding="utf-8") as f:
        stats = []
        for si, sess in enumerate(sessions):
            st = run_session(perc, sess, f, a.limit)
            stats.append(st)
            print(f"[{si + 1}/{len(sessions)}] {sess.name}: {st['frames']} 帧 "
                  f"{st['hz']:.1f}fps states={st['states']} err={st['err']}", flush=True)
    print("\n== 汇总 ==")
    tot = {"frames": 0, "change": 0, "bv": 0, "br": 0}
    states: dict[str, int] = {}
    residuals: list[float] = []
    for st in stats:
        tot["frames"] += st["frames"]
        tot["change"] += len(st["change_events"])
        tot["bv"] += st["boundary_valid"]
        tot["br"] += st["boundary_rows"]
        for k, v in st["states"].items():
            states[k] = states.get(k, 0) + v
        residuals += st["residuals"]
    print(f"帧 {tot['frames']}，变道事件 {tot['change']}（{tot['change']/max(1,tot['frames'])*100:.1%}/帧）")
    print("状态分布:", {k: f"{v/max(1,tot['frames']):.1%}" for k, v in sorted(states.items())})
    print(f"边界有效率 {tot['bv']}/{tot['br']} = {tot['bv']/max(1,tot['br']):.1%}")
    if residuals:
        srt = sorted(residuals)
        print(f"straight_residual: P50={srt[len(srt)//2]:.3f} "
              f"P90={srt[int(0.9*len(srt))]:.3f} P99={srt[int(0.99*len(srt))]:.3f} "
              f"max={srt[-1]:.3f}")


if __name__ == "__main__":
    main()
