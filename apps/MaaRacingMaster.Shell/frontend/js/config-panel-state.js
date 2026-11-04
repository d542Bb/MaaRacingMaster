/* 配置面板的异步时序纯逻辑（不碰 DOM，可用 node --test 直接覆盖）。
 *
 * 三条规则（对应评审里最容易出竞态的地方）：
 * 1. 响应携带后端的配置修订号 _rev；比已应用版本旧的一律丢弃。
 * 2. 同卡保存串行；保存中再改只合并成一个 pending patch，当前请求结束后再发。
 * 3. 卡片重建/移除时递增代际，旧卡的在途回调据此作废，不写已卸载节点。
 */
(function () {
  'use strict';

  function createEntry(mid, gen) {
    return {
      mid: mid,
      gen: gen,
      lastRev: -1,
      saving: false,
      pending: null,
      dead: false,
      cfg: null,
      nodes: null,
      sig: '',
    };
  }

  function isAlive(entry, gen) {
    return !!entry && !entry.dead && entry.gen === gen;
  }

  function kill(entry) {
    if (entry) entry.dead = true;
  }

  // 响应是否足够新：旧的丢弃；新的推进 lastRev；缺 _rev 的按当前版本处理（兼容内置步骤）。
  function acceptResponse(entry, resp) {
    if (!entry || !resp) return false;
    var rev = Number.isFinite(resp._rev) ? resp._rev : entry.lastRev;
    if (rev < entry.lastRev) return false;
    if (rev > entry.lastRev) entry.lastRev = rev;
    return true;
  }

  function mergePatch(current, patch) {
    return Object.assign({}, current || {}, patch || {});
  }

  function beginSave(entry) {
    if (!entry || entry.saving) return false;
    entry.saving = true;
    return true;
  }

  // 结束本次保存；返回合并后的待发 patch（没有则为 null）。
  function finishSave(entry) {
    if (!entry) return null;
    entry.saving = false;
    var pending = entry.pending;
    entry.pending = null;
    return pending;
  }

  var api = {
    createEntry: createEntry,
    isAlive: isAlive,
    kill: kill,
    acceptResponse: acceptResponse,
    mergePatch: mergePatch,
    beginSave: beginSave,
    finishSave: finishSave,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.MRAConfigPanelState = api;
})();
