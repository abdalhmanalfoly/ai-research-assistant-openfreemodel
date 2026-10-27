"""Resumable, batch-wise re-embedding of EXISTING chunks (no PDF re-extraction, no new chunks).

Progress is persisted per batch (the 'pending' predicate), so interrupting and re-running just continues.
A chunk whose embedding fails stays pending; it is never marked as indexed.

Error policy with a remote provider:
  * rate limit        -> bounded wait-and-retry (backoff below), then stop (completed batches are already saved)
  * auth/quota/outage -> stop immediately (EmbeddingUnavailable); retrying every chunk would only burn requests
  * wrong dimensions  -> stop immediately (configuration error affecting every chunk)
  * bad single text   -> isolated item by item; that chunk stays pending
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from app.core.errors import AppError, EmbeddingDimensionMismatch, EmbeddingRateLimited, EmbeddingUnavailable

log = logging.getLogger(__name__)


class ChunkRepo(Protocol):
    def count_pending(self) -> int: ...
    def fetch_pending(self, after_id: str | None, limit: int) -> list[tuple[str, str]]: ...
    def save(self, items: list[tuple[str, list[float]]]) -> None: ...


@dataclass
class ReindexReport:
    pending_at_start: int = 0
    updated: int = 0
    failed_ids: list[str] = field(default_factory=list)
    batches: int = 0


class ReindexService:
    def __init__(self, repo: ChunkRepo, embedder, batch_size: int = 64,
                 on_progress: Callable[[ReindexReport], None] | None = None,
                 rate_limit_backoff: tuple[float, ...] = (5.0, 15.0, 45.0), sleep: Callable[[float], None] = time.sleep):
        self.repo, self.embedder, self.batch_size, self.on_progress = repo, embedder, batch_size, on_progress
        self.rate_limit_backoff, self._sleep = rate_limit_backoff, sleep

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(len(self.rate_limit_backoff) + 1):
            try:
                return self.embedder.embed_documents_sync(texts)
            except EmbeddingRateLimited:
                if attempt == len(self.rate_limit_backoff):
                    raise  # still limited after the bounded retries: stop; re-running resumes
                wait = self.rate_limit_backoff[attempt]
                log.warning("embedding rate limited; waiting %.0fs (retry %d/%d)", wait, attempt + 1, len(self.rate_limit_backoff))
                self._sleep(wait)
        raise AssertionError("unreachable")

    def run(self, max_batches: int | None = None) -> ReindexReport:
        rep = ReindexReport(pending_at_start=self.repo.count_pending())
        after: str | None = None
        while max_batches is None or rep.batches < max_batches:
            rows = self.repo.fetch_pending(after, self.batch_size)
            if not rows:
                break
            after = rows[-1][0]
            ok = self._embed(rows, rep)
            self.repo.save(ok)  # committed before the next batch
            rep.updated += len(ok)
            rep.batches += 1
            if self.on_progress:
                self.on_progress(rep)
        return rep

    def _embed(self, rows: list[tuple[str, str]], rep: ReindexReport) -> list[tuple[str, list[float]]]:
        try:
            vecs = self._embed_texts([t for _, t in rows])
            return list(zip([i for i, _ in rows], vecs))
        except (EmbeddingUnavailable, EmbeddingDimensionMismatch):
            raise  # infrastructure/config problem (provider down, auth, quota, wrong dims): stop; completed batches are saved
        except AppError:
            pass  # bad text or bad vector somewhere in the batch: isolate it item by item
        ok = []
        for cid, text in rows:
            try:
                ok.append((cid, self._embed_texts([text])[0]))
            except (EmbeddingUnavailable, EmbeddingDimensionMismatch):
                raise
            except AppError as e:
                log.warning("chunk %s could not be embedded (%s); left pending", cid, e.code)
                rep.failed_ids.append(cid)
        return ok
