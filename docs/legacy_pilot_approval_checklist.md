# Pilot Import Approval Checklist (separate gates — NOTHING runs until each box is signed off)

Each numbered gate below must be **independently approved** in writing by the user. No gate chains into the next
one automatically; the runbook re-asks for confirmation before every mutating step.

## Gate 0 — Preconditions confirmed by the user (review-mode sign-off)
- [ ] Migration-history blocker resolved: teammate confirms `0006`/`0007` files are in the repo (or declares
      them unnecessary for this import) and live `alembic_version` is still `0007`.
- [ ] `owner_id=NULL` shared-visibility for archive records is the approved product decision (library and chat
      currently expose all `papers` rows unfiltered to every caller; nothing here changes that).
- [ ] Current-corpus snapshot recorded (3 papers / 211 chunks / model breakdown) and backup export created.

## Gate 1 — Pilot database import (13 staged rows per `docs/legacy_arxiv_pilot_manifest.md`)
- [ ] Approved: execution of `docs/sql/legacy_arxiv_import_draft.sql` Phase 1–2 restricted to the 13 manifest
      records (~papers 13 + chunks 13 inserts, `status='pending'`).
- [ ] Reads-only before write: `docs/sql/legacy_arxiv_preflight.sql` re-run green.
- [ ] Acceptance: inserted counts == 13/13 papers+chunks; snapshot from `docs/sql/legacy_arxiv_validation.sql`;
      existing user corpus byte-unchanged; rerun of same import inserts 0 (idempotency verified).

## Gate 2 — Pilot embedding generation (bounded)
- [ ] Approved: ONE bounded run —
      `python -m scripts.reindex_embeddings --batch-size 16 --max-batches 1` (≈ 1 API request, ~14.4 k tokens).
- [ ] Acceptance: `--verify` clean for the 13 (append at scale later); `--status` shows `pending=13 → 0`;
      every pilot chunk `embedding_model='nvidia/nemotron-3.5-embed-1b:free'`, dim 1024.
- [ ] Stop conditions honored: rate-limit stalls → pause; dimension mismatch → abort (config drift).

## Gate 3 — Pilot retrieval validation (read-only; no writes)
- [ ] Approved: up to 5 test questions via `/api/chat` (one archive-only, one user-paper, one both-source,
      one off-corpus, one near-duplicate-abstracts probe). Answers + citations verified against expectations.
- [ ] Acceptance: archive citation shows `page_number=null, doi=null, source_url=arXiv link, section=Abstract`;
      no fabricated pages; off-corpus → honest `insufficient_evidence`.

## Gate 4 — Full archive import (papers status flip included)
- [ ] Approved: re-run import draft for the remaining ≈ 2 763 rows (batched, resume-safe).
- [ ] Phase 2b status flip `pending → indexed` gated on `verify()==PASS` per runbook Phase 6.
- [ ] Acceptance: `docs/sql/legacy_arxiv_validation.sql` all-green; user corpus unchanged.

## Gate 5 — Full embedding backfill
- [ ] Approved: full `python -m scripts.reindex_embeddings` (default batch 64 ⇒ ~44 requests ≈ 782 k input
      tokens, $0 pricing; free-tier rate limits unknown → bounded batches + backoff; wall-time estimate minutes).
- [ ] Acceptance: `--verify` PASS over the entire combined index; provenance 100 % current-model;
      retention of pilot results (no re-embedding, no overwrite).

## Explicit refusals (never allowed without a NEW written approval)
- Any statement unclear → abort and re-review.
- Any schema/migration attempt, threshold/model change, or legacy-vector reuse.
- Auto-proceeding from Gate N to N+1 without the user's confirmation.
