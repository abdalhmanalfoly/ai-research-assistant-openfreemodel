# 2048-D Nemotron Migration — Execution Plan (NOT EXECUTED; requires explicit authorization)

Target: `nvidia/nemotron-3.5-embed-1b:free` at its **native 2048-d** output everywhere (config, schema, reindex,
retrieval, tests). Approved application/code/test changes are already implemented; every step below that touches
the database or `.env` waits for authorization.

## Audit facts (verified live 2026-10-28)

- `alembic_version = 0007` (teammate head; 0006/0007 FILES still not in this checkout — they are a prerequisite).
- `public.chunks` = 10 columns; `chunks.embedding` = **vector(1024)**; `chunks_pkey`, unique `(paper_id, chunk_index)`
  b-tree, `ix_chunks_paper_id`, `ix_chunks_embedding_model`, and **`ix_chunks_embedding_hnsw` (hnsw, 1024)**.
- Corpus: 211 chunks — single generation `nvidia/nemotron-3.5-embed-1b:free @ 1024` (0 mixed rows).
- No `embedding_prev`/`embedding_prev_or` columns exist (0005 ran on the "metadata" branch; preservation slot free).
- `public.papers_arxiv_legacy.embedding` is also vector(1024) — the archive table is NOT part of the RAG pipeline
  and is NOT touched by this migration; its legacy vectors are never reused.
- pgvector HNSW limit = 2000 dims → at 2048 the column must be index-less (full-scan cosine; fine at this scale).

## Affected objects (exactly these; nothing else)

| Object | Change | Reverse |
|---|---|---|
| `chunks.embedding` | recreate as `vector(2048)`; old 1024 vectors preserved via the rename/copy branches of the migration | documented `downgrade` (restores preserved vectors byte-for-byte) |
| `ix_chunks_embedding_hnsw` | dropped (cannot exist above 2000 dims) | recreation at the restored dimension on downgrade |
| chunks per-row metadata (`embedding_model`, `embedding_dim`) | set NULL where != current model → reindex targets | reindex regenerates deterministically from stored text |
| new state table `alembic_0008_embedding_migrate` | records branch for exact downgrade | dropped by downgrade |
| users/papers/chunk text/RLS/owner data | **untouched** | — |

## Pre-conditions (in order)

1. ~~Teammate's `0006`/`0007` migration files~~ **RESOLVED (2026-10-28):** explicit RECONCILIATION revisions
   `0006_ownership_schema.py` (users table, papers.owner_id + owner-scoped uniques, index) and
   `0007_aux_tables.py` (conversations/messages/signup_invites/signup_allowlist, RLS-parity) were authored from
   live-schema catalog inspection (single head = 0008 verified with `alembic heads`; chain-integrity unit tests).
   They are idempotent and fail-fast; the live DB (stamped 0007) never re-executes them — only 0008 applies.
   If the teammate's ORIGINAL 0006/0007 files ever surface, our reconciliation scripts must be reconciled
   against them (compare outcomes, not contents) BEFORE any other checkout upgrades.
2. Backend `.env`: set `EMBEDDING_DIMENSIONS=2048` (the ONLY way to pass it to the migration and the app;
   the migration reads it, and mismatch between column size and setting is rejected at every layer).
3. Backup before anything: `pg_dump`-equivalent logical backup or Supabase dashboard backup
   (tables: `papers`, `chunks`, `ingestion_jobs` at minimum; vectors are regenerable from chunk text, but keep them anyway).
4. Drain: no running ingestion/reindex jobs (advisory lock 726001 checked free).

## Execution (after authorization — staged, never automatic)

1. `alembic upgrade head` (applies 0008 on top of the teammate chain; prints the 2000-d/HNSW warning).
   Expected: branch `rename` on current live state → `embedding_prev` = old nvidia@1024 vectors +
   `embedding_prev_model = nvidia/nemotron-3.5-embed-1b:free`; `embedding` = fresh `vector(2048)` (all NULL).
2. `python -m scripts.reindex_embeddings --status` → expect `pending=211` (metadata was nulled).
3. Bounded pilot: `python -m scripts.reindex_embeddings --batch-size 32 --max-batches 1` ≈ 1 API request
   (~32 texts × ~200 chars ≈ 6-7 k tokens); `--verify` must be clean for those rows; retrieval spot-check via `/api/chat`.
4. Full reindex: `python -m scripts.reindex_embeddings` (resume-safe, per-batch commit, advisory-locked).
   ~7 batches of 32 ≈ 7 API requests ≈ 60-70 k tokens total; `$0` pricing, rate limits unknown.
5. Post-run validation: `python -m scripts.reindex_embeddings --verify` (must be PASS: 211/211 current model}
   and `vector_dims = 2048`; wrong_dimension=0, inconsistent=0) + a retrieval smoke question +
   `select format_type(...) = 'vector(2048)'` for `chunks.embedding`.
6. Only after verified retrieval: optional space reclaim
   `ALTER TABLE chunks DROP COLUMN embedding_prev, DROP COLUMN embedding_prev_model;` (deliberate manual step).

## Recovery / rollback

- `alembic downgrade 0007` → reverses the branch recorded in `alembic_0008_embedding_migrate`: the preserved
  1024-vectors return byte-for-byte under `embedding` with their true model names (`rename` restores
  `embedding_prev`; `copy` restores `embedding_prev_or`). Nothing user-owned is involved (chunks carry no owner).
- Worst case (backup lost AND vectors lost): every chunk's source text still lives in `chunks.text`; a full
  re-run of the reindex script reconstructs all vectors. Papers, users, permissions and metadata are never dropped
  by any step.
- Rolling back only `.env` (`EMBEDDING_DIMENSIONS=1024`) after the column is 2048 is NOT supported — sizes must
  match; do not edit `.env` mid-flight.

## Retrieval threshold

`RETRIEVAL_THRESHOLD=0.30` is uncalibrated for Nemotron at ANY dimension (documented in config) — 2048 does not
magically fix cosine-score calibration. Evaluated separately after the pilot retrieval smoke test; recalibrate
via `python -m scripts.calibrate_threshold questions.json`. Do not reuse BGE-era calibrations.

## History/consistency note

This checkout's migration chain now ends at `0008` (its `down_revision` = `0007`). Until 0006/0007 arrive,
`alembic` commands fail with "Can't locate revision '0007'" — unchanged status quo, not caused by 0008.
