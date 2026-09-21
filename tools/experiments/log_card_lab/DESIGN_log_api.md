# 日志结构化 API 设计提案（研究稿，未实施）

> 起因：step 1 后逐条改日志文案被维护者否决——「到处改输出是打补丁，需要 API 级规范与 GUI 联动，核心逻辑要复用」。
> 本文 = 一手信源研究 + 设计方向 + 待裁决策点。**未拍板前不动生产代码。**

## 一、病根诊断

当前日志的**结构**（分组、状态、事件载荷）靠自然语言措辞隐式传递，两端各自硬撑：

- 生产端（插件）：把「这是阶段边界」「组内有告警」「载荷字段是累计 4/8」全塞进一行散文——行越写越长，GUI 行与诊断载荷混在一句里；
- 消费端（前端）：`SECTION_ANCHORS` 正则匹配措辞开卡、`KW_RULES` 正则着色、`finalizeSection` 数行算状态——**措辞一改前端就瞎**，这就是为什么只能"到处改输出"。

## 二、一手信源结论（四家）

| 系统 | 机制 | 出处 |
|---|---|---|
| Chrome DevTools Console | `group/groupCollapsed/groupEnd` 把层级做成 API 一等公民，渲染器据此建可折叠树；level 驱动过滤；`count/time` 把重复压缩为行内状态 | developer.chrome.com/docs/devtools/console/api |
| GitHub Actions | **流内命令协议**：`::group::title` / `::endgroup::` / `::warning file=..,line=..::msg` 与普通文本行共存于同一 stdout 流；渲染端解析命令行，其余原样显示；`::stop-commands::{token}` 防误解析 | docs.github.com workflow-commands |
| OpenTelemetry Logs Data Model | 记录 = 语义字段（SeverityNumber/Text、Body、Attributes、EventName）；**生产者只声明语义，展示/过滤/聚合全留给下游** | opentelemetry-specification logs/data-model |
| MaaFramework（本仓库依赖） | 框架日志回调只有 level+message 字符串——**生态没有现成的结构化分组协议可抄**，这是我们要补的核 | docs/MAAFW_GUIDE.md、maa 包 |

三家共同形状：**结构进 API，呈现归渲染器**。差别只在传输：Console 是活对象调用，GH 是流内命令，OTel 是结构化记录。

## 三、设计方向（核心机制，插件无关）

### 3.1 core/logger.py 增加分组原语

```python
logger.group("进入阶段: 拍品观察")        # 开卡（自动记 ts）
logger.log("识别到 汝窑天青釉", "INFO")    # 组内普通行（现有签名不变）
logger.group_end()                        # 收卡；状态自动推导：组内有 ERROR→失败 / WARNING→警告 / 否则→成功
logger.group("模块启动", status="ok")      # 可选显式状态；kind= 可选分类（阶段/会话/循环），前端不再猜
```

- 环形缓冲改存**结构化记录**（ts, level, kind, title, msg, fields, group 开合），单一真源；
- **落盘渲染器**把记录写成人类可读文本（现有格式兼容：`[ts] [LEVEL] msg`，组开合写 `::group::` 风格标记，grep 友好）；
- **GUI 通道**（fetch_logs）返回记录数组（JSON），不再返回拼好的字符串行。

### 3.2 载荷与标题分离（治"啰嗦"的根）

```python
logger.log("完成 1 场", "INFO", fields={"累计": "4/8", "OCR侧": 4, "停止开新场": False})
```

GUI 渲染 `title`（短事件句）；`fields` 进卡体行或悬停详情。诊断载荷**天然不上标题**——不再需要"这条写 INFO 还是 DEBUG"的措辞级纠结，级别管严重度，fields 管载荷。

### 3.3 前端 js/log.js 退化为纯渲染器

- `SECTION_ANCHORS` / `KW_RULES` / 前端 `finalizeSection` 状态推导**全部删除**——分组、状态、着色由记录字段驱动；
- step 1 的状态词徽章槽位直接吃 `status` 字段；
- 插件措辞怎么改都不会弄坏前端（解耦达成）。

### 3.4 复用面

speedrush / 未来任何活动域零成本接入（调 group/group_end 即得同款卡）；sidecar 的 `[sidecar]` 行、pipeline_logger 的节点生命周期同样可以按记录走。这就是"核心逻辑复用"的落点。

## 四、待裁决策点

1. **传输形态**：GUI 通道返回结构化记录（干净，fetch_logs 契约变更，前后端同仓同发版——推荐）vs GH 式流内命令字符串（兼容任何行消费者，但前端仍要解析文本协议）。
2. **落盘格式**：纯文本保持现状（组开合用注释性标记）vs 双写 JSONL（机器可再加工，体积翻倍）。
3. **fields 的 GUI 呈现**：卡体行 vs 悬停详情 vs 折叠内二级展开——先选一种。
4. **状态推导权**：logger 自动数组内级别（推荐，零心智）vs 调用点显式传。
5. **迁移范围**：core + treasure 一次做完（speedrush 不动，等它自己接）vs 只做 core 机制 + 文档，插件迁移另开任务。

## 五、与已提交 step 1 的关系

step 1（状态词徽章，`628271c`）与本设计**正交**：徽章槽位保留，只是它的状态来源从"前端数行推导"换成"记录字段直给"。step 0/1 的卡样式结论不废弃。
