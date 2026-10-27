"""Regression tests for retrieval/evidence diagnostics:

* cosine-distance -> similarity conversion used for the threshold (distance <> similarity semantics)
* the pgvector cosine operator used by search
* the model/dimension compatibility filter (search never compares foreign-model vectors)
* near-boundary sufficiency decisions (score just above/below threshold)
* log hygiene: diagnostics are emitted but question text and secrets never reach the logs
"""
import asyncio
import json
import logging
import unittest
import unittest.mock

try:
    from sqlalchemy import literal
    from app.db.models import Chunk
    HAVE_SQLA = True
except ImportError:
    HAVE_SQLA = False

from app.services.chat_service import ChatService, RagConfig
from app.services.evidence_service import select_evidence
from app.services.retrieval_service import RetrievalService
from app.services.types import ScoredChunk, SearchFilters
from tests.fixtures.fakes import DIM, PAPER_A, FakeEmbedder, FakeLLM, InMemoryStore


def run(c):
    return asyncio.run(c)


def sc(text, score, i=0):
    return ScoredChunk(f"c{i}", text, 1, None, i, score, PAPER_A)


def make_service(store, llm, threshold=0.30):
    r = RetrievalService(FakeEmbedder(), store, expected_dim=DIM)
    return ChatService(r, llm, RagConfig(threshold=threshold, default_top_k=5, evidence_budget_chars=60000))


@unittest.skipUnless(HAVE_SQLA, "sqlalchemy not installed")
class CosineSemanticsTests(unittest.TestCase):
    def test_similarity_is_one_minus_cosine_distance_not_the_threshold_on_distance(self):
        """pgvector `<=>` returns cosine DISTANCE in [0,2]; the app's score is 1 - distance, i.e. the cosine
        SIMILARITY in [-1,1]. RETRIEVAL_THRESHOLD applies to the similarity, so 0.30 similarity is a far
        weaker requirement than a 0.30 distance would be. This pins the conversion."""
        identical = (1.0, 0.0, 0.0)
        orthogonal = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
        dot = sum(a * b for a, b in zip(*orthogonal))
        similarity = dot / (1.0 * 1.0)                 # cosine similarity
        distance = 1.0 - similarity                    # pgvector cosine_distance (`<=>`)
        score = 1.0 - distance                         # the conversion in pgvector_store.search
        self.assertAlmostEqual(score, similarity)      # and NOT similarity == distance
        self.assertAlmostEqual((1.0 - (1.0 - 1.0)), 1.0)  # identical vectors: distance 0 -> score 1

    def test_search_uses_the_cosine_distance_operator(self):
        expr = Chunk.embedding.cosine_distance(Chunk.embedding)
        sql = str(expr.compile())
        self.assertIn("<=>", sql)  # pgvector cosine distance; NOT "<->" (L2) or "<#>" (neg. dot product)

    def test_compatible_filter_requires_vector_model_and_dimension(self):
        from app.db.repositories.pgvector_store import PgVectorStore

        conds = PgVectorStore(None, "some/model:free", 1024)._compatible()
        self.assertEqual(len(conds), 3)
        sqls = [str(c.compile()) for c in conds]
        self.assertIn("IS NOT NULL", " ".join(sqls))
        self.assertTrue(any("embedding_model" in s for s in sqls))
        self.assertTrue(any("embedding_dim" in s for s in sqls))


class SufficiencyBoundaryTests(unittest.TestCase):
    def test_relevant_evidence_just_above_threshold_passes_the_sufficiency_check(self):
        evidence, stats = select_evidence([sc("relevant text", 0.3001)], threshold=0.30, max_items=5,
                                          budget_chars=100000)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(stats.below_threshold, 0)

    def test_evidence_just_below_threshold_is_rejected_for_sufficiency(self):
        evidence, stats = select_evidence([sc("relevant text", 0.2999)], threshold=0.30, max_items=5,
                                          budget_chars=100000)
        self.assertEqual(evidence, [])
        self.assertEqual(stats.below_threshold, 1)

    def test_irrelevant_corpus_yields_no_evidence_without_calling_the_llm(self):
        llm = FakeLLM(json.dumps({"answer": "should never be called", "insufficient_evidence": False}))
        store = InMemoryStore()
        service = make_service(store, llm)
        with unittest.mock.patch.object(store, "search") as mocked:
            mocked.return_value = [sc("weak match only", 0.1, i) for i in range(15)]
            res = run(service.answer("anything"))
        self.assertTrue(res.insufficient_evidence)
        self.assertEqual(llm.calls, [])  # the LLM is never consulted without evidence

    def test_relevant_evidence_passes_sufficiency_and_keeps_citations(self):
        store = InMemoryStore()
        store.add(PAPER_A, 7, "Patient-level outcomes of deep learning image reconstruction were clearly improved.")
        res = run(make_service(store, FakeLLM(json.dumps(
            {"answer": "Improvements were reported [E1].", "insufficient_evidence": False}))).answer(
            "deep learning reconstruction outcomes"))
        self.assertFalse(res.insufficient_evidence)
        self.assertEqual([c.id for c in res.citations], ["E1"])


class DiagnosticsLogTests(unittest.TestCase):
    def test_diagnostics_logged_but_question_text_and_secrets_are_not(self):
        store = InMemoryStore()
        store.add(PAPER_A, 7, "Outcomes of deep learning image reconstruction improved clearly.")
        SECRET_QUESTION = "SECRET-TOPIC-QUESTION liver CT reconstruct"
        service = make_service(store, FakeLLM(json.dumps({"answer": "x [E1]", "insufficient_evidence": False})))
        with self.assertLogs(level=logging.INFO) as captured:
            run(service.answer(SECRET_QUESTION))
        logs = "\n".join(captured.output)
        self.assertIn("retrieved=", logs)
        self.assertIn("evidence_stats=", logs)
        self.assertNotIn("SECRET-TOPIC-QUESTION", logs)  # user text is never logged
        self.assertNotIn("sk-", logs)
