"""Enable Row Level Security and revoke Data API roles on application tables.

Why: on Supabase, tables in the exposed `public` schema created by Alembic do NOT get RLS automatically.
Without this, the publishable (anon) key could read/write them through the Supabase Data API.
The FastAPI backend connects as the table owner (`postgres`), which is not subject to RLS (we deliberately do
NOT use FORCE ROW LEVEL SECURITY, as that would lock the backend out). On plain PostgreSQL (no anon/authenticated
roles) the REVOKE steps are skipped.

Revision ID: 0002
"""
from alembic import op

revision = "0002"
down_revision = "0001"
TABLES = ("papers", "chunks", "ingestion_jobs")


def upgrade():
    for t in TABLES:
        op.execute(f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY")
    op.execute("""
    DO $$
    DECLARE r text;
    BEGIN
      FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
          EXECUTE format('REVOKE ALL ON TABLE public.papers, public.chunks, public.ingestion_jobs FROM %I', r);
        END IF;
      END LOOP;
    END $$;
    """)


def downgrade():
    for t in TABLES:
        op.execute(f"ALTER TABLE public.{t} DISABLE ROW LEVEL SECURITY")
