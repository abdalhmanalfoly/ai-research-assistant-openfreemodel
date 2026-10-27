"""TEST-ONLY fake of the OpenRouter HTTP API, served through httpx.MockTransport.

The REAL `openai` SDK builds every request (URL, headers, JSON body) and parses every response, including retries and
its exception classes; only the server is fake. Passing these tests therefore says "our code drives the SDK correctly
and handles the documented response/error shapes" - it does NOT prove the real OpenRouter service accepts our requests.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

import httpx

FAKE_KEY = "sk-FAKE-TEST-KEY-do-not-leak-0123456789"
CHAT_MODEL = "nvidia/nemotron-3.5-super-vl:free"
EMBED_MODEL = "nvidia/nemotron-3.5-embed-1b:free"


def hash_vec(text: str, dim: int) -> list[float]:
    v = [0.0] * dim
    for tok in re.findall(r"\w+", text.lower()) or ["_"]:
        v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def error_body(message: str, code: str | None = None, typ: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": typ, "param": None, "code": code}}


class FakeOpenAI:
    """Scriptable fake. `script` is a list of (status, json_body, headers) returned, in order, BEFORE normal behaviour."""

    def __init__(self, dim: int = 1024, chat_reply=None):
        self.dim, self.chat_reply = dim, chat_reply
        self.requests: list[httpx.Request] = []
        self.script: list[tuple[int, dict, dict]] = []
        self.chat_script: list[tuple[int, dict, dict]] = []  # consumed only by /chat/completions requests
        self.mode = "ok"            # embeddings: ok | shuffle | short | drop | zero
        self.finish_reason = "stop"
        self.refusal: str | None = None
        self.model_catalog = {CHAT_MODEL}       # what GET /models lists (membership is what provider-check tests)
        self.embeddings_catalog = {EMBED_MODEL}  # what GET /embeddings/models lists
        self.raise_exc: Exception | None = None  # e.g. httpx.ConnectError / httpx.ReadTimeout

    # ----- plumbing -----
    def async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def sync_client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def bodies(self, path_suffix: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(path_suffix) and r.content]

    def count(self, path_suffix: str) -> int:
        return sum(r.url.path.endswith(path_suffix) for r in self.requests)

    # ----- server -----
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.chat_script and request.url.path.endswith("/chat/completions"):
            status, body, headers = self.chat_script.pop(0)
            return httpx.Response(status, json=body, headers=headers)
        if self.script:
            status, body, headers = self.script.pop(0)
            return httpx.Response(status, json=body, headers=headers)
        path = request.url.path
        if path.endswith("/embeddings/models"):
            return httpx.Response(200, json={"object": "list",
                                             "data": [{"id": m} for m in sorted(self.embeddings_catalog)]})
        if path.endswith("/models"):
            return httpx.Response(200, json={"object": "list",
                                             "data": [{"id": m} for m in sorted(self.model_catalog)]})
        m = re.search(r"/models/(.+)$", path)
        if m and request.method == "GET":
            # OpenRouter does not offer GET /models/{id}; connectivity checks use the catalog listings instead.
            return httpx.Response(404, json=error_body("Not Found", "404"))
        if path.endswith("/embeddings"):
            return self._embeddings(json.loads(request.content))
        if path.endswith("/chat/completions"):
            return self._chat(json.loads(request.content))
        return httpx.Response(404, json=error_body("unknown route"))

    def _embeddings(self, body: dict) -> httpx.Response:
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        dim = body.get("dimensions") or 2048  # the configured model's NATURAL size (2048), per NVIDIA's docs
        vecs = [hash_vec(t, dim) for t in inputs]
        if self.mode == "short":
            vecs = [v[:-1] for v in vecs]
        elif self.mode == "zero":
            vecs = [[0.0] * dim for _ in vecs]
        data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vecs)]
        if self.mode == "drop":
            data = data[:-1]
        elif self.mode == "shuffle":
            data = list(reversed(data))  # the SDK/our code must restore order via `index`
        n = sum(len(t.split()) for t in inputs)
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"],
                                         "usage": {"prompt_tokens": n, "total_tokens": n}})

    def _chat(self, body: dict) -> httpx.Response:
        reply = self.chat_reply(body) if callable(self.chat_reply) else (self.chat_reply or "")
        msg = {"role": "assistant", "content": reply, "refusal": self.refusal}
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "message": msg, "finish_reason": self.finish_reason}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def settings(**kw):
    """Settings for tests: no .env, fake key, tiny SDK retries unless overridden."""
    from app.core.config import Settings
    kw.setdefault("openrouter_api_key", FAKE_KEY)
    kw.setdefault("openrouter_max_retries", 0)
    return Settings(_env_file=None, **kw)


def chat_model(fake: FakeOpenAI, **kw):
    from app.services.providers.openrouter_chat import OpenRouterChatModel
    return OpenRouterChatModel.from_settings(settings(**kw), http_client=fake.async_client())


def embedder(fake: FakeOpenAI, **kw):
    from app.services.providers.openrouter_embeddings import OpenRouterEmbedder
    kw.setdefault("embedding_dim", fake.dim)
    return OpenRouterEmbedder.from_settings(settings(**kw), async_http_client=fake.async_client(),
                                            sync_http_client=fake.sync_client())
