// MaaRM shell 前端 —— 运行控制：开始/停止/三秒倒计时、阶段列表、状态轮询与性能卡渲染。
// 跨文件调用经 window.MRA（见文件末尾导出）。
(function () {
  'use strict';

  // 共享物导入（js/rpc.js、js/settings.js 已先行加载）
  const {
    mra, $, state, reportError,
    updateModuleOptionsDisabled, refreshModuleOptions, refreshSpeedrushStatus,
  } = window.MRA;

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
        ? MRAIcons.svg('media-play')
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
      // 运行状态跳变时：锁定/解锁模块专属选项 + 刷新该模块的运行实况
      const runState = !!(d.is_running || d.worker_active);
      if (runState !== state._lastRunState) {
        state._lastRunState = runState;
        updateModuleOptionsDisabled(runState);
        const mid = $('module-select') ? $('module-select').value : null;
        if (mid) refreshModuleOptions(mid);
      }
      // 录制实况每秒刷一次：帧数在涨，只在状态跳变时刷是看不出进度的
      if (runState && MRA.currentModuleId === 'speedrush'
          && Date.now() - state._lastRecRefresh >= 1000) {
        state._lastRecRefresh = Date.now();
        refreshSpeedrushStatus();
      }
    } catch (e) {
      console.error(e);
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
    $('btn-start').disabled = d.is_running || d.worker_active;
    $('btn-stop').disabled = !(d.is_running || d.worker_active);
    // 数据页：当前阶段（id 带当前模块前缀）
    const perfStage = $('' + MRA.currentModuleId + '-perf-stage');
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
    renderPerfCard(d.perf);
  }

  // ---------- 鉴宝性能卡渲染 ----------
  // 三态的样式与"给用户看的词"放前端（纯呈现）；语义诊断文案来自后端 health.text。
  const PERF_LEVEL_CLASS = {
    ok: 'perf-badge--ok', warn: 'perf-badge--warn',
    error: 'perf-badge--error', idle: 'perf-badge--idle'
  };
  const PERF_RESP_LABEL = { ok: '流畅', warn: '偏慢', error: '卡顿', idle: '空闲' };
  const PERF_LOAD_LABEL = { ok: '正常', warn: '偏高', error: '过载', idle: '空闲' };
  const PERF_HEALTH_LABEL = { ok: '正常', warn: '偏慢', error: '读不到', idle: '空闲' };

  function setPerfText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;   // textContent：后端文案不进 HTML 通道
  }

  function setPerfBadge(id, level, label) {
    const el = $(id);
    if (!el) return;
    el.className = 'perf-badge ' + (PERF_LEVEL_CLASS[level] || PERF_LEVEL_CLASS.idle);
    el.textContent = '';
    const dot = document.createElement('span');
    dot.className = 'perf-badge-dot';
    el.appendChild(dot);
    el.appendChild(document.createTextNode(label || ''));
  }

  function pushPerfHist(key, value) {
    const h = state.perfHist[key];
    if (!h || !Number.isFinite(value)) return;
    h.push(value);
    if (h.length > state.PERF_HIST_MAX) h.shift();
  }

  function drawSpark(id, hist, ceiling) {
    const svg = $(id);
    if (!svg) return;
    if (hist.length < 2) { svg.textContent = ''; return; }
    const top = Math.max(ceiling, hist.reduce((a, b) => (b > a ? b : a), 0));
    let pts = '';
    for (let i = 0; i < hist.length; i++) {
      const x = (i / (hist.length - 1)) * 100;
      const y = 21 - Math.min(1, hist[i] / top) * 20;
      pts += x.toFixed(2) + ',' + y.toFixed(2) + ' ';
    }
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('points', pts.trim());
    svg.textContent = '';
    svg.appendChild(line);
  }

  function renderPerfCard(perf) {
    const mid = MRA.currentModuleId;
    const r = (perf && perf.response) || {};
    const c = (perf && perf.cpu) || {};
    const h = (perf && perf.health) || {};

    const fps = Number(r.fps) || 0;
    const respLevel = perf ? (r.level || 'idle') : 'idle';
    setPerfText(mid + '-perf-fps', fps > 0 ? fps.toFixed(1) + ' 次/秒' : '-- 次/秒');
    setPerfBadge(mid + '-perf-fps-badge', respLevel, PERF_RESP_LABEL[respLevel] || '空闲');
    setPerfText(mid + '-perf-fps-note', respLevel === 'warn' || respLevel === 'error'
      ? '反应变慢：关掉调试落盘，或降低游戏画质后再看'
      : '程序每秒查看游戏画面的次数，越高越跟手');
    pushPerfHist('fps', fps);
    drawSpark(mid + '-perf-fps-spark', state.perfHist.fps, 10);

    const loadLevel = perf && c.available ? (c.level || 'idle') : 'idle';
    if (perf && c.available) {
      setPerfText(mid + '-perf-cpu', Math.round((c.load_p50 || 0) * 100) + '%');
      setPerfText(mid + '-perf-cpu-note',
        '本机 ' + (c.cores || '?') + ' 核 · 偏高时关闭其它程序，或在设置里关掉调试落盘');
    } else {
      setPerfText(mid + '-perf-cpu', '--');
      setPerfText(mid + '-perf-cpu-note', '偏高时关闭其它程序，或在设置里关掉调试落盘');
    }
    setPerfBadge(mid + '-perf-cpu-badge', loadLevel, PERF_LOAD_LABEL[loadLevel] || '空闲');
    pushPerfHist('load', perf && c.available ? (c.load_p50 || 0) : NaN);
    drawSpark(mid + '-perf-cpu-spark', state.perfHist.load, 1);

    const healthLevel = h.level || 'idle';
    setPerfBadge(mid + '-perf-health-badge', healthLevel, PERF_HEALTH_LABEL[healthLevel] || '空闲');
    setPerfText(mid + '-perf-health-note', h.text || '等待识别');
  }

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

  // ---------- 导出到 window.MRA ----------
  Object.assign(window.MRA, { renderStageList, pollStatus, setStatus });
})();
