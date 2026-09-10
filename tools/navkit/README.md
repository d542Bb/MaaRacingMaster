# NavKit v4 工具链

> v3 校准台（server.py + React 前端 + 编译链）已于 P4a 退役删除；画布编辑由 MPE 承担（round-trip 保真已实证），本目录只留 v4 活件。

## 入口

| 目的 | 命令 |
| --- | --- |
| 打开 MPE Studio（C 形态：MPE 即 Studio，无壳） | `tools/navkit/mpe.cmd` |
| 只停本地桥 | `tools/navkit/mpe.cmd --stop` |

`mpe.cmd` 流程：探测 `mpelb.exe`（`--mpelb` > `dev/mpelb.exe` > PATH > `%LOCALAPPDATA%`）→ 以仓库根为 root 起 LocalBridge（端口 26521）→ 起策略表薄页 `policy_server.py`（26530）→ 等端口真 LISTENING 后打开浏览器（MPE 满幅 + 策略表两个标签页）。mpelb 二进制是本地开发工具，放 `dev/`（gitignore），不入库。

**编辑真源请优先在 MPE 里做**；agent/脚本直改 JSON + 跑校验同样是合法路径（见 `skills/mpe-pipeline-edit.md`）。

## 目录

```
tools/navkit/
├── mpe.cmd              # v4 Studio 入口（必须保持 ASCII+CRLF，见文件头 NOTE）
├── policy_server.py     # 策略表薄页（读写 treasure.policy.json，原子落盘）
├── migrate_v4.py        # v3→v4 迁移器 + 图自洽校验（--full --split-global 重生成三件套真源）
├── schema/              # pipeline / custom action / custom recognition JSON Schema 三件套
├── skills/              # agent 操作规范（mpe-pipeline-edit.md）
└── dev/                 # 本地工具二进制（mpelb.exe 等，gitignore）
```

## 真源

- `maaracing_assistant/core/resources/pipeline/global.json` —— 大厅骨架（global.* 命名空间）
- `maaracing_assistant/plugins/treasure/resources/pipeline/treasure.json` —— 鉴宝图
- `maaracing_assistant/plugins/treasure/resources/policy/treasure.policy.json` —— 策略表

校验（CI 同款）：`python tools/navkit/migrate_v4.py --full --check-only`

重生成（仅迁移器语义变更时）：`--full --split-global` **两 flag 必须连用**，漏掉会把合并图覆写进 treasure.json 破坏分文件真源。
