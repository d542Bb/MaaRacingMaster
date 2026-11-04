# 极速狂飙深度推理管线性能归因（2026-10-04）

> **本文是什么**：对「深度 worker 耗时 p50 130~153ms」的一次**只读性能归因**——把耗时拆到
> 段、把瓶颈钉到证据、把改进选项排成表。测量对象=2026-10-04 三场六阶段实机落盘产物
> （证据包 / trace / 日志）+ 产线同路径的离线探针。
> **本文不是什么**：不是改法（未改任何产线代码、未提交 git）；不是最终结论——凡「推断」都
> 标了出处与缺什么，凡「测得」都给了文件/探针。
> **信源等级**：L4（过程与证据）。
> **硬边界**：本机当前**未跑游戏**，所有 GPU 侧绝对值为「无游戏并发」口径；与游戏并发需实机复核。

## 0. 结论速览

1. **耗时构成（测得，同一进程内顺序计时）**：observe 五段中，
   **点云后处理（上采样 + 3D 找边）≈ 58%**，**MoGe DML 推理 ≈ 30%**，
   preprocess+reconstruct ≈ 12%。「推理是主耗时」的印象不成立——**后处理才是最大单项**。
2. **推理本体不是 GPU 算力瓶颈**（测得）：MoGe q4f16 整图被 DML 融成**单节点**
   （`DmlFusedNode`，executor 0.3ms/次），而 `model_run` 52~55ms —— 时间不在图执行里，
   而在 DML EP 每帧的 I/O 与 CPU↔GPU 同步里（输入上传 ~7.7ms；输出读回+同步 ~57ms）。
   且**减输出体积无效**（去掉未使用的 `normal` 2.41MB：53.3→54.5ms）→ 同步主导，非带宽。
3. **超龄丢弃率随 worker 耗时单调上升**（测得）：dur p50 130ms→丢弃 29~30%；
   dur p50 148~153ms→丢弃 50~51%。机制=**产出间隔 ≈ 节流窗 70ms + observe 耗时**，
   ≈200~235ms，逼近/超过 350ms age 闸。**与 DML 锁让路次数不相关**。
4. **锁让路（busy_skips 18~53/局）不是丢弃率的原因**，但它把**控制拍感知 p95 抬到 59~62ms**
   （≈ 深度 run 的持锁时长），超感知 33ms 预算。
5. **graph capture 的「30x」是假象**（测得）：捕获图**冻结输入**，重放不随帧更新
   （12 帧中仅第 1 帧正确）→ 该路线不可用。`enable_cpu_sync_spinning` 无收益。

---

## 1. 耗时构成分解

### 1.1 分解表

**测量方法**：`probe_moge_stage_decompose.py` 在同一进程内、用**产线同一路径**
（`depth_geo.load_session` + `moge_post.preprocess/forward/reconstruct` + `reading_from_points`）
对 `demos/manual_20261003_215150_p1` 的 50 帧（stride=12）逐段计时；两次独立复跑值稳定
（合计 204.9 / 204.4ms）。后处理两段另有**独立方法**复核（证据包重放，见 §1.2）。

| 段 | p50 (ms) | 占比 | 与产线函数的对应 | 方法 |
|---|---|---|---|---|
| preprocess（RGB→720×1280 fp32 NCHW） | 12.1 | 5.9% | `moge_post.preprocess` | 探针同进程计时 |
| **forward（DML run + 输出读回）** | **61.1** | **29.9%** | `moge_post.forward` | 同上 |
| reconstruct（`recover_focal_shift` + force_projection + ×metric_scale） | 13.4 | 6.6% | `moge_post.reconstruct` | 同上 |
| **上采样**（336×598 原生点图 → 720×1280 全幅 + nan 掩码） | **26.9** | **13.1%** | `infer_points` 里的 `cv2.resize` 段 | 同上；证据包重放独立复核 31.6（441 帧）/ 27.1（141244 子集 70 帧） |
| **reading_from_points**（平面拟合 + 列剖面 + 3D 找边） | **92.2** | **45.1%** | `reading_from_points` | 同上；证据包重放独立复核 94.0（441 帧）/ 92.8（141244 子集） |
| **合计** | **204.4** | 100% | `DepthRoadObserver.observe_debug` | 端到端复算 ~202（同探针另一次复跑） |
| 　其中 **后处理（上采样+找边）** | **119.0** | **58.2%** | — | — |

**生产口径对照**：六阶段日志/`trace` 的 `dgeo_ms`（= `reading.latency_ms` = 整段 observe）
p50 **130~153ms**、p95 139~170ms。离线合计 204.4ms 是生产的 **1.35~1.57×**——即
**本次离线测量的机器状态比实机慢**（当前 GPU 与 MuMu/壁纸引擎等共享、CPU 状态不同；
离线 forward 61ms vs 代码注释实测 ~48ms，同向）。故**绝对值以生产日志为准，比例以离线分解为准**。

**生产口径的段估算（推断，按同一比例缩放）**——总 130~153ms 时：

| 段 | 估算 (ms) | 说明 |
|---|---|---|
| forward | 39~46 | 唯一 GPU 侧段；若生产时 CPU 更快则其占比更高 |
| reading_from_points | 59~69 | 最大单项 |
| 上采样 | 17~20 | |
| reconstruct | 8.6~10 | |
| preprocess | 7.7~9 | |

> 标尺独立性：分解用**产线函数本体**计时，未用待验证实现生成判据；后处理两段有
> 「同进程计时」与「证据包离线重放」两条互相独立的路径，互相印证到 0.3~0.7ms。

### 1.2 后处理两段的独立复核（证据包离线重放）

`probe_depth_pipeline_attribution.py` 读 `control_traces/depth_debug_*` 的 `d*_evid.npz`
（原生 336×598 点图 fp16 + valid + 归一化焦距），逐位重放产线口径（口径见 `depth_geo` 模块
docstring「离线复算」）：

- **441 帧 / 9 目录**：组装（拷贝/缩放）p50 **31.6ms**、`reading_from_points` p50 **94.0ms**、
  合计 p50 **125.1ms**；逐目录合计 p50 稳定在 **117~129ms**。
- 单目录复跑（141244，70 帧，连跑两次）：组装 27.1/27.2、找边 92.8/93.0，**复跑一致 ±1.2ms**。
- 与 §1.1 的同进程计时（上采样 26.9 / 找边 92.2）**吻合**（差异来自帧集与机器状态，同量级）。

### 1.3 `reading_from_points` 内部（cProfile，20 帧）

| 子段 | p50 (ms/帧) | 占本函数 | 备注 |
|---|---|---|---|
| `_scan_side`（3D 分箱找边，10 次/帧） | 39.0 | 42% | 每次 96 个 x 格循环 + 逐格 `np.median` |
| `_fit_road_plane`（含列剖面 + lstsq） | 34.8 | 37% | 其中 `_road_by_column_profile` 12.0、`lstsq` 9.8（6 次/帧）、种子拟合 4.6 |
| 其余（hgt/sky/掩码/分箱/侧别合成） | ~20 | 21% | |

`_road_by_column_profile` 的 12.0ms 与 `depth_geo` 模块 docstring 记的「~10ms/帧」同量级（互证）。

### 1.4 forward 内部（ORT profiler + A/B）

| 量 | 值 | 方法 |
|---|---|---|
| 图结构 | **单节点** `DmlFusedNode_0_11`（provider=DML） | `enable_profiling` |
| executor / 节点 kernel 时间 | **0.3ms/次** | 同上 |
| `model_run` 整段 | **51.7~54.6ms** | 同上 |
| 输入上传（11.06MB） | ~7.7ms | `probe_dml_orchestration` `in_dml` A/B |
| 输出读回 + 同步 | ~57ms | 同上 `out_dml` A/B（输出绑 dml 时 wall 63.7→7.0ms） |
| 输出张量 | points 2.41 + normal 2.41 + mask 0.80 + scale 0 = **5.63MB/帧** | `sess.get_outputs()` |
| **去掉未使用的 normal 输出** | **无收益**（53.3→54.5ms） | 自测（见 §3 选项 E） |
| graph capture | 表面 63.6→4.3ms，**实测假象** | `probe_dml_orchestration` + 自测逐帧比对 |
| `enable_cpu_sync_spinning` | 63.9 vs 63.7ms，无收益 | `probe_dml_orchestration` |

**判读**：`model_run` 52~55ms 减去节点执行 0.3ms、减去输入上传 7.7ms，余 ~45ms 落在
**输出读回/同步**段；而删掉 43% 的读回体积毫无改善 → 该段是**同步等待**而非拷贝带宽。
「GPU 算力不够 / DML 算子低效」在本口径下**不成立**（整图单节点、executor 0.3ms）。

---

## 2. 瓶颈归因

### 2.1 主因（测得）

**主因是 CPU 侧后处理，其次是 GPU 侧的 DML I/O/同步；两者合计 >85%，且都不在「模型算力」上。**

证据链：

1. 后处理两段（上采样 13.1% + 找边 45.1%）= **58.2%**，两条独立方法互证（§1.1/§1.2）。
   找边内部是**纯 Python 逐格循环 + 逐格 median**（§1.3）——典型 CPU 密集、无 GPU 参与。
2. forward 段（29.9%）**不是算力瓶颈**：整图单 DML 节点、executor 0.3ms（§1.4）。
3. 锁让路（busy_skips）**与丢弃率不相关**（§2.3），说明它不在「耗时」这条因果链上。

### 2.2 推理侧：为什么 55ms 而不是 5ms？（部分证据不足）

- 测得：`model_run` 52~55ms，节点执行 0.3ms，输入上传 7.7ms，输出读回+同步 ~57ms（A/B）。
- 测得：**删输出体积无效** → 同步主导。
- **证据不足**：无法判定这 ~45ms 是「DML EP 每帧设备侧准备/同步」还是「GPU 实际执行时间
  （profiler 的 kernel_time 未覆盖异步执行）」。
  **缺什么**：需要 GPU 时间线工具（PresentMon / GPUView / Nsight Systems）或与游戏并发的
  实机 A/B；本机 profiler 只给到 CPU 侧调用时长。**该段必须实机复核。**

### 2.3 波动归因（29% ↔ 51%）

**测得**：丢弃率与 **worker 耗时**同向，与让锁次数无关。

| 阶段 | 耗时 p50 | 丢弃率 | 让锁跳帧 | 感知 p95 | 产出间隔 p50 |
|---|---|---|---|---|---|
| 140724_p1 | 136 | 38% | 33 | 61.7 | 217 |
| 140807_p2 | 153 | 51% | 18 | 59.2 | 233 |
| 140930_p1 | 150 | 50% | 24 | 54.1 | 234 |
| 141015_p2 | 148 | 51% | 23 | — | 235 |
| 141201_p1 | 130 | 30% | 38 | 60.1 | 200 |
| 141244_p2 | 130 | 29% | **53** | 59.7 | 194 |

- **让锁最多的一局（53 次）丢弃率最低（29%）** → 让锁不是丢弃的原因。
- **产出间隔 p50 = 194~235ms ≈ 节流窗 70ms + observe 耗时 130~150ms**（测得，`trace` 相邻
  `dgeo_new` 拍间隔）。这是 `AsyncDepthRoadObserver._loop` 的语义：节流窗在 observe **之后**
  计时（`_last_infer` 在 observe 返回后才更新），故**每周期多付 70ms 死时间**。
- **感知 p95 = 54~62ms 与深度 forward 的持锁时长同量级**（推断）：深度 worker 只在
  `moge_post.forward` 本体持 `core.dml_lock`（前/后处理在锁外），而感知是**阻塞持锁**；
  二者数值吻合提示感知尾部由锁等待主导——**未做归因实验（缺感知自身的 run 时长分布），需实机复核**。
- 丢弃机制（推断，算术）：结果在 age>350ms 的拍被清槽。结果年龄 = 发布龄（observe 耗时 +
  入队 0~50ms）+ 驻留（至下一结果发布，≈ 产出间隔）。**2×dur + 排队 ≈ 260~350ms（dur=130）
  ~ 300~400ms（dur=150）** → dur 一过 ~150ms 就系统性越闸。故丢弃率对 dur 高度敏感。

### 2.4 下游后果

- **有效新读数率**（applied / 循环时长）：3.3~4.6/s（六阶段；如 140724_p1 = 169/39.0s = 4.33/s）。
  > 注：任务背景给的 2.6~3.5/s 与本口径（applied ÷ 循环秒）对不上，差异应为「分母是否含
  > 模型加载/等待进页」的口径差；本表统一用循环秒，逐阶段值见 §4 复现指引。
- **ro 供给率**（`trace.ro_source != 'none'`）：52%~76%（与任务背景 53%~76% 一致），
  与 dur 同向（dur 130 → none 24~35%；dur 148~153 → none 42~47%）。
- **控制环**：19.1~20.6Hz（目标 30Hz）；控制链本体 p50 0.6~0.7ms、p95 1.1~1.4ms（远低于 2ms 判据）
  → **控制链本身不是瓶颈**，环频被感知/深度这条 DML 串行链压住。

---

## 3. 改进选项排序

> 收益/风险均为**空载口径的预期**，标注了需要实机复核的项。排序按「预期收益 × 实施成本」。

| # | 选项 | 预期收益 | 风险 | 验证方法 | 实施层级 |
|---|---|---|---|---|---|
| **1** | **后处理向量化**：`_scan_side`（39ms，逐格 median）/ 平面 `lstsq`（10ms）/ 列剖面（12ms） | 后处理 92ms 若砍半 → observe 总时长 **−20~25%**；丢弃率随之显著降 | 改的是**找边判据实现**，易改行为；须逐帧等价回归 | 离线：cProfile 定位 + **441 帧证据包逐位读数比对**（`replay_debug_evid` 口径）；金标集回归 | speedrush 插件实现 |
| **2** | **降低/取消节流窗 `INFER_MIN_INTERVAL_S`(70ms)** | 产出间隔 200~235→**130~150ms**；丢弃率 29~51%→**~0~10%**（算术：dur<150 时 2×dur<350）；有效新读数率 +30~50% | 深度 worker 抢 DML 锁占空比 ~26%→~39%，控制拍感知（阻塞持锁）碰撞更频繁；深度自身 GPU 占空比上升，与游戏争用加剧 → **必须实机 A/B** | 离线：按 `trace` 的 dur 分布重算产出间隔与越闸比例；实机：两个节流值跑同场景比 stale/age/感知 p95/控制 dt | 产线参数（一行） |
| **3** | **上采样降本**（26.9ms：3×`cv2.resize` + `np.stack` + `nan` 掩码） | 若把平面/找边改到原生分辨率、只回投锚点像素，可省 10~25ms | 改 3D 找边输入口径 → 等价回归 | 离线：同一证据包对比读数 | 插件实现 |
| **4** | **输入 ROI 裁剪 / 降分辨率**（重折叠导出权重） | 降 GPU 与传输；但**读回体积不是成本主因**（§1.4 已证去 normal 无效）→ 收益存疑 | **重大变更**（改权重/重折叠）+ 质量回归 | 新权重 + 金标 122 帧回归 + 实机 | 权重导出（重大） |
| **5** | **worker/进程优先级**（`core/process_priority.py` 已有方向） | **现状已生效**：日志「性能优先(提级): CPU」；对 GPU 排队无公开旋钮（该模块 docstring 已记） | 低 | — | 已有，无需动 |
| — | 锁策略（缩短持锁/改等待/拆会话） | 持锁=run 本体（~55ms），**无公开手段缩短**；缩短需先降 forward（见 4）或换 EP | — | — | 依赖 1~4 |
| **X1** | graph capture（`ep.dml.enable_graph_capture=1`） | 表面 63.6→4.3ms（`probe_dml_orchestration`）——**是假象** | **不可用**：捕获图**冻结输入**，重放不随帧更新（12 帧逐帧比对，仅第 1 帧正确；探针自身 hash 亦报不一致） | 已测，勿采纳 | 不实施 |
| **X2** | `enable_cpu_sync_spinning=1` | 实测 63.9 vs 63.7ms，**无收益** | — | 已测 | 不实施 |
| **X3** | 只绑需要的输出（去 unused `normal` 2.41MB） | 实测 **无收益**（53.3→54.5ms）→ 反证读回是同步而非带宽 | — | 已测 | 不实施 |

**排序理由**：1 打在最大单项（45%）且不需碰权重；2 是**一行参数**换丢弃率腰斩（但风险在锁竞争，
必须先实机 A/B）；3 中等收益；4 重大变更且收益存疑；X1~X3 已被本次测量否掉，列出来是为了防止
后人重复评估。

---

## 4. 复现指引

**环境**：仓库根、`.venv/Scripts/python.exe`（3.11；PATH 裸 `python` 是 3.9，结论作废）。
GPU：RTX 4060 Laptop 8GB，ORT 1.24.4 / DML。本次测量期间**未跑游戏**，GPU 基线 util≈40~49%
（MuMu/壁纸引擎等）。

```bash
# ① CPU 侧：证据包离线重放（不需要 GPU）——后处理两段耗时
.venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_pipeline_attribution.py
#   只跑某目录：--dir depth_debug_20261004_141244 ；限制帧数：--limit 20

# ② GPU 侧：observe 五段同进程分解（空载口径）
.venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_moge_stage_decompose.py \
    --n 50 --stride 12 --with-observe

# ③ 推理侧：DML orchestration 分解（复用既有探针）
.venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_dml_orchestration.py --iters 100
```

**新增探针**（只读，未改产线）：`probe_depth_pipeline_attribution.py`、`probe_moge_stage_decompose.py`。

**日志/trace 出处**：

- worker 健康行：`%APPDATA%\MaaRacingMaster\logs\20261004_140638\MaaRM_20261004_140638.log`
  第 443/926/1376/1795 行；`...\20261004_141125\MaaRM_20261004_141125.log` 第 458/925 行。
- 逐拍 trace：`...\data\speedrush\control_traces\trace_20261004_14*.jsonl`（列 `dgeo_ms` /
  `dgeo_age_ms` / `dgeo_new` / `ro_source` / `dgeo_sides`）。
  ⚠ `dgeo_*` 列在 `dgeo is None`（含超龄清槽）时为 `null` —— **超龄拍不落进 trace**，
  统计丢弃率要用日志的健康行，不能只数 trace。
- 证据包：`...\control_traces\depth_debug_20261004_140724|140807|140930|141015|141201|141244\*.npz`。
- 录制语料：`...\demos\manual_20261003_215150_p1|215237_p2\frames\`（1280×720 RGB JPEG）。

**逐阶段基准表**（applied ÷ 循环秒）：

| 阶段 | 轮数/时长 | applied | 丢弃 | 耗时 p50 | 有效新读数率 |
|---|---|---|---|---|---|
| 140724_p1 | 778 / 39.0s | 169 | 104 (38%) | 136 | 4.33/s |
| 140807_p2 | 925 / 48.4s | 168 | 178 (51%) | 153 | 3.47/s |
| 140930_p1 | 810 / 40.7s | 148 | 150 (50%) | 150 | 3.64/s |
| 141015_p2 | 802 / 39.0s | 143 | 150 (51%) | 148 | 3.67/s |
| 141201_p1 | 801 / 39.6s | 179 | 76 (30%) | 130 | 4.52/s |
| 141244_p2 | 899 / 43.8s | 203 | 83 (29%) | 130 | 4.63/s |

**未做 / 待实机**：① forward 那 ~45ms 的归属（需 GPU 时间线工具）；② 节流窗调整的锁竞争实测；
③ 后处理向量化的等价性回归（本次只做了归因，未动实现）。

---

## 后处理向量化：reading_from_points −23%，441 帧逐位等价回归锁（2026-10-04）

> 承接上文 §1：后处理（上采样 + 3D 找边）占 observe ~58%，其中 `reading_from_points`
> 是最大单项。本节是据此实施的**只读后处理向量化**——只动
> `maaracing_master/plugins/speedrush/depth_geo.py` 的后处理段
> （`reading_from_points` / `_scan_side` / `_fit_road_plane` / `_road_by_column_profile`
> 及其直接辅助函数）与 `tests/test_speedrush_depth_geo.py`。推理侧（ORT/EP/绑定）、
> `moge_post.py`、`INFER_INTERVAL_S` 节流窗**一律未动**。

### 1. 改了什么（逐位等价是硬约束）

| # | 改动 | 机理 | 为什么逐位等价 |
|---|---|---|---|
| 1 | `_scan_side` 分箱：逐格 `while`（96 次全量布尔扫描）→ 一次算格号 `floor((x+X_MAX)/DX)` + `bincount` | 消掉 O(96·n) 的重复掩码 | 格定义不变（左闭右开）；float64 下 `+X_MAX` 精确、`DX=0.25` 是 2 的幂故除法精确 ⇒ 与 `(x>=lo)&(x<hi)` 同判定 |
| 2 | `_scan_side` 格中位：逐格 `np.median` → 复合键（格号`<<32` \| 顺序保持的 float32 位模式）**一次排序** | 25~29 次 numpy 调用 → 1 次 | 排序只改排列不改多重集；偶数格两中项均值在 **float32** 内做（同 `np.median` 的 `mean(part[i-1:i+1])`）；位模式变换是保序双射 |
| 3 | `_scan_side` 选点化简：`(x·side>0.2) & (x·side≤X_MAX) & isfinite(hb)` | 省 `abs()` 与 `isfinite(xb)` | side=±1 下等价（另一半由 `>0.2` 蕴含）；NaN/±inf 由两次比较自然出局 |
| 4 | `reading_from_points`：Z 无关掩码提出 ZBIN 循环 | 5 箱各省 3 遍全带布尔运算 | 布尔与可交换结合 |
| 5 | 平面拟合两个循环：内点集不动即**提前收** | 平均 6 轮 `lstsq` → **3.8 轮** | 掩码不变 ⇒ 下一轮吃同一矩阵、出同一 coef、得同一掩码；只是跳过若干次结果相同的迭代 |
| 6 | `_road_by_column_profile`：省 `cur>=0`（不变量）+ 复用 `cont&~good` | 每轮少 3 次 numpy 调用 × ~187 轮 | `cur>=0` 恒真（初值取有效行最大下标，循环内只在 `cont⊆(nx≥0)` 时改写）；复用是同一表达式 |

### 2. 前后耗时对比（三条独立协议）

**A. 441 帧逐帧交错 A/B**（同一进程内 A/B 交替，消除机器漂移；`DEPTH_GEO_REF` 载入改造前实现）

| | p50 | p95 | mean |
|---|---|---|---|
| 参照（改造前） | 117.39ms | 142.59ms | 117.69ms |
| 向量化（改造后） | **90.44ms** | **116.78ms** | **91.80ms** |
| 变化 | **−23.0%** | −18.1% | −22.0% |

逐帧配对差：p50 −26.11ms、mean −25.89ms，**97% 的帧更快**（441×3 样本）。

**B. `probe_depth_pipeline_attribution.py`（441 帧证据包重放，背靠背两次）**

| 段 | 改造前 p50/p95 | 改造后 p50/p95 | 变化 |
|---|---|---|---|
| reading_from_points | 72.79 / 91.06 | **53.01 / 72.87** | −27.2% / −20.0% |
| 组装（未改动，作漂移对照） | 22.43 / 28.49 | 20.80 / 25.29 | −7.3%（机器变快） |

**C. `probe_moge_stage_decompose.py --n 50 --stride 12 --with-observe`（同 GPU 状态背靠背）**

| 段 | 改造前 p50 | 改造后 p50 | 变化 |
|---|---|---|---|
| preprocess（未改动） | — | — | 对照 |
| forward（未改动） | 104.74 | 103.94 | −0.8%（漂移对照） |
| reconstruct（未改动） | 9.99 | 10.06 | +0.7% |
| 上采样（未改动） | 21.93 | 21.51 | −1.9% |
| **reading_from_points** | **71.62** | **52.97** | **−26.0%**（p95 −16.2%） |
| **observe 五段和** | **216.91** | **196.95** | **−9.2%** |
| observe_debug 端到端 | 211.48 | 195.85 | −7.4% |

> **机器状态说明**：CPU 探针绝对值在本次会话内漂了 2× 以上（同代码 reading 从 42ms 到 117ms），
> 故**一律以同会话内 A/B 的比值与配对差为准**，不跨时段比绝对值。C 的三段未改动项
> （forward/reconstruct/上采样）在 ±2% 内，说明该次 A/B 的机器状态是匹配的。

### 3. 441 帧逐位等价

`probe_depth_equiv_441.py`：441 帧证据包全量重放，逐帧比对全部输出字段
（`left/right_edge_lane`、`left_x/right_x`、`sides`、`rejects`、`edge_pts`）：

```
帧数 441
标量比对 1286 项；非零 ulp 0 项（全部逐位一致）
✅ 全部字段逐位一致
```

标量用 `struct.pack("<d", …)` 做**逐位**比较（比 `==` 更严：±0 的符号差也会被抓），
`edge_pts` 的每个浮点分量同样逐位比。**无一段需要降级到「≤1 ulp + 零翻转」口径。**

### 4. 回归锁（落仓库）

`tests/test_speedrush_depth_geo.py` 新增 **44 条**，参照实现（改造前的逐格循环
`_ref_scan_side`）**原样保留在测试文件内**，不依赖 `%APPDATA%` 用户数据：

- `test_scan_side_matches_reference_on_random_clouds`（24 组，固定种子）：随机点云掺
  NaN/±inf/野值，2 侧 × 4 个 `zc` × 3 个 `extent` 全组合严格相等；
- `test_scan_side_matches_reference_on_bin_edges`：点**精确落在格边界** `−12+0.25k` 上
  （最容易分箱漂的位置）严格相等；
- `test_binned_median_matches_numpy_median`（12 组）：复合键中位数 vs 逐格 `np.median`，
  `uint64` 视图**逐位**相等（覆盖 1e-3~1e6 量程与奇偶格）；
- `test_reading_from_points_matches_reference_pipeline`（5 个既有合成场景：双墙/单侧弃权/
  物体掩码/近场护栏/弯道逐箱漂移）：把 `_scan_side` 换回参照实现，端到端全字段一字不差；
- `test_reading_from_points_abstain_paths_match_reference`：全 NaN（平面拟合失败）与单侧无墙；
- `test_fit_road_plane_early_exit_matches_full_iterations`：把 `PLANE_ITERS` 临时放大到 6
  （早收失效、必跑满），系数与产线路径逐位相同——证明第 5 项改动不改结果。

### 5. 复现

```bash
# 改造前实现（HEAD 提交即改动前状态，指针不随本次改动漂）
git show 5d184d3:maaracing_master/plugins/speedrush/depth_geo.py > /tmp/depth_geo_ref.py

# 441 帧逐位等价（before 用参照实现 dump，after 用仓库当前实现）
DEPTH_GEO_REF=/tmp/depth_geo_ref.py .venv/Scripts/python.exe \
    tools/experiments/speedrush_vision/probe_depth_equiv_441.py \
    dump --out .workbuddy-ai/depth_equiv/before.pkl
.venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_equiv_441.py \
    dump --out .workbuddy-ai/depth_equiv/after.pkl
.venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_equiv_441.py \
    check --ref .workbuddy-ai/depth_equiv/before.pkl --cur .workbuddy-ai/depth_equiv/after.pkl

# 前后耗时（DEPTH_GEO_REF 只换 reading_from_points 入口，其余段不动）
DEPTH_GEO_REF=/tmp/depth_geo_ref.py .venv/Scripts/python.exe \
    tools/experiments/speedrush_vision/probe_depth_pipeline_attribution.py
.venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_depth_pipeline_attribution.py
DEPTH_GEO_REF=/tmp/depth_geo_ref.py .venv/Scripts/python.exe \
    tools/experiments/speedrush_vision/probe_moge_stage_decompose.py --n 50 --stride 12 --with-observe
.venv/Scripts/python.exe tools/experiments/speedrush_vision/probe_moge_stage_decompose.py \
    --n 50 --stride 12 --with-observe
```

**全量测试**：`.venv/Scripts/python.exe -m pytest tests/ -q` → **1251 passed / 3 failed /
10 skipped**。3 个失败全在 `tests/test_navkit_v4_runner.py`（NavGraph.run 完成轮询节拍
的回归锁），**与本改动无关**：该模块不 import `depth_geo`/`speedrush`，且用 `git archive HEAD`
在**不含本改动的干净树**里复跑得到**同样 3 红**。本改动未引入新红。

### 6. 未达成项：observe 只降到 −9%，与「−20~25%」目标差在哪

**结论：observe −9.2%（五段和）/ −7.4%（端到端），未达 −20~25% 目标。** 原因是目标的
分母口径：observe 里 `reading_from_points` 只占 33~45%（取决于 GPU 侧 forward 的占比），
要让 observe 降 20% 需把 reading 砍掉 44~60%；本次在**逐位等价**约束下砍到 −23%，
再往下每一段都撞到硬边界：

| 剩余成本（占 reading） | 为什么砍不动（逐位等价下） |
|---|---|
| `lstsq` ~11%（3.8 次/帧） | 提前收敛已把 6 轮压到 3.8 轮；再减轮次=改结果。换法方程/QR 求解更快但**不是逐位**（条件数平方，偏差远大于 1 ulp），与本任务第一目标冲突 |
| `_road_by_column_profile` ~12% | 列剖面是**逐行递推**（每行的判据依赖前一行），行维不可向量化；已把每轮 numpy 调用从 42 降到 ~39，剩下的 187 轮 × ~39 次调用就是调用开销地板 |
| 格中位 ~14% | 已压到「一次复合键排序 + 段中项」；再快只能近似（直方图选择），会改值 |
| ZBIN 掩码 + `hgt`/`sky` + 点选取 ~26% | 全带（375×1280）布尔运算，已把 Z 无关项提出循环；`searchsorted` 变体实测更慢（3.8→4.4ms），不采纳 |

**若后续要冲 −20% observe**，需要的是**算法级**而非实现级改动，且都得先答清「判据等价」：
① `lstsq` 换法方程（省 ~8ms/帧）+ 441 帧零翻转证明（本次未做，因第一目标是逐位）；
② 把列剖面行走改成「按列并行 + 段内向量化」（改判据实现，风险最高）；
③ 缩走廊域/剖面分辨率（改行为，须重新标定）。三条都超出「向量化」的范围。

**边界**：本节全部为**空载口径**（未跑游戏）；GPU 侧绝对值需与游戏并发实机复核——
但本节改动全在 CPU 侧，forward 的争用变化不在其内。

