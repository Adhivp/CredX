/**
 * Browser session for the bearer-token API. The backend issues short-lived access
 * tokens (≤10 min) and rotating refresh tokens; replaying an old refresh token revokes
 * the whole login, so tokens are shared across tabs in localStorage and refreshed under a
 * cross-tab lock (see client.ts).
 */

import type { TokenResponse, User } from '../types';

const TOKENS = 'credx.session';
const USER = 'credx.user';

export interface StoredTokens {
	access_token: string;
	refresh_token: string;
	/** Milliseconds since the epoch when the access token stops being accepted. */
	access_expires_at: number;
}

function read<T>(key: string): T | null {
	try {
		const raw = localStorage.getItem(key);
		return raw ? (JSON.parse(raw) as T) : null;
	} catch {
		return null;
	}
}

export function loadTokens(): StoredTokens | null {
	return read<StoredTokens>(TOKENS);
}

export function saveTokens(tokens: TokenResponse): StoredTokens {
	const stored: StoredTokens = {
		access_token: tokens.access_token,
		refresh_token: tokens.refresh_token,
		access_expires_at: Date.now() + tokens.expires_in * 1000,
	};
	localStorage.setItem(TOKENS, JSON.stringify(stored));
	return stored;
}

export function loadUser(): User | null {
	return read<User>(USER);
}

export function saveUser(user: User): void {
	localStorage.setItem(USER, JSON.stringify(user));
}

export function clearSession(): void {
	localStorage.removeItem(TOKENS);
	localStorage.removeItem(USER);
}

export const hasSession = () => loadTokens() !== null;

export const SESSION_KEY = TOKENS;
