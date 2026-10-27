"""SQL access for (re-)embedding existing chunks. A chunk is 'pending' when it has no embedding, or its
embedding_model / embedding_dim differ from the configured ones."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import AppError, DatabaseError, EmbeddingDimensionMismatch
from app.db.models import Chunk


REINDEX_LOCK_KEY = 726_001  # arbitrary application-wide advisory-lock id for "re-index running"


class ReindexAlreadyRunning(AppError):
    status_code, code, message = 409, "reindex_already_running", "Another re-index job is already running."


@dataclass
class IndexVerification:
    total: int
    current: int            # embedded with the configured model AND dimension (and the stored vector really has it)
    pending: int            # missing, or made by another model/dimension
    wrong_dimension: int    # stored vector length differs from the configured dimension
    inconsistent: int       # model metadata set although the vector is NULL

    @property
    def ok(self) -> bool:
        return self.total == self.current and self.pending == 0 and self.wrong_dimension == 0 and self.inconsistent == 0


@contextmanager
def reindex_lock(session_factory):
    """Hold a PostgreSQL session-level advisory lock for the duration of a re-index run, so two runs cannot
    overlap and double-spend API calls. The lock lives on one dedicated connection (released on exit/crash)."""
    engine = session_factory.kw["bind"]
    try:
        conn = engine.connect()
    except Exception:
        raise DatabaseError() from None
    try:
        if not conn.execute(select(func.pg_try_advisory_lock(REINDEX_LOCK_KEY))).scalar():
            raise ReindexAlreadyRunning()
        try:
            yield
        finally:
            conn.execute(select(func.pg_advisory_unlock(REINDEX_LOCK_KEY)))
    finally:
        conn.close()


class SqlChunkRepo:
    def __init__(self, session_factory, model: str, dim: int):
        self.sf, self.model, self.dim = session_factory, model, dim

    def _pending(self):
        return or_(Chunk.embedding.is_(None), Chunk.embedding_model.is_distinct_from(self.model),
                   Chunk.embedding_dim.is_distinct_from(self.dim))

    def count_pending(self) -> int:
        try:
            with self.sf() as s:
                return s.scalar(select(func.count()).select_from(Chunk).where(self._pending())) or 0
        except SQLAlchemyError:
            raise DatabaseError() from None

    def count_total(self) -> int:
        try:
            with self.sf() as s:
                return s.scalar(select(func.count()).select_from(Chunk)) or 0
        except SQLAlchemyError:
            raise DatabaseError() from None

    def pending_chars(self) -> int:
        """Total characters of text still to embed (for a rough cost estimate: roughly 4 characters per token)."""
        try:
            with self.sf() as s:
                return int(s.scalar(select(func.coalesce(func.sum(func.length(Chunk.text)), 0)).where(self._pending())) or 0)
        except SQLAlchemyError:
            raise DatabaseError() from None

    def verify(self) -> IndexVerification:
        """Post-re-index validation, computed in SQL from the stored vectors themselves (not from metadata alone)."""
        d, m = self.dim, self.model
        q = text("""
            select count(*),
                   count(*) filter (where embedding is not null and embedding_model = :m and embedding_dim = :d
                                      and vector_dims(embedding) = :d),
                   count(*) filter (where embedding is null or embedding_model is distinct from :m
                                      or embedding_dim is distinct from :d),
                   count(*) filter (where embedding is not null and vector_dims(embedding) <> :d),
                   count(*) filter (where embedding is null and (embedding_model is not null or embedding_dim is not null))
            from chunks""")
        try:
            with self.sf() as s:
                return IndexVerification(*s.execute(q, {"m": m, "d": d}).one())
        except SQLAlchemyError:
            raise DatabaseError() from None

    def model_breakdown(self) -> list[tuple[str | None, int]]:
        try:
            with self.sf() as s:
                return [(m, n) for m, n in s.execute(
                    select(Chunk.embedding_model, func.count()).group_by(Chunk.embedding_model))]
        except SQLAlchemyError:
            raise DatabaseError() from None

    def fetch_pending(self, after_id: str | None, limit: int) -> list[tuple[str, str]]:
        """Keyset pagination by id, so an interrupted run simply resumes with whatever is still pending."""
        q = select(Chunk.id, Chunk.text).where(self._pending()).order_by(Chunk.id).limit(limit)
        if after_id:
            q = q.where(Chunk.id > uuid.UUID(after_id))
        try:
            with self.sf() as s:
                return [(str(i), t) for i, t in s.execute(q)]
        except SQLAlchemyError:
            raise DatabaseError() from None

    def save(self, items: list[tuple[str, list[float]]]) -> None:
        if not items:
            return
        for _, v in items:  # reject incompatible vectors before they reach the database
            if len(v) != self.dim:
                raise EmbeddingDimensionMismatch(f"Refusing to store a {len(v)}-dimensional vector; expected {self.dim}.")
        rows = [{"id": uuid.UUID(i), "embedding": v, "embedding_model": self.model, "embedding_dim": len(v)}
                for i, v in items]
        try:
            with self.sf() as s:
                s.execute(update(Chunk), rows)  # bulk UPDATE by primary key; one commit per batch
                s.commit()
        except SQLAlchemyError:
            raise DatabaseError() from None
