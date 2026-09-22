# cjk_render_lab —— 中文渲染对比实验

status: active

## 问题

用户观感：GUI 文字相比其他软件「差点意思」。这是不可证伪的主观描述，本实验把它拆成**三个可判定维度**：字体族 / 排版参数 / 渲染锐度，由用户在对比页上指认哪一类成立。

## 已核事实（2026-09-22）

- 字体栈真源：`apps/MaaRacingMaster.Shell/frontend/style.css` `:root` 的 `--mra-font-sans = "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", system-ui, sans-serif`（PingFang SC 是 macOS 字体，Windows 上永远落空）；`--mra-font-mono = Consolas, ...`。
- 字号分布：11px/12px 占大头（元信息/徽章/描述），正文 12–13px，标题 13–14px/600，日志行高 1.7。
- 宿主：WinAppSDK 1.8，`app.manifest` 已声明 `PerMonitorV2`；WebView2 以 file:// 直载 index.html。
- 本机已装中文候选字体：Microsoft YaHei UI（Regular/Bold/Light）、Noto Sans SC（思源黑体 Google 发行版，Thin–Black 全字重）、等线（Light/Regular）。
- WebView2 与 Edge 同为 Chromium 渲染管线；Windows 上 Chromium 中文为灰度抗锯齿（无 ClearType 子像素）。

## 假设清单

- **H1 字体族**：雅黑小字号发灰、字形松散，对照软件用了更现代的字体 → 改 `--mra-font-sans`。注意：改字体栈只在本机有效，**玩家机器未必装了新字体，正式分发需打包字体文件**（Noto/思源为 SIL OFL 许可，允许再分发；属第三方资产，落地前须确认）。
- **H2 宿主锐度**：非整数 DPR / 缩放链路导致整体发虚 → 换字体无效，修宿主。PerMonitorV2 已声明，嫌疑较低，探针环境读数可证。
- **H3 排版参数**：小字号 + letter-spacing 0 + 行高密度 → 只调 token/CSS，零依赖小改动。
- **H4 引擎 AA**：对照对象是原生 Win32/Qt 软件（ClearType 子像素）→ CSS 层无法等价，只能靠字重/字号缓解；此为边界结论，需向用户明说。

## 仪器

`render_probe.html` —— 自包含单文件，无外部依赖，双击用 Edge/Chrome 打开；或在 Shell 宿主内按 F12 → Console，把 `location.href` 指向本页在本机仓库中的 file:// 绝对路径（按本机仓库位置拼，不写入仓库），此时环境读数的 DPR 即真机宿主值。浏览器里看请先 Ctrl+0 归零缩放。

页面内容：① 现状基线（逐条复刻 GUI 当前 token 的角色样张）→ ② 字体族矩阵（JS 文本测宽自动探测本机字体，未装则标灰跳过）→ ③ 排版参数（letter-spacing / 行高 / 数字混排）→ ④ 锐度参照（1px/0.5px 细线 + 10–11px 小字）→ 判读指南。

## 判据

- ② 里换字体明显更顺眼 → H1：先改字体栈真机验证，再议打包分发。
- ③ 里某参数明显改善 → H3：改 token。
- ④ 所有字体整体发虚 → H2：查 DPR/缩放链路。
- 对照原生软件仍觉不够「锐」→ H4：边界结论，明说。
- 判据落在用户肉眼对比，agent 不代判。

## 退出计划（四步）

结论迁 home（前端 README / style token 注释；若打包字体则立 ADR 记录选型与许可）→ 仪器留档（git 历史）→ 拔引用 → 删目录。
