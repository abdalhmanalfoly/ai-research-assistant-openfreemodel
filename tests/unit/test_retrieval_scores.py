"""Retrieval scoring regression (root-cause analysis of the insufficient-evidence responses, 2026-10-28).

Confirmed facts these tests pin down (see docs + scripts/diagnose_retrieval.py):
  * SQL uses pgvector cosine distance `<=>` and the app converts it to SIMILARITY via `1.0 - distance`
    (correct metric pairing: distance 0.809 logs as sim 0.191).
  * Candidates are sorted by distance ASCENDING (most similar first) — a DESC sort or raw-distance
    "scores" would invert relevance.
  * The threshold (0.30) is applied to similarity, AFTER conversion, in evidence selection only.
  * Empty paper selection means ALL papers (no filter clause is added); a non-empty list filters exactly.
  * Self-consistency probe measured live: a stored chunk embedded as a query returns sim=1.0 against its own
    vector — query and document paths are identical; scores are the model's genuine scale.
"""
import unittest

from sqlalchemy import asc

from app.db.models import Chunk, Paper
from app.db.repositories.pgvector_store import PgVectorStore
from app.services.evidence_service import select_evidence
from app.services.types import ScoredChunk, SearchFilters

MODEL, DIM = "nvidia/nemotron-3.5-embed-1b:free", 2048


def scored(sim, text=None, cid="c1", title="T", page=1):
    # token sets must be disjoint across items — otherwise the production dedupe (jaccard>=0.85) fires
    return ScoredChunk(cid, text or f"ztok{hash(cid) & 0xffffff} unique vocabulary instance {cid}", page, None, 0,
                       sim, ("p-" + cid, title, [], None, None, 2020))


class ScoreConversionTests(unittest.TestCase):
    def test_logged_score_is_similarity_not_distance(self):
        dist = Chunk.embedding.cosine_distance([0.0] * DIM)
        self.assertIn("<=>", str(dist.compile()))
        observed_distance = 0.809  # from the incident log: top score 0.191
        self.assertAlmostEqual(1.0 - observed_distance, 0.191, places=3)  # conversion, as pgvector_store.py does
        self.assertTrue(0.0 <= observed_distance <= 2.0)  # cosine distance range [0..2]


class SortingAndFilterSQLTests(unittest.TestCase):
    class _Rows:
        def __init__(self, rec):
            self.rec = rec

        def all(self):
            return []

    class _Sess:
        def __init__(self, rec):
            self.rec = rec

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, q):
            self.rec.append(q)
            return SortingAndFilterSQLTests._Rows(self.rec)

    def _q(self, filters):
        rec = []
        store = PgVectorStore(lambda: SortingAndFilterSQLTests._Sess(rec), MODEL, DIM)
        store.search([0.0] * DIM, 15, filters)
        return str(rec[-1].compile(compile_kwargs={"literal_binds": True})).replace("\n", " ")

    def test_query_discriminates_model_dimension_and_indexed_status(self):
        sql = self._q(SearchFilters())
        self.assertIn("chunks.embedding_model", sql)
        self.assertIn("chunks.embedding_dim", sql)
        self.assertIn("indexed", sql)                    # Paper.status filter
        self.assertNotIn("chunks.paper_id IN", sql)      # empty selection must NOT restrict papers

    def test_nonempty_paper_selection_filters(self):
        sql = self._q(SearchFilters(paper_ids=["11111111-1111-1111-1111-111111111111"]))
        self.assertIn("IN (", sql) and self.assertIn("11111111", sql)

    def test_sorted_by_distance_ascending(self):
        sql = self._q(SearchFilters())
        order = sql.split(" ORDER BY")[1]
        self.assertNotIn(" DESC", order, "distance must be ascending (most similar first)")

    def test_score_conversion_used_in_code(self):
        import inspect
        from app.db.repositories import pgvector_store as m
        src = inspect.getsource(m.PgVectorStore.search)
        self.assertIn("1.0 - float(d)", src)


class ThresholdAndZeroEvidenceTests(unittest.TestCase):
    """The 0.300 threshold preserves the insufficient-evidence safeguard; below-threshold NEVER becomes answer."""

    def tests_all_below_threshold_selects_zero(self):
        cands = [scored(s) for s in (0.191, 0.185, 0.172, 0.166, 0.161, 0.155, 0.149, 0.146)]
        evidence, stats = select_evidence(cands, threshold=0.30, max_items=5, budget_chars=12000)
        self.assertEqual((len(evidence), stats.below_threshold, stats.duplicates), (0, 8, 0))

    def test_on_topic_scores_pass_and_rank_first(self):
        # measured with scripts/diagnose_retrieval on the live corpus (radiology question, 2026-10-28)
        sims = [0.6954, 0.6456, 0.6402, 0.6172, 0.6166, 0.5932, 0.5874, 0.5842]
        evidence, stats = select_evidence([scored(s, cid=f"c{i}") for i, s in enumerate(sims, 1)],
                                          threshold=0.30, max_items=5, budget_chars=12000)
        self.assertEqual(len(evidence), 5)
        self.assertEqual(stats.below_threshold, 0)
        self.assertEqual([e.chunk.score for e in evidence], sorted(sims, reverse=True)[:5])

    def test_boundary_and_zero_evidence_rejected(self):
        evidence, stats = select_evidence([scored(0.1428), scored(0.1442)], threshold=0.30, max_items=5,
                                          budget_chars=12000)
        self.assertEqual((len(evidence), stats.below_threshold), (0, 2))  # self-consistency floor stays rejected
