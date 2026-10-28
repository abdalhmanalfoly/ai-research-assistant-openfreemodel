import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import get_settings

DIM = get_settings().embedding_dim


def _now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"
    # Owner-scoped uniqueness (audit 2026-10-28: the live schema replaced 0001's global doi/content_hash uniques
    # with per-owner ones; all rows uploaded without an owner keep owner_id NULL and are application-invisible
    # to scoping). NOTE: unique constraints treat NULLs as distinct, so unowned rows are only ever "unique"
    # accidentally — the ingestion path should always set owner_id when auth is wired up.
    __table_args__ = (UniqueConstraint("owner_id", "doi", name="uq_papers_owner_doi"),
                      UniqueConstraint("owner_id", "content_hash", name="uq_papers_owner_content_hash"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # FK users(id) — created by migration 0006; no ORM User model yet (docs: tables added only when a feature needs them)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list] = mapped_column(JSONB, default=list)
    abstract: Mapped[str | None] = mapped_column(Text)
    publication_year: Mapped[int | None] = mapped_column(Integer, index=True)
    doi: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 of PDF; idempotency key
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|processing|indexed|failed|needs_ocr
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="paper", cascade="all, delete-orphan", passive_deletes=True)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("paper_id", "chunk_index"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer)  # NULL = unknown
    section: Mapped[str | None] = mapped_column(String(100))
    chunk_index: Mapped[int] = mapped_column(Integer)
    # NULL embedding = not (yet) embedded with the current model (e.g. pending re-index). Old vectors, if any,
    # live in the unmapped column embedding_legacy (migration 0003) and are never searched.
    embedding = mapped_column(Vector(DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), index=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    paper: Mapped[Paper] = relationship(back_populates="chunks")


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|succeeded|failed
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
