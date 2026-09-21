# 日志结构化 API 契约（v2 · 契约冻结稿，待维护者批准实施）

> 起因：step 1 后逐条改日志文案被维护者否决——「到处改输出是打补丁，需要 API 级规范与 GUI 联动，核心逻辑要复用」。
> v1（研究稿）经外部审查后重写为本稿：补齐生命周期、状态模型、字段约束、RPC 契约、异常语义、兼容与迁移顺序。
> 信源结论不变（Console API / GitHub Actions 流内命令 / OTel 数据模型 / MaaFW 现状，见文末附录）：**结构进 API，呈现归渲染器**。

## 1. 事件模型（单一真源）

环形缓冲改存结构化记录 `(seq, record)`，文本行与 GUI 事件都是它的**投影**。记录字段：

```json
{
  "schema_version": 1,
  "seq": 42,
  "event_type": "group_start | group_end | log",
  "group_id": "g-<seq>",
  "session_id": "20260921T140211-<rand>",
  "ts": "14:02:15",
  "channel": "treasure",
  "level": "INFO",
  "kind": "phase | session | loop | null",
  "title": "拍品观察",
  "message": "识别到目标",
  "fields": {"item_name": "汝窑天青釉"},
  "outcome": "running | success | warning | failure | incomplete | null"
}
```

- `title` 仅 group 事件有，只用于卡头；`message` 是事件文本；**中文是显示值，`kind`/`outcome`/`channel` 是稳定机器枚举**——API 分类值一律英文。
- v1 **平铺不嵌套**：无 `parent_group_id`（出现真实嵌套消费者再加字段，向后兼容）；多开组靠 `group_id` 天然支持。
- 不设 `source`/`event_id`：`channel` 与 `seq` 已覆盖。

## 2. 生命周期与并发契约（P0 补齐项）

```python
g = logger.group("拍品观察", kind="phase", channel="treasure")   # 返回 GroupHandle
g.log("识别到目标", "INFO", fields={"item_name": "汝窑天青釉"})
g.end()                                    # 不传 outcome → 按组内事件自动推导

with logger.group("大师场", kind="phase") as g:   # 上下文糖：正常退出=自动推导，
    ...                                          # 抛异常=outcome=failure 后**原异常照抛**

logger.log("Pipeline 动作失败", "WARNING",
           group_id=g.id, fields={"node": "bid_confirm_red_btn"})   # 跨线程显式挂组
```

规则（全部有单测锁定）：

| 场景 | 行为 |
|---|---|
| 并发/跨线程 | 组状态表挂在既有 `_lock` 下；**不依赖线程隐式栈**，异步路径必须显式 `group_id` |
| `end()` 重复调用 | 幂等：第二次忽略，内部计数 +1（不回灌日志，防递归） |
| `group_id` 不存在/已关 | 事件降级为无组 `log`，内部计数 |
| 未收尾组 | shutdown 时统一补 `group_end(outcome="incomplete")`；GUI 显示「未完成」灰态 |
| `fields` 序列化失败 | 保 message 丢 fields，内部计数；**日志失败永不抛进业务线程**（沿现有纪律） |
| 参数非法（level/kind 不在枚举） | 开发期 assert；运行时回落 null/默认值并计数 |

## 3. 状态模型（过程态与终态分离）

- `group_start` 记录 `outcome="running"` → GUI 状态槽「进行中」；
- 终态由 `group_end(outcome=...)` 给出；**不显式传时自动推导**：组内有 ERROR→`failure`，否则有 WARNING→`warning`，否则 `success`；
- 显式 `outcome="success"` 与组内出现过 WARNING **可以共存**——本项目 WARNING 惯例是「可恢复降级」，不必然污染整组终态；`has_warning/has_error` 作为推导输入记录在 end 事件里，GUI 可用于次级标记；
- 枚举定版：`running / success / warning / failure / incomplete`（不设 cancelled——用户停止导致的未收尾就是 incomplete）。

## 4. fields 约束

- 值只允许 JSON 标量（str/int/float/bool/None）；非标量 `str()` 强转；NaN/Inf → None；
- ≤16 键、键 ≤32 字符、字符串值 ≤200 字符，超限截断加 `…`；
- 键名用**英文机器名**（`completed_count` 而非 `累计`）；**GUI 通用渲染 `key: value`，不做翻译表**——前端维护标签表等于把解耦又焊回去（对审查建议 R1 的反驳）；
- 敏感数据规则不变：`input.password` 类字段禁入日志（fields 与 message 同规）。

## 5. channel 语义保留

`channel` 是**写入闸门**（生成前过滤，沿现状），结构化后仍是顶层字段：
- 组事件与组内事件各带自己的 channel；组默认继承 `group()` 调用点的 channel；
- 被 channel 闸门挡掉的事件**不产生记录**，因此不参与 outcome 推导——闸门语义先于结构语义。

## 6. fetch_logs 契约

```json
{
  "schema_version": 1,
  "session_id": "20260921T140211-a3f",
  "events": [ ... ],
  "next_seq": 1234,
  "truncated": false,
  "gap": null
}
```

- 游标语义不变：服务端 `_last_log_seq` 单调 seq（环形回绕不重不漏，沿现状）；`next_seq` 含被过滤记录占用的号段；
- 溢出补偿：`truncated=true` + `gap={from_seq, to_seq}` **显式事件**，不再伪装成 `[!!] 文本行`；前端渲染为「早期日志已截断」占位卡；
- 组开始事件已被环形冲出、正文还在：前端按「无头组」渲染（匿名卡收正文，不丢行）；
- WebView 刷新/重连：`since_seq=0` 全量重放即可恢复未完成组（记录含 running 态）；
- 多客户端：现状单 GUI 消费者，游标服务端持有；预留可选 `params.since_seq`（传入则不推进服务端游标），不实现多订阅扇出。

## 7. 落盘投影（文本兼容）

`.log` 文件保持人类可读单流全量按序：
- `log` → `[ts] [LEVEL] msg`（现状不变，grep/测试零影响）；
- `group_start/end` → `::group:: 标题` / `::endgroup:: success`（GH Actions 风格）；标题/消息中 `%`→`%25`、`:`→`%3A`、换行→`%0A`（沿 GH 转义规则）；
- 轮转（`.log.1/.2/.3`）允许把组拆开到不同文件——排障按 ts 串联，不做跨文件缝合；
- 不双写 JSONL（v1 无机器读盘消费者；出现时加伴生文件，不改主格式）。

## 8. 兼容与迁移顺序（采纳审查十条，微调两处）

1. 冻结本契约（=本文件，待批准）；
2. core/logger 实现记录化 + 单测：并发、重复 end、野 group_id、异常穿透、shutdown 补 incomplete、环形截断含无头组；
3. **保留** `log()/get_lines()/get_lines_since()` 旧签名（内部走投影）；
4. `fetch_logs` 过渡期**同时返回 `lines`（投影）与 `events`**，加 `log_protocol_version` 字段；
5. 前端加结构化 renderer，**legacy renderer 并存不删**；
6. treasure 迁移（组边界 = 现 SECTION_ANCHORS 的十处措辞锚点）；
7. sidecar `[sidecar]` 行、pipeline_logger 迁移（后者把 Notification 的结构化 details 放进 fields——对 v1 附录中「MaaFW 只有字符串」表述的更正：**上游回调带类型化 details，是我们 core API 停留在字符串**）；
8. speedrush 由其维护者按契约自接（契约对插件的复用成本 = 声明组边界与终态，不是零迁移——修正 v1「零成本接入」的说法）；
9. **全部生产者迁完**才删 `SECTION_ANCHORS`/`KW_RULES`/前端状态推导；
10. 复制/导出：显示文本由记录经 textContent 拼装（fields 逐行 `key: value`），含 seq/ts/group_id；fields 渲染禁 innerHTML。

## 9. 与 step 1 的关系（修正 v1 的「正交」说法）

概念正交，代码尚未会师：状态词徽章目前只在试验区（`628271c`），生产前端没有状态字段。合流点 = 徽章槽位直接吃 `group_start.outcome` / `group_end.outcome`，前端不再自推。

## 附录 · 信源

- Chrome DevTools Console API（group/groupCollapsed/groupEnd、level 过滤、count/time 行内聚合）— developer.chrome.com/docs/devtools/console/api
- GitHub Actions workflow commands（::group::/::endgroup::/::warning 参数形态、% 转义、stop-commands 令牌）— docs.github.com
- OpenTelemetry Logs Data Model（severity/body/attributes 分层；生产者声明语义、下游决定呈现）— opentelemetry-specification
- MaaFramework：本仓库 docs/MAAFW_GUIDE.md、core/pipeline_logger.py（NotificationType + details 的边沿去噪纪律）
