"""Re-index logic with an in-memory repo (SYNTHETIC) and the OpenRouter embedder over a MOCKED server.
Real SQL is covered only by tests/integration."""
import unittest

from app.core.errors import EmbeddingDimensionMismatch, EmbeddingQuotaExceeded, EmbeddingRateLimited, EmbeddingUnavailable
from app.services.reindex_service import ReindexService
from tests.fixtures.fakes import FakeChunkRepo
from tests.fixtures.openrouter_fakes import FakeOpenAI, embedder, error_body

TEXTS = {f"id{i:02d}": f"chunk text number {i} about topic{i}" for i in range(10)}


def svc(repo, fake, batch=4, sleeps=None, **kw):
    return ReindexService(repo, embedder(fake, **kw), batch_size=batch, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def embedded_count(fake):
    return sum(len(b["input"]) for b in fake.bodies("/embeddings"))


class ReindexTests(unittest.TestCase):
    def test_updates_all_pending_without_duplicating(self):
        repo, fake = FakeChunkRepo(dict(TEXTS)), FakeOpenAI()
        rep = svc(repo, fake).run()
        self.assertEqual((rep.pending_at_start, rep.updated, rep.failed_ids, rep.batches), (10, 10, [], 3))
        self.assertEqual(len(repo.rows), 10)
        self.assertEqual(repo.count_pending(), 0)
        self.assertTrue(all(r["model"] == "nvidia/nemotron-3.5-embed-1b:free" and len(r["vec"]) == 1024
                            for r in repo.rows.values()))

    def test_resume_after_interruption_only_sends_remaining_chunks(self):
        repo, fake = FakeChunkRepo(dict(TEXTS)), FakeOpenAI()
        svc(repo, fake).run(max_batches=1)
        self.assertEqual(repo.count_pending(), 6)
        fake.requests.clear()
        rep = svc(repo, fake).run()
        self.assertEqual((rep.pending_at_start, rep.updated, embedded_count(fake)), (6, 6, 6))  # no money spent twice
        self.assertEqual(repo.count_pending(), 0)

    def test_chunks_already_on_current_model_are_not_sent_to_the_api(self):
        repo, fake = FakeChunkRepo(dict(TEXTS), done={"id00", "id01"}), FakeOpenAI()
        rep = svc(repo, fake).run()
        self.assertEqual((rep.updated, embedded_count(fake)), (8, 8))
        self.assertEqual(repo.rows["id00"]["vec"], [0.0] * 1024)  # 1024-d placeholder vector, untouched

    def test_unembeddable_chunk_stays_pending_others_succeed(self):
        texts = dict(TEXTS)
        texts["id03"] = "   "
        repo, fake = FakeChunkRepo(texts), FakeOpenAI()
        rep = svc(repo, fake).run()
        self.assertEqual((rep.failed_ids, rep.updated), (["id03"], 9))
        self.assertIsNone(repo.rows["id03"]["vec"])
        self.assertEqual(repo.count_pending(), 1)

    def test_oversized_chunk_isolated_without_failing_the_batch(self):
        texts = dict(TEXTS)
        texts["id05"] = "word " * 40
        repo, fake = FakeChunkRepo(texts), FakeOpenAI()
        rep = svc(repo, fake, embedding_max_input_chars=100).run()
        self.assertEqual((rep.failed_ids, rep.updated), (["id05"], 9))

    def test_quota_or_auth_failure_stops_immediately_without_retry_storm(self):
        repo, fake = FakeChunkRepo(dict(TEXTS)), FakeOpenAI()
        svc(repo, fake).run(max_batches=1)
        fake.requests.clear()
        fake.script.append((429, error_body("quota", "insufficient_quota", "insufficient_quota"), {}))
        sleeps = []
        with self.assertRaises(EmbeddingQuotaExceeded):
            svc(repo, fake, sleeps=sleeps).run()
        self.assertEqual((fake.count("/embeddings"), sleeps), (1, []))  # one request, no waiting, no per-chunk fallback
        self.assertEqual(repo.count_pending(), 6)  # completed batch kept, nothing falsely marked indexed
        fake.script.clear()
        svc(repo, fake).run()  # resume works
        self.assertEqual(repo.count_pending(), 0)

    def test_rate_limit_is_waited_out_with_bounded_backoff(self):
        repo, fake = FakeChunkRepo(dict(TEXTS)), FakeOpenAI()
        limited = (429, error_body("slow", "rate_limit_exceeded"), {})
        fake.script += [limited, limited]
        sleeps = []
        rep = svc(repo, fake, sleeps=sleeps).run()
        self.assertEqual((rep.updated, sleeps), (10, [5.0, 15.0]))

    def test_persistent_rate_limit_gives_up_after_bounded_retries(self):
        repo, fake = FakeChunkRepo(dict(TEXTS)), FakeOpenAI()
        fake.script += [(429, error_body("slow", "rate_limit_exceeded"), {})] * 20
        sleeps = []
        with self.assertRaises(EmbeddingRateLimited):
            svc(repo, fake, sleeps=sleeps).run()
        self.assertEqual(sleeps, [5.0, 15.0, 45.0])
        self.assertEqual(fake.count("/embeddings"), 4)  # 1 + 3 retries
        self.assertEqual(repo.count_pending(), 10)

    def test_wrong_dimension_is_a_config_error_not_a_per_chunk_failure(self):
        repo, fake = FakeChunkRepo(dict(TEXTS)), FakeOpenAI()
        fake.mode = "short"
        with self.assertRaises(EmbeddingDimensionMismatch):
            svc(repo, fake).run()
        self.assertEqual(fake.count("/embeddings"), 1)  # did not pay for 4 more single-chunk requests
        self.assertEqual(repo.count_pending(), 10)

    def test_provider_outage_aborts_but_keeps_completed_batches(self):
        repo, fake = FakeChunkRepo(dict(TEXTS)), FakeOpenAI()
        svc(repo, fake).run(max_batches=1)
        fake.script.append((500, error_body("down"), {}))
        with self.assertRaises(EmbeddingUnavailable):
            svc(repo, fake).run()
        self.assertEqual(repo.count_pending(), 6)

    def test_zero_vector_batch_is_not_marked_indexed(self):
        repo, fake = FakeChunkRepo({"a": "alpha text one", "b": "beta text two"}), FakeOpenAI()
        fake.mode = "zero"
        rep = svc(repo, fake).run()
        self.assertEqual((sorted(rep.failed_ids), repo.count_pending()), (["a", "b"], 2))

    def test_empty_corpus(self):
        rep = svc(FakeChunkRepo({}), FakeOpenAI()).run()
        self.assertEqual((rep.pending_at_start, rep.updated, rep.batches), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
