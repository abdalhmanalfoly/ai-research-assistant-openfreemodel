"""papers, chunks, ingestion_jobs, pgvector HNSW index

Revision ID: 0001
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB


revision = "0001"
down_revision = None


def upgrade():
    dim = 768  # historical: 0001 was created for nomic-embed-text (768). Migration 0003 moves to 1024 (BGE).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    ts = lambda n: sa.Column(n, sa.DateTime(timezone=True), nullable=False)
    op.create_table(
        "papers",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("authors", JSONB, nullable=False),
        sa.Column("abstract", sa.Text),
        sa.Column("publication_year", sa.Integer),
        sa.Column("doi", sa.String(255), unique=True),
        sa.Column("source_url", sa.Text),
        sa.Column("storage_key", sa.Text),
        sa.Column("content_hash", sa.String(64), unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text),
        ts("created_at"), ts("updated_at"),
    )
    op.create_index("ix_papers_publication_year", "papers", ["publication_year"])
    op.create_index("ix_papers_status", "papers", ["status"])
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("paper_id", sa.Uuid, sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("page_number", sa.Integer),
        sa.Column("section", sa.String(100)),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("embedding", Vector(dim)),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("embedding_dim", sa.Integer, nullable=False),
        ts("created_at"),
        sa.UniqueConstraint("paper_id", "chunk_index"),
    )
    op.create_index("ix_chunks_paper_id", "chunks", ["paper_id"])
    op.create_index("ix_chunks_embedding_model", "chunks", ["embedding_model"])
    op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("paper_id", sa.Uuid, sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text),
        ts("created_at"), ts("updated_at"),
    )
    op.create_index("ix_ingestion_jobs_paper_id", "ingestion_jobs", ["paper_id"])


def downgrade():
    op.drop_table("ingestion_jobs")
    op.drop_table("chunks")
    op.drop_table("papers")
