// PEEP 帧消费的唯一实现：控制页常驻预览、数据页预览卡与悬浮窗共用这一份。
//
// 互斥约定：同一时刻只有一个实例真正拉帧（页面可见性 + 悬浮态共同决定），
// 因此后端每帧只被编码一次、通道上只有一路 base64。
// 新协议：请求携带 after_seq；state=same 表示序号未变，不重解码、不清画面。
(function () {
  'use strict';

  const ACTIVE_INTERVAL_MS = 100; // 有人看：~10fps
  const IDLE_INTERVAL_MS = 400;   // 无人看：降频空转

  const EMPTY_TEXT = {
    disabled: 'PEEP 已关闭',
    idle: '等待运行',
    gap: '当前段无画面',
    stale: '画面停滞'
  };

  /**
   * 创建帧消费者。
   * @param {object} maarm 通信层，需提供 call(method, params)
   * @param {object} opts
   *   opts.isActive() → 当前是否应当拉帧（唯一门控）
   *   opts.img()      → 画面 <img> 元素（未挂载时返回 null 即可）
   *   opts.empty()    → 占位层元素（同上）
   *   opts.meta       → 可选：(meta) => void，消费元数据（归属/阶段/帧龄）
   *   opts.onFrame    → 可选：(base64|null, meta) => void
   */
  function create(maarm, opts) {
    let running = false;
    let timer = null;
    let lastSeq = null;

    function el(name) {
      const get = opts[name];
      return get ? get() : null;
    }

    function setEmpty(meta) {
      const img = el('img');
      const empty = el('empty');
      if (img) {
        img.src = '';
        img.style.display = 'none';
      }
      if (empty) {
        empty.style.display = 'flex';
        const txt = empty.querySelector('[data-peep-empty-text]')
          || empty.querySelector('span');
        if (txt) txt.textContent = EMPTY_TEXT[meta && meta.state] || 'PEEP 开启后显示实时画面';
      }
    }

    function render(meta) {
      if (!meta) return;
      const img = el('img');
      const empty = el('empty');
      if (meta.seq != null) lastSeq = meta.seq;
      if (meta.frame) {
        if (img) {
          img.src = 'data:image/jpeg;base64,' + meta.frame;
          img.style.display = 'block';
        }
        if (empty) empty.style.display = 'none';
      } else if (meta.state !== 'same') {
        setEmpty(meta);
      }
      if (opts.onFrame) opts.onFrame(meta.frame || null, meta);
      if (opts.meta) opts.meta(meta);
    }

    async function tick() {
      const active = !!opts.isActive();
      if (active) {
        try {
          const params = lastSeq == null ? {} : { after_seq: lastSeq };
          const d = await maarm.call('get_peep_frame', params);
          render(d);
        } catch (e) { /* 预览轮询失败静默：不打扰用户 */ }
      }
      timer = setTimeout(tick, opts.isActive() ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS);
    }

    /** 启动自调度轮询（幂等，只启一次，之后由 tick 自递归）。 */
    function start(delayMs) {
      if (running) return;
      running = true;
      timer = setTimeout(tick, typeof delayMs === 'number' ? delayMs : 0);
    }

    /** 停止轮询（画面保持最后一帧不动）。 */
    function stop() {
      running = false;
      if (timer) { clearTimeout(timer); timer = null; }
    }

    return { start, stop, render };
  }

  /** 切换 PEEP 开关。真源是后端，前端只负责发起（失败由调用方回滚视觉）。 */
  function setPeep(maarm, on) {
    return maarm.call('set_peep', { enabled: !!on });
  }

  window.PeepConsumer = {
    create: create,
    setPeep: setPeep,
    ACTIVE_INTERVAL_MS: ACTIVE_INTERVAL_MS,
    IDLE_INTERVAL_MS: IDLE_INTERVAL_MS
  };
})();
