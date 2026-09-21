// MaaRM shell 前端 —— 通信层（WebView2 ↔ C# ↔ Python sidecar）+ window.MRA 共享命名空间。
// 本文件必须最先加载：共享物（mra / $ / state / showError / reportError / setInlineStatus /
// mkEl / appendIcon / postWindowAction / currentModuleId）只在此处定义一次，其余模块经
// window.MRA 取用；各文件内部私有函数保持文件级作用域，跨文件要用的在各自文件末尾挂上 MRA。
// 通信层对 HTML 只暴露 mra.call，不暴露 JSONL/Python 存在。
//
// 错误反馈通道协议（三级；统一入口 reportError(level, message, opts)，同一失败只报一个
// 通道，禁止「行内红字 + toast」双报）：
//   toast  系统级失败：初始化失败、RPC/连接不可达、启动失败等——toast 4s 自动消失。
//   inline 操作级失败：选项/配置保存失败等——行内状态红字并停留；默认落主控页「模块选项」
//          状态行 #module-options-status，opts.el 可指定其他元素；落点不可用时回退 toast
//          （错误不得被吞）。设置/数据页暂无行内落点，该页操作失败保持 toast——新增落点
//          属视觉变更，不在本协议范围。
//   modal  需用户决策/确认的场景——经 MRA.openModal 弹窗（modal.js 晚于本文件加载，
//          调用时经 MRA 延迟取用；启动早期 modal 尚未就绪时回退 toast）。
// 非错误的行内反馈（保存中/已保存/运行实况）不走 reportError，由各模块自己的 status 函数处理。
window.MRA = (function () {
  'use strict';

  // ---------- 通信层（WebView2 ↔ C# ↔ Python sidecar） ----------
  const mra = (() => {
    let nextCallId = 1;
    const pending = new Map();

    function bootBridge() {
      window.chrome.webview.addEventListener('message', (e) => {
        const msg = e.data;
        if (!msg) return;
        // 非 response 消息 = C# 主动推送（如 maximized 状态），分发给订阅者
        if (msg.type !== 'response') {
          nativeListeners.forEach((fn) => fn(msg));
          return;
        }
        const p = pending.get(msg.callId);
        if (!p) return;
        pending.delete(msg.callId);
        if (msg.ok) p.resolve(msg.data);
        else p.reject(new Error(msg.error || 'rpc error'));
      });
    }

    const nativeListeners = new Set();
    function onNativeMessage(fn) { nativeListeners.add(fn); }

    function call(method, params) {
      return new Promise((resolve, reject) => {
        const callId = nextCallId++;
        pending.set(callId, { resolve, reject });
        window.chrome.webview.postMessage({ type: 'call', callId, method, params: params || {} });
      });
    }

    bootBridge();
    return { call, onNativeMessage };
  })();
  window.mra = mra;

  // 静态 HTML 的 <i data-icon> 占位替换为真源 SVG（icons.js 先于本文件加载）。
  // 覆盖对象是 index.html 静态骨架；模块动态内容经 MRAIcons.svg / appendIcon 自取，
  // 不依赖此处时序。
  MRAIcons.hydrate(document);
  // 注册 <morph-icon> 自定义元素（vendor.morphicons.js 先于本文件加载；幂等）
  if (window.MorphIcons) MorphIcons.defineMorphIcon();

  // ---------- 工具 ----------
  const $ = (id) => document.getElementById(id);
  const state = {
    stages: [],
    selected_index: -1,
    is_running: false,
    // get_initial_state 返回的模块列表（含 expired / valid_until：下拉栏置灰与过期警告文案的数据源）
    modules: [],
    _lastRunState: false,
    // 录制实况的刷新节流（毫秒时间戳）：运行中每秒刷一次，别跟着 250ms 状态轮询跑
    _lastRecRefresh: 0,
    peepEnabled: false,
    // 性能卡走势图环形缓冲：250ms 轮询 × 120 点 ≈ 近 30 秒。快照里的数值本身
    // 已是滑窗 p50，曲线看的是"什么时候开始变差"，不是逐帧抖动。
    perfHist: { fps: [], load: [] },
    PERF_HIST_MAX: 120
  };

  function showError(msg) {
    const toast = $('error-toast');
    toast.textContent = msg;
    toast.style.display = 'block';
    clearTimeout(showError._t);
    showError._t = setTimeout(() => { toast.style.display = 'none'; }, 4000);
  }

  // 行内状态渲染原语：inline 通道的唯一实现（settings.js 的 setOptionsStatus 委托到它，
  // 不产生第二份渲染逻辑）。level ∈ '' | 'ok' | 'warn' | 'error'。
  function setInlineStatus(el, text, level) {
    if (!el) return;
    el.textContent = text || '';
    el.classList.remove('mra-status--ok', 'mra-status--warn', 'mra-status--error');
    if (level === 'ok') el.classList.add('mra-status--ok');
    else if (level === 'warn') el.classList.add('mra-status--warn');
    else if (level === 'error') el.classList.add('mra-status--error');
  }

  // 错误反馈统一入口（三级协议与归类规则见文件头注释）
  function reportError(level, message, opts) {
    const text = String(message);
    if (level === 'inline') {
      const el = (opts && opts.el) || $('module-options-status');
      if (el) { setInlineStatus(el, text, 'error'); return; }
      // 行内落点不可用（页面未渲染等）：回退 toast，错误不得被吞
    } else if (level === 'modal' && typeof MRA.openModal === 'function') {
      // 消息转义后落地：错误串可能带异常/远程内容，不进 HTML 解析通道
      const esc = text.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
      MRA.openModal({
        title: (opts && opts.title) || '出错了',
        maxWidth: opts && opts.maxWidth,
        bodyHtml: '<p class="mra-modal-text mra-modal-text--flush">' + esc + '</p>',
        buttons: [{ text: '知道了', primary: true, onClick: (m) => m.close() }],
      });
      return;
    }
    showError(text); // toast：默认通道（含 modal 尚未就绪的启动早期）
  }

  // 建元素 + 纯文本：远程数据（公告/更新元数据）一律经 textContent 落地，
  // 不参与 HTML 解析——它可能来自任何一处 CDN/镜像，一律视为不可信输入。
  function mkEl(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // 插入本地图标（icons.js 的静态 SVG 串）。这是远程渲染路径上唯一允许 innerHTML 的
  // 地方：源串是本仓库静态资源、与远程数据无关，静态锁（tests/test_frontend_remote_render.py）
  // 据此把「远程字段 + innerHTML」的组合钉死在这一处之外。
  function appendIcon(node, name) {
    const tpl = document.createElement('template');
    tpl.innerHTML = MRAIcons.svg(name);
    node.appendChild(tpl.content.firstElementChild);
  }

  // 窗口动作出口（minimize/maximize/close/peep-float）：非 RPC 的 postMessage，与 mra
  // 同属通信层。app.js 的窗口控制按钮与 settings.js 的预览悬浮按钮共用；放本文件是
  // 因为它先于两个使用方加载。
  function postWindowAction(action) {
    try {
      window.chrome.webview.postMessage({ type: 'win-action', action });
    } catch (e) { /* 非 WebView2 环境（浏览器直开）忽略 */ }
  }

  // 当前活动模块 id（可变共享状态）：一律经 MRA.currentModuleId 读写（下方访问器），
  // 保证唯一真源在本文件。
  let currentModuleId = 'treasure';

  const MRA = { mra, $, state, showError, reportError, setInlineStatus, mkEl, appendIcon, postWindowAction };
  Object.defineProperty(MRA, 'currentModuleId', {
    get: () => currentModuleId,
    set: (v) => { currentModuleId = v; },
  });
  return MRA;
})();
