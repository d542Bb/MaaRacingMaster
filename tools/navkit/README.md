# NavKit v4 工具链

> 画布编辑由 MPE 承担（round-trip 保真已实证）：`mpe.cmd` 起本地桥即 Studio（C 形态，无壳）。本目录只含 v4 活件。

## 入口

| 目的                                  | 命令                            |
| ----------------------------------- | ----------------------------- |
| 打开 MPE Studio（C 形态：MPE 即 Studio，无壳） | `tools/navkit/mpe.cmd`        |
| 只停本地桥                               | `tools/navkit/mpe.cmd --stop` |

`mpe.cmd` 流程：探测 `mpelb.exe`（`--mpelb` > `dev/mpelb.exe` > PATH > `%LOCALAPPDATA%`）→ 以仓库根为 root 起 LocalBridge（端口 26521）→ 起策略表薄页 `policy_server.py`（26530）→ 等端口真 LISTENING 后打开浏览器（MPE 满幅 + 策略表两个标签页）。mpelb 二进制是本地开发工具，放 `dev/`（gitignore），不入库。

**编辑真源请优先在 MPE 里做**；agent/脚本直改 JSON + 跑校验同样是合法路径（见 `skills/mpe-pipeline-edit.md`）。

## 目录

```
tools/navkit/
├── mpe.cmd              # v4 Studio 入口（必须保持 ASCII+CRLF，见文件头 NOTE）
├── policy_server.py     # 策略表薄页（读写 treasure.policy.json，原子落盘）
├── check_truth.py       # 真源自洽校验（图闭合+数据面装配+交叉互洽；CI 同款）
├── schema/              # pipeline / custom action / custom recognition JSON Schema 三件套
├── skills/              # agent 操作规范（mpe-pipeline-edit.md）
└── dev/                 # 本地工具二进制（mpelb.exe 等，gitignore）
```

## 真源

- `maaracing_assistant/core/resources/pipeline/global.json` —— 大厅骨架（global.\* 命名空间）

- `maaracing_assistant/plugins/treasure/resources/pipeline/treasure.json` —— 鉴宝图

- `maaracing_assistant/plugins/treasure/resources/policy/treasure.policy.json` —— 策略表 + **感知执行规格**（`perception.spec/stages/transitions/match` 是 detector/OCR/模板装载器的运行时唯一真源，P4b 起；编辑后与图节点同权）

校验（CI 同款）：`python tools/navkit/check_truth.py`

> 真源数值等价性的历史对拍记录在 `tools/experiments/v4-p4b-source/`。

