# Pipeline 工具链（MPE + Studio + 校验）

> **本文是什么**：pipeline 真源的编辑与取证工具——入口命令、Studio 两页、真源文件位置、模板图采集工作流、校验器护栏、MPE 排错。
> **本文不是什么**：不是真源组织的规则与理由（见 [MAAFW_GUIDE §5.6](../../docs/MAAFW_GUIDE.md#56-真源组织与分层协议没有命名空间分离只能靠引用方向) / [ADR-0001](../../docs/adr/0001-arch-global-分层靠引用方向不靠目录.md)）；不是引擎侧装配契约（见 [core/CODE_WIKI.md](../../maaracing_master/core/CODE_WIKI.md)）；不是节点 schema 定义（见 [PIPELINE_V4.md §2](../../docs/PIPELINE_V4.md)）。
> **信源等级**：L2 —— 可作「工具怎么用、资产怎么采、校验怎么跑」的直接依据；不得作为真源规则（L1）与节点/锚点取值（L0）的最终依据。
> **真源路由**：节点真源 = `plugins/*/resources/pipeline/**` 与 `core/resources/**`（本文只写位置，不复制取值）；校验项 = [`truth.py`](./truth.py)。

> 画布编辑由 MPE 承担（round-trip 保真已实证）；回放预览与 ROI 调整由 `tools/pipeline/studio/` 的单端口两页服务承担。

## 入口

| 目的 | 命令 |
| --- | --- |
| 打开完整工作台（MPE + 回放 / ROI，零控制台弹窗） | `tools/pipeline/studio.cmd` |
| 只开 MPE（附起 Studio 服务） | `tools/pipeline/mpe.cmd` |
| 停 MPE 的本地桥 | `tools/pipeline/mpe.cmd --stop` |
| 停 mpelb + Studio 服务 | `tools/pipeline/studio.cmd --stop` |
| 查看本地/最新 mpelb | `tools/pipeline/mpe.cmd --check` |
| 安装/更新项目内 mpelb | `tools/pipeline/mpe.cmd --update` |

`studio.cmd` 流程：探测 `mpelb.exe`（`--mpelb` > `dev/mpelb.exe` > PATH > `%APPDATA%`）→ 隐藏窗口起 LocalBridge（仓库根为 root，端口 26521）→ 隐藏窗口起 Studio 服务（端口 26530）→ 轮询两端口真 LISTENING → 开浏览器两标签（MPE 满幅 + Studio）。`--stop` 把两者一并停掉。

Studio 服务在页面心跳停止 `--idle-exit` 秒（默认 15）后自动退出；脚本/curl 直接调 API 时每次请求都会续期，需要常驻就传 `--idle-exit 0`。

**编辑真源请优先在 MPE 里做**；agent/脚本直改 JSON + 跑校验同样是合法路径（见 `guides/mpe-pipeline-edit.md`）。

## Studio 两页

`python -m tools.pipeline.studio.server` 一个进程承载两页，浏览器只开 `http://127.0.0.1:26530/`：

| 路径 | 页 |
| --- | --- |
| `/` | 壳页：会话 / 行为 / 帧导航 + 回放 / ROI 两个标签 |
| `/api/replay/*` | 只读 v5 会话：`debug/<会话>/<模块>/<行为>/manifest.json` + 帧索引 |
| `/api/roi/*` | 节点归一化矩形读写；保存前后跑 `truth.py` 的图侧校验 |

**回放**：按会话枚举行为目录，读清单与最小帧索引（序号、单调时刻、相对路径），显示帧与元数据；不解析模块私有格式。

**ROI**：只编辑节点 JSON 里 `recognition.param.custom_recognition_param.rect`（[0,1] 归一化、x1<x2、y1<y2）。保存走文本替换，只改目标字面量，缩进 / 换行 / 键序 / 其它数字原样保留；保存后重跑图校验，出现保存前没有的新错误就回滚原字节。

## 目录

```
tools/pipeline/
├── studio.cmd           # 完整工作台入口（mpelb + Studio 均隐藏窗口；必须 ASCII+CRLF）
├── mpe.cmd              # MPE 入口（必须 ASCII+CRLF，见文件头 NOTE）
├── truth.py             # 真源自洽校验（图闭合/分层红线/几何；CI 同款）
├── studio/
│   ├── server.py        # 单端口两页服务（回放 + ROI，端口 26530）
│   ├── replay.py        # v5 会话清单/帧索引读取器
│   ├── nodes.py         # 节点矩形索引与文本替换写盘 + 校验回滚
│   └── static/          # 壳页与两个标签（index.html / app.js / style.css）
├── schema/              # pipeline / custom action / custom recognition JSON Schema 三件套
├── guides/              # agent 操作规范（mpe-pipeline-edit.md）
├── mpelb_update.py      # 按官方 release 清单安装/更新 dev/mpelb.exe（--check / --update）
├── mpelb_update.cmd     # 更新入口（优先 .venv，缺省回退系统 Python 3）
└── dev/                 # 本地工具二进制（gitignore；上一版留 mpelb.previous.exe）
```

## 真源

- 活动插件：`plugins/<id>/resources/pipeline/*.json` —— 节点图与锚点
- 跨模块共用链：`maaracing_master/core/resources/pipeline/` —— **当前不存在**：尚无第二个模块接图，抽公共层为时过早。目录按存在性纳入加载；真出现共用链时再建，且受分层红线约束（core 不得引用 `<module>.` 节点，见 `truth.namespace_checks` 与 MAAFW_GUIDE §5.6）
- 启动步骤自有链：`maaracing_master/core/resources/startup/` —— （`pipeline.json` + `image/`）。有终点、有业务归属，由 `LaunchGameStep` 显式加载，**不进**共用目录（否则会被所有模块的导航图一起并入）。规则见该目录下的 `README.md`，决策见 ADR-0018
- 归档鉴宝的图与策略表仍在 `plugins/_archive/treasure/resources/`；默认发现不扫归档区

校验（CI 同款）：`python tools/pipeline/truth.py`

> **纯锚点形态**：`attach._anchor_only` 的节点必须被引用或声明模块直驱；节点名全城唯一、无命名空间，core/plugin 的分离靠引用方向单向守住。

## 模板图采集工作流（2026-09-07 定案，v4 载体沿用）

> 适用：为图节点锚点采集模板图。分工：**人工截图+裁剪标注 → 导出 regions.json + PNG → 工具侧换算入库**（rect 归一化/JSON/校验/提交均为工具侧职责）。

**来源纪律**：只从 1280×720 运行帧截图裁剪（启动即统一 720p），1:1 像素裁剪、**永不缩放**（模板与运行帧同尺度，匹配单尺度 1.0）。

**人工截图通道的坐标系换算（2026-09-16 实测）**：截图工具产出的是**带窗口边框的窗口截图**，实测 1281×759——比客户区多出左侧 1 px 边框与顶部 38 px 标题栏。此类 PNG 必须先裁出客户区（`img[38:758, 1:1281]`，恰 720×1280）再定位、算 rect，否则 ROI 整体偏移 38 px、运行期全部失配。校验办法：取一张来自真正运行帧的既有模板（如 speedrush 域的 `hall_race_btn.png`）在该客户区上匹配，分数应达 ~1.0000。

**裁剪纪律**：只装"一年后还长这样的像素"——角标/数字/倒计时/红点/限时横幅一律留在模板外。变化在模板图**外**（哪怕紧贴）不影响匹配得分；在图**内**才失效（阈值 0.75 容忍渲染抖动，不容忍结构性变化）。

**半透明元素不宜作锚点**：压在动态画布上的 HUD 文字与进度条，其像素是逐帧与背景混合的结果，匹配分随背景大幅摆动。此类位置改取**不透明图标**替代，或退为多锚点 Or 组合兜底。

背景干扰还有**第二个入口**：彩色匹配（`colorspace: rgb`）会把搜索区内的**背景颜色**一起算进相似度——换了不透明图标，分数照样会被天光拉低。只比形状（`gray`）可显著收敛，但**根治要靠"跨天光、多轮样本"标定阈值**：单轮样本读出的"稳定"是假象，阈值贴着实际分布的下缘画，就会在实机上抖动成假失配。各域的实测数据见其域文档（如 [speedrush 域 §1](../../maaracing_master/plugins/speedrush/CODE_WIKI.md)）。

**命名**：`<页面>_<元素>[_限定词].png`，全小写下划线；首段页面名与页注册表一致（`hall_` / `rank_` / `speedrush_`…），看文件名即知归属。

**位置跟 owner 走**：global 锚点 → `core/resources/image/`；模块锚点 → `plugins/<id>/resources/image/`。归属与物理位置矛盾由编辑评审盯。

**回传**：标注工具导出 regions.json（像素区域 x/y/w/h）+ PNG → 换算归一化 rect（外扩 15% 宽 / 25% 高、4 位小数）写入真源。**JSON 真源由 MPE 画布 / Studio 工具链维护，人工不手改裸文件**；agent/脚本直改 JSON 后跑 `truth.py` 机检 + 提交。

> **ROI 是「这个按钮可能出现在哪里」，不是「它上次出现在哪」**：一个节点服务多页时，ROI 必须覆盖各页位置的**并集**再留余量——按单帧模板外扩算出的 rect 会在同一按钮的另一页失配（2026-09-16 speedrush 结算页实证，见其域文档 §4）。

**MAA 对照**：MaaFramework 侧仅约定"720p 无损原图裁剪勿缩放 + `roi`/`box`/`target` 三概念分离"（MAAFW_GUIDE §5.1/§5.2）；社区靠 ImageCropper 类工具 + 人工纪律，无结构化工作流。本仓库在其上加 regions 机器可读导出 + 校验守卫闭环。

## MPE 常见疑惑与排错

面向第一次用 Studio 的开发者。口径：mpelb 与 MPE 在线 stable **必须同代**；项目内用
`mpe.cmd --check` 看本地与官方最新、用 `mpe.cmd --update` 更新，不在文档里写死版本号。
机器本地配置在 `%APPDATA%\MaaPipelineEditor\LocalBridge\`（**重装 mpelb 即丢，换机器要重设**）。
原理与机检口径见 `guides/mpe-pipeline-edit.md` §5，这里只给能照抄的处置。

**打开 MPE 没几秒 LocalBridge 自己退出了**
先怀疑**协议版本不匹配**：MPE 在线版每次大版本更新都会抬高 LocalBridge 协议要求，本地 mpelb
落后时**后端会主动退出**（不是崩溃，是它检测到前后端协议不一致后自行收摊）。日志里是这三行：

```
level=warning msg="协议版本不匹配，前端需求: 1.5.0，当前本地服务协议: 1.4.6"
level=error   msg="检测到前后端协议版本不一致，当前前端需求: 1.5.0，后端协议: 1.4.6"
level=error   msg="请更新 MaaPipelineEditor 或 Local Bridge 后重试，后端即将主动退出"
```

处置：项目内直接运行 `tools\pipeline\mpe.cmd --update`（或 `studio.cmd --update`）。它读取
官方 release 清单 `mpe-manifest.json`，校验 SHA256；干净检出没有二进制时直接安装，已安装时
先把上一版留成 `dev\mpelb.previous.exe`（只留一份）再替换。`--check` 只看不换。更新会先停掉
正在运行的 mpelb。首次安装只要求机器有 Python 3（仓库开发环境按 CONTRIBUTING 建 `.venv`）。

官方的全局安装/更新（会同时准备 MaaFramework/OCR 运行时并加入 PATH）仍然可用：

```powershell
irm https://raw.githubusercontent.com/kqcoxn/MaaPipelineEditor/main/scripts/install/install.ps1 | iex
```

`raw.githubusercontent.com` 不可达时用项目内 `--update`；它走 GitHub release 资产，不依赖 raw 域。

自检：起 `mpe.cmd` 后看 LocalBridge 的 `lb-*.log`；没有“协议版本不匹配”告警即正常。

**首次打开弹"新手引导"答题，挡住工作面**
在浏览器控制台执行 `mpedev("skipNewcomer")` 即可跳过。答过一次后 MPE 自己会提供跳过入口
（v1.8.0 起）。注意 `mpedev` 是前端 dev 接口，官方文档与 release 均未收录，**别写进自动化脚本**。

**打开 MPE 不会自动定位到文件，得手动在文件面板里点**
正常行为：mpelb 只负责把真源**列进**文件面板，不会自动打开任何一个；MPE 会按浏览器本地记录
恢复"上次成功打开的文件"。所以**手点一次**你要编辑的真源（例如 speedrush 的图或归档鉴宝的入口图），
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
git status --short -- maaracing_master/plugins
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
tools\pipeline\dev\mpelb.exe config set-lib "<仓库根>\.venv\Lib\site-packages\maa\bin"
```

该目录含 `MaaFramework.dll`，版本随 `.venv`。`set-lib` 会顺带置 `maafw.enabled=true`。
OCR 资源路径按需另配 `config set-resource`——`.venv` 的 maa 包里没有 `resource/` 目录，
不配则原生 OCR 不可用（MPE 前端 OCR 仍可用），日志那句 WARN 也这么写。

**文件面板里混进退役图**
mpelb 只索引 `pipeline` 目录下的 json，而它的 `file.exclude` 默认**不含** **`archive`**。已在
`config.json` 里加上；自己机器上没加过的话补一下。索引文件数随真源变化，不在这里记。

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
日志 INFO 级**不打请求路径**，要查越界的具体路径就 `--log-level DEBUG` 重起，复现一次后
按返回码判断；需要自连 WS 时，客户端逻辑可从 git 历史里的旧诊断脚本取回。

**只想起 LocalBridge 不起 Studio 服务**
`mpe.cmd` 会一并起两者（26521 / 26530）。只要文件管理就直接跑 `tools\pipeline\dev\mpelb.exe --root <仓库根> --port 26521`；
停服务用 `mpe.cmd --stop`（只停 mpelb）或 `studio.cmd --stop`（mpelb + Studio 服务一起停）。
