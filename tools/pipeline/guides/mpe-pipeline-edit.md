# 编辑 Pipeline 真源（与 MPE 协调）

> 用途：让 coding agent 正确、安全地编辑/维护 pipeline 节点真源，并知道何时该交给
> 人用 MPE 画布、何时该直接改 JSON。简体中文，可被 agent 在相关任务时读取。
> 一手依据：MPE round-trip 实证与运行时校验；mpelb 版本、协议与排错以
> [README](../README.md) 的 MPE 章节为准，本文不复制版本号。

## 0. 核心结论（先读这段）

- **pipeline 节点 JSON 是唯一真源。** agent 编辑真源 = 直接改 JSON + 跑校验器；
  不需要也无推荐用 MPE 的浏览器 UI 改。
- **MPE 是面向「人」的画布**，用于可视化审阅、拖拽编辑与调试；agent 的角色是把
  真源改对 + 校验，把「要不要看图画」的判断留给用户。
- **Studio 是回放 + ROI 两页服务**，与 MPE 是两个面：MPE 编辑图结构，Studio 只读
  会话做回放、只改节点里的归一化矩形。`mpe.cmd` 会一并拉起两者。

## 1. 真源位置与契约

| 内容 | 路径 |
| --- | --- |
| 活动插件图 | `maaracing_master/plugins/<id>/resources/pipeline/*.json` |
| 启动步骤自有链 | `maaracing_master/core/resources/startup/pipeline.json` + `image/` |
| 跨模块共用链 | `maaracing_master/core/resources/pipeline/`——**当前不存在**；出现第二个真实使用者再建 |
| 归档鉴宝的图与策略表 | `maaracing_master/plugins/_archive/treasure/resources/`；默认发现不扫归档区 |

- **分层红线**：节点名全城唯一、无命名空间；core 真源不得占用、也不得引用
  `<module>.` 节点名（`truth.namespace_checks` 机检）。想加“跨模块共用链”先读
  `MAAFW_GUIDE §5.6`——只有一个使用者时不该有 core 图。

- **目录必须叫 `pipeline`**：mpelb / MSE / MaaMCP 等生态工具按 ProjectInterface 惯例
  只索引名为 `pipeline` 的目录或 `interface.json`。不要改成别的名字。

- **policy 表不放在 pipeline 目录**：MaaFW 递归加载会把它当 pipeline 解析，导致整目录失败。
  策略表本身只属于归档鉴宝；不要再给新模块引入 policy 数据面。

## 2. agent 编辑真源的正确姿势

1. 直接改 JSON（文件编辑，注意 UTF-8、换行、键序——保持现有顺序，避免无意义 diff）。
2. 改完跑校验：`.venv\Scripts\python.exe tools\pipeline\truth.py`
3. 需要运行时态确认时，跑对应的图执行 / 模块测试；不要把节点数之类的计数抄进文档。
4. 约定字段：

   - 标准字段（recognition/action/next/timeout/rate_limit/on_error…）按 MaaFW 协议。

   - 节点元数据一律放官方扩展位 `attach` 内、键名带 `_` 前缀（`attach:{_page,_route,_dwell,_boot,_park…}`）——
     框架保留可经 `get_node_data` 回读；**节点顶层 `_xxx` 会被框架解析器静默丢弃（5.12.3 实测），禁止再写**。
     归档鉴宝的 policy 数据面 JSON 不受此规约束（框架不解析该文件）。

   - custom 识别/动作：`type:"Custom"` + `param:{custom_recognition/custom_action, *_param}`（MPE 归一化形态，协议标准）。

   - `next` 跳转可用 `"[JumpBack]<node>"` 写 jump_back（MaaFW 原生语法）。

## 3. 何时把真源交给 MPE（人的画布）

- 用户要看图画 / 手动微调 / 调试 FlowScope → 让用户运行 `tools\pipeline\mpe.cmd`
  （起 mpelb，root=仓库根，并在浏览器打开 MPE 与 Studio）。**mpelb 只把真源列进文件面板，
  不会自动打开任何一个**——MPE 仅按浏览器本地记录恢复“上次打开的文件”，记录为空/失效
  就得手点一次（实测：恢复成仓库外路径时反而每开必报 `PERMISSION_DENIED`）。

- **项目规范形态 = v2 归一**：Custom 识别/动作写 `recognition/action:{type,param}`；
  等于默认值的键可以省略；MPE 会回写画布元数据 `$__mpe_code.position`、根级
  `$__mpe_config_*` 与 `$__mpe_external_*`（后者是跨文件外部节点占位，画布上紫色块的来源；
  协议明文 `$` 前缀根级字段不被解析）。**agent 不要把这些改回 v1**。
  同时**读取面保持双形态兼容**：v1 在协议上仍合法，官方文档与 sample 仍以 v1 叙述，
  人手写/历史文件都可能再出现——这条兼容不是冗余，不许删。

- MPE **无插件/侧栏面板注入点**（Iframe 目录只承载 loadPipeline/save/document_changed）——
  不要承诺把自研 UI 挂进 MPE。

## 4. 不要做

- 不要直接在真源里手写非标准字段冒充 MPE 配置。
- 不要让 MPE 用「分离导出」（会生成 `.xxx.mpe.json`，不在 pipeline 目录、不被加载）。
- 不要在归档鉴宝的 policy 规则里重复发明 MaaFW 已有概念；新模块不要复制 policy 数据面。

## 5. mpelb 本地服务运维要点（机器本地，重装即丢）

配置与日志都在 `%APPDATA%\MaaPipelineEditor\LocalBridge\`（`config/config.json`、`logs/lb-*.log`），
**不在仓库内**，换机器/重装要重设一遍：

- **安装/更新 mpelb**：项目内用 `tools\pipeline\mpe.cmd --update`（或 `studio.cmd --update`）。
  干净检出没有二进制时直接安装；已安装时先把上一版留成 `dev\mpelb.previous.exe`（只留一份）
  再替换。它读取官方 release 清单并校验 SHA256，`--check` 只看不换。官方全局安装/更新会另外
  准备 MaaFramework/OCR 运行时，按需使用。

- **MaaFramework 库路径**（不设则 MFW 侧功能不可用，文件编辑不受影响）：
  `tools\pipeline\dev\mpelb.exe config set-lib "<repo>\.venv\Lib\site-packages\maa\bin"`
  （该 bin 目录含 `MaaFramework.dll`，版本随 `.venv`；`set-lib` 会同时置 `maafw.enabled=true`）。
  OCR 资源路径按需 `config set-resource`——`.venv` 的 maa 包不含 resource 目录，
  不配则原生 OCR 不可用（前端 OCR 仍可用）。

- **索引范围**：mpelb 只索引 `pipeline` 目录下的 json/jsonc，且 `config.json` 的
  `file.exclude` 默认含 `build`/`.venv` 等但**不含** `archive` → 需手工加入 `archive`，
  否则退役图会混进文件面板。索引文件数随真源变化，不在文档里记。

- **改 config.json 严禁带 UTF-8 BOM**：mpelb（Go/viper）遇 BOM 直接退出，只留一句
  `invalid character 'ï' looking for beginning of value`，端口不监听、无更多线索。
  PowerShell 5.1 的 `Set-Content -Encoding UTF8` **会写 BOM**——用 `[IO.File]::WriteAllBytes`
  或 `UTF8Encoding($false)` 写；改完验首三字节应为 `7B 0A 20`（`{`）。

- **报错码三分**（`mpelb` WS 实测，用于快速定位成因）：root 内存在 → `/lte/file_content`；
  root 内不存在 → `FILE_NOT_FOUND`；**root 外 → `PERMISSION_DENIED`（reason=路径不在根目录范围内）**。
  看到权限码即可排除“文件被删/改名”，只查越界（常见来源：浏览器恢复了仓库外旧路径）。
  INFO 级日志不打请求路径，需要时 `--log-level DEBUG` 复现一次。

- **首次答题防呆跳过**：MPE 前端 dev 接口 `mpedev("skipNewcomer")`（浏览器控制台执行；
  官方文档站与 release 说明均未收录该接口，勿在文档里当作可依赖的稳定 API）。
