# MaaRacingMaster.Shell — GUI 壳工程（WinUI 3 + WebView2）

> **本文件是什么**：壳工程与 sidecar transport 的**施工事实**——锁定版本、WinUI 3 API 细节、构建坑、IPC 契约。
> **本文件不是什么**：不是 GUI 宿主选型的理由（见 [ADR-0004](../../docs/adr/0004-GUI宿主定案.md)）、不是启动流程与进程模型（见 [docs/CODE_WIKI.md §4 运行流程](../../docs/CODE_WIKI.md)）、不是前端与图标规范（见 [frontend/README.md](./frontend/README.md)）。
> **信源等级**：L2 —— 可作「壳工程怎么构建、IPC 契约是什么」的直接依据；不得作为宿主选型理由（L1，见 ADR-0004）与业务实现（见 [core/CODE_WIKI.md](../../maaracing_master/core/CODE_WIKI.md)）的依据。
> **继承**：通用协作与信源规则见 [`AGENTS.md`](../../AGENTS.md)。

## 职责边界

`MaaRacingMaster.Shell.exe` 只做三件事：**窗口、启停 Python sidecar、转发消息**。Controller 业务一律不进 shell——Python 侧是唯一业务后端。

| 文件 | 职责 |
|---|---|
| `Program.cs` | 入口（`DISABLE_XAML_GENERATED_MAIN`）+ 单实例互斥 |
| `App.xaml(.cs)` | 应用生命周期、UAC 提权环境变量注入 |
| `MainWindow.xaml(.cs)` | 主窗口、WebView2 加载、`AppWindowTitleBar` drag rects |
| `PeepWindow.xaml(.cs)` | PEEP 悬浮窗（工具窗 + 置顶 + 无系统边框，标题栏 HTML 自绘） |
| `RpcBridge.cs` | HTML ↔ C# ↔ sidecar 的 RPC 转发（主窗口与 PEEP 窗**共用**：回发按 `sender.CoreWebView2` 定位，不依赖窗口实例字段，故各窗互不串台） |
| `PythonSidecar.cs` | JSONL transport（见下） |
| `app.manifest` | UAC 提权声明（一次提权，child 继承） |
| `NuGet.Config` | 项目级包源（见「NuGet 网络坑」） |

PEEP 悬浮窗的消费互斥：本窗口存在期间主界面预览卡退化为占位符并停止拉帧，帧消费唯一由该窗口承担（共用实现 `frontend/peep-consumer.js`）；不记忆位置/尺寸，随主窗口退出连带销毁。

## 锁定版本与依赖

| 项 | 取值 | 说明 |
|---|---|---|
| 目标框架 | `net8.0-windows10.0.19041.0` | .NET SDK 8.0.123 |
| Windows App SDK | **1.8.260710003** | NuGet meta 包，依赖拆成 9 个子包，restore 自动拉 |
| WebView2 | 随 WindowsAppSDK 提供 | WinUI 3 用 `Microsoft.UI.Xaml.Controls.WebView2`，无需单独引 WebView2 SDK |
| 部署形态 | `WindowsPackageType=None` + `WindowsAppSDKSelfContained=true` | 未打包应用，免装 Windows App Runtime |

## NuGet 网络坑

| 坑点 | 说明 | 解法 |
|---|---|---|
| nuget.org 可能被网络阻断 | dotnet 报 SSL EOF，而 curl 能 302 到 CDN | 换 **Azure CN 镜像** `https://nuget.azure.cn/v3/index.json`（restore 可用） |
| `dotnet restore --source <源名>` 被当路径 | 源名被解析为相对目录 | 用**项目级** `NuGet.Config`（`<clear/>` + 指定源），restore 不带 `--source` |
| 需要本地包源 | — | `dotnet nuget add source <dir>`（或手动下 nupkg 到目录） |

## WinUI 3 关键 API（AppWindowTitleBar）

| API | 用途 | 注意 |
|---|---|---|
| `AppWindow.TitleBar.ExtendsContentIntoTitleBar = true` | 内容延伸到标题栏，保留系统 caption buttons | 关闭系统标题栏视觉，按钮仍在右上 overlay |
| `AppWindow.TitleBar.SetDragRectangles(RectInt32[])` | HTML 拖拽区 | **物理像素**；窗口尺寸变化（`AppWindow.Changed` + `args.DidSizeChange`）需重设 |
| `AppWindow.TitleBar.PreferredHeightOption` | 系统按钮高度 | `Standard` / `Tall` |
| `Window.AppWindow` | 获取 AppWindow | Windows App SDK 1.4+ |

- **drag region 交互区挖孔**：顶部整条设为 drag rect 时，双击 tab / 品牌按钮区会触发最大化（按钮被「标题栏」行为吃掉）。方案：前端 `reportDragExcludes()` 测量 `.brand` + `.tabs` 合并矩形 → `postMessage({type:'drag-exclude', rect})` → C# 存 DIP 矩形，`UpdateDragRects()` 按 DPI 换算挖孔（左段 + 右段 + 按钮下方段三段）。坐标基准 = 窗口左上角（HTML 延伸进标题栏后 DOM (0,0) 即窗口左上角）。
- **单实例互斥**：`AcquireSingleInstance()` 用命名 Mutex（`Global\MaaRM_SingleInstance`，权限异常降级会话级）检测已有实例 → `MessageBoxW` 询问「启动新进程（`taskkill /T` 连 sidecar 杀旧进程）或取消保留旧进程」；旧进程被强杀后接管 Mutex 需捕获 `AbandonedMutexException`。
- 系统按钮颜色跟随系统主题（native 正常表现，非 bug）。
- Snap Layout hover 在 AppWindowTitleBar 下不出现（疑似系统 SnapAssist 设置，未深究）。

## 构建坑

| 坑点 | 解决 |
|---|---|
| 手写 `Program.cs` 与 XAML 自动生成 Main 冲突（CS0101） | csproj 加 `DefineConstants=$(DefineConstants);DISABLE_XAML_GENERATED_MAIN` |
| `Application.Start` 回调参数用 `_` 与丢弃赋值冲突（CS0029） | 参数命名 `p` |
| 未打包应用入口样板 | `WinRT.ComWrappersSupport.InitializeComWrappers()` + `DispatcherQueueSynchronizationContext` |

## sidecar transport 契约（`PythonSidecar.cs`）

| 项 | 实现 |
|---|---|
| 唯一 stdout reader | 一个常驻 `ReaderLoopAsync`，按 `response.id` 匹配 `ConcurrentDictionary<ulong, TCS>` |
| stdin 串行 | `SemaphoreSlim` 写锁 |
| 超时 | `Task.WaitAsync(timeout)`，超时清理 pending，只影响单请求 |
| backend 断开 | reader EOF → 所有 pending 立即抛 `BackendDisconnectedException` |
| malformed stdout | 忽略并记 stderr，不 crash 整个 IPC |
| stderr drain | 独立 task 持续读，防 OS pipe 填满卡死 Python |
| shutdown | `ShutdownAsync(grace)`：发 shutdown → 等进程自退 → 超时 `Kill(entireProcessTree:true)`；返回退出码，不 Dispose |
| 防孤儿 | `Dispose()` 对存活进程 KillTree |

### 契约坑点（必记）

| 坑点 | 说明 |
|---|---|
| `JsonDocument` 生命周期 | reader 中 `using var doc` 循环末释放，`SetResult` 必须传 `root.Clone()`（深拷贝），否则调用方访问即 `ObjectDisposedException` |
| Python worker 线程退出 | `sys.exit()` 在非主线程只抛 `SystemExit` 不退出进程，必须 `os._exit(n)` |
| Dispose 后访问 Process | `_process.Dispose()` 后访问属性抛「No process is associated」；验证进程存活用 `ProcessId` + `Process.GetProcessById(pid)` 捕获 `ArgumentException` |
| Python 侧 handler 线程 | 必须非 daemon——stdin EOF 后主线程退出会杀 daemon，导致 shutdown 等响应丢失；入口强制 `sys.stdout = _StdoutGuard`（一切误写转 stderr），见 [`core/sidecar.py`](../../maaracing_master/core/sidecar.py) |
