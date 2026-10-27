-- =============================================================================
-- legacy_arxiv_stage_preview.sql — 100% READ-ONLY. Shows the exact candidate
-- records the import draft would create, plus exclusions, WITHOUT writing.
-- The candidate expression MUST mirror docs/sql/legacy_arxiv_import_draft.sql.
-- =============================================================================
\set NAMESPACE '6ba7b811-9dad-11d1-80b4-00c04fd430c8'   -- uuid.NAMESPACE_URL, fixed constant shared with the draft

-- ---------------------------------------------------------------------------
-- 0. Validation summary: how many rows pass every eligibility rule
-- ---------------------------------------------------------------------------
with cand as (
    select l.id,
           left(l.title, 60)                                  title_head,
           length(btrim(l.summary::text))                     summary_len,
           date_part('year', l.published)::int                pub_year,
           -- deterministic import PK (same expression the draft uses)
           uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                            'urn:x-arxiv-legacy:' || l.id)    target_paper_id,
           l.title is null or btrim(l.title) = ''              invalid_title,
           l.summary::text is null or btrim(l.summary::text) = '' invalid_summary,
           l.pdf_url is null or btrim(l.pdf_url) = ''          invalid_url,
           length(btrim(l.summary::text)) > 3200               oversize_summary,
           exists (select 1 from public.papers p where p.source_url = l.id
                                                   or p.source_url = replace(l.id,'http://','https://')) already_imported,
           exists (select 1 from public.papers_arxiv_legacy l2
                    where regexp_replace(l2.id, 'v[0-9]+$', '') = regexp_replace(l.id, 'v[0-9]+$', '')
                      and l2.id <> l.id)                       version_sibling
    from public.papers_arxiv_legacy l
)
select count(*)                                                        total_rows,
       count(*) filter (where not invalid_title and not invalid_summary and not invalid_url
                          and not oversize_summary and not already_imported and not version_sibling)
                                                                       eligible,
       count(*) filter (where invalid_title)                           excluded_blank_title,
       count(*) filter (where invalid_summary)                         excluded_blank_summary,
       count(*) filter (where invalid_url)                             excluded_bad_url,
       count(*) filter (where oversize_summary and not invalid_summary) excluded_oversize,
       count(*) filter (where already_imported)                        excluded_already_in_papers,
       count(*) filter (where version_sibling and not already_imported) quarantined_version_siblings,
       count(distinct target_paper_id)                                 unique_paper_ids,
       min(summary_len), round(avg(summary_len)), max(summary_len)
from cand;

-- ---------------------------------------------------------------------------
-- 1. Sample of candidate rows (privacy-safe: no abstract text printed)
-- ---------------------------------------------------------------------------
select uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                        'urn:x-arxiv-legacy:' || l.id)  target_paper_id,
       left(id, 46)                                     legacy_id_head,
       left(title, 60)                                  title,
       array_length(authors, 1)                         author_count,
       left(categories::text, 50)                       categories,
       date_part('year', published)::int                pub_year,
       (pdf_url is not null)                            has_pdf_url,
       length(btrim(summary::text))                     summary_len
from public.papers_arxiv_legacy l
where btrim(title) <> ''
  and btrim(summary::text) <> ''
  and btrim(pdf_url) <> ''
  and length(btrim(summary::text)) <= 3200
order by id
limit 25;

-- ---------------------------------------------------------------------------
-- 2. Bucket/profile distribution (import coverage audit)
-- ---------------------------------------------------------------------------
select field, count(*) n, round(avg(length(btrim(summary::text)))) avg_len
from public.papers_arxiv_legacy
group by field order by n desc;

-- ---------------------------------------------------------------------------
-- 3. Simulated idempotency check (read-only): simulate a rerun of the import
--    by computing which candidate IDs ALREADY exist in papers right now.
-- ---------------------------------------------------------------------------
select count(*) as would_conflict_rows
from public.papers_arxiv_legacy l
join public.papers p
  on p.id = uuid_generate_v5('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
                             'urn:x-arxiv-legacy:' || l.id);
-- EXPECT: 0 today. After a first approved import: 2776 (proves uuid5 determinism).
