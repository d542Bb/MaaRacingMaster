# Runtime Pruning Policy

正式 Release 的裁剪白名单（`assemble.ps1 -Configuration Release` 默认全部启用）。

每个条目都经过了独立单变量实验验证。修改依赖 / WindowsAppSDK / Python / MaaFw 版本时，
若本白名单匹配不到目标文件，`-Configuration Release` 会**发布失败**（PRUNING-FAIL），而不是悄悄漏删。

## Production-safe removals

\| 项 | 来源实验 | 收益 | 验证结论 |
\|---|---:|---|
\| WindowsAppSDK AI/ML 死链 | exp1 | \~43.65 MB | SAFE |
\| WindowsAppSDK Widgets 死链 | exp2 | \~2.49 MB | SAFE |
\| Python ORT `capi\onnxruntime.dll` | exp3 | \~20.13 MB | SAFE（pyd 自带 ORT 引擎） |
\| PIL `_avif` native ext | exp4A | \~7.52 MB | SAFE（惰性，项目零 avif 路径） |
\| `.NET` crash diagnostics | exp4B | \~4.73 MB | **SAFE FOR NORMAL OPERATION** |
\| NumPy dev/build 目录 | exp5A | \~1.87 MB | SAFE |
\| `.pyi` typing stubs | exp5B-1 | \~1.13 MB | SAFE |
\| Python console wrappers | exp5E-1 | \~0.83 MB | SAFE |
\| SymPy | exp6 | \~25.37 MB | **SAFE FOR CURRENT MaaRM** |
\| MaaAgentBinary | exp7 | \~12.53 MB | **SAFE FOR CURRENT MaaRM** |
\| ORT offline toolchain（google/protobuf + flatbuffers） | exp8 | \~0.96 MB | SAFE（onnxruntime+RapidOCR closure 探针确认运行时未加载；cost：ORT quantization / offline shape-infer / ort\_format\_model 不可用，推理路径不受影响） |
\| OpenCV videoio ffmpeg backend（`opencv_videoio_ffmpeg500_64.dll`） | exp9 | \~29.45 MB | SAFE（dumpbin 证实 cv2.pyd 非静态依赖，ffmpeg 为运行时 LoadLibrary 的 videoio 后端；阶段二删除后 import cv2/中文路径/dnn.NMSBoxes 全 PASS，videoio 失败仅告警到 stderr，stdout/JSONL 干净；cost：OpenCV 视频文件读写/回放禁用，MaaRM 生产零 videoio 调用） |

## 注意与代价

- **Crash diagnostics**：删除后没有 application-level createdump / DAC (`mscordaccore`)，
  降低崩溃转储与 SOS 分析能力；Windows Error Reporting OS 级 dump 仍可配置。
  保留 `mscordbi.dll`（debugger attach 能力）。

- **SymPy**：仅当前 MaaRM 运行路径安全。若未来使用 onnxruntime 的离线
  `symbolic_shape_infer` / `transformers` 工具，需恢复依赖。

- **MaaAgentBinary**：Android/ADB 路径不再支持。当前 MaaRM 的 `Win32Controller`
  （Win32 截图 + 手柄）不受影响。

## 明确不纳入正式裁剪

| 项                                                         | 原因                                         |
| --------------------------------------------------------- | ------------------------------------------ |
| `pygrun` (6 KB)                                           | 收益过低，不值得增加规则复杂度                            |
| `mpmath`                                                  | 不因"看似无用"顺手删，等真正需要时独立实验                     |
| `mscordbi.dll`                                            | 保留 debugger attach，1.18 MB 不值得牺牲           |
| `numpy.typing`                                            | 收益 \~0.14 MB，进入第三方源码层，不划算                  |
| dist-info `RECORD`                                        | `importlib.metadata.files()` 语义可能依赖，价值低于风险 |
| `INSTALLER/WHEEL/REQUESTED`                               | 收益仅 2.8 KB，不纳入                             |
| `PublishTrimmed` / WindowsAppSDKSelfContained 调整 / TFM 调整 | 大决策，未验证，禁止盲开                               |
| DirectML hardlink                                         | 未纳入                                        |

## 负结果归档（本轮候选方向，实测后关闭）

> 原则：**关闭必须有实测数字 + 语义化命名**。禁止只写编号。编号 `A#/B#` 无语义、
> 会与其他轮次实验编号重复占用——凡无法用「候选名+实测→结论」形式复原的，一律标注
> 为「不可复用」，不得当已验证关闭项留存（否则下轮会重复询问或误当已验证）。
>
> 状态码语义（评审定，本表统一采用）：
> `KEEP`=保留依赖，凭证据/代价不值得动 · `CLOSED-ABSENT`=目标物不存在，基本永久关闭 ·
> `CLOSED-NEGATIVE`=有实树实测，收益≈0 · `CLOSED-UNSAFE`=有收益但风险不可接受（上游修复可复活）。

### 落地项（本表即账本；对 status = 落地，补 Download 内容/形态列）

| 语义名                  | 一手证据                                                                                | Installed Δ | Download Δ（内容/形态） | 风险·cost                                                | 失效条件    | status |
| -------------------- | ----------------------------------------------------------------------------------- | ----------- | ----------------- | ------------------------------------------------------ | ------- | ------ |
| OpenCVVideoioBackend | dumpbin 证 ffmpeg dll 为运行时 LoadLibrary 的 videoio 后端，非 cv2.pyd 静态依赖；删后 JSONL 污染专测 0 行 | −29.45 MB   | −12.20（内容）        | OpenCV 视频读写/回放禁用；MaaRM 生产零 videoio 调用                    | 若引入视频功能 | 落地     |
| OrtOfflineTooling    | closure 探针（onnxruntime+RapidOCR 构造+推理）确认 protobuf/flatbuffers 未加载                   | −0.96 MB    | −0.39（内容）         | ORT quantization/离线 shape-infer/ort\_format\_model 不可用 | 若需离线量化  | 落地     |

### CLOSED-ABSENT（目标物不存在，基本永久关闭）

| 语义名                    | 一手证据                                                                                                                              | Installed Δ | 失效条件                          |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ----------- | ----------------------------- |
| DotnetIcuGlobalization | `app\` 下 0 个 `icudt*.dat`（glob `icu*` count=0，0MB）；Win10 1703+ 系统集成 ICU，.NET 5+ 在 1903+ 直接走 OS ICU，故 self-contained 不打包 icudt.dat | 0 MB        | TFM / publish mode / 目标 OS 变更 |

### CLOSED-NEGATIVE（有实树实测，收益≈0）

| 语义名                | 一手证据                                                                                                                                                                                                                                                                                                           | Installed Δ |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| OpenCVHeadless     | `opencv-python-headless==5.0.0.93` = 111.98 MB vs 原版 111.95 MB；仍暴露 imshow/namedWindow/VideoCapture                                                                                                                                                                                                             | ≈0 MB       |
| SatelliteResources | 非目标 culture = 0（仅 `zh-CN`）；`.resources.dll` 仅 1 个 0.150 MB 主 `Microsoft.Windows.ApplicationModel.Resources.dll`（非卫星，须保留）                                                                                                                                                                                       | 0 MB        |
| ReleasePdb         | `MaaRacingMaster.Shell.pdb` = 0.038 MB → 保留（39 KB 换异常堆栈行号不划算，同 pygrun 6 KB 先例）                                                                                                                                                                                                                                             | 0 MB        |
| NonX64Artifacts    | 判定须**双条件**，不可单看 PE machine 字段：托管（IL）程序集的 machine 是历史遗留占位值，运行时架构由 CLR 决定，与字段无关——单看字段会把 105 个 `System.*`/`*Projection.dll` 误报成 x86 payload。正确判定：`IMAGE_FILE_MACHINE_AMD64(0x8664) 且非 ILONLY` → 真 x64 native（实测 135 个）；`COMIMAGE_FLAGS_ILONLY` 置位 → 架构中立，machine 字段忽略（实测 105 个托管程序集，非真 x86）。托管侧（37.85 MB）全 AnyCPU | 0 MB        |

### CLOSED-UNSAFE（有收益但风险不可接受，上游修复后可复活）

| 语义名                   | 一手证据                                                                                                                                                                                                                                   | 复活路径                          |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| RapidOcrDetClsPruning | `rapidocr==3.9.2` 构造即加载全部 det/cls/rec（`RapidOCR.__init__` 无条件 `TextDetector/TextClassifier/TextRecognizer`）；`use_det/use_cls=False` 只关执行路径不阻止加载；删除任一 → OCR 静默失效。B4′-0 核验通过：release 三模型齐全（det 9.47/cls 0.558/rec 20.251 MB）且离线+干净副本构造成功 | 升级到上游惰性加载版本后 det/cls 才可能从包里消失 |

### KEEP（保留依赖；凭证据/代价不值得动）

| 语义名                                                                     | 一手证据                                                                                                                                                 |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| ShapelyGeos                                                             | rapidocr 顶层 `from .ch_ppocr_det import TextDetector`（main.py:15）→ `ch_ppocr_det/utils.py` 模块级 `from shapely.geometry import Polygon`；closure 探针显示恒加载 |
| RuntimeHttpChain（requests/tqdm/urllib3/certifi/idna/charset-normalizer） | rapidocr 的 `utils/load_image.py` + `download_file.py` 模块级硬导入                                                                                         |

### 形态候选（独立立项，不改 payload）

| 语义名                | 一手证据 / 收益分解                                                                                                                                                                                                                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ArchiveFormatSolid | 7z LZMA2 256M solid：198.79 → 136.59 MB（−31.3% Download / 0 Installed）。收益分解 = 算法 −22.3%（154.54）+ 固实额外 −17.95 + 档位 −3.1%（192.59）；**solid 是唯一关键变量**。落地属发布形态改造，不与代码裁剪混 PR。**已立项（立即，与 PublishTrimmed 并行）**，主推 `7z LZMA2 256M solid 主推 + zip 保底` 双产物（非 SFX，避免签名/杀软问题）；待产品方拍板 7z 依赖/双产物/Release 多挂 asset |

**C1 解压五字段基准**（7za 26.03 x64；payload = 0.20.0 解压目录，2267 文件 / **468.86 MiB**（= 491.64 MB 十进制；下表体积/内存全统一 **MiB**（/1048576），与账本 total 468.86 一致）：

| 方案                          | 压缩体积 MiB   | 压缩耗时         | 解压耗时      | **解压峰值内存**    | 成品份数     | 退出码 |
| --------------------------- | ---------- | ------------ | --------- | ------------- | -------- | --- |
| B: LZMA2 256M 非solid        | 154.54     | \~77.7s      | 10.1s     | **116.2 MiB** | 2267     | 0   |
| **C: LZMA2 256M solid（主推）** | **136.59** | \~100.9s     | 10.6s     | **342.1 MiB** | 2267     | 0   |
| ~~D: LZMA2 64M solid~~      | ~~145.27~~ | ~~\~108.4s~~ | ~~11.0s~~ | ~~351.0 MiB~~ | ~~2267~~ | 0   |
| E: zip deflate mx9（保底）      | 192.59     | \~62.5s      | 4.7s      | **10.7 MiB**  | 2267     | 0   |

> **D 组（LZMA2 64M solid）已排除**：四维度全部劣于 C（体积 +8.68、压缩耗时 +7.5s、解压耗时 +0.4s、解压峰值内存 +8.9 MiB）。其存在理由是"内存受限折中"，但实测内存反比 C 高——264 与 256M 字典应同属 solid 解压常驻窗口，该差异为测量噪声；在现有数据下 D 无任何维度优于 C，不作为产品选项。若后续复核确认小字典确能降内存，再回收此档。

> 产品决策关键点（D 已排除后**三选一**）：
>
> - **C：7z LZMA2 256M solid**，136.59 MiB / 342 MiB 内存 / **−31.3%** ← 体积最优
>
> - **B：7z LZMA2 256M 非solid**，154.54 MiB / 116 MiB 内存 / −22.3% ← 平衡点（+17.95 MiB 体积 换 −226 MiB 内存；面向普通玩家机器交换比划算，评审倾向推荐主推）
>
> - **E：zip deflate mx9**，192.59 MiB / 10.7 MiB 内存 / −3.1% ← 零新增工具依赖
>
> 保底档依赖口径须注明：E（192.59 MiB）是 7za 压出的 zip；若要"零新增工具依赖"，保底退化为现状 A（bsdtar，198.79 MiB），与 E 差 6.20 MiB。立项时明确选 E 还是 A。
> A 组（现状 bsdtar zip）解压耗时/峰值内存**未测**——可见 `build\release\release-size-report.md §6` 标注待补；如需"换 7z 后用户体验变化"参照系，立项时补 A 的解压成本。
> 数据源：`build\_c1_extract\extract-bench.json`（测量脚本已清理）。

### 旧编号 → 语义名映射（供下轮反查，勿据此重开）

> 旧会谈编号 `A#/B#` 无语义、跨轮可能被不同实验重复占用。下列编号已在会谈记录中给出
> 语义名与实测并全部落库，故**不做"待复命/不可复用"处理**——它们是结清项，仅供下轮
> 翻到旧编号时反查语义名与结论，**不要据此重新调查**。

| 旧编号 | 语义名                | 当前状态            | 证据所在表               |
| --- | ------------------ | --------------- | ------------------- |
| A1  | SatelliteResources | CLOSED-NEGATIVE | 见「CLOSED-NEGATIVE」表 |
| A4  | NonX64Artifacts    | CLOSED-NEGATIVE | 见「CLOSED-NEGATIVE」表 |
| B7  | ShapelyGeos        | KEEP            | 见「KEEP」表            |

## 验证状态

- 最后验证版本：`0.20.0`（新增 exp8 ORT offline toolchain \~0.96 MB、exp9 cv2 ffmpeg 29.45 MB）

- 关联 baseline：**`0.20.0`（total 468.86 MB / zip 198.79 MB）**——由 exp7（503.23/212.98）在 exp8+exp9 落地后升级；`assemble.ps1` 8 段的 size gate 常量已同步为此基线。
  实测验证：0.20.0 total 468.86 / zip 198.79 MB

- 反向验证（防误删）由 `assemble.ps1` 5.5 段自动执行：确认应删项不存在、应保留项存在。

- **rapidocr 模型完整性守卫（B4′-0 教训）**：`assemble.ps1` 5.5 段现对 det/cls/rec 三个 `.onnx` 逐项校验「存在且 > 0 字节」，不再仅 `Test-Path` 目录——空/截断 models/ 会直接 PRUNING-REGRESSION 失败，杜绝"空目录放行 → OCR 静默失效"。

- Size gate：`assemble.ps1` 8 段，与 baseline（0.20.0）对比，**仅"正向变大" > 5 MB 判 SIZE REGRESSION**（delta<0 为新增裁剪收益，不算回归）。

