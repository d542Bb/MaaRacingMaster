# Release Size Report — 0.20.0

> 本文件是 `assemble.ps1` 的解压落盘产物（`$OutRoot\release-size-report.md`，默认 `scripts\release\`）。
> 与 `build\release\release-size-report.md` 内容一致（同一轮，双路径存档）。

- version: 0.20.0
- generated: 2026-09-05T13:08:40Z
- configuration: Release (all SAFE pruning on)
- git: ea9dfa1

## 1. 最终体积（单位为 MiB，PowerShell `Measure /1MB` = /1048576）

| part | MiB |
|---|---|
| runtime | 299.96 |
| app | 154.58 |
| sidecar | 1.67 |
| other | 12.66 |
| total unpacked | 468.86 |
| zip | 198.79 |

## 2. vs baseline（历史演进）

| 版本 | 起点 total | 起点 zip | Δ total | Δ zip | 说明 |
|---|---|---|---|---|---|
| 0.19.0 → 0.20.0 | 499.24 | 211.37 | **−30.41** | **−12.59** | 本轮 exp8+exp9 落地（对应 §5） |
| 上一轮（exp7 前） | 503.23 | 212.98 | −3.99 | −1.61 | 历史，移入此段，非本轮口径 |

> size gate 基线现为 **0.20.0（468.86 / 198.79）**——exp8+exp9 落地后升级；涨超 5 MiB 判 REGRESSION。
> 起点统一为 0.20.0 之后，连续构建之间 delta 应为 0（新裁剪再降）。

## 3. Regression
no regression（本轮 total −30.41 / zip −12.59 为 exp8 EXP-9 新增裁剪收益，非膨胀；size gate 已修正为仅"变大"判 regression）

## 4. 构建来源与裁剪（0.20.0）

- Production pruning：**12 项全部执行**；Guard/反向验证无 PRUNING-FAIL / PRUNING-REGRESSION。
  新增 2 项 = exp8 ORT offline tooling（google/protobuf 0.88 + flatbuffers 0.08）、exp9 cv2 videoio ffmpeg（opencv_videoio_ffmpeg500_64.dll 29.45 MB）。
- 依赖版本：requirements-runtime-lock.txt 锁定 rapidocr==3.9.2 / onnxruntime-directml==1.24.4 等。

## 5. Smoke test（正式产物实测）

| 项 | 结果 |
|---|---|
| Python import self-check（assemble 内置） | PASS |
| NumPy（linalg/random/fft） | PASS |
| OpenCV 5.0.0（cvtColor/putText/imencode/imdecode/dnn.NMSBoxes） | PASS |
| ORT/DML 真实推理（model.onnx, DmlExecutionProvider） | PASS |
| RapidOCR（构造+推理） | PASS |
| windows_capture / vgamepad | PASS |
| MaaFramework Toolkit/Tasker/Resource + 桌面枚举 | PASS |
| Win32Controller（MaaRM 实际控制路径） | PASS |
| Racing（manifest/module/loop） | PASS |
| Treasure（manifest/module/ocr/strategy/store + 业务逻辑） | PASS |
| yolo_detector / wgcap | PASS |
| 截图 WgcCapture | NOT VERIFIED（hwnd=0 环境限制，与历次口径一致） |
| GUI | NOT FULLY VERIFIED（requireAdministrator，不弹 UAC） |

### EXP-9 JSONL 污染专测（本轮新增，0.20.0 产物实测）

删除 ffmpeg dll 后跑真实 `sidecar` 会话逐行校验：stdout 全部为合法 JSONL（**非协议行 0 行**），10 RPC 全 `ok=true`，shutdown rc=0；stderr 仅 opencv_utf8_patch 提示，无 ffmpeg 告警。

### B4′-0 发布完整性核验（本轮新增，0.20.0 产物实测）

release 携带 rapidocr models 齐全（det 9.47 / cls 0.558 / rec 20.251 MiB），三个文件与原始 wheel SHA256 逐字节一致（非 0 字节、非截断）；断网 + 干净 models 副本 → `RapidOCR()` 构造成功。

## 6. C1 压缩格式基准（已立项，落地独立于本节）

详见 `runtime-pruning-policy.md`「ArchiveFormatSolid」节的 C1 解压五字段表。要点：
- 主推 **7z LZMA2 256M solid = 136.59 MiB（−31.3%，基 0.20.0 起点 198.79）**；zip 保底双产物，非 SFX。
- 解压峰值内存：solid 342–351 MiB，**zip 仅 10.7 MiB**（产品折中关键）。
- 叠加形态后 Download **211.38 → 136.59 MiB（−74.79 / −35.4%，基 211.38）**。
- 收益分解 = 算法 −22.3%（154.54）+ 固实额外 −17.95 + 档位 −3.1%（192.59）；**solid 是唯一关键变量**。

## 7. 最终结论
Production Release 0.20.0 = **READY**；baseline 已由 0.19.0 升级为 0.20.0（468.86 / 198.79）。