import React, { useEffect, useState } from 'react';
import { Tag, Banner, Spin, Descriptions, Button } from '@douyinfe/semi-ui';
import { api } from './api';
import { SEMI_TAG_COLOR } from './theme';

// 资产 v3：/api/assets —— schema 概览 + 校验报告 + 四段统计
export default function AssetsView({ graphDoc, setView }) {
  const [doc, setDoc] = useState(null);
  const [err, setErr] = useState(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    setErr(null);
    api.assets().then(setDoc).catch(e => setErr(String(e)));
  }, [tick]);

  if (err) return <Banner type="danger" closeIcon={null} bordered description={<span>
    资产加载失败：{err}{' '}
    <Button size="small" type="tertiary" theme="light" onClick={() => setTick(t => t + 1)}>重试</Button>
  </span>} />;
  if (!doc) return <div className="view-loading"><Spin size="large" /></div>;

  const { document: d, report } = doc;
  const anchors = Object.entries(d.anchors || {});
  const stages = Object.entries((d.stages || {}).definitions || {});
  const transitions = d.transitions || [];
  const routes = Object.entries(d.routes || {});
  const ruleCount = (d.policies?.rules || []).length;
  const goPolicy = () => setView?.('policy');

  return (
    <div className="panel-view">
      <Banner
        type={report.ok ? 'success' : 'danger'} closeIcon={null} bordered
        description={report.ok
          ? '校验通过：E01-E20 阻断 0 项' + (report.warnings.length ? `，W01-W07 告警 ${report.warnings.length} 项` : '')
          : `校验未通过：${report.errors.join('；')}`}
      />
      {report.warnings.length > 0 && (
        <Banner type="warning" closeIcon={null} bordered description={report.warnings.join('；')} />
      )}

      <Descriptions
        align="left" row
        data={[
          { key: '模块', value: d._module || '-' },
          { key: '锚点', value: String(anchors.length) },
          { key: '阶段', value: String(stages.length) },
          { key: '转移', value: String(transitions.length) },
          { key: '路由', value: String(routes.length) },
          {
            key: '策略规则',
            value: (
              <a className="mono" style={{ cursor: 'pointer' }} onClick={goPolicy}>
                {ruleCount} 条 → 策略编辑
              </a>
            ),
          },
        ]}
      />

      {graphDoc && (
        <Descriptions
          align="left" row
          data={[
            { key: '孤儿锚点', value: String(graphDoc.orphans?.length || 0) },
            { key: '未担保点', value: String(graphDoc.unguarded_points?.length || 0) },
            { key: '页面', value: Object.keys(d.pages || {}).join(' / ') || '-' },
          ]}
        />
      )}

      <div className="insp-title" style={{ marginTop: 16 }}>阶段顺序（stages.order）</div>
      <div className="stage-chain">
        {((d.stages || {}).order || []).map((s, i) => (
          <React.Fragment key={s}>
            {i > 0 && <span className="chain-arrow">→</span>}
            <Tag size="small" color={SEMI_TAG_COLOR.stage} style={{ margin: 0 }}>{s}</Tag>
          </React.Fragment>
        ))}
      </div>

      <div className="insp-title" style={{ marginTop: 16 }}>锚点清单</div>
      <div className="tpl-cols">
        {anchors.map(([name, a]) => (
          <div key={name} className="tpl-row">
            <span className="mono">{name}</span>
            <span className="tpl-row-right">
              <Tag size="small" color={SEMI_TAG_COLOR[a.kind] || 'grey'} style={{ margin: 0 }}>{a.kind}</Tag>
              <span className="muted small">{a.page || '-'}</span>
              {a.guarded_by && <span className="small" title={`guarded_by: ${a.guarded_by}`}>盾</span>}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}