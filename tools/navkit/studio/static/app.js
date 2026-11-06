"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  session: "", module: "", behavior: "",
  behaviors: [], frames: [], manifest: null, idx: 0,
  nodes: [], node: null, rect: null, drag: null,
};

function setStatus(text) {
  $("status").textContent = text || "";
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => null);
  if (!res.ok || (body && body.ok === false)) {
    const errs = body && body.errors ? body.errors.join("\n") : res.statusText;
    throw new Error(errs || `HTTP ${res.status}`);
  }
  return body;
}

function option(select, value, label) {
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = label;
  select.appendChild(opt);
}

function frameQuery() {
  return new URLSearchParams({
    session: S.session, module: S.module, behavior: S.behavior,
  }).toString();
}

// ---------------- 会话 / 行为 / 帧 ----------------

async function loadSessions() {
  const data = await api("/api/replay/sessions");
  const el = $("session");
  el.innerHTML = "";
  for (const name of data.sessions) option(el, name, name);
  if (!data.sessions.length) {
    S.session = ""; S.frames = []; S.manifest = null;
    setStatus("没有可回放的 v5 会话");
    return;
  }
  el.value = data.sessions[0];
  await loadBehaviors();
}

async function loadBehaviors() {
  S.session = $("session").value;
  const data = await api(`/api/replay/behaviors?session=${encodeURIComponent(S.session)}`);
  S.behaviors = data.behaviors;
  const el = $("behavior");
  el.innerHTML = "";
  for (const b of S.behaviors) {
    option(el, `${b.module}/${b.behavior}`,
      `${b.module}/${b.behavior}（${b.frames} 帧）`);
  }
  if (!S.behaviors.length) {
    S.module = S.behavior = ""; S.frames = [];
    setStatus("该会话没有带清单的行为目录");
    renderAll();
    return;
  }
  el.value = `${S.behaviors[0].module}/${S.behaviors[0].behavior}`;
  await loadFrames();
  await loadTimeline();
}

async function loadFrames() {
  const [module, behavior] = $("behavior").value.split("/");
  S.module = module || "";
  S.behavior = behavior || "";
  const data = await api(`/api/replay/frames?${frameQuery()}`);
  S.manifest = data.manifest;
  S.frames = data.frames;
  S.idx = Math.max(0, Math.min(S.idx, S.frames.length - 1));
  $("scrub").max = String(Math.max(0, S.frames.length - 1));
  $("scrub").value = String(S.idx);
  $("manifest").textContent = S.manifest
    ? JSON.stringify(S.manifest, null, 2) : "（无清单）";
  setStatus(`${S.frames.length} 帧`);
  renderAll();
}

async function loadTimeline() {
  if (!S.session) { $("timeline").textContent = ""; return; }
  const data = await api(`/api/replay/timeline?session=${encodeURIComponent(S.session)}`);
  $("timeline").textContent = data.rows.map((r) => JSON.stringify(r)).join("\n");
}

function currentFrame() {
  return S.frames[S.idx] || null;
}

function frameUrl() {
  const row = currentFrame();
  if (!row) return "";
  return `/api/replay/frame?${frameQuery()}&seq=${encodeURIComponent(row.seq)}`;
}

function renderAll() {
  const row = currentFrame();
  $("frame-pos").textContent = S.frames.length
    ? `${S.idx + 1} / ${S.frames.length}` : "0 / 0";
  $("prev").disabled = S.idx <= 0;
  $("next").disabled = S.idx >= S.frames.length - 1;
  const url = frameUrl();
  if (url) {
    $("frame").src = url;
    $("roi-frame").src = url;
  } else {
    $("frame").removeAttribute("src");
    $("roi-frame").removeAttribute("src");
  }
  const meta = $("frame-meta");
  meta.innerHTML = "";
  if (row) {
    const fields = {
      seq: row.seq, ts_mono_ns: row.ts_mono_ns, file: row.file,
      fid: row.fid ?? "", stage: row.stage ?? "", round: row.round ?? "",
      run_id: row.run_id ?? "", hud: row.hud ?? "",
    };
    for (const [key, value] of Object.entries(fields)) {
      const dt = document.createElement("dt"); dt.textContent = key;
      const dd = document.createElement("dd"); dd.textContent = String(value);
      meta.append(dt, dd);
    }
  }
  drawOverlay();
  drawRoi();
}

function step(delta) {
  const next = Math.max(0, Math.min(S.frames.length - 1, S.idx + delta));
  if (next === S.idx) return;
  S.idx = next;
  $("scrub").value = String(S.idx);
  renderAll();
}

// ---------------- 回放叠加层（空；留给 ROI 页） ----------------

function drawOverlay() {
  const img = $("frame");
  const canvas = $("overlay");
  if (!img.naturalWidth) return;
  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
}

// ---------------- ROI ----------------

async function loadNodes() {
  const data = await api("/api/roi/nodes");
  S.nodes = data.nodes;
  const el = $("node");
  el.innerHTML = "";
  for (const n of S.nodes) {
    option(el, String(S.nodes.indexOf(n)),
      `${n.module} · ${n.page ? n.page + " · " : ""}${n.node}`);
  }
  if (S.nodes.length) {
    el.value = "0";
    selectNode();
  } else {
    S.node = null; S.rect = null;
    $("node-info").textContent = "没有带归一化矩形的节点";
    drawRoi();
  }
}

function selectNode() {
  const idx = Number($("node").value);
  S.node = S.nodes[idx] || null;
  S.rect = S.node ? [...S.node.rect] : null;
  if (S.node) {
    $("node-info").textContent =
      `${S.node.file} · ${S.node.colorspace || "—"} · ${(S.node.templates || []).join(", ")}`;
    syncRectInputs();
  }
  drawRoi();
}

function syncRectInputs() {
  const r = S.rect;
  if (!r) return;
  $("rx1").value = r[0]; $("ry1").value = r[1];
  $("rx2").value = r[2]; $("ry2").value = r[3];
}

function rectFromInputs() {
  const values = ["rx1", "ry1", "rx2", "ry2"].map((id) => Number($(id).value));
  if (values.some((v) => !Number.isFinite(v))) return null;
  const [x1, y1, x2, y2] = values;
  if (!(x1 >= 0 && y1 >= 0 && x2 <= 1 && y2 <= 1 && x1 < x2 && y1 < y2)) return null;
  return values;
}

function drawRoi() {
  const img = $("roi-frame");
  const canvas = $("roi-overlay");
  if (!img.naturalWidth) return;
  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!S.rect) return;
  const [x1, y1, x2, y2] = S.rect;
  const x = x1 * canvas.width, y = y1 * canvas.height;
  const w = (x2 - x1) * canvas.width, h = (y2 - y1) * canvas.height;
  ctx.strokeStyle = "#ff3b30";
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, w, h);
  ctx.fillStyle = "rgba(255,59,48,.15)";
  ctx.fillRect(x, y, w, h);
  ctx.fillStyle = "#ff3b30";
  for (const [hx, hy] of [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) {
    ctx.fillRect(hx - 4, hy - 4, 8, 8);
  }
}

function normPoint(evt, canvas) {
  const box = canvas.getBoundingClientRect();
  return [
    Math.min(1, Math.max(0, (evt.clientX - box.left) / box.width)),
    Math.min(1, Math.max(0, (evt.clientY - box.top) / box.height)),
  ];
}

function nearCorner(r, p) {
  if (!r) return -1;
  const [x1, y1, x2, y2] = r;
  const corners = [[x1, y1], [x2, y1], [x1, y2], [x2, y2]];
  return corners.findIndex(([x, y]) =>
    Math.abs(x - p[0]) < 0.015 && Math.abs(y - p[1]) < 0.015);
}

function installRoiDrag() {
  const canvas = $("roi-overlay");
  canvas.addEventListener("mousedown", (evt) => {
    if (!S.node || !S.rect) return;
    const p = normPoint(evt, canvas);
    const corner = nearCorner(S.rect, p);
    if (corner >= 0) {
      S.drag = { mode: "resize", corner, start: S.rect.slice() };
    } else if (p[0] >= S.rect[0] && p[0] <= S.rect[2]
               && p[1] >= S.rect[1] && p[1] <= S.rect[3]) {
      S.drag = { mode: "move", start: S.rect.slice(), origin: p };
    } else {
      S.drag = { mode: "draw", anchor: p };
      S.rect = [p[0], p[1], p[0], p[1]];
    }
    evt.preventDefault();
  });
  window.addEventListener("mousemove", (evt) => {
    if (!S.drag) return;
    const p = normPoint(evt, canvas);
    if (S.drag.mode === "draw") {
      const [ax, ay] = S.drag.anchor;
      S.rect = [Math.min(ax, p[0]), Math.min(ay, p[1]),
                Math.max(ax, p[0]), Math.max(ay, p[1])];
    } else if (S.drag.mode === "move") {
      const [x1, y1, x2, y2] = S.drag.start;
      const dx = p[0] - S.drag.origin[0], dy = p[1] - S.drag.origin[1];
      const w = x2 - x1, h = y2 - y1;
      const nx1 = Math.min(1 - w, Math.max(0, x1 + dx));
      const ny1 = Math.min(1 - h, Math.max(0, y1 + dy));
      S.rect = [nx1, ny1, nx1 + w, ny1 + h];
    } else {
      const r = S.drag.start.slice();
      const c = S.drag.corner;
      if (c === 0) { r[0] = p[0]; r[1] = p[1]; }
      if (c === 1) { r[2] = p[0]; r[1] = p[1]; }
      if (c === 2) { r[0] = p[0]; r[3] = p[1]; }
      if (c === 3) { r[2] = p[0]; r[3] = p[1]; }
      S.rect = [Math.min(r[0], r[2]), Math.min(r[1], r[3]),
                Math.max(r[0], r[2]), Math.max(r[1], r[3])];
    }
    syncRectInputs();
    drawRoi();
  });
  window.addEventListener("mouseup", () => {
    S.drag = null;
    if (S.rect && (S.rect[2] - S.rect[0] < 0.002 || S.rect[3] - S.rect[1] < 0.002)) {
      S.rect = S.node ? [...S.node.rect] : null;
      syncRectInputs();
      drawRoi();
    }
  });
}

async function saveRoi() {
  if (!S.node) { $("save-result").textContent = "未选节点"; return; }
  const rect = rectFromInputs();
  if (!rect) { $("save-result").textContent = "矩形非法：需 0≤x1<x2≤1、0≤y1<y2≤1"; return; }
  $("save").disabled = true;
  try {
    const data = await api("/api/roi/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: S.node.file, node: S.node.node, rect }),
    });
    S.rect = data.rect || rect;
    S.node.rect = [...S.rect];
    $("save-result").textContent = data.changed
      ? (data.diff || "已保存") : "值未变化";
    setStatus(data.changed ? "已保存并通过校验" : "未变化");
  } catch (err) {
    $("save-result").textContent = String(err.message || err);
    setStatus("保存失败");
  } finally {
    $("save").disabled = false;
  }
}

// ---------------- 事件绑定 ----------------

function bind() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".pane").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $(`pane-${tab.dataset.tab}`).classList.add("active");
      if (tab.dataset.tab === "roi") drawRoi();
    });
  });
  $("session").addEventListener("change", () => loadBehaviors().catch(report));
  $("behavior").addEventListener("change", () => {
    S.idx = 0;
    loadFrames().catch(report);
  });
  $("reload").addEventListener("click", () => {
    loadSessions().catch(report);
    loadNodes().catch(report);
  });
  $("prev").addEventListener("click", () => step(-1));
  $("next").addEventListener("click", () => step(1));
  $("scrub").addEventListener("input", () => {
    S.idx = Number($("scrub").value);
    renderAll();
  });
  $("frame").addEventListener("load", drawOverlay);
  $("roi-frame").addEventListener("load", drawRoi);
  window.addEventListener("resize", () => { drawOverlay(); drawRoi(); });
  $("node").addEventListener("change", selectNode);
  ["rx1", "ry1", "rx2", "ry2"].forEach((id) => {
    $(id).addEventListener("change", () => {
      const rect = rectFromInputs();
      if (rect) { S.rect = rect; drawRoi(); }
    });
  });
  $("save").addEventListener("click", saveRoi);
  installRoiDrag();
}

function report(err) {
  setStatus(String(err.message || err));
}

async function boot() {
  bind();
  setInterval(() => fetch("/api/ping").catch(() => null), 5000);
  await loadNodes().catch(report);
  await loadSessions().catch(report);
}

boot();
