"""跟踪层离线回放：真实帧流灌进 Tracker，出 §7.5（重识别口径）与关联门限的起值校准数据。

**要回答的问题**（C 类）：`tracking.py` 的最小近邻关联在实机素材上的行为画像——
①轨迹寿命分布（宽限窗 grace_ticks 起值 15 是否够）；②遮挡保持的失联档分布与
恢复率（gap=1/2/3… 帧后重新配上的比例——§7.5"遮挡场景 id 保持率"的实测代理）；
③far→near 晋升占比（域外金币进域保住身份的比例）；④每 tick 近场/远场负载分布
（决策层与规划层的预算画像）。

**方法**：两阶段。
- `scan`：逐帧全图推理（`StreetPerception` 与实机同一条路：插件自带模型、conf 0.35、
  全帧无 ROI），三类框几何全量落盘——此前的 streetcar 扫描只留 car 几何、
  coin/bonus 只有计数，喂不了跟踪回放。帧标识取 recorder 的 frames.jsonl
  （frame_id 严格递增，满足 Tracker 的单调契约）。
- `replay`：按会话把扫描行重建为 `PerceptionResult` 喂 `Tracker.update()`，
  只从公开输出观测统计（不触碰内部轨迹表）。frame_age_ms 用录制时记录的 age_ms。

用法（仓库根；扫描耗时与帧数成正比）：
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_tracking_replay.py scan [--only 20260921] [--limit 0]
    .venv/Scripts/python.exe tools/experiments/speedrush_drive/probe_tracking_replay.py replay [--only 20260921]

输出（数据目录，不入库）：`<APPDATA>/MaaRacingMaster/data/speedrush/tracking_scan.jsonl`
与终端汇总表。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.perception import (  # noqa: E402
    Detection, PerceptionResult, StreetPerception)
from maaracing_master.plugins.speedrush.tracking import Tracker  # noqa: E402

_DATA = Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" / "speedrush"
DEMOS = _DATA / "demos"
OUT = _DATA / "tracking_scan.jsonl"


def cmd_scan(only: str, limit: int) -> None:
    sessions = sorted(d for d in DEMOS.iterdir() if (d / "frames.jsonl").is_file())
    if only:
        sessions = [s for s in sessions if only in s.name]
    print(f"场次: {len(sessions)} → {OUT}")
    perc = StreetPerception(str(ROOT / "maaracing_master" / "plugins" / "speedrush"
                                  / "resources" / "onnx" / "model.onnx"))
    t_all = time.time()
    with open(OUT, "w", encoding="utf-8") as f:
        for si, sess in enumerate(sessions):
            index = [json.loads(x)
                     for x in (sess / "frames.jsonl")
                     .read_text(encoding="utf-8").splitlines() if x.strip()]
            if limit:
                index = index[:limit]
            t0 = time.time()
            for item in index:
                img = cv2.imread(str(sess / "frames" / item["file"]))
                if img is None:
                    continue
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                res = perc.detect(rgb, frame_id=item["frame_id"], ts_ns=item["ts_ns"])
                rec = {"sess": sess.name, "frame_id": item["frame_id"],
                       "ts_ns": item["ts_ns"], "age_ms": item.get("age_ms", 0.0),
                       "boxes": {"coin": [list(b) for b in res.coins],
                                 "car": [list(b) for b in res.cars],
                                 "bonus": [list(b) for b in res.bonuses]}}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"[{si + 1}/{len(sessions)}] {sess.name}: {len(index)} 帧 "
                  f"{time.time() - t0:.1f}s", flush=True)
    print(f"全部完成 {time.time() - t_all:.0f}s")


def _pct(xs: list[int], q: float) -> int:
    if not xs:
        return 0
    srt = sorted(xs)
    return srt[min(len(srt) - 1, int(round(q * (len(srt) - 1))))]


class _SessStats:
    """单会话回放统计：只消费公开输出字段（id / last_seen_fid / first_seen_fid）。

    失联段的口径：一段 = 从最后配到帧起的连续保持帧数；段末或"重新配上"（恢复）
    或"宽限退役"（未恢复）。直方图键 = 段长（帧数），grace_ticks 该设多大直接看
    这个分布的右尾。
    """

    def __init__(self) -> None:
        self.frames = 0
        self.lifetimes: list[int] = []
        self.hold_hist: dict[int, int] = {}   # 失联段长 → 段数（含未恢复段）
        self.recovered = 0
        self.segments = 0
        self.promotions = 0                   # far→near 同 id 晋升
        self.near_load: list[int] = []
        self.far_load: list[int] = []
        self.ids_seen = 0
        self._info: dict[int, dict] = {}

    def feed(self, obs) -> None:
        self.frames += 1
        fid = obs.frame_id
        now: dict[int, bool] = {}             # id -> 本帧是否远场
        for t in obs.targets:
            now[t.id] = False
            self._touch(t.id, t.first_seen_fid, t.last_seen_fid, fid)
        for t in obs.far_targets:
            now[t.id] = True
            self._touch(t.id, t.first_seen_fid, t.last_seen_fid, fid)
        for tid, is_far in now.items():
            info = self._info[tid]
            if info["was_far"] is True and is_far is False:
                self.promotions += 1
            info["was_far"] = is_far
        self.near_load.append(len(obs.targets))
        self.far_load.append(len(obs.far_targets))

    def _touch(self, tid: int, first: int, last: int, fid: int) -> None:
        info = self._info.get(tid)
        if info is None:
            self._info[tid] = {"first": first, "last": last,
                               "was_far": None, "hold": 0}
            self.ids_seen += 1
            return
        if last == fid:                        # 本帧真实配到
            if info["hold"] > 0:               # 一段失联就此结束：恢复
                self.segments += 1
                self.recovered += 1
                self.hold_hist[info["hold"]] = self.hold_hist.get(info["hold"], 0) + 1
                info["hold"] = 0
            info["last"] = fid
        else:                                  # 保持输出（遮挡/漏检宽限）
            info["hold"] += 1
        # last_seen 单调，不会倒退

    def finish(self) -> None:
        for info in self._info.values():
            self.lifetimes.append(info["last"] - info["first"] + 1)
            if info["hold"] > 0:               # 未恢复段（宽限退役或素材截尾）
                self.segments += 1
                self.hold_hist[info["hold"]] = self.hold_hist.get(info["hold"], 0) + 1

    def line(self, sess: str) -> str:
        gaps = " ".join(f"{k}:{v}" for k, v in sorted(self.hold_hist.items())) or "—"
        return (f"{sess:>20s} {self.frames:>5d} {self.ids_seen:>5d} "
                f"{_pct(self.lifetimes, 0.5):>4d}/{_pct(self.lifetimes, 0.95):>4d} "
                f"{self.recovered:>4d}/{self.segments:<4d} "
                f"{gaps:>18s} {self.promotions:>4d} "
                f"{_pct(self.near_load, 0.5):>3d}/{_pct(self.near_load, 0.95):<3d} "
                f"{_pct(self.far_load, 0.5):>3d}/{_pct(self.far_load, 0.95):<3d}")


def cmd_replay(only: str) -> None:
    rows_by_sess: dict[str, list[dict]] = defaultdict(list)
    with open(OUT, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if not only or only in r["sess"]:
                rows_by_sess[r["sess"]].append(r)

    head = (f"{'session':>20s} {'帧':>5s} {'建轨':>5s} {'寿命P50/P95':>11s} "
            f"{'恢复/段':>9s} {'段长直方图':>18s} {'晋升':>4s} {'近场':>6s} {'远场':>6s}")
    print(head)
    total = _SessStats()
    for sess, rows in sorted(rows_by_sess.items()):
        tracker = Tracker()
        st = _SessStats()
        prev_fid = 0
        for row in rows:
            if row["frame_id"] <= prev_fid:      # 帧号必须严格递增（契约），违例跳帧
                continue
            prev_fid = row["frame_id"]
            per = PerceptionResult(
                frame_id=row["frame_id"], ts_ns=row["ts_ns"],
                cars=[Detection(*b) for b in row["boxes"]["car"]],
                coins=[Detection(*b) for b in row["boxes"]["coin"]],
                bonuses=[Detection(*b) for b in row["boxes"]["bonus"]])
            st.feed(tracker.update(per, frame_age_ms=row["age_ms"], stage=1))
        st.finish()
        print(st.line(sess))
        total.frames += st.frames
        total.ids_seen += st.ids_seen
        total.lifetimes += st.lifetimes
        total.hold_hist = {k: total.hold_hist.get(k, 0) + v
                           for k, v in st.hold_hist.items()}
        total.recovered += st.recovered
        total.segments += st.segments
        total.promotions += st.promotions
    print(total.line("== 汇总 =="))
    print("\n注：恢复/段 = 失联后重新配上同 id 的段数 / 全部失联段数；段长直方图键为"
          "连续失联帧数（grace_ticks 定档看右尾）。晋升 = far→near 同 id。")


def main() -> None:
    ap = argparse.ArgumentParser(description="跟踪层离线回放探针")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="逐帧推理落盘（三类框全几何）")
    p_scan.add_argument("--only", default="", help="会话名子串过滤")
    p_scan.add_argument("--limit", type=int, default=0, help="每会话帧数上限")
    p_rep = sub.add_parser("replay", help="回放扫描结果进 Tracker 并统计")
    p_rep.add_argument("--only", default="")
    a = ap.parse_args()
    if a.cmd == "scan":
        cmd_scan(a.only, a.limit)
    else:
        cmd_replay(a.only)


if __name__ == "__main__":
    main()
