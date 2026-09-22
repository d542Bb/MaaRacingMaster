// MaaRM shell 前端 —— 启动装配入口（在 index.html 中最后加载）。
// 职责：tab 切换、窗口控制、模块下拉、关于页、init 装配与全局事件绑定入口。
// 通信层与共享 state 在 js/rpc.js；弹窗在 js/modal.js；日志在 js/log.js；预览在
// js/preview.js；模块选项/优化中心/模块页面在 js/settings.js；运行控制在 js/run.js。
// 共享物一律经 window.MRA 传递（下方导入清单）。
//
// 关于页「检查更新 / 公告」与 ViGEmBus 引导弹窗保留在本文件：
// tests/test_frontend_remote_render.py 静态锁按函数签名从本文件抽取
// checkUpdate / fetchAnnouncement 的函数体（且抽取锚点要求签名串的首个出现位置
// 即函数定义，故本注释不得写出完整签名文本），并断言本文件存在
// open_external_url / open_vigembus_download / openTarget 调用点——迁出会让 CI 即红
//（该锁的看护范围调整需另行修改 Python 测试，不在本次拆分范围）。
(function () {
  'use strict';

  // 共享物导入（js/ 各模块已先行加载）
  const {
    mra, $, state, showError, reportError, mkEl, appendIcon, postWindowAction,
    openModal, setPreviewFloating, peepConsumer,
    bindModuleOptionsUI, refreshModuleOptions, updateModuleOptionsDisabled,
    renderModulePages, checkRegistryOptimizations, pollTodayBoard,
    renderStageList, pollStatus, setStatus, pollLogs,
  } = window.MRA;

  // ---------- ViGEmBus 驱动缺失引导弹框 ----------
  // 外链一律只报**逻辑目标名**（home / issue / docs / announcement / download / vigembus），
  // 地址由 sidecar 侧白名单映射（core/remote_meta.py EXTERNAL_TARGETS）——前端与远程数据
  // 都不持有可打开的 URL，XSS 也就无法把「打开浏览器」的能力用在别处。
  // VIGEM_DL_URL 是本仓库静态常量（非远程数据），只用于弹框里的 <a> 与后端失败兜底。
  const VIGEM_DL_URL = 'https://github.com/nefarius/ViGEmBus/releases/latest';
  function showVigemDialog(detailMsg) {
    openModal({
      title: '缺少 ViGEmBus 驱动',
      titleColor: 'var(--mra-danger,#ef4444)',
      bodyHtml:
        '<p class="mra-modal-text">' +
        '需要虚拟手柄（vgamepad）控制的模块，底层依赖 <b>ViGEmBus</b> 内核驱动。它无法随解压包分发，需在本机安装一次。</p>',
      buttons: [
        { text: '手动打开下载页', asLink: true, href: VIGEM_DL_URL },
        {
          text: '下载并安装 ViGEmBus 驱动', primary: true,
          onClick: async (modal) => {
            try {
              await mra.call('open_vigembus_download', {});
            } catch (err) {
              // 后端打开失败：前端兜底新开标签页
              window.open(VIGEM_DL_URL, '_blank');
            }
            showError('已打开 ViGEmBus 下载页。下载安装后请重新运行。');
            modal.close();
          }
        },
        { text: '暂不', onClick: (modal) => modal.close() },
      ],
    });
    void detailMsg;
  }
  // run.js 的 _doStart 先于本文件加载，经 MRA 延迟取用
  Object.assign(window.MRA, { showVigemDialog });

  // ---------- Tab 切换 ----------
  const tabButtons = Array.from(document.querySelectorAll('.mra-tab'));
  const TAB_ORDER = ['control', 'data', 'settings', 'about'];
  let _curTabIdx = 0; // 初始即主控 tab（HTML 默认激活），防止首次点当前 tab 误触发切换动画
  function switchTab(name) {
    const nextIdx = TAB_ORDER.indexOf(name);
    if (nextIdx === _curTabIdx) return; // 点击当前激活 tab：不重播切换动画
    const prevIdx = _curTabIdx >= 0 ? _curTabIdx : nextIdx;
    _curTabIdx = nextIdx;
    const fromRight = nextIdx > prevIdx; // 前进：新页从右滑入；后退：从左滑入
    tabButtons.forEach((btn) => {
      btn.classList.toggle('mra-tab--active', btn.dataset.tab === name);
    });
    TAB_ORDER.forEach((p) => {
      $('page-' + p).classList.toggle('hidden', p !== name);
    });
    animatePageIn($('page-' + name), fromRight);
    moveTabSlider(name);
    if (name === 'data') pollTodayBoard(); // 切回数据页立即补拉一次看板，不等下一个 3s 轮询拍
    if (name === 'about') fetchAnnouncement(); // 切到关于页重拉公告，避免停留在启动时的旧缓存
    glitchVersionEgg(); // 1% 概率版本号乱码（彩蛋，详见 glitchVersionEgg 注释）
  }
  // 滑块缓动平移到目标 tab 底部（用 offsetLeft/offsetWidth，不逐页遍历）
  // 宽度取 tab 的 85%，并在 tab 内水平居中
  function moveTabSlider(name) {
    const btn = tabButtons.find((b) => b.dataset.tab === name);
    const slider = $('tab-slider');
    if (!btn || !slider) return;
    const w = Math.round(btn.offsetWidth * 0.85);
    slider.style.left = (btn.offsetLeft + Math.round((btn.offsetWidth - w) / 2)) + 'px';
    slider.style.width = w + 'px';
  }
  // 只对新页做单次滑入（缓动曲线），跨多 tab 也只在本页与当前视觉间播放，不逐页遍历
  function animatePageIn(page, fromRight) {
    if (!page) return;
    page.classList.remove('page-slide-in');
    void page.offsetWidth; // 强制 reflow 重置动画
    page.style.setProperty('--slide-from', fromRight ? '42px' : '-42px');
    page.classList.add('page-slide-in');
    // 动画结束/被打断后立即摘掉动画类：页面回归主文档光栅化。
    // 合成层提升窗口严格限定在动画期间；残留类会让页面停留在合成层路径，
    // 非整数 DPI（150%）下文字发虚。animationName 过滤避免子元素动画误触发。
    const detach = () => {
      page.removeEventListener('animationend', onEnd);
      page.removeEventListener('animationcancel', detach);
      if (page.classList.contains('page-slide-in'))
        page.classList.remove('page-slide-in');
    };
    const onEnd = (e2) => {
      if (e2.animationName !== 'page-slide') return;
      detach();
    };
    page.addEventListener('animationend', onEnd);
    page.addEventListener('animationcancel', detach);
  }
  tabButtons.forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  moveTabSlider('control'); // 初始：主控 tab 默认激活，滑块落地到首项
  // 关于页底部跳转按钮（data-link 值即逻辑目标名，经 sidecar 用默认浏览器打开）
  document.querySelectorAll('[data-link]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const target = btn.dataset.link;
      if (!target) return;
      try {
        await mra.call('open_external_url', { target });
      } catch (e) {
        showError('打开链接失败: ' + e.message);
      }
    });
  });

  // ---------- 关于页：检查更新 / 公告 ----------
  // 打开外部目标：只传逻辑目标名，地址由 sidecar 侧白名单给出（前端不传 URL）。
  function openTarget(target) {
    if (!target) return;
    mra.call('open_external_url', { target }).catch((e) => showError('打开链接失败: ' + e.message));
  }

  // 渲染「版本与更新」卡状态：cls 决定配色，text 只作纯文本落地。
  // 入参是字符串而不是 HTML——调用点会喂远程文案（latest_tag）与异常信息，一旦拼接
  // 就等于把远程数据交给 HTML 解析器。
  function renderUpdateStatus(statusEl, cls, text, opts) {
    statusEl.className = 'ver-status ' + cls;
    statusEl.textContent = '';
    if (opts && opts.spinner) statusEl.appendChild(mkEl('span', 'spinner'));
    statusEl.appendChild(document.createTextNode(text));
  }

  async function checkUpdate() {
    const btn = $('btn-check-update');
    const statusEl = $('about-update-status');
    const releaseEl = $('about-new-release');
    if (!btn || !statusEl) return;
    btn.disabled = true;
    renderUpdateStatus(statusEl, 'ver-status--checking', '正在检查更新…', { spinner: true });
    releaseEl.style.display = 'none';
    try {
      const d = await mra.call('check_update');
      if (d.error) {
        renderUpdateStatus(statusEl, 'ver-status--err', d.error);
      } else if (d.status === 'no_release') {
        renderUpdateStatus(statusEl, 'ver-status--ok', '暂无发布版本');
      } else if (d.has_update) {
        renderUpdateStatus(statusEl, 'ver-status--new', '发现新版本 v' + d.latest_tag);
        releaseEl.style.display = 'flex';
        releaseEl.textContent = '';
        const info = mkEl('div', 'nr-info');
        info.appendChild(mkEl('div', 'nr-title', 'v' + d.latest_tag + ' 已发布'));
        info.appendChild(mkEl('div', 'nr-sub',
          (d.published_at ? '发布于 ' + d.published_at + ' · ' : '') + '建议更新到最新版本'));
        releaseEl.appendChild(info);
        const goBtn = mkEl('button', 'mra-btn mra-btn--primary', '前往下载');
        goBtn.id = 'btn-go-download';
        goBtn.type = 'button';
        appendIcon(goBtn, 'arrow-up-right');
        goBtn.addEventListener('click', () => openTarget('download'));
        releaseEl.appendChild(goBtn);
      } else {
        renderUpdateStatus(statusEl, 'ver-status--ok', '已是最新版本');
      }
    } catch (e) {
      console.error(e);
      renderUpdateStatus(statusEl, 'ver-status--err', '检查失败：' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // 拉取并渲染公告（title/date/url_text/body 全部走 textContent）
  async function fetchAnnouncement() {
    const bodyEl = $('about-announce-body');
    if (!bodyEl) return;
    bodyEl.textContent = '';
    try {
      const d = await mra.call('fetch_announcement');
      if (!d || d.level === 'none' || !d.title) {
        bodyEl.appendChild(mkEl('div', 'about-announce-empty', '暂无公告'));
        return;
      }
      const cls = d.level === 'warn' ? 'about-announce--warn' : 'about-announce--info';
      const card = mkEl('div', 'about-announce ' + cls);
      card.appendChild(mkEl('span', 'about-announce-badge', d.level === 'warn' ? '重要' : '公告'));
      const main = mkEl('div', 'about-announce-main');
      const titleRow = mkEl('div', 'about-announce-title', d.title);
      if (d.date) titleRow.appendChild(mkEl('span', 'about-announce-date', d.date));
      main.appendChild(titleRow);
      if (d.body) main.appendChild(mkEl('div', 'about-announce-body', d.body));
      if (d.url) {
        const linkBtn = mkEl('button', 'about-announce-link', d.url_text || '查看详情');
        linkBtn.id = 'btn-announce-link';
        linkBtn.type = 'button';
        linkBtn.addEventListener('click', () => openTarget('announcement'));
        main.appendChild(linkBtn);
      }
      card.appendChild(main);
      bodyEl.appendChild(card);
    } catch (e) {
      console.error(e);
      bodyEl.textContent = '';
      bodyEl.appendChild(mkEl('div', 'about-announce-empty', '暂无公告'));
    }
  }

  function initAbout() {
    const btn = $('btn-check-update');
    if (btn) btn.addEventListener('click', checkUpdate);
    fetchAnnouncement(); // 启动拉一次公告（进入关于页即展示）
  }
  initAbout();

  // ---------- 关于页彩蛋：点击版本号掉落文字（同款 MAA） ----------
  // 掉落池：怪谈短语，每第 FALLING_EGG_EVERY 次点击混入一条（其余掉当前版本号）
  const FALLING_EGG_TEXTS = ['3:33', '不要清点', '它记得你'];
  const FALLING_EGG_EVERY = 5;
  const MAX_FALLING = 40;
  let eggFallCount = 0;
  // 连点 EGG_DIALOG_CLICKS 次（2s 内不中断）弹出「员工守则」
  const EGG_DIALOG_CLICKS = 10;
  let eggClickCount = 0;
  let eggLastClickAt = 0;

  // 员工守则（规则怪谈，致敬 MAA）。结构：池中每次抽 EGG_RULE_SAMPLE 条（编号 1-8，随机）
  // + 第 9 条固定（修订声明，给抽样一个"官方解释"）+ 第 10 条（数字乱码 + 悬停显影）。
  // 条目锚点全部是 GUI 真实存在的元素（静音恢复/日志轮转/状态栏绿点/自动关机/数据页看板）；
  // 口味：执行体口吻不许笑、暗示事件不解释、数字母题（清点/数错）贯穿首尾。
  const EGG_RULE_META = '本守则会不定期修订。你读到的版本，就是你需要读的版本。';
  const EGG_RULES_POOL = [
    '正式版没有「调试模式」。如果你在任何页面看到它，不要点击，也不要截图。开发者会知道你来过。',
    '运行前请断开所有物理手柄。如果你确定已经断开，界面却显示「已连接」，请相信你的手，不要相信界面。',
    '运行期间游戏会被静音，结束后自动恢复。如果音量没有回来，先别动音量键——想一想，刚才那一场，是不是你没有开始过的。',
    '日志按会话分目录，只保留最近若干份。如果某个目录的修改时间早于你安装软件的时间，不要打开它。里面没有你会想看的东西。',
    '左下角的绿点表示系统就绪。它应该一直是静止的。如果你看到它闪了两下，那不是故障，是确认。',
    '从关于页掉落的版本号是正常的。如果它们开始排队，请不要清点数量。',
    '任务结束后它会关闭模拟器。它只会关闭模拟器。到目前为止，它只关闭模拟器。',
    '「停止」按钮随时有效。如果它在你按下之前就已经变灰，说明有人替你停止了这一场。不要去查是谁。',
    '软件不会在你睡觉时更新自己。如果你早上发现版本号变了，那不是更新。',
    '数据页会如实记录今天的每一场。如果数字对不上，多出来的那一场，不算你的。',
    '版本号由版本控制自动推导，任何人不得手改。如果你在关于页看到自己的名字，而你从没参与开发——这一版是专门为你编译的。',
    '软件没有语音。如果你听到低语，先听清它在说什么。多数时候，它只是在重复你自己说过的话。'
  ];
  const EGG_RULE_LAST = '最后一条规则不存在。如果你已经数到了它，说明你数错了。这很正常。';
  const EGG_RULE_SAMPLE = 8;

  function blinkStatusDot() {
    const dot = $('status-dot');
    if (!dot) return;
    dot.classList.remove('mra-dot-blink');
    void dot.offsetWidth; // 重置动画：摘类→强制回流→再挂
    dot.classList.add('mra-dot-blink');
  }

  // 离开按钮的 DodgeField [v6]：躲避场 = 整张弹层卡，按钮二维逃跑（能扑到正文上）。
  // 位移 = 背离光标方向 × 强度（随距离平方衰减）× reach；出界钳制 + 撞墙滑移（被夹掉的
  // 分量沿自由轴重分配，角落困不死）+ 角落反困死（残余压力转墙向滑移选离光标最远候选）。
  // 感知基准 = 按钮视觉中心。抓到 = 亮下一句（EGG_CATCH_LINES），末句亮起后放弃躲避，
  // 再抓一次才关门。灵感来自 reactbits.dev 的 DodgeField / DecryptedText（MIT + Commons
  // Clause）——未复制其代码，按公开交互行为 vanilla 自实现。
  // reduced-motion 与触屏不启用；监听挂 window，经返回的 cleanup 随弹层关闭解绑。
  const EGG_DODGE = { radius: 120, reach: 140, falloff: 2, inset: 12 };
  const EGG_CATCH_LINES = ['抓不到', '差一点', '太慢了', '别追了', '你为什么执意要走', '好吧。你走吧。'];
  function wireEggDodge(modal, chase) {
    const field = modal.card;
    const btn = field.querySelector('.mra-modal-btn');
    if (!field || !btn) return null;
    btn.classList.add('mra-modal-btn--egg');
    // 抓到 = 亮下一句：pointerdown 无条件挂（按下瞬间即判定，逃跑中也能抓到）。
    // 躲避受环境门控可缺席（reduced-motion/触屏/远桌面下 media query 不匹配），
    // 但交互不能跟着缺席——否则按钮成死按钮，用户被困在弹层里。
    btn.addEventListener('pointerdown', () => {
      chase.catches++;
      const idx = chase.catches - 1;
      if (idx < EGG_CATCH_LINES.length) {
        btn.textContent = EGG_CATCH_LINES[idx];
        if (idx === EGG_CATCH_LINES.length - 1) chase.gave = true;
        return;
      }
      modal.close();
    });
    // 移动层门控：动画缺席时交互退化为纯点击推进
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return null;
    if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return null;
    btn.style.minWidth = btn.offsetWidth + 'px'; // 换字不抖行
    let raf = 0, pointer = null;
    let curX = 0, curY = 0; // 当前位移，用于从视觉矩形反推布局位
    const setT = (tx, ty, fleeing) => {
      // 逃离用短促缓出，返回用带回弹的长缓动（对齐原组件 flee/return 双段手感）
      btn.style.transition = 'transform ' + (fleeing ? '.13s' : '.62s') + ' cubic-bezier('
        + (fleeing ? '0.23, 1, 0.32, 1' : '0.34, 1.56, 0.64, 1') + ')';
      btn.style.transform = 'translate(' + tx + 'px,' + ty + 'px)';
      curX = tx; curY = ty;
    };
    function frame() {
      raf = 0;
      if (!document.body.contains(btn)) return; // 弹层已卸载，正式清理走 cleanup
      const cardR = field.getBoundingClientRect();
      const btnR = btn.getBoundingClientRect();
      const layoutL = btnR.left - curX, layoutT = btnR.top - curY;
      const cx = btnR.left + btnR.width / 2, cy = btnR.top + btnR.height / 2;
      const d = pointer ? Math.hypot(pointer.x - cx, pointer.y - cy) : Infinity;
      let flee = 0, tx = 0, ty = 0, fleeing = false;
      if (!chase.gave && pointer && Number.isFinite(d) && d > 0) {
        flee = d < EGG_DODGE.radius ? Math.pow(1 - d / EGG_DODGE.radius, EGG_DODGE.falloff) : 0;
        if (flee > 0) {
          const dx = pointer.x - cx, dy = pointer.y - cy;
          const dd = Math.hypot(dx, dy) || 1;
          tx = -(dx / dd) * flee * EGG_DODGE.reach;
          ty = -(dy / dd) * flee * EGG_DODGE.reach;
          fleeing = true;
        }
      }
      // 出界钳制 + 撞墙滑移：被夹掉的分量沿自由轴重分配——角落不再困死按钮
      const minX = cardR.left + EGG_DODGE.inset - layoutL;
      const maxX = cardR.right - EGG_DODGE.inset - btnR.width - layoutL;
      const minY = cardR.top + EGG_DODGE.inset - layoutT;
      const maxY = cardR.bottom - EGG_DODGE.inset - btnR.height - layoutT;
      const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
      const desiredX = tx, desiredY = ty;
      tx = clamp(desiredX, minX, maxX);
      ty = clamp(desiredY, minY, maxY);
      const lostX = Math.abs(desiredX - tx);
      const lostY = Math.abs(desiredY - ty);
      if (lostY > 0.5) tx = clamp(tx + Math.sign(desiredX || 1) * lostY, minX, maxX);
      if (lostX > 0.5) ty = clamp(ty + Math.sign(desiredY || 1) * lostX, minY, maxY);
      // 角落反困死：主方向被两面墙同时吃掉时（残余压力大），把残余压力转成墙向滑移——
      // 四个候选里选「离光标最远」的那个，总位移仍不超过 reach
      const press = flee > 0 ? flee * EGG_DODGE.reach : 0;
      const got = Math.hypot(tx, ty);
      const residual = press - got;
      if (residual > 6 && pointer) {
        const dist2 = (vx, vy) => {
          const px = layoutL + curX + vx + btnR.width / 2;
          const py = layoutT + curY + vy + btnR.height / 2;
          return (px - pointer.x) ** 2 + (py - pointer.y) ** 2;
        };
        const cands = [
          [clamp(tx + residual, minX, maxX), ty],
          [clamp(tx - residual, minX, maxX), ty],
          [tx, clamp(ty + residual, minY, maxY)],
          [tx, clamp(ty - residual, minY, maxY)],
        ];
        let best = null, bestD = -1;
        for (const cd of cands) {
          const mag = Math.hypot(cd[0], cd[1]);
          const scale = mag > EGG_DODGE.reach ? EGG_DODGE.reach / mag : 1;
          const dd2 = dist2(cd[0] * scale, cd[1] * scale);
          if (dd2 > bestD) { bestD = dd2; best = [cd[0] * scale, cd[1] * scale]; }
        }
        if (best && bestD > dist2(tx, ty)) { tx = best[0]; ty = best[1]; }
      }
      setT(tx, ty, fleeing);
    }
    const onMove = (e) => { pointer = { x: e.clientX, y: e.clientY }; if (!raf) raf = requestAnimationFrame(frame); };
    const onLeave = () => { pointer = null; if (!raf) raf = requestAnimationFrame(frame); };
    window.addEventListener('pointermove', onMove, { passive: true });
    document.addEventListener('pointerleave', onLeave);
    window.addEventListener('blur', onLeave);
    return () => {
      window.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerleave', onLeave);
      window.removeEventListener('blur', onLeave);
      if (raf) cancelAnimationFrame(raf);
    };
  }

  // [v6] 第 10 条数字乱码：字符集内随机翻动，自左向右逐位落定为「10.」（DecryptedText 手法
  // 自实现）。起播在悬停触发——若挂在渐现延迟里起播，整行还透明，等于白播。
  function scrambleNumber(el, finalText, frames) {
    const glyphs = '01<>#$%&@?!/\\_-';
    let frame = 0;
    // 帧数缺省按文本长度适配（40ms/帧）：版本号长串也来得及看清解码过程
    const total = frames || Math.max(20, Math.ceil(finalText.length * 1.2));
    const timer = setInterval(() => {
      if (!document.body.contains(el)) { clearInterval(timer); return; }
      frame++;
      const settled = Math.floor((frame / total) * finalText.length);
      let s = finalText.slice(0, settled);
      for (let i = settled; i < finalText.length; i++) {
        s += glyphs[Math.floor(Math.random() * glyphs.length)];
      }
      el.textContent = s;
      if (frame >= total) { el.textContent = finalText; clearInterval(timer); }
    }, 40);
    return timer;
  }

  // 1% 概率：切到关于页时版本号套一次乱码解码（与第 10 条同款手法，同源世界观）。
  // 概率常量放在这里：想预览效果临时改成 1 即可。reduced-motion 下不出。
  const EGG_VERSION_GLITCH_P = 0.01;
  let versionScrTimer = null;
  function glitchVersionEgg() {
    if (Math.random() >= EGG_VERSION_GLITCH_P) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const n = $('about-version');
    if (!n || !n.textContent) return;
    if (versionScrTimer) clearInterval(versionScrTimer);
    // 捕获触发时的文本作为落定值；期间若被更新检查重写，下一次重写会覆盖回来
    versionScrTimer = scrambleNumber(n, n.textContent);
  }

  function showRulesDialog() {
    const pool = EGG_RULES_POOL.slice();
    const picked = [];
    while (picked.length < EGG_RULE_SAMPLE && pool.length) {
      picked.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
    }
    const rows = [];
    picked.forEach((r, i) => {
      rows.push('<p class="mra-modal-rule" style="animation-delay:' + (i * 100) + 'ms">'
        + '<b class="mra-modal-rule-num">' + (i + 1) + '.</b><span>' + r + '</span></p>');
    });
    rows.push('<p class="mra-modal-rule" style="animation-delay:' + (picked.length * 100) + 'ms">'
      + '<b class="mra-modal-rule-num">9.</b><span>' + EGG_RULE_META + '</span></p>');
    rows.push('<p class="mra-modal-rule mra-modal-rule--ghost" style="animation-delay:'
      + (picked.length * 100 + 150) + 'ms"><b class="mra-modal-rule-num" data-final="10.">10.</b>'
      + '<span class="mra-modal-rule-secret">' + EGG_RULE_LAST + '</span></p>');
    const chase = { catches: 0, gave: false };
    let cleanupDodge = null;
    let scrTimer = null;
    const modal = openModal({
      title: '员工守则',
      maxWidth: 580,
      bodyHtml:
        '<div class="mra-modal-rules">' +
        '<img class="mra-modal-rules-icon" src="../../../assets/icon.ico" alt="">' +
        '<div class="mra-modal-rules-list">' + rows.join('') + '</div>' +
        '</div>',
      closeOnOverlay: false, // 不给逃课出口：点空白不关（Esc 仍可）
      onClose: () => {
        blinkStatusDot();
        if (cleanupDodge) cleanupDodge();
        if (scrTimer) clearInterval(scrTimer);
      },
      buttons: [
        { text: '确定要退出吗？', primary: true },
      ],
    });
    const title = modal.card.querySelector('.mra-modal-title');
    if (title) title.classList.add('mra-modal-title--egg');
    // 第 10 条：整行（序号+正文）默认隐形；悬停时序号乱码解码 + 正文显影，移出复隐
    const ghost = modal.card.querySelector('.mra-modal-rule--ghost');
    if (ghost) {
      ghost.addEventListener('mouseenter', () => {
        ghost.classList.add('revealed');
        const num = ghost.querySelector('.mra-modal-rule-num');
        if (scrTimer) clearInterval(scrTimer);
        scrTimer = scrambleNumber(num, '10.');
      });
      ghost.addEventListener('mouseleave', () => ghost.classList.remove('revealed'));
    }
    cleanupDodge = wireEggDodge(modal, chase);
  }

  function spawnFallingText(text, cx, cy, isEgg) {
    const alive = document.querySelectorAll('.falling-text');
    if (alive.length >= MAX_FALLING) alive[0].remove();
    const el = document.createElement('div');
    el.className = 'falling-text' + (isEgg ? ' falling-text--egg' : '');
    el.textContent = text;
    document.body.appendChild(el);
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    let x = cx - w / 2;
    let y = cy - h / 2;
    let vx = (Math.random() - 0.5) * 240;
    let vy = -80 - Math.random() * 100;
    let rot = 0;
    let vr = (Math.random() - 0.5) * 160;
    let bounces = 0;
    let gone = false;
    let last = performance.now();
    function step(now) {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      vy += 1800 * dt;
      x += vx * dt;
      y += vy * dt;
      rot += vr * dt;
      const floor = window.innerHeight - h - 8;
      if (y > floor) {
        y = floor;
        vy = -vy * 0.42;
        vx *= 0.72;
        vr *= 0.5;
        if (++bounces > 3 || Math.abs(vy) < 60) gone = true;
      }
      el.style.transform = 'translate(' + x + 'px,' + y + 'px) rotate(' + rot + 'deg)';
      if (gone) {
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 500);
        return;
      }
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function initVersionEgg() {
    ['about-version'].forEach((id) => {
      const n = $(id);
      if (!n) return;
      n.addEventListener('click', (e) => {
        const now = Date.now();
        eggClickCount = now - eggLastClickAt < 2000 ? eggClickCount + 1 : 1;
        eggLastClickAt = now;
        if (eggClickCount >= EGG_DIALOG_CLICKS) {
          eggClickCount = 0;
          showRulesDialog();
        }
        eggFallCount++;
        const eggHit = Array.isArray(FALLING_EGG_TEXTS) && FALLING_EGG_TEXTS.length
          && eggFallCount % FALLING_EGG_EVERY === 0;
        const t = eggHit
          ? FALLING_EGG_TEXTS[Math.floor(Math.random() * FALLING_EGG_TEXTS.length)]
          : n.textContent;
        spawnFallingText(t, e.clientX, e.clientY, eggHit);
      });
    });
  }
  initVersionEgg();

  // ---------- 标题栏交互区上报 ----------
  // 交互区（brand / tabs / win-controls）逐元素矩形上报给 C#：精确注册 Passthrough，
  // 空白区保持 Draggable（整条标题栏带，系统处理拖动与双击最大化）
  function reportDragExcludes() {
    const header = document.getElementById('titlebar');
    if (!header || !window.chrome.webview) return;
    const rects = Array.from(header.querySelectorAll('.brand, .tabs, .win-controls'))
      .map((n) => {
        const r = n.getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height };
      });
    window.chrome.webview.postMessage({
      type: 'drag-exclude',
      rects, // CSS 像素（DIP）
    });
  }
  reportDragExcludes();
  // resize：重报拖拽区 + tab 滑块按当前激活 tab 重定位（窗口宽窄变化会改变 tab 的
  // offsetLeft/offsetWidth，不重算滑块会留在旧位置造成视觉错位）
  window.addEventListener('resize', () => {
    reportDragExcludes();
    moveTabSlider(TAB_ORDER[_curTabIdx]);
  });

  // ---------- 自绘窗口控制按钮 ----------
  // 点击 → C# win-action（最小化/最大化/关闭，postWindowAction 在 js/rpc.js）；
  // C# 推送 maximized → 切换图标与无障碍文案
  function initWindowControls() {
    $('btn-win-min').addEventListener('click', () => postWindowAction('minimize'));
    $('btn-win-max').addEventListener('click', () => postWindowAction('maximize'));
    $('btn-win-close').addEventListener('click', () => postWindowAction('close'));
    mra.onNativeMessage((msg) => {
      // PEEP 悬浮窗开/关：预览卡与占位符互换，帧消费权交接
      if (msg.type === 'peep-floating') {
        setPreviewFloating(Boolean(msg.value));
        return;
      }
      if (msg.type !== 'maximized') return;
      const btn = $('btn-win-max');
      if (!btn) return;
      const maxed = Boolean(msg.value);
      btn.classList.toggle('win-btn--maximized', maxed);
      btn.setAttribute('aria-label', maxed ? '还原' : '最大化');
      btn.setAttribute('title', maxed ? '还原' : '最大化');
    });
  }
  initWindowControls();

  // ---------- 主控 ----------
  async function init() {
    try {
      const data = await mra.call('get_initial_state');
      $('app-version').textContent = 'v' + data.version;
      $('about-version').textContent = 'v' + data.version;
      const verCur = $('about-ver-current');
      if (verCur) verCur.textContent = 'v' + data.version;
      renderModuleSelect(data.modules, data.selected_module);
      // 初始就按运行状态锁定（重连/刷新时可能已有模块在跑）
      updateModuleOptionsDisabled(!!data.is_running);
      state.stages = data.stages || [];
      renderStageList();
      // 初始化模块选项（仅 treasure 展示）
      bindModuleOptionsUI();
      refreshModuleOptions(data.selected_module);
      // 按当前模块渲染「数据/设置」页卡片
      renderModulePages(data.selected_module);
      if (!data.model_ok) setStatus('插件资源缺失', 'error');
    } catch (e) {
      console.error(e);
      reportError('toast', '初始化失败: ' + e.message);
    }
    checkRegistryOptimizations(); // 注册表权限体检（独立于初始化成败，内部自捕获异常）
    setTimeout(pollStatus, 250);
    setTimeout(pollLogs, 300);
    setInterval(pollTodayBoard, 3000); // 今日看板（仅数据页可见时刷新）
    peepConsumer.start(500);           // PEEP 预览轮询（peep 开启 + 数据页可见 + 未脱离悬浮窗时 ~10fps）
  }

  // 「（空）」选项：不选择任何活动模块（无可用模块 / 用户主动清空）时的合法状态，
  // value 为空串，避免空下拉栏取值报错。
  const EMPTY_MODULE_LABEL = '（空）';

  // 有效期端点（manifest 声明的 ISO 8601）→「YYYY-MM-DD HH:MM」原样展示：
  // 端点本就是游戏服时间，不做时区换算；格式不符时原样返回。
  function formatValidity(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ''));
    return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(iso || '');
  }

  function renderModuleSelect(modules, selectedId) {
    const sel = $('module-select');
    state.modules = Array.isArray(modules) ? modules : [];
    sel.innerHTML = '';
    state.modules.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.expired ? `${m.id} — ${m.name}（已过期）` : `${m.id} — ${m.name}`;
      // 到期时间给自绘下拉投影层算倒计时徽章（js/select.js）；缺省 = 永久
      if (m.valid_until) opt.dataset.validUntil = m.valid_until;
      if (m.expired) {
        // 置灰只是视觉提示：仍然可选中，选中时由 onModuleChange 弹窗确认
        opt.dataset.expired = '1';
        opt.style.color = 'var(--mra-foreground-secondary,#4A5160)';
      }
      if (m.id === selectedId) opt.selected = true;
      sel.appendChild(opt);
    });
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = EMPTY_MODULE_LABEL;
    if (!selectedId) blank.selected = true;
    sel.appendChild(blank);
    sel.onchange = onModuleChange;
    updateModuleDesc(selectedId);
    if (MRA.syncModuleSelect) MRA.syncModuleSelect(); // 自绘下拉投影跟进（js/select.js）
  }

  function updateModuleDesc(moduleId) {
    const el = $('module-desc');
    if (!el) return;
    if (!moduleId) {
      el.textContent = '未选择活动模块';
      return;
    }
    // 描述优先取本表（更通俗的叫法），没有则回落到后端注册表给的模块名
    // ——模块名只有一处真源（模块类的 NAME），前端不另抄一份。
    const descs = { treasure: '寻宝模式' };
    const m = state.modules.find((x) => x.id === moduleId);
    const parts = [descs[moduleId] || (m && m.name) || ''];
    if (m && m.expired) {
      parts.push('已过期' + (m.valid_until ? `（有效期至 ${formatValidity(m.valid_until)}）` : ''));
    }
    el.textContent = parts.filter(Boolean).join(' · ');
  }

  // 过期模块的强制选择确认：确认后按 force 下发，取消/点空白处一律撤销选项。
  // 复用通用模态（openModal），Promise 化以便 onModuleChange 顺序处理。
  function confirmExpiredModule(moduleId) {
    const m = state.modules.find((x) => x.id === moduleId);
    const until = (m && m.valid_until) ? formatValidity(m.valid_until) : '';
    return new Promise((resolve) => {
      let settled = false;
      const finish = (ok, modal) => {
        if (settled) return;
        settled = true;
        if (modal) modal.close();
        resolve(ok);
      };
      const modal = openModal({
        title: '模块已过期',
        titleColor: 'var(--mra-warning,#F59E0B)',
        bodyHtml:
          '<p class="mra-modal-text">' +
          '此模块已过期，可能无法正常使用！</p>' +
          (until
            ? '<p class="mra-modal-text--sub">' +
              '声明有效期至 ' + until + '。</p>'
            : ''),
        buttons: [
          { text: '取消', onClick: (mm) => finish(false, mm) },
          { text: '仍然选择', primary: true, onClick: (mm) => finish(true, mm) },
        ],
      });
      // 点空白关闭是 openModal 内置行为：一并视为取消（close 在点击时才取值，可安全包一层）
      const origClose = modal.close;
      modal.close = function () { finish(false, null); origClose(); };
    });
  }

  async function onModuleChange() {
    const sel = $('module-select');
    const mid = sel.value;
    const opt = sel.options[sel.selectedIndex];
    let force = false;
    if (mid && opt && opt.dataset.expired === '1') {
      const confirmed = await confirmExpiredModule(mid);
      if (!confirmed) {
        sel.value = MRA.currentModuleId || ''; // 撤销选择：回到当前生效模块
        if (MRA.syncModuleSelect) MRA.syncModuleSelect();
        return;
      }
      force = true;
    }
    try {
      const data = await mra.call('select_module', { module_id: mid, force: force });
      const applied = data.module_id || '';
      state.stages = data.stages || [];
      state.selected_index = 0;
      renderStageList();
      updateModuleDesc(applied);
      // 模块切换后：重新刷新模块专属选项（显示/隐藏 + 配置回读）
      refreshModuleOptions(applied);
      // 数据/设置页卡片跟随模块切换
      renderModulePages(applied);
    } catch (e) {
      console.error(e);
      showError(e.message);
      sel.value = MRA.currentModuleId || ''; // 后端拒绝时同样撤销显示值，避免前后端不一致
      if (MRA.syncModuleSelect) MRA.syncModuleSelect();
    }
  }

  // ---------- 启动 ----------
  init();
})();
