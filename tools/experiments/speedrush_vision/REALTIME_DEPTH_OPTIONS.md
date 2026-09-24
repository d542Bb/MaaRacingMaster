# REALTIME_DEPTH_OPTIONS — DA-V2 Small @ ONNX Runtime + DirectML 的高分辨率/低延迟路线调研

> **本文是什么**：针对「Depth Anything V2 Small 在 ORT+DML 栈上如何以高分辨率+低延迟运行」的一手信源调研结论。信源等级 L4（实验区调研笔记）；正式知识（L1~L3）不得引用本文路径。
> **撰写日期**：2026-09-24。**方法**：全部结论回溯到官方文档/源码/论文/官方 repo issue；每条声明标注出处类型；查不到一手出处的写明「查不到」。
> **基线（本仓库内部实测，非外部事实）**：q4f16、free-dim override 常量折叠后（patch 14 网格，时延≈正比 token 数）：
> @336(336×588, 1008 tok) p50 **16.5ms**；@448(448×784, 1792 tok) **46.9ms**；@518(518×910, 2405 tok) **90.8ms**。
> 用途约束：只需画面第 340~715 行（道路带）的横向台阶边界质量；16:9 纵横保持是几何标定前提。

---

## ① 结论摘要（按性价比排序的可行动路线）

| # | 路线 | 预期收益（对齐基线） | 风险 | 工作量 |
|---|---|---|---|---|
| R1 | **路面带裁剪 + 非方形推理**（裁 y340~715 后再定分辨率） | 同延迟（~50ms 档）路面带纵向 336px（全帧 @518 仅 270px，**+24%**）；或纵向 280px 持平 @518 而延迟降到 ~25–35ms（**-60%+）。变形重采样（横向压缩）可把 336px 压到 ~1104 tok（≈17–20ms），待裁决 | 3.4:1 宽带或变形输入偏离训练分布（训练为短边 518 保持纵横 + 方形随机裁剪）；读数带坐标需仿射迁移（y−340、×s，机械） | 观测器预处理 + 标定映射 + 目测/金标裁决（本目录已有 probe_crop_quality 流程可复用） |
| R2 | **CUDA 可选加速包（DML 默认、NVIDIA 检测后启用 onnxruntime-gpu）** | 量级估计 @518 从 90.8ms → **<20ms**（依据：DA-V2-S @770 TensorRT fp16 在 Tesla T4 = 20.99ms；4060 Laptop 应显著快于 T4；须实测确认） | 分发 +160MB 轮子 + CUDA 12.8/13 + cuDNN 9 版本矩阵；仅惠及 NVIDIA 用户 | EP 选择逻辑 + 打包开关；模式有成熟先例（FaceFusion、MaaFramework 本体、ORT 官方 EP 优先级机制） |
| R3 | **DML 会话级核对**（低成本兜底） | 个位 ms 级：确认 `ORT_SEQUENTIAL`+`enable_mem_pattern=false` 已设（DML 官方要求）、用 IO binding 免除入出参拷贝 | 几乎无 | 半小时核对 |
| R4 | **双频模型互补**（高频粗深度 + 低频精读数带） | 深度更新率与读数质量解耦：yolo26n/s-depth@768 DML 实测 9.4–11.6ms（内部）供高频，DA-V2 裁剪档（R1）供低频精读 | 两模型输出语义不同（log 距离 vs 视差）需统一；此前内部裁决 yolo 墙缘锐度不足 | 中：需要消费侧统一抽象 |
| R5 | **不建议**：ToMe/token merging、跨帧特征缓存、主干-头解耦 | —— | —— | —— |
| | | 三者均无「可导 ONNX 的成熟先例」，详见 Q3/Q6；属于自研图手术范畴（≥A 类投入）。 | | |

> R1 的 token/时延换算为按内部正比基线的插值估计（518 档曾有内部观测的“渐进超线性”现象，未对外验证），以实测为准。

### ①-b 实测验证（同日，本机折叠会话 q4f16，warmup 3 + 30，路面带 = 帧 y340~715 裁出）

| 配置 | 输入 | token | p50 | p95 | 路面纵向有效分辨率 |
|---|---|---|---|---|---|
| 全帧@336（生产现状） | 336×588 | 1008 | 22.5ms | 30.0 | 175px |
| 全帧@336 + mem_pattern=False | 336×588 | 1008 | **37.1ms** | 40.6 | （R3 反例，见下） |
| **路面带@280** | 280×952 | 1360 | **39.6ms** | 42.2 | **280px** |
| 路面带@336 | 336×1134 | 1944 | 80.3ms | 115.0 | 336px |
| 路面带@448 | 448×1526 | 3488 | 311.1ms | 326.2 | 448px |
| 带→变形 336×588 | 336×588 | 1008 | 36.9ms | 39.7 | 336px（横向压缩 2.1×，质量待金标） |
| 全帧@518（参照） | 518×910 | 2405 | 90.8ms | 96.6 | 270px |

**三条修正**：
1. **时延对 token 强烈超线性**（1008→22.5 / 1944→80 / 3488→311ms，token ×1.9→时间 ×3.6、×1.8→×3.9），
   R1 的线性插值估计全部偏乐观；@518 的“悬崖”实为 token 超线性，与分辨率本身无关。
2. **R1 的甜点不是“@336 裁带”而是“带@280”**：39.6ms 拿到路面纵向 280px——
   **严格优于全帧@518（91ms、270px）双轴**，也优于全帧@336 的质量（175px）；
   更新率 ~25Hz 配 20Hz 控制环成立。变形档同 token 却比全帧@336 慢（36.9 vs 22.5ms，
   疑与输入内存布局/热漂移有关，复测前不作数）。
3. **R3 反例**：DML 官方“mem_pattern=false”在本机折叠图上实测**更慢**（22.5→37.1ms）——
   生产保持默认（开启）即可，不要照抄文档开关；此条以本表为准。

（单轮 bench、机器有噪声，带@280 的 p95-p50 仅 2.6ms 可信度高；复跑入口
`tools/experiments/speedrush_vision/probe_fp_vs_q.py` 同款会话构造。）

---

## ② 逐问题详述

（【官方文档/源码】= 一手明说；【实测/官方表】= 信源自报的测量；【社区】= repo issue/PR 里的用户或贡献者经验；【推断】= 我方工程分析，无一手出处。）

### Q1. ORT + DirectML 的 ViT 性能最佳实践

1. 【官方文档】DML EP **不支持 memory pattern 优化与并行执行**：会话必须 `ExecutionMode::ORT_SEQUENTIAL` 且 `enable_mem_pattern=false`；同一 session **只允许单线程串行 `Run`**。
   出处：ONNX Runtime DirectML EP 文档 https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html
2. 【官方文档】DML EP 明确的性能主张与我们的做法同向：**张量形状在会话创建时已知**是最有效手段——带来常量折叠（减少 CPU/GPU 拷贝与停顿）、初始化期权重预处理、DML 图内算子融合（如移除 Concat）；自由维用 `dim_value` 固定 / `AddFreeDimensionOverrideByName` / dimension denotation 三种方式。→ **free-dimension 固定已是官方口径的“折叠/固定 shape”主线，没有更高级的开关。**
3. 【官方文档】DML EP 只支持到 **ONNX opset 20**（GridSample 5D、DeformConv 不支持）；DML 捆绑版本 1.15.2；DML EP 进入 **sustained engineering**（新功能转向 WinML）——即不要期待 DML 侧后续性能红利。
4. 【官方文档】DML Python 分发：`onnxruntime-directml`（当前 1.24.4，win_amd64 轮子约 **25MB**，自带所需组件）https://pypi.org/project/onnxruntime-directml/ （wheel 尺寸来自 https://pypi.org/pypi/onnxruntime-directml/json ）
5. 【官方文档】通用手段中对 DML 仍可用的：**IO binding** 免除输入/输出拷贝（`io_binding`，https://onnxruntime.ai/docs/performance/tune-performance/ 索引的 I/O Binding 子页）。`enable_cuda_graph` 等 graph capture 开关是 **CUDA EP 专属**（见其文档），DML 无对应项。
6. 【社区】已知坑样本：DML EP 在部分 RDNA2 设备（Xbox Series S）对 `com.microsoft.GroupQueryAttention`/MHA 解码图产生**确定性错误 logits**（CPU EP 正确），issue microsoft/onnxruntime#29739（已关闭）。属注意力算子正确性而非 ViT 编码器时延坑；引用价值：DML 上注意力类算子融合行为依赖厂商驱动，异常时先差分 CPU EP。https://github.com/microsoft/onnxruntime/issues/29739
7. 【查证结论】除「固定 shape / 常量折叠 / 单线程顺序会话 / IO binding / 避免非支持 op」外，官方文档**没有列出更多 DML 降开销手段**。

### Q2. 非方形输入与裁剪推理

1. 【官方源码】DINOv2 `interpolate_pos_encoding` 把 37×37（518/14）位置编码网格按 **w、h 各自独立** bicubic 插值到目标网格，**任意 H×W 非方形原生支持**（`facebookresearch/dinov2` `dinov2/models/vision_transformer.py`，https://github.com/facebookresearch/dinov2/blob/main/dinov2/models/vision_transformer.py ）；DA-V2 的 vendored 副本同构（仅 `interpolate_offset` 处理不同，https://github.com/DepthAnything/Depth-Anything-V2/blob/main/depth_anything_v2/dinov2.py ）。约束只是 H、W 为 14 的倍数（patch 对齐 + 源码 assert）。
2. 【官方源码】DA-V2 官方预处理即「短边缩放 + keep_aspect_ratio + ensure_multiple_of=14 + lower_bound」（`dpt.py` 的 `image2tensor`，https://github.com/DepthAnything/Depth-Anything-V2/blob/main/depth_anything_v2/dpt.py ）——我们的短边缩放与官方一致；论文训练为短边 518、方形随机裁剪（arXiv:2406.09414 §7.1、App. B.8，https://arxiv.org/abs/2406.09414 ）——**宽带输入在训练分布之外**，这是 R1 的主要风险所在。
3. 【社区】「裁掉上部再放大」的先例：
   - DA-V2 官方 repo issue **#278「Tiled inference」**（open）：用户报告 *放大局部（zoom/crop）结果更好*，但自行平铺出现棋盘格效应，官方未给出策略。https://github.com/DepthAnything/Depth-Anything-V2/issues/278 —— 这同时确认：官方**无** batch/滑窗/平铺推理支持（与我们已排除项一致）。
   - PR **#184/#185「ADD UI with crop zone」**（closed 未合并）：有人做过「crop zone + ONNX/TensorRT 推理」的 UI 尝试。https://github.com/DepthAnything/Depth-Anything-V2/pull/185
4. 【推断】标定迁移：裁 y340~715 不改变相机光心/消失点在画面中的位置，只是读数带行号平移 340 与等比缩放 s（短边 375→目标），横向台阶判据可整体仿射迁移；变形重采样（横缩竖拉）才会改变边缘表观斜率，需金标裁决。收益/开销测算见摘要 R1。

### Q3. 更高分辨率的注意力降算——有没有现成可导 ONNX 的

1. 【官方文档/源码】**ToMe**（facebookresearch/ToMe，论文 arXiv:2210.09461）：对既有 ViT **免训练** 2–3× 提速（分类域为主，如 ViT-S/16 953→1564 img/s）。但官方支持的实现只有 **timm / DeiT / MAE / SWAG**（`tome/patch/` 目录仅 timm.py、mae.py、swag.py），**无 DINOv2/DPT 补丁**。https://github.com/facebookresearch/ToMe
2. 【社区】ToMe→ONNX：官方 repo issue **#32**（「timm+tome 导出 ONNX 失败」）给出的解法是扩散方向 tomesd 的 PR（U-Net 裁剪，与深度 ViT 无关）——**不存在 DA-V2+ToMe 的 ONNX 化先例**。https://github.com/facebookresearch/ToMe/issues/32
3. 【官方文档】window attention 类主干（如 Swin）= 换模型重训练，不是 DA-V2 上的可插拔项；「下采样 stage / 更大 patch」同样改变模型定义。
4. 【结论】token 削减三流派（merging、窗口、粗 patch）在「DA-V2-S + ONNX + DML」语境下都**不是拿来即用**；若走此路只能自研图手术（ROI 局部注意力等），归 A 类设计工作，非本次调研范围。

### Q4. 候选模型横评（实时单目深度，2024–2025）

| 模型 | 规模（一手出处） | 官方报告时延（硬件/精度） | ONNX 可得性 | 许可证 | 合成/游戏域证据 | 判定 |
|---|---|---|---|---|---|---|
| **DA-V2-S**（现用） | 24.8M @768（Ultralytics 实测表，见 R2 行）；全系 25M–1.3B（论文摘要，arXiv:2406.09414） | **论文正文（本轮抓取部分）无逐模型 ms 表**——别再找“官方时延表” | onnx-community/depth-anything-v2-small（用户在用；HF 页本轮网络不可达，未复核） | **Apache-2.0**（GitHub API license 字段，https://github.com/DepthAnything/Depth-Anything-V2 ） | 训练管线即以合成数据教师蒸馏（论文摘要）；域内表现为我方实测 | 基线 |
| DA-V2-B/L/G | 97.5M/0.34B/1.3B | B：T4 TensorRT fp16 **55.4ms@770**（Ultralytics PR#25399 表） | 部分见 HF | Apache-2.0 | 同上 | 太重，放弃 |
| **Depth Anything 3** | Small 0.08B / Base 0.12B（官方模型表，https://github.com/ByteDance-Seed/Depth-Anything-3 ）；论文 arXiv:2511.10647 | 论文未报单帧实时；官方 DA3-Streaming 整管线 **8.5–10 FPS@A100、~500px 宽**（da3_streaming/README） | 无官方 ONNX；社区 ROS2 **TensorRT** 节点（ika-rwth-aachen/ros2-depth-anything-v3-trt） | Small/Base **Apache-2.0**；**Large 及以上 CC BY-NC 4.0（不可商用）**；官方注：-1.1 系重训、街景更好 | 多视角/位姿/GS 管线模型，非单图 DPT | 与「单帧路缘读数」错位；商用只余 S/B，但无导出与延迟证据 |
| **Metric3D v2** | ViT-L / ViT-g2 | 官方未报实时数 | **官方支持 ONNX**：repo `onnx/`（`metric3d_onnx_export.py`）+ onnx-community/metric3d-vit-large、metric3d-vit-giant2 | 代码 BSD-2（repo LICENSE）；权重许可证未复核（HF 不可达） | 真实街景/室内为主 | 推理输入 (616,1064)（`hubconf.py`）→ token ~3.3k 起，比 DA-V2@518 更重；出局 |
| **UniDepth / V2** | ViT-L 级 | 未查时延 | README 明有 “ONNX support” | **CC BY-NC 4.0（README License 段）→ 不可商用** | 真实域 | 出局（许可） |
| **Depth Pro (Apple)** | 多尺度 ViT，1.3B 级 | **2.25MP 深度图 0.3s @“standard GPU”**（论文摘要，arXiv:2410.02073；repo README） | 无官方导出；社区有 TRT（yuvraj108c）与 Rust-ORT（shigedangao/brioche）移植 | Apple 软件许可（LICENSE 原文：可再分发、**不含专利授权**） | 真实域+边界锐度是其卖点（boundary metrics） | 太重，出局 |
| **Fast-FoundationStereo** | NVlabs CVPR26 | TRT **14.0–23.4ms**、PyTorch 29.3–49.4ms（README 表，GPU 未注明） | **官方单文件 ONNX 导出脚本**（`scripts/make_single_onnx.py`）+ TRT 引擎/plugin | NVIDIA Source Code License（**research & evaluation only**，model_card 原文）；商用走 C-FFS（NVIDIA Open Model License） | 真实域 | **需校正双目对**输入，单屏截图不适用 |
| **Video-Depth-Anything-S** | 28.4M | **7.5ms / 32 帧**（FP16，A100，输入 1×32×518×518；README 表，https://github.com/DepthAnything/Video-Depth-Anything ） | 无官方 ONNX | Apache-2.0（同组织惯例，未逐文件核） | 视频时序一致 | 时序收益大但单帧成本≈×32 摊销、且换执行栈；列为观察对象 |
| **yolo26n/s-depth**（超纲补充） | 6.4M/13.2M @768 | Ultralytics 官方文档表（PR#25399）：**T4 TensorRT10 fp16 2.73ms/3.82ms@768** | 官方 release 直发 ONNX（我方记忆 v8.4.0 已核） | **AGPL-3.0**（与生产 model.onnx 同级） | 真实域为主；墙缘锐度不足（我方金标裁决） | 即 R4 的高频粗深度来源 |
| **LiteRmon** | —— | —— | —— | —— | —— | **查无此模型**（arXiv `all:`、GitHub repo 搜索、Semantic Scholar 均未命中，见③） |
| **“Python (CVPR24)”** | —— | —— | —— | —— | —— | **查无同名 CVPR2024 单目深度模型**（检索式见③）；最接近的 “P³Depth” 是 ICLR24 的 3D 检测加速，非深度估计。如有出处请补链接 |

### Q5. CUDA/TensorRT 路径的性价比与双栈分发

1. 【实测/官方表】DA-V2-S @770（14 的倍数对齐）在 **Tesla T4 + TensorRT10 fp16 = 20.99ms（47.6 FPS）**；@644 = 13.62ms。出处：Ultralytics 官方文档 PR #25399（docs/en/tasks/depth.md 新增对比表，https://github.com/ultralytics/ultralytics/pull/25399 ）——T4 是 70W Turing 推理卡，RTX 4060 Laptop 显著强于它；外推我们 @518 在 CUDA/TRT 下 **<20ms**（估计，须实测）。同一表内 DA-V2-B 55.4ms、YOLO26n-depth 2.73ms 可作量级参照。
2. 【官方文档】CUDA EP 分发要求：`pip onnxruntime-gpu`；ORT 1.21–1.26 绑 **CUDA 12.8 + cuDNN 9**（1.27+ 转 CUDA 13），cuDNN 8/9 互不兼容；Windows 侧 `enable_cuda_graph`（需 IOBinding+固定形状/地址）、`cudnn_conv_algo_search`、TF32、NHWC 等开关。https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html
3. 【一手数据·PyPI】`onnxruntime-gpu` win_amd64 轮子 **160MB**（v1.30.0，https://pypi.org/pypi/onnxruntime-gpu/json ）——不含 CUDA/cuDNN 运行库，按上表要求另发 → NVIDIA 加速包净增 ≥ 数百 MB 级；对比 DML 轮子 25MB。
4. 【官方文档】双栈机制本身是 ORT 一等公民：**EP 按注册顺序定优先级、逐节点回退**（“execute a node using CUDAExecutionProvider if capable, otherwise CPU”）。https://onnxruntime.ai/docs/execution-providers/
5. 先例：
   - 【官方源码】**MAA 生态本体**：MaaFramework 的 `MLProvider.h` 编译期检测 `onnxruntime/dml_provider_factory.h` 定义 `MAA_WITH_DML`，`ONNXResMgr` 提供 `use_cpu()/use_cuda(int)`——同一框架内 CPU/DML/CUDA 按构建/运行选择（https://github.com/MaaXYZ/MaaFramework ，source/MaaFramework/Resource/MLProvider.h ）。**「DML 为默认、NVIDIA 可选 CUDA」在 MAA 生态无需外来先例。**
   - 【官方源码】FaceFusion：`execution.py` 维护有序 provider 表（cuda/directml/coreml/rocm...），运行时按 `get_available_providers()` 取首个可用——与目标形态一致（https://github.com/facefusion/facefusion/blob/master/facefusion/execution.py ）。
6. 【查证结论】「DML 默认 + CUDA 可选包」的机制、依赖矩阵与先例均有官方出处；**没有**先例的是「A/I/N 三厂同包 + NVIDIA 子包」这种具体打包形态，需自定。

### Q6. DPT 头/主干解耦与跨帧特征复用

1. 【官方源码】DA-V2 的 DPT 头取主干 4 个中间层 token 网格（vits: [2,5,8,11]），头内为 refinenet 卷积 + 最后 `interpolate 到 patch×14` 再 2 个 conv（`dpt.py` ）——头的算力相对注意力可忽略，**“主干低分辨率+头高分辨率”在该架构中不成立**：头不产生新信息，只放大主干 token 网格的信息；主干分辨率才是天花板。【推断（架构依据同上）】
2. 【官方文档】全局注意力无 KV-cache 式接缝；跨帧只重算局部 = 自研 token 级 ROI 注意力，无成熟工程先例（Q3 已述）。
3. 【官方文档/源码】名字沾边的成熟工作都不同题：**TokenFlow**（omerbt，CVPR24）解决的是**扩散模型视频特征一致性**，不产出可导出的单帧深度推理管线（https://github.com/omerbt/TokenFlow ）；**DeepCache**（CVPR24）缓存的是**扩散 U-Net** 的高层特征（https://github.com/horseee/DeepCache ）；两者均无对应 ONNX 化先例。
4. 【社区/官方】真正利用帧间连续性的现成品是**换模型**路线：Video-Depth-Anything（在线时序传播模块，A100 上 32 帧 7.5ms FP16 摊到 ~0.23ms/帧；见 Q4 表）与 DA3-Streaming（分块+keyframe，面向里程计整管线非实时读数）。【推断】对我们最可行的“跨帧”收益其实在**调度层**：latest-only 异步槽已把 90ms 与 20Hz 解耦，提分辨率不威胁控制环。
5. arXiv 检索「Incremental ViT / 增量推理」未命中可部署的 ViT 增量推理框架（见③）。

---

## ③ 查证后不成立 / 不存在 / 查不到的清单（防止重复调查）

| 事项 | 检索式（本轮实际执行） | 结果 |
|---|---|---|
| DA-V2 官方支持 batch/平铺/滑窗推理 | repo issue/PR：`crop`, `Tiled inference` | **不存在**。#278 open 无人应答；crop-zone PR #184/#185 被关闭未合并 |
| DINOv2 位置编码限制方形输入 | 读 `facebookresearch/dinov2` 源码 | **不成立**，H×W 独立插值（仅要求 14 对齐） |
| ORT DML 有 CUDA graph capture 类开关 | tune-performance / DML EP 文档 | **没有**；`enable_cuda_graph` 是 CUDA EP 专属；DML 官方禁用 mem-pattern 与并行执行 |
| DML EP 持续演进 | DML EP 文档 “sustained engineering，新特性转 WinML” | 无后续性能红利预期 |
| 「LiteRmon」模型 | arXiv `all:"LiteRmon"`；GitHub repo 搜 `litermon`；Semantic Scholar | **查无**（三处均零结果） |
| 「Python (CVPR24)」单目深度模型 | arXiv `ti:"Python" AND abs:"depth estimation"`、`ti:"depth estimation" AND abs:"Python"`；GitHub/S2 | **查无**同名者；若指他物请补一手链接 |
| DA-V2 论文含逐模型实时时延表 | ar5iv 全文抓取 2406.09414 | 正文只报 518×518 训练/短边 518 测试、25M–1.3B、对 SD 类 10×+；**无逐模型 ms 表** |
| ToMe 可用于 DA-V2 并导 ONNX | facebookresearch/ToMe `tome/patch/` 目录 + issue #32 | patch 不支持 DINOv2/DPT；ONNX 导出无解（issue 关闭且方向不对题） |
| DA3 Large 及以上可商用 | 官方模型表 | CC BY-NC 4.0，**不可商用**（Small/Base 为 Apache-2.0） |
| UniDepth/V2 可商用 | lpiccinelli-eth/UniDepth README License 段 | CC BY-NC 4.0，**不可商用** |
| Metric3D v2 / Depth Pro / DA3 适合本栈 | 见 Q4 表出处 | 输入/规模/管线错位：Metric3D (616,1064) 更重、Depth Pro 0.3s@2.25MP、DA3 多视角管线——**均非 518 档实时代替品** |
| 「主干低分辨率+头高分辨率」在 DPT 类模型是成熟做法 | dpt.py 源码 + 上述 Q6 检索 | **未找到任何可引用先例**；架构上头不增信息 |
| MAA 生态「DML 默认 + CUDA 可选」先例 | MaaXYZ/MaaFramework 源码 | **有**：MLProvider 编译期 DML + ONNXResMgr use_cuda；FaceFusion 运行期 provider 表 |

**本环境信源限制（诚实声明）**：huggingface.co 与 arxiv.org 的 html 版在本机不可达（连接超时），HF 侧「现成 ONNX 产物」的断言全部取自**官方 GitHub README 的明示链接**（Metric3D onnx 目录与 onnx-community 链接、DA3 官方模型表）与用户现状，HF 页面本身未逐一复核；DuckDuckGo 抓取被拒。Tesla T4 数字来自 Ultralytics 官方 PR（第三方项目自报实测，非 NVIDIA 官方）。
