# -*- coding: utf-8 -*-
"""freshness curve 离线证伪探针（实验 A，stdout-only，2026-10-08 挂账）。

回答一个问题：深度边界读数的年龄（age）在多大 ego motion 下、经运动外推
补偿后还剩多大误差——即 `depth age × ego motion × boundary error` 传递函
数的第一版。曲线是后续"要不要动 148ms compute"的裁判（设计稿 §4A 挂账
三更；GPT 反审计轮护栏已并入）。

构造（不是裸 age×error）：
  对每对相邻新鲜 dgeo 边界读数 (t0, t1)（同侧、双侧读数非空）：
    baseline 误差 = |x(t1) − x(t0)|                 （不补偿，直接用旧值）
    candidate 误差 = |x(t1) − (x(t0) − v_lat·Δt)|   （一阶横移外推）
  v_lat_est 单位=车道/s（planner 同款，产线前瞻补偿同形式），外推方向：
  自车右移(v_lat>0)时右边界(正值)读数减小。
  identity 守门：|x(t1)−x(t0)|>2.0 道的对子剔除并计数（疑似换对象——
  影子 bump 拒绝后真墙接棒等产线语义，不算外推误差）。

  已知限制（如实声明）：
  • Δt 由深度产出间隔决定（~186ms 为主），<150ms 桶样本薄——低 age 段
    覆盖不足，结论以 150~600ms 段为准；
  • 外推只含横移一阶项，弯道 yaw/前进分量未建模（用 |steer_norm| 分桶
    观察恶化，属代理非测量）；
  • 观测噪声同时进入两列——低运动桶即噪声底。

用法：.venv python probe_freshness_curve.py <trace.jsonl> [...]
"""
import json
import sys
from collections import defaultdict

DT_BUCKETS = [(0, 150), (150, 250), (250, 400), (400, 10_000)]
VL_BUCKETS = [(0.0, 0.3), (0.3, 1.0), (1.0, 2.0), (2.0, 100.0)]
STEER_BUCKETS = [(0.0, 0.1), (0.1, 0.3), (0.3, 10.0)]
IDENTITY_JUMP = 2.0   # 道；超过即疑换对象，剔除

# 侧别 → (trace 字段名, 外推符号)：变道段级实证（2026-10-08）——车右移
# (v_lat>0) 时左边界相对车右靠（Δx=+∫v，读数增大）、右边界相对车左近
# （Δx=−∫v，读数减小），两侧反号；跟随系数 ≈0.29（读数为混合参照系）。
# pred = x0 − sgn·v_lat·Δt：L 用 −1（预测增大）、R 用 +1（预测减小）。
SIDES = {"L": ("dgeo_left", -1.0), "R": ("dgeo_right", +1.0)}


def pctl(xs, q):
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def bucket_of(v, buckets):
    for lo, hi in buckets:
        if lo <= v < hi:
            return (lo, hi)
    return None


def bname(b):
    return f"[{b[0]:g},{b[1]:g})" if b else "?"


def main(paths):
    pairs = []          # (dt_ms, vlat, steer, base_err, comp_err, side)
    dropped = 0
    kept = 0
    for path in paths:
        rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        for side, (field, sgn) in SIDES.items():
            seq = []    # (ts_s, x, v_lat, |steer|)
            for r in rows:
                x = r.get(field)
                if x is None:
                    continue
                # 只取新鲜实例拍：dgeo_left/right 是消费面字段，驻留复用拍
                # 带同一读数连续出现——不按 dgeo_new 过滤，对子会混入大量
                # Δx=0 的同观测自我复制（2026-10-08 首跑实锤，Δt P50 虚低到 50ms）
                if not r.get("dgeo_new"):
                    continue
                vl = r.get("v_lat_est")
                st = r.get("steer_norm")
                seq.append((r["ts_ns"] / 1e9, float(x),
                            0.0 if vl is None else float(vl),
                            0.0 if st is None else abs(float(st))))
            for a, b in zip(seq, seq[1:]):
                dt_s = b[0] - a[0]
                if dt_s <= 0 or dt_s > 2.0:
                    continue
                jump = abs(b[1] - a[1])
                if jump > IDENTITY_JUMP:
                    dropped += 1
                    continue
                kept += 1
                vlat = (a[2] + b[2]) / 2.0
                pred = a[1] - sgn * vlat * dt_s   # 一阶横移外推
                pairs.append((dt_s * 1000.0, abs(vlat), max(a[3], b[3]),
                              jump, abs(b[1] - pred), side))

    print(f"对子 n={kept}（identity 剔除 {dropped}，阈 |Δx|>{IDENTITY_JUMP:g} 道）"
          f"  覆盖 trace {len(paths)} 个")
    dts = [p[0] for p in pairs]
    if dts:
        print(f"Δt 分布: P10={pctl(dts, .1):.0f} P50={pctl(dts, .5):.0f} "
              f"P90={pctl(dts, .9):.0f} ms")
    vls = [p[1] for p in pairs]
    if vls:
        print(f"|v_lat| 分布: P50={pctl(vls, .5):.2f} P90={pctl(vls, .9):.2f} "
              f"max={max(vls):.2f} 道/s")

    def report(title, key_idx, buckets, dt_buckets):
        print(f"\n[{title}]  （每格: n / base P50 / base P90 / comp P50 / comp P90，单位=道）")
        for vb in buckets:
            cells = []
            for db in dt_buckets:
                sel = [p for p in pairs
                       if bucket_of(p[key_idx], [vb]) and bucket_of(p[0], [db])]
                if not sel:
                    cells.append(f"{bname(db)}:  —")
                    continue
                be = [p[3] for p in sel]
                ce = [p[4] for p in sel]
                cells.append(f"{bname(db)}: n={len(sel)} "
                             f"{pctl(be, .5):.2f}/{pctl(be, .9):.2f} → "
                             f"{pctl(ce, .5):.2f}/{pctl(ce, .9):.2f}")
            print(f"  |v|{bname(vb)}: " + "  |  ".join(cells))

    # [A] 低运动桶按 Δt：慢变量假设检验 + 噪声底
    report("A 低运动 |v_lat|<0.3 道/s", 1, [(0.0, 0.3)], DT_BUCKETS)
    # [B] 横移 × Δt 矩阵
    report("B 横移分桶", 1, VL_BUCKETS, DT_BUCKETS)
    # [C] 转向代理 × Δt（弯道恶化观察）
    print("\n[C] 转向代理分桶（steer_norm 为代理，非曲率测量）")
    for sb in STEER_BUCKETS:
        sel = [p for p in pairs if bucket_of(p[2], [sb])]
        if not sel:
            continue
        be = [p[3] for p in sel]
        ce = [p[4] for p in sel]
        hi = [p for p in sel if p[0] >= 250]
        hi_be = [p[3] for p in hi]
        hi_ce = [p[4] for p in hi]
        print(f"  |steer|{bname(sb)}: n={len(sel)}  全段 base {pctl(be, .5):.2f}/"
              f"{pctl(be, .9):.2f} comp {pctl(ce, .5):.2f}/{pctl(ce, .9):.2f}"
              + (f"   Δt≥250ms: base {pctl(hi_be, .5):.2f}/{pctl(hi_be, .9):.2f}"
                 f" comp {pctl(hi_ce, .5):.2f}/{pctl(hi_ce, .9):.2f}" if hi else ""))

    # [D] 汇总 + 补偿改善面
    if pairs:
        be = [p[3] for p in pairs]
        ce = [p[4] for p in pairs]
        better = sum(1 for p in pairs if p[4] < p[3] - 1e-6)
        worse = sum(1 for p in pairs if p[4] > p[3] + 1e-6)
        print(f"\n[D] 全对子: base P50/P90/P95 = {pctl(be, .5):.2f}/"
              f"{pctl(be, .9):.2f}/{pctl(be, .95):.2f} 道")
        print(f"    comp P50/P90/P95 = {pctl(ce, .5):.2f}/"
              f"{pctl(ce, .9):.2f}/{pctl(ce, .95):.2f} 道")
        print(f"    补偿改善 {better}/{len(pairs)}（{better/len(pairs)*100:.0f}%）、"
              f"恶化 {worse}（{worse/len(pairs)*100:.0f}%）")
        hi = [p for p in pairs if p[1] >= 1.0]
        if hi:
            hb = [p[3] for p in hi]
            hc = [p[4] for p in hi]
            print(f"    高横移 |v_lat|≥1 道/s 子集 n={len(hi)}: base "
                  f"{pctl(hb, .5):.2f}/{pctl(hb, .9):.2f} → comp "
                  f"{pctl(hc, .5):.2f}/{pctl(hc, .9):.2f} 道")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
