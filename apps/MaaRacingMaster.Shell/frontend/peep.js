// PEEP 悬浮窗页面逻辑：通信层 + 常驻状态标题栏 + 播放器式悬浮控件。
// 帧消费与数据页预览卡共用 peep-consumer.js；本窗口存在期间由本窗口独占消费
// （主界面此时退化为占位符，见 app.js 的 previewMode 状态机）。
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const body = $('peep-body');

  // ---------- 通信层（与 app.js 的 mra 同构：同一协议、同一后端） ----------
  const mra = (() => {
    let nextCallId = 1;
    const pending = new Map();

    window.chrome.webview.addEventListener('message', (e) => {
      const msg = e.data;
      if (!msg || msg.type !== 'response') return;
      const p = pending.get(msg.callId);
      if (!p) return;
      pending.delete(msg.callId);
      if (msg.ok) p.resolve(msg.data);
      else p.reject(new Error(msg.error || 'rpc error'));
    });

    function call(method, params) {
      return new Promise((resolve, reject) => {
        const callId = nextCallId++;
        pending.set(callId, { resolve, reject });
        window.chrome.webview.postMessage({ type: 'call', callId, method, params: params || {} });
      });
    }

    return { call };
  })();

  MRAIcons.hydrate(document);

  // ---------- 帧消费（本窗口独占；无帧时显示占位层） ----------
  const consumer = PeepConsumer.create(mra, {
    isActive: () => true, // 悬浮窗存在即代表自己是唯一消费者，无需额外门控
    img: () => $('peep-img'),
    empty: () => $('peep-empty')
  });
  consumer.start(0);

  // ---------- PEEP 开关（播放/暂停） ----------
  let peepEnabled = false;

  function setPlayState(on) {
    peepEnabled = on;
    body.classList.toggle('peep-body--playing', on);
    const btn = $('peep-btn-toggle');
    if (btn) btn.title = on ? '暂停预览' : '开始预览';
  }

  $('peep-btn-toggle').addEventListener('click', async () => {
    const on = !peepEnabled;
    setPlayState(on); // 先翻转视觉状态
    try {
      await PeepConsumer.setPeep(mra, on);
    } catch (e) {
      setPlayState(!on); // 失败回滚
    }
  });

  // ---------- 关闭（还原到主界面） ----------
  $('peep-btn-close').addEventListener('click', () => {
    window.chrome.webview.postMessage({ type: 'win-action', action: 'close' });
  });

  // ---------- 常驻状态标题栏（与主界面底栏同源：get_status，250ms） ----------
  function renderStatus(d) {
    const dot = $('peep-status-dot');
    const txt = $('peep-status-text');
    if (!dot || !txt) return;
    if (d.current_stage) {
      txt.textContent = '运行中 · 当前: ' + d.current_stage;
      dot.className = 'mra-status-dot mra-status-dot--running mra-status-dot--pulse';
    } else if (d.is_running || d.worker_active) {
      txt.textContent = '停止中...';
      dot.className = 'mra-status-dot mra-status-dot--stopping';
    } else {
      txt.textContent = '系统就绪';
      dot.className = 'mra-status-dot mra-status-dot--ready';
    }
  }

  async function pollStatus() {
    try {
      const d = await mra.call('get_status');
      renderStatus(d);
    } catch (e) { /* 状态轮询失败静默 */ }
    finally { setTimeout(pollStatus, 250); }
  }

  // ---------- 启动：回填 PEEP 开关状态，再起状态轮询 ----------
  (async () => {
    try {
      const d = await mra.call('get_debug_state');
      setPlayState(!!d.peep_enabled);
    } catch (e) { /* 后端未就绪：保持默认（未播放）态 */ }
    pollStatus();
  })();
})();
