// MaaRM shell 前端 —— 运行日志：fetch_logs 轮询、区块化渲染（参考 MAA：锚点分段 +
// 区块卡片 + 色点级别）、日志按钮（清空/复制）。跨文件调用经 window.MRA（见文件末尾导出）。
(function () {
  'use strict';

  // 共享物导入（js/rpc.js 已先行加载）
  const { mra, $, showError } = window.MRA;

  async function pollLogs() {
    try {
      const d = await mra.call('fetch_logs');
      if (d && d.schema_version && Array.isArray(d.events)) {
        // 结构化通道在场即唯一事实源（legacy lines 是同数据的派生投影，不再双吃）
        if (d.events.length || d.truncated) renderEvents(d.events, d.truncated);
      } else if (d && d.lines && d.lines.length > 0) {
        appendLogs(d.lines); // 旧 sidecar：纯 legacy 管线
      }
    } catch (e) {
      console.error(e);
    } finally {
      setTimeout(pollLogs, 400);
    }
  }

  const levelClassMap = { INFO: 'INFO', OK: 'OK', WARNING: 'WARNING', ERROR: 'ERROR', DEBUG: 'DEBUG' };

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
    // 计数徽章按专属类取：`!` 标记也带 .log-badge，用裸 .log-badge 会取到标记本身，
    // 标记在下面被移除后就变成「非本节点子节点」，insertBefore 抛 NotFoundError，
    // 该批次剩余的行整批丢失（且计数从此不再更新）。
    const badge = sec.querySelector('.log-badge--count');
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

  // 关闭当前区块：统计细节行数/告警、刷新徽章（含错误/警告时卡头挂「!」标记），然后绑上点击开关。
  // 展开态不在这里动——含错误的区块也折叠着交出去，错误靠徽章提示，点一下即可看细节。
  function finalizeSection(sec) {
    let count = 0, hasError = false, hasWarn = false;
    sec.querySelectorAll('.log-section-body .log-line').forEach((d) => {
      count++;
      if (d.classList.contains('log-line--ERROR')) hasError = true;
      else if (d.classList.contains('log-line--WARNING')) hasWarn = true;
    });
    updateSectionMeta(sec, count, hasError, hasWarn);
    bindSectionToggle(sec);
  }

  // 「只在已贴底时才自动跟随」：判定必须在追加之前取——追加会抬高 scrollHeight，
  // 追加后再比就永远算不出「用户已经翻上去了」。阈值覆盖 .log-area 的 12px 底部内边距。
  const FOLLOW_BOTTOM_PX = 24;
  function isNearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_BOTTOM_PX;
  }

  // 区块骨架（锚点管线与结构化管线共用，保证两条路径的卡片逐字节同构）：
  // 默认折叠，建卡即绑开关（未收尾的「当前卡」也必须能点开看细节；bindSectionToggle 自带去重）
  function _makeSection(anchorType, ts, msgHtml, rawText) {
    const sec = document.createElement('div');
    sec.className = 'log-section log-section--' + anchorType;
    const head = document.createElement('div');
    head.className = 'log-line log-section-head';
    head.dataset.raw = rawText; // 复制/导出保留原始完整行（含时间/级别）
    head.innerHTML =
      '<span class="log-chev">▸</span>' +
      '<span class="log-msg">' + msgHtml + '</span>' +
      '<span class="log-section-meta">' +
        '<span class="log-badge log-badge--count">0</span>' +
        '<span class="log-time">' + ts + '</span>' +
      '</span>';
    const body = document.createElement('div');
    body.className = 'log-section-body';
    sec.appendChild(head);
    sec.appendChild(body);
    $('log-area').appendChild(sec);
    bindSectionToggle(sec);
    return sec;
  }

  function appendLogs(lines) {
    const area = $('log-area');
    const followBottom = isNearBottom(area); // 整批共用一次判定：用户在翻旧卡时不许被拽回底部
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
      // 面板上的第一行也开卡：首个锚点之前还有一批运行环境行（PEEP 预览 / 配置注入 /
      // 连接窗口之前），不这样它们只能裸挂在面板上、脱离卡片体系。
      const anchorType = matchSectionAnchor(msg) || (_curSec ? null : 'session');
      if (!anchorType) {
        // 非锚点行 → 追加到当前区块正文（卡片默认折叠，细节行展开后才看得见）。
        // 走到这里必然已有当前区块：首行是结构化行时上面已把它当隐式锚点开了卡，
        // 非结构化行则在前面就降级成散行返回了。
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
        return;
      }
      // 锚点：先关闭上一区块（统计/徽章），再开新区块并把该行作为头部（核心状态常显）
      if (_curSec) { finalizeSection(_curSec.el); _curSec = null; }
      const sec = _makeSection(anchorType, ts, highlightKeywords(escapeHtml(msg)), raw);
      _curSec = { el: sec, count: 0, hasError: false, hasWarn: false };
    });
    if (followBottom) area.scrollTop = area.scrollHeight;
  }

  // ---------- 结构化通道 renderer（契约 §8 第 4-6 步：双 renderer 并存） ----------
  // 新 sidecar 返回 events（结构化记录）：group_start/group_end 驱动卡片开合，
  // 带 group_id 的 log 事件进对应卡正文（fields 以通用 `key: value` 后缀呈现，先转义，
  // 不做前端翻译表——契约 §4）；**无 group_id 的 log 事件（未迁移生产者：speedrush、
  // sidecar 行）回灌 legacy appendLogs 锚点管线**——迁移期观感与现网逐字节一致。
  // SECTION_ANCHORS/KW_RULES 等全部生产者迁完才退役（契约 §8 第 9 步）。
  const _openGroups = new Map(); // group_id → { sec, count, hasError, hasWarn }
  function _fieldsSuffix(fields) {
    if (!fields) return '';
    return Object.keys(fields).map((k) => ' · ' + k + ': ' + fields[k]).join('');
  }
  function renderEvents(events, truncated) {
    const area = $('log-area');
    const followBottom = isNearBottom(area);
    if (truncated) {
      const hint = document.createElement('div');
      hint.className = 'log-line';
      hint.textContent = '[!!] 日志界面因落后过多已自动截断，以下为最新日志';
      area.appendChild(hint);
    }
    events.forEach((rec) => {
      if (rec.event_type === 'group_start') {
        const kind = (rec.kind === 'phase' || rec.kind === 'session' || rec.kind === 'loop')
          ? rec.kind : 'session';
        const title = rec.title || '';
        const sec = _makeSection(kind, rec.ts, highlightKeywords(escapeHtml(title)),
                                 '[' + rec.ts + '] [INFO] ' + title);
        _openGroups.set(rec.group_id, { sec, count: 0, hasError: false, hasWarn: false });
        return;
      }
      if (rec.event_type === 'group_end') {
        const g = _openGroups.get(rec.group_id);
        if (!g) return; // 无头组的 end 先到（start 被环形冲出）：正文行会建匿名卡，这里静默
        updateSectionMeta(g.sec, g.count, !!rec.has_error, !!rec.has_warning);
        _openGroups.delete(rec.group_id);
        return;
      }
      // log 事件
      let g = rec.group_id ? _openGroups.get(rec.group_id) : null;
      if (rec.group_id && !g) {
        // 无头组：组开事件已被环形缓冲冲出——建匿名卡收正文，不丢行
        g = { sec: _makeSection('session', rec.ts, '…（更早的组头已截断）', ''), count: 0, hasError: false, hasWarn: false };
        _openGroups.set(rec.group_id, g);
      }
      const lvl = levelClassMap[rec.level] ? rec.level : 'INFO';
      const suffix = _fieldsSuffix(rec.fields);
      const rawText = '[' + rec.ts + '] [' + rec.level + '] ' + (rec.message || '') + suffix;
      const row = document.createElement('div');
      row.className = 'log-line log-line--' + lvl;
      row.dataset.raw = rawText;
      row.innerHTML =
        '<span class="log-dot"></span>' +
        '<span class="log-msg">' + highlightKeywords(escapeHtml((rec.message || '') + suffix)) + '</span>';
      if (!g) {
        appendLogs([rawText]); // 未迁移生产者的散文行：交回锚点管线（其内部自管跟随滚动）
        return;
      }
      g.sec.querySelector('.log-section-body').appendChild(row);
      g.count += 1;
      if (lvl === 'ERROR') g.hasError = true;
      else if (lvl === 'WARNING') g.hasWarn = true;
      updateSectionMeta(g.sec, g.count, g.hasError, g.hasWarn);
    });
    if (followBottom) area.scrollTop = area.scrollHeight;
  }

  // ---------- 日志按钮 ----------
  $('btn-log-clear').addEventListener('click', () => {
    $('log-area').innerHTML = '';
    _curSec = null;
    _openGroups.clear(); // 结构化通道的开组表随 DOM 一起作废
  });
  // 复制反馈：图标弹簧形变（morph-icon 换图标即形变，与预览卡放大按钮同款手法）——
  // 成功 copy→check、失败 copy→x，停 1.2s 滚回；失败文案与空日志提示走 toast
  // （错误协议：日志卡无行内落点，保持 toast）。
  const COPY_ICON = MRAIcons.node('copy');
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
    const text = Array.from($('log-area').querySelectorAll('.log-line'))
      .map((d) => d.dataset.raw || d.textContent).join('\n');
    if (!text) {
      showError('暂无日志可复制');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      _flashCopyIcon(MRAIcons.node('check'));
    } catch (e) {
      console.error(e);
      _flashCopyIcon(MRAIcons.node('x'));
      showError('复制失败: ' + (e.message || e));
    }
  });

  // ---------- 导出到 window.MRA ----------
  Object.assign(window.MRA, { pollLogs });
})();
