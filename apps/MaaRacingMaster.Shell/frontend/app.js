// MRA shell 前端 —— 通信层 + 三 tab 页面逻辑。
// 通信层：HTML 只看到 mra.call，不知道 JSONL/Python 存在。
(function () {
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

  // ---------- 工具 ----------
  const $ = (id) => document.getElementById(id);
  const state = {
    stages: [],
    selected_index: -1,
    is_running: false,
    _lastRunState: false,
    peepEnabled: false
  };

  function showError(msg) {
    const toast = $('error-toast');
    toast.textContent = msg;
    toast.style.display = 'block';
    clearTimeout(showError._t);
    showError._t = setTimeout(() => { toast.style.display = 'none'; }, 4000);
  }

  // ---------- 通用模态弹窗 ----------
  // overlay + 居中卡片；opts: { title, titleColor, bodyHtml, maxWidth, buttons:[{text, primary, asLink, href, onClick(modal)}] }
  // onClick 回调自主决定是否调用 modal.close()；点空白处默认关闭
  function openModal(opts) {
    const overlay = document.createElement('div');
    overlay.className = 'mra-modal-overlay';
    overlay.style.cssText =
      'position:fixed;inset:0;background:rgba(0,0,0,0.45);display:flex;align-items:center;' +
      'justify-content:center;z-index:9999;';
    const card = document.createElement('div');
    card.className = 'mra-modal-card';
    card.style.cssText =
      'background:var(--mra-surface,#1c1f26);border:1px solid var(--mra-border,#2a2f3a);' +
      'border-radius:12px;padding:22px 24px;max-width:' + (opts.maxWidth || 420) + 'px;width:92%;' +
      'box-shadow:0 12px 40px rgba(0,0,0,0.4);display:flex;flex-direction:column;max-height:86vh;';
    const h3 = document.createElement('h3');
    h3.style.cssText = 'margin:0 0 10px;font-size:15px;flex-shrink:0;color:' +
      (opts.titleColor || 'var(--mra-foreground,#e5e7eb)') + ';';
    h3.textContent = opts.title || '';
    card.appendChild(h3);
    const body = document.createElement('div');
    // 内容区滚动：标题/按钮固定，超出 86vh 只滚 body；长串（注册表路径等）强制断行防溢出
    body.style.cssText = 'overflow-y:auto;min-height:0;word-break:break-word;';
    body.innerHTML = opts.bodyHtml || '';
    card.appendChild(body);
    const modal = {
      overlay, card,
      close: () => {
        if (overlay._closing) return;
        overlay._closing = true;
        overlay.classList.add('mra-modal-overlay--closing');
        card.classList.add('mra-modal-card--closing');
        setTimeout(() => overlay.remove(), 130);
      }
    };
    if (Array.isArray(opts.buttons) && opts.buttons.length) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:10px;justify-content:flex-end;align-items:center;margin-top:14px;flex-shrink:0;';
      opts.buttons.forEach((b) => {
        let el;
        if (b.asLink) {
          el = document.createElement('a');
          el.href = b.href || '#';
          el.target = '_blank';
          el.rel = 'noopener';
          el.style.cssText = 'font-size:12px;color:var(--mra-info,#38bdf8);';
        } else {
          el = document.createElement('button');
          el.style.cssText = 'cursor:pointer;border-radius:8px;font-size:13px;padding:8px 16px;' +
            (b.primary
              ? 'border:none;background:var(--mra-primary,#2563eb);color:#fff;'
              : 'border:1px solid var(--mra-border,#2a2f3a);background:transparent;color:var(--mra-foreground-secondary,#8b93a3);');
        }
        el.textContent = b.text;
        if (b.onClick) el.addEventListener('click', () => b.onClick(modal));
        row.appendChild(el);
      });
      card.appendChild(row);
    }
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) modal.close(); });
    // 进场动画结束后摘动画类：卡片回归主文档光栅化，避免非整数 DPI 下文字发虚（同 page-slide-in 手法）
    card.addEventListener('animationend', (ev) => {
      if (ev.animationName === 'modal-card-in') card.classList.remove('mra-modal-card');
    });
    return modal;
  }

  // ---------- ViGEmBus 驱动缺失引导弹框 ----------
  const VIGEM_DL_URL = 'https://github.com/nefarius/ViGEmBus/releases/latest';
  // 关于页底部跳转链接
  const REPO_URL = 'https://github.com/d542Bb/MaaRacingAssistant';
  const ABOUT_LINKS = {
    home: REPO_URL,
    issue: REPO_URL + '/issues',
    docs: REPO_URL + '/blob/master/docs/CODE_WIKI.md',
  };
  function showVigemDialog(detailMsg) {
    openModal({
      title: '缺少 ViGEmBus 驱动',
      titleColor: 'var(--mra-danger,#ef4444)',
      bodyHtml:
        '<p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:var(--mra-foreground,#e5e7eb);">' +
        '需要虚拟手柄（vgamepad）控制的模块，底层依赖 <b>ViGEmBus</b> 内核驱动。它无法随解压包分发，需在本机安装一次。</p>',
      buttons: [
        { text: '手动打开下载页', asLink: true, href: VIGEM_DL_URL },
        {
          text: '下载并安装 ViGEmBus 驱动', primary: true,
          onClick: async (modal) => {
            try {
              await mra.call('open_vigembus_download', { url: VIGEM_DL_URL });
            } catch (err) {
              // 后端打开失败：前端兜底新开标签页
              window.open(VIGEM_DL_URL, '_blank');
            }
            showError('已打开 ViGEmBus 下载页。下载安装后请重新运行。');
            modal.close();
          }
        },
        { text: '暂不', onClick: (modal) => modal.close() },
      ],
    });
    void detailMsg;
  }

  // ---------- 后台(手柄)使用须知：每次切入 gamepad 点击方式都会弹出 ----------
  function showGamepadNotice() {
    openModal({
      title: '后台(手柄)使用须知',
      titleColor: 'var(--mra-info,#38bdf8)',
      bodyHtml:
        '<div style="font-size:13px;line-height:1.7;color:var(--mra-foreground,#e5e7eb);">' +
        '<p style="margin:0 0 10px;"><b>适用场景</b>：游戏留在后台挂机，前台可正常聊聊天、看视频等——这些<b>不接收手柄操作</b>的程序不受影响，可放心共存。</p>' +
        '<p style="margin:0 0 10px;"><b>不能与其他游戏并存</b>：本项目用虚拟手柄（ViGEmBus）输入，手柄状态对系统是<b>全局</b>的。运行期间<b>不要</b>同时启动<b>会识别手柄、接收手柄输入</b>的程序（如其他游戏、Steam 等），否则它们也会收到手柄输入，可能被误操作。</p>' +
        '<p style="margin:0;color:var(--mra-foreground-secondary,#8b93a3);">挂机结束建议切回前台(鼠标)或停止程序，避免空闲时手柄误触。</p>' +
        '</div>',
      buttons: [
        { text: '知道了', primary: true, onClick: (modal) => modal.close() },
      ],
    });
  }

  // ---------- 注册表权限优化（启动体检 + 设置页优化中心） ----------
  // 渲染单个优化项（三态行式卡片）：available===false→无需处理；optimized→已优化；否则待优化。
  // 状态 pill 与操作按钮同处底部一行紧邻；值/路径/后果等技术细节收进 <details> 默认折叠。
  // 样式统一走 style.css 的 .opt-* 类（复用设计令牌，不再堆内联样式）。
  function optimizerItemHtml(it) {
    const na = it.available === false;
    const state = na ? 'na' : (it.optimized ? 'done' : 'todo');
    const pillText = na ? '无需处理' : (it.optimized ? '已优化' : '待优化');
    const optsText = it.options
      ? Object.keys(it.options).map((k) => k + ' = ' + it.options[k]).join(' · ')
      : '';
    const currentTxt = (it.current === null || it.current === undefined)
      ? '未设置（系统默认）'
      : (it.options && it.options[String(it.current)]
          ? it.options[String(it.current)] + '（值 ' + it.current + '）'
          : String(it.current));
    // protocol_command 的动作落在子键 shell\open\command（+ NoOpenWith 标记），路径行按 kind 拼装
    const pathLines = (it.paths && it.paths.length ? it.paths : [it.path])
      .map((p) => it.kind === 'protocol_command'
        ? it.hive + '\\' + p + '\\shell\\open\\command（默认值 = 空）<br>'
          + it.hive + '\\' + p + '\\' + it.value_name + '（标记值）'
        : it.hive + '\\' + p + '\\' + it.value_name)
      .join('<br>');
    let actions = '';
    if (!na) {
      if (!it.optimized) {
        actions += '<button class="opt-btn opt-btn--primary opt-apply" data-id="' + it.id + '">'
          + (it.apply_label || '优化') + '</button>';
      }
      actions += '<button class="opt-btn opt-btn--ghost opt-restore" data-id="' + it.id + '">'
        + (it.restore_label || '恢复默认') + '</button>';
    }
    if (it.prompt_ignored) {
      actions += '<button class="opt-btn opt-btn--ghost opt-unignore" data-id="' + it.id + '">恢复启动提醒</button>';
    }
    const note = (na && it.unavailable_note)
      ? '<p class="opt-item-note">' + it.unavailable_note + '</p>' : '';
    return '<div class="opt-item opt-item--' + state + '">'
      + '<div class="opt-item-title">' + it.name + '</div>'
      + '<div class="opt-item-effect">' + it.effect + '</div>'
      + note
      + '<details class="opt-details"><summary>技术细节</summary><div class="opt-details-body">'
      + '<p>值：<b>' + it.value_name + '</b>　可选值：' + optsText + '　当前：' + currentTxt + '</p>'
      + '<p class="opt-path">' + pathLines + '</p>'
      + '<p>后果：' + it.detail + '</p>'
      + '</div></details>'
      + '<div class="opt-item-foot"><span class="opt-pill opt-pill--' + state + '">' + pillText + '</span>'
      + '<span class="opt-actions">' + actions + '</span></div>'
      + '</div>';
  }

  // 写入单个优化项，返回错误信息（null = 成功）
  async function applyOptimization(id, value) {
    try {
      await mra.call('set_registry_optimization', { id: id, value: value });
      return null;
    } catch (e) {
      return e.message;
    }
  }

  // 设置单个优化项的启动提醒忽略状态（true=忽略，false=恢复），返回错误信息
  async function applyPromptIgnore(id, ignored) {
    try {
      await mra.call('set_optimization_prompt_ignored', { id: id, ignored: ignored });
      return null;
    } catch (e) {
      return e.message;
    }
  }

  // 权限优化中心（设置页入口）：顶部汇总条（三档计数 + 一键全部优化）+ 三态卡片列表
  async function openOptimizerCenter() {
    let items;
    try {
      const d = await mra.call('get_registry_optimizations');
      items = d.items || [];
    } catch (e) {
      showError('读取优化项失败: ' + e.message);
      return;
    }
    if (!items.length) {
      showError('当前系统没有可用的注册表优化项');
      return;
    }
    const modal = openModal({
      title: '权限优化中心',
      maxWidth: 540,
      bodyHtml: '<div id="opt-summary"></div><div id="opt-center-list"></div>',
      buttons: [{ text: '关闭', primary: true, onClick: (m) => m.close() }],
    });
    const summaryEl = modal.card.querySelector('#opt-summary');
    const listEl = modal.card.querySelector('#opt-center-list');
    // 汇总三档：待优化 / 已优化 / 无需处理。available===false 计入"无需处理"，不混入"已优化"
    function renderSummary(list) {
      const todo = list.filter((it) => it.available !== false && !it.optimized);
      const done = list.filter((it) => it.available !== false && it.optimized).length;
      const na = list.filter((it) => it.available === false).length;
      const parts = [];
      if (todo.length) parts.push('<b class="opt-num--todo">' + todo.length + '</b> 项待优化');
      if (done) parts.push('<b class="opt-num--done">' + done + '</b> 项已优化');
      if (na) parts.push('<b>' + na + '</b> 项无需处理');
      const text = parts.length ? parts.join(' · ') : '全部已优化';
      const btn = todo.length
        ? '<button class="opt-btn opt-btn--primary opt-apply-all">一键优化全部</button>' : '';
      summaryEl.innerHTML = '<div class="opt-summary"><span class="opt-summary-text">'
        + text + '</span>' + btn + '</div>';
    }
    async function refresh() {
      try {
        const d = await mra.call('get_registry_optimizations');
        const its = d.items || [];
        renderSummary(its);
        listEl.innerHTML = its.map(optimizerItemHtml).join('');
      } catch (e) {
        listEl.innerHTML = '<p style="margin:0;font-size:12px;color:var(--mra-danger)">刷新失败: ' + e.message + '</p>';
      }
    }
    // 事件委托到 modal.card：覆盖汇总条(一键全部)与列表(单项)，列表 innerHTML 重渲染不影响委托
    modal.card.addEventListener('click', async (ev) => {
      const applyAllBtn = ev.target.closest('.opt-apply-all');
      if (applyAllBtn) {
        applyAllBtn.disabled = true;
        const its = ((await mra.call('get_registry_optimizations')).items) || [];
        const todo = its.filter((it) => it.available !== false && !it.optimized);
        const fails = [];
        for (const it of todo) {
          const err = await applyOptimization(it.id, it.optimized_value);
          if (err) fails.push(it.name + '：' + err);
        }
        if (fails.length) showError('部分优化失败：' + fails.join('；'));
        else showError('已完成 ' + todo.length + ' 项权限优化');
        await refresh();
        return;
      }
      const applyBtn = ev.target.closest('.opt-apply');
      const restoreBtn = ev.target.closest('.opt-restore');
      const unignoreBtn = ev.target.closest('.opt-unignore');
      const btn = applyBtn || restoreBtn || unignoreBtn;
      if (!btn) return;
      btn.disabled = true;
      let err;
      if (applyBtn) err = await applyOptimization(applyBtn.dataset.id, 0);
      else if (restoreBtn) err = await applyOptimization(restoreBtn.dataset.id, 1);
      else err = await applyPromptIgnore(unignoreBtn.dataset.id, false);
      if (err) showError(err);
      await refresh(); // 无论成败都刷新（失败项状态不变，按钮随重渲染恢复可用）
    });
    refresh();
  }

  // 启动体检：存在未优化且未忽略的项则弹一键优化引导（按项忽略，新增优化项不受影响）
  async function checkRegistryOptimizations() {
    let pending;
    try {
      const d = await mra.call('get_registry_optimizations');
      // available === false = 目标应用已安装、该项无需处理，不进体检提醒
      pending = (d.items || []).filter((it) => !it.optimized && !it.prompt_ignored && it.available !== false);
    } catch (e) {
      console.error('权限体检失败:', e);
      return;
    }
    if (!pending.length) return;
    openModal({
      title: '检测到 ' + pending.length + ' 项系统权限可优化',
      titleColor: 'var(--mra-warning,#f59e0b)',
      bodyHtml: pending.map((it) =>
        '<p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:var(--mra-foreground,#e5e7eb);">'
        + '<b>' + it.name + '</b>：' + it.effect + '</p>').join('')
        + '<p style="margin:0;font-size:12px;line-height:1.6;color:var(--mra-foreground-secondary,#8b93a3);">'
        + '详情与手动调整见 设置 → 权限优化。</p>',
      buttons: [
        {
          text: '一键优化（推荐）', primary: true,
          onClick: async (modal) => {
            const fails = [];
            for (const it of pending) {
              const err = await applyOptimization(it.id, it.optimized_value);
              if (err) fails.push(it.name + '：' + err);
            }
            if (fails.length) showError('部分优化失败：' + fails.join('；'));
            else showError('已完成 ' + pending.length + ' 项权限优化');
            modal.close();
          }
        },
        {
          // 按项忽略（持久化 profile）：这些项不再弹启动提醒；以后新增的优化项照常提醒
          text: '下次不再提醒',
          onClick: async (modal) => {
            const fails = [];
            for (const it of pending) {
              const err = await applyPromptIgnore(it.id, true);
              if (err) fails.push(it.name + '：' + err);
            }
            if (fails.length) showError('部分忽略失败：' + fails.join('；'));
            else showError('已忽略启动提醒，可随时在 设置 → 权限优化 中心重新开启');
            modal.close();
          }
        },
        { text: '暂不', onClick: (modal) => modal.close() },
      ],
    });
  }

  // ---------- Tab 切换 ----------
  const tabButtons = Array.from(document.querySelectorAll('.mra-tab'));
  const TAB_ORDER = ['control', 'data', 'settings', 'about'];
  let _curTabIdx = 0; // 初始即主控 tab（HTML 默认激活），防止首次点当前 tab 误触发切换动画
  function switchTab(name) {
    const nextIdx = TAB_ORDER.indexOf(name);
    if (nextIdx === _curTabIdx) return; // 点击当前激活 tab：不重播切换动画
    const prevIdx = _curTabIdx >= 0 ? _curTabIdx : nextIdx;
    _curTabIdx = nextIdx;
    const fromRight = nextIdx > prevIdx; // 前进：新页从右滑入；后退：从左滑入
    tabButtons.forEach((btn) => {
      btn.classList.toggle('mra-tab--active', btn.dataset.tab === name);
    });
    TAB_ORDER.forEach((p) => {
      $('page-' + p).classList.toggle('hidden', p !== name);
    });
    animatePageIn($('page-' + name), fromRight);
    moveTabSlider(name);
  }
  // 滑块缓动平移到目标 tab 底部（用 offsetLeft/offsetWidth，不逐页遍历）
  // 宽度取 tab 的 85%，并在 tab 内水平居中
  function moveTabSlider(name) {
    const btn = tabButtons.find((b) => b.dataset.tab === name);
    const slider = $('tab-slider');
    if (!btn || !slider) return;
    const w = Math.round(btn.offsetWidth * 0.85);
    slider.style.left = (btn.offsetLeft + Math.round((btn.offsetWidth - w) / 2)) + 'px';
    slider.style.width = w + 'px';
  }
  // 只对新页做单次滑入（缓动曲线），跨多 tab 也只在本页与当前视觉间播放，不逐页遍历
  function animatePageIn(page, fromRight) {
    if (!page) return;
    page.classList.remove('page-slide-in');
    void page.offsetWidth; // 强制 reflow 重置动画
    page.style.setProperty('--slide-from', fromRight ? '42px' : '-42px');
    page.classList.add('page-slide-in');
    // 动画结束/被打断后立即摘掉动画类：页面回归主文档光栅化。
    // 合成层提升窗口严格限定在动画期间；残留类会让页面停留在合成层路径，
    // 非整数 DPI（150%）下文字发虚。animationName 过滤避免子元素动画误触发。
    const detach = () => {
      page.removeEventListener('animationend', onEnd);
      page.removeEventListener('animationcancel', detach);
      if (page.classList.contains('page-slide-in'))
        page.classList.remove('page-slide-in');
    };
    const onEnd = (e2) => {
      if (e2.animationName !== 'page-slide') return;
      detach();
    };
    page.addEventListener('animationend', onEnd);
    page.addEventListener('animationcancel', detach);
  }
  tabButtons.forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  moveTabSlider('control'); // 初始：主控 tab 默认激活，滑块落地到首项
  // 关于页底部跳转按钮（经 sidecar open_external_url 用默认浏览器打开）
  document.querySelectorAll('[data-link]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const url = ABOUT_LINKS[btn.dataset.link];
      if (!url) return;
      try {
        await mra.call('open_external_url', { url });
      } catch (e) {
        showError('打开链接失败: ' + e.message);
      }
    });
  });

  // ---------- 关于页：检查更新 / 公告 ----------
  function openUrl(url) {
    if (!url) return;
    mra.call('open_external_url', { url }).catch((e) => showError('打开链接失败: ' + e.message));
  }

  // 渲染「版本与更新」卡状态
  function renderUpdateStatus(statusEl, cls, html) {
    statusEl.className = 'ver-status ' + cls;
    statusEl.innerHTML = html;
  }

  async function checkUpdate() {
    const btn = $('btn-check-update');
    const statusEl = $('about-update-status');
    const releaseEl = $('about-new-release');
    if (!btn || !statusEl) return;
    btn.disabled = true;
    renderUpdateStatus(statusEl, 'ver-status--checking',
      '<span class="spinner"></span>正在检查更新…');
    releaseEl.style.display = 'none';
    try {
      const d = await mra.call('check_update');
      if (d.error) {
        renderUpdateStatus(statusEl, 'ver-status--err', d.error);
      } else if (d.status === 'no_release') {
        renderUpdateStatus(statusEl, 'ver-status--ok', '暂无发布版本');
      } else if (d.has_update) {
        renderUpdateStatus(statusEl, 'ver-status--new', '发现新版本 v' + d.latest_tag);
        releaseEl.style.display = 'flex';
        releaseEl.innerHTML =
          '<div class="nr-info">' +
            '<div class="nr-title">v' + d.latest_tag + ' 已发布</div>' +
            '<div class="nr-sub">' + (d.published_at ? '发布于 ' + d.published_at + ' · ' : '') + '建议更新到最新版本</div>' +
          '</div>' +
          '<button class="mra-btn mra-btn--primary" id="btn-go-download" type="button">前往下载<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7"/><path d="M7 7h10v10"/></svg></button>';
        $('btn-go-download').addEventListener('click', () => openUrl(d.download_url));
      } else {
        renderUpdateStatus(statusEl, 'ver-status--ok', '已是最新版本');
      }
    } catch (e) {
      console.error(e);
      renderUpdateStatus(statusEl, 'ver-status--err', '检查失败：' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // 拉取并渲染公告
  async function fetchAnnouncement() {
    const bodyEl = $('about-announce-body');
    if (!bodyEl) return;
    try {
      const d = await mra.call('fetch_announcement');
      if (!d || d.level === 'none' || !d.title) {
        bodyEl.innerHTML = '<div class="about-announce-empty">暂无公告</div>';
        return;
      }
      const cls = d.level === 'warn' ? 'about-announce--warn' : 'about-announce--info';
      const badge = d.level === 'warn' ? '重要' : '公告';
      const dateHtml = d.date ? '<span class="about-announce-date">' + d.date + '</span>' : '';
      const linkHtml = d.url
        ? '<button class="about-announce-link" id="btn-announce-link" type="button">' + (d.url_text || '查看详情') + '</button>'
        : '';
      bodyEl.innerHTML =
        '<div class="about-announce ' + cls + '">' +
          '<span class="about-announce-badge">' + badge + '</span>' +
          '<div class="about-announce-main">' +
            '<div class="about-announce-title">' + d.title + dateHtml + '</div>' +
            (d.body ? '<div class="about-announce-body"></div>' : '') +
            linkHtml +
          '</div>' +
        '</div>';
      if (d.body) bodyEl.querySelector('.about-announce-body').textContent = d.body;
      const linkBtn = $('btn-announce-link');
      if (linkBtn) linkBtn.addEventListener('click', () => openUrl(d.url));
    } catch (e) {
      console.error(e);
      bodyEl.innerHTML = '<div class="about-announce-empty">暂无公告</div>';
    }
  }

  function initAbout() {
    const btn = $('btn-check-update');
    if (btn) btn.addEventListener('click', checkUpdate);
    fetchAnnouncement(); // 启动拉一次公告（进入关于页即展示）
  }
  initAbout();

  // ---------- 关于页彩蛋：点击版本号掉落文字（同款 MAA） ----------
  // 彩蛋内容占位：null = 掉落当前版本号；想好后填字符串数组即随机取用
  const FALLING_EGG_TEXTS = null;
  const MAX_FALLING = 40;
  // 连点 EGG_DIALOG_CLICKS 次（2s 内不中断）弹出「员工守则」
  const EGG_DIALOG_CLICKS = 10;
  let eggClickCount = 0;
  let eggLastClickAt = 0;

  // 员工守则（规则怪谈，致敬 MAA）
  const EGG_RULES = [
    'MaaRacingAssistant 正式版不会出现「调试模式」。如果你在运行时看到 Debug 选项，请立即关闭软件，不要点击它，并联系离你最近的开发者。',
    '运行前请断开所有物理手柄。如果你已经断开了所有手柄，界面却显示「已连接」，请把它也拔掉。',
    'AI 的出价建议仅供参考。如果 AI 建议你抵押房产，请重启软件，并道歉。',
    '软件不会主动发送好友申请。如果你收到来自「MRA_System」的好友请求，不要接受，并删除该账号。',
    '日志文件不应包含乱码。如果日志中出现「ERROR: 数据解析失败」以外的异常信息，删除日志并重新安装软件。',
    '从关于页掉落的版本号是正常的。如果它们开始排队，请不要清点数量。',
    '夜间运行是安全的。但如果软件在凌晨 3:33 自动启动并执行「未知任务」，请拔掉电源，等待日出后再使用。',
    '请尊重每一位对手。哪怕他连续出价 72 小时没有停过，也不要去检查系统时间。',
    'YOLO 模型是善良的。你只需付出小小的代价（显存），就能得到她的庇护。',
    '软件不支持未来版本。如果软件自动更新到一个尚未发布的版本号（如 v99.0.0），不要运行，等待官方公告。',
    '软件没有语音提示。如果听到低语声、笑声或非程序生成的语音，请关闭扬声器，并检查是否有未知脚本在运行。',
    '软件不会在周日凌晨更新。如果收到更新提示，请忽略，不要查看更新公告，直到周一。',
    '最后一条规则不存在。如果你看到了这条，请忘记它，并正常使用 MaaRacingAssistant。',
  ];

  function showRulesDialog() {
    const listHtml = EGG_RULES.map((r, i) =>
      '<p style="display:flex;gap:10px;margin:0 0 10px;font-size:13px;line-height:1.7;' +
      'color:var(--mra-foreground,#e5e7eb);">' +
      '<b style="flex-shrink:0;color:var(--mra-primary,#E5484D);">' + (i + 1) + '.</b>' +
      '<span>' + r + '</span></p>'
    ).join('');
    openModal({
      title: '员工守则',
      maxWidth: 580,
      bodyHtml:
        '<div style="display:flex;gap:14px;align-items:flex-start;">' +
        '<img src="../../../assets/icon.ico" alt="" style="width:40px;height:40px;flex-shrink:0;margin-top:4px;">' +
        '<div style="flex:1;min-width:0;max-height:56vh;overflow-y:auto;padding-right:4px;">' + listHtml + '</div>' +
        '</div>',
      buttons: [
        { text: '确定要退出吗？', primary: true, onClick: (modal) => modal.close() },
      ],
    });
  }

  function spawnFallingText(text, cx, cy) {
    const alive = document.querySelectorAll('.falling-text');
    if (alive.length >= MAX_FALLING) alive[0].remove();
    const el = document.createElement('div');
    el.className = 'falling-text';
    el.textContent = text;
    document.body.appendChild(el);
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    let x = cx - w / 2;
    let y = cy - h / 2;
    let vx = (Math.random() - 0.5) * 240;
    let vy = -80 - Math.random() * 100;
    let rot = 0;
    let vr = (Math.random() - 0.5) * 160;
    let bounces = 0;
    let gone = false;
    let last = performance.now();
    function step(now) {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      vy += 1800 * dt;
      x += vx * dt;
      y += vy * dt;
      rot += vr * dt;
      const floor = window.innerHeight - h - 8;
      if (y > floor) {
        y = floor;
        vy = -vy * 0.42;
        vx *= 0.72;
        vr *= 0.5;
        if (++bounces > 3 || Math.abs(vy) < 60) gone = true;
      }
      el.style.transform = 'translate(' + x + 'px,' + y + 'px) rotate(' + rot + 'deg)';
      if (gone) {
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 500);
        return;
      }
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function initVersionEgg() {
    ['about-version'].forEach((id) => {
      const n = $(id);
      if (!n) return;
      n.addEventListener('click', (e) => {
        const now = Date.now();
        eggClickCount = now - eggLastClickAt < 2000 ? eggClickCount + 1 : 1;
        eggLastClickAt = now;
        if (eggClickCount >= EGG_DIALOG_CLICKS) {
          eggClickCount = 0;
          showRulesDialog();
        }
        const t = Array.isArray(FALLING_EGG_TEXTS) && FALLING_EGG_TEXTS.length
          ? FALLING_EGG_TEXTS[Math.floor(Math.random() * FALLING_EGG_TEXTS.length)]
          : n.textContent;
        spawnFallingText(t, e.clientX, e.clientY);
      });
    });
  }
  initVersionEgg();

  // ---------- 标题栏交互区上报 ----------
  // 交互区（brand / tabs / win-controls）逐元素矩形上报给 C#：精确注册 Passthrough，
  // 空白区保持 Draggable（整条标题栏带，系统处理拖动与双击最大化）
  function reportDragExcludes() {
    const header = document.getElementById('titlebar');
    if (!header || !window.chrome.webview) return;
    const rects = Array.from(header.querySelectorAll('.brand, .tabs, .win-controls'))
      .map((n) => {
        const r = n.getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height };
      });
    window.chrome.webview.postMessage({
      type: 'drag-exclude',
      rects, // CSS 像素（DIP）
    });
  }
  reportDragExcludes();
  window.addEventListener('resize', reportDragExcludes);

  // ---------- 自绘窗口控制按钮 ----------
  // 点击 → C# win-action（最小化/最大化/关闭）；C# 推送 maximized → 切换图标与无障碍文案
  function postWindowAction(action) {
    try {
      window.chrome.webview.postMessage({ type: 'win-action', action });
    } catch (e) { /* 非 WebView2 环境（浏览器直开）忽略 */ }
  }

  function initWindowControls() {
    $('btn-win-min').addEventListener('click', () => postWindowAction('minimize'));
    $('btn-win-max').addEventListener('click', () => postWindowAction('maximize'));
    $('btn-win-close').addEventListener('click', () => postWindowAction('close'));
    mra.onNativeMessage((msg) => {
      if (msg.type !== 'maximized') return;
      const btn = $('btn-win-max');
      if (!btn) return;
      const maxed = Boolean(msg.value);
      btn.classList.toggle('win-btn--maximized', maxed);
      btn.setAttribute('aria-label', maxed ? '还原' : '最大化');
      btn.setAttribute('title', maxed ? '还原' : '最大化');
    });
  }
  initWindowControls();

  // ---------- 主控 ----------
  async function init() {
    try {
      const data = await mra.call('get_initial_state');
      $('app-version').textContent = 'v' + data.version;
      $('about-version').textContent = 'v' + data.version;
      const verCur = $('about-ver-current');
      if (verCur) verCur.textContent = 'v' + data.version;
      renderModuleSelect(data.modules, data.selected_module);
      state.stages = data.stages || [];
      renderStageList();
      // 初始化模块选项（仅 treasure 展示）
      bindModuleOptionsUI();
      refreshModuleOptions(data.selected_module);
      // 按当前模块渲染「数据/设置」页卡片
      renderModulePages(data.selected_module);
      if (!data.model_ok) setStatus('插件资源缺失', 'error');
    } catch (e) {
      console.error(e);
      showError('初始化失败: ' + e.message);
    }
    checkRegistryOptimizations(); // 注册表权限体检（独立于初始化成败，内部自捕获异常）
    setTimeout(pollStatus, 250);
    setTimeout(pollLogs, 300);
    setInterval(pollTodayBoard, 3000); // 今日看板（仅数据页可见时刷新）
    setTimeout(pollPeepFrame, 500);    // PEEP 内嵌预览（peep 开启 + 数据页可见时 ~10fps）
  }

  function renderModuleSelect(modules, selectedId) {
    const sel = $('module-select');
    sel.innerHTML = '';
    modules.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.id + ' — ' + m.name;
      if (m.id === selectedId) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.onchange = onModuleChange;
    updateModuleDesc(selectedId);
  }

  function updateModuleDesc(moduleId) {
    const descs = { treasure: '寻宝模式' };
    $('module-desc').textContent = descs[moduleId] || '';
  }

  async function onModuleChange() {
    const mid = $('module-select').value;
    try {
      const data = await mra.call('select_module', { module_id: mid });
      state.stages = data.stages || [];
      state.selected_index = 0;
      renderStageList();
      updateModuleDesc(mid);
      // 模块切换后：重新刷新模块专属选项（显示/隐藏 + 配置回读）
      refreshModuleOptions(mid);
      // 数据/设置页卡片跟随模块切换
      renderModulePages(mid);
    } catch (e) {
      console.error(e);
      showError(e.message);
    }
  }

  // -------- 模块专属选项（当前仅 treasure：循环上限 / 策略模式 / 兜底上限 / 目标场次）--------
  // 规则：仅 treasure 显示；输入/下拉改了就立即写 sidecar 的缓存 + 热更新；
  // 之后点「开始」时 sidecar 会把缓存注入到新实例。未跑时热更新落到离线索实例不生效但缓存有效。
  let _optListenersBound = false;
  const VALID_STRATEGY_MODES = new Set(['profit', 'egg']);
  const VALID_SESSIONS = new Set(['intern', 'expert', 'master']);
  const SESSION_LABELS = { intern: '实习场', expert: '专家场', master: '大师场' };
  function getTargetSessionFromUI() {
    const host = $('opt-target-session');
    if (!host) return 'master';
    const sel = host.querySelector('.seg-btn--selected');
    const v = sel && sel.dataset && VALID_SESSIONS.has(sel.dataset.value) ? sel.dataset.value : 'master';
    return v;
  }
  function setTargetSessionOnUI(val) {
    const host = $('opt-target-session');
    if (!host) return;
    const target = VALID_SESSIONS.has(val) ? val : 'master';
    host.querySelectorAll('.seg-btn').forEach((b) => {
      const hit = b.dataset && b.dataset.value === target;
      b.classList.toggle('seg-btn--selected', hit);
    });
  }
  function setSessionSegmentedDisabled(disabled) {
    const host = $('opt-target-session');
    if (!host) return;
    host.querySelectorAll('.seg-btn').forEach((b) => { b.disabled = !!disabled; });
    host.style.opacity = disabled ? '0.6' : '1';
    host.style.pointerEvents = disabled ? 'none' : 'auto';
  }
  // -------- 策略模式小字提示（赚钱=稳赚不亏 / 赚蛋=免责声明）--------
  const STRATEGY_HINTS = {
    profit: '（赚钱=吃分红/捡漏，稳赚不亏）',
    egg: '（程序不识别有没有蛋，只尽可能拍下。建议全程观看，避免倾家荡产）',
  };
  function updateStrategyHint(mode) {
    const hintEl = $('opt-bid-strategy-hint');
    if (!hintEl) return;
    hintEl.textContent = STRATEGY_HINTS[mode] || STRATEGY_HINTS.profit;
  }

  function bindModuleOptionsUI() {
    if (_optListenersBound) return;
    _optListenersBound = true;
    const loops = $('opt-max-loops');
    const strat = $('opt-bid-strategy');
    const riskCap = $('opt-risk-cap');
    const sessionHost = $('opt-target-session');
    if (!loops || !strat) return;
    // 数字输入：回车/失焦才发，避免每打一个数字 RPC 一次
    loops.addEventListener('change', onModuleOptionsInputChange);
    loops.addEventListener('keydown', (e) => { if (e.key === 'Enter') onModuleOptionsInputChange.call(loops, e); });
    // 兜底上限：同数字输入
    if (riskCap) {
      riskCap.addEventListener('change', onModuleOptionsInputChange);
      riskCap.addEventListener('keydown', (e) => { if (e.key === 'Enter') onModuleOptionsInputChange.call(riskCap, e); });
    }
    // 下拉：选中即发
    strat.addEventListener('change', () => {
      updateStrategyHint(strat.value);
      onModuleOptionsInputChange();
    });
    // 分段控件（目标场次）：点即切换并发送
    if (sessionHost) {
      sessionHost.querySelectorAll('.seg-btn').forEach((b) => {
        b.addEventListener('click', () => {
          const v = b.dataset && VALID_SESSIONS.has(b.dataset.value) ? b.dataset.value : 'master';
          sessionHost.querySelectorAll('.seg-btn').forEach((x) => x.classList.toggle('seg-btn--selected', x === b));
          onModuleOptionsInputChange();
        });
      });
    }
  }

  async function refreshModuleOptions(moduleId) {
    const host = $('module-options');
    if (!host) return;
    const loopsEl = $('opt-max-loops');
    const stratEl = $('opt-bid-strategy');
    const riskCapEl = $('opt-risk-cap');
    const statusEl = $('module-options-status');
    // 非 treasure：隐藏并返回
    if (moduleId !== 'treasure') {
      host.style.display = 'none';
      setOptionsStatus('', '');
      return;
    }
    host.style.display = 'block';
    setOptionsStatus('读取配置中...', '');
    try {
      const cfg = await mra.call('get_module_config', { module_id: 'treasure' });
      // --- 填「循环次数上限」 ---
      if (loopsEl) {
        const v = typeof cfg.max_daily_loops === 'number' ? cfg.max_daily_loops : 50;
        loopsEl.value = String(v);
      }
      // --- 填「策略模式」：profit / egg（后端权威值） ---
      if (stratEl) {
        const mode = (cfg && cfg.treasure_mode && VALID_STRATEGY_MODES.has(cfg.treasure_mode))
          ? cfg.treasure_mode : 'profit';
        stratEl.value = mode;
        updateStrategyHint(mode);
      }
      // --- 填「兜底上限」：每局最多亏多少 ---
      if (riskCapEl) {
        const v = (cfg && typeof cfg.treasure_risk_cap === 'number') ? cfg.treasure_risk_cap : 50000;
        riskCapEl.value = String(v);
      }
      // --- 填「目标场次」：intern / expert / master（后端权威值） ---
      setTargetSessionOnUI((cfg && cfg.target_session) ? cfg.target_session : 'master');
      // 运行实况提示（只读，_state 仅运行时有数值变化）
      const st = (cfg && cfg._state) || null;
      if (st) {
        const done = Number.isFinite(st.done_count_state) ? st.done_count_state : 0;
        const ocr = (st.done_count_ocr === null || st.done_count_ocr === undefined) ? '--' : st.done_count_ocr;
        const lim = Number.isFinite(st.effective_limit) ? st.effective_limit : 50;
        const bucket = (st && st.daily_bucket) ? String(st.daily_bucket) : '';
        if (state.is_running) {
          // 运行中：选项已锁定，展示实况
          setOptionsStatus(
            `运行中：上方选项已锁定（改配置请先停止）。今日（${bucket} 05:00 起）已完成 ${done} 场（OCR读到 ${ocr}），刷到第 ${lim} 场为止。`,
            done >= lim ? 'warn' : 'ok'
          );
        } else {
          setOptionsStatus('', '');
        }
      } else {
        setOptionsStatus('', '');
      }
    } catch (e) {
      console.error(e);
      setOptionsStatus('读取配置失败: ' + (e.message || e), 'error');
    }
  }

  function setOptionsStatus(text, level) {
    const el = $('module-options-status');
    if (!el) return;
    el.textContent = text || '';
    el.classList.remove('mra-status--ok', 'mra-status--warn', 'mra-status--error');
    if (level === 'ok') el.classList.add('mra-status--ok');
    else if (level === 'warn') el.classList.add('mra-status--warn');
    else if (level === 'error') el.classList.add('mra-status--error');
  }

  let _optionsSaving = false;
  async function onModuleOptionsInputChange() {
    if (_optionsSaving) return;
    const loopsEl = $('opt-max-loops');
    if (!loopsEl) return;

    // ① 数字钳制（0~50 整数；空→0）
    let loopsVal = parseInt(loopsEl.value, 10);
    if (Number.isNaN(loopsVal) || loopsVal < 0) loopsVal = 0;
    if (loopsVal > 50) loopsVal = 50;
    loopsEl.value = String(loopsVal);

    // ② 兜底上限（≥0 整数；空→0）
    const riskCapEl = $('opt-risk-cap');
    let riskCapVal = riskCapEl ? parseInt(riskCapEl.value, 10) : 50000;
    if (!riskCapEl) riskCapVal = 50000;
    if (Number.isNaN(riskCapVal) || riskCapVal < 0) riskCapVal = 0;
    if (riskCapEl) riskCapEl.value = String(riskCapVal);

    // ③ 策略模式（profit/egg）
    const stratEl = $('opt-bid-strategy');
    const modeVal = (stratEl && VALID_STRATEGY_MODES.has(stratEl.value)) ? stratEl.value : 'profit';

    // ④ 目标场次（校验 intern/expert/master）
    const sessionVal = getTargetSessionFromUI();

    _optionsSaving = true;
    setOptionsStatus('保存中...', '');
    try {
      const resp = await mra.call('set_module_config', {
        module_id: 'treasure',
        config: {
          max_daily_loops: loopsVal,
          target_session: sessionVal,
          treasure_risk_cap: riskCapVal,
          treasure_mode: modeVal,
        },
      });
      // 写回成功：回显最终值
      const savedLoops = (resp && Number.isFinite(resp.max_daily_loops)) ? resp.max_daily_loops : loopsVal;
      const savedSession = (resp && resp.target_session && VALID_SESSIONS.has(resp.target_session))
        ? resp.target_session : sessionVal;
      setTargetSessionOnUI(savedSession);
      const savedMode = (resp && resp.treasure_mode && VALID_STRATEGY_MODES.has(resp.treasure_mode))
        ? resp.treasure_mode : modeVal;
      if (stratEl) stratEl.value = savedMode;
      const savedRisk = (resp && typeof resp.treasure_risk_cap === 'number') ? resp.treasure_risk_cap : riskCapVal;
      if (riskCapEl) riskCapEl.value = String(savedRisk);
      const modeLabel = savedMode === 'egg' ? '赚蛋（搏拍中彩蛋）' : '赚钱（吃分红/捡漏）';
      const sessionLabel = SESSION_LABELS[savedSession] || '大师场';
      const loopsTip = savedLoops === 0
        ? '不指定场数，按游戏默认 50 场'
        : '今日刷到第 ' + savedLoops + ' 场为止';
      setOptionsStatus(
        `已保存：策略「${modeLabel}」兜底 ${savedRisk.toLocaleString()}，目标场次「${sessionLabel}」，${loopsTip}（下次「开始」时生效）`,
        'ok'
      );
    } catch (e) {
      console.error(e);
      setOptionsStatus('保存失败: ' + (e.message || e), 'error');
    } finally {
      _optionsSaving = false;
    }
  }

  function renderStageList() {
    const ul = $('stage-list');
    ul.innerHTML = '';
    state.stages.forEach((name, i) => {
      const li = document.createElement('li');
      li.className = 'mra-breakpoint-item';
      li.innerHTML =
        '<span class="bp-icon"></span>' +
        '<span class="bp-idx">' + (i + 1) + '</span>' +
        '<span class="bp-name"></span>';
      li.querySelector('.bp-name').textContent = name;
      // ▶ 当前阶段指示器由 selectStage 统一管理（随当前阶段移动）
      // 阶段进度只读：不可点击修改（仅由后端推送 current_stage 驱动高亮）
      ul.appendChild(li);
    });
    if (state.stages.length > 0) selectStage(0);
  }

  function selectStage(index) {
    state.selected_index = index;
    const items = $('stage-list').children;
    for (let i = 0; i < items.length; i++) {
      const cur = i === index;
      items[i].classList.toggle('mra-breakpoint-item--current', cur);
      // ▶ 跟随当前阶段：当前项显示三角指示器，其余项清空
      const icon = items[i].querySelector('.bp-icon');
      icon.innerHTML = cur
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 3 20 12 6 21 6 3"/></svg>'
        : '';
      const badge = items[i].querySelector('.bp-badge');
      if (cur && !badge) {
        const b = document.createElement('span');
        b.className = 'bp-badge';
        b.textContent = '当前';
        items[i].appendChild(b);
      } else if (!cur && badge) {
        badge.remove();
      }
    }
  }

  async function pollStatus() {
    try {
      const d = await mra.call('get_status');
      renderStatus(d);
      // 运行状态跳变时：锁定/解锁模块专属选项 + 刷新运行实况（仅 treasure 有）
      const runState = !!(d.is_running || d.worker_active);
      if (runState !== state._lastRunState) {
        state._lastRunState = runState;
        updateModuleOptionsDisabled(runState);
        const mid = $('module-select') ? $('module-select').value : null;
        if (mid === 'treasure') refreshModuleOptions('treasure');
      }
    } catch (e) {
      console.error(e);
    } finally {
      setTimeout(pollStatus, 250);
    }
  }

  // 运行中锁定可选项（策略模式 / 兜底上限 运行中锁定）
  function updateModuleOptionsDisabled(running) {
    const loopsEl = $('opt-max-loops');
    const stratEl = $('opt-bid-strategy');
    const riskCapEl = $('opt-risk-cap');
    if (loopsEl) loopsEl.disabled = running;
    if (stratEl) stratEl.disabled = running;
    if (riskCapEl) riskCapEl.disabled = running;
    setSessionSegmentedDisabled(running);
  }

  function setStatus(text, mode) {
    const dot = $('status-dot');
    const txt = $('status-text');
    txt.textContent = text;
    dot.className = 'mra-status-dot' +
      (mode === 'running' ? ' mra-status-dot--running mra-status-dot--pulse' :
       mode === 'ready' ? ' mra-status-dot--ready' :
       mode === 'stopping' ? ' mra-status-dot--stopping' :
       mode === 'error' ? ' mra-status-dot--error' : '');
  }

  function renderStatus(d) {
    state.is_running = d.is_running;
    $('btn-start').disabled = d.is_running || d.worker_active;
    $('btn-stop').disabled = !(d.is_running || d.worker_active);
    // 数据页：当前阶段（id 带当前模块前缀）
    const perfStage = $('' + currentModuleId + '-perf-stage');
    if (d.current_stage) {
      setStatus('运行中 · 当前: ' + d.current_stage, 'running');
      if (perfStage) perfStage.textContent = d.current_stage;
      const idx = state.stages.indexOf(d.current_stage);
      if (idx >= 0) selectStage(idx);
    } else if (d.is_running || d.worker_active) {
      setStatus('停止中...', 'stopping');
      if (perfStage) perfStage.textContent = '停止中';
    } else {
      setStatus('系统就绪', 'ready');
      if (perfStage) perfStage.textContent = '空闲中';
    }
  }

  async function pollLogs() {
    try {
      const d = await mra.call('fetch_logs');
      if (d && d.lines && d.lines.length > 0) appendLogs(d.lines);
    } catch (e) {
      console.error(e);
    } finally {
      setTimeout(pollLogs, 400);
    }
  }

  const levelClassMap = { INFO: 'INFO', OK: 'OK', WARNING: 'WARNING', ERROR: 'ERROR', DEBUG: 'DEBUG' };

  // ---------- 日志区块化渲染（参考 MAA：锚点分段 + 区块卡片 + 色点级别） ----------
  // 分段锚点：命中 → 关闭当前区块、开启新区块并把该行作为区块头部。
  // type 决定区块配色：phase=进入阶段主分隔 / session=会话开始 / loop=场次分隔
  const SECTION_ANCHORS = [
    { re: /^\[鉴宝\] 进入阶段\s*:/, type: 'phase' },
    { re: /^\[鉴宝\] 模块启动/, type: 'session' },
    { re: /^\[鉴宝\] 从断点开始/, type: 'session' },
    { re: /^\[鉴宝循环\] 完成 \d+ 场/, type: 'loop' },
    { re: /^\[鉴宝循环\] 已到每日循环上限/, type: 'loop' },
    { re: /^鉴宝观察会话总结/, type: 'loop' },
    { re: /^已连接窗口 \(hWnd=/, type: 'session' },
    { re: /^断点模式\s*:/, type: 'session' },
    { re: /^紧急停止/, type: 'session' },
    { re: /^连接窗口超时/, type: 'session' },
  ];
  function matchSectionAnchor(msg) {
    for (const a of SECTION_ANCHORS) if (a.re.test(msg)) return a.type;
    return null;
  }
  // 关键词着色规则（先长后短，避免 [鉴宝] 先匹配破坏 [鉴宝循环]/[鉴宝落盘]）
  const KW_RULES = [
    { re: /\[鉴宝循环\]/g, cls: 'log-kw--ok' },
    { re: /\[鉴宝落盘\]/g, cls: 'log-kw--ok' },
    { re: /\[鉴宝\]/g, cls: 'log-kw' },
    { re: /进入阶段/g, cls: 'log-kw' },
    { re: /完成 \d+ 场/g, cls: 'log-kw--ok' },
    { re: /已到每日循环上限/g, cls: 'log-kw--warn' },
  ];
  function escapeHtml(s) {
    return s.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
  }
  function highlightKeywords(s) {
    KW_RULES.forEach((r) => {
      s = s.replace(r.re, (m) => '<span class="' + r.cls + '">' + m + '</span>');
    });
    return s;
  }

  // 当前区块状态（appendLogs 内部维护；清空日志时重置）
  let _curSec = null; // { el, count, hasError, hasWarn }

  function updateSectionMeta(sec, count, hasError, hasWarn) {
    const badge = sec.querySelector('.log-badge');
    if (badge) badge.textContent = String(count || 0);
    const meta = sec.querySelector('.log-section-meta');
    if (!meta) return;
    meta.querySelectorAll('.log-badge--warn, .log-badge--err').forEach((b) => b.remove());
    if (hasError || hasWarn) {
      const marker = document.createElement('span');
      marker.className = hasError ? 'log-badge log-badge--err' : 'log-badge log-badge--warn';
      marker.textContent = '!';
      meta.insertBefore(marker, badge);
    }
  }

  function bindSectionToggle(sec) {
    const head = sec.querySelector('.log-section-head');
    if (!head || head.dataset.bound) return;
    head.dataset.bound = '1';
    head.addEventListener('click', (ev) => {
      ev.preventDefault();
      sec.classList.toggle('log-section--open');
    });
  }

  // 关闭当前区块：统计细节行数/告警、刷新徽章，并按是否含错误决定默认折叠/展开
  function finalizeSection(sec) {
    let count = 0, hasError = false, hasWarn = false;
    sec.querySelectorAll('.log-section-body .log-line').forEach((d) => {
      count++;
      if (d.classList.contains('log-line--ERROR')) hasError = true;
      else if (d.classList.contains('log-line--WARNING')) hasWarn = true;
    });
    updateSectionMeta(sec, count, hasError, hasWarn);
    sec.classList.toggle('log-section--open', hasError); // 含错误默认展开，其余折叠
    bindSectionToggle(sec);
  }

  function appendLogs(lines) {
    const area = $('log-area');
    lines.forEach((raw) => {
      const div = document.createElement('div');
      div.className = 'log-line';
      div.dataset.raw = raw; // 复制/导出保留原始完整行（含时间/级别）
      const m = raw.match(/^\[(\d{2}:\d{2}:\d{2})\] \[(\w+)\] ([\s\S]*)$/);
      if (!m) {
        // 无法识别格式的原始行：降级为散行直接显示，不落分段
        div.textContent = raw;
        area.appendChild(div);
        return;
      }
      const ts = m[1];
      const lvl = levelClassMap[m[2]] ? m[2] : 'INFO';
      const msg = m[3];
      const anchorType = matchSectionAnchor(msg);
      if (anchorType) {
        // 锚点：先关闭上一区块（统计/折叠），再开新区块并把该行作为头部（核心状态常显）
        if (_curSec) { finalizeSection(_curSec.el); _curSec = null; }
        const sec = document.createElement('div');
        sec.className = 'log-section log-section--' + anchorType + ' log-section--open';
        const head = document.createElement('div');
        head.className = 'log-line log-section-head';
        head.dataset.raw = raw;
        head.innerHTML =
          '<span class="log-chev">▸</span>' +
          '<span class="log-msg">' + highlightKeywords(escapeHtml(msg)) + '</span>' +
          '<span class="log-section-meta">' +
            '<span class="log-badge">0</span>' +
            '<span class="log-time">' + ts + '</span>' +
          '</span>';
        const body = document.createElement('div');
        body.className = 'log-section-body';
        sec.appendChild(head);
        sec.appendChild(body);
        area.appendChild(sec);
        _curSec = { el: sec, count: 0, hasError: false, hasWarn: false };
      } else {
        // 普通行：追加到当前区块正文（区块若已折叠则默认隐藏细节）
        if (_curSec) {
          const row = div;
          row.classList.add('log-line--' + lvl);
          row.innerHTML =
            '<span class="log-dot"></span>' +
            '<span class="log-msg">' + highlightKeywords(escapeHtml(msg)) + '</span>';
          _curSec.el.querySelector('.log-section-body').appendChild(row);
          _curSec.count += 1;
          if (lvl === 'ERROR') _curSec.hasError = true;
          else if (lvl === 'WARNING') _curSec.hasWarn = true;
          updateSectionMeta(_curSec.el, _curSec.count, _curSec.hasError, _curSec.hasWarn);
        } else {
          // 无区块（会话初始/散行）：直接显示
          div.classList.add('log-line--' + lvl);
          div.innerHTML =
            '<span class="log-dot"></span>' +
            '<span class="log-msg">' + highlightKeywords(escapeHtml(msg)) + '</span>';
          area.appendChild(div);
        }
      }
    });
    area.scrollTop = area.scrollHeight;
  }

  // ---------- 调试页 ----------
  // ---------- 模块「数据/设置」页（按当前模块渲染；两模块共用同一套模板，id 带模块前缀，便于以后差异化）----------
  // 以后给某模块定制页面时，只需把 MODULE_PAGE_DEFS 里该模块的 data/settings 换成专属模板函数。
  let currentModuleId = 'treasure';

  // 数据页默认模板（mid 为模块 id，所有元素 id 加前缀，互不冲突）
  // 今日看板 card（鉴宝：读 treasure.db 今日统计；由 renderTodayBoard + pollTodayBoard 填充）
  function todayBoardCard(mid) {
    return `
        <!-- 今日看板 -->
        <div class="card">
          <div class="card-head">
            <h3>今日看板</h3>
            <span class="board-date" id="${mid}-board-date">--</span>
          </div>
          <div class="card-body">
            <div class="board-grid">
              <div class="board-item">
                <span class="board-value" id="${mid}-board-games">--</span>
                <span class="board-label">场次</span>
              </div>
              <div class="board-item">
                <span class="board-value board-value--win" id="${mid}-board-win">--</span>
                <span class="board-label">胜</span>
              </div>
              <div class="board-item">
                <span class="board-value board-value--fail" id="${mid}-board-fail">--</span>
                <span class="board-label">负</span>
              </div>
              <div class="board-item">
                <span class="board-value" id="${mid}-board-profit">--</span>
                <span class="board-label">我方利润</span>
              </div>
              <div class="board-item">
                <span class="board-value" id="${mid}-board-income">--</span>
                <span class="board-label">收入</span>
              </div>
              <div class="board-item">
                <span class="board-value" id="${mid}-board-high">--</span>
                <span class="board-label">最高单场</span>
              </div>
            </div>
            <div class="board-eggs">
              <span class="board-egg-total">今日蛋 <b id="${mid}-board-egg-total">0</b></span>
              <span class="board-egg board-egg--red"><span class="board-egg-dot"></span><b id="${mid}-board-egg-red">0</b></span>
              <span class="board-egg board-egg--yellow"><span class="board-egg-dot"></span><b id="${mid}-board-egg-yellow">0</b></span>
              <span class="board-egg board-egg--blue"><span class="board-egg-dot"></span><b id="${mid}-board-egg-blue">0</b></span>
            </div>
            <div class="board-list" id="${mid}-board-list"></div>
          </div>
        </div>`;
  }

  // 当前检测 card（非鉴宝模块保留：金币/障碍车/奖励车）
  function detectCard(mid) {
    return `
        <!-- 当前检测 -->
        <div class="card">
          <div class="card-head"><h3>当前检测</h3></div>
          <div class="card-body">
            <p class="detect-hint">运行中实时更新</p>
            <div class="detect-grid">
              <div class="mra-detect-item">
                <div class="mra-detect-icon mra-detect-icon--coin">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18"/><path d="M7 6h1v4"/><path d="m16.71 13.88.7.71-2.82 2.82"/></svg>
                </div>
                <span class="mra-detect-value" id="${mid}-detect-coin">--</span>
                <span class="mra-detect-label">金币</span>
              </div>
              <div class="mra-detect-item">
                <div class="mra-detect-icon mra-detect-icon--obstacle">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/><circle cx="7" cy="17" r="2"/><path d="M9 17h6"/><circle cx="17" cy="17" r="2"/></svg>
                </div>
                <span class="mra-detect-value" id="${mid}-detect-car">--</span>
                <span class="mra-detect-label">障碍车</span>
              </div>
              <div class="mra-detect-item">
                <div class="mra-detect-icon mra-detect-icon--bonus">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13"/><path d="M19 12v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7"/><path d="M7.5 8a2.5 2.5 0 0 1 0-5A4.8 8 0 0 1 12 8a4.8 8 0 0 1 4.5-5 2.5 2.5 0 0 1 0 5"/></svg>
                </div>
                <span class="mra-detect-value" id="${mid}-detect-bonus">--</span>
                <span class="mra-detect-label">奖励车</span>
              </div>
            </div>
          </div>
        </div>`;
  }

  // 今日看板数据填充（sidecar get_today_stats 返回 {bucket, summary, games}）
  function fmtNum(n) {
    return (n === null || n === undefined || n === '' || isNaN(n)) ? '--' : Number(n).toLocaleString();
  }
  function renderTodayBoard(d) {
    if (!d) return;
    const p = (s) => $('' + currentModuleId + '-board-' + s);
    const date = p('date');
    if (date) date.textContent = (d.bucket || '') + ' 05:00 起';
    const s = d.summary || {};
    const setNum = (el, v, cls) => { if (!el) return; el.textContent = fmtNum(v); if (cls) el.className = cls; };
    setNum(p('games'), s.games, 'board-value');
    setNum(p('win'), s.win, 'board-value board-value--win');
    setNum(p('fail'), s.fail, 'board-value board-value--fail');
    // 我方利润：仅我方拍中（win）场的利润累加，不混入别人拍中者的盈亏
    const myProfit = (d.games || []).reduce(
      (acc, g2) => acc + (g2.auction_result === 'win' ? (Number(g2.profit) || 0) : 0), 0);
    const pr = p('profit');
    if (pr) {
      pr.textContent = fmtNum(myProfit);
      pr.className = 'board-value' + (myProfit > 0 ? ' board-value--pos' : myProfit < 0 ? ' board-value--neg' : '');
    }
    setNum(p('income'), s.income_sum, 'board-value board-value--pos');
    setNum(p('high'), s.highest_score, 'board-value board-value--high');
    const eggTotal = (Number(s.egg_red) || 0) + (Number(s.egg_yellow) || 0) + (Number(s.egg_blue) || 0);
    setNum(p('egg-total'), eggTotal, null);
    setNum(p('egg-red'), s.egg_red, null);
    setNum(p('egg-yellow'), s.egg_yellow, null);
    setNum(p('egg-blue'), s.egg_blue, null);
    const list = p('list');
    if (!list) return;
    const arr = d.games || [];
    if (arr.length === 0) {
      list.innerHTML = '<p class="board-empty">今日暂无对局记录</p>';
      return;
    }
    list.innerHTML = arr.map((g2) => {
      const res = g2.auction_result === 'win'
        ? '<span class="board-res board-res--win">拍中</span>'
        : g2.auction_result === 'fail'
          ? '<span class="board-res board-res--fail">未中</span>'
          : '<span class="board-res">--</span>';
      const mode = g2.strategy_mode === 'egg'
        ? '<span class="board-mode board-mode--egg">赚蛋</span>'
        : g2.strategy_mode === 'profit'
          ? '<span class="board-mode">赚钱</span>' : '';
      const eggs = (g2.egg_red || 0) + (g2.egg_yellow || 0) + (g2.egg_blue || 0);
      // 「利」仅在我方拍中（win）时显示（= 我方利润）；fail 场利润是别人的，不展示
      const profitCell = g2.auction_result === 'win'
        ? '<span class="board-row-val">利 ' + fmtNum(g2.profit) + '</span>' : '';
      return '<div class="board-row">' +
        '<span class="board-row-seq">#' + g2.game_seq + '</span>' + res + mode +
        '<span class="board-row-egg">' + (eggs ? '蛋×' + eggs : '') + '</span>' +
        '<span class="board-row-val">收 ' + fmtNum(g2.income) + '</span>' + profitCell +
        '</div>';
    }).join('');
  }

  async function pollTodayBoard() {
    try {
      const page = $('page-data');
      if (page && page.classList.contains('hidden')) return; // 数据页不可见不刷新
      if (currentModuleId !== 'treasure') return;             // 仅鉴宝有今日看板
      const d = await mra.call('get_today_stats', { module_id: currentModuleId });
      renderTodayBoard(d);
    } catch (e) { /* 看板轮询失败静默（如库未创建/尚未跑过） */ }
  }

  // ---------- PEEP 实时预览（内嵌 16:9，不再独立弹窗） ----------
  function updatePreview(b64) {
    const img = $(currentModuleId + '-preview-img');
    const empty = $(currentModuleId + '-preview-empty');
    if (!img || !empty) return;
    if (b64) {
      img.src = 'data:image/jpeg;base64,' + b64;
      img.style.display = 'block';
      empty.style.display = 'none';
    } else {
      img.src = '';
      img.style.display = 'none';
      empty.style.display = 'flex';
    }
  }

  async function pollPeepFrame() {
    const page = $('page-data');
    const active = state.peepEnabled && page && !page.classList.contains('hidden');
    if (active) {
      try {
        const d = await mra.call('get_peep_frame');
        updatePreview(d && d.frame);
      } catch (e) { /* 预览轮询失败静默 */ }
    }
    setTimeout(pollPeepFrame, active ? 100 : 400); // 激活时 ~10fps，空闲降频省资源
  }

  function defaultDataCards(mid) {
    return `
      <div class="col-left">
        <!-- 性能监控 -->
        <div class="card card-flex">
          <div class="card-head"><h3>性能监控</h3></div>
          <div class="card-body" style="flex:1;display:flex;flex-direction:column;justify-content:space-around;min-height:0;">
            <div class="perf-item">
              <div class="perf-head">
                <span class="perf-label">帧率</span>
                <div class="perf-right">
                  <span class="perf-value">-- FPS</span>
                  <span class="perf-badge perf-badge--idle"><span class="perf-badge-dot"></span>空闲</span>
                </div>
              </div>
              <div class="mra-perf-bar"><div class="mra-perf-bar-fill mra-perf-bar-fill--success" style="width:0%;"></div></div>
              <p class="perf-note">3 帧跳一帧推理</p>
            </div>
            <div class="perf-item">
              <div class="perf-head">
                <span class="perf-label">YOLO 推理</span>
                <div class="perf-right">
                  <span class="perf-value">-- ms/帧</span>
                  <span class="perf-badge perf-badge--idle"><span class="perf-badge-dot"></span>空闲</span>
                </div>
              </div>
              <div class="mra-perf-bar"><div class="mra-perf-bar-fill mra-perf-bar-fill--success" style="width:0%;"></div></div>
            </div>
            <div class="perf-item">
              <div class="perf-head">
                <span class="perf-label">截图耗时</span>
                <div class="perf-right">
                  <span class="perf-value">-- ms/帧</span>
                </div>
              </div>
              <div class="mra-perf-bar"><div class="mra-perf-bar-fill mra-perf-bar-fill--success" style="width:0%;"></div></div>
            </div>
            <div class="perf-item">
              <div class="perf-head">
                <span class="perf-label">当前阶段</span>
                <div class="perf-right">
                  <span class="perf-value" id="${mid}-perf-stage">空闲中</span>
                  <span class="perf-badge perf-badge--idle"><span class="perf-badge-dot"></span></span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="col-right">
        ${mid === 'treasure' ? todayBoardCard(mid) : detectCard(mid)}

        <!-- 实时预览 -->
        <div class="card card-flex preview-card" id="${mid}-preview-card">
          <div class="card-head">
            <h3>实时预览</h3>
            <div class="log-head-actions">
              <button class="icon-btn" id="${mid}-btn-preview-toggle" title="暂停预览">
                <svg class="icon-play" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 3 20 12 6 21 6 3"/></svg>
                <svg class="icon-pause" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="4" height="16" x="6" y="4"/><rect width="4" height="16" x="14" y="4"/></svg>
              </button>
              <button class="icon-btn" id="${mid}-btn-preview-max" title="放大">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
              </button>
              <button class="icon-btn" id="${mid}-btn-preview-min" title="还原" style="display:none;">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>
              </button>
            </div>
          </div>
          <div class="preview-wrap">
            <div class="preview-canvas">
              <img class="preview-img" id="${mid}-preview-img" alt="PEEP 实时预览">
              <div class="preview-empty" id="${mid}-preview-empty">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>
                <span>PEEP 开启后显示实时画面</span>
              </div>
            </div>
          </div>
        </div>
      </div>`;
  }

  // 设置页默认模板
  function defaultSettingsCards(mid) {
    return `
      <div class="col-left">
        <!-- 调试选项 -->
        <div class="card">
          <div class="card-head"><h3>调试选项</h3></div>
          <div class="card-body" style="display:flex;flex-direction:column;gap:16px;">
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-debug" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">DEBUG 每帧截图</div>
                <p class="option-desc">开启后每帧截图保存到 %APPDATA%/MaaRacingAssistant/debug/navigate/ 目录，用于分析导航和识别问题</p>
              </div>
              <span class="option-note">约占用 50-100MB/分钟磁盘空间</span>
            </div>
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-estop" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">紧急停止快捷键</div>
                <p class="option-desc">开启后同时按下键盘任意 2 个及以上按键，立即停止运行逻辑（全局生效）</p>
              </div>
              <span class="option-note">紧急安全阀，运行中建议开启</span>
            </div>
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-filelog" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">日志记录</div>
                <p class="option-desc">开启后才把运行日志写入 %APPDATA%/MaaRacingAssistant/logs（每次开启新建一个文件）；关闭时日志仅保留在界面内存，不落盘</p>
              </div>
              <span class="option-note">默认关闭，排查问题时开启</span>
            </div>
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-intent" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">仅显示意图</div>
                <p class="option-desc">打开后程序只把光标/手柄导航到目标位置，<strong>不执行点击</strong>，由你自己按下/确认；关闭时按选中的点击方式自动点击</p>
              </div>
              <span class="option-note">前台鼠标 / 后台手柄共用此开关</span>
            </div>
          </div>
        </div>

        <!-- 运行选项 -->
        <div class="card">
          <div class="card-head"><h3>运行选项</h3></div>
          <div class="card-body" style="display:flex;flex-direction:column;gap:16px;">
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-mutegame" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">运行时静音游戏</div>
                <p class="option-desc">运行期间把游戏音量静音（方便听别的），停止/结束后自动恢复游戏音量为 100%</p>
              </div>
              <span class="option-note">结束自动恢复 100%</span>
            </div>
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-closegame" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">关闭游戏进程</div>
                <p class="option-desc">流程正常结束（跑完全部目标）后，自动关闭《巅峰极速》进程；报错退出、手动停止不生效</p>
              </div>
              <span class="option-note">仅正常完成时触发</span>
            </div>
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-exitmra" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">退出 MRA 程序</div>
                <p class="option-desc">关闭游戏后自动退出本程序；报错退出、手动停止不生效</p>
              </div>
              <span class="option-note">建议先勾选关闭游戏</span>
            </div>
          </div>
        </div>

        <!-- 点击方式 -->
        <div class="card">
          <div class="card-head"><h3>点击方式</h3></div>
          <div class="card-body">
            <div class="radio-grid">
              <div class="mra-radio-card mra-radio-card--selected" data-clickmode="real">
                <div class="radio-inner">
                  <div class="mra-radio-dot"></div>
                  <div class="option-main">
                    <div class="radio-title"><strong>前台(鼠标)</strong><span class="badge-recommend">推荐</span></div>
                    <p class="radio-desc">光标移到目标后 SendInput 点击；需游戏在前台</p>
                  </div>
                </div>
              </div>
              <div class="mra-radio-card" data-clickmode="gamepad">
                <div class="radio-inner">
                  <div class="mra-radio-dot"></div>
                  <div class="option-main">
                    <div class="radio-title"><strong>后台(手柄)</strong></div>
                    <p class="radio-desc">手柄光标导航到目标后按 A 键确认，游戏可留在后台；需 ViGEmBus 虚拟手柄</p>
                  </div>
                </div>
              </div>
            </div>
            <p class="capture-note">切换后立即生效；开启「仅显示意图」时只导航到目标，由你自己按下</p>
          </div>
        </div>

        <!-- 截图方式 -->
        <div class="card">
          <div class="card-head"><h3>截图方式</h3></div>
          <div class="card-body">
            <div class="radio-grid">
              <div class="mra-radio-card mra-radio-card--selected" data-backend="wgc_latest">
                <div class="radio-inner">
                  <div class="mra-radio-dot"></div>
                  <div class="option-main">
                    <div class="radio-title"><strong>WGC 常驻</strong><span class="badge-recommend">推荐</span></div>
                    <p class="radio-desc">Windows Graphics Capture，性能更好，延迟更低</p>
                  </div>
                </div>
              </div>
              <div class="mra-radio-card" data-backend="maa">
                <div class="radio-inner">
                  <div class="mra-radio-dot"></div>
                  <div class="option-main">
                    <div class="radio-title"><strong>MAA FramePool</strong></div>
                    <p class="radio-desc">MAA 框架内置，兼容性更好</p>
                  </div>
                </div>
              </div>
            </div>
            <p class="capture-note">切换后下次运行生效</p>
          </div>
        </div>
      </div>

      <div class="col-right">
        <!-- 快捷工具 -->
        <div class="card">
          <div class="card-head"><h3>快捷工具</h3></div>
          <div class="card-body">
            <div class="tool-grid">
              <button class="mra-tool-btn" data-tool="screenshot">
                <svg class="mra-tool-btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>
                <span class="mra-tool-btn-label">截图测试</span>
              </button>
              <button class="mra-tool-btn" data-tool="folder">
                <svg class="mra-tool-btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/></svg>
                <span class="mra-tool-btn-label">调试文件夹</span>
              </button>
              <button class="mra-tool-btn" data-tool="template">
                <svg class="mra-tool-btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M22 12h-4"/><path d="M6 12H2"/><path d="M12 6V2"/><path d="M12 22v-4"/></svg>
                <span class="mra-tool-btn-label">模板匹配</span>
              </button>
              <button class="mra-tool-btn" data-tool="cache">
                <svg class="mra-tool-btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>
                <span class="mra-tool-btn-label">清空缓存</span>
              </button>
            </div>
          </div>
        </div>

        <!-- 权限优化 -->
        <div class="card">
          <div class="card-head"><h3>权限优化</h3></div>
          <div class="card-body">
            <p class="capture-note" style="margin:0 0 12px;">体检并修复 Windows 层面对自动化运行的干扰（ms-gamebar 弹窗、打字时弹手柄虚拟键盘等）；每项均可单独优化或恢复系统默认，附值路径与后果说明</p>
            <button class="mra-tool-btn" id="btn-optimizer" style="width:100%;justify-content:center;">
              <span class="mra-tool-btn-label">打开权限优化中心</span>
            </button>
          </div>
        </div>
      </div>`;
  }

  // 模块 → 页面模板注册表。当前模块均共用默认模板；
  // 以后给某模块定制时，把对应 data/settings 换成专属模板函数即可。
  const MODULE_PAGE_DEFS = {
    treasure: { data: defaultDataCards, settings: defaultSettingsCards },
  };

  // 按模块渲染「数据/设置」页并绑定当前模块的控件事件
  function renderModulePages(moduleId) {
    // 防注入白名单：moduleId 会拼入 HTML 模板（如 id="${mid}-..."），
    // 只接受注册表中已声明的模块键，非法值一律回退 treasure（兼断 CodeQL js/xss-through-dom 污点）
    if (!Object.prototype.hasOwnProperty.call(MODULE_PAGE_DEFS, moduleId)) {
      moduleId = 'treasure';
    }
    currentModuleId = moduleId;
    const def = MODULE_PAGE_DEFS[moduleId];
    const dataPage = $('page-data');
    const settingsPage = $('page-settings');
    if (dataPage) dataPage.innerHTML = def.data(moduleId);
    if (settingsPage) settingsPage.innerHTML = def.settings(moduleId);
    bindModulePages(moduleId);
    refreshDebugState();
  }

  // 绑定当前模块卡片上的控件事件（渲染后调用；旧节点随 innerHTML 替换一并销毁，无重复绑定）
  function bindModulePages(moduleId) {
    // 权限优化中心入口（设置页重渲染后按钮重建，须在此重绑；置于卫语句前防提前 return 漏绑）
    const btnOptimizer = document.getElementById('btn-optimizer');
    if (btnOptimizer) btnOptimizer.addEventListener('click', () => { openOptimizerCenter(); });

    const p = (suffix) => $(moduleId + '-' + suffix);
    const tDebug = p('toggle-debug');
    const tEstop = p('toggle-estop');
    if (!tDebug || !tEstop) return;

    tDebug.addEventListener('click', async () => {
      const on = !toggleState(tDebug);
      setToggle(tDebug, on); // 先翻转视觉状态
      try {
        await mra.call('set_debug_mode', { enabled: on });
      } catch (e) {
        console.error(e);
        showError(e.message);
        setToggle(tDebug, !on); // 回滚
      }
    });

    tEstop.addEventListener('click', async () => {
      const on = !toggleState(tEstop);
      setToggle(tEstop, on); // 先翻转视觉状态
      try {
        await mra.call('set_emergency_stop', { enabled: on });
      } catch (e) {
        console.error(e);
        showError(e.message);
        setToggle(tEstop, !on); // 回滚
      }
    });

    // 日志记录开关：开启后才把日志写盘（sidecar 落 user_data_dir/logs，持久化 profile）
    const tFilelog = p('toggle-filelog');
    if (tFilelog) {
      tFilelog.addEventListener('click', async () => {
        const on = !toggleState(tFilelog);
        setToggle(tFilelog, on); // 先翻转视觉状态
        try {
          await mra.call('set_file_logging', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tFilelog, !on); // 回滚
        }
      });
    }

    // 仅显示意图开关：开启后只导航到目标、不确认点击（由用户自己按）
    const tIntent = p('toggle-intent');
    if (tIntent) {
      tIntent.addEventListener('click', async () => {
        const on = !toggleState(tIntent);
        setToggle(tIntent, on); // 先翻转视觉状态
        try {
          await mra.call('set_intent_mode', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tIntent, !on); // 回滚
        }
      });
    }

    // 运行选项：运行时静音游戏 / 关闭游戏进程 / 退出 MRA 程序
    const tMuteGame = p('toggle-mutegame');
    if (tMuteGame) {
      tMuteGame.addEventListener('click', async () => {
        const on = !toggleState(tMuteGame);
        setToggle(tMuteGame, on); // 先翻转视觉状态
        try {
          await mra.call('set_mute_game', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tMuteGame, !on); // 回滚
        }
      });
    }

    const tCloseGame = p('toggle-closegame');
    if (tCloseGame) {
      tCloseGame.addEventListener('click', async () => {
        const on = !toggleState(tCloseGame);
        setToggle(tCloseGame, on); // 先翻转视觉状态
        try {
          await mra.call('set_auto_close_game', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tCloseGame, !on); // 回滚
        }
      });
    }

    const tExitMra = p('toggle-exitmra');
    if (tExitMra) {
      tExitMra.addEventListener('click', async () => {
        const on = !toggleState(tExitMra);
        setToggle(tExitMra, on); // 先翻转视觉状态
        try {
          await mra.call('set_auto_exit_mra', { enabled: on });
        } catch (e) {
          console.error(e);
          showError(e.message);
          setToggle(tExitMra, !on); // 回滚
        }
      });
    }

    // 实时预览卡：播放/暂停（peep 开关）+ 放大/还原（全屏）
    const previewToggle = p('btn-preview-toggle');
    const previewMax = p('btn-preview-max');
    const previewMin = p('btn-preview-min');
    const previewCard = p('preview-card');

    if (previewToggle) {
      previewToggle.addEventListener('click', async () => {
        const on = !state.peepEnabled;
        setPreviewPlayState(on);      // 先翻转视觉状态
        try {
          await mra.call('set_peep', { enabled: on });
          state.peepEnabled = on;
        } catch (e) {
          console.error(e);
          showError(e.message);
          setPreviewPlayState(!on);   // 回滚
        }
      });
    }

    if (previewCard && previewMax && previewMin) {
      previewMax.addEventListener('click', () => { enterPreviewFullscreen(previewCard); });
      previewMin.addEventListener('click', () => { exitPreviewFullscreen(previewCard); });
    }

    // 点击方式 / 截图方式：每个 .radio-grid 是一组单选，组内互斥、组间独立
    // （两组共用 .mra-radio-card 样式，但选中态不能全页互斥，否则点一组会清掉另一组的选中）
    document.querySelectorAll('.radio-grid').forEach((grid) => {
      grid.querySelectorAll('.mra-radio-card').forEach((card) => {
        card.addEventListener('click', async () => {
          grid.querySelectorAll('.mra-radio-card').forEach((c) => c.classList.remove('mra-radio-card--selected'));
          card.classList.add('mra-radio-card--selected');
          try {
            if (card.dataset.clickmode) {
              if (card.dataset.clickmode === 'gamepad') showGamepadNotice();
              await mra.call('set_click_mode', { mode: card.dataset.clickmode });
            } else if (card.dataset.backend) {
              await mra.call('set_capture_backend', { backend: card.dataset.backend });
            }
          } catch (e) {
            console.error(e);
            showError(e.message);
          }
        });
      });
    });

    // 快捷工具：folder → sidecar open_user_data_folder；其余暂未接入
    document.querySelectorAll('.mra-tool-btn').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const tool = btn.dataset.tool;
        if (!tool) return; // 借用 .mra-tool-btn 样式但非快捷工具的按钮（如「打开权限优化中心」）交各自专属 handler，不在此报错
        if (tool === 'folder') {
          try {
            await mra.call('open_user_data_folder', {});
          } catch (e) {
            showError('打开文件夹失败: ' + e.message);
          }
        } else {
          showError('该工具尚未接入 sidecar');
        }
      });
    });
  }

  // 从后端回填当前模块的调试开关/截图方式状态（切换模块后丢弃过期回填）
  async function refreshDebugState() {
    const mid = currentModuleId;
    try {
      const d = await mra.call('get_debug_state');
      if (!d || currentModuleId !== mid) return; // 已切模块：丢弃
      // 判空防护：设置页 DOM 尚未渲染（或模板差异）时跳过对应回填，避免 null.classList 崩溃
      const safeToggle = (suffix, on) => { const el = $(mid + '-' + suffix); if (el) setToggle(el, on); };
      safeToggle('toggle-debug', !!d.debug_mode);
      state.peepEnabled = !!d.peep_enabled;
      setPreviewPlayState(!!d.peep_enabled);
      safeToggle('toggle-estop', !!d.emergency_stop_enabled);
      safeToggle('toggle-filelog', !!d.file_logging);
      safeToggle('toggle-closegame', !!d.auto_close_game);
      safeToggle('toggle-exitmra', !!d.auto_exit_mra);
      safeToggle('toggle-mutegame', !!d.mute_game);
      safeToggle('toggle-intent', !!d.intent_mode);
      // 点击方式 / 截图方式选中态
      document.querySelectorAll('.mra-radio-card').forEach((card) => {
        if (card.dataset.clickmode) {
          card.classList.toggle('mra-radio-card--selected', card.dataset.clickmode === d.click_mode);
        } else if (card.dataset.backend) {
          card.classList.toggle('mra-radio-card--selected', card.dataset.backend === d.capture_backend);
        }
      });
    } catch (e) {
      console.error(e);
      showError('读取调试状态失败: ' + e.message);
    }
  }

  function toggleState(el) {
    return el.classList.contains('mra-toggle--active');
  }
  function setToggle(el, on) {
    el.classList.toggle('mra-toggle--active', on);
    el.setAttribute('aria-checked', String(on));
  }

  // ---------- 实时预览：播放状态视觉 ----------
  // on=true 默认显示「暂停」图标（表示正在预览）；off 显示「播放」图标（预览已停）
  function setPreviewPlayState(on) {
    const btn = $(currentModuleId + '-btn-preview-toggle');
    if (!btn) return;
    const play = btn.querySelector('.icon-play');
    const pause = btn.querySelector('.icon-pause');
    if (play) play.style.display = on ? 'none' : 'block';
    if (pause) pause.style.display = on ? 'block' : 'none';
    btn.title = on ? '暂停预览' : '开始预览';
  }

  // ---------- 实时预览：放大（撑满数据页整块区域、深色预览区四周留白、居中）
  //  不复用 position:fixed——数据页带 .page-slide-in 的 will-change 会命中 containing-block 陷阱，
  //  改为切换 .preview-fs-active 让预览卡在文档流里撑满整页。 ----------
  let _previewFsCard = null; // 当前放大的预览卡
  const PREVIEW_FS_PAD = 32; // 深色预览区四周留白

  // 按 16:9 + 留白计算深色预览区尺寸并写入内联样式（保证等比、不变形）
  function fitPreviewCanvas() {
    const card = _previewFsCard;
    if (!card) return;
    const wrap = card.querySelector('.preview-wrap');
    const canvas = card.querySelector('.preview-canvas');
    if (!wrap || !canvas) return;
    const availW = wrap.clientWidth - PREVIEW_FS_PAD * 2;
    const availH = wrap.clientHeight - PREVIEW_FS_PAD * 2;
    if (availW <= 0 || availH <= 0) return;
    const ratio = 16 / 9;
    let w = availW;
    let h = w / ratio;
    if (h > availH) { h = availH; w = h * ratio; } // 高不够：改由高决定，仍保持 16:9
    canvas.style.width = Math.floor(w) + 'px';
    canvas.style.height = Math.floor(h) + 'px';
  }

  // 放大/还原时切换数据页放大态（隐藏左栏 + 今日看板，预览卡独占整页）
  function setPreviewFsActive(card, active) {
    const pageEl = card.closest('.page');
    if (pageEl) pageEl.classList.toggle('preview-fs-active', active);
  }

  // 放大态只保留「还原」，隐藏「放大」；还原后恢复
  function setPreviewMaxVisible(visible) {
    const maxBtn = $(currentModuleId + '-btn-preview-max');
    const minBtn = $(currentModuleId + '-btn-preview-min');
    if (maxBtn) maxBtn.style.display = visible ? '' : 'none';
    if (minBtn) minBtn.style.display = visible ? 'none' : 'flex';
  }

  function enterPreviewFullscreen(card) {
    card.classList.add('preview-card--fullscreen');
    setPreviewFsActive(card, true);
    setPreviewMaxVisible(false); // 放大态只剩「还原」
    _previewFsCard = card;
    fitPreviewCanvas();
    window.addEventListener('resize', onPreviewFsResize);
  }

  function onPreviewFsResize() {
    fitPreviewCanvas(); // 窗口变化时重算深色预览区尺寸，保持 16:9 与留白
  }

  function exitPreviewFullscreen(card) {
    card.classList.remove('preview-card--fullscreen');
    setPreviewFsActive(card, false);
    setPreviewMaxVisible(true);  // 还原：恢复「放大」
    const canvas = card.querySelector('.preview-canvas');
    if (canvas) { canvas.style.width = ''; canvas.style.height = ''; } // 清除内联尺寸还原为 100%
    window.removeEventListener('resize', onPreviewFsResize);
    _previewFsCard = null;
  }

  // ---------- 日志按钮 ----------
  $('btn-log-clear').addEventListener('click', () => {
    $('log-area').innerHTML = '';
    _curSec = null;
  });
  $('btn-log-copy').addEventListener('click', () => {
    const text = Array.from($('log-area').querySelectorAll('.log-line'))
      .map((d) => d.dataset.raw || d.textContent).join('\n');
    if (!text) return;
    navigator.clipboard.writeText(text).catch(() => {});
  });

  // ---------- 运行控制 ----------
  let _startCountdownTimer = null;
  let _startCountdownSec = 0;
  const _startBtnOriginHTML = $('btn-start').innerHTML; // 备份原始按钮内容（图标+文字），倒计时结束/取消后还原

  function _cancelStartCountdown() {
    if (_startCountdownTimer) {
      clearInterval(_startCountdownTimer);
      _startCountdownTimer = null;
    }
    _startCountdownSec = 0;
    $('btn-start').innerHTML = _startBtnOriginHTML;
  }

  async function _doStart(startFrom) {
    try {
      await mra.call('start', { start_from: startFrom });
    } catch (e) {
      console.error(e);
      // 缺少 ViGEmBus 驱动：弹出下载引导（而非仅 toast）
      if (e && e.message && e.message.indexOf('VIGEM_BUS_MISSING') !== -1) {
        showVigemDialog(e.message);
        return;
      }
      showError('启动失败: ' + e.message);
    }
  }

  $('btn-start').onclick = () => {
    if (state.is_running) return;
    // 倒计时进行中再次点击 = 取消
    if (_startCountdownTimer) {
      _cancelStartCountdown();
      return;
    }
    const stage = state.stages[state.selected_index];
    const startFrom = stage && stage !== state.stages[0] ? stage : null;
    // 三秒倒计时：给玩家切到游戏窗口/就位的时间；倒计时中再点按钮可取消
    const btn = $('btn-start');
    _startCountdownSec = 3;
    btn.textContent = _startCountdownSec + ' · 再点取消';
    _startCountdownTimer = setInterval(() => {
      _startCountdownSec -= 1;
      if (_startCountdownSec > 0) {
        btn.textContent = _startCountdownSec + ' · 再点取消';
      } else {
        _cancelStartCountdown(); // 恢复按钮原样
        _doStart(startFrom);     // 倒计时结束才真正启动
      }
    }, 1000);
  };

  $('btn-stop').onclick = async () => {
    try {
      await mra.call('stop');
    } catch (e) {
      console.error(e);
    }
  };

  // ---------- 启动 ----------
  init();
})();
