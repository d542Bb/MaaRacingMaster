using System;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.UI.Xaml.Controls;
using Microsoft.Web.WebView2.Core;

namespace MaaRacingMaster.Shell;

/// <summary>
/// HTML ↔ C# ↔ Python sidecar 的 RPC 转发（主窗口与 PEEP 悬浮窗共用）。
///
/// 之所以能共用：回发按 <c>sender.CoreWebView2</c> 定位，不依赖任何窗口实例字段，
/// 因此每个 WebView2 注册同一套处理即可各自收到自己的响应，不存在串台。
/// sidecar 始终是单进程单通道（<see cref="PythonSidecar"/> 内部有写锁 + 按 id 匹配 pending），
/// 多个窗口并发发请求是安全的。
/// </summary>
internal static class RpcBridge
{
    private static PythonSidecar? s_sidecar;

    /// <summary>绑定/解绑业务后端。窗口启动时绑定，sidecar 关闭后置 null（此后请求回 backend unavailable）。</summary>
    public static void Attach(PythonSidecar? sidecar) => s_sidecar = sidecar;

    /// <summary>
    /// 尝试接管一条前端消息：只处理 <c>type=call</c>，其余消息类型留给各窗口自行处理。
    /// </summary>
    /// <returns>已接管返回 true。</returns>
    public static bool TryHandleCall(WebView2 sender, JsonElement root)
    {
        if (!root.TryGetProperty("type", out var t) || t.GetString() != "call")
            return false;

        var callId = root.GetProperty("callId").GetInt64();
        var method = root.GetProperty("method").GetString() ?? "";
        var paramsEl = root.TryGetProperty("params", out var p) && p.ValueKind != JsonValueKind.Null
            ? p
            : (JsonElement?)null;
        _ = HandleCallAsync(sender, callId, method, paramsEl);
        return true;
    }

    private static async Task HandleCallAsync(WebView2 sender, long callId, string method, JsonElement? paramsEl)
    {
        object? data = null;
        string? error = null;
        var ok = true;
        try
        {
            if (s_sidecar is null)
                throw new InvalidOperationException("backend unavailable");
            var resp = await s_sidecar.CallAsync(method, paramsEl, TimeSpan.FromSeconds(10));
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
            // 窗口关闭边缘 WebView 可能已释放，忽略即可
            Console.Error.WriteLine($"[shell] 回传 JS 失败: {ex.Message}");
        }
    }
}
