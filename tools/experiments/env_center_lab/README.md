# env_center_lab —— 环境中心（权限优化中心改造）样本

status: active

## 目标

把「权限优化中心」改造为**「环境中心」**：两个 tab——「权限优化」（现有内容原样迁入）+「可选依赖」（新增：手柄驱动 ViGEmBus、字体 Noto Sans SC）。设置页卡片「权限优化」改「运行环境」，按钮改「打开环境中心」。

## 已核事实（2026-09-22）

- 现中心实现：`frontend/js/settings.js` `openOptimizerCenter()`（modal，maxWidth 540，汇总条三档计数 + 一键优化全部 + 三态卡片列表 `optimizerItemHtml`，事件委托到 modal.card）；后端 `core/sidecar.py` `get_registry_optimizations`，现有三条：Xbox 后台捕获（ms-gamebar 弹窗）/ 手柄 UI 导航（打字弹手柄虚拟键盘）/ ms-gamebar 协议弹窗（获取应用对话框）。
- 生态检索：MAA 仓库 issues 检索「依赖检测」未见 GUI 依赖引导中心先例（检索记录 2026-09-22，条目均无关）；本改造为生态内无直接对标的扩展。
- 依赖条目两条候选均有真实降级路径与证据：ViGEmBus（官方仓库 nefarius/ViGEmBus，内核驱动；净机走前台鼠标降级分支，treasure/module.py 有注释与日志）；Noto Sans SC（缺失回落雅黑，2026-09-22 字体栈已改 Noto 优先，commit 9285e76）。

## 收录判据（可选依赖 tab 准入，三条同时满足）

1. 缺失不阻断核心功能（有降级路径）；
2. 降级行为明确、可向用户一句话说清；
3. 修复动作可引导（打开官方页 / 打开系统设置）。

红线：**驱动类只引导不代装**（内核驱动自动安装越权）；字体走"引导装系统"路线，与打包 webfont 互斥，不并行。

## 两种条目交互语义（tab 隔离的理由）

- 权限条目 = 开关：优化/恢复系统默认，动 Windows 配置；
- 依赖条目 = 检测 + 引导：不动系统配置，缺失时给"缺什么 / 影响什么 / 去哪装"。

## 判据

样本页 `env_center_demo.html`（自包含）：设置卡片改前/改后条 + 环境中心 modal 样本（tab 切换、权限 tab 三条真实条目、依赖 tab 字体条目以 `document.fonts.check` 实探当前环境、ViGEmBus 条目带模拟状态切换看双形态）。用户肉眼确认形态与文案 → 并入生产（settings.js/modal.js/style.css + sidecar 依赖检测接口 + 启动体检联动）→ 实验四步退出。

## 退出计划（四步）

结论迁 home（前端 README 运行环境节 + core CODE_WIKI 侧车接口说明）→ 仪器留档（git 历史）→ 拔引用 → 删目录。
