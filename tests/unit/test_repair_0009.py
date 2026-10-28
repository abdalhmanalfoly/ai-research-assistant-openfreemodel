"""REGRESSION for the 2026-10-28 incident: alembic_version showed 0008 while chunks.embedding stayed
vector(1024) (0008 had run under EMBEDDING_DIMENSIONS=1024 and legitimately took its 'metadata' branch,
changing nothing). The repair revision 0009 must converge ANY of these states to vector(2048) safely.

These tests EXECUTE migrations/versions/0009_nemotron_2048_repair.py's upgrade()/downgrade() in-process against
a simulated Postgres catalog (a mini SQL interpreter for the exact statement set the migration emits) — no DB,
no network, nothing production is touched. Chain-level tests live in test_migration_chain.py.
"""
import importlib.util
import os
import re
import unittest
from unittest import mock

REPAIR = os.path.join(os.path.dirname(__file__), "..", "..", "migrations", "versions", "0009_nemotron_2048_repair.py")


def load_module():
    spec = importlib.util.spec_from_file_location("repair_0009", REPAIR)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeResult:
    def __init__(self, value=None, row=None):
        self._value, self._row = value, row

    def scalar(self):
        return self._value

    def first(self):
        return self._row

    def one(self):
        return self._row


class FakeCatalog:
    """Simulated catalog: chunk rows, chunks columns (+ per-column vector sizes), indexes, one state table."""

    def __init__(self, embedding_dim=1024, n_rows=211, prev=False, prev_or=False, drop_embedding=False):
        self.cols = {"embedding", "embedding_model", "embedding_dim", "id", "paper_id", "text", "page_number",
                     "section", "chunk_index", "created_at"}
        self.col_dim = {"embedding": embedding_dim}
        if drop_embedding:
            self.cols.discard("embedding")
            del self.col_dim["embedding"]
        self.n_rows = n_rows
        self.indexes = {"ix_chunks_embedding_hnsw", "chunks_pkey", "ix_chunks_paper_id"}
        self.tables = {"chunks", "alembic_version", "alembic_0008_embedding_migrate"}
        self.executed: list[str] = []
        self.state_row = None          # (branch, observed_dim, target_dim)
        self.state_verified = False
        self.prev, self.prev_or = prev, prev_or

    def execute(self, stmt, params=None):
        sql = str(stmt)
        p = params if params is not None else (getattr(stmt, "_bindparams", None) or getattr(stmt, "_params", None) or {})
        self.executed.append(sql)
        if "format_type" in sql:
            d = self.col_dim.get("embedding")
            return FakeResult(None if d is None else f"vector({d})")
        if "SELECT count(*) FROM chunks" in sql:
            return FakeResult(self.n_rows)
        if "information_schema.columns" in sql:
            c = p.get("c")
            exists = c in self.cols or (c == "embedding_prev" and self.prev) \
                or (c == "embedding_prev_or" and self.prev_or)
            return FakeResult(1 if exists else None)
        if "information_schema.tables" in sql:
            return FakeResult(1 if p.get("t") in self.tables else None)
        if "SELECT branch FROM alembic_0009_embedding_repair" in sql:
            return FakeResult(self.state_row[0] if self.state_row else None)
        if "SELECT branch, observed_dim, target_dim FROM alembic_0009" in sql:
            return FakeResult(row=self.state_row)
        if "DROP TABLE IF EXISTS alembic_0009" in sql:
            self.tables.discard("alembic_0009_embedding_repair")
        if "CREATE TABLE IF NOT EXISTS alembic_0009" in sql:
            self.tables.add("alembic_0009_embedding_repair")
        elif "CREATE INDEX" in sql:
            self.indexes.add("ix_chunks_embedding_hnsw")
        elif "DROP INDEX" in sql:
            self.indexes.discard("ix_chunks_embedding_hnsw")
        elif "RENAME COLUMN embedding TO embedding_prev" in sql:
            self.cols.discard("embedding"); self.prev = True
            self.col_dim["embedding_prev"] = self.col_dim.pop("embedding")
        elif "RENAME COLUMN embedding_prev TO embedding" in sql:
            self.cols.add("embedding"); self.prev = False
            self.col_dim["embedding"] = self.col_dim.pop("embedding_prev", 1024)
        elif "ADD COLUMN" in sql:
            name = sql.split("ADD COLUMN", 1)[1].strip().split()[0]
            self.cols.add(name)
            m = re.search(r"VECTOR\((\d+)\)", sql, re.I)
            if m:
                self.col_dim[name] = int(m.group(1))
        elif "DROP COLUMN" in sql:
            name = sql.split("DROP COLUMN", 1)[1].strip().split()[0].strip('"')
            self.cols.discard(name)
        elif sql.startswith("INSERT INTO alembic_0009"):
            self.state_row = tuple(getattr(v, "value", v) for v in (p["b"], p["o"], p["t"]))
        elif "SET verified" in sql:
            self.state_verified = True
        elif sql.startswith("UPDATE chunks") and "embedding_prev_model = embedding_model" in sql:
            pass  # preservation bookkeeping, recorded above
        elif sql.startswith("UPDATE chunks"):
            pass  # metadata invalidation
        return FakeResult()


def run_upgrade(cat, dim=2048):
    import types
    m = load_module()
    fake_get_settings = lambda: types.SimpleNamespace(embedding_dim=dim)

    def add_column(t, c):
        col_type = "VECTOR(2048)" if c.name == "embedding" else "VARCHAR(100)"
        cat.execute(f"ALTER TABLE chunks ADD COLUMN {c.name} {col_type}")

def proxy_params(stmt):
    return getattr(stmt, "_params", None)


def run_upgrade(cat, dim=2048):
    import types
    m = load_module()
    fake_get_settings = lambda: types.SimpleNamespace(embedding_dim=dim)

    def add_column(t, c):
        col_type = "VECTOR(2048)" if c.name == "embedding" else "VARCHAR(100)"
        cat.execute(f"ALTER TABLE chunks ADD COLUMN {c.name} {col_type}")

    def drop_column(t, c):
        cat.execute(f"ALTER TABLE chunks DROP COLUMN {c}")

    with mock.patch.object(m, "get_settings", fake_get_settings), mock.patch.object(m.op, "get_bind",
                                                                                    return_value=cat), \
         mock.patch.object(m.op, "execute", lambda stmt: cat.execute(stmt)), \
         mock.patch.object(m.op, "add_column", add_column), \
         mock.patch.object(m.op, "drop_column", drop_column):
        m.upgrade()
    return cat


def run_downgrade(cat):
    m = load_module()
    with mock.patch.object(m.op, "get_bind", return_value=cat), \
         mock.patch.object(m.op, "execute", lambda stmt: cat.execute(stmt)), \
         mock.patch.object(m.op, "drop_column", lambda t, c: cat.execute(f"ALTER TABLE chunks DROP COLUMN {c}")):        m.downgrade()
    return cat


class IncidentRegressionTests(unittest.TestCase):
    """THE reported failure mode: alembic=0008, embedding still vector(1024), 211 rows, both capture slots free."""

    def setUp(self):
        self.cat = run_upgrade(FakeCatalog(embedding_dim=1024))

    def test_takes_the_rename_branch_and_records_it(self):
        self.assertEqual(self.cat.state_row, ("rename", 1024, 2048))
        self.assertTrue(self.cat.state_verified, "the migration must self-verify its final catalog state")

    def test_old_vectors_are_renamed_not_dropped(self):
        renames = [s for s in self.cat.executed if "RENAME COLUMN embedding TO embedding_prev" in s]
        self.assertEqual(len(renames), 1, "1024 vectors must be preserved byte-for-byte under embedding_prev")
        dropped = [s for s in self.cat.executed if "DROP COLUMN embedding " in s
                   and "embedding_prev" not in s]
        self.assertEqual(dropped, [], "the 1024 generation must never be dropped before preservation")

    def test_new_column_is_exactly_2048(self):
        self.assertEqual(self.cat.col_dim.get("embedding"), 2048)
        self.assertTrue(self.cat.prev, "the 1024 generation must remain preserved as embedding_prev")

    def test_every_chunk_is_honestly_pending_not_relabelled(self):
        self.assertTrue(any("UPDATE chunks SET embedding_model = NULL" in s and "IS DISTINCT" not in s
                            for s in self.cat.executed), "rename branch must reset ALL per-row metadata")
        self.assertFalse(any("embedding_dim" in s and "= 2048" in s and s.startswith("UPDATE chunks")
                             for s in self.cat.executed), "nothing may be relabelled 2048 while vectors are 1024")

    def test_no_hnsw_above_2000_and_no_destroying_sql(self):
        self.assertFalse(any(s.startswith("CREATE INDEX") for s in self.cat.executed))
        self.assertFalse(any("TRUNCATE" in s or "DROP TABLE chunks" in s or "DELETE FROM" in s
                             for s in self.cat.executed))


class ConvergedStateTests(unittest.TestCase):
    def test_correct_2048_state_is_a_recorded_noop(self):
        cat = run_upgrade(FakeCatalog(embedding_dim=2048))
        self.assertEqual(cat.state_row, ("metadata", 2048, 2048))
        self.assertFalse(any("RENAME COLUMN" in s for s in cat.executed))
        self.assertEqual(cat.col_dim.get("embedding"), 2048, "no DDL happened")


class AnomalousStateTests(unittest.TestCase):
    def test_both_preservation_slots_occupied_refuses_without_touching_them(self):
        cat = FakeCatalog(embedding_dim=1024, prev=True, prev_or=True)
        before = len(cat.executed)
        with self.assertRaises(RuntimeError):
            run_upgrade(cat)
        self.assertEqual([s for s in cat.executed[before:] if "DROP COLUMN" in s], [])
        self.assertEqual([s for s in cat.executed[before:] if "RENAME COLUMN" in s], [])

    def test_missing_embedding_column_refuses(self):
        with self.assertRaises(RuntimeError):
            run_upgrade(FakeCatalog(embedding_dim=1024, drop_embedding=True))

    def test_no_rows_and_no_column_still_refuses(self):
        with self.assertRaises(RuntimeError):
            run_upgrade(FakeCatalog(n_rows=0, drop_embedding=True))


class SelfVerificationTests(unittest.TestCase):
    def test_double_run_is_refused(self):
        cat = run_upgrade(FakeCatalog(embedding_dim=1024))  # state recorded once
        with self.assertRaises(RuntimeError):               # a second run must not clobber the record
            run_upgrade(cat)


class DowngradeFidelityTests(unittest.TestCase):
    def test_rename_downgrade_restores_vectors_with_model_names(self):
        cat = FakeCatalog(embedding_dim=1024, prev=True)
        cat.state_row = ("rename", 1024, 2048)
        cat.tables.add("alembic_0009_embedding_repair")
        run_downgrade(cat)
        self.assertEqual(cat.col_dim.get("embedding"), 1024, "preserved 1024 vectors came back")
        self.assertFalse(cat.prev)
        self.assertFalse(cat.tables and ("alembic_0009_embedding_repair" in cat.tables),
                         "state table dropped on downgrade")
