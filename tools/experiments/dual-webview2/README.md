# 同进程双 WebView2 探测（dual-webview2）

## 问题（C 类：外部系统行为，禁止靠猜）

PEEP 脱离 GUI 的悬浮窗方案，要求主窗口的 WebView2 已存在时，**同进程**再创建第二个
WebView2，加载 `file://` 页面，并独立跑通 WebMessage RPC 转发（回发按 `sender` 定位，
不得与主窗口串台）。这条不成立，方案就得退回原生控件渲染。

## 方法

在 shell 项目内临时挂一个探测窗（`OverlappedPresenter` 工具窗 + 置顶 + 无系统边框 +
内嵌 WebView2），加载本目录的 `probe.html`：

- 探针页：`probe.html` / `probe.js`（本目录保留，可复跑）
- 宿主临时窗：`ProbeWindow.xaml(.cs)`（**探测结束已删除**，复现方式见文末）
- 探针每 500ms 调一次 `get_debug_state` 与 `get_peep_frame`，结果 postMessage 回宿主写日志，
  同时渲染在窗口上便于肉眼确认
- 日志：`%TEMP%\maarm_dualwebview2_probe.log`
  （坑：DSH 沙箱下 shell 看到的 `$env:TEMP` 是被重定向的 `...\Temp\dsh-XXXXXX`，
  与提权进程 `Path.GetTempPath()` 不是同一目录，找日志要看真实用户 Temp）

## 结论（2026-09-15，Windows App SDK 1.8.260710003 / net8.0-windows10.0.19041.0）

**全部通过，悬浮窗方案的技术前提成立。**

| 验证点 | 结果 |
| --- | --- |
| 同进程第二个 WebView2 实例化 | 通过 |
| `window.chrome.webview` 桥在第二实例可用 | 通过（`chrome.webview = true`） |
| 加载 `file://` 页面 | 通过（`success=True`） |
| 经第二实例调真 sidecar 的 RPC | 16 轮全通，0 失败 |
| 两实例回发是否串台 | 无「未知 callId」告警（回发确按 `sender` 定位） |
| 无帧时的返回结构 | 正常返回 `{frame: null}`（会话未运行，符合预期） |

## 证据（日志摘录，完整 4.1KB）

```
16:49:10.589 probe-window-created（第二个 WebView2 已实例化）
16:49:10.727 page> probe.js 已执行，chrome.webview = true
16:49:10.735 page-loaded success=True status=Unknown
16:49:11.228 rpc via 2nd webview: get_debug_state ok=True err=-
16:49:11.229 page> 第1轮 RPC 通，peep_enabled=false
16:49:11.231 rpc via 2nd webview: get_peep_frame ok=True err=-
…
16:49:19.227 page> === 汇总 rpc_ok=16 rpc_fail=0 frame_ok=0 frame_null=16 最大帧长=0 ===
```

程序在探测结束后正常退出（窗口被关闭），Windows 应用程序日志无 .NET Runtime / 应用程序错误记录，
排除崩溃。

## 未覆盖的验证点

- **有真帧时的表现**未验：需要运行会话且会话启动时 PEEP 已开。RPC 通道与数据结构已在无帧条件下验证，
  真帧只影响载荷非空，不引入新的技术风险。
- **置顶/无边框的观感**未留截图（窗口在截图前已被关闭）。该项在正式实现里随 PeepWindow 一并验收。

## 复现方式

1. 在 `apps/MaaRacingMaster.Shell/` 加一个临时 `ProbeWindow`（XAML 里放一个 `WebView2`），
   构造函数中按需设 `OverlappedPresenter.IsAlwaysOnTop / SetBorderAndTitleBar`，
   把 `web.Source` 指向本目录 `probe.html`；
2. 消息处理与 `MainWindow.HandleCallAsync` 同构：解析 `type=call` → 调
   `_sidecar.CallAsync(method, null, TimeSpan.FromSeconds(10))` → 用 `sender.CoreWebView2.PostWebMessageAsJson` 回发；
3. 在 `MainWindow` 构造函数尾部延迟 4 秒打开它（等 sidecar 就绪）；
4. `dotnet build apps\MaaRacingMaster.Shell\MaaRacingMaster.Shell.csproj -c Debug -p:Platform=x64`；
5. 以管理员启动 exe（manifest 要求 `requireAdministrator`），读 `%TEMP%\maarm_dualwebview2_probe.log`。

注意：shell 沙箱（DSH）不允许子进程写 `apps/**`，构建需在放宽模式下执行。
