"""Standalone Nemotron embedding utility for the legacy arXiv archive (READ-ONLY database, LOCAL-ONLY output).

Reads abstracts from `public.papers_arxiv_legacy`, embeds them with the ALREADY-CONFIGURED OpenRouter embedding
model (EMBEDDING_MODEL, default nvidia/nemotron-3.5-embed-1b:free) through the official OpenAI SDK pointed at
OPENROUTER_BASE_URL, and writes the resulting vectors to local JSONL files. It never touches `papers`, `chunks`,
the corpus re-index pipeline, Alembic, or the database: all SQL is SELECT-only and every session is discarded
without commit.

This tool intentionally does NOT reuse the application's `chunks` column size (whatever EMBEDDING_DIMENSIONS is
configured to be; the app is itself migrating to 2048): it exports the vectors the model returns natively and
records the ACTUAL observed dimension per record.

Usage:
  python -m scripts.nemotron_embedding_tool inspect
  python -m scripts.nemotron_embedding_tool sample  --limit 3
  python -m scripts.nemotron_embedding_tool export  --limit 100
  python -m scripts.nemotron_embedding_tool export  --all          # typed confirmation required

Options: --output-dir, --batch-size, --dimensions, --timeout, --max-attempts, --fresh, --yes
Resume: rerunning `export` skips records already completed in the output JSONL (matched by record id AND
content hash; a hash mismatch aborts instead of overwriting). A `--fresh` flag starts an empty file.

Exit codes: 0 ok, 1 records failed / partial, 2 configuration or provider error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # allow `python scripts/...` as well as `python -m scripts....`
    sys.path.insert(0, str(_ROOT))

import openai  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.errors import (AppError, ConfigurationError, EmbeddingDimensionMismatch, InvalidEmbedding,  # noqa: E402
                             InvalidEmbeddingInput)
from app.core.logging import setup_logging  # noqa: E402
from app.services.providers.openrouter_common import make_sync_client, translate_openai_error  # noqa: E402

SCHEMA_VERSION = 1
SOURCE_TABLE = "papers_arxiv_legacy"
DEFAULT_OUTPUT_DIR = "outputs/nemotron_embeddings"
_RETRIABLE = ("EmbeddingRateLimited", "EmbeddingTimeout", "EmbeddingUnavailable")
# Percentage-free metadata that may leave the machine: never the full abstract text.
_SAMPLE_ABSTRACT_PREVIEW = 0

# ---------------------------------------------------------------- database (SELECT-only)

_SQL_PING = text("select 1")
_SQL_IDENTITY = text("select current_user")
_SQL_TABLE_EXISTS = text("select to_regclass('public.papers_arxiv_legacy') is not null as present")
_SQL_COLUMNS = text("""
    select column_name, data_type, is_nullable
      from information_schema.columns
     where table_schema = 'public' and table_name = 'papers_arxiv_legacy'
     order by ordinal_position""")
_SQL_PRIVS = text("select has_table_privilege(current_user, 'public.papers_arxiv_legacy', 'SELECT') as can_select")
_SQL_COUNTS = text("""
    select count(*) as total,
           coalesce(sum(case when btrim(summary) <> '' then 1 else 0 end), 0) as with_abstract
      from papers_arxiv_legacy""")
_SQL_SAMPLE_META = text("""
    select id, left(title, 60) as title, field,
           case when published is null then null
                else to_char(published, 'YYYY-MM-DD') end as published,
           length(btrim(summary)) as abstract_len,
           left(md5(btrim(summary)), 12) as abstract_fingerprint
      from papers_arxiv_legacy
     order by id limit :n""")
_SQL_PAGE = text("""
    select id, title, btrim(summary) as summary, field, published, authors, categories, pdf_url
      from papers_arxiv_legacy
     where btrim(summary) <> '' and id > :after
     order by id
     limit :n""")


def _iso(value) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.isoformat()


def _maybe_list(value) -> list | None:
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):  # e.g. SQLite-backed tests serialise arrays as JSON strings
        try:
            v = json.loads(value)
            return v if isinstance(v, list) else [value]
        except (ValueError, TypeError):
            return [value]
    return [value]


class LegacyArchive:
    """Read-only access to `public.papers_arxiv_legacy` through the application's existing engine."""

    REQUIRED_COLUMNS = ("id", "title", "summary")

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def _run(self, stmt, params=None):
        with self._session_factory() as session:  # SELECT-only: closed without commit, nothing persists
            try:
                result = session.execute(stmt, params or {})
                return result
            finally:
                session.rollback()  # belt and braces: the tool can never leave a write behind

    def ping(self) -> bool:
        self._run(_SQL_PING)
        return True

    def identity(self) -> dict:
        return {"db_user": self._run(_SQL_IDENTITY).scalar(), }

    def table_exists(self) -> bool:
        return bool(self._run(_SQL_TABLE_EXISTS).scalar())

    def columns(self) -> list[dict]:
        return [dict(m) for m in self._run(_SQL_COLUMNS).mappings().all()]

    def can_select(self) -> bool:
        return bool(self._run(_SQL_PRIVS).scalar())

    def counts(self) -> dict:
        row = self._run(_SQL_COUNTS).mappings().one()
        return {"total": int(row["total"]), "with_abstract": int(row["with_abstract"])}

    def sample_meta(self, n: int) -> list[dict]:
        return [dict(m) for m in self._run(_SQL_SAMPLE_META, {"n": n}).mappings().all()]

    def verify_columns(self) -> list[str]:
        names = {c["column_name"] for c in self.columns()}
        return [c for c in self.REQUIRED_COLUMNS if c not in names]

    def page(self, after_id: str, limit: int) -> list[dict]:
        rows = self._run(_SQL_PAGE, {"after": after_id, "n": limit}).mappings().all()
        return [{"id": r["id"], "title": r["title"], "summary": r["summary"], "field": r["field"],
                 "published": _iso(r["published"]), "authors": _maybe_list(r["authors"]),
                 "categories": _maybe_list(r["categories"]), "pdf_url": r["pdf_url"]} for r in rows]


def arxiv_id_of(record_id: str) -> str:
    """`http://arxiv.org/abs/0705.3360v1` -> `0705.3360v1`; falls back to the raw id if the shape differs."""
    marker = "/abs/"
    return record_id.split(marker, 1)[1] if marker in record_id else record_id


def content_hash(abstract: str) -> str:
    return hashlib.sha256(abstract.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- OpenRouter embedding client

def validate_vectors(payload_items: list[dict], expected_count: int) -> list[list[float]]:
    """Validate an embeddings response' data entries (already as dicts with 'index'/'embedding').

    Checks completeness and index order (the caller maps vectors back to inputs BY INDEX, so a shuffled
    response cannot silently re-map texts to other papers' vectors), finiteness and non-zero, and returns the
    vectors in input order. The observed dimension is returned implicitly (len of vectors); the caller
    enforces run-level consistency, never the application's column size."""
    if len(payload_items) != expected_count:
        raise InvalidEmbedding(
            f"Embedding provider returned {len(payload_items)} vectors for {expected_count} inputs.")
    ordered = sorted(payload_items, key=lambda d: int(d["index"]))
    if [int(d["index"]) for d in ordered] != list(range(expected_count)):
        raise InvalidEmbedding("Embedding provider returned a broken index sequence.")
    out: list[list[float]] = []
    for d in ordered:
        raw = d.get("embedding")
        if raw is None or not isinstance(raw, (list, tuple)):
            raise InvalidEmbedding("Embedding provider returned no vector for an input.")
        try:
            vec = [float(x) for x in raw]
        except (TypeError, ValueError):  # a non-numeric entry must never become a silent zero in the file
            raise InvalidEmbedding("Embedding contains a non-numeric value.") from None
        if not vec or not all(math.isfinite(x) for x in vec) or not any(vec):
            raise InvalidEmbedding("Embedding contains NaN/infinite values or is all zeros.")
        out.append(vec)
    return out


class OpenRouterVectorClient:
    """Thin, standalone embeddings client: native dimension preserved, bounded backoff, safe errors."""

    def __init__(self, client: openai.OpenAI, model: str, *, dimensions: int | None = None,
                 max_input_chars: int = 24000, attempts: int = 4, sleep=time.sleep,
                 known_min: int | None = None, known_max: int | None = None):
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.client, self.model = client, model
        self.dimensions, self.max_input_chars = dimensions, max_input_chars
        self.attempts, self.sleep = attempts, sleep
        self.known_min, self.known_max = known_min, known_max

    def _body(self, batch: list[str]) -> dict:
        kw: dict = {"model": self.model, "input": batch, "encoding_format": "float"}
        if self.dimensions is not None:
            if self.known_max is not None and not (self.known_min <= self.dimensions <= self.known_max):
                raise ConfigurationError(
                    f"--dimensions {self.dimensions} is outside the documented range "
                    f"{self.known_min}..{self.known_max} for {self.model}.")
            kw["dimensions"] = self.dimensions
        return kw

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed one batch; returns vectors in the SAME ORDER as `texts`. May retry transient errors."""
        for t in texts:
            if not isinstance(t, str) or not t.strip():
                raise InvalidEmbeddingInput()
            if len(t) > self.max_input_chars:
                raise InvalidEmbeddingInput(
                    f"Text is too long ({len(t)} > {self.max_input_chars} characters).")
        last: AppError | None = None
        for attempt in range(self.attempts):
            try:
                resp = self.client.embeddings.create(**self._body(texts))
                items = [{"index": d.index, "embedding": d.embedding} for d in (resp.data or [])]
                return validate_vectors(items, len(texts))
            except openai.OpenAIError as exc:
                mapped = translate_openai_error(exc, "embedding")  # safe, secret-free message
                if type(mapped).__name__ not in _RETRIABLE or attempt == self.attempts - 1:
                    raise mapped from None
                last = mapped
            # bounded exponential backoff: 1s, 2s, 4s, ... (transient workloads only)
            self.sleep(2 ** attempt)
        raise last  # pragma: no cover - attempts >= 1 guarantees a raise above

    def embed_one(self, txt: str) -> list[float]:
        return self.embed_batch([txt])[0]


def make_vector_client(s: Settings, *, dimensions: int | None, timeout: float, attempts: int,
                       sleep=time.sleep, http_client=None) -> OpenRouterVectorClient:
    info = Settings.embedding_info(s.embedding_model)
    return OpenRouterVectorClient(
        make_sync_client(s, timeout=timeout, http_client=http_client), s.embedding_model,
        dimensions=dimensions, max_input_chars=s.embedding_max_input_chars, attempts=attempts, sleep=sleep,
        known_min=(info or {}).get("min"), known_max=(info or {}).get("max"))


# ---------------------------------------------------------------- local JSONL output

class JsonlSink:
    """Incremental JSONL writer with resume indexing (id -> content hash; never leaves a broken state silent)."""

    def __init__(self, path: Path):
        self.path = path
        self.completed: dict[str, str] = {}

    def scan_existing(self) -> int:
        """Load ids+hashes from an existing file. Returns the number of skipped-unparseable trailing bytes."""
        if not self.path.exists():
            return 0
        raw = self.path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raise ConfigurationError(
                f"Output file {self.path} ends in a partial line (interrupted write). Use --fresh to start "
                "a clean export, or remove/repair the file; resume must never trust a truncated record.")
        broken = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                self.completed[str(rec["record_id"])] = str(rec["content_sha256"])
            except (ValueError, KeyError):
                broken += 1  # count but continue: skip lines that are not valid completed records
        return broken

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
        self.completed[record["record_id"]] = record["content_sha256"]


def build_record(row: dict, vector: list[float], *, model: str, dim: int, now: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_table": SOURCE_TABLE,
        "record_id": row["id"],
        "arxiv_id": arxiv_id_of(row["id"]),
        "title": row["title"],
        "abstract": row["summary"],
        "authors": row.get("authors"),
        "categories": row.get("categories"),
        "field": row.get("field"),
        "published": row.get("published"),
        "pdf_url": row.get("pdf_url"),
        "model": model,
        "embedding_dim": dim,
        "embedding": vector,
        "embedded_at": now,
        "content_sha256": content_hash(row["summary"]),
    }


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def guard_no_vm(dim: int, known_min: int | None, known_max: int | None) -> str | None:
    """A note (not an exception): the observed size vs the documented one; nothing is truncated or padded."""
    if known_max is None or dim == known_max:
        return None
    return (f"observed dimension {dim} differs from the documented default {known_max} for this model "
            "(most likely because --dimensions was requested); the native vector is preserved as returned")


def ensure_output_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    gitignore = output_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n!.gitignore\n", encoding="utf-8")  # never commit vectors to version control
    return output_dir


# ---------------------------------------------------------------- commands

def cmd_inspect(archive: LegacyArchive) -> int:
    print("== Nemotron embedding tool :: inspect (read-only) ==")
    archive.ping()
    print(f"db: reachable, connected as {archive.identity()['db_user']}")
    if not archive.table_exists():
        print(f"FAIL: table public.{SOURCE_TABLE} does not exist", file=sys.stderr)
        return 2
    missing = archive.verify_columns()
    print("source table: public.papers_arxiv_legacy is present")
    print("columns:", ", ".join(f"{c['column_name']}:{c['data_type']}" for c in archive.columns()))
    if missing:
        print(f"FAIL: required columns missing: {missing}", file=sys.stderr)
        return 2
    print(f"table readable as current role: {archive.can_select()}")
    counts = archive.counts()
    print(f"records: total={counts['total']} with nonempty abstract={counts['with_abstract']}")
    print("metadata sample (no abstract text printed):")
    for m in archive.sample_meta(5):
        print(f"  - {m['id']} | {m['title']} | field={m['field']} | published={m['published']} "
              f"| abstract_len={m['abstract_len']} | abstract_fingerprint(md5[:12])={m['abstract_fingerprint']}")
    print("note: the legacy table has its own historical 'embedding' VECTOR column (foreign-model provenance); "
          "this tool IGNORES it entirely.")
    return 0


def cmd_sample(archive: LegacyArchive, client: OpenRouterVectorClient, limit: int) -> int:
    print(f"== Nemotron embedding tool :: sample (limit={limit}) ==")
    rows = archive.page("", limit)
    if not rows:
        print("no eligible rows found", file=sys.stderr)
        return 1
    dims: set[int] = set()
    for r in rows:
        vec = client.embed_batch([r["summary"]])[0]
        dims.add(len(vec))
        print(f"embedded {r['id']} -> dim={len(vec)} finite=True sha256={content_hash(r['summary'])[:12]}")
    print(f"observed dimensions: {sorted(dims)} (native response; no truncation/padding)")
    return 0


def run_export(archive: LegacyArchive, client: OpenRouterVectorClient, output_dir: Path, *, batch_size: int,
               limit: int | None, resume: bool = True, fresh: bool = False,
               progress=print, max_records: int | None = None) -> dict:
    """Embed up to `limit` (or all eligible) archive rows into <output_dir>/embeddings.jsonl, incrementally.

    Bounded memory: one page of `batch_size` rows is held at a time (keyset pagination on the stable `id` PK).
    Returns a summary dict; the caller prints it."""
    output_dir = ensure_output_dir(output_dir)
    jsonl_path = output_dir / "embeddings.jsonl"
    if fresh and jsonl_path.exists():
        jsonl_path.unlink()
    sink = JsonlSink(jsonl_path)
    broken_lines = sink.scan_existing() if resume else 0
    if not resume:
        sink.completed = {}
    if not jsonl_path.exists():
        jsonl_path.touch()  # the JSONL file always exists after a run (even an all-failed one: resume state is valid)

    dims = batch_no = embedded = skipped = failed = 0
    failures: list[str] = []
    after = ""
    started_at = now_utc()
    _stop = max_records if max_records is not None else limit

    while _stop is None or embedded + failed < _stop:
        page_limit = batch_size if _stop is None else min(batch_size, _stop - (embedded + failed))
        batch = archive.page(after, page_limit)
        if not batch:
            break
        after = batch[-1]["id"]

        todo = []
        for r in batch:
            if resume and sink.completed.get(r["id"]) == content_hash(r["summary"]):
                skipped += 1
            elif resume and r["id"] in sink.completed:  # same id, changed content -> never overwrite silently
                raise ConfigurationError(
                    f"record {r['id']} already exists in {jsonl_path} with a DIFFERENT content hash. "
                    "Resume cannot overwrite completed records; use --fresh.")
            else:
                todo.append(r)
        if not todo:
            progress(f"  batch: nothing new (all skipped or already completed); scanned id={after}")
            continue

        batch_no += 1
        try:
            vectors = client.embed_batch([r["summary"] for r in todo])
        except (AppError, ValueError) as batch_err:
            # isolate bad/persistent-failure records instead of losing the whole batch:
            progress(f"  batch failed ({type(batch_err).__name__}); retrying record-by-record")
            vectors = []
            for r in list(todo):
                try:
                    vectors.append(client.embed_batch([r["summary"]])[0])
                except (AppError, ValueError) as one_err:
                    progress(f"  record failed: {r['id']} -> {type(one_err).__name__}")
                    failures.append(r["id"])
                    todo.remove(r)
            if not vectors:
                continue

        if dims == 0:
            dims = len(vectors[0])
        elif any(len(v) != dims for v in vectors):
            raise EmbeddingDimensionMismatch(
                f"provider returned mixed vector sizes ({dims} then {len(vectors[0])}); aborting export.")
        stamped = now_utc()
        for r, v in zip(todo, vectors):  # vectors are in the same order as `todo` by construction
            sink.append(build_record(r, v, model=client.model, dim=len(v), now=stamped))
            embedded += 1
        progress(f"  batch {batch_no}: embedded={len(vectors)} skipped_total={skipped} "
                 f"failed_total={len(failures)} last_id={after}")

    summary = {"jsonl": str(jsonl_path), "batches": batch_no, "embedded": embedded, "skipped_already": skipped,
               "broken_lines_ignored": broken_lines, "failed": len(failures), "failed_ids": failures,
               "embedding_dim_observed": dims or None, "model": client.model, "schema_version": SCHEMA_VERSION,
               "started_at": started_at, "finished_at": now_utc()}
    meta = output_dir / "metadata.json"
    meta.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def report_export(summary: dict) -> int:
    print("== export summary ==")
    for k, v in summary.items():
        if k == "failed_ids" and v:
            print(f"  {k}: {', '.join(v[:20])}{' ...' if len(v) > 20 else ''}")
        elif k == "failed":
            print(f"  {k}: {v}")
        else:
            print(f"  {k}: {v}")
    return 0 if not summary["failed"] else 1


def cmd_export(archive: LegacyArchive, client: OpenRouterVectorClient, output_dir: Path, *, batch_size: int,
               limit: int | None, export_all: bool, assume_yes: bool, fresh: bool, resume: bool = True) -> int:
    if not limit and not export_all:
        print("refusing to start: give an explicit --limit N (e.g. --limit 100), or --all for the WHOLE",
              "archive (requires typed confirmation).", file=sys.stderr)
        return 2
    if export_all and not limit:
        full = archive.counts()
        print(f"FULL EXPORT requested: all {full['with_abstract']} eligible records of public.{SOURCE_TABLE}.")
        if not assume_yes:
            answer = input("Type FULL-EXPORT to proceed (anything else aborts): ").strip()
            if answer != "FULL-EXPORT":
                print("aborted by user", file=sys.stderr)
                return 2
    summary = run_export(archive, client, output_dir, batch_size=batch_size, limit=limit,
                         resume=resume, fresh=fresh)
    return report_export(summary)
# ---------------------------------------------------------------- CLI


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Standalone Nemotron embedding utility (read-only DB, local vectors).")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect", help="verify DB + table + columns + counts (metadata only)")
    sp = sub.add_parser("sample", help="embed N records in-memory; report observed dimensions (writes nothing)")
    sp.add_argument("--limit", type=int, default=3)
    ep = sub.add_parser("export", help="embed records and write JSONL vector files locally")
    ep.add_argument("--limit", type=int, help="max records to process this run (recommended)")
    ep.add_argument("--all", dest="all", action="store_true", help="process the WHOLE table (typed confirmation)")
    ep.add_argument("--yes", action="store_true", help="skip the interactive confirmation for --all")
    ep.add_argument("--batch-size", type=int, default=16, help="records per API request (default 16)")
    ep.add_argument("--dimensions", type=int, help="explicit `dimensions` param; DEFAULT: none = native size")
    ep.add_argument("--timeout", type=float, default=None, help="embedding API timeout (default EMBEDDING_TIMEOUT_SECONDS)")
    ep.add_argument("--max-attempts", type=int, default=4, help="transient-error attempts with backoff (default 4)")
    ep.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    ep.add_argument("--fresh", action="store_true", help="start a clean embeddings.jsonl")
    ep.add_argument("--no-resume", action="store_true", help="ignore previously completed records' index")
    a = p.parse_args(argv)
    if a.command == "export" and a.limit is not None and a.limit <= 0:
        p.error("--limit must be >= 1")
    if a.command == "sample" and a.limit <= 0:
        p.error("--limit must be >= 1")
    return a


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level)
    for noisy in ("httpx", "httpx2"):  # request-log noise is not part of the tool's reporting
        logging.getLogger(noisy).setLevel(logging.WARNING)
    archive = LegacyArchive(get_session_factory_late())
    try:
        if args.command == "inspect":
            return cmd_inspect(archive)
        # sample/export need the API key; fail fast with the app's safe configuration error
        settings.require_openrouter_api_key()
        client = make_vector_client(settings, dimensions=args.dimensions if args.command == "export" else None,
                                    timeout=args.timeout if args.command == "export" and args.timeout
                                    else settings.embedding_timeout_seconds,
                                    attempts=args.max_attempts if args.command == "export" else 1,
                                    )
        if args.command == "sample":
            return cmd_sample(archive, client, args.limit)
        return cmd_export(archive, client, Path(args.output_dir), batch_size=args.batch_size, limit=args.limit,
                          export_all=args.all, assume_yes=args.yes, fresh=args.fresh,
                          resume=not args.no_resume)
    except AppError as e:  # ConfigurationError is an AppError subclass: one safe terminal path for all
        print(f"stopped: [{e.code}] {e.message}", file=sys.stderr)
        return 2


def get_session_factory_late():
    from app.db.session import get_session_factory
    return get_session_factory()


if __name__ == "__main__":
    sys.exit(main())
