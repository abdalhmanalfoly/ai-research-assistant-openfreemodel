"""READ-ONLY retrieval diagnostic — no DB writes, no chat-model calls, no reindexing.

  python -m scripts.diagnose_retrieval "your research question here"

For the configured model/dimension it reports:
  1. CORPUS: how many chunks the exact production filter would search.
  2. TOP-10 candidates for the question, with the production score conversion (1.0 - cosine distance),
     paper titles, chunk ids, page numbers and short previews. PASS/FAIL against the configured threshold.
  3. SELF-CONSISTENCY PROBE: one stored chunk's OWN text embedded AS A QUERY and searched — the strongest
     possible match is that chunk itself. Indexing and queries send identical requests, so a healthy pipeline
     must place it at the top with a score near the top of the scale. Low => structural divergence between
     the query path and the document path (a bug). High but real questions at ~0.2 => healthy vectors,
     threshold miscalibrated for this model's score scale.
  4. UNRELATED-PROBE: a query with no plausible overlap ("quantum supergravity flux compactification")
     to show what IRRELEVANT looks like on this corpus — the scale's floor.

Needs OPENROUTER_API_KEY in .env (used only to embed the probe texts; the chat model is never called).
SELECT-only database access; prints no secrets.
"""
import asyncio
import json
import sys

import httpx
import sqlalchemy as sa

from app.core.config import get_settings
from app.db.models import Chunk, Paper  # noqa: F401  (models register the tables on Base)
from app.db.session import get_session_factory
from app.services.providers.openrouter_embeddings import OpenRouterEmbedder

S = get_settings()
SF = get_session_factory()


def sf_exec(sql, params=None):
    with SF() as s:
        return s.execute(sa.text(sql), params or {}).all()


async def embed(texts):
    e = OpenRouterEmbedder.from_settings(S)
    try:
        return await e.embed_documents(texts)
    finally:
        await e.aclose()


def top10(vector):
    return sf_exec(
        "select c.id::text, c.paper_id::text, c.embedding_model, c.embedding_dim, length(c.text), "
        "left(c.text, 160), c.page_number, p.title, p.status, 1.0 - (c.embedding <=> :v) sim "
        "from chunks c join papers p on p.id = c.paper_id "
        "where p.status='indexed' and c.embedding_model = :m and c.embedding_dim = :d "
        "order by c.embedding <=> :v limit 10",
        {"v": json.dumps(vector), "m": S.embedding_model, "d": S.embedding_dim})


TH = S.retrieval_threshold


def show(question, vector):
    print(f"\n=== question: «{question[:80]}» ===")
    rows = top10(vector)
    print(f"{'#':>2} {'pass)':<5} {'sim':<7} {'chars':>5}  paper title / preview")
    for i, (cid, pid, model, dim, chars, preview, page, title, status, sim) in enumerate(rows, 1):
        print(f"{i:>2} {'PASS' if sim >= TH else 'FAIL':<5} {sim:.4f}  {chars:>5}  [{title[:40]}] p.{page} "
              f"chunk={cid[:8]} dim={dim}")
        print(f"      «{preview[:110]}»")
    return [(r[9], r[5], r) for r in rows]


async def run(question: str):
    n = sf_exec("select count(*) from chunks c join papers p on p.id=c.paper_id where p.status='indexed' "
                "and c.embedding_model=:m and c.embedding_dim=:d", {"m": S.embedding_model, "d": S.embedding_dim})
    print(f"config: model={S.embedding_model} dim={S.embedding_dim} threshold={TH}")
    print(f"corpus (exact production filter): {n[0][0]} chunk(s)")

    v = (await embed([question]))[0]
    show(question, v)

    # SELF-CONSISTENCY probe: a stored text embedded as a QUERY should find itself at sim ~ top of scale.
    row = sf_exec("select c.id::text, c.text from chunks c join papers p on p.id=c.paper_id "
                  "where p.status='indexed' and c.embedding_model=:m and c.embedding_dim=:d "
                  "and length(c.text) between 300 and 900 order by random() limit 1",
                  {"m": S.embedding_model, "d": S.embedding_dim})[0]
    chunk_id, chunk_text = row[0], row[1]
    qv = (await embed([chunk_text]))[0]
    sims = sf_exec("select 1.0 - (c.embedding <=> :v) sim from chunks c join papers p on p.id=c.paper_id "
                   "where p.status='indexed' and c.embedding_model=:m and c.embedding_dim=:d "
                   "order by c.embedding <=> :v limit 3", {"v": json.dumps(qv), "m": S.embedding_model,
                                                          "d": S.embedding_dim})
    self_sim = sims[0][0]
    print(f"\nSELF-CONSISTENCY PROBE (stored chunk {chunk_id[:8]} embedded AS A QUERY):")
    for i, r in enumerate(sims, 1):
        print(f"{i}. sim={r[0]:.4f}")
    if self_sim < 0.7:
        print("  -> LOW self-similarity: query path DIVERGES from the document path (structural bug!).")
    else:
        print("  -> high self-similarity: query/document paths AGREE; scores reflect the model's real scale.")

    # UNRELATED probe: the floor of the scale on this corpus.
    v_bad = (await embed(["quantum supergravity flux compactification in transducers"]))[0]
    floor = sf_exec("select 1.0 - (c.embedding <=> :v) sim from chunks c join papers p on p.id=c.paper_id "
                    "where p.status='indexed' and c.embedding_model=:m and c.embedding_dim=:d "
                    "order by c.embedding <=> :v limit 3",
                    {"v": json.dumps(v_bad), "m": S.embedding_model, "d": S.embedding_dim})
    print("\nUNRELATED-PROBE floor of the scale:")
    for r in floor:
        print(f"sim={r[0]:.4f}")
    print("\nThreshold 0.30 verdict: " + (
        "too strict for this model (genuine content scores below 0.30)" if self_sim >= 0.7 and sims[1][0] >= TH
        else "see cases above"))


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "What are the main findings, methodology and limitations "\
                                             "of the selected research papers?"
    asyncio.run(run(q))
