"""Switch chunk embeddings from the OpenAI text-embedding-3-* era to OpenRouter's
nvidia/nemotron-3.5-embed-1b:free, sized from EMBEDDING_DIMENSIONS.

Old and new vectors live in different vector spaces (even where their dimensions happen to match), so they can never
be reused, converted, truncated or compared. NON-DESTRUCTIVE strategy (same spirit as 0003/0004):

  * chunks table EMPTY                   -> the `embedding` column is re-created as vector(N).
  * rows exist, column already vector(N) -> nothing is rebuilt; embedding_model/embedding_dim are set to NULL wherever
                                            they do not already name the CURRENT model, so every such chunk counts as
                                            "pending re-index" and each vector is overwritten in place by
                                            python -m scripts.reindex_embeddings (incompatible vectors are never
                                            searched in the meantime because pgvector_store only compares chunks
                                            embedded with the CURRENT model and dimension).
  * rows exist, column is vector(M), M != N
      - no `embedding_prev` column yet   -> the current column is renamed to `embedding_prev` (byte-for-byte; its
                                            namesake HNSW index is dropped because it is never searched) and each
                                            row's true model name is copied into `embedding_prev_model`; a NEW
                                            nullable vector(N) `embedding` is added. Chunk ids, texts, page numbers,
                                            sections, paper relations and citations are untouched.
      - `embedding_prev` already exists  -> 0004's preservation slot is taken; the current vectors are first copied
                                            into a fresh `embedding_prev_or` column (same size, per-row model name
                                            recorded in `embedding_prev_or_model`), so a downgrade can restore them
                                            byte-for-byte with their CORRECT model metadata.

The branch taken (and the dimensions seen) is recorded in the small table alembic_0005_embedding_migrate so
downgrade() can undo EXACTLY what upgrade() did, under any later settings. The table is dropped again by downgrade().

N is read from EMBEDDING_DIMENSIONS (default 1024) when the migration runs. The column size must match that setting
for as long as the data is in use; changing it later needs another migration + re-index. N > 2000 exceeds the pgvector
HNSW index limit, so at such N the column is created WITHOUT an index (full-scan cosine search; acceptable for
small/medium corpora) — explicitly, not silently.

Restored `embedding_prev*` vectors keep their TRUE model names, so they can never be searched under the wrong model:
pgvector_store compares only chunks whose (embedding_model, embedding_dim) equal the configured ones.

Between this migration and the end of the re-index, semantic search finds nothing. Run the migration, then
immediately:  python -m scripts.reindex_embeddings

After you have verified retrieval you may reclaim space manually (deliberately NOT done by any migration):
    ALTER TABLE chunks DROP COLUMN embedding_prev, DROP COLUMN embedding_prev_model;          -- "rename" branch
    ALTER TABLE chunks DROP COLUMN embedding_prev_or, DROP COLUMN embedding_prev_or_model;    -- "copy" branch

The whole upgrade/downgrade runs in one PostgreSQL transaction (transactional DDL): it either fully applies or not at
all. Offline mode (`alembic upgrade --sql`) is not supported because the migration inspects the table first.

Revision ID: 0005
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from app.core.config import MAX_INDEXED_DIM, get_settings

revision = "0005"
down_revision = "0004"
OR_COLUMN, OR_MODEL_COLUMN = "embedding_prev_or", "embedding_prev_or_model"
STATE_TABLE = "alembic_0005_embedding_migrate"
_DEFAULT_0004_DIM = 1536  # migration 0004's default EMBEDDING_DIMENSIONS (only a fallback for an unrecorded state)


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


def _hnsw(dim: int) -> None:
    if dim <= MAX_INDEXED_DIM:
        op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
    else:
        op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")  # N>2000 cannot be HNSW-indexed


def _record_branch(bind, branch: str, current_dim: int | None, new_dim: int) -> None:
    op.execute(sa.text(f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} (branch text NOT NULL, "
                       "current_dim integer, new_dim integer NOT NULL)"))
    op.execute(sa.text(f"DELETE FROM {STATE_TABLE}"))
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
        # First preservation slot is free: keep the current vectors byte-for-byte and their exact model names.
        branch = "rename"
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding TO embedding_prev")
        _add_col("embedding_prev_model", sa.Column("embedding_prev_model", sa.String(100)))
        op.execute("UPDATE chunks SET embedding_prev_model = embedding_model")
        op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
    elif not _has_col(bind, OR_COLUMN):
        # 0004's slot is taken (the BGE era already lives in embedding_prev): preserve the current generation
        # under a second column, again with its exact per-row model names, before replacing the live column.
        branch = "copy"
        _add_col(OR_COLUMN, sa.Column(OR_COLUMN, Vector(current_dim), nullable=True))
        op.execute(f"UPDATE chunks SET {OR_COLUMN} = embedding")
        _add_col(OR_MODEL_COLUMN, sa.Column(OR_MODEL_COLUMN, sa.String(100)))
        op.execute(f"UPDATE chunks SET {OR_MODEL_COLUMN} = embedding_model")
        op.drop_column("chunks", "embedding")
        op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
    else:  # a 0005-style state already exists: preserve under OR and remember the true previous size via copy
        branch = "copy"
        _add_col(OR_COLUMN, sa.Column(OR_COLUMN, Vector(current_dim), nullable=True))
        op.execute(f"UPDATE chunks SET {OR_COLUMN} = embedding")
        op.drop_column("chunks", "embedding")
        op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
    _record_branch(bind, branch, current_dim, dim)
    _record_branch(bind, branch, current_dim, dim)
    # Every chunk stored with anything other than the CURRENT model counts as pending re-index. Their (now unusable)
    # vectors stay in place until reindex_embeddings overwrites each one - nothing is lost and no table is wiped.
    op.execute(sa.text("UPDATE chunks SET embedding_model = NULL, embedding_dim = NULL "
                       "WHERE embedding_model IS DISTINCT FROM :m").bindparams(m=s.embedding_model))
    _hnsw(dim)


def downgrade():
    """Undo EXACTLY what upgrade() did, using the branch state it recorded. Preserved vectors return byte-for-byte
    WITH their true model names; OpenRouter vectors are dropped (regenerable from chunk text), so chunks embedded
    after the upgrade become pending."""
    bind = op.get_bind()
    branch, current_dim, new_dim = "metadata", None, _DEFAULT_0004_DIM
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
        # 0005 created embedding_prev_model here (there was none before) and filled it with the TRUE model names.
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
        op.drop_column("chunks", OR_MODEL_COLUMN)  # 0005 added it, so taking it back is safe
    # branch == "metadata": the column and its data were untouched by the upgrade; per-row metadata was nulled and
    # is not reconstructed (conservatively pending; the old vectors are usable again the moment they are re-indexed).
    _hnsw(current_dim or new_dim)  # the restored (or unchanged) column's size decides the index layout
