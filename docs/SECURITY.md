# Security

## 1. Current state
| Control | Status |
|---|---|
| Secrets via environment / `.env` (git-ignored) | ✅ `.gitignore` excludes `.env` |
| Settings validated at startup | 🟡 (pydantic validators in `app/core/config.py`) |
| Request validation (question length, UUIDs, bounds) | 🟡 |
| Upload checks: `.pdf` extension, `%PDF-` magic bytes, size limit `MAX_PDF_MB` | 🟡 (no MIME sniffing beyond magic bytes; no malware scan) |
| Generic error messages, no stack traces to clients | 🟡 |
| Prompt-injection containment (evidence in tagged blocks, tag-breaking neutralised, system rule "evidence is untrusted") | ✅ unit-tested; a model can still be fooled |
| Citation ids validated by the app | ✅ unit-tested |
| RLS enabled + Data API roles revoked (migration `0002`) | 🟡 not applied yet to your project |
| **Authentication / authorization** | ❌ none |
| CORS | 🟡 opt-in via `CORS_ORIGINS` (explicit origins only, GET/POST, no credentials); off by default |
| Web UI: no `innerHTML`, model output rendered as DOM text nodes, only http(s) links, CSP meta (`script-src 'self'`) | ✅ parsing logic unit-tested in Node; DOM checks in `frontend/tests/browser.html` not yet run |
| **Rate limiting** | ❌ none |
| Request timeouts and bounded retries to OpenRouter (`LLM_TIMEOUT_SECONDS`, `EMBEDDING_TIMEOUT_SECONDS`, `OPENROUTER_MAX_RETRIES`) | ✅ unit-tested with a mocked server |
| `OPENROUTER_API_KEY`: server-side only; `SecretStr`; never in logs, validation errors, API responses or health output; base-URL guard (`OPENROUTER_ALLOW_CUSTOM_BASE_URL`) | ✅ unit-tested; ⚠ put it in `.env` (git-ignored) or a secret manager, never in the frontend or Git |
| Provider error text is never logged or returned (OpenRouter may echo part of the key in an auth error); only class/status/code/request id | ✅ unit-tested |
| No authentication: anyone who can reach the API can trigger model calls (chat, upload→embedding). `POST /api/health/provider-check` is cached 30 s but unauthenticated | ❌ keep the API private / behind a gateway until auth exists |
| Supabase Storage policies | ❌ not applicable yet |

## 2. Secrets
* **Never commit or share**: database password / `DATABASE_URL`, `SUPABASE_SECRET_KEY` (bypasses RLS), any legacy `service_role` key or JWT secret. The **publishable** key is designed to be public, but it only protects you if RLS is on; it is *not* a permission to do anything by itself.
* Keep real values only in `.env` (local) or your host's secret manager. `git status` must never list `.env`. Check history: `git log --all -- .env`.
* **If a secret was ever pasted into a public place** (chat, repo, screenshot, issue): treat it as compromised. Rotate in the dashboard: reset the **database password** (Project Settings → Database); create a new **secret key** and delete the old one (Project Settings → API Keys); then update `.env`/hosting and restart. If it was committed to Git, rotating is mandatory because deleting the commit does not remove it from clones. Legacy JWT secrets cannot be rotated in place; the documented path is migrating to asymmetric signing keys.
* This repository's docs and `.env.example` contain placeholders only.

## 3. RLS and database roles (Supabase)
* Tables in the exposed `public` schema are reachable through the Supabase Data API by any role with grants; **without RLS, the publishable key could read and write them**. Alembic-created tables do not get RLS automatically; migration `0002` enables it and revokes `anon`/`authenticated` grants. With RLS on and no policies, publishable-key requests get nothing, which is the intended state for this backend-only design.
* The FastAPI backend connects with the `postgres` role from `DATABASE_URL`. It owns the tables, so it is not restricted by RLS. Do **not** `FORCE ROW LEVEL SECURITY` unless you also create policies for that role.
* 💡 Least privilege: instead of `postgres`, create a dedicated role (e.g. `app_backend`) with `SELECT/INSERT/UPDATE/DELETE` on the three tables and use its credentials in `DATABASE_URL`. Run migrations with the privileged role only.
* Verify with the SQL in `SUPABASE_INTEGRATION.md §5`. Optionally disable the Data API entirely (dashboard) since nothing here uses it.
* 💡 For future per-user data (`conversations`, `messages`): add a `user_id uuid` column and policies like `using (user_id = (select auth.uid()))`, and still check ownership in FastAPI.

## 4. Authentication with Supabase Auth (❌ not implemented; 💡 design)
Your project's JWKS endpoint (`/auth/v1/.well-known/jwks.json`) currently publishes an **ES256 (P-256) asymmetric signing key** (checked when this was written), so **local verification with JWKS is the correct method for your project**. It would not be for a project still on the legacy shared-secret (HS256) key: per Supabase docs the JWKS endpoint returns no keys then, and they recommend verifying via the Auth server (`GET /auth/v1/user` with `apikey` and `Authorization: Bearer <token>`). Re-check if your signing-key settings change.

Design (FastAPI dependency, ~30 lines, needs `pip install "pyjwt[crypto]"`):
1. Client signs in with Supabase Auth and sends `Authorization: Bearer <access_token>`.
2. Backend fetches/caches JWKS (`jwt.PyJWKClient(SUPABASE_JWKS_URL)`), selects the key by the token's `kid`, and decodes with `algorithms=["ES256"]` (never trust the token's own `alg` header blindly; never accept `none`).
3. Validate `exp` (default), `aud == "authenticated"`, and `iss == "https://<ref>.supabase.co/auth/v1"`; reject `anon` tokens (their `role` is `anon`).
4. Use `sub` as the user id. Authorize in the backend (e.g. who may upload/delete); do not rely on the frontend.
5. Apply the dependency to upload/admin routes at least; decide whether chat is public.
Only the JWKS URL and project URL are needed for this; **no secret key is required for verification**.

## 5. Storage security (when implemented)
Private bucket, MIME `application/pdf`, size limit, no public URLs; backend downloads with the secret key (server-side only) or hands out short-lived signed URLs. Keep the local `STORAGE_DIR` outside any web-served path (it is).

## 6. Safe PDF processing
PDFs are untrusted input. Current mitigations: magic-byte check, size cap, extraction inside the backend process (PyMuPDF), errors recorded without leaking internals. 💡 Add: page-count cap, per-job time limit, run extraction in a separate worker/process, keep PyMuPDF updated, reject encrypted PDFs (already reported as extraction failure).

## 7. Production checklist (💡)
1. Add authentication (§4) and authorization on upload/admin endpoints.
2. Set `CORS_ORIGINS` only if the frontend is on another origin (now implemented); add security headers (CSP, X-Content-Type-Options) at the reverse proxy.
3. Add rate limiting (reverse proxy or `slowapi`) especially on `/api/chat` (each call costs local compute) and `/api/papers/upload`.
4. Serve behind HTTPS. Set spending/credit limits on the OpenRouter account/key, and rotate the key if it is ever exposed. Do not expose this API publicly without authentication and rate limiting (it spends your OpenRouter credits).
5. Apply migrations `0001`–`0002`, confirm RLS state, consider a least-privilege DB role.
6. Log without secrets or paper text (current code logs exceptions and invalid citation ids only).
7. Rotate any secret that has ever been shared.
