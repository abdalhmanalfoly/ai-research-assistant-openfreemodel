# Combined Retrieval Execution Runbook (future approved phase — DO NOT RUN ANY STEP NOW)

Applies to: import of `public.papers_arxiv_legacy` abstracts into `papers`/`chunks` (Option B; see
`docs/combined_retrieval_architecture.md`), then embedding backfill via the existing reindex machinery.
Every numbered step that touches the database is **explicitly gated on user approval** and produces a
verifiable artifact before the next step may start. `NOT FOR EXECUTION` SQL lives in `docs/sql/`.

---

## Phase 0 — Preflight (read-only; artifact = `preflight report`)

Steps:
1. Run `docs/sql/legacy_arxiv_preflight.sql` (read-only): extension availability (`uuid-ossp` / `gen_random_uuid`),
   `alembic_version` (must still be 0007 or later-known; **stop on regression** to 0005/0006), counts
   (papers=3, chunks=211, legacy=2776 but re-measured live), RLS flags, grants of the intended operator role,
   legacy duplicates/blank/overlap checks re-run (expect 0/0/0), `no_other_pending` check (`chunks` pending == 0
   via `SqlChunkRepo._pending` logic; if > 0 resolve separately first).
2. Verify the deployed app is on the current model/dim config (API `/api/health` and `--verify PASS`).
3. Confirm the operator role used for import is the DB owner (`postgres.uvtektqsgthsjrezqlln` pooler role) and
   that no `papers`/`chunks` RLS *policies* exist that could hide inserted rows (re-checked live).

Stop conditions:
- `alembic_version` changed (teammate migrations in flux) → pause, re-review.
- Legacy duplicates/overlap checks return non-zero → re-audit; do not import.
- Non-zero *foreign* pending chunks → resolve or scope the backfill narrowly.

## Phase 1 — Backup / restore point (NOT a write to user data)

1. Export logical backups of `papers`, `chunks`, `ingestion_jobs` (row-level snapshot; small — 3/211/3 rows).
2. Record snapshot marker: `select max(updated_at)/max(created_at)` per table, row counts, model breakdown.

## Phase 2 — Pilot staging (10–25 rows)

1. Reviewer approves pilot candidate list from `docs/sql/legacy_arxiv_stage_preview.sql` output.
2. Run the pilot batch of `docs/sql/legacy_arxiv_import_draft.sql` (limit-N form), labeled NOT FOR EXECUTION until
   approval.
3. Validate: inserted `papers` count == N, one chunk each, `status='pending'`, `owner_id NULL`, `doi NULL`,
   `content_hash NULL`, `storage_key NULL`, `source_url = legacy URL`; pre-existing rows unchanged.

## Phase 3 — Pilot embedding (approved execution)

1. `python -m scripts.reindex_embeddings --status` (read-only) — expect `pending_total == N (+0 foreign)`.
2. `python -m scripts.reindex_embeddings --max-batches 1 --batch-size 8` (bounded write — **approved only**).
3. Validate vectors: `--verify`, plus per-chunk `vector_dims=1024`, `embedding_model=<cfg>` for pilot chunk ids.
4. Status flip pilot batch via `legacy_arxiv_import_draft.sql` phase-2b block gated on `--verify` PASS.

## Phase 4 — Full staging import

1. Approval of the final non-pilot candidate set (expected ≈ 2 751–2 766 depending on pilot coverage; exact from
   preview counts).
2. Execute docs/sql import phase-2 full batch in the approved transaction grouping; commit per batch of ≤ 500
   papers to bound lock time; artifact = per-batch inserted/pregnant counts (`RETURNING` aggregates logged),
   **never** placeholders.
3. Run `docs/sql/legacy_arxiv_validation.sql` (read-only) after each batch and after completion:

Stop conditions (any → halt + rollback decision):
- inserted papers ≠ expected within a batch,
- any pre-existing `papers`/`chunks` row changed (snapshot diff),
- duplicate identity detected (should be impossible by `uuid5` + unique keys — treat as extraordinary),
- batch wall time > 30 s or DB connection instability,
- any conflict error.

## Phase 5 — Full embedding backfill (approved execution)

1. `python -m scripts.reindex_embeddings` (full run; resumable; rate-limit-aware; advisory-locked). Monitor:
   batch callback `updated/failed` counts; expect ≈ 44 batches at the default 64.
2. Acceptance: `--verify` PASS over the whole combined index (expected `total=2990`, after both phases ≈ 2 776 +
   211 + any pilot adjustments; recompute live), `wrong_dimension == 0`, `inconsistent == 0`, and per-chunk
   provenance 100 % on the import set.
3. Stop conditions: > 5 % of chunks fail after retries; provider quota exhaustion / hard 429s persisting after
   backoff; mismatched vector dimension (config drift — **stop immediately**).

## Phase 6 — Status flip + acceptance retrieval

1. Flip archive papers `pending → indexed` **only if** `--verify` PASS persists (phase-2b block, approved).
2. Handset retrieval checks (live API, no writes):
   - archive-only question → returns evidence citing an abstract chunk (`section='Abstract'`, `page_number` null,
     `doi` null, `source_url` = arXiv link),
   - user-paper question → unchanged behavior (regression gate),
   - off-corpus question → `insufficient_evidence` (honest refusal) — "15-passages" family of incidents already
     instrumented by chat diagnostics.
   - both-source question → citations from both sources in one answer.
3. Frontend sanity: citations render; archive paper count does not break library pagination (server-paginated).

## Phase 7 — Reconciliation

1. `docs/sql/legacy_arxiv_validation.sql` full run — all counts/flags OK.
2. Logs: no secrets/URLs/credentials; chat diagnostics hashed-question privacy retained.
3. Update `docs/legacy_arxiv_data_audit.md` §6 with actual numbers observed.

## Phase 8 — Monitoring / post-run

- Watch chat-service diagnostics for a week: insufficiency rates, archive-evidence share (top scores appear on
  log lines), report regressions.
- Watch reindex/ingestion lock contention (advisory lock 726001).
- Optional follow-ups (separate approval): library UI grouping of archive papers; the teammate user-RLS policy
  must explicitly include public archive rows or they become invisible under future RLS.

## Phase 9 — Rollback criteria and route

Invoke `docs/sql/legacy_arxiv_rollback_draft.sql` in whole-or-partial mode when (approved only):
- acceptance retrieval fails / citations wrong,
- -any- user-corpus regression detected,
- too many failed embeddings (>5 %),
- product decides to unship the archive.

Rollback is idempotent, deletes **only** `uuid5`-derived IDs (`'urn:x-arxiv-legacy:' || legacy.id`), guarded by
`storage_key IS NULL AND owner_id IS NULL` — pre-existing user papers are physically unremovable by that key
shape. Never delete by title/date/status alone. Restores from Phase 1 backups are the fallback for any
out-of-set damage.
