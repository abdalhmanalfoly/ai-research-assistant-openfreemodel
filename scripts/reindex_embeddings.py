"""Re-embed EXISTING chunks with the configured OpenRouter embedding model (default: nvidia/nemotron-3.5-embed-1b:free).

  python -m scripts.reindex_embeddings --status    # counts + rough size of the work. No API call, no key needed.
  python -m scripts.reindex_embeddings             # re-embed all pending chunks (resumable). Needs OPENROUTER_API_KEY.
  python -m scripts.reindex_embeddings --batch-size 64 --max-batches 5     # bounded trial run
  python -m scripts.reindex_embeddings --verify    # SQL check that every chunk holds a current, correctly sized vector

A chunk is "pending" when it has no embedding, or its embedding_model/embedding_dim differ from the configured ones
(e.g. after migration 0005). Reads chunk text from the database, writes new vectors + model/dim metadata, commits per
batch. Safe to Ctrl+C and re-run; chunks already embedded with the current model are never sent to the API again.
Chunks that fail stay pending. Does not re-extract PDFs and cannot create duplicate chunks. Old vectors are kept in
chunks.embedding_prev (and embedding_prev_or) and are never read or deleted by this script. A database lock prevents
two runs overlapping.
Exit code 0 = finished AND verified; 1 = failures or anything still pending; 2 = configuration/provider error.
"""
import argparse
import sys

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import setup_logging
from app.db.repositories.chunk_repo import SqlChunkRepo, reindex_lock
from app.db.session import get_session_factory
from app.services.reindex_service import ReindexService


def _print_status(repo, st):
    pending, total = repo.count_pending(), repo.count_total()
    chars = repo.pending_chars()
    print(f"model={st.embedding_model} dimensions={st.embedding_dim} total_chunks={total} pending={pending}")
    print(f"pending text: {chars} characters (roughly {chars // 4} tokens; check the provider's pricing/limits - "
          "the configured model is free per its published pricing but has rate limits)")
    print("embedding_model breakdown:", repo.model_breakdown())


def _print_verify(repo) -> bool:
    v = repo.verify()
    print(f"verify: total={v.total} current={v.current} pending={v.pending} wrong_dimension={v.wrong_dimension} "
          f"inconsistent={v.inconsistent} -> {'PASS' if v.ok else 'FAIL'}")
    return v.ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--batch-size", type=int, default=None, help="chunks per API request (default EMBEDDING_BATCH_SIZE)")
    ap.add_argument("--max-batches", type=int)
    a = ap.parse_args()
    st = get_settings()
    setup_logging(st.log_level)
    sf = get_session_factory()
    repo = SqlChunkRepo(sf, st.embedding_model, st.embedding_dim)
    _print_status(repo, st)
    if a.status:
        return 0
    if a.verify:
        return 0 if _print_verify(repo) else 1
    try:
        st.require_openrouter_api_key()
        from app.main import build_embedder  # imported late: --status/--verify must not need the key
        with reindex_lock(sf):
            emb = build_embedder(st)
            svc = ReindexService(repo, emb, a.batch_size or st.embedding_batch_size,
                                 lambda r: print(f"  batch {r.batches}: updated={r.updated} failed={len(r.failed_ids)}"))
            rep = svc.run(a.max_batches)
    except AppError as e:
        print(f"stopped: [{e.code}] {e.message}", file=sys.stderr)
        print("Completed batches are saved; fix the problem and re-run to resume.", file=sys.stderr)
        return 2
    print(f"done: updated={rep.updated} failed={len(rep.failed_ids)} still_pending={repo.count_pending()}")
    if rep.failed_ids:
        print("failed chunk ids (left pending):", ", ".join(rep.failed_ids[:50]))
    if a.max_batches is None:
        return 0 if _print_verify(repo) else 1
    return 0 if not rep.failed_ids else 1


if __name__ == "__main__":
    sys.exit(main())
