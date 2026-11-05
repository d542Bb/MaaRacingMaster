using Microsoft.UI.Xaml;

namespace MaaRacingMaster.Shell;

public partial class App : Application
{
    private Window? _window;

    /// <summary>系统通知服务：构造期注册通知身份；失败时对象仍可存在但 IsAvailable=false。</summary>
    internal static AppNotifications? Notifications { get; private set; }

    public App()
    {
        InitializeComponent();
        Notifications = new AppNotifications();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new MainWindow();
        _window.Activate();
    }
}
