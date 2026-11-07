# maafw_audit — MaaFw 生态审计工具

升 MaaFw 版本前、或 pipeline 真源形态变更后，用这里的工具核对「本项目的真源在 MaaFw
实装链路上是否仍保真」。样本是现役的 core 与 speedrush 真源，不依赖归档插件。

结论不在这里复制：官方行为口径与已知风险见 [`docs/MAAFW_GUIDE.md`](../../docs/MAAFW_GUIDE.md)。

## 工具

| 工具 | 回答什么 |
|---|---|
| `load_truth.py` | core 与插件 pipeline 合并 post 能否加载；分次 post 的互指坑是否仍在；自定义注册是否必需；`get_node_data` 回读是否保留节点形态 |
| `field_inventory.py` | 逐节点字段清点：根键、识别/动作枚举、v2 嵌套形态是否落在官方 schema 内 |
| `template_exist.py` | 真源引用的模板是否都在磁盘上；磁盘上是否有未被引用的模板 |
| `policy_in_pipeline_test.py` | 布局红线实证：policy 数据混进 pipeline 目录时，递归加载是否整目录失败 |
| `v1_duplicate_register.py` | 自定义识别/动作命名空间的实际行为（当前按安装版本实测，升版后重跑） |
| `probe_a0/` | `Context.run_recognition(节点, 帧)` 只读链路的放行验证：override 传参形态、传帧是否生效、通道契约、桥异常与停止语义、与框架识别轮的同帧等价性。用 `CustomController` 喂录制帧，注册现役 `TemplateRecognizer` |

## 用法

```bash
.venv/Scripts/python.exe tools/maafw_audit/load_truth.py
.venv/Scripts/python.exe tools/maafw_audit/field_inventory.py
.venv/Scripts/python.exe tools/maafw_audit/template_exist.py
.venv/Scripts/python.exe tools/maafw_audit/policy_in_pipeline_test.py
.venv/Scripts/python.exe tools/maafw_audit/v1_duplicate_register.py
.venv/Scripts/python.exe tools/maafw_audit/probe_a0/step4_equivalence.py 880 900
```

`load_truth.py`、`policy_in_pipeline_test.py` 与 `probe_a0/` 需要 `.venv` 内的 MaaFw；
`probe_a0/` 另需会话 `20261106_185832` 的 nav 调试帧；其余只读文件、可直接运行。
