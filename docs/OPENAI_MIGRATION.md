# Migration: local Ollama + BGE → OpenAI API

> ⚠ **SUPERSEDED.** The backend now uses **OpenRouter** (`OPENROUTER_API_KEY`, `nvidia/nemotron-3.5-super-vl:free`, `nvidia/nemotron-3.5-embed-1b:free`).
> Read [docs/OPENROUTER_MIGRATION.md](OPENROUTER_MIGRATION.md) for the current configuration and runbook. This document is kept only as
> historical context for the earlier Ollama/BGE → OpenAI migration; the environment-variable names, model ids and module names it
> describes no longer exist in the code.

One server-side `OPENAI_API_KEY` now powers **both** answer generation (`gpt-4.1-mini`, Chat Completions) and embeddings
(`text-embedding-3-small`, 1536 dimensions by default). Nothing runs locally any more: no Ollama, no model downloads, no
torch / sentence-transformers. Public API contracts (`/api/chat`, papers, jobs) are unchanged; `/api/health` fields changed (below).

> OpenAI usage is **billed separately** to your OpenAI account under its current pricing and usage limits. Check your
> account's pricing page before running a large re-index (`python -m scripts.reindex_embeddings --status` prints how much text is pending).

## What changed

| Area | Before | After |
|---|---|---|
| Chat | `OllamaClient` (`/api/chat`, qwen2.5:3b) | `OpenAIChatModel` (`app/services/providers/openai_chat.py`), JSON mode |
| Embeddings | `HuggingFaceEmbedder` (BGE, local, 1024-dim, query instruction) | `OpenAIEmbedder` (`openai_embeddings.py`), same model + dimensions for documents and queries, no prefixes |
| Errors | Ollama-specific | `openai_common.translate_openai_error` → safe messages (`llm_auth_failed`, `llm_quota_exceeded`, `llm_rate_limited`, `model_not_available`, `embedding_*`) |
| Schema | `chunks.embedding vector(1024)` | `vector(EMBEDDING_DIMENSIONS)` (migration `0004`); old vectors kept in `embedding_prev` |
| Re-index | `scripts.reindex_embeddings` | same command; adds `--verify`, cost estimate in `--status`, DB lock, rate-limit backoff |
| Health | `ollama`, `chat_model`, `embedding_model` (cached/loaded), device, dtype | `llm`, `embedding` = `configured`/`not_configured` (no provider call); new `POST /api/health/provider-check` |
| Removed | `app/services/providers/{ollama,embeddings}.py`, `scripts/download_model.py`, torch, sentence-transformers, huggingface-hub | — |
| Added | — | `openai` SDK, `scripts/check_openai.py`, `app/services/provider_check.py`, `migrations/versions/0004_openai_embeddings.py` |

Ollama and BGE are **not** an optional mode: the app has no local fallback, and a provider failure is returned as an error, never silently routed elsewhere.

## Configuration (`.env`, backend only)

```env
OPENAI_API_KEY=                      # required at startup; never in the frontend, Git, logs or screenshots
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_ALLOW_CUSTOM_BASE_URL=false   # a non-OpenAI host is refused unless true (it would receive your key)
OPENAI_MAX_RETRIES=2                 # SDK retries: connection errors, 408/409/429/5xx, with backoff; 0-5, never infinite
LLM_PROVIDER=openai
LLM_MODEL=gpt-4.1-mini
LLM_TIMEOUT_SECONDS=60
LLM_MAX_OUTPUT_TOKENS=1000
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536            # must equal the DB column; legacy name EMBEDDING_DIM still read
EMBEDDING_BATCH_SIZE=64
```
Validated centrally in `app/core/config.py`. The key is a `SecretStr` (never in `repr`, validation errors, logs or responses),
is checked when the app (or a script that calls OpenAI) starts, **not** at import, so `alembic` works without it. Removed settings
(`OLLAMA_BASE_URL`, `LLM_NUM_CTX`, `EMBEDDING_DEVICE`, …) are ignored with a startup warning naming them.
`LLM_PROVIDER=ollama` / `EMBEDDING_PROVIDER=huggingface` / a Hugging Face `EMBEDDING_MODEL` fail validation with a message saying what to set.

## Vector-dimension strategy (and trade-offs)

BGE (1024) and OpenAI vectors are different vector spaces: **every chunk must be re-embedded**, whatever the dimension. Never truncated or padded.

* **Chosen: new `vector(1536)` column** (default). Full-size embeddings; old BGE vectors stay in `embedding_prev` (never searched) until you drop them, and `alembic downgrade 0003` restores them byte-for-byte. Cost: 1.5× vector storage, bigger HNSW index.
* **Alternative: `EMBEDDING_DIMENSIONS=1024`**. The API shortens vectors server-side (`dimensions` parameter), so the column stays 1024 wide; less storage/memory, slightly lower embedding quality for a given model (OpenAI documents shortening as a trade-off). Migration `0004` still creates a fresh column and still keeps the old vectors, so it is not a shortcut around re-indexing.
* Limit: `EMBEDDING_DIMENSIONS ≤ 2000` (pgvector HNSW). `text-embedding-3-large` therefore needs `dimensions` ≤ 2000.

## Production runbook

Retrieval returns **nothing** between step 3 and the end of step 6 (no chunk has a comparable vector yet). Plan a short maintenance window.

1. **Back up** the database (`pg_dump`). Stop other writers / ingestion if possible.
2. Set `OPENAI_API_KEY` and, if not 1536, `EMBEDDING_DIMENSIONS` **before** migrating (the migration reads it). Run `python -m scripts.check_openai --deep` (real requests, a fraction of a cent): proves the key, model access and quota.
3. `alembic upgrade head` (one transaction; applies `0004`). Confirm `alembic current` → `0004`.
4. `python -m scripts.reindex_embeddings --status` → total / pending / approximate tokens. Trial: `python -m scripts.reindex_embeddings --max-batches 2`.
5. `python -m scripts.reindex_embeddings` (resumable; Ctrl+C is safe; a DB lock stops a second copy running). Rate limits are waited out (5/15/45 s) then the run stops; quota/auth errors stop immediately. Re-run to resume.
6. `python -m scripts.reindex_embeddings --verify` → must print `PASS` (checks stored vector sizes in SQL, not just metadata).
7. Start the API; `GET /api/health` → `status: ok`; `POST /api/health/provider-check` → `ok: true`.
8. **Recalibrate `RETRIEVAL_THRESHOLD`** (`python -m scripts.calibrate_threshold questions.json`). Cosine scores differ between embedding models; the default 0.30 is a guess, not a measurement.
9. After you are satisfied: `ALTER TABLE chunks DROP COLUMN embedding_prev, DROP COLUMN embedding_prev_model;` (never done automatically; do not do it before step 8).

**Rollback** (before dropping `embedding_prev`): stop the API, `alembic downgrade 0003`, deploy the previous release (which expects `EMBEDDING_DIM=1024`). BGE vectors return exactly; chunks embedded since the upgrade become pending.

## Diagnosing problems

| Symptom / error code | Cause | Fix |
|---|---|---|
| Startup: `OPENAI_API_KEY is not set` | key missing | set it in `.env` / environment of the **backend**, restart |
| `llm_auth_failed` / `embedding_auth_failed` (503) | key rejected/revoked | create a new key, update `.env` |
| `llm_quota_exceeded` / `embedding_quota_exceeded` | no credit / billing not set up (OpenAI returns HTTP 429 + `insufficient_quota`) | fix billing/limits in the OpenAI dashboard |
| `*_rate_limited` | request/token rate limit | retry later; lower `EMBEDDING_BATCH_SIZE`; raise your tier |
| `model_not_available` / `embedding_model_not_available` | model id wrong, or project has no access | `python -m scripts.check_openai`; fix `LLM_MODEL` / project model permissions |
| `llm_timeout` / `embedding_timeout` (504) | slow response | raise `LLM_TIMEOUT_SECONDS` / `EMBEDDING_TIMEOUT_SECONDS` |
| `invalid_llm_response` "cut off" | answer hit the token cap | raise `LLM_MAX_OUTPUT_TOKENS` |
| `embedding_dimension_mismatch`; health `schema_dimension` ≠ `embedding_dimension` | `EMBEDDING_DIMENSIONS` differs from the DB column | align the setting with the column, or migrate + re-index |
| Chat says "No papers are indexed yet" right after migrating | chunks are pending re-index | run the re-index (`--status` shows pending) |
| Everything is `insufficient_evidence` | threshold too high for the new model, or re-index incomplete | `--verify`; recalibrate the threshold |

Every provider error is logged as `class / HTTP status / OpenAI error code / request id` only (support tickets can quote the request id); provider message text is never logged or returned because OpenAI's 401 text can contain part of the key.

## Cost and safety controls
Bounded SDK retries; bounded batches (`EMBEDDING_BATCH_SIZE`, default 64); oversized texts rejected before any call; chunks already embedded with the current model are never re-sent; a DB advisory lock prevents two re-index runs; `/api/health` never calls OpenAI; `POST /api/health/provider-check` is free by default (`GET /models/{id}`), `?deep=true` adds two tiny billable calls, and results are cached 30 s. **The API still has no authentication** (see `SECURITY.md`): do not expose it publicly, or anyone could trigger paid chat/embedding calls.

## What was verified, and what was not

| Claim | Evidence |
|---|---|
| Config, SDK request construction, error mapping, retries, secret handling, reindex logic, health/provider-check | automated tests; the **real `openai` SDK** against a **mocked server** (`httpx.MockTransport`) |
| Migration `0004` (preserve, pending, downgrade byte-exact, 1536 and 1024, empty DB, bad dimension refused), re-index, `--verify`, advisory lock, vector-size guard | integration tests on **real PostgreSQL 16 + pgvector 0.6.0** |
| Whole app: upload PDF → ingest → embed → retrieve → chat with page-level citations; provider quota errors; key never in responses | `tests/integration/e2e_openai_http.py`: real app + real Postgres + real SDK over HTTP to a **local stand-in** for OpenAI |
| **Real OpenAI API**: key accepted, `gpt-4.1-mini` / `text-embedding-3-small` available to your account, `max_completion_tokens` + JSON mode + `dimensions` accepted, real vectors have the expected size | **NOT verified** (the build sandbox could not reach api.openai.com and had no key). Run `python -m scripts.check_openai --deep` and `RUN_OPENAI_LIVE_TESTS=1 pytest tests/integration/test_openai_live.py` |
| Production database / Supabase pgvector version, production migration | **NOT verified** (never applied there) |
| Retrieval quality / threshold / citation support with OpenAI embeddings | **NOT measured** |
