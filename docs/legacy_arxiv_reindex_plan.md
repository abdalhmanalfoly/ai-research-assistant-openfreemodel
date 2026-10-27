# Archive Reindex/Embedding Plan (review-only — Phase 5)

Covers: the existing script, why it is safe or not for the archive backfill, the proposed targeted extension,
and the measured workload. **Nothing here has been executed.**

---

## 1. `scripts/reindex_embeddings.py` + `app/services/reindex_service.py` + `app/db/repositories/chunk_repo.py` — audited behavior

| Property | Actual behavior (verified in code) |
|---|---|
| Selection criteria | `SqlChunkRepo._pending()` = `embedding IS NULL OR embedding_model IS DISTINCT FROM :cfg OR embedding_dim IS DISTINCT FROM :cfg` (`chunk_repo.py:60-62`). Missing **or** foreign-model/chunks only — **never re-embeds chunks already on the current model+dim** |
| Overwrites valid vectors? | **No.** `save()` writes only rows the pending predicate selected; a current, valid 211-vector corpus is invisible to it |
| Provenance | Written with every vector: `embedding_model = self.model` (from `Settings`), `embedding_dim = len(v)` (`chunk_repo.py:129`); store-side dimension guard refuses wrong-size vectors (`chunk_repo.py:126-128`) |
| Paper-ID / batch scoping | **No CLI scoping** — it processes *all* pending chunks globally. Limitation for a target-only backfill (2.7 below) |
| Dry-run | `--status` prints pending counts + model breakdown + API-usage estimate and exits **without any API call or DB write**; `--verify` is read-only SQL. But the *default* invocation writes — there is no `--apply` gate; noted as a proposed patch below |
| Resume safety | Keyset pagination by `chunk.id` (`fetch_pending(..., after_id)`); failed chunks stay pending; Ctrl+C-safe; per-batch commit (`chunk_repo.py:132-134`) |
| Partial batch failure | `ReindexService._embed_rows`: batch failure → isolates single rows and retries them individually; unembeddable rows remain pending and are logged `chunk %s could not be embedded (%s); left pending`; **never marks a chunk indexed after a failed embedding** |
| Concurrency protection | Session-level advisory lock `pg_try_advisory_lock(726001)` (`chunk_repo.py:37-53`) — two overlapping runs cannot happen |
| Backoff | bounded 5/15/45 s on provider rate limit; non-rate `EmbeddingUnavailable` subclasses stop the run (fail-fast, resumable) |
| Completion signal | `--verify` returns PASS only when `total == current AND pending == 0 AND wrong_dimension == 0 AND inconsistent == 0` (`IndexVerification.ok`) — *vector length verified in SQL, not just metadata* |
| Affecting the user-paper corpus | None: the 211 current chunks are `current` on model+dim → never selected |

## 2. Fit to the archive backfill (Option B)

After the paper/chunk staging import, our 2,776 abstract chunks are `embedding IS NULL` → they are **exactly**
what the existing script selects. It will: pick them up in id-order, embed in batches of `EMBEDDING_BATCH_SIZE`
(=64 default), store vectors + provenance in one commit per batch, fail rows independently, resume on rerun, and
`verify()` will confirm. The 3 pre-existing papers remain untouched (not pending).

## 3. Gaps found and proposed (unexecuted) changes

3.1 **No subset scoping** → in the approved phase, add flags
`--only-paper-ids <uuid,...>` (injected as `Chunk.paper_id.in_(...)` into `fetch_pending`) and
`--exclude-paper-ids`. Minimal patch, no behavior change otherwise.

3.2 **No explicit write gate** → proposed: default invocation behaves like `--status`+`--verify` and requires
`--apply` (or env `REINDEX_CONFIRM=yes`) to write. Aligns with the "dry-run by default" policy.

3.3 **Fails-closed on unexpected vectors** — already correct (`save()` guards). No change.

3.4 **status flip** — the script intentionally does not touch `Paper.status`. For the import, flipping archive
papers `pending → indexed` must be a separate, explicitly approved SQL step gated on `--verify` PASS
(`docs/sql/legacy_arxiv_import_draft.sql` phase 2b), preserving the invariant "indexed ⟹ embedded with the
current model".

## 4. Workload estimate (measured, 2026-10-27)

- Eligible abstracts: **2 776 / 2 776** (0 excluded: no blanks, no oversize, no duplicates).
- Embed texts = chunk text only (summaries; titles are *not* part of chunk text in this project's conventions).
- Input volume: **3 127 793 chars ≈ 782 000 tokens** (~4 chars/token heuristic; non-English titles in some
  abstracts may vary ±15 %).
- Requests by batch size (one API request per batch): **batch 64 → 44 requests**, batch 32 → 87, batch 16 → 174,
  batch 8 → 348. Per-request payload ≤ 64 × ~285 tokens ≈ 18 000 tokens (within typical request limits; model
  context is 16 k+ … provider caps not peak-relevant for embeddings).
- Pricing: catalog shows `completion: 0, prompt: 0` for `nvidia/nemotron-3.5-embed-1b:free` (verified from the
  public OpenRouter catalog) → **$0 embedding cost**; chat model not involved in the backfill.
- Quotas/limits: **free-tier request-rate caps are not published** (unknown — the only unverified cost figure).
  Observed throughput in the earlier 211-chunk reindex run: well under one batch/second with the provider's
  occasional 429s absorbed by backoff → expected wall time for 44 batches: **≈ 2–10 minutes** at batch 64
  (estimate, not a verified figure); batch 8 → roughly 5× more requests.
- DB impact: +2 776 papers rows (~1 MB), +2 776 chunks rows (~3.1 MB text), + 2 776 × 1024 f32 vectors
  ≈ 11.4 MB; HNSW index maintenance proportional; no schema change.

## 5. Pilot plan (when approved; NOT executed here)

1. `python -m scripts.reindex_embeddings --status` → report pending ≈ 2 776 (+ whatever other pending exist; if
   the 211 user chunks are pending that indicates external drift → **stop**).
2. Import small staging set = **10–25 papers/chunks** via the draft SQL restricted to `limit N` on the candidate
   SELECT (portable idempotency).
3. `--max-batches 1 --batch-size 8` (bounded trial) → `--verify` → assert `updated == n_pilot`.
4. Spot-check one pilot chunk's vector dimension (`=1024`) and model provenance (`= configured model`).
5. Run retrieval for one pilot query handset — assert archive evidence is retrievable and cites correct source.
6. Only then: full import → full reindex → verify → status flip (runbook).
