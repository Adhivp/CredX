/**
 * CredX Banking API Client
 * Backend: https://credx-auwn.onrender.com
 * Documented in backend/docs/API.md
 */

// Prefer relative path in dev if Vite proxy is used, fallback to direct HTTPS URL
export const BACKEND_URL = 'https://credx-auwn.onrender.com';

export interface UserProfile {
	id: string;
	email: string;
	full_name: string;
	is_active: boolean;
	created_at: number;
}

export interface AuthTokens {
	access_token: string;
	refresh_token: string;
	token_type: string;
	expires_in: number;
	stored_at?: number;
}

export interface ApiError {
	detail: string | Array<{ loc: string[]; msg: string; type: string }>;
}

const STORAGE_KEY_TOKENS = 'credx_auth_tokens';
const STORAGE_KEY_USER = 'credx_user_profile';

export class CredxApi {
	private baseUrl: string;

	constructor(baseUrl: string = BACKEND_URL) {
		this.baseUrl = baseUrl.replace(/\/+$/, '');
	}

	/**
	 * Build full endpoint URL
	 */
	private url(endpoint: string): string {
		const cleanEndpoint = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
		// If running in browser and hitting same origin via proxy
		if (typeof window !== 'undefined' && window.location.origin && window.location.hostname === 'localhost') {
			// Can use direct URL or proxy
			return `${this.baseUrl}${cleanEndpoint}`;
		}
		return `${this.baseUrl}${cleanEndpoint}`;
	}

	/**
	 * Check health of backend API
	 */
	async checkHealth(): Promise<{ status: string }> {
		const res = await fetch(this.url('/health'), {
			method: 'GET',
			headers: { 'Accept': 'application/json' },
		});
		if (!res.ok) throw new Error(`Health check failed with status ${res.status}`);
		return res.json();
	}

	/**
	 * Login with email and password
	 * POST /api/v1/auth/login
	 */
	async login(email: string, password: string): Promise<{ tokens: AuthTokens; user: UserProfile }> {
		const res = await fetch(this.url('/api/v1/auth/login'), {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'Accept': 'application/json',
			},
			body: JSON.stringify({ email: email.trim().toLowerCase(), password }),
		});

		if (!res.ok) {
			const errorData: ApiError = await res.json().catch(() => ({ detail: 'Login failed. Please check credentials.' }));
			const message = typeof errorData.detail === 'string'
				? errorData.detail
				: Array.isArray(errorData.detail)
				? errorData.detail.map((e) => e.msg).join(', ')
				: 'Invalid credentials or inactive account.';
			throw new Error(message);
		}

		const tokens: AuthTokens = await res.json();
		tokens.stored_at = Date.now();
		this.saveTokens(tokens);

		// Fetch authenticated user profile
		const user = await this.getMe(tokens.access_token);
		this.saveUser(user);

		return { tokens, user };
	}

	/**
	 * Register a new user
	 * POST /api/v1/auth/register
	 * Requirements: email, full_name (1-100 chars), password (12-128 chars)
	 */
	async register(email: string, fullName: string, password: string): Promise<UserProfile> {
		const res = await fetch(this.url('/api/v1/auth/register'), {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'Accept': 'application/json',
			},
			body: JSON.stringify({
				email: email.trim().toLowerCase(),
				full_name: fullName.trim(),
				password,
			}),
		});

		if (!res.ok) {
			const errorData: ApiError = await res.json().catch(() => ({ detail: 'Registration failed.' }));
			if (res.status === 409) {
				throw new Error('This email address is already registered in CredX.');
			}
			const message = typeof errorData.detail === 'string'
				? errorData.detail
				: Array.isArray(errorData.detail)
				? errorData.detail.map((e) => `${e.loc.slice(-1)}: ${e.msg}`).join(', ')
				: 'Registration validation failed.';
			throw new Error(message);
		}

		return res.json();
	}

	/**
	 * Get current authenticated user profile
	 * GET /api/v1/users/me
	 */
	async getMe(accessToken?: string): Promise<UserProfile> {
		const token = accessToken || this.getAccessToken();
		if (!token) throw new Error('No access token available.');

		const res = await fetch(this.url('/api/v1/users/me'), {
			method: 'GET',
			headers: {
				'Authorization': `Bearer ${token}`,
				'Accept': 'application/json',
			},
		});

		if (res.status === 401) {
			// Token might be expired, attempt refresh
			const refreshed = await this.refreshToken().catch(() => null);
			if (refreshed) {
				return this.getMe(refreshed.access_token);
			}
			this.clearStorage();
			throw new Error('Session expired. Please log in again.');
		}

		if (!res.ok) {
			throw new Error('Failed to fetch user profile.');
		}

		const user: UserProfile = await res.json();
		this.saveUser(user);
		return user;
	}

	/**
	 * Refresh access token using refresh token
	 * POST /api/v1/auth/refresh
	 */
	async refreshToken(): Promise<AuthTokens> {
		const currentTokens = this.getTokens();
		if (!currentTokens?.refresh_token) {
			this.clearStorage();
			throw new Error('No refresh token available.');
		}

		const res = await fetch(this.url('/api/v1/auth/refresh'), {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'Accept': 'application/json',
			},
			body: JSON.stringify({ refresh_token: currentTokens.refresh_token }),
		});

		if (!res.ok) {
			this.clearStorage();
			throw new Error('Session invalid or revoked. Please sign in again.');
		}

		const newTokens: AuthTokens = await res.json();
		newTokens.stored_at = Date.now();
		this.saveTokens(newTokens);
		return newTokens;
	}

	/**
	 * Revoke session and log out
	 * POST /api/v1/auth/logout
	 */
	async logout(): Promise<void> {
		const token = this.getAccessToken();
		if (token) {
			try {
				await fetch(this.url('/api/v1/auth/logout'), {
					method: 'POST',
					headers: {
						'Authorization': `Bearer ${token}`,
					},
				});
			} catch (e) {
				// Ignore network errors on logout
			}
		}
		this.clearStorage();
	}

	// Storage Utilities
	saveTokens(tokens: AuthTokens): void {
		if (typeof window === 'undefined') return;
		localStorage.setItem(STORAGE_KEY_TOKENS, JSON.stringify(tokens));
	}

	getTokens(): AuthTokens | null {
		if (typeof window === 'undefined') return null;
		const raw = localStorage.getItem(STORAGE_KEY_TOKENS);
		if (!raw) return null;
		try {
			return JSON.parse(raw);
		} catch {
			return null;
		}
	}

	getAccessToken(): string | null {
		return this.getTokens()?.access_token || null;
	}

	saveUser(user: UserProfile): void {
		if (typeof window === 'undefined') return;
		localStorage.setItem(STORAGE_KEY_USER, JSON.stringify(user));
	}

	getUser(): UserProfile | null {
		if (typeof window === 'undefined') return null;
		const raw = localStorage.getItem(STORAGE_KEY_USER);
		if (!raw) return null;
		try {
			return JSON.parse(raw);
		} catch {
			return null;
		}
	}

	clearStorage(): void {
		if (typeof window === 'undefined') return;
		localStorage.removeItem(STORAGE_KEY_TOKENS);
		localStorage.removeItem(STORAGE_KEY_USER);
		sessionStorage.removeItem('credx_user');
	}

	isAuthenticated(): boolean {
		return !!this.getAccessToken();
	}
}

export const api = new CredxApi();
