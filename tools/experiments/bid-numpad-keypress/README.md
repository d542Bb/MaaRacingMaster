# bid-numpad-keypress — 出价面板数字键「按下 A 到底有没有生效」

**问题类型**：C 类（外部系统行为——游戏面板是否接受手柄数字键输入），未经实验不得写成结论。

## 起因（真机 2026-09-15）

日志 `MaaRM_20260915_071523.log` + 抽帧：

| 时刻 | 现象 |
| --- | --- |
| 07:17:28 | 点「智能出价」→ 输入框被填入 16,900（**生效**） |
| 07:17:31 | 点「✖ 清空」→ 输入框清空（**生效**） |
| 07:17:34 | 提交 `bid_numpad_2`（帧 987），光标停在 '2' 键中心 |
| 07:17:36 / 07:17:44 抽帧 | 光标规矩停在 '2' 键上，输入框**仍为空** |
| 07:17:34–07:17:58 | `_bid_input_progress` 不推进 → 指纹锁按设计不再重发 → 光标 24s 原地不动 |

同一面板上其它键生效、唯独数字键看不到效果，且光标位置正确，故需要真机分型。

## 实验（只按一次键，导航不参与）

光标位置由生产链路负责（GUI 的「仅意图」模式会把光标导航到当前点击意图上但不按 A），
或用户自己的手柄摆位；本探针只按一次键并给出**可客观判定**的前后帧差异。

```powershell
# 0) 基线：不按键，量出游戏自身动画的自然差异
.venv\Scripts\python.exe tools/experiments/bid-numpad-keypress/probe_keypress.py --title 巅峰极速 --dry-run --label baseline
# 1) 目标键：光标停在数字键「2」上
.venv\Scripts\python.exe tools/experiments/bid-numpad-keypress/probe_keypress.py --title 巅峰极速 --label numpad2
# 2) 对照键：光标停在「智能出价」上（已知生效）
.venv\Scripts\python.exe tools/experiments/bid-numpad-keypress/probe_keypress.py --title 巅峰极速 --label smart
```

输出：`out/<label>_NN_{before,after,diff}.png`（diff 为差异放大 4 倍图）+ 控制台
「变化像素数 / 占比 / 包围盒」。三组各跑 3 次取最大占比最稳。

## 判定矩阵

| numpad2 vs 基线 | smart vs 基线 | 结论 | 生产侧后续 |
| --- | --- | --- | --- |
| ≈ 基线 | ≫ 基线 | **(甲)** 数字键需要额外前置（先点活/聚焦输入框），游戏忽略裸数字键 | `S3_edit_type` 链路补前置动作 |
| ≫ 基线 | ≫ 基线 | **(乙)** A 与落点都正常 → 之前那次是导航落点/时序，或输入框读数问题 | 查「到位判定 + 按 A 时机」或 `(丙)` OCR 读区 |
| ≈ 基线 | ≈ 基线 | **(丙)** A 没送达该面板（输入通道/面板焦点） | 查 ViGEm 通道与面板焦点，不是数字键特例 |

## 结论

**待真机执行后填写**（含三组实测占比与包围盒、最终分型、复现日期）。

## 复现命令

见上文三条命令；脚本自包含，不 import `maaracing_master`（见 `tools/experiments/README.md`）。
