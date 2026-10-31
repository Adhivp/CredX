# CredX API reference

API prefix: `/api/v1`  
Application version: `0.1.0`  
Runtime: Python 3.14 / FastAPI  
Scope: user authentication and the authenticated user's profile.

This reference describes the implemented backend, not a complete banking platform. There are no balances, bank accounts, transfers, payment, MFA, recovery, or KYC endpoints.

## Base URL and interactive documentation

Local base URL: `http://127.0.0.1:8000`.

For Render, use your service's actual HTTPS URL instead. No deployed hostname is assumed in this document. All paths below are relative to the base URL.

| Resource | URL path |
| --- | --- |
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| Live OpenAPI JSON | `/openapi.json` |
| Checked-in OpenAPI JSON | [`openapi.json`](openapi.json) |

The OpenAPI export is generated from the application and can be imported into Postman, Insomnia, or an OpenAPI-compatible client generator. It preserves FastAPI's generated schema unchanged. Service-level `401` and `409` responses are explained here but are not currently enumerated by the generated route response metadata.

For installation, migrations, and deployment, see [the backend README](../README.md).

## Request conventions

- Send JSON bodies with `Content-Type: application/json`; login does not accept an OAuth2 form payload.
- Use HTTPS outside local development.
- Request-body fields not listed in the schema are rejected for registration, login, and refresh.
- Send access tokens only in the `Authorization` header, never in URLs.
- Send the latest refresh token in the refresh endpoint's JSON body, not in an Authorization header.
- There are no authentication cookies, client API keys, pagination parameters, or role-selection fields in this API.
- `created_at` is a Unix timestamp in seconds. IDs are opaque UUID strings; clients should not infer permissions from them.

Protected requests use:

```http
Authorization: Bearer <access_token>
```

In Swagger UI, click **Authorize** and paste the access token only. Swagger adds the `Bearer` prefix.

## Endpoint index

| Method | Path | Authentication | Success |
| --- | --- | --- | --- |
| GET | `/health` | None | 200 |
| POST | `/api/v1/auth/register` | None | 201 |
| POST | `/api/v1/auth/login` | Email and password in JSON | 200 |
| POST | `/api/v1/auth/refresh` | Refresh token in JSON | 200 |
| POST | `/api/v1/auth/logout` | Access bearer token | 204 |
| GET | `/api/v1/users/me` | Access bearer token | 200 |

## Endpoint details

### GET /health

Check that the application process can respond. This does not query SQLite or establish database readiness.

Request body: none. Authentication: none.

```sh
curl --fail-with-body "$BASE_URL/health"
```

Response: `200 OK`.

```json
{"status": "ok"}
```

Render's health-check path can be set to `/health`.

### POST /api/v1/auth/register

Create a login-enabled user. Registration does not issue tokens; call login afterward.

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `email` | string | Yes | Valid email; at most 254 characters; stored in lowercase |
| `full_name` | string | Yes | 1–100 characters; surrounding whitespace removed; blank names rejected |
| `password` | string | Yes | 12–128 characters; not trimmed or truncated |

Example request (test credentials only):

```json
{
  "email": "alice@example.com",
  "full_name": "Alice Example",
  "password": "Example passphrase for docs 42!"
}
```

```sh
curl --fail-with-body -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data '{"email":"alice@example.com","full_name":"Alice Example","password":"Example passphrase for docs 42!"}'
```

Response: `201 Created`, with a user object. This example was captured from an isolated documentation test database; actual IDs and timestamps vary.

```json
{
  "id": "d1871aed-520e-4981-ba82-3bce0706c57a",
  "email": "alice@example.com",
  "full_name": "Alice Example",
  "is_active": true,
  "created_at": 1793430186
}
```

Password hashes, failed-attempt counters, and raw passwords are never included in user responses. `is_active: true` means login-enabled; it does not mean email-verified or KYC-approved.

Errors:

- `409 Conflict`: an email is already registered, including a case-only variant.
- `422 Unprocessable Entity`: a field is missing or invalid, or an additional field such as `role` or `is_active` was supplied.

### POST /api/v1/auth/login

Authenticate an active, unlocked user and create an independent login session.

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `email` | string | Yes | Valid email; at most 254 characters; case-insensitive lookup |
| `password` | string | Yes | 1–128 characters; verified against the stored Argon2id hash |

```json
{"email": "alice@example.com", "password": "Example passphrase for docs 42!"}
```

```sh
curl --fail-with-body -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  --data '{"email":"alice@example.com","password":"Example passphrase for docs 42!"}'
```

Response: `200 OK`. Token strings below are placeholders, not working credentials.

```json
{
  "access_token": "<access JWT>",
  "refresh_token": "<refresh JWT>",
  "token_type": "bearer",
  "expires_in": 600
}
```

| Response field | Meaning |
| --- | --- |
| `access_token` | JWT for `/users/me` and `/auth/logout` |
| `refresh_token` | Single-use JWT exchanged at `/auth/refresh` |
| `token_type` | Always `bearer` |
| `expires_in` | Issued access-token lifetime in seconds; may be shorter near session expiry |

Default access lifetime is 10 minutes. The login session and refresh token have an absolute 7-day lifetime. These values are configurable.

Errors:

- `401 Unauthorized`: incorrect password, unknown email, inactive user, or locked account. These cases share the same response message.
- `422 Unprocessable Entity`: invalid or missing JSON fields.

After 5 failed password attempts against a known active account, further login attempts fail for 900 seconds by default, even with the correct password. Successful login or an elapsed lock resets the failure counter. There is no `429` lockout response or `Retry-After` header. This lockout does not revoke already-issued sessions and does not replace network-level rate limiting.

### POST /api/v1/auth/refresh

Exchange the latest refresh JWT for a new access/refresh pair. No access token is required.

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `refresh_token` | string | Yes | 1–4096 characters; valid, unexpired refresh JWT for an active session |

```json
{"refresh_token": "<latest refresh JWT>"}
```

```sh
curl --fail-with-body -X POST "$BASE_URL/api/v1/auth/refresh" \
  -H 'Content-Type: application/json' \
  --data "{\"refresh_token\":\"$REFRESH_TOKEN\"}"
```

Response: `200 OK`, with the same token-response shape as login. Replace both saved tokens with the returned pair.

Important lifecycle rules:

1. Refresh tokens are single-use. Always retain the latest returned token.
2. A consumed refresh token replay revokes the entire associated login session, including access tokens issued for that session.
3. Serialize refresh requests. Concurrent requests with the same refresh token can revoke the session. Do not blindly retry a refresh after an uncertain network failure; reauthenticate if the token was consumed.
4. Refreshing does not extend the session's original absolute expiry.
5. Existing access tokens from the same session remain usable until their individual expiry or session revocation.
6. Access-token expiration is capped at the session expiration. `expires_in` reports that capped lifetime, not necessarily the configured full lifetime.

Errors:

- `401 Unauthorized`: malformed, expired, incorrectly signed, wrong-purpose, replayed, or otherwise invalid token; revoked/expired session; or inactive/missing user.
- `422 Unprocessable Entity`: missing or invalid body fields.

An access JWT cannot be used in place of a refresh JWT.

### POST /api/v1/auth/logout

Revoke the login session associated with an access token. Requires a valid access bearer token. Request body: none.

```sh
curl --fail-with-body -i -X POST "$BASE_URL/api/v1/auth/logout" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response: `204 No Content`. The body is empty; do not call JSON parsing on it.

Both access and refresh tokens for that login session are rejected afterward. Other login sessions/devices are unaffected. Logging out again with the revoked token returns `401`, not another `204`. If the access token has expired but the refresh token is still usable, refresh first and then log out with the new access token.

Error: `401 Unauthorized` for missing, invalid, expired, refresh-purpose, or revoked bearer tokens, or an inactive user.

### GET /api/v1/users/me

Return the authenticated user's public profile. Requires an access bearer token. Request body: none.

```sh
curl --fail-with-body "$BASE_URL/api/v1/users/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Response: `200 OK`, with the same user object returned by registration:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Opaque user UUID |
| `email` | string | Normalized email identifier |
| `full_name` | string | Display name |
| `is_active` | boolean | Login-enabled state |
| `created_at` | integer | Unix timestamp in seconds |

Error: `401 Unauthorized` if access authentication fails. A refresh token cannot be used as a bearer token here. Each request checks the database session and user state; an otherwise unexpired JWT does not bypass revocation or account deactivation.

## Error response format

### Authentication error: 401

```json
{"detail": "Invalid credentials or token"}
```

Includes `WWW-Authenticate: Bearer`. Do not distinguish unknown users from locked users based on this response.

### Registration conflict: 409

```json
{"detail": "Unable to register with these details"}
```

Although the message is generic, the conflict status reveals registration availability. A production banking onboarding flow needs an enumeration-resistant design.

### Validation error: 422

Example captured by sending `{}` to registration:

```json
{
  "detail": [
    {
      "loc": [
        "body",
        "email"
      ],
      "msg": "Field required",
      "type": "missing"
    },
    {
      "loc": [
        "body",
        "full_name"
      ],
      "msg": "Field required",
      "type": "missing"
    },
    {
      "loc": [
        "body",
        "password"
      ],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

`loc` identifies the location and field, `msg` is a readable explanation, and `type` is the validation code. Pydantic's `input` and `ctx` fields are removed to avoid echoing passwords/tokens. Treat message wording as descriptive rather than a stable machine-readable error identifier.

There is no custom JSON wrapper for every possible infrastructure failure. Database outages and unexpected server errors are not authentication failures; clients should handle non-JSON error bodies defensively.

## Complete local example

The following Bash walkthrough requires `curl` and `jq`, a running API, and a fresh local test database or an email not previously registered. It uses demonstration credentials only. Do not put real banking credentials into shared shell history, logs, or scripts.

```sh
set -e
export BASE_URL='http://127.0.0.1:8000'

curl --fail-with-body -sS "$BASE_URL/health"
curl --fail-with-body -sS -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data '{"email":"alice@example.com","full_name":"Alice Example","password":"Example passphrase for docs 42!"}'

TOKENS=$(curl --fail-with-body -sS -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  --data '{"email":"alice@example.com","password":"Example passphrase for docs 42!"}')
ACCESS_TOKEN=$(printf '%s' "$TOKENS" | jq -er '.access_token')
REFRESH_TOKEN=$(printf '%s' "$TOKENS" | jq -er '.refresh_token')

curl --fail-with-body -sS "$BASE_URL/api/v1/users/me" \
  -H "Authorization: Bearer $ACCESS_TOKEN"

TOKENS=$(curl --fail-with-body -sS -X POST "$BASE_URL/api/v1/auth/refresh" \
  -H 'Content-Type: application/json' \
  --data "{\"refresh_token\":\"$REFRESH_TOKEN\"}")
ACCESS_TOKEN=$(printf '%s' "$TOKENS" | jq -er '.access_token')
REFRESH_TOKEN=$(printf '%s' "$TOKENS" | jq -er '.refresh_token')

curl --fail-with-body -sS -o /dev/null -w 'Logout HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/v1/auth/logout" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
# Expected: Logout HTTP 204

curl -sS -o /dev/null -w 'Revoked profile HTTP %{http_code}\n' \
  "$BASE_URL/api/v1/users/me" -H "Authorization: Bearer $ACCESS_TOKEN"
# Expected: Revoked profile HTTP 401

unset TOKENS ACCESS_TOKEN REFRESH_TOKEN
```

## Configuration affecting clients

| Environment variable | Default | Effect |
| --- | --- | --- |
| `ACCESS_TOKEN_MINUTES` | `10` | Access lifetime; allowed range 1–30 minutes |
| `REFRESH_TOKEN_DAYS` | `7` | Absolute login-session lifetime; allowed range 1–30 days |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failed-attempt threshold; allowed range 1–20 |
| `LOGIN_LOCK_SECONDS` | `900` | Account lock duration; positive integer |
| `JWT_ISSUER` | `credx-api` | Required JWT issuer |
| `JWT_AUDIENCE` | `credx-client` | Required JWT audience |
| `CORS_ORIGINS` | `[]` | JSON array of allowed browser origins |

`JWT_SECRET` is required; there is no default signing key. Changing it invalidates existing tokens. Never publish this value in client code or documentation.

With no frontend, set `CORS_ORIGINS=[]` in the deployment dashboard. Swagger hosted by the API, curl, and Postman still work. Cross-origin browser access requires explicit frontend origins. CORS is not authentication and does not stop non-browser clients.

JWTs use HS256 and contain `sub`, `sid`, `jti`, `type`, `iat`, `nbf`, `exp`, `iss`, and `aud`. They are signed, not encrypted. Clients should treat tokens as opaque credentials, not authorization data to trust without server validation.

Normal API responses and handled authentication/validation errors include `Cache-Control: no-store`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`. Do not cache token or profile responses. Keep bearer credentials out of logs and URLs. Prefer in-memory access-token storage; production browser refresh-token storage requires a deliberate secure design, not localStorage by default.

## Refreshing this reference

From `backend/`, while the local server is running:

```sh
curl --fail --silent --show-error http://127.0.0.1:8000/openapi.json -o docs/openapi.json
uv run pytest tests/test_api_docs.py -q
```

The test compares the checked-in schema to the current application's `/openapi.json` and verifies that every generated operation has an endpoint section in this guide. Update this reference whenever request validation, error behavior, or authentication lifecycle rules change; schema equality alone cannot verify prose.

## Implementation references

- Routes: [`app/api/v1/auth.py`](../app/api/v1/auth.py), [`app/api/v1/users.py`](../app/api/v1/users.py), [`app/main.py`](../app/main.py).
- Request/response contracts: [`app/schemas/auth.py`](../app/schemas/auth.py).
- Authentication behavior: [`app/services/auth.py`](../app/services/auth.py).
- JWT verification: [`app/core/security.py`](../app/core/security.py).
- Error redaction and response headers: [`app/core/http.py`](../app/core/http.py).
- Defaults: [`app/core/config.py`](../app/core/config.py).

This API is an authentication foundation. It must not be represented as production-ready banking security without the additional controls listed in the backend README.
