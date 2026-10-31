// MaaRM shell 前端 —— 选卡弹窗的卡片轮廓与描边几何。
//
// 为什么需要这个文件：卡片要的是**曲率连续**的拐角（G2），而 CSS 的 border-radius 只能
// 画圆弧——圆弧与直边相切，但曲率在切点从 0 直接跳到 1/r，观感是「到这里突然开始转」。
// 这里让曲率沿拐角从 0 平滑升到 1/R、再平滑回到 0，切点处与直边曲率一致。
// 代价是拐角沿边延伸约 1.5R——「过渡长、看着软」正是这么来的；硬把跨度压回 R 会把
// 曲率峰值抬高（超椭圆 n=3 时 1.78 倍），反而更像折了一下。
//
// 同一套采样点供两个用途：卡片轮廓（clip-path 裁照片/红光/描边）与描边中心线
// （沿法线内移，保证整条贴着边缘）。两者共用几何，不会错位。
// 不做的事：不改任何动效时序——描边的生长动画仍由 style.css 的 dashoffset 过渡驱动，
// 本文件只负责「路径长什么样」。
(function () {
  'use strict';

  // 可见线宽 2.5px。实际画 3.5px：描边若正好画到卡片边界，边界那圈像素是半覆盖的
  // （描边被裁掉一半、底图透上来），看着像没贴紧；多画的 1px 压在卡片外，由轮廓裁切
  // 切出硬边，边界上被切的是实色。
  const STROKE_VISIBLE = 2.5;
  const STROKE_BLEED = 1;                              // 越过卡片边界、被裁掉的量
  const STROKE_WIDTH = STROKE_VISIBLE + STROKE_BLEED;  // 3.5：只往内可见 2.5，往外 1 出血
  const CORNER_STEPS = 24; // 每个拐角的采样段数（R=10 时每段约 1px，肉眼无棱）

  // 卡片自带 overflow:hidden + border-radius 会先把内容裁成圆弧矩形，再叠 clip-path
  // 只取交集——所以不支持 path() 的引擎整体退回 CSS 圆角，不做半套。
  const SUPPORTED = typeof CSS !== 'undefined' && !!CSS.supports
    && CSS.supports('clip-path', 'path("M0 0 L1 1 Z")');

  // 拐角局部坐标：从顶边切点 (S,0) 走到侧边切点 (0,S)。
  // 曲率 κ(s) = κmax·sin(πs/L)，积分得转角 θ(s)；取峰值曲率 = 1/R 反解 L。
  function cornerLocal(R) {
    const L = Math.PI * Math.PI * R / 4;
    const kmax = Math.PI * Math.PI / (4 * L);
    const N = CORNER_STEPS * 2;
    const ds = L / N;
    const raw = [[0, 0]];
    let x = 0;
    let y = 0;
    for (let k = 1; k <= N; k++) {
      const s = (k - 0.5) * ds;                        // 中点法积分
      const th = kmax * (L / Math.PI) * (1 - Math.cos(Math.PI * s / L));
      x -= Math.cos(th) * ds;
      y += Math.sin(th) * ds;
      raw.push([x, y]);
    }
    const S = y;                                       // 对称：两个方向跨度相同
    return { S, pts: raw.map((p) => [S + p[0], p[1]]) };
  }

  // 以底边中点为起点、顶边中点为终点，拆成左右两条半程轮廓：
  // 描边生长动画要求两半同时从底边中点出发、在顶边中点会合。
  function outlineHalves(w, h, R) {
    const { S, pts: c } = cornerLocal(R);
    const rev = c.slice().reverse();
    const cx = w / 2;
    const right = [[cx, h], [w - S, h]]
      .concat(c.slice(1).map((p) => [w - p[0], h - p[1]]))  // 右下角（首点与上一段重复）
      .concat([[w, S]])
      .concat(rev.slice(1).map((p) => [w - p[0], p[1]]))    // 右上角
      .concat([[cx, 0]]);
    const left = [[cx, h], [S, h]]
      .concat(c.slice(1).map((p) => [p[0], h - p[1]]))      // 左下角
      .concat([[0, S]])
      .concat(rev.slice(1).map((p) => [p[0], p[1]]))        // 左上角
      .concat([[cx, 0]]);
    return { right, left };
  }

  // 沿法线向内偏移：描边必须与轮廓平行。按「同形状缩小一圈」算会在拐角处漂离边缘
  // （超椭圆缩小后拐角向外偏），所以逐点取切线法线、朝卡片中心方向内移。
  function offsetInward(pts, d, cx, cy) {
    return pts.map((p, k) => {
      const a = pts[Math.max(0, k - 1)];
      const b = pts[Math.min(pts.length - 1, k + 1)];
      let tx = b[0] - a[0];
      let ty = b[1] - a[1];
      const len = Math.hypot(tx, ty) || 1;
      tx /= len;
      ty /= len;
      let nx = -ty;
      let ny = tx;
      if ((cx - p[0]) * nx + (cy - p[1]) * ny < 0) { nx = -nx; ny = -ny; }
      return [p[0] + nx * d, p[1] + ny * d];
    });
  }

  function toPath(pts) {
    return pts.map((p, k) => (k ? 'L ' : 'M ') + p[0].toFixed(2) + ' ' + p[1].toFixed(2)).join(' ');
  }

  // 给一张卡上形状：轮廓裁切 + 描边路径。可反复调用（窗口缩放、字体回流后重算），
  // 幂等：路径按当前尺寸重建，选中态由 CSS 的 dashoffset 决定，不会重播生长动画。
  function paint(cell) {
    const svg = cell.querySelector('.task-gallery-stroke');
    if (!svg || !SUPPORTED) return;
    const w = cell.offsetWidth;
    const h = cell.offsetHeight;
    if (!w || !h) return;
    // 拐角强度取卡片自己的 CSS 变量（CSS 是唯一真源，JS 不另存一份数值）
    const R = parseFloat(getComputedStyle(cell).getPropertyValue('--gal-radius')) || 0;
    if (R <= 0) return;

    // 外沿轮廓 → 裁切（照片、压暗层、红光、描边都被它裁）
    const outer = outlineHalves(w, h, R);
    const loop = outer.right.concat(outer.left.slice().reverse());
    cell.style.clipPath = 'path("' + toPath(loop) + ' Z")';
    cell.style.overflow = 'visible';   // 让 clip-path 成为唯一的形状来源

    // 描边中心线：中心落在「外沿以内 (线宽/2 − 出血)」处，于是描边外沿越过卡片边界 1px，
    // 由轮廓裁切切出硬边——边界那圈是实色，不再有半覆盖的透底像素。
    const offset = STROKE_WIDTH / 2 - STROKE_BLEED;
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    // 线宽由这里下发（CSS 只写 var(--gal-stroke-width)）：几何与描边共用同一个数，
    // 避免「改了 CSS 忘了改 JS」导致描边漂离边缘。
    svg.style.setProperty('--gal-stroke-width', STROKE_WIDTH);
    svg.innerHTML = [outer.right, outer.left].map((pts) => {
      return '<path pathLength="1" d="' + toPath(offsetInward(pts, offset, w / 2, h / 2)) + '"></path>';
    }).join('');
  }

  // 批量上形状（新建网格后调用一次）
  function paintAll(root) {
    if (!root) return;
    root.querySelectorAll('.task-gallery-cell[data-manage]').forEach(paint);
  }

  window.MRAGalleryShape = { paint, paintAll, supported: SUPPORTED, strokeWidth: STROKE_VISIBLE };
})();
