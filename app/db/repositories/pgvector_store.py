"""PRODUCTION chunk store: PostgreSQL + pgvector cosine search. Only chunks produced by the configured
embedding model, belonging to fully indexed papers, are searched."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import DatabaseError
from app.db.models import Chunk, Paper
from app.services.types import PaperRef, ScoredChunk, SearchFilters


def paper_ref(p: Paper) -> PaperRef:
    return PaperRef(str(p.id), p.title, list(p.authors or []), p.doi, p.source_url, p.publication_year)


class PgVectorStore:
    def __init__(self, session_factory, embedding_model: str, embedding_dim: int | None = None):
        self.session_factory, self.embedding_model, self.embedding_dim = session_factory, embedding_model, embedding_dim

    def _compatible(self):
        """Only vectors made by the configured model AND dimension may be compared with the query vector."""
        conds = [Chunk.embedding.is_not(None), Chunk.embedding_model == self.embedding_model]
        if self.embedding_dim is not None:
            conds.append(Chunk.embedding_dim == self.embedding_dim)
        return conds

    def search(self, vector: list[float], limit: int, filters: SearchFilters) -> list[ScoredChunk]:
        dist = Chunk.embedding.cosine_distance(vector)
        q = (select(Chunk, Paper, dist.label("dist")).join(Paper, Paper.id == Chunk.paper_id)
             .where(Paper.status == "indexed", *self._compatible()))  # never compare against other models' vectors
        if filters.paper_ids:
            q = q.where(Paper.id.in_([uuid.UUID(p) for p in filters.paper_ids]))
        if filters.year_min is not None:
            q = q.where(Paper.publication_year >= filters.year_min)
        if filters.year_max is not None:
            q = q.where(Paper.publication_year <= filters.year_max)
        try:
            with self.session_factory() as s:
                rows = s.execute(q.order_by(dist).limit(limit)).all()
        except SQLAlchemyError:
            raise DatabaseError() from None
        return [ScoredChunk(str(c.id), c.text, c.page_number, c.section, c.chunk_index, 1.0 - float(d), paper_ref(p))
                for c, p, d in rows]

    def count_indexed_chunks(self) -> int:
        try:
            with self.session_factory() as s:
                return s.scalar(select(func.count()).select_from(Chunk).join(Paper).where(
                    Paper.status == "indexed", *self._compatible())) or 0
        except SQLAlchemyError:
            raise DatabaseError() from None
