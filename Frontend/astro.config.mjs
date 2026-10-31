// @ts-check
import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
	vite: {
		server: {
			proxy: {
				'/api': {
					target: 'https://credx-auwn.onrender.com',
					changeOrigin: true,
					secure: true,
				},
				'/health': {
					target: 'https://credx-auwn.onrender.com',
					changeOrigin: true,
					secure: true,
				},
			},
		},
	},
});
