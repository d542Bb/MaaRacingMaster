# ecosystem-audit — MaaFw 生态对拍（管线真值 dump/load）

status: active

回答「本项目 pipeline 真值在 MaaFw 实装链路上的保真与成本」：`load_truth.py`（真值
装载 + `get_node_data` 回读对照）、`contract_dump.py` / `probe_schema.py` /
`probe_parse.py`（官方 PipelineDumper 形态契约）、`template_exist.py`（模板齐全性）、
`field_inventory.py`。**升 MaaFw 版本前必须重跑**（`docs/MAAFW_GUIDE.md` And/Or 子项
风险条目指向本实验，见 commit `0554f58`）。结论沉淀于 `docs/MAAFW_GUIDE.md`。
