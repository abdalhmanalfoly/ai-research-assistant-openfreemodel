"""Question embedding + vector search."""
from __future__ import annotations

import asyncio
import hashlib
import logging

from app.core.errors import AppError, EmbeddingDimensionMismatch, RetrievalError
from app.services.types import ChunkStore, Embedder, ScoredChunk, SearchFilters

log = logging.getLogger(__name__)


def _qhash(question: str) -> str:
    """Short hash for logs: correlates requests without recording user text."""
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]


class RetrievalService:
    def __init__(self, embedder: Embedder, store: ChunkStore, expected_dim: int | None = None):
        self.embedder, self.store, self.expected_dim = embedder, store, expected_dim

    async def retrieve(self, question: str, limit: int, filters: SearchFilters) -> list[ScoredChunk]:
        vector = await self.embedder.embed_query(question)  # raises EmbeddingUnavailable
        if self.expected_dim and len(vector) != self.expected_dim:
            raise EmbeddingDimensionMismatch(
                f"Query embedding has {len(vector)} dimensions; index expects {self.expected_dim}. "
                "Check EMBEDDING_MODEL / EMBEDDING_DIM and re-index if the model changed."
            )
        log.info("retrieve: q=%s model=%s dim=%d paper_filter=%d limit=%d", _qhash(question),
                 getattr(self.embedder, "model", "?"), len(vector), len(filters.paper_ids or []), limit)
        try:
            return await asyncio.to_thread(self.store.search, vector, limit, filters)
        except AppError:
            raise
        except Exception:
            log.exception("vector search failed")
            raise RetrievalError() from None

    async def corpus_has_chunks(self) -> bool:
        try:
            has = await asyncio.to_thread(self.store.count_indexed_chunks)
            log.info("retrieve: chunks_eligible_for_search=%d", has)
            return has > 0
        except Exception:
            log.exception("corpus count failed")
            raise RetrievalError() from None
