// 后端 API 封装（与 tools/navkit/server.py 契约对齐）

async function j(url) {
  const r = await fetch(url);
  const text = await r.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (!r.ok) {
    const err = new Error(body?.error || `${url} → HTTP ${r.status}`);
    err.status = r.status;
    err.body = body;
    throw err;
  }
  if (body === null) throw new Error(`${url} → 响应 JSON 解析失败`);
  return body;
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
  modules: () => j('/api/modules'),
};

// 切换编辑模块：成功返回 {ok, module, changed}，失败返回 {ok:false, error}
export async function switchModule(module) {
  const r = await fetch('/api/switch_module', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ module }),
  });
  let body = null;
  try { body = await r.json(); } catch { /* 非 JSON 响应按网络错误处理 */ }
  if (body === null) throw new Error(`POST /api/switch_module → HTTP ${r.status}（响应体不可解析）`);
  return body;
}

// 保存整份 document：非 2xx 也要读 body（400 时 report/error 携带 P 码，
// 通用 j() 会在 !r.ok 时 throw 并丢弃响应体，故单独实现）
export async function previewAssets(document, base_hash) {
  const r = await fetch('/api/assets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document, base_hash, preview: true }),
  });
  const text = await r.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (body === null) throw new Error(`POST /api/assets → 响应 JSON 解析失败`);
  return body;
}

export async function saveAssets(document, base_hash) {
  const r = await fetch('/api/assets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document, base_hash }),
  });
  let body = null;
  try { body = await r.json(); } catch { /* 非 JSON 响应按网络错误处理 */ }
  if (body === null) throw new Error(`POST /api/assets → HTTP ${r.status}（响应体不可解析）`);
  return body; // {ok:true, report} | {ok:false, report} | {ok:false, error}
}

// 关闭 server 进程（顶栏「退出」按钮配套）：server 会先返回 200 再调 os._exit，
// 所以正常拿到响应，也可能因 socket 已关拿不到——任何错误都按"已退出"处理。
export async function shutdownServer() {
  try {
    const r = await fetch('/api/shutdown', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    let body = null;
    try { body = await r.json(); } catch { /* server may already be gone */ }
    return body;
  } catch {
    return null; // 网络层失败也按已退出处理
  }
}