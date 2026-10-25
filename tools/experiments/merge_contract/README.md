# merge_contract — 换道结局与 blocker 基线（活跃）

status: active

> **本文是什么**：阶段 0 的只读基线探针。读 SpeedRush debug trace，按“到达 /
> 超时 / 车因撤销 / 空间作废”四类拆换道结局，并统计权限覆盖、计划/PD
> 一致性、线融合误差。用于给行为契约的验收提供可复算分母。
> **本文不是什么**：不是控制规则真源，不改控制；不是正式知识，输出只作过程
> 证据。正式结论按契约落地后折入 ADR / `plugins/speedrush/CODE_WIKI.md`。

## 运行

```bash
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_merge_baseline.py
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_merge_baseline.py --data <debug/speedrush 目录>
.venv/Scripts/python.exe tools/experiments/merge_contract/probe_merge_baseline.py --runs a,b,c
```

数据默认取 `%APPDATA%/MaaRacingMaster/debug/speedrush`，也可用
`SPEEDRUSH_DEBUG` 环境变量或 `--data` 指定。脚本只读，不写 trace、不碰模型。

## 口径

- 换道段 = `route.phase` 连续为 `merging` 的拍；结束拍取离开 merging 的那一拍。
- 成功只认 `verdict=finish` 且 `why=arrived`；`merge_timeout` 是独立 `timeout`
  结局，不算成功。
- `cancel/rebuild` 目标道的 blocker 优先读 `route.blk`（阶段 0 新列）；旧 trace
  回退到 `coarse.cand`。
- 时长分到达段与全部段两套，不混用。
- 计划/PD 一致性只在计划有效且机动的拍上算；PD 按 `x_target/executed/v_lat_est`
  复算（0.7/0.2，前瞻 0.25 s，上限 0.85）。

## 数据与退出

- 只依赖 debug trace；不引入业务模块 import，不依赖游戏在跑。
- 阶段 1/2 的行为 A/B 稳定后，结论折入正式文档，本目录按实验区四步退役。
