# Shell 前端（WebView2）

> **本文是什么**：Shell 前端的开发与图标规范——文件构成、图标唯一真源、`file://` 约束、无 CDN 硬约束。
> **本文不是什么**：不是壳工程与 IPC 契约（见 [../README.md](../README.md)）；不是 GUI 宿主选型的理由（见 [ADR-0004](../../../docs/adr/0004-GUI宿主定案.md)），窗口与标题栏形态的理由见 [../README.md](../README.md)「窗口形态与标题栏」（原 ADR-0005，按 [ADR-0006](../../../docs/adr/0006-决策粒度只收跨模块约束.md) 迁入）。
> **信源等级**：L2 —— 可作「前端怎么改、图标怎么加」的直接依据；图标数据真源 = [`icons.js`](./icons.js)。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../../AGENTS.md)。

MaaRM GUI 的界面层：`index.html`（静态骨架）+ `app.js`（页面逻辑与 WebView2 桥）+
`style.css`（样式）+ `icons.js`（图标真源）+ `vendor.morphicons.js`（图标变形动画库）。
Shell 经 `file://` 绝对路径直接加载（见 `MainWindow.xaml.cs`），无构建链、无 npm 安装步骤、
**禁止引入任何 CDN 运行时依赖**（离线可用是硬约束）。

## 远程数据渲染口径（硬约束）

关于页的公告卡与「版本与更新」卡渲染的是**远程元数据**（公告 JSON、release 标记，规范见
[`docs/announcement.md`](../../../docs/announcement.md)），一律按不可信输入对待：

- **动态文本只走 `textContent` / DOM API**（用 `mkEl()` 建元素再填文本），不得拼字符串进
  `innerHTML`。远程渲染路径上唯一允许 `innerHTML` 的位置是 `appendIcon()`——它只喂本仓库
  静态 SVG 串。回归锁：[`tests/test_frontend_remote_render.py`](../../../tests/test_frontend_remote_render.py)。
- **外链只报逻辑目标名**：`openTarget('home' | 'issue' | 'docs' | 'announcement' | 'download')`，
  地址由 sidecar 侧白名单给出（[`core/remote_meta.py`](../../../maaracing_master/core/remote_meta.py)
  的 `EXTERNAL_TARGETS`）；前端不持有、也不传递可打开的 URL。
- `index.html` 里 `data-link` 的属性值**就是目标名**。新增外链入口必须同时在 `EXTERNAL_TARGETS`
  里登记，否则静态锁红（名字对不上时按钮点了没反应，属静默失效）。

理由：前端持有 `mra.call()` RPC 能力，远程数据一旦能形成 XSS，就会顺着 RPC 放大成
「可信程序替攻击者打开任意站点」。两层防线各自独立成立——渲染层不把数据当标记解析，
RPC 层不信任调用方给的地址。

## 图标规范

- **唯一真源是 `icons.js`**（`window.MRAIcons`）。数据格式为 IconNode
  （`[标签, 属性]` 元组数组，与 Lucide / morphicons 兼容）。
- **UI 图标一律来自 Lucide**（当前锁定 v1.45.0，ISC 许可，声明见仓库根
  `THIRD_PARTY_LICENSES.md`）。新增图标：到 <https://lucide.dev> 找同名图标，
  把 `<svg>` 内的几何元素转成 IconNode 拷入 `icons.js` 的 `LUCIDE` 分区并注明来自 lucide；
  **禁止脱离真源随手画**。Lucide 确无对应图标时，放入 `CUSTOM` 分区，须按 Lucide 规格
  绘制（24×24 网格、`fill="none"`、stroke-width 2、round linecap/linejoin），并注明来历。
- **渲染方式**：
  - 静态 HTML：写占位 `<i data-icon="图标名" ...>`，app.js 启动时 `MRAIcons.hydrate()`
    替换为真 `<svg>`（占位元素上的 style/class 会搬到 svg 上）。
  - JS 动态模板：`${MRAIcons.svg('图标名'[, {class: '...'}])}`。
  - 变形动画：`MRAIcons.node('图标名')` 取 IconNode 喂给 `<morph-icon>`。
- **不属于图标系统的两类图形**（刻意排除，勿迁入 icons.js）：
  - 窗口标题栏 chrome（最小化/最大化/关闭，自绘 10×10，模拟 Windows 原生标题栏度量），
    留在 `index.html` 原位；
  - 性能走势图（`perf-spark` 内联 polyline），是数据可视化不是图标。
- **实心媒体图标**（`media-play/pause/stop`，实心是媒体控件惯例画法）不参与变形动画，
  `MRAIcons.node()` 对其直接抛错。

## 图标动画（morphicons）

- `vendor.morphicons.js` 是 morphicons v1.7.1（MIT）的 vendor 摊平版：上游 dist 的 5 个
  ESM chunk 机械合并为经典脚本挂 `window.MorphIcons`，未改动函数体。摊平是必须的——
  上游是相对 import 的多 chunk ESM，`file://` 页面下模块静态 import 会被拦截。
  **升级时重新摊平，勿手改本文件函数体**。
- 用法：`<morph-icon reduced-motion="user">` + `el.icon = MRAIcons.node(...)`
  （换 icon 即弹簧变形）；`defineMorphIcon()` 已由 rpc.js 启动时调用。
- 现成用例：数据页「实时预览」放大/还原按钮（scan ↔ shrink）；
  主控页日志复制按钮的结果反馈（copy ↔ check / x，见 `js/log.js`）。
  成就系统等后续动画场景直接复用这套模式。

## 键盘可达性（免维护约定）

- **焦点环**：style.css 顶部一条全局 `:focus-visible { outline: 2px solid var(--mra-primary) }`
  覆盖所有交互元素；`:focus-visible` 只在键盘/程序化导航时命中，鼠标点击不出环，
  所以**控件类里不要再写 `outline: none`**（旧四处已删）。特例只允许改偏移：
  窗控按钮贴屏幕边缘用 `outline-offset: -2px` 内收。
- **模态焦点陷阱**：`openModal`（js/modal.js）自带——开卡聚焦卡内第一个可聚焦元素、
  Tab/Shift+Tab 卡内回环、关闭归还焦点、`role=dialog + aria-modal + aria-labelledby`。
  经 openModal 的弹窗零成本继承；自绘浮层若要做陷阱，参照其实现而不是另起一套。
- **开关 aria 接线**：`.option-row` 结构里的 `.mra-toggle[role="switch"]` 由
  `wireSwitchAria`（js/settings.js）按结构自动挂 `aria-labelledby`/`aria-describedby`
  （引用 id 从开关自身 id 派生 `-label`/`-desc`）。**新开关只要按 option-row 既有结构
  写、开关带 id 即可**，不要手写 aria 属性；函数幂等，静态启动跑一次、模板重渲染在
  `bindModulePages` 再跑一次。

## 文字扫描光效（`.mra-text-scan`）

给任意文字元素加 `.mra-text-scan` 即生效，无需额外 DOM。两层背景裁进文字：底层铺基色、
上层是一条高光带，动画只平移高光带的 `background-position`；默认 6s 周期里前 1.8s 扫过一次、
其余静止，速度与频率刻意压低，供长时间驻留的提示文字使用。`prefers-reduced-motion: reduce`
下退化为静态文字。

- 现成用例：底栏状态文字——`app.js` 的 `setStatus` 在运行中挂类、结束时摘掉。
- 现成变体：`.mra-text-scan--rave`（动态彩虹字 + 白光束 + 色相流转），默认不启用，加类即用。

可调项（全是 CSS 变量，元素内联或派生类覆写）：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `--mra-scan-base` | `currentColor` | 基色，默认继承元素自身文字色 |
| `--mra-scan-beam` | `#FFFFFF` | 光带色 |
| `--mra-scan-angle` | `100deg` | 光带倾角 |
| `--mra-scan-half` | `5%` | 光带半宽（相对光带层） |
| `--mra-scan-span` | `250%` | 光带层宽度倍数：越大光带越细，≥200% 才扫得完 |
| `--mra-scan-cycle` | `6s` | 周期时长 |
| `--mra-scan-timing` | `linear(0, 1 30%, 1 100%)` | 周期内节律：前 30% 走完行程、其余静止 |
| `--mra-scan-hue-cycle` | `6s` | 附加动效「色相流转」的周期 |
| `--mra-scan-anim` | 只有扫描光带 | 动效清单，要叠加附加动效就覆写它 |
| `--mra-scan-base-layer` / `--mra-scan-layer` | 由上面几项合成 | 底层 / 光带层的整层渐变，换配色写到这一层 |
| `--mra-scan-rainbow-l` / `--mra-scan-rainbow-c` | `0.7` / `0.16`（仅 `--rave`） | 彩虹字的 OKLCH 感知亮度 / 彩度 |
| `--mra-scan-weight` | `600`（仅 `--rave`） | 字重，小字号彩色字的可读性靠它 |

### 彩虹配色走 OKLCH 的原因

RGB 色标或 HSL 拼出来的彩虹，各色相的真实亮度差很大（黄显亮、蓝显暗），横铺一行就是
「明度忽明忽暗」。做法是写进**感知均匀的极坐标空间 OKLCH**：固定感知亮度 L 与彩度 C，只让色相
绕一圈——`linear-gradient(90deg in oklch longer hue, oklch(L C H), oklch(L C H))`（两个色标
同色相 + `longer hue` 即走满整圈）。逐列实测行内 OKLab 感知亮度极差（900×40 条带）：

| 配色 | L 极差 | 平均彩度 |
| --- | --- | --- |
| RGB 色标彩虹 | 0.281 | 0.188 |
| HSL `longer hue` | 0.471 | 0.225 |
| **OKLCH L=0.70 C=0.16** | **0.024** | 0.153 |
| OKLCH L=0.70 C=0.24 | 0.089 | 0.190 |

C 不能拉太高：超出色域的颜色会被浏览器裁回 sRGB，而裁剪会连带改变亮度（C=0.24 那行的起伏就是
裁出来的）。0.16 已接近实测不触发裁剪的上限。

### 动态版走色相角，不走 `filter: hue-rotate()`

`filter: hue-rotate()` 是线性矩阵近似，实测把色相转到 120° 时上面那条 0.024 会涨到 **0.117**，
明度抖动又被请回来。动态改用注册过的自定义属性承载色相角：

```css
@property --mra-scan-hue { syntax: '<angle>'; inherits: false; initial-value: 0deg; }
@keyframes mra-text-scan-hue { from { --mra-scan-hue: 0deg; } to { --mra-scan-hue: 360deg; } }
```

色相角写进渐变色标，动画每帧重算彩虹，明度与彩度不动（各相位实测恒为 0.024）。未注册的自定义
属性无法平滑插值、只会跳变，`@property` 是前提。整套需要 Chromium 111+ 的渐变颜色插值
（`in oklch longer hue`）与 `@property`；WebView2 常青自动更新，实测 Edge 150 全部支持。

### 小字号彩色字的坑

11px 彩色细笔画会被抗锯齿冲淡，肉眼近乎灰／透明：同一款彩虹 11px 发灰、22px 转深、44px 才饱满，
且不经本效果、直接 `background-clip: text` 的对照组同样发灰——与效果实现无关。对策是抬字重
（`--rave` 默认 600），换更深或更饱和的配色救不回来。

### 自己写变体

把光带换成一道等亮度彩虹窄带（色相只取一段，明度彩度仍恒定）：

```css
.my-scan-rainbow {
  --mra-scan-layer: linear-gradient(100deg, transparent 42%,
    oklch(0.70 0.16 20) 46%, oklch(0.70 0.16 90) 50%,
    oklch(0.70 0.16 200) 54%, transparent 58%);
}
```

## 运行日志卡片

主控页右栏的日志按**区块**渲染（`app.js` 的 `appendLogs` / `finalizeSection`），几条别改坏的不变量：

- **面板上的第一行必定成卡**：命中锚点即开新区块，未命中锚点但当前无卡时也开（隐式锚点）——
  首个锚点之前的运行环境行（PEEP 预览 / 配置注入 / 连接窗口前）同样在卡内，没有裸行旁路。
  清空日志后同理，下一行重新开卡。
- **卡片一律默认折叠**，展开与否全交给用户点击（含 ERROR 的卡也不自动展开）；细节行照常
  追加进正文并按行数刷新徽章，含错误/警告时卡头挂「!」标记。**当前正在写入的那张卡也要绑
  点击开关**——建卡时就得 `bindSectionToggle`，只在收尾时绑会让「当前卡点不开」。
- **计数徽章必须按 `.log-badge--count` 取**，不能用裸 `.log-badge`：`!` 标记同样带
  `.log-badge`，裸取会拿到标记本身，它被移除后 `insertBefore` 抛 `NotFoundError`，
  整批剩余日志行静默丢失。
- **展开动画**是正文的高度过渡（`height: 0` ↔ `height: auto`），靠 `interpolate-size:
  allow-keywords` 才成立（Chromium 129+，WebView2 常青）；引擎不支持时只是退化为瞬时开合，
  版式不受影响。箭头与正文同拍（同为 0.18s），`prefers-reduced-motion` 下过渡关闭。
- **只在已贴底时才自动跟随**：贴底判定必须在追加之前取（追加会抬高 `scrollHeight`），
  否则用户翻旧卡时每轮轮询都会被拽回底部。

## 冒烟验证

无构建链也能验证：仓库根起临时 HTTP 服务指向本目录，浏览器打开
（app.js 通信层会因无 WebView2 桥报预期错误，不影响图标层验证）；
或在 node 里 `global.window={}; require('./icons.js')` 后逐个
`MRAIcons.svg(name)` 渲染检查。

样式与动效（纯 CSS 部分）可用无头 Edge 定格取帧，不必靠截图撞运气：

```powershell
msedge.exe --headless=new --screenshot=out.png --window-size=900,240 "file:///…/probe.html"
```

- **钉住动画相位**：元素写 `animation-play-state: paused` + `animation-delay: -Xs`
  （多条动画用逗号对齐顺序，如 `-3s, -2s` 分别钉光束与色相），页面一加载就停在该相位。
- **量化颜色**：把同一套类与变量铺成实心条带（`background-clip: border-box` 覆盖），
  逐列做 sRGB→OKLab 换算后统计感知亮度极差／彩度均值，用数字判断「明度是否真的平」，
  比目视可靠——本次彩虹配色就是这么从 RGB／HSL 换到 OKLCH 的。
- **查语法与特性支持**：`--dump-dom` 跑一个用 `CSS.supports()` 逐条打印结果的页面，
  拿到的是当前引擎的真实判定（比猜版本号可靠）。

### 驱动真实 app.js 的桥桩探针（渲染路径验收）

`app.js` 启动即访问 `window.chrome.webview`，直接开 `index.html` 会因无桥而中断。要验证
「远程数据在**真实渲染路径**下的表现」（公告/更新卡是否只出纯文本、按钮发出的调用形态），
用一份**临时副本**：复制 `index.html`，在 `<body>` 后插桥桩、在 `</body>` 前插判据脚本。

- **桥桩**：`window.chrome = { webview: { addEventListener, postMessage } }`；`postMessage`
  里按 `msg.method` 回包（`fetch_announcement` / `check_update` 喂构造好的 payload）。
  回包必须 `setTimeout(..., 0)` **异步**派发——同步派发会让 app.js 的初始化在同一次调用里重入。
- **判据**：点 `[data-tab="about"]` 切页、点卡片按钮，然后把桥桩记录的调用
  （`window.__calls`）、两个卡片的 `innerHTML`、以及 `window.__xss` 有没有被
  `onerror`/`onload` 置位，写进 `<pre id="probe-out">`。
- **取回**：`--dump-dom` 抓判据，`--screenshot` 看版式（两条命令同上面的无头 Edge 用法）。
  ⚠️ `--screenshot=` 要给 **Windows 路径**：Edge 不认 Git Bash 的 `/tmp/...`。
- **读法**：payload 在卡片 `innerHTML` 里应呈现为 `&lt;img …&gt;`（转义文本）、`__xss`
  保持 undefined、外链调用只有 `open_external_url {"target":"…"}` 形态。

探针是临时验证产物，**用完即移出仓库**（见 [`AGENTS.md`](../../../AGENTS.md) 红线 4）。
