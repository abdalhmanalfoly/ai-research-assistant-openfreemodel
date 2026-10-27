-- =============================================================================
-- legacy_arxiv_validation.sql — read-only post-import validation for the
-- future approved execution. Mirror of the import draft's identity scheme.
-- Every EXPECT mismatch = stop condition; keep output logs as artifacts.
-- =============================================================================
\set IMPORT_NS '6ba7b811-9dad-11d1-80b4-00c04fd430c8'  -- shared with draft

-- 1. Imported set == candidate set (identity completeness; predicate mirrors
--    legacy_arxiv_import_draft.sql 'accepted' exactly: non-blank fields, ≤3200
--    chars, no version-sibling duplicate, not already imported by URL).
select count(*) imported_papers,
       (select count(*)
          from papers_arxiv_legacy l
         where btrim(l.title) <> ''
           and btrim(l.summary::text) <> ''
           and btrim(l.pdf_url) <> ''
           and length(btrim(l.summary::text)) <= 3200
           and not exists (select 1 from papers_arxiv_legacy l2
                            where regexp_replace(l2.id, 'v[0-9]+$', '')
                                      = regexp_replace(l.id, 'v[0-9]+$', '')
                              and l2.id <> l.id)
           and not exists (select 1 from papers p2
                            where p2.source_url in (l.id, replace(l.id, 'http://', 'https://')))) expected
from public.papers p
where p.storage_key is null and p.owner_id is null
  and exists (select 1 from papers_arxiv_legacy l
              where uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                                     'urn:x-arxiv-legacy:' || l.id) = p.id);
-- EXPECT imported_papers == expected (plus pilot rows still present, not deleted,
-- unless rollback ran first).

-- 2. Exactly one abstract chunk per imported paper, no duplicates
select count(*) from (
 select paper_id, count(*) n from public.chunks
 where paper_id in (select id from public.papers where storage_key is null and owner_id is null)
 group by paper_id having count(*) <> 1) bad;
-- EXPECT: 0 rows.

-- 3. Embedding integrity AFTER the reindex phase
select embedding_model, vector_dims(embedding) d, count(*) n
from public.chunks
where paper_id in (select id from public.papers where storage_key is null and owner_id is null)
group by 1,2;
-- EXPECT after reindex: single row — configured model / 1024 / ≈2776.

-- 4. NO legacy vector ever copied (provenance sentinel)
select count(*) flagged
from public.chunks c
join public.papers p on p.id = c.paper_id
where p.owner_id is null
  and (c.embedding_model is null or c.embedding_model <> 'nvidia/nemotron-3.5-embed-1b:free'
       or c.embedding_dim <> 1024);
-- EXPECT: 0.

-- 5. Citation honesty sentinels
select count(*) abstract_chunks_with_page  -- abstract chunks must not carry fake pages
from public.chunks c join public.papers p on p.id = c.paper_id
where p.owner_id is null and (c.page_number is not null or c.section is distinct from 'Abstract');
-- EXPECT: 0.

select count(*) fake_doi
from public.papers where owner_id is null and doi is not null;
-- EXPECT: 0 (arXiv ids never stored in the DOI column).

-- 6. User corpus untouched (compare against the Phase-1 backup snapshot)
select (select count(*) from public.papers where owner_id is not null) user_papers,  -- EXPECT: 3
       (select count(*) from public.chunks c join public.papers p on p.id=c.paper_id
         where p.owner_id is not null) user_chunks,                                  -- EXPECT: 211
       (select count(distinct embedding_model) from public.chunks) model_plurality;  -- EXPECT: 1

-- 7. Global key sanity
select count(*) from public.chunks c where not exists (select 1 from papers p where p.id=c.paper_id);
-- EXPECT: 0.
