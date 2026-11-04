# speedrush 运行时性能剖析报告

**日期**：2026-10-08  ·  **样本**：本机实机对局 4 段（p1/p2，单段 39~86s）  ·  **方法**：全外部观测，未改动任何产线代码

---

## 0. 一句话结论

| 问题 | 结论 |
|---|---|
| 哪些进程碰图像？ | 只有 **sidecar（python3.11.exe）** 一个进程；游戏在 MuMu 模拟器里被截图 |
| 频率多快？ | 控制拍 **19.1Hz**、YOLO **18.5Hz**、深度 **5.84Hz**、HUD **1.71Hz**、截图 ≤60fps |
| 耗时多少？ | 深度单帧 **130ms**（后处理 115ms > 推理 69ms）；控制拍 P50 50ms |
| 开销多大？ | sidecar 对局中 **4.33 核**；其中 Python 线程仅 **1.3 核**，**~2.6 核是原生线程** |
| 最大单项浪费 | **RapidOCR 的 onnxruntime 线程池空转**：1.7Hz 识别却常驻 **2.28 核**（应为 0.09 核） |
| GPU 是不是瓶颈？ | **不是**。利用率 34~75%、温度 55~59°C、SM 时钟 2775MHz 满血、功耗 ~50W |

---

## 1. 进程与线程拓扑

### 1.1 进程

| 进程 | 内容 | 碰图像？ | 对局中 CPU |
|---|---|---|---|
| `python3.11.exe`（**sidecar 真实解释器**） | **speedrush 全部图像处理** | **是** | **4.33 核** |
| `python.exe`（venv 启动器壳） | 仅转发参数 | 否 | 0.00 核 |
| `MaaRacingMaster.Shell.exe` | C# WinUI3 + WebView2，GUI/看板/日志 | 否 | ~0.03 核 |
| `msedgewebview2.exe` ×2 | Shell 的内嵌浏览器 | 否 | ~0.44 核 |
| `g112-Win64-Shipping.exe` | 游戏本体（巅峰极速），跑在 MuMu 里 | 被截图/被控制 | 0.92 核（峰 2.09） |
| `MuMuVMMHeadless` / `MuMuNxMain` / … | 安卓模拟器 | 承载游戏 | ~0.07 核 |

**进程模型**：Shell 以**管理员权限**启动 → sidecar 继承 elevated。这一点决定了所有线程级工具是否可用（见第 7 节）。

### 1.2 sidecar 内与图像相关的线程

| 线程名 | 职责 | 名义频率 | 实测频率 | 对局中占核 |
|---|---|---|---|---|
| `speedrush-depth-worker` | 深度几何（MoGe 推理 + 找边 + 栅格） | 最新帧优先 | **5.84 Hz** | **0.88** |
| `module-worker` | 主控 + YOLO 检测 | 每拍 | 18.5 Hz | 0.25~0.29 |
| `speedrush-hud-sample` | HUD 读数（OCR） | 2 Hz | **1.71 Hz** | 0.09~0.11 |
| `speedrush-debug-io` | 调试图渲染 + 落盘 | ~2 Hz | ~2 Hz | 0.03 |
| WGC 原生回调线程 | `windows_capture` 帧回调 | ≤60 fps | ≤60 fps | 0.01 |
| 主控制线程 | 控制拍循环 | 20 Hz | **19.1 Hz** | — |
| **未命名的原生线程 ×3~4** | **见第 4 节** | — | — | **各 ~0.88** |

---

## 2. 频率（实测）

| 环节 | 目标/名义 | 实测 | 达成 |
|---|---|---|---|
| 控制拍 | 20 Hz | **19.1 Hz**（P50 拍间隔 50.1ms） | 95% |
| YOLO 检测 | 每拍 | 18.5 Hz | 93% |
| 深度几何 | — | **5.84 Hz** | — |
| HUD 读数 | 2 Hz | **1.71 Hz** | 86% |
| 截图 | ≤60 fps | ≤60 fps | 达标 |
| 控制链（跟踪+聚合+决策+规划） | ≤2 ms | P50 1.0~1.5 ms | 达标 |

---

## 3. 耗时（墙钟）

### 3.1 控制拍

```
P50 50.1ms   P95 67.2ms   max 499.6ms
```

P95 超 20Hz 预算（50ms）的部分来自 **DML 等锁**：YOLO 与深度共用 DML 设备，YOLO 的
`P95 62~69ms` 里 **等锁占 45~57ms**。

### 3.2 深度单帧 130ms（126~170ms，跨会话有上升趋势）

```
forward（MoGe q4f16 @336×598，DML 推理）   68.8 ms
edges（点云 3D 找边，含平面拟合 26ms）      71.5 ms   ← 单项最大
grid（可行驶栅格）                          30.0 ms
reconstruct 6.3 + upsize 7.6 + pre 7.0
────────────────────────────────────────────────────
推理 ≈69ms   |   后处理 ≈115ms   ← 后处理比推理还慢
```

**深度数据年龄**：P50 **244ms** / P95 313ms（安全下限 100ms）。

### 3.3 其余

| 环节 | 单次耗时 |
|---|---|
| YOLO 检测 | P50 12.8ms / P95 62~69ms |
| HUD 读数 | **584ms / 行** |
| 调试图渲染 + 落盘 | ~23ms（2Hz 节流） |

---

## 4. CPU 开销归因（本报告核心）

### 4.1 三层口径

| 口径 | 核数 | 来源 |
|---|---|---|
| **进程级**（对局中） | **4.33**（三次独立采集：4.10 / 4.24 / 4.33） | psutil `cpu_times` 差分 |
| **Python 线程** | **~1.30** | py-spy 采样 |
| **原生线程** | **~2.6~3.5** | 差额，经每线程 CPU 探针定位 |

### 4.2 Python 线程明细（py-spy，函数级）

| 线程 | CPU秒 | 占核 | 主要去处 |
|---|---|---|---|
| `speedrush-depth-worker` | 34.5 | 0.88 | **32% 在 DML 推理调用** `run_with_iobinding`；其余散在后处理 |
| `module-worker` | 10.0 | 0.26 | YOLO 推理 5.29s（53%）、**建会话 1.16s（12%）**、`sleep` 0.63s |
| `speedrush-hud-sample` | 3.7 | 0.09 | OCR 推理 2.75s（74%） |
| `speedrush-debug-io` | 1.1 | 0.03 | 写图 `_imwrite_utf8` 0.69s |
| 截图线程 | 0.5 | 0.01 | `screencap` —— 截图 Python 侧几乎不花钱 |

深度 worker 后处理明细：`_binned_median` 1.63s、`infer_points` 2.38s、`preprocess` 1.44s、
`stack` 1.10s、`lstsq` 0.96s、`reading_from_points` 0.77s、`_fit_road_plane` 0.51s。

### 4.3 原生线程 —— 定位到 RapidOCR

每线程 CPU 探针在对局中抓到 **4 个各 ~0.88 核的线程**（79/82 拍恒定是这 4 个并发）：

| 现象 | 证据 |
|---|---|
| 这 4 个线程**成组诞生** | 首现于 t=67s 与 t=111s，各一组 4 个 |
| 诞生时刻 = **19:19:28 / 19:20:12** | 由探针记录的墙钟换算 |
| 该时刻日志正在做什么 | **RapidOCR 加载 `PP-OCRv6_det_small` / `ch_ppocr_mobile_v2.0_cls_mobile` / `PP-OCRv6_rec_small` 三个 onnx** |
| 其中几个 py-spy 看得见 | 3 个**完全采不到样**（纯原生线程） |

**本地受控复现**（同参数建 RapidOCR 引擎）：

| 场景 | 进程核数 |
|---|---|
| 引擎就绪、不识别 | 0.00 |
| **1.7Hz 反复识别（intra_op=4）** | **2.28** ← 3 个线程各 ~0.75~0.88 核 |
| 停止识别后 | 0.00 |

**单次调用开销对比**（同一输入）：

| intra_op | 单次墙钟 | 单次 CPU | CPU/墙钟 |
|---|---|---|---|
| 4 | 17.4 ms | **154.2 ms** | **8.9 核** |
| 2 | 16.5 ms | 62.5 ms | 3.8 核 |
| 1 | 27.5 ms | 28.1 ms | 1.0 核 |

**判定**：`intra_op_num_threads=4` 下，onnxruntime 的 intra-op 线程池在两次识别
之间**持续自旋**——1.7Hz 的调用占空比只有 2%，线程却烧掉 75% CPU。**约 96% 的
OCR 引擎 CPU 不是干活，是空转。**

已排除的旁因：`OMP_WAIT_POLICY=PASSIVE` 无效（2.33 vs 2.29 核）→ **不是 OpenMP 忙等**，
是 onnxruntime 自身的线程池自旋。

---

## 5. GPU 侧（排除瓶颈）

| 指标 | 对局中实测 | 判读 |
|---|---|---|
| 利用率 | 34~75% | 未跑满 |
| 显存 | 3.8~4.4 / 8.2 GB | 有余量 |
| 温度 | 55~59 °C | 低 |
| SM 时钟 | **2775 MHz（满血，无降频）** | **排除热降频** |
| 功耗 | 48~52 W（4060 Laptop TGP 约 60~115W） | 未触顶 |

---

## 6. 逐项实测：各环节单独跑多少核

为定位那 2.6 核，逐一把组件拆出来单独量（均为本地受控复现）：

| 组件 | 单独实测 | 结论 |
|---|---|---|
| 项目自建 WGC 截图（60fps + 20Hz 消费者） | 0.40 核 | 不是 |
| MaaFW `Win32Controller(FramePool)` 60Hz 截图 | 0.06 核 | 不是 |
| onnxruntime YOLO（DML，20Hz） | 0.12 核 | 不是 |
| 两个 DML 会话并存 + 共享锁 | 0.29 核 | 不是 |
| 深度 observe 全链（infer + edges + grid） | 1.02 核 | 单线程，只吃 1 核 |
| 调试图渲染 + 落盘 | 0.03 核 | 不是 |
| **RapidOCR 引擎（intra_op=4，1.7Hz）** | **2.28 核** | **是** |

上表加总（含 OCR）≈ **4.2 核**，与进程级 4.33 核对得上。

---

## 7. 工具坑清单（都会误导结论，务必留档）

1. **`psutil.Process.threads()` 对 elevated 进程静默返回空列表**，不报错——第一版采样器据此以为"没线程"。
2. **`OpenThread` / `GetThreadTimes` 对 elevated 目标一律 `err=5`**。根因：MRA 以管理员权限启动。
3. **py-spy 看不见纯原生线程**：受控负载实测 psutil 1.45 核 vs py-spy 1.02 核，漏掉的正是 BLAS 原生线程池。
4. **py-spy `--idle` 给的是墙钟不是 CPU**（每线程恒等于窗口时长），对 CPU 归因无用。
5. **`typeperf` 的线程计数器严重低估**：受控负载实际 ~1 核，它只读到 0.07 核（14× 偏差）。
6. **`NtQuerySystemInformation(SystemProcessInformation)` 免提权即可读 elevated 进程的每线程 CPU**（任务管理器同款）——本报告的突破口。
7. **`SYSTEM_THREAD_INFORMATION.StartAddress` 被 Windows 清零**（ASLR 缓解），线程起始地址这条路不通。
8. **`EnumProcessModules` 对 elevated 进程仍被拒**（err=5）——模块级归因走不通。
9. `Start-Process -Verb RunAs` 不允许用**解释器**（python/powershell）做提权跳板；py-spy.exe 这类专用 exe 可以。
10. 进程角色匹配别用宽串：`maaracing_master` 会命中**自己的诊断脚本命令行**，造成假阳性。

---

## 8. 未决项与边界

- **深度越跑越慢**（11:49 的 126ms → 15:01 的 170ms）**不是热降频**（GPU 满血、55~59°C）。同机同权重下变慢的成因本轮**未定位**，是独立问题。
- 本轮**未测得**线程级上下文切换率（探针只在 t=0 存了一次线程元数据，重线程当时尚未诞生），因此"自旋 vs 阻塞"是靠**受控复现**（第 4.3 节）判定的，不是靠上下文切换数。
- 频率天花板（深度 5.84Hz）的**直接成因是单帧 130ms**（后处理占 115ms）；OCR 空转是**另一笔独立的 CPU 开销**，两者不宜混为一谈——12 逻辑核下 4.3 核并未打满，但 OCR 空转与深度 worker 争抢 CPU，可能加剧拍抖动（P95 67~77ms）。

---

## 附：本次新增的观测工具（均为只读探针，未改产线）

| 文件 | 用途 |
|---|---|
| `probe_thread_cpu.py` | **免提权**每线程 CPU 探针（`NtQuerySystemInformation`） |
| `probe_full_census.py` | 全量采集：全系统进程 + 有 CPU 的进程逐线程 + GPU |
| `probe_runtime_census.py` | 进程级 CPU/RSS/GPU 时间序列（角色识别） |
| `probe_observe_cost.py` | 深度链各阶段实占核数 |
| `probe_ocr_cost.py` | RapidOCR 引擎线程开销复现 |
| `probe_ort_threads.py` | onnxruntime 会话是否自旋 |
| `probe_dml_multi.py` | 多 DML 会话并存开销 |
| `probe_wgc_cost.py` | WGC 截图链路开销 |
| `probe_maafw_cost.py` | MaaFW FramePool 截图开销 |
| `spy.ps1` | py-spy 提权运行器 |
