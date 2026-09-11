using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace MaaRacingMaster.Shell;

/// <summary>backend 进程非零退出 / 连接断开。</summary>
public sealed class BackendDisconnectedException : Exception
{
    public BackendDisconnectedException(int? exitCode)
        : base($"backend disconnected (exit code: {exitCode?.ToString() ?? "unknown"})")
    {
    }
}

/// <summary>RPC 层返回 ok=false。</summary>
public sealed class RpcErrorException : Exception
{
    public RpcErrorException(string message) : base(message) { }
}

/// <summary>
/// PythonSidecar —— 进程生命周期 + JSONL transport（契约测试 11/11 验证通过后复用）。
/// 唯一 stdout reader + pending map + timeout + stderr drain + grace shutdown。
/// </summary>
public sealed class PythonSidecar : IDisposable
{
    private readonly Process _process;
    private readonly StreamWriter _stdin;
    private readonly StreamReader _stdout;
    private readonly string? _workDir;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly ConcurrentDictionary<ulong, TaskCompletionSource<JsonElement>> _pending = new();
    private ulong _nextId;
    private bool _backendDown;
    private bool _disposed;

    /// <summary>sidecar 主动推送的事件名（type=event）。由 shell 侧订阅并按需响应，如运行结束自动退出（auto_exit）。</summary>
    public event Action<string>? SidecarEvent;

    /// <summary>便捷构造：脚本直跑（契约测试用）。</summary>
    public PythonSidecar(string pythonExe, string scriptPath)
        : this(pythonExe, $"-u \"{scriptPath}\"", null)
    {
    }

    /// <summary>完整构造：fileName + arguments + workingDirectory（正式 shell 用 -m 模块启动）。</summary>
    public PythonSidecar(string fileName, string arguments, string? workingDirectory)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            Arguments = arguments,
            WorkingDirectory = workingDirectory ?? string.Empty,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        _workDir = workingDirectory;
        // 强制 Python UTF-8 模式：本 shell 带 requireAdministrator，UAC 提权后
        // 父进程的 PYTHONUTF8 环境变量不继承，Python stdout 会落回 GBK/cp936，
        // 与 C# 的 UTF-8 解码不一致 → 中文乱码（\uFFFD）。
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        _process = Process.Start(psi)
            ?? throw new InvalidOperationException("failed to start sidecar process");
        _stdin = _process.StandardInput;
        _stdout = _process.StandardOutput;

        _ = Task.Run(ReaderLoopAsync); // 唯一 stdout reader
        _ = Task.Run(DrainStderrAsync); // stderr 持续 drain，防 pipe 填满
    }

    private async Task ReaderLoopAsync()
    {
        string? line;
        while ((line = await _stdout.ReadLineAsync()) is not null)
        {
            try
            {
                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;
                if (root.TryGetProperty("type", out var t) && t.GetString() == "response")
                {
                    var id = root.GetProperty("id").GetUInt64();
                    if (_pending.TryRemove(id, out var tcs))
                    {
                        bool ok = root.GetProperty("ok").GetBoolean();
                        if (ok)
                        {
                            // Clone()：JsonElement 深拷贝，脱离 JsonDocument 生命周期
                            tcs.SetResult(root.Clone());
                        }
                        else
                        {
                            var err = root.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String
                                ? e.GetString() ?? "rpc error"
                                : "rpc error";
                            tcs.SetException(new RpcErrorException(err));
                        }
                    }
                    // 未知 id 的 response：忽略（可能已超时移除）
                }
                // type=event：sidecar 主动推送事件（作为 response 分支的 else），上抛给 shell 订阅方处理
                else
                {
                    var evtName = root.TryGetProperty("event", out var ev) &&
                                  ev.ValueKind == JsonValueKind.String
                        ? ev.GetString()
                        : null;
                    if (evtName is not null)
                    {
                        try { SidecarEvent?.Invoke(evtName); }
                        catch (Exception evEx) { Console.Error.WriteLine($"[sidecar] event handler error: {evEx.Message}"); }
                    }
                }
            }
            catch (JsonException ex)
            {
                // malformed stdout：记录并继续，不 crash
                Console.Error.WriteLine($"[sidecar] malformed stdout line ignored: {ex.Message}");
            }
        }

        // EOF = Python 进程退出 → 所有 pending 立即 disconnected
        FailAllPending(new BackendDisconnectedException(_process.HasExited ? _process.ExitCode : null));
        _backendDown = true;
    }

    private async Task DrainStderrAsync()
    {
        // GUI 无控制台：sidecar stderr 落盘便于排查。固定落用户数据目录
        // （%APPDATA%/MaaRacingMaster/logs，开发/发行一致、不受日志开关控制——框架诊断日志）；
        // APPDATA 不可用时回退工作目录，保证跨机器可移植。
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        var baseDir = string.IsNullOrEmpty(appData)
            ? (_workDir ?? Environment.CurrentDirectory)
            : Path.Combine(appData, "MaaRacingMaster");
        var logPath = Path.Combine(baseDir, "logs", "sidecar_stderr.log");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
            using var fs = new StreamWriter(logPath, append: true) { AutoFlush = true };
            string? line;
            while ((line = await _process.StandardError.ReadLineAsync()) is not null)
            {
                fs.WriteLine(line);
            }
        }
        catch
        {
            // 进程退出后 reader 结束，忽略
        }
    }

    private void FailAllPending(Exception ex)
    {
        foreach (var kv in _pending)
        {
            if (_pending.TryRemove(kv.Key, out var tcs))
            {
                tcs.SetException(ex);
            }
        }
    }

    public bool HasExited => _process.HasExited;
    public int? ExitCode => _process.HasExited ? _process.ExitCode : null;
    public int ProcessId => _process.Id;

    /// <summary>发送 request 并等待对应 response，按 id 匹配；超时只影响本请求。</summary>
    public async Task<JsonElement> CallAsync(
        string method, object? paramsObj = null, TimeSpan? timeout = null, CancellationToken ct = default)
    {
        if (_backendDown || _process.HasExited)
            throw new BackendDisconnectedException(_process.HasExited ? _process.ExitCode : null);

        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;

        try
        {
            var req = JsonSerializer.Serialize(new
            {
                type = "request",
                id,
                method,
                @params = paramsObj,
            });
            await _writeLock.WaitAsync(ct);
            try
            {
                await _stdin.WriteLineAsync(req.AsMemory(), ct);
                await _stdin.FlushAsync(ct);
            }
            finally
            {
                _writeLock.Release();
            }

            return await tcs.Task.WaitAsync(timeout ?? TimeSpan.FromSeconds(10), ct);
        }
        catch (Exception)
        {
            _pending.TryRemove(id, out _); // 超时/取消/断开：清理占位，不留悬空
            throw;
        }
    }

    /// <summary>graceful shutdown：发 shutdown → 等进程自退（grace 内）→ 超时 Kill 整个进程树。不 Dispose。</summary>
    public async Task<int?> ShutdownAsync(TimeSpan grace, CancellationToken ct = default)
    {
        if (!_process.HasExited)
        {
            try
            {
                await CallAsync("shutdown", null, grace, ct);
            }
            catch
            {
                // Python 未响应/已断开：继续走退出流程
            }
        }

        if (!_process.HasExited)
        {
            try
            {
                await _process.WaitForExitAsync(ct).WaitAsync(grace, ct);
            }
            catch (TimeoutException)
            {
                KillTree(); // grace 超时，强制终止
            }
        }
        return ExitCode;
    }

    private void KillTree()
    {
        if (_process.HasExited) return;
        try
        {
            _process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException)
        {
            // 已在退出竞态中退出，忽略
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try { _stdin.Dispose(); } catch { }
        try { _stdout.Dispose(); } catch { }
        try { _writeLock.Dispose(); } catch { }
        if (!_process.HasExited)
            KillTree();
        _process.Dispose();
    }
}
