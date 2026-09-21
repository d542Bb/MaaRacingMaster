"""补采裁定 · 预分析：虚线检出密度 vs 车速/直道（2026-09-20，维护者裁定「补采新素材」）。

要回答的问题（把「真实操作环境会不会阵亡」从疑问变成数字）：
  线位轮三死因里，「虚线 1~2 段/帧」是**运行期素材属性**还是**判决窗属性**？
  若检出密度随车速显著变化（运动模糊 ∝ 速度），低/中速段就存在可采材料 →
  补采有靶；若全速度带都稀疏 → 补采救不了，「会阵亡」成立，须回头重裁后备路线。

口径：
  - 场次 = hud.jsonl 含 rate 字段的 0918/19 批（里程批无速度读数，跳过并计数）；
  - 帧 = 比赛态（staged_filter stage4）∧ 每场等时隙抽样 ≤60 帧（控成本，抽样间隔入档）；
  - 检出 = o3.classify(dash 前端，四条语义冻结) 的 selected 段数/帧；
  - 车速 = 最近邻 HUD rate_a（缺则 rate_b），±0.6s 内无读数 → 该帧弃（计 missing）；
  - 直道 = straight_mask v2 按 seq 桥接（stride2 缓存）。

用法：.venv\\Scripts\\python.exe tools/experiments/speedrush_calibration/probe_gate3_yield.py [sessions...]
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from probe_gate0_vp import DEMOS, CACHE, analyze_series, load_base, score_intervals, staged_filter
from probe_gate2_hist import get_cal, straight_mask
import probe_gate3_0_object as o3

RATE_WIN = 0.6          # HUD 读数最近邻时间窗 s
MAX_FRAMES = 60         # 每场等时隙抽样上限
RATE_BINS = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 999)]


def hud_rates(sess):
    p = DEMOS / sess / "hud.jsonl"
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        f = d["fields"]
        v = None
        for k in ("rate_a", "rate_b"):
            if f.get(k, {}).get("value") is not None:
                v = f[k]["value"]
                break
        out.append((d["ts_ns"] / 1e9, v))
    return out


def main(sessions):
    base = load_base()
    rows_all = []        # (sess, seq, n_sel, rate, straight)
    n_norate = 0
    for sess in sessions:
        try:
            hud = [(t, v) for t, v in hud_rates(sess)]
        except FileNotFoundError:
            continue
        if not any(v is not None for _, v in hud):
            n_norate += 1
            continue
        rows, _ = analyze_series(sess, 2)
        sm = straight_mask(sess, rows)
        # 比赛态判据 = HUD stage∈{1,2}（补采验收轮改，同 linepos：staged_filter stage4
        # 是 VP 标定质量口径，雨景误杀 83%）
        from probe_gate3_linepos import hud_race_mask
        hm = hud_race_mask(sess, rows)
        race = [(r, bool(m)) for r, m, h in zip(rows, sm, hm) if h]
        if not race:
            continue
        step = max(1, len(race) // MAX_FRAMES)
        race = race[::step][:MAX_FRAMES]
        cal = get_cal(sess, [r for r, _ in race if r.get("vpx") is not None and r.get("y_h") is not None])
        y_h, vpx, ax = cal["y_h"], cal["vpx"], cal["A_x_used"]
        ts = np.array([t for t, _ in hud])
        vs = np.array([v if v is not None else np.nan for _, v in hud])
        t0 = time.perf_counter()
        for r, st in race:
            i = int(np.argmin(np.abs(ts - r["ts"])))
            rate = vs[i] if abs(ts[i] - r["ts"]) <= RATE_WIN else np.nan
            rgb = np.array(Image.open(DEMOS / sess / "frames" / r["file"]).convert("RGB"))
            n_sel = sum(1 for x in o3.classify(rgb, y_h, vpx, frontend="dash", ax=ax) if x["selected"])
            rows_all.append((sess, r["seq"], n_sel, rate, st))
        print(f"  [{sess}] {len(race)} 帧（抽样步长 {step}，用时 {time.perf_counter()-t0:.0f}s）", flush=True)
    d = np.array([(n, rt, 1.0 * st) for _, _, n, rt, st in rows_all if np.isfinite(rt)])
    if len(d) == 0:
        sys.exit("无带速度读数的比赛态帧")
    n_all = len(rows_all)
    print(f"\n== 有效帧 {len(d)}/{n_all}（无速度读数弃 {n_all-len(d)}；无 rate 场 {n_norate} 场）==")
    print("车速带        n帧  段/帧中位  ≥1段占比(运行KPI)  ≥3段占比(计量门)")
    for lo, hi in RATE_BINS:
        m = (d[:, 1] >= lo) & (d[:, 1] < hi)
        if m.sum() < 5:
            print(f"  {lo:3d}-{hi:<4d} {m.sum():5d}   （样本不足）")
            continue
        seg = d[m, 0]
        print(f"  {lo:3d}-{hi:<4d} {m.sum():5d}    {np.median(seg):5.1f}      "
              f"{(seg >= 1).mean():6.0%}             {(seg >= 3).mean():6.0%}")
    print("直道/弯道 × 速度带（≥1段占比 | ≥3段占比）：")
    for st, tag in ((1.0, "直道"), (0.0, "弯道")):
        row = f"  {tag}："
        for lo, hi in RATE_BINS:
            m = (d[:, 1] >= lo) & (d[:, 1] < hi) & (d[:, 2] == st)
            row += f" {lo}-{hi}: {(d[m,0]>=1).mean():.0%}|{(d[m,0]>=3).mean():.0%}({m.sum()})" if m.sum() >= 5 else f" {lo}-{hi}: —"
        print(row)
    out = CACHE / "yield_scan.json"
    out.write_text(json.dumps([{"sess": s, "seq": q, "n_sel": int(n),
                                "rate": None if not np.isfinite(rt) else float(rt),
                                "straight": bool(st)} for s, q, n, rt, st in rows_all],
                              ensure_ascii=False), encoding="utf-8")
    print(f"逐帧明细：{out}")


if __name__ == "__main__":
    ss = sys.argv[1:] or sorted(p.name for p in DEMOS.iterdir() if p.is_dir())
    main(ss)
