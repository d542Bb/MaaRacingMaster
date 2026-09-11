// MaaRacingAssistant native Launcher (PoC)
// ---------------------------------------------------------------
// 职责范围（第一版最小 PoC，见 docs/.trae 报告 §5.5/§5.6）：
//   1. 定位并归一化 AppRoot = Launcher 自身所在目录
//   2. 检查 app\MaaRacingMaster.Shell.exe 存在
//   3. 设置环境变量 MaaRM_APP_ROOT = AppRoot（transport，不经命令行 quoting）
//   4. cwd = AppRoot（兼容保护层，非数据目录）
//   5. CreateProcessW 启动 app\MaaRacingMaster.Shell.exe（不传任何用户参数）
//   6. 等待子进程退出并回传其退出码
// 不做：用户参数透传、自动更新、日志框架、崩溃告警 UI。
//
// 编译（VS2019 MSVC，Hostx64/x64）：
//   cl /nologo /O2 /MT /Fe:MaaRacingAssistant.exe launcher.c /link /SUBSYSTEM:WINDOWS user32.lib shell32.lib
// 说明：/MT 静态链接 CRT，避免运行时依赖 msvcrt；图标后续由 rc 资源嵌入。
// ---------------------------------------------------------------

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

// 失败提示：native 无控制台，用 MessageBox 汇报，避免静默失败
static void FatalBox(const wchar_t *msg)
{
    MessageBoxW(NULL, msg, L"MaaRacingAssistant — 启动失败",
                MB_OK | MB_ICONERROR);
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                    LPWSTR lpCmdLine, int nCmdShow)
{
    (void)hInstance; (void)hPrevInstance; (void)lpCmdLine; (void)nCmdShow;

    wchar_t exePath[32768];
    DWORD n = GetModuleFileNameW(NULL, exePath, (DWORD)(sizeof(exePath) / sizeof(exePath[0])));
    if (n == 0 || n >= (DWORD)(sizeof(exePath) / sizeof(exePath[0])))
    {
        FatalBox(L"无法获取 Launcher 自身路径。");
        return 1;
    }

    // 归一化 AppRoot：截掉最后的文件名（保留尾部路径分隔符语义交给 MaaRacingMaster.Shell 处理）
    wchar_t *slash = NULL;
    for (wchar_t *p = exePath; *p; ++p)
        if (*p == L'\\' || *p == L'/')
            slash = p;
    if (slash == NULL)
    {
        FatalBox(L"无法解析 AppRoot（Launcher 路径非法）。");
        return 1;
    }
    *slash = L'\0';                // exePath 现在 = AppRoot（绝对路径）
    const wchar_t *appRoot = exePath;

    // 拼接子进程路径：<AppRoot>\app\MaaRacingMaster.Shell.exe
    wchar_t childExe[32768];
    if (swprintf(childExe, (size_t)(sizeof(childExe) / sizeof(childExe[0])),
                 L"%s\\app\\MaaRacingMaster.Shell.exe", appRoot) < 0)
    {
        FatalBox(L"路径过长，无法构建子进程路径。");
        return 1;
    }

    if (GetFileAttributesW(childExe) == INVALID_FILE_ATTRIBUTES)
    {
        wchar_t msg[34000];
        swprintf(msg, (size_t)(sizeof(msg) / sizeof(msg[0])),
                 L"未找到核心程序：\n%ls\n\n"
                 L"请确认 MaaRacingAssistant 安装完整（app\\MaaRacingMaster.Shell.exe 缺失）。",
                 childExe);
        FatalBox(msg);
        return 1;
    }

    // 通过环境变量传 AppRoot（不经 command-line quoting，中文/空格/尾部反斜杠均安全）
    if (!SetEnvironmentVariableW(L"MaaRM_APP_ROOT", appRoot))
    {
        FatalBox(L"无法设置环境变量 MaaRM_APP_ROOT。");
        return 1;
    }

    // 显式设置 cwd = AppRoot（兼容保护层；长期走 UserDataRoot 三段式，见报告 §5.3）
    SetCurrentDirectoryW(appRoot);

    // CreateProcessW：command line 只含子进程 exe（第一版不透传用户参数）
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    SecureZeroMemory(&si, sizeof(si));
    SecureZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);

    BOOL ok = CreateProcessW(
        childExe,               // 应用名（绝对路径，含空格也无需额外 quoting）
        NULL,                   // 无命令行参数
        NULL, NULL,             // 默认安全属性，不继承句柄
        FALSE,                  // 不继承句柄
        0,                      // 无创建标志
        NULL,                   // 继承父进程环境（已含 MaaRM_APP_ROOT）
        appRoot,                // 工作目录（cwd = AppRoot）
        &si, &pi);

    if (!ok)
    {
        wchar_t msg[34000];
        swprintf(msg, (size_t)(sizeof(msg) / sizeof(msg[0])),
                 L"无法启动核心程序：\n%ls\n\n错误码: %lu",
                 childExe, (unsigned long)GetLastError());
        FatalBox(msg);
        return 1;
    }

    CloseHandle(pi.hThread);

    // 等待子进程退出并回传其退出码（使外层 taskkill / 脚本能感知 GUI 生命周期）
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD exitCode = 1;
    if (!GetExitCodeProcess(pi.hProcess, &exitCode))
        exitCode = 1;
    CloseHandle(pi.hProcess);

    return (int)exitCode;
}