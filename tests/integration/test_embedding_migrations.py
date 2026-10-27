"""INTEGRATION: embedding migrations (0004/0005) and re-indexing on a REAL PostgreSQL + pgvector.

Path 1 (BGE database): schema at 0003 with 1024-dim vectors -> `alembic upgrade head` (0004 + 0005 with
EMBEDDING_DIMENSIONS=1024) and re-indexing with the configured OpenRouter embedding model.
Path 2 (OpenAI database): schema at 0004 with 1536-dim text-embedding-3-small vectors -> 0005 at 1024 preserves them
in embedding_prev_or, re-dimensions the column, and marks everything pending.
SKIPPED unless TEST_DATABASE_URL is set (database name must contain 'test'). DESTRUCTIVE to that database.
The embedding provider is a MOCKED OpenRouter server; the real provider API is never called."""
import os
import random
import subprocess
import sys
import unittest

URL = os.getenv("TEST_DATABASE_URL", "")
try:
    import sqlalchemy as sa
    from app.db.repositories.chunk_repo import ReindexAlreadyRunning, SqlChunkRepo, reindex_lock
    from app.db.repositories.pgvector_store import PgVectorStore
    HAVE = True
except Exception:
    HAVE = False
ENABLED = bool(URL) and "test" in URL.rsplit("/", 1)[-1] and HAVE

from app.core.errors import EmbeddingDimensionMismatch
from app.services.reindex_service import ReindexService
from app.services.types import SearchFilters
from tests.fixtures.openrouter_fakes import FakeOpenAI, embedder, hash_vec

NEW, OLD = "nvidia/nemotron-3.5-embed-1b:free", "BAAI/bge-large-en-v1.5"
LEGACY_OPENAI = "text-embedding-3-small"  # the model the migration-0004 era was deployed with
P = lambda i: f"00000000-0000-0000-0000-00000000000{i}"
C = lambda n: f"00000000-0000-0000-0000-0000000001{n:02d}"
TYPE_Q = ("select format_type(atttypid, atttypmod) from pg_attribute "
          "where attrelid='chunks'::regclass and attname=:c")


@unittest.skipUnless(ENABLED, "needs TEST_DATABASE_URL (database name must contain 'test')")
class Migration0005Tests(unittest.TestCase):
    def alembic(self, *args, dims="1024"):
        env = {**os.environ, "DATABASE_URL": URL, "EMBEDDING_DIMENSIONS": dims}
        r = subprocess.run([sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])

    def reset(self):
        self.eng = sa.create_engine(URL)
        with self.eng.begin() as c:
            c.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))

    def seed_bge_database(self):
        """A database as deployed with BGE: schema at 0003, 3 papers, 12 chunks with 1024-dim vectors."""
        self.reset()
        self.alembic("upgrade", "0003")
        rnd = random.Random(7)
        with self.eng.begin() as c:
            for i in (1, 2, 3):
                c.execute(sa.text("INSERT INTO papers(id,title,authors,doi,status,created_at,updated_at,content_hash,publication_year) "
                                  "VALUES (:id,:t,'[\"A. Author\"]',:doi,'indexed',now(),now(),:h,2020+:i)"),
                          {"id": P(i), "t": f"Synthetic paper {i}", "doi": f"10.0/syn{i}", "h": f"h{i}", "i": i})
            n = 0
            for i in (1, 2, 3):
                for j in range(4):
                    n += 1
                    v = "[" + ",".join(f"{rnd.uniform(-1, 1):.6f}" for _ in range(1024)) + "]"
                    c.execute(sa.text("INSERT INTO chunks(id,paper_id,text,page_number,section,chunk_index,embedding,embedding_model,"
                                      "embedding_dim,created_at) VALUES (:id,:p,:t,:pg,:s,:ci,CAST(:v AS vector),:m,1024,now())"),
                              {"id": C(n), "p": P(i), "t": f"chunk {n} text about topic{n}", "pg": None if n % 5 == 0 else n,
                               "s": "Methods" if n % 2 else None, "ci": j, "v": v, "m": OLD})

    def q(self, sql, **kw):
        with self.eng.connect() as c:
            return c.execute(sa.text(sql), kw).all()

    def test_upgrade_preserves_everything_and_marks_all_chunks_pending(self):
        self.seed_bge_database()
        meta = "select id::text, paper_id::text, text, page_number, section, chunk_index from chunks order by id"
        old_vecs = "select id::text, embedding::text from chunks order by id"
        before_meta, before_vecs = self.q(meta), self.q(old_vecs)
        self.assertEqual(len(before_meta), 12)
        self.alembic("upgrade", "head")
        self.assertEqual(self.q(meta), before_meta)  # ids, text, pages, sections, paper links, order: identical
        self.assertEqual(self.q("select id::text, embedding_prev::text from chunks order by id"), before_vecs)  # BGE kept
        with self.eng.connect() as c:
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding"}), "vector(1024)")
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding_prev"}), "vector(1024)")
        self.assertEqual(self.q("select count(*) from chunks where embedding is null and embedding_model is null "
                                "and embedding_dim is null and embedding_prev_model = :m", m=OLD)[0][0], 12)
        self.assertEqual(self.q("select count(*) from papers")[0][0], 3)
        # nothing is searchable until re-indexed; and old vectors can never be mixed in
        sf = sa.orm.sessionmaker(self.eng)
        self.assertEqual(PgVectorStore(sf, NEW, 1024).search(hash_vec("topic1", 1024), 5, SearchFilters()), [])

    def test_reindex_after_migration_fills_new_column_and_verifies(self):
        self.seed_bge_database()
        self.alembic("upgrade", "head")
        sf = sa.orm.sessionmaker(self.eng, expire_on_commit=False)
        repo, fake = SqlChunkRepo(sf, NEW, 1024), FakeOpenAI()
        self.assertEqual((repo.count_pending(), repo.count_total()), (12, 12))
        self.assertFalse(repo.verify().ok)
        self.assertGreater(repo.pending_chars(), 0)
        rep = ReindexService(repo, embedder(fake), batch_size=5).run()
        self.assertEqual((rep.updated, rep.failed_ids, rep.batches), (12, [], 3))
        v = repo.verify()
        self.assertTrue(v.ok, v)
        self.assertEqual((v.total, v.current, v.pending, v.wrong_dimension), (12, 12, 0, 0))
        self.assertEqual(self.q("select count(*) from chunks where embedding_prev is not null")[0][0], 12)  # still there
        # semantic retrieval works on the new vectors, with metadata intact
        hits = PgVectorStore(sf, NEW, 1024).search(hash_vec("chunk 7 text about topic7", 1024), 3, SearchFilters())
        self.assertEqual(hits[0].chunk_id, C(7))
        self.assertAlmostEqual(hits[0].score, 1.0, places=4)
        self.assertEqual((hits[0].page_number, hits[0].paper.title), (7, "Synthetic paper 2"))
        # idempotent: a second run sends nothing to the API
        fake.requests.clear()
        self.assertEqual(ReindexService(repo, embedder(fake)).run().updated, 0)
        self.assertEqual(fake.requests, [])

    def test_downgrade_restores_bge_vectors_exactly_and_upgrade_is_repeatable(self):
        self.seed_bge_database()
        before = self.q("select id::text, embedding::text from chunks order by id")
        self.alembic("upgrade", "head")
        self.alembic("downgrade", "0003")
        self.assertEqual(self.q("select id::text, embedding::text from chunks order by id"), before)
        with self.eng.connect() as c:
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding"}), "vector(1024)")
        self.assertEqual(self.q("select min(embedding_model), min(embedding_dim) from chunks")[0], (OLD, 1024))
        self.assertEqual(self.q("select count(*) from information_schema.columns where table_name='chunks' "
                                "and column_name like 'embedding_prev%'")[0][0], 0)
        self.alembic("upgrade", "head")

    def test_configured_dimension_1024_is_honoured(self):
        """EMBEDDING_DIMENSIONS=1024 end to end. Runs in a subprocess because the ORM model reads the dimension at import
        time, exactly as a deployment configured with 1024 would."""
        self.seed_bge_database()
        self.alembic("upgrade", "head", dims="1024")
        with self.eng.connect() as c:
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding"}), "vector(1024)")
        # old BGE rows are still 'pending': same dimension does NOT mean same vector space
        self.assertEqual(self.q("select count(*) from chunks where embedding is null and embedding_model is null")[0][0], 12)
        code = (
            "from app.core.config import get_settings\n"
            "from tests.fixtures.openrouter_fakes import FakeOpenAI, embedder\n"
            "from app.db.session import get_session_factory\n"
            "from app.db.repositories.chunk_repo import SqlChunkRepo\n"
            "from app.services.reindex_service import ReindexService\n"
            "st = get_settings(); assert st.embedding_dim == 1024\n"
            "fake = FakeOpenAI(dim=1024); repo = SqlChunkRepo(get_session_factory(), st.embedding_model, st.embedding_dim)\n"
            "ReindexService(repo, embedder(fake, embedding_dim=1024)).run()\n"
            "assert repo.verify().ok\n"
            "assert all(b['dimensions'] == 1024 for b in fake.bodies('/embeddings'))\n"
            "print('OK1024')\n")
        env = {**os.environ, "DATABASE_URL": URL, "EMBEDDING_DIMENSIONS": "1024"}
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertIn("OK1024", r.stdout, r.stderr[-1500:])

    def test_empty_database_migrates_cleanly_without_prev_column(self):
        self.reset()
        self.alembic("upgrade", "head")
        with self.eng.connect() as c:
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding"}), "vector(1024)")
        self.assertEqual(self.q("select count(*) from information_schema.columns where table_name='chunks' "
                                "and column_name='embedding_prev'")[0][0], 0)

    def test_out_of_range_dimension_is_refused_before_touching_data(self):
        self.seed_bge_database()
        env = {**os.environ, "DATABASE_URL": URL, "EMBEDDING_DIMENSIONS": "3072"}
        r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        with self.eng.connect() as c:  # still at 0003, vectors untouched
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding"}), "vector(1024)")
        self.assertEqual(self.q("select count(*) from chunks where embedding is not null")[0][0], 12)


@unittest.skipUnless(ENABLED, "needs TEST_DATABASE_URL (database name must contain 'test')")
class Migration0005FromOpenAITests(Migration0005Tests):
    """A database as deployed with OpenAI text-embedding-3-small (schema at 0004, 1536-dim vectors). Migration 0005
    with EMBEDDING_DIMENSIONS=1024 re-sizes the column, preserves the old vectors in embedding_prev_or (with their
    TRUE model metadata) and marks everything pending."""

    def seed_openai_database(self):
        self.reset()
        self.alembic("upgrade", "0004", dims="1536")
        rnd = random.Random(11)
        with self.eng.begin() as c:
            for i in (1, 2, 3):
                c.execute(sa.text("INSERT INTO papers(id,title,authors,doi,status,created_at,updated_at,content_hash,publication_year) "
                                  "VALUES (:id,:t,'[\"A. Author\"]',:doi,'indexed',now(),now(),:h,2020+:i)"),
                          {"id": P(i), "t": f"OpenAI-era paper {i}", "doi": f"10.0/oa{i}", "h": f"h{i}", "i": i})
            n = 0
            for i in (1, 2, 3):
                for j in range(4):
                    n += 1
                    v = "[" + ",".join(f"{rnd.uniform(-1, 1):.6f}" for _ in range(1536)) + "]"
                    c.execute(sa.text("INSERT INTO chunks(id,paper_id,text,page_number,section,chunk_index,embedding,embedding_model,"
                                      "embedding_dim,created_at) VALUES (:id,:p,:t,:pg,:s,:ci,CAST(:v AS vector),:m,1536,now())"),
                              {"id": C(n), "p": P(i), "t": f"chunk {n} text about topic{n}", "pg": None if n % 5 == 0 else n,
                               "s": "Results" if n % 2 else None, "ci": j, "v": v, "m": LEGACY_OPENAI})

    def test_upgrade_re_sizes_column_and_preserves_vectors_and_metadata(self):
        self.seed_openai_database()
        meta = "select id::text, paper_id::text, text, page_number, section, chunk_index from chunks order by id"
        old_vecs = "select id::text, embedding::text from chunks order by id"
        before_meta, before_vecs = self.q(meta), self.q(old_vecs)
        self.alembic("upgrade", "head")
        self.assertEqual(self.q(meta), before_meta)  # ids, texts, pages, sections, paper links: identical
        self.assertEqual(self.q(f"select id::text, embedding_prev_or::text from chunks order by id"), before_vecs)
        self.assertEqual(self.q("select distinct embedding_prev_or_model from chunks")[0][0], LEGACY_OPENAI)
        with self.eng.connect() as c:
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding"}), "vector(1024)")
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding_prev_or"}), "vector(1536)")
        self.assertEqual(self.q("select count(*) from chunks where embedding is null and embedding_model is null "
                                "and embedding_dim is null")[0][0], 12)  # all pending re-index
        self.assertEqual(self.q("select count(*) from papers")[0][0], 3)
        sf = sa.orm.sessionmaker(self.eng)
        self.assertEqual(PgVectorStore(sf, NEW, 1024).search(hash_vec("topic1", 1024), 5, SearchFilters()), [])

    def test_reindex_after_migration_replaces_vectors_and_keeps_prev_or(self):
        self.seed_openai_database()
        self.alembic("upgrade", "head")
        sf = sa.orm.sessionmaker(self.eng, expire_on_commit=False)
        repo, fake = SqlChunkRepo(sf, NEW, 1024), FakeOpenAI()
        self.assertEqual((repo.count_pending(), repo.count_total()), (12, 12))
        rep = ReindexService(repo, embedder(fake), batch_size=6).run()
        self.assertEqual((rep.updated, rep.failed_ids), (12, []))
        self.assertTrue(repo.verify().ok)
        self.assertEqual(self.q("select count(*) from chunks where embedding_prev_or is not null")[0][0], 12)
        hits = PgVectorStore(sf, NEW, 1024).search(hash_vec("chunk 7 text about topic7", 1024), 3, SearchFilters())
        self.assertEqual(hits[0].chunk_id, C(7))  # retrieval works on the re-indexed vectors

    def test_downgrade_restores_openai_vectors_with_correct_metadata(self):
        self.seed_openai_database()
        before = self.q("select id::text, embedding::text, embedding_model from chunks order by id")
        self.alembic("upgrade", "head")
        self.alembic("downgrade", "0004")
        old_vecs = self.q("select id::text, embedding::text from chunks order by id")
        self.assertEqual(old_vecs, [(i, v) for i, v, _ in before])  # byte-for-byte back, model metadata correct
        self.assertEqual(self.q("select distinct embedding_model from chunks")[0][0], LEGACY_OPENAI)
        self.assertEqual(self.q("select min(embedding_dim) from chunks")[0][0], 1536)
        with self.eng.connect() as c:
            self.assertEqual(c.scalar(sa.text(TYPE_Q), {"c": "embedding"}), "vector(1536)")
            self.assertEqual(c.scalar("select count(*) from information_schema.columns where table_name='chunks' "
                                      "and column_name='embedding_prev_or_model'"), 0)


@unittest.skipUnless(ENABLED, "needs TEST_DATABASE_URL (database name must contain 'test')")
class RepoSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "DATABASE_URL": URL, "EMBEDDING_DIMENSIONS": "1024"}
        cls.eng = sa.create_engine(URL)
        with cls.eng.begin() as c:
            c.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.sf = sa.orm.sessionmaker(cls.eng, expire_on_commit=False)

    def test_incompatible_vector_dimension_is_rejected_before_reaching_the_database(self):
        repo = SqlChunkRepo(self.sf, NEW, 1024)
        with self.assertRaises(EmbeddingDimensionMismatch):
            repo.save([(C(1), [0.1] * 1536)])

    def test_only_one_reindex_can_run_at_a_time(self):
        with reindex_lock(self.sf):
            with self.assertRaises(ReindexAlreadyRunning):
                with reindex_lock(self.sf):
                    pass
        with reindex_lock(self.sf):  # released afterwards
            pass


if __name__ == "__main__":
    unittest.main()
