"""RECONCILIATION revision — authored from live-schema inspection (2026-10-28), NOT the original teammate file.

Preamble for reviewers: the original 0006/0007 scripts were never committed to this checkout (the teammate's
branch was the only source, and it predates this repository's initial commit). The live database is stamped
"0007" but local Alembic cannot run without scripts for those revisions. This revision and 0007 recreate the
changes whose OUTCOME was established authoritatively by inspecting the live schema (the only verified artifact
of the original 0006/0007 executions) and diffing it against the 0001..0005 chain's output. They do NOT rewrite
history or restamp alembic_version: the live database (already at 0007) never re-executes them, and fresh
installations execute them and land on the identical schema.

What the live-schema audit established that 0001..0005 do NOT produce (all verified with read-only catalog
queries on 2026-10-28):

  * public.users table: id uuid PK, email varchar(320) (nullable, UNIQUE), display_name varchar(120),
    avatar_url text, created_at/updated_at timestamptz NOT NULL, email_confirmed_at timestamptz nullable,
    RLS enabled, no policies (intentionally — no application code queries it yet; docs/SUPABASE_INTEGRATION.md:
    "add them only when a feature needs them").
  * papers multi-tenant columns: owner_id uuid NULLABLE REFERENCES users(id) ON DELETE SET NULL,
    index ix_papers_owner_id, and the GLOBAL unique constraints on doi/content_hash (created by 0001 as
    `unique=True` columns) REPLACED by owner-scoped uniques uq_papers_owner_doi / uq_papers_owner_content_hash.
  * NOT in 0006 (belongs to 0007): conversations / messages / signup_invites / signup_allowlist.

Everything here is conditional and fail-safe: each step verifies the catalog first, anomalies RAISE (nothing is
overwritten), and running on a database whose schema already matches (like the live one, if it were ever
re-ran) is a no-op. Since the original script contents are unavailable, this downgrade restores the 0005-era
schema shape only when it is SAFE: it refuses when owner_id data would be orphaned.

Revision ID: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


def _bind():
    return op.get_bind()


def _has_col(b, table, name):
    return bool(b.execute(sa.text("SELECT 1 FROM information_schema.columns "
                                  "WHERE table_schema='public' AND table_name=:t AND column_name=:c"),
                          {"t": table, "c": name}).scalar())


def _has_table(b, table):
    return bool(b.execute(sa.text("SELECT 1 FROM information_schema.tables "
                                  "WHERE table_schema='public' AND table_name=:t"), {"t": table}).scalar())


def _has_constraint(b, table, name):
    return bool(b.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname=:n AND conrelid::regclass::text=:t"),
                          {"n": name, "t": table}).scalar())


def _missing_columns(b, table, expected: dict):
    """Fails fast if the table exists but diverges from the expected column set (added AND unexpected columns)."""
    if not _has_table(b, table):
        return
    have = {r[0] for r in b.execute(sa.text("SELECT column_name FROM information_schema.columns "
                                           "WHERE table_schema='public' AND table_name=:t"), {"t": table})}
    extra = have - set(expected) - {"owner_id"}  # owner_id is this revision's own addition
    if extra or set(expected) - have:
        raise RuntimeError(f"Table public.{table} diverges from the audited live schema "
                           f"(missing={sorted(set(expected) - have)}, unexpected={sorted(extra)}). "
                           "Refusing to run a reconciliation against an unexpected schema; inspect manually.")


def upgrade():
    b = _bind()
    _missing_columns(b, "papers", {"id", "title", "authors", "abstract", "publication_year", "doi", "source_url",
                                   "storage_key", "content_hash", "status", "error_message",
                                   "created_at", "updated_at"})
    # --- 1. users (the FK target of papers.owner_id; verified column-for-column against the live DB) ---------
    if not _has_table(b, "users"):
        op.execute(
            'CREATE TABLE users ('
            'id uuid PRIMARY KEY,'
            'email varchar(320),'
            'display_name varchar(120),'
            'avatar_url text,'
            'created_at timestamptz NOT NULL,'
            'updated_at timestamptz NOT NULL,'
            'email_confirmed_at timestamptz,'
            'CONSTRAINT users_email_key UNIQUE (email))')
        op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")  # live DB: RLS on, zero policies (unused yet)

    # --- 2. papers.owner_id ---
    if not _has_col(b, "papers", "owner_id"):
        op.add_column("papers", sa.Column("owner_id", sa.Uuid, nullable=True))
    if not _has_constraint(b, "papers", "fk_papers_owner_id_users"):
        op.execute("ALTER TABLE papers ADD CONSTRAINT fk_papers_owner_id_users "
                   "FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL")
    if not b.execute(sa.text("SELECT 1 FROM pg_indexes WHERE schemaname='public' "
                             "AND indexname='ix_papers_owner_id'")).scalar():
        op.create_index("ix_papers_owner_id", "papers", ["owner_id"])

    # --- 3. replace the global DOIs/content-hash uniques with owner-scoped ones ---
    # 0001 declared doi/content_hash globally unique (papers_doi_key / papers_content_hash_key).
    # The audited live schema has NO global uniques on those columns — only uq_papers_owner_doi and
    # uq_papers_owner_content_hash. Global uniques in Postgres are implemented as indexes, so drop them by name.
    for idx in ("papers_doi_key", "papers_content_hash_key"):
        op.execute(f'DROP INDEX IF EXISTS "{idx}"')
    for name, cols in (("uq_papers_owner_doi", "owner_id, doi"),
                       ("uq_papers_owner_content_hash", "owner_id, content_hash")):
        if not b.execute(sa.text("SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=:n"),
                         {"n": name}).scalar():
            # Named explicitly instead of op.create_index(..., unique=True) so the live index names match exactly.
            op.execute(f'CREATE UNIQUE INDEX "{name}" ON papers ({cols})')


def downgrade():
    """Restores the 0005-era shape: owner-scoped uniques dropped, global uniques rebuilt (refuses when the data
    no longer satisfies them), owner_id dropped when unused, users table dropped when unreferenced."""
    b = _bind()
    for name in ("uq_papers_owner_doi", "uq_papers_owner_content_hash"):
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
    # Rebuild the global uniques 0001 declared — but ONLY when no duplicates and NO unowned rows exist:
    # a single owner_id-bearing row makes global uniqueness semantically wrong, and duplicate NULL-owner
    # rows would violate the constraint. Refuse loudly; manual reconciliation is required in that case.
    owner_rows = b.execute(sa.text("SELECT count(*) FROM papers WHERE owner_id IS NOT NULL")).scalar() or 0
    if owner_rows:
        raise RuntimeError(f"papers.owner_id has {owner_rows} non-null values; cannot restore 0005-era global "
                           "uniqueness safely. Reconcile ownership data manually before downgrading past 0006.")
    for name, idx, col in (("papers_doi_key", "papers_doi_key", "doi"),
                           ("papers_content_hash_key", "papers_content_hash_key", "content_hash")):
        if not _has_constraint(b, "papers", name) and not b.execute(sa.text(
                "SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=:n"), {"n": idx}).scalar():
            n_dupes = b.execute(sa.text(
                f'SELECT count(*) FROM (SELECT {col} FROM papers WHERE {col} IS NOT NULL '
                f'GROUP BY {col} HAVING count(*) > 1) d')).scalar()
            if n_dupes:
                raise RuntimeError(f"papers.{col} contains duplicates; unique index '{idx}' cannot be restored.")
            op.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx}" ON papers ({col}) WHERE {col} IS NOT NULL')
    if _has_col(b, "papers", "owner_id"):
        op.drop_column("papers", "owner_id")
    # Drop users only when nothing references it anymore (papers.owner_id was just removed).
    if _has_table(b, "users"):
        refs = b.execute(sa.text("SELECT count(*) FROM pg_constraint WHERE confrelid='users'::regclass")).scalar()
        if not refs:
            op.execute("DROP TABLE IF EXISTS users")
