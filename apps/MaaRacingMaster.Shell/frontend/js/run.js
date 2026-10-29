// MaaRM shell 前端 —— 运行控制与任务列表：开始/停止/三秒倒计时、任务卡列表
// （拖拽排序带 FLIP 让位预览、启用勾选、展开面板 = 模块选项 + 细分阶段断点起点、
// 模块管理选卡弹窗）、状态轮询与性能卡渲染。跨文件调用经 window.MRA（见文件末尾导出）。
(function () {
  'use strict';

  // 共享物导入（js/rpc.js、js/settings.js、js/modal.js 已先行加载）
  const {
    mra, $, state, reportError, showError,
    updateModuleOptionsDisabled, refreshModuleOptions, refreshSpeedrushStatus,
    renderModulePages, stashOptionBlocks, openModal,
  } = window.MRA;

  // ---------- 模块展示元数据（图标 / 头图 / 配置摘要） ----------
  // 头图为品牌渐变 + emoji 占位，登记处只此一处；模块自带美术图后在这里替换。
  // 未登记模块回落通用渐变 + 名称首字——新增模块不需要改前端即可入列。
  const MODULE_META = {
    speedrush: {
      icon: '🏎️',
      art: 'radial-gradient(120% 90% at 20% 10%, rgba(96,165,250,.35), transparent 55%), linear-gradient(135deg, #0f172a 0%, #312e81 60%, #6d28d9 100%)',
      summary: (cfg) => [
        '录制: ' + (cfg && cfg.record_mode ? '开' : '关'),
        '驾驶模式: ' + (cfg && cfg.straight_mode ? '全程直行' : '深度几何'),
      ],
    },
    treasure: {
      icon: '🏺',
      art: 'radial-gradient(120% 90% at 80% 15%, rgba(251,191,36,.4), transparent 55%), linear-gradient(135deg, #1c1917 0%, #78350f 60%, #b45309 100%)',
      summary: (cfg) => [
        '刷到第 ' + ((cfg && Number.isFinite(cfg.max_daily_loops)) ? cfg.max_daily_loops : 50) + ' 场',
        '目标场次: ' + ({ intern: '实习场', expert: '专家场', master: '大师场' }[(cfg && cfg.target_session) || 'master']),
      ],
    },
  };
  // 内置步骤（启动游戏 / 关闭游戏）与活动模块共用同一套卡片：只登记图标与头图
  MODULE_META.launch_game = {
    icon: '🚀',
    art: 'radial-gradient(120% 90% at 20% 10%, rgba(45,212,191,.42), transparent 55%), linear-gradient(135deg, #06281f 0%, #0f766e 60%, #22d3ee 100%)',
  };
  MODULE_META.close_game = {
    icon: '🛑',
    art: 'radial-gradient(120% 90% at 80% 15%, rgba(248,113,113,.4), transparent 55%), linear-gradient(135deg, #2b1111 0%, #7f1d1d 60%, #ef4444 100%)',
  };
  const FALLBACK_ART = 'linear-gradient(135deg, #232837 0%, #3a4152 60%, #4b5568 100%)';
  const SESSION_LABELS = { intern: '实习场', expert: '专家场', master: '大师场' };
  const modOf = (mid) => state.modules.find((m) => m.id === mid) || null;
  const metaOf = (mid) => MODULE_META[mid] || null;
  // 名字/阶段/配置值出自插件注册表（本仓库可信数据），与 settings.js 模板同一纪律：
  // 拼进 HTML 前统一转义
  const esc = (s) => String(s).replace(/[<>&"]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));

  // ---------- 内置步骤（启动游戏 / 关闭游戏） ----------
  // 步骤没有插件那套「单实例 DOM 选项块」，字段少且固定，面板由这张表生成。
  // 默认值一律来自后端（get_initial_state 的 config_defaults），前端不另存一份。
  const BUILTIN_FIELDS = {
    launch_game: [
      { key: 'path', kind: 'path', label: '程序路径', hint: '（游戏主程序；留空则从启动器配置自动探测）' },
      { key: 'args', kind: 'args', label: '启动参数', hint: '（与启动器实测命令行一致）' },
      { key: 'timeout_s', kind: 'number', label: '等待游戏窗口', hint: '（超时则中止本轮）', suffix: '秒', min: 10, max: 600, step: 10 },
      { key: 'skip_if_running', kind: 'switch', title: '已在运行则跳过', desc: '检测到游戏窗口已经在时不再启动，避免拉起第二个进程', note: '推荐开启' },
      { key: 'detached', kind: 'switch', title: '独立进程', desc: '游戏不挂在本程序进程树下：本程序退出或被结束时，游戏照常运行' },
      { key: 'fallback_launcher', kind: 'switch', title: '启动失败时回退到启动器', desc: '等窗口超时仍未出现时，改走官方启动器（会先做版本校验与更新）。平时不会弹出启动器窗口', note: '推荐开启' },
    ],
    close_game: [
      { key: 'exit_app', kind: 'switch', title: '关闭后退出本程序', desc: '这一步完成后关闭 MaaRM；排在队尾时就是「跑完自动退出」。后面的步骤不再执行', note: '默认关闭' },
    ],
  };

  function isBuiltin(mid) {
    const m = modOf(mid);
    return !!m && m.kind === 'builtin';
  }

  function defaultsOf(mid) {
    const m = modOf(mid);
    return (m && m.config_defaults) || {};
  }

  // 取值：已存配置优先，缺项回落后端默认值
  function cfgValue(mid, key) {
    const cfg = state.cfg[mid] || {};
    if (Object.prototype.hasOwnProperty.call(cfg, key)) return cfg[key];
    return defaultsOf(mid)[key];
  }

  function builtinChips(mid) {
    if (mid === 'launch_game') {
      const out = [chipHtml('等待窗口 ' + (cfgValue(mid, 'timeout_s') || 120) + 's')];
      if (cfgValue(mid, 'skip_if_running')) out.push(chipHtml('已运行则跳过'));
      return out.join('');
    }
    if (mid === 'close_game' && cfgValue(mid, 'exit_app')) return chipHtml('关闭后退出');
    return '';
  }

  // 内置步骤的选项面板：字段行与开关行放进同一个纵向节奏容器（.builtin-fields 统一 gap），
  // 开关的「推荐开启」这类旁注并进标题行右端，避免与标题错行
  function builtinPanelHtml(mid) {
    const fields = BUILTIN_FIELDS[mid] || [];
    const rows = fields.map((f) => {
      if (f.kind === 'switch') {
        const on = !!cfgValue(mid, f.key);
        return '<div class="option-row">'
          + '<button class="mra-toggle' + (on ? ' mra-toggle--active' : '') + '" role="switch"'
          + ' aria-checked="' + on + '" data-key="' + f.key + '" data-kind="switch"></button>'
          + '<div class="option-main"><div class="option-title">' + esc(f.title)
          + (f.note ? '<span class="option-note">' + esc(f.note) + '</span>' : '')
          + '</div>'
          + '<p class="option-desc">' + esc(f.desc) + '</p></div>'
          + '</div>';
      }
      const val = cfgValue(mid, f.key);
      // 启动参数是多个开关项拼成的长串，单行输入必然截断：改多行等宽，一眼能核对完整
      const input = f.kind === 'number'
        ? '<input type="number" data-key="' + f.key + '" data-kind="number" value="' + esc(val == null ? '' : val) + '"'
          + ' min="' + (f.min || 0) + '" max="' + (f.max || 9999) + '" step="' + (f.step || 1) + '">'
          + (f.suffix ? '<span class="field-suffix">' + esc(f.suffix) + '</span>' : '')
        : (f.kind === 'args'
          ? '<textarea rows="2" spellcheck="false" data-key="' + f.key + '" data-kind="args">' + esc(val == null ? '' : val) + '</textarea>'
          : '<input type="text" data-key="' + f.key + '" data-kind="' + f.kind + '" value="' + esc(val == null ? '' : val) + '">');
      const extra = f.kind === 'path'
        ? '<button class="mra-btn" type="button" data-act="browse">浏览…</button>'
        : (f.kind === 'args' ? '<button class="mra-btn" type="button" data-act="reset" hidden>重置</button>' : '');
      return '<div class="module-options-row module-options-row--stack">'
        + '<div class="mra-field' + (f.kind === 'number' ? ' mra-field--narrow' : '') + '">'
        + '<label class="mra-field-label">' + esc(f.label)
        + (f.hint ? '<span class="field-hint">' + esc(f.hint) + '</span>' : '') + '</label>'
        + '<div class="mra-field-control-row' + (f.kind === 'args' ? ' mra-field-control-row--multi' : '') + '">'
        + '<div class="mra-field-control">' + input + '</div>' + extra + '</div>'
        + '</div></div>';
    });
    return '<div class="builtin-fields">' + rows.join('') + '</div>';
  }

  async function saveBuiltinCfg(mid, patch) {
    const cfg = Object.assign({}, state.cfg[mid] || {}, patch);
    try {
      state.cfg[mid] = await mra.call('set_module_config', { module_id: mid, config: cfg });
      refreshCard(mid);
    } catch (e) {
      showError(e.message);
    }
  }

  // 面板挂载 + 绑定（列表重建后面板随之重建，绑定一并重挂）
  function renderBuiltinPanel(mid) {
    const panel = document.querySelector('#task-list [data-id="' + mid + '"] [data-options]');
    if (!panel) return;
    panel.innerHTML = builtinPanelHtml(mid);
    const syncReset = () => {
      const argsEl = panel.querySelector('[data-key="args"]');
      const resetBtn = panel.querySelector('[data-act="reset"]');
      if (argsEl && resetBtn) resetBtn.hidden = argsEl.value === String(defaultsOf(mid).args || '');
    };
    panel.querySelectorAll('[data-kind="switch"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const on = !btn.classList.contains('mra-toggle--active');
        btn.classList.toggle('mra-toggle--active', on);
        btn.setAttribute('aria-checked', String(on));
        saveBuiltinCfg(mid, { [btn.dataset.key]: on });
      });
    });
    panel.querySelectorAll('input[data-key], textarea[data-key]').forEach((el) => {
      el.addEventListener('change', () => {
        const raw = el.dataset.kind === 'number' ? Number(el.value) : el.value;
        if (el.dataset.kind === 'number' && !Number.isFinite(raw)) return;
        saveBuiltinCfg(mid, { [el.dataset.key]: raw });
        syncReset();
      });
      if (el.dataset.kind === 'args') el.addEventListener('input', syncReset);
    });
    const browse = panel.querySelector('[data-act="browse"]');
    if (browse) {
      browse.addEventListener('click', async () => {
        try {
          const r = await mra.call('pick_game_exe', {});
          if (!r || !r.path) return;
          const el = panel.querySelector('[data-key="path"]');
          if (el) el.value = r.path;
          saveBuiltinCfg(mid, { path: r.path });
        } catch (e) {
          showError(e.message);
        }
      });
    }
    const reset = panel.querySelector('[data-act="reset"]');
    if (reset) {
      reset.addEventListener('click', () => {
        const def = String(defaultsOf(mid).args || '');
        const el = panel.querySelector('[data-key="args"]');
        if (el) el.value = def;
        saveBuiltinCfg(mid, { args: def });
        syncReset();
      });
    }
    syncReset();
  }

  // ---------- 任务卡渲染 ----------
  const RESULT_LABEL = {
    ok: ['task-result--ok', '完成'],
    fail: ['task-result--fail', '失败'],
    stopped: ['task-result--skip', '已停止'],
    skip: ['task-result--skip', '已中止'],
  };

  function chipHtml(text, cls) {
    return '<span class="task-chip' + (cls ? ' ' + cls : '') + '">' + esc(text) + '</span>';
  }

  function enabledIds() {
    return state.queue.order.filter((mid) => state.queue.enabled[mid]);
  }

  // 摘要 chips：空闲 = 已存配置投影；运行中 = 队列进度 + 实况阶段；待跑 = 排队中
  function headerChips(mid) {
    const run = state.queueRun;
    if (state.is_running && run && run.current_module === mid) {
      return chipHtml((run.index + 1) + '/' + run.total, 'task-chip--queue')
        + chipHtml(state.currentStage ? '运行中 · ' + state.currentStage : '运行中', 'task-chip--live');
    }
    if (state.is_running && run && enabledIds().indexOf(mid) > run.index) {
      return chipHtml('排队中', 'task-chip--queue') + summaryChips(mid);
    }
    return summaryChips(mid);
  }

  function summaryChips(mid) {
    if (isBuiltin(mid)) return builtinChips(mid);
    const meta = metaOf(mid);
    if (!meta || !meta.summary) return '';
    return meta.summary(state.cfg[mid]).map((s) => chipHtml(s)).join('');
  }

  // 徽章：过期 / 插件资源缺失（数据源 = modules 注册表 + get_status.module_assets）
  function badgeHtml(mid) {
    const m = modOf(mid);
    let html = '';
    if (m && m.expired) html += chipHtml('已过期', 'task-chip--warn');
    if (state.moduleAssets[mid] === false) html += chipHtml('资源缺失', 'task-chip--bad');
    return html;
  }

  function resultHtml(mid) {
    const r = state.roundResult && state.roundResult[mid];
    if (!r || !RESULT_LABEL[r]) return '';
    return '<span class="task-result ' + RESULT_LABEL[r][0] + '">' + RESULT_LABEL[r][1] + '</span>';
  }

  // 细分阶段列表：运行中高亮当前阶段；未运行时点选 = 断点起点（不记忆，重开回第一步）
  function stageListHtml(mid) {
    const m = modOf(mid);
    if (isBuiltin(mid)) {
      const tail = mid === 'close_game'
        ? '排在队尾时，只有整轮正常跑完才会走到它；手动停止或报错会跳过。'
        : '任务按列表顺序执行，失败或手动停止会中止整轮，后面的步骤全部跳过。';
      return '<div class="task-note">这一步没有细分阶段。' + tail + '</div>';
    }
    if (!m || !Array.isArray(m.stages)) return '';
    const runningThis = state.is_running && state.queueRun && state.queueRun.current_module === mid;
    const from = state.breakpoints[mid] || m.stages[0];
    let html = '';
    if (!runningThis) {
      html += '<div class="bp-hint-breakpoint">未运行：点选某一步 = 本次从该步开始（不记忆，重新打开回到第一步）</div>';
    }
    html += '<ul class="breakpoint-list task-stage-list">';
    m.stages.forEach((s, i) => {
      const isCur = runningThis && state.currentStage === s;
      const isFrom = !runningThis && s === from;
      html += '<li class="mra-breakpoint-item' + (isCur ? ' mra-breakpoint-item--current' : '')
        + '" data-stage="' + esc(s) + '">'
        + '<span class="bp-icon">' + (isCur ? MRAIcons.svg('media-play') : '') + '</span>'
        + '<span class="bp-idx">' + (i + 1) + '</span>'
        + '<span class="bp-name">' + esc(s) + '</span>'
        + (isFrom ? '<span class="bp-badge bp-badge--from">从此处开始</span>' : '')
        + '</li>';
    });
    return html + '</ul>';
  }

  function cardHtml(mid) {
    const m = modOf(mid);
    const meta = metaOf(mid);
    const en = !!state.queue.enabled[mid];
    const open = !!state.expanded[mid];
    const runningThis = state.is_running && state.queueRun && state.queueRun.current_module === mid;
    const icon = meta ? meta.icon : esc((m ? m.name : mid).slice(0, 1));
    return '<li class="task-item'
      + (en ? '' : ' task-item--disabled')
      + (open ? ' task-item--open' : '')
      + (runningThis ? ' task-item--running' : '')
      + (state.roundResult && state.roundResult[mid] === 'fail' ? ' task-item--failed' : '')
      + '" data-id="' + esc(mid) + '">'
      + '<div class="task-row">'
      + '<span class="task-grip" title="拖拽排序"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="4" cy="3" r="1.4"/><circle cx="12" cy="3" r="1.4"/><circle cx="4" cy="8" r="1.4"/><circle cx="12" cy="8" r="1.4"/><circle cx="4" cy="13" r="1.4"/><circle cx="12" cy="13" r="1.4"/></svg></span>'
      + '<input type="checkbox" class="task-check" data-act="toggle"' + (en ? ' checked' : '') + ' title="启用 / 停用">'
      + '<span class="task-icon">' + icon + '</span>'
      + '<span class="task-title">' + esc(m ? m.name : mid) + '</span>'
      + '<span class="task-chips">' + headerChips(mid) + badgeHtml(mid) + '</span>'
      + '<span class="task-result-slot">' + resultHtml(mid) + '</span>'
      + '<button type="button" class="task-chev" title="展开 / 收起" aria-expanded="' + open + '"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>'
      + '</div>'
      + '<div class="task-panel">'
      + '<div class="task-panel-label">任务设置' + (state.is_running ? '（运行中锁定）' : '') + '</div>'
      + '<div class="task-options" data-options></div>'
      + '<div class="task-panel-label">细分阶段</div>'
      + '<div data-stages>' + stageListHtml(mid) + '</div>'
      + '</div>'
      + '</li>';
  }

  function renderTaskList() {
    // 选项块是单实例 DOM（index.html 暂存区 ↔ 任务卡面板间搬移）：
    // 重建列表前先搬回暂存区，防 innerHTML 连带销毁已挂绑定的控件
    stashOptionBlocks();
    const ul = $('task-list');
    ul.innerHTML = state.queue.order.map(cardHtml).join('');
    if (!state.queue.order.length) {
      ul.innerHTML = '<li class="task-empty">列表为空——点右上角 ⚙ 添加要运行的模块</li>';
    }
    $('btn-manage').disabled = state.is_running;
    // 步骤：已展开的挂面板，未取过配置的拉一次（摘要 chips 要用）
    state.queue.order.filter(isBuiltin).forEach((mid) => {
      if (state.expanded[mid]) renderBuiltinPanel(mid);
      if (!state.cfg[mid]) ensureConfig(mid).then(() => refreshCard(mid));
    });
  }

  // 轻刷单卡动态区（chips / 结果 / 阶段列表 / 状态类），不重建面板——运行中每 250ms
  // 调用也无所谓，且不打断面板里的选项编辑
  function refreshCard(mid) {
    const item = $('task-list').querySelector('[data-id="' + mid + '"]');
    if (!item) return;
    item.querySelector('.task-chips').innerHTML = headerChips(mid) + badgeHtml(mid);
    item.querySelector('.task-result-slot').innerHTML = resultHtml(mid);
    item.querySelector('[data-stages]').innerHTML = stageListHtml(mid);
    const runningThis = state.is_running && state.queueRun && state.queueRun.current_module === mid;
    item.classList.toggle('task-item--running', runningThis);
    item.classList.toggle('task-item--disabled', !state.queue.enabled[mid]);
    item.classList.toggle('task-item--failed', !!(state.roundResult && state.roundResult[mid] === 'fail'));
  }

  // 配置读取（卡片摘要数据源）：入列/展开/启动后各刷一次
  async function ensureConfig(mid) {
    try {
      state.cfg[mid] = await mra.call('get_module_config', { module_id: mid });
    } catch (e) {
      console.error(e); // 读不到就按缺省摘要显示，不打扰
    }
  }

  // settings.js 保存配置后回调：更新摘要数据源并轻刷对应卡
  function noteConfigSaved(mid, cfg) {
    if (cfg) state.cfg[mid] = cfg;
    refreshCard(mid);
  }

  // ---------- 启用勾选 / 模块管理 ----------
  // 过期模块的强制加入确认：确认后按 force 下发，取消一律不加。
  // 复用通用模态（openModal），Promise 化便于调用点顺序处理。
  function formatValidity(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ''));
    return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(iso || '');
  }

  function confirmExpiredModule(moduleId) {
    const m = modOf(moduleId);
    const until = (m && m.valid_until) ? formatValidity(m.valid_until) : '';
    return new Promise((resolve) => {
      let settled = false;
      const finish = (ok, modal) => {
        if (settled) return;
        settled = true;
        if (modal) modal.close();
        resolve(ok);
      };
      const modal = openModal({
        title: '模块已过期',
        titleColor: 'var(--mra-warning,#F59E0B)',
        bodyHtml:
          '<p class="mra-modal-text">' +
          '此模块已过期，可能无法正常使用！</p>' +
          (until
            ? '<p class="mra-modal-text--sub">' +
              '声明有效期至 ' + until + '。</p>'
            : ''),
        buttons: [
          { text: '取消', onClick: (mm) => finish(false, mm) },
          { text: '仍然加入', primary: true, onClick: (mm) => finish(true, mm) },
        ],
      });
      // 点空白关闭是 openModal 内置行为：一并视为取消（close 在点击时才取值，可安全包一层）
      const origClose = modal.close;
      modal.close = function () { finish(false, null); origClose(); };
    });
  }

  async function pushQueue(order, enabled, force) {
    const resp = await mra.call('set_run_queue', { order, enabled, force: !!force });
    state.queue = resp;
    renderTaskList();
    return resp;
  }

  async function onToggleEnable(mid, checked) {
    if (state.is_running) { renderTaskList(); return; } // 运行中锁定（checkbox 亦已禁用，防御）
    let force = false;
    const m = modOf(mid);
    if (checked && m && m.expired) {
      const ok = await confirmExpiredModule(mid);
      if (!ok) { renderTaskList(); return; } // 取消：回滚勾选视觉
      force = true;
    }
    const enabled = Object.assign({}, state.queue.enabled);
    if (checked) enabled[mid] = true; else delete enabled[mid];
    try {
      await pushQueue(state.queue.order, enabled, force);
      ensureConfig(mid).then(() => refreshCard(mid));
    } catch (e) {
      showError(e.message);
      renderTaskList(); // 后端拒绝：按服务器状态回滚
    }
  }

  // 模块管理弹窗：游戏选卡风格（3 列网格、露 2.5 行、底部淡出）；点卡片加入/移出
  function renderGallery(grid) {
    let cells = state.modules.map((m) => {
      const inList = state.queue.order.includes(m.id);
      const meta = metaOf(m.id);
      const icon = meta ? meta.icon : esc(m.name.slice(0, 1));
      const art = meta ? meta.art : FALLBACK_ART;
      return '<button type="button" class="task-gallery-cell' + (inList ? ' task-gallery-cell--selected' : '')
        + '" data-manage="' + esc(m.id) + '" title="' + (inList ? '点击移出列表' : '点击加入列表') + '">'
        + '<span class="task-gallery-art" style="background:' + art + '">' + icon + '</span>'
        + '<i class="task-gallery-scan"></i>'
        + (m.expired ? '<span class="task-gallery-flag">已过期</span>' : '')
        + '<span class="task-gallery-banner">' + esc(m.name) + '</span>'
        + '</button>';
    }).join('');
    // 空槽位补齐 3×3（无交互、无特效），让网格连续、底部淡出有依托
    for (let i = state.modules.length; i < 9; i++) {
      cells += '<span class="task-gallery-cell task-gallery-cell--empty" aria-hidden="true">'
        + '<span class="task-gallery-empty-plus">＋</span></span>';
    }
    grid.innerHTML = cells;
  }

  function openManageModal() {
    if (state.is_running) return; // 运行中列表锁定（勾选/拖拽/增删同一把锁）
    const modal = openModal({
      title: '管理模块',
      maxWidth: 880,
      bodyHtml:
        '<div class="task-gallery-wrap"><div class="task-gallery" id="manage-grid"></div></div>'
        + '<p class="mra-modal-text--sub" style="margin:12px 0 0">点选卡片加入下方运行任务列表（排到末尾）；移除不影响该模块已保存的配置。</p>',
      buttons: [{ text: '完成', primary: true, onClick: (m) => m.close() }],
    });
    const grid = modal.card.querySelector('#manage-grid');
    renderGallery(grid);
    grid.addEventListener('click', async (e) => {
      const cell = e.target.closest('[data-manage]');
      if (!cell) return;
      const mid = cell.dataset.manage;
      let order;
      let enabled = Object.assign({}, state.queue.enabled);
      let force = false;
      if (!state.queue.order.includes(mid)) {
        const m = modOf(mid);
        if (m && m.expired) {
          const ok = await confirmExpiredModule(mid);
          if (!ok) return;
          force = true;
        }
        order = state.queue.order.concat([mid]);
        enabled[mid] = true;
      } else {
        order = state.queue.order.filter((x) => x !== mid);
        delete enabled[mid];
      }
      try {
        state.queue = await mra.call('set_run_queue', { order, enabled, force });
        renderTaskList();
        // 就地切类（不重建网格）：重建会让节点一出生就是终态，色带涌起与头图推近的过渡就没了
        const inList = state.queue.order.includes(mid);
        cell.classList.toggle('task-gallery-cell--selected', inList);
        cell.title = inList ? '点击移出列表' : '点击加入列表';
      } catch (err) {
        showError(err.message);
      }
    });
  }

  // ---------- 断点起点 ----------
  async function onPickBreakpoint(mid, stage) {
    if (state.is_running) return; // 运行中断点不可改（后端同拒）
    try {
      const resp = await mra.call('set_start_from', { module_id: mid, stage });
      state.breakpoints = resp.breakpoints || {};
      const item = $('task-list').querySelector('[data-id="' + mid + '"]');
      if (item) item.querySelector('[data-stages]').innerHTML = stageListHtml(mid);
    } catch (e) {
      showError(e.message);
    }
  }

  // ---------- 展开面板 ----------
  function toggleExpand(mid, item) {
    state.expanded[mid] = !state.expanded[mid];
    item.classList.toggle('task-item--open', state.expanded[mid]);
    item.querySelector('.task-chev').setAttribute('aria-expanded', String(state.expanded[mid]));
    if (state.expanded[mid]) {
      if (isBuiltin(mid)) {
        // 步骤：面板由前端生成，且不接管数据/设置页（那两页是活动模块的）
        ensureConfig(mid).then(() => { renderBuiltinPanel(mid); refreshCard(mid); });
        return;
      }
      refreshModuleOptions(mid);                    // 模块选项块搬入面板 + 读配置
      ensureConfig(mid).then(() => refreshCard(mid)); // 摘要 chips 跟进最新配置
      renderModulePages(mid);                        // 数据/设置页跟随最后展开的卡片
    }
  }

  // ---------- 拖拽排序（指针驱动 + FLIP 让位预览） ----------
  // 不用 HTML5 原生拖拽：WebView2 里拖拽的 dragover 是否触发取决于载荷与宿主设置，
  // 一旦不触发就表现为「拖了没反应」（顺序从头到尾没变）。指针事件是同一套交互的
  // 确定性实现：按下手柄 → 移动时按指针所在卡片算落点 → 松开提交。
  let dragId = null;
  let liveOrder = null;
  let dragCommitted = false;
  const listEl = $('task-list');

  function captureRects() {
    const map = new Map();
    listEl.querySelectorAll('.task-item').forEach((n) => map.set(n.dataset.id, n.getBoundingClientRect()));
    return map;
  }
  // FLIP：先记旧位，DOM 移动后从位移差反向起步、过渡归零——让位动画的内核
  function flip(oldRects) {
    listEl.querySelectorAll('.task-item').forEach((n) => {
      if (n.dataset.id === dragId) return; // 被拖卡自身不参与（它的半透明占位就是落点预览）
      const old = oldRects.get(n.dataset.id);
      if (!old) return;
      const now = n.getBoundingClientRect();
      const dy = old.top - now.top;
      if (!dy) return;
      n.style.transition = 'none';
      n.style.transform = 'translateY(' + dy + 'px)';
      requestAnimationFrame(() => {
        n.style.transition = 'transform .24s cubic-bezier(0.2, 0.9, 0.25, 1)';
        n.style.transform = '';
        setTimeout(() => { n.style.transition = ''; }, 280);
      });
    });
  }
  function moveLive(id, toIndex) {
    const from = liveOrder.indexOf(id);
    if (from < 0 || from === toIndex) return;
    const oldRects = captureRects();
    liveOrder.splice(from, 1);
    liveOrder.splice(toIndex, 0, id);
    // 只移动现有节点（不重建），展开态/运行态样式原样保留
    liveOrder.forEach((x) => listEl.appendChild(listEl.querySelector('[data-id="' + x + '"]')));
    flip(oldRects);
  }

  function endDragVisual() {
    listEl.classList.remove('dragging-mode');
    listEl.querySelectorAll('.task-item--dragging').forEach((n) => n.classList.remove('task-item--dragging'));
  }

  // 一次拖拽只提交一次；顺序没变就不提交（点一下手柄不该产生一次保存）
  async function commitDragOrder() {
    if (dragCommitted) return;
    dragCommitted = true;
    const finalOrder = liveOrder;
    const wasDragging = !!dragId;
    dragId = null;
    liveOrder = null;
    endDragVisual();
    if (!wasDragging || !finalOrder) return;
    if (finalOrder.join() === state.queue.order.join()) return;
    try {
      state.queue = await mra.call('set_run_queue', { order: finalOrder, enabled: state.queue.enabled });
      renderTaskList();
    } catch (e) {
      showError('保存排序失败: ' + e.message);
      renderTaskList(); // 后端拒绝：按服务器状态回滚
    }
  }

  listEl.addEventListener('pointerdown', (e) => {
    if (state.is_running || e.button !== 0) return;
    const item = e.target.closest('.task-item');
    if (!item || !e.target.closest('.task-grip')) return;
    e.preventDefault();               // 别让按下变成选中文字/触发点击展开
    dragId = item.dataset.id;
    dragCommitted = false;
    liveOrder = state.queue.order.slice();
    listEl.classList.add('dragging-mode');
    item.classList.add('task-item--dragging');
    const grip = item.querySelector('.task-grip');
    if (grip && grip.setPointerCapture) {
      try { grip.setPointerCapture(e.pointerId); } catch (_) { /* 指针已失效：忽略 */ }
    }
  });

  document.addEventListener('pointermove', (e) => {
    if (!dragId) return;
    const hit = document.elementFromPoint(e.clientX, e.clientY);
    const target = hit && hit.closest ? hit.closest('.task-item') : null;
    if (!target || target.dataset.id === dragId) return;
    const without = liveOrder.filter((x) => x !== dragId);
    const r = target.getBoundingClientRect();
    const after = e.clientY > r.top + r.height / 2;
    moveLive(dragId, without.indexOf(target.dataset.id) + (after ? 1 : 0));
  });

  document.addEventListener('pointerup', () => { if (dragId) commitDragOrder(); });
  document.addEventListener('pointercancel', () => { if (dragId) commitDragOrder(); });

  // ---------- 任务卡事件（展开 / 勾选 / 断点） ----------
  // 头栏任意空白处点击 = 展开/折叠（勾选框与拖拽手柄除外；箭头不再单独处理，
  // 点击它同样冒泡到这一层，避免双触发）
  listEl.addEventListener('click', (e) => {
    const item = e.target.closest('.task-item');
    if (!item || !item.dataset.id) return;
    const mid = item.dataset.id;
    const row = e.target.closest('.task-row');
    if (row && !e.target.closest('.task-check') && !e.target.closest('.task-grip')) {
      toggleExpand(mid, item);
      return;
    }
    const stageEl = e.target.closest('[data-stage]');
    if (stageEl && !state.is_running && state.queue.enabled[mid]) {
      onPickBreakpoint(mid, stageEl.dataset.stage);
    }
  });
  listEl.addEventListener('change', (e) => {
    if (!e.target.matches('[data-act="toggle"]')) return;
    const item = e.target.closest('.task-item');
    onToggleEnable(item.dataset.id, e.target.checked);
  });
  $('btn-manage').addEventListener('click', openManageModal);

  // ---------- 状态轮询 ----------
  // 后台进程可能在我们不知情时消失（收尾崩溃 / 被杀）。此时 RPC 全部失败，
  // 但界面若只是静默重试，状态栏会永远停在最后一次拿到的「停止中...」，
  // 按钮照旧可点却毫无反应——观感就是「停不下来」。连续失败到阈值即明说断连。
  const POLL_FAIL_LIMIT = 4;   // 4 次 × 250ms ≈ 1 秒问不到就认定断开
  let _pollFails = 0;
  let _offline = false;

  function markOffline() {
    if (_offline) return;
    _offline = true;
    setStatus('后台已断开（进程已退出），请重启程序', 'error');
    $('btn-start').disabled = true;
    $('btn-stop').disabled = true;
  }

  async function pollStatus() {
    try {
      const d = await mra.call('get_status');
      _pollFails = 0;
      _offline = false;
      renderStatus(d);
      const runState = !!(d.is_running || d.worker_active);
      if (runState !== state._lastRunState) {
        state._lastRunState = runState;
        updateModuleOptionsDisabled(runState);
        if (runState) {
          // 轮次受理：断点已被后端消费、上一轮结局清零——卡片切到 排队中/运行中 形态
          state.breakpoints = {};
          state.roundResult = null;
          renderTaskList();
        } else {
          renderTaskList(); // 轮次结束：各卡显示结局标记
        }
        if (MRA.panelModuleId) refreshModuleOptions(MRA.panelModuleId);
      }
      // 录制实况每秒刷一次：帧数在涨，只在状态跳变时刷是看不出进度的
      if (runState && MRA.panelModuleId === 'speedrush'
          && Date.now() - state._lastRecRefresh >= 1000) {
        state._lastRecRefresh = Date.now();
        refreshSpeedrushStatus();
      }
      // 运行中当前模块/阶段变化 → 轻刷新对应卡（不重建面板，不打断选项编辑）
      if (runState && d.queue_run) {
        const key = d.current_module + '|' + d.current_stage + '|' + d.queue_run.index;
        if (key !== state._lastLiveKey) {
          state._lastLiveKey = key;
          const prev = state._lastLiveModule;
          refreshCard(d.current_module);
          if (prev && prev !== d.current_module) refreshCard(prev); // 上一张卡从「运行中」换回「排队中」
          state._lastLiveModule = d.current_module;
        }
      }
    } catch (e) {
      console.error(e);
      if (++_pollFails >= POLL_FAIL_LIMIT) markOffline();
    } finally {
      setTimeout(pollStatus, 250);
    }
  }

  function setStatus(text, mode) {
    const dot = $('status-dot');
    const txt = $('status-text');
    txt.textContent = text;
    // 运行期文字带扫描光效，结束（就绪/停止中/出错）恢复常态；样式见 style.css「通用文字扫描光效」
    txt.classList.toggle('mra-text-scan', mode === 'running');
    dot.className = 'mra-status-dot' +
      (mode === 'running' ? ' mra-status-dot--running mra-status-dot--pulse' :
       mode === 'ready' ? ' mra-status-dot--ready' :
       mode === 'stopping' ? ' mra-status-dot--stopping' :
       mode === 'error' ? ' mra-status-dot--error' : '');
  }

  function renderStatus(d) {
    state.is_running = d.is_running;
    state.queueRun = d.queue_run || null;
    state.currentModule = d.current_module || '';
    state.currentStage = d.current_stage || '';
    if (d.module_assets) state.moduleAssets = d.module_assets;
    if (d.round_result !== undefined) state.roundResult = d.round_result;
    $('btn-start').disabled = d.is_running || d.worker_active;
    $('btn-stop').disabled = !(d.is_running || d.worker_active);
    if (d.current_stage) {
      setStatus('运行中 · 当前: ' + d.current_stage, 'running');
    } else if (d.is_running || d.worker_active) {
      setStatus('停止中...', 'stopping');
    } else if (d.model_ok === false) {
      // 资源缺失由轮询字段持续点亮（get_status.model_ok），资源恢复后自然回到就绪
      setStatus('插件资源缺失', 'error');
    } else {
      setStatus('系统就绪', 'ready');
    }
    renderPerfCard(d.perf);
  }

  // ---------- 性能卡渲染 ----------
  // 前端只呈现：资源面磁贴由 core 采样器供数，模块行按后端下发的声明行渲染。
  // 不认识任何模块——新增模块只声明行，这里零改动。
  const PERF_LEVEL_RANK = { ok: 0, idle: 1, warn: 2, error: 3 };
  const PERF_HEALTH_TEXT = { ok: '运行正常', warn: '偏高', error: '异常', idle: '待运行' };
  let _perfSampleId = null;

  function setPerfText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;   // textContent：后端文案不进 HTML 通道
  }

  function pushPerfHist(key, value) {
    const h = state.perfHist[key];
    if (!h || !Number.isFinite(value)) return;
    h.push(value);
    if (h.length > state.PERF_HIST_MAX) h.shift();
  }

  function drawSpark(id, hist, floor) {
    const svg = $(id);
    if (!svg) return;
    if (hist.length < 2) { svg.textContent = ''; return; }
    const top = Math.max(floor || 1, hist.reduce((a, b) => (b > a ? b : a), 0));
    let pts = '';
    for (let i = 0; i < hist.length; i++) {
      const x = (i / (hist.length - 1)) * 100;
      const y = 24 - Math.min(1, hist[i] / top) * 22;
      pts += x.toFixed(2) + ',' + y.toFixed(2) + ' ';
    }
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('points', pts.trim());
    svg.textContent = '';
    svg.appendChild(line);
  }

  // 模块行：结构由声明决定（一次会话内不变），只在 key 序列变化时重建 DOM。
  // 显示串由后端格式化（含单位与百分比），前端不再算——避免两处各算一套。
  function renderPerfRows(mid, rows) {
    const box = $(mid + '-perf-rows');
    if (!box) return;
    const sig = rows.map((r) => r.key).join(',');
    if (box.dataset.sig !== sig) {
      box.textContent = '';
      rows.forEach((row) => {
        const el = document.createElement('div');
        el.className = 'perf-row';
        el.dataset.key = row.key;
        const label = document.createElement('span');
        label.className = 'perf-row-label';
        label.textContent = row.label;
        const val = document.createElement('span');
        val.className = 'perf-row-val';
        el.appendChild(label);
        el.appendChild(val);
        box.appendChild(el);
      });
      box.dataset.sig = sig;
    }
    rows.forEach((row) => {
      const el = box.querySelector('.perf-row[data-key="' + row.key + '"]');
      if (!el) return;
      const val = el.querySelector('.perf-row-val');
      val.textContent = row.display;
      val.className = 'perf-row-val'
        + (row.level === 'warn' ? ' perf-row-val--warn' : '')
        + (row.level === 'error' ? ' perf-row-val--error' : '');
      // 建议只在真出问题时给，正常态不塞文案
      if (row.hint && (row.level === 'warn' || row.level === 'error')) {
        el.title = row.hint;
      } else {
        el.removeAttribute('title');
      }
    });
  }

  // 右上角健康度：取所有行里最差的一档，坏行多于一个时补计数。
  function renderPerfHealth(mid, rows) {
    const el = $(mid + '-perf-health');
    if (!el) return;
    let worst = 'ok';
    let bad = 0;
    rows.forEach((row) => {
      const lv = row.reason ? 'error' : (row.level || 'ok');
      if (PERF_LEVEL_RANK[lv] > PERF_LEVEL_RANK[worst]) worst = lv;
      if (lv === 'warn' || lv === 'error') bad += 1;
    });
    const level = rows.length ? worst : 'idle';
    let text = PERF_HEALTH_TEXT[level] || '待运行';
    if (bad > 1) text += ' ' + bad + ' 项';
    el.className = 'perf-health perf-health--' + level;
    el.textContent = '';
    const dot = document.createElement('span');
    dot.className = 'perf-health-dot';
    el.appendChild(dot);
    el.appendChild(document.createTextNode(text));
  }

  // 线程明细：默认折叠，折叠只影响显示（采集与落盘始终全量）。
  // 没贴中文别名的线程显示原始线程名——core 不认识业务，不会替它翻译。
  function renderPerfThreads(mid, threads) {
    const rows = (threads && threads.rows) || [];
    const meta = $(mid + '-perf-threads-meta');
    const box = $(mid + '-perf-thread-rows');
    const toggle = $(mid + '-perf-threads');
    if (meta) meta.textContent = rows.length ? rows.length + ' 个' : '';
    if (toggle && toggle.dataset.bound !== '1') {
      toggle.dataset.bound = '1';
      toggle.addEventListener('click', () => {
        const body = $(mid + '-perf-threads-body');
        if (!body) return;
        const open = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
        body.hidden = open;
      });
    }
    if (!box) return;
    box.textContent = '';
    rows.forEach((row) => {
      const el = document.createElement('div');
      el.className = 'perf-trow';
      const name = document.createElement('span');
      name.className = 'perf-tname';
      name.textContent = row.label || row.name;
      if (row.label) name.title = row.name;      // 原始名放悬停，不占版面
      const bar = document.createElement('span');
      bar.className = 'perf-tbar';
      const fill = document.createElement('i');
      const pct = Number.isFinite(row.cpu_pct)
        ? Math.min(100, Math.max(0, row.cpu_pct)) : null;
      fill.style.width = (pct === null ? 0 : pct) + '%';
      if (pct !== null && pct >= 15) fill.className = 'hot';
      bar.appendChild(fill);
      const cpu = document.createElement('span');
      cpu.className = 'perf-tnum';
      cpu.textContent = pct === null ? '—' : pct.toFixed(0) + '%';
      const hz = document.createElement('span');
      hz.className = 'perf-tnum';
      hz.textContent = Number.isFinite(row.hz) ? row.hz.toFixed(0) + '/秒' : '—';
      el.appendChild(name);
      el.appendChild(bar);
      el.appendChild(cpu);
      el.appendChild(hz);
      box.appendChild(el);
    });
    if (threads && threads.hidden > 0) {
      const more = document.createElement('div');
      more.className = 'perf-tmore';
      more.textContent = '其余 ' + threads.hidden + ' 个未列出';
      box.appendChild(more);
    }
  }

  function renderPerfCard(perf) {
    const mid = MRA.panelModuleId;
    const c = (perf && perf.cpu) || {};
    const sys = (perf && perf.sys) || {};
    const rows = (perf && perf.rows) || [];

    // 资源面磁贴（整机 / 本程序）
    const hostRaw = sys.cpu_pct;
    const hostPct = (typeof hostRaw === 'number' && Number.isFinite(hostRaw))
      ? hostRaw : NaN;
    setPerfText(mid + '-perf-host',
      Number.isFinite(hostPct) ? String(Math.round(hostPct)) : '--');
    const selfPct = perf && c.available ? (c.load_p50 || 0) * 100 : NaN;
    setPerfText(mid + '-perf-self',
      Number.isFinite(selfPct) ? String(Math.round(selfPct)) : '--');
    if (Number.isFinite(c.cores)) {
      setPerfText(mid + '-perf-host-label', '整机占用 · ' + c.cores + ' 核');
    }

    // 走势只在采样号变化时推进：4Hz 轮询 / 1Hz 采样，否则同一个值被塞四次，曲线被拉平
    const sid = perf ? perf.sample_id : null;
    if (sid !== null && sid !== undefined && sid !== _perfSampleId) {
      _perfSampleId = sid;
      pushPerfHist('host', hostPct);
      pushPerfHist('self', selfPct);
    }
    drawSpark(mid + '-perf-host-spark', state.perfHist.host, 20);
    drawSpark(mid + '-perf-self-spark', state.perfHist.self, 10);

    renderPerfRows(mid, rows);
    renderPerfHealth(mid, rows);
    renderPerfThreads(mid, perf && perf.threads);
  }

  // ---------- 运行控制 ----------
  // 三秒倒计时：按钮双层内容形变（样式见 style.css 的 mra-start-* 区块），JS 只切类与编排；
  // 数字滚动是纯 CSS 数列，3s 与其 animation 周期配对，改一处必须同步另一处。
  let _startCountdownTimer = null;

  function _cancelStartCountdown() {
    if (_startCountdownTimer) {
      clearTimeout(_startCountdownTimer);
      _startCountdownTimer = null;
    }
    const btn = $('btn-start');
    btn.classList.remove('mra-start--counting');
    btn.removeAttribute('aria-label');
  }

  async function _doStart() {
    try {
      await mra.call('start', {});
      // 断点消费即清（与后端同口径）：本次计划的起点已用掉，下次回从头跑
      state.breakpoints = {};
    } catch (e) {
      console.error(e);
      // 缺少 ViGEmBus 驱动：弹出下载引导（而非仅 toast）
      // （showVigemDialog 在 app.js 定义、晚于本文件加载，故经 MRA 延迟取用）
      if (e && e.message && e.message.indexOf('VIGEM_BUS_MISSING') !== -1) {
        MRA.showVigemDialog(e.message);
        return;
      }
      reportError('toast', '启动失败: ' + e.message);
    }
  }

  $('btn-start').onclick = () => {
    if (state.is_running) return;
    // 倒计时进行中再次点击 = 取消
    if (_startCountdownTimer) {
      _cancelStartCountdown();
      return;
    }
    const btn = $('btn-start');
    btn.classList.add('mra-start--counting');
    btn.setAttribute('aria-label', '即将启动，倒计时 3 秒，再点一次取消');
    _startCountdownTimer = setTimeout(() => {
      _startCountdownTimer = null;
      btn.classList.remove('mra-start--counting');
      btn.removeAttribute('aria-label');
      _doStart(); // 倒计时结束才真正启动
    }, 3000);
  };

  $('btn-stop').onclick = async () => {
    try {
      await mra.call('stop');
    } catch (e) {
      console.error(e);
      showError('后台已断开，停止指令没能送达');
    }
  };

  // ---------- 导出到 window.MRA ----------
  Object.assign(window.MRA, {
    renderTaskList, pollStatus, setStatus, noteConfigSaved,
  });
})();
