# Migration: OpenAI → OpenRouter

One server-side `OPENROUTER_API_KEY` now powers **both** answer generation (`nvidia/nemotron-3.5-super-vl:free`, Chat
Completions) and embeddings (`nvidia/nemotron-3.5-embed-1b:free`). Both shipped models are `:free` (published pricing 0;
OpenRouter's free tier still applies its own rate limits and availability windows). No paid model is ever used as a
fallback — a wrong `:free` id fails loudly at the provider check or the call itself. Public API contracts (`/api/chat`,
papers, jobs) are unchanged; `/api/health` keys are unchanged, the values changed (`llm_provider: "openrouter"`,
new model ids, `embedding_dimension` from `EMBEDDING_DIMENSIONS`).

> **What is actually verified vs. not** (this migration was prepared without an API key or a database, so be honest
> with yourself on the first run):
> * ✅ Unit-tested offline: config validation, both provider clients against a **mocked** OpenRouter server, error
>   mapping, retries, health/provider-check logic, re-index logic (100 passed, 27 skipped — the skipped ones need
>   PostgreSQL).
> * ✅ Verified live **keylessly** against openrouter.ai: both model ids exist in OpenRouter's public catalogs; the
>   chat model supports `response_format`/`temperature` and `max_tokens` (**not** `max_completion_tokens`).
> * ❌ Not verified: a real key (401/402 paths are unit-tested only), migration 0005 against a real database, and —
>   most importantly — **whether OpenRouter honours the `dimensions` parameter for the embedding model** (its catalog
>   entry lists no supported parameters). Every returned vector is checked against `EMBEDDING_DIMENSIONS` at runtime
>   and a mismatch fails loudly, so the worst case is a clear error, not silent corruption.

## 1. Configuration

Create a key at <https://openrouter.ai/keys> (no billing/credits required for `:free` models) and put it in the
backend `.env` **only** — never in the frontend, Git, screenshots or logs. The relevant settings
(`app/core/config.py`; full annotated template: `.env.example`):

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — (required) | `SecretStr`; whitespace-free; never printed or logged |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | guard: a non-`openrouter.ai` host needs `OPENROUTER_ALLOW_CUSTOM_BASE_URL=true` (the key would be sent there) |
| `OPENROUTER_MAX_RETRIES` | `2` | SDK retries (connection errors, 408/409/429, 5xx) with backoff |
| `LLM_PROVIDER` / `EMBEDDING_PROVIDER` | `openrouter` | anything else is rejected at startup |
| `LLM_MODEL` | `nvidia/nemotron-3.5-super-vl:free` | use the exact id |
| `EMBEDDING_MODEL` | `nvidia/nemotron-3.5-embed-1b:free` | use the exact id |
| `EMBEDDING_DIMENSIONS` | `1024` | must match the `vector(N)` column; **set before `alembic upgrade head`** |

The implementation uses the official `openai` Python SDK as a compatibility client pointed at the OpenRouter base
URL. One key is sent with every request (Authorization header); it is never sent anywhere except the configured
(official-guarded) host.

### Why `EMBEDDING_DIMENSIONS=1024` and not 2048?

NVIDIA documents the embedding model's **default output at 2048 dimensions** with `dimensions` accepted in
128–2048. pgvector **HNSW indexes cap at 2000 dimensions**, so at 2048 the column is created *without* an index
(full-scan cosine search — acceptable for small/medium corpora, slower for large ones). The shipped configuration
requests 1024 instead: inside the documented range and index-friendly. `EMBEDDING_DIMENSIONS` may be up to 4096;
anything above 2000 simply runs unindexed (`embedding_supports_hnsw=False`). `EMBEDDING_DIMENSIONS` above the
model's documented max (2048) is rejected at startup.

### Compatibility notes

* Chat requests always send `response_format={"type":"json_object"}` and `max_tokens` (not `max_completion_tokens`,
  which the OpenRouter catalog does not list for this model) plus `temperature`.
* Embedding requests send `dimensions` on every call. The response's vectors are expired/checked in order by
  `index`, validated (exact length, finite, non-zero) and only then stored.
* `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_ALLOW_CUSTOM_BASE_URL` / `OPENAI_MAX_RETRIES` and all `OLLAMA_*` /
  `EMBEDDING_*` legacy inference settings are obsolete: they are ignored, and the app logs a warning naming them
  (never their values).

## 2. First-time run (empty or new database)

```powershell
cd C:\path\to\ai-research-assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env ; notepad .env      # set DATABASE_URL and OPENROUTER_API_KEY

python -m scripts.check_openrouter              # free: both model ids exist in OpenRouter's catalogs
python -m scripts.check_openrouter --deep       # 1 tiny embedding + 1 tiny chat call (free-tier)
alembic upgrade head                            # creates vector(EMBEDDING_DIMENSIONS)
alembic current                                 # expect: 0005 (head)
uvicorn app.main:app --reload
curl.exe http://localhost:8000/api/health       # expect "status":"ok" once DB is up and key is set
```

If `EMBEDDING_DIMENSIONS` differs from 1024, set it **before** the migration runs (the column size is fixed in the
schema from that value).

## 3. Upgrading an existing database (migration 0005)

`migrations/versions/0005_openrouter_embeddings.py` is non-destructive and picks a branch automatically
(recorded in the small table `alembic_0005_embedding_migrate` so `downgrade()` can undo exactly what happened):

| State at upgrade | What 0005 does |
|---|---|
| `chunks` table empty | `embedding` re-created as `vector(N)` |
| rows exist, column already `vector(N)` | nothing rebuilt; `embedding_model`/`embedding_dim` set to NULL where they don't already name the current model → every such chunk counts as pending re-index; `reindex_embeddings` overwrites vectors in place |
| rows exist, column is `vector(M)`, M ≠ N, no `embedding_prev` yet | current column **renamed** to `embedding_prev` (byte-for-byte; index dropped — it is never searched), per-row model name kept in `embedding_prev_model`; new nullable `vector(N)` column added |
| rows exist, `embedding_prev` already taken (0004-era data) | current vectors first **copied** into `embedding_prev_or` (with `embedding_prev_or_model`), then handled as above |

Chunk ids, texts, page numbers, sections, paper relations and citations are untouched in every branch. Old vectors
keep their true model names, so they can never be searched under the wrong model (pgvector_store compares
`(embedding_model, embedding_dim)` against configuration). The upgrade/downgrade runs in a single transaction; it
either fully applies or not at all. Offline mode (`--sql`) is not supported (the migration inspects the table first).

```powershell
# 0. Back up first (pg_dump or your host's snapshot).
python -m scripts.reindex_embeddings --status    # size of the pending work, no API call
alembic upgrade head
python -m scripts.reindex_embeddings             # resumable; commits per batch; DB lock against double runs
python -m scripts.reindex_embeddings --verify    # SQL check of stored vector sizes; exit 0 only on PASS
```

Between the migration and the end of the re-index, retrieval finds nothing (chat answers "no papers indexed").
Failed chunks stay pending and are listed; rate-limit errors are waited out then the run stops; auth/quota errors
stop immediately; a wrong vector size stops immediately (configuration error). Completed batches are always kept.

After verifying retrieval you may reclaim space manually (deliberately not automated):

```sql
ALTER TABLE chunks DROP COLUMN embedding_prev, DROP COLUMN embedding_prev_model;         -- "rename" branch
ALTER TABLE chunks DROP COLUMN embedding_prev_or, DROP COLUMN embedding_prev_or_model;   -- "copy" branch
```

### Rollback

`alembic downgrade 0004` replays the reverse operation recorded in `alembic_0005_embedding_migrate` and restores
the previous vectors byte-for-byte with their model metadata, then drops the 0005 artifacts. New chunks embedded
after the upgrade are lost (they are derivable: re-run the re-index). Existing-column and empty-table backups come
free; a fresh backup is still strongly recommended.

## 4. Verify (runbook)

```powershell
python -m scripts.check_openrouter --deep        # key + both models + 2 tiny real calls
curl.exe http://localhost:8000/api/health        # embedding_dimension / schema_dimension must agree; pending = 0
curl.exe -X POST http://localhost:8000/api/health/provider-check
# ask a real question and check citations against the PDF pages
```

```sql
select embedding_model, embedding_dim, count(*) from chunks group by 1,2;
-- one row after re-index: nvidia/nemotron-3.5-embed-1b:free, EMBEDDING_DIMENSIONS
select count(*) from chunks where embedding is null
  or embedding_model is distinct from 'nvidia/nemotron-3.5-embed-1b:free'
  or embedding_dim <> 1024;   -- substitute your EMBEDDING_DIMENSIONS; 0 when complete
```

## 5. Errors and troubleshooting

| HTTP / code | Cause | Fix |
|---|---|---|
| 503 `llm_auth_failed`, `embedding_auth_failed` | key missing/revoked/typo | create/replace at openrouter.ai/keys; `.env`; restart; `python -m scripts.check_openrouter` |
| 503 `*_quota_exceeded` | free-tier credit/privacy-limit exhausted | wait for the reset, top up credits, or lower usage; `--deep` re-check |
| 503 `*_rate_limited` / 429 hits | free-tier request/minute limits | SDK retries absorb transient hits; lower `EMBEDDING_BATCH_SIZE`, spread re-index runs |
| 503 `model_not_available` / `*_model_not_available` | wrong model id or provider outage | `python -m scripts.check_openrouter`; use the exact `...:free` ids |
| 502 `invalid_llm_response` | model output not usable JSON / truncated / refused / rejected by provider | retry; if frequent, raise `LLM_MAX_OUTPUT_TOKENS` |
| 504 `llm_timeout` / `embedding_timeout` | slow response | raise `LLM_TIMEOUT_SECONDS` / `EMBEDDING_TIMEOUT_SECONDS`; lower `top_k` |
| 500 `embedding_dimension_mismatch` | served vector size ≠ `EMBEDDING_DIMENSIONS` (e.g. OpenRouter ignored `dimensions`) | **do not** truncate manually; align `EMBEDDING_DIMENSIONS` with reality and migrate + re-index, or keep 2048 and accept an unindexed column |
| startup `configuration_error` | invalid setting (bad host, `EMBEDDING_DIMENSIONS` outside the model's range, wrong provider) | read the validator message; never prints the key |
| stale OpenAI settings warning at startup | `OPENAI_API_KEY` etc. still set | remove them from `.env`/environment; they are ignored |

Free-tier reality check: `:free` models are rate-limited and can be intermittently unavailable (429/502). Requests
retry with bounded backoff; a re-index that hits persistent limits pauses and is resumable — re-run it.

## 6. What changed in the code (summary)

* Provider layer renamed and rewritten: `app/services/providers/openrouter_common.py`, `openrouter_chat.py`,
  `openrouter_embeddings.py` (`OpenRouterChatModel`, `OpenRouterEmbedder`); 402 now maps to a quota error.
* `app/core/config.py`: OpenRouter settings, validators, `KNOWN_EMBEDDING_MODELS` (per-model dimension facts),
  obsolete-setting detection, secret-free diagnostics.
* Migration `0005_openrouter_embeddings.py` (branching, non-destructive, exact downgrade); `0004` unchanged.
* `scripts/check_openrouter.py` (catalog checks + optional real calls), `scripts/reindex_embeddings.py` (unchanged
  behaviour, refreshed messages).
* Tests renamed/updated: `tests/fixtures/openrouter_fakes.py`, `tests/unit/test_openrouter_*.py`,
  `tests/integration/test_embedding_migrations.py`, `test_e2e_openrouter_http.py`, `test_openrouter_live.py`
  (`RUN_OPENROUTER_LIVE_TESTS=1`).
* Frontend health hint text and fixtures updated (same response keys).
* Historical doc: `docs/OPENAI_MIGRATION.md` (superseded; kept for context).

## 7. Security

* The key is server-side only (`SecretStr`, excluded from logs/health/errors); outbound host is guarded
  (`OPENROUTER_ALLOW_CUSTOM_BASE_URL`). See `SECURITY.md`.
* If a previous OpenAI key was ever exposed (e.g. shared in a chat or committed), treat it as compromised and
  revoke it in its own dashboard; removing it from `.env` does not retire it.
