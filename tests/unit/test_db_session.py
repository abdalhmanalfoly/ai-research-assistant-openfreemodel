"""Tests for the DATABASE_URL driver normalisation (Psycopg 3 coercion) in app/db/session.py.

Password-masking note: SQLAlchemy 2.x masks the password in BOTH repr() and str(); assertions therefore
compare structured components (drivername, credentials, host, port, database, query), never rendered strings.
"""
import unittest

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.errors import ConfigurationError
from app.db.session import coerce_postgres_driver, dispose_engine


def parts(url):
    q = dict(url.query)
    q.pop("password", None)
    return (url.drivername, url.username, url.password, url.host, url.port, url.database, frozenset(q.items()))


class PostgresDriverCoercionTests(unittest.TestCase):
    def test_bare_postgresql_url_becomes_psycopg3_with_everything_else_preserved(self):
        out = coerce_postgres_driver("postgresql://user:p%40ss@aws-0-eu.pooler.supabase.com:6543/postgres?sslmode=require")
        self.assertEqual(parts(make_url(str(out))),
                         ("postgresql+psycopg", "user", "p@ss", "aws-0-eu.pooler.supabase.com", 6543,
                          "postgres", frozenset({("sslmode", "require")})))

    def test_bare_postgres_url_is_also_normalised(self):
        out = make_url(coerce_postgres_driver("postgres://research:research@localhost:5432/research"))
        self.assertEqual(out.drivername, "postgresql+psycopg")
        self.assertEqual((out.username, out.host, out.port, out.database), ("research", "localhost", 5432, "research"))

    def test_explicit_psycopg_url_is_untouched(self):
        self.assertEqual(parts(make_url(coerce_postgres_driver(
            "postgresql+psycopg://postgres.X:pw@h:5432/postgres?sslmode=require"))),
            ("postgresql+psycopg", "postgres.X", "pw", "h", 5432, "postgres", frozenset({("sslmode", "require")})))

    def test_other_explicit_postgres_dialects_are_not_silently_rewritten(self):
        out = coerce_postgres_driver("postgresql+psycopg2://u:p@h:5432/db")
        self.assertEqual(make_url(out).drivername, "postgresql+psycopg2")  # untouched
        self.assertEqual(make_url(out).password, "p")                     # and not password-masked
        self.assertEqual(coerce_postgres_driver("postgresql+asyncpg://u:p@h:5432/db"),
                         "postgresql+asyncpg://u:p@h:5432/db")

    def test_non_postgres_url_fails_fast_with_a_clear_message(self):
        # the exact historical incident: the Supabase *console* URL pasted as DATABASE_URL
        with self.assertRaises(ConfigurationError):
            coerce_postgres_driver("https://uvtektqsgthsjrezqlln.supabase.co")

    def test_malformed_url_fails_fast_with_a_clear_error(self):
        with self.assertRaises(ArgumentError):
            coerce_postgres_driver("://nonsense")

    def test_password_is_masked_in_url_objects_but_preserved_in_the_returned_string(self):
        url = coerce_postgres_driver("postgres://u:SuperSecretPassword@h:5432/db")
        reparsed = make_url(url)
        self.assertNotIn("SuperSecretPassword", repr(reparsed))   # URL objects mask the password in repr/str
        self.assertNotIn("SuperSecretPassword", str(reparsed))
        self.assertEqual(reparsed.password, "SuperSecretPassword")  # the value survives inside the URL
        self.assertIn("SuperSecretPassword", url)                   # the string handed to create_engine keeps it


dispose_engine()  # safe if no engine was ever created; keeps test runs engine-free
