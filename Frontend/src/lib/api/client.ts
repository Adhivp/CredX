/**
 * The single HTTP path to the CredX API: base URL, bearer token, refresh, error
 * mapping, cancellation and a small GET cache. Every API module goes through request().
 */

import { clearSession, loadTokens, saveTokens } from './session';
import type { TokenResponse } from '../types';

const configured = (import.meta.env.PUBLIC_API_BASE_URL ?? '').trim().replace(/\/+$/, '');
/** API base including /api/v1. Empty configuration means same origin (dev proxy). */
export const API_BASE = configured || '/api/v1';
/** /health lives at the API root, outside /api/v1. */
export const HEALTH_URL = `${API_BASE.replace(/\/api\/v1$/, '')}/health`;

// ── Errors ───────────────────────────────────────────────────────────────

export class ApiError extends Error {
	constructor(
		readonly status: number,
		message: string,
		readonly detail?: unknown,
	) {
		super(message);
		this.name = 'ApiError';
	}
	get forbidden() {
		return this.status === 403;
	}
	get notFound() {
		return this.status === 404;
	}
}

/** The session could not be renewed; the user must sign in again. */
export class SessionExpiredError extends ApiError {
	constructor() {
		super(401, 'Your session has ended. Sign in again to continue.');
		this.name = 'SessionExpiredError';
	}
}

/** The API could not be reached at all (network failure, server down, proxy error). */
export class ApiUnavailableError extends ApiError {
	constructor() {
		super(0, 'The CredX API is not reachable. Check that the backend is running, then retry.');
		this.name = 'ApiUnavailableError';
	}
}

export const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';

function messageOf(body: unknown, status: number): string {
	const detail = (body as { detail?: unknown } | null)?.detail;
	if (typeof detail === 'string') return detail;
	if (Array.isArray(detail)) {
		return detail
			.map((d) => {
				const loc = Array.isArray(d?.loc) ? d.loc.filter((p: unknown) => p !== 'body' && p !== 'query').join('.') : '';
				return loc ? `${loc}: ${d.msg}` : d?.msg ?? String(d);
			})
			.join('; ');
	}
	if (status === 403) return 'Your role does not permit this action.';
	if (status === 404) return 'Not found, or outside your data scope.';
	if (status >= 500) return `The API failed to complete the request (${status}).`;
	return `Request failed (${status}).`;
}

// ── Session expiry hook ──────────────────────────────────────────────────

let onSessionExpired: () => void = () => {};
export function setSessionExpiredHandler(handler: () => void) {
	onSessionExpired = handler;
}

// ── Token refresh ────────────────────────────────────────────────────────

const REFRESH_MARGIN_MS = 30_000;
let refreshing: Promise<string> | null = null;

async function performRefresh(rejectedToken: string | null): Promise<string> {
	const current = loadTokens();
	if (!current) throw new SessionExpiredError();
	// Another tab (or an earlier call) may already have rotated the tokens.
	if (current.access_token !== rejectedToken && current.access_expires_at - Date.now() > REFRESH_MARGIN_MS) {
		return current.access_token;
	}
	let res: Response;
	try {
		res = await fetch(`${API_BASE}/auth/refresh`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
			body: JSON.stringify({ refresh_token: current.refresh_token }),
		});
	} catch {
		throw new ApiUnavailableError();
	}
	if (!res.ok) {
		clearSession();
		throw new SessionExpiredError();
	}
	return saveTokens((await res.json()) as TokenResponse).access_token;
}

function refresh(rejectedToken: string | null): Promise<string> {
	if (!refreshing) {
		const run = () => performRefresh(rejectedToken);
		const locked = typeof navigator !== 'undefined' && navigator.locks
			? navigator.locks.request('credx-token-refresh', run)
			: run();
		refreshing = locked.finally(() => {
			refreshing = null;
		});
	}
	return refreshing;
}

async function accessToken(): Promise<string> {
	const tokens = loadTokens();
	if (!tokens) throw new SessionExpiredError();
	if (tokens.access_expires_at - Date.now() <= REFRESH_MARGIN_MS) return refresh(tokens.access_token);
	return tokens.access_token;
}

// ── Requests ─────────────────────────────────────────────────────────────

export interface RequestOptions {
	method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
	query?: Record<string, string | number | boolean | null | undefined>;
	body?: unknown;
	form?: FormData;
	signal?: AbortSignal;
	/** Send without a bearer token (login, register). */
	anonymous?: boolean;
}

export function buildPath(path: string, query?: RequestOptions['query']): string {
	if (!query) return path;
	const params = new URLSearchParams();
	for (const [key, value] of Object.entries(query)) {
		if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
	}
	const qs = params.toString();
	return qs ? `${path}?${qs}` : path;
}

async function send(path: string, opts: RequestOptions, token: string | null): Promise<Response> {
	const headers: Record<string, string> = { Accept: 'application/json' };
	if (token) headers.Authorization = `Bearer ${token}`;
	let body: BodyInit | undefined;
	if (opts.form) body = opts.form;
	else if (opts.body !== undefined) {
		headers['Content-Type'] = 'application/json';
		body = JSON.stringify(opts.body);
	}
	try {
		return await fetch(`${API_BASE}${buildPath(path, opts.query)}`, {
			method: opts.method ?? 'GET',
			headers,
			body,
			signal: opts.signal,
		});
	} catch (error) {
		if (isAbort(error)) throw error;
		throw new ApiUnavailableError();
	}
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
	let token = opts.anonymous ? null : await accessToken().catch((error) => {
		if (error instanceof SessionExpiredError) onSessionExpired();
		throw error;
	});
	let res = await send(path, opts, token);
	if (res.status === 401 && !opts.anonymous) {
		try {
			token = await refresh(token);
		} catch (error) {
			if (error instanceof SessionExpiredError) onSessionExpired();
			throw error;
		}
		res = await send(path, opts, token);
		if (res.status === 401) {
			clearSession();
			onSessionExpired();
			throw new SessionExpiredError();
		}
	}
	if (res.status === 204) return undefined as T;
	const text = await res.text();
	let body: unknown = null;
	try {
		body = text ? JSON.parse(text) : null;
	} catch {
		body = null;
	}
	if (!res.ok) {
		// A proxy that cannot reach the backend answers 502/503/504 with a non-JSON body.
		if (res.status >= 502 && body === null) throw new ApiUnavailableError();
		throw new ApiError(res.status, messageOf(body, res.status), body);
	}
	return body as T;
}

// ── GET cache ────────────────────────────────────────────────────────────
// Navigations swap pages without reloading, so recently fetched reads are reused instead
// of being requested again. Mutations invalidate by path prefix.

const CACHE_TTL_MS = 60_000;
const cache = new Map<string, { at: number; value: Promise<unknown> }>();

export function cachedGet<T>(path: string, opts: Omit<RequestOptions, 'method' | 'body' | 'form'> = {}, ttl = CACHE_TTL_MS): Promise<T> {
	const key = buildPath(path, opts.query);
	const hit = cache.get(key);
	if (hit && Date.now() - hit.at < ttl) return hit.value as Promise<T>;
	// The shared promise is not tied to one page's AbortSignal; callers ignore late results.
	const value = request<T>(path, { query: opts.query });
	cache.set(key, { at: Date.now(), value });
	value.catch(() => cache.delete(key));
	return value;
}

export function invalidate(...prefixes: string[]): void {
	for (const key of [...cache.keys()]) {
		if (prefixes.some((p) => key.startsWith(p))) cache.delete(key);
	}
}

export const clearCache = () => cache.clear();

export async function checkHealth(): Promise<boolean> {
	try {
		const res = await fetch(HEALTH_URL, { headers: { Accept: 'application/json' } });
		return res.ok;
	} catch {
		return false;
	}
}
