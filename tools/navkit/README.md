# NavKit v4 工具链

> **本文是什么**：NavKit v4 工具链的使用与流程——入口命令、Studio 三页、真源文件位置、**模板图采集工作流**、校验器护栏、MPE 排错。
> **本文不是什么**：不是真源组织的规则与理由（见 [MAAFW_GUIDE §5.6](../../docs/MAAFW_GUIDE.md#56-真源组织与分层协议没有命名空间分离只能靠引用方向) / [ADR-0001](../../docs/adr/0001-分层靠引用方向不靠目录.md)）；不是引擎侧装配契约（见 [core/CODE_WIKI.md](../../maaracing_master/core/CODE_WIKI.md)）；不是节点 schema 定义（见 [NAVKIT_V4_PLAN.md §2](../../docs/NAVKIT_V4_PLAN.md)）。
> **信源等级**：L2 —— 可作「工具怎么用、资产怎么采、校验怎么跑」的直接依据；不得作为真源规则（L1）与节点/锚点取值（L0）的最终依据。
> **真源路由**：节点与策略真源 = `plugins/*/resources/**`（本文只写位置，不复制取值）；校验项 = [`check_truth.py`](./check_truth.py)。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../AGENTS.md)。

> 画布编辑由 MPE 承担（round-trip 保真已实证）；ROI 校准台 / 策略表 / 模板截取由单进程单端口的 `studio_server.py` 承担（统一标签栏互切）。

## 入口

| 目的                                          | 命令                            |
| ------------------------------------------- | ----------------------------- |
| 打开完整工作台（MPE + ROI 校准台 / 策略表 / 模板截取，零控制台弹窗） | `tools/navkit/studio.cmd`     |
| 只开 MPE（附起 Studio 服务）                          | `tools/navkit/mpe.cmd`        |
| 停 MPE 的本地桥                                   | `tools/navkit/mpe.cmd --stop` |
| 停 mpelb + Studio 服务                          | `tools/navkit/studio.cmd --stop` |

`studio.cmd` 流程：探测 `mpelb.exe`（`--mpelb` > `dev/mpelb.exe` > PATH > `%APPDATA%`）→ 隐藏窗口起 LocalBridge（仓库根为 root，端口 26521）→ 隐藏窗口起 `studio_server.py`（端口 26530，优先 `pythonw.exe`）→ 轮询两端口真 LISTENING → 开浏览器两标签（MPE 满幅 + Studio 壳页）。`--stop` 把 mpelb 与 Studio 服务一并停掉。

关闭浏览器页面后，Studio 服务在 `--idle-exit` 秒（默认 15）内自动退出：页面开着一条 SSE 保活连接（`/api/events`），关页签即断连，计数归零后延迟收摊；期间刷新或重开页面会取消退出。脚本/curl 直接调 API 时不会建立该连接，服务保持常驻（CI 与验收脚本不受影响）；需要服务一直挂着就加 `--idle-exit 0`。

`mpe.cmd` 流程：探测 mpelb → 起 LocalBridge（26521）→ 起 `studio_server.py`（26530，端口已占用则跳过）→ 等端口真 LISTENING 后打开 MPE 与 Studio 壳页两个标签。mpelb 二进制是本地开发工具，放 `dev/`（gitignore），不入库。

**编辑真源请优先在 MPE 里做**；agent/脚本直改 JSON + 跑校验同样是合法路径（见 `skills/mpe-pipeline-edit.md`）。

## Studio 三页

`studio_server.py` 一个进程承载三页，浏览器只开 `http://127.0.0.1:26530/`：

| 路径         | 页                                                          |
| ---------- | ---------------------------------------------------------- |
| `/`        | 壳页（统一标签栏 + iframe 切三页，ROI 页未保存点显示在标签上）                      |
| `/roi`     | ROI 校准台：离线回放会话帧 + 拖框选区 + 单帧测分 / 跨帧分布 / OCR 识别 / 裁剪模板           |
| `/policy`  | 策略表：表格编辑 `treasure.policy.json` 的 `policy.rules`，原子落盘             |
| `/cropper` | 模板截取：静态只读服务 `tools/template_cropper/index.html`              |

ROI 校准台的数据面（`/api/rois` 读写）以 **v4 真源**为准：

- 读面 flat 投影 = spec 三组（`template` / `point` / `ocr`，按 `kind` 分组）+ `nodes` 组（pipeline 两文件逐处 `rect`，含 `mirrors` 与 colorspace 现值）+ `tuning` 组（`policy.tuning.perception` 的第 54 个 rect）+ `_meta`；
- 写面管线：`base_hash` 比对(409) → 内存合并 → 结构校验 → `check_truth` 各闸（图自洽/分层红线/交叉互洽/几何/页面清单等集/页面归属闭合 `page_checks`）→ preview 回 diff/report 不落盘 → 逐文件原子替换；
- 11 个两面同值锚点在 `nodes` 组编辑时默认同步写 spec 同名锚点（UI 提供「仅改此面」逃生口）；colorspace 两面各自维护（语义独立，台内如实呈现各自现值）；
- 测分/跨帧/OCR 走**生产同源**引擎（`core.template_match.find_any_cs`、`plugins.treasure.ocr.TreasureOcr.recognize_single`）。

### speedrush HUD 只读复核通路

用途：在**真实速度录制的帧**上把 speedrush 驾驶页 HUD 区域画出来并读一次文本，供维护者复核
区域压得准不准。入口与 treasure 校准台**同一台服务、同一个 `/roi` 页**，只在分类 tab 里多一组
`speedrush HUD`——切到该组时，会话/帧下拉会自动换成速度录制库（帧源按分类切换）。

| 面        | 端点                                                                              |
| --------- | ------------------------------------------------------------------------------- |
| 会话 / 帧列表 | `GET /api/speedrush/list_sessions` · `GET /api/speedrush/list_images?session=`   |
| 帧图       | `GET /api/speedrush/image?session=&name=`（形状同 `/api/image`，逐字节直传录制 jpg）          |
| 读数       | `POST /api/speedrush/ocr` `{session, image, rect}` → `{text, lines, crop_size, crop_preview, duration_ms}` |

起法：`tools/navkit/studio.cmd`（或 `.venv\Scripts\python.exe tools\navkit\studio_server.py`），
在 `/roi` 页切到 `speedrush HUD` 分类；选中任一区域即在下方「OCR 识别结果」出读数。

**边界**：

- **只读**：该分类不接拖拽、改值、新增、删除，也不进保存 body（`collectSaveBody` 剔除），
  服务端 `apply_save` 没有它的合并分支——保存管线对它是无操作。三处门由
  [`tests/test_navkit_studio_speedrush.py`](../../tests/test_navkit_studio_speedrush.py) 锁住。
  注意这**不**涉及 treasure 的 `nodes`/`tuning`：它们的 rect 值仍可编辑并触发镜像联动
  （只读的是「锚点增删」），这是成文语义且有测试锁（`test_navkit_studio_save_flow.py::TestMirrorLinkage`）。
- **区域真源仍在实验目录**：`tools/experiments/speedrush_scoring/hud_regions.json`
  （纯 `{区域名: [x1, y1, x2, y2]}` 归一化、x2/y2 排他）。**刻意不复制进插件 resources**——
  实时读数落地后本区域集会移入插件 resources，届时此处只留指向；现在复制会建出一份无人读的
  第二真源。
- **帧源**：`data/speedrush/demos/<会话>_p<N>/frames/NNNNNN.jpg`（根由 `core.paths.data_dir()`
  派生，不硬编码路径）。会话名/帧目录/帧名三份白名单与 debug 截图会话不同，由
  `studio_sessions.SessionBrowser` 的构造参数注入，**穿越防护（`is_relative_to` + 双白名单）
  两处共用同一份实现**。
- **读数引擎**：走 `maaracing_master.core.ocr.RapidOcrEngine`（core 唯一真源），**不经** treasure 的
  `TreasureOcr`；故响应里没有 `amount`/`amounts` 这类鉴宝金额语义，区域名只表示"画面上这块框"。

## 目录

```
tools/navkit/
├── studio.cmd           # 完整工作台入口（mpelb + studio_server 均隐藏窗口；必须 ASCII+CRLF）
├── studio_server.py     # 单进程三页服务（ROI 校准台 / 策略表 / 模板截取，端口 26530）
├── studio_sessions.py   # 帧库（会话/帧/模板名白名单 + 目录穿越防护；布局由调用方注入）
├── static/              # ROI 校准台前端（shell.html + calibrator.html + app.js + history.js + style.css）
├── mpe.cmd              # MPE 入口（起 mpelb + studio_server；必须 ASCII+CRLF，见文件头 NOTE）
├── check_truth.py       # 真源自洽校验（图闭合+数据面装配+交叉互洽+几何+分层红线；CI 同款）
├── schema/              # pipeline / custom action / custom recognition JSON Schema 三件套
├── skills/              # agent 操作规范（mpe-pipeline-edit.md）
└── dev/                 # 本地工具二进制（mpelb.exe 等，gitignore）
```

## 真源

- `maaracing_master/plugins/treasure/resources/pipeline/treasure.json` —— 鉴宝对局图

- `maaracing_master/plugins/treasure/resources/pipeline/treasure.entry.json` —— 鉴宝大厅入口链与页面锚点（游戏大厅/活动页面 dwell + 两个入口锚点 + `hall_to_treasure` 链；2026-09-10 自 core 归位）

- `maaracing_master/core/resources/pipeline/` —— 跨模块共用链目录，**当前不存在**：尚无第二个模块接图，抽公共层为时过早。目录按存在性纳入加载；真出现共用链时再建，且受分层红线约束（core 不得引用 `<module>.` 节点，见 `check_truth.namespace_checks` 与 MAAFW\_GUIDE §5.6）

- `maaracing_master/plugins/treasure/resources/policy/treasure.policy.json` —— 策略表 + **感知执行规格** + **引擎契约**（`perception.spec/stages/transitions/match` 是 detector/OCR/模板装载器的运行时唯一真源，P4b 起；`engine_contract` 段供决策引擎消费域白名单/推导/副作用形——接入新模块写新数据面，不改 core；编辑后与图节点同权）

校验（CI 同款）：`python tools/navkit/check_truth.py`

> 真源数值等价性的历史对拍记录在 `tools/experiments/v4-p4b-source/`。

### 校验器护栏（CI 同款）

`check_truth.py` 的校验项：next/on\_error 引用闭合（含 **And/Or 按名子项**——框架只校验 `next`/`on_error`，子项拼错要到运行期才 `Bad sub ref` 静默失败）、入口可达、疑似重复识别告警、policy 数据面可装配、图↔spec 交叉互洽、方向红线（`namespace_checks`）、**两面同图**（templates 相同的图侧参数与 spec 锚点逐字段比对 `rect`/`threshold`/`arbitration`/`mode↔kind`/`colorspace`，任一面不等即拦）。

配套 [`tests/test_navkit_truth.py`](../../tests/test_navkit_truth.py)：锁归位形态（真源全在模块命名空间、core 侧零节点、plugin 文件集）、锁**红线活性**（合成违规图必须报错，防校验退化成装饰）、图↔spec 交叉见证与节点/锚点计数见证防漂。

颜色口径定案：**默认 gray，灰度拉不开差距才转 rgb**（2026-09-11 两面统一；依据 = 检测面 detector 早已按 `spec.arbitration.margin` 做领先判定，P4C 对拍 812 帧两引擎命中数相等、零翻转，gray 快 2.8 倍）。

模板文件缺失由运行时 `load_template` 返回 None 降级 + 装载器 WARNING 暴露。**体积账**：单张 5\~60KB、全部模板预期 <1MB（对比 rapidocr 模型 30MB、onnx 12MB 不成量级）——要防的是"图死"（孤儿图定期对照 spec/节点引用清一次）不是"图多"。

## 模板图采集工作流（2026-09-07 定案，v4 载体沿用）

> 适用：为图节点 / policy `perception.spec` 锚点采集模板图。分工：**人工截图+裁剪标注 → 导出 regions.json + PNG → 工具侧换算入库**（rect 归一化/JSON/校验/提交均为工具侧职责）。

**来源纪律**：只从 1280×720 运行帧截图裁剪（启动即统一 720p），1:1 像素裁剪、**永不缩放**（模板与运行帧同尺度，匹配单尺度 1.0）。

**人工截图通道的坐标系换算（2026-09-16 实测）**：截图工具产出的是**带窗口边框的窗口截图**，实测 1281×759——比客户区多出左侧 1 px 边框与顶部 38 px 标题栏。此类 PNG 必须先裁出客户区（`img[38:758, 1:1281]`，恰 720×1280）再定位、算 rect，否则 ROI 整体偏移 38 px、运行期全部失配。校验办法：取一张来自真正运行帧的既有模板（如鉴宝域的 `hall_peak_appraise_card.png`）在该客户区上匹配，分数应达 ~1.0000。

**裁剪纪律**：只装"一年后还长这样的像素"——角标/数字/倒计时/红点/限时横幅一律留在模板外。变化在模板图**外**（哪怕紧贴）不影响匹配得分；在图**内**才失效（阈值 0.75 容忍渲染抖动，不容忍结构性变化）。

**半透明元素不宜作锚点**：压在动态画布上的 HUD 文字与进度条，其像素是逐帧与背景混合的结果，匹配分随背景大幅摆动。此类位置改取**不透明图标**替代，或退为多锚点 Or 组合兜底。

背景干扰还有**第二个入口**：彩色匹配（`colorspace: rgb`）会把搜索区内的**背景颜色**一起算进相似度——换了不透明图标，分数照样会被天光拉低。只比形状（`gray`）可显著收敛，但**根治要靠"跨天光、多轮样本"标定阈值**：单轮样本读出的"稳定"是假象，阈值贴着实际分布的下缘画，就会在实机上抖动成假失配。各域的实测数据见其域文档（如 [speedrush 域 §1](../../maaracing_master/plugins/speedrush/CODE_WIKI.md)）。

**命名**：`<页面>_<元素>[_限定词].png`，全小写下划线；首段页面名与页注册表一致（`hall_` / `rank_` / `speedrush_`…），看文件名即知归属。

**位置跟 owner 走**：global 锚点 → `core/resources/image/`；模块锚点 → `plugins/<id>/resources/image/`。归属与物理位置矛盾由编辑评审盯。

**回传**：标注工具导出 regions.json（像素区域 x/y/w/h）+ PNG → 换算归一化 rect（外扩 15% 宽 / 25% 高、4 位小数）写入真源。**JSON 真源由 MPE 画布 / Studio 工具链维护，人工不手改裸文件**；agent/脚本直改 JSON 后跑 `check_truth.py` 机检 + 提交。

> **ROI 是「这个按钮可能出现在哪里」，不是「它上次出现在哪」**：一个节点服务多页时，ROI 必须覆盖各页位置的**并集**再留余量——按单帧模板外扩算出的 rect 会在同一按钮的另一页失配（2026-09-16 speedrush 结算页实证，见其域文档 §4）。

**MAA 对照**：MaaFramework 侧仅约定"720p 无损原图裁剪勿缩放 + `roi`/`box`/`target` 三概念分离"（MAAFW_GUIDE §5.1/§5.2）；社区靠 ImageCropper 类工具 + 人工纪律，无结构化工作流。本仓库在其上加 regions 机器可读导出 + 校验守卫闭环。

## MPE 常见疑惑与排错

面向第一次用 Studio 的开发者。口径：mpelb **1.10.0** + MPE 在线 stable（**两者必须同代**，
版本号对齐即可，见下方「协议版本不匹配」条）；机器本地配置在
`%APPDATA%\MaaPipelineEditor\LocalBridge\`（**重装 mpelb 即丢，换机器要重设**）。
原理与机检口径见 `skills/mpe-pipeline-edit.md` §5，这里只给能照抄的处置。

**打开 MPE 没几秒 LocalBridge 自己退出了**
先怀疑**协议版本不匹配**：MPE 在线版每次大版本更新都会抬高 LocalBridge 协议要求，本地 mpelb
落后时**后端会主动退出**（不是崩溃，是它检测到前后端协议不一致后自行收摊）。日志里是这三行：

```
level=warning msg="协议版本不匹配，前端需求: 1.5.0，当前本地服务协议: 1.4.6"
level=error   msg="检测到前后端协议版本不一致，当前前端需求: 1.5.0，后端协议: 1.4.6"
level=error   msg="请更新 MaaPipelineEditor 或 Local Bridge 后重试，后端即将主动退出"
```

处置：把 mpelb 升到与 MPE 同版本号。`dev/` 下的 mpelb 是本地工具（gitignore，不入库），
按 release tag 取对应平台二进制即可（Windows x64 用 `mpelb-windows-amd64.exe`，重命名为
`mpelb.exe` 放 `dev/`）：

```
https://github.com/kqcoxn/MaaPipelineEditor/releases/download/v<版本>/mpelb-windows-amd64.exe
```

自检（起隔离端口 + WS 握手，不碰正在用的实例）：

```powershell
.venv\Scripts\python.exe tools\experiments\v4-p3-studio\diag_lb_protocol_handshake.py
```

期望输出 `协议版本: 本地=1.5.0 要求=1.5.0 success=True -> 匹配` 与 `mpelb 进程存活: True`。

**首次打开弹"新手引导"答题，挡住工作面**
在浏览器控制台执行 `mpedev("skipNewcomer")` 即可跳过。答过一次后 MPE 自己会提供跳过入口
（v1.8.0 起）。注意 `mpedev` 是前端 dev 接口，官方文档与 release 均未收录，**别写进自动化脚本**。

**打开 MPE 不会自动定位到文件，得手动在文件面板里点**
正常行为：mpelb 只负责把真源**列进**文件面板，不会自动打开任何一个；MPE 会按浏览器本地记录
恢复"上次成功打开的文件"。所以**手点一次** `treasure.entry.json`（或 `treasure.json`），
之后自动定位就恢复并记住它。无痕窗口、换浏览器、IDE 内嵌 simple browser 的新会话都留不下这条记录。

**"我明明没点保存，文件怎么被 MPE 改了？"**
先确认事实：mpelb 日志会明确记一条 `文件已保存: <路径>`（module=FileService），那就是有一次
`/etl/save_file` 到达后端；且**只有在你视口里打开过的文件**会被重写。重写后文件会有三处变化
（都不改语义，工具链已兼容）：Custom 识别归一化成 v2 嵌套、等于默认值的键被省略（如
`action:"DoNothing"`）、回写画布元数据 `$__mpe_code` / `$__mpe_config_*` / `$__mpe_external_*`。

是不是"自动保存"目前**未证实**：官方文档把"保存到文件"列为显式动作、release notes 无自动保存条目，
但 MPE 前端确实会发 `document_changed`（Iframe 契约的三个动作之一），变更驱动写回是可能的实现路径。
想自己判定，两步就够：

```powershell
# 基线：记下真源当前的修改状态
git status --short -- maaracing_master/plugins/treasure/resources/pipeline
# ① 只打开文件、什么都不动，等几分钟后重跑上面那条——无变化 = 不存在纯定时自动保存
# ② 在画布上拖动一个节点（不改任何字段），再查后端日志：
Select-String -Path "$env:APPDATA\MaaPipelineEditor\LocalBridge\logs\lb-*.log" -Pattern '文件已保存'
# 立刻多出一条 = 变更驱动的自动保存（MPE 把布局变更也算变更）
```

**MPE 导出该选 v1 还是 v2？**
本项目**统一 v2 归一形**（2026-09-10 定案），所以**不用再把编辑器导出锁成 v1**。理由不是"v1 被废弃"
——v1 在协议上仍合法、新字段两种形态同时生效（框架只有一条参数解析路径）；而是 **v2 是框架内部的
规范表示**：`get_node_data`（官方 PipelineDumper）只吐 v2，MPE 保存也产 v2，强制 v1 等于每次存盘
跟工具对赌、diff 反复翻转。读官方文档与 sample 时看到的仍是 v1，字段对照表见 MAAFW\_GUIDE §5.6。
仓库形态由 `test_truth_source_normalized_to_v2` 机检；**读取面双形态兼容不许删**（人手写/历史文件
仍可能是 v1）。

**控制台反复刷** **`[PERMISSION_DENIED] 权限不足或路径非法`**
成因几乎总是：MPE 恢复了**仓库外**的旧路径（越界），不是文件被删。三种返回码可以直接分辨：

| mpelb 返回                                 | 含义                                 |
| ---------------------------------------- | ---------------------------------- |
| `/lte/file_content`                      | 路径在 root 内且存在 —— 正常                |
| `FILE_NOT_FOUND`                         | 路径在 root 内但文件不存在（改名/删了/迁走了）        |
| `PERMISSION_DENIED`（`reason=路径不在根目录范围内`） | **路径越出** **`--root`** —— 与文件是否存在无关 |

处置：在文件面板重新点开仓库内真源；仍反复弹就清一次 `mpe.codax.site` 的站点数据；
把历史遗留的仓库外试验田目录（旧的 `pipeline` 副本所在地）改名或删除，否则它会一直勾住"最近文件"。

**`MFW 服务初始化失败：MaaFramework 库路径未配置`**
只影响在 MPE 里直接跑识别/调试，文件编辑不受影响。要消掉：

```powershell
tools\navkit\dev\mpelb.exe config set-lib "<仓库根>\.venv\Lib\site-packages\maa\bin"
```

该目录含 `MaaFramework.dll`，版本随 `.venv`（maafw 5.12.3）。`set-lib` 会顺带置 `maafw.enabled=true`。
OCR 资源路径按需另配 `config set-resource`——`.venv` 的 maa 包里没有 `resource/` 目录，
不配则原生 OCR 不可用（MPE 前端 OCR 仍可用），日志那句 WARN 也这么写。

**文件面板里混进退役图（`archive/racing/...`）**
mpelb 只索引 `pipeline` 目录下的 json，而它的 `file.exclude` 默认**不含** **`archive`**。已在
`config.json` 里加上，现在索引 2 个文件 = 两个真源。自己机器上没加过的话补一下。

**改完** **`config.json`** **后 mpelb 直接起不来、端口不监听**
先怀疑写进了 **UTF-8 BOM**（其次是 JSON 语法被改坏）：mpelb（Go/viper）遇 BOM 静默退出，只留一句
`invalid character 'ï' looking for beginning of value`。PowerShell 5.1 的
`Set-Content -Encoding UTF8` **会加 BOM**，别用它。改法与自检：

```powershell
$p = "$env:APPDATA\MaaPipelineEditor\LocalBridge\config\config.json"
$b = [IO.File]::ReadAllBytes($p)
if ($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) { [IO.File]::WriteAllBytes($p, $b[3..($b.Length-1)]) }
```

改完首三字节应为 `7B 0A 20`（即 `{` 开头）。

**想知道配置/日志在哪**
不用手翻目录：`mpelb config open`（配置）、`mpelb config open-log`（日志）、`mpelb info`（路径）。
日志 INFO 级**不打请求路径**，要查越界的具体路径就 `--log-level DEBUG` 重起，
或自连 WS 探（可复用 `tools/experiments/v4-p3-studio/diag_lb_ws_permission.py` 里的
`ws_connect` / `ws_send` / `FrameReader` / `read_until_response`）。

**只想起 LocalBridge 不起 Studio 服务**
`mpe.cmd` 会一并起两者（26521 / 26530）。只要文件管理就直接跑 `dev\mpelb.exe --root <仓库根> --port 26521`；
停服务用 `mpe.cmd --stop`（只停 mpelb）或 `studio.cmd --stop`（mpelb + Studio 服务一起停）。
