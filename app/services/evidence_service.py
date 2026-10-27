"""Filters retrieved chunks and builds the evidence set that fits the model's context budget."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.services.types import Evidence, ScoredChunk

PER_ITEM_OVERHEAD = 120  # approximate characters of tag/metadata wrapper per evidence item
_TOKEN = re.compile(r"\w+", re.UNICODE)


@dataclass
class EvidenceStats:
    candidates: int = 0
    below_threshold: int = 0
    duplicates: int = 0
    dropped_for_budget: int = 0
    truncated: bool = False


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text)}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def select_evidence(
    candidates: list[ScoredChunk],
    *,
    threshold: float,
    max_items: int,
    budget_chars: int,
    dedupe_similarity: float = 0.85,
    min_partial_chars: int = 300,
) -> tuple[list[Evidence], EvidenceStats]:
    stats = EvidenceStats(candidates=len(candidates))
    kept: list[ScoredChunk] = []
    kept_tokens: list[set[str]] = []
    remaining = budget_chars

    for chunk in sorted(candidates, key=lambda c: c.score, reverse=True):
        if len(kept) >= max_items:
            break
        if chunk.score < threshold:
            stats.below_threshold += 1
            continue
        toks = _tokens(chunk.text)
        if any(_jaccard(toks, t) >= dedupe_similarity for t in kept_tokens):
            stats.duplicates += 1
            continue
        cost = len(chunk.text) + PER_ITEM_OVERHEAD
        if cost > remaining:
            room = remaining - PER_ITEM_OVERHEAD
            if room >= min_partial_chars:
                chunk = replace(chunk, text=chunk.text[:room].rstrip() + " …[truncated]")
                stats.truncated = True
                kept.append(chunk)
                kept_tokens.append(toks)
            else:
                stats.dropped_for_budget += 1
            break
        kept.append(chunk)
        kept_tokens.append(toks)
        remaining -= cost

    evidence = [Evidence(id=f"E{i}", chunk=c) for i, c in enumerate(kept, start=1)]
    return evidence, stats
