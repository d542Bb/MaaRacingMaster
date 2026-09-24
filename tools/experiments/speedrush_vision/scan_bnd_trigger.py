"""兜底通道真实触发率：翻全部带 bnd_* 的 trace，按生产判据统计「黄线彻底失效」（2026-09-24）。

**为什么有这个文件**：region 无锚兜底的需求规模此前只有离线判据读数——弯道段
113809_p2 的 verify 口径「L 侧 64/91 帧无黄线」（70%）。那是**帧×侧**的离线 HSV
判据，比生产侧严：生产闭环（`_EgoRoadObserver`，module.py）在单侧黄线 + 半宽记忆
下照常产出 road_offset，只有真正无燃料才给 None。兜底通道（region 连通块 +
射线角守卫）的真实触发率必须用生产判据翻全部 trace 才能定——它决定守卫/跨帧
验证是主路径还是边角路径。

**判据真源（不另造，全部读 trace 已有列）**：
- 口径 A「边界层全盲」：bnd_sides==0 ∨ bnd_valid is not True——boundary.py 一票否决。
- 口径 B「闭环真失燃料」：road_offset is None——module.py `_EgoRoadObserver` 最终
  输出，含 A + 单侧无半宽记忆 + 路宽对不物理（HW∉[1.5,3.2] 整帧弃）+ 越物理界。
  **这是兜底通道的设计触发条件**（A 架构：位置由黄线给，None 才轮到深度）。
- 口径 C「单侧在场」：bnd_sides==1——黄线层仍给一侧位置（深度可补另一侧），
  不触发兜底；这是离线 70% 读数的主要构成（帧×侧把单侧在场全算成"缺"）。

**版本批（git 时间线硬对齐，不可省）**：trace 列的语义随 09-22 当天四次提交漂移——
`road_offset` 列本身 6ce5011（19:39）才存在；单侧反推 84aecb4（20:02）；双护栏
008347d（20:54，垃圾对从"给毒值"变"整帧弃"，B 因此抬高）；v2 几何选择器
dfc217e（21:07）。**只有 b4 两场反映当前生产语义**；b1 四场无 road_offset 列，
B 口径对它们恒 True（首跑曾把缺列误读成"整场失效"，此即分批的原因）。

| 批 | 场次时段 | 运行时代码 |
|---|---|---|
| b1 | 18:41~18:51（4 场） | 17c741a 后：有 sides；**无 road_offset 列** |
| b2 | 19:42~19:54（4 场） | step 2.5：road_offset 仅双侧；**单侧恒 None** |
| b3 | 20:18~20:46（6 场） | 2.5b：单侧反推有；**无护栏**（垃圾对计入可用，B 偏低） |
| b4 | 21:10~21:11（2 场） | **当前语义**：v2 选择器 + 双护栏 + 单侧反推 |

**连续段**：trace 行 fid 步长中位 ≈2（20fps 录制 ⇒ 1 行 ≈ 0.1s）。≥2s 的长段才是
跨帧验证/守卫的设计场景；孤行用 EMA/记忆即可桥接。

用法（仓库根，.venv Python）：
    python tools/experiments/speedrush_vision/scan_bnd_trigger.py
"""

from __future__ import annotations

import numpy as np

from scan_corpus_strata import load, runs

# 场次名前缀（trace_ 去掉）→ 版本批；依据见模块 docstring 的 git 时间线
BATCH_OF = {
    "20260922_184114_p1": "b1", "20260922_184158_p2": "b1",
    "20260922_185055_p1": "b1", "20260922_185140_p2": "b1",
    "20260922_194235_p1": "b2", "20260922_194319_p2": "b2",
    "20260922_195318_p1": "b2", "20260922_195403_p2": "b2",
    "20260922_201847_p1": "b3", "20260922_201932_p2": "b3",
    "20260922_203259_p1": "b3", "20260922_203343_p2": "b3",
    "20260922_204519_p1": "b3", "20260922_204603_p2": "b3",
    "20260922_211035_p1": "b4", "20260922_211120_p2": "b4",
}


def _has(x) -> bool:
    return x is not None and x == x


def main() -> None:
    sess = load()
    rows_all = [(s, r) for s, v in sorted(sess.items()) for r in v]
    n = len(rows_all)
    steps = {s: float(np.median(np.diff([r["fid"] for r in v])))
             for s, v in sess.items() if len(v) > 10}
    step = float(np.median(list(steps.values())))
    print(f"== 兜底通道真实触发率（{len(sess)} 场、{n} 行 trace、"
          f"1 行 ≈ {step / 20:.2f}s）==\n")

    def A(r):  # 边界层全盲
        return r.get("bnd_valid") is not True or r.get("bnd_sides") == 0

    def B(r):  # 闭环真失燃料（生产判据；仅对有 road_offset 列的场有意义）
        return "road_offset" in r and r.get("road_offset") is None

    def C(r):  # 单侧在场
        return r.get("bnd_sides") == 1

    print("—— 分版本批（b4 = 当前语义，主读数）——")
    for batch in ["b1", "b2", "b3", "b4"]:
        rows = [(s, r) for s, r in rows_all if BATCH_OF[s] == batch]
        m = len(rows)
        ca = sum(1 for _, r in rows if A(r))
        cb = sum(1 for _, r in rows if B(r))
        cc = sum(1 for _, r in rows if C(r))
        # B∧sides 拆解：s2 被弃=护栏拦下的垃圾对；s1 为 None=无半宽记忆或单侧越界
        b1_ = sum(1 for _, r in rows if B(r) and C(r))
        b2_ = sum(1 for _, r in rows if B(r) and r.get("bnd_sides") == 2)
        tag = "  ← 当前生产语义" if batch == "b4" else ""
        print(f"{batch}  {m:5d} 行   A {ca * 100 / m:5.1f}%   B {cb * 100 / m:5.1f}%"
              f"（s1 {b1_} / s2 {b2_}）   C {cc * 100 / m:5.1f}%{tag}")
    print("（b1 无 road_offset 列，B 恒 False 不计；A/C 仍有效）\n")

    print("—— b4 每场 breakdown ——")
    for s in sorted(sess):
        if BATCH_OF[s] != "b4":
            continue
        v = sess[s]
        m = len(v)
        print(f"  {s}  {m} 行  A {sum(1 for r in v if A(r)) * 100 / m:.1f}%  "
              f"B {sum(1 for r in v if B(r)) * 100 / m:.1f}%  "
              f"C {sum(1 for r in v if C(r)) * 100 / m:.1f}%")

    # 连续段：B 口径，仅 b4（其余批语义不同不可混）；≥1s 段起步
    print("\n—— B 口径连续段（仅 b4；≥1s 段）——")
    segs = [(s, a, b, ln) for s, v in sess.items() if BATCH_OF[s] == "b4"
            for a, b, ln in runs(v, B, min_rows=10)]
    segs.sort(key=lambda x: -x[3])
    total_rows = sum(ln for _, _, _, ln in segs)
    n4 = sum(len(sess[s]) for s in sess if BATCH_OF[s] == "b4")
    print(f"≥1s 段 {len(segs)} 个、共 {total_rows} 行（≈{total_rows * step / 20:.0f}s，"
          f"占 b4 trace {total_rows / n4:.1%}）；≥2s 段 {sum(1 for *_x, ln in segs if ln >= 20)} 个")
    for s, a, b, ln in segs:
        print(f"  demos/{s}/frames  fid {a}~{b}（{ln} 行 ≈ {ln * step / 20:.1f}s）")

    # 弯道分层（b4）：验证「兜底触发集中在弯道内侧」
    rows4 = [(s, r) for s, r in rows_all if BATCH_OF[s] == "b4"]
    curve = [(s, r) for s, r in rows4 if abs(r.get("steer_norm") or 0) > 0.35]
    nc_b = sum(1 for _, r in curve if B(r))
    st_b = sum(1 for _, r in rows4
               if abs(r.get("steer_norm") or 0) <= 0.35 and B(r))
    print(f"\n—— b4 弯道分层（|steer_norm|>0.35）——\n弯道 {len(curve)} 行中 B {nc_b}"
          f"（{nc_b / max(len(curve), 1):.1%}）；非弯道 {len(rows4) - len(curve)} 行中 B "
          f"{st_b}（{st_b / max(len(rows4) - len(curve), 1):.1%}）")

    # 单侧在场时哪侧有线（仅 b4——其 sides==1 分支才对齐当前代码）
    l_only = sum(1 for _, r in rows4 if C(r) and _has(r.get("bnd_left_x"))
                 and not _has(r.get("bnd_right_x")))
    r_only = sum(1 for _, r in rows4 if C(r) and _has(r.get("bnd_right_x"))
                 and not _has(r.get("bnd_left_x")))
    both_x = sum(1 for _, r in rows4 if C(r) and _has(r.get("bnd_left_x"))
                 and _has(r.get("bnd_right_x")))
    print(f"\n—— b4 sides==1 侧别 ——\n仅左 {l_only}、仅右 {r_only}、"
          f"两侧 x 均有值 {both_x}（若 both 占多，说明 sides 字段与 x 有效性不同源，需回查）")


if __name__ == "__main__":
    main()
