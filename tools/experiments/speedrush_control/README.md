# speedrush_control — 控制环实机 trace 回放标定

status: active

回答的事实：**planner 闭环增益（obs_alpha × obs_jump × k_p × k_d，及单侧路观测
是否应进闭环）能否把 13:02 局的杆饱和率/符号翻转率压下去，代价是什么。**

- 对象：`LateralPlanner`（生产码本身，C3 纯函数纪律就是为这种回放立的）。
- 标尺：实机控制 trace（用户数据目录，非本脚本产物）——脚本只消费不生产判据。
- 探针：`probe_planner_replay.py <trace.jsonl> [--grid]`。先以生产配置复现——
  **首个 CONSERVE 拍前**逐拍对齐（trace 录于 CONSERVE 旧语义，c1512bf 之后
  分叉属预期，前缀才是流重建检验）；复现不过则网格结论无效。复现过后跑变体
  网格，按 饱和率 / 翻转率 / |executed| p90 报表（指标只看模拟流自身）。
- 统计前置四问自查（平台 README）：判决量全部来自同一模拟流（同层）；翻转率
  按拍归一（次/秒，用 trace dt）；样本身份=trace 逐拍 fid；obs_jump 网格含
  生产值 1.5 作对照档（不假设其为常量真理）。

结论兑现后按平台 README 出口四步退役；定档值落 `decision.json` planner 段。
