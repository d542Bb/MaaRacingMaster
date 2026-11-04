"""基于目标存在性的未知占用影子与七例回归（2026-10-22）。

把「距离有效性」与「占用」彻底分开：

- 占用身份 = 轨道 id，不是车道级布尔——释放一辆车不会清掉另一辆。
- 占用成立 = 有 2D 框 + 近威胁（可信距离 <8m / 框高 ≥80px / 框底接地行
  在 0.25s 内下移 ≥20px）+ 横向在自车道（≤1.7m；横向未知时只在约 8m 内
  保守占用）。框高比不作接近证据：框尺寸突变/截断修复会让高比跳变。
- 距离是否可信只决定发布不发布 ``m``，不决定占用；质量门拒绝、深度过期、
  框冻结都不单独等于车道空闲。
- 释放只认独立证据：掠过（pass 事件 / t_ov≤0）、横向连续 2 次新鲜观测
  >2.0m（建占仍 ≤1.7m，滞回防边界抖动）。z<0 不单列：它与距离锚同源；
  24 局里 8 次 z<0 释放全部发生在框底 476~718px（车仍在 3~8m 近旁），
  框没有后退，不是确认离道。
  没有证据时，仅在最后一次**新鲜** 2D 命中后 >500ms 有界释放，并显式
  标 uncertain（最后观测仍近/在道 → lost_near；否则 lost_clear）。
  冻结框（seen>0）不刷新新鲜度。
- 未知占用 >1.5s 只记 cap，不自动判空。

回归集（2026-10-22 审计得到的 7 例）：
- 140508 fid 13043 id77：m=2.57 被质量门拒绝、横向 1.18m、粗层无占用
  ——影子必须仍占用该车道。
- 6 次旧口径的丢失释放：2 例最后观测仍近且横向在自车道，必须标
  lost_near + uncertain；2 例在最后新鲜框之后等到了同一 id 的 pass 事件，
  应按 passed 确认释放；另 2 例横向读数缺失、平面距离约 11~20m，车道归属
  不成立，不应建立近场占用。

另附合成多车用例：A 离道只释放 A；A 超时不清 B；B 进入不被 A 的旧状态
挡住；冻结框不刷新新鲜度；未知占用有界但不自动判空。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_crash/probe_presence_occupancy.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

H_NOM = 1.5
NEAR_M = 8.0
LO, HI = 0.5, 2.0
NEAR_H = 80.0
NEAR_Y2 = 500.0
APPROACH_Y2 = 20.0
WIN_S = 0.25
LANE_IN = 1.2
LANE_OUT = 1.7
LANE_RELEASE = 2.0
DEPART_TICKS = 2
LOST_MS = 500.0
CAP_MS = 1500.0

SESSIONS = [
    "20261022_132510_p1", "20261022_132554_p2",
    "20261022_140023_p1", "20261022_140108_p2", "20261022_140223_p1",
    "20261022_140307_p2", "20261022_140424_p1", "20261022_140508_p2",
    "20261022_140625_p1", "20261022_140709_p2", "20261022_140822_p1",
    "20261022_140907_p2",
]

# 旧审计（probe_occupancy_state.py）标出的 6 次 uncertain release。
# 新影子会消费“轨道已离开检测但仍有 pass 事件”的掠过证据：其中 2 例在
# 最后新鲜框之后等到了同一 id 的 pass 事件，属于独立离道证据，另行验收。
REFERENCE_LOST_UNCERTAIN = [
    ("20261022_140223_p1", 15),
    ("20261022_140709_p2", 4),
]
REFERENCE_PASS_RESOLVED = [
    ("20261022_140307_p2", 17),
    ("20261022_140822_p1", 47),
]
# 另 2 例旧 uncertain release 的横向读数缺失、平面距离约 11~20m：车道
# 归属不成立，收紧后不再建立近场占用（见实验 20 的车道归属段）。
REFERENCE_LANE_UNKNOWN_FAR = [
    ("20261022_140108_p2", 77),
    ("20261022_140822_p1", 25),
]


def _root() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("需要 APPDATA（Windows 运行数据目录）")
    return Path(base) / "MaaRacingMaster" / "debug" / "speedrush"


def _load(name: str) -> list[dict]:
    p = _root() / name / f"trace_{name}.jsonl"
    if not p.exists():
        raise SystemExit(f"找不到 {p}")
    return [json.loads(line) for line in p.open(encoding="utf-8")]


def _decision(focal, cam_h, m, h, cy, y_h) -> str:
    if (focal is None or cam_h is None or m is None or m <= 0.0
            or h is None or cy is None):
        return "unknown"
    y2 = cy + h / 2.0
    bz = float(focal) * H_NOM / h
    pz = (float(focal) * float(cam_h) / (y2 - y_h)
          if y2 - y_h > 1.0 else None)
    if pz is None or not (LO <= bz / pz <= HI):
        return "unknown"
    if m < LO * min(bz, pz):
        return "reject_near"
    if m > HI * max(bz, pz):
        return "reject_far"
    return "allow"


def _near(h, y2) -> bool:
    return ((h is not None and h >= NEAR_H)
            or (y2 is not None and y2 >= NEAR_Y2))


def _appr(hist) -> bool:
    """接近 = 框底（接地行）在窗口内下移。框高比不作证据：框尺寸突变/
    截断修复会让高比跳变，但不代表车在靠近。"""
    if len(hist) < 2:
        return False
    _, _, y0 = hist[0]
    _, _, y1 = hist[-1]
    return (y0 is not None and y1 is not None
            and y1 - y0 >= APPROACH_Y2)


def _lat(x, ego_x, lane_w):
    if x is None or ego_x is None:
        return None
    return abs(x - ego_x) * lane_w


def _coarse_blocked(row) -> bool:
    cand = (row.get("coarse") or {}).get("cand") or []
    return any(o[3] in ("car", "unknown_car") or o[1] < 10.0 for o in cand)


@dataclass
class Obs:
    tid: int
    present: bool
    fresh: bool
    h: float | None
    cy: float | None
    x: float | None
    m: float | None
    v: float | None
    dec: str
    passed: bool
    z: float | None
    t_ov: float | None
    pass_ev: str | None


def _extract(row, y_h) -> list[Obs]:
    dist = {c["id"]: c for c in (row.get("dist") or [])}
    hbb = {c["id"]: c for c in (row.get("car_h") or [])}
    pos = {c["id"]: c for c in (row.get("car_pos") or [])}
    mc = {c["id"]: c for c in (row.get("mcars") or [])}
    passed_ids = {e["id"] for e in (row.get("passes") or [])}
    focal = row.get("dgeo_focal")
    cam_h = row.get("dgeo_cam_h")
    out: list[Obs] = []
    for tid in set(dist) | set(hbb) | set(pos) | set(mc) | passed_ids:
        c = dist.get(tid) or {}
        hr = hbb.get(tid) or {}
        pr = pos.get(tid) or {}
        mr = mc.get(tid) or {}
        h = hr.get("h")
        seen = hr.get("seen")
        present = tid in hbb
        m = c.get("m")
        cy = pr.get("cy")
        z = mr.get("z")
        tov = mr.get("t_ov")
        dec = _decision(focal, cam_h, m, h, cy, y_h)
        if tid in passed_ids:
            pass_ev = "pass_event"
        elif tov is not None and tov <= 0.0:
            pass_ev = "t_ov_nonpositive"
        else:
            pass_ev = None
        passed = pass_ev is not None
        out.append(Obs(
            tid=tid, present=present, fresh=present and seen == 0,
            h=h, cy=cy, x=pr.get("x"), m=m, v=c.get("v"), dec=dec,
            passed=passed, z=z, t_ov=tov, pass_ev=pass_ev))
    return out


@dataclass
class Slot:
    occ: bool = False
    since: float | None = None
    unknown_since: float | None = None
    cap_reported: bool = False
    last_fresh: float | None = None
    last_present: float | None = None
    last_lat: float | None = None
    last_threat: bool = False
    last_mode: str | None = None
    dec_now: str | None = None
    depart_n: int = 0
    idle_since: float | None = None
    max_idle: float = 0.0


@dataclass
class Release:
    tid: int
    fid: int
    ts: float
    reason: str
    confirmed: bool
    evidence: str
    duration_s: float
    hold_after_fresh_ms: float | None
    row_gap_ms: float
    last_lat: float | None
    last_threat: bool
    max_idle_s: float


class Shadow:
    """逐目标占用影子；``step`` 只吃观测，不读决策层占用。"""

    def __init__(self):
        self.slots: dict[int, Slot] = {}
        self.hist: dict[int, list] = {}
        self.releases: list[Release] = []
        self.windows: list[float] = []
        self.caps = 0
        self.acquires = 0
        self.acq_lat_unknown = 0
        self.skipped_no_lat = 0
        self.skipped_no_lat_rows: list[tuple] = []
        self.skipped_outlane = 0
        self.skipped_far = 0
        self.acq_modes: dict[str, int] = {}
        self._released: set[int] = set()
        self.gap_ms = 0.0

    def lane_blocked(self) -> bool:
        return any(s.occ for s in self.slots.values())

    def blind_blocked(self) -> bool:
        return any(s.occ and s.dec_now != "allow"
                   for s in self.slots.values())

    def occ_ids(self) -> set[int]:
        return {tid for tid, s in self.slots.items() if s.occ}

    def _close_unknown(self, s: Slot, ts: float) -> None:
        if s.unknown_since is not None:
            self.windows.append(ts - s.unknown_since)
            s.unknown_since = None

    def _release(self, s: Slot, ts: float, fid: int, tid: int,
                 reason: str, confirmed: bool, evidence: str) -> None:
        self._close_unknown(s, ts)
        if s.idle_since is not None:
            s.max_idle = max(s.max_idle, ts - s.idle_since)
        self.releases.append(Release(
            tid=tid, fid=fid, ts=ts, reason=reason, confirmed=confirmed,
            evidence=evidence,
            duration_s=0.0 if s.since is None else ts - s.since,
            hold_after_fresh_ms=(None if s.last_fresh is None
                                 else (ts - s.last_fresh) * 1000.0),
            row_gap_ms=self.gap_ms,
            last_lat=s.last_lat, last_threat=s.last_threat,
            max_idle_s=s.max_idle))
        s.occ = False
        s.since = None
        s.cap_reported = False
        s.depart_n = 0
        s.idle_since = None
        s.max_idle = 0.0
        self._released.add(tid)

    def step(self, ts: float, fid: int, obs_list: list[Obs],
             ego_x: float | None, lane_w: float,
             gap_ms: float = 0.0) -> None:
        self._released = set()
        self.gap_ms = gap_ms
        for o in obs_list:
            s = self.slots.setdefault(o.tid, Slot())
            s.dec_now = o.dec
            lat = _lat(o.x, ego_x, lane_w)
            if o.present:
                s.last_present = ts
            if o.fresh:
                s.last_fresh = ts
            if lat is not None:
                s.last_lat = lat
            y2 = (None if (o.h is None or o.cy is None)
                  else o.cy + o.h / 2.0)
            if o.h is not None:
                hh = self.hist.setdefault(o.tid, [])
                hh.append((ts, o.h, y2))
                self.hist[o.tid] = [e for e in hh if ts - e[0] <= 0.5]
            near_metric = (o.dec == "allow" and o.m is not None
                           and 0.0 < o.m < NEAR_M)
            near2d = _near(o.h, y2)
            appr = _appr(self.hist.get(o.tid) or [])
            threat = near_metric or near2d or appr
            s.last_threat = threat
            if threat:
                s.last_mode = ("metric" if near_metric
                               else "2d" if near2d else "approach")
            if s.occ:
                if o.passed:
                    self._release(s, ts, fid, o.tid, "passed", True,
                                  o.pass_ev or "pass")
                    continue
                if lat is not None and lat > LANE_RELEASE:
                    if o.fresh:
                        s.depart_n += 1
                    if s.depart_n >= DEPART_TICKS:
                        self._release(s, ts, fid, o.tid, "departed", True,
                                      f"lateral>{LANE_RELEASE:.1f}m"
                                      f"×{DEPART_TICKS}")
                        continue
                else:
                    s.depart_n = 0
            if not s.occ:
                # 建占需要近场证人：可信近距 / 近场框 / 接地行下移；远处
                # 框高突变不单独建占。横向未知时只允许“车已贴近自车”
                # （落地行 ≥500px，约 8m）。
                lat_ok = ((lat is not None and lat <= LANE_OUT)
                          or (lat is None and y2 is not None
                              and y2 >= NEAR_Y2))
                if o.present and threat and lat_ok:
                    s.occ = True
                    s.since = ts
                    s.depart_n = 0
                    s.max_idle = 0.0
                    s.idle_since = None if threat else ts
                    s.last_threat = True
                    self.acquires += 1
                    if lat is None:
                        self.acq_lat_unknown += 1
                    mode = ("metric" if near_metric
                            else "2d" if near2d else "approach")
                    key = mode + ("_known" if lat is not None
                                  else "_unknown")
                    self.acq_modes[key] = self.acq_modes.get(key, 0) + 1
                    if o.dec != "allow":
                        s.unknown_since = ts
                elif o.present:
                    if near_metric or near2d:
                        if lat is None:
                            self.skipped_no_lat += 1
                            self.skipped_no_lat_rows.append(
                                (fid, o.tid, o.h,
                                 None if y2 is None else round(y2, 1),
                                 o.m, o.dec))
                        elif lat > LANE_OUT:
                            self.skipped_outlane += 1
                    elif appr:
                        self.skipped_far += 1
                continue
            if o.dec != "allow":
                if s.unknown_since is None:
                    s.unknown_since = ts
                if ((ts - s.unknown_since) * 1000.0 > CAP_MS
                        and not s.cap_reported):
                    self.caps += 1
                    s.cap_reported = True
            else:
                self._close_unknown(s, ts)
            idle = (not threat) and o.present and o.dec == "allow"
            if idle:
                if s.idle_since is None:
                    s.idle_since = ts
            elif s.idle_since is not None:
                s.max_idle = max(s.max_idle, ts - s.idle_since)
                s.idle_since = None
        for tid, s in self.slots.items():
            if not s.occ or tid in self._released:
                continue
            ref = s.last_fresh if s.last_fresh is not None else s.since
            if ref is None or (ts - ref) * 1000.0 <= LOST_MS:
                continue
            if not s.last_threat:
                reason = "lost_clear"
            elif s.last_lat is None or s.last_lat <= LANE_IN:
                reason = "lost_near"
            else:
                reason = "lost_ambiguous"
            self._release(s, ts, fid, tid, reason, False,
                          "no_fresh_2d>500ms")


class Episodes:
    def __init__(self):
        self.start: float | None = None
        self.durs: list[float] = []

    def feed(self, ts: float, on: bool) -> None:
        if on and self.start is None:
            self.start = ts
        elif not on and self.start is not None:
            self.durs.append(ts - self.start)
            self.start = None

    def finish(self, ts: float) -> None:
        if self.start is not None:
            self.durs.append(ts - self.start)
            self.start = None


def _stats(vals: list[float]) -> str:
    if not vals:
        return "—"
    ds = sorted(vals)
    p90 = ds[min(len(ds) - 1, int(len(ds) * 0.9))]
    return (f"P50={statistics.median(ds):.2f}s "
            f"P90={p90:.2f}s max={max(ds):.2f}s")


def replay(name: str) -> dict:
    rows = _load(name)
    cal = load_calib()
    sh = Shadow()
    r = {
        "name": name, "ticks": 0, "blocked_ticks": 0, "blind_ticks": 0,
        "blind_free_ticks": 0, "multi_ticks": 0,
        "box_missing_ticks": 0, "dist_none_ticks": 0,
        "lane_unknown_ticks": 0, "release_keep_other": 0,
        "acq_lat_unknown": 0, "skipped_no_lat": 0,
        "near_inlane": 0, "near_inlane_unocc": 0,
        "lost_hold_max_ms": 0.0, "lost_excess_max_ms": 0.0,
        "releases": {"passed": 0, "departed": 0,
                     "lost_near": 0, "lost_ambiguous": 0,
                     "lost_clear": 0},
        "gap": {"inlane_near": 0, "inlane_near_occ": 0,
                "inlane_phantom": 0, "outlane": 0},
        "gap_rows": [], "violations": [],
    }
    ep_blocked, ep_blind, ep_blind_free = Episodes(), Episodes(), Episodes()
    last_ts = 0.0
    prev_ts: float | None = None
    for row in rows:
        ts = row["ts_ns"] / 1e9
        gap_ms = 0.0 if prev_ts is None else (ts - prev_ts) * 1000.0
        prev_ts = ts
        last_ts = ts
        ego = row.get("executed_lane")
        obs = _extract(row, cal.y_h)
        occ_before = sh.occ_ids()
        sh.step(ts, row["fid"], obs, ego, cal.lane_w_m, gap_ms)
        blocked = sh.lane_blocked()
        blind = sh.blind_blocked()
        cb = _coarse_blocked(row)
        r["ticks"] += 1
        r["blocked_ticks"] += int(blocked)
        r["blind_ticks"] += int(blind)
        r["blind_free_ticks"] += int(blind and not cb)
        if len(sh.occ_ids()) >= 2:
            r["multi_ticks"] += 1
        obs_by = {o.tid: o for o in obs}
        occ = sh.occ_ids()
        if occ and all(sh.slots[tid].last_lat is None for tid in occ):
            r["lane_unknown_ticks"] += 1
        if any((obs_by.get(tid) is None
                or not obs_by[tid].present) for tid in occ):
            r["box_missing_ticks"] += 1
        if any(obs_by.get(tid) is not None and obs_by[tid].m is None
               for tid in occ):
            r["dist_none_ticks"] += 1
        ep_blocked.feed(ts, blocked)
        ep_blind.feed(ts, blind)
        ep_blind_free.feed(ts, blind and not cb)
        # 多车隔离：释放只作用于自己的槽位；别的槽位仍占时车道不得变空
        released = {e.tid for e in sh.releases
                    if abs(e.ts - ts) < 1e-9}
        occ_after = sh.occ_ids()
        if released and (occ_after - released):
            r["release_keep_other"] += 1
        if not occ_after and (occ_before - released):
            r["violations"].append(
                {"fid": row["fid"], "released": sorted(released)})
        # 安全覆盖：在道且近、未掠过的新鲜目标必须处于占用
        for o in obs:
            if not o.present or o.passed:
                continue
            lat = _lat(o.x, ego, cal.lane_w_m)
            if lat is None or lat > LANE_OUT:
                continue
            y2 = (None if (o.h is None or o.cy is None)
                  else o.cy + o.h / 2.0)
            near = ((o.dec == "allow" and o.m is not None
                     and 0.0 < o.m < NEAR_M) or _near(o.h, y2))
            if not near:
                continue
            r["near_inlane"] += 1
            slot = sh.slots.get(o.tid)
            if not (slot and slot.occ):
                r["near_inlane_unocc"] += 1
        # 质量门 gap 逐拍核对（与实验 18 同口径：m<8、dec!=allow、粗层无占用）
        if row.get("dgeo_focal") is None or row.get("dgeo_cam_h") is None:
            continue
        for o in obs:
            if (o.m is None or not 0.0 < o.m < NEAR_M
                    or o.dec == "allow" or o.passed):
                continue
            if o.h is None or o.cy is None or _coarse_blocked(row):
                continue
            lat = _lat(o.x, ego, cal.lane_w_m)
            y2 = o.cy + o.h / 2.0
            slot = sh.slots.get(o.tid)
            occupied = bool(slot and slot.occ)
            if lat is not None and lat <= LANE_OUT:
                if _near(o.h, y2):
                    r["gap"]["inlane_near"] += 1
                    r["gap"]["inlane_near_occ"] += int(occupied)
                else:
                    r["gap"]["inlane_phantom"] += 1
            else:
                r["gap"]["outlane"] += 1
            r["gap_rows"].append({
                "fid": row["fid"], "id": o.tid, "m": round(o.m, 2),
                "dec": o.dec, "h": o.h, "lat": None if lat is None
                else round(lat, 2), "occ": occupied})
    ep_blocked.finish(last_ts)
    ep_blind.finish(last_ts)
    ep_blind_free.finish(last_ts)
    r["ep_blocked"] = ep_blocked.durs
    r["ep_blind"] = ep_blind.durs
    r["ep_blind_free"] = ep_blind_free.durs
    r["windows"] = sh.windows
    r["caps"] = sh.caps
    r["acquires"] = sh.acquires
    r["acq_lat_unknown"] = sh.acq_lat_unknown
    r["skipped_no_lat"] = sh.skipped_no_lat
    r["skipped_no_lat_rows"] = sh.skipped_no_lat_rows
    r["skipped_outlane"] = sh.skipped_outlane
    r["skipped_far"] = sh.skipped_far
    r["acq_modes"] = sh.acq_modes
    r["releases_detail"] = sh.releases
    for e in sh.releases:
        r["releases"][e.reason] += 1
    r["max_occ_s"] = max((e.duration_s for e in sh.releases), default=0.0)
    r["max_idle_s"] = max((e.max_idle_s for e in sh.releases), default=0.0)
    r["idle_over_1s"] = sum(1 for e in sh.releases if e.max_idle_s > 1.0)
    lost_rels = [e for e in sh.releases if e.reason.startswith("lost")]
    r["lost_hold_max_ms"] = max(
        (e.hold_after_fresh_ms or 0.0 for e in lost_rels), default=0.0)
    r["lost_excess_max_ms"] = max(
        ((e.hold_after_fresh_ms or 0.0) - LOST_MS - e.row_gap_ms
         for e in lost_rels), default=0.0)
    by_tid: dict[int, list[Release]] = {}
    for e in sh.releases:
        by_tid.setdefault(e.tid, []).append(e)
    flaps = []
    for tid, es in by_tid.items():
        es.sort(key=lambda x: x.ts)
        for a, b in zip(es, es[1:]):
            gap_s = (b.ts - b.duration_s) - a.ts
            if 0.0 <= gap_s < 0.5:
                flaps.append((tid, round(gap_s, 2), a.reason, b.reason))
    r["reacquire_fast"] = flaps
    return r


def report(r: dict) -> None:
    print(f"===== {r['name']} | 拍 {r['ticks']} | 占用 {r['blocked_ticks']}"
          f"（{100.0 * r['blocked_ticks'] / max(1, r['ticks']):.0f}%）"
          f" | 盲占 {r['blind_ticks']} | 盲占且粗层空 {r['blind_free_ticks']}")
    print(f"  占用段 {_stats(r['ep_blocked'])} | 盲占段 "
          f"{_stats(r['ep_blind'])} | 盲占且粗层空段 {_stats(r['ep_blind_free'])}")
    print(f"  未知窗口 {len(r['windows'])} {_stats(r['windows'])}"
          f" | cap>{CAP_MS / 1000:.1f}s {r['caps']}"
          f" | 建立占用 {r['acquires']}")
    print(f"  释放 掠过 {r['releases']['passed']} / 离道 "
          f"{r['releases']['departed']} / 丢失-近 {r['releases']['lost_near']}"
          f" / 丢失-横向模糊 {r['releases']['lost_ambiguous']}"
          f" / 丢失-不近 {r['releases']['lost_clear']}"
          f" | 单目标最长占用 {r['max_occ_s']:.2f}s"
          f" | 最长非威胁驻留 {r['max_idle_s']:.2f}s"
          f"（>1s {r['idle_over_1s']} 次）")
    fl = r.get("reacquire_fast") or []
    if fl:
        gs = [x[2] if len(x) == 5 else x[1] for x in fl]
        print(f"  释放后 0.5s 内重新建占 {len(fl)} 次"
              f"（空档 {min(gs):.2f}~{max(gs):.2f}s）")
        for x in fl[:4]:
            if len(x) == 5:
                print(f"    {x[0]} id{x[1]} 空档 {x[2]}s {x[3]}→{x[4]}")
            else:
                print(f"    id{x[0]} 空档 {x[1]}s {x[2]}→{x[3]}")
    print(f"  距离无效仍占：深度缺 {r['dist_none_ticks']} 拍 / 框缺 "
          f"{r['box_missing_ticks']} 拍 | 横向未知占道 "
          f"{r['lane_unknown_ticks']} 拍（建占时横向未知 "
          f"{r['acq_lat_unknown']}/{r['acquires']}）"
          f" | 丢失释放最长驻留 {r['lost_hold_max_ms']:.0f}ms")
    print(f"  建占来源：{r.get('acq_modes', {})}"
          f" | 横向未知不建占 {r.get('skipped_no_lat', 0)} 拍"
          f" | 离道不建占 {r.get('skipped_outlane', 0)} 拍"
          f" | 仅放大无归属不建占 {r.get('skipped_far', 0)} 拍")
    for x in (r.get("skipped_no_lat_rows") or [])[:4]:
        print(f"    横向未知未建占样例 fid {x[0]} id{x[1]} h={x[2]} "
              f"y2={x[3]} m={x[4]} {x[5]}")
    print(f"  安全覆盖：在道且近未掠过 {r['near_inlane']} 拍，其中未占用 "
          f"{r['near_inlane_unocc']} 拍")
    g = r["gap"]
    print(f"  gap 核对：在道且近 {g['inlane_near']}（占用 "
          f"{g['inlane_near_occ']}）/ 在道但非近 {g['inlane_phantom']}"
          f" / 离道 {g['outlane']} | 多车拍 {r['multi_ticks']}"
          f" | 释放后他人仍占 {r['release_keep_other']}"
          f" | 释放清空他人占用 {len(r['violations'])}")


def _run_synthetic() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def ok(name, cond, msg=""):
        out.append((name, bool(cond), msg))

    def o(tid=1, h=200.0, cy=500.0, x=0.2, m=2.0, dec="allow",
          passed=False, fresh=True, present=True):
        return Obs(tid, present, fresh, h, cy, x, m, None, dec, passed,
                   m, 0.5, "pass_event" if passed else None)

    # S1 距离被拒仍占用；掠过才释放
    sh = Shadow()
    for i in range(5):
        sh.step(i * 0.05, i, [o(dec="reject_near")], 0.0, 3.38)
    blocked = sh.lane_blocked()
    sh.step(0.30, 6, [o(dec="allow", passed=True)], 0.0, 3.38)
    ok("S1 距离被拒仍占用/掠过释放",
       blocked and not sh.slots[1].occ
       and sh.releases[-1].reason == "passed",
       f"blocked={blocked} release={sh.releases[-1].reason}")

    # S2 400ms 丢失仍占，600ms 才有界释放并标 uncertain
    sh = Shadow()
    for i in range(3):
        sh.step(i * 0.05, i, [o()], 0.0, 3.38)
    sh.step(0.45, 3, [], 0.0, 3.38)
    still = sh.slots[1].occ
    sh.step(0.66, 4, [], 0.0, 3.38)
    rel = sh.releases[-1]
    ok("S2 丢失 400ms 仍占/600ms uncertain",
       still and not sh.slots[1].occ and rel.reason == "lost_near"
       and not rel.confirmed,
       f"still={still} reason={rel.reason}")

    # S3 A 离道只释放 A，B 仍占（横向释放需连续 2 次新鲜 >2.0m）
    sh = Shadow()
    for i in range(3):
        sh.step(i * 0.05, i, [o(1), o(2, x=-0.2)], 0.0, 3.38)
    sh.step(0.20, 3, [o(1, x=1.0), o(2, x=-0.2)], 0.0, 3.38)
    held_one = sh.slots[1].occ
    sh.step(0.25, 4, [o(1, x=1.0), o(2, x=-0.2)], 0.0, 3.38)
    ok("S3 A 离道只释放 A（1 拍滞回）",
       held_one and (not sh.slots[1].occ) and sh.slots[2].occ
       and sh.lane_blocked(),
       f"held1={held_one} A={sh.slots[1].occ} B={sh.slots[2].occ}")

    # S4 A 丢失超时不清 B
    sh = Shadow()
    for i in range(3):
        sh.step(i * 0.05, i, [o(1), o(2, x=-0.2)], 0.0, 3.38)
    sh.step(0.70, 3, [o(2, x=-0.2)], 0.0, 3.38)
    rel_a = [e for e in sh.releases if e.tid == 1]
    ok("S4 A 超时不清 B",
       (not sh.slots[1].occ) and sh.slots[2].occ and sh.lane_blocked()
       and rel_a and rel_a[-1].reason == "lost_near",
       f"A={sh.slots[1].occ} B={sh.slots[2].occ}")

    # S5 冻结框不刷新新鲜度
    sh = Shadow()
    for i in range(2):
        sh.step(i * 0.05, i, [o()], 0.0, 3.38)
    sh.step(0.40, 2, [o(fresh=False)], 0.0, 3.38)
    still = sh.slots[1].occ
    sh.step(0.62, 3, [o(fresh=False)], 0.0, 3.38)
    rel = sh.releases[-1]
    ok("S5 冻结框不刷新新鲜度",
       still and not sh.slots[1].occ and rel.reason == "lost_near"
       and not rel.confirmed,
       f"still={still} reason={rel.reason}")

    # S6 未知占用有界但不自动判空
    sh = Shadow()
    for i in range(41):
        sh.step(i * 0.05, i, [o(dec="unknown")], 0.0, 3.38)
    ok("S6 未知 2s 仍占且记 cap",
       sh.slots[1].occ and sh.caps >= 1 and not sh.releases,
       f"occ={sh.slots[1].occ} caps={sh.caps}")

    # S7 A 离道与 B 进入同拍：车道持续占用；A 两拍后释放
    sh = Shadow()
    for i in range(3):
        sh.step(i * 0.05, i, [o(1)], 0.0, 3.38)
    sh.step(0.20, 3, [o(1, x=1.0), o(2, x=0.2)], 0.0, 3.38)
    held_one = sh.slots[1].occ
    blocked_one = sh.lane_blocked()
    sh.step(0.25, 4, [o(1, x=1.0), o(2, x=0.2)], 0.0, 3.38)
    ok("S7 A 离道/B 进入同拍不落空",
       held_one and blocked_one and sh.slots[2].occ
       and sh.lane_blocked() and not sh.slots[1].occ,
       f"held1={held_one} blocked1={blocked_one} "
       f"B={sh.slots[2].occ}")

    # S8 离道车不建立占用
    sh = Shadow()
    for i in range(3):
        sh.step(i * 0.05, i, [o(x=1.0)], 0.0, 3.38)
    ok("S8 离道车不建立占用", not sh.slots[1].occ,
       f"occ={sh.slots[1].occ}")

    # S9 轨道 id 切换：新 id 接续占用，旧 id 超时不清新 id
    sh = Shadow()
    for i in range(3):
        sh.step(i * 0.05, i, [o(1)], 0.0, 3.38)
    sh.step(0.20, 3, [o(2)], 0.0, 3.38)
    blocked_switch = sh.lane_blocked()
    sh.step(0.80, 4, [o(2)], 0.0, 3.38)
    ok("S9 id 切换不清新占用", blocked_switch and sh.slots[2].occ
       and not sh.slots[1].occ and sh.lane_blocked(),
       f"switch={blocked_switch} old={sh.slots[1].occ} "
       f"new={sh.slots[2].occ}")

    # S10 横向滞回：1.8m 不释放，2.1m 连续 2 拍才释放
    sh = Shadow()
    for i in range(3):
        sh.step(i * 0.05, i, [o()], 0.0, 3.38)
    for i in range(3, 6):
        sh.step(i * 0.05, i, [o(x=1.8 / 3.38)], 0.0, 3.38)
    in_band = sh.slots[1].occ
    sh.step(0.30, 6, [o(x=2.1 / 3.38)], 0.0, 3.38)
    one_far = sh.slots[1].occ
    sh.step(0.35, 7, [o(x=2.1 / 3.38)], 0.0, 3.38)
    ok("S10 横向滞回", in_band and one_far and not sh.slots[1].occ
       and sh.releases and sh.releases[-1].reason == "departed",
       f"band={in_band} far1={one_far} occ={sh.slots[1].occ}")
    return out


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    names = argv[1:] or SESSIONS
    agg = {"ticks": 0, "blocked_ticks": 0, "blind_ticks": 0,
           "blind_free_ticks": 0, "multi_ticks": 0, "caps": 0,
           "box_missing_ticks": 0, "dist_none_ticks": 0,
           "lane_unknown_ticks": 0, "release_keep_other": 0,
           "acq_lat_unknown": 0, "skipped_no_lat": 0,
           "skipped_outlane": 0, "skipped_far": 0,
           "acq_modes": {},
           "near_inlane": 0, "near_inlane_unocc": 0,
           "lost_hold_max_ms": 0.0, "lost_excess_max_ms": 0.0,
           "acquires": 0, "windows": [], "ep_blocked": [], "ep_blind": [],
           "ep_blind_free": [], "releases": {"passed": 0, "departed": 0,
                                             "lost_near": 0,
                                             "lost_ambiguous": 0,
                                             "lost_clear": 0},
           "gap": {"inlane_near": 0, "inlane_near_occ": 0,
                   "inlane_phantom": 0, "outlane": 0},
           "violations": [], "releases_detail": [], "name": "合计",
           "reacquire_fast": [],
           "max_occ_s": 0.0, "max_idle_s": 0.0, "idle_over_1s": 0}
    per_session: dict[str, dict] = {}
    for n in names:
        r = replay(n)
        per_session[n] = r
        report(r)
        for k in ("ticks", "blocked_ticks", "blind_ticks",
                  "blind_free_ticks", "multi_ticks", "caps", "acquires",
                  "box_missing_ticks", "dist_none_ticks",
                  "lane_unknown_ticks", "release_keep_other",
                  "acq_lat_unknown", "skipped_no_lat",
                  "skipped_outlane", "skipped_far",
                  "near_inlane", "near_inlane_unocc"):
            agg[k] += r[k]
        agg["lost_hold_max_ms"] = max(agg["lost_hold_max_ms"],
                                      r["lost_hold_max_ms"])
        agg["lost_excess_max_ms"] = max(agg["lost_excess_max_ms"],
                                        r["lost_excess_max_ms"])
        for k, v in r["acq_modes"].items():
            agg["acq_modes"][k] = agg["acq_modes"].get(k, 0) + v
        for k in ("windows", "ep_blocked", "ep_blind", "ep_blind_free"):
            agg[k] += r[k]
        for k in ("passed", "departed", "lost_near", "lost_ambiguous",
                  "lost_clear"):
            agg["releases"][k] += r["releases"][k]
        for k in ("inlane_near", "inlane_near_occ", "inlane_phantom",
                  "outlane"):
            agg["gap"][k] += r["gap"][k]
        agg["violations"] += r["violations"]
        agg["releases_detail"] += [(n, e) for e in r["releases_detail"]]
        agg["reacquire_fast"] += [(n, *x) for x in r["reacquire_fast"]]
        agg["max_occ_s"] = max(agg["max_occ_s"], r["max_occ_s"])
        agg["max_idle_s"] = max(agg["max_idle_s"], r["max_idle_s"])
        agg["idle_over_1s"] += r["idle_over_1s"]
    print("\n== 合计")
    report(agg)

    # 回归 1：id77 安全性质
    r508 = per_session.get("20261022_140508_p2")
    id77_ok = True
    id77_msg = "N/A（本批未含 140508）"
    if r508 is not None:
        hit = [g for g in r508["gap_rows"] if g["fid"] == 13043
               and g["id"] == 77]
        id77_ok = bool(hit and hit[0]["occ"]
                       and hit[0]["dec"] in ("reject_near", "unknown"))
        id77_msg = f"{hit[0]}" if hit else "未命中 fid 13043/id77"
    rels: dict[tuple[str, int], list[Release]] = {}
    for n, e in agg["releases_detail"]:
        rels.setdefault((n, e.tid), []).append(e)

    def find(key, reason, evidence=None):
        for e in rels.get(key, []):
            if e.reason == reason and (evidence is None
                                       or e.evidence == evidence):
                return e
        return None

    has_ref_lost = all(p[0] in per_session
                       for p in REFERENCE_LOST_UNCERTAIN)
    has_ref_pass = all(p[0] in per_session
                       for p in REFERENCE_PASS_RESOLVED)
    lost_ok = (not has_ref_lost) or all(
        find(p, "lost_near") is not None
        and not find(p, "lost_near").confirmed
        for p in REFERENCE_LOST_UNCERTAIN)
    all_lost = [e for _, e in agg["releases_detail"]
                if e.reason.startswith("lost")]
    all_lost_ok = all(not e.confirmed for e in all_lost)
    pass_ok = (not has_ref_pass) or all(
        find(p, "passed", "pass_event") is not None
        and find(p, "passed", "pass_event").confirmed
        for p in REFERENCE_PASS_RESOLVED)
    has_excl = all(p[0] in per_session for p in REFERENCE_LANE_UNKNOWN_FAR)
    excl_ok = (not has_excl) or all(not rels.get(p)
                                    for p in REFERENCE_LANE_UNKNOWN_FAR)
    uncertain_ok = lost_ok and pass_ok and all_lost_ok and excl_ok
    gap_ok = agg["gap"]["inlane_near_occ"] == agg["gap"]["inlane_near"]
    viol_ok = not agg["violations"]
    cover_ok = agg["near_inlane_unocc"] == 0
    bound_ok = agg["lost_excess_max_ms"] <= 150.0
    synth = _run_synthetic()
    synth_ok = all(cond for _, cond, _ in synth)

    print("\n== 回归")
    print(f"[{'PASS' if id77_ok else 'FAIL'}] id77 距离被拒仍占用：{id77_msg}")
    print(f"[{'PASS' if lost_ok else 'FAIL'}] 4 例固定回归"
          f"{'命中' if has_ref_lost else ' N/A（本批未含该局）'}")
    for p in REFERENCE_LOST_UNCERTAIN:
        e = find(p, "lost_near")
        if not has_ref_lost:
            break
        print(f"    {p[0]} id{p[1]} "
              f"{'命中 ' + str(round(e.duration_s, 2)) + 's' if e else '缺'}")
    print(f"[{'PASS' if all_lost_ok else 'FAIL'}] 全部丢失释放均未确认："
          f"{len(all_lost)} 例")
    print(f"[{'PASS' if pass_ok else 'FAIL'}] 2 例掠过确认"
          f"{'命中' if has_ref_pass else ' N/A（本批未含该局）'}")
    for p in REFERENCE_PASS_RESOLVED:
        e = find(p, "passed", "pass_event")
        if not has_ref_pass:
            break
        print(f"    {p[0]} id{p[1]} "
              f"{'pass@fid ' + str(e.fid) + ' 距最后新鲜框 ' + str(round(e.hold_after_fresh_ms or 0)) + 'ms' if e else '缺'}")
    print(f"[{'PASS' if excl_ok else 'FAIL'}] 横向未知远框不建占"
          f"{'命中' if has_excl else ' N/A（本批未含该局）'}")
    if has_excl:
        for p in REFERENCE_LANE_UNKNOWN_FAR:
            print(f"    {p[0]} id{p[1]} 占用释放记录 "
                  f"{len(rels.get(p, []))} 条")
    print(f"[{'PASS' if gap_ok else 'FAIL'}] 在道且近的 gap 拍全部占用："
          f"{agg['gap']['inlane_near_occ']}/{agg['gap']['inlane_near']}")
    print(f"[{'PASS' if viol_ok else 'FAIL'}] 多车释放不清他人占用："
          f"{len(agg['violations'])} 次")
    print(f"[{'PASS' if cover_ok else 'FAIL'}] 在道且近未掠过目标全部占用："
          f"未占 {agg['near_inlane_unocc']}/{agg['near_inlane']}")
    print(f"[{'PASS' if bound_ok else 'FAIL'}] 丢失释放有界：最长驻留 "
          f"{agg['lost_hold_max_ms']:.0f}ms，扣除行间隔后超出 500ms "
          f"{agg['lost_excess_max_ms']:.0f}ms（≤150ms）")
    for name, cond, msg in synth:
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {msg}")
    all_ok = (id77_ok and uncertain_ok and gap_ok and viol_ok and cover_ok
              and bound_ok and synth_ok)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
