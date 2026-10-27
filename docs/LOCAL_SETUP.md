# Local Setup on Windows (PowerShell)

The OpenRouter steps below were exercised with a mocked OpenRouter server (unit tests on Windows), **not** against openrouter.ai or Supabase, and migration 0005 has not been applied to any real database. Treat the first run as the real test.
Prerequisites: Windows 10/11, an OpenRouter API key (create one at https://openrouter.ai/keys; the shipped `...:free` models are usable on the free tier), internet access, ~1 GB free disk (no local model files).

## 1. Install tools
* Python 3.11 or newer from python.org (tick **Add python.exe to PATH**). Check: `python --version`
* Optional: Docker Desktop (only for a local Postgres test database); Git.

## 2. Extract and open
Extract `ai-research-assistant.zip`, then:
```powershell
cd C:\path\to\ai-research-assistant
```

## 3. Virtual environment and dependencies
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   (then re-open PowerShell)
python -m pip install --upgrade pip
pip install -r requirements.txt
```
`requirements.txt` uses version ranges that were never installed together by the author. If `pip` reports a conflict, note the message; after a successful install run `pip freeze > requirements.lock.txt` to pin it.

## 4. Configure `.env`
```powershell
Copy-Item .env.example .env
notepad .env
```
Set `DATABASE_URL` (a `postgresql+psycopg://user:password@host:port/db` string; see `SUPABASE_INTEGRATION.md §3`; start with the **session pooler** string from Supabase → Connect if you are unsure about IPv6) and `OPENROUTER_API_KEY` (create it at https://openrouter.ai/keys; backend only). Leave the other defaults (default embedding size is 1024); if you want a different size, set `EMBEDDING_DIMENSIONS` **now**, before step 7. Never share `.env` or commit it (it is git-ignored).

## 5. Supabase dashboard (once)
Enable the `vector` extension; reset/store the DB password; copy the connection string (details in `SUPABASE_INTEGRATION.md §2`).

## 6. Check the OpenRouter key and models
```powershell
python -m scripts.check_openrouter          # free: asks OpenRouter whether your key can see the two :free models
python -m scripts.check_openrouter --deep   # also 1 tiny embedding + 1 tiny chat request (free-tier; rate limits apply)
```
Exit code 0 and `"ok": true` means the key, both models and (with `--deep`) your quota work. Nothing is downloaded and no local model server is needed.

## 7. Create the schema
```powershell
alembic upgrade head
alembic current         # expect 0005 (head)
```
Common failure: wrong URL prefix (must start with `postgresql+psycopg://`), unencoded password characters, or IPv6-only direct host on an IPv4 network (use the session pooler).

## 8. Start the API and check health
```powershell
uvicorn app.main:app --reload
# second PowerShell window:
curl.exe http://localhost:8000/api/health
```
Target: `"status":"ok"`. Open `http://localhost:8000/` for the web UI (Research Chat, Library, Upload, Jobs, System Status) or `http://localhost:8000/docs` for the raw API.

## 9. Import PDFs and verify
Put your own PDFs in `data\pdfs\` (the repo has none), then:
```powershell
python -m scripts.import_corpus --dir data\pdfs
# if you already had chunks from an earlier model/size version: follow docs/OPENROUTER_MIGRATION.md, then python -m scripts.reindex_embeddings
curl.exe "http://localhost:8000/api/papers?status=indexed"
```
`failed` or `needs_ocr` papers show an `error_message`.

## 10. Ask a question and check citations
```powershell
curl.exe -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" -d "{\"question\":\"What is the main contribution of the papers?\"}"
```
Verify: every `citations[].title` and `page_number` matches a real paper in your corpus (open the PDF at that page); ask an off-topic question and confirm `insufficient_evidence: true`; `warnings` should be empty.

## 11. Tests
```powershell
pytest                                             # 100 passed, 27 skipped without a database; no network or API key needed
# with a throwaway Postgres (database name must contain "test"; it is wiped): set TEST_DATABASE_URL, then pytest
```
| Kind | Where | Needs |
|---|---|---|
| Unit (pure logic, fakes) | `tests/unit/test_core.py` | nothing |
| OCR fallback (synthetic PDFs + fake engine) | `tests/unit/test_ocr.py` | PyMuPDF (in `requirements.txt`) |
| Mocked OpenRouter (real SDK, fake server), config, API | `tests/unit/test_openrouter_*.py`, `test_config.py`, `test_api*.py`, `test_reindex.py` | `pip install -r requirements.txt` |
| Integration with real PostgreSQL (migrations 0001–0005, re-index, E2E) | `tests/integration/test_embedding_migrations.py`, `test_pgvector.py`, `test_e2e_openrouter_http.py` | Postgres + pgvector, `TEST_DATABASE_URL` |
| Live OpenRouter smoke test (opt-in) | `tests/integration/test_openrouter_live.py` | `RUN_OPENROUTER_LIVE_TESTS=1`, `OPENROUTER_API_KEY` |
| Live OCR (opt-in) | `tests/integration/test_ocr_live.py` | `rapidocr-onnxruntime`; set `RUN_OCR_TESTS=1` |

## 12. Troubleshooting
| Symptom | Likely cause | Check / fix |
|---|---|---|
| `status: degraded`, `llm: not_configured` | `OPENROUTER_API_KEY` missing in the backend's environment | set it in `.env`, restart; verify with `python -m scripts.check_openrouter` |
| `model_not_available` / `embedding_model_not_available` | wrong model id, or OpenRouter cannot serve it | `python -m scripts.check_openrouter`; fix `LLM_MODEL` / `EMBEDDING_MODEL` (use the exact `...:free` ids) |
| `llm_auth_failed` / `embedding_auth_failed` | key rejected | create the key at openrouter.ai/keys, paste it into `.env`, restart |
| `*_quota_exceeded` / `*_rate_limited` | free-tier credit/rate limit hit | wait, or top up credits; see the table in `OPENROUTER_MIGRATION.md` |
| `database: down` | bad `DATABASE_URL`, firewall, IPv6-only host | test with `psql "<url>"`; try session pooler; check `sslmode=require` |
| `pgvector: missing` / `type "vector" does not exist` | extension not enabled | enable in Database → Extensions; re-run `alembic upgrade head` |
| `prepared statement ... does not exist` | transaction pooler (6543) with prepared statements | set `DB_DISABLE_PREPARED_STATEMENTS=true` or use session pooler |
| `password authentication failed` | wrong/old password, special chars not encoded | reset DB password; percent-encode |
| Pydantic error at startup | invalid setting (e.g. `EMBEDDING_DIMENSIONS` above the model's maximum, or `LLM_PROVIDER=ollama`) | read the message; compare to `.env.example` (the key is never printed) |
| `embedding_dimension_mismatch` | `EMBEDDING_DIMENSIONS` ≠ model output or DB column | check `/api/health` `schema_dimension`; align the setting or migrate + re-index (`OPENROUTER_MIGRATION.md`) |
| Paper `needs_ocr` | scanned PDF and OCR found no readable text, or OCR disabled/unavailable | check `/api/health` → `ocr`; `pip install -r requirements.txt`; try `OCR_DPI=300`; OCR externally (e.g. OCRmyPDF) as a last resort |
| OCR is very slow | CPU inference at high DPI / many scanned pages | lower `OCR_DPI` (150), split the document, or raise `OCR_MAX_PAGES`; expect roughly 5–30 s/page |
| `insufficient_evidence` for everything | threshold too high, nothing indexed, or filters too narrow | `select status,count(*) from papers group by 1;` lower `RETRIEVAL_THRESHOLD` after calibration |
| `llm_timeout` | slow model response | raise `LLM_TIMEOUT_SECONDS`; lower `top_k` |
| Duplicate import does nothing | same SHA-256 already registered | expected (idempotent) |
| Browser says CORS error | CORS not configured | see `SECURITY.md` |
| `ModuleNotFoundError: scripts` | run from the wrong folder | `cd` to the project root, run with `python -m` |
