# 日志卡重设计试验区（log_card_lab）

status: active

## 目的

在**原版界面基线**上一步步重建运行日志卡的设计：卡片样式、卡头层级、入场动画、
回顶按钮、滚动指示。基线 = 真 `index.html` 日志卡的逐字复刻（渲染管线移植自
`js/log.js`），喂**分级重划后**插件实际送进 GUI 的流（措辞取自 `plugins/treasure` 真行）。

## 协议（维护者定的，不得偏离）

1. **试验区即原版**：`index.html` 未进入某步设计改造前，必须与生产观感/行为逐字一致；
   禁止预引入非原版元素再改。
2. **每步一提交**：每个设计改动 = 一个 commit（只碰本目录），消息写清改了什么、为什么。
3. **有问题就回撤**：`git revert` 对应步，不在坏状态上继续叠。
4. **出口**：设计定稿后并入 `apps/MaaRacingMaster.Shell/frontend/`（style.css / js/log.js /
   index.html），删除本目录，主改动收成一个提交。

## 步骤日志

- step 0（`2b5e2c7`）：原版基线立起——单卡、默认折叠、真管线、重划后流、尾部活体滴入。
- step 1（`628271c`）：卡头唯一状态槽（学 v-collapsible-9 的自洽性：一行一个状态元素、槽位常驻、
  含义由词承载）——「进行中/成功/警告/失败」词徽章常驻行首；三色类型边线废除
  （类型维度由标题关键词着色承担）；计数徽章与「!」标记废除（意义收进状态词，
  右侧只剩时间+箭头）；标题单行省略 + title 悬停全文；「进行中」用 info 蓝呼吸脉冲
  （primary 赛车红与「失败」红语义撞车，色彩语义互斥是底线）。
- 设计稿 v1→v2（`61cbbfb`→`81c2eb2`）：外部审查 12 项采纳、3 项反驳（前端标签表破坏解耦 /
  v1 平铺不做嵌套 / 删 source+event_id 冗余），契约冻结见 `DESIGN_log_api.md`。
- 契约 §8 第 2 步已实施（`3671551`，维护者批准范围：仅 core/logger + 单测）：记录化缓冲、
  GroupHandle 生命周期、双投影（GUI 文本字节兼容）、fields 净化管道、close 补 incomplete。
  三点实施裁决入代码：shutdown=Logger.close()（sidecar 唯一退出路径）单次执行；协议字段
  统一 schema_version；end 首次返回终态、重复幂等回读。插件迁移 / fetch_logs / 前端未动。
- §8 第 4-6 步已实施（`ed0cd25`）：`get_events_since` 结构化通道 + `fetch_logs` 双格式
  （legacy lines 逐字节兼容派生）+ 前端双 renderer（组事件驱动卡片；无 group_id 散文行
  回灌 legacy 锚点管线，迁移期未迁生产者观感不变）。桥桩探针端到端过。
- 第 7 步已实施（`92f03b6`，维护者裁定 A 案 + 边界规则五条）：模块级
  `_tlog/_open_grp/_end_grp` 显式句柄机制、锚点转组（标题逐字保留）、STAGE_FLOW
  21 处机械注入、跨切面 16 处有意无组、run finally 落 incomplete、彩蛋正常收尾显式
  success；结构性锁测试禁止 INFO+ 裸 logger.log 逃逸无组白名单。worker 派发捕获
  group_id / 迟到旧 id 降级两锁入 logger 测试。351 相关回归全绿。
- §8 第 8 步已实施（本 commit）：core 行（已连接窗口/断点模式/紧急停止）与
  pipeline_logger **有意保持无组**（sink 回调线程不读可变当前组——裁定规则 2；
  跨切面无业务归属——规则 4），零 core 改动；前端补双管线交接锁：group_start
  开卡即 finalize legacy「当前卡」，框架散行自开 implicit 新卡，不再埋进
  「已连接窗口」旧卡（桥桩混合序列端到端验证）。
- 待做：第 9 步（speedrush 自接组机制后删 SECTION_ANCHORS/KW_RULES——阻塞于
  speedrush 维护者）、第 10 步（复制/导出含 seq/group_id、协议一致性测试）、
  第 11 步（结论迁 home、目录退役）。卡片样式线（入场动画、回顶按钮）可并行。
