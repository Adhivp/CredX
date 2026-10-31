/**
 * Shared client helpers for the investigation, review, portfolio and governance pages.
 *
 * All data comes from authenticated backend endpoints (window.fetch is wrapped in
 * Layout.astro to attach the bearer token). Every value interpolated into HTML goes
 * through esc(): customer data, notes and agent text are untrusted.
 */

export class ApiError extends Error {
	status: number;
	constructor(status: number, message: string) {
		super(message);
		this.status = status;
	}
}

const ESCAPES: Record<string, string> = {
	'&': '&amp;',
	'<': '&lt;',
	'>': '&gt;',
	'"': '&quot;',
	"'": '&#39;',
};

export function esc(value: unknown): string {
	return String(value ?? '').replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function detailOf(body: any, status: number): string {
	const detail = body?.detail;
	if (typeof detail === 'string') return detail;
	if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
	return `Request failed (${status})`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
	const res = await fetch(path, {
		...init,
		headers: { Accept: 'application/json', ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...(init.headers || {}) },
	});
	const body = await res.json().catch(() => null);
	if (!res.ok) throw new ApiError(res.status, detailOf(body, res.status));
	return body as T;
}

export const apiGet = <T = any>(path: string) => request<T>(path);
export const apiPost = <T = any>(path: string, body: unknown = {}) =>
	request<T>(path, { method: 'POST', body: JSON.stringify(body) });
export const apiPut = <T = any>(path: string, body: unknown = {}) =>
	request<T>(path, { method: 'PUT', body: JSON.stringify(body) });

export const label = (s: string | null | undefined) => (s ? s.replace(/_/g, ' ') : '—');

export function fmtTime(epoch?: number | null): string {
	if (!epoch) return '—';
	return new Date(epoch * 1000).toLocaleString('en-GB', {
		day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
	});
}

export function fmtMoney(value?: number | null): string {
	if (value === null || value === undefined) return '—';
	return '₹' + Math.round(value).toLocaleString('en-IN');
}

export function fmtPct(value?: number | null, digits = 0): string {
	if (value === null || value === undefined) return '—';
	return `${(value * 100).toFixed(digits)}%`;
}

export type Tone = 'neutral' | 'info' | 'good' | 'warning' | 'serious' | 'critical' | 'accent';

export const STATUS: Record<string, { text: string; tone: Tone }> = {
	created: { text: 'Created', tone: 'neutral' },
	investigating: { text: 'Investigating', tone: 'info' },
	awaiting_analyst_review: { text: 'Awaiting analyst review', tone: 'warning' },
	awaiting_approval: { text: 'Awaiting approval', tone: 'serious' },
	approved: { text: 'Approved', tone: 'good' },
	rejected: { text: 'Rejected', tone: 'critical' },
	incomplete: { text: 'Incomplete', tone: 'warning' },
	monitoring: { text: 'Monitoring', tone: 'good' },
	reopened: { text: 'Reopened', tone: 'info' },
	cancelled: { text: 'Cancelled', tone: 'neutral' },
	failed: { text: 'Failed', tone: 'critical' },
};

export const CLASSIFICATION_TONE: Record<string, Tone> = {
	low_risk: 'good',
	recovery: 'good',
	emerging_deterioration: 'warning',
	sustained_deterioration: 'serious',
	accelerating_deterioration: 'critical',
	insufficient_evidence: 'neutral',
	portfolio_cluster: 'serious',
	portfolio_stable: 'good',
};

export const SEVERITY_TONE: Record<string, Tone> = {
	low: 'good', moderate: 'warning', high: 'serious', severe: 'critical', undetermined: 'neutral',
};

export const AGENTS: Record<string, string> = {
	investigation_director: 'Investigation Director',
	repayment_specialist: 'Repayment Behaviour',
	cashflow_specialist: 'Cash-flow Intelligence',
	exposure_specialist: 'Exposure & Obligation',
	baseline_specialist: 'Behavioural Baseline',
	integrity_specialist: 'Evidence & Data Integrity',
	portfolio_specialist: 'Portfolio Intelligence',
	evidence_challenger: 'Evidence Challenger',
	credit_risk_adjudicator: 'Credit Risk Adjudicator',
};

export const agentName = (id: string) => AGENTS[id] || label(id);

export function pill(text: string, tone: Tone = 'neutral', title = ''): string {
	return `<span class="cx-pill cx-tone-${tone}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
}

export function statusPill(status: string): string {
	const meta = STATUS[status] || { text: label(status), tone: 'neutral' as Tone };
	return pill(meta.text, meta.tone);
}

/** Who requested a task, for display: never shows a human as an agent or vice versa. */
export function requester(value: string, users: Record<string, string> = {}): string {
	if (value.startsWith('human:')) return `Human · ${users[value.slice(6)] || 'reviewer'}`;
	if (value.startsWith('agent:')) return agentName(value.slice(6));
	return agentName(value);
}

export interface Me {
	id: string;
	email: string;
	full_name: string;
	role: string;
}

let mePromise: Promise<Me> | null = null;
export function me(): Promise<Me> {
	if (!mePromise) mePromise = apiGet<Me>('/api/v1/users/me');
	return mePromise;
}

export const ROLE_LABEL: Record<string, string> = {
	unassigned: 'No role assigned',
	relationship_officer: 'Relationship Officer',
	credit_analyst: 'Credit Risk Analyst',
	credit_manager: 'Credit Manager',
	portfolio_manager: 'Portfolio Risk Manager',
	risk_admin: 'Risk Administrator',
	auditor: 'Auditor',
};

const ROLE_CAN: Record<string, string[]> = {
	relationship_officer: ['context', 'request'],
	credit_analyst: ['context', 'request', 'review', 'resolve', 'complete', 'escalate', 'reopen', 'cancel', 'monitor', 'evidence'],
	credit_manager: ['context', 'request', 'review', 'resolve', 'complete', 'escalate', 'reopen', 'cancel', 'monitor', 'evidence', 'assign', 'approve'],
	portfolio_manager: ['context', 'request', 'reopen', 'portfolio'],
};

/** UI hint only; the backend independently enforces every permission. */
export const can = (role: string, action: string) => (ROLE_CAN[role] || []).includes(action);

export function toast(message: string, tone: 'good' | 'critical' = 'good') {
	const el = document.createElement('div');
	el.className = `cx-toast cx-tone-${tone}`;
	el.setAttribute('role', 'status');
	el.textContent = message;
	document.body.appendChild(el);
	setTimeout(() => el.remove(), 4200);
}

export interface FormField {
	name: string;
	label: string;
	type?: 'text' | 'textarea' | 'select';
	options?: { value: string; text: string }[];
	required?: boolean;
	minLength?: number;
	placeholder?: string;
	value?: string;
}

/** Modal form used for every action that needs a reason or parameters. */
export function openForm(opts: {
	title: string;
	description?: string;
	fields: FormField[];
	submit: string;
	onSubmit: (values: Record<string, string>) => Promise<void>;
}) {
	const dialog = document.createElement('dialog');
	dialog.className = 'cx-dialog';
	const fields = opts.fields
		.map((f) => {
			const common = `name="${esc(f.name)}" id="cx-f-${esc(f.name)}" ${f.required ? 'required' : ''} ${f.minLength ? `minlength="${f.minLength}"` : ''} placeholder="${esc(f.placeholder || '')}"`;
			let input = '';
			if (f.type === 'textarea') input = `<textarea class="form-input" rows="4" ${common}>${esc(f.value || '')}</textarea>`;
			else if (f.type === 'select')
				input = `<select class="form-select" ${common}>${(f.options || [])
					.map((o) => `<option value="${esc(o.value)}" ${o.value === f.value ? 'selected' : ''}>${esc(o.text)}</option>`)
					.join('')}</select>`;
			else input = `<input class="form-input" type="text" value="${esc(f.value || '')}" ${common} />`;
			return `<div class="form-group"><label class="form-label" for="cx-f-${esc(f.name)}">${esc(f.label)}</label>${input}</div>`;
		})
		.join('');
	dialog.innerHTML = `
		<form method="dialog" class="cx-dialog-form">
			<h3 class="cx-dialog-title">${esc(opts.title)}</h3>
			${opts.description ? `<p class="cx-dialog-desc">${esc(opts.description)}</p>` : ''}
			${fields}
			<p class="cx-dialog-error" hidden></p>
			<div class="cx-dialog-actions">
				<button type="button" class="btn btn-glass" data-cancel>Cancel</button>
				<button type="submit" class="btn btn-gold">${esc(opts.submit)}</button>
			</div>
		</form>`;
	document.body.appendChild(dialog);
	const form = dialog.querySelector('form') as HTMLFormElement;
	const error = dialog.querySelector('.cx-dialog-error') as HTMLElement;
	dialog.querySelector('[data-cancel]')?.addEventListener('click', () => dialog.close());
	dialog.addEventListener('close', () => dialog.remove());
	form.addEventListener('submit', async (event) => {
		event.preventDefault();
		const values = Object.fromEntries(new FormData(form).entries()) as Record<string, string>;
		const button = form.querySelector('[type=submit]') as HTMLButtonElement;
		button.disabled = true;
		error.hidden = true;
		try {
			await opts.onSubmit(values);
			dialog.close();
		} catch (err: any) {
			error.textContent = err?.message || 'Action failed';
			error.hidden = false;
			button.disabled = false;
		}
	});
	dialog.showModal();
}
