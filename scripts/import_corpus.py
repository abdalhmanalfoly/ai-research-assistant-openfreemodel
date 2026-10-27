"""Import existing research PDFs.

  python -m scripts.import_corpus --dir data/pdfs
  python -m scripts.import_corpus --manifest corpus.json
  python -m scripts.import_corpus --reindex      # alias for: python -m scripts.reindex_embeddings (chunk-level, resumable)

Manifest: JSON list of {"path": "...pdf", "title": "...", "authors": ["..."], "year": 2021, "doi": "...",
"source_url": "...", "abstract": "..."}; only "path" and "title" are required. Metadata is taken from YOUR files;
nothing is invented. Without a manifest the title is the file name.
"""
import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db.models import Paper
from app.main import build_container


async def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dir")
    g.add_argument("--manifest")
    g.add_argument("--reindex", action="store_true")
    a = ap.parse_args()
    c = build_container()
    ing = c.ingestion
    try:
        if a.reindex:
            from app.db.repositories.chunk_repo import SqlChunkRepo
            from app.services.reindex_service import ReindexService
            repo = SqlChunkRepo(c.session_factory, c.settings.embedding_model, c.settings.embedding_dim)
            rep = ReindexService(repo, c.embedder).run()
            print(f"reindex: updated={rep.updated} failed={len(rep.failed_ids)} still_pending={repo.count_pending()}")
            return
        else:
            if a.manifest:
                entries = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
                base = Path(a.manifest).parent
                entries = [{**e, "path": str(base / e["path"])} for e in entries]
            else:
                entries = [{"path": str(p), "title": p.stem} for p in sorted(Path(a.dir).glob("*.pdf"))]
            jobs = []
            for e in entries:
                pid, jid = ing.register(Path(e["path"]).read_bytes(), title=e["title"], authors=e.get("authors"),
                                        abstract=e.get("abstract"), year=e.get("year"), doi=e.get("doi"),
                                        source_url=e.get("source_url"))
                print(f"{e['path']}: paper={pid} {'queued' if jid else 'already registered (skipped)'}")
                if jid:
                    jobs.append(jid)
        for jid in jobs:
            await ing.run_job(jid)
        print(f"processed {len(jobs)} job(s)")
    finally:
        await c.llm.aclose()
        await c.embedder.aclose()


if __name__ == "__main__":
    asyncio.run(main())
