status: active

# speedrush_planning — 决策供给与横位口径（C 类实验）

> **本文是什么**：决策层两条前置供给线的实验台账——自车状态/米制距离能否不靠逐帧深度算出来
> （A 线），以及现有横位决策口径与曲率意图口径的差距（B 线）；含各自探针、输入与收口状态。
> **本文不是什么**：不是规划器设计；不是规则真源（归 [`plugins/speedrush/RULES.md`](../../../maaracing_master/plugins/speedrush/RULES.md)）。
> **信源等级**：L4（过程与证据）。
> **继承**：实验规范见 [`tools/experiments/README.md`](../README.md)。

## 已收口的旧题：贪心 vs 带视野（吃币）

原题问「人类的转向选择里，有多少次要用未来 2~3 秒累计收益解释」。迭代 1 量了三条感知约束
（旧模型对币尺度盲、近场被自车车身遮住、币的可操作窗 2~4 秒，commit `d013488`）；
迭代 2 的个体配对（币轨迹预期到达 ↔ 比分跳变）在四个收集行上都贴着循环平移零分布，
没有稳定信号（commit `a020ad3`）。随后模型外的颜色计数证明「路上本来就几乎没有币」——
不是模型漏检（commit `ba3bd4b`）；浮字面额直读又把收入结构结掉：**极限动作约占算内收入一半，
30 分档（金币 / 普通超车）只占小头**（`RULES.md` §4.8，[`speedrush_scoring`](../speedrush_scoring/README.md) 结论 11）。

故「为币写规划器」的前提不成立，题目作废。原 `probe_decision_points.py` 依赖的旧 3 类检测模型
已不在仓库、脚本无法复跑，随之删除；过程与数字留在 git（`git log -- tools/experiments/speedrush_planning`）。

## 现行问题

新题是**车流中的超车时机与横位选择**，本目录尚未为它立探针；在库的是它需要的前置供给：

- **A. 自车状态与几何供给**：速度、横位、米制距离能不能不靠逐帧深度算出来；
- **B. 决策口径与机会上限**：按现有换道口径，人工选择在机会序列里排第几；换一种口径差多少。

⚠ **B 线的机会表以币/奖励为收益、车为安全惩罚**（`probe_potential_shadow.py` 的 `cars` 开关
是惩罚项）。新题下车是目标而非惩罚，现有 B 线结果不能直接当超车时机的判据；重跑前先改收益项。

## 探针清单（第二批，2026-10-21~23，全部只读回放）

**状态**：只入库、**结论未收口**——哪几条留下、哪几条随主题退役，等结论写下来再判。
输出统一落 `tools/experiments/_probe_data/`（gitignored）。

### A. 自车状态与几何供给

- `probe_3d_fusion.py` — 地面逆透视 + 车道标记跟踪出位姿，MoGe 点图按位姿累积成世界点云；
  判两件事：点云跨帧是否重合（位姿对不对）、走廊自由空间是否稳定（深度对不对）。
  产物 `metrics_full.jsonl` 是 `speedrush_geom` 的参考链。
- `probe_ego_motion.py` — 静态场景光流 + 深度点图反推自车横速与横摆角速度
  （`du ≈ fx·(v_y·dt/Z + ω·dt)`，近场不同 Z 给出不同方程，最小二乘解），与人工杆值对照，
  看能否当快回路的自车状态观测。
- `probe_flow_supply.py` — 先不估速度，只体检供数：前向-后向误差多大、点落在静态场景还是
  移动车辆、这些点上深度是否有效、像素坐标/焦距/帧时间戳能否对上。
- `probe_ground_plane.py` — 针孔模型下 `Z = K/(v − y_h)`、`K = 焦距 × 相机高`；用 Y≈相机高的
  点逐帧拟合 K：K 稳则固定 K 就能从 YOLO 框底点算米制距离，漂则 MoGe 仍需留着做慢校准。
- `probe_ground_vs_moge.py` — 同一个框底点：平面式距离 vs MoGe 点图 Z 中位，误差对比。
- `probe_triangulate.py` — 用 `probe_3d_fusion` 的位姿把墙面/护栏稀疏特征逐帧跟踪后做 DLT
  三角化，与同像素 MoGe 单帧深度对照：时序结构能否替代或补强远场深度。

### B. 决策口径与机会上限

- `probe_curvature_intent.py` — 同一批机会表、同一执行器（PD + 横移能力上限），只换决策：
  现状「目标横位 + 换道时间 `0.3+2.0·|dx|/道` + 0.3 道死区」vs「曲率意图 + 运动学可达性
  （`a_lat`/`v_lat_max`）+ 无死区、平滑放在横加速指令的变化率上」。
- `probe_potential_shadow.py` — 重放当前感知/深度/走廊链，在连续横位网格上算多目标势场并做
  消融，与人工未来 0.5 s 的横向位移对照；落盘 `potential_shadow_*.jsonl`（下游两个的输入）。
- `probe_route_density.py` — 按 `t_move=0.3+2.0·|Δx|` 的换道时间门，统计未来 HORIZON 秒内够得着
  的币/奖励期望（墙作硬约束、车作安全惩罚、中心与远场作可开关弱先验），看人工未来横位排第几。
- `probe_route_oracle.py` — 两个问题：同一 `(stage, timer)` 下不同人工局看到的机会是否一致
  （决定离线机会地图能否当 oracle）；只用当前帧可见机会的路线估计离「未来 3 秒全知」还多远。

## 复跑

各探针头部有完整用法；以 A 线主探针为例：

```bash
.venv/Scripts/python.exe tools/experiments/speedrush_planning/probe_3d_fusion.py \
    --video video_20261021_160547_p1 --start 400 --limit 60
```
