import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

globalThis.window = {};
const require = createRequire(import.meta.url);
const ST = require('../../apps/MaaRacingMaster.Shell/frontend/js/config-panel-state.js');

test('旧轮询响应晚到：按 _rev 丢弃', () => {
  const entry = ST.createEntry('mid', 1);
  assert.equal(ST.acceptResponse(entry, { _rev: 3 }), true);
  assert.equal(entry.lastRev, 3);
  assert.equal(ST.acceptResponse(entry, { _rev: 2 }), false);
  assert.equal(entry.lastRev, 3);
});

test('缺 _rev 的响应按当前版本处理', () => {
  const entry = ST.createEntry('mid', 1);
  assert.equal(ST.acceptResponse(entry, {}), true);
  assert.equal(entry.lastRev, -1);
});

test('卡片重建：旧代际回调作废', () => {
  const entry = ST.createEntry('mid', 1);
  assert.equal(ST.isAlive(entry, 1), true);
  ST.kill(entry);
  assert.equal(ST.isAlive(entry, 1), false);
  const fresh = ST.createEntry('mid', 2);
  assert.equal(ST.isAlive(entry, 2), false);
  assert.equal(ST.isAlive(fresh, 2), true);
});

test('保存串行：保存中再改只合并成 pending', () => {
  const entry = ST.createEntry('mid', 1);
  assert.equal(ST.beginSave(entry), true);
  assert.equal(ST.beginSave(entry), false);
  entry.pending = ST.mergePatch(entry.pending, { a: 1 });
  entry.pending = ST.mergePatch(entry.pending, { b: 2 });
  assert.deepEqual(entry.pending, { a: 1, b: 2 });
  const pending = ST.finishSave(entry);
  assert.deepEqual(pending, { a: 1, b: 2 });
  assert.equal(entry.saving, false);
  assert.equal(entry.pending, null);
  assert.equal(ST.beginSave(entry), true);
});

test('跨字段副作用：后一次写入覆盖前一次同键值', () => {
  const entry = ST.createEntry('mid', 1);
  entry.pending = ST.mergePatch(entry.pending, { record_mode: true });
  entry.pending = ST.mergePatch(entry.pending, { record_mode: false });
  assert.equal(entry.pending.record_mode, false);
});
