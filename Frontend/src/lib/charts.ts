/**
 * Small, dependency-free charts. Every mark has a tooltip (hover and keyboard focus) and
 * every chart is accompanied by a table view, so no value is reachable only by hovering.
 * Colours follow meaning: risk categories use the validated risk palette from tokens.css;
 * a single series uses the accent. Text always uses text colours, never series colours.
 */

import { html, raw, type Html } from './html';

// ── Scales & ticks ───────────────────────────────────────────────────────

export function niceTicks(min: number, max: number, count = 4): number[] {
	if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
	if (min === max) {
		const pad = Math.abs(min) * 0.1 || 1;
		min -= pad;
		max += pad;
	}
	const span = max - min;
	const step0 = span / count;
	const mag = 10 ** Math.floor(Math.log10(step0));
	const norm = step0 / mag;
	const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
	const start = Math.floor(min / step) * step;
	const ticks: number[] = [];
	for (let v = start; v <= max + step * 0.5; v += step) ticks.push(Number(v.toFixed(10)));
	return ticks;
}

const scale = (d0: number, d1: number, r0: number, r1: number) => (v: number) =>
	d1 === d0 ? (r0 + r1) / 2 : r0 + ((v - d0) / (d1 - d0)) * (r1 - r0);

// ── Responsive SVG host ──────────────────────────────────────────────────

const observers = new WeakMap<Element, ResizeObserver>();

/** Draw into el at its current width, and redraw when the width changes. */
export function responsive(el: HTMLElement, draw: (width: number) => Html): void {
	let last = 0;
	const render = () => {
		const width = Math.floor(el.clientWidth);
		if (!width || width === last) return;
		last = width;
		el.innerHTML = String(draw(width));
	};
	observers.get(el)?.disconnect();
	const ro = new ResizeObserver(() => render());
	ro.observe(el);
	observers.set(el, ro);
	render();
}

// ── Donut (composition) ──────────────────────────────────────────────────

export interface Segment {
	key: string;
	label: string;
	value: number;
	cls: string; // colour class, e.g. "fill-risk-high"
	tip: string;
}

export function donut(segments: Segment[], opts: { size?: number; thickness?: number; center?: Html; label: string }): Html {
	const size = opts.size ?? 168;
	const t = opts.thickness ?? 18;
	const r = size / 2 - t / 2;
	const c = size / 2;
	const total = segments.reduce((s, x) => s + x.value, 0);
	const shown = segments.filter((s) => s.value > 0);
	const gap = shown.length > 1 ? 2 / r : 0; // 2px surface gap between arcs, in radians
	let angle = -Math.PI / 2;
	const arcs = shown.map((s) => {
		const sweep = (s.value / total) * Math.PI * 2;
		const a0 = angle + gap / 2;
		const a1 = angle + sweep - gap / 2;
		angle += sweep;
		if (shown.length === 1) {
			return raw(
				`<circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke-width="${t}" class="arc ${s.cls}" tabindex="0" data-tip="${escAttr(s.tip)}" aria-label="${escAttr(s.tip)}"/>`,
			);
		}
		const large = a1 - a0 > Math.PI ? 1 : 0;
		const p = (a: number) => `${(c + r * Math.cos(a)).toFixed(2)} ${(c + r * Math.sin(a)).toFixed(2)}`;
		return raw(
			`<path d="M ${p(a0)} A ${r} ${r} 0 ${large} 1 ${p(a1)}" fill="none" stroke-width="${t}" class="arc ${s.cls}" tabindex="0" data-tip="${escAttr(s.tip)}" aria-label="${escAttr(s.tip)}"/>`,
		);
	});
	return html`<div class="donut" style="width:${size}px;height:${size}px">
		<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="${opts.label}">
			<circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke-width="${t}" class="arc-track"/>
			${total > 0 ? arcs : ''}
		</svg>
		${opts.center ? html`<div class="donut__center">${opts.center}</div>` : ''}
	</div>`;
}

function escAttr(s: string) {
	return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// ── Horizontal bars (comparison) ─────────────────────────────────────────

export interface BarRow {
	label: string;
	value: number | null;
	display: string;
	tip: string;
	cls?: string;
	href?: string;
	note?: Html | string;
	muted?: boolean;
}

export function hbars(rows: BarRow[], opts: { max?: number; reference?: { value: number; label: string }; label: string }): Html {
	const max = opts.max ?? Math.max(1e-9, ...rows.map((r) => r.value ?? 0), opts.reference?.value ?? 0);
	const ref = opts.reference;
	return html`<div class="hbars" role="list" aria-label="${opts.label}">
		${rows.map((r) => {
			const width = r.value === null ? 0 : Math.max(0, (r.value / max) * 100);
			const label = r.href
				? html`<a class="hbars__label" href="${r.href}">${r.label}</a>`
				: html`<span class="hbars__label">${r.label}</span>`;
			return html`<div class="hbars__row ${r.muted ? 'is-muted' : ''}" role="listitem">
				${label}
				<span class="hbars__track" data-tip="${r.tip}" tabindex="0" aria-label="${r.tip}">
					${r.value === null ? '' : html`<span class="hbars__bar ${r.cls ?? 'fill-accent'}" style="width:${width.toFixed(2)}%"></span>`}
					${ref ? html`<span class="hbars__ref" style="left:${((ref.value / max) * 100).toFixed(2)}%" aria-hidden="true"></span>` : ''}
				</span>
				<span class="hbars__value tnum">${r.display}</span>
				${r.note ? html`<span class="hbars__note">${r.note}</span>` : ''}
			</div>`;
		})}
		${ref ? html`<p class="hbars__legend"><span class="ref-key" aria-hidden="true"></span>${ref.label}</p>` : ''}
	</div>`;
}

// ── Stacked bars (composition across groups) ─────────────────────────────

export interface StackPart {
	key: string;
	value: number;
	cls: string;
	tip: string;
}

export function stacked(
	rows: { label: string; href?: string; parts: StackPart[]; total: number; display: string; muted?: Html | string }[],
	legend: { cls: string; label: string }[],
	label: string,
): Html {
	const legendHtml = html`<ul class="legend">${legend.map((l) => html`<li><span class="swatch ${l.cls}" aria-hidden="true"></span>${l.label}</li>`)}</ul>`;
	return html`<div class="stacked" aria-label="${label}">
		${legendHtml}
		${rows.map((r) => {
			const head = r.href ? html`<a class="stacked__label" href="${r.href}">${r.label}</a>` : html`<span class="stacked__label">${r.label}</span>`;
			if (r.muted) return html`<div class="stacked__row is-muted">${head}<span class="stacked__muted">${r.muted}</span><span class="stacked__value"></span></div>`;
			return html`<div class="stacked__row">${head}
				<span class="stacked__bar">${r.parts
					.filter((p) => p.value > 0)
					.map((p) => html`<span class="stacked__part ${p.cls}" style="flex-grow:${p.value}" data-tip="${p.tip}" tabindex="0" aria-label="${p.tip}"></span>`)}</span>
				<span class="stacked__value tnum">${r.display}</span></div>`;
		})}
	</div>`;
}

// ── Line chart (change over time) ────────────────────────────────────────

export interface LinePoint {
	x: number;
	y: number;
	tip: string;
	cls?: string; // marker class override
}

export interface LineOptions {
	height?: number;
	yDomain?: [number, number];
	xFormat: (x: number) => string;
	yFormat?: (y: number) => string;
	references?: { y: number; label: string }[];
	area?: boolean;
	label: string;
	dark?: boolean;
	compact?: boolean;
	xTickCount?: number;
}

export function line(points: LinePoint[], width: number, opts: LineOptions): Html {
	const height = opts.height ?? 220;
	const m = opts.compact ? { l: 30, r: 10, t: 10, b: 22 } : { l: 44, r: 16, t: 14, b: 28 };
	const xs = points.map((p) => p.x);
	const ys = points.map((p) => p.y).concat((opts.references ?? []).map((r) => r.y));
	const [y0, y1] = opts.yDomain ?? [Math.min(...ys, 0), Math.max(...ys)];
	const yTicks = opts.yDomain ? niceTicks(y0, y1, opts.compact ? 2 : 4).filter((t) => t >= y0 && t <= y1) : niceTicks(y0, y1, opts.compact ? 2 : 4);
	const yMin = Math.min(y0, yTicks[0]);
	const yMax = Math.max(y1, yTicks[yTicks.length - 1]);
	const x0 = Math.min(...xs);
	const x1 = Math.max(...xs);
	const sx = scale(x0, x1, m.l, width - m.r);
	const sy = scale(yMin, yMax, height - m.b, m.t);
	const fy = opts.yFormat ?? ((v: number) => String(Math.round(v)));
	const grid = yTicks.map(
		(t) => raw(`<line class="grid" x1="${m.l}" x2="${width - m.r}" y1="${sy(t).toFixed(1)}" y2="${sy(t).toFixed(1)}"/>` +
			`<text class="tick" x="${m.l - 8}" y="${(sy(t) + 4).toFixed(1)}" text-anchor="end">${escAttr(fy(t))}</text>`),
	);
	const xTickCount = Math.min(opts.xTickCount ?? (opts.compact ? 2 : 5), points.length);
	const xTicks: number[] = [];
	if (points.length === 1) xTicks.push(x0);
	else for (let i = 0; i < xTickCount; i++) xTicks.push(x0 + ((x1 - x0) * i) / Math.max(1, xTickCount - 1));
	const xLabels = xTicks.map((t, i) => {
		const anchor = xTicks.length === 1 ? 'middle' : i === 0 ? 'start' : i === xTicks.length - 1 ? 'end' : 'middle';
		return raw(`<text class="tick" x="${sx(t).toFixed(1)}" y="${height - 6}" text-anchor="${anchor}">${escAttr(opts.xFormat(t))}</text>`);
	});
	const sorted = [...points].sort((a, b) => a.x - b.x);
	const d = sorted.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)} ${sy(p.y).toFixed(1)}`).join(' ');
	const area = opts.area && sorted.length > 1
		? raw(`<path class="area" d="${d} L${sx(sorted[sorted.length - 1].x).toFixed(1)} ${sy(yMin).toFixed(1)} L${sx(sorted[0].x).toFixed(1)} ${sy(yMin).toFixed(1)} Z"/>`)
		: '';
	const refs = (opts.references ?? []).map((r) =>
		raw(`<line class="ref" x1="${m.l}" x2="${width - m.r}" y1="${sy(r.y).toFixed(1)}" y2="${sy(r.y).toFixed(1)}"/>` +
			`<text class="ref-label" x="${width - m.r}" y="${(sy(r.y) - 6).toFixed(1)}" text-anchor="end">${escAttr(r.label)}</text>`),
	);
	const showMarkers = sorted.length <= 24;
	const marks = sorted.map((p, i) => {
		const cx = sx(p.x).toFixed(1);
		const cy = sy(p.y).toFixed(1);
		const visible = showMarkers || i === sorted.length - 1;
		return raw(
			`<g class="pt" tabindex="0" data-tip="${escAttr(p.tip)}" aria-label="${escAttr(p.tip)}">` +
				`<circle class="hit" cx="${cx}" cy="${cy}" r="12"/>` +
				(visible ? `<circle class="marker ${p.cls ?? ''}" cx="${cx}" cy="${cy}" r="4"/>` : '') +
				`</g>`,
		);
	});
	return html`<svg class="linechart ${opts.dark ? 'is-dark' : ''}" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${opts.label}">
		${grid}${refs}${area}
		${sorted.length > 1 ? raw(`<path class="line" d="${d}"/>`) : ''}
		${marks}${xLabels}
	</svg>`;
}

// ── Columns (one value per period) ───────────────────────────────────────

export function columns(
	points: { label: string; value: number; cls: string; tip: string }[],
	width: number,
	opts: { height?: number; yFormat?: (v: number) => string; label: string; xEvery?: number },
): Html {
	const height = opts.height ?? 200;
	const m = { l: 40, r: 8, t: 12, b: 28 };
	const max = Math.max(1, ...points.map((p) => p.value));
	const ticks = niceTicks(0, max, 3);
	const yMax = ticks[ticks.length - 1];
	const sy = scale(0, yMax, height - m.b, m.t);
	const band = (width - m.l - m.r) / Math.max(1, points.length);
	const bw = Math.max(2, Math.min(24, band - 2));
	const fy = opts.yFormat ?? ((v: number) => String(Math.round(v)));
	const every = opts.xEvery ?? Math.max(1, Math.ceil(points.length / Math.max(1, Math.floor((width - m.l) / 64))));
	const grid = ticks.map((t) =>
		raw(`<line class="grid" x1="${m.l}" x2="${width - m.r}" y1="${sy(t).toFixed(1)}" y2="${sy(t).toFixed(1)}"/>` +
			`<text class="tick" x="${m.l - 8}" y="${(sy(t) + 4).toFixed(1)}" text-anchor="end">${escAttr(fy(t))}</text>`),
	);
	const bars = points.map((p, i) => {
		const x = m.l + band * i + (band - bw) / 2;
		const y = sy(p.value);
		const h = Math.max(0, height - m.b - y);
		const r = Math.min(4, h, bw / 2);
		// Rounded data end, square at the baseline.
		const path = h > 0
			? `M${x.toFixed(1)} ${(height - m.b).toFixed(1)} V${(y + r).toFixed(1)} Q${x.toFixed(1)} ${y.toFixed(1)} ${(x + r).toFixed(1)} ${y.toFixed(1)} H${(x + bw - r).toFixed(1)} Q${(x + bw).toFixed(1)} ${y.toFixed(1)} ${(x + bw).toFixed(1)} ${(y + r).toFixed(1)} V${(height - m.b).toFixed(1)} Z`
			: '';
		const label = i % every === 0 ? `<text class="tick" x="${(x + bw / 2).toFixed(1)}" y="${height - 8}" text-anchor="middle">${escAttr(p.label)}</text>` : '';
		const zero = h === 0 ? `<line class="zero-mark ${p.cls}" x1="${x.toFixed(1)}" x2="${(x + bw).toFixed(1)}" y1="${(height - m.b - 1).toFixed(1)}" y2="${(height - m.b - 1).toFixed(1)}"/>` : '';
		return raw(
			`<g class="col" tabindex="0" data-tip="${escAttr(p.tip)}" aria-label="${escAttr(p.tip)}">` +
				`<rect class="hit" x="${(m.l + band * i).toFixed(1)}" y="${m.t}" width="${band.toFixed(1)}" height="${height - m.b - m.t}"/>` +
				(path ? `<path class="${p.cls}" d="${path}"/>` : zero) +
				label +
				`</g>`,
		);
	});
	return html`<svg class="colchart" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${opts.label}">
		${grid}<line class="baseline" x1="${m.l}" x2="${width - m.r}" y1="${height - m.b}" y2="${height - m.b}"/>${bars}
	</svg>`;
}

// ── Table view ───────────────────────────────────────────────────────────

export function tableView(caption: string, head: string[], rows: (string | Html)[][]): Html {
	return html`<details class="tableview"><summary>View as table</summary>
		<div class="table-wrap"><table class="table table--compact"><caption class="sr-only">${caption}</caption>
		<thead><tr>${head.map((h) => html`<th scope="col">${h}</th>`)}</tr></thead>
		<tbody>${rows.map((r) => html`<tr>${r.map((c) => html`<td class="tnum">${c}</td>`)}</tr>`)}</tbody></table></div></details>`;
}
