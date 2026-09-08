import React, { useEffect, useState } from 'react';
import { Tag, Banner, Spin, Empty, Button } from '@douyinfe/semi-ui';
import { api } from './api';

// 模板库：/api/template_status —— 已列出 / 未分配 / 悬空
export default function TemplatesView() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    setErr(null);
    api.templateStatus().then(setSt).catch(e => setErr(String(e)));
  }, [tick]);

  if (err) return <Banner type="danger" closeIcon={null} bordered description={<span>
    模板状态加载失败：{err}{' '}
    <Button size="small" type="tertiary" theme="light" onClick={() => setTick(t => t + 1)}>重试</Button>
  </span>} />;
  if (!st) return <div className="view-loading"><Spin size="large" /></div>;

  return (
    <div className="panel-view">
      <div className="stats-row">
        <div className="stat-card">
          <div className="stat-num">{st.listed.length}</div>
          <div className="stat-label">模板文件</div>
        </div>
        <div className="stat-card">
          <div className="stat-num warn">{st.unassigned.length}</div>
          <div className="stat-label">未分配（有文件未被引用）</div>
        </div>
        <div className="stat-card">
          <div className="stat-num danger">{st.dangling.length}</div>
          <div className="stat-label">悬空（引用了不存在的文件）</div>
        </div>
      </div>

      {st.dangling.length > 0 && (
        <Banner type="danger" closeIcon={null} bordered
          description={`悬空引用：${st.dangling.join('、')} —— 运行时会匹配失败，需修复配置或补传模板`} />
      )}

      <div className="tpl-cols">
        <div className="tpl-col">
          <div className="insp-title">未分配模板</div>
          {st.unassigned.length === 0
            ? <Empty description="全部模板均已被引用" style={{ padding: 12 }} />
            : st.unassigned.map(n => (
              <div key={n} className="tpl-row">
                <span className="mono">{n}</span>
                <Tag size="small" color="amber" style={{ margin: 0 }}>可登记</Tag>
              </div>
            ))}
        </div>
        <div className="tpl-col">
          <div className="insp-title">被引用模板（按分类）</div>
          {Object.entries(st.referenced).map(([cat, refs]) => {
            const keys = Object.entries(refs);
            if (keys.length === 0) return null;
            return (
              <div key={cat} style={{ marginBottom: 10 }}>
                <Tag size="small" color="blue" style={{ margin: '0 0 6px' }}>{cat}</Tag>
                {keys.map(([key, tpls]) => (
                  <div key={key} className="tpl-row">
                    <span className="mono">{key}</span>
                    <span className="muted small">{tpls.join(', ')}</span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}