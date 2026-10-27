"""INTEGRATION tests: need a REAL PostgreSQL with the pgvector extension. SKIPPED unless TEST_DATABASE_URL is set.

    $env:TEST_DATABASE_URL = "postgresql+psycopg://research:research@localhost:5432/research_test"

DESTRUCTIVE to that database (drops/creates tables). The URL's database name must contain "test".
They use synthetic text and a MOCKED OpenRouter server; they never call the real OpenRouter API.
"""
import os
import subprocess
import sys
import unittest

URL = os.getenv("TEST_DATABASE_URL", "")
try:
    import sqlalchemy as sa
    from sqlalchemy.exc import DBAPIError, StatementError
    from app.db.models import Base, Chunk, Paper
    from app.db.repositories.chunk_repo import SqlChunkRepo
    from app.db.repositories.pgvector_store import PgVectorStore
    HAVE = True
except Exception:
    HAVE = False

from app.core.config import get_settings
from app.services.reindex_service import ReindexService
from app.services.types import SearchFilters
from tests.fixtures.openrouter_fakes import FakeOpenAI, embedder

CONFIGURED_MODEL = "nvidia/nemotron-3.5-embed-1b:free"  # name kept short: "the configured model"; the legacy BGE name is LEGACY below
LEGACY = "BAAI/bge-large-en-v1.5"
DIMS = get_settings().embedding_dim  # 1024 unless EMBEDDING_DIMENSIONS is set when the tests run
ENABLED = bool(URL) and "test" in URL.rsplit("/", 1)[-1] and HAVE


def vec(i, dim=DIMS):
    v = [0.0] * dim
    v[i] = 1.0
    return v


@unittest.skipUnless(ENABLED, "set TEST_DATABASE_URL (database name must contain 'test') and install requirements")
class PgVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = sa.create_engine(URL)
        with cls.engine.begin() as c:
            c.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.drop_all(cls.engine)
        Base.metadata.create_all(cls.engine)
        cls.sf = sa.orm.sessionmaker(cls.engine, expire_on_commit=False)

    def setUp(self):
        with self.sf() as s:
            s.execute(sa.text("TRUNCATE chunks, ingestion_jobs, papers CASCADE"))
            self.paper = Paper(title="Synthetic paper", authors=[], status="indexed", content_hash="h")
            s.add(self.paper)
            s.commit()

    def add(self, text, v, model=CONFIGURED_MODEL, page=1, idx=[0]):
        idx[0] += 1
        with self.sf() as s:
            s.add(Chunk(paper_id=self.paper.id, text=text, page_number=page, chunk_index=idx[0], embedding=v,
                        embedding_model=model if v is not None else None,
                        embedding_dim=len(v) if v is not None else None))
            s.commit()

    def test_schema_accepts_configured_dimension_and_rejects_other_dims(self):
        self.add("ok", vec(0))
        with self.sf() as s:
            self.assertEqual(len(s.scalar(sa.select(Chunk.embedding))), DIMS)
        # pgvector-python >= 0.4 rejects a wrong-sized vector client-side (StatementError) before it reaches the
        # database; older versions let the server reject it (DBAPIError). Either way it is never stored.
        for wrong in (768, 1024 if DIMS != 1024 else 2048):
            with self.assertRaises((DBAPIError, StatementError)):
                self.add("bad", vec(0, wrong))
        with self.engine.connect() as c:
            typ = c.scalar(sa.text("select format_type(atttypid, atttypmod) from pg_attribute "
                                   "where attrelid='chunks'::regclass and attname='embedding'"))
        self.assertEqual(typ, f"vector({DIMS})")

    def test_search_cosine_order_score_and_model_filter(self):
        self.add("near", vec(0))
        self.add("far", vec(5))
        self.add("other-model", vec(0), model=LEGACY)  # identical vector, wrong model -> excluded
        self.add("pending", None)                                   # no embedding -> excluded
        hits = PgVectorStore(self.sf, CONFIGURED_MODEL).search(vec(0), 10, SearchFilters())
        self.assertEqual([h.text for h in hits], ["near", "far"])
        self.assertAlmostEqual(hits[0].score, 1.0, places=5)
        self.assertAlmostEqual(hits[1].score, 0.0, places=5)
        self.assertEqual(PgVectorStore(self.sf, CONFIGURED_MODEL).count_indexed_chunks(), 2)

    def test_reindex_updates_without_duplicates_and_resumes(self):
        for i in range(7):
            self.add(f"legacy chunk {i} text", None, page=i + 1)
        repo = SqlChunkRepo(self.sf, CONFIGURED_MODEL, DIMS)
        fake = FakeOpenAI(dim=DIMS)
        emb = embedder(fake, embedding_dim=DIMS)
        self.assertEqual(repo.count_pending(), 7)
        ReindexService(repo, emb, batch_size=3).run(max_batches=1)
        self.assertEqual(repo.count_pending(), 4)
        rep = ReindexService(repo, emb, batch_size=3).run()
        self.assertEqual((rep.updated, repo.count_pending(), repo.count_total()), (4, 0, 7))
        with self.sf() as s:  # metadata preserved, model recorded
            rows = s.execute(sa.select(Chunk.page_number, Chunk.embedding_model, Chunk.embedding_dim)).all()
        self.assertEqual({r[1:] for r in rows}, {(CONFIGURED_MODEL, DIMS)})
        self.assertEqual(sorted(r[0] for r in rows), list(range(1, 8)))


@unittest.skipUnless(ENABLED, "needs TEST_DATABASE_URL")
class MigrationTests(unittest.TestCase):
    """Runs real Alembic against the test DB (drops everything first)."""

    def alembic(self, *args):
        env = {**os.environ, "DATABASE_URL": URL}
        r = subprocess.run([sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def reset(self):
        eng = sa.create_engine(URL)
        with eng.begin() as c:
            c.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        return eng

    def test_fresh_install_reaches_configured_dimension(self):
        eng = self.reset()
        self.alembic("upgrade", "head")
        with eng.connect() as c:
            typ = c.scalar(sa.text("select format_type(atttypid, atttypmod) from pg_attribute "
                                   "where attrelid='chunks'::regclass and attname='embedding'"))
            legacy = c.scalar(sa.text("select 1 from information_schema.columns where column_name='embedding_legacy'"))
        self.assertEqual(typ, f"vector({DIMS})")
        self.assertIsNone(legacy)

    def test_existing_768_data_is_preserved_and_marked_pending(self):
        eng = self.reset()
        self.alembic("upgrade", "0002")
        with eng.begin() as c:
            c.execute(sa.text("INSERT INTO papers(id,title,authors,status,created_at,updated_at) VALUES "
                              "('00000000-0000-0000-0000-000000000001','Synthetic','[]','indexed',now(),now())"))
            c.execute(sa.text("INSERT INTO chunks(id,paper_id,text,page_number,chunk_index,embedding,embedding_model,"
                              "embedding_dim,created_at) VALUES ('00000000-0000-0000-0000-0000000000a1',"
                              "'00000000-0000-0000-0000-000000000001','kept text',7,0,"
                              f"'[{','.join(['0.1'] * 768)}]','nomic-embed-text',768,now())"))
        self.alembic("upgrade", "head")
        with eng.connect() as c:
            row = c.execute(sa.text("select text, page_number, embedding is null, embedding_model, embedding_dim, "
                                    "embedding_legacy is not null, embedding_legacy_model from chunks")).one()
        self.assertEqual(tuple(row), ("kept text", 7, True, None, None, True, "nomic-embed-text"))
        self.alembic("downgrade", "0002")  # reversible through 0004 and 0003
        with eng.connect() as c:
            self.assertEqual(c.scalar(sa.text("select embedding_model from chunks")), "nomic-embed-text")


if __name__ == "__main__":
    unittest.main()
