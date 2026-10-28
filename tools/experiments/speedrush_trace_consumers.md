# speedrush 控制流水消费者盘点（探针五项依赖登记表）

> **用途**：控制流水自 2026-10-25 迁入会话目录单文件（ADR-0010）后，旧布局
> 消费者的迁移依据与验收台账。**裁定**：采用「一次性统一读取器迁移」路径——
> 79 个探针多为钉死历史局的一次性调查，逐个迁移不可行；统一读取器
> [speedrush_trace_reader.py](speedrush_trace_reader.py) 与竞速接线同批入库，
> 旧探针在下次被动用时经读取器换读新布局（历史文件不动，旧探针对旧数据
> 照常可用）。**来源**：2026-10-25 逐文件实读核对 + AST 字符串扫描交叉验证，
> 行号为当时实读；动用前按需复核。

## 盘点口径

只登记「实际打开并解析 speedrush 控制流水」的 Python 脚本（`trace*.jsonl` 或
同会话 `game_summary.jsonl`）。只读 demo 语料（frames/pads）、其它探针输出
jsonl、纯图片/模型/证据包的不登记。覆盖 `tools/experiments/` 全部 14 个
`speedrush_*` 目录，含退役实验。共 **79** 个。

## 三种旧布局

- **布局 A（逐阶段目录，51 个）**：`%APPDATA%\MaaRacingMaster\debug\speedrush\<YYYYMMDD_HHMMSS>_p<N>\trace_<同名>.jsonl`，伴 `raw/`、调试图 jpg、`hud/`；个别指向 `debug\speedrush\legacy_control_traces\`。
- **布局 B（平铺按阶段文件，19 个）**：`%APPDATA%\MaaRacingMaster\data\speedrush\control_traces\trace_<YYYYMMDD_HHMMSS>_p<N>.jsonl`，伴 `depth_debug_*\`、`badframes_*\`。
- **布局 C（实验区本地副本）**：人工局影子流水拷贝 `manual_score_20261016/p1_trace.jsonl`、`p2_trace.jsonl`（行结构同控制 trace）。

新布局：`logs/<会话>/trace/speedrush.jsonl` 单文件（阶段由行内 `stage_id`/`run_id`
表达；数据行保留全部旧列名，新增协议头 `v/mod/run_id/stage_id/seq/fid/
ts_wall_ns/ts_mono_ns`；gap 与控制行带 `type` 键可与数据行区分）。

## 公共 helper（迁移时一并处理）

| helper | 位置 | 说明 |
|---|---|---|
| `select_blackout_frames.TRACES` / `SEQ2FID` | speedrush_vision L115-116/L58 起 | 指向布局 B；`probe_s2b_falsify`（L41 导入，L182/187 使用）、`gold_blackout_score`（L38）消费 |
| `replay_plan.load_rows/scene_of` | speedrush_plan L36-46 | 逐行 json.loads + **按 fid 排序**；`intent_shadow` L30 使用；`scene` 嵌套列结构敏感 |
| `HudOcr/DEFAULT_RECTS` | speedrush_vision/probe_hud_ocr.py | HUD 横幅 OCR helper（`probe_extreme_events` L35），非 trace 加载器 |
| 布局 A 通用范式 | speedrush_crash 系 | `_resolve(session)`=debug/speedrush+`<session>/trace_*.jsonl` glob 取第一（如 probe_anchor_funnel L34-39）；或 `_load(name)`=`<root>/<name>/trace_<name>.jsonl` 精确名（如 probe_anchor_attribution L42-46）；多数另接受 `.jsonl` 直传 |

## 汇总表（79 项全量）

图例：阶段切分依赖=代码是否依赖阶段由目录/文件名表达；跨文件拼接=是否把多个流水文件的行并成一条序列。

| # | 脚本 | 用途 | 阶段依赖 | 跨文件拼接 | 迁移破坏点 |
|---|---|---|---|---|---|
| 1 | crash/probe_anchor_funnel.py | 米制距离漏斗归因 | 是 | 否 | `<sess>/trace_*.jsonl` glob 与会话目录消失 |
| 2 | crash/probe_anchor_attribution.py | 深度锚点归因 | 是 | 否 | `trace_<name>.jsonl` 文件名构造失效 |
| 3 | crash/probe_anchor_gate.py | 锚点质量门对账 | 是 | 否 | 同上 |
| 4 | crash/probe_box_clusters.py | 车框点云簇取证 | 是 | 否 | trace 文件名 + `raw/NNNN_raw.jpg` 按行号取帧约定失效 |
| 5 | crash/probe_close_coverage.py | 近距覆盖/致盲窗口 | 是 | 否 | glob 与会话目录消失 |
| 6 | crash/probe_depth_veto_replay.py | 框高否决重放 | 是 | 否 | 文件名构造失效 |
| 7 | crash/probe_hold_clock.py | UNKNOWN 保持时钟 | 是 | 否 | 同上 |
| 8 | crash/probe_horizon.py | t_ov 视界统计 | 是 | 否 | 同上 |
| 9 | crash/probe_motion_consistency.py | 运动一致性 | 是 | 否 | 同上 |
| 10 | crash/probe_nearfield_20hz.py | 20Hz 近场读法/卡顿归因 | 是 | 否 | 文件名失效；pace 三列归属上拍口径需随行内字段复核 |
| 11 | crash/probe_occ_shadow.py | 占用影子 blocked 对账 | 是 | 否 | 文件名构造失效 |
| 12 | crash/probe_occupancy_state.py | 占用状态机重放 | 是 | 否 | 同上 |
| 13 | crash/probe_presence_occupancy.py | 在场/占用验收 | 是 | 否 | 同上 |
| 14 | crash/probe_quality_gate.py | 质量门逐拍判定 | 是 | 否 | 同上 |
| 15 | crash/probe_sheet_replay.py | 单 fid 深度清单重放 | 是 | 否 | 文件名 + raw 行号取帧失效 |
| 16 | crash/probe_speed_avail.py | v/t_ov 时延漏斗 | 是 | 否 | glob 与会话目录消失 |
| 17 | crash/probe_ttc_outage.py | TTC 中断段取证 | 是 | 否 | 同上 |
| 18 | crash/probe_unknown_hold.py | UNKNOWN 保持反事实 | 是 | 否 | 同上（另支持直传 jsonl） |
| 19 | crash/viz_crash_window.py | 撞车窗口可视化 | 是 | 否 | 硬编码会话目录 + 同目录调试图 jpg 约定失效 |
| 20 | drive/probe_grid_bias.py | 三来源路心偏差体检 | 半 | 是（会话内多文件合并） | 目录 glob 失效；多文件拼接改单文件按 stage_id 分段 |
| 21 | drive/probe_trace_accept.py | 场跟随验收 | 半 | 是（会话内合并+跨会话汇总） | 同上 + fid 全局排序假设 |
| 22 | drive/probe_selection_calib.py | 选择层对照 | 否 | 否 | 输入路径换新 |
| 23 | geom/probe_ego_attitude.py | 自车姿态/界面污染 | 是 | 否 | 文件名 + `raw/NNNN_raw.jpg` 约定失效 |
| 24 | geom/probe_repeat_and_flow.py | 速度真值+点云重复性 | 是 | 否 | 同上 |
| 25 | geom/probe_plane_hold.py | 平面持拍反事实 | 是 | 否 | 同上 |
| 26 | plan/fit_plant.py | 横向 plant 标定 | 否 | 否 | 输入路径；steer_raw 等列/fid 排序口径复核 |
| 27 | plan/replay_plan.py | 计划回放对照 | 否 | 否 | 输入路径；scene 嵌套列结构（也是 helper） |
| 28 | plan/intent_shadow.py | 远场意图影子 | 否 | 否 | 输入路径（经 replay_plan.load_rows） |
| 29 | state/probe_state_est.py | 目标状态插值探针 | 是 | 否（统计池化） | 目录 glob 选样失效；改按会话目录/行内 stage_id |
| 30 | control/probe_planner_replay.py | 生产 planner 回放 | 否 | 否 | 输入路径 |
| 31 | scoring/probe_extreme_events.py | 极限超车横幅对照 | 是 | 否 | 文件名 + `raw/` 逐帧 OCR 约定失效 |
| 32 | vision/probe_anchor_clock.py | 锚点时钟对账 | 是 | 否 | 会话目录 + `SR_TRACES` 局名约定失效 |
| 33 | vision/probe_anchor_diag.py | 锚点诊断 | 是 | 否 | 硬编码会话清单 + 文件名失效 |
| 34 | vision/probe_anchor_life.py | 锚点生命周期分桶 | 是 | 否 | 同上 |
| 35 | vision/probe_anchor_z_cf.py | 锚 z 反事实 | 是 | 否 | 同上 |
| 36 | vision/probe_asheet_funnel.py | 深度清单漏斗 | 是 | 否 | 同上 |
| 37 | vision/probe_chain_latency.py | 链路时延分段 | 是 | 否 | 同上（`SR_TRACES` 批次约定） |
| 38 | vision/probe_cluster_gate.py | 簇跳变门对账 | 是 | 否 | 同上 |
| 39 | vision/probe_hold_cf.py | hold 反事实 | 是 | 否 | 同上 |
| 40 | vision/probe_hug_ab.py | R3 贴身对账 | 是 | 否 | 会话目录失效；**game_summary.jsonl 同目录约定**；外部日志按会话名墙钟配对需改 |
| 41 | vision/probe_hug_attrib.py | 贴身分归因 | 是 | 否 | 会话清单 + 文件名失效 |
| 42 | vision/probe_latency_budget.py | 时延预算对账 | 是 | 否 | 同上（`SR_TRACES`） |
| 43 | vision/probe_log_reconcile.py | 分数三渠道对账 | 是 | 否 | 会话目录失效；外部日志/会话名墙钟配对需改 |
| 44 | vision/probe_lost_pass.py | 改道丢车形态 | 是 | 否 | 会话清单 + 文件名失效 |
| 45 | vision/probe_manual_control.py | 人工对照局取数 | 是 | 否 | trace + **game_summary** + demos HUD 三件套会话目录约定失效 |
| 46 | vision/probe_percep_flicker.py | 感知抖动 | 是 | 否 | 会话清单 + 文件名失效 |
| 47 | vision/probe_r1_time.py | 一局时间结构分解 | 是 | **是（p1+p2 拼时间线）** | 跨阶段拼接改行内 stage_id 分段；绝对路径硬编码 |
| 48 | vision/probe_reach_cf.py | reach 域反事实 | 是 | 否 | 会话清单 + 文件名失效 |
| 49 | vision/probe_resid_verify.py | 锚值残差验证 | 是 | 否 | 同上 |
| 50 | vision/probe_shadow_gap_audit.py | 影子缺口审计 | 是 | 否 | 绝对路径 + 会话清单失效 |
| 51 | vision/probe_anchor_gap.py | 锚点缺席段审计 | 是 | 否 | 同上 |
| 52 | vision/probe_authority_replay.py | 单权威影子重放 | 是 | 否 | 硬编码完整 trace 路径失效 |
| 53 | vision/probe_warmup_cf.py | warmup 反事实 | 是 | 否 | 绝对路径 + 会话清单失效 |
| 54 | vision/probe_wait_ledger.py | 等待账本 | 是 | 否 | 同上 |
| 55 | vision/probe_decision_review.py | 决策行为对比 | 是 | 否 | 代理局会话目录 + 实验区 p1/p2_trace.jsonl 副本命名失效 |
| 56 | vision/probe_response_compare.py | 响应性对比 | 是 | 否 | 同上 |
| 57 | vision/montage_manual_vs_agent.py | 人工/代理蒙太奇 | 是 | 否 | 代理 trace + `raw/` + `hud/hud.jsonl`；人工副本命名 |
| 58 | vision/probe_coin_channel.py | 币渠道诊断 | 是 | 否 | 会话清单 + 文件名失效 |
| 59 | vision/probe_lane_gold.py | 车道线 gold | 是 | 否 | `--dir` 会话目录内单 trace glob + `raw/` 约定失效 |
| 60 | vision/probe_beta_timebase.py | obs_beta 时间基准 | 半 | 否 | control_traces 全扫 glob 失效；报告用文件名当会话名 |
| 61 | vision/probe_value_audit.py | 找边价值审计 | 半 | 否 | 全扫 glob + `--since` 文件名日期子串过滤失效 |
| 62 | vision/scan_corpus_strata.py | 语料分层扫描 | 是 | 否 | **从文件名正则提会话时间戳**逻辑失效 |
| 63 | vision/probe_drivable_grid.py | 可行驶栅格评测 | 是 | 否 | `depth_debug_<ts>` ↔ `trace_<ts>*` 文件名对齐 + SEQ2FID 誊录失效 |
| 64 | vision/probe_s2b_falsify.py | S2B 证伪 | 是 | 否 | helper 的 TRACES + 「剥 depth_debug_ 前缀找 trace」失效 |
| 65 | vision/probe_ab_pairs.py | A/B 局配对 | **是（强）** | **是（p1+p2 按文件名时间戳配对）** | 阶段配对逻辑整体失效，须改行内 stage_id/run_id 配对 |
| 66 | vision/probe_gate_recal.py | 质量门重标定 | 否 | 否 | `--trace` 文件名白名单 + control_traces 根失效 |
| 67 | vision/probe_side_profile.py | 被拒侧剖面重放 | 否 | 否 | control_traces 根 + 文件名 stem；`--debug` 目录对齐 |
| 68 | vision/probe_decision_ledger.py | 决策门账本分析 | 否 | 否 | 输入路径；`ledger` 列结构 |
| 69 | vision/probe_lane_counterfactual.py | 道级收益反事实 | 否 | 否 | 输入路径（调用者 glob） |
| 70 | vision/probe_corridor_replay.py | 走廊影子重放 | 否 | 否 | `--trace` 完整路径 + 证据包目录对齐 |
| 71 | vision/probe_far_direction_replay.py | 远场方向复算验收 | 否 | 否 | 输入路径 |
| 72 | vision/probe_episode_pause.py | 改道集生命周期 | 否 | 否 | 输入路径 |
| 73 | vision/probe_freshness_curve.py | freshness curve 证伪 | 否 | 否 | 输入路径 |
| 74 | vision/probe_metric_increment.py | 米制增量价值 | 否 | 否 | 输入路径 |
| 75 | vision/probe_metric_value.py | 米制决策价值 | 否 | 否 | 输入路径 |
| 76 | vision/probe_ttc_looming.py | TTC 估计器对照 | 否 | 否 | 输入路径 |
| 77 | vision/probe_v_projection.py | v_close 投影判决 | 否 | 否 | 输入路径 |
| 78 | vision/probe_lost_vanish.py | lost 终态取证 | 否 | 否 | 输入路径；会话名取自 `trace_` 前缀 stem |
| 79 | vision/manual_score_20261016/score_account.py | 人工局分数总账 | 是 | 否 | 本地副本文件名（实验区资产，可不动，注明来源布局） |

## 关键列名（按消费面聚类）

时间轴/帧：`fid`、`ts_ns`、`dt`；节拍：`pace_body/pace_sleep/pace_wake`、
`det_ms/yolo_wait_ms/yolo_run_ms/dgeo_ms/dbg_*`；决策：`state/reason/x_target/
steer_norm/steer_raw/steer_x/executed_lane/v_lat_est/goal/exec_branch/sel_*`；
供数：`road_offset/ro_off/ro_grid/ro_source/ro_w/ro_pair_w/ro_reject/
dgeo_*/cor_*/lane_obs/lane_fuse/lateral_frame`；对象面：`dist/car_h/car_pos/
mcars/car_views/passes/cgroups/n_groups/best_*/occ/coarse/sel_dbg`；旧局回退列：
`wlc_c|wc_c`、`cr_coins/cr_best/cr_scores`（新局已由 route/scene 系替代）；
局汇总：`ticks/dur_s/score/rate/conserve_segments`。**全部旧列名在新格式中
逐字保留**（新增协议头键不覆盖旧键），键式读取天然兼容；唯一硬断点是
**文件定位/阶段切分/伴生产物目录约定**。

## 迁移面小结

1. 布局 A 的 51 个全部依赖「会话目录=阶段、文件名=会话名」；其中 7 个还按行号
   读 `raw/` 帧（#4/15/23/24/25/31/59），2 个读会话目录调试图或 `hud/hud.jsonl`
   （#19/57）——这些的**帧证据仍留在 debug/speedrush/**（本次只迁流水），
   新局需按 stage 时间窗桥接对齐。
2. 布局 B 的 19 个依赖平铺文件名内嵌 `_pN`；3 个从文件名正则提会话时间戳
   （#62/63/64 经目录名对齐）。
3. 跨文件拼时间线的 4 个（#20/21/47/65）是行内 `stage_id/run_id` 的第一批
   验证消费者。
4. `game_summary.jsonl` 消费者 2 个（#40/45）——局汇总迁会话目录
   `trace/speedrush_summary.jsonl` 后，这两个改读新路径（列结构未变）。
5. 路径外置、无阶段假设的 16 个（#22/26/27/28/30/66~78）只需换输入路径；
   `fid` 全局排序/`steer_raw` 等列语义在格式切换时复核（新格式 fid 语义不变）。

## 裁定与落地

- **不迁移旧探针本体**：历史文件不动，旧探针对旧数据照常可用（实验区纪律：
  探针钉的是当时的会话，证据随 commit）。
- **统一读取器**：`tools/experiments/speedrush_trace_reader.py`——枚举三种旧
  布局 + 新布局、只产出数据行（跳过协议行）、按 stage 切分/过滤、p1/p2 配对、
  新布局行内 run_id/stage_id ↔ 旧布局目录名对齐。新探针一律经读取器取数；
  旧探针下次动用时换读取器入口。
- **回放验证**：读取器对真实旧布局文件与新布局产物各做一次等价性冒烟
  （同局同行同列）；证据 commit `276d8ce`→`ce1541a`→`7cbc3e8`（2026-10-25 记录流落地）。
