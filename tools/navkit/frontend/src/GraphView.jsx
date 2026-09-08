import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ReactFlow, Background, Controls, MiniMap,
  useNodesState, useEdgesState,
} from '@xyflow/react';
import { Button, Banner, Spin } from '@douyinfe/semi-ui';
import { api } from './api';
import { nodeTypes } from './GraphNodes';
import { edgeTypes } from './ScanEdge';
import { layoutGraph } from './layout';
import { kindColor } from './theme';

// 路径树视图：/api/graph 真实数据 + dagre 自动布局 + 白光扫描。
// 未引用锚点/未担保点不进主图（避免孤链淹没拓扑），收进底部书签抽屉。
export default function GraphView({ traceRows, onSelectNode }) {
  const [doc, setDoc] = useState(null);
  const [err, setErr] = useState(null);
  const [animOn, setAnimOn] = useState(true);
  const [stats, setStats] = useState(null);
  const [rf, setRf] = useState(null);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  // null = 收起；'orphans' / 'unguarded' = 展开对应书签
  const [dock, setDock] = useState(null);
  const rfInstance = useRef(null);

  useEffect(() => {
    api.graph().then(d => {
      setDoc(d);
      const layout = layoutGraph(d, new Set(d.orphans || []));
      const { rfNodes, rfEdges, stats } = layout;
      setNodes(rfNodes);
      setEdges(rfEdges);
      setStats(stats);
      setRf(layout);
    }).catch(e => setErr(String(e)));
  }, []);

  const orphanNodes = useMemo(() => doc
    ? (doc.orphans || []).map(id => doc.nodes.find(n => n.id === id)).filter(Boolean)
    : [], [doc]);
  const unguardedNodes = useMemo(() => doc
    ? (doc.unguarded_points || []).map(id => doc.nodes.find(n => n.id === id)).filter(Boolean)
    : [], [doc]);

  const dockCats = [
    { key: 'orphans', label: '未引用锚点', items: orphanNodes },
    { key: 'unguarded', label: '未担保点', items: unguardedNodes },
  ].filter(c => c.items.length > 0);
  const activeCat = dockCats.find(c => c.key === dock);

  useEffect(() => {
    const fit = () => rfInstance.current?.fitView({ padding: 0.12 });
    window.addEventListener('navkit-fit-view', fit);
    return () => window.removeEventListener('navkit-fit-view', fit);
  }, []);

  if (err) return <Banner type="danger" closeIcon={null} description={`路径树加载失败：${err}`} />;
  if (!doc) return <div className="view-loading"><Spin size="large" tip="加载 /api/graph…" /></div>;

  return (
    <div className="graph-view">
      <div className="graph-toolbar">
        <Button size="small" onClick={() => window.dispatchEvent(new CustomEvent('navkit-fit-view'))}>适应视图（滚轮缩放 / 空格拖移）</Button>
        <label className="toggle">
          <input type="checkbox" checked={animOn} onChange={e => setAnimOn(e.target.checked)} />
          流向高光
        </label>
        <div style={{ flex: 1 }} />
          <span className="muted">
          画布 {stats.edges} · 通配 {stats.hiddenWildcard || 0} · 路由 {stats.hiddenRoutes || 0}
        </span>
      </div>

      <div className={'graph-stage' + (animOn ? '' : ' anim-off')}>
        <ReactFlow
          nodes={nodes} edges={edges}
          onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
          onInit={instance => { rfInstance.current = instance; }}
          nodeTypes={nodeTypes} edgeTypes={edgeTypes}
          onNodeClick={(_, n) => onSelectNode(n)}
          onPaneClick={() => onSelectNode(null)}
          fitView fitViewOptions={{ padding: 0.12 }}
          minZoom={0.08} maxZoom={1.8}
          defaultEdgeOptions={{ type: 'scan' }}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={24} size={1.2} color="#262b36" variant="dots" />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable maskColor="rgba(20,22,28,0.72)"
            nodeColor={n => kindColor(n.data.kind)}
            style={{ background: '#1f232b', width: 160, height: 110 }} />
        </ReactFlow>

        <div className="legend">
          <span className="legend-flow"><i />白光流向 = 寻路方向</span>
          <span><i style={{ background: '#5b8def' }} />模板</span>
          <span><i style={{ background: '#4fd1c5' }} />OCR</span>
          <span><i style={{ background: '#a98ff0' }} />坐标点</span>
          <span><i style={{ background: '#d18a4f' }} />动作</span>
          <span><i style={{ background: '#6d7583' }} />阶段</span>
        </div>
      </div>

      {/* 底部书签抽屉：未引用/未担保收纳 */}
      {dockCats.length > 0 && (
        <div className={'dock' + (dock ? ' open' : '')}>
          <div className="dock-tabs">
            {dockCats.map(c => (
              <button key={c.key} type="button"
                className={'dock-tab' + (dock === c.key ? ' active' : '')}
                onClick={() => setDock(dock === c.key ? null : c.key)}>
                {c.label}
                <span className="dock-badge">{c.items.length}</span>
              </button>
            ))}
            <div style={{ flex: 1 }} />
            {activeCat && <span className="muted small">点击条目 → 右侧检查器查看详情与 Trace</span>}
          </div>
          {activeCat && (
            <div className="dock-body">
              {activeCat.items.map(n => (
                <button key={n.id} type="button" className="dock-chip"
                  onClick={() => onSelectNode({ data: n })}>
                  <span className="nk-dot" style={{ background: kindColor(n.kind) }} />
                  <span className="mono">{n.label}</span>
                  {n.page && <span className="muted small">{n.page}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}