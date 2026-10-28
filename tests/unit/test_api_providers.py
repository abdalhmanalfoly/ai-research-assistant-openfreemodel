"""Health / diagnostics / error-envelope tests with a MOCKED OpenRouter server. Proves: ordinary health checks never
call the provider, the explicit check does (and is cached), and no secret is ever serialised."""
import json
import unittest

from fastapi.testclient import TestClient

from app.api.deps import Container
from app.core import errors as E
from app.main import create_app
from app.services.chat_service import ChatService, RagConfig
from app.services.provider_check import ProviderCheck
from app.services.retrieval_service import RetrievalService
from tests.fixtures.fakes import DIM, PAPER_A, FakeEmbedder, FakeLLM, InMemoryStore
from tests.fixtures.openrouter_fakes import CHAT_MODEL, EMBED_MODEL, FAKE_KEY, FakeOpenAI, chat_model, embedder, settings


def broken_session_factory():
    raise RuntimeError("no database in unit tests")


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def client(fake=None, key=True, llm_fail=None):
    fake = fake or FakeOpenAI(chat_reply="OK")
    s = settings(openrouter_api_key=FAKE_KEY if key else None)
    llm, emb = chat_model(fake), embedder(fake)
    store = InMemoryStore()
    store.add(PAPER_A, 7, "Convolutional networks detect diabetic retinopathy.")
    chat = ChatService(RetrievalService(FakeEmbedder(), store, DIM),
                       FakeLLM(fail=llm_fail, reply=json.dumps({"answer": "x [E1]", "insufficient_evidence": False})),
                       RagConfig(0.1, 5, 6000))
    clock = Clock()
    app = create_app()
    app.state.container = Container(s, llm, chat, None, broken_session_factory, emb, None, ProviderCheck(llm, emb, clock=clock))
    return TestClient(app), fake, clock


class HealthTests(unittest.TestCase):
    def test_health_never_calls_a_provider_and_never_leaks_the_key(self):
        c, fake, _ = client()
        r = c.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(fake.requests, [])  # zero (paid or free) provider requests
        j = r.json()
        self.assertEqual((j["llm_provider"], j["llm"], j["embedding"]), ("openrouter", "configured", "configured"))
        self.assertEqual((j["chat_model_name"], j["embedding_model_name"], j["embedding_dimension"]),
                         (CHAT_MODEL, EMBED_MODEL, 2048))
        self.assertEqual(j["database"], "down")
        self.assertEqual(j["status"], "degraded")  # the API process being up is not "healthy"
        self.assertNotIn(FAKE_KEY, r.text)
        self.assertNotIn("FAKE-TEST-KEY", r.text)

    def test_health_reports_missing_key_without_claiming_ok(self):
        c, _, _ = client(key=False)
        j = c.get("/api/health").json()
        self.assertEqual((j["llm"], j["embedding"], j["status"]), ("not_configured", "not_configured", "degraded"))


class ProviderCheckTests(unittest.TestCase):
    def test_default_check_is_free_and_verifies_model_access(self):
        c, fake, _ = client()
        j = c.post("/api/health/provider-check").json()
        self.assertTrue(j["ok"])
        self.assertEqual([x["check"] for x in j["checks"]], ["chat_model_access", "embedding_model_access"])
        self.assertEqual([r.url.path.rsplit("/", 1)[-1] for r in fake.requests], ["models", "models"])  # GET /models, GET /embeddings/models
        self.assertEqual(fake.count("/embeddings") + fake.count("/chat/completions"), 0)  # nothing billable
        self.assertNotIn(FAKE_KEY, json.dumps(j))

    def test_deep_check_sends_one_tiny_request_per_endpoint(self):
        c, fake, _ = client()
        j = c.post("/api/health/provider-check?deep=true").json()
        self.assertTrue(j["ok"])
        self.assertEqual((fake.count("/embeddings"), fake.count("/chat/completions")), (1, 1))
        self.assertLessEqual(fake.bodies("/chat/completions")[0]["max_tokens"], 16)

    def test_results_are_cached_so_the_endpoint_cannot_become_a_billing_loop(self):
        c, fake, clock = client()
        c.post("/api/health/provider-check?deep=true")
        n = len(fake.requests)
        again = c.post("/api/health/provider-check?deep=true").json()
        self.assertTrue(again["cached"])
        self.assertEqual(len(fake.requests), n)
        clock.t += 31  # cooldown elapsed -> a real check again
        self.assertFalse(c.post("/api/health/provider-check?deep=true").json()["cached"])
        self.assertGreater(len(fake.requests), n)

    def test_reports_inaccessible_models_and_auth_failures_precisely(self):
        fake = FakeOpenAI()
        fake.model_catalog.discard(CHAT_MODEL)
        j = client(fake)[0].post("/api/health/provider-check").json()
        self.assertFalse(j["ok"])
        self.assertEqual({x["check"]: x.get("error_code") for x in j["checks"]},
                         {"chat_model_access": "model_not_available", "embedding_model_access": None})

        fake = FakeOpenAI()
        fake.script += [(401, {"error": {"message": f"bad key {FAKE_KEY}", "code": "invalid_api_key"}}, {})] * 2
        r = client(fake)[0].post("/api/health/provider-check")
        self.assertEqual({x["error_code"] for x in r.json()["checks"]}, {"llm_auth_failed", "embedding_auth_failed"})
        self.assertNotIn("FAKE-TEST-KEY", r.text)


class ErrorEnvelopeTests(unittest.TestCase):
    def test_provider_errors_keep_the_existing_envelope_and_leak_nothing(self):
        for exc, status, code in ((E.LLMAuthFailed(), 503, "llm_auth_failed"), (E.LLMQuotaExceeded(), 503, "llm_quota_exceeded"),
                                  (E.LLMRateLimited(), 503, "llm_rate_limited"), (E.LLMTimeout(), 504, "llm_timeout"),
                                  (E.ModelNotAvailable(), 503, "model_not_available"), (E.InvalidLLMResponse(), 502, "invalid_llm_response"),
                                  (E.EmbeddingAuthFailed(), 503, "embedding_auth_failed")):
            c, _, _ = client(llm_fail=exc)
            r = c.post("/api/chat", json={"question": "convolutional retinopathy"})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (status, code))
            self.assertEqual(set(r.json()), {"error"})
            self.assertNotIn("FAKE", r.text)


if __name__ == "__main__":
    unittest.main()
