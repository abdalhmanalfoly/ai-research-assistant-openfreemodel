"""Text normalisation and page-aware chunking. Chunks never span pages, so page numbers stay exact."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based PDF page number
    text: str


@dataclass(frozen=True)
class ChunkDraft:
    chunk_index: int
    page_number: int
    section: str | None
    text: str


_HEADING_RE = re.compile(
    r"^(?:(?:\d{1,2}(?:\.\d{1,2})*\.?)\s+)?"
    r"(abstract|introduction|related work|background|methods?|methodology|materials and methods|"
    r"experiments?|results?|discussion|limitations|conclusions?|references|acknowledg(?:e)?ments)\s*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[(])")


def normalize_text(text: str) -> str:
    """Conservative clean-up. Keeps symbols, equations and Unicode (no NFKC folding)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)  # "segmen-\ntation" -> "segmentation"
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _paragraphs(text: str) -> list[str]:
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        if len(lines) > 1 and _HEADING_RE.match(lines[0].strip()):
            paras.append(lines[0].strip())  # heading stays its own paragraph
            block = " ".join(l.strip() for l in lines[1:])
        else:
            block = " ".join(l.strip() for l in lines)
        paras.append(block)
    return paras


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(paragraph):
        while len(sentence) > max_chars:  # no sentence boundary available
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_pages(pages: list[PageText], max_chars: int = 1200, min_chars: int = 40) -> list[ChunkDraft]:
    """Split pages into chunks of at most max_chars, preserving page number and last-seen section."""
    if max_chars < 100:
        raise ValueError("max_chars must be >= 100")
    drafts: list[ChunkDraft] = []
    section: str | None = None
    for page in pages:
        buf = ""
        buf_section = section
        for para in _paragraphs(normalize_text(page.text)):
            m = _HEADING_RE.match(para)
            if m:
                if len(buf.strip()) >= min_chars:
                    drafts.append(ChunkDraft(len(drafts), page.page_number, buf_section, buf.strip()))
                buf = ""
                section = m.group(1).title()
                buf_section = section
                continue
            for piece in (_split_long(para, max_chars) if len(para) > max_chars else [para]):
                if buf and len(buf) + 2 + len(piece) > max_chars:
                    if len(buf.strip()) >= min_chars:
                        drafts.append(ChunkDraft(len(drafts), page.page_number, buf_section, buf.strip()))
                    buf = ""
                    buf_section = section
                buf = f"{buf}\n\n{piece}".strip()
        if len(buf.strip()) >= min_chars:
            drafts.append(ChunkDraft(len(drafts), page.page_number, buf_section, buf.strip()))
    return drafts
