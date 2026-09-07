// 后端 API 封装（与 tools/navkit/server.py 契约对齐）

async function j(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} → HTTP ${r.status}`);
  return r.json();
}

export const api = {
  sessions: () => j('/api/list_sessions'),
  images: (session) => j(`/api/list_images?session=${encodeURIComponent(session)}`),
  imageUrl: (session, name) => `/api/image?session=${encodeURIComponent(session)}&name=${encodeURIComponent(name)}`,
  templateStatus: () => j('/api/template_status'),
  graph: () => j('/api/graph'),
  assets: () => j('/api/assets'),
  trace: () => j('/api/trace'),
  traceFor: (session) => j(`/api/trace?session=${encodeURIComponent(session)}`),
};

// 保存整份 document：非 2xx 也要读 body（400 时 report/error 携带 P 码，
// 通用 j() 会在 !r.ok 时 throw 并丢弃响应体，故单独实现）
export async function saveAssets(document) {
  const r = await fetch('/api/assets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document }),
  });
  let body = null;
  try { body = await r.json(); } catch { /* 非 JSON 响应按网络错误处理 */ }
  if (body === null) throw new Error(`POST /api/assets → HTTP ${r.status}（响应体不可解析）`);
  return body; // {ok:true, report} | {ok:false, report} | {ok:false, error}
}