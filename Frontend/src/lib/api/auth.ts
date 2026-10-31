import { cachedGet, clearCache, invalidate, request } from './client';
import { clearSession, loadTokens, saveTokens, saveUser } from './session';
import type { TokenResponse, User } from '../types';

export async function login(email: string, password: string): Promise<User> {
	const tokens = await request<TokenResponse>('/auth/login', {
		method: 'POST',
		body: { email: email.trim().toLowerCase(), password },
		anonymous: true,
	});
	saveTokens(tokens);
	clearCache();
	return me(true);
}

export function register(email: string, fullName: string, password: string): Promise<User> {
	return request<User>('/auth/register', {
		method: 'POST',
		body: { email: email.trim().toLowerCase(), full_name: fullName.trim(), password },
		anonymous: true,
	});
}

export async function me(fresh = false): Promise<User> {
	if (fresh) invalidate('/users/me');
	const user = await cachedGet<User>('/users/me', {}, 5 * 60_000);
	saveUser(user);
	return user;
}

/** Revokes the server session, then forgets local tokens whatever the outcome. */
export async function logout(): Promise<void> {
	try {
		if (loadTokens()) await request<void>('/auth/logout', { method: 'POST' });
	} catch {
		// The session is cleared locally even when the API is unreachable or already expired.
	} finally {
		clearSession();
		clearCache();
	}
}
