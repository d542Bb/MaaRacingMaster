# speedrush 深度供数链路：时间口径与几何块时延（2026-10-17）

**它是什么**：本轮「先让时间对得上，再让链路变快」的预登记、改前基线与改后实测记录。
**它不是**：设计稿或结论汇总——锚点策略结论在 `docs/plan/`，这里只放本轮的测量与验收。

## 1. 改前核查（与任务前提的偏差，两条）

### 1.1 锚龄钟已经是采集钟，不是到达钟

车锚链一路都是采集时刻，自 `b385d4b`（2026-10-07 落产线）起未变：

`push(frame_ts_ns)` → `anchor_sheet_from_detections(ts_ns=frame_ts_ns)` →
`DepthAnchorSheet.ts_ns` → `t.anchor_ts_ns = sheet.ts_ns` →
`age_ms = (per.ts_ns − anchor_ts_ns)`、`dt_s = (sheet.ts_ns − anchor_ts_ns)/1e9`、
EXPIRED 判据。

数据侧（`probe_anchor_clock.py`，20261017 四局 2937 拍）：**401 个新锚 ingest 样本，
实测 age / 预期内容龄 = P50 0.95（P10 0.85 / P90 1.11）**。到达钟应≈0.1。

### 1.2 「投影残差 86~113px」是口径伪影

`probe_hold_cf` 的残差式 `|cy − (y_h + VPX·cam_h/m)|` 用 **VPX=640.7 当焦距**、
把**框心当接地点**。实际焦距是 `dgeo_focal`（P50 589.7），接地点是框底 `cy+h/2`：

| 5-10m 段（近场轨 n=71） | 残差 P50 | m − m_pix P50 |
|---|---|---|
| 原口径（VPX＋框心） | 103px | −3.72m |
| 换框底（仍用 VPX） | 73px | — |
| **焦距＋框底** | **3px** | **+0.05m** |

框心偏移两处独立印证：`cam_h_eff=(cy−y_h)·m/vpx ≈ 1.08` vs `cam_h=1.90` → 半车高
0.82m；`h·m/f ≈ 1.49m` 车高。近场残差大是 1/m 放大的几何偏置，不是陈旧度；
B1 外推反事实的 +4280% 也是拿这个偏口径量的。近场真实误差是离散度（P10 −3.1m /
P90 +4.9m，z 与框抖动），不是系统性偏大。

## 2. 改前基线（20261017 四局，2937 拍 / 893 新证据拍）

- `fid_lag` P50 = **6 帧**（分布 4:7 5:120 6:238 7:122 8:172 9:23 10:3 11:1）；采集
  25.1ms/帧（40fps），控制拍 50.1ms（20Hz）。
- `dgeo_ms` P50 = 128.0 = pre 5.0 + forward 45.1 + reconstruct 6.1 + upsize 5.1 +
  plane 26.4 + edges 实段 31.1 + grid 31.1（与 edges 并行；`stages.edges=57.5` 含 plane）。
- cap 21.7（pick 12.3 = 取帧时帧已龄 / det 9.7）· queue 21.8 · pub 32.2。
- `dgeo_age_ms` P50 = 189.1（push 起算）· 锚龄 P50 = 282ms。
- MoGe provider：**DML 生效**，forward 46.9ms（DML）vs 3053ms（CPU-only）= 65×。

**≤2 帧不可达**：仅 `cap_pick` 12.3 + forward 45.1 = 57.4ms > 50ms 预算；几何块归零
的理论地板 ≈146ms ≈ 5.8 帧。改后目标按 **≤4 帧** 重定（用户裁决 2026-10-17）。

## 3. 预登记（改代码之前写定）

| # | 指标 | 阈值 | 测量面 |
|---|---|---|---|
| 1 | `dgeo_age_ms` 起点改采集钟后的位移 | 上移 15~30ms（≈cap_ms 21.7） | trace `dgeo_age_ms` |
| 2 | 超龄丢弃率 `stale_drops/pushed` | 升幅 ≤2pp | `health()` / trace |
| 3 | `stages.plane` / `stages.edges` / `stages.grid` P50 | 各降至改前 ≤1/3 | trace `dgeo_seg.stages` |
| 4 | `dgeo_ms` P50 | 128.0 → ≤95 | trace `dgeo_ms` |
| 5 | `fid_lag` P50 | 6 → ≤4 帧 | trace `asheet.fid_lag` |
| 6 | 读数一致性（离线证据包 ≥40 帧） | sides 同判 ≥95%；左右缘车道差 P50 ≤0.05 lane；栅格近场裁决带（z3~9m、\|x\|≤5m）同判 ≥98%、blocked→drivable 漏判 ≤1% | `probe_anchor_clock.py` / 本轮离线对照 |
| 7 | speedrush 测试 | 643 passed | `pytest -k speedrush` |
| 8 | 质量门 | **不放宽任何门限**；新增可行动目标不得靠放宽门槛制造 | diff 审查 |

**否定条件（预登记）**：任一段时延降幅不足 1/3，或第 6 项任一条不达标 → 回滚该段
抽取，只保留通过的段。

## 4. 离线可行性（改代码前实测，共享 coef，10 帧证据包）

| 段 | 全幅 | 2×2 抽取 | 加速 | 一致性 |
|---|---|---|---|---|
| plane | 38.7ms | 9.2ms | 4.2× | — |
| edges | 40.5ms | 8.8ms | 4.6× | sides 10/10 同判，左右缘车道差 P50 = 0.000 lane |
| grid | 43.0ms | 13.4ms | 3.2× | 近场裁决带同判 99.4%，blocked→drivable 漏判 0.000 |

全栅格三态同判率只有 82.3%——分歧全部落在近场裁决带之外的远场稀疏格（抽取后
格内点数跌破 `GRID_MIN_PTS`）。`grid_veto` 只吃 z3~9m 带，故按裁决带口径验收。

抽取不丢测量：MoGe 原生输出 336×598，`infer_points` 上采样到 720×1280，几何块
处理的点里约 4/5 是插值冗余。

## 5. 改后实测

### 5.1 离线（`probe_geo_decimation.py`，产线同路径 observe_debug，26 帧）

| 臂 | observe_debug 端到端 P50 |
|---|---|
| GEO_DS=1（旧全幅） | 175.9ms |
| GEO_DS=2（2×2 抽取） | 113.9ms |

分段（同一台机器、同时段，机器负载偏高故绝对值高于产线 trace）：plane 51.1→11.9、
edges 实段 50.1→15.8、grid 50.1→15.0 ms。

读数一致性（预登记第 6 项）：sides 同判 26/26；左右缘车道差 P50 = 0.000 lane、
P90 = 0.004；栅格近场裁决带同判 99.6%、blocked→drivable 漏判 0.000 —— 全部达标。

**已知代价（如实记录）**：左缘在 4/26 帧与全幅不同（差 0.37~0.62 lane），逐帧跳变
8/25 vs 全幅 6/25——「最近一致边界」在车道级/护栏级两种语义间换选（`_side()`
注释记录的既有不稳定面），抽取后 x 格点数降至 1/4 使 EDGE_MATCH_M 判定的临界帧
更容易翻边。右缘 26/26 完全一致。行抽取（只减行、保列）实测更慢（18.3 vs
12.8ms）且换选帧更多（5/26），故不采用。

**回滚**：`GEO_DS = 1` 即逐位回到旧路径（带区/焦距/像素诊断在 ds=1 时恒等），
无需改其它代码。

### 5.2 实机 trace（待取）

跑一局后按下表填：`fid_lag` / `dgeo_ms` / `dgeo_seg.stages` / `dgeo_age_ms` /
`stale_drops`。口径脚本：`probe_anchor_clock.py`。

