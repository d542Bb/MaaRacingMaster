using System;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.Web.WebView2.Core;
using Windows.Graphics;

namespace MaaRacingMaster.Shell;

/// <summary>
/// PEEP 悬浮窗：脱离主 GUI 的置顶播放器形态窗口。
///
/// 形态：工具窗（不占 Alt-Tab 与任务栏）+ 置顶 + 无系统边框，标题栏由 HTML 自绘（顶部 32 DIP 为拖动区）。
/// 消费者互斥：本窗口存在期间主界面预览卡退化为占位符并停止拉帧，帧消费唯一由本窗口承担
/// （共用实现见 frontend/peep-consumer.js）。
/// 防呆：不记忆任何位置/尺寸/形态，每次打开都是主屏左上角的默认尺寸；随主窗口退出连带销毁。
/// </summary>
public sealed partial class PeepWindow : Window
{
    // 内容区逻辑尺寸（DIP）：按 16:9 的 270p 反算 —— 270p 指物理像素 480×270，
    // 在 125% 缩放下对应 384×216 DIP（直接写 480 DIP 会被系统放大成 600 物理像素，观感偏大）。
    private const int ContentWidthDips = 384;
    private const int VideoHeightDips = 216;
    private const int TitleBarHeightDips = 32;
    private const int ScreenMarginDips = 16;

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    private readonly AppWindow _appWindow;

    public PeepWindow(string peepHtmlPath, AppWindow mainAppWindow)
    {
        InitializeComponent();
        Title = "MaaRM · PEEP";
        _appWindow = AppWindow;

        ConfigurePresenter();
        PlaceOnMainScreen(mainAppWindow);

        AppWindow.Changed += (_, args) =>
        {
            if (args.DidSizeChange || args.DidPresenterChange)
                UpdateDragRects();
        };

        web.WebMessageReceived += OnPeepMessageReceived;
        web.CoreWebView2Initialized += (_, _) =>
        {
            if (web.CoreWebView2 is not null)
                web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
        };
        web.NavigationCompleted += (_, _) =>
        {
            web.Focus(FocusState.Programmatic);
            UpdateDragRects(); // 页面就绪时窗口尺寸已定，再校准一次拖动区
        };
        web.Source = new Uri(peepHtmlPath);

        UpdateDragRects();
    }

    /// <summary>工具窗 + 置顶 + 无系统边框；不可缩放（悬浮窗只需拖动与关闭）。</summary>
    private void ConfigurePresenter()
    {
        if (AppWindow.Presenter is not OverlappedPresenter)
            return;
        try
        {
            var presenter = OverlappedPresenter.CreateForToolWindow();
            presenter.IsAlwaysOnTop = true;
            presenter.IsResizable = false;
            presenter.IsMaximizable = false;
            presenter.IsMinimizable = false;
            // 保留边框、只去掉系统标题栏。不能设成完全无边框（false,false）：
            // AppWindow.TitleBar 的拖动区命中依赖非客户区，无边框时 SetDragRectangles 不生效，
            // 实机表现为标题栏拖不动。
            presenter.SetBorderAndTitleBar(true, false);
            AppWindow.SetPresenter(presenter);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[peep] 窗口形态设置失败，退回默认 presenter: {ex.Message}");
        }
    }

    /// <summary>默认落点：主窗口所在显示器的左上角（多屏用户不会跑到别的屏），尺寸按该屏 DPI 换算。</summary>
    private void PlaceOnMainScreen(AppWindow mainAppWindow)
    {
        var display = DisplayArea.GetFromWindowId(mainAppWindow.Id, DisplayAreaFallback.Nearest);
        var work = display.WorkArea;
        var scale = GetDpiForWindow(WinRT.Interop.WindowNative.GetWindowHandle(this)) / 96.0;
        var width = (int)(ContentWidthDips * scale);
        var height = (int)((VideoHeightDips + TitleBarHeightDips) * scale);
        _appWindow.MoveAndResize(new RectInt32(
            work.X + (int)(ScreenMarginDips * scale),
            work.Y + (int)(ScreenMarginDips * scale),
            width,
            height));
    }

    /// <summary>HTML 自绘标题栏（顶部 32 DIP）整条注册为拖动区。单位=物理像素，与 hit-test 一致。</summary>
    private void UpdateDragRects()
    {
        try
        {
            var scale = GetDpiForWindow(WinRT.Interop.WindowNative.GetWindowHandle(this)) / 96.0;
            var size = _appWindow.Size;
            var top = Math.Min((int)(TitleBarHeightDips * scale), size.Height);
            _appWindow.TitleBar.SetDragRectangles(new[] { new RectInt32(0, 0, size.Width, top) });
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[peep] 设置拖动区失败: {ex.Message}");
        }
    }

    private void OnPeepMessageReceived(WebView2 sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        try
        {
            using var doc = JsonDocument.Parse(args.WebMessageAsJson);
            var root = doc.RootElement;

            // RPC 转发与主窗口共用同一实现（回发按 sender 定位）
            if (RpcBridge.TryHandleCall(sender, root))
                return;

            var msgType = root.TryGetProperty("type", out var t) ? t.GetString() : "";
            if (msgType == "win-action"
                && root.TryGetProperty("action", out var action)
                && action.GetString() == "close")
            {
                Close(); // 悬浮窗的「关闭（还原）」：关掉本窗，主界面卡片由 MainWindow 广播恢复
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[peep] webmessage 解析失败: {ex.Message}");
        }
    }
}
