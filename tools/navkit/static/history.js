// NavKit Studio 会话内编辑历史：无依赖 ESM，供校准台与策略页复用。
// 模型：每个条目存「该次变更前的快照(before)」；cursor 指向最后一条已应用的变更。
// undo(current) 把当前态记入该条目的 after（供 redo 精确前进），并返回 before；
// redo(current) 目标态优先取已记录的 after，其次取下一条目的 before（不变量），
// 最后回退调用方传入的 current——三个来源覆盖全部到达路径，杜绝差一拍。
const MAX_STEPS = 60;
const MERGE_MS = 800;

export function createHistory({ clone = structuredClone, equal = (a, b) => JSON.stringify(a) === JSON.stringify(b) } = {}) {
  const entries = [];
  let cursor = -1;
  let lastPush = 0;

  function trim() {
    if (entries.length > MAX_STEPS) {
      entries.shift();
      cursor -= 1;
    }
  }

  function nextNonMarker(from, step) {
    let i = from;
    while (i >= 0 && i < entries.length && entries[i].marker) i += step;
    return i;
  }

  function push(label, snapshot, key = '') {
    entries.splice(cursor + 1);
    const now = Date.now();
    const prev = entries[cursor];
    if (key && prev && !prev.marker && prev.key === key && now - lastPush <= MERGE_MS) {
      // 合并：保留首快照（撤回的起点），只更新标签与时间窗
      prev.label = label;
      prev.after = undefined;
      lastPush = now;
      return;
    }
    entries.push({ label, snapshot: clone(snapshot), key, marker: false });
    cursor = entries.length - 1;
    trim();
    lastPush = now;
  }

  function marker(label = '— 已保存 —') {
    entries.splice(cursor + 1);
    entries.push({ label, snapshot: null, key: '', marker: true });
    cursor = entries.length - 1;
    trim();
    lastPush = 0;
  }

  function undo(current) {
    const i = nextNonMarker(cursor, -1);
    if (i < 0) return null;
    entries[i].after = clone(current);
    cursor = i - 1;
    return clone(entries[i].snapshot);
  }

  function redo(current) {
    const j = nextNonMarker(cursor + 1, 1);
    if (j < 0 || j >= entries.length) return null;
    let target = entries[j].after;
    if (target === undefined) {
      const k = nextNonMarker(j + 1, 1);
      target = (k >= 0 && k < entries.length) ? entries[k].snapshot : current;
    }
    cursor = j;
    return clone(target);
  }

  function clear() { entries.length = 0; cursor = -1; lastPush = 0; }
  function count() { return entries.filter(e => !e.marker).length; }
  function labels() { return entries.map(e => e.label); }
  return { push, marker, undo, redo, clear, count, labels, equal };
}

export default createHistory;
