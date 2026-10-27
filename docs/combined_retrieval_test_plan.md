# Combined Retrieval Test Plan (specs; only baseline commands were executed)

Legend: **[RUN]** = actually executed during this review phase; **[SPEC]** = proposed test, not yet run (its
subject code/import phase does not exist or is not yet approved). Local unit tests use `tests/fixtures/fakes.py`
(`FakeEmbedder`, `FakeLLM`, `InMemoryStore`) and mocked OpenRouter fakes (`tests/fixtures/openrouter_fakes.py`);
the integration suite (`tests/integration/`) is `TEST_DATABASE_URL`-gated and *never* runs against live Supabase
(its guard requires the DB name to contain `test`).

## 1. Baseline executed this phase [RUN]

```
.venv/Scripts/python.exe -m pytest tests -q
→ 115 passed, 27 skipped in 6.05s
```
(+) includes `tests/unit/test_core.py` (selection/citation/insufficiency semantics), `tests/unit/test_openrouter_chat.py`
(provider contract incl. `finish_reason="length"` → `InvalidLLMResponse`), `tests/unit/test_diagnostics.py`
(cosine semantics, threshold boundary, log hygiene), `tests/unit/test_db_session.py`, `tests/unit/test_api.py`,
`tests/unit/test_reindex.py` (pending selection/resume/isolation), `tests/unit/test_openrouter_embeddings.py`.
No new production code paths exist yet → no new tests could run. Commands/skips below are the review-phase plan.

## 2. Retrieval after Option B import [SPEC]

| # | Test | Fixture/level | Expected |
|---|---|---|---|
| R1 | Question answerable by a user-paper chunk still returns that chunk (regression) | unit: `InMemoryStore` + user paper seed + archive seed | user chunk selected, citation `page_number` intact |
| R2 | Question answerable by an archive abstract retrieves that abstract | unit: archive seed (abstract-text, page=None, section='Abstract') | abstract selected; citation `page_number is None`, `source_url` set, `doi is None` |
| R3 | Both corpora contribute in one answer: evidence set contains ≥1 of each source type | unit: two seeds with distinct, both-relevant texts | ≤5 items total, together within budget |
| R4 | Unrelated query → `insufficient_evidence=True`, 0 LLM call | unit | reason `no_evidence_after_filtering` logged |
| R5 | Archive source still absent (all vectors pending) → results degrade to user corpus only | unit: archive chunk `embedding=NULL` | user evidence returned; no fabricated archive content |
| R6 | Source *outage* (store raises `RetrievalError`) honesty | unit: `store.fail` | `AppError` reaches the client; no silent partial results |
| R7 | Scores comparable across sources | unit: synthetic store returning one of each source at same score | same threshold semantics applied; no source re-weighting |
| R8 | Duplicate/near-duplicate abstracts | unit: two nearly identical abstracts | one dropped by 0.85 Jaccard dedupe |
| R9 | Fabricated citations / unknown ids again stripped | existing `test_fabricated_citation_ids_are_removed` remains green [will RUN post-import] |

## 3. Security / access [SPEC]

| # | Test | Attestation target |
|---|---|---|
| S1 | `paper_ids` filter still restricts to caller-chosen papers | regression of current behavior unchanged |
| S2 | Archive rows: `owner_id IS NULL` cannot collide with user-owned `uq_papers_owner_doi`/… | no user-paper ownership semantics are altered; SQL validation assertions |
| S3 | Error paths never leak credentials/URLs | extend existing `test_secret_never_reaches_exception_message_or_logs` pattern to import scripts' error sites |
| S4 | Logs contain no raw question text / full abstracts | assertions on captured logs (pattern of `test_diagnostics.py`) |

## 4. Embedding integrity [SPEC → run only in approved execution, on a test DB]

| # | Check | How |
|---|---|---|
| E1 | Every imported indexed chunk: `embedding IS NOT NULL AND vector_dims=1024 AND embedding_model=:cfg AND embedding_dim=1024` | `SqlChunkRepo.verify()` == PASS (this is the script's own gate) |
| E2 | Legacy vectors never miscopied | `SELECT count(*) FROM chunks WHERE paper_id IN (import set) AND embedding_model IS NOT 'nvidia/nemotron-3.5-embed-1b:free'` = 0; the legacy table is never written (preflight/preview only) |
| E3 | Failed chunks stay pending and are re-embeddable (resume) | already covered by `tests/unit/test_reindex.py` [existing proof]; post-import: `--status` again |
| E4 | Rerun of a completed reindex performs 0 API calls | invoked twice; second run `updated=0, still_pending=0` |

## 5. Import idempotency / rollback (draft SQL review tests; run against an isolated DB only) [SPEC]

| # | Test |
|---|---|
| I1 | Import twice → second run inserts 0 rows (`ON CONFLICT DO NOTHING` + deterministic `uuid5` ids) |
| I2 | Chunks rerun → 0 duplicates (unique `(paper_id, chunk_index)`) |
| I3 | Invalid-quarantine paths: blank summary / duplicate identity rows are excluded by the candidate SELECT (measured today: 0 such rows in the archive) |
| I4 | Rollback deletes exactly the imported set (uuid5-derivable), leaves pre-existing 3 papers/211 chunks byte-identical (counts + `max(updated_at)` snapshot) |
| I5 | Partial import (simulate commit stop) → validation report flags missing chunks; rerun completes rather than duplicating |

## 6. Acceptance criteria for the future execution phase
1. `pytest tests -q` all green **and** zero regressions in api/chat semantics.
2. `reindex --verify` PASS over the combined corpus (2 987 chunks expected).
3. Post-import validation SQL (`docs/sql/legacy_arxiv_validation.sql`) yields all-OK.
4. Spot retrieval: one archive-abstract question, one user-paper question, one off-corpus question — from the
   runbook's handset.
5. No unapproved schema/migration/data writes; approval trail in the runbook.
