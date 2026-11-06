/* 模块配置面板：按插件声明的 schema 通用渲染（契约见 core/config_ui_contract.py）。
 *
 * 渲染器只认识控件类型与通用规则（显隐、按值换文案、动态候选），不认识任何模块字段；
 * 所有动态文本走 textContent，不拼 HTML。
 *
 * 并发口径（与 config-panel-state.js 配套）：
 * - 每卡 lastRev：比已应用版本旧的响应丢弃（后端每次 set 成功 _rev+1）；
 * - 同卡保存串行，保存中再改合并成 pending，当前请求完成后补发；
 * - 列表重建时 reset() 递增代际，旧回调作废，不写已卸载节点。
 */
(function () {
  'use strict';

  var MaaRM = window.MaaRM;
  var ST = window.MaaRMConfigPanelState;
  var entries = new Map();
  var generation = 0;
  var locked = false;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function panelEl(mid) {
    return document.querySelector('#task-list [data-id="' + mid + '"] [data-options]');
  }

  function signature(cfg) {
    if (!cfg) return '';
    return JSON.stringify({ state: cfg._ui_state || '', ui: cfg._ui || null });
  }

  function sameValue(a, b) {
    return a === b;
  }

  function evalCond(cond, cfg) {
    if (!cond) return true;
    var value = cfg ? cfg[cond.key] : undefined;
    if (cond.op === 'equals') return sameValue(value, cond.value);
    if (cond.op === 'not_equals') return !sameValue(value, cond.value);
    if (cond.op === 'in') return Array.isArray(cond.value) && cond.value.some(function (v) {
      return sameValue(v, value);
    });
    return true;
  }

  function pickVariant(field, cfg) {
    var list = field.variants || [];
    for (var i = 0; i < list.length; i += 1) {
      if (evalCond(list[i].when, cfg)) return list[i];
    }
    return null;
  }

  function choicesFor(field, cfg) {
    if (field.choices) {
      var all = (cfg && cfg._choices) || {};
      return Array.isArray(all[field.choices]) ? all[field.choices] : [];
    }
    return Array.isArray(field.options) ? field.options : [];
  }

  function domId(entry, field, suffix) {
    var mid = String(entry.mid).replace(/[^a-zA-Z0-9_-]/g, '_');
    return 'cfgp-' + mid + '-' + field.key + '-' + suffix;
  }

  // ---------- 字段构建 ----------

  function buildSwitch(entry, field) {
    var wrap = el('div', 'option-row');
    var sw = el('button', 'maarm-toggle');
    sw.type = 'button';
    sw.id = domId(entry, field, 'switch');
    sw.setAttribute('role', 'switch');
    sw.setAttribute('aria-checked', 'false');
    var main = el('div', 'option-main');
    var title = el('div', 'option-title', field.label);
    title.id = domId(entry, field, 'label');
    sw.setAttribute('aria-labelledby', title.id);
    main.appendChild(title);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      desc.id = domId(entry, field, 'desc');
      sw.setAttribute('aria-describedby', desc.id);
      main.appendChild(desc);
    }
    wrap.appendChild(sw);
    wrap.appendChild(main);
    sw.addEventListener('click', function () {
      if (locked || entry.locked) return;
      var patch = {};
      patch[field.key] = !entry.cfg[field.key];
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: wrap, sw: sw, title: title, desc: desc,
    };
  }

  function buildSegmented(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var seg = el('div', 'maarm-segmented');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', field.label);
    var buttons = (field.options || []).map(function (option, index) {
      var btn = el('button', 'seg-btn');
      btn.appendChild(el('span', 'seg-label', option.label));
      if (option.badge && option.badge.text) {
        var badge = el('span', 'seg-badge', option.badge.text);
        if (option.badge.level) badge.classList.add('seg-badge--' + option.badge.level);
        btn.appendChild(badge);
      }
      btn.type = 'button';
      btn.addEventListener('click', function () {
        if (locked || entry.locked) return;
        var patch = {};
        patch[field.key] = option.value;
        savePatch(entry.mid, patch);
      });
      btn.dataset.index = String(index);
      seg.appendChild(btn);
      return btn;
    });
    box.appendChild(label);
    box.appendChild(seg);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    return {
      field: field, wrap: wrap, seg: seg, buttons: buttons, title: label, desc: desc,
    };
  }

  function buildSelect(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var control = el('div', 'maarm-field-control');
    var select = el('select');
    select.id = domId(entry, field, 'select');
    label.setAttribute('for', select.id);
    control.appendChild(select);
    box.appendChild(label);
    box.appendChild(control);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    select.addEventListener('change', function () {
      if (locked || entry.locked) return;
      var option = select.options[select.selectedIndex];
      if (!option) return;
      var patch = {};
      patch[field.key] = option._maarmValue;
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: wrap, select: select, title: label, desc: desc,
      optSig: '',
    };
  }

  function buildNumber(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var control = el('div', 'maarm-field-control');
    var input = el('input');
    input.type = 'number';
    input.id = domId(entry, field, 'number');
    label.setAttribute('for', input.id);
    if (field.min != null) input.min = String(field.min);
    if (field.max != null) input.max = String(field.max);
    if (field.step != null) input.step = String(field.step);
    control.appendChild(input);
    if (field.suffix) control.appendChild(el('span', 'field-suffix', field.suffix));
    box.appendChild(label);
    box.appendChild(control);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    function commit() {
      if (locked || entry.locked) return;
      var value = parseFloat(input.value);
      if (Number.isNaN(value)) value = 0;
      if (Number.isFinite(field.min) && value < field.min) value = field.min;
      if (Number.isFinite(field.max) && value > field.max) value = field.max;
      input.value = String(value);
      var patch = {};
      patch[field.key] = value;
      savePatch(entry.mid, patch);
    }
    input.addEventListener('change', commit);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') commit();
    });
    return {
      field: field, wrap: wrap, input: input, title: label, desc: desc,
    };
  }

  function buildText(entry, field) {
    var wrap = el('div', 'module-options-row');
    var box = el('div', 'maarm-field');
    var label = el('label', 'maarm-field-label');
    label.textContent = field.label;
    if (field.hint) label.appendChild(el('span', 'field-hint', '（' + field.hint + '）'));
    var control = el('div', 'maarm-field-control');
    var input = el('input');
    input.type = 'text';
    input.id = domId(entry, field, 'text');
    if (field.placeholder) input.placeholder = field.placeholder;
    label.setAttribute('for', input.id);
    control.appendChild(input);
    box.appendChild(label);
    box.appendChild(control);
    var desc = null;
    if (field.description) {
      desc = el('p', 'option-desc', field.description);
      box.appendChild(desc);
    }
    wrap.appendChild(box);
    input.addEventListener('change', function () {
      if (locked || entry.locked) return;
      var patch = {};
      patch[field.key] = input.value;
      savePatch(entry.mid, patch);
    });
    return {
      field: field, wrap: wrap, input: input, title: label, desc: desc,
    };
  }

  function runNoticeAction(entry, action) {
    var call;
    if (action.action === 'download_release') {
      call = MaaRM.maarm.call('open_external_url', { target: 'download' });
    } else if (action.action === 'open_user_data_folder') {
      var params = action.subpath ? { subpath: action.subpath } : {};
      call = MaaRM.maarm.call('open_user_data_folder', params);
    } else {
      return;
    }
    Promise.resolve(call).catch(function (err) {
      var text = err && err.message ? err.message : String(err);
      flashStatus(entry, '打开失败: ' + text, 'error', 3000);
    });
  }

  function buildNotice(entry, notice) {
    var row = el('div', 'field-notice');
    if (notice.level) row.classList.add('field-notice--' + notice.level);
    row.appendChild(el('span', 'field-notice-text', notice.text));
    var actions = Array.isArray(notice.actions) ? notice.actions : [];
    if (actions.length) {
      var box = el('div', 'field-notice-actions');
      actions.forEach(function (action) {
        var btn = el('button', 'field-notice-btn', action.label);
        btn.type = 'button';
        btn.addEventListener('click', function () { runNoticeAction(entry, action); });
        box.appendChild(btn);
      });
      row.appendChild(box);
    }
    return row;
  }

  function buildField(entry, field) {
    switch (field.type) {
      case 'switch': return buildSwitch(entry, field);
      case 'segmented': return buildSegmented(entry, field);
      case 'select': return buildSelect(entry, field);
      case 'number': return buildNumber(entry, field);
      case 'text': return buildText(entry, field);
      default: return null;
    }
  }

  // ---------- 字段更新 ----------

  function applyText(node, field, cfg) {
    var variant = pickVariant(field, cfg);
    var label = (variant && variant.label) || field.label;
    var desc = variant && variant.description !== undefined
      ? variant.description : field.description;
    node.title.textContent = label;
    if (node.desc) {
      if (desc) {
        node.desc.textContent = desc;
        node.desc.style.display = '';
      } else {
        node.desc.style.display = 'none';
      }
    }
  }

  function syncSelect(node, cfg) {
    var options = choicesFor(node.field, cfg);
    var sig = JSON.stringify(options.map(function (o) { return [o.value, o.label]; }));
    if (node.optSig !== sig) {
      node.select.textContent = '';
      options.forEach(function (option) {
        var item = el('option', null, option.label);
        item._maarmValue = option.value;
        node.select.appendChild(item);
      });
      node.optSig = sig;
    }
    var value = cfg ? cfg[node.field.key] : undefined;
    var found = false;
    Array.prototype.forEach.call(node.select.options, function (item) {
      var hit = sameValue(item._maarmValue, value);
      item.selected = hit;
      if (hit) found = true;
    });
    if (!found && value !== undefined && value !== null && value !== '') {
      var missing = el('option', null, '无（原选项已失效）');
      missing._maarmValue = value;
      node.select.appendChild(missing);
      missing.selected = true;
    }
  }

  function applyField(node, cfg) {
    var field = node.field;
    var visible = evalCond(field.visible_if, cfg);
    node.wrap.style.display = visible ? '' : 'none';
    if (node.noticeEl) node.noticeEl.style.display = visible ? '' : 'none';
    applyText(node, field, cfg);
    var value = cfg ? cfg[field.key] : undefined;
    if (node.sw) {
      var on = !!value;
      node.sw.classList.toggle('maarm-toggle--active', on);
      node.sw.setAttribute('aria-checked', String(on));
    } else if (node.buttons) {
      node.buttons.forEach(function (btn, index) {
        var option = field.options[index];
        btn.classList.toggle('seg-btn--selected', sameValue(option.value, value));
      });
    } else if (node.select) {
      syncSelect(node, cfg);
    } else if (node.input && node.input.type === 'number') {
      if (document.activeElement !== node.input) {
        node.input.value = value == null ? '' : String(value);
      }
    } else if (node.input) {
      if (document.activeElement !== node.input) {
        node.input.value = value == null ? '' : String(value);
      }
    }
  }

  function applyAll(entry, cfg) {
    entry.nodes.fields.forEach(function (node) { applyField(node, cfg); });
  }

  function applyNote(entry, cfg) {
    var note = entry.nodes.note;
    var text = cfg && cfg._ui_note;
    note.textContent = text || '';
    note.classList.toggle('maarm-status--warn', !!text);
    note.style.display = text ? '' : 'none';
  }

  function applyLock(entry) {
    if (!entry.nodes) return;
    entry.nodes.fields.forEach(function (node) {
      if (node.sw) node.sw.disabled = locked;
      if (node.buttons) node.buttons.forEach(function (b) { b.disabled = locked; });
      if (node.select) node.select.disabled = locked;
      if (node.input) node.input.disabled = locked;
    });
  }

  // ---------- 状态行 ----------

  function setStatus(entry, text, level) {
    if (!entry.nodes || !entry.nodes.status) return;
    MaaRM.setInlineStatus(entry.nodes.status, text, level);
  }

  function renderStatus(entry) {
    if (!entry.cfg || entry.localStatus) return;
    var status = entry.cfg._status;
    setStatus(entry, status ? status.text : '', status ? status.level : '');
  }

  function showSaving(entry) {
    clearTimeout(entry._statusTimer);
    entry.localStatus = true;
    setStatus(entry, '保存中...', '');
  }

  function flashStatus(entry, text, level, ms) {
    clearTimeout(entry._statusTimer);
    entry.localStatus = true;
    setStatus(entry, text, level);
    entry._statusTimer = setTimeout(function () {
      entry.localStatus = false;
      renderStatus(entry);
    }, ms || 1600);
  }

  // ---------- 构建与渲染 ----------

  function build(entry, host, cfg) {
    host.textContent = '';
    var root = el('div', 'config-panel');
    var note = el('p', 'module-options-status config-panel-note');
    note.style.display = 'none';
    root.appendChild(note);
    var nodes = { host: host, root: root, note: note, status: null, fields: new Map() };
    entry.nodes = nodes;
    var ui = cfg && cfg._ui;
    if (ui && Array.isArray(ui.groups)) {
      ui.groups.forEach(function (group) {
        if (!group || !Array.isArray(group.fields)) return;
        if (group.title) root.appendChild(el('div', 'task-panel-label', group.title));
        group.fields.forEach(function (field) {
          var node = buildField(entry, field);
          if (!node) return;
          root.appendChild(node.wrap);
          node.noticeEl = field.notice ? buildNotice(entry, field.notice) : null;
          if (node.noticeEl) root.appendChild(node.noticeEl);
          nodes.fields.set(field.key, node);
        });
      });
    }
    var status = el('p', 'module-options-status');
    root.appendChild(status);
    nodes.status = status;
    host.appendChild(root);
    entry.sig = signature(cfg);
  }

  function render(mid, cfg) {
    if (!cfg) return;
    var host = panelEl(mid);
    if (!host) return;
    var entry = entries.get(mid);
    var rebuild = !entry || !ST.isAlive(entry, generation)
      || entry.sig !== signature(cfg)
      || !entry.nodes || entry.nodes.host !== host;
    if (rebuild) {
      if (entry) {
        ST.kill(entry);
        clearTimeout(entry._statusTimer);
      }
      entry = ST.createEntry(mid, generation);
      entries.set(mid, entry);
      build(entry, host, cfg);
    }
    entry.cfg = cfg;
    entry.locked = locked;
    applyNote(entry, cfg);
    applyAll(entry, cfg);
    applyLock(entry);
    renderStatus(entry);
  }

  // 运行中轮询：只更新状态行与摘要，不重渲表单、不回写输入框。
  function updateLive(mid, cfg) {
    var entry = entries.get(mid);
    if (!entry || !ST.isAlive(entry, generation) || !cfg) return;
    if (entry.saving) return;                  // 保存期间不接受轮询覆盖
    if (!ST.acceptResponse(entry, cfg)) return; // 旧响应丢弃
    var before = entry.cfg && entry.cfg._summary;
    entry.cfg = Object.assign({}, entry.cfg || {}, cfg);
    // 只有摘要变化才轻刷卡片；状态行每秒更新不值得重建 chips/阶段列表
    if (JSON.stringify(before) !== JSON.stringify(cfg._summary) && MaaRM.noteConfigSaved) {
      MaaRM.noteConfigSaved(mid, cfg);
    }
    renderStatus(entry);
  }

  function reset() {
    generation += 1;
    entries.forEach(function (entry) {
      ST.kill(entry);
      clearTimeout(entry._statusTimer);
    });
    entries.clear();
  }

  function setLocked(running) {
    locked = !!running;
    entries.forEach(function (entry) {
      entry.locked = locked;
      applyLock(entry);
    });
  }

  function has(mid) {
    return entries.has(mid);
  }

  // ---------- 保存 ----------

  function applyOptimistic(entry, patch) {
    entry.cfg = Object.assign({}, entry.cfg || {}, patch);
    applyAll(entry, entry.cfg);
  }

  async function runSave(entry, patch) {
    var mid = entry.mid;
    var snapshot = Object.assign({}, entry.cfg);
    var failed = false;
    applyOptimistic(entry, patch);
    showSaving(entry);
    try {
      var resp = await MaaRM.maarm.call('set_module_config', {
        module_id: mid,
        config: patch,
      });
      if (!ST.isAlive(entry, generation)) return;
      if (!ST.acceptResponse(entry, resp)) return;
      entry.cfg = resp;
      applyNote(entry, resp);
      applyAll(entry, resp);
      if (MaaRM.noteConfigSaved) MaaRM.noteConfigSaved(mid, resp);
      // 成功不刷回执：状态行回落到模块自己的运行实况（界面只报阶段进度与影响结果的异常），
      // 失败仍走行内红字停留。这里必须解锁，否则状态行会停在「保存中」。
      entry.localStatus = false;
    } catch (e) {
      if (!ST.isAlive(entry, generation)) return;
      failed = true;
      entry.cfg = snapshot;
      applyNote(entry, snapshot);
      applyAll(entry, snapshot);
      MaaRM.reportError('inline', '保存失败: ' + (e.message || e),
        { el: entry.nodes && entry.nodes.status });
    } finally {
      if (ST.isAlive(entry, generation)) {
        var pending = ST.finishSave(entry);
        if (pending) {
          savePatch(mid, pending);
        } else if (!failed && !entry.localStatus) {
          renderStatus(entry);
        }
      }
    }
  }

  function savePatch(mid, patch) {
    var entry = entries.get(mid);
    if (!entry || !ST.isAlive(entry, generation) || locked || entry.locked) return;
    if (entry.saving) {
      entry.pending = ST.mergePatch(entry.pending, patch);
      applyOptimistic(entry, patch);
      return;
    }
    if (!ST.beginSave(entry)) return;
    runSave(entry, patch);
  }

  Object.assign(MaaRM, {
    configPanel: {
      render: render,
      updateLive: updateLive,
      reset: reset,
      setLocked: setLocked,
      has: has,
    },
  });
})();
