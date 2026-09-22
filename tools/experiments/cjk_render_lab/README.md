# cjk_render_lab —— 中文渲染对比实验

status: active

## 问题

用户观感：GUI 文字相比其他软件「差点意思」。这是不可证伪的主观描述，本实验把它拆成**三个可判定维度**：字体族 / 排版参数 / 渲染参照，归因走**逐层排除**（字体 → 参数 → 渲染链），不做单点定因。

## 已核事实（2026-09-22）

- 字体栈真源：`apps/MaaRacingMaster.Shell/frontend/style.css` `:root` 的 `--mra-font-sans = "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif`（PingFang SC 是 macOS 字体，Windows 上永远落空）；`--mra-font-mono = Consolas, ...`。
- 字号分布：11px/12px 占大头（元信息/徽章/描述），正文 12–13px，标题 13–14px/600，日志行高 1.7。
- 宿主：WinAppSDK 1.8，`app.manifest` 已声明 `PerMonitorV2`；WebView2 以 file:// 直载 index.html。
- 维护者本机已装中文候选字体（PowerShell InstalledFontCollection 查询）：Microsoft YaHei UI（Regular/Bold/Light）、Noto Sans SC（思源黑体 Google 发行版，Thin–Black 全字重）、等线（Light/Regular）。
- 雅黑真字重只有三档：CSS 请求 500 按字体匹配规则落到 Regular，600 落到 Bold；Noto Sans SC 的 500/600 有真字重面。

## 假设清单

- **H1 字体族**：雅黑小字号发灰、字形松散，对照软件用了更现代的字体 → 改 `--mra-font-sans`。注意：改字体栈只在本机有效，**玩家机器未必装了新字体，正式分发需打包字体文件**（思源/Noto 为 SIL OFL 许可，允许再分发；属第三方资产，落地前须确认）。
- **H2 宿主锐度**：非整数 DPR / 缩放链路导致整体发虚 → 换字体无效，修宿主。PerMonitorV2 已声明，嫌疑较低；页内读数只提供线索。
- **H3 排版参数**：小字号 + letter-spacing 0 + 行高密度 → 只调 token/CSS，零依赖小改动。
- **H4 渲染链（逐层排除项）**：对照原生软件的观感差异，成因不做预设；仅当字体与参数实验均无明显改善、且软化特征跨字号一致时，才进入渲染链专项排查（系统 DPI → WebView2 viewport → 页面缩放 → CSS 布局 → 字体选择/fallback → 文本栅格化 → 屏幕像素）。

## 仪器

`render_probe.html` —— 自包含单文件，无外部依赖。双击用 Edge/Chrome 打开；或在 Shell 宿主内按 F12 → Console，把 `location.href` 指向本页在本机仓库中的 file:// 绝对路径（按本机仓库位置拼，不写入仓库），此时环境读数即真机宿主值。浏览器里看请先 Ctrl+0 归零页面缩放。

页面结构：

1. **环境读数**：devicePixelRatio（受系统缩放与页面缩放共同影响）+ visualViewport.scale + 屏幕尺寸——只作线索，不作归因结论。
2. **① 现状基线**：逐条复刻 GUI 当前 token 的角色样张。
3. **② 字体族矩阵**：候选与安装状态为**静态清单**（来自维护者本机 PowerShell 查询，写死页面）；**不做运行时安装探测**——文本测宽启发式存在局部 fallback 误判空间，不作为实验结论依据。Segoe UI 行仅观察拉丁字形与回落链。
4. **③ 排版参数**：letter-spacing / 行高；数字混排行**仅数字套 mono**（中文保持 UI 字体），不测整句换 mono。
5. **④ 渲染参照（三段式）**：A 几何栅格化参照（2px/1px/0.5px 线；线条是几何图形，不能判定文字渲染质量；0.5px 为亚像素覆盖样本，只作线索）→ B 文字锐度基准（同句扫字号 12/13/14/16 × 字重 400/500/600，观察是否出现字号阈值现象；雅黑 500→Regular、600→Bold 的映射为档位事实）→ C 缩放基准（区分系统 DPI 与页面缩放的判读协议）。

## 判据（逐层排除）

1. **字体层**：② 中某字体同等字号下明显更清晰/顺眼 → H1 成立，先改字体栈真机验证，再议打包分发。
2. **参数层**：③ 与 ④B 中某字号/字重/间距组合明显改善 → H3 成立，改 token。
3. **渲染链**：仅当 1、2 均无改善且软化特征跨字号一致 → 进入 H4 专项排查；几何读数与 DPR/scale 只提供线索，不单独定因。
4. 判据落在用户肉眼对比，agent 不代判；页内自动读数不充当归因结论。

## 退出计划（四步）

结论迁 home（前端 README / style token 注释；若打包字体则立 ADR 记录选型与许可）→ 仪器留档（git 历史）→ 拔引用 → 删目录。
