"""Switch chunk embeddings from BGE (vector(1024)) to OpenAI text-embedding-3-* (vector(EMBEDDING_DIMENSIONS)).

OpenAI and BGE vectors live in different vector spaces, so old vectors can never be reused or converted:
they are NEVER truncated, padded or compared with new vectors. NON-DESTRUCTIVE strategy (same spirit as 0003):

  * chunks table EMPTY      -> replace the `embedding` column with a vector(N) one.
  * chunks table has rows   -> rename `embedding` to `embedding_prev` (data kept, its HNSW index dropped because it is
                               never searched), remember the old model name in `embedding_prev_model`, add a NEW
                               nullable vector(N) `embedding` and set embedding_model/embedding_dim to NULL, so every
                               chunk counts as "pending re-index". Chunk ids, text, page numbers, sections, paper
                               relations and citations are untouched.

N is read from EMBEDDING_DIMENSIONS (default 1536) when the migration runs. The column size must match that setting
for as long as the data is in use; changing it later needs another migration + re-index.

Between this migration and the end of the re-index, semantic search finds nothing (no chunk has a comparable
vector). Run the migration, then immediately:  python -m scripts.reindex_embeddings

After you have verified retrieval you may reclaim space manually (deliberately NOT done by any migration):
    ALTER TABLE chunks DROP COLUMN embedding_prev, DROP COLUMN embedding_prev_model;

The whole upgrade/downgrade runs in one PostgreSQL transaction (transactional DDL): it either fully applies or not at all.
Offline mode (`alembic upgrade --sql`) is not supported because the migration inspects the table first.

Revision ID: 0004
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from app.core.config import MAX_INDEXED_DIM, get_settings

revision = "0004"
down_revision = "0003"
PREVIOUS_DIM = 1024  # BGE schema created by 0003


def _has_col(bind, name: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name='chunks' AND column_name=:c"), {"c": name}).scalar())


def upgrade():
    dim = get_settings().embedding_dim
    if not 1 <= dim <= MAX_INDEXED_DIM:
        raise RuntimeError(f"EMBEDDING_DIMENSIONS={dim} is outside 1..{MAX_INDEXED_DIM} (pgvector HNSW limit)")
    bind = op.get_bind()
    if _has_col(bind, "embedding_prev"):
        raise RuntimeError("chunks.embedding_prev already exists; refusing to overwrite it. Inspect the schema manually.")
    rows = bind.execute(sa.text("SELECT count(*) FROM chunks")).scalar() or 0

    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    if rows == 0:
        op.drop_column("chunks", "embedding")
    else:
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding TO embedding_prev")
        op.add_column("chunks", sa.Column("embedding_prev_model", sa.String(100)))
        op.execute("UPDATE chunks SET embedding_prev_model = embedding_model")
    op.add_column("chunks", sa.Column("embedding", Vector(dim), nullable=True))
    if rows:
        op.execute("UPDATE chunks SET embedding_model = NULL, embedding_dim = NULL")
    op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade():
    """Restores the vector(1024) BGE schema. Vectors preserved by upgrade() come back byte-for-byte; OpenAI vectors are
    dropped (they are regenerable from chunk text), so chunks embedded after the upgrade become pending."""
    bind = op.get_bind()
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.drop_column("chunks", "embedding")
    if _has_col(bind, "embedding_prev"):
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding_prev TO embedding")
        op.execute("UPDATE chunks SET embedding_model = embedding_prev_model, "
                   "embedding_dim = CASE WHEN embedding IS NULL THEN NULL ELSE vector_dims(embedding) END")
        op.drop_column("chunks", "embedding_prev_model")
    else:
        op.add_column("chunks", sa.Column("embedding", Vector(PREVIOUS_DIM), nullable=True))
        op.execute("UPDATE chunks SET embedding_model = NULL, embedding_dim = NULL")
    op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
