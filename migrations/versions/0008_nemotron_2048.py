"""PROPOSED (requires explicit authorization; not yet executed) — resize chunk embeddings to 2048-d for
nvidia/nemotron-3.5-embed-1b:free, read from EMBEDDING_DIMENSIONS at migration time (must be set to 2048).

MODELLING NOTE: this revision chains from `0007`, which is the head recorded in the LIVE database's
`alembic_version`. The 0006/0007 FILES live in the teammate's repository and must be present in
migrations/versions/ BEFORE this revision can run locally (`alembic upgrade head` needs the whole graph).
This file does not reconstruct or rewrite that history; it appends after it.

Why 0008 exists despite 0005: 0005 already ran on the live database at EMBEDDING_DIMENSIONS=1024 and its
branches were recorded in alembic_0005_embedding_migrate; Alembic migrations run once per database. Re-sizing
later requires a NEW revision (same non-destructive pattern, fresh state table).

Strategy (same spirit as 0003/0004/0005 — preservation before replacement, one transaction):

  * chunks EMPTY                       -> rebuild `embedding` in place as vector(N).
  * column already vector(N)           -> no DDL; only per-row re-index bookkeeping (metadata branch).
  * rows exist, no `embedding_prev`    -> RENAME branch: current vectors + index dropped-then-kept? NO — the
                                         column is renamed to `embedding_prev` byte-for-byte (its HNSW index
                                         is dropped first; never searched again) and each row's CURRENT model
                                         name is copied into `embedding_prev_model`; a NEW nullable vector(N)
                                         `embedding` is added. Chunk ids, texts, page numbers, sections,
                                         paper relations, ownership and citations are untouched.
  * `embedding_prev` already exists    -> COPY branch: preserve the current generation under a fresh
                                         `embedding_prev_or` (+ per-row model name in `embedding_prev_or_model`)
                                         before replacing the live column.

The branch taken and dimensions seen are recorded in alembic_0008_embedding_migrate; downgrade() reverses EXACTLY
what upgrade() did and drops the table again.

At N = 2048 the pgvector HNSW index limit (MAX_INDEXED_DIM = 2000) is exceeded: `ix_chunks_embedding_hnsw` is
dropped and the column is created WITHOUT an index (full-scan cosine search; fine for small/medium corpora,
explicitly NOT silently: a warning is printed).

After upgrade(), every chunk whose metadata does not name the CURRENT model+dimension counts as pending; their
old vectors are preserved (embedding_prev / embedding_prev_or) until `python -m scripts.reindex_embeddings`
overwrites `embedding` row by row from the stored chunk text. Between this migration and the end of the re-index
semantic search finds nothing (pgvector_store only compares chunks with matching model+dimension metadata).

Revision ID: 0008
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from app.core.config import MAX_INDEXED_DIM, get_settings

revision = "0008"
down_revision = "0007"  # recorded head of the live database (teammate's 0006/0007 files must be present to run)

OR_COLUMN, OR_MODEL_COLUMN = "embedding_prev_or", "embedding_prev_or_model"
STATE_TABLE = "alembic_0008_embedding_migrate"
_DEFAULT_PREV_DIM = 1024  # the size 0008 finds today (recorded per branch for a correct downgrade anyway)


def _has_col(bind, name: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name='chunks' AND column_name=:c"), {"c": name}).scalar())


def _add_col(name: str, col) -> None:
    if not _has_col(op.get_bind(), name):
        op.add_column("chunks", col)


def _embedding_dim(bind) -> int | None:
    """Actual size of chunks.embedding's vector column (None when the column is missing)."""
    typ = bind.execute(sa.text(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid='public.chunks'::regclass AND attname='embedding'")).scalar()
    if not typ or not typ.startswith("vector("):
        return None
    return int(typ.split("(", 1)[1].rstrip(")"))


def _record_branch(bind, branch: str, current_dim: int | None, new_dim: int) -> None:
    op.execute(sa.text(f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} (branch text NOT NULL, "
                       "current_dim integer, new_dim integer NOT NULL)"))
    # Refuse to clobber previously recorded state: a non-empty table means THIS migration is in an unexpected
    # partial/re-run state and silently replacing the branch record would break the downgrade's fidelity.
    prior = bind.execute(sa.text(f"SELECT branch FROM {STATE_TABLE} LIMIT 1")).scalar()
    if prior is not None:
        raise RuntimeError(f"{STATE_TABLE} already records branch '{prior}'; 0008 appears to have already run "
                           "(or is mid-flight). Inspect before proceeding.")
    op.execute(sa.text(f"INSERT INTO {STATE_TABLE} (branch, current_dim, new_dim) VALUES (:b, :cd, :nd)")
               .bindparams(b=branch, cd=current_dim, nd=new_dim))


def upgrade():
    s = get_settings()
    dim = s.embedding_dim
    if not 1 <= dim <= 4096:
        raise RuntimeError(f"EMBEDDING_DIMENSIONS={dim} is outside 1..4096")
    if dim > MAX_INDEXED_DIM:
        print(f"WARNING: EMBEDDING_DIMENSIONS={dim} exceeds the pgvector HNSW limit ({MAX_INDEXED_DIM}); creating "
              "chunks.embedding WITHOUT an index (full-scan cosine search).")
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT count(*) FROM chunks")).scalar() or 0
    current_dim = _embedding_dim(bind)
    if rows and current_dim is None:
        raise RuntimeError("chunks has rows but no embedding column; inspect the schema manually.")

    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    if current_dim == dim:
        branch = "metadata"  # same column size: no DDL, only per-row re-index bookkeeping below
    elif not rows:
        branch = "empty"
        op.drop_column("chunks", "embedding")
        op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
    elif not _has_col(bind, "embedding_prev"):
        branch = "rename"  # first preservation slot is free: keep the current vectors byte-for-byte
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding TO embedding_prev")
        _add_col("embedding_prev_model", sa.Column("embedding_prev_model", sa.String(100)))
        op.execute("UPDATE chunks SET embedding_prev_model = embedding_model")
        op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
    elif not _has_col(bind, OR_COLUMN):
        branch = "copy"  # preserve the current generation under a second column before replacing the live one
        _add_col(OR_COLUMN, sa.Column(OR_COLUMN, Vector(current_dim), nullable=True))
        op.execute(f"UPDATE chunks SET {OR_COLUMN} = embedding")
        _add_col(OR_MODEL_COLUMN, sa.Column(OR_MODEL_COLUMN, sa.String(100)))
        op.execute(f"UPDATE chunks SET {OR_MODEL_COLUMN} = embedding_model")
        op.drop_column("chunks", "embedding")
        op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
    else:
        # BOTH preservation slots are occupied: this is an anomalous state (e.g. a partially-applied foreign
        # variant). 0005 tolerated overwriting embedding_prev_or here; overwriting a preserved generation is
        # data-destroying, so fail instead (the DB rolls its transaction back untouched).
        raise RuntimeError("chunks already has BOTH embedding_prev and embedding_prev_or; the schema is in an "
                           "unexpected preservation state and 0008 refuses to overwrite either generation. "
                           "Inspect the table manually (or reclaim slots with an explicit DROP COLUMN backup).")
    _record_branch(bind, branch, current_dim, dim)
    # Every chunk stored with anything other than the CURRENT model counts as pending re-index; their unusable
    # vectors stay preserved until reindex_embeddings overwrites `embedding` — nothing is lost, nothing wiped.
    op.execute(sa.text("UPDATE chunks SET embedding_model = NULL, embedding_dim = NULL "
                       "WHERE embedding_model IS DISTINCT FROM :m").bindparams(m=s.embedding_model))
    if dim <= MAX_INDEXED_DIM:
        op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
    # else: intentionally left WITHOUT an index (documented above + warning printed)


def downgrade():
    """Undo EXACTLY what upgrade() did, using the branch state it recorded; vectors come back byte-for-byte with
    their true model names whenever a preserved column holds them. Post-upgrade 2048-d vectors are regenerable
    from chunk text (reindex), so dropping them in the rollback is data-loss-free."""
    bind = op.get_bind()
    branch, current_dim, new_dim = "metadata", None, _DEFAULT_PREV_DIM
    if bind.execute(sa.text("SELECT count(*) FROM information_schema.tables WHERE table_name=:t"),
                    {"t": STATE_TABLE}).scalar():
        try:
            branch, current_dim, new_dim = bind.execute(
                sa.text(f"SELECT branch, current_dim, new_dim FROM {STATE_TABLE}")).one()
        except Exception:
            pass
    op.execute(f"DROP TABLE IF EXISTS {STATE_TABLE}")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    if branch == "empty":
        op.drop_column("chunks", "embedding")
        op.add_column("chunks", sa.Column("embedding", Vector(current_dim or new_dim), nullable=True))
        op.execute("UPDATE chunks SET embedding_model = NULL, embedding_dim = NULL")
    elif branch == "rename":
        op.drop_column("chunks", "embedding")
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding_prev TO embedding")
        op.execute("UPDATE chunks SET embedding_model = CASE WHEN embedding IS NULL THEN NULL "
                   "ELSE COALESCE(embedding_prev_model, embedding_model) END")
        op.execute("UPDATE chunks SET embedding_dim = CASE WHEN embedding IS NULL THEN NULL "
                   "ELSE vector_dims(embedding) END")
        if _has_col(bind, "embedding_prev_model"):
            op.drop_column("chunks", "embedding_prev_model")
    elif branch == "copy":
        op.drop_column("chunks", "embedding")
        op.execute(f"ALTER TABLE chunks RENAME COLUMN {OR_COLUMN} TO embedding")
        op.execute(f"UPDATE chunks SET embedding_model = CASE WHEN embedding IS NULL THEN NULL "
                   f"ELSE {OR_MODEL_COLUMN} END")
        op.execute("UPDATE chunks SET embedding_dim = CASE WHEN embedding IS NULL THEN NULL "
                   "ELSE vector_dims(embedding) END")
        if _has_col(bind, OR_MODEL_COLUMN):
            op.drop_column("chunks", OR_MODEL_COLUMN)
    # branch == "metadata": column and data untouched; per-row metadata was nulled and stays conservatively
    # pending — the moment the configured model/dimension match again they are searchable after one re-index.
    if (current_dim or new_dim) <= MAX_INDEXED_DIM:
        op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
