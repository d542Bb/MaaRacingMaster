// MaaRM shell 图标真源（唯一来源，规范见同目录 README.md）。
// 数据格式：IconNode（Lucide 数据格式，[标签, 属性] 元组的数组），与 morphicons 直接兼容。
// 分区一 `LUCIDE`：path 数据拷自 lucide v1.45.0（ISC 许可，声明见仓库 THIRD_PARTY_LICENSES.md），
//   来源 https://lucide.dev ；升级版本时逐个重拷并以 `node tools/... 校验`（见 README）。
// 分区二 `CUSTOM`：lucide 没有或不适用的图标 —— github 为 lucide 0.x 时代的品牌图标
//   （lucide 1.x 已移除品牌图标），media-* 为实心媒体控件惯例画法（lucide 仅提供描边版），
//   实心图标不参与 morph 变形动画。
// 铁律：新增图标先查 lucide 同名图标拷数据，禁止随手画；窗口标题栏 chrome（最小化/最大化/
//   关闭）与性能走势图 polyline 不属于图标系统，分别留在 index.html 原位，不进本文件。
(function () {
  'use strict';

  var BASE_SVG = {
    'viewBox': '0 0 24 24',
    'fill': 'none',
    'stroke': 'currentColor',
    'stroke-width': '2',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true'
  };
  var SOLID_SVG = {
    'viewBox': '0 0 24 24',
    'fill': 'currentColor',
    'stroke': 'currentColor',
    'stroke-width': '2',
    'aria-hidden': 'true'
  };

  var LUCIDE = {
    'chevron-down': [['path', { 'd': 'm6 9 6 6 6-6' }]],
    'trash': [
      ['path', { 'd': 'M10 11v6' }],
      ['path', { 'd': 'M14 11v6' }],
      ['path', { 'd': 'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6' }],
      ['path', { 'd': 'M3 6h18' }],
      ['path', { 'd': 'M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2' }]
    ],
    'copy': [
      ['rect', { 'width': '14', 'height': '14', 'x': '8', 'y': '8', 'rx': '2', 'ry': '2' }],
      ['path', { 'd': 'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2' }]
    ],
    'rotate-cw': [
      ['path', { 'd': 'M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8' }],
      ['path', { 'd': 'M21 3v5h-5' }]
    ],
    'bot': [
      ['path', { 'd': 'M12 8V4H8' }],
      ['rect', { 'width': '16', 'height': '12', 'x': '4', 'y': '8', 'rx': '2' }],
      ['path', { 'd': 'M2 14h2' }],
      ['path', { 'd': 'M20 14h2' }],
      ['path', { 'd': 'M15 13v2' }],
      ['path', { 'd': 'M9 13v2' }]
    ],
    'book-open': [
      ['path', { 'd': 'M12 5v16' }],
      ['path', { 'd': 'M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z' }]
    ],
    'arrow-up-right': [
      ['path', { 'd': 'M7 7h10v10' }],
      ['path', { 'd': 'M7 17 17 7' }]
    ],
    'circle-dollar-sign': [
      ['circle', { 'cx': '12', 'cy': '12', 'r': '10' }],
      ['path', { 'd': 'M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8' }],
      ['path', { 'd': 'M12 18V6' }]
    ],
    'car': [
      ['path', { 'd': 'M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2' }],
      ['circle', { 'cx': '7', 'cy': '17', 'r': '2' }],
      ['path', { 'd': 'M9 17h6' }],
      ['circle', { 'cx': '17', 'cy': '17', 'r': '2' }]
    ],
    'gift': [
      ['path', { 'd': 'M12 7v14' }],
      ['path', { 'd': 'M20 11v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8' }],
      ['path', { 'd': 'M7.5 7a1 1 0 0 1 0-5A4.8 8 0 0 1 12 7a4.8 8 0 0 1 4.5-5 1 1 0 0 1 0 5' }],
      ['rect', { 'x': '3', 'y': '7', 'width': '18', 'height': '4', 'rx': '1' }]
    ],
    'scan': [
      ['path', { 'd': 'M3 7V5a2 2 0 0 1 2-2h2' }],
      ['path', { 'd': 'M17 3h2a2 2 0 0 1 2 2v2' }],
      ['path', { 'd': 'M21 17v2a2 2 0 0 1-2 2h-2' }],
      ['path', { 'd': 'M7 21H5a2 2 0 0 1-2-2v-2' }]
    ],
    'shrink': [
      ['path', { 'd': 'm15 15 6 6m-6-6v4.8m0-4.8h4.8' }],
      ['path', { 'd': 'M9 19.8V15m0 0H4.2M9 15l-6 6' }],
      ['path', { 'd': 'M15 4.2V9m0 0h4.8M15 9l6-6' }],
      ['path', { 'd': 'M9 4.2V9m0 0H4.2M9 9 3 3' }]
    ],
    'camera': [
      ['path', { 'd': 'M13.997 4a2 2 0 0 1 1.76 1.05l.486.9A2 2 0 0 0 18.003 7H20a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h1.997a2 2 0 0 0 1.759-1.048l.489-.904A2 2 0 0 1 10.004 4z' }],
      ['circle', { 'cx': '12', 'cy': '13', 'r': '3' }]
    ],
    'folder-open': [
      ['path', { 'd': 'm6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2' }]
    ],
    'crosshair': [
      ['circle', { 'cx': '12', 'cy': '12', 'r': '10' }],
      ['line', { 'x1': '22', 'x2': '18', 'y1': '12', 'y2': '12' }],
      ['line', { 'x1': '6', 'x2': '2', 'y1': '12', 'y2': '12' }],
      ['line', { 'x1': '12', 'x2': '12', 'y1': '6', 'y2': '2' }],
      ['line', { 'x1': '12', 'x2': '12', 'y1': '22', 'y2': '18' }]
    ]
  };

  var CUSTOM = {
    'github': [
      ['path', { 'd': 'M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4' }],
      ['path', { 'd': 'M9 18c-4.51 2-5-2-7-2' }]
    ],
    'media-play': [['polygon', { 'points': '6 3 20 12 6 21 6 3' }]],
    'media-pause': [
      ['rect', { 'width': '4', 'height': '16', 'x': '6', 'y': '4' }],
      ['rect', { 'width': '4', 'height': '16', 'x': '14', 'y': '4' }]
    ],
    'media-stop': [['rect', { 'width': '14', 'height': '14', 'x': '5', 'y': '5', 'rx': '2' }]]
  };
  var SOLID = { 'media-play': 1, 'media-pause': 1, 'media-stop': 1 };

  var ICONS = {};
  Object.keys(LUCIDE).forEach(function (name) { ICONS[name] = { nodes: LUCIDE[name], solid: false }; });
  Object.keys(CUSTOM).forEach(function (name) { ICONS[name] = { nodes: CUSTOM[name], solid: !!SOLID[name] }; });

  function attrsToStr(map) {
    return Object.keys(map).map(function (k) { return k + '="' + map[k] + '"'; }).join(' ');
  }

  // 渲染为 SVG 标记字符串（供 innerHTML / 模板拼接）。尺寸、类名等由 CSS 控制；
  // extraAttrs 可覆写 svg 根属性（如 { class: 'icon-play' } 供状态切换 CSS 选择）。
  function svg(name, extraAttrs) {
    var def = ICONS[name];
    if (!def) throw new Error('MRAIcons: 未知图标 ' + name);
    var base = def.solid ? SOLID_SVG : BASE_SVG;
    if (extraAttrs) base = Object.assign({}, base, extraAttrs);
    var root = attrsToStr(base);
    var body = def.nodes.map(function (n) {
      return '<' + n[0] + ' ' + attrsToStr(n[1]) + '/>';
    }).join('');
    return '<svg ' + root + '>' + body + '</svg>';
  }

  // 返回 IconNode 数据（供 morphicons 的 <morph-icon> 元素经 property 赋值做变形动画；
  // 返回的是内部数组引用，调用方不得修改）。
  function node(name) {
    var def = ICONS[name];
    if (!def) throw new Error('MRAIcons: 未知图标 ' + name);
    if (def.solid) throw new Error('MRAIcons: 实心图标 ' + name + ' 不支持变形动画');
    return def.nodes;
  }

  // 把静态 HTML 里的 <i data-icon="名称"> 占位元素整体替换为对应 <svg>；
  // 占位元素上除 data-icon 外的属性（style、class 等）原样搬到 svg 上。
  function hydrate(root) {
    (root || document).querySelectorAll('i[data-icon]').forEach(function (el) {
      var tpl = document.createElement('template');
      tpl.innerHTML = svg(el.getAttribute('data-icon'));
      var svgEl = tpl.content.firstElementChild;
      for (var i = 0; i < el.attributes.length; i++) {
        var attr = el.attributes[i];
        if (attr.name !== 'data-icon') svgEl.setAttribute(attr.name, attr.value);
      }
      el.replaceWith(svgEl);
    });
  }

  window.MRAIcons = { svg: svg, node: node, hydrate: hydrate };
})();
