// PEEP 帧消费的唯一实现：GUI 数据页预览卡与 PEEP 悬浮窗共用这一份。
//
// 互斥约定：同一时刻只有一个实例在真正拉帧（由 C# 广播的悬浮态 + 页面可见性共同决定），
// 因此后端每帧只被编码一次、通道上只有一路 base64，不存在双消费者。
// 消费门控由调用方通过 isActive() 提供，本模块不假设任何页面结构。
(function () {
  'use strict';

  const ACTIVE_INTERVAL_MS = 100; // 有人看：~10fps
  const IDLE_INTERVAL_MS = 400;   // 无人看：降频空转（保持原有节拍）

  /**
   * 创建帧消费者。
   * @param {object} mra 通信层，需提供 call(method, params)
   * @param {object} opts
   *   opts.isActive() → 当前是否应当拉帧（唯一门控）
   *   opts.img()      → 画面 <img> 元素（未挂载时返回 null 即可）
   *   opts.empty()    → 占位层元素（同上）
   *   opts.onFrame    → 可选：(base64|null) => void
   */
  function create(mra, opts) {
    let running = false;
    let timer = null;

    function render(b64) {
      const img = opts.img ? opts.img() : null;
      const empty = opts.empty ? opts.empty() : null;
      if (img) {
        if (b64) {
          img.src = 'data:image/jpeg;base64,' + b64;
          img.style.display = 'block';
        } else {
          img.src = '';
          img.style.display = 'none';
        }
      }
      // 无帧时显示占位层（保持原行为：不清理残帧语义由后端缓冲决定）
      if (empty) empty.style.display = b64 ? 'none' : 'flex';
      if (opts.onFrame) opts.onFrame(b64 || null);
    }

    async function tick() {
      const active = !!opts.isActive();
      if (active) {
        try {
          const d = await mra.call('get_peep_frame');
          render(d && d.frame);
        } catch (e) { /* 预览轮询失败静默：与既有行为一致，不打扰用户 */ }
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

  /** 切换 PEEP 开关。真源是后端 profile 的 peep_enabled，前端只负责发起（失败由调用方回滚视觉）。 */
  function setPeep(mra, on) {
    return mra.call('set_peep', { enabled: !!on });
  }

  window.PeepConsumer = {
    create: create,
    setPeep: setPeep,
    ACTIVE_INTERVAL_MS: ACTIVE_INTERVAL_MS,
    IDLE_INTERVAL_MS: IDLE_INTERVAL_MS
  };
})();
