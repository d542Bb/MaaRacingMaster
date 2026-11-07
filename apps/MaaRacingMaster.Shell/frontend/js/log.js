// MaaRM shell 前端 —— 运行日志：fetch_logs 轮询 + 事件驱动渲染（契约 §8 第 9 步终态：
// SECTION_ANCHORS/KW_RULES/前端状态推导已全部退役，组卡徽章由 group_start/group_end 的
// outcome 驱动，前端不按行数自行推导；无组散行收进「其他日志」容器卡，承载规则见 ADR-0017）。
// 日志按钮：清空/复制/回顶。跨文件调用经 window.MaaRM（见文件末尾导出）。
// 一切动态文本经 textContent 落 DOM，无 innerHTML 注入面。
(function () {
  'use strict';

  // 共享物导入（js/rpc.js 已先行加载）
  const { maarm, $, showError } = window.MaaRM;

  async function pollLogs() {
    try {
      const d = await maarm.call('fetch_logs');
      if (d && d.schema_version && Array.isArray(d.events)) {
        // 结构化通道在场即唯一事实源（legacy lines 是同数据的派生投影，不再双吃）
        if (d.events.length || d.truncated) renderEvents(d.events, d.truncated);
      } else if (d && d.lines && d.lines.length > 0) {
        appendLogs(d.lines); // 旧 sidecar：散行直显
      }
    } catch (e) {
      console.error(e);
    } finally {
      setTimeout(pollLogs, 400);
    }
  }

  const levelClassMap = { INFO: 'INFO', OK: 'OK', WARNING: 'WARNING', ERROR: 'ERROR', DEBUG: 'DEBUG' };

  // ---------- 卡状态槽：徽章 = 组卡生命周期终态，词与色由状态机给出 ----------
  const STATUS = {
    run:  ['log-status--run', '进行中'], ok: ['log-status--ok', '成功'],
    warn: ['log-status--warn', '警告'],  err: ['log-status--err', '失败'],
    part: ['log-status--part', '未完成'],
  };
  function setPill(sec, key) {
    let pill = sec.querySelector('.log-status');
    if (!pill) {
      pill = document.createElement('span');
      sec.querySelector('.log-section-head').prepend(pill);
    }
    pill.className = 'log-status ' + STATUS[key][0];
    pill.textContent = STATUS[key][1];
  }

  const _openGroups = new Map(); // group_id → { sec }

  // ---------- 复制/导出事实源（契约 §8 第 10 步）：渲染过的记录原样留存 ----------
  // 显示文本由记录拼装（含 seq/ts/group_id，fields 逐行 `key: value`），与文件日志
  // 的 ::group:: 标记和环形序号可对照；封顶与后端环形缓冲同阶，只丢最旧。
  const _records = [];
  const RECORDS_MAX = 2000;
  function _remember(rec) {
    _records.push(rec);
    if (_records.length > RECORDS_MAX) _records.splice(0, _records.length - RECORDS_MAX);
  }
  function _copyText() {
    const lines = [];
    _records.forEach((rec) => {
      const head = '#' + rec.seq + ' ' + rec.ts + ' ';
      if (rec.event_type === 'group_start') {
        lines.push(head + 'GROUP-START g=' + rec.group_id + ' ' + (rec.kind || '') + ' ' + (rec.title || ''));
      } else if (rec.event_type === 'group_end') {
        lines.push(head + 'GROUP-END g=' + rec.group_id + ' outcome=' + (rec.outcome || '') +
                   ' duration_ms=' + (rec.duration_ms == null ? '' : rec.duration_ms));
      } else {
        lines.push(head + (rec.level || 'INFO') + (rec.group_id ? ' g=' + rec.group_id : '') + ' ' + (rec.message || ''));
        if (rec.fields) Object.keys(rec.fields).forEach((k) => {
          lines.push('#' + rec.seq + '   ' + k + ': ' + rec.fields[k]);
        });
      }
    });
    return lines.join('\n');
  }

  function _makeSection(kind, ts, titleText, rawText, pillKey) {
    const sec = document.createElement('div');
    sec.className = 'log-section log-section--' + kind;
    const head = document.createElement('div');
    head.className = 'log-line log-section-head';
    head.dataset.raw = rawText; // 复制/导出保留原始完整行（含时间/级别）
    head.title = rawText;       // 标题展卷的安全出口之一
    if (pillKey) {
      const pill = document.createElement('span');
      pill.className = 'log-status ' + STATUS[pillKey][0];
      pill.textContent = STATUS[pillKey][1];
      head.appendChild(pill);
    }
    const msg = document.createElement('span');
    msg.className = 'log-msg';
    msg.textContent = titleText;
    head.appendChild(msg);
    const meta = document.createElement('span');
    meta.className = 'log-section-meta';
    const time = document.createElement('span');
    time.className = 'log-time';
    time.textContent = ts;
    meta.appendChild(time);
    head.appendChild(meta);
    const body = document.createElement('div');
    body.className = 'log-section-body';
    sec.appendChild(head);
    sec.appendChild(body);
    $('log-area').appendChild(sec);
    bindSectionToggle(sec);
    return sec;
  }

  // 正文行统一入口：落进卡体的那一刻卡才获得可展开性（计数胶囊 + 指针光标 + 悬停底色）
  function addRow(sec, rowEl) {
    sec.querySelector('.log-section-body').appendChild(rowEl);
    sec.classList.add('log-section--expandable');
    const meta = sec.querySelector('.log-section-meta');
    let chip = sec.querySelector('.log-count');
    if (!chip) {
      chip = document.createElement('span');
      chip.className = 'log-count';
      meta.insertBefore(chip, meta.firstChild);
    }
    chip.textContent = String(Number(chip.textContent || 0) + 1);
  }

  function _makeRow(lvl, msgText, rawText) {
    const row = document.createElement('div');
    row.className = 'log-line log-line--' + lvl;
    row.dataset.raw = rawText;
    const msg = document.createElement('span');
    msg.className = 'log-msg';
    msg.textContent = msgText;
    row.appendChild(msg);
    return row;
  }

  function bindSectionToggle(sec) {
    const head = sec.querySelector('.log-section-head');
    if (!head || head.dataset.bound) return;
    head.dataset.bound = '1';
    head.addEventListener('click', (ev) => {
      ev.preventDefault();
      // 空卡不展开：可点承诺只在首条正文落卡体时给出（--expandable 同源判据，
      // README「可点性与内容一致」）——无正文的卡头点击不得产生任何视觉变化。
      if (!sec.classList.contains('log-section--expandable')) return;
      sec.classList.toggle('log-section--open');
      _syncTopBtn(); // 展开改变内容高度，回顶按钮的可见性要跟上
    });
  }

  // ---------- 标题溢出：一次性接线——超宽的卡头标题包内层卷动体并量出位移 ----------
  // dataset.fit 去重：每张卡只测一次（标题建卡后不再变化）。
  function fitTitles() {
    document.querySelectorAll('.log-section-head .log-msg').forEach((el) => {
      if (el.dataset.fit) return;
      el.dataset.fit = '1';
      if (el.scrollWidth <= el.clientWidth + 1) return;
      el.classList.add('log-msg--more');
      const inner = document.createElement('span');
      inner.className = 'log-msg-inner';
      while (el.firstChild) inner.appendChild(el.firstChild);
      el.appendChild(inner);
      el.style.setProperty('--scroll-x',
        -(inner.scrollWidth - el.clientWidth + 8) + 'px');
    });
  }

  // 「只在已贴底时才自动跟随」：判定必须在追加之前取——追加会抬高 scrollHeight，
  // 追加后再比就永远算不出「用户已经翻上去了」。阈值覆盖 .log-area 的 12px 底部内边距。
  const FOLLOW_BOTTOM_PX = 24;
  function isNearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_BOTTOM_PX;
  }

  // ---------- 无组散行 → 「其他日志」容器卡（旧 sidecar 兼容路径同走此路） ----------
  // 容器不承载生命周期：不给徽章、不按内容推导状态（组卡徽章仍只吃 outcome，ADR-0017）。
  // 叙事视图（ADR-0024/0025）：纯 INFO 容器不落 DOM——家务行只进复制与文件；卡体
  // 收到首条 WARNING+ 时整卡连同此前缓冲行按原时序插入，ERROR（或截断提示）才展开。
  // 相邻即合并：开出任何组卡（含无头组匿名卡）即作废当前容器，散行不跨组卡串联。
  const BARE_TITLE = '其他日志';
  let _bareSec = null;     // 当前容器（可能已渲染，也可能还在缓冲）
  let _bareAnchor = null;  // 缓冲时的插入锚：开缓冲那一刻 log-area 的末元素
  function _bareSection(ts, force) {
    if (!_bareSec) {
      const area = $('log-area');
      _bareAnchor = area.lastElementChild; // 锚先取：_makeSection 建卡即落 DOM
      _bareSec = _makeSection('session', ts, BARE_TITLE, '', null);
      _bareSec.remove(); // 缓冲卡摘离 DOM，过渲染门槛（首条 WARNING+）再按锚挂回
    }
    if (force) _bareRender();
    return _bareSec;
  }
  function _bareRender() {
    if (!_bareSec || _bareSec.isConnected) return;
    const area = $('log-area');
    area.insertBefore(_bareSec, _bareAnchor ? _bareAnchor.nextSibling : null);
    _bareAnchor = null;
  }
  function _bareAdd(ts, lvl, rowEl, force) {
    const sec = _bareSection(ts, force);
    addRow(sec, rowEl);
    if (force || lvl === 'WARNING' || lvl === 'ERROR') {
      _bareRender(); // 渲染门槛（ADR-0024）：WARNING+ 才落 DOM
      if (force || lvl === 'ERROR') {
        sec.classList.add('log-section--open'); // 真异常才展开（ADR-0025）
        _syncTopBtn();
      }
    }
  }
  function _bareDrop() {
    _bareSec = null;
    _bareAnchor = null;
  }

  function appendLogs(lines) {
    const area = $('log-area');
    const followBottom = isNearBottom(area); // 整批共用一次判定
    lines.forEach((raw) => {
      const m = raw.match(/^\[(\d{2}:\d{2}:\d{2})\] \[(\w+)\] /);
      const lvl = m && levelClassMap[m[2]] ? m[2] : 'INFO';
      _bareAdd(m ? m[1] : '', lvl,
               _makeRow(lvl, m ? raw.slice(m[0].length) : raw, raw));
    });
    if (followBottom) area.scrollTop = area.scrollHeight;
    _syncTopBtn();
  }

  // ---------- 结构化管线：组事件开合卡，组内行进卡体，无组行收进容器卡 ----------
  function _fieldsSuffix(fields) {
    if (!fields) return '';
    return Object.keys(fields).map((k) => ' · ' + k + ': ' + fields[k]).join('');
  }
  function renderEvents(events, truncated) {
    const area = $('log-area');
    const followBottom = isNearBottom(area);
    if (truncated) {
      const hint = '[!!] 日志界面因落后过多已自动截断，以下为最新日志';
      _bareAdd(events.length ? events[0].ts : '', 'INFO',
               _makeRow('INFO', hint, hint), true); // 系统级提示：无条件可见
    }
    events.forEach((rec) => {
      _remember(rec);
      if (rec.event_type === 'group_start') {
        const kind = (rec.kind === 'phase' || rec.kind === 'session' || rec.kind === 'loop')
          ? rec.kind : 'session';
        const title = rec.title || '';
        const sec = _makeSection(kind, rec.ts, title,
                                 '[' + rec.ts + '] [INFO] ' + title, 'run');
        _openGroups.set(rec.group_id, { sec });
        _bareDrop(); // 组卡在场即断开散行序列，后续散行另起容器
        return;
      }
      if (rec.event_type === 'group_end') {
        const g = _openGroups.get(rec.group_id);
        if (!g) return; // 无头组的 end 先到（start 被环形冲出）：正文行会建匿名卡，这里静默
        const pill = { success: 'ok', warning: 'warn', failure: 'err',
                       incomplete: 'part' }[rec.outcome] || 'ok';
        setPill(g.sec, pill);
        // 叙事视图（ADR-0025）：只有真异常（failure）自动展开——警告与未完成
        // 由卡头徽章承载信号（手动停止的原因用户自己刚按下），展开是留给异常
        // 的信号，多数情况保持折叠。由 outcome 驱动，前端不读正文内容；空身卡
        // 不展开，与「可点性=内容存在」同口径（没有可看的东西就不给展开态）。
        if (rec.outcome === 'failure' && g.sec.classList.contains('log-section--expandable')) {
          g.sec.classList.add('log-section--open');
          _syncTopBtn();
        }
        _openGroups.delete(rec.group_id);
        return;
      }
      // log 事件
      let g = rec.group_id ? _openGroups.get(rec.group_id) : null;
      if (rec.group_id && !g) {
        // 无头组：组开事件已被环形缓冲冲出——建匿名卡收正文，不丢行
        g = { sec: _makeSection('session', rec.ts, '…（更早的组头已截断）', '', null) };
        _openGroups.set(rec.group_id, g);
        _bareDrop();
      }
      const lvl = levelClassMap[rec.level] ? rec.level : 'INFO';
      const suffix = _fieldsSuffix(rec.fields);
      const rawText = '[' + rec.ts + '] [' + rec.level + '] ' + (rec.message || '') + suffix;
      if (!g) { _bareAdd(rec.ts, lvl, _makeRow(lvl, (rec.message || '') + suffix, rawText)); return; }
      addRow(g.sec, _makeRow(lvl, (rec.message || '') + suffix, rawText));
    });
    fitTitles();
    if (followBottom) area.scrollTop = area.scrollHeight;
    _syncTopBtn();
  }

  // ---------- 回顶按钮：随滚动位置换向 ----------
  // 贴底 → 去最旧（顶部）；翻上去 → 回最新（底部）；内容不溢出时隐藏。
  function _scrollLogs(target) {
    const area = $('log-area');
    const top = target === 'top' ? 0 : area.scrollHeight;
    const smooth = !(window.matchMedia &&
                     window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    if (!smooth) { area.scrollTop = top; return; }
    // WebView2 遮挡时合成器会挂起 smooth 动画（探针实测：可见标签页内 smooth 位移为 0，
    // auto 正常）——发出 smooth 后 600ms 检查，未动则强制瞬移，按钮不得表现为失灵。
    const before = area.scrollTop;
    area.scrollTo({ top, behavior: 'smooth' });
    setTimeout(() => { if (area.scrollTop === before && top !== before) area.scrollTop = top; }, 600);
  }
  function _syncTopBtn() {
    const area = $('log-area');
    const btn = $('btn-log-top');
    if (!btn) return;
    btn.hidden = area.scrollHeight - area.clientHeight <= FOLLOW_BOTTOM_PX;
    const atBottom = isNearBottom(area);
    btn.classList.toggle('log-top-btn--down', !atBottom);
    if (btn.hidden) btn.classList.remove('log-top-btn--show');
    const label = atBottom ? '回到顶部（最旧日志）' : '回到底部（最新日志）';
    btn.title = label;
    btn.setAttribute('aria-label', label);
  }
  $('log-area').addEventListener('scroll', _syncTopBtn);
  $('btn-log-top').addEventListener('click', () => {
    _scrollLogs(isNearBottom($('log-area')) ? 'top' : 'bottom');
  });
  // 浮现判定：鼠标进入按钮中心一小圈才亮（mousemove 只读不拦——悬浮层不挡日志点击）；
  // 离开日志卡即收。键盘经 focus-visible 浮现（CSS），不依赖本判定。
  const TOP_BTN_NEAR_PX = 64;
  const _logCard = document.querySelector('.log-card');
  if (_logCard) {
    _logCard.addEventListener('mousemove', (e) => {
      const btn = $('btn-log-top');
      if (!btn || btn.hidden) return;
      const r = btn.getBoundingClientRect();
      const near = Math.hypot(e.clientX - (r.left + r.width / 2),
                              e.clientY - (r.top + r.height / 2)) <= TOP_BTN_NEAR_PX;
      btn.classList.toggle('log-top-btn--show', near);
    });
    _logCard.addEventListener('mouseleave', () => {
      const btn = $('btn-log-top');
      if (btn) btn.classList.remove('log-top-btn--show');
    });
  }

  // ---------- 日志按钮 ----------
  $('btn-log-clear').addEventListener('click', () => {
    $('log-area').innerHTML = '';
    _openGroups.clear(); // 开组表随 DOM 一起作废
    _bareDrop();         // 散行容器卡（含缓冲态与插入锚）同样作废，否则会挂进已删除节点
    _records.length = 0; // 复制事实源同步作废（清空即「不留可导出的历史」）
    _syncTopBtn();
  });
  // 复制反馈：图标弹簧形变（morph-icon 换图标即形变，与预览卡放大按钮同款手法）——
  // 成功 copy→check、失败 copy→x，停 1.2s 滚回；失败文案与空日志提示走 toast
  // （错误协议：日志卡无行内落点，保持 toast）。
  const COPY_ICON = MaaRMIcons.node('copy');
  const copyMorphEl = document.querySelector('#btn-log-copy morph-icon');
  let _copyRevertTimer = null;
  function _flashCopyIcon(nodes) {
    if (!copyMorphEl) return;
    clearTimeout(_copyRevertTimer);
    copyMorphEl.icon = nodes;
    _copyRevertTimer = setTimeout(() => { copyMorphEl.icon = COPY_ICON; }, 1200);
  }
  if (copyMorphEl) copyMorphEl.icon = COPY_ICON;
  $('btn-log-copy').addEventListener('click', async () => {
    // 结构化通道在场：复制文本由记录拼装（含 seq/ts/group_id，fields 逐行）；
    // 旧 sidecar（无 events）回退 DOM 原始行。
    const text = _records.length
      ? _copyText()
      : Array.from($('log-area').querySelectorAll('.log-section-body .log-line'))
          .map((d) => d.dataset.raw || d.textContent).join('\n');
    if (!text) {
      showError('暂无日志可复制');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      _flashCopyIcon(MaaRMIcons.node('check'));
    } catch (e) {
      console.error(e);
      _flashCopyIcon(MaaRMIcons.node('x'));
      showError('复制失败: ' + (e.message || e));
    }
  });

  // ---------- 导出到 window.MaaRM ----------
  Object.assign(window.MaaRM, { pollLogs });
})();
