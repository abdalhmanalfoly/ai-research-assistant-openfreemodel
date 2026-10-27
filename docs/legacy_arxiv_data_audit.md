# Legacy arXiv Archive — Verified Data Audit

All numbers below were **observed** on the live Supabase project (`postgres` role, read-only) on **2026-10-27**.
Queries used: `notes` in the audit §5. Unverified items are explicitly marked.

## 1. Counts (live, read-only)

| Table | Rows |
|---|---|
| `papers` | **3** — `inbound7528663078505781792` (98 chunks, owner set), `Attention Is All You Need` (40 chunks, DOI `10.48550/arXiv.1706.03762`), `radiol.221257` (73 chunks, DOI null) |
| `chunks` | **211** — all on `nvidia/nemotron-3.5-embed-1b:free` / dim 1024, 0 NULL vectors, unit-norm 1.0000 |
| `papers_arxiv_legacy` | **2 776** — live count (the brief-level estimate of 2 789 is stale or from an earlier tool summarizing a different point in time) |
| `ingestion_jobs` | 3 |
| `alembic_version` | **0007** |

## 2. Legacy schema (verified via `information_schema` + `pg_constraint`)

```
field          text        -- crawl-bucket label (machine_learning 1000, ai 996, deep_learning 741, + small manual batches summing exactly to 2776)
id             text  PK    -- PRIMARY KEY confirmed: papers_arxiv_legacy_pkey; values = http://arxiv.org/abs/<base>v<n>  (100% http, 100% versioned, 0 "/pdf/" variants)
title          text        -- non-blank 2776/2776
authors        ARRAY       -- non-null 2776/2776
published      timestamptz -- 0 null; range 1998-01-28 .. 2026-10-20
summary        text        -- non-blank 2776/2776 (the arXiv ABSTRACT — metadata only, no full text)
pdf_url        text        -- non-blank 2776/2776 (external arxiv.org link; NOT a stored bucket object)
categories     ARRAY       -- cs.AI-heavy
embedding      vector      -- non-null 2776/2776, all exactly 1024-dim, unit norm (avg/±0.0004)
```

**Absent by design**: no `status`, no `owner_id`, no `created_at`/`updated_at`, **no `embedding_model` /
`embedding_dim` provenance columns**, no FK to `papers`/`chunks`. RDS `relrowsecurity = false` (the RLS-disabled
advisory already reported).

## 3. Data quality / duplicates (all verified)

| Check | Result |
|---|---|
| Blank / missing title | 0 |
| Blank / missing summary | 0 |
| Blank / missing pdf_url | 0 |
| Summary length | min 84 / p50 1 111 / mean 1 127 / p90 1 653 / p95 1 801 / max **2 790** chars → 0 rows need truncation; 0 rows under 50 chars; 0 rows over 6 000 chars |
| Duplicate **version-collapsed** arXiv IDs | **0 groups / 0 excess rows** (base id `regexp_replace(id, 'v[0-9]+$', '')` |
| Duplicate full `id` URLs | none possible — `id` is a PRIMARY KEY |
| Summary character volume | 3 127 793 chars summary + 214 740 chars title |
| Overlap with `papers` | **0** rows match any `papers.source_url` (checks both http and https forms), and `papers` DOI `10.48550/arXiv.1706.03762` has **0** legacy matches — the two corpora are disjoint |
| Legacy `paper_id`-style join attempt to `chunks` | structurally impossible (`chunks.paper_id` is uuid, legacy `id` is a URL text) |

## 4. Legacy vectors (foreign-model provenance — verified finding)

Probe (0 writes; one query-side embedding call): a legacy row's **own summary**, embedded with the current
model (`nvidia/nemotron-3.5-embed-1b:free`, dim 1024), scored cosine similarity **−0.007** against its own stored
legacy vector; its top-3 nearest legacy neighbors scored 0.076–0.083 (noise regime).

If the stored vectors had been produced by the current model, self-similarity would be ≈0.8+. Conclusion
(verified, not assumption): **all 2776 legacy embeddings come from a different (unrecorded) model** and are
architecturally unusable; they must never be copied into retrieval. The current 211-chunk corpus is proven
current-model + correct: per-paper "self-retrieval" re-embedding probes returned 5/5 in-paper top hits with
scores 0.95–1.00, and unit-norm checks pass on all 211 vectors.

## 5. Unverified items (blockers / required preflight)

- Whether extension `uuid-ossp` (or `pgcrypto` for `gen_random_uuid` fallback) is enabled — `TODO: VERIFY`
  (interpretation query shipped in preflight SQL).
- Row-level security policies are **not** defined (checked; empty `pg_policies`) and the archive has RLS off —
  this is historical, not a part of this import.
- The exact free-tier request-rates for the embedding provider — not officially documented; treated unknown and
  mitigated by bounded batches/backoff.
- `papers.owner_id —` semantic future: all 3 live papers carry owners, governance/sign-off for archive rows to
  have `owner_id = NULL` (import draft defaults to NULL = corpus-wide visibility, matching today's unfiltered
  chat behavior).

## 6. Reconciliation contract (what future validation queries will compare)

The import phase (if approved) defines:
- expected papers delta = count(*) from staging candidate SELECT = **2 776** (measured today),
- expected chunks delta = identical number (exactly 1 abstract chunk each),
- all new `papers.id` ∈ {uuid5('urn:x-arxiv-legacy:' || legacy.id)}; all new chunks have `embedding IS NULL`
  until the reindex phase fills them with provenance `(model='nvidia/nemotron-3.5-embed-1b:free', dim=1024)`,
- the 3 pre-existing papers and 211 chunks must remain byte-identical (counts + max(updated_at) snapshots).
