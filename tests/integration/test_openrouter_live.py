"""LIVE smoke test against the REAL OpenRouter API. SKIPPED unless BOTH are set:
    RUN_OPENROUTER_LIVE_TESTS=1   and   OPENROUTER_API_KEY=<your key>
Uses the FREE models from your environment/.env (defaults: nvidia/nemotron-3.5-super-vl:free and
nvidia/nemotron-3.5-embed-1b:free @ EMBEDDING_DIMENSIONS). Their published pricing is 0, but free tiers have rate
limits, so the test stays tiny. Run:
    RUN_OPENROUTER_LIVE_TESTS=1 python -m pytest tests/integration/test_openrouter_live.py -v
Passing proves: key accepted, both models reachable, request parameters valid, vectors have the configured size,
JSON mode works with the RAG prompt shape. It does not prove retrieval quality."""
import asyncio
import json
import math
import os
import unittest

ENABLED = os.getenv("RUN_OPENROUTER_LIVE_TESTS") == "1" and bool(os.getenv("OPENROUTER_API_KEY"))


@unittest.skipUnless(ENABLED, "set RUN_OPENROUTER_LIVE_TESTS=1 and OPENROUTER_API_KEY to run the live OpenRouter smoke test")
class LiveOpenRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.core.config import Settings
        from app.services.providers.openrouter_chat import OpenRouterChatModel
        from app.services.providers.openrouter_embeddings import OpenRouterEmbedder
        cls.s = Settings()
        cls.chat, cls.emb = OpenRouterChatModel.from_settings(cls.s), OpenRouterEmbedder.from_settings(cls.s)

    def test_embeddings_live(self):
        v = self.emb.embed_query_sync("diabetic retinopathy detection with convolutional networks")
        self.assertEqual(len(v), self.s.embedding_dim)
        self.assertTrue(all(math.isfinite(x) for x in v))
        a, b = self.emb.embed_documents_sync(["retinal fundus images", "protein structure prediction"])
        self.assertEqual((len(a), len(b)), (self.s.embedding_dim,) * 2)
        print("live embedding norm:", math.sqrt(sum(x * x for x in v)))  # informational (expected ~1.0)

    def test_chat_json_mode_live(self):
        msgs = [{"role": "system", "content": 'Reply with a single JSON object: {"answer": "<text>", "insufficient_evidence": false}'},
                {"role": "user", "content": "Say hello in one short sentence."}]
        out = asyncio.run(self.chat.chat(msgs))
        self.assertIsInstance(json.loads(out)["answer"], str)

    def test_provider_check_live(self):
        from app.services.provider_check import ProviderCheck
        res = asyncio.run(ProviderCheck(self.chat, self.emb, min_interval_seconds=0).run(deep=False))
        self.assertTrue(res["ok"], res)


if __name__ == "__main__":
    unittest.main()
