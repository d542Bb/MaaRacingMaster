// 底栏状态球：一个会看人、会眨眼的小球，承担运行状态指示（替代原来的 8px 静态圆点）。
//
// 造型、参数与动效按「扁平球体 + 两只胶囊眼 + 视线跟随」这一效果规格自绘，
// 不依赖外部素材、第三方脚本或设计稿。状态语义沿用原指示灯的四种：
// ready / running / stopping / error。
//
// 眼睛是真 3D：不是贴在圆片上的两个矩形，而是球面上的弧。
// 每帧把球面基向量（右、上）与眼睛中心方向一起旋转，再沿球面取一串点投影到屏幕，
// 连成一条弧线当作笔画的脊。于是眼睛会随球面弯、转到侧面会变短变窄、
// 转到球边会被球轮廓切掉——都是投影的自然结果，不是逐角度手调的。
//
// 行为要点：
//   - 球体本身不动，动的只有眼睛。
//   - 指针进入激活半径才开始跟随；离开或静止后眼睛自己四处看，偶尔转回来。
//   - 空闲即休眠：视线收敛、又不在运行态时停掉 rAF，只在下一个事件（走神 / 眨眼 /
//     指针移动 / 状态切换）前唤醒——底栏常驻组件不该白烧 CPU。
//   - 动效降级：系统开启「减弱动效」时，漂移、眨眼、回声环全部停掉，
//     视线固定在默认位置；指针跟随保留（由用户自己触发，不属于自动播放）。
//
// 本模块只负责「怎么动」；「什么时候是什么状态」由调用方 setState 决定。
// 支持同时挂多个球（形态预览页需要并排对比），实例之间互不影响。
(function () {
  'use strict';

  // ---------- 球面几何 ----------
  // 眼睛在球面上的基准方向（球半径归一化为 1）：左右分开、略高于赤道。
  // 与效果规格一致：眼中心距 0.608 半径、高于球心 0.076 半径。
  const EYE_L = [-0.3038, 0.0759, 0.9495];
  const EYE_R = [0.3038, 0.0759, 0.9495];
  // 视线满偏时绕竖轴 / 横轴转过的弧度
  const YAW_GAIN = 0.92;
  const PITCH_GAIN = 0.78;
  // viewBox 内球心与半径（与骨架里的 cx/cy/r 一致）
  const ORB_CX = 20;
  const ORB_CY = 20;
  const ORB_R = 17;

  // ---------- 眼睛形状 ----------
  // 基准眼宽（球半径 17 时的 viewBox 单位）。倍率 1 = 参考组件的原始比例，
  // 22px 上照搬只有 1.2px 宽，所以默认乘 1.8；倍率可由外部调整。
  const EYE_W_BASE = 1.836;
  // 眼睛跨越的球面半角：眼高 0.364R × 1.5 补偿 ÷ 2
  const EYE_HALF_ANGLE = 0.273;
  // 沿弧线取的采样点数，折线在 22px 上看不出棱角
  const EYE_ARC_STEPS = 8;
  // 眼睛外圈那道「缝」的宽度，取眼宽的比例：固定值会在窄眼睛上显得过宽，
  // 像暗圈包着一根白线；跟着眼宽走才始终是「一圈细缝」
  const EYE_RING_RATIO = 0.26;
  // 停止中「--」：长度按「弧长 + 两端圆头」算，必须小于眼中心距（0.608R），
  // 否则左右两条接在一起就读成一条「—」。粗细略小于正常眼——
  // 横线的视觉重量本来就比同宽的竖线大，等宽会显得这两笔更重
  const DASH_LENGTH_GAIN = 0.72;
  const DASH_WIDTH_GAIN = 1;
  // 错误「××」：交叉线必须比正常眼细一点，等宽时交叉处会糊成一个实心块；
  // 但也不能太细，否则和另外两个表情不像同一支笔
  const CROSS_LENGTH_GAIN = 1;
  const CROSS_WIDTH_GAIN = 0.8;

  // ---------- 跟随 ----------
  // 指针进入这个半径才激活跟随；离开时放宽 20%，避免在边界上反复启停
  const ACTIVATE_RADIUS = 100;
  const ACTIVATE_HYSTERESIS = 1.2;
  // 方向归一化的饱和距离：贴着球时眼睛温和偏转，稍远就盯住
  const LOOK_RAMP = 45;
  // 每帧趋近比例：跟手（0.16）比自行漂移（0.1）更紧，漂移看起来才像走神
  const EASE_STEER = 0.16;
  const EASE_DRIFT = 0.1;
  // 指针静止多久算「没在看我」，之后眼睛开始自己看
  const IDLE_MS = 3000;
  // 默认视线：略微朝下（视线 y 向上为正，负值即向下）
  const REST_LOOK = { x: 0, y: -0.06 };
  const GLANCE_MIN = 900;
  const GLANCE_MAX = 3000;
  const GLANCE_BACK_CHANCE = 0.3;

  // ---------- 眨眼 ----------
  const BLINK_GAP_MIN = 2200;
  const BLINK_GAP_MAX = 6000;
  // 与 CSS 里眼睛的 transform 过渡时长配套：闭眼动画走完再摘类
  const BLINK_DURATION = 230;

  // 流转：渐变中心在球内缓慢游走。用相对坐标（0~1）而不是任何像素值，
  // 所以 22px 和 96px 上看到的是同一件事——一片亮在球里移动，
  // 不会像叠加光斑那样在大球上是晕、小球上缩成一个点。
  const FLOW_SPEED = 1;

  // 只有「正常睁眼」的状态才眨眼；半闭与皱眉本身就是表情，不再叠加眨眼
  const BLINKABLE = { ready: true, running: true, stopping: false, error: false };

  const instances = [];
  let pointer = null;        // 指针屏幕坐标；null 表示指针不在页面内
  let pointerAt = -Infinity; // 指针最后一次移动的时刻
  let reduced = false;
  let rafId = 0;
  let eyeWScale = 1.8;       // 眼宽倍率，外部可调
  let eyeHScale = 0.8;       // 眼高倍率，与宽度独立

  /** 把球面上的向量按视线角度旋转（先绕横轴俯仰，再绕竖轴偏航）。 */
  function orient(v, look) {
    const yaw = look.x * YAW_GAIN;
    const pitch = -look.y * PITCH_GAIN;
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const y = v[1] * cp - v[2] * sp;
    const z = v[1] * sp + v[2] * cp;
    return [v[0] * cy + z * sy, y, -v[0] * sy + z * cy];
  }

  /** 两个球面基向量的斜向合成（sign = ±1），用来给「××」取 ±45° 的两条对角线。 */
  function diagonal(a, b, sign) {
    let x = a[0] + b[0] * sign;
    let y = a[1] + b[1] * sign;
    let z = a[2] + b[2] * sign;
    const len = Math.sqrt(x * x + y * y + z * z) || 1;
    x /= len; y /= len; z /= len;
    // 再在屏幕平面内单位化：up 与 right 的投影长度不等，不校正的话
    // × 的两条对角线会一长一短，交叉点偏到一边
    const xy = Math.sqrt(x * x + y * y) || 1;
    return [x / xy, y / xy, z / xy];
  }

  /**
   * 沿球面画一段弧：以 dir 为中心、沿 axis 方向张开 ±halfAngle，
   * 逐点投影到屏幕，连成折线。折线本身就是弯曲的，这正是「贴在球面上」的来源。
   * 换 axis 就换方向——竖着是眼睛，横着是停止的「--」，斜着是错误的「××」。
   */
  function arcPath(dir, axis, halfAngle) {
    let d = '';
    for (let i = 0; i <= EYE_ARC_STEPS; i++) {
      const t = -halfAngle + (2 * halfAngle * i) / EYE_ARC_STEPS;
      const ct = Math.cos(t), st = Math.sin(t);
      const x = dir[0] * ct + axis[0] * st;
      const y = dir[1] * ct + axis[1] * st;
      const sx = Math.round((ORB_CX + x * ORB_R) * 100) / 100;
      const sy = Math.round((ORB_CY - y * ORB_R) * 100) / 100;
      d += (i === 0 ? 'M' : 'L') + sx + ' ' + sy;
    }
    return d;
  }

  // ---------- 全局指针与动效偏好（所有实例共享） ----------
  function onPointerMove(e) {
    pointer = { x: e.clientX, y: e.clientY };
    pointerAt = performance.now();
    // 循环可能因页面不可见而停过，指针一动就重新点火
    wake();
  }

  function onPointerOut() {
    pointer = null;
  }

  document.addEventListener('pointermove', onPointerMove, { passive: true });
  document.addEventListener('pointerdown', onPointerMove, { passive: true });
  document.addEventListener('pointerleave', onPointerOut, { passive: true });
  window.addEventListener('blur', onPointerOut);

  const mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  if (mq) {
    reduced = mq.matches;
    const onMotionChange = (e) => {
      reduced = e.matches;
      for (let i = 0; i < instances.length; i++) instances[i].onMotionChange();
    };
    if (mq.addEventListener) mq.addEventListener('change', onMotionChange);
    else if (mq.addListener) mq.addListener(onMotionChange); // 旧内核兜底
  }

  // ---------- 单帧推进（所有实例共用一个循环） ----------
  // 两个「不烧 CPU」的出口：
  //   页面不可见 → 只推进这一帧、不再排队，切回前台的第一帧不会是旧姿势；
  //   所有实例都静止（视线收敛、非运行态）→ 停 rAF，定时器等到下一个事件再唤醒。
  // 底栏组件常驻，后者是常态——不能让它每帧空转。
  let idleTimer = 0;

  function frame(now) {
    rafId = 0;
    let active = false;
    for (let i = 0; i < instances.length; i++) {
      if (instances[i].update(now)) active = true;
    }
    if (!instances.length || document.visibilityState === 'hidden') return;
    if (active) rafId = requestAnimationFrame(frame);
    else scheduleIdleWake();
  }

  /** 静止时按「下一个该动的时刻」设一个一次性定时器，到点再起循环。 */
  function scheduleIdleWake() {
    if (idleTimer) return;
    // 减弱动效下没有走神这回事，视线也不会再变——静止就是真静止，
    // 不排定时器（否则 nextGlance 永不推进，会退化成每 16ms 空转一次）
    if (reduced) return;
    let next = Infinity;
    for (let i = 0; i < instances.length; i++) {
      const t = instances[i].nextGlance;
      if (t < next) next = t;
    }
    if (!isFinite(next)) return;
    const delay = Math.max(16, next - performance.now());
    idleTimer = setTimeout(() => {
      idleTimer = 0;
      wake();
    }, delay);
  }

  function wake() {
    if (idleTimer) {
      clearTimeout(idleTimer);
      idleTimer = 0;
    }
    if (rafId || !instances.length) return;
    rafId = requestAnimationFrame(frame);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') wake();
  });

  // ---------- 挂载 ----------
  // root 需内含两个 .maarm-orb__eye-wrap（左右各一），每个 wrap 里是
  // .maarm-orb__blink > (.maarm-orb__eye-halo + .maarm-orb__eye)。骨架见 preview.html。
  function mount(root) {
    if (!root) return null;
    for (let i = 0; i < instances.length; i++) {
      if (instances[i].root === root) return instances[i];
    }

    const wrapL = root.querySelector('.maarm-orb__eye-wrap--l');
    const wrapR = root.querySelector('.maarm-orb__eye-wrap--r');
    const blinks = root.querySelectorAll('.maarm-orb__blink');
    const pick = (sel) => root.querySelector(sel);

    const parts = {
      haloL: pick('.maarm-orb__eye-wrap--l .maarm-orb__eye-halo'),
      eyeL: pick('.maarm-orb__eye-wrap--l .maarm-orb__eye'),
      haloR: pick('.maarm-orb__eye-wrap--r .maarm-orb__eye-halo'),
      eyeR: pick('.maarm-orb__eye-wrap--r .maarm-orb__eye')
    };

    const inst = {
      root: root,
      state: 'ready',
      look: { x: REST_LOOK.x, y: REST_LOOK.y }, // 当前视线
      goal: { x: REST_LOOK.x, y: REST_LOOK.y }, // 目标视线
      grad: null,                               // 该球自己的径向渐变（流转靠移动它的中心）
      applied: '',                              // 上次写进 DOM 的路径，用于跳过重复写入
      awake: false,                             // 指针是否在激活半径内
      nextGlance: performance.now() + GLANCE_MIN,
      blinkTimer: 0,

      update(now) {
        let steering = false;

        if (pointer) {
          const rect = this.root.getBoundingClientRect();
          const dx = pointer.x - (rect.left + rect.width / 2);
          const dy = (rect.top + rect.height / 2) - pointer.y; // 屏幕 y 向下，视线 y 向上
          const dist = Math.sqrt(dx * dx + dy * dy);
          // 迟滞：进入用一个半径，离开用略大的半径，边界上不会来回抖
          const limit = this.awake ? ACTIVATE_RADIUS * ACTIVATE_HYSTERESIS : ACTIVATE_RADIUS;
          this.awake = dist <= limit;
          // 指针停住 IDLE_MS 就不再算「在看我」：底栏角落的球不该被一只搁着不动的光标钉住
          const idle = now - pointerAt > IDLE_MS;

          if (this.awake && !idle && dist >= 1) {
            steering = true;
            // 只取方向；距离只用来把「贴着球」时的偏转放缓，稍远即盯住
            const ramp = Math.min(dist / LOOK_RAMP, 1);
            this.goal.x = (dx / dist) * ramp;
            this.goal.y = (dy / dist) * ramp;
          }
        } else {
          this.awake = false;
        }

        if (!steering) {
          if (reduced) {
            this.goal.x = REST_LOOK.x;
            this.goal.y = REST_LOOK.y;
          } else if (now >= this.nextGlance) {
            // 没人管的时候自己看看别处，偶尔转回来
            const back = Math.random() < GLANCE_BACK_CHANCE;
            this.goal.x = back ? REST_LOOK.x : (Math.random() * 2 - 1) * 0.8;
            this.goal.y = back ? REST_LOOK.y : (Math.random() * 2 - 1) * 0.5;
            this.nextGlance = now + GLANCE_MIN + Math.random() * (GLANCE_MAX - GLANCE_MIN);
          }
        }

        const ease = steering ? EASE_STEER : EASE_DRIFT;
        this.look.x += (this.goal.x - this.look.x) * ease;
        this.look.y += (this.goal.y - this.look.y) * ease;
        this.applyGaze();
        this.applyFlow(now);

        // 是否还需要下一帧：跟着指针、还在缓动、或者运行态内部在流转。
        // 全为否就让循环休眠——静止的球没有任何理由每帧重算一遍。
        return (
          steering ||
          Math.abs(this.goal.x - this.look.x) + Math.abs(this.goal.y - this.look.y) > 0.002 ||
          (this.state === 'running' && !reduced)
        );
      },

      /** 运行态：渐变中心沿两条不同频率的轨迹游走，合成不规则的缓慢流动。 */
      applyFlow(now) {
        const grad = this.grad;
        if (!grad || this.state !== 'running' || reduced) return;
        const t = now * 0.001 * FLOW_SPEED;
        const cx = 0.5 + 0.21 * Math.sin(t * 0.85) + 0.08 * Math.sin(t * 1.73 + 1.2);
        const cy = 0.5 + 0.19 * Math.cos(t * 0.62) + 0.07 * Math.cos(t * 1.31 + 0.7);
        grad.setAttribute('cx', cx.toFixed(3));
        grad.setAttribute('cy', cy.toFixed(3));
      },

      applyGaze() {
        if (!parts.eyeL && !parts.eyeR) return;

        const right = orient([1, 0, 0], this.look);
        const up = orient([0, 1, 0], this.look);
        // 水平压缩系数：眼睛宽度方向沿「右」，其屏幕投影长度就是透视压缩比
        const squash = Math.sqrt(right[0] * right[0] + right[1] * right[1]);
        const w = EYE_W_BASE * eyeWScale * squash;
        const ring = w * EYE_RING_RATIO;
        const halfAngle = EYE_HALF_ANGLE * eyeHScale;
        const dirL = orient(EYE_L, this.look);
        const dirR = orient(EYE_R, this.look);

        // 表情决定画什么，都是同一条球面弧线：睁眼竖着、停止横着、错误斜着交叉。
        // 线条粗细不再等于眼宽——「--」和「××」要更细才读得出来，按眼宽取比例。
        let dL, dR, stroke;
        if (this.state === 'error') {
          const a = diagonal(up, right, 1);
          const b = diagonal(up, right, -1);
          const ha = halfAngle * CROSS_LENGTH_GAIN;
          dL = arcPath(dirL, a, ha) + arcPath(dirL, b, ha);
          dR = arcPath(dirR, a, ha) + arcPath(dirR, b, ha);
          stroke = w * CROSS_WIDTH_GAIN;
        } else if (this.state === 'stopping') {
          const ha = halfAngle * DASH_LENGTH_GAIN;
          dL = arcPath(dirL, right, ha);
          dR = arcPath(dirR, right, ha);
          stroke = w * DASH_WIDTH_GAIN;
        } else {
          dL = arcPath(dirL, up, halfAngle);
          dR = arcPath(dirR, up, halfAngle);
          stroke = w;
        }

        const key = dL + '|' + dR + '|' + stroke.toFixed(2);
        if (key === this.applied) return; // 值没变就不碰 DOM
        this.applied = key;

        if (parts.haloL) {
          parts.haloL.setAttribute('d', dL);
          parts.haloL.setAttribute('stroke-width', (stroke + ring * 2).toFixed(2));
        }
        if (parts.eyeL) {
          parts.eyeL.setAttribute('d', dL);
          parts.eyeL.setAttribute('stroke-width', stroke.toFixed(2));
        }
        if (parts.haloR) {
          parts.haloR.setAttribute('d', dR);
          parts.haloR.setAttribute('stroke-width', (stroke + ring * 2).toFixed(2));
        }
        if (parts.eyeR) {
          parts.eyeR.setAttribute('d', dR);
          parts.eyeR.setAttribute('stroke-width', stroke.toFixed(2));
        }
      },

      scheduleBlink() {
        clearTimeout(this.blinkTimer);
        this.blinkTimer = 0;
        if (reduced || !BLINKABLE[this.state]) return;
        const gap = BLINK_GAP_MIN + Math.random() * (BLINK_GAP_MAX - BLINK_GAP_MIN);
        this.blinkTimer = setTimeout(() => this.blink(), gap);
      },

      blink() {
        this.blinkTimer = 0;
        for (let i = 0; i < blinks.length; i++) {
          const el = blinks[i];
          el.classList.add('is-blinking');
          setTimeout(() => el.classList.remove('is-blinking'), BLINK_DURATION);
        }
        this.scheduleBlink();
      },

      onMotionChange() {
        if (reduced) {
          for (let i = 0; i < blinks.length; i++) blinks[i].classList.remove('is-blinking');
        }
        this.scheduleBlink();
        wake();
      },

      setState(mode) {
        const next = Object.prototype.hasOwnProperty.call(BLINKABLE, mode) ? mode : 'ready';
        const changed = next !== this.state;
        this.state = next;
        this.root.dataset.state = next;
        if (!changed) return;

        if (next !== 'running' && this.grad) {
          // 离开运行态：把渐变中心复位，球回到均匀的实心
          this.grad.setAttribute('cx', '0.5');
          this.grad.setAttribute('cy', '0.5');
        }

        if (!BLINKABLE[next]) {
          // 切进半闭 / 皱眉：摘掉可能在途的闭眼类，免得回位时闪一下
          for (let i = 0; i < blinks.length; i++) blinks[i].classList.remove('is-blinking');
        }

        this.scheduleBlink();
        // 表情几何是按状态算的，切完必须立刻重算一帧（休眠中也要唤醒）
        wake();
      },

      destroy() {
        clearTimeout(this.blinkTimer);
        const i = instances.indexOf(this);
        if (i >= 0) instances.splice(i, 1);
      }
    };

    inst.grad = root.querySelector('.maarm-orb__grad');

    instances.push(inst);
    inst.setState(root.dataset.state || 'ready');
    inst.applyGaze(); // 首帧就把眼睛摆到球面上，否则会是两条空路径
    inst.scheduleBlink();
    wake();
    return inst;
  }

  // 底栏那颗球由本模块自己挂载：脚本放在 body 末尾，DOM 已就绪；
  // 宿主不存在时静默跳过，模块可被其他页面安全引用。
  function autoMount() {
    const root = document.getElementById('status-dot');
    if (root && root.classList.contains('maarm-orb')) mount(root);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoMount);
  } else {
    autoMount();
  }

  window.StatusOrb = {
    mount: mount,
    // 页面里只有一个球的常见情形：按 id 直接取，省掉调用方自己查节点
    mountById(id) {
      return mount(document.getElementById(id || 'status-dot'));
    },
    /**
     * 按状态名切换（ready / running / stopping / error），作用到所有已挂载的球。
     * 底栏只有一个球，调用方（js/run.js 的 setStatus）用这个就够，不必自己持有实例。
     * 不认识的状态名回落 ready——与后端 get_status 的判定分支一一对应。
     */
    setState(mode) {
      for (let i = 0; i < instances.length; i++) instances[i].setState(mode);
    },
    /**
     * 眼宽 / 眼高倍率：1 = 参考比例，默认 1.8 / 0.8。两者独立，改完下一帧生效。
     * 省略 height 时沿用当前值，只调宽度。
     */
    setEyeSize(widthScale, heightScale) {
      eyeWScale = Math.max(0.2, Math.min(5, Number(widthScale) || 1));
      if (heightScale !== undefined) {
        eyeHScale = Math.max(0.2, Math.min(5, Number(heightScale) || 1));
      }
      for (let i = 0; i < instances.length; i++) {
        instances[i].applied = ''; // 让下一帧强制重写
      }
      wake();
    }
  };
})();
