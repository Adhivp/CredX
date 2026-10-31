# CredX API reference

API prefix: `/api/v1`  
Application version: `0.1.0`  
Runtime: Python 3.14 / FastAPI  
Scope: user authentication, the authenticated user's profile, and credit early warning system (EWS) data ingestion, assessment, watchlists, alerts, and explanations.

This reference describes the implemented backend, not a complete banking platform. EWS stores imported loan balances and credit-risk data; it does not provide bank accounts, transfers, payments, MFA, recovery, or KYC endpoints.

**Security boundary: every `/api/v1/ews/*` operation is currently unauthenticated.** EWS has no per-user/tenant authorization; reads and writes act on shared data. An access token is required only for `/api/v1/users/me` and `/api/v1/auth/logout`. Do not expose EWS to untrusted clients or upload real customer data without an external access-control boundary. Supplying a bearer token does not add ownership checks to EWS.

## Base URL and interactive documentation

Local base URL: `http://127.0.0.1:8000`.

For a deployed service, use its actual HTTPS URL instead. No deployed hostname is assumed in this document. All paths below are relative to the base URL.

| Resource | URL path |
| --- | --- |
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| Live OpenAPI JSON | `/openapi.json` |
| Checked-in OpenAPI JSON | [`openapi.json`](openapi.json) |

The OpenAPI export is generated from the application and can be imported into Postman, Insomnia, or an OpenAPI-compatible client generator. It preserves FastAPI's generated schema unchanged. Runtime `400`, `401`, `404`, and `409` responses are explained here but are not currently enumerated by the generated route response metadata. Assessment and portfolio responses have no declared response model, so their generated success schemas are unconstrained. Alert decision values are validated at runtime, not represented as an OpenAPI enum.

For installation, migrations, and deployment, see [the backend README](../README.md).

## Request conventions

- Send JSON bodies with `Content-Type: application/json` for registration, login, refresh, and alert classification. Login does not accept an OAuth2 form payload. EWS upload uses `multipart/form-data` with a `file` part and a `data_type` query parameter; let your HTTP client set the multipart boundary.
- Use HTTPS outside local development.
- Request-body fields not listed in the schema are rejected for registration, login, refresh, and alert classification. Uploaded tables use separate, less strict column checks; unused columns are ignored.
- Send access tokens only in the `Authorization` header, never in URLs.
- Send the latest refresh token in the refresh endpoint's JSON body, not in an Authorization header.
- There are no authentication cookies, client API keys, or role-selection fields. The EWS watchlist has `limit`/`offset` pagination.
- `created_at` and `assessed_at` are Unix timestamps in seconds. Internal record IDs are opaque UUID strings. EWS route `customer_id` values are external identifiers imported from your data, not user IDs or internal customer UUIDs. See the EWS identifier notes below.

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
| POST | `/api/v1/ews/upload` | None | 200 (inspect `status`) |
| POST | `/api/v1/ews/assess` | None | 200 |
| GET | `/api/v1/ews/watchlist` | None | 200 |
| GET | `/api/v1/ews/customer/{customer_id}` | None | 200 |
| POST | `/api/v1/ews/alert/{assessment_id}` | None | 200 |
| POST | `/api/v1/ews/explain/{customer_id}` | None | 200 |
| GET | `/api/v1/ews/portfolio` | None | 200 |
| POST | `/api/v1/ews/monitoring/history/rebuild` | None | 200 |
| GET | `/api/v1/ews/monitoring/customer/{customer_id}/trend` | None | 200 |
| GET | `/api/v1/ews/monitoring/priority` | None | 200 |
| GET | `/api/v1/ews/monitoring/dashboard` | None | 200 |
| GET | `/api/v1/ews/workspace/{customer_id}` | None | 200 |
| POST | `/api/v1/ews/workspace/{customer_id}/briefing` | None | 200 |
| POST | `/api/v1/ews/cases` | Optional bearer (actor) | 201 |
| GET | `/api/v1/ews/cases` | None | 200 |
| PATCH | `/api/v1/ews/cases/{case_id}` | Optional bearer (actor) | 200 |
| POST | `/api/v1/ews/cases/{case_id}/notes` | Optional bearer (actor) | 201 |
| POST | `/api/v1/ews/cases/{case_id}/info-requests` | Optional bearer (actor) | 201 |
| PATCH | `/api/v1/ews/info-requests/{activity_id}` | Optional bearer (actor) | 200 |
| POST | `/api/v1/ews/cases/{case_id}/classification` | Optional bearer (actor) | 200 |
| POST | `/api/v1/ews/repayments/{repayment_id}/classification` | Optional bearer (actor) | 200 |
| DELETE | `/api/v1/ews/repayments/{repayment_id}/classification` | Optional bearer (actor) | 200 |
| POST | `/api/v1/ews/interventions` | Optional bearer (actor) | 201 |
| GET | `/api/v1/ews/interventions` | None | 200 |
| PATCH | `/api/v1/ews/interventions/{iv_id}` | Optional bearer (actor) | 200 |
| POST | `/api/v1/ews/interventions/{iv_id}/approve` | Access bearer token (not the proposer) | 200 |

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

Use `/health` as the process health-check path; it is not a database readiness check.

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

## Early warning system (EWS)

All operations in this section require **no authentication**, including uploads, assessments, and alert classification. They share one data store across callers. There are no authenticated analyst identities or per-customer ownership checks.

Identifier conventions:

- Uploads, assessment filters, customer/explanation routes, watchlists, and portfolio summaries use external `customer_id` values such as `DOC-C001`.
- `customer.id`, `loans[].customer_id`, and `assessments[].customer_id` in customer detail are internal customer UUIDs. `customer.customer_id` is the external identifier.
- Uploaded `loan_id` values are external identifiers. Returned `loans[].id` and `repayments[].loan_id` are internal loan UUIDs.
- Alert routes use the internal assessment UUID from `assessments[].id` in customer detail. The assessment-run response does **not** return this ID.

### POST /api/v1/ews/upload

Import a table. Required query parameter: `data_type`, one of `customers`, `loans`, `repayments`, `exposure`, or `cashflow`. Required multipart field: `file`. No bearer token is required.

```sh
curl --fail-with-body -X POST "$BASE_URL/api/v1/ews/upload?data_type=customers" \
  -F 'file=@customers.csv'
```

The parser selects its format from the filename extension (case-insensitive), not the MIME type: `.csv`, `.xlsx`, `.xls`, or `.json`. JSON must be a table readable by pandas, for example an array of row objects. Excel uses pandas' default first sheet. `.xlsx` is supported by the installed `openpyxl`; `.xls` may fail without an additional pandas-compatible engine such as `xlrd`, which is not a declared dependency.

| `data_type` | Required columns | Optional columns used |
| --- | --- | --- |
| `customers` | `customer_id`, `customer_type` | `employment_category`, `portfolio_segment` |
| `loans` | `customer_id`, `loan_id`, `product_type`, `sanctioned_amount`, `outstanding_balance` | `tenure_months`, `instalment_amount`, `disbursement_date`, `status` |
| `repayments` | `customer_id`, `loan_id`, `due_date`, `amount_due`, `amount_paid`, `repayment_status` | `payment_date`, `dpd` |
| `exposure` | `customer_id`, `facility_type`, `outstanding_balance` | `sanctioned_amount`, `credit_limit`, `current_utilisation`, `instalment_amount`, `disbursement_date`, `snapshot_date` |
| `cashflow` | `customer_id`, `month`, `monthly_inflow` | `monthly_outflow`, `record_date` |

Numeric fields are converted with Python `float()`/`int()`; `month`, `dpd`, and `tenure_months` are integers. Use ISO date strings such as `2026-01-01` for date columns. Dates and status values are not validated by the upload route. There is no row-level Pydantic validation, nonnegative-value enforcement, or explicit application upload-size limit. Omitted numeric fields generally default to zero, text fields to empty strings, and loan `status` to `active`; `payment_date` and `monthly_outflow` can be null. Avoid blank numeric cells: omitted columns and missing cell values are not reliably interchangeable.

Import customers → loans → repayments; upload exposure/cashflow after customers exist:

- Customers are inserted or updated by external ID. Updating a customer without optional text columns resets those fields to empty strings.
- Loans create a default customer if needed. Existing external loan IDs are skipped, not updated.
- Repayments append records for existing external loan IDs; unknown loans are silently skipped. The required `customer_id` column is not used to cross-check the loan's owner.
- Exposure and cashflow append records for existing external customer IDs; unknown customers are silently skipped. Re-uploading append-only data duplicates records. Uploaded `facility_id` is not consumed.

Response: `200 OK`, `DataUploadResponse` with `upload_id` (UUID), `filename`, `records_loaded` (integer), `status`, and `errors` (string array). Success uses `status: "success"` and `errors: []`; zero loaded records can still be successful. `records_loaded` counts inserted/updated rows, not necessarily the number supplied.

Missing required columns or an unknown `data_type` return **HTTP 200** with `status: "error"`, `records_loaded: 0`, and an explanation in `errors`. The failed upload metadata is recorded. Inspect the body rather than relying on HTTP status alone.

Errors: `400` with `{"detail":"Unsupported file format. Use CSV, Excel, or JSON."}` for an unrecognized extension; `422` for missing query/file fields. Parsing, conversion, missing Excel-engine, or database failures are not normalized to row-validation errors and can surface as `500`.

### POST /api/v1/ews/assess

Run synchronous rules and persist a new assessment for every selected customer. No request body or authentication. Optional query `customer_id` selects an external customer ID; omitted or empty selects all customers. Repeated calls append history; they do not replace assessments.

```sh
curl --fail-with-body -X POST "$BASE_URL/api/v1/ews/assess?customer_id=DOC-C001"
```

Response: `200 OK`, an object with `total_assessed` and an `assessments` array. Each item contains external `customer_id`, numeric `warning_score`, `risk_category`, `rule_delay_score`, `rule_miss_score`, `rule_exposure_score`, `rule_completeness_score`, `cashflow_score`, and `top_indicators` (string array). IDs/timestamps are retrieved separately through customer detail. The array has no guaranteed order.

Current score: `0.75 × model score + 0.25 × cashflow_score` when the cash-flow model ran (at least 3 distinct months of cash flow), otherwise the model score alone. The model score comes from `core/models/warning_score_model.py`, an XGBoost classifier trained only on Home Credit (all 307,511 customers, test AUC 0.678; see `core/models/saved/warning_score_model.metrics.json`). Its calibrated default probability ÷ the 8.07% baseline (lift) is mapped piecewise onto 0–100: lift 1.15 → 20, 1.35 → 45, 1.65 → 70, 4.0 → 100. When the model files are absent, the score falls back to `(0.30 × delay + 0.20 × miss + 0.25 × exposure + 0.25 × cashflow) / (sum of weights of the signals that ran)`. Rule scores are stored for explanation; they are not model inputs. Category: `Low` below 20, `Medium` from 20 to below 45, `High` from 45 to below 70, otherwise `Critical`.

**Insufficient data:** when the missing-history rule finds the file too thin to rate (completeness below 50, or fewer than 6 distinct instalment due dates), `risk_category` is `Insufficient Data` instead of a risk band. The provisional `warning_score` is still stored for audit but is not a risk rating, and `top_indicators` lead with the data gaps.

Each response item also carries `signals_used` (which of `delay`, `miss`, `exposure`, `cashflow` ran), `score_source` (`warning_score_model` or `rule_composite`), `model_warning_score` and `model_default_probability` (null when the model files are absent), and `model_top_contributors` (per-customer XGBoost feature contributions). The model's probability and main drivers are also added to `top_indicators`.

Inputs: repayments are de-duplicated per loan and due date, exposure rows per identical record, and cash flow per customer and month before scoring. The models load from the gitignored `core/models/saved/*.joblib`; the Docker image copies `core/rules` only, so deployed instances score without the cash-flow signal. Rule or model exceptions are logged and the remaining signals still produce `200`. A successful response is not proof that every rule ran or that the result is a validated lending decision.

Error: `404` with `{"detail":"No customers found"}` when no selected customers exist.

### GET /api/v1/ews/watchlist

Return latest assessments, sorted by descending warning score. No body or authentication.

| Query | Default | Behavior |
| --- | --- | --- |
| `risk_level` | null | Exact, case-sensitive category match, normally `Low`, `Medium`, `High`, `Critical`, `Insufficient Data`; not an enum validator |
| `customer_segment` | null | Exact match on customer `portfolio_segment` |
| `product_type` | null | **Echoed in filters only; currently does not filter results** |
| `min_score` | `0.0` | Numeric inclusive lower bound, no 0–100 validation |
| `limit` | `50` | Integer 1–1000 |
| `offset` | `0` | Integer ≥ 0 |

Response: `200 OK`, `WatchlistResponse`: `items`, `total` (matching rows before pagination), and `filters`. Each item has external `customer_id`, `product_type`, `outstanding_exposure`, `warning_score`, `risk_category`, up to three `top_indicators`, integer `active_signals`, and `monitoring_status` (`"Active"`, or `"Insufficient Data"` for thin-file customers). Product and exposure come from the single loan with the largest outstanding balance, **not** summed facilities or uploaded exposure records; absent loans yield `""` and `0.0`.

`active_signals` counts delay, miss, exposure, and cashflow scores strictly greater than 20; it excludes completeness. `filters.max_results` echoes the effective `limit`, alongside the four filter values; there is no returned `offset` or `has_more`. No assessed customers yields an empty array and total zero. Invalid numeric pagination/filter inputs return `422`.

Latest selection joins on second-resolution `assessed_at`: multiple assessments for the same customer in the same latest second can appear more than once and inflate `total`. Portfolio aggregation shares this limitation. Ordering among tied scores is unspecified.

### GET /api/v1/ews/customer/{customer_id}

Fetch a customer's history by **external** customer ID. No body or authentication. Response: `200 OK`, `CustomerDetailOut`:

| Field | Contents |
| --- | --- |
| `customer` | `id`, external `customer_id`, `customer_type`, `employment_category`, `portfolio_segment`, `created_at` |
| `loans` | Internal `id`/`customer_id`, external `loan_id`, `product_type`, `sanctioned_amount`, `outstanding_balance`, `tenure_months`, `instalment_amount`, `disbursement_date`, `status` |
| `repayments` | Internal `id`/`loan_id`, `due_date`, nullable `payment_date`, `amount_due`, `amount_paid`, `dpd`, `repayment_status`; ordered by stored due-date string ascending |
| `assessments` | Internal `id`/`customer_id`, warning/category and all rule/cashflow scores, `top_indicators` string array, `assessed_at`; descending timestamp |
| `alerts` | Alert objects described below, descending `created_at` |

Arrays can be empty and are not paginated. Loan ordering and ordering among equal timestamps are unspecified. Uploaded exposure/cashflow rows are **not** returned. Error: `404` with `{"detail":"Customer not found"}`.

### POST /api/v1/ews/alert/{assessment_id}

Append an analyst classification to an assessment UUID obtained from customer detail. No authentication. `analyst_id` is a caller-supplied label, **not a verified user identity**.

| JSON field | Required | Rules |
| --- | --- | --- |
| `decision` | Yes | Exactly `Continue Monitoring`, `Requires Investigation`, `False Positive`, or `Escalate for Review` |
| `reason` | Yes | String, 1–2000 characters; not trimmed (whitespace-only is accepted) |
| `analyst_id` | No | String or null; defaults to null |

Additional JSON fields are rejected. Example body:

```json
{"decision":"Requires Investigation","reason":"Review the supplied repayment history.","analyst_id":"docs-analyst"}
```

Response: `200 OK` (not `201`), `AlertOut`: `id`, `assessment_id`, `decision`, `reason`, nullable `analyst_id`, `original_score`, `original_category`, `preserved_evidence`, and integer `created_at`. Evidence is an object containing `top_indicators` and `rule_scores` with `delay`, `miss`, `exposure`, `completeness`, and `cashflow` values. Classification preserves the assessment's score/evidence; it does not alter the assessment, remove the customer from the watchlist, or change its `monitoring_status`. Repeated posts append more alerts.

Errors: `404` with `{"detail":"Assessment not found"}`; `422` for invalid decisions, missing/invalid fields, or extra fields. Decision error wording/order is not stable.

### POST /api/v1/ews/explain/{customer_id}

Explain the latest stored assessment for an **external** customer ID. No request body or authentication. This does not run an assessment or persist an explanation.

Response: `200 OK`, `ExplainResponse`: external `customer_id`; `explanation` (plain-text summary); `risk_factors` and `recommended_actions` (string arrays); `data_status` (`assessable` or `insufficient`); `source` (`ai` or `fallback`); `model` (model ID when `source` is `ai`, else null); and `contributing_factors` (array of objects with `signal` and numeric `score`). Signals are `repayment_delay`, `miss_classification`, `exposure`, and `cashflow`, included only when their score is greater than zero; `data_completeness` is excluded because it is a data-quality score, not a risk score. Treat the narrative as advisory, not independently validated risk reasoning.

With no `NEBIUS_API_KEY`, the service uses a local template. With a key, it requests a JSON-mode explanation through the configured Nebius endpoint/model (default `deepseek-ai/DeepSeek-V4-Flash-0731`, with thinking disabled for latency and a 20-second timeout). Provider failures fall back to the template and still return `200` with `source: "fallback"`. A configured provider receives a prompt containing the customer identifier, warning score/category, all rule scores, the stored indicators, and repayment facts (record count, late count, max DPD, first/last due dates, loan count, total outstanding, whether income data exists). Review data-sharing permissions before enabling it. Repeated requests can make repeated external calls; no response cache is implemented.

Errors: `404` with `{"detail":"Customer not found"}` or `{"detail":"No assessment found for customer"}`. Equal latest timestamps have no deterministic tiebreaker.

### GET /api/v1/ews/portfolio

Return a shared-portfolio summary. No query parameters, body, or authentication. Response: `200 OK`:

| Field | Contents |
| --- | --- |
| `total_customers` | Count of all imported customers, including unassessed customers |
| `total_assessed` | Count of latest-assessment rows, subject to the timestamp-tie caveat above |
| `average_warning_score` | Mean of rated assessment scores (excluding `Insufficient Data`), rounded to two decimals; `0.0` if none |
| `risk_distribution` | Category-to-count object with `Low`, `Medium`, `High`, `Critical`, `Insufficient Data` initialized to zero |
| `top_risky_customers` | Up to five highest-score rated rows (excluding `Insufficient Data`): external `customer_id`, `warning_score`, `risk_category`, and up to three `top_indicators` |

This is not a sum of loan/exposure amounts. An empty database returns zero counts/scores and an empty top-customer array.

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

## Isolated EWS walkthrough

Run this Python example from `backend/` with the development dependencies installed (`uv run python`) and an optional `TMPDIR` pointing to a writable scratch directory. It uses the real app with FastAPI's TestClient and a disposable SQLite database, without dotenv, real credentials, or external AI calls. It sends no Authorization header, demonstrating the current unprotected EWS boundary. `tests/test_api_docs.py` executes this exact code block.

<!-- ews-walkthrough -->
```python
from fastapi.testclient import TestClient

from scripts.export_openapi import isolated_app

uploads = {
    "customers": (
        "customer_id,customer_type,portfolio_segment\n"
        "DOC-C001,individual,docs\n"
    ),
    "loans": (
        "customer_id,loan_id,product_type,sanctioned_amount,outstanding_balance\n"
        "DOC-C001,DOC-L001,term,10000,8000\n"
    ),
    "repayments": (
        "customer_id,loan_id,due_date,amount_due,amount_paid,repayment_status,dpd\n"
        "DOC-C001,DOC-L001,2026-01-01,100,100,on_time,0\n"
    ),
    "exposure": (
        "customer_id,facility_type,outstanding_balance\n"
        "DOC-C001,term,8000\n"
    ),
    "cashflow": "customer_id,month,monthly_inflow\nDOC-C001,1,1500\n",
}

with isolated_app() as app, TestClient(app) as client:
    assert client.get("/api/v1/ews/portfolio").json()["total_customers"] == 0
    assert client.post("/api/v1/ews/assess").status_code == 404
    for data_type, csv in uploads.items():
        response = client.post(
            "/api/v1/ews/upload",
            params={"data_type": data_type},
            files={"file": (f"{data_type}.csv", csv.encode(), "text/csv")},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "success" and result["records_loaded"] == 1

    assert client.post("/api/v1/ews/explain/DOC-C001").status_code == 404
    response = client.post("/api/v1/ews/assess", params={"customer_id": "DOC-C001"})
    assert response.status_code == 200 and response.json()["total_assessed"] == 1
    assessment = response.json()["assessments"][0]
    assert assessment["cashflow_score"] == 0.0  # the cash-flow model needs >= 3 months
    assert assessment["risk_category"] == "Insufficient Data"  # one instalment is a thin file

    response = client.get("/api/v1/ews/customer/DOC-C001")
    assert response.status_code == 200
    detail = response.json()
    assert len(detail["loans"]) == len(detail["repayments"]) == 1
    assessment_id = detail["assessments"][0]["id"]
    response = client.post(
        f"/api/v1/ews/alert/{assessment_id}",
        json={"decision": "Requires Investigation", "reason": "Review imported data."},
    )
    assert response.status_code == 200
    alert = response.json()
    assert alert["analyst_id"] is None
    assert alert["original_score"] == assessment["warning_score"]
    assert isinstance(alert["preserved_evidence"], dict)
    assert client.get("/api/v1/ews/customer/DOC-C001").json()["alerts"][0] == alert

    response = client.get(
        "/api/v1/ews/watchlist", params={"product_type": "not-a-real-product", "limit": 1}
    )
    assert response.status_code == 200
    watchlist = response.json()
    assert watchlist["total"] == 1  # product_type is currently ignored by filtering
    assert watchlist["items"][0]["customer_id"] == "DOC-C001"
    assert watchlist["items"][0]["monitoring_status"] == "Insufficient Data"
    response = client.post("/api/v1/ews/explain/DOC-C001")
    assert response.status_code == 200 and response.json()["explanation"]
    response = client.get("/api/v1/ews/portfolio")
    assert response.status_code == 200 and response.json()["total_assessed"] == 1

    response = client.post(
        "/api/v1/ews/upload", params={"data_type": "customers"},
        files={"file": ("invalid.csv", b"customer_id\nDOC-C002\n", "text/csv")},
    )
    assert response.status_code == 200 and response.json()["status"] == "error"
    assert response.json()["records_loaded"] == 0 and response.json()["errors"]
    assert client.get("/api/v1/ews/watchlist?limit=0").status_code == 422
    assert client.get("/api/v1/ews/customer/unknown").status_code == 404
    assert client.get("/api/v1/users/me").status_code == 401

print("Isolated EWS walkthrough passed")
```

The external AI success/failure paths are not exercised by this offline example. It verifies the local fallback only. Use synthetic data and a controlled provider configuration when testing external explanations.

## Configuration affecting clients

| Environment variable | Default | Effect |
| --- | --- | --- |
| `ACCESS_TOKEN_MINUTES` | `10` | Access lifetime; allowed range 1–30 minutes |
| `REFRESH_TOKEN_DAYS` | `7` | Absolute login-session lifetime; allowed range 1–30 days |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failed-attempt threshold; allowed range 1–20 |
| `LOGIN_LOCK_SECONDS` | `900` | Account lock duration; positive integer |
| `JWT_ISSUER` | `credx-api` | Required JWT issuer |
| `JWT_AUDIENCE` | `credx-client` | Required JWT audience |
| `CORS_ORIGINS` | `[]` | Parsed setting, currently **not used** by the app middleware |
| `NEBIUS_API_KEY` | Empty | Enables external AI explanations when configured; never send this key from clients |
| `NEBIUS_BASE_URL` | `https://api.tokenfactory.us-central1.nebius.com/v1/` | Explanation provider endpoint |
| `NEBIUS_MODEL` | `deepseek-ai/DeepSeek-V4-Flash-0731` | Explanation model |

`JWT_SECRET` is required; there is no default signing key. Changing it invalidates existing tokens. Never publish this value in client code or documentation.

**Current CORS caveat:** `create_app()` hard-codes `allow_origins=["*"]`, `allow_credentials=True`, and all methods/headers. Changing `CORS_ORIGINS` does not currently restrict browser origins. Do not mistake this permissive middleware for access control; CORS never authenticates non-browser clients. This document records current behavior, not a recommended production policy.

JWTs use HS256 and contain `sub`, `sid`, `jti`, `type`, `iat`, `nbf`, `exp`, `iss`, and `aud`. They are signed, not encrypted. Clients should treat tokens as opaque credentials, not authorization data to trust without server validation.

Normal API responses and handled authentication/validation errors include `Cache-Control: no-store`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`. Do not cache token or profile responses. Keep bearer credentials out of logs and URLs. Prefer in-memory access-token storage; production browser refresh-token storage requires a deliberate secure design, not localStorage by default.

## Refreshing this reference

From `backend/`, with Python 3.14 dependencies installed (optionally set `TMPDIR` to a writable scratch directory):

```sh
uv run python -m scripts.export_openapi
uv run pytest tests/test_api_docs.py -q
uv run ruff check scripts/export_openapi.py tests/test_api_docs.py
```

The exporter calls the actual `create_app()` factory and `app.openapi()` with a temporary SQLite database under `TMPDIR` (otherwise `RUNNER_TEMP`, then `~/.cache/credx`), synthetic signing material, an isolated environment, and dotenv disabled. It neither reads `.env` nor uses the tracked database; it disables the external AI key and disposes/removes its database afterward. No live server is required. Use it in a dedicated development process, not inside a concurrently running server.

The test compares the checked-in schema to the current application's `/openapi.json` and verifies that every generated operation has an endpoint section in this guide. Update this reference whenever request validation, error behavior, or authentication lifecycle rules change; schema equality alone cannot verify prose.


## Level 2: risk monitoring and interventions

No endpoint below changes a loan, a credit limit or contacts a customer: interventions record human actions, and customer-impacting ones need approval by a second signed-in user.

### POST /api/v1/ews/monitoring/history/rebuild

Replays the assessment pipeline at each of the last 12 month ends on the data known at that date and stores one risk snapshot per customer per month. Optional `customer_id` query. `POST /ews/assess` already calls this.

### GET /api/v1/ews/monitoring/customer/{customer_id}/trend

Risk history (snapshots, oldest first), trend analysis (`improving` / `stable` / `deteriorating`, deltas, indicator drivers, rule and model contribution changes) and multi-signal correlation.

### GET /api/v1/ews/monitoring/priority

Prioritised customer queue. Query: `sort` (priority, warning_score, exposure, expected_loss, probability, delinquency, trend, segment), `product`, `segment`, `portfolio`, `period`, `trend`, `delinquency` (delinquent, pre_delinquent, current), `level`, `tier`, `quadrant`, `limit`. Each item separates `probability_of_deterioration` from impact (`ead`, `loss_if_default`, `expected_loss`).

### GET /api/v1/ews/monitoring/dashboard

Portfolio early-warning dashboard. Query: `product`, `segment`, `portfolio`, `period` (YYYY-MM as-of month), `window` (2-12 months). Returns KPIs, customers and exposure by warning level, delinquent vs pre-delinquent split, DPD trend series, worsening customers, outstanding interventions and breakdowns.

### GET /api/v1/ews/workspace/{customer_id}

Investigation workspace: profile, facilities, latest assessment, trend, correlation, priority, recommendations, case, interventions, activity trail, information requests and repayment events with any analyst classification.

### POST /api/v1/ews/workspace/{customer_id}/briefing

LLM case briefing (Nebius) written only from the computed facts; replies containing numbers not in the facts are rejected and a template briefing is returned (`source`: ai | template).

### POST /api/v1/ews/cases

Open an investigation case. Body: `customer_id`, `owner`, `owner_role` (relationship_manager, risk_analyst, collections, credit_committee), `summary`, `actor`. 409 when a case is already open.

### GET /api/v1/ews/cases

List cases. Query: `status` (a status or `open`), `owner`.

### PATCH /api/v1/ews/cases/{case_id}

Update `status`, `disposition` (pending, accepted, false_positive), `owner`, `owner_role`, with an optional `note`. Closing needs a note and no open interventions. Every change is logged.

### POST /api/v1/ews/cases/{case_id}/notes

Add an investigation note. Body: `body`, `kind` (note, decision, evidence), `actor`.

### POST /api/v1/ews/cases/{case_id}/info-requests

Request additional information. Body: `items` (list), `requested_from`, `due_date`, `message`, `actor`. Moves an open case to `awaiting_information`.

### PATCH /api/v1/ews/info-requests/{activity_id}

Resolve an information request. Body: `status` (open, fulfilled, cancelled), `response`, `actor`.

### POST /api/v1/ews/cases/{case_id}/classification

Correct the warning level. Body: `corrected_category` (Low, Medium, High, Critical, Insufficient Data, or null to clear), `reason`, `actor`. The model score is kept on record.

### POST /api/v1/ews/repayments/{repayment_id}/classification

Classify a repayment event. Body: `classification` (processing_error, bank_holiday, data_error exclude the event from scoring; customer_delay, disputed, confirmed_miss keep it), `reason`, `case_id`, `actor`. The raw record is unchanged; the customer is reassessed and `score_before` / `score_after` returned.

### DELETE /api/v1/ews/repayments/{repayment_id}/classification

Remove an event classification and reassess the customer.

### POST /api/v1/ews/interventions

Accept a recommendation as an intervention. Body: `customer_id`, `scenario` (temporary_missed_instalment, repeated_repayment_delays, declining_cash_flow, increasing_exposure, sustained_high_risk, restructuring_review, insufficient_information), `case_id`, `facility_ref`, `owner`, `owner_role`, `priority`, `target_date`, `notes`, `actor`. Customer-impacting scenarios start as `pending_approval`; others as `planned`.

### GET /api/v1/ews/interventions

List interventions. Query: `status` (a status or `open`), `owner`, `customer_id`, `overdue`.

### PATCH /api/v1/ews/interventions/{iv_id}

Update `status` (planned → initiated / deferred / cancelled; initiated → completed / deferred / escalated; ...), `owner`, `owner_role`, `priority`, `target_date`, `notes`, `decision`, `outcome`, `evidence`. Completing needs an outcome; deferring, cancelling or escalating needs a reason. 409 on an invalid move.

### POST /api/v1/ews/interventions/{iv_id}/approve

Authorised approval of a customer-impacting intervention. Requires an access bearer token for a user other than the proposer. Body: `approve` (bool), `comment`. 401 without a token, 403 for the proposer.

## Implementation references

- Routes: [`app/api/v1/auth.py`](../app/api/v1/auth.py), [`app/api/v1/users.py`](../app/api/v1/users.py), [`app/api/v1/ews.py`](../app/api/v1/ews.py), [`app/main.py`](../app/main.py).
- Request/response contracts: [`app/schemas/auth.py`](../app/schemas/auth.py), [`app/schemas/ews.py`](../app/schemas/ews.py).
- EWS persistence and explanation behavior: [`app/models/ews.py`](../app/models/ews.py), [`app/services/explainability.py`](../app/services/explainability.py).
- Authentication behavior: [`app/services/auth.py`](../app/services/auth.py).
- JWT verification: [`app/core/security.py`](../app/core/security.py).
- Error redaction and response headers: [`app/core/http.py`](../app/core/http.py).
- Defaults: [`app/core/config.py`](../app/core/config.py).

This API combines an authentication foundation with an unprotected EWS prototype. It is not production-ready banking security: EWS authorization, restrictive CORS, input hardening, and operational controls remain outside this documentation update.
