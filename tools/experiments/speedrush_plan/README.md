# speedrush_plan —— 横向轨迹规划标定与回放

status: active

motion_plan 架构（场景快照 → 时空横向轨迹评估 → 预览跟踪）的离线实验区。

## fit_plant.py

从控制 trace 拟合横向 plant 参数 `(a_lat_gain, tau_align_s, v_lat_max, tau_steer_s)`。

```bash
.venv/Scripts/python.exe tools/experiments/speedrush_plan/fit_plant.py \
    --trace <trace.jsonl> [--trace ...] [--out report.json]
```

口径：

- 似然 = 一步转移 + 生产同款 α-β 观测模型；修正拍不剔除。
- 留出验证按整段 trace 留一；参数 CI 用 Jacobian 近似（仅作退化检查）。
- `sigma_ego(t)` 由多步开环残差按 horizon 拟合，保守含路心观测噪声。
- 缺 `steer_raw` 列的旧 trace 只能拟合三个参数，`tau_steer` 不可辨识，
  `calibrated=false`；需要新录一局带 `steer_raw` 的 trace 才能解锁。
- `calibrated=false` 时 `motion.mode=live` 必须 fail-loud（影子/离线不受限）。

## replay_plan.py

读带 `scene/plan` 列的控制 trace，按录制顺序回放 `plan_motion`（gate 同构），
输出确定性对齐率、`plan.valid` 覆盖率、层分布、预览抖动与单拍 P99。

```bash
.venv/Scripts/python.exe tools/experiments/speedrush_plan/replay_plan.py \
    <trace.jsonl> [--out summary.json]
```

## 待建

- A3 离线指标对比脚本（金币层占比/预测碰撞/走廊余量 vs 旧选择基线）。
