// MaaRM shell 前端 —— 通用模态弹窗与轻量引导弹窗。
// openModal：overlay + 居中卡片，点空白处默认关闭；各场景弹窗（手柄须知、优化中心、
// 过期确认、员工守则等）都复用它。跨文件调用经 window.MRA（见文件末尾导出）。
(function () {
  'use strict';

  // ---------- 通用模态弹窗 ----------
  // overlay + 居中卡片；opts: { title, titleColor, bodyHtml, maxWidth, buttons:[{text, primary, asLink, href, onClick(modal)}] }
  // onClick 回调自主决定是否调用 modal.close()；点空白处或按 Esc 默认关闭（Esc 仅最顶层模态响应）。
  // 结构与配色由 style.css 的 mra-modal-* 类承载，这里只设动态值（maxWidth / titleColor）。
  // 键盘可达性（所有弹窗经本函数免费获得）：开卡即聚焦卡内第一个可聚焦元素，
  // Tab/Shift+Tab 在卡内首尾回环不逃逸，关闭后焦点归还触发元素；
  // 卡片带 role=dialog + aria-modal + aria-labelledby（指向标题）。
  let _modalTitleSeq = 0;
  function openModal(opts) {
    const prevFocus = document.activeElement; // 关闭后焦点归还目标
    const overlay = document.createElement('div');
    overlay.className = 'mra-modal-overlay';
    const card = document.createElement('div');
    card.className = 'mra-modal-card mra-modal-card--enter';
    card.tabIndex = -1; // 卡内无按钮时的兜底聚焦目标
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    // 动态值：卡片最大宽度按场景传参，经 CSS 变量落到样式表定义的默认值之上
    if (opts.maxWidth) card.style.setProperty('--mra-modal-max-width', opts.maxWidth + 'px');
    const h3 = document.createElement('h3');
    h3.className = 'mra-modal-title';
    h3.id = 'mra-modal-title-' + (++_modalTitleSeq);
    card.setAttribute('aria-labelledby', h3.id);
    // 动态值：标题色按场景取状态色 token（danger/warning/info），缺省用类里的正文色
    if (opts.titleColor) h3.style.color = opts.titleColor;
    h3.textContent = opts.title || '';
    card.appendChild(h3);
    const body = document.createElement('div');
    body.className = 'mra-modal-body';
    body.innerHTML = opts.bodyHtml || '';
    card.appendChild(body);
    const modal = { overlay, card, close };
    // 卡内可聚焦元素（打开时实时查询：bodyHtml 里也可能有按钮/链接）
    const focusables = () => Array.from(card.querySelectorAll(
      'a[href], button:not(:disabled), input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )).filter((el) => el.offsetParent !== null);
    // Esc 关闭 + Tab 焦点陷阱：监听挂 document，仅最顶层（DOM 中最后一个 overlay）响应；close 时解绑防泄漏
    document.addEventListener('keydown', onKeydown);
    function onKeydown(ev) {
      if (ev.key === 'Escape') {
        const overlays = document.querySelectorAll('.mra-modal-overlay');
        if (!overlays.length || overlays[overlays.length - 1] !== overlay) return;
        ev.preventDefault();
        modal.close();
        return;
      }
      if (ev.key !== 'Tab') return;
      const els = focusables();
      if (!els.length) { ev.preventDefault(); card.focus(); return; }
      const first = els[0];
      const last = els[els.length - 1];
      const cur = document.activeElement;
      if (ev.shiftKey) {
        if (cur === first || cur === card) { ev.preventDefault(); last.focus(); }
      } else if (cur === last || !card.contains(cur)) {
        ev.preventDefault(); first.focus();
      }
    }
    function close() {
      if (overlay._closing) return;
      overlay._closing = true;
      document.removeEventListener('keydown', onKeydown);
      // 关闭回调（可选）：三条关闭路径（按钮/空白/Esc）都会走到这里，只触发一次
      if (typeof opts.onClose === 'function') opts.onClose();
      overlay.classList.add('mra-modal-overlay--closing');
      card.classList.add('mra-modal-card--closing');
      // 与 --mra-duration-fast（0.15s）退出动画同拍，动画结束后移除节点、焦点归还触发元素
      setTimeout(() => {
        overlay.remove();
        if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
      }, 160);
    }
    if (Array.isArray(opts.buttons) && opts.buttons.length) {
      const row = document.createElement('div');
      row.className = 'mra-modal-actions';
      opts.buttons.forEach((b) => {
        let el;
        if (b.asLink) {
          el = document.createElement('a');
          el.className = 'mra-modal-link';
          el.href = b.href || '#';
          el.target = '_blank';
          el.rel = 'noopener';
        } else {
          el = document.createElement('button');
          el.className = 'mra-modal-btn' + (b.primary ? ' mra-modal-btn--primary' : '');
        }
        el.textContent = b.text;
        if (b.onClick) el.addEventListener('click', () => b.onClick(modal));
        row.appendChild(el);
      });
      card.appendChild(row);
    }
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (ev) => {
      // closeOnOverlay: false = 点空白不关（员工守则弹层堵逃课出口用）；默认 true
      if (ev.target === overlay && opts.closeOnOverlay !== false) modal.close();
    });
    (focusables()[0] || card).focus(); // 开卡即把焦点请进卡内（陷阱的起点）
    // 进场动画结束后摘动画类：卡片回归主文档光栅化，避免非整数 DPI 下文字发虚（同 page-slide-in 手法）
    card.addEventListener('animationend', (ev) => {
      if (ev.animationName === 'modal-card-in') card.classList.remove('mra-modal-card--enter');
    });
    return modal;
  }

  // ---------- 后台(手柄)使用须知：每次切入 gamepad 点击方式都会弹出 ----------
  function showGamepadNotice() {
    openModal({
      title: '后台(手柄)使用须知',
      titleColor: 'var(--mra-info,#38bdf8)',
      bodyHtml:
        '<div class="mra-modal-notice">' +
        '<p><b>适用场景</b>：游戏留在后台挂机，前台可正常聊聊天、看视频等——这些<b>不接收手柄操作</b>的程序不受影响，可放心共存。</p>' +
        '<p><b>不能与其他游戏并存</b>：本项目用虚拟手柄（ViGEmBus）输入，手柄状态对系统是<b>全局</b>的。运行期间<b>不要</b>同时启动<b>会识别手柄、接收手柄输入</b>的程序（如其他游戏、Steam 等），否则它们也会收到手柄输入，可能被误操作。</p>' +
        '<p class="mra-modal-notice-hint">挂机结束建议切回前台(鼠标)或停止程序，避免空闲时手柄误触。</p>' +
        '</div>',
      buttons: [
        { text: '知道了', primary: true, onClick: (modal) => modal.close() },
      ],
    });
  }

  // ---------- 导出到 window.MRA ----------
  Object.assign(window.MRA, { openModal, showGamepadNotice });
})();
