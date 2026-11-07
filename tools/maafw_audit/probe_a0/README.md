# A0 实验：`probe()` 只读链路的放行前置

对应 [ADR-0023](../../../docs/adr/0023-contract-core-页面查询走只读接口与存盘声明继承.md)
的放行验证（A0-1 链路与收尾语义、A0-2 同帧等价），以及其实施后的回归复核。
本目录只做实验，不改生产代码。结论已回写方案；脚本留在这里供复核。

## 怎么跑

```bash
# A0-1 链路 + 传帧 + 通道
python tools/maafw_audit/probe_a0/step3_real_reco.py

# A0-1 超时 / 停止 / 迟到回调竞态
python tools/maafw_audit/probe_a0/step5_race.py

# A0-1 收摊失败、桥异常、入口识别失败
python tools/maafw_audit/probe_a0/step6_edges.py
python tools/maafw_audit/probe_a0/step7_recover.py

# A0-2 同帧逐帧等价（帧段可调）
python tools/maafw_audit/probe_a0/step4_equivalence.py 880 900
python tools/maafw_audit/probe_a0/step4_equivalence.py 1405 1425
python tools/maafw_audit/probe_a0/step4_equivalence.py 2798 2815

# A0-1 通道量化 + 单次开销
python tools/maafw_audit/probe_a0/step8_channel.py

# A0-1 结果提交的有效性检查（tombstone 对照）
python tools/maafw_audit/probe_a0/step9_commit.py

# A5 端到端：真实 maafw 跑 NavGraph.probe（不是桩）
python tools/maafw_audit/probe_a0/step10_probe_e2e.py

# A6 离线重放：真实录制帧上的命中序列、单次耗时、主循环占用
python tools/maafw_audit/probe_a0/step11_replay.py

# T1 端到端：save="self" 时真实 run() 开的 nav 段零存盘帧
python tools/maafw_audit/probe_a0/step12_t1_nav_no_frames.py

# 存盘策略对照：继承 self 零帧 / 显式 save=on 落图（段声明与 NAV_SAVE_FPS=3）
python tools/maafw_audit/probe_a0/step13_nav_save.py
```

依赖会话 `20261106_185832` 的 nav 调试帧（`debug/<会话>/speedrush/nav`，缺省按
`%APPDATA%/MaaRacingMaster` 解析；换会话或换数据根用环境变量 `MAAR_A0_NAV`
直接指向 nav 目录）。
`step1_boot.py` / `step2_chain.py` 是最早的探测版本，已被 step3 取代，保留只为对照。

## 帧源怎么来的

本机 maafw 5.12.3 **不带** `MaaDbgControlUnit.dll`，`DbgController` 建不出来。
改用 `CustomController`（`MaaCustomControlUnit.dll` 在包内）自实现 `screencap`，
每次返回脚本指定的同一张帧——帧身份由脚本保证，正好满足 A0-2 的「同一次取帧」。

识别侧注册的是项目**真实**的 `TemplateRecognizer`（`core/nav_graph.py`），
graph 依赖用最小替身补齐（`image_dirs` / `frame()` / `_log` / `_publish_nav`），
所以两条路径走的是同一份识别实现，不是复刻。

## 结论

### T1 端到端（`step12_t1_nav_no_frames.py`）

真实 `NavGraph.run()` + `NavigationDebugger`，栈底 scope 声明 `save="self"`：

- `run` 返回 True，nav 段产出 3 条边界事件（`segment_start` / `stage` / `segment_end`）；
- **磁盘上 0 个 jpg**——内层 nav 段继承了栈底策略，不再按默认 `"on"` 落帧。

这是 A4（继承）+ A1b（`save="self"` 零存盘）在真实链路上的组合验证。实机目录扫描
（A1a：nav 无帧、drive 有帧、无第三处产帧）仍需跑一局。

### A6：离线重放（`step11_replay.py`）

会话 `20261106_185832` 的真实录制帧，逐帧跑 `probe()`：

| 段 | 命中帧 | probe 中位 | 占控制拍 |
|---|---|---|---|
| 880~900 驾驶页锚点 | 895~900（画面 895 帧切页） | **16ms** | 0.3 / 10 拍 |
| 1405~1425 商店横幅 | 1412 / 1419 / 1421 | **16ms** | 0.3 / 10 拍 |

- 命中序列与画面切换**零滞后**：894 帧仍 miss，895 帧 hit。
- 按用户裁决不测得分率（跳转是成功/失败二值，不参与操作质量）。

**首版曾有一个实机回归（2026-11-07，已修）**：probe 的入口 override 漏写
`pre_delay` / `post_delay`，吃框架默认各 200ms，每次查询 420ms。实机每拍两次查询
（锚点 + 横幅）= 800ms，把驾驶主循环从 20Hz 压到 **1.1Hz**（tick 中位 880ms），
深度帧 100% 超龄（投喂间隔 > 350ms 预算）、视角确认失败、阶段中止。
补齐零节拍后 probe 降到 16ms。回归锁：`test_probe_entry_override_zeroes_framework_pace`。

诊断方法（`step13_cost.py` / `step14_node_diff.py`）：对比**框架解析后的节点数据**
（`Resource.get_node_data`）能直接看出 `pre_delay=0` vs `200`，比逐项猜 override
形态快得多——那次排查里节点名、文件、识别类型、action、timeout、attach 全是烟幕。

### A0-1：B 链路

| 项 | 实测 |
|---|---|
| 链路 | `post_task(入口, pipeline_override)` → `CustomAction.run(context, argv)` → `context.run_recognition(节点, 帧)` → `RecognitionDetail.hit` 全程跑通，`context` 在回调里可得 |
| **override 键路径** | `custom_action_param` 必须传**结构化 dict**。传 `json.dumps(...)` 字符串会被二次编码，桥收到 `'"{\\"probe_id\\": ...}"'` |
| 传帧生效 | 反向验证：controller 喂商店帧、桥传驾驶帧 → 命中；controller 喂驾驶帧、桥传商店帧 → 未命中。证明 `run_recognition` 用的是传入帧，不是 controller 截图 |
| 通道契约 | `analyze` 内无条件 `BGR→RGB`，故传入帧必须是 BGR。误传 RGB 在 12 个边界帧上判定与 box **全等**（这两个节点对通道不敏感），但契约仍写 BGR |
| **桥异常** | 桥抛异常，`job.succeeded` 仍为 **True**。不能靠任务状态判断有没有结果，桥必须自己往容器写 error |
| 任务失败 | 入口识别恒不命中：`succeeded=False`，入口识别按 `rate_limit` 循环到 `timeout`（100ms × 约 9 次） |
| **超时 / 停止** | `post_stop()` **不打断**正在执行的 CustomAction，回调会跑完；`stopping=True` 期间新的 `post_task` 被**直接忽略**（框架日志 `check_stop: stopping, ignore new post`），不是排队 |
| 恢复 | `stopping` 变回 False（旧回调退出后）新任务立即正常 |
| **提交有效性** | 无检查时，超时置 `stale` 的记录会被迟到回调**复活**为 `completed`；加「仅接受 `pending`」检查后同一时刻为 `DROPPED`，框架恢复后清墓碑、新调用正常 |
| 单次开销 | `probe` 中位 **437ms**（p90 438ms），与 `run` 同源同量级 |

### A0-2：同帧等价

同一帧、同一 controller、两条路径（框架识别轮 vs `run_recognition`）：

| 帧段 | 驾驶页锚点 | 商店横幅锚点 | 不一致 |
|---|---|---|---|
| 880~900 | 命中 895~900，其余 miss | 全 miss | 0 |
| 1405~1425 | 全命中 | 命中 1412/1419/1421，其余 miss | 0 |
| 2798~2815 | 全命中 | 命中 2804/2811，其余 miss | 0 |

共 28 帧、两个锚点，**命中序列与命中框逐帧全等**。

## 对方案的修正（四条）

1. **「收摊失败 → 标记 stale → 新调用用新 id」在框架层走不通**：`stopping=True` 时新
   `post_task` 被忽略，不是排队。所以 `probe()` 超时后必须**有界等到 `stopping=False`**
   才返回；等不到只能报 `framework_not_ready`，且调用方不得立刻重试——否则重试次数被空耗。
2. **`job.succeeded` 不能作为「有没有结果」的判据**（桥异常仍 True）。结果只能以容器里
   有没有本次 `probe_id` 的记录为准，桥异常由桥显式写 error。
3. **override 传 dict**，不传字符串。
4. **只标记 `stale` 不够**：提交必须带「仍为 `pending`」检查，否则迟到回写会重建/复活记录；
   收摊失败时**不能删墓碑**，只能在确认 `stopping=False` 后清理。锁只保护记录状态转换，
   不保护框架调用。

## 没证到的

- 实机（真实 WGC 帧源 + 真实手柄/鼠标）下的耗时与稳定性。本实验是离线帧源。
- `probe` 437ms 的开销对驾驶主循环 0.5s 锚点复查的挤占，要实机量（A6）。
- **同一 `NavGraph` 实例上 `run()` / `probe()` 的并发入口**：这里证明的是「框架在
  `stopping` 期间忽略新任务」，属框架侧行为，推不出「调用方不会并发调用」。该项仍是
  调用方前置条件，不是已验证事实。
- `_wait_drive_ready()` 所用节点的同帧等价：A0-2 只覆盖驾驶页锚点与商店横幅锚点，
  就绪判据的节点不在其中，故本次不替换它。
