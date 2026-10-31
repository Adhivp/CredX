# CredX frontend

Early-warning workspace for credit risk teams: portfolio overview, customer watchlist,
customer risk detail, investigation workspace, review centre, portfolio concentration,
data upload and administration.

Built with Astro 4 as static pages. Every page is rendered in the browser from the
authenticated CredX API (`backend/`); the frontend holds no business data of its own.

## Run locally

Start the API first (see `backend/README.md`), on port 8000:

```sh
cd backend
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

The committed `backend/data/credx.db` predates the migration chain, so `alembic upgrade
head` fails on it; set `DATABASE_URL` to a new SQLite file (for example
`sqlite:///./data/local.db`) for a fresh database.

Then the frontend:

```sh
cd Frontend
npm install
npm run dev          # http://localhost:4321
```

In development the Astro server proxies `/api` and `/health` to
`CREDX_API_PROXY_TARGET` (default `http://localhost:8000`), so no CORS setup is needed.

Accounts: self-registration creates a user with no role. Create the first
administrator with `python -m scripts.create_user ... --role risk_admin` in `backend/`,
then assign roles and data scopes from Administration. Email addresses must use a
deliverable domain; the API rejects reserved domains such as `.local` and `.test` at
sign-in.

## Scripts

| Command | Does |
| --- | --- |
| `npm run dev` | Development server with the API proxy |
| `npm run check` | Type-check Astro and TypeScript |
| `npm run build` | Type-check, then build static files to `dist/` |
| `npm run preview` | Serve `dist/` locally (no API proxy) |

## Configuration

Copy `.env.example` to `.env` when needed.

- `PUBLIC_API_BASE_URL`: API base including `/api/v1`. Leave empty for same-origin
  requests (development proxy, or a deployment that serves the API on the same origin).
  For a separate API origin, set it and add the site's origin to the backend's
  `CORS_ORIGINS`. `PUBLIC_` values are shipped to the browser; never put secrets here.
- `CREDX_API_PROXY_TARGET`: development proxy target only.

## Structure

```text
src/
  layouts/AppShell.astro   head, navigation, user menu, view transitions
  pages/                   one file per screen; detail pages take ?id=
  lib/api/                 HTTP client (tokens, refresh, errors, cache) and endpoint modules
  lib/permissions.ts       mirror of backend/app/core/rbac.py, for presentation only
  lib/workspace.ts         investigation workspace and every human-decision flow
  lib/charts.ts            dependency-free SVG/HTML charts with table views
  lib/ui.ts, lib/html.ts   states, dialogs, toasts; escaping HTML templates
  styles/                  tokens, base, components, charts, pages
```

The backend enforces every permission and data scope; hiding a control here is a
courtesy, not a security boundary.
