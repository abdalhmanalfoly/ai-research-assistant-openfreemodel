"""API tests using FastAPI TestClient with the fake (synthetic) core. Skipped if fastapi/sqlalchemy/pgvector are missing."""
import json
import unittest

try:
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.api.deps import Container
    HAVE = True
except ImportError:
    HAVE = False

from app.services.chat_service import ChatService, RagConfig
from app.services.retrieval_service import RetrievalService
from tests.fixtures.fakes import DIM, PAPER_A, FakeEmbedder, FakeLLM, InMemoryStore


@unittest.skipUnless(HAVE, "fastapi stack not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        store = InMemoryStore()
        store.add(PAPER_A, 7, "Convolutional networks detect diabetic retinopathy from fundus images.")
        llm = FakeLLM(json.dumps({"answer": "They do [E1].", "insufficient_evidence": False}))
        chat = ChatService(RetrievalService(FakeEmbedder(), store, DIM), llm, RagConfig(0.1, 5, 6000))
        app = create_app()
        app.state.container = Container(None, None, chat, None, None)  # type: ignore[arg-type]
        self.client = TestClient(app)

    def test_chat_ok(self):
        r = self.client.post("/api/chat", json={"question": "convolutional retinopathy"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["citations"][0]["page_number"], 7)

    def test_frontend_is_served_and_does_not_shadow_api(self):
        r = self.client.get("/pages/chat.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        r = self.client.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("text/html", r.headers["content-type"])  # API misses must never return index.html
        self.assertEqual(self.client.post("/api/chat", json={"question": "convolutional retinopathy"}).status_code, 200)

    def test_validation(self):
        for body in ({"question": "   "}, {}, {"question": "ok", "top_k": 0}, {"question": "ok", "paper_ids": ["nope"]}):
            r = self.client.post("/api/chat", json=body)
            self.assertEqual(r.status_code, 422)
            self.assertEqual(r.json()["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
