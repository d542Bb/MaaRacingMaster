# -*- coding: utf-8 -*-
"""离线验证：真实纵深断供窗口里，框高 TTC 还剩多少覆盖（实验 10b）。

只读控制 trace，不 import 生产代码、不发控制指令。

对每个「纵深 z 变 None、但 2D 轨仍在世界模型里」的断供拍：
- 新鲜 2D：本拍原因不是 TRACK_LOST，且近 3 拍框高有变化（冻结框不算）；
- 用最近 ≤6 拍（0.3s，20Hz）的框高做稳健 log 斜率，τ_h = 1/slope；
- 统计新鲜断供拍里 τ_h 的有效覆盖率；
- 预警 = τ_h ≤ 阈值连续 2 拍；事故局用 fid 1563 作碰撞真值，其余局用
  3 秒内是否出现 pass 事件作近似真值。

注意：这是“框还在、深度没了”的覆盖；二维框一起消失时（事故末段），
任何图像运动估计都没有输入。
"""

from __future__ import annotations

import argparse
import glob
import json
import os

import numpy as np

W = 6            # 0.3s @20Hz
TTC_WARN = 1.5   # 预警阈值（秒）
PERSIST = 2      # 连续有效拍数
IMPACT_FID = 1563
ENVELOPE_M = 2.3  # 自车碰撞包络（ego 半宽 + 车半宽 + 余量）


def _resolve(session: str) -> str:
    base = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug\speedrush")
    hits = glob.glob(os.path.join(base, session, "trace_*.jsonl"))
    if not hits:
        raise SystemExit(f"找不到 trace：{session}")
    return hits[0]


def _robust_line(tt, yy):
    slopes = []
    n = len(tt)
    for i in range(n):
        for j in range(i + 1, n):
            dt = tt[j] - tt[i]
            if dt > 1e-9:
                slopes.append((yy[j] - yy[i]) / dt)
    if not slopes:
        return None
    b = float(np.median(slopes))
    a = float(np.median(yy - b * tt))
    pred = a + b * tt
    ss_res = float(((yy - pred) ** 2).sum())
    ss_tot = float(((yy - yy.mean()) ** 2).sum()) or 1.0
    return b, 1.0 - ss_res / ss_tot


def _ttc_at(series, k):
    """用 k 及之前最多 W 拍的框高算 τ_h；返回 (tau, fresh, jump)。"""
    win = []
    for j in range(max(0, k - W + 1), k + 1):
        h = series[j]["h"]
        if h is not None and h >= 8:
            win.append((series[j]["t"], float(h)))
    if len(win) < 4:
        return None, False, False
    hs = [h for _t, h in win]
    # 冻结框（如事故末段 h=60）不算新鲜：最近 3 个样本必须有变化
    fresh = len(set(hs[-3:])) >= 2
    # 框突变：相邻样本 log 高变化 >0.4（约 50%）
    jump = any(abs(np.log(hs[i + 1] / hs[i])) > 0.4
               for i in range(len(hs) - 1))
    if not fresh:
        return None, False, jump
    tt = np.array([t - win[0][0] for t, _h in win])
    yy = np.log(np.array(hs))
    fit = _robust_line(tt, yy)
    if not fit:
        return None, False, jump
    slope, r2 = fit
    if slope <= 1e-3 or r2 < 0.5:
        return None, fresh, jump
    tau = 1.0 / slope
    if not 0.05 <= tau <= 10.0:
        return None, fresh, jump
    return tau, fresh, jump


def analyze(path: str):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    t = [0.0]
    for r in rows[1:]:
        t.append(t[-1] + (r.get("dt") or 0.05))
    ids = set()
    for r in rows:
        ids |= {c["id"] for c in (r.get("car_h") or [])}
        ids |= {m["id"] for m in (r.get("mcars") or [])}
    series = {cid: [] for cid in ids}
    for i, r in enumerate(rows):
        hmap = {c["id"]: c.get("h") for c in (r.get("car_h") or [])}
        zmap = {m["id"]: m.get("z") for m in (r.get("mcars") or [])}
        xmap = {m["id"]: m.get("x") for m in (r.get("mcars") or [])}
        whymap = {d["id"]: d.get("why") for d in (r.get("dist") or [])}
        vis = set(hmap)
        for cid in ids:
            series[cid].append({
                "i": i, "t": t[i], "fid": r.get("fid"),
                "h": hmap.get(cid), "z": zmap.get(cid),
                "x": xmap.get(cid),
                "why": whymap.get(cid), "vis": cid in vis,
                "raw_cars": r.get("raw_cars"),
                "pass": any(p.get("id") == cid for p in (r.get("passes") or [])),
            })
    out = []
    for cid, s in series.items():
        outage = [k for k, c in enumerate(s)
                  if c["z"] is None and c["vis"]]
        if not outage:
            continue
        # 断供段（连续拍）
        runs = []
        cur = []
        for k in outage:
            if cur and k == cur[-1] + 1:
                cur.append(k)
            else:
                if cur:
                    runs.append(cur)
                cur = [k]
        if cur:
            runs.append(cur)
        valid = fresh_n = 0
        state = {"stable": 0, "abnormal": 0, "stale": 0}
        episodes = []
        streak = 0
        ep = None
        for k in outage:
            tau, fresh, jump = _ttc_at(s, k)
            if fresh:
                fresh_n += 1
            if tau is not None:
                valid += 1
                state["stable"] += 1
            elif fresh:
                state["abnormal"] += 1
            else:
                state["stale"] += 1
            x = _last_x(s, k)
            in_path = x is not None and abs(x) <= ENVELOPE_M
            cond = tau is not None and tau <= TTC_WARN and in_path
            if cond:
                streak += 1
                if streak >= PERSIST and ep is None:
                    ep = {"cid": cid, "start": s[k]["t"],
                          "fid": s[k]["fid"], "tau": tau, "x": x,
                          "jump": jump}
            else:
                streak = 0
                if ep is not None:
                    ep["end"] = s[k]["t"]
                    episodes.append(ep)
                    ep = None
        if ep is not None:
            ep["end"] = s[-1]["t"]
            episodes.append(ep)
        passed_t = next((c["t"] for c in s if c["pass"]), None)
        out.append({"cid": cid, "runs": runs, "n": len(outage),
                    "fresh": fresh_n, "valid": valid, "state": state,
                    "episodes": episodes, "pass_t": passed_t,
                    "detail": s})
    return out, t


def _last_x(s, k):
    for j in range(k, -1, -1):
        if s[j]["x"] is not None:
            return s[j]["x"]
    return None


def _last_fresh(s):
    for k in range(len(s) - 1, -1, -1):
        if s[k]["z"] is not None:
            return s[k]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--focus", type=int, default=None)
    args = ap.parse_args()
    for sess in args.sessions:
        path = sess if sess.endswith(".jsonl") else _resolve(sess)
        out, t = analyze(path)
        crash = "150545" in path
        total = sum(x["n"] for x in out)
        fresh = sum(x["fresh"] for x in out)
        valid = sum(x["valid"] for x in out)
        print(f"== {os.path.basename(os.path.dirname(path)) or path}")
        print(f"   断供拍 {total}（有车轨）| 新鲜 2D {fresh} "
              f"({fresh*100/(total or 1):.0f}%) | τ_h 有效 {valid} "
              f"({valid*100/(fresh or 1):.0f}% 的新鲜拍，"
              f"{valid*100/(total or 1):.0f}% 的全部断供拍)")
        st = {"stable": 0, "abnormal": 0, "stale": 0}
        for x in out:
            for k in st:
                st[k] += x["state"][k]
        tt = sum(st.values()) or 1
        print(f"   三态：稳定 {st['stable']} ({st['stable']*100/tt:.0f}%) / "
              f"新鲜异常 {st['abnormal']} ({st['abnormal']*100/tt:.0f}%) / "
              f"冻结或缺失 {st['stale']} ({st['stale']*100/tt:.0f}%)")
        impact_t = (next((c["t"] for c in out[0]["detail"]
                          if c["fid"] == IMPACT_FID), None)
                    if crash and out else None)
        eps = [ep for x in out for ep in x["episodes"]]
        for ep in eps:
            if crash:
                ep["true"] = ep["cid"] == 10 and ep["start"] < impact_t
                ep["lead"] = (impact_t - ep["start"]) if ep["true"] else None
            else:
                passed = next((x["pass_t"] for x in out
                               if x["cid"] == ep["cid"]), None)
                ep["true"] = (passed is not None
                              and ep["start"] <= passed <= ep["start"] + 3.0)
                ep["lead"] = (passed - ep["start"]) if ep["true"] else None
        true_eps = [e for e in eps if e["true"]]
        false_eps = [e for e in eps if not e["true"]]
        false_pre = [e for e in false_eps
                     if impact_t is None or e["start"] < impact_t]
        print(f"   预警段 {len(eps)}：真 {len(true_eps)} / 假 {len(false_eps)}"
              + (f"（碰撞前假预警 {len(false_pre)}）" if crash else ""))
        for ep in eps:
            tag = "跳跃" if ep["jump"] else "平滑"
            lead = "" if ep["lead"] is None else f" lead {ep['lead']:.2f}s"
            print(f"     car{ep['cid']} fid{ep['fid']} t={ep['start']:.2f} "
                  f"τ_h={ep['tau']:.2f} x={ep['x']} {tag} → "
                  f"{'真' if ep['true'] else '假'}{lead}")
        if args.focus is not None:
            x = next((x for x in out if x["cid"] == args.focus), None)
            if x:
                print(f"   焦点 car{args.focus}:")
                s = x["detail"]
                for k, c in enumerate(s):
                    if 1530 <= (c["fid"] or 0) <= 1570:
                        tau, fresh_flag, jump_flag = _ttc_at(s, k)
                        print(f"     fid{c['fid']} t={c['t']:.2f} "
                              f"h={c['h']} z={c['z']} why={c['why']} "
                              f"τ_h={None if tau is None else round(tau,2)} "
                              f"新鲜={fresh_flag} 突变={jump_flag}")
        # 事故局：碰撞车末段的框是否冻结
        if crash:
            print("   碰撞车 car10 断供段末段框高：")
            x = next((x for x in out if x["cid"] == 10), None)
            if x:
                s = x["detail"]
                tail = [c for c in s if c["fid"] is not None and c["fid"] >= 1554][:12]
                print("     " + " | ".join(
                    f"f{c['fid']}:h{c['h']}:z{c['z']}:{c['why']}"
                    for c in tail))


if __name__ == "__main__":
    main()
