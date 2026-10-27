# Architecture

**Status labels used in all docs:** ✅ Implemented & verified · 🟡 Implemented, not verified · ❌ Missing · 💡 Recommended

## 1. Audit summary (checked against the source in this repository)

| # | Question | Finding |
|---|---|---|
| 1 | What is implemented? | FastAPI app, 6 endpoints, chunking, evidence selection, prompt building, citation validation, RAG orchestration, OpenRouter chat client and OpenRouter embedder, SQLAlchemy models, pgvector store, Alembic migrations (0001 schema, 0002 RLS, 0003-0005 embedding columns), PDF ingestion with a local OCR fallback (RapidOCR/Tesseract), corpus import script. |
| 2 | Implemented but untested? | GPU embedding (not applicable), the corpus import script, OCR quality on real scans, very large scanned documents (`OCR_MAX_PAGES` guard), and an automated live-OpenRouter verification (opt-in test; mocked unit tests pass). In this environment there was no database, so no path has been run live here. |
| 3 | What is missing? | Authentication, CORS, rate limiting, Supabase Storage, chat history, `collections`/`conversations`/`messages` tables, paper delete/update endpoints, automatic recovery of jobs stuck in `processing`. |
| 4 | Which database does it expect? | PostgreSQL ≥ 13 with the `vector` extension (`DATABASE_URL`). Nothing Supabase-specific. |
| 5 | PostgreSQL + pgvector integrated? | ✅ Yes (`app/db/models.py`, `app/db/repositories/pgvector_store.py`, migration `0001_initial.py` with an HNSW cosine index). Verified against Supabase Postgres: migrations, ingestion, and cosine retrieval all ran. |
| 6 | SQLAlchemy / SQL / Supabase client? | **SQLAlchemy 2.0 + psycopg 3.** No raw SQL except in migrations and `health.py`. `grep -ri supabase` over the code finds nothing: no Supabase client library is used. |
| 7 | Where are PDFs stored? | **Local disk**: `STORAGE_DIR` (default `./data/storage`), file name `<sha256>.pdf`, referenced by `papers.storage_key` (`app/services/ingestion_service.py`). Not Supabase Storage. |
| 8 | How are chunks/embeddings persisted and retrieved? | `chunks.embedding` is `vector(EMBEDDING_DIMENSIONS)` (1024 by default; nullable; migration 0005; earlier generations of vectors kept in `embedding_prev`/`embedding_prev_or`/`embedding_legacy`), `embedding_model`/`embedding_dim` record the producer. NULL = pending re-index. Retrieval uses `Chunk.embedding.cosine_distance(query_vec)`, filtered to `papers.status='indexed'` and `chunks.embedding_model = EMBEDDING_MODEL`; score = `1 - distance`. |
| 9 | Existing endpoints? | See `API_REFERENCE.md`. All under `/api`, **none authenticated**. |
| 10 | Switch to Supabase without a rewrite? | **Yes, for the database**: it's a connection-string change plus two small changes made during this task (migration `0002_enable_rls.py`, `DB_DISABLE_PREPARED_STATEMENTS`/pool settings). **Storage and Auth would need new code.** |

## 2. Recommended architecture

```
Frontend / other app ──HTTPS──▶ FastAPI backend (this repo) ──SQLAlchemy/psycopg──▶ Supabase Postgres (+pgvector)
                                   │  └──HTTPS──▶ OpenRouter API: nvidia/nemotron-3.5-super-vl:free (answers) + nvidia/nemotron-3.5-embed-1b:free (embeddings), one OPENROUTER_API_KEY
                                   └──(no local model servers or weights)
                                   └──in-process──▶ RapidOCR / Tesseract (local; scanned pages only)
                                   └──(optional, 💡 future)──▶ Supabase Storage (private bucket) / Supabase Auth (JWT verify)
```

* **Primary approach: SQLAlchemy → Supabase PostgreSQL** ("Approach A"). Reasons: the code is already SQLAlchemy; pgvector similarity search, transactions (replace-all-chunks on re-ingest), and Alembic migrations all need a real Postgres connection; the Supabase client talks to PostgREST (HTTP) and is a poor fit for those.
* The Supabase Python client is *not* added. Add it later only if you implement Supabase Storage (💡), and even then plain HTTPS calls would work.
* **`@supabase/server` / `npm install @supabase/server` is NOT needed.** It is a JavaScript/TypeScript server library; this backend is Python. Do not add Node.js to this project. If you later build a JS frontend or edge function, that is where it would belong.
* The backend needs outbound HTTPS to `openrouter.ai` (`OPENROUTER_BASE_URL`, default `https://openrouter.ai/api/v1`); no model server is hosted anywhere.

## 3. Lifecycles

### Initial ingestion (runs once per paper)
1. 🟡 `scripts/import_corpus.py` (or `POST /api/papers/upload`) calls `IngestionService.register()`: checks `%PDF-` magic bytes, computes SHA-256, copies the PDF to `STORAGE_DIR/<hash>.pdf`, inserts `papers` (`status=pending`) and `ingestion_jobs` (`pending`). Same hash already indexed/pending/processing → returns the existing paper, **no new job** (idempotent).
2. 🟡 `run_job()` sets job `running`, paper `processing`; PyMuPDF extracts text per physical page (1-based). Pages with < `OCR_MIN_PAGE_CHARS` characters are rendered at `OCR_DPI` and passed to the local OCR engine.
3. If OCR is disabled/unavailable or finds nothing and average text is < 25 chars/page → paper `needs_ocr`, job `failed`. `OCR_MAX_PAGES` bounds very large scanned documents.
4. `chunk_pages()` normalises text and splits into ≤ `CHUNK_MAX_CHARS` chunks that never cross a page, tagging a heuristic `section`.
5. Embeddings from `nvidia/nemotron-3.5-embed-1b:free` via OpenRouter (no prefix on passages), batches of `EMBEDDING_BATCH_SIZE`; every vector is validated (exactly `EMBEDDING_DIMENSIONS` floats, finite, non-zero) or the job fails.
6. Old chunks of that paper are deleted and new ones inserted in one transaction; paper → `indexed`, job → `succeeded`. Errors → `failed` + `error_message`.

### Every question (`POST /api/chat`)
1. Validate request (✅ validation logic is covered by pure-Python tests; the HTTP layer is 🟡).
2. Embed the question with the same OpenRouter model and dimensions (no instruction), validate dimension.
3. pgvector cosine search (only chunks with `embedding_model = EMBEDDING_MODEL` and a non-NULL embedding): top `top_k × 3` candidates (filters: `paper_ids`, `year_min`, `year_max`).
4. Drop chunks below `RETRIEVAL_THRESHOLD`, remove near-duplicates, fit into the evidence budget → ids `E1…En`.
5. No evidence → `insufficient_evidence: true` **without calling the LLM**.
6. Send evidence + question to `nvidia/nemotron-3.5-super-vl:free` (Chat Completions, JSON mode). SDK retries are bounded (`OPENROUTER_MAX_RETRIES`).
7. Parse JSON; strip any `[E#]` not supplied; resolve valid ids to paper title/authors/DOI/URL/page from the database rows; return.

✅ = logic verified by automated tests (mocked OpenRouter server). Integration tests need a real PostgreSQL with pgvector and were **skipped in this environment** (no database available); nothing was run against the real OpenRouter API here.

## 4. PDF binary vs. searchable text
* **PDF binary** = the original file, kept for download/re-processing/re-indexing. Belongs in file storage (local disk now; Supabase Storage 💡).
* **Text chunks + vectors** = derived data used for search. Belongs in Postgres/pgvector (`chunks`).
* Losing the PDF does not break search, but you cannot re-index (e.g. after changing embedding model) without it.

## 5. Embedding subsystem (added later)
See `EMBEDDINGS.md` and `OPENROUTER_MIGRATION.md`. Embeddings are produced by `OpenRouterEmbedder` (`app/services/providers/openrouter_embeddings.py`) through the OpenRouter API; chat uses `OpenRouterChatModel` (`openrouter_chat.py`). Migration `0005_openrouter_embeddings.py` creates the new vector column non-destructively; `scripts/reindex_embeddings.py` fills it.

## 6. Web frontend
`frontend/` is plain HTML/CSS/JavaScript (ES modules, no build step, no npm). FastAPI serves it at `/` (mounted after the API routers so `/api/*` and `/docs` always win). It only calls the HTTP API; see `frontend/README.md`.

## 7. Source map
`app/main.py` wiring · `app/api/routes/*` endpoints · `app/services/{chunking,evidence_service,citation_service,chat_service,retrieval_service,ingestion_service,prompts}.py` · `app/services/providers/{openrouter_common,openrouter_chat,openrouter_embeddings,llm_base}.py` · `app/db/{models,session}.py`, `app/db/repositories/pgvector_store.py` · `migrations/versions/` · `scripts/import_corpus.py` · `tests/`.
