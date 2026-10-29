status: active

# game_launch_probe — 直启游戏主程序能不能跑到游戏窗口

**回答什么**：「启动游戏」步骤打算不经官方启动器、直接拉游戏主程序。这条路径是否成立——进程起得来、窗口会不会出现、出现要多久、窗口标题是什么、会不会先弹登录/更新窗、反外挂组件什么时机起来。

**怎么跑**：

```
.venv\Scripts\python.exe tools\experiments\game_launch_probe\probe.py            # 直启主程序
.venv\Scripts\python.exe tools\experiments\game_launch_probe\probe.py --dry-run  # 只打印将要执行的命令
```

脚本自己拉进程（脱离本进程树）、逐秒记录窗口与进程变化、把时间线写成 `result.json` 打在同目录。**不点击、不抢焦点、不改窗口**；跑完游戏仍在运行，需要手动关闭。

**当前状态**：已跑通（2026-10-29 16:33:48 一次）。

## 实测结果

命令：`g112-Win64-Shipping.exe -nosound -nohmd --MuMuLauncher -noexceptionhandler -Hdd -force_load_content`，工作目录为 exe 所在目录。

| 时刻（自拉起） | 事件 |
|---|---|
| 13.2s | 窗口出现，标题 `g112  `，类名 `UnrealWindow`，1038×614（标题在加载中变为「巅峰极速」） |
| 37s | 进入登录流程，开始加载大厅地图（`hall_map` / `LV_Hall_Outside_Night`） |
| 39s | SDK 登录完成（`OnLoginDone, code = 0`），进大厅 |

- 全程没有启动器窗口、没有人工登录、没有更新提示或反外挂弹窗；观测期间也没看到反外挂用户态进程。
- 关闭：按窗口所属进程结束，2 秒内退出（退出前常驻内存约 3.5 GB）。
- 结论：**直启成立**，默认 120s 等待窗口有约 3 倍余量；冷启动（首次读盘）可能更慢，超时给到 180s 更稳。

**探针自身的坑**：首次运行 `result.json` 里 `time_to_game_window_s` 记成 0（窗口对象没带时间戳），实际为 13.2s；已在脚本里修正，以上表为准。


**出口**：结论已进 [ADR-0013](../../../docs/adr/0013-内置步骤与游戏进程归属.md) 与 [core/CODE_WIKI.md §1.17](../../../maaracing_master/core/CODE_WIKI.md)，本目录待删除。
