-- =============================================================================
-- legacy_arxiv_rollback_draft.sql
--
--            ***  NOT FOR EXECUTION — AWAITING EXPLICIT APPROVAL ***
--
-- Deletes ONLY the uuid5-derived import set; pre-existing user papers are
-- safe BY KEY SHAPE (they carry owner_id / storage_key values the archive set
-- has as NULL). Never delete by title/date/status alone.
-- Foreign-key facts verified 2026-10-27:
--   chunks.paper_id   → papers.id ON DELETE CASCADE
--   ingestion_jobs.paper_id → papers.id ON DELETE CASCADE  (imports create none)
--   no other table references papers (pg_constraint confrelid = papers).
-- The papers_arxiv_legacy table is NEVER dropped or modified by this script.
-- =============================================================================
\set IMPORT_NS '6ba7b811-9dad-11d1-80b4-00c04fd430c8'

-- -----------------------------------------------------------------------------
-- Step 0. Pre-delete audit (read-only) — the exact set that WOULD be deleted.
--         Every row here must have: uuid5-derived id, storage_key NULL,
--         owner_id NULL, created_at at/after the import start marker.
--         If ANY row fails those guards → STOP (mis-targeted keys).
-- -----------------------------------------------------------------------------
with import_ids as (
    select uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                            'urn:x-arxiv-legacy:' || l.id) import_id
    from papers_arxiv_legacy l)
select p.id, left(p.title, 60) title, p.status, p.created_at,
       (p.storage_key is null)  guard_storage_null,   -- must be true
       (p.owner_id is null)     guard_owner_null,     -- must be true
       (p.doi is null)          guard_doi_null        -- must be true
from papers p join import_ids i on i.import_id = p.id
order by p.created_at limit 50;
select count(*) total_would_delete from papers p
where p.id in (select uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                                       'urn:x-arxiv-legacy:' || l.id)
               from papers_arxiv_legacy l)
and p.storage_key is null and p.owner_id is null;

-- -----------------------------------------------------------------------------
-- Step 1. Smoke check: confirm NO ingest artifacts or external deps. exist
--         (belt-and-braces; imports create no ingestion_jobs).
-- -----------------------------------------------------------------------------
select count(*) dependent_jobs
from ingestion_jobs ij
where ij.paper_id in (select uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                                              'urn:x-arxiv-legacy:' || l.id)
                      from papers_arxiv_legacy l);
-- EXPECT: 0.

-- -----------------------------------------------------------------------------
-- Step 2. DELETION (explicit children first; the FK CASCADE is belt-and-braces).
-- Approve + wrap in begin/commit per runbook. Batch by id-range to bound locks.
-- -----------------------------------------------------------------------------
begin;
-- 2a. chunks first (explicit children removal; the FK CASCADE is belt-and-braces)
delete from public.chunks
where paper_id in (select uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                                           'urn:x-arxiv-legacy:' || l.id)
                   from papers_arxiv_legacy l);
-- 2b. papers (guarded: only rows whose identity/shape match the import set)
delete from public.papers
where id in (select uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                                     'urn:x-arxiv-legacy:' || l.id)
             from papers_arxiv_legacy l)
  and storage_key is null
  and owner_id is null
  and doi is null;
-- commit;   -- after the runbook-approval of a verify step in the same
             -- transaction (stdout rowcounts recorded).

-- -----------------------------------------------------------------------------
-- Step 3. Post-rollback verification (read-only)
-- -----------------------------------------------------------------------------
select (select count(*) from public.papers)  papers,     -- EXPECT: 3
       (select count(*) from public.chunks)  chunks,    -- EXPECT: 211
       (select count(*) from papers p where p.storage_key is null)   archive_left,  -- EXPECT: 0
       (select count(*) from papers p where p.owner_id  is not null) user_papers;    -- EXPECT: 3
select embedding_model, count(*) n from public.chunks group by 1;  -- EXPECT: single current-model row
-- Invariant: the papers_arxiv_legacy table is byte-identical to pre-import (it
-- was never written); if validation Step 3 disagrees with the Phase-1 backup
-- snapshot → restore from backup (runbook Phase 1) — never manual row surgery.
