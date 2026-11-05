using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Xml.Linq;
using Microsoft.Win32;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 系统通知（Windows 老版 Toast 通道，与 MAA 同类做法）：
/// 只做显示。身份 = HKCU 的 AppUserModelId 注册 + 进程级 AUMID；
/// 不建开始菜单快捷方式、不注册激活器，通知标为 background 激活——点击没有可启动目标，
/// 不会拉起主窗口，也不会触发 UAC。
///
/// 为什么不用 Windows App SDK 的通知接口：本项目壳以管理员权限运行，
/// 该接口在提权进程下被官方禁用（注册/显示静默跳过）。老版 Toast 没有这道闸门。
/// </summary>
internal sealed class AppNotifications
{
    private const string Aumid = "MaaRacingMaster.Shell";
    private const string DisplayName = "MaaRacingMaster";

    private readonly bool _available;

    public bool IsAvailable => _available;

    public AppNotifications()
    {
        try
        {
            SetProcessAumid();
            RegisterAumid();
            _available = true;
        }
        catch (Exception ex)
        {
            _available = false;
            Console.Error.WriteLine($"[shell] 系统通知初始化失败，通知能力降级: {ex.Message}");
        }
    }

    /// <summary>显示两行通知：首行标题（系统加粗），次行正文。失败只记诊断，不抛。</summary>
    public void Show(string title, string body)
    {
        if (!_available) return;
        try
        {
            // 不写 launch / 不注册激活器 / 不建快捷方式；activationType=background
            // 且没有后台任务：点击通知只消失，没有可启动目标。
            var xml = new XElement(
                "toast",
                new XAttribute("activationType", "background"),
                new XElement(
                    "visual",
                    new XElement(
                        "binding",
                        new XAttribute("template", "ToastGeneric"),
                        new XElement("text", title ?? string.Empty),
                        new XElement("text", body ?? string.Empty))),
                new XElement("audio", new XAttribute("src", "ms-winsoundevent:Notification.Default")));

            var doc = new XmlDocument();
            doc.LoadXml(xml.ToString(SaveOptions.DisableFormatting));
            ToastNotificationManager.CreateToastNotifier(Aumid).Show(new ToastNotification(doc));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[shell] 系统通知显示失败: {ex.Message}");
        }
    }

    /// <summary>通知身份（显示名/图标）登记到当前用户；不写激活器、不建快捷方式。</summary>
    private static void RegisterAumid()
    {
        using var key = Registry.CurrentUser.CreateSubKey(@"Software\Classes\AppUserModelId\" + Aumid);
        if (key is null) return;
        key.SetValue("DisplayName", DisplayName);
        var iconPath = ResolveIconPath();
        if (iconPath is not null)
            key.SetValue("IconUri", iconPath);
    }

    /// <summary>发布版 assets 在安装根、开发版在仓库根：从 exe 目录向上找同一张图。</summary>
    private static string? ResolveIconPath()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, "assets", "maarm_icon.png");
            if (File.Exists(candidate)) return candidate;
            dir = dir.Parent;
        }
        return null;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    private static void SetProcessAumid()
    {
        try { SetCurrentProcessExplicitAppUserModelID(Aumid); }
        catch { /* 设置失败不阻断启动；通知显示会自行暴露问题 */ }
    }
}
