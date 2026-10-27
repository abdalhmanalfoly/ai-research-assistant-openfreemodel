"""RAG orchestration: retrieve -> select evidence -> generate -> validate citations."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from app.services.citation_service import parse_llm_output, process_answer
from app.services.evidence_service import select_evidence
from app.services.prompts import build_messages
from app.services.retrieval_service import RetrievalService
from app.services.types import ChatModel, ChatResult, SearchFilters

log = logging.getLogger(__name__)

NO_CORPUS = "No papers are indexed yet, so I cannot answer from the research corpus."
NO_EVIDENCE = "The indexed papers do not contain sufficiently relevant evidence to answer this question."


def _qhash(question: str) -> str:
    """Short hash of the question for logs: correlates requests without recording user text."""
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]


def _top_scores(candidates, n: int = 8) -> str:
    return "/".join(f"{c.score:.3f}" for c in candidates[:n])


@dataclass(frozen=True)
class RagConfig:
    threshold: float
    default_top_k: int
    evidence_budget_chars: int
    overfetch_factor: int = 3


class ChatService:
    def __init__(self, retrieval: RetrievalService, llm: ChatModel, config: RagConfig):
        self.retrieval, self.llm, self.cfg = retrieval, llm, config

    def _insufficient(self, text: str, retrieved: int = 0, warnings: list[str] | None = None) -> ChatResult:
        return ChatResult(text, [], retrieved, self.llm.model, True, warnings or [])

    async def answer(self, question: str, filters: SearchFilters | None = None, top_k: int | None = None) -> ChatResult:
        filters = filters or SearchFilters()
        top_k = top_k or self.cfg.default_top_k
        qh = _qhash(question)
        log.info("chat: q=%s paper_filter=%d year_filter=%s top_k=%d threshold=%.3f",
                 qh, len(filters.paper_ids or []), (filters.year_min, filters.year_max), top_k, self.cfg.threshold)
        candidates = await self.retrieval.retrieve(question, top_k * self.cfg.overfetch_factor, filters)
        log.info("chat: q=%s retrieved=%d top_scores=%s", qh, len(candidates), _top_scores(candidates))
        if not candidates and not await self.retrieval.corpus_has_chunks():
            log.info("chat: q=%s insufficient reason=no_corpus", qh)
            return self._insufficient(NO_CORPUS)

        evidence, stats = select_evidence(
            candidates, threshold=self.cfg.threshold, max_items=top_k, budget_chars=self.cfg.evidence_budget_chars
        )
        log.info("chat: q=%s evidence_stats=%s selected=%d", qh, stats, len(evidence))
        if not evidence:
            log.info("chat: q=%s insufficient reason=no_evidence_after_filtering candidates=%d below_threshold=%d "
                     "duplicates=%d dropped_for_budget=%d budget=%d",
                     qh, len(candidates), stats.below_threshold, stats.duplicates, stats.dropped_for_budget,
                     self.cfg.evidence_budget_chars)
            return self._insufficient(NO_EVIDENCE, retrieved=len(candidates))

        warnings = ["evidence_truncated_to_fit_context"] if stats.truncated else []
        raw = await self.llm.chat(build_messages(question, evidence))  # no automatic retry on timeout
        answer, model_says_insufficient = parse_llm_output(raw)
        if model_says_insufficient and not answer:
            log.info("chat: q=%s insufficient reason=model_flagged_no_answer evidence=%d", qh, len(evidence))
            return self._insufficient(NO_EVIDENCE, retrieved=len(evidence), warnings=warnings)
        processed = process_answer(answer, evidence)

        if processed.invalid_ids:
            log.warning("removed invalid citation ids from model output: %s", processed.invalid_ids)
            warnings.append("invalid_citations_removed")
        if model_says_insufficient:
            # The model supplied an answer AND flagged insufficiency: keep the text but no citations
            # (the UI presents it as "not a research-backed answer"), matching the documented contract.
            log.info("chat: q=%s insufficient reason=model_flagged_with_answer evidence=%d valid_citations=%d",
                     qh, len(evidence), len(processed.citations))
            return ChatResult(processed.text, [], len(evidence), self.llm.model, True, warnings)
        log.info("chat: q=%s ok evidence=%d citations=%d invalid_citations=%d", qh, len(evidence),
                 len(processed.citations), len(processed.invalid_ids))
        if not processed.citations:
            warnings.append("no_valid_citations")
        return ChatResult(processed.text, processed.citations, len(evidence), self.llm.model, False, warnings)
