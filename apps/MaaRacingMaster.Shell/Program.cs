using System;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;

namespace MaaRacingMaster.Shell;

public static class Program
{
    // 单实例互斥体名：应用 requireAdministrator，用 Global 前缀跨提升级别可见；
    // 权限异常时降级为会话级命名空间（见 AcquireSingleInstance）。
    private const string SingleInstanceMutexName = "Global\\MaaRM_SingleInstance";

    // MessageBox 常量（user32）
    private const uint MB_YESNO = 0x00000004;
    private const uint MB_ICONQUESTION = 0x00000020;
    private const int IDYES = 6;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    [STAThread]
    private static void Main(string[] args)
    {
        // 单实例检查：已有实例运行 → 弹窗询问用户如何处理（见 AcquireSingleInstance）
        if (!AcquireSingleInstance())
            return; // 用户选择保留旧进程，本进程退出

        WinRT.ComWrappersSupport.InitializeComWrappers();
        Application.Start(p =>
        {
            var context = new DispatcherQueueSynchronizationContext(
                DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            new App();
        });
    }

    // 持有互斥体引用直到进程退出（Application.Start 阻塞，Main 返回时进程才结束）
    private static Mutex? s_instanceMutex;

    /// <summary>
    /// 单实例互斥：持有命名 Mutex 直到进程退出。
    /// 检测到已有实例 → MessageBox 询问：
    ///   「是」= 启动新进程并关闭旧进程；「否」= 取消启动，保留旧进程。
    /// 返回 false 表示本进程应直接退出。
    /// </summary>
    private static bool AcquireSingleInstance()
    {
        Mutex? mutex = null;
        try
        {
            mutex = new Mutex(true, SingleInstanceMutexName, out var createdNew);
            if (createdNew)
            {
                s_instanceMutex = mutex;
                return true;
            }
        }
        catch (Exception)
        {
            // Global 前缀权限不足（异常场景）：降级为会话级命名空间
            try
            {
                mutex = new Mutex(true, "MaaRM_SingleInstance", out var createdNew);
                if (createdNew)
                {
                    s_instanceMutex = mutex;
                    return true;
                }
            }
            catch (Exception)
            {
                // 极端情况互斥体创建失败：放行单开（避免把用户锁死在外）
                s_instanceMutex = mutex;
                return true;
            }
        }

        // 已有实例在运行 → 询问用户
        var choice = MessageBoxW(
            IntPtr.Zero,
            "检测到 MRA 已在运行。\n\n" +
            "是(Y)：启动新进程（将自动关闭当前运行的旧进程）\n" +
            "否(N)：取消启动，保留当前运行的进程",
            "MRA — 是否启动新进程？",
            MB_YESNO | MB_ICONQUESTION);
        if (choice != IDYES)
            return false; // 用户选择保留旧进程 → 本进程退出

        // 用户选择替换：关闭旧进程（连带子进程，如 sidecar），等其退出后接管互斥体
        KillOtherInstances();
        try
        {
            mutex.WaitOne();
        }
        catch (AbandonedMutexException)
        {
            // 旧进程被强杀 → 互斥体 abandoned，属正常接管
        }
        s_instanceMutex = mutex;
        return true;
    }

    /// <summary>结束除本进程外的所有 MaaRacingMaster.Shell 实例（taskkill /T 连带子进程）。</summary>
    private static void KillOtherInstances()
    {
        var me = Process.GetCurrentProcess().Id;
        foreach (var p in Process.GetProcessesByName("MaaRacingMaster.Shell")
                     .Where(p => p.Id != me).ToList())
        {
            try
            {
                using var k = Process.Start(new ProcessStartInfo
                {
                    FileName = "taskkill.exe",
                    Arguments = $"/PID {p.Id} /T /F",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                });
                k?.WaitForExit(3000);
            }
            catch { /* 尽力而为，失败继续下一个 */ }
            try { p.WaitForExit(3000); } catch { }
        }
    }
}
