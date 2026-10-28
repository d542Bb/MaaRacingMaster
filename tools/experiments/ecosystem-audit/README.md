# ecosystem-audit — MaaFw 生态对拍（管线真值 dump/load）

status: active

回答「本项目 pipeline 真值在 MaaFw 实装链路上的保真与成本」：`load_truth.py`（真值
装载 + `get_node_data` 回读对照）、`contract_dump.py` / `probe_schema.py` /
`probe_parse.py`（官方 PipelineDumper 形态契约）、`template_exist.py`（模板齐全性）、
`field_inventory.py`。**升 MaaFw 版本前必须重跑**（`docs/MAAFW_GUIDE.md` And/Or 子项
风险条目指向本实验，见 commit `0554f58`）。结论沉淀于 `docs/MAAFW_GUIDE.md`。

## 红线实证脚本（2026-10-28 补索引，此前只在 docstring 里）

结论都在 `docs/MAAFW_GUIDE.md`（`pipeline/` 递归读取所有 json；互指必须合并单次 `post`），
这两个脚本是那两条论断的**可复跑实证**，不属于"升版必跑"的常规五件：

- `load_test.py`：B1——官方 `Resource.post_pipeline` 能否加载两真源目录，复现
  `core/nav_graph` 的合并注入路径。
- `policy_in_pipeline_test.py`：B4 红线——policy 表混进 pipeline 目录时，递归加载是否**整目录
  失败**（即"数据面 JSON 不能与任务 JSON 同目录"这条红线的实证）。
