"""C. Embedding service tests: OpenRouterEmbedder through the REAL openai SDK (OpenRouter base URL) and a MOCKED
OpenRouter server."""
import asyncio
import logging
import math
import unittest

import httpx

from app.core import errors as E
from tests.fixtures.openrouter_fakes import EMBED_MODEL, FAKE_KEY, FakeOpenAI, embedder, error_body, hash_vec

run = asyncio.run


class RequestTests(unittest.TestCase):
    def test_model_dimensions_and_auth_come_from_configuration(self):
        fake = FakeOpenAI()
        e = embedder(fake)
        e.embed_documents_sync(["alpha beta"])
        body = fake.bodies("/embeddings")[0]
        self.assertEqual((body["model"], body["dimensions"], body["encoding_format"]), (EMBED_MODEL, 1024, "float"))
        self.assertEqual(fake.requests[0].headers["authorization"], f"Bearer {FAKE_KEY}")

        fake = FakeOpenAI(dim=2048)
        embedder(fake, embedding_dim=2048).embed_documents_sync(["alpha"])  # the model's default/full size
        self.assertEqual(fake.bodies("/embeddings")[0]["dimensions"], 2048)

    def test_models_without_documented_dimension_support_do_not_send_dimensions(self):
        # Unknown model ids are never ASSUMED to honour `dimensions`: their natural size is configured and every
        # response is validated at runtime instead (never truncated or padded silently).
        fake = FakeOpenAI(dim=2048)
        embedder(fake, embedding_model="some/unlisted-embed-model", embedding_dim=2048).embed_documents_sync(["x y"])
        self.assertNotIn("dimensions", fake.bodies("/embeddings")[0])

    def test_documents_and_queries_use_same_model_dimensions_and_untouched_text(self):
        fake = FakeOpenAI()
        e = embedder(fake)
        e.embed_documents_sync(["what is retinopathy"])
        run(e.embed_documents(["what is retinopathy"]))
        e.embed_query_sync("what is retinopathy")
        run(e.embed_query("what is retinopathy"))
        bodies = fake.bodies("/embeddings")
        self.assertEqual(len(bodies), 4)
        for b in bodies:  # no BGE-style instruction/prefix: input is exactly the text, model/dimensions identical
            self.assertEqual((b["model"], b["dimensions"], b["input"]), (EMBED_MODEL, 1024, ["what is retinopathy"]))
        # and the resulting vectors are identical, so queries land in the same space as stored chunks
        self.assertEqual(e.embed_query_sync("what is retinopathy"), e.embed_documents_sync(["what is retinopathy"])[0])


class OrderAndBatchTests(unittest.TestCase):
    def test_order_preserved_across_bounded_batches_even_if_server_shuffles(self):
        fake = FakeOpenAI()
        fake.mode = "shuffle"
        e = embedder(fake, embedding_batch_size=3)
        texts = [f"distinct text number{i} word{i}" for i in range(10)]
        out = e.embed_documents_sync(texts)
        self.assertEqual(out, [hash_vec(t, 1024) for t in texts])
        self.assertEqual([len(b["input"]) for b in fake.bodies("/embeddings")], [3, 3, 3, 1])
        self.assertEqual(run(e.embed_documents(texts)), out)  # async path identical

    def test_empty_list_makes_no_request(self):
        fake = FakeOpenAI()
        e = embedder(fake)
        self.assertEqual(e.embed_documents_sync([]), [])
        self.assertEqual(run(e.embed_documents([])), [])
        self.assertEqual(fake.requests, [])


class InputValidationTests(unittest.TestCase):
    def test_invalid_inputs_rejected_before_any_network_call(self):
        fake = FakeOpenAI()
        e = embedder(fake, embedding_max_input_chars=100)
        for bad in (["ok", ""], ["  \n"], [None], [123], ["x" * 101]):
            with self.assertRaises(E.InvalidEmbeddingInput, msg=str(bad)[:20]):
                e.embed_documents_sync(bad)
        for bad in ("", "   ", None, 5):
            with self.assertRaises(E.InvalidEmbeddingInput):
                e.embed_query_sync(bad)
            with self.assertRaises(E.InvalidEmbeddingInput):
                run(e.embed_query(bad))
        with self.assertRaises(E.InvalidEmbeddingInput):
            e.embed_documents_sync("not a list")
        self.assertEqual(fake.requests, [])


class ResponseValidationTests(unittest.TestCase):
    def test_bad_vectors_rejected(self):
        for mode, exc in (("short", E.EmbeddingDimensionMismatch), ("drop", E.InvalidEmbedding), ("zero", E.InvalidEmbedding)):
            fake = FakeOpenAI()
            fake.mode = mode
            with self.assertRaises(exc, msg=mode):
                embedder(fake).embed_documents_sync(["a b", "c d"])
            with self.assertRaises(exc, msg=mode):
                run(embedder(fake).embed_documents(["a b", "c d"]))

    def test_vectors_have_configured_size_and_are_finite_floats(self):
        v = embedder(FakeOpenAI(dim=2048), embedding_dim=2048).embed_query_sync("hello world")
        self.assertEqual(len(v), 2048)
        self.assertTrue(all(isinstance(x, float) and math.isfinite(x) for x in v))


class ErrorTests(unittest.TestCase):
    def expect(self, status, body, exc):
        fake = FakeOpenAI()
        fake.script.append((status, body, {}))
        with self.assertRaises(exc):
            embedder(fake).embed_documents_sync(["a b"])
        fake.script.append((status, body, {}))
        with self.assertRaises(exc):
            run(embedder(fake).embed_documents(["a b"]))

    def test_provider_errors_map_to_safe_errors(self):
        self.expect(401, error_body("bad key", "invalid_api_key", "authentication_error"), E.EmbeddingAuthFailed)
        self.expect(429, error_body("quota", "insufficient_quota", "insufficient_quota"), E.EmbeddingQuotaExceeded)
        self.expect(429, error_body("slow", "rate_limit_exceeded", "requests"), E.EmbeddingRateLimited)
        self.expect(404, error_body("no such model", "model_not_found"), E.EmbeddingModelNotAvailable)
        self.expect(500, error_body("oops", None, "server_error"), E.EmbeddingUnavailable)
        self.expect(400, error_body("input too long", "invalid_request_error"), E.InvalidEmbeddingInput)

    def test_all_provider_failures_that_stop_a_reindex_are_EmbeddingUnavailable(self):
        for cls in (E.EmbeddingAuthFailed, E.EmbeddingQuotaExceeded, E.EmbeddingRateLimited, E.EmbeddingTimeout,
                    E.EmbeddingModelNotAvailable):
            self.assertTrue(issubclass(cls, E.EmbeddingUnavailable))

    def test_network_failures(self):
        for exc, expected in ((httpx.ConnectError("refused"), E.EmbeddingUnavailable), (httpx.ReadTimeout("slow"), E.EmbeddingTimeout)):
            fake = FakeOpenAI()
            fake.raise_exc = exc
            with self.assertRaises(expected):
                embedder(fake).embed_documents_sync(["a b"])

    def test_retries_are_bounded(self):
        fake = FakeOpenAI()
        fake.script += [(429, error_body("slow", "rate_limit_exceeded"), {"retry-after-ms": "1"})] * 10
        with self.assertRaises(E.EmbeddingRateLimited):
            embedder(fake, openrouter_max_retries=2).embed_documents_sync(["a b"])
        self.assertEqual(fake.count("/embeddings"), 3)

    def test_no_secret_and_no_paper_text_in_errors_or_logs(self):
        fake = FakeOpenAI()
        fake.script.append((401, error_body(f"Incorrect API key provided: {FAKE_KEY}", "invalid_api_key"), {}))
        with self.assertLogs("app.services.providers.openrouter_common", level=logging.WARNING) as logs:
            with self.assertRaises(E.EmbeddingAuthFailed) as ctx:
                embedder(fake).embed_documents_sync(["confidential unpublished paper sentence"])
        text = str(ctx.exception) + "".join(logs.output)
        self.assertNotIn("FAKE-TEST-KEY", text)
        self.assertNotIn("confidential", text)


if __name__ == "__main__":
    unittest.main()
