# CredX backend

Modular FastAPI authentication starter for the CredX monorepo. Everything lives in `backend/`; no frontend or root-level project configuration is required. The backend targets Python 3.14 (`>=3.14,<3.15`); both uv and pip installations were exercised on CPython 3.14.9. `backend/.python-version` selects the 3.14 series for uv.

This is a development foundation, **not a production banking system**. There are no account balances, transfers, KYC, or payment APIs.

## Quick start with uv

Run from the monorepo root:

```sh
cd backend
uv sync --locked
uv run python scripts/init_env.py
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

`init_env.py` generates a cryptographically random JWT signing key in the ignored `backend/.env` (mode 0600 on POSIX), never prints the key, and never overwrites an existing file. Configuration is loaded from this file, regardless of working directory. Environment variables override it. See `.env.example` for optional settings.

Open http://127.0.0.1:8000/docs for Swagger UI, http://127.0.0.1:8000/redoc for ReDoc, or http://127.0.0.1:8000/openapi.json for the schema.

## Alternative: standard pip, without uv

Choose one environment for normal development; using both is not necessary. The two paths use the same resolved dependency versions.

```sh
cd backend
python3.14 -m venv .venv-pip
. .venv-pip/bin/activate
python -m pip install --only-binary=:all: --require-hashes -r requirements-dev.txt
python scripts/init_env.py
python -m alembic upgrade head
python -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

For runtime dependencies only, install `requirements.txt` instead of `requirements-dev.txt`. On Windows, use `py -3.14 -m venv .venv-pip` and `.venv-pip\Scripts\Activate.ps1`. These commands run from `backend/`, so the `app` package does not require an editable pip install.

`pyproject.toml` is the dependency source of truth. `uv.lock` and the hash-pinned pip exports are checked-in reproducibility artifacts. Never edit generated requirements independently. After intentionally changing dependencies:

```sh
uv lock
uv sync --locked
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
uv export --locked --all-groups --no-emit-project --format requirements-txt --output-file requirements-dev.txt
```

## Render deployment (Python 3.14)

Configure a Python web service with **Root Directory** set to `backend`.

Environment variables:

```text
PYTHON_VERSION=3.14.9
CORS_ORIGINS=[]
```

Keep `JWT_SECRET` as a stable, cryptographically random secret of at least 48 characters in Render's environment settings. Do not regenerate it on every start. In the dashboard, enter `[]` as the CORS value without outer single quotes; no frontend URL is required.

Build command (pip):

```sh
python -m pip install --only-binary=:all: --require-hashes -r requirements.txt
```

Start command:

```sh
python -m alembic upgrade head && python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT
```

Health check path: `/health`.

Alternatively, build with `uv sync --locked --no-dev` and prefix the start commands with `uv run --no-sync` instead of `python -m`. Choose one installation path, not both.

After committing and pushing the Python upgrade, use Render's **Clear build cache & deploy** so an older virtual environment or requirements export is not reused. Confirm Python 3.14 in the build log. The regenerated requirements include Python 3.14 wheel hashes. The pip `--only-binary=:all:` option forbids source compilation: if no compatible wheel exists for a different platform, installation fails explicitly rather than trying Rust/Cargo in a read-only directory. All locked runtime and development packages installed as wheels on Linux x86-64 / CPython 3.14.9 during verification.

Mount a Render persistent disk and set `DATABASE_URL` to a SQLite file under that mount (for example `sqlite:////var/data/credx.db` for a `/var/data` mount). Without persistent storage, redeployments can discard registered users and sessions. Creating a persistent disk or changing Render's remote settings is not performed by this repository upgrade.

## Layout

```text
backend/
  app/
    main.py                  application factory and router composition
    api/dependencies.py      database, configuration and bearer-auth dependencies
    api/v1/auth.py           registration, login, refresh and logout routes
    api/v1/users.py          authenticated profile route
    core/config.py           validated environment configuration
    core/security.py         Argon2id and strict JWT encode/decode
    core/http.py             safe validation errors and response headers
    db/session.py            SQLAlchemy sessions and SQLite connection settings
    models/                  users and revocable authentication sessions
    schemas/auth.py          request/response validation
    services/auth.py          authentication logic and transactions
  migrations/                Alembic versioned database changes
  scripts/init_env.py         safe local-secret initialization
  tests/                     isolated API/security/migration/setup tests
  data/credx.db              local SQLite database (generated, ignored)
  .env                       local secrets (generated, ignored)
  pyproject.toml / uv.lock
  requirements.txt / requirements-dev.txt
```

## API v1

All request bodies below are JSON. Authentication is `Authorization: Bearer <access_token>`, not cookies. No token or password belongs in a URL.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | Process liveness; not database readiness |
| POST | `/api/v1/auth/register` | Create a user; 201 |
| POST | `/api/v1/auth/login` | Return access/refresh JWTs; 200 |
| POST | `/api/v1/auth/refresh` | Rotate refresh JWT; 200 |
| POST | `/api/v1/auth/logout` | Revoke current login session using access JWT; 204 |
| GET | `/api/v1/users/me` | Return the authenticated user's public profile; 200 |

Register:

```json
{
  "email": "alice@example.com",
  "full_name": "Alice Example",
  "password": "Use a unique long passphrase!"
}
```

Passwords must be 12–128 characters; they are not trimmed or silently truncated. This is length validation, not a breached-password check. Email identifiers are normalized to lowercase. Duplicate email registration returns 409. Invalid inputs return 422 without Pydantic's input/context fields, which can otherwise leak passwords. Additional fields such as `is_active` or `role` are rejected.

Login:

```json
{"email": "alice@example.com", "password": "Use a unique long passphrase!"}
```

The response contains `access_token`, `refresh_token`, `token_type: "bearer"`, and `expires_in` (access lifetime in seconds). Default access lifetime is 10 minutes; the login session expires after 7 days. Tokens contain opaque identifiers and standard time/issuer/audience claims, not profile data. JWTs are signed, not encrypted.

To call `/users/me` from Swagger, copy the access token from login, click **Authorize**, and paste the token (without adding `Bearer` yourself).

Refresh:

```json
{"refresh_token": "<latest refresh JWT>"}
```

Always replace the stored refresh token with the new one. Rotation is single-use and uses a database compare-and-swap. Reusing an older refresh token revokes that entire login session, including its access tokens. Clients must serialize refresh attempts: parallel refreshes or retrying a consumed token can revoke the session. The 7-day expiry is absolute, not extended by refreshing. Access tokens issued earlier in the same session remain usable until their own expiry or session revocation.

Logout requires the access token and has no body. It revokes that login session only, not all devices. Missing/invalid/expired tokens, disabled users, and revoked sessions receive 401. JWT algorithm, issuer, audience, required claims, and token purpose are checked. Refresh tokens cannot be used as access tokens.

## SQLite and migrations

The default database is anchored to `backend/data/credx.db`. Override with `DATABASE_URL=sqlite:////absolute/path/credx.db` if needed. Relative SQLite URLs are relative to the process working directory. This starter intentionally supports SQLite only, and is tested with file-backed databases. Use a local disk, not a network-shared database file.

```sh
uv run alembic upgrade head
uv run alembic current
uv run alembic check
# After intentional model edits, generate and REVIEW a migration:
uv run alembic revision --autogenerate -m "describe schema change"
```

Application startup does not silently create or modify tables; run migrations first. Tests use disposable databases, and the migration test exercises upgrade, model/schema comparison, and downgrade. Do not run downgrade on valuable data without a verified backup. Back up SQLite with its supported backup API or while the application is quiescent; do not assume copying a live database and sidecars is atomic.

## Configuration and security boundaries

- `JWT_SECRET` is required, minimum 48 characters. Use the generator; length alone cannot establish entropy. There is no default signing key. Changing the key invalidates all existing tokens.
- Argon2id hashes passwords. SQLite stores only SHA-256 digests of refresh JWTs, never raw tokens or plaintext passwords.
- Every authenticated request checks the database session and active-user flag, enabling immediate logout/replay revocation.
- Failed login attempts persist in SQLite. After 5 failures a known account is locked for 900 seconds. Wrong passwords, unknown users, locked accounts, and inactive users receive the same generic 401 response. Successful login or an elapsed lock resets the counter. Unknown-user password verification uses a dummy Argon2 hash.
- This per-account lockout is **not global rate limiting** and can itself be abused to deny access to an account. Add trusted edge/IP throttling, request-size/concurrency limits, and monitoring before exposure. Do not trust arbitrary client forwarding headers.
- Public registration returns 409 for duplicates, so email existence is discoverable. Banking onboarding needs an enumeration-resistant verification workflow before public deployment. `is_active` here only means login-enabled, not email-verified or KYC-approved.
- CORS denies cross-origin browser access by default. For local frontend development set `CORS_ORIGINS=["http://localhost:3000"]` in `.env`, then restart. Only explicitly configured origins are allowed. CORS is not an authorization boundary.
- Auth/profile responses use `Cache-Control: no-store`. Avoid logging request bodies, credentials, or Authorization headers at the application and proxy layers.
- The API returns bearer tokens in JSON. Keep access tokens in memory; do not recommend localStorage for banking tokens. A browser production deployment needs a deliberate secure refresh-token storage design (e.g. a backend-for-frontend with HttpOnly/Secure cookies and CSRF protection).
- Before real banking use: add phishing-resistant MFA/passkeys and step-up authentication, verified onboarding, breached-password checks, password reset/change and account recovery, tamper-resistant audit logging, fraud/abuse controls, session/device management, TLS, secret management/key rotation, database access controls/encrypted storage/backups, independent security review, and applicable compliance controls. Protect database files and their parent directory with restrictive OS permissions. SQLite is not encrypted by default and is not a high-concurrency banking datastore.
- Schedule expired-session cleanup before long-running deployment. This starter retains expired/revoked session records; no automatic retention policy is configured.

## Verification

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pip-audit -r requirements.txt --disable-pip --no-deps
```

Pip equivalents: `python -m pytest -q`, `python -m ruff check .`, and `python -m pip check` inside `.venv-pip`. The locked Starlette test client uses `httpx2`, included only in development dependencies.

No live bank integration, external email delivery, deployment, or frontend is configured.
