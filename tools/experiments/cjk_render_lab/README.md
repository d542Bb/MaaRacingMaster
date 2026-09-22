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

## 字体选型许可核查（2026-09-22）

采纳自委托研究（原文不进树；条款均为官方一手出处逐字核验，页面对象为厂商官网 / 官方包内协议）：

| 候选 | 内嵌分发（随应用打包） | 义务与限制 | 全量体积 | 子集化/转格式 |
|---|---|---|---|---|
| MiSans（小米） | **❌ 明文禁止**：「不得……进一步分发字体软件或其任何副本」，豁免仅覆盖「用字体创作的作品」 | 需注明使用；不可改编 | 主包 227MB（全语言合集 379MB） | 未获允许 |
| OPPO Sans 4.0 | ✅ 包内协议明示「embed, bundle, redistribute and/or sell unmodified copies with any software except for fonts software」 | 显著声明使用+随附协议副本；**不得修改**；revocable、non-transferable | 4.0 单 ttf 21.7MB（3 字重） | ❌ 不得修改 |
| HarmonyOS Sans | ✅ 同上模板明示允许 | 同上 | SC 单字重 8.1–8.4MB × 6 字重 | ❌ 不得修改 |
| Noto Sans SC（思源） | ✅ OFL 1.1：允许 bundled/embedded/redistributed and/or sold with any software | 随附 OFL.txt + 版权声明；不得单独出售字体；RFN「Source」限制改名 | 可变 ttf 16.9MB（wght 100–900） | ✅ 允许修改（子集化/woff2 均可） |

出处：MiSans 协议 PDF（hyperos.mi.com/font-download/MiSans字体知识产权许可协议.pdf）；OPPO Sans 4.0 包内 License Agreement（coloros.com/article/A00000074/，CDN 直链 coloros-website-cn.allawnfs.com/font/OPPO_Sans_4.0.zip 需 Referer）；HarmonyOS Sans zip 内 LICENSE.txt（developer.huawei.com/consumer/cn/design/resource/，直链 developer.huawei.com/images/download/general/HarmonyOS-Sans.zip）；Noto OFL.txt（github.com/google/fonts ofl/notosanssc）。

**打包技术关（待实测）**：壳前端以 file:// 直载 index.html，`@font-face` 引用本地字体文件可能被 Chromium 的字体 CORS 策略拦截；若被拦，宿主侧以 `SetVirtualHostNameToFolderMapping` 建虚拟主机映射为标准解法。落地前须在 WebView2 宿主实测本条。

## 假设清单

- **H1 字体族**：雅黑小字号发灰、字形松散，对照软件用了更现代的字体 → 改 `--mra-font-sans`。候选许可核查见上节：MiSans 出局；OPPO/HarmonyOS 可嵌但不得修改（体积只能全量）；Noto 许可最自由。**玩家机器未必装了新字体，正式分发需打包字体文件（属第三方资产，按上节许可落地）。**
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
