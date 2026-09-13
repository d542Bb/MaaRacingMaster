# Shell 前端（WebView2）

MaaRM GUI 的界面层：`index.html`（静态骨架）+ `app.js`（页面逻辑与 WebView2 桥）+
`style.css`（样式）+ `icons.js`（图标真源）+ `vendor.morphicons.js`（图标变形动画库）。
Shell 经 `file://` 绝对路径直接加载（见 `MainWindow.xaml.cs`），无构建链、无 npm 安装步骤、
**禁止引入任何 CDN 运行时依赖**（离线可用是硬约束）。

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
  （换 icon 即弹簧变形）；`defineMorphIcon()` 已由 app.js 启动时调用。
- 现成用例：数据页「实时预览」放大/还原按钮（scan ↔ shrink）。
  成就系统等后续动画场景直接复用这套模式。

## 冒烟验证

无构建链也能验证：仓库根起临时 HTTP 服务指向本目录，浏览器打开
（app.js 通信层会因无 WebView2 桥报预期错误，不影响图标层验证）；
或在 node 里 `global.window={}; require('./icons.js')` 后逐个
`MRAIcons.svg(name)` 渲染检查。
