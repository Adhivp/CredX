/**
 * Shared interface pieces rendered from TypeScript: badges, states, toasts, dialogs and
 * the section loader every API-driven block uses (loading → success | empty | error, with
 * retry). Business values only ever arrive through these from API responses.
 */

import { ApiError, ApiUnavailableError, SessionExpiredError, isAbort } from './api/client';
import { html, mount, raw, type Html } from './html';
import { INVESTIGATION_STATUS, type Tone, textOf, toneOf } from './labels';
import type { RiskCategory } from './types';

// ── Badges ───────────────────────────────────────────────────────────────

const RISK_LEVEL: Record<string, { cls: string; level: number; text: string }> = {
	Low: { cls: 'low', level: 1, text: 'Low' },
	Medium: { cls: 'medium', level: 2, text: 'Medium' },
	High: { cls: 'high', level: 3, text: 'High' },
	Critical: { cls: 'critical', level: 4, text: 'Critical' },
	'Insufficient Data': { cls: 'insufficient', level: 0, text: 'Insufficient data' },
};

export const riskClass = (category: string) => RISK_LEVEL[category]?.cls ?? 'insufficient';

/** Risk category as text plus a four-step level glyph, so colour never carries it alone. */
export function riskBadge(category: RiskCategory | string, size: 'sm' | 'md' = 'md'): Html {
	const meta = RISK_LEVEL[category] ?? { cls: 'insufficient', level: 0, text: category };
	const bars = [1, 2, 3, 4].map((n) => raw(`<i class="${n <= meta.level ? 'on' : ''}"></i>`));
	return html`<span class="risk risk--${meta.cls} risk--${size}" data-level="${meta.level}"><span class="risk__glyph" aria-hidden="true">${bars}</span>${meta.text}</span>`;
}

export function pill(text: string, tone: Tone = 'neutral', title = ''): Html {
	return html`<span class="pill pill--${tone}" title="${title}">${text}</span>`;
}

export const statusPill = (status: string) =>
	pill(textOf(INVESTIGATION_STATUS, status), toneOf(INVESTIGATION_STATUS, status));

/** 0–100 score with a proportional track; the track is decoration, the number is the value. */
export function scoreBar(value: number | null | undefined, category?: string): Html {
	if (value === null || value === undefined) return html`<span class="muted">—</span>`;
	const width = Math.max(0, Math.min(100, value));
	return html`<span class="score"><span class="score__value tnum">${value.toFixed(1)}</span><span class="score__track" aria-hidden="true"><span class="score__fill score__fill--${riskClass(category ?? '')}" style="width:${width}%"></span></span></span>`;
}

// ── States ───────────────────────────────────────────────────────────────

export function skeleton(lines = 3, kind: 'rows' | 'block' | 'kpi' = 'rows'): Html {
	if (kind === 'block') return raw('<div class="skeleton skeleton--block" aria-hidden="true"></div>');
	const rows = Array.from({ length: lines }, (_, i) => `<span class="skeleton skeleton--line" style="width:${92 - ((i * 17) % 40)}%"></span>`);
	return raw(`<div class="skeleton-stack" aria-busy="true" aria-label="Loading">${rows.join('')}</div>`);
}

export function emptyState(title: string, body = '', action?: Html): Html {
	return html`<div class="state state--empty"><p class="state__title">${title}</p>${body ? html`<p class="state__body">${body}</p>` : ''}${action ?? ''}</div>`;
}

export function errorState(error: unknown, retry = true): Html {
	const forbidden = error instanceof ApiError && error.forbidden;
	const unavailable = error instanceof ApiUnavailableError;
	const title = forbidden ? 'Not available for your role' : unavailable ? 'API unavailable' : 'This section could not load';
	const body = error instanceof Error ? error.message : String(error);
	return html`<div class="state state--error" role="alert"><p class="state__title">${title}</p><p class="state__body">${body}</p>${
		retry && !forbidden ? html`<button type="button" class="btn btn--ghost btn--sm" data-retry>Retry</button>` : ''
	}</div>`;
}

export interface LoadOptions<T> {
	signal?: AbortSignal;
	/** Shown while the first request is in flight. */
	loading?: Html;
	isEmpty?: (data: T) => boolean;
	empty?: Html | ((data: T) => Html);
	/** Called after a successful render (bind events, draw charts). */
	after?: (data: T, el: HTMLElement) => void;
}

/**
 * Fetch and render one section. A refetch keeps the previous render visible at reduced
 * opacity rather than flashing a skeleton. Returns the data, or null on failure/abort.
 */
export async function load<T>(
	el: HTMLElement | null,
	fetcher: () => Promise<T>,
	render: (data: T, el: HTMLElement) => Html,
	opts: LoadOptions<T> = {},
): Promise<T | null> {
	if (!el) return null;
	const hasContent = el.dataset.loaded === 'true';
	if (hasContent) el.classList.add('is-refreshing');
	else mount(el, opts.loading ?? skeleton());
	try {
		const data = await fetcher();
		if (opts.signal?.aborted) return null;
		if (opts.isEmpty?.(data)) {
			mount(el, typeof opts.empty === 'function' ? opts.empty(data) : opts.empty ?? emptyState('Nothing to show yet'));
		} else {
			mount(el, render(data, el));
		}
		el.dataset.loaded = 'true';
		opts.after?.(data, el);
		return data;
	} catch (error) {
		if (isAbort(error) || opts.signal?.aborted) return null;
		if (error instanceof SessionExpiredError) return null; // the shell redirects to sign-in
		mount(el, errorState(error));
		el.dataset.loaded = 'false';
		el.querySelector('[data-retry]')?.addEventListener('click', () => void load(el, fetcher, render, opts), { once: true });
		return null;
	} finally {
		el.classList.remove('is-refreshing');
	}
}

// ── Toasts ───────────────────────────────────────────────────────────────

export function toast(message: string, tone: 'good' | 'critical' | 'info' = 'good'): void {
	const region = document.getElementById('toasts');
	if (!region) return;
	const el = document.createElement('div');
	el.className = `toast toast--${tone}`;
	el.setAttribute('role', tone === 'critical' ? 'alert' : 'status');
	el.textContent = message;
	region.appendChild(el);
	setTimeout(() => {
		el.classList.add('is-leaving');
		setTimeout(() => el.remove(), 250);
	}, tone === 'critical' ? 7000 : 4500);
}

export function failureMessage(error: unknown): string {
	if (error instanceof Error) return error.message;
	return 'The request failed.';
}

// ── Dialogs ──────────────────────────────────────────────────────────────

export interface Field {
	name: string;
	label: string;
	type?: 'text' | 'textarea' | 'select' | 'number' | 'radio';
	options?: { value: string; text: string; hint?: string }[];
	required?: boolean;
	minLength?: number;
	maxLength?: number;
	min?: number;
	max?: number;
	step?: number;
	placeholder?: string;
	value?: string;
	hint?: string;
}

export interface DialogOptions {
	title: string;
	description?: Html | string;
	fields: Field[];
	submit: string;
	/** Show a review step summarising the values before the request is sent. */
	confirm?: (values: Record<string, string>) => Html;
	danger?: boolean;
	onSubmit: (values: Record<string, string>) => Promise<void>;
}

function fieldHtml(f: Field): Html {
	const id = `f-${f.name}`;
	const hint = f.hint ? html`<span class="field__hint" id="${id}-hint">${f.hint}</span>` : '';
	const common = {
		required: f.required ? raw('required') : '',
		min: f.minLength ? raw(`minlength="${f.minLength}"`) : '',
		max: f.maxLength ? raw(`maxlength="${f.maxLength}"`) : '',
		describedby: f.hint ? raw(`aria-describedby="${id}-hint"`) : '',
	};
	let control: Html;
	if (f.type === 'textarea') {
		control = html`<textarea class="input" id="${id}" name="${f.name}" rows="4" placeholder="${f.placeholder ?? ''}" ${common.required} ${common.min} ${common.max} ${common.describedby}>${f.value ?? ''}</textarea>`;
	} else if (f.type === 'select') {
		control = html`<select class="input" id="${id}" name="${f.name}" ${common.required} ${common.describedby}>${(f.options ?? []).map(
			(o) => html`<option value="${o.value}" ${o.value === f.value ? raw('selected') : ''}>${o.text}</option>`,
		)}</select>`;
	} else if (f.type === 'radio') {
		return html`<fieldset class="field field--radio"><legend class="field__label">${f.label}</legend>${(f.options ?? []).map(
			(o, i) => html`<label class="choice"><input type="radio" name="${f.name}" value="${o.value}" ${
				(f.value ? o.value === f.value : i === 0) ? raw('checked') : ''
			} ${common.required}/><span><span class="choice__text">${o.text}</span>${o.hint ? html`<span class="choice__hint">${o.hint}</span>` : ''}</span></label>`,
		)}${hint}</fieldset>`;
	} else {
		const numberAttrs =
			f.type === 'number'
				? raw(`${f.min !== undefined ? `min="${f.min}"` : ''} ${f.max !== undefined ? `max="${f.max}"` : ''} step="${f.step ?? 'any'}"`)
				: '';
		control = html`<input class="input" id="${id}" name="${f.name}" type="${f.type ?? 'text'}" value="${f.value ?? ''}" placeholder="${f.placeholder ?? ''}" ${numberAttrs} ${common.required} ${common.min} ${common.max} ${common.describedby}/>`;
	}
	return html`<div class="field"><label class="field__label" for="${id}">${f.label}</label>${control}${hint}</div>`;
}

/** Modal form for every action that takes parameters or a reason. */
export function openDialog(opts: DialogOptions): void {
	const dialog = document.createElement('dialog');
	dialog.className = 'dialog';
	dialog.setAttribute('aria-labelledby', 'dialog-title');
	mount(
		dialog,
		html`<form class="dialog__form" novalidate>
			<header class="dialog__head"><h2 class="dialog__title" id="dialog-title">${opts.title}</h2>
				<button type="button" class="icon-btn" data-close aria-label="Close">${raw('&times;')}</button></header>
			<div class="dialog__body" data-step="edit">
				${opts.description ? html`<div class="dialog__desc">${opts.description}</div>` : ''}
				${opts.fields.map(fieldHtml)}
			</div>
			<div class="dialog__body" data-step="confirm" hidden></div>
			<p class="dialog__error" role="alert" hidden></p>
			<footer class="dialog__actions">
				<button type="button" class="btn btn--ghost" data-cancel>Cancel</button>
				<button type="submit" class="btn ${opts.danger ? 'btn--danger' : 'btn--primary'}" data-submit>${opts.confirm ? 'Review' : opts.submit}</button>
			</footer>
		</form>`,
	);
	document.body.appendChild(dialog);
	const form = dialog.querySelector('form') as HTMLFormElement;
	const edit = dialog.querySelector('[data-step="edit"]') as HTMLElement;
	const review = dialog.querySelector('[data-step="confirm"]') as HTMLElement;
	const error = dialog.querySelector('.dialog__error') as HTMLElement;
	const submit = dialog.querySelector('[data-submit]') as HTMLButtonElement;
	const cancel = dialog.querySelector('[data-cancel]') as HTMLButtonElement;
	let confirming = false;

	const close = () => dialog.close();
	dialog.querySelector('[data-close]')?.addEventListener('click', close);
	cancel.addEventListener('click', () => {
		if (confirming) {
			confirming = false;
			review.hidden = true;
			edit.hidden = false;
			submit.textContent = 'Review';
			cancel.textContent = 'Cancel';
			return;
		}
		close();
	});
	dialog.addEventListener('close', () => dialog.remove());

	form.addEventListener('submit', async (event) => {
		event.preventDefault();
		error.hidden = true;
		if (!confirming && !form.reportValidity()) return;
		const values: Record<string, string> = {};
		new FormData(form).forEach((v, k) => (values[k] = String(v).trim()));
		for (const f of opts.fields) {
			if (f.minLength && f.required && (values[f.name] ?? '').length < f.minLength) {
				error.textContent = `${f.label} needs at least ${f.minLength} characters.`;
				error.hidden = false;
				return;
			}
		}
		if (opts.confirm && !confirming) {
			confirming = true;
			mount(review, opts.confirm(values));
			edit.hidden = true;
			review.hidden = false;
			submit.textContent = opts.submit;
			cancel.textContent = 'Back';
			submit.focus();
			return;
		}
		submit.disabled = true;
		cancel.disabled = true;
		submit.classList.add('is-busy');
		try {
			await opts.onSubmit(values);
			close();
		} catch (err) {
			error.textContent = failureMessage(err);
			error.hidden = false;
			submit.disabled = false;
			cancel.disabled = false;
		} finally {
			submit.classList.remove('is-busy');
		}
	});
	dialog.showModal();
	(dialog.querySelector('input:not([type=radio]), textarea, select, input[type=radio]:checked') as HTMLElement | null)?.focus();
}

/** Summary list used in confirmation steps. */
export function summary(rows: [string, Html | string][], note?: string): Html {
	return html`<dl class="summary">${rows.map(([k, v]) => html`<div><dt>${k}</dt><dd>${v}</dd></div>`)}</dl>${
		note ? html`<p class="dialog__note">${note}</p>` : ''
	}`;
}

// ── Tooltips for charts and data marks ───────────────────────────────────

let tipEl: HTMLElement | null = null;

function tip(): HTMLElement {
	if (!tipEl || !document.body.contains(tipEl)) {
		tipEl = document.createElement('div');
		tipEl.className = 'tooltip';
		tipEl.setAttribute('role', 'tooltip');
		document.body.appendChild(tipEl);
	}
	return tipEl;
}

function showTip(target: Element, x?: number, y?: number) {
	const text = target.getAttribute('data-tip');
	if (!text) return;
	const el = tip();
	el.textContent = '';
	text.split('\n').forEach((line, i) => {
		const row = document.createElement('div');
		if (i === 0) row.className = 'tooltip__title';
		row.textContent = line;
		el.appendChild(row);
	});
	el.classList.add('is-visible');
	const rect = target.getBoundingClientRect();
	const px = x ?? rect.left + rect.width / 2;
	const py = y ?? rect.top;
	const w = el.offsetWidth;
	const left = Math.min(window.innerWidth - w - 8, Math.max(8, px - w / 2));
	const top = py - el.offsetHeight - 12 < 8 ? py + 18 : py - el.offsetHeight - 12;
	el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
}

const hideTip = () => tipEl?.classList.remove('is-visible');

/** Hover and keyboard-focus tooltips for every [data-tip] inside root. */
export function bindTips(root: Element) {
	root.addEventListener('pointermove', (e) => {
		const t = (e.target as Element).closest?.('[data-tip]');
		if (t) showTip(t, (e as PointerEvent).clientX, (e as PointerEvent).clientY);
		else hideTip();
	});
	root.addEventListener('pointerleave', hideTip);
	root.addEventListener('focusin', (e) => {
		const t = (e.target as Element).closest?.('[data-tip]');
		if (t) showTip(t);
	});
	root.addEventListener('focusout', hideTip);
}
