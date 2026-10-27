-- =============================================================================
-- legacy_arxiv_preflight.sql — 100% READ-ONLY. Run in the approved execution
-- phase BEFORE any import or embedding step. Artifacts to keep: output logs.
-- Expected results inline; any mismatch = STOP CONDITION per the runbook.
-- =============================================================================
\echo '== 0. Environment =='
select version();                                           -- record
select extname, extversion from pg_extension where extname in ('uuid-ossp','pgcrypto','vector');
-- EXPECT: vector present. uuid-ossp preferred (uuid_generate_v5). If missing but
-- pgcrypto present, import draft uses a pre-generated ID manifest (see draft §5).

\echo '== 1. Migration state (blocker check) =='
select version_num as live_alembic_version from alembic_version;
-- EXPECT: '0007' (or newer known team revision). If it has REGRESSED → STOP.

\echo '== 2. Row counts (re-measure; expect papers=3, chunks=211, legacy=2776, jobs=3) =='
select (select count(*) from public.papers)          papers,
       (select count(*) from public.chunks)          chunks,
       (select count(*) from public.papers_arxiv_legacy) legacy,
       (select count(*) from public.ingestion_jobs)  jobs;

\echo '== 3. Current corpus integrity (regression baseline) =='
select embedding_model, embedding_dim, count(*) n, count(embedding) with_vector
from public.chunks group by 1,2;
-- EXPECT: single row nvidia/nemotron-3.5-embed-1b:free / 1024 / 211/211.
select left(title,40) title, status, count(c.id) chunks
from public.papers p left join public.chunks c on c.paper_id = p.id
group by p.id, p.title, p.status order by p.title;
select bool_and(owner_id is not null) all_user_papers_have_owner,
       bool_and(storage_key is not null) all_have_storage_key
from public.papers;
-- EXPECT: true/true (all 3 rows). These snapshot the "untouched" invariant.

\echo '== 4. Legacy data quality (expect all zeros) =='
select count(*) filter (where btrim(title)='')              blank_title,
       count(*) filter (where btrim(summary::text)='')      blank_summary,
       count(*) filter (where btrim(pdf_url)='')            blank_pdf_url,
       count(*) from public.papers_arxiv_legacy;
select regexp_replace(id, 'v[0-9]+$', '') base_id, count(*)
from public.papers_arxiv_legacy group by 1 having count(*) > 1;
-- EXPECT: 0 rows (no version-collapsed duplicate arXiv ids).

\echo '== 5. Corpus overlap (expect 0) =='
select count(*) matched
from public.papers_arxiv_legacy l
join public.papers p on p.source_url = l.id
                      or p.source_url = replace(l.id, 'http://', 'https://');

\echo '== 6. Import exits available (RLS / privileges) =='
select c.relname, c.relrowsecurity, c.relforcerowsecurity,
       (select count(*) from pg_policies pol where pol.tablename = c.relname) policy_count
from pg_class c where c.relname in ('papers','chunks','papers_arxiv_legacy');
-- EXPECT: papers/chunks relrowsecurity=true, force=false, policy_count=0 (owner-role bypass);
--         papers_arxiv_legacy rls off. Any other configuration → re-review.
select current_user,
       has_table_privilege(current_user, 'public.papers', 'insert')  can_insert_papers,
       has_table_privilege(current_user, 'public.chunks', 'insert')  can_insert_chunks,
       has_table_privilege(current_user, 'public.chunks', 'update')  can_update_chunks;
-- EXPECT: all true for the operator role.

\echo '== 7. Foreign pending chunks (expect 0 before the import) =='
select count(*) pending_now
from public.chunks
where embedding is null or embedding_model is distinct from 'nvidia/nemotron-3.5-embed-1b:free'
   or embedding_dim  is distinct from 1024;
-- EXPECT: 0. If > 0: external drift — resolve or scope backfill narrowly.

\echo '== 8. Reindex advisory lock status (pure catalog read; takes no lock) =='
select count(*) active_reindex_locks
from pg_locks
where locktype = 'advisory' and objid = 726001;
-- EXPECT: 0. If 1, wait for the other run to finish (or confirm source with teammate).
