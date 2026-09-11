using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.UI.Input;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.Web.WebView2.Core;
using Windows.Graphics;
using WinRT.Interop;

namespace mra_shell;

/// <summary>
/// MRA 正式 shell —— 唯一 GUI。职责边界：窗口 + sidecar 生命周期 + 消息转发。
/// 业务（Controller）完全在 Python sidecar，本类不承载任何业务逻辑。
/// </summary>
public sealed partial class MainWindow : Window
{
    // 最小窗口尺寸（DIP，逻辑像素）
    private const int MinWindowWidth = 1000;
    private const int MinWindowHeight = 700;

    /// <summary>
    /// 定位仓库根 / AppRoot（含 pyproject.toml 的目录，发布版为安装根）。
    /// 优先级（见 .trae/documents 报告 §5.3）：--app-root 参数 > MRA_APP_ROOT 环境变量 > exe 向上探测 pyproject.toml。
    /// 第一版 PoC：native Launcher 经环境变量 MRA_APP_ROOT 传入安装根（避免命令行 quoting），
    /// 此处用 Path.GetFullPath 归一化为绝对路径（不变量：启动期只解析一次，此后不因 cwd/exe 位置再推导）。
    /// 找不到返回 null。
    /// </summary>
    private static string? ResolveRepoRoot()
    {
        // --app-root 参数（未来 CLI 用，当前 Launcher 未走此路径）
        var argRoot = Environment.GetCommandLineArgs()
            .SkipWhile(a => a != "--app-root").Skip(1).FirstOrDefault();
        if (argRoot is not null && argRoot.Length > 0)
        {
            try { return Path.GetFullPath(argRoot); }
            catch { /* 非法路径，继续降级 */ }
        }

        // MRA_APP_ROOT 环境变量（第一版 PoC transport）
        var envRoot = Environment.GetEnvironmentVariable("MRA_APP_ROOT");
        if (!string.IsNullOrWhiteSpace(envRoot))
        {
            try { return Path.GetFullPath(envRoot); }
            catch { /* 非法路径，继续降级 */ }
        }

        // 开发模式回退：从 exe 运行目录逐级向上找含 pyproject.toml 的目录（repo marker）
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "pyproject.toml")))
                return dir.FullName;
            dir = dir.Parent;
        }
        return null;
    }

    /// <summary>
    /// 从仓库根向上查找指定相对路径的资源文件（sidecar / venv 等一律以仓库根为锚）。
    /// 找不到返回 null，调用方自行决定是否降级。
    /// </summary>
    private static string? ResolveRepoAssetPath(params string[] relativeSegments)
    {
        var root = ResolveRepoRoot();
        if (root is null)
            return null;
        var candidate = Path.Combine(new[] { root }.Concat(relativeSegments).ToArray());
        return File.Exists(candidate) ? candidate : null;
    }

    private readonly PythonSidecar? _sidecar;

    // WM_GETMINMAXINFO 拦截（Win32 subclass，保证最小尺寸在系统级生效）
    private const int WM_GETMINMAXINFO = 0x0024;
    private const int GWLP_WNDPROC = -4;
    private readonly WndProcDelegate _wndProcHook;
    private readonly IntPtr _oldWndProc;

    private AppWindow? _appWindow;
    private InputNonClientPointerSource? _inputNonClientPointerSource;

    // 前端上报的标题栏交互区（DIP，各元素矩形）：精确注册为 Passthrough（输入穿透交给 HTML），
    // Draggable = 整条标题栏带（Passthrough 优先于 drag rects，重叠无冲突，见 UpdateDragRects）
    private readonly List<RectInt32> _interactionRectsDips = new();

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    public MainWindow()
    {
        InitializeComponent();
        Title = "MRA";

        // 窗口图标（任务栏/Alt-Tab 显示）：AppWindow.SetIcon 只接受 .ico 路径
        // 从 exe 运行目录向上查找仓库根 assets/icon.ico（避免硬编码本机绝对路径）
        var iconPath = ResolveRepoAssetPath("assets", "icon.ico");
        if (iconPath is not null)
            AppWindow.SetIcon(iconPath);

        // 标题栏：保留系统边框（可缩放），去掉系统标题栏与 —□× 按钮，由 HTML 自绘（win-controls）
        if (AppWindow.Presenter is OverlappedPresenter presenter)
            presenter.SetBorderAndTitleBar(true, false);

        _appWindow = AppWindow;
        _appWindow.Changed += AppWindow_Changed;

        // 非客户区指针源：Draggable 空白区系统处理拖动/双击最大化，Passthrough 交互区穿透给 HTML
        _inputNonClientPointerSource = InputNonClientPointerSource.GetForWindowId(AppWindow.Id);
        UpdateDragRects();

        // 最小尺寸：subclass 窗口过程拦截 WM_GETMINMAXINFO（系统级，拖拽/Resize 均被钳制）
        var hwnd = WindowNative.GetWindowHandle(this);
        _wndProcHook = WndProc;
        _oldWndProc = SetWindowLongPtr(hwnd, GWLP_WNDPROC, Marshal.GetFunctionPointerForDelegate(_wndProcHook));

        // 启动尺寸：以最小安全尺寸启动（= MinWindowWidth/Height，物理像素按 DPI 换算，
        // 与 WndProc 的 MINMAXINFO 同一套换算；避免默认尺寸偏大导致低分辨率屏溢出）
        var startupScale = GetDpiForWindow(hwnd) / 96.0;
        AppWindow.Resize(new SizeInt32(
            (int)(MinWindowWidth * startupScale),
            (int)(MinWindowHeight * startupScale)));

        // 仓库根（含 pyproject.toml 的目录）作为所有相对路径的锚：venv、前端 HTML 一律以其为基准。
        // 从 exe 基目录向上推导，彻底摆脱硬编码的本机绝对路径，仓库可 clone 到任意位置运行。
        var projectRoot = ResolveRepoRoot();
        if (projectRoot is null)
        {
            Console.Error.WriteLine("[shell] 未找到仓库根（pyproject.toml 向上搜索失败），后端与页面不可用");
        }

        // 1. 启动 Python sidecar（唯一业务后端；失败不阻塞窗口，前端显示 backend unavailable）
        if (projectRoot is not null)
        {
            // 发布模式优先用随包的自带 runtime（runtime\python\python.exe），
            // 否则回退开发模式的 .venv\Scripts\python.exe，保证两种环境都能跑。
            var releasePython = Path.Combine(projectRoot, "runtime", "python", "python.exe");
            var pythonExe = File.Exists(releasePython)
                ? releasePython
                : Path.Combine(projectRoot, ".venv", "Scripts", "python.exe");
            try
            {
                _sidecar = new PythonSidecar(pythonExe, "-u -m maaracing_master.core.sidecar", projectRoot);
                // 运行结束自动退出：sidecar 推 auto_exit → shell 关闭主窗口优雅退出
                _sidecar.SidecarEvent += OnSidecarEvent;
            }
            catch (Exception ex)
            {
                _sidecar = null;
                Console.Error.WriteLine($"[shell] sidecar 启动失败: {ex.Message}");
            }
        }

        web.WebMessageReceived += OnWebMessageReceived;
        Closed += OnClosed;
        // 自定义 GUI：禁用右键默认上下文菜单（放大预览 / 任意区域右键都不再弹菜单）
        web.CoreWebView2Initialized += (_, _) =>
        {
            if (web.CoreWebView2 is not null)
                web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
        };
        // 滚轮修复：WM_MOUSEWHEEL 投递给键盘焦点窗口，WebView2 无焦点时收不到滚轮
        // （表现为滚轮滚不动、中键点击后才恢复）。页面加载完成后主动聚焦。
        // 同时主动上报一次初始最大化状态（自绘最大化/还原按钮图标需要对齐）
        web.NavigationCompleted += (_, _) =>
        {
            web.Focus(FocusState.Programmatic);
            SendMaximizedState();
        };
        var indexPath = ResolveRepoAssetPath("apps", "mra_shell", "frontend", "index.html");
        if (indexPath is not null)
        {
            // 绝对 windows 路径按文件 URI 解析（auto file:/// scheme），WebView 本地加载
            web.Source = new Uri(indexPath);
        }
    }

    /// <summary>窗口过程钩子：钳制最小尺寸。MINMAXINFO 单位 = 物理像素，需按 DPI 换算。</summary>
    private IntPtr WndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == WM_GETMINMAXINFO)
        {
            var mmi = Marshal.PtrToStructure<MINMAXINFO>(lParam);
            var scale = GetDpiForWindow(hWnd) / 96.0;
            mmi.ptMinTrackSize.X = (int)(MinWindowWidth * scale);
            mmi.ptMinTrackSize.Y = (int)(MinWindowHeight * scale);
            Marshal.StructureToPtr(mmi, lParam, false);
            return IntPtr.Zero;
        }
        return CallWindowProc(_oldWndProc, hWnd, msg, wParam, lParam);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MINMAXINFO
    {
        public POINT ptReserved;
        public POINT ptMaxSize;
        public POINT ptMaxPosition;
        public POINT ptMinTrackSize;
        public POINT ptMaxTrackSize;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowLongPtr(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr CallWindowProc(IntPtr lpPrevWndFunc, IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    /// <summary>HTML 标题栏（顶部 52px）双区域注册：Draggable = 整条标题栏带（系统处理拖动 +
    /// 双击最大化）；Passthrough = 各交互区精确矩形（brand/tabs/win-controls，输入穿透交给 HTML）。
    /// Passthrough 优先于 drag rects，故交互区之间的空白（如 tabs 右侧）保持可拖拽。
    /// 坐标单位物理像素（系统级 hit-test），与 WndProc 的 DPI 换算一致。</summary>
    private void UpdateDragRects()
    {
        var scale = GetDpiForWindow(WindowNative.GetWindowHandle(this)) / 96.0;
        int width = AppWindow.Size.Width;
        int top = (int)(52 * scale);
        AppWindow.TitleBar.SetDragRectangles(new[] { new RectInt32(0, 0, width, top) });
        var passthrough = _interactionRectsDips
            .Select(d => new RectInt32(
                (int)(d.X * scale),
                (int)(d.Y * scale),
                (int)(d.Width * scale),
                (int)(d.Height * scale)))
            .ToArray();
        _inputNonClientPointerSource?.SetRegionRects(NonClientRegionKind.Passthrough, passthrough);
    }

    // ---------- HTML → C# → Python 转发 ----------

    private void OnWebMessageReceived(WebView2 sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        try
        {
            using var doc = JsonDocument.Parse(args.WebMessageAsJson);
            var root = doc.RootElement;
            var msgType = root.TryGetProperty("type", out var t) ? t.GetString() : "";
            if (msgType == "drag-exclude")
            {
                // 前端上报标题栏交互区（DIP，各元素矩形）→ 重算非客户区双区域（不回复）
                _interactionRectsDips.Clear();
                if (root.TryGetProperty("rects", out var arr) && arr.ValueKind == JsonValueKind.Array)
                {
                    foreach (var r in arr.EnumerateArray())
                    {
                        if (r.ValueKind != JsonValueKind.Object) continue;
                        _interactionRectsDips.Add(new RectInt32(
                            (int)r.GetProperty("x").GetDouble(),
                            (int)r.GetProperty("y").GetDouble(),
                            (int)r.GetProperty("w").GetDouble(),
                            (int)r.GetProperty("h").GetDouble()));
                    }
                }
                UpdateDragRects();
            }
            else if (msgType == "win-action")
            {
                // 自绘标题栏按钮 → 窗口控制
                var action = root.GetProperty("action").GetString();
                switch (action)
                {
                    case "minimize": MinimizeWindow(); break;
                    case "maximize": ToggleMaximizeWindow(); break;
                    case "close": CloseWindow(); break;
                }
            }
            else if (msgType == "call")
            {
                var callId = root.GetProperty("callId").GetInt64();
                var method = root.GetProperty("method").GetString() ?? "";
                var paramsEl = root.TryGetProperty("params", out var p) && p.ValueKind != JsonValueKind.Null
                    ? p
                    : (JsonElement?)null;
                _ = HandleCallAsync(sender, callId, method, paramsEl);
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[shell] webmessage 解析失败: {ex.Message}");
        }
    }

    // ---------- 自绘标题栏：窗口控制 + 最大化状态上报 ----------

    private void MinimizeWindow()
    {
        if (AppWindow.Presenter is OverlappedPresenter p) p.Minimize();
    }

    private void ToggleMaximizeWindow()
    {
        if (AppWindow.Presenter is not OverlappedPresenter p) return;
        if (p.State == OverlappedPresenterState.Maximized) p.Restore();
        else p.Maximize();
    }

    private void CloseWindow() => Close();

    // sidecar 主动事件（type=event）。当前仅支持"运行结束自动退出"。
    private void OnSidecarEvent(string evt)
    {
        if (evt != "auto_exit") return;
        // 事件来自侧car reader 线程：封回 UI 线程再关主窗口，走 OnClosed 优雅关 sidecar
        DispatcherQueue.TryEnqueue(() => Close());
    }

    // Presenter 变化（最大化/还原）→ 通知前端切换最大化/还原图标；
    // 尺寸变化（拖拽缩放）→ 重算非客户区 Draggable/Passthrough 区域。
    // 坑：Restore 后常只触发 DidSizeChange 而不触发 DidPresenterChange，
    // 故两者任一变化都检查并上报最大化状态（前端按值幂等设置，重复消息无副作用）
    private void AppWindow_Changed(AppWindow sender, AppWindowChangedEventArgs args)
    {
        if (args.DidSizeChange)
            UpdateDragRects();
        if (!args.DidSizeChange && !args.DidPresenterChange) return;
        if (sender.Presenter is not OverlappedPresenter presenter) return;
        PostMaximizedState(presenter.State == OverlappedPresenterState.Maximized);
    }

    private void SendMaximizedState()
    {
        if (AppWindow.Presenter is not OverlappedPresenter presenter) return;
        PostMaximizedState(presenter.State == OverlappedPresenterState.Maximized);
    }

    private void PostMaximizedState(bool value)
    {
        try
        {
            var json = JsonSerializer.Serialize(new { type = "maximized", value });
            web.CoreWebView2?.PostWebMessageAsJson(json);
        }
        catch (Exception ex)
        {
            // 窗口关闭边缘 Presenter 变化时 WebView 可能已释放，忽略即可
            Console.Error.WriteLine($"[shell] 上报最大化状态失败: {ex.Message}");
        }
    }

    private async Task HandleCallAsync(WebView2 sender, long callId, string method, JsonElement? paramsEl)
    {
        object? data = null;
        string? error = null;
        bool ok = true;
        try
        {
            if (_sidecar is null)
                throw new InvalidOperationException("backend unavailable");
            var resp = await _sidecar.CallAsync(method, paramsEl, TimeSpan.FromSeconds(10));
            data = resp.GetProperty("data"); // JsonElement：null 或对象直接嵌入回传
        }
        catch (Exception ex)
        {
            ok = false;
            error = ex.Message;
        }

        try
        {
            var reply = JsonSerializer.Serialize(new { type = "response", callId, ok, data, error });
            sender.CoreWebView2.PostWebMessageAsJson(reply); // 同步 API
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[shell] 回传 JS 失败: {ex.Message}");
        }
    }

    // ---------- 关闭钩子：grace period shutdown ----------

    private void OnClosed(object sender, WindowEventArgs args)
    {
        if (_sidecar is not null)
        {
            try
            {
                // UI 线程同步 await 带 UI SynchronizationContext 的 async 会死锁
                // （OnClosed 在 UI 线程，ShutdownAsync 内部 await 需要回 UI 上下文续体）。
                // Task.Run 把整个调用挪到线程池（无 SynchronizationContext），
                // 同步等待只阻塞本线程，不会死锁；最坏等待 grace 3s。
                Task.Run(() => _sidecar.ShutdownAsync(TimeSpan.FromSeconds(3)))
                    .GetAwaiter().GetResult();
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[shell] sidecar 关闭异常: {ex.Message}");
            }
            _sidecar.Dispose();
        }
    }
}
