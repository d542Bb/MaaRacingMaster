status: active

# builtin_steps_ui — 启动/关闭游戏步骤的界面形态预览

**回答什么**：把「启动游戏 / 关闭游戏」作为任务列表里的普通模块卡，视觉上是否成立——同一个任务列表、同一个选卡弹窗、同一套展开面板，不加任何第二套操作入口。

**怎么用**：直接用浏览器打开 `preview.html`。样式表指向仓库真实前端（`apps/MaaRacingMaster.Shell/frontend/style.css`），外观与实机同源，不是另画的稿子。

- 默认视图 = 主控页三张任务卡（启动游戏 / 极速狂飙 / 关闭游戏）
- `preview.html?view=settings` = 设置页「运行选项」
- `preview.html?view=gallery` = 管理模块选卡弹窗
- `preview.html?expand=close_game` = 直接展开某张卡（id 取 launch_game / close_game）

**当前状态**：形态已按本预览落地（core 侧步骤 + 前端卡片）。实机以真实前端为准，本页只留作设计过程记录。

## 已定形态

启动游戏直接拉**游戏主程序**，不经官方启动器（静默、无启动器窗口）。选项：程序路径（留空自动探测）、启动参数、等待窗口超时、已在运行则跳过、独立进程、启动失败时回退到启动器。

关闭游戏只有一个选项「关闭后退出本程序」——「跑完自动退出」并进这张卡，不再占设置页一行。

## 启动链盘面事实（本次取证，供实现参照）

- 官方启动器 `Launcher.exe` 起游戏前做：版本比对 → 远端 hash 清单 → 本地 MD5 增量校验 → 清 ShaderCache → 磁盘检查 → 拼参数 → 起游戏。启动器自身窗口约 6.6s 出现。
- 游戏主程序实际命令行（游戏日志 `LogInit: Command Line` 原文）：`-nosound -nohmd --MuMuLauncher -noexceptionhandler -Hdd -force_load_content`。
- 反外挂组件独立安装（`C:\Program Files\AntiCheatExpert`），随游戏启动拉起；服务平时 Stopped。

**出口**：已在实机验证后删除本目录；决定与结论见 [ADR-0013](../../../docs/adr/0013-内置步骤与游戏进程归属.md) 与 [core/CODE_WIKI.md §1.17](../../../maaracing_master/core/CODE_WIKI.md)。
