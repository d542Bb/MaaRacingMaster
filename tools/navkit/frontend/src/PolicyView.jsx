import React, { useEffect, useMemo, useState } from 'react';
import {
  Banner, Button, Input, InputNumber, RadioGroup, Radio, Select, SideSheet, Spin,
  Switch, Tag, TextArea, Toast, Tooltip,
} from '@douyinfe/semi-ui';
import {
  IconChevronDown, IconChevronUp, IconDelete, IconPlus, IconRefresh, IconSave, IconTick,
} from '@douyinfe/semi-icons';
import { api, previewAssets, saveAssets } from './api';
import createHistory from '../../static/history.js';
import {
  CONTRACT_MIRROR, DECISION_SOURCE_LABELS, EFFECT_LABELS, FACT_LABELS, OP_LABELS,
  TUNING_KEY_LABELS, TUNING_SECTION_LABELS, validateDraft, guardApplyJson,
  deriveKeyCandidates, parseWhen,
} from './policyMeta';

const ISSUE_RE = /^\[([A-Z]\d+)\]\s+([^:]+):\s*([\s\S]*)$/;

function parseIssueLine(s) {
  const m = ISSUE_RE.exec(s);
  return m ? { code: m[1], path: m[2], message: m[3] } : null;
}

// 策略视图（P2-1 Editor Core）：draft 状态机 + 即时校验 + JSON 高级编辑 + 保存链路
export default function PolicyView({ onDirtyChange }) {
  const [original, setOriginal] = useState(null); // 冻结快照；保存 roundtrip 后随 roundtrip document 重建（守卫 B）
  const [draft, setDraft] = useState(null);
  const [report, setReport] = useState(null); // 后端 report（GET 随附，或保存响应）
  const [loadErr, setLoadErr] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState(null); // {type:'success'|'error', text}
  const [jsonOpen, setJsonOpen] = useState(false);
  const [jsonText, setJsonText] = useState('');
  const [jsonErr, setJsonErr] = useState(null);
  const [baseHash, setBaseHash] = useState(null);
  const [preview, setPreview] = useState(null);
  const history = React.useMemo(() => createHistory(), []);

  useEffect(() => {
    api.assets()
      .then(d => { setOriginal(d.document); setDraft(structuredClone(d.document)); setReport(d.report); setBaseHash(d.base_hash || null); })
      .catch(e => setLoadErr(String(e)));
  }, []);

  // dirty 上报给外壳（切视图拦截用）；卸载时复位
  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);

  // 刷新拦截（dirty 时）
  useEffect(() => {
    if (!dirty) return undefined;
    const h = (e) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', h);
    return () => window.removeEventListener('beforeunload', h);
  }, [dirty]);

  const issues = useMemo(() => (draft ? validateDraft(draft) : []), [draft]);
  const hasError = issues.some(i => i.level === 'error');

  // ---------- 字段编辑（P2-2）：全部作用于 draft.policies，编辑即 dirty ----------
  const [expandedRule, setExpandedRule] = useState(null); // 手风琴：同时只展开一张卡

  const touchDraft = (mutate) => {
    history.push('策略编辑', draft, 'draft');
    setDraft(prev => {
      const d = structuredClone(prev);
      mutate(d);
      return d;
    });
    setDirty(true);
  };

  const updateRule = (index, nextRule) => {
    touchDraft(d => { d.policies.rules[index] = nextRule; });
  };

  const moveRule = (index, delta) => {
    const rules = draft.policies?.rules || [];
    const target = index + delta;
    if (target < 0 || target >= rules.length) return;
    touchDraft(d => {
      const [r] = d.policies.rules.splice(index, 1);
      d.policies.rules.splice(target, 0, r);
    });
    setExpandedRule(target);
  };

  // 新建规则：decision 留空（守卫 A——不自动生成 key/source/fatal 空值字段，
  // 即时校验将以 P02 提示用户显式选择输出）
  const addRule = () => {
    const rules = draft.policies?.rules || [];
    const usedIds = new Set(rules.map(r => r?.id).filter(Boolean));
    let n = rules.length + 1;
    let id = `new_rule_${n}`;
    while (usedIds.has(id)) { n += 1; id = `new_rule_${n}`; }
    touchDraft(d => { d.policies.rules.push({ id, when: {}, decision: {} }); });
    setExpandedRule(rules.length);
  };

  const removeRule = (index) => {
    touchDraft(d => { d.policies.rules.splice(index, 1); });
    setExpandedRule(null);
  };

  const updateTuning = (nextTuning) => {
    touchDraft(d => { d.policies.tuning = nextTuning; });
  };

  // 后端 report（最近一次保存/加载）行内映射：policies.rules[N] → 第 N 张卡
  const backendByRule = useMemo(() => {
    const map = {};
    if (!report) return map;
    const lines = [...(report.errors || []).map(s => [s, 'error']), ...(report.warnings || []).map(s => [s, 'warn'])];
    for (const [line, level] of lines) {
      const p = parseIssueLine(line);
      const m = p && /^policies\.rules\[(\d+)\]/.exec(p.path);
      if (m) (map[+m[1]] ||= []).push({ ...p, level, raw: line });
      else (map._global ||= []).push({ level, raw: line, parsed: p });
    }
    return map;
  }, [report]);

  const reset = () => {
    if (!original) return;
    setDraft(structuredClone(original));
    setDirty(false);
    setSaveMsg(null);
  };

  const restoreDraft = next => { if (next) { setDraft(next); setDirty(true); } };
  useEffect(() => {
    const onKey = e => {
      if (!(e.ctrlKey || e.metaKey) || !['z', 'y'].includes(e.key.toLowerCase())) return;
      e.preventDefault(); restoreDraft(e.key.toLowerCase() === 'z' ? history.undo(draft) : history.redo(draft));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [draft, history]);

  // 保存 → 先预览 diff/校验/编译，确认后正式保存。
  const save = async () => {
    if (!draft || saving || hasError) return;
    setSaving(true); setSaveMsg(null);
    try {
      const p = await previewAssets(draft, baseHash);
      setPreview(p);
      if (!p.ok || p.report?.errors?.length || p.compile?.error) {
        setSaveMsg({ type: 'error', text: p.report?.errors?.join('；') || p.compile?.error || '预览未通过' });
        return;
      }
      const confirmed = window.confirm(`保存预览：新增 ${p.diff.added.length} · 删除 ${p.diff.removed.length} · 修改 ${p.diff.changed.length}\n编译：${p.compile.status}\n确认落盘？`);
      if (!confirmed) return;
      const res = await saveAssets(draft, p.base_hash || baseHash);
      if (!res.ok) throw new Error(res.error || res.report?.errors?.join('；') || '保存失败');
      const fresh = await api.assets();
      setOriginal(fresh.document); setDraft(structuredClone(fresh.document));
      setReport(fresh.report); setBaseHash(fresh.base_hash || null); setDirty(false);
      history.marker();
      setSaveMsg({ type: 'success', text: `已保存 · 编译 ${res.compiled || 'skipped_no_routes'}` });
    } catch (e) {
      setSaveMsg({ type: 'error', text: String(e.message || e) });
    } finally { setSaving(false); }
  };

  // JSON 高级编辑：打开时同步 draft 全文
  const openJson = () => {
    setJsonText(JSON.stringify(draft, null, 2));
    setJsonErr(null);
    setJsonOpen(true);
  };

  // 解析并应用：结构守卫（§4.2）不通过则拒绝并保留原 draft
  const applyJson = () => {
    let parsed;
    try {
      parsed = JSON.parse(jsonText);
    } catch (e) {
      setJsonErr(`JSON 解析失败：${e.message}`);
      return;
    }
    const verdict = guardApplyJson(original, parsed);
    if (!verdict.ok) {
      setJsonErr(`结构守卫拒绝应用：${verdict.reason}（原 draft 已保留）`);
      return;
    }
    setDraft(parsed);
    setDirty(true);
    setJsonErr(null);
    Toast.success({ content: '已应用到 draft（尚未落盘）', showClose: true });
  };

  if (loadErr) return <Banner type="danger" closeIcon={null} description={`策略加载失败：${loadErr}`} />;
  if (!draft || !original) return <div className="view-loading"><Spin size="large" /></div>;

  return renderMain();

  // ---------- 渲染 ----------

  function renderMain() {
    const anchorKeys = Object.keys(original.anchors || {});

    return (
      <div className="panel-view policy-view">
        <div className="policy-toolbar">
          <span className="policy-title">策略编辑</span>
          {dirty ? <Tag size="small" color="orange" style={{ margin: 0 }}>未保存</Tag>
            : <Tag size="small" color="green" style={{ margin: 0 }}>与磁盘一致</Tag>}
          {hasError && <Tag size="small" color="red" style={{ margin: 0 }}>即时校验 {issues.filter(i => i.level === 'error').length} 项 error</Tag>}
          <div style={{ flex: 1 }} />
          <Button icon={<IconPlus />} onClick={addRule}>添加规则</Button>
          <Button icon={<IconRefresh />} disabled={!dirty || saving} onClick={reset}>重置</Button>
          <Button icon={<IconSave />} theme="solid" loading={saving} disabled={!dirty || hasError} onClick={save}>保存</Button>
          <Button icon={<IconTick />} onClick={openJson}>JSON 高级编辑</Button>
        </div>

        {saveMsg && (
          <Banner
            type={saveMsg.type === 'success' ? 'success' : 'danger'} closeIcon={null} bordered
            description={saveMsg.text}
          />
        )}
        <ReportPane backendGlobal={backendByRule._global} report={report} />
        {issues.length > 0 && (
          <Banner
            type={hasError ? 'danger' : 'warning'} closeIcon={null} bordered
            description={(
              <div className="policy-issue-list">
                <div className="muted small">前端即时校验（提示非判定，权威以保存后的后端报告为准）：</div>
                {issues.map((it, k) => (
                  <div key={k} className={`policy-issue policy-issue-${it.level}`}>
                    <Tag size="small" type={it.level === 'error' ? 'solid' : 'light'} color={it.level === 'error' ? 'red' : 'orange'} style={{ margin: 0 }}>{it.code}</Tag>
                    <span className="mono small">{it.path}</span>
                    <span className="small">{it.message}</span>
                  </div>
                ))}
              </div>
            )} />
        )}

        <div className="policy-meta-line muted small">
          锚点 {anchorKeys.length} · 规则 {draft.policies?.rules?.length ?? 0} 条 ·
          stage_map 只读（可在 JSON 高级编辑中查看）· tuning 修改需重启进程生效
        </div>

        <div className="policy-rules">
          <div className="insp-title">规则（数组顺序即优先级，#N 与报告 rules[N] 对齐；上移/下移 = 修改程序语义）</div>
          {(draft.policies?.rules || []).map((rule, i) => (
            <RuleCard
              key={i}
              index={i}
              total={(draft.policies?.rules || []).length}
              rule={rule}
              stageMap={draft.policies?.stage_map || {}}
              anchorKeys={anchorKeys}
              policyTuning={draft.policies?.tuning?.policy || {}}
              frontIssues={issues.filter(it => it.ruleIndex === i)}
              backendIssues={backendByRule[i] || []}
              expanded={expandedRule === i}
              onToggle={() => setExpandedRule(expandedRule === i ? null : i)}
              onMove={delta => moveRule(i, delta)}
              onRemove={() => removeRule(i)}
              onChange={next => updateRule(i, next)}
            />
          ))}
        </div>
        <TuningPanel tuning={draft.policies?.tuning || {}} rules={draft.policies?.rules || []} onChange={updateTuning} />
        {renderJsonSheet()}
      </div>
    );
  }

  // JSON 高级编辑抽屉：整份 document 可视（守卫放行范围 = policies.rules / tuning）。
  // 应用前走结构守卫；拒绝时保留原 draft。stage_map 只读，可在此查看。
  function renderJsonSheet() {
    const stageMap = draft.policies?.stage_map || {};
    return (
      <SideSheet
        title="JSON 高级编辑"
        size="large"
        visible={jsonOpen}
        onCancel={() => setJsonOpen(false)}
        footer={(
          <div className="policy-json-footer">
            <span className="muted small">只放行 policies.rules / policies.tuning 的修改；anchors/stages/transitions/routes/_schema_ver/_module/stage_map 一律拒绝。</span>
            <Button theme="solid" onClick={applyJson}>解析并应用</Button>
          </div>
        )}
      >
        <div className="policy-json-body">
          <Banner type="info" closeIcon={null} bordered
            description={`原则：这是文档编辑器，不是绕过约束的后门。应用后仍需点「保存」走后端校验与原子落盘。stage_map（只读）：${Object.keys(stageMap).length} 个稳定 ID。`} />
          {jsonErr && <Banner type="danger" closeIcon={null} bordered description={jsonErr} />}
          <TextArea
            value={jsonText}
            onChange={setJsonText}
            autosize={{ minRows: 24, maxRows: 40 }}
            className="mono policy-json-text"
            spellCheck={false}
          />
        </div>
      </SideSheet>
    );
  }
}

// ---------- 规则只读列表（P2-2 将升级为 RuleCard 手风琴 + 字段编辑器） ----------

function fmtCondValue(v, stageMap) {
  if (v === null) return 'null';
  if (typeof v === 'boolean') return String(v);
  if (typeof v === 'string') {
    if (v.startsWith('@')) return v;
    return stageMap[v] ? `${stageMap[v]}(${v})` : `"${v}"`;
  }
  return String(v);
}

function whenSummary(rule, stageMap) {
  const entries = Object.entries(rule?.when || {});
  if (!entries.length) return <span className="muted">（无条件 —— 遮蔽其后一切规则）</span>;
  return entries.map(([field, spec], i) => {
    const conds = field === 'stage' && typeof spec === 'string'
      ? [{ op: 'eq', value: spec }]
      : typeof spec === 'object' && spec !== null ? Object.entries(spec).map(([op, value]) => ({ op, value })) : [{ op: 'eq', value: spec }];
    return (
      <span key={field} className="policy-cond">
        {i > 0 && <span className="muted"> AND </span>}
        <span className="mono">{field}</span>
        {conds.map((c, j) => (
          <span key={j}> {OP_LABELS[c.op] || c.op} <span className="mono">{fmtCondValue(c.value, stageMap)}</span></span>
        ))}
      </span>
    );
  });
}

function decisionSummary(dec) {
  if (!dec || typeof dec !== 'object') return <span className="muted">—</span>;
  return (
    <span className="policy-decision">
      {dec.source
        ? <Tag size="small" color="violet" style={{ margin: 0 }}>⇒ {dec.source}</Tag>
        : dec.key != null ? <span className="mono">→ {dec.key}</span> : null}
      {dec.fallback_key && <span className="muted small"> ↩ {dec.fallback_key}</span>}
      {dec.center != null && <span className="muted small"> ◎ {typeof dec.center === 'string' ? dec.center : `[${dec.center.join(', ')}]`}</span>}
      {dec.effect && <Tag size="small" color="cyan" style={{ margin: 0 }}>{dec.effect}</Tag>}
      {dec.fatal != null && <Tag size="small" color="red" style={{ margin: 0 }}>⛔ fatal</Tag>}
    </span>
  );
}

// ---------- WhenEditor：条件行 [字段][算子][值][删除]，重复键静默覆盖 → 过滤已选 ----------

const OPS_SCALAR = ['eq', 'neq'];
const OPS_NUMERIC = ['gt', 'gte', 'lt', 'lte'];
// UI 展示顺序（PRESENTATION 层自有定义，与契约 set 无关）
const FACT_FIELD_ORDER = [
  'stage', 'popup_kind', 'clicked_once', 'settle_income', 'retry_count', 'retry_elapsed',
  'cooldown', 'session_decision', 'appraiser_decision', 'bidding_decision',
  'daily_high_score', 'egg_reading', 'egg_read_done', 'reward_elapsed', 'skip_cycle', 'frame_counter',
];

function WhenEditor({ rule, stageMap, policyTuning, onChange }) {
  const when = rule?.when && typeof rule.when === 'object' && !Array.isArray(rule.when) ? rule.when : {};
  const entries = Object.entries(when);
  const used = entries.map(([f]) => f);
  const addable = FACT_FIELD_ORDER.filter(f => !used.includes(f));

  const setCond = (field, spec) => onChange({ ...when, [field]: spec });
  const removeCond = (field) => { const n = { ...when }; delete n[field]; onChange(n); };
  // 新条件默认：stage → stage_map 首个稳定 ID；其余 → eq 空文本（显式留空待填）
  const addCond = (field) => onChange({ ...when, [field]: field === 'stage' ? (Object.keys(stageMap)[0] ?? '') : '' });

  return (
    <div className="policy-when">
      {entries.map(([field, spec]) => (
        <div key={field} className="policy-cond-row">
          <span className="mono policy-cond-field">{FACT_LABELS[field] || field}</span>
          <CondEditor field={field} spec={spec} stageMap={stageMap} policyTuning={policyTuning} onChange={spec2 => setCond(field, spec2)} />
          <Button size="small" theme="borderless" type="danger" icon={<IconDelete />} onClick={() => removeCond(field)} />
        </div>
      ))}
      {addable.length > 0 && (
        <Select
          size="small" className="policy-cond-add" placeholder="＋ 添加条件（选择事实字段）"
          value={undefined}
          onChange={v => { if (v) addCond(v); }}
          optionList={addable.map(f => ({ value: f, label: FACT_LABELS[f] || f }))}
        />
      )}
    </div>
  );
}

const VTYPE_OPTIONS = [
  { value: 'text', label: '文本' },
  { value: 'num', label: '数字' },
  { value: 'bool', label: '布尔' },
  { value: 'null', label: 'null' },
  { value: 'tuning', label: '@tuning 引用' },
];

function vtypeOf(v) {
  if (v === null) return 'null';
  if (typeof v === 'boolean') return 'bool';
  if (typeof v === 'number') return 'num';
  if (typeof v === 'string' && v.startsWith('@')) return 'tuning';
  return 'text';
}

function CondEditor({ field, spec, stageMap, policyTuning, onChange }) {
  // stage 特判：算子锁 eq、值下拉展示 stage_map（value 存稳定 ID）；不提供 prefix
  if (field === 'stage') {
    return (
      <span className="policy-cond-editor">
        <Tag size="small" color="blue" style={{ margin: 0 }}>等于</Tag>
        <Select size="small" filter className="policy-cond-value"
          value={typeof spec === 'string' ? spec : undefined}
          onChange={v => onChange(v)}
          optionList={Object.entries(stageMap).map(([sid, sname]) => ({ value: sid, label: `${sname}（${sid}）` }))}
        />
      </span>
    );
  }

  const isScalar = spec === null || typeof spec !== 'object';
  const op = isScalar ? 'eq' : Object.keys(spec)[0];
  const value = isScalar ? spec : spec[op];

  const changeOp = (nextOp) => {
    if (nextOp === op) return;
    if (OPS_SCALAR.includes(nextOp)) {
      const v = OPS_NUMERIC.includes(op) ? (typeof value === 'number' ? value : 0) : value;
      onChange(nextOp === 'eq' ? v : { [nextOp]: v });
    } else {
      onChange({ [nextOp]: typeof value === 'number' ? value : 0 });
    }
  };
  // eq 落盘标量（"clicked_once": true），不写 {"eq": true}
  const changeValue = (v) => onChange(op === 'eq' ? v : { [op]: v });

  const tuningOptions = Object.keys(policyTuning).map(n => ({ value: `@${n}`, label: `@${n}` }));

  if (OPS_NUMERIC.includes(op)) {
    const isTuning = typeof value === 'string' && value.startsWith('@');
    return (
      <span className="policy-cond-editor">
        <Select size="small" value={op} onChange={changeOp} className="policy-cond-op"
          optionList={[...OPS_SCALAR, ...OPS_NUMERIC].map(o => ({ value: o, label: OP_LABELS[o] }))} />
        {isTuning ? (
          <>
            <Select size="small" filter value={value} onChange={changeValue} className="policy-cond-value" optionList={tuningOptions} />
            <Button size="small" theme="borderless" onClick={() => changeValue(0)}>改数字</Button>
          </>
        ) : (
          <>
            <InputNumber size="small" style={{ width: 110 }} value={typeof value === 'number' ? value : 0}
              onChange={v => { if (typeof v === 'number') changeValue(v); }} />
            <Button size="small" theme="borderless" disabled={!tuningOptions.length}
              onClick={() => changeValue(tuningOptions[0]?.value ?? '@')}>改 @tuning</Button>
          </>
        )}
      </span>
    );
  }

  const vType = vtypeOf(value);
  return (
    <span className="policy-cond-editor">
      <Select size="small" value={op} onChange={changeOp} className="policy-cond-op"
        optionList={[...OPS_SCALAR, ...OPS_NUMERIC].map(o => ({ value: o, label: OP_LABELS[o] }))} />
      <Select size="small" value={vType} onChange={t => {
        if (t === 'text') changeValue('');
        else if (t === 'num') changeValue(0);
        else if (t === 'bool') changeValue(true);
        else if (t === 'null') changeValue(null);
        else changeValue(tuningOptions[0]?.value ?? '@');
      }} className="policy-cond-vtype" optionList={VTYPE_OPTIONS} />
      {vType === 'text' && <Input size="small" style={{ width: 150 }} value={String(value ?? '')} onChange={changeValue} />}
      {vType === 'num' && <InputNumber size="small" style={{ width: 110 }} value={typeof value === 'number' ? value : 0}
        onChange={v => { if (typeof v === 'number') changeValue(v); }} />}
      {vType === 'bool' && (
        <Select size="small" value={String(value === true)} className="policy-cond-value"
          onChange={s => changeValue(s === 'true')} optionList={[{ value: 'true', label: 'true' }, { value: 'false', label: 'false' }]} />
      )}
      {vType === 'null' && <span className="muted small">null（语义值：尚未读出，与 0 / false 不同）</span>}
      {vType === 'tuning' && <Select size="small" filter value={value} onChange={changeValue} className="policy-cond-value" optionList={tuningOptions} />}
    </span>
  );
}

// ---------- RuleCard 手风琴（默认收起：#N + id + when 摘要 + decision 摘要） ----------

function RuleCard({
  index, total, rule, stageMap, anchorKeys, policyTuning,
  frontIssues, backendIssues, expanded, onToggle, onMove, onRemove, onChange,
}) {
  const errs = frontIssues.filter(x => x.level === 'error');
  const warns = frontIssues.filter(x => x.level === 'warn');
  return (
    <div className={`policy-rule${errs.length ? ' policy-rule-err' : ''}${expanded ? ' policy-rule-open' : ''}`}>
      <div className="policy-rule-head" onClick={onToggle} role="button" tabIndex={0}
        onKeyDown={e => { if (e.key === 'Enter') onToggle(); }}>
        <Tag size="small" color="blue" style={{ margin: 0 }}>#{index + 1}</Tag>
        <span className="mono policy-rule-id">{rule?.id ?? '—'}</span>
        {errs.length > 0 && <Tag size="small" color="red" type="solid" style={{ margin: 0 }}>error {errs.length}</Tag>}
        {warns.length > 0 && <Tag size="small" color="orange" style={{ margin: 0 }}>提示 {warns.length}</Tag>}
        <span className="policy-rule-when small">{whenSummary(rule, stageMap)}</span>
        <span className="policy-rule-decision" onClick={e => e.stopPropagation()}>
          {decisionSummary(rule?.decision)}
          <Tooltip content="上移（提高优先级）">
            <Button size="small" theme="borderless" icon={<IconChevronUp />} disabled={index === 0} onClick={() => onMove(-1)} />
          </Tooltip>
          <Tooltip content="下移（降低优先级）">
            <Button size="small" theme="borderless" icon={<IconChevronDown />} disabled={index === total - 1} onClick={() => onMove(1)} />
          </Tooltip>
          <Tooltip content="删除此规则">
            <Button size="small" theme="borderless" type="danger" icon={<IconDelete />} onClick={() => { if (window.confirm(`删除规则 ${rule?.id ?? `#${index + 1}`}？（仅 draft，保存后落盘）`)) onRemove(); }} />
          </Tooltip>
          <Button size="small" theme="borderless" icon={expanded ? <IconChevronUp /> : <IconChevronDown />} onClick={onToggle} />
        </span>
      </div>
      {expanded && (
        <div className="policy-rule-body">
          <div className="policy-edit-block">
            <div className="insp-title">when · 以下全部满足（AND）</div>
            <WhenEditor rule={rule} stageMap={stageMap} policyTuning={policyTuning}
              onChange={when => onChange({ ...rule, when })} />
          </div>
          <div className="policy-edit-block">
            <div className="insp-title">decision · 输出（二轴：输出源 + fatal 叠加）</div>
            <DecisionEditor rule={rule} anchorKeys={anchorKeys}
              onChange={decision => onChange({ ...rule, decision })} />
          </div>
          <div className="policy-edit-block">
            <div className="insp-title">id</div>
            <Input value={rule?.id ?? ''} onChange={v => onChange({ ...rule, id: v })} className="mono" showClear />
          </div>
          {backendIssues.length > 0 && (
            <div className="policy-backend-issues">
              <div className="muted small">后端校验报告（最近一次保存/加载，编辑后以重新保存的结果为准）：</div>
              {backendIssues.map((it, k) => (
                <div key={k} className={`small policy-issue policy-issue-${it.level}`}>
                  <Tag size="small" type={it.level === 'error' ? 'solid' : 'light'} color={it.level === 'error' ? 'red' : 'orange'} style={{ margin: 0 }}>{it.code}</Tag>
                  <span className="mono small">{it.path}</span>
                  <span className="small">{it.message}</span>
                </div>
              ))}
            </div>
          )}
          {frontIssues.map((it, k) => (
            <div key={`f${k}`} className={`small policy-issue policy-issue-${it.level}`}>
              <Tag size="small" type={it.level === 'error' ? 'solid' : 'light'} color={it.level === 'error' ? 'red' : 'orange'} style={{ margin: 0 }}>{it.code}</Tag>
              <span className="small">{it.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------- DecisionEditor（P2_PLAN §1.1 二轴模型 + §1.3-A 序列化守卫） ----------
// 轴 1 输出源：静态 key / 动态 source 视角切换，切换不清空另一字段（仅灰显
// 未生效方）；字段只在用户实际填值时写入，不自动生成空值字段。
// 轴 2 fatal：独立开关 + 文案（{frame} 占位），可叠加在任一输出源之上。

function DecisionEditor({ rule, anchorKeys, onChange }) {
  const dec = rule?.decision && typeof rule.decision === 'object' && !Array.isArray(rule.decision) ? rule.decision : {};
  const hasSource = dec.source != null;
  const hasKey = dec.key != null;
  const hasFatal = dec.fatal != null;
  const [viewMode, setViewMode] = useState(hasSource ? 'source' : 'key');

  const patch = fields => onChange({ ...dec, ...fields });
  const omit = keys => {
    const n = { ...dec };
    keys.forEach(k => delete n[k]);
    onChange(n);
  };
  // 文本输入：空串 = 删除字段（"字段不存在"与"字段为空"不同语义）
  const patchText = key => v => {
    if (v === '' || v == null) {
      if (key in dec) omit([key]);
      return;
    }
    patch({ [key]: v });
  };

  const keyCandidates = deriveKeyCandidates(anchorKeys, dec);
  const fallbackCandidates = deriveKeyCandidates(anchorKeys, { key: dec.fallback_key });
  const keyOptions = keyCandidates.map(o => ({ value: o.value, label: o.label || o.value }));
  const fallbackOptions = [{ value: '__none__', label: '（无 fallback_key）' },
    ...fallbackCandidates.map(o => ({ value: o.value, label: o.label || o.value }))];
  const sourceOptions = CONTRACT_MIRROR.DECISION_SOURCES.map(s => ({
    value: s, label: DECISION_SOURCE_LABELS[s] || s,
  }));

  return (
    <div className="policy-decision-editor">
      <div className="policy-axis">
        <RadioGroup type="button" buttonSize="small" value={viewMode}
          onChange={e => setViewMode(e.target.value)}>
          <Radio value="key">静态 key</Radio>
          <Radio value="source">动态 source</Radio>
        </RadioGroup>
        {viewMode === 'source' && hasKey && (
          <span className="muted small">source 有产出时以 source 为准，此处 key 不生效（字段保留）</span>
        )}
        {viewMode === 'key' && hasSource && (
          <span className="muted small">source 生效中：此处选 key 会与 source 共存（运行时 source 优先）</span>
        )}
      </div>

      {viewMode === 'key' ? (
        <div className="policy-axis-body">
          <label className="policy-field">
            <span className="policy-field-label">key{hasKey && hasSource ? '（当前不生效）' : ''}</span>
            <Select size="small" filter className="policy-field-ctl" placeholder="选择意图 key（锚点 / 保留等待 key）"
              value={dec.key ?? undefined}
              onChange={v => { if (v) patch({ key: v }); }}
              optionList={keyOptions} />
          </label>
          <label className="policy-field">
            <span className="policy-field-label">hint</span>
            <Input size="small" className="policy-field-ctl" value={dec.hint ?? ''} onChange={patchText('hint')} placeholder="展示文案（留空删除该字段）" />
          </label>
          <label className="policy-field">
            <span className="policy-field-label">center</span>
            <CenterEditor dec={dec} anchorKeys={anchorKeys} onChange={onChange} />
          </label>
          <label className="policy-field">
            <span className="policy-field-label">effect</span>
            <Select size="small" className="policy-field-ctl"
              value={dec.effect ?? undefined} placeholder="（无副作用）"
              onChange={v => { if (v === '__none__') omit(['effect']); else patch({ effect: v }); }}
              optionList={[{ value: '__none__', label: '（无）' },
                ...CONTRACT_MIRROR.EFFECT_WHITELIST.map(e => ({ value: e, label: `${e} · ${EFFECT_LABELS[e] || ''}` }))]} />
          </label>
          {dec.effect === 'settle_skip_retry' && (
            <div className="muted small">effect=settle_skip_retry 要求 when 覆盖 stage / clicked_once / settle_income / retry_elapsed / retry_count（后端保存时校验）</div>
          )}
        </div>
      ) : (
        <div className="policy-axis-body">
          <label className="policy-field">
            <span className="policy-field-label">source{hasSource && hasKey ? '（生效中）' : ''}</span>
            <Select size="small" className="policy-field-ctl" placeholder="选择上游决策算法产出"
              value={dec.source ?? undefined}
              onChange={v => { if (v) patch({ source: v }); }}
              optionList={sourceOptions} />
          </label>
          <label className="policy-field">
            <span className="policy-field-label">fallback_key</span>
            <Select size="small" filter className="policy-field-ctl"
              value={dec.fallback_key ?? '__none__'}
              onChange={v => { if (v === '__none__') omit(['fallback_key']); else patch({ fallback_key: v }); }}
              optionList={fallbackOptions} />
          </label>
          <label className="policy-field">
            <span className="policy-field-label">hint</span>
            <Input size="small" className="policy-field-ctl" value={dec.hint ?? ''} onChange={patchText('hint')} placeholder="source 无产出走 fallback 时展示" />
          </label>
        </div>
      )}

      <div className="policy-axis policy-axis-fatal">
        <Switch size="small" checked={hasFatal}
          onChange={c => { if (c) patch({ fatal: '' }); else omit(['fatal']); }} />
        <span className="policy-field-label">fatal（独立开关，可叠加 · 终止原因文案，支持 {'{frame}'} 占位）</span>
      </div>
      {hasFatal && (
        <TextArea size="small" value={dec.fatal} rows={2}
          onChange={v => patch({ fatal: v })}
          placeholder="终止原因文案，例如：点击重试 3 次仍无响应（frame {frame}）" />
      )}
    </div>
  );
}

function CenterEditor({ dec, anchorKeys, onChange }) {
  const cur = dec.center;
  const mode = cur == null ? 'none' : Array.isArray(cur) ? 'xy' : 'anchor';
  const setMode = m => {
    if (m === 'none') {
      const n = { ...dec };
      delete n.center;
      onChange(n);
    } else if (m === 'xy') {
      onChange({ ...dec, center: [0.5, 0.5] });
    } else {
      onChange({ ...dec, center: anchorKeys[0] ?? '' });
    }
  };
  const setAxis = (i, v) => {
    const next = Array.isArray(cur) ? [...cur] : [0.5, 0.5];
    next[i] = v;
    onChange({ ...dec, center: next });
  };
  return (
    <span className="policy-center">
      <Select size="small" value={mode} onChange={setMode} className="policy-center-mode"
        optionList={[
          { value: 'none', label: '（无 center）' },
          { value: 'anchor', label: '锚点 ID' },
          { value: 'xy', label: '[x, y] 坐标（0~1）' },
        ]} />
      {mode === 'anchor' && (
        <Select size="small" filter className="policy-field-ctl" value={typeof cur === 'string' ? cur : undefined}
          onChange={v => onChange({ ...dec, center: v })}
          optionList={anchorKeys.map(k => ({ value: k, label: k }))} />
      )}
      {mode === 'xy' && Array.isArray(cur) && [0, 1].map(i => (
        <InputNumber key={i} size="small" min={0} max={1} step={0.01} style={{ width: 80 }}
          value={cur[i]} onChange={v => { if (typeof v === 'number') setAxis(i, v); }} />
      ))}
    </span>
  );
}

// ---------- ReportPane：后端 report 的非规则级行（解析失败降级整行展示） ----------

function ReportPane({ backendGlobal, report }) {
  const lines = backendGlobal || [];
  if (!report && !lines.length) return null;
  const errCount = (report?.errors || []).length;
  const warnCount = (report?.warnings || []).length;
  return (
    <Banner
      type={errCount > 0 ? 'warning' : 'info'} closeIcon={null} bordered
      description={(
        <div className="policy-issue-list">
          <div className="muted small">
            磁盘/最近保存文档的后端校验：error {errCount} · warning {warnCount}（规则级行已映射到对应卡片）
          </div>
          {lines.map((it, k) => it.parsed ? (
            <div key={k} className={`policy-issue policy-issue-${it.level}`}>
              <Tag size="small" type={it.level === 'error' ? 'solid' : 'light'} color={it.level === 'error' ? 'red' : 'orange'} style={{ margin: 0 }}>{it.parsed.code}</Tag>
              <span className="mono small">{it.parsed.path}</span>
              <span className="small">{it.parsed.message}</span>
            </div>
          ) : (
            <pre key={k} className="policy-issue-raw">{it.raw}</pre>
          ))}
        </div>
      )} />
  );
}

// ---------- TuningPanel：三区键名只读、只改值（未知键/缺区 = P01 硬阻断 → 不提供增删） ----------

function tuningRefCounts(rules) {
  const counts = {};
  for (const rule of rules) {
    for (const cond of parseWhen(rule?.when)) {
      if (typeof cond.value === 'string' && cond.value.startsWith('@')) {
        const name = cond.value.slice(1);
        counts[name] = (counts[name] || 0) + 1;
      }
    }
  }
  return counts;
}

function TuningValueEditor({ section, k, v, onChange }) {
  if (k === 'appraiser_search_roi') {
    if (!Array.isArray(v) || v.length !== 4) {
      return <span className="muted small">当前值非 4 元数组，请经 JSON 高级编辑修正</span>;
    }
    return (
      <span className="policy-roi">
        {[0, 1, 2, 3].map(i => (
          <InputNumber key={i} size="small" min={0} max={1} step={0.01} style={{ width: 72 }}
            value={v[i]} onChange={val => {
              if (typeof val !== 'number') return;
              const next = [...v];
              next[i] = val;
              onChange(next);
            }} />
        ))}
      </span>
    );
  }
  if (typeof v === 'number') {
    return (
      <InputNumber size="small" style={{ width: 140 }}
        min={k === 'click_cooldown_s' ? 0.05 : 0} step={k === 'click_cooldown_s' ? 0.05 : 1}
        value={v} onChange={val => { if (typeof val === 'number') onChange(val); }} />
    );
  }
  return <span className="muted small">{JSON.stringify(v)}（非数值，请经 JSON 高级编辑修改）</span>;
}

function TuningPanel({ tuning, rules, onChange }) {
  const refCounts = tuningRefCounts(rules);
  const setVal = (sec, key, value) => {
    onChange({ ...tuning, [sec]: { ...tuning[sec], [key]: value } });
  };
  return (
    <div className="policy-tuning">
      <div className="insp-title">调参（键名硬白名单只读、只改值 · 改完需重启进程生效）</div>
      <div className="tpl-cols">
        {Object.keys(TUNING_SECTION_LABELS).map(sec => {
          const sectionRaw = tuning[sec];
          return (
            <div key={sec} className="tpl-col">
              <div className="policy-tuning-sec muted small">{TUNING_SECTION_LABELS[sec]}</div>
              {!sectionRaw || typeof sectionRaw !== 'object' ? (
                <div className="small policy-issue policy-issue-error">
                  <Tag size="small" type="solid" color="red" style={{ margin: 0 }}>P01</Tag>
                  <span>tuning.{sec} 缺失或非法（保存将被后端硬阻断；如确需重建请经 JSON 高级编辑）</span>
                </div>
              ) : (
                Object.entries(sectionRaw).map(([k, v]) => {
                  const unknown = !CONTRACT_MIRROR.TUNING_KEYS[sec]?.includes(k);
                  const refs = refCounts[k];
                  return (
                    <div key={k} className="tpl-row policy-tuning-row">
                      <Tooltip content={k}>
                        <span className="mono">{TUNING_KEY_LABELS[k] || k}{unknown ? ' ⚠' : ''}</span>
                      </Tooltip>
                      <span className="tpl-row-right policy-tuning-val">
                        {unknown ? (
                          <span className="small policy-issue policy-issue-error">
                            <Tag size="small" type="solid" color="red" style={{ margin: 0 }}>P01</Tag>
                            <span>未知调参键，保存将被硬阻断</span>
                          </span>
                        ) : (
                          <TuningValueEditor section={sec} k={k} v={v} onChange={val => setVal(sec, k, val)} />
                        )}
                        {sec === 'policy' && refs > 0 && (
                          <span className="muted small">被 {refs} 条规则以 @{k} 引用</span>
                        )}
                      </span>
                    </div>
                  );
                })
              )}
              {sec === 'perception' && (
                <div className="muted small" style={{ marginTop: 4 }}>
                  以上为场景级阈值（作用面是具体算法）；逐锚点 ROI 阈值在 anchors[].threshold，两者不同粒度。
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
