# maafw-docs-constraints — 官方文档论断在 5.12.3 的实测校准

status: active


回答「文档写的外部系统行为在**本项目实际安装的版本**上成立不成立」。
文档口径 = maafw\.com（main，v5.13.0 后）；被测 = `.venv` 内 MaaFw 5.12.3。

## v1\_duplicate\_register.py — Custom 注册命名空间

复现：`& .venv\Scripts\python.exe tools/experiments/maafw-docs-constraints/v1_duplicate_register.py`（离线，假控制器黑帧，约 3s）

**文档口径（2.2，v5.13 起写明）**：识别与动作共享一个区分大小写的命名空间；
空名或重名 → 返回 false 且**保留已有注册**；替换须先 unregister。

**5.12.3 实测（条件成立→文档口径不成立，行为不同）**：

| 实验点                                 | 文档预期          | 5.12.3 实测                                             |
| ----------------------------------- | ------------- | ----------------------------------------------------- |
| A1 识别器同名二次注册                        | false + 保留旧注册 | **True**；B1 实测任务命中**新实现（覆盖式）**                        |
| A2 跨类型同名（recognition 占用后再注册 action） | false         | **True**，两类并存，未观察到共享命名空间互斥                            |
| A3 空名注册                             | false         | **True**（不可信返回值）；C 侧仅打日志 `empty name or handle` 并拒绝注册 |
| A4 仅大小写不同                           | 允许            | True（一致）                                              |
| B2 覆盖注册后旧实例被 GC                     | （潜在悬垂）        | 稳定命中新实现，无崩溃——覆盖生效后旧实例不再被 C 侧引用                        |
| C unregister → 再 register           | 生效            | True + 命中新实现（一致）；unregister 不存在的名也返回 True（幂等）         |

**结论与规范影响**：

1. 「热重载静默失效」坑在 5.12.3 **不存在**（重复注册即覆盖生效）；但 v5.13 文档口径相反，
   **升级 MaaFw 前需重跑本实验**。
2. 防御性规范（跨版本安全，两版行为下都正确）：**替换注册一律先** **`unregister_custom_*`** **再
   `register_custom_*`**，不依赖同名覆盖。
3. **返回值不可信面**：空名注册返回 True 但实际未注册——注册名必须是非空字面量/常量，
   pipeline 引用名与注册名的一致性由本项目 check\_truth 机检兜底。

