-- =============================================================================
-- legacy_arxiv_import_draft.sql
--
--        ***  NOT FOR EXECUTION — AWAITING EXPLICIT APPROVAL  ***
--
-- Dry, transient TEMP table only (schema-local, session-scoped, auto-dropped).
-- Requires uuid-ossp (preflight §0). One transaction per batch; step 2b (status
-- flip) has its own approval gate after reindex --verify PASS.
--
-- Schema verified live 2026-10-27: papers(...) + chunks(...) — see blast_radius
-- notes inline. NOTHING here touches the 3 pre-existing papers/211 chunks.
-- =============================================================================
\set NAMESPACE '6ba7b811-9dad-11d1-80b4-00c04fd430c8'

-- -----------------------------------------------------------------------------
-- Phase 1: deterministic staging manifest (TEMP — vanishes with the session;
--          no production schema is created or altered).
-- -----------------------------------------------------------------------------
create temp table legacy_candidates as
select 'urn:x-arxiv-legacy:' || l.id              provenance_key,
       uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                        'urn:x-arxiv-legacy:' || l.id)  paper_id,
       l.id                                      legacy_id,
       l.title                                   title,
       (select coalesce(jsonb_agg(a::text), '[]'::jsonb) from unnest(l.authors) a) authors,
       btrim(l.summary::text)                    summary,
       l.pdf_url                                 source_url,
       date_part('year', l.published)::int       pub_year,
       l.categories                              categories,
       left(coalesce(categories::text,''), 255)  bucket_key,
       -- fail-closed validation per row (mirrors law of the preview §0)
       (btrim(l.title) <> ''
        and btrim(l.summary::text) <> ''
        and btrim(l.pdf_url) <> ''
        and length(btrim(l.summary::text)) <= 3200
        and not exists (select 1 from papers_arxiv_legacy l2
                         where regexp_replace(l2.id, 'v[0-9]+$', '')
                                   = regexp_replace(l.id, 'v[0-9]+$', '')
                           and l2.id <> l.id)
        and not exists (select 1 from papers p
                         where p.source_url in (l.id, replace(l.id,'http://','https://')))) accepted,
       -- version-sibling quarantine reason
       exists (select 1 from papers_arxiv_legacy l2
                where regexp_replace(l2.id, 'v[0-9]+$', '')
                          = regexp_replace(l.id, 'v[0-9]+$', '')
                  and l2.id <> l.id) as is_quarantined
from papers_arxiv_legacy l;

-- Report summary (log to the runbook artifact):
select count(*) candidate_rows,
       count(*) filter (where accepted) accepted_rows,
       count(*) filter (where is_quarantined) quarantined_rows,
       count(distinct paper_id) filter (where accepted) unique_ids
from legacy_candidates;
-- STOP branch: if unique_ids <> accepted_rows → abort (identity collision impossible today).

-- -----------------------------------------------------------------------------
-- Phase 2: INSERT papers (status='pending' — becomes 'indexed' only in 2b)
--          batch loop lives in the approved execution; SQL provides one batch
--          body per call (LIMIT/OFFSET on paper_id ordering).
-- -----------------------------------------------------------------------------
begin;

insert into public.papers (
    id, title, authors, abstract, publication_year, doi, source_url,
    storage_key, content_hash, status, error_message, created_at, updated_at, owner_id)
select c.paper_id, c.title, c.authors, c.summary, c.pub_year,
       null::varchar(255),                                    -- never an arXiv id inside DOIs
       c.source_url,
       null::text,                                            -- no PDF claim
       null::varchar(64),                                     -- content_hash stays for PDF-only
       'pending'::varchar(20),
       null::text,
       now(), now(),
       null::uuid                                             -- shared/corpus-neutral owner decision
from legacy_candidates c
where c.accepted
  and not c.is_quarantined
  -- batch window: set per approved execution (e.g. id BETWEEN batch_lo and batch_hi)
  and true
on conflict (id) do nothing;   -- idempotent rerun; concurrency serialized by reindex advisory lock

with ins as (select count(*) n from public.papers p where p.owner_id is null and p.storage_key is null
             and exists (select 1 from legacy_candidates c where c.accepted and c.paper_id = p.id))
select n as inserted_papers_snapshot from ins;   -- artifact = count; compare per batch

commit;

-- -----------------------------------------------------------------------------
-- Phase 2b (separate approval + gate: run ONLY after reindex --verify PASS)
-- -----------------------------------------------------------------------------
-- begin;
-- update public.papers p set status='indexed', updated_at=now()
-- where p.status='pending'
--   and p.storage_key is null
--   and p.owner_id is null
--   and exists (select 1 from legacy_candidates c where c.accepted and c.paper_id = p.id);
-- commit;

-- -----------------------------------------------------------------------------
-- invariants (assert after phase 2 and after 2b)
-- -----------------------------------------------------------------------------
select count(*) papers_created   -- = accepted_rows
from papers_arxiv_legacy l
where exists (select 1 from papers pp
              where pp.id = uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                                             'urn:x-arxiv-legacy:' || l.id));
-- EXPECT papers_created = accepted_rows (mirroring the runbook-import preview)
-- and no doi/content_hash ever written:
select count(*) flagged
from papers p
where p.owner_id is null and p.storage_key is null
  and p.content_hash is not null;
-- EXPECT: 0 (this query is a post-import sentinel).
