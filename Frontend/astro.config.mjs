// @ts-check
import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
	output: 'static',
	vite: {
		server: {
			proxy: {
				'/api': {
					target: 'https://credxapi.development.catalystappsail.in/',
					changeOrigin: true,
				},
				'/health': {
					target: 'https://credxapi.development.catalystappsail.in/',
					changeOrigin: true,
				},
			},
		},
	},
});
