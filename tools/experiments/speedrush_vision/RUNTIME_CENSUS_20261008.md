# speedrush 运行时画像（2026-10-08）

> **问**：speedrush 真的在跑时，有哪些进程和图像有关、耗时多少、开销多大、频率多快。
> **口径**：全部外部观测（psutil + nvidia-smi + 性能计数器 + 产线 trace/日志），零产线改动；深度链 CPU 成本用本进程内直测（`probe_observe_cost.py`）。
> **环境**：RTX 4060 Laptop 8GB / 12 逻辑核 / MuMu 模拟器跑巅峰极速（`g112-Win64-Shipping.exe`）。
> **样本**：2026-10-08 下午 16 个驾驶阶段（trace+日志）+ 傍晚 3 轮实机 census。

## 1. 进程拓扑

| 进程 | 角色 | 权限 | 碰游戏图像 |
|---|---|---|---|
| `MaaRacingMaster.Shell.exe` | GUI（WinUI3 + WebView2），sidecar 生命周期 | **elevated** | 否 |
| `python3.11.exe` / `python.exe` | **sidecar**：`-u -m maaracing_master.core.sidecar`，**全部图像处理在此进程内** | **elevated**（继承自 Shell） | **是** |
| `MuMuVMMHeadless.exe` / `MuMuNx*.exe` | MuMu 安卓模拟器 | 否 | 是（渲染游戏画面） |
| `g112-Win64-Shipping.exe` | 游戏本体（`E:\DFJS\g112\...`，`--MuMuLauncher`） | elevated（OpenProcessToken err=5） | 是 |

sidecar 内与图像相关的线程：主控制拍、深度 worker、HUD 采样 + 写线程、WGC 原生回调、MaaFW 框架（锚点复查）。

## 2. 图像相关环节：频率与耗时（实测）

| 环节 | 目标/名义 | 实测频率 | 单次耗时 |
|---|---|---|---|
| WGC 截图 | 60 fps 上限 | 系统全速、按 60 节流 | 客户区 1280×720 |
| 控制拍（主循环） | 20 Hz | **18.1~19.4 Hz** | P50 **50.1ms** / P95 **67~77ms** |
| YOLO 检测 | 每拍 | 18.5 Hz | P50 12.8ms / P95 62~69ms（等锁 P95 **45~57ms**） |
| **深度几何** | — | **4.5~5.8 Hz** | **126~170 ms** |
| 深度数据年龄 | ≤100ms（安全下限） | — | P50 **230~277ms** / P95 312~342ms（复用上限 350ms） |
| HUD 读数 | 2 Hz（`SAMPLE_INTERVAL_S=0.5`） | **1.71 Hz** | 584ms/行（14 区域 OCR） |
| 控制链（跟踪+聚合+决策+规划） | ≤2ms/tick | 每拍 | P50 1.0~1.5ms / P95 2.0~2.6ms |
| 调试图渲染+落盘 | 2 fps（`DEBUG_INTERVAL_S=0.5`） | — | 落盘 CPU ~23ms/次，1.79MB/帧 |

## 3. 深度 observe 链拆解

**实机 stages 滑窗（`LAST_STAGE_MS`）**：

| 阶段 | 耗时 |
|---|---|
| pre（预处理） | 7.0 ms |
| **forward（MoGe q4f16 @336×598 推理）** | **68.8 ms** |
| reconstruct | 6.3 ms |
| upsize | 7.6 ms |
| **edges（点云 3D 找边，含 plane 26.0ms）** | **71.5 ms** |
| **grid（可行驶栅格）** | **30.0 ms** |
| **推理小计** | **≈69 ms** |
| **后处理小计** | **≈115 ms** |

→ **后处理比推理还慢**（115ms vs 69ms），其中找边 + 栅格占 101ms。实测 `dgeo_ms` P50 = 168ms（含 push/take 调度）。

**同一台机器越跑越慢**：11:49 深度 126ms（forward 52.8 / edges 63.2）→ 15:01 涨到 170ms（forward 68.8 / edges 71.5）。

## 4. GPU 画像（排除热降频）

| 指标 | 实测 | 判读 |
|---|---|---|
| 利用率 | 34~69% | 未饱和 |
| 显存 | 3.8~4.4 / 8.2 GB | 占一半，有余量 |
| 温度 | 55~59 °C | 低 |
| SM 时钟 | **2775 MHz 稳定** | **满血，无降频** |
| 功耗 | 50 W（TGP 约 60~115W） | 未触顶 |

→ **GPU 不是瓶颈**；"越跑越慢"与热降频无关。

## 5. CPU 画像

**进程级**（12 核归一化）：

| 进程 | 中位 CPU | 折合 |
|---|---|---|
| **sidecar** | **28.8~32.3%** | **3.46~3.88 核** |
| 游戏本体 | 7.6% | 0.91 核 |
| MRA Shell | ~0% | — |
| MuMu | ~0% | — |

**深度链 CPU 成本**（本进程直测，`probe_observe_cost.py`）：

| 环节 | 墙钟 | CPU | 核数 |
|---|---|---|---|
| observe 全链 | 138~143 ms | 145.8 ms | **1.02~1.06** |
| ├ infer_points（含 DML） | 78.6 ms | 99.0 ms | 1.26 |
| ├ reading_from_points | 43.8 ms | 41.7 ms | 0.95 |
| └ drivable_grid | 15.6 ms | 15.6 ms | 1.00 |

→ **深度链是单线程的，约 1 个核**。

**调试图落盘**：`imwrite` 渲染图 7.0ms（核 1.42）+ 原帧 12.5ms（核 4.01，JPEG 多线程）+ `savez` 3.1ms ≈ 23ms/次 × 2fps ≈ 0.05 核。

**CPU 账（与 sidecar 实测对不上）**：

| 环节 | 核数 |
|---|---|
| 深度 observe（145.8ms × 5Hz） | 0.73 |
| 主控制循环（14.8ms × 18.5Hz） | 0.27 |
| YOLO（12.8ms × 18.5Hz） | 0.24 |
| 调试图落盘 | 0.05 |
| **小计** | **≈1.29** |
| **sidecar 实测** | **3.46~3.88** |
| **缺口** | **≈2.2~2.6 核（未定位）** |

## 6. 工具坑（本次实测，均会误导结论）

1. **psutil `Process.threads()` 对 elevated 进程静默返回空列表**（不报错、不抛异常）——`num_threads()` 正常（78），`threads()` 返回 `[]`。极易误判成脚本逻辑 bug。
2. **`OpenThread` 对 elevated 进程线程一律 `err=5`（Access Denied）**——普通权限进程无法读取 elevated 进程的逐线程 CPU。MRA 以管理员权限启动是根因。
3. **`typeperf "\Thread(*)\% Processor Time"` 严重低估**：受控负载（GIL 限制实际 0.99 核）下，psutil 读 0.99 核、typeperf 只读到 **0.07 核（14× 低估）**。此路不可用于归因。
4. **Windows `GetProcessTimes` 精度 15.6ms**——短操作（<15ms）的 CPU 时间测量不可靠，核数须在 ≥100ms 量级操作上取。

## 7. 结论与待办

**已定位**
1. 频率瓶颈在**深度通道**：4.5~5.8 Hz（安全地板 10 Hz），数据年龄 P50 250ms（上限 100ms 的 2.5 倍）。
2. 深度耗时结构：**后处理（找边 + 栅格 ≈101ms）> 推理（69ms）**——优化杠杆在后处理，不在模型分辨率。
3. GPU 侧富余（未降频、未跑满、显存有余）——**不是瓶颈**。
4. 控制拍 P95（67~77ms）超 50ms 预算，来自 YOLO 的**等锁 P95 45~57ms**（与深度 worker 争 DML）。

**未定位**
5. sidecar CPU 有 **~2.2~2.6 核去向不明**（已知环节只能解释 1.29 核）。线程级归因当前受阻于权限（见 §6），需要 **`py-spy`（需安装 + 以管理员运行）** 才能精确到函数级。

**复跑入口**
- `probe_runtime_census.py`：进程级 CPU + GPU 时间序列（`--watch` 等 sidecar）
- `probe_observe_cost.py`：深度链各阶段墙钟 / CPU / 实占核数
- `probe_latency.py`：全链时延分解（既有）
