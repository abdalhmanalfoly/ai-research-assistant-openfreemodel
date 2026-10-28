"""FORWARD REPAIR — resize public.chunks.embedding to the configured dimension, whichever branch DB state now takes.

ROOT CAUSE this repairs (established 2026-10-28 by read-only catalog inspection + this repo's 0008 source):
Alembic stamped `0008` while EMBEDDING_DIMENSIONS was still 1024. 0008's parameters are read at MIGRATION time
(`get_settings().embedding_dim`), the live column was vector(1024), so `current_dim == dim` selected 0008's
`metadata` branch and NO DDL ran — a legitimate outcome of 0008's contract, recorded faithfully:
alembic_0008_embedding_migrate = {branch='metadata', current_dim=1024, new_dim=1024}.
The later reindex pilot then embedded chunks at 2048 (HTTP 200) and failed committing them into the unchanged
vector(1024) column, so the run aborted with zero writes (verify: current=0, pending=211, wrong_dimension=211;
all 211 original nvidia@1024 vectors intact per catalog read).

Because `alembic_version` is already 0008, Alembic will never re-run 0008 — a data version bump can never be
"undone" by touching alembic_version (forbidden anyway). The safe remedy is this NEW revision appended after
0008: on the live DB it takes the `rename` branch below (preservation slot is free — verified: no
embedding_prev* columns exist); on a correctly-migrated fresh install it self-verifies and no-ops.

What this revision does on the audited live state, in ONE transaction:

  * reads the ACTUAL column type from the catalog (never trusts alembic_version or the config alone);
  * drops the HNSW index if the configured size exceeds MAX_INDEXED_DIM (2000+) and keeps it otherwise;
  * observed == configured   -> metadata branch: record + self-verify, no DDL (converged, e.g. fresh installs);
  * observed != configured   -> rename branch: preserve ALL current vectors + their per-row model names
    (embedding column renamed byte-for-byte to embedding_prev, embedding_prev_model populated), then a NEW
    nullable vector(configured) `embedding` column. Chunk ids, texts, page numbers, sections, paper relations,
    ownership and citations are untouched. The fresh column's metadata starts NULL, so every chunk shows as
    pending for `python -m scripts.reindex_embeddings` — nothing is ever relabeled or faked.
  * both preservation slots occupied -> RAISE (never overwrite a generation);
  * afterwards SELF-VERIFIES the catalog again and raises if chunks.embedding is not vector(configured) —
    the exact mistake class this repair exists for cannot pass silently again.

Downgrade: reverses the recorded branch by renaming embedding_prev back (1024 vectors + true model names) or,
for the metadata branch, does nothing but drop self-state data. State table: alembic_0009_embedding_repair.

Revision ID: 0009
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from app.core.config import MAX_INDEXED_DIM, get_settings

revision = "0009"
down_revision = "0008"

OR_COLUMN, OR_MODEL_COLUMN = "embedding_prev_or", "embedding_prev_or_model"
STATE_TABLE = "alembic_0009_embedding_repair"


def _bind():
    return op.get_bind()


def _has_col(bind, name):
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name='chunks' AND column_name=:c"), {"c": name}).scalar())


def _embedding_dim(bind):
    """ACTUAL size of chunks.embedding read from the catalog (None when the column is missing/other type)."""
    typ = bind.execute(sa.text(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid='public.chunks'::regclass AND attname='embedding'")).scalar()
    if not typ or not typ.startswith("vector("):
        return None
    return int(typ.split("(", 1)[1].rstrip(")"))


def _occupation(bind):
    """Which preservation generations already exist (rename-then-copy like 0008/0005: never overwrite)."""
    return _has_col(bind, "embedding_prev"), _has_col(bind, OR_COLUMN)


def _record(bind, branch, observed, target):
    op.execute(sa.text(f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} (branch text NOT NULL, observed_dim integer, "
                       "target_dim integer NOT NULL, verified boolean NOT NULL DEFAULT false)"))
    prior = bind.execute(sa.text(f"SELECT branch FROM {STATE_TABLE} LIMIT 1")).scalar()
    if prior is not None:
        raise RuntimeError(f"{STATE_TABLE} already records branch '{prior}'; 0009 must not run twice.")
    op.execute(sa.text(f"INSERT INTO {STATE_TABLE} (branch, observed_dim, target_dim) VALUES (:b, :o, :t)")
               .bindparams(b=branch, o=observed, t=target))


def _mark_verified(bind):
    op.execute(sa.text(f"UPDATE {STATE_TABLE} SET verified = true"))


def upgrade():
    s = get_settings()
    dim = s.embedding_dim
    if not 1 <= dim <= 4096:
        raise RuntimeError(f"EMBEDDING_DIMENSIONS={dim} is outside 1..4096")
    bind = _bind()
    observed = _embedding_dim(bind)
    rows = bind.execute(sa.text("SELECT count(*) FROM chunks")).scalar() or 0
    if rows and observed is None:
        raise RuntimeError("chunks has rows but its embedding column is not a vector type; inspect manually.")
    if observed is None:
        raise RuntimeError("chunks.embedding is missing entirely; 0009 cannot infer the intended shape.")

    # HNSW housekeeping FIRST (applies to every branch): impossible above 2000, obsolete below.
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")

    if observed == dim:
        branch = "metadata"  # already the requested size: record + self-verify only, no DDL
        print(f"0009 repair: chunks.embedding is already vector({observed}) = configured size; "
              "no DDL, recording convergence.")
    else:
        prev, prev_or = _occupation(bind)
        if prev and prev_or:
            raise RuntimeError("chunks has BOTH embedding_prev and embedding_prev_or; refusing to overwrite "
                               "either preserved generation. Inspect/reclaim slots manually first.")
        branch = "rename"
        # Vectors preserved byte-for-byte; per-row model names travel with them (they describe THESE vectors).
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding TO embedding_prev")
        if not _has_col(bind, "embedding_prev_model"):
            op.add_column("chunks", sa.Column("embedding_prev_model", sa.String(100)))
        op.execute("UPDATE chunks SET embedding_prev_model = embedding_model")
        op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
        if dim > MAX_INDEXED_DIM:
            print(f"WARNING: EMBEDDING_DIMENSIONS={dim} exceeds the pgvector HNSW limit ({MAX_INDEXED_DIM}); "
                  "chunks.embedding is created WITHOUT an index (full-scan cosine search).")
    _record(bind, branch, observed, dim)

    # A no-op guard on the invalidation step (0008's): rows whose CURRENT metadata could describe the NEW
    # column are none — after the rename the fresh column has no vectors at all, so nothing may claim to be
    # indexed with the current generation. Nulling only rows whose model mismatches (0008 semantics) is NOT
    # sufficient here: model matches but vectors now live elsewhere. Be explicit, honestly pending:
    if branch == "rename":
        op.execute("UPDATE chunks SET embedding_model = NULL, embedding_dim = NULL")
    if dim <= MAX_INDEXED_DIM:
        op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")

    # SELF-VERIFICATION — the anti-regression invariant: the catalog MUST now report exactly vector(configured).
    final = _embedding_dim(bind)
    if final != dim:
        raise RuntimeError(f"0009 self-check FAILED: chunks.embedding is vector({final}), expected vector({dim}). "
                           "The transaction will roll back; inspect manually.")
    _mark_verified(bind)


def downgrade():
    """Undo exactly the recorded branch: rename the preserved vectors back under `embedding` with their true
    model names (per-row), or (metadata branch) leave column+data untouched."""
    bind = _bind()
    branch = observed = target = None
    has_state = bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:t"),
        {"t": STATE_TABLE}).scalar())
    if has_state:
        row = bind.execute(sa.text(f"SELECT branch, observed_dim, target_dim FROM {STATE_TABLE} LIMIT 1")).first()
        if row:
            branch, observed, target = row
    op.execute(f"DROP TABLE IF EXISTS {STATE_TABLE}")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    if branch == "rename":
        op.drop_column("chunks", "embedding")
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding_prev TO embedding")
        op.execute("UPDATE chunks SET embedding_model = CASE WHEN embedding IS NULL THEN NULL "
                   "ELSE COALESCE(embedding_prev_model, embedding_model) END")
        op.execute("UPDATE chunks SET embedding_dim = CASE WHEN embedding IS NULL THEN NULL "
                   "ELSE vector_dims(embedding) END")
        if _has_col(bind, "embedding_prev_model"):
            op.drop_column("chunks", "embedding_prev_model")
        observed = observed or 1024
    if branch in (None, "metadata") or (observed or 0) <= MAX_INDEXED_DIM:
        # unknown/metadata state: only recreate the index when the column size demonstrably allows HNSW
        n = _embedding_dim(bind)
        if n is not None and n <= MAX_INDEXED_DIM:
            op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
