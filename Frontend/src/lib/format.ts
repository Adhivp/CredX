/**
 * Display formatting. Amounts are rendered in INR because the backend's ingestion and
 * the existing CredX product treat balances as rupee amounts; the API itself carries
 * no currency field, so amounts are never converted.
 */

const inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const num1 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 });
const int = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });

export const DASH = '—';

export function money(value: number | null | undefined): string {
	if (value === null || value === undefined || Number.isNaN(value)) return DASH;
	return `₹${inr.format(value)}`;
}

/** Compact INR using the Indian lakh/crore convention. */
export function moneyCompact(value: number | null | undefined): string {
	if (value === null || value === undefined || Number.isNaN(value)) return DASH;
	const abs = Math.abs(value);
	const sign = value < 0 ? '-' : '';
	if (abs >= 1e7) return `${sign}₹${num1.format(abs / 1e7)} Cr`;
	if (abs >= 1e5) return `${sign}₹${num1.format(abs / 1e5)} L`;
	if (abs >= 1e3) return `${sign}₹${num1.format(abs / 1e3)}K`;
	return `${sign}₹${inr.format(abs)}`;
}

export function count(value: number | null | undefined): string {
	if (value === null || value === undefined) return DASH;
	return int.format(value);
}

export function score(value: number | null | undefined): string {
	if (value === null || value === undefined || Number.isNaN(value)) return DASH;
	return value.toFixed(1);
}

/** A 0–1 ratio as a percentage. */
export function pct(value: number | null | undefined, digits = 0): string {
	if (value === null || value === undefined || Number.isNaN(value)) return DASH;
	return `${(value * 100).toFixed(digits)}%`;
}

export function signedPct(value: number | null | undefined, digits = 1): string {
	if (value === null || value === undefined || Number.isNaN(value)) return DASH;
	const v = value * 100;
	return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;
}

export function signed(value: number, digits = 1): string {
	return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`;
}

const dateTime = new Intl.DateTimeFormat('en-IN', {
	day: '2-digit',
	month: 'short',
	year: 'numeric',
	hour: '2-digit',
	minute: '2-digit',
});
const dateOnly = new Intl.DateTimeFormat('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
const shortDate = new Intl.DateTimeFormat('en-IN', { day: '2-digit', month: 'short' });

export function epochDateTime(epoch: number | null | undefined): string {
	if (!epoch) return DASH;
	return dateTime.format(new Date(epoch * 1000));
}

export function epochDate(epoch: number | null | undefined): string {
	if (!epoch) return DASH;
	return dateOnly.format(new Date(epoch * 1000));
}

export function epochShort(epoch: number | null | undefined): string {
	if (!epoch) return DASH;
	return shortDate.format(new Date(epoch * 1000));
}

/** Dates stored as uploaded strings (e.g. "2026-03-31"); shown as-is when unparseable. */
export function isoDate(value: string | null | undefined): string {
	if (!value) return DASH;
	const d = new Date(value.length <= 10 ? `${value}T00:00:00` : value);
	return Number.isNaN(d.getTime()) ? value : dateOnly.format(d);
}

export function relative(epoch: number | null | undefined, now = Date.now()): string {
	if (!epoch) return DASH;
	const seconds = Math.round(now / 1000 - epoch);
	if (seconds < 45) return 'just now';
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) return `${minutes} min ago`;
	const hours = Math.round(minutes / 60);
	if (hours < 36) return `${hours} h ago`;
	const days = Math.round(hours / 24);
	if (days < 60) return `${days} days ago`;
	return epochDate(epoch);
}

/** Age of an open item, e.g. "3 h" or "2 d". */
export function age(epoch: number | null | undefined, now = Date.now()): string {
	if (!epoch) return DASH;
	const minutes = Math.max(0, Math.round((now / 1000 - epoch) / 60));
	if (minutes < 60) return `${minutes} min`;
	const hours = Math.round(minutes / 60);
	if (hours < 48) return `${hours} h`;
	return `${Math.round(hours / 24)} d`;
}

export function duration(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined || seconds < 0) return DASH;
	if (seconds < 1) return '<1 s';
	if (seconds < 90) return `${Math.round(seconds)} s`;
	return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

export function bytes(n: number): string {
	if (n < 1024) return `${n} B`;
	if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
	return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

/** snake_case backend codes as sentence-case text. */
export function humanize(value: string | null | undefined): string {
	if (!value) return DASH;
	const text = value.replace(/[_:]+/g, ' ').trim();
	return text.charAt(0).toUpperCase() + text.slice(1);
}

export const shortId = (id: string | null | undefined) => (id ? id.slice(0, 8) : DASH);
