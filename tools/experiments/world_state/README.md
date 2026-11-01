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

## 结论去向

基线数值与缺口清单进 `docs/plan/world_state_persistence.md`；对象记忆与地图
保留的事实落地后，结论归 `maaracing_master/plugins/speedrush/CODE_WIKI.md`，
本目录随退役四步删除。
