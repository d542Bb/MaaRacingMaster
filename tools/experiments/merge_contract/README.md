# merge_contract — 换道结局与 blocker 基线（活跃）

status: active

> **本文是什么**：换道契约的只读基线探针。读 SpeedRush debug trace，按“到达 /
> 超时 / 车因撤销 / 空间作废”四类拆换道结局，并统计权限覆盖、计划/PD
> 一致性、线融合误差。用于给行为契约的验收提供可复算分母。
> **本文不是什么**：不是控制规则真源，不改控制；不是正式知识，输出只作过程
> 证据。正式结论按契约落地后折入 ADR / `plugins/speedrush/CODE_WIKI.md`。

## 运行

```bash
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_merge_baseline.py
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_merge_baseline.py --data <debug/speedrush 目录>
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_merge_baseline.py --runs a,b,c
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_merge_baseline.py --session <会话 token>
```

数据默认取 `%APPDATA%/MaaRacingMaster/debug/speedrush`（旧局，一文件一局），也可用
`SPEEDRUSH_DEBUG` 环境变量或 `--data` 指定。会话流（`logs/<会话>/trace/speedrush.jsonl`，
一文件多局，按 stage 标记切段）走 `--logs`/`--session`，可逗号分隔多个会话。
脚本只读，不写 trace、不碰模型。

## 口径

- 换道段 = `route.phase` 连续为 `merging` 的拍；结束拍取离开 merging 的那一拍。
- 成功只认 `verdict=finish` 且 `why=arrived`；`merge_timeout` 是独立 `timeout`
  结局，不算成功。
- **实动到达** = 到达段内 `executed_lane` 位移 ≥0.3 道。低于此的“到达”是参考道与
  实测道不同步时的空动作（车本来就在目标道带内），不是换道，单列不混入。
- `cancel/rebuild` 目标道的 blocker 优先读 `route.blk`（阶段 0 新列）；旧 trace
  回退到 `coarse.cand`。
- 时长分到达段与全部段两套，不混用；到达时长只统计实动到达。
- 计划/PD 一致性只在计划有效且机动的拍上算；PD 按 `x_target/executed/v_lat_est`
  复算（0.7/0.2，前瞻 0.25 s，上限 0.85）。

## 阶段 1 A/B（普通车不撤销）

- 只改 `decision.json [route].soft_car`（false→true），其余键、模型与路线不变；
  `route.control` 保持 true。
- 两组各 4 局（初赛/决赛沿用 p1/p2 后缀）；基线的 4 局已在
  `probe_merge_baseline.py` 的 `RUNS` 里列明，阶段 1 的新局同法加进去或走
  `--runs`/`--session`。
- 读法：`cancel` 应为 0（目标道普通车不再撤销）；到达率要同时看**实动到达**，
  空动作增长不代表换道成功；同时看新出现的 `timeout` 与擦撞时间损失。
  `space` 作废不属本阶段验收。
- 2026-10-25 结果（基线 4 局 vs soft_car=true 4 局）：原始到达 16/36 → 25/41，
  但实动到达 10/36（27.8%）→ 11/41（26.8%）；多出的 8 次到达全部落在空动作
  口径（车已在目标道带内，含原会被“车因撤销”取消的那类）。`space` 作废
  12/36 → 13/41。结论：车因撤销不是真实换道的约束，开关默认保持关闭。
- **做计划 vs PD 的执行器 A/B 前先有承诺期修正**（2026-10-25 已落库）：并线期
  选择/计划都只认路线承诺道（机会层关闭、决策 slew 绕过）。旧 4 局 897 个并线
  拍里，248 拍目标与承诺道差 >0.5 道——其中 155 拍是 slew 滞后、93 拍是目标
  来源偏离；不先修，A/B 会同时改变目标与执行器。

## 横向状态观测（2026-10-26）

- 离线结论（3 会话 12 局，`probe_lateral_state.py` / `probe_plant_cv.py`）：
  控制用的模型横速与线速相关 0.4~0.6、幅值中位约为线速 1/4，相位超前约
  0.45s；航向横速滞后 0.2~0.3s。模型与航向各半融合后，0.25s 横位预测误差
  中位降 10~20%，相位回到 0 附近。线角/线速的比值能在线自标定前向速度
  （5s 中位稳定，单拍跨 20~180 m/s）；它的绝对量级受 theta_rel 尺度影响，
  只作融合的自洽标定，不作为米制真值——固定 45 m/s 仍是量级代理。
- 实现：`planner.HeadingObserver`，开关 `decision.json [planner].heading_fuse_gain`
  （0=旧行为）。只改控制阻尼/前瞻，模型状态 `v_lat_est` 不变；trace 的
  `lat_ctrl` 暴露 v_ctrl/v_model/v_head/speed。
- A/B 口径：`motion.mode=shadow`、gain=0.5 跑 4 局，对
  `20261025_221703_1`（shadow、gain=0）用 `probe_lateral_ab.py` 对账。
  墙边占比（|x|>1.4）、压墙（>1.8）、过目标偏差、带内停留、穿越/分、
  实动到达一起看；只看到达率会被空动作骗。
- 2026-10-26 结果（gain=0.5 4 局 vs gain=0 4 局）：穿越/分 24.1→15.9、
  过目标偏差 p50 0.63→0.54、带内 33%→37%、实动到达 5/35→8/30；墙边
  21.7%→24.0%、压墙 10.8%→11.4%。横摆频率降了、墙边没降；墙边拍里
  控制器常以为已在目标带（位置估计问题），已转到 ⑥b 大新息判据修正。
- 参考帧修正后（2026-10-26 下午）4 局 vs 上一组：墙边 27.4%→17.6%
  （四局 29/30/29/21→17/23/17/13，全部下降）、压墙 13.3%→7.9%、
  带内 34%→37%、过目标偏差 p90 1.64→1.45、穿越/分 18.4→17.6、
  实动到达 7/41→8/33。墙边 |bias| max 0.6→0.2、p50 0.17~0.40→
  0.06~0.09，锁线否决 7~38 次/局；累计 vs 最初 gain=0：墙边
  21.7%→17.6%、压墙 10.8%→7.9%、穿越 24.1→17.6、带内 33%→37%。

```bash
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_lateral_state.py --session <会话>
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_lateral_ab.py \
    --baseline 20261025_221703_1 --trial <新会话>
```

## 数据与退出

- 只依赖 debug trace；不引入业务模块 import，不依赖游戏在跑。
- 阶段 1/2 的行为 A/B 稳定后，结论折入正式文档，本目录按实验区四步退役。
