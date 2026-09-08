import React, { useCallback, useEffect, useState } from 'react';
import { Select, Timeline, Empty, Tag, Banner, Button } from '@douyinfe/semi-ui';
import { api } from './api';

function fmtTs(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function evTitle(r) {
  if (r.event === 'click_result') return `点击结果 → ${r.click_result?.key || '?'} ${r.click_result?.ok ? '✓' : '✗'}`;
  if (r.event === 'intent_submitted') return `意图提交 → ${r.intent?.key || '?'}`;
  return r.stage || '-';
}

// 会话回放：真实会话/帧列表 + 决策流水时间轴
// traceRows 为全量兜底（会话切换瞬间的旧值）；正常展示按当前会话单独拉取。
// 事件行（click_result / intent_submitted）按类型渲染；decision 快照行是
// 机读明细，不占人读时间轴的 30 行名额。
export default function ReplayView({ traceRows }) {
  const [sessions, setSessions] = useState([]);
  const [session, setSession] = useState(null);
  const [images, setImages] = useState([]);
  const [image, setImage] = useState(null);
  const [err, setErr] = useState(null);
  const [sessionTrace, setSessionTrace] = useState(null);
  const [tick, setTick] = useState(0);

  const loadSessions = useCallback(() => {
    setErr(null);
    api.sessions().then(list => {
      setSessions(list);
      if (list.length) setSession(list[0]);
    }).catch(e => setErr(String(e)));
  }, []);

  useEffect(() => { loadSessions(); }, [loadSessions, tick]);

  useEffect(() => {
    if (!session) return;
    setImages([]); setImage(null); setSessionTrace(null);
    api.images(session).then(list => {
      setImages(list);
      if (list.length) setImage(list[Math.min(2, list.length - 1)]);
    }).catch(e => setErr(`帧列表加载失败：${e.message}`));
    api.traceFor(session).then(setSessionTrace).catch(e => setErr(`决策流水加载失败：${e.message}`));
  }, [session]);

  // 决策流水按当前会话过滤（trace.jsonl 按会话落盘，行内无会话名，靠文件归属对应）
  const rows = (sessionTrace ?? (traceRows || []).slice(-30))
    .filter(r => r.event !== 'decision').slice(-30).reverse();

  return (
    <div className="replay-view">
      {err && (
        <Banner type="danger" closeIcon={null} bordered
          description={<span>{err}{' '}
            <Button size="small" type="tertiary" theme="light" onClick={() => { setErr(null); setTick(t => t + 1); }}>重试</Button>
          </span>} />
      )}
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
                      <span className="mono">{evTitle(r)}</span>
                      {r.hit_anchor && !r.event && <span> · 命中 <span className="mono">{r.hit_anchor}</span></span>}
                      {r.click_result && r.event === 'click_result' && (
                        <Tag size="small" color={r.click_result.ok ? 'green' : 'red'} style={{ marginLeft: 6 }}>
                          {r.click_result.ok ? '成功' : r.click_result.device_lost ? '设备丢失' : '失败'}
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
