"""B. Chat service tests: OpenRouterChatModel + the real RAG pipeline, through the REAL openai SDK (OpenRouter
base URL) and a MOCKED OpenRouter server."""
import asyncio
import json
import logging
import unittest

import httpx

from app.core import errors as E
from app.services.chat_service import ChatService, RagConfig
from app.services.retrieval_service import RetrievalService
from tests.fixtures.fakes import DIM, PAPER_A, FakeEmbedder, InMemoryStore
from tests.fixtures.openrouter_fakes import CHAT_MODEL, FAKE_KEY, FakeOpenAI, chat_model, error_body

MSGS = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Question: why?"}]
GOOD = json.dumps({"answer": "They do [E1].", "insufficient_evidence": False})


def run(coro):
    return asyncio.run(coro)


class RequestConstructionTests(unittest.TestCase):
    def test_request_uses_configured_model_and_parameters(self):
        fake = FakeOpenAI(chat_reply=GOOD)
        m = chat_model(fake, llm_model=CHAT_MODEL, llm_temperature=0.2, llm_max_output_tokens=321)
        self.assertEqual(run(m.chat(MSGS)), GOOD)
        req = fake.requests[0]
        self.assertTrue(req.url.path.endswith("/v1/chat/completions"))
        self.assertEqual(req.url.host, "openrouter.ai")
        self.assertEqual(req.headers["authorization"], f"Bearer {FAKE_KEY}")  # same key the embedder uses
        body = fake.bodies("/chat/completions")[0]
        self.assertEqual(body["model"], CHAT_MODEL)
        self.assertEqual(body["messages"], MSGS)  # prompt (system rules + evidence + question) passed through verbatim
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.2, 321))
        self.assertNotIn("max_completion_tokens", body)
        self.assertFalse(body.get("stream"))

    def test_model_is_configurable(self):
        fake = FakeOpenAI(chat_reply=GOOD)
        run(chat_model(fake, llm_model="nvidia/nemotron-3.5-super-vl:free").chat(MSGS))
        self.assertEqual(fake.bodies("/chat/completions")[0]["model"], "nvidia/nemotron-3.5-super-vl:free")


class ResponseParsingTests(unittest.TestCase):
    def bad(self, setup, exc):
        fake = FakeOpenAI(chat_reply=GOOD)
        setup(fake)
        with self.assertRaises(exc):
            run(chat_model(fake).chat(MSGS))

    def test_unusable_responses(self):
        self.bad(lambda f: setattr(f, "chat_reply", ""), E.InvalidLLMResponse)                        # empty content
        self.bad(lambda f: setattr(f, "chat_reply", "   "), E.InvalidLLMResponse)
        self.bad(lambda f: setattr(f, "finish_reason", "length"), E.InvalidLLMResponse)               # truncated JSON
        self.bad(lambda f: setattr(f, "finish_reason", "content_filter"), E.InvalidLLMResponse)
        self.bad(lambda f: setattr(f, "refusal", "I can't help with that."), E.InvalidLLMResponse)
        self.bad(lambda f: f.script.append((200, {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                                  "choices": []}, {})), E.InvalidLLMResponse)       # no choices


class ProviderErrorTests(unittest.TestCase):
    def expect(self, status, body, exc, headers=None):
        fake = FakeOpenAI()
        fake.script.append((status, body, headers or {}))
        with self.assertRaises(exc) as ctx:
            run(chat_model(fake).chat(MSGS))
        return ctx.exception

    def test_http_errors_map_to_safe_application_errors(self):
        self.expect(401, error_body("Incorrect API key provided", "invalid_api_key", "authentication_error"), E.LLMAuthFailed)
        self.expect(429, error_body("You exceeded your current quota", "insufficient_quota", "insufficient_quota"), E.LLMQuotaExceeded)
        self.expect(429, error_body("Rate limit reached", "rate_limit_exceeded", "requests"), E.LLMRateLimited)
        self.expect(404, error_body("The model `x` does not exist", "model_not_found"), E.ModelNotAvailable)
        self.expect(403, error_body("Project does not have access to model", "model_not_found"), E.ModelNotAvailable)
        self.expect(500, error_body("server error", None, "server_error"), E.LLMUnavailable)
        self.expect(400, error_body("context length exceeded", "context_length_exceeded"), E.InvalidLLMResponse)

    def test_network_failures(self):
        for exc, expected in ((httpx.ConnectError("refused"), E.LLMUnavailable), (httpx.ReadTimeout("slow"), E.LLMTimeout)):
            fake = FakeOpenAI()
            fake.raise_exc = exc
            with self.assertRaises(expected):
                run(chat_model(fake).chat(MSGS))

    def test_status_codes_returned_to_clients(self):
        self.assertEqual((E.LLMTimeout.status_code, E.LLMAuthFailed.status_code, E.InvalidLLMResponse.status_code), (504, 503, 502))

    def test_retry_is_bounded_and_recovers_from_transient_errors(self):
        fake = FakeOpenAI(chat_reply=GOOD)
        fast = {"retry-after-ms": "1"}
        fake.script += [(429, error_body("slow down", "rate_limit_exceeded"), fast), (500, error_body("oops"), fast)]
        self.assertEqual(run(chat_model(fake, openrouter_max_retries=2).chat(MSGS)), GOOD)
        self.assertEqual(fake.count("/chat/completions"), 3)  # 2 failures + 1 success

        fake = FakeOpenAI()
        fake.script += [(500, error_body("oops"), fast)] * 10
        with self.assertRaises(E.LLMUnavailable):
            run(chat_model(fake, openrouter_max_retries=2).chat(MSGS))
        self.assertEqual(fake.count("/chat/completions"), 3)  # 1 try + 2 retries, then it stops

    def test_secret_never_reaches_exception_message_or_logs(self):
        # Worst case: the provider's error text itself echoes the key.
        body = error_body(f"Incorrect API key provided: {FAKE_KEY}", "invalid_api_key", "authentication_error")
        fake = FakeOpenAI()
        fake.script.append((401, body, {"x-request-id": "req_abc123"}))
        with self.assertLogs("app.services.providers.openrouter_common", level=logging.WARNING) as logs:
            with self.assertRaises(E.LLMAuthFailed) as ctx:
                run(chat_model(fake).chat(MSGS))
        everything = str(ctx.exception) + ctx.exception.message + "".join(logs.output)
        self.assertNotIn(FAKE_KEY, everything)
        self.assertNotIn("FAKE-TEST-KEY", everything)
        self.assertIn("req_abc123", "".join(logs.output))  # request id IS logged for support tickets


class RagPipelineTests(unittest.TestCase):
    """The existing RAG flow end to end with the OpenRouter chat model: retrieval first, evidence + metadata in the prompt,
    citation identifiers and paper metadata surviving into the result."""

    def build(self, reply):
        store = InMemoryStore()
        store.add(PAPER_A, 7, "Convolutional networks detect diabetic retinopathy from fundus images.")
        store.add(PAPER_A, 9, "Completely unrelated sentence about cooking pasta.")
        fake = FakeOpenAI(chat_reply=reply)
        chat = ChatService(RetrievalService(FakeEmbedder(), store, DIM), chat_model(fake), RagConfig(0.1, 5, 6000))
        return chat, fake

    def test_retrieved_evidence_reaches_the_model_and_citations_survive(self):
        chat, fake = self.build(GOOD)
        res = run(chat.answer("convolutional retinopathy fundus"))
        sent = fake.bodies("/chat/completions")[0]["messages"]
        user = sent[-1]["content"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn('<evidence id="E1"', user)
        self.assertIn("Convolutional networks detect diabetic retinopathy", user)  # retrieved passage
        self.assertIn(PAPER_A.title, user)                                       # paper metadata
        self.assertNotIn("cooking pasta", user)                                  # irrelevant chunk not sent (threshold)
        self.assertEqual(res.model, CHAT_MODEL)
        self.assertFalse(res.insufficient_evidence)
        c = res.citations[0]
        self.assertEqual((c.id, c.paper_id, c.title, c.page_number, c.doi), ("E1", "p-a", PAPER_A.title, 7, PAPER_A.doi))

    def test_no_model_call_when_nothing_relevant_is_retrieved(self):
        chat, fake = self.build(GOOD)
        res = run(chat.answer("quantum chromodynamics lattice"))
        self.assertTrue(res.insufficient_evidence)
        self.assertEqual(fake.count("/chat/completions"), 0)  # no evidence => no (paid) generation

    def test_fabricated_citation_ids_are_removed(self):
        chat, _ = self.build(json.dumps({"answer": "Real [E1] and invented [E9].", "insufficient_evidence": False}))
        res = run(chat.answer("convolutional retinopathy fundus"))
        self.assertIn("invalid_citations_removed", res.warnings)
        self.assertEqual([c.id for c in res.citations], ["E1"])
        self.assertNotIn("E9", res.answer)

    def test_provider_failure_surfaces_instead_of_a_fabricated_answer(self):
        chat, fake = self.build(GOOD)
        fake.raise_exc = httpx.ConnectError("down")
        with self.assertRaises(E.LLMUnavailable):
            run(chat.answer("convolutional retinopathy fundus"))


if __name__ == "__main__":
    unittest.main()
