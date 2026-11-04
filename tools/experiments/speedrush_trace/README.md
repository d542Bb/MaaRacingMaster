status: active

# speedrush_trace — 控制流水读取器与消费者盘点

回答「旧探针怎么读 2026-10-25 之后的新流水布局」：一个统一读取器吃四种布局，
一份消费者台账记录 79 个探针的迁移依据。两者是同一主题的代码与清单，故同目录存放。

## 文件

- `speedrush_trace_reader.py`：统一读取器。自动识别新布局（`logs/<会话>/trace/speedrush.jsonl`）、
  旧布局 A（逐阶段目录）、旧布局 B（平铺按阶段文件）与直接 `.jsonl` 路径；跳过协议行，
  数据行保留旧列名。探针侧最小改造即可换读。
- `speedrush_trace_consumers.md`：消费者盘点与迁移裁定。逐个登记实际解析流水的脚本，
  记录其布局、证据与迁移方式。

## 用法

```python
from speedrush_trace_reader import iter_rows, load_stage, sessions, summary_rows

rows = list(iter_rows(path))            # 任意布局 → 数据行
rows = load_stage(path, stage_id=1)     # 新格式按行内阶段切段
for sess in sessions():
    ...
```

## 生命周期

读取器在被正式工具消费前保持本目录；若提升进 `tools/` 或插件，按实验区出口规则
迁移并拔引用。消费者清单是一次性迁移台账，结论兑现后可随目录退役。
