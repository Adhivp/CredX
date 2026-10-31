/**
 * Escaping HTML templates. Every interpolated value is escaped unless it is itself an
 * `Html` fragment, so customer data, notes and agent text (all untrusted, as the
 * backend's read models note) can never inject markup.
 */

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

export class Html {
	constructor(readonly value: string) {}
	toString(): string {
		return this.value;
	}
}

type Part = unknown;

function render(part: Part): string {
	if (part === null || part === undefined || part === false || part === true) return '';
	if (part instanceof Html) return part.value;
	if (Array.isArray(part)) return part.map(render).join('');
	return esc(part);
}

export function html(strings: TemplateStringsArray, ...values: Part[]): Html {
	let out = strings[0];
	for (let i = 0; i < values.length; i++) out += render(values[i]) + strings[i + 1];
	return new Html(out);
}

/** Trusted markup produced by this codebase (never backend text). */
export const raw = (markup: string) => new Html(markup);

export function mount(el: Element | null, content: Html | string): void {
	if (el) el.innerHTML = String(content);
}

/** Attribute-safe JSON for data-* payloads. */
export const dataJson = (value: unknown) => esc(JSON.stringify(value));
