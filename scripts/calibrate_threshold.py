"""Help choose RETRIEVAL_THRESHOLD from YOUR labelled questions (needs DB + OPENROUTER_API_KEY; embeds each question
once = one small API request per question).

  python -m scripts.calibrate_threshold questions.json
questions.json: [{"question": "...", "on_topic": true}, {"question": "...", "on_topic": false}, ...]
on_topic = your corpus should contain the answer. Prints the top-1 cosine score per question and the threshold
that best separates the two groups. Use >= 20 on-topic and >= 10 off-topic questions, then sanity-check by hand.
"""
import json
import sys

from app.core.config import get_settings
from app.db.repositories.pgvector_store import PgVectorStore
from app.db.session import get_session_factory
from app.main import build_embedder
from app.services.types import SearchFilters


def main(path):
    st = get_settings()
    emb, store = build_embedder(st), PgVectorStore(get_session_factory(), st.embedding_model, st.embedding_dim)
    rows = []
    for q in json.load(open(path, encoding="utf-8")):
        hits = store.search(emb.embed_query_sync(q["question"]), 1, SearchFilters())
        rows.append((hits[0].score if hits else 0.0, bool(q["on_topic"]), q["question"]))
        print(f"{rows[-1][0]:.3f}  {'ON ' if rows[-1][1] else 'OFF'}  {q['question'][:70]}")
    best = max((sum((s >= t) == on for s, on, _ in rows) / len(rows), t) for t in sorted({r[0] for r in rows}))
    print(f"\nbest accuracy {best[0]:.0%} at threshold >= {best[1]:.3f} (suggestion only; review misclassified rows)")


if __name__ == "__main__":
    main(sys.argv[1])
