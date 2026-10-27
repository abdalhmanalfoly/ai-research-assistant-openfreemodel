# Combined Retrieval Architecture — User Papers + Shared arXiv Archive

Status: **REVIEW-ONLY / NOT EXECUTED.** This document proposes a design; nothing in it has been run against
production. Authoritative schema facts were verified read-only against the live Supabase project on 2026-10-27.

---

## 1. Current architecture (verified by code inspection)

### 1.1 Request → answer pipeline (actual code paths)

| Step | File / function | Notes |
|---|---|---|
| 1. Endpoint | `app/api/routes/chat.py::chat()` | `POST /api/chat`; `ChatRequest` validation in `app/schemas/chat.py` (question ≤2000 chars, `paper_ids` must be UUIDs, `top_k`, year bounds) |
| 2. Authentication | **none on `/api/chat` today** | no auth dependency; `papers.owner_id` exists in the live DB (added by teammate revisions `0006`/`0007`) but `app/api/routes/chat.py` and `app/services/types.py::SearchFilters` carry **no user scoping**. Local ORM (`app/db/models.py`) does not even map `owner_id` — local/live schema drift, recorded in §7 as a blocker |
| 3. Orchestration | `app/services/chat_service.py::ChatService.answer()` | retrieves → selects evidence → LLM → validates citations; shortfall handling: `NO_CORPUS` / `NO_EVIDENCE` / model-flagged branches |
| 4. Query embedding | `app/services/retrieval_service.py::RetrievalService.retrieve()` | `embed_query(question)`; loud `EmbeddingDimensionMismatch` on size mismatch |
| 5. Vector search | `app/db/repositories/pgvector_store.py::PgVectorStore.search()` | `chunks ⨝ papers` WHERE `Paper.status='indexed'` AND `embedding IS NOT NULL` AND `embedding_model = :cfg` AND `embedding_dim = :cfg`; `ORDER BY embedding <=> :query LIMIT top_k*3`; score = `1 − cosine_distance` |
| 6. Filtering/selection | `app/services/evidence_service.py::select_evidence()` | threshold 0.30 → token-Jaccard dedupe 0.85 → 12 000-char budget → max 5 items |
| 7. Prompt | `app/services/prompts.py::build_messages()` | system rules + `<evidence id=…>` tags |
| 8. LLM | `app/services/providers/openrouter_chat.py::OpenRouterChatModel.chat()` | `nvidia/nemotron-3.5-super-vl:free`, JSON mode; `finish_reason="length"` → loud `InvalidLLMResponse` |
| 9. Citations | `app/services/citation_service.py::parse_llm_output()/process_answer()` | invalid ids stripped; ids resolve to actually-retrieved chunks only |
| 10. Frontend | `frontend/js/api/chat.js`, `frontend/js/pages/chat.js` | `retrieved_chunks` → meta line; warn callout from `insufficient_evidence === true` |

Configuration values verified in `app/core/config.py`: `RETRIEVAL_TOP_K=5`, `RETRIEVAL_THRESHOLD=0.30`,
`CHUNK_MAX_CHARS=1200`, `EMBEDDING_BATCH_SIZE=64`, effective evidence budget 12 000 chars, overfetch factor 3
(`RagConfig.overfetch_factor` default in `chat_service.py`).

### 1.2 Root cause: why the archive is not searched

`PgVectorStore.search()` selects **exclusively** from `chunks` joined to `papers`. `public.papers_arxiv_legacy`
is referenced **zero times** in `app/`, `scripts/`, `migrations/`, or `tests/` (repo-wide grep), is not mapped by
any ORM model, and cannot even be joined structurally (its PK `id` is the raw arXiv-URL text; `chunks.paper_id`
is `uuid → papers.id`). The archive is completely disconnected — not filtered out, simply unreachable.

Current retrieval therefore already treats "one source only"; there is no existing multi-source normalisation,
no per-source failure policy, and no auth-aware scoping beyond caller-supplied `paper_ids`.

---

## 2. The two data sources (verified, 2026-10-27, read-only)

### 2.1 User papers — `papers` (3 rows) / `chunks` (211 rows)

- `papers`: `id uuid PK` (no DB default — the ORM supplies `uuid4` in Python); `title`, `authors`, `status` NOT NULL **with no DB defaults**; `doi varchar(255) NULL` unique **per owner** (`uq_papers_owner_doi(owner_id, doi)`); `content_hash varchar(64) NULL` unique **per owner**; `source_url`, `storage_key` nullable (no uniqueness); `owner_id uuid NULL → users(id) ON DELETE SET NULL`; **all 3 live papers have an owner**.
- `chunks`: `id uuid PK`; FK `paper_id → papers(id) ON DELETE CASCADE`; **UNIQUE (paper_id, chunk_index)** — a rerun-safe keyed slot; `embedding vector(1024)` **nullable** (a chunk may exist before embedding); `embedding_model`/`embedding_dim` nullable provenance columns (written by the reindex machinery); HNSW cosine index (`ix_chunks_embedding_hnsw`).
- RLS: `papers`/`chunks` `relrowsecurity = true`, `relforcerowsecurity = false`, **zero policies** in `pg_policies` → the table-owner role the app connects as is unaffected (owner bypass without FORCE). **No per-user isolation is currently enforced anywhere in chat retrieval**; the 3 user papers are visible to every caller today. We preserve exactly this behavior — the design must neither weaken nor silently "improve" it (any real ACL work is an explicit approved change, §6.4).
- Searchability: a paper becomes searchable when `status='indexed'` (set by `IngestionService.run_job` only after vectors are stored and validated) **or**, for the import path, via an explicit status flip gated on `SqlChunkRepo.verify() == PASS` (§5).

### 2.2 Archive — `public.papers_arxiv_legacy` (2 776 rows)

- PK `id text` = original arXiv abs URL (`http://arxiv.org/abs/<id>v<n>`); 100 % http, 100 % version-suffixed, no `/pdf/` variants. **Zero** version-collapsed duplicates (0 groups where the base identifier repeats).
- 100 % of rows have non-blank `title`, `summary`, `pdf_url`; summaries 84–2 790 chars (avg 1 127, p90 1 653, p95 1 801) — none oversize, none undersize; 0 overlap with any live `papers` row by URL/DOI.
- `summary` is the arXiv **abstract**. There is no full text, no stored PDF (only external `pdf_url`), no page metadata.
- `embedding`: non-null ×2 776, 1024-dim, **foreign-model provenance** — a probe embedding a legacy row's own abstract with the *current* model scored self-similarity **−0.007** (orthogonal); nearest-neighbor sims ~0.08 (noise). **Never reuse; treat as incompatible by construction.**
- RLS **disabled**; no provenance columns; unrelated to any application code.

Full profile: `docs/legacy_arxiv_data_audit.md`.

---

## 3. Architecture decision

### Comparison (Option A = separate archive retrieval; Option B = import abstracts into `papers`/`chunks`)

| Dimension | Option A — separate archive retrieval | Option B — staged abstract import (chosen) |
|---|---|---|
| Implementation effort | New legacy-mapped ORM repo or raw-SQL archiver, second search call, new merge/ranking layer, second failure path, new embedder script for a table the reindex tooling does not know | One-time SQL import (draft exists) + existing reindex run; **no new retrieval code** |
| Data integrity | Legacy table untouched; but archive embeddings need **a provenance-bearing home** — the legacy table has no model/dim columns, so Option A *requires* either overwriting in place (loses data, ambiguous provenance — both rejected) or a **new table = production migration** | Uses verified columns only; `(paper_id, chunk_index)` uniqueness + deterministic IDs make dup-impossible; zero schema change |
| Privacy / access control | Cleanly separated from user data | Archive inserted with `owner_id = NULL` (corpus-neutral, mirrors today's "no owner filter"; retrieval is unfiltered either way) |
| Embedding work | New storage + new script (2 776 rows) | **Existing, tested `scripts/reindex_embeddings.py` selects and embeds our chunks directly** (pending predicate: no embedding / model/dim mismatch), resumable, provenance-writing, verify()-gated |
| Search quality | Same model+dim ⇒ same scores, but two merge paths must be calibrated/kept in sync | Single `<=>` score space, single threshold semantics — **no cross-source calibration at all** |
| Latency | 2 DB searches per question | 1 search over ~2,987 vectors with the existing HNSW index |
| Citation correctness | New citation variant (no page numbers, archive metadata) grafted into `Citation` consumers | One path: `page_number=NULL` (schema documents `None = unknown`), `doi=NULL`, `source_url=legacy URL` — rendered correctly by the existing frontend (`normalizeCitation` handles nulls) |
| Idempotency | Own implementation required | Deterministic `uuid5` keys + `ON CONFLICT DO NOTHING` + unique `(paper_id, chunk_index)` |
| Rollback | Disconnect a config flag, data untouched | Delete exactly the `uuid5`-derived rows (draft `legacy_arxiv_rollback_draft.sql`); FK cascade verified |
| Future maintenance | Two retrieval stacks, two embedding pipelines to keep model-matched | One; model changes flow through the existing reindex flow |

### Decision: **Option B**

Rationale grounded in the actual code:

1. **Option A cannot proceed without a schema migration.** Its embedding store must carry `embedding_model` /
   `embedding_dim` provenance (architectural rule: vectors are never compared across models; the whole
   `_compatible()` + `verify()` design depends on it). The legacy table has no such columns, and overwriting its
   historical vectors is forbidden in this phase. "No schema change" is a hard review constraint → Option A is
   the riskier path despite its clean separation.
2. **Option B needs zero migration and zero retrieval code**: the import lands as ordinary `papers`+`chunks`
   rows; the existing search, scoring, threshold, budget, dedupe, LLM, and citation machinery then retrieve from
   *both* corpora in one query with one score semantics. "Combined retrieval" is achieved by eliminating the
   disconnect, not by adding a merge layer.
3. The archive is *metadata-only*; as abstracts it is structurally equivalent to an existing chunk (text +
   provenance). No fidelity is lost; honest non-claims are preserved by `page_number=NULL`, `section='Abstract'`, and `papers.storage_key IS NULL` (no PDF claim).
4. Rollback of B is deterministic (`uuid5` provenance baked into primary keys) — rollback precision without
   manifest dependency and without touching legacy data.### What we deliberately do NOT do (Phase 8 scope)

- Do **not** store raw arXiv IDs in `doi` (the citation contract treats DOI as a real DOI; `uq_papers_owner_doi` also makes global per-owner collisions possible). Legacy DOI stays `NULL`.
- Do **not** load legacy vectors into `chunks.embedding` under any model label.
- Do **not** change embeddings/thresholds/models/schemas; do **not** touch the 3 user papers.
- Do **not** claim full-text archive search — `options: QUERY returns abstracts only`, enforced structurally (`storage_key` NULL, one abstract chunk per paper).

---

## 4. Unified retrieval after Option B

After the approved execution (runbook: `docs/combined_retrieval_runbook.md`):

```
question ──embed (nematron-3.5-embed-1b:free, dim 1024)
        │
        ▼
  PgVectorStore.search  (chunks ⨝ papers: 3 user papers' full-text chunks
        │                + imported archive papers' abstract chunks,
        │                all status='indexed', all current-model 1024-d vectors)
        ▼
  select_evidence (0.30 threshold, Jaccard 0.85, 12k budget, top 5)
        ▼
  build_messages → nemotron-3.5-super-vl:free → citations + insufficient_evidence
```

- **Scores are directly comparable across sources** — same operator, same metric, same vectors' model/dim — so
  no cross-source rank normalisation is needed, and neither corpus can "win" by construction difference.
- **Dedup**: the existing 0.85 token-Jaccard dedupe also collapses near-identical abstracts (the archive has no
  duplicate base IDs; near-duplicate *content* across papers is caught by the existing rule). Identity dedupe is
  done at import time (unique keys), not at query time.
- **Failure policy (already inherent, now documented)**: a source is either eligible or not. The store filters
  model-compatible eligible chunks; if the archive's vectors were malformed/missing, those rows are excluded and
  results degrade to the user corpus only — the deployment's standard `no_evidence_after_filtering` path then
  fires; there is no "partial success" flag to fake. Health observability: `/api/health` + `SqlChunkRepo.verify()`
  + per-request chat diagnostics (`qhash`, `top_scores`, `evidence_stats`, `no_evidence_after_filtering` reasons)
  already expose which corpus contributed (retained).
- **`paper_ids` filter** continues to work as-is — callers may restrict to the 3 user papers, and the UI's
  existing "paper chips" keep filtering both compiled; archive papers appear as normal selectable papers.

## 5. Import design (Option B execution sketch — full draft in `docs/sql/`)

- **Identity**: `papers.id = uuid5(NAMESPACE_URL, 'urn:x-arxiv-legacy:' || legacy.id)` — deterministic,
  rollback-computable, collision-free, no new columns; requires `uuid-ossp` (preflight verifies presence;
  `TODO: VERIFY` in DB — checklist in preflight SQL).
- **Rows per legacy record**: 1 `papers` row (`owner_id=NULL`, `status='pending'`, `doi=NULL`,
  `content_hash=NULL`, `source_url=<raw legacy URL>`, `title`/`authors`/`abstract=summary`,
  `publication_year=date_part('year', published)`) + 1 `chunks` row (`text=summary`, `chunk_index=0`,
  `page_number=NULL`, `section='Abstract'`, `embedding=NULL` — filled by the reindex run).
- **Idempotency**: `ON CONFLICT (id) DO NOTHING` (papers), `ON CONFLICT (paper_id, chunk_index) DO NOTHING` (chunks); rerun-safe; the insert SELECT's WHERE clause re-validates eligibility per row so partial re-runs cannot double-insert.
- **Serialization**: insertion order papers→chunks in one transaction per batch; runbook requires the existing
  `reindex_lock` advisory lock idiom (concurrent-safe; reconciliation query detects any anomaly).
- **Status flip**: only after `SqlChunkRepo.verify()` reports `ok` for the imported set — implements the same
  "indexed ⟹ current-model vector exists" invariant the upload pipeline guarantees.
- **Existing 3 papers**: never touched — preflight asserts their counts + `owner_id` non-null + `storage_key`
  non-null remain unchanged pre/post import; rollback deletes *only* `uuid5`-derived rows.

## 6. Privacy, attribution, and honesty

### 6.1 User paper access rules (preserved)
No change: retrieval remains corpus-wide exactly as today (chat accepts caller-supplied `paper_ids`).
Nothing in this design weakens, bypasses, or introduces user-scoping.

### 6.2 Archive visibility
`owner_id=NULL` + retrieval unfiltered = visible corpus-wide, same as user papers today. **Open product
decision (blocker, §7)**: when teammate's per-user RLS policies land, an explicit "archive rows are public"
policy must be added (e.g. `USING (owner_id IS NULL)`), or the archive becomes invisible to end users. Tracked in
the runbook's stop conditions.

### 6.3 Honest citations for abstracts
`Citation` fields for archive evidence: `title` (legacy title), `authors` (legacy authors), `page_number=NULL`
(abstract), `section='Abstract'`, `source_url=legacy URL` (external, verified present on 100 % rows), `doi=NULL`.
Frontend (`frontend/js/utils/renderCitations` + `normalizeCitation`) already renders null-safe metadata.
The system prompt's grounding rules already prevent claiming page-level facts; nothing pretends the model read
the full PDF.

### 6.4 Known limitation carried forward (not created by this design)
Chat retrieval is not user-scoped today (no auth dependency on `/api/chat`; `SearchFilters` has no user field).
This is unchanged by Option B. Any real multi-tenant isolation is a separate approved change out of scope here.

## 7. Vocabulary / risks / blockers

1. **Migration history drift**: live `alembic_version=0007` (`0006`/`0007` add `users`/`owner_id`/auth stack),
   local chain ends at `0005`; local ORM doesn't map `owner_id`. **Blocked until the teammate's revisions are in
   the repo**; no plan in this doc executes Alembic.
2. **`uuid-ossp` presence in the target DB** — `TODO: VERIFY` (preflight query provided; import draft falls back
   to pre-generated ID manifest if absent).
3. **Provider free-tier quotas** — model is $0/catalog-listed; request-rate limits not officially published;
   reindex script already rate-limit-aware with bounded backoff; time estimate in the reindex plan.
4. **UX**: 2 777 archive papers enter `papers` (library list server-paginated, limit 20, so robust); library will
   be dominated by archive entries. Optional, non-blocking follow-up: UI filter (`owner_id IS NULL` tag or
   category chips). Backend behavior unaffected.
5. Legacy table remains as historical data (never written; ideally future RLS-enable decision separate from this
   plan).

## 8. Deliverables index

- `docs/legacy_arxiv_data_audit.md` — verified DB facts.
- `docs/legacy_arxiv_reindex_plan.md` — embedder/reindex audit + backfill plan + costs.
- `docs/combined_retrieval_test_plan.md` — test matrix mapping to existing suites.
- `docs/combined_retrieval_runbook.md` — future approved execution.
- `docs/sql/legacy_arxiv_preflight.sql` — read-only checks.
- `docs/sql/legacy_arxiv_stage_preview.sql` — read-only candidate preview.
- `docs/sql/legacy_arxiv_import_draft.sql` — **NOT FOR EXECUTION**.
- `docs/sql/legacy_arxiv_validation.sql` — post-import checks.
- `docs/sql/legacy_arxiv_rollback_draft.sql` — **NOT FOR EXECUTION — DRAFT**.
