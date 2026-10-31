// @ts-check
import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
	output: 'server',
	vite: {
		server: {
			proxy: {
				'/api': {
					target: 'http://localhost:8000',
					changeOrigin: true,
				},
				'/health': {
					target: 'http://localhost:8000',
					changeOrigin: true,
				},
			},
		},
	},
});
