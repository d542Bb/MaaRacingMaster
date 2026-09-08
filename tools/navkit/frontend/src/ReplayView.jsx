import React, { useEffect, useState } from 'react';
import { Select, Timeline, Empty, Tag, Banner } from '@douyinfe/semi-ui';
import { api } from './api';

function fmtTs(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 会话回放：真实会话/帧列表 + 决策流水时间轴
// traceRows 为全量兜底（会话切换瞬间的旧值）；正常展示按当前会话单独拉取。
export default function ReplayView({ traceRows }) {
  const [sessions, setSessions] = useState([]);
  const [session, setSession] = useState(null);
  const [images, setImages] = useState([]);
  const [image, setImage] = useState(null);
  const [err, setErr] = useState(null);
  const [sessionTrace, setSessionTrace] = useState(null);

  useEffect(() => {
    api.sessions().then(list => {
      setSessions(list);
      if (list.length) setSession(list[0]);
    }).catch(e => setErr(String(e)));
  }, []);

  useEffect(() => {
    if (!session) return;
    setImages([]); setImage(null); setSessionTrace(null);
    api.images(session).then(list => {
      setImages(list);
      if (list.length) setImage(list[Math.min(2, list.length - 1)]);
    }).catch(e => setErr(String(e)));
    api.traceFor(session).then(setSessionTrace).catch(e => setErr(`决策流水加载失败：${e.message}`));
  }, [session]);

  if (err) return <Banner type="danger" closeIcon={null} description={`会话数据加载失败：${err}`} />;

  // 决策流水按当前会话过滤（trace.jsonl 按会话落盘，行内无会话名，靠文件归属对应）
  const rows = (sessionTrace ?? (traceRows || []).slice(-30)).slice(-30).reverse();

  return (
    <div className="replay-view">
      <div className="graph-toolbar">
        <span className="muted">会话</span>
        <Select value={session} onChange={setSession} style={{ width: 210 }}
          optionList={sessions.map(s => ({ value: s, label: s }))} />
        <span className="muted">帧</span>
        <Select value={image} onChange={setImage} style={{ width: 150 }} filter
          optionList={images.map(n => ({ value: n, label: n.replace(/_raw\.(png|jpg)$/, '') }))} />
        <div style={{ flex: 1 }} />
        <span className="muted">{images.length} 帧</span>
      </div>

      <div className="replay-body">
        <div className="replay-frame">
          {image
            ? <img src={api.imageUrl(session, image)} alt={image} />
            : <Empty title="选择一帧" description="从上方帧下拉中选择截图" style={{ padding: 24 }} />}
          {image && <div className="replay-frame-name mono">{image}</div>}
        </div>
        <div className="replay-trace">
          <div className="insp-title">决策流水（最近 30 行）</div>
          {rows.length === 0
            ? <Empty title="暂无 trace" description="未发现 trace.jsonl 记录" style={{ padding: 24 }} />
            : (
              <Timeline>
                {rows.map((r, i) => {
                  const type = r.click_result?.ok === true ? 'success'
                    : r.click_result && r.click_result.ok === false ? 'warning' : 'ongoing';
                  return (
                    <Timeline.Item key={i} time={fmtTs(r.timestamp_ms)} type={type}
                      extra={`frame ${r.frame}${r.round_no != null ? ` · r${r.round_no}` : ''}`}>
                      <span className="mono">{r.stage || '-'}</span>
                      {r.hit_anchor && <span> · 命中 <span className="mono">{r.hit_anchor}</span></span>}
                      {r.click_result && (
                        <Tag size="small" color={r.click_result.ok ? 'green' : 'red'} style={{ marginLeft: 6 }}>
                          {r.click_result.ok ? '点击' : '失败'}
                        </Tag>
                      )}
                    </Timeline.Item>
                  );
                })}
              </Timeline>
            )}
        </div>
      </div>
    </div>
  );
}