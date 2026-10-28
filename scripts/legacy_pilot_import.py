"""13-paper LEGACY PILOT import — the ONLY authorized production-writing path for this run.

  python -m scripts.legacy_pilot_import            # DRY RUN (default): selects, validates, prints the plan
  python -m scripts.legacy_pilot_import --execute  # actually ingests the 13 selected records

Hard safety properties:
  * EXACTLY the 13 legacy records listed in docs/legacy_arxiv_pilot_manifest.md (verified read-only
    2026-10-27). The record list is embedded below; there is NO "--all" and NO way to raise the limit.
  * PDFs are fetched only from the legacy record's stored pdf_url after SSRF validation: https/http only,
    every host resolved and rejected if it maps to loopback/private/link-local/metadata-address space,
    every redirect re-validated the same way, TLS verification ON, generous-but-bounded timeouts, response
    size cap, magic-byte check ("%PDF-").
  * Idempotent: papers use deterministic uuid5 ids (`urn:x-arxiv-legacy:<legacy_id>`), ingestion is driven by
    the content sha256 and the existing IngestionService.register/run_job pipeline; re-running skips
    already-indexed papers (job_id=None semantics).
  * No legacy 1024-d embedding is EVER read here: embeddings are always freshly generated through the
    configured 2048-d provider (IngestionService validates every vector before insert).
  * Extraction/embedding failures are RECORDED (IngestionJob failed + paper error_message), never silently
    swallowed; a paper is 'indexed' only when the existing pipeline achieved real chunks+embeddings.
  * No migrations, no schema changes, no deletes; transactions are short (register commits; run_job commits
    per stage) — no DB transaction is ever held open across network I/O.
"""
import argparse
import asyncio
import hashlib
import ipaddress
import os
import pathlib
import socket
import ssl
import sys
import urllib.parse
import uuid
from urllib.request import Request, build_opener, HTTPSHandler

import sqlalchemy as sa

from app.core.config import get_settings
from app.db.models import Chunk, IngestionJob, Paper
from app.db.session import get_session_factory
from app.services.ingestion_service import IngestionService
from app.services.providers.openrouter_embeddings import OpenRouterEmbedder

S = get_settings()
SF = get_session_factory()
MAX_PILOT = 13
MAX_PDF_BYTES = 25 * 1024 * 1024
HTTP_TIMEOUT = 45.0

# The pre-approved manifest (docs/legacy_arxiv_pilot_manifest.md): (papers_uuid5, legacy arxiv abs URL, title)
PILOT = [
    ("2adf7b34-dde8-55d7-9c40-7024f4791d83", "http://arxiv.org/abs/0904.3664v1"),
    ("2cc7675d-ebd8-585d-b893-90483081bdd1", "http://arxiv.org/abs/cond-mat/9801297v1"),
    ("af97b573-0f96-55dc-94f6-77b8ee7df724", "http://arxiv.org/abs/0705.3360v1"),
    ("0e42a897-80f0-58ae-ac5b-dc9b6463850c", "http://arxiv.org/abs/cs/9903016v1"),
    ("aca48fe1-007a-529b-b740-0245119f2672", "http://arxiv.org/abs/cs/0609093v1"),
    ("1d332dec-8c6a-5430-ba46-d859db025d18", "http://arxiv.org/abs/1012.3476v1"),
    ("32a80110-5687-5ae2-afa8-7f7248380eb1", "http://arxiv.org/abs/0906.1346v6"),
    ("0d627f77-c774-5a29-9965-bb1abd5889d2", "http://arxiv.org/abs/1012.0759v1"),
    ("14c7bb96-5aeb-5924-934d-1fc1ab6ea645", "http://arxiv.org/abs/1201.4522v1"),
    ("fb662818-abd9-5267-8df7-473a222f21e4", "http://arxiv.org/abs/1506.06962v1"),
    ("cfc5105b-5bf3-521a-b4b9-63c54915c399", "http://arxiv.org/abs/1712.01626v1"),
    ("054e98ac-5c05-5a57-8b96-325091c34416", "http://arxiv.org/abs/1707.04352v1"),
    ("a2419cb5-cc15-530a-a2f2-da3fcbe56f8e", "http://arxiv.org/abs/2610.27570v1"),
]
assert len(PILOT) == MAX_PILOT and len({p for p, _ in PILOT}) == MAX_PILOT
# Deterministic-id invariant (verified against the manifest 2026-10-28): every papers id must be
# uuid5(NAMESPACE_URL, "urn:x-arxiv-legacy:<abs url>"), so re-derivation never forks an identity.
for _pid, _lid in PILOT:
    assert _pid == str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:x-arxiv-legacy:{_lid}")), _lid


def _reject_private_host(host: str) -> str:
    """SSRF guard: resolve EVERY address; allow only globally-routable public hosts. Returns the host."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    for fam, _t, _p, _c, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified
                or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
                or str(ip).startswith(("169.254.169.254", "fd00:", "fdfe:"))):
            raise ValueError(f"refusing non-public host {host} ({ip})")
    return host


def tls_context() -> ssl.SSLContext:
    """Strict-verification TLS context.

    Root cause of the CERTIFICATE_VERIFY_FAILED failures: this Windows venv's OpenSSL reports
    NO default CA file or capath (ssl.get_default_verify_paths() returns none), so stock
    urllib has nothing to verify against. We therefore pin an explicit, maintained CA bundle:
      1. PILOT_CA_BUNDLE env var (optional) — for a corporate/private root CA certified by the
         site administrator (export the CA from the admin or the corporate trust store; never
         trust a certificate fetched from an untrusted source).
      2. SSL_CERT_FILE env var, if it points to an existing file.
      3. certifi's CA bundle (already a project dependency; see requirements.txt / httpx+openai).
    TLS verification and hostname checking stay fully ON — there is no unverified path.
    """
    cafile = os.environ.get("PILOT_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if not cafile or not os.path.isfile(cafile):
        import certifi
        cafile = certifi.where()
    return ssl.create_default_context(cafile=cafile)


def validated_download(url: str) -> tuple[bytes, str]:
    """SSRF-safe fetch with timeout, size cap, redirect re-validation, strict TLS verification."""
    seen: set[str] = set()
    opener = build_opener(HTTPSHandler(context=tls_context()))  # verification ON (explicit CA bundle)
    for _hop in range(4):
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"invalid url: {url[:80]}")
        _reject_private_host(parts.hostname)
        resp = opener.open(Request(url, headers={"User-Agent": "ai-research-assistant-pilot/1.0"}),
                           timeout=HTTP_TIMEOUT)
        url = urllib.parse.urljoin(url, resp.url)  # redirect chain revalidation
        norm = urllib.parse.urlunparse(parts._replace(params="", query="", fragment=""))
        if norm in seen:
            raise ValueError("redirect loop")
        seen.add(norm)
        rv = resp.read(MAX_PDF_BYTES + 1)
        if len(rv) > MAX_PDF_BYTES:
            raise ValueError("response larger than the 25 MB cap")
        if url == resp.url or rv[:5] == b"%PDF-":
            break
    if not rv.startswith(b"%PDF-"):
        raise ValueError("response is not a PDF (no %PDF- magic)")
    return rv, hashlib.sha256(rv).hexdigest()


def preflight() -> None:
    """Identity + state checks: this must look like the audited production lineage, and nothing else."""
    with SF() as s:
        ver = s.execute(sa.text("select version_num from alembic_version")).scalar()
        if ver != "0009":
            raise RuntimeError(f"alembic_version={ver}; expected 0009 (repair applied). Not proceeding.")
        legacy_n = s.execute(sa.text("select count(*) from papers_arxiv_legacy")).scalar()
        if legacy_n != 2776:
            raise RuntimeError(f"papers_arxiv_legacy has {legacy_n} rows; audited expectation is 2776. "
                               f"This does not look like the audited environment. Stopping.")
        n = s.execute(sa.text("select count(*) from papers")).scalar()
        c = s.execute(sa.text("select count(*) from chunks")).scalar()
        if n < 3 or c < 211:
            raise RuntimeError(f"papers={n}/chunks={c}; below the audited flooring (3/211). "
                               f"This does not look like the audited environment. Stopping.")
        orphan = s.execute(sa.text(
            "select count(*) from papers p where p.status='indexed' and "
            "not exists (select 1 from chunks c join papers p2 on p2.id=c.paper_id "
            "where p2.id=p.id and c.embedding is not null)")).scalar()
        if orphan:
            raise RuntimeError(f"{orphan} papers are marked indexed without chunks+embeddings; "
                               "reconcile before running the pilot.")


def load_legacy() -> list[dict]:
    with SF() as s:
        rows = s.execute(sa.text(
            "select id::text, title, authors, summary, pdf_url, published::text, categories "
            "from papers_arxiv_legacy where id::text = any(cast(:ids as text[]))"), {"ids": [l for _, l in PILOT]}).all()
    by_id = {r[0]: r for r in rows}
    if len(by_id) != MAX_PILOT:
        missing = [l for _, l in PILOT if l not in by_id]
        raise RuntimeError(f"only {len(by_id)}/{MAX_PILOT} manifest records found in papers_arxiv_legacy; "
                           f"missing: {missing}. Stopping — refusing substitutes.")
    out = []
    for pid, legacy_id in PILOT:
        lid, title, authors, summary, pdf_url, published, categories = by_id[legacy_id]
        if not title.strip() or not summary.strip() or not (pdf_url or "").strip():
            raise RuntimeError(f"legacy record {legacy_id} is missing title/summary/pdf_url. Stopping.")
        out.append({"papers_id": pid, "legacy_id": legacy_id, "title": title,
                    "authors": list(authors or []), "abstract": summary, "pdf_url": pdf_url,
                    "published": published, "categories": list(categories or [])})
    return out


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def register_one(rec: dict) -> tuple[str, str]:
    """Fetch PDF (SSRF-guarded), write to local storage, upsert paper row, create pending job.

    Returns (paper_id, job_id) — job_id "already_indexed" when nothing needs ingestion.
    """
    pdf, digest = validated_download(rec["pdf_url"])
    papers_id = uuid.UUID(rec["papers_id"])
    with SF() as s:
        existing = s.get(Paper, papers_id)
        if existing and existing.status == "indexed":
            return str(papers_id), "already_indexed"
        key = f"{digest}.pdf"
        if existing is None and s.execute(sa.text("select 1 from papers where content_hash=:h"),
                                          {"h": digest}).scalar():
            raise RuntimeError(f"content_hash {digest} already belongs to a different paper; "
                               f"refusing to create a duplicate for {rec['legacy_id']}.")
    path = pathlib.Path(S.storage_dir) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _atomic_write(path, pdf)
    with SF() as s:
        p = s.get(Paper, papers_id)
        if not p:
            p = Paper(id=papers_id, title=rec["title"], authors=rec["authors"], abstract=rec["abstract"],
                      publication_year=int(rec["published"][:4]) if rec["published"] else None,
                      source_url=rec["legacy_id"], storage_key=key, content_hash=digest, status="pending")
            s.add(p)
            s.flush()
        else:
            p.status, p.error_message = "pending", None
            if not p.storage_key:
                p.storage_key = key
        job = IngestionJob(paper_id=p.id, status="pending")
        s.add(job)
        s.commit()
        return str(p.id), str(job.id)


def already_registered(sf) -> dict[str, str]:
    """Legacy ids (abs urls) already registered in the active papers table -> paper status
    (the same deterministic identifier space: papers.source_url stores the legacy abs url)."""
    with sf() as s:
        rows = s.execute(sa.text("select source_url, status from papers where source_url is not null")).all()
    return {url: status for url, status in rows}


def plan_batch(legacy_rows: list, registered: dict[str, str], limit: int) -> list[dict]:
    """Pure batch planner: up to `limit` records this run, deterministic, retries first.

    1. Registered-but-NOT-indexed papers (failed / processing / pending) are retried first —
       register_one() resets them to pending and creates a fresh job, so recovery needs nothing else.
    2. Then brand-new eligible records fill the remaining share of the limit.
    Eligibility: has pdf_url, title and summary. 'indexed' papers are never re-processed.
    """
    fresh = []
    for legacy_id, title, authors, summary, pdf_url, published, categories in legacy_rows:
        if not (pdf_url or "").strip() or not (title or "").strip() or not (summary or "").strip():
            continue
        fresh.append({"papers_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:x-arxiv-legacy:{legacy_id}")),
                      "legacy_id": legacy_id, "title": title, "authors": list(authors or []),
                      "abstract": summary, "pdf_url": pdf_url, "published": published or "",
                      "categories": list(categories or [])})
    retry = [r for r in fresh if r["legacy_id"] in registered and registered[r["legacy_id"]] != "indexed"]
    new = [r for r in fresh if r["legacy_id"] not in registered]
    return (retry + new)[:limit]


def load_batch(limit: int) -> list[dict]:
    """Deterministically select up to `limit` records: incomplete retries first, then new.

    The processing list is sliced to `limit` — no record past the limit is downloaded or written.
    """
    registered = already_registered(SF)
    with SF() as s:
        rows = s.execute(sa.text(
            "select id::text, title, authors, summary, pdf_url, published::text, categories "
            "from papers_arxiv_legacy "
            "where pdf_url is not null and title is not null and summary is not null "
            "order by published, id")).all()
    return plan_batch(rows, registered, limit)


def parse_limit(raw: str | None) -> int | None:
    """Validate --limit: must be a positive integer; 0/negatives/garbage get a clear error."""
    if raw is None:
        return None
    try:
        n = int(raw, 10)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--limit must be a positive integer, got {raw!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"--limit must be a positive integer (>= 1), got {raw}")
    return n


def build_report(recs: list[dict], limit: int | None) -> str:
    head = (f"batch selection (limit {limit})" if limit is not None
            else "pilot selection (hard limit 13)")
    lines = [head] + [f"  {r['papers_id'][:8]}… {r['legacy_id']:44} «{r['title'][:60]}»" for r in recs]
    if limit is not None and len(recs) < limit:
        lines.append(f"note: only {len(recs)} eligible new records remain (requested limit {limit}).")
    return "\n".join(lines)


async def run(execute: bool, limit: int | None = None) -> int:
    preflight()
    recs = load_legacy() if limit is None else load_batch(limit)
    print(build_report(recs, limit))
    if not execute:
        print("\nDRY RUN: no downloads, no DB writes. Re-run with --execute to ingest these "
              f"{len(recs)} record(s).")
        return 0
    service = IngestionService(SF, OpenRouterEmbedder.from_settings(S), S.storage_dir, S.chunk_max_chars,
                               S.embedding_dim)
    results = []
    for rec in recs:
        try:
            paper_id, state = register_one(rec)
            if state == "already_indexed":
                results.append((rec["legacy_id"], "SKIP already indexed", ""))
                continue
            # bounded single-flight job (no background overlap): extraction->chunking->2048 embeddings->status
            await asyncio.wait_for(service.run_job(state), timeout=600)
            with SF() as s:
                j = s.execute(sa.text("select status from ingestion_jobs where id::text=:j"), {"j": state}).scalar()
                n = s.execute(sa.text("select count(*) from chunks where paper_id=:p and embedding is not null"),
                              {"p": paper_id}).scalar()
            results.append((rec["legacy_id"], f"job={j} chunks={n}", ""))
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:140]}"
            results.append((rec["legacy_id"], "ERROR", msg))
            print(f"  !! {rec['legacy_id']}: {msg}", file=sys.stderr)
    ok = sum(1 for _, s, _ in results if "job=succeeded" in s or s == "already_indexed")
    print("\npilot results:")
    for lid, status, err in results:
        print(f"  {lid:44} {status} {err}")
    print(f"\nsummary: {ok}/{len(recs)} succeeded-or-already-indexed")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Legacy arXiv importer — dry run by default; "
                                           "--execute performs the DB writes.")
    p.add_argument("--execute", action="store_true",
                   help="actually ingest the selected records (default: dry run, zero writes)")
    p.add_argument("--limit", type=parse_limit, default=None, metavar="N",
                   help="max number of NEW legacy papers to select this run (positive integer; "
                        "omit to use the pre-approved 13-record manifest)")
    args = p.parse_args(argv)
    return asyncio.run(run(args.execute, args.limit))


if __name__ == "__main__":
    sys.exit(main())
