# AI Research Assistant — Backend

FastAPI + PostgreSQL/pgvector + the OpenRouter API (`nvidia/nemotron-3.5-super-vl:free` for answers, `nvidia/nemotron-3.5-embed-1b:free` for embeddings, one `OPENROUTER_API_KEY` server-side). Answers questions about an indexed corpus of research PDFs, with application-validated citations (paper title, DOI/URL, real PDF page number). Scanned PDFs are handled by a local OCR fallback (RapidOCR); page images never leave the machine, but **chunk text and questions are sent to OpenRouter** for embedding and answering. Migrating from the earlier OpenAI version: see [docs/OPENROUTER_MIGRATION.md](docs/OPENROUTER_MIGRATION.md).

## Verification status (read first)

| Area | Status |
|---|---|
| Chunking, evidence selection, prompt building, citation validation, RAG orchestration | **Unit-tested, passing** (synthetic fixtures) |
| OpenRouter config, chat + embedding clients, error mapping, retries, secret handling, re-index logic, health / provider-check | **Automated tests passing**: the real `openai` SDK against a **mocked** OpenRouter server (100 passed, 27 skipped without a test database; see Tests) |
| Migration `0005` (OpenRouter embeddings), pgvector search, SQL re-index/verify, advisory lock | **Migration written and unit-tested, NOT applied to any database** (no local/remote DB was available in this environment). Run it and verify on your database before trusting it |
| Whole app (upload PDF → ingest → embed → retrieve → cited chat) | **Updated for OpenRouter, NOT executed here** (needs a test database): `tests/integration/e2e_openrouter_http.py` |
| **Real OpenRouter API** (key, model access, parameters, real vector sizes, answer quality) | **NOT verified**: no API key was available. Run `python -m scripts.check_openrouter --deep` after adding your key |
| Whether OpenRouter honours the `dimensions` parameter for `nvidia/nemotron-3.5-embed-1b:free` | **UNVERIFIED** (model catalog lists no supported parameters). Every returned vector is checked against `EMBEDDING_DIMENSIONS` at runtime and a mismatch fails loudly |
| Retrieval quality and `RETRIEVAL_THRESHOLD` with these embeddings | **Not measured**; the threshold is an uncalibrated guess |
| OCR fallback (`rapidocr-onnxruntime`) | Live-tested earlier on a synthetic scanned page; real scans not evaluated |
| Web UI | 28 Node tests with mocked fetch pass (fixture values updated); DOM tests in `frontend/tests/browser.html` not run |

Supabase is supported as a plain PostgreSQL host via `DATABASE_URL`; no Supabase client library, Auth, or Storage is used yet. Migrations: `0001` schema, `0002` RLS, `0003` vector(1024) for BGE, `0004` OpenAI embeddings (`vector(EMBEDDING_DIMENSIONS)`), `0005` OpenRouter embeddings (re-opens the column at `vector(EMBEDDING_DIMENSIONS)`, keeps old vectors in `embedding_prev*` columns).

Treat anything not marked as passing/live-tested as a first draft that still needs its first real run.

## Documentation
| Doc | Contents |
|---|---|
| [frontend/README.md](frontend/README.md) | Web UI (HTML/CSS/vanilla JS): run, structure, tests, limits |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Audit of what exists, architecture, data flow |
| [docs/SUPABASE_INTEGRATION.md](docs/SUPABASE_INTEGRATION.md) | Supabase database setup, connection strings, schema, storage plan |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Verified endpoints, errors, curl examples |
| [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) | Windows step-by-step setup and troubleshooting |
| [docs/OPENROUTER_MIGRATION.md](docs/OPENROUTER_MIGRATION.md) | OpenAI → OpenRouter: configuration, production runbook, rollback, diagnostics, what is verified |
| [docs/EMBEDDINGS.md](docs/EMBEDDINGS.md) | Embeddings: policy, dimensions, re-indexing, verification |
| [docs/PDF_INGESTION_AND_RAG.md](docs/PDF_INGESTION_AND_RAG.md) | Corpus import, chunking, retrieval, citations |
| [docs/SECURITY.md](docs/SECURITY.md) | Secrets, RLS, auth design, production checklist |

## Web UI
Start the backend (`uvicorn app.main:app --reload`) and open **http://localhost:8000/**. Pages: Research Chat, Library, Upload, Jobs, System Status, Settings. No Node/npm is needed to run it. See `frontend/README.md`. Tested so far: 28 Node unit tests with mocked fetch (passing); DOM tests in `frontend/tests/browser.html` and all real-backend behaviour are **not yet run**.

## Setup

```bash
# 1. Database
docker compose up -d db          # pgvector/pgvector:pg16

# 2. Python 3.11+
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt  # no model downloads, no torch
cp .env.example .env             # then set OPENROUTER_API_KEY (backend only; .env is git-ignored)

# 3. Schema (EMBEDDING_DIMENSIONS defaults to 1024 in .env.example; the migration reads it)
alembic upgrade head

# 4. Check the key and models (real requests; --deep adds two tiny billable calls)
python -m scripts.check_openrouter

# 5. Run
uvicorn app.main:app --reload    # docs: http://localhost:8000/docs
curl localhost:8000/api/health   # configuration + database status (never calls OpenRouter)
curl -X POST localhost:8000/api/health/provider-check   # explicit, rate-limited OpenRouter connectivity test
```
On Windows PowerShell use `.venv\Scripts\Activate.ps1` and `Set DATABASE_URL=...`-style environment variables (see `docs/LOCAL_SETUP.md`). Only `:free` model IDs are shipped in `.env.example`; using them keeps OpenRouter usage free-tier, but its own rate limits still apply.

## Importing the corpus

```bash
python -m scripts.import_corpus --dir data/pdfs                 # titles default to file names
python -m scripts.import_corpus --manifest corpus.json          # real metadata (see script docstring)
```
Re-running is safe: PDFs are keyed by SHA-256, so already-indexed files are skipped. Scanned pages are OCR'd automatically (RapidOCR, local; see `OCR_*` settings); a paper is marked `needs_ocr` only when OCR still finds no readable text. Upload one-off PDFs with `POST /api/papers/upload` and poll `GET /api/jobs/{id}`.

## Chat

```bash
curl -X POST localhost:8000/api/chat -H 'content-type: application/json' \
  -d '{"question":"What limitations do the authors report?","paper_ids":[],"top_k":5}'
```
Response shape: `answer, citations[{id, paper_id, title, authors, page_number|null, section, doi, source_url, relevance_score}], retrieved_chunks, model, insufficient_evidence, warnings`.
`warnings` may contain `invalid_citations_removed`, `no_valid_citations`, `evidence_truncated_to_fit_context`.

## How citations work
Retrieved chunks get ids `E1..En` assigned by the app; the model must cite them as `[E1]`. Unknown ids are stripped; valid ids are resolved from the database. Unknown pages return `null`. **A valid id only proves that chunk was supplied to the model, not that it supports the sentence**; claim-to-evidence alignment needs separate evaluation.

## Embedding model changes
Embeddings are `nvidia/nemotron-3.5-embed-1b:free` via OpenRouter (1024-dim by default via `EMBEDDING_DIMENSIONS`; the model's native size is 2048); see `docs/EMBEDDINGS.md`. **Upgrading from the OpenAI version: follow the runbook in `docs/OPENROUTER_MIGRATION.md`** (back up, `alembic upgrade head` keeps the old vectors in `embedding_prev`/`embedding_prev_or`, then `python -m scripts.reindex_embeddings`, then `--verify`). Retrieval returns nothing until the re-index finishes. Switching to another embedding model or size later needs a new migration (the vector size is fixed in the schema), `EMBEDDING_DIMENSIONS`, and a re-index.

## Calibrating `RETRIEVAL_THRESHOLD`
The default 0.30 is an **uncalibrated guess** for these embeddings (cosine scores differ between embedding models; 0.30 was previously tuned for none). Collect ~30 questions with known relevant papers plus ~10 off-topic ones, run `python -m scripts.calibrate_threshold questions.json`, pick a threshold separating them, and revisit whenever the embedding model or dimensions change.

## Tests
```bash
pytest                                            # unit tests + (if configured) integration; no network, no API key needed
node --test "frontend/tests/*.test.js"            # frontend (Node 20+, testing only)

# integration tests need a THROWAWAY PostgreSQL with pgvector (database name must contain "test"; it is wiped):
TEST_DATABASE_URL=postgresql+psycopg://research:research@localhost:5432/research_test pytest
python -m tests.integration.e2e_openrouter_http   # full-stack run against a local OpenRouter stand-in (needs TEST_DATABASE_URL)

# real OpenRouter smoke test (opt-in; free-tier models, but rate-limited):
RUN_OPENROUTER_LIVE_TESTS=1 OPENROUTER_API_KEY=... pytest tests/integration/test_openrouter_live.py -v
```
Mocked OpenRouter tests use the real SDK against `httpx.MockTransport` (`tests/fixtures/openrouter_fakes.py`); they prove our code drives the SDK correctly, **not** that openrouter.ai accepts the requests. `tests/fixtures/fakes.py` is test-only synthetic data. OCR live test is opt-in (`RUN_OCR_TESTS=1`).

## Known limitations
- OCR is CPU-bound (~5-30 s/page depending on DPI and page density); low-quality or handwritten scans may still fail.
- Questions and paper text are sent to OpenRouter (a third party); check this is acceptable for your documents.
- Free models can be rate-limited (429) or intermittently unavailable (502) on OpenRouter's free tier; requests are retried with backoff, but expect occasional 502 responses.
- During a model/dimension change, retrieval is empty until the re-index finishes (chat then says no papers are indexed).
- No conversation history; **no authentication** and no rate limiting (anyone who can reach the API can trigger model calls; keep it private).
- Ingestion runs in-process via FastAPI BackgroundTasks (lost on restart; stuck `processing` rows are not auto-recovered).
- Section detection is heuristic (common heading names only).
- Evidence budget uses ~3 chars/token, an approximation.
- The model may ignore the JSON/citation format; the service handles this (parse failure -> 502, missing citations -> warning) but quality with `nvidia/nemotron-3.5-super-vl:free` has not been evaluated on real papers.
- No retrieval-quality or claim-alignment evaluation harness yet.

## Integrating into the team repo
1. Copy `app/`, `migrations/`, `scripts/`, `alembic.ini`, `requirements.txt`, `.env.example`.
2. Merge dependencies; set `DATABASE_URL` to a Postgres with the `vector` extension.
3. Mount via `from app.main import create_app` or include the routers under `/api`.
4. `alembic upgrade head`, import the corpus, hit `/api/health` until `status: ok`.
