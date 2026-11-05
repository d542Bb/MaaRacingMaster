using System;
using System.Runtime.InteropServices;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 任务栏进度条（ITaskbarList3）：运行期走不确定进度，结束/关窗即熄灭。
/// 失败只记诊断，不抛——任务栏能力可整体降级，不得影响主流程与状态迁移。
/// </summary>
internal sealed class TaskbarProgress : IDisposable
{
    private const int TBPF_NOPROGRESS = 0x0;
    private const int TBPF_INDETERMINATE = 0x2;

    private readonly IntPtr _hwnd;
    private ITaskbarList3? _taskbar;

    public TaskbarProgress(IntPtr hwnd)
    {
        _hwnd = hwnd;
        try
        {
            _taskbar = (ITaskbarList3)new TaskbarList();
            Marshal.ThrowExceptionForHR(_taskbar.HrInit());
        }
        catch (Exception ex)
        {
            if (_taskbar is not null)
            {
                try { Marshal.ReleaseComObject(_taskbar); } catch { }
                _taskbar = null;
            }
            Console.Error.WriteLine($"[shell] 任务栏进度初始化失败，本能力降级: {ex.Message}");
        }
    }

    /// <summary>亮：不确定进度（不设置百分比，没有有意义的数值）。</summary>
    public void SetRunning() => SetState(TBPF_INDETERMINATE);

    /// <summary>灭：无进度。</summary>
    public void Clear() => SetState(TBPF_NOPROGRESS);

    private void SetState(int state)
    {
        if (_taskbar is null) return;
        try
        {
            Marshal.ThrowExceptionForHR(_taskbar.SetProgressState(_hwnd, state));
        }
        catch (Exception ex)
        {
            // 运行中单次失败只记诊断：状态迁移只以事件为准，不以接口调用结果为准
            Console.Error.WriteLine($"[shell] 任务栏进度设置失败: {ex.Message}");
        }
    }

    public void Dispose()
    {
        if (_taskbar is null) return;
        try { Marshal.ReleaseComObject(_taskbar); } catch { }
        _taskbar = null;
    }

    [ComImport]
    [Guid("56FDF344-FD6D-11d0-958A-006097C9A090")]
    [ClassInterface(ClassInterfaceType.None)]
    private class TaskbarList
    {
    }

    /// <summary>
    /// ITaskbarList3 的 vtable 顺序必须完整覆盖 ITaskbarList / ITaskbarList2 的全部方法，
    /// 漏一个就会错位到别的虚函数上（照样能编译，运行期才暴露）。
    /// </summary>
    [ComImport]
    [Guid("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface ITaskbarList3
    {
        // ITaskbarList
        [PreserveSig] int HrInit();
        [PreserveSig] int AddTab(IntPtr hwnd);
        [PreserveSig] int DeleteTab(IntPtr hwnd);
        [PreserveSig] int ActivateTab(IntPtr hwnd);
        [PreserveSig] int SetActiveAlt(IntPtr hwnd);

        // ITaskbarList2
        [PreserveSig] int MarkFullscreenWindow(IntPtr hwnd, [MarshalAs(UnmanagedType.Bool)] bool fFullscreen);

        // ITaskbarList3
        [PreserveSig] int SetProgressValue(IntPtr hwnd, ulong ullCompleted, ulong ullTotal);
        [PreserveSig] int SetProgressState(IntPtr hwnd, int tbpFlags);
    }
}
