status: active

# speedrush_state — 目标状态能否在 MoGe 锚点之间“插帧”

## 目的

MoGe 锚点约 12~15 Hz 到达，控制拍 20 Hz，锚间距离由“保持”填满；横向米制
`x = (cx−vpx)·z/fx` 里 z 一跳横向就跟着跳。选择层又需要提前 0.5~0.8 s 知道
“目标在哪条道、还有多远”，当前供数给不出稳定的提前量。

本实验只回答一个问题：**用 20 Hz 的 YOLO 框高做相对尺度，能否把 MoGe 的
z / 横向在锚点之间插值到可用**。不验证、也不实现任何决策改动。

## 方法（只读 trace，不跑感知）

输入是生产 trace 的 `mcars` / `car_h` / `scene.road.bands` 三列：

- **z 真值**：每轨相邻的两个 `ANCHOR` 值。锚点之间没有真值，所以评估点取
  ①下一个锚点到达时刻、②锚点曲线线性插值跨过 20 m / 10 m 的时刻。
- **被测方法**（同一评估点上比较）：
  - `HOLD`：保持上一锚（≈今日生产在未确认接近时的行为）；
  - `CV`：锚值 − v_close·Δt（≈今日 `RESCALED_ANCHOR`）；
  - `BOX`：`z0·h0/h(t)`，只在框新鲜、且框高未发生 >35% 单拍翻跳时有效；
  - `FUSE`：`CV` 与 `BOX` 的均值（BOX 失效时退 `CV`）；
  - `BOXV`：用框高比反推区间速度，再按该速度外推（不直接用框的绝对距离）。
- **横向**：`x` 按锚点 z 缩放后做滞回锁道；统计远距可得率、逐拍跳变、
  锁道翻转次数，以及“到达 40/30/20/15 m 前，车道可读的提前量”。
- **道路方向**：3~18 m 带的中心线做一次线性拟合取切线角，统计覆盖率与
  逐拍抖动；不做二次/三次外推（既往实测高阶外推过拟合）。

## 判据（先定，后跑）

1. **z 桥接有用**：`FUSE` 在下一锚点与 20 m 跨点的绝对误差中位 ≤ 0.7×`HOLD`，
   且 p90 不高于 `HOLD`；`BOX` 有效率（框高不翻跳、框新鲜）≥ 60% 区间。
2. **横向锁道可用**：20~40 m 带横向可得率 ≥ 70%；锁道翻转 ≤ 0.3 次/轨/10 s；
   20 m 与 15 m 处的锁道与最终锁道一致率 ≥ 85%；车道可读提前量中位 ≥ 0.5 s。
3. **道路切线可用**：近带线性拟合覆盖率 ≥ 80%，逐拍切线角变化 p95 ≤ 1.0°。
4. 三条全不成立 → 不再在锚点/框高上加工，转标线检测；只成立 z 桥接 →
   只做距离插值，横向继续等新的独立源。

## 运行

```bash
.venv/Scripts/python.exe tools/experiments/speedrush_state/probe_state_est.py \
    --trace "%APPDATA%/MaaRacingMaster/debug/speedrush/20261023_132204_p1/trace_20261023_132204_p1.jsonl" \
    --trace "%APPDATA%/MaaRacingMaster/debug/speedrush/20261023_132606_p2/trace_20261023_132606_p2.jsonl"
```

不带 `--trace` 时读取数据根下最新的 4 个 `trace_*.jsonl`。原始结果写到
`%APPDATA%/MaaRacingMaster/data/speedrush/state/<时间戳>/state_est.json`。

## 边界

- 只读 trace，不改生产插件；脚本不 import `maaracing_master`。
- “锚点”不是地面真值，锚间误差含 MoGe 自身噪声；判据只比较方法间相对优劣，
  不宣称绝对米制精度。
- 结论先写本目录 `RESULTS.md`，成立后再谈是否进生产。
