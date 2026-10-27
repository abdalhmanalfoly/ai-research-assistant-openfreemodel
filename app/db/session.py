"""SQLAlchemy engine/session factory for the backend database (single pooled engine, module-level cache).

The DATABASE_URL may omit the driver suffix (e.g. `postgresql://...` in pasted Supabase strings). Bare
`postgresql://` URLs would make SQLAlchemy pick its first *installed* postgres DBAPI (this project ships
and must use Psycopg 3), so the URL is normalised to `postgresql+psycopg://` explicitly; everything else
(host, port, database, user, password, URL parameters) is preserved. URLs that already name a driver
(`postgresql+psycopg://`, `+psycopg2`, ...) pass through, and non-PostgreSQL URLs fail fast with a clear
configuration error.
"""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.errors import ConfigurationError

_engine: Engine | None = None
_factory: sessionmaker | None = None


def coerce_postgres_driver(database_url: str) -> str:
    """Normalise DATABASE_URL to an explicit psycopg 3 driver, preserving everything else.

    * bare `postgresql://` / `postgres://` -> `postgresql+psycopg://` (SQLAlchemy would otherwise pick
      its first *installed* postgres dialect, so name the one the project actually ships);
    * any other explicit `postgresql+<dialect>://` passes through untouched;
    * a non-PostgreSQL URL (e.g. the project's https:// Supabase console URL) fails fast with a clear
      message instead of the obscure "invalid connection option" error at engine creation.
    Never logs the URL (it contains the password)."""
    url = make_url(database_url)  # fails fast with a clear ArgumentError on malformed URLs
    dn = url.drivername
    if dn.startswith("postgresql+"):     # an explicit dialect is a deliberate choice: keep it
        return url.render_as_string(hide_password=False)
    if dn in ("postgresql", "postgres"):  # bare scheme: pin the actually-installed Psycopg 3
        return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    raise ConfigurationError(
        f"DATABASE_URL must be a PostgreSQL connection string (postgresql://...); got scheme {dn!r}. "
        "Copy the database connection string from your database host (e.g. Supabase Dashboard -> "
        "Connect -> Session pooler), not the project console URL.")


def get_session_factory() -> sessionmaker:
    global _engine, _factory
    if _factory is None:
        st = get_settings()
        connect_args = ({"prepare_threshold": None}  # required by Supabase transaction pooler (port 6543)
                        if st.db_disable_prepared_statements else {})
        _engine = create_engine(coerce_postgres_driver(st.database_url), pool_pre_ping=True,
                                pool_size=st.db_pool_size, max_overflow=st.db_max_overflow,
                                connect_args=connect_args)
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _factory


def dispose_engine() -> None:
    """Release the cached engine (e.g. between test runs / process shutdown)."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine, _factory = None, None


@contextmanager
def session_scope():
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
