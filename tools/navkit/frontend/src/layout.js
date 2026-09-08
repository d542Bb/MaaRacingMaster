// 路径树自动布局：dagre 分层（LR），transitions 拆成 stage→锚点→stage 两段。
// 纯函数：输入 /api/graph 的 document，输出 React Flow 的 nodes/edges。
import Dagre from '@dagrejs/dagre';

const STAGE_W = 208, STAGE_H = 64;
const ANCHOR_W = 176, ANCHOR_H = 46;

function nodeSpec(n) {
  if (n.kind === 'stage') return { w: STAGE_W, h: STAGE_H };
  return { w: ANCHOR_W, h: ANCHOR_H };
}

export function layoutGraph(doc, excludeIds) {
  const exclude = excludeIds instanceof Set ? excludeIds : new Set(excludeIds || []);
  const nodes = exclude.size ? (doc.nodes || []).filter(n => !exclude.has(n.id)) : (doc.nodes || []);
  const edges = doc.edges || [];
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
  const stageId = (name) => (name && byId['stage:' + name] ? 'stage:' + name : null);

  // ---- 生成图元素：transitions 拆两段 ----
  const rfNodes = nodes.map(n => {
    const { w, h } = nodeSpec(n);
    return { id: n.id, position: { x: 0, y: 0 }, type: n.kind === 'stage' ? 'stage' : 'anchor',
             data: n, w, h };
  });
  const rfEdges = [];
  const seen = new Set();
  for (const t of edges) {
    if (t.kind !== 'transition') continue;
    const fromId = stageId(t.from);
    const anchorId = t.on && byId[t.on] ? t.on : null;
    let toId = null;
    if (t.to === 'same') toId = fromId;
    else if (t.to === '*' || (t.to || '').startsWith('$')) toId = fromId; // 特殊目标画回环
    else toId = stageId(t.to);
    if (!fromId) continue;

    // stage → 锚点（on 指定的锚点若存在则汇聚到锚点节点，否则直达）
    const leg1To = anchorId || toId;
    if (!leg1To) continue;
    const k1 = `${fromId}->${leg1To}`;
    if (!seen.has(k1)) {
      seen.add(k1);
      rfEdges.push({ id: 'e1:' + k1, source: fromId, target: leg1To, type: 'scan',
                     data: { label: anchorId ? '' : t.on || '' } });
    }
    // 锚点 → 目标 stage
    if (anchorId && toId) {
      const k2 = `${anchorId}->${toId}`;
      const label = t.to === 'same' ? 'same'
        : (t.to === '*' ? '*' : (t.to || '').startsWith('$') ? t.to : '');
      if (!seen.has(k2)) {
        seen.add(k2);
        rfEdges.push({ id: 'e2:' + k2, source: anchorId, target: toId, type: 'scan',
                       data: { label } });
      }
    }
  }

  // ---- dagre 布局 ----
  const g = new Dagre.graphlib.Graph({ multigraph: true });
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'LR', nodesep: 26, ranksep: 72, marginx: 24, marginy: 24 });
  for (const n of rfNodes) g.setNode(n.id, { width: n.w, height: n.h });
  for (const e of rfEdges) g.setEdge(e.source, e.target, { weight: 2 }, e.id);
  Dagre.layout(g);

  for (const n of rfNodes) {
    const pos = g.node(n.id);
    n.position = { x: pos.x - n.w / 2, y: pos.y - n.h / 2 };
  }

  const transitionEdges = edges.filter(e => e.kind === 'transition');
  const routeEdges = edges.filter(e => e.kind === 'route');
  const hiddenWildcard = transitionEdges.filter(e => e.to === '*' || (e.to || '').startsWith('$')).length;
  const hiddenRoutes = routeEdges.length;
  const stats = {
    stages: rfNodes.filter(n => n.type === 'stage').length,
    anchors: rfNodes.filter(n => n.type === 'anchor').length,
    edges: rfEdges.length,
    transitions: transitionEdges.length,
    hiddenWildcard,
    hiddenRoutes,
  };
  return { rfNodes, rfEdges, stats };
}