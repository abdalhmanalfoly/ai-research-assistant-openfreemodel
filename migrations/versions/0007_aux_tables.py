"""RECONCILIATION revision (part 2 of the 0006/0007 reconstruction) — authored from live-schema inspection
(2026-10-28), NOT the original teammate file; see 0006_ownership_schema.py's docstring for the full rationale.

Creates the auxiliary tables the audited live database has but the 0001..0005 chain never created
(docs/SUPABASE_INTEGRATION.md documents them as future-feature tables created ahead of any code):

  * conversations  (id uuid PK, user_id -> users CASCADE, title varchar(200) NOT NULL DEFAULT 'New conversation',
                    created_at/updated_at timestamptz NOT NULL)
  * messages       (id uuid PK, conversation_id -> conversations CASCADE, role varchar(20) NOT NULL,
                    content text NOT NULL, citations jsonb NOT NULL DEFAULT '[]',
                    metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL)
  * signup_invites (id uuid PK DEFAULT gen_random_uuid(), code_hash varchar(64) NOT NULL UNIQUE,
                    label varchar(120), max_uses int NOT NULL DEFAULT 1 CHECK >0, uses int NOT NULL DEFAULT 0,
                    CHECK uses>=0 AND uses<=max_uses, expires_at/revoked_at timestamptz, created_by -> users SET NULL,
                    created_at timestamptz NOT NULL DEFAULT now())
  * signup_allowlist(email varchar(320) PRIMARY KEY, invite_id -> signup_invites CASCADE, token_hash varchar(64)
                    NOT NULL, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                    CHECK email = lower(email))

All four have RLS ENABLED and NO policies on the live database (verified: pg_policies returns empty for them) —
mirrored exactly here; they are intentionally application-invisible ("no code uses them").

Every step is conditional (probe the catalog first, match the audited live shape, raise on anomalies) so the
reconciliation is converged by construction: live-like DB -> no-op; fresh install -> identical schema.

Revision ID: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"

DDL = {
    "conversations": (
        "CREATE TABLE conversations ("
        "id uuid PRIMARY KEY,"
        "user_id uuid NOT NULL,"
        "title varchar(200) NOT NULL DEFAULT 'New conversation',"
        "created_at timestamptz NOT NULL,"
        "updated_at timestamptz NOT NULL,"
        "CONSTRAINT conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)",
    ),
    "messages": (
        "CREATE TABLE messages ("
        "id uuid PRIMARY KEY,"
        "conversation_id uuid NOT NULL,"
        "role varchar(20) NOT NULL,"
        "content text NOT NULL,"
        "citations jsonb NOT NULL DEFAULT '[]'::jsonb,"
        "metadata jsonb NOT NULL DEFAULT '{}'::jsonb,"
        "created_at timestamptz NOT NULL,"
        "CONSTRAINT messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES conversations(id) "
        "ON DELETE CASCADE)",
    ),
    "signup_invites": (
        "CREATE TABLE signup_invites ("
        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
        "code_hash varchar(64) NOT NULL,"
        "label varchar(120),"
        "max_uses int NOT NULL DEFAULT 1,"
        "uses int NOT NULL DEFAULT 0,"
        "expires_at timestamptz,"
        "revoked_at timestamptz,"
        "created_by uuid,"
        "created_at timestamptz NOT NULL DEFAULT now(),"
        "CONSTRAINT signup_invites_code_hash_key UNIQUE (code_hash),"
        "CONSTRAINT ck_signup_invites_max_uses CHECK (max_uses > 0),"
        "CONSTRAINT ck_signup_invites_uses CHECK (uses >= 0 AND uses <= max_uses),"
        "CONSTRAINT signup_invites_created_by_fkey FOREIGN KEY (created_by) REFERENCES users(id) "
        "ON DELETE SET NULL)",
    ),
    "signup_allowlist": (
        "CREATE TABLE signup_allowlist ("
        "email varchar(320) PRIMARY KEY,"
        "invite_id uuid,"
        "token_hash varchar(64) NOT NULL,"
        "expires_at timestamptz NOT NULL,"
        "created_at timestamptz NOT NULL DEFAULT now(),"
        "CONSTRAINT ck_signup_allowlist_lower_email CHECK (email::text = lower(email::text)),"
        "CONSTRAINT signup_allowlist_invite_id_fkey FOREIGN KEY (invite_id) REFERENCES signup_invites(id) "
        "ON DELETE CASCADE)",
    ),
}
COLUMNS = {
    "conversations": {"id", "user_id", "title", "created_at", "updated_at"},
    "messages": {"id", "conversation_id", "role", "content", "citations", "metadata", "created_at"},
    "signup_invites": {"id", "code_hash", "label", "max_uses", "uses", "expires_at", "revoked_at",
                       "created_by", "created_at"},
    "signup_allowlist": {"email", "invite_id", "token_hash", "expires_at", "created_at"},
}


def _bind():
    return op.get_bind()


def _has_table(b, table):
    return bool(b.execute(sa.text("SELECT 1 FROM information_schema.tables "
                                  "WHERE table_schema='public' AND table_name=:t"), {"t": table}).scalar())


def upgrade():
    b = _bind()
    users_ok = _has_table(b, "users")
    for table, ddl in DDL.items():
        if not _has_table(b, table):
            if table in ("conversations", "signup_invites") and not users_ok:
                raise RuntimeError(f"public.users is missing; 0007's {table} needs it as a FK target — "
                                   "the 0006 reconciliation did not run (or was altered).")
            op.execute(ddl)
        else:  # exists: verify it matches the audited live shape instead of silently "repairing" it
            have = {r[0] for r in b.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t"), {"t": table})}
            if have != COLUMNS[table]:
                raise RuntimeError(f"public.{table} diverges from the audited live schema "
                                   f"(missing={sorted(COLUMNS[table] - have)}, "
                                   f"unexpected={sorted(have - COLUMNS[table])}); inspect manually.")
        # RLS parity with the audited live database (enabled, deliberately no policies yet).
        b.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))


def downgrade():
    """Reverse in FK-dependency order; refuse when the tables contain feature data (rows exist) that a plain
    drop would silently discard."""
    b = _bind()
    for table in ("signup_allowlist", "messages", "signup_invites", "conversations"):
        if _has_table(b, table):
            rows = b.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0
            if rows:
                raise RuntimeError(f"public.{table} holds {rows} rows; refusing to downgrade into a data-losing "
                                   "drop. Delete/archive the rows deliberately first.")
            op.execute(f"DROP TABLE {table}")
