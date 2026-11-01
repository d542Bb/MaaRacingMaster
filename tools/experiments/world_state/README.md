status: active

# world_state — 世界状态与分级规划的基线与回放

## 问题

极速狂飙的规划每拍从零重建，对象在计划前被硬删，机会层用固定横距门截断
光锥。改这三件事之前要先有一把**可比、可复算**的尺子：同一批 trace 上，
计划有效率、层切换、请求→并线、币组可见寿命与提前量、对象在遮挡后的保留
时长、杆值反向与过冲各是多少。

本实验只读 trace，不改任何生产行为。`baseline_replay.py` 是这份尺子的实现；
它同时接受旧列（无身份/锥余量）与新列（schema 4 之后）——旧局缺的字段如实
标 `null` 并在 `missing` 里列出，不用猜数补齐。

## 口径

- 时间轴一律用行内 `ts_ns`，段切分遇 >0.5 s 空档即断开（不看 fid 差：fid
  会因丢帧跳号，`dt` 会因卡顿抖动）。
- **光锥余量** = 到站时间 −（横移时间 + 反应 + 余量）。新局直接读
  `sel_dbg.opp.*[][1]`；旧局按同一算式在脚本内复算（常量与当时策略一致，
  见 `_lane_move_time` 注释），报告里标 `slack_source`。
- **并线速度** = 段内横移位移 / 段时长；**过冲** = 越过承诺道中心后继续
  多走的最大距离（没走到目标记 0，另报 `settle_err` = 收尾时离目标多远）。
- **杆值反向率** = 忽略 |杆值|<0.05 后的符号翻面次数 / 总时长。
- **对象保留** = 某个 `(kind, id)` 在 `seen=False` 出现后，直到从场景消失
  的持续时长；只在 schema 4 起可用。
- 这只是**逐拍决策面**的回放：录像里自车位置与可见目标是既成事实，策略一改
  后续轨迹就不可比。请求数、命中、杆值的变化只能当决策差异，不能当实车结果。

## 用法

```bat
.venv\Scripts\python.exe tools\experiments\world_state\baseline_replay.py ^
    --session 20261101_113145 --json out\baseline_113145.json
```

`--trace <path>` 可直接给任意 `speedrush.jsonl`。

```bat
.venv\Scripts\python.exe tools\experiments\world_state\map_support.py ^
    --session <新局> --json out\map_support.json
```

`map_support.py` 用 `scene` 带与 `v_ego/v_lat` 复算「预测距离 → 中心误差」与
车道级支撑距离。**新局才有精确 `v_ego`**；旧局可用 `--speed hud41`，从 HUD
得分速度拆巡航底值（<45 取读数、否则按 41 m/s，得分加 30k 的代理），报告会
标注为代理；`--speed const --speed-value 35/45` 做速度敏感性对照。

```bat
.venv\Scripts\python.exe tools\experiments\world_statecceptance.py ^
    --baseline 20261101_113145 --new <局1> --new <局2>
```

`acceptance.py` 复用上面两个分析器，把新局与基线的计划/层/并线/请求/杆值/
地图支撑逐项并排；只列差值，不设通过阈值——判据在方案第 5 步。旧局缺
`v_ego/otab/map` 的项显示 None，不拿缺列当零；旧局对照可加
`--speed hud41` 用得分速度底值代理跑地图误差。

```bat
.venv\Scripts\python.exe tools\experiments\world_state
equest_cone.py ^
    --session 20261101_113145
```

`request_cone.py` 用旧局 `sel_dbg` 反事实复算「旧 1.2/1.5 道固定门挡住、但
时间锥+相邻请求会发」的机会。它**不覆盖**长币线远端时间与命中拍请求贯通
（旧局缺组账/命中上下文），`cone_only` 未计层优先级与确认门，是上界。

```bat
.venv\Scripts\python.exe tools\experiments\world_state\horizon_sensitivity.py ^
    --session 20261101_113145
```

`horizon_sensitivity.py` 复算同一批选择帧在 2.0/2.5/3.0 s 固定视界下的请求
机会，再按旧局走廊宽/安全车近似开度给出拟议视界，统计相对 3.0 s 砍掉多少。
只是机会集合代理，无闭环反馈。

## 结论去向

基线数值与缺口清单进 `docs/plan/world_state_persistence.md`；对象记忆与地图
保留的事实落地后，结论归 `maaracing_master/plugins/speedrush/CODE_WIKI.md`，
本目录随退役四步删除。
