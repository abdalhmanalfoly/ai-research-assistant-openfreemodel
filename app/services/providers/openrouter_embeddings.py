"""Embeddings with OpenRouter's Embeddings API (free model: nvidia/nemotron-3.5-embed-1b:free).

The client is the official OpenAI SDK configured with the OpenRouter base URL (`OPENROUTER_BASE_URL`) and
`OPENROUTER_API_KEY`; the model id always comes from `EMBEDDING_MODEL`.

Policy (identical for documents and queries): same model, same `dimensions`, no prefixes or instructions.
OpenRouter's request schema supports `dimensions` (this model's published range is 128..2048, default 2048), but its
catalog does not ADVERTISE per-model parameter support, so whether `dimensions` is honoured for the configured value
must be observed from a real response. That is enforced here: every returned vector is checked against the configured
`embedding_dimension` and anything else raises EmbeddingDimensionMismatch loudly (no silent truncation, no padding).
pgvector searches by cosine distance, which is scale-invariant, so no local normalisation is applied; vectors must be
finite and non-zero. Input order is preserved: the response is re-ordered by its `index` field and checked for
completeness.
"""
from __future__ import annotations

import logging
import math

import httpx
import openai

from app.core.config import Settings
from app.core.errors import (EmbeddingDimensionMismatch, InvalidEmbedding, InvalidEmbeddingInput)
from app.services.providers.openrouter_common import make_async_client, make_sync_client, translate_openai_error

log = logging.getLogger(__name__)


class OpenRouterEmbedder:
    provider = "openrouter"

    def __init__(self, *, model: str, dimension: int, async_client: openai.AsyncOpenAI, sync_client: openai.OpenAI,
                 batch_size: int = 64, max_input_chars: int = 24000, send_dimensions: bool | None = None):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.model, self.dimension, self.batch_size, self.max_input_chars = model, dimension, batch_size, max_input_chars
        # `dimensions` is sent for models whose published documentation confirms they support it; unknown models get
        # their natural default size (validated at runtime instead of assumed).
        info = Settings.embedding_info(model)
        self.send_dimensions = info["dimensions_param"] if (info and send_dimensions is None) else bool(send_dimensions)
        self._aclient, self._sclient = async_client, sync_client

    @classmethod
    def from_settings(cls, s: Settings, async_http_client: httpx.AsyncClient | None = None,
                      sync_http_client: httpx.Client | None = None) -> "OpenRouterEmbedder":
        t = s.embedding_timeout_seconds
        return cls(model=s.embedding_model, dimension=s.embedding_dim, batch_size=s.embedding_batch_size,
                   max_input_chars=s.embedding_max_input_chars,
                   async_client=make_async_client(s, timeout=t, http_client=async_http_client),
                   sync_client=make_sync_client(s, timeout=t, http_client=sync_http_client))

    async def aclose(self) -> None:
        await self._aclient.close()
        self._sclient.close()

    # ---------- validation ----------
    def _check_texts(self, texts: list[str]) -> None:
        for t in texts:
            if not isinstance(t, str) or not t.strip():
                raise InvalidEmbeddingInput()
            if len(t) > self.max_input_chars:
                raise InvalidEmbeddingInput(f"Text to embed is too long ({len(t)} > {self.max_input_chars} characters).")

    def _kwargs(self, batch: list[str]) -> dict:
        kw: dict = {"model": self.model, "input": batch, "encoding_format": "float"}
        if self.send_dimensions:
            kw["dimensions"] = self.dimension
        return kw

    def _validate(self, resp, expected: int) -> list[list[float]]:
        items = sorted(resp.data or [], key=lambda d: d.index)
        if len(items) != expected or [d.index for d in items] != list(range(expected)):
            raise InvalidEmbedding("Embedding provider returned an unexpected number of vectors.")
        out: list[list[float]] = []
        for d in items:
            v = [float(x) for x in d.embedding]
            if len(v) != self.dimension:
                raise EmbeddingDimensionMismatch(
                    f"Embedding has {len(v)} dimensions; expected {self.dimension} (EMBEDDING_DIMENSIONS).")
            if not all(math.isfinite(x) for x in v) or not any(v):
                raise InvalidEmbedding("Embedding contains NaN/infinite values or is all zeros.")
            out.append(v)
        return out

    # ---------- sync API (re-index scripts, worker threads) ----------
    def embed_documents_sync(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list):
            raise InvalidEmbeddingInput()
        if not texts:
            return []
        self._check_texts(texts)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):  # bounded request size
            batch = texts[i:i + self.batch_size]
            try:
                resp = self._sclient.embeddings.create(**self._kwargs(batch))
            except openai.OpenAIError as e:
                raise translate_openai_error(e, "embedding") from None
            out.extend(self._validate(resp, len(batch)))
        return out

    def embed_query_sync(self, text: str) -> list[float]:
        return self.embed_documents_sync([text])[0] if isinstance(text, str) else self._reject()

    @staticmethod
    def _reject():
        raise InvalidEmbeddingInput()

    # ---------- async API (request path) ----------
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list):
            raise InvalidEmbeddingInput()
        if not texts:
            return []
        self._check_texts(texts)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            try:
                resp = await self._aclient.embeddings.create(**self._kwargs(batch))
            except openai.OpenAIError as e:
                raise translate_openai_error(e, "embedding") from None
            out.extend(self._validate(resp, len(batch)))
        return out

    async def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str):
            raise InvalidEmbeddingInput()
        return (await self.embed_documents([text]))[0]
