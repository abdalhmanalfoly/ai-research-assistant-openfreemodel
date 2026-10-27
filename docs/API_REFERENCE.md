# API Reference (verified against the route source code)

Base URL (local): `http://localhost:8000` · all endpoints are under `/api` · interactive docs: `/docs` (Swagger) · JSON only (except upload = multipart).
**Authentication: none (❌ not implemented).** Anyone who can reach the server can call every endpoint, including upload. Do not expose it publicly before reading `SECURITY.md`.
Status: all routes 🟡 (written; the route tests exist but were never run).

## Endpoints

| Method | Path | Purpose | DB operations |
|---|---|---|---|
| GET | `/api/health` | Configuration + database status (never calls OpenRouter) | `SELECT 1`; check `pg_extension` for `vector` |
| POST | `/api/health/provider-check` | Explicit OpenRouter connectivity test (`?deep=true` adds 2 tiny real calls); cached 30 s | `GET /models`, `GET /embeddings/models` catalog checks per model |
| POST | `/api/chat` | Ask a question (RAG) | vector search over `chunks` ⨝ `papers` (indexed only); `count(chunks)` when nothing is found |
| GET | `/api/papers` | List papers | `SELECT count`, paged `SELECT papers` |
| GET | `/api/papers/{paper_id}` | One paper's metadata | `SELECT papers` by PK |
| POST | `/api/papers/upload` | Upload a PDF → background ingestion | insert `papers` + `ingestion_jobs`; later insert `chunks` |
| GET | `/api/jobs` | List jobs (newest first; added for the web UI) | `SELECT count`, paged join `ingestion_jobs` ⨝ `papers` |
| GET | `/api/jobs/{job_id}` | Ingestion job status | `SELECT ingestion_jobs` ⨝ `papers` |

### GET /api/health → 200 always
```json
{"status":"ok|degraded","api":"up","max_pdf_mb":50,"llm_provider":"openrouter","llm":"configured|not_configured",
 "embedding":"configured|not_configured","embedding_provider":"openrouter","embedding_dimension":1024,
 "ocr":"rapidocr|tesseract|unavailable|disabled","ocr_language":"eng","ocr_dpi":200,
 "schema_dimension":1024,"chunks_pending_reindex":0,"database":"up|down","pgvector":"installed|missing|unknown",
 "chat_model_name":"nvidia/nemotron-3.5-super-vl:free","embedding_model_name":"nvidia/nemotron-3.5-embed-1b:free"}
```
`status` is `ok` only if the OpenRouter key is configured, the database is up, pgvector is installed and `schema_dimension` equals `embedding_dimension`. **`llm` / `embedding` report configuration only (is `OPENROUTER_API_KEY` set?); `/api/health` never calls OpenRouter, so it cannot prove OpenRouter is reachable.** Use `POST /api/health/provider-check` for that. `ocr` and `chunks_pending_reindex` are informational / re-index progress (chunks without a current-model vector are not searchable). **Changed from the Ollama/BGE version:** `ollama`, `chat_model`, `embedding_model` (cached/loaded), `embedding_device` and `embedding_dtype` were removed; `llm` and `embedding` were added.

### POST /api/chat
Request: `question` (string, trimmed, 1–2000 chars, required); `paper_ids` (list of UUID strings, ≤100, optional); `top_k` (1–20, optional, default `RETRIEVAL_TOP_K`); `year_min`, `year_max` (1000–3000, optional, `year_min ≤ year_max`).
Response 200:
```json
{"answer":"...","citations":[{"id":"E1","paper_id":"<uuid>","title":"<stored title>","authors":["..."],
  "page_number":7,"section":"Results","doi":null,"source_url":null,"relevance_score":0.6123,"excerpt":"first ≤600 chars of the retrieved passage or null"}],
 "retrieved_chunks":3,"model":"nvidia/nemotron-3.5-super-vl:free","insufficient_evidence":false,"warnings":[]}
```
`page_number` is `null` when unknown. `warnings` may contain `invalid_citations_removed`, `no_valid_citations`, `evidence_truncated_to_fit_context`. `insufficient_evidence: true` with `citations: []` is returned (HTTP 200) when the corpus is empty, nothing passes the threshold, or the model says the evidence is insufficient. Filtering by a paper that is not `indexed` simply yields insufficient evidence (the `paper_processing` error exists in code but is never raised).

### GET /api/papers
Query: `limit` (1–100, default 20), `offset` (≥0), `year`, `status` (`pending|processing|indexed|failed|needs_ocr`), `q` (case-insensitive title substring). Response: `{"items":[Paper],"total":n,"limit":20,"offset":0}`; `Paper` = `id,title,authors,abstract,publication_year,doi,source_url,status,error_message,created_at,updated_at`.

### GET /api/papers/{paper_id}
Response `Paper`. 404 `not_found`; 422 if not a UUID.

### POST /api/papers/upload (multipart/form-data) → 202
Fields: `file` (required, `.pdf`, ≤ `MAX_PDF_MB`, must start with `%PDF-`), optional `title` (defaults to file name), `authors` (comma-separated), `year`, `doi`, `source_url`.
Response: `{"paper_id":"<uuid>","job_id":"<uuid>|null","already_registered":false}`. `already_registered: true` (and `job_id: null`) means the identical file is already indexed/pending/processing. A previously `failed`/`needs_ocr` file gets a new job. Errors: 400 `invalid_upload` (not PDF, too big, DOI belongs to another paper).
Note: the size check reads the file into memory (up to the limit + 1 byte) before rejecting.

### GET /api/jobs (new)
Query: `limit` (1–100, default 20), `offset`, `status` (`pending|running|succeeded|failed`), `paper_id` (UUID). Response `{"items":[Job],"total","limit","offset"}`. 🟡 unverified (FastAPI not installable in the build sandbox).

### GET /api/jobs/{job_id}
`{"id","paper_id","paper_title","status":"pending|running|succeeded|failed","error_message","created_at","updated_at"}`; 404 `not_found`.

## Error format
```json
{"error":{"code":"llm_unavailable","message":"The language model service (OpenRouter) is unreachable."}}
```
| HTTP | code | Meaning |
|---|---|---|
| 422 | `validation_error` (+`details:[{field,message}]`) | Bad payload; the submitted body is not echoed |
| 400 | `invalid_upload` | Bad upload |
| 404 | `not_found` | Unknown paper/job |
| 409 | `paper_processing` | Defined, not currently raised |
| 422 | `pdf_extraction_failed`, `ocr_required` | Recorded on the job/paper, not returned by upload (ingestion is async). `ocr_required` = no usable text even after OCR, or more scanned pages than `OCR_MAX_PAGES` |
| 502 | `invalid_llm_response` | Model output not usable JSON, truncated at `LLM_MAX_OUTPUT_TOKENS`, refused, or rejected by the provider |
| 503 | `llm_unavailable`, `llm_auth_failed`, `llm_quota_exceeded`, `llm_rate_limited`, `model_not_available`, `embedding_unavailable`, `embedding_auth_failed`, `embedding_quota_exceeded`, `embedding_rate_limited`, `embedding_model_not_available` (OpenRouter problems; see `OPENROUTER_MIGRATION.md`), `ocr_unavailable`, `retrieval_failed`, `database_error` | Provider/infrastructure problem; messages are fixed strings and never contain provider text or secrets |
| 504 | `llm_timeout`, `embedding_timeout` | OpenRouter call exceeded `LLM_TIMEOUT_SECONDS` / `EMBEDDING_TIMEOUT_SECONDS` (after the SDK's bounded retries) |
| 500 | `embedding_dimension_mismatch`, `invalid_embedding` (NaN/inf/zero vector), `configuration_error`, `internal_error` | Config mismatch / bug; stack traces are logged server-side only |

## curl examples (Windows: use `curl.exe`, not the PowerShell alias `curl`)
```powershell
curl.exe http://localhost:8000/api/health
curl.exe "http://localhost:8000/api/papers?limit=5&status=indexed"
curl.exe http://localhost:8000/api/papers/PASTE-PAPER-UUID
curl.exe -X POST http://localhost:8000/api/papers/upload -F "file=@C:\path\paper.pdf;type=application/pdf" -F "title=My paper" -F "year=2022"
curl.exe http://localhost:8000/api/jobs/PASTE-JOB-UUID
curl.exe -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" -d "{\"question\":\"What limitations do the authors report?\",\"top_k\":5}"
```
PowerShell alternative: `Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/chat -ContentType 'application/json' -Body '{"question":"..."}'`.

## Calling from a frontend or another service
* Put the backend base URL in the caller's config (e.g. `VITE_API_BASE_URL=https://api.example.com`); never put database or Supabase secret keys in the frontend.
* **Same-origin by default:** the backend serves the web UI from `frontend/` at `/` (`SERVE_FRONTEND=true`), so no CORS is needed. **CORS is opt-in:** set `CORS_ORIGINS=http://localhost:5500` (comma-separated explicit origins; `*` is rejected) when the frontend is served from another origin. Only GET/POST and the `Content-Type` header are allowed; there are no credentials/cookies.
* Treat `422` as a user-input problem (show `details`), `503/504` as retryable-later, and `insufficient_evidence: true` as a valid answer, not an error.

## ❌ Not Implemented Yet (proposed design)
| Endpoint | Purpose |
|---|---|
| `DELETE /api/papers/{id}` | Remove paper + chunks (cascade) + stored PDF |
| `GET /api/papers/{id}/file` | Download/view the stored PDF (the web UI has no PDF link without it) |
| `POST /api/jobs/{id}/retry`, `.../cancel` | Job actions (the web UI shows none because they do not exist) |
| `POST /api/papers/{id}/reindex` | Re-embed one paper (today: CLI `scripts.reindex_embeddings`, whole corpus only) |
| `GET /api/papers/{id}/chunks` (admin) | Inspect extracted chunks/pages |
| `POST /api/conversations`, `GET /api/conversations/{id}` | Chat history (needs new tables) |
| Auth on all of the above | See `SECURITY.md` |
