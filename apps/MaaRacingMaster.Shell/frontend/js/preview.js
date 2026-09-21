// MaaRM shell 前端 —— PEEP 实时预览（三态：内嵌 / 全屏 / 悬浮窗）。
// 帧消费实现来自 peep-consumer.js（与悬浮窗共用同一份代码，不重复实现）。
// 消费者互斥：悬浮窗打开期间本页不拉帧，卡片退化为占位符（见 setPreviewFloating）。
// 跨文件调用经 window.MRA（见文件末尾导出）。
(function () {
  'use strict';

  // 共享物导入（js/rpc.js 已先行加载）
  const { mra, $, state } = window.MRA;

  let previewMode = 'normal'; // 'normal' | 'fullscreen' | 'floating'

  const peepConsumer = PeepConsumer.create(mra, {
    isActive: () => {
      if (previewMode === 'floating') return false; // 消费权已交给悬浮窗
      const page = $('page-data');
      if (!state.peepEnabled || !page || page.classList.contains('hidden')) return false;
      // 当前模块得有预览卡才拉帧：没有卡（如 speedrush）时拉了也没处显示，
      // 白白让后端每帧编码一次 base64
      return !!(MRA.currentModuleId && $(MRA.currentModuleId + '-preview-img'));
    },
    img: () => $(MRA.currentModuleId + '-preview-img'),
    empty: () => $(MRA.currentModuleId + '-preview-empty')
  });

  /** 悬浮窗开关（C# 广播）：卡片与占位符互换，并把帧消费权交出去 / 收回来。 */
  function setPreviewFloating(floating) {
    if (floating === (previewMode === 'floating')) return;
    previewMode = floating ? 'floating' : 'normal';
    const card = $(MRA.currentModuleId + '-preview-card');
    if (card) card.classList.toggle('preview-card--floating', floating);
    // 收回消费权：PEEP 开关可能已在悬浮窗里改过，回填一次
    // （refreshDebugState 在 js/settings.js 定义、晚于本文件加载，故经 MRA 延迟取用）
    if (!floating) MRA.refreshDebugState();
  }

  // ---------- 实时预览：播放/暂停图标状态 ----------
  // on=true 显示「暂停」图标（表示正在预览）；off 显示「播放」图标（预览已停）。
  // title 即悬停提示（暂停预览 / 开始预览），与悬浮窗上的同名按钮语义一致。
  function setPreviewPlayState(on) {
    const btn = $(MRA.currentModuleId + '-btn-preview-toggle');
    if (!btn) return;
    const play = btn.querySelector('.icon-play');
    const pause = btn.querySelector('.icon-pause');
    if (play) play.style.display = on ? 'none' : 'block';
    if (pause) pause.style.display = on ? 'block' : 'none';
    btn.title = on ? '暂停预览' : '开始预览';
  }

  // ---------- 实时预览：放大（撑满数据页整块区域、深色预览区四周留白、居中）
  //  不复用 position:fixed——数据页带 .page-slide-in 的 will-change 会命中 containing-block 陷阱，
  //  改为切换 .preview-fs-active 让预览卡在文档流里撑满整页。 ----------
  let _previewFsCard = null; // 当前放大的预览卡
  const PREVIEW_FS_PAD = 32; // 深色预览区四周留白

  // 按 16:9 + 留白计算深色预览区尺寸并写入内联样式（保证等比、不变形）
  function fitPreviewCanvas() {
    const card = _previewFsCard;
    if (!card) return;
    const wrap = card.querySelector('.preview-wrap');
    const canvas = card.querySelector('.preview-canvas');
    if (!wrap || !canvas) return;
    const availW = wrap.clientWidth - PREVIEW_FS_PAD * 2;
    const availH = wrap.clientHeight - PREVIEW_FS_PAD * 2;
    if (availW <= 0 || availH <= 0) return;
    const ratio = 16 / 9;
    let w = availW;
    let h = w / ratio;
    if (h > availH) { h = availH; w = h * ratio; } // 高不够：改由高决定，仍保持 16:9
    canvas.style.width = Math.floor(w) + 'px';
    canvas.style.height = Math.floor(h) + 'px';
  }

  // 放大/还原时切换数据页放大态（隐藏左栏 + 今日看板，预览卡独占整页）
  function setPreviewFsActive(card, active) {
    const pageEl = card.closest('.page');
    if (pageEl) pageEl.classList.toggle('preview-fs-active', active);
  }

  // 全屏/还原共用一个图标按钮：morph-icon 在 scan ↔ shrink 间弹簧变形，title 同步切换
  function setPreviewMaxState(fs) {
    const btn = $(MRA.currentModuleId + '-btn-preview-max');
    if (!btn) return;
    const morphEl = btn.querySelector('morph-icon');
    if (morphEl) morphEl.icon = MRAIcons.node(fs ? 'shrink' : 'scan');
    btn.title = fs ? '还原' : '全屏';
  }

  function enterPreviewFullscreen(card) {
    card.classList.add('preview-card--fullscreen');
    setPreviewFsActive(card, true);
    setPreviewMaxState(true); // 图标变形为「还原」
    _previewFsCard = card;
    fitPreviewCanvas();
    window.addEventListener('resize', onPreviewFsResize);
  }

  function onPreviewFsResize() {
    fitPreviewCanvas(); // 窗口变化时重算深色预览区尺寸，保持 16:9 与留白
  }

  function exitPreviewFullscreen(card) {
    card.classList.remove('preview-card--fullscreen');
    setPreviewFsActive(card, false);
    setPreviewMaxState(false); // 图标变形回「放大」
    const canvas = card.querySelector('.preview-canvas');
    if (canvas) { canvas.style.width = ''; canvas.style.height = ''; } // 清除内联尺寸还原为 100%
    window.removeEventListener('resize', onPreviewFsResize);
    _previewFsCard = null;
  }

  // ---------- 导出到 window.MRA ----------
  Object.assign(window.MRA, {
    peepConsumer,
    setPreviewFloating,
    setPreviewPlayState,
    enterPreviewFullscreen,
    exitPreviewFullscreen,
  });
})();
