// MaaRM shell 前端 —— 通信层（WebView2 ↔ C# ↔ Python sidecar）+ window.MaaRM 共享命名空间。
// 本文件必须最先加载：共享物（maarm / $ / state / showError / reportError / setInlineStatus /
// mkEl / appendIcon / postWindowAction / currentModuleId）只在此处定义一次，其余模块经
// window.MaaRM 取用；各文件内部私有函数保持文件级作用域，跨文件要用的在各自文件末尾挂上 MaaRM。
// 通信层对 HTML 只暴露 maarm.call，不暴露 JSONL/Python 存在。
//
// 错误反馈通道协议（三级；统一入口 reportError(level, message, opts)，同一失败只报一个
// 通道，禁止「行内红字 + toast」双报）：
//   toast  系统级失败：初始化失败、RPC/连接不可达、启动失败等——toast 4s 自动消失。
//   inline 操作级失败：选项/配置保存失败等——行内状态红字并停留；opts.el 指定落点
//          （配置面板传自己的状态行）；未传或落点不在页面上时回退 toast（错误不得被吞）。
//   modal  需用户决策/确认的场景——经 MaaRM.openModal 弹窗（modal.js 晚于本文件加载，
//          调用时经 MaaRM 延迟取用；启动早期 modal 尚未就绪时回退 toast）。
// 非错误的行内反馈（保存中/运行实况）不走 reportError，由各模块自己的 status 函数处理。
// 成功回执不占任何反馈通道：配置保存成功静默回落到运行实况，界面只报阶段进度与
// 会让本次运行达不到预期的异常（toast 因此只留给系统级失败，不播「已完成」）。
window.MaaRM = (function () {
  'use strict';

  // ---------- 通信层（WebView2 ↔ C# ↔ Python sidecar） ----------
  const maarm = (() => {
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
  window.maarm = maarm;

  // 静态 HTML 的 <i data-icon> 占位替换为真源 SVG（icons.js 先于本文件加载）。
  // 覆盖对象是 index.html 静态骨架；模块动态内容经 MaaRMIcons.svg / appendIcon 自取，
  // 不依赖此处时序。
  MaaRMIcons.hydrate(document);
  // 注册 <morph-icon> 自定义元素（vendor.morphicons.js 先于本文件加载；幂等）
  if (window.MorphIcons) MorphIcons.defineMorphIcon();

  // ---------- 工具 ----------
  const $ = (id) => document.getElementById(id);
  const state = {
    is_running: false,
    // get_initial_state 返回的模块列表（含 expired / valid_until：任务卡徽章与
    // 模块管理弹窗的数据源；stages 供展开面板的细分阶段列表使用）
    modules: [],
    // 运行任务列表：order = 列表内模块（数组序即执行序），enabled = 启用勾选
    queue: { order: [], enabled: {} },
    // 断点起点（模块 id → 阶段名；仅内存，start 时后端消费即清）
    breakpoints: {},
    // 模块 → 插件资源齐备状态（get_status 持续下发，任务卡「资源缺失」徽章）
    moduleAssets: {},
    // 通用默认封面 URL（get_initial_state 的 cover_default）：模块无专属封面或封面
    // 加载失败时的回落目标。由核心解析，前端不拼路径。
    coverDefault: '',
    // 上一轮各模块结局（ok|fail|stopped|skip；null = 尚未跑完整轮）
    roundResult: null,
    // 模块 → 最近一次读到的配置（卡片摘要 chips 的数据源；含运行实况 _state）
    cfg: {},
    // 任务卡展开态（会话内记忆，不持久化）
    expanded: {},
    _lastRunState: false,
    // 录制实况的刷新节流（毫秒时间戳）：运行中每秒刷一次，别跟着 250ms 状态轮询跑
    _lastRecRefresh: 0,
    peepEnabled: false,
    // 性能卡走势图环形缓冲：250ms 轮询 × 120 点 ≈ 近 30 秒。快照里的数值本身
    // 已是滑窗 p50，曲线看的是"什么时候开始变差"，不是逐帧抖动。
    perfHist: { cpuTotal: [], cpuProg: [], gpuTotal: [], gpuProg: [] },
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
    el.classList.remove('maarm-status--ok', 'maarm-status--warn', 'maarm-status--error');
    if (level === 'ok') el.classList.add('maarm-status--ok');
    else if (level === 'warn') el.classList.add('maarm-status--warn');
    else if (level === 'error') el.classList.add('maarm-status--error');
  }

  // 错误反馈统一入口（三级协议与归类规则见文件头注释）
  function reportError(level, message, opts) {
    const text = String(message);
    if (level === 'inline') {
      const el = (opts && opts.el) || null;
      if (el) { setInlineStatus(el, text, 'error'); return; }
      // 行内落点不可用（页面未渲染等）：回退 toast，错误不得被吞
    } else if (level === 'modal' && typeof MaaRM.openModal === 'function') {
      // 消息转义后落地：错误串可能带异常/远程内容，不进 HTML 解析通道
      const esc = text.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
      MaaRM.openModal({
        title: (opts && opts.title) || '出错了',
        maxWidth: opts && opts.maxWidth,
        bodyHtml: '<p class="maarm-modal-text maarm-modal-text--flush">' + esc + '</p>',
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
    tpl.innerHTML = MaaRMIcons.svg(name);
    node.appendChild(tpl.content.firstElementChild);
  }

  // 窗口动作出口（minimize/maximize/close/peep-float）：非 RPC 的 postMessage，与 maarm
  // 同属通信层。app.js 的窗口控制按钮与 settings.js 的预览悬浮按钮共用；放本文件是
  // 因为它先于两个使用方加载。
  function postWindowAction(action) {
    try {
      window.chrome.webview.postMessage({ type: 'win-action', action });
    } catch (e) { /* 非 WebView2 环境（浏览器直开）忽略 */ }
  }

  // 面板模块 id（可变共享状态）：任务卡展开时指向该模块，性能卡与设置页据此渲染；
  // 数据页的看板中心由任务列表独立决定，不读这个值。一律经 MaaRM.panelModuleId 读写，
  // 保证唯一真源在本文件。初始为空（无面板）——真实值由展开/初始化按任务列表写入。
  let panelModuleId = '';

  const MaaRM = { maarm, $, state, showError, reportError, setInlineStatus, mkEl, appendIcon, postWindowAction };
  Object.defineProperty(MaaRM, 'panelModuleId', {
    get: () => panelModuleId,
    set: (v) => { panelModuleId = v; },
  });
  return MaaRM;
})();
