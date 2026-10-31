// MaaRM shell 前端 —— 选卡卡片的描边几何。
//
// **卡片轮廓不在这里。** 形状交给 CSS：border-radius 20px + corner-shape: squircle
// （指数 4 的超椭圆），照片、红光、压暗层、描边越界的那 1px 都由卡片自带的
// overflow:hidden 原生裁切。本文件只做 CSS 做不到的那件事——**按同一条方程生成描边路径**：
// 描边要从底边中点出发、绕一圈在顶边中点会合，CSS 没有这个能力。
//
// 为什么半径是 20：卡片原来用曲率连续（G2）的拐角、半径 10。换成指数 4 之后，让
// 「切掉的角部面积」和「逐行轮廓」同时对齐，解出的等效半径是 20.5（面积解 20.3、
// 45° 切深解 21.1）。取 20 这个整数，与 G2@10 的轮廓最大偏差 0.46px（浏览器实测，
// WebView2 141 与 Edge 150 两套引擎结论一致）——肉眼不可分。**所以这次换的是实现，
// 不是观感。** 推导与验收阈值见 tools/measure_corner_shape.py。
//
// 降级：引擎不认识 corner-shape 时，border-radius 画出的是圆弧；本文件同步切到指数 2
// （同一条超椭圆方程的特例）。形状仍然自洽，不会出现「描边按方角、卡片按圆角」。
//
// 不做的事：不改任何动效时序——描边的生长动画仍由 style.css 的 dashoffset 过渡驱动，
// 本文件只负责「路径长什么样」。左右两半必须等长（同时出发、同时会合），这一点与
// 用哪条曲线无关，所以换曲线时拆半逻辑不用动。
(function () {
  'use strict';

  // 可见线宽 2.5px。实际画 3.5px：描边若正好画到卡片边界，边界那圈像素是半覆盖的
  // （描边被裁掉一半、底图透上来），看着像没贴紧；多画的 1px 压在卡片外，由 overflow
  // 切出硬边，边界上被切的是实色。
  const STROKE_VISIBLE = 2.5;
  const STROKE_BLEED = 1;                              // 越过卡片边界、被裁掉的量
  const STROKE_WIDTH = STROKE_VISIBLE + STROKE_BLEED;  // 3.5：只往内可见 2.5，往外 1 出血
  const CORNER_STEPS = 24; // 每个拐角的采样段数（R=20 时每段约 1.3px，肉眼无棱）

  // 与 CSS 实际画出的那条曲线一致：支持 corner-shape 时指数 4，否则圆弧（指数 2）。
  const SQUIRCLE = typeof CSS !== 'undefined' && !!CSS.supports
    && CSS.supports('corner-shape', 'squircle');
  const EXPONENT = SQUIRCLE ? 4 : 2;

  // 拐角局部坐标：角点在原点，从顶边切点 (R,0) 走到侧边切点 (0,R)。
  // 超椭圆 |u|^n + |v|^n = 1（u = (R−x)/R，v = (R−y)/R），参数化后取点；
  // n = 2 退化成圆弧（圆心 (R,R)、半径 R），n = 4 就是 CSS 的 squircle。
  function cornerLocal(R) {
    const N = CORNER_STEPS * 2;
    const pts = [];
    for (let k = 0; k <= N; k++) {
      const t = (Math.PI / 2) * (k / N);
      pts.push([
        R - R * Math.pow(Math.cos(t), 2 / EXPONENT),
        R - R * Math.pow(Math.sin(t), 2 / EXPONENT),
      ]);
    }
    pts.reverse();                                     // (0,R)→(R,0) 翻成 (R,0)→(0,R)
    return { S: R, pts };
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

  // 给一张卡上描边路径。可反复调用（窗口缩放、字体回流后重算），幂等：
  // 路径按当前尺寸重建，选中态由 CSS 的 dashoffset 决定，不会重播生长动画。
  function paint(cell) {
    const svg = cell.querySelector('.task-gallery-stroke');
    if (!svg) return;
    const w = cell.offsetWidth;
    const h = cell.offsetHeight;
    if (!w || !h) return;
    // 半径取卡片自己的 CSS 变量（CSS 是唯一真源，JS 不另存一份数值）
    const R = parseFloat(getComputedStyle(cell).getPropertyValue('--gal-radius')) || 0;
    if (R <= 0) return;

    const outer = outlineHalves(w, h, R);

    // 描边中心线：中心落在「外沿以内 (线宽/2 − 出血)」处，于是描边外沿越过卡片边界 1px，
    // 由卡片的 overflow 切出硬边——边界那圈是实色，不再有半覆盖的透底像素。
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

  window.MRAGalleryShape = {
    paint,
    paintAll,
    squircle: SQUIRCLE,
    exponent: EXPONENT,
    strokeWidth: STROKE_VISIBLE,
  };
})();
