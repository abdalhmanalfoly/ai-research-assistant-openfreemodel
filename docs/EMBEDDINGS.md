# Embeddings (OpenRouter `nvidia/nemotron-3.5-embed-1b:free`)

Embeddings come from the OpenRouter Embeddings API through `OpenRouterEmbedder` (`app/services/providers/openrouter_embeddings.py`), using the
same `OPENROUTER_API_KEY` as the chat model. Nothing is downloaded or loaded locally. History and the migration runbook: `OPENROUTER_MIGRATION.md`.

## 1. Policy (identical for documents and queries)
* Model `EMBEDDING_MODEL` (default `nvidia/nemotron-3.5-embed-1b:free`) and `dimensions=EMBEDDING_DIMENSIONS` (default 1024) are sent on **every** request, for stored chunks and for user questions. Nothing else may embed text.
* **No prefixes or instructions.** The BGE query instruction (`Represent this sentence for searching relevant passages: `) was BGE-specific and is gone; text is sent exactly as stored/asked.
* The `dimensions` parameter is sent with every request. ⚠ **Whether OpenRouter honours it for this model is UNVERIFIED** (the model's OpenRouter catalog entry lists no supported parameters). NVIDIA documents a native 2048-dim output with an accepted `dimensions` range of 128–2048; trust only the runtime check below. If the server returns a different size than configured, the call fails before anything is stored.
* pgvector compares with **cosine distance**, which ignores vector length, so no local normalisation is applied. Vectors must be finite, non-zero and exactly `EMBEDDING_DIMENSIONS` long, or the call fails (`embedding_dimension_mismatch` / `invalid_embedding`) before anything is stored.
* Input order is preserved: responses are re-ordered by their `index` and checked for completeness.

## 2. Limits and batching
`EMBEDDING_BATCH_SIZE` (default 64) texts per request; inputs over `EMBEDDING_MAX_INPUT_CHARS` (24000) or empty/non-string are rejected before any network call. Chunks are ≤ `CHUNK_MAX_CHARS` (1200), far below the model's per-input token limit. SDK retries (`OPENROUTER_MAX_RETRIES`, default 2) cover connection errors, 408/409/429 and 5xx with exponential backoff; they are bounded.

## 3. Dimensions
`vector(N)` in the database must equal `EMBEDDING_DIMENSIONS` (health shows `schema_dimension` vs `embedding_dimension`; a mismatch makes `status: degraded`). Migrations `0004`/`0005` create the column from that setting (default 1024). Up to 2000, pgvector keeps an HNSW index; **above 2000 the column is created with no HNSW index and search falls back to a full scan** (the code still works; the health check reports it). Shortening saves storage at some quality cost; it does **not** make old vectors reusable.

## 4. Model identity in the database
Each chunk records `embedding_model` and `embedding_dim`. Search only compares vectors whose model **and** dimension equal the configured ones, so vectors from different models can never be mixed. A chunk is *pending* when it has no vector or either field differs; pending chunks are invisible to search until re-indexed.

## 5. Re-indexing
```bash
python -m scripts.reindex_embeddings --status     # counts, pending text size (~tokens); no API call
python -m scripts.reindex_embeddings              # resumable; commits per batch; DB lock against double runs
python -m scripts.reindex_embeddings --verify     # SQL check of stored vector sizes; exit 0 only on PASS
```
Failed chunks stay pending and are listed. Rate limits are waited out (5/15/45 s) then the run stops; quota/auth/outage errors stop immediately; wrong vector sizes stop immediately (a config error). Completed batches are always kept. Ingestion of new PDFs embeds with the same service.

## 6. Checking it works
```bash
python -m scripts.check_openrouter --deep         # real, tiny, (free-tier) requests: key + models + quota
```
```sql
select embedding_model, embedding_dim, count(*) from chunks group by 1,2;          -- one row after re-index
select vector_dims(embedding), count(*) from chunks where embedding is not null group by 1;   -- = EMBEDDING_DIMENSIONS
```

## 7. Threshold
`RETRIEVAL_THRESHOLD` (default 0.30) is **not calibrated** for these embeddings; cosine ranges differ between models. Use `python -m scripts.calibrate_threshold questions.json` (one small embedding call per question) and revisit whenever the model or dimensions change.
