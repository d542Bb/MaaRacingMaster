// MaaRM shell 前端 —— 模块专属选项、toggle 开关、环境中心、模块「数据/设置」页
// 模板渲染与控件绑定、今日看板。跨文件调用经 window.MRA（见文件末尾导出）。
(function () {
  'use strict';

  // 共享物导入（js/rpc.js、js/modal.js、js/preview.js 已先行加载）
  const {
    mra, $, state, showError, reportError, setInlineStatus, postWindowAction,
    openModal, showGamepadNotice,
    setPreviewPlayState, enterPreviewFullscreen, exitPreviewFullscreen,
  } = window.MRA;

  // -------- 模块专属选项（treasure：循环上限 / 目标场次；speedrush：演示录制开关）--------
  // 规则：按 MODULE_OPTION_BLOCKS 显示对应块；控件改动立即写 sidecar 的配置槽，
  // 下次「开始」注入新实例——不做运行中热更新，运行期间控件锁定（见 updateModuleOptionsDisabled）。
  let _optListenersBound = false;
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
  function bindModuleOptionsUI() {
    if (_optListenersBound) return;
    _optListenersBound = true;
    // 录制开关：绑在 treasure 控件的卫语句之前——那些控件缺失时不得连带漏绑
    const recToggle = $('opt-record-mode');
    if (recToggle) recToggle.addEventListener('click', onRecordToggleClick);
    const loops = $('opt-max-loops');
    const sessionHost = $('opt-target-session');
    if (!loops) return;
    // 数字输入：回车/失焦才发，避免每打一个数字 RPC 一次
    loops.addEventListener('change', onModuleOptionsInputChange);
    loops.addEventListener('keydown', (e) => { if (e.key === 'Enter') onModuleOptionsInputChange.call(loops, e); });
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

  // 有专属选项块的模块登记表（新增模块：这里加一行 + index.html 加对应块，别处不再改分支）。
  // refresh 直接放函数引用——函数声明会提升，所以本表可以在函数定义之前求值。
  const MODULE_OPTION_BLOCKS = {
    treasure: { block: 'opt-treasure-block', refresh: refreshTreasureOptions },
    speedrush: { block: 'opt-speedrush-block', refresh: refreshSpeedrushOptions },
  };

  // 按当前模块分派：先切换块的显隐，再刷该模块的选项与实况
  async function refreshModuleOptions(moduleId) {
    const host = $('module-options');
    if (!host) return;
    const def = MODULE_OPTION_BLOCKS[moduleId];
    // 未登记专属选项的模块：整块隐藏
    if (!def) {
      host.style.display = 'none';
      setOptionsStatus('', '');
      return;
    }
    host.style.display = 'block';
    Object.keys(MODULE_OPTION_BLOCKS).forEach((mid) => {
      const el = $(MODULE_OPTION_BLOCKS[mid].block);
      if (el) el.style.display = mid === moduleId ? 'block' : 'none';
    });
    setOptionsStatus('读取配置中...', '');
    try {
      await def.refresh();
    } catch (e) {
      console.error(e);
      reportError('inline', '读取配置失败: ' + (e.message || e));
    }
  }

  // 鉴宝专属选项（循环上限 / 目标场次）
  async function refreshTreasureOptions() {
    const loopsEl = $('opt-max-loops');
    const cfg = await mra.call('get_module_config', { module_id: 'treasure' });
    // --- 填「循环次数上限」 ---
    if (loopsEl) {
      const v = typeof cfg.max_daily_loops === 'number' ? cfg.max_daily_loops : 50;
      loopsEl.value = String(v);
    }
    // --- 填「目标场次」：intern / expert / master（后端权威值） ---
    setTargetSessionOnUI((cfg && cfg.target_session) ? cfg.target_session : 'master');
    // 运行实况提示（只读；_state 由运行实例提供，未运行时没有）
    const st = (cfg && cfg._state) || null;
    if (!st || !state.is_running) {
      setOptionsStatus('', '');
      return;
    }
    const done = Number.isFinite(st.done_count_state) ? st.done_count_state : 0;
    const ocr = (st.done_count_ocr === null || st.done_count_ocr === undefined) ? '--' : st.done_count_ocr;
    const lim = Number.isFinite(st.effective_limit) ? st.effective_limit : 50;
    const bucket = (st && st.daily_bucket) ? String(st.daily_bucket) : '';
    // 运行中：选项已锁定，展示实况
    setOptionsStatus(
      `运行中：上方选项已锁定（改配置请先停止）。今日（${bucket} 05:00 起）已完成 ${done} 场（OCR读到 ${ocr}），刷到第 ${lim} 场为止。`,
      done >= lim ? 'warn' : 'ok'
    );
  }

  // 极速狂飙专属选项（演示数据录制开关）
  async function refreshSpeedrushOptions() {
    const btn = $('opt-record-mode');
    const cfg = await mra.call('get_module_config', { module_id: 'speedrush' });
    if (btn) setToggle(btn, !!(cfg && cfg.record_mode));
    renderSpeedrushStatus(cfg);
  }

  // 录制实况（只读）。_state 来自运行实例：未运行、或在导航阶段（录制只在驾驶阶段开写）时
  // 都没有"正在录"的实况，此时按开关状态给出准确的一句话。
  function renderSpeedrushStatus(cfg) {
    const st = (cfg && cfg._state) || null;
    const on = !!(cfg && cfg.record_mode);
    if (st && st.recording) {
      setOptionsStatus(
        `录制中：已写 ${st.frames} 帧 · 丢帧 ${st.frames_dropped} · 手柄样本 ${st.pad_samples}；`
        + `目录 ${st.demos_dir}`, 'ok'
      );
      return;
    }
    if (state.is_running) {
      setOptionsStatus(
        on ? '运行中：录制已开启，进入驾驶阶段后开始写入'
           : '运行中：本次未开启录制（开关改动在下次「开始」时生效）',
        on ? '' : 'warn'
      );
      return;
    }
    setOptionsStatus('', '');
  }

  // 运行中周期性刷新录制实况：只更新状态行，不动开关、不打"读取中"（避免每秒闪一下）
  async function refreshSpeedrushStatus() {
    try {
      const cfg = await mra.call('get_module_config', { module_id: 'speedrush' });
      renderSpeedrushStatus(cfg);
    } catch (e) {
      console.error(e); // 轮询失败不打扰用户，下次再试
    }
  }

  // 录制开关：只写 sidecar 缓存（下次 start 注入），不做运行中热更新
  async function onRecordToggleClick() {
    const btn = $('opt-record-mode');
    if (!btn || btn.disabled || _optionsSaving) return;
    const on = !toggleState(btn);
    setToggle(btn, on); // 先翻转视觉状态
    _optionsSaving = true;
    setOptionsStatus('保存中...', '');
    try {
      const resp = await mra.call('set_module_config', {
        module_id: 'speedrush',
        config: { record_mode: on },
      });
      const saved = !!(resp && resp.record_mode);
      setToggle(btn, saved); // 回显后端最终值
      setOptionsStatus(
        saved ? '已开启录制：下次「开始」后，驾驶阶段请自行手动驾驶'
              : '已关闭录制（下次「开始」时生效）',
        'ok'
      );
    } catch (e) {
      console.error(e);
      setToggle(btn, !on); // 回滚
      reportError('inline', '保存失败: ' + (e.message || e));
    } finally {
      _optionsSaving = false;
    }
  }

  // 行内状态的渲染实现已收敛进 js/rpc.js 的 setInlineStatus（错误反馈通道协议：
  // 错误一律走 reportError('inline', ...)；本函数只留非错误反馈——保存中/已保存/运行实况）
  function setOptionsStatus(text, level) {
    setInlineStatus($('module-options-status'), text, level);
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

    // ② 目标场次（校验 intern/expert/master）
    const sessionVal = getTargetSessionFromUI();

    _optionsSaving = true;
    setOptionsStatus('保存中...', '');
    try {
      const resp = await mra.call('set_module_config', {
        module_id: 'treasure',
        config: {
          max_daily_loops: loopsVal,
          target_session: sessionVal,
        },
      });
      // 写回成功：回显最终值
      const savedLoops = (resp && Number.isFinite(resp.max_daily_loops)) ? resp.max_daily_loops : loopsVal;
      const savedSession = (resp && resp.target_session && VALID_SESSIONS.has(resp.target_session))
        ? resp.target_session : sessionVal;
      setTargetSessionOnUI(savedSession);
      const sessionLabel = SESSION_LABELS[savedSession] || '大师场';
      const loopsTip = savedLoops === 0
        ? '不指定场数，按游戏默认 50 场'
        : '今日刷到第 ' + savedLoops + ' 场为止';
      setOptionsStatus(
        `已保存：目标场次「${sessionLabel}」，${loopsTip}（下次「开始」时生效）`,
        'ok'
      );
    } catch (e) {
      console.error(e);
      reportError('inline', '保存失败: ' + (e.message || e));
    } finally {
      _optionsSaving = false;
    }
  }

  // 运行中锁定可选项
  function updateModuleOptionsDisabled(running) {
    const loopsEl = $('opt-max-loops');
    if (loopsEl) loopsEl.disabled = running;
    setSessionSegmentedDisabled(running);
    // 录制开关：运行中锁定（配置不做热更新，改动在下次「开始」时生效）
    const recToggle = $('opt-record-mode');
    if (recToggle) recToggle.disabled = running;
    // 模块下拉：运行中禁止切换（后端同时拒绝，这里把入口也关掉，让"能不能切"一眼可见）
    const sel = $('module-select');
    if (sel) {
      sel.disabled = running;
      sel.title = running ? '运行中不允许切换活动模块，请先停止' : '';
      if (MRA.syncModuleSelect) MRA.syncModuleSelect(); // 自绘下拉投影跟进（js/select.js）
    }
  }

  // ---------- 注册表权限优化（启动体检 + 环境中心「权限优化」tab） ----------
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

  // ---------- 环境中心：可选依赖 tab ----------
  // 收录判据：缺失不阻断核心功能、降级路径一句话说得清、修复动作可引导；硬前置不进。
  // 条目两类来源：字体由前端自检（document.fonts.check 是渲染层真源）；
  // 驱动由侧车检测（get_optional_dependencies，与启动拦截 VIGEM_BUS_MISSING 同源）。
  // 驱动类只引导到官方发布页，不代装；外链走逻辑目标名白名单（open_external_url）。
  function fontDepItemHtml(ready) {
    const actions = ready
      ? '<span class="opt-item-note" style="margin-top:0">当前界面正在使用该字体</span>'
      : '<button type="button" class="opt-btn opt-btn--primary dep-guide" data-target="noto_font">打开下载页</button>';
    return '<div class="opt-item ' + (ready ? 'opt-item--done' : 'opt-item--todo') + '">'
      + '<div class="opt-item-title">界面字体 Noto Sans SC（思源黑体）</div>'
      + '<div class="opt-item-effect">思源字形：小字号更锐利，500/600 为真实字重，界面文字更清爽。'
      + '未安装时界面回落微软雅黑，功能不受影响。</div>'
      + '<details class="opt-details"><summary>技术细节</summary><div class="opt-details-body">'
      + '<p>检测方式：页面内 document.fonts.check（渲染层真源，当前状态即实探结果）。</p>'
      + '<p>降级路径：字体栈回落 Microsoft YaHei UI。</p>'
      + '<p>后果：仅影响观感；安装到系统后重启本程序生效。</p>'
      + '</div></details>'
      + '<div class="opt-item-foot"><span class="opt-pill opt-pill--' + (ready ? 'done' : 'todo') + '">'
      + (ready ? '已就绪' : '未检测到') + '</span>'
      + '<span class="opt-actions">' + actions + '</span></div>'
      + '</div>';
  }

  function depItemHtml(it) {
    const ready = it.state === 'ready';
    const actions = ready
      ? '<span class="opt-item-note" style="margin-top:0">当前可用</span>'
      : '<button type="button" class="opt-btn opt-btn--primary dep-guide" data-target="' + it.guide_target + '">'
        + (it.guide_label || '打开官方页') + '</button>';
    return '<div class="opt-item ' + (ready ? 'opt-item--done' : 'opt-item--todo') + '">'
      + '<div class="opt-item-title">' + it.name + '</div>'
      + '<div class="opt-item-effect">' + it.effect + '</div>'
      + '<details class="opt-details"><summary>技术细节</summary><div class="opt-details-body">'
      + '<p>降级路径：' + it.degrade + '</p>'
      + '<p class="opt-path">官方页：' + it.official + '</p>'
      + '<p>后果：' + it.detail + '</p>'
      + '</div></details>'
      + '<div class="opt-item-foot"><span class="opt-pill opt-pill--' + (ready ? 'done' : 'todo') + '">'
      + (ready ? '已就绪' : '未检测到') + '</span>'
      + '<span class="opt-actions">' + actions + '</span></div>'
      + '</div>';
  }

  async function renderDepList(depEl) {
    let items;
    try {
      const d = await mra.call('get_optional_dependencies');
      items = d.items || [];
    } catch (e) {
      depEl.innerHTML = '<p class="mra-modal-error">读取可选依赖失败: ' + e.message + '</p>';
      return;
    }
    let fontReady = false;
    try { fontReady = document.fonts.check('12px "Noto Sans SC"'); } catch (e) { fontReady = false; }
    const total = items.length + 1;
    const missing = items.filter((it) => it.state !== 'ready').length + (fontReady ? 0 : 1);
    const summary = missing
      ? '<b class="opt-num--todo">' + missing + '</b> 项缺失（可选项） · <b class="opt-num--done">' + (total - missing) + '</b> 项就绪'
      : '<b class="opt-num--done">' + total + '</b> 项可选依赖全部就绪';
    depEl.innerHTML = '<div class="opt-summary"><span class="opt-summary-text">' + summary + '</span></div>'
      + '<div class="dep-note">这里收录的都是可选项：缺失不影响核心功能，只影响体验与可选玩法；'
      + '安装动作只做引导，由你确认后自行完成。</div>'
      + fontDepItemHtml(fontReady)
      + items.map(depItemHtml).join('');
  }

  // 环境中心（设置页入口）：tab「权限优化」（注册表优化，三档汇总 + 一键全部）
  // + tab「可选依赖」（ViGEmBus 驱动 / 界面字体，检测 + 引导）
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
      title: '环境中心',
      maxWidth: 540,
      bodyHtml:
        '<div class="env-tabs">'
        + '<button type="button" class="env-tab env-tab--on" data-pane="pane-perm">权限优化</button>'
        + '<button type="button" class="env-tab" data-pane="pane-dep">可选依赖</button>'
        + '</div>'
        + '<div class="env-pane env-pane--on" id="pane-perm"><div id="opt-summary"></div><div id="opt-center-list"></div></div>'
        + '<div class="env-pane" id="pane-dep"><div id="dep-center-list"></div></div>',
      buttons: [{ text: '关闭', primary: true, onClick: (m) => m.close() }],
    });
    const summaryEl = modal.card.querySelector('#opt-summary');
    const listEl = modal.card.querySelector('#opt-center-list');
    const depEl = modal.card.querySelector('#dep-center-list');
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
        listEl.innerHTML = '<p class="mra-modal-error">刷新失败: ' + e.message + '</p>';
      }
    }
    // 事件委托到 modal.card：覆盖 tab 切换、依赖引导、汇总条(一键全部)与列表(单项)，
    // 列表 innerHTML 重渲染不影响委托
    modal.card.addEventListener('click', async (ev) => {
      const tabBtn = ev.target.closest('.env-tab');
      if (tabBtn) {
        modal.card.querySelectorAll('.env-tab').forEach((b) => b.classList.toggle('env-tab--on', b === tabBtn));
        modal.card.querySelectorAll('.env-pane').forEach((p) => p.classList.toggle('env-pane--on', p.id === tabBtn.dataset.pane));
        return;
      }
      const guideBtn = ev.target.closest('.dep-guide');
      if (guideBtn) {
        try {
          await mra.call('open_external_url', { target: guideBtn.dataset.target });
        } catch (e) {
          showError('打开链接失败: ' + e.message);
        }
        return;
      }
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
    renderDepList(depEl);
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
        '<p class="mra-modal-text">'
        + '<b>' + it.name + '</b>：' + it.effect + '</p>').join('')
        + '<p class="mra-modal-text--sub">'
        + '详情与手动调整见 设置 → 运行环境。</p>',
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
            else showError('已忽略启动提醒，可随时在 设置 → 运行环境 中心重新开启');
            modal.close();
          }
        },
        { text: '暂不', onClick: (modal) => modal.close() },
      ],
    });
  }

  // ---------- 模块「数据/设置」页（按当前模块渲染；两模块共用同一套模板，id 带模块前缀，便于以后差异化）----------
  // 以后给某模块定制页面时，只需把 MODULE_PAGE_DEFS 里该模块的 data/settings 换成专属模板函数。

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
                <span class="board-value" id="${mid}-board-coinnet">--</span>
                <span class="board-label">银币盈亏</span>
              </div>
              <div class="board-item">
                <span class="board-value" id="${mid}-board-egg-total">0</span>
                <span class="board-label">领取彩蛋</span>
                <span class="board-eggs">
                  <b class="board-egg-num board-egg-num--red" id="${mid}-board-egg-red">0</b><b class="board-egg-num board-egg-num--yellow" id="${mid}-board-egg-yellow">0</b><b class="board-egg-num board-egg-num--blue" id="${mid}-board-egg-blue">0</b>
                </span>
              </div>
              <div class="board-item">
                <span class="board-value" id="${mid}-board-score">--</span>
                <span class="board-label">今日积分</span>
              </div>
              <div class="board-item">
                <span class="board-value" id="${mid}-board-games">--</span>
                <span class="board-label" id="${mid}-board-games-wl">场次</span>
              </div>
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
                  ${MRAIcons.svg('circle-dollar-sign')}
                </div>
                <span class="mra-detect-value" id="${mid}-detect-coin">--</span>
                <span class="mra-detect-label">金币</span>
              </div>
              <div class="mra-detect-item">
                <div class="mra-detect-icon mra-detect-icon--obstacle">
                  ${MRAIcons.svg('car')}
                </div>
                <span class="mra-detect-value" id="${mid}-detect-car">--</span>
                <span class="mra-detect-label">障碍车</span>
              </div>
              <div class="mra-detect-item">
                <div class="mra-detect-icon mra-detect-icon--bonus">
                  ${MRAIcons.svg('gift')}
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
    const p = (s) => $('' + MRA.currentModuleId + '-board-' + s);
    const date = p('date');
    if (date) date.textContent = (d.bucket || '') + ' 05:00 起';
    const s = d.summary || {};
    const setNum = (el, v, cls) => { if (!el) return; el.textContent = fmtNum(v); if (cls) el.className = cls; };
    const num = (v) => Number(v) || 0;
    // T0 银币盈亏 = 当日银币净变化 = 我方本场收入（拍中=利润、未拍中=分红）+ 彩蛋任务领取银币。
    // 依据 RULES §3：未拍中不花钱，中标者亏钱时我方按顺位拿 5/10/15% 分红——分红是真收入，
    // 只算「拍中场的利润」会把当天赚到的分红整个漏掉（2026-09-17：漏 61,488）。
    // 逐场收入由落盘侧累加成 daily_summary.income_sum，此处直接读汇总，不在前端重算口径
    //（前端再算一遍＝第二份真相，两处迟早对不上）。
    const eggCoin = num(s.egg_coin);
    const eggScore = num(s.egg_score);
    const high = num(s.highest_score);
    const coinNet = num(s.income_sum) + eggCoin;
    const cn = p('coinnet');
    if (cn) {
      cn.textContent = d.summary ? fmtNum(coinNet) : '--';
      cn.className = 'board-value' + (coinNet > 0 ? ' board-value--pos' : coinNet < 0 ? ' board-value--neg' : '');
    }
    // T0 领取彩蛋（红+黄+蓝）；分项以三色读数并入本卡片，零值淡出突出非零
    setNum(p('egg-total'), num(s.egg_red) + num(s.egg_yellow) + num(s.egg_blue), null);
    const setEgg = (key, v) => {
      const el = p(key);
      if (!el) return;
      el.textContent = fmtNum(v);
      el.classList.toggle('board-egg-num--zero', !num(v));
    };
    setEgg('egg-red', s.egg_red);
    setEgg('egg-yellow', s.egg_yellow);
    setEgg('egg-blue', s.egg_blue);
    // T1 今日积分 = 最高单场（现有口径）+ 领取积分
    const scEl = p('score');
    if (scEl) scEl.textContent = d.summary ? fmtNum(high + eggScore) : '--';
    // T2 场次：胜负合并进标签（次要信息，不占格）
    setNum(p('games'), s.games, 'board-value');
    const wl = p('games-wl');
    if (wl && d.summary) wl.textContent = '场次 · 胜' + num(s.win) + '/负' + num(s.fail);
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
      // 「利」仅在我方拍中（win）时显示（= 我方利润）；fail 场利润是别人的，不展示
      const profitCell = g2.auction_result === 'win'
        ? '<span class="board-row-val">利 ' + fmtNum(g2.profit) + '</span>' : '';
      return '<div class="board-row">' +
        '<span class="board-row-seq">#' + g2.game_seq + '</span>' + res +
        '<span class="board-row-val">收 ' + fmtNum(g2.income) + '</span>' + profitCell +
        '</div>';
    }).join('');
  }

  async function pollTodayBoard() {
    try {
      const page = $('page-data');
      if (page && page.classList.contains('hidden')) return; // 数据页不可见不刷新
      if (MRA.currentModuleId !== 'treasure') return;             // 仅鉴宝有今日看板
      const d = await mra.call('get_today_stats', { module_id: MRA.currentModuleId });
      renderTodayBoard(d);
    } catch (e) { /* 看板轮询失败静默（如库未创建/尚未跑过） */ }
  }

  // 默认（竞速形状）性能卡：YOLO 推理 / 截图耗时 / 当前阶段。
  // 鉴宝不用这套——它没有 YOLO，模板见 treasurePerfCard。
  function perfCardLegacy(mid) {
    return `
        <div class="card card-flex">
          <div class="card-head"><h3>性能监控</h3></div>
          <div class="card-body card-body--fill">
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
        </div>`;
  }

  // 鉴宝性能卡：只放「看得懂 + 看完能行动」的三项，诊断细节一律留在日志。
  // 走势图取近 30 秒（250ms 轮询 × 120 点）；数值本身已是滑窗 p50，曲线看的是趋势。
  function treasurePerfCard(mid) {
    return `
        <div class="card card-flex">
          <div class="card-head"><h3>性能监控</h3></div>
          <div class="card-body card-body--fill">
            <div class="perf-item">
              <div class="perf-head">
                <span class="perf-label">画面响应</span>
                <div class="perf-right">
                  <span class="perf-value" id="${mid}-perf-fps">-- 次/秒</span>
                  <span class="perf-badge perf-badge--idle" id="${mid}-perf-fps-badge"><span class="perf-badge-dot"></span>空闲</span>
                </div>
              </div>
              <svg class="perf-spark" id="${mid}-perf-fps-spark" viewBox="0 0 100 22" preserveAspectRatio="none" aria-hidden="true"></svg>
              <p class="perf-note" id="${mid}-perf-fps-note">启动后显示程序每秒查看画面的次数</p>
            </div>
            <div class="perf-item">
              <div class="perf-head">
                <span class="perf-label">机器负载</span>
                <div class="perf-right">
                  <span class="perf-value" id="${mid}-perf-cpu">--</span>
                  <span class="perf-badge perf-badge--idle" id="${mid}-perf-cpu-badge"><span class="perf-badge-dot"></span>空闲</span>
                </div>
              </div>
              <svg class="perf-spark" id="${mid}-perf-cpu-spark" viewBox="0 0 100 22" preserveAspectRatio="none" aria-hidden="true"></svg>
              <p class="perf-note" id="${mid}-perf-cpu-note">偏高时关闭其它程序，或在设置里关掉调试落盘</p>
            </div>
            <div class="perf-item">
              <div class="perf-head">
                <span class="perf-label">识别健康</span>
                <div class="perf-right">
                  <span class="perf-badge perf-badge--idle" id="${mid}-perf-health-badge"><span class="perf-badge-dot"></span>空闲</span>
                </div>
              </div>
              <p class="perf-note" id="${mid}-perf-health-note">等待识别</p>
            </div>
          </div>
        </div>`;
  }

  function defaultDataCards(mid, perfCardHtml) {
    return `
      <div class="col-left">
        <!-- 性能监控 -->
        ${perfCardHtml || perfCardLegacy(mid)}
      </div>

      <div class="col-right">
        ${mid === 'treasure' ? todayBoardCard(mid) : detectCard(mid)}

        <!-- 实时预览 -->
        <div class="card card-flex preview-card" id="${mid}-preview-card">
          <!-- 悬浮窗接管期间的占位符（三态互斥，见 previewMode） -->
          <div class="preview-away" id="${mid}-preview-away">
            <span>PEEP 离家出走啦~</span>
          </div>
          <div class="card-head">
            <h3>实时预览</h3>
            <div class="log-head-actions">
              <button class="icon-btn" id="${mid}-btn-preview-float" type="button" title="悬浮窗显示（脱离主界面）">
                ${MRAIcons.svg('picture-in-picture-2')}
              </button>
              <button class="icon-btn" id="${mid}-btn-preview-max" type="button" title="全屏">
                <morph-icon reduced-motion="user"></morph-icon>
              </button>
              <button class="icon-btn" id="${mid}-btn-preview-toggle" type="button" title="开始预览">
                ${MRAIcons.svg('media-play', {class: 'icon-play'})}
                ${MRAIcons.svg('media-pause', {class: 'icon-pause'})}
              </button>
            </div>
          </div>
          <div class="preview-wrap">
            <div class="preview-canvas">
              <img class="preview-img" id="${mid}-preview-img" alt="PEEP 实时预览">
              <div class="preview-empty" id="${mid}-preview-empty">
                ${MRAIcons.svg('camera')}
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
          <div class="card-body card-body--col">
            <div class="option-row">
              <button class="mra-toggle" id="${mid}-toggle-debug" role="switch" aria-checked="false"></button>
              <div class="option-main">
                <div class="option-title">DEBUG 每帧截图</div>
                <p class="option-desc">开启后每帧截图保存到 %APPDATA%/MaaRacingMaster/debug/navigate/ 目录，用于分析导航和识别问题</p>
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
                <p class="option-desc">开启后才把运行日志与决策流水（trace.jsonl）写入 %APPDATA%/MaaRacingMaster/logs/&lt;会话&gt;/，两者同放一个会话目录，排查时整个目录发出来即可。每次开启新建一个会话目录，日志按大小轮转、并按会话保留最近若干份；关闭时日志仅保留在界面内存，决策流水不落盘</p>
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
          <div class="card-body card-body--col">
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
                <div class="option-title">退出 MaaRM 程序</div>
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

      </div>

      <div class="col-right">
        <!-- 快捷工具 -->
        <div class="card">
          <div class="card-head"><h3>快捷工具</h3></div>
          <div class="card-body">
            <div class="tool-grid">
              <button class="mra-tool-btn" data-tool="screenshot">
                ${MRAIcons.svg('camera', {class: 'mra-tool-btn-icon'})}
                <span class="mra-tool-btn-label">截图测试</span>
              </button>
              <button class="mra-tool-btn" data-tool="folder">
                ${MRAIcons.svg('folder-open', {class: 'mra-tool-btn-icon'})}
                <span class="mra-tool-btn-label">调试文件夹</span>
              </button>
              <button class="mra-tool-btn" data-tool="template">
                ${MRAIcons.svg('crosshair', {class: 'mra-tool-btn-icon'})}
                <span class="mra-tool-btn-label">模板匹配</span>
              </button>
              <button class="mra-tool-btn" data-tool="cache">
                ${MRAIcons.svg('trash', {class: 'mra-tool-btn-icon'})}
                <span class="mra-tool-btn-label">清空缓存</span>
              </button>
            </div>
          </div>
        </div>

        <!-- 运行环境 -->
        <div class="card">
          <div class="card-head"><h3>运行环境</h3></div>
          <div class="card-body">
            <p class="capture-note" style="margin:0 0 12px;">体检并修复 Windows 层面对自动化运行的干扰（ms-gamebar 弹窗、打字时弹手柄虚拟键盘等，均可单独优化或恢复系统默认，附值路径与后果说明）；可选依赖（手柄驱动、界面字体）的就绪状态与安装引导。均为可选项，缺失不影响核心功能</p>
            <button class="mra-tool-btn mra-tool-btn--block" id="btn-optimizer">
              <span class="mra-tool-btn-label">打开环境中心</span>
            </button>
          </div>
        </div>
      </div>`;
  }

  // 模块 → 页面模板注册表。未登记的模块走 DEFAULT_PAGE_DEFS（默认模板）。
  // 鉴宝的数据页只换性能卡（其余两栏同构），故把卡片 HTML 作参数传入。
  function treasureDataCards(mid) {
    return defaultDataCards(mid, treasurePerfCard(mid));
  }

  // 极速狂飙的数据页：驾驶数据落在会话目录里，界面这边只给入口与说明。
  // **不套用默认数据页**——那两张卡（性能监控、金币/障碍车检测）是别的模块的概念，
  // 摆在这里只会让人以为"检测没跑起来"。
  function speedrushDataCards(mid) {
    return `
      <div class="col-left">
        <div class="card">
          <div class="card-head"><h3>驾驶演示数据</h3></div>
          <div class="card-body">
            <p class="module-desc">录制开关在「主控」页的模块选项里。每段驾驶各写一个会话目录，
              含逐帧图与手柄序列；门控判定的日志带帧号（<code>frame=</code>），按它可回查
              判定时刻的画面。</p>
            <button class="mra-tool-btn mra-tool-btn--block" id="${mid}-open-demos" style="margin-top:12px;">
              <span class="mra-tool-btn-label">打开数据目录</span>
            </button>
          </div>
        </div>
      </div>
      <div class="col-right">
        <div class="card">
          <div class="card-head"><h3>在线指标</h3></div>
          <div class="card-body">
            <p class="module-desc">驾驶控制接入后，这里显示模型输出与几何兜底的接管次数——
              接管频次上升是分布漂移最灵敏的信号。</p>
          </div>
        </div>
      </div>`;
  }

  // 未登记模板的模块用默认模板（此前是"回退到 treasure"，那会让合法但未定制的模块
  // 显示成鉴宝的页面，包括读 treasure.db 的今日看板）
  const DEFAULT_PAGE_DEFS = { data: (mid) => defaultDataCards(mid), settings: defaultSettingsCards };

  const MODULE_PAGE_DEFS = {
    treasure: { data: treasureDataCards, settings: defaultSettingsCards },
    speedrush: { data: speedrushDataCards, settings: defaultSettingsCards },
  };

  // 未选择模块（下拉栏「（空）」或注册表为空）时，「数据/设置」页的占位内容
  const EMPTY_MODULE_HTML =
    '<div class="card"><div class="card-head"><h3>未选择活动模块</h3></div>' +
    '<div class="card-body"><p class="module-desc">请在左上「活动模块」下拉栏中选择一个模块。</p></div></div>';

  // 按模块渲染「数据/设置」页并绑定当前模块的控件事件
  function renderModulePages(moduleId) {
    // 未选择模块：两页只放占位内容、不挂模块控件；currentModuleId 置空后，
    // 轮询里所有带模块前缀的取值都会落空并由各自的判空守卫跳过。
    if (!moduleId) {
      MRA.currentModuleId = '';
      const dataHost = $('page-data');
      const settingsHost = $('page-settings');
      if (dataHost) dataHost.innerHTML = EMPTY_MODULE_HTML;
      if (settingsHost) settingsHost.innerHTML = EMPTY_MODULE_HTML;
      return;
    }
    // 防注入白名单：moduleId 会拼入 HTML 模板（如 id="${mid}-..."），只接受后端注册表
    // 给出的模块 id（get_initial_state 的 modules），非法值退回注册表第一个
    // ——此前是硬编码回退 treasure，那会让合法但未定制的模块显示成鉴宝的页面。
    // （兼断 CodeQL js/xss-through-dom 污点：所有拼进模板的 mid 都出自本表）
    if (!state.modules.some((m) => m && m.id === moduleId)) {
      const first = state.modules.find((m) => m && m.id);
      if (!first) return;
      moduleId = first.id;
    }
    MRA.currentModuleId = moduleId;
    const def = MODULE_PAGE_DEFS[moduleId] || DEFAULT_PAGE_DEFS;
    const dataPage = $('page-data');
    const settingsPage = $('page-settings');
    if (dataPage) dataPage.innerHTML = def.data(moduleId);
    if (settingsPage) settingsPage.innerHTML = def.settings(moduleId);
    bindModulePages(moduleId);
    refreshDebugState();
  }

  // ---------- switch aria 通用接线（免维护：约定优于配置） ----------
  // .option-row 结构里的 .mra-toggle[role=switch] 自动关联同排 .option-title（名称）
  // 与 .option-desc（描述），引用 id 从开关自身 id 派生（<开关id>-label / -desc）。
  // 新开关按既有结构写即自动获得读屏播报，无需逐实例手写 aria 属性；
  // hasAttribute 守卫幂等，可对同一子树重复调用。
  function wireSwitchAria(root) {
    (root || document).querySelectorAll('.option-row').forEach((row) => {
      const sw = row.querySelector('.mra-toggle[role="switch"]');
      if (!sw || sw.hasAttribute('aria-labelledby') || !sw.id) return; // 无 id 无法建立引用，跳过
      const title = row.querySelector('.option-title');
      if (!title) return;
      title.id = sw.id + '-label';
      sw.setAttribute('aria-labelledby', title.id);
      const desc = row.querySelector('.option-desc');
      if (desc) { desc.id = sw.id + '-desc'; sw.setAttribute('aria-describedby', desc.id); }
    });
  }
  wireSwitchAria(document); // 静态 HTML 里的开关（如录制演示）脚本加载即接线

  // 绑定当前模块卡片上的控件事件（渲染后调用；旧节点随 innerHTML 替换一并销毁，无重复绑定）
  function bindModulePages(moduleId) {
    wireSwitchAria($('page-settings')); // 模板重渲染后新开关补接线（幂等）
    // 环境中心入口（设置页重渲染后按钮重建，须在此重绑；置于卫语句前防提前 return 漏绑）
    const btnOptimizer = document.getElementById('btn-optimizer');
    if (btnOptimizer) btnOptimizer.addEventListener('click', () => { openOptimizerCenter(); });

    const p = (suffix) => $(moduleId + '-' + suffix);

    // 驾驶数据目录入口（同样置于卫语句前：控件由模块模板决定，缺某个模块的控件不该漏绑）
    const btnDemos = p('open-demos');
    if (btnDemos) {
      btnDemos.addEventListener('click', async () => {
        try {
          await mra.call('open_user_data_folder', {});
        } catch (e) {
          console.error(e);
          showError(e.message);
        }
      });
    }
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

    // 运行选项：运行时静音游戏 / 关闭游戏进程 / 退出 MaaRM 程序
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

    // 实时预览卡：三个图标按钮 —— 悬浮窗 / 全屏 / 开关（三态互斥，见 previewMode）
    const previewToggle = p('btn-preview-toggle');
    const previewMax = p('btn-preview-max');
    const previewFloat = p('btn-preview-float');
    const previewCard = p('preview-card');
    if (previewMax) {
      const morphEl = previewMax.querySelector('morph-icon');
      if (morphEl) morphEl.icon = MRAIcons.node('scan'); // mount 后首个 icon 直接绘制不动画
    }

    if (previewToggle) {
      previewToggle.addEventListener('click', async () => {
        const on = !state.peepEnabled;
        setPreviewPlayState(on);      // 先翻转视觉状态
        try {
          await PeepConsumer.setPeep(mra, on);
          state.peepEnabled = on;
        } catch (e) {
          console.error(e);
          showError(e.message);
          setPreviewPlayState(!on);   // 回滚
        }
      });
    }

    if (previewCard && previewMax) {
      previewMax.addEventListener('click', () => {
        if (previewCard.classList.contains('preview-card--fullscreen')) exitPreviewFullscreen(previewCard);
        else enterPreviewFullscreen(previewCard);
      });
    }

    if (previewFloat) {
      previewFloat.addEventListener('click', () => {
        // 三态互斥：全屏态点悬浮窗先退回普通态，不与悬浮窗形态并存
        if (previewCard && previewCard.classList.contains('preview-card--fullscreen')) {
          exitPreviewFullscreen(previewCard);
        }
        postWindowAction('peep-float');
      });
    }

    // 点击方式：每个 .radio-grid 是一组单选，组内互斥、组间独立
    // （共用 .mra-radio-card 样式，选中态不能全页互斥，否则点一组会清掉另一组的选中）
    document.querySelectorAll('.radio-grid').forEach((grid) => {
      grid.querySelectorAll('.mra-radio-card').forEach((card) => {
        card.addEventListener('click', async () => {
          grid.querySelectorAll('.mra-radio-card').forEach((c) => c.classList.remove('mra-radio-card--selected'));
          card.classList.add('mra-radio-card--selected');
          try {
            if (card.dataset.clickmode) {
              if (card.dataset.clickmode === 'gamepad') showGamepadNotice();
              await mra.call('set_click_mode', { mode: card.dataset.clickmode });
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
        if (!tool) return; // 借用 .mra-tool-btn 样式但非快捷工具的按钮（如「打开环境中心」）交各自专属 handler，不在此报错
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

  // 从后端回填当前模块的调试开关状态（切换模块后丢弃过期回填）
  async function refreshDebugState() {
    const mid = MRA.currentModuleId;
    try {
      const d = await mra.call('get_debug_state');
      if (!d || MRA.currentModuleId !== mid) return; // 已切模块：丢弃
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
      // 点击方式选中态
      document.querySelectorAll('.mra-radio-card').forEach((card) => {
        if (card.dataset.clickmode) {
          card.classList.toggle('mra-radio-card--selected', card.dataset.clickmode === d.click_mode);
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

  // ---------- 导出到 window.MRA ----------
  Object.assign(window.MRA, {
    bindModuleOptionsUI,
    refreshModuleOptions,
    updateModuleOptionsDisabled,
    refreshSpeedrushStatus,
    renderModulePages,
    checkRegistryOptimizations,
    pollTodayBoard,
    refreshDebugState,
  });
})();
