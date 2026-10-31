/**
 * Page lifecycle under Astro view transitions. A page's bundled script runs once, so it
 * registers an initialiser that runs on every `astro:page-load` for that page. Each run
 * gets an AbortSignal that fires when the user navigates away, so in-flight requests and
 * polling from the previous page never write into the next one.
 */

import { navigate } from 'astro:transitions/client';
import { SessionExpiredError } from './api/client';
import { me } from './api/auth';
import { hasSession } from './api/session';
import { html, mount } from './html';
import { NAV, navAllowed, roleLabel } from './permissions';
import type { User } from './types';
import { bindTips, errorState } from './ui';

export interface PageContext {
	user: User;
	signal: AbortSignal;
	params: URLSearchParams;
	main: HTMLElement;
}

export function signInUrl(reason?: 'expired' | 'signed-out') {
	const next = location.pathname + location.search;
	const params = new URLSearchParams();
	if (next && next !== '/') params.set('next', next);
	if (reason) params.set(reason === 'expired' ? 'expired' : 'signedout', '1');
	const qs = params.toString();
	return qs ? `/?${qs}` : '/';
}

export function definePage(id: string, init: (ctx: PageContext) => void | Promise<void>) {
	document.addEventListener('astro:page-load', async () => {
		if (document.body.dataset.page !== id) return;
		const controller = new AbortController();
		document.addEventListener('astro:before-swap', () => controller.abort(), { once: true });
		const main = document.getElementById('main') as HTMLElement;
		if (!hasSession()) {
			void navigate(signInUrl());
			return;
		}
		let user: User;
		try {
			user = await me();
		} catch (error) {
			if (error instanceof SessionExpiredError) return; // shell handles the redirect
			document.body.dataset.auth = 'ready';
			mount(main, html`<div class="page-state">${errorState(error)}</div>`);
			main.querySelector('[data-retry]')?.addEventListener('click', () => location.reload());
			return;
		}
		if (controller.signal.aborted) return;
		document.dispatchEvent(new CustomEvent('credx:user', { detail: user }));
		const entry = NAV.find((n) => n.page === id);
		if (entry && !navAllowed(user.role, entry.needs)) {
			mount(
				main,
				html`<div class="page-state"><div class="state state--empty"><p class="state__title">${entry.label} is not part of your role</p>
				<p class="state__body">You are signed in as ${roleLabel(user.role)}. A risk administrator can change your role if you need this area.</p>
				<a class="btn btn--primary btn--sm" href="/account">View your access</a></div></div>`,
			);
			return;
		}
		bindTips(main);
		await init({ user, signal: controller.signal, params: new URLSearchParams(location.search), main });
	});
}

export const $ = <T extends Element = HTMLElement>(selector: string, root: ParentNode = document) =>
	root.querySelector(selector) as T | null;
export const $$ = <T extends Element = HTMLElement>(selector: string, root: ParentNode = document) =>
	Array.from(root.querySelectorAll(selector)) as T[];

/** Replace the query string without a navigation (filters, tabs). */
export function setParams(updates: Record<string, string | number | null | undefined>) {
	const params = new URLSearchParams(location.search);
	for (const [k, v] of Object.entries(updates)) {
		if (v === null || v === undefined || v === '') params.delete(k);
		else params.set(k, String(v));
	}
	const qs = params.toString();
	history.replaceState(history.state, '', qs ? `${location.pathname}?${qs}` : location.pathname);
}

export { navigate };
