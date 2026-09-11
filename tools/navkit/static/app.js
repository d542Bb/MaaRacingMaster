/* 鉴宝视觉调试台前端逻辑（ROI 校准台 · 数据面适配 v4 真源） */
import createHistory from "/static/history.js";
"use strict";

function emitDirty() {
  parent.postMessage({ source: "navkit-calib", dirty: state.dirty }, "*");
}
function snapshotState() {
  return {
    rois: structuredClone(state.rois),
    currentCat: state.currentCat,
    selected: state.selected,
    added: structuredClone(state.added),
    deleted: structuredClone(state.deleted),
  };
}
function pushHistory(label, key = "") {
  if (state.rois) state.history.push(label, snapshotState(), key);
}
function restoreHistory(snapshot) {
  if (!snapshot) return;
  state.rois = snapshot.rois;
  state.currentCat = snapshot.currentCat;
  state.selected = snapshot.selected;
  state.added = snapshot.added;
  state.deleted = snapshot.deleted;
  markDirty();
  emitDirty();
  renderCatTabs(); renderRoiList(); updatePropPanel(); draw();
}
function markDirty() { state.dirty = true; emitDirty(); }

function runHistory(dir) {
  const cur = snapshotState();
  const s = dir === "undo" ? state.history.undo(cur) : state.history.redo(cur);
  if (s) { restoreHistory(s); flash(dir === "undo" ? "已撤回" : "已重做"); }
  else flash(dir === "undo" ? "没有可撤回的步骤" : "已是最新状态");
}

// ---------------- 全局状态 ----------------
// 分类 tab 不硬编码，而是从 JSON 实际存在的键动态生成（见 catList()）。
// 固定追加「未分配」页签。分类键 = spec 的 kind 三值 + v4 新增的 nodes / tuning 两组。
// 分类中文标签映射：
const CAT_LABELS = {
  template: "模板锚点",
  point: "点位锚点",
  ocr: "OCR 锚点",
  nodes: "节点 rect",
  tuning: "调参 rect",
};
// 只有 spec 锚点分组可新增/删除（nodes/tuning 是只读镜像面，图结构编辑归 MPE）
const EDITABLE_CATS = new Set(["template", "point", "ocr"]);
// 当前 JSON 存在哪些分类（基于 state.rois 的键），未分配始终追加。
// 顶层 `_meta` 等元数据按键前缀 `_` 或在 KNOWN 白名单中排除。
const CAT_KEYS = new Set(["template", "point", "ocr", "nodes", "tuning"]);
function catList() {
  const cats = Object.keys((state.rois && typeof state.rois === "object") ? state.rois : {})
    .filter((k) => CAT_KEYS.has(k)) // 只认已知分类，忽略 _schema_ver/reference_size 等元数据
    .map((id) => ({ id, label: CAT_LABELS[id] || id }));
  cats.push({ id: "unassigned", label: "未分配" });
  return cats;
}

const DEFAULT_MATCH_THRESHOLD = 0.75;

const state = {
  sessions: [],
  session: null,
  images: [],
  image: null,
  templates: [],
  rois: null,          // {template, point, ocr, nodes, tuning, _meta}
  templateStatus: null, // {listed, referenced, unassigned, dangling}
  currentCat: "template",
  selected: null,      // 当前选中的 ROI key（属于 currentCat）
  img: null,           // HTMLImageElement
  imgW: 0, imgH: 0,    // 实际尺寸
  scale: 1, offsetX: 0, offsetY: 0,
  activeTpl: {},       // {"cat/key": templateName}
  onlyThisSide: {},    // {"nodes/<key>": true} 节点组「仅改此面」逃生口（默认联动）
  dirty: false,
  baseHash: null,
  added: {},
  deleted: [],
  history: null,
  // 画布显示设置
  showRois: "all",     // "all" 全部框 | "selected" 仅选中 | "none" 不显示
  showHit: true,       // 是否显示匹配命中位置（黄色高亮框 + 中心点）
  matchHit: null,      // 最近一次匹配命中 {key, rect:[归一化4值], score} | null
  eggHits: null,       // 彩蛋识别测试结果 [{color, box, count_rect, count, score}, ...] | null
};

// 当前 ROI 的自定义阈值（缺省 0.75）
function getRoiThreshold(roi) {
  if (!roi || typeof roi !== "object") return DEFAULT_MATCH_THRESHOLD;
  const t = roi.threshold;
  if (typeof t === "number" && !Number.isNaN(t) && t >= 0 && t <= 1) return t;
  return DEFAULT_MATCH_THRESHOLD;
}

state.history = createHistory();
const $ = (id) => document.getElementById(id);
const canvas = $("stage");
const ctx = canvas.getContext("2d");
const COLORS = [
  "#89b4fa", "#a6e3a1", "#f9e2af", "#f38ba8", "#cba6f7",
  "#94e2d5", "#fab387", "#f5c2e7", "#89dceb", "#b4befe",
];

// 当前分类的 ROI 字典
function currentRois() {
  return (state.rois && state.rois[state.currentCat]) || {};
}
function roiKeys(cat) {
  // 过滤段内 `_` 前缀元数据键（如 _comment）：只返回真实 ROI，避免假条目进列表/被选中
  return Object.keys((cat === "unassigned" ? {} : state.rois[cat]) || {})
    .filter((k) => !k.startsWith("_"));
}
// activeTpl 的键：区分不同分类下的同名 ROI
function tplKey(roiKey) {
  return `${state.currentCat}/${roiKey}`;
}

// ---------------- 初始化 ----------------
async function init() {
  try {
    await loadSessions();
    await loadTemplates();
    await loadRois();
    await loadTemplateStatus();
    bindEvents();
    renderCatTabs();
    renderRoiList();
    if (state.session) await loadImages();
    if (state.image) await loadImage();
    updatePropPanel();
    // 等待 Flex 布局完全稳定后再 fit 一次 canvas（防容器尺寸为 0 / 被压缩成小条）
    requestAnimationFrame(() => requestAnimationFrame(fitCanvas));
  } catch (e) {
    console.error("init 失败:", e);
    alert("页面初始化失败：" + e.message + "\n请按 F12 打开控制台查看详细错误");
  }
}

function fillSelect(sel, items, selected) {
  sel.innerHTML = items.map(v => `<option value="${v}" ${v === selected ? "selected" : ""}>${v}</option>`).join("");
}

async function apiGet(url) {
  const r = await fetch(url);
  const ct = r.headers.get("Content-Type") || "";
  const text = await r.text();
  if (!r.ok) {
    let errMsg = `HTTP ${r.status}`;
    if (ct.includes("application/json")) {
      try { const o = JSON.parse(text); if (o && o.error) errMsg += ": " + o.error; } catch {}
    } else if (text && text.trim().startsWith("<")) {
      errMsg += "（返回 HTML，非预期 JSON）";
    } else if (text) {
      errMsg += ": " + text.slice(0, 200);
    }
    throw new Error(errMsg);
  }
  if (ct.includes("application/json") || text.trim().startsWith("{") || text.trim().startsWith("[")) {
    try { return JSON.parse(text); } catch (e) {
      throw new Error("响应 JSON 解析失败: " + e.message + "（" + text.slice(0, 80) + "）");
    }
  }
  throw new Error("返回内容不是 JSON，无法解析（" + text.slice(0, 80) + "）");
}
async function apiPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const ct = r.headers.get("Content-Type") || "";
  const text = await r.text();
  if (!r.ok) {
    let errMsg = `HTTP ${r.status}`;
    if (ct.includes("application/json")) {
      try { const o = JSON.parse(text); if (o && o.error) errMsg += ": " + o.error; } catch {}
    } else if (text && text.trim().startsWith("<")) {
      errMsg += "（返回 HTML，路由可能不存在或被静态服务拦截）";
    } else if (text) {
      errMsg += ": " + text.slice(0, 200);
    }
    throw new Error(errMsg);
  }
  if (ct.includes("application/json") || text.trim().startsWith("{") || text.trim().startsWith("[")) {
    try { return JSON.parse(text); } catch (e) {
      throw new Error("响应 JSON 解析失败: " + e.message + "（前 80 字: " + text.slice(0, 80) + "）");
    }
  }
  throw new Error("返回内容不是 JSON，无法解析（前 80 字: " + text.slice(0, 80) + "）");
}

async function loadSessions() {
  state.sessions = await apiGet("/api/list_sessions");
  state.session = state.sessions[0] || null;
  fillSelect($("sessionSelect"), state.sessions, state.session);
}
async function loadTemplates() {
  state.templates = await apiGet("/api/list_templates");
}
async function loadRois() {
  state.rois = await apiGet("/api/rois");
  state.baseHash = state.rois._meta?.base_hash || state.baseHash;
  state.selected = roiKeys(state.currentCat)[0] || null;
}
async function loadTemplateStatus() {
  state.templateStatus = await apiGet("/api/template_status");
}
async function loadImages() {
  state.images = await apiGet(`/api/list_images?session=${encodeURIComponent(state.session)}`);
  state.image = state.images[state.images.length - 1] || null;
  fillSelect($("imageSelect"), state.images, state.image);
}
function loadImage() {
  return new Promise((resolve) => {
    if (!state.session || !state.image) {
      // 会话无帧（纯 trace 会话/无 raw 目录）：必须清掉上一会话画面，
      // 否则会对着已不属于当前会话的旧图拖框校准
      state.img = null; state.imgW = 0; state.imgH = 0; state.matchHit = null;
      fitCanvas();
      return resolve();
    }
    const img = new Image();
    img.crossOrigin = "anonymous";
    let finished = false;
    const done = () => { if (!finished) { finished = true; resolve(); } };
    const timer = setTimeout(() => {
      console.warn("图片加载超时:", state.image);
      done();
    }, 10000);
    img.onload = () => {
      clearTimeout(timer);
      state.img = img;
      state.imgW = img.naturalWidth;
      state.imgH = img.naturalHeight;
      state.matchHit = null; // 新截图旧命中失效
      fitCanvas();
      done();
      // 截图加载完毕后重跑一次（非 OCR→匹配；OCR→识别）
      if (state.currentCat === "ocr") scheduleOcr(); else scheduleMatch();
    };
    img.onerror = (e) => {
      clearTimeout(timer);
      console.error("图片加载失败:", state.image, e);
      flash("❌ 加载截图失败: " + state.image);
      done();
    };
    img.src = `/api/image?session=${encodeURIComponent(state.session)}&name=${encodeURIComponent(state.image)}`;
  });
}

// ---------------- Canvas 布局 ----------------
function fitCanvas() {
  const wrap = document.querySelector(".canvas-wrap");
  const cw = wrap.clientWidth, ch = wrap.clientHeight;
  canvas.width = cw; canvas.height = ch;
  if (!state.img) return;
  state.scale = Math.min(cw / state.imgW, ch / state.imgH);
  state.offsetX = (cw - state.imgW * state.scale) / 2;
  state.offsetY = (ch - state.imgH * state.scale) / 2;
  draw();
}

// 归一化坐标 <-> 画布像素
function normToCanvas(xn, yn) {
  return [state.offsetX + xn * state.imgW * state.scale,
          state.offsetY + yn * state.imgH * state.scale];
}
function canvasToNorm(cx, cy) {
  return [(cx - state.offsetX) / (state.imgW * state.scale),
          (cy - state.offsetY) / (state.imgH * state.scale)];
}
// 圆角矩形工具：stroke+fill 可选
function roundRect(ctx, x, y, w, h, r, fill, stroke) {
  if (w < 2 * r) r = w / 2;
  if (h < 2 * r) r = h / 2;
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
  if (fill) ctx.fill();
  if (stroke) ctx.stroke();
}

// ============ 框样式约定（各分类统一）============
// 【模板匹配框】：粗实线 + 四角角标 + 顶部深色标题栏（score 等）→ 表达「模板匹配命中」
function drawMatchBox(x1, y1, x2, y2, color, label) {
  ctx.save();
  ctx.lineWidth = 3;
  ctx.strokeStyle = color;
  ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
  const sq = 5;
  ctx.fillStyle = color;
  ctx.fillRect(x1 - 2, y1 - 2, sq, sq); ctx.fillRect(x2 - sq + 2, y1 - 2, sq, sq);
  ctx.fillRect(x1 - 2, y2 - sq + 2, sq, sq); ctx.fillRect(x2 - sq + 2, y2 - sq + 2, sq, sq);
  ctx.font = "bold 12px sans-serif";
  const tw = ctx.measureText(label).width;
  ctx.fillRect(x1, y1 - 20, tw + 10, 18);
  ctx.fillStyle = "#111";
  ctx.fillText(label, x1 + 5, y1 - 7);
  ctx.restore();
}

// 【OCR 区域框】：细虚线 + 半透明填充 + pill 标签 → 表达「这块是 OCR 识别区」
function drawOcrBox(x1, y1, x2, y2, color, label) {
  ctx.save();
  ctx.fillStyle = color + "22";
  ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
  ctx.setLineDash([5, 4]);
  ctx.lineWidth = 1.8;
  ctx.strokeStyle = color;
  ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
  ctx.setLineDash([]);
  ctx.font = "11px sans-serif";
  const tw = ctx.measureText(label).width;
  ctx.fillStyle = "#111";
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  roundRect(ctx, x1 + 3, y1 - 15, tw + 8, 14, 4, true, true);
  ctx.fillStyle = color;
  ctx.fillText(label, x1 + 7, y1 - 5);
  ctx.restore();
}

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!state.img) return;
  ctx.drawImage(state.img, state.offsetX, state.offsetY, state.imgW * state.scale, state.imgH * state.scale);
  // 只画当前分类的 ROI（未分配 tab 无 ROI）
  if (state.currentCat === "unassigned") return;
  const rois = currentRois();
  const keys = Object.keys(rois).filter((k) => !k.startsWith("_"));
  keys.forEach((key, i) => {
    if (state.showRois === "none") return;
    if (state.showRois === "selected" && key !== state.selected) return;
    const color = COLORS[i % COLORS.length];
    const r = rois[key];
    if (!r || !Array.isArray(r.rect)) return;
    const [x1, y1] = normToCanvas(r.rect[0], r.rect[1]);
    const [x2, y2] = normToCanvas(r.rect[2], r.rect[3]);
    const isSel = key === state.selected;
    if (state.currentCat === "ocr") {
      // OCR 分类：区域即 OCR 识别区 → 用 OCR 框样式（虚线 + 半透明 + pill 标签）
      drawOcrBox(x1, y1, x2, y2, color, key);
    } else {
      // 模板匹配分类：矩形只是搜索区 → 细实线 + 键名
      ctx.strokeStyle = color;
      ctx.lineWidth = isSel ? 3 : 1.5;
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      ctx.fillStyle = color;
      ctx.font = "12px sans-serif";
      ctx.fillText(key, x1 + 2, y1 - 4);
    }
    if (isSel) {
      // 右下角缩放手柄
      ctx.fillStyle = "#fff";
      ctx.fillRect(x2 - 8, y2 - 8, 8, 8);
      ctx.strokeStyle = color;
      ctx.strokeRect(x2 - 8, y2 - 8, 8, 8);
    }
  });
  // 匹配命中位置（由 showHit 开关控制）：模板匹配框样式（粗实线 + 标题栏 + 中心十字）
  if (state.showHit && state.matchHit) {
    const r = state.matchHit.rect;
    if (r && r.length === 4) {
      const [hx1, hy1] = normToCanvas(r[0], r[1]);
      const [hx2, hy2] = normToCanvas(r[2], r[3]);
      const color = state.matchHit.color || "#f9e2af";
      // 与 ROI 同色的模板匹配框
      let label = state.matchHit.key || "";
      if (state.matchHit.score != null) label += ` score=${state.matchHit.score.toFixed(3)}`;
      if (state.matchHit.scale) label += ` @${state.matchHit.scale}×`;
      drawMatchBox(hx1, hy1, hx2, hy2, color, label.trim());
      // 中心十字（命中锚点）
      const cx = (hx1 + hx2) / 2, cy = (hy1 + hy2) / 2;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(cx - 8, cy); ctx.lineTo(cx + 8, cy);
      ctx.moveTo(cx, cy - 8); ctx.lineTo(cx, cy + 8);
      ctx.stroke();
      ctx.restore();
    }
  }
  // 彩蛋识别测试结果（复用统一框样式：匹配框 vs OCR 框 + dy/dx 连接线），仅彩蛋分类显示
  if (state.currentCat === "eggs" && Array.isArray(state.eggHits)) {
    const eggColor = { red: "#f38ba8", yellow: "#f9e2af", blue: "#89dceb" };
    state.eggHits.forEach((e) => {
      const box = e.box, cr = e.count_rect;
      if (!box || box.length !== 4) return;
      const [bx1, by1] = normToCanvas(box[0], box[1]);
      const [bx2, by2] = normToCanvas(box[2], box[3]);
      const color = eggColor[e.color] || "#a6e3a1";
      // 【模板匹配框】蛋卡图标命中（center 采样色块在右侧「计数区定位参数」栏展示，避免遮挡画面）
      drawMatchBox(bx1, by1, bx2, by2, color, `🥚 ${e.color} s=${e.score}`);
      // 【OCR 计数区】图标下方 ×N
      if (cr && cr.length === 4) {
        const [cx1, cy1] = normToCanvas(cr[0], cr[1]);
        const [cx2, cy2] = normToCanvas(cr[2], cr[3]);
        const ocrText = `×${e.count} OCR${e.count_text ? "「" + e.count_text + "」" : ""}`;
        drawOcrBox(cx1, cy1, cx2, cy2, color, ocrText);
        // 【匹配框 → OCR 区 连接线】：标注 dy / dx 关系
        ctx.save();
        ctx.setLineDash([2, 3]);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        // 从匹配框底部中心 → OCR 区顶部中心
        const bCx = (bx1 + bx2) / 2, bBy = by2;
        const cCx = (cx1 + cx2) / 2, cTy = cy1;
        ctx.beginPath(); ctx.moveTo(bCx, bBy); ctx.lineTo(cCx, cTy); ctx.stroke();
        ctx.setLineDash([]);
        // 箭头
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(cCx, cTy); ctx.lineTo(cCx - 4, cTy - 6); ctx.lineTo(cCx + 4, cTy - 6); ctx.closePath(); ctx.fill();
        ctx.restore();
      }
    });
  }
}

// ---------------- 分类 Tab ----------------
function renderCatTabs() {
  const box = $("catTabs");
  box.innerHTML = "";
  catList().forEach((c) => {
    const b = document.createElement("button");
    b.className = "cat-tab" + (c.id === state.currentCat ? " active" : "");
    b.textContent = c.label;
    b.onclick = () => {
      state.currentCat = c.id;
      state.selected = roiKeys(c.id)[0] || null;
      state.matchHit = null; // 切分类旧命中失效
      updatePreviewMode();
      renderCatTabs(); renderRoiList(); updatePropPanel(); draw();
    };
    box.appendChild(b);
  });
}

// ---------------- ROI 列表 ----------------
function renderRoiList() {
  if (state.currentCat === "unassigned") { renderUnassigned(); return; }
  const box = $("roiList");
  box.innerHTML = "";
  const rois = currentRois();
  Object.keys(rois).filter((k) => !k.startsWith("_")).forEach((key, i) => {
    const r = rois[key];
    const color = COLORS[i % COLORS.length];
    const dotClass = dotStatus(r);
    const item = document.createElement("div");
    item.className = "roi-item" + (key === state.selected ? " active" : "");
    item.innerHTML = `<span class="roi-dot ${dotClass}" style="background:${color}"></span>
      <span class="roi-name">${htmlEscape(key)}</span>`;
    item.onclick = () => { state.selected = key; renderRoiList(); updatePropPanel(); draw(); };
    box.appendChild(item);
  });
  if (EDITABLE_CATS.has(state.currentCat)) {
    const add = document.createElement("button");
    add.className = "btn roi-add";
    add.textContent = "+ 新增 ROI";
    add.onclick = addRoi;
    box.appendChild(add);
  }
}

function renderUnassigned() {
  const box = $("roiList");
  box.innerHTML = "";
  const list = (state.templateStatus && state.templateStatus.unassigned) || [];
  if (!list.length) {
    box.innerHTML = '<div class="empty">无未分配模板</div>';
  } else {
    list.forEach((name) => {
      const item = document.createElement("div");
      item.className = "roi-item";
      item.innerHTML = `<span class="roi-dot dot-warn"></span><span class="roi-name">${htmlEscape(name)}</span>`;
      const btn = document.createElement("button");
      btn.textContent = "分配";
      btn.className = "btn";
      btn.onclick = (e) => { e.stopPropagation(); assignTemplate(name); };
      item.appendChild(btn);
      box.appendChild(item);
    });
  }
  const dangling = (state.templateStatus && state.templateStatus.dangling) || [];
  if (dangling.length) {
    const w = document.createElement("div");
    w.className = "empty";
    w.style.color = "var(--bad)";
    w.textContent = "⚠ JSON 引用了不存在的模板: " + dangling.join(", ");
    box.appendChild(w);
  }
}

function dotStatus(r) {
  if (!r || !Array.isArray(r.rect)) return "dot-none";
  const [x1, y1, x2, y2] = r.rect;
  if (x2 - x1 < 0.02 || y2 - y1 < 0.02) return "dot-warn";
  return "dot-ok"; // 详细状态在右侧面板
}

function markAdded(cat, name) {
  state.added[cat] ||= {};
  state.added[cat][name] = {
    kind: cat,           // v4：分类键即 kind（template/point/ocr）
    page: "",
    rect: state.rois[cat][name].rect,
    templates: [],
  };
}

function deleteRoi(cat, key, { ask = true } = {}) {
  const seg = state.rois[cat];
  if (!seg || !seg[key]) return;
  if (!EDITABLE_CATS.has(cat)) { flash("该分组不支持删除（图结构编辑归 MPE）"); return; }
  if (ask && !confirm(`删除 ROI「${key}」？`)) return;
  pushHistory("删除 ROI", `delete:${cat}`);
  if (state.added[cat] && state.added[cat][key]) {
    // 本次会话新增、尚未落盘的项：直接取消新增，而不是记入 deleted
    delete state.added[cat][key];
  } else {
    state.deleted.push(`${cat}.${key}`);
  }
  delete seg[key];
  state.selected = roiKeys(cat)[0] || null;
  markDirty();
  renderRoiList(); updatePropPanel(); draw();
}

function addRoi() {
  const cat = state.currentCat;
  if (!EDITABLE_CATS.has(cat)) { flash("该分组不支持新增（仅 spec 锚点可新增）"); return; }
  const name = prompt("输入新 ROI 名称（英文/数字/下划线/连字符）：");
  if (!name || !/^[\w-]+$/.test(name)) { if (name) alert("名称仅允许字母、数字、下划线和连字符"); return; }
  if (Object.values(state.rois).some(seg => seg?.[name])) { alert("该名称已存在"); return; }
  pushHistory("新增 ROI", `add:${cat}`);
  state.rois[cat][name] = { rect: [0.4, 0.4, 0.6, 0.6], templates: [] };
  markAdded(cat, name);
  state.selected = name;
  markDirty();
  renderRoiList(); updatePropPanel(); draw();
}

async function assignTemplate(name) {
  const catAns = prompt("分配到的分类（template / point / ocr）：");
  const cat = catAns && catAns.trim();
  if (!EDITABLE_CATS.has(cat)) { flash("无效分类（仅 template / point / ocr）"); return; }
  const key = prompt("新 ROI 名称（英文/下划线）：", name.replace(/\.png$/, ""));
  if (!key) return;
  if (state.rois[cat][key]) { alert("该 ROI 已存在"); return; }
  state.rois[cat][key] = { rect: [0, 0, 0, 0], templates: [name] };
  state.currentCat = cat;
  state.selected = key;
  markDirty();
  await loadTemplateStatus();
  renderCatTabs(); renderRoiList(); updatePropPanel(); draw();
}

// ---------------- 属性面板 ----------------
function updatePreviewMode() {
  const row = $("previewRow");
  if (!row) return;
  if (state.currentCat === "ocr") {
    row.classList.remove("preview-template");
    row.classList.add("preview-ocr-mode");
    // OCR 框默认 display:flex（preview-ocr-mode 下展开），但初始 hidden class 也要去掉
    const ocrBox = row.querySelector(".preview-ocr");
    if (ocrBox) ocrBox.classList.remove("hidden");
  } else {
    row.classList.add("preview-template");
    row.classList.remove("preview-ocr-mode");
  }
}

function updatePropPanel() {
  updatePreviewMode();
  const panel = $("propPanel");
  if (state.currentCat === "unassigned") {
    panel.innerHTML = '<div class="empty">在「未分配」列表分配模板到分类</div>';
    return;
  }
  const key = state.selected;
  const rois = currentRois();
  if (!key || !rois[key]) {
    panel.innerHTML = '<div class="empty">← 在左侧选择一个 ROI</div>';
    return;
  }
  const r = rois[key];
  const rect = r.rect || [0, 0, 0, 0];
  const [x1, y1] = normToCanvas(rect[0], rect[1]);
  const [x2, y2] = normToCanvas(rect[2], rect[3]);
  const selTplKey = tplKey(key);
  const isOcr = state.currentCat === "ocr"; // OCR 区域用 RapidOCR 识别，不关联模板图
  const isPoint = state.currentCat === "point"; // 点位锚点只用 rect 中心做准星目标，无模板图
  const isNode = state.currentCat === "nodes";  // 节点 rect：双真源可见性 + 镜像联动逃生口
  const isTuning = state.currentCat === "tuning"; // 调参 rect：spec 之外的第 54 个 rect
  const addedEntry = (state.added[state.currentCat] || {})[key]; // 本次新增项：需补结构字段
  const addOpts = state.rois._meta || {};
  const addedGroupHtml = addedEntry ? `
    <div class="prop-group">
      <h3>新增锚点结构</h3>
      <label class="prop-row"><span class="k">page *</span>
        <select id="addedPage" class="rect-input">
          <option value="">（选择页面）</option>
          ${(addOpts.pages || []).map(p => `<option value="${p}" ${p === addedEntry.page ? "selected" : ""}>${p}</option>`).join("")}
        </select>
      </label>
      <label class="prop-row"><span class="k">kind</span>
        <select id="addedKind" class="rect-input">
          ${["template", "point", "ocr"].map(k => `<option value="${k}" ${k === addedEntry.kind ? "selected" : ""}>${k}</option>`).join("")}
        </select>
      </label>
      <label class="prop-row" id="addedGuardRow" style="${addedEntry.kind === "point" ? "" : "display:none"}">
        <span class="k">guarded_by *</span>
        <select id="addedGuard" class="rect-input">
          <option value="">（选择模板锚点作担保）</option>
          ${(addOpts.stage_anchors || []).map(a => `<option value="${a}" ${a === addedEntry.guarded_by ? "selected" : ""}>${a}</option>`).join("")}
        </select>
      </label>
      <div class="prop-row" style="color:var(--dim);font-size:12px">page/kind 决定该锚点在 v4 的归属；point 必须挂模板担保人（E10）</div>
    </div>
  ` : "";

  // 只读元信息（透传展示：kind/page/label/order/templates/colorspace/guarded_by/mirrors/arbitration/_comment）
  const metaHtml = `
    <div class="prop-group">
      <h3>元信息（只读）</h3>
      <div class="prop-row"><span class="k">kind</span><span>${htmlEscape(r.kind ?? state.currentCat)}</span></div>
      ${r.page ? `<div class="prop-row"><span class="k">page</span><span>${htmlEscape(r.page)}</span></div>` : ""}
      ${r.label ? `<div class="prop-row"><span class="k">label</span><span>${htmlEscape(r.label)}</span></div>` : ""}
      ${r.order != null ? `<div class="prop-row"><span class="k">order</span><span>${r.order}</span></div>` : ""}
      ${Array.isArray(r.templates) && r.templates.length ? `<div class="prop-row" style="align-items:flex-start"><span class="k">templates</span><span style="color:var(--dim)">${htmlEscape(r.templates.join(", "))}</span></div>` : ""}
      <div class="prop-row"><span class="k">colorspace</span><span>${htmlEscape(r.colorspace ?? "（默认 rgb）")}</span></div>
      ${r.guarded_by ? `<div class="prop-row"><span class="k">guarded_by</span><span>${htmlEscape(r.guarded_by)}</span></div>` : ""}
      ${r.mirrors ? `<div class="prop-row"><span class="k">mirrors</span><span>${htmlEscape(r.mirrors)}</span></div>` : ""}
      ${r.arbitration ? `<div class="prop-row" style="align-items:flex-start"><span class="k">arbitration</span><span style="color:var(--dim)">${htmlEscape(JSON.stringify(r.arbitration))}</span></div>` : ""}
      ${r._comment ? `<div class="prop-row" style="align-items:flex-start"><span class="k">_comment</span><span style="color:var(--dim);white-space:pre-wrap">${htmlEscape(r._comment)}</span></div>` : ""}
      ${isNode && r.mirrors ? `
      <label class="prop-row"><span class="k">仅改此面</span>
        <input type="checkbox" id="onlyThisSide" ${state.onlyThisSide[selTplKey] ? "checked" : ""}>
      </label>
      <div class="prop-row" style="color:var(--dim);font-size:12px">默认联动：改此处 rect 会同步写 spec 同名锚点 ${htmlEscape(r.mirrors)}；勾选后只改 pipeline 面</div>
      ` : ""}
    </div>
  `;

  // 色彩空间编辑（编辑面三件套之一；空 = 回落默认 rgb）
  const csOptions = ["", "rgb", "gray", "rgb_strict"];
  const csHtml = (isOcr || isPoint || isNode) ? "" : `
    <label class="prop-row"><span class="k">colorspace</span>
      <select id="colorspaceSel" class="rect-input">
        ${csOptions.map(v => `<option value="${v}" ${(r.colorspace ?? "") === v ? "selected" : ""}>${v || "（默认 rgb）"}</option>`).join("")}
      </select>
    </label>
  `;

  panel.innerHTML = `
    <div class="prop-group">
      <h3>[${state.currentCat}] ${key}</h3>
      <div class="prop-row"><span class="k">Pixel</span><span id="pxText">${Math.round(x1)},${Math.round(y1)} → ${Math.round(x2)},${Math.round(y2)}</span></div>
      <div class="prop-row"><span class="k">Normalized</span></div>
      <input id="rectInput" class="rect-input" value="${rect.map(n => n.toFixed(3)).join(', ')}">
      <button id="delRoi" class="btn" style="margin-top:8px;width:100%;border-color:var(--bad);color:var(--bad)">删除 {{key}}</button>
    </div>
    ${addedGroupHtml}
    ${metaHtml}
    ${isOcr ? `
    <div class="prop-group">
      <h3>OCR 识别</h3>
      <div class="prop-row"><span class="k">引擎</span><span>RapidOCR</span></div>
      <div class="prop-row"><span class="k">说明</span><span>此区域识别文字，不关联模板图</span></div>
    </div>
    ` : (isPoint ? `
    <div class="prop-group">
      <h3>纯 rect 点位锚点</h3>
      <div class="prop-row"><span class="k">说明</span><span>点位锚点只用 rect 中心作为准星目标，不关联模板图；配置 rect 即可</span></div>
      ${r.guarded_by ? `<div class="prop-row"><span class="k">担保人</span><span>${htmlEscape(r.guarded_by)}</span></div>` : ""}
    </div>
    ` : (isTuning ? `
    <div class="prop-group">
      <h3>调参 rect（policy.tuning.perception）</h3>
      <div class="prop-row"><span class="k">说明</span><span>不在 spec 内的第 54 个 rect，仅此一处；拖框或改归一化值即可</span></div>
    </div>
    ` : `
    <div class="prop-group">
      <h3>关联模板（多选）</h3>
      <div id="tplList"></div>
      <button id="uploadTpl" class="btn" style="margin-top:8px;width:100%;border-color:var(--accent);color:var(--accent)">⬆ 上传/替换模板图</button>
      <input type="file" id="tplFile" accept="image/*" hidden>
      <div style="margin-top:8px;display:flex;gap:6px">
        <input id="cropTplName" class="rect-input" placeholder="目标模板名，如 settle_final_price_title.png" value="${(r.templates && r.templates[0]) || (key+'.png')}">
        <button id="cropAsTpl" class="btn" style="flex-shrink:0;border-color:var(--ok);color:var(--ok)">✂ 裁剪当前区域作模板</button>
      </div>
      <label class="prop-row" style="margin-top:8px"><span class="k">测试用模板</span>
        <select id="activeTpl"></select>
      </label>
    </div>
    <div class="prop-group">
      <h3>尺寸检查</h3>
      <div id="sizeInfo" class="prop-row"><span class="k">—</span></div>
    </div>
    <div class="prop-group">
      <h3>置信度阈值（当前 ROI 全部模板共用）</h3>
      ${csHtml}
      <div class="threshold-row">
        <input id="thresholdRange" type="range" min="0.40" max="0.99" step="0.01"
          value="${getRoiThreshold(r).toFixed(2)}">
        <input id="thresholdNum" type="number" min="0.40" max="0.99" step="0.01"
          value="${getRoiThreshold(r).toFixed(2)}" style="width:72px">
      </div>
      <div id="thresholdInfo" class="prop-row" style="justify-content:center">
        <span class="k">达标 ≥</span><span id="thresholdDisplay">${getRoiThreshold(r).toFixed(3)}</span>
        <span style="color:var(--dim);margin-left:8px">默认 0.75</span>
        <button id="resetThreshold" class="btn" style="margin-left:8px;padding:2px 8px;font-size:12px">重置</button>
      </div>
    </div>
    <div class="prop-group">
      <h3>匹配分数（TM_CCOEFF_NORMED）</h3>
      <div id="scoreBox" class="score-big score-dim">—</div>
      <div id="scoreNote" class="prop-row" style="justify-content:center">实时预览</div>
    </div>
    ${state.currentCat === "eggs" ? `
    <div class="prop-group">
      <h3>🥚 计数区定位参数</h3>
      <div class="prop-row">
        <span class="k" style="flex-direction:column;align-items:flex-start;line-height:1.3">
          <span>dx 水平偏移</span>
          <span id="eggDxPx" style="font-size:11px;color:var(--dim);font-weight:400"></span>
        </span>
        <input id="eggDx" class="rect-input" type="number" step="0.005" min="-0.5" max="0.5" value="${(state.rois.eggs && state.rois.eggs._count_dx_norm) ?? 0}">
      </div>
      <div class="prop-row">
        <span class="k" style="flex-direction:column;align-items:flex-start;line-height:1.3">
          <span>dy 向下偏移</span>
          <span id="eggDyPx" style="font-size:11px;color:var(--dim);font-weight:400"></span>
        </span>
        <input id="eggDy" class="rect-input" type="number" step="0.005" min="0" max="0.5" value="${(state.rois.eggs && state.rois.eggs._count_dy_norm) ?? 0.02}">
      </div>
      <div class="prop-row">
        <span class="k" style="flex-direction:column;align-items:flex-start;line-height:1.3">
          <span>w 计数区宽度</span>
          <span id="eggWPx" style="font-size:11px;color:var(--dim);font-weight:400"></span>
        </span>
        <input id="eggW" class="rect-input" type="number" step="0.005" min="0.02" max="0.5" value="${(state.rois.eggs && state.rois.eggs._count_w_norm) ?? 0.14}">
      </div>
      <div class="prop-row">
        <span class="k" style="flex-direction:column;align-items:flex-start;line-height:1.3">
          <span>h 计数区高度</span>
          <span id="eggHPx" style="font-size:11px;color:var(--dim);font-weight:400"></span>
        </span>
        <input id="eggH" class="rect-input" type="number" step="0.005" min="0.01" max="0.2" value="${(state.rois.eggs && state.rois.eggs._count_h_norm) ?? 0.05}">
      </div>
      <div class="prop-row" style="justify-content:center;color:var(--dim);font-size:12px">计数区 = 图标下缘 +dy，宽 w、高 h</div>
      <button id="eggsTestBtn" class="btn" style="margin-top:8px;width:100%;border-color:var(--ok);color:var(--ok)">🥚 彩蛋识别测试（当前帧）</button>
      <div id="eggsTestOut" class="prop-row" style="justify-content:center;margin-top:6px">—</div>
      <div id="eggCenterInfo" class="prop-row" style="flex-direction:column;align-items:stretch;gap:4px;margin-top:8px"></div>
    </div>
    ` : ""}
    `))}
  `;
  // 修正删除按钮文字
  panel.querySelector("#delRoi").textContent = `删除 ${key}`;
  panel.querySelector("#delRoi").onclick = () => deleteRoi(state.currentCat, key);

  if (addedEntry) {
    const pageSel = panel.querySelector("#addedPage");
    const kindSel = panel.querySelector("#addedKind");
    const guardSel = panel.querySelector("#addedGuard");
    const guardRow = panel.querySelector("#addedGuardRow");
    pageSel.onchange = () => {
      pushHistory("设置 page", `struct:${key}:page`);
      addedEntry.page = pageSel.value;
      markDirty();
    };
    kindSel.onchange = () => {
      pushHistory("设置 kind", `struct:${key}:kind`);
      addedEntry.kind = kindSel.value;
      guardRow.style.display = addedEntry.kind === "point" ? "" : "none";
      markDirty();
    };
    guardSel.onchange = () => {
      pushHistory("挂担保人", `struct:${key}:guard`);
      addedEntry.guarded_by = guardSel.value || undefined;
      markDirty();
    };
  }

  // 彩蛋计数区偏移：改 → 写回段级元数据并标记 dirty（保存时统一落盘）
  if (state.currentCat === "eggs" && state.rois.eggs) {
    // 刷新归一化 → 像素的提示文字（基于当前截图尺寸 state.imgW / state.imgH）
    const refreshPxLabels = () => {
      const W = state.imgW || 0, H = state.imgH || 0;
      const fmt = (norm, pxPer, signOk) => {
        const px = Math.round(norm * pxPer);
        const pxStr = (signOk && px > 0 ? `+${px}` : `${px}`) + " 像素";
        return W && H
          ? `≈ ${pxStr}（归一化 ${norm.toFixed(3)}）`
          : `（归一化 ${norm.toFixed(3)}，请先加载截图查看像素）`;
      };
      const dx = panel.querySelector("#eggDx"); const dxLbl = panel.querySelector("#eggDxPx");
      if (dx && dxLbl) dxLbl.textContent = fmt(parseFloat(dx.value) || 0, W, true);
      const dy = panel.querySelector("#eggDy"); const dyLbl = panel.querySelector("#eggDyPx");
      if (dy && dyLbl) dyLbl.textContent = fmt(parseFloat(dy.value) || 0, H, false);
      const w = panel.querySelector("#eggW"); const wLbl = panel.querySelector("#eggWPx");
      if (w && wLbl) wLbl.textContent = fmt(parseFloat(w.value) || 0, W, false);
      const h = panel.querySelector("#eggH"); const hLbl = panel.querySelector("#eggHPx");
      if (h && hLbl) hLbl.textContent = fmt(parseFloat(h.value) || 0, H, false);
    };
    const bindEggMeta = (id, metaKey, def) => {
      const el = panel.querySelector("#" + id);
      if (!el) return;
      const onChange = () => {
        let v = parseFloat(el.value);
        if (Number.isNaN(v)) v = def;
        state.rois.eggs[metaKey] = v;
        markDirty();
        refreshPxLabels();
      };
      el.onchange = onChange;
      el.oninput = () => { refreshPxLabels(); };
    };
    bindEggMeta("eggDx", "_count_dx_norm", 0);
    bindEggMeta("eggDy", "_count_dy_norm", 0.02);
    bindEggMeta("eggW", "_count_w_norm", 0.14);
    bindEggMeta("eggH", "_count_h_norm", 0.05);
    refreshPxLabels();
    const testBtn = panel.querySelector("#eggsTestBtn");
    if (testBtn) testBtn.onclick = runEggsTest;
    renderEggCenterInfo(); // 面板重建后恢复「中心采样颜色」区（若有历史识别结果）
  }

  // 模板多选（OCR 区域、纯 rect 点位锚点、调参 rect 不显示模板，跳过）
  if (!isOcr && !isPoint && !isTuning) {
  const tplList = panel.querySelector("#tplList");
  const activeTpl = panel.querySelector("#activeTpl");
  state.templates.forEach((t) => {
    const lab = document.createElement("label");
    lab.className = "prop-row";
    lab.innerHTML = `<span class="k" style="flex:1">${t}</span>`;
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = (r.templates || []).includes(t);
    cb.onchange = () => {
      pushHistory("模板勾选", `tpl:${state.currentCat}:${key}`);
      if (cb.checked) {
        if (!r.templates) r.templates = [];
        if (!r.templates.includes(t)) r.templates.push(t);
      } else {
        r.templates = (r.templates || []).filter(x => x !== t);
      }
      markDirty();
      updatePropPanel();
      scheduleMatch();
    };
    lab.appendChild(cb);
    tplList.appendChild(lab);
  });
  // 测试用模板下拉（默认第一个已选；新建 ROI 兜底用 key+'.png'，避免落到字母序第一个模板）
  const active = state.activeTpl[selTplKey] || (r.templates && r.templates[0]) || (key + ".png");
  activeTpl.innerHTML = state.templates.map(t =>
    `<option value="${t}" ${t === active ? "selected" : ""}>${t}</option>`).join("");
  const cropName = panel.querySelector("#cropTplName");
  activeTpl.onchange = (e) => {
    state.activeTpl[selTplKey] = e.target.value;
    cropName.value = e.target.value;  // 切换「测试用模板」时同步裁剪目标
    scheduleMatch();
  };
  state.activeTpl[selTplKey] = active;
  cropName.value = active;  // 初始默认值跟随 active

  // 上传/替换模板图
  const uploadBtn = panel.querySelector("#uploadTpl");
  const tplFile = panel.querySelector("#tplFile");
  uploadBtn.onclick = () => tplFile.click();
  tplFile.onchange = () => {
    const file = tplFile.files[0];
    tplFile.value = "";
    if (!file) return;
    // 支持 png/jpg/webp/bmp 等任意图片格式：后端统一解码并转存为 PNG
    const reader = new FileReader();
    reader.onload = async () => {
      const dataUrl = reader.result;
      // 存盘统一为 .png（模板引用均为 .png，扩展名归一化）
      const name = file.name.replace(/\.[^.]+$/, "") + ".png";
      const res = await apiPost("/api/template_upload", { name, dataUrl });
      if (res.ok) {
        flash(`✅ 已写入 ${name} (${res.size[0]}×${res.size[1]})`);
        state.templates = await apiGet("/api/list_templates");
        if (!r.templates) r.templates = [];
        if (!r.templates.includes(name)) r.templates.push(name);
        state.activeTpl[selTplKey] = name;
        markDirty();
        updatePropPanel();
        scheduleMatch();
      } else {
        flash("❌ " + (res.error || "上传失败"));
      }
    };
    reader.readAsDataURL(file);
  };

  // ✂ 裁剪当前区域作模板
  const cropBtn = panel.querySelector("#cropAsTpl");
  cropBtn.onclick = async () => {
    const target = cropName.value.trim();
    if (!target) { flash("请填目标模板名"); return; }
    if (!target.toLowerCase().endsWith(".png")) { flash("必须以 .png 结尾"); return; }
    if (!state.session || !state.image) { flash("请选择截图"); return; }
    cropBtn.disabled = true;
    try {
      const res = await apiPost("/api/crop_to_template", {
        session: state.session, image: state.image,
        rect: r.rect, target,
      });
      if (res.ok) {
        flash(`✅ 已写入 ${target} (${res.size[0]}×${res.size[1]})`);
        state.templates = await apiGet("/api/list_templates");
        if (!r.templates) r.templates = [];
        if (!r.templates.includes(target)) r.templates.push(target);
        state.activeTpl[selTplKey] = target;
        markDirty();
        updatePropPanel();
        scheduleMatch();
      } else {
        flash("❌ " + (res.error || "裁剪失败"));
      }
    } finally { cropBtn.disabled = false; }
  };
  }

  // ---- 置信度阈值（range + number 双向同步，非 OCR 才存在）----
  const thrRange = panel.querySelector("#thresholdRange");
  const thrNum = panel.querySelector("#thresholdNum");
  const thrDisplay = panel.querySelector("#thresholdDisplay");
  const thrReset = panel.querySelector("#resetThreshold");
  function applyThreshold(newVal, writeToRoi) {
    const v = Math.max(0.4, Math.min(0.99, Number(newVal)));
    if (Number.isNaN(v)) return;
    if (writeToRoi) {
      pushHistory("阈值调整", `threshold:${state.currentCat}:${key}`);
      // 默认值也必须以数值写回，避免重置后字段删除造成回弹
      r.threshold = Number(v.toFixed(3));
      markDirty();
    }
    if (thrRange) thrRange.value = v.toFixed(2);
    if (thrNum) thrNum.value = v.toFixed(2);
    if (thrDisplay) thrDisplay.textContent = v.toFixed(3);
  }
  if (thrRange) thrRange.oninput = (e) => applyThreshold(e.target.value, true);
  if (thrNum) thrNum.onchange = (e) => applyThreshold(e.target.value, true);
  if (thrReset) thrReset.onclick = () => applyThreshold(DEFAULT_MATCH_THRESHOLD, true);

  // ---- 色彩空间（编辑面三件套之一；空 = 回落默认 rgb）----
  const csSel = panel.querySelector("#colorspaceSel");
  if (csSel) csSel.onchange = (e) => {
    pushHistory("色彩空间", `cs:${state.currentCat}:${key}`);
    const v = e.target.value;
    if (v) r.colorspace = v; else delete r.colorspace;
    markDirty();
    scheduleMatch();
  };

  // ---- 节点组「仅改此面」逃生口（默认联动；勾选态只影响保存 body，不改数据面）----
  const ots = panel.querySelector("#onlyThisSide");
  if (ots) ots.onchange = (e) => {
    if (e.target.checked) state.onlyThisSide[selTplKey] = true;
    else delete state.onlyThisSide[selTplKey];
  };

  // 归一化输入
  const rectInput = panel.querySelector("#rectInput");
  rectInput.onchange = () => {
    const parts = rectInput.value.split(/[,\s]+/).map(Number);
    if (parts.length === 4 && parts.every(n => !isNaN(n))) {
      r.rect = parts.map(n => Math.min(1, Math.max(0, n)));
      markDirty();
      draw(); updatePropPanel();
      state.currentCat === "ocr" ? scheduleOcr() : scheduleMatch();
    } else {
      rectInput.value = rect.map(n => n.toFixed(3)).join(', ');
    }
  };

  if (isOcr) scheduleOcr();
  else if (!isPoint && !isNode && state.currentCat !== "tuning") scheduleMatch();
}

// ---------------- 匹配分数 + OCR 识别（debounce） ----------------
let matchTimer = null;
function scheduleMatch() {
  clearTimeout(matchTimer);
  matchTimer = setTimeout(runMatch, 150); // 150ms debounce
}

let ocrTimer = null;
function scheduleOcr() {
  clearTimeout(ocrTimer);
  ocrTimer = setTimeout(runOcr, 200);
}

async function runMatch() {
  const key = state.selected;
  const scoreBox = $("scoreBox"), scoreNote = $("scoreNote");
  const sizeInfo = $("sizeInfo");
  if (state.currentCat !== "template") return;   // 只有模板锚点组跑单帧测分
  const rois = currentRois();
  if (!key || !rois[key] || !state.session || !state.image) return;
  const r = rois[key];
  const threshold = getRoiThreshold(r);
  const warnLine = Math.max(0.4, threshold - 0.20);
  const tpl = state.activeTpl[tplKey(key)];
  // v4：该 ROI 的全部模板一起匹配（引擎取最高分），colorspace 一并传入
  const tpls = (Array.isArray(r.templates) && r.templates.length) ? r.templates.slice() : (tpl ? [tpl] : []);
  if (!tpls.length) {
    if (scoreBox) scoreBox.textContent = "无模板";
    return;
  }
  scoreBox.innerHTML = '<span class="spinner"></span>';
  const cat = state.currentCat;
  try {
    const res = await apiPost("/api/match_score", {
      session: state.session, name: state.image,
      rect: r.rect, templates: tpls, threshold: threshold,
      colorspace: r.colorspace || "rgb",
    });
    // 竞态守卫带分类维度：不同分类下存在同名 key（如 session_start_match_btn），
    // 只比 key 会把别处分类的分数画到当前面板
    if (key !== state.selected || cat !== state.currentCat) return;
    if (res.score === -1) {
      state.matchHit = null;
      scoreBox.textContent = "SIZE 不足";
      scoreBox.className = "score-big score-bad";
      if (sizeInfo) sizeInfo.innerHTML =
        `<span class="k">crop ${res.crop_size?.[0]}×${res.crop_size?.[1]} &lt; tpl ${res.tpl_size?.[0]}×${res.tpl_size?.[1]}</span>`;
      if (scoreNote) scoreNote.innerHTML = `阈值 ${threshold.toFixed(3)} · 当前帧不匹配（尺寸不足）`;
    } else if (res.score >= 0) {
      scoreBox.textContent = res.score.toFixed(3);
      let cls = "score-bad", status = "不达标";
      if (res.score >= threshold) { cls = "score-ok"; status = "✅ 达标"; }
      else if (res.score >= warnLine) { cls = "score-warn"; status = "⚠ 接近阈值"; }
      scoreBox.className = "score-big " + cls;
      if (sizeInfo && res.size_ok) sizeInfo.innerHTML =
        `<span class="k">crop ${res.crop_size?.[0]}×${res.crop_size?.[1]} · tpl ${res.tpl_size?.[0]}×${res.tpl_size?.[1]}${res.best_scale ? ` @${res.best_scale}×` : ""}</span>`;
      const hitTpl = res.hit_template ? ` · 命中 ${htmlEscape(res.hit_template)}` : "";
      if (scoreNote) scoreNote.innerHTML =
        `${status}${hitTpl} · 阈值 ≥ ${threshold.toFixed(3)}（差 ${(threshold - res.score).toFixed(3)}）`;
      // 命中位置（归一化）→ 画布高亮；只在 size_ok 时更新（颜色取 ROI 序号色，与矩形框一致）
      if (res.size_ok && Array.isArray(res.hit_norm) && res.hit_norm.length === 4) {
        const keys = Object.keys(rois).filter((k) => !k.startsWith("_"));
        const cIdx = keys.indexOf(key);
        const mColor = COLORS[((cIdx % COLORS.length) + COLORS.length) % COLORS.length];
        state.matchHit = { key, rect: res.hit_norm, score: res.score, scale: res.best_scale, color: mColor };
      } else {
        state.matchHit = null;
      }
      draw();
    }
    $("cropPreview").src = res.crop_preview || "";
    // OCR 分类下右侧面板不显示 tpl，这里保留仅写入（页面没显示元素，安全）
    const tp = $("tplPreview"); if (tp) tp.src = res.tpl_preview || "";
  } catch (e) {
    if (scoreBox) scoreBox.textContent = "错误";
    if (scoreNote) scoreNote.textContent = String(e?.message || e);
  }
}

async function runOcr() {
  const box = $("ocrResult");
  const cropImg = $("cropPreview");
  if (state.currentCat !== "ocr") {
    // 非 OCR 分类下清空右侧 OCR 区，等 match_score 把 cropPreview 填上
    if (box) box.innerHTML = `<div class="ocr-empty">（仅 OCR 分类显示）</div>`;
    return;
  }
  const rois = currentRois();
  const key = state.selected;
  if (!key || !rois[key] || !state.session || !state.image) {
    if (box) box.innerHTML = `<div class="ocr-empty">请选择 OCR ROI 且加载截图</div>`;
    return;
  }
  const r = rois[key];
  if (!r || !Array.isArray(r.rect)) return;
  if (box) box.innerHTML = `<div class="ocr-empty"><span class="spinner"></span> 识别中…</div>`;
  try {
    const res = await apiPost("/api/ocr_recognize", {
      session: state.session, image: state.image,
      key: key, rect: r.rect,
    });
    if (key !== state.selected || state.currentCat !== "ocr") return;
    cropImg.src = res.crop_preview || "";
    if (!box) return;
    if (res.error) {
      box.innerHTML = `<div class="ocr-meta"><span class="bad">❌ ${htmlEscape(res.error)}</span></div>`;
      return;
    }
    const cs = res.crop_size || [0, 0];
    const meta = [
      `<span>裁剪 ${cs[0]}×${cs[1]}</span>`,
      `<span class="ok">耗时 ${res.duration_ms}ms</span>`,
    ];
    const sizeWarnHtml = res.size_warning
      ? `<div class="ocr-size-warn">${htmlEscape(res.size_warning).replace(/\n/g, "<br>")}</div>`
      : "";
    const lines = Array.isArray(res.lines) ? res.lines : [];
    const amountHtml = (res.amount != null)
      ? `<div class="ocr-amount">￥${Number(res.amount).toLocaleString("en-US")}</div>`
      : "";
    // 多段金额（bid_history 这种 4~5 个历史出价并排时，每段单独解析的金额列表）
    const amounts = Array.isArray(res.amounts) ? res.amounts : [];
    const amountsHtml = amounts.length
      ? `<div class="ocr-amounts-title">分段金额（${amounts.length} 段）</div>` +
        amounts.map((v, i) =>
          `<div class="ocr-line"><span class="ocr-am-n">第${i + 1}段</span>￥${Number(v).toLocaleString("en-US")}</div>`
        ).join("")
      : "";
    let bodyHtml;
    if (lines.length) {
      bodyHtml = lines.map((l, i) => `<div class="ocr-line">${htmlEscape(String(i + 1).padStart(2, " "))}. ${htmlEscape(l)}</div>`).join("");
    } else if (res.text) {
      bodyHtml = `<div class="ocr-line">${htmlEscape(res.text)}</div>`;
    } else {
      bodyHtml = `<div class="ocr-line" style="color:var(--dim)">（无可识别行）</div>`;
    }
    const textHtml = (res.text && lines.length && res.text !== lines.join(""))
      ? `<div class="ocr-text-label">全文：</div><div class="ocr-line">${htmlEscape(res.text)}</div>`
      : "";
    box.innerHTML = `<div class="ocr-meta">${meta.join("")}</div>${sizeWarnHtml}${amountHtml}${amountsHtml}${bodyHtml}${textHtml}`;
  } catch (e) {
    if (box) box.innerHTML = `<div class="ocr-meta"><span class="bad">请求失败 ${htmlEscape(e.message || String(e))}</span></div>`;
  }
}

function htmlEscape(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------- 彩蛋识别测试 ----------------
async function runEggsTest() {
  const out = $("eggsTestOut");
  if (!state.session || !state.image) {
    if (out) out.innerHTML = `<span class="bad">请先加载截图</span>`;
    return;
  }
  if (out) out.innerHTML = `<span class="spinner"></span> 识别中…`;
  state.eggHits = null; draw();
  try {
    const res = await apiPost("/api/eggs_recognize", {
      session: state.session, name: state.image,
    });
    if (state.currentCat !== "eggs") return;
    if (res.error) {
      state.eggHits = null; draw();
      renderEggCenterInfo();
      if (out) out.innerHTML = `<span class="bad">${htmlEscape(res.error)}</span>`;
      return;
    }
    state.eggHits = Array.isArray(res.eggs) ? res.eggs : [];
    draw();
    renderEggCenterInfo();
    if (!out) return;
    const c = res.counts || {};
    const items = state.eggHits.length
      ? state.eggHits.map((e) => `${e.color}×${e.count}(${e.score})`).join(" · ")
      : `<span style="color:var(--dim)">未命中任何蛋卡</span>`;
    out.innerHTML = `<span class="ok">红${c.red} 黄${c.yellow} 蓝${c.blue}</span> · ${items}`;
  } catch (e) {
    if (out) out.innerHTML = `<span class="bad">请求失败 ${htmlEscape(e.message || String(e))}</span>`;
  }
}

// 右侧「计数区定位参数」栏的「中心采样颜色」区：展示每个命中蛋的 center_rgb 实际色块 + 判色结果
function renderEggCenterInfo() {
  const box = $("eggCenterInfo");
  if (!box) return;
  const hits = Array.isArray(state.eggHits) ? state.eggHits : [];
  if (!hits.length) {
    box.innerHTML = `<div style="color:var(--dim);font-size:12px;text-align:center">— 识别测试后显示各蛋中心采样色块 —</div>`;
    return;
  }
  const colorName = { red: "红", yellow: "黄", blue: "蓝" };
  box.innerHTML = hits.map((e) => {
    const crgb = e.center_rgb || [0, 0, 0];
    const sw = `rgb(${Math.round(crgb[0])},${Math.round(crgb[1])},${Math.round(crgb[2])})`;
    return `
      <div style="display:flex;align-items:center;gap:8px;font-size:12px">
        <span style="display:inline-block;width:16px;height:16px;background:${sw};border:1px solid #111;border-radius:3px;flex-shrink:0" title="中心采样实际颜色 RGB(${Math.round(crgb[0])},${Math.round(crgb[1])},${Math.round(crgb[2])})"></span>
        <span style="font-weight:600">${colorName[e.color] || e.color}</span>
        <span style="color:var(--dim)">s=${e.score} · RGB(${Math.round(crgb[0])},${Math.round(crgb[1])},${Math.round(crgb[2])})</span>
      </div>`;
  }).join("");
}

// ---------------- 跨帧测试 ----------------
$("testBtn").onclick = async () => {
  const key = state.selected;
  if (state.currentCat === "unassigned" || !key) { alert("请先选择一个 ROI"); return; }
  const rois = currentRois();
  const r = rois[key];
  if (!r) { alert("请先选择一个 ROI"); return; }
  const tpl = state.activeTpl[tplKey(key)];
  if (!tpl) { alert("请先为该 ROI 选择模板"); return; }
  const threshold = getRoiThreshold(r);
  const modal = $("testModal");
  const out = $("testResult");
  modal.classList.remove("hidden");
  out.innerHTML = `<div class="spinner"></div> 正在跨帧匹配 ${r.templates.length} 个模板 × ${state.images.length} 帧（阈值 ${threshold.toFixed(3)}）…`;
  const results = [];
  const tpls = r.templates && r.templates.length ? r.templates : [tpl];
  for (const t of tpls) {
    try {
      const res = await apiPost("/api/cross_frame_test", {
        session: state.session, rect: r.rect, templates: [t],
        threshold: threshold, colorspace: r.colorspace || "rgb",
      });
      results.push({ tpl: t, res });
    } catch (e) { results.push({ tpl: t, res: null }); }
  }
  out.textContent = formatTestResult(results, threshold);
};
$("closeTest").onclick = () => $("testModal").classList.add("hidden");

function formatTestResult(results, threshold) {
  const th = typeof threshold === "number" ? threshold : DEFAULT_MATCH_THRESHOLD;
  const lines = [
    `ROI: [${state.currentCat}] ${state.selected}  ·  会话: ${state.session}`,
    `置信度阈值: ${th.toFixed(3)}（≥ 此值视为命中）`,
    "=".repeat(56),
  ];
  for (const { tpl, res } of results) {
    if (!res) { lines.push(`\n[${tpl}] 测试失败`); continue; }
    lines.push(`\n▌ ${tpl}  (${res.total_frames} 帧)`);
    if (res.max === undefined) { lines.push("  全部帧尺寸不足"); continue; }
    const hit = typeof res.frames_ge_threshold === "number" ? res.frames_ge_threshold : 0;
    const hitRate = res.total_frames ? ((hit / res.total_frames) * 100).toFixed(1) : "0.0";
    lines.push(`  max:    ${res.max.toFixed(3)}`);
    lines.push(`  p95:    ${res.p95.toFixed(3)}`);
    lines.push(`  median: ${res.median.toFixed(3)}`);
    lines.push(`  ≥0.60:  ${res.frames_ge_060}  ≥0.70: ${res.frames_ge_070}  ≥0.80: ${res.frames_ge_080}`);
    lines.push(`  ≥阈值(${th.toFixed(2)}): ${hit} / ${res.total_frames} 帧  (${hitRate}%)`);
    // 直方图
    lines.push("  分布:");
    const maxC = Math.max(1, ...res.histogram.map(h => h[2]));
    for (const [lo, hi, c] of res.histogram) {
      const bar = "█".repeat(Math.round(c / maxC * 20));
      const mark = (th >= lo && th < hi) ? " ← 阈值线" : "";
      lines.push(`  ${lo.toFixed(1)}-${hi.toFixed(1)} ${bar.padEnd(20)} ${c}${mark}`);
    }
    if (res.best_frames && res.best_frames.length) {
      lines.push(`  Top-10 最佳帧:`);
      res.best_frames.forEach((b, i) =>
        lines.push(`    ${String(i + 1).padStart(2)}. ${b.name}  ${b.score.toFixed(3)}`));
    }
  }
  return lines.join("\n");
}

// ---------------- 保存 JSON（preview → diff 确认 → 落盘） ----------------
function collectSaveBody(preview) {
  const added = {};
  for (const [cat, entries] of Object.entries(state.added)) {
    for (const [key, item] of Object.entries(entries)) {
      const live = (state.rois[cat] || {})[key];
      if (!live) continue; // 已被撤回/删除的项不进 body
      added[cat] ||= {};
      const rect = live.rect ?? item.rect;
      const templates = Array.isArray(live.templates) ? live.templates : (item.templates || []);
      added[cat][key] = {
        kind: item.kind, page: item.page, label: item.label || key,
        rect, templates,
        ...(item.threshold ?? live.threshold) != null
          ? { threshold: item.threshold ?? live.threshold } : {},
        ...(item.guarded_by ? { guarded_by: item.guarded_by } : {}),
      };
    }
  }
  const deleted = state.deleted.filter(ref => {
    const [cat, key] = ref.split(".");
    return !state.added[cat] || !state.added[cat][key];
  });
  // nodes 组：注入「仅改此面」勾选态（缺省 false = 联动同步写 spec 同名锚点）
  const rois = { ...state.rois };
  if (state.rois && state.rois.nodes) {
    rois.nodes = {};
    for (const [k, v] of Object.entries(state.rois.nodes)) {
      rois.nodes[k] = { ...v, only_this_side: !!state.onlyThisSide[`nodes/${k}`] };
    }
  }
  return { ...rois, added, deleted, base_hash: state.baseHash, preview };
}

function pendingStructureProblems() {
  const problems = [];
  for (const [cat, entries] of Object.entries(state.added)) {
    for (const [key, item] of Object.entries(entries)) {
      if (!(state.rois[cat] || {})[key]) continue;
      if (!item.page) problems.push(`新增 ${cat}.${key}：未选择 page`);
      if (item.kind === "point" && !item.guarded_by) problems.push(`新增 ${cat}.${key}：point 锚点必须挂模板担保人`);
    }
  }
  return problems;
}

function closeSaveDialog() {
  const el = $("saveDialog");
  if (el) el.remove();
}

function openSaveDialog(p, onConfirm) {
  closeSaveDialog();
  const diff = p.diff || { added: [], removed: [], changed: [] };
  const errors = (p.report && p.report.errors) || [];
  const warnings = (p.report && p.report.warnings) || [];
  const esc = htmlEscape;
  const short = (v) => {
    const s = JSON.stringify(v);
    return s && s.length > 72 ? s.slice(0, 69) + "…" : String(s);
  };
  const rows = [
    ...diff.added.map(d => `<div class="sd-row sd-add">＋ ${esc(d.path)} = ${esc(short(d.new))}</div>`),
    ...diff.removed.map(d => `<div class="sd-row sd-del">－ ${esc(d.path)}（原 ${esc(short(d.old))}）</div>`),
    ...diff.changed.map(d => `<div class="sd-row sd-chg">✳ ${esc(d.path)}：${esc(short(d.old))} → ${esc(short(d.new))}</div>`),
  ];
  const overlay = document.createElement("div");
  overlay.id = "saveDialog";
  overlay.className = "modal";
  overlay.innerHTML = `
    <div class="modal-body">
      <div class="modal-head"><span>保存预览 · ＋${diff.added.length} －${diff.removed.length} ✳${diff.changed.length} · 编译 ${esc((p.compile && p.compile.status) || "?")}</span></div>
      <div class="sd-content">
        ${errors.length ? `<div class="sd-err">阻断校验未通过（保存被禁止）：<br>${errors.map(esc).join("<br>")}</div>` : ""}
        ${warnings.length ? `<div class="sd-warn">告警 ${warnings.length} 条：<br>${warnings.slice(0, 8).map(esc).join("<br>")}${warnings.length > 8 ? "<br>…" : ""}</div>` : ""}
        ${p.compile && p.compile.error ? `<div class="sd-err">编译失败：${esc(p.compile.error)}</div>` : ""}
        ${p.compile && p.compile.status === "skipped_no_routes" ? `<div class="sd-warn">该模块无 routes 段，保存不产生成物</div>` : ""}
        <div class="sd-list">${rows.length ? rows.join("") : '<div class="sd-row">（无字段变化）</div>'}</div>
      </div>
      <div class="sd-actions">
        <button id="sdCancel" class="btn">取消</button>
        <button id="sdOk" class="btn primary" ${p.ok ? "" : "disabled"}>确认保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  $("sdCancel").onclick = closeSaveDialog;
  overlay.onclick = (e) => { if (e.target === overlay) closeSaveDialog(); };
  const okBtn = $("sdOk");
  if (okBtn && p.ok) okBtn.onclick = async () => {
    okBtn.disabled = true;
    try { await onConfirm(p); closeSaveDialog(); }
    catch (e) { okBtn.disabled = false; flash("❌ 保存失败: " + e.message); }
  };
}

$("saveBtn").onclick = async () => {
  if (!state.dirty) { flash("无改动"); return; }
  const problems = pendingStructureProblems();
  if (problems.length) { flash("❌ " + problems.join("；")); return; }
  $("saveBtn").disabled = true;
  try {
    let p;
    try {
      p = await apiPost("/api/rois", collectSaveBody(true));
    } catch (e) {
      flash("❌ 预览失败: " + e.message);
      return;
    }
    await openSaveDialog(p, async () => {
      const res = await apiPost("/api/rois", Object.assign(collectSaveBody(false),
        { base_hash: p.base_hash || state.baseHash }));
      if (res.ok) {
        state.dirty = false; emitDirty();
        state.added = {}; state.deleted = [];
        state.history.marker();
        flash(`✅ 已保存 · 编译 ${res.compiled || "跳过"}`);
        await loadRois(); await loadTemplateStatus();
        renderRoiList(); updatePropPanel(); draw();
      } else {
        flash("❌ " + (res.error || ((res.report && res.report.errors) || []).join("；") || "失败"));
        throw new Error(res.error || "保存被拒绝");
      }
    });
  } catch (e) {
    flash("❌ 保存失败: " + e.message);
  } finally {
    $("saveBtn").disabled = false;
  }
};
function flash(msg) {
  const el = $("statusMsg");
  el.textContent = msg;
  setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 2000);
}

// ---------------- 事件绑定 ----------------
function bindEvents() {
  $("sessionSelect").onchange = async (e) => {
    state.session = e.target.value;
    await loadImages();
    await loadImage();
    updatePropPanel(); draw();
  };
  $("imageSelect").onchange = async (e) => {
    state.image = e.target.value;
    await loadImage();
    updatePropPanel(); draw();
  };
  // 显示设置：框显示模式 + 命中位置开关
  const roiShow = $("roiShowMode");
  if (roiShow) {
    roiShow.value = state.showRois;
    roiShow.onchange = (e) => { state.showRois = e.target.value; draw(); };
  }
  const showHit = $("showHit");
  if (showHit) {
    showHit.checked = state.showHit;
    showHit.onchange = (e) => { state.showHit = e.target.checked; draw(); };
  }
  window.addEventListener("resize", fitCanvas);
  // 监听 canvas-wrap 自身尺寸变化（flex 撑大 / 侧栏收缩都会触发）
  const wrap = document.querySelector(".canvas-wrap");
  if (wrap && "ResizeObserver" in window) {
    new ResizeObserver(() => fitCanvas()).observe(wrap);
  }
  bindCanvas();
}

// ---------------- Canvas 交互（创建 / 移动 / 右下缩放） ----------------
const drag = { mode: null, startX: 0, startY: 0, origRect: null, key: null };

function bindCanvas() {
  canvas.addEventListener("mousedown", onDown);
  canvas.addEventListener("mousemove", onMove);
  canvas.addEventListener("mouseup", onUp);
  canvas.addEventListener("mouseleave", onUp);
  window.addEventListener("keydown", (e) => {
    const tag = document.activeElement && document.activeElement.tagName;
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === "z" || e.key === "Z")) {
      if (tag === "INPUT" || tag === "TEXTAREA") return; // 文本框内保留原生撤销
      e.preventDefault();
      runHistory("undo");
      return;
    }
    if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) {
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      e.preventDefault();
      runHistory("redo");
      return;
    }
    if (e.key === "Delete" || e.key === "Backspace") {
      if (tag === "INPUT" || tag === "SELECT") return;
      if (state.currentCat === "unassigned") return;
      if (state.selected && currentRois()[state.selected]) {
        deleteRoi(state.currentCat, state.selected);
      }
    }
  });
  const undoBtn = $("undoBtn");
  const redoBtn = $("redoBtn");
  if (undoBtn) undoBtn.onclick = () => runHistory("undo");
  if (redoBtn) redoBtn.onclick = () => runHistory("redo");
}

function hitTest(cx, cy) {
  // 返回 {key, zone}  zone: 'resize' | 'move' | null
  // 「框显示模式」开关限制交互范围：
  //   • "none"     → 所有框不响应（纯浏览）
  //   • "selected" → 仅当前选中项响应（未显示的框不拦截点击）
  //   • "all"      → 所有框正常响应
  if (state.currentCat === "unassigned") return null;
  if (state.showRois === "none") return null;
  const rois = currentRois();
  const keys = Object.keys(rois).filter((k) => !k.startsWith("_"));
  for (let i = keys.length - 1; i >= 0; i--) {
    const key = keys[i];
    if (state.showRois === "selected" && key !== state.selected) continue;
    const r = rois[key];
    if (!r || !Array.isArray(r.rect)) continue;
    const [x1, y1] = normToCanvas(r.rect[0], r.rect[1]);
    const [x2, y2] = normToCanvas(r.rect[2], r.rect[3]);
    // 右下角手柄（选中态）
    if (key === state.selected && cx >= x2 - 12 && cx <= x2 + 4 && cy >= y2 - 12 && cy <= y2 + 4) {
      return { key, zone: "resize" };
    }
    const pad = 4;
    if (cx >= x1 - pad && cx <= x2 + pad && cy >= y1 - pad && cy <= y2 + pad) {
      return { key, zone: "move" };
    }
  }
  return null;
}

function onDown(e) {
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
  const hit = hitTest(cx, cy);
  if (hit) {
    pushHistory("拖拽调整 ROI", `drag:${state.currentCat}:${hit.key}`);
    state.selected = hit.key;
    renderRoiList(); updatePropPanel();
    drag.mode = hit.zone;
    drag.key = hit.key;
    drag.origRect = [...currentRois()[hit.key].rect];
    drag.startX = cx; drag.startY = cy;
  } else {
    // 空白处：开始创建新矩形（仅可新增的 spec 锚点分组）
    if (!EDITABLE_CATS.has(state.currentCat)) return;
    const [nx, ny] = canvasToNorm(cx, cy);
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;
    drag.mode = "create";
    drag.key = null;
    drag.startX = cx; drag.startY = cy;
    drag.origRect = [0, 0, 0, 0];
  }
  draw();
}

function onMove(e) {
  if (!drag.mode) return;
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
  const [nx, ny] = canvasToNorm(cx, cy);
  const c = (v) => Math.min(1, Math.max(0, v));

  if (drag.mode === "create") {
    const [sx, sy] = canvasToNorm(drag.startX, drag.startY);
    // 临时绘制
    draw();
    const [px1, py1] = normToCanvas(c(sx), c(sy));
    const [px2, py2] = normToCanvas(c(nx), c(ny));
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(Math.min(px1, px2), Math.min(py1, py2), Math.abs(px2 - px1), Math.abs(py2 - py1));
    return;
  }
  if (drag.mode === "move") {
    const [sx, sy] = canvasToNorm(drag.startX, drag.startY);
    const dx = nx - sx, dy = ny - sy;
    const r = currentRois()[drag.key].rect;
    r[0] = c(drag.origRect[0] + dx);
    r[1] = c(drag.origRect[1] + dy);
    r[2] = c(drag.origRect[2] + dx);
    r[3] = c(drag.origRect[3] + dy);
    markDirty();
    draw(); updatePropPanel();
  } else if (drag.mode === "resize") {
    const r = currentRois()[drag.key].rect;
    r[2] = c(nx);
    r[3] = c(ny);
    markDirty();
    draw(); updatePropPanel();
  }
}

function onUp(e) {
  if (drag.mode === "create") {
    const rect = canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
    const [sx, sy] = canvasToNorm(drag.startX, drag.startY);
    const [nx, ny] = canvasToNorm(cx, cy);
    const c = (v) => Math.min(1, Math.max(0, v));
    const lx = Math.min(c(sx), c(nx)), rx = Math.max(c(sx), c(nx));
    const ty = Math.min(c(sy), c(ny)), by = Math.max(c(sy), c(ny));
    if (rx - lx > 0.01 && by - ty > 0.01 && EDITABLE_CATS.has(state.currentCat)) {
      const name = prompt("输入新 ROI 名称：");
      const rois = currentRois();
      if (name && /^[\w-]+$/.test(name) && !rois[name]) {
        pushHistory("画框新增 ROI", `add:${state.currentCat}`);
        rois[name] = { rect: [lx, ty, rx, by], templates: [] };
        markAdded(state.currentCat, name);
        state.added[state.currentCat][name].rect = rois[name].rect;
        state.selected = name;
        markDirty();
        renderRoiList(); updatePropPanel();
      }
    }
  }
  drag.mode = null;
  draw();
}

init().catch((e) => console.error(e));