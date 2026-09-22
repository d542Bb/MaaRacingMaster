// MaaRM shell 前端 —— 活动模块下拉：原生 <select> 的自绘 combobox 投影层。
// 视觉语言 = OriginUI Select（shadcn/Radix 家族）：等宽浮层、选项行圆角 hover、
// 选中=左 checkmark+加粗（不整行反色）、过期项灰字+后缀。
// **值真源仍是隐藏的原生 select**：app.js 填充 options、run.js 读 value、
// settings.js 切 disabled、onModuleChange 撤销回写——本层一律不碰，只在
// 这些点后调用 MRA.syncModuleSelect() 让投影跟上；用户选择经原生 change 派发，
// 现有监听（含过期确认弹窗）零改动。试验区样本见 git 历史（select_lab）。
(function () {
  'use strict';

  const native = document.getElementById('module-select');
  const wrap = document.getElementById('msel');
  if (!native || !wrap) return;
  const trigger = document.getElementById('msel-trigger');
  const list = document.getElementById('msel-list');
  const labelEl = document.getElementById('msel-label');

  let open = false, active = -1;

  function opts() { return Array.from(native.options); }

  // 到期倒计时徽章：每次下拉展开重建列表（renderList），剩余时间随之刷新。
  // valid_until 为 ISO 8601 含时区（manifest 声明），Date.parse 直接可比本地 now。
  // 空选项与已过期项（已有「已过期」后缀）不显示；无声明 = 永久；
  // 剩余不足 1 天显示「x小时x分」，否则「x天x小时」；3 天内底色转淡黄。
  const SOON_MS = 3 * 24 * 60 * 60 * 1000;
  function badgeText(o) {
    if (!o.value || o.dataset.expired) return null;
    const iso = o.dataset.validUntil;
    if (!iso) return '永久';
    const until = Date.parse(iso);
    if (!Number.isFinite(until)) return null;
    const ms = until - Date.now();
    if (ms <= 0) return null; // 过期标记尚未轮询刷新的间隙，不显示倒计时
    const totalMin = Math.floor(ms / 60000);
    const d = Math.floor(totalMin / 1440);
    const h = Math.floor((totalMin % 1440) / 60);
    const m = totalMin % 60;
    return d > 0 ? `${d}天${h}小时` : (h > 0 ? `${h}小时${m}分` : `${m}分`);
  }
  function renderList() {
    list.innerHTML = '';
    opts().forEach((o, i) => {
      const row = document.createElement('div');
      row.className = 'msel-opt'
        + (o.value === native.value ? ' msel-opt--selected' : '')
        + (o.dataset.expired ? ' msel-opt--expired' : '')
        + (i === active ? ' msel-opt--active' : '');
      row.id = 'msel-opt-' + i;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', o.value === native.value ? 'true' : 'false');
      const txt = document.createElement('span');
      txt.textContent = o.textContent.replace(/（已过期）$/, '');
      row.appendChild(txt);
      const badge = badgeText(o);
      if (badge) {
        const el = document.createElement('span');
        el.className = 'msel-badge'
          + (o.dataset.validUntil && Date.parse(o.dataset.validUntil) - Date.now() < SOON_MS
            ? ' msel-badge--soon' : '');
        el.textContent = badge;
        row.appendChild(el);
      }
      if (o.dataset.expired) {
        const sfx = document.createElement('span');
        sfx.className = 'msel-suffix';
        sfx.textContent = '已过期';
        row.appendChild(sfx);
      }
      row.addEventListener('mousedown', (e) => { e.preventDefault(); pick(i); });
      row.addEventListener('mousemove', () => { if (active !== i) { active = i; paintActive(); } });
      list.appendChild(row);
    });
  }
  function paintActive() {
    Array.from(list.children).forEach((el, i) => el.classList.toggle('msel-opt--active', i === active));
    trigger.setAttribute('aria-activedescendant', open && active >= 0 ? 'msel-opt-' + active : '');
    if (active >= 0 && list.children[active]) list.children[active].scrollIntoView({ block: 'nearest' });
  }
  function renderLabel() {
    const o = native.options[native.selectedIndex];
    const text = o ? o.textContent.replace(/（已过期）$/, '') : '';
    labelEl.textContent = text;
    labelEl.classList.toggle('msel-label--empty', native.value === '');
  }
  function setOpen(v) {
    open = v;
    wrap.classList.toggle('msel--open', v);
    trigger.setAttribute('aria-expanded', v ? 'true' : 'false');
    if (v) {
      active = opts().findIndex((o) => o.value === native.value);
      renderList(); paintActive();
    } else { active = -1; }
  }
  function pick(i) {
    const o = opts()[i];
    if (!o || o.value === native.value) { setOpen(false); trigger.focus(); return; }
    native.value = o.value;
    native.dispatchEvent(new Event('change', { bubbles: true }));
    setOpen(false); trigger.focus();
  }
  function move(d) {
    const n = opts().length;
    active = active < 0 ? (d > 0 ? 0 : n - 1) : (active + d + n) % n;
    paintActive();
  }
  trigger.addEventListener('click', () => {
    if (trigger.getAttribute('aria-disabled') === 'true') return;
    setOpen(!open);
  });
  trigger.addEventListener('keydown', (e) => {
    if (trigger.getAttribute('aria-disabled') === 'true') return;
    const k = e.key;
    if (!open && (k === 'Enter' || k === ' ' || k === 'ArrowDown' || k === 'ArrowUp')) {
      e.preventDefault(); setOpen(true); return;
    }
    if (!open) return;
    if (k === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (k === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (k === 'Home') { e.preventDefault(); active = 0; paintActive(); }
    else if (k === 'End') { e.preventDefault(); active = opts().length - 1; paintActive(); }
    else if (k === 'Enter' || k === ' ') { e.preventDefault(); pick(active); }
    else if (k === 'Escape') { e.preventDefault(); setOpen(false); trigger.focus(); }
    else if (k === 'Tab') { setOpen(false); }
    else if (k.length === 1 && /\S/.test(k)) {
      const idx = opts().findIndex((o) => o.textContent.toLowerCase().startsWith(k.toLowerCase()));
      if (idx >= 0) { e.preventDefault(); active = idx; paintActive(); }
    }
  });
  document.addEventListener('mousedown', (e) => {
    if (open && !wrap.contains(e.target)) setOpen(false);
  });
  native.addEventListener('change', () => { renderLabel(); if (open) renderList(); });

  // 投影同步：原生 select 被代码改动（填充 options / 程序化改 value / 切 disabled）
  // 后调用。change 事件只覆盖用户路径，程序化赋值不派发 change——调用点：
  // app.js renderModuleSelect 末尾、onModuleChange 两处撤销回写、settings.js 运行锁定。
  function sync() {
    trigger.setAttribute('aria-disabled', native.disabled ? 'true' : 'false');
    trigger.title = native.title || '';
    if (native.disabled && open) setOpen(false);
    renderLabel();
    if (open) renderList();
  }
  Object.assign(window.MRA, { syncModuleSelect: sync });
  sync();
})();
