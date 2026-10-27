# PDF Ingestion, Chunking, Embeddings, Retrieval, Citations

## 1. Loading a pre-existing corpus (no manual upload)
The repository ships **no papers** (`data/pdfs/` is empty and git-ignored for `*.pdf`). Supply your own legally obtained PDFs. Actual, existing command (🟡 never executed):

```powershell
# every *.pdf in a folder; titles default to the file name
python -m scripts.import_corpus --dir data\pdfs
# with real metadata
python -m scripts.import_corpus --manifest corpus.json
# re-embed EXISTING chunks (e.g. after a model/dimension switch; see OPENROUTER_MIGRATION.md); resumable, no PDF re-extraction
python -m scripts.reindex_embeddings        # same as: python -m scripts.import_corpus --reindex
python -m scripts.reindex_embeddings --status   # counts only
```
Manifest (JSON list; only `path` and `title` required; `path` is relative to the manifest file):
```json
[{"path":"pdfs/paper1.pdf","title":"Exact title","authors":["A. Author"],"year":2021,"doi":"10.xxxx/xxxx","source_url":"https://...","abstract":"..."}]
```
Requires `OPENROUTER_API_KEY` (chunk text is sent to OpenRouter to be embedded; free-tier models are rate-limited). No local model is downloaded. The script prints one line per file (`queued` or `already registered (skipped)`), then processes jobs **sequentially**.

## 2. Stages and where to look
| Stage | Code | Notes |
|---|---|---|
| Register | `IngestionService.register` | magic-byte check, SHA-256, copy to `STORAGE_DIR`, insert rows |
| Extract | `extract_pages` (PyMuPDF `page.get_text("text")`) | `page_number` = physical page index starting at 1; printed page labels (e.g. roman numerals) are **not** used |
| Scanned check + OCR | `extract_pages` + `run_job` | pages with < `OCR_MIN_PAGE_CHARS` text are rendered at `OCR_DPI` and OCR'd locally; avg < 25 chars/page **after** OCR ⇒ `needs_ocr` |
| Normalise | `chunking.normalize_text` | rejoins `word-\nbreaks`, collapses whitespace, keeps Unicode/math; no NFKC folding |
| Chunk | `chunking.chunk_pages` | ≤ `CHUNK_MAX_CHARS` (1200), paragraph-first, sentence fallback; never crosses a page; heading names (Abstract, Methods, Results, …) set `section` |
| Embed | `OpenRouterEmbedder` (`nvidia/nemotron-3.5-embed-1b:free`) | Batches of `EMBEDDING_BATCH_SIZE`; no prefix on passages; every vector validated (exactly `EMBEDDING_DIMENSIONS` floats, finite, non-zero) |
| Store | `IngestionService.run_job` | one transaction: delete old chunks, insert new, mark indexed |

Known extraction limits: two-column layouts may interleave text; tables, equations and figure captions extract poorly; footnotes/headers repeat on each page; section detection only recognises common heading names. OCR does not fix layout problems (it reads the rendered page in visual order), so check OCR'd papers too. Check a few real papers by reading `chunks.text` (SQL below).

### OCR settings (scanned PDFs)
`OCR_ENABLED=true` by default. `OCR_PROVIDER=auto` picks `rapidocr-onnxruntime` (models ship with the package, CPU, no external binary) and falls back to Tesseract if the executable is installed. Pages with a real text layer are never OCR'd. `OCR_DPI` (default 200) trades speed for accuracy; `OCR_MIN_PAGE_CHARS` (25) defines "scanned"; `OCR_MAX_PAGES` (0 = unlimited) bounds very large documents. OCR is CPU-bound: expect roughly 5–30 s/page at 200 DPI. `OCR_LANGUAGE` applies to Tesseract only (`eng`, `ara`, `eng+ara`, …); RapidOCR's default model handles English and Chinese. OCR output is line-based; `normalize_text` rejoins hyphenated line breaks.

## 3. Retrieval and answer generation
`RetrievalService` embeds the question with the same OpenRouter model and dimensions (no instruction) and runs `ORDER BY embedding <=> query LIMIT top_k*3`, restricted to chunks whose `embedding_model` and `embedding_dim` match the configuration. `select_evidence` then applies the threshold, per-paper de-duplication and the evidence budget.

**Threshold calibration is required, and the default is NOT validated for these embeddings.** `RETRIEVAL_THRESHOLD=0.30` is a guess (0.45 was for BGE, 0.30 for the OpenAI embeddings); cosine-score ranges differ per model. Run `python -m scripts.calibrate_threshold questions.json` on your own labelled questions and re-run it whenever the embedding model or dimensions change.

## 4. Citation guarantees (and limits)
* The app, not the model, owns ids `E1..En`. Unknown ids are removed from the text and `invalid_citations_removed` is added to `warnings`.
* Title/authors/DOI/URL/page come from database rows, never from model text. Unknown page ⇒ `null`.
* ✅ These behaviours are unit-tested with fakes; they have **not** been run against the real chat model in this environment (no database and no API key were available), and citation *support* is still not verified (see the limit below).
* **Limit:** a valid id means "this chunk was in the prompt", **not** "this chunk supports the sentence". Any model, including `nvidia/nemotron-3.5-super-vl:free`, can mis-attribute. If an answer has no valid citation the API still returns it with `no_valid_citations`; show that to users or hide such answers.

## 5. Useful SQL (SQL Editor or psql)
```sql
select status, count(*) from papers group by status;
select p.title, count(c.*) chunks, min(c.page_number) first_page, max(c.page_number) last_page
  from papers p left join chunks c on c.paper_id=p.id group by p.id;
select page_number, section, left(text,200) from chunks where paper_id='PASTE-UUID' order by chunk_index limit 10;
select embedding_model, embedding_dim, count(*) from chunks group by 1,2;   -- after re-index: one row (nvidia/nemotron-3.5-embed-1b:free, 1024); NULLs = still pending
select count(*) from chunks where embedding is null or embedding_model is distinct from 'nvidia/nemotron-3.5-embed-1b:free';   -- pending re-index
select vector_dims(embedding), count(*) from chunks where embedding is not null group by 1;   -- must equal EMBEDDING_DIMENSIONS (1024 by default)
select id, status, error_message from ingestion_jobs where status='failed';
```
Stuck jobs: if the process dies mid-job, rows stay `running`/`processing` forever (❌ no recovery). Fix manually: `update papers set status='failed' where status='processing';` then re-run the import (failed papers get a new job).
