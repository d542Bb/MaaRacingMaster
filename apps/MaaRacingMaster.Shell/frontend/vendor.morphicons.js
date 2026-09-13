// morphicons v1.7.1 —— vendor 摊平版（供 file:// 直载的经典脚本环境使用）
// 上游：https://github.com/guillermolg00/morphicons （MIT，原文见本文件头下方注释块；
//   汇总声明见仓库 THIRD_PARTY_LICENSES.md 第四节）
// 本文件由上游 dist 的 5 个 ESM chunk（normalize/spring/dom/controller/element）机械摊平：
// 仅移除 import/export 语句并以 IIFE 作用域重建依赖绑定，未改动任何函数体。
// 升级版本时重新执行同一摊平流程，勿手改函数体。
//
// ----- 以下为上游 LICENSE 原文（MIT）-----
// MIT License
//
// Copyright (c) 2026 Guillermo
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.
// ----- 上游 LICENSE 原文结束 -----
(function () {
  'use strict';
  // ---------- normalize chunk ----------
  const mc_normalize = (() => {
  //#region src/core/parse.ts
  const COMMANDS = "MmLlHhVvCcSsQqTtAaZz";
  function parsePath(d) {
  	const subs = [];
  	const n = d.length;
  	let i = 0;
  	let cx = 0;
  	let cy = 0;
  	let sx = 0;
  	let sy = 0;
  	let cur = null;
  	let cmd = "";
  	let px = 0;
  	let py = 0;
  	let prev = "";
  	let started = false;
  	const err = (msg) => {
  		throw new Error(`morphicons: ${msg} at d[${i}]`);
  	};
  	const isDigit = (c) => c >= 48 && c <= 57;
  	const skip = () => {
  		while (i < n) {
  			const c = d.charCodeAt(i);
  			if (c === 32 || c === 9 || c === 10 || c === 13 || c === 12 || c === 44) i++;
  			else break;
  		}
  	};
  	const num = () => {
  		skip();
  		const start = i;
  		if (i < n && (d[i] === "+" || d[i] === "-")) i++;
  		let dig = false;
  		while (i < n && isDigit(d.charCodeAt(i))) {
  			i++;
  			dig = true;
  		}
  		if (i < n && d[i] === ".") {
  			i++;
  			while (i < n && isDigit(d.charCodeAt(i))) {
  				i++;
  				dig = true;
  			}
  		}
  		if (!dig) err("expected number");
  		if (i < n && (d[i] === "e" || d[i] === "E")) {
  			const save = i;
  			i++;
  			if (i < n && (d[i] === "+" || d[i] === "-")) i++;
  			let ed = false;
  			while (i < n && isDigit(d.charCodeAt(i))) {
  				i++;
  				ed = true;
  			}
  			if (!ed) i = save;
  		}
  		return Number(d.slice(start, i));
  	};
  	const flag = () => {
  		skip();
  		const c = d[i];
  		if (c === "0" || c === "1") {
  			i++;
  			return c === "1" ? 1 : 0;
  		}
  		return err("expected arc flag (0|1)");
  	};
  	const open = () => {
  		if (!started) err("path must start with M/m");
  		if (!cur) {
  			cur = {
  				x0: cx,
  				y0: cy,
  				segs: [],
  				closed: false
  			};
  			subs.push(cur);
  		}
  		return cur;
  	};
  	let rel = false;
  	const nx = () => num() + (rel ? cx : 0);
  	const ny = () => num() + (rel ? cy : 0);
  	while (true) {
  		skip();
  		if (i >= n) break;
  		const ch = d[i];
  		if (COMMANDS.includes(ch)) {
  			cmd = ch;
  			i++;
  		} else if (cmd === "") err("path must start with M/m");
  		else if (cmd === "M") cmd = "L";
  		else if (cmd === "m") cmd = "l";
  		else if (cmd === "Z" || cmd === "z") err("stray data after Z");
  		rel = cmd >= "a";
  		switch (rel ? cmd.toUpperCase() : cmd) {
  			case "M": {
  				started = true;
  				const x = nx();
  				const y = ny();
  				cx = x;
  				cy = y;
  				sx = x;
  				sy = y;
  				cur = {
  					x0: x,
  					y0: y,
  					segs: [],
  					closed: false
  				};
  				subs.push(cur);
  				prev = "";
  				break;
  			}
  			case "L": {
  				const x = nx();
  				const y = ny();
  				open().segs.push([
  					"L",
  					x,
  					y
  				]);
  				cx = x;
  				cy = y;
  				prev = "";
  				break;
  			}
  			case "H": {
  				const x = nx();
  				open().segs.push([
  					"L",
  					x,
  					cy
  				]);
  				cx = x;
  				prev = "";
  				break;
  			}
  			case "V": {
  				const y = ny();
  				open().segs.push([
  					"L",
  					cx,
  					y
  				]);
  				cy = y;
  				prev = "";
  				break;
  			}
  			case "C":
  			case "S": {
  				let x1;
  				let y1;
  				if (cmd === "C" || cmd === "c") {
  					x1 = nx();
  					y1 = ny();
  				} else {
  					x1 = prev === "C" ? 2 * cx - px : cx;
  					y1 = prev === "C" ? 2 * cy - py : cy;
  				}
  				const x2 = nx();
  				const y2 = ny();
  				const x = nx();
  				const y = ny();
  				open().segs.push([
  					"C",
  					x1,
  					y1,
  					x2,
  					y2,
  					x,
  					y
  				]);
  				px = x2;
  				py = y2;
  				cx = x;
  				cy = y;
  				prev = "C";
  				break;
  			}
  			case "Q":
  			case "T": {
  				let x1;
  				let y1;
  				if (cmd === "Q" || cmd === "q") {
  					x1 = nx();
  					y1 = ny();
  				} else {
  					x1 = prev === "Q" ? 2 * cx - px : cx;
  					y1 = prev === "Q" ? 2 * cy - py : cy;
  				}
  				const x = nx();
  				const y = ny();
  				open().segs.push([
  					"Q",
  					x1,
  					y1,
  					x,
  					y
  				]);
  				px = x1;
  				py = y1;
  				cx = x;
  				cy = y;
  				prev = "Q";
  				break;
  			}
  			case "A": {
  				const rx = num();
  				const ry = num();
  				const rot = num();
  				const large = flag();
  				const sweep = flag();
  				const x = nx();
  				const y = ny();
  				open().segs.push([
  					"A",
  					rx,
  					ry,
  					rot,
  					large,
  					sweep,
  					x,
  					y
  				]);
  				cx = x;
  				cy = y;
  				prev = "";
  				break;
  			}
  			case "Z":
  				if (cur) {
  					cur.closed = true;
  					cur = null;
  				}
  				cx = sx;
  				cy = sy;
  				prev = "";
  				break;
  			default: err(`unsupported command "${cmd}"`);
  		}
  	}
  	return subs.filter((s) => s.segs.length > 0);
  }
  //#endregion
  //#region src/core/serialize.ts
  function fmt(v) {
  	return String(Math.round(v * 100) / 100);
  }
  /** Sampled subpaths → polyline `d` attribute. `closed[k]` appends Z to
  *  subpath k (closed loops in flight); without flags everything is open. */
  function serialize(subs, closed) {
  	let d = "";
  	for (let k = 0; k < subs.length; k++) {
  		const o = subs[k];
  		const n = o.length / 2;
  		d += `M${fmt(o[0])} ${fmt(o[1])}`;
  		for (let i = 1; i < n; i++) d += `L${fmt(o[2 * i])} ${fmt(o[2 * i + 1])}`;
  		if (closed?.[k]) d += "Z";
  	}
  	return d;
  }
  function fmtCanon(v) {
  	return String(Math.round(v * 1e4) / 1e4);
  }
  /** Cubic subpaths → canonical `d`, quantized to 4 decimals (engine-stable
  *  bytes; see fmtCanon). */
  function cubicsToPathD(paths) {
  	let d = "";
  	for (const { pts, closed } of paths) {
  		d += `M${fmtCanon(pts[0])} ${fmtCanon(pts[1])}`;
  		for (let i = 2; i < pts.length; i += 6) d += `C${fmtCanon(pts[i])} ${fmtCanon(pts[i + 1])} ${fmtCanon(pts[i + 2])} ${fmtCanon(pts[i + 3])} ${fmtCanon(pts[i + 4])} ${fmtCanon(pts[i + 5])}`;
  		if (closed) d += "Z";
  	}
  	return d;
  }
  //#endregion
  //#region src/core/normalize.ts
  /** Control-point offset for a quarter circle: (4/3)·tan(π/8) ≈ 0.5523. */
  const KAPPA = 4 / 3 * Math.tan(Math.PI / 8);
  const TAU = 2 * Math.PI;
  function builder(x0, y0) {
  	const pts = [x0, y0];
  	let cx = x0;
  	let cy = y0;
  	const cubic = (x1, y1, x2, y2, x, y) => {
  		pts.push(x1, y1, x2, y2, x, y);
  		cx = x;
  		cy = y;
  	};
  	const line = (x, y) => {
  		if (Math.abs(x - cx) < 1e-12 && Math.abs(y - cy) < 1e-12) return;
  		cubic(cx + (x - cx) / 3, cy + (y - cy) / 3, cx + 2 * (x - cx) / 3, cy + 2 * (y - cy) / 3, x, y);
  	};
  	const quad = (x1, y1, x, y) => {
  		cubic(cx + 2 / 3 * (x1 - cx), cy + 2 / 3 * (y1 - cy), x + 2 / 3 * (x1 - x), y + 2 / 3 * (y1 - y), x, y);
  	};
  	const arc = (rx0, ry0, rotDeg, large, sweep, x, y) => {
  		const x1 = cx;
  		const y1 = cy;
  		if (Math.abs(x - x1) < 1e-12 && Math.abs(y - y1) < 1e-12) return;
  		let rx = Math.abs(rx0);
  		let ry = Math.abs(ry0);
  		if (rx < 1e-12 || ry < 1e-12) {
  			line(x, y);
  			return;
  		}
  		const phi = rotDeg * Math.PI / 180;
  		const cosP = Math.cos(phi);
  		const sinP = Math.sin(phi);
  		const hx = (x1 - x) / 2;
  		const hy = (y1 - y) / 2;
  		const x1p = cosP * hx + sinP * hy;
  		const y1p = -sinP * hx + cosP * hy;
  		const lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry);
  		if (lam > 1) {
  			const s = Math.sqrt(lam);
  			rx *= s;
  			ry *= s;
  		}
  		const rx2 = rx * rx;
  		const ry2 = ry * ry;
  		const xp2 = x1p * x1p;
  		const yp2 = y1p * y1p;
  		let rad = (rx2 * ry2 - rx2 * yp2 - ry2 * xp2) / (rx2 * yp2 + ry2 * xp2);
  		if (rad < 0) rad = 0;
  		const co = (large === sweep ? -1 : 1) * Math.sqrt(rad);
  		const cxp = co * rx * y1p / ry;
  		const cyp = -co * ry * x1p / rx;
  		const ccx = cosP * cxp - sinP * cyp + (x1 + x) / 2;
  		const ccy = sinP * cxp + cosP * cyp + (y1 + y) / 2;
  		const th1 = Math.atan2((y1p - cyp) / ry, (x1p - cxp) / rx);
  		let dth = Math.atan2((-y1p - cyp) / ry, (-x1p - cxp) / rx) - th1;
  		if (sweep === 0 && dth > 0) dth -= TAU;
  		else if (sweep === 1 && dth < 0) dth += TAU;
  		const slices = Math.max(1, Math.ceil(Math.abs(dth) / (Math.PI / 2) - 1e-9));
  		const delta = dth / slices;
  		const alpha = 4 / 3 * Math.tan(delta / 4);
  		const ex = (t) => ccx + rx * Math.cos(t) * cosP - ry * Math.sin(t) * sinP;
  		const ey = (t) => ccy + rx * Math.cos(t) * sinP + ry * Math.sin(t) * cosP;
  		const dx = (t) => -rx * Math.sin(t) * cosP - ry * Math.cos(t) * sinP;
  		const dy = (t) => -rx * Math.sin(t) * sinP + ry * Math.cos(t) * cosP;
  		let t0 = th1;
  		let p0x = x1;
  		let p0y = y1;
  		for (let s = 1; s <= slices; s++) {
  			const t1 = th1 + delta * s;
  			const p1x = s === slices ? x : ex(t1);
  			const p1y = s === slices ? y : ey(t1);
  			cubic(p0x + alpha * dx(t0), p0y + alpha * dy(t0), p1x - alpha * dx(t1), p1y - alpha * dy(t1), p1x, p1y);
  			t0 = t1;
  			p0x = p1x;
  			p0y = p1y;
  		}
  	};
  	const finish = (closed) => {
  		if (closed) line(pts[0], pts[1]);
  		if (pts.length < 8) return null;
  		return {
  			pts: Float64Array.from(pts),
  			closed
  		};
  	};
  	return [
  		cubic,
  		line,
  		quad,
  		arc,
  		finish
  	];
  }
  function lowerSubpath(raw) {
  	const [cubic, line, quad, arc, finish] = builder(raw.x0, raw.y0);
  	for (const s of raw.segs) switch (s[0]) {
  		case "L":
  			line(s[1], s[2]);
  			break;
  		case "C":
  			cubic(s[1], s[2], s[3], s[4], s[5], s[6]);
  			break;
  		case "Q":
  			quad(s[1], s[2], s[3], s[4]);
  			break;
  		case "A": arc(s[1], s[2], s[3], s[4], s[5], s[6], s[7]);
  	}
  	return finish(raw.closed);
  }
  function attrNum(attrs, key, fallback = 0) {
  	const v = attrs[key];
  	if (v === void 0) return fallback;
  	const x = typeof v === "number" ? v : Number(v);
  	return Number.isFinite(x) ? x : fallback;
  }
  function parsePoints(v) {
  	const s = String(v ?? "").trim();
  	if (!s) return [];
  	const nums = s.split(/[\s,]+/).map(Number);
  	if (nums.some((x) => !Number.isFinite(x))) throw new Error(`morphicons: invalid points: "${s}"`);
  	return nums;
  }
  function polyPath(nums, closed) {
  	if (nums.length < 4) return null;
  	const [, line, , , finish] = builder(nums[0], nums[1]);
  	for (let i = 2; i + 1 < nums.length; i += 2) line(nums[i], nums[i + 1]);
  	return finish(closed);
  }
  function ellipsePath(cx, cy, rx, ry) {
  	if (rx < 1e-12 || ry < 1e-12) return null;
  	const kx = KAPPA * rx;
  	const ky = KAPPA * ry;
  	const e = cx + rx;
  	const w = cx - rx;
  	const s = cy + ry;
  	const n = cy - ry;
  	const [cubic, , , , finish] = builder(e, cy);
  	cubic(e, cy + ky, cx + kx, s, cx, s);
  	cubic(cx - kx, s, w, cy + ky, w, cy);
  	cubic(w, cy - ky, cx - kx, n, cx, n);
  	cubic(cx + kx, n, e, cy - ky, e, cy);
  	return finish(true);
  }
  function rectPath(attrs) {
  	const x = attrNum(attrs, "x");
  	const y = attrNum(attrs, "y");
  	const w = attrNum(attrs, "width");
  	const h = attrNum(attrs, "height");
  	if (w < 1e-12 || h < 1e-12) return null;
  	let rx = attrNum(attrs, "rx", NaN);
  	let ry = attrNum(attrs, "ry", NaN);
  	if (Number.isNaN(rx)) rx = Number.isNaN(ry) ? 0 : ry;
  	if (Number.isNaN(ry)) ry = rx;
  	rx = Math.min(Math.max(rx, 0), w / 2);
  	ry = Math.min(Math.max(ry, 0), h / 2);
  	if (rx < 1e-12 || ry < 1e-12) return polyPath([
  		x,
  		y,
  		x + w,
  		y,
  		x + w,
  		y + h,
  		x,
  		y + h
  	], true);
  	const xa = x + rx;
  	const xb = x + w - rx;
  	const xr = x + w;
  	const ya = y + ry;
  	const yb = y + h - ry;
  	const yd = y + h;
  	const kx = KAPPA * rx;
  	const ky = KAPPA * ry;
  	const [cubic, line, , , finish] = builder(xa, y);
  	line(xb, y);
  	cubic(xb + kx, y, xr, ya - ky, xr, ya);
  	line(xr, yb);
  	cubic(xr, yb + ky, xb + kx, yd, xb, yd);
  	line(xa, yd);
  	cubic(xa - kx, yd, x, yb + ky, x, yb);
  	line(x, ya);
  	cubic(x, ya - ky, xa - kx, y, xa, y);
  	return finish(true);
  }
  /** Icon (IconNode or `d` string) → list of cubic subpaths. */
  function iconToCubics(input) {
  	const out = [];
  	const push = (p) => {
  		if (p) out.push(p);
  	};
  	if (typeof input === "string") {
  		for (const s of parsePath(input)) push(lowerSubpath(s));
  		return out;
  	}
  	for (const [tag, attrs] of input) switch (tag) {
  		case "path":
  			for (const s of parsePath(String(attrs.d ?? ""))) push(lowerSubpath(s));
  			break;
  		case "line": {
  			const [, line, , , finish] = builder(attrNum(attrs, "x1"), attrNum(attrs, "y1"));
  			line(attrNum(attrs, "x2"), attrNum(attrs, "y2"));
  			push(finish(false));
  			break;
  		}
  		case "circle": {
  			const r = attrNum(attrs, "r");
  			push(ellipsePath(attrNum(attrs, "cx"), attrNum(attrs, "cy"), r, r));
  			break;
  		}
  		case "ellipse":
  			push(ellipsePath(attrNum(attrs, "cx"), attrNum(attrs, "cy"), attrNum(attrs, "rx"), attrNum(attrs, "ry")));
  			break;
  		case "rect":
  			push(rectPath(attrs));
  			break;
  		case "polyline":
  			push(polyPath(parsePoints(attrs.points), false));
  			break;
  		case "polygon":
  			push(polyPath(parsePoints(attrs.points), true));
  			break;
  		default: throw new Error(`morphicons: unsupported tag <${tag}>`);
  	}
  	return out;
  }
  function parseViewBox(vb) {
  	const v = typeof vb === "number" ? [
  		0,
  		0,
  		vb,
  		vb
  	] : typeof vb === "string" ? vb.trim().split(/[\s,]+/).map(Number) : vb;
  	const [minX, minY, w, h] = v;
  	if (v.length !== 4 || !(w > 0) || !(h > 0) || !Number.isFinite(minX) || !Number.isFinite(minY)) throw new Error(`morphicons: invalid viewBox "${String(vb)}"`);
  	return [
  		minX,
  		minY,
  		w,
  		h
  	];
  }
  /** Re-grids an icon drawn on `viewBox` onto the shared `grid` (24 by default),
  *  centred and preserving aspect ratio — the SVG `xMidYMid meet` rule.
  *
  *  Both endpoints of a morph must live on the same coordinate space. Lucide and
  *  Tabler already draw on 24×24; packs on 20 (Heroicons solid) or 32 (Carbon)
  *  do not, and mixing them unfitted makes Procrustes read the scale/offset gap
  *  as rotation. Apply once at module scope (not per render) and pass the
  *  resulting `d` anywhere an icon is accepted. */
  function fitIcon(input, viewBox, grid = 24) {
  	const [minX, minY, w, h] = parseViewBox(viewBox);
  	const s = Math.min(grid / w, grid / h);
  	const tx = (grid - w * s) / 2 - minX * s;
  	const ty = (grid - h * s) / 2 - minY * s;
  	const paths = iconToCubics(input);
  	for (const { pts } of paths) for (let i = 0; i < pts.length; i += 2) {
  		pts[i] = pts[i] * s + tx;
  		pts[i + 1] = pts[i + 1] * s + ty;
  	}
  	return cubicsToPathD(paths);
  }
  //#endregion
    return { i: serialize, n: iconToCubics, r: cubicsToPathD, t: fitIcon };
  })();

  // ---------- spring chunk ----------
  const mc_spring = (() => {
    const { n: iconToCubics } = mc_normalize;
  //#region src/core/interpolate.ts
  /** Preallocated output buffers for a plan (zero allocation per frame). */
  function allocOutputs(plan) {
  	return plan.items.map(() => new Float64Array(2 * plan.n));
  }
  function interpPolar(plan, t, out) {
  	for (let k = 0; k < plan.items.length; k++) {
  		const it = plan.items[k];
  		const o = out[k];
  		const n = plan.n;
  		const s = Math.exp(it.lnSigma * t);
  		const ang = it.theta * t;
  		const cos = Math.cos(ang) * s;
  		const sin = Math.sin(ang) * s;
  		let cx;
  		let cy;
  		if (it.block) {
  			const [ox, oy] = it.block.off;
  			const [dx, dy] = it.block.drift;
  			cx = it.ca[0] + dx * t + (ox * cos - oy * sin - ox);
  			cy = it.ca[1] + dy * t + (ox * sin + oy * cos - oy);
  		} else {
  			cx = it.ca[0] + (it.cb[0] - it.ca[0]) * t;
  			cy = it.ca[1] + (it.cb[1] - it.ca[1]) * t;
  		}
  		for (let i = 0; i < n; i++) {
  			const px = it.aC[2 * i] + (it.bT[2 * i] - it.aC[2 * i]) * t;
  			const py = it.aC[2 * i + 1] + (it.bT[2 * i + 1] - it.aC[2 * i + 1]) * t;
  			o[2 * i] = cx + px * cos - py * sin;
  			o[2 * i + 1] = cy + px * sin + py * cos;
  		}
  	}
  }
  /** Raw coordinate lerp (same correspondence, no decomposition). */
  function interpLinear(plan, t, out) {
  	for (let k = 0; k < plan.items.length; k++) {
  		const it = plan.items[k];
  		const o = out[k];
  		const n = plan.n;
  		for (let i = 0; i < n; i++) {
  			o[2 * i] = it.a[2 * i] + (it.bO[2 * i] - it.a[2 * i]) * t;
  			o[2 * i + 1] = it.a[2 * i + 1] + (it.bO[2 * i + 1] - it.a[2 * i + 1]) * t;
  		}
  	}
  }
  //#endregion
  //#region src/core/plan.ts
  /** Weight of |ΔL| in the subpath pairing cost. */
  const LEN_WEIGHT = .35;
  /** λ of the minimal-rotation tie-break: score = res + λ·|θ|/π.
  *  It exists because shapes symmetric under inversion (lines) tie in
  *  residual for both traversal orientations yet produce different rotations. */
  const LAMBDA = .05;
  /** Global residual below which the whole icon counts as congruent and the
  *  plan shares (θ, σ) across all items (hybrid variant of Procrustes). */
  const GLOBAL_EPS = .005;
  /** Bounds for exhaustive matching; above them it falls back to greedy with
  *  repair. 8! = 40 320 permutations / 1e5 assignments — both sub-ms. */
  const PERM_MAX = 8;
  const SURJ_MAX = 1e5;
  function centroid(p) {
  	const n = p.length / 2;
  	let cx = 0;
  	let cy = 0;
  	for (let i = 0; i < n; i++) {
  		cx += p[2 * i];
  		cy += p[2 * i + 1];
  	}
  	return [cx / n, cy / n];
  }
  function polyLen(p) {
  	const n = p.length / 2;
  	let L = 0;
  	for (let i = 1; i < n; i++) L += Math.hypot(p[2 * i] - p[2 * i - 2], p[2 * i + 1] - p[2 * i - 1]);
  	return L;
  }
  function reversePts(p) {
  	const n = p.length / 2;
  	const out = new Float64Array(2 * n);
  	for (let i = 0; i < n; i++) {
  		out[2 * i] = p[2 * (n - 1 - i)];
  		out[2 * i + 1] = p[2 * (n - 1 - i) + 1];
  	}
  	return out;
  }
  /** Circular re-indexing of a loop: out[i] = p[(i+off) mod n]. Same point
  *  set, different cut point — the circular degree of freedom of closed paths. */
  function rotatePts(p, off) {
  	const n = p.length / 2;
  	const out = new Float64Array(2 * n);
  	for (let i = 0; i < n; i++) {
  		const j = (i + off) % n;
  		out[2 * i] = p[2 * j];
  		out[2 * i + 1] = p[2 * j + 1];
  	}
  	return out;
  }
  /** Optimal similarity (θ, σ) minimizing Σ|σ·R(θ)·(a−c_A) − (b−c_B)|².
  *  θ* = atan2(S_xy − S_yx, S_xx + S_yy); σ* by zero derivative.
  *  res = RMS residual normalized by b's energy (0 → same shape). */
  function procrustes(a, b, ca, cb) {
  	const n = a.length / 2;
  	let sxx = 0;
  	let sxy = 0;
  	let syx = 0;
  	let syy = 0;
  	let na = 0;
  	let nb = 0;
  	for (let i = 0; i < n; i++) {
  		const ax = a[2 * i] - ca[0];
  		const ay = a[2 * i + 1] - ca[1];
  		const bx = b[2 * i] - cb[0];
  		const by = b[2 * i + 1] - cb[1];
  		sxx += ax * bx;
  		syy += ay * by;
  		sxy += ax * by;
  		syx += ay * bx;
  		na += ax * ax + ay * ay;
  		nb += bx * bx + by * by;
  	}
  	const theta = Math.atan2(sxy - syx, sxx + syy);
  	const num = Math.cos(theta) * (sxx + syy) + Math.sin(theta) * (sxy - syx);
  	let sigma = na > 1e-12 ? num / na : 1;
  	if (!(sigma > 1e-6)) sigma = 1e-6;
  	const res2 = Math.max(0, sigma * sigma * na - 2 * sigma * num + nb);
  	const res = nb > 1e-12 ? Math.sqrt(res2 / nb) : 0;
  	return {
  		theta,
  		sigma,
  		res
  	};
  }
  /** Best index-to-index correspondence between a and b: tries both traversal
  *  directions and, if there is a closed loop, its N circular offsets,
  *  scoring with score = res + λ·|θ|/π. The freedom is applied to ONE cloud
  *  — the closed one (b if both are); varying both at once would be
  *  redundant. */
  function alignPair(aPts, bPts, aClosed = false, bClosed = false) {
  	const ca = centroid(aPts);
  	const cb = centroid(bPts);
  	const varyA = aClosed && !bClosed;
  	const base = varyA ? aPts : bPts;
  	const offs = aClosed || bClosed ? base.length / 2 : 1;
  	let bestScore = Number.POSITIVE_INFINITY;
  	let best = base;
  	let sim = {
  		theta: 0,
  		sigma: 1,
  		res: 0
  	};
  	for (let dir = 0; dir < 2; dir++) {
  		const walk = dir ? reversePts(base) : base;
  		for (let off = 0; off < offs; off++) {
  			const cand = off ? rotatePts(walk, off) : walk;
  			const s = varyA ? procrustes(cand, bPts, ca, cb) : procrustes(aPts, cand, ca, cb);
  			const score = s.res + LAMBDA * Math.abs(s.theta) / Math.PI;
  			if (score < bestScore) {
  				bestScore = score;
  				best = cand;
  				sim = s;
  			}
  		}
  	}
  	return varyA ? {
  		ca,
  		cb,
  		a: best,
  		b: bPts,
  		...sim
  	} : {
  		ca,
  		cb,
  		a: aPts,
  		b: best,
  		...sim
  	};
  }
  function costMatrix(A, B) {
  	const cbs = B.map(centroid);
  	const lbs = B.map(polyLen);
  	return A.map((a) => {
  		const ca = centroid(a);
  		const la = polyLen(a);
  		return cbs.map((cb, j) => Math.hypot(ca[0] - cb[0], ca[1] - cb[1]) + LEN_WEIGHT * Math.abs(la - lbs[j]));
  	});
  }
  function bestPermutation(C) {
  	const n = C.length;
  	if (n > PERM_MAX) {
  		const pairs = [];
  		for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) pairs.push([
  			C[i][j],
  			i,
  			j
  		]);
  		pairs.sort((x, y) => x[0] - y[0]);
  		const out = new Array(n).fill(-1);
  		const used = new Array(n).fill(false);
  		for (const [, i, j] of pairs) if (out[i] < 0 && !used[j]) {
  			out[i] = j;
  			used[j] = true;
  		}
  		return out;
  	}
  	const idx = Array.from({ length: n }, (_, i) => i);
  	let best = idx.slice();
  	let bc = Number.POSITIVE_INFINITY;
  	const perm = (arr, k, acc) => {
  		if (acc >= bc) return;
  		if (k === n) {
  			bc = acc;
  			best = arr.slice();
  			return;
  		}
  		for (let i = k; i < n; i++) {
  			[arr[k], arr[i]] = [arr[i], arr[k]];
  			perm(arr, k + 1, acc + C[k][arr[k]]);
  			[arr[k], arr[i]] = [arr[i], arr[k]];
  		}
  	};
  	perm(idx, 0, 0);
  	return best;
  }
  function bestSurjection(C) {
  	const B = C.length;
  	const S = C[0].length;
  	if (S ** B > SURJ_MAX) {
  		const f = C.map((row) => {
  			let m = 0;
  			for (let j = 1; j < row.length; j++) if (row[j] < row[m]) m = j;
  			return m;
  		});
  		const mult = new Array(S).fill(0);
  		for (const s of f) mult[s]++;
  		for (let s = 0; s < S; s++) {
  			if (mult[s] > 0) continue;
  			let bi = -1;
  			let bc = Number.POSITIVE_INFINITY;
  			for (let i = 0; i < B; i++) {
  				if (mult[f[i]] < 2) continue;
  				const extra = C[i][s] - C[i][f[i]];
  				if (extra < bc) {
  					bc = extra;
  					bi = i;
  				}
  			}
  			mult[f[bi]]--;
  			f[bi] = s;
  			mult[s]++;
  		}
  		return f;
  	}
  	let best = null;
  	let bc = Number.POSITIVE_INFINITY;
  	const f = new Array(B);
  	const mult = new Array(S).fill(0);
  	const rec = (i, acc, covered) => {
  		if (acc >= bc || S - covered > B - i) return;
  		if (i === B) {
  			bc = acc;
  			best = f.slice();
  			return;
  		}
  		for (let s = 0; s < S; s++) {
  			f[i] = s;
  			mult[s]++;
  			rec(i + 1, acc + C[i][s], covered + (mult[s] === 1 ? 1 : 0));
  			mult[s]--;
  		}
  	};
  	rec(0, 0, 0);
  	if (!best) throw new Error("morphicons: no valid surjection (B < S)");
  	return best;
  }
  function applyGlobal(items, n) {
  	const T = items.length * n;
  	const ga = new Float64Array(2 * T);
  	const gb = new Float64Array(2 * T);
  	items.forEach((it, k) => {
  		ga.set(it.a, 2 * n * k);
  		gb.set(it.bO, 2 * n * k);
  	});
  	const gca = centroid(ga);
  	const g = procrustes(ga, gb, gca, centroid(gb));
  	if (g.res >= GLOBAL_EPS) return;
  	const cos = Math.cos(-g.theta);
  	const sin = Math.sin(-g.theta);
  	const rc = Math.cos(g.theta);
  	const rs = Math.sin(g.theta);
  	for (const it of items) {
  		let e2 = 0;
  		let nb = 0;
  		for (let i = 0; i < n; i++) {
  			const bx = it.bO[2 * i] - it.cb[0];
  			const by = it.bO[2 * i + 1] - it.cb[1];
  			it.bT[2 * i] = (bx * cos - by * sin) / g.sigma;
  			it.bT[2 * i + 1] = (bx * sin + by * cos) / g.sigma;
  			const ex = g.sigma * (rc * it.aC[2 * i] - rs * it.aC[2 * i + 1]) - bx;
  			const ey = g.sigma * (rs * it.aC[2 * i] + rc * it.aC[2 * i + 1]) - by;
  			e2 += ex * ex + ey * ey;
  			nb += bx * bx + by * by;
  		}
  		it.theta = g.theta;
  		it.lnSigma = Math.log(g.sigma);
  		it.res = nb > 1e-12 ? Math.sqrt(e2 / nb) : 0;
  		const s1 = Math.exp(it.lnSigma);
  		const c1 = Math.cos(it.theta) * s1;
  		const n1 = Math.sin(it.theta) * s1;
  		const ox = it.ca[0] - gca[0];
  		const oy = it.ca[1] - gca[1];
  		const rx = ox * c1 - oy * n1 - ox;
  		const ry = ox * n1 + oy * c1 - oy;
  		it.block = {
  			off: [ox, oy],
  			drift: [it.cb[0] - it.ca[0] - rx, it.cb[1] - it.ca[1] - ry]
  		};
  	}
  }
  /** Builds the morph plan between two lists of sampled subpaths. The plan is
  *  cacheable and serializable; it accepts any list — including intermediate
  *  shapes (interruptions). */
  function buildPlan(srcSubs, dstSubs) {
  	const p = srcSubs.length;
  	const q = dstSubs.length;
  	if (p === 0 || q === 0) throw new Error("morphicons: icon has no subpaths");
  	const A = srcSubs.map((s) => s.pts);
  	const B = dstSubs.map((s) => s.pts);
  	const pairs = [];
  	if (p === q) {
  		const perm = bestPermutation(costMatrix(A, B));
  		for (let i = 0; i < p; i++) pairs.push([i, perm[i]]);
  	} else if (p < q) {
  		const f = bestSurjection(costMatrix(B, A));
  		for (let j = 0; j < q; j++) pairs.push([f[j], j]);
  	} else {
  		const f = bestSurjection(costMatrix(A, B));
  		for (let i = 0; i < p; i++) pairs.push([i, f[i]]);
  	}
  	const n = A[0].length / 2;
  	const items = pairs.map(([si, di]) => {
  		const al = alignPair(A[si], B[di], srcSubs[si].closed, dstSubs[di].closed);
  		const a = al.a;
  		const aC = new Float64Array(2 * n);
  		const bT = new Float64Array(2 * n);
  		const bO = new Float64Array(2 * n);
  		const cos = Math.cos(-al.theta);
  		const sin = Math.sin(-al.theta);
  		for (let i = 0; i < n; i++) {
  			aC[2 * i] = a[2 * i] - al.ca[0];
  			aC[2 * i + 1] = a[2 * i + 1] - al.ca[1];
  			const bx = al.b[2 * i] - al.cb[0];
  			const by = al.b[2 * i + 1] - al.cb[1];
  			bT[2 * i] = (bx * cos - by * sin) / al.sigma;
  			bT[2 * i + 1] = (bx * sin + by * cos) / al.sigma;
  			bO[2 * i] = al.b[2 * i];
  			bO[2 * i + 1] = al.b[2 * i + 1];
  		}
  		return {
  			a,
  			aC,
  			bT,
  			bO,
  			ca: al.ca,
  			cb: al.cb,
  			theta: al.theta,
  			lnSigma: Math.log(al.sigma),
  			res: al.res,
  			closed: srcSubs[si].closed && dstSubs[di].closed,
  			block: null
  		};
  	});
  	if (items.length > 1) applyGlobal(items, n);
  	return {
  		items,
  		n
  	};
  }
  //#endregion
  //#region src/core/resample.ts
  /** Default angular threshold for a segment joint to count as a corner. */
  const CORNER_THRESHOLD = Math.PI / 8;
  const GX = [
  	.18343464249564978,
  	.525532409916329,
  	.7966664774136267,
  	.9602898564975363
  ];
  const GW = [
  	.362683783378362,
  	.31370664587788727,
  	.22238103445337448,
  	.10122853629037626
  ];
  function speed(p, k, t) {
  	const i = 6 * k;
  	const u = 1 - t;
  	const c0 = 3 * u * u;
  	const c1 = 6 * u * t;
  	const c2 = 3 * t * t;
  	const dx = c0 * (p[i + 2] - p[i]) + c1 * (p[i + 4] - p[i + 2]) + c2 * (p[i + 6] - p[i + 4]);
  	const dy = c0 * (p[i + 3] - p[i + 1]) + c1 * (p[i + 5] - p[i + 3]) + c2 * (p[i + 7] - p[i + 5]);
  	return Math.hypot(dx, dy);
  }
  function segLen(p, k, t1 = 1) {
  	const half = t1 / 2;
  	let s = 0;
  	for (let j = 0; j < 4; j++) s += GW[j] * (speed(p, k, half + half * GX[j]) + speed(p, k, half - half * GX[j]));
  	return s * half;
  }
  function point(p, k, t, out, o) {
  	const i = 6 * k;
  	const u = 1 - t;
  	const b0 = u * u * u;
  	const b1 = 3 * u * u * t;
  	const b2 = 3 * u * t * t;
  	const b3 = t * t * t;
  	out[o] = b0 * p[i] + b1 * p[i + 2] + b2 * p[i + 4] + b3 * p[i + 6];
  	out[o + 1] = b0 * p[i + 1] + b1 * p[i + 3] + b2 * p[i + 5] + b3 * p[i + 7];
  }
  function tangent(p, k, atEnd) {
  	const i = 6 * k;
  	const b = atEnd ? i + 6 : i;
  	const s = atEnd ? -1 : 1;
  	for (const j of atEnd ? [
  		4,
  		2,
  		0
  	] : [
  		2,
  		4,
  		6
  	]) {
  		const dx = s * (p[i + j] - p[b]);
  		const dy = s * (p[i + j + 1] - p[b + 1]);
  		if (dx * dx + dy * dy > 1e-18) return [dx, dy];
  	}
  	return null;
  }
  /** Segment boundaries (index of the segment starting at the corner) whose
  *  tangent discontinuity exceeds the threshold. For closed paths this
  *  includes the closing joint (boundary = first active segment). */
  function detectCorners(path, threshold = CORNER_THRESHOLD) {
  	const p = path.pts;
  	const m = (p.length / 2 - 1) / 3;
  	const active = [];
  	for (let k = 0; k < m; k++) if (segLen(p, k) > 1e-9) active.push(k);
  	if (active.length === 0) return [];
  	const corners = /* @__PURE__ */ new Set();
  	const test = (a, b) => {
  		const u = tangent(p, a, true);
  		const v = tangent(p, b, false);
  		if (!u || !v) return;
  		if (Math.abs(Math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])) > threshold) corners.add(b);
  	};
  	for (let j = 0; j + 1 < active.length; j++) test(active[j], active[j + 1]);
  	if (path.closed && active.length > 1) test(active[active.length - 1], active[0]);
  	return [...corners].sort((a, b) => a - b);
  }
  function invert(p, k, s, ls) {
  	if (s <= 0) return 0;
  	if (s >= ls) return 1;
  	let lo = 0;
  	let hi = 1;
  	let t = s / ls;
  	for (let it = 0; it < 12; it++) {
  		const f = segLen(p, k, t) - s;
  		if (Math.abs(f) < 1e-10 * ls + 1e-14) break;
  		if (f > 0) hi = t;
  		else lo = t;
  		const sp = speed(p, k, t);
  		let nt = sp > 1e-12 ? t - f / sp : (lo + hi) / 2;
  		if (!(nt > lo && nt < hi)) nt = (lo + hi) / 2;
  		t = nt;
  	}
  	return t;
  }
  /** Samples a cubic subpath at N points equidistant by arc length, anchoring
  *  corners and endpoints as exact samples. Returns Float64Array(2N). Closed
  *  paths distribute N intervals around the loop (without duplicating the
  *  first point); the circular start-point freedom is resolved by the plan's
  *  circular correspondence. */
  function resamplePath(path, N = 64, cornerThreshold = CORNER_THRESHOLD) {
  	const p = path.pts;
  	const m = (p.length / 2 - 1) / 3;
  	const out = new Float64Array(2 * N);
  	const fill = () => {
  		for (let i = 0; i < N; i++) {
  			out[2 * i] = p[0];
  			out[2 * i + 1] = p[1];
  		}
  		return out;
  	};
  	if (m < 1) return fill();
  	const lens = new Array(m);
  	let L = 0;
  	for (let k = 0; k < m; k++) {
  		lens[k] = segLen(p, k);
  		L += lens[k];
  	}
  	if (L < 1e-12) return fill();
  	const cs = detectCorners(path, cornerThreshold);
  	const anchors = path.closed ? cs.length > 0 ? cs : [0] : [.../* @__PURE__ */ new Set([
  		0,
  		...cs,
  		m
  	])].sort((a, b) => a - b);
  	const runs = [];
  	if (path.closed) for (let j = 0; j < anchors.length; j++) {
  		const a = anchors[j];
  		const b = j + 1 < anchors.length ? anchors[j + 1] : anchors[0] + m;
  		runs.push([a, b]);
  	}
  	else for (let j = 0; j + 1 < anchors.length; j++) runs.push([anchors[j], anchors[j + 1]]);
  	const rl = runs.map(([a, b]) => {
  		let s = 0;
  		for (let k = a; k < b; k++) s += lens[k % m];
  		return s;
  	});
  	const intervals = path.closed ? N : N - 1;
  	if (runs.length > intervals) throw new Error(`morphicons: N=${N} too small (${runs.length} runs)`);
  	const total = rl.reduce((a, b) => a + b, 0) || 1;
  	const ideal = rl.map((l) => intervals * l / total);
  	const counts = ideal.map((q) => Math.max(1, Math.floor(q)));
  	let R = intervals - counts.reduce((a, b) => a + b, 0);
  	if (R > 0) {
  		const order = ideal.map((q, idx) => [Math.round((q - Math.floor(q)) * 1e9), idx]).sort((a, b) => b[0] - a[0] || a[1] - b[1]);
  		for (let j = 0; j < R; j++) counts[order[j % counts.length][1]]++;
  	}
  	while (R < 0) {
  		let bi = 0;
  		for (let idx = 1; idx < counts.length; idx++) if (counts[idx] > counts[bi]) bi = idx;
  		if (counts[bi] <= 1) break;
  		counts[bi]--;
  		R++;
  	}
  	let w = 0;
  	for (let r = 0; r < runs.length; r++) {
  		const [k0, k1] = runs[r];
  		const cnt = counts[r];
  		const Lr = rl[r];
  		const vi = 6 * (k0 % m);
  		out[2 * w] = p[vi];
  		out[2 * w + 1] = p[vi + 1];
  		w++;
  		let seg = k0;
  		let acc = 0;
  		for (let j = 1; j < cnt; j++) {
  			const target = Lr * j / cnt;
  			while (seg < k1 - 1 && acc + lens[seg % m] < target) {
  				acc += lens[seg % m];
  				seg++;
  			}
  			const k = seg % m;
  			const ls = lens[k];
  			point(p, k, ls > 1e-12 ? invert(p, k, target - acc, ls) : 0, out, 2 * w);
  			w++;
  		}
  	}
  	if (!path.closed) {
  		const vi = 6 * m;
  		out[2 * w] = p[vi];
  		out[2 * w + 1] = p[vi + 1];
  	}
  	return out;
  }
  /** Full input pipeline: icon → cubics → sampled subpaths with their
  *  topology (the plan needs to know which subpaths are closed loops). */
  function resampleIcon(input, N = 64) {
  	return iconToCubics(input).map((path) => ({
  		pts: resamplePath(path, N),
  		closed: path.closed
  	}));
  }
  //#endregion
  //#region src/core/spring.ts
  var Spring = class {
  	x = 1;
  	v = 0;
  	k = 250;
  	c = 24;
  	config(k, c) {
  		this.k = k;
  		this.c = c;
  	}
  	/** Starts (or restarts mid-flight) preserving velocity. */
  	start() {
  		this.x = 0;
  		if (this.v > 14) this.v = 14;
  		if (this.v < -14) this.v = -14;
  	}
  	/** Advances dt seconds. Returns true on settle (|1−x| < 0.001 ∧ |v| < 0.02). */
  	step(dt) {
  		const steps = Math.max(1, Math.min(16, Math.ceil(dt / (1 / 240))));
  		const s = dt / steps;
  		for (let i = 0; i < steps; i++) {
  			const a = this.k * (1 - this.x) - this.c * this.v;
  			this.v += a * s;
  			this.x += this.v * s;
  		}
  		return Math.abs(1 - this.x) < .001 && Math.abs(this.v) < .02;
  	}
  };
  /** Spring presets (ζ = c/(2√k)) with the API's public names. */
  const SPRING_PRESETS = {
  	/** ζ = 1.00 — critically damped, no overshoot. */
  	smooth: {
  		k: 170,
  		c: 26
  	},
  	/** ζ = 0.73 — fast, subtle overshoot. */
  	snappy: {
  		k: 420,
  		c: 30
  	},
  	/** ζ = 0.40 — playful. */
  	bouncy: {
  		k: 300,
  		c: 14
  	}
  };
  //#endregion
    return { a: buildPlan, c: interpPolar, i: resamplePath, n: Spring, o: allocOutputs, r: resampleIcon, s: interpLinear, t: SPRING_PRESETS };
  })();

  // ---------- dom chunk ----------
  const mc_dom = (() => {
    const { a: buildPlan, c: interpPolar, n: Spring, o: allocOutputs, r: resampleIcon, t: SPRING_PRESETS } = mc_spring;
    const { i: serialize, n: iconToCubics, r: cubicsToPathD } = mc_normalize;
  //#region src/dom/index.ts
  const tickers = /* @__PURE__ */ new Set();
  let rafId = 0;
  let last = -1;
  function loop(ts) {
  	const dt = last < 0 ? 0 : Math.min(Math.max((ts - last) / 1e3, 0), .1);
  	last = ts;
  	for (const tick of [...tickers]) tick(dt);
  	if (tickers.size > 0) rafId = requestAnimationFrame(loop);
  	else {
  		rafId = 0;
  		last = -1;
  	}
  }
  function addTicker(tick) {
  	tickers.add(tick);
  	if (rafId === 0) {
  		last = -1;
  		rafId = requestAnimationFrame(loop);
  	}
  }
  function removeTicker(tick) {
  	tickers.delete(tick);
  	if (tickers.size === 0 && rafId !== 0) {
  		cancelAnimationFrame(rafId);
  		rafId = 0;
  		last = -1;
  	}
  }
  const samples = /* @__PURE__ */ new WeakMap();
  const canon = /* @__PURE__ */ new WeakMap();
  const plans = /* @__PURE__ */ new WeakMap();
  function sampledOf(icon) {
  	if (typeof icon === "string") return resampleIcon(icon);
  	let s = samples.get(icon);
  	if (!s) {
  		s = resampleIcon(icon);
  		samples.set(icon, s);
  	}
  	return s;
  }
  /** Canonical `d` of an icon: the input string verbatim, or the real cubics
  *  quantized to 4 decimals (the at-rest snap; engine-stable bytes so SSR
  *  hydration matches, see fmtCanon in core/serialize). Exported because it
  *  is what a binding renders at SSR/rest before any runtime exists. */
  function canonicalD(icon) {
  	if (typeof icon === "string") return icon;
  	let d = canon.get(icon);
  	if (!d) {
  		d = cubicsToPathD(iconToCubics(icon));
  		canon.set(icon, d);
  	}
  	return d;
  }
  function planBetween(src, dst) {
  	if (typeof src === "string" || typeof dst === "string") return buildPlan(sampledOf(src), sampledOf(dst));
  	let inner = plans.get(src);
  	if (!inner) {
  		inner = /* @__PURE__ */ new WeakMap();
  		plans.set(src, inner);
  	}
  	let p = inner.get(dst);
  	if (!p) {
  		p = buildPlan(sampledOf(src), sampledOf(dst));
  		inner.set(dst, p);
  	}
  	return p;
  }
  function resolveSpring(s) {
  	if (typeof s === "string") return SPRING_PRESETS[s];
  	const d = SPRING_PRESETS.snappy;
  	return {
  		k: s?.stiffness ?? d.k,
  		c: s?.damping ?? d.c
  	};
  }
  /** Creates the morph instance over a `<path>` and paints the initial icon. */
  function createMorph(el, icon, options) {
  	const spring = new Spring();
  	let reducedMotion = options?.reducedMotion ?? "never";
  	let target = icon;
  	let rest = true;
  	let plan = null;
  	let out = null;
  	let closed = null;
  	let t = 1;
  	let flying = false;
  	let dead = false;
  	el.setAttribute("d", canonicalD(icon));
  	const render = (tt) => {
  		const p = plan;
  		const o = out;
  		const cl = closed;
  		if (!p || !o || !cl) return;
  		t = tt;
  		interpPolar(p, tt, o);
  		el.setAttribute("d", serialize(o, cl));
  	};
  	const stop = () => {
  		if (!flying) return;
  		flying = false;
  		removeTicker(tick);
  	};
  	const tick = (dt) => {
  		const settled = spring.step(dt);
  		render(spring.x);
  		if (settled) {
  			stop();
  			settle();
  		}
  	};
  	const settle = () => {
  		rest = true;
  		plan = null;
  		out = null;
  		closed = null;
  		t = 1;
  		spring.x = 1;
  		spring.v = 0;
  		el.setAttribute("d", canonicalD(target));
  	};
  	/** The current shape as plan source: the at-rest icon, or the rendered
  	*  buffers (already N points per subpath). */
  	const snapshot = () => {
  		const p = plan;
  		const o = out;
  		if (rest || !p || !o) return sampledOf(target);
  		return o.map((buf, k) => ({
  			pts: Float64Array.from(buf),
  			closed: p.items[k].closed
  		}));
  	};
  	const retarget = (icon) => {
  		plan = rest ? planBetween(target, icon) : buildPlan(snapshot(), sampledOf(icon));
  		out = allocOutputs(plan);
  		closed = plan.items.map((it) => it.closed);
  		target = icon;
  		rest = false;
  	};
  	const setNow = (icon) => {
  		stop();
  		target = icon;
  		settle();
  	};
  	/** True when the policy says this morphTo must jump instead of flying. */
  	const motionOff = () => {
  		if (reducedMotion === "always") return true;
  		if (reducedMotion !== "user") return false;
  		if (typeof matchMedia === "undefined") return false;
  		return matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  	};
  	const seek = (icon, tt) => {
  		if (dead) return;
  		const reuse = !rest && plan !== null && icon === target;
  		stop();
  		spring.v = 0;
  		if (!reuse) retarget(icon);
  		render(tt);
  	};
  	return {
  		morphTo(icon, sp) {
  			if (dead) return;
  			if (icon === target && (rest || flying)) return;
  			if (motionOff()) {
  				setNow(icon);
  				return;
  			}
  			const { k, c } = resolveSpring(sp);
  			spring.config(k, c);
  			retarget(icon);
  			spring.start();
  			if (!flying) {
  				flying = true;
  				addTicker(tick);
  			}
  		},
  		set(icon) {
  			if (dead) return;
  			setNow(icon);
  		},
  		seek,
  		get progress() {
  			return rest ? 1 : t;
  		},
  		set progress(v) {
  			if (!dead) seek(target, v);
  		},
  		get reducedMotion() {
  			return reducedMotion;
  		},
  		set reducedMotion(v) {
  			reducedMotion = v;
  		},
  		destroy() {
  			stop();
  			dead = true;
  			plan = null;
  			out = null;
  			closed = null;
  		}
  	};
  }
  //#endregion
    return { canonicalD, createMorph };
  })();

  // ---------- controller chunk ----------
  const mc_controller = (() => {
    const { a: buildPlan, c: interpPolar, o: allocOutputs, r: resampleIcon } = mc_spring;
    const { i: serialize } = mc_normalize;
    const { canonicalD, createMorph } = mc_dom;
  //#region src/dom/controller.ts
  /** Frozen shape of the from→to pair at t, using the pure core (SSR-safe).
  *  At exact endpoints returns the canonical `d` (real curves, not polyline). */
  function frozenD(from, to, t) {
  	if (t <= 0) return canonicalD(from);
  	if (t >= 1) return canonicalD(to);
  	const plan = buildPlan(resampleIcon(from), resampleIcon(to));
  	const out = allocOutputs(plan);
  	interpPolar(plan, t, out);
  	return serialize(out, plan.items.map((it) => it.closed));
  }
  /** The initial d is a constant for every binding: computed once from the
  *  mount-time props (server and client produce the same string → hydration
  *  without mismatch) and from then on only the driver mutates it outside the
  *  template. */
  function computeInitialD({ icon, from, to, progress }) {
  	if (from !== void 0 && to !== void 0) return frozenD(from, to, progress ?? 0);
  	const first = icon ?? from ?? to;
  	return first !== void 0 ? canonicalD(first) : "";
  }
  /** Per-instance driver state — the exact logic of the React/Vue bindings
  *  (mount, mode watch, controlled seek with re-basing, imperative). Takes the
  *  init-time props: they seed the watch baselines so a watcher that fires on
  *  mount (Svelte's $effect, unlike Vue's watch) is a no-op. All change
  *  detection lives HERE, under tsc — the shells only wire their reactive
  *  surface to these methods.
  *
  *  Lifecycle contract (shared verbatim by the bindings):
  *  - Lazy driver: an iconless mount keeps the element and births the driver
  *    on the FIRST icon that shows up (prop or imperative). `morphTo` with no
  *    driver behaves as `set` — there is nothing to fly from.
  *  - Controlled wins: while `from` and `to` are both present the pair owns
  *    the path and `icon` changes are ignored; dropping the pair hands the
  *    path back to `icon`.
  *  - Every exit from controlled mode (imperative call or icon takeover)
  *    invalidates the frozen pair, so returning to it re-bases on `from`. */
  function createController({ icon, from, to, progress, reducedMotion }) {
  	let el = null;
  	let dead = false;
  	let morph = null;
  	let rm = reducedMotion ?? "never";
  	let based = false;
  	let pair = null;
  	let prevIcon = icon;
  	let prevControlled = from !== void 0 && to !== void 0;
  	let prevFrom = from;
  	let prevTo = to;
  	let prevProgress = progress;
  	/** Driver birth, lazy included: the first icon to show up creates it. */
  	const ensure = (birth) => {
  		if (morph) return morph;
  		if (dead || !el) return null;
  		morph = createMorph(el, birth, { reducedMotion: rm });
  		return morph;
  	};
  	/** Controlled mode: freeze the pair at `progress` via seek (no spring). */
  	const applyPair = (from, to, progress) => {
  		const m = morph;
  		if (!m) return;
  		const t = progress ?? 0;
  		if (!pair || pair[0] !== from || pair[1] !== to) {
  			pair = [from, to];
  			based = false;
  		}
  		if (t <= 0) {
  			m.set(from);
  			based = false;
  		} else if (t >= 1) {
  			m.set(to);
  			based = false;
  		} else {
  			if (!based) {
  				m.set(from);
  				based = true;
  			}
  			m.seek(to, t);
  		}
  	};
  	return {
  		mount(mountEl, { icon, from, to, progress, reducedMotion }) {
  			el = mountEl;
  			rm = reducedMotion ?? rm;
  			const controlled = from !== void 0 && to !== void 0;
  			const initialIcon = icon ?? from ?? to;
  			if (initialIcon === void 0) return;
  			const m = createMorph(el, controlled ? from : initialIcon, { reducedMotion: rm });
  			morph = m;
  			if (controlled) {
  				pair = [from, to];
  				const t = progress ?? 0;
  				if (t <= 0) m.set(from);
  				else if (t >= 1) m.set(to);
  				else {
  					m.seek(to, t);
  					based = true;
  				}
  			}
  		},
  		destroy() {
  			dead = true;
  			el = null;
  			morph?.destroy();
  			morph = null;
  			based = false;
  			pair = null;
  		},
  		/** Prop watcher: ONE owner decides per run — the controlled pair while it
  		*  is fully present, `icon` otherwise (mount doesn't fire thanks to the
  		*  init-time baselines). */
  		watch({ icon, from, to, progress, spring, reducedMotion }) {
  			rm = reducedMotion ?? "never";
  			if (morph) morph.reducedMotion = rm;
  			const controlled = from !== void 0 && to !== void 0;
  			const left = prevControlled && !controlled;
  			const iconChanged = icon !== prevIcon;
  			const pairChanged = from !== prevFrom || to !== prevTo || progress !== prevProgress;
  			prevControlled = controlled;
  			prevIcon = icon;
  			prevFrom = from;
  			prevTo = to;
  			prevProgress = progress;
  			if (controlled) {
  				if (!pairChanged) return;
  				if (!(morph ?? ensure(from))) return;
  				applyPair(from, to, progress);
  				return;
  			}
  			if (icon === void 0 || !iconChanged && !left) return;
  			pair = null;
  			based = false;
  			if (morph) morph.morphTo(icon, spring);
  			else ensure(icon);
  		},
  		morphTo(icon, spring) {
  			pair = null;
  			based = false;
  			if (morph) morph.morphTo(icon, spring);
  			else ensure(icon);
  		},
  		set(icon) {
  			pair = null;
  			based = false;
  			if (morph) morph.set(icon);
  			else ensure(icon);
  		}
  	};
  }
  //#endregion
    return { n: createController, t: computeInitialD };
  })();

  // ---------- element chunk ----------
  const mc_element = (() => {
    const { n: createController, t: computeInitialD } = mc_controller;
  //#region src/element/index.ts
  const SVG_NS = "http://www.w3.org/2000/svg";
  const REACTIVE = [
  	"icon",
  	"from",
  	"to",
  	"progress",
  	"spring",
  	"reducedMotion"
  ];
  const PRESENTATION = /* @__PURE__ */ new Set([
  	"size",
  	"color",
  	"stroke-width",
  	"absolute-stroke-width",
  	"label"
  ]);
  const Base = typeof HTMLElement === "undefined" ? class {} : HTMLElement;
  var MorphIconElement = class extends Base {
  	static observedAttributes = [
  		"icon",
  		"from",
  		"to",
  		"progress",
  		"spring",
  		"reduced-motion",
  		"size",
  		"color",
  		"stroke-width",
  		"absolute-stroke-width",
  		"label"
  	];
  	#props = {};
  	#ctrl = null;
  	#path = null;
  	#adopted = false;
  	#preConnectDirty = false;
  	#size = "24";
  	#color = "currentColor";
  	#strokeWidth = "2";
  	#absoluteStrokeWidth = false;
  	#label = null;
  	get icon() {
  		return this.#props.icon;
  	}
  	set icon(v) {
  		this.#props.icon = v;
  		this.#watch();
  	}
  	get from() {
  		return this.#props.from;
  	}
  	set from(v) {
  		this.#props.from = v;
  		this.#watch();
  	}
  	get to() {
  		return this.#props.to;
  	}
  	set to(v) {
  		this.#props.to = v;
  		this.#watch();
  	}
  	get progress() {
  		return this.#props.progress;
  	}
  	set progress(v) {
  		this.#props.progress = v;
  		this.#watch();
  	}
  	get spring() {
  		return this.#props.spring;
  	}
  	set spring(v) {
  		this.#props.spring = v;
  		this.#watch();
  	}
  	get reducedMotion() {
  		return this.#props.reducedMotion;
  	}
  	set reducedMotion(v) {
  		this.#props.reducedMotion = v;
  		this.#watch();
  	}
  	morphTo(icon, spring) {
  		const ctrl = this.#ctrl;
  		if (!ctrl) {
  			this.#imperativeBeforeDriver(icon);
  			return;
  		}
  		ctrl.morphTo(icon, spring ?? this.#props.spring);
  	}
  	set(icon) {
  		const ctrl = this.#ctrl;
  		if (!ctrl) {
  			this.#imperativeBeforeDriver(icon);
  			return;
  		}
  		ctrl.set(icon);
  	}
  	connectedCallback() {
  		for (const key of REACTIVE) this.#upgradeProperty(key);
  		if (!this.#path) this.#adoptOrRender();
  		if (this.style.display === "") this.style.display = "contents";
  		if (this.#path && !this.#ctrl) {
  			const path = this.#path;
  			const gate = this.#adopted && !this.#preConnectDirty;
  			this.#adopted = false;
  			this.#ctrl = createController(this.#props);
  			if (gate) {
  				let armed = false;
  				const gated = { setAttribute(name, value) {
  					if (armed) path.setAttribute(name, value);
  				} };
  				try {
  					this.#ctrl.mount(gated, this.#props);
  				} finally {
  					armed = true;
  				}
  			} else this.#ctrl.mount(path, this.#props);
  		}
  	}
  	disconnectedCallback() {
  		this.#ctrl?.destroy();
  		this.#ctrl = null;
  	}
  	attributeChangedCallback(name, _old, value) {
  		switch (name) {
  			case "icon":
  				this.#props.icon = value ?? void 0;
  				break;
  			case "from":
  				this.#props.from = value ?? void 0;
  				break;
  			case "to":
  				this.#props.to = value ?? void 0;
  				break;
  			case "progress": {
  				const n = value === null ? NaN : Number(value);
  				this.#props.progress = Number.isFinite(n) ? n : void 0;
  				break;
  			}
  			case "spring":
  				this.#props.spring = value ?? void 0;
  				break;
  			case "reduced-motion":
  				this.#props.reducedMotion = value ?? void 0;
  				break;
  			case "size":
  				this.#size = value ?? "24";
  				break;
  			case "color":
  				this.#color = value ?? "currentColor";
  				break;
  			case "stroke-width":
  				this.#strokeWidth = value ?? "2";
  				break;
  			case "absolute-stroke-width":
  				this.#absoluteStrokeWidth = value !== null;
  				break;
  			case "label": this.#label = value;
  		}
  		if (!this.#path) return;
  		if (PRESENTATION.has(name)) {
  			const svg = this.querySelector("svg");
  			if (svg) this.#applyPresentation(svg);
  			return;
  		}
  		this.#watch();
  	}
  	/** A property assigned before the element was defined lands as an own
  	*  value that shadows the accessor; re-route it through the setter. */
  	#upgradeProperty(key) {
  		if (Object.hasOwn(this, key)) {
  			const self = this;
  			const v = self[key];
  			delete self[key];
  			self[key] = v;
  		}
  	}
  	#watch() {
  		const ctrl = this.#ctrl;
  		if (!ctrl) {
  			this.#preConnectDirty = true;
  			return;
  		}
  		ctrl.watch(this.#props);
  	}
  	/** Imperative call before the controller exists: adopt the icon as the
  	*  mount state AND exit controlled mode — "every imperative call
  	*  invalidates the frozen pair" must hold pre-connect too, or a pending
  	*  pair would win the mount and silently discard this call. */
  	#imperativeBeforeDriver(icon) {
  		this.#props.icon = icon;
  		this.#props.from = void 0;
  		this.#props.to = void 0;
  		this.#preConnectDirty = true;
  	}
  	/** SSR markup is adopted verbatim (the element never rewrites server bytes
  	*  at rest); without it, the element renders its own <svg> like the
  	*  framework shells do. */
  	#adoptOrRender() {
  		const existing = this.querySelector("path");
  		if (existing) {
  			this.#path = existing;
  			this.#adopted = true;
  			const p = this.#props;
  			if (p.icon === void 0 && p.from === void 0 && p.to === void 0) {
  				const d = existing.getAttribute("d");
  				if (d) p.icon = d;
  			}
  			return;
  		}
  		const doc = this.ownerDocument;
  		const svg = doc.createElementNS(SVG_NS, "svg");
  		svg.setAttribute("xmlns", SVG_NS);
  		svg.setAttribute("viewBox", "0 0 24 24");
  		svg.setAttribute("fill", "none");
  		svg.setAttribute("stroke-linecap", "round");
  		svg.setAttribute("stroke-linejoin", "round");
  		this.#applyPresentation(svg);
  		const path = doc.createElementNS(SVG_NS, "path");
  		path.setAttribute("d", computeInitialD(this.#props));
  		svg.appendChild(path);
  		this.appendChild(svg);
  		this.#path = path;
  	}
  	#applyPresentation(svg) {
  		svg.setAttribute("width", this.#size);
  		svg.setAttribute("height", this.#size);
  		svg.setAttribute("stroke", this.#color);
  		const sw = this.#absoluteStrokeWidth ? String(Number(this.#strokeWidth) * 24 / Number(this.#size)) : this.#strokeWidth;
  		svg.setAttribute("stroke-width", sw);
  		const label = this.#label;
  		let title = svg.querySelector("title");
  		if (label) {
  			svg.setAttribute("role", "img");
  			svg.removeAttribute("aria-hidden");
  			if (!title) {
  				title = this.ownerDocument.createElementNS(SVG_NS, "title");
  				svg.insertBefore(title, svg.firstChild);
  			}
  			title.textContent = label;
  		} else {
  			svg.removeAttribute("role");
  			svg.setAttribute("aria-hidden", "true");
  			title?.remove();
  		}
  	}
  };
  let registered = false;
  /** Defines `<morph-icon>` (or a custom tag). Idempotent per tag; a no-op
  *  without a DOM (safe to call from code that also runs during SSR). */
  function defineMorphIcon(tag = "morph-icon") {
  	if (typeof customElements === "undefined" || customElements.get(tag)) return;
  	customElements.define(tag, registered ? class extends MorphIconElement {} : MorphIconElement);
  	registered = true;
  }
  //#endregion
    return { MorphIconElement, computeInitialD, defineMorphIcon };
  })();

  window.MorphIcons = mc_element;
})();
