// 双 WebView2 探测脚本（实验产物，验证后随目录一并删除）。
// 目的：确认同进程第二个 WebView2 能加载 file:// 页面、且 WebMessage RPC 通道独立可用。
(function () {
  'use strict';

  var elLog = document.getElementById('log');
  var elStage = document.getElementById('stage');

  function log(text) {
    try { window.chrome.webview.postMessage({ type: 'probe-log', text: text }); } catch (e) { /* 宿主未就绪 */ }
    if (elLog) elLog.textContent += text + '\n';
    if (elStage) elStage.textContent = text;
  }

  var hasBridge = !!(window.chrome && window.chrome.webview);
  log('probe.js 已执行，chrome.webview = ' + hasBridge);
  if (!hasBridge) return;

  // 最小通信层：与 app.js 的 mra 同构（含超时，用于暴露「回发串台」）
  var mra = (function () {
    var nextCallId = 1;
    var pending = new Map();
    window.chrome.webview.addEventListener('message', function (e) {
      var msg = e.data;
      if (!msg || msg.type !== 'response') return;
      var p = pending.get(msg.callId);
      if (!p) { log('!! 收到未知 callId=' + msg.callId + '（疑似两个 WebView2 串台）'); return; }
      pending.delete(msg.callId);
      if (msg.ok) p.resolve(msg.data); else p.reject(new Error(msg.error || 'rpc error'));
    });
    function call(method, params) {
      return new Promise(function (resolve, reject) {
        var callId = nextCallId++;
        pending.set(callId, { resolve: resolve, reject: reject });
        window.chrome.webview.postMessage({ type: 'call', callId: callId, method: method, params: params || {} });
        setTimeout(function () {
          if (pending.has(callId)) { pending.delete(callId); reject(new Error('timeout(3s)')); }
        }, 3000);
      });
    }
    return { call: call };
  })();

  var stat = { rpcOk: 0, rpcFail: 0, frameOk: 0, frameNull: 0, frameBytes: 0 };

  function tick(round) {
    return mra.call('get_debug_state').then(function (d) {
      stat.rpcOk++;
      log('第' + round + '轮 RPC 通，peep_enabled=' + (d && d.peep_enabled));
    }).catch(function (e) {
      stat.rpcFail++;
      log('第' + round + '轮 RPC 失败：' + e.message);
    }).then(function () {
      return mra.call('get_peep_frame').then(function (f) {
        if (f && f.frame) { stat.frameOk++; stat.frameBytes = f.frame.length; log('取到帧，base64 长度=' + f.frame.length); }
        else { stat.frameNull++; log('无帧（会话未运行，预期）'); }
      }).catch(function (e) { log('取帧失败：' + e.message); });
    });
  }

  var round = 0;
  var timer = setInterval(function () {
    round++;
    if (round > 16) {
      clearInterval(timer);
      log('=== 汇总 rpc_ok=' + stat.rpcOk + ' rpc_fail=' + stat.rpcFail +
          ' frame_ok=' + stat.frameOk + ' frame_null=' + stat.frameNull +
          ' 最大帧长=' + stat.frameBytes + ' ===');
      return;
    }
    tick(round);
  }, 500);
})();
