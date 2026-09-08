import React from 'react';
import { Tag, Tabs, Timeline, Empty, Descriptions } from '@douyinfe/semi-ui';
import { SEMI_TAG_COLOR } from './theme';

function fmtTs(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function rowTitle(r) {
  if (r.event === 'click_result') {
    const cr = r.click_result || {};
    return `点击结果 → ${cr.key || '?'} ${cr.ok ? '✓' : '✗'}`;
  }
  if (r.event === 'intent_submitted') return `意图提交 → ${r.intent?.key || '?'}`;
  if (r.event === 'decision') return '决策快照';
  return r.hit_anchor || r.stage || '-';
}

function TraceList({ rows, loadFailed }) {
  if (loadFailed) {
    return <Empty title="决策流水加载失败" description="读取 /api/trace 未成功，刷新或重试后再看" style={{ padding: 16 }} />;
  }
  if (!rows || rows.length === 0) {
    return <Empty title="无关联记录" description="决策流水（trace.jsonl）中没有命中该元素的帧" style={{ padding: 16 }} />;
  }
  return (
    <Timeline style={{ marginTop: 12 }}>
      {rows.map((r, i) => {
        const type = r.click_result?.ok === true ? 'success'
          : r.click_result && r.click_result.ok === false ? 'warning' : 'ongoing';
        const scoreTxt = r.scores && r.hit_anchor && r.scores[r.hit_anchor] != null
          ? ` · ${r.scores[r.hit_anchor].toFixed(2)}` : '';
        return (
          <Timeline.Item key={i} time={fmtTs(r.timestamp_ms)} type={type}
            extra={`frame ${r.frame}${r.round_no != null ? ` · r${r.round_no}` : ''}`}>
            <span className="mono">{rowTitle(r)}</span>
            {scoreTxt}
            {r.click_result && r.click_result.ok === false && (
              <Tag size="small" color="amber" style={{ marginLeft: 6 }}>
                {r.click_result.device_lost ? '设备丢失' : '点击未命中'}
              </Tag>
            )}
          </Timeline.Item>
        );
      })}
    </Timeline>
  );
}

// 右侧检查器：详情 + Trace 两个 Tab（trace 按 stage / hit_anchor / 事件行 key 过滤）
export default function Inspector({ node, traceRows, traceError }) {
  if (!node) {
    return (
      <aside className="inspector">
        <div className="insp-title">检查器</div>
        <Empty title="未选中" description="点击路径树中的节点查看详情与决策流水" style={{ padding: 16 }} />
      </aside>
    );
  }
  const isStage = node.data.kind === 'stage';
  const key = isStage ? node.data.label : node.data.id;
  const rows = (traceRows || [])
    .filter(r => isStage
      ? r.stage === key
      : (r.hit_anchor === key || r.scores?.[key] != null
        || r.click_result?.key === key || r.intent?.key === key))
    .slice(-12).reverse();

  const detail = isStage ? (
    <Descriptions
      align="left"
      data={[
        { key: '阶段', value: <span className="mono">{node.data.label}</span> },
        { key: '页面', value: <Tag size="small" color="white" style={{ margin: 0 }}>{node.data.page || '-'}</Tag> },
        { key: '锚点', value: String(node.data.anchors?.length || 0) },
        { key: 'OCR', value: String(node.data.ocr?.length || 0) },
        { key: '动态收窄', value: node.data.dynamic
          ? <Tag size="small" color="violet" style={{ margin: 0 }}>dynamic_narrow</Tag> : '否' },
      ]}
    />
  ) : (
    <Descriptions
      align="left"
      data={[
        { key: '锚点', value: <span className="mono">{node.data.label}</span> },
        { key: '类型', value: <Tag size="small" color={SEMI_TAG_COLOR[node.data.kind] || 'grey'} style={{ margin: 0 }}>{node.data.kind}</Tag> },
        { key: '归属', value: node.data.owner || '-' },
        { key: '页面', value: node.data.page || '-' },
        { key: '担保', value: node.data.guarded_by
          ? <span className="mono">{node.data.guarded_by}</span>
          : <Tag size="small" color="grey" style={{ margin: 0 }}>无</Tag> },
        { key: '模板', value: (node.data.templates || []).join(', ') || '-' },
      ]}
    />
  );

  return (
    <aside className="inspector">
      <div className="insp-title">检查器</div>
      <Tabs type="button" size="small">
        <Tabs.TabPane tab="详情" itemKey="detail">
          <div style={{ paddingTop: 10 }}>{detail}</div>
        </Tabs.TabPane>
        <Tabs.TabPane tab={`Trace${rows.length ? ` (${rows.length})` : ''}`} itemKey="trace">
          <TraceList rows={rows} loadFailed={traceError} />
        </Tabs.TabPane>
      </Tabs>
    </aside>
  );
}