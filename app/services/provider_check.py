"""Explicit, on-demand OpenRouter connectivity check. NEVER runs as part of ordinary health checks.

Default check: the chat model id must appear in OpenRouter's public model catalog (GET /models) and the embedding
model id in its embeddings catalog (GET /embeddings/models). Both listings are free, prove the ids are correctly
spelled and still offered, and cost nothing — but they do NOT authenticate, so a wrong key is only caught by the deep
check. `deep=True` additionally sends one real request per endpoint (a 1-input embedding and a <=16-token chat
completion on the configured FREE models, whose pricing is 0). Results are cached for `min_interval_seconds` so
repeated calls (the API has no authentication) cannot become a rate-limit loop.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

import openai

from app.core.errors import AppError, EmbeddingModelNotAvailable, ModelNotAvailable
from app.services.providers.openrouter_common import translate_openai_error


async def _catalog_ids(client, path: str) -> set[str]:
    """Fetch one catalog page via the SDK (same httpx client/base URL as the real requests). The catalog fits one
    page today; listing endpoints are public, so no model inference and no credentials are consumed."""
    resp = await client.get(path, cast_to=dict)
    return {m["id"] for m in resp.get("data", [])}


class ProviderCheck:
    def __init__(self, chat_model, embedder, *, min_interval_seconds: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.chat, self.embedder = chat_model, embedder
        self.min_interval, self._clock = min_interval_seconds, clock
        self._cache: dict[bool, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def run(self, deep: bool = False) -> dict:
        async with self._lock:
            hit = self._cache.get(deep)
            now = self._clock()
            if hit and now - hit[0] < self.min_interval:
                return {**hit[1], "cached": True, "age_seconds": round(now - hit[0], 1)}
            checks = [await self._catalog_access("chat", self.chat._client, self.chat.model, "llm", "/models"),
                      await self._catalog_access("embedding", self.embedder._aclient, self.embedder.model,
                                                 "embedding", "/embeddings/models")]
            if deep:
                checks.append(await self._deep_embedding())
                checks.append(await self._deep_chat())
            result = {"ok": all(c["status"] == "ok" for c in checks), "deep": deep, "checks": checks, "cached": False}
            self._cache[deep] = (self._clock(), result)
            return result

    @staticmethod
    def _error(name: str, model: str, err: AppError) -> dict:
        return {"check": name, "model": model, "status": "error", "error_code": err.code, "message": err.message}

    @classmethod
    async def _guard(cls, name: str, model: str, coro) -> dict:
        try:
            await coro
        except openai.OpenAIError as e:
            return cls._error(name, model, translate_openai_error(e, "embedding" if "embedding" in name else "llm"))
        except AppError as e:  # e.g. already translated by the embedder
            return cls._error(name, model, e)
        return {"check": name, "model": model, "status": "ok"}

    @classmethod
    async def _catalog_access(cls, name, client, model, kind, path: str) -> dict:
        """Model id is listed in OpenRouter's catalog (free GET; no authentication required)."""
        check = f"{name}_model_access"

        async def run():
            ids = await _catalog_ids(client, path)
            if model not in ids:  # a provider outage surfaces as a translated openai error instead
                if kind == "llm":
                    raise ModelNotAvailable(f"The configured chat model is not listed by the provider catalog.")
                raise EmbeddingModelNotAvailable()
        return await cls._guard(check, model, run())

    async def _deep_embedding(self):
        return await self._guard("embedding_request", self.embedder.model, self.embedder.embed_query("connectivity check"))

    async def _deep_chat(self):
        return await self._guard("chat_request", self.chat.model, self.chat._client.chat.completions.create(
            model=self.chat.model, messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=16))
