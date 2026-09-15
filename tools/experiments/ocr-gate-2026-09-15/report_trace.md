# trace 复验：trace.jsonl

总记录 8598 行

## decision_snapshot 样本
```
{"facts_projection": {"stage": "session", "popup_kind": "hall_session_cards", "session_decision": {"key": "session_master_badge", "center": [0.43813122677289107, 0.3139260304982642], "hint": "意图: 目标场次「大师场」→ 先点击场次标签选择场次", "score": 0.0}, "appraiser_decision": null, "bidding_decision": null, "frame_counter": 1, "settle_income": null, "clicked_once": false, "retry_count": 0, "cooldown": 0, "daily_high_score": null, "egg_reading": false, "egg_read_done": false, "retry_elapsed": 1, "reward_elapsed": 1, "skip_cycle": 1}, "decision": {"key": "session_master_badge", "source": "session_decision", "paylo
{"facts_projection": {"stage": "session", "popup_kind": "hall_session_cards", "session_decision": {"key": "session_master_badge", "center": [0.43813122677289107, 0.3139260304982642], "hint": "意图: 目标场次「大师场」→ 先点击场次标签选择场次", "score": 0.0}, "appraiser_decision": null, "bidding_decision": null, "frame_counter": 2, "settle_income": null, "clicked_once": false, "retry_count": 0, "cooldown": 0, "daily_high_score": null, "egg_reading": false, "egg_read_done": false, "retry_elapsed": 2, "reward_elapsed": 2, "skip_cycle": 2}, "decision": {"key": "session_master_badge", "source": "session_decision", "paylo
```

## 快照内字段（出现次数，前 40）

- `facts_projection.stage`：4192   例值='session'
- `facts_projection.popup_kind`：4192   例值='hall_session_cards'
- `facts_projection.session_decision.key`：4192   例值='session_master_badge'
- `facts_projection.session_decision.hint`：4192   例值='意图: 目标场次「大师场」→ 先点击场次标签选择场次'
- `facts_projection.session_decision.score`：4192   例值=0.0
- `facts_projection.frame_counter`：4192   例值=1
- `facts_projection.settle_income`：4192   例值=None
- `facts_projection.clicked_once`：4192   例值=False
- `facts_projection.retry_count`：4192   例值=0
- `facts_projection.cooldown`：4192   例值=0
- `facts_projection.daily_high_score`：4192   例值=None
- `facts_projection.egg_reading`：4192   例值=False
- `facts_projection.egg_read_done`：4192   例值=False
- `facts_projection.retry_elapsed`：4192   例值=1
- `facts_projection.reward_elapsed`：4192   例值=1
- `facts_projection.skip_cycle`：4192   例值=1
- `decision.key`：4192   例值='session_master_badge'
- `decision.source`：4192   例值='session_decision'
- `decision.side_effects`：4192   例值=[]
- `fatal`：4192   例值=None
- `facts_projection.appraiser_decision`：4021   例值=None
- `facts_projection.bidding_decision.state`：2795   例值='S0_transition'
- `facts_projection.bidding_decision.key`：2795   例值=None
- `facts_projection.bidding_decision.center`：2795   例值=None
- `facts_projection.bidding_decision.hint`：2795   例值='回合转场中，等待动画稳定...'
- `facts_projection.bidding_decision.score`：2795   例值=0.0
- `facts_projection.bidding_decision`：1397   例值=None
- `facts_projection.session_decision.center`：202   例值=[0.43813122677289107, 0.3139260304982642]
- `facts_projection.appraiser_decision.key`：171   例值=None
- `facts_projection.appraiser_decision.center`：171   例值=None
- `facts_projection.appraiser_decision.hint`：171   例值='选择鉴宝师转场中，等待画面稳定...'
- `facts_projection.appraiser_decision.score`：171   例值=0.0
- `facts_projection.appraiser_decision.box`：29   例值=[0.18125, 0.4263888888888889]
- `decision.payload.center`：26   例值=[0.49998282672162125, 0.8143414047741714]

## 出价相位分布（共 2795 帧）

- `S4_wait_result`：736
- `S1_waiting`：716
- `S3_edit_type`：437
- `S3_smart`：320
- `S3_confirm_price`：231
- `S3_edit_clear`：137
- `S2_bid`：81
- `S3_need_h`：68
- `S0_transition`：65
- `S3_wait_phase`：2
- `S4_fake_fallback`：1
- `S3_edit_wait_ocr`：1

## 出价决策 hint 分布（前 15）

- `等待出价按钮亮起...（OCR=等待出价）`：346
- `等待出价按钮亮起（OCR=?）...`：254
- `意图: 面板已开 H 未读 → 点智能出价（√S=1.00）`：170
- `等待公布第 1 回合报价...（epoch#1）`：160
- `等待公布第 2 回合报价...（epoch#2）`：156
- `等待公布第 3 回合报价...（epoch#3）`：144
- `意图: 出价按钮已亮（OCR=出价）→ 点出价`：81
- `等待公布第 4 回合报价...（epoch#5）`：69
- `[observe] 输入框为空，等待填入建议价或默认值...（策略建议 228,300）`：68
- `回合转场中，等待动画稳定...`：65
- `等待公布第 2 回合报价...（epoch#3）`：58
- `等待公布第 4 回合报价...（epoch#4）`：58
- `意图: [target_second] 输入 3（已 空 → 目标 338,688）`：52
- `等待公布第 3 回合报价...（epoch#4）`：51
- `等待出价按钮亮起（OCR=已出价）...`：50

## 按「阶段 × 回合」的相位分布


## 同一相位连续 ≥150 帧的段落（旧的「静默锁死」形态，期望大幅减少）

- 无