"""Plain data types and interfaces used by the RAG core. No third-party imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PaperRef:
    id: str
    title: str
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    source_url: str | None = None
    year: int | None = None


@dataclass(frozen=True)
class ScoredChunk:
    chunk_id: str
    text: str
    page_number: int | None  # None = unknown; never guessed
    section: str | None
    chunk_index: int
    score: float  # cosine similarity, higher is better
    paper: PaperRef


@dataclass(frozen=True)
class Evidence:
    id: str  # "E1", "E2", ... assigned by the application
    chunk: ScoredChunk


@dataclass(frozen=True)
class Citation:
    id: str
    paper_id: str
    title: str
    authors: list[str]
    page_number: int | None
    section: str | None
    doi: str | None
    source_url: str | None
    relevance_score: float
    excerpt: str | None = None  # start of the evidence chunk text (may itself be truncated), for display


@dataclass
class ChatResult:
    answer: str
    citations: list[Citation]
    retrieved_chunks: int
    model: str
    insufficient_evidence: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchFilters:
    paper_ids: list[str] | None = None
    year_min: int | None = None
    year_max: int | None = None


class Embedder(Protocol):
    model: str

    async def embed_query(self, text: str) -> list[float]: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class ChunkStore(Protocol):
    def search(self, vector: list[float], limit: int, filters: SearchFilters) -> list[ScoredChunk]: ...
    def count_indexed_chunks(self) -> int: ...


class ChatModel(Protocol):
    model: str

    async def chat(self, messages: list[dict]) -> str: ...
