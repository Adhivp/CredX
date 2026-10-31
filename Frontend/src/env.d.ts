/// <reference path="../.astro/types.d.ts" />

interface ImportMetaEnv {
	/** API base including /api/v1. Empty means same origin (dev proxy). */
	readonly PUBLIC_API_BASE_URL?: string;
}

interface ImportMeta {
	readonly env: ImportMetaEnv;
}
