import { defineConfig } from 'astro/config';
import { loadEnv } from 'vite';

// Every page is rendered in the browser from authenticated API responses (the backend
// issues bearer tokens, not cookies), so the site is built as static files.
const env = loadEnv(process.env.NODE_ENV ?? 'development', process.cwd(), '');
// Dev-only: where the Vite dev server forwards same-origin /api and /health requests.
const proxyTarget = env.CREDX_API_PROXY_TARGET || 'http://localhost:8000';

// https://astro.build/config
export default defineConfig({
	output: 'static',
	vite: {
		server: {
			proxy: {
				'/api': { target: proxyTarget, changeOrigin: true },
				'/health': { target: proxyTarget, changeOrigin: true },
			},
		},
	},
});
