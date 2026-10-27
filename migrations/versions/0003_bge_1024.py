"""Move chunk embeddings to vector(1024) for BAAI/bge-large-en-v1.5.

NON-DESTRUCTIVE strategy (never reinterprets or truncates 768-dim nomic vectors):
  * chunks table EMPTY      -> drop the old 768 column/index and create vector(1024).
  * chunks table has rows   -> rename old column to `embedding_legacy` (data kept), remember the old model name in
                               `embedding_legacy_model`, add a NEW nullable vector(1024) `embedding`, and set
                               embedding_model/embedding_dim to NULL so those chunks count as "pending re-index".
    Chunk ids, text, page numbers, sections and paper relations are untouched. Search ignores NULL embeddings and
    other models' vectors. Fill the new column with:  python -m scripts.reindex_embeddings
    After you have verified retrieval you may reclaim space manually:
        ALTER TABLE chunks DROP COLUMN embedding_legacy, DROP COLUMN embedding_legacy_model;
    (deliberately NOT done by any migration).

Revision ID: 0003
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0003"
down_revision = "0002"
NEW_DIM = 1024


def _has_col(bind, name: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name='chunks' AND column_name=:c"), {"c": name}).scalar())


def upgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT count(*) FROM chunks")).scalar() or 0
    if rows == 0:
        op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
        op.drop_column("chunks", "embedding")
    else:
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding TO embedding_legacy")
        op.execute("ALTER INDEX ix_chunks_embedding_hnsw RENAME TO ix_chunks_embedding_legacy_hnsw")
        op.add_column("chunks", sa.Column("embedding_legacy_model", sa.String(100)))
        op.execute("UPDATE chunks SET embedding_legacy_model = embedding_model")
    op.add_column("chunks", sa.Column("embedding", Vector(NEW_DIM), nullable=True))
    op.alter_column("chunks", "embedding_model", existing_type=sa.String(100), nullable=True)
    op.alter_column("chunks", "embedding_dim", existing_type=sa.Integer, nullable=True)
    if rows:
        op.execute("UPDATE chunks SET embedding_model = NULL, embedding_dim = NULL")
    op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade():
    """Restores the 768 schema. BGE vectors are dropped (they are regenerable from chunk text)."""
    bind = op.get_bind()
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.drop_column("chunks", "embedding")
    if _has_col(bind, "embedding_legacy"):
        op.execute("ALTER TABLE chunks RENAME COLUMN embedding_legacy TO embedding")
        op.execute("ALTER INDEX ix_chunks_embedding_legacy_hnsw RENAME TO ix_chunks_embedding_hnsw")
        op.execute("UPDATE chunks SET embedding_model = COALESCE(embedding_legacy_model, 'unknown')")
        op.drop_column("chunks", "embedding_legacy_model")
    else:
        op.add_column("chunks", sa.Column("embedding", Vector(768)))
        op.execute("UPDATE chunks SET embedding_model = COALESCE(embedding_model, 'unknown')")
        op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
    op.execute("UPDATE chunks SET embedding_dim = 768")
    op.alter_column("chunks", "embedding_model", existing_type=sa.String(100), nullable=False)
    op.alter_column("chunks", "embedding_dim", existing_type=sa.Integer, nullable=False)
