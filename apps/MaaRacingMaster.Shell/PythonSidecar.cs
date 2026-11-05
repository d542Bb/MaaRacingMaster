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
    private volatile bool _backendDown;
    private volatile bool _closing;
    private bool _deathReported; // 同一进程世代最多一条死亡事件（随实例创建归零）
    private bool _disposed;

    /// <summary>sidecar 主动推送的事件（type=event）：事件名 + 数据负载（可为 Undefined）。
    /// 由 shell 侧订阅并按需响应，如轮次开始/结束、运行结束自动退出（auto_exit）。</summary>
    public event Action<string, JsonElement>? SidecarEvent;

    /// <summary>后端进程退出（读到 stdout 末尾且非本壳主动关闭）。退出码可能未知。</summary>
    public event Action<int?>? BackendExited;

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
        try
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
                            // 数据负载必须深拷贝：解析文档在循环末释放，跨线程访问即抛已释放异常
                            JsonElement payload = default;
                            if (root.TryGetProperty("data", out var d) && d.ValueKind != JsonValueKind.Null)
                                payload = d.Clone();
                            try { SidecarEvent?.Invoke(evtName, payload); }
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
        }
        catch (Exception ex) when (ex is ObjectDisposedException or IOException)
        {
            // 主动 Dispose / 关窗竞态：流被本壳关闭，不当作后端异常
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[sidecar] stdout reader error: {ex.Message}");
        }
        finally
        {
            // EOF = Python 进程退出 → 所有 pending 立即 disconnected
            FailAllPending(new BackendDisconnectedException(SafeExitCode()));
            _backendDown = true;

            // 只有非主动关闭的 EOF 才是后端死亡：正常关窗/主动关停不报
            if (!_closing && !_deathReported)
            {
                _deathReported = true;
                var exitCode = WaitForExitCode();
                try { BackendExited?.Invoke(exitCode); }
                catch (Exception ex) { Console.Error.WriteLine($"[sidecar] backend exited handler error: {ex.Message}"); }
            }
        }
    }

    /// <summary>读退出码；进程已释放/不可访问时返回 null（退出码只作诊断，不参与判定）。</summary>
    private int? SafeExitCode()
    {
        try { return _process.HasExited ? _process.ExitCode : null; }
        catch { return null; }
    }

    /// <summary>EOF 后短暂等进程真正退出，尽量给死亡通知带上退出码。</summary>
    private int? WaitForExitCode()
    {
        try
        {
            if (_process.WaitForExit(500)) return _process.ExitCode;
            return SafeExitCode();
        }
        catch
        {
            return null;
        }
    }

    private async Task DrainStderrAsync()
    {
        // GUI 无控制台：sidecar stderr 落盘便于排查。固定落用户数据目录
        // （%APPDATA%/MaaRacingMaster/logs，开发/发行一致、不受日志开关控制——框架诊断日志）；
        // APPDATA 不可用时回退工作目录，保证跨机器可移植。
        // 体积有界：超过上限轮转一份备份后重开，长跑不再无界追加。
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        var baseDir = string.IsNullOrEmpty(appData)
            ? (_workDir ?? Environment.CurrentDirectory)
            : Path.Combine(appData, "MaaRacingMaster");
        var logPath = Path.Combine(baseDir, "logs", "sidecar_stderr.log");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
            RotateIfOversize(logPath);
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

    /// <summary>stderr 日志上限（字节）：超过即轮转为 .1（只留一份备份）。</summary>
    private const long StderrLogMaxBytes = 8L * 1024 * 1024;

    /// <summary>超限则把当前 stderr 日志轮转为 .1；失败不抛（不影响日志本身）。</summary>
    private static void RotateIfOversize(string path)
    {
        try
        {
            var fi = new FileInfo(path);
            if (!fi.Exists || fi.Length < StderrLogMaxBytes) return;
            var bak = path + ".1";
            if (File.Exists(bak)) File.Delete(bak);
            File.Move(path, bak);
        }
        catch
        {
            // 轮转失败就继续追加，宁可超限也不丢日志
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
        _closing = true; // 本壳主动关停：随后的 EOF 不再报后端异常退出
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
        _closing = true; // Dispose 会关流并 KillTree，随后的 EOF 不是后端异常退出
        try { _stdin.Dispose(); } catch { }
        try { _stdout.Dispose(); } catch { }
        try { _writeLock.Dispose(); } catch { }
        if (!_process.HasExited)
            KillTree();
        _process.Dispose();
    }
}
