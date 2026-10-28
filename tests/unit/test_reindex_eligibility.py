"""Re-index ELIGIBILITY: a chunk must be pending when its embedding is missing OR was produced by another
model OR by the same model with a DIFFERENT dimension (the 1024->2048 migration case). The SQL repo, the
synthetic fake and the provider's response validation must all agree on this contract."""
import unittest

from app.core.errors import EmbeddingDimensionMismatch
from app.db.repositories.chunk_repo import SqlChunkRepo
from tests.fixtures.fakes import FakeChunkRepo
from tests.fixtures.openrouter_fakes import FakeOpenAI, embedder

TEXTS = {f"id{i:02d}": f"chunk text number {i}" for i in range(4)}


class EligibilityParityTests(unittest.TestCase):
    """The REAL SqlChunkRepo._pending() must contain all three conditions; the fake must mirror them."""

    def test_real_pending_sql_checks_model_and_dimension(self):
        conds = SqlChunkRepo(None, "nvidia/nemotron-3.5-embed-1b:free", 2048)._pending()
        self.assertEqual(len(conds), 3)  # no embedding / other model / other dimension
        sqls = [str(c.compile()) for c in conds]
        self.assertTrue(any("embedding_model" in s and "DISTINCT FROM" in s.upper() for s in sqls))
        self.assertTrue(any("embedding_dim" in s and "DISTINCT FROM" in s.upper() for s in sqls))

    def test_fake_repo_matches_real_contract_across_dimensions(self):
        repo = FakeChunkRepo(TEXTS, done=set(TEXTS), embedded_dim=1024)   # nvidia @ 1024, target 1024
        self.assertEqual(repo.count_pending(), 0)
        repo2 = FakeChunkRepo(TEXTS, done=set(TEXTS), embedded_dim=2048)  # nvidia @ 2048 with target 2048
        self.assertEqual(repo2.count_pending(), 0)


class SameModelWrongDimensionTests(unittest.TestCase):
    def test_same_model_1024_vectors_are_pending_at_target_2048(self):
        """THE migration case: model name matches, dimension does not -> every chunk must be pending."""
        repo = FakeChunkRepo(TEXTS, done=set(TEXTS), embedded_dim=1024, target_dim=2048)
        self.assertEqual(len(list(repo.fetch_pending(None, 10))), 4,
                         "1024-d vectors of the SAME model must count as pending at target 2048")

    def test_missing_embedding_is_pending_even_when_metadata_matches(self):
        repo = FakeChunkRepo(TEXTS, done=set(TEXTS), embedded_dim=1024)
        self.assertEqual(repo.count_pending(), 0)
        for cid in TEXTS:
            repo.rows[cid]["vec"] = None  # metadata claims current, vector absent -> still pending
        self.assertEqual(repo.count_pending(), 4)


class MixedGenerationTests(unittest.TestCase):
    def test_retrieval_can_never_compare_across_generations(self):
        from app.core.config import Settings
        from app.db.repositories.pgvector_store import PgVectorStore
        s = Settings(_env_file=None)  # shipped configuration: nemotron @ 2048
        conds = PgVectorStore(None, s.embedding_model, s.embedding_dim)._compatible()
        self.assertTrue(any(str(c.left) == "chunks.embedding_model" and c.right.value == s.embedding_model
                            for c in conds))          # same model only
        self.assertTrue(any(str(c.left) == "chunks.embedding_dim" and c.right.value == 2048
                            for c in conds))          # same dimension only


class ProviderResponseValidationTests(unittest.TestCase):
    def test_provider_validates_actual_vector_length_not_the_request(self):
        fake = FakeOpenAI()
        fake.script = [(200, {"data": [{"index": 0, "embedding": [j * 0.1 - 1.0 for j in range(2048)],
                                        "object": "embedding"}]}, {})]
        self.assertEqual([len(v) for v in embedder(fake, embedding_dim=2048).embed_documents_sync(["alpha"])],
                         [2048])
        self.assertNotIn("dimensions", fake.bodies("/embeddings")[0])  # native-size request omits the parameter

        fake.script = [(200, {"data": [{"index": 0, "embedding": [j * 0.1 - 1.0 for j in range(1024)],
                                        "object": "embedding"}]}, {})]
        with self.assertRaises(EmbeddingDimensionMismatch):
            embedder(fake, embedding_dim=2048).embed_documents_sync(["alpha"])

    def test_reindex_never_stores_a_wrong_length_vector(self):
        from app.services.reindex_service import ReindexService
        repo = FakeChunkRepo(dict(TEXTS))
        fake = FakeOpenAI()
        fake.mode = "short"                                  # provider returns one FEWER value than expected
        with self.assertRaises(EmbeddingDimensionMismatch):  # a 2047-valued vector is never accepted as 2048-d
            ReindexService(repo, embedder(fake, embedding_dim=2048), batch_size=4).run()
        self.assertEqual(repo.saves, 0)                     # nothing written, nothing faked as 2048
        self.assertEqual(repo.count_pending(), len(TEXTS))  # everything stays honestly pending
